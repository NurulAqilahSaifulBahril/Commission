#!/usr/bin/env python3
"""
EGA / ESA Awards Report -- Internal Full-Time Agents (2026).

EP Point Rules (from 1. EGA ASA Awards.xlsx -- Internal 2026 sheet):
  Sales Price = total_amount - EPP interest
  Points are calculated on Sales Price.

  Package              | Before May 2026        | From May 2026 (invoice_date >= 2026-05-01)
  ---------------------|------------------------|---------------------------------------------
  Residential          | 1 pt per RM1           | 1 pt per RM1
  Shop Lot / Commercial| 1 pt per RM1           | 1 pt per RM1
  Factory (< 36 pcs)   | follows Residential    | follows Residential
  Factory (>= 36 pcs)  | 1 pt per RM1           | first RM40,000 -> 100% of sales price
                       |                        | balance above RM40,000 -> 40% of balance

Eligibility -- Internal 2026:
  EGA (half-year award):
    Early Bird -- by end of Feb  : >= 350,000 pts  -> "EGA (Feb)"
    Early Bird -- by end of Mar  : >= 400,000 pts  -> "EGA (Mar)"
    Early Bird -- by end of Apr  : >= 450,000 pts  -> "EGA (Apr)"
    Early Bird -- by end of May  : >= 500,000 pts  -> "EGA (May)"
    Standard   -- any time       :  > 600,000 pts  -> "EGA"
  ESA (end-of-year award):
    Early Bird -- by end of Oct  : >= 1,000,000 pts -> "ESA (Oct)"
    Early Bird -- by end of Nov  : >= 1,200,000 pts -> "ESA (Nov)"
    Standard   -- any time       :  > 1,300,000 pts -> "ESA"

  Eligibility is determined by CUMULATIVE EP points accumulated
  up to the end of each month (based on invoice_date).
  Each early-bird tier is an independent qualification track.

Filters:
  - EXTRACT(YEAR FROM i.invoice_date) = 2026
  - agent_type IN ('internal', 'full time')
  - i.1st_payment_date IS NOT NULL (first payment secured)
  - COALESCE(is_deleted, FALSE) IS NOT TRUE
  - Deduplication by bubble_id (avoids SEDA double-join duplicates)

Tables:
  Table 1 -- Final Commission Payout Summary by Agent
             Headers: Agent Name, Total Sales, EP Point, Eligibility
  Table 2 -- Residential & Shop Lot / Commercial
             Headers: Agent Name, Customer Name, Invoice Number, Package,
                      Invoice Date, Sales Price, EP Point, Eligibility
  Table 3 -- Factory / Government / NGO / Corporate
             Headers: Agent Name, Customer Name, Invoice Number, Package,
                      Invoice Date, Sales Price, EP Point, Eligibility

Usage (from "3. Python script" folder):
  set PG_PROXY_TOKEN=<token>   (or place in .env)
  python full_internal_EGA_ESA_Awards.py
  python full_internal_EGA_ESA_Awards.py --year 2026
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT   = _SCRIPT_DIR.parents[1]          # Commission/
_OUTPUT_DIR  = _SCRIPT_DIR.parent / "2. Output"
_NFP_SCRIPT  = _REPO_ROOT / "2. NFP Commission" / "3. Python script"

# Reuse api_client from NFP Commission folder (same proxy)
if str(_NFP_SCRIPT) not in sys.path:
    sys.path.insert(0, str(_NFP_SCRIPT))

from api_client import query_sql  # noqa: E402  (after sys.path setup)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AGENT_TYPES_SQL = "'internal', 'full time'"


def _rates_module():
    """basic_commission_rates, or None. Routing must never be the thing that
    breaks a report, so callers fall back to the Postgres-only behaviour."""
    try:
        rates_dir = str(_REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        import basic_commission_rates as _bcr
        return _bcr
    except Exception:
        return None


def _agent_type_override_sql() -> str:
    """Names the Agent Roles & Hierarchy page routes explicitly, as a SQL list.
    Postgres' agent_type is blank for several internal agents, so filtering on
    it alone hands them to the outsource report. The widened filter lets
    _is_internal_row() decide per invoice, where the invoice date is known."""
    bcr = _rates_module()
    names = bcr.agent_type_override_names() if bcr else []
    if not names:
        return "''"
    return ", ".join("'" + n.replace("'", "''") + "'" for n in names)


def _is_internal_row(row: dict) -> bool:
    """Whether this invoice belongs to the internal report — Agent Roles &
    Hierarchy page first, then Postgres' agent_type."""
    pg = str(row.get("agent_type") or "").strip().lower()
    bcr = _rates_module()
    if bcr is None:
        return pg in ("internal", "full time")
    parsed = _parse_invoice_date(row.get("invoice_date"))
    return bcr.resolve_agent_type(
        row.get("agent_name"), pg,
        month=parsed.month if parsed else None,
        year=parsed.year if parsed else None,
    ) == "internal"

