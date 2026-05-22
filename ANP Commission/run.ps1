# Always runs from this script's folder (fixes 'cd' / path not found issues)
$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

$py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Virtual environment not found. Running setup first..." -ForegroundColor Yellow
    & (Join-Path $ProjectRoot "setup.ps1")
}

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "ERROR: Missing .env file." -ForegroundColor Red
    Write-Host "Run:  .\setup.ps1" -ForegroundColor Yellow
    Write-Host "Then edit .env and set PG_PROXY_TOKEN." -ForegroundColor Yellow
    exit 1
}

$tokenLine = Get-Content $envFile | Where-Object { $_ -match '^\s*PG_PROXY_TOKEN\s*=' } | Select-Object -First 1
if ($tokenLine -match 'your_bearer_token_here' -or [string]::IsNullOrWhiteSpace(($tokenLine -split '=', 2)[1])) {
    Write-Host "ERROR: PG_PROXY_TOKEN is not set in .env" -ForegroundColor Red
    Write-Host "Open .env and paste your Postgres proxy Bearer token." -ForegroundColor Yellow
    exit 1
}

Write-Host "Running ANP commission from: $ProjectRoot" -ForegroundColor Cyan
& $py (Join-Path $ProjectRoot "anp_commission.py") @args
