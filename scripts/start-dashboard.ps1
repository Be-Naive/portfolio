$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TmpDir = Join-Path $Root "tmp"
$StatePath = Join-Path $TmpDir "dashboard-watchdog.json"
$StopPath = Join-Path $TmpDir "dashboard-watchdog.stop"
$WatchdogScript = Join-Path $PSScriptRoot "dashboard-watchdog.ps1"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PortCandidates = 8000..8010

function Test-Dashboard {
    param([int]$Port)

    try {
        $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/dashboard" -TimeoutSec 3
        return ($null -ne $payload.summary -and $null -ne $payload.timeseries)
    }
    catch {
        return $false
    }
}

function Test-PortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

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

function Open-Dashboard {
    param([int]$Port)

    $url = "http://127.0.0.1:$Port/"
    Start-Process $url
    Write-Host "Portfolio dashboard is running at $url"
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null

if (Test-Path -LiteralPath $StatePath) {
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        $watchdog = Get-Process -Id ([int]$state.watchdogPid) -ErrorAction SilentlyContinue
        if ($null -ne $watchdog -and (Test-Dashboard -Port ([int]$state.port))) {
            Open-Dashboard -Port ([int]$state.port)
            exit 0
        }

        Set-Content -LiteralPath $StopPath -Value "stop" -Encoding ASCII
        Stop-MatchingProcess -ProcessId ([int]$state.childPid) -CommandPattern 'python(.exe)?"?\s+run\.py'
        Stop-MatchingProcess -ProcessId ([int]$state.watchdogPid) -CommandPattern "dashboard-watchdog\.ps1"
    }
    catch {
        # A stale or partially written state file should not block recovery.
    }
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}

$preferredPort = $null
foreach ($candidate in $PortCandidates) {
    if (-not (Test-Dashboard -Port $candidate)) {
        continue
    }

    $preferredPort = $candidate
    $connection = Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $connection) {
        Stop-MatchingProcess -ProcessId ([int]$connection.OwningProcess) -CommandPattern 'python(.exe)?"?\s+run\.py'
        Start-Sleep -Milliseconds 750
    }
    break
}

$port = $null
if ($null -ne $preferredPort -and (Test-PortAvailable -Port $preferredPort)) {
    $port = $preferredPort
}
else {
    foreach ($candidate in $PortCandidates) {
        if (Test-PortAvailable -Port $candidate) {
            $port = $candidate
            break
        }
    }
}

if ($null -eq $port) {
    throw "No free dashboard port was found between 8000 and 8010."
}

Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$WatchdogScript`" -Port $port"
$watchdog = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($watchdog.HasExited) {
        break
    }
    if (Test-Dashboard -Port $port) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    $errorLog = Join-Path $TmpDir "dashboard-server.stderr.log"
    if (Test-Path -LiteralPath $errorLog) {
        Get-Content -LiteralPath $errorLog -Tail 20 | Write-Host
    }
    throw "Dashboard did not become ready on port $port."
}

Open-Dashboard -Port $port
