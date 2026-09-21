$ErrorActionPreference = "Stop"
if ((Get-TimeZone).Id -ne "China Standard Time") {
    throw "任务按北京时间运行，请先确认Windows时区为中国标准时间。"
}
$wrapper = Join-Path $PSScriptRoot "run_microcap_rotation_task.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$wrapper"""
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:20"),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:20")
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "InvestmentDashboard Microcap20 ABCD" -Description "工作日15:20和16:20北京时收盘模拟；缺证据暂停；无实盘交易或外部推送" -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Select-Object TaskName,State
