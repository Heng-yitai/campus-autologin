@echo off
chcp 65001 >nul
cd /d "%~dp0"
wscript.exe //nologo "run_guard.vbs" stop
pause
