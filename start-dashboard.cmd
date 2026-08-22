@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-dashboard.ps1"
if errorlevel 1 (
  echo.
  echo Failed to start the portfolio dashboard.
  pause
  exit /b 1
)
endlocal
