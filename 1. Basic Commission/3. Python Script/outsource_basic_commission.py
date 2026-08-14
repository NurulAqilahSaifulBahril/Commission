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

def get_factory_rates(raw_rows: list[dict[str, Any]], cli_factory_rates: dict[str, Decimal] | None = None, default_profit_sharing: Decimal | None = None) -> dict[str, dict[str, Any]]:
    cli_factory_rates = cli_factory_rates or {}
    factory_rates: dict[str, dict[str, Any]] = {}
    for r in raw_rows:
        if classify_property_type(r) == "Factory":
            inv_num = str(r.get("invoice_number") or "").strip()
            rate_val = Decimal("0")
            if inv_num in cli_factory_rates:
                rate_val = cli_factory_rates[inv_num]
            elif default_profit_sharing is not None:
                rate_val = default_profit_sharing
            factory_rates[inv_num] = {"sharing": rate_val}
    return factory_rates


_DASHBOARD_FACTORY_RATE_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}


def get_dashboard_factory_rate(agent_name: str, customer_name: str, year: int | str, month: int | str,
                               agent_type: str = "outsource") -> dict[str, Decimal] | None:
    """The profit-sharing rates saved on the dashboard's Factory "Profit
    Sharing" popup for this (agent, customer) pair, under the given
    year/month/agent_type bucket -- the same bucket key the dashboard's
    /api/factory-rates endpoint uses. Returns {"agent_rate", "safwan_rate"}
    as Decimal fractions (5.0 saved as "5" becomes Decimal("0.05")), or None
    if nothing has been saved for this pair yet, so the caller can fall back
    to its own default (CLI arg / interactive prompt / plain 0)."""
    key = (str(year), str(month), agent_type)
    rows = _DASHBOARD_FACTORY_RATE_CACHE.get(key)
    if rows is None:
        dashboard_dir = _REPO_ROOT / "8. Web Dashboard"
        try:
            if str(dashboard_dir) not in sys.path:
                sys.path.insert(0, str(dashboard_dir))
            import db as _dashboard_db
            rows = _dashboard_db.get_factory_rates_rows(str(year), str(month), agent_type)
        except Exception as exc:
            print(f"[Factory Rates] Warning: could not read dashboard factory_rates for "
                  f"{year}-{month} ({exc}); Factory profit sharing defaults to 0% for this period.",
                  file=sys.stderr)
            rows = []
        _DASHBOARD_FACTORY_RATE_CACHE[key] = rows

    agent_key = str(agent_name or "").strip().lower()
    cust_key = str(customer_name or "").strip().lower()
    for r in rows:
        if str(r.get("agent") or "").strip().lower() == agent_key \
           and str(r.get("customer") or "").strip().lower() == cust_key:
            def _pct(field):
                val = r.get(field)
                try:
                    return Decimal(str(val)) / Decimal("100")
                except Exception:
                    return None
            agent_rate = _pct("agent_rate")
            safwan_rate = _pct("safwan_rate")
            if agent_rate is None and safwan_rate is None:
                return None
            return {"agent_rate": agent_rate or Decimal("0"), "safwan_rate": safwan_rate or Decimal("0")}
    return None


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


