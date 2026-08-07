# PowerShell script to setup local Postgres database and restore the clone data.

$ErrorActionPreference = "Continue"

Write-Host "=== Local PostgreSQL Setup ===" -ForegroundColor Cyan
Write-Host "Checking for psql command line tool..." -ForegroundColor Yellow

# Attempt to find psql.exe automatically if it's not in PATH
$psqlPath = "psql"
if (!(Get-Command psql -ErrorAction SilentlyContinue)) {
    Write-Host "psql not found in PATH. Searching standard installation folders..." -ForegroundColor Yellow
    # Search common install paths in C:\Program Files\PostgreSQL
    $installDirs = Get-ChildItem -Path "C:\Program Files\PostgreSQL" -Directory -ErrorAction SilentlyContinue
    $found = $false
    foreach ($dir in $installDirs) {
        $candidate = Join-Path $dir.FullName "bin\psql.exe"
        if (Test-Path $candidate) {
            $psqlPath = $candidate
            $found = $true
            Write-Host "Found psql at: $psqlPath" -ForegroundColor Green
            break
        }
    }
    
    if (!$found) {
        Write-Error "PostgreSQL tool 'psql' is not found in your system PATH or standard directories.`nMake sure PostgreSQL is installed and 'C:\Program Files\PostgreSQL\<version>\bin' is added to your Environment Path variables."
        exit 1
    }
} else {
    Write-Host "psql is available in PATH." -ForegroundColor Green
}

# 2. Create database prod_main (ignoring error if database already exists)
Write-Host "Creating database 'prod_main' locally on port 5433..." -ForegroundColor Yellow
& $psqlPath -U postgres -p 5433 -c "CREATE DATABASE prod_main;" 2>$null

# 3. Restore data from local_db_dump.sql
Write-Host "`nRestoring data from local_db_dump.sql..." -ForegroundColor Yellow
Write-Host "This will take a moment because it contains all tables and records." -ForegroundColor Yellow
& $psqlPath -U postgres -p 5433 -d prod_main -f local_db_dump.sql

Write-Host "`n=== Setup Complete! ===" -ForegroundColor Green
Write-Host "Your local 'prod_main' database has been populated." -ForegroundColor Green
Write-Host "You can now run your commission scripts locally." -ForegroundColor Green
