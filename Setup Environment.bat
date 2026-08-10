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
:: Generate one per install so sessions from one machine are never valid on
:: another.
::
:: Deliberately NOT `for /f ('"%PYEXE%" -c "..."')`. That form re-parses the
:: inner quotes and hands cmd 'runtime\python.exe" -c "import' as the command,
:: so every install printed a raw "is not recognized" error into the setup
:: console and wrote no key at all; usebackq does not save it either. Running
:: the interpreter as a plain command and reading the result back through a
:: file quotes correctly whatever the path looks like. Written without a
:: parenthesised block so %SECRET% is not expanded before set /p fills it.
::
:: app.py generates and persists a key on first boot when this is missing, so
:: the only real damage was that error line during an otherwise silent setup.
findstr /b /c:"FLASK_SECRET_KEY=" ".env" >nul 2>&1
if not errorlevel 1 goto havesecret
echo Generating a session secret ...
set "SECRETTMP=%TEMP%\commission-secret.tmp"
"%PYEXE%" -c "import secrets;print(secrets.token_hex(32))" > "%SECRETTMP%"
set "SECRET="
set /p SECRET=<"%SECRETTMP%"
del "%SECRETTMP%" >nul 2>&1
if defined SECRET echo FLASK_SECRET_KEY=%SECRET%>> ".env"
:havesecret

:: -- Accounts ---------------------------------------------------------------
:: Deliberately nothing to do here. Accounts live in the shared database and
:: are issued by IT ahead of time, so this used to prompt every installer for a
:: username and password that the person had already been given - creating a
:: second, unwanted account (or silently resetting the real one's password when
:: the names happened to match). Sign in with the credentials IT sent instead.
:: IT can still run "8. Web Dashboard\create_admin.py" by hand to add or reset
:: an account.

:: -- Closing message --------------------------------------------------------
:: Only ask for access keys when they are actually missing. An installer built
:: with --seed-env ships .env already filled in, and telling that user to go
:: and paste keys sent them hunting for a step that was already done - then
:: made them dismiss a "press any key" for it. With keys present there is
:: nothing to say and nothing to wait for, so say it briefly and get out.
findstr /b /c:"PG_MIRROR_TOKEN=" ".env" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo ============================================================
    echo  Setup complete. Start the dashboard from the Start Menu
    echo  shortcut and sign in with the username and password IT
    echo  gave you.
    echo ============================================================
    echo.
    goto done
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
echo  Then start the dashboard from the Start Menu shortcut and
echo  sign in with the username and password IT gave you.
echo ============================================================
echo.
pause

:done
endlocal
