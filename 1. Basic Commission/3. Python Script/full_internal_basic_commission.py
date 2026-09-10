"""
Full Internal Basic Commission Report (2026) via Postgres read-only proxy.
Calculates basic commissions categorized by Residential, Shop Lot, Commercial, and Factory packages,
including senior overrides, Table 3 Factory separation, and Table 4 Referral Fees.

Environment:
  PG_PROXY_TOKEN   Bearer token (required)

Optional:
  PG_PROXY_URL     default https://pg-proxy-production.up.railway.app/api/sql
  PG_PROXY_DB      default prod_main
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
_OUTPUT_DIR = _SCRIPT_DIR.parent / "2. Output"


def _import_pdf_writer():
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from commission_pdf import PdfSection, write_commission_pdf
    return PdfSection, write_commission_pdf


def _write_basic_pdf(
    *,
    path: Path,
    year: int,
    user_count: int,
    customer_tokens: list[str],
    table1: list[list[str]],
    table2: list[list[str]],
    table3: list[list[str]],
    table4: list[list[str]],
) -> Path:
    PdfSection, write_commission_pdf = _import_pdf_writer()
    meta = [
        ("Year (full_payment_date)", str(year)),
        ("Filters", "paid = TRUE; agent_type in (internal, full time)"),
        (
            "Customer filter",
            " OR ".join(customer_tokens) if customer_tokens else "(none)",
        ),
        ("Qualifying agents", str(user_count)),
        ("Executive rate", "3.00%"),
        ("Senior rate", "3.25% (Sunny, Martin, Kent, Zhe Hang)"),
        (
            "Senior override",
            "+0.25% of each report's accumulated basic commission (Executive tier only)",
        ),
        ("Formula", "Basic Commission = Sales Price x Rate%"),
        ("Factory rate", "2.00% + profit sharing"),
    ]
    write_commission_pdf(
        path,
        title="Basic Commission Report",
        meta_lines=meta,
        sections=[
            PdfSection(
                title="Table 1: Final Commission Payout Summary by agent",
                headers=["Agent Name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"],
                rows=table1,
            ),
            PdfSection(
                title="Table 2: Accumulated Basic Commission by Customer (Residential and Shop Lot)",
                headers=[
                    "Agent Name",
                    "Customer Name",
                    "Invoice Number",
                    "Package",
                    "Invoice Date",
                    "Full Payment Date",
                    "Total Amount",
                    "Payment Received",
                    "Epp",
                    "Sales Price",
                    "Rate %",
                    "Basic Commission",
                    "Senior Override",
                ],
                rows=table2,
                landscape=True,
            ),
            PdfSection(
                title="Table 3: Accumulated Basic Commission by Customer (Factory)",
                headers=[
                    "Agent Name",
                    "Customer Name",
                    "Invoice Number",
                    "Package",
                    "Invoice Date",
                    "Full Payment Date",
                    "Total Amount",
                    "Payment Received",
                    "Epp",
                    "Sales Price",
                    "Rate %",
                    "Profit Sharing",
                    "Basic Commission",
                ],
                rows=table3,
                landscape=True,
            ),
            PdfSection(
                title="Table 4: Referral Fee",
                headers=[
                    "Referral Name",
                    "Agent Name",
                    "Customer Name",
                    "Invoice Number",
                    "Invoice Date",
                    "Full Payment Date",
                    "Sales Price",
                    "Rate %",
                    "Referral Fee",
                ],
                rows=table4 if table4 else [["No referral fee", "-", "-", "-", "-", "-", "-", "-", "-"]],
            ),
        ],
    )
    return path


EXECUTIVE_RATE = Decimal("0.03")
SENIOR_RATE = Decimal("0.0325")
SENIOR_OVERRIDE_RATE = Decimal("0.0025")

AGENT_TYPES_SQL = "'internal', 'full time'"


def _rates_module():
    """basic_commission_rates, or None when it cannot be imported. Routing must
    never be the thing that breaks a report, so every caller falls back to the
    Postgres-only behaviour this module had before."""
    try:
        import basic_commission_rates as _bcr
        return _bcr
    except Exception:
        return None


def _agent_type_override_sql() -> str:
    """Names the Agent Roles & Hierarchy page routes explicitly, as a SQL list.
    Postgres' agent_type is blank for several internal agents, so filtering on
    it alone drops them here and hands them to the outsource report. Widening
    the filter with these names lets _process_invoices() decide per invoice —
    it can see the invoice date, which is what the roles page is dated by."""
    bcr = _rates_module()
    names = bcr.agent_type_override_names() if bcr else []
    if not names:
        return "''"
    return ", ".join("'" + n.replace("'", "''") + "'" for n in names)


def _internal_hierarchy(agent_name: str, month: int, year: int, property_type: str,
                        is_agent_senior: bool) -> str:
    """The role to price this invoice under. The Agent Roles & Hierarchy page is
    asked first, so Data page rows entered against an internal role like
    "Branch Sales Manager" can actually be reached — is_senior() only ever
    produced "Senior" or "Executive", which left every other internal role's
    row dead.

    A role with no rate row entered against it falls back to the old
    Senior/Executive answer rather than dropping through to the hardcoded
    default. Roles are renamed on that page more often than rates are entered
    (Executive -> "Sales Consultant" in 2026-07), and a rename must not quietly
    cut an agent's rate."""
    bcr = _rates_module()
    fallback = "Senior" if is_agent_senior else "Executive"
    if bcr is None:
        return fallback
    role = bcr.get_agent_role(agent_name, month, year=year, agent_type="Internal")
    if not role:
        return fallback
    _rate, source, _eff = bcr.get_basic_rate_detail(
        "Internal", role, month, agent=agent_name, property_type=property_type,
        year=year)
    return role if source in ("unified", "legacy") else fallback


