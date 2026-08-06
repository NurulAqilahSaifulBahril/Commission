#!/usr/bin/env python3
"""
ANP commission report for internal full-time agents.

Data source: prod_main via Postgres SQL proxy API.

Amount per invoice: invoice total_amount (EPP not applied).

Eligibility:
    - Agent agent_type = 'internal' only. Outsource agents are not eligible for
      ANP Commission at all; fetch_agents() returns an empty list for any
      non-internal --agent-types value.
    - Invoice has 1st payment secured (1st_payment_date IS NOT NULL)
    - Invoice not soft-deleted (is_deleted IS NOT TRUE)
    - 1st_payment_date must fall in the SAME calendar month as invoice_date
    - At least RM0.01 has actually been paid on the invoice (SUM of the
      `payment` table's `amount` column for that invoice, same exclusion list
      used by Basic/NFP Commission's paid_amount)

Commission timing:
    Payout in month M includes invoices with invoice_date in month M-1
    (e.g. January invoice -> February commission), once 1st payment is secured
    in the same month as the invoice date.

ANP tier (on accumulated total amount per agent within each invoicing month):
    RM 0      : below RM 60,000
    RM 500    : RM 60,000  - 179,999.99
    RM 1,000  : RM 180,000 - 359,999.99
    RM 1,500  : RM 360,000 - 719,999.99
    RM 2,000  : RM 720,000 and above
"""

from __future__ import annotations

import argparse
import calendar
import csv
import os
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

load_dotenv()

MONEY = Decimal("0.01")
# Smallest payment that counts as "this invoice has been paid".
MIN_PAID = Decimal("0.01")

# Bad/duplicate payment rows excluded from any "amount actually paid"
# calculation — the same exclusion list Basic/NFP Commission use for their
# paid_amount column.
EXCLUDED_PAYMENT_IDS_SQL = "(101334, 104412, 101333, 104413, 4899)"


@dataclass(frozen=True)
class CommissionTier:
    min_inclusive: Decimal
    max_inclusive: Decimal | None
    commission_rm: Decimal


TIERS: tuple[CommissionTier, ...] = (
    CommissionTier(Decimal("0"), Decimal("59999.99"), Decimal("0")),
    CommissionTier(Decimal("60000"), Decimal("179999.99"), Decimal("500")),
    CommissionTier(Decimal("180000"), Decimal("359999.99"), Decimal("1000")),
    CommissionTier(Decimal("360000"), Decimal("719999.99"), Decimal("1500")),
    CommissionTier(Decimal("720000"), None, Decimal("2000")),
)


# ---------------------------------------------------------------------------
# Rules entered on the dashboard Data page. Every value falls back to the
# constant that was hardcoded here before, so a period that has not been set up
# on the Data page produces exactly the figures it always did.
# ---------------------------------------------------------------------------
def _load_anp_rules(effective_from: str | None = None):
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
        payload = _db.get_anp_rules(key)
        if not payload.get("saved"):
            return None
    except Exception as exc:
        print(f"[ANP Rules] Warning: could not read rules from the dashboard DB "
              f"({type(exc).__name__}: {exc}); using built-in defaults.", file=sys.stderr)
        return None
    print(f"[ANP Rules] Loaded {key} from the dashboard Data page.")
    return payload


def _apply_anp_rules(payload) -> None:
    """Overwrite the module-level tiers and qualifying values from a saved rule
    set. Anything blank or unparseable keeps the built-in value."""
    global TIERS, EXCLUDED_PAYMENT_IDS_SQL, MIN_PAID, ANP_SAME_MONTH_EXCEPTIONS

    rules = payload.get("rules") or {}

    # Invoice-month overrides. Blank keeps the built-in list, so an empty field
    # can never silently drop an approved exception; a non-empty field replaces
    # it wholesale, which is what editing the list on the Data page means.
    raw_overrides = str(rules.get("invoice_overrides") or "")
    parsed = {t.strip().upper() for t in raw_overrides.replace(";", ",").split(",") if t.strip()}
    if parsed:
        ANP_SAME_MONTH_EXCEPTIONS = frozenset(parsed)

    raw_min = str(rules.get("min_paid") or "").strip()
    if raw_min:
        try:
            MIN_PAID = Decimal(raw_min)
        except Exception:
            pass

    ids = [t.strip() for t in str(rules.get("excluded_payment_ids") or "").split(",") if t.strip().isdigit()]
    if ids:
        EXCLUDED_PAYMENT_IDS_SQL = "(" + ", ".join(ids) + ")"

    tiers = []
    for row in payload.get("tiers") or []:
        try:
            lo = Decimal(str(row.get("from_amount") or "0").replace(",", "").strip())
            hi_raw = str(row.get("to_amount") or "").replace(",", "").strip()
            hi = Decimal(hi_raw) if hi_raw else None
            rm = Decimal(str(row.get("commission_rm") or "0").replace(",", "").strip())
        except Exception:
            continue
        tiers.append(CommissionTier(lo, hi, rm))
    if tiers:
        TIERS = tuple(sorted(tiers, key=lambda t: t.min_inclusive))


# Applied at import: TIERS and EXCLUDED_PAYMENT_IDS_SQL are read inside the
# query and matching functions, so overriding them here reaches every caller.
_ANP_RULES = _load_anp_rules()
if _ANP_RULES:
    _apply_anp_rules(_ANP_RULES)

# ---------------------------------------------------------------------------
# Special-case invoice overrides
# Invoices listed here are included in ANP for their invoice_date month even
# if their 1st_payment_date falls in a different calendar month.
# Add new overrides as: "INV-XXXXXXX",  # Agent Name – reason / date approved
# ---------------------------------------------------------------------------
ANP_SAME_MONTH_EXCEPTIONS: frozenset[str] = frozenset({
    "INV-1009932",  # Goh Hock Lye   – May 2026 special case (1st pay in Jun)
    "INV-1009895",  # Liew Chan Sang  – May 2026 special case (1st pay in Jun)
    "INV-1010642",  # Chan Jia Wei   – July 2026 special case (1st pay in Jun)
})


