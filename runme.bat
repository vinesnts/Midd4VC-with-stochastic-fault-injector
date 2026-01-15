@echo off
setlocal EnableDelayedExpansion

REM =========================
REM CONFIG
REM =========================
cd /d C:\Users/vsa/Documents/Midd4VC-with-stochastic-fault-injector

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Run applications client in background
start /B cmd /c "python client\applications.py >> applications.log 2>>&1"

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
    start /B cmd /c "python server\Midd4VCServer.py f !EXP_DIR! >> experiments\!TIMESTAMP!\server.log 2>>&1"
    start /B cmd /c "python client\vehicles.py f !EXP_DIR! >> experiments\!TIMESTAMP!\vehicles.log 2>>&1"

    REM Sleep 600 seconds
    timeout /t !SECONDS_IN_HOUR! /nobreak >nul

    echo Run experiment: %%i/%N_EXP%: Finished
)

endlocal
