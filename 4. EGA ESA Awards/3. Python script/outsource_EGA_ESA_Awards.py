#!/usr/bin/env python3
"""
EGA / ESA Awards Report -- Outsource Agents (2026).

EP Point Rules (from 1. EGA ASA Awards.xlsx -- Outsource 2026 sheet):
  Sales Price = total_amount - EPP interest
  Points are calculated on Sales Price.

  Package              | Before May 2026        | From May 2026 (invoice_date >= 2026-05-01)
  ---------------------|------------------------|---------------------------------------------
  Residential          | 1 pt per RM1           | 1 pt per RM1
  Shop Lot / Commercial| 1 pt per RM1           | 1 pt per RM1
  Factory (< 36 pcs)   | follows Residential    | follows Residential
  Factory (>= 36 pcs)  | 1 pt per RM1           | first RM40,000 -> 100% of sales price
                       |                        | balance above RM40,000 -> 40% of balance

Eligibility -- Outsource 2026:
  EGA (half-year award):
    Early Bird -- by end of Feb  : >= 420,000 pts  -> "EGA (Feb)"
    Early Bird -- by end of Mar  : >= 480,000 pts  -> "EGA (Mar)"
    Early Bird -- by end of Apr  : >= 540,000 pts  -> "EGA (Apr)"
    Early Bird -- by end of May  : >= 600,000 pts  -> "EGA (May)"
    Standard   -- any time       :  > 720,000 pts  -> "EGA"
  ESA (end-of-year award):
    Early Bird -- by end of Oct  : >= 1,360,000 pts -> "ESA (Oct)"
    Early Bird -- by end of Nov  : >= 1,460,000 pts -> "ESA (Nov)"
    Standard   -- any time       :  > 1,560,000 pts -> "ESA"

  Eligibility is determined by CUMULATIVE EP points accumulated
  up to the end of each month (based on invoice_date).
  Each early-bird tier is an independent qualification track.

Filters:
  - EXTRACT(YEAR FROM i.invoice_date) = 2026
  - agent_type = 'Outsource' OR known outsource agent by name
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
  python outsource_EGA_ESA_Awards.py
  python outsource_EGA_ESA_Awards.py --year 2026
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
# Standard thresholds (cumulative EP, any time in the year)
EGA_THRESHOLD = Decimal("720000")    # EP Points > 720,000  -> EGA
ESA_THRESHOLD = Decimal("1560000")   # EP Points > 1,560,000 -> ESA

# Early Bird EGA -- cumulative EP by END of that calendar month qualifies
# {month_number: (threshold, label)}
EGA_EARLY_BIRD: dict[int, tuple[Decimal, str]] = {
    2: (Decimal("420000"), "EGA (Feb)"),
    3: (Decimal("480000"), "EGA (Mar)"),
    4: (Decimal("540000"), "EGA (Apr)"),
    5: (Decimal("600000"), "EGA (May)"),
}

# Early Bird ESA -- cumulative EP by END of that calendar month qualifies
ESA_EARLY_BIRD: dict[int, tuple[Decimal, str]] = {
    10: (Decimal("1360000"), "ESA (Oct)"),
    11: (Decimal("1460000"), "ESA (Nov)"),
}

FACTORY_CUTOFF_DATE  = datetime(2026, 5, 1)   # "After May 2026" starts here
FACTORY_FIRST_BLOCK  = Decimal("40000")        # first RM40,000 at 100%
FACTORY_BALANCE_RATE = Decimal("0.4")          # balance above RM40,000 at 40%
FACTORY_MIN_PANELS   = 36                       # < 36 pcs -> follows Residential


# ---------------------------------------------------------------------------
# Rules entered on the dashboard Data page (Outsource). The Outsource scheme
# uses higher EP thresholds than Internal, so the rule set is keyed on year AND
# agent type. Every value falls back to the constant above, so a year not set up
# on the Data page produces exactly the figures it always did.
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
        payload = _db.get_ega_rules(str(year), "outsource")
        if not payload.get("saved"):
            return None
    except Exception as exc:
        print(f"[EGA Rules] Warning: could not read Outsource rules from the dashboard DB "
              f"({type(exc).__name__}: {exc}); using built-in defaults.", file=sys.stderr)
        return None
    print(f"[EGA Rules] Loaded {year} (Outsource) from the dashboard Data page.")
    return payload


def _apply_ega_rules(payload) -> None:
    global EGA_THRESHOLD, ESA_THRESHOLD, FACTORY_CUTOFF_DATE
    global FACTORY_FIRST_BLOCK, FACTORY_BALANCE_RATE, FACTORY_MIN_PANELS

    rules = payload.get("rules") or {}

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
# Outsource Agent Logic
# ---------------------------------------------------------------------------
def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def get_agent_hierarchy_info(agent_name: str) -> dict[str, Any] | None:
    n = _norm_name(agent_name)
    if "stephanie chua" in n:
        return {"canonical_name": "Stephanie Chua", "tier": "osa", "manager": "Poh Ee"}
    if "poh ee" in n:
        return {"canonical_name": "Poh Ee", "tier": "oum", "manager": "Evelyn Lim"}
    if "evelyn lim" in n:
        return {"canonical_name": "Evelyn Lim", "tier": "ogm", "manager": None}
    
    if "angie" in n:
        return {"canonical_name": "Angie", "tier": "osa", "manager": "Sam Mak"}
    if "sam mak" in n:
        return {"canonical_name": "Sam Mak", "tier": "oum", "manager": "May Wong"}
    if "may wong" in n:
        return {"canonical_name": "May Wong", "tier": "ogm", "manager": None}

    if "lim cheng" in n or "lcm" in n:
        return {"canonical_name": "Lim Cheng Ming", "tier": "osa", "manager": "Liew Siew Yee"}
    if "liew siew" in n or "siew yee" in n:
        return {"canonical_name": "Liew Siew Yee", "tier": "oum", "manager": "Gillian Ang"}
    if "gillian" in n:
        return {"canonical_name": "Gillian Ang", "tier": "ogm", "manager": None}

    if "tina" in n:
        return {"canonical_name": "Tina", "tier": "osa", "manager": "Yee Wai Kit"}
    if "yee wai" in n or "waikit" in n:
        return {"canonical_name": "Yee Wai Kit", "tier": "oum", "manager": "May Wong"}

    if "wong kai" in n or "kai yuen" in n:
        return {"canonical_name": "Wong Kai Yuen", "tier": "osa", "manager": "Ang Wei Jie"}
    if "ang wei" in n or "wei jie" in n:
        return {"canonical_name": "Ang Wei Jie", "tier": "oum", "manager": "Evelyn Lim"}

    return None

def get_agent_type_override(agent_name: str, invoice_date: Any = None) -> str | None:
    """'internal' / 'outsource' from the Agent Roles & Hierarchy page for the
    invoice's own month, or None when that page says nothing. It outranks both
    Postgres' agent_type and the name map above — Postgres' agent_type is blank
    for several internal agents, and blank has always read as outsource here."""
    try:
        rates_dir = str(_REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import get_agent_type_override as _override
    except Exception:
        return None
    parsed = _parse_invoice_date(invoice_date) if invoice_date else None
    return _override(agent_name,
                     month=parsed.month if parsed else None,
                     year=parsed.year if parsed else None)


def is_outsource_agent(agent_name: str, agent_type_field: str | None,
                       invoice_date: Any = None) -> bool:
    override = get_agent_type_override(agent_name, invoice_date)
    if override:
        return override == "outsource"
    info = get_agent_hierarchy_info(agent_name)
    if info is not None:
        return True
    t = _norm_name(agent_type_field)
    return t not in {"internal", "full time"}

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
    _payload = _load_ega_rules(year)
    if _payload:
        _apply_ega_rules(_payload)
    if upto_month is None and may_only:
        upto_month = 5
    if upto_month is not None:
        month_filter = f"AND EXTRACT(MONTH FROM i.invoice_date)::int <= {int(upto_month)}"
    else:
        month_filter = ""
    # Notice we remove the agent_type = 'internal', 'full time' filter here.
    # The python logic filters strictly for outsource agents.
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

    return "Residential"

TABLE2_PACKAGES = {"Residential", "Shop Lot", "Commercial"}
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
    """
    Accumulate EP points directly by Sales Price (1 pt per RM1 of Sales Price).
    """
    return sales_price

# ---------------------------------------------------------------------------
# Eligibility -- Early Bird cumulative logic
# ---------------------------------------------------------------------------
def determine_eligibility(agent_invoice_ep: list[tuple[str, Decimal]]) -> str:
    if not agent_invoice_ep:
        return "-"

    sorted_ep = sorted(agent_invoice_ep, key=lambda x: x[0])
    monthly_cumulative: dict[int, Decimal] = {}
    running = Decimal("0")

    for inv_date_str, ep in sorted_ep:
        running += ep
        month = _month_from_date_str(inv_date_str)
        if month is not None:
            monthly_cumulative[month] = running

    total_ep = running

    def cumulative_at_end_of(month: int) -> Decimal:
        best = Decimal("0")
        for m in sorted(monthly_cumulative):
            if m <= month:
                best = monthly_cumulative[m]
        return best

    esa_result: str | None = None
    for month in sorted(ESA_EARLY_BIRD):
        threshold, label = ESA_EARLY_BIRD[month]
        if cumulative_at_end_of(month) >= threshold:
            esa_result = label
            break

    if esa_result is None and total_ep > ESA_THRESHOLD:
        esa_result = "ESA"

    if esa_result:
        return esa_result

    for month in sorted(EGA_EARLY_BIRD):
        threshold, label = EGA_EARLY_BIRD[month]
        if cumulative_at_end_of(month) >= threshold:
            return label

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
    dict[str, Decimal],
    dict[str, Decimal],
    dict[str, str],
]:
    lines: list[AwardLine] = []
    agent_ep:         dict[str, Decimal]                    = defaultdict(Decimal)
    agent_sales:      dict[str, Decimal]                    = defaultdict(Decimal)
    agent_invoice_ep: dict[str, list[tuple[str, Decimal]]]  = defaultdict(list)

    for row in rows:
        raw_agent_name = str(row.get("agent_name") or "(unknown)").strip()
        agent_type_field = str(row.get("agent_type") or "")

        if not is_outsource_agent(raw_agent_name, agent_type_field,
                                  row.get("invoice_date")):
            continue

        info = get_agent_hierarchy_info(raw_agent_name)
        agent_name = info["canonical_name"] if info else raw_agent_name

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
T3_HEADERS = T2_HEADERS


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
        description="EGA/ESA Awards report for Outsource agents."
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

    sep = "=" * 72
    print(sep)
    print(f"  EGA / ESA AWARDS REPORT  --  {args.year}  (Outsource Agents)")
    print(sep)
    print(f"  EGA Early Bird : Feb>=420k | Mar>=480k | Apr>=540k | May>=600k | Standard>720k")
    print(f"  ESA Early Bird : Oct>=1.36M | Nov>=1.46M | Standard>1.56M")
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

    if not args.no_csv:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print("Saving CSV files...")
        _write_csv(_OUTPUT_DIR / f"outsource_EGA_ESA_summary_{args.year}_{stamp}.csv",          T1_HEADERS, t1)
        _write_csv(_OUTPUT_DIR / f"outsource_EGA_ESA_residential_shoplot_{args.year}_{stamp}.csv", T2_HEADERS, t2)
        _write_csv(_OUTPUT_DIR / f"outsource_EGA_ESA_factory_{args.year}_{stamp}.csv",          T3_HEADERS, t3)
        print("Done.")

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
