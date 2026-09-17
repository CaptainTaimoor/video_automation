@echo off
cd /d "%~dp0"
echo 1> "%~dp0flags\boot_without_login.enabled"
echo Boot-without-login flag ON. Re-run setup\INSTALL.bat as Admin to apply.
pause