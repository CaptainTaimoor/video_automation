@echo off
cd /d "%~dp0"
echo 1> "%~dp0flags\autostart.enabled"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\setup\install.ps1" -NoStart
echo Autostart ENABLED.
pause