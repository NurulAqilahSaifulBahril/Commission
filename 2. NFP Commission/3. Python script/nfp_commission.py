#!/usr/bin/env python3
"""
NFP (Net Floor Price) commission report for internal full-time agents.

Filters:
  - invoice payment progress 0–100% (percent_of_total_amount; values <= 1 treated as fraction)
  - agent.agent_type in ('internal', 'FULL TIME')
  - EXTRACT(YEAR FROM invoice.invoice_date) = report year (default 2026)

Formulas:
  Sales Price  = invoice total_amount - epp_cost
  System Price = main package line unit_price
  Sales Price    = total_amount - EPP interest (always)
  Commission a   = (Sales Price - NFP) * tier rate   when Sales Price > NFP
  Commission b   = (System Price - NFP) * tier rate  when System Price > NFP (audit only)
  Commission c   = (NFP - Sales Price) * tier rate   when Sales Price < NFP (deduction)
  NFP Commission = a - c   (sales-price component only; b is not added)

  Tier rates come from the dashboard Data page (Net Floor Price section),
  resolved on the invoice month per agent. Tiers with nothing entered fall back
  to the built-ins this report has always used: 25% / 100% / 20%.

Net Floor Price:
  - invoice_date before Oct 2025 -> no NFP
  - Oct–Dec 2025 + 620W panels -> data/nfp_620w_schedule.json
  - Nov 2025+ Excel schedules by inverter type (590W / 620W / 650W)
  - If invoice month lacks the required inverter table, fall back to another month
  - TNG rebate on invoice -> use FINAL PRICE AFTER DISCOUNT (WITH TNG REBATE)

Folder layout (under NFP Commission):
  1. Excel/  3. Python script/  4. data/  (reports -> data/reports/)

Usage (from "3. Python script" folder):
  set POSTGRES_PROXY_TOKEN=<bearer token>
  python nfp_commission.py
  python nfp_commission.py --year 2026
  .\\run_nfp.ps1
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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
REPORTS_DIR = ensure_reports_dir()
OUTPUT_DIR = ensure_output_dir()

AGENT_TYPES = ("internal", "full time")


def _rates_module():
    """basic_commission_rates, or None. Routing must never be the thing that
    breaks a report, so callers fall back to the Postgres-only behaviour."""
    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
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
    inv_date = parse_date(row.get("invoice_date"))
    return bcr.resolve_agent_type(
        row.get("agent_name"), pg,
        month=inv_date.month if inv_date else None,
        year=inv_date.year if inv_date else None,
    ) == "internal"

INVOICES_SQL = """
WITH target_invoices AS (
  SELECT *
  FROM invoice
  WHERE total_amount > 0 
    AND (
      extract(year from invoice_date) = {year} 
      OR bubble_id IN (SELECT linked_invoice FROM payment WHERE extract(year from payment_date) = {year})
    )
),
pct75 AS (
  SELECT sub.linked_invoice,
         MIN(sub.payment_date) AS pct75_date
  FROM (
    SELECT p.linked_invoice,
           p.payment_date,
           SUM(p.amount) OVER (
             PARTITION BY p.linked_invoice
             ORDER BY p.payment_date ASC, p.id ASC
           ) AS running_total,
           i.total_amount
    FROM payment p
    JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) sub
  WHERE sub.running_total >= sub.total_amount * 0.75
  GROUP BY sub.linked_invoice
),
pct100 AS (
  SELECT sub.linked_invoice,
         MIN(sub.payment_date) AS pct100_date
  FROM (
    SELECT p.linked_invoice,
           p.payment_date,
           SUM(p.amount) OVER (
             PARTITION BY p.linked_invoice
             ORDER BY p.payment_date ASC, p.id ASC
           ) AS running_total,
           i.total_amount
    FROM payment p
    JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) sub
  WHERE sub.running_total >= sub.total_amount
  GROUP BY sub.linked_invoice
),
invoice_items_mapped AS (
    SELECT linked_invoice AS invoice_bubble_id, bubble_id, epp, description, amount, unit_price, linked_package, is_a_package, voucher_remark, inv_item_type, linked_voucher, id
    FROM invoice_item
    WHERE linked_invoice IS NOT NULL
    UNION
    SELECT i.bubble_id AS invoice_bubble_id, ii.bubble_id, ii.epp, ii.description, ii.amount, ii.unit_price, ii.linked_package, ii.is_a_package, ii.voucher_remark, ii.inv_item_type, ii.linked_voucher, ii.id
    FROM target_invoices i
    CROSS JOIN LATERAL unnest(i.linked_invoice_item) AS item_id
    INNER JOIN invoice_item ii ON ii.bubble_id = item_id
),
epp_items AS (
    SELECT invoice_bubble_id, COALESCE(
        NULLIF(SUM(CASE WHEN COALESCE(epp_val, 0) > 0 THEN epp_val ELSE 0 END), 0),
        SUM(epp_interest_amount),
        0
    ) AS epp_cost
    FROM (
        SELECT 
            invoice_bubble_id,
            MAX(COALESCE(epp, 0)) AS epp_val,
            MAX(
                CASE
                    WHEN COALESCE(description, '') ILIKE '%%epp%%interest%%'
                         OR COALESCE(description, '') ILIKE '%%epp interest%%'
                    THEN COALESCE(amount, unit_price, 0)
                    ELSE 0
                END
            ) AS epp_interest_amount
        FROM invoice_items_mapped
        GROUP BY invoice_bubble_id, TRIM(
            REGEXP_REPLACE(
                REGEXP_REPLACE(
                    REGEXP_REPLACE(COALESCE(description, ''), 'moths', 'months', 'gi'),
                    '(\\d+)\\s*months',
                    '\\1months',
                    'gi'
                ),
                '\\s+',
                ' ',
                'g'
            )
        )
    ) sub
    GROUP BY invoice_bubble_id
),
payment_epp AS (
    SELECT p.linked_invoice, SUM(COALESCE(p.epp_cost, 0)) AS epp_sum
    FROM payment p
    JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
    GROUP BY p.linked_invoice
),
pkg_ranked AS (
    SELECT
        invoice_bubble_id,
        COALESCE(NULLIF(unit_price, 0), amount, 0) AS system_price,
        description AS package_description,
        linked_package,
        ROW_NUMBER() OVER (
            PARTITION BY invoice_bubble_id
            ORDER BY
                CASE WHEN is_a_package IS TRUE THEN 0 ELSE 1 END,
                COALESCE(NULLIF(unit_price, 0), amount, 0) DESC,
                id
        ) as rn
    FROM invoice_items_mapped
),
pkg AS (
    SELECT p.invoice_bubble_id, p.system_price, p.package_description, COALESCE(pkg_table.nett_price, 0) AS db_net_floor_price
    FROM pkg_ranked p
    LEFT JOIN package pkg_table ON pkg_table.bubble_id = p.linked_package
    WHERE p.rn = 1
),
items AS (
    SELECT invoice_bubble_id, string_agg(COALESCE(description, ''), ' | ') AS all_item_text
    FROM invoice_items_mapped
    GROUP BY invoice_bubble_id
),
tng AS (
    SELECT
        invoice_bubble_id,
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
    FROM invoice_items_mapped ii
    LEFT JOIN voucher v ON v.bubble_id = ii.linked_voucher
    GROUP BY invoice_bubble_id
),
candidates AS (
    SELECT
        i.bubble_id,
        i.invoice_number,
        i.invoice_date,
        i.full_payment_date,
        pct75.pct75_date,
        pct100.pct100_date,
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
        COALESCE(pkg.system_price, 0) AS system_price,
        pkg.package_description,
        COALESCE(pkg.db_net_floor_price, 0) AS db_net_floor_price,
        items.all_item_text,
        COALESCE(tng.has_tng, FALSE) AS has_tng_rebate,
        tng.tng_evidence,
        COALESCE(NULLIF(TRIM(i.invoice_number), ''), i.bubble_id) AS invoice_key
    FROM target_invoices i
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
    LEFT JOIN pct75 ON pct75.linked_invoice = i.bubble_id
    LEFT JOIN pct100 ON pct100.linked_invoice = i.bubble_id
    LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
    LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
    LEFT JOIN epp_items ON epp_items.invoice_bubble_id = i.bubble_id
    LEFT JOIN payment_epp pay ON pay.linked_invoice = i.bubble_id
    LEFT JOIN pkg ON pkg.invoice_bubble_id = i.bubble_id
    LEFT JOIN items ON items.invoice_bubble_id = i.bubble_id
    LEFT JOIN tng ON tng.invoice_bubble_id = i.bubble_id
    WHERE (
        LOWER(TRIM(COALESCE(a.agent_type, ''))) IN ({agent_types})
        OR LOWER(TRIM(COALESCE(a.name, ''))) IN ({agent_type_overrides})
      )
      AND LOWER(TRIM(COALESCE(a.name, ''))) != 'gan lai soon'
      AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
),
ranked AS (
    SELECT
        c.*,
        ROW_NUMBER() OVER (
            PARTITION BY c.invoice_key
            ORDER BY COALESCE(c.is_latest, FALSE) DESC,
                     c.pct100_date DESC NULLS LAST,
                     c.invoice_row_id DESC
        ) AS rn
    FROM candidates c
),
epp_once AS (
    SELECT
        invoice_key,
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
    GROUP BY invoice_key
)
SELECT
    r.bubble_id,
    r.invoice_number,
    r.invoice_date,
    r.pct100_date AS full_payment_date,
    r.pct75_date,
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
INNER JOIN epp_once e ON e.invoice_key = r.invoice_key
WHERE r.rn = 1
ORDER BY r.agent_name, r.invoice_date, r.invoice_number
"""


def q(sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    return query_sql(sql, params)


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


@dataclass
class InvoiceCommission:
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
    package_description: Optional[str] = None
    pct75_date: Optional[str] = None
    # The month this commission is recognised in. Equal to full_payment_date
    # unless a Data page tier states a different payment stage, so consumers
    # bucket on this and full_payment_date keeps meaning "paid in full".
    payout_date: Optional[str] = None
    payout_trigger_pct: Optional[float] = None



# The tier rates the engine falls back to when the Data page has nothing entered
# for a tier. Kept as plain strings so calc_commission stays usable with no
# dashboard database in reach.
NFP_TIER_DEFAULTS = {
    "sales_above": Decimal("0.25"),
    "system_above": Decimal("1.00"),
    "sales_below": Decimal("0.20"),
}

def nfp_tier_rates_for(agent_name: str, agent_type: str,
                       month: Optional[int], year: Optional[int]) -> Optional[dict]:
    """Tier rates from the Data page for this agent and invoice month, or None
    to leave the built-in rates in charge.

    Resolved on the INVOICE month, matching how basic commission reads its rate:
    the Data page's Invoice Month is the month a value takes effect from, not
    the month the money is paid out in.

    Deliberately not memoised here. basic_commission_rates already caches the
    tables it reads and drops them on reset_cache(), which the dashboard calls
    the moment a rate is saved -- a second cache in this module would survive
    that call and keep paying the old rate until the process restarted.

    A rate lookup must never be the thing that breaks a report, so every failure
    (no dashboard module, unreachable database, an older module without the
    resolver) falls back to the built-ins rather than raising.
    """
    if not month or not year:
        return None
    bcr = _rates_module()
    if bcr is None or not hasattr(bcr, "get_nfp_tier_rates"):
        return None
    try:
        role = bcr.get_agent_role(agent_name, month, year=year,
                                  agent_type=agent_type)
    except Exception:
        role = None
    try:
        return bcr.get_nfp_tier_rates(agent_type, month, year=year,
                                      agent=agent_name, hierarchy=role)
    except Exception:
        return None


def calc_commission(
    sales_price: Decimal,
    system_price: Decimal,
    net_floor: Optional[Decimal],
    tier_rates: Optional[dict] = None,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if net_floor is None:
        return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")

    rates = dict(NFP_TIER_DEFAULTS)
    if tier_rates:
        for k, v in tier_rates.items():
            if k in rates and v is not None:
                rates[k] = Decimal(str(v))

    a = Decimal("0")
    b = Decimal("0")
    c = Decimal("0")

    if sales_price > net_floor:
        a = (sales_price - net_floor) * rates["sales_above"]
    if system_price > net_floor:
        b = (system_price - net_floor) * rates["system_above"]
    if sales_price < net_floor:
        c = (net_floor - sales_price) * rates["sales_below"]

    # NFP commission = sales-side only (a - c). Component b is kept for audit, not summed here.
    nfp_total = a - c
    return a, b, c, nfp_total


def resolve_panels(row: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    qty = row.get("panel_qty")
    rating = row.get("panel_rating")
    panel_qty = int(qty) if qty is not None else None
    panel_rating = int(rating) if rating is not None else None

    text_blob = " | ".join(
        filter(
            None,
            [row.get("package_description"), row.get("all_item_text")],
        )
    )
    if panel_qty is None:
        panel_qty = infer_panel_qty_from_text(text_blob)
    if panel_rating is None:
        panel_rating = infer_panel_rating_from_text(text_blob)

    inv_date = parse_date(row.get("invoice_date"))
    if panel_rating is None and inv_date and inv_date >= NFP_CUTOFF:
        panel_rating = 650

    return panel_qty, panel_rating


def is_three_phase_from_seda(phase_type: Optional[str]) -> bool:
    """Use public.seda_registration.phase_type (single vs three phase)."""
    if not phase_type or not str(phase_type).strip():
        return False
    t = str(phase_type).lower().strip()
    if "single" in t:
        return False
    if t in ("1", "1 phase"):
        return False
    if t in ("3", "3 phase"):
        return True
    if "three" in t:
        return True
    if "3 phase" in t:
        return True
    return False


# ── Payout condition (dashboard "Net Floor Price Rate Tiers") ────────────────
# NFP used to recognise strictly at 100% payment. The tier rows on the Data page
# can now state a payment stage instead, so the month an invoice's NFP commission
# lands in follows that trigger. With nothing entered every invoice still lands
# on its 100% date, exactly as before.
#
# The main query already returns the 75% and 100% dates; any other percentage a
# tier asks for is computed by one extra pass, built from the same SQL the basic
# engine uses so the two cannot drift apart on what "reached N%" means.

_PCT_75 = Decimal("75")
_PCT_100 = Decimal("100")


def _extra_milestone_dates(year: int) -> Dict[str, Dict[Decimal, str]]:
    """{invoice bubble_id: {threshold: date}} for the NFP payout thresholds the
    main query does not already compute. Empty when nothing beyond 75/100 is
    asked for, which is the normal case."""
    bcr = _rates_module()
    if bcr is None or not hasattr(bcr, "nfp_payout_thresholds_in_use"):
        return {}
    try:
        wanted = [t for t in bcr.nfp_payout_thresholds_in_use()
                  if Decimal(t) not in (_PCT_75, _PCT_100)]
    except Exception:
        return {}
    if not wanted:
        return {}
    try:
        ctes, selects, _joins = bcr.milestone_sql_parts(wanted)
        cols = [(Decimal(t), bcr.milestone_column(t)) for t in wanted]
        joins = "\n".join(
            f"  LEFT JOIN pct_{bcr._threshold_slug(t)} "
            f"ON pct_{bcr._threshold_slug(t)}.linked_invoice = i.bubble_id"
            for t, _c in cols)
        select_cols = "\n".join(f"    pct_{bcr._threshold_slug(t)}.{c}," for t, c in cols)
        sql = f"""
