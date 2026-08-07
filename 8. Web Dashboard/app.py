import os
import re
import sys
import tempfile
import traceback
import logging
import html
import json
import uuid
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory, session, redirect, make_response

import db
import auth
import updater
from auth import login_required, admin_required

# Resolve directory structure (needed early for log path)
CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent

# Unique per-process id, regenerated every time the server starts. The frontend
# compares this against what it last saw and wipes its localStorage commission
# cache on mismatch, so a server restart (e.g. after a code fix) always shows
# fresh data instead of a stale browser-cached snapshot.
SERVER_BOOT_ID = uuid.uuid4().hex

# â”€â”€ Safe streams â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# On Windows, stdout/stderr handles can be broken (OSError [Errno 22]) when
# launched via `start cmd /k`. Wrap both so imported modules don't crash.
class _SafeStream:
    """Wraps a stream and silently discards writes that raise any OS error."""
    def __init__(self, stream):
        self._stream = stream
    def write(self, msg):
        try:
            if self._stream:
                self._stream.write(msg)
        except Exception:
            pass
    def flush(self):
        try:
            if self._stream:
                self._stream.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._stream, name)

sys.stdout = _SafeStream(sys.stdout)
sys.stderr = _SafeStream(sys.stderr)

# â”€â”€ File-based logger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Write all app errors to a log file so they're visible regardless of whether
# the console handles are working.
LOG_FILE = CURRENT_DIR / "dashboard.log"

