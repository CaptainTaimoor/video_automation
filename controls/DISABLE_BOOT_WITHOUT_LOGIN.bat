@echo off
cd /d "%~dp0"
echo 0> "%~dp0flags\boot_without_login.enabled"
schtasks /Delete /TN "YT Automation Bot (Startup)" /F >nul 2>&1
echo Boot-without-login DISABLED. Logon autostart still works if enabled.
pause