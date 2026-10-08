@echo off
setlocal EnableExtensions

title Cross-border E-commerce Ad Agent - Shutdown

echo ============================================================
echo   Cross-border E-commerce Ad Agent - Shutdown
echo ============================================================
echo.

echo   Stopping services on ports 8000 and 8502 ...
powershell -NoProfile -Command "$pids = Get-NetTCPConnection -LocalPort 8000,8502 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($pids) { foreach ($p in $pids) { Write-Host ('    Stopping PID ' + $p); Stop-Process -Id $p -Force } } else { Write-Host '    No running Ad Agent services found.' }"

echo.
echo Done.
echo.

endlocal
