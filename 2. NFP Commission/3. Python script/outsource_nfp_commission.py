#!/usr/bin/env python3
"""
NFP (Net Floor Price) Commission Report – Outsource Agents.

Filters:
  - invoice.paid IS TRUE
  - invoice.full_payment_date year = report year (default 2026)
  - COALESCE(percent_of_total_amount, 0) >= 100.0  (fully paid)
  - agent is an outsource agent (agent_type contains 'outsource'
    OR agent name matches the known outsource hierarchy)
  - COALESCE(is_deleted, FALSE) IS NOT TRUE

Commission formula (same as internal NFP):
  Sales Price    = total_amount - EPP cost
  System Price   = main package line unit_price
  Commission a   = (Sales Price - NFP) * 25%  when Sales Price > NFP
  Commission b   = (System Price - NFP) * 100% (audit only, not summed)
  Commission c   = (NFP - Sales Price) * 20%   when Sales Price < NFP
  NFP Commission = a - c

Net Floor Price source:
  - invoice_date before Oct 2025 → no NFP
  - Oct–Dec 2025 + 620W panels   → data/nfp_620w_schedule.json
  - 650W panels                  → monthly sheet in 1. Excel/STRING 650W package...xlsx
  - TNG rebate on invoice        → use FINAL PRICE AFTER DISCOUNT (WITH TNG REBATE) column

Tables printed / saved:
  Table 1 – Final Commission Payout Summary by agent
             Headers: Agent_name, Total Commission Payout

  Table 2 – Accumulated NFP Commission by Customer
             Headers: Agent Name, Customer Name, Invoice Number, Invoice Date,
                      Sales Price, System Price, Net Floor Price, NFP Commission,
                      Accumulated NFP Commission

Usage (run from "3. Python script" folder):
  python outsource_nfp_commission.py
  python outsource_nfp_commission.py --year 2026
  python outsource_nfp_commission.py --no-pdf
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

from api_client import query_sql, test_connection
from nfp_paths import ensure_output_dir, ensure_reports_dir
from net_floor_prices import (
    NFP_CUTOFF,
    infer_panel_qty_from_text,
    infer_panel_rating_from_text,
    load_620w_schedule,
    load_650w_schedules,
    lookup_net_floor_price,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
REPORTS_DIR = ensure_reports_dir()
OUTPUT_DIR = ensure_output_dir()

# ---------------------------------------------------------------------------
# Outsource agent recognition
# Mirrors the logic in Basic Commission/outsource_basic_commission.py so both
# reports always cover the exact same set of agents.
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def get_agent_hierarchy_info(agent_name: str) -> dict[str, Any] | None:
    """Return hierarchy metadata for a known outsource agent, or None."""
    n = _norm_name(agent_name)

    # OSA 1
    if "hanis" in n and "marjian" in n:
        return {"canonical_name": "Mohd Hanis Bin Marjian", "tier": "OSA 1",
                "osa_parent": "Mohd Azhar Bin Ibrahim", "oum_parent": "Oliver Koh"}
    if "sue cherk" in n:
        return {"canonical_name": "Tan Sue Cherk", "tier": "OSA 1",
                "osa_parent": "Liew Lee Ching", "oum_parent": "Carol Siow"}
    if "kim swee" in n:
        return {"canonical_name": "Low Kim Swee", "tier": "OSA 1",
                "osa_parent": "Low Chin Chai", "oum_parent": "Carol Siow"}
    if "siong hing" in n:
        return {"canonical_name": "Lai Siong Hing", "tier": "OSA 1",
                "osa_parent": "Chang Soon Huat", "oum_parent": "Carol Siow"}
    if "ka kit" in n:
        return {"canonical_name": "Lai Ka Kit", "tier": "OSA 1",
                "osa_parent": "Loo Chew Yin", "oum_parent": None}

    # OSAs under OUMs
    if "lam wai leng" in n:
        return {"canonical_name": "Lam Wai Leng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Dean Wai"}
    if "tee kok kian" in n:
        return {"canonical_name": "Tee Kok Kian", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Dean Wai"}
    if "yeong cherng" in n:
        return {"canonical_name": "Koh Yeong Cherng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "kok xing" in n:
        return {"canonical_name": "Ang Kok Xing", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "zhi yun" in n:
        return {"canonical_name": "Tey Zhi Yun", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "azhar" in n and "ibrahim" in n:
        return {"canonical_name": "Mohd Azhar Bin Ibrahim", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "chin seng" in n:
        return {"canonical_name": "Lim Chin Seng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Oliver Koh"}
    if "jun sheng" in n:
        return {"canonical_name": "Kwong Jun Sheng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "yue peng" in n:
        return {"canonical_name": "Lee Yue Peng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "pok jen" in n:
        return {"canonical_name": "Too Pok Jen", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Wing On"}
    if "hock xiang" in n:
        return {"canonical_name": "Tay Hock Xiang", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "wen lin" in n:
        return {"canonical_name": "Ho Wen Lin", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "zhee hao" in n:
        return {"canonical_name": "Ng Zhee Hao", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Chan Jia Wei"}
    if "wei hung" in n:
        return {"canonical_name": "Tan Wei Hung", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Ling Liang Kang"}
    if "kai zhe" in n:
        return {"canonical_name": "Lim Kai Zhe", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "chun xun" in n:
        return {"canonical_name": "Lee Chun Xun", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "wei perng" in n:
        return {"canonical_name": "Ling Wei Perng", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Caryn Dong"}
    if "seok yun" in n:
        return {"canonical_name": "Lee Seok Yun", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}
    if "lee ching" in n:
        return {"canonical_name": "Liew Lee Ching", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}
    if "hui wen" in n:
        return {"canonical_name": "Lee Hui Wen", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}
    if "cheak ching" in n:
        return {"canonical_name": "See Cheak Ching", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}
    if "chin chai" in n:
        return {"canonical_name": "Low Chin Chai", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}
    if "soon huat" in n:
        return {"canonical_name": "Chang Soon Huat", "tier": "OSA",
                "osa_parent": None, "oum_parent": "Carol Siow"}

    # OSA with Senior Internal
    if "kok tong" in n:
        return {"canonical_name": "Lim Kok Tong", "tier": "OSA",
                "osa_parent": None, "oum_parent": None,
                "internal_senior_parent": "Teng Kah Kent"}

    # OUMs
    if "gan lai hock" in n:
        return {"canonical_name": "Gan Lai Hock", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "phil moo" in n or "wui kead" in n:
        return {"canonical_name": "Phil Moo", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "shao hong" in n:
        return {"canonical_name": "Kok Shao Hong", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "wilson tan" in n or ("tan" in n and "wei" in n and "sheng" in n):
        return {"canonical_name": "Wilson Tan", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "dean wai" in n or "leong yee" in n:
        return {"canonical_name": "Dean Wai", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "oliver koh" in n or "chong lee" in n:
        return {"canonical_name": "Oliver Koh", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "wing on" in n:
        return {"canonical_name": "Chan Wing On", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "jia wei" in n:
        return {"canonical_name": "Chan Jia Wei", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "liang kang" in n:
        return {"canonical_name": "Ling Liang Kang", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "caryn" in n or "leong mui" in n:
        return {"canonical_name": "Caryn Dong", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}
    if "carol" in n or "sio chui" in n:
        return {"canonical_name": "Carol Siow", "tier": "OUM",
                "osa_parent": None, "oum_parent": None}

    # Independent OSAs
    if "cj loo" in n or "chew yin" in n:
        return {"canonical_name": "Loo Chew Yin", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "yun yun" in n:
        return {"canonical_name": "Kang Yun Yun", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "bling" in n:
        return {"canonical_name": "Bling", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "brendon" in n or "oliver" in n:
        return {"canonical_name": "Brendon Liew", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "lim gang" in n:
        return {"canonical_name": "Lim Gang", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "vin liew" in n:
        return {"canonical_name": "Vin Liew", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "brandon law" in n:
        return {"canonical_name": "Brandon Law", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "jason law" in n:
        return {"canonical_name": "Jason Law", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}
    if "elaine" in n:
        return {"canonical_name": "Elaine Ng Yie Kie", "tier": "OSA",
                "osa_parent": None, "oum_parent": None}

    return None


def is_outsource_agent(agent_name: str, agent_type_field: str | None) -> bool:
    """Return True if this agent is an outsource agent (name-match OR DB field)."""
    info = get_agent_hierarchy_info(agent_name)
    is_db_outsource = "outsource" in str(agent_type_field or "").lower()
    return (info is not None) or is_db_outsource


# ---------------------------------------------------------------------------
# SQL – fetch all fully-paid 2026 invoices (no agent_type filter in SQL;
# outsource filtering is done in Python via is_outsource_agent()).
# ---------------------------------------------------------------------------
INVOICES_SQL = """
WITH candidates AS (
    SELECT
        i.bubble_id,
        i.invoice_number,
        i.invoice_date,
        i.full_payment_date,
        i.total_amount,
        i.effective_epp,
        i.panel_qty,
        i.panel_rating,
        i.is_latest,
        i.id AS invoice_row_id,
        COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name) AS customer_name,
        a.name AS agent_name,
        a.agent_type,
        COALESCE(sr_link.phase_type, sr_back.phase_type) AS phase_type,
        COALESCE(epp_items.epp_cost, 0) AS line_epp_cost,
        COALESCE(pay.epp_sum, 0) AS payment_epp_sum,
        pkg.system_price,
        pkg.package_description,
        pkg.db_net_floor_price,
        items.all_item_text,
        COALESCE(tng.has_tng, FALSE) AS has_tng_rebate,
        tng.tng_evidence,
        COALESCE(NULLIF(TRIM(i.invoice_number), ''), i.bubble_id) AS invoice_key
    FROM invoice i
    INNER JOIN agent a ON a.bubble_id = i.linked_agent
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    LEFT JOIN seda_registration sr_link
        ON sr_link.bubble_id = i.linked_seda_registration
    LEFT JOIN seda_registration sr_back
        ON i.bubble_id = ANY(sr_back.linked_invoice)
    LEFT JOIN LATERAL (
        SELECT COALESCE(
            NULLIF(SUM(CASE WHEN COALESCE(ii_dedup.epp_val, 0) > 0 THEN ii_dedup.epp_val ELSE 0 END), 0),
            SUM(ii_dedup.epp_interest_amount),
            0
        ) AS epp_cost
        FROM (
            SELECT 
                MAX(COALESCE(ii.epp, 0)) AS epp_val,
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
    LEFT JOIN LATERAL (
        SELECT
            COALESCE(NULLIF(ii.unit_price, 0), ii.amount, 0) AS system_price,
            ii.description AS package_description,
            p.nett_price AS db_net_floor_price
        FROM invoice_item ii
        LEFT JOIN package p ON p.bubble_id = ii.linked_package
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
        ORDER BY
            CASE WHEN ii.is_a_package IS TRUE THEN 0 ELSE 1 END,
            COALESCE(NULLIF(ii.unit_price, 0), ii.amount, 0) DESC,
            ii.id
        LIMIT 1
    ) pkg ON TRUE
    LEFT JOIN LATERAL (
        SELECT string_agg(COALESCE(ii.description, ''), ' | ') AS all_item_text
        FROM invoice_item ii
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
    ) items ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            bool_or(
                COALESCE(ii.description, '') ILIKE '%%tng%%'
                OR COALESCE(ii.description, '') ILIKE '%%touch n go%%'
                OR COALESCE(ii.description, '') ILIKE '%%touch''n go%%'
                OR COALESCE(ii.description, '') ILIKE '%%swap tng%%'
                OR (
                    COALESCE(ii.description, '') ILIKE '%%road show%%'
                    AND COALESCE(ii.description, '') ILIKE '%%tng%%'
                )
                OR COALESCE(ii.voucher_remark, '') ILIKE '%%tng%%'
                OR COALESCE(ii.inv_item_type, '') ILIKE '%%tng%%'
                OR COALESCE(v.title, '') ILIKE '%%tng%%'
                OR COALESCE(v.invoice_description, '') ILIKE '%%tng%%'
            ) AS has_tng,
            NULLIF(
                string_agg(
                    DISTINCT TRIM(COALESCE(ii.description, v.title, '')),
                    ' | '
                ) FILTER (
                    WHERE COALESCE(ii.description, '') ILIKE '%%tng%%'
                       OR COALESCE(ii.description, '') ILIKE '%%swap tng%%'
                       OR (
                           COALESCE(ii.description, '') ILIKE '%%road show%%'
                           AND COALESCE(ii.description, '') ILIKE '%%tng%%'
                       )
                       OR COALESCE(v.title, '') ILIKE '%%tng%%'
                ),
                ''
            ) AS tng_evidence
        FROM invoice_item ii
        LEFT JOIN voucher v ON v.bubble_id = ii.linked_voucher
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
    ) tng ON TRUE
    WHERE i.paid IS TRUE
      AND i.full_payment_date IS NOT NULL
      AND EXTRACT(YEAR FROM i.full_payment_date) = {year}
      AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
      AND (COALESCE(i.percent_of_total_amount, 0) >= 1.0 OR i.paid IS TRUE)
),
epp_once AS (
    SELECT
        bubble_id,
        COALESCE(
            NULLIF(MAX(CASE WHEN line_epp_cost > 0 THEN line_epp_cost END), NULL),
            NULLIF(MAX(CASE WHEN payment_epp_sum > 0 THEN payment_epp_sum END), NULL),
            NULLIF(
                MAX(
                    CASE
                        WHEN effective_epp > 1.0 AND effective_epp < 2.0
                        THEN (total_amount * (effective_epp - 1.0) / effective_epp)
                        WHEN effective_epp >= 2.0 AND effective_epp <= 100.0
                        THEN (total_amount * (effective_epp / 100.0) / (1.0 + effective_epp / 100.0))
                        ELSE NULL
                    END
                ),
                NULL
            ),
            0
        ) AS epp_cost
    FROM candidates
    GROUP BY bubble_id
),
ranked AS (
    SELECT
        c.*,
        ROW_NUMBER() OVER (
            PARTITION BY c.bubble_id
            ORDER BY COALESCE(c.is_latest, FALSE) DESC,
                     c.full_payment_date DESC NULLS LAST,
                     c.invoice_row_id DESC
        ) AS rn
    FROM candidates c
)
SELECT
    r.bubble_id,
    r.invoice_number,
    r.invoice_date,
    r.full_payment_date,
    r.total_amount,
    e.epp_cost,
    r.panel_qty,
    r.panel_rating,
    r.customer_name,
    r.agent_name,
    r.agent_type,
    r.phase_type,
    r.system_price,
    r.package_description,
    r.db_net_floor_price,
    r.all_item_text,
    r.has_tng_rebate,
    r.tng_evidence