# Standard thresholds (cumulative EP, any time in the year)
EGA_THRESHOLD = Decimal("600000")    # EP Points > 600,000  -> EGA
ESA_THRESHOLD = Decimal("1300000")   # EP Points > 1,300,000 -> ESA

# Early Bird EGA -- cumulative EP by END of that calendar month qualifies
# {month_number: (threshold, label)}
EGA_EARLY_BIRD: dict[int, tuple[Decimal, str]] = {
    2: (Decimal("350000"), "EGA (Feb)"),
    3: (Decimal("400000"), "EGA (Mar)"),
    4: (Decimal("450000"), "EGA (Apr)"),
    5: (Decimal("500000"), "EGA (May)"),
}

# Early Bird ESA -- cumulative EP by END of that calendar month qualifies
ESA_EARLY_BIRD: dict[int, tuple[Decimal, str]] = {
    10: (Decimal("1000000"), "ESA (Oct)"),
    11: (Decimal("1200000"), "ESA (Nov)"),
}

FACTORY_CUTOFF_DATE  = datetime(2026, 5, 1)   # "After May 2026" starts here
FACTORY_FIRST_BLOCK  = Decimal("40000")        # first RM40,000 at 100%
FACTORY_BALANCE_RATE = Decimal("0.4")          # balance above RM40,000 at 40%
FACTORY_MIN_PANELS   = 36                       # < 36 pcs -> follows Residential


# ---------------------------------------------------------------------------
# Rules entered on the dashboard Data page (Internal). Every value falls back to
# the constant hardcoded above, so a year not set up there produces exactly the
# figures it always did. The Outsource script reads the same table under its own
# agent_type, where it carries its own higher thresholds.
# ---------------------------------------------------------------------------
def _load_ega_rules(year):
    import os as _os
    import sys as _sys
    dashboard_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "8. Web Dashboard",
    )
    try:
        if dashboard_dir not in _sys.path:
            _sys.path.insert(0, dashboard_dir)
        import db as _db
        payload = _db.get_ega_rules(str(year))
        if not payload.get("saved"):
            return None
    except Exception as exc:
        print(f"[EGA Rules] Warning: could not read rules from the dashboard DB "
              f"({type(exc).__name__}: {exc}); using built-in defaults.", file=sys.stderr)
        return None
    print(f"[EGA Rules] Loaded {year} from the dashboard Data page.")
    return payload


_rules_applied_for: set = set()


