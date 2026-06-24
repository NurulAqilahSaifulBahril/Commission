#!/usr/bin/env python3
"""
Production Bonus Report -- Outsource Agents (2026).

Rules:
  Sales Price = total_amount - EPP interest
  Production Bonus = Team-based
  - OUM Bonus: 0.5% of Team Total Sales (if Team Total >= 2M AND Personal Sales >= 300K)
  - OGM Bonus: 0.75% of OSA Sales + 0.25% of OUM Sales (if Team Total >= 8M)
  - Applies across ALL packages (Residential, Shop Lot, Factory, etc.)

Tables:
  Table 1A -- OUM Production Bonus Summary
  Table 1B -- OGM Production Bonus Summary (if any)
  Table 2  -- Team Sales Detail
  Table 3  -- Unassigned Agents
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
if _SCRIPT_DIR.name == "3. Python Script":
    _REPO_ROOT = _SCRIPT_DIR.parent.parent
elif _SCRIPT_DIR.name == "5. Production Bonus":
    _REPO_ROOT = _SCRIPT_DIR.parent
else:
    _REPO_ROOT = Path(r"C:\Users\User\OneDrive\Documents\Commission")

_OUTPUT_DIR = _REPO_ROOT / "5. Production Bonus" / "2. Output"
_NFP_SCRIPT = _REPO_ROOT / "2. NFP Commission" / "3. Python script"

AGENT_DETAILS_PATH = _REPO_ROOT / "1. Basic Commission" / "1. Excel" / "1. Agent Details.xlsx"

# Reuse api_client from NFP Commission folder
if str(_NFP_SCRIPT) not in sys.path:
    sys.path.insert(0, str(_NFP_SCRIPT))

try:
    from api_client import query_sql
except ImportError:
    def query_sql(sql: str) -> list[dict[str, Any]]:
        raise ImportError("Cannot import query_sql from api_client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUM_TEAM_THRESHOLD     = Decimal("2000000")
OUM_PERSONAL_THRESHOLD = Decimal("300000")
OUM_BONUS_RATE         = Decimal("0.005")

OGM_TEAM_THRESHOLD     = Decimal("8000000")
OGM_OSA_BONUS_RATE     = Decimal("0.0075")
OGM_OUM_BONUS_RATE     = Decimal("0.0025")

# ---------------------------------------------------------------------------
# Hierarchy Parsing
# ---------------------------------------------------------------------------
def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip()).lower()


def _try_read_excel(path: Path) -> pd.DataFrame | None:
    """Try to read Excel file, copying to temp to avoid lock issues."""
    if not path.is_file():
        return None

    def get_outsource_sheet(xl_file: pd.ExcelFile) -> pd.DataFrame | None:
        for sheet in xl_file.sheet_names:
            if str(sheet).lower().startswith("outsource"):
                return xl_file.parse(sheet)
        return None

    # Try direct read first
    try:
        xl = pd.ExcelFile(path)
        df = get_outsource_sheet(xl)
        if df is not None:
            return df
    except Exception:
        pass
    # Try via temp copy (handles open-file locks)
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            shutil.copy2(path, tmp_path)
            xl = pd.ExcelFile(tmp_path)
            return get_outsource_sheet(xl)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception as e:
        print(f"Warning: Failed to read Agent Details.xlsx: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Fallback hierarchy when Excel is locked or unavailable.
# ---------------------------------------------------------------------------
_FALLBACK_HIERARCHY: dict[str, str] = {
    # OUM
    "gan lai hock": "OUM",
    "phil moo": "OUM", "moo wui kead": "OUM", "philmoowuikead": "OUM",
    "kok shao hong": "OUM",
    "wilson tan": "OUM", "tan wei sheng": "OUM", "wilsontanweisheng": "OUM",
    "dean wai": "OUM", "wai leong yee": "OUM", "deanwaileongyee": "OUM",
    "oliver koh": "OUM", "koh chong lee": "OUM", "olivierkohconglee": "OUM",
    "chan wing on": "OUM",
    "chan jia wei": "OUM",
    "ling liang kang": "OUM",
    "caryn dong": "OUM", "dong leong mui": "OUM",
    "carol siow": "OUM", "siow sio chui": "OUM",
    # OSA
    "lam wai leng": "OSA",
    "tee kok kian": "OSA",
    "koh yeong cherng": "OSA",
    "ang kok xing": "OSA",
    "tey zhi yun": "OSA",
    "mohd azhar bin ibrahim": "OSA",
    "lim chin seng": "OSA",
    "kwong jun sheng": "OSA",
    "lee yue peng": "OSA",
    "too pok jen": "OSA",
    "tay hock xiang": "OSA",
    "ho wen lin": "OSA",
    "ng zhee hao": "OSA",
    "tan wei hung": "OSA",
    "lim kai zhe": "OSA",
    "lee chun xun": "OSA",
    "ling wei perng": "OSA",
    "lee seok yun": "OSA",
    "liew lee ching": "OSA",
    "lee hui wen": "OSA",
    "see cheak ching": "OSA",
    "low chin chai": "OSA",
    "chang soon huat": "OSA",
    # OSA 1
    "mohd hanis bin marjian": "OSA 1",
    "low kim swee": "OSA 1",
    "lai siong hing": "OSA 1",
    "tan sue cherk": "OSA 1",
}

_FALLBACK_TEAM_MAP: dict[str, list[str]] = {
    "dean wai/ wai leong yee": ["dean wai", "wai leong yee", "deanwaileongyee", "lam wai leng", "tee kok kian"],
    "oliver koh/ koh chong lee": ["oliver koh", "koh chong lee", "olivierkohconglee", "koh yeong cherng", "ang kok xing", "tey zhi yun", "mohd azhar bin ibrahim", "lim chin seng", "mohd hanis bin marjian"],
    "chan wing on": ["chan wing on", "kwong jun sheng", "lee yue peng", "too pok jen"],
    "chan jia wei": ["chan jia wei", "tay hock xiang", "ho wen lin", "ng zhee hao"],
    "ling liang kang": ["ling liang kang", "tan wei hung"],
    "caryn dong/ dong leong mui": ["caryn dong", "dong leong mui", "lim kai zhe", "lee chun xun", "ling wei perng"],
    "carol siow/ siow sio chui": ["carol siow", "siow sio chui", "lee seok yun", "liew lee ching", "lee hui wen", "see cheak ching", "low chin chai", "chang soon huat", "tan sue cherk", "low kim swee", "lai siong hing"],
}

# Maps: normalised_name -> tier label (OGM, OUM, OSA, OSA 1, Outsource)
_HIERARCHY: dict[str, str] = {}
# Maps: raw team string (e.g. 'Carol Siow/ Siow Sio Chui') -> list of member normalised names
_TEAM_MAP: dict[str, list[str]] = {}
# Maps: raw ogm string -> list of member normalised names
_OGM_TEAM_MAP: dict[str, list[str]] = {}


def _build_hierarchy() -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    global _HIERARCHY, _TEAM_MAP, _OGM_TEAM_MAP
    if _HIERARCHY:
        return _HIERARCHY, _TEAM_MAP, _OGM_TEAM_MAP

    result: dict[str, str] = {}
    team_map: dict[str, list[str]] = {}
    ogm_team_map: dict[str, list[str]] = {}

    df = _try_read_excel(AGENT_DETAILS_PATH)
    if df is None:
        print("  Note: Agent Details.xlsx unavailable; using built-in hierarchy fallback.",
              file=sys.stderr)
        _HIERARCHY = dict(_FALLBACK_HIERARCHY)
        _TEAM_MAP = dict(_FALLBACK_TEAM_MAP)
        _OGM_TEAM_MAP = {}
        return _HIERARCHY, _TEAM_MAP, _OGM_TEAM_MAP

    has_ogm = "OGM" in df.columns
    current_ogms: list[str] = []
    current_ogm_team: str | None = None
    current_oums: list[str] = []
    current_oum_team: str | None = None

    for _, row in df.iterrows():
        # Parse names in current row
        row_ogms = []
        if has_ogm:
            val = str(row.get("OGM", "nan")).strip()
            if val.lower() not in ("nan", ""):
                current_ogm_team = val
                if current_ogm_team not in ogm_team_map:
                    ogm_team_map[current_ogm_team] = []
                for n in val.split("/"):
                    n = _norm_name(n)
                    if n:
                        result.setdefault(n, "OGM")
                        row_ogms.append(n)

        row_oums = []
        val = str(row.get("OUM", "nan")).strip()
        if val.lower() not in ("nan", ""):
            current_oum_team = val
            if current_oum_team not in team_map:
                team_map[current_oum_team] = []
            for n in val.split("/"):
                n = _norm_name(n)
                if n:
                    result.setdefault(n, "OUM")
                    row_oums.append(n)

        row_osas = []
        val = str(row.get("OSA", "nan")).strip()
        if val.lower() not in ("nan", ""):
            for n in val.split("/"):
                n = _norm_name(n)
                if n:
                    result.setdefault(n, "OSA")
                    row_osas.append(n)

        val = str(row.get("OSA 1", "nan")).strip()
        if val.lower() not in ("nan", ""):
            for n in val.split("/"):
                n = _norm_name(n)
                if n:
                    result.setdefault(n, "OSA 1")
                    row_osas.append(n)

        # Hierarchy tracking logic
        if row_ogms:
            current_ogms = row_ogms
            current_oums = []
            current_oum_team = None
        if row_oums:
            current_oums = row_oums

        # Add members to current OUMs
        if current_oum_team:
            # OUMs belong to their own team
            for n in current_oums:
                if n not in team_map[current_oum_team]:
                    team_map[current_oum_team].append(n)
            # OSAs belong to the OUM's team
            for n in row_osas:
                if n not in team_map[current_oum_team]:
                    team_map[current_oum_team].append(n)

        # Add members to current OGMs
        if current_ogm_team:
            for n in current_ogms:
                if n not in ogm_team_map[current_ogm_team]:
                    ogm_team_map[current_ogm_team].append(n)
            for n in row_oums + row_osas:
                if n not in ogm_team_map[current_ogm_team]:
                    ogm_team_map[current_ogm_team].append(n)

    if not result:
        result = dict(_FALLBACK_HIERARCHY)
        team_map = dict(_FALLBACK_TEAM_MAP)
        ogm_team_map = {}

    # Explicit mapping for database names that combine parts of split Excel names
    db_mapping = [
        ("deanwaileongyee", "Dean Wai/ Wai Leong Yee", "OUM"),
        ("philmoowuikead", "Phil Moo/ Moo Wui Kead", "OUM"),
        ("wilsontanweisheng", "Wilson Tan/ Tan Wei Sheng", "OUM"),
        ("olivierkohconglee", "Oliver Koh/ Koh Chong Lee", "OUM"),
    ]
    for db_norm, team_key, tier in db_mapping:
        result[db_norm] = tier
        # Add to OUM team map
        if team_key in team_map:
            if db_norm not in team_map[team_key]:
                team_map[team_key].append(db_norm)
        # Also check lowercase key (as in fallback map)
        lower_key = team_key.lower().strip()
        if lower_key in team_map:
            if db_norm not in team_map[lower_key]:
                team_map[lower_key].append(db_norm)

        # Add to OGM team map
        first_member = _norm_name(team_key.split("/")[0])
        for ogm_key, ogm_members in ogm_team_map.items():
            if first_member in ogm_members:
                if db_norm not in ogm_members:
                    ogm_members.append(db_norm)

    _HIERARCHY = result
    _TEAM_MAP = team_map
    _OGM_TEAM_MAP = ogm_team_map
    return result, team_map, ogm_team_map


def get_agent_tier(agent_name: str, is_db_outsource: bool) -> str:
    """Return display tier for an agent."""
    hier, _, _ = _build_hierarchy()
    norm = _norm_name(agent_name)
    if norm in hier:
        return hier[norm]
    # Not in Excel — fall back based on DB flag (Outsource = OSA)
    return "OSA" if is_db_outsource else "Unknown"


# ---------------------------------------------------------------------------
# SQL & Property Classification
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    for env_path in [_SCRIPT_DIR / ".env", _NFP_SCRIPT / ".env", _REPO_ROOT / ".env"]:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
                value = value[1:-1].strip()
            if value.lower().startswith("bearer "):
                value = value[7:].strip()
            if name and name not in os.environ:
                os.environ[name] = value
        break


def _get_token() -> str | None:
    return os.environ.get("PG_PROXY_TOKEN") or os.environ.get("POSTGRES_PROXY_TOKEN")


def _invoices_sql(year: int, upto_month: int | None = None) -> str:
    month_filter = f"AND EXTRACT(MONTH FROM i.invoice_date)::int <= {upto_month}" if upto_month else ""
    return f"""