FROM ranked r
INNER JOIN epp_once e ON e.bubble_id = r.bubble_id
WHERE r.rn = 1
ORDER BY r.agent_name, r.invoice_date, r.invoice_number
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def _fmt_rm(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value):,.2f}"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class OutsourceNfpLine:
    agent_name: str
    agent_type: str
    customer_name: str
    invoice_number: str
    invoice_date: Optional[str]
    full_payment_date: Optional[str]
    total_amount: float
    epp_cost: float
    sales_price: float
    system_price: float
    panel_qty: Optional[int]
    panel_rating: Optional[int]
    phase_type: Optional[str]
    has_tng_rebate: bool
    tng_rebate: str
    tng_evidence: Optional[str]
    net_floor_price: Optional[float]
    nfp_source: str
    commission_a: float
    commission_b: float
    commission_c: float
    nfp_commission: float


# ---------------------------------------------------------------------------
# Commission calculation (identical to internal NFP)
# ---------------------------------------------------------------------------

def calc_commission(
    sales_price: Decimal,
    system_price: Decimal,
    net_floor: Optional[Decimal],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if net_floor is None:
        return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")

    a = Decimal("0")
    b = Decimal("0")
    c = Decimal("0")

    if sales_price > net_floor:
        a = (sales_price - net_floor) * Decimal("0.25")
    if system_price > net_floor:
        b = (system_price - net_floor) * Decimal("1.00")
    if sales_price < net_floor:
        c = (net_floor - sales_price) * Decimal("0.20")

    nfp_total = a - c
    return a, b, c, nfp_total


def resolve_panels(row: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    qty = row.get("panel_qty")
    rating = row.get("panel_rating")
    panel_qty = int(qty) if qty is not None else None
    panel_rating = int(rating) if rating is not None else None

    text_blob = " | ".join(
        filter(None, [row.get("package_description"), row.get("all_item_text")])
    )
    if panel_qty is None:
        panel_qty = infer_panel_qty_from_text(text_blob)
    if panel_rating is None:
        panel_rating = infer_panel_rating_from_text(text_blob)

    inv_date = parse_date(row.get("invoice_date"))
    if inv_date and inv_date >= date(2026, 2, 1):
        panel_rating = 650
    elif panel_rating is None and inv_date and inv_date >= NFP_CUTOFF:
        panel_rating = 650

    return panel_qty, panel_rating


def is_three_phase_from_seda(phase_type: Optional[str]) -> bool:
    if not phase_type or not str(phase_type).strip():
        return False
    t = str(phase_type).lower().strip()
    if "single" in t:
        return False
    if t in ("1", "1 phase"):
        return False
    if t in ("3", "3 phase"):
        return True
    if "three" in t or "3 phase" in t:
        return True
    return False


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(year: int, month: Optional[int] = None) -> tuple[List[OutsourceNfpLine], Dict[str, Any]]:
    sql = INVOICES_SQL.format(year=year)
    all_rows = query_sql(sql)
    if month is not None:
        all_rows = [r for r in all_rows if parse_date(r.get("full_payment_date")) and parse_date(r.get("full_payment_date")).month == month]

    schedules_650 = load_650w_schedules()
    schedule_620 = load_620w_schedule()

    results: List[OutsourceNfpLine] = []
    skipped_not_outsource = 0

    for row in all_rows:
        raw_agent_name = (row.get("agent_name") or "").strip()
        agent_type_field = row.get("agent_type") or ""

        # Only keep outsource agents
        info = get_agent_hierarchy_info(raw_agent_name)
        is_db_outsource = "outsource" in str(agent_type_field).lower()
        if not info and not is_db_outsource:
            skipped_not_outsource += 1
            continue

        # Use canonical name if available
        agent_name = info["canonical_name"] if info else raw_agent_name

        total = to_decimal(row.get("total_amount"))
        epp = to_decimal(row.get("epp_cost"))
        sales = money(total - epp)
        system = money(to_decimal(row.get("system_price")))
        db_nfp = to_decimal(row.get("db_net_floor_price"))

        panel_qty, panel_rating = resolve_panels(row)
        inv_date = parse_date(row.get("invoice_date"))
        has_tng = bool(row.get("has_tng_rebate"))
        tng_evidence = (row.get("tng_evidence") or "").strip() or None
        three_phase = is_three_phase_from_seda(row.get("phase_type"))

        nfp_value: Optional[Decimal] = None
        nfp_source = "n/a"
        if inv_date and inv_date < NFP_CUTOFF:
            nfp_source = "before_oct_2025_no_nfp"
        elif inv_date and panel_qty:
            nfp_raw, fallback_source = lookup_net_floor_price(
                inv_date,
                panel_qty,
                panel_rating or 650,
                has_tng,
                schedules_650,
                schedule_620,
                three_phase=three_phase,
            )
            if nfp_raw is not None:
                nfp_value = money(Decimal(str(nfp_raw)))
                nfp_source = f"Excel fallback ({fallback_source})"
            elif db_nfp > 0:
                nfp_value = money(db_nfp)
                nfp_source = "package.nett_price (fallback)"
            else:
                nfp_source = "missing_in_excel"
        elif db_nfp > 0:
            nfp_value = money(db_nfp)
            nfp_source = "package.nett_price"
        else:
            nfp_source = "missing_panel_qty_or_rating"

        a, b, c, total_comm = calc_commission(sales, system, nfp_value)

        results.append(
            OutsourceNfpLine(
                agent_name=agent_name,
                agent_type=agent_type_field,
                customer_name=(row.get("customer_name") or "").strip(),
                invoice_number=str(row.get("invoice_number") or ""),
                invoice_date=str(row.get("invoice_date") or "")[:10] or None,
                full_payment_date=str(row.get("full_payment_date") or "")[:10] or None,
                total_amount=float(money(total)),
                epp_cost=float(money(epp)),
                sales_price=float(sales),
                system_price=float(system),
                panel_qty=panel_qty,
                panel_rating=panel_rating,
                phase_type=row.get("phase_type"),
                has_tng_rebate=has_tng,
                tng_rebate="Yes" if has_tng else "No",
                tng_evidence=tng_evidence,
                net_floor_price=float(nfp_value) if nfp_value is not None else None,
                nfp_source=nfp_source,
                commission_a=float(money(a)),
                commission_b=float(money(b)),
                commission_c=float(money(c)),
                nfp_commission=float(money(total_comm)),
            )
        )

    # Accumulate per-agent totals
    agents = {r.agent_name for r in results if r.agent_name}
    accumulated: Dict[str, Dict[str, float]] = {}
    for r in results:
        if not r.agent_name:
            continue
        if r.agent_name not in accumulated:
            accumulated[r.agent_name] = {
                "sales_price": 0.0,
                "system_price": 0.0,
                "net_floor_price": 0.0,
                "nfp_commission": 0.0,
            }
        acc = accumulated[r.agent_name]
        acc["sales_price"] += r.sales_price
        acc["system_price"] += r.system_price
        acc["net_floor_price"] += r.net_floor_price or 0.0
        acc["nfp_commission"] += r.nfp_commission

    tng_invoices = [r for r in results if r.has_tng_rebate]

    summary = {
        "report_year": year,
        "report_month": month,
        "agent_filter": "Outsource",
        "total_qualifying_agents": len(agents),
        "total_invoices": len(results),
        "invoices_with_tng_rebate": len(tng_invoices),
        "total_nfp_commission": round(sum(r.nfp_commission for r in results), 2),
        "accumulated_by_agent": {
            agent: {k: round(v, 2) for k, v in totals.items()}
            for agent, totals in sorted(accumulated.items())
        },
        "tng_invoices": [
            {
                "invoice_number": r.invoice_number,
                "customer_name": r.customer_name,
                "agent_name": r.agent_name,
                "net_floor_price": r.net_floor_price,
                "nfp_source": r.nfp_source,
                "tng_evidence": r.tng_evidence,
            }
            for r in tng_invoices
        ],
        "_debug_skipped_not_outsource": skipped_not_outsource,
    }
    return results, summary


# ---------------------------------------------------------------------------
# Table headers
# ---------------------------------------------------------------------------

TABLE1_HEADERS = [
    "Agent_name",
    "Total Commission Payout",
]

TABLE2_HEADERS = [
    "Agent Name",
    "Customer Name",
    "Invoice Number",
    "Invoice Date",
    "Sales Price",
    "System Price",
    "Net Floor Price",
    "NFP Commission",
    "Accumulated NFP Commission",
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_grid_table(headers: List[str], rows: List[List[str]]) -> str:
    if not headers:
        return ""
    col_count = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(col_count):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], len(str(cell)))

    def pad(text: str, width: int) -> str:
        return str(text).ljust(width)

    def border(sep: str = "+") -> str:
        return sep + sep.join("-" * (w + 2) for w in widths) + sep

    def data_row(cells: List[str]) -> str:
        parts = [pad(cells[i] if i < len(cells) else "", widths[i]) for i in range(col_count)]
        return "| " + " | ".join(parts) + " |"

    lines = [border(), data_row(headers), border()]
    for row in rows:
        lines.append(data_row(row))
    lines.append(border())
    return "\n".join(lines)


def _tabulate_safe(headers: List[str], rows: List[List[str]], *, headers_first_row: bool = True) -> str:
    try:
        from tabulate import tabulate
        if headers_first_row:
            dict_rows = [dict(zip(headers, row)) for row in rows]
            return tabulate(dict_rows, headers="keys", tablefmt="grid", showindex=False)
        return tabulate(rows, headers=headers, tablefmt="grid")
    except ImportError:
        return format_grid_table(headers, rows)


def _agent_accumulated_nfp(summary: Dict[str, Any], agent_name: str) -> float:
    acc = summary.get("accumulated_by_agent", {}).get(agent_name, {})
    if isinstance(acc, dict):
        return float(acc.get("nfp_commission", 0))
    return float(acc or 0)


def build_display_rows(
    rows: List[OutsourceNfpLine], summary: Dict[str, Any]
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in rows:
        d = asdict(r)
        out.append(
            {
                "Agent Name": d["agent_name"],
                "Customer Name": d["customer_name"],
                "Invoice Number": d["invoice_number"],
                "Invoice Date": d["invoice_date"] or "-",
                "Sales Price": _fmt_rm(d["sales_price"]),
                "System Price": _fmt_rm(d["system_price"]),
                "Net Floor Price": _fmt_rm(d["net_floor_price"]),
                "NFP Commission": _fmt_rm(d["nfp_commission"]),
                "Accumulated NFP Commission": _fmt_rm(
                    _agent_accumulated_nfp(summary, d["agent_name"])
                ),
            }
        )
    return out


def display_rows_as_lists(
    rows: List[OutsourceNfpLine], summary: Dict[str, Any]
) -> List[List[str]]:
    return [[d[h] for h in TABLE2_HEADERS] for d in build_display_rows(rows, summary)]


def build_agent_summary_table(summary: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for agent, totals in sorted(summary.get("accumulated_by_agent", {}).items()):
        if isinstance(totals, dict):
            rows.append([agent, _fmt_rm(totals.get("nfp_commission"))])
        else:
            rows.append([agent, _fmt_rm(totals)])
    return rows


# ---------------------------------------------------------------------------
# Render (text / HTML)
# ---------------------------------------------------------------------------

def render_tables(rows: List[OutsourceNfpLine], summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("OUTSOURCE NFP COMMISSION REPORT")
    lines.append("=" * 80)
    lines.append("")

    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Outsource"],
        ["Filter: Full payment period", f"{summary['report_year']}{month_str}"],
        ["Total qualifying agents (users)", str(summary["total_qualifying_agents"])],
        ["Total invoices", str(summary["total_invoices"])],
        ["Invoices with TNG rebate", str(summary.get("invoices_with_tng_rebate", 0))],
        ["Total NFP commission (RM)", _fmt_rm(summary["total_nfp_commission"])],
    ]
    lines.append("SUMMARY")
    lines.append(_tabulate_safe(["Metric", "Value"], summary_info, headers_first_row=False))
    lines.append("")

    tng_rows = summary.get("tng_invoices") or []
    if tng_rows:
        lines.append("INVOICES WITH TNG REBATE (use net floor WITH TNG column)")
        tng_table = [
            [
                t["invoice_number"],
                t["customer_name"][:40],
                "Yes",
                _fmt_rm(t.get("net_floor_price")),
                t.get("nfp_source", ""),
            ]
            for t in tng_rows
        ]
        lines.append(
            _tabulate_safe(
                ["Invoice No.", "Customer", "TNG", "Net Floor (RM)", "NFP source"],
                tng_table,
                headers_first_row=False,
            )
        )
        lines.append("")

    agent_rows = build_agent_summary_table(summary)
    if agent_rows:
        lines.append("Table 1: Final Commission Payout Summary by agent")
        lines.append(_tabulate_safe(TABLE1_HEADERS, agent_rows, headers_first_row=False))
        lines.append("")

    detail_rows = display_rows_as_lists(rows, summary)
    if detail_rows:
        lines.append("Table 2: Accumulated NFP Commission by Customer")
        lines.append(_tabulate_safe(TABLE2_HEADERS, detail_rows, headers_first_row=False))

    return "\n".join(lines)


def render_html_table(rows: List[OutsourceNfpLine], summary: Dict[str, Any]) -> str:
    def html_grid(title: str, headers: List[str], data: List[List[str]]) -> str:
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in data
        )
        return f"<h2>{title}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Outsource"],
        ["Filter: Full payment period", f"{summary['report_year']}{month_str}"],
        ["Total qualifying agents (users)", str(summary["total_qualifying_agents"])],
        ["Total invoices", str(summary["total_invoices"])],
        ["Total NFP commission (RM)", _fmt_rm(summary["total_nfp_commission"])],
    ]
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Outsource NFP Commission Report</title>",
        "<style>table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;font-size:13px}"
        "th{background:#f0f0f0}h1,h2{font-family:sans-serif}</style></head><body>",
        "<h1>Outsource NFP Commission Report</h1>",
        html_grid("Summary", ["Metric", "Value"], summary_info),
        html_grid(
            "Table 1: Final Commission Payout Summary by agent",
            TABLE1_HEADERS,
            build_agent_summary_table(summary),
        ),
        html_grid(
            "Table 2: Accumulated NFP Commission by Customer",
            TABLE2_HEADERS,
            display_rows_as_lists(rows, summary),
        ),
        "</body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def write_table_file(path: Path, rows: List[OutsourceNfpLine], summary: Dict[str, Any]) -> None:
    path.write_text(render_tables(rows, summary), encoding="utf-8")


