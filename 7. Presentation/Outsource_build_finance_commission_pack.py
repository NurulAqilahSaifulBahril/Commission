#!/usr/bin/env python3
"""
Build one finance Excel workbook for Basic, ANP, and NFP commission (year 2026).

Output:
  Finance Output/Outsource_Commission_Pack_2026_<timestamp>.xlsx
  Finance Output/Outsource_Commission_Pack_2026_<timestamp>.pdf   (with --pdf or --both)

Sheets:
  - Finance Summary
  - Basic - By Agent / Basic - By Invoice
  - ANP - By Agent / ANP - By Invoice
  - NFP - By Agent / NFP - By Invoice

Requires: openpyxl, python-dotenv, and each commission script's dependencies.
Token (any one location — never commit the real token):
  - Commission-git/.env  or  Commission-git/pg_proxy_token.txt  (easiest for finance pack)
  - 1. Basic Commission/3. Python Script/.env
  - 3. ANP Commission/.env
  - 2. NFP Commission/4. data/pg_proxy_token.txt
"""

from __future__ import annotations

import importlib.util
import os
import sys
import re
import math
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
FINANCE_DIR = Path(__file__).resolve().parent / "Finance Output"
YEAR_DEFAULT = 2026

DEFAULT_PROXY_URL = "https://pg-proxy-production.up.railway.app/api/sql"
DEFAULT_DB_NAME = "prod_main"

MOCK_MODE = False


class MockInvoiceLine:
    def __init__(self, agent_name, customer_name, invoice_number, invoice_date, full_payment_date, total_amount, epp_interest, tier_label, commission_rate, basic_commission, package="Shop Lot", profit_sharing=0.0):
        self.agent_name = agent_name
        self.customer_name = customer_name
        self.invoice_number = invoice_number
        self.invoice_date = invoice_date
        self.full_payment_date = full_payment_date
        self.total_amount = Decimal(str(total_amount))
        self.epp_interest = Decimal(str(epp_interest))
        self.tier_label = tier_label
        self.commission_rate = Decimal(str(commission_rate))
        self.basic_commission = Decimal(str(basic_commission))
        self.package = package
        self.profit_sharing = Decimal(str(profit_sharing))

    @property
    def net_base(self):
        return self.total_amount - self.epp_interest


class MockInvoiceCommission:
    def __init__(self, agent_name, customer_name, invoice_number, invoice_date, total_amount, epp_cost, sales_price, system_price, net_floor_price, nfp_commission, full_payment_date=None):
        self.agent_name = agent_name
        self.customer_name = customer_name
        self.invoice_number = invoice_number
        self.invoice_date = invoice_date
        self.total_amount = float(total_amount)
        self.epp_cost = float(epp_cost)
        self.sales_price = float(sales_price)
        self.system_price = float(system_price)
        self.net_floor_price = float(net_floor_price) if net_floor_price is not None else None
        self.nfp_commission = float(nfp_commission)
        self.agent_type = "internal"
        self.full_payment_date = full_payment_date


def get_mock_data(year: int) -> dict[str, Any]:
    basic_lines = []
    anp_detail = []
    nfp_rows = []
    
    agents = ["Sunny Tan", "Martin Hing", "Teng Kah Kent", "Zul"]
    
    # Generate 12 months of high-quality sample data
    for m in range(1, 13):
        date_str = f"{year}-{m:02d}-15"
        
        # Month 1: Basic
        basic_lines.append(MockInvoiceLine("Sunny Tan", "Eternalgy Corp", f"INV-{year}{m:02d}01", date_str, date_str, 80000.0, 5000.0, "Executive", 0.03, 75000.0 * 0.03))
        
        if m % 2 == 1:
            basic_lines.append(MockInvoiceLine("Martin Hing", "Aero Solar", f"INV-{year}{m:02d}02", date_str, date_str, 60000.0, 0.0, "Senior", 0.0325, 60000.0 * 0.0325))
            basic_lines.append(MockInvoiceLine("Martin Hing", "Aero Solar", f"INV-{year}{m:02d}03", date_str, date_str, 40000.0, 0.0, "Senior", 0.0325, 40000.0 * 0.0325))
        else:
            basic_lines.append(MockInvoiceLine("Martin Hing", "Aero Solar", f"INV-{year}{m:02d}02", date_str, date_str, 100000.0, 0.0, "Senior", 0.0325, 100000.0 * 0.0325))
            
        basic_lines.append(MockInvoiceLine("Teng Kah Kent", "EcoTech Solution", f"INV-{year}{m:02d}04", date_str, date_str, 120000.0, 10000.0, "Senior", 0.0325, 110000.0 * 0.0325))
        
        # Month 2: ANP
        anp_detail.append({
            "agent_name": "Sunny Tan",
            "customer_name": "Eternalgy Corp",
            "invoice_number": f"INV-{year}{m:02d}01",
            "invoice_date": date_str,
            "invoice_total_amount": Decimal("80000.00"),
            "sales_price": Decimal("75000.00"),
            "anp_commission_accumulated_tier": Decimal("500.00"),
            "anp_commission_date": f"{year}-{m:02d}",
            "anp_commission_date_remark": "Paid next month"
        })
        
        if m % 2 == 1:
            anp_detail.append({
                "agent_name": "Martin Hing",
                "customer_name": "Aero Solar",
                "invoice_number": f"INV-{year}{m:02d}02",
                "invoice_date": date_str,
                "invoice_total_amount": Decimal("60000.00"),
                "sales_price": Decimal("60000.00"),
                "anp_commission_accumulated_tier": Decimal("500.00"),
                "anp_commission_date": f"{year}-{m:02d}",
                "anp_commission_date_remark": "Paid next month"
            })
            anp_detail.append({
                "agent_name": "Martin Hing",
                "customer_name": "Aero Solar",
                "invoice_number": f"INV-{year}{m:02d}03",
                "invoice_date": date_str,
                "invoice_total_amount": Decimal("40000.00"),
                "sales_price": Decimal("40000.00"),
                "anp_commission_accumulated_tier": Decimal("500.00"),
                "anp_commission_date": f"{year}-{m:02d}",
                "anp_commission_date_remark": "Paid next month"
            })
        else:
            anp_detail.append({
                "agent_name": "Martin Hing",
                "customer_name": "Aero Solar",
                "invoice_number": f"INV-{year}{m:02d}02",
                "invoice_date": date_str,
                "invoice_total_amount": Decimal("100000.00"),
                "sales_price": Decimal("100000.00"),
                "anp_commission_accumulated_tier": Decimal("500.00"),
                "anp_commission_date": f"{year}-{m:02d}",
                "anp_commission_date_remark": "Paid next month"
            })
            
        anp_detail.append({
            "agent_name": "Teng Kah Kent",
            "customer_name": "EcoTech Solution",
            "invoice_number": f"INV-{year}{m:02d}04",
            "invoice_date": date_str,
            "invoice_total_amount": Decimal("120000.00"),
            "sales_price": Decimal("110000.00"),
            "anp_commission_accumulated_tier": Decimal("500.00"),
            "anp_commission_date": f"{year}-{m:02d}",
            "anp_commission_date_remark": "Paid next month"
        })
        
        # Month 3: NFP
        nfp_rows.append(MockInvoiceCommission("Zul", "Sina Energy", f"INV-{year}{m:02d}05", date_str, 95000.0, 5000.0, 90000.0, 85000.0, 80000.0, 2500.0, full_payment_date=date_str))
        nfp_rows.append(MockInvoiceCommission("Sunny Tan", "Sina Energy", f"INV-{year}{m:02d}06", date_str, 110000.0, 0.0, 110000.0, 105000.0, 100000.0, 2500.0))
        
    return {
        "basic_lines": basic_lines,
        "anp_detail": anp_detail,
        "nfp_rows": nfp_rows
    }