WITH candidates AS (
  SELECT
    i.bubble_id,
    i.id            AS invoice_row_id,
    i.is_latest,
    i.invoice_number,
    i.invoice_date,
    COALESCE(i.total_amount, 0)::numeric              AS total_amount,
    COALESCE(
      NULLIF(epp_items.epp_interest, 0),
      NULLIF(pay.epp_sum, 0),
      NULLIF(
        CASE
          WHEN i.effective_epp > 1.0 AND i.effective_epp < 2.0
            THEN (i.total_amount * (i.effective_epp - 1.0) / i.effective_epp)::numeric
          WHEN i.effective_epp >= 2.0 AND i.effective_epp <= 100.0
            THEN (i.total_amount * (i.effective_epp / 100.0) / (1.0 + i.effective_epp / 100.0))::numeric
          ELSE 0
        END, 0), 0
    )::numeric AS epp_interest,
    COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name, '(unknown)') AS customer_name,
    COALESCE(NULLIF(TRIM(a.name), ''), '(unknown)')   AS agent_name,
    a.agent_type,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type)      AS seda_nem_type,
    ref.project_type                                  AS referral_project_type,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC, i.invoice_date DESC NULLS LAST, i.id DESC
    ) AS rn
  FROM invoice i
  INNER JOIN agent a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
  LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
  LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
  LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
  LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(ii_dedup.epp_interest_amount), 0) AS epp_interest
    FROM (
      SELECT MAX(
        CASE WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%' OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
             THEN COALESCE(ii.amount, ii.unit_price, 0) ELSE 0 END
      ) AS epp_interest_amount
      FROM invoice_item ii
      WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
      GROUP BY TRIM(
        REGEXP_REPLACE(
          REGEXP_REPLACE(
            REGEXP_REPLACE(COALESCE(ii.description, ''), 'moths', 'months', 'gi'),
            '(\\d+)\\s*months',
            '\\1months',
            'gi'
          ),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) ii_dedup
  ) epp_items ON TRUE
  LEFT JOIN LATERAL (
    SELECT SUM(COALESCE(p.epp_cost, 0)) AS epp_sum
    FROM payment p
    WHERE p.linked_invoice = i.bubble_id
  ) pay ON TRUE
  WHERE i.invoice_date IS NOT NULL
    AND EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
    {month_filter}
    AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
    AND COALESCE(i.percent_of_total_amount, 0) >= 100.0
)
SELECT * FROM candidates WHERE rn = 1
ORDER BY agent_name ASC, invoice_date ASC NULLS LAST, invoice_number ASC NULLS LAST
""".strip()


def classify_property_type(row: dict[str, Any]) -> str:
    nem = str(row.get("seda_nem_type") or "").upper().strip()
    if "RAKYAT" in nem: return "Residential"
    if "SHOPLOT" in nem or "SHOP-LOT" in nem or "COMMERCIAL" in nem: return "Shop Lot"
    if "FACTORY" in nem: return "Factory"
    if "GOVERNMENT" in nem or "GOV" in nem: return "Government"
    if "NGO" in nem: return "NGO"
    if "CORPORATE" in nem: return "Corporate"

    ref = str(row.get("referral_project_type") or "").upper().strip()
    if "RESIDENTIAL" in ref: return "Residential"
    if "SHOP-LOT" in ref or "SHOPLOT" in ref or "COMMERCIAL" in ref: return "Shop Lot"
    if "FACTORY" in ref: return "Factory"
    if "GOVERNMENT" in ref or "GOV" in ref: return "Government"
    if "NGO" in ref: return "NGO"
    if "CORPORATE" in ref: return "Corporate"

    for fld in ("package_type", "package_name_snapshot", "description"):
        val = str(row.get(fld) or "").upper().strip()
        if not val:
            continue
        if "FACTORY" in val: return "Factory"
        if "GOVERNMENT" in val or "GOV" in val: return "Government"
        if "NGO" in val: return "NGO"
        if "CORPORATE" in val: return "Corporate"
        if "RESIDENTIAL" in val: return "Residential"
        if "SHOP" in val or "COMMERCIAL" in val: return "Shop Lot"

    return "Residential"


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------
def _fmt_money(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.00'), rounding=ROUND_HALF_UP):,}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt_row(headers), sep, *[fmt_row(r) for r in rows]])


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------
def build_report(year: int, upto_month: int | None = None, may_only: bool = False) -> dict | None:
    # Support legacy may_only flag: treat it as upto_month=5
    if may_only and upto_month is None:
        upto_month = 5
    _load_dotenv()

    token = _get_token()
    if not token:
        token_file = (_NFP_SCRIPT / ".." / "4. data" / "pg_proxy_token.txt").resolve()
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
    if not token:
        print("ERROR: No proxy token found. Set PG_PROXY_TOKEN.", file=sys.stderr)
        return None

    os.environ["POSTGRES_PROXY_TOKEN"] = token

    print("Parsing Agent Hierarchy...")
    hier, team_map, ogm_team_map = _build_hierarchy()

    label = f"Jan–{['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][upto_month]}" if upto_month else "full year"
    print(f"Fetching 100% paid invoices for year {year} ({label})...")
    rows = query_sql(_invoices_sql(year, upto_month=upto_month))
    print(f"  {len(rows)} invoice rows fetched.\n")

    # Accumulate agent sales across ALL packages
    agent_sales: dict[str, Decimal] = {}
    agent_details: dict[str, list] = {}  # agent_name -> list of detail rows

    total_invoices = 0

    for r in rows:
        agent_type_raw = str(r.get("agent_type") or "").lower()
        is_db_outsource = "outsource" in agent_type_raw

        raw_name = str(r.get("agent_name") or "(unknown)").strip()
        norm = _norm_name(raw_name)

        # Determine tier from hierarchy
        tier = get_agent_tier(raw_name, is_db_outsource)

        # Only process outsource agents
        if tier == "Unknown":
            continue

        prop_type = classify_property_type(r)
        total = Decimal(str(r.get("total_amount") or 0))
        epp   = Decimal(str(r.get("epp_interest") or 0))
        sales_price = total - epp

        customer_name  = str(r.get("customer_name") or "(unknown)").strip()
        invoice_number = str(r.get("invoice_number") or "").strip()
        invoice_date   = str(r.get("invoice_date") or "")[:10]

        total_invoices += 1

        if norm not in agent_sales:
            agent_sales[norm] = Decimal(0)
        agent_sales[norm] += sales_price

        if raw_name not in agent_details:
            agent_details[raw_name] = []
        agent_details[raw_name].append({
            "tier": tier,
            "customer_name": customer_name,
            "invoice_number": invoice_number,
            "prop_type": prop_type,
            "invoice_date": invoice_date,
            "sales_price": sales_price
        })

    # OUM Bonus Calculation
    oum_summary = []
    
    # We will build Table 2 (Team detail)
    t2_rows: list[list[str]] = []

    processed_oums = set()
    for team_name, team_members in team_map.items():
        if team_name in processed_oums:
            continue
            
        # team_name is raw string like "Carol Siow/ Siow Sio Chui"
        
        # OUMs can be a paired team, so "personal sales" here means the combined personal sales of the OUMs 
        # (the people actually listed as OUM in that team).
        # We can extract the OUM names from team_name
        oum_names = [_norm_name(n) for n in team_name.split("/")]
        personal_sales = sum(agent_sales.get(n, Decimal(0)) for n in oum_names)
        team_total = sum(agent_sales.get(member, Decimal(0)) for member in team_members)
        
        qualified = "Yes" if (team_total >= OUM_TEAM_THRESHOLD and personal_sales >= OUM_PERSONAL_THRESHOLD) else "No"
        bonus = (team_total * OUM_BONUS_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if qualified == "Yes" else Decimal(0)

        oum_summary.append([
            team_name,
            _fmt_money(personal_sales),
            _fmt_money(team_total),
            qualified,
            _fmt_money(bonus)
        ])
        processed_oums.add(team_name)

        # Add team details to T2
        for member_norm in team_members:
            for raw_name, details in agent_details.items():
                if _norm_name(raw_name) == member_norm:
                    for d in details:
                        row_bonus = (d["sales_price"] * OUM_BONUS_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if qualified == "Yes" else Decimal(0)
                        t2_rows.append([
                            raw_name,
                            d["customer_name"],
                            d["invoice_number"],
                            d["prop_type"],
                            d["invoice_date"],
                            _fmt_money(d["sales_price"]),
                            _fmt_money(row_bonus)
                        ])
                        
    # OGM Bonus Calculation
    ogm_summary = []
    processed_ogms = set()
    for team_name, team_members in ogm_team_map.items():
        if team_name in processed_ogms:
            continue
            
        team_total = sum(agent_sales.get(member, Decimal(0)) for member in team_members)
        
        qualified = "Yes" if team_total >= OGM_TEAM_THRESHOLD else "No"
        
        osa_sales = sum(agent_sales.get(m, Decimal(0)) for m in team_members if hier.get(m) in ("OSA", "OSA 1", "Outsource"))
        oum_sales = sum(agent_sales.get(m, Decimal(0)) for m in team_members if hier.get(m) == "OUM")
        
        if qualified == "Yes":
            bonus = (osa_sales * OGM_OSA_BONUS_RATE + oum_sales * OGM_OUM_BONUS_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            bonus = Decimal(0)
            
        ogm_summary.append([
            team_name,
            _fmt_money(team_total),
            _fmt_money(osa_sales),
            _fmt_money(oum_sales),
            qualified,
            _fmt_money(bonus)
        ])
        processed_ogms.add(team_name)

    T1_HEADERS = ["OUM Name", "Personal Sales", "Team Total Sales", "Qualified", "Production Bonus"]
    T1_OGM_HEADERS = ["OGM Name", "Team Total Sales", "OSA Sales", "OUM Sales", "Qualified", "Production Bonus"]
    T2_HEADERS = ["Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Sales Price", "Production Bonus"]

    oum_summary.sort(key=lambda x: str(x[0]).lower())
    ogm_summary.sort(key=lambda x: str(x[0]).lower())
    # Sort Table 2 by Agent Name, Customer Name, then Invoice Date
    t2_rows.sort(key=lambda x: (str(x[0]).lower(), str(x[1]).lower(), x[4]))

    return {
        "oum_summary": oum_summary,
        "ogm_summary": ogm_summary,
        "team_detail": t2_rows,
        "headers": {
            "oum": T1_HEADERS,
            "ogm": T1_OGM_HEADERS,
            "detail": T2_HEADERS
        }
    }

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Production Bonus report for Outsource agents.")
    parser.add_argument("--year", type=int, default=2026, help="Invoice date year (default: 2026)")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV output")
    parser.add_argument("--May", "--may", action="store_true", dest="May", help="Limit report to January through May")
    parser.add_argument("--upto-month", type=int, default=None, choices=range(1, 13),
                        help="Limit report to January through this month (1-12). Overrides --May.")
    args = parser.parse_args(argv)

    upto_month = args.upto_month if args.upto_month else (5 if args.May else None)
    report_data = build_report(args.year, upto_month=upto_month)
    if report_data is None:
        return 2
        
    oum_summary = report_data["oum_summary"]
    ogm_summary = report_data["ogm_summary"]
    t2_rows = report_data["team_detail"]
    
    T1_HEADERS = report_data["headers"]["oum"]
    T1_OGM_HEADERS = report_data["headers"]["ogm"]
    T2_HEADERS = report_data["headers"]["detail"]

    sep = "=" * 72
    print(sep)
    print(f"  PRODUCTION BONUS REPORT  --  {args.year}  (Outsource Agents)")
    print(sep)
    print()

    print("Table 1A: OUM Production Bonus Summary")
    print(_render_table(T1_HEADERS, oum_summary) if oum_summary else "  (no OUM data)")
    print()

    if ogm_summary:
        print("Table 1B: OGM Production Bonus Summary")
        print(_render_table(T1_OGM_HEADERS, ogm_summary))
        print()

    print("Table 2: Team Sales Detail")
    print(_render_table(T2_HEADERS, t2_rows) if t2_rows else "  (no team invoices)")
    print()

    # ---------------------------------------------------------------------------
    # Save CSVs
    # ---------------------------------------------------------------------------
    if not args.no_csv:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print("Saving CSV files...")
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        summary_path = _OUTPUT_DIR / f"outsource_production_bonus_oum_summary_{args.year}_{stamp}.csv"
        with summary_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(T1_HEADERS)
            w.writerows(oum_summary)
        print(f"  Saved: {summary_path}")
        
        if ogm_summary:
            ogm_summary_path = _OUTPUT_DIR / f"outsource_production_bonus_ogm_summary_{args.year}_{stamp}.csv"
            with ogm_summary_path.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(T1_OGM_HEADERS)
                w.writerows(ogm_summary)
            print(f"  Saved: {ogm_summary_path}")

        detail_path = _OUTPUT_DIR / f"outsource_production_bonus_team_detail_{args.year}_{stamp}.csv"
        with detail_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(T2_HEADERS)
            w.writerows(t2_rows)
        print(f"  Saved: {detail_path}")

        print("Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