def ensure_rules_applied(year) -> None:
    """Push the Data page's rules for `year` into this module, once per process.

    Building the SQL used to be the only thing that did this, which was fine for
    the CLI and the export pack -- they fetch and report in one breath. The
    dashboard does not: it reports off cached invoice rows, so on a warm cache
    no fetch runs, nothing applies the rules, and the report silently grades
    against the constants hardcoded above. Every entry point that computes a
    figure calls this instead, so the source of truth is the same either way.

    Memoised because the report path runs per request and the rules are a
    database round trip. Sitting in module state means it is discarded exactly
    when it should be: clear_commission_cache() drops this module from
    sys.modules on save, so the next import starts clean.
    """
    key = str(year)
    if key in _rules_applied_for:
        return
    payload = _load_ega_rules(year)
    if payload:
        _apply_ega_rules(payload)
    # Recorded even when nothing was saved: the answer for this year is "the
    # built-in constants stand", and re-asking the database every request would
    # not change it.
    _rules_applied_for.add(key)


def _apply_early_bird_rows(months) -> None:
    """Rebuild the Early Bird ladders from the Data page's month rows.

    The page is the source of truth, so its rows replace the built-in ladders
    outright -- a month deleted there stops qualifying early. Rows are sorted
    into the two ladders by label: anything naming ESA is an ESA row, everything
    else is EGA. Rows missing a month or a threshold are skipped rather than
    taking the whole ladder down with them.
    """
    global EGA_EARLY_BIRD, ESA_EARLY_BIRD

    ega_ladder: dict[int, tuple[Decimal, str]] = {}
    esa_ladder: dict[int, tuple[Decimal, str]] = {}
    for row in months or []:
        try:
            month = int(str(row.get("month") or "").strip())
        except (TypeError, ValueError):
            continue
        if not 1 <= month <= 12:
            continue
        raw = str(row.get("ep_threshold") or "").replace(",", "").strip()
        if not raw:
            continue
        try:
            threshold = Decimal(raw)
        except Exception:
            continue
        label = (str(row.get("label") or "").strip()
                 or f"EGA ({datetime(2000, month, 1).strftime('%b')})")
        ladder = esa_ladder if "ESA" in label.upper() else ega_ladder
        ladder[month] = (threshold, label)

    # An empty table means "nothing saved" rather than "no early bird at all",
    # so the built-in ladders stand until at least one usable row arrives.
    if ega_ladder or esa_ladder:
        EGA_EARLY_BIRD = ega_ladder
        ESA_EARLY_BIRD = esa_ladder


def _apply_ega_rules(payload) -> None:
    global EGA_THRESHOLD, ESA_THRESHOLD, FACTORY_CUTOFF_DATE
    global FACTORY_FIRST_BLOCK, FACTORY_BALANCE_RATE, FACTORY_MIN_PANELS

    rules = payload.get("rules") or {}
    _apply_early_bird_rows(payload.get("months"))

    def _dec(key, current):
        raw = str(rules.get(key) or "").replace(",", "").strip()
        if not raw:
            return current
        try:
            return Decimal(raw)
        except Exception:
            return current

    EGA_THRESHOLD = _dec("ega_threshold", EGA_THRESHOLD)
    ESA_THRESHOLD = _dec("esa_threshold", ESA_THRESHOLD)
    FACTORY_FIRST_BLOCK = _dec("factory_first_block", FACTORY_FIRST_BLOCK)

    rate = str(rules.get("factory_balance_rate") or "").strip()
    if rate:
        try:
            FACTORY_BALANCE_RATE = Decimal(rate) / Decimal("100")
        except Exception:
            pass

    panels = str(rules.get("factory_min_panels") or "").strip()
    if panels.isdigit():
        FACTORY_MIN_PANELS = int(panels)

    cutoff = str(rules.get("factory_from") or "").strip()
    if cutoff:
        try:
            y, m = cutoff.split("-")
            FACTORY_CUTOFF_DATE = datetime(int(y), int(m), 1)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    for env_path in [_SCRIPT_DIR / ".env", _REPO_ROOT / ".env"]:
        if not env_path.is_file():
            continue
        proxy_keys = frozenset({"PG_PROXY_TOKEN", "PG_PROXY_URL", "PG_PROXY_DB",
                                  "PG_DB_NAME", "POSTGRES_PROXY_TOKEN"})
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name  = name.strip()
            value = value.strip().strip('"').strip("'")
            if value.lower().startswith("bearer "):
                value = value[7:].strip()
            if not name:
                continue
            if name in proxy_keys and os.environ.get(name, "").strip():
                continue
            if name not in os.environ:
                os.environ[name] = value
        break


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return default if (val is None or val.strip() == "") else val


