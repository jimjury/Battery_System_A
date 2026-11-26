@echo off
cd /d "%~dp0"
F:\Anaconda\python.exe tools\firmware.py flash --port COM12
pause
