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


def classify_property_type(row: dict[str, Any]) -> str:
    # 1. Check SEDA Registration nem_type
    nem = str(row.get("seda_nem_type") or "").upper().strip()
    if "RAKYAT" in nem:
        return "Residential"
    if "SHOPLOT" in nem or "SHOP-LOT" in nem or "COMMERCIAL" in nem:
        return "Shop Lot"
    if "FACTORY" in nem:
        return "Factory"

    # 2. Check Referral project_type
    ref = str(row.get("referral_project_type") or "").upper().strip()
    if "RESIDENTIAL" in ref:
        return "Residential"
    if "SHOP-LOT" in ref or "SHOPLOT" in ref or "COMMERCIAL" in ref:
        return "Shop Lot"
    if "FACTORY" in ref:
        return "Factory"

    # 3. Check invoice package_type or package_name_snapshot
    pkg_type = str(row.get("package_type") or "").upper().strip()
    if "RESIDENTIAL" in pkg_type:
        return "Residential"
        
    pkg_name = str(row.get("package_name_snapshot") or "").upper().strip()
    if "FACTORY" in pkg_name:
        return "Factory"
    if "SHOP" in pkg_name or "COMMERCIAL" in pkg_name:
        return "Shop Lot"
    if "RESIDENTIAL" in pkg_name:
        return "Residential"

    # 4. Check description or item descriptions
    desc = str(row.get("description") or "").upper().strip()
    if "FACTORY" in desc:
        return "Factory"
    if "SHOP" in desc or "COMMERCIAL" in desc:
        return "Shop Lot"
    if "RESIDENTIAL" in desc:
        return "Residential"

    if row.get("package_type") == "Residential":
        return "Residential"
        
    return "Shop Lot"


def is_senior(agent_name: str) -> bool:
    n = agent_name.lower().strip()
    return any(s in n for s in ["sunny", "martin", "kent", "zhe hang"])


def get_reporting_senior(agent_name: str) -> str | None:
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
    return f"""
WITH candidates AS (
  SELECT
    i.invoice_number,
    i.invoice_date,
    i.full_payment_date,
    COALESCE(i.total_amount, 0)::numeric AS total_amount,
    COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0)::numeric AS paid_amount,
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
  FROM invoice i
  INNER JOIN agent a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
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
      SELECT 1 FROM agent a_ref
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
  ) pay ON TRUE
  WHERE i.paid IS TRUE
    AND i.full_payment_date IS NOT NULL
    AND (
      EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
      OR EXTRACT(YEAR FROM i.full_payment_date)::int = {int(year)}
    )
    AND LOWER(btrim(COALESCE(a.agent_type, ''))) IN ({AGENT_TYPES_SQL})
    AND (COALESCE(i.percent_of_total_amount, 0) >= 1.0 OR i.paid IS TRUE)
    {customer_filter_sql}
)
SELECT
  invoice_number,
  invoice_date,
  full_payment_date,
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
ORDER BY agent_name ASC, full_payment_date ASC NULLS LAST, invoice_number ASC NULLS LAST
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
        
        prop_type = classify_property_type(row)
        
        total = _to_decimal(row.get("total_amount"))
        paid_amount = _to_decimal(row.get("paid_amount"))
        epp = _to_decimal(row.get("epp_interest"))
        sales_price = total - epp
        
        is_agent_senior = is_senior(agent_name)
        sharing = Decimal("0")
        
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
            from basic_commission_rates import get_basic_rate
            pay_date_parsed = _parse_invoice_date(row.get("full_payment_date"))
            pay_month = pay_date_parsed.month if pay_date_parsed else 5
            hierarchy = "Senior" if is_agent_senior else "Executive"
            rate = get_basic_rate("Internal", hierarchy, pay_month)
            comm = sales_price * rate
            
        inv_dt = str(row.get("invoice_date") or "")[:10]
        pay_dt = str(row.get("full_payment_date") or "")[:10]
        
        # Determine referral name: default to database values
        ref_name = row.get("referral_name")
        
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
            rate = _referral_rate(ln.invoice_date)
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
        filtered_rows = []
        for r in raw_rows:
            inv_dt = _parse_invoice_date(r.get("invoice_date"))
            pay_dt = _parse_invoice_date(r.get("full_payment_date"))
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
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission"
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
