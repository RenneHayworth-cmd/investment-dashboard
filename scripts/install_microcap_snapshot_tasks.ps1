param(
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard 微盘股真实成分快照"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wrapper = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_microcap_snapshot_task.ps1"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Output "Removed scheduled task: $taskName"
    exit 0
}

$actionArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""
if ($DryRun) {
    Write-Output "Task: $taskName"
    Write-Output "Triggers: Weekdays 15:05, 15:20, 16:20"
    Write-Output "Retry behavior: local complete snapshot is checked before any network request"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $actionArguments
$triggers = @("15:05", "15:20", "16:20") | ForEach-Object {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $_
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description "15:05采集BK1158成分；15:20和16:20仅在当天快照缺失时重试。" `
    -Force | Out-Null
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "TaskName=$($task.TaskName)"
Write-Output "State=$($task.State)"
Write-Output "NextRunTime=$($info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$task.Triggers | ForEach-Object { Write-Output "Trigger=$($_.StartBoundary)" }
