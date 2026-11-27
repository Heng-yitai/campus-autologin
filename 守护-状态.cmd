@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === task list ===
wmic process where "name like 'python%%'" get processid,commandline 2>nul | findstr /i net_guard
echo.
echo === last 15 log lines ===
if exist net_guard.log (powershell -NoProfile -Command "Get-Content 'net_guard.log' -Tail 15") else (echo no log yet)
pause
