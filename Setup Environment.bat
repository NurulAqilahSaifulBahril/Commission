@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Finance Commission Dashboard - environment setup
echo ============================================================
echo.

:: ── Locate Python ───────────────────────────────────────────────────────────
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

:: ── Private virtual environment ─────────────────────────────────────────────
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

:: ── Session secret ──────────────────────────────────────────────────────────
:: Flask refuses to start without FLASK_SECRET_KEY. Generate one per install so
:: sessions from one machine are never valid on another.
findstr /b /c:"FLASK_SECRET_KEY=" ".env" >nul 2>&1
if errorlevel 1 (
    echo Generating a session secret ...
    for /f %%k in ('".venv\Scripts\python.exe" -c "import secrets;print(secrets.token_hex(32))"') do (
        echo FLASK_SECRET_KEY=%%k>> ".env"
    )
)

:: ── First admin account ─────────────────────────────────────────────────────
if not exist "8. Web Dashboard\dashboard.db" (
    echo.
    echo No user database yet - create the first administrator account.
    ".venv\Scripts\python.exe" "8. Web Dashboard\create_admin.py"
)

echo.
echo ============================================================
echo  Setup complete.
echo.
echo  One more step: the dashboard needs a PG_PROXY_TOKEN to read
echo  live data. Add this line to the .env file in this folder:
echo.
echo      PG_PROXY_TOKEN=your-token-here
echo.
echo  Then start the dashboard from the Start Menu shortcut.
echo ============================================================
echo.
pause
endlocal