def _get_token() -> str | None:
    return _env("PG_PROXY_TOKEN") or _env("POSTGRES_PROXY_TOKEN")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

def _invoices_sql(year: int, may_only: bool = False, upto_month: int | None = None) -> str:
    # Both the CLI and the commission pack call this first, so it is the one
    # place the year is known before any figure is computed.
    ensure_rules_applied(year)
    if upto_month is None and may_only:
        upto_month = 5
    if upto_month is not None:
        month_filter = f"AND EXTRACT(MONTH FROM i.invoice_date)::int <= {int(upto_month)}"
    else:
        month_filter = ""
    return f"""
WITH candidates AS (
  SELECT
    i.bubble_id,
    i.id            AS invoice_row_id,
    i.is_latest,
    i.invoice_number,
    i.invoice_date,
    i.full_payment_date,
    COALESCE(i.panel_qty, 0)                          AS panel_qty,
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
        END,
        0
      ),
      0
    )::numeric AS epp_interest,
    COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name, '(unknown)') AS customer_name,
    COALESCE(NULLIF(TRIM(a.name), ''), '(unknown)')   AS agent_name,
    a.agent_type,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type)      AS seda_nem_type,
    ref.project_type                                   AS referral_project_type,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC,
               i.invoice_date DESC NULLS LAST,
               i.id DESC
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
    SELECT COALESCE(SUM(ii_dedup.epp_interest_amount), 0) AS epp_interest
    FROM (
      SELECT MAX(
        CASE
          WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%'
               OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
          THEN COALESCE(ii.amount, ii.unit_price, 0)
          ELSE 0
        END
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
    AND (
      LOWER(TRIM(COALESCE(a.agent_type, ''))) IN ({AGENT_TYPES_SQL})
      OR LOWER(TRIM(COALESCE(a.name, ''))) IN ({_agent_type_override_sql()})
    )
    AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
    AND i."1st_payment_date" IS NOT NULL
)
SELECT
  bubble_id,
  invoice_number,
  invoice_date,
  full_payment_date,
  panel_qty,
  total_amount,
  epp_interest,
  customer_name,
  agent_name,
  agent_type,
  package_type,
  package_name_snapshot,
  description,
  seda_nem_type,
  referral_project_type
FROM candidates
WHERE rn = 1
ORDER BY agent_name ASC, invoice_date ASC NULLS LAST, invoice_number ASC NULLS LAST
""".strip()

# ---------------------------------------------------------------------------
# Property type classification
# ---------------------------------------------------------------------------

def classify_property_type(row: dict[str, Any]) -> str:
    # 1. SEDA nem_type
    nem = str(row.get("seda_nem_type") or "").upper().strip()
    if "RAKYAT" in nem:
        return "Residential"
    if "SHOPLOT" in nem or "SHOP-LOT" in nem or "COMMERCIAL" in nem:
        return "Shop Lot"
    if "FACTORY" in nem:
        return "Factory"
    if "GOVERNMENT" in nem or "GOV" in nem:
        return "Government"
    if "NGO" in nem:
        return "NGO"
    if "CORPORATE" in nem:
        return "Corporate"

    # 2. Referral project_type
    ref = str(row.get("referral_project_type") or "").upper().strip()
    if "RESIDENTIAL" in ref:
        return "Residential"
    if "SHOP-LOT" in ref or "SHOPLOT" in ref or "COMMERCIAL" in ref:
        return "Shop Lot"
    if "FACTORY" in ref:
        return "Factory"
    if "GOVERNMENT" in ref or "GOV" in ref:
        return "Government"
    if "NGO" in ref:
        return "NGO"
    if "CORPORATE" in ref:
        return "Corporate"

    # 3. package_type / package_name_snapshot / description
    for field in ("package_type", "package_name_snapshot", "description"):
        val = str(row.get(field) or "").upper().strip()
        if not val:
            continue
        if "FACTORY" in val:
            return "Factory"
        if "GOVERNMENT" in val or "GOV" in val:
            return "Government"
        if "NGO" in val:
            return "NGO"
        if "CORPORATE" in val:
            return "Corporate"
        if "RESIDENTIAL" in val:
            return "Residential"
        if "SHOP" in val or "COMMERCIAL" in val:
            return "Shop Lot"

    return "Residential"  # default fallback