def _factory_hierarchy_map() -> dict[str, dict[str, Any]]:
    """normalized agent name -> {canonical_name, hierarchy, reports_to}, from
    the Agent Roles & Hierarchy Data page (agent_roles table) -- deliberately
    NOT get_agent_hierarchy_info()'s hardcoded map above. Used only by
    resolve_factory_split_hierarchy() for the Factory profit-sharing split, per
    an explicit decision to keep that one feature driven by what's editable on
    the page, even though every other calculation in this file still uses the
    hardcoded map. One row per agent wins: the latest effective_from among
    non-hidden rows, the same rule agent_names.py uses for canonical names."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    rates_dir = str(_SCRIPT_DIR)
    if rates_dir not in sys.path:
        sys.path.insert(0, rates_dir)
    import agent_names as _names
    from basic_commission_rates import _load_db_table, effective_start

    try:
        rows = _load_db_table("agent_roles")
    except Exception:
        return {}

    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("hidden"):
            continue
        agent = str(r.get("agent") or "").strip()
        if not agent:
            continue
        key = _names.normalize_key(agent)
        eff = effective_start(r.get("effective_from"))
        prev = latest.get(key)
        if prev is None or eff > effective_start(prev.get("effective_from")):
            latest[key] = r

    out: dict[str, dict[str, Any]] = {}
    for r in latest.values():
        canon = _names.resolve(r.get("agent"))
        reports_to = str(r.get("reports_to") or "").strip()
        out[_names.normalize_key(canon)] = {
            "canonical_name": canon,
            "hierarchy": str(r.get("hierarchy") or "").strip().upper(),
            "reports_to": _names.resolve(reports_to) if reports_to else None,
        }
    return out


_FACTORY_HIERARCHY_CACHE: dict[str, dict[str, Any]] | None = None


def resolve_factory_split_hierarchy(agent_name: str) -> dict[str, Any]:
    """OSA/OUM/OGM tier, and who to credit for the Factory profit-sharing
    70/20/10 split, read from the Agent Roles & Hierarchy Data page.

    Walks reports_to up to two hops: agent -> (OUM or OGM directly) -> OGM.
    Returns {"tier", "oum_name", "ogm_name"}. Either name is None when the
    chain doesn't reach that tier -- the caller pays nothing for a missing
    hop rather than guessing who should get it (confirmed: a missing OUM
    hop does not enlarge OGM's cut, and a missing reports_to entirely burns
    that share).
    """
    global _FACTORY_HIERARCHY_CACHE
    if _FACTORY_HIERARCHY_CACHE is None:
        _FACTORY_HIERARCHY_CACHE = _factory_hierarchy_map()

    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    import agent_names as _names

    key = _names.normalize_key(_names.resolve(agent_name))
    row = _FACTORY_HIERARCHY_CACHE.get(key)
    if not row:
        return {"tier": "", "oum_name": None, "ogm_name": None}

    def _lookup(name):
        if not name:
            return None
        return _FACTORY_HIERARCHY_CACHE.get(_names.normalize_key(name))

    tier = row["hierarchy"]
    oum_name = None
    ogm_name = None

    if tier in ("OSA", "OSA 1", "OSA1"):
        parent = _lookup(row["reports_to"])
        if parent:
            if parent["hierarchy"] == "OUM":
                oum_name = parent["canonical_name"]
                grandparent = _lookup(parent["reports_to"])
                if grandparent and grandparent["hierarchy"] == "OGM":
                    ogm_name = grandparent["canonical_name"]
            elif parent["hierarchy"] == "OGM":
                ogm_name = parent["canonical_name"]
    elif tier == "OUM":
        parent = _lookup(row["reports_to"])
        if parent and parent["hierarchy"] == "OGM":
            ogm_name = parent["canonical_name"]

    return {"tier": tier, "oum_name": oum_name, "ogm_name": ogm_name}


def get_agent_type_override(agent_name: str, invoice_date: Any = None) -> str | None:
    """'internal' / 'outsource' from the Agent Roles & Hierarchy page for the
    invoice's own month, or None when that page says nothing. It outranks both
    Postgres' agent_type and the hardcoded tier map below — those are what put
    an agent whose agent_type was never set in Bubble into this report and paid
    them the outsource rate regardless of what the Data page said."""
    try:
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
    is_db_outsource = str(agent_type_field or "").lower().strip() not in {"internal", "full time"}
    return (info is not None) or is_db_outsource

def _normalize_outsource_role(role: str) -> str | None:
    """The role as the Outsource rate table spells it, or None if it isn't an
    Outsource role at all. Only OGM / OUM / OSA / OSA1 can ever match an
    Outsource rate row; anything else is an Internal role sitting on this
    agent's record and must not be used as their outsource hierarchy."""
    key = str(role or "").strip().lower().replace(" ", "")
    if key in ("ogm",):
        return "OGM"
    if key in ("oum",):
        return "OUM"
    if key in ("osa", "osa1", "osa/osa1"):
        return "OSA/OSA1"
    return None


