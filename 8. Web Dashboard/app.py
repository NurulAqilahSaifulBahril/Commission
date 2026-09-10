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
    """Write the cache atomically: full pickle to a temp file alongside it, then
    one rename over the real path.

    Pickling straight into CACHE_FILE truncates it on open and only refills it
    as the dump proceeds, so anything that interrupts the write -- a kill, a
    crash, the machine losing power -- leaves a half-written file that
    unpickles as EOFError. load_disk_cache() then falls back to an empty cache
    and the next request rebuilds from scratch, silently, for minutes.
    os.replace() is atomic on Windows and POSIX alike, so a reader sees either
    the previous good cache or the new one, never a partial one."""
    with _disk_cache_lock:
        tmp_path = None
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(CACHE_FILE.parent),
                                            prefix=CACHE_FILE.name + ".", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                pickle.dump(_data_cache, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CACHE_FILE)
            tmp_path = None
            _log("Saved cache to disk.")
        except Exception as e:
            _log(f"Failed to save cache to disk: {e}\n" + traceback.format_exc())
        finally:
            # A failed attempt must not leave stray .tmp files behind.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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

    _clear_derived_caches()

    _log("Cleared commission cache (raw proxy data preserved). Triggering background re-build...")
    start_prefetch(2026)


def _clear_derived_caches():
    """Drop every in-process cache built ON TOP of the commission data.

    These live in module globals rather than _data_cache, so the loop above
    never reached them and a rebuild left them standing. Two of them have no
    expiry at all, and the rest sit on a five-minute timer -- so after an admin
    edited a rate, the Sales Report went on showing the previous numbers with
    nothing on screen to say so.

    Only derived values are dropped. Raw proxy data is preserved, which is the
    whole point of clearing the commission cache rather than everything.
    """
    for cache in (_SALES_REPORT_PAYLOAD_CACHE, _FIRST_PAYMENT_CACHE,
                  _PAYMENTS_CACHE, _PAYMENT_ROWS_CACHE,
                  _LIVE_CUSTOMER_DATES_CACHE, _RESTATED_PLACEMENT_CACHE):
        cache.clear()


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


# Process-lifetime cache for _fetch_live_customer_dates: special cases are
# rare and their invoice/payment dates never change once paid, so there is no
# need to re-hit the proxy on every request that renders this customer's row.
_LIVE_CUSTOMER_DATES_CACHE: dict[str, dict] = {}
_RESTATED_PLACEMENT_CACHE: dict[tuple[str, float], dict] = {}


def _fetch_live_customer_dates(customer_name: str, invoice_number: str | None = None) -> dict:
    """Invoice/1st-payment/full-payment dates for a customer straight from the
    live DB, keyed the same way _organic_dates/_lines_dates return them.

    A hand-added special case (e.g. a Durapower-style restatement) can name a
    customer whose only invoice falls outside the report year -- the cached
    commission bundle _lines_dates reads from is scoped to that one year, so
    it never finds it. This looks the customer up directly, with no year
    restriction, and -- critically -- takes the full/last payment date from
    SUM'd payment rows rather than invoice.full_payment_date: that column
    goes stale whenever a payment is recorded through the newer payment app,
    which is exactly the kind of invoice a special case gets raised for.

    All three dates come back shifted from UTC to Asia/Kuala_Lumpur (+8h,
    no DST) before truncation to a calendar date -- Bubble-era rows store
    local midnight as 16:00 UTC the prior day, so truncating the raw UTC
    string reads one day early.

    `invoice_number`, when given, narrows the match to that one invoice
    instead of every invoice under the customer's name -- needed once a
    customer can have more than one invoice and only one of them is the
    restated deal this case is about.
    """
    invoice_number = str(invoice_number or "").strip()
    key = f"{customer_name.strip().lower()}|{invoice_number}"
    if not customer_name.strip():
        return {}
    if key in _LIVE_CUSTOMER_DATES_CACHE:
        return _LIVE_CUSTOMER_DATES_CACHE[key]

    out: dict = {}
    try:
        import urllib.request
        from datetime import timedelta

        token, proxy_url, db_name = build_commission_pack._resolve_proxy_credentials()
        if not token:
            return {}
        # The pg-proxy for this DB rejects the extended-query protocol (a %s
        # placeholder errors with "syntax error at or near \"%\""); every
        # other query in this codebase inlines its literals for the same
        # reason, so this does too, with the customer name's own quotes
        # doubled per standard SQL string-literal escaping.
        safe_customer = customer_name.replace("'", "''")
        invoice_filter = ""
        if invoice_number:
            safe_inv = invoice_number.replace("'", "''")
            invoice_filter = f" AND TRIM(i.invoice_number) = '{safe_inv}'"
        sql = f"""
            SELECT i.invoice_date, i."1st_payment_date" AS first_payment_date,
                   MAX(p.payment_date) AS live_full_pay,
                   i.full_payment_date AS stale_full_pay
            FROM invoice i
            LEFT JOIN customer c ON c.customer_id = i.linked_customer
            LEFT JOIN payment p ON p.linked_invoice = i.bubble_id
            WHERE i.is_deleted IS NOT TRUE
              AND LOWER(TRIM(COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name))) = LOWER(TRIM('{safe_customer}')){invoice_filter}
            GROUP BY i.bubble_id, i.invoice_date, i."1st_payment_date", i.full_payment_date
        """
        body = json.dumps({"db_name": db_name, "sql": sql, "params": []}).encode()
        req = urllib.request.Request(
            proxy_url, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())

        def _local_date(v) -> str:
            if not v or str(v).lower() in ("none", "null"):
                return ""
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00")) + timedelta(hours=8)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return str(v)[:10]

        inv_dates, first_dates, full_dates = set(), set(), set()
        for r in payload.get("rows") or []:
            d = _local_date(r.get("invoice_date"))
            if d:
                inv_dates.add(d)
            d = _local_date(r.get("first_payment_date"))
            if d:
                first_dates.add(d)
            d = _local_date(r.get("live_full_pay") or r.get("stale_full_pay"))
            if d:
                full_dates.add(d)

        cells: dict[str, str] = {}
        if inv_dates:
            cells["invoice_date"] = "<br/>".join(sorted(inv_dates))
        if first_dates:
            cells["first_payment_date"] = "<br/>".join(sorted(first_dates))
        if full_dates:
            cells["full_payment_date"] = "<br/>".join(sorted(full_dates))
        if cells:
            out = cells
    except Exception:
        out = {}

    _LIVE_CUSTOMER_DATES_CACHE[key] = out
    return out


