param(
    [switch]$NoNotify,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$taskTitle = "微盘股真实成分快照"
$projectRoot = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
$logDir = Join-Path $projectRoot "output\logs"
$logFile = Join-Path $logDir "microcap_snapshot_task.log"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

function Write-TaskLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Show-TaskNotification {
    param(
        [string]$Title,
        [string]$Text,
        [ValidateSet("Info", "Warning", "Error")]
        [string]$Level = "Info"
    )

    if ($NoNotify) {
        return
    }

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $icon = New-Object System.Windows.Forms.NotifyIcon
    if ($Level -eq "Error") {
        $icon.Icon = [System.Drawing.SystemIcons]::Error
        $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Error
    } elseif ($Level -eq "Warning") {
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
    } else {
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
    }

    $icon.BalloonTipTitle = $Title
    $icon.BalloonTipText = $Text
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000)
    Start-Sleep -Seconds 8
    $icon.Dispose()
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if ($DryRun) {
    Write-TaskLog "dry_run=ok"
    Write-Output "微盘股真实成分快照任务包装脚本检查通过。"
    exit 0
}

try {
    $arguments = "-d Ubuntu -- /home/renne/investment_dashboard/.venv/bin/python /home/renne/investment_dashboard/scripts/update_microcap_snapshot.py --require-today"
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $wsl
    $processInfo.Arguments = $arguments
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::Start($processInfo)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    $combined = (($stdout, $stderr) -join "`n").Trim()
    if (-not $combined) {
        $combined = "无输出。"
    }

    Write-TaskLog "exit_code=$($process.ExitCode); output=$combined"

    if ($process.ExitCode -ne 0) {
        Show-TaskNotification -Title "$taskTitle 失败" -Text $combined -Level "Error"
        exit $process.ExitCode
    }

    if ($combined -like "*跳过*") {
        Show-TaskNotification -Title "$taskTitle 已跳过" -Text $combined -Level "Warning"
    } else {
        Show-TaskNotification -Title "$taskTitle 更新成功" -Text $combined -Level "Info"
    }
    exit 0
} catch {
    $message = "任务执行异常：$($_.Exception.Message)"
    Write-TaskLog $message
    Show-TaskNotification -Title "$taskTitle 失败" -Text $message -Level "Error"
    exit 1
}