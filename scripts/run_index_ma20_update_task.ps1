param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$projectRoot = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
$logDir = Join-Path $projectRoot "output\logs"
$logFile = Join-Path $logDir "index_ma20_scheduled.log"
$fallbackLogFile = Join-Path $env:TEMP "investment_dashboard_index_ma20_scheduled.log"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

$pythonCommand = "cd /home/renne/investment_dashboard && exec .venv/bin/python scripts/update_index_ma20_scheduled.py"
if ($DryRun) {
    $pythonCommand += " --dry-run"
}
$arguments = "-d Ubuntu -- /bin/bash -lc `"$pythonCommand`""

$exitCode = 1
$combined = ""
try {
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $wsl
    $processInfo.Arguments = $arguments
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $processInfo.StandardOutputEncoding = $utf8
    $processInfo.StandardErrorEncoding = $utf8

    $process = [System.Diagnostics.Process]::Start($processInfo)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $combined = (($stdout, $stderr) -join "`n").Trim()
    $exitCode = $process.ExitCode
} catch {
    $combined = "Wrapper failure: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    $exitCode = 1
} finally {
    if (-not $combined) {
        $combined = "No output."
    }
    $logLine = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit_code=$exitCode; output=$combined"
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Add-Content -LiteralPath $logFile -Value $logLine -Encoding UTF8
    } catch {
        Add-Content -LiteralPath $fallbackLogFile -Value $logLine -Encoding UTF8
    }
}
Write-Output $combined
exit $exitCode