def _is_internal_invoice(agent_name: str, pg_agent_type: str, invoice_date: str) -> bool:
    """Whether this invoice belongs to the internal report. The Agent Roles &
    Hierarchy page wins over Postgres; without it an agent whose agent_type was
    never set in Bubble is treated as outsource and paid the outsource rate."""
    bcr = _rates_module()
    if bcr is None:
        return str(pg_agent_type or "").strip().lower() in ("internal", "full time")
    parsed = _parse_invoice_date(invoice_date)
    return bcr.resolve_agent_type(
        agent_name, pg_agent_type,
        month=parsed.month if parsed else None,
        year=parsed.year if parsed else None,
    ) == "internal"


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

    # 2. Check SEDA Registration nem_type
    nem = str(row.get("seda_nem_type") or "").upper().strip()
    if "RAKYAT" in nem:
        return "Residential"
    if "SHOPLOT" in nem or "SHOP-LOT" in nem or "COMMERCIAL" in nem:
        return "Shop Lot"
    if "FACTORY" in nem:
        return "Factory"

    # 3. Check Referral project_type
    ref = str(row.get("referral_project_type") or "").upper().strip()
    if "RESIDENTIAL" in ref:
        return "Residential"
    if "SHOP-LOT" in ref or "SHOPLOT" in ref or "COMMERCIAL" in ref:
        return "Shop Lot"
    if "FACTORY" in ref:
        return "Factory"

    # 4. Check package_type, package_name_snapshot, description
    for field in ("package_type", "package_name_snapshot", "description"):
        val = str(row.get(field) or "").upper().strip()
        if not val:
            continue
        if "FACTORY" in val:
            return "Factory"
        if "RESIDENTIAL" in val:
            return "Residential"
        if "SHOP" in val or "COMMERCIAL" in val:
            return "Shop Lot"

    return "Residential"


def is_senior(agent_name: str) -> bool:
    n = agent_name.lower().strip()
    return any(s in n for s in ["sunny", "martin", "kent", "zhe hang"])


def _table_reporting_senior(agent_name: str, month: int | None = None):
    """(listed, reports_to) from the dashboard role table. Being listed is
    authoritative even when Reports To is blank — that means "reports to
    nobody", not "guess"."""
    try:
        import basic_commission_rates as _bcr
        return _bcr.get_reporting_senior_from_table(agent_name, month or 7)
    except Exception:
        return False, None


def get_reporting_senior(agent_name: str, month: int | None = None) -> str | None:
    listed, senior = _table_reporting_senior(agent_name, month)
    if listed:
        return senior

    n = agent_name.lower().strip()
    if is_senior(agent_name):
        return None

    # Louis Ng, Anisah Najwa and Anisah are under Teng Kah Kent
    if any(tok in n for tok in ["louis ng", "anisah najwa", "anisah"]):
        return "Teng Kah Kent"
        
    # Jia Keat, Zul, Zulkarnain, Denise, Jia Xuan, and Ah Zu are under Sunny Tan
    if any(tok in n for tok in ["jia keat", "zul", "zulkarnain", "denise", "jia xuan", "ah zu"]):
        return "Sunny Tan"
        
    # Joshua is under Zhe Hang
    if "joshua" in n:
        return "CHING ZHE HANG"
        
    # Js are under Martin Hing (exclude Joshua, Jia Keat, Jia Xuan)
    if n.startswith('j') and not any(ex in n for ex in ["joshua", "jia keat", "jia xuan"]):
        return "MARTIN HING"
        
    return None


def _load_dotenv() -> None:
    env_path = _SCRIPT_DIR / ".env"
    if not env_path.is_file():
        env_path = _REPO_ROOT / ".env"
        if not env_path.is_file():
            return
    proxy_keys = frozenset(
        {"PG_PROXY_TOKEN", "PG_PROXY_URL", "PG_PROXY_DB", "PG_DB_NAME"}
    )
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if not name:
            continue
        if name in proxy_keys and os.environ.get(name, "").strip():
            continue
        if name not in os.environ:
            os.environ[name] = value