def write_table_csv(path: Path, rows: List[OutsourceNfpLine], summary: Dict[str, Any]) -> None:
    import csv
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TABLE2_HEADERS)
        w.writeheader()
        for row in build_display_rows(rows, summary):
            w.writerow(row)


def write_tng_audit_csv(path: Path, rows: List[OutsourceNfpLine]) -> None:
    import csv
    fields = [
        "invoice_number", "customer_name", "agent_name",
        "tng_rebate", "tng_evidence", "net_floor_price", "nfp_source",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "invoice_number": r.invoice_number,
                "customer_name": r.customer_name,
                "agent_name": r.agent_name,
                "tng_rebate": r.tng_rebate,
                "tng_evidence": r.tng_evidence or "",
                "net_floor_price": r.net_floor_price if r.net_floor_price is not None else "",
                "nfp_source": r.nfp_source,
            })


def write_full_csv(path: Path, rows: List[OutsourceNfpLine], summary: Dict[str, Any]) -> None:
    import csv
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    extra = ["agent_accumulated_nfp_commission"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames + extra)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["agent_accumulated_nfp_commission"] = _agent_accumulated_nfp(summary, r.agent_name)
            w.writerow(d)


def _import_pdf_writer():
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from commission_pdf import PdfSection, write_commission_pdf
    return PdfSection, write_commission_pdf


