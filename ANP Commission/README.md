# ANP Commission

## Quick start (Windows)

1. In Cursor: **File → Open Folder** and select the folder that contains `anp_commission.py`.
2. Open terminal (**Ctrl+`**).
3. Run setup once:

```powershell
.\setup.ps1
```

4. Edit `.env` and set your `PG_PROXY_TOKEN` (from the Postgres proxy admin).
5. Generate report:

```powershell
.\run.ps1 --invoice-year 2026
.\run.ps1 --year-invoice-months 2026
```

Same as above: one CSV/XLSX bundle per invoicing month (Jan–Dec) where data exists. Commission payout is the **following** calendar month.

**Headcount:** By default the script prints how many **distinct agents** have at least one qualifying invoice with `invoice_date` in **2026**. Change with `--headcount-year 2025`.

**Tier rule:** ANP tiers use **accumulated invoice total amount** (`total_amount`) within each invoicing month—not sales price after EPP.

**Columns:** Each invoice row includes **ANP Commission Date** (`YYYY-MM`) and a short **remark** (e.g. `February 2026 (paid in this calendar month)`).

Other modes:

```powershell
.\run.ps1 --all-time
.\run.ps1 --payout-month 2026-02
```

Or double-click `run.bat` after setup (pass arguments from a terminal instead).

Reports are saved under `output/`.

## Troubleshooting

| Problem | Fix |
|--------|-----|
| `cd` path not found | Do not `cd` manually. Use `.\setup.ps1` and `.\run.ps1` from the folder where these files live. |
| Python not found | Install Python 3.10+ and enable **Add to PATH**. |
| `PG_PROXY_TOKEN` error | Copy `.env.example` to `.env` and paste your token. |
| Script not allowed to run | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` then retry. |

## Manual run (optional)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python anp_commission.py --all-time
```
