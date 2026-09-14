Option Explicit

Dim shell, command
Set shell = CreateObject("WScript.Shell")
command = """C:\Windows\System32\wsl.exe"" -d Ubuntu -- /bin/bash -lc """ _
    & "export ENABLE_FANGTANG=false; export ENABLE_WECHAT=true; " _
    & "exec /home/renne/investment_dashboard/.venv/bin/python " _
    & "/home/renne/investment_dashboard/scripts/monitor_iron_ore_price.py"""
shell.Run command, 0, False
