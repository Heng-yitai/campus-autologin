@echo off
setlocal enabledelayedexpansion
title 打包 exe — 校园网一键登录
cd /d "%~dp0"

echo.
echo  ============================================================
echo    打包「校园网一键登录.exe」并生成发布压缩包
echo  ============================================================
echo.

rem ── 0. 找 Python ────────────────────────────────────────────
rem 这台电脑上 `python` 命令实际是应用商店的占位程序（121 字节），
rem 直接调用会弹微软商店而不是跑脚本。所以必须自己找真实解释器。
set "PY="

if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

rem WorkBuddy 自带的 Python（这台机器上真正可用的那个）
if not defined PY (
  for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if not defined PY if exist "%%D\python.exe" set "PY=%%D\python.exe"
  )
)

rem 最后才试 PATH 里的 python，但要排除 WindowsApps 占位程序
if not defined PY (
  for /f "delims=" %%E in ('python -c "import sys;print(sys.executable)" 2^>nul') do (
    echo %%E | "%SystemRoot%\System32\findstr.exe" /I "WindowsApps" >nul
    if errorlevel 1 set "PY=%%E"
  )
)

if not defined PY (
  echo  [!] 没找到可用的 Python。
  echo.
  echo      这台机器上 `python` 命令指向的是应用商店占位程序，不是真的 Python。
  echo      装一个就行： https://www.python.org/downloads/
  echo      安装时记得勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

echo  [0/4] Python：!PY!
"!PY!" -V

rem ── 1. 检查程序是否正在运行（运行中会锁住 exe，打包必失败）────
tasklist /fi "imagename eq 校园网一键登录.exe" 2>nul | "%SystemRoot%\System32\find.exe" /I "校园网一键登录.exe" >nul
if not errorlevel 1 (
  echo.
  echo  [!] 程序正在运行，先把它关掉再打包。
  echo      任务栏右下角找一下，或者用任务管理器结束「校园网一键登录.exe」。
  echo.
  pause
  exit /b 1
)

rem ── 2. 检查 PyInstaller，没装就装 ──────────────────────────
echo  [1/4] 检查打包工具……
"!PY!" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo        没装，正在安装（约 15 MB，用阿里云镜像加速）……
  "!PY!" -m pip install -q pyinstaller --proxy "" --timeout 60 -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
  if errorlevel 1 (
    echo.
    echo  [!] 安装失败。把上面的报错拍照发给小硕。
    pause
    exit /b 1
  )
)
echo        就绪。

rem ── 3. 清理旧产物后打包 ─────────────────────────────────
echo  [2/4] 清理旧产物……
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist "校园网一键登录.spec" del /q "校园网一键登录.spec"

echo  [3/4] 开始打包（1~2 分钟，别关窗口）……
echo.
"!PY!" -m PyInstaller -F -w -i icon.ico --name "校园网一键登录" campus_login.py --noconfirm
if errorlevel 1 (
  echo.
  echo  [!] 打包失败，看上面最后几行报错。
  pause
  exit /b 1
)

if not exist "dist\校园网一键登录.exe" (
  echo.
  echo  [!] 没找到产物 dist\校园网一键登录.exe
  pause
  exit /b 1
)

rem ── 4. 组装发布包（exe + 使用说明）────────────────────────
echo.
echo  [4/4] 生成发布压缩包……
if not exist "发布" mkdir "发布"
copy /y "dist\校园网一键登录.exe" "发布\校园网一键登录.exe" >nul
copy /y "发布用-使用说明.txt" "发布\使用说明-先看这个.txt" >nul
if exist "发布\校园网一键登录-v6.zip" del /q "发布\校园网一键登录-v6.zip"

powershell -NoProfile -Command "Compress-Archive -Path '发布\校园网一键登录.exe','发布\使用说明-先看这个.txt' -DestinationPath '发布\校园网一键登录-v6.zip' -Force"

if not exist "发布\校园网一键登录-v6.zip" (
  echo  [!] 压缩包没生成成功。
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo    打包完成
echo  ============================================================
echo.
for %%A in ("dist\校园网一键登录.exe")   do echo    exe ： %%~zA 字节
for %%A in ("发布\校园网一键登录-v6.zip") do echo    zip ： %%~zA 字节
echo.
echo    位置：%~dp0发布\
echo.
echo    下一步：把这个 zip 上传到 GitHub Release。
echo.
start "" "%~dp0发布"
pause