WITH target_invoices AS (
  SELECT *
  FROM invoice
  WHERE total_amount > 0
    AND (
      extract(year from invoice_date) = {year}
      OR bubble_id IN (SELECT linked_invoice FROM payment WHERE extract(year from payment_date) = {year})
    )
),
{ctes}
SELECT
    i.bubble_id,
{select_cols}
    i.bubble_id AS _tail
FROM target_invoices i
{joins}
"""
        out: Dict[str, Dict[Decimal, str]] = {}
        for r in q(sql):
            key = str(r.get("bubble_id") or "")
            out[key] = {t: str(r.get(c) or "")[:10] for t, c in cols}
        return out
    except Exception as e:
        print(f"[NFP] Warning: extra milestone dates unavailable ({e}); "
              f"payout conditions other than 75% / 100% fall back to 100%.")
        return {}


def _payout_trigger_for(row: dict) -> Decimal:
    """The percentage this invoice's NFP commission is recognised at, from the
    Data page tier rows for its agent and invoice month."""
    bcr = _rates_module()
    if bcr is None or not hasattr(bcr, "get_nfp_payout_trigger"):
        return _PCT_100
    inv_date = parse_date(row.get("invoice_date"))
    if not inv_date:
        return _PCT_100
    try:
        role = bcr.get_agent_role(row.get("agent_name"), inv_date.month,
                                  year=inv_date.year,
                                  agent_type=row.get("agent_type"))
    except Exception:
        role = None
    try:
        return Decimal(bcr.get_nfp_payout_trigger(
            row.get("agent_type"), inv_date.month, year=inv_date.year,
            agent=row.get("agent_name"), hierarchy=role))
    except Exception:
        return _PCT_100


def _recognition_date(row: dict, trigger: Decimal,
                      extra: Dict[str, Dict[Decimal, str]]) -> Optional[date]:
    """The date this invoice reached the trigger, or None if it never has."""
    if trigger == _PCT_100:
        return parse_date(row.get("full_payment_date"))
    if trigger == _PCT_75:
        return parse_date(row.get("pct75_date"))
    raw = (extra.get(str(row.get("bubble_id") or "")) or {}).get(trigger)
    return parse_date(raw) if raw else None


def _effective_nfp_date(row: dict) -> Optional[date]:
    """The date this row's NFP commission is recognised — the payout trigger's
    milestone, resolved into the row by build_report, or the 100% date for a row
    that has not been through it."""
    if "_nfp_payout_date" in row:
        return row["_nfp_payout_date"]
    return parse_date(row.get("full_payment_date"))


def build_report(year: int, month: Optional[int] = None) -> tuple[List[InvoiceCommission], Dict[str, Any]]:
    agent_types = [t.lower() for t in AGENT_TYPES]
    types_sql = ", ".join(f"'{t}'" for t in agent_types)
    sql = INVOICES_SQL.format(year=year, agent_types=types_sql,
                              agent_type_overrides=_agent_type_override_sql())
    rows = [r for r in q(sql) if _is_internal_row(r)]
    # Resolve each invoice's payout trigger and the date it was reached BEFORE
    # the month filter: the trigger decides which month a row belongs to, so it
    # cannot be applied after the rows for the month have been chosen.
    extra_milestones = _extra_milestone_dates(year)
    for r in rows:
        trigger = _payout_trigger_for(r)
        r["_nfp_trigger"] = trigger
        r["_nfp_payout_date"] = _recognition_date(r, trigger, extra_milestones)
    if month is not None:
        rows = [r for r in rows if _effective_nfp_date(r) and _effective_nfp_date(r).month == month]
    # Net floor prices come from the price list in the system (nfp_prices).
    # The Excel schedules are no longer read: across internal and outsource
    # for 2025 and 2026 the Excel-only source labels never appeared once,
    # and substituting an empty map in the same process produced byte-
    # identical output. Parsing the nine-sheet workbook only cost time.
    # load_650w_schedules() is kept in net_floor_prices for ad-hoc use, and
    # parse_schedule_workbook_rows still backs the Data page price upload.
    schedules_650 = {}
    schedule_620 = load_620w_schedule()

    results: List[InvoiceCommission] = []
    for row in rows:
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
                src = str(fallback_source or "")
                label = "Price list" if src.startswith("db_") else "Excel schedule"
                nfp_source = f"{label} ({src})"
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

        a, b, c, total_comm = calc_commission(
            sales, system, nfp_value,
            nfp_tier_rates_for(row.get("agent_name"), row.get("agent_type"),
                               inv_date.month if inv_date else None,
                               inv_date.year if inv_date else None))

        inv_num = str(row.get("invoice_number") or "").strip()
        row_fp = str(row.get("full_payment_date") or "")[:10] or None
        row_p75 = str(row.get("pct75_date") or "")[:10] or None
        
        row_trigger = row.get("_nfp_trigger") or _PCT_100
        row_payout = row.get("_nfp_payout_date")
        row_payout = row_payout.isoformat() if row_payout else None

        NOT_FULLY_PAID_INVS = {'1008316', '1007905'}
        if inv_num in NOT_FULLY_PAID_INVS:
            row_fp = None
            row_p75 = None
            # These are held back regardless of what stage they have reached, so
            # the payout date has to go with them or the trigger would let them
            # back into a month through the side door.
            row_payout = None

        results.append(
            InvoiceCommission(
                agent_name=(row.get("agent_name") or "").strip(),
                agent_type=row.get("agent_type") or "",
                customer_name=(row.get("customer_name") or "").strip(),
                invoice_number=inv_num,
                invoice_date=str(row.get("invoice_date") or "")[:10] or None,
                full_payment_date=row_fp,
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
                package_description=row.get("package_description"),
                pct75_date=row_p75,
                payout_date=row_payout,
                payout_trigger_pct=float(row_trigger),
            )
        )

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

    def _round_agent_totals(totals: Dict[str, float]) -> Dict[str, float]:
        return {k: round(v, 2) for k, v in totals.items()}

    summary = {
        "report_year": year,
        "report_month": month,
        "agent_types": list(AGENT_TYPES),
        "total_qualifying_agents": len(agents),
        "total_invoices": len(results),
        "invoices_with_tng_rebate": len(tng_invoices),
        "total_nfp_commission": round(sum(r.nfp_commission for r in results), 2),
        "accumulated_by_agent": {
            agent: _round_agent_totals(totals)
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
    }
    return results, summary


def _fmt_rm(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value):,.2f}"


# Main report table columns (matches your sp# Main report table columns (matches your spec)
TABLE1_HEADERS = [
    "Agent_name",
    "Total Commission Payout"
]

TABLE2_HEADERS = [
    "Agent Name",
    "Customer Name",
    "Invoice Number",
    "Invoice Date",
    "Full Payment Date",
    "Sales Price",
    "System Price",
    "Net Floor Price",
    "NFP Commission",
    "Accumulated NFP Commission",
]


def format_grid_table(headers: List[str], rows: List[List[str]]) -> str:
    """ASCII grid table (stdlib only — always available in this script)."""
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
    """Use tabulate if installed; otherwise built-in grid table."""
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
    rows: List[InvoiceCommission], summary: Dict[str, Any]
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
                "Full Payment Date": d["full_payment_date"] or "-",
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


def build_agent_summary_table(summary: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for agent, totals in sorted(summary.get("accumulated_by_agent", {}).items()):
        if isinstance(totals, dict):
            rows.append(
                [
                    agent,
                    _fmt_rm(totals.get("nfp_commission")),
                ]
            )
        else:
            rows.append([agent, _fmt_rm(totals)])
    return rows


def display_rows_as_lists(
    rows: List[InvoiceCommission], summary: Dict[str, Any]
) -> List[List[str]]:
    return [[d[h] for h in TABLE2_HEADERS] for d in build_display_rows(rows, summary)]


def render_tables(
    rows: List[InvoiceCommission], summary: Dict[str, Any]
) -> str:
    lines: List[str] = []
    lines.append("NFP COMMISSION REPORT")
    lines.append("=" * 80)
    lines.append("")

    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Internal + FULL TIME"],
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
        lines.append(
            _tabulate_safe(TABLE1_HEADERS, agent_rows, headers_first_row=False)
        )
        lines.append("")

    detail_rows = display_rows_as_lists(rows, summary)
    if detail_rows:
        lines.append("Table 2: Accumulated NFP Commission by Customer")
        lines.append(_tabulate_safe(TABLE2_HEADERS, detail_rows, headers_first_row=False))

    return "\n".join(lines)


def render_html_table(
    rows: List[InvoiceCommission], summary: Dict[str, Any]
) -> str:
    def html_grid(title: str, headers: List[str], data: List[List[str]]) -> str:
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in data
        )
        return f"<h2>{title}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Internal + FULL TIME"],
        ["Filter: Full payment period", f"{summary['report_year']}{month_str}"],
        ["Total qualifying agents (users)", str(summary["total_qualifying_agents"])],
        ["Total invoices", str(summary["total_invoices"])],
        ["Total NFP commission (RM)", _fmt_rm(summary["total_nfp_commission"])],
    ]
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>NFP Commission Report</title>",
        "<style>table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;font-size:13px}"
        "th{background:#f0f0f0}h1,h2{font-family:sans-serif}</style></head><body>",
        "<h1>NFP Commission Report</h1>",
        html_grid("Summary", ["Metric", "Value"], summary_info),
        html_grid(
            "Table 1: Final Commission Payout Summary by agent",
            TABLE1_HEADERS,
            build_agent_summary_table(summary),
        ),
        html_grid("Table 2: Accumulated NFP Commission by Customer", TABLE2_HEADERS, display_rows_as_lists(rows, summary)),
        "</body></html>",
    ]
    return "".join(parts)


def print_tables(rows: List[InvoiceCommission], summary: Dict[str, Any]) -> None:
    """Print all report tables to stdout."""
    print(render_tables(rows, summary))


def write_table_file(path: Path, rows: List[InvoiceCommission], summary: Dict[str, Any]) -> None:
    path.write_text(render_tables(rows, summary), encoding="utf-8")


def write_tng_audit_csv(path: Path, rows: List[InvoiceCommission]) -> None:
    import csv

    fields = [
        "invoice_number",
        "customer_name",
        "agent_name",
        "tng_rebate",
        "tng_evidence",
        "net_floor_price",
        "nfp_source",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "invoice_number": r.invoice_number,
                    "customer_name": r.customer_name,
                    "agent_name": r.agent_name,
                    "tng_rebate": r.tng_rebate,
                    "tng_evidence": r.tng_evidence or "",
                    "net_floor_price": r.net_floor_price if r.net_floor_price is not None else "",
                    "nfp_source": r.nfp_source,
                }
            )


def write_table_csv(path: Path, rows: List[InvoiceCommission], summary: Dict[str, Any]) -> None:
    """CSV with the same columns as the printed table."""
    import csv

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TABLE2_HEADERS)
        w.writeheader()
        for row in build_display_rows(rows, summary):
            w.writerow(row)


def _import_pdf_writer():
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from commission_pdf import PdfSection, write_commission_pdf

    return PdfSection, write_commission_pdf


def write_nfp_pdf(path: Path, rows: List[InvoiceCommission], summary: Dict[str, Any]) -> Path:
    PdfSection, write_commission_pdf = _import_pdf_writer()
    month_str = f" / {summary['report_month']:02d}" if summary.get("report_month") is not None else ""
    summary_info = [
        ("Filter: Paid", "TRUE"),
        ("Filter: Agent Type", "Internal + FULL TIME"),
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
        title="NFP Commission Report",
        meta_lines=summary_info,
        sections=sections,
    )
    return path


def write_full_csv(path: Path, rows: List[InvoiceCommission], summary: Dict[str, Any]) -> None:
    """CSV with all calculated fields (debug / audit)."""
    import csv

    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    extra = ["agent_accumulated_nfp_commission"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames + extra)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["agent_accumulated_nfp_commission"] = _agent_accumulated_nfp(
                summary, r.agent_name
            )
            w.writerow(d)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="NFP commission report")
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
        help="PDF report (default: ../2. Output/nfp_commission_<year>.pdf)",
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
    output_csv = args.output or (REPORTS_DIR / f"nfp_commission_report{month_suffix}.csv")
    table_output = args.table_output or (REPORTS_DIR / f"nfp_commission_report{month_suffix}.txt")
    html_output = args.html_output or (REPORTS_DIR / f"nfp_commission_report{month_suffix}.html")

    rows, summary = build_report(args.year, args.month)

    # --- Table output (console) ---
    print_tables(rows, summary)

    if not rows:
        print("\nNo invoices matched filters.")
    elif not args.no_save:
        write_table_file(table_output, rows, summary)
        write_table_csv(output_csv, rows, summary)
        html_output.write_text(render_html_table(rows, summary), encoding="utf-8")
        tng_audit_path = REPORTS_DIR / f"tng_audit_{args.year}{month_suffix}.csv"
        write_tng_audit_csv(tng_audit_path, rows)
        if not args.no_pdf:
            pdf_path = args.pdf_output or (
                OUTPUT_DIR / f"nfp_commission_{args.year}{month_suffix}.pdf"
            )
            try:
                write_nfp_pdf(pdf_path, rows, summary)
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
