@echo off
cd /d "%~dp0.."
title Restart Scheduler (graceful)
echo Stopping scheduler only (watchdog/dashboard stay up)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path '%CD%').Path;" ^
  "$procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*'+$root+'*') -and $_.CommandLine -match 'run.py schedule' };" ^
  "foreach($p in $procs){ Stop-Process -Id $p.ProcessId -Force -EA SilentlyContinue; Write-Host ('Stopped scheduler PID '+$p.ProcessId) };" ^
  "Start-Sleep 2;" ^
  "$py=Join-Path $root '.venv\Scripts\python.exe';" ^
  "Start-Process -FilePath $py -ArgumentList @('run.py','schedule','--mode','cron','--upload') -WorkingDirectory $root -WindowStyle Hidden;" ^
  "Write-Host 'Scheduler restarted with latest config.'"
pause