def _outsource_hierarchy_for(info: dict[str, Any] | None, parsed_dt: Any = None) -> str:
    """The Outsource role to price this invoice under.

    The Role set on the Agent Roles & Hierarchy page wins — it's effective-dated
    by the invoice's own month/year, so a promotion mid-year still recalculates
    old invoices under the role that applied then. The hardcoded tier map
    (get_agent_hierarchy_info) is only the fallback for agents not yet listed
    there. Role values on that page are constrained to "OGM"/"OUM"/"OSA/OSA1"
    for Outsource agents, so whatever comes back is already usable as-is.

    The rate and the payout condition both resolve through here, so they can
    never disagree about which role an invoice belongs to.
    """
    from basic_commission_rates import get_agent_role
    month = parsed_dt.month if parsed_dt else 5
    year = parsed_dt.year if parsed_dt else 2026
    canonical_name = info.get("canonical_name") if info else None
    # Ask for the OUTSOURCE role specifically, and sanity-check it. The roles
    # table can carry an Internal row for the same person (a transfer, or a
    # mis-set Agent Type) — e.g. Chan Jia Wei picked up "Branch Sales Manager"
    # from 2026-07. Used as-is it looks up ("Outsource", "branch sales
    # manager"), which matches no Data page rate row, so the agent silently
    # dropped to the hardcoded 4.5% default instead of their entered rate.
    db_role = get_agent_role(canonical_name, month, year=year,
                             agent_type="Outsource") if canonical_name else None
    if db_role and _normalize_outsource_role(db_role) is None:
        db_role = None
    if db_role:
        return db_role
    tier = info.get("tier") if info else "OSA/OSA1"
    if tier == "OUM":
        return "OUM"
    if tier == "OGM":
        return "OGM"
    return "OSA/OSA1"


def _invoice_milestones(row, *, hierarchy, month, year, agent, property_type):
    from basic_commission_rates import invoice_milestones
    return invoice_milestones(row, "Outsource", hierarchy, month, year=year,
                              agent=agent, property_type=property_type)


