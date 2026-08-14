param(
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard Index MA20 Update"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wrapper = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_index_ma20_update_task.ps1"

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
    Write-Output "Triggers: Daily 15:10, Daily 16:10"
    Write-Output "Action: $powerShell $actionArguments"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $actionArguments
$trigger1510 = New-ScheduledTaskTrigger -Daily -At "15:10"
$trigger1610 = New-ScheduledTaskTrigger -Daily -At "16:10"
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($trigger1510, $trigger1610) `
    -Settings $settings `
    -Principal $principal `
    -Description "Update completed-session index cards and MA20 cache daily at 15:10 and 16:10." `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "TaskName=$($task.TaskName)"
Write-Output "State=$($task.State)"
Write-Output "NextRunTime=$($info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$task.Triggers | ForEach-Object {
    Write-Output "Trigger=$($_.StartBoundary)"
}
