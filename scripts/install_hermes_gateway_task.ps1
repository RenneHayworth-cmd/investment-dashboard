param(
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard Hermes Gateway"
$powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"
$wrapper = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_hermes_gateway_task.ps1"
$actionArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""

if ($DryRun) {
    Write-Output "Task: $taskName"
    Write-Output "Trigger: at logon for $env:USERNAME"
    Write-Output "Action: $powerShell $actionArguments"
    Write-Output "The WSL systemd hermes-gateway.service will be disabled to avoid duplicate gateways."
    exit 0
}

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

if ($Remove) {
    Write-Output "Removed scheduled task: $taskName"
    Write-Output "The WSL systemd hermes-gateway.service remains disabled."
    exit 0
}

$disableCommand = "systemctl --user disable --now hermes-gateway.service"
$disableArguments = "-d Ubuntu -- /bin/bash -lc `"$disableCommand`""
$disableProcess = Start-Process `
    -FilePath $wsl `
    -ArgumentList $disableArguments `
    -Wait `
    -PassThru `
    -NoNewWindow
if ($disableProcess.ExitCode -ne 0) {
    throw "Failed to disable the WSL systemd Hermes Gateway (exit code $($disableProcess.ExitCode))."
}

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Keep Hermes Weixin Gateway running in WSL after Windows logon." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "TaskName=$($task.TaskName)"
Write-Output "State=$($task.State)"
Write-Output "LastTaskResult=$($info.LastTaskResult)"
Write-Output "Trigger=$($task.Triggers[0].StartBoundary)"
Write-Output "SystemdGatewayEnabled=false"