# Table 2 packages: Residential, Shop Lot, Commercial
TABLE2_PACKAGES = {"Residential", "Shop Lot", "Commercial"}
# Table 3 packages: Factory, Government, NGO, Corporate
TABLE3_PACKAGES = {"Factory", "Government", "NGO", "Corporate"}

# ---------------------------------------------------------------------------
# EP Point calculation
# ---------------------------------------------------------------------------

def _parse_invoice_date(val: Any) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if fmt == "%Y-%m-%d %H:%M:%S" else s, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _month_from_date_str(date_str: str) -> int | None:
    """Extract month number (1-12) from a YYYY-MM-DD string."""
    if not date_str or len(date_str) < 7:
        return None
    try:
        return int(date_str[5:7])
    except ValueError:
        return None


def calc_ep_points(
    sales_price: Decimal,
    prop_type: str,
    invoice_date_val: Any,
    panel_qty: int,
) -> Decimal:
    """EP points for one invoice, per the Rate % table on the Internal 2026
    sheet of "1. EGA ASA Awards.xlsx":

        Residence              1 pt per RM1, before and after the cutoff
        Shop Lot / Commercial  1 pt per RM1, before and after the cutoff
        Factory, < 36 pcs      "price follows residence" -> 1 pt per RM1
        Factory, >= 36 pcs     before the cutoff: 1 pt per RM1
                               from the cutoff:   first RM40,000 at 100%,
                                                  the balance at 40%

    Every figure above (block size, balance rate, panel minimum, cutoff month)
    comes from the Data page via ensure_rules_applied(); only which package
    types the reduced rate applies to is fixed here.
    """
    # Anything that is not a large factory job earns a point per ringgit.
    if prop_type != "Factory" or int(panel_qty or 0) < FACTORY_MIN_PANELS:
        return sales_price

    when = _parse_invoice_date(invoice_date_val)
    if when is None:
        # No date means no way to place it either side of the cutoff. Award the
        # full rate rather than quietly docking points on a missing field.
        return sales_price
    # Compared at month granularity: the cutoff is always the 1st of a month,
    # and this sidesteps naive/aware datetime comparisons on the invoice date.
    if (when.year, when.month) < (FACTORY_CUTOFF_DATE.year, FACTORY_CUTOFF_DATE.month):
        return sales_price

    if sales_price <= FACTORY_FIRST_BLOCK:
        return sales_price
    return FACTORY_FIRST_BLOCK + (sales_price - FACTORY_FIRST_BLOCK) * FACTORY_BALANCE_RATE

# ---------------------------------------------------------------------------
# Eligibility -- Early Bird cumulative logic
# ---------------------------------------------------------------------------