def _resolve_restated_case_placement(invoice_number: str, restated_total: float) -> dict:
    """Where/how-paid a restated-total special case is, from real payments.

    A restated customer case (e.g. Durapower) names a deal total that isn't
    anywhere in the invoice data -- the invoice's own total_amount is the
    pre-restatement figure. This mirrors the running-total-vs-threshold
    pattern basic_commission_rates.milestone_sql_parts uses for the 100%
    milestone (same -0.01 tolerance, same MILESTONE_PAYMENT_EXCLUSIONS), but
    against `restated_total` instead of invoice.total_amount, and in Python
    rather than SQL since there is only ever one invoice to walk.

    Returns {"placed": True, "year", "month", "crossing_date", "pct_paid"}
    once cumulative payments reach the restated total, else
    {"placed": False, "pct_paid": ...} (optionally with "error").
    """
    invoice_number = str(invoice_number or "").strip()
    try:
        restated_total = float(restated_total)
    except (TypeError, ValueError):
        restated_total = 0.0
    if not invoice_number or restated_total <= 0:
        return {"placed": False, "pct_paid": 0.0, "error": "Missing invoice number or restated total"}

    cache_key = (invoice_number.lower(), restated_total)
    if cache_key in _RESTATED_PLACEMENT_CACHE:
        return _RESTATED_PLACEMENT_CACHE[cache_key]

    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import MILESTONE_PAYMENT_EXCLUSIONS

        safe_inv = invoice_number.replace("'", "''")
        rows = _pg_query(f"""
            SELECT p.id, p.payment_date, p.amount
            FROM payment p
            JOIN invoice i ON i.bubble_id = p.linked_invoice
            WHERE TRIM(i.invoice_number) = '{safe_inv}'
              AND p.id NOT IN ({MILESTONE_PAYMENT_EXCLUSIONS})
            ORDER BY p.payment_date ASC, p.id ASC
        """)
    except Exception:
        _log("[RESTATED CASE PLACEMENT ERROR]\n" + traceback.format_exc())
        return {"placed": False, "pct_paid": 0.0, "error": "Could not read payments"}

    def _local_date(v):
        if not v:
            return None
        try:
            from datetime import datetime, timedelta
            # Same +8h (Asia/Kuala_Lumpur, no DST) shift as _fetch_live_customer_dates:
            # Bubble-era rows store local midnight as 16:00 UTC the prior day, so a raw
            # UTC truncation would push a late-day payment into the wrong month.
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")) + timedelta(hours=8)
        except (ValueError, TypeError):
            return None

    running = 0.0
    crossing_dt = None
    for r in rows:
        try:
            amt = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        running += amt
        if crossing_dt is None and running >= restated_total - 0.01:
            crossing_dt = _local_date(r.get("payment_date"))

    pct_paid = round((running / restated_total * 100) if restated_total else 0.0, 1)
    result = ({"placed": False, "pct_paid": pct_paid} if crossing_dt is None else {
        "placed": True,
        "year": crossing_dt.year,
        "month": crossing_dt.month,
        "crossing_date": crossing_dt.strftime("%Y-%m-%d"),
        "pct_paid": pct_paid,
    })
    _RESTATED_PLACEMENT_CACHE[cache_key] = result
    return result


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
    safwan_i = idx("Safwan (RM)")
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

    # Safwan's own factory profit-sharing rate, keyed by (agent, customer),
    # from the same factory_rates bucket build_commission_pack.py reads for
    # organic invoices (get_dashboard_factory_rate in outsource_basic_commission.py).
    # A hand-added factory special case has no invoice for that lookup to run
    # against, so it must read the same table directly to land on the same
    # figure instead of always falling back to 0%.
    try:
        _factory_rate_rows = db.get_factory_rates_rows(str(year), str(month), agent_type)
    except Exception:
        _factory_rate_rows = []

    def _safwan_sharing_pct(agent_name: str, customer_name: str) -> float:
        want_agent = agent_name.strip().lower()
        want_cust = customer_name.strip().lower()
        for r in _factory_rate_rows:
            if str(r.get("agent") or "").strip().lower() == want_agent \
               and str(r.get("customer") or "").strip().lower() == want_cust:
                try:
                    return float(r.get("safwan_rate") or 0) / 100.0
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

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
            linked_invoice_number = str(case.get("linkedInvoiceNumber") or "").strip()
            restated_total_raw = case.get("restatedTotal")
            restated_total = float(restated_total_raw) if restated_total_raw else 0.0
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

        # Safwan's own factory profit-sharing cut: 0.5% of sales by default,
        # plus whatever rate was negotiated on the Factory "Profit Sharing"
        # popup for this (agent, customer) pair -- same formula as the organic
        # Factory path in build_commission_pack.py. He earns nothing on his
        # own invoices, and only Factory-package cases carry this at all.
        is_factory_case = "factory" in pkg.lower()
        safwan_comm = (sales_for_calc * (0.005 + _safwan_sharing_pct(agent, customer))
                       if (is_factory_case and agent.strip().lower() != "safwan") else 0.0)

        data_json = json.dumps({k: case.get(k) for k in db.SPECIAL_CASE_FIELDS})

        # Invoice/payment dates for this case's customer: their rows already in
        # the table first, else the cached commission lines (a hand-added
        # customer has no rows this month), else a direct live-DB lookup for a
        # customer whose only invoice falls outside this report's year (e.g. a
        # 2025 invoice restated into a 2026 month). A customer with none of
        # these keeps "-".
        organic_dates = _organic_dates(agent, customer) or _lines_dates(customer)
        if not organic_dates and date_cols:
            live = _fetch_live_customer_dates(customer, linked_invoice_number or None)
            if live:
                live_cells = {}
                if inv_date_i != -1 and live.get("invoice_date"):
                    live_cells[inv_date_i] = live["invoice_date"]
                if first_pay_i != -1 and live.get("first_payment_date"):
                    live_cells[first_pay_i] = live["first_payment_date"]
                if full_pay_i != -1 and live.get("full_payment_date"):
                    live_cells[full_pay_i] = live["full_payment_date"]
                if live_cells:
                    organic_dates = {"basic": live_cells}

        # A restated case's Full Payment Date is the date cumulative payments
        # crossed its own restated total -- what placed it in this month in
        # the first place -- not the invoice's raw last-payment date, which
        # can land later once an already-restated deal is overpaid. The
        # placement engine is the single source of truth for that date.
        if restated_total and linked_invoice_number and full_pay_i != -1:
            placement = _resolve_restated_case_placement(linked_invoice_number, restated_total)
            if placement.get("placed") and placement.get("crossing_date"):
                organic_dates.setdefault("basic", {})[full_pay_i] = placement["crossing_date"]
                organic_dates.setdefault("nfp", {})[full_pay_i] = placement["crossing_date"]

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
            if safwan_i != -1:
                row[safwan_i] = (_special_case_money(safwan_comm)
                                 if (safwan_comm and "basic" in comm_type.lower()) else "-")
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
{steps}
</ol>
<p class="muted">Details for IT: {error}</p>
</div></body></html>"""


def _setup_steps() -> str:
    """The "how to add your keys" list, in terms that fit the machine.

    This page used to say "open this file in Notepad" on every platform and
    print the path on its own. On a Mac that is two dead ends at once: there is
    no Notepad, and the file begins with a dot, so Finder hides it and there is
    no obvious way to create it. Give each platform a route that actually
    works there.
    """
    env_path = REPO_ROOT / ".env"
    ask = ("<li><b>Ask IT for your access keys</b> — a few lines that look "
           "like <code>PG_PROXY_TOKEN=…</code> and "
           "<code>PG_MIRROR_TOKEN=…</code></li>")
    reopen = "<li><b>Close the Portal and open it again.</b></li>"

    if sys.platform == "darwin":
        # `touch` first because `open -e` cannot open a file that is not there
        # yet, and on a fresh install it never is.
        cmd = f'touch "{env_path}" && open -e "{env_path}"'
        return (
            ask +
            "<li>Open <b>Terminal</b> — press <code>⌘ Space</code>, type "
            "<i>Terminal</i>, press Return — then paste this line and press "
            "Return:"
            f'<div class="path">{html.escape(cmd)}</div>'
            "An empty TextEdit window opens. (The file name starts with a dot, "
            "so Finder hides it; this is the dependable way to reach it.)</li>"
            "<li>Paste the lines in, each on its own line, then press "
            "<code>⌘ S</code> to save and close the window.</li>" +
            reopen
        )

    return (
        ask +
        "<li>Open this file in Notepad (create it if it does not exist):"
        f'<div class="path">{html.escape(str(env_path))}</div></li>'
        "<li>Paste the lines in, each on its own line, and save.</li>" +
        reopen
    )


@app.before_request
def _setup_gate():
    """While the database is unreachable, every page is the setup page.

    Nothing else can work — there are no users to log in, no rates to read —
    so showing the normal login form would just move the dead end one screen
    later and hide the actual problem."""
    if SETUP_ERROR is None:
        return None
    from markupsafe import escape
    return _SETUP_PAGE.format(steps=_setup_steps(),
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

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
@login_required
def index():
    """Landing page: the sales and commission overview."""
    return _no_store(make_response(send_from_directory(str(CURRENT_DIR / "static"), "overview.html")))


@app.route("/report")
@login_required
def report_page():
    """The commission report tables -- served from "/" until the overview took
    that slot, so any link still pointing at "/" now lands on the overview."""
    return _no_store(make_response(send_from_directory(str(CURRENT_DIR / "static"), "index.html")))

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


@app.route("/api/agent-role-maps")
@login_required
def agent_role_maps_api():
    """Just the two role maps /api/commission embeds, for one month.

    The browser keeps a commission payload in localStorage for up to six hours
    and renders it before the network refresh lands. Roles are edited on the
    Data page without rebuilding commissions, so an edit made during that
    window is invisible in the report until the payload expires -- and if the
    background refresh fails (it is deliberately silent), until it is cleared
    by hand. These maps are cheap, so the client re-fetches them whenever it
    renders from cache rather than trusting the copy baked into the payload."""
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 8, type=int)
    try:
        return jsonify({
            "agent_roles": _agent_roles_for_month(year, month),
            "agent_role_history": _agent_role_history(),
        })
    except Exception as e:
        _log("[AGENT ROLE MAPS ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


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
    # Every change to a user used to land in the audit log as the same
    # "Updated user 'x'", so a password reset and a reactivation were
    # indistinguishable after the fact -- which is most of what the log is
    # for. Say which fields moved, never the values of the secret ones.
    changed = []
    if "username" in updates:
        changed.append(f"renamed to '{updates['username']}'")
    if "role" in updates:
        changed.append(f"role -> {updates['role']}")
    if "password_hash" in updates:
        changed.append("password set")
    if "is_active" in updates:
        changed.append("reactivated" if updates["is_active"] else "deactivated")
    summary = f"Updated user '{target['username']}'"
    if changed:
        summary += ": " + ", ".join(changed)
    db.insert_audit(actor["username"], "update", "user", summary, user_id=actor["id"])
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


def _agent_roles_for_month(year: int, month: int) -> dict[str, dict]:
    """{normalised agent name: {"role", "agent_type"}} as at this month.

    One row per agent wins -- the latest effective_from among the rows whose
    range actually covers the month, which is the same "latest row wins" rule
    basic_commission_rates uses to price them. Keyed on the same normalised
    form agent_names.resolve() uses so the browser can match a displayed name
    (which may be a nickname or full name) back to its role.
    """
    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import split_effective_range
        target = f"{year}-{month:02d}"

        best: dict[str, tuple[str, dict]] = {}
        for r in db.list_agent_roles():
            if r.get("hidden"):
                continue
            agent = str(r.get("agent") or "").strip()
            if not agent:
                continue
            start, end = split_effective_range(str(r.get("effective_from") or ""))
            if not start or not (start <= target <= end):
                continue
            display = agent_names.resolve(agent)
            key = agent_names.normalize_key(display)
            prev = best.get(key)
            if prev is None or start > prev[0]:
                best[key] = (start, {
                    "agent": display,
                    "role": str(r.get("hierarchy") or "").strip(),
                    "agent_type": str(r.get("agent_type") or "").strip(),
                })
        return {k: v[1] for k, v in best.items()}
    except Exception:
        # A tinting aid must never take the commission table down with it.
        _log("[AGENT ROLES FOR MONTH]\n" + traceback.format_exc())
        return {}


def _agent_role_history() -> dict[str, list[dict]]:
    """{normalised agent name: [{"role", "agent_type", "start", "end"}, ...]},
    every effective range on file, latest start first.

    _agent_roles_for_month() collapses this to a single role as at the *report*
    month, which misstates any row whose invoice predates a role change: an
    invoice dated January that only reaches full payment in August lands on the
    August report, and the role it should read is January's. Shipping the whole
    history lets the browser resolve each row against its own Invoice Date --
    the same "latest row wins among those covering the month" rule
    _agent_roles_for_month() applies, just evaluated per row instead of once.
    """
    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import split_effective_range

        history: dict[str, list[dict]] = {}
        for r in db.list_agent_roles():
            if r.get("hidden"):
                continue
            agent = str(r.get("agent") or "").strip()
            if not agent:
                continue
            start, end = split_effective_range(str(r.get("effective_from") or ""))
            if not start:
                continue
            display = agent_names.resolve(agent)
            history.setdefault(agent_names.normalize_key(display), []).append({
                "agent": display,
                "role": str(r.get("hierarchy") or "").strip(),
                "agent_type": str(r.get("agent_type") or "").strip(),
                "start": start,
                "end": end,
            })
        for entries in history.values():
            entries.sort(key=lambda e: e["start"], reverse=True)
        return history
    except Exception:
        # A hover aid must never take the commission table down with it.
        _log("[AGENT ROLE HISTORY]\n" + traceback.format_exc())
        return {}


def _parse_money_text(value) -> float:
    """Sum of every RM amount in a rendered cell. One Other Commission cell can
    hold several credits joined by <br/>, so the leading figure alone would
    under-count it."""
    text = str(value or "").strip()
    if not text or text == "-":
        return 0.0
    amounts = re.findall(r"-?\s*RM\s*-?\s*[\d,]+(?:\.\d+)?", text, flags=re.IGNORECASE)
    if not amounts:
        return 0.0
    total = 0.0
    for a in amounts:
        try:
            total += float(re.sub(r"[^0-9.\-]", "", a))
        except ValueError:
            pass
    return total


def _overview_scope_agent() -> str | None:
    """The agent this user may see, or None for a whole-company view.

    NOTE: the users table carries only role ('admin' / 'staff') -- there is no
    per-user agent mapping server side, so today this always returns None and
    the overview is company-wide for every signed-in user. The single-agent
    scoping that exists (USER_ROLES.filterAgentName in app.js) is a client-side
    display filter, not an access boundary: /api/commission already returns
    every agent's figures to anyone signed in. This hook is where that scoping
    belongs once users gain an agent column, so the landing page does not have
    to be retrofitted then.
    """
    return None


def _overview_referral_people(*row_lists) -> set:
    """The distinct people named as referrers on the detail rows.

    Counted once each however many cases they brought, so it reads beside
    Total Agents rather than beside Total Customers.

    Referral Name sits five columns from the end in both layouts. The outsource
    table carries one extra column before it ("Gan Lai Soon"), making its rows
    20 wide against internal's 19, so the index is taken from each row's own
    width instead of being hardcoded per channel.
    """
    people = set()
    for rows in row_lists:
        for row in rows or []:
            if not row or len(row) < 6:
                continue
            name = str(row[len(row) - 5] or "").strip()
            if not name or name == "-":
                continue
            low = name.lower()
            if "total" in low or "summary" in low or "grand" in low:
                continue
            # A bare "Referral" with no name after it is a placeholder, not a
            # person, and must not add to the headcount.
            name = agent_names.clean_referral_name(name)
            if not name:
                continue
            key = agent_names.normalize_key(name)
            if key:
                people.add(key)
    return people


def _overview_public_totals(totals):
    """The totals without the private key sets used to union a date range."""
    if not totals:
        return totals
    return {k: v for k, v in totals.items() if not k.startswith("_")}


def _overview_unified_for_range(year: int, months):
    """(unified_agent_rows, totals) summed across several months.

    Money adds up, but the counts cannot: an agent selling in March and in July
    is one agent, not two, and the same customer or referrer recurring across
    months must be counted once. So the per-month keys are unioned rather than
    summed, and the effective rate is recomputed from the merged totals instead
    of being averaged.
    """
    merged: dict = {}
    customers: set = set()
    referrers: set = set()
    other_commission = 0.0
    referral_fee = 0.0
    got_any = False

    for mth in months:
        unified, totals = _overview_unified_for_month(year, int(mth))
        if unified is None:
            continue
        got_any = True
        for r in unified:
            key = (r["agent"], r["type"])
            acc = merged.setdefault(key, {
                "agent": r["agent"], "type": r["type"], "basic": 0.0,
                "nfp": 0.0, "anp": 0.0, "sales": 0.0, "invoices": 0, "total": 0.0,
            })
            for field in ("basic", "nfp", "anp", "sales", "total"):
                acc[field] += float(r.get(field) or 0.0)
            acc["invoices"] += int(r.get("invoices") or 0)
        customers |= totals.get("_customer_keys") or set()
        referrers |= totals.get("_referral_keys") or set()
        other_commission += float(totals.get("other_commission") or 0.0)
        referral_fee += float(totals.get("referral_fee") or 0.0)

    if not got_any:
        return None, None

    unified = sorted(merged.values(), key=lambda r: r["total"], reverse=True)
    totals = build_commission_pack._report_overview_totals(unified)
    totals["customers"] = len(customers)
    totals["referrals"] = len(referrers)
    totals["other_commission"] = other_commission
    totals["referral_fee"] = referral_fee
    return unified, totals


def _overview_unified_for_month(year: int, month: int):
    """(unified_agent_rows, overview_totals) for one month, across Internal and
    Outsource together.

    Built from the SAME helpers the PDF's Executive Summary uses
    (_unified_agent_totals / _report_overview_totals) and fed by the same
    per-agent totals build_*_summary_tables already returns -- app.py was
    already computing those and discarding them. Reusing them is what keeps the
    landing page and the PDF from ever quoting different numbers.

    Returns (None, None) when the cache has not been populated yet: this is the
    landing page, so it must never kick off a fresh multi-minute build.
    """
    needed = ("basic", "anp", "nfp")

    def _bundles():
        return get_cached_data(year, "internal"), get_cached_data(year, "outsource")

    def _usable(bundle):
        return bool(bundle) and all(k in bundle for k in needed)

    int_cached, out_cached = _bundles()
    if not _usable(int_cached) or not _usable(out_cached):
        # The in-memory cache is empty for a few seconds after a restart, while
        # the disk copy is already complete -- re-reading it here turns what was
        # a dead "not ready" page into a normal load.
        load_disk_cache()
        int_cached, out_cached = _bundles()

    if not _usable(int_cached) or not _usable(out_cached):
        missing = [name for name, b in (("internal", int_cached), ("outsource", out_cached))
                   if not _usable(b)]
        _log(f"[OVERVIEW] {year}-{month:02d} not ready; no cached bundle for: {', '.join(missing)}")
        return None, None

    invoice_dates_map = int_cached.get("invoice_dates") or out_cached.get("invoice_dates") or {}

    int_basic_t1, _t2, _t3, int_basic_t4, _meta, int_basic_lines = int_cached["basic"]
    int_anp_summary, int_anp_detail, _int_anp_meta = int_cached["anp"]
    int_nfp_agent, _int_nfp_detail, _int_nfp_meta, int_nfp_rows, int_nfp_by_inv_all = int_cached["nfp"]

    out_basic_t1, _o2, _o3, out_basic_meta, out_basic_lines = out_cached["basic"]
    _out_anp_summary, out_anp_detail, _out_anp_meta = out_cached["anp"]
    out_nfp_agent, _out_nfp_detail, _out_nfp_meta, out_nfp_rows, out_nfp_by_inv_all = out_cached["nfp"]

    (int_agent_summary, int_customer_summary, _iaa, _ica,
     int_totals_by_month) = build_commission_pack.build_internal_summary_tables(
        basic_t1=int_basic_t1, basic_lines=int_basic_lines, basic_t4=int_basic_t4,
        nfp_agent_rows=int_nfp_agent, nfp_rows=int_nfp_rows,
        nfp_by_inv_all=int_nfp_by_inv_all,
        anp_summary_rows=int_anp_summary,
        anp_detail=[r for r in int_anp_detail
                    if build_commission_pack._parse_month(r.get("invoice_date")) == month],
        year=year, invoice_dates_map=invoice_dates_map, month=month,
    )
    (out_agent_summary, out_customer_summary, _oca,
     out_totals_by_month) = build_commission_pack.build_outsource_summary_tables(
        basic_t1=out_basic_t1, basic_lines=out_basic_lines, basic_meta=out_basic_meta,
        nfp_agent_rows=out_nfp_agent, nfp_rows=out_nfp_rows,
        nfp_by_inv_all=out_nfp_by_inv_all,
        anp_summary_rows=[],
        anp_detail=[r for r in out_anp_detail
                    if build_commission_pack._parse_month(r.get("invoice_date")) == month],
        year=year, invoice_dates_map=invoice_dates_map, month=month,
    )

    unified = build_commission_pack._unified_agent_totals(
        int_totals_by_month.get(month, {}),
        out_totals_by_month.get(month, {}),
        build_commission_pack._max_anp_by_agent_for_month(out_anp_detail, month),
    )
    totals = build_commission_pack._report_overview_totals(unified)

    # The remaining report-page cards. Customers are counted per (agent,
    # customer) pair the same way the report page's Total Customers card does,
    # so the two can't disagree; Other Commission is the agent_summary rollup's
    # own last column.
    customers = set()
    for rows in (int_customer_summary.get(month, []), out_customer_summary.get(month, [])):
        current_agent = ""
        for row in rows:
            agent = str((row or [""])[0] or "").strip() or current_agent
            current_agent = agent
            customer = str(row[1] or "").strip() if len(row) > 1 else ""
            low = f"{agent} {customer}".lower()
            if not customer or customer == "-" or "total" in low or "summary" in low or "grand" in low:
                continue
            customers.add((agent.lower(), customer.lower()))

    other_commission = 0.0
    for rows in (int_agent_summary.get(month, []), out_agent_summary.get(month, [])):
        current_agent = ""
        for row in rows:
            agent = str((row or [""])[0] or "").strip() or current_agent
            current_agent = agent
            low = agent.lower()
            if not agent or "total" in low or "summary" in low or "grand" in low:
                continue
            if len(row) >= 8:
                other_commission += _parse_money_text(row[7])

    referral_fee = sum(v.get("referral", 0.0) for v in int_totals_by_month.get(month, {}).values())
    referral_fee += sum(v.get("referral", 0.0) for v in out_totals_by_month.get(month, {}).values())

    referrers = _overview_referral_people(int_customer_summary.get(month, []),
                                          out_customer_summary.get(month, []))

    totals["customers"] = len(customers)
    totals["referrals"] = len(referrers)
    totals["other_commission"] = other_commission
    totals["referral_fee"] = referral_fee
    # Kept so a multi-month view can union them; a count cannot be summed
    # across months without double-counting anyone who appears in two.
    totals["_customer_keys"] = customers
    totals["_referral_keys"] = referrers
    return unified, totals


@app.route("/api/overview")
@login_required
def get_overview():
    """Sales and commission analysis for the landing page."""
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 8, type=int)
    # month=0 asks for the year to date: January through the latest month the
    # ledger reaches. Every other value is that single month, as before.
    ytd = month <= 0
    months = list(range(1, _sales_report_last_month(year) + 1)) if ytd else []

    try:
        if ytd:
            unified, totals = _overview_unified_for_range(year, months)
        else:
            unified, totals = _overview_unified_for_month(year, month)
        if unified is None:
            _trigger_background_refresh(year, "internal")
            _trigger_background_refresh(year, "outsource")
            return jsonify({"ready": False,
                            "message": "Commission data is still being prepared. Refresh shortly."})

        # An agent-scoped user must never see the company-wide roll-up, so the
        # whole overview is narrowed to their own row before any total is taken.
        scope_agent = _overview_scope_agent()
        if scope_agent:
            target = _normalize_search_text(scope_agent)
            unified = [r for r in unified if _normalize_search_text(r.get("agent")) == target]
            totals = build_commission_pack._report_overview_totals(unified)

        # No month-on-month arrow on a year-to-date view: there is no single
        # prior period to compare it against.
        prev_month = None if ytd else (month - 1 if month > 1 else None)
        prev_unified, prev_totals = (None, None)
        if prev_month:
            prev_unified, prev_totals = _overview_unified_for_month(year, prev_month)
            if prev_unified is not None and scope_agent:
                target = _normalize_search_text(scope_agent)
                prev_unified = [r for r in prev_unified
                                if _normalize_search_text(r.get("agent")) == target]
                prev_totals = build_commission_pack._report_overview_totals(prev_unified)

        rank_changes = (build_commission_pack._rank_changes(unified, prev_unified)
                        if prev_unified else {})

        # The sales leaderboard is its own ranking, not the commission one
        # reordered: an agent with one large low-rate deal can top sales while
        # sitting well down on commission, so the movement arrows have to be
        # measured against last month's SALES order too.
        by_sales = sorted(unified, key=lambda r: r.get("sales") or 0.0, reverse=True)
        prev_by_sales = (sorted(prev_unified, key=lambda r: r.get("sales") or 0.0, reverse=True)
                         if prev_unified else None)
        sales_rank_changes = (build_commission_pack._rank_changes(by_sales, prev_by_sales)
                              if prev_by_sales else {})

        return jsonify({
            "ready": True,
            "year": year,
            "month": 0 if ytd else month,
            "ytd": ytd,
            "ytd_through": months[-1] if months else None,
            "scoped_to_agent": scope_agent,
            # The key sets are internal working state for the year-to-date
            # union and are not JSON-serialisable, so neither payload carries
            # them out.
            "totals": _overview_public_totals(totals),
            "prev_totals": _overview_public_totals(prev_totals),
            "top_performers": [
                {**r, "rank_change": rank_changes.get(r["agent"], "flat")}
                for r in unified[:10]
            ],
            "top_sales": [
                {**r, "rank_change": sales_rank_changes.get(r["agent"], "flat")}
                for r in by_sales[:10]
            ],
            # Sales split by who sold it. Summed from the agent rows already in
            # hand rather than re-read, so the two can never disagree.
            "sales_by_type": {
                "internal": sum(r.get("sales") or 0.0
                                for r in unified if r.get("type") == "Internal"),
                "outsource": sum(r.get("sales") or 0.0
                                 for r in unified if r.get("type") != "Internal"),
            },
            "agent_count": len(unified),
        })
    except Exception as e:
        _log("[OVERVIEW ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# â”€â”€ Sales Report (EGA half-year sales table) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
# Rebuilds the printed "ETERNALGY SALES REPORT" ledger from the database: per
# agent, cases and net sales for each month of the EGA half year, the June split
# either side of the campaign cut-off, then EP point and the balance left to
# qualify. Company-wide first, then one block per branch, as the print does.

SALES_REPORT_SPLIT_MONTH = 6     # the EGA campaign window closes 10 June
SALES_REPORT_SPLIT_DAY = 10      # June is shown as 1-10 / 11-30, per the campaign
# The order the branch blocks read in, which is not alphabetical: Kluang comes
# before Klang. A branch not named here follows the named ones, alphabetically,
# so a new branch appears without anyone having to touch this list.
SALES_REPORT_BRANCH_ORDER = ("(unassigned)", "JB", "Kluang", "Klang")
# Not sales agents, so never listed on the report even when an invoice carries
# their name. Same exclusion the commission tables apply via remove_agent_block
# ("Safwan"); matched on the normalized first word so "Safwan Nazri" is caught.
SALES_REPORT_EXCLUDED_AGENTS = ("safwan",)


def _sales_report_excluded(raw: str) -> bool:
    key = _normalize_search_text(raw)
    return any(key.startswith(x) for x in SALES_REPORT_EXCLUDED_AGENTS)
SALES_REPORT_TARGETS = {"Internal": 600000.0, "Outsource": 720000.0}
# ESA is the higher award and runs to December, where EGA closes in June.
# Both are overridden per channel by the Data page; these are only the fallback
# for a year nobody has set up yet.
SALES_REPORT_ESA_TARGETS = {"Internal": 1300000.0, "Outsource": 1560000.0}
# From this date a case is placed by when it was created rather than by when it
# was first paid. A one-off change of system, not a yearly cycle, so later years
# are all created-date by the same comparison.
SALES_REPORT_CREATED_FROM = "2026-08-01"


def _sales_report_last_month(year: int) -> int:
    """How many months of `year` the ledger covers: always at least the June
    campaign close, extended to the current month as the calendar moves past
    it so the report keeps accumulating instead of staying frozen at June."""
    from datetime import date
    today = date.today()
    if year < today.year:
        return 12
    if year > today.year:
        return SALES_REPORT_SPLIT_MONTH
    return min(12, max(SALES_REPORT_SPLIT_MONTH, today.month))


SALES_REPORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _award_qualified_month(rows, threshold, ladder, max_month: int):
    """The month these EP rows first win the award, or None.

    An early-bird rung is checked first and the earliest one wins, which is the
    whole point of the ladder; failing that, the month the running total first
    clears the full threshold. Nothing past `max_month` is considered, so a
    month that has not happened yet can never be named -- the award modules'
    own walk carries the running total forward indefinitely and will happily
    report an October rung in September.
    """
    from decimal import Decimal as _D

    monthly: dict[int, "_D"] = {}
    running = _D("0")
    for when, ep in sorted(rows, key=lambda x: str(x[0])):
        try:
            mth = int(str(when)[5:7])
        except (TypeError, ValueError):
            continue
        running += ep
        monthly[mth] = running
    if not monthly:
        return None

    def cumulative_by(month: int):
        """Carried forward, so a month with no invoices still counts what came
        before it -- the same reading the award ladder uses."""
        best = _D("0")
        for m in sorted(monthly):
            if m <= month:
                best = monthly[m]
        return best

    for month in sorted(ladder or {}):
        if month > max_month:
            continue
        rung = ladder[month]
        rung = rung[0] if isinstance(rung, (tuple, list)) else rung
        if cumulative_by(month) >= _D(str(rung)):
            return month

    for month in sorted(monthly):
        if month <= max_month and monthly[month] >= threshold:
            return month
    return None


def _qualifier_label(month) -> str:
    """"Apr Qualifier", in the printed report's wording."""
    if not month or not (1 <= int(month) <= 12):
        return "Qualifier"
    return f"{SALES_REPORT_MONTHS[int(month) - 1]} Qualifier"


