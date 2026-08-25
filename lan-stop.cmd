@echo off
setlocal
set "PROXY_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$conns = Get-NetTCPConnection -LocalPort 7272 -ErrorAction SilentlyContinue; if ($conns) { $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; Write-Host \"[lan-stop] Stopped process $p (port 7272)\"; } } else { $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*' }; if ($procs) { foreach ($pr in $procs) { Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host \"[lan-stop] Stopped process $($pr.ProcessId) (main.py)\"; } } else { Write-Host '[lan-stop] No running lan instance found.'; } }"
exit /b %ERRORLEVEL%
