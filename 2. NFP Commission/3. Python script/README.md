# NFP Commission — Python scripts

## Folder layout (NFP Commission)

| Folder | Contents |
|--------|----------|
| **1. Excel** | `STRING 650W package STRING INVERTER.xlsx` |
| **2. Screenshot** | PNG/JPEG (not used by scripts) |
| **3. Python script** | `nfp_commission.py`, `net_floor_prices.py`, `nfp_paths.py` |
| **4. data** | `nfp_620w_schedule.json` (or `nfp_620W_schedule.json`), `requirements.txt`, **`reports/`** |
| **5. _py_cache** | Optional Python cache (via `run_nfp.ps1`) |

## Same database as Basic Commission?

**Yes.** NFP does **not** need its own Postgres or Docker. Both use **pg-proxy** (HTTPS + Bearer token) to read **`prod_main`**.  
The NFP folder only holds scripts, Excel, and reports.

If Basic uses a **local** docker pg-proxy, use the same URL for NFP:

```powershell
$env:POSTGRES_PROXY_URL="http://127.0.0.1:YOUR_PORT/api/sql"
```

## Run

```powershell
cd "C:\Users\User\Desktop\Commission\NFP Commission\3. Python script"
pip install -r "..\4. data\requirements.txt"

# Easiest: save token in NFP data folder (one line, no quotes)
#   ..\4. data\pg_proxy_token.txt

# Or same as Basic:
#   $env:POSTGRES_PROXY_TOKEN="your-bearer-token"

python nfp_commission.py --test-api
python nfp_commission.py --year 2026
```

Or double-click / run:

```powershell
.\run_nfp.ps1 --year 2026
```

Reports are written to **`..\4. data\reports\`** (not inside the Python script folder).

## Token

Use any one of:

1. File: **`..\4. data\pg_proxy_token.txt`** (recommended for NFP folder)
2. **`POSTGRES_PROXY_TOKEN`** or **`PG_PROXY_TOKEN`** in PowerShell (same as Basic)

Test API only (no report):

```powershell
python nfp_commission.py --test-api
```

### `ConnectionResetError` / WinError 10054

The script reached the server but the connection was **closed by the remote host**. This is **not** a wrong folder path. Common fixes:

1. **New Bearer token** — JWT tokens expire; request a fresh read-only token.
2. **Network** — try another Wi‑Fi, turn VPN on/off, or allow `pg-proxy-production.up.railway.app` through firewall.
3. **Retry** — the script retries 3 times automatically; run again after a minute.
4. Paste token **without** surrounding quotes:  
   `$env:POSTGRES_PROXY_TOKEN="eyJ..."`  (not double quotes inside quotes).
