#!/usr/bin/env python3
"""
Outsource Agent Basic Commission Report (2026) via Postgres read-only proxy.
Calculates basic commissions, overrides, and Factory commissions for outsource agents,
including interactive factory profit-sharing input.
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

def _load_dotenv() -> None:
    env_path = _SCRIPT_DIR / ".env"
    if not env_path.is_file():
        # check repo root .env
        env_path = _REPO_ROOT / ".env"
        if not env_path.is_file():
            return
    proxy_keys = frozenset({"PG_PROXY_TOKEN", "PG_PROXY_URL", "PG_PROXY_DB", "PG_DB_NAME"})
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

def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val

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

def _normalize_proxy_url(url: str | None) -> str | None:
    if not url:
        return url
    base = url.strip().rstrip("/")
    if base.endswith("/api/sql"):
        return base
    return f"{base}/api/sql"

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

    # Fallback to Residential if package_type is Residential
    if row.get("package_type") == "Residential":
        return "Residential"
        
    return "Shop Lot"

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()

def get_agent_hierarchy_info(agent_name: str) -> dict[str, Any] | None:
    n = _norm_name(agent_name)
    
    # 1. Check OSA 1 first
    if "hanis" in n and "marjian" in n:
        return {"canonical_name": "Mohd Hanis Bin Marjian", "tier": "OSA 1", "osa_parent": "Mohd Azhar Bin Ibrahim", "oum_parent": "Oliver Koh"}
    if "sue cherk" in n:
        return {"canonical_name": "Tan Sue Cherk", "tier": "OSA 1", "osa_parent": "Liew Lee Ching", "oum_parent": "Carol Siow"}
    if "kim swee" in n:
        return {"canonical_name": "Low Kim Swee", "tier": "OSA 1", "osa_parent": "Low Chin Chai", "oum_parent": "Carol Siow"}
    if "siong hing" in n:
        return {"canonical_name": "Lai Siong Hing", "tier": "OSA 1", "osa_parent": "Chang Soon Huat", "oum_parent": "Carol Siow"}
    if "ka kit" in n:
        return {"canonical_name": "Lai Ka Kit", "tier": "OSA 1", "osa_parent": "Loo Chew Yin", "oum_parent": None}

    # 2. Check OSAs under OUMs
    if "lam wai leng" in n:
        return {"canonical_name": "Lam Wai Leng", "tier": "OSA", "osa_parent": None, "oum_parent": "Dean Wai"}
    if "tee kok kian" in n:
        return {"canonical_name": "Tee Kok Kian", "tier": "OSA", "osa_parent": None, "oum_parent": "Dean Wai"}
    if "yeong cherng" in n:
        return {"canonical_name": "Koh Yeong Cherng", "tier": "OSA", "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "kok xing" in n:
        return {"canonical_name": "Ang Kok Xing", "tier": "OSA", "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "zhi yun" in n:
        return {"canonical_name": "Tey Zhi Yun", "tier": "OSA", "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "azhar" in n and "ibrahim" in n:
        return {"canonical_name": "Mohd Azhar Bin Ibrahim", "tier": "OSA", "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "chin seng" in n:
        return {"canonical_name": "Lim Chin Seng", "tier": "OSA", "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "jun sheng" in n:
        return {"canonical_name": "Kwong Jun Sheng", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "yue peng" in n:
        return {"canonical_name": "Lee Yue Peng", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "pok jen" in n:
        return {"canonical_name": "Too Pok Jen", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "hock xiang" in n:
        return {"canonical_name": "Tay Hock Xiang", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "wen lin" in n:
        return {"canonical_name": "Ho Wen Lin", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "zhee hao" in n:
        return {"canonical_name": "Ng Zhee Hao", "tier": "OSA", "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "wei hung" in n:
        return {"canonical_name": "Tan Wei Hung", "tier": "OSA", "osa_parent": None, "oum_parent": "Ling Liang Kang"}
    if "kai zhe" in n:
        return {"canonical_name": "Lim Kai Zhe", "tier": "OSA", "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "chun xun" in n:
        return {"canonical_name": "Lee Chun Xun", "tier": "OSA", "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "wei perng" in n:
        return {"canonical_name": "Ling Wei Perng", "tier": "OSA", "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "seok yun" in n:
        return {"canonical_name": "Lee Seok Yun", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}
    if "lee ching" in n:
        return {"canonical_name": "Liew Lee Ching", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}
    if "hui wen" in n:
        return {"canonical_name": "Lee Hui Wen", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}
    if "cheak ching" in n:
        return {"canonical_name": "See Cheak Ching", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}
    if "chin chai" in n:
        return {"canonical_name": "Low Chin Chai", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}
    if "soon huat" in n:
        return {"canonical_name": "Chang Soon Huat", "tier": "OSA", "osa_parent": None, "oum_parent": "Carol Siow"}

    # 3. Check OSA with Senior Internal
    if "kok tong" in n:
        return {"canonical_name": "Lim Kok Tong", "tier": "OSA", "osa_parent": None, "oum_parent": None, "internal_senior_parent": "Teng Kah Kent"}

    # 4. Check OUMs
    if "gan lai hock" in n:
        return {"canonical_name": "Gan Lai Hock", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "phil moo" in n or "wui kead" in n:
        return {"canonical_name": "Phil Moo", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "shao hong" in n:
        return {"canonical_name": "Kok Shao Hong", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "wilson tan" in n or ("tan" in n and "wei" in n and "sheng" in n):
        return {"canonical_name": "Wilson Tan", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "dean wai" in n or "leong yee" in n:
        return {"canonical_name": "Dean Wai", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "oliver koh" in n or "chong lee" in n:
        return {"canonical_name": "Oliver Koh", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "wing on" in n:
        return {"canonical_name": "Chan Wing On", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "jia wei" in n:
        return {"canonical_name": "Chan Jia Wei", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "liang kang" in n:
        return {"canonical_name": "Ling Liang Kang", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "caryn" in n or "leong mui" in n:
        return {"canonical_name": "Caryn Dong", "tier": "OUM", "osa_parent": None, "oum_parent": None}
    if "carol" in n or "sio chui" in n:
        return {"canonical_name": "Carol Siow", "tier": "OUM", "osa_parent": None, "oum_parent": None}

    # 5. Check OSA without OUM (Independent)
    if "cj loo" in n or "chew yin" in n:
        return {"canonical_name": "Loo Chew Yin", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "yun yun" in n:
        return {"canonical_name": "Kang Yun Yun", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "bling" in n:
        return {"canonical_name": "Bling", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "brendon" in n or "oliver" in n:
        return {"canonical_name": "Brendon Liew", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "lim gang" in n:
        return {"canonical_name": "Lim Gang", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "vin liew" in n:
        return {"canonical_name": "Vin Liew", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "brandon law" in n:
        return {"canonical_name": "Brandon Law", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "jason law" in n:
        return {"canonical_name": "Jason Law", "tier": "OSA", "osa_parent": None, "oum_parent": None}
    if "elaine" in n:
        return {"canonical_name": "Elaine Ng Yie Kie", "tier": "OSA", "osa_parent": None, "oum_parent": None}

    return None

def is_outsource_agent(agent_name: str, agent_type_field: str | None) -> bool:
    info = get_agent_hierarchy_info(agent_name)
    is_db_outsource = "outsource" in str(agent_type_field or "").lower()
    return (info is not None) or is_db_outsource

def get_own_commission_rate(info: dict[str, Any], agent_comm_field: int | None, invoice_date: Any = None) -> Decimal:
    from basic_commission_rates import get_basic_rate
    month = 5
    if invoice_date:
        parsed_dt = _parse_invoice_date(invoice_date)
        if parsed_dt:
            month = parsed_dt.month
            
    tier = info.get("tier") if info else "OSA/OSA1"
    if tier == "OUM":
        hierarchy = "OUM"
    elif tier == "OGM":
        hierarchy = "OGM"
    else:
        hierarchy = "OSA/OSA1"
        
    return get_basic_rate("Outsource", hierarchy, month)

def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt_row(headers), sep, *[fmt_row(r) for r in rows]])

def _invoices_sql(year: int) -> str:
    return f"""
