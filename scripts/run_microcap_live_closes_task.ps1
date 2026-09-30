param(
    [switch]$DryRun
)

# 收盘后保存微盘实盘持仓股票的当日正式收盘价；本地已完整时脚本自身不联网。
# 只在失败或仍有缺失时弹出通知，成功不打扰。
$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$taskTitle = "微盘实盘收盘价"
$projectRoot = "\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
$logDir = Join-Path $projectRoot "output\logs"
$logFile = Join-Path $logDir "microcap_live_closes.log"
$fallbackLogFile = Join-Path $env:TEMP "investment_dashboard_microcap_live_closes.log"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

$pythonCommand = "cd /home/renne/investment_dashboard && exec /usr/bin/timeout --signal=TERM --kill-after=10s 15m .venv/bin/python scripts/update_microcap_live_closes.py"
if ($DryRun) {
    $pythonCommand += " --dry-run"
}
$arguments = "-d Ubuntu -- /bin/bash -lc `"$pythonCommand`""

function Show-FailureNotification {
    param([string]$Text)
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Warning
    $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
    $icon.BalloonTipTitle = "$taskTitle 未完成"
    $icon.BalloonTipText = if ($Text.Length -gt 250) { $Text.Substring(0, 250) + "…" } else { $Text }
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000)
    Start-Sleep -Seconds 8
    $icon.Dispose()
}

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
    $combined = "任务执行异常：$($_.Exception.GetType().Name): $($_.Exception.Message)"
    $exitCode = 1
} finally {
    if (-not $combined) {
        $combined = "无输出。"
    }
    $logLine = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit_code=$exitCode; output=$combined"
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Add-Content -LiteralPath $logFile -Value $logLine -Encoding UTF8
    } catch {
        Add-Content -LiteralPath $fallbackLogFile -Value $logLine -Encoding UTF8
    }
}
if ($exitCode -ne 0 -and -not $DryRun) {
    try { Show-FailureNotification -Text $combined } catch { }
}
Write-Output $combined
exit $exitCode