def _sales_report_award_targets(year: int) -> tuple[dict[str, float], dict[str, float]]:
    """(EGA thresholds, ESA thresholds) per channel, from the Data page.

    Read from the same ega_rules rows the EGA/ESA Award section and the printed
    packs use, so the three cannot drift apart: change a threshold on the Data
    page and every one of them moves with it.
    """
    ega = dict(SALES_REPORT_TARGETS)
    esa = dict(SALES_REPORT_ESA_TARGETS)
    for key, agent_type in (("Internal", "internal"), ("Outsource", "outsource")):
        try:
            rules = db.get_ega_rules(str(year), agent_type).get("rules") or {}
            for field, bag in (("ega_threshold", ega), ("esa_threshold", esa)):
                raw = str(rules.get(field) or "").replace(",", "").strip()
                if raw:
                    bag[key] = float(raw)
        except Exception:
            # A missing or unreadable rules row must not take the page down; the
            # defaults above are the thresholds the printed report used.
            _log(f"[SALES REPORT] could not read {agent_type} ega_rules; using default thresholds")
    return ega, esa


def _sales_report_targets(year: int) -> dict[str, float]:
    """EGA qualifying thresholds only. Kept for callers that predate ESA."""
    return _sales_report_award_targets(year)[0]


_FIRST_PAYMENT_CACHE: dict[int, tuple[float, dict]] = {}
_PAYMENTS_CACHE: dict[tuple[int, int], tuple[float, dict]] = {}


def _sales_report_local_day(value) -> str:
    """A stored timestamp as its Kuala Lumpur calendar day, "2026-06-10".

    Bubble-era rows keep local midnight as 16:00 UTC the day before, so reading
    the raw date would show a payment on the wrong day. Same +8h shift the rest
    of the dashboard applies. Asia/Kuala_Lumpur has no daylight saving.
    """
    if not value or str(value).lower() in ("none", "null"):
        return ""
    try:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                + timedelta(hours=8)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(value)[:10]


def _sales_report_case_dates(year: int) -> dict[str, str]:
    """invoice bubble_id -> the date that decides which month a case falls in.

    Two regimes, and the switch is a change of system rather than a yearly
    cycle:

      * created before 1 Aug 2026 -- the case belongs to the month its FIRST
        PAYMENT landed. Teo Kim Seng is invoiced 5 February but first paid 31
        January, and the printed report counts him in January; Lim Lee Peng is
        invoiced 5 January but first paid 30 December, so he is not in 2026 at
        all.
      * created on or after 1 Aug 2026 -- the case belongs to the month it was
        CREATED. Diong Wei Yuan is created 7 September and first paid 29
        August, and belongs to September.

    `created_at` is in practice the invoice date: across 2026's 3,472 invoices
    it is never null and falls on the same day 3,468 times. It is read rather
    than invoice_date because it is what the rule names.

    Keyed on bubble_id, not invoice number: one number can carry several
    revisions with different dates -- 1008358 has first payments at 2026-01-03
    and 2025-12-30 -- so keying on the number can return a revision the report
    is not using. The same "latest revision" rule the report applies keeps the
    date and the money on one row.
    """
    key = int(year)
    hit = _FIRST_PAYMENT_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]

    sql = f"""
        SELECT bubble_id, p1, c FROM (
          SELECT i.bubble_id,
                 i."1st_payment_date"::date AS p1,
                 i.created_at::date AS c,
                 ROW_NUMBER() OVER (
                   PARTITION BY i.bubble_id
                   ORDER BY COALESCE(i.is_latest, FALSE) DESC,
                            i.invoice_date DESC NULLS LAST, i.id DESC) AS rn
            FROM invoice i
           WHERE i.invoice_date IS NOT NULL
             AND EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
             AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
        ) z WHERE rn = 1
    """
    # Worth retrying: the proxy fails intermittently, and an empty map does not
    # blank the page, it silently re-buckets every case by invoice date. That
    # moves every figure and moves it back on the next refresh, which is far
    # worse than a slow load.
    rows = None
    for attempt in range(3):
        try:
            rows = _pg_query(sql)
            break
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if rows is None:
        if hit:
            _log("[SALES REPORT] case dates unavailable; reusing the last good set")
            _FIRST_PAYMENT_CACHE[key] = (time.time(), hit[1])
            return hit[1]
        _log("[SALES REPORT] could not read case dates after 3 tries; "
             "falling back to invoice dates")
        return {}

    out: dict[str, str] = {}
    for r in rows or []:
        bid = str(r.get("bubble_id") or "").strip()
        if not bid:
            continue
        created = str(r.get("c") or "")[:10]
        paid = str(r.get("p1") or "")[:10]
        chosen = created if (created and created >= SALES_REPORT_CREATED_FROM) else paid
        if chosen:
            out[bid] = chosen
    _FIRST_PAYMENT_CACHE[key] = (time.time(), out)
    return out


_PAYMENT_ROWS_CACHE: dict[tuple[int, int], tuple[float, dict]] = {}


def _sales_report_payment_rows(year: int, upto_month: int) -> dict[str, list]:
    """invoice bubble_id -> the payments against it, for the case hover panel.

    Each payment is {"a": amount, "d": paid on, "m": entered on}. "Entered" is
    when it was keyed in, which can be well after the money arrived -- Diong Wei
    Yuan's 500 was paid on 29 August and entered on 9 September -- and that gap
    is the thing someone reconciling the report needs to see.

    Cut off at the end of `upto_month`, like the collected column, so every
    figure on a row describes the same window.
    """
    key = (int(year), int(upto_month))
    hit = _PAYMENT_ROWS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]

    end_year, end_month = (year + 1, 1) if upto_month >= 12 else (year, upto_month + 1)
    sql = f"""
        WITH inv AS (
          SELECT bubble_id FROM invoice
           WHERE invoice_date >= DATE '{year:04d}-01-01'
             AND invoice_date <  DATE '{year + 1:04d}-01-01'
             AND COALESCE(is_deleted, FALSE) IS NOT TRUE
        )
        SELECT p.linked_invoice AS inv,
               COALESCE(p.amount, 0)::numeric AS a,
               p.payment_date::date AS d,
               COALESCE(p.created_date, p.created_at)::date AS m
          FROM payment p
          JOIN inv ON inv.bubble_id = p.linked_invoice
         WHERE p.payment_date IS NOT NULL
           AND p.payment_date < DATE '{end_year:04d}-{end_month:02d}-01'
         ORDER BY p.payment_date DESC
    """
    try:
        rows = _pg_query(sql)
    except Exception:
        _log("[SALES REPORT] could not read payment rows; case hover will be empty")
        return {}

    out: dict[str, list] = {}
    for r in rows or []:
        inv = str(r.get("inv") or "").strip()
        if not inv:
            continue
        out.setdefault(inv, []).append({
            "a": float(r.get("a") or 0),
            "d": str(r.get("d") or "")[:10],
            "m": str(r.get("m") or "")[:10],
        })
    _PAYMENT_ROWS_CACHE[key] = (time.time(), out)
    return out


def _case_hover_entry(customer_name, payments):
    """One case for the hover panel, or None when there is no customer to name.

    An entry with no payments is still worth returning: the panel says "no
    payment recorded in this period", which is a real answer rather than a
    silently missing row.
    """
    name = str(customer_name or "").strip()
    if not name:
        return None
    return {"n": name, "p": list(payments or [])}


def _sales_report_payments(year: int, upto_month: int) -> dict[str, "Decimal"]:
    """Cash received against `year`'s invoices, keyed by invoice bubble_id.

    Counts money settling this year's invoices only, so the figure sits beside
    the ANS and EP columns built from those same invoices and the row reads as
    "invoiced this much, collected this much". Cash arriving this year against
    an earlier year's invoice is a different question and is left out -- it is
    RM 3m company-wide and would not reconcile with anything else on the row.

    Payments are cut off at the end of `upto_month` so every column on a row
    describes the same window: asking for January to June must not show cash
    that arrived in August.

    `actual_received` is a text column and is empty on every row, so `amount`
    is the only figure available. A failure here returns nothing rather than
    taking the page down -- the column then reads as zero, which the caller
    surfaces as a dash.
    """
    from decimal import Decimal as _D

    key = (int(year), int(upto_month))
    hit = _PAYMENTS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]

    end_year, end_month = (year + 1, 1) if upto_month >= 12 else (year, upto_month + 1)
    sql = f"""
        WITH inv AS (
          SELECT bubble_id FROM invoice
           WHERE invoice_date >= DATE '{year:04d}-01-01'
             AND invoice_date <  DATE '{year + 1:04d}-01-01'
             AND COALESCE(is_deleted, FALSE) IS NOT TRUE
        )
        SELECT p.linked_invoice AS inv, SUM(COALESCE(p.amount, 0))::numeric AS paid
          FROM payment p
          JOIN inv ON inv.bubble_id = p.linked_invoice
         WHERE p.payment_date IS NOT NULL
           AND p.payment_date < DATE '{end_year:04d}-{end_month:02d}-01'
         GROUP BY p.linked_invoice
    """
    try:
        rows = _pg_query(sql)
    except Exception:
        _log("[SALES REPORT] could not read payments; collected column left blank")
        return {}

    out: dict[str, "Decimal"] = {}
    for r in rows or []:
        inv = str(r.get("inv") or "").strip()
        if not inv:
            continue
        try:
            out[inv] = _D(str(r.get("paid") or 0))
        except Exception:
            continue
    _PAYMENTS_CACHE[key] = (time.time(), out)
    return out


def _sales_report_carryover(year: int) -> dict[str, dict]:
    """Pre-cutoff case counts for `year`, keyed by normalized agent name.

    Feeds the campaign bonus below. Entered by hand on the Data page because
    nothing in Postgres reproduces the printed report's counts -- see the
    ega_carryover table comment in db.py.
    """
    index: dict[str, dict] = {}
    try:
        rows = db.list_ega_carryover()
    except Exception:
        _log("[SALES REPORT] could not read ega_carryover; campaign bonus skipped")
        return index
    for r in rows:
        if str(r.get("year") or "").strip() != str(year):
            continue
        key = agent_names.normalize_key(str(r.get("agent") or ""))
        if key:
            index[key] = r
    return index


def _carryover_for(carryover: dict, *names) -> dict:
    """The carry-over row for an agent under any name they are known by.

    The Data page stores whatever name was typed into it, which may be the
    eeAdmin spelling, the Agent Roles spelling, or the display name this report
    shows -- for Elyn Pua Yee Ling those are three different strings. Trying
    each in turn stops a correctly entered row from silently scoring no bonus
    because it was filed under a different one of them.
    """
    for name in names:
        key = agent_names.normalize_key(str(name or ""))
        if key and key in carryover:
            return carryover[key]
    return {}


def _carryover_cases(raw) -> int:
    """The case count as an int, tolerating blanks and thousands separators."""
    try:
        return max(0, int(float(str(raw or "0").replace(",", "").strip() or 0)))
    except (TypeError, ValueError):
        return 0


