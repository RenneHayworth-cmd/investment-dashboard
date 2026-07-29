Option Explicit

Dim shell, command
Set shell = CreateObject("WScript.Shell")
command = """C:\Windows\System32\wsl.exe"" -d Ubuntu -- " _
    & "/home/renne/investment_dashboard/.venv/bin/python " _
    & "/home/renne/investment_dashboard/scripts/monitor_iron_ore_price.py"
shell.Run command, 0, False