def _log(msg: str):
    """Append msg to dashboard.log â€” never raises."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _public_error_message(exc: Exception, fallback: str = "Dashboard request failed") -> str:
    """Convert internal exceptions into short user-facing messages."""
    text = str(exc or "").strip()
    lower = text.lower()
    if "winerror 10061" in lower or "connection refused" in lower or "urlopen error" in lower:
        return "Unable to reach PG Proxy. Check the proxy/server connection and try again."
    if "timed out" in lower or "timeout" in lower:
        return "The dashboard request timed out. Please try again in a moment."
    if "pg_proxy_token" in lower or "postgres proxy token" in lower:
        return "Postgres proxy token not found. Set PG_PROXY_TOKEN in .env."
    return text or fallback

# Add presentation folder to sys.path so we can import build_commission_pack
sys.path.append(str(REPO_ROOT / "7. Presentation"))
import build_commission_pack

# Repo root on the path so we can import the shared agent-name resolver.
sys.path.append(str(REPO_ROOT))
import agent_names
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
import time

# Load all commission modules at startup so classes are defined for pickle/running
try:
    _log("Preloading commission modules...")
    # Basic
    basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
    build_commission_pack._load_module("int_basic_commission", basic_path)
    
    out_basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
    build_commission_pack._load_module("out_basic_commission", out_basic_path)
    
    # NFP
    nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
    build_commission_pack._load_module("int_nfp_commission", nfp_dir / "nfp_commission.py")
    build_commission_pack._load_module("int_nfp_paths", nfp_dir / "nfp_paths.py")
    build_commission_pack._load_module("int_nfp_paths2", nfp_dir / "nfp_paths.py")
    
    build_commission_pack._load_module("out_nfp_commission", nfp_dir / "outsource_nfp_commission.py")
    build_commission_pack._load_module("out_nfp_paths", nfp_dir / "nfp_paths.py")
    build_commission_pack._load_module("out_nfp_paths2", nfp_dir / "nfp_paths.py")
    
    # ANP
    anp_path = build_commission_pack._anp_script_path()
    build_commission_pack._load_module("int_anp_commission", anp_path)
    build_commission_pack._load_module("out_anp_commission", anp_path)
    
    # EGA/ESA
    ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
    build_commission_pack._load_module("int_ega_esa", ega_dir / "full_internal_EGA_ESA_Awards.py")
    build_commission_pack._load_module("out_ega_esa", ega_dir / "outsource_EGA_ESA_Awards.py")

    # API Clients
    build_commission_pack._load_module("api_client", nfp_dir / "api_client.py")
    build_commission_pack._load_module("api_client_out", nfp_dir / "api_client.py")

    # Production Bonus
    pb_path = REPO_ROOT / "5. Production Bonus" / "3. Python Script" / "full_outsource_Production_Bonus.py"
    build_commission_pack._load_module("out_production_bonus", pb_path)
    
    _log("Commission modules preloaded successfully.")
except Exception as e:
    _log("[STARTUP MODULE LOAD ERROR]\n" + traceback.format_exc())

app = Flask(__name__)

@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

# â”€â”€ Persistent disk cache for PG proxy data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CACHE_FILE = CURRENT_DIR / "data" / "dashboard_cache.pkl"
_data_cache = {}
_disk_cache_lock = threading.RLock()
_response_refresh_lock = threading.Lock()
_refreshing_response_keys: set[tuple[int, int, str]] = set()
_RESPONSE_CACHE_TTL_SECONDS = 300

def load_disk_cache():
    global _data_cache
    with _disk_cache_lock:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "rb") as f:
                    _data_cache = pickle.load(f)
                _log(f"Loaded cache from disk: {list(_data_cache.keys())}")
            except Exception as e:
                _log(f"Failed to load cache from disk: {e}\n" + traceback.format_exc())
                _data_cache = {}

def save_disk_cache():
    with _disk_cache_lock:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(_data_cache, f)
            _log("Saved cache to disk.")
        except Exception as e:
            _log(f"Failed to save cache to disk: {e}\n" + traceback.format_exc())

def get_cached_data(year, agent_type):
    """Retrieve cached data for a given year and agent type."""
    cache_key = (year, agent_type)
    with _disk_cache_lock:
        return _data_cache.get(cache_key)

def set_cached_data(year, agent_type, data):
    """Store data in cache for a given year and agent type."""
    cache_key = (year, agent_type)
    with _disk_cache_lock:
        _data_cache[cache_key] = data
        save_disk_cache()

def clear_cache():
    """Clear all cached data."""
    with _disk_cache_lock:
        _data_cache.clear()
        if CACHE_FILE.exists():
            try:
                CACHE_FILE.unlink()
                _log("Deleted cache file from disk.")
            except Exception as e:
                _log(f"Failed to delete cache file: {e}")
    
    # Clear dynamically loaded modules from memory to force fresh reload from disk
    dynamic_mods = [
        "int_basic_commission", "int_anp_commission", "int_nfp_commission", "int_ega_esa",
        "out_basic_commission", "out_anp_commission", "out_nfp_commission", "out_ega_esa"
    ]
    for m in dynamic_mods:
        sys.modules.pop(m, None)

def clear_commission_cache():
    """Clear only the computed commission cache, preserving raw proxy data.
    This is faster than clear_cache() as it avoids re-fetching from the proxy."""
    with _disk_cache_lock:
        keys_to_remove = [
            k for k in _data_cache
            if isinstance(k, tuple) and (
                len(k) == 2 or (len(k) == 4 and k[0] == "commission_response")
            )
        ]
        for k in keys_to_remove:
            del _data_cache[k]
        save_disk_cache()
    
    dynamic_mods = [
        "int_basic_commission", "int_anp_commission", "int_nfp_commission", "int_ega_esa",
        "out_basic_commission", "out_anp_commission", "out_nfp_commission", "out_ega_esa"
    ]
    for m in dynamic_mods:
        sys.modules.pop(m, None)
        
    _log("Cleared commission cache (raw proxy data preserved). Triggering background re-build...")
    start_prefetch(2026)


def _commission_response_cache_key(year: int, month: int, agent_type: str) -> tuple[str, int, int, str]:
    return ("commission_response", int(year), int(month), str(agent_type).lower())


def get_cached_commission_response(year: int, month: int, agent_type: str) -> dict | None:
    with _disk_cache_lock:
        return _data_cache.get(_commission_response_cache_key(year, month, agent_type))


def set_cached_commission_response(year: int, month: int, agent_type: str, payload: dict) -> None:
    with _disk_cache_lock:
        _data_cache[_commission_response_cache_key(year, month, agent_type)] = {
            "generated_at": time.time(),
            "payload": payload,
        }
        save_disk_cache()


def _trigger_background_refresh(year: int, agent_type: str) -> None:
    key = (int(year), str(agent_type).lower())
    with _response_refresh_lock:
        if key in _refreshing_response_keys:
            return
        _refreshing_response_keys.add(key)

    def _worker() -> None:
        try:
            # Refresh the shared yearly commission bundles in the background.
            start_prefetch(year)
        finally:
            with _response_refresh_lock:
                _refreshing_response_keys.discard(key)

    threading.Thread(target=_worker, daemon=True).start()


def _empty_commission_payload(agent_type: str) -> dict:
    if str(agent_type).lower() == "outsource":
        headers = [
            "Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date",
            "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission",
            "Commission Price", "OVERRIDE", "Safwan (RM)", "Gan Lai Soon", "Referral Name", "Referral Fee"
        ]
    else:
        headers = [
            "Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date",
            "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission",
            "Commission Price", "OVERRIDE", "Safwan (RM)", "Referral Name", "Referral Fee"
        ]

    return {
        "summary": {
            "total_agents": 0,
            "total_customers": 0
        },
        "sections": {
            "basic_nfp": {
                "headers": headers,
                "rows": []
            }
        }
    }


def _special_case_money(v: float) -> str:
    return f"RM {v:,.2f}" if v else "-"


def _annotate_gan_override(rows: list, headers: list, agent_i: int, customer_i: int,
                           comm_i: int, gan_i: int, agent: str, customer: str,
                           gan_comm: float, data_json: str = "") -> None:
    """Show a revised OGM override on the invoice's existing Basic row.

    A case raised from Gan Lai Soon's column changes his cut and nothing else,
    so no row is restated. The old figure stays on top with the new one in red
    beneath it, the same shape the Net Floor Price cell uses for a revised
    floor price. The markup is emitted here rather than client-side because
    this path also feeds a fresh page load, where the browser has no memory of
    the edit; the table writes cell values with innerHTML, as it already does
    for the factory profit-sharing cell.
    """
    want_agent = agent.strip().lower()
    want_customer = customer.strip().lower()

    for row in rows:
        if agent_i != -1 and str(row[agent_i] or "").strip().lower() != want_agent:
            continue
        if customer_i != -1 and str(row[customer_i] or "").strip().lower() != want_customer:
            continue
        if "basic" not in str(row[comm_i] or "").lower():
            continue

        original = str(row[gan_i] or "-").strip()
        # Already annotated (two cases for one invoice, or a repeat render), or
        # an invoice he earns nothing on: leave the cell alone either way.
        if original.startswith("<") or original == "-":
            return

        new_value = _special_case_money(gan_comm)
        if new_value == original:
            return
        row[gan_i] = (
            '<div class="special-case-merge-stack">'
            f'<span class="special-case-primary-value">{html.escape(original)}</span>'
            f'<span class="special-case-secondary-value">{html.escape(new_value)}</span>'
            "</div>"
        )
        # The browser recognises a stored case by the extra element on the row,
        # and needs it here so re-opening this cell loads the rate that was
        # saved instead of an empty box. It reads rowKind to know this one
        # restated nothing, so the row keeps its ordinary styling.
        if data_json and len(row) == len(headers):
            row.append(data_json)
        return


def _inject_special_case_rows(headers: list, rows: list, year: int, month: int, agent_type: str) -> None:
    """Overlay special cases (added via the "Add Special Case" / "Add Special
    Case Customer" modal) onto the commission rows returned to the browser.

    Without this, a special case only ever existed as a live mutation of the
    ONE browser tab that added it â€” the modal edited state.rawData.rows
    directly in JS and POSTed to /api/special-cases purely for persistence,
    but nothing ever read it back on the next request. So it looked saved to
    whoever added it, then vanished for everyone else (and for that same
    browser after its next refresh) even though the record was sitting in the
    special_cases table the whole time. This reads it back and rebuilds the
    same pair of rows the browser would have built, using the identical
    formula, so a fresh page load can never show a different number than what
    was in the Add Special Case tab.
    """
    try:
        cases = db.get_special_cases(str(year), str(month), agent_type)
    except Exception:
        return
    if not cases:
        return

    def idx(name: str, fallback: str | None = None) -> int:
        target = name.lower().strip()
        for i, h in enumerate(headers):
            if str(h).lower().strip() == target:
                return i
        if fallback:
            target = fallback.lower().strip()
            for i, h in enumerate(headers):
                if str(h).lower().strip() == target:
                    return i
        return -1

    agent_i = idx("Agent")
    customer_i = idx("Customer")
    pkg_i = idx("Package Type", "Package")
    system_i = idx("System Price")
    nfp_i = idx("Net Floor Price", "netfloor price")
    sales_i = idx("Sales Price")
    comm_i = idx("Commission", "Commission Type")
    price_i = idx("Commission Price")
    gan_i = idx("Gan Lai Soon", "Gan Lai Soon (RM)")
    remarks_i = idx("Remarks")
    inv_date_i = idx("Invoice Date")
    first_pay_i = idx("1st Payment Date")
    # July 2026+ months title this column "75% Payment Date".
    full_pay_i = idx("Full Payment Date", "75% Payment Date")
    date_cols = [i for i in (inv_date_i, first_pay_i, full_pay_i) if i != -1]

    # Mirrors getOutsourceAgentTier() in app.js: Gan Lai Soon is the sole OGM
    # and takes no override on his own invoices.
    def _is_ogm(name: str) -> bool:
        return name.lower().strip() == "gan lai soon"

    def _organic_dates(agent_name: str, customer_name: str) -> dict:
        """Date cells ('basic'/'nfp' -> {col: value}) of the customer's own rows
        already in the table, so a special case keeps showing the invoice's
        Invoice Date / 1st Payment Date / Full Payment Date instead of "-".

        The Agent column names the agent only on the first row of their block,
        so the scan carries it forward exactly like the client does. Rows with
        the special-case JSON tail are other cases — never a date source."""
        want_agent = agent_name.lower().strip()
        want_cust = customer_name.lower().strip()
        found: dict = {}
        if not date_cols or customer_i == -1:
            return found
        cur_agent = ""
        for r in rows:
            if len(r) > len(headers):
                continue
            cell_agent = str(r[agent_i] or "").strip() if agent_i != -1 else ""
            if cell_agent and cell_agent != "-":
                cur_agent = cell_agent
            if cur_agent.lower().strip() != want_agent:
                continue
            if str(r[customer_i] or "").strip().lower() != want_cust:
                continue
            kind = str(r[comm_i] or "").lower() if comm_i != -1 else ""
            key = "nfp" if ("net floor" in kind or "netfloor" in kind) else "basic"
            dates = {i: r[i] for i in date_cols
                     if str(r[i] or "").strip() not in ("", "-")}
            if dates and key not in found:
                found[key] = dates
        return found

    def _lines_dates(customer_name: str) -> dict:
        """Fallback for a hand-added customer with no rows in this month's
        table: their invoice dates from the cached commission lines (any month
        of the year), formatted the way get_dates_for_invoices renders them."""
        out: dict = {}
        if not date_cols:
            return out
        try:
            bundle = get_cached_data(year, agent_type) or {}
            basic_tuple = bundle.get("basic") or ()
            lines = basic_tuple[-1] if basic_tuple else []
            want = customer_name.lower().strip()
            inv_dates, first_dates, full_dates = set(), set(), set()
            for ln in lines:
                if str(getattr(ln, "customer_name", "") or "").strip().lower() != want:
                    continue
                d = str(getattr(ln, "invoice_date", "") or "")[:10]
                if d:
                    inv_dates.add(d)
                d = str(getattr(ln, "first_payment_date", "") or "")[:10]
                if d:
                    first_dates.add(d)
                d = str(getattr(ln, "full_payment_date", "") or "")[:10]
                if d:
                    full_dates.add(d)
            cells = {}
            if inv_date_i != -1 and inv_dates:
                cells[inv_date_i] = "<br/>".join(sorted(inv_dates))
            if first_pay_i != -1 and first_dates:
                cells[first_pay_i] = "<br/>".join(sorted(first_dates))
            if full_pay_i != -1 and full_dates:
                cells[full_pay_i] = "<br/>".join(sorted(full_dates))
            if cells:
                out["basic"] = cells
        except Exception:
            pass
        return out

    for case in cases:
        try:
            agent = str(case.get("agent") or "").strip()
            customer = str(case.get("customer") or "").strip()
            pkg = str(case.get("pkg") or "-")
            system = float(case.get("system") or 0)
            nfp = float(case.get("nfp") or 0)
            sales = float(case.get("sales") or 0)
            rate = float(case.get("rate") or 0)
            profit_sharing_pct = float(case.get("profitSharingPct") or 0)
            special_case_type = str(case.get("specialCaseType") or "adjusted_nfp")
            fee_waiver = float(case.get("feeWaiver") or 0)
            adjusted_sales_price = case.get("adjustedSalesPrice")
            adjusted_sales_price = float(adjusted_sales_price) if adjusted_sales_price else 0.0
            remarks = str(case.get("remarks") or "").strip() or "-"
            # Blank means "use the standard rate", which is not the same as 0.
            # Anything unparseable falls back to the standard rate rather than
            # raising: the enclosing except would drop the whole case, making a
            # special case vanish from the report over one bad rate. This also
            # keeps it in step with ganOverridePctFor() in app.js.
            try:
                raw_gan_pct = str(case.get("ganOverridePct") or "").strip()
                gan_override_pct = (float(raw_gan_pct) if raw_gan_pct
                                    else db.DEFAULT_GAN_OVERRIDE_PCT)
            except (TypeError, ValueError):
                gan_override_pct = db.DEFAULT_GAN_OVERRIDE_PCT
        except (TypeError, ValueError):
            continue
        if not agent or not customer:
            continue

        # Same formulas as updateSpecialCasePreview()/confirmModalBtn in
        # app.js â€” kept in lockstep so the saved value can never drift from
        # what the modal previewed. A management-adjusted sales price replaces
        # the auto-calculated one for every downstream calc here too.
        sales_for_calc = adjusted_sales_price if (special_case_type == "adjusted_sales_price" and adjusted_sales_price) else sales
        basic_comm = sales_for_calc * ((rate + profit_sharing_pct) / 100)

        if special_case_type == "fee_waiver":
            if sales_for_calc > nfp:
                orig_nfp_comm = (sales_for_calc - nfp) * 0.25
            elif sales_for_calc < nfp:
                orig_nfp_comm = (sales_for_calc - nfp) * 0.20
            else:
                orig_nfp_comm = 0.0
            nfp_comm = orig_nfp_comm + fee_waiver
        else:
            if sales_for_calc > nfp:
                nfp_comm = (sales_for_calc - nfp) * 0.25
            elif sales_for_calc < nfp:
                nfp_comm = (sales_for_calc - nfp) * 0.20
            else:
                nfp_comm = 0.0

        if nfp == 0:
            nfp_comm_str = "TBC with Finance"
        elif nfp_comm != 0:
            nfp_comm_str = _special_case_money(nfp_comm)
        else:
            nfp_comm_str = "-"

        # OGM override: Gan Lai Soon takes a cut of the sales price on every
        # OUM/OSA invoice (outsource_basic_commission.py), so a special case
        # booked under one of his agents credits his column too. The rate is
        # 0.75% by default but negotiated per agent, so the case may carry its
        # own. He still earns nothing on his own invoices.
        gan_comm = (sales_for_calc * (gan_override_pct / 100)
                    if (agent_type == "outsource" and not _is_ogm(agent)) else 0.0)

        data_json = json.dumps({k: case.get(k) for k in db.SPECIAL_CASE_FIELDS})

        # Invoice/payment dates for this case's customer: their rows already in
        # the table first, else the cached commission lines (a hand-added
        # customer has no rows this month). A customer with neither keeps "-".
        organic_dates = _organic_dates(agent, customer) or _lines_dates(customer)

        def build_row(comm_type: str, price_val: str, sales_val: str) -> list:
            row = ["-"] * len(headers)
            if agent_i != -1: row[agent_i] = agent
            if customer_i != -1: row[customer_i] = customer
            if pkg_i != -1: row[pkg_i] = pkg
            if system_i != -1: row[system_i] = _special_case_money(system)
            if nfp_i != -1: row[nfp_i] = _special_case_money(nfp)
            if sales_i != -1: row[sales_i] = sales_val
            if comm_i != -1: row[comm_i] = comm_type
            if price_i != -1: row[price_i] = price_val
            # The override rides on the basic commission row only.
            if gan_i != -1:
                row[gan_i] = _special_case_money(gan_comm) if "basic" in comm_type.lower() else "-"
            if remarks_i != -1: row[remarks_i] = remarks
            # Each restated row takes the dates of the commission row it
            # replaces, falling back to the other side of the pair.
            date_src = (organic_dates.get("nfp" if "net floor" in comm_type.lower() else "basic")
                        or organic_dates.get("basic") or organic_dates.get("nfp"))
            if date_src:
                for _ci, _val in date_src.items():
                    row[_ci] = _val
            row.append(data_json)
            return row

        # Only the commission row the case was raised from is restated. A case
        # added from the Net Floor Price row leaves Basic Commission alone, and
        # vice versa; "all" (a brand-new customer) still produces both.
        row_kind = str(case.get("rowKind") or "all").strip().lower()
        if row_kind not in ("basic", "nfp", "gan"):
            row_kind = "all"

        # "gan" is the odd one out: raised from Gan Lai Soon's column, it changes
        # only his override and must leave the agent's own commissions exactly as
        # they were. So it restates nothing — no New Basic Commission row, no New
        # Net Floor Price row — and instead annotates the existing Basic row so
        # its override cell shows the old figure above the new one.
        if row_kind == "gan":
            if gan_i != -1 and comm_i != -1:
                _annotate_gan_override(rows, headers, agent_i, customer_i, comm_i,
                                       gan_i, agent, customer, gan_comm, data_json)
            continue
        # Exception, mirroring app.js: Adjusted Sales Price changes the figure
        # BOTH commissions are derived from, so it always restates both.
        if special_case_type == "adjusted_sales_price":
            row_kind = "all"

        if row_kind in ("basic", "all"):
            rows.append(build_row("New Basic Commission",
                                  _special_case_money(basic_comm),
                                  _special_case_money(sales_for_calc)))

        if row_kind in ("nfp", "all"):
            nfp_row = build_row("New Net Floor Price Commission", nfp_comm_str, "-")
            if agent_i != -1:
                nfp_row[agent_i] = ""  # matches the client: the NFP row's Agent cell is blanked
            rows.append(nfp_row)


# â”€â”€ Background pre-fetching â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_prefetch_lock = threading.Lock()
_is_prefetching = False

def prefetch_data_worker(year=2026):
    global _is_prefetching
    with _prefetch_lock:
        if _is_prefetching:
            return
        _is_prefetching = True
    
    _log("Starting background pre-fetch...")
    try:
        # Resolve credentials
        token, url, db_name = build_commission_pack._resolve_proxy_credentials()
        if not token:
            _log("Prefetch aborted: Postgres proxy token not found.")
            return

        # Pre-fetch all agents and customers list
        _log("Pre-fetching all agents and customers list...")
        anp_mod = sys.modules.get("int_anp_commission")
        if anp_mod is None:
            anp_path = build_commission_pack._anp_script_path()
            anp_mod = build_commission_pack._load_module("int_anp_commission", anp_path)
        client = anp_mod.PostgresProxyClient(url.rstrip("/").replace("/api/sql", ""), token, db_name)
        try:
            agents_list = client.query(
                """
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
                """
            )
            customers_list = client.query("SELECT customer_id, name FROM customer ORDER BY name")
            with _disk_cache_lock:
                _data_cache['all_agents'] = agents_list
                _data_cache['all_customers'] = customers_list
                save_disk_cache()
            _log("Agents and customers list pre-fetched and saved to disk.")
        except Exception as e_ac:
            _log(f"Failed to pre-fetch agents/customers: {e_ac}")

        # Load invoice dates first
        basic_mod = sys.modules.get("int_basic_commission")
        if basic_mod is None:
            basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
            basic_mod = build_commission_pack._load_module("int_basic_commission", basic_path)
        # Pull the heavy bundle fetches in parallel so startup warm-up is faster.
        _log("Pre-fetching commission bundles in parallel...")
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_invoice_dates = ex.submit(build_commission_pack.fetch_invoice_dates, year, basic_mod)
                fut_outsource = ex.submit(_fetch_commission_bundle, year, "outsource")

                invoice_dates_map = fut_invoice_dates.result()
                _log("Invoice dates fetched successfully")
                out_bundle = fut_outsource.result()
                _log("Outsource bundle fetched successfully")
                
                # Fetch internal separately to identify which specific fetch hangs
                _log("Fetching internal bundle separately...")
                try:
                    int_bundle = _fetch_commission_bundle(year, "internal")
                    _log("Internal bundle fetched successfully")
                except Exception as e:
                    _log(f"Internal bundle fetch failed: {e}. Using empty data.")
                    int_bundle = {
                        "basic": ([], [], [], [], [], []),
                        "anp": ([], [], {}),
                        "nfp": ([], [], {}, [], {}),
                        "ega": []
                    }
        except Exception as e:
            _log(f"[PARALLEL FETCH ERROR] {e}\n{traceback.format_exc()}")
            raise

        # 1. Fetch Internal
        _log("Pre-fetching Internal Agents data...")
        int_basic = int_bundle["basic"]
        int_anp_summary, int_anp_detail, int_anp_meta = int_bundle["anp"]
        int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = int_bundle["nfp"]
        int_ega = int_bundle["ega"]

        int_ega_raw = int_bundle["ega_raw"]
        with _disk_cache_lock:
            _data_cache[(year, 'internal')] = {
                'basic': int_basic,
                'anp': (int_anp_summary, int_anp_detail, int_anp_meta),
                'nfp': (int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all),
                'ega': int_ega,
                'ega_raw': int_ega_raw,
                'invoice_dates': invoice_dates_map
            }
            save_disk_cache()
        _log("Internal data pre-fetched and saved to disk.")

        # 2. Fetch Outsource
        _log("Pre-fetching Outsource Agents data...")
        out_basic = out_bundle["basic"]
        out_anp_summary, out_anp_detail, out_anp_meta = out_bundle["anp"]
        out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = out_bundle["nfp"]
        out_ega = out_bundle["ega"]

        out_ega_raw = out_bundle["ega_raw"]
        with _disk_cache_lock:
            _data_cache[(year, 'outsource')] = {
                'basic': out_basic,
                'anp': (out_anp_summary, out_anp_detail, out_anp_meta),
                'nfp': (out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all),
                'ega': out_ega,
                'ega_raw': out_ega_raw,
                'production_bonus': {},
                'invoice_dates': invoice_dates_map
            }
            save_disk_cache()
        _log("Outsource data pre-fetched and saved to disk.")
        
    except Exception as e:
        _log("[PREFETCH ERROR]\n" + traceback.format_exc())
    finally:
        with _prefetch_lock:
            _is_prefetching = False

def start_prefetch(year=2026):
    t = threading.Thread(target=prefetch_data_worker, args=(year,), daemon=True)
    t.start()


def _fetch_commission_bundle(year: int, agent_type: str):
    """Fetch basic, ANP, NFP, and EGA data for one agent type in parallel."""
    _log(f"Starting fetch for {agent_type} agents...")
    if agent_type == "internal":
        basic_fn = lambda: build_commission_pack.fetch_internal_basic(year, h1_only=False)
        anp_fn = lambda: build_commission_pack.fetch_internal_anp(year, h1_only=False)
        nfp_fn = lambda: build_commission_pack.fetch_internal_nfp(year, h1_only=False)
        ega_fn = lambda: build_commission_pack.fetch_internal_ega_esa(year, may_only=False)
    else:
        basic_fn = lambda: build_commission_pack.fetch_outsource_basic(year, h1_only=False)
        anp_fn = lambda: build_commission_pack.fetch_outsource_anp(year, h1_only=False)
        nfp_fn = lambda: build_commission_pack.fetch_outsource_nfp(year, h1_only=False)
        ega_fn = lambda: build_commission_pack.fetch_outsource_ega_esa(year, may_only=False)

    try:
        _log(f"Fetching basic data for {agent_type}...")
        
        # Use threading for timeout (works on Windows)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(basic_fn)
            try:
                basic = future.result(timeout=180)
            except concurrent.futures.TimeoutError:
                _log(f"Basic data fetch for {agent_type} timed out after 180 seconds")
                raise
                
        _log(f"Basic data fetched for {agent_type}")
        
        _log(f"Fetching ANP data for {agent_type}...")
        anp = anp_fn()
        _log(f"ANP data fetched for {agent_type}")
        
        _log(f"Fetching NFP data for {agent_type}...")
        nfp = nfp_fn()
        _log(f"NFP data fetched for {agent_type}")
        
        _log(f"Fetching EGA data for {agent_type}...")
        ega = ega_fn()
        _log(f"EGA data fetched for {agent_type}")
        
        _log(f"Fetching raw EGA data for {agent_type}...")
        ega_raw_fn = lambda: build_commission_pack.fetch_internal_ega_raw(year) if agent_type == "internal" else build_commission_pack.fetch_outsource_ega_raw(year)
        ega_raw = ega_raw_fn()
        _log(f"Raw EGA data fetched for {agent_type}")
        
        return {
            "basic": basic,
            "anp": anp,
            "nfp": nfp,
            "ega": ega,
            "ega_raw": ega_raw,
        }
    except Exception as e:
        _log(f"[BUNDLE FETCH ERROR for {agent_type}] {e}\n{traceback.format_exc()}")
        raise

# â”€â”€ Global error handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.errorhandler(Exception)
def handle_exception(e):
    tb_str = ""
    try:
        tb_str = traceback.format_exc()
    except Exception:
        tb_str = repr(e)
    _log("[UNHANDLED EXCEPTION]\n" + tb_str)
    public_msg = _public_error_message(e, "Internal server error")
    try:
        return jsonify({"error": public_msg}), 500
    except Exception:
        return '{"error":"Internal server error"}', 500, {"Content-Type": "application/json"}

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


# Ensure environment variables are loaded
build_commission_pack._load_env_files()

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    # Generate one rather than refusing to boot. Historically Setup
    # Environment.bat wrote this, so a fresh machine where that step failed
    # (or was skipped) produced a server that died before it could explain
    # itself. Persisted to .env so sessions survive restarts; only kept
    # in-memory if .env cannot be written.
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    try:
        env_path = REPO_ROOT / ".env"
        with open(env_path, "a", encoding="utf-8") as fh:
            fh.write(f"\nFLASK_SECRET_KEY={app.secret_key}\n")
        _log(f"Generated FLASK_SECRET_KEY and saved it to {env_path}")
    except Exception:
        _log("Generated a session-only FLASK_SECRET_KEY (.env not writable); "
             "logins will not survive a restart until one is saved to .env.")

# A fresh install has no access keys yet, and every dashboard table lives
# behind them. Dying here (as this used to, via an unguarded init_db()) kills
# the server before it can say anything, so the Electron shell times out and
# blames a missing Python environment — wrongly, since v1.2.0 bundles one.
# Boot anyway and remember why we couldn't reach the database; a gate below
# serves a setup page that says exactly what to add and where.
SETUP_ERROR: str | None = None
try:
    db.init_db()
    db.migrate_from_json()
except Exception as _setup_exc:
    SETUP_ERROR = str(_setup_exc)
    _log("[SETUP] Database unreachable at startup - serving the setup page.\n"
         + traceback.format_exc())


_SETUP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Commission Portal — Setup needed</title>
<style>
 body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#F5F7FA; color:#22303C;
        display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
 .card {{ background:#fff; border:1px solid #DCE1E6; border-radius:10px; max-width:640px;
         padding:36px 42px; box-shadow:0 8px 28px rgba(11,31,46,.08); }}
 h1 {{ font-size:22px; margin:0 0 6px; color:#0B1F2E; }}
 p  {{ line-height:1.55; }}
 code {{ background:#F3F4F6; padding:2px 6px; border-radius:4px; font-size:13px; }}
 .path {{ background:#F3F4F6; padding:8px 12px; border-radius:6px; font-family:Consolas,monospace;
         font-size:12.5px; word-break:break-all; }}
 ol {{ line-height:1.7; }}
 .muted {{ color:#7A8794; font-size:13px; }}
</style></head><body><div class="card">
<h1>Almost there — the Portal needs its access keys</h1>
<p>The app itself installed fine. It just cannot reach the company database yet,
because the access keys are not in place on this computer.</p>
<ol>
  <li><b>Ask IT for your access keys</b> — a few lines that look like
      <code>PG_PROXY_TOKEN=…</code> and <code>PG_MIRROR_TOKEN=…</code></li>
  <li>Open this file in Notepad (create it if it does not exist):
      <div class="path">{env_path}</div></li>
  <li>Paste the lines in, each on its own line, and save.</li>
  <li><b>Close the Portal window and open it again.</b></li>
</ol>
<p class="muted">Details for IT: {error}</p>
</div></body></html>"""


@app.before_request
def _setup_gate():
    """While the database is unreachable, every page is the setup page.

    Nothing else can work — there are no users to log in, no rates to read —
    so showing the normal login form would just move the dead end one screen
    later and hide the actual problem."""
    if SETUP_ERROR is None:
        return None
    from markupsafe import escape
    return _SETUP_PAGE.format(env_path=escape(str(REPO_ROOT / ".env")),
                              error=escape(SETUP_ERROR)), 503

# Load disk cache and start pre-fetch if empty or missing main data keys
load_disk_cache()
with _disk_cache_lock:
    has_main_keys = (2026, 'internal') in _data_cache and (2026, 'outsource') in _data_cache
if SETUP_ERROR is not None:
    # No keys, no data: every fetch would fail the same way init_db just did.
    has_main_keys = True
FAST_START = False  # Disabled to ensure full pre-fetch when cache is incomplete
if not has_main_keys and not FAST_START:
    _log("Cache is missing internal or outsource data on startup. Triggering background pre-fetch...")
    start_prefetch(2026)
elif not has_main_keys:
    _log("Fast-start enabled: skipping startup pre-fetch so the dashboard can open immediately.")

@app.route("/")
@login_required
def index():
    resp = make_response(send_from_directory(str(CURRENT_DIR / "static"), "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/boot-id")
def boot_id():
    """Lets the frontend detect a server restart and drop its stale localStorage cache."""
    return jsonify({"boot_id": SERVER_BOOT_ID})

# â”€â”€ Self-update (OTA from GitHub Releases) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/version")
@login_required
def api_version():
    """Installed version, shown in the sidebar footer."""
    info = updater.read_local_version()
    return jsonify({
        "version": info.get("version", "0.0.0"),
        "channel": info.get("channel", "stable"),
        "repo": info.get("repo", updater.DEFAULT_REPO),
    })


@app.route("/api/update/check")
@login_required
def api_update_check():
    """Ask GitHub whether a newer release exists. Cached for 15 minutes unless
    ?force=1, so the sidebar can poll without burning the API rate limit."""
    try:
        force = request.args.get("force") in ("1", "true", "yes")
        return jsonify(updater.check_for_update(force=force))
    except Exception:
        _log("[UPDATE CHECK ERROR]\n" + traceback.format_exc())
        return jsonify({
            "current_version": updater.current_version(),
            "update_available": False,
            "error": "Update check failed",
        }), 200


@app.route("/api/update/apply", methods=["POST"])
@admin_required
def api_update_apply():
    """Download and install the latest release, then restart the server.
    Admin-only: it replaces code on this machine."""
    try:
        if updater.is_busy():
            return jsonify(updater.get_state()), 409
        state = updater.start_update()
        status = 500 if state.get("phase") == "error" else 202
        return jsonify(state), status
    except Exception as e:
        _log("[UPDATE APPLY ERROR]\n" + traceback.format_exc())
        return jsonify({"phase": "error", "error": str(e)}), 500


@app.route("/api/update/status")
@login_required
def api_update_status():
    """Progress feed for the update banner."""
    return jsonify(updater.get_state())


@app.route("/api/agent-name-map")
def agent_name_map():
    """Nickname -> canonical full-name lookup so the frontend can display full
    agent names (Title Case) in every table. Sourced from the Agent Roles &
    Hierarchy page (agent_roles table)."""
    try:
        return jsonify({"map": agent_names.get_map()})
    except Exception:
        _log("[AGENT NAME MAP ERROR]\n" + traceback.format_exc())
        return jsonify({"map": {}}), 200

@app.route("/api/contest-months")
def contest_months():
    """Months the Monthly Contest workbook holds a sheet for, so the frontend
    can show the tab without hardcoding a month."""
    try:
        import calendar as _calendar
        import openpyxl as _openpyxl

        contest_path = REPO_ROOT / "6. Monthly Contest" / "3. Python Script" / "monthly_contest.py"
        contest_mod = build_commission_pack._load_module("monthly_contest", contest_path)
        excel_path = REPO_ROOT / "6. Monthly Contest" / "1. Excel" / "1. Monthly Contest.xlsx"
        wb = _openpyxl.load_workbook(excel_path, read_only=True)
        try:
            sheet_names = list(wb.sheetnames)
        finally:
            wb.close()

        months = []
        for m in range(1, 13):
            try:
                contest_mod.resolve_sheet_name(sheet_names, "Agent Close Case", m)
            except ValueError:
                continue
            months.append(m)
        return jsonify({"months": months})
    except Exception:
        _log("[CONTEST MONTHS ERROR]\n" + traceback.format_exc())
        return jsonify({"months": [], "error": "Could not read the Monthly Contest workbook"}), 200


@app.route("/static/<path:path>")
def static_files(path):
    resp = send_from_directory(str(CURRENT_DIR / "static"), path)
    # The dashboard's JS/CSS change often; a browser holding a stale copy looks
    # exactly like a broken feature, so never let these be cached.
    if path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return send_from_directory(str(CURRENT_DIR / "static"), "login.html")

    req_data = request.json if request.is_json else request.form
    username = str((req_data or {}).get("username", "")).strip()
    password = str((req_data or {}).get("password", ""))
    user = auth.verify_login(username, password)
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    auth.login_user(user)
    ip_address = (
        request.headers.get("CF-Connecting-IP")
        or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
        or request.remote_addr
    )
    db.insert_login(user["id"], user["username"], user["role"], request.host, ip_address, request.headers.get("User-Agent"))
    return jsonify({"status": "success", "username": user["username"], "role": user["role"]})


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return jsonify({"status": "success"})


@app.route("/api/me")
def api_me():
    user = auth.current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401
    return jsonify(user)


@app.route("/admin")
@admin_required
def admin_page():
    return send_from_directory(str(CURRENT_DIR / "static"), "admin.html")


@app.route("/api/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users_api():
    if request.method == "GET":
        users = [dict(u) for u in db.list_users()]
        return jsonify(users)

    req_data = request.json or {}
    username = str(req_data.get("username", "")).strip()
    password = str(req_data.get("password", ""))
    role = str(req_data.get("role", "staff"))
    if role not in ("admin", "staff"):
        return jsonify({"error": "Role must be 'admin' or 'staff'"}), 400
    if not username or len(password) < 6:
        return jsonify({"error": "Username required and password must be at least 6 characters"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": f"User '{username}' already exists"}), 409

    user_id = db.create_user(username, auth.hash_password(password), role)
    actor = auth.current_user()
    db.insert_audit(actor["username"], "create", "user", f"Created user '{username}' ({role})", user_id=actor["id"])
    return jsonify({"status": "success", "id": user_id})


@app.route("/api/admin/users/<int:user_id>", methods=["PUT", "DELETE"])
@admin_required
def admin_user_detail_api(user_id: int):
    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    actor = auth.current_user()

    if request.method == "DELETE":
        db.deactivate_user(user_id)
        db.insert_audit(actor["username"], "delete", "user", f"Deactivated user '{target['username']}'", user_id=actor["id"])
        return jsonify({"status": "success"})

    req_data = request.json or {}
    updates: dict = {}
    if "username" in req_data and str(req_data["username"]).strip():
        updates["username"] = str(req_data["username"]).strip()
    if "role" in req_data:
        role = str(req_data["role"])
        if role not in ("admin", "staff"):
            return jsonify({"error": "Role must be 'admin' or 'staff'"}), 400
        updates["role"] = role
    if "password" in req_data and str(req_data["password"]):
        password = str(req_data["password"])
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        updates["password_hash"] = auth.hash_password(password)
    if "is_active" in req_data:
        updates["is_active"] = bool(req_data["is_active"])

    db.update_user(user_id, **updates)
    db.insert_audit(actor["username"], "update", "user", f"Updated user '{target['username']}'", user_id=actor["id"])
    return jsonify({"status": "success"})


@app.route("/api/admin/audit-log")
@admin_required
def admin_audit_log_api():
    entity_type = request.args.get("entity_type") or None
    limit = min(int(request.args.get("limit", 200)), 1000)
    offset = int(request.args.get("offset", 0))
    return jsonify(db.list_audit(entity_type=entity_type, limit=limit, offset=offset))


@app.route("/api/admin/login-log")
@admin_required
def admin_login_log_api():
    limit = min(int(request.args.get("limit", 200)), 1000)
    offset = int(request.args.get("offset", 0))
    return jsonify(db.list_logins(limit=limit, offset=offset))

def build_anp_customer_rows_custom(anp_summary, anp_detail, invoice_dates_map, month, is_internal=False):
    from collections import defaultdict
    # Build agent-level final accumulated total amount from summary rows
    anp_final_accum_by_agent = {}
    for r in anp_summary:
        ag_name = str(r.get("agent_name", "")).strip()
        anp_final_accum_by_agent[build_commission_pack.to_title_case(ag_name)] = float(r.get("accumulated_ep_points") or r.get("accumulated_total_amount") or 0.0)

    # Group details by (agent, customer)
    anp_by_cust = defaultdict(float)  # (agent, customer) -> max accumulated commission
    anp_cust_inv = defaultdict(list)   # (agent, customer) -> invoice numbers
    anp_sales_by_cust = defaultdict(float) # (agent, customer) -> sum of sales price
    anp_clawback_by_cust = defaultdict(float) # (agent, customer) -> sum of clawbacks
    anp_comm_by_agent = {}
    anp_agent_monthly = defaultdict(float)

    for r in anp_detail:
        ag = build_commission_pack.to_title_case(str(r.get("agent_name", "")).strip())
        cu = build_commission_pack.to_title_case(str(r.get("customer_name", "") or "").strip())
        if not cu:
            cu = "(Unknown)"
        key = (ag, cu)
        comm = float(r.get("anp_commission_accumulated_tier", 0.0))
        if comm > anp_by_cust[key]:
            anp_by_cust[key] = comm
        inv = r.get("invoice_number")
        if inv and inv not in anp_cust_inv[key]:
            anp_cust_inv[key].append(inv)
        anp_sales_by_cust[key] += float(r.get("invoice_total_amount", 0.0))
        anp_clawback_by_cust[key] += float(r.get("clawback", 0.0))

        # Max commission per agent in monthly details
        agent_comm_val = float(r.get("anp_commission_accumulated_tier", 0.0))
        if agent_comm_val > anp_agent_monthly[ag]:
            anp_agent_monthly[ag] = agent_comm_val

    for agent, comm in anp_agent_monthly.items():
        anp_comm_by_agent[agent] = comm

    def agent_sort_key(ag_name):
        if is_internal and hasattr(build_commission_pack, "internal_agent_sort_key"):
            return build_commission_pack.internal_agent_sort_key(ag_name)
        elif not is_internal and hasattr(build_commission_pack, "outsource_agent_sort_key"):
            return build_commission_pack.outsource_agent_sort_key(ag_name)
        return ag_name.lower()

    anp_agents_seen = []
    for (ag, cu) in sorted(anp_by_cust.keys(), key=lambda k: (agent_sort_key(k[0]), k[1])):
        if ag not in anp_agents_seen:
            anp_agents_seen.append(ag)

    customer_anp_rows = []
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
            cust_dates = build_commission_pack.get_dates_for_invoices(inv_list, invoice_dates_map)
            row_comm_str = final_anp_comm_str if first_row else "-"

            cust_anp_pkgs = set(build_commission_pack.get_invoice_package(inv, r)
                                for r in anp_detail if build_commission_pack.to_title_case(str(r.get("agent_name", "")).strip()) == ag
                                and build_commission_pack.to_title_case(str(r.get("customer_name", "") or "").strip()) == cu
                                for inv in [r.get("invoice_number")] if inv)
            cust_anp_pkg_str = "<br/>".join(sorted(cust_anp_pkgs)) if cust_anp_pkgs else "-"

            customer_anp_rows.append([
                show_ag, cu, cust_dates[0], cust_dates[1],
                cust_anp_pkg_str,
                build_commission_pack.ensure_rm_prefix(f"{anp_sales_by_cust[key]:,.2f}" if anp_sales_by_cust[key] != 0 else "-"),
                build_commission_pack.ensure_rm_prefix(f"{anp_final_accum_by_agent.get(ag, 0.0):,.2f}" if anp_final_accum_by_agent.get(ag, 0.0) != 0 else "-"),
                build_commission_pack.ensure_rm_prefix(row_comm_str),
                build_commission_pack.ensure_rm_prefix(f"{anp_clawback_by_cust[key]:,.2f}" if anp_clawback_by_cust[key] != 0 else "-")
            ])
            show_ag = ""
            first_row = False

    return customer_anp_rows


def remove_agent_block(rows, agent_name):
    filtered = []
    current_agent = ""
    target = str(agent_name).strip().lower()
    for row in rows:
        row_list = list(row)
        if len(row_list) > 0 and str(row_list[0]).strip():
            current_agent = str(row_list[0]).strip()
        if current_agent.lower() == target:
            continue
        filtered.append(row_list)
    return filtered


def _normalize_search_text(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _commission_agent_name_pool(year: int, month: int, agent_type: str) -> list[dict]:
    """Collect agent names from the generated commission summaries for the given period."""
    cached = get_cached_data(year, agent_type)
    if not cached:
        return []

    invoice_dates_map = cached.get("invoice_dates")
    if not invoice_dates_map:
        return []

    names: dict[str, dict] = {}

    def add_name(name: str, source: str, row_agent_type: str | None = None):
        clean = str(name or "").strip()
        if not clean:
            return
        key = _normalize_search_text(clean)
        if not key:
            return
        if key not in names:
            names[key] = {
                "name": clean,
                "agent_type": row_agent_type,
                "source": source,
            }

    if agent_type == "internal":
        if cached and "basic" in cached and "anp" in cached and "nfp" in cached and "ega" in cached:
            int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = cached["basic"]
            int_anp_summary, int_anp_detail, int_anp_meta = cached["anp"]
            int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = cached["nfp"]
            int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3 = cached["ega"]
        else:
            int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = build_commission_pack.fetch_internal_basic(year, h1_only=False)
            int_anp_summary, int_anp_detail, int_anp_meta = build_commission_pack.fetch_internal_anp(year, h1_only=False)
            int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = build_commission_pack.fetch_internal_nfp(year, h1_only=False)
            int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3 = build_commission_pack.fetch_internal_ega_esa(year, may_only=False)

        int_anp_detail_filtered = [r for r in int_anp_detail if build_commission_pack._parse_month(r.get("invoice_date")) == month]
        int_agent_summary, int_customer_summary, int_agent_anp, int_customer_anp, _int_agent_totals = build_commission_pack.build_internal_summary_tables(
            basic_t1=int_basic_t1,
            basic_lines=int_basic_lines,
            basic_t4=int_basic_t4,
            nfp_agent_rows=int_nfp_agent,
            nfp_rows=int_nfp_rows,
            nfp_by_inv_all=int_nfp_by_inv_all,
            anp_summary_rows=int_anp_summary,
            anp_detail=int_anp_detail_filtered,
            year=year,
            invoice_dates_map=invoice_dates_map,
            month=month
        )
        basic_nfp_rows = remove_agent_block(int_customer_summary.get(month, []), "Safwan")
        agent_summary_rows = remove_agent_block(int_agent_summary.get(month, []), "Safwan")
        for row in basic_nfp_rows:
            if len(row) > 0:
                add_name(row[0], "commission-basic", agent_type)
        for row in agent_summary_rows:
            if len(row) > 0:
                add_name(row[0], "commission-agent", agent_type)
        for row in int_anp_detail_filtered:
            add_name(row.get("agent"), "commission-anp", agent_type)
    else:
        if cached and "basic" in cached and "anp" in cached and "nfp" in cached and "ega" in cached:
            out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = cached["basic"]
            out_anp_summary, out_anp_detail, out_anp_meta = cached["anp"]
            out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = cached["nfp"]
            out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3 = cached["ega"]
        else:
            out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = build_commission_pack.fetch_outsource_basic(year, h1_only=False)
            out_anp_summary, out_anp_detail, out_anp_meta = build_commission_pack.fetch_outsource_anp(year, h1_only=False)
            out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = build_commission_pack.fetch_outsource_nfp(year, h1_only=False)
            out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3 = build_commission_pack.fetch_outsource_ega_esa(year, may_only=False)

        out_anp_detail_filtered = [r for r in out_anp_detail if build_commission_pack._parse_month(r.get("invoice_date")) == month]
        out_agent_summary, out_customer_summary, out_customer_anp_summary, _out_agent_totals = build_commission_pack.build_outsource_summary_tables(
            basic_t1=out_basic_t1,
            basic_lines=out_basic_lines,
            basic_meta=out_basic_meta,
            nfp_agent_rows=out_nfp_agent,
            nfp_rows=out_nfp_rows,
            nfp_by_inv_all=out_nfp_by_inv_all,
            anp_summary_rows=[],
            anp_detail=out_anp_detail_filtered,
            year=year,
            invoice_dates_map=invoice_dates_map,
            month=month
        )
        basic_nfp_rows = remove_agent_block(out_customer_summary.get(month, []), "Safwan")
        agent_summary_rows = remove_agent_block(out_agent_summary.get(month, []), "Safwan")
        for row in basic_nfp_rows:
            if len(row) > 0:
                add_name(row[0], "commission-basic", agent_type)
        for row in agent_summary_rows:
            if len(row) > 0:
                add_name(row[0], "commission-agent", agent_type)
        for row in out_anp_detail_filtered:
            add_name(row.get("agent"), "commission-anp", agent_type)

    return list(names.values())


INTERNAL_CONTEST_AGENTS = {
    "CHING ZHE HANG", "Vincent Tan", "TAN JIA HAO", "MARTIN HING ", "Ng Zhan Yi", "ZUL", "JOSHUA YAP JIA HAO", "Sunny Tan", "Teng Kah Kent",
    "ZH", "Jiahao", "Martin Hing", "Louis", "Joshua", "Sunny", "Kent"
}

def is_internal_contest_agent(name):
    if not name:
        return False
    clean_name = str(name).replace("*", "").strip().lower()
    for int_agent in INTERNAL_CONTEST_AGENTS:
        if int_agent.lower().strip() == clean_name:
            return True
    return False


_PANEL_BRAND_RE = re.compile(r"jinko\s*solar|jinkosolar|jinko", re.IGNORECASE)


def _panel_brand(package_description: str) -> str:
    """Brand for the panel line, normalised.

    The source spells it "Jinko", "JinkoSolar" and "JINKO" across invoices, so
    every variant is reported as one name. Invoices whose package text names no
    brand return "" and are shown as a bare quantity/rating.
    """
    first_line = str(package_description or "").split("\n")[0]
    return "JinkoSolar" if _PANEL_BRAND_RE.search(first_line) else ""


def _phase_label(phase_type) -> str:
    """"1P (Single Phase)" / "3P (Three Phase)", or "" when SEDA has no record.

    Deliberately NOT defaulting a blank to single phase. The NFP engine treats
    missing as not-three-phase for its own arithmetic, but printing "1P" for an
    invoice with no registration on file would state a fact we do not have.
    """
    text = str(phase_type or "").strip().lower()
    if not text:
        return ""
    if "three" in text or text in ("3", "3 phase") or text.startswith("3"):
        return "3P (Three Phase)"
    if "single" in text or text in ("1", "1 phase") or text.startswith("1"):
        return "1P (Single Phase)"
    return ""


def _build_system_details(nfp_by_inv_all: dict) -> dict:
    """customer name (lowercased) -> list of system detail dicts, one per invoice.

    Powers the Customer-column hover on the Basic & NFP table. Keyed off
    ``nfp_by_inv_all`` rather than the NFP commission report: the report only
    covers fully-paid invoices (~55% of the table), while this map carries every
    invoice and matches 100% of the rows on screen.
    """
    details: dict = {}
    for inv in (nfp_by_inv_all or {}).values():
        customer = str(getattr(inv, "customer_name", "") or "").strip()
        if not customer:
            continue
        qty = getattr(inv, "panel_qty", None)
        rating = getattr(inv, "panel_rating", None)
        entry = {
            "invoice": str(getattr(inv, "invoice_number", "") or ""),
            "invoice_date": str(getattr(inv, "invoice_date", "") or ""),
            "panel_qty": int(qty) if qty is not None else None,
            "panel_rating": int(rating) if rating is not None else None,
            "brand": _panel_brand(getattr(inv, "package_description", "")),
            "phase": _phase_label(getattr(inv, "phase_type", None)),
            # The 75% milestone is NOT on the table row: the column headed
            # "75% Payment Date" is filled from the invoice's full_payment_date
            # (the 100% date), so a post-July invoice that reached 75% but not
            # 100% reads "pending" there. Carried here so the Basic Commission
            # hover can name the date each tranche actually became payable.
            "pct75_date": str(getattr(inv, "pct75_date", "") or ""),
            "full_payment_date": str(getattr(inv, "full_payment_date", "") or ""),
        }
        if (entry["panel_qty"] is None and entry["panel_rating"] is None
                and not entry["phase"] and not entry["pct75_date"]
                and not entry["full_payment_date"]):
            continue
        details.setdefault(customer.lower(), []).append(entry)
    for rows in details.values():
        rows.sort(key=lambda e: e.get("invoice_date") or "")
    return details


@app.route("/api/commission")
@login_required
def get_commission():
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 5, type=int)
    agent_type = request.args.get("agent_type", "internal").lower()

    try:
        cached_response = get_cached_commission_response(year, month, agent_type)
        if cached_response and isinstance(cached_response, dict):
            age = time.time() - float(cached_response.get("generated_at") or 0.0)
            if age >= _RESPONSE_CACHE_TTL_SECONDS:
                _trigger_background_refresh(year, agent_type)
            return jsonify(cached_response.get("payload") or {})

        cached = get_cached_data(year, agent_type)
        if not cached or not cached.get("invoice_dates"):
            _trigger_background_refresh(year, agent_type)

            # Wait a little for the background build to populate the cache so
            # the UI gets real commission rows instead of an empty shell.
            deadline = time.time() + 90
            while time.time() < deadline:
                time.sleep(2)
                cached = get_cached_data(year, agent_type)
                if cached and cached.get("invoice_dates"):
                    break

            if cached and cached.get("invoice_dates"):
                invoice_dates_map = cached.get("invoice_dates")
            else:
                # Fall back to the direct path if the background build still
                # has not produced the cached bundle.
                token, proxy_url, db_name = build_commission_pack._resolve_proxy_credentials()
                if not token:
                    return jsonify({"error": "Postgres proxy token not found. Set PG_PROXY_TOKEN in .env"}), 500

                basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
                basic_mod = build_commission_pack._load_module("int_basic_commission", basic_path)
                invoice_dates_map = build_commission_pack.fetch_invoice_dates(year, basic_mod)
                cached = None
        else:
            invoice_dates_map = cached.get("invoice_dates")

        # Response payload structure
        sections = {}
        agents_set = set()
        customers_set = set()

        if agent_type == "internal":
            # 1. Fetch data (use cache if available)
            if cached and 'basic' in cached and 'anp' in cached and 'nfp' in cached and 'ega' in cached:
                int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = cached['basic']
                int_anp_summary, int_anp_detail, int_anp_meta = cached['anp']
                int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = cached['nfp']
                int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3 = cached['ega']
            else:
                int_bundle = _fetch_commission_bundle(year, "internal")
                int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = int_bundle["basic"]
                int_anp_summary, int_anp_detail, int_anp_meta = int_bundle["anp"]
                int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = int_bundle["nfp"]
                int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3 = int_bundle["ega"]
                int_ega_raw = int_bundle["ega_raw"]
                # Cache the fetched data
                set_cached_data(year, agent_type, {
                    'basic': (int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines),
                    'anp': (int_anp_summary, int_anp_detail, int_anp_meta),
                    'nfp': (int_nfp_agent, int_nfp_detail, int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all),
                    'ega': (int_ega_t1, int_ega_t2, int_ega_t3, int_ega_h1, int_ega_h2, int_ega_h3),
                    'ega_raw': int_ega_raw,
                    'invoice_dates': invoice_dates_map
                })

            # Filter ANP by month
            int_anp_detail_filtered = [r for r in int_anp_detail if build_commission_pack._parse_month(r.get("invoice_date")) == month]

            # Run build internal summary tables to get customer layout rows
            int_agent_summary, int_customer_summary, int_agent_anp, int_customer_anp, _int_agent_totals = build_commission_pack.build_internal_summary_tables(
                basic_t1=int_basic_t1,
                basic_lines=int_basic_lines,
                basic_t4=int_basic_t4,
                nfp_agent_rows=int_nfp_agent,
                nfp_rows=int_nfp_rows,
                nfp_by_inv_all=int_nfp_by_inv_all,
                anp_summary_rows=int_anp_summary,
                anp_detail=int_anp_detail_filtered,
                year=year,
                invoice_dates_map=invoice_dates_map,
                month=month
            )

            # Determine headers and pop Safwan column if no factory deal exists
            if month >= 7:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Referral Name", "Referral Fee"]
            else:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Referral Name", "Referral Fee"]

            # Extract ANP rows before column removal
            anp_rows = _apply_anp_agent_display_names(int_customer_anp.get(month, []))
            anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Total Amount", "Accumulated Total Amount", "Commission Price", "Clawback"]

            basic_nfp_rows = remove_agent_block(int_customer_summary.get(month, []), "Safwan")
            has_factory = any("factory" in str(r[6 if month >= 7 else 5]).lower() for r in basic_nfp_rows if len(r) > (6 if month >= 7 else 5))
            if not has_factory:
                if "Safwan (RM)" in basic_nfp_headers:
                    safwan_idx = basic_nfp_headers.index("Safwan (RM)")
                    basic_nfp_headers.pop(safwan_idx)
                    for r in reversed(basic_nfp_rows):
                        if len(r) > safwan_idx:
                            r.pop(safwan_idx)
                    for r in reversed(anp_rows):
                        if len(r) > safwan_idx:
                            r.pop(safwan_idx)
                for r in basic_nfp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
                for r in anp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
            else:
                for r in basic_nfp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
                for r in anp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
            if "Remarks" not in basic_nfp_headers:
                basic_nfp_headers.append("Remarks")
            for r in basic_nfp_rows:
                while len(r) < len(basic_nfp_headers):
                    r.append("-")
            for r in anp_rows:
                while len(r) < len(basic_nfp_headers):
                    r.append("-")

            _inject_special_case_rows(basic_nfp_headers, basic_nfp_rows, year, month, "internal")

            agent_summary_rows = remove_agent_block(int_agent_summary.get(month, []), "Safwan")
            agent_summary_headers = ["Agent", "Count of Customer", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission Price", "Other Commission"]

            from decimal import Decimal
            # Get raw internal EGA ESA invoices
            int_ega_raw = cached.get('ega_raw') if (cached and 'ega_raw' in cached) else None
            if not int_ega_raw:
                int_ega_raw = build_commission_pack.fetch_internal_ega_raw(year)
                if cached:
                    cached['ega_raw'] = int_ega_raw
                    save_disk_cache()

            # Filter invoices: January up to selected month M
            from collections import defaultdict
            filtered_ega_raw = []
            for r in (int_ega_raw or []):
                inv_date = r.get("invoice_date")
                if inv_date:
                    m = build_commission_pack._parse_month(inv_date)
                    if m is not None and m <= month:
                        filtered_ega_raw.append(r)

            # Build reports using filtered rows
            int_ega_mod = sys.modules.get("int_ega_esa")
            if int_ega_mod is None:
                ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
                int_ega_mod = build_commission_pack._load_module("int_ega_esa", ega_dir / "full_internal_EGA_ESA_Awards.py")

            lines, agent_ep, agent_sales, agent_eligibility = int_ega_mod.build_report(filtered_ega_raw)

            # Build Table 1: Agent Name, Customer Count, Accumulated Sales Price, Accumulated EP Point, Eligibility
            t1_rows = []
            agent_customers = defaultdict(set)
            for ln in lines:
                agent_customers[ln.agent_name].add(ln.customer_name)

            for agent in sorted(agent_ep):
                cust_count = len(agent_customers[agent])
                sales = agent_sales.get(agent, Decimal("0"))
                ep = agent_ep.get(agent, Decimal("0"))
                eligibility = agent_eligibility.get(agent, "-")
                t1_rows.append([
                    build_commission_pack.to_title_case(agent),
                    str(cust_count),
                    f"RM {sales:,.2f}" if sales != 0 else "-",
                    f"{ep:,.2f}" if ep != 0 else "-",
                    eligibility
                ])

            # Build Table 2: Agent Name, Customer Name, Invoice Date, 1st Payment Date, Sales Price, Accumulated EP Point, Eligibility
            t2_rows = []
            sorted_lines = sorted(lines, key=lambda x: (x.agent_name.lower(), x.invoice_date, x.invoice_number))
            for ln in sorted_lines:
                inv_key = str(ln.invoice_number).strip()
                dates = invoice_dates_map.get(inv_key) if invoice_dates_map else None
                inv_date = dates[0] if dates else ln.invoice_date
                first_pay_date = dates[1] if dates else ""
                t2_rows.append([
                    build_commission_pack.to_title_case(ln.agent_name),
                    build_commission_pack.to_title_case(ln.customer_name),
                    inv_date,
                    first_pay_date,
                    f"RM {ln.sales_price:,.2f}" if ln.sales_price != 0 else "-",
                    f"{ln.accum_ep:,.2f}" if ln.accum_ep != 0 else "-",
                    agent_eligibility.get(ln.agent_name, "-")
                ])

            for r in basic_nfp_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())
            for r in anp_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
            for r in t2_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())

            sections = {
                "basic_nfp": {
                    "headers": basic_nfp_headers,
                    "rows": basic_nfp_rows,
                    # Customer-column hover: panel and phase per invoice.
                    "system_details": _build_system_details(int_nfp_by_inv_all),
                },
                "agent_summary": {"headers": agent_summary_headers, "rows": agent_summary_rows},
                "anp": {"headers": anp_headers, "rows": anp_rows},
                "ega_esa": {
                    "headers_t1": ["Agent Name", "Customer Count", "Accumulated Sales Price", "Accumulated EP Point", "Eligibility"],
                    "rows_t1": t1_rows,
                    "headers_t2": ["Agent Name", "Customer Name", "Invoice Date", "1st Payment Date", "Sales Price", "Accumulated EP Point", "Eligibility"],
                    "rows_t2": t2_rows
                },
                "production_bonus": {
                    "headers_oum": ["Agent", "Total Sales", "Status", "Bonus Amount"],
                    "rows_oum": [],
                    "headers_ogm": ["Agent", "Total Sales", "Status", "Bonus Amount"],
                    "rows_ogm": [],
                    "headers_detail": ["Agent", "Customer", "Sales Price"],
                    "rows_detail": []
                }
            }

            if True:
                try:
                    contest_path = REPO_ROOT / "6. Monthly Contest" / "3. Python Script" / "monthly_contest.py"
                    contest_mod = build_commission_pack._load_module("monthly_contest", contest_path)
                    token, _, _ = build_commission_pack._resolve_proxy_credentials()
                    df_t1, _, df_t3, _, _, _, _, _, _, _, _ = contest_mod.calculate_monthly_contest(token=token, base_path=REPO_ROOT / "6. Monthly Contest", month=month, year=year)
                    df_t1 = df_t1.fillna("-")
                    df_t3 = df_t3.fillna("-")
                    t1_rows = [list(r) for r in df_t1.values]
                    t3_rows = [list(r) for r in df_t3.values]
                    sections["monthly_contest"] = {
                        "headers_t1": list(df_t1.columns),
                        "rows_t1": t1_rows,
                        "headers_t3": list(df_t3.columns),
                        "rows_t3": t3_rows
                    }
                    for r in t3_rows:
                        if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                        if len(r) > 2 and r[2]: customers_set.add(str(r[2]).strip())
                except Exception as exc:
                    _log("[CONTEST ROUTE ERROR]\n" + traceback.format_exc())
                    # Surface the reason; otherwise the UI shows a bare "no records" state.
                    sections["monthly_contest"] = {"error": str(exc)}

        else:
            if cached and 'basic' in cached and 'anp' in cached and 'nfp' in cached and 'ega' in cached:
                out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = cached['basic']
                out_anp_summary, out_anp_detail, out_anp_meta = cached['anp']
                out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = cached['nfp']
                out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3 = cached['ega']
            else:
                out_bundle = _fetch_commission_bundle(year, "outsource")
                out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = out_bundle["basic"]
                out_anp_summary, out_anp_detail, out_anp_meta = out_bundle["anp"]
                out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = out_bundle["nfp"]
                out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3 = out_bundle["ega"]
                out_ega_raw = out_bundle["ega_raw"]
                set_cached_data(year, agent_type, {
                    'basic': (out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines),
                    'anp': (out_anp_summary, out_anp_detail, out_anp_meta),
                    'nfp': (out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all),
                    'ega': (out_ega_t1, out_ega_t2, out_ega_t3, out_ega_h1, out_ega_h2, out_ega_h3),
                    'ega_raw': out_ega_raw,
                    'invoice_dates': invoice_dates_map
                })

            out_anp_detail_filtered = [r for r in out_anp_detail if build_commission_pack._parse_month(r.get("invoice_date")) == month]
            out_agent_summary, out_customer_summary, out_customer_anp_summary, _out_agent_totals = build_commission_pack.build_outsource_summary_tables(
                basic_t1=out_basic_t1,
                basic_lines=out_basic_lines,
                basic_meta=out_basic_meta,
                nfp_agent_rows=out_nfp_agent,
                nfp_rows=out_nfp_rows,
                nfp_by_inv_all=out_nfp_by_inv_all,
                anp_summary_rows=[],
                anp_detail=out_anp_detail_filtered,
                year=year,
                invoice_dates_map=invoice_dates_map,
                month=month
            )

            if month >= 7:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Gan Lai Soon", "Referral Name", "Referral Fee"]
            else:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Gan Lai Soon", "Referral Name", "Referral Fee"]

            out_anp_rows = _apply_anp_agent_display_names(build_anp_customer_rows_custom(
                anp_summary=out_anp_summary,
                anp_detail=out_anp_detail_filtered,
                invoice_dates_map=invoice_dates_map,
                month=month,
                is_internal=False
            ))

            basic_nfp_rows = remove_agent_block(out_customer_summary.get(month, []), "Safwan")
            has_factory = any("factory" in str(r[6 if month >= 7 else 5]).lower() for r in basic_nfp_rows if len(r) > (6 if month >= 7 else 5))
            if not has_factory:
                if "Safwan (RM)" in basic_nfp_headers:
                    safwan_idx = basic_nfp_headers.index("Safwan (RM)")
                    basic_nfp_headers.pop(safwan_idx)
                    for r in reversed(basic_nfp_rows):
                        if len(r) > safwan_idx:
                            r.pop(safwan_idx)
                    for r in reversed(out_anp_rows):
                        if len(r) > safwan_idx:
                            r.pop(safwan_idx)
                for r in basic_nfp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
                for r in out_anp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
            else:
                for r in basic_nfp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
                for r in out_anp_rows:
                    while len(r) < len(basic_nfp_headers):
                        r.append("-")
            if "Remarks" not in basic_nfp_headers:
                basic_nfp_headers.append("Remarks")
            for r in basic_nfp_rows:
                while len(r) < len(basic_nfp_headers):
                    r.append("-")
            for r in out_anp_rows:
                while len(r) < len(basic_nfp_headers):
                    r.append("-")

            _inject_special_case_rows(basic_nfp_headers, basic_nfp_rows, year, month, "outsource")

            anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Total Amount", "Accumulated Total Amount", "Commission Price", "Clawback"]

            from decimal import Decimal
            # Get raw outsource EGA ESA invoices
            out_ega_raw = cached.get('ega_raw') if (cached and 'ega_raw' in cached) else None
            if not out_ega_raw:
                out_ega_raw = build_commission_pack.fetch_outsource_ega_raw(year)
                if cached:
                    cached['ega_raw'] = out_ega_raw
                    save_disk_cache()

            # Filter invoices: January up to selected month M
            from collections import defaultdict
            filtered_ega_raw = []
            for r in (out_ega_raw or []):
                inv_date = r.get("invoice_date")
                if inv_date:
                    m = build_commission_pack._parse_month(inv_date)
                    if m is not None and m <= month:
                        filtered_ega_raw.append(r)

            # Build reports using filtered rows
            out_ega_mod = sys.modules.get("out_ega_esa")
            if out_ega_mod is None:
                ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
                out_ega_mod = build_commission_pack._load_module("out_ega_esa", ega_dir / "outsource_EGA_ESA_Awards.py")

            lines, agent_ep, agent_sales, agent_eligibility = out_ega_mod.build_report(filtered_ega_raw)

            # Build Table 1: Agent Name, Customer Count, Accumulated Sales Price, Accumulated EP Point, Eligibility
            t1_rows = []
            agent_customers = defaultdict(set)
            for ln in lines:
                agent_customers[ln.agent_name].add(ln.customer_name)

            for agent in sorted(agent_ep):
                cust_count = len(agent_customers[agent])
                sales = agent_sales.get(agent, Decimal("0"))
                ep = agent_ep.get(agent, Decimal("0"))
                eligibility = agent_eligibility.get(agent, "-")
                t1_rows.append([
                    build_commission_pack.to_title_case(agent),
                    str(cust_count),
                    f"RM {sales:,.2f}" if sales != 0 else "-",
                    f"{ep:,.2f}" if ep != 0 else "-",
                    eligibility
                ])

            # Build Table 2: Agent Name, Customer Name, Invoice Date, 1st Payment Date, Sales Price, Accumulated EP Point, Eligibility
            t2_rows = []
            sorted_lines = sorted(lines, key=lambda x: (x.agent_name.lower(), x.invoice_date, x.invoice_number))
            for ln in sorted_lines:
                inv_key = str(ln.invoice_number).strip()
                dates = invoice_dates_map.get(inv_key) if invoice_dates_map else None
                inv_date = dates[0] if dates else ln.invoice_date
                first_pay_date = dates[1] if dates else ""
                t2_rows.append([
                    build_commission_pack.to_title_case(ln.agent_name),
                    build_commission_pack.to_title_case(ln.customer_name),
                    inv_date,
                    first_pay_date,
                    f"RM {ln.sales_price:,.2f}" if ln.sales_price != 0 else "-",
                    f"{ln.accum_ep:,.2f}" if ln.accum_ep != 0 else "-",
                    agent_eligibility.get(ln.agent_name, "-")
                ])

            # Production Bonus is always "Jan 1 through today" — not scoped to
            # whichever month tab is selected — so it isn't cached per month.
            out_prod_data = build_commission_pack.fetch_production_bonus(year)
            prod_bonus = {"headers_oum": [], "rows_oum": [], "headers_ogm": [], "rows_ogm": [], "headers_detail": [], "rows_detail": []}
            if out_prod_data:
                pb_headers = out_prod_data.get("headers", {})
                prod_bonus = {
                    "headers_oum": pb_headers.get("oum", ["Agent", "Total Sales", "Status", "Bonus Amount"]),
                    "rows_oum": out_prod_data.get("oum_summary", []),
                    "headers_ogm": pb_headers.get("ogm", ["Agent", "Total Sales", "Status", "Bonus Amount"]),
                    "rows_ogm": out_prod_data.get("ogm_summary", []),
                    "headers_detail": pb_headers.get("detail", ["Agent", "Customer", "Sales Price"]),
                    "rows_detail": out_prod_data.get("team_detail", [])
                }

            agent_summary_rows = remove_agent_block(out_agent_summary.get(month, []), "Safwan")
            agent_summary_headers = ["Agent", "Total Invoice", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission Price", "Other Commission"]

            for r in basic_nfp_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())
            for r in out_anp_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())
            for r in t2_rows:
                if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())
            if out_prod_data:
                for r in out_prod_data.get("team_detail", []):
                    if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                    if len(r) > 1 and r[1]: customers_set.add(str(r[1]).strip())

            sections = {
                "basic_nfp": {
                    "headers": basic_nfp_headers,
                    "rows": basic_nfp_rows,
                    # Customer-column hover: panel and phase per invoice.
                    "system_details": _build_system_details(out_nfp_by_inv_all),
                },
                "agent_summary": {"headers": agent_summary_headers, "rows": agent_summary_rows},
                "anp": {"headers": anp_headers, "rows": out_anp_rows},
                "ega_esa": {
                    "headers_t1": ["Agent Name", "Customer Count", "Accumulated Sales Price", "Accumulated EP Point", "Eligibility"],
                    "rows_t1": t1_rows,
                    "headers_t2": ["Agent Name", "Customer Name", "Invoice Date", "1st Payment Date", "Sales Price", "Accumulated EP Point", "Eligibility"],
                    "rows_t2": t2_rows
                },
                "production_bonus": prod_bonus
            }

            if True:
                try:
                    contest_path = REPO_ROOT / "6. Monthly Contest" / "3. Python Script" / "monthly_contest.py"
                    contest_mod = build_commission_pack._load_module("monthly_contest", contest_path)
                    token, _, _ = build_commission_pack._resolve_proxy_credentials()
                    df_t1, _, df_t3, _, _, _, _, _, _, _, _ = contest_mod.calculate_monthly_contest(token=token, base_path=REPO_ROOT / "6. Monthly Contest", month=month, year=year)
                    df_t1 = df_t1.fillna("-")
                    df_t3 = df_t3.fillna("-")
                    t1_rows = [list(r) for r in df_t1.values]
                    t3_rows = [list(r) for r in df_t3.values]
                    sections["monthly_contest"] = {
                        "headers_t1": list(df_t1.columns),
                        "rows_t1": t1_rows,
                        "headers_t3": list(df_t3.columns),
                        "rows_t3": t3_rows
                    }
                    for r in t3_rows:
                        if len(r) > 0 and r[0]: agents_set.add(str(r[0]).strip())
                        if len(r) > 2 and r[2]: customers_set.add(str(r[2]).strip())
                except Exception as exc:
                    _log("[CONTEST ROUTE ERROR (outsource)]\n" + traceback.format_exc())
                    # Surface the reason; otherwise the UI shows a bare "no records" state.
                    sections["monthly_contest"] = {"error": str(exc)}

        payload = {
            "summary": {
                "total_agents": len(agents_set),
                "total_customers": len(customers_set)
            },
            "sections": sections
        }
        set_cached_commission_response(year, month, agent_type, payload)
        return jsonify(payload)

    except Exception as e:
        tb_str = ""
        try:
            tb_str = traceback.format_exc()
        except Exception:
            tb_str = repr(e)
        _log("[COMMISSION API ERROR]\n" + tb_str)
        public_msg = _public_error_message(e, "Commission API error")
        try:
            return jsonify({"error": public_msg}), 500
        except Exception:
            return '{"error":"Commission API error"}', 500, {"Content-Type": "application/json"}




@app.route("/api/download/pdf")
@login_required
def download_pdf():
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 5, type=int)
    
    try:
        dashboard_data_dir = CURRENT_DIR / "data"
        dashboard_data_dir.mkdir(parents=True, exist_ok=True)
        file_path = dashboard_data_dir / f"Commission_Pack_{year}_{month}.pdf"
        
        build_commission_pack.build_pdf(year, file_path, month=month)
        
        return send_file(
            str(file_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Commission_Pack_{year}_{month}.pdf"
        )
    except Exception as e:
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/excel")
@login_required
def download_excel():
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 5, type=int)
    
    try:
        dashboard_data_dir = CURRENT_DIR / "data"
        dashboard_data_dir.mkdir(parents=True, exist_ok=True)
        file_path = dashboard_data_dir / f"Commission_Pack_{year}_{month}.xlsx"
        
        build_commission_pack.build_pdf(year, file_path, month=month)
        
        return send_file(
            str(file_path),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"Commission_Pack_{year}_{month}.xlsx"
        )
    except Exception as e:
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500
@app.route("/api/cache/clear", methods=["POST"])
@login_required
def clear_cache_api():
    """Clear the in-memory and disk data cache."""
    clear_cache()
    start_prefetch(2026)
    return jsonify({"status": "success", "message": "Cache cleared, background refresh started"})


@app.route("/api/special-cases", methods=["GET", "POST"])
@login_required
def special_cases_api():
    if request.method == "GET":
        year = request.args.get("year", "2026")
        month = request.args.get("month", "5")
        agent_type = request.args.get("agent_type", "internal")
        return jsonify(db.get_special_cases(year, month, agent_type))

    req_data = request.json or {}
    year = str(req_data.get("year", "2026"))
    month = str(req_data.get("month", "5"))
    agent_type = str(req_data.get("agent_type", "internal"))
    new_sc_list = req_data.get("special_cases", [])
    deleted = req_data.get("deleted", []) or []

    # The browser posts the cases it currently holds, and db.save_special_cases
    # replaces the whole (year, month, agent_type) bucket. That combination
    # silently destroyed saved cases whenever the page's in-memory list was
    # short of what was stored — e.g. rows hidden by a customer filter. So a
    # post now MERGES: it upserts what it was given and removes only what the
    # user explicitly deleted. "replace" is still available for a deliberate
    # full overwrite.
    def _case_key(c):
        return (str(c.get("agent") or "").strip().lower(),
                str(c.get("customer") or "").strip().lower())

    try:
        if str(req_data.get("mode") or "merge").lower() != "replace":
            merged = {}
            for c in db.get_special_cases(year, month, agent_type):
                merged[_case_key(c)] = c
            for c in deleted:
                merged.pop(_case_key(c), None)
            for c in new_sc_list:
                # A case with no agent can never be rendered again
                # (_inject_special_case_rows skips it), so refuse to store one.
                if not str(c.get("agent") or "").strip():
                    continue
                merged[_case_key(c)] = c
            new_sc_list = list(merged.values())

        db.save_special_cases(year, month, agent_type, new_sc_list, auth.current_user()["username"])
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[SPECIAL CASES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/factory-rates", methods=["GET", "POST"])
@login_required
def factory_rates_api():
    if request.method == "GET":
        year = request.args.get("year", "2026")
        month = request.args.get("month", "5")
        agent_type = request.args.get("agent_type", "internal")
        return jsonify(db.get_factory_rates_rows(year, month, agent_type))

    req_data = request.json or {}
    year = str(req_data.get("year", "2026"))
    month = str(req_data.get("month", "5"))
    agent_type = str(req_data.get("agent_type", "internal"))
    new_rates = req_data.get("factory_rates", [])

    try:
        db.save_factory_rates(year, month, agent_type, new_rates, auth.current_user()["username"])
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[FACTORY RATES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500



def _rates_module():
    """Import (or fetch the already-imported) basic_commission_rates module."""
    mod = sys.modules.get("basic_commission_rates")
    if mod is None:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        import basic_commission_rates as mod
    return mod


def _apply_anp_agent_display_names(rows: list) -> list:
    """ANP report Agent column: Full Name from the Agent Roles & Hierarchy
    page, falling back to the Agent Name (from eeAdmin) already in the row
    when Full Name isn't set. Applied only here, after every Basic/NFP/ANP
    cross-referencing join (which matches by the raw eeAdmin name) is already
    done — a pure display-layer swap that can't disturb those joins."""
    try:
        get_display_name = _rates_module().get_agent_display_name
    except Exception:
        return rows
    out = []
    for row in rows:
        if not row:
            out.append(row)
            continue
        new_row = list(row)
        new_row[0] = get_display_name(new_row[0]) or new_row[0]
        out.append(new_row)
    return out


@app.route("/data")
@login_required
def data_page():
    resp = make_response(send_from_directory(str(CURRENT_DIR / "static"), "data.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/basic-rates", methods=["GET", "POST"])
@login_required
def basic_rates_api():
    if request.method == "GET":
        return jsonify(db.list_basic_rates())

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    rows = (request.json or {}).get("rates", [])
    try:
        db.save_basic_rates(rows, user["username"])
        _rates_module().reset_cache()
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[BASIC RATES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/rule-settings", methods=["GET", "POST"])
@login_required
def rule_settings_api():
    if request.method == "GET":
        return jsonify(db.list_rule_settings())

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    rows = (request.json or {}).get("rules", [])
    try:
        db.save_rule_settings(rows, user["username"])
        _rates_module().reset_cache()
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[RULE SETTINGS SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/commission-rates", methods=["GET", "POST"])
@login_required
def commission_rates_api():
    """Unified rates + rules table behind the merged Data grid. A row here can
    carry a rate and a payout condition at once; the engine reads it in
    preference to the legacy basic_rates / rule_settings tables."""
    if request.method == "GET":
        return jsonify(db.list_commission_rates())

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    rows = (request.json or {}).get("entries", [])
    try:
        db.save_commission_rates(rows, user["username"])
        _rates_module().reset_cache()
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[COMMISSION RATES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent-roles/pg-list")
@login_required
def agent_roles_pg_list_api():
    """Query Postgres for users/agents with status=active or pending signup
    whose active tags contain outsource, internal, or sales. Also pulls IC No.
    from My Kad / Agent IC document tables. Used to populate the Agent Roles &
    Hierarchy table with live Postgres data."""
    try:
        mod = _basic_module()
        mod._load_dotenv()
        proxy_url = mod._normalize_proxy_url(os.environ.get("PG_PROXY_URL")) or \
            "https://pg-proxy-production.up.railway.app/api/sql"
        db_name = os.environ.get("PG_PROXY_DB") or os.environ.get("PG_DB_NAME")
        token = os.environ.get("PG_PROXY_TOKEN")
        if not token:
            return jsonify({"error": "PG_PROXY_TOKEN is not configured"}), 400

        # â”€â”€ Main query: UNION ALL across "user" + "agent" tables â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # The seed query already proved this pattern works.  We extend it with:
        #   â€¢ status filter (active OR pending signup)
        #   â€¢ sales tag in addition to internal/outsource
        #   â€¢ bubble_id so we can join IC numbers later

        def _try_users_sql(sql_str):
            try:
                r = mod._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                                   sql=sql_str, params=[])
                return r.get("rows") or r.get("data") or [], None
            except Exception as ex:
                return [], str(ex)

# Query Postgres with a schema-correct single query that joins user and agent tables,
        # filters status from access_level, and excludes blocked users.
        # One row per PERSON. Merged primarily by link_key -- an agent row's own
        # linked_user_login, resolved through the join below -- because that is
        # the one signal a typo cannot break: eeAdmin's own foreign key, not a
        # name comparison. A name mismatch on an otherwise-linked pair (e.g. the
        # user account spelled "Chong Lee", the agent record spelled "Cong Lee")
        # used to produce two rows for one person, the agent-table typo
        # unnoticed because eeAdmin's own UI reads the user record. Falling back
        # to the normalized name only when link_key is NULL (no linked_user_login
        # at all) keeps today's behaviour for agent records with no user account
        # to link to -- matching on whitespace-collapsed name still catches
        # "KOH YEONG CHERNG" vs "KOH  YEONG CHERNG" for those.
        #
        # role_rank decides which record survives the merge first, then whichever
        # side actually carries a non-blank agent_type (a blank must never beat
        # a real one merely because it belongs to the row ranked first), then pri.
        user_rows, err_sql = _try_users_sql("""
            SELECT DISTINCT ON (COALESCE(au.link_key, LOWER(regexp_replace(BTRIM(au.name), '[[:space:]]+', ' ', 'g'))))
                   au.bubble_id,
                   au.name,
                   au.agent_type,
                   au.access_level,
                   au.status,
                   au.branch,
                   au.start_date
            FROM (
              -- Users
              SELECT u.bubble_id,
                     u.bubble_id AS link_key,
                     u.name,
                     u.agent_type,
                     u.access_level,
                     CASE WHEN 'pending' = ANY(u.access_level) THEN 'Pending' ELSE 'Active' END AS status,
                     u.main_department AS branch,
                     u.created_at AS start_date,
                     1 AS pri,
                     CASE WHEN LOWER(COALESCE(u.access_level::text, '')) ~
                       '(oum|osa|ogm|senior|executive|consultant|manager|director)'
                       THEN 0 ELSE 1 END AS role_rank
              FROM "user" u
              WHERE COALESCE(BTRIM(u.name), '') <> ''
                AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                AND (
                  LOWER(COALESCE(u.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.access_level::text, '')) ~ '(internal|outsource|sales)'
                )

              UNION ALL

              -- Agents joined with users. link_key is the linked user's own
              -- bubble_id (NULL when linked_user_login names no real user), so
              -- a linked agent row merges with its user row above regardless of
              -- whether the two name fields agree.
              SELECT ag.bubble_id,
                     u.bubble_id AS link_key,
                     ag.name,
                     COALESCE(ag.agent_type, u.agent_type) AS agent_type,
                     u.access_level,
                     CASE WHEN u.access_level IS NOT NULL AND 'pending' = ANY(u.access_level) THEN 'Pending' ELSE 'Active' END AS status,
                     u.main_department AS branch,
                     COALESCE(ag.created_date, u.created_at) AS start_date,
                     2 AS pri,
                     CASE WHEN LOWER(COALESCE(u.access_level::text, '')) ~
                       '(oum|osa|ogm|senior|executive|consultant|manager|director)'
                       THEN 0 ELSE 1 END AS role_rank
              FROM agent ag
              LEFT JOIN "user" u ON u.bubble_id = ag.linked_user_login
              WHERE COALESCE(BTRIM(ag.name), '') <> ''
                AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                AND (
                  LOWER(COALESCE(ag.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.access_level::text, '')) ~ '(internal|outsource|sales)'
                )
            ) au
            ORDER BY COALESCE(au.link_key, LOWER(regexp_replace(BTRIM(au.name), '[[:space:]]+', ' ', 'g'))),
                     au.role_rank,
                     CASE WHEN COALESCE(au.agent_type, '') = '' THEN 1 ELSE 0 END,
                     au.pri
        """)

        # If it failed, log the error
        if err_sql:
            _log(f"[PG LIST ERROR] Query failed: {err_sql}")

        # Attempt C: broadest fallback â€” status only, no tag filter
        if not user_rows:
            _log("[PG LIST] Falling back to status-only query (no tag filter)")
            user_rows, _ = _try_users_sql("""
                SELECT DISTINCT ON (LOWER(BTRIM(au.name)))
                       au.bubble_id,
                       au.name,
                       au.agent_type,
                       au.status,
                       au.branch
                  FROM (
                    SELECT u._id AS bubble_id,
                           u.name,
                           COALESCE(NULLIF(BTRIM(u.agent_type), ''), '') AS agent_type,
                           COALESCE(NULLIF(BTRIM(u.status),     ''), '') AS status,
                           u.main_department AS branch,
                           1 AS pri
                      FROM "user" u
                     WHERE COALESCE(BTRIM(u.name), '') <> ''
                       AND (
                             LOWER(COALESCE(BTRIM(u.status), '')) IN ('active', 'pending signup')
                          OR LOWER(COALESCE(BTRIM(u.status), '')) LIKE '%pending%'
                       )
                       AND LOWER(COALESCE(BTRIM(u.status), '')) NOT LIKE '%blocked%'
                    UNION ALL
                    SELECT ag._id AS bubble_id,
                           ag.name,
                           COALESCE(NULLIF(BTRIM(ag.agent_type), ''), '') AS agent_type,
                           COALESCE(NULLIF(BTRIM(ag.status),     ''), '') AS status,
                           NULL AS branch,
                           2 AS pri
                      FROM agent ag
                     WHERE COALESCE(BTRIM(ag.name), '') <> ''
                       AND (
                             LOWER(COALESCE(BTRIM(ag.status), '')) IN ('active', 'pending signup')
                          OR LOWER(COALESCE(BTRIM(ag.status), '')) LIKE '%pending%'
                       )
                       AND LOWER(COALESCE(BTRIM(ag.status), '')) NOT LIKE '%blocked%'
                  ) au
                 ORDER BY LOWER(BTRIM(au.name)), au.pri
            """)

        # â”€â”€ IC photo from agent/user.ic_front â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Postgres never stores the IC number as text â€” ic_front/ic_back are
        # photos of the physical card. There is no OCR step here, so the IC
        # No. itself stays a manually-entered field in the roles grid (from
        # someone reading the uploaded photo). We only surface the photo URL,
        # matched by name since agent.bubble_id and user.bubble_id are two
        # unrelated ID sequences (matching by linked_user_login can miss).
        ic_photo_by_name = {}

        def _try_optional_query(sql_str, label="OPTIONAL"):
            """Run a query whose failure must not take the whole page down."""
            try:
                r = mod._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                                   sql=sql_str, params=[])
                return r.get("rows") or r.get("data") or []
            except Exception as ex:
                _log(f"[{label} QUERY SKIPPED] {ex}")
                return []

        ic_photo_rows = _try_optional_query("""
            SELECT LOWER(BTRIM(name)) AS name_key, ic_front FROM agent WHERE ic_front IS NOT NULL
            UNION ALL
            SELECT LOWER(BTRIM(name)) AS name_key, ic_front FROM "user" WHERE ic_front IS NOT NULL
        """, "IC PHOTO")
        for row in ic_photo_rows:
            key = str(row.get("name_key") or "").strip()
            url = str(row.get("ic_front") or "").strip()
            if key and url and key not in ic_photo_by_name:
                ic_photo_by_name[key] = url

        # ── Blocked accounts ─────────────────────────────────────────────────
        # The main query already leaves blocked accounts out of the pull, but
        # that alone does not keep them off the page: a row saved before someone
        # was blocked is still drawn by the "agent not in Postgres" path, which
        # exists for Excel-only entries and for people deleted from eeAdmin.
        # Naming the blocked ids is what lets the page tell those cases apart —
        # deleted from eeAdmin means keep the row, blocked means drop it.
        #
        # Matching is by bubble_id only. Names are not unique here (one person
        # can hold several eeAdmin records under different spellings), so a
        # name match could hide the wrong agent's hierarchy.
        blocked_ids = []
        for row in _try_optional_query("""
            SELECT u.bubble_id FROM "user" u WHERE 'blocked' = ANY(u.access_level)
            UNION ALL
            SELECT ag.bubble_id FROM agent ag
              JOIN "user" u2 ON u2.bubble_id = ag.linked_user_login
             WHERE 'blocked' = ANY(u2.access_level)
        """, "BLOCKED IDS"):
            bid = str(row.get("bubble_id") or "").strip()
            if bid:
                blocked_ids.append(bid)

        # ── Build response ───────────────────────────────────────────────────
        def to_sentence_case(name):
            """Convert name to Sentence Case (Title Case per word)."""
            if not name:
                return name
            return " ".join(word.capitalize() for word in str(name).strip().split())

        # Tags that describe role/type/branch/permission, not a person's nickname.
        _KNOWN_TAG_RE = re.compile(
            r'^(team-|tam-)|'
            r'\b(sales|internal|outsource|admin|hr|seda|finance|engineer(?:ing)?|'
            r'electrical|project|notification|superadmin|ceo|support|ec|ee-core|'
            r'invoiceapprove|kc|o&m|inventory|cameraman|dealer|manual|pending|blocked|'
            r'referral\s+program|report|special|invoice\s+editor|js)\b',
            re.IGNORECASE,
        )

        # ── Role vocabulary ──────────────────────────────────────────────────
        # Outsource tiers, most senior first — checked before the Internal tiers
        # so an outsource agent's tags never fall through to an Internal label.
        # The acronyms are matched on a word boundary, never as a bare substring:
        # "osa" inside a nickname tag like "Rosa" is not a role.
        _OUTSOURCE_TIER_PATTERNS = [
            # OGM is retired — kept only so the handful of agents still carrying
            # the tag keep resolving instead of silently losing their role.
            (re.compile(r"\bogm\b", re.IGNORECASE), "OGM"),
            (re.compile(r"\boum\b", re.IGNORECASE), "OUM"),
            (re.compile(r"\bosa\b", re.IGNORECASE), "OSA"),
        ]
        # Internal tiers, most senior first. The specific multi-word titles have
        # to precede the two loose entries at the bottom: "senior sales
        # consultant" and "senior branch director" both contain "senior", and
        # the looser rule would otherwise swallow them.
        _INTERNAL_TIER_PATTERNS = [
            # Retired like OGM, and kept for the same reason.
            (re.compile(r"regional\s+sales\s+director", re.IGNORECASE), "Regional Sales Director"),
            (re.compile(r"senior\s+branch\s+director", re.IGNORECASE), "Senior Branch Director"),
            (re.compile(r"branch\s+sales\s+manager", re.IGNORECASE), "Branch Sales Manager"),
            (re.compile(r"sales\s+development\s+manager", re.IGNORECASE), "Sales Development Manager"),
            (re.compile(r"sales\s+team\s+manager", re.IGNORECASE), "Sales Team Manager"),
            (re.compile(r"senior\s+sales\s+consultant", re.IGNORECASE), "Senior Sales Consultant"),
            (re.compile(r"sales\s+consultant", re.IGNORECASE), "Sales Consultant"),
            # Deliberately loose: any leftover tag carrying "senior" or
            # "executive" is that tier, whether or not it spells out the full
            # "sales senior" / "sales executive". eeAdmin tags are typed by hand
            # and both spellings are in use.
            (re.compile(r"senior", re.IGNORECASE), "Sales Senior"),
            (re.compile(r"executive", re.IGNORECASE), "Sales Executive"),
        ]

        def _classify_role_tag(tag):
            """(agent_type, Role) for one tag, or None. Each tag is classified on
            its own rather than against a joined blob, so a phrase like "senior
            sales consultant" can never bleed into a separate "senior" tag."""
            for pat, role in _OUTSOURCE_TIER_PATTERNS:
                if pat.search(tag):
                    return ("Outsource", role)
            for pat, role in _INTERNAL_TIER_PATTERNS:
                if pat.search(tag):
                    return ("Internal", role)
            return None

        def extract_nick_name(access_level):
            """The one access_level tag that is neither a known
            role/branch/permission label nor a role title is treated as the
            agent's nickname (e.g. 'jerry', 'lk'). Role tags have to be excluded
            explicitly as well as by _KNOWN_TAG_RE: a bare "senior" or
            "executive" tag names a role under the loose matching above but
            carries none of the words that pattern looks for."""
            for tag in (access_level or []):
                tag = str(tag).strip()
                if tag and not _KNOWN_TAG_RE.search(tag) and not _classify_role_tag(tag):
                    return to_sentence_case(tag)
            return ""

        def extract_roles(access_level):
            """Every distinct role the agent's tags name, most senior first, as
            [{"hierarchy": ..., "agent_type": ...}].

            An agent routinely holds several at once — a promotion adds the new
            tag without removing the old one, and a transfer can leave an
            Internal and an Outsource tag side by side. The tags carry no dates,
            so Postgres cannot say which period each covers and this must not
            guess: every role is returned and the page turns each into its own
            row for a human to date. Collapsing them to one role (or refusing
            to pick, as the old "needs split" flag did) either invented history
            or hid it."""
            seen, out = set(), []
            classified = set()
            for tag in (access_level or []):
                hit = _classify_role_tag(str(tag).strip())
                if hit:
                    classified.add(hit)
            # Emit in seniority order rather than tag order, so the row a reader
            # sees first is the agent's most senior title.
            for pat_list, want_type in ((_OUTSOURCE_TIER_PATTERNS, "Outsource"),
                                        (_INTERNAL_TIER_PATTERNS, "Internal")):
                for _, role in pat_list:
                    if (want_type, role) in classified and role not in seen:
                        seen.add(role)
                        out.append({"hierarchy": role, "agent_type": want_type})
            return out

        # "tam-" is a misspelling of "team-" that a handful of eeAdmin records
        # carry. _KNOWN_TAG_RE above already allows for it so those tags are not
        # mistaken for nicknames; matching it here too is what stops the same
        # agents silently showing no branch at all.
        # Named after the eeAdmin tag they come from, so the value on screen and
        # the tag an admin sees in eeAdmin are the same word. Anything not on
        # this list is not a branch.
        _BRANCH_TAG_KEYWORDS = [
            ("jb", "Team-JB"),
            ("kluang", "Team-Kluang"),
            ("klang", "Team-Klang"),
            ("seremban", "Team-Seremban"),
        ]
        _BRANCH_TAG_PREFIX_RE = re.compile(r"^t(?:e)?am-", re.IGNORECASE)

        def extract_branch(access_level):
            """A 'team-xxx' active tag names the branch far more reliably than
            main_department, which is blank for most agents. Only the part after
            the prefix is compared, so "klang" can never match inside
            "team-kluang" — the two branch names differ by one letter."""
            for tag in (access_level or []):
                low = str(tag).strip().lower()
                if not _BRANCH_TAG_PREFIX_RE.match(low):
                    continue
                suffix = _BRANCH_TAG_PREFIX_RE.sub("", low, count=1).strip()
                for kw, branch in _BRANCH_TAG_KEYWORDS:
                    if suffix == kw:
                        return branch
            return ""

        result = []
        for r in user_rows:
            bid = str(r.get("bubble_id") or "").strip()
            name = to_sentence_case(str(r.get("name") or "").strip())
            raw_type = str(r.get("agent_type") or "").strip()
            access_level = r.get("access_level") or []
            
            # Normalise agent type label by checking both agent_type and access_level
            low_type = raw_type.lower()
            access_text = " ".join(access_level).lower()
            combined_text = f"{low_type} {access_text}"
            
            # Only "internal" and "outsource" name a rate table. A bare "sales"
            # tag (or no matching tag at all) does not, so it must not resolve to
            # one silently: agent_type picks which RATE_ROLES table prices the
            # commission, and "Sales" was landing on the Internal list by way of
            # a missing key rather than by anyone's decision. Leave it blank and
            # let the page ask for a human call instead.
            type_unknown = False
            if "outsource" in combined_text:
                agent_type = "Outsource"
            elif "internal" in combined_text:
                agent_type = "Internal"
            else:
                agent_type = ""
                type_unknown = True
                
            status = str(r.get("status") or "Active").strip().title()
            # Only the four sales branches are branches. main_department is NOT
            # a fallback for them — it holds things like "Sales Dept",
            # "Engineering Dept" or "Strategy & Partnership; C&I Project",
            # which are departments, not the branch this page means. Letting it
            # through is how 38 rows ended up with a Branch that is not one.
            branch = extract_branch(access_level)
            ic_no = ""
            ic_photo_url = ic_photo_by_name.get(name.strip().lower(), "")
            nick_name = extract_nick_name(access_level)
            roles = extract_roles(access_level)
            # `hierarchy` stays in the payload for any reader that predates the
            # split and expects one role — it names the most senior. The page
            # itself reads `roles` and draws a row per entry.
            hierarchy = roles[0]["hierarchy"] if roles else ""
            # An agent whose tags name only outsource (or only internal) roles
            # settles the agent type that the tag blob left unknown.
            role_types = {r["agent_type"] for r in roles}
            if type_unknown and len(role_types) == 1:
                agent_type = next(iter(role_types))
                type_unknown = False

            start_date_raw = r.get("start_date")
            start_date = ""
            if start_date_raw:
                try:
                    start_date = str(start_date_raw)[:10]
                except Exception:
                    pass

            result.append({
                "bubble_id": bid,
                "name": name,
                "agent_type": agent_type,
                "type_unknown": type_unknown,
                "status": status,
                "branch": branch,
                "ic_no": ic_no,
                "ic_photo_url": ic_photo_url,
                "nick_name": nick_name,
                "hierarchy": hierarchy,
                "roles": roles,
                # Retired: several role tags at once is the normal case now and
                # produces one row each, not an error a human has to unpick.
                "role_conflict": False,
                "start_date": start_date,
            })

        result.sort(key=lambda x: x.get("name", "").lower())
        return jsonify({"agents": result, "total": len(result),
                        "blocked_ids": sorted(set(blocked_ids))})
    except Exception as e:
        _log("[AGENT PG LIST ERROR]\n" + traceback.format_exc())
        return jsonify({"error": _public_error_message(e, "Failed to load agent list from Postgres")}), 500


@app.route("/api/agent-roles", methods=["GET", "POST"])
@login_required
def agent_roles_api():
    """Agent role hierarchy: who holds which role, who they report to, which
    branch. Role and Reports To exist nowhere else â€” Postgres does not carry
    them â€” so this table is the only source for the override attribution."""
    if request.method == "GET":
        return jsonify(db.list_agent_roles())

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    rows = (request.json or {}).get("entries", [])
    try:
        db.save_agent_roles(rows, user["username"])
        _rates_module().reset_cache()
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[AGENT ROLES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


def _rules_endpoint(loader, saver, key_name, key_pattern, child_keys, list_args):
    """Shared GET/POST handler for the ANP, EGA/ESA and Production Bonus rule
    sets. GET on a period with nothing saved returns the built-in defaults with
    saved=false, so the page can be filled in and saved as a new period."""
    if request.method == "GET":
        key = (request.args.get(key_name) or "").strip()
        if not re.fullmatch(key_pattern, key):
            return jsonify({"error": f"{key_name} must match {key_pattern}"}), 400
        try:
            payload = loader(key)
            payload["saved_periods"] = db.list_rule_periods(*list_args)
            return jsonify(payload)
        except Exception as e:
            _log(f"[{key_name} RULES LOAD ERROR]\n" + traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    body = request.json or {}
    key = str(body.get(key_name) or "").strip()
    if not re.fullmatch(key_pattern, key):
        return jsonify({"error": f"{key_name} must match {key_pattern}"}), 400
    try:
        args = [key, body.get("rules") or {}]
        for ck in child_keys:
            args.append(body.get(ck) or [])
        saver(*args, user["username"])
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log(f"[{key_name} RULES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/anp-rules", methods=["GET", "POST"])
@login_required
def anp_rules_api():
    return _rules_endpoint(db.get_anp_rules, db.save_anp_rules,
                           "effective_from", r"\d{4}-\d{2}", ["tiers"],
                           ("anp_rules", "effective_from"))


@app.route("/api/ega-rules", methods=["GET", "POST"])
@login_required
def ega_rules_api():
    """EGA/ESA rules are keyed on year AND agent type: Internal and Outsource
    run the same award on different EP thresholds."""
    src = request.args if request.method == "GET" else (request.json or {})
    agent_type = str(src.get("agent_type") or "internal").strip().lower()
    if agent_type not in ("internal", "outsource"):
        return jsonify({"error": "agent_type must be internal or outsource"}), 400

    if request.method == "GET":
        year = (request.args.get("year") or "").strip()
        if not re.fullmatch(r"\d{4}", year):
            return jsonify({"error": "year must be YYYY"}), 400
        try:
            payload = db.get_ega_rules(year, agent_type)
            payload["saved_periods"] = db.list_rule_periods("ega_rules", "year")
            return jsonify(payload)
        except Exception as e:
            _log("[EGA RULES LOAD ERROR]\n" + traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    body = request.json or {}
    year = str(body.get("year") or "").strip()
    if not re.fullmatch(r"\d{4}", year):
        return jsonify({"error": "year must be YYYY"}), 400
    try:
        db.save_ega_rules(year, body.get("rules") or {}, body.get("months") or [],
                          user["username"], agent_type=agent_type)
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[EGA RULES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/production-bonus-rules", methods=["GET", "POST"])
@login_required
def production_bonus_rules_api():
    return _rules_endpoint(db.get_production_bonus_rules, db.save_production_bonus_rules,
                           "effective_from", r"\d{4}-\d{2}", [],
                           ("production_bonus_rules", "effective_from"))


@app.route("/api/contest-invoices")
@login_required
def contest_invoices_api():
    """Candidate invoices for a contest month, for picking cases directly instead
    of typing an amount and letting the matcher guess.

    Reuses the contest module's own query so agent-name resolution is identical
    to what the calculation sees. Filtered on 1st payment date, since that is
    what decides the month a case closed in; invoices with no payment yet are
    returned with paid=false so they can still be chosen deliberately.
    """
    month = (request.args.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify({"error": "month must be YYYY-MM"}), 400
    needle = (request.args.get("agent") or "").strip().lower()
    limit = min(int(request.args.get("limit") or 400), 1000)

    try:
        contest_path = REPO_ROOT / "6. Monthly Contest" / "3. Python Script" / "monthly_contest.py"
        contest_mod = build_commission_pack._load_module("monthly_contest", contest_path)
        try:
            token, _, _ = build_commission_pack._resolve_proxy_credentials()
        except Exception:
            token = ""
        # The pack's resolver depends on env/token files that are not always
        # populated; the contest module has its own loader with a fallback.
        if not token:
            token = contest_mod.load_auth_token()
        df = contest_mod.fetch_invoices_from_db(token)
    except Exception as e:
        _log("[CONTEST INVOICES ERROR]\n" + traceback.format_exc())
        return jsonify({"invoices": [], "error": str(e)}), 500

    import pandas as _pd
    paid = _pd.to_datetime(df["1st_payment_date"], errors="coerce")
    in_month = paid.dt.strftime("%Y-%m") == month

    rows = []
    for _, r in df[in_month].iterrows():
        agent = str(r.get("agent_name") or "").strip()
        customer = str(r.get("customer_name") or "").strip()
        if needle and needle not in agent.lower() and needle not in customer.lower():
            continue
        first_pay = r.get("1st_payment_date")
        rows.append({
            "id": r.get("id"),
            "invoice_number": r.get("invoice_number"),
            "agent": agent,
            "customer": customer,
            "invoice_date": str(r.get("invoice_date") or "")[:10],
            "first_payment_date": str(first_pay or "")[:10],
            "paid": bool(_pd.notnull(first_pay)),
            "sales_price": float(r.get("inv_total_amount") or 0),
            "system_price": float(r.get("pkg_price") or 0) or float(r.get("inv_total_amount") or 0),
        })
    rows.sort(key=lambda x: (x["agent"].lower(), x["first_payment_date"]))
    return jsonify({"month": month, "count": len(rows), "invoices": rows[:limit]})


@app.route("/api/contest-rules/list")
@login_required
def contest_rules_list_api():
    """Every configured month, for the Data page landing table."""
    try:
        return jsonify({"sets": db.list_contest_rule_sets()})
    except Exception as e:
        _log("[CONTEST RULES LIST ERROR]\n" + traceback.format_exc())
        return jsonify({"sets": [], "error": str(e)}), 500


@app.route("/api/contest-rules", methods=["GET", "POST"])
@login_required
def contest_rules_api():
    """Monthly Contest rule set for one month: per-team targets and handicaps
    plus the scoring and award amounts. GET on a month that has not been set up
    returns the framework defaults with saved=false, so the page can be filled
    in and saved as a new month."""
    if request.method == "GET":
        month = (request.args.get("month") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return jsonify({"error": "month must be YYYY-MM"}), 400
        try:
            payload = db.get_contest_rules(month)
            payload["saved_months"] = db.list_contest_rule_months()
            return jsonify(payload)
        except Exception as e:
            _log("[CONTEST RULES LOAD ERROR]\n" + traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    body = request.json or {}
    month = str(body.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify({"error": "month must be YYYY-MM"}), 400

    rules = body.get("rules") or {}
    teams = body.get("teams") or []

    # Targets and handicaps drive award money, so a typo that Postgres would
    # silently store as text is rejected here instead.
    for team in teams:
        for field in ("original_target", "handicap"):
            raw = str(team.get(field) or "0").replace(",", "").strip()
            try:
                float(raw)
            except ValueError:
                return jsonify({
                    "error": f"{team.get('team')}: {field.replace('_', ' ')} must be a number"
                }), 400
            team[field] = raw

    roster = body.get("roster") or []
    try:
        db.save_contest_rules(month, rules, teams, user["username"], roster=roster)
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[CONTEST RULES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent-ic", methods=["POST"])
@login_required
def agent_ic_api():
    """Set one agent's IC No, so it can be corrected straight from the Monthly
    Commission Slip instead of only from the Data page's roles table.

    save_agent_roles() replaces the whole table, so this reads every row back,
    edits the one that matches, and writes the full set again — anything else
    would wipe the rest of the hierarchy.
    """
    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    body = request.json or {}
    agent = str(body.get("agent") or "").strip()
    ic_no = str(body.get("ic_no") or "").strip()
    if not agent:
        return jsonify({"error": "Agent name is required"}), 400

    try:
        rows = db.list_agent_roles()
        target = next(
            (r for r in rows
             if str(r.get("agent") or "").strip().lower() == agent.lower()
             and not r.get("hidden")),
            None,
        )
        if target is not None:
            target["ic_no"] = ic_no
        else:
            # No hierarchy row for this agent yet (their name came straight from
            # Postgres); create the minimal row that carries the IC.
            rows.append({
                "agent": agent,
                "ic_no": ic_no,
                "effective_from": time.strftime("%Y-%m"),
            })
        db.save_agent_roles(rows, user["username"])
        _rates_module().reset_cache()
        clear_commission_cache()
        return jsonify({"status": "success", "ic_no": ic_no})
    except Exception as e:
        _log("[AGENT IC SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent-roles/seed", methods=["POST"])
@login_required
def agent_roles_seed_api():
    """Pull the agent list from Postgres (name, agent type, branch) so the
    hierarchy only needs Role and Reports To filled in by hand. Never
    overwrites an existing row — only adds agents not already listed."""
    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    try:
        month = (request.json or {}).get("month") or "2026-01"
        mod = _basic_module()
        mod._load_dotenv()
        proxy_url = mod._normalize_proxy_url(os.environ.get("PG_PROXY_URL")) or             "https://pg-proxy-production.up.railway.app/api/sql"
        db_name = os.environ.get("PG_PROXY_DB") or os.environ.get("PG_DB_NAME")
        token = os.environ.get("PG_PROXY_TOKEN")
        if not token:
            return jsonify({"error": "PG_PROXY_TOKEN is not configured"}), 400

        # Query Postgres with a schema-correct single query that joins user and agent tables,
        # filters status from access_level, and excludes blocked users.
        sql = """
            SELECT DISTINCT ON (COALESCE(au.link_key, LOWER(BTRIM(au.name))))
                   au.name, au.agent_type, au.access_level, au.branch, au.start_date
            FROM (
              -- Users
              SELECT u.bubble_id AS link_key,
                     u.name,
                     u.agent_type,
                     u.access_level,
                     u.main_department AS branch,
                     u.created_at AS start_date,
                     1 AS pri
              FROM "user" u
              WHERE COALESCE(BTRIM(u.name), '') <> ''
                -- Exclude blocked
                AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                -- Filter tags/type
                AND (
                  LOWER(COALESCE(u.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.access_level::text, '')) ~ '(internal|outsource|sales)'
                )

              UNION ALL

              -- Agents joined with users. Same link_key trick as the pg-list
              -- query above: merge with the linked user's row by bubble_id
              -- when linked_user_login resolves to one, so a name typo on
              -- either side can no longer seed a duplicate agent.
              SELECT u.bubble_id AS link_key,
                     ag.name,
                     COALESCE(ag.agent_type, u.agent_type) AS agent_type,
                     u.access_level,
                     u.main_department AS branch,
                     COALESCE(ag.created_date, u.created_at) AS start_date,
                     2 AS pri
              FROM agent ag
              LEFT JOIN "user" u ON u.bubble_id = ag.linked_user_login
              WHERE COALESCE(BTRIM(ag.name), '') <> ''
                -- Exclude blocked if user is blocked
                AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                -- Filter tags/type
                AND (
                  LOWER(COALESCE(ag.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.agent_type, '')) ~ '(internal|outsource|sales)'
                  OR LOWER(COALESCE(u.access_level::text, '')) ~ '(internal|outsource|sales)'
                )
            ) au
            ORDER BY COALESCE(au.link_key, LOWER(BTRIM(au.name))),
                     CASE WHEN COALESCE(au.agent_type, '') = '' THEN 1 ELSE 0 END,
                     au.pri
        """
        res = mod._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                             sql=sql, params=[])
        rows = res.get("rows") or res.get("data") or []

        valid_pg_names = {str(r.get("name") or "").strip().lower() for r in rows if r.get("name")}

        existing = db.list_agent_roles()
        # Prune seeded placeholder rows that are no longer in Postgres's active internal/outsource list
        merged = []
        for ex in existing:
            ex_name = str(ex.get("agent") or "").strip().lower()
            ex_hier = str(ex.get("hierarchy") or "").strip()
            ex_rep = str(ex.get("reports_to") or "").strip()
            # A hidden row is a deliberate exclusion made through the roles
            # grid's Delete button, not an unfilled placeholder. It must
            # survive this prune even after the agent drops out of Postgres
            # entirely — otherwise the exclusion is erased the moment they
            # leave, and silently undone if they are ever re-tagged again
            # later under the same name.
            if (not ex.get("hidden") and not ex_hier and not ex_rep
                    and ex_name not in valid_pg_names):
                continue
            merged.append(ex)

        known = {str(r.get("agent") or "").strip().lower() for r in merged}
        added = 0
        for r in rows:
            name = str(r.get("name") or "").strip()
            if not name or name.lower() in known:
                continue
            
            raw_type = str(r.get("agent_type") or "").strip()
            access_level = r.get("access_level") or []
            
            # Normalise agent type label
            low_type = raw_type.lower()
            access_text = " ".join(access_level).lower()
            combined_text = f"{low_type} {access_text}"
            
            # Same rule as the pg-list endpoint: only "internal" and "outsource"
            # name a rate table. Seeding is the worse place to guess, because the
            # guess is written to SQLite and then outranks Postgres forever after
            # (a saved type is treated as a human decision), so a bare "sales"
            # tag is stored blank and shown as needing review instead.
            if "outsource" in combined_text:
                atype = "Outsource"
            elif "internal" in combined_text:
                atype = "Internal"
            else:
                atype = ""
                
            branch = str(r.get("branch") or "").strip()
            if "branch" not in branch.lower():
                branch = ""
            
            start_date_raw = r.get("start_date")
            start_date = ""
            if start_date_raw:
                try:
                    start_date = str(start_date_raw)[:10]
                except Exception:
                    pass
                    
            merged.append({
                "effective_from": month, "agent": name,
                "agent_type": atype,
                "hierarchy": "", "reports_to": "", "branch": branch,
                "remarks": "seeded from Postgres",
                "start_date": start_date,
            })
            known.add(name.lower())
            added += 1

        db.save_agent_roles(merged, user["username"])
        return jsonify({"status": "success", "added": added, "total": len(merged)})
    except Exception as e:
        _log("[AGENT ROLES SEED ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


def _basic_module():
    """Import (or fetch) the internal basic commission module â€” it owns the
    Postgres proxy helpers."""
    mod = sys.modules.get("full_internal_basic_commission")
    if mod is None:
        d = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if d not in sys.path:
            sys.path.append(d)
        import full_internal_basic_commission as mod
    return mod


def _nfp_module():
    """Import (or fetch the already-imported) net_floor_prices module."""
    mod = sys.modules.get("net_floor_prices")
    if mod is None:
        nfp_dir = str(REPO_ROOT / "2. NFP Commission" / "3. Python script")
        if nfp_dir not in sys.path:
            sys.path.append(nfp_dir)
        import net_floor_prices as mod
    return mod


# Parsed uploads awaiting admin confirmation: token -> {rows, months, ts}
_NFP_UPLOAD_PENDING = {}


def _nfp_sheet_months(sheet_name, month_names):
    """Months ('YYYY-MM') a schedule sheet covers, e.g. 'NOV and DEC 2025' -> two."""
    import re as _re
    sn = sheet_name.upper()
    year_m = _re.search(r"(\d{4})", sn)
    year = int(year_m.group(1)) if year_m else 2026
    lookup = dict(month_names)
    lookup[7] = "JUL"
    months = {num for num, name in lookup.items() if name in sn}
    if "JULY" in sn:
        months.add(7)
    return [f"{year:04d}-{m:02d}" for m in sorted(months)]


@app.route("/api/nfp-prices/upload", methods=["POST"])
@login_required
def nfp_prices_upload():
    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403

    mode = request.args.get("mode", "preview")
    try:
        if mode == "confirm":
            token = str((request.json or {}).get("token") or "")
            pending = _NFP_UPLOAD_PENDING.pop(token, None)
            if not pending:
                return jsonify({"error": "Upload expired â€” please upload the file again."}), 400
            replaced = db.replace_nfp_prices_for_months(
                pending["months"], pending["rows"], user["username"])
            try:
                _nfp_module().reset_db_prices_cache()
            except Exception:
                pass
            clear_commission_cache()
            return jsonify({"status": "success", "rows": len(pending["rows"]),
                            "months": pending["months"], "replaced": replaced})

        # preview: parse the uploaded xlsx with the same parser the engine uses
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "No file received"}), 400
        if not f.filename.lower().endswith(".xlsx"):
            return jsonify({"error": "Only .xlsx files are supported"}), 400

        nfp = _nfp_module()
        tmp_path = Path(tempfile.mkstemp(suffix=".xlsx")[1])
        try:
            f.save(str(tmp_path))
            parsed = nfp.parse_schedule_workbook_rows(tmp_path, source_label=f"upload {f.filename}")
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

        rows, summary_by_sheet = [], {}
        for r in parsed:
            months = _nfp_sheet_months(r["sheet"], nfp.MONTH_NAMES)
            if not months:
                continue
            info = summary_by_sheet.setdefault(
                r["sheet"], {"sheet": r["sheet"], "months": months, "tables": {}})
            tkey = (r["panel_rating"], r["inverter_type"])
            info["tables"][tkey] = info["tables"].get(tkey, 0) + 1
            for month in months:
                row = {k: r[k] for k in ("panel_rating", "table_no", "panels", "final_price",
                                          "final_with_tng", "package_price", "tng_rebate",
                                          "inverter_type", "power_system", "source_sheet")}
                row["columns_json"] = (json.dumps(r["columns"], ensure_ascii=False)
                                        if r.get("columns") else None)
                row["month"] = month
                rows.append(row)
        summary = []
        for info in summary_by_sheet.values():
            summary.append({
                "sheet": info["sheet"], "months": info["months"],
                "tables": [{"rating": f"{rating}W {itype}", "rows": count}
                            for (rating, itype), count in sorted(info["tables"].items())],
            })

        if not rows:
            return jsonify({"error": "No price tables recognized in this file. "
                            "Sheets must be named with month + year (e.g. 'AUG 2026') "
                            "and use the standard schedule layout."}), 400

        months_all = sorted({r["month"] for r in rows})
        existing = [p for p in db.list_nfp_prices() if p["month"] in months_all]
        token = uuid.uuid4().hex
        _NFP_UPLOAD_PENDING[token] = {"rows": rows, "months": months_all, "ts": time.time()}
        # Drop stale pending uploads (>30 min)
        for k in [k for k, v in _NFP_UPLOAD_PENDING.items() if time.time() - v["ts"] > 1800]:
            _NFP_UPLOAD_PENDING.pop(k, None)
        return jsonify({"token": token, "summary": summary, "months": months_all,
                        "total_rows": len(rows), "replaces": len(existing)})
    except Exception as e:
        _log("[NFP UPLOAD ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/nfp-prices")
@login_required
def nfp_prices_api():
    """Net Floor Price list transferred from the monthly Excel/JSON schedules."""
    try:
        return jsonify(db.list_nfp_prices())
    except Exception as e:
        _log("[NFP PRICES ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/basic-rates/resolved")
@login_required
def basic_rates_resolved_api():
    """Preview which rate/rule value actually applies for a month, and whether it
    comes from a Data page entry ('system') or the Google Sheet fallback ('sheet')."""
    # Accept YYYY-MM string (new) or plain integer month (legacy fallback)
    raw_month = str(request.args.get("month", "")).strip()
    is_all = raw_month.lower() in ("all", "all months")
    import re as _re
    _ym_match = _re.match(r"^(\d{4})-(\d{2})$", raw_month)
    if _ym_match:
        target_year = int(_ym_match.group(1))
        month = int(_ym_match.group(2))
        target = f"{target_year:04d}-{month:02d}"
    else:
        month = int(raw_month) if str(raw_month).isdigit() else 7
        target_year = 2026
        target = "all" if is_all else f"{target_year:04d}-{month:02d}"

    def _eff_covers(eff_str, tgt):
        """Return True if eff_str (single YYYY-MM or 'YYYY-MM to YYYY-MM') covers tgt."""
        if tgt == "all":
            return True
        s = str(eff_str or "").strip()
        if not s:
            return False
        if " to " in s or ".." in s:
            parts = s.split(" to ") if " to " in s else s.split("..")
            start = parts[0].strip()
            end = parts[1].strip() if len(parts) > 1 else "9999-12"
            return start <= tgt <= end
        return s <= tgt

    try:
        bcr = _rates_module()
        if is_all:
            rates = []
            db_rows = list(db.list_commission_rates()) + list(db.list_basic_rates())
            if db_rows:
                for r in db_rows:
                    if str(r.get("rate_type") or "Basic Commission").strip() != "Basic Commission":
                        continue
                    remarks = str(r.get("remarks") or "").strip().lower()
                    r_val = r.get("rate_pct")
                    rate_str = str(r_val or "").strip()
                    if remarks == "deleted" or (not rate_str and not str(r.get("amount_rm") or "").strip() and not str(r.get("override_rate_pct") or "").strip()):
                        continue
                    try:
                        rate_val = float(r_val) if r_val is not None and rate_str != "" else 0.0
                    except Exception:
                        rate_val = 0.0
                    rates.append({
                        "id": r.get("id"),
                        "agent_type": str(r.get("agent_type") or "").strip(),
                        "hierarchy": str(r.get("hierarchy") or "").strip(),
                        "agent": str(r.get("agent") or "").strip(),
                        "property_type": str(r.get("property_type") or "").strip(),
                        "rate_pct": rate_val,
                        "override_rate_pct": str(r.get("override_rate_pct") or "").strip(),
                        "override_from": str(r.get("override_from") or "").strip(),
                        "effective_from": str(r.get("effective_from") or "").strip(),
                        "trigger_pct": str(r.get("trigger_pct") or "").strip(),
                        "condition": str(r.get("condition") or "").strip(),
                        "rule_type": str(r.get("rule_type") or "").strip(),
                        "amount_rm": str(r.get("amount_rm") or "").strip(),
                        "source": str(r.get("source") or "unified").strip(),
                    })
            if not rates:
                combos = [
                    ("Internal", "Executive", ""),
                    ("Internal", "Senior", ""),
                    ("Outsource", "OUM", ""),
                    ("Outsource", "OSA/OSA1", ""),
                ]
                for agent_type, hierarchy, agent in combos:
                    rate, source, eff = bcr.get_basic_rate_detail(
                        agent_type, hierarchy, month, agent=agent or None, year=target_year)
                    rates.append({
                        "agent_type": agent_type,
                        "hierarchy": hierarchy,
                        "agent": agent,
                        "rate_pct": float(rate * 100),
                        "effective_from": eff,
                        "trigger_pct": "",
                        "condition": "",
                        "source": source,
                    })
        else:
            # Legacy pairs always shown; plus every role-level system entry covering
            # the month, and agent-specific exceptions (e.g. Sunny) shown as own rows.
            combos = [
                ("Internal", "Executive", "", ""),
                ("Internal", "Senior", "", ""),
                ("Outsource", "OUM", "", ""),
                ("Outsource", "OSA/OSA1", "", ""),
            ]
            deleted_combos = set()
            system_combos = []
            for r in db.list_commission_rates():
                if str(r.get("rate_type") or "Basic Commission").strip() != "Basic Commission":
                    continue
                if not _eff_covers(r.get("effective_from"), target):
                    continue
                at = str(r.get("agent_type") or "").strip()
                h = str(r.get("hierarchy") or "").strip()
                ag = str(r.get("agent") or "").strip()
                pt = str(r.get("property_type") or "").strip()
                combo = (at, h, ag, pt)
                rate_str = str(r.get("rate_pct") or "").strip()
                remarks = str(r.get("remarks") or "").strip().lower()
                if remarks == "deleted" or (not rate_str and not str(r.get("amount_rm") or "").strip() and not str(r.get("override_rate_pct") or "").strip()):
                    deleted_combos.add(combo)
                    deleted_combos.add((at, h, ag, ""))
                else:
                    if combo not in system_combos:
                        system_combos.append(combo)

            if system_combos or deleted_combos:
                base_roles_in_system = {(at, h, ag) for at, h, ag, pt in system_combos} | {(at, h, ag) for at, h, ag, pt in deleted_combos}
                combos = [c for c in combos if (c[0], c[1], c[2]) not in base_roles_in_system]
                for c in system_combos:
                    if c not in combos and c not in deleted_combos:
                        combos.append(c)

            # Per-row payout conditions entered on the Data page, keyed by combo, so
            # a row shows ITS OWN condition rather than the global rule blob.
            own_cond = {}
            for r in db.list_commission_rates():
                eff = str(r.get("effective_from") or "")
                if not _eff_covers(eff, target):
                    continue
                k = (str(r.get("agent_type") or "").strip(),
                     str(r.get("hierarchy") or "").strip(),
                     str(r.get("agent") or "").strip(),
                     str(r.get("property_type") or "").strip())
                cond_text = str(r.get("condition") or "").strip()
                trig_text = str(r.get("trigger_pct") or "").strip()
                prop_type = str(r.get("property_type") or "").strip()
                ovr_pct = str(r.get("override_rate_pct") or "").strip()
                ovr_from = str(r.get("override_from") or "").strip()
                own_cond[k] = (cond_text, trig_text, prop_type, ovr_pct, ovr_from)

            rates = []
            for agent_type, hierarchy, agent, prop_type in combos:
                # A row's Agent Name cell can list several agents ("A, B") — they
                # all share the one rate, so resolve with the first name. Passing
                # the whole list matches nothing and the row would silently fall
                # back to the role rate.
                lookup_agent = agent.split(",")[0].strip() if agent else ""
                rate, source, eff = bcr.get_basic_rate_detail(
                    agent_type, hierarchy, month, agent=lookup_agent or None, property_type=prop_type or None, year=target_year)
                c_val, t_val, p_val, ovr_p, ovr_f = own_cond.get((agent_type, hierarchy, agent, prop_type), ("", "", prop_type, "", ""))
                rates.append({
                    "agent_type": agent_type,
                    "hierarchy": hierarchy,
                    "agent": agent,
                    "property_type": p_val or prop_type,
                    "rate_pct": float(rate * 100),
                    "override_rate_pct": ovr_p,
                    "override_from": ovr_f,
                    "effective_from": eff,
                    "condition": c_val,
                    "trigger_pct": t_val,
                    "source": source,
                })
        rates.sort(key=lambda x: (x["agent_type"] != "Internal", x["hierarchy"], x["agent"]))
        # All rules resolved for the month; the cap keeps its built-in RM300 default
        rule_units = {"basic_commission_cap": "RM"}
        best_rules = {}
        for r in db.list_rule_settings():
            eff = str(r.get("effective_from") or "")
            if not _eff_covers(eff, target):
                continue
            key = str(r.get("rule_key") or "")
            if key not in best_rules or eff > str(best_rules[key].get("effective_from") or ""):
                best_rules[key] = r
        # Unified-table rows expressed in the same rule_key shape, so the read
        # view shows one set of payout bullets no matter which table they came
        # from. A unified row wins over a legacy rule of the same key â€” the same
        # precedence the calculation engine applies.
        for r in db.list_commission_rates():
            eff = str(r.get("effective_from") or "")
            if not _eff_covers(eff, target):
                continue
            cell = lambda k: str(r.get(k) or "").strip()
            rate_val = cell("rate_pct") or cell("override_rate_pct") or cell("profit_sharing_rate_pct")
            rtype = cell("rule_type").lower()
            amt = cell("amount_rm")
            trig = cell("trigger_pct")

            if rtype in ("advance", "multi-stage") or amt:
                key = "basic_commission_cap"
                synth = {
                    "value": float(amt) if amt else 300.0,
                    "label": "Advance â€” 1st payout (RM, once)",
                    "rule_type": "Advance",
                    "trigger_pct": trig.split(",")[0].strip() if (trig and "," in trig) else (trig or "5"),
                    "effective_from": eff,
                    "_unified": True
                }
                prev = best_rules.get(key)
                if prev is None or not prev.get("_unified") or eff > str(prev.get("effective_from") or ""):
                    best_rules[key] = synth

            if rtype in ("payout", "multi-stage") or (not rate_val and trig):
                key = "basic_balance_payout_trigger"
                val_str = trig.split(",")[-1].strip() if (trig and "," in trig) else (trig or "75")
                synth = {
                    "value": float(val_str) if val_str else 75.0,
                    "label": "Payout â€” balance",
                    "rule_type": "Payout",
                    "trigger_pct": val_str,
                    "effective_from": eff,
                    "_unified": True
                }
                prev = best_rules.get(key)
                if prev is None or not prev.get("_unified") or eff > str(prev.get("effective_from") or ""):
                    best_rules[key] = synth

        rules = []
        for key, r in sorted(best_rules.items()):
            try:
                val = float(str(r.get("value")).strip())
            except (TypeError, ValueError):
                continue
            label = r.get("label") or key
            unit = rule_units.get(key)
            if unit is None:
                ll = label.rstrip().lower()
                if ll.endswith("(rm)"):
                    unit = "RM"
                elif ll.endswith("(months)"):
                    unit = "months"
                elif ll.endswith("(yes/no)"):
                    unit = "yesno"
                else:
                    unit = "%"
            rules.append({
                "rule_key": key,
                "rule_type": r.get("rule_type") or "",
                "label": label,
                "value": val,
                "trigger_pct": r.get("trigger_pct") or "",
                "agent_type": r.get("agent_type") or "",
                "hierarchy": r.get("hierarchy") or "",
                "agent": r.get("agent") or "",
                "property_type": r.get("property_type") or "",
                "invoice_date_from": r.get("invoice_date_from") or "",
                "unit": unit,
                "source": "unified" if r.get("_unified") else "legacy",
            })
        if "basic_commission_cap" not in best_rules:
            rules.insert(0, {
                "rule_key": "basic_commission_cap", "rule_type": "Advance",
                "label": "Advance â€” 1st payout (RM, once)", "value": 300.0,
                "trigger_pct": "5", "agent_type": "", "hierarchy": "", "agent": "",
                "property_type": "", "unit": "RM", "source": "default",
            })

        # NFP tier rates: resolve per (agent_type, role, agent, condition) so each
        # tier keeps its own effective-dated lineage.
        best_nfp = {}
        for src, rows in (("legacy", db.list_basic_rates()),
                          ("unified", db.list_commission_rates())):
            for r in rows:
                if str(r.get("rate_type") or "").strip() != "Net Floor Price Rate":
                    continue
                eff = str(r.get("effective_from") or "")
                if not eff or eff > target:
                    continue
                key = (str(r.get("agent_type") or "").strip(),
                       str(r.get("hierarchy") or "").strip(),
                       str(r.get("agent") or "").strip(),
                       str(r.get("condition") or "").strip())
                prev = best_nfp.get(key)
                # Unified rows outrank legacy ones; among equals the latest wins.
                if prev is None or (src == "unified" and prev.get("_src") != "unified") \
                        or eff > str(prev.get("effective_from") or ""):
                    r = dict(r)
                    r["_src"] = src
                    best_nfp[key] = r
        nfp_rates = []
        for r in best_nfp.values():
            try:
                pct = float(str(r.get("rate_pct")).replace("%", "").strip())
            except (TypeError, ValueError):
                continue
            nfp_rates.append({
                "agent_type": r.get("agent_type"),
                "hierarchy": r.get("hierarchy"),
                "agent": r.get("agent") or "",
                "condition": r.get("condition") or "",
                "rate_pct": pct,
                "source": r.get("_src") or "legacy",
            })
        nfp_rates.sort(key=lambda x: (x["agent_type"], x["condition"]))

        return jsonify({"month": month, "rates": rates, "rules": rules, "nfp_rates": nfp_rates})
    except Exception as e:
        _log("[RESOLVED RATES ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/customers/search", methods=["GET"])
@login_required
def customer_search_api():
    query = str(request.args.get("q", "") or "").strip().lower()
    limit = request.args.get("limit", 25, type=int)
    if limit <= 0:
        limit = 25
    limit = min(limit, 100)

    try:
        with _disk_cache_lock:
            cached_custs = _data_cache.get('all_customers')
        if not cached_custs:
            token, base_url, db_name = build_commission_pack._resolve_proxy_credentials()
            if not token:
                return jsonify({"error": "Postgres proxy token not found. Set PG_PROXY_TOKEN in .env"}), 500
            anp_mod = sys.modules.get("int_anp_commission")
            if anp_mod is None:
                anp_path = build_commission_pack._anp_script_path()
                anp_mod = build_commission_pack._load_module("int_anp_commission", anp_path)
            client = anp_mod.PostgresProxyClient(base_url.rstrip("/").replace("/api/sql", ""), token, db_name)
            cached_custs = client.query("SELECT customer_id, name FROM customer ORDER BY name")
            with _disk_cache_lock:
                _data_cache['all_customers'] = cached_custs
                save_disk_cache()

        results = []
        for row in cached_custs:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if not query or query in name.lower():
                results.append({
                    "customer_id": row.get("customer_id"),
                    "name": name
                })
                if len(results) >= limit:
                    break
        return jsonify(results)
    except Exception as e:
        _log("[CUSTOMER SEARCH ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents/search", methods=["GET"])
@login_required
def agent_search_api():
    query = str(request.args.get("q", "") or "").strip()
    agent_type = str(request.args.get("agent_type", "") or "").strip().lower()
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 5, type=int)
    limit = request.args.get("limit", 25, type=int)
    if limit <= 0:
        limit = 25
    limit = min(limit, 100)

    try:
        with _disk_cache_lock:
            cached_agents = _data_cache.get('all_agents')
        if not cached_agents:
            token, base_url, db_name = build_commission_pack._resolve_proxy_credentials()
            if not token:
                return jsonify({"error": "Postgres proxy token not found. Set PG_PROXY_TOKEN in .env"}), 500
            anp_mod = sys.modules.get("int_anp_commission")
            if anp_mod is None:
                anp_path = build_commission_pack._anp_script_path()
                anp_mod = build_commission_pack._load_module("int_anp_commission", anp_path)
            client = anp_mod.PostgresProxyClient(base_url.rstrip("/").replace("/api/sql", ""), token, db_name)
            cached_agents = client.query(
                """
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
                """
            )
            with _disk_cache_lock:
                _data_cache['all_agents'] = cached_agents
                save_disk_cache()

        commission_pool = _commission_agent_name_pool(year, month, agent_type)
        results_by_key = {}
        normalized_query = _normalize_search_text(query)

        def score(name: str, row_agent_type: str, source: str) -> tuple[int, int, int]:
            normalized_name = _normalize_search_text(name)
            if normalized_query and normalized_query not in normalized_name:
                return (-1, -1, -1)
            clean_name = str(name).strip().lower()
            row_type = str(row_agent_type or "").strip().lower()
            # Same precedence the report itself now uses: the Agent Roles &
            # Hierarchy page decides, and Postgres' agent_type only answers for
            # agents that page says nothing about. Otherwise an agent whose
            # agent_type is blank in Bubble is unfindable under the agent type
            # their commission is actually calculated as.
            try:
                override = _rates_module().get_agent_type_override(name)
            except Exception:
                override = None
            if override:
                is_internal = override == "internal" and "gan lai soon" not in clean_name
            else:
                is_internal = row_type in {"internal", "full time"} and "gan lai soon" not in clean_name
            if agent_type == "internal" and not is_internal:
                return (-1, -1, -1)
            if agent_type == "outsource" and is_internal:
                return (-1, -1, -1)
            preferred = 2 if agent_type else 1
            source_score = 1 if source.startswith("commission") else 0
            return (preferred, source_score, len(normalized_name))

        def maybe_add(row_name: str, row_agent_type, agent_id, source: str):
            clean_name = str(row_name or "").strip()
            if not clean_name:
                return
            key = _normalize_search_text(clean_name)
            if not key:
                return
            row_type = str(row_agent_type or "").strip().lower()
            cur_score = score(clean_name, row_type, source)
            if cur_score[0] < 0:
                return
            existing = results_by_key.get(key)
            if existing is None or cur_score > existing["_score"]:
                results_by_key[key] = {
                    "agent_id": agent_id,
                    "name": clean_name,
                    "agent_type": row_agent_type,
                    "_score": cur_score,
                }

        for row in cached_agents:
            maybe_add(row.get("name"), row.get("agent_type"), row.get("bubble_id"), "agent-table")

        for row in commission_pool:
            maybe_add(row.get("name"), row.get("agent_type"), row.get("agent_id"), row.get("source", "commission"))

        results = sorted(results_by_key.values(), key=lambda r: r["_score"], reverse=True)
        results = [{k: v for k, v in row.items() if k != "_score"} for row in results[:limit]]
        return jsonify(results)
    except Exception as e:
        _log("[AGENT SEARCH ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # debug=False is important: debug=True activates Werkzeug's interactive
    # debugger which returns HTML error pages and bypasses our JSON error handlers.
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)



