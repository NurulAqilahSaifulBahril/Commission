"""
Paths for the reorganized NFP Commission project.

Expected layout (sibling folders under NFP Commission):

  1. Excel/          — .xlsx price schedules
  2. Screenshot/     — images (not used by scripts)
  3. Python script/  — .py files (run scripts from here)
  4. data/           — .json, requirements.txt, generated reports
  5. _py_cache/      — optional; set PYTHONPYCACHEPREFIX in run_nfp.ps1
"""

from __future__ import annotations

import os
from pathlib import Path

# Folder that contains nfp_commission.py
SCRIPT_DIR = Path(__file__).resolve().parent
# NFP Commission root (parent of Python script folder)
PROJECT_ROOT = SCRIPT_DIR.parent

EXCEL_FILE_NAME = "STRING 650W package STRING INVERTER.xlsx"
# Accept common spellings (Windows paths are case-insensitive; glob helps on copy/rename)
SCHEDULE_620W_JSON_CANDIDATES = (
    "nfp_620w_schedule.json",
    "nfp_620W_schedule.json",
    "NFP_620w_schedule.json",
    "NFP_620W_schedule.json",
)


def _first_existing_dir(*names: str) -> Path:
    """Pick the first folder name that exists under PROJECT_ROOT."""
    for name in names:
        path = PROJECT_ROOT / name
        if path.is_dir():
            return path
    return PROJECT_ROOT / names[0]


def _resolve_excel_dir() -> Path:
    return _first_existing_dir("1. Excel", "Excel")


def _resolve_data_dir() -> Path:
    return _first_existing_dir("4. data", "data")


EXCEL_DIR = _resolve_excel_dir()
DATA_DIR = _resolve_data_dir()


def get_default_excel_path() -> Path:
    preferred = EXCEL_DIR / EXCEL_FILE_NAME
    if preferred.is_file():
        return preferred
    xlsx_files = sorted(EXCEL_DIR.glob("*.xlsx"))
    if not xlsx_files:
        raise FileNotFoundError(
            f"No Excel file in {EXCEL_DIR}. "
            f"Add '{EXCEL_FILE_NAME}' to the Excel folder."
        )
    if len(xlsx_files) == 1:
        return xlsx_files[0]
    for path in xlsx_files:
        if "650" in path.name.upper() or "STRING" in path.name.upper():
            return path
    return xlsx_files[0]


def get_620w_json_path() -> Path:
    """
    620W schedule JSON — expected in:
    C:\\...\\NFP Commission\\4. data\\nfp_620w_schedule.json
    (or nfp_620W_schedule.json)
    """
    for name in SCHEDULE_620W_JSON_CANDIDATES:
        path = DATA_DIR / name
        if path.is_file():
            return path.resolve()

    # Any nfp*620*.json in data folder
    matches = sorted(DATA_DIR.glob("nfp*620*.json"))
    if matches:
        return matches[0].resolve()

    legacy = SCRIPT_DIR / "data"
    for name in SCHEDULE_620W_JSON_CANDIDATES:
        path = legacy / name
        if path.is_file():
            return path.resolve()

    expected = DATA_DIR / SCHEDULE_620W_JSON_CANDIDATES[0]
    raise FileNotFoundError(
        f"620W schedule JSON not found in {DATA_DIR}. "
        f"Expected e.g. {expected}"
    )


def ensure_reports_dir() -> Path:
    """Reports go under data/reports/ (created if missing)."""
    reports = DATA_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    return reports


def _clean_token(raw: str) -> str:
    token = raw.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    return token


def get_proxy_token() -> str:
    """
    Token lookup order:
      1. POSTGRES_PROXY_TOKEN
      2. PG_PROXY_TOKEN (same as many Basic Commission scripts)
      3. 4. data/pg_proxy_token.txt  (one line, no quotes — keeps NFP folder self-contained)
    """
    for key in ("POSTGRES_PROXY_TOKEN", "PG_PROXY_TOKEN"):
        token = _clean_token(os.environ.get(key, ""))
        if token:
            return token

    for filename in ("pg_proxy_token.txt", ".pg_proxy_token"):
        path = DATA_DIR / filename
        if path.is_file():
            token = _clean_token(path.read_text(encoding="utf-8"))
            if token:
                return token
    return ""


def proxy_token_help() -> str:
    token_file = DATA_DIR / "pg_proxy_token.txt"
    return (
        "Database token required (same pg-proxy as Basic Commission).\n"
        "Option A — PowerShell:\n"
        '  $env:POSTGRES_PROXY_TOKEN="your-bearer-token"\n'
        "Option B — file (recommended for NFP folder):\n"
        f"  Create {token_file}\n"
        "  Paste the Bearer token as a single line (no quotes).\n"
        "Optional if Basic uses local docker proxy:\n"
        '  $env:POSTGRES_PROXY_URL="http://127.0.0.1:PORT/api/sql"'
    )
