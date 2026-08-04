@echo off
REM Wrapper for Task Scheduler. Mirrors the raw ERP tables prod_main -> NUrul_DB.
REM Usage:
REM   run_mirror.bat            -> mirror every table, then verify (nightly)
REM   run_mirror.bat verify     -> verify only, change nothing
REM Appends to mirror.log next to this script so a silent scheduled failure
REM stays inspectable. A non-zero exit code means at least one table differs;
REM the previous data is left intact in that case, never a half-loaded table.
REM
REM No parenthesised blocks around %errorlevel% below: cmd expands variables
REM when it parses a whole block, so an %errorlevel% read inside if(...) shows
REM the value from *before* the command ran. goto keeps each read on its own
REM parsed line, where the value is current.

setlocal
set SCRIPT_DIR=%~dp0
set PYTHON=C:\Python314\python.exe

set LOG=%SCRIPT_DIR%mirror.log
echo ================ %date% %time% ================>> "%LOG%"

if not exist "%PYTHON%" goto nopython
if /I "%~1"=="verify" goto verifyonly

"%PYTHON%" -u "%SCRIPT_DIR%sync_mirror.py" >> "%LOG%" 2>&1
set MIRROR_RC=%errorlevel%
>> "%LOG%" echo mirror exit code: %MIRROR_RC%

REM Only worth verifying if the mirror itself reported success.
if not "%MIRROR_RC%"=="0" goto failed

"%PYTHON%" -u "%SCRIPT_DIR%verify_mirror.py" >> "%LOG%" 2>&1
set VERIFY_RC=%errorlevel%
>> "%LOG%" echo verify exit code: %VERIFY_RC%
endlocal & exit /b %VERIFY_RC%

:verifyonly
"%PYTHON%" -u "%SCRIPT_DIR%verify_mirror.py" >> "%LOG%" 2>&1
set VERIFY_RC=%errorlevel%
>> "%LOG%" echo verify exit code: %VERIFY_RC%
endlocal & exit /b %VERIFY_RC%

:failed
>> "%LOG%" echo mirror failed, skipping verify
endlocal & exit /b %MIRROR_RC%

:nopython
>> "%LOG%" echo ERROR: python not found at %PYTHON%
endlocal & exit /b 9
