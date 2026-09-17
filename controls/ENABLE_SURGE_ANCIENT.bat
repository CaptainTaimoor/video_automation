@echo off
cd /d "%~dp0"
echo 1> "%~dp0flags\surge_ancient.enabled"
echo Surge flag ON for Phase B (daytime hourly Ancient Shorts).
echo Edit config\settings.yaml surge slots then run RESTART_SCHEDULER.bat
echo Phase A stays active until you raise volume intentionally.
pause