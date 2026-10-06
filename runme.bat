@echo off
setlocal EnableDelayedExpansion

REM =========================
REM CONFIG
REM =========================
cd /d C:\Users/vsa/Documents/Midd4VC-with-stochastic-fault-injector

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Number of experiments
set N_EXP=10
set SECONDS_IN_HOUR=3600

REM =========================
REM LOOP
REM =========================
for /L %%i in (1,1,%N_EXP%) do (

    REM Generate timestamp YYYYMMDD_HHMMSS
    for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do (
        set TIMESTAMP=%%t
    )

    set EXP_DIR=%CD%\experiments\!TIMESTAMP!
    mkdir "!EXP_DIR!" 2>nul

    echo Run experiment: %%i/%N_EXP% - !EXP_DIR!

    REM Run server and vehicles in background
    start /B cmd /c "python server\Midd4VCServer.py normal !EXP_DIR! >> experiments\!TIMESTAMP!\server.log 2>>&1"
    start /B cmd /c "python client\vehicles.py normal !EXP_DIR! >> experiments\!TIMESTAMP!\vehicles.log 2>>&1"
    start /B cmd /c "python client\applications.py exp !EXP_DIR! >> experiments\!TIMESTAMP!\applications.log 2>>&1"
    start /B cmd /c "python monitor\monitor.py --output-dir !EXP_DIR! --runtime !SECONDS_IN_HOUR! >> experiments\!TIMESTAMP!\monitor.log 2>>&1"
    start /B cmd /c "python injector\fault_injector.py server server --output-dir !EXP_DIR! --runtime !SECONDS_IN_HOUR! >> experiments\!TIMESTAMP!\server-faults.log 2>>&1"
    for /L %%v in (1,1,40) do (
        start /B cmd /c "python injector\fault_injector.py vehicle veh%%v --output-dir !EXP_DIR! --runtime !SECONDS_IN_HOUR!"
        start /B cmd /c "python rental\rental_generator.py veh%%v --output-dir !EXP_DIR! --runtime !SECONDS_IN_HOUR!"
    )

    REM Sleep 600 seconds
    timeout /t !SECONDS_IN_HOUR! /nobreak >nul

    echo Run experiment: %%i/%N_EXP%: Finished
)

endlocal