def write_outsource_nfp_pdf(
    path: Path, rows: List[OutsourceNfpLine], summary: Dict[str, Any]
) -> Path:
    PdfSection, write_commission_pdf = _import_pdf_writer()
    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ("Filter: Paid", "TRUE"),
        ("Filter: Agent Type", "Outsource"),
        ("Filter: Full payment period", f"{summary['report_year']}{month_str}"),
        ("Total qualifying agents", str(summary["total_qualifying_agents"])),
        ("Total invoices", str(summary["total_invoices"])),
        ("Invoices with TNG rebate", str(summary.get("invoices_with_tng_rebate", 0))),
        ("Total NFP commission (RM)", _fmt_rm(summary["total_nfp_commission"])),
    ]
    sections = []
    agent_rows = build_agent_summary_table(summary)
    if agent_rows:
        sections.append(
            PdfSection(
                title="Table 1: Final Commission Payout Summary by agent",
                headers=TABLE1_HEADERS,
                rows=agent_rows,
            )
        )
    detail_rows = display_rows_as_lists(rows, summary)
    if detail_rows:
        sections.append(
            PdfSection(
                title="Table 2: Accumulated NFP Commission by Customer",
                headers=TABLE2_HEADERS,
                rows=detail_rows,
                landscape=True,
            )
        )
    write_commission_pdf(
        path,
        title="Outsource NFP Commission Report",
        meta_lines=summary_info,
        sections=sections,
    )
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Outsource NFP commission report")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=None, help="Month to filter (1-12)")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Table-format CSV (default: ../4. data/reports/)",
    )
    parser.add_argument(
        "--table-output",
        type=Path,
        default=None,
        help="ASCII table text file (default: ../4. data/reports/)",
    )
    parser.add_argument(
        "--html-output",
        type=Path,
        default=None,
        help="HTML table file (default: ../4. data/reports/)",
    )
    parser.add_argument(
        "--pdf-output",
        type=Path,
        default=None,
        help="PDF report (default: ../2. Output/outsource_nfp_commission_<year>.pdf)",
    )
    parser.add_argument("--no-pdf", action="store_true", help="Do not write PDF file")
    parser.add_argument(
        "--full-csv",
        type=Path,
        default=None,
        help="Optional full audit CSV with all fields",
    )
    parser.add_argument("--json", type=Path, default=None, help="Optional JSON summary path")
    parser.add_argument("--no-save", action="store_true", help="Print tables only, do not write files")
    parser.add_argument(
        "--test-api",
        action="store_true",
        help="Only test database API connection (no report)",
    )
    args = parser.parse_args()

    if args.test_api:
        rows = test_connection()
        print("OK — API connection works.", rows)
        return

    month_suffix = f"_{args.month:02d}" if args.month is not None else ""
    output_csv = args.output or (REPORTS_DIR / f"outsource_nfp_commission_report{month_suffix}.csv")
    table_output = args.table_output or (REPORTS_DIR / f"outsource_nfp_commission_report{month_suffix}.txt")
    html_output = args.html_output or (REPORTS_DIR / f"outsource_nfp_commission_report{month_suffix}.html")

    rows, summary = build_report(args.year, args.month)

    # Print to console
    print(render_tables(rows, summary))

    if not rows:
        print("\nNo outsource invoices matched filters.")
    elif not args.no_save:
        write_table_file(table_output, rows, summary)
        write_table_csv(output_csv, rows, summary)
        html_output.write_text(render_html_table(rows, summary), encoding="utf-8")
        tng_audit_path = REPORTS_DIR / f"outsource_tng_audit_{args.year}{month_suffix}.csv"

        if not args.no_pdf:
            pdf_path = args.pdf_output or (
                OUTPUT_DIR / f"outsource_nfp_commission_{args.year}{month_suffix}.pdf"
            )
            try:
                write_outsource_nfp_pdf(pdf_path, rows, summary)
                print(f"  Table (PDF):  {pdf_path}")
            except ImportError as exc:
                print(f"  Table (PDF):  skipped ({exc})", file=sys.stderr)

        print(f"\nFiles saved:")
        print(f"  Table (text): {table_output}")
        print(f"  Table (CSV):  {output_csv}")
        print(f"  Table (HTML): {html_output}")
        print(f"  TNG audit:    {tng_audit_path}")

        if args.full_csv:
            write_full_csv(args.full_csv, rows, summary)
            print(f"  Full audit:   {args.full_csv}")

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Summary JSON: {args.json}")


if __name__ == "__main__":
    main()
