param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding

$projectRoot = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
$logDir = Join-Path $projectRoot "output\logs"
$logFile = Join-Path $logDir "hermes_gateway_task.log"
$fallbackLogFile = Join-Path $env:TEMP "investment_dashboard_hermes_gateway.log"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"
$gatewayCommand = "if pgrep -f '/home/renne/.local/bin/hermes gateway run$' >/dev/null 2>&1; then echo gateway_already_running; else mkdir -p /home/renne/.hermes/logs; setsid -f /home/renne/.local/bin/hermes gateway run >>/home/renne/.hermes/logs/gateway_task_process.log 2>&1 < /dev/null; echo gateway_started; fi"
$arguments = "-d Ubuntu -- /bin/bash -lc `"$gatewayCommand`""

if ($DryRun) {
    Write-Output "Action: $wsl $arguments"
    exit 0
}

function Write-TaskLog([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch {
        Add-Content -LiteralPath $fallbackLogFile -Value $line -Encoding UTF8
    }
}

$exitCode = 1
try {
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $wsl
    $processInfo.Arguments = $arguments
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::Start($processInfo)
    if ($null -eq $process) {
        throw "Failed to start Hermes Gateway in WSL."
    }
    Write-TaskLog "event=start windows_pid=$($process.Id)"
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    Write-TaskLog "event=exit exit_code=$exitCode"
} catch {
    Write-TaskLog "event=error type=$($_.Exception.GetType().Name) message=$($_.Exception.Message)"
    $exitCode = 1
}

exit $exitCode
