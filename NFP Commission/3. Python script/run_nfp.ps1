# Run NFP commission report from this folder.
# Usage: .\run_nfp.ps1
#        .\run_nfp.ps1 --year 2026

$ScriptDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir

# Optional: keep __pycache__ out of the Python script folder
$PyCache = Join-Path $ProjectRoot "5. _py_cache"
if (-not (Test-Path $PyCache)) {
    New-Item -ItemType Directory -Path $PyCache -Force | Out-Null
}
$env:PYTHONPYCACHEPREFIX = $PyCache

# Accept either token variable name
if (-not $env:POSTGRES_PROXY_TOKEN -and $env:PG_PROXY_TOKEN) {
    $env:POSTGRES_PROXY_TOKEN = $env:PG_PROXY_TOKEN
}

# Or read token from 4. data\pg_proxy_token.txt (NFP folder — separate from Basic, same API)
$TokenFile = Join-Path $ProjectRoot "4. data\pg_proxy_token.txt"
if (-not $env:POSTGRES_PROXY_TOKEN -and (Test-Path $TokenFile)) {
    $env:POSTGRES_PROXY_TOKEN = (Get-Content $TokenFile -Raw).Trim()
}

Set-Location $ScriptDir
python nfp_commission.py @args
