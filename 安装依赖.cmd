@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-dependencies.ps1"
set "SHOPPER_INSTALL_EXIT=%ERRORLEVEL%"
pause
exit /b %SHOPPER_INSTALL_EXIT%
