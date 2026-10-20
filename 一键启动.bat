@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_local.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Please read the message above.
    pause
)
