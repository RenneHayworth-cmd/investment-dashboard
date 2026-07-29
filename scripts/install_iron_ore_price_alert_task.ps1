param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "InvestmentDashboard-IronOre-Below730"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$schtasks = Join-Path $env:WINDIR "System32\schtasks.exe"
$hiddenRunner = "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\run_iron_ore_price_alert_hidden.vbs"

if ($Remove) {
    $deleteArguments = "/Delete /TN `"$taskName`" /F"
    $process = Start-Process -FilePath $schtasks -ArgumentList $deleteArguments -Wait -PassThru -NoNewWindow
    exit $process.ExitCode
}

$taskCommand = "`"$wscript`" //B //Nologo `"$hiddenRunner`""
$createArguments = "/Create /TN `"$taskName`" /TR `"$taskCommand`" /SC MINUTE /MO 1 /F"
$process = Start-Process -FilePath $schtasks -ArgumentList $createArguments -Wait -PassThru -NoNewWindow
if ($process.ExitCode -ne 0) {
    throw "创建Windows计划任务失败，退出代码：$($process.ExitCode)"
}

Write-Output "铁矿石价格监控任务已创建：$taskName（每分钟检查，非交易时段自动跳过）。"
