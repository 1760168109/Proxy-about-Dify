@echo off
setlocal
set "PROXY_DIR=%~dp0"
cd /d "%PROXY_DIR%"

where python >nul 2>&1
if errorlevel 1 (
  echo [lan-bc] python not found. Install Python 3 and re-open the terminal.
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process python -ArgumentList 'main.py' -WorkingDirectory '%PROXY_DIR%' -WindowStyle Hidden"
echo [lan-bc] lan is running in the background (http://127.0.0.1:7272)
exit /b 0
