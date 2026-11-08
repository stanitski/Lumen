@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m lumen.main %*
) else (
    python -m lumen.main %*
)
set "LUMEN_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %LUMEN_EXIT_CODE%
