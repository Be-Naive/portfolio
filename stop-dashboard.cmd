@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-dashboard.ps1"
if errorlevel 1 (
  echo.
  echo Failed to stop the portfolio dashboard cleanly.
  pause
  exit /b 1
)
endlocal
