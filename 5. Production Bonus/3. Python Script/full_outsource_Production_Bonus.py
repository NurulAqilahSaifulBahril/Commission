#!/usr/bin/env python3
"""
Production Bonus Report -- Outsource Agents (2026).

Rules:
  Scope: any invoice with at least one payment dated January 1 of the report
    year through today — invoice_date itself is not a filter, so an invoice
    issued earlier still counts once a payment lands in the window. Not
    just invoices that have reached 100% paid.
  Collected Payment = every payment received for the invoice, whenever it was
    made, minus its total EPP interest. The window above decides which
    invoices count as this year's production, not how much of each one does.
    EPP interest is read, per invoice, in priority order:
    1) the invoice's own "EPP Interest (RM... x N%) ... Months" line item(s)
       (the real-world source of truth — usually one such line per EPP
       instalment payment); 2) payment.epp_cost summed across its payments
       (rarely populated in practice); 3) calculated from the invoice's
       effective_epp rate applied to the amount actually paid. A
       partially-paid invoice contributes only what has actually come in,
       and its figure grows as further payments land — there is no "wait
       until fully paid" gate.
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
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

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
# Rates and thresholds entered on the dashboard Data page. Each falls back to
# the constant above, so a period not set up there produces the same figures it
# always did.
#
# NOTE: agent ROLES and TEAM MEMBERSHIP are deliberately still read from the
# hierarchy below. agent_roles carries the roles but has `reports_to` filled in
# for only one outsource agent, and this bonus is team-based — switching before
# that column is populated would empty every team and zero every bonus.
# ---------------------------------------------------------------------------
def _load_production_bonus_rules(effective_from: str | None = None):
    import os as _os
    import sys as _sys
    from datetime import date as _date

    key = effective_from or _date.today().strftime("%Y-%m")
    dashboard_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "8. Web Dashboard",
    )
    try:
        if dashboard_dir not in _sys.path:
            _sys.path.insert(0, dashboard_dir)
        import db as _db
        payload = _db.get_production_bonus_rules(key)
        if not payload.get("saved"):
            return None
    except Exception as exc:
        print(f"  Note: could not read production bonus rules from the dashboard DB "
              f"({type(exc).__name__}: {exc}); using built-in defaults.", file=sys.stderr)
        return None
    print(f"  Production bonus rules loaded for {key} from the dashboard Data page.")
    return payload


def _apply_production_bonus_rules(payload) -> None:
    global OUM_TEAM_THRESHOLD, OUM_PERSONAL_THRESHOLD, OUM_BONUS_RATE
    global OGM_TEAM_THRESHOLD, OGM_OSA_BONUS_RATE, OGM_OUM_BONUS_RATE

    rules = payload.get("rules") or {}

    def _amount(key, current):
        raw = str(rules.get(key) or "").replace(",", "").strip()
        if not raw:
            return current
        try:
            return Decimal(raw)
        except Exception:
            return current

    def _rate(key, current):
        """Stored as a percentage on the page; used as a fraction here."""
        raw = str(rules.get(key) or "").replace(",", "").strip()
        if not raw:
            return current
        try:
            return Decimal(raw) / Decimal("100")
        except Exception:
            return current

    OUM_TEAM_THRESHOLD = _amount("oum_team_target", OUM_TEAM_THRESHOLD)
    OUM_PERSONAL_THRESHOLD = _amount("oum_personal_target", OUM_PERSONAL_THRESHOLD)
    OUM_BONUS_RATE = _rate("oum_rate_pct", OUM_BONUS_RATE)
    OGM_TEAM_THRESHOLD = _amount("ogm_team_target", OGM_TEAM_THRESHOLD)
    OGM_OSA_BONUS_RATE = _rate("ogm_osa_rate_pct", OGM_OSA_BONUS_RATE)
    OGM_OUM_BONUS_RATE = _rate("ogm_oum_rate_pct", OGM_OUM_BONUS_RATE)


_PB_RULES = _load_production_bonus_rules()
if _PB_RULES:
    _apply_production_bonus_rules(_PB_RULES)

# ---------------------------------------------------------------------------
# Hierarchy Parsing
# ---------------------------------------------------------------------------
def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip()).lower()


# ---------------------------------------------------------------------------
# Hardcoded outsource hierarchy. The Agent Roles & Hierarchy page carries the
# roles, but its `reports_to` column is not yet populated for outsource agents
# and this bonus is team-based — switching before that column is filled in
# would empty every team and zero every bonus.
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
    _HIERARCHY = dict(_FALLBACK_HIERARCHY)
    _TEAM_MAP = dict(_FALLBACK_TEAM_MAP)
    _OGM_TEAM_MAP = {}
    return _HIERARCHY, _TEAM_MAP, _OGM_TEAM_MAP


def _agent_type_override(agent_name: str, invoice_date: Any = None):
    """'internal' / 'outsource' from the Agent Roles & Hierarchy page for the
    invoice's own month, or None when that page says nothing about this
    agent."""
    try:
        rates_dir = str(_REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import get_agent_type_override as _override
    except Exception:
        return None
    raw = str(invoice_date or "")[:7]
    month = year = None
    if len(raw) == 7 and raw[4] == "-":
        try:
            year, month = int(raw[:4]), int(raw[5:])
        except ValueError:
            month = year = None
    return _override(agent_name, month=month, year=year)


def _agent_display_name(agent_name: str, invoice_date: Any = None):
    """Full Name from the Agent Roles & Hierarchy page for the invoice's own
    month, falling back to that page's own Agent Name (from eeAdmin) field,
    or None when that page says nothing about this agent."""
    try:
        rates_dir = str(_REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import get_agent_display_name as _display
    except Exception:
        return None
    raw = str(invoice_date or "")[:7]
    month = year = None
    if len(raw) == 7 and raw[4] == "-":
        try:
            year, month = int(raw[:4]), int(raw[5:])
        except ValueError:
            month = year = None
    return _display(agent_name, month=month, year=year)


def get_agent_tier(agent_name: str, is_db_outsource: bool) -> str:
    """Return display tier for an agent."""
    hier, _, _ = _build_hierarchy()
    norm = _norm_name(agent_name)
    if norm in hier:
        return hier[norm]
    # Not in the hierarchy map — fall back based on DB flag (Outsource = OSA)
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


# Bad/duplicate payment rows excluded from any "amount actually paid"
# calculation — the same exclusion list Basic/NFP/ANP Commission use.
_EXCLUDED_PAYMENT_IDS_SQL = "(101334, 104412, 101333, 104413, 4899)"


def _invoices_sql(year: int) -> str:
    """Any invoice, regardless of its own invoice_date, that has received at
    least one real payment dated Jan 1 of `year` through today — an old
    invoice with a fresh payment still counts.

    Collected Payment is every payment on such an invoice, whenever it was
    made, less its EPP interest. The year decides WHICH invoices are this
    year's production; it does not carve up the money within one. A partly
    paid invoice contributes only what has come in, and its figure grows as
    further payments land — there is no "wait until 100% paid" gate."""
    return f"""
