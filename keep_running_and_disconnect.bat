@echo off
setlocal
cd /d "%~dp0"
rem Move this session to the remote PC's own screen, then Remote Desktop disconnects.
rem The desktop keeps running unlocked so WeChat automation still works.
for /f %%i in ('powershell -NoProfile -Command "(Get-Process -Id $PID).SessionId"') do set SID=%%i
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0session_keeper.ps1\"','-SessionId','%SID%'"