def determine_eligibility(
    agent_invoice_ep: list[tuple[str, Decimal]],
) -> str:
    """
    Determines eligibility by simulating cumulative EP accumulation month by month.

    Algorithm:
      1. Sort all invoices by invoice_date (ascending).
      2. Walk through invoices, accumulating EP points.
         After processing each invoice, record the running total for that month.
      3. At the END of each month, check:
         - ESA early bird thresholds (Oct >= 1,000,000 / Nov >= 1,200,000)
         - EGA early bird thresholds (Feb >= 350,000 / Mar >= 400,000 /
                                      Apr >= 450,000 / May >= 500,000)
      4. After all invoices, check standard thresholds (ESA > 1,300,000 / EGA > 600,000).
      5. Return the EARLIEST and BEST qualifying label found.

    Priority: ESA > EGA (if somehow both qualify, ESA wins).
    Within the same award type, the earliest qualifying month wins.
    """
    if not agent_invoice_ep:
        return "-"

    # Sort by invoice date string (YYYY-MM-DD sorts correctly lexicographically)
    sorted_ep = sorted(agent_invoice_ep, key=lambda x: x[0])

    # Build month -> cumulative EP at end of that month
    # (each month stores the total EP accumulated UP TO AND INCLUDING that month)
    monthly_cumulative: dict[int, Decimal] = {}
    running = Decimal("0")

    for inv_date_str, ep in sorted_ep:
        running += ep
        month = _month_from_date_str(inv_date_str)
        if month is not None:
            monthly_cumulative[month] = running   # overwrite -> keeps last (= highest) value

    total_ep = running

    def cumulative_at_end_of(month: int) -> Decimal:
        """
        Return the cumulative EP at the end of the given month.
        If no invoices exist in that month, use the last known cumulative
        from any earlier month (carry-forward).
        """
        best = Decimal("0")
        for m in sorted(monthly_cumulative):
            if m <= month:
                best = monthly_cumulative[m]
        return best

    # ── Check ESA early bird (higher award, checked first) ──────────────────
    esa_result: str | None = None
    for month in sorted(ESA_EARLY_BIRD):
        threshold, label = ESA_EARLY_BIRD[month]
        if cumulative_at_end_of(month) >= threshold:
            esa_result = label
            break   # earliest qualifying month wins

    # ── Check ESA standard ──────────────────────────────────────────────────
    if esa_result is None and total_ep > ESA_THRESHOLD:
        esa_result = "ESA"

    if esa_result:
        return esa_result   # ESA beats EGA; return immediately

    # ── Check EGA early bird ────────────────────────────────────────────────
    for month in sorted(EGA_EARLY_BIRD):
        threshold, label = EGA_EARLY_BIRD[month]
        if cumulative_at_end_of(month) >= threshold:
            return label    # earliest qualifying month wins

    # ── Check EGA standard ──────────────────────────────────────────────────
    if total_ep > EGA_THRESHOLD:
        return "EGA"

    return "-"

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class AwardLine:
    agent_name:     str
    customer_name:  str
    invoice_number: str
    prop_type:      str
    invoice_date:   str
    sales_price:    Decimal
    ep_points:      Decimal
    panel_qty:      int
    accum_ep:       Decimal = Decimal("0")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dec(v: Decimal, decimals: int = 2) -> str:
    fmt = Decimal(f"0.{'0'*decimals}")
    return f"{v.quantize(fmt, rounding=ROUND_HALF_UP):,}"


def _fmt_money(v: Decimal) -> str:
    return _fmt_dec(v, 2)


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
# Build report
# ---------------------------------------------------------------------------

