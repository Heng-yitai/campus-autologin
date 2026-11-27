@echo off
title Campus Net Guard
cd /d "%~dp0"
wscript.exe //nologo "%~dp0run_guard.vbs"
ping -n 4 127.0.0.1 >nul
tasklist /FI "IMAGENAME eq pythonw.exe" | find /I "pythonw.exe" >nul
if errorlevel 1 (
  echo.
  echo [FAILED] guard not running. Send this window's text to Xiaoshuo.
) else (
  echo.
  echo [OK] guard is running in background.
)
ping -n 4 127.0.0.1 >nul