def _campaign_bonus_tier(campaign_cases: int) -> "Decimal":
    """Multiplier applied to the 1 May - 10 Jun campaign sales.

    Driven by how many cases the agent invoiced INSIDE that window: every third
    one adds another half point per ringgit, so 3-5 cases give 0.5, 6-8 give
    1.0 and 9-11 give 1.5.

    Derived from the printed report ("ETERNALGY SALES REPORT AT YEAR 2026",
    updated 6 July 2026). Its multiplier column carries a case count and a
    ringgit figure, and the ringgit figure is the campaign sales with the
    multiplier already applied -- Chan Jia Wei's campaign of 320,463 plus his
    480,694.50 bonus is exactly the 801,157.50 printed there, and the same
    holds for every agent whose row can be read in full. The count matches the
    agent's own May and 1-10 June case counts wherever those can be pinned
    exactly (Ching Zhe Hang 6+3=9, Tan Jia Hao 6+1=7, Ah Zhu 4+2=6, Martin
    2+1=3, Sunny 3+0=3, Woon Jia Keat 2+0=2).

    This was read as "cases closed before the 2025 cutoff" until 2026-09-09,
    which is why no database query ever reproduced the counts and why they had
    to be typed in by hand. They were campaign cases all along, and the report
    already counts those itself.

    Known gap: the printed column splits those cases into before-31/12/25 and
    after-1/1/26 vintages, and the after side appears to pay double -- Najwa's
    3 cases there earned 1.0x where 3 on the before side earn 0.5x. Nothing
    available here says which vintage a case belongs to, so every agent is
    charged the before-side rate. An agent on the after side is understated.
    """
    from decimal import Decimal as _D
    return _D(int(campaign_cases) // 3) * _D("0.5")


def _resolve_channel(raw: str, role: dict, out_mod, year: int, as_at_month: int,
                     row: dict) -> str:
    """"Internal" or "Outsource" for one agent, as at `as_at_month`.

    Decided by the role the agent holds now, not by whichever invoice happens
    to come first. Classifying on the first invoice put Chan Jia Wei (Outsource
    until April, Internal since) and Pua Yee Ling (first invoice in March,
    Internal role dated from June) on the Outsource table despite both being
    internal agents today.

    The roles override is looked up by exact name, so a misspelled ERP variant
    finds nothing on its own; asking again under the roles table's own spelling
    is what stops a merged agent's channel depending on which spelling happened
    to be invoiced first. Only when the roles page says nothing at all about the
    agent does the invoice's own classification still decide.
    """
    as_at = f"{year:04d}-{int(as_at_month):02d}-01"
    canonical = str((role or {}).get("agent") or "").strip()
    current = out_mod.get_agent_type_override(raw, as_at)
    if not current and canonical and canonical.lower() != raw.lower():
        current = out_mod.get_agent_type_override(canonical, as_at)
    if current:
        return "Outsource" if current == "outsource" else "Internal"
    return ("Outsource" if out_mod.is_outsource_agent(
        raw, (row or {}).get("agent_type"), (row or {}).get("invoice_date"))
        else "Internal")


def _company_ega_rows(year: int):
    """Every agent's invoices for `year`, both channels.

    The outsource pull carries no agent_type filter, so its rows are the whole
    company; the internal pull is filtered in SQL and would drop the months an
    agent spent on the other side. Returns None when that bundle is not cached,
    and the caller keeps its own rows.
    """
    bundle = get_cached_data(year, "outsource")
    if not bundle or "ega_raw" not in bundle:
        load_disk_cache()
        bundle = get_cached_data(year, "outsource")
    return (bundle or {}).get("ega_raw") or None


class _AwardLine:
    """One invoice on the EGA/ESA detail table. Mirrors the AwardLine the award
    modules build, but defined here so the dashboard's own assembly does not
    depend on their internals."""

    __slots__ = ("agent_name", "customer_name", "invoice_number", "prop_type",
                 "invoice_date", "sales_price", "ep_points", "panel_qty", "accum_ep")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _ega_award_report(mod, out_mod, rows, year, upto_month, channel, carryover, roles,
                      first_paid=None):
    """The EGA/ESA award tables for one channel, with the campaign bonus.

    Differs from the award modules' own build_report() in two ways, both chosen
    on the Data page's terms rather than the invoice's:

      * membership is the channel the agent holds as at `upto_month`, so an
        agent who transferred mid-year is judged on their whole year in one
        report instead of being split across both;
      * EP carries the campaign bonus -- every third pre-cutoff case adds half
        a point per ringgit of 1 May - 10 Jun sales -- so this page agrees with
        the Sales Report and with the printed report it was derived from.

    The bonus enters the cumulative walk dated 10 June, the day the campaign
    closes, so an early-bird rung can never be reached on it before it is
    earned. Returns the same 4-tuple build_report() does.
    """
    from collections import defaultdict as _dd
    from decimal import Decimal as _D, ROUND_HALF_UP as _RH

    lines: list = []
    agent_ep: dict = _dd(_D)
    agent_sales: dict = _dd(_D)
    agent_invoice_ep: dict = _dd(list)
    campaign: dict = _dd(_D)
    campaign_n: dict = _dd(int)
    channel_of: dict = {}
    named_as: dict = {}

    first_paid = first_paid or {}
    for row in rows:
        # Same rule as the Sales Report: a case belongs to the month its first
        # payment arrived, so the two pages agree case for case.
        paid_on = first_paid.get(str(row.get("bubble_id") or ""))
        when = mod._parse_invoice_date(paid_on) if paid_on else None
        if when is None and not first_paid:
            when = mod._parse_invoice_date(row.get("invoice_date"))
        if when is None or when.year != year or when.month > upto_month:
            continue
        raw = str(row.get("agent_name") or "(unknown)").strip()
        if _sales_report_excluded(raw):
            continue
        if raw not in channel_of:
            role = _sales_report_lookup_role(raw, roles)
            # One person, one row, even when the ERP spells them two ways
            # ("OLIVIER KOH CHONG LEE" / "...CONG LEE"). Splitting them halves
            # each half's EP and can report someone short of a target they met.
            # Same identity rule the Sales Report uses, so the pages agree.
            channel_of[raw] = _resolve_channel(raw, role, out_mod, year, upto_month, row)
            named_as[raw] = str(role.get("agent") or "").strip() or raw
        if channel_of[raw] != channel:
            continue
        agent = named_as[raw]

        prop = mod.classify_property_type(row)
        panel_qty = int(row.get("panel_qty") or 0)
        sales_price = (_D(str(row.get("total_amount") or 0))
                       - _D(str(row.get("epp_interest") or 0)))
        ep = mod.calc_ep_points(sales_price, prop, row.get("invoice_date"),
                                panel_qty).quantize(_D("0.01"), rounding=_RH)
        inv_dt = str(row.get("invoice_date") or "")[:10]

        agent_ep[agent] += ep
        agent_sales[agent] += sales_price
        agent_invoice_ep[agent].append((inv_dt, ep))
        if when.month == 5 or (when.month == SALES_REPORT_SPLIT_MONTH
                               and when.day <= SALES_REPORT_SPLIT_DAY):
            campaign[agent] += sales_price
            campaign_n[agent] += 1

        lines.append(_AwardLine(
            agent_name=agent,
            customer_name=str(row.get("customer_name") or "(unknown)").strip(),
            invoice_number=str(row.get("invoice_number") or "").strip(),
            prop_type=prop, invoice_date=inv_dt, sales_price=sales_price,
            ep_points=ep, panel_qty=panel_qty, accum_ep=_D("0")))

    # Campaign bonus, dated the day the window closes.
    bonus_date = f"{year:04d}-{SALES_REPORT_SPLIT_MONTH:02d}-{SALES_REPORT_SPLIT_DAY:02d}"
    bonuses: dict = {}
    for agent in list(agent_ep):
        tier = _campaign_bonus_tier(campaign_n.get(agent, 0))
        if not tier or not campaign.get(agent):
            continue
        bonus = (tier * campaign[agent]).quantize(_D("0.01"), rounding=_RH)
        bonuses[agent] = bonus
        agent_ep[agent] += bonus
        agent_invoice_ep[agent].append((bonus_date, bonus))

    # Running EP down each agent's invoices, in date order. The bonus is not an
    # invoice, so it is folded into every line from 10 June onwards rather than
    # inventing a row for it -- the column still ends on the agent's total EP.
    per_agent: dict = _dd(list)
    for ln in lines:
        per_agent[ln.agent_name].append(ln)
    for agent, ln_list in per_agent.items():
        ln_list.sort(key=lambda x: (x.invoice_date, x.invoice_number))
        running = _D("0")
        bonus = bonuses.get(agent, _D("0"))
        for ln in ln_list:
            running += ln.ep_points
            ln.accum_ep = running + (bonus if ln.invoice_date >= bonus_date else _D("0"))

    eligibility = {a: mod.determine_eligibility(agent_invoice_ep[a]) for a in agent_ep}
    return lines, dict(agent_ep), dict(agent_sales), eligibility


def _sales_report_role_index() -> dict[str, dict]:
    """agent_roles keyed by every name it is known under, preferring a row that
    actually names a branch so an agent with several role rows still lands in
    the right block."""
    index: dict[str, dict] = {}
    for r in db.list_agent_roles():
        branch = str(r.get("branch") or "").strip()
        for field in ("agent", "nick_name", "full_name"):
            key = agent_names.normalize_key(str(r.get(field) or ""))
            if not key:
                continue
            prev = index.get(key)
            if prev is None or (branch and not str(prev.get("branch") or "").strip()):
                index[key] = r
    return index


def _sales_report_lookup_role(raw: str, index: dict[str, dict]) -> dict:
    """The agent_roles row for `raw`, tolerating the spelling drift between the
    ERP and the roles table ("Olivier Koh Chong Lee" vs "...Cong Lee"). Only a
    very close match counts, so two different agents can never merge."""
    key = agent_names.normalize_key(raw)
    if not key:
        return {}
    if key in index:
        return index[key]
    import difflib
    close = difflib.get_close_matches(key, list(index), n=1, cutoff=0.9)
    return index[close[0]] if close else {}


def _sales_report_identity(raw: str, role: dict) -> str:
    """The key every spelling of one person shares.

    The ERP holds more than one spelling for some agents -- "OLIVIER KOH CHONG
    LEE" and "OLIVIER KOH CONG LEE" are one person -- and keying the ledger on
    the raw invoice name split them into two rows that displayed the same name,
    each carrying part of the cases and sales. EGA qualification compares each
    row's EP against the target, so a split could report someone as short of a
    target they had actually met.

    Identity comes from the Agent Roles row the name resolves to, which is the
    same close-match test that already decides the displayed name and branch,
    so no two agents merge here that were not already being shown as one. When
    no roles row matches, the normalized raw name is the key: two unidentified
    spellings stay apart rather than being guessed into one person.
    """
    if role:
        for field in ("pg_bubble_id", "full_name", "agent", "nick_name"):
            value = str(role.get(field) or "").strip()
            if value:
                return "role:" + agent_names.normalize_key(value)
    return "raw:" + agent_names.normalize_key(raw)


def _is_initials_of(word: str, words: list[str]) -> bool:
    """Whether `word` spells out the initials of `words`, in order.

    "ZH" against "Ching Zhe Hang" does (Z-he H-ang) and the printed report drops
    it; "CJ" against "Loo Chew Yin" does not, and the report keeps it. Length is
    no guide -- both are two letters -- so this is the test that separates them."""
    if len(word) > 3:
        return False
    initials = [w[0] for w in words if w]
    i = 0
    for ch in word:
        while i < len(initials) and initials[i] != ch:
            i += 1
        if i == len(initials):
            return False
        i += 1
    return True


def _sales_report_display_name(raw: str, role: dict) -> str:
    """Nickname followed by full name, in the printed report's style.

    A nickname word is dropped when it adds nothing to the full name: it repeats
    a word already there ("Louis Ng" + "Ng Zhan Yi" -> "LOUIS NG ZHAN YI"), it
    misspells one ("Oliver Koh" against "Olivier Koh Cong Lee" must not print as
    "OLIVER OLIVIER KOH CONG LEE"), it abbreviates one ("Zul" -> "Zulkarnain"),
    or it is just the person's initials ("Zh", "LK"). Everything else is kept,
    which is how "AH ZHU CHOONG YE HONG" and "CJ LOO CHEW YIN" stay intact."""
    import difflib
    full = (str(role.get("full_name") or "").strip() or str(raw or "").strip()).upper()
    nick = str(role.get("nick_name") or "").strip().upper()
    if not nick:
        return full
    words = full.split()

    def redundant(word: str) -> bool:
        if difflib.get_close_matches(word, words, n=1, cutoff=0.8):
            return True
        if len(word) >= 3 and any(w.startswith(word) for w in words):
            return True
        return _is_initials_of(word, words)

    lead = [w for w in nick.split() if not redundant(w)]
    return " ".join(lead + [full]) if lead else full


def _sales_report_campaign_label(last_month: int) -> str:
    """What the campaign column covers for a period ending in `last_month`.
    The campaign runs 1 May to 10 June, so a period ending in May covers only
    part of it and one ending before May covers none."""
    if last_month < 5:
        return "1/5 - 10/6 (outside period)"
    if last_month == 5:
        return "1/5 - 31/5"
    return f"1/5 - {SALES_REPORT_SPLIT_DAY}/{SALES_REPORT_SPLIT_MONTH}"


_SALES_REPORT_PAYLOAD_CACHE: dict[tuple, tuple[float, dict]] = {}


def _sales_report_payload(year: int, through: int | None = None) -> dict | None:
    """_sales_report_payload_uncached(), memoised for the period picker.

    Building one period costs a few seconds, and the picker asks for a
    different period on every change. Without this, switching month looks
    broken: nothing moves, and a second change while the first is still
    running can land its answer last and leave the wrong month on screen.

    The cost is round trips to the proxy, not Python -- profiling put nearly
    all of a 15-second build in socket reads. Those six lookups now run
    together (see _sales_report_payload_uncached), which took a cold build to
    under five seconds; this cache is what makes a repeat visit instant.

    Keyed on the agent scope as well as the period -- an agent-scoped user sees
    only their own row, and that view must never be served to anyone else.
    Same TTL as the other response caches, so a rebuild is picked up as
    promptly as it is everywhere else.
    """
    try:
        scope = _overview_scope_agent() or ""
    except Exception:
        scope = ""          # called outside a request; treat as unscoped
    key = (int(year), None if through is None else int(through), scope)
    hit = _SALES_REPORT_PAYLOAD_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]

    payload = _sales_report_payload_uncached(year, through)
    if payload is not None:
        _SALES_REPORT_PAYLOAD_CACHE[key] = (time.time(), payload)
    return payload


