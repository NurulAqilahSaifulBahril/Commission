# Commission — Finance Commission Dashboard

## ⬇️ Download

### **[Download the latest release →](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest)**

[![Latest release](https://img.shields.io/github/v/release/NurulAqilahSaifulBahril/Commission?label=latest%20version&style=for-the-badge)](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/NurulAqilahSaifulBahril/Commission/total?style=for-the-badge)](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest)

On that page, grab **`CommissionDashboard-Setup-<version>.exe`** and run it.

| Step | What happens |
|------|--------------|
| 1. Run the installer | Installs to `%LOCALAPPDATA%\Programs\Eternalgy\Commission Dashboard` — no admin rights needed |
| 2. Setup window opens | Creates a private Python environment, installs dependencies, generates a session secret, asks you to create the first admin account |
| 3. Add your token | Put `PG_PROXY_TOKEN=<your token>` in the `.env` file inside the install folder |
| 4. Launch | Start Menu → **Finance Commission Dashboard**. It opens <http://127.0.0.1:5001> |

**Requires Python 3.10+** on the machine ([python.org](https://www.python.org/downloads/windows/) — tick *Add python.exe to PATH*). The installer checks for it and tells you if it is missing.

### Updating

You only ever run the installer once. After that the dashboard updates itself:

- It checks GitHub for a newer release on startup and hourly after that.
- When one exists, a **Software Update** panel appears in the sidebar showing the new version number and a link to the release notes.
- An admin clicks **Install Update** — the app downloads the package, verifies its SHA-256 checksum, swaps the files in, restarts, and the browser tab reloads on its own.
- Your database, `.env`, Excel workbooks, generated reports and admin-edited rule settings are never touched by an update.

If an update fails part-way, the previous version is restored automatically and the reason is written to `8. Web Dashboard\dashboard.log`.

### Cutting a release (maintainers)

```bash
git tag v1.2.0 && git push origin v1.2.0
```

That triggers [`.github/workflows/release.yml`](.github/workflows/release.yml), which builds the installer and the OTA package and publishes them to a new GitHub Release. Every installed dashboard picks it up within the hour.

To build locally instead:

```bash
python tools/build_package.py --version 1.2.0
```

---

## Running from source

**Folder:** `C:\Users\User\OneDrive\Documents\Commission`

Token is stored in `.env` files (same as running each report alone). You do **not** need `$env:PG_PROXY_TOKEN` each time.

## Individual reports

```powershell
cd "C:\Users\User\OneDrive\Documents\Commission\1. Basic Commission\3. Python Script"
python full_internal_basic_commission.py

cd "C:\Users\User\OneDrive\Documents\Commission\3. ANP Commission\3. Python Script"
python anp_commission.py
REM Default: full-year invoice-year-2026 (same data as finance Excel/PDF ANP sheet)

cd "C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script"
python nfp_commission.py
REM Do not run anp_commission.py from the NFP folder — that file is not there.

cd "C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script"
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

- `3. ANP Commission\3. Python Script\.env`
- `1. Basic Commission\3. Python Script\.env`
- `2. NFP Commission\4. data\pg_proxy_token.txt`

Get a new JWT from your Postgres proxy admin when you see **Token expired**.

## GitHub

```powershell
.\push_to_github.ps1
```

(If you add it; otherwise use `git` from this folder.)
