@echo off
setlocal
title YT Automation - One Click Setup
cd /d "%~dp0.."

echo.
echo ============================================================
echo  YT Automation - Portable One-Click Setup
echo  This will install Python/Node (if needed), create .venv,
echo  install dependencies, configure auto-start + LAN access.
echo ============================================================
echo.

:: Elevate if possible (needed for firewall + boot task)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator privileges...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set ERR=%ERRORLEVEL%
echo.
if %ERR% neq 0 (
  echo SETUP FAILED with code %ERR%
  pause
  exit /b %ERR%
)
echo Done. You can close this window.
pause