@echo off
REM Wrapper for Task Scheduler. Usage:
REM   run_sync.bat            -> full 2-year sync (nightly)
REM   run_sync.bat current    -> current month only (hourly)
REM Appends output to sync .log files next to this script, keeping the last
REM run inspectable when a scheduled run fails silently.
REM
REM The interpreter is DISCOVERED and TESTED, not hardcoded:
REM
REM   * A pinned path (this used to say C:\Python314\python.exe) stops working
REM     the day Python is upgraded or moved, and the task then does nothing
REM     every night.
REM   * Finding *a* python is not enough. sync_invoices.py needs psycopg2, and
REM     it swallows a missing python-dotenv with "except ImportError: pass" --
REM     so an interpreter without it loads no .env, finds no credentials, and
REM     fails in a way that reads like a token problem rather than a
REM     wrong-interpreter one. Each candidate is probed before use.
REM
REM Set SYNC_PYTHON to pin a specific interpreter.

setlocal
set SCRIPT_DIR=%~dp0
for %%I in ("%~dp0..\..") do set REPO_ROOT=%%~fI

if /I "%~1"=="current" (
    set ARGS=--current-month
    set LOG=%SCRIPT_DIR%sync_current.log
) else (
    set ARGS=
    set LOG=%SCRIPT_DIR%sync_full.log
)

echo ================ %date% %time% ================>> "%LOG%"

set PYTHON=
set PYFLAGS=

REM -- 1. Explicit override ----------------------------------------------------
if not defined SYNC_PYTHON goto tryvenv
if not exist "%SYNC_PYTHON%" goto badoverride
set PYTHON=%SYNC_PYTHON%
"%PYTHON%" -c "import dotenv, psycopg2" >nul 2>&1
if errorlevel 1 goto overridedeps
goto havepython

:badoverride
>> "%LOG%" echo ERROR: SYNC_PYTHON is set to "%SYNC_PYTHON%" but no such file exists
endlocal & exit /b 9

:overridedeps
>> "%LOG%" echo ERROR: SYNC_PYTHON ("%SYNC_PYTHON%") cannot import python-dotenv and psycopg2. Run: "%SYNC_PYTHON%" -m pip install python-dotenv psycopg2-binary
endlocal & exit /b 9

REM -- 2. The dashboard install's own environment ------------------------------
:tryvenv
if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" goto trypath
set PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe
"%PYTHON%" -c "import dotenv, psycopg2" >nul 2>&1
if errorlevel 1 goto trypath
goto havepython

REM -- 3. python on PATH ------------------------------------------------------
:trypath
set PYTHON=
where /q python
if errorlevel 1 goto trylauncher
set PYTHON=python
python -c "import dotenv, psycopg2" >nul 2>&1
if errorlevel 1 goto trylauncher
goto havepython

REM -- 4. The py launcher -----------------------------------------------------
:trylauncher
set PYTHON=
where /q py
if errorlevel 1 goto nopython
py -3 -c "import dotenv, psycopg2" >nul 2>&1
if errorlevel 1 goto nopython
set PYTHON=py
set PYFLAGS=-3
goto havepython

:havepython
>> "%LOG%" echo interpreter: %PYTHON% %PYFLAGS%
"%PYTHON%" %PYFLAGS% -u "%SCRIPT_DIR%sync_invoices.py" %ARGS% >> "%LOG%" 2>&1
set SYNC_RC=%errorlevel%
>> "%LOG%" echo exit code: %SYNC_RC%
endlocal & exit /b %SYNC_RC%

:nopython
>> "%LOG%" echo ERROR: no usable Python found (needs python-dotenv and psycopg2). Tried SYNC_PYTHON, "%REPO_ROOT%\.venv\Scripts\python.exe", python on PATH, and the py launcher.
endlocal & exit /b 9