def _get_mock_data_for_fetching(year: int, h1_only: bool = False) -> dict[str, Any]:
    mock_raw = get_mock_data(year)
    basic_lines = mock_raw["basic_lines"]
    anp_detail = mock_raw["anp_detail"]
    nfp_rows = [r for r in mock_raw["nfp_rows"] if r.full_payment_date]
    
    if h1_only:
        basic_lines = [ln for ln in basic_lines if _parse_month(ln.invoice_date) in (1, 2, 3, 4, 5, 6)]
        anp_detail = [r for r in anp_detail if _parse_month(r.get("invoice_date")) in (1, 2, 3, 4, 5, 6)]
        nfp_rows = [r for r in nfp_rows if _parse_month(r.invoice_date) in (1, 2, 3, 4, 5, 6)]
    
    # Basic
    basic_total_sales = defaultdict(float)
    basic_total_comm = defaultdict(float)
    basic_t2 = []
    for ln in basic_lines:
        basic_total_sales[ln.agent_name] += float(ln.total_amount)
        basic_total_comm[ln.agent_name] += float(ln.basic_commission)
        basic_t2.append([
            ln.agent_name, ln.customer_name, ln.invoice_number, str(ln.invoice_date),
            f"{ln.epp_interest:,.2f}", f"{ln.total_amount:,.2f}", f"{ln.basic_commission:,.2f}"
        ])
    basic_t1 = []
    for agent in sorted(basic_total_sales.keys()):
        basic_t1.append([
            agent, f"{basic_total_sales[agent]:,.2f}", f"{basic_total_comm[agent]:,.2f}"
        ])
    basic_meta = {
        "agents": len(basic_t1),
        "invoices": len(basic_t2),
        "total_commission": Decimal(str(sum(basic_total_comm.values()))),
        "filter": "mock data",
    }
    basic_t3 = []
    
    # ANP
    anp_agent_sales = defaultdict(float)
    anp_agent_monthly_comm = defaultdict(lambda: defaultdict(float))
    for r in anp_detail:
        agent = r["agent_name"]
        m = _parse_month(r.get("invoice_date"))
        anp_agent_sales[agent] += float(r["invoice_total_amount"])
        comm = float(r.get("anp_commission_accumulated_tier", 0.0))
        if m and comm > anp_agent_monthly_comm[agent][m]:
            anp_agent_monthly_comm[agent][m] = comm
            
    anp_agent_comm = defaultdict(float)
    for agent, monthly_comms in anp_agent_monthly_comm.items():
        anp_agent_comm[agent] = sum(monthly_comms.values())
        
    anp_summary = []
    for agent in sorted(anp_agent_sales.keys()):
        anp_summary.append({
            "payout_period": f"invoice-year-{year}",
            "agent_name": agent,
            "agent_bubble_id": "mock_id",
            "agent_type": "internal",
            "invoice_count": 4,
            "accumulated_total_amount": Decimal(str(anp_agent_sales[agent])),
            "anp_commission": Decimal(str(anp_agent_comm[agent]))
        })
    anp_meta = {
        "agents": len(anp_agent_sales),
        "invoices": len(anp_detail),
        "total_commission": sum(Decimal(str(r["anp_commission"])) for r in anp_summary),
        "filter": "1st payment secured; internal (MOCK)",
    }
    
    # NFP
    nfp_agent_sales = defaultdict(float)
    nfp_agent_sys = defaultdict(float)
    nfp_agent_nfp = defaultdict(float)
    nfp_agent_comm = defaultdict(float)
    for r in nfp_rows:
        nfp_agent_sales[r.agent_name] += r.sales_price
        nfp_agent_sys[r.agent_name] += r.system_price
        nfp_agent_nfp[r.agent_name] += r.net_floor_price if r.net_floor_price else 0.0
        nfp_agent_comm[r.agent_name] += r.nfp_commission
        
    nfp_agent_summary_table = []
    for agent in sorted(nfp_agent_sales.keys()):
        nfp_agent_summary_table.append([
            agent, f"{nfp_agent_sales[agent]:,.2f}", f"{nfp_agent_sys[agent]:,.2f}",
            f"{nfp_agent_nfp[agent]:,.2f}", f"{nfp_agent_comm[agent]:,.2f}"
        ])
        
    nfp_detail_table = []
    for r in nfp_rows:
        nfp_detail_table.append([
            r.agent_name, r.customer_name, r.invoice_number, str(r.invoice_date),
            "No", f"{r.epp_cost:,.2f}", f"{r.sales_price:,.2f}", f"{r.system_price:,.2f}",
            f"{r.net_floor_price:,.2f}" if r.net_floor_price else "-", f"{r.nfp_commission:,.2f}",
            f"{nfp_agent_comm[r.agent_name]:,.2f}"
        ])
    nfp_meta = {
        "agents": len(nfp_agent_sales),
        "invoices": len(nfp_rows),
        "total_commission": Decimal(str(sum(nfp_agent_comm.values()))),
        "filter": "payment 0-100%; internal (MOCK)",
    }
    
    basic_t4 = []
    return {
        "basic": (basic_t1, basic_t2, basic_t3, basic_t4, basic_meta, basic_lines),
        "anp": (anp_summary, anp_detail, anp_meta),
        "nfp": (nfp_agent_summary_table, nfp_detail_table, nfp_meta, nfp_rows)
    }


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    script_dir = str(path.resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _clean_token(raw: str) -> str:
    token = raw.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _read_token_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return _clean_token(path.read_text(encoding="utf-8"))


def _is_valid_jwt(token: str) -> bool:
    return len(token.split(".")) == 3 and len(token) >= 100


def _load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (
        REPO_ROOT / "1. Basic Commission" / "3. Python Script" / ".env",
        REPO_ROOT / "3. ANP Commission" / "3. Python Script" / ".env",
        REPO_ROOT / "3. ANP Commission" / ".env",
        REPO_ROOT / ".env",
    ):
        if candidate.is_file():
            load_dotenv(candidate, override=True)


def _resolve_proxy_credentials() -> tuple[str, str, str]:
    for key in ("PG_PROXY_TOKEN", "POSTGRES_PROXY_TOKEN"):
        if not _is_valid_jwt(_clean_token(os.environ.get(key, ""))):
            os.environ.pop(key, None)

    _load_env_files()

    token = ""
    for key in ("PG_PROXY_TOKEN", "POSTGRES_PROXY_TOKEN"):
        token = _clean_token(os.environ.get(key, ""))
        if _is_valid_jwt(token):
            break
        token = ""

    if not token:
        for path in (
            REPO_ROOT / "2. NFP Commission" / "4. data" / "pg_proxy_token.txt",
            REPO_ROOT / "pg_proxy_token.txt",
            REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "pg_proxy_token.txt",
        ):
            token = _read_token_file(path)
            if _is_valid_jwt(token):
                break
            token = ""

    url = (
        os.environ.get("PG_PROXY_URL", "").strip()
        or os.environ.get("POSTGRES_PROXY_URL", "").strip()
    )
    if url and not url.rstrip("/").endswith("/api/sql"):
        url = url.rstrip("/") + "/api/sql"
    if not url:
        url = DEFAULT_PROXY_URL

    db_name = (
        os.environ.get("PG_PROXY_DB", "").strip()
        or os.environ.get("PG_DB_NAME", "").strip()
        or DEFAULT_DB_NAME
    )

    if token:
        os.environ["PG_PROXY_TOKEN"] = token
        os.environ["POSTGRES_PROXY_TOKEN"] = token
    os.environ["PG_PROXY_URL"] = url
    os.environ["POSTGRES_PROXY_URL"] = url
    os.environ["PG_PROXY_DB"] = db_name
    os.environ["PG_DB_NAME"] = db_name

    return token, url, db_name


def _token_help_message() -> str:
    return (
        "Missing database proxy token.\n"
        "Use ONE of these (token only — no 'Bearer ' prefix):\n"
        f"  1. {REPO_ROOT / '.env'}\n"
        "       PG_PROXY_TOKEN=your_jwt_here\n"
        f"  2. {REPO_ROOT / 'pg_proxy_token.txt'}\n"
        "       (single line with the JWT)\n"
        f"  3. {REPO_ROOT / '1. Basic Commission' / '3. Python Script' / '.env'}\n"
        f"  4. {REPO_ROOT / '3. ANP Commission' / '.env'}\n"
        f"  5. {REPO_ROOT / '2. NFP Commission' / '4. data' / 'pg_proxy_token.txt'}\n"
        "Get a fresh JWT from your Postgres proxy admin if you see 'Token expired'."
    )


def _style_sheet(ws, headers: list[str], *, money_cols: set[int] | None = None) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    money_cols = money_cols or set()
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
            if col_idx in money_cols and isinstance(cell.value, (int, float, Decimal)):
                cell.value = float(cell.value)
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")

    for col_idx in range(1, len(headers) + 1):
        letter = get_column_letter(col_idx)
        max_len = len(str(headers[col_idx - 1]))
        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, min(len(str(val)), 48))
        ws.column_dimensions[letter].width = max_len + 2
    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


# Shared agent full-name resolver, sourced from the dashboard's Agent Roles &
# Hierarchy page (agent_roles table). Applied to Agent columns at sheet-write
# time only, so joins keyed on raw eeAdmin names upstream are unaffected.
try:
    import agent_names as _agent_names
except Exception:
    _agent_names = None


def _resolve_agent_columns(headers: list[str], rows: list[list[Any]]) -> list[list[Any]]:
    if not _agent_names:
        return rows
    cols = [i for i, h in enumerate(headers)
            if isinstance(h, str) and "agent" in h.lower()]
    if not cols:
        return rows
    out = []
    for row in rows:
        row = list(row)
        for i in cols:
            if i < len(row):
                v = row[i]
                if isinstance(v, str) and v.strip() and v.strip() not in ("-", "Total"):
                    row[i] = _agent_names.resolve(v.strip())
        out.append(row)
    return out


def _append_sheet(wb, title: str, headers: list[str], rows: list[list[Any]], money_cols: set[int]) -> None:
    safe_title = title[:31]
    ws = wb.create_sheet(safe_title)
    ws.append(headers)
    for row in _resolve_agent_columns(headers, rows):
        out: list[Any] = []
        for i, cell in enumerate(row):
            if (i + 1) in money_cols:
                try:
                    out.append(float(str(cell).replace(",", "")))
                except (TypeError, ValueError):
                    out.append(cell)
            else:
                out.append(cell)
        ws.append(out)
    _style_sheet(ws, headers, money_cols=money_cols)


def _money_sum(values: list[Any]) -> Decimal:
    total = Decimal("0")
    for v in values:
        try:
            total += Decimal(str(v).replace(",", ""))
        except Exception:
            pass
    return total


def _parse_month(date_val: Any) -> int | None:
    if not date_val:
        return None
    if isinstance(date_val, (date, datetime)):
        return date_val.month
    s = str(date_val).strip()
    m = re.search(r'\b\d{4}[-/](\d{2})[-/]\d{2}', s)
    if m:
        return int(m.group(1))
    m2 = re.match(r'^(\d{4})[-/](\d{2})', s)
    if m2:
        return int(m2.group(2))
    return None


