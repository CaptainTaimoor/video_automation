@echo off
schtasks /Delete /TN "YT Automation Service Guard" /F >nul 2>&1
echo Service Guard task removed.
pause