def _sales_report_payload_uncached(year: int, through: int | None = None) -> dict | None:
    """Per-agent sales ledger for Jan..last_month of `year`, or None when the
    cache is cold. Like the overview, this is a page people land on -- it reads
    whatever the prefetch already built rather than starting a multi-minute job.

    `through` shortens the reporting period: the report always opens in January
    and runs to that month, so picking June accumulates Jan-Jun and nothing
    after it. Every figure follows, EP included -- an invoice outside the period
    is not counted at all, so the campaign bonus of a period that stops before
    10 June rests only on the campaign sales the period actually covers.

    EP is not simply the sales total. It follows the printed report: accumulated
    sales for the year, plus a bonus on the 1 May - 10 Jun campaign window whose
    size is set by how many cases the agent closed before the cutoff. See
    _campaign_bonus_tier().
    """
    from decimal import Decimal

    max_month = _sales_report_last_month(year)
    last_month = max_month if through is None else max(1, min(int(through), max_month))
    bundle = get_cached_data(year, "outsource")
    if not bundle or "ega_raw" not in bundle:
        load_disk_cache()
        bundle = get_cached_data(year, "outsource")
    if not bundle or "ega_raw" not in bundle:
        _log(f"[SALES REPORT] {year} not ready; no cached outsource ega_raw")
        return None

    # The outsource EGA query carries no agent_type filter, so its rows are the
    # whole company; the internal one is pre-filtered and would under-count.
    rows = bundle.get("ega_raw") or []

    int_mod = sys.modules.get("int_ega_esa")
    out_mod = sys.modules.get("out_ega_esa")
    if int_mod is None or out_mod is None:
        ega_dir = REPO_ROOT / "4. EGA ESA Awards" / "3. Python script"
        int_mod = int_mod or build_commission_pack._load_module(
            "int_ega_esa", ega_dir / "full_internal_EGA_ESA_Awards.py")
        out_mod = out_mod or build_commission_pack._load_module(
            "out_ega_esa", ega_dir / "outsource_EGA_ESA_Awards.py")

    # The early-bird ladders are module globals that ensure_rules_applied()
    # rewrites from the Data page, and Internal and Outsource have their own.
    # Read them per channel so a rung edited on the Data page moves the month
    # named in the qualifier column with it.
    ladders: dict[str, tuple[dict, dict]] = {}
    for label, mod in (("Internal", int_mod), ("Outsource", out_mod)):
        try:
            mod.ensure_rules_applied(year)
        except Exception:
            _log(f"[SALES REPORT] could not apply {label} EGA rules; using built-in ladder")
        ladders[label] = (dict(getattr(mod, "EGA_EARLY_BIRD", {}) or {}),
                          dict(getattr(mod, "ESA_EARLY_BIRD", {}) or {}))

    # Six independent lookups, fetched together rather than one after another.
    # Run in sequence they cost about 15 seconds, and profiling puts nearly all
    # of that in socket reads -- this is not Python being slow, it is six round
    # trips to the proxy taken one at a time. In parallel the wait is the
    # slowest of them instead of their sum.
    #
    # Safe to thread: none of the six touches the Flask request context, and
    # db.py takes its lock for writes only, so these reads do not serialise
    # against each other. The scope filter that DOES read the request is
    # applied further down, on this thread.
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_roles = ex.submit(_sales_report_role_index)
        f_payments = ex.submit(_sales_report_payments, year, last_month)
        f_payment_rows = ex.submit(_sales_report_payment_rows, year, last_month)
        f_case_dates = ex.submit(_sales_report_case_dates, year)
        f_carryover = ex.submit(_sales_report_carryover, year)
        f_targets = ex.submit(_sales_report_award_targets, year)

        # .result() re-raises in this thread, so a failing lookup still fails
        # the request the way it did when these ran in a straight line.
        roles = f_roles.result()
        payments = f_payments.result()
        payment_rows = f_payment_rows.result()
        case_dates = f_case_dates.result()
        carryover = f_carryover.result()
        targets, esa_targets = f_targets.result()
    # The multiplier is a first-half device. A report that runs into July or
    # beyond applies none at all.
    multiplier_in_period = last_month <= SALES_REPORT_SPLIT_MONTH
    zero = Decimal("0")
    agents: dict[str, dict] = {}
    seen_bubble: set = set()
    prop_counts: dict[str, int] = {}

    for r in rows:
        bubble = r.get("bubble_id")
        if bubble in seen_bubble:
            continue
        seen_bubble.add(bubble)

        # A case lands in the month its money first arrived. An invoice
        # raised in December and first paid in January is January's; one raised
        # in January but first paid last December belongs to last year and
        # drops out of this report entirely.
        placed_on = case_dates.get(str(bubble or ""))
        when = int_mod._parse_invoice_date(placed_on) if placed_on else None
        if when is None and not case_dates:
            # The lookup failed outright; fall back to invoice dates rather
            # than emptying the report.
            when = int_mod._parse_invoice_date(r.get("invoice_date"))
        if when is None or when.year != year or when.month > last_month:
            continue

        raw = str(r.get("agent_name") or "").strip()
        if _sales_report_excluded(raw):
            continue
        sales = (Decimal(str(r.get("total_amount") or 0))
                 - Decimal(str(r.get("epp_interest") or 0)))
        prop = int_mod.classify_property_type(r)
        prop_counts[prop] = prop_counts.get(prop, 0) + 1
        kind = "RES" if prop.lower().startswith("resid") else "COM"

        role = _sales_report_lookup_role(raw, roles)
        identity = _sales_report_identity(raw, role)
        entry = agents.get(identity)
        if entry is None:
            branch = str(role.get("branch") or "").strip().replace("Team-", "")
            channel = _resolve_channel(raw, role, out_mod, year, last_month, r)
            display = _sales_report_display_name(raw, role)
            carry = _carryover_for(carryover, raw, role.get("agent"),
                                   role.get("full_name"), role.get("nick_name"), display)
            entry = agents[identity] = {
                "agent": display,
                "carry_cases": _carryover_cases(carry.get("cases")),
                "carry_sales": carry.get("sales") or "",
                "branch": branch or "(unassigned)",
                "channel": channel,
                # Every ERP spelling folded into this row, so the scope filter
                # below can still be asked in the ERP's own terms.
                "raw_names": set(),
                "lines": {},              # kind -> month -> [noc, ans]
                # kind -> month -> [customer, ...], so a NOC cell can say who
                # its cases were. Names only; the counts stay in "lines".
                "custs": {},
                "campaign_custs": [],
                "jun": {},                # kind -> [1-10, 11-30]
                "campaign_cases": 0, "campaign_ans": zero,
                "acc_noc": 0, "acc_ans": zero, "collected": zero,
                "ep": zero, "ep_rows": [],
            }

        entry["raw_names"].add(raw)

        months = entry["lines"].setdefault(kind, {})
        cell = months.setdefault(when.month, [0, zero])
        cell[0] += 1
        cell[1] += sales
        customer = _case_hover_entry(r.get("customer_name"),
                                     payment_rows.get(str(bubble or "")))
        if customer:
            entry["custs"].setdefault(kind, {}).setdefault(when.month, []).append(customer)
        if when.month == SALES_REPORT_SPLIT_MONTH:
            split = entry["jun"].setdefault(kind, [zero, zero])
            split[0 if when.day <= SALES_REPORT_SPLIT_DAY else 1] += sales
        # Campaign window: 1 May through 10 June. Fixed to the actual EGA
        # campaign dates regardless of how far the ledger now extends -- a
        # period that stops earlier simply never reaches the later rows.
        if when.month == 5 or (when.month == SALES_REPORT_SPLIT_MONTH
                               and when.day <= SALES_REPORT_SPLIT_DAY):
            entry["campaign_cases"] += 1
            entry["campaign_ans"] += sales
            if customer:
                entry["campaign_custs"].append(customer)

        entry["acc_noc"] += 1
        entry["acc_ans"] += sales
        # One invoice is visited once (seen_bubble above), so its receipts are
        # added once. An invoice nobody has paid simply contributes nothing.
        entry["collected"] += payments.get(str(bubble or ""), zero)
        entry["ep_rows"].append((
            when.strftime("%Y-%m-%d"),
            int_mod.calc_ep_points(sales, prop, r.get("invoice_date"),
                                   int(r.get("panel_qty") or 0)),
        ))

    scope_agent = _overview_scope_agent()
    if scope_agent:
        target = _normalize_search_text(scope_agent)
        agents = {k: v for k, v in agents.items()
                  if _normalize_search_text(v["agent"]) == target
                  or any(_normalize_search_text(n) == target
                         for n in v["raw_names"])}

    out_agents = []
    for entry in agents.values():
        # Base points, one per ringgit of sales (large factory jobs earn less
        # -- calc_ep_points decides that per invoice).
        base_ep = sum((ep for _, ep in entry["ep_rows"]), zero)
        tier = _campaign_bonus_tier(entry["campaign_cases"])
        # The May multiplier belongs to the first half of the year and to EGA.
        # It is applied only while the report still ends inside that half: from
        # a July period onward no multiplier is applied anywhere, so the EP on
        # screen is plain sales points. ESA never uses it at all, whatever the
        # period, so ESA is always judged on base_ep below.
        bonus = (tier * entry["campaign_ans"]) if multiplier_in_period else zero
        entry["ep"] = base_ep + bonus
        # EGA is settled at the June deadline and cannot be reached on later
        # sales, so it reads EP as at then.
        jun_end = f"{year:04d}-{SALES_REPORT_SPLIT_MONTH:02d}-31"
        jun_rows = [(d, e) for d, e in entry["ep_rows"] if d <= jun_end]
        if bonus:
            jun_rows.append(
                (f"{year:04d}-{SALES_REPORT_SPLIT_MONTH:02d}-{SALES_REPORT_SPLIT_DAY:02d}", bonus))
        ep_jun = sum((e for _, e in jun_rows), zero)

        threshold = Decimal(str(targets.get(entry["channel"], 720000.0)))
        esa_threshold = Decimal(str(esa_targets.get(entry["channel"], 1560000.0)))
        # Qualification is the EGA one -- that is what the summary card counts
        # and what the printed report's own qualifier column means.
        ega_ladder, esa_ladder = ladders.get(entry["channel"], ({}, {}))
        # EGA is settled at the June deadline; ESA keeps running, so it is read
        # against every month the report covers.
        ega_month = _award_qualified_month(
            jun_rows, threshold, ega_ladder, min(SALES_REPORT_SPLIT_MONTH, last_month))
        # ESA on plain sales points: the campaign multiplier is an EGA device
        # and does not count toward it.
        esa_month = _award_qualified_month(
            list(entry["ep_rows"]), esa_threshold, esa_ladder, last_month)

        # Clearing an early-bird rung IS qualifying -- that is what the ladder
        # is for, and the printed report lists agents who never reach the full
        # threshold (Denise Ng at 523,484 against a 600,000 bar) as April
        # qualifiers. Testing the full threshold alone dropped every one of
        # them. The balance shown to those still short stays measured against
        # the full threshold, as the printed report measures it.
        qualified = ega_month is not None
        ega_double = ep_jun >= threshold * 2
        esa_qualified = esa_month is not None
        esa_ep = base_ep
        types = [k for k in ("RES", "COM") if entry["lines"].get(k)]
        out_agents.append({
            "agent": entry["agent"],
            "branch": entry["branch"],
            "channel": entry["channel"],
            "types": [{
                "type": kind,
                "months": [
                    {"noc": entry["lines"][kind].get(m, [0, zero])[0],
                     "ans": float(entry["lines"][kind].get(m, [0, zero])[1]),
                     "custs": entry["custs"].get(kind, {}).get(m, [])}
                    for m in range(1, last_month + 1)
                ],
                "jun_early": float(entry["jun"].get(kind, [zero, zero])[0]),
                "jun_late": float(entry["jun"].get(kind, [zero, zero])[1]),
                "total_noc": sum(v[0] for v in entry["lines"][kind].values()),
                "total_ans": float(sum((v[1] for v in entry["lines"][kind].values()), zero)),
            } for kind in types],
            "acc_noc": entry["acc_noc"],
            "acc_ans": float(entry["acc_ans"]),
            # Cash in against the invoices on this row, so a reader can see how
            # much of the sales figure has actually been paid.
            "collected": float(entry["collected"]),
            "campaign_cases": entry["campaign_cases"],
            "campaign_ans": float(entry["campaign_ans"]),
            "campaign_custs": entry["campaign_custs"],
            "ep": float(entry["ep"]),
            # Shown alongside EP so the bonus can be checked without recomputing
            # it by hand, and so an agent with a blank carry-over row is
            # visibly blank rather than silently scoring zero bonus.
            "base_ep": float(base_ep),
            "carry_cases": entry["carry_cases"],
            "carry_sales": entry["carry_sales"],
            "bonus_tier": float(tier),
            "campaign_bonus": float(bonus),
            # EP as at the EGA deadline, so the EGA column can be read without
            # recomputing it, and so the difference from the running EP is
            # visible rather than implied.
            "ep_jun": float(ep_jun),
            "qualified": qualified,
            "balance": 0.0 if qualified else float(threshold - ep_jun),
            "ega_threshold": float(threshold),
            # Twice the target earns a second trip ticket. Only one agent in the
            # printed report reaches it, so this rests on a single example --
            # treat the label as provisional until a second one appears.
            "ega_double": ega_double,
            "esa_threshold": float(esa_threshold),
            "esa_qualified": esa_qualified,
            # Measured on plain sales points, so the shortfall shown matches
            # the basis the award is judged on.
            "esa_ep": float(esa_ep),
            "esa_balance": 0.0 if esa_qualified else float(esa_threshold - esa_ep),
            # "Apr Qualifier" and the like, in the printed report's wording.
            "status": _qualifier_label(ega_month) if qualified else "",
            "esa_status": _qualifier_label(esa_month) if esa_qualified else "",
        })

    # Qualifiers first, then everyone else, each group by EP descending.
    # Sorting on EP alone buried them: EGA is settled on EP as at June while
    # the column sorts on the running total, so an agent who qualified in April
    # can sit below one who has out-earned them since without winning anything.
    # Either award counts as qualifying -- today every ESA winner is also an
    # EGA winner, but ranking on EGA alone would drop that out one day.
    out_agents.sort(key=lambda a: (0 if (a["qualified"] or a["esa_qualified"]) else 1,
                                   -a["ep"]))
    def _branch_key(name: str) -> tuple:
        try:
            return (SALES_REPORT_BRANCH_ORDER.index(name), "")
        except ValueError:
            return (len(SALES_REPORT_BRANCH_ORDER), name)

    branches = sorted({a["branch"] for a in out_agents}, key=_branch_key)
    return {
        "ready": True,
        "year": year,
        "last_month": last_month,
        # How far the ledger could go, so the period picker can offer every
        # month there is data for without guessing at today's date.
        "max_month": max_month,
        "split_month": SALES_REPORT_SPLIT_MONTH,
        "split_day": SALES_REPORT_SPLIT_DAY,
        # The slice of the 1 May - 10 June campaign this period covers, said
        # plainly in the column heading rather than left to be inferred.
        "campaign_label": _sales_report_campaign_label(last_month),
        # False when the period stops before 1 May: the carry-over cases earn
        # nothing then, and printing a count next to a zero bonus reads as if
        # they did. Both cells say "not in this period" instead.
        "campaign_in_period": last_month >= 5,
        # One award column at a time, whichever the period is still deciding.
        # EGA is settled on EP as at 30 June, so a period reaching July can no
        # longer move it; ESA runs to December and is what those months are
        # still playing for.
        "show_ega": last_month <= SALES_REPORT_SPLIT_MONTH,
        "show_esa": last_month > SALES_REPORT_SPLIT_MONTH,
        "scoped_to_agent": scope_agent,
        "targets": targets,
        "esa_targets": esa_targets,
        "multiplier_in_period": multiplier_in_period,
        "branches": branches,
        "agents": out_agents,
        "totals": {
            "cases": sum(a["acc_noc"] for a in out_agents),
            "sales": sum(a["acc_ans"] for a in out_agents),
            "agents": len(out_agents),
        },
        # Surfaced so the page can say why the RES/COM split looks thin: the
        # classifier falls back to Residential whenever SEDA nem type, referral
        # project type and the package fields are all silent.
        "property_mix": prop_counts,
    }


@app.route("/sales-report")
@login_required
def sales_report_page():
    """The sales ledger behind the EGA award, in the printed report's layout."""
    return _no_store(make_response(
        send_from_directory(str(CURRENT_DIR / "static"), "sales_report.html")))


