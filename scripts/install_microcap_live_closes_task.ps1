param(
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard 微盘实盘收盘价"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wrapper = "\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_microcap_live_closes_task.ps1"
$times = @("15:10", "15:40", "17:10")

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
    Write-Output "Triggers: Weekdays $($times -join ', ')"
    Write-Output "Retry behavior: local cache is checked first; complete days make no network request"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $actionArguments
$triggers = $times | ForEach-Object {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $_
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description "15:10保存微盘实盘持仓股票当日收盘价（收盘快照优先，日线补充）；15:40和17:10仅在仍有缺失时重试。" `
    -Force | Out-Null
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "TaskName=$($task.TaskName)"
Write-Output "State=$($task.State)"
Write-Output "NextRunTime=$($info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$task.Triggers | ForEach-Object { Write-Output "Trigger=$($_.StartBoundary)" }
