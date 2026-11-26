@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_b\Start_B.ps1" -Module desktop_a.main
pause
