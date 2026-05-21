#!/usr/bin/env python3
"""
NFP (Net Floor Price) commission report for internal full-time agents.

Filters:
  - invoice.paid = TRUE
  - agent.agent_type in ('internal', 'FULL TIME')
  - EXTRACT(YEAR FROM invoice.full_payment_date) = report year (default 2026)

Formulas:
  Sales Price  = invoice total_amount - epp_cost
  System Price = main package line unit_price
  Sales Price    = total_amount - EPP interest (always)
  Commission a   = (Sales Price - NFP) * 25%  when Sales Price > NFP
  Commission b   = (System Price - NFP) * 100% when System Price > NFP (audit only)
  Commission c   = (NFP - Sales Price) * 20%  when Sales Price < NFP (deduction)
  NFP Commission = a - c   (sales-price component only; b is not added)

Net Floor Price:
  - invoice_date before Oct 2025 -> no NFP
  - Oct–Dec 2025 + 620W panels -> data/nfp_620w_schedule.json
  - 650W panels -> monthly sheet in ../1. Excel/STRING 650W package STRING INVERTER.xlsx
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
from nfp_paths import ensure_reports_dir
from net_floor_prices import (
    NFP_CUTOFF,
    infer_panel_qty_from_text,
    infer_panel_rating_from_text,
    load_620w_schedule,
    load_650w_schedules,
    lookup_net_floor_price,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = ensure_reports_dir()

AGENT_TYPES = ("internal", "full time")

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
        pkg.system_price,
        pkg.package_description,
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
            NULLIF(SUM(CASE WHEN COALESCE(ii.epp, 0) > 0 THEN ii.epp ELSE 0 END), 0),
            SUM(
                CASE
                    WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%'
                         OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
                    THEN COALESCE(ii.amount, ii.unit_price, 0)
                    ELSE 0
                END
            ),
            0
        ) AS epp_cost
        FROM invoice_item ii
        WHERE ii.linked_invoice = i.bubble_id
    ) epp_items ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            ii.unit_price AS system_price,
            ii.description AS package_description
        FROM invoice_item ii
        WHERE ii.linked_invoice = i.bubble_id
          AND ii.is_a_package IS TRUE
        ORDER BY ii.id
        LIMIT 1
    ) pkg ON TRUE
    LEFT JOIN LATERAL (
        SELECT string_agg(COALESCE(ii.description, ''), ' | ') AS all_item_text
        FROM invoice_item ii
        WHERE ii.linked_invoice = i.bubble_id
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
        WHERE ii.linked_invoice = i.bubble_id
    ) tng ON TRUE
    WHERE i.paid IS TRUE
      AND EXTRACT(YEAR FROM i.full_payment_date) = {year}
      AND LOWER(TRIM(COALESCE(a.agent_type, ''))) IN ({agent_types})
      AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
),
epp_once AS (
    SELECT
        invoice_key,
        COALESCE(
            NULLIF(MAX(CASE WHEN line_epp_cost > 0 THEN line_epp_cost END), NULL),
            NULLIF(MAX(CASE WHEN effective_epp > 0 THEN effective_epp END), NULL),
            0
        ) AS epp_cost
    FROM candidates
    GROUP BY invoice_key
),
ranked AS (
    SELECT
        c.*,
        ROW_NUMBER() OVER (
            PARTITION BY c.invoice_key
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
    # NFP tables from 2026 use 650W monthly Excel; ignore "620W" in legacy line text.
    if inv_date and inv_date.year >= 2026:
        panel_rating = 650
    elif panel_rating is None and inv_date and inv_date >= NFP_CUTOFF:
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


def build_report(year: int) -> tuple[List[InvoiceCommission], Dict[str, Any]]:
    agent_types = [t.lower() for t in AGENT_TYPES]
    types_sql = ", ".join(f"'{t}'" for t in agent_types)
    sql = INVOICES_SQL.format(year=year, agent_types=types_sql)
    rows = q(sql)
    schedules_650 = load_650w_schedules()
    schedule_620 = load_620w_schedule()

    results: List[InvoiceCommission] = []
    for row in rows:
        total = to_decimal(row.get("total_amount"))
        epp = to_decimal(row.get("epp_cost"))
        sales = money(total - epp)
        system = money(to_decimal(row.get("system_price")))

        panel_qty, panel_rating = resolve_panels(row)
        inv_date = parse_date(row.get("invoice_date"))
        has_tng = bool(row.get("has_tng_rebate"))
        tng_evidence = (row.get("tng_evidence") or "").strip() or None
        three_phase = is_three_phase_from_seda(row.get("phase_type"))

        nfp_value: Optional[Decimal] = None
        nfp_source = "n/a"
        if inv_date and panel_qty:
            nfp_raw, nfp_source = lookup_net_floor_price(
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
        elif inv_date and inv_date < NFP_CUTOFF:
            nfp_source = "before_oct_2025_no_nfp"
        else:
            nfp_source = "missing_panel_qty_or_rating"

        a, b, c, total_comm = calc_commission(sales, system, nfp_value)

        results.append(
            InvoiceCommission(
                agent_name=(row.get("agent_name") or "").strip(),
                agent_type=row.get("agent_type") or "",
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


# Main report table columns (matches your spec)
TABLE_HEADERS = [
    "Agent Name",
    "Customer Name",
    "Invoice No.",
    "Invoice Date",
    "TNG Rebate",
    "EPP Interest (RM)",
    "Sales Price (RM)",
    "System Price (RM)",
    "Net Floor Price (RM)",
    "NFP Commission (RM)",
    "Agent Accumulated NFP (RM)",
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
                "Invoice No.": d["invoice_number"],
                "Invoice Date": d["invoice_date"] or "-",
                "TNG Rebate": d["tng_rebate"],
                "EPP Interest (RM)": _fmt_rm(d["epp_cost"]),
                "Sales Price (RM)": _fmt_rm(d["sales_price"]),
                "System Price (RM)": _fmt_rm(d["system_price"]),
                "Net Floor Price (RM)": _fmt_rm(d["net_floor_price"]),
                "NFP Commission (RM)": _fmt_rm(d["nfp_commission"]),
                "Agent Accumulated NFP (RM)": _fmt_rm(
                    _agent_accumulated_nfp(summary, d["agent_name"])
                ),
            }
        )
    return out


AGENT_SUMMARY_HEADERS = [
    "Agent Name",
    "Sales Price (RM)",
    "System Price (RM)",
    "Net Floor Price (RM)",
    "Accumulated NFP Commission (RM)",
]


def build_agent_summary_table(summary: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for agent, totals in sorted(summary.get("accumulated_by_agent", {}).items()):
        if isinstance(totals, dict):
            rows.append(
                [
                    agent,
                    _fmt_rm(totals.get("sales_price")),
                    _fmt_rm(totals.get("system_price")),
                    _fmt_rm(totals.get("net_floor_price")),
                    _fmt_rm(totals.get("nfp_commission")),
                ]
            )
        else:
            rows.append([agent, "-", "-", "-", _fmt_rm(totals)])
    return rows


def display_rows_as_lists(
    rows: List[InvoiceCommission], summary: Dict[str, Any]
) -> List[List[str]]:
    return [[d[h] for h in TABLE_HEADERS] for d in build_display_rows(rows, summary)]


def render_tables(
    rows: List[InvoiceCommission], summary: Dict[str, Any]
) -> str:
    lines: List[str] = []
    lines.append("NFP COMMISSION REPORT")
    lines.append("=" * 80)
    lines.append("")

    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Internal + FULL TIME"],
        ["Filter: Full Payment Year", str(summary["report_year"])],
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
        lines.append("ACCUMULATED NFP COMMISSION BY AGENT")
        lines.append(
            _tabulate_safe(AGENT_SUMMARY_HEADERS, agent_rows, headers_first_row=False)
        )
        lines.append("")

    detail_rows = display_rows_as_lists(rows, summary)
    if detail_rows:
        lines.append("INVOICE DETAIL")
        lines.append(_tabulate_safe(TABLE_HEADERS, detail_rows, headers_first_row=False))

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

    summary_info = [
        ["Filter: Paid", "TRUE"],
        ["Filter: Agent Type", "Internal + FULL TIME"],
        ["Filter: Full Payment Year", str(summary["report_year"])],
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
            "Accumulated NFP Commission by Agent",
            AGENT_SUMMARY_HEADERS,
            build_agent_summary_table(summary),
        ),
        html_grid("Invoice Detail", TABLE_HEADERS, display_rows_as_lists(rows, summary)),
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
        w = csv.DictWriter(f, fieldnames=TABLE_HEADERS)
        w.writeheader()
        for row in build_display_rows(rows, summary):
            w.writerow(row)


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
    parser = argparse.ArgumentParser(description="NFP commission report")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPORTS_DIR / "nfp_commission_report.csv",
        help="Table-format CSV (default: ../4. data/reports/)",
    )
    parser.add_argument(
        "--table-output",
        type=Path,
        default=REPORTS_DIR / "nfp_commission_report.txt",
        help="ASCII table text file (default: ../4. data/reports/)",
    )
    parser.add_argument(
        "--html-output",
        type=Path,
        default=REPORTS_DIR / "nfp_commission_report.html",
        help="HTML table file (default: ../4. data/reports/)",
    )
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

    rows, summary = build_report(args.year)

    # --- Table output (console) ---
    print_tables(rows, summary)

    if not rows:
        print("\nNo invoices matched filters.")
    elif not args.no_save:
        write_table_file(args.table_output, rows, summary)
        write_table_csv(args.output, rows, summary)
        args.html_output.write_text(render_html_table(rows, summary), encoding="utf-8")
        tng_audit_path = REPORTS_DIR / f"tng_audit_{args.year}.csv"
        write_tng_audit_csv(tng_audit_path, rows)
        print(f"\nFiles saved:")
        print(f"  Table (text): {args.table_output}")
        print(f"  Table (CSV):  {args.output}")
        print(f"  Table (HTML): {args.html_output}")
        print(f"  TNG audit:    {tng_audit_path}")
        if args.full_csv:
            write_full_csv(args.full_csv, rows, summary)
            print(f"  Full audit:   {args.full_csv}")

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Summary JSON: {args.json}")


if __name__ == "__main__":
    main()
