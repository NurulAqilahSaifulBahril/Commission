#!/usr/bin/env python3
"""
Build unified Commission PDF containing both Internal and Outsource commissions.

Output:
  Finance Output/Commission_<year>_<timestamp>.pdf

Content:
  1. Table of Contents
  2. Internal Commission Highlight (Dashboard)
  3. Summary Internal Agent Commission (stacked Basic/NFP/ANP rows per agent)
  4. Summary Internal Agent Commission by Customer
  5. EGA/ESA Awards (Internal)
  6. Outsource Commission Highlight (Dashboard)
  7. Summary Outsource Agent Commission (stacked Basic/NFP/ANP rows per agent)
  8. Summary Outsource Agent Commission by Customer
  9. EGA/ESA Awards (Outsource)
  10. Production Bonus

Requires: reportlab, openpyxl, python-dotenv and each commission script's dependencies.
Token (any one location):
  - Commission/.env  => PG_PROXY_TOKEN=your_jwt_here
  - Commission/pg_proxy_token.txt
"""

from __future__ import annotations

import importlib.util
import os
import sys
import re
import copy as _copy
from collections import defaultdict
from dataclasses import replace as _dc_replace, is_dataclass as _is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
FINANCE_DIR = Path(__file__).resolve().parent / "Finance Output"
YEAR_DEFAULT = 2026

DEFAULT_PROXY_URL = "https://pg-proxy-production.up.railway.app/api/sql"
DEFAULT_DB_NAME = "prod_main"

MOCK_MODE = False


# ---------------------------------------------------------------------------
# Token / env helpers (copied from existing builders)
# ---------------------------------------------------------------------------

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
        "Get a fresh JWT from your Postgres proxy admin if you see 'Token expired'."
    )


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _nfp_payout_date(line: Any) -> Any:
    """The date an NFP line is recognised on.

    Equal to full_payment_date unless a Net Floor Price tier on the Data page
    states a different payment stage, in which case the engine resolves the
    milestone for that stage and puts it here. Falls back to full_payment_date
    for lines built before the field existed, so bucketing is unchanged for
    anything that does not carry it.
    """
    return getattr(line, "payout_date", None) or getattr(line, "full_payment_date", None)


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