@app.route("/api/sales-report")
@login_required
def get_sales_report():
    year = request.args.get("year", 2026, type=int)
    through = request.args.get("month", type=int)
    try:
        payload = _sales_report_payload(year, through)
        if payload is None:
            _trigger_background_refresh(year, "outsource")
            return jsonify({"ready": False,
                            "message": "Sales data is still being prepared. Refresh shortly."})
        return jsonify(payload)
    except Exception as e:
        _log("[SALES REPORT ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


def _sales_report_visible(payload: dict, search: str) -> list[dict]:
    """The agents a given search box would show, in report order. Mirrors
    normalizeName()/visibleAgents() in static/sales_report.js so the PDF and
    the screen never disagree about who is in the report."""
    q = _normalize_search_text(search or "")
    if not q:
        return payload.get("agents") or []
    return [a for a in (payload.get("agents") or [])
            if q in _normalize_search_text(a.get("agent") or "")]


def _sales_report_pdf(payload: dict, search: str) -> bytes:
    """Render the sales ledger as a landscape A3 PDF, block for block the same
    as the page: Company-wide Internal/Outsource first, then each branch."""
    import io
    from datetime import datetime
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle)

    MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]

    lm = int(payload.get("last_month") or 6)
    sm = int(payload.get("split_month") or 6)
    # June splits into 1-10 and 11-30 only to measure the campaign window, so a
    # period with no multiplier prints it as an ordinary month.
    split_june = payload.get("multiplier_in_period") is not False
    sd = int(payload.get("split_day") or 10)
    year = payload.get("year")
    targets = payload.get("targets") or {}
    shown = _sales_report_visible(payload, search)
    # The campaign opens on 1 May; a period stopping before then earns no bonus,
    # so the carry-over cases and the bonus both read as a dash, as on the page.
    # Dropped when the period ends before the window opens, and equally when it
    # runs into July or beyond, where no multiplier is applied at all.
    no_campaign = (payload.get("campaign_in_period") is False
                   or payload.get("multiplier_in_period") is False)

    def rm(v) -> str:
        """Two decimals, rounded the way the page's toLocaleString rounds.
        Formatting the float directly rounds half to even off the exact binary
        value and prints a cent less than the screen does on a tie, which is
        exactly the kind of difference someone would read as a bug."""
        from decimal import Decimal, ROUND_HALF_UP
        d = Decimal(repr(float(v or 0))).quantize(Decimal("0.01"),
                                                  rounding=ROUND_HALF_UP)
        return f"{d:,.2f}"

    # Two header rows, in exactly the column order agentRows() emits on screen.
    top = ["NO", "NAME", "TYPE"]
    sub = ["", "", ""]
    spans = []
    col = 3
    for m in range(1, lm + 1):
        if m == sm and split_june:
            top += [MONTH_ABBR[m - 1], "", ""]
            sub += ["NOC", f"1-{sd} {MONTH_ABBR[m - 1]}",
                    f"{sd + 1}-30 {MONTH_ABBR[m - 1]}"]
            spans.append(("SPAN", (col, 0), (col + 2, 0)))
            col += 3
        else:
            top += [MONTH_ABBR[m - 1], ""]
            sub += ["NOC", "ANS"]
            spans.append(("SPAN", (col, 0), (col + 1, 0)))
            col += 2
    campaign_label = payload.get("campaign_label") or f"1/5 - {sd}/{sm}"
    # The multiplier columns only exist while the period reaches the campaign.
    groups = [("TOTAL", ("NOC", "ANS")), ("ACC", ("NOC", "ANS"))]
    if not no_campaign:
        groups += [("MAY MULTIPLIER POINT " + campaign_label,
                    ("CASES", "BEFORE\n31/12/25")),
                   ("", ("CASES", "AFTER 1/1/26"))]
    for label, pair in groups:
        top += [label, ""]
        sub += list(pair)
        spans.append(("SPAN", (col, 0), (col + 1, 0)))
        col += 2
    # One award column, whichever the period is still deciding: EGA up to June,
    # ESA from July. Same rule the page follows, from the same payload flags.
    show_ega = payload.get("show_ega", lm <= sm) is not False
    show_esa = bool(payload.get("show_esa", lm > sm))
    top += ["COLLECTED\nPAYMENT", "UP TO DATE\nEP POINT"]
    sub += ["", ""]
    collected_col, ep_col = col, col + 1
    col += 2
    ega_col = esa_col = None
    if show_ega:
        top += [""]
        sub += [""]
        ega_col = col
        col += 1
    if show_esa:
        top += [""]
        sub += [""]
        esa_col = col
        col += 1
    ncols = col
    # NO/NAME/TYPE and the trailing columns are one logical cell each.
    spans += [("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)),
              ("SPAN", (2, 0), (2, 1)),
              ("SPAN", (collected_col, 0), (collected_col, 1)),
              ("SPAN", (ep_col, 0), (ep_col, 1))]
    for award in (ega_col, esa_col):
        if award is not None:
            spans.append(("SPAN", (award, 0), (award, 1)))

    def fmt_threshold(value) -> str:
        """"600K", "1.3M" -- the page's own shorthand for a bar."""
        n = float(value or 0)
        if n >= 1e6:
            return f"{n / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
        if n >= 1e3:
            return f"{round(n / 1e3):.0f}K"
        return rm(n)

    def header_rows(channel: str) -> list[list]:
        """The two header rows for one block. The print carries both channels'
        bars in one heading, "600K/720K", over the Internal and Outsource
        tables alike, and headerFor() follows it; so does this."""
        def bars(bag) -> str:
            bag = bag or {}
            return (f"{fmt_threshold(bag.get('Internal'))}"
                    f"/{fmt_threshold(bag.get('Outsource'))}")

        head = list(top)
        if ega_col is not None:
            head[ega_col] = ("EGA - HANOI\nBalance to Qualify\n"
                             f"({bars(payload.get('targets'))} EP Point Till 30Jun)")
        if esa_col is not None:
            head[esa_col] = ("ESA - CHONGQING\nBalance to Qualify\n"
                             f"({bars(payload.get('esa_targets'))} EP Point Till 31Dec)")
        return [head, list(sub)]

    page_w, page_h = landscape(A3)
    avail = page_w - 24 * mm
    no_w, name_w, type_w = 15.0, 74.0, 20.0
    num_w = (avail - no_w - name_w - type_w) / (ncols - 3)
    col_widths = [no_w, name_w, type_w] + [num_w] * (ncols - 3)
    # A longer period means more month columns and less room in each, so the
    # type shrinks to keep "1,234,567.89" inside its cell instead of letting it
    # run over the neighbour. Capped at 5pt, which is what the shorter periods
    # get and as large as the widest report can be.
    font_size = max(3.8, min(5.0, (num_w - 3.0) / 6.7))

    # A long name has to wrap rather than run over the JAN column, so the
    # name cell is a Paragraph while every other cell stays a plain string.
    name_style = ParagraphStyle("srName", fontName="Helvetica", fontSize=font_size,
                                leading=font_size + 0.8,
                                textColor=colors.HexColor("#0f172a"))

    def agent_rows(agent: dict, no: int) -> list[list]:
        types = agent.get("types") or [{"type": "RES", "months": [], "jun_early": 0,
                                        "jun_late": 0, "total_noc": 0, "total_ans": 0}]
        out = []
        for i, t in enumerate(types):
            first, last = i == 0, i == len(types) - 1
            row = [str(no) if first else "",
                   Paragraph(html.escape(str(agent.get("agent", ""))), name_style)
                   if first else "",
                   t.get("type", "")]
            months = t.get("months") or []
            for m in range(1, lm + 1):
                cell = months[m - 1] if m - 1 < len(months) else {}
                if m == sm and split_june:
                    row += [str(cell.get("noc", 0)), rm(t.get("jun_early")),
                            rm(t.get("jun_late"))]
                else:
                    row += [str(cell.get("noc", 0)), rm(cell.get("ans"))]
            tier = float(agent.get("bonus_tier") or 0)
            row += [
                str(t.get("total_noc", 0)), rm(t.get("total_ans")),
                str(agent.get("acc_noc", 0)) if last else "",
                rm(agent.get("acc_ans")) if last else "",
            ]
            if not no_campaign:
                row += [
                    str(agent.get("campaign_cases", 0)) if first else "",
                    # The window's sales with the multiplier already in them, as
                    # the printed report carries it. Blank when no multiplier is
                    # earned, since there is then nothing to award.
                    (("-" if not tier else
                      rm((agent.get("campaign_ans") or 0)
                         + (agent.get("campaign_bonus") or 0)))
                     if first else ""),
                    # The newer vintage is not derivable, so its pair stays empty
                    # rather than carrying a figure nobody can stand behind.
                    ("-" if first else ""), ("-" if first else ""),
                ]
            row += [
                ((rm(agent.get("collected")) if agent.get("collected") else "-")
                 if first else ""),
                rm(agent.get("ep")) if first else "",
            ]
            if not first:
                row += [""] * ((1 if show_ega else 0) + (1 if show_esa else 0))
            else:
                if show_ega:
                    if agent.get("ega_double"):
                        row.append("DOUBLE TICKET")
                    elif agent.get("qualified"):
                        row.append(agent.get("status") or "Qualifier")
                    else:
                        row.append(rm(agent.get("balance")))
                if show_esa:
                    if agent.get("esa_qualified"):
                        row.append(agent.get("esa_status") or "Qualifier")
                    else:
                        row.append(rm(agent.get("esa_balance")))
            out.append(row)
        return out

    def subtotal_row(members: list[dict]) -> list:
        row = ["", "SUBTOTAL", ""]
        for m in range(1, lm + 1):
            noc = sum((t.get("months") or [{}] * m)[m - 1].get("noc", 0)
                      for a in members for t in (a.get("types") or [])
                      if m - 1 < len(t.get("months") or []))
            if m == sm and split_june:
                row += [str(noc),
                        rm(sum(t.get("jun_early") or 0
                               for a in members for t in (a.get("types") or []))),
                        rm(sum(t.get("jun_late") or 0
                               for a in members for t in (a.get("types") or [])))]
            else:
                ans = sum((t.get("months") or [{}] * m)[m - 1].get("ans", 0)
                          for a in members for t in (a.get("types") or [])
                          if m - 1 < len(t.get("months") or []))
                row += [str(noc), rm(ans)]
        acc_noc = sum(a.get("acc_noc", 0) for a in members)
        acc_ans = sum(a.get("acc_ans", 0) for a in members)
        row += [str(acc_noc), rm(acc_ans), str(acc_noc), rm(acc_ans)]
        if not no_campaign:
            row += [str(sum(a.get("campaign_cases", 0) for a in members)),
                    rm(sum((a.get("campaign_ans") or 0)
                           + (a.get("campaign_bonus") or 0)
                           for a in members if a.get("bonus_tier"))),
                    "-", "-"]
        # Collected, EP, then one blank per award column in the header.
        row += [rm(sum(a.get("collected") or 0 for a in members)), ""]
        row += [""] * ((1 if show_ega else 0) + (1 if show_esa else 0))
        return row

    def block(title: str, members: list[dict], channel: str) -> list:
        if not members:
            return []
        rows = header_rows(channel)
        for i, a in enumerate(members):
            rows += agent_rows(a, i + 1)
        rows.append(subtotal_row(members))
        style = [
            ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
            ("FONTNAME", (0, 2), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("LEADING", (0, 0), (-1, -1), font_size + 1.2),
            ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#1f2a44")),
            ("TEXTCOLOR", (0, 0), (-1, 1), colors.white),
            ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (2, -1), "CENTER"),
            ("ALIGN", (3, 0), (-1, 1), "CENTER"),
            ("ALIGN", (1, 2), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8cedb")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef1f7")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), 1.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
        ] + spans
        # Qualified reads green and short reads red, the same signal the page
        # gives with .sr-qualified / .sr-short.
        green, red = colors.HexColor("#166534"), colors.HexColor("#b91c1c")
        r = 2
        for a in members:
            for i in range(max(1, len(a.get("types") or []))):
                if i == 0:
                    if ega_col is not None:
                        style.append(("TEXTCOLOR", (ega_col, r), (ega_col, r),
                                      green if a.get("qualified") else red))
                    if esa_col is not None:
                        style.append(("TEXTCOLOR", (esa_col, r), (esa_col, r),
                                      green if a.get("esa_qualified") else red))
                r += 1
        target = float(targets.get(channel, 0) or 0)
        head = Paragraph(
            f"<b>{html.escape(title)}</b> &nbsp;&nbsp;{len(members)} agent"
            f"{'' if len(members) == 1 else 's'} &middot; target RM {target:,.2f} EP",
            ParagraphStyle("blockHead", fontName="Helvetica", fontSize=8,
                           textColor=colors.HexColor("#334155"), spaceAfter=3))
        table = Table(rows, colWidths=col_widths, repeatRows=2)
        table.setStyle(TableStyle(style))
        return [head, table, Spacer(1, 8)]

    title_style = ParagraphStyle("srTitle", fontName="Helvetica-Bold", fontSize=15,
                                 textColor=colors.HexColor("#0f172a"), spaceAfter=2)
    meta_style = ParagraphStyle("srMeta", fontName="Helvetica", fontSize=8,
                                textColor=colors.HexColor("#64748b"), spaceAfter=10)
    section_style = ParagraphStyle("srSection", fontName="Helvetica-Bold", fontSize=10,
                                   textColor=colors.HexColor("#0f172a"),
                                   spaceBefore=4, spaceAfter=4)

    def fmt_compact(value) -> str:
        n = float(value or 0)
        if abs(n) >= 1e6:
            return f"RM {n / 1e6:.2f}m"
        if abs(n) >= 1e3:
            return f"RM {n / 1e3:.1f}k"
        return f"RM {rm(n)}"

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

    def summary_cards():
        """The five cards the page shows above the tables, in the same order
        and computed the same way -- from the agents on screen, not from
        data.totals, so a search moves them."""
        internal = [a for a in shown if a.get("channel") == "Internal"]
        outsource = [a for a in shown if a.get("channel") == "Outsource"]
        qual = sum(1 for a in shown if a.get("qualified"))
        cards = [
            ("Total Cases", str(sum(a.get("acc_noc", 0) for a in shown)),
             plural(len(shown), "agent")),
            ("Total Net Sales", fmt_compact(sum(a.get("acc_ans", 0) for a in shown)),
             "net of EPP interest"),
            ("Internal", fmt_compact(sum(a.get("acc_ans", 0) for a in internal)),
             f"{plural(len(internal), 'agent')} · "
             f"{plural(sum(a.get('acc_noc', 0) for a in internal), 'case')}"),
            ("Outsource", fmt_compact(sum(a.get("acc_ans", 0) for a in outsource)),
             f"{plural(len(outsource), 'agent')} · "
             f"{plural(sum(a.get('acc_noc', 0) for a in outsource), 'case')}"),
            ("Qualified", f"{qual} / {len(shown)}",
             f"RM {rm(targets.get('Internal'))} internal · "
             f"RM {rm(targets.get('Outsource'))} outsource"),
        ]
        width = (page_w - 24 * mm) / len(cards)
        table = Table([[c[0] for c in cards], [c[1] for c in cards],
                       [c[2] for c in cards]],
                      colWidths=[width] * len(cards))
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, 0), 6.5),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#64748b")),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 1), (-1, 1), 12),
            ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#0f172a")),
            ("FONTNAME", (0, 2), (-1, 2), "Helvetica"),
            ("FONTSIZE", (0, 2), (-1, 2), 6),
            ("TEXTCOLOR", (0, 2), (-1, 2), colors.HexColor("#94a3b8")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
            ("LINEAFTER", (0, 0), (-2, -1), 0.4, colors.HexColor("#e2e8f0")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 2), (-1, 2), 6),
        ]))
        return table

    period = f"January &ndash; {MONTH_NAMES[lm - 1]} {year}"
    cases = sum(a.get("acc_noc", 0) for a in shown)
    sales = sum(a.get("acc_ans", 0) for a in shown)
    qualified = sum(1 for a in shown if a.get("qualified"))
    meta = (f"Generated {datetime.now().strftime('%d %b %Y %H:%M')} &middot; "
            f"{len(shown)} agents &middot; {cases} cases &middot; "
            f"RM {sales:,.2f} net sales &middot; {qualified} qualified for EGA")
    if search:
        meta += f" &middot; filtered to &ldquo;{html.escape(search)}&rdquo;"

    story = [Paragraph(f"Sales Report &mdash; {period}", title_style),
             Paragraph(meta, meta_style), summary_cards(), Spacer(1, 12)]

    def pick(channel, branch):
        return [a for a in shown if a.get("channel") == channel
                and (branch is None or a.get("branch") == branch)]

    if not shown:
        story.append(Paragraph("No agent matches the current filter.", meta_style))
    else:
        story.append(Paragraph("Company-wide", section_style))
        story += block("Internal Sales Agent", pick("Internal", None), "Internal")
        story += block("Outsource Sales Agent", pick("Outsource", None), "Outsource")
        for branch in (payload.get("branches") or []):
            internal, outsource = pick("Internal", branch), pick("Outsource", branch)
            if not internal and not outsource:
                continue
            story.append(Paragraph(f"{html.escape(str(branch))} Branch", section_style))
            story += block("Internal Sales Agent", internal, "Internal")
            story += block("Outsource Sales Agent", outsource, "Outsource")

    # The page carries a note about the three columns that differ from the
    # printed report. Leaving it out of the file would let the RES/COM split be
    # read as data loss, so it travels with the tables.
    mix = payload.get("property_mix") or {}
    mix_total = sum(mix.values())
    res = mix.get("Residential", 0)
    if mix_total and res >= mix_total * 0.9:
        others = ", ".join(f"{v} {k}" for k, v in mix.items()
                           if k != "Residential") or "none"
        note_style = ParagraphStyle("srNote", fontName="Helvetica", fontSize=6.5,
                                    leading=8.5, spaceBefore=6,
                                    textColor=colors.HexColor("#64748b"))
        story.append(Paragraph(
            "<b>Three columns differ from the printed report.</b> "
            f"<b>RES / COM</b> - the property classifier returned Residential for {res} "
            f"of {mix_total} invoices (others: {html.escape(others)}); it falls back to "
            "Residential whenever SEDA nem type, referral project type and the package "
            "fields are all silent, so most agents show a single RES line where the print "
            "splits them. <b>EP point</b> - 1 point per RM1 of sales, plus the campaign "
            "bonus: every third case an agent closed before the cutoff adds half a point "
            "per ringgit of their campaign sales. Those case counts are typed in under "
            "Data &rarr; EGA/ESA Award; an agent with no row there earns no bonus, shown "
            "as a dash. The print also splits the campaign window into cases before "
            "31/12/25 and after 1/1/26, which is not derivable here, so it is shown as "
            "one figure.", note_style))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(12 * mm, 8 * mm,
                          f"Eternalgy Commission Portal - Sales Report {year}")
        canvas.drawRightString(page_w - 12 * mm, 8 * mm,
                               f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=landscape(A3),
                          leftMargin=12 * mm, rightMargin=12 * mm,
                          topMargin=12 * mm, bottomMargin=14 * mm,
                          title=f"Sales Report {year}", author="Eternalgy")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="sr", frames=[frame], onPage=footer)])
    doc.build(story)
    return buf.getvalue()