def _effective_nfp_date(r: Any) -> str | None:
    pct75 = getattr(r, "pct75_date", None)
    if not pct75 and isinstance(r, dict):
        pct75 = r.get("pct75_date")
    
    full_pay = getattr(r, "full_payment_date", None)
    if not full_pay and isinstance(r, dict):
        full_pay = r.get("full_payment_date")
        
    pct75_str = str(pct75)[:10] if pct75 else None
    full_pay_str = str(full_pay)[:10] if full_pay else None
    
    if pct75_str:
        parts = pct75_str.split("-")
        if len(parts) >= 2:
            try:
                y = int(parts[0])
                m = int(parts[1])
                if y > 2026 or (y == 2026 and m >= 7):
                    return pct75_str
                
                # Transition check:
                if y < 2026 or (y == 2026 and m < 7):
                    if not full_pay_str:
                        return "2026-07-01"
                    fp_parts = full_pay_str.split("-")
                    if len(fp_parts) >= 2:
                        fpy = int(fp_parts[0])
                        fpm = int(fp_parts[1])
                        if fpy > 2026 or (fpy == 2026 and fpm >= 7):
                            return "2026-07-01"
            except ValueError:
                pass
                
    return full_pay_str


def _basic_commission_payout_for_month(ln: Any, m: int) -> float:
    # m is 1..12 representing the month we are querying payout for.
    comm_val = 0.0
    basic_comm_attr = getattr(ln, "basic_commission", None)
    if basic_comm_attr is None and isinstance(ln, dict):
        basic_comm_attr = ln.get("basic_commission")
    if basic_comm_attr is not None:
        try:
            comm_val = float(basic_comm_attr)
        except (ValueError, TypeError):
            pass

    first_pay = getattr(ln, "first_payment_date", None)
    if not first_pay and isinstance(ln, dict):
        first_pay = ln.get("first_payment_date")
    
    full_pay = getattr(ln, "full_payment_date", None)
    if not full_pay and isinstance(ln, dict):
        full_pay = ln.get("full_payment_date")

    first_pay_month = _parse_month(first_pay)
    full_pay_month = _parse_month(full_pay)
    
    if m < 7:
        if full_pay_month == m:
            return comm_val
        return 0.0
    
    payout_m = 0.0
    if first_pay_month == m:
        payout_m += 300.0
    if full_pay_month == m:
        if first_pay_month and first_pay_month >= 7:
            payout_m += (comm_val - 300.0)
        else:
            payout_m += comm_val
            
    return payout_m


def compute_table_spans(rows: list[list[Any]], start_row: int = 2) -> tuple[list[list[Any]], list[tuple[str, tuple[int, int], tuple[int, int]]]]:
    cleaned_rows = [list(r) for r in rows]
    span_commands = []
    n_rows = len(cleaned_rows)
    if n_rows <= 1:
        return cleaned_rows, span_commands

    agent_start = 0
    while agent_start < n_rows:
        agent_end = agent_start
        while agent_end + 1 < n_rows and cleaned_rows[agent_end + 1][0] == cleaned_rows[agent_start][0]:
            agent_end += 1

        if agent_end > agent_start:
            span_commands.append(('SPAN', (0, agent_start + start_row), (0, agent_end + start_row)))
            for r in range(agent_start + 1, agent_end + 1):
                cleaned_rows[r][0] = ""

        cust_start = agent_start
        while cust_start <= agent_end:
            cust_end = cust_start
            while (cust_end + 1 <= agent_end and 
                   cleaned_rows[cust_end + 1][1] == cleaned_rows[cust_start][1]):
                cust_end += 1

            if cust_end > cust_start:
                span_commands.append(('SPAN', (1, cust_start + start_row), (1, cust_end + start_row)))
                span_commands.append(('SPAN', (2, cust_start + start_row), (2, cust_end + start_row)))
                for r in range(cust_start + 1, cust_end + 1):
                    cleaned_rows[r][1] = ""
                    cleaned_rows[r][2] = ""

            cust_start = cust_end + 1

        agent_start = agent_end + 1

    return cleaned_rows, span_commands


def _role_table_senior(agent_name: str, month: int | None = None):
    """(listed, reports_to) from the dashboard role table; the hardcoded map
    below is the fallback only for agents not yet entered there. Listed with a
    blank Reports To means "nobody"."""
    try:
        import basic_commission_rates as _bcr
        return _bcr.get_reporting_senior_from_table(agent_name, month or 7)
    except Exception:
        return False, None


def get_reporting_senior(agent_name: str, month: int | None = None) -> str | None:
    listed, senior = _role_table_senior(agent_name, month)
    if listed:
        return senior
    n = agent_name.lower().strip()
    seniors = ["sunny", "martin", "kent", "zhe hang"]
    if any(s in n for s in seniors):
        return None
    if any(tok in n for tok in ["louis ng", "anisah najwa", "anisah"]):
        return "Teng Kah Kent"
    if any(tok in n for tok in ["jia keat", "zul", "zulkarnain", "denise", "jia xuan", "ah zu"]):
        return "Sunny Tan"
    if "joshua" in n:
        return "CHING ZHE HANG"
    if n.startswith('j') and not any(ex in n for ex in ["joshua", "jia keat", "jia xuan"]):
        return "MARTIN HING"
    return None

def get_override_rate(agent_name: str) -> float:
    if get_reporting_senior(agent_name):
        return 0.0025
    return 0.0


def get_basic_monthly_tables(lines: list[Any]) -> dict[int, list[list[str]]]:
    monthly_groups = {}
    for m in range(1, 13):
        m_lines = []
        for ln in lines:
            payout = _basic_commission_payout_for_month(ln, m)
            if payout > 0:
                m_lines.append((ln, payout))
                
        if not m_lines:
            continue
            
        m_lines.sort(key=lambda x: (x[0].agent_name.strip().lower(), x[0].customer_name.strip().lower()))
        
        cust_counts = defaultdict(int)
        for ln, payout in m_lines:
            key = (ln.agent_name.strip().lower(), ln.customer_name.strip().lower())
            cust_counts[key] += 1
            
        rows = []
        for ln, payout in m_lines:
            key = (ln.agent_name.strip().lower(), ln.customer_name.strip().lower())
            count = cust_counts[key]
            override_val = get_override_rate(ln.agent_name)
            
            first_pay_m = _parse_month(getattr(ln, "first_payment_date", ""))
            full_pay_m = _parse_month(getattr(ln, "full_payment_date", ""))
            
            rate_val = float(ln.rate) * 100
            sharing_val = float(getattr(ln, "profit_sharing", 0.0))
            sharing_str = f" + {sharing_val*100:.2f}%" if sharing_val > 0 else ""
            
            if m >= 7:
                if first_pay_m == m and full_pay_m == m:
                    rate_str = f"{rate_val:.2f}%{sharing_str}"
                elif first_pay_m == m:
                    rate_str = "1st Pay (RM300)"
                elif full_pay_m == m:
                    rate_str = f"{rate_val:.2f}%{sharing_str} Bal"
                else:
                    rate_str = f"{rate_val:.2f}%{sharing_str}"
            else:
                rate_str = f"{rate_val:.2f}%{sharing_str}"
                
            rows.append([
                ln.agent_name,
                ln.customer_name,
                str(count),
                f"{ln.sales_price:,.2f}",
                rate_str,
                f"{override_val * 100:.2f}%" if override_val > 0 else "-",
                f"{payout:,.2f}"
            ])
        monthly_groups[m] = rows
    return monthly_groups


def get_nfp_monthly_tables(nfp_rows: list[Any]) -> dict[int, list[list[str]]]:
    monthly_groups = {}
    for m in range(1, 13):
        m_rows = [r for r in nfp_rows if _parse_month(_effective_nfp_date(r)) == m]
        if not m_rows:
            continue
            
        m_rows.sort(key=lambda x: (x.agent_name.strip().lower(), x.customer_name.strip().lower(), str(_effective_nfp_date(x))))
        
        cust_counts = defaultdict(int)
        for r in m_rows:
            key = (r.agent_name.strip().lower(), r.customer_name.strip().lower())
            cust_counts[key] += 1
            
        rows = []
        for r in m_rows:
            key = (r.agent_name.strip().lower(), r.customer_name.strip().lower())
            count = cust_counts[key]
            
            net_floor = r.net_floor_price
            if net_floor is not None:
                if r.sales_price > net_floor:
                    rate_str = "25.00%"
                elif r.sales_price < net_floor:
                    rate_str = "20.00%"
                else:
                    rate_str = "-"
            else:
                rate_str = "-"
                
            rows.append([
                r.agent_name,
                r.customer_name,
                str(count),
                f"{r.sales_price:,.2f}",
                f"{r.system_price:,.2f}",
                f"{net_floor:,.2f}" if net_floor is not None else "-",
                rate_str,
                f"{r.nfp_commission:,.2f}"
            ])
        monthly_groups[m] = rows
    return monthly_groups


