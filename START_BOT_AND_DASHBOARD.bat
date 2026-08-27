@echo off
setlocal
title YouTube Bot Launcher
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "DASH_PORT=8787"
set "DASH_URL=http://127.0.0.1:%DASH_PORT%/"

echo.
echo ============================================================
echo  YouTube Bot Launcher
echo ============================================================
echo  Starting scheduler if needed...
echo  Starting premium dashboard if needed...
echo  Starting watchdog if needed...
echo  Installing automatic recovery guard...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root='%ROOT%'.TrimEnd('\');" ^
  "$python='%PY%';" ^
  "$scheduler=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'run.py schedule' };" ^
  "if(-not $scheduler){Start-Process -FilePath $python -ArgumentList @('run.py','schedule','--mode','cron','--upload') -WorkingDirectory $root -WindowStyle Hidden; Write-Host 'Scheduler: started'} else {Write-Host ('Scheduler: already running PID ' + (($scheduler | Select-Object -ExpandProperty ProcessId) -join ', '))};" ^
  "$port=Get-NetTCPConnection -LocalPort %DASH_PORT% -State Listen -ErrorAction SilentlyContinue;" ^
  "if(-not $port){$env:YT_DASHBOARD_HOST='0.0.0.0'; $env:YT_DASHBOARD_PORT='%DASH_PORT%'; Start-Process -FilePath $python -ArgumentList @('scripts\live_dashboard_server.py','--no-open') -WorkingDirectory $root -WindowStyle Hidden; Start-Sleep -Seconds 2; Write-Host 'Dashboard: started for this PC and same Wi-Fi/LAN'} else {Write-Host 'Dashboard: already running'};" ^
  "$env:YT_WATCHDOG_ALLOW_UPLOAD='1';" ^
  "$watchdog=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'scripts.watchdog.py|scripts\\watchdog.py' };" ^
  "if(-not $watchdog){Start-Process -FilePath $python -ArgumentList @('scripts\watchdog.py') -WorkingDirectory $root -WindowStyle Hidden; Write-Host 'Watchdog: started'} else {Write-Host ('Watchdog: already running PID ' + (($watchdog | Select-Object -ExpandProperty ProcessId) -join ', '))};" ^
  "& (Join-Path $root 'scripts\install_service_guard_task.ps1');" ^
  "$ip=(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1 -ExpandProperty IPAddress);" ^
  "Write-Host ('Open on this PC:  http://127.0.0.1:%DASH_PORT%/');" ^
  "Write-Host ('Open on phone/LAN: http://' + $ip + ':%DASH_PORT%/');" ^
  "$rule=Get-NetFirewallRule -DisplayName 'YT Automation Dashboard 8787' -ErrorAction SilentlyContinue;" ^
  "if(-not $rule){Write-Host 'If phone cannot open it, allow TCP port 8787 in Windows Firewall once.' -ForegroundColor Yellow};" ^
  "Start-Process '%DASH_URL%';"

echo.
echo Dashboard opened: %DASH_URL%
echo For phone / another device: use the "Open on phone/LAN" URL shown above.
echo You can close this window. Bot/dashboard/watchdog will keep running.
echo.
pause