def build_report(rows: list[dict[str, Any]]) -> tuple[
    list[AwardLine],
    dict[str, Decimal],   # agent -> total EP
    dict[str, Decimal],   # agent -> total sales
    dict[str, str],       # agent -> eligibility label
]:
    lines: list[AwardLine] = []
    agent_ep:         dict[str, Decimal]                    = defaultdict(Decimal)
    agent_sales:      dict[str, Decimal]                    = defaultdict(Decimal)
    agent_invoice_ep: dict[str, list[tuple[str, Decimal]]]  = defaultdict(list)

    for row in rows:
        # The SQL filter is deliberately wide (it cannot date-match the roles
        # page), so drop anything that resolves to the outsource report here.
        if not _is_internal_row(row):
            continue

        agent_name    = str(row.get("agent_name") or "(unknown)").strip()
        customer_name = str(row.get("customer_name") or "(unknown)").strip()
        invoice_num   = str(row.get("invoice_number") or "").strip()
        prop_type     = classify_property_type(row)
        panel_qty     = int(row.get("panel_qty") or 0)

        total       = Decimal(str(row.get("total_amount") or 0))
        epp         = Decimal(str(row.get("epp_interest") or 0))
        sales_price = total - epp

        ep = calc_ep_points(
            sales_price,
            prop_type,
            row.get("invoice_date"),
            panel_qty,
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        inv_dt = str(row.get("invoice_date") or "")[:10]

        agent_ep[agent_name]    += ep
        agent_sales[agent_name] += sales_price
        agent_invoice_ep[agent_name].append((inv_dt, ep))

        lines.append(AwardLine(
            agent_name=agent_name,
            customer_name=customer_name,
            invoice_number=invoice_num,
            prop_type=prop_type,
            invoice_date=inv_dt,
            sales_price=sales_price,
            ep_points=ep,
            panel_qty=panel_qty,
        ))

    # Calculate combined running total of EP points chronologically per agent
    agent_lines = defaultdict(list)
    for ln in lines:
        agent_lines[ln.agent_name].append(ln)

    for agent, ln_list in agent_lines.items():
        ln_list.sort(key=lambda x: (x.invoice_date, x.invoice_number))
        running_sum = Decimal("0")
        for ln in ln_list:
            running_sum += ln.ep_points
            ln.accum_ep = running_sum

    # Determine eligibility per agent using cumulative early bird logic
    agent_eligibility: dict[str, str] = {
        agent: determine_eligibility(agent_invoice_ep[agent])
        for agent in agent_ep
    }

    return lines, dict(agent_ep), dict(agent_sales), agent_eligibility

# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------

T1_HEADERS = ["Agent Name", "Total Sales", "Accumulated EP Point", "Eligibility"]
T2_HEADERS = ["Agent Name", "Customer Name", "Invoice Number", "Package",
              "Invoice Date", "Sales Price", "Accumulated EP Point", "Eligibility"]
T3_HEADERS = T2_HEADERS   # identical columns


def build_table1(
    agent_ep:          dict[str, Decimal],
    agent_sales:       dict[str, Decimal],
    agent_eligibility: dict[str, str],
) -> list[list[str]]:
    rows: list[list[str]] = []
    for agent in sorted(agent_ep):
        ep    = agent_ep[agent]
        sales = agent_sales.get(agent, Decimal("0"))
        rows.append([
            agent,
            _fmt_money(sales),
            _fmt_dec(ep, 2),
            agent_eligibility.get(agent, "-"),
        ])
    return rows


def build_table2(
    lines:             list[AwardLine],
    agent_eligibility: dict[str, str],
) -> list[list[str]]:
    filtered = [ln for ln in lines if ln.prop_type in TABLE2_PACKAGES]
    filtered.sort(key=lambda x: (x.agent_name.lower(), x.invoice_date))
    return [
        [
            ln.agent_name,
            ln.customer_name,
            ln.invoice_number,
            ln.prop_type,
            ln.invoice_date,
            _fmt_money(ln.sales_price),
            _fmt_dec(ln.accum_ep, 2),
            agent_eligibility.get(ln.agent_name, "-"),
        ]
        for ln in filtered
    ]


def build_table3(
    lines:             list[AwardLine],
    agent_eligibility: dict[str, str],
) -> list[list[str]]:
    filtered = [ln for ln in lines if ln.prop_type in TABLE3_PACKAGES]
    filtered.sort(key=lambda x: (x.agent_name.lower(), x.invoice_date))
    return [
        [
            ln.agent_name,
            ln.customer_name,
            ln.invoice_number,
            ln.prop_type,
            ln.invoice_date,
            _fmt_money(ln.sales_price),
            _fmt_dec(ln.accum_ep, 2),
            agent_eligibility.get(ln.agent_name, "-"),
        ]
        for ln in filtered
    ]

# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    print(f"  Saved: {path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="EGA/ESA Awards report for internal full-time agents."
    )
    parser.add_argument("--year", type=int, default=2026,
                        help="Invoice date year (default: 2026)")
    parser.add_argument("--no-csv", action="store_true",
                        help="Skip CSV output")
    parser.add_argument("--May", "--may", action="store_true", dest="May",
                        help="Limit report to January through May")
    parser.add_argument("--month", type=int, default=None,
                        help="Limit report to January through this month")
    args = parser.parse_args(argv)

    token = _get_token()
    if not token:
        token_file = (_NFP_SCRIPT / ".." / "4. data" / "pg_proxy_token.txt").resolve()
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
    if not token:
        print(
            "ERROR: No proxy token found.\n"
            "Set PG_PROXY_TOKEN in environment or in a .env file in this folder.",
            file=sys.stderr,
        )
        return 2

    if not os.environ.get("POSTGRES_PROXY_TOKEN"):
        os.environ["POSTGRES_PROXY_TOKEN"] = token

    upto_month = args.month
    if upto_month is None and args.May:
        upto_month = 5
    print(f"Fetching invoices for year {args.year} (up to month: {upto_month if upto_month else 'all'})...")
    rows = query_sql(_invoices_sql(args.year, upto_month=upto_month))
    print(f"  {len(rows)} invoice rows fetched.\n")

    lines, agent_ep, agent_sales, agent_eligibility = build_report(rows)

    t1 = build_table1(agent_ep, agent_sales, agent_eligibility)
    t2 = build_table2(lines, agent_eligibility)
    t3 = build_table3(lines, agent_eligibility)

    # ── Console output ───────────────────────────────────────────────────────
    sep = "=" * 72
    print(sep)
    print(f"  EGA / ESA AWARDS REPORT  --  {args.year}  (Internal Agents)")
    print(sep)
    print(f"  EGA Early Bird : Feb>=350k | Mar>=400k | Apr>=450k | May>=500k | Standard>600k")
    print(f"  ESA Early Bird : Oct>=1.0M | Nov>=1.2M | Standard>1.3M")
    print(f"  Total invoices   : {len(lines)}")
    print(f"  Qualifying agents: {len(agent_ep)}")
    ega_agents = [a for a, e in agent_eligibility.items() if e.startswith("EGA")]
    esa_agents = [a for a, e in agent_eligibility.items() if e.startswith("ESA")]
    print(f"  EGA qualifiers   : {len(ega_agents)}")
    print(f"  ESA qualifiers   : {len(esa_agents)}")
    print()

    print("Table 1: Final Commission Payout Summary by Agent")
    print(_render_table(T1_HEADERS, t1))
    print()

    print("Table 2: Accumulated EGA ESA Awards by Customer (Residential and Shop Lot)")
    print(_render_table(T2_HEADERS, t2) if t2 else "  (no Residential / Shop Lot invoices)")
    print()

    print("Table 3: Accumulated EGA ESA Awards by Customer (Factory)")
    print(_render_table(T3_HEADERS, t3) if t3 else "  (no Factory / Government / NGO / Corporate invoices)")
    print()

    # ── CSV output ───────────────────────────────────────────────────────────
    if not args.no_csv:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print("Saving CSV files...")
        _write_csv(_OUTPUT_DIR / f"EGA_ESA_summary_{args.year}_{stamp}.csv",          T1_HEADERS, t1)
        _write_csv(_OUTPUT_DIR / f"EGA_ESA_residential_shoplot_{args.year}_{stamp}.csv", T2_HEADERS, t2)
        _write_csv(_OUTPUT_DIR / f"EGA_ESA_factory_{args.year}_{stamp}.csv",          T3_HEADERS, t3)
        print("Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
