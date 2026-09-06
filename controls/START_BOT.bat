@echo off
setlocal
cd /d "%~dp0.."
title Start YT Bot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0START_BOT_SILENT.ps1"
echo Bot start requested.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do set IP=%%a
set IP=%IP: =%
echo Local:  http://127.0.0.1:8787/
if defined IP echo LAN:    http://%IP%:8787/
pause