@echo off
:: Starts the Finance Commission Dashboard and opens it in the browser.
:: This is the shortcut target the installer creates, and the command the
:: self-updater re-runs after it swaps in a new version.
setlocal
cd /d "%~dp0"

:: An install ships its own interpreter under runtime\. Only a source checkout
:: or a pre-bundle install has to build a .venv first.
if exist "runtime\python.exe" goto haveruntime
if not exist ".venv\Scripts\python.exe" (
    call "Setup Environment.bat"
    if errorlevel 1 exit /b 1
)

:haveruntime
:: python.exe, not pythonw.exe — under pythonw sys.stdout/stderr are None, which
:: the server's stream wrapper cannot fully paper over. Minimised instead.
set "PYEXE=%~dp0runtime\python.exe"
if not exist "%PYEXE%" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

:: Free port 5001 so a stale server can never shadow the new build.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)

:: Drop the computed-data cache so a restart after an update never serves
:: results shaped by the previous version.
if exist "8. Web Dashboard\data\dashboard_cache.pkl" (
    del /f /q "8. Web Dashboard\data\dashboard_cache.pkl" >nul 2>&1
)

start "Finance Commission Dashboard" /min "%PYEXE%" "8. Web Dashboard\app.py"

:: Wait for the port to answer before opening the browser (up to ~30s).
set /a tries=0
:waitloop
set /a tries+=1
netstat -ano | findstr ":5001 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% geq 30 goto ready
timeout /t 1 >nul
goto waitloop

:ready
start "" http://127.0.0.1:5001
endlocal
