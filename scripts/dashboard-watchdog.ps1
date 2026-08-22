param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$Port
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TmpDir = Join-Path $Root "tmp"
$StatePath = Join-Path $TmpDir "dashboard-watchdog.json"
$StopPath = Join-Path $TmpDir "dashboard-watchdog.stop"
$WatchdogLog = Join-Path $TmpDir "dashboard-watchdog.log"
$StdoutLog = Join-Path $TmpDir "dashboard-server.stdout.log"
$StderrLog = Join-Path $TmpDir "dashboard-server.stderr.log"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$MutexName = "Local\CrossBrokerPortfolioDashboardWatchdog"
$child = $null
$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, $MutexName, [ref]$createdNew)

if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

function Write-State {
    param([int]$ChildPid)

    $state = [ordered]@{
        watchdogPid = $PID
        childPid = $ChildPid
        port = $Port
        url = "http://127.0.0.1:$Port/"
        startedAt = (Get-Date).ToString("o")
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
    Set-Location -LiteralPath $Root
    $env:HOST = "127.0.0.1"
    $env:PORT = [string]$Port

    while (-not (Test-Path -LiteralPath $StopPath)) {
        $child = Start-Process `
            -FilePath $Python `
            -ArgumentList "run.py" `
            -WorkingDirectory $Root `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -PassThru
        Write-State -ChildPid $child.Id
        $child.WaitForExit()

        if (Test-Path -LiteralPath $StopPath) {
            break
        }

        $message = "{0} server exited with code {1}; restarting in 2 seconds" -f (
            Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        ), $child.ExitCode
        Add-Content -LiteralPath $WatchdogLog -Value $message -Encoding UTF8
        Start-Sleep -Seconds 2
    }
}
finally {
    if ($null -ne $child -and -not $child.HasExited) {
        $child.Kill()
        $child.WaitForExit(5000) | Out-Null
    }

    if (Test-Path -LiteralPath $StatePath) {
        try {
            $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
            if ([int]$state.watchdogPid -eq $PID) {
                Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
