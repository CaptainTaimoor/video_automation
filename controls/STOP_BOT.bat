@echo off
setlocal
cd /d "%~dp0.."
title Stop YT Bot
echo Stopping bot processes (scheduler / dashboard / watchdog / service_guard)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = (Resolve-Path '%CD%').Path.Replace('\','\\');" ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and ($_.CommandLine -like ('*' + $root.Replace('\\','\') + '*') -or $_.CommandLine -match 'run.py schedule|live_dashboard_server|watchdog.py|service_guard.py') -and ($_.CommandLine -match 'run.py schedule|live_dashboard_server|watchdog.py|service_guard.py') };" ^
  "foreach($p in $procs){ try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host ('Stopped PID ' + $p.ProcessId) } catch { Write-Host $_.Exception.Message } };" ^
  "if(-not $procs){ Write-Host 'No matching bot processes found.' }"
echo Done.
pause