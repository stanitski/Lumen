param(
    [string]$BaseUrl = "http://127.0.0.1:8010"
)

$ErrorActionPreference = "Stop"

function Invoke-LumenPost {
    param(
        [string]$Path,
        [hashtable]$Body
    )

    return Invoke-RestMethod `
        -Method Post `
        -Uri ($BaseUrl.TrimEnd("/") + $Path) `
        -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Depth 8)
}

function Invoke-LumenGet {
    param(
        [string]$Path
    )

    return Invoke-RestMethod `
        -Method Get `
        -Uri ($BaseUrl.TrimEnd("/") + $Path)
}

Write-Host "== Health =="
$health = Invoke-LumenGet -Path "/health/system"
$health | ConvertTo-Json -Depth 8

Write-Host "`n== Reindex =="
$reindex = Invoke-LumenPost -Path "/admin/reindex" -Body @{
    paths = @("./data/knowledge")
}
$reindex | ConvertTo-Json -Depth 8

Write-Host "`n== Bootstrap Home Assistant Snapshot =="
$bootstrap = Invoke-LumenPost -Path "/admin/bootstrap-home-assistant" -Body @{}
$bootstrap | ConvertTo-Json -Depth 8

Write-Host "`n== Ask =="
$ask = Invoke-LumenPost -Path "/assist/process" -Body @{
    text = "turn on guest mode"
    conversation_id = "smoke-assist-1"
    user_id = "smoke-test"
    session_id = "smoke-session-1"
    language = "uk"
    exposed_entities = @(
        "input_boolean.guest_mode",
        "script.turnoffeverything"
    )
}
$ask | ConvertTo-Json -Depth 8

if ($ask.requires_confirmation -and $ask.action_id) {
    Write-Host "`n== Confirm =="
    $confirm = Invoke-LumenPost -Path "/assist/confirm" -Body @{
        action_id = $ask.action_id
        confirmed = $true
        conversation_id = "smoke-assist-1"
        user_id = "smoke-test"
    }
    $confirm | ConvertTo-Json -Depth 8
}
else {
    Write-Host "`nNo pending action returned by /assist/process."
}
