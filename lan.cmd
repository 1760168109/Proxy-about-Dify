@echo off
setlocal
set "PROXY_DIR=%~dp0"
cd /d "%PROXY_DIR%"

where python >nul 2>&1
if errorlevel 1 (
  echo [lan] python not found. Install Python 3 and re-open the terminal.
  exit /b 1
)

python "%PROXY_DIR%main.py" %*
exit /b %ERRORLEVEL%
