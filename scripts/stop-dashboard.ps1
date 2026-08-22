$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TmpDir = Join-Path $Root "tmp"
$StatePath = Join-Path $TmpDir "dashboard-watchdog.json"
$StopPath = Join-Path $TmpDir "dashboard-watchdog.stop"

function Stop-MatchingProcess {
    param(
        [int]$ProcessId,
        [string]$CommandPattern
    )

    if ($ProcessId -le 0) {
        return
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -ne $process -and $process.CommandLine -match $CommandPattern) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $StatePath)) {
    Write-Host "Portfolio dashboard watchdog is not running."
    exit 0
}

$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
Set-Content -LiteralPath $StopPath -Value "stop" -Encoding ASCII
Stop-MatchingProcess -ProcessId ([int]$state.childPid) -CommandPattern 'python(.exe)?"?\s+run\.py'

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($null -eq (Get-Process -Id ([int]$state.watchdogPid) -ErrorAction SilentlyContinue)) {
        break
    }
}

Stop-MatchingProcess -ProcessId ([int]$state.watchdogPid) -CommandPattern "dashboard-watchdog\.ps1"
Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
Write-Host "Portfolio dashboard stopped."
