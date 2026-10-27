@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行 安装依赖.cmd。
  pause
  exit /b 1
)
node "%CD%\launcher.cjs" --debug
if errorlevel 1 goto end
powershell -NoProfile -Command "Get-Content -LiteralPath (Join-Path (Get-Location) 'logs\server.log') -Tail 40 -Wait"
:end
pause
