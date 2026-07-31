@echo off
:: Auto-start script: launches the dashboard + Cloudflare quick tunnel together.
:: Registered to run at user logon (survives PC reboots) via Task Scheduler.
cd /d "%~dp0.."

:: Kill any existing server already running on port 5001 so we start clean
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 >nul

:: Drop the cached commission data so a restart never serves stale computed
:: results carried over from the previous run
if exist "8. Web Dashboard\data\dashboard_cache.pkl" (
    del /f /q "8. Web Dashboard\data\dashboard_cache.pkl" >nul 2>&1
)

:: Start the dashboard server (minimized console window)
start "Commission Dashboard" /min cmd /c python "8. Web Dashboard\app.py"

:: Wait for the server to come up before pointing the tunnel at it
timeout /t 8 >nul

:: Start the Cloudflare quick tunnel. Output (including the current public URL)
:: is logged to tunnel.log -- check that file after each reboot for the new link,
:: since quick tunnel URLs change every time this restarts.
start "Commission Tunnel" /min cmd /c ""%LOCALAPPDATA%\cloudflared\cloudflared.exe" tunnel --url http://localhost:5001 > "%LOCALAPPDATA%\cloudflared\tunnel.log" 2>&1"
