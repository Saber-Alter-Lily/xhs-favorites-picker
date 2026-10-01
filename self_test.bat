@echo off
setlocal
cd /d "%~dp0"
python -m unittest discover -s tests -v
if errorlevel 1 (
  echo.
  echo Tests failed.
  pause
  exit /b 1
)
node --check web\app.js
if errorlevel 1 (
  echo JavaScript syntax check failed.
  pause
  exit /b 1
)
node --check scripts\export_cookies_cdp.mjs
if errorlevel 1 (
  echo Cookie exporter syntax check failed.
  pause
  exit /b 1
)
echo.
echo All local tests passed.
pause