@app.route("/api/sales-report/pdf")
@login_required
def get_sales_report_pdf():
    """The sales ledger as a PDF, honouring the page's agent search so the file
    holds what is on screen rather than always the whole company."""
    year = request.args.get("year", 2026, type=int)
    through = request.args.get("month", type=int)
    search = (request.args.get("agent") or "").strip()
    try:
        payload = _sales_report_payload(year, through)
        if payload is None:
            _trigger_background_refresh(year, "outsource")
            return jsonify({"ready": False,
                            "message": "Sales data is still being prepared. "
                                       "Refresh shortly."}), 409
        pdf = _sales_report_pdf(payload, search)
        abbr = ["jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec"][int(payload.get("last_month") or 6) - 1]
        name = f"sales_report_{payload.get('year')}_jan_{abbr}.pdf"
        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return _no_store(resp)
    except Exception:
        _log("[SALES REPORT PDF ERROR]\n" + traceback.format_exc())
        return jsonify({"error": "Failed to build the sales report PDF."}), 500


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
            cached_payload = cached_response.get("payload") or {}
            # Roles are read fresh rather than served from the cached body:
            # they are cheap, they are edited on the Data page without
            # rebuilding commissions, and a response cached before this field
            # existed carries none at all.
            if isinstance(cached_payload, dict):
                cached_payload["agent_roles"] = _agent_roles_for_month(year, month)
                cached_payload["agent_role_history"] = _agent_role_history()
            return jsonify(cached_payload)

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

            # Determine headers. The Safwan column is popped further below, AFTER
            # special-case rows are injected -- a hand-added factory customer
            # (e.g. a Durapower-style restatement) only exists in those injected
            # rows, so checking has_factory before injection always missed it.
            if month >= 7:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Referral Name", "Referral Fee", "Basic Rate %", "Advance Deducted", "Advance Payment Date"]
            else:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Referral Name", "Referral Fee", "Basic Rate %", "Advance Deducted", "Advance Payment Date"]

            # Extract ANP rows before column removal
            anp_rows = _apply_anp_agent_display_names(int_customer_anp.get(month, []))
            anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Total Amount", "Accumulated Total Amount", "Commission Price", "Clawback"]

            basic_nfp_rows = remove_agent_block(int_customer_summary.get(month, []), "Safwan")
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

            pkg_i = basic_nfp_headers.index("Package Type") if "Package Type" in basic_nfp_headers else -1
            has_factory = pkg_i != -1 and any(
                "factory" in str(r[pkg_i]).lower() for r in basic_nfp_rows if len(r) > pkg_i
            )
            if not has_factory and "Safwan (RM)" in basic_nfp_headers:
                safwan_idx = basic_nfp_headers.index("Safwan (RM)")
                basic_nfp_headers.pop(safwan_idx)
                for r in reversed(basic_nfp_rows):
                    if len(r) > safwan_idx:
                        r.pop(safwan_idx)
                for r in reversed(anp_rows):
                    if len(r) > safwan_idx:
                        r.pop(safwan_idx)

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

            # These rows come from cache, so no fetch necessarily ran this
            # process to apply the Data page's thresholds. Without this the
            # award grades against the constants hardcoded in the script.
            int_ega_mod.ensure_rules_applied(year)
            # Company-wide rows so an agent who transferred mid-year is judged
            # on their whole year here, exactly as the Sales Report shows them.
            # Falls back to this section's own rows when that bundle is cold.
            _ega_rows = _company_ega_rows(year) or filtered_ega_raw
            lines, agent_ep, agent_sales, agent_eligibility = _ega_award_report(
                int_ega_mod, sys.modules.get("out_ega_esa") or int_ega_mod,
                _ega_rows, year, month, "Internal",
                _sales_report_carryover(year), _sales_report_role_index(),
                _sales_report_case_dates(year))

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
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Basic Commission (RM300)", "75% Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Gan Lai Soon", "Referral Name", "Referral Fee", "Basic Rate %", "Advance Deducted", "Advance Payment Date"]
            else:
                basic_nfp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Full Payment Date", "Package Type", "System Price", "Net Floor Price", "Sales Price", "Commission", "Commission Price", "OVERRIDE", "Safwan (RM)", "Gan Lai Soon", "Referral Name", "Referral Fee", "Basic Rate %", "Advance Deducted", "Advance Payment Date"]

            out_anp_rows = _apply_anp_agent_display_names(build_anp_customer_rows_custom(
                anp_summary=out_anp_summary,
                anp_detail=out_anp_detail_filtered,
                invoice_dates_map=invoice_dates_map,
                month=month,
                is_internal=False
            ))

            basic_nfp_rows = remove_agent_block(out_customer_summary.get(month, []), "Safwan")
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

            pkg_i = basic_nfp_headers.index("Package Type") if "Package Type" in basic_nfp_headers else -1
            has_factory = pkg_i != -1 and any(
                "factory" in str(r[pkg_i]).lower() for r in basic_nfp_rows if len(r) > pkg_i
            )
            if not has_factory and "Safwan (RM)" in basic_nfp_headers:
                safwan_idx = basic_nfp_headers.index("Safwan (RM)")
                basic_nfp_headers.pop(safwan_idx)
                for r in reversed(basic_nfp_rows):
                    if len(r) > safwan_idx:
                        r.pop(safwan_idx)
                for r in reversed(out_anp_rows):
                    if len(r) > safwan_idx:
                        r.pop(safwan_idx)

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

            # These rows come from cache, so no fetch necessarily ran this
            # process to apply the Data page's thresholds. Without this the
            # award grades against the constants hardcoded in the script.
            out_ega_mod.ensure_rules_applied(year)
            _ega_rows = _company_ega_rows(year) or filtered_ega_raw
            lines, agent_ep, agent_sales, agent_eligibility = _ega_award_report(
                out_ega_mod, out_ega_mod, _ega_rows, year, month, "Outsource",
                _sales_report_carryover(year), _sales_report_role_index(),
                _sales_report_case_dates(year))

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
            "sections": sections,
            # Each agent's role as at this month -- the fallback the agent-name
            # hover uses on tables that carry no invoice date of their own.
            "agent_roles": _agent_roles_for_month(year, month),
            # Full role history, so a row whose invoice predates a role change
            # can be resolved against its own Invoice Date rather than the
            # report month it happens to be paid in.
            "agent_role_history": _agent_role_history(),
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

    # A restated-customer case (caseType "customer" with a restatedTotal +
    # linkedInvoiceNumber) never trusts the (year, month) the browser posted
    # -- that's just whichever tab was open -- it goes wherever
    # _resolve_restated_case_placement says cumulative payments actually
    # crossed the restated total, computed fresh here server-side. Split
    # those out before the ordinary merge-into-one-bucket logic below runs.
    restated_placed, restated_pending = [], []
    plain_sc_list = []
    for c in new_sc_list:
        linked_invoice_number = str(c.get("linkedInvoiceNumber") or "").strip()
        restated_total = c.get("restatedTotal")
        if (str(c.get("caseType") or "").strip().lower() == "customer"
                and linked_invoice_number and restated_total):
            placement = _resolve_restated_case_placement(linked_invoice_number, restated_total)
            if not placement.get("placed"):
                restated_pending.append(
                    (str(c.get("customer") or "").strip(), placement.get("pct_paid", 0.0)))
                continue
            placed_case = dict(c)
            placed_case["year"], placed_case["month"] = str(placement["year"]), str(placement["month"])
            restated_placed.append(placed_case)
        else:
            plain_sc_list.append(c)

    if restated_pending:
        detail = "; ".join(f"{name}: {pct}% paid" for name, pct in restated_pending)
        return jsonify({"error": "Not yet fully paid against the restated total, so this "
                                 f"case can't be placed on the report yet ({detail})."}), 400

    try:
        if str(req_data.get("mode") or "merge").lower() != "replace":
            merged = {}
            for c in db.get_special_cases(year, month, agent_type):
                merged[_case_key(c)] = c
            for c in deleted:
                merged.pop(_case_key(c), None)
            for c in plain_sc_list:
                # A case with no agent can never be rendered again
                # (_inject_special_case_rows skips it), so refuse to store one.
                if not str(c.get("agent") or "").strip():
                    continue
                merged[_case_key(c)] = c
            plain_sc_list = list(merged.values())

        db.save_special_cases(year, month, agent_type, plain_sc_list, auth.current_user()["username"])

        # Each restated case is merged into its OWN computed bucket the same
        # way, then pruned from whatever bucket it used to live in if that
        # bucket differs -- e.g. its restated total was edited and now
        # crosses in a different month than it used to.
        for placed_case in restated_placed:
            r_year, r_month = placed_case["year"], placed_case["month"]
            r_bucket = {c2: c1 for c1, c2 in
                        ((c, _case_key(c)) for c in db.get_special_cases(r_year, r_month, agent_type))}
            r_bucket[_case_key(placed_case)] = placed_case
            db.save_special_cases(r_year, r_month, agent_type, list(r_bucket.values()),
                                  auth.current_user()["username"])
            if (r_year, r_month) != (year, month):
                stale_bucket = db.get_special_cases(year, month, agent_type)
                if any(_case_key(c) == _case_key(placed_case) for c in stale_bucket):
                    pruned = [c for c in stale_bucket if _case_key(c) != _case_key(placed_case)]
                    db.save_special_cases(year, month, agent_type, pruned,
                                          auth.current_user()["username"])

        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[SPECIAL CASES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


def _pg_query(sql: str):
    """Run one read-only query against Postgres through the proxy."""
    token, base_url, db_name = build_commission_pack._resolve_proxy_credentials()
    if not token:
        raise RuntimeError("Postgres proxy token not found. Set PG_PROXY_TOKEN in .env")
    anp_mod = sys.modules.get("int_anp_commission")
    if anp_mod is None:
        anp_mod = build_commission_pack._load_module(
            "int_anp_commission", build_commission_pack._anp_script_path())
    client = anp_mod.PostgresProxyClient(
        base_url.rstrip("/").replace("/api/sql", ""), token, db_name)
    return client.query(sql)


@app.route("/api/special-cases/resolve-restatement", methods=["POST"])
@login_required
def resolve_restatement_api():
    """Live preview for the "Restated Total" field on the Special Case Customer
    modal: where a restated deal would land and how much of it is paid,
    computed the same way special_cases_api() enforces at save time."""
    req_data = request.json or {}
    invoice_number = str(req_data.get("invoice_number") or "").strip()
    restated_total = req_data.get("restated_total")
    if not invoice_number or not restated_total:
        return jsonify({"error": "invoice_number and restated_total are required"}), 400
    return jsonify(_resolve_restated_case_placement(invoice_number, restated_total))


@app.route("/api/customer-invoices")
@login_required
def customer_invoices_api():
    """A customer's invoices (number, total, status), for the Special Case
    Customer modal's invoice picker -- only shown when a customer has more
    than one, since a restated total has to name exactly which one it's for."""
    customer = str(request.args.get("customer", "") or "").strip()
    if not customer:
        return jsonify({"error": "customer is required"}), 400
    safe_customer = customer.replace("'", "''")
    try:
        rows = _pg_query(f"""
            SELECT i.invoice_number, i.invoice_date, i.total_amount, i.status
            FROM invoice i
            LEFT JOIN customer c ON c.customer_id = i.linked_customer
            WHERE i.is_deleted IS NOT TRUE
              AND LOWER(TRIM(COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name))) = LOWER(TRIM('{safe_customer}'))
            ORDER BY i.invoice_date DESC
        """)
        invoices = [{
            "invoice_number": str(r.get("invoice_number") or "").strip(),
            "invoice_date": str(r.get("invoice_date") or "")[:10],
            "total_amount": float(r.get("total_amount") or 0),
            "status": str(r.get("status") or "").strip(),
        } for r in rows if str(r.get("invoice_number") or "").strip()]
        return jsonify({"invoices": invoices})
    except Exception as e:
        _log("[CUSTOMER INVOICES ERROR]\n" + traceback.format_exc())
        return jsonify({"error": _public_error_message(e, "Could not load invoices"),
                        "invoices": []}), 500


@app.route("/api/invoice-payments")
@login_required
def invoice_payments_api():
    """Every payment behind one invoice, with its deposit slip.

    Backs the click-through on a Collected Payment figure: that cell is a sum,
    and without this nothing on the page says what it is a sum of. It is also
    where a refund's bank details actually live -- the slip names the bank and
    the account holder, which no column in this database does.
    """
    invoice_number = str(request.args.get("invoice_number", "") or "").strip()
    if not invoice_number:
        return jsonify({"error": "invoice_number is required"}), 400
    # Only ever interpolated as a quoted literal, and quotes are doubled: this
    # value arrives from the query string.
    safe = invoice_number.replace("'", "''")
    try:
        rows = _pg_query(f"""
            SELECT p.id, p.payment_date, p.amount,
                   COALESCE(p.payment_method, '')    AS payment_method,
                   COALESCE(p.issuer_bank, '')       AS issuer_bank,
                   COALESCE(p.remark, '')            AS remark,
                   COALESCE(p.attachment, ARRAY[]::text[]) AS attachment
            FROM payment p
            JOIN invoice i ON i.bubble_id = p.linked_invoice
            WHERE TRIM(i.invoice_number) = '{safe}'
              AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)
            ORDER BY p.payment_date, p.id
        """)
        payments, total = [], 0.0
        for r in rows:
            try:
                amount = float(r.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            total += amount
            # Bubble stores some attachments protocol-relative ("//cdn…/x.jpg").
            # Left as-is the browser resolves them against the dashboard's own
            # http://localhost, and the image never loads.
            slips = []
            for u in (r.get("attachment") or []):
                u = str(u or "").strip()
                if not u:
                    continue
                if u.startswith("//"):
                    u = "https:" + u
                slips.append(u)
            payments.append({
                "id": r.get("id"),
                "payment_date": str(r.get("payment_date") or "")[:10],
                "amount": amount,
                "payment_method": str(r.get("payment_method") or "").strip(),
                "issuer_bank": str(r.get("issuer_bank") or "").strip(),
                "remark": str(r.get("remark") or "").strip(),
                "slips": slips,
            })
        return jsonify({"invoice_number": invoice_number, "payments": payments,
                        "total": round(total, 2)})
    except Exception as e:
        _log("[INVOICE PAYMENTS ERROR]\n" + traceback.format_exc())
        return jsonify({"error": _public_error_message(e, "Could not load payments"),
                        "payments": []}), 500


@app.route("/api/pb-refunds", methods=["GET", "POST"])
@login_required
def pb_refunds_api():
    """Refunds owed where a Production Bonus invoice collected more than it was
    worth. Any signed-in user may record one — this is finance housekeeping, not
    a rate change — and who ticked it is stamped on the row and audited."""
    if request.method == "GET":
        try:
            return jsonify({"refunds": db.list_pb_refunds()})
        except Exception as e:
            _log("[PB REFUNDS LOAD ERROR]\n" + traceback.format_exc())
            return jsonify({"refunds": {}, "error": str(e)}), 200

    user = auth.current_user()
    payload = request.json or {}
    invoice_number = str(payload.get("invoice_number") or "").strip()
    if not invoice_number:
        return jsonify({"error": "invoice_number is required"}), 400
    try:
        saved = db.save_pb_refund(
            invoice_number,
            str(payload.get("bank_account") or "").strip(),
            bool(payload.get("refund_done")),
            user["username"],
        )
        return jsonify({"status": "success", "invoice_number": invoice_number, "refund": saved})
    except Exception as e:
        _log("[PB REFUND SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/referral-overrides", methods=["GET", "POST"])
@login_required
def referral_overrides_api():
    """The referral name and rate typed on the Basic & NFP table.

    A save clears the commission cache, because the fee is recomputed by the
    same builder the printed packs use -- the row on screen and the money in
    the pack come from one place, so they cannot drift apart.
    """
    if request.method == "GET":
        year = request.args.get("year", "2026")
        try:
            return jsonify(db.list_referral_overrides(year))
        except Exception as e:
            _log("[REFERRAL OVERRIDES READ ERROR]\n" + traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    req = request.json or {}
    try:
        rate = str(req.get("rate") or "").strip().replace("%", "")
        if rate:
            value = float(rate)
            if value < 0 or value > 100:
                return jsonify({"error": "Rate must be between 0 and 100 percent."}), 400
        saved = db.save_referral_override(
            str(req.get("year") or "2026"),
            str(req.get("agent") or ""),
            str(req.get("customer") or ""),
            str(req.get("referral_name") or ""),
            rate,
            auth.current_user()["username"],
        )
        clear_commission_cache()
        # Cached under (year, month, agent_type); the referral belongs to the
        # customer, so every month of that year has to be rebuilt.
        try:
            build_commission_pack._REFERRAL_OVERRIDE_CACHE.clear()
        except Exception:
            pass
        return jsonify({"status": "success", "saved": saved})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log("[REFERRAL OVERRIDE SAVE ERROR]\n" + traceback.format_exc())
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


@app.route("/api/factory-split-hierarchy")
@login_required
def factory_split_hierarchy_api():
    """OSA/OUM/OGM tier and who to credit for the Factory profit-sharing
    70/20/10 split, for one agent -- read from the Agent Roles & Hierarchy
    Data page. Backs the "Calculated Commissions Preview" breakdown in the
    Profit Sharing popup, so the popup's math can never disagree with what
    build_commission_pack.py actually pays out."""
    agent = (request.args.get("agent") or "").strip()
    if not agent:
        return jsonify({"error": "agent is required"}), 400
    try:
        out_basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
        basic = build_commission_pack._load_module("out_basic_commission", out_basic_path)
        return jsonify(basic.resolve_factory_split_hierarchy(agent))
    except Exception as e:
        _log("[FACTORY SPLIT HIERARCHY ERROR]\n" + traceback.format_exc())
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


def _pg_agent_names():
    """Names of the agents eeAdmin currently lists as internal/outsource/sales,
    as [{"bubble_id", "name"}], one row per person, sorted by name.

    Since 2026-09 this is the ONLY thing the Agent Roles & Hierarchy page
    takes from Postgres. Agent type, Role, Branch, IC No, Nick Name and start
    date are entered and kept on the Data page itself; eeAdmin's tags no
    longer prefill, overwrite or flag any of them. The query still filters on
    the tags so office staff never show up, and still leaves blocked accounts
    out of the pull -- a row already saved for a blocked agent stays on the
    page until someone deletes it by hand.
    """
    mod = _basic_module()
    mod._load_dotenv()
    proxy_url = mod._normalize_proxy_url(os.environ.get("PG_PROXY_URL")) or \
        "https://pg-proxy-production.up.railway.app/api/sql"
    db_name = os.environ.get("PG_PROXY_DB") or os.environ.get("PG_DB_NAME")
    token = os.environ.get("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError("PG_PROXY_TOKEN is not configured")

    def _try_users_sql(sql_str):
        try:
            r = mod._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                               sql=sql_str, params=[])
            return r.get("rows") or r.get("data") or [], None
        except Exception as ex:
            return [], str(ex)

    if True:  # keeps the SQL below at its original indentation
        # One row per PERSON. Merged primarily by link_key -- an agent row's own
        # linked_user_login, resolved through the join below -- because that is
        # the one signal a typo cannot break: eeAdmin's own foreign key, not a
        # name comparison. Falling back to the normalized name only when
        # link_key is NULL keeps agent records with no user account to link to.
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

    def to_sentence_case(name):
        return " ".join(word.capitalize() for word in str(name).strip().split())

    seen, result = set(), []
    for r in user_rows:
        name = to_sentence_case(str(r.get("name") or "").strip())
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({"bubble_id": str(r.get("bubble_id") or "").strip(),
                       "name": name})
    result.sort(key=lambda x: x["name"].lower())
    return result


@app.route("/api/agent-roles/pg-list")
@login_required
def agent_roles_pg_list_api():
    """Live agent NAMES from Postgres, used to list every current agent on the
    Agent Roles & Hierarchy page. Nothing else is pulled -- see
    _pg_agent_names()."""
    try:
        result = _pg_agent_names()
        return jsonify({"agents": result, "total": len(result)})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log("[AGENT PG LIST ERROR]\n" + traceback.format_exc())
        return jsonify({"error": _public_error_message(e, "Failed to load agent list from Postgres")}), 500


def _pg_search_people(term):
    """Every eeAdmin person whose name contains `term`, tagged or not.

    Deliberately wider than _pg_agent_names(): that one filters on the
    internal/outsource/sales tags so office staff stay off the hierarchy page,
    which also hides anyone whose agent_type was simply never filled in. The
    Add Agent modal has to be able to find exactly those people, so the tag
    filter is dropped here and only blocked accounts are left out. Rows are
    merged per person the same way the list is -- by the agent row's linked
    user, falling back to the normalized name -- so a linked pair shows once.
    """
    mod = _basic_module()
    mod._load_dotenv()
    proxy_url = mod._normalize_proxy_url(os.environ.get("PG_PROXY_URL")) or \
        "https://pg-proxy-production.up.railway.app/api/sql"
    db_name = os.environ.get("PG_PROXY_DB") or os.environ.get("PG_DB_NAME")
    token = os.environ.get("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError("PG_PROXY_TOKEN is not configured")

    # The term reaches the proxy as a quoted literal, so the escaping is what
    # makes it safe; this second pass just drops anything with no business in a
    # person's name. Characters are dropped rather than refused because real
    # eeAdmin names carry "@" and brackets, and a typed name should still find
    # them instead of returning an error.
    safe = re.sub(r"[^\w .,'&/@()-]", " ", term, flags=re.UNICODE).strip()
    if len(safe) < 2:
        return []
    lit = mod._sql_string_literal("%" + safe + "%")

    sql = f"""
        SELECT d.bubble_id, d.name, d.agent_type, d.branch
          FROM (
            SELECT DISTINCT ON (COALESCE(au.link_key,
                     LOWER(regexp_replace(BTRIM(au.name), '[[:space:]]+', ' ', 'g'))))
                   au.bubble_id, au.name, au.agent_type, au.branch
              FROM (
                SELECT u.bubble_id,
                       u.bubble_id AS link_key,
                       u.name,
                       u.agent_type,
                       u.main_department AS branch,
                       1 AS pri
                  FROM "user" u
                 WHERE COALESCE(BTRIM(u.name), '') <> ''
                   AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                   AND u.name ILIKE {lit}

                UNION ALL

                SELECT ag.bubble_id,
                       u.bubble_id AS link_key,
                       ag.name,
                       COALESCE(ag.agent_type, u.agent_type) AS agent_type,
                       u.main_department AS branch,
                       2 AS pri
                  FROM agent ag
                  LEFT JOIN "user" u ON u.bubble_id = ag.linked_user_login
                 WHERE COALESCE(BTRIM(ag.name), '') <> ''
                   AND (u.access_level IS NULL OR NOT ('blocked' = ANY(u.access_level)))
                   AND ag.name ILIKE {lit}
              ) au
             ORDER BY COALESCE(au.link_key,
                        LOWER(regexp_replace(BTRIM(au.name), '[[:space:]]+', ' ', 'g'))),
                      CASE WHEN COALESCE(au.agent_type, '') = '' THEN 1 ELSE 0 END,
                      au.pri
          ) d
         ORDER BY LOWER(d.name)
         LIMIT 25
    """

    rows = mod._proxy_sql(proxy_url=proxy_url, db_name=db_name, token=token,
                          sql=sql, params=[])
    rows = rows.get("rows") or rows.get("data") or []

    def to_sentence_case(name):
        return " ".join(word.capitalize() for word in str(name).strip().split())

    seen, result = set(), []
    for r in rows:
        # Sentence case on purpose: it is what the page's own list uses, so a
        # row saved from a search hit matches its Postgres twin by name later.
        name = to_sentence_case(str(r.get("name") or "").strip())
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append({
            "bubble_id": str(r.get("bubble_id") or "").strip(),
            "name": name,
            "agent_type": str(r.get("agent_type") or "").strip(),
            "branch": str(r.get("branch") or "").strip(),
        })
    return result


@app.route("/api/agent-roles/pg-search")
@login_required
def agent_roles_pg_search_api():
    """Name lookup behind the Add Agent modal. See _pg_search_people() for why
    it does not reuse the hierarchy page's own list."""
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify({"agents": [], "total": 0})
    try:
        result = _pg_search_people(term)
        return jsonify({"agents": result, "total": len(result)})
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log("[AGENT PG SEARCH ERROR]\n" + traceback.format_exc())
        return jsonify({"error": _public_error_message(e, "Failed to search eeAdmin")}), 500


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
                           "effective_from", r"\d{4}-\d{2}( to \d{4}-\d{2})?", ["tiers"],
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
        if not re.fullmatch(r"\d{4}( to \d{4})?", year):
            return jsonify({"error": "year must be YYYY or YYYY to YYYY"}), 400
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
    if not re.fullmatch(r"\d{4}( to \d{4})?", year):
        return jsonify({"error": "year must be YYYY or YYYY to YYYY"}), 400
    try:
        db.save_ega_rules(year, body.get("rules") or {}, body.get("months") or [],
                          user["username"], agent_type=agent_type)
        clear_commission_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        _log("[EGA RULES SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/ega-rule-sets")
@login_required
def ega_rule_sets_api():
    """Every saved EGA/ESA rule set, for the landing list that shows Internal
    and Outsource together."""
    try:
        return jsonify(db.list_ega_rule_sets())
    except Exception as e:
        _log("[EGA RULE SETS LOAD ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/ega-carryover", methods=["GET", "POST"])
@login_required
def ega_carryover_api():
    """Cases each agent closed before the EGA carry-over cutoff.

    The count picks the campaign bonus tier on the Sales Report, so a wrong
    figure moves an agent's EP by half their May campaign sales per three
    cases. Admin-only to write, like every other rules grid."""
    if request.method == "GET":
        return jsonify(db.list_ega_carryover())

    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    try:
        entries = (request.json or {}).get("entries", [])
        rows = []
        for e in entries:
            agent = str(e.get("agent") or "").strip()
            year = str(e.get("year") or "").strip()
            if not agent or not year:
                continue
            rows.append({"year": year, "agent": agent,
                         "cases": str(e.get("cases") or "").strip(),
                         "sales": str(e.get("sales") or "").strip()})
        db.save_ega_carryover(rows, user["username"])
        clear_commission_cache()
        return jsonify({"status": "success", "total": len(rows)})
    except Exception as e:
        _log("[EGA CARRYOVER SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/production-bonus-rules", methods=["GET", "POST"])
@login_required
def production_bonus_rules_api():
    return _rules_endpoint(db.get_production_bonus_rules, db.save_production_bonus_rules,
                           "effective_from", r"\d{4}-\d{2}( to \d{4}-\d{2})?", [],
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
    """Pull the agent NAMES from Postgres so the hierarchy only needs the
    rest filled in by hand. Adds a name-only row for each agent not already
    listed; never touches or prunes an existing row -- the Data page is the
    source of truth for everything but the name."""
    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    try:
        month = (request.json or {}).get("month") or ""
        rows = _pg_agent_names()

        merged = db.list_agent_roles()
        known_names = {str(r.get("agent") or "").strip().lower() for r in merged}
        known_ids = {str(r.get("pg_bubble_id") or "").strip() for r in merged
                     if r.get("pg_bubble_id")}
        added = 0
        for r in rows:
            name = r["name"]
            if name.lower() in known_names or (r["bubble_id"] and r["bubble_id"] in known_ids):
                continue
            merged.append({
                "effective_from": month, "agent": name,
                "agent_type": "", "hierarchy": "", "reports_to": "", "branch": "",
                "ic_no": "", "nick_name": "", "full_name": "",
                "pg_bubble_id": r["bubble_id"],
                "remarks": "seeded from Postgres (name only)",
                "start_date": "",
            })
            known_names.add(name.lower())
            added += 1

        db.save_agent_roles(merged, user["username"])
        return jsonify({"status": "success", "added": added, "total": len(merged)})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
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

    def _eff_start(eff_str):
        """The month a row starts applying -- what "the latest row wins" sorts
        on. Ranking the raw cell puts "2026-07 to 2026-08" above the plain
        "2026-07" it starts in, purely because it is a longer string."""
        s = str(eff_str or "").strip()
        if " to " in s:
            return s.split(" to ")[0].strip()
        if ".." in s:
            return s.split("..")[0].strip()
        return s

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

        # NFP tier rates. Under a specific month only the row that governs it is
        # shown, resolved per (agent_type, role, agent, condition) so each tier
        # keeps its own effective-dated lineage. Under "All Months" every entered
        # row is listed instead -- same as the Basic grid above, and the only way
        # to see a revision you have just entered for a future month, or the one
        # it supersedes.
        best_nfp = {}
        all_nfp = []
        for src, rows in (("legacy", db.list_basic_rates()),
                          ("unified", db.list_commission_rates())):
            for r in rows:
                if str(r.get("rate_type") or "").strip() != "Net Floor Price Rate":
                    continue
                eff = str(r.get("effective_from") or "")
                if not eff:
                    continue
                if is_all:
                    r = dict(r)
                    r["_src"] = src
                    all_nfp.append(r)
                    continue
                # A closed range ("2025-01 to 2026-06") stops applying after its
                # end month. Comparing the raw cell would keep it forever, since
                # "2025-01 to 2026-06" sorts below every later single month.
                if not _eff_covers(eff, target):
                    continue
                key = (str(r.get("agent_type") or "").strip(),
                       str(r.get("hierarchy") or "").strip(),
                       str(r.get("agent") or "").strip(),
                       str(r.get("condition") or "").strip())
                prev = best_nfp.get(key)
                # Unified rows outrank legacy ones; among equals the latest wins.
                # Rank on the month a row starts, not the cell as written.
                if prev is None or (src == "unified" and prev.get("_src") != "unified") \
                        or _eff_start(eff) > _eff_start(str(prev.get("effective_from") or "")):
                    r = dict(r)
                    r["_src"] = src
                    best_nfp[key] = r
        nfp_rates = []
        for r in (all_nfp if is_all else list(best_nfp.values())):
            try:
                pct = float(str(r.get("rate_pct")).replace("%", "").strip())
            except (TypeError, ValueError):
                continue
            nfp_rates.append({
                "id": r.get("id"),
                # Stated explicitly: the edit form falls back to "Basic
                # Commission" for a row that does not name its type, which would
                # aim a tier's save (and its delete) at the Basic rows instead.
                "rate_type": "Net Floor Price Rate",
                "agent_type": r.get("agent_type"),
                "hierarchy": r.get("hierarchy"),
                "agent": r.get("agent") or "",
                "condition": r.get("condition") or "",
                "rate_pct": pct,
                # An NFP row keeps the tier in `condition`, so its payout stages
                # go in `label` -- same text the Basic grid renders, and enough
                # for the edit form to rebuild the stage rows from.
                "label": r.get("label") or "",
                "property_type": r.get("property_type") or "",
                "trigger_pct": r.get("trigger_pct") or "",
                "rule_type": r.get("rule_type") or "",
                "amount_rm": r.get("amount_rm") or "",
                "invoice_date_from": r.get("invoice_date_from") or "",
                "effective_from": r.get("effective_from") or "",
                "remarks": r.get("remarks") or "",
                "source": r.get("_src") or "legacy",
            })
        nfp_rates.sort(key=lambda x: (x["agent_type"], x["condition"]))

        # Nothing has ever been entered for NFP, so without this the Net Floor
        # Price page reads as "no rates exist" when in fact three tiers are hard
        # coded in the engine (nfp_commission.calc_commission) and paying out
        # every month. They ship as "Built-in default" rows -- the same badge the
        # Basic page uses -- so the page states what is actually in force and an
        # admin can take control of a tier by revising it.
        if not nfp_rates:
            _default_tier = lambda cond, pct: {
                "id": None, "rate_type": "Net Floor Price Rate",
                "agent_type": "All", "hierarchy": "All", "agent": "",
                "condition": cond, "rate_pct": pct, "label": "",
                "property_type": "", "trigger_pct": "", "rule_type": "",
                "amount_rm": "", "invoice_date_from": "", "effective_from": "",
                "remarks": "", "source": "default",
            }
            nfp_rates = [
                _default_tier("Sales Price > Net Floor Price", 25.0),
                _default_tier("System Price > Net Floor Price", 100.0),
                _default_tier("Sales Price < Net Floor Price", 20.0),
            ]

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



