@echo off
:: Change to the Commission root (parent of this bat file's directory)
cd /d "%~dp0.."

echo Testing for required Python libraries...
python -c "import flask" 2>nul
if %errorlevel% neq 0 (
    echo Flask is missing. Installing Flask...
    pip install flask
)
python -c "import dotenv" 2>nul
if %errorlevel% neq 0 (
    echo python-dotenv is missing. Installing python-dotenv...
    pip install python-dotenv
)

:: Kill any existing server already running on port 5001 so we start clean
echo Stopping any existing server on port 5001...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 >nul

:: Drop the cached commission data so a restart (e.g. after a code fix) never
:: serves stale computed results carried over from the previous run
if exist "8. Web Dashboard\data\dashboard_cache.pkl" (
    echo Clearing stale dashboard cache...
    del /f /q "8. Web Dashboard\data\dashboard_cache.pkl" >nul 2>&1
)

echo Starting local web server...
start "Finance Commission Dashboard" cmd /k python "8. Web Dashboard/app.py"

echo Waiting for server to start...
timeout /t 3 >nul

echo Launching Finance Commission Portal...
start http://127.0.0.1:5001
echo Dashboard is now running. Close the server window to shut down.
