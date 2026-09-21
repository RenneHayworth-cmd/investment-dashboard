param([switch]$DryRun)
$ErrorActionPreference = "Stop"
$arguments = "-d Ubuntu -- bash -lc ""cd /home/renne/investment_dashboard && .venv/bin/python scripts/update_microcap_rotation.py --scheduled --refresh"""
if ($DryRun) { $arguments = "-d Ubuntu -- bash -lc ""cd /home/renne/investment_dashboard && .venv/bin/python scripts/update_microcap_rotation.py --preflight""" }
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = Join-Path $env:WINDIR "System32\wsl.exe"
$info.Arguments = $arguments
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$process = [System.Diagnostics.Process]::Start($info)
$outTask = $process.StandardOutput.ReadToEndAsync()
$errTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$logDir = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\output\logs"
[IO.Directory]::CreateDirectory($logDir) | Out-Null
$logFile = Join-Path $logDir "microcap_rotation_task.log"
$line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$($process.ExitCode) $($outTask.Result) $($errTask.Result)"
Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
Write-Output $line
exit $process.ExitCode
