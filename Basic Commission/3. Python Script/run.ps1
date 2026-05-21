# Run Basic Commission report (loads .env, uses system Python — no venv required)
$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

function Import-DotEnv {
    param([string]$Path)
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { return }
        if ($line -notmatch '^\s*([^=]+)=(.*)$') { return }
        $name = $matches[1].Trim()
        $value = $matches[2].Trim().Trim('"').Trim("'")
        if ($value.ToLower().StartsWith("bearer ")) {
            $value = $value.Substring(7).Trim()
        }
        Set-Item -Path "env:$name" -Value $value
    }
}

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "ERROR: Missing .env in this folder." -ForegroundColor Red
    Write-Host "Copy .env from ANP Commission (same PG_PROXY_TOKEN) or create from .env.example" -ForegroundColor Yellow
    exit 1
}

Import-DotEnv -Path $envFile

if (-not $env:PG_PROXY_TOKEN -or $env:PG_PROXY_TOKEN -match 'paste_your|your_bearer') {
    Write-Host "ERROR: Set PG_PROXY_TOKEN in .env (JWT only, no 'Bearer ' prefix)." -ForegroundColor Red
    exit 1
}

$baseUrl = ($env:PG_PROXY_URL).Trim().TrimEnd("/")
if ($baseUrl -and $baseUrl -notmatch '/api/sql$') {
    $env:PG_PROXY_URL = "$baseUrl/api/sql"
}

if (-not $env:PG_PROXY_DB -and $env:PG_DB_NAME) {
    $env:PG_PROXY_DB = $env:PG_DB_NAME
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "ERROR: Python not found. Install from https://www.python.org/downloads/ and enable 'Add to PATH'." -ForegroundColor Red
    exit 1
}

Write-Host "Running Basic Commission report from: $ProjectRoot" -ForegroundColor Cyan
& python (Join-Path $ProjectRoot "internal_basic_commission_report.py") @args
exit $LASTEXITCODE
