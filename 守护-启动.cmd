@echo off
cd /d "%~dp0"
wscript.exe //nologo "run_guard.vbs"
exit /b 0
