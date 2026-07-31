@echo off
REM Wrapper for Task Scheduler. Usage:
REM   run_sync.bat            -> full 2-year sync (nightly)
REM   run_sync.bat current    -> current month only (hourly)
REM Appends output to sync .log files next to this script, keeping the last
REM run inspectable when a scheduled run fails silently.

setlocal
set SCRIPT_DIR=%~dp0
set PYTHON=C:\Python314\python.exe

if /I "%~1"=="current" (
    set ARGS=--current-month
    set LOG=%SCRIPT_DIR%sync_current.log
) else (
    set ARGS=
    set LOG=%SCRIPT_DIR%sync_full.log
)

echo ================ %date% %time% ================>> "%LOG%"
"%PYTHON%" -u "%SCRIPT_DIR%sync_invoices.py" %ARGS% >> "%LOG%" 2>&1
>> "%LOG%" echo exit code: %errorlevel%
endlocal
