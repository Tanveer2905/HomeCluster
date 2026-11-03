@echo off
rem PhoneCluster - start the coordinator + PC worker in its own window and open the dashboard.
rem Double-click this file, or run it from a terminal. Use stop.bat to stop everything.
setlocal
cd /d "%~dp0"
set "PC_ROOT=%CD%"
set "PC_DATA_DIR=%CD%\data"

set "PY=tools\python\python.exe"
if not exist "%PY%" (
    echo [PhoneCluster] setting up the cluster's own Python once...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
    if errorlevel 1 (
        echo [PhoneCluster] setup failed
        pause
        exit /b 1
    )
)

"%PY%" -c "import cryptography" 2>nul
if errorlevel 1 (
    echo [PhoneCluster] installing the new requirements into tools\python once...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
)

netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
    echo [PhoneCluster] something is already listening on port 8765 - is it already running?
    echo                Run stop.bat first, or open http://127.0.0.1:8764
    pause
    exit /b 1
)

echo [PhoneCluster] starting coordinator + PC worker (logs in the new window)...
start "PhoneCluster coordinator" "%PY%" -m coordinator %*

rem give it a moment, then open the dashboard (ping = sleep that needs no console input)
ping -n 5 127.0.0.1 >nul
start "" http://127.0.0.1:8764
echo [PhoneCluster] dashboard: http://127.0.0.1:8764  (only on this PC; PIN in data\config.yaml)
echo [PhoneCluster] add phones from the Join page: each gets a one-time code. stop.bat stops it.
endlocal
