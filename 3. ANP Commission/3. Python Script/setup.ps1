# Run once: creates venv, installs packages, creates .env from template
$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Project folder: $ProjectRoot" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Python not found. Install Python 3.10+ from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "During install, check 'Add python.exe to PATH'." -ForegroundColor Yellow
    exit 1
}

$pythonVersion = python --version 2>&1
Write-Host "Using $pythonVersion"

$venvPath = Join-Path $ProjectRoot ".venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "Creating virtual environment..."
    python -m venv $venvPath
}

$pip = Join-Path $venvPath "Scripts\pip.exe"
$py = Join-Path $venvPath "Scripts\python.exe"

Write-Host "Installing dependencies..."
& $py -m pip install -q -r (Join-Path $ProjectRoot "requirements.txt")

$envFile = Join-Path $ProjectRoot ".env"
$exampleFile = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path $envFile)) {
    Copy-Item $exampleFile $envFile
    Write-Host "Created .env from .env.example" -ForegroundColor Yellow
    Write-Host "Edit .env and set PG_PROXY_TOKEN before running the report." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists."
}

Write-Host ""
Write-Host "Setup complete. Next steps:" -ForegroundColor Green
Write-Host "  1. Edit .env and set PG_PROXY_TOKEN"
Write-Host "  2. Run:  .\run.ps1 --all-time"
Write-Host "     Or:  .\run.ps1 --payout-month 2026-02"
