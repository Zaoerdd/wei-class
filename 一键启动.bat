@echo off
setlocal
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0start_web_app.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Please read the message above.
    pause
)