WITH candidates AS (
  SELECT
    i.bubble_id,
    i.id AS invoice_row_id,
    i.is_latest,
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
    a.commission AS agent_comm_field,
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
    AND (COALESCE(i.percent_of_total_amount, 0) >= 1.0 OR i.paid IS TRUE)
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
  agent_comm_field,
  package_type,
  package_name_snapshot,
  description,
  seda_nem_type,
  referral_project_type,
  referral_name
FROM candidates
WHERE rn = 1
ORDER BY agent_name ASC, full_payment_date ASC NULLS LAST, invoice_number ASC NULLS LAST;
""".strip()

@dataclass
class OutsourceInvoiceLine:
    agent_name: str
    customer_name: str
    invoice_number: str
    package: str
    invoice_date: str
    full_payment_date: str
    total_amount: Decimal
    paid_amount: Decimal
    epp: Decimal
    sales_price: Decimal
    rate: Decimal
    basic_commission: Decimal
    referral_name: str | None = None
    gan_lai_soon: Decimal = Decimal("0")

@dataclass
class OutsourceFactoryInvoiceLine:
    agent_name: str
    customer_name: str
    invoice_number: str
    package: str
    invoice_date: str
    full_payment_date: str
    total_amount: Decimal
    paid_amount: Decimal
    epp: Decimal
    sales_price: Decimal
    rate: Decimal
    profit_sharing: Decimal
    basic_commission: Decimal
    referral_name: str | None = None
    gan_lai_soon: Decimal = Decimal("0")

def main(argv: list[str]) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Calculate outsource basic commission, overrides, and factory commission."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=None, help="Month to filter (1-12)")
    parser.add_argument("--profit-sharing", type=float, default=None, help="Default profit sharing %% for Factory (e.g. 5.0 for 5%%)")
    parser.add_argument(
        "--factory-rates",
        default=None,
        help="JSON string or comma-separated key:value pairs mapping invoice_number to profit sharing %% (e.g. '1007637:5.0,1008000:3.5')"
    )
    parser.add_argument(
        "--proxy-url",
        default=_env("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
    )
    parser.add_argument(
        "--db-name",
        default=_env("PG_PROXY_DB") or _env("PG_DB_NAME", "prod_main")
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Write PDF output"
    )
    args = parser.parse_args(argv)

    token = _env("PG_PROXY_TOKEN")
    if not token:
        print("Error: PG_PROXY_TOKEN not found in environment or .env files.", file=sys.stderr)
        return 2

    proxy_url = _normalize_proxy_url(args.proxy_url)
    try:
        payload = _proxy_sql(
            proxy_url=proxy_url,
            db_name=args.db_name,
            token=token,
            sql=_invoices_sql(args.year),
            params=[]
        )
    except Exception as e:
        print(f"Failed to fetch data from Postgres proxy: {e}", file=sys.stderr)
        return 1

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

    # Filter outsource Factory invoices for Table 3
    outsource_factory_rows = []
    for r in raw_rows:
        agent_name = str(r.get("agent_name") or "(unknown)").strip()
        agent_type = str(r.get("agent_type") or "").strip().lower()
        if is_outsource_agent(agent_name, agent_type):
            prop_type = classify_property_type(r)
            if prop_type == "Factory":
                outsource_factory_rows.append(r)

    # Parse CLI factory rates override if specified
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

    # Resolve rates for each Factory invoice
    factory_rates: dict[str, Decimal] = {}
    if outsource_factory_rows:
        default_profit_sharing = None
        if args.profit_sharing is not None:
            default_profit_sharing = Decimal(str(args.profit_sharing)) / Decimal("100")

        for i, r in enumerate(outsource_factory_rows, 1):
            inv_num = str(r.get("invoice_number") or "").strip()
            cust_name = str(r.get("customer_name") or "(unknown)").strip()
            total = _to_decimal(r.get("total_amount"))
            epp = _to_decimal(r.get("epp_interest"))
            sales_price = total - epp

            if inv_num in cli_factory_rates:
                factory_rates[inv_num] = cli_factory_rates[inv_num]
            elif default_profit_sharing is not None:
                factory_rates[inv_num] = default_profit_sharing
            else:
                prompt_label = f"INV: {inv_num} | Customer: {cust_name} | Sales Price: {_fmt_money(sales_price)}"
                while True:
                    try:
                        val = input(f"Insert Rate% for {prompt_label}: ").strip()
                        if not val:
                            rate_val = Decimal("0")
                        else:
                            rate_val = Decimal(val.replace("%", "").strip()) / Decimal("100")
                        factory_rates[inv_num] = rate_val
                        break
                    except (KeyboardInterrupt, EOFError):
                        print(f"\nInterrupted. Defaulting profit sharing for {inv_num} to 0%.")
                        factory_rates[inv_num] = Decimal("0")
                        break
                    except Exception as e:
                        print(f"Invalid input: {e}. Please enter a numeric value (e.g. 5 or 5%).")

    # Process Outsource Invoices
    processed_outsource_invoices: list[OutsourceInvoiceLine] = []
    processed_outsource_factory: list[OutsourceFactoryInvoiceLine] = []
    agent_own_commissions: dict[str, Decimal] = defaultdict(Decimal)
    agent_sales: dict[str, Decimal] = defaultdict(Decimal)
    override_commissions: dict[str, Decimal] = defaultdict(Decimal)

    for r in raw_rows:
        agent_name = str(r.get("agent_name") or "(unknown)").strip()
        agent_comm_field = r.get("agent_comm_field")
        
        info = get_agent_hierarchy_info(agent_name)
        is_db_outsource = "outsource" in str(r.get("agent_type") or "").lower()
        if not info and not is_db_outsource:
            continue
            
        if not info:
            info = {"canonical_name": agent_name, "tier": "OUM", "osa_parent": None, "oum_parent": None}
            
        canonical_name = info["canonical_name"]
        customer_name = str(r.get("customer_name") or "(unknown)").strip()
        invoice_num = str(r.get("invoice_number") or "").strip()
        prop_type = classify_property_type(r)
        
        total = _to_decimal(r.get("total_amount"))
        paid_amount = _to_decimal(r.get("paid_amount"))
        epp = _to_decimal(r.get("epp_interest"))
        sales_price = total - epp
        
        inv_dt = str(r.get("invoice_date") or "")[:10]
        pay_dt = str(r.get("full_payment_date") or "")[:10]
        
        # Determine referral name
        ref_name = r.get("referral_name")

        if prop_type == "Factory":
            rate = Decimal("0.02")
            sharing = factory_rates.get(invoice_num, Decimal("0"))
            
            osa_sharing = sharing
            if info["tier"] in ("OSA", "OSA 1") and sharing > 0:
                osa_sharing = sharing * Decimal("0.70")
                oum_p = info.get("oum_parent")
                if oum_p:
                    override_commissions[oum_p] += sales_price * sharing * Decimal("0.20")
                override_commissions["OGM Pool"] += sales_price * sharing * Decimal("0.10")
                
            # Gan Lai Soon OGM Override Commission
            tier = info["tier"]
            if tier in ("OSA", "OSA 1", "OUM"):
                ogm_rate = Decimal("0.0075")
            else:
                ogm_rate = Decimal("0")
            ogm_comm = sales_price * ogm_rate
            override_commissions["Gan Lai Soon"] += ogm_comm

            comm = sales_price * (rate + osa_sharing)
            
            processed_outsource_factory.append(
                OutsourceFactoryInvoiceLine(
                    agent_name=canonical_name,
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    paid_amount=paid_amount,
                    epp=epp,
                    sales_price=sales_price,
                    rate=rate,
                    profit_sharing=osa_sharing,
                    basic_commission=comm,
                    referral_name=ref_name,
                    gan_lai_soon=ogm_comm
                )
            )
            
            safwan_rate = Decimal("0.005")
            safwan_comm = sales_price * (safwan_rate + sharing)
            processed_outsource_factory.append(
                OutsourceFactoryInvoiceLine(
                    agent_name="Safwan",
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    paid_amount=paid_amount,
                    epp=epp,
                    sales_price=sales_price,
                    rate=safwan_rate,
                    profit_sharing=sharing,
                    basic_commission=safwan_comm,
                    referral_name=ref_name,
                    gan_lai_soon=Decimal("0")
                )
            )
            agent_sales["Safwan"] += total
            agent_own_commissions["Safwan"] += safwan_comm
        else:
            rate = get_own_commission_rate(info, agent_comm_field, pay_dt)
            comm = sales_price * rate
            
            # Gan Lai Soon OGM Override Commission
            tier = info["tier"]
            if tier in ("OSA", "OSA 1", "OUM"):
                ogm_rate = Decimal("0.0075")
            else:
                ogm_rate = Decimal("0")
            ogm_comm = sales_price * ogm_rate
            override_commissions["Gan Lai Soon"] += ogm_comm
 
            processed_outsource_invoices.append(
                OutsourceInvoiceLine(
                    agent_name=canonical_name,
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    paid_amount=paid_amount,
                    epp=epp,
                    sales_price=sales_price,
                    rate=rate,
                    basic_commission=comm,
                    referral_name=ref_name,
                    gan_lai_soon=ogm_comm
                )
            )
        
        # Accumulate sales and own commission
        agent_sales[canonical_name] += total
        agent_own_commissions[canonical_name] += comm
        
        # Calculate overrides on total_amount
        tier = info["tier"]
        if tier == "OSA 1":
            oum_p = info["oum_parent"]
            if oum_p:
                override_commissions[oum_p] += total * Decimal("0.005")
        elif tier == "OSA":
            oum_p = info["oum_parent"]
            internal_senior = info.get("internal_senior_parent")
            if oum_p:
                override_commissions[oum_p] += total * Decimal("0.005")
            if internal_senior:
                override_commissions[internal_senior] += total * Decimal("0.005")

    # ------------------ TABLE 1 ------------------
    # Final Commission Payout Summary by Agent
    all_payout_agents = set(agent_own_commissions.keys()) | set(override_commissions.keys())
    t1_rows = []
    for agent in sorted(all_payout_agents):
        own = agent_own_commissions.get(agent, Decimal("0"))
        ovr = override_commissions.get(agent, Decimal("0"))
        sales = agent_sales.get(agent, Decimal("0"))
        total_payout = own + ovr
        t1_rows.append([
            agent,
            _fmt_money(sales),
            _fmt_money(own),
            _fmt_money(ovr),
            _fmt_money(total_payout)
        ])

    # ------------------ TABLE 2 ------------------
    # Filter: Residential & Shop Lot for outsource agents
    # Sorted by Agent Name ascending, then by full_payment_date
    processed_outsource_invoices.sort(key=lambda x: (x.agent_name.lower(), x.full_payment_date))
    t2_rows = []
    for inv in processed_outsource_invoices:
        t2_rows.append([
            inv.agent_name,
            inv.customer_name,
            inv.invoice_number,
            inv.package,
            inv.invoice_date,
            inv.full_payment_date,
            _fmt_money(inv.total_amount),
            _fmt_money(inv.paid_amount),
            _fmt_money(inv.epp),
            _fmt_money(inv.sales_price),
            _fmt_rate(inv.rate),
            _fmt_money(inv.basic_commission),
            _fmt_money(inv.gan_lai_soon)
        ])

    # ------------------ TABLE 3 ------------------
    # Filter: Factory packages for outsource agents
    # Sorted by Agent Name ascending, then by full_payment_date
    processed_outsource_factory.sort(key=lambda x: (x.agent_name.lower(), x.full_payment_date))
    t3_rows = []
    for inv in processed_outsource_factory:
        t3_rows.append([
            inv.agent_name,
            inv.customer_name,
            inv.invoice_number,
            inv.package,
            inv.invoice_date,
            inv.full_payment_date,
            _fmt_money(inv.total_amount),
            _fmt_money(inv.paid_amount),
            _fmt_money(inv.epp),
            _fmt_money(inv.sales_price),
            _fmt_rate(inv.rate),
            _fmt_rate(inv.profit_sharing),
            _fmt_money(inv.basic_commission),
            _fmt_money(inv.gan_lai_soon)
        ])

    # ------------------ TABLE 4 ------------------
    # Referral Fee Table
    t4_rows = []
    for ln in processed_outsource_invoices + processed_outsource_factory:
        if _is_valid_referral(ln.referral_name):
            sales_price = ln.sales_price
            rate = _referral_rate(ln.invoice_date)
            fee = sales_price * rate
            t4_rows.append([
                ln.referral_name.strip(),
                ln.agent_name.strip(),
                ln.customer_name.strip(),
                ln.invoice_number.strip(),
                ln.invoice_date.strip(),
                ln.full_payment_date.strip(),
                _fmt_money(sales_price),
                _fmt_rate(rate),
                _fmt_money(fee)
            ])
    t4_rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))

    # Print to console
    print("\n=== Table 1: Final Commission Payout Summary by Agent ===")
    print(_render_table([
        "Agent_name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"
    ], t1_rows))
    print()

    print("=== Table 2: Accumulated Basic Commission by Customer (Residential & Shop Lot) ===")
    print(_render_table([
        "Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
        "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Gan Lai Soon (RM)"
    ], t2_rows))
    print()

    print("=== Table 3: Accumulated Basic Commission by Customer (Factory) ===")
    print(_render_table([
        "Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
        "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Profit Sharing", "Basic Commission", "Gan Lai Soon (RM)"
    ], t3_rows))
    print()

    print("=== Table 4: Referral Fee ===")
    print(_render_table([
        "Referral Name", "Agent Name", "Customer Name", "Invoice Number", "Invoice Date", "Full Payment Date", "Sales Price", "Rate %", "Referral Fee"
    ], t4_rows))
    print()

    # Save to CSV
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    csv_t1 = _OUTPUT_DIR / f"outsource_basic_commission_payout_summary_{args.year}_{stamp}.csv"
    with open(csv_t1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Agent_name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"])
        w.writerows(t1_rows)
        
    csv_t2 = _OUTPUT_DIR / f"outsource_basic_commission_residential_shoplot_{args.year}_{stamp}.csv"
    with open(csv_t2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Gan Lai Soon (RM)"
        ])
        w.writerows(t2_rows)
        
    csv_t3 = _OUTPUT_DIR / f"outsource_basic_commission_factory_{args.year}_{stamp}.csv"
    with open(csv_t3, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date",
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Profit Sharing", "Basic Commission", "Gan Lai Soon (RM)"
        ])
        w.writerows(t3_rows)

    if t4_rows:
        csv_t4 = _OUTPUT_DIR / f"outsource_basic_commission_referral_{args.year}_{stamp}.csv"
        with open(csv_t4, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Referral Name", "Agent Name", "Customer Name", "Invoice Number", "Invoice Date", "Full Payment Date", "Sales Price", "Rate %", "Referral Fee"])
            w.writerows(t4_rows)
        
    print(f"CSV files saved to {_OUTPUT_DIR.resolve()}")

    # Save to PDF
    if args.pdf:
        pdf_path = _OUTPUT_DIR / f"outsource_basic_commission_{args.year}_{stamp}.pdf"
        try:
            PdfSection, write_commission_pdf = _import_pdf_writer()
            meta = [
                ("Year (Full Payment)", str(args.year)),
                ("Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ]
            sections = [
                PdfSection(
                    title="Table 1: Final Commission Payout Summary by Agent",
                    headers=["Agent_name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"],
                    rows=t1_rows
                ),
                PdfSection(
                    title="Table 2: Accumulated Basic Commission by Customer (Residential & Shop Lot)",
                    headers=["Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date", "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Gan Lai Soon (RM)"],
                    rows=t2_rows,
                    landscape=True
                ),
                PdfSection(
                    title="Table 3: Accumulated Basic Commission by Customer (Factory)",
                    headers=["Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date", "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Profit Sharing", "Basic Commission", "Gan Lai Soon (RM)"],
                    rows=t3_rows,
                    landscape=True
                ),
                PdfSection(
                    title="Table 4: Referral Fee",
                    headers=["Referral Name", "Agent Name", "Customer Name", "Invoice Number", "Invoice Date", "Full Payment Date", "Sales Price", "Rate %", "Referral Fee"],
                    rows=t4_rows if t4_rows else [["No referral fee", "-", "-", "-", "-", "-", "-", "-", "-"]],
                )
            ]
            write_commission_pdf(
                pdf_path,
                title="Outsource Basic Commission Report",
                meta_lines=meta,
                sections=sections
            )
            print(f"PDF saved: {pdf_path.resolve()}")
        except Exception as e:
            print(f"Failed to generate PDF: {e}", file=sys.stderr)

    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
