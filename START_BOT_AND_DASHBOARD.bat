@echo off
setlocal
title YouTube Bot Launcher
cd /d "%~dp0"

echo.
echo ============================================================
echo  YouTube Bot Launcher
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0controls\START_BOT_SILENT.ps1"

set "DASH_PORT=8787"
set "DASH_URL=http://127.0.0.1:%DASH_PORT%/"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ip=(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1 -ExpandProperty IPAddress);" ^
  "Write-Host ('Open on this PC:  http://127.0.0.1:%DASH_PORT%/');" ^
  "if($ip){Write-Host ('Open on phone/LAN: http://' + $ip + ':%DASH_PORT%/')};"

start "" "%DASH_URL%"
echo.
echo You can close this window. Bot keeps running.
echo Toggles: controls\ folder
echo.
pause