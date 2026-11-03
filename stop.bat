@echo off
rem PhoneCluster - stop the coordinator and every worker started from this project.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop.ps1"
endlocal