def get_anp_monthly_tables(anp_detail: list[dict[str, Any]]) -> dict[int, list[list[str]]]:
    monthly_groups = {}
    for m in range(1, 13):
        m_rows = [r for r in anp_detail if _parse_month(r.get("invoice_date")) == m]
        if not m_rows:
            continue
            
        m_rows.sort(key=lambda x: (x.get("agent_name", "").strip().lower(), x.get("customer_name", "").strip().lower(), str(x.get("invoice_date"))))
        
        cust_counts = defaultdict(int)
        for r in m_rows:
            key = (r.get("agent_name", "").strip().lower(), r.get("customer_name", "").strip().lower())
            cust_counts[key] += 1
            
        rows = []
        for r in m_rows:
            key = (r.get("agent_name", "").strip().lower(), r.get("customer_name", "").strip().lower())
            count = cust_counts[key]
            rows.append([
                r.get("agent_name"),
                r.get("customer_name"),
                str(count),
                f"{float(r.get('sales_price') or 0.0):,.2f}",
                f"{float(r.get('anp_commission_accumulated_tier') or 0.0):,.2f}"
            ])
        monthly_groups[m] = rows
    return monthly_groups


def write_excel_stacked_details(ws, title_suffix: str, headers: list[str], monthly_tables: dict[int, list[list[str]]], money_cols: set[int]) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    
    header_fill = PatternFill("solid", fgColor="1F4E79")
    sub_header_fill = PatternFill("solid", fgColor="2F5597")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    sub_header_font = Font(bold=True, color="FFFFFF", size=9.5)
    thin_side = Side(style="thin", color="CCCCCC")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    
    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    
    current_row = 1
    
    for m in range(1, 13):
        rows = monthly_tables.get(m)
        if not rows:
            continue
            
        month_title = f"{month_names[m-1]} - {title_suffix}"
        
        # Month Header
        ws.row_dimensions[current_row].height = 24
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=current_row, column=c)
            cell.border = border
            if c == 1:
                cell.value = month_title
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.fill = header_fill
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(headers))
        current_row += 1
        
        # Sub-headers
        ws.row_dimensions[current_row].height = 20
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=current_row, column=c, value=h)
            cell.font = sub_header_font
            cell.fill = sub_header_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        current_row += 1
        
        # Data
        data_start_row = current_row
        for row in rows:
            ws.row_dimensions[current_row].height = 18
            for c, val in enumerate(row, start=1):
                cell = ws.cell(row=current_row, column=c)
                cell.border = border
                
                if c in money_cols:
                    try:
                        cell.value = float(str(val).replace(",", "").replace("RM", "").strip())
                        cell.number_format = "#,##0.00"
                        cell.alignment = Alignment(horizontal="right", vertical="center")
                    except ValueError:
                        cell.value = val
                        cell.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    cell.value = val
                    if "count" in headers[c-1].lower() or "no." in headers[c-1].lower() or "%" in val or "rate" in headers[c-1].lower():
                        cell.alignment = Alignment(horizontal="center", vertical="center")
                    else:
                        cell.alignment = Alignment(horizontal="left", vertical="center")
            current_row += 1
            
        data_end_row = current_row - 1
        
        # Merge cell blocks
        raw_data = [list(r) for r in rows]
        cleaned_data, spans = compute_table_spans(raw_data, start_row=data_start_row)
        
        for cmd, (start_col, start_r), (end_col, end_r) in spans:
            for r in range(start_r + 1, end_r + 1):
                ws.cell(row=r, column=start_col+1).value = None
            ws.merge_cells(start_row=start_r, start_column=start_col+1, end_row=end_r, end_column=end_col+1)
            
        current_row += 1
        
    for c in range(1, len(headers) + 1):
        letter = get_column_letter(c)
        max_len = len(str(headers[c - 1]))
        ws.column_dimensions[letter].width = max(max_len + 4, 15)


def fetch_basic(year: int, h1_only: bool = False) -> tuple[list[list[str]], list[list[str]], list[list[str]], list[list[str]], dict[str, Any], list[Any]]:
    if MOCK_MODE:
        return [], [], [], [], {"agents":0, "invoices":0, "total_commission":0, "filter":""}, []

    out_dir = REPO_ROOT / "1. Basic Commission" / "3. Python Script"
    out_path = out_dir / "outsource_basic_commission.py"
    basic = _load_module("outsource_basic_commission", out_path)
    
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or basic._env("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError(_token_help_message())

    proxy_url = basic._normalize_proxy_url(
        basic._env("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
    )
    db_name = basic._env("PG_PROXY_DB") or basic._env("PG_DB_NAME", "prod_main")

    payload = basic._proxy_sql(
        proxy_url=proxy_url,
        db_name=db_name,
        token=token,
        sql=basic._invoices_sql(year=year),
        params=[]
    )
    raw_rows = list(payload.get("rows") or [])
    
    from decimal import Decimal
    from collections import defaultdict
    processed_outsource_invoices = []
    processed_outsource_factory = []
    agent_own_commissions = defaultdict(Decimal)
    agent_sales = defaultdict(Decimal)
    override_commissions = defaultdict(Decimal)
    
    for r in raw_rows:
        agent_name = str(r.get("agent_name") or "(unknown)").strip()
        agent_comm_field = r.get("agent_comm_field")
        
        info = basic.get_agent_hierarchy_info(agent_name)
        # Agent Roles & Hierarchy page first, then Postgres' agent_type — see
        # outsource_basic_commission.is_outsource_agent().
        if not basic.is_outsource_agent(agent_name, r.get("agent_type"), r.get("invoice_date")):
            continue

        if not info:
            info = {"canonical_name": agent_name, "tier": "OSA", "osa_parent": None, "oum_parent": None}
            
        canonical_name = info["canonical_name"]
        customer_name = str(r.get("customer_name") or "(unknown)").strip()
        invoice_num = str(r.get("invoice_number") or "").strip()
        prop_type = basic.classify_property_type(r)
        
        total = basic._to_decimal(r.get("total_amount"))
        paid_amount = basic._to_decimal(r.get("paid_amount"))
        epp = basic._to_decimal(r.get("epp_interest"))
        sales_price = total - epp
        
        inv_dt = str(r.get("invoice_date") or "")[:10]
        real_full_pay_str = str(r.get("real_full_payment_date") or "")[:10]
        # The milestone columns are named per Data-page threshold now
        # (pct_75_date etc.); read the balance date via the policy helpers.
        from basic_commission_rates import multi_stage_cutover as _cutover, \
            milestone_date as _milestone_date
        _cut = _cutover(year)
        pct75_str = _milestone_date(r, _cut[1].balance_trigger if _cut else Decimal("100"))
        pay_dt = basic._get_effective_payment_date(real_full_pay_str, pct75_str)
        
        ref_name = r.get("referral_name")
        
        if h1_only:
            m = _parse_month(pay_dt)
            if not m or m > 6:
                continue
        
        if prop_type == "Factory":
            rate = Decimal("0.02")
            sharing = Decimal("0") # fallback
            
            osa_sharing = sharing
            if info["tier"] in ("OSA", "OSA 1") and sharing > 0:
                osa_sharing = sharing * Decimal("0.70")
                oum_p = info.get("oum_parent")
                if oum_p:
                    override_commissions[oum_p] += sales_price * sharing * Decimal("0.20")
                override_commissions["OGM Pool"] += sales_price * sharing * Decimal("0.10")
                
            comm = sales_price * (rate + osa_sharing)
            
            processed_outsource_factory.append(
                basic.OutsourceFactoryInvoiceLine(
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
                    referral_name=ref_name
                )
            )
        else:
            rate = basic.get_own_commission_rate(info, agent_comm_field, pay_dt)
            comm = sales_price * rate
            
            processed_outsource_invoices.append(
                basic.OutsourceInvoiceLine(
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
                    referral_name=ref_name
                )
            )
            
        agent_sales[canonical_name] += total
        agent_own_commissions[canonical_name] += comm
        
        tier = info["tier"]
        if tier == "OSA 1":
            oum_p = info.get("oum_parent")
            if oum_p: override_commissions[oum_p] += total * Decimal("0.005")
        elif tier == "OSA":
            oum_p = info.get("oum_parent")
            internal_senior = info.get("internal_senior_parent")
            if oum_p: override_commissions[oum_p] += total * Decimal("0.005")
            if internal_senior: override_commissions[internal_senior] += total * Decimal("0.005")
            
    all_payout_agents = set(agent_own_commissions.keys()) | set(override_commissions.keys())
    t1_rows = []
    for agent in sorted(all_payout_agents):
        own = agent_own_commissions.get(agent, Decimal("0"))
        ovr = override_commissions.get(agent, Decimal("0"))
        sales = agent_sales.get(agent, Decimal("0"))
        total_payout = own + ovr
        t1_rows.append([agent, f"{sales:,.2f}", f"{own:,.2f}", f"{ovr:,.2f}", f"{total_payout:,.2f}"])
        
    t2_rows = []
    for inv in processed_outsource_invoices:
        t2_rows.append([inv.agent_name, inv.customer_name, inv.invoice_number, inv.package, inv.invoice_date, inv.full_payment_date, f"{inv.total_amount:,.2f}", f"{inv.epp:,.2f}", f"{inv.sales_price:,.2f}", f"{(inv.rate * 100):.2f}%", f"{inv.basic_commission:,.2f}"])
        
    t3_rows = []
    for inv in processed_outsource_factory:
        t3_rows.append([inv.agent_name, inv.customer_name, inv.invoice_number, inv.package, inv.invoice_date, inv.full_payment_date, f"{inv.total_amount:,.2f}", f"{inv.epp:,.2f}", f"{inv.sales_price:,.2f}", f"{(inv.rate * 100):.2f}%", f"{(getattr(inv, 'profit_sharing', 0) * 100):.2f}%", f"{inv.basic_commission:,.2f}"])
        
    t4_rows = []
    for ln in processed_outsource_invoices + processed_outsource_factory:
        if basic._is_valid_referral(ln.referral_name):
            sales_price = ln.sales_price
            rate = basic._referral_rate(ln.invoice_date)
            fee = sales_price * rate
            t4_rows.append([
                ln.referral_name.strip(),
                ln.agent_name.strip(),
                ln.customer_name.strip(),
                ln.invoice_number.strip(),
                ln.invoice_date.strip(),
                ln.full_payment_date.strip(),
                f"{sales_price:,.2f}",
                f"{(rate * 100):.2f}%",
                f"{fee:,.2f}"
            ])
    t4_rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))

    total_comm = sum(Decimal(r[4].replace(',','')) for r in t1_rows if len(r)>4)
    meta = {"agents": len(t1_rows), "invoices": len(t2_rows) + len(t3_rows), "total_commission": total_comm, "filter": "outsource agents"}
    processed_lines = processed_outsource_invoices + processed_outsource_factory
    
    return t1_rows, t2_rows, t3_rows, t4_rows, meta, processed_lines

