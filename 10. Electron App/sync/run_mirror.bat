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
REM
REM The interpreter is DISCOVERED and TESTED, not hardcoded:
REM
REM   * A pinned path (this used to say C:\Python314\python.exe) stops working
REM     the day Python is upgraded or moved. The task then exits 9 every night
REM     having mirrored nothing, and the reports quietly go stale.
REM   * Finding *a* python is not enough. sync_mirror.py swallows a missing
REM     python-dotenv with "except ImportError: pass", so an interpreter without
REM     it loads no .env, finds no tokens, and reports "No token for prod_main"
REM     -- which reads like a credentials problem, not a wrong-interpreter one.
REM     Each candidate is therefore probed with "import dotenv" before use.
REM
REM Set MIRROR_PYTHON to pin a specific interpreter.

setlocal
set SCRIPT_DIR=%~dp0
for %%I in ("%~dp0..\..") do set REPO_ROOT=%%~fI

set LOG=%SCRIPT_DIR%mirror.log
echo ================ %date% %time% ================>> "%LOG%"

set PYTHON=
set PYFLAGS=

REM ── 1. Explicit override ────────────────────────────────────────────────────
if not defined MIRROR_PYTHON goto tryvenv
if not exist "%MIRROR_PYTHON%" goto badoverride
set PYTHON=%MIRROR_PYTHON%
"%PYTHON%" -c "import dotenv" >nul 2>&1
if errorlevel 1 goto overridedeps
goto havepython

:badoverride
>> "%LOG%" echo ERROR: MIRROR_PYTHON is set to "%MIRROR_PYTHON%" but no such file exists
endlocal & exit /b 9

:overridedeps
>> "%LOG%" echo ERROR: MIRROR_PYTHON ("%MIRROR_PYTHON%") cannot import python-dotenv. Run: "%MIRROR_PYTHON%" -m pip install python-dotenv
endlocal & exit /b 9

REM ── 2. The dashboard install's own environment ──────────────────────────────
:tryvenv
if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" goto trypath
set PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe
"%PYTHON%" -c "import dotenv" >nul 2>&1
if errorlevel 1 goto trypath
goto havepython

REM ── 3. python on PATH ───────────────────────────────────────────────────────
:trypath
set PYTHON=
where /q python
if errorlevel 1 goto trylauncher
set PYTHON=python
python -c "import dotenv" >nul 2>&1
if errorlevel 1 goto trylauncher
goto havepython

REM ── 4. The py launcher, newest first ────────────────────────────────────────
:trylauncher
set PYTHON=
where /q py
if errorlevel 1 goto nopython
py -3 -c "import dotenv" >nul 2>&1
if errorlevel 1 goto nopython
set PYTHON=py
set PYFLAGS=-3
goto havepython

:havepython
>> "%LOG%" echo interpreter: %PYTHON% %PYFLAGS%
if /I "%~1"=="verify" goto verifyonly

"%PYTHON%" %PYFLAGS% -u "%SCRIPT_DIR%sync_mirror.py" >> "%LOG%" 2>&1
set MIRROR_RC=%errorlevel%
>> "%LOG%" echo mirror exit code: %MIRROR_RC%

REM Only worth verifying if the mirror itself reported success.
if not "%MIRROR_RC%"=="0" goto failed

"%PYTHON%" %PYFLAGS% -u "%SCRIPT_DIR%verify_mirror.py" >> "%LOG%" 2>&1
set VERIFY_RC=%errorlevel%
>> "%LOG%" echo verify exit code: %VERIFY_RC%
endlocal & exit /b %VERIFY_RC%

:verifyonly
"%PYTHON%" %PYFLAGS% -u "%SCRIPT_DIR%verify_mirror.py" >> "%LOG%" 2>&1
set VERIFY_RC=%errorlevel%
>> "%LOG%" echo verify exit code: %VERIFY_RC%
endlocal & exit /b %VERIFY_RC%

:failed
>> "%LOG%" echo mirror failed, skipping verify
endlocal & exit /b %MIRROR_RC%

:nopython
>> "%LOG%" echo ERROR: no usable Python found (needs python-dotenv). Tried MIRROR_PYTHON, "%REPO_ROOT%\.venv\Scripts\python.exe", python on PATH, and the py launcher.
endlocal & exit /b 9
