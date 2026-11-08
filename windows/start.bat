@echo off
rem MeetLite launcher. Keep this file CRLF and ASCII-only.
rem cmd.exe splits lines on CR: an LF-only copy makes every if/goto misfire.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "MeetLite.exe" goto useexe
if exist "runtime\python.exe" goto usepython

echo runtime\python.exe not found. Please extract the zip again.
pause
exit /b 1

:useexe
start "" "MeetLite.exe"
exit /b 0

:usepython
echo MeetLite is starting, the UI will open in your browser.
echo This console window is the backend log. Closing it stops MeetLite.
"runtime\python.exe" -X utf8 -u "app\server.py" --port 0 --open
echo.
echo MeetLite stopped.
pause
exit /b 0
