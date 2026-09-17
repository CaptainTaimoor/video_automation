@echo off
cd /d "%~dp0"
echo 0> "%~dp0flags\lan.enabled"
net session >nul 2>&1
if %errorlevel%==0 (
  powershell -NoProfile -Command "Disable-NetFirewallRule -DisplayName 'YT Automation Dashboard 8787' -ErrorAction SilentlyContinue"
)
echo LAN DISABLED (dashboard will bind localhost only after restart).
echo Run STOP_BOT.bat then START_BOT.bat to apply.
pause