def get_own_commission_rate(info: dict[str, Any], agent_comm_field: int | None, invoice_date: Any = None,
                            property_type: str | None = None) -> Decimal:
    from basic_commission_rates import get_basic_rate
    month = 5
    year = 2026
    parsed_dt = None
    if invoice_date:
        parsed_dt = _parse_invoice_date(invoice_date)
        if parsed_dt:
            month = parsed_dt.month
            year = parsed_dt.year

    canonical_name = info.get("canonical_name") if info else None
    hierarchy = _outsource_hierarchy_for(info, parsed_dt)

    # property_type lets Data page rows scoped to certain property types match;
    # without it every scoped row is invisible to the engine. year matters
    # because effective_from ranges like "2025-01 to 2025-09" are calendar-year
    # specific — without it every lookup silently assumed the current year.
    # agent matters for the same reason: a Data page row naming one outsource
    # agent (e.g. "Chan Jia Wei @ 5%") outranks the role-level row, but only if
    # the name reaches the lookup — without it the report kept showing the OUM
    # role rate no matter what was entered against the person. The internal
    # engine has always passed it (full_internal_basic_commission.py).
    return get_basic_rate("Outsource", hierarchy, month, agent=canonical_name,
                          property_type=property_type, year=year)

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
    # The payment-milestone thresholds come from the Data page (see
    # basic_commission_rates.milestone_sql_parts); they used to be 0.05/0.75/1.0
    # literals here, which made every payout trigger entered on that page inert.
    from basic_commission_rates import (milestone_sql_parts, milestone_column,
                                        multi_stage_cutover, payout_thresholds_in_use)
    from decimal import Decimal as _D
    milestone_ctes, milestone_selects, milestone_joins = milestone_sql_parts()
    # Re-select the milestone columns out of `candidates` by name.
    milestone_out_selects = "\n".join(
        f"  {milestone_column(t)}," for t in payout_thresholds_in_use())
    full_col = milestone_column(_D("100"))
    cutover = multi_stage_cutover(year)
    # No multi-stage rule this year means the old full-payment rule governs all
    # twelve months; month 13 makes the "from the cutover" arms match nothing.
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
    i.bubble_id,
    i.id AS invoice_row_id,
    i.is_latest,
    i.invoice_number,
    i.invoice_date,
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
  FROM target_invoices i
  -- Agents live in BOTH the agent table and the user table since the
  -- 2026-07-20 "agent retirement" migration moved most agent rows into
  -- "user" (same bubble_id). Resolve from either, preferring the row that
  -- actually carries an agent_type.
  INNER JOIN (
    SELECT DISTINCT ON (au.bubble_id) au.bubble_id, au.name, au.agent_type, au.commission
    FROM (
      SELECT u.bubble_id, u.name, u.agent_type, NULL::integer AS commission, 1 AS pri FROM "user" u
       WHERE u.bubble_id IS NOT NULL AND COALESCE(BTRIM(u.agent_type), '') <> ''
      UNION ALL
      SELECT ag.bubble_id, ag.name, ag.agent_type, ag.commission, 2 FROM agent ag
       WHERE ag.bubble_id IS NOT NULL
      UNION ALL
      SELECT u2.bubble_id, u2.name, u2.agent_type, NULL::integer, 3 FROM "user" u2
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
    -- Before the multi-stage cutover: the commission is recognised only at full
    -- payment, so an invoice that never got there has nothing to report.
    ({full_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {full_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {full_col})::int BETWEEN 1 AND {cutover_month - 1})
    OR
    -- From the cutover: reaching the advance trigger is already worth reporting.
    ({advance_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {advance_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {advance_col})::int BETWEEN {cutover_month} AND 12)
    OR
    ({full_col} IS NOT NULL
     AND EXTRACT(YEAR FROM {full_col})::int = {int(year)}
     AND EXTRACT(MONTH FROM {full_col})::int BETWEEN {cutover_month} AND 12)
  )
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
  agent_comm_field,
  package_type,
  package_name_snapshot,
  description,
  seda_nem_type,
  referral_project_type,
  referral_name
FROM candidates
WHERE rn = 1
ORDER BY agent_name ASC, real_full_payment_date ASC NULLS LAST, invoice_number ASC NULLS LAST;
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
    senior_override: Decimal = Decimal("0")
    first_payment_date: str = ""
    pct100_date: str = ""
    pct75_date: str = ""
    pct5_date: str = ""

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
    senior_override: Decimal = Decimal("0")
    senior_override_name: str | None = None
    first_payment_date: str = ""
    pct100_date: str = ""
    pct75_date: str = ""
    pct5_date: str = ""

def _cutover_ym(year: int = 2026) -> tuple[int, int] | None:
    """(year, month) the multi-stage payout starts, per the Data page."""
    from basic_commission_rates import multi_stage_cutover
    hit = multi_stage_cutover(year)
    return (year, hit[0]) if hit else None


def _get_effective_payment_date(real_full_pay_str: str, pct75_str: str, pct100_str: str = "",
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

    return ""


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
        from basic_commission_rates import multi_stage_cutover, milestone_date
        _cut = multi_stage_cutover(args.year)
        _bal_t = _cut[1].balance_trigger if _cut else Decimal("100")
        filtered_rows = []
        for r in raw_rows:
            invoice_num = str(r.get("invoice_number") or "").strip()
            inv_dt = _parse_invoice_date(r.get("invoice_date"))
            real_fp = str(r.get("real_full_payment_date") or "")[:10]
            pct75 = milestone_date(r, _bal_t)
            pct100 = milestone_date(r, Decimal("100"))
            
            NOT_FULLY_PAID_INVS = {'1008316', '1007905'}
            if invoice_num in NOT_FULLY_PAID_INVS:
                real_fp = ""
                pct75 = ""
                pct100 = ""
                
            eff_pay = _get_effective_payment_date(real_fp, pct75, pct100)
            pay_dt = _parse_invoice_date(eff_pay)
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
        if is_outsource_agent(agent_name, agent_type, r.get("invoice_date")):
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

    # Resolve rates for each Factory invoice. The dashboard's saved rate (set
    # via the "Profit Sharing" popup) always wins when present; --factory-rates
    # / --profit-sharing / the interactive prompt are the fallback for periods
    # nothing has been saved for yet, same as before.
    factory_rates: dict[str, Decimal] = {}
    factory_safwan_rates: dict[str, Decimal] = {}
    if outsource_factory_rows:
        default_profit_sharing = None
        if args.profit_sharing is not None:
            default_profit_sharing = Decimal(str(args.profit_sharing)) / Decimal("100")

        for i, r in enumerate(outsource_factory_rows, 1):
            inv_num = str(r.get("invoice_number") or "").strip()
            agent_nm = str(r.get("agent_name") or "(unknown)").strip()
            cust_name = str(r.get("customer_name") or "(unknown)").strip()
            total = _to_decimal(r.get("total_amount"))
            epp = _to_decimal(r.get("epp_interest"))
            sales_price = total - epp

            inv_month_dt = _parse_invoice_date(r.get("invoice_date"))
            dash_rate = get_dashboard_factory_rate(agent_nm, cust_name, args.year, inv_month_dt.month) \
                if inv_month_dt else None
            if dash_rate is not None:
                factory_rates[inv_num] = dash_rate["agent_rate"]
                factory_safwan_rates[inv_num] = dash_rate["safwan_rate"]
                continue

            factory_safwan_rates[inv_num] = Decimal("0")
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
        if not is_outsource_agent(agent_name, r.get("agent_type"), r.get("invoice_date")):
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
        
        real_full_pay_str = str(r.get("real_full_payment_date") or "")[:10]
        # Which payment milestones this invoice is measured against comes from
        # the Data page row that also set its rate — so pct5_str/pct75_str below
        # mean "advance due" and "balance due", whatever percentages back them.
        _inv_dt_parsed = _parse_invoice_date(r.get("invoice_date"))
        _policy, pct5_str, pct75_str, pct100_str = _invoice_milestones(
            r,
            hierarchy=_outsource_hierarchy_for(info, _inv_dt_parsed),
            month=_inv_dt_parsed.month if _inv_dt_parsed else 5,
            year=_inv_dt_parsed.year if _inv_dt_parsed else 2026,
            agent=canonical_name,
            property_type=prop_type,
        )

        NOT_FULLY_PAID_INVS = {'1008316', '1007905'}
        if invoice_num in NOT_FULLY_PAID_INVS:
            real_full_pay_str = ""
            pct75_str = ""
            pct100_str = ""
            pct5_str = ""
            
        inv_dt = str(r.get("invoice_date") or "")[:10]

        # A multi-stage invoice is recognised on its own milestones; a
        # single-stage one still falls back to the full-payment date.
        if _policy.is_multi_stage:
            pay_dt = pct75_str
            first_pay_dt = pct5_str if pct5_str else str(r.get("first_payment_date") or "")[:10]
        else:
            pay_dt = _get_effective_payment_date(real_full_pay_str, pct75_str, pct100_str)
            first_pay_dt = str(r.get("first_payment_date") or "")[:10]
            
        pay_dt = str(pay_dt or "")[:10]
        first_pay_dt = str(first_pay_dt or "")[:10]
        
        # Determine referral name
        ref_name = r.get("referral_name")

        if prop_type == "Factory":
            rate = Decimal("0.02")
            agent_sharing = factory_rates.get(invoice_num, Decimal("0"))
            safwan_sharing = factory_safwan_rates.get(invoice_num, Decimal("0"))

            full_comm = sales_price * (rate + agent_sharing)

            # Confirmed split, applied to the WHOLE Agent Commission (base +
            # sharing), read from the Agent Roles & Hierarchy Data page --
            # deliberately NOT the hardcoded `info` map used elsewhere in this
            # loop (see resolve_factory_split_hierarchy()'s docstring):
            #   OSA -> OUM found:    OSA 70% / OUM 20% / OGM 10%
            #   OSA -> OGM directly: OSA 70% / OGM 10%, 20% unpaid
            #   OSA -> no report:    OSA 70%, 30% unpaid
            #   agent is the OUM:    OUM 20%, 80% unpaid
            hier = resolve_factory_split_hierarchy(canonical_name)
            factory_tier = hier["tier"]

            comm = full_comm
            factory_oum_name = None
            factory_oum_cut = Decimal("0")
            factory_ogm_cut = Decimal("0")

            if agent_sharing > 0:
                if factory_tier in ("OSA", "OSA 1", "OSA1"):
                    comm = full_comm * Decimal("0.70")
                    if hier["oum_name"]:
                        factory_oum_name = hier["oum_name"]
                        factory_oum_cut = full_comm * Decimal("0.20")
                    if hier["ogm_name"]:
                        factory_ogm_cut = full_comm * Decimal("0.10")
                elif factory_tier == "OUM":
                    comm = full_comm * Decimal("0.20")

            if factory_oum_name:
                override_commissions[factory_oum_name] += factory_oum_cut
            if factory_ogm_cut > 0:
                override_commissions["Gan Lai Soon"] += factory_ogm_cut

            # Gan Lai Soon's regular 0.75% override, same on every OSA/OSA1/OUM
            # invoice regardless of type -- on top of (not instead of) his
            # Factory-specific cut above.
            tier = info["tier"]
            if tier in ("OSA", "OSA 1", "OUM"):
                ogm_rate = Decimal("0.0075")
            else:
                ogm_rate = Decimal("0")
            ogm_comm = sales_price * ogm_rate
            override_commissions["Gan Lai Soon"] += ogm_comm

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
                    profit_sharing=agent_sharing,
                    basic_commission=comm,
                    referral_name=ref_name,
                    gan_lai_soon=ogm_comm + factory_ogm_cut,
                    senior_override=factory_oum_cut,
                    senior_override_name=factory_oum_name,
                    first_payment_date=first_pay_dt,
                    pct100_date=pct100_str,
                    pct75_date=pct75_str,
                    pct5_date=pct5_str,
                )
            )

            safwan_rate = Decimal("0.005")
            safwan_comm = sales_price * (safwan_rate + safwan_sharing)
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
                    profit_sharing=safwan_sharing,
                    basic_commission=safwan_comm,
                    referral_name=ref_name,
                    gan_lai_soon=Decimal("0"),
                    first_payment_date=first_pay_dt,
                    pct100_date=pct100_str,
                    pct75_date=pct75_str,
                    pct5_date=pct5_str,
                )
            )
            agent_sales["Safwan"] += total
            agent_own_commissions["Safwan"] += safwan_comm
        else:
            # Rate (and the Role it's looked up against) are based on Invoice
            # Date, not Payment Date — a sale invoiced in September still gets
            # September's rate/role even if payment isn't completed until many
            # months later. Payout TIMING (whether this invoice is due this
            # month at all) is a separate concern, still driven by payment date.
            rate = get_own_commission_rate(info, agent_comm_field, inv_dt, property_type=prop_type)
            comm = sales_price * rate

            # Gan Lai Soon OGM Override Commission
            tier = info["tier"]
            if tier in ("OSA", "OSA 1", "OUM"):
                ogm_rate = Decimal("0.0075")
            else:
                ogm_rate = Decimal("0")
            ogm_comm = sales_price * ogm_rate
            override_commissions["Gan Lai Soon"] += ogm_comm

            # Senior Override per invoice (based on sales_price)
            senior_ovr = Decimal("0")
            if tier == "OSA 1":
                oum_p = info.get("oum_parent")
                if oum_p:
                    senior_ovr = sales_price * Decimal("0.005")
            elif tier == "OSA":
                oum_p = info.get("oum_parent")
                internal_senior = info.get("internal_senior_parent")
                if oum_p:
                    senior_ovr += sales_price * Decimal("0.005")
                if internal_senior:
                    senior_ovr += sales_price * Decimal("0.005")

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
                    gan_lai_soon=ogm_comm,
                    senior_override=senior_ovr,
                    first_payment_date=first_pay_dt,
                    pct100_date=pct100_str,
                    pct75_date=pct75_str,
                    pct5_date=pct5_str,
                )
            )
        
        # Accumulate sales and own commission
        agent_sales[canonical_name] += total
        agent_own_commissions[canonical_name] += comm
        
        # Calculate overrides on sales_price -- Factory invoices are excluded:
        # they already got their own complete override treatment (the
        # confirmed 70/20/10 split, above) inside the `if prop_type ==
        # "Factory":` branch, and applying this flat 0.5% on top would
        # double-credit the same OUM.
        if prop_type != "Factory":
            tier = info["tier"]
            if tier == "OSA 1":
                oum_p = info["oum_parent"]
                if oum_p:
                    override_commissions[oum_p] += sales_price * Decimal("0.005")
            elif tier == "OSA":
                oum_p = info["oum_parent"]
                internal_senior = info.get("internal_senior_parent")
                if oum_p:
                    override_commissions[oum_p] += sales_price * Decimal("0.005")
                if internal_senior:
                    override_commissions[internal_senior] += sales_price * Decimal("0.005")

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
            _fmt_money(inv.senior_override),
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
        "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Senior Override", "Gan Lai Soon (RM)"
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
            "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Senior Override", "Gan Lai Soon (RM)"
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
                    headers=["Agent name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Full Payment Date", "Total Amount", "Payment Received", "Epp", "Sales Price", "Rate %", "Basic Commission", "Senior Override", "Gan Lai Soon (RM)"],
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
