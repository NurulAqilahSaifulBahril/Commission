# Commission — Documents folder

**Folder:** `C:\Users\User\OneDrive\Documents\Commission`

Token is stored in `.env` files (same as running each report alone). You do **not** need `$env:PG_PROXY_TOKEN` each time.

## Individual reports

```powershell
cd "C:\Users\User\OneDrive\Documents\Commission\Basic Commission\3. Python Script"
python full_internal_basic_commission.py

cd "C:\Users\User\OneDrive\Documents\Commission\ANP Commission\3. Python Script"
python anp_commission.py
REM Default: full-year invoice-year-2026 (same data as finance Excel/PDF ANP sheet)

cd "C:\Users\User\OneDrive\Documents\Commission\NFP Commission\3. Python script"
python nfp_commission.py
REM Do not run anp_commission.py from the NFP folder — that file is not there.

cd "C:\Users\User\OneDrive\Documents\Commission\NFP Commission\3. Python script"
python nfp_commission.py
```

## Finance pack (Basic + ANP + NFP)

```powershell
cd C:\Users\User\OneDrive\Documents\Commission
python build_finance_commission_pack.py --year 2026
```

| Output | Command |
|--------|---------|
| Excel only | `python build_finance_commission_pack.py --year 2026` or `run_finance_pack.bat` |
| PDF only (finance summary) | `python build_finance_commission_pack.py --year 2026 --pdf` or `run_finance_pdf.bat` |
| Excel + PDF | `python build_finance_commission_pack.py --year 2026 --both` or `run_finance_both.bat` |

Files save to `Finance Output\Commission_Pack_2026_<timestamp>.xlsx` / `.pdf`

PDF layout (presentation for finance):
- Cover page with KPI totals
- Dashboard with pie chart (Basic/ANP/NFP mix) and bar chart (top 3 earners)
- One page per table (summary, each agent table, each invoice detail)

## Token (one-time / when expired)

Keep `PG_PROXY_TOKEN` in any of these (already set up):

- `ANP Commission\3. Python Script\.env`
- `Basic Commission\3. Python Script\.env`
- `NFP Commission\4. data\pg_proxy_token.txt`

Get a new JWT from your Postgres proxy admin when you see **Token expired**.

## GitHub

```powershell
.\push_to_github.ps1
```

(If you add it; otherwise use `git` from this folder.)
