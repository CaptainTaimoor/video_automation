@echo off
cd /d "%~dp0"
echo 0> "%~dp0flags\autostart.enabled"
schtasks /Delete /TN "YT Automation Bot (Logon)" /F >nul 2>&1
schtasks /Delete /TN "YT Automation Bot (Startup)" /F >nul 2>&1
echo Autostart DISABLED (scheduled tasks removed). Service guard task left as-is.
echo Use DISABLE_SERVICE_GUARD.bat if you also want that off.
pause