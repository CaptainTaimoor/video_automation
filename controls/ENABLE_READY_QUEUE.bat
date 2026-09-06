@echo off
cd /d "%~dp0.."
echo 1> "controls\flags\ready_queue.enabled"
powershell -NoProfile -Command ^
  "$p='.env'; if (-not (Test-Path $p)) { New-Item -ItemType File -Path $p | Out-Null }; ^
   $t=Get-Content $p -Raw -ErrorAction SilentlyContinue; if ($null -eq $t) { $t='' }; ^
   if ($t -match '(?m)^YT_READY_QUEUE=') { $t=[regex]::Replace($t,'(?m)^YT_READY_QUEUE=.*$','YT_READY_QUEUE=1') } else { $t=$t.TrimEnd()+\"`r`nYT_READY_QUEUE=1`r`n\" }; ^
   Set-Content -Path $p -Value $t -NoNewline"
echo Ready Queue ENABLED (controls\flags\ready_queue.enabled + YT_READY_QUEUE=1)
echo Restart the scheduler for jobs to pick this up.
pause