def _year_month(date_val: Any) -> tuple[int, int] | None:
    if not date_val:
        return None
    if isinstance(date_val, (date, datetime)):
        return (date_val.year, date_val.month)
    s = str(date_val).strip()
    m = re.match(r'^(\d{4})[-/](\d{2})', s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def _is_new_policy_line(ln) -> bool:
    """True when this line is paid in two stages (an advance, then the balance).

    The Data page row that priced the invoice decides; the July-2026 invoice-date
    test is only the fallback for lines built before the policy was attached.
    """
    policy = getattr(ln, "payout_policy", None)
    if policy is not None:
        return policy.is_multi_stage
    inv_ym = _year_month(getattr(ln, "invoice_date", ""))
    return inv_ym is not None and inv_ym >= (2026, 7)


def _line_advance_amount(ln) -> float:
    policy = getattr(ln, "payout_policy", None)
    if policy is not None and policy.advance_amount is not None:
        return float(policy.advance_amount)
    return 300.0


def _rm300_column_display(cust_basic_lines: list) -> str:
    """Advance-tranche cell for a customer row (multi-stage invoices).

    - Multi-stage invoices show the advance actually payable this month (capped
      at the Data page's advance amount, and only for the first-milestone /
      combined line, never the balance line via ``_show_rm300``).
    - Single-stage invoices have no advance tranche at all -- they pay the full
      Basic Commission at their payout trigger -- so the cell reads
      "invoice before july".
    """
    if not any(_is_new_policy_line(ln) for ln in cust_basic_lines):
        return "invoice before july"
    rm300_total = sum(
        min(float(ln.basic_commission), _line_advance_amount(ln))
        for ln in cust_basic_lines
        if _is_new_policy_line(ln)
        and getattr(ln, "pct5_date", "")
        and getattr(ln, "_show_rm300", True)
    )
    if rm300_total != 0:
        return ensure_rm_prefix(f"{rm300_total:,.2f}")
    # No RM300 tranche recognized this month: either the 5% milestone hasn't
    # been reached yet (genuinely pending), or it was already paid out and
    # shown in an earlier month (this line is balance-only, _show_rm300=False).
    still_pending = any(
        _is_new_policy_line(ln) and not getattr(ln, "pct5_date", "")
        for ln in cust_basic_lines
    )
    if still_pending:
        return "pending"
    return "-"


def _replace_line(ln, **changes):
    """Return a shallow copy of a basic-commission line with ``changes`` applied.

    The internal path uses frozen-ish dataclasses (``dataclasses.replace``); the
    outsource path uses lightweight ``SimpleNamespace`` line objects that
    ``dataclasses.replace`` cannot handle, so fall back to copy + setattr.
    """
    if _is_dataclass(ln):
        return _dc_replace(ln, **changes)
    new = _copy.copy(ln)
    for k, v in changes.items():
        setattr(new, k, v)
    return new


def _expand_basic_lines_for_month(basic_lines: list, m: int) -> list:
    """
    July 2026 onwards, Basic Commission pays out in two milestones: a flat
    RM300 (or less, if the full rate would be less) once an invoice reaches
    >=5% payment, and the remaining balance once it reaches >=75% payment.

    When both milestones fall in the same month (or for pre-policy invoices,
    i.e. invoice_date before July 2026), nothing changes here: the line is
    returned as-is and contributes a single combined row with overrides/
    referral fee attributed normally.

    When the two milestones fall in different months, the invoice
    contributes to TWO separate months' reports: an RM300-only line in the
    5%-month (marked to skip override/referral attribution and to not show
    a "75% Payment Date", since the sale isn't finalized yet) and a
    balance-only line in the 75%-month (override/referral attributed there
    as usual, RM300 marked not to be re-shown so it isn't double counted).
    """
    result = []
    for ln in basic_lines:
        policy = getattr(ln, "payout_policy", None)
        if policy is not None:
            is_new_policy = policy.is_multi_stage
        else:
            inv_ym = _year_month(ln.invoice_date)
            is_new_policy = inv_ym is not None and inv_ym >= (2026, 7)

        if not is_new_policy:
            # Single-stage invoices: the full Basic Commission is recognized
            # only when the invoice reaches its payout trigger (100% payment
            # unless the Data page says otherwise). Bucket strictly by that
            # date and drop the invoice from every month until then -- e.g. a
            # May invoice only partially paid does NOT appear in July.
            pct100_str = getattr(ln, "pct100_date", "") or ""
            if pct100_str and _parse_month(pct100_str) == m:
                result.append(ln)
            continue

        pct5_str = getattr(ln, "pct5_date", "") or ""
        pct75_str = getattr(ln, "pct75_date", "") or ""
        pct5_m = _parse_month(pct5_str) if pct5_str else None
        pct75_m = _parse_month(pct75_str) if pct75_str else None
        if pct5_m is None:
            continue

        full_amt = ln.basic_commission
        advance_rm = policy.advance_amount if policy is not None else 300
        rm300_amt = min(full_amt, full_amt.__class__(str(advance_rm)))

        if pct75_m is not None and pct5_m == pct75_m:
            if pct75_m == m:
                result.append(ln)
        elif pct75_m is not None and pct5_m != pct75_m:
            if pct5_m == m:
                rm300_ln = _replace_line(ln, basic_commission=rm300_amt)
                rm300_ln._skip_override = True
                result.append(rm300_ln)
            if pct75_m == m:
                balance_ln = _replace_line(ln, basic_commission=full_amt - rm300_amt)
                balance_ln._show_rm300 = False
                # This row is the balance, so its commission is already net of
                # the advance paid in an earlier month. Record how much came
                # off: the Basic Commission hover shows it as an explicit
                # "- 300" rather than bending the rate to fit the net figure.
                balance_ln._advance_deducted = rm300_amt
                result.append(balance_ln)
        else:
            # Only the 5% milestone has been reached so far.
            if pct5_m == m:
                rm300_ln = _replace_line(ln, basic_commission=rm300_amt)
                rm300_ln._skip_override = True
                result.append(rm300_ln)
    return result


def _effective_recognition_date(r: dict, year: int = 2026) -> str:
    """The date an invoice should be recognized/bucketed by for reporting.

    Mirrors the eligibility SQL's own payment-percentage rule (full payment
    before the multi-stage cutover, the advance trigger after it) instead of the
    invoice's stored full_payment_date, which can be stale — e.g. set to an early
    partial-payment date rather than the date the invoice actually crossed the
    threshold. The cutover month and the trigger percentages both come from the
    Data page, so this stays in step with the query that selected the rows.
    """
    from decimal import Decimal as _D
    import basic_commission_rates as _bcr

    cutover = _bcr.multi_stage_cutover(year)
    cut_m = cutover[0] if cutover else 13
    full = _bcr.milestone_date(r, _D("100"))
    advance = _bcr.milestone_date(r, cutover[1].advance_trigger) if cutover else ""
    m_full = _parse_month(full) if full else None
    m_adv = _parse_month(advance) if advance else None
    if m_full and 1 <= m_full < cut_m:
        return full
    if m_adv and cut_m <= m_adv <= 12:
        return advance
    if m_full and cut_m <= m_full <= 12:
        return full
    return str(r.get("real_full_payment_date") or "")[:10]


def _parse_rm(val: Any) -> Decimal:
    try:
        return Decimal(str(val).replace(",", "").replace("RM", "").strip())
    except Exception:
        return Decimal("0")


def to_title_case(name: Any) -> str:
    if not name:
        return ""
    name_str = str(name).strip()
    words = name_str.split()
    capitalized = []
    for w in words:
        w_upper = w.upper()
        if w_upper in ("OUM", "OSA", "OGM", "ANP", "NFP", "RM", "H1", "H2", "EP", "EGA", "ESA", "KPI"):
            capitalized.append(w_upper)
        elif w_upper == "ZUL":
            capitalized.append("Zulkarnain")
        else:
            if w.startswith("(") and w.endswith(")"):
                capitalized.append("(" + w[1:-1].lower().capitalize() + ")")
            else:
                capitalized.append(w[0].upper() + w[1:].lower() if len(w) > 1 else w.upper())
    return " ".join(capitalized)


# Shared agent full-name resolver (nickname -> canonical full name, Title Case),
# sourced from the dashboard's Agent Roles & Hierarchy page (agent_roles table).
# Applied to Agent columns at render time so tier/hierarchy logic (which keys
# on nicknames) is unaffected.
import agent_names as _agent_names


def to_full_name(name: Any) -> str:
    """Resolve an agent name to its canonical full name for display."""
    if not name:
        return ""
    return _agent_names.resolve(str(name).strip())


def is_agent_header(header: Any) -> bool:
    return isinstance(header, str) and "agent" in header.lower()


def ensure_rm_prefix(val: Any) -> str:
    s = str(val).strip()
    if not s or s == "-":
        return "-"
    if s.startswith("RM"):
        return s
    if re.match(r'^-?[0-9,.]+$', s):
        if s.startswith("-"):
            return f"-RM {s[1:]}"
        return f"RM {s}"
    return s


def internal_agent_sort_key(agent_name: str) -> tuple[int, str, int, str]:
    name_lower = agent_name.lower().strip()
    senior = _int_get_reporting_senior(agent_name)
    
    if name_lower == "sunny tan" or "sunny" in name_lower:
        return (0, "", 0, name_lower)
    elif name_lower == "teng kah kent" or "kent" in name_lower:
        return (1, "", 0, name_lower)
    elif name_lower == "ching zhe hang" or "zhe hang" in name_lower:
        return (2, "", 0, name_lower)
    elif name_lower == "martin hing" or "martin" in name_lower:
        return (3, "", 0, name_lower)
        
    if senior:
        senior_lower = senior.lower()
        if "sunny" in senior_lower:
            return (0, name_lower, 1, name_lower)
        elif "kent" in senior_lower:
            return (1, name_lower, 1, name_lower)
        elif "zhe hang" in senior_lower:
            return (2, name_lower, 1, name_lower)
        elif "martin" in senior_lower:
            return (3, name_lower, 1, name_lower)
            
    return (4, "", 0, name_lower)


def outsource_agent_sort_key(agent_name: str) -> tuple[int, str, int, str]:
    n = agent_name.lower().strip()
    if n == "gan lai soon":
        return (0, "", 0, n)
        
    # Carol Siow group
    if n == "carol siow":
        return (1, "", 1, n)
    if n in ("liew lee ching", "low chin chai", "chang soon huat", "lee seok yun", "lee hui wen", "see cheak ching"):
        return (1, n, 2, n)
    if "sue cherk" in n:
        return (1, "liew lee ching", 3, n)
    if "kim swee" in n:
        return (1, "low chin chai", 3, n)
    if "siong hing" in n:
        return (1, "chang soon huat", 3, n)
        
    # Oliver Koh group
    if n == "oliver koh":
        return (2, "", 1, n)
    if n in ("mohd azhar bin ibrahim", "koh yeong cherng", "ang kok xing", "tey zhi yun", "lim chin seng"):
        return (2, n, 2, n)
    if "hanis" in n and "marjian" in n:
        return (2, "mohd azhar bin ibrahim", 3, n)
        
    # Dean Wai group
    if n == "dean wai":
        return (3, "", 1, n)
    if n in ("lam wai leng", "tee kok kian"):
        return (3, n, 2, n)
        
    # Chan Wing On group
    if n == "chan wing on":
        return (4, "", 1, n)
    if n in ("kwong jun sheng", "lee yue peng", "too pok jen"):
        return (4, n, 2, n)
        
    # Chan Jia Wei group
    if n == "chan jia wei":
        return (5, "", 1, n)
    if n in ("tay hock xiang", "ho wen lin", "ng zhee hao"):
        return (5, n, 2, n)
        
    # Caryn Dong group
    if n == "caryn dong":
        return (6, "", 1, n)
    if n in ("lim kai zhe", "lee chun xun", "ling wei perng"):
        return (6, n, 2, n)
        
    # Ling Liang Kang group
    if n == "ling liang kang":
        return (7, "", 1, n)
    if n == "tan wei hung":
        return (7, n, 2, n)
        
    # Loo Chew Yin group
    if n == "loo chew yin":
        return (8, "", 1, n)
    if n == "lai ka kit":
        return (8, "loo chew yin", 3, n)
        
    # Other OUMs
    other_oums = ["gan lai hock", "phil moo", "kok shao hong", "wilson tan"]
    for idx, oum in enumerate(other_oums):
        if n == oum or oum in n:
            return (9 + idx, "", 1, n)
            
    # Safwan
    if n == "safwan":
        return (20, "", 2, n)
        
    # Fallback / Independent OSAs
    return (30, "", 2, n)


def sort_internal_table_rows(rows: list[list[Any]], agent_index: int = 0) -> list[list[Any]]:
    if not rows:
        return []
    data_rows = []
    total_rows = []
    for r in rows:
        if r and str(r[agent_index]).strip().lower() in ("total", "grand total", "summary"):
            total_rows.append(r)
        else:
            data_rows.append(r)
    sorted_data = sorted(data_rows, key=lambda r: internal_agent_sort_key(str(r[agent_index])))
    return sorted_data + total_rows


def sort_outsource_table_rows(rows: list[list[Any]], agent_index: int = 0) -> list[list[Any]]:
    if not rows:
        return []
    data_rows = []
    total_rows = []
    for r in rows:
        if r and str(r[agent_index]).strip().lower() in ("total", "grand total", "summary"):
            total_rows.append(r)
        else:
            data_rows.append(r)
    sorted_data = sorted(data_rows, key=lambda r: outsource_agent_sort_key(str(r[agent_index])))
    return sorted_data + total_rows


def format_senior_override_cell(breakdown_dict: dict[str, float]) -> str:
    if not breakdown_dict:
        return "-"
    parts = []
    for exec_name, amount in sorted(breakdown_dict.items()):
        if amount > 0:
            parts.append(f"RM {amount:,.2f} override from {exec_name}")
    return "<br/>".join(parts) if parts else "-"


def format_other_commission_for_customer(breakdown_dict: dict[str, float]) -> str:
    """Per-customer counterpart to format_senior_override_cell: shown on the
    SOURCE agent's own customer row, naming who the override is credited to."""
    if not breakdown_dict:
        return "-"
    parts = []
    for recipient, amount in sorted(breakdown_dict.items()):
        if amount > 0:
            parts.append(f"RM {amount:,.2f} ({recipient})")
    return "<br/>".join(parts) if parts else "-"


invoice_package_map = {}


def get_invoice_package(invoice_number: str, invoice_obj: Any = None) -> str:
    inv_num_clean = str(invoice_number).strip()
    if inv_num_clean in invoice_package_map:
        return invoice_package_map[inv_num_clean]
    if invoice_obj:
        desc = ""
        for attr in ("package_description", "description", "all_item_text", "package", "package_name_snapshot"):
            if hasattr(invoice_obj, attr) and getattr(invoice_obj, attr):
                desc += " " + str(getattr(invoice_obj, attr))
            elif isinstance(invoice_obj, dict) and invoice_obj.get(attr):
                desc += " " + str(invoice_obj.get(attr))
        desc_upper = desc.upper()
        if "FACTORY" in desc_upper:
            return "Factory"
        if "RAKYAT" in desc_upper or "RESIDENTIAL" in desc_upper:
            return "Residential"
        if "SHOP" in desc_upper or "COMMERCIAL" in desc_upper:
            return "Shop Lot"

        # Fallback: infer from customer name when the description text gives no
        # signal (e.g. NFP rows whose package_description only lists panel specs,
        # never the property-type keyword itself). Mirrors classify_property_type's
        # customer-name check and default in outsource_basic_commission.py: most
        # customers are individual homeowners, so "Residential" is the default
        # unless the name itself signals a registered business.
        cust_name = ""
        if hasattr(invoice_obj, "customer_name") and getattr(invoice_obj, "customer_name"):
            cust_name = str(getattr(invoice_obj, "customer_name"))
        elif isinstance(invoice_obj, dict) and invoice_obj.get("customer_name"):
            cust_name = str(invoice_obj.get("customer_name"))
        cust_name_upper = cust_name.upper()
        if cust_name_upper:
            comm_keywords = (
                "SDN BHD", "SDN. BHD.", "BHD", "PRIVATE LIMITED", "LIMITED", "LTD",
                "ENTERPRISE", "COMMERCIAL", "SHOP", "TRADING", "INDUSTRIES", "INDUSTRY",
                "ENGINEERING", "CONSTRUCTION", "SERVICES", "SERVICE", "MARKET", "MART",
                "BUSINESS", "CORP", "CORPORATION",
            )
            if any(kw in cust_name_upper for kw in comm_keywords):
                if "FACTORY" in cust_name_upper or "EDGING" in cust_name_upper:
                    return "Factory"
                return "Shop Lot"
        return "Residential"
    return "Shop Lot"


def get_internal_agent_tier(agent_name: str) -> str:
    n = agent_name.lower().strip()
    if not n or any(x in n for x in ("total", "grand total", "summary")):
        return ""
    seniors = ["sunny", "martin", "kent", "zhe hang"]
    if any(s in n for s in seniors):
        return "Senior"
    return "Executive"


def get_outsource_agent_tier(agent_name: str) -> str:
    n = agent_name.lower().strip()
    if not n or any(x in n for x in ("total", "grand total", "summary")):
        return ""
    if n == "gan lai soon":
        return "OGM"
        
    basic_mod = sys.modules.get("out_basic_commission")
    if not basic_mod:
        try:
            out_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
            basic_mod = _load_module("out_basic_commission", out_path)
        except Exception:
            pass
            
    if basic_mod:
        info = basic_mod.get_agent_hierarchy_info(agent_name)
        if info:
            tier = info.get("tier", "OSA")
            if tier in ("OSA 1", "OSA"):
                return "OSA"
            return str(tier).strip()
            
    oums = [
        "carol siow", "oliver koh", "dean wai", "chan wing on", "chan jia wei", 
        "caryn dong", "ling liang kang", "loo chew yin", "gan lai hock", 
        "phil moo", "kok shao hong", "wilson tan"
    ]
    if any(oum in n for oum in oums):
        return "OUM"
    return "OSA"


# ---------------------------------------------------------------------------
# Internal hierarchy helpers (mirrors build_finance_commission_pack.py)
# ---------------------------------------------------------------------------

def _role_table_senior(agent_name: str, month: int | None = None):
    """(listed, reports_to) from the dashboard role table. Same precedence as
    the calculation engine, so the pack and the dashboard can never disagree
    about the hierarchy. Listed with a blank Reports To means "nobody"."""
    try:
        import basic_commission_rates as _bcr
        return _bcr.get_reporting_senior_from_table(agent_name, month or 7)
    except Exception:
        return False, None


def _int_get_reporting_senior(agent_name: str, month: int | None = None) -> str | None:
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


def _int_get_senior_label(agent_name: str) -> str | None:
    """Returns the Safwan-like override label for internal agents.
    For Internal, 'Safwan' is used as the override column name when the agent
    is a factory agent who has an override from the senior."""
    # For internal, the override is applied by senior agents.
    # The 'Safwan (RM)' column maps to Safwan's factory override commission.
    return None  # will be computed separately per agent


def determine_nfp_rate(matched_rows: list[Any]) -> str:
    rates = set()
    for r in matched_rows:
        sales = float(getattr(r, "sales_price", 0))
        nfp = float(getattr(r, "net_floor_price", None) or 0)
        if nfp <= 0:
            continue
        
        # Check components from InvoiceCommission if present
        comm_b = float(getattr(r, "commission_b", 0))
        comm_a = float(getattr(r, "commission_a", 0))
        comm_c = float(getattr(r, "commission_c", 0))
        
        if comm_b > 0 and comm_a == 0 and comm_c == 0:
            rates.add("100%")
        elif comm_c > 0 or sales < nfp:
            rates.add("bears 20%")
        elif comm_a > 0 or sales > nfp:
            rates.add("25%")
        else:
            rates.add("25%")
            
    if not rates:
        return "25%"
    sorted_rates = sorted(list(rates), key=lambda x: x, reverse=True)
    return "/".join(sorted_rates)


def get_anp_tier_label(comm_amount: float) -> str:
    val = float(comm_amount)
    if val == 0.0:
        return "RM 0 - 59K"
    elif val == 500.0:
        return "RM 60k - 179K"
    elif val == 1000.0:
        return "RM 180K - 359K"
    elif val == 1500.0:
        return "Above RM 360K"
    elif val == 2000.0:
        return "RM 720K"
    else:
        return "-"


# ---------------------------------------------------------------------------
# Unified (Internal + Outsource combined) report-overview helpers
#
# These feed the Executive Summary, Top Performers & Concentration, and Total
# Commission Payout by Type pages -- all "all agents together" views. They
# only ever combine numbers the per-type builders above already computed
# (agent_totals_by_month), so they can't drift from the detailed ledgers.
# ---------------------------------------------------------------------------

def _max_anp_by_agent_for_month(anp_detail_full: list, m: int) -> dict[str, float]:
    """Outsource has no per-agent monthly ANP total anywhere upstream --
    anp_commission_accumulated_tier is a running tier value repeated across
    an agent's invoices, so (mirroring how the internal side handles it) the
    agent's figure for the month is the MAX seen that month, not a sum."""
    agent_max: dict[str, float] = {}
    for r in anp_detail_full:
        if _parse_month(r.get("invoice_date")) != m:
            continue
        agent = to_title_case(str(r.get("agent_name", "")).strip())
        if not agent:
            continue
        comm = float(r.get("anp_commission_accumulated_tier", 0.0) or 0.0)
        if comm > agent_max.get(agent, 0.0):
            agent_max[agent] = comm
    return agent_max


def _unified_agent_totals(int_totals: dict, out_totals: dict, out_anp_by_agent: dict) -> list[dict]:
    """Merge Internal + Outsource per-agent Basic+NFP+ANP totals for one
    month into a single list, tagged by type, ranked by total commission
    descending -- the "all agents together" ranking used by Top Performers."""
    merged: list[dict] = []
    for agent, t in (int_totals or {}).items():
        total = t["basic"] + t["nfp"] + t["anp"]
        if total <= 0 and t["invoices"] == 0:
            continue
        merged.append({
            "agent": agent, "type": "Internal",
            "basic": t["basic"], "nfp": t["nfp"], "anp": t["anp"],
            "sales": t["sales"], "invoices": t["invoices"], "total": total,
        })
    for agent, t in (out_totals or {}).items():
        anp = out_anp_by_agent.get(agent, 0.0)
        total = t["basic"] + t["nfp"] + anp
        if total <= 0 and t["invoices"] == 0:
            continue
        merged.append({
            "agent": agent, "type": "Outsource",
            "basic": t["basic"], "nfp": t["nfp"], "anp": anp,
            "sales": t["sales"], "invoices": t["invoices"], "total": total,
        })
    merged.sort(key=lambda r: r["total"], reverse=True)
    return merged


def _report_overview_totals(unified: list[dict]) -> dict:
    """Grand totals across every agent, plus the Effective Commission Rate
    (total commission paid / total sales value) -- see the methodology note
    rendered alongside it on the Executive Summary page."""
    grand_total = sum(r["total"] for r in unified)
    grand_sales = sum(r["sales"] for r in unified)
    return {
        "basic": sum(r["basic"] for r in unified),
        "nfp": sum(r["nfp"] for r in unified),
        "anp": sum(r["anp"] for r in unified),
        "total": grand_total,
        "sales": grand_sales,
        "effective_rate": (grand_total / grand_sales * 100.0) if grand_sales else 0.0,
        "agents": len(unified),
        "invoices": sum(r["invoices"] for r in unified),
    }


def _rank_changes(current: list[dict], previous: list[dict]) -> dict[str, str]:
    """agent -> 'up:N' / 'down:N' / 'flat' / 'new', vs. the previous month's
    rank in the same unified (Internal+Outsource) leaderboard."""
    prev_rank = {r["agent"]: i + 1 for i, r in enumerate(previous)}
    changes: dict[str, str] = {}
    for i, r in enumerate(current):
        agent = r["agent"]
        rank_now = i + 1
        if agent not in prev_rank:
            changes[agent] = "new"
            continue
        delta = prev_rank[agent] - rank_now
        if delta > 0:
            changes[agent] = f"up:{delta}"
        elif delta < 0:
            changes[agent] = f"down:{-delta}"
        else:
            changes[agent] = "flat"
    return changes


# ---------------------------------------------------------------------------
# Data fetchers (reusing logic from existing builder scripts)
# ---------------------------------------------------------------------------

def fetch_internal_basic(year: int, h1_only: bool = True, month: int | None = None):
    """Returns (basic_t1, basic_t2, basic_t3, basic_t4, meta, basic_lines)."""
    basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
    basic = _load_module("int_basic_commission", basic_path)
    token = os.environ.get("PG_PROXY_TOKEN", "") or basic._env("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError(_token_help_message())
    proxy_url = basic._normalize_proxy_url(
        basic._env("PG_PROXY_URL", DEFAULT_PROXY_URL)
    )
    db_name = basic._env("PG_PROXY_DB") or basic._env("PG_DB_NAME", DEFAULT_DB_NAME)
    payload = basic._proxy_sql(
        proxy_url=proxy_url,
        db_name=db_name,
        token=token,
        sql=basic._invoices_sql(year=year, customer_filter_sql=""),
        params=[],
    )
    raw_rows = list(payload.get("rows") or [])
    if month is not None:
        h1_only = False
        raw_rows = [r for r in raw_rows if _parse_month(r.get("real_full_payment_date")) == month]
    factory_rates = basic.get_factory_rates(raw_rows, default_profit_sharing=(Decimal("0"), Decimal("0")))
    lines = basic._process_invoices(raw_rows, factory_rates)
    if h1_only:
        lines = [ln for ln in lines if _parse_month(ln.full_payment_date) in range(1, 7)]
    user_count, table1 = basic._table1_rows(lines)
    table2 = basic._table2_rows(lines)
    table3 = basic._table3_rows(lines)
    table4 = basic._table4_rows(lines)
    total_comm = sum(_parse_rm(r[4]) for r in table1)
    meta = {
        "agents": user_count,
        "invoices": len(table2) + len(table3),
        "total_commission": total_comm,
        "filter": "paid=TRUE; full_payment_date year; agent internal/full time" + (" (H1)" if h1_only else ""),
    }
    return table1, table2, table3, table4, meta, lines


def _anp_script_path() -> Path:
    for candidate in (
        REPO_ROOT / "3. ANP Commission" / "anp_commission.py",
        REPO_ROOT / "3. ANP Commission" / "3. Python Script" / "anp_commission.py",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"ANP script not found under {REPO_ROOT / '3. ANP Commission'}")


def fetch_internal_anp(year: int, h1_only: bool = True):
    """Returns (summary_rows, detail_rows, meta)."""
    anp_path = _anp_script_path()
    anp = _load_module("int_anp_commission", anp_path)
    base_url = os.getenv("PG_PROXY_URL", "").strip().rstrip("/")
    if base_url.endswith("/api/sql"):
        base_url = base_url[: -len("/api/sql")]
    token = anp.normalize_proxy_token(os.getenv("PG_PROXY_TOKEN", ""))
    db_name = os.getenv("PG_DB_NAME", DEFAULT_DB_NAME).strip()
    if not base_url or not token:
        raise RuntimeError(_token_help_message())
    anp.validate_proxy_token(token)
    client = anp.PostgresProxyClient(base_url, token, db_name)
    agent_types = ["internal", "FULL TIME"]
    agents = anp.fetch_agents(client, agent_types)
    agent_ids = [str(a["bubble_id"]) for a in agents]
    invoices = anp.fetch_invoices_for_agents(client, agent_ids)
    invoice_ids = [str(i["bubble_id"]) for i in invoices if i.get("bubble_id")]
    customer_ids = list({str(i["linked_customer"]) for i in invoices if i.get("linked_customer")})
    planning = anp.fetch_payment_planning(client, invoice_ids)
    customers = anp.fetch_customers(client, customer_ids)
    period_start = date(year, 1, 1)
    period_end = date(year, 6, 30) if h1_only else date(year, 12, 31)
    payout_label = f"invoice-year-{year}-h1" if h1_only else f"invoice-year-{year}"
    detail_rows, summary_rows = anp.build_report_rows(
        agents, invoices, planning, customers, period_start, period_end, payout_label
    )
    total_comm = sum(Decimal(str(r.get("anp_commission", 0))) for r in summary_rows)
    meta = {
        "agents": len(summary_rows),
        "invoices": len(detail_rows),
        "total_commission": total_comm,
        "filter": "invoice_date in year; 1st payment secured; internal + FULL TIME" + (" (H1)" if h1_only else ""),
    }
    return summary_rows, detail_rows, meta


def fetch_internal_nfp(year: int, h1_only: bool = True):
    """Returns (agent_rows, detail_rows, meta, nfp_rows, nfp_by_inv_all).

    ``nfp_by_inv_all`` maps every invoice_number to its NFP row regardless of
    payment status, for System Price/Net Floor Price lookups on invoices that
    haven't reached full payment yet (``nfp_rows`` only has fully-paid ones).
    """
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp = _load_module("int_nfp_commission", nfp_dir / "nfp_commission.py")
    nfp_paths = _load_module("int_nfp_paths", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    rows, summary = nfp.build_report(year)
    nfp_by_inv_all = {r.invoice_number.strip(): r for r in rows if r.invoice_number}
    rows = [r for r in rows if _nfp_payout_date(r)]
    if h1_only:
        rows = [r for r in rows if _parse_month(_nfp_payout_date(r)) in range(1, 7)]
    agents_filtered = {r.agent_name for r in rows if r.agent_name}
    accumulated = {}
    for r in rows:
        if not r.agent_name:
            continue
        if r.agent_name not in accumulated:
            accumulated[r.agent_name] = {"sales_price": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "nfp_commission": 0.0}
        a = accumulated[r.agent_name]
        a["sales_price"] += r.sales_price
        a["system_price"] += r.system_price
        a["net_floor_price"] += r.net_floor_price or 0.0
        a["nfp_commission"] += r.nfp_commission
    summary = {
        "report_year": year,
        "total_qualifying_agents": len(agents_filtered),
        "total_invoices": len(rows),
        "invoices_with_tng_rebate": 0,
        "total_nfp_commission": round(sum(r.nfp_commission for r in rows), 2),
        "accumulated_by_agent": {
            agent: {k: round(v, 2) for k, v in totals.items()}
            for agent, totals in sorted(accumulated.items())
        }
    }
    agent_rows = nfp.build_agent_summary_table(summary)
    detail_rows = nfp.display_rows_as_lists(rows, summary)
    meta = {
        "agents": summary.get("total_qualifying_agents", 0),
        "invoices": summary.get("total_invoices", 0),
        "total_commission": Decimal(str(summary.get("total_nfp_commission", 0))),
        "filter": "payment 100%; invoice_date year; internal/full time" + (" (H1)" if h1_only else ""),
    }
    return agent_rows, detail_rows, meta, rows, nfp_by_inv_all


def fetch_internal_ega_esa(year: int, may_only: bool = False, upto_month: int | None = None):
    """Returns (t1, t2, t3, h1, h2, h3)."""
    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    ega = _load_module("int_ega_esa", ega_dir / "full_internal_EGA_ESA_Awards.py")
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_paths = _load_module("int_nfp_paths2", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    os.environ["POSTGRES_PROXY_TOKEN"] = token
    # api_client is in the same directory as ega script
    api_client_path = ega_dir / "api_client.py"
    if not api_client_path.is_file():
        # fall back to NFP commission directory
        api_client_path = nfp_dir / "api_client.py"
    _load_module("api_client", api_client_path)
    from api_client import query_sql
    
    if upto_month is None:
        upto_month = 5 if may_only else None
        
    sql = ega._invoices_sql(year, upto_month=upto_month)
    rows = query_sql(sql)
    lines, agent_ep, agent_sales, agent_eligibility = ega.build_report(rows)
    t1 = ega.build_table1(agent_ep, agent_sales, agent_eligibility)
    t2 = ega.build_table2(lines, agent_eligibility)
    t3 = ega.build_table3(lines, agent_eligibility)
    return t1, t2, t3, ega.T1_HEADERS, ega.T2_HEADERS, ega.T3_HEADERS


def fetch_internal_ega_raw(year: int):
    """Returns raw invoices for internal agents from the database."""
    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    ega = _load_module("int_ega_esa", ega_dir / "full_internal_EGA_ESA_Awards.py")
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_paths = _load_module("int_nfp_paths2", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    os.environ["POSTGRES_PROXY_TOKEN"] = token
    api_client_path = ega_dir / "api_client.py"
    if not api_client_path.is_file():
        api_client_path = nfp_dir / "api_client.py"
    _load_module("api_client", api_client_path)
    from api_client import query_sql
    sql = ega._invoices_sql(year, upto_month=None)
    return query_sql(sql)


def fetch_outsource_basic(year: int, h1_only: bool = True, month: int | None = None):
    """Returns (basic_t1, basic_t2, basic_t3, meta, basic_lines)."""
    out_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
    basic = _load_module("out_basic_commission", out_path)
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or basic._env("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError(_token_help_message())
    proxy_url = basic._normalize_proxy_url(basic._env("PG_PROXY_URL", DEFAULT_PROXY_URL))
    db_name = basic._env("PG_PROXY_DB") or basic._env("PG_DB_NAME", DEFAULT_DB_NAME)
    payload = basic._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                                sql=basic._invoices_sql(year=year), params=[])
    raw_rows = list(payload.get("rows") or [])
    if month is not None:
        h1_only = False
        raw_rows = [r for r in raw_rows if _parse_month(_effective_recognition_date(r)) == month]

    processed_invoices = []
    processed_factory = []
    agent_own_commissions = defaultdict(Decimal)
    agent_sales_totals = defaultdict(Decimal)
    override_commissions = defaultdict(Decimal)
    gan_lai_soon_commissions = defaultdict(Decimal)

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
        epp = basic._to_decimal(r.get("epp_interest"))
        sales_price = total - epp

        inv_dt = str(r.get("invoice_date") or "")[:10]
        pay_dt = _effective_recognition_date(r)

        # Payment-milestone dates carried through so the summary builder can
        # apply the multi-stage rule (an advance at the first trigger, the
        # balance at the second) via _expand_basic_lines_for_month(). Which
        # percentages those are is whatever the Data page row that priced this
        # invoice says — see basic_commission_rates.invoice_milestones.
        _inv_parsed = basic._parse_invoice_date(r.get("invoice_date"))
        policy, pct5_str, pct75_str, pct100_str = basic._invoice_milestones(
            r,
            hierarchy=basic._outsource_hierarchy_for(info, _inv_parsed),
            month=_inv_parsed.month if _inv_parsed else 5,
            year=_inv_parsed.year if _inv_parsed else year,
            agent=canonical_name,
            property_type=prop_type,
        )
        first_pay_str = str(r.get("first_payment_date") or r.get("1st_payment_date") or "")[:10]

        if h1_only:
            m = _parse_month(pay_dt)
            if not m or m > 6:
                continue

        # Determine Gan Lai Soon OGM override
        tier = info["tier"]
        if tier in ("OSA", "OSA 1", "OUM"):
            ogm_rate = Decimal("0.0075")
        else:
            ogm_rate = Decimal("0")

        if not agent_name.lower() == "safwan":
            ogm_comm = sales_price * ogm_rate
            override_commissions["Gan Lai Soon"] += ogm_comm
        else:
            ogm_comm = Decimal("0")

        if prop_type == "Factory":
            base_rate = Decimal("0.02")

            # The dashboard saves a Factory rate under whatever month the
            # invoice was being viewed as, i.e. its recognition month
            # (pay_dt) -- the same month raw_rows was filtered by above.
            rate_month = _parse_month(pay_dt) or (month if month is not None else _parse_month(inv_dt))
            dash_rate = basic.get_dashboard_factory_rate(agent_name, customer_name, year, rate_month) \
                if rate_month else None
            agent_sharing = dash_rate["agent_rate"] if dash_rate else Decimal("0")
            safwan_sharing = dash_rate["safwan_rate"] if dash_rate else Decimal("0")

            full_commission = sales_price * (base_rate + agent_sharing)

            # Confirmed split, applied to the WHOLE Agent Commission (base +
            # sharing), read from the Agent Roles & Hierarchy Data page:
            #   OSA -> OUM found:      OSA 70% / OUM 20% / OGM 10%
            #   OSA -> OGM directly:   OSA 70% / OGM 10%, 20% unpaid
            #   OSA -> no report:      OSA 70%, 30% unpaid
            #   agent is the OUM:      OUM 20%, 80% unpaid
            # Only applies once profit sharing is actually set (> 0); at 0%
            # the agent keeps the flat 2% base alone, same as before.
            hier = basic.resolve_factory_split_hierarchy(canonical_name)
            factory_tier = hier["tier"]

            own_comm = full_commission
            factory_oum_name = None
            factory_oum_cut = Decimal("0")
            factory_ogm_cut = Decimal("0")

            if agent_sharing > 0:
                if factory_tier in ("OSA", "OSA 1", "OSA1"):
                    own_comm = full_commission * Decimal("0.70")
                    if hier["oum_name"]:
                        factory_oum_name = hier["oum_name"]
                        factory_oum_cut = full_commission * Decimal("0.20")
                    if hier["ogm_name"]:
                        factory_ogm_cut = full_commission * Decimal("0.10")
                elif factory_tier == "OUM":
                    own_comm = full_commission * Decimal("0.20")

            agent_own_commissions[canonical_name] += own_comm
            agent_sales_totals[canonical_name] += sales_price
            if factory_oum_name:
                override_commissions[factory_oum_name] += factory_oum_cut
            if factory_ogm_cut > 0:
                override_commissions["Gan Lai Soon"] += factory_ogm_cut

            # Safwan's own separate profit-sharing cut on this invoice,
            # independent of the agent's own rate above.
            safwan_comm = Decimal("0")
            if not agent_name.lower() == "safwan":
                safwan_comm = sales_price * (Decimal("0.005") + safwan_sharing)
                override_commissions["Safwan"] += safwan_comm

            obj = SimpleNamespace(
                agent_name=canonical_name,
                customer_name=customer_name,
                invoice_number=invoice_num,
                invoice_date=inv_dt,
                full_payment_date=pay_dt,
                sales_price=float(sales_price),
                rate=float(base_rate),
                profit_sharing=float(agent_sharing),
                basic_commission=float(own_comm),
                is_factory=True,
                gan_lai_soon=float(ogm_comm + factory_ogm_cut),
                senior_override=float(factory_oum_cut),
                senior_override_name=factory_oum_name,
                safwan_override=float(safwan_comm),
                package=prop_type,
                pct5_date=pct5_str,
                pct75_date=pct75_str,
                pct100_date=pct100_str,
                first_payment_date=first_pay_str,
                payout_policy=policy,
            )
            processed_factory.append(obj)
        else:
            # property_type must be passed — without it a Data-page row scoped
            # to a specific property type (e.g. "Factory") is invisible to the
            # matcher, and get_unified_basic_rate_row() currently degrades to
            # picking an arbitrary same-effective-date row instead of erroring.
            # Rate (and the Role it's looked up against) are based on Invoice
            # Date, not Payment Date — a sale invoiced in September still gets
            # September's rate/role even if payment completes months later.
            rate = basic.get_own_commission_rate(info, agent_comm_field, inv_dt, property_type=prop_type)
            own_comm = sales_price * rate
            agent_own_commissions[canonical_name] += own_comm
            agent_sales_totals[canonical_name] += total

            # OUM override: 0.5% of total amount from OSA/OSA 1
            tier = info["tier"]
            if tier == "OSA 1":
                oum_p = info.get("oum_parent")
                if oum_p:
                    override_commissions[oum_p] += total * Decimal("0.005")
            elif tier == "OSA":
                oum_p = info.get("oum_parent")
                internal_senior = info.get("internal_senior_parent")
                if oum_p:
                    override_commissions[oum_p] += total * Decimal("0.005")
                if internal_senior:
                    override_commissions[internal_senior] += total * Decimal("0.005")

            obj = SimpleNamespace(
                agent_name=canonical_name,
                customer_name=customer_name,
                invoice_number=invoice_num,
                invoice_date=inv_dt,
                full_payment_date=pay_dt,
                sales_price=float(sales_price),
                rate=float(rate),
                profit_sharing=0.0,
                basic_commission=float(own_comm),
                is_factory=False,
                gan_lai_soon=float(ogm_comm),
                package=prop_type,
                pct5_date=pct5_str,
                pct75_date=pct75_str,
                pct100_date=pct100_str,
                first_payment_date=first_pay_str,
                payout_policy=policy,
            )
            processed_invoices.append(obj)

    all_lines = processed_invoices + processed_factory

    # Build summary table
    all_agents_set = set(agent_own_commissions.keys()) | set(override_commissions.keys())
    table1 = []
    for agent in sorted(all_agents_set):
        own = float(agent_own_commissions[agent])
        override = float(override_commissions.get(agent, 0))
        total_comm = own + override
        sales_total = float(agent_sales_totals[agent])
        table1.append([agent, f"{sales_total:,.2f}", f"{own:,.2f}", f"{override:,.2f}", f"{total_comm:,.2f}"])

    total_commission = sum(agent_own_commissions.values()) + sum(override_commissions.values())

    meta = {
        "agents": len(all_agents_set),
        "invoices": len(all_lines),
        "total_commission": total_commission,
        "filter": "paid=TRUE; full_payment_date year; agent outsource" + (" (H1)" if h1_only else ""),
        "override_commissions": dict(override_commissions),
        "own_commissions": dict(agent_own_commissions),
    }

    return table1, processed_invoices, processed_factory, meta, all_lines


def fetch_outsource_nfp(year: int, h1_only: bool = True):
    """Returns (agent_rows, detail_rows, meta, nfp_rows, nfp_by_inv_all).

    ``nfp_by_inv_all`` maps every invoice_number to its NFP row regardless of
    payment status, for System Price/Net Floor Price lookups on invoices that
    haven't reached full payment yet (``nfp_rows`` only has fully-paid ones).
    """
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp = _load_module("out_nfp_commission", nfp_dir / "outsource_nfp_commission.py")
    nfp_paths = _load_module("out_nfp_paths", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    rows, summary = nfp.build_report(year)
    nfp_by_inv_all = {r.invoice_number.strip(): r for r in rows if r.invoice_number}
    rows = [r for r in rows if _nfp_payout_date(r)]
    if h1_only:
        rows = [r for r in rows if _parse_month(_nfp_payout_date(r)) in range(1, 7)]
    accumulated = {}
    for r in rows:
        if not r.agent_name:
            continue
        if r.agent_name not in accumulated:
            accumulated[r.agent_name] = {"sales_price": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "nfp_commission": 0.0}
        a = accumulated[r.agent_name]
        a["sales_price"] += r.sales_price
        a["system_price"] += r.system_price
        a["net_floor_price"] += r.net_floor_price or 0.0
        a["nfp_commission"] += r.nfp_commission
    summary = {
        "report_year": year,
        "total_qualifying_agents": len(accumulated),
        "total_invoices": len(rows),
        "total_nfp_commission": round(sum(r.nfp_commission for r in rows), 2),
        "accumulated_by_agent": {
            agent: {k: round(v, 2) for k, v in totals.items()}
            for agent, totals in sorted(accumulated.items())
        }
    }
    agent_rows = nfp.build_agent_summary_table(summary)
    detail_rows = nfp.display_rows_as_lists(rows, summary)
    meta = {
        "agents": summary.get("total_qualifying_agents", 0),
        "invoices": summary.get("total_invoices", 0),
        "total_commission": Decimal(str(summary.get("total_nfp_commission", 0))),
        "filter": "payment 100%; invoice_date year; outsource" + (" (H1)" if h1_only else ""),
    }
    return agent_rows, detail_rows, meta, rows, nfp_by_inv_all


def fetch_outsource_anp(year: int, h1_only: bool = True):
    """Returns (summary_rows, detail_rows, meta)."""
    anp_path = _anp_script_path()
    anp = _load_module("out_anp_commission", anp_path)
    base_url = os.getenv("PG_PROXY_URL", "").strip().rstrip("/")
    if base_url.endswith("/api/sql"):
        base_url = base_url[: -len("/api/sql")]
    token = anp.normalize_proxy_token(os.getenv("PG_PROXY_TOKEN", ""))
    db_name = os.getenv("PG_DB_NAME", DEFAULT_DB_NAME).strip()
    if not base_url or not token:
        raise RuntimeError(_token_help_message())
    anp.validate_proxy_token(token)
    client = anp.PostgresProxyClient(base_url, token, db_name)
    agent_types = ["outsource"]
    agents = anp.fetch_agents(client, agent_types)
    agent_ids = [str(a["bubble_id"]) for a in agents]
    invoices = anp.fetch_invoices_for_agents(client, agent_ids)
    invoice_ids = [str(i["bubble_id"]) for i in invoices if i.get("bubble_id")]
    customer_ids = list({str(i["linked_customer"]) for i in invoices if i.get("linked_customer")})
    planning = anp.fetch_payment_planning(client, invoice_ids)
    customers = anp.fetch_customers(client, customer_ids)
    period_start = date(year, 1, 1)
    period_end = date(year, 6, 30) if h1_only else date(year, 12, 31)
    payout_label = f"invoice-year-{year}-h1" if h1_only else f"invoice-year-{year}"
    detail_rows, summary_rows = anp.build_report_rows(
        agents, invoices, planning, customers, period_start, period_end, payout_label
    )
    total_comm = sum(Decimal(str(r.get("anp_commission", 0))) for r in summary_rows)
    meta = {
        "agents": len(summary_rows),
        "invoices": len(detail_rows),
        "total_commission": total_comm,
        "filter": "invoice_date in year; 1st payment secured; outsource" + (" (H1)" if h1_only else ""),
    }
    return summary_rows, detail_rows, meta


def fetch_monthly_contest(month: int | None = None, year: int | None = None):
    """Returns (t1_headers, t1_rows, t2_headers, t2_rows, t3_headers, t3_rows).

    Company-wide data (Team Championship / Golden Boot / Cases-and-Awards) — not
    split by internal/outsource agent type. ``month`` selects the contest sheet
    (defaults to the current calendar month inside calculate_monthly_contest).
    """
    try:
        contest_path = REPO_ROOT / "6. Monthly Contest" / "3. Python Script" / "monthly_contest.py"
        contest_mod = _load_module("monthly_contest", contest_path)
        token, _, _ = _resolve_proxy_credentials()
        df_t1, df_t2, df_t3, *_ = contest_mod.calculate_monthly_contest(
            token=token, base_path=REPO_ROOT / "6. Monthly Contest", month=month, year=year
        )
        df_t1 = df_t1.fillna("-")
        df_t2 = df_t2.fillna("-")
        df_t3 = df_t3.fillna("-")
        return (
            list(df_t1.columns), [list(r) for r in df_t1.values],
            list(df_t2.columns), [list(r) for r in df_t2.values],
            list(df_t3.columns), [list(r) for r in df_t3.values],
        )
    except Exception as e:
        print(f"  Warning: Monthly Contest fetch failed: {e}")
        return [], [], [], [], [], []


def fetch_outsource_ega_esa(year: int, may_only: bool = False, upto_month: int | None = None):
    """Returns (t1, t2, t3, h1, h2, h3)."""
    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    ega = _load_module("out_ega_esa", ega_dir / "outsource_EGA_ESA_Awards.py")
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_paths = _load_module("out_nfp_paths2", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    os.environ["POSTGRES_PROXY_TOKEN"] = token
    api_client_path = ega_dir / "api_client.py"
    if not api_client_path.is_file():
        api_client_path = nfp_dir / "api_client.py"
    _load_module("api_client_out", api_client_path)
    sys.modules["api_client"] = sys.modules["api_client_out"]
    from api_client import query_sql
    
    if upto_month is None:
        upto_month = 5 if may_only else None
        
    sql = ega._invoices_sql(year, upto_month=upto_month)
    rows = query_sql(sql)
    lines, agent_ep, agent_sales, agent_eligibility = ega.build_report(rows)
    t1 = ega.build_table1(agent_ep, agent_sales, agent_eligibility)
    t2 = ega.build_table2(lines, agent_eligibility)
    t3 = ega.build_table3(lines, agent_eligibility)
    return t1, t2, t3, ega.T1_HEADERS, ega.T2_HEADERS, ega.T3_HEADERS


def fetch_outsource_ega_raw(year: int):
    """Returns raw invoices for outsource agents from the database."""
    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    ega = _load_module("out_ega_esa", ega_dir / "outsource_EGA_ESA_Awards.py")
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    nfp_paths = _load_module("out_nfp_paths2", nfp_dir / "nfp_paths.py")
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or nfp_paths.get_proxy_token()
    if not token:
        raise RuntimeError(nfp_paths.proxy_token_help())
    os.environ["POSTGRES_PROXY_TOKEN"] = token
    api_client_path = ega_dir / "api_client.py"
    if not api_client_path.is_file():
        api_client_path = nfp_dir / "api_client.py"
    _load_module("api_client_out", api_client_path)
    sys.modules["api_client"] = sys.modules["api_client_out"]
    from api_client import query_sql
    sql = ega._invoices_sql(year, upto_month=None)
    return query_sql(sql)


def fetch_production_bonus(year: int):
    """Returns prod_data dict with oum_summary, ogm_summary, team_detail, headers.

    Always covers Jan 1 of `year` through today (real payments received so
    far) — there's no month cutoff to pass in; the report is always
    up to date."""
    pb_path = REPO_ROOT / "5. Production Bonus" / "3. Python Script" / "full_outsource_Production_Bonus.py"
    if not pb_path.is_file():
        print(f"  Production Bonus script not found: {pb_path}")
        return None
    try:
        pb = _load_module("out_production_bonus", pb_path)
        result = pb.build_report(year)
        return result
    except Exception as e:
        print(f"  Warning: Production Bonus fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Summary table builders
# ---------------------------------------------------------------------------

def fetch_invoice_dates(year: int, basic_module) -> dict[str, tuple[str, str, str]]:
    token = os.environ.get("PG_PROXY_TOKEN", "") or basic_module._env("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError(_token_help_message())
    proxy_url = basic_module._normalize_proxy_url(
        basic_module._env("PG_PROXY_URL", DEFAULT_PROXY_URL)
    )
    db_name = basic_module._env("PG_PROXY_DB") or basic_module._env("PG_DB_NAME", DEFAULT_DB_NAME)
    sql = """
    SELECT 
      COALESCE(NULLIF(TRIM(invoice_number), ''), bubble_id) AS inv_key,
      invoice_date,
      "1st_payment_date",
      full_payment_date
    FROM invoice
    """
    payload = basic_module._proxy_sql(
        proxy_url=proxy_url,
        db_name=db_name,
        token=token,
        sql=sql,
        params=[],
    )
    
    def _clean_dt(v):
        if not v or str(v).lower() in ("none", "null"):
            return ""
        return str(v)[:10]

    dates_map = {}
    for r in payload.get("rows") or []:
        inv_key = str(r.get("inv_key") or "").strip()
        inv_date = _clean_dt(r.get("invoice_date"))
        first_pay = _clean_dt(r.get("1st_payment_date"))
        full_pay = _clean_dt(r.get("full_payment_date"))
        dates_map[inv_key] = (inv_date, first_pay, full_pay)
    return dates_map


def get_dates_for_invoices(inv_nums: list[str], dates_map: dict) -> tuple[str, str, str]:
    inv_dates = set()
    first_pays = set()
    full_pays = set()
    for num in inv_nums:
        num_clean = str(num).strip()
        if num_clean in dates_map:
            d, p1, p2 = dates_map[num_clean]
            if d: inv_dates.add(d)
            if p1: first_pays.add(p1)
            if p2: full_pays.add(p2)
            
    inv_date_str = "<br/>".join(sorted(inv_dates)) if inv_dates else "-"
    first_pay_str = "<br/>".join(sorted(first_pays)) if first_pays else "-"
    full_pay_str = "<br/>".join(sorted(full_pays)) if full_pays else "-"
    
    return inv_date_str, first_pay_str, full_pay_str


def _full_payment_display(dates: tuple) -> str:
    """Full/75% Payment Date cell: once an invoice exists but hasn't reached
    that payment milestone yet, show "pending" instead of "-"."""
    inv_date, _, full_pay = dates
    if full_pay and full_pay != "-":
        return full_pay
    if inv_date and inv_date != "-":
        return "pending"
    return full_pay


def _balance_payment_display(cust_basic_lines: list, dates: tuple) -> str:
    """The "75% Payment Date" cell for a Basic Commission customer row.

    A multi-stage invoice settles its balance at the 75% milestone, so that is
    the date this column has to show. It used to render the invoice's
    full_payment_date -- the 100% date -- under a "75% Payment Date" heading,
    which made every invoice sitting between the two milestones read "pending"
    while its balance was in fact due and paid. 17 of 115 July/August rows were
    doing that.

    Single-stage (pre-July) invoices genuinely settle at 100% and keep the old
    display, as do NFP rows, which are never advanced and so never reach this
    function.
    """
    if not any(_is_new_policy_line(ln) for ln in cust_basic_lines):
        return _full_payment_display(dates)
    reached = sorted({
        str(getattr(ln, "pct75_date", "") or "").strip()
        for ln in cust_basic_lines
        if _is_new_policy_line(ln) and str(getattr(ln, "pct75_date", "") or "").strip()
    })
    if reached:
        return "<br/>".join(reached)
    # Multi-stage but the 75% milestone has not been reached: genuinely pending.
    inv_date = dates[0] if dates else ""
    return "pending" if inv_date and inv_date != "-" else _full_payment_display(dates)


def _is_cleaning_inspection_service(r_nfp) -> bool:
    """True when the NFP row's item is a standalone cleaning/inspection service
    call (e.g. "Cleaning And Inspection Service") rather than an actual solar
    panel sale -- such invoices have no panel data, so Net Floor Price is not
    a meaningful figure for them."""
    if r_nfp is None:
        return False
    text = " ".join(
        str(getattr(r_nfp, attr, "") or "")
        for attr in ("package_description", "all_item_text")
    ).lower()
    return "cleaning" in text and "inspection" in text


def is_before_october_2025(date_str: str) -> bool:
    if not date_str or date_str == "-":
        return False
    parts = [p.strip() for p in date_str.replace("<br/>", "\n").replace("<br>", "\n").split("\n") if p.strip()]
    for p in parts:
        if len(p) >= 10 and p[4] in ("-", "/") and p[7] in ("-", "/"):
            try:
                y = int(p[:4])
                m = int(p[5:7])
                if y < 2025 or (y == 2025 and m < 10):
                    return True
            except ValueError:
                pass
    return False


def _has_jinko_power_output(pkg_str: str, panel_ratings: list = None) -> bool:
    if panel_ratings:
        for r in panel_ratings:
            if r is not None and str(r).isdigit() and int(r) in (590, 620, 650):
                return True
    if not pkg_str or pkg_str == "-":
        return False
    s = str(pkg_str).lower()
    if any(w in s for w in ["590", "620", "650", "66hl4"]):
        return True
    return False


def build_agent_summary_rows(
    agent: str,
    basic_dates: tuple[str, str, str],
    basic_comm_rm: float,
    basic_referral_rm: float,
    basic_safwan_rm: float,
    nfp_dates: tuple[str, str, str],
    nfp_comm_rm: float,
    nfp_referral_rm: float,
    nfp_safwan_rm: float,
    anp_dates: tuple[str, str, str],
    anp_comm_rm: float,
    anp_referral_rm: float,
    anp_safwan_rm: float,
    include_anp: bool = True,
    gan_lai_soon_basic: float = 0.0,
    gan_lai_soon_nfp: float = 0.0,
    gan_lai_soon_anp: float = 0.0,
    is_outsource: bool = False,
    basic_override_rm: Any = 0.0,
    basic_sales_rm: float = 0.0,
    nfp_sales_rm: float = 0.0,
    basic_packages: str = "-",
    nfp_packages: str = "-",
    anp_packages: str = "-",
    basic_system_rm: float = 0.0,
    basic_nfp_rm: float = 0.0,
    nfp_system_rm: float = 0.0,
    nfp_nfp_rm: float = 0.0,
    anp_system_rm: float = 0.0,
    anp_nfp_rm: float = 0.0,
    total_invoices: int = 0,
) -> list[list[str]]:
    """
    Builds stacked rows for one agent:
      [agent, total_invoices, basic_packages, system_price_str, netfloor_price_str, sales_price_str, "Basic Commission", total_comm_str, override_rm_str]
    """
    b_inv, b_p1, b_p2 = basic_dates
    n_inv, n_p1, n_p2 = nfp_dates
    a_inv, a_p1, a_p2 = anp_dates

    is_before_oct_25 = is_before_october_2025(b_inv) or is_before_october_2025(n_inv)
    has_power_out = (
        nfp_comm_rm != 0 or nfp_nfp_rm != 0 or nfp_sales_rm != 0 or basic_nfp_rm != 0 or
        _has_jinko_power_output(nfp_packages) or _has_jinko_power_output(basic_packages) or
        (not nfp_packages or nfp_packages == "-")
    )

    def _fmt(v):
        if isinstance(v, str):
            return v
        return f"RM {v:,.2f}" if v != 0 else "-"

    def _fmt_comm(v, date_str):
        if isinstance(v, str):
            return v
        if v != 0:
            return f"RM {v:,.2f}"
        if date_str and "2026" in str(date_str):
            return "pending full payment"
        return "-"

    def _fmt_nfp_comm(v, date_str):
        if isinstance(v, str):
            return v
        if is_before_oct_25:
            return "invoice before Oct 25"
        if not has_power_out:
            return "JinkoSolar package not included"
        if v != 0:
            return f"RM {v:,.2f}"
        if date_str and "2026" in str(date_str):
            return "pending full payment"
        return "-"

    def _fmt_nfp_cell(v):
        if isinstance(v, str):
            return v
        if is_before_oct_25:
            return "invoice before Oct 25"
        if not has_power_out:
            return "JinkoSolar package not included"
        return f"RM {v:,.2f}" if v != 0 else "-"

    # If override is a string, use directly, otherwise format
    if isinstance(basic_override_rm, str):
        override_str = basic_override_rm
    else:
        override_str = _fmt(basic_override_rm)

    pkgs = []
    if basic_packages and basic_packages != "-":
        pkgs.append(basic_packages)
    if nfp_packages and nfp_packages != "-" and nfp_packages not in pkgs:
        pkgs.append(nfp_packages)
    combined_pkgs = "<br/>".join(pkgs) if pkgs else "-"

    tot_system = max(basic_system_rm, nfp_system_rm)
    tot_nfp = max(basic_nfp_rm, nfp_nfp_rm)
    tot_sales = max(basic_sales_rm, nfp_sales_rm)

    b_val = basic_comm_rm if isinstance(basic_comm_rm, (int, float)) else 0.0
    n_val = nfp_comm_rm if isinstance(nfp_comm_rm, (int, float)) else 0.0
    tot_comm = b_val + n_val

    comm_str = _fmt_comm(tot_comm, b_inv or n_inv)

    merged_row = [
        agent,
        str(total_invoices),
        combined_pkgs,
        _fmt(tot_system),
        _fmt_nfp_cell(tot_nfp),
        _fmt(tot_sales),
        comm_str,
        override_str
    ]
    return [merged_row]


def build_customer_summary_rows(
    agent: str,
    customer: str,
    rate_basic: str,
    rate_nfp: str,
    rate_anp: str,
    basic_dates: tuple[str, str, str],
    basic_comm_rm: float,
    basic_referral: str,
    basic_referral_rm: float,
    basic_safwan: str,
    basic_safwan_rm: float,
    nfp_dates: tuple[str, str, str],
    nfp_comm_rm: float,
    nfp_referral: str,
    nfp_referral_rm: float,
    nfp_safwan: str,
    nfp_safwan_rm: float,
    anp_dates: tuple[str, str, str],
    anp_comm_rm: float,
    anp_referral: str,
    anp_referral_rm: float,
    anp_safwan: str,
    anp_safwan_rm: float,
    is_outsource: bool = False,
    gan_lai_soon_label: str = "-",
    gan_lai_soon_basic: float = 0.0,
    gan_lai_soon_nfp: float = 0.0,
    gan_lai_soon_anp: float = 0.0,
) -> list[list[str]]:
    """Builds 3 stacked rows per (agent, customer) combo with dates directly after customer."""
    b_inv, b_p1, b_p2 = basic_dates
    n_inv, n_p1, n_p2 = nfp_dates
    a_inv, a_p1, a_p2 = anp_dates

    is_before_oct_25 = is_before_october_2025(b_inv) or is_before_october_2025(n_inv)

    def _fmt(v): return f"RM {v:,.2f}" if v != 0 else "-"
    def _fmt_nfp(v):
        if v != 0:
            return f"RM {v:,.2f}"
        if is_before_oct_25:
            return "invoice before Oct 25"
        return "-"
    def _fmt_nfp_str(val):
        if val and val != "-":
            return val
        if is_before_oct_25:
            return "invoice before Oct 25"
        return "-"

    rate_nfp_val = _fmt_nfp_str(rate_nfp)
    nfp_referral_val = nfp_referral if (nfp_referral and nfp_referral != "-") else "-"
    nfp_safwan_val = nfp_safwan if (nfp_safwan and nfp_safwan != "-") else "-"

    if is_outsource:
        rows = [
            [agent, customer, b_inv, b_p1, b_p2, rate_basic, "Basic Commission", _fmt(basic_comm_rm), basic_referral, _fmt(basic_referral_rm), basic_safwan, _fmt(basic_safwan_rm), _fmt(gan_lai_soon_basic)],
            ["",    "",       n_inv, n_p1, n_p2, rate_nfp_val,  "Net Floor Price Commission", _fmt_nfp(nfp_comm_rm), nfp_referral_val, _fmt(nfp_referral_rm), nfp_safwan_val, _fmt(nfp_safwan_rm), _fmt_nfp(gan_lai_soon_nfp)],
            ["",    "",       a_inv, a_p1, a_p2, rate_anp,  "ANP Commission", _fmt(anp_comm_rm), anp_referral, _fmt(anp_referral_rm), anp_safwan, _fmt(anp_safwan_rm), _fmt(gan_lai_soon_anp)],
        ]
    else:
        rows = [
            [agent, customer, b_inv, b_p1, b_p2, rate_basic, "Basic Commission", _fmt(basic_comm_rm), basic_referral, _fmt(basic_referral_rm), basic_safwan, _fmt(basic_safwan_rm)],
            ["",    "",       n_inv, n_p1, n_p2, rate_nfp_val,  "Net Floor Price Commission", _fmt_nfp(nfp_comm_rm), nfp_referral_val, _fmt(nfp_referral_rm), nfp_safwan_val, _fmt(nfp_safwan_rm)],
            ["",    "",       a_inv, a_p1, a_p2, rate_anp,  "ANP Commission", _fmt(anp_comm_rm), anp_referral, _fmt(anp_referral_rm), anp_safwan, _fmt(anp_safwan_rm)],
        ]
    return rows


# ---------------------------------------------------------------------------
# Referral overrides entered on the dashboard's Basic & NFP table
# ---------------------------------------------------------------------------

_REFERRAL_OVERRIDE_CACHE: dict[str, dict] = {}


def referral_overrides(year) -> dict:
    """Hand-entered referrals for `year`, keyed by (agent, customer) lowercased.

    The ERP names a referrer on only a handful of invoices, so most referrals
    are typed on the dashboard instead. Read once per year and cached: this is
    called for every invoice line of every month.
    """
    key = str(year)
    hit = _REFERRAL_OVERRIDE_CACHE.get(key)
    if hit is not None:
        return hit
    out: dict = {}
    try:
        dashboard_dir = REPO_ROOT / "8. Web Dashboard"
        if str(dashboard_dir) not in sys.path:
            sys.path.insert(0, str(dashboard_dir))
        import db as _dashboard_db
        for r in _dashboard_db.list_referral_overrides(key):
            agent = str(r.get("agent") or "").strip().lower()
            customer = str(r.get("customer") or "").strip().lower()
            if not agent or not customer:
                continue
            out[(agent, customer)] = {
                "name": str(r.get("referral_name") or "").strip(),
                "rate": _referral_rate_fraction(r.get("rate")),
            }
    except Exception as exc:
        print(f"[Referral] Warning: could not read referral overrides for {key} "
              f"({exc}); the standard rate applies to every row.", file=sys.stderr)
        out = {}
    _REFERRAL_OVERRIDE_CACHE[key] = out
    return out


def _referral_rate_fraction(raw):
    """"2.5" (percent, as typed on the dashboard) -> Decimal("0.025").

    A blank or unreadable rate returns None so the caller falls back to the
    scripts' own standard rate rather than silently paying nothing.
    """
    from decimal import Decimal, InvalidOperation
    text = str(raw or "").strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text) / Decimal("100")
    except (InvalidOperation, ValueError):
        return None


def apply_referral_overrides(lines: list, year) -> None:
    """Stamp each invoice line with its customer's hand-entered referral.

    Done once, in place, before any of the summary loops run, so every reader
    downstream -- the customer table, the agent totals, Table 4 -- sees the same
    name and rate without each having to look the override up for itself.
    """
    overrides = referral_overrides(year)
    if not overrides:
        return
    for ln in lines:
        hit = overrides.get((str(getattr(ln, "agent_name", "")).strip().lower(),
                             str(getattr(ln, "customer_name", "")).strip().lower()))
        if not hit:
            continue
        if hit["name"]:
            ln.referral_name = hit["name"]
        if hit["rate"] is not None:
            ln.referral_rate_override = hit["rate"]


# ---------------------------------------------------------------------------
# Build the two summary tables for internal
# ---------------------------------------------------------------------------

def build_internal_summary_tables(
    basic_t1: list,
    basic_lines: list,
    basic_t4: list,
    nfp_agent_rows: list,
    nfp_rows: list,
    anp_summary_rows: list,
    anp_detail: list,
    year: int,
    invoice_dates_map: dict,
    month: int | None = None,
    nfp_by_inv_all: dict | None = None) -> tuple[dict[int, list[list[str]]], dict[int, list[list[str]]], dict[int, list[list[str]]], dict[int, list[list[str]]], dict[int, dict[str, dict]]]:
    """
    Returns:
      agent_summary_by_month - dict of month -> rows for "Summary Internal Agent Commission"
      customer_summary_by_month - dict of month -> rows for "Summary Internal Agent Commission by Customer"
      agent_anp_by_month - dict of month -> rows for agent ANP Commission
      customer_anp_by_month - dict of month -> rows for agent ANP Commission by Customer
      agent_totals_by_month - dict of month -> {agent: {"basic","nfp","anp","sales","invoices"}}
    """
    nfp_by_inv_all = nfp_by_inv_all or {}
    basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
    basic = _load_module("int_basic_commission", basic_path)

    # In-place format names in Title Case for all loaded records
    for ln in basic_lines:
        ln.agent_name = to_title_case(ln.agent_name)
        ln.customer_name = to_title_case(ln.customer_name)
        ln.referral_name = to_title_case(ln.referral_name)
    apply_referral_overrides(basic_lines, year)
    for r in nfp_rows:
        r.agent_name = to_title_case(r.agent_name)
        r.customer_name = to_title_case(r.customer_name)
    for r in anp_detail:
        if r.get("agent_name"):
            r["agent_name"] = to_title_case(r["agent_name"])
        if r.get("customer_name"):
            r["customer_name"] = to_title_case(r["customer_name"])

    agent_summary_by_month = {}
    customer_summary_by_month = {}
    agent_anp_by_month = {}
    customer_anp_by_month = {}
    # Per-agent Basic+NFP+ANP totals, one entry per agent per month -- feeds
    # the unified (Internal+Outsource combined) Executive Summary, Top
    # Performers ranking and Effective Commission Rate on the report overview
    # pages. Captured straight from the same per-agent numbers this loop
    # already computes for the detailed ledger, so it can never drift from
    # what the ledger itself shows.
    agent_totals_by_month = {}
    nfp_by_inv = {r.invoice_number.strip(): r for r in nfp_rows if r.invoice_number}

    for m in range(1, 13):
        if month is not None and m != month:
            continue
        basic_comm_by_agent = {}
        basic_override_by_agent = {}
        basic_override_breakdown = defaultdict(lambda: defaultdict(float))
        # Same contributions as basic_override_breakdown, but split by the
        # SOURCE (executive)'s customer too, so the customer-level table can
        # show exactly which of the executive's own sales generated each
        # amount (instead of lumping the whole month's total onto the senior's row).
        basic_override_by_customer = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        basic_rate_by_agent = {}
        referral_by_agent = {}
        referral_name_by_agent = {}
        safwan_rate_by_agent = {}

        month_basic_lines = _expand_basic_lines_for_month(basic_lines, m)
        for ln in month_basic_lines:
            agent = ln.agent_name.strip()
            customer = ln.customer_name.strip()
            comm = float(ln.basic_commission)
            basic_comm_by_agent[agent] = basic_comm_by_agent.get(agent, 0.0) + comm

            rate_val = float(ln.commission_rate) * 100
            rate_str = f"{rate_val:g}%"
            if getattr(ln, "package", "") == "Factory" and float(getattr(ln, "profit_sharing", 0)) > 0:
                ps_str = f"{float(ln.profit_sharing)*100:g}%"
                rate_str += f" + {ps_str}"
            existing = basic_rate_by_agent.get(agent, "")
            if rate_str not in existing:
                basic_rate_by_agent[agent] = (existing + "<br/>" + rate_str).lstrip("<br/>")

            # RM300-only contributions (invoice has only reached >=5% payment,
            # balance not finalized yet) don't attribute overrides/referral
            # fee -- those are recognized once, in the month the balance
            # (>=75% payment) lands. See _expand_basic_lines_for_month().
            if getattr(ln, "_skip_override", False):
                continue

            # Senior override
            senior = _int_get_reporting_senior(ln.agent_name)
            if senior:
                senior = to_title_case(senior)
                override = float(ln.net_base) * 0.0025
                exec_name = to_title_case(ln.agent_name)
                basic_override_breakdown[senior][exec_name] += override
                basic_override_by_agent[senior] = basic_override_by_agent.get(senior, 0.0) + override
                basic_override_by_customer[agent][customer][senior] += override

            # Referral fee
            if basic._is_valid_referral(ln.referral_name):
                sales_price = float(ln.sales_price)
                rate = float(getattr(ln, "referral_rate_override", None)
                             or basic._referral_rate(ln.invoice_date))
                fee = sales_price * rate
                referral_by_agent[agent] = referral_by_agent.get(agent, 0.0) + fee
                ref_name = ln.referral_name.strip()
                existing_ref = referral_name_by_agent.get(agent, "")
                if ref_name and ref_name not in existing_ref:
                    referral_name_by_agent[agent] = (existing_ref + ", " + ref_name).lstrip(", ")

            # Safwan override
            if getattr(ln, "package", "") == "Factory":
                senior_fac = _int_get_reporting_senior(ln.agent_name)
                if senior_fac and "safwan" in senior_fac.lower():
                    ps = float(getattr(ln, "profit_sharing", 0)) * 100
                    rate_str_saf = f"0.5% + {ps:g}%"
                    safwan_rate_by_agent[ln.agent_name.strip()] = rate_str_saf

        # NFP commission
        nfp_comm_by_agent = {}
        nfp_rate_by_agent = {}
        month_nfp_rows = [r for r in nfp_rows if _parse_month(_nfp_payout_date(r)) == m]
        for r in month_nfp_rows:
            agent = r.agent_name.strip()
            nfp_comm_by_agent[agent] = nfp_comm_by_agent.get(agent, 0.0) + float(r.nfp_commission)
            rate = determine_nfp_rate([r])
            existing = nfp_rate_by_agent.get(agent, "")
            if rate not in existing:
                nfp_rate_by_agent[agent] = (existing + "<br/>" + rate).lstrip("<br/>")

        # ANP commission
        anp_comm_by_agent = {}
        anp_agent_monthly = defaultdict(float)
        month_anp_detail = [r for r in anp_detail if _parse_month(r.get("invoice_date")) == m]
        for r in month_anp_detail:
            agent = str(r.get("agent_name", "")).strip()
            comm = float(r.get("anp_commission_accumulated_tier", 0.0))
            if comm > anp_agent_monthly[agent]:
                anp_agent_monthly[agent] = comm
        for agent, comm in anp_agent_monthly.items():
            anp_comm_by_agent[agent] = comm

        month_agents = sorted(
            set(
                list(basic_comm_by_agent.keys()) +
                list(nfp_comm_by_agent.keys()) +
                list(anp_comm_by_agent.keys())
            ),
            key=internal_agent_sort_key
        )

        # Build rows
        agent_rows = []
        agent_anp_rows = []
        for agent in month_agents:
            basic_comm = basic_comm_by_agent.get(agent, 0.0)
            basic_override = basic_override_by_agent.get(agent, 0.0)
            basic_total = basic_comm  # Exclude override from basic total / own commission
            nfp_comm = nfp_comm_by_agent.get(agent, 0.0)
            anp_comm = anp_comm_by_agent.get(agent, 0.0)
            referral_rm = referral_by_agent.get(agent, 0.0)
            safwan_rm = basic_override

            basic_invs = [ln.invoice_number for ln in month_basic_lines if ln.agent_name.strip() == agent]
            nfp_invs = [r.invoice_number for r in month_nfp_rows if r.agent_name.strip() == agent]
            anp_invs = [r.get("invoice_number") for r in month_anp_detail if str(r.get("agent_name", "")).strip() == agent and r.get("invoice_number")]
            total_invoices_count = len(set(basic_invs + nfp_invs))

            basic_dates = get_dates_for_invoices(basic_invs, invoice_dates_map)
            nfp_dates = get_dates_for_invoices(nfp_invs, invoice_dates_map)
            anp_dates = get_dates_for_invoices(anp_invs, invoice_dates_map)

            # Special request: Include invoice date for JOSHUA YAP JIA HAO and Ng Zhan Yi
            # in Basic/NFP lines in Summary Internal Agent Commission
            if agent.strip().upper() in ("JOSHUA YAP JIA HAO", "NG ZHAN YI"):
                if not basic_invs and anp_dates[0] != "-":
                    basic_dates = (anp_dates[0], "-", "-")
                if not nfp_invs and anp_dates[0] != "-":
                    nfp_dates = (anp_dates[0], "-", "-")

            basic_sales = sum(float(ln.sales_price) for ln in month_basic_lines if ln.agent_name.strip() == agent)
            nfp_sales = sum(float(r.sales_price) for r in month_nfp_rows if r.agent_name.strip() == agent)

            agent_totals_by_month.setdefault(m, {})[agent] = {
                "basic": basic_total, "nfp": nfp_comm, "anp": anp_comm,
                "sales": basic_sales + nfp_sales, "invoices": total_invoices_count,
                "referral": referral_rm,
            }

            basic_system = sum(float(nfp_by_inv[ln.invoice_number.strip()].system_price) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.invoice_number.strip() in nfp_by_inv)
            basic_nfp = sum(float(nfp_by_inv[ln.invoice_number.strip()].net_floor_price or 0.0) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.invoice_number.strip() in nfp_by_inv)

            nfp_system = sum(float(r.system_price) for r in month_nfp_rows if r.agent_name.strip() == agent)
            nfp_nfp = sum(float(r.net_floor_price or 0.0) for r in month_nfp_rows if r.agent_name.strip() == agent)

            # Package types
            basic_pkgs_set = set(get_invoice_package(ln.invoice_number, ln) for ln in month_basic_lines if ln.agent_name.strip() == agent)
            basic_packages = "<br/>".join(sorted(basic_pkgs_set)) if basic_pkgs_set else "-"
            
            nfp_pkgs_set = set(get_invoice_package(r.invoice_number, r) for r in month_nfp_rows if r.agent_name.strip() == agent)
            nfp_packages = "<br/>".join(sorted(nfp_pkgs_set)) if nfp_pkgs_set else "-"

            anp_pkgs_set = set(get_invoice_package(r.get("invoice_number"), r) for r in month_anp_detail if str(r.get("agent_name", "")).strip() == agent)
            anp_packages = "<br/>".join(sorted(anp_pkgs_set)) if anp_pkgs_set else "-"

            # Detailed senior override breakdown cell
            basic_override_val = format_senior_override_cell(basic_override_breakdown[agent])

            # Skip pure placeholder rows: no invoices this month AND nothing
            # payable (no basic/NFP commission, referral fee or override).
            # Agents with 0 invoices but real amounts (e.g. override-only
            # seniors) are kept. The ANP row below is appended regardless.
            if total_invoices_count == 0 and not (
                basic_total or nfp_comm or referral_rm or safwan_rm
                or basic_override_val != "-"
            ):
                if anp_comm != 0:
                    a_inv, a_p1, a_p2 = anp_dates
                    agent_anp_rows.append([agent, a_inv, a_p1, a_p2, anp_packages, "ANP Commission", ensure_rm_prefix(f"{anp_comm:,.2f}"), "-", "-"])
                continue

            rows = build_agent_summary_rows(
                agent=agent,
                basic_dates=basic_dates,
                basic_comm_rm=basic_total,
                basic_referral_rm=referral_rm,
                basic_safwan_rm=safwan_rm,
                nfp_dates=nfp_dates,
                nfp_comm_rm=nfp_comm,
                nfp_referral_rm=0.0,
                nfp_safwan_rm=0.0,
                anp_dates=anp_dates,
                anp_comm_rm=anp_comm,
                anp_referral_rm=0.0,
                anp_safwan_rm=0.0,
                include_anp=False,
                is_outsource=False,
                basic_override_rm=basic_override_val,
                basic_sales_rm=basic_sales,
                nfp_sales_rm=nfp_sales,
                basic_packages=basic_packages,
                nfp_packages=nfp_packages,
                anp_packages=anp_packages,
                basic_system_rm=basic_system,
                basic_nfp_rm=basic_nfp,
                nfp_system_rm=nfp_system,
                nfp_nfp_rm=nfp_nfp,
                total_invoices=total_invoices_count,
            )
            agent_rows.extend(rows)

            if anp_comm != 0:
                a_inv, a_p1, a_p2 = anp_dates
                anp_row = [agent, a_inv, a_p1, a_p2, anp_packages, "ANP Commission", ensure_rm_prefix(f"{anp_comm:,.2f}"), "-", "-"]
                agent_anp_rows.append(anp_row)

        if agent_rows:
            agent_summary_by_month[m] = agent_rows
        if agent_anp_rows:
            agent_anp_by_month[m] = agent_anp_rows

        # Customer summary
        basic_by_cust = defaultdict(lambda: {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "is_cleaning_service": False, "advance": 0.0, "advance_date": ""})
        for ln in month_basic_lines:
            key = (ln.agent_name.strip(), ln.customer_name.strip())
            basic_by_cust[key]["comm"] += float(ln.basic_commission)
            basic_by_cust[key]["advance"] += float(getattr(ln, "_advance_deducted", 0) or 0)
            # The date the advance was actually earned (the >=4.99% milestone).
            # NOT the invoice's first payment: a first payment can fall short of
            # the trigger, and dating the advance by it credits that payment
            # with earning something it did not earn.
            _adv_dt = str(getattr(ln, "pct5_date", "") or "").strip()
            if _adv_dt and (not basic_by_cust[key]["advance_date"]
                            or _adv_dt < basic_by_cust[key]["advance_date"]):
                basic_by_cust[key]["advance_date"] = _adv_dt
            basic_by_cust[key]["sales"] += float(ln.sales_price)
            inv_num = ln.invoice_number.strip()
            r_nfp = nfp_by_inv.get(inv_num) or nfp_by_inv_all.get(inv_num)
            if r_nfp:
                basic_by_cust[key]["system_price"] += float(r_nfp.system_price)
                basic_by_cust[key]["net_floor_price"] += float(r_nfp.net_floor_price or 0.0)
                if _is_cleaning_inspection_service(r_nfp):
                    basic_by_cust[key]["is_cleaning_service"] = True
            rate_str = f"{float(ln.commission_rate) * 100:g}%"
            basic_by_cust[key]["rate"].add(rate_str)

        nfp_by_cust = defaultdict(lambda: {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "is_cleaning_service": False})
        for r in month_nfp_rows:
            key = (r.agent_name.strip(), r.customer_name.strip())
            nfp_by_cust[key]["comm"] += float(r.nfp_commission)
            nfp_by_cust[key]["sales"] += float(r.sales_price)
            nfp_by_cust[key]["rate"].add(determine_nfp_rate([r]))
            nfp_by_cust[key]["system_price"] += float(r.system_price)
            nfp_by_cust[key]["net_floor_price"] += float(r.net_floor_price or 0.0)
            if _is_cleaning_inspection_service(r):
                nfp_by_cust[key]["is_cleaning_service"] = True

        customer_rows = []
        customer_anp_rows = []
        for agent in month_agents:
            # Get all customers for basic/nfp
            agent_customers = sorted(list(set(c for a, c in list(basic_by_cust.keys()) + list(nfp_by_cust.keys()) if a == agent)))

            # Accumulated ANP details for the agent
            anp_comm = anp_comm_by_agent.get(agent, 0.0)
            rate_anp = get_anp_tier_label(anp_comm)

            anp_invs = [r.get("invoice_number") for r in month_anp_detail if str(r.get("agent_name", "")).strip() == agent and r.get("invoice_number")]
            anp_dates = get_dates_for_invoices(anp_invs, invoice_dates_map)

            referral_label = referral_name_by_agent.get(agent, "-")
            referral_rm = referral_by_agent.get(agent, 0.0)
            safwan_rm = basic_override_by_agent.get(agent, 0.0)

            show_agent = agent
            if agent_customers:
                for customer in agent_customers:
                    basic_info = basic_by_cust.get((agent, customer), {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0})
                    nfp_info = nfp_by_cust.get((agent, customer), {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0})

                    rate_basic = ", ".join(sorted(basic_info["rate"])) if basic_info["rate"] else "-"
                    rate_nfp = ", ".join(sorted(nfp_info["rate"])) if nfp_info["rate"] else "-"

                    basic_invs = [ln.invoice_number for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer]
                    nfp_invs = [r.invoice_number for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer]

                    # RM300 tranche: paid once a July 2026+ invoice reaches >=5%
                    # payment; pre-July invoices show "invoice before july"
                    # instead (their Basic Commission pays in full at 100%).
                    cust_basic_lines = [ln for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer]
                    rm300_display = _rm300_column_display(cust_basic_lines)

                    basic_dates = get_dates_for_invoices(basic_invs, invoice_dates_map)
                    nfp_dates = get_dates_for_invoices(nfp_invs, invoice_dates_map)

                    # Referral name and RM for this customer
                    cust_referral_names = []
                    cust_referral_rm = 0.0
                    for ln in month_basic_lines:
                        if getattr(ln, "_skip_override", False):
                            continue
                        if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer:
                            if basic._is_valid_referral(ln.referral_name):
                                sales_price = float(ln.sales_price)
                                rate = float(getattr(ln, "referral_rate_override", None)
                                             or basic._referral_rate(ln.invoice_date))
                                fee = sales_price * rate
                                cust_referral_rm += fee
                                ref_name = ln.referral_name.strip()
                                if ref_name not in cust_referral_names:
                                    cust_referral_names.append(ref_name)
                    row_referral_label = ", ".join(cust_referral_names) if cust_referral_names else "-"

                    # Safwan override for this customer
                    row_safwan_rm = 0.0
                    if agent.strip().lower() != "safwan":
                        for ln in month_basic_lines:
                            if ln.agent_name.strip() == "Safwan" and ln.customer_name.strip() == customer and ln.invoice_number in basic_invs:
                                row_safwan_rm += float(ln.basic_commission)

                    # Customer specific package type
                    cust_basic_pkgs = set(get_invoice_package(ln.invoice_number, ln) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer)
                    cust_basic_pkg_str = "<br/>".join(sorted(cust_basic_pkgs)) if cust_basic_pkgs else "-"
                    
                    cust_nfp_pkgs = set(get_invoice_package(r.invoice_number, r) for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer)
                    cust_nfp_pkg_str = "<br/>".join(sorted(cust_nfp_pkgs)) if cust_nfp_pkgs else "-"

                    basic_comm_val = f"{basic_info['comm']:,.2f}" if basic_info['comm'] != 0 else ("pending full payment" if basic_dates[0] and "2026" in str(basic_dates[0]) else "-")
                    row_other_commission = format_other_commission_for_customer(basic_override_by_customer[agent].get(customer, {}))

                    is_before_oct_25 = is_before_october_2025(basic_dates[0]) or is_before_october_2025(nfp_dates[0])
                    cust_all_pkg_text = " ".join(filter(None, [
                        cust_basic_pkg_str,
                        cust_nfp_pkg_str,
                        *[getattr(ln, 'package_description', '') for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(ln, 'all_item_text', '') for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(r, 'package_description', '') for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                        *[getattr(r, 'all_item_text', '') for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                    ]))
                    cust_panel_ratings = [
                        *[getattr(ln, 'panel_rating', None) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(r, 'panel_rating', None) for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                    ]
                    has_power_out = (
                        nfp_info['net_floor_price'] != 0 or nfp_info['comm'] != 0 or nfp_info['sales'] != 0 or
                        basic_info['net_floor_price'] != 0 or
                        _has_jinko_power_output(cust_all_pkg_text, cust_panel_ratings)
                    )

                    if is_before_oct_25:
                        basic_nfp_cell = "invoice before Oct 25"
                        nfp_nfp_cell = "invoice before Oct 25"
                        nfp_comm_val = "invoice before Oct 25"
                    elif not has_power_out:
                        basic_nfp_cell = "JinkoSolar package not included"
                        nfp_nfp_cell = "JinkoSolar package not included"
                        nfp_comm_val = "JinkoSolar package not included"
                    else:
                        basic_nfp_cell = (
                            "cleaning and inspection service" if basic_info['net_floor_price'] == 0 and basic_info.get('is_cleaning_service')
                            else ensure_rm_prefix(f"{basic_info['net_floor_price']:,.2f}" if basic_info['net_floor_price'] != 0 else "-")
                        )
                        nfp_nfp_cell = (
                            "cleaning and inspection service" if nfp_info['net_floor_price'] == 0 and nfp_info.get('is_cleaning_service')
                            else ensure_rm_prefix(f"{nfp_info['net_floor_price']:,.2f}" if nfp_info['net_floor_price'] != 0 else "-")
                        )
                        nfp_comm_val = ensure_rm_prefix(
                            f"{nfp_info['comm']:,.2f}" if nfp_info['comm'] != 0
                            else ("pending full payment" if nfp_dates[0] and "2026" in str(nfp_dates[0]) else "-")
                        )

                    basic_row = [show_agent, customer, basic_dates[0], basic_dates[1]]
                    if m >= 7:
                        basic_row.append(rm300_display)
                    basic_row.extend([
                        _balance_payment_display(cust_basic_lines, basic_dates),
                        cust_basic_pkg_str,
                        ensure_rm_prefix(f"{basic_info['system_price']:,.2f}" if basic_info['system_price'] != 0 else "-"),
                        basic_nfp_cell,
                        ensure_rm_prefix(f"{basic_info['sales']:,.2f}" if basic_info['sales'] != 0 else "-"),
                        "Basic Commission", ensure_rm_prefix(basic_comm_val),
                        row_other_commission,
                        ensure_rm_prefix(f"{row_safwan_rm:,.2f}" if row_safwan_rm != 0 else "-"),
                        to_title_case(row_referral_label), ensure_rm_prefix(f"{cust_referral_rm:,.2f}" if cust_referral_rm != 0 else "-"),
                        # Trailing, and hidden in the UI (HIDDEN_DETAIL_HEADERS
                        # in app.js): appended so no existing column index in
                        # either the dashboard or the PDF pack shifts.
                        rate_basic,
                        f"{basic_info.get('advance', 0.0):,.2f}" if basic_info.get("advance") else "-",
                        basic_info.get("advance_date") or "-",
                    ])
                    customer_rows.append(basic_row)
                    show_agent = ""

                    nfp_row = [show_agent, customer, nfp_dates[0], nfp_dates[1]]
                    if m >= 7:
                        nfp_row.append("-")
                    nfp_row.extend([
                        nfp_dates[2],
                        cust_nfp_pkg_str,
                        ensure_rm_prefix(f"{nfp_info['system_price']:,.2f}" if nfp_info['system_price'] != 0 else "-"),
                        nfp_nfp_cell,
                        ensure_rm_prefix(f"{nfp_info['sales']:,.2f}" if nfp_info['sales'] != 0 else "-"),
                        "Net Floor Price Commission", nfp_comm_val,
                        "-", "-", "-", "-", "-", "-", "-"
                    ])
                    customer_rows.append(nfp_row)

        # Build ANP customer rows: one row per customer per agent (from int_anp_detail)
        anp_by_cust = defaultdict(float)  # (agent, customer) -> max accumulated commission
        anp_cust_inv = defaultdict(list)   # (agent, customer) -> invoice numbers
        anp_sales_by_cust = defaultdict(float) # (agent, customer) -> sum of sales price
        anp_clawback_by_cust = defaultdict(float) # (agent, customer) -> sum of clawbacks
        for r in month_anp_detail:
            ag = str(r.get("agent_name", "")).strip()
            cu = str(r.get("customer_name", "") or "").strip()
            if not cu:
                cu = "(unknown)"
            key = (ag, cu)
            comm = float(r.get("anp_commission_accumulated_tier", 0.0))
            if comm > anp_by_cust[key]:
                anp_by_cust[key] = comm
            inv = r.get("invoice_number")
            if inv and inv not in anp_cust_inv[key]:
                anp_cust_inv[key].append(inv)
            anp_sales_by_cust[key] += float(r.get("invoice_total_amount", 0.0))
            anp_clawback_by_cust[key] += float(r.get("clawback", 0.0))

        # Group by agent, emit one row per customer
        anp_agents_seen = []
        for (ag, cu) in sorted(anp_by_cust.keys(), key=lambda k: (internal_agent_sort_key(k[0]), k[1])):
            if ag not in anp_agents_seen:
                anp_agents_seen.append(ag)
        for ag in anp_agents_seen:
            rate_anp = get_anp_tier_label(anp_comm_by_agent.get(ag, 0.0))
            cust_keys = sorted([(ag, cu) for (a, cu) in anp_by_cust.keys() if a == ag])
            show_ag = ag
            first_row = True
            final_anp_comm = anp_comm_by_agent.get(ag, 0.0)
            final_anp_comm_str = f"{final_anp_comm:,.2f}" if final_anp_comm != 0 else "-"
            for key in cust_keys:
                cu = key[1]
                comm = anp_by_cust[key]
                inv_list = anp_cust_inv[key]
                if not inv_list:
                    continue
                cust_dates = get_dates_for_invoices(inv_list, invoice_dates_map)
                
                row_comm_str = final_anp_comm_str if first_row else "-"
                first_row = False

                cust_anp_pkgs = set(get_invoice_package(inv, r) for r in month_anp_detail if str(r.get("agent_name", "")).strip() == ag and (r.get("customer_name") or "").strip() == cu for inv in [r.get("invoice_number")] if inv)
                cust_anp_pkg_str = "<br/>".join(sorted(cust_anp_pkgs)) if cust_anp_pkgs else "-"

                customer_anp_rows.append([
                    show_ag, cu, cust_dates[0], cust_dates[1],
                    cust_anp_pkg_str,
                    ensure_rm_prefix(f"{anp_sales_by_cust[key]:,.2f}" if anp_sales_by_cust[key] != 0 else "-"),
                    ensure_rm_prefix(row_comm_str),
                    ensure_rm_prefix(f"{anp_clawback_by_cust[key]:,.2f}" if anp_clawback_by_cust[key] != 0 else "-")
                ])
                show_ag = ""

        if customer_rows:
            customer_summary_by_month[m] = customer_rows
        if customer_anp_rows:
            customer_anp_by_month[m] = customer_anp_rows

    return agent_summary_by_month, customer_summary_by_month, agent_anp_by_month, customer_anp_by_month, agent_totals_by_month


# ---------------------------------------------------------------------------
# Build the two summary tables for outsource
# ---------------------------------------------------------------------------

def build_outsource_summary_tables(
    basic_t1: list,
    basic_lines: list,
    basic_meta: dict,
    nfp_agent_rows: list,
    nfp_rows: list,
    anp_summary_rows: list,
    anp_detail: list,
    year: int,
    invoice_dates_map: dict,
    month: int | None = None,
    nfp_by_inv_all: dict | None = None,
) -> tuple[dict[int, list[list[str]]], dict[int, list[list[str]]], dict[int, list[list[str]]], dict[int, dict[str, dict]]]:
    """
    Returns:
      agent_summary_by_month - dict of month -> rows for "Summary Outsource Agent Commission"
      customer_summary_by_month - dict of month -> rows for "Summary Outsource Agent Commission by Customer"
      customer_anp_by_month - dict of month -> rows for "ANP Commission by Customer" (outsource)
      agent_totals_by_month - dict of month -> {agent: {"basic","nfp","anp","sales","invoices"}} (anp always 0 here -- outsource ANP is aggregated separately from anp_detail)
    """
    nfp_by_inv_all = nfp_by_inv_all or {}
    out_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
    basic = _load_module("out_basic_commission", out_path)

    # In-place format names in Title Case for all loaded outsource records
    for ln in basic_lines:
        ln.agent_name = to_title_case(ln.agent_name)
        ln.customer_name = to_title_case(ln.customer_name)
    for r in nfp_rows:
        r.agent_name = to_title_case(r.agent_name)
        r.customer_name = to_title_case(r.customer_name)

    agent_summary_by_month = {}
    customer_summary_by_month = {}
    customer_anp_by_month = {}
    agent_totals_by_month = {}
    nfp_by_inv = {r.invoice_number.strip(): r for r in nfp_rows if r.invoice_number}

    for m in range(1, 13):
        if month is not None and m != month:
            continue
        basic_comm_by_agent = {}
        basic_override_by_agent = {}
        out_override_breakdown = defaultdict(lambda: defaultdict(float))
        # Same contributions as out_override_breakdown, but split by the
        # SOURCE agent's customer too, so the customer-level table can show
        # exactly which of the source agent's own sales generated each amount
        # (instead of lumping the whole month's total onto the recipient's row).
        out_override_by_customer = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        gan_lai_soon_by_agent = {}

        month_basic_lines = _expand_basic_lines_for_month(basic_lines, m)
        for ln in month_basic_lines:
            agent = ln.agent_name.strip()
            customer = ln.customer_name.strip()
            basic_comm_by_agent[agent] = basic_comm_by_agent.get(agent, 0.0) + ln.basic_commission

            # RM300-only tranche lines (invoice reached >=5% but not yet 75%)
            # don't attribute overrides/referral -- those are recognized once,
            # in the month the balance (75%) is reached.
            if getattr(ln, "_skip_override", False):
                continue

            info = basic.get_agent_hierarchy_info(agent)
            if not info:
                info = {"canonical_name": agent, "tier": "OUM", "osa_parent": None, "oum_parent": None}
            canonical_name = to_title_case(info["canonical_name"])

            # Calculate Gan Lai Soon OGM override commission
            gls_comm = getattr(ln, "gan_lai_soon", 0.0)
            if gls_comm > 0:
                basic_override_by_agent["Gan Lai Soon"] = basic_override_by_agent.get("Gan Lai Soon", 0.0) + gls_comm
                gan_lai_soon_by_agent[agent] = gan_lai_soon_by_agent.get(agent, 0.0) + gls_comm
                out_override_breakdown["Gan Lai Soon"][agent] += gls_comm

            if getattr(ln, "is_factory", False) or getattr(ln, "package", "") == "Factory":
                # The OSA/OUM/OGM split (and Safwan's own cut) is computed
                # once, in fetch_outsource_basic(), and attached to the line
                # there -- read it back instead of re-deriving it here, so
                # this table can never disagree with what was actually paid.
                # (Gan Lai Soon's OGM cut is already folded into ln.gan_lai_soon
                # above, alongside the same-invoice 0.75% override.)
                safwan_override = getattr(ln, "safwan_override", 0.0)
                if safwan_override:
                    basic_override_by_agent["Safwan"] = basic_override_by_agent.get("Safwan", 0.0) + safwan_override
                    out_override_breakdown["Safwan"][agent] += safwan_override
                    out_override_by_customer[agent][customer]["Safwan"] += safwan_override

                oum_cut = getattr(ln, "senior_override", 0.0)
                oum_name = getattr(ln, "senior_override_name", None)
                if oum_name and oum_cut:
                    basic_override_by_agent[oum_name] = basic_override_by_agent.get(oum_name, 0.0) + oum_cut
                    out_override_breakdown[oum_name][agent] += oum_cut
                    out_override_by_customer[agent][customer][oum_name] += oum_cut
            else:
                oum_parent = info.get("oum_parent")
                if oum_parent:
                    oum_parent = to_title_case(oum_parent)
                    sales_price = float(ln.sales_price)
                    oum_override = sales_price * 0.005
                    out_override_breakdown[oum_parent][agent] += oum_override
                    basic_override_by_agent[oum_parent] = basic_override_by_agent.get(oum_parent, 0.0) + oum_override
                    out_override_by_customer[agent][customer][oum_parent] += oum_override

        # NFP commission
        nfp_comm_by_agent = {}
        month_nfp_rows = [r for r in nfp_rows if _parse_month(_nfp_payout_date(r)) == m]
        for r in month_nfp_rows:
            agent = r.agent_name.strip()
            nfp_comm_by_agent[agent] = nfp_comm_by_agent.get(agent, 0.0) + float(r.nfp_commission)

        month_agents = sorted(
            set(
                list(basic_comm_by_agent.keys()) +
                list(nfp_comm_by_agent.keys()) +
                list(basic_override_by_agent.keys())
            ),
            key=outsource_agent_sort_key
        )

        # Referral fees on the outsource side exist only where someone typed
        # one on the dashboard, so they are worked out straight from the
        # overrides against each customer's basic sales. Computed here, before
        # the agent rows are built, so the agent total and the customer rows
        # below quote the same figure.
        out_referral_by_agent: dict[str, float] = {}
        out_referral_names_by_agent: dict[str, list] = {}
        _out_overrides = referral_overrides(year)
        if _out_overrides:
            for ln in month_basic_lines:
                if getattr(ln, "_skip_override", False):
                    continue
                hit = _out_overrides.get((ln.agent_name.strip().lower(),
                                          ln.customer_name.strip().lower()))
                if not hit or not hit["name"]:
                    continue
                rate = float(hit["rate"]) if hit["rate"] is not None else 0.02
                agent_key = ln.agent_name.strip()
                out_referral_by_agent[agent_key] = (
                    out_referral_by_agent.get(agent_key, 0.0)
                    + float(ln.sales_price) * rate)
                names = out_referral_names_by_agent.setdefault(agent_key, [])
                if hit["name"] not in names:
                    names.append(hit["name"])

        agent_rows = []
        for agent in month_agents:
            basic_comm = basic_comm_by_agent.get(agent, 0.0)
            basic_override = basic_override_by_agent.get(agent, 0.0)
            basic_total = basic_comm
            nfp_comm = nfp_comm_by_agent.get(agent, 0.0)
            gan_lai_soon = gan_lai_soon_by_agent.get(agent, 0.0)
            basic_sales = sum(float(ln.sales_price) for ln in month_basic_lines if ln.agent_name.strip() == agent)
            nfp_sales = sum(float(r.sales_price) for r in month_nfp_rows if r.agent_name.strip() == agent)

            basic_system = sum(float(nfp_by_inv[ln.invoice_number.strip()].system_price) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.invoice_number.strip() in nfp_by_inv)
            basic_nfp = sum(float(nfp_by_inv[ln.invoice_number.strip()].net_floor_price or 0.0) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.invoice_number.strip() in nfp_by_inv)

            nfp_system = sum(float(r.system_price) for r in month_nfp_rows if r.agent_name.strip() == agent)
            nfp_nfp = sum(float(r.net_floor_price or 0.0) for r in month_nfp_rows if r.agent_name.strip() == agent)

            basic_invs = [ln.invoice_number for ln in month_basic_lines if ln.agent_name.strip() == agent]
            nfp_invs = [r.invoice_number for r in month_nfp_rows if r.agent_name.strip() == agent]
            total_invoices_count = len(set(basic_invs + nfp_invs))

            agent_totals_by_month.setdefault(m, {})[agent] = {
                "basic": basic_total, "nfp": nfp_comm, "anp": 0.0,
                "sales": basic_sales + nfp_sales, "invoices": total_invoices_count,
            }

            basic_dates = get_dates_for_invoices(basic_invs, invoice_dates_map)
            nfp_dates = get_dates_for_invoices(nfp_invs, invoice_dates_map)

            basic_pkgs_set = set(get_invoice_package(ln.invoice_number, ln) for ln in month_basic_lines if ln.agent_name.strip() == agent)
            basic_packages = "<br/>".join(sorted(basic_pkgs_set)) if basic_pkgs_set else "-"
            
            nfp_pkgs_set = set(get_invoice_package(r.invoice_number, r) for r in month_nfp_rows if r.agent_name.strip() == agent)
            nfp_packages = "<br/>".join(sorted(nfp_pkgs_set)) if nfp_pkgs_set else "-"

            if agent.strip().lower() == "gan lai soon":
                _gls_total = sum(out_override_breakdown[agent].values())
                basic_override_val = f"RM {_gls_total:,.2f} override from all outsource agent" if _gls_total > 0 else "-"
            else:
                basic_override_val = format_senior_override_cell(out_override_breakdown[agent])

            # Skip pure placeholder rows: no invoices this month AND nothing
            # payable. Override-only rows (e.g. Gan Lai Soon OGM) are kept.
            if total_invoices_count == 0 and not (
                basic_total or nfp_comm or gan_lai_soon
                or basic_override_val != "-"
            ):
                continue

            rows = build_agent_summary_rows(
                agent=agent,
                basic_dates=basic_dates,
                basic_comm_rm=basic_total,
                basic_referral_rm=out_referral_by_agent.get(agent, 0.0),
                basic_safwan_rm=basic_override,
                nfp_dates=nfp_dates,
                nfp_comm_rm=nfp_comm,
                nfp_referral_rm=0.0,
                nfp_safwan_rm=0.0,
                anp_dates=("-", "-", "-"),
                anp_comm_rm=0.0,
                anp_referral_rm=0.0,
                anp_safwan_rm=0.0,
                include_anp=False,
                is_outsource=True,
                gan_lai_soon_basic=gan_lai_soon,
                gan_lai_soon_nfp=0.0,
                gan_lai_soon_anp=0.0,
                basic_override_rm=basic_override_val,
                basic_sales_rm=basic_sales,
                nfp_sales_rm=nfp_sales,
                basic_packages=basic_packages,
                nfp_packages=nfp_packages,
                anp_packages="-",
                basic_system_rm=basic_system,
                basic_nfp_rm=basic_nfp,
                nfp_system_rm=nfp_system,
                nfp_nfp_rm=nfp_nfp,
                total_invoices=total_invoices_count,
            )
            agent_rows.extend(rows)
        if agent_rows:
            agent_summary_by_month[m] = agent_rows

        # Customer summary
        basic_by_cust = defaultdict(lambda: {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "is_cleaning_service": False, "advance": 0.0, "advance_date": ""})
        for ln in month_basic_lines:
            key = (ln.agent_name.strip(), ln.customer_name.strip())
            basic_by_cust[key]["comm"] += ln.basic_commission
            basic_by_cust[key]["advance"] += float(getattr(ln, "_advance_deducted", 0) or 0)
            # The date the advance was actually earned (the >=4.99% milestone).
            # NOT the invoice's first payment: a first payment can fall short of
            # the trigger, and dating the advance by it credits that payment
            # with earning something it did not earn.
            _adv_dt = str(getattr(ln, "pct5_date", "") or "").strip()
            if _adv_dt and (not basic_by_cust[key]["advance_date"]
                            or _adv_dt < basic_by_cust[key]["advance_date"]):
                basic_by_cust[key]["advance_date"] = _adv_dt
            basic_by_cust[key]["sales"] += float(ln.sales_price)
            inv_num = ln.invoice_number.strip()
            r_nfp = nfp_by_inv.get(inv_num) or nfp_by_inv_all.get(inv_num)
            if r_nfp:
                basic_by_cust[key]["system_price"] += float(r_nfp.system_price)
                basic_by_cust[key]["net_floor_price"] += float(r_nfp.net_floor_price or 0.0)
                if _is_cleaning_inspection_service(r_nfp):
                    basic_by_cust[key]["is_cleaning_service"] = True
            rate_str = f"{ln.rate * 100:.2g}%"
            if hasattr(ln, "profit_sharing") and ln.profit_sharing > 0:
                rate_str += f" + {ln.profit_sharing * 100:.2g}%"
            basic_by_cust[key]["rate"].add(rate_str)

        nfp_by_cust = defaultdict(lambda: {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0, "is_cleaning_service": False})
        for r in month_nfp_rows:
            key = (r.agent_name.strip(), r.customer_name.strip())
            nfp_by_cust[key]["comm"] += float(r.nfp_commission)
            nfp_by_cust[key]["sales"] += float(r.sales_price)
            nfp_by_cust[key]["rate"].add(determine_nfp_rate([r]))
            nfp_by_cust[key]["system_price"] += float(r.system_price)
            nfp_by_cust[key]["net_floor_price"] += float(r.net_floor_price or 0.0)
            if _is_cleaning_inspection_service(r):
                nfp_by_cust[key]["is_cleaning_service"] = True

        customer_rows = []
        for agent in month_agents:
            agent_customers = sorted(list(set(c for a, c in list(basic_by_cust.keys()) + list(nfp_by_cust.keys()) if a == agent)))

            # For outsource safwan/gan lai soon
            safwan_rm = basic_override_by_agent.get(agent, 0.0)
            gan_lai_soon = gan_lai_soon_by_agent.get(agent, 0.0)

            show_agent = agent
            if agent_customers:
                for customer in agent_customers:
                    basic_info = basic_by_cust.get((agent, customer), {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0})
                    nfp_info = nfp_by_cust.get((agent, customer), {"comm": 0.0, "rate": set(), "sales": 0.0, "system_price": 0.0, "net_floor_price": 0.0})

                    rate_basic = ", ".join(sorted(basic_info["rate"])) if basic_info["rate"] else "-"
                    rate_nfp = ", ".join(sorted(nfp_info["rate"])) if nfp_info["rate"] else "-"

                    basic_invs = [ln.invoice_number for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer]
                    nfp_invs = [r.invoice_number for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer]

                    # RM300 tranche: paid once a July 2026+ invoice reaches >=5%
                    # payment; pre-July invoices show "invoice before july"
                    # instead (their Basic Commission pays in full at 100%).
                    cust_basic_lines = [ln for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer]
                    rm300_display = _rm300_column_display(cust_basic_lines)

                    basic_dates = get_dates_for_invoices(basic_invs, invoice_dates_map)
                    nfp_dates = get_dates_for_invoices(nfp_invs, invoice_dates_map)

                    row_gan_lai_soon = sum(getattr(ln, "gan_lai_soon", 0.0) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer and not getattr(ln, "_skip_override", False))

                    # Safwan override for this customer in outsource -- read
                    # back what fetch_outsource_basic() already computed,
                    # rather than re-deriving it here.
                    row_safwan_rm = sum(
                        getattr(ln, "safwan_override", 0.0)
                        for ln in month_basic_lines
                        if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer
                        and not getattr(ln, "_skip_override", False)
                    )

                    # Customer specific package type
                    cust_basic_pkgs = set(get_invoice_package(ln.invoice_number, ln) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer)
                    cust_basic_pkg_str = "<br/>".join(sorted(cust_basic_pkgs)) if cust_basic_pkgs else "-"
                    
                    cust_nfp_pkgs = set(get_invoice_package(r.invoice_number, r) for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer)
                    cust_nfp_pkg_str = "<br/>".join(sorted(cust_nfp_pkgs)) if cust_nfp_pkgs else "-"

                    basic_has_source = bool(basic_invs)
                    nfp_has_source = bool(nfp_invs)
                    if basic_has_source:
                        basic_display_dates = basic_dates
                        basic_display_pkg = cust_basic_pkg_str
                        basic_display_system = basic_info["system_price"]
                        basic_display_nfp = basic_info["net_floor_price"]
                        basic_display_sales = basic_info["sales"]
                        basic_display_is_cleaning = basic_info.get("is_cleaning_service", False)
                    elif nfp_has_source:
                        # Some outsource customers only have NFP source rows. Backfill the
                        # shared columns onto the paired Basic row so the dashboard and
                        # exports do not render an empty first row.
                        basic_display_dates = nfp_dates
                        basic_display_pkg = cust_nfp_pkg_str
                        basic_display_system = nfp_info["system_price"]
                        basic_display_nfp = nfp_info["net_floor_price"]
                        basic_display_sales = nfp_info["sales"]
                        basic_display_is_cleaning = nfp_info.get("is_cleaning_service", False)
                    else:
                        basic_display_dates = basic_dates
                        basic_display_pkg = cust_basic_pkg_str
                        basic_display_system = basic_info["system_price"]
                        basic_display_nfp = basic_info["net_floor_price"]
                        basic_display_sales = basic_info["sales"]
                        basic_display_is_cleaning = basic_info.get("is_cleaning_service", False)

                    is_before_oct_25 = is_before_october_2025(basic_dates[0]) or is_before_october_2025(nfp_dates[0])
                    cust_all_pkg_text = " ".join(filter(None, [
                        cust_basic_pkg_str,
                        cust_nfp_pkg_str,
                        *[getattr(ln, 'package_description', '') for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(ln, 'all_item_text', '') for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(r, 'package_description', '') for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                        *[getattr(r, 'all_item_text', '') for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                    ]))
                    cust_panel_ratings = [
                        *[getattr(ln, 'panel_rating', None) for ln in month_basic_lines if ln.agent_name.strip() == agent and ln.customer_name.strip() == customer],
                        *[getattr(r, 'panel_rating', None) for r in month_nfp_rows if r.agent_name.strip() == agent and r.customer_name.strip() == customer],
                    ]
                    has_power_out = (
                        nfp_info['net_floor_price'] != 0 or nfp_info['comm'] != 0 or nfp_info['sales'] != 0 or
                        basic_display_nfp != 0 or
                        _has_jinko_power_output(cust_all_pkg_text, cust_panel_ratings)
                    )

                    if is_before_oct_25:
                        basic_nfp_cell = "invoice before Oct 25"
                        nfp_nfp_cell = "invoice before Oct 25"
                        nfp_comm_val = "invoice before Oct 25"
                    elif not has_power_out:
                        basic_nfp_cell = "JinkoSolar package not included"
                        nfp_nfp_cell = "JinkoSolar package not included"
                        nfp_comm_val = "JinkoSolar package not included"
                    else:
                        basic_nfp_cell = (
                            "cleaning and inspection service" if basic_display_nfp == 0 and basic_display_is_cleaning
                            else ensure_rm_prefix(f"{basic_display_nfp:,.2f}" if basic_display_nfp != 0 else "-")
                        )
                        nfp_nfp_cell = (
                            "cleaning and inspection service" if nfp_info['net_floor_price'] == 0 and nfp_info.get('is_cleaning_service')
                            else ensure_rm_prefix(f"{nfp_info['net_floor_price']:,.2f}" if nfp_info['net_floor_price'] != 0 else "-")
                        )
                        nfp_comm_val = ensure_rm_prefix(
                            f"{nfp_info['comm']:,.2f}" if nfp_info['comm'] != 0
                            else ("pending full payment" if nfp_dates[0] and "2026" in str(nfp_dates[0]) else "-")
                        )

                    basic_comm_val = f"{basic_info['comm']:,.2f}" if basic_info['comm'] != 0 else ("pending full payment" if basic_dates[0] and "2026" in str(basic_dates[0]) else "-")
                    row_other_commission = format_other_commission_for_customer(out_override_by_customer[agent].get(customer, {}))
                    # Outsource invoices carry no referrer in the ERP, so the
                    # only referral an outsource row can have is one typed on
                    # the dashboard. Charged on the same basis as internal:
                    # the rate against this customer's basic sales price.
                    ref_name_cell, ref_fee_cell = "-", "-"
                    ref_hit = referral_overrides(year).get(
                        (agent.strip().lower(), customer.strip().lower()))
                    if ref_hit and ref_hit["name"]:
                        ref_rate = float(ref_hit["rate"]) if ref_hit["rate"] is not None else 0.02
                        ref_fee = float(basic_display_sales) * ref_rate
                        ref_name_cell = to_title_case(ref_hit["name"])
                        ref_fee_cell = ensure_rm_prefix(f"{ref_fee:,.2f}") if ref_fee else "-"
                        out_referral_by_agent[agent] = out_referral_by_agent.get(agent, 0.0) + ref_fee
                    basic_row = [show_agent, customer, basic_display_dates[0], basic_display_dates[1]]
                    if m >= 7:
                        basic_row.append(rm300_display)
                    basic_row.extend([
                        _balance_payment_display(cust_basic_lines, basic_display_dates),
                        basic_display_pkg,
                        ensure_rm_prefix(f"{basic_display_system:,.2f}" if basic_display_system != 0 else "-"),
                        basic_nfp_cell,
                        ensure_rm_prefix(f"{basic_display_sales:,.2f}" if basic_display_sales != 0 else "-"),
                        "Basic Commission", ensure_rm_prefix(basic_comm_val),
                        row_other_commission,
                        ensure_rm_prefix(f"{row_safwan_rm:,.2f}" if row_safwan_rm != 0 else "-"), ensure_rm_prefix(f"{row_gan_lai_soon:,.2f}" if row_gan_lai_soon != 0 else "-"),
                        ref_name_cell, ref_fee_cell,
                        rate_basic,
                        f"{basic_info.get('advance', 0.0):,.2f}" if basic_info.get("advance") else "-",
                        basic_info.get("advance_date") or "-",
                    ])
                    customer_rows.append(basic_row)
                    show_agent = ""

                    nfp_row = [show_agent, customer, nfp_dates[0], nfp_dates[1]]
                    if m >= 7:
                        nfp_row.append("-")
                    nfp_row.extend([
                        nfp_dates[2],
                        cust_nfp_pkg_str,
                        ensure_rm_prefix(f"{nfp_info['system_price']:,.2f}" if nfp_info['system_price'] != 0 else "-"),
                        nfp_nfp_cell,
                        ensure_rm_prefix(f"{nfp_info['sales']:,.2f}" if nfp_info['sales'] != 0 else "-"),
                        "Net Floor Price Commission", nfp_comm_val,
                        "-", "-", "-", "-", "-", "-", "-", "-"
                    ])
                    customer_rows.append(nfp_row)

        if customer_rows:
            customer_summary_by_month[m] = customer_rows

        # Build ANP customer rows: one row per customer per agent (from anp_detail)
        month_anp_detail = [r for r in anp_detail if _parse_month(r.get("invoice_date")) == m]
        anp_comm_by_agent = {}
        anp_agent_monthly = defaultdict(float)
        for r in month_anp_detail:
            agent = str(r.get("agent_name", "")).strip()
            comm = float(r.get("anp_commission_accumulated_tier", 0.0))
            if comm > anp_agent_monthly[agent]:
                anp_agent_monthly[agent] = comm
        for agent, comm in anp_agent_monthly.items():
            anp_comm_by_agent[agent] = comm

        anp_by_cust = defaultdict(float)  # (agent, customer) -> max accumulated commission
        anp_cust_inv = defaultdict(list)   # (agent, customer) -> invoice numbers
        anp_sales_by_cust = defaultdict(float) # (agent, customer) -> sum of sales price
        anp_clawback_by_cust = defaultdict(float) # (agent, customer) -> sum of clawbacks
        for r in month_anp_detail:
            ag = str(r.get("agent_name", "")).strip()
            cu = str(r.get("customer_name", "") or "").strip()
            if not cu:
                cu = "(unknown)"
            key = (ag, cu)
            comm = float(r.get("anp_commission_accumulated_tier", 0.0))
            if comm > anp_by_cust[key]:
                anp_by_cust[key] = comm
            inv = r.get("invoice_number")
            if inv and inv not in anp_cust_inv[key]:
                anp_cust_inv[key].append(inv)
            anp_sales_by_cust[key] += float(r.get("invoice_total_amount", 0.0))
            anp_clawback_by_cust[key] += float(r.get("clawback", 0.0))

        customer_anp_rows = []
        anp_agents_seen = []
        for (ag, cu) in sorted(anp_by_cust.keys(), key=lambda k: (outsource_agent_sort_key(k[0]), k[1])):
            if ag not in anp_agents_seen:
                anp_agents_seen.append(ag)
        for ag in anp_agents_seen:
            cust_keys = sorted([(ag, cu) for (a, cu) in anp_by_cust.keys() if a == ag])
            show_ag = ag
            first_row = True
            final_anp_comm = anp_comm_by_agent.get(ag, 0.0)
            final_anp_comm_str = f"{final_anp_comm:,.2f}" if final_anp_comm != 0 else "-"
            for key in cust_keys:
                cu = key[1]
                inv_list = anp_cust_inv[key]
                if not inv_list:
                    continue
                cust_dates = get_dates_for_invoices(inv_list, invoice_dates_map)

                row_comm_str = final_anp_comm_str if first_row else "-"
                first_row = False

                cust_anp_pkgs = set(get_invoice_package(inv, r) for r in month_anp_detail if str(r.get("agent_name", "")).strip() == ag and (r.get("customer_name") or "").strip() == cu for inv in [r.get("invoice_number")] if inv)
                cust_anp_pkg_str = "<br/>".join(sorted(cust_anp_pkgs)) if cust_anp_pkgs else "-"

                customer_anp_rows.append([
                    show_ag, cu, cust_dates[0], cust_dates[1],
                    cust_anp_pkg_str,
                    ensure_rm_prefix(f"{anp_sales_by_cust[key]:,.2f}" if anp_sales_by_cust[key] != 0 else "-"),
                    ensure_rm_prefix(row_comm_str),
                    ensure_rm_prefix(f"{anp_clawback_by_cust[key]:,.2f}" if anp_clawback_by_cust[key] != 0 else "-")
                ])
                show_ag = ""

        if customer_anp_rows:
            customer_anp_by_month[m] = customer_anp_rows

    return agent_summary_by_month, customer_summary_by_month, customer_anp_by_month, agent_totals_by_month

# ---------------------------------------------------------------------------

FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont('Verdana', 'verdana.ttf'))
    pdfmetrics.registerFont(TTFont('Verdana-Bold', 'verdanab.ttf'))
    FONT_REGULAR = "Verdana"
    FONT_BOLD = "Verdana-Bold"
except Exception:
    pass


def _rl_color(hex_str: str):
    from reportlab.lib import colors
    return colors.HexColor(hex_str)


def _build_summary_table_rl(title: str, headers: list[str], rows: list[list[str]], page_width: float, is_outsource: bool = False):
    """Builds a ReportLab Table for the stacked summary commission table."""
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle("SumHdr", parent=styles["Normal"], fontSize=8, leading=9,
                                   fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    cell_left = ParagraphStyle("SumLeft", parent=styles["Normal"], fontSize=7, leading=8,
                                fontName=FONT_REGULAR, alignment=TA_LEFT)
    cell_right = ParagraphStyle("SumRight", parent=styles["Normal"], fontSize=7, leading=8,
                                 fontName=FONT_REGULAR, alignment=TA_RIGHT)
    cell_center = ParagraphStyle("SumCenter", parent=styles["Normal"], fontSize=7, leading=8,
                                  fontName=FONT_REGULAR, alignment=TA_CENTER)
    cell_bold_left = ParagraphStyle("SumBoldLeft", parent=styles["Normal"], fontSize=7, leading=8,
                                     fontName=FONT_BOLD, alignment=TA_LEFT)

    ratio_map = {
        "agent": 1.8,
        "total invoice": 1.2,
        "invoice date": 1.4,
        "1st payment date": 1.4,
        "full payment date": 1.4,
        "package type": 1.4,
        "system price": 1.6,
        "netfloor price": 1.6,
        "net floor price": 1.6,
        "sales price": 1.6,
        "commission": 2.4,
        "commission price": 1.6,
        "senior override": 2.4,
        "oum override": 2.4,
        "referral fee": 1.6,
        "safwan (rm)": 1.6,
    }

    col_ratios = []
    for h in headers:
        hl = h.lower().strip()
        col_ratios.append(ratio_map.get(hl, 1.0))

    total_ratio = sum(col_ratios)
    col_widths = [page_width * r / total_ratio for r in col_ratios]

    header_row = [Paragraph(h, header_style) for h in headers]
    table_data = [header_row]

    for row in rows:
        formatted = []
        for i, val in enumerate(row):
            s = str(val)
            hdr = headers[i].lower()
            if "agent" in hdr:
                formatted.append(Paragraph(s, cell_bold_left if s else cell_left))
            elif "date" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "package" in hdr:
                formatted.append(Paragraph(s, cell_left))
            elif "sales price" in hdr or "system price" in hdr or "netfloor price" in hdr or "net floor price" in hdr or "commission price" in hdr or "referral fee" in hdr or "safwan" in hdr or "gan lai soon" in hdr or "override" in hdr:
                formatted.append(Paragraph(s, cell_right))
            elif "referral name" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "commission" in hdr:  # type name
                formatted.append(Paragraph(s, cell_left))
            elif "total invoice" in hdr:
                formatted.append(Paragraph(s, cell_center))
            else:
                formatted.append(Paragraph(s, cell_left))
        table_data.append(formatted)

    t = Table(table_data, colWidths=col_widths, repeatRows=1, splitByRow=1)

    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _rl_color("#1A365D")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, _rl_color("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    current_agent = None
    agent_start_row = 1
    agent_row_count = 0
    agent_spans = []

    for r_idx, row in enumerate(rows):
        table_row_idx = r_idx + 1  # offset for header
        agent_val = str(row[0]).strip() if (row and row[0]) else ""

        if agent_val and agent_val != current_agent:
            # Finalize previous agent span
            if current_agent and agent_row_count > 1:
                agent_spans.append((current_agent, agent_start_row, agent_row_count))
                t_styles.append(("SPAN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1)))
                t_styles.append(("VALIGN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1), "TOP"))
                
                # Dynamically locate columns to span vertically
                lower_hdrs = [h.lower().strip() for h in headers]
                try:
                    ti_idx = lower_hdrs.index("total invoice")
                    t_styles.append(("SPAN", (ti_idx, agent_start_row), (ti_idx, agent_start_row + agent_row_count - 1)))
                    t_styles.append(("VALIGN", (ti_idx, agent_start_row), (ti_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                except ValueError:
                    pass
                try:
                    p_idx = lower_hdrs.index("package type")
                    t_styles.append(("SPAN", (p_idx, agent_start_row), (p_idx, agent_start_row + agent_row_count - 1)))
                    t_styles.append(("VALIGN", (p_idx, agent_start_row), (p_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                except ValueError:
                    pass
                try:
                    s_idx = lower_hdrs.index("sales price")
                    t_styles.append(("SPAN", (s_idx, agent_start_row), (s_idx, agent_start_row + agent_row_count - 1)))
                    t_styles.append(("VALIGN", (s_idx, agent_start_row), (s_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                except ValueError:
                    pass
                try:
                    o_idx = lower_hdrs.index("senior override")
                    t_styles.append(("SPAN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1)))
                    t_styles.append(("VALIGN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                except ValueError:
                    pass
                try:
                    o_idx = lower_hdrs.index("oum override")
                    t_styles.append(("SPAN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1)))
                    t_styles.append(("VALIGN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                except ValueError:
                    pass

            current_agent = agent_val
            agent_start_row = table_row_idx
            agent_row_count = 1
        else:
            agent_row_count += 1

        # Background color based on agent tier
        row_agent = agent_val if agent_val else (current_agent or "")
        row_agent_lower = row_agent.lower()
        if "total" in row_agent_lower or "summary" in row_agent_lower or not row_agent:
            t_styles.append(("BACKGROUND", (0, table_row_idx), (-1, table_row_idx), _rl_color("#EDF2F7")))
        else:
            if is_outsource:
                tier = get_outsource_agent_tier(row_agent)
                if tier == "OGM":
                    row_bg = "#CBD5E0"
                elif tier == "OUM":
                    row_bg = "#E2E8F0"
                else:
                    row_bg = "#F7FAFC"
            else:
                tier = get_internal_agent_tier(row_agent)
                if tier == "Senior":
                    row_bg = "#C3DAF2"
                else:
                    row_bg = "#E9F2FA"
            t_styles.append(("BACKGROUND", (0, table_row_idx), (-1, table_row_idx), _rl_color(row_bg)))

    # Finalize last agent span
    if current_agent and agent_row_count > 1:
        agent_spans.append((current_agent, agent_start_row, agent_row_count))
        t_styles.append(("SPAN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1)))
        t_styles.append(("VALIGN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1), "TOP"))
        lower_hdrs = [h.lower().strip() for h in headers]
        try:
            ti_idx = lower_hdrs.index("total invoice")
            t_styles.append(("SPAN", (ti_idx, agent_start_row), (ti_idx, agent_start_row + agent_row_count - 1)))
            t_styles.append(("VALIGN", (ti_idx, agent_start_row), (ti_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
        except ValueError:
            pass
        try:
            p_idx = lower_hdrs.index("package type")
            t_styles.append(("SPAN", (p_idx, agent_start_row), (p_idx, agent_start_row + agent_row_count - 1)))
            t_styles.append(("VALIGN", (p_idx, agent_start_row), (p_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
        except ValueError:
            pass
        try:
            s_idx = lower_hdrs.index("sales price")
            t_styles.append(("SPAN", (s_idx, agent_start_row), (s_idx, agent_start_row + agent_row_count - 1)))
            t_styles.append(("VALIGN", (s_idx, agent_start_row), (s_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
        except ValueError:
            pass
        try:
            o_idx = lower_hdrs.index("senior override")
            t_styles.append(("SPAN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1)))
            t_styles.append(("VALIGN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
        except ValueError:
            pass
        try:
            o_idx = lower_hdrs.index("oum override")
            t_styles.append(("SPAN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1)))
            t_styles.append(("VALIGN", (o_idx, agent_start_row), (o_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
        except ValueError:
            pass

    # Horizontal/Vertical span for System Price and Net Floor Price columns
    # (lower_hdrs is normally set by the agent-span finalizers above, but
    # those never run when every agent has exactly one row -- e.g. a month
    # with just a single qualifying agent -- so it's re-derived here too.)
    lower_hdrs = [h.lower().strip() for h in headers]
    try:
        sys_idx = lower_hdrs.index("system price")
        try:
            nfp_idx = lower_hdrs.index("net floor price")
        except ValueError:
            nfp_idx = lower_hdrs.index("netfloor price")
            
        spanned_cells = set()
        
        # 1. Process agent groups for vertical merging
        for agent_name, start_row, row_count in agent_spans:
            sys_vals = [str(rows[r][sys_idx]).strip() for r in range(start_row - 1, start_row + row_count - 1)]
            sys_identical = len(set(sys_vals)) == 1 and sys_vals[0]
            
            nfp_vals = [str(rows[r][nfp_idx]).strip() for r in range(start_row - 1, start_row + row_count - 1)]
            nfp_identical = len(set(nfp_vals)) == 1 and nfp_vals[0]
            
            if sys_identical and nfp_identical and sys_vals[0] == nfp_vals[0]:
                t_styles.append(("SPAN", (sys_idx, start_row), (nfp_idx, start_row + row_count - 1)))
                t_styles.append(("VALIGN", (sys_idx, start_row), (nfp_idx, start_row + row_count - 1), "MIDDLE"))
                for r in range(start_row, start_row + row_count):
                    spanned_cells.add((sys_idx, r))
                    spanned_cells.add((nfp_idx, r))
            else:
                if sys_identical:
                    t_styles.append(("SPAN", (sys_idx, start_row), (sys_idx, start_row + row_count - 1)))
                    t_styles.append(("VALIGN", (sys_idx, start_row), (sys_idx, start_row + row_count - 1), "MIDDLE"))
                    for r in range(start_row, start_row + row_count):
                        spanned_cells.add((sys_idx, r))
                if nfp_identical:
                    t_styles.append(("SPAN", (nfp_idx, start_row), (nfp_idx, start_row + row_count - 1)))
                    t_styles.append(("VALIGN", (nfp_idx, start_row), (nfp_idx, start_row + row_count - 1), "MIDDLE"))
                    for r in range(start_row, start_row + row_count):
                        spanned_cells.add((nfp_idx, r))
                        
        # 2. Process remaining rows for horizontal merging if not already spanned
        for r_idx, row in enumerate(rows):
            table_row_idx = r_idx + 1
            if (sys_idx, table_row_idx) not in spanned_cells and (nfp_idx, table_row_idx) not in spanned_cells:
                val1 = str(row[sys_idx]).strip()
                val2 = str(row[nfp_idx]).strip()
                if val1 == val2 and val1 and val1 != "-":
                    t_styles.append(("SPAN", (sys_idx, table_row_idx), (nfp_idx, table_row_idx)))
                    t_styles.append(("VALIGN", (sys_idx, table_row_idx), (nfp_idx, table_row_idx), "MIDDLE"))
    except ValueError:
        pass

    t.setStyle(TableStyle(t_styles))
    return t


def _build_customer_summary_table_rl(title: str, headers: list[str], rows: list[list[str]], page_width: float, is_outsource: bool = False) -> list:
    """Chunks the customer rows into several page-sized Tables (returned as a
    list of flowables) instead of one giant Table.

    A single Table only splits across pages where no vertical SPAN crosses the
    split row. An agent with enough customers produces an agent-name SPAN
    taller than a whole page, leaving ReportLab no legal split point at all --
    doc.build() then dies with "Flowable too large on page ...". Chunking at
    span-safe boundaries (whole Basic/NFP row-pairs, agent name re-stamped on
    each continuation chunk) keeps every table under a page tall so the
    question never arises.
    """
    MAX_ROWS_PER_TABLE = 14  # conservative: fits an empty landscape-A4 frame even with multi-line cells
    lower_hdrs = [h.lower().strip() for h in headers]
    # Basic/NFP tables pair every 2 rows (SPANs stitch each pair); ANP tables don't.
    step = 2 if ("customer" in lower_hdrs and "package type" in lower_hdrs and "commission" in lower_hdrs) else 1

    chunks: list[list[list[str]]] = []
    current: list[list[str]] = []
    current_agent = ""
    i = 0
    while i < len(rows):
        unit = rows[i:i + step]
        first_agent = str(unit[0][0]).strip() if unit and unit[0] else ""
        if first_agent:
            current_agent = first_agent
        if current and len(current) + len(unit) > MAX_ROWS_PER_TABLE:
            chunks.append(current)
            current = []
            if not first_agent and current_agent:
                # Continuation chunk starts mid-agent: re-stamp the agent name
                # on a COPY (pass 1 and pass 2 render from the same row lists).
                unit = [list(unit[0])] + list(unit[1:])
                unit[0][0] = current_agent
        current.extend(unit)
        i += step
    if current:
        chunks.append(current)

    return [
        _build_customer_summary_table_chunk(title, headers, chunk, page_width, is_outsource)
        for chunk in chunks
    ]


def _build_customer_summary_table_chunk(title: str, headers: list[str], rows: list[list[str]], page_width: float, is_outsource: bool = False):
    """Builds a ReportLab Table for the stacked customer summary commission table."""
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle("CustHdr", parent=styles["Normal"], fontSize=6.5, leading=7.5,
                                   fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    cell_left = ParagraphStyle("CustLeft", parent=styles["Normal"], fontSize=6, leading=7,
                                fontName=FONT_REGULAR, alignment=TA_LEFT)
    cell_right = ParagraphStyle("CustRight", parent=styles["Normal"], fontSize=6, leading=7,
                                 fontName=FONT_REGULAR, alignment=TA_RIGHT)
    cell_center = ParagraphStyle("CustCenter", parent=styles["Normal"], fontSize=6, leading=7,
                                  fontName=FONT_REGULAR, alignment=TA_CENTER)
    cell_bold = ParagraphStyle("CustBold", parent=styles["Normal"], fontSize=6, leading=7,
                                fontName=FONT_BOLD, alignment=TA_LEFT)

    ratio_map = {
        "agent": 1.3,
        "customer": 1.6,
        "invoice date": 1.2,
        "1st payment date": 1.2,
        "full payment date": 1.2,
        "75% payment date": 1.2,
        "basic commission (rm300)": 1.3,
        "package type": 1.4,
        "system price": 1.4,
        "netfloor price": 1.4,
        "net floor price": 1.4,
        "sales price": 1.4,
        "total amount": 1.4,
        "payment received": 1.4,
        "paid amount": 1.4,
        "commission": 1.8,
        "commission price": 1.3,
        "senior override": 1.6,
        "referral name": 1.1,
        "referral fee": 1.1,
        "safwan (rm)": 1.1,
        "gan lai soon": 1.1,
        "clawback": 1.1,
    }

    col_ratios = []
    for h in headers:
        hl = h.lower().strip()
        col_ratios.append(ratio_map.get(hl, 1.0))

    total_ratio = sum(col_ratios)
    col_widths = [page_width * r / total_ratio for r in col_ratios]

    header_row = [Paragraph(h, header_style) for h in headers]
    table_data = [header_row]

    for row in rows:
        formatted = []
        for i, val in enumerate(row):
            s = str(val)
            hdr = headers[i].lower()
            if "agent" in hdr:
                formatted.append(Paragraph(s, cell_bold if s else cell_left))
            elif "customer" in hdr:
                formatted.append(Paragraph(s, cell_left))
            elif "date" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "package" in hdr:
                formatted.append(Paragraph(s, cell_left))
            elif "gan lai soon" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "sales price" in hdr or "system price" in hdr or "netfloor price" in hdr or "net floor price" in hdr or "payment received" in hdr or "paid amount" in hdr or "commission price" in hdr or "referral fee" in hdr or "safwan" in hdr or "override" in hdr or "clawback" in hdr:
                formatted.append(Paragraph(s, cell_right))
            elif "referral name" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "rate" in hdr:
                formatted.append(Paragraph(s, cell_center))
            elif "commission" in hdr:  # type name
                formatted.append(Paragraph(s, cell_left))
            else:
                formatted.append(Paragraph(s, cell_left))
        table_data.append(formatted)

    t = Table(table_data, colWidths=col_widths, repeatRows=1, splitByRow=1)

    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _rl_color("#1A365D")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, _rl_color("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]

    current_agent = None
    agent_start_row = 1
    agent_row_count = 0

    for r_idx, row in enumerate(rows):
        table_row_idx = r_idx + 1
        agent_text = str(row[0]).strip() if row else ""
        if agent_text and agent_text != current_agent:
            if current_agent and agent_row_count > 1:
                t_styles.append(("SPAN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1)))
                t_styles.append(("VALIGN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1), "TOP"))
                
                # Check for dynamic columns to span for ANP customer table
                lower_hdrs = [h.lower().strip() for h in headers]
                if "commission" not in lower_hdrs: # ANP Customer table
                    try:
                        c_idx = lower_hdrs.index("commission price")
                        t_styles.append(("SPAN", (c_idx, agent_start_row), (c_idx, agent_start_row + agent_row_count - 1)))
                        t_styles.append(("VALIGN", (c_idx, agent_start_row), (c_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
                    except ValueError:
                        pass
            
            current_agent = agent_text
            agent_start_row = table_row_idx
            agent_row_count = 1
        else:
            agent_row_count += 1

        # Background color based on agent tier
        row_agent = agent_text if agent_text else (current_agent or "")
        row_agent_lower = row_agent.lower()
        if "total" in row_agent_lower or "summary" in row_agent_lower or not row_agent:
            t_styles.append(("BACKGROUND", (0, table_row_idx), (-1, table_row_idx), _rl_color("#EDF2F7")))
        else:
            if is_outsource:
                tier = get_outsource_agent_tier(row_agent)
                if tier == "OGM":
                    row_bg = "#CBD5E0"
                elif tier == "OUM":
                    row_bg = "#E2E8F0"
                else:
                    row_bg = "#F7FAFC"
            else:
                tier = get_internal_agent_tier(row_agent)
                if tier == "Senior":
                    row_bg = "#C3DAF2"
                else:
                    row_bg = "#E9F2FA"
            t_styles.append(("BACKGROUND", (0, table_row_idx), (-1, table_row_idx), _rl_color(row_bg)))

    # Finalize last agent span
    if current_agent and agent_row_count > 1:
        t_styles.append(("SPAN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1)))
        t_styles.append(("VALIGN", (0, agent_start_row), (0, agent_start_row + agent_row_count - 1), "TOP"))
        lower_hdrs = [h.lower().strip() for h in headers]
        if "commission" not in lower_hdrs: # ANP Customer table
            try:
                c_idx = lower_hdrs.index("commission price")
                t_styles.append(("SPAN", (c_idx, agent_start_row), (c_idx, agent_start_row + agent_row_count - 1)))
                t_styles.append(("VALIGN", (c_idx, agent_start_row), (c_idx, agent_start_row + agent_row_count - 1), "MIDDLE"))
            except ValueError:
                pass

    # Vertical span for Customer, Package Type, and Sales Price on each Basic/NFP customer pair
    lower_hdrs = [h.lower().strip() for h in headers]
    if "customer" in lower_hdrs and "package type" in lower_hdrs and "commission" in lower_hdrs:
        cust_idx = lower_hdrs.index("customer")
        pkg_idx = lower_hdrs.index("package type")
        try:
            sales_idx = lower_hdrs.index("sales price")
        except ValueError:
            sales_idx = -1
        
        for r_idx in range(0, len(rows), 2):
            if r_idx + 1 < len(rows):
                row_start = r_idx + 1
                row_end = r_idx + 2
                t_styles.append(("SPAN", (cust_idx, row_start), (cust_idx, row_end)))
                t_styles.append(("VALIGN", (cust_idx, row_start), (cust_idx, row_end), "MIDDLE"))
                t_styles.append(("SPAN", (pkg_idx, row_start), (pkg_idx, row_end)))
                t_styles.append(("VALIGN", (pkg_idx, row_start), (pkg_idx, row_end), "MIDDLE"))
                if sales_idx != -1:
                    t_styles.append(("SPAN", (sales_idx, row_start), (sales_idx, row_end)))
                    t_styles.append(("VALIGN", (sales_idx, row_start), (sales_idx, row_end), "MIDDLE"))

    # Vertical span System Price and Net Floor Price on each Basic/NFP customer pair if identical
    try:
        sys_idx = lower_hdrs.index("system price")
        try:
            nfp_idx = lower_hdrs.index("net floor price")
        except ValueError:
            nfp_idx = lower_hdrs.index("netfloor price")
            
        spanned_cells_cust = set()
        for r_idx in range(0, len(rows), 2):
            if r_idx + 1 < len(rows):
                row_start = r_idx + 1
                row_end = r_idx + 2
                
                # Check System Price
                sys_v1 = str(rows[row_start - 1][sys_idx]).strip()
                sys_v2 = str(rows[row_end - 1][sys_idx]).strip()
                # Check Net Floor Price
                nfp_v1 = str(rows[row_start - 1][nfp_idx]).strip()
                nfp_v2 = str(rows[row_end - 1][nfp_idx]).strip()
                
                if sys_v1 == sys_v2 and nfp_v1 == nfp_v2 and sys_v1 == nfp_v1:
                    # 2D block span
                    t_styles.append(("SPAN", (sys_idx, row_start), (nfp_idx, row_end)))
                    t_styles.append(("VALIGN", (sys_idx, row_start), (nfp_idx, row_end), "MIDDLE"))
                    spanned_cells_cust.add((sys_idx, row_start))
                    spanned_cells_cust.add((sys_idx, row_end))
                    spanned_cells_cust.add((nfp_idx, row_start))
                    spanned_cells_cust.add((nfp_idx, row_end))
                else:
                    if sys_v1 == sys_v2 and sys_v1:
                        t_styles.append(("SPAN", (sys_idx, row_start), (sys_idx, row_end)))
                        t_styles.append(("VALIGN", (sys_idx, row_start), (sys_idx, row_end), "MIDDLE"))
                        spanned_cells_cust.add((sys_idx, row_start))
                        spanned_cells_cust.add((sys_idx, row_end))
                    if nfp_v1 == nfp_v2 and nfp_v1:
                        t_styles.append(("SPAN", (nfp_idx, row_start), (nfp_idx, row_end)))
                        t_styles.append(("VALIGN", (nfp_idx, row_start), (nfp_idx, row_end), "MIDDLE"))
                        spanned_cells_cust.add((nfp_idx, row_start))
                        spanned_cells_cust.add((nfp_idx, row_end))
                        
        # Horizontal span System Price and Net Floor Price if equal and not already vertically merged
        for r_idx, row in enumerate(rows):
            table_row_idx = r_idx + 1
            if (sys_idx, table_row_idx) not in spanned_cells_cust and (nfp_idx, table_row_idx) not in spanned_cells_cust:
                val1 = str(row[sys_idx]).strip()
                val2 = str(row[nfp_idx]).strip()
                if val1 == val2 and val1 and val1 != "-":
                    t_styles.append(("SPAN", (sys_idx, table_row_idx), (nfp_idx, table_row_idx)))
                    t_styles.append(("VALIGN", (sys_idx, table_row_idx), (nfp_idx, table_row_idx), "MIDDLE"))
    except ValueError:
        pass

    t.setStyle(TableStyle(t_styles))
    return t


def _build_ega_table_rl(headers: list[str], rows: list[list[str]], page_width: float):
    """Builds a generic EGA/ESA table."""
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle("EgaHdr", parent=styles["Normal"], fontSize=7.5, leading=8.5,
                                   fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    cell_style = ParagraphStyle("EgaCell", parent=styles["Normal"], fontSize=7, leading=8,
                                 fontName=FONT_REGULAR, alignment=TA_LEFT)
    cell_right = ParagraphStyle("EgaRight", parent=styles["Normal"], fontSize=7, leading=8,
                                 fontName=FONT_REGULAR, alignment=TA_RIGHT)
    cell_bold = ParagraphStyle("EgaBold", parent=styles["Normal"], fontSize=7, leading=8,
                                fontName=FONT_BOLD, alignment=TA_LEFT)

    ncols = len(headers)
    col_widths = [page_width / ncols] * ncols

    header_row = [Paragraph(h, header_style) for h in headers]
    table_data = [header_row]

    for row in rows:
        formatted = []
        is_total = str(row[0]).strip().lower() == "total" if row else False
        for i, val in enumerate(row):
            s = str(val)
            is_monetary = any(x in headers[i].lower() for x in ["rm", "amount", "commission", "total", "sales", "bonus"])
            if is_monetary:
                s = ensure_rm_prefix(s)
            if is_total:
                style = ParagraphStyle("EgaTotalCell", parent=cell_bold, alignment=TA_RIGHT if i > 0 else TA_LEFT)
            elif i == 0:
                style = cell_style
            elif is_monetary:
                style = cell_right
            else:
                style = cell_style
            formatted.append(Paragraph(s, style))
        table_data.append(formatted)

    t = Table(table_data, colWidths=col_widths,
              repeatRows=1, splitByRow=True)
    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _rl_color("#1A365D")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, _rl_color("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            t_styles.append(("BACKGROUND", (0, r), (-1, r), _rl_color("#F8FAFC")))
    if table_data and table_data[-1][0]:
        last = len(table_data) - 1
        t_styles.append(("BACKGROUND", (0, last), (-1, last), _rl_color("#EDF2F7")))
    t.setStyle(TableStyle(t_styles))
    return t


def _build_kpi_card_row(items: list[tuple[str, str]], page_width: float):
    """Builds a row of KPI cards for the highlight dashboard.

    Uses a FLAT two-row table (label row + value row) rather than nested
    Table-in-Table cells, which avoids the Python 3.13 / ReportLab
    incompatibility where max(rh) fails when row heights contain None.
    """
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "KpiTitle2", parent=styles["Normal"], fontSize=8, leading=9,
        fontName=FONT_BOLD, textColor=_rl_color("#4A5568"), alignment=TA_CENTER,
    )
    value_style = ParagraphStyle(
        "KpiValue2", parent=styles["Normal"], fontSize=13, leading=15,
        fontName=FONT_BOLD, textColor=_rl_color("#1A365D"), alignment=TA_CENTER,
    )

    n = len(items)
    col_w = page_width / n
    col_widths = [col_w] * n

    # Flat 2-row table: row 0 = labels, row 1 = values
    label_row = [Paragraph(lbl, title_style) for lbl, _ in items]
    value_row = [Paragraph(val, value_style) for _, val in items]
    table_data = [label_row, value_row]
    row_heights = [22, 34]  # explicit heights → never None

    t = Table(table_data, colWidths=col_widths, rowHeights=row_heights,
              splitByRow=False)
    t_styles = [
        ("BACKGROUND", (0, 0), (-1, -1), _rl_color("#F8FAFC")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, _rl_color("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    # Blue top-accent on label row, per column
    for col in range(n):
        t_styles.append(("LINEABOVE", (col, 0), (col, 0), 3, _rl_color("#1A365D")))
    t.setStyle(TableStyle(t_styles))
    return t


from reportlab.platypus import Flowable

class PageTracker(Flowable):
    def __init__(self, key, registry):
        Flowable.__init__(self)
        self.key = key
        self.registry = registry

    def draw(self):
        if self.registry is not None:
            self.registry[self.key] = self.canv.getPageNumber()


def build_commission_pdf(
    output_path: Path,
    year: int,
    *,
    invoice_dates_map: dict = None,
    # Internal data
    int_basic_t1, int_basic_lines, int_basic_t4,
    int_nfp_agent, int_nfp_rows,
    int_anp_summary, int_anp_detail, int_anp_meta,
    int_basic_meta, int_nfp_meta,
    int_ega_t1, int_ega_h1, int_ega_t2, int_ega_t3, int_ega_h3,
    int_agent_summary_rows, int_customer_summary_rows,
    int_agent_anp_rows=None, int_customer_anp_rows=None,
    # Outsource data
    out_basic_t1, out_basic_lines, out_basic_meta,
    out_nfp_agent, out_nfp_rows,
    out_anp_summary, out_anp_detail, out_anp_meta,
    out_nfp_meta,
    out_ega_t1, out_ega_h1, out_ega_t2, out_ega_t3, out_ega_h3,
    out_agent_summary_rows, out_customer_summary_rows,
    out_customer_anp_rows=None,
    out_prod_data=None,
    contest_t1_headers=None, contest_t1_rows=None,
    contest_t2_headers=None, contest_t2_rows=None,
    contest_t3_headers=None, contest_t3_rows=None,
    report_overview: dict | None = None,
    page_nums_dict: dict[str, int] = None,
    page_registry: dict[str, int] = None,
    month: int | None = None,
) -> Path:
    """Build the unified Commission PDF."""
    # Python 3.13 compatibility: ReportLab's Table._culprit does max(rh) which
    # fails when _rowHeights contains None values (comparison with float removed
    # in Py 3.13).  Patch it to return a safe string in that case.
    try:
        import reportlab.platypus.tables as _rl_tbl
        _orig_culprit = _rl_tbl.Table._culprit
        def _safe_culprit(self):
            rh = getattr(self, '_rowHeights', None) or []
            if any(h is None for h in rh):
                nr = getattr(self, '_nrows', '?')
                nc = getattr(self, '_ncols', '?')
                return f'table({nr} rows x {nc} cols — row heights not yet computed)'
            return _orig_culprit(self)
        _rl_tbl.Table._culprit = _safe_culprit
    except Exception:
        pass

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch, mm
    from reportlab.platypus import (
        BaseDocTemplate, Frame, NextPageTemplate, PageBreak,
        PageTemplate, Paragraph, Spacer, Table, TableStyle,
        KeepTogether,
    )
    from reportlab.platypus.flowables import HRFlowable

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Resolve rates dynamically from basic_commission_rates
    try:
        import basic_commission_rates
        rate_month = month if month is not None else 5
        exec_rate_val = basic_commission_rates.get_basic_rate("Internal", "executive", rate_month)
        senior_rate_val = basic_commission_rates.get_basic_rate("Internal", "senior", rate_month)
        exec_rate_str = f"{exec_rate_val * 100:.2f}%".replace(".00", "")
        senior_rate_str = f"{senior_rate_val * 100:.2f}%".replace(".00", "")
        
        oum_rate_val = basic_commission_rates.get_basic_rate("Outsource", "oum", rate_month)
        osa_rate_val = basic_commission_rates.get_basic_rate("Outsource", "osa/osa1", rate_month)
        oum_rate_str = f"{oum_rate_val * 100:.2f}%".replace(".00", "")
        osa_rate_str = f"{osa_rate_val * 100:.2f}%".replace(".00", "")
    except Exception:
        exec_rate_str = "4%" if month == 6 else "3%"
        senior_rate_str = "4.25%" if month == 6 else "3.25%"
        oum_rate_str = "5.5%" if month == 6 else "5%"
        osa_rate_str = "5.5%" if month == 6 else "4.5%"

    # In-place format and sort EGA/ESA awards and Production Bonus tables
    # Internal EGA/ESA
    for t in (int_ega_t1, int_ega_t3):
        if t:
            for r in t:
                if r and len(r) > 0 and str(r[0]).strip().lower() not in ("total", "grand total", "summary"):
                    r[0] = to_title_case(r[0])
    if int_ega_t1:
        int_ega_t1 = sort_internal_table_rows(int_ega_t1, 0)
    if int_ega_t3:
        int_ega_t3 = sort_internal_table_rows(int_ega_t3, 0)

    # Outsource EGA/ESA
    for t in (out_ega_t1, out_ega_t3):
        if t:
            for r in t:
                if r and len(r) > 0 and str(r[0]).strip().lower() not in ("total", "grand total", "summary"):
                    r[0] = to_title_case(r[0])
    if out_ega_t1:
        out_ega_t1 = sort_outsource_table_rows(out_ega_t1, 0)
    if out_ega_t3:
        out_ega_t3 = sort_outsource_table_rows(out_ega_t3, 0)

    # Production Bonus
    if out_prod_data:
        for key in ("oum_summary", "ogm_summary"):
            if out_prod_data.get(key):
                for r in out_prod_data[key]:
                    if r and len(r) > 0 and str(r[0]).strip().lower() not in ("total", "grand total", "summary"):
                        r[0] = to_title_case(r[0])
        if out_prod_data.get("team_detail"):
            for r in out_prod_data["team_detail"]:
                if len(r) > 0:
                    r[0] = to_title_case(r[0])
                if len(r) > 1:
                    r[1] = to_title_case(r[1])
            # Sort team detail by agent name (index 0)
            out_prod_data["team_detail"] = sorted(
                out_prod_data["team_detail"],
                key=lambda row: outsource_agent_sort_key(str(row[0])) if len(row) > 0 else (30, "", 2, "")
            )
        if out_prod_data.get("oum_summary"):
            out_prod_data["oum_summary"] = sort_outsource_table_rows(out_prod_data["oum_summary"], 0)
        if out_prod_data.get("ogm_summary"):
            out_prod_data["ogm_summary"] = sort_outsource_table_rows(out_prod_data["ogm_summary"], 0)

    # Page dimensions
    PAGE_W, PAGE_H = landscape(A4)
    MARGIN = int(0.55 * inch)  # ~39.6 pt
    CONTENT_W = PAGE_W - 2 * MARGIN
    LOGO_PATH = Path(__file__).resolve().parent.parent / "9. assets" / "eternalgy_logo.png"

    # Styles
    styles = getSampleStyleSheet()
    # ---- Style system (mirroring commission_pdf.py) ----
    title_style = ParagraphStyle("CommTitle", parent=styles["Normal"],
                                  fontSize=22, leading=26, fontName=FONT_BOLD,
                                  textColor=_rl_color("#1A365D"), alignment=TA_CENTER,
                                  spaceAfter=12)
    sub_title_style = ParagraphStyle("CommSubTitle", parent=styles["Normal"],
                                      fontSize=12, leading=14, fontName=FONT_REGULAR,
                                      textColor=_rl_color("#4A5568"), alignment=TA_CENTER)
    cover_meta_style = ParagraphStyle("CoverMeta", parent=styles["Normal"],
                                       fontSize=10, leading=14, fontName=FONT_REGULAR,
                                       textColor=_rl_color("#4A5568"), alignment=TA_CENTER,
                                       spaceAfter=4)
    section_title_style = ParagraphStyle("SectionTitle", parent=styles["Normal"],
                                          fontSize=14, leading=16, fontName=FONT_BOLD,
                                          textColor=_rl_color("#2C5282"),
                                          spaceBefore=6, spaceAfter=8)
    sub_section_style = ParagraphStyle("SubSectionTitle", parent=styles["Normal"],
                                        fontSize=11, leading=14, fontName=FONT_BOLD,
                                        textColor=_rl_color("#2C5282"))
    sub_heading_2_style = ParagraphStyle("SubHeading2", parent=styles["Normal"],
                                         fontSize=9.5, leading=12, fontName=FONT_BOLD,
                                         textColor=_rl_color("#319795"), spaceBefore=4, spaceAfter=4)
    body_style = ParagraphStyle("Body", parent=styles["Normal"],
                                 fontSize=10, leading=14, fontName=FONT_REGULAR,
                                 textColor=_rl_color("#2D3748"), spaceAfter=6)
    # TOC styles
    toc_left_style = ParagraphStyle("TocLeft", parent=styles["Normal"], fontSize=10,
                                     leading=12, fontName=FONT_BOLD,
                                     textColor=_rl_color("#1A365D"))
    toc_right_style = ParagraphStyle("TocRight", parent=styles["Normal"], fontSize=10,
                                      leading=12, fontName=FONT_BOLD,
                                      textColor=_rl_color("#4A5568"), alignment=TA_RIGHT)
    toc_sub_style = ParagraphStyle("TocSub", parent=styles["Normal"], fontSize=8.5,
                                    leading=11, fontName=FONT_REGULAR,
                                    textColor=_rl_color("#4A5568"), leftIndent=24)
    toc_group_style = ParagraphStyle("TocGroup", parent=styles["Normal"], fontSize=9.5,
                                      leading=11.5, fontName=FONT_BOLD,
                                      textColor=_rl_color("#1A365D"), leftIndent=12)
    # KPI highlight styles
    kpi_title_style = ParagraphStyle("KpiTitleH", parent=styles["Normal"], fontSize=8.5,
                                      leading=10, fontName=FONT_BOLD,
                                      textColor=_rl_color("#718096"))
    kpi_value_style = ParagraphStyle("KpiValueH", parent=styles["Normal"], fontSize=22,
                                      leading=26, fontName=FONT_BOLD,
                                      textColor=_rl_color("#1A365D"))
    kpi_sub_style = ParagraphStyle("KpiSubH", parent=styles["Normal"], fontSize=8,
                                    leading=10, fontName=FONT_REGULAR,
                                    textColor=_rl_color("#718096"))
    kpi_small_style = ParagraphStyle("KpiSmallH", parent=styles["Normal"], fontSize=13,
                                      leading=16, fontName=FONT_BOLD,
                                      textColor=_rl_color("#1A365D"))

    meta_style = ParagraphStyle("MetaStyle", parent=styles["Normal"], fontSize=8.5,
                                 leading=11, fontName=FONT_REGULAR,
                                 textColor=_rl_color("#4A5568"), spaceBefore=2, spaceAfter=6)

    # Report-overview styles (Executive Summary / Top Performers / Payout by Type)
    overview_eyebrow_style = ParagraphStyle("OverviewEyebrow", parent=styles["Normal"],
                                             fontSize=9, leading=11, fontName=FONT_BOLD,
                                             textColor=_rl_color("#0E7C66"), spaceAfter=2)
    ov_kpi_label_style = ParagraphStyle("OvKpiLabel", parent=styles["Normal"], fontSize=8.5,
                                         leading=10, fontName=FONT_BOLD,
                                         textColor=_rl_color("#576270"))
    ov_kpi_value_style = ParagraphStyle("OvKpiValue", parent=styles["Normal"], fontSize=24,
                                         leading=28, fontName=FONT_BOLD,
                                         textColor=_rl_color("#141A1F"))
    ov_kpi_sub_style = ParagraphStyle("OvKpiSub", parent=styles["Normal"], fontSize=8,
                                       leading=10, fontName=FONT_REGULAR,
                                       textColor=_rl_color("#8B95A3"))
    ov_tile_value_style = ParagraphStyle("OvTileValue", parent=styles["Normal"], fontSize=17,
                                          leading=20, fontName=FONT_BOLD,
                                          textColor=_rl_color("#141A1F"))
    ov_delta_style = ParagraphStyle("OvDelta", parent=styles["Normal"], fontSize=8.5,
                                     leading=11, fontName=FONT_BOLD)
    ov_row_style = ParagraphStyle("OvRow", parent=styles["Normal"], fontSize=9,
                                   leading=12, fontName=FONT_REGULAR,
                                   textColor=_rl_color("#141A1F"))
    ov_amt_style = ParagraphStyle("OvAmt", parent=styles["Normal"], fontSize=9,
                                   leading=12, fontName=FONT_REGULAR,
                                   textColor=_rl_color("#141A1F"), alignment=TA_RIGHT)
    ov_hdr_style = ParagraphStyle("OvHdr", parent=styles["Normal"], fontSize=7.5,
                                   leading=9, fontName=FONT_BOLD,
                                   textColor=_rl_color("#8B95A3"))
    ov_hdr_right_style = ParagraphStyle("OvHdrRight", parent=styles["Normal"], fontSize=7.5,
                                         leading=9, fontName=FONT_BOLD,
                                         textColor=_rl_color("#8B95A3"), alignment=TA_RIGHT)

    # ---- Style revision: full-bleed band header + gauge/podium/ledger ----
    BAND_BG = "#0F1922"       # matches the dashboard sidebar's background -- the band every interior page opens with
    BAND_INK = "#EAF3EF"
    BAND_MUTED = "#8FB3A8"
    ACCENT = "#FF7A45"        # coral, spent on exactly one number per page

    band_eyebrow_style = ParagraphStyle("BandEyebrow", parent=styles["Normal"], fontSize=9,
                                         leading=11, fontName=FONT_BOLD,
                                         textColor=_rl_color(BAND_MUTED), spaceAfter=3)
    band_title_style = ParagraphStyle("BandTitle", parent=styles["Normal"], fontSize=19,
                                       leading=22, fontName=FONT_BOLD,
                                       textColor=_rl_color(BAND_INK))
    band_sub_style = ParagraphStyle("BandSub", parent=styles["Normal"], fontSize=10,
                                     leading=13, fontName=FONT_REGULAR,
                                     textColor=_rl_color(BAND_MUTED), spaceBefore=4)

    cover_title_style = ParagraphStyle("CoverTitle2", parent=styles["Normal"], fontSize=40,
                                        leading=44, fontName=FONT_BOLD,
                                        textColor=_rl_color(BAND_INK))
    cover_sub_style = ParagraphStyle("CoverSub2", parent=styles["Normal"], fontSize=13,
                                      leading=17, fontName=FONT_REGULAR,
                                      textColor=_rl_color(BAND_MUTED))
    cover_meta_style2 = ParagraphStyle("CoverMeta2", parent=styles["Normal"], fontSize=10,
                                        leading=15, fontName=FONT_REGULAR,
                                        textColor=_rl_color(BAND_MUTED))
    cover_meta_b_style = ParagraphStyle("CoverMetaB2", parent=styles["Normal"], fontSize=10,
                                         leading=15, fontName=FONT_BOLD,
                                         textColor=_rl_color(BAND_INK))

    hero_num_style = ParagraphStyle("HeroNum", parent=styles["Normal"], fontSize=46,
                                     leading=48, fontName=FONT_BOLD,
                                     textColor=_rl_color("#141A1F"))
    hero_label_style = ParagraphStyle("HeroLabel", parent=styles["Normal"], fontSize=9.5,
                                       leading=11, fontName=FONT_BOLD,
                                       textColor=_rl_color("#576270"))
    hero_sub_style = ParagraphStyle("HeroSub", parent=styles["Normal"], fontSize=8.5,
                                     leading=11, fontName=FONT_REGULAR,
                                     textColor=_rl_color("#8B95A3"))
    rail_label_style = ParagraphStyle("RailLabel", parent=styles["Normal"], fontSize=9,
                                       leading=11, fontName=FONT_BOLD,
                                       textColor=_rl_color("#576270"))
    rail_value_style = ParagraphStyle("RailValue", parent=styles["Normal"], fontSize=14,
                                       leading=17, fontName=FONT_BOLD,
                                       textColor=_rl_color("#141A1F"), alignment=TA_RIGHT)
    gauge_title_style = ParagraphStyle("GaugeTitle", parent=styles["Normal"], fontSize=9,
                                        leading=11, fontName=FONT_BOLD, alignment=TA_CENTER,
                                        textColor=_rl_color("#576270"))
    gauge_value_style = ParagraphStyle("GaugeValue", parent=styles["Normal"], fontSize=24,
                                        leading=27, fontName=FONT_BOLD, alignment=TA_CENTER,
                                        textColor=_rl_color("#141A1F"))
    gauge_caption_style = ParagraphStyle("GaugeCaption", parent=styles["Normal"], fontSize=7.8,
                                          leading=10, fontName=FONT_REGULAR, alignment=TA_CENTER,
                                          textColor=_rl_color("#576270"))

    podium_rank_style = ParagraphStyle("PodiumRank", parent=styles["Normal"], fontSize=8.5,
                                        leading=10, fontName=FONT_BOLD,
                                        textColor=_rl_color(BAND_MUTED))
    podium_rank_style_light = ParagraphStyle("PodiumRankL", parent=styles["Normal"], fontSize=8.5,
                                              leading=10, fontName=FONT_BOLD,
                                              textColor=_rl_color("#8B95A3"))
    podium_name_style = ParagraphStyle("PodiumName", parent=styles["Normal"], fontSize=12,
                                        leading=15, fontName=FONT_BOLD,
                                        textColor=_rl_color(BAND_INK))
    podium_name_style_light = ParagraphStyle("PodiumNameL", parent=styles["Normal"], fontSize=12,
                                              leading=15, fontName=FONT_BOLD,
                                              textColor=_rl_color("#141A1F"))
    podium_amt_style = ParagraphStyle("PodiumAmt", parent=styles["Normal"], fontSize=16,
                                       leading=19, fontName=FONT_BOLD,
                                       textColor=_rl_color(ACCENT))
    podium_amt_style_light = ParagraphStyle("PodiumAmtL", parent=styles["Normal"], fontSize=16,
                                             leading=19, fontName=FONT_BOLD,
                                             textColor=_rl_color("#141A1F"))
    rest_name_style = ParagraphStyle("RestName", parent=styles["Normal"], fontSize=10,
                                      leading=13, fontName=FONT_REGULAR,
                                      textColor=_rl_color("#141A1F"))
    rest_amt_style = ParagraphStyle("RestAmt", parent=styles["Normal"], fontSize=9.5,
                                     leading=12, fontName=FONT_REGULAR, alignment=TA_RIGHT,
                                     textColor=_rl_color("#576270"))

    type_item_name_style = ParagraphStyle("TypeItemName", parent=styles["Normal"], fontSize=9.5,
                                           leading=12, fontName=FONT_BOLD,
                                           textColor=_rl_color("#141A1F"))
    type_item_amt_style = ParagraphStyle("TypeItemAmt", parent=styles["Normal"], fontSize=14,
                                          leading=17, fontName=FONT_BOLD,
                                          textColor=_rl_color("#141A1F"))
    type_item_pct_style = ParagraphStyle("TypeItemPct", parent=styles["Normal"], fontSize=8.5,
                                          leading=10, fontName=FONT_REGULAR,
                                          textColor=_rl_color("#8B95A3"))
    grand_label_style = ParagraphStyle("GrandLabel", parent=styles["Normal"], fontSize=10,
                                        leading=13, fontName=FONT_BOLD,
                                        textColor=_rl_color("#576270"))
    grand_value_style = ParagraphStyle("GrandValue", parent=styles["Normal"], fontSize=22,
                                        leading=25, fontName=FONT_BOLD, alignment=TA_RIGHT,
                                        textColor=_rl_color("#141A1F"))

    ledger_hdr_style = ParagraphStyle("LedgerHdr", parent=styles["Normal"], fontSize=7.8,
                                       leading=9, fontName=FONT_BOLD,
                                       textColor=_rl_color("#8B95A3"))
    ledger_hdr_right_style = ParagraphStyle("LedgerHdrRight", parent=styles["Normal"], fontSize=7.8,
                                             leading=9, fontName=FONT_BOLD, alignment=TA_RIGHT,
                                             textColor=_rl_color("#8B95A3"))
    ledger_row_style = ParagraphStyle("LedgerRow", parent=styles["Normal"], fontSize=9.5,
                                       leading=12, fontName=FONT_REGULAR,
                                       textColor=_rl_color("#141A1F"))
    ledger_num_style = ParagraphStyle("LedgerNum", parent=styles["Normal"], fontSize=9,
                                       leading=12, fontName=FONT_REGULAR, alignment=TA_RIGHT,
                                       textColor=_rl_color("#576270"))
    ledger_tot_style = ParagraphStyle("LedgerTot", parent=styles["Normal"], fontSize=9.5,
                                       leading=12, fontName=FONT_BOLD, alignment=TA_RIGHT,
                                       textColor=_rl_color("#141A1F"))
    ledger_grand_style = ParagraphStyle("LedgerGrand", parent=styles["Normal"], fontSize=10.5,
                                         leading=13, fontName=FONT_BOLD, alignment=TA_RIGHT,
                                         textColor=_rl_color("#141A1F"))
    ledger_grand_label_style = ParagraphStyle("LedgerGrandLabel", parent=styles["Normal"], fontSize=10.5,
                                               leading=13, fontName=FONT_BOLD,
                                               textColor=_rl_color("#141A1F"))
    toc_num_style = ParagraphStyle("TocNum", parent=styles["Normal"], fontSize=9,
                                    leading=11, fontName=FONT_REGULAR,
                                    textColor=_rl_color("#8B95A3"))
    toc_grp2_style = ParagraphStyle("TocGrp2", parent=styles["Normal"], fontSize=9.5,
                                     leading=12, fontName=FONT_BOLD,
                                     textColor=_rl_color("#095A4A"), spaceBefore=10, spaceAfter=4)
    toc_title2_style = ParagraphStyle("TocTitle2", parent=styles["Normal"], fontSize=11,
                                       leading=14, fontName=FONT_BOLD,
                                       textColor=_rl_color("#141A1F"))
    toc_pg2_style = ParagraphStyle("TocPg2", parent=styles["Normal"], fontSize=10,
                                    leading=13, fontName=FONT_REGULAR, alignment=TA_RIGHT,
                                    textColor=_rl_color("#576270"))
    toc_about_style = ParagraphStyle("TocAbout2", parent=styles["Normal"], fontSize=9.5,
                                      leading=15, fontName=FONT_REGULAR,
                                      textColor=_rl_color("#576270"))

    def _band_header(eyebrow: str, title: str, subtitle: str = "") -> "Table":
        """Full-width dark band that opens every interior page -- replaces
        the old plain-white-page-with-colored-eyebrow-text look."""
        cell = [Paragraph(eyebrow.upper(), band_eyebrow_style), Paragraph(title, band_title_style)]
        if subtitle:
            cell.append(Paragraph(subtitle, band_sub_style))
        t = Table([[cell]], colWidths=[CONTENT_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _rl_color(BAND_BG)),
            ("LEFTPADDING", (0, 0), (-1, -1), 18), ("RIGHTPADDING", (0, 0), (-1, -1), 18),
            ("TOPPADDING", (0, 0), (-1, -1), 13), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return t

    def _gauge_drawing(pct: float, max_pct: float = 6.0, size: float = 150):
        """Semicircle gauge (Effective Commission Rate is a rate against a
        range, not a headcount, so it gets a dial instead of a flat tile).

        Built on the same Pie class as the donut, not hand-rolled annular
        Wedges: a Wedge with radius1 set self-intersects into a bowtie for
        any sweep wider than a few degrees (confirmed empirically), while
        Pie's own annular ("donut") mode handles wide sweeps correctly. The
        gauge look comes from a third data slice covering the entire bottom
        half in the page's own background color, so it's invisible.
        """
        from reportlab.graphics.shapes import Drawing
        from reportlab.graphics.charts.piecharts import Pie
        frac = max(0.0, min(1.0, pct / max_pct)) if max_pct else 0.0
        fill_deg = frac * 180.0
        # Full square Drawing, bottom half painted to match the page (not
        # clipped) -- a Drawing flowable doesn't reliably clip its children
        # to a smaller declared height, so asking for only the top half's
        # worth of space risks the bottom half bleeding into what follows.
        d = Drawing(size, size)
        pie = Pie()
        pie.x, pie.y = 0, 0
        pie.width = pie.height = size
        pie.data = [max(fill_deg, 0.001), max(180 - fill_deg, 0.001), 180]
        pie.labels = None
        pie.startAngle = 180
        pie.direction = "clockwise"
        pie.innerRadiusFraction = 0.62
        pie.slices.strokeColor = None
        pie.slices[0].fillColor = _rl_color("#0E7C66")
        pie.slices[1].fillColor = _rl_color("#E7EBEF")
        pie.slices[2].fillColor = _rl_color("#FFFFFF")
        d.add(pie)
        return d

    def _proportion_bar(segments: list[tuple[str, float, str]], width: float, height: float = 32):
        """Single stacked bar -- the Payout by Type page's replacement for a
        thin donut ring, reads left-to-right and scales past 3-4 categories
        far better than a pie ever does."""
        total = sum(v for _, v, _ in segments) or 1.0
        cols, colors_list = [], []
        for _, val, color_hex in segments:
            w = max(1, int(round(width * (val / total)))) if val > 0 else 0
            if w > 0:
                cols.append(w)
                colors_list.append(color_hex)
        if not cols:
            cols, colors_list = [int(width)], ["#E7EBEF"]
        t = Table([["" for _ in cols]], colWidths=cols, rowHeights=[height])
        styles_list = [("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                       ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]
        for i, c in enumerate(colors_list):
            styles_list.append(("BACKGROUND", (i, 0), (i, 0), _rl_color(c)))
        t.setStyle(TableStyle(styles_list))
        return t

    # Current page number tracking
    page_counter = [0]

    # Logo reader for cover page
    logo_reader = None
    try:
        from reportlab.lib.utils import ImageReader
        if LOGO_PATH.exists():
            logo_reader = ImageReader(str(LOGO_PATH))
    except Exception:
        pass

    def on_page(canvas, doc):
        page_counter[0] = doc.page
        canvas.saveState()
        # Cover page (1) is full-bleed dark -- painted before the frame's
        # flowables land on top of it, per BaseDocTemplate.handle_pageBegin.
        if doc.page == 1:
            canvas.setFillColor(_rl_color(BAND_BG))
            canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        # Footer
        canvas.setFont(FONT_REGULAR, 9)
        canvas.setFillColor(_rl_color("#8FB3A8") if doc.page == 1 else _rl_color("#4A5568"))
        canvas.drawRightString(PAGE_W - MARGIN, 0.35 * inch, str(doc.page))
        # Logo on cover (page 1)
        if doc.page == 1 and logo_reader is not None:
            lh = 0.55 * inch
            lw_logo = 1.80 * inch
            canvas.drawImage(logo_reader, MARGIN, PAGE_H - 0.22 * inch - lh,
                             width=lw_logo, height=lh,
                             preserveAspectRatio=True, mask="auto")
        canvas.restoreState()

    # Create document
    doc = BaseDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=30,
        bottomMargin=24,
    )
    frame = Frame(MARGIN, int(0.55 * inch), CONTENT_W, PAGE_H - MARGIN - int(0.55 * inch),
                  id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    template = PageTemplate(id="main_page", frames=[frame], onPage=on_page)
    doc.addPageTemplates([template])

    story = []

    from datetime import datetime as _dt
    def _fmt_cover_date(ts: str) -> str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return _dt.strptime(ts[:19] if " " in ts else ts, fmt).strftime("%d %B %Y")
            except ValueError:
                continue
        return ts

    def _sep_line():
        """Thin horizontal separator line (full CONTENT_W)."""
        tbl = Table([[""]], colWidths=[CONTENT_W], rowHeights=[2])
        tbl.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, -1), 0.75, _rl_color("#CBD5E0")),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return tbl

    def _add_note_paragraphs(note_lines, space_before=6):
        story.append(Spacer(1, space_before))
        note_title_style = ParagraphStyle(
            "NoteTitle",
            parent=styles["Normal"],
            fontSize=8,
            leading=10,
            fontName=FONT_BOLD,
            textColor=_rl_color("#1A365D"),
            spaceAfter=4
        )
        note_text_style = ParagraphStyle(
            "NoteText",
            parent=styles["Normal"],
            fontSize=7.5,
            leading=9.5,
            fontName=FONT_REGULAR,
            textColor=_rl_color("#4A5568"),
            spaceAfter=2
        )
        cell_elements = []
        cell_elements.append(Paragraph("<b>Note:</b>", note_title_style))
        for line in note_lines:
            cell_elements.append(Paragraph(line, note_text_style))
            
        note_table = Table([[cell_elements]], colWidths=[CONTENT_W])
        note_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _rl_color("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.5, _rl_color("#E2E8F0")),
            ("LINELEFT", (0, 0), (-1, -1), 3.0, _rl_color("#1A365D")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(note_table)

    def _mini_bar(frac: float, width: float, height: float = 8,
                   color_hex: str = "#0E7C66", bg_hex: str = "#EDF2F7"):
        """A horizontal bar built from a 1-row Table -- ReportLab has no
        native bar-chart flowable, but a two-cell table with proportional
        colWidths renders identically."""
        frac = max(0.0, min(1.0, frac))
        filled = max(2, int(round(width * frac)))
        empty = max(0, int(width) - filled)
        if empty <= 0:
            t = Table([[""]], colWidths=[width], rowHeights=[height])
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _rl_color(color_hex))]))
            return t
        t = Table([["", ""]], colWidths=[filled, empty], rowHeights=[height])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), _rl_color(color_hex)),
            ("BACKGROUND", (1, 0), (1, 0), _rl_color(bg_hex)),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        return t

    def _donut_drawing(segments: list[tuple[str, float, str]], size: float = 150,
                        hole_frac: float = 0.55, center_label: str = ""):
        """segments: list of (label, value, hex color). Pie with
        innerRadiusFraction -- hand-built annular Wedges glitch on sweeps
        past ~180 degrees, the charts module's donut mode doesn't."""
        from reportlab.graphics.shapes import Drawing, String
        from reportlab.graphics.charts.piecharts import Pie
        d = Drawing(size, size)
        pie = Pie()
        pie.x = pie.y = 4
        pie.width = pie.height = size - 8
        pie.data = [max(v, 0.0) for _, v, _ in segments] or [1.0]
        pie.labels = None
        pie.startAngle = 90
        pie.direction = "clockwise"
        pie.innerRadiusFraction = hole_frac
        pie.slices.strokeColor = _rl_color("#FFFFFF")
        pie.slices.strokeWidth = 1
        for i, (_, _, color_hex) in enumerate(segments):
            pie.slices[i].fillColor = _rl_color(color_hex)
        d.add(pie)
        if center_label:
            d.add(String(size / 2.0, size / 2.0 - 4, center_label,
                         fontName=FONT_BOLD, fontSize=10, fillColor=_rl_color("#141A1F"),
                         textAnchor="middle"))
        return d

    def _swatch(color_hex: str, size: float = 10):
        t = Table([[""]], colWidths=[size], rowHeights=[size])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _rl_color(color_hex))]))
        return t

    def _delta_text(curr: float, prev: float | None, mode: str = "pct") -> tuple[str, str]:
        """Month-over-month delta as (display text, color). mode is "pct"
        (percentage change), "points" (percentage-POINT change, e.g. a rate),
        or "count" (raw difference, e.g. agent headcount)."""
        if prev is None:
            return ("No prior month on file", "#8B95A3")
        diff = curr - prev
        if mode == "points":
            label = f"{abs(diff):.2f} pts"
        elif mode == "count":
            label = f"{abs(diff):,.0f}"
        else:
            pct = (diff / prev * 100.0) if prev else 0.0
            label = f"{abs(pct):.1f}%"
        # ASCII +/- only: the registered Verdana TTF has no glyphs for the
        # &#9650;/&#9660; triangles, which render as empty boxes.
        if diff > 1e-9:
            return (f"+{label} vs last month", "#1F8A4C")
        if diff < -1e-9:
            return (f"-{label} vs last month", "#C6432E")
        return ("No change vs last month", "#8B95A3")

    def _rank_badge_text(code: str) -> tuple[str, str]:
        if code == "new":
            return ("NEW", "#B5651D")
        if code == "flat":
            return ("&mdash;", "#8B95A3")
        if code.startswith("up:"):
            return (f"+{code.split(':', 1)[1]}", "#1F8A4C")
        if code.startswith("down:"):
            return (f"-{code.split(':', 1)[1]}", "#C6432E")
        return ("-", "#8B95A3")

    def _highlight_block(label: str, grand: float,
                          basic: float, nfp: float, anp: float,
                          agents: int, invoices: int) -> list:
        """Builds a commission highlight block matching commission_pdf.py design."""
        out = []
        # Row 1: Total Commission Paid | sub-breakdown
        col_w = CONTENT_W / 2
        row1 = Table(
            [[Paragraph("TOTAL COMMISSION PAID", kpi_title_style),
              Paragraph("BREAKDOWN", kpi_title_style)],
             [Paragraph(f"RM {grand:,.2f}", kpi_value_style),
              [Paragraph(f"Basic: RM {basic:,.2f}", kpi_sub_style),
               Spacer(1, 2),
               Paragraph(f"NFP:&nbsp;&nbsp;&nbsp; RM {nfp:,.2f}", kpi_sub_style),
               Spacer(1, 2),
               Paragraph(f"ANP:&nbsp;&nbsp;&nbsp;RM {anp:,.2f}", kpi_sub_style)]]],
            colWidths=[col_w, col_w]
        )
        row1.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        out.append(row1)
        out.append(Spacer(1, 8))
        # Row 2: Agents | Invoices
        row2 = Table(
            [[Paragraph("ACTIVE AGENTS", kpi_title_style),
              Paragraph("TOTAL INVOICES", kpi_title_style)],
             [Paragraph(str(agents), kpi_small_style),
              Paragraph(str(invoices), kpi_small_style)]],
            colWidths=[col_w, col_w]
        )
        row2.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        out.append(row2)
        return out

    def _spacer(h=8):
        story.append(Spacer(1, h))

    # ----------------------------------------------------------------
    # Compute totals (cover + highlights)
    # ----------------------------------------------------------------
    int_basic_total = float(int_basic_meta.get("total_commission", 0))
    int_nfp_total = float(int_nfp_meta.get("total_commission", 0))
    int_anp_total = float(int_anp_meta.get("total_commission", 0))
    int_grand = int_basic_total + int_nfp_total + int_anp_total
    out_basic_total = float(out_basic_meta.get("total_commission", 0))
    out_nfp_total = float(out_nfp_meta.get("total_commission", 0))
    out_anp_total = float(out_anp_meta.get("total_commission", 0))
    out_grand = out_basic_total + out_nfp_total + out_anp_total
    grand_total = int_grand + out_grand

    int_agents = int_basic_meta.get("agents", 0)
    int_invoices = (int_basic_meta.get("invoices", 0)
                    + int_nfp_meta.get("invoices", 0)
                    + int_anp_meta.get("invoices", 0))
    out_agents = out_basic_meta.get("agents", 0)
    out_invoices = (out_basic_meta.get("invoices", 0)
                    + out_nfp_meta.get("invoices", 0)
                    + out_anp_meta.get("invoices", 0))

    # ----------------------------------------------------------------
    # Cover Page  (design: commission_pdf.py Page 1)
    # ----------------------------------------------------------------
    MONTH_NAMES = {
        1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
        7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"
    }
    sub_title_text = f"{MONTH_NAMES[month]} {year}" if month is not None else "H1 — January to June"
    contest_title = f"{MONTH_NAMES[month]} Monthly Contest" if month is not None else "Monthly Contest"
    story.append(Spacer(1, int(0.9 * inch)))
    story.append(Paragraph("Commission<br/>Report", cover_title_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph("All Agents &mdash; Internal &amp; Outsource, Combined", cover_sub_style))
    period_pill = Table([[Paragraph(sub_title_text.upper(), ParagraphStyle(
        "PeriodPill", parent=styles["Normal"], fontSize=10.5, leading=13, fontName=FONT_BOLD,
        textColor=_rl_color("#1A365D")))]], colWidths=[None])
    period_pill.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _rl_color("#C3DAF2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(Spacer(1, 10))
    story.append(period_pill)
    story.append(Spacer(1, int(0.45 * inch)))
    story.append(Paragraph('<font color="#EAF3EF"><b>Prepared for</b></font> &nbsp;Eternalgy HR and Finance Department', cover_meta_style2))
    story.append(Paragraph('<font color="#EAF3EF"><b>Prepared by</b></font> &nbsp;Nurul Aqilah', cover_meta_style2))
    story.append(Paragraph(f'<font color="#EAF3EF"><b>Generated on</b></font> &nbsp;{_fmt_cover_date(run_at)}', cover_meta_style2))
    story.append(PageBreak())

    # ----------------------------------------------------------------
    # Table of Contents  (design: commission_pdf.py Page 2)
    # ----------------------------------------------------------------
    story.append(_band_header("Contents", "Table of Contents"))
    _spacer(10)
    story.append(Paragraph(
        "<b>About this document.</b> This document is the consolidated Commission Report for the "
        f"{year} calendar year. It covers every agent &mdash; Internal and Outsource alike &mdash; "
        "in one Overview (Executive Summary, Top Performers and Total Payout by Type), "
        "followed by the per-agent commission summary. Invoice-level detail lives in the "
        "Excel export from the dashboard.",
        body_style,
    ))
    story.append(Spacer(1, int(0.15 * inch)))

    if page_nums_dict is None:
        page_nums_dict = {}

    page_exec_summary = page_nums_dict.get("exec_summary", 3)
    page_top_performers = page_nums_dict.get("top_performers", 4)
    page_payout_type = page_nums_dict.get("payout_type", 5)
    page_agent_summary = page_nums_dict.get("agent_summary", 6)

    def _toc_group(label):
        story.append(Paragraph(label.upper(), toc_grp2_style))

    def _toc_row(idx, title, page):
        row = Table([[Paragraph(f"{idx:02d}", toc_num_style),
                      Paragraph(title, toc_title2_style),
                      Paragraph(f"Pg {page}", toc_pg2_style)]],
                    colWidths=[26, CONTENT_W - 26 - 60, 60])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, _rl_color("#E7EBEF")),
        ]))
        return row

    _toc_group("Overview")
    story.append(_toc_row(1, "Executive Summary", page_exec_summary))
    story.append(_toc_row(2, "Top Performers &amp; Concentration", page_top_performers))
    story.append(_toc_row(3, "Total Commission Payout by Type", page_payout_type))
    _toc_group("Commission Detail")
    story.append(_toc_row(4, "Summary Agent Commission &mdash; All Agents", page_agent_summary))
    story.append(PageBreak())

    # ----------------------------------------------------------------
    # Overview: Executive Summary / Top Performers & Concentration /
    # Total Commission Payout by Type -- all agents combined.
    # ----------------------------------------------------------------
    if report_overview is not None:
        ov = report_overview["overview_current"]
        ov_prev = report_overview["overview_prev"]
        period_label = report_overview.get("period_label", "")
        prev_label = report_overview.get("prev_period_label")

        # --- Executive Summary ---
        story.append(PageTracker("exec_summary", page_registry))
        scope_txt = f"All Agents &middot; {period_label}"
        scope_txt += (f" &middot; compared against {prev_label}" if prev_label
                      else " &middot; no prior month on file for comparison")
        story.append(_band_header("Overview", "Executive Summary", scope_txt))
        _spacer(14)

        total_delta_txt, total_delta_color = _delta_text(ov["total"], ov_prev["total"] if ov_prev else None)
        eff_txt, eff_color = _delta_text(ov["effective_rate"], ov_prev["effective_rate"] if ov_prev else None, mode="points")
        ag_txt, ag_color = _delta_text(ov["agents"], ov_prev["agents"] if ov_prev else None, mode="count")
        inv_txt, inv_color = _delta_text(ov["invoices"], ov_prev["invoices"] if ov_prev else None, mode="count")

        # Left: one oversized number carries the page instead of a row of
        # equal-weight tiles, plus a thin rail for the two headcount stats.
        left_col = [
            Paragraph("TOTAL COMMISSION PAID", hero_label_style),
            Spacer(1, 2),
            Paragraph(f"RM {ov['total']:,.0f}", hero_num_style),
            Spacer(1, 6),
            Paragraph("Basic + NFP + ANP &middot; bonuses are added on the Payout by Type page", hero_sub_style),
            Paragraph(total_delta_txt, ParagraphStyle("DeltaHero2", parent=styles["Normal"], fontSize=12.5,
                                                       fontName=FONT_BOLD, spaceBefore=8,
                                                       textColor=_rl_color(total_delta_color))),
        ]
        rail = Table(
            [[Paragraph("ACTIVE AGENTS", rail_label_style), Paragraph(str(ov["agents"]), rail_value_style),
              Paragraph(ag_txt, ParagraphStyle("RailD1", parent=ov_delta_style, alignment=TA_RIGHT, textColor=_rl_color(ag_color)))],
             [Paragraph("TOTAL INVOICES", rail_label_style), Paragraph(str(ov["invoices"]), rail_value_style),
              Paragraph(inv_txt, ParagraphStyle("RailD2", parent=ov_delta_style, alignment=TA_RIGHT, textColor=_rl_color(inv_color)))]],
            colWidths=[None, 70, 140],
        )
        rail.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.75, _rl_color("#E7EBEF")),
            ("LINEABOVE", (0, 0), (-1, 0), 0.75, _rl_color("#E7EBEF")),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        left_col.append(Spacer(1, 16))
        left_col.append(rail)

        # Right: Effective Rate as a gauge -- it's a rate against a range,
        # not a headcount, so a dial reads more honestly than a flat number.
        gauge = _gauge_drawing(ov["effective_rate"], max_pct=6.0, size=170)
        right_col = [
            Paragraph("EFFECTIVE COMMISSION RATE", gauge_title_style),
            Spacer(1, 4),
            gauge,
            # Pulls the value text up into the gauge's own blank bottom half
            # instead of stacking below the Drawing's full declared height.
            Spacer(1, -96),
            Paragraph(f"{ov['effective_rate']:.2f}%", gauge_value_style),
            Paragraph(eff_txt, ParagraphStyle("GaugeDelta", parent=styles["Normal"], fontSize=9.5,
                                               fontName=FONT_BOLD, alignment=TA_CENTER,
                                               textColor=_rl_color(eff_color), spaceBefore=2, spaceAfter=8)),
            Paragraph("Commission Paid &divide; Sales Value &times; 100. Rising faster than sales is worth a "
                      "closer look; steady or falling means payout is keeping pace with revenue.",
                      gauge_caption_style),
        ]

        es_grid = Table([[left_col, right_col]], colWidths=[CONTENT_W * 0.58, CONTENT_W * 0.42])
        es_grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "CENTER"),
            ("LINEBEFORE", (1, 0), (1, 0), 0.75, _rl_color("#E7EBEF")),
            ("LEFTPADDING", (1, 0), (1, 0), 24), ("RIGHTPADDING", (0, 0), (0, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(es_grid)
        story.append(PageBreak())

        # --- Top Performers & Concentration ---
        story.append(PageTracker("top_performers", page_registry))
        top_n = min(10, len(report_overview["unified_current"]))
        story.append(_band_header(
            "Overview", "Top Performers &amp; Concentration",
            f"Top {top_n} of {ov['agents']} active agents, by total commission (Basic + NFP + ANP) &middot; {period_label}"))
        _spacer(14)

        unified_current = report_overview["unified_current"]
        rank_changes = report_overview["rank_changes"]

        if top_n == 0:
            story.append(Paragraph("No agents with commission this month.", body_style))
        else:
            top_rows = unified_current[:top_n]
            podium_rows = top_rows[:3]
            rest_rows = top_rows[3:]

            def _podium_card(rank, r, dark):
                rank_style = podium_rank_style if dark else podium_rank_style_light
                name_style = podium_name_style if dark else podium_name_style_light
                amt_style = podium_amt_style if dark else podium_amt_style_light
                tag_color = ("#8FB3A8" if dark else "#0B6350") if r["type"] == "Internal" else ("#F4C9A8" if dark else "#B54A1D")
                tag_label = "INTERNAL" if r["type"] == "Internal" else "OUTSOURCE"
                badge_txt, badge_color = _rank_badge_text(rank_changes.get(r["agent"], "flat"))
                cell = [
                    Paragraph(f"RANK {rank:02d}", rank_style),
                    Spacer(1, 5),
                    Paragraph(r["agent"], name_style),
                    Paragraph(f'<font color="{tag_color}"><b>{tag_label}</b></font>',
                              ParagraphStyle(f"PodTag{rank}", parent=styles["Normal"], fontSize=7.5, fontName=FONT_BOLD)),
                    Spacer(1, 8),
                    Paragraph(f'RM {r["total"]:,.0f}', amt_style),
                    Paragraph(badge_txt, ParagraphStyle(f"PodChg{rank}", parent=styles["Normal"], fontSize=8.5,
                                                         fontName=FONT_BOLD, spaceBefore=3,
                                                         textColor=_rl_color(badge_color if dark else badge_color))),
                ]
                return cell

            podium_cells = [_podium_card(i + 1, r, dark=(i == 0)) for i, r in enumerate(podium_rows)]
            while len(podium_cells) < 3:
                podium_cells.append([Paragraph("", body_style)])
            podium = Table([podium_cells], colWidths=[(CONTENT_W - 24) / 3.0] * 3)
            p_styles = [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                ("BACKGROUND", (0, 0), (0, 0), _rl_color(BAND_BG)),
            ]
            if len(podium_rows) > 1:
                p_styles += [("BACKGROUND", (1, 0), (1, 0), _rl_color("#F5F8F7")),
                             ("BOX", (1, 0), (1, 0), 0.75, _rl_color("#E7EBEF"))]
            if len(podium_rows) > 2:
                p_styles += [("BACKGROUND", (2, 0), (2, 0), _rl_color("#F5F8F7")),
                             ("BOX", (2, 0), (2, 0), 0.75, _rl_color("#E7EBEF"))]
            podium.setStyle(TableStyle(p_styles))
            story.append(podium)
            _spacer(14)

            if rest_rows:
                rest_data = []
                for i, r in enumerate(rest_rows):
                    rank = i + 4
                    tag_color = "#0B6350" if r["type"] == "Internal" else "#B54A1D"
                    tag_label = "INT" if r["type"] == "Internal" else "OUT"
                    name_para = Paragraph(
                        f'<font color="{tag_color}"><b>{tag_label}</b></font>&nbsp;&nbsp;{r["agent"]}', rest_name_style)
                    amt_para = Paragraph(f'RM {r["total"]:,.2f}', rest_amt_style)
                    badge_txt, badge_color = _rank_badge_text(rank_changes.get(r["agent"], "flat"))
                    chg_para = Paragraph(badge_txt, ParagraphStyle(f"RestChg{i}", parent=styles["Normal"], fontSize=9,
                                                                    fontName=FONT_BOLD, alignment=TA_RIGHT,
                                                                    textColor=_rl_color(badge_color)))
                    rest_data.append([Paragraph(f"{rank:02d}", toc_num_style), name_para, amt_para, chg_para])
                rest_table = Table(rest_data, colWidths=[26, CONTENT_W - 26 - 130 - 90, 130, 90])
                rest_table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, _rl_color("#E7EBEF")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ]))
                story.append(rest_table)

            top_total = sum(r["total"] for r in top_rows)
            pct_of_total = (top_total / ov["total"] * 100.0) if ov["total"] else 0.0
            pct_of_roster = (len(top_rows) / ov["agents"] * 100.0) if ov["agents"] else 0.0
            watch_line = ("Worth a standing watch: if that share keeps climbing, payout is riding on fewer people."
                          if pct_of_total >= 30 else
                          "A broad base right now &mdash; payout isn't concentrated in a small group.")
            _add_note_paragraphs([
                f"<b>Concentration.</b> The top {len(top_rows)} agents ({pct_of_roster:.0f}% of the active roster) "
                f"generated RM {top_total:,.2f} &mdash; {pct_of_total:.0f}% of all Basic+NFP+ANP commission paid this month.",
                watch_line,
            ])
        story.append(PageBreak())

        # --- Total Commission Payout by Type ---
        story.append(PageTracker("payout_type", page_registry))
        story.append(_band_header("Overview", "Total Commission Payout by Type",
                                   f"All Agents combined &middot; {period_label}"))
        _spacer(16)

        prod_bonus_total = float(report_overview.get("prod_bonus_total", 0.0))
        team_bonus_total = float(report_overview.get("team_bonus_total", 0.0))
        ega_eligible = int(report_overview.get("ega_eligible", 0))
        grand_all_types = ov["total"] + prod_bonus_total + team_bonus_total

        segs = [
            ("Basic Commission", ov["basic"], "#0E7C66"),
            ("NFP Commission", ov["nfp"], "#1B9A81"),
            ("ANP Commission", ov["anp"], "#33B79B"),
            ("Production Bonus", prod_bonus_total, "#5CCEB4"),
            ("Team Championship / Achievement", team_bonus_total, "#93E1CC"),
        ]
        # A single stacked bar instead of a donut -- reads left-to-right and
        # scales to 5+ categories far better than a thin ring of slices does.
        story.append(_proportion_bar(segs, CONTENT_W, height=34))
        _spacer(18)

        legend_cells = []
        for name, val, color in segs:
            pct = (val / grand_all_types * 100.0) if grand_all_types else 0.0
            legend_cells.append([
                Paragraph(name, type_item_name_style),
                Spacer(1, 3),
                Paragraph(f"RM {val:,.2f}", type_item_amt_style),
                Paragraph(f"{pct:.1f}%", type_item_pct_style),
            ])
        if ega_eligible:
            legend_cells.append([
                Paragraph("EGA / ESA Awards", ParagraphStyle("EgaName", parent=type_item_name_style, textColor=_rl_color("#8B95A3"))),
                Spacer(1, 3),
                Paragraph(f"{ega_eligible} eligible", ParagraphStyle("EgaAmt", parent=type_item_amt_style, textColor=_rl_color("#8B95A3"))),
                Paragraph("non-cash award", type_item_pct_style),
            ])
        legend_colors = [c for _, _, c in segs] + (["#E7EBEF"] if ega_eligible else [])
        legend_row1 = legend_cells[:3]
        legend_row2 = legend_cells[3:]
        legend_grid_rows = [legend_row1]
        legend_border_row1 = [("LINEABOVE", (i, 0), (i, 0), 2.5, _rl_color(legend_colors[i])) for i in range(len(legend_row1))]
        border_styles = list(legend_border_row1)
        if legend_row2:
            legend_grid_rows.append(legend_row2 + [[Paragraph("", type_item_name_style)]] * (3 - len(legend_row2)))
            border_styles += [("LINEABOVE", (i, 1), (i, 1), 2.5, _rl_color(legend_colors[3 + i]))
                              for i in range(len(legend_row2))]
        legend_grid = Table(legend_grid_rows, colWidths=[CONTENT_W / 3.0] * 3)
        legend_grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ] + border_styles))
        story.append(legend_grid)

        grand_line = Table([[Paragraph("GRAND TOTAL, ALL TYPES", grand_label_style),
                             Paragraph(f"RM {grand_all_types:,.2f}", grand_value_style)]],
                           colWidths=[CONTENT_W - 200, 200])
        grand_line.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEABOVE", (0, 0), (-1, 0), 1.2, _rl_color("#141A1F")),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(grand_line)
        _payout_notes = [
            "The Executive Summary total covers commission proper (Basic + NFP + ANP). "
            "This page adds Production Bonus and Team Championship / Achievement Bonus for the full payout picture.",
            "Production Bonus accumulates from January through the report month (year-to-date).",
        ]
        if ega_eligible:
            _payout_notes.append(
                f"EGA / ESA Awards: {ega_eligible} agent{'s' if ega_eligible != 1 else ''} currently eligible. "
                "These are non-cash awards (recognition trips), so they carry no ringgit value in this chart.")
        _add_note_paragraphs(_payout_notes)
        story.append(PageBreak())

    # ----------------------------------------------------------------
    # Commission Detail: Summary Agent Commission (all agents, unified)
    #
    # The per-invoice/per-customer ledgers (dates, rates, overrides, referral
    # fees) were removed from the PDF on request -- the Download Excel export
    # still carries all of them for reconciliation.
    # ----------------------------------------------------------------
    story.append(PageTracker("agent_summary", page_registry))
    _asc_sub = ""
    if report_overview is not None and report_overview.get("unified_current"):
        _asc_sub = (f"All {len(report_overview['unified_current'])} agents, ranked by total commission "
                   f"(Basic + NFP + ANP) &middot; {report_overview.get('period_label', '')}")
    story.append(_band_header("Commission Detail", "Summary Agent Commission", _asc_sub))
    _spacer(14)
    if report_overview is not None and report_overview.get("unified_current"):
        unified = report_overview["unified_current"]
        ov_det = report_overview["overview_current"]

        det_rows = [[
            Paragraph("AGENT", ledger_hdr_style),
            Paragraph("BASIC (RM)", ledger_hdr_right_style),
            Paragraph("NFP (RM)", ledger_hdr_right_style),
            Paragraph("ANP (RM)", ledger_hdr_right_style),
            Paragraph("TOTAL (RM)", ledger_hdr_right_style),
        ]]
        for r in unified:
            tag_color = "#0B6350" if r["type"] == "Internal" else "#B54A1D"
            tag_label = "INT" if r["type"] == "Internal" else "OUT"
            det_rows.append([
                Paragraph(f'<font color="{tag_color}"><b>[{tag_label}]</b></font>&nbsp;&nbsp;{r["agent"]}', ledger_row_style),
                Paragraph(f'{r["basic"]:,.2f}' if r["basic"] else "&ndash;", ledger_num_style),
                Paragraph(f'{r["nfp"]:,.2f}' if r["nfp"] else "&ndash;", ledger_num_style),
                Paragraph(f'{r["anp"]:,.2f}' if r["anp"] else "&ndash;", ledger_num_style),
                Paragraph(f'{r["total"]:,.2f}', ledger_tot_style),
            ])
        det_rows.append([
            Paragraph("GRAND TOTAL", ledger_grand_label_style),
            Paragraph(f'{ov_det["basic"]:,.2f}', ledger_grand_style),
            Paragraph(f'{ov_det["nfp"]:,.2f}', ledger_grand_style),
            Paragraph(f'{ov_det["anp"]:,.2f}', ledger_grand_style),
            Paragraph(f'{ov_det["total"]:,.2f}', ledger_grand_style),
        ])

        num_w = 110
        det_tbl = Table(det_rows, colWidths=[CONTENT_W - 4 * num_w, num_w, num_w, num_w, num_w],
                        repeatRows=1, splitByRow=1)
        det_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, _rl_color("#141A1F")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, _rl_color("#E7EBEF")),
            ("LINEABOVE", (0, -1), (-1, -1), 1.2, _rl_color("#141A1F")),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(det_tbl)
        _add_note_paragraphs([
            "This page totals commission per agent. Invoice-level detail (dates, rates, "
            "overrides, referral fees) is available in the Excel export from the dashboard.",
        ])
    else:
        story.append(Paragraph("No commission data available for this period.", body_style))
    story.append(PageBreak())

    # ----------------------------------------------------------------
    # Build PDF
    # ----------------------------------------------------------------
    print(f"  Building PDF with {len(story)} elements...")
    doc.build(story)
    return output_path


def build_commission_excel(
    output_path: Path,
    year: int,
    month: int | None = None,
    *,
    invoice_dates_map: dict = None,
    int_agent_summary_rows,
    int_customer_summary_rows,
    int_customer_anp_rows,
    out_agent_summary_rows,
    out_customer_summary_rows,
    int_ega_t1,
    int_ega_h1,
    int_ega_t2,
    int_ega_t3,
    int_ega_h3,
    out_ega_t1,
    out_ega_h1,
    out_ega_t2,
    out_ega_t3,
    out_ega_h3,
    out_prod_data,
    contest_t1_headers=None, contest_t1_rows=None,
    contest_t2_headers=None, contest_t2_rows=None,
    contest_t3_headers=None, contest_t3_rows=None,
) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    
    font_name = "Segoe UI"
    title_fill = PatternFill("solid", fgColor="1A365D")  # Navy
    header_fill = PatternFill("solid", fgColor="2F5597")  # Lighter navy
    total_fill = PatternFill("solid", fgColor="F1F5F9")  # Soft gray-blue
    
    title_font = Font(name=font_name, size=11, bold=True, color="FFFFFF")
    header_font = Font(name=font_name, size=9.5, bold=True, color="FFFFFF")
    data_font = Font(name=font_name, size=9.5, bold=False, color="000000")
    total_font = Font(name=font_name, size=9.5, bold=True, color="000000")
    
    thin_border_side = Side(style="thin", color="CCCCCC")
    border_all = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    double_bottom_side = Side(style="double", color="000000")
    total_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=double_bottom_side)

    # Clean and Format Cell
    def clean_and_format_cell(cell, val, header_name, is_total_row=False):
        cell.font = total_font if is_total_row else data_font
        cell.border = total_border if is_total_row else border_all
        if is_total_row:
            cell.fill = total_fill

        if isinstance(val, str):
            val = re.sub(r'<[^>]*>', ' ', val).strip()

        # Agent columns: display the canonical full name (Title Case).
        if isinstance(val, str) and val and val != "-" and is_agent_header(header_name):
            cell.value = to_full_name(val)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            return

        # If it's a date or invoice/number column, keep as text and center
        if isinstance(val, str) and any(w in header_name.lower() for w in ["date", "invoice", "number", "no."]):
            cell.value = val
            cell.alignment = Alignment(horizontal="center", vertical="center")
            return

        # 1. Parse currency or generic number strings
        if isinstance(val, str):
            cleaned = val.replace("RM", "").replace(",", "").strip()
            if re.match(r'^[-+]?\d+\.\d+$', cleaned):
                try:
                    cell.value = float(cleaned)
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                    return
                except ValueError:
                    pass
            elif re.match(r'^[-+]?\d+$', cleaned):
                try:
                    cell.value = int(cleaned)
                    cell.number_format = '#,##0'
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                    return
                except ValueError:
                    pass

        # 2. Percentage check
        if isinstance(val, str) and val.endswith("%"):
            try:
                num = float(val.replace("%", "").strip()) / 100.0
                cell.value = num
                cell.number_format = '0.00%'
                cell.alignment = Alignment(horizontal="center", vertical="center")
                return
            except ValueError:
                pass

        # 3. Direct float/int/Decimal check
        if isinstance(val, (int, float, Decimal)):
            cell.value = float(val)
            if any(w in header_name.lower() for w in ["price", "sales", "fee", "safwan", "override", "payout", "bonus", "amount", "total", "commission"]):
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif "rate" in header_name.lower() or "%" in header_name.lower():
                if float(val) <= 1.0:
                    cell.number_format = '0.00%'
                else:
                    cell.number_format = '0.00'
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            return

        # Fallback to text
        cell.value = val
        if val == "-":
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif any(w in header_name.lower() for w in ["date", "invoice", "payment", "number", "no."]):
            cell.alignment = Alignment(horizontal="center", vertical="center")
        else:
            cell.alignment = Alignment(horizontal="left", vertical="center")

    def write_block(ws, start_row, title, headers, rows):
        ws.row_dimensions[start_row].height = 24
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=start_row, column=col_idx)
            c.border = border_all
            c.fill = title_fill
            if col_idx == 1:
                c.value = title
                c.font = title_font
                c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=len(headers))
        start_row += 1
        
        ws.row_dimensions[start_row].height = 20
        for col_idx, h in enumerate(headers, start=1):
            c = ws.cell(row=start_row, column=col_idx, value=h)
            c.font = header_font
            c.fill = header_fill
            c.border = border_all
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        start_row += 1
        
        if not rows:
            ws.row_dimensions[start_row].height = 18
            c = ws.cell(row=start_row, column=1, value="No data available")
            c.font = data_font
            c.border = border_all
            c.alignment = Alignment(horizontal="center", vertical="center")
            for col_idx in range(2, len(headers) + 1):
                ws.cell(row=start_row, column=col_idx).border = border_all
            ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=len(headers))
            start_row += 2
            return start_row

        lower_hdrs = [h.lower().strip() for h in headers]
        try:
            sys_col = lower_hdrs.index("system price") + 1
            try:
                nfp_col = lower_hdrs.index("net floor price") + 1
            except ValueError:
                nfp_col = lower_hdrs.index("netfloor price") + 1
        except ValueError:
            sys_col = None
            nfp_col = None

        for r in rows:
            ws.row_dimensions[start_row].height = 18
            first_val = str(r[0]).strip().lower() if r and len(r) > 0 else ""
            is_total = "total" in first_val or "grand total" in first_val
            
            for col_idx, val in enumerate(r):
                if col_idx < len(headers):
                    c = ws.cell(row=start_row, column=col_idx + 1)
                    clean_and_format_cell(c, val, headers[col_idx], is_total_row=is_total)
                    if headers[col_idx].strip().lower() == "gan lai soon":
                        c.alignment = Alignment(horizontal="center", vertical="center")

            if sys_col and nfp_col:
                val1 = str(ws.cell(row=start_row, column=sys_col).value).strip()
                val2 = str(ws.cell(row=start_row, column=nfp_col).value).strip()
                if val1 == val2 and val1:
                    ws.merge_cells(start_row=start_row, start_column=sys_col, end_row=start_row, end_column=nfp_col)
            
            start_row += 1
        
        ws.row_dimensions[start_row].height = 18
        start_row += 1
        return start_row

    def autofit_cols(ws):
        ws.views.sheetView[0].showGridLines = True
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val_str = str(cell.value or '')
                val_str = re.sub(r'<[^>]*>', '', val_str)
                if len(val_str) > 50:
                    continue
                if len(val_str) > max_len:
                    max_len = len(val_str)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    MONTH_NAMES = {
        1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
        7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"
    }
    months_to_export = [month] if month is not None else list(range(1, 13))

    # 1. Int Basic & NFP by Customer
    ws = wb.active
    ws.title = "Int Basic & NFP by Customer"
    int_cust_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "Senior Override", "Referral Name", "Referral Fee", "Safwan (RM)"]
    int_cust_headers_july = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "Senior Override", "Referral Name", "Referral Fee", "Safwan (RM)"]
    curr_row = 1
    for m in months_to_export:
        rows = int_customer_summary_rows.get(m, [])
        if rows:
            curr_row = write_block(ws, curr_row, f"{MONTH_NAMES[m]} 2026 - Summary Internal Agent Commission by Customer", int_cust_headers_july if m >= 7 else int_cust_headers, rows)
    if curr_row == 1:
        curr_row = write_block(ws, curr_row, "Summary Internal Agent Commission by Customer", int_cust_headers, [])
    autofit_cols(ws)

    # 2. Int ANP Details
    ws = wb.create_sheet("Int ANP Details")
    int_anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Sales Price", "Commission Price", "Clawback"]
    curr_row = 1
    for m in months_to_export:
        rows = int_customer_anp_rows.get(m, []) if int_customer_anp_rows else []
        if rows:
            curr_row = write_block(ws, curr_row, f"{MONTH_NAMES[m]} 2026 - ANP Commission by Customer", int_anp_headers, rows)
    if curr_row == 1:
        curr_row = write_block(ws, curr_row, "ANP Commission by Customer", int_anp_headers, [])
    autofit_cols(ws)

    # 3. Out Basic & NFP by Customer
    ws = wb.create_sheet("Out Basic & NFP by Customer")
    out_cust_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "Senior Override", "Referral Name", "Referral Fee", "Safwan (RM)", "Gan Lai Soon"]
    out_cust_headers_july = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "Senior Override", "Referral Name", "Referral Fee", "Safwan (RM)", "Gan Lai Soon"]
    curr_row = 1
    for m in months_to_export:
        rows = out_customer_summary_rows.get(m, [])
        if rows:
            local_headers = list(out_cust_headers_july if m >= 7 else out_cust_headers)
            _pkg_idx = 6 if m >= 7 else 5
            local_rows = [list(r) for r in rows]
            has_factory = any("factory" in str(r[_pkg_idx]).lower() for r in local_rows if len(r) > _pkg_idx)
            if not has_factory:
                lower_hdrs = [h.lower().strip() for h in local_headers]
                if "safwan (rm)" in lower_hdrs:
                    safwan_idx = lower_hdrs.index("safwan (rm)")
                    local_headers.pop(safwan_idx)
                    for r in local_rows:
                        if len(r) > safwan_idx:
                            r.pop(safwan_idx)
            curr_row = write_block(ws, curr_row, f"{MONTH_NAMES[m]} 2026 - Summary Outsource Agent Commission by Customer", local_headers, local_rows)
    if curr_row == 1:
        curr_row = write_block(ws, curr_row, "Summary Outsource Agent Commission by Customer", out_cust_headers, [])
    autofit_cols(ws)

    # 4. Int EGA ESA Details
    ws = wb.create_sheet("Int EGA ESA Details")
    curr_row = 1
    curr_row = write_block(ws, curr_row, "Internal EGA / ESA Awards - Summary by Agent", int_ega_h1 or ["Agent", "EP Point Status", "Qualifying Status"], int_ega_t1)
    _int_ega_all = (int_ega_t2 or []) + (int_ega_t3 or [])
    _int_ega_ytd_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package", "Sales Price", "Accumulated EP", "Eligibility"]
    _int_ega_ytd_rows = []
    if _int_ega_all:
        for _er in sorted(_int_ega_all, key=lambda x: (str(x[0]).lower() if x else "", str(x[4]) if len(x) > 4 else "")):
            if len(_er) >= 8:
                inv_key = str(_er[2]).strip()
                dates = invoice_dates_map.get(inv_key) if invoice_dates_map else None
                inv_date = dates[0] if dates else str(_er[4]).strip()
                first_pay_dt = dates[1] if dates else ""
                _int_ega_ytd_rows.append([
                    to_title_case(str(_er[0]).strip()),
                    to_title_case(str(_er[1]).strip()),
                    inv_date,
                    first_pay_dt,
                    str(_er[3]).strip(),
                    str(_er[5]).strip(),
                    str(_er[6]).strip(),
                    str(_er[7]).strip(),
                ])
    curr_row = write_block(ws, curr_row, "Internal EGA / ESA Awards (YTD) - Detail by Customer", _int_ega_ytd_headers, _int_ega_ytd_rows)
    autofit_cols(ws)

    # 5. Out EGA ESA Details
    ws = wb.create_sheet("Out EGA ESA Details")
    curr_row = 1
    curr_row = write_block(ws, curr_row, "Outsource EGA / ESA Awards - Summary by Agent", out_ega_h1 or ["Agent", "EP Point Status", "Qualifying Status"], out_ega_t1)
    _out_ega_all = (out_ega_t2 or []) + (out_ega_t3 or [])
    _out_ega_ytd_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package", "Sales Price", "Accumulated EP", "Eligibility"]
    _out_ega_ytd_rows = []
    if _out_ega_all:
        for _er in sorted(_out_ega_all, key=lambda x: (str(x[0]).lower() if x else "", str(x[4]) if len(x) > 4 else "")):
            if len(_er) >= 8:
                inv_key = str(_er[2]).strip()
                dates = invoice_dates_map.get(inv_key) if invoice_dates_map else None
                inv_date = dates[0] if dates else str(_er[4]).strip()
                first_pay_dt = dates[1] if dates else ""
                _out_ega_ytd_rows.append([
                    to_title_case(str(_er[0]).strip()),
                    to_title_case(str(_er[1]).strip()),
                    inv_date,
                    first_pay_dt,
                    str(_er[3]).strip(),
                    str(_er[5]).strip(),
                    str(_er[6]).strip(),
                    str(_er[7]).strip(),
                ])
    curr_row = write_block(ws, curr_row, "Outsource EGA / ESA Awards (YTD) - Detail by Customer", _out_ega_ytd_headers, _out_ega_ytd_rows)
    autofit_cols(ws)

    # 6. Outsource Production Bonus
    ws = wb.create_sheet("Outsource Production Bonus")
    curr_row = 1
    if out_prod_data:
        oum_summary = out_prod_data.get("oum_summary", [])
        ogm_summary = out_prod_data.get("ogm_summary", [])
        team_detail = out_prod_data.get("team_detail", [])
        pb_headers = out_prod_data.get("headers", {})

        curr_row = write_block(ws, curr_row, "Summary of OUM Bonus", pb_headers.get("oum", ["Agent", "Total Sales", "Status", "Bonus Amount"]), oum_summary)
        curr_row = write_block(ws, curr_row, "Production Bonus — OGM Summary", pb_headers.get("ogm", ["Agent", "Total Sales", "Status", "Bonus Amount"]), ogm_summary)
        curr_row = write_block(ws, curr_row, "Summary of OUM Bonus by Customer", pb_headers.get("detail", ["Agent", "Customer", "Sales Price"]), team_detail)
    else:
        curr_row = write_block(ws, curr_row, "Summary of OUM Bonus", ["Agent", "Total Sales", "Status", "Bonus Amount"], [])
        curr_row = write_block(ws, curr_row, "Production Bonus — OGM Summary", ["Agent", "Total Sales", "Status", "Bonus Amount"], [])
        curr_row = write_block(ws, curr_row, "Summary of OUM Bonus by Customer", ["Agent", "Customer", "Sales Price"], [])
    autofit_cols(ws)

    # 7. June Monthly Contest
    ws = wb.create_sheet("Team Championship")
    curr_row = 1
    curr_row = write_block(ws, curr_row, "Team Championship and Team Achievement Bonus",
                            contest_t1_headers or ["Team Name", "Accumulated System Price (RM)", "Accumulated Sales Price (RM)", "Rank", "Team Ranking Award (RM)", "Team Achievement Bonus (RM)"],
                            contest_t1_rows or [])
    autofit_cols(ws)

    ws = wb.create_sheet("Cases and Awards by Agent")
    curr_row = 1
    curr_row = write_block(ws, curr_row, "Summary of Cases and Awards by Agent",
                            contest_t3_headers or ["Agent Name", "Team Name", "Customer Name", "Invoice Date", "1st Payment Date", "System Price (RM)", "Sales Price (RM)", "Golden Boot Award (Top Individual Performance) (RM)", "Fast Start Award"],
                            contest_t3_rows or [])
    autofit_cols(ws)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def build_pdf(year: int, output_path: Path, month: int | None = None, may_only: bool = False) -> Path:
    print(f"\nBuilding Commission PDF for year {year}...")
    # Resolve proxy credentials once, up front: _resolve_proxy_credentials()
    # pops/re-sets PG_PROXY_TOKEN in os.environ, so letting the concurrent
    # fetches below each resolve on their own thread can race and make one of
    # them see "missing token" mid-swap.
    _resolve_proxy_credentials()

    # Month packs fetch the FULL year and let build_*_summary_tables() bucket
    # rows into the requested month (same as the web dashboard). Pre-filtering
    # by real_full_payment_date/full_payment_date drops July-2026-policy
    # invoices, which are recognized by their 5%/75% payment-milestone dates.
    h1_only = month is None

    # --- Fetch all data concurrently ---
    # All fetches below are independent network round-trips to the same PG proxy
    # (previously ~65s run sequentially). Running them concurrently cuts total
    # fetch time down to roughly the slowest single call.
    from concurrent.futures import ThreadPoolExecutor
    ega_upto_month = month if month is not None else (5 if may_only else None)

    print("\nFetching all commission data concurrently...")
    with ThreadPoolExecutor(max_workers=10) as _executor:
        _f_int_basic = _executor.submit(fetch_internal_basic, year, h1_only=h1_only)
        _f_int_anp = _executor.submit(fetch_internal_anp, year, h1_only=h1_only)
        _f_int_nfp = _executor.submit(fetch_internal_nfp, year, h1_only=h1_only)
        _f_int_ega = _executor.submit(fetch_internal_ega_esa, year, upto_month=ega_upto_month)
        _f_out_basic = _executor.submit(fetch_outsource_basic, year, h1_only=h1_only)
        _f_out_anp = _executor.submit(fetch_outsource_anp, year, h1_only=h1_only)
        _f_out_nfp = _executor.submit(fetch_outsource_nfp, year, h1_only=h1_only)
        _f_out_ega = _executor.submit(fetch_outsource_ega_esa, year, upto_month=ega_upto_month)
        _f_prod = _executor.submit(fetch_production_bonus, year)
        _f_contest = _executor.submit(fetch_monthly_contest, month, year)

        print("  Fetching Basic Commission (Internal)...")
        int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = _f_int_basic.result()

        print("  Fetching ANP Commission (Internal)...")
        int_anp_summary, int_anp_detail, int_anp_meta = _f_int_anp.result()

        print("  Fetching NFP Commission (Internal)...")
        int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = _f_int_nfp.result()
        print(f"    {int_nfp_meta['agents']} agents, {int_nfp_meta['invoices']} invoices")

        print("  Fetching EGA/ESA Awards (Internal)...")
        try:
            int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3 = _f_int_ega.result()
            print(f"    {len(int_ega_t1)} rows")
        except Exception as e:
            print(f"    Warning: {e}")
            int_ega_t1, int_ega_t2, int_ega_t3 = [], [], []
            int_ega_h1, int_ega_h2, int_ega_h3 = [], [], []

        print("  Fetching Basic Commission (Outsource)...")
        out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = _f_out_basic.result()
        print(f"    {out_basic_meta['agents']} agents, {out_basic_meta['invoices']} invoices")

        print("  Fetching ANP Commission (Outsource)...")
        try:
            out_anp_summary, out_anp_detail, out_anp_meta = _f_out_anp.result()
            print(f"    {out_anp_meta['agents']} agents, {out_anp_meta['invoices']} invoices")
        except Exception as e:
            print(f"    Warning: {e}")
            out_anp_summary, out_anp_detail = [], []
            out_anp_meta = {"agents": 0, "invoices": 0, "total_commission": Decimal("0"), "filter": "error"}

        print("  Fetching NFP Commission (Outsource)...")
        try:
            out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = _f_out_nfp.result()
            print(f"    {out_nfp_meta['agents']} agents, {out_nfp_meta['invoices']} invoices")
        except Exception as e:
            print(f"    Warning: {e}")
            out_nfp_agent, out_nfp_detail, out_nfp_rows = [], [], []
            out_nfp_by_inv_all = {}
            out_nfp_meta = {"agents": 0, "invoices": 0, "total_commission": Decimal("0"), "filter": "error"}

        print("  Fetching EGA/ESA Awards (Outsource)...")
        try:
            out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3 = _f_out_ega.result()
            print(f"    {len(out_ega_t1)} rows")
        except Exception as e:
            print(f"    Warning: {e}")
            out_ega_t1, out_ega_t2, out_ega_t3 = [], [], []
            out_ega_h1, out_ega_h2, out_ega_h3 = [], [], []

        print("  Fetching Production Bonus...")
        out_prod_data = _f_prod.result()
        if out_prod_data:
            print(f"    OUM: {len(out_prod_data.get('oum_summary', []))} rows")
        else:
            print("    No data")

        print("  Fetching Monthly Contest...")
        (contest_t1_headers, contest_t1_rows, contest_t2_headers, contest_t2_rows,
         contest_t3_headers, contest_t3_rows) = _f_contest.result()

    # Build internal summary tables
    global invoice_package_map
    invoice_package_map.clear()
    for ln in int_basic_lines:
        inv_num = str(getattr(ln, "invoice_number", "")).strip()
        if inv_num:
            invoice_package_map[inv_num] = get_invoice_package(inv_num, ln)

    print("  Building internal summary tables...")
    int_anp_summary_for_table = []
    if "int_anp_commission" in sys.modules:
        anp_mod = sys.modules["int_anp_commission"]
        if hasattr(anp_mod, "summary_table_rows"):
            int_anp_summary_for_table = anp_mod.summary_table_rows(int_anp_summary)

    basic_mod = sys.modules.get("int_basic_commission")
    if not basic_mod:
        basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
        basic_mod = _load_module("int_basic_commission", basic_path)
    invoice_dates_map = fetch_invoice_dates(year, basic_mod)

    # Full-year copy kept aside before the month filter below mutates
    # int_anp_detail -- the Top Performers "vs last month" comparison needs
    # the previous month's rows too, and re-fetching just for that would cost
    # another network round-trip for no reason (the data's already here).
    int_anp_detail_full = list(int_anp_detail)

    # If a specific month is requested, keep ANP detail for that month only and
    # re-scope the highlight metas. Basic/NFP lines intentionally stay
    # full-year: build_internal_summary_tables() buckets them per month via
    # _expand_basic_lines_for_month() (payment milestones), so pre-filtering by
    # full_payment_date here would empty the July-policy months.
    if month is not None:
        int_anp_detail = [r for r in int_anp_detail if _parse_month(r.get("invoice_date")) == month]
        _m_lines = _expand_basic_lines_for_month(int_basic_lines, month)
        _m_nfp = [r for r in int_nfp_rows if _parse_month(_nfp_payout_date(r)) == month]
        int_basic_meta = {**int_basic_meta,
                          "agents": len({ln.agent_name.strip() for ln in _m_lines}),
                          "invoices": len(_m_lines),
                          "total_commission": Decimal(str(round(sum(float(ln.basic_commission) for ln in _m_lines), 2)))}
        int_nfp_meta = {**int_nfp_meta,
                        "agents": len({r.agent_name.strip() for r in _m_nfp if r.agent_name}),
                        "invoices": len(_m_nfp),
                        "total_commission": Decimal(str(round(sum(float(r.nfp_commission) for r in _m_nfp), 2)))}

    int_agent_summary_rows, int_customer_summary_rows, int_agent_anp_rows, int_customer_anp_rows, int_agent_totals_by_month = build_internal_summary_tables(
        basic_t1=int_basic_t1,
        basic_lines=int_basic_lines,
        basic_t4=int_basic_t4,
        nfp_agent_rows=int_nfp_agent,
        nfp_rows=int_nfp_rows,
        nfp_by_inv_all=int_nfp_by_inv_all,
        anp_summary_rows=int_anp_summary_for_table,
        anp_detail=int_anp_detail,
        year=year,
        invoice_dates_map=invoice_dates_map,
        month=month,
    )
    print(f"    Agent summary: {len(int_agent_summary_rows)} rows")
    print(f"    Customer summary: {len(int_customer_summary_rows)} rows")
    print("\n[Outsource]")

    # Populate outsource package mapping before filtering
    for ln in out_basic_lines:
        inv_num = str(getattr(ln, "invoice_number", "")).strip()
        if inv_num:
            invoice_package_map[inv_num] = get_invoice_package(inv_num, ln)

    # Same reasoning as int_anp_detail_full above.
    out_anp_detail_full = list(out_anp_detail)

    # Same month handling as the internal side above: only ANP detail is
    # pre-filtered; Basic/NFP lines stay full-year for milestone bucketing.
    if month is not None:
        out_anp_detail = [r for r in out_anp_detail if _parse_month(r.get("invoice_date")) == month]
        _m_out_lines = _expand_basic_lines_for_month(out_basic_lines, month)
        _m_out_nfp = [r for r in out_nfp_rows if _parse_month(_nfp_payout_date(r)) == month]
        out_basic_meta = {**out_basic_meta,
                          "agents": len({ln.agent_name.strip() for ln in _m_out_lines}),
                          "invoices": len(_m_out_lines),
                          "total_commission": Decimal(str(round(sum(float(ln.basic_commission) for ln in _m_out_lines), 2)))}
        out_nfp_meta = {**out_nfp_meta,
                        "agents": len({r.agent_name.strip() for r in _m_out_nfp if r.agent_name}),
                        "invoices": len(_m_out_nfp),
                        "total_commission": Decimal(str(round(sum(float(r.nfp_commission) for r in _m_out_nfp), 2)))}

    # Build outsource summary tables

    print("  Building outsource summary tables...")
    out_agent_summary_rows, out_customer_summary_rows, out_customer_anp_rows, out_agent_totals_by_month = build_outsource_summary_tables(
        basic_t1=out_basic_t1,
        basic_lines=out_basic_lines,
        basic_meta=out_basic_meta,
        nfp_agent_rows=out_nfp_agent,
        nfp_rows=out_nfp_rows,
        nfp_by_inv_all=out_nfp_by_inv_all,
        anp_summary_rows=[],
        anp_detail=out_anp_detail,
        year=year,
        invoice_dates_map=invoice_dates_map,
        month=month,
    )
    print(f"    Agent summary: {len(out_agent_summary_rows)} rows")
    print(f"    Customer summary: {len(out_customer_summary_rows)} rows")

    # ----------------------------------------------------------------
    # Unified (Internal + Outsource combined) report overview: Executive
    # Summary totals + Effective Commission Rate, the Top Performers &
    # Concentration ranking, the Payout by Type donut, and the unified
    # Summary Agent Commission page. For the H1/no-month pack the totals
    # aggregate across every month present (no prior-period deltas).
    # ----------------------------------------------------------------
    _MN = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
           7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"}

    # Cash bonus totals for the Payout by Type page. EGA/ESA is a non-cash
    # award (eligibility for a recognition trip), so it contributes a count,
    # not a ringgit segment.
    def _sum_last_money_col(rows: list) -> float:
        total = 0.0
        for r in rows or []:
            if not r or str(r[0]).strip().lower().startswith(("total", "grand")):
                continue
            total += float(_parse_rm(r[-1]))
        return total

    prod_bonus_total = 0.0
    if out_prod_data:
        prod_bonus_total = (_sum_last_money_col(out_prod_data.get("oum_summary"))
                            + _sum_last_money_col(out_prod_data.get("ogm_summary")))

    team_bonus_total = 0.0
    if contest_t1_headers and contest_t1_rows:
        _lc_hdrs = [str(h).strip().lower() for h in contest_t1_headers]
        _bonus_cols = [i for i, h in enumerate(_lc_hdrs)
                       if "award (rm)" in h or "bonus (rm)" in h]
        for r in contest_t1_rows:
            if not r or str(r[0]).strip().lower().startswith(("total", "grand")):
                continue
            for i in _bonus_cols:
                if i < len(r):
                    team_bonus_total += float(_parse_rm(r[i]))

    ega_eligible = sum(
        1 for r in (list(int_ega_t1 or []) + list(out_ega_t1 or []))
        if r and len(r) >= 4 and str(r[3]).strip() not in ("", "-")
    )

    print("  Building unified report overview (all agents, Basic+NFP+ANP)...")
    if month is not None:
        out_anp_by_agent = _max_anp_by_agent_for_month(out_anp_detail_full, month)
        unified_current = _unified_agent_totals(
            int_agent_totals_by_month.get(month, {}),
            out_agent_totals_by_month.get(month, {}),
            out_anp_by_agent,
        )
        period_label = f"{_MN[month]} {year}"
    else:
        # H1/no-month pack: aggregate the per-month unified totals per agent.
        _acc: dict[tuple[str, str], dict] = {}
        _months = sorted(set(int_agent_totals_by_month) | set(out_agent_totals_by_month))
        for _m in _months:
            for r in _unified_agent_totals(
                    int_agent_totals_by_month.get(_m, {}),
                    out_agent_totals_by_month.get(_m, {}),
                    _max_anp_by_agent_for_month(out_anp_detail_full, _m)):
                a = _acc.setdefault((r["agent"], r["type"]), {
                    "agent": r["agent"], "type": r["type"],
                    "basic": 0.0, "nfp": 0.0, "anp": 0.0,
                    "sales": 0.0, "invoices": 0, "total": 0.0,
                })
                for k in ("basic", "nfp", "anp", "sales", "invoices", "total"):
                    a[k] += r[k]
        unified_current = sorted(_acc.values(), key=lambda r: r["total"], reverse=True)
        period_label = (f"{_MN[_months[0]]} to {_MN[_months[-1]]} {year}"
                        if _months else f"{year}")
    overview_current = _report_overview_totals(unified_current)

    prev_month = (month - 1 if month is not None and month > 1 else None)
    unified_prev: list[dict] = []
    overview_prev = None
    if prev_month is not None:
        _, _, _, _, int_totals_prev = build_internal_summary_tables(
            basic_t1=int_basic_t1,
            basic_lines=int_basic_lines,
            basic_t4=int_basic_t4,
            nfp_agent_rows=int_nfp_agent,
            nfp_rows=int_nfp_rows,
            nfp_by_inv_all=int_nfp_by_inv_all,
            anp_summary_rows=int_anp_summary_for_table,
            anp_detail=int_anp_detail_full,
            year=year,
            invoice_dates_map=invoice_dates_map,
            month=prev_month,
        )
        _, _, _, out_totals_prev = build_outsource_summary_tables(
            basic_t1=out_basic_t1,
            basic_lines=out_basic_lines,
            basic_meta=out_basic_meta,
            nfp_agent_rows=out_nfp_agent,
            nfp_rows=out_nfp_rows,
            nfp_by_inv_all=out_nfp_by_inv_all,
            anp_summary_rows=[],
            anp_detail=out_anp_detail_full,
            year=year,
            invoice_dates_map=invoice_dates_map,
            month=prev_month,
        )
        out_anp_by_agent_prev = _max_anp_by_agent_for_month(out_anp_detail_full, prev_month)
        unified_prev = _unified_agent_totals(
            int_totals_prev.get(prev_month, {}),
            out_totals_prev.get(prev_month, {}),
            out_anp_by_agent_prev,
        )
        overview_prev = _report_overview_totals(unified_prev)

    report_overview = {
        "month": month,
        "prev_month": prev_month,
        "period_label": period_label,
        "prev_period_label": (f"{_MN[prev_month]} {year}" if prev_month is not None else None),
        "unified_current": unified_current,
        "overview_current": overview_current,
        "unified_prev": unified_prev,
        "overview_prev": overview_prev,
        "rank_changes": _rank_changes(unified_current, unified_prev),
        "prod_bonus_total": prod_bonus_total,
        "team_bonus_total": team_bonus_total,
        "ega_eligible": ega_eligible,
    }
    print(f"    {len(unified_current)} agents ranked, "
          f"RM {overview_current['total']:,.2f} total commission"
          + (f", vs RM {overview_prev['total']:,.2f} last month" if overview_prev else "")
          + f"; bonus: prod RM {prod_bonus_total:,.2f}, team RM {team_bonus_total:,.2f}, "
          + f"EGA/ESA eligible: {ega_eligible}")

    # --- Build Excel or PDF ---
    if str(output_path).lower().endswith(".xlsx"):
        print(f"  Exporting commission pack to Excel: {output_path}")
        return build_commission_excel(
            output_path=output_path,
            year=year,
            month=month,
            invoice_dates_map=invoice_dates_map,
            int_agent_summary_rows=int_agent_summary_rows,
            int_customer_summary_rows=int_customer_summary_rows,
            int_customer_anp_rows=int_customer_anp_rows,
            out_agent_summary_rows=out_agent_summary_rows,
            out_customer_summary_rows=out_customer_summary_rows,
            int_ega_t1=int_ega_t1,
            int_ega_h1=int_ega_h1,
            int_ega_t2=int_ega_t2,
            int_ega_t3=int_ega_t3,
            int_ega_h3=int_ega_h3,
            out_ega_t1=out_ega_t1,
            out_ega_h1=out_ega_h1,
            out_ega_t2=out_ega_t2,
            out_ega_t3=out_ega_t3,
            out_ega_h3=out_ega_h3,
            out_prod_data=out_prod_data,
            contest_t1_headers=contest_t1_headers, contest_t1_rows=contest_t1_rows,
            contest_t2_headers=contest_t2_headers, contest_t2_rows=contest_t2_rows,
            contest_t3_headers=contest_t3_headers, contest_t3_rows=contest_t3_rows,
        )

    # --- Build PDF ---
    print(f"\nRendering PDF (Pass 1 - mapping page numbers)...")
    page_registry = {}
    build_commission_pdf(
        output_path=output_path,
        year=year,
        invoice_dates_map=invoice_dates_map,
        int_basic_t1=int_basic_t1,
        int_basic_lines=int_basic_lines,
        int_basic_t4=int_basic_t4,
        int_nfp_agent=int_nfp_agent,
        int_nfp_rows=int_nfp_rows,
        int_anp_summary=int_anp_summary,
        int_anp_detail=int_anp_detail,
        int_anp_meta=int_anp_meta,
        int_basic_meta=int_basic_meta,
        int_nfp_meta=int_nfp_meta,
        int_ega_t1=int_ega_t1,
        int_ega_h1=int_ega_h1,
        int_ega_t2=int_ega_t2,
        int_ega_t3=int_ega_t3,
        int_ega_h3=int_ega_h3,
        int_agent_summary_rows=int_agent_summary_rows,
        int_customer_summary_rows=int_customer_summary_rows,
        int_agent_anp_rows=int_agent_anp_rows,
        int_customer_anp_rows=int_customer_anp_rows,
        out_basic_t1=out_basic_t1,
        out_basic_lines=out_basic_lines,
        out_basic_meta=out_basic_meta,
        out_nfp_agent=out_nfp_agent,
        out_nfp_rows=out_nfp_rows,
        out_anp_summary=out_anp_summary,
        out_anp_detail=out_anp_detail,
        out_anp_meta=out_anp_meta,
        out_nfp_meta=out_nfp_meta,
        out_ega_t1=out_ega_t1,
        out_ega_h1=out_ega_h1,
        out_ega_t2=out_ega_t2,
        out_ega_t3=out_ega_t3,
        out_ega_h3=out_ega_h3,
        out_agent_summary_rows=out_agent_summary_rows,
        out_customer_summary_rows=out_customer_summary_rows,
        out_customer_anp_rows=out_customer_anp_rows,
        out_prod_data=out_prod_data,
        contest_t1_headers=contest_t1_headers, contest_t1_rows=contest_t1_rows,
        contest_t2_headers=contest_t2_headers, contest_t2_rows=contest_t2_rows,
        contest_t3_headers=contest_t3_headers, contest_t3_rows=contest_t3_rows,
        report_overview=report_overview,
        page_nums_dict=None,
        page_registry=page_registry,
        month=month,
    )
    print(f"  Page registry from Pass 1: {page_registry}")

    print(f"\nRendering PDF (Pass 2 - compiling final Table of Contents)...")
    return build_commission_pdf(
        output_path=output_path,
        year=year,
        invoice_dates_map=invoice_dates_map,
        int_basic_t1=int_basic_t1,
        int_basic_lines=int_basic_lines,
        int_basic_t4=int_basic_t4,
        int_nfp_agent=int_nfp_agent,
        int_nfp_rows=int_nfp_rows,
        int_anp_summary=int_anp_summary,
        int_anp_detail=int_anp_detail,
        int_anp_meta=int_anp_meta,
        int_basic_meta=int_basic_meta,
        int_nfp_meta=int_nfp_meta,
        int_ega_t1=int_ega_t1,
        int_ega_h1=int_ega_h1,
        int_ega_t2=int_ega_t2,
        int_ega_t3=int_ega_t3,
        int_ega_h3=int_ega_h3,
        int_agent_summary_rows=int_agent_summary_rows,
        int_customer_summary_rows=int_customer_summary_rows,
        int_agent_anp_rows=int_agent_anp_rows,
        int_customer_anp_rows=int_customer_anp_rows,
        out_basic_t1=out_basic_t1,
        out_basic_lines=out_basic_lines,
        out_basic_meta=out_basic_meta,
        out_nfp_agent=out_nfp_agent,
        out_nfp_rows=out_nfp_rows,
        out_anp_summary=out_anp_summary,
        out_anp_detail=out_anp_detail,
        out_anp_meta=out_anp_meta,
        out_nfp_meta=out_nfp_meta,
        out_ega_t1=out_ega_t1,
        out_ega_h1=out_ega_h1,
        out_ega_t2=out_ega_t2,
        out_ega_t3=out_ega_t3,
        out_ega_h3=out_ega_h3,
        out_agent_summary_rows=out_agent_summary_rows,
        out_customer_summary_rows=out_customer_summary_rows,
        out_customer_anp_rows=out_customer_anp_rows,
        out_prod_data=out_prod_data,
        contest_t1_headers=contest_t1_headers, contest_t1_rows=contest_t1_rows,
        contest_t2_headers=contest_t2_headers, contest_t2_rows=contest_t2_rows,
        contest_t3_headers=contest_t3_headers, contest_t3_rows=contest_t3_rows,
        report_overview=report_overview,
        page_nums_dict=page_registry,
        page_registry=None,
        month=month,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Unified Commission PDF — Internal + Outsource (year 2026)"
    )
    parser.add_argument("--year", type=int, default=YEAR_DEFAULT)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for PDF. Defaults to Finance Output/Commission_<year>_<timestamp>.pdf",
    )
    parser.add_argument(
        "--month",
        type=int,
        default=None,
        choices=range(1, 13),
        help="Limit report to a specific month (1-12).",
    )
    parser.add_argument(
        "--May",
        "--may",
        action="store_true",
        dest="May",
        help="Limit EGA/ESA Awards and Production Bonus to January through May.",
    )
    parser.add_argument(
        "--excel",
        action="store_true",
        help="Generate Excel report instead of PDF.",
    )
    args = parser.parse_args()

    token, proxy_url, db_name = _resolve_proxy_credentials()
    if not token:
        print(_token_help_message(), file=sys.stderr)
        return 1
    print(f"Proxy: {proxy_url}  DB: {db_name}  Token: found ({len(token)} chars)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    FINANCE_DIR.mkdir(parents=True, exist_ok=True)
    
    ext = ".xlsx" if args.excel else ".pdf"
    out_file = args.output or (FINANCE_DIR / f"Commission_{args.year}_{stamp}{ext}")
    if out_file.suffix.lower() != ext:
        out_file = out_file.with_suffix(ext)

    try:
        path_res = build_pdf(args.year, out_file, month=args.month, may_only=args.May)
        label = "Excel" if ext == ".xlsx" else "PDF"
        print(f"\nCommission {label} saved:\n  {path_res.resolve()}")
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

