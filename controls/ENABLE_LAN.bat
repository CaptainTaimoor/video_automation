@echo off
cd /d "%~dp0"
echo 1> "%~dp0flags\lan.enabled"
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Admin for firewall rule...
  powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'YT Automation Dashboard 8787' -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -ErrorAction SilentlyContinue; Enable-NetFirewallRule -DisplayName 'YT Automation Dashboard 8787' -ErrorAction SilentlyContinue"
echo LAN ENABLED. Restart bot for bind change: run STOP_BOT.bat then START_BOT.bat
pause