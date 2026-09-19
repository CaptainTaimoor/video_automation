@echo off
setlocal
title YT Automation - Go Live Check
cd /d "%~dp0"

echo.
echo ============================================================
echo  GO LIVE CHECK
echo  Verifies this PC can publish, then builds one test video
echo  WITHOUT uploading anything.
echo ============================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo No .venv found - creating one now...
  python -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo.
    echo Could not create .venv. Is Python installed and on PATH?
    pause
    exit /b 1
  )
  echo Installing dependencies, this takes a few minutes...
  "%PY%" -m pip install --upgrade pip >nul
  "%PY%" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo.
    echo Dependency install failed. Read the error above.
    pause
    exit /b 1
  )
)

"%PY%" "%~dp0scripts\go_live.py" %*
set ERR=%ERRORLEVEL%

echo.
pause
exit /b %ERR%
