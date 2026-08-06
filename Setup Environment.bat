@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Finance Commission Dashboard - environment setup
echo ============================================================
echo.

:: An install built since the runtime was bundled ships its own Python under
:: runtime\, dependencies included. There is nothing to download or build, so
:: this whole step is a no-op for the user -- which is the point: it used to
:: mean installing Python by hand, then several minutes of pip behind a console
:: they were told not to close.
if exist "runtime\python.exe" goto bundled

:: ---- No bundled runtime (source checkout, or an older install) -------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python 3.10 or newer was not found on this machine.
    echo.
    echo Install it from https://www.python.org/downloads/windows/
    echo and be sure to tick "Add python.exe to PATH", then run this again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a private Python environment in .venv ...
    %PY% -m venv ".venv"
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo Installing dependencies ^(this can take a few minutes the first time^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r "requirements-dashboard.txt"
if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection and retry.
    pause
    exit /b 1
)
set "PYEXE=.venv\Scripts\python.exe"
goto haveenv

:bundled
set "PYEXE=runtime\python.exe"
echo Using the bundled Python runtime - nothing to install.
echo.

:haveenv
:: -- Session secret ---------------------------------------------------------
:: Flask refuses to start without FLASK_SECRET_KEY. Generate one per install so
:: sessions from one machine are never valid on another.
findstr /b /c:"FLASK_SECRET_KEY=" ".env" >nul 2>&1
if errorlevel 1 (
    echo Generating a session secret ...
    for /f %%k in ('"%PYEXE%" -c "import secrets;print(secrets.token_hex(32))"') do (
        echo FLASK_SECRET_KEY=%%k>> ".env"
    )
)

:: -- First admin account ----------------------------------------------------
if not exist "8. Web Dashboard\dashboard.db" (
    echo.
    echo No user database yet - create the first administrator account.
    "%PYEXE%" "8. Web Dashboard\create_admin.py"
)

echo.
echo ============================================================
echo  Setup complete.
echo.
echo  One more step: the dashboard needs its access keys to read
echo  live data. Add the lines your IT admin gave you to the .env
echo  file in this folder, for example:
echo.
echo      PG_PROXY_TOKEN=your-token-here
echo.
echo  Then start the dashboard from the Start Menu shortcut.
echo ============================================================
echo.
pause
endlocal
