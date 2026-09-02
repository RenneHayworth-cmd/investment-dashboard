param(
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard ETF Timing Trade Alert"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wrapper = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_position_timing_trade_alert_task.ps1"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Output "Removed scheduled task: $taskName"
    exit 0
}

$actionArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""
$triggerTimes = @("09:45", "11:45", "14:45", "14:50", "14:54")
if ($DryRun) {
    Write-Output "Task: $taskName"
    Write-Output "Triggers: $($triggerTimes -join ', ') on every calendar day; the Python script skips non-trading days"
    Write-Output "Action: $powerShell $actionArguments"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $actionArguments
$triggers = $triggerTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Send ServerChan and Hermes Weixin trade quantities for the fixed 500,000-yuan ETF MA strategy at 09:45, 11:45, 14:45, 14:50, and 14:54 on A-share trading days." `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "TaskName=$($task.TaskName)"
Write-Output "State=$($task.State)"
Write-Output "NextRunTime=$($info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$task.Triggers | ForEach-Object {
    Write-Output "Trigger=$($_.StartBoundary)"
}