def _normalize_proxy_url(url: str | None) -> str | None:
    if not url:
        return url
    base = url.strip().rstrip("/")
    if base.endswith("/api/sql"):
        return base
    return f"{base}/api/sql"


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sanitize_contains_token(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    if not re.fullmatch(r"[\w .,'&/-]+", token, flags=re.UNICODE):
        raise ValueError(
            f"Unsafe customer filter token {token!r}. "
            "Use letters, numbers, spaces, and limited punctuation only."
        )
    return token


def _customer_filter_sql(tokens: list[str]) -> str:
    if not tokens:
        return ""
    parts: list[str] = []
    for raw in tokens:
        t = _sanitize_contains_token(raw)
        if not t:
            continue
        lit = _sql_string_literal(f"%{t}%")
        parts.append(
            f"(coalesce(c.name, i.customer_name_snapshot, '') ilike {lit})"
        )
    if not parts:
        return ""
    return " AND (" + " OR ".join(parts) + ")"


def _proxy_sql(
    *,
    proxy_url: str,
    db_name: str,
    token: str,
    sql: str,
    params: list[Any],
) -> dict[str, Any]:
    body = json.dumps({"db_name": db_name, "sql": sql, "params": params}).encode()
    req = urllib.request.Request(
        proxy_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 400 and "token expired" in detail.lower():
            raise RuntimeError(
                "Postgres proxy token expired. Request a new read-only JWT from your "
                "proxy admin, then update PG_PROXY_TOKEN in .env in this folder:\n"
                f"  {_SCRIPT_DIR / '.env'}"
            ) from e
        raise RuntimeError(f"HTTP {e.code} from proxy: {detail}") from e


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return Decimal(str(value))


def _fmt_money(value: Any) -> str:
    return f"{_to_decimal(value):,.2f}"


def _fmt_rate(rate: Decimal) -> str:
    return f"{(rate * 100):.2f}%"


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


def _invoices_sql(*, year: int, customer_filter_sql: str) -> str:
    # Milestone thresholds and the multi-stage cutover come from the Data page
    # (see basic_commission_rates.milestone_sql_parts); they used to be
    # 0.05/0.75/1.0 literals here, which made the payout triggers entered on
    # that page inert.
    from basic_commission_rates import (milestone_sql_parts, milestone_column,
                                        multi_stage_cutover, payout_thresholds_in_use)
    from decimal import Decimal as _D
    milestone_ctes, milestone_selects, milestone_joins = milestone_sql_parts()
    milestone_out_selects = "\n".join(
        f"  {milestone_column(t)}," for t in payout_thresholds_in_use())
    full_col = milestone_column(_D("100"))
    cutover = multi_stage_cutover(year)
    cutover_month = cutover[0] if cutover else 13
    advance_col = milestone_column(
        cutover[1].advance_trigger if cutover else _D("100"))
    return f"""
WITH target_invoices AS (
  SELECT *
  FROM invoice
  WHERE total_amount > 0 
    AND (
      extract(year from invoice_date) = {int(year)} 
      OR bubble_id IN (SELECT linked_invoice FROM payment WHERE extract(year from payment_date) = {int(year)})
    )
),
{milestone_ctes},
candidates AS (
  SELECT
    i.invoice_number,
    i.invoice_date,
    i.bubble_id AS invoice_bubble_id,
    i."1st_payment_date" AS first_payment_date,
    i.full_payment_date AS real_full_payment_date,
{milestone_selects}
    COALESCE(i.total_amount, 0)::numeric AS total_amount,
    COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)), 0)::numeric AS paid_amount,
    COALESCE(
      NULLIF(epp_items.epp_interest, 0),
      NULLIF(pay.epp_sum, 0),
      NULLIF(
        CASE
          WHEN i.effective_epp > 1.0 AND i.effective_epp < 2.0 THEN (i.total_amount * (i.effective_epp - 1.0) / i.effective_epp)::numeric
          WHEN i.effective_epp >= 2.0 AND i.effective_epp <= 100.0 THEN (i.total_amount * (i.effective_epp / 100.0) / (1.0 + i.effective_epp / 100.0))::numeric
          ELSE 0
        END,
        0
      ),
      0
    )::numeric AS epp_interest,
    COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name, '(unknown)') AS customer_name,
    COALESCE(NULLIF(TRIM(a.name), ''), '(unknown)') AS agent_name,
    a.agent_type,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
    ref.project_type AS referral_project_type,
    COALESCE(NULLIF(TRIM(c_referrer.name), ''), NULLIF(TRIM(i.referrer_name), '')) AS referral_name,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC,
               i.full_payment_date DESC NULLS LAST,
               i.id DESC
    ) AS rn
  FROM target_invoices i
  -- Agents live in BOTH the agent table and the user table since the
  -- 2026-07-20 "agent retirement" migration moved most agent rows into
  -- "user" (same bubble_id). Resolve from either, preferring the row that
  -- actually carries an agent_type.
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
{milestone_joins}
  LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
  LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
  LEFT JOIN customer c_ref ON LOWER(TRIM(c_ref.name)) = LOWER(TRIM(COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name)))
  LEFT JOIN referral ref ON (
      ref.linked_invoice = c_ref.customer_id
      OR LOWER(TRIM(ref.name)) = LOWER(TRIM(c_ref.name))
      OR (
        ref.mobile_number IS NOT NULL
        AND c_ref.phone IS NOT NULL
        AND right(regexp_replace(ref.mobile_number, '\\D', '', 'g'), 9) = right(regexp_replace(c_ref.phone, '\\D', '', 'g'), 9)
      )
      OR ref.linked_customer_profile = c_ref.customer_id
    )
    AND EXISTS (
      SELECT 1 FROM (
        SELECT id, bubble_id, name FROM agent
        UNION ALL
        SELECT id, bubble_id, name FROM "user"
      ) a_ref
      WHERE (
        CASE
          WHEN ref.linked_agent ~ '^[0-9]+$' THEN a_ref.id = CAST(ref.linked_agent AS integer)
          ELSE a_ref.bubble_id = ref.linked_agent
        END
      ) AND LOWER(TRIM(a_ref.name)) = LOWER(TRIM(a.name))
    )
  LEFT JOIN customer c_referrer ON c_referrer.customer_id = ref.linked_customer_profile
  LEFT JOIN LATERAL (
    SELECT COALESCE(
      SUM(ii_dedup.epp_interest_amount),
      0
    ) AS epp_interest
    FROM (
      SELECT 
        MAX(
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
      AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) pay ON TRUE
  WHERE (
    -- Before the multi-stage cutover: recognised only at full payment.
    ({full_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {full_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {full_col})::int BETWEEN 1 AND {cutover_month - 1})
    OR
    -- From the cutover: reaching the advance trigger is already reportable.
    ({advance_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {advance_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {advance_col})::int BETWEEN {cutover_month} AND 12)
    OR
    -- July onwards: 100% payment also valid
    ({full_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {full_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {full_col})::int BETWEEN {cutover_month} AND 12)
  )
    AND (
      LOWER(btrim(COALESCE(a.agent_type, ''))) IN ({AGENT_TYPES_SQL})
      OR LOWER(btrim(COALESCE(a.name, ''))) IN ({_agent_type_override_sql()})
    )
    AND LOWER(btrim(COALESCE(a.name, ''))) != 'gan lai soon'
    {customer_filter_sql}
)
SELECT
  invoice_number,
  invoice_date,
  first_payment_date,
  real_full_payment_date,
{milestone_out_selects}
  total_amount,
  paid_amount,
  epp_interest,
  customer_name,
  agent_name,
  agent_type,
  package_type,
  package_name_snapshot,
  description,
  seda_nem_type,
  referral_project_type,
  referral_name
FROM candidates
WHERE rn = 1
ORDER BY agent_name ASC, real_full_payment_date ASC NULLS LAST, invoice_number ASC NULLS LAST
""".strip()


@dataclass
class InvoiceLine:
    agent_name: str
    customer_name: str
    invoice_number: str
    package: str
    invoice_date: str
    full_payment_date: str
    total_amount: Decimal
    paid_amount: Decimal
    epp_interest: Decimal
    sales_price: Decimal
    commission_rate: Decimal
    profit_sharing: Decimal
    basic_commission: Decimal
    referral_name: str | None = None
    gan_lai_soon: Decimal = Decimal("0")
    senior_override: Decimal = Decimal("0")
    first_payment_date: str = ""
    pct100_date: str = ""
    pct75_date: str = ""
    pct5_date: str = ""
    # The Data page payout condition this invoice was recognised under, so the
    # month-bucketing in build_commission_pack does not have to re-derive it.
    payout_policy: object = None

    @property
    def net_base(self) -> Decimal:
        return self.sales_price


def _parse_invoice_date(date_val: Any) -> datetime | None:
    if not date_val:
        return None
    if isinstance(date_val, datetime):
        return date_val
    s = str(date_val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
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


def _referral_rate(invoice_date_val: Any) -> Decimal:
    """
    Referral fee is 2% from sales price per invoice.
    """
    return Decimal("0.02")


def _is_valid_referral(ref_name: str | None) -> bool:
    if not ref_name:
        return False
    name_clean = ref_name.strip().lower()
    return name_clean not in ("", "none", "no", "client", "(unknown)", "referrer")


def _cutover_ym(year: int = 2026) -> tuple[int, int] | None:
    """(year, month) the multi-stage payout starts, per the Data page."""
    from basic_commission_rates import multi_stage_cutover
    hit = multi_stage_cutover(year)
    return (year, hit[0]) if hit else None


def _get_effective_payment_date(real_full_pay_str: str, pct75_str: str, pct100_str: str = "", pct5_str: str = "",
                                cutover: tuple[int, int] | None = None) -> str:
    # If 100% payment date exists, use it
    if pct100_str:
        return pct100_str

    if cutover is None:
        cutover = _cutover_ym()
    if cutover is None:
        return real_full_pay_str
    cut_y, cut_m = cutover

    pct75_m = None
    pct75_y = None
    if pct75_str:
        parts = pct75_str.split("-")
        if len(parts) >= 2:
            try:
                pct75_y = int(parts[0])
                pct75_m = int(parts[1])
            except ValueError:
                pass

    real_fp_m = None
    real_fp_y = None
    if real_full_pay_str:
        parts = real_full_pay_str.split("-")
        if len(parts) >= 2:
            try:
                real_fp_y = int(parts[0])
                real_fp_m = int(parts[1])
            except ValueError:
                pass

    if pct75_y is not None and pct75_m is not None:
        if (pct75_y, pct75_m) >= (cut_y, cut_m):
            return pct75_str

        # The balance milestone landed before the cutover.
        is_paid_before_cutover = False
        if real_fp_y is not None and real_fp_m is not None:
            if (real_fp_y, real_fp_m) < (cut_y, cut_m):
                is_paid_before_cutover = True

        if not is_paid_before_cutover:
            return f"{cut_y:04d}-{cut_m:02d}-01"

        return real_full_pay_str

    # June forward 5% booking fee
    if pct5_str:
        return pct5_str

    return ""


def _process_invoices(
    rows: list[dict[str, Any]],
    factory_rates: dict[str, Decimal] | None = None
) -> list[InvoiceLine]:
    factory_rates = factory_rates or {}
    lines: list[InvoiceLine] = []
    
    for row in rows:
        agent_name = str(row.get("agent_name") or "(unknown)").strip()
        customer_name = str(row.get("customer_name") or "(unknown)").strip()
        invoice_num = str(row.get("invoice_number") or "").strip()

        # The SQL filter is deliberately wide (it cannot date-match the roles
        # page), so drop anything that resolves to the outsource report here.
        if not _is_internal_invoice(agent_name, row.get("agent_type"),
                                    row.get("invoice_date")):
            continue

        prop_type = classify_property_type(row)
        
        total = _to_decimal(row.get("total_amount"))
        paid_amount = _to_decimal(row.get("paid_amount"))
        epp = _to_decimal(row.get("epp_interest"))
        sales_price = total - epp
        
        is_agent_senior = is_senior(agent_name)
        sharing = Decimal("0")
        
        real_full_pay_str = str(row.get("real_full_payment_date") or "")[:10]

        # Which payment milestones govern this invoice comes from the Data page
        # row that also priced it, so pct5_str/pct75_str mean "advance due" and
        # "balance due" whatever percentages happen to back them.
        inv_dt = str(row.get("invoice_date") or "")[:10]
        _inv_parsed = _parse_invoice_date(inv_dt)
        _inv_month = _inv_parsed.month if _inv_parsed else 5
        _inv_year = _inv_parsed.year if _inv_parsed else 2026
        _hierarchy = _internal_hierarchy(agent_name, _inv_month, _inv_year,
                                         prop_type, is_agent_senior)
        from basic_commission_rates import invoice_milestones as _milestones
        policy, pct5_str, pct75_str, pct100_str = _milestones(
            row, "Internal", _hierarchy, _inv_month, year=_inv_year,
            agent=agent_name, property_type=prop_type)

        NOT_FULLY_PAID_INVS = {'1008316', '1007905'}
        if invoice_num in NOT_FULLY_PAID_INVS:
            real_full_pay_str = ""
            pct75_str = ""
            pct100_str = ""

        if policy.is_multi_stage:
            pay_dt = pct75_str
            first_pay_dt = pct5_str if pct5_str else str(row.get("first_payment_date") or "")[:10]
        else:
            pay_dt = _get_effective_payment_date(real_full_pay_str, pct75_str, pct100_str, pct5_str)
            first_pay_dt = str(row.get("first_payment_date") or "")[:10]
        
        if prop_type == "Factory":
            rate = Decimal("0.02")
            sharing_tuple = factory_rates.get(invoice_num, (Decimal("0"), Decimal("0")))
            if isinstance(sharing_tuple, tuple):
                sharing = sharing_tuple[0]
                safwan_sharing = sharing_tuple[1]
            else:
                sharing = sharing_tuple
                safwan_sharing = sharing_tuple
            comm = sales_price * (rate + sharing)
        else:
            from basic_commission_rates import get_basic_rate, get_rule_amount
            # Rates and rules are based on Invoice Date (inv_month/inv_year), not
            # Payment Date. Pass the deal's property type so rows scoped on the
            # Data page can match, and the year so a range like "2025-01 to
            # 2025-09" isn't matched against the wrong calendar year. The
            # hierarchy is the one the payout policy above already resolved, so
            # rate and payout condition can never come from different roles.
            inv_month, inv_year, hierarchy = _inv_month, _inv_year, _hierarchy
            rate = get_basic_rate("Internal", hierarchy, inv_month, agent=agent_name,
                                  property_type=prop_type, year=inv_year)
            comm = sales_price * rate

            # Only the advance has been reached: cap the commission at the
            # advance amount. That amount comes from the Data page row's own
            # Amount (RM) column, falling back to the legacy
            # 'basic_commission_cap' rule and then to RM 300.
            if not pct75_str and pct5_str:
                cap = (policy.advance_amount if policy.advance_amount is not None
                       else get_rule_amount("basic_commission_cap", inv_month,
                                            year=inv_year, default=Decimal("300")))
                comm = min(comm, cap)
            
        pay_dt = str(pay_dt or "")[:10]
        first_pay_dt = str(first_pay_dt or "")[:10]

        # Determine referral name: default to database values, with the
        # "Referral"/"Referrer" prefix some entries carry stripped off so one
        # person spelled several ways is one referrer.
        import agent_names as _names
        ref_name = _names.clean_referral_name(row.get("referral_name"))

        # Calculate senior override per invoice (based on sales_price)
        senior_ovr = sales_price * SENIOR_OVERRIDE_RATE
        senior = get_reporting_senior(agent_name)
        if senior:
            senior_ovr = sales_price * SENIOR_OVERRIDE_RATE

        lines.append(
            InvoiceLine(
                agent_name=agent_name,
                customer_name=customer_name,
                invoice_number=invoice_num,
                package=prop_type,
                invoice_date=inv_dt,
                full_payment_date=pay_dt,
                total_amount=total,
                paid_amount=paid_amount,
                epp_interest=epp,
                sales_price=sales_price,
                commission_rate=rate,
                profit_sharing=sharing,
                basic_commission=comm,
                referral_name=ref_name,
                gan_lai_soon=Decimal("0"),
                senior_override=senior_ovr,
                first_payment_date=first_pay_dt,
                pct100_date=pct100_str,
                pct75_date=pct75_str,
                pct5_date=pct5_str,
                payout_policy=policy,
            )
        )
        
        if prop_type == "Factory":
            safwan_rate = Decimal("0.005")
            safwan_comm = sales_price * (safwan_rate + safwan_sharing)
            lines.append(
                InvoiceLine(
                    agent_name="Safwan",
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    paid_amount=paid_amount,
                    epp_interest=epp,
                    sales_price=sales_price,
                    commission_rate=safwan_rate,
                    profit_sharing=safwan_sharing,
                    basic_commission=safwan_comm,
                    referral_name=ref_name,
                    first_payment_date=first_pay_dt,
                    # Milestone dates deliberately not set: this share has never
                    # appeared in the monthly buckets (no dates to bucket by),
                    # and wiring the payout policy must not change that. The
                    # policy still rides along so the line is classified like
                    # its parent instead of falling back to the July rule.
                    payout_policy=policy,
                )
            )
            
    return lines


def _table1_rows(lines: list[InvoiceLine]) -> tuple[int, list[list[str]]]:
    """
    Final Commission Payout Summary by agent
    Header: Agent Name, Total Sales, Own Commission, Senior Override Commission, Total Commission Payout
    """
    agent_own_commissions: dict[str, Decimal] = defaultdict(Decimal)
    agent_sales: dict[str, Decimal] = defaultdict(Decimal)
    override_commissions: dict[str, Decimal] = defaultdict(Decimal)
    
    for ln in lines:
        agent_own_commissions[ln.agent_name] += ln.basic_commission
        agent_sales[ln.agent_name] += ln.total_amount
        
        # Calculate senior override if the agent is an executive under a senior
        senior = get_reporting_senior(ln.agent_name)
        if senior:
            override_commissions[senior] += ln.sales_price * SENIOR_OVERRIDE_RATE

    all_payout_agents = set(agent_own_commissions.keys()) | set(override_commissions.keys())
    
    table_rows: list[list[str]] = []
    for agent in sorted(all_payout_agents, key=lambda a: a.lower()):
        own = agent_own_commissions.get(agent, Decimal("0"))
        ovr = override_commissions.get(agent, Decimal("0"))
        sales = agent_sales.get(agent, Decimal("0"))
        total_payout = own + ovr
        table_rows.append([
            agent,
            _fmt_money(sales),
            _fmt_money(own),
            _fmt_money(ovr),
            _fmt_money(total_payout),
        ])
    return len(all_payout_agents), table_rows


def _table2_rows(lines: list[InvoiceLine]) -> list[list[str]]:
    """
    Accumulated Basic Commission by Customer (Residential and Shop Lot)
    """
    raw_lines = [ln for ln in lines if ln.package in ("Residential", "Shop Lot")]
    raw_lines.sort(key=lambda x: (x.agent_name.lower(), x.full_payment_date))
    return [
        [
            ln.agent_name,
            ln.customer_name,
            ln.invoice_number,
            ln.package,
            ln.invoice_date,
            ln.full_payment_date,
            _fmt_money(ln.total_amount),
            _fmt_money(ln.paid_amount),
            _fmt_money(ln.epp_interest),
            _fmt_money(ln.sales_price),
            _fmt_rate(ln.commission_rate),
            _fmt_money(ln.basic_commission),
            _fmt_money(ln.senior_override),
        ]
        for ln in raw_lines
    ]


def _table3_rows(lines: list[InvoiceLine]) -> list[list[str]]:
    """
    Accumulated Basic Commission by Customer (Factory)
    """
    raw_lines = [ln for ln in lines if ln.package == "Factory"]
    raw_lines.sort(key=lambda x: (x.agent_name.lower(), x.full_payment_date))
    return [
        [
            ln.agent_name,
            ln.customer_name,
            ln.invoice_number,
            ln.package,
            ln.invoice_date,
            ln.full_payment_date,
            _fmt_money(ln.total_amount),
            _fmt_money(ln.paid_amount),
            _fmt_money(ln.epp_interest),
            _fmt_money(ln.sales_price),
            _fmt_rate(ln.commission_rate),
            _fmt_rate(ln.profit_sharing),
            _fmt_money(ln.basic_commission),
        ]
        for ln in raw_lines
    ]


def _table4_rows(lines: list[InvoiceLine]) -> list[list[str]]:
    """
    Referral Fee Table
    """
    table_rows: list[list[str]] = []
    for ln in lines:
        if _is_valid_referral(ln.referral_name):
            sales_price = ln.sales_price
            # A rate typed on the dashboard wins; otherwise the standard one.
            rate = (getattr(ln, "referral_rate_override", None)
                    or _referral_rate(ln.invoice_date))
            fee = sales_price * rate
            table_rows.append([
                ln.referral_name.strip(),
                ln.agent_name.strip(),
                ln.customer_name.strip(),
                ln.invoice_number.strip(),
                ln.invoice_date.strip(),
                ln.full_payment_date.strip(),
                _fmt_money(sales_price),
                _fmt_rate(rate),
                _fmt_money(fee),
            ])
    table_rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    return table_rows


def get_factory_rates(raw_rows: list[dict[str, Any]], cli_factory_rates: dict[str, tuple[Decimal, Decimal]] | None = None, default_profit_sharing: tuple[Decimal, Decimal] | None = None) -> dict[str, tuple[Decimal, Decimal]]:
    cli_factory_rates = cli_factory_rates or {}
    factory_invoices = [r for r in raw_rows if classify_property_type(r) == "Factory"]
    factory_rates: dict[str, tuple[Decimal, Decimal]] = {}
    if factory_invoices:
        for r in factory_invoices:
            inv_num = str(r.get("invoice_number") or "").strip()
            cust_name = str(r.get("customer_name") or "(unknown)").strip()
            total = _to_decimal(r.get("total_amount"))
            epp = _to_decimal(r.get("epp_interest"))
            sales_price = total - epp

            if inv_num in cli_factory_rates:
                factory_rates[inv_num] = cli_factory_rates[inv_num]
            elif default_profit_sharing is not None:
                factory_rates[inv_num] = default_profit_sharing
            elif not sys.stdin.isatty():
                factory_rates[inv_num] = (Decimal("0"), Decimal("0"))
            else:
                prompt_label = f"INV: {inv_num} | Customer: {cust_name} | Sales Price: {_fmt_money(sales_price)}"
                while True:
                    try:
                        val = input(f"Insert Main Agent Profit Sharing Rate% for {prompt_label}: ").strip()
                        if not val:
                            rate_val = Decimal("0")
                        else:
                            rate_val = Decimal(val.replace("%", "").strip()) / Decimal("100")
                            
                        val_safwan = input(f"Insert Safwan Profit Sharing Rate% for {prompt_label}: ").strip()
                        if not val_safwan:
                            safwan_rate_val = Decimal("0")
                        else:
                            safwan_rate_val = Decimal(val_safwan.replace("%", "").strip()) / Decimal("100")
                            
                        factory_rates[inv_num] = (rate_val, safwan_rate_val)
                        break
                    except (KeyboardInterrupt, EOFError):
                        print(f"\nInterrupted. Defaulting profit sharing for {inv_num} to 0%.")
                        factory_rates[inv_num] = (Decimal("0"), Decimal("0"))
                        break
                    except Exception as e:
                        print(f"Invalid input: {e}. Please enter a numeric value (e.g. 5 or 5%).")
    return factory_rates


def main(argv: list[str]) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Basic Commission report — Tables 1, 2, 3 and 4."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=None, help="Month to filter (1-12)")
    parser.add_argument(
        "--customer-contains",
        action="append",
        default=[],
        help="Substring filter on customer name (repeat for OR).",
    )
    parser.add_argument(
        "--profit-sharing",
        type=float,
        default=None,
        help="Default profit sharing %% for Factory (e.g. 5.0 for 5%%)",
    )
    parser.add_argument(
        "--factory-rates",
        default=None,
        help="JSON string or comma-separated key:value pairs mapping invoice_number to profit sharing %%",
    )
    parser.add_argument(
        "--proxy-url",
        default=_normalize_proxy_url(
            _env(
                "PG_PROXY_URL",
                "https://pg-proxy-production.up.railway.app/api/sql",
            )
        ),
    )
    parser.add_argument(
        "--db-name",
        default=_env("PG_PROXY_DB") or _env("PG_DB_NAME", "prod_main"),
    )
    parser.add_argument(
        "--pdf-output",
        type=Path,
        default=None,
        help="PDF path",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Do not write PDF file",
    )
    args = parser.parse_args(argv)

    token = _env("PG_PROXY_TOKEN")
    if not token:
        print(
            f"Missing PG_PROXY_TOKEN. Create or edit:\n  {_SCRIPT_DIR / '.env'}",
            file=sys.stderr,
        )
        return 2
    if not args.proxy_url or not args.db_name:
        print("Missing proxy URL or database name.", file=sys.stderr)
        return 2

    tokens: list[str] = []
    for chunk in args.customer_contains:
        for part in str(chunk).split(","):
            part = part.strip()
            if part:
                tokens.append(part)
    try:
        customer_filter_sql = _customer_filter_sql(tokens)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    payload = _proxy_sql(
        proxy_url=args.proxy_url,
        db_name=args.db_name,
        token=token,
        sql=_invoices_sql(year=args.year, customer_filter_sql=customer_filter_sql),
        params=[],
    )
    raw_rows = list(payload.get("rows") or [])

    if args.month is not None:
        from basic_commission_rates import multi_stage_cutover, milestone_date
        _cut = multi_stage_cutover(args.year)
        _bal_t = _cut[1].balance_trigger if _cut else Decimal("100")
        filtered_rows = []
        for r in raw_rows:
            inv_dt = _parse_invoice_date(r.get("invoice_date"))
            real_fp = str(r.get("real_full_payment_date") or "")[:10]
            pct75 = milestone_date(r, _bal_t)
            pct100 = milestone_date(r, Decimal("100"))
            eff_pay = _get_effective_payment_date(real_fp, pct75, pct100)
            pay_dt = _parse_invoice_date(eff_pay)
            inv_match = inv_dt and inv_dt.year == args.year and inv_dt.month == args.month
            pay_match = pay_dt and pay_dt.year == args.year and pay_dt.month == args.month
            if inv_match or pay_match:
                filtered_rows.append(r)
        raw_rows = filtered_rows
    
    cli_factory_rates: dict[str, Decimal] = {}
    if args.factory_rates:
        raw_rates = args.factory_rates.strip()
        if raw_rates.startswith("{"):
            try:
                parsed = json.loads(raw_rates)
                for k, v in parsed.items():
                    cli_factory_rates[str(k).strip()] = Decimal(str(v)) / Decimal("100")
            except Exception as e:
                print(f"Error parsing --factory-rates as JSON: {e}", file=sys.stderr)
                return 1
        else:
            for part in raw_rates.split(","):
                if not part.strip():
                    continue
                if ":" not in part:
                    print(f"Error: --factory-rates format should be invoice_number:rate. Got: '{part}'", file=sys.stderr)
                    return 1
                k, _, v = part.partition(":")
                try:
                    cli_factory_rates[k.strip()] = Decimal(v.strip().replace("%", "")) / Decimal("100")
                except Exception as e:
                    print(f"Error parsing rate for invoice {k}: {e}", file=sys.stderr)
                    return 1

    default_profit_sharing = None
    if args.profit_sharing is not None:
        default_profit_sharing = Decimal(str(args.profit_sharing)) / Decimal("100")
        
    factory_rates = get_factory_rates(raw_rows, cli_factory_rates, default_profit_sharing)
    
    lines = _process_invoices(raw_rows, factory_rates)

    user_count, table1 = _table1_rows(lines)
    table2 = _table2_rows(lines)
    table3 = _table3_rows(lines)
    table4 = _table4_rows(lines)

    print("=== Basic Commission Report ===")
    print(f"Year (full_payment_date): {args.year}")
    print("Filters: paid = TRUE, agent_type in (internal, full time)")
    print(
        "Customer filter: "
        + (" OR ".join(repr(t) for t in tokens) if tokens else "(none)")
    )
    print(f"Total internal full-time agents: {user_count}")
    print()
    print("Rates: Executive m = 3.00%; Senior m = 3.25%; Factory = 2.00% + profit sharing")
    print(
        "Senior override: +0.25% of each report's accumulated basic commission (Executive tier only) "
        "(Sunny: Jia Keat, Zulkarnain, Denise, Jia Xuan, Ah Zu; "
        "Kent: Louis Ng, Anisah Najwa)"
    )
    print()

    print("=== Table 1: Final Commission Payout Summary by agent ===")
    print(_render_table(["Agent Name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"], table1))
    print()

    print("=== Table 2: Accumulated Basic Commission by Customer (Residential and Shop Lot) ===")
    print(
        _render_table(
            [
                "Agent Name",
                "Customer Name",
                "Invoice Number",
                "Package",
                "Invoice Date",
                "Full Payment Date",
                "Total Amount",
                "Payment Received",
                "Epp",
                "Sales Price",
                "Rate %",
                "Basic Commission",
                "Senior Override",
            ],
            table2,
        )
    )
    print()

    print("=== Table 3: Accumulated Basic Commission by Customer (Factory) ===")
    print(
        _render_table(
            [
                "Agent Name",
                "Customer Name",
                "Invoice Number",
                "Package",
                "Invoice Date",
                "Full Payment Date",
                "Total Amount",
                "Payment Received",
                "Epp",
                "Sales Price",
                "Rate %",
                "Profit Sharing",
                "Basic Commission",
            ],
            table3,
        )
    )
    print()

    print("=== Table 4: Referral Fee ===")
    print(
        _render_table(
            [
                "Referral Name",
                "Agent Name",
                "Customer Name",
                "Invoice Number",
                "Invoice Date",
                "Full Payment Date",
                "Sales Price",
                "Rate %",
                "Referral Fee",
            ],
            table4,
        )
    )

    # Save to CSV
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    csv_t1 = _OUTPUT_DIR / f"full_basic_commission_payout_summary_{args.year}_{stamp}.csv"
    with open(csv_t1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Agent Name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"])
        w.writerows(table1)
        
    csv_t2 = _OUTPUT_DIR / f"full_basic_commission_residential_shoplot_{args.year}_{stamp}.csv"
    with open(csv_t2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Senior Override"
        ])
        w.writerows(table2)
        
    csv_t3 = _OUTPUT_DIR / f"full_basic_commission_factory_{args.year}_{stamp}.csv"
    with open(csv_t3, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Profit Sharing", "Basic Commission"
        ])
        w.writerows(table3)

    if table4:
        csv_t4 = _OUTPUT_DIR / f"full_basic_commission_referral_{args.year}_{stamp}.csv"
        with open(csv_t4, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([
                "Referral Name", "Agent Name", "Customer Name", "Invoice Number", "Invoice Date", "Full Payment Date", "Sales Price", "Rate %", "Referral Fee"
            ])
            w.writerows(table4)
        
    print(f"CSV files saved to {_OUTPUT_DIR.resolve()}")

    if not args.no_pdf:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_path = args.pdf_output or (
            _OUTPUT_DIR / f"full_internal_basic_commission_{args.year}_{stamp}.pdf"
        )
        try:
            _write_basic_pdf(
                path=pdf_path,
                year=args.year,
                user_count=user_count,
                customer_tokens=tokens,
                table1=table1,
                table2=table2,
                table3=table3,
                table4=table4,
            )
            print()
            print(f"PDF saved: {pdf_path.resolve()}")
        except ImportError as e:
            print(f"\nPDF not saved: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