def normalize_proxy_token(raw: str) -> str:
    """Accept token only, or values pasted as 'Bearer <jwt>' / quoted strings."""
    token = (raw or "").strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    return token.strip()


def validate_proxy_token(token: str) -> None:
    placeholders = {
        "",
        "your_bearer_token_here",
        "paste_your_jwt_token_here",
        "paste_token_here",
        "changeme",
    }
    if token.lower() in placeholders:
        raise ValueError(
            "PG_PROXY_TOKEN is still the placeholder. Edit .env and paste the JWT "
            "from your Postgres proxy connection packet (token only, no 'Bearer ' prefix)."
        )
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(
            "PG_PROXY_TOKEN does not look like a valid JWT (expected 3 parts separated by '.'). "
            "Paste only the token string from the proxy admin, not the full URL or SQL body."
        )


class PostgresProxyClient:
    def __init__(self, base_url: str, token: str, db_name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.db_name = db_name
        token = normalize_proxy_token(token)
        validate_proxy_token(token)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def query(self, sql: str) -> list[dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/api/sql",
            json={"db_name": self.db_name, "sql": sql, "params": []},
            timeout=120,
        )
        if not response.ok:
            raise RuntimeError(
                f"SQL proxy error {response.status_code}: {response.text}\nSQL: {sql[:500]}"
            )
        payload = response.json()
        return list(payload.get("rows") or [])


def to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
    return None


def _same_month(d1: date | None, d2: date | None) -> bool:
    """Return True if both dates are non-None and share the same year and month."""
    if d1 is None or d2 is None:
        return False
    return d1.year == d2.year and d1.month == d2.month


def _is_paid(invoice: dict[str, Any]) -> bool:
    """True once at least the configured minimum has been paid on the invoice."""
    return to_decimal(invoice.get("paid_amount")) >= MIN_PAID


def anp_commission(accumulated_total_amount: Decimal) -> Decimal:
    for tier in TIERS:
        if accumulated_total_amount < tier.min_inclusive:
            continue
        if tier.max_inclusive is None or accumulated_total_amount <= tier.max_inclusive:
            return tier.commission_rm
    return TIERS[-1].commission_rm


def anp_commission_payout_ym(inv_date: date | None) -> str:
    if inv_date is None:
        return ""
    py, pm = commission_payout_month_for_invoice_month(inv_date.year, inv_date.month)
    return f"{py:04d}-{pm:02d}"


def count_distinct_agents_in_year(
    invoices: list[dict[str, Any]],
    allowed_agent_ids: set[str],
    year: int,
) -> int:
    seen: set[str] = set()
    for inv in invoices:
        if is_deleted_type(inv):
            continue
        inv_date = parse_date(inv.get("invoice_date"))
        if inv_date is None or inv_date.year != year:
            continue
        aid = str(inv.get("linked_agent") or "")
        if aid in allowed_agent_ids:
            seen.add(aid)
    return len(seen)


def invoice_period_bounds(payout_year: int, payout_month: int) -> tuple[date, date]:
    """Invoices counted when invoice_date falls in the month before payout month."""
    if payout_month == 1:
        inv_year, inv_month = payout_year - 1, 12
    else:
        inv_year, inv_month = payout_year, payout_month - 1
    start = date(inv_year, inv_month, 1)
    last_day = monthrange(inv_year, inv_month)[1]
    end = date(inv_year, inv_month, last_day)
    return start, end


def invoice_calendar_month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of a calendar invoicing month (invoice_date falls in this range)."""
    start = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def commission_payout_month_for_invoice_month(inv_year: int, inv_month: int) -> tuple[int, int]:
    """Commission is paid in the month after the invoicing calendar month."""
    if inv_month == 12:
        return inv_year + 1, 1
    return inv_year, inv_month + 1


def anp_month_receive(inv_date: date | None) -> str:
    """Human-readable month the agent receives ANP for this invoice."""
    if inv_date is None:
        return ""
    py, pm = commission_payout_month_for_invoice_month(inv_date.year, inv_date.month)
    return f"{calendar.month_name[pm]} {py}"


def payout_label_calendar_invoice_month(inv_year: int, inv_month: int) -> str:
    py, pm = commission_payout_month_for_invoice_month(inv_year, inv_month)
    return f"inv-{inv_year:04d}-{inv_month:02d}_pay-{py:04d}-{pm:02d}"


def _agent_type_override(agent_name: str):
    """'internal' / 'outsource' as set on the Agent Roles & Hierarchy page, or
    None when that page says nothing about this agent."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        rates_dir = str(repo_root / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import get_agent_type_override as _override
    except Exception:
        return None
    return _override(agent_name)


def _agent_display_name(agent_name: str):
    """Full Name from the Agent Roles & Hierarchy page, falling back to that
    page's own Agent Name (from eeAdmin) field, or None when that page says
    nothing about this agent — callers keep the Postgres name in that case."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        rates_dir = str(repo_root / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import get_agent_display_name as _display
    except Exception:
        return None
    return _display(agent_name)


def sql_in_list(values: Iterable[str]) -> str:
    escaped = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return escaped


def fetch_agents(client: PostgresProxyClient, agent_types: list[str]) -> list[dict[str, Any]]:
    # ANP Commission applies to internal agents only — outsource agents are
    # not eligible, so any non-internal request returns no agents at all
    # (rather than the outsource roster), which in turn makes every
    # downstream ANP table/report empty for outsource.
    if not any(t.lower() in ("internal", "full time") for t in agent_types):
        return []
    # Agents live in BOTH the agent table and the user table since the
    # 2026-07-20 "agent retirement" migration (same bubble_id kept).
    all_agents = client.query(
        """
        SELECT DISTINCT ON (au.bubble_id)
               au.bubble_id, au.name, au.agent_type, au.unique_id, au.linked_user_login
        FROM (
          SELECT u.bubble_id, u.name, u.agent_type, u.unique_id,
                 NULL::text AS linked_user_login, 1 AS pri
            FROM "user" u
           WHERE u.bubble_id IS NOT NULL AND COALESCE(BTRIM(u.agent_type), '') <> ''
          UNION ALL
          SELECT ag.bubble_id, ag.name, ag.agent_type, ag.unique_id,
                 ag.linked_user_login, 2
            FROM agent ag
           WHERE ag.bubble_id IS NOT NULL
          UNION ALL
          SELECT u2.bubble_id, u2.name, u2.agent_type, u2.unique_id,
                 NULL::text, 3
            FROM "user" u2
           WHERE u2.bubble_id IS NOT NULL
        ) au
        ORDER BY au.bubble_id, au.pri
        """
    )
    all_agents = sorted(all_agents, key=lambda a: str(a.get("name") or ""))
    filtered = []
    for a in all_agents:
        name = str(a.get("name") or "").strip()
        name_lower = name.lower()
        row_type = str(a.get("agent_type") or "").strip().lower()
        if row_type in ("block", "test"):
            continue
        # The Agent Roles & Hierarchy page wins over Postgres' agent_type,
        # which is blank for several internal agents — and blank reads as
        # outsource, which put them in the wrong report entirely. No invoice
        # date to date-match against here (this picks the agent list for a
        # whole-year period), so the agent's latest entry decides.
        override = _agent_type_override(name)
        if override:
            is_internal = override == "internal" and "gan lai soon" not in name_lower
        else:
            is_internal = row_type in ("internal", "full time") and "gan lai soon" not in name_lower
        if is_internal:
            filtered.append(a)
    return filtered


def fetch_invoices_for_agents(
    client: PostgresProxyClient, agent_bubble_ids: list[str]
) -> list[dict[str, Any]]:
    if not agent_bubble_ids:
        return []
    ids_sql = sql_in_list(agent_bubble_ids)
    return client.query(
        f"""
        SELECT
          i.bubble_id,
          i.linked_agent,
          i.invoice_number,
          i.invoice_date,
          i.amount,
          i.total_amount,
          i.effective_epp,
          i.customer_name_snapshot,
          i.linked_customer,
          i."1st_payment_date",
          i.is_deleted,
          i.type,
          i.status,
          i.approval_status,
          COALESCE(i.panel_qty, 0) AS panel_qty,
          i.package_type,
          i.package_name_snapshot,
          i.description,
          COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
          ref.project_type AS referral_project_type,
          COALESCE((SELECT SUM(p.amount) FROM payment p
                     WHERE p.linked_invoice = i.bubble_id
                       AND p.id NOT IN {EXCLUDED_PAYMENT_IDS_SQL}), 0)::numeric AS paid_amount
        FROM invoice i
        LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
        LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
        LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
        WHERE i.linked_agent IN ({ids_sql})
          AND i."1st_payment_date" IS NOT NULL
        ORDER BY i.linked_agent, i.invoice_date, i.invoice_number
        """
    )


def fetch_payment_planning(
    client: PostgresProxyClient, invoice_bubble_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not invoice_bubble_ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk_size = 200
    for i in range(0, len(invoice_bubble_ids), chunk_size):
        chunk = invoice_bubble_ids[i : i + chunk_size]
        ids_sql = sql_in_list(chunk)
        rows = client.query(
            f"""
            SELECT linked_invoice, payment_1_charges
            FROM invoice_payment_planning
            WHERE linked_invoice IN ({ids_sql})
            """
        )
        for row in rows:
            key = row.get("linked_invoice")
            if key:
                out[str(key)] = row
    return out


def classify_property_type(row: dict[str, Any]) -> str:
    # 1. Check customer name for explicit company or property type hints
    cust_name = ""
    for k in ("customer_name", "customer_name_snapshot", "db_customer_name"):
        if row.get(k):
            cust_name += " " + str(row.get(k))
    cust_name = cust_name.upper().strip()
    if cust_name:
        comm_keywords = (
            "SDN BHD", "SDN. BHD.", "BHD", "PRIVATE LIMITED", "LIMITED", "LTD",
            "ENTERPRISE", "COMMERCIAL", "SHOP", "TRADING", "INDUSTRIES", "INDUSTRY",
            "ENGINEERING", "CONSTRUCTION", "SERVICES", "SERVICE", "MARKET", "MART",
            "BUSINESS", "CORP", "CORPORATION"
        )
        if any(kw in cust_name for kw in comm_keywords):
            if "FACTORY" in cust_name or "EDGING" in cust_name:
                return "Factory"
            return "Shop Lot"
        if "FACTORY" in cust_name or "EDGING" in cust_name:
            return "Factory"

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

    for field in ("package_type", "package_name_snapshot", "description"):
        val = str(row.get(field) or "").upper().strip()
        if not val:
            continue
        if "FACTORY" in val: return "Factory"
        if "GOVERNMENT" in val or "GOV" in val: return "Government"
        if "NGO" in val: return "NGO"
        if "CORPORATE" in val: return "Corporate"
        if "RESIDENTIAL" in val: return "Residential"
        if "SHOP" in val or "COMMERCIAL" in val: return "Shop Lot"

    return "Residential"



def fetch_customers(
    client: PostgresProxyClient, customer_ids: list[str]
) -> dict[str, str]:
    if not customer_ids:
        return {}
    out: dict[str, str] = {}
    chunk_size = 200
    for i in range(0, len(customer_ids), chunk_size):
        chunk = customer_ids[i : i + chunk_size]
        ids_sql = sql_in_list(chunk)
        rows = client.query(
            f"""
            SELECT customer_id, name
            FROM customer
            WHERE customer_id IN ({ids_sql})
            """
        )
        for row in rows:
            name = row.get("name") or ""
            if row.get("customer_id"):
                out[str(row["customer_id"])] = name
    return out


def resolve_customer_name(
    invoice: dict[str, Any], customers: dict[str, str]
) -> str:
    snapshot = (invoice.get("customer_name_snapshot") or "").strip()
    if snapshot:
        return snapshot
    linked = invoice.get("linked_customer")
    if linked:
        return customers.get(str(linked), str(linked))
    return ""


def invoice_in_period(
    invoice: dict[str, Any], period_start: date | None, period_end: date | None
) -> bool:
    if period_start is None or period_end is None:
        return True
    inv_date = parse_date(invoice.get("invoice_date"))
    if inv_date is None:
        return False
    return period_start <= inv_date <= period_end


def is_cancelled_or_rejected(invoice: dict[str, Any]) -> bool:
    if invoice.get("is_deleted") is True:
        return True
    if str(invoice.get("status")).lower() == "deleted":
        return True
    if invoice.get("approval_status") in ("Rejected", "Deleted"):
        return True
    inv_type = (invoice.get("type") or "").upper()
    if "DELETED" in inv_type:
        return True
    return False


def is_deleted_type(invoice: dict[str, Any]) -> bool:
    return is_cancelled_or_rejected(invoice)


def count_distinct_agents_in_year(
    invoices: list[dict[str, Any]],
    allowed_agent_ids: set[str],
    year: int,
) -> int:
    seen: set[str] = set()
    for inv in invoices:
        if is_deleted_type(inv):
            continue
        inv_date = parse_date(inv.get("invoice_date"))
        if inv_date is None or inv_date.year != year:
            continue
        aid = str(inv.get("linked_agent") or "")
        if aid in allowed_agent_ids:
            seen.add(aid)
    return len(seen)


def table2_sort_key(row: dict[str, Any]) -> tuple:
    inv_date = row.get("invoice_date")
    if not isinstance(inv_date, date):
        inv_date = parse_date(inv_date) or date.min
    return (
        row.get("agent_name", ""),
        inv_date,
        str(row.get("invoice_number", "")),
    )


def table1_sort_key(row: dict[str, Any]) -> tuple:
    return (
        row.get("agent_name", ""),
        row.get("invoice_year", 0),
        row.get("invoice_month", 0),
    )


def finalize_table2_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


def build_anp_tables(
    agents: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    customers: dict[str, str],
    period_start: date | None,
    period_end: date | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Build Table 1 (per agent per invoicing month) and Table 2 (per invoice).

    Tier uses accumulated total_amount within each (agent, year, month) group.
    ANP Commission Date = month after invoice_date (Jan invoice -> Feb payout).
    """
    agent_by_id = {str(a["bubble_id"]): a for a in agents}
    table1_rows: list[dict[str, Any]] = []
    table2_rows: list[dict[str, Any]] = []

    # (agent_id, inv_year, inv_month) -> list of invoice dicts
    buckets: dict[tuple[str, int, int], list[dict[str, Any]]] = {}

    for inv in invoices:
        if not invoice_in_period(inv, period_start, period_end):
            continue
        inv_date = parse_date(inv.get("invoice_date"))
        if inv_date is None:
            continue
        # Require 1st payment date in the same month as invoice date,
        # unless this invoice is a whitelisted special case.
        inv_num = str(inv.get("invoice_number") or "").strip()
        if inv_num not in ANP_SAME_MONTH_EXCEPTIONS:
            first_pay_date = parse_date(inv.get("1st_payment_date"))
            if not _same_month(inv_date, first_pay_date):
                continue
        # A 1st_payment_date can be set without a real payment behind it
        # (data entry gap) — require an actual amount paid, however small.
        if not _is_paid(inv):
            continue
        agent_id = str(inv.get("linked_agent") or "")
        if agent_id not in agent_by_id:
            continue
        key = (agent_id, inv_date.year, inv_date.month)
        buckets.setdefault(key, []).append(inv)

    for agent_id, inv_year, inv_month in sorted(
        buckets.keys(), key=lambda k: (k[0], k[1], k[2])
    ):
        inv_list = buckets[(agent_id, inv_year, inv_month)]
        agent = agent_by_id[agent_id]
        # This report's own output (CSV/Excel/console) is self-contained — no
        # other script joins against these dicts by name — so it's safe to
        # show the Agent Roles "Full Name" here directly.
        agent_name = _agent_display_name(agent.get("name")) or (agent.get("name") or "").strip()

        sorted_invs = sorted(
            inv_list,
            key=lambda x: (
                parse_date(x.get("invoice_date")) or date.min,
                str(x.get("invoice_number") or ""),
            ),
        )

        active_invs = [inv for inv in sorted_invs if not is_cancelled_or_rejected(inv)]
        cancelled_invs = [inv for inv in sorted_invs if is_cancelled_or_rejected(inv)]

        original_total = sum(to_decimal(inv.get("total_amount") or inv.get("amount")) for inv in sorted_invs)
        active_total = sum(to_decimal(inv.get("total_amount") or inv.get("amount")) for inv in active_invs)

        original_commission = anp_commission(original_total)
        active_commission = anp_commission(active_total)
        total_clawback = original_commission - active_commission

        clawback_per_inv = Decimal("0")
        if cancelled_invs and total_clawback > 0:
            clawback_per_inv = (total_clawback / len(cancelled_invs)).quantize(MONEY)

        pay_y, pay_m = commission_payout_month_for_invoice_month(inv_year, inv_month)
        payout_ym = f"{pay_y:04d}-{pay_m:02d}"

        table1_rows.append(
            {
                "agent_name": agent_name,
                "invoice_year": inv_year,
                "invoice_month": inv_month,
                "accumulated_total_amount": active_total,
                "anp_commission": active_commission,
                "clawback": total_clawback,
                "anp_commission_date": payout_ym,
            }
        )

        running_active_amount = Decimal("0")
        for inv in sorted_invs:
            inv_date = parse_date(inv.get("invoice_date"))
            total = to_decimal(inv.get("total_amount") or inv.get("amount"))
            payout_ym = anp_commission_payout_ym(inv_date) if inv_date else ""

            cancelled = is_cancelled_or_rejected(inv)
            if not cancelled:
                running_active_amount += total
                line_clawback = Decimal("0")
                line_accumulated = running_active_amount
                line_comm = anp_commission(running_active_amount)
            else:
                line_clawback = clawback_per_inv
                line_accumulated = Decimal("0")
                line_comm = Decimal("0")

            table2_rows.append(
                {
                    "agent_name": agent_name,
                    "customer_name": resolve_customer_name(inv, customers),
                    "invoice_number": inv.get("invoice_number") or inv.get("bubble_id"),
                    "invoice_date": inv_date,
                    "total_amount": total,
                    "accumulated_total_amount": line_accumulated,
                    "anp_commission": line_comm,
                    "clawback": line_clawback,
                    "anp_commission_date": payout_ym,
                }
            )

    table2_rows = finalize_table2_rows(table2_rows)
    return table1_rows, table2_rows


def build_report_rows(
    agents: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    planning: dict[str, dict[str, Any]],
    customers: dict[str, str],
    period_start: date | None,
    period_end: date | None,
    payout_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    agent_by_id = {str(a["bubble_id"]): a for a in agents}
    detail_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    buckets: dict[tuple[str, int, int], list[dict[str, Any]]] = {}

    for inv in invoices:
        if not invoice_in_period(inv, period_start, period_end):
            continue
        inv_date = parse_date(inv.get("invoice_date"))
        if inv_date is None:
            continue
        # Require 1st payment date in the same month as invoice date,
        # unless this invoice is a whitelisted special case.
        inv_num = str(inv.get("invoice_number") or "").strip()
        if inv_num not in ANP_SAME_MONTH_EXCEPTIONS:
            first_pay_date = parse_date(inv.get("1st_payment_date"))
            if not _same_month(inv_date, first_pay_date):
                continue
        # A 1st_payment_date can be set without a real payment behind it
        # (data entry gap) — require an actual amount paid, however small.
        if not _is_paid(inv):
            continue
        agent_id = str(inv.get("linked_agent") or "")
        if agent_id not in agent_by_id:
            continue
        key = (agent_id, inv_date.year, inv_date.month)
        buckets.setdefault(key, []).append(inv)

    for (agent_id, inv_year, inv_month), inv_list in sorted(
        buckets.items(), key=lambda k: (k[0][0], k[0][1], k[0][2])
    ):
        agent = agent_by_id[agent_id]
        # Kept as the raw Postgres/eeAdmin name here (not the Agent Roles
        # "Full Name" override) — this is the key build_commission_pack.py's
        # PDF pack uses to join ANP rows back to the matching Basic Commission
        # agent name. The Full Name display swap happens downstream, on the
        # already-joined output only (build_anp_tables() / dashboard rows).
        agent_name = (agent.get("name") or "").strip()

        sorted_invs = sorted(
            inv_list,
            key=lambda x: (
                parse_date(x.get("invoice_date")) or date.min,
                str(x.get("invoice_number") or ""),
            ),
        )

        active_invs = [inv for inv in sorted_invs if not is_cancelled_or_rejected(inv)]
        cancelled_invs = [inv for inv in sorted_invs if is_cancelled_or_rejected(inv)]

        original_total = sum(to_decimal(inv.get("total_amount") or inv.get("amount")) for inv in sorted_invs)
        active_total = sum(to_decimal(inv.get("total_amount") or inv.get("amount")) for inv in active_invs)

        original_commission = anp_commission(original_total)
        active_commission = anp_commission(active_total)
        total_clawback = original_commission - active_commission

        clawback_per_inv = Decimal("0")
        if cancelled_invs and total_clawback > 0:
            clawback_per_inv = (total_clawback / len(cancelled_invs)).quantize(MONEY)

        running_active_amount = Decimal("0")
        for inv in sorted_invs:
            inv_date = parse_date(inv.get("invoice_date"))
            total = to_decimal(inv.get("total_amount") or inv.get("amount"))
            prop_type = classify_property_type(inv)

            cancelled = is_cancelled_or_rejected(inv)
            if not cancelled:
                running_active_amount += total
                line_clawback = Decimal("0")
                line_accumulated = running_active_amount
                line_comm = anp_commission(running_active_amount)
            else:
                line_clawback = clawback_per_inv
                line_accumulated = Decimal("0")
                line_comm = Decimal("0")

            detail_rows.append(
                {
                    "payout_period": payout_label,
                    "agent_name": agent_name,
                    "agent_bubble_id": agent_id,
                    "agent_type": agent.get("agent_type"),
                    "customer_name": resolve_customer_name(inv, customers),
                    "invoice_number": inv.get("invoice_number") or inv.get("bubble_id"),
                    "prop_type": prop_type,
                    "invoice_date": inv_date,
                    "first_payment_date": parse_date(inv.get("1st_payment_date")),
                    "invoice_total_amount": total,
                    "ep_points": Decimal("0") if cancelled else total,
                    "accumulated_ep_points": line_accumulated,
                    "anp_commission_accumulated_tier": line_comm,
                    "clawback": line_clawback,
                    "anp_commission_date": anp_commission_payout_ym(inv_date),
                    "anp_commission_date_remark": anp_month_receive(inv_date),
                }
            )

        summary_rows.append(
            {
                "payout_period": payout_label,
                "agent_name": agent_name,
                "agent_bubble_id": agent_id,
                "agent_type": agent.get("agent_type"),
                "invoice_count": len(active_invs),
                "accumulated_ep_points": active_total,
                "anp_commission": active_commission,
                "clawback": total_clawback,
            }
        )

    return detail_rows, summary_rows


def summary_table_rows(summary_rows: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for r in summary_rows:
        rows.append(
            [
                r.get("agent_name", ""),
                str(r.get("invoice_count", 0)),
                _fmt_money(r.get("accumulated_ep_points")),
                _fmt_money(r.get("anp_commission")),
            ]
        )
    return rows



def _fmt_date(val: Any) -> str:
    if isinstance(val, date):
        return val.isoformat()
    return str(val) if val else ""


def _fmt_money(val: Any) -> str:
    if val is None or val == "":
        return "0.00"
    if isinstance(val, Decimal):
        return f"{val:,.2f}"
    return f"{Decimal(str(val)):,.2f}"


TABLE1_HEADERS = [
    "Agent Name",
    "Accumulated Total Amount (RM)",
    "ANP Commission (RM)",
    "Clawback (RM)",
    "ANP Commission Date",
]

TABLE2_HEADERS = [
    "Agent Name",
    "Customer Name",
    "Invoice #",
    "Invoice Date",
    "Total Amount (RM)",
    "Accumulated Total Amount (RM)",
    "ANP Commission (RM)",
    "Clawback (RM)",
    "ANP Commission Date",
]


def table1_display_rows(rows: list[dict[str, Any]]) -> list[list[str]]:
    out = []
    for r in sorted(rows, key=table1_sort_key):
        out.append(
            [
                r.get("agent_name", ""),
                _fmt_money(r.get("accumulated_total_amount")),
                _fmt_money(r.get("anp_commission")),
                _fmt_money(r.get("clawback", Decimal("0"))),
                r.get("anp_commission_date", ""),
            ]
        )
    return out


def table2_display_rows(rows: list[dict[str, Any]]) -> list[list[str]]:
    out = []
    for r in sorted(rows, key=table2_sort_key):
        out.append(
            [
                r.get("agent_name", ""),
                r.get("customer_name", ""),
                str(r.get("invoice_number", "")),
                _fmt_date(r.get("invoice_date")),
                _fmt_money(r.get("total_amount")),
                _fmt_money(r.get("accumulated_total_amount")),
                _fmt_money(r.get("anp_commission")),
                _fmt_money(r.get("clawback", Decimal("0"))),
                r.get("anp_commission_date", ""),
            ]
        )
    return out


def print_report_tables(
    meta: dict[str, Any],
    table1_rows: list[dict[str, Any]],
    table2_rows: list[dict[str, Any]],
    max_detail_console_rows: int | None,
) -> None:
    from tabulate import tabulate

    table_fmt = "simple"

    print()
    print("=" * 100)
    print("ANP COMMISSION REPORT")
    print("=" * 100)
    overview = [
        ["Filter", meta.get("filter_description", "")],
        ["Invoice date from", meta.get("invoice_date_from", "")],
        ["Invoice date to", meta.get("invoice_date_to", "")],
        ["Agent types", meta.get("agent_types", "")],
        ["Total qualifying agents (distinct)", meta.get("total_qualifying_agents", 0)],
        ["Table 1 rows (agent-month)", len(table1_rows)],
        ["Table 2 rows (invoices)", len(table2_rows)],
    ]
    print(tabulate(overview, headers=["Field", "Value"], tablefmt=table_fmt, disable_numparse=True))
    print()

    print("TABLE 1 - Accumulated ANP Commission")
    print("-" * 100)
    t1 = table1_display_rows(table1_rows)
    if t1:
        print(tabulate(t1, headers=TABLE1_HEADERS, tablefmt=table_fmt, disable_numparse=True))
        total_anp = sum(Decimal(str(r.get("anp_commission", 0))) for r in table1_rows)
        print(
            tabulate(
                [["TOTAL", "-", _fmt_money(total_anp), "-"]],
                headers=TABLE1_HEADERS,
                tablefmt=table_fmt,
                disable_numparse=True,
            )
        )
    else:
        print("(no qualifying data for Table 1)")
    print()

    print("TABLE 2 - ANP commission by customer")
    print("-" * 100)
    t2 = table2_display_rows(table2_rows)
    if t2:
        limit = max_detail_console_rows
        if limit is not None and len(t2) > limit:
            print(
                f"(Showing first {limit} of {len(t2)} invoice rows in console. "
                "Full output is in the Excel/CSV files.)"
            )
            t2 = t2[:limit]
        print(tabulate(t2, headers=TABLE2_HEADERS, tablefmt=table_fmt, disable_numparse=True))
    else:
        print("(no qualifying data for Table 2)")
    print()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fieldnames:
                val = row.get(key)
                if isinstance(val, (date, datetime)):
                    out[key] = val.isoformat()
                elif isinstance(val, Decimal):
                    out[key] = f"{val:.2f}"
                else:
                    out[key] = val
            writer.writerow(out)


def _style_sheet_header(
    ws,
    headers: list[str],
    money_cols: set[int] | None = None,
    date_cols: set[int] | None = None,
) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    money_cols = money_cols or set()
    date_cols = date_cols or set()
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=len(headers)):
        for col_idx, cell in enumerate(row, start=1):
            cell.border = border
            if col_idx in money_cols and isinstance(cell.value, (int, float)):
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            elif col_idx in date_cols and cell.value:
                cell.alignment = Alignment(horizontal="center")

    for col_idx in range(1, len(headers) + 1):
        letter = get_column_letter(col_idx)
        max_len = len(str(headers[col_idx - 1]))
        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, min(len(str(val)), 50))
        ws.column_dimensions[letter].width = max_len + 2

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


def write_excel(
    path: Path,
    table1_rows: list[dict[str, Any]],
    table2_rows: list[dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "Overview"
    ws_meta["A1"] = "ANP Commission Report"
    ws_meta["A1"].font = Font(bold=True, size=14)
    row = 3
    for key, value in meta.items():
        ws_meta.cell(row=row, column=1, value=key)
        ws_meta.cell(row=row, column=2, value=value)
        row += 1
    ws_meta.column_dimensions["A"].width = 28
    ws_meta.column_dimensions["B"].width = 40

    # Table 1 — Accumulated ANP Commission
    ws1 = wb.create_sheet("Table1_Accumulated_ANP")
    ws1.append(TABLE1_HEADERS)
    for row in table1_display_rows(table1_rows):
        ws1.append(row)
    _style_sheet_header(ws1, TABLE1_HEADERS, money_cols={2, 3, 4}, date_cols={5})

    # Table 2 — ANP commission by customer
    ws2 = wb.create_sheet("Table2_ANP_by_Customer")
    ws2.append(TABLE2_HEADERS)
    for row in table2_display_rows(table2_rows):
        ws2.append(row)
    _style_sheet_header(ws2, TABLE2_HEADERS, money_cols={5, 6, 7, 8}, date_cols={4})

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


TABLE1_CSV_FIELDS = [
    "agent_name",
    "accumulated_total_amount",
    "anp_commission",
    "clawback",
    "anp_commission_date",
]
TABLE2_CSV_FIELDS = [
    "agent_name",
    "customer_name",
    "invoice_number",
    "invoice_date",
    "total_amount",
    "accumulated_total_amount",
    "anp_commission",
    "clawback",
    "anp_commission_date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ANP commission report.")
    parser.add_argument(
        "--year",
        type=int,
        default=2026,
        help="Invoicing year (default: 2026). Use with --month to filter a single month.",
    )
    parser.add_argument(
        "--month",
        type=int,
        default=None,
        help="Invoicing month (1-12) to filter.",
    )
    parser.add_argument(
        "--payout-month",
        help="Commission payout month (YYYY-MM). Invoices from prior calendar month are included.",
    )
    parser.add_argument(
        "--all-time",
        action="store_true",
        help="Include all qualifying invoices (no invoice_date month filter).",
    )
    parser.add_argument(
        "--agent-types",
        default="internal,FULL TIME",
        help='Comma-separated agent_type values (default: "internal,FULL TIME").',
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for CSV/XLSX output (default: output)",
    )
    parser.add_argument(
        "--no-excel",
        action="store_true",
        help="Skip Excel workbook generation.",
    )
    parser.add_argument(
        "--full-console",
        action="store_true",
        help="Print every invoice row in the console (default: first 40 rows only).",
    )
    parser.add_argument(
        "--year-invoice-months",
        type=int,
        metavar="YYYY",
        help=(
            "Generate one report per calendar invoicing month in that year that has "
            "qualifying data (e.g. 2026 outputs each calendar month Jan-Dec where data exists). "
            "Skips months with no matching invoices."
        ),
    )
    parser.add_argument(
        "--print-each-month",
        action="store_true",
        help="With --year-invoice-months: print full console tables for every non-empty month.",
    )
    parser.add_argument(
        "--console-detail-limit",
        type=int,
        default=40,
        metavar="N",
        help="Max invoice rows in console table (default: 40). Use with --full-console for all rows.",
    )
    return parser.parse_args()


def print_year_month_overview(rows: list[list[Any]]) -> None:
    from tabulate import tabulate

    headers = [
        "Invoice month",
        "Commission payout",
        "Agents",
        "Invoices",
        "Total ANP (RM)",
    ]
    print(tabulate(rows, headers=headers, tablefmt="simple", disable_numparse=True))


def main() -> int:
    args = parse_args()

    base_url = os.getenv("PG_PROXY_URL", "").strip()
    token = normalize_proxy_token(os.getenv("PG_PROXY_TOKEN", ""))
    db_name = os.getenv("PG_DB_NAME", "prod_main").strip()

    if not base_url or not token:
        print(
            "Set PG_PROXY_URL and PG_PROXY_TOKEN in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1

    try:
        validate_proxy_token(token)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            f"Edit this file: {Path('.env').resolve()}",
            file=sys.stderr,
        )
        return 1

    mode_flags = sum(
        [
            bool(args.all_time),
            bool(args.payout_month),
            args.year_invoice_months is not None,
            args.month is not None,
        ]
    )
    default_invoice_year_mode = mode_flags == 0
    if mode_flags == 0:
        # Default behavior requested by user: all invoices in 2026 (single report).
        args.year_invoice_months = None
    if mode_flags > 1:
        print(
            "Use only one period mode: --all-time, --payout-month YYYY-MM, --year-invoice-months YYYY, or --month M",
            file=sys.stderr,
        )
        return 1

    agent_types = [t.strip() for t in args.agent_types.split(",") if t.strip()]
    if not agent_types:
        print("At least one --agent-types value is required.", file=sys.stderr)
        return 1
    if not any(t.lower() in ("internal", "full time") for t in agent_types):
        print(
            "ANP Commission is internal agents only — outsource agents are not "
            "eligible. Omit --agent-types (or set it to internal/FULL TIME) to "
            "generate a report; there is nothing to generate for other agent types.",
            file=sys.stderr,
        )
        return 1

    client = PostgresProxyClient(base_url, token, db_name)

    print("Fetching agents...")
    agents = fetch_agents(client, agent_types)
    agent_ids = [str(a["bubble_id"]) for a in agents]
    print(f"  Agents with types {agent_types}: {len(agents)}")

    print("Fetching invoices (1st payment secured)...")
    invoices = fetch_invoices_for_agents(client, agent_ids)
    print(f"  Raw invoices: {len(invoices)}")

    customer_ids = list(
        {str(i["linked_customer"]) for i in invoices if i.get("linked_customer")}
    )

    print("Fetching customer names...")
    customers = fetch_customers(client, customer_ids)

    out_dir = Path(args.output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    agent_types_str = ", ".join(agent_types)

    if args.year_invoice_months:
        year = args.year_invoice_months
        overview_rows: list[list[Any]] = []

        months = list(range(1, 13))
        for inv_month in months:
            ps, pe = invoice_calendar_month_bounds(year, inv_month)
            pay_y, pay_m = commission_payout_month_for_invoice_month(year, inv_month)
            label = payout_label_calendar_invoice_month(year, inv_month)
            calendar_label = f"{year:04d}-{inv_month:02d}"
            pay_label = f"{pay_y:04d}-{pay_m:02d}"

            table1_rows, table2_rows = build_anp_tables(
                agents, invoices, customers, ps, pe
            )
            if not table2_rows:
                continue

            total_anp = sum(Decimal(str(r.get("anp_commission", 0))) for r in table1_rows)
            overview_rows.append(
                [
                    calendar_label,
                    pay_label,
                    len({r["agent_name"] for r in table1_rows}),
                    len(table2_rows),
                    _fmt_money(total_anp),
                ]
            )

            prefix = f"anp_commission_{calendar_label}_{stamp}"
            meta = {
                "payout_period": label,
                "invoice_calendar_month": calendar_label,
                "commission_payout_month": pay_label,
                "invoice_date_from": ps.isoformat(),
                "invoice_date_to": pe.isoformat(),
                "agent_types": agent_types_str,
                "total_qualifying_agents": len({r["agent_name"] for r in table1_rows}),
                "total_qualifying_invoices": len(table2_rows),
                "filter_description": (
                    "2026 invoices, 1st payment secured, agent types: internal + FULL TIME"
                ),
                "tier_basis": "Accumulated total amount (invoice total_amount)",
                "anp_timing": "Commission paid in month after invoice_date month",
            }

            dc = out_dir / f"{prefix}_table2_by_customer.csv"
            sc = out_dir / f"{prefix}_table1_accumulated_anp.csv"
            write_csv(dc, table2_rows, TABLE2_CSV_FIELDS)
            write_csv(sc, table1_rows, TABLE1_CSV_FIELDS)

            xlsx_path = out_dir / f"{prefix}.xlsx"
            if not args.no_excel:
                try:
                    write_excel(xlsx_path, table1_rows, table2_rows, meta)
                except ImportError:
                    print("openpyxl not installed; skipped Excel.", file=sys.stderr)

            if args.print_each_month:
                print(f"\n--- {calendar_label} (paid {pay_label}) ---")
                try:
                    detail_limit = (
                        None if args.full_console else max(0, args.console_detail_limit)
                    )
                    print_report_tables(meta, table1_rows, table2_rows, detail_limit)
                except ImportError:
                    print(
                        "Install tabulate for table output: pip install tabulate",
                        file=sys.stderr,
                    )

        if not overview_rows:
            print(
                f"No qualifying invoices found for invoicing months in {year} "
                "(check agent types and filters).",
                file=sys.stderr,
            )
            return 0

        print()
        print("=" * 100)
        print(f"ANP COMMISSION - ALL INVOICE MONTHS IN {year}")
        print("=" * 100)
        print(
            "One report per invoicing calendar month where data exists "
            "(first payment secured). Commission is paid in the following month.",
        )
        print()
        try:
            print_year_month_overview(overview_rows)
        except ImportError:
            for row in overview_rows:
                print("  ", row)
        print()

        overview_csv = (
            out_dir / f"anp_commission_{year}_months_overview_{stamp}.csv"
        )
        overview_csv.parent.mkdir(parents=True, exist_ok=True)
        with overview_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "invoice_month",
                    "commission_payout_month",
                    "agent_count",
                    "invoice_count",
                    "total_anp_rm",
                ]
            )
            for row in overview_rows:
                writer.writerow(row)

        print(f"Month overview CSV: {overview_csv.resolve()}")
        print(f"Generated {len(overview_rows)} monthly report bundles under: {out_dir.resolve()}")

        return 0

    period_start: date | None = None
    period_end: date | None = None

    if args.all_time:
        payout_label = "all-time"
        invoice_calendar_month = "all"
        commission_payout_month = "N/A"
        if args.payout_month:
            print(
                "Warning: --payout-month ignored when --all-time is set.", file=sys.stderr
            )
    elif default_invoice_year_mode:
        period_start = date(args.year, 1, 1)
        period_end = date(args.year, 12, 31)
        invoice_calendar_month = f"{args.year}-01..{args.year}-12"
        commission_payout_month = "varies by invoice month"
        payout_label = f"invoice-year-{args.year}"
    elif args.month is not None:
        period_start = date(args.year, args.month, 1)
        last_day = monthrange(args.year, args.month)[1]
        period_end = date(args.year, args.month, last_day)
        invoice_calendar_month = f"{args.year:04d}-{args.month:02d}"
        pay_y, pay_m = commission_payout_month_for_invoice_month(args.year, args.month)
        commission_payout_month = f"{pay_y:04d}-{pay_m:02d}"
        payout_label = f"inv-{invoice_calendar_month}_pay-{commission_payout_month}"
    else:
        if args.payout_month:
            py, pm = map(int, args.payout_month.split("-"))
        else:
            tdy = date.today()
            py, pm = tdy.year, tdy.month
        period_start, period_end = invoice_period_bounds(py, pm)
        invoice_calendar_month = (
            f"{period_start.year:04d}-{period_start.month:02d}"
            if period_start
            else "unknown"
        )
        commission_payout_month = f"{py:04d}-{pm:02d}"
        payout_label = f"inv-{invoice_calendar_month}_pay-{commission_payout_month}"

    table1_rows, table2_rows = build_anp_tables(
        agents, invoices, customers, period_start, period_end
    )

    report_year = period_start.year if period_start else date.today().year
    total_users = count_distinct_agents_in_year(
        invoices, set(agent_ids), report_year
    )

    prefix = f"anp_commission_{payout_label}_{stamp}"
    table1_csv = out_dir / f"{prefix}_table1_accumulated_anp.csv"
    table2_csv = out_dir / f"{prefix}_table2_by_customer.csv"
    write_csv(table1_csv, table1_rows, TABLE1_CSV_FIELDS)
    write_csv(table2_csv, table2_rows, TABLE2_CSV_FIELDS)

    meta = {
        "payout_period": payout_label,
        "invoice_calendar_month": invoice_calendar_month,
        "commission_payout_month": commission_payout_month,
        "invoice_date_from": period_start.isoformat() if period_start else "all",
        "invoice_date_to": period_end.isoformat() if period_end else "all",
        "agent_types": agent_types_str,
        "total_qualifying_agents": total_users,
        "total_qualifying_invoices": len(table2_rows),
        "filter_description": (
            f"{args.year} invoices, 1st payment secured, agent types: {agent_types_str}"
        ),
        "tier_basis": "Accumulated total amount (invoice total_amount)",
        "anp_timing": "Commission paid in month after invoice_date month",
    }

    xlsx_path = out_dir / f"{prefix}.xlsx"
    if not args.no_excel:
        try:
            write_excel(xlsx_path, table1_rows, table2_rows, meta)
        except ImportError:
            print("openpyxl not installed; skipped Excel.", file=sys.stderr)

    try:
        detail_limit = None if args.full_console else max(0, args.console_detail_limit)
        print_report_tables(meta, table1_rows, table2_rows, detail_limit)
    except ImportError:
        print("Install tabulate for table output: pip install tabulate", file=sys.stderr)

    print("Files saved:")
    print(f"  Excel (tables): {xlsx_path.resolve() if xlsx_path.exists() else '(skipped)'}")
    print(f"  Table 1 CSV:    {table1_csv.resolve()}")
    print(f"  Table 2 CSV:    {table2_csv.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
