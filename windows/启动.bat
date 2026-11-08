@echo off
rem MeetLite 启动器（浏览器窗口模式）
rem 如果同目录有编译好的 MeetLite.exe（原生窗口壳），优先用它；否则用运行时 + 默认浏览器。
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist "MeetLite.exe" (
  start "" "MeetLite.exe"
  exit /b 0
)

if not exist "runtime\python.exe" (
  echo.
  echo 找不到 runtime\python.exe —— 压缩包可能没解压完整，请重新解压。
  echo.
  pause
  exit /b 1
)

echo 正在启动 MeetLite，稍后会自动打开浏览器窗口…
runtime\python.exe -X utf8 -u app\server.py --port 0 --open
echo.
echo MeetLite 已退出。
pause
