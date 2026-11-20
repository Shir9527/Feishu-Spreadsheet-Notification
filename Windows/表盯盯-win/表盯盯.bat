@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 表盯盯

echo.
echo === 表盯盯 . 环境自检 ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [X] 没找到 Python
  echo     请先装 Python 3：即将打开下载页，安装时勾选 "Add python.exe to PATH"
  start "" https://www.python.org/downloads/windows/
  echo     装完再双击本文件。
  pause
  exit /b 1
)
for /f "delims=" %%i in ('python -V 2^>^&1') do set PYVER=%%i
echo   [OK] %PYVER%

if not exist "app.py" (
  echo [X] 同目录没有 app.py
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo   [!] 没找到 Node.js，飞书 CLI 需要它
  echo       即将打开下载页，装 LTS 版（一路下一步）后重新双击本文件
  start "" https://nodejs.org/zh-cn/download
  pause
  exit /b 1
)
echo   [OK] npm 已装

where lark-cli >nul 2>nul
if errorlevel 1 (
  echo   安装飞书 CLI（约 1 分钟）...
  call npm install -g @larksuite/cli
)
echo   [OK] 飞书 CLI 已装

echo   检查登录状态...
lark-cli contact +get-user --as user >nul 2>nul
if errorlevel 1 (
  echo   第 1 步：配置应用凭证（会打开浏览器）
  call lark-cli config init --new
  echo   第 2 步：用你的飞书账号授权
  call lark-cli auth login --recommend
)

echo.
echo === 启动配置页（后台运行，本窗口可以关） ===
where pythonw >nul 2>nul
if errorlevel 1 (
  start "" python app.py
) else (
  start "" pythonw app.py
)
timeout /t 3 >nul
start "" http://127.0.0.1:8799
echo   配置页：http://127.0.0.1:8799
echo   本窗口可以直接关掉，不影响后台检查。
echo.
timeout /t 3 >nul
exit /b 0
