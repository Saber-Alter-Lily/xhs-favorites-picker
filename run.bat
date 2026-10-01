@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure_login.ps1"
if errorlevel 1 (
  echo.
  echo Login recovery did not complete. The web picker was not started.
  pause
  exit /b 1
)
python xhs_web.py
pause