WITH candidates AS (
  SELECT
    i.bubble_id,
    i.id            AS invoice_row_id,
    i.is_latest,
    i.invoice_number,
    i.invoice_date,
    COALESCE(i.total_amount, 0)::numeric              AS total_amount,
    pay.paid_amount,
    COALESCE(
      NULLIF(epp_items.epp_interest, 0),
      NULLIF(pay.paid_epp_cost_sum, 0),
      NULLIF(
        CASE
          WHEN i.effective_epp > 1.0 AND i.effective_epp < 2.0
            THEN (pay.paid_amount * (i.effective_epp - 1.0) / i.effective_epp)::numeric
          WHEN i.effective_epp >= 2.0 AND i.effective_epp <= 100.0
            THEN (pay.paid_amount * (i.effective_epp / 100.0) / (1.0 + i.effective_epp / 100.0))::numeric
          ELSE 0
        END, 0), 0
    )::numeric AS paid_epp_total,
    -- The invoice's own Sales Price, the figure Basic & NFP reports: the whole
    -- invoice less its whole EPP interest, with no payment window applied. It
    -- sits beside the collected figure rather than replacing it -- one says what
    -- the deal is worth, the other what has actually come in, and the bonus is
    -- still earned on the latter.
    (COALESCE(i.total_amount, 0) - COALESCE(invoice_epp.epp_cost, 0))::numeric
                                                      AS invoice_sales_price,
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
  -- Agents live in BOTH the agent table and the user table since the
  -- 2026-07-20 "agent retirement" migration (same bubble_id kept).
  INNER JOIN (
    SELECT DISTINCT ON (au.bubble_id) au.bubble_id, au.name, au.agent_type
    FROM (
      SELECT u.bubble_id, u.name, u.agent_type, 1 AS pri FROM "user" u
       WHERE u.bubble_id IS NOT NULL AND COALESCE(BTRIM(u.agent_type), '') <> ''
      UNION ALL
      SELECT ag.bubble_id, ag.name, ag.agent_type, 2 FROM agent ag
       WHERE ag.bubble_id IS NOT NULL
      UNION ALL
      SELECT u2.bubble_id, u2.name, u2.agent_type, 3 FROM "user" u2
       WHERE u2.bubble_id IS NOT NULL
    ) au
    ORDER BY au.bubble_id, au.pri
  ) a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
  LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
  LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
  LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
  LEFT JOIN LATERAL (
    -- EVERYTHING collected on the invoice, whenever it was paid, and the sum of
    -- payment.epp_cost across those payments (rarely populated in practice --
    -- see epp_items below, the real-world source of truth). The report year
    -- decides which invoices appear (see the EXISTS below), not how much of
    -- each one counts: an invoice that took a deposit in December and the rest
    -- in January is one deal, and splitting it across two reports made the
    -- collected figure smaller than the money the customer had handed over.
    SELECT
      COALESCE(SUM(p.amount), 0)::numeric AS paid_amount,
      COALESCE(SUM(COALESCE(p.epp_cost, 0)), 0)::numeric AS paid_epp_cost_sum
    FROM payment p
    WHERE p.linked_invoice = i.bubble_id
      AND p.id NOT IN {_EXCLUDED_PAYMENT_IDS_SQL}
  ) pay ON TRUE
  LEFT JOIN LATERAL (
    -- EPP interest booked as its own invoice line item (e.g. "EPP Interest
    -- (RM15,179.32 x 6%) 36 Months PBB") — one such line typically appears
    -- per EPP instalment payment. Unbounded, matching the payments above: the
    -- date window used to sit here too, so that both sides covered the same
    -- period. Keeping it while the payments went unbounded would deduct one
    -- year's interest from every year's money.
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
    -- The invoice's WHOLE EPP, unbounded by the payment window -- what Basic &
    -- NFP subtracts from total_amount. Same shape as their epp_items: the line
    -- items' own `epp` field when any carries one, else their EPP interest
    -- amounts, deduplicated on the normalised description because the same
    -- line is repeated per instalment.
    SELECT COALESCE(
             NULLIF(SUM(CASE WHEN COALESCE(d.epp_val, 0) > 0 THEN d.epp_val ELSE 0 END), 0),
             SUM(d.epp_interest_amount),
             0
           )::numeric AS epp_cost
    FROM (
      SELECT
        MAX(COALESCE(ii.epp, 0)) AS epp_val,
        MAX(
          CASE WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%'
                    OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
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
    ) d
  ) invoice_epp ON TRUE
  WHERE COALESCE(i.is_deleted, FALSE) IS NOT TRUE
    AND pay.paid_amount > 0
    -- The report year gate, and the only place it applies: the invoice has to
    -- have seen money this year to be this year's production. Without it every
    -- invoice ever paid would join the report -- 219 more of them, enough to
    -- push a team over its threshold and pay a bonus on business done last year.
    AND EXISTS (
      SELECT 1
      FROM payment p2
      WHERE p2.linked_invoice = i.bubble_id
        AND p2.id NOT IN {_EXCLUDED_PAYMENT_IDS_SQL}
        AND p2.payment_date >= '{int(year)}-01-01'
        AND p2.payment_date <= NOW()
    )
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
def build_report(year: int) -> dict | None:
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

    print(f"Fetching invoices with real payments received, {year}-01-01 through today...")
    rows = query_sql(_invoices_sql(year))
    print(f"  {len(rows)} invoice rows fetched.\n")

    # Accumulate agent sales across ALL packages
    agent_sales: dict[str, Decimal] = {}          # collected payment, drives the bonus
    agent_invoice_sales: dict[str, Decimal] = {}   # invoiced Sales Price, reported only
    agent_details: dict[str, list] = {}  # agent_name -> list of detail rows

    total_invoices = 0

    for r in rows:
        agent_type_raw = str(r.get("agent_type") or "").lower()
        is_db_outsource = agent_type_raw.strip() not in {"internal", "full time"}

        raw_name = str(r.get("agent_name") or "(unknown)").strip()
        norm = _norm_name(raw_name)

        # The Agent Roles & Hierarchy page wins over Postgres' agent_type (and
        # over the hardcoded hierarchy below): agent_type is blank for several
        # internal agents, and blank reads as outsource everywhere.
        override = _agent_type_override(raw_name, r.get("invoice_date"))
        if override == "internal":
            continue
        if override == "outsource":
            is_db_outsource = True

        # Determine tier from hierarchy
        tier = get_agent_tier(raw_name, is_db_outsource)

        # Only process outsource agents
        if tier == "Unknown":
            continue

        prop_type = classify_property_type(r)
        paid_amount = Decimal(str(r.get("paid_amount") or 0))
        paid_epp    = Decimal(str(r.get("paid_epp_total") or 0))
        # What has actually come in, less its EPP interest. This is the figure
        # the bonus is earned on, and the one the tables call Collected Payment.
        sales_price = paid_amount - paid_epp
        # What the deal is worth: the invoice's own Sales Price, as Basic & NFP
        # states it. Reported alongside, never used in the bonus maths.
        invoice_sales = Decimal(str(r.get("invoice_sales_price") or 0))

        customer_name  = str(r.get("customer_name") or "(unknown)").strip()
        invoice_number = str(r.get("invoice_number") or "").strip()
        invoice_date   = str(r.get("invoice_date") or "")[:10]

        total_invoices += 1

        if norm not in agent_sales:
            agent_sales[norm] = Decimal(0)
        agent_sales[norm] += sales_price

        if norm not in agent_invoice_sales:
            agent_invoice_sales[norm] = Decimal(0)
        agent_invoice_sales[norm] += invoice_sales

        if raw_name not in agent_details:
            agent_details[raw_name] = []
        agent_details[raw_name].append({
            "tier": tier,
            "customer_name": customer_name,
            "invoice_number": invoice_number,
            "prop_type": prop_type,
            "invoice_date": invoice_date,
            "sales_price": sales_price,
            "invoice_sales_price": invoice_sales
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
        # Reported beside the collected figure; qualification and the bonus below
        # are untouched by it — both still turn on money actually received.
        personal_invoice_sales = sum(agent_invoice_sales.get(n, Decimal(0)) for n in oum_names)

        qualified = "Yes" if (team_total >= OUM_TEAM_THRESHOLD and personal_sales >= OUM_PERSONAL_THRESHOLD) else "No"
        bonus = (team_total * OUM_BONUS_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if qualified == "Yes" else Decimal(0)

        oum_summary.append([
            team_name,
            _fmt_money(personal_sales),
            _fmt_money(personal_invoice_sales),
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
                        display_name = _agent_display_name(raw_name, d["invoice_date"]) or raw_name
                        t2_rows.append([
                            display_name,
                            d["customer_name"],
                            d["invoice_number"],
                            d["prop_type"],
                            d["invoice_date"],
                            _fmt_money(d["sales_price"]),
                            _fmt_money(d["invoice_sales_price"]),
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

    # "Collected Payment" is what these figures have always been -- payments
    # received in the window, less EPP interest -- so they are named for it
    # rather than for "Sales", which now means the invoiced Sales Price sitting
    # next to them. The OGM table keeps its original wording.
    T1_HEADERS = ["OUM Name", "Personal Collected Payment", "Personal Sales Price",
                  "Team Total Sales", "Qualified", "Production Bonus"]
    T1_OGM_HEADERS = ["OGM Name", "Team Total Sales", "OSA Sales", "OUM Sales", "Qualified", "Production Bonus"]
    T2_HEADERS = ["Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date",
                  "Collected Payment", "Sales Price", "Production Bonus"]

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
    args = parser.parse_args(argv)

    report_data = build_report(args.year)
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