def fetch_anp(year: int, h1_only: bool = False) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from decimal import Decimal
    return [], [], {"agents": 0, "invoices": 0, "total_commission": Decimal("0"), "filter": "N/A"}



def fetch_nfp(year: int, h1_only: bool = False) -> tuple[list[list[str]], list[list[str]], dict[str, Any], list[Any]]:
    if MOCK_MODE:
        return _get_mock_data_for_fetching(year, h1_only=h1_only)["nfp"]

    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_path = nfp_dir / "outsource_nfp_commission.py"
    nfp = _load_module("outsource_nfp_commission", nfp_path)
    nfp_paths = _load_module("nfp_paths", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    rows, summary = nfp.build_report(year)
    
    # Filter NFP rows for cases with full payment date or pct75 date (for July 2026+)
    rows = [r for r in rows if r.full_payment_date or getattr(r, "pct75_date", None)]
    
    # Filter by month if h1_only
    if h1_only:
        rows = [r for r in rows if _parse_month(_effective_nfp_date(r)) in (1, 2, 3, 4, 5, 6)]
        
    agents_filtered = {r.agent_name for r in rows if r.agent_name}
    accumulated_filtered = {}
    for r in rows:
        if not r.agent_name:
            continue
        if r.agent_name not in accumulated_filtered:
            accumulated_filtered[r.agent_name] = {
                "sales_price": 0.0,
                "system_price": 0.0,
                "net_floor_price": 0.0,
                "nfp_commission": 0.0,
            }
        acc = accumulated_filtered[r.agent_name]
        acc["sales_price"] += r.sales_price
        acc["system_price"] += r.system_price
        acc["net_floor_price"] += r.net_floor_price or 0.0
        acc["nfp_commission"] += r.nfp_commission
    tng_invoices_filtered = [r for r in rows if r.has_tng_rebate]
    summary = {
        "report_year": year,
        "total_qualifying_agents": len(agents_filtered),
        "total_invoices": len(rows),
        "invoices_with_tng_rebate": len(tng_invoices_filtered),
        "total_nfp_commission": round(sum(r.nfp_commission for r in rows), 2),
        "accumulated_by_agent": {
            agent: {k: round(v, 2) for k, v in totals.items()}
            for agent, totals in sorted(accumulated_filtered.items())
        }
    }
    
    agent_rows = nfp.build_agent_summary_table(summary)
    detail_rows = nfp.display_rows_as_lists(rows, summary)
    meta = {
        "agents": summary.get("total_qualifying_agents", 0),
        "invoices": summary.get("total_invoices", 0),
        "total_commission": Decimal(str(summary.get("total_nfp_commission", 0))),
        "filter": "payment 100% or 75% paid (Jul 2026+); invoice_date year; internal/full time" + (" (H1)" if h1_only else ""),
    }
    return agent_rows, detail_rows, meta, rows


def fetch_ega_esa(year: int) -> tuple[list[list[str]], list[list[str]], list[list[str]], list[str], list[str], list[str]]:
    if MOCK_MODE:
        return [], [], [], [], [], []

    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    ega_path = ega_dir / "outsource_EGA_ESA_Awards.py"
    ega = _load_module("outsource_ega_esa", ega_path)
    
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_paths = _load_module("nfp_paths", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
        
    os.environ["POSTGRES_PROXY_TOKEN"] = token
    
    from api_client import query_sql
    sql = ega._invoices_sql(year)
    rows = query_sql(sql)
    
    lines, agent_ep, agent_sales, agent_eligibility = ega.build_report(rows)
    t1 = ega.build_table1(agent_ep, agent_sales, agent_eligibility)
    t2 = ega.build_table2(lines, agent_eligibility)
    t3 = ega.build_table3(lines, agent_eligibility)
    
    return t1, t2, t3, ega.T1_HEADERS, ega.T2_HEADERS, ega.T3_HEADERS


def fetch_production_bonus(year: int) -> dict[str, Any] | None:
    if MOCK_MODE:
        return None

    prod_dir = REPO_ROOT / "5. Production Bonus" / "3. Python Script"
    prod_path = prod_dir / "full_outsource_Production_Bonus.py"
    prod = _load_module("full_outsource_Production_Bonus", prod_path)
    
    return prod.build_report(year)


def aggregate_monthly_metrics(basic_lines: list[Any], anp_detail: list[dict[str, Any]], nfp_rows: list[Any], year: int) -> dict[str, Any]:
    monthly_data = {m: {"month": m, "year": year, "customer_count": 0, "total_commission": 0.0, "total_sales": 0.0, "agent_commissions": {}} for m in range(1, 13)}
    monthly_agent_cust = {m: defaultdict(set) for m in range(1, 13)}
    monthly_split = {m: {"Basic": 0.0, "NFP": 0.0, "ANP": 0.0} for m in range(1, 13)}
    monthly_agent_split = {m: defaultdict(lambda: {"Basic": 0.0, "NFP": 0.0, "ANP": 0.0}) for m in range(1, 13)}
    monthly_agent_sales_map = {m: defaultdict(float) for m in range(1, 13)}
    
    all_agents = set()
    all_customers = set()
    monthly_customers_union = {m: set() for m in range(1, 13)}
    
    # Process Basic
    for line in basic_lines:
        agent = line.agent_name.strip()
        cust = line.customer_name.strip()
        sales = float(line.sales_price)
        
        all_agents.add(agent)
        all_customers.add(cust)
        
        # Sales are recognized in the full payment date month
        full_pay_m = _parse_month(line.full_payment_date)
        if full_pay_m:
            monthly_customers_union[full_pay_m].add(cust)
            monthly_agent_cust[full_pay_m][agent].add(cust)
            monthly_data[full_pay_m]["total_sales"] += sales
            monthly_agent_sales_map[full_pay_m][agent] += sales
            
        # Commission is split
        for m in range(1, 13):
            comm = _basic_commission_payout_for_month(line, m)
            if comm > 0:
                monthly_data[m]["total_commission"] += comm
                monthly_split[m]["Basic"] += comm
                if agent not in monthly_data[m]["agent_commissions"]:
                    monthly_data[m]["agent_commissions"][agent] = 0.0
                monthly_data[m]["agent_commissions"][agent] += comm
                monthly_agent_split[m][agent]["Basic"] += comm
            
    # Process ANP
    # To avoid double-counting the monthly tier commission for each agent,
    # we track the maximum ANP commission achieved per agent per month.
    anp_agent_monthly_comm = defaultdict(lambda: defaultdict(float))
    for r in anp_detail:
        m = _parse_month(r.get("invoice_date"))
        if m:
            agent = r.get("agent_name", "").strip()
            cust = r.get("customer_name", "").strip()
            comm = float(r.get("anp_commission_accumulated_tier", 0.0))
            sales = float(r.get("sales_price", 0.0))
            
            all_agents.add(agent)
            all_customers.add(cust)
            monthly_customers_union[m].add(cust)
            monthly_agent_cust[m][agent].add(cust)
            monthly_agent_sales_map[m][agent] += sales
            
            if comm > anp_agent_monthly_comm[m][agent]:
                anp_agent_monthly_comm[m][agent] = comm
                
    for m, agent_comms in anp_agent_monthly_comm.items():
        for agent, comm in agent_comms.items():
            monthly_data[m]["total_commission"] += comm
            monthly_data[m]["total_sales"] += sales
            monthly_split[m]["ANP"] += comm
            
            if agent not in monthly_data[m]["agent_commissions"]:
                monthly_data[m]["agent_commissions"][agent] = 0.0
            monthly_data[m]["agent_commissions"][agent] += comm
            monthly_agent_split[m][agent]["ANP"] += comm
            
    # Process NFP
    for r in nfp_rows:
        m = _parse_month(_effective_nfp_date(r))
        if m:
            agent = r.agent_name.strip()
            cust = r.customer_name.strip()
            comm = float(r.nfp_commission)
            sales = float(r.sales_price)
            
            all_agents.add(agent)
            all_customers.add(cust)
            monthly_customers_union[m].add(cust)
            monthly_agent_cust[m][agent].add(cust)
            monthly_data[m]["total_commission"] += comm
            monthly_data[m]["total_sales"] += sales
            monthly_split[m]["NFP"] += comm
            monthly_agent_sales_map[m][agent] += sales
            
            if agent not in monthly_data[m]["agent_commissions"]:
                monthly_data[m]["agent_commissions"][agent] = 0.0
            monthly_data[m]["agent_commissions"][agent] += comm
            monthly_agent_split[m][agent]["NFP"] += comm

    for m in range(1, 13):
        monthly_data[m]["customer_count"] = len(monthly_customers_union[m])
        
    monthly_agent_counts = []
    monthly_agent_sales_list = []
    for m in range(1, 13):
        counts = {}
        for agent, cust_set in monthly_agent_cust[m].items():
            counts[agent] = len(cust_set)
        monthly_agent_counts.append(counts)
        monthly_agent_sales_list.append(dict(monthly_agent_sales_map[m]))
        
    monthly_data_list = [monthly_data[m] for m in range(1, 13)]
    monthly_split_list = [monthly_split[m] for m in range(1, 13)]
    
    # Convert monthly_agent_split defaultdicts to standard dicts
    monthly_agent_split_dicts = {}
    for m in range(1, 13):
        monthly_agent_split_dicts[m] = {agent: dict(splits) for agent, splits in monthly_agent_split[m].items()}
        
    return {
        "monthly_trend": monthly_data_list,
        "monthly_agent_customers": monthly_agent_counts,
        "monthly_agent_sales": monthly_agent_sales_list,
        "agent_names": sorted(list(all_agents)),
        "monthly_commission_split": monthly_split_list,
        "monthly_agent_commission_split": monthly_agent_split_dicts,
        "total_agents": len(all_agents),
        "total_customers": len(all_customers)
    }


def build_workbook(year: int, output_path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    print(f"Building finance pack for {year}...")
    basic_t1, basic_t2, basic_t3, basic_t4, basic_meta, basic_lines = fetch_basic(year)
    print(f"  Basic: {basic_meta['agents']} agents, {basic_meta['invoices']} invoices")

    anp_summary, anp_detail, anp_meta = fetch_anp(year)
    print(f"  ANP: {anp_meta['agents']} agents, {anp_meta['invoices']} invoices")

    nfp_agent, nfp_detail, nfp_meta, nfp_rows = fetch_nfp(year)
    print(f"  NFP: {nfp_meta['agents']} agents, {nfp_meta['invoices']} invoices")

    ega_t1, ega_t2, ega_t3, ega_h1, ega_h2, ega_h3 = fetch_ega_esa(year)
    print(f"  EGA/ESA: {len(ega_t1)} agents, {len(ega_t2) + len(ega_t3)} invoices")

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "Finance Summary"
    ws0["A1"] = f"Outsource Commission Pack — calendar year {year}"
    ws0["A1"].font = Font(bold=True, size=14)
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overview = [
        ("Generated at", run_at),
        ("Data year", str(year)),
        ("", ""),
        ("Report", "Agents", "Invoices", "Total commission (RM)", "Main filter"),
        (
            "Basic",
            basic_meta["agents"],
            basic_meta["invoices"],
            float(basic_meta["total_commission"]),
            basic_meta["filter"],
        ),
        (
            "ANP",
            anp_meta["agents"],
            anp_meta["invoices"],
            float(anp_meta["total_commission"]),
            anp_meta["filter"],
        ),
        (
            "NFP",
            nfp_meta["agents"],
            nfp_meta["invoices"],
            float(nfp_meta["total_commission"]),
            nfp_meta["filter"],
        ),
    ]
    grand = basic_meta["total_commission"] + anp_meta["total_commission"] + nfp_meta["total_commission"]
    overview.append(("", "", "", "", ""))
    overview.append(("Grand total (RM)", "", "", float(grand), "Sum of three reports"))
    overview.append(
        (
            "Note",
            "",
            "",
            "",
            "Snapshot at run time. Re-run monthly to include new paid invoices.",
        )
    )
    for r, row in enumerate(overview, start=3):
        for c, val in enumerate(row, start=1):
            ws0.cell(row=r, column=c, value=val)

    # Agent summary sheets
    _append_sheet(
        wb,
        "Basic - By Agent",
        ["Agent Name", "Total Sales", "Own Commission", "Senior Override Commission", "Total Commission Payout"],
        basic_t1,
        {2, 3, 4, 5},
    )
    _append_sheet(
        wb,
        "NFP - By Agent",
        ["Agent Name", "Sales Price (RM)", "System Price (RM)", "Net Floor Price (RM)", "Accumulated NFP Commission (RM)"],
        nfp_agent,
        {2, 3, 4, 5},
    )
    ws_basic_det = wb.create_sheet("Basic - By Invoice")
    write_excel_stacked_details(
        ws_basic_det,
        "Basic Commission",
        ["Agent Name", "Customer Name", "Count of Customer", "Sales Price", "Commission Rate", "Override Commission Rate", "Basic Commission"],
        get_basic_monthly_tables(basic_lines),
        {4, 7}
    )

    ws_nfp_det = wb.create_sheet("NFP - By Invoice")
    write_excel_stacked_details(
        ws_nfp_det,
        "NFP Commission",
        ["Agent Name", "Customer Name", "Count of Customer", "Sales Price", "System Price", "Net Floor Price", "Commission Rate", "NFP Commission"],
        get_nfp_monthly_tables(nfp_rows),
        {4, 5, 6, 8}
    )



    if ega_t1:
        _append_sheet(wb, "EGA ESA - Summary", ega_h1, ega_t1, {2, 3})
    if ega_t3:
        _append_sheet(wb, "EGA ESA - Factory", ega_h3, ega_t3, {6, 7})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


def _fmt_rm(amount: Any) -> str:
    try:
        return f"{float(Decimal(str(amount).replace(',', ''))):,.2f}"
    except Exception:
        return str(amount)


def _rows_to_str(rows: list[list[Any]]) -> list[list[str]]:
    return [[str(c) if c is not None else "" for c in row] for row in rows]


def _parse_rm(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").replace("RM", "").strip())
    except Exception:
        return Decimal("0")


def _combined_agent_totals(
    basic_t1: list[list[Any]],
    anp_summary_table: list[list[str]],
    nfp_agent: list[list[Any]],
    agent_customers: dict[str, int] = None,
) -> list[tuple[str, float, float, float, float, int]]:
    from collections import defaultdict

    buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"basic": Decimal("0"), "anp": Decimal("0"), "nfp": Decimal("0")}
    )

    def key(name: str) -> str:
        return name.strip().casefold()

    display: dict[str, str] = {}

    for row in basic_t1:
        if not row:
            continue
        name = str(row[0]).strip()
        if not name:
            continue
        display[key(name)] = name
        # Sales volume is at index 1
        buckets[key(name)]["basic"] += _parse_rm(row[1] if len(row) > 1 else 0)

    for row in anp_summary_table:
        if not row:
            continue
        name = str(row[0]).strip()
        if not name:
            continue
        display[key(name)] = name
        # Sales volume (Accumulated Total) is at index 2
        buckets[key(name)]["anp"] += _parse_rm(row[2] if len(row) > 2 else 0)

    for row in nfp_agent:
        if not row:
            continue
        name = str(row[0]).strip()
        if not name:
            continue
        display[key(name)] = name
        # Sales volume (Sales Price) is at index 1
        buckets[key(name)]["nfp"] += _parse_rm(row[1] if len(row) > 1 else 0)

    combined: list[tuple[str, float, float, float, float, int]] = []
    for k, amounts in buckets.items():
        b, a, n = amounts["basic"], amounts["anp"], amounts["nfp"]
        total = b + a + n
        if total <= 0:
            continue
        agent_name = display.get(k, k)
        cust_cnt = agent_customers.get(agent_name, 0) if agent_customers else 0
        combined.append(
            (
                agent_name,
                float(b),
                float(a),
                float(n),
                float(total),
                cust_cnt,
            )
        )
    combined.sort(key=lambda x: x[4], reverse=True)
    return combined


def determine_nfp_rate(matched_rows: list[Any]) -> str:
    rates = set()
    for r in matched_rows:
        sales = float(r.sales_price)
        nfp = float(r.net_floor_price) if r.net_floor_price is not None else None
        if nfp is None:
            continue
        if sales > nfp:
            rates.add("25%")
        elif sales < nfp:
            rates.add("20%")
        else:
            rates.add("25%")
    if not rates:
        return "25%"
    sorted_rates = sorted(list(rates), key=lambda x: x, reverse=True)
    return "/".join(sorted_rates)


def build_pdf(year: int, output_path: Path) -> Path:
    from commission_pdf import FinanceChartData, PdfSection, write_finance_presentation_pdf

    print(f"Building finance PDF for {year}...")
    basic_t1, basic_t2, basic_t3, basic_t4, basic_meta, basic_lines = fetch_basic(year, h1_only=False)
    print(f"  Basic: {basic_meta['agents']} agents, {basic_meta['invoices']} invoices")

    anp_summary, anp_detail, anp_meta = fetch_anp(year, h1_only=False)
    print(f"  ANP: {anp_meta['agents']} agents, {anp_meta['invoices']} invoices")

    nfp_agent, nfp_detail, nfp_meta, nfp_rows = fetch_nfp(year, h1_only=False)
    print(f"  NFP: {nfp_meta['agents']} agents, {nfp_meta['invoices']} invoices")

    ega_t1, ega_t2, ega_t3, ega_h1, ega_h2, ega_h3 = fetch_ega_esa(year)
    print(f"  EGA/ESA: {len(ega_t1)} agents, {len(ega_t2) + len(ega_t3)} invoices")

    monthly_metrics = aggregate_monthly_metrics(basic_lines, anp_detail, nfp_rows, year)

    anp_summary_table = []

    grand = (
        basic_meta["total_commission"]
        + anp_meta["total_commission"]
        + nfp_meta["total_commission"]
    )
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Fetch agent rates from basic lines
    agent_rates_map = defaultdict(set)
    for ln in basic_lines:
        rate_val = float(ln.rate) * 100
        rate_str = f"{rate_val:g}%"
        
        if ln.package == "Factory":
            if float(ln.profit_sharing) > 0:
                ps_val = float(ln.profit_sharing) * 100
                ps_str = f"{ps_val:g}%"
                agent_rates_map[ln.agent_name.strip()].add(f"{rate_str} + {ps_str}")
            else:
                agent_rates_map[ln.agent_name.strip()].add(f"{rate_str}")
        else:
            agent_rates_map[ln.agent_name.strip()].add(f"{rate_str}")
            
        # Senior override logic removed for outsource agents            
    agent_rates = {}
    for agent, rates in agent_rates_map.items():
        # Sort so 3.25% is first, then 2%, then 0.5%, then 0.25%
        # Python string sort will sort "3.25%" > "2% + profit sharing" > "0.5% + profit sharing" > "0.25%"
        sorted_rates = sorted(list(rates), reverse=True)
        # ReportLab Paragraphs require <br/> for line breaks.
        agent_rates[agent] = "<br/>".join(sorted_rates)

    # Build Basic Commission Matrix Rows
    basic_matrix = defaultdict(lambda: {m: {"sales": 0.0, "comm": 0.0} for m in range(1, 13)})
    for ln in basic_lines:
        agent = ln.agent_name.strip()
        full_pay_m = _parse_month(ln.full_payment_date)
        if full_pay_m and 1 <= full_pay_m <= 12:
            basic_matrix[agent][full_pay_m]["sales"] += float(ln.sales_price)
        for m in range(1, 13):
            payout = _basic_commission_payout_for_month(ln, m)
            if payout > 0:
                basic_matrix[agent][m]["comm"] += payout

    # Senior override commission logic removed for outsource agents

    basic_matrix_rows = []
    for agent in sorted(basic_matrix.keys()):
        rate = agent_rates.get(agent, "-")
        row = [agent, rate]
        for m in range(1, 13):
            sales = basic_matrix[agent][m]["sales"]
            comm = basic_matrix[agent][m]["comm"]
            row.append(f"{sales:,.2f}" if sales > 0 else "-")
            row.append(f"{comm:,.2f}" if comm > 0 else "-")
        basic_matrix_rows.append(row)

    basic_total_row = ["Total", ""]
    for m in range(1, 13):
        m_sales = sum(basic_matrix[a][m]["sales"] for a in basic_matrix)
        m_comm = sum(basic_matrix[a][m]["comm"] for a in basic_matrix)
        basic_total_row.append(f"{m_sales:,.2f}" if m_sales > 0 else "-")
        basic_total_row.append(f"{m_comm:,.2f}" if m_comm > 0 else "-")
    basic_matrix_rows.append(basic_total_row)

    # Build ANP Commission Matrix Rows
    anp_matrix = defaultdict(lambda: {m: {"sales": 0.0, "comm": 0.0} for m in range(1, 13)})
    for r in anp_detail:
        m = _parse_month(r.get("invoice_date"))
        if m and 1 <= m <= 12:
            agent = r.get("agent_name", "").strip()
            anp_matrix[agent][m]["sales"] += float(r.get("invoice_total_amount", 0.0))
            anp_matrix[agent][m]["comm"] += float(r.get("anp_commission_accumulated_tier", 0.0))

    anp_matrix_rows = []
    for agent in sorted(anp_matrix.keys()):
        row = [agent, "-"]
        for m in range(1, 13):
            sales = anp_matrix[agent][m]["sales"]
            comm = anp_matrix[agent][m]["comm"]
            row.append(f"{sales:,.2f}" if sales > 0 else "-")
            row.append(f"{comm:,.2f}" if comm > 0 else "-")
        anp_matrix_rows.append(row)

    anp_total_row = ["Total", ""]
    for m in range(1, 13):
        m_sales = sum(anp_matrix[a][m]["sales"] for a in anp_matrix)
        m_comm = sum(anp_matrix[a][m]["comm"] for a in anp_matrix)
        anp_total_row.append(f"{m_sales:,.2f}" if m_sales > 0 else "-")
        anp_total_row.append(f"{m_comm:,.2f}" if m_comm > 0 else "-")
    anp_matrix_rows.append(anp_total_row)

    # Build NFP Commission Matrix Rows
    nfp_matrix = defaultdict(lambda: {m: {"sales": 0.0, "system": 0.0, "nfp": 0.0, "comm": 0.0} for m in range(1, 13)})
    for r in nfp_rows:
        m = _parse_month(_effective_nfp_date(r))
        if m and 1 <= m <= 12:
            agent = r.agent_name.strip()
            nfp_matrix[agent][m]["sales"] += float(r.sales_price)
            nfp_matrix[agent][m]["system"] += float(r.system_price)
            nfp_matrix[agent][m]["nfp"] += float(r.net_floor_price) if r.net_floor_price else 0.0
            nfp_matrix[agent][m]["comm"] += float(r.nfp_commission)

    nfp_matrix_rows = []
    for agent in sorted(nfp_matrix.keys()):
        agent_rows = [r for r in nfp_rows if r.agent_name.strip().lower() == agent.strip().lower()]
        rate_str = determine_nfp_rate(agent_rows)
        row = [agent, rate_str]
        for m in range(1, 13):
            sales = nfp_matrix[agent][m]["sales"]
            system = nfp_matrix[agent][m]["system"]
            nfp_val = nfp_matrix[agent][m]["nfp"]
            comm = nfp_matrix[agent][m]["comm"]
            row.append(f"{sales:,.0f}" if sales > 0 else "-")
            row.append(f"{system:,.0f}" if system > 0 else "-")
            row.append(f"{nfp_val:,.0f}" if nfp_val > 0 else "-")
            row.append(f"{comm:,.0f}" if comm > 0 else "-")
        nfp_matrix_rows.append(row)

    nfp_total_row = ["Total", ""]
    for m in range(1, 13):
        m_sales = sum(nfp_matrix[a][m]["sales"] for a in nfp_matrix)
        m_system = sum(nfp_matrix[a][m]["system"] for a in nfp_matrix)
        m_nfp = sum(nfp_matrix[a][m]["nfp"] for a in nfp_matrix)
        m_comm = sum(nfp_matrix[a][m]["comm"] for a in nfp_matrix)
        nfp_total_row.append(f"{m_sales:,.0f}" if m_sales > 0 else "-")
        nfp_total_row.append(f"{m_system:,.0f}" if m_system > 0 else "-")
        nfp_total_row.append(f"{m_nfp:,.0f}" if m_nfp > 0 else "-")
        nfp_total_row.append(f"{m_comm:,.0f}" if m_comm > 0 else "-")
    nfp_matrix_rows.append(nfp_total_row)

    # Calculate full payment and pending payment counts from report-relevant invoices
    if MOCK_MODE:
        mock_raw = get_mock_data(year)
        unfiltered_h1_rows = [r for r in mock_raw["nfp_rows"] if _parse_month(r.invoice_date) in range(1, 13)]
    else:
        nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
        nfp_mod = _load_module("outsource_nfp_commission", nfp_dir / "outsource_nfp_commission.py")
        unfiltered_rows, _ = nfp_mod.build_report(year)
        unfiltered_h1_rows = [r for r in unfiltered_rows if _parse_month(r.invoice_date) in range(1, 13)]

    payment_map = {r.invoice_number.strip(): _effective_nfp_date(r) for r in unfiltered_h1_rows if r.invoice_number}

    basic_keys = {ln.invoice_number.strip() for ln in basic_lines if getattr(ln, "invoice_number", None)}
    nfp_keys = {r.invoice_number.strip() for r in nfp_rows}
    anp_keys = {r.get("invoice_number", "").strip() for r in anp_detail if r.get("invoice_number")}

    all_report_keys = basic_keys | nfp_keys | anp_keys

    invoice_sales_map = {}
    for ln in basic_lines:
        inv = getattr(ln, "invoice_number", None)
        if inv: invoice_sales_map[inv.strip()] = float(ln.sales_price)
    for r in anp_detail:
        inv = r.get("invoice_number")
        if inv: invoice_sales_map[inv.strip()] = float(r.get("sales_price", 0.0))
    for r in nfp_rows:
        inv = r.invoice_number
        if inv: invoice_sales_map[inv.strip()] = float(r.sales_price)

    full_payment_count = 0
    pending_payment_count = 0
    full_payment_sales = 0.0

    for key in all_report_keys:
        is_full_payment = False
        if key in basic_keys or key in nfp_keys:
            is_full_payment = True
        else:
            fp_date = payment_map.get(key)
            if fp_date:
                is_full_payment = True
                
        if is_full_payment:
            full_payment_count += 1
            full_payment_sales += invoice_sales_map.get(key, 0.0)
        else:
            pending_payment_count += 1

    agents_with_closed = set()
    agents_with_pending = set()

    for ln in basic_lines:
        agents_with_closed.add(ln.agent_name.strip())

    for r in nfp_rows:
        agents_with_closed.add(r.agent_name.strip())

    for r in anp_detail:
        agent = r.get("agent_name", "").strip()
        inv = r.get("invoice_number", "").strip()
        if inv in basic_keys or inv in nfp_keys or payment_map.get(inv):
            agents_with_closed.add(agent)
        else:
            agents_with_pending.add(agent)

    closed_agents_count = len(agents_with_closed)
    pending_agents_count = len(agents_with_pending)

    sections = [
        PdfSection(
            "Basic Commission Details",
            [],
            basic_matrix_rows,
            landscape=True,
            total_agents=basic_meta["agents"],
            total_customers=len(basic_lines),
        ),
    ]
    
    # Referral fee rows
    basic_t4_pdf = _rows_to_str(basic_t4) if basic_t4 else [["No referral fee", "-", "-", "-", "-", "-", "-", "-", "-"]]
        
    sections.append(PdfSection(
        "Referral Fee Details",
        ["Referral Name", "Agent Name", "Customer Name", "Invoice Number", "Invoice Date", "Full Payment Date", "Sales Price", "Rate %", "Referral Fee"],
        basic_t4_pdf,
        footer_text=[
            "<b>Note:</b>",
            "• Referral Fee: All Residential and non-residential: 1% (Before Mar 2026), 2% (From Mar 2026), Additional 0.5% (Mar Specials 2026)",
            "• Referral fee eligibility excludes spouses"
        ]
    ))

    if basic_t3:
        sections.append(PdfSection(
            "Outsource Factory Details",
            ["Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Payment Date", "Total (RM)", "EPP (RM)", "Sales (RM)", "Rate", "Profit Share", "Commission"],
            basic_t3,
            landscape=True
        ))
    sections.append(PdfSection(
        "NFP Commission Details",
        [],
        nfp_matrix_rows,
        landscape=True,
        total_agents=nfp_meta["agents"],
        total_customers=len(nfp_rows),
        footer_text=[
            "<b>Note:</b>",
            "• NFP Commission distributions are contingent upon the receipt of 100% full payment.",
            "• Three types of Net Floor Price Commission:",
            "  * Sales above Net Floor Price: Sales Price > Net Floor Price. Formula: (Sales Price - Net Floor Price) x 25% = NFP Commission",
            "  * Sales above System Price: System Price > Net Floor Price. Formula: (System Price - Net Floor Price) x 100% = NFP Commission",
            "  * Sales below Net Floor price: Sales Price < Net Floor Price. Formula: (Sales Price - Net Floor Price) x Bears 20% = NFP Commission",
            "• Effective October 1, 2025, NFP computations are applicable exclusively to invoices issued on or after this date. Invoices predating this period are structurally excluded from NFP allocations."
        ]
    ))

    if ega_t1:
        sections.append(PdfSection(
            "EGA / ESA Awards - Summary", 
            ega_h1, 
            ega_t1, 
            landscape=False,
            total_agents=len(ega_t1) - 1 if ega_t1 and ega_t1[-1][0] == "Total" else len(ega_t1),
            total_customers=(len(ega_t2) if ega_t2 else 0) + (len(ega_t3) if ega_t3 else 0),
            footer_text=[
                "<b>EGA Targets (Standard):</b> EP Points > 720,000. <b>Early Bird EGA:</b> Feb &ge; 420,000 | Mar &ge; 480,000 | Apr &ge; 540,000 | May &ge; 600,000",
                "<b>ESA Targets (Standard):</b> EP Points > 1,560,000. <b>Early Bird ESA:</b> Oct &ge; 1,360,000 | Nov &ge; 1,460,000",
                "",
                "<b>EP Point Recognition Structure:</b>",
                "• Requires a minimum 5% payment",
                "• Residential / Shop Lot / Commercial: 100% recognition rate",
                "• Factory / Corporate Projects (Prior to May 2026): 100% recognition rate (unless factory has less than 36pcs, in which case it follows Residence rate)",
                "• Factory / Corporate Projects (Effective May 2026 onwards): 100% recognition for the initial RM 40,000; 40% recognition for subsequent amounts (unless factory has less than 36pcs, in which case it follows Residence rate)"
            ]
        ))

    if ega_t3:
        sections.append(PdfSection("EGA / ESA Awards - Factory", ega_h3, ega_t3, landscape=True))

    prod_data = fetch_production_bonus(year)
    if prod_data:
        oum_summary = prod_data.get("oum_summary", [])
        ogm_summary = prod_data.get("ogm_summary", [])
        team_detail = prod_data.get("team_detail", [])
        headers = prod_data.get("headers", {})

        if oum_summary:
            sections.append(PdfSection(
                "Production Bonus - OUM Summary",
                headers.get("oum", []),
                oum_summary,
                landscape=True
            ))
        if ogm_summary:
            sections.append(PdfSection(
                "Production Bonus - OGM Summary",
                headers.get("ogm", []),
                ogm_summary,
                landscape=True
            ))
        if team_detail:
            sections.append(PdfSection(
                "Production Bonus - Team Details",
                headers.get("detail", []),
                team_detail,
                landscape=True
            ))

    meta_lines = [
        ("Generated", run_at),
        ("Data year", str(year)),
        ("Basic filter", basic_meta["filter"]),
        ("ANP filter", anp_meta["filter"]),
        ("NFP filter", nfp_meta["filter"]),
        (
            "Note",
            "Snapshot at run time. Re-run monthly when new invoices are paid in-period.",
        ),
    ]

    agent_unique_customers = defaultdict(set)
    for ln in basic_lines:
        agent_unique_customers[ln.agent_name.strip()].add(ln.customer_name.strip())
    for r in anp_detail:
        agent_unique_customers[r.get("agent_name", "").strip()].add(r.get("customer_name", "").strip())
    for r in nfp_rows:
        agent_unique_customers[r.agent_name.strip()].add(r.customer_name.strip())
    agent_customers_count = {name: len(custs) for name, custs in agent_unique_customers.items()}

    all_agents = _combined_agent_totals(basic_t1, anp_summary_table, nfp_agent, agent_customers_count)
    top_agents = [x[0] for x in all_agents[:5]]

    charts = FinanceChartData(
        basic_total=float(basic_meta["total_commission"]),
        anp_total=float(anp_meta["total_commission"]),
        nfp_total=float(nfp_meta["total_commission"]),
        grand_total=float(grand),
        top3=all_agents,
        monthly_trend=monthly_metrics["monthly_trend"],
        monthly_agent_customers=monthly_metrics["monthly_agent_customers"],
        monthly_agent_sales=monthly_metrics["monthly_agent_sales"],
        agent_names=monthly_metrics["agent_names"],
        monthly_commission_split=monthly_metrics["monthly_commission_split"],
        monthly_agent_commission_split=monthly_metrics["monthly_agent_commission_split"],
        total_agents=monthly_metrics["total_agents"],
        total_customers=len(all_report_keys),
        full_payment_count=full_payment_count,
        pending_payment_count=pending_payment_count,
        closed_agents_count=closed_agents_count,
        pending_agents_count=pending_agents_count,
        full_payment_sales=full_payment_sales
    )

    write_finance_presentation_pdf(
        output_path,
        title=f"Commission Report {year}",
        year=year,
        generated_at=run_at,
        period_subtitle="for full year",
        logo_path=str(REPO_ROOT / "assets" / "eternalgy_logo.png"),
        meta_lines=meta_lines,
        sections=sections,
        charts=charts,
        is_outsource=True,
    )
    return output_path


def main() -> int:
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Finance pack for Basic + ANP + NFP (Excel and/or PDF)"
    )
    parser.add_argument("--year", type=int, default=YEAR_DEFAULT)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (.xlsx or .pdf). Default name uses Finance Output/ and timestamp.",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Generate PDF only (presentable summary for finance)",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Generate both Excel and PDF",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock mode with sample data for visual testing",
    )
    
    args = parser.parse_args()
    
    global MOCK_MODE
    MOCK_MODE = args.mock
    
    if not MOCK_MODE:
        token, proxy_url, db_name = _resolve_proxy_credentials()
        if not token:
            print(_token_help_message(), file=sys.stderr)
            return 1
        print(f"Proxy: {proxy_url}  DB: {db_name}  Token: found ({len(token)} chars)")
    else:
        print("Running in MOCK mode (local sample data, no DB connection needed)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    make_excel = args.both
    make_pdf = True

    try:
        if make_excel:
            out_xlsx = args.output or (FINANCE_DIR / f"Outsource_Commission_Pack_{args.year}_{stamp}.xlsx")
            if out_xlsx.suffix.lower() != ".xlsx":
                out_xlsx = out_xlsx.with_suffix(".xlsx")
            path_xlsx = build_workbook(args.year, out_xlsx)
            print(f"\nFinance Excel saved:\n  {path_xlsx.resolve()}")
        if make_pdf:
            out_pdf = args.output or (FINANCE_DIR / f"Outsource_Commission_Pack_{args.year}_{stamp}.pdf")
            if out_pdf.suffix.lower() != ".pdf":
                out_pdf = out_pdf.with_suffix(".pdf")
            path_pdf = build_pdf(args.year, out_pdf)
            print(f"\nFinance PDF saved:\n  {path_pdf.resolve()}")
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        print("Install: pip install -r requirements-finance.txt", file=sys.stderr)
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
