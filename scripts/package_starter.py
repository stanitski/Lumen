"""Build a source starter ZIP from an allowlist, never from local runtime data."""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import zipfile


ROOT = Path(__file__).resolve().parents[1]

OLLAMA_SETUP = r'''# Run from PowerShell after SETUP.bat and editing .env.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath '.env')) {
    throw 'Run SETUP.bat and configure .env first.'
}
$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
$configCode = @'
import json
from lumen.config import Settings
s = Settings()
models = [s.ollama_model, s.ollama_dialogue_model, s.ollama_embedding_model]
if s.nutrition_enabled:
    models.append(s.nutrition_model)
print(json.dumps(dict(url=s.ollama_base_url, models=list(dict.fromkeys(x.strip() for x in models if x.strip())))))
'@
$configJson = $configCode | & $python -
if ($LASTEXITCODE -ne 0) { throw 'Could not read Lumen configuration.' }
$config = $configJson | ConvertFrom-Json
$base = [uri]$config.url
if ($base.Scheme -ne 'http' -or $base.Host -notin @('localhost', '127.0.0.1', '[::1]') -or $base.AbsolutePath -ne '/' -or $base.Query -or $base.UserInfo) {
    throw 'This installer supports a local HTTP Ollama server only. Configure remote servers separately.'
}
foreach ($model in $config.models) {
    if ($model -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$') { throw 'Invalid model name in .env.' }
}
function Find-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    return $null
}
$ollama = Find-Ollama
if (!$ollama) {
    if (!(Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Install Ollama from https://ollama.com/download/windows then run this script again.'
    }
    & winget.exe install --id Ollama.Ollama --exact --source winget
    if ($LASTEXITCODE -ne 0) { throw 'Ollama installation failed. See the winget output.' }
    $ollama = Find-Ollama
    if (!$ollama) { throw 'Ollama executable not found. Reopen PowerShell and try again.' }
}
# This process variable keeps CLI downloads on the same server as Lumen.
$env:OLLAMA_HOST = $base.Authority
$tagsUrl = $config.url.TrimEnd('/') + '/api/tags'
function Test-Ollama {
    try {
        $null = Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 2
        return $true
    } catch { return $false }
}
if (!(Test-Ollama)) {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (Test-Ollama) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (!$ready) { throw 'Ollama API did not become ready. Check Ollama logs and OLLAMA_BASE_URL.' }
}
foreach ($model in $config.models) {
    Write-Host "Downloading model: $model"
    & $ollama pull $model
    if ($LASTEXITCODE -ne 0) { throw "Model download failed: $model. Check name, internet and disk space; then rerun." }
}
& $ollama list
if ($LASTEXITCODE -ne 0) { throw 'Could not list installed models.' }
Write-Host 'Ollama is ready. Start Lumen with START.bat and test /api/generate.'
'''


def build_package(output: Path) -> int:
    overrides = {
        "LUMEN_USER_ID": "local-user",
        "LUMEN_HOST": "127.0.0.1",
        "LOG_LEVEL": "INFO",
        "HOME_ASSISTANT_URL": "",
        "HOME_ASSISTANT_TOKEN": "",
        "HOME_ASSISTANT_WEBHOOK_ID": secrets.token_hex(24),
        "HOME_ASSISTANT_WEBHOOK_URL": "",
        "TELEGRAM_API_ID": "0",
        "TELEGRAM_API_HASH": "",
        "NUTRITION_ENABLED": "false",
    }
    template = ROOT.joinpath(".env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        key, separator, _ = line.partition("=")
        lines.append(f"{key}={overrides[key]}" if separator and key in overrides else line)
    source = sorted(ROOT.joinpath("src/lumen").rglob("*.py"))
    source = [path for path in source if "__pycache__" not in path.parts]
    source += [ROOT / "src/lumen/data/zoneinfo/Europe/Kyiv", ROOT / "pyproject.toml"]
    generated = {
        ".env.example": "\n".join(lines) + "\n",
        "README.md": ROOT.joinpath("docs/DISTRIBUTION.md").read_text(encoding="utf-8"),
        "AGENTS.md": "# Lumen transfer\n\nRead README.md before configuring, building or starting Lumen.\n"
        "Use src/lumen/main.py and src/lumen/config.py as runtime truth.\n"
        "Never copy another user's .env, databases, Telegram sessions or credentials.\n"
        "Do not overwrite local settings or change the schema for initial setup.\n",
        "SETUP_OLLAMA.ps1": OLLAMA_SETUP,
        "BUILD.bat": '''@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\\Scripts\\python.exe" goto failed
".venv\\Scripts\\python.exe" -m PyInstaller --onefile --name Lumen --paths src --add-data "src\\lumen\\data;lumen\\data" --collect-all trafilatura src\\lumen\\main.py
if errorlevel 1 goto failed
echo Built dist\\Lumen.exe. Keep dist inside this folder; .env and data stay in its parent.
popd
exit /b 0
:failed
echo Build failed. Run SETUP.bat then .venv\\Scripts\\python.exe -m pip install pyinstaller
popd
exit /b 1
''',
        "SETUP.bat": '''@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\\Scripts\\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\\Scripts\\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto failed
rem Editable install retains the bundled timezone asset in src.
rem FastAPI File/Form endpoints require python-multipart in addition to project dependencies.
".venv\\Scripts\\python.exe" -m pip install -e . python-multipart
if errorlevel 1 goto failed
if not exist ".env" copy /y ".env.example" ".env" >nul
if errorlevel 1 goto failed
echo Setup complete. Edit .env, prepare Ollama models, then run START.bat.
popd
exit /b 0
:failed
echo Setup failed. Python 3.11+ and internet access are required. See README.md.
popd
exit /b 1
''',
        "START.bat": '''@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\\Scripts\\python.exe" goto missing
if not exist ".env" goto missing
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
".venv\\Scripts\\python.exe" -m lumen.main
set "LUMEN_START_EXIT=%ERRORLEVEL%"
popd
exit /b %LUMEN_START_EXIT%
:missing
echo Run SETUP.bat and configure .env first.
popd
exit /b 1
''',
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an existing deliverable from accidental replacement.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source:
            archive.write(path, "Lumen/" + path.relative_to(ROOT).as_posix())
        for name, content in generated.items():
            if name.endswith(".bat"):
                content = content.replace("\n", "\r\n")
            archive.writestr("Lumen/" + name, content.encode("utf-8"))
    return len(source) + len(generated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New ZIP path (must not already exist)")
    args = parser.parse_args()
    print(f"Created {args.output.resolve()} ({build_package(args.output)} files)")
