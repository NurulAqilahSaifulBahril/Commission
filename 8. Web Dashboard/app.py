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

# Before db, auth, updater or any commission engine is imported. Each reads its
# proxy token from the environment at import time, so anything filling gaps has
# to run first or it runs too late to matter. It loads .env itself before
# filling anything, so a machine carrying real credentials still wins -- see
# bundled_keys for why that order is load-bearing.
import bundled_keys
bundled_keys.apply()

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
import query_replay
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
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
# The cache is kept between launches and thrown away only when the code that
# built it has changed -- see _cache_stamp(). It used to be deleted on every
# start by all three launchers (the Electron shell, Launch Dashboard.bat and
# start_dashboard.bat), so every launch began with a two-minute rebuild and
# every page sat on "still being prepared" until it finished.
#
# The file name changed with that. The Electron shell sits outside the OTA
# update's reach and still deletes "dashboard_cache.pkl" on every start; under
# a name it does not know, installs that update in place keep their cache
# without anyone having to reinstall.
CACHE_FILE = CURRENT_DIR / "data" / "dashboard_cache_stamped.pkl"
_CACHE_STAMP_KEY = "__stamp__"
_data_cache = {}
_disk_cache_lock = threading.RLock()
_response_refresh_lock = threading.Lock()
_refreshing_response_keys: set[tuple[int, int, str]] = set()
_RESPONSE_CACHE_TTL_SECONDS = 300
_SALES_REPORT_PAYLOAD_CACHE: dict[tuple, tuple[float, dict]] = {}

def _cache_stamp() -> str:
    """Fingerprint of everything that shapes what the cache holds.

    The version number alone is not enough: a code fix restarted on a
    development copy keeps the same version.json, and that is exactly the case
    the old delete-on-launch existed for. So the stamp also hashes the source of
    the dashboard and every commission script. Reading them costs a few
    milliseconds at startup, against a rebuild measured at about two minutes.
    """
    import hashlib
    h = hashlib.sha1()
    files = [REPO_ROOT / "version.json", CURRENT_DIR / "app.py",
             REPO_ROOT / "7. Presentation" / "build_commission_pack.py",
             REPO_ROOT / "agent_names.py"]
    for folder in ("1. Basic Commission", "2. NFP Commission", "3. ANP Commission",
                   "4. EGA ESA Awards", "5. Production Bonus"):
        base = REPO_ROOT / folder
        if base.is_dir():
            files.extend(sorted(p for p in base.rglob("*.py")
                                if "__pycache__" not in p.parts))
    for path in files:
        try:
            h.update(str(path.relative_to(REPO_ROOT)).encode("utf-8"))
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()


_CACHE_STAMP = _cache_stamp()


def load_disk_cache():
    global _data_cache
    with _disk_cache_lock:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "rb") as f:
                    loaded = pickle.load(f)
            except Exception as e:
                _log(f"Failed to load cache from disk: {e}\n" + traceback.format_exc())
                _data_cache = {}
                return
            if not isinstance(loaded, dict):
                _data_cache = {}
                return
            if loaded.get(_CACHE_STAMP_KEY) != _CACHE_STAMP:
                # Keep last month's yearly bundles so sign-in stays fast, but
                # drop assembled month pages. Those embed special-case rows
                # and hover labels; serving them after an inject/label change
                # is why the screen looked "not updated yet".
                _log("Disk cache was built by a different version; serving yearly bundles while month pages rebuild.")
                stale_pages = [k for k in loaded
                               if isinstance(k, tuple) and len(k) == 4 and k[0] == "commission_response"]
                for k in stale_pages:
                    loaded.pop(k, None)
            _data_cache = loaded
            query_replay.restore(_data_cache.get("sql_replay"))
            _SALES_REPORT_PAYLOAD_CACHE.clear()
            _log(f"Loaded cache from disk: {[k for k in _data_cache if k != _CACHE_STAMP_KEY]}")

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
            _data_cache[_CACHE_STAMP_KEY] = _CACHE_STAMP
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

# Bumped whenever the figures the report shows may have changed, so an open
# report page can tell it should reload (see /api/data-version).
_data_version = {"n": 0}
_data_version_lock = threading.Lock()


def _bump_data_version() -> None:
    with _data_version_lock:
        _data_version["n"] += 1


def clear_commission_cache():
    """A Data page setting changed: recalculate every commission figure, fast.

    Every Data page save lands here -- rates and rules, roles, special cases,
    profit sharing, referral overrides. None of them changes the invoices, so
    the rebuild answers each query from the record of the last download
    (query_replay.py) and only recalculates: a few seconds, where downloading
    everything again took about a minute.

    The bundles are no longer deleted first. They go on being served until the
    rebuild swaps in the new ones, so the report never drops to "Commission
    data is still being prepared" after a save. Response bodies ARE dropped at
    once: special cases and referral overrides are applied as a response is
    built, and have to show the moment they are saved.
    """
    with _disk_cache_lock:
        keys_to_remove = [
            k for k in _data_cache
            if isinstance(k, tuple) and len(k) == 4 and k[0] == "commission_response"
        ]
        for k in keys_to_remove:
            del _data_cache[k]
        save_disk_cache()

    # Rates, roles and names are read once and held; re-read them now.
    try:
        _rates_module().reset_cache()
    except Exception:
        pass
    try:
        agent_names.reset_cache()
    except Exception:
        pass

    dynamic_mods = [
        "int_basic_commission", "int_anp_commission", "int_nfp_commission", "int_ega_esa",
        "out_basic_commission", "out_anp_commission", "out_nfp_commission", "out_ega_esa"
    ]
    for m in dynamic_mods:
        sys.modules.pop(m, None)

    _clear_derived_caches()
    _bump_data_version()

    _log("Data page change: recalculating commissions from the last download...")
    start_prefetch(2026, replay=True)


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
    # A month payload can be rebuilt from bundles already on disk. Only hit
    # the database when those bundles are missing — a full prefetch takes
    # minutes and used to wipe the month cache mid-page-load.
    existing = get_cached_data(year, agent_type)
    if existing and existing.get("invoice_dates"):
        return
    key = (int(year), str(agent_type).lower())
    with _response_refresh_lock:
        if key in _refreshing_response_keys:
            return
        _refreshing_response_keys.add(key)

    def _worker() -> None:
        try:
            start_prefetch(year)
        finally:
            with _response_refresh_lock:
                _refreshing_response_keys.discard(key)

    threading.Thread(target=_worker, daemon=True).start()


# First paint after sign-in must return within this many seconds. The year
# rebuild keeps running in the background; the next poll picks it up.
_COMMISSION_REQUEST_SECONDS = 5
_commission_build_lock = threading.Lock()
_commission_build_futs: dict[tuple, object] = {}
_commission_build_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="comm-build")


def _decorate_commission_payload(payload: dict, year: int, month: int) -> dict:
    if isinstance(payload, dict):
        payload["agent_roles"] = _agent_roles_for_month(year, month)
        payload["agent_role_history"] = _agent_role_history()
        payload["agent_ics"] = agent_names.get_ic_map()
    return payload


def _commission_payload_within_seconds(year: int, month: int, agent_type: str,
                                       seconds: float = _COMMISSION_REQUEST_SECONDS):
    """Return a month payload in `seconds` or None so the page can retry.

    Concurrent requests for the same month share one build. The first one
    that finishes writes the response cache; later hits are instant."""
    cached_response = get_cached_commission_response(year, month, agent_type)
    if cached_response and isinstance(cached_response, dict) and cached_response.get("payload"):
        return cached_response["payload"]

    key = (int(year), int(month), str(agent_type).lower())
    with _commission_build_lock:
        fut = _commission_build_futs.get(key)
        if fut is None or fut.done():
            if fut is not None and fut.done() and fut.exception() is None:
                result = fut.result()
                if result is not None:
                    return result
            fut = _commission_build_pool.submit(
                _build_commission_payload, int(year), int(month), str(agent_type).lower())
            _commission_build_futs[key] = fut
    try:
        return fut.result(timeout=seconds)
    except FuturesTimeout:
        return None


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
    so no row is restated: the new figure simply replaces his cell. The old
    one is kept in the special case itself, not shown (decided 12 Oct 2026).
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
        if len(row) > len(headers) or original == "-":
            return

        new_value = _special_case_money(gan_comm)
        if new_value == original:
            return
        row[gan_i] = new_value
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

    def idx(*names: str) -> int:
        for name in names:
            if not name:
                continue
            target = name.lower().strip()
            for i, h in enumerate(headers):
                if str(h).lower().strip() == target:
                    return i
        return -1

    agent_i = idx("Agent")
    customer_i = idx("Customer")
    pkg_i = idx("Property Type", "Package Type", "Package")
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

    def _place_row(new_row: list, agent_name: str, customer_name: str, kind: str) -> None:
        """Insert a restated row directly under the row it replaces.

        Mirrors insertSpecialCaseRows() in app.js, which is where the Add
        Special Case modal puts them: New Basic Commission under the original
        Basic Commission, New Net Floor Price Commission under the original
        NFP. Appending them to the end of the table instead split every case
        from its invoice, so filtering to an agent showed the original in the
        agent's block and the replacement at the bottom, and the report's
        side-by-side rendering of old and new never engaged.

        A second case for the same row stacks under the first. A customer with
        no original row of that kind goes after their last row, then after the
        agent's last row, and only then at the end.
        """
        want_a = agent_name.lower().strip()
        want_c = customer_name.lower().strip()
        cur_agent = ""
        anchor = cust_last = agent_last = -1
        for i, r in enumerate(rows):
            cell = str(r[agent_i] or "").strip() if agent_i != -1 and len(r) > agent_i else ""
            if cell and cell != "-":
                cur_agent = cell
            low = cur_agent.lower().strip()
            if low != want_a or "total" in low or "summary" in low or "grand" in low:
                continue
            agent_last = i
            cust = (str(r[customer_i] or "").strip().lower()
                    if customer_i != -1 and len(r) > customer_i else "")
            if cust != want_c:
                continue
            cust_last = i
            if anchor != -1 or len(r) > len(headers):
                continue          # already found, or a restated row itself
            comm = str(r[comm_i] or "").lower() if comm_i != -1 else ""
            is_nfp = "net floor" in comm or "netfloor" in comm
            if (kind == "nfp") == is_nfp:
                anchor = i
        if anchor != -1:
            # Step past replacements already stacked under this row.
            while (anchor + 1 < len(rows) and len(rows[anchor + 1]) > len(headers)
                   and str(rows[anchor + 1][customer_i] or "").strip().lower() == want_c
                   and (("net floor" in str(rows[anchor + 1][comm_i] or "").lower()) == (kind == "nfp"))):
                anchor += 1
            pos = anchor + 1
        elif cust_last != -1:
            pos = cust_last + 1
        elif agent_last != -1:
            pos = agent_last + 1
        else:
            pos = len(rows)
        rows.insert(pos, new_row)

    _package_prices: dict = {}

    def _package_price(customer_name: str) -> float:
        """The customer's package price -- the package line on their invoice(s),
        summed -- from the cached NFP rows. The table's System Price column
        shows the invoice Total Amount, so it cannot answer this. 0 if unknown."""
        if not _package_prices:
            try:
                bundle = get_cached_data(year, agent_type) or {}
                nfp_tuple = bundle.get("nfp") or ()
                by_inv = nfp_tuple[4] if len(nfp_tuple) > 4 else {}
                for inv in (by_inv or {}).values():
                    key = str(getattr(inv, "customer_name", "") or "").strip().lower()
                    _package_prices[key] = (_package_prices.get(key, 0.0)
                                            + float(getattr(inv, "system_price", 0) or 0))
            except Exception:
                pass
            _package_prices.setdefault("", 0.0)
        return _package_prices.get(customer_name.strip().lower(), 0.0)

    def _inject_order(c):
        kind = str((c or {}).get("rowKind") or "all").strip().lower()
        # Basic first so its auto NFP restatement (new sales, same floor)
        # lands above a later NFP-cell assignment (new floor).
        if kind == "basic":
            return 0
        if kind == "nfp":
            return 1
        if kind == "gan":
            return 2
        return 3

    for case in sorted(cases, key=_inject_order):
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
            adjusted_sales_price = case.get("adjustedSalesPrice")
            adjusted_sales_price = float(adjusted_sales_price) if adjusted_sales_price else 0.0
            # "Adjusted Cash Price After Discount" and "Adjusted Rate on
            # Adjusted Cash Price %": the NFP commission's own Cash Price. It
            # outranks a Basic case's New Sales Price (copied below for other
            # NFP types), and only an NFP case carries one.
            adjusted_cash_price = case.get("adjustedCashPrice")
            adjusted_cash_price = float(adjusted_cash_price) if adjusted_cash_price else 0.0
            if special_case_type in ("adjusted_cash_price", "adjusted_cash_price_rate") and adjusted_cash_price:
                adjusted_sales_price = adjusted_cash_price
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
        # app.js — kept in lockstep so the saved value can never drift from
        # what the modal previewed. A management-adjusted sales price replaces
        # the auto-calculated one for every downstream calc here too.
        # An NFP-only case copies the Basic cell's New Sales Price when it
        # did not store one of its own.
        row_kind_early = str(case.get("rowKind") or "all").strip().lower()
        if not adjusted_sales_price and row_kind_early == "nfp":
            want_a, want_c = agent.lower(), customer.lower()
            for other in cases:
                if other is case:
                    continue
                if str(other.get("rowKind") or "").strip().lower() != "basic":
                    continue
                if str(other.get("agent") or "").strip().lower() != want_a:
                    continue
                if str(other.get("customer") or "").strip().lower() != want_c:
                    continue
                other_adj = other.get("adjustedSalesPrice")
                try:
                    other_adj = float(other_adj) if other_adj else 0.0
                except (TypeError, ValueError):
                    other_adj = 0.0
                if other_adj:
                    adjusted_sales_price = other_adj
                break
        sales_for_calc = adjusted_sales_price if adjusted_sales_price else sales
        basic_comm = sales_for_calc * ((rate + profit_sharing_pct) / 100)
        # A Balance row's figure is net of the RM 300 advance already paid. A
        # case saved since 2026-10-23 keeps the assigned rate and carries that
        # advance; older cases folded it into their rate instead.
        try:
            basic_comm -= float(case.get("advanceDeducted") or 0)
        except (TypeError, ValueError):
            pass

        if special_case_type == "fee_waiver":
            # A fee waiver sets the Net Floor Price Commission to RM 0 outright
            # (decided 12 Oct 2026). It used to add a typed amount to the
            # original figure, which Finance only ever used to cancel a
            # deduction, and which stopped landing on 0 once NFP moved to the
            # Cash Price.
            nfp_comm = 0.0
        else:
            # NFP commission = (Cash Price - Net Floor Price) x Rate
            # (invoice_price_basis.nfp_commission, corrected 13 Oct 2026): 100%
            # above the package price, 25% above the floor, 20% below it, where
            # the difference is negative and so deducts. "Adjusted Net Floor
            # Price Commission Rate %" replaces whichever rate applies and is
            # taken as it stands: 0.5 pays (Cash Price - Floor) x 0.5%.
            above_rate, below_rate, package_rate = 0.25, 0.20, 1.00
            floor_gap = sales_for_calc - nfp
            typed_rate = None
            cash_rate = None
            if special_case_type in ("adjusted_nfp_rate", "cash_price_rate", "adjusted_cash_price_rate"):
                try:
                    raw_nfp_rate = str(case.get("nfpRatePct") or "").strip()
                    if raw_nfp_rate:
                        if special_case_type in ("cash_price_rate", "adjusted_cash_price_rate"):
                            cash_rate = float(raw_nfp_rate) / 100
                        else:
                            typed_rate = float(raw_nfp_rate) / 100
                except (TypeError, ValueError):
                    pass
            package_price = _package_price(customer)
            if cash_rate is not None:
                # "Adjusted Rate on Cash Price %" and "Adjusted Rate on Adjusted
                # Cash Price %" (13 Oct 2026): the whole Cash Price -- the
                # invoice's, or the one typed with the case -- x the typed rate,
                # sign included; the floor plays no part.
                nfp_comm = sales_for_calc * cash_rate
            elif typed_rate is not None:
                nfp_comm = floor_gap * typed_rate
            elif package_price > 0 and sales_for_calc > package_price and floor_gap > 0:
                nfp_comm = floor_gap * package_rate
            elif floor_gap > 0:
                nfp_comm = floor_gap * above_rate
            elif floor_gap < 0:
                nfp_comm = floor_gap * below_rate
            else:
                nfp_comm = 0.0

        if special_case_type == "fee_waiver":
            nfp_comm_str = "RM 0.00"
        elif nfp == 0:
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
        if row_kind in ("basic", "all"):
            _place_row(build_row("New Basic Commission",
                                 _special_case_money(basic_comm),
                                 _special_case_money(sales_for_calc)),
                       agent, customer, "basic")

        # First NFP change lives on its own NFP case (filed from Basic New
        # Sales Price). Only auto-inject from the Basic case when that NFP
        # case has not been saved yet, so the two never stack as duplicates.
        has_nfp_case = any(
            str((other or {}).get("rowKind") or "").strip().lower() == "nfp"
            and str((other or {}).get("agent") or "").strip().lower() == agent.lower()
            and str((other or {}).get("customer") or "").strip().lower() == customer.lower()
            for other in cases
        )
        sales_restates_nfp = (row_kind == "basic" and bool(adjusted_sales_price)
                              and not has_nfp_case)
        if row_kind in ("nfp", "all") or sales_restates_nfp:
            nfp_sales = (_special_case_money(sales_for_calc)
                         if sales_for_calc else "-")
            nfp_row = build_row("New Net Floor Price Commission", nfp_comm_str, nfp_sales)
            if agent_i != -1:
                nfp_row[agent_i] = ""  # matches the client: the NFP row's Agent cell is blanked
            _place_row(nfp_row, agent, customer, "nfp")


# â”€â”€ Background pre-fetching â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_prefetch_lock = threading.Lock()
_is_prefetching = False

# A rule change made while a rebuild is already running must not be dropped:
# that rebuild started from the old rules. It is queued and run straight after.
_prefetch_again = {"pending": False}


def prefetch_data_worker(year=2026, replay=False):
    """Rebuild the commission bundles. With `replay`, queries identical to the
    last download are answered from its record rather than the database --
    the Data page fast path. Without it (scheduled refresh, first start),
    everything is downloaded fresh."""
    import contextlib
    import time as _time
    while True:
        started = _time.time()
        with (query_replay.replaying() if replay else contextlib.nullcontext()):
            outcome = _prefetch_run(year)
        if outcome == "busy":
            if replay:
                with _prefetch_lock:
                    _prefetch_again["pending"] = True
            return
        if replay:
            _log(f"Recalculated after a Data page change in {_time.time() - started:.1f}s "
                 f"(query record: {query_replay.stats()}).")
        with _prefetch_lock:
            again = _prefetch_again["pending"]
            _prefetch_again["pending"] = False
        if not again:
            return
        _log("Another Data page change arrived during that rebuild; recalculating again.")
        replay = True


def _prefetch_run(year=2026):
    global _is_prefetching
    with _prefetch_lock:
        if _is_prefetching:
            return "busy"
        _is_prefetching = True
    
    _log("Starting background pre-fetch...")
    try:
        # Resolve credentials
        token, url, db_name = build_commission_pack._resolve_proxy_credentials()
        if not token:
            _log("Prefetch aborted: Postgres proxy token not found.")
            return

        # Pre-fetch all agents and customers list
        if not query_replay.is_replaying():
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
        # Invoice dates and both channels' bundles are fetched together. The
        # internal bundle used to wait for the outsource one to finish -- a
        # leftover from chasing down a hang -- which put the two back to back
        # and made a cold rebuild about two minutes long.
        _log("Pre-fetching commission bundles in parallel...")
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_invoice_dates = ex.submit(build_commission_pack.fetch_invoice_dates, year, basic_mod)
                fut_outsource = ex.submit(_fetch_commission_bundle, year, "outsource")
                fut_internal = ex.submit(_fetch_commission_bundle, year, "internal")

                invoice_dates_map = fut_invoice_dates.result()
                _log("Invoice dates fetched successfully")
                out_bundle = fut_outsource.result()
                _log("Outsource bundle fetched successfully")
                # A failure here aborts the refresh and leaves whatever is
                # already cached in place. It used to swap in an empty internal
                # bundle, which could not even be saved -- it had no ega_raw, so
                # the next line raised -- and, had it been, would have blanked
                # every internal figure. Now that a refresh runs over data the
                # pages are already showing, keeping the old copy is the only
                # safe outcome.
                int_bundle = fut_internal.result()
                _log("Internal bundle fetched successfully")
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

        # Everything computed from the bundles just replaced is now out of date.
        # Cached commission responses in particular were never dropped by a
        # rebuild: past their five-minute mark they asked for a fresh prefetch
        # and went on being served regardless. That hardly mattered while every
        # launch deleted the cache; now that the cache outlives a restart, a
        # response from yesterday would otherwise stay on screen indefinitely.
        with _disk_cache_lock:
            for k in [k for k in _data_cache
                      if isinstance(k, tuple) and len(k) == 4 and k[0] == "commission_response"]:
                del _data_cache[k]
            _data_cache["sql_replay"] = query_replay.snapshot()
            save_disk_cache()
        _clear_derived_caches()
        _bump_data_version()
        _log("Dropped results computed from the previous data.")
        # Sign-in opens on a month view. Assemble those payloads now so the
        # first /api/commission is a cache hit instead of a 15-second wait.
        try:
            from datetime import date as _date
            warm_months = {5, _date.today().month}
            for warm_month in sorted(warm_months):
                for warm_type in ("internal", "outsource"):
                    try:
                        _build_commission_payload(year, warm_month, warm_type)
                    except Exception as warm_err:
                        _log(f"Warm {warm_type} {year}-{warm_month:02d} failed: {warm_err}")
        except Exception:
            pass

    except Exception as e:
        _log("[PREFETCH ERROR]\n" + traceback.format_exc())
    finally:
        with _prefetch_lock:
            _is_prefetching = False

def start_prefetch(year=2026, replay=False):
    t = threading.Thread(target=prefetch_data_worker, args=(year, replay), daemon=True)
    t.start()


@app.route("/api/data-version")
@login_required
def data_version_api():
    """A counter that moves whenever the report's figures may have changed, so
    an open report page can reload itself after a Data page save instead of
    waiting for someone to press refresh."""
    _sync_data_page_rules()
    with _prefetch_lock:
        rebuilding = bool(_is_prefetching) or bool(_prefetch_again["pending"])
    with _data_version_lock:
        return jsonify({"version": _data_version["n"], "rebuilding": rebuilding})


def _fetch_commission_bundle(year: int, agent_type: str):
    """Fetch basic, ANP, NFP, EGA and raw EGA data for one agent type.

    The five run together. Each is its own round of proxy queries with nothing
    shared between them -- the commission modules are loaded once at startup and
    each fetch only reads them -- and run one after another they took about a
    minute per channel, the slowest single one about twenty seconds.
    """
    _log(f"Starting fetch for {agent_type} agents...")
    B = build_commission_pack
    if agent_type == "internal":
        fns = {
            "basic": lambda: B.fetch_internal_basic(year, h1_only=False),
            "anp": lambda: B.fetch_internal_anp(year, h1_only=False),
            "nfp": lambda: B.fetch_internal_nfp(year, h1_only=False),
            "ega": lambda: B.fetch_internal_ega_esa(year, may_only=False),
            "ega_raw": lambda: B.fetch_internal_ega_raw(year),
        }
    else:
        fns = {
            "basic": lambda: B.fetch_outsource_basic(year, h1_only=False),
            "anp": lambda: B.fetch_outsource_anp(year, h1_only=False),
            "nfp": lambda: B.fetch_outsource_nfp(year, h1_only=False),
            "ega": lambda: B.fetch_outsource_ega_esa(year, may_only=False),
            "ega_raw": lambda: B.fetch_outsource_ega_raw(year),
        }

    def _with_retries(name, fn):
        # The proxy fails intermittently -- "HTTP 502 from proxy: upstream error"
        # -- and more often when several queries arrive at once. One retry
        # after a short pause clears nearly all of it, so a blip no longer
        # throws away a refresh that is otherwise complete.
        for attempt in range(3):
            try:
                return fn()
            except Exception as e:
                if attempt == 2:
                    raise
                _log(f"{name} fetch for {agent_type} failed ({e}); retrying...")
                time.sleep(2 * (attempt + 1))

    try:
        with ThreadPoolExecutor(max_workers=len(fns)) as ex:
            futures = {name: ex.submit(_with_retries, name, fn) for name, fn in fns.items()}
            out = {}
            for name, fut in futures.items():
                # The basic fetch keeps the three-minute ceiling it always had.
                out[name] = fut.result(timeout=180) if name == "basic" else fut.result()
                _log(f"{name} data fetched for {agent_type}")
        return out
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
# Keep people signed in. The login cookie used to last only while the window
# was open, so every launch of the dashboard began at the sign-in page. It now
# lasts 14 days and each visit pushes that out again, so anyone who uses the
# dashboard at least every two weeks stays signed in. auth.current_user()
# re-checks the account, so deactivating a user or resetting a password still
# signs them out.
from datetime import timedelta as _timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(days=14)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
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
# Registered before the startup prefetch below, which may run the ANP
# calculation straight away from the saved query record.
def _ega_row_ep(r: dict, ega_mod):
    """EGA/ESA EP of one ega_raw invoice row: the figure set by hand in
    SALES_REPORT_EP_OVERRIDES, else sales price (total - EPP) through the
    award module's calc_ep_points(). None when it cannot be worked out."""
    from decimal import Decimal as _D, ROUND_HALF_UP as _RH
    inv = str(r.get("invoice_number") or "").strip()
    bid = str(r.get("bubble_id") or "").strip()
    hand = SALES_REPORT_EP_OVERRIDES.get(bid) or SALES_REPORT_EP_OVERRIDES.get(inv)
    try:
        if hand is not None:
            return _D(str(hand))
        if ega_mod is None:
            return None
        sales = _D(str(r.get("total_amount") or 0)) - _D(str(r.get("epp_interest") or 0))
        ep = ega_mod.calc_ep_points(sales, ega_mod.classify_property_type(r),
                                    r.get("invoice_date"), int(r.get("panel_qty") or 0))
        return ep.quantize(_D("0.01"), rounding=_RH)
    except Exception:
        return None


def _ega_ep_by_invoice(ega_raw, ega_mod) -> dict:
    """{invoice_number: EP} over ega_raw rows (see _ega_row_ep)."""
    out = {}
    for r in ega_raw or []:
        inv = str(r.get("invoice_number") or "").strip()
        if not inv:
            continue
        ep = _ega_row_ep(r, ega_mod)
        if ep is not None:
            out[inv] = ep
    return out


_ANP_EP_CACHE: dict = {}
_ANP_EP_CACHE_SECONDS = 300


def _anp_ep_lookup_factory(year: int):
    """The per-invoice EP the ANP tier is measured in (build_commission_pack
    ANP_EP_LOOKUP_FACTORY): the same EGA/ESA EP the ANP table's EP Point
    column shows, keyed on invoice number, for internal and outsource alike.
    Built at most once every few minutes per year, since the internal and
    outsource ANP runs ask for it back to back."""
    now = time.time()
    hit = _ANP_EP_CACHE.get(year)
    if hit and now - hit[0] < _ANP_EP_CACHE_SECONDS:
        ep_map = hit[1]
    else:
        ep_map = {}
        for fetch, mod_name in ((build_commission_pack.fetch_internal_ega_raw, "int_ega_esa"),
                                (build_commission_pack.fetch_outsource_ega_raw, "out_ega_esa")):
            try:
                ep_map.update(_ega_ep_by_invoice(fetch(year), sys.modules.get(mod_name)))
            except Exception as e:
                _log(f"[ANP EP] could not read {mod_name} invoices for {year}: {e}")
        _ANP_EP_CACHE[year] = (now, ep_map)

    def lookup(inv: dict):
        return ep_map.get(str(inv.get("invoice_number") or "").strip())
    return lookup


build_commission_pack.ANP_EP_LOOKUP_FACTORY = _anp_ep_lookup_factory


load_disk_cache()
with _disk_cache_lock:
    has_main_keys = (2026, 'internal') in _data_cache and (2026, 'outsource') in _data_cache
if SETUP_ERROR is not None:
    # No keys, no data: every fetch would fail the same way init_db just did.
    has_main_keys = True
if not has_main_keys:
    _log("Cache is missing internal or outsource data on startup. Triggering background pre-fetch...")
    start_prefetch(2026, replay=True)
elif SETUP_ERROR is None:
    # Pages open straight away on the data saved last time, and the rebuild
    # runs behind them. Each piece replaces its old copy only once the new one
    # is complete, so nobody ever sees a half-built figure -- just figures that
    # catch up to the database a minute or two after launch.
    _log("Serving cached data from the last session; refreshing it in the background...")
    start_prefetch(2026, replay=True)

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
            "agent_ics": agent_names.get_ic_map(),
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
@login_required
def agent_name_map():
    """Nickname -> canonical full-name lookup so the frontend can display full
    agent names (Title Case) in every table. Also the IC map the Monthly
    Commission Slip prints. Both are sourced from Agent Roles & Hierarchy on
    the Data page — the only place IC is stored."""
    try:
        return jsonify({
            "map": agent_names.get_map(),
            "ic_map": agent_names.get_ic_map(),
        })
    except Exception:
        _log("[AGENT NAME MAP ERROR]\n" + traceback.format_exc())
        return jsonify({"map": {}, "ic_map": {}}), 200

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
    # The pages this account may open, for the menu (static/greeting.js).
    return jsonify({**user, "available_pages": auth.allowed_pages(user)})


# Which Page Access key each page and its data belong to (auth.PAGES). "report"
# is the commission report as a whole; a section of it is checked by key.
_PAGE_BY_PATH = {
    "/": "overview", "/api/overview": "overview",
    "/sales-report": "sales_report", "/api/sales-report": "sales_report",
    "/api/sales-report/pdf": "sales_report",
    "/data": "data",
    "/report": "report", "/api/commission": "report",
    "/api/download/pdf": "report", "/api/download/excel": "report",
    "/api/download/excel-pdf": "report",
}


@app.before_request
def _enforce_available_pages():
    """Refuse a page, or its data, that the signed-in account may not open
    (the Page Access column on the Admin page). Anyone not signed in is
    left to the routes' own login checks."""
    key = _PAGE_BY_PATH.get(request.path)
    if not key:
        return None
    user = auth.current_user()
    if not user:
        return None
    allowed = auth.allowed_pages(user)
    if key == "report":
        mine = [k for k in auth.COMMISSION_PAGES if k in allowed]
        section = (request.args.get("section") or "").strip().lower()
        ok = bool(mine) and (not section or section not in auth.COMMISSION_PAGES or section in mine)
        if not ok and request.path == "/report" and mine:
            return redirect(f"/report?section={mine[0]}")
        # The report opens on Basic & NFP; someone without it opens on theirs.
        if ok and request.path == "/report" and not section and "basic_nfp" not in mine:
            return redirect(f"/report?section={mine[0]}")
    else:
        ok = key in allowed
    if ok:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "This page is not available to your account"}), 403
    target = auth.first_page_path(user)
    if target and target.split("?")[0] != request.path:
        return redirect(target)
    return "No pages are available to your account. Ask an admin to give you access.", 403


def _restrict_commission_sections(payload: dict) -> dict:
    """The commission report's data with only the sections this account may
    open. agent_summary is the Basic & NFP rollup (its Commission Price and
    Other Commission), so it goes with basic_nfp."""
    user = auth.current_user()
    allowed = auth.allowed_pages(user)
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), dict):
        return payload
    hidden = [k for k in auth.COMMISSION_PAGES if k not in allowed]
    if not hidden:
        return payload
    if "basic_nfp" in hidden:
        hidden.append("agent_summary")
    return {**payload, "sections": {k: v for k, v in payload["sections"].items() if k not in hidden}}


@app.route("/admin")
@admin_required
def admin_page():
    return send_from_directory(str(CURRENT_DIR / "static"), "admin.html")


@app.route("/api/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users_api():
    if request.method == "GET":
        users = [dict(u) for u in db.list_users()]
        for u in users:
            u["available_pages"] = auth.allowed_pages({"role": u.get("role"),
                                                       "pages": auth.parse_pages(u.get("pages"))})
            u.pop("pages", None)
        return jsonify({"users": users,
                        "pages": [{"key": k, "label": l} for k, l, _p in auth.PAGES]})

    req_data = request.json or {}
    username = str(req_data.get("username", "")).strip()
    name = str(req_data.get("name", "") or "").strip()
    password = str(req_data.get("password", ""))
    role = str(req_data.get("role", "staff"))
    if role not in ("admin", "staff"):
        return jsonify({"error": "Role must be 'admin' or 'staff'"}), 400
    if not username or len(password) < 6:
        return jsonify({"error": "Username required and password must be at least 6 characters"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": f"User '{username}' already exists"}), 409

    user_id = db.create_user(username, auth.hash_password(password), role, name)
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
        auth.forget_user(user_id)
        db.insert_audit(actor["username"], "delete", "user", f"Deactivated user '{target['username']}'", user_id=actor["id"])
        return jsonify({"status": "success"})

    req_data = request.json or {}
    updates: dict = {}
    if "username" in req_data and str(req_data["username"]).strip():
        updates["username"] = str(req_data["username"]).strip()
    if "name" in req_data:
        # Blank is allowed and clears the name.
        updates["name"] = str(req_data["name"] or "").strip()
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
    if "pages" in req_data:
        chosen = [k for k in (req_data.get("pages") or []) if k in auth.PAGE_KEYS and k != "admin"]
        everything = [k for k in auth.PAGE_KEYS if k != "admin"]
        # Every page ticked is stored as "no limit", so pages added later
        # reach the account without anyone ticking them.
        updates["pages"] = "" if set(chosen) >= set(everything) else ",".join(chosen) or "none"

    db.update_user(user_id, **updates)
    auth.forget_user(user_id)
    # Every change to a user used to land in the audit log as the same
    # "Updated user 'x'", so a password reset and a reactivation were
    # indistinguishable after the fact -- which is most of what the log is
    # for. Say which fields moved, never the values of the secret ones.
    changed = []
    if "username" in updates:
        changed.append(f"renamed to '{updates['username']}'")
    if "name" in updates:
        changed.append(f"name -> '{updates['name']}'" if updates["name"] else "name cleared")
    if "role" in updates:
        changed.append(f"role -> {updates['role']}")
    if "password_hash" in updates:
        changed.append("password set")
    if "is_active" in updates:
        changed.append("reactivated" if updates["is_active"] else "deactivated")
    if "pages" in updates:
        labels = dict((k, l) for k, l, _p in auth.PAGES)
        keys = auth.parse_pages(updates["pages"])
        changed.append("page access -> all" if keys is None
                       else "page access -> " + (", ".join(labels[k] for k in keys) or "none"))
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


def _inverter_label(package_name, package_description) -> str:
    """"Hybrid" / "String" / "Microinverter", or "" when the invoice does not say.

    The package name is read first: a hybrid invoice's item text only lists the
    parts ("SAJ H2 ..."), and only its package, e.g. "[1P] HYBRID SAJ JINKO 15
    PCS", says hybrid. Invoices with no linked package fall back to the item
    text, where string packages name a "String Inverter" and micro-inverter
    jobs list NEP BDM units.
    """
    for text in (package_name, package_description):
        low = str(text or "").lower()
        if "hybrid" in low:
            return "Hybrid"
        if "string" in low:
            return "String"
    low = str(package_description or "").lower()
    if "bdm" in low or "micro" in low:
        return "Microinverter"
    return ""


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


def _as_at_date(value, as_at: str) -> str:
    """A milestone date, or "" when it falls after the month being reported."""
    text = str(value or "")[:10]
    if not text:
        return ""
    return "" if build_commission_pack._is_after_month(text, as_at) else text


def _pct_text(value) -> str:
    """4.99 -> "4.99", 100 -> "100"."""
    try:
        text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""
    return text


def _build_payout_policies(basic_lines, as_at: str = "") -> dict:
    """customer (lower case) -> the payout rule each of their invoices is paid
    under, as the Data page states it, with the date each stage is reached.

    The report page used to assume every July-onward invoice settles its
    balance at 75%: the Balance Payout filter tested the 75% milestone and the
    hover said "Balance Payout (75% Payment)" whatever the Data page said. A
    role moved to "pays at 100%" kept showing an 80%-paid invoice in the
    balance run. These entries carry the rule the engine actually applied --
    the same PayoutPolicy that priced the line -- so the page can follow it.
    """
    out: dict = {}
    for ln in basic_lines or []:
        customer = str(getattr(ln, "customer_name", "") or "").strip().lower()
        policy = getattr(ln, "payout_policy", None)
        if not customer or policy is None:
            continue
        multi = bool(getattr(policy, "is_multi_stage", False))
        entry = {
            "invoice": str(getattr(ln, "invoice_number", "") or ""),
            "invoice_date": str(getattr(ln, "invoice_date", "") or "")[:10],
            "multi_stage": multi,
            "advance_pct": _pct_text(policy.advance_trigger) if multi else "",
            "advance_rm": _pct_text(policy.advance_amount) if multi else "",
            "balance_pct": _pct_text(policy.balance_trigger),
            # pct75_date/pct5_date on a line are named for the 2026 defaults
            # but hold whatever milestones this policy names.
            # The advance counts from its payment's Created at (see
            # advance_recognized_date), so it reads as pending until then.
            "advance_date": (str(getattr(ln, "pct5_date", "") or "")[:10]
                             if multi and _as_at_date(build_commission_pack.advance_recognized_date(ln), as_at)
                             else ""),
            # A stage the invoice only reached after this month reads as
            # pending here: the report is the month as it stood.
            "balance_date": _as_at_date(getattr(ln, "pct75_date", ""), as_at),
        }
        # Created at of the payment that reached each stage -- when Finance
        # entered it. Blank whenever the stage itself reads as pending.
        entry["advance_created"] = (str(getattr(ln, "pct5_created", "") or "")[:10]
                                    if entry["advance_date"] else "")
        entry["balance_created"] = (str(getattr(ln, "pct75_created", "") or "")[:10]
                                    if entry["balance_date"] else "")
        bucket = out.setdefault(customer, [])
        if entry not in bucket:
            bucket.append(entry)
    for entries in out.values():
        entries.sort(key=lambda e: e.get("invoice_date") or "")
    _add_advance_reached_pct(out)
    _add_current_payment(out)
    return out


def _add_current_payment(policies: dict) -> None:
    """Give each invoice its latest payment: "paid", "total" and "paid_pct",
    counting every payment received up to now -- not as at the month being
    reported -- for the "Current payment" line on the Basic Commission hover
    (Rabihah Binti Ismail: 80%, RM 22,880.00 of RM 28,600.00). One query for
    all of them, same payment exclusions as the milestone SQL; where an
    invoice number has several versions, the one with the most paid on it is
    used. On any failure the hover simply leaves the line out."""
    want = {}
    for entries in policies.values():
        for e in entries:
            if e.get("invoice"):
                want.setdefault(e["invoice"].strip(), []).append(e)
    if not want:
        return
    try:
        from basic_commission_rates import MILESTONE_PAYMENT_EXCLUSIONS as _excl
    except Exception:
        _excl = "101334, 104412, 101333, 104413, 4899"
    inv_list = ", ".join("'" + inv.replace("'", "''") + "'" for inv in want)
    try:
        rows = _pg_query(f"""
            SELECT TRIM(i.invoice_number) AS inv, i.bubble_id, i.total_amount,
                   COALESCE(SUM(p.amount), 0) AS paid
            FROM invoice i
            JOIN payment p ON p.linked_invoice = i.bubble_id
            WHERE TRIM(i.invoice_number) IN ({inv_list})
              AND p.id NOT IN ({_excl})
            GROUP BY TRIM(i.invoice_number), i.bubble_id, i.total_amount
        """)
    except Exception as e:
        _log(f"[CURRENT PAYMENT] could not read payments: {e}")
        return
    best: dict = {}
    for r in rows or []:
        inv = str(r.get("inv") or "")
        try:
            total, paid = float(r.get("total_amount") or 0), float(r.get("paid") or 0)
        except (TypeError, ValueError):
            continue
        if total > 0 and (inv not in best or paid > best[inv][1]):
            best[inv] = (total, paid)
    for inv, (total, paid) in best.items():
        for e in want.get(inv, []):
            e["paid"] = round(paid, 2)
            e["total"] = round(total, 2)
            e["paid_pct"] = _pct_text(round(paid / total * 100, 2))


def _add_advance_reached_pct(policies: dict) -> None:
    """Give each advanced invoice its "advance_reached_pct": the share of the
    invoice actually paid by the payment that reached the advance trigger, so
    the Commission Price hover can say "Advance Commission = RM 300 as achieved
    5%" (Lim Ai Lian paid 5% on 23 Sep 2026) rather than restate the 4.5%
    trigger. One query for all of them; on any failure the hover keeps the
    trigger. Same payment exclusions as the milestone SQL."""
    want = {}
    for entries in policies.values():
        for e in entries:
            if e.get("multi_stage") and e.get("advance_date") and e.get("invoice"):
                want.setdefault(e["invoice"].strip(), []).append(e)
    if not want:
        return
    try:
        from basic_commission_rates import MILESTONE_PAYMENT_EXCLUSIONS as _excl
    except Exception:
        _excl = "101334, 104412, 101333, 104413, 4899"
    inv_list = ", ".join("'" + inv.replace("'", "''") + "'" for inv in want)
    try:
        rows = _pg_query(f"""
            SELECT TRIM(i.invoice_number) AS inv, i.bubble_id, i.total_amount,
                   p.payment_date, p.id, p.amount
            FROM payment p
            JOIN invoice i ON i.bubble_id = p.linked_invoice
            WHERE TRIM(i.invoice_number) IN ({inv_list})
              AND p.id NOT IN ({_excl})
            ORDER BY i.bubble_id, p.payment_date, p.id
        """)
    except Exception as e:
        _log(f"[ADVANCE %] could not read payments: {e}")
        return
    from collections import defaultdict as _dd
    by_bubble = _dd(list)
    for r in rows or []:
        by_bubble[(str(r.get("inv") or ""), str(r.get("bubble_id") or ""))].append(r)
    for (inv, _bid), pays in by_bubble.items():
        for e in want.get(inv, []):
            if e.get("advance_reached_pct"):
                continue
            try:
                total = float(pays[0].get("total_amount") or 0)
                trigger = float(e.get("advance_pct") or 0) / 100.0
            except (TypeError, ValueError):
                continue
            if total <= 0:
                continue
            running = 0.0
            for r in pays:
                running += float(r.get("amount") or 0)
                if running >= total * trigger - 1e-9:
                    # The invoice version whose crossing date is the advance date.
                    if str(r.get("payment_date") or "")[:10] == e["advance_date"]:
                        e["advance_reached_pct"] = _pct_text(round(running / total * 100, 2))
                    break


def _invoice_price_breakdown(inv):
    """invoice_price_basis.price_breakdown for one NFP engine invoice record,
    or None when the record predates its `price_items` (an older cache)."""
    items = getattr(inv, "price_items", None)
    if items is None:
        return None
    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        import invoice_price_basis as ipb
        return ipb.price_breakdown(getattr(inv, "total_amount", 0), getattr(inv, "epp_cost", 0),
                                   getattr(inv, "invoice_date", None), items)
    except Exception:
        return None


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
        # An EV charger job has no panels. Its model number sits where the
        # panel rating goes ("111" for an ARMORVOLT 11kW), so reporting it as
        # panels read as "5x 111W" on the customer hover. Name the package
        # instead, and leave the panel fields empty.
        is_ev = build_commission_pack._is_ev_package(getattr(inv, "package_description", ""))
        entry = {
            "invoice": str(getattr(inv, "invoice_number", "") or ""),
            "invoice_date": str(getattr(inv, "invoice_date", "") or ""),
            "package": (build_commission_pack.ev_package_label(
                getattr(inv, "package_description", "")) if is_ev else ""),
            "panel_qty": None if is_ev else (int(qty) if qty is not None else None),
            "panel_rating": None if is_ev else (int(rating) if rating is not None else None),
            "brand": "" if is_ev else _panel_brand(getattr(inv, "package_description", "")),
            "phase": "" if is_ev else _phase_label(getattr(inv, "phase_type", None)),
            "inverter": "" if is_ev else _inverter_label(getattr(inv, "package_name", None),
                                                         getattr(inv, "package_description", "")),
            # The 75% milestone is NOT on the table row: the column headed
            # "75% Payment Date" is filled from the invoice's full_payment_date
            # (the 100% date), so a post-July invoice that reached 75% but not
            # 100% reads "pending" there. Carried here so the Basic Commission
            # hover can name the date each tranche actually became payable.
            "pct75_date": str(getattr(inv, "pct75_date", "") or ""),
            "full_payment_date": str(getattr(inv, "full_payment_date", "") or ""),
            # The package line's own price. The table's System Price column
            # shows the invoice Total Amount instead, so the Special Case NFP
            # Baseline Rate reads this to spot a Cash Price above the package
            # price (the 100% rate).
            "package_price": float(getattr(inv, "system_price", 0) or 0),
            # Total Amount, EPP and the add-on / lucky draw lines behind the
            # Original Price and Cash Price: the price cell hover shows the
            # formula with this invoice's own figures (user, 2026-10-23).
            "price_basis": _invoice_price_breakdown(inv),
        }
        if (not entry["package"] and entry["panel_qty"] is None and entry["panel_rating"] is None
                and not entry["phase"] and not entry["inverter"] and not entry["pct75_date"]
                and not entry["full_payment_date"]):
            continue
        details.setdefault(customer.lower(), []).append(entry)
    for rows in details.values():
        rows.sort(key=lambda e: e.get("invoice_date") or "")
    return details


def _build_special_case_stage(headers: list, basic_lines, nfp_by_inv_all: dict,
                              year: int, month: int, agent_type: str = "internal") -> dict:
    """The "Special Case" Payout Stage: every customer who, as at the end of
    the month being reported, has paid something on their invoice but not yet
    100% -- whenever that first payment was made.

    The month's own rows cannot supply this list. They hold only the invoices
    whose advance or balance milestone lands in that month, so a customer who
    paid a deposit in May and still owes the balance in October is in neither.
    Built here from every Basic line instead, with the commission each invoice
    WOULD pay, laid out in the month table's own columns so the page can show
    it with the same renderer. Nothing on it is payable until an admin ticks
    it: the approval (build_commission_pack.apply_payout_approvals) pays it as
    a balance in this month. An approved customer stays listed here, ticked,
    in its approval month so the tick can be undone; it is listed untickable in
    the months before that and not at all in the months after.

    `customers` maps "agent||customer" (lower case) to that customer's
    invoices and approval, for the tick.
    """
    import calendar
    month_end = f"{int(year):04d}-{int(month):02d}-{calendar.monthrange(int(year), int(month))[1]:02d}"
    approvals = build_commission_pack.payout_approvals(agent_type)
    approval_info = build_commission_pack.payout_approval_info(agent_type)
    advance_approvals = build_commission_pack.payout_advance_approvals(agent_type)
    advance_info = build_commission_pack.payout_approval_info(agent_type, "advance")
    this_month = (int(year), int(month))
    # An advance approved early is the invoice's advance from its month on, so
    # the lines are read with those re-dated (full approvals are not applied:
    # this list shows what a full approval WOULD pay).
    basic_lines = build_commission_pack.apply_payout_approvals(
        basic_lines, [], {}, year, agent_type, approvals={})[0]

    # One line per invoice and agent. Safwan's own block is dropped from the
    # month table (remove_agent_block), so it is dropped here too.
    candidates: dict = {}
    for ln in basic_lines or []:
        inv = str(getattr(ln, "invoice_number", "") or "").strip()
        agent = str(getattr(ln, "agent_name", "") or "").strip()
        first_pay = str(getattr(ln, "first_payment_date", "") or "")[:10]
        if not inv or not agent or "safwan" in agent.lower():
            continue
        if not first_pay or first_pay > month_end:
            continue
        approved_for = approvals.get(inv)
        if approved_for is not None and approved_for < this_month:
            continue      # already paid out, in an earlier month
        candidates.setdefault((inv, agent.lower()), ln)
    if not candidates:
        return {"headers": list(headers), "rows": [], "customers": {}}

    # What each invoice had been paid by the month's end, not today: the line's
    # paid_amount is the current figure, which would hide a customer who paid
    # in full after the month closed. Same payment exclusions as the milestone
    # SQL. Several versions of one invoice number can exist, so payments are
    # summed per version and the version matching the line's total is used.
    # The invoice total comes from this query too: outsource lines are
    # SimpleNamespaces that carry no total_amount.
    try:
        from basic_commission_rates import MILESTONE_PAYMENT_EXCLUSIONS as _excl
    except Exception:
        _excl = "101334, 104412, 101333, 104413, 4899"
    invs = sorted({inv for inv, _ in candidates})
    inv_list = ", ".join("'" + inv.replace("'", "''") + "'" for inv in invs)
    try:
        pay_rows = _pg_query(f"""
            SELECT TRIM(i.invoice_number) AS inv, i.bubble_id, i.total_amount,
                   COALESCE(SUM(p.amount), 0) AS paid
            FROM invoice i
            JOIN payment p ON p.linked_invoice = i.bubble_id
            WHERE TRIM(i.invoice_number) IN ({inv_list})
              AND p.id NOT IN ({_excl})
              AND p.payment_date::date <= DATE '{month_end}'
            GROUP BY TRIM(i.invoice_number), i.bubble_id, i.total_amount
        """)
    except Exception as e:
        _log(f"[SPECIAL CASE STAGE] could not read payments: {e}")
        return {"headers": list(headers), "rows": [], "customers": {},
                "error": "Payments could not be read."}
    paid_by_inv: dict = {}
    for r in pay_rows or []:
        paid_by_inv.setdefault(str(r.get("inv") or ""), []).append(
            (float(r.get("total_amount") or 0), float(r.get("paid") or 0)))

    def idx(*names) -> int:
        for name in names:
            for i, h in enumerate(headers):
                if str(h).lower().strip() == name.lower():
                    return i
        return -1

    cols = {
        "agent": idx("Agent"), "customer": idx("Customer"),
        "inv_date": idx("Invoice Date"), "first_pay": idx("1st Payment Date"),
        "rm300": idx("Basic Commission (RM300)"),
        "milestone": idx("75% Payment Date", "Full Payment Date"),
        "pkg": idx("Property Type", "Package Type"), "system": idx("System Price"),
        "nfp": idx("Net Floor Price"), "sales": idx("Sales Price"),
        "comm": idx("Commission"), "price": idx("Commission Price"),
        "rate": idx("Basic Rate %"),
        "override": idx("OVERRIDE"), "safwan": idx("Safwan (RM)"),
        "gan": idx("Gan Lai Soon", "Gan Lai Soon (RM)"),
        "ref_name": idx("Referral Name"), "ref_fee": idx("Referral Fee"),
        "adv_deducted": idx("Advance Deducted"), "adv_date": idx("Advance Payment Date"),
    }

    def rm(value) -> str:
        v = float(value or 0)
        return f"RM {v:,.2f}" if v else "-"

    # Overrides and referral fees are recognised with the balance, so they are
    # what approving would pay on top of the agent's own commission. Worked
    # out by the same rules the month rows use (build_*_summary_tables):
    # Data-page overrides up the Reports To chain, an outsource factory
    # invoice's Safwan / OUM split and Gan Lai Soon's OGM cut, and the
    # referral named on the invoice or typed on the dashboard.
    import basic_commission_rates as _bcr
    is_out = str(agent_type).lower() == "outsource"
    ovr_names = _bcr.override_name_map(
        [build_commission_pack.to_title_case(ln.agent_name) for ln in basic_lines or []])
    ref_overrides = build_commission_pack.referral_overrides(year)
    basic_mod = None if is_out else build_commission_pack._load_module(
        "int_basic_commission",
        REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py")
    # Safwan's own commission on an internal factory invoice, by invoice.
    safwan_by_inv: dict = {}
    for ln in basic_lines or []:
        if str(getattr(ln, "agent_name", "") or "").strip().lower() == "safwan":
            full = getattr(ln, "full_commission", None)
            inv = str(ln.invoice_number or "").strip()
            safwan_by_inv[inv] = safwan_by_inv.get(inv, 0.0) + float(
                full if full is not None else ln.basic_commission or 0)

    def line_overrides(ln) -> dict:
        out: dict = {}
        if is_out and (getattr(ln, "is_factory", False) or getattr(ln, "package", "") == "Factory"):
            safwan_cut = float(getattr(ln, "safwan_override", 0) or 0)
            if safwan_cut:
                out["Safwan"] = out.get("Safwan", 0.0) + safwan_cut
            oum_cut = float(getattr(ln, "senior_override", 0) or 0)
            oum_name = getattr(ln, "senior_override_name", None)
            if oum_name and oum_cut:
                out[oum_name] = out.get(oum_name, 0.0) + oum_cut
        else:
            for name, amount in build_commission_pack._basic_line_overrides(
                    ln, "Outsource" if is_out else "Internal", ovr_names):
                out[name] = out.get(name, 0.0) + float(amount)
        return out

    def line_referral(ln, agent_title: str, customer_title: str) -> tuple:
        hit = ref_overrides.get((agent_title.strip().lower(), customer_title.strip().lower()))
        if is_out:
            if not (hit and hit["name"]):
                return "-", 0.0
            rate = float(hit["rate"]) if hit["rate"] is not None else 0.02
            return build_commission_pack.to_title_case(hit["name"]), float(ln.sales_price or 0) * rate
        name = hit["name"] if hit and hit["name"] else getattr(ln, "referral_name", None)
        if not basic_mod._is_valid_referral(name):
            return "-", 0.0
        rate = float(hit["rate"] if hit and hit["rate"] is not None
                     else basic_mod._referral_rate(ln.invoice_date))
        return build_commission_pack.to_title_case(str(name).strip()), float(ln.sales_price or 0) * rate

    rows = []
    customers: dict = {}
    last_agent = None
    for (inv, _agent_key), ln in sorted(
            candidates.items(),
            key=lambda kv: (str(kv[1].agent_name).lower(), str(kv[1].customer_name).lower(), kv[0][0])):
        line_total = float(getattr(ln, "total_amount", 0) or 0)
        versions = paid_by_inv.get(inv) or []
        match = [v for v in versions if line_total and abs(v[0] - line_total) < 0.01]
        if not (match or versions):
            continue
        total, paid = max(match or versions, key=lambda v: v[1])
        if total <= 0 or paid <= 0 or paid >= total - 0.005:
            continue
        nfp_row = (nfp_by_inv_all or {}).get(inv)
        agent_title = build_commission_pack.to_title_case(ln.agent_name)
        customer_title = build_commission_pack.to_title_case(ln.customer_name)
        entry = customers.setdefault(f"{agent_title.lower()}||{customer_title.lower()}", {
            "agent": agent_title, "customer": customer_title, "agent_type": agent_type,
            "invoices": [], "items": [], "approved": False, "approved_later": "",
            "approved_by": "", "approved_at": "",
            # Advance-only approvals, and which invoices may still take one.
            "advance_approved": False, "advance_approved_later": "",
            "advance_approved_by": "", "advance_approved_at": "",
            "advance_invoices": [],
        })
        entry["invoices"].append(inv)
        approved_for = approvals.get(inv)
        if approved_for is not None:
            info = approval_info.get(inv) or {}
            entry["approved_by"] = info.get("by", "")
            entry["approved_at"] = info.get("at", "")
        if approved_for == this_month:
            entry["approved"] = True
        elif approved_for is not None:
            entry["approved_later"] = f"{calendar.month_name[approved_for[1]]} {approved_for[0]}"
        # What approving would pay this month, worked out the way
        # apply_payout_approvals() pays it: the whole Basic Commission less an
        # advance already paid in an earlier month, plus the NFP in full.
        full = getattr(ln, "full_commission", None)
        basic_full = float(full if full is not None else ln.basic_commission or 0)
        # The advance counts if it was reached by the month's end: paid in an
        # earlier month, or paid in this month's Advance run. Either way the
        # approval only adds what is left (Rabihah, Sep 2026: RM 844.00).
        multi = build_commission_pack._is_new_policy_line(ln)
        advance_on = build_commission_pack.advance_recognized_date(ln)
        advance = 0.0
        if multi and advance_on and advance_on <= month_end:
            advance = min(basic_full, build_commission_pack._line_advance_amount(ln))
        # Advance approved on its own: in this month (can be withdrawn here),
        # or in a later one. An earlier one already shows as `advance` above.
        adv_for = advance_approvals.get(inv)
        if adv_for is not None and adv_for >= this_month:
            ainfo = advance_info.get(inv) or {}
            entry["advance_approved_by"] = ainfo.get("by", "")
            entry["advance_approved_at"] = ainfo.get("at", "")
            if adv_for == this_month:
                entry["advance_approved"] = True
            else:
                entry["advance_approved_later"] = (
                    f"{calendar.month_name[adv_for[1]]} {adv_for[0]}")
        # The choice between the advance alone and the full Basic Commission:
        # a two-stage invoice whose deposit is still below the advance trigger
        # at the month's end, with nothing approved for it yet.
        advance_option = 0.0
        if multi and not advance and adv_for is None and approved_for is None:
            advance_option = min(basic_full, build_commission_pack._line_advance_amount(ln))
            entry["advance_invoices"].append(inv)
        nfp_amt =float(getattr(nfp_row, "nfp_commission", 0) or 0) if nfp_row else 0.0
        # Paid to other people with the balance: overrides up the chain (plus
        # Gan Lai Soon's OGM cut on outsource, Safwan's own commission on an
        # internal factory invoice) and the referral fee. Each counted once --
        # an outsource factory row repeats Safwan's cut in its own column.
        overrides = line_overrides(ln)
        if is_out:
            gls = float(getattr(ln, "gan_lai_soon", 0) or 0)
            if gls:
                overrides["Gan Lai Soon"] = overrides.get("Gan Lai Soon", 0.0) + gls
        elif safwan_by_inv.get(inv):
            overrides["Safwan"] = overrides.get("Safwan", 0.0) + safwan_by_inv[inv]
        ref_name, ref_fee = line_referral(ln, agent_title, customer_title)
        entry["items"].append({
            "invoice": inv, "paid": round(paid, 2), "total": round(total, 2),
            "paid_pct": _pct_text(round(paid / total * 100, 2)),
            "basic": round(basic_full, 2), "advance": round(advance, 2),
            "advance_on": advance_on if advance else "",
            "nfp": round(nfp_amt, 2), "payable": round(basic_full - advance + nfp_amt, 2),
            "overrides": [{"name": n, "amount": round(a, 2)}
                          for n, a in sorted(overrides.items()) if round(a, 2)],
            "referral_name": ref_name if ref_fee else "",
            "referral_fee": round(ref_fee, 2),
            # What "Advance RM300 only" would pay; 0 where it is not offered.
            "advance_option": round(advance_option, 2),
            # The advance shown was released by an advance-only approval (not
            # the real one, which wins when it came first).
            "advance_early": bool(advance) and getattr(ln, "_advance_approved", None) is not None,
        })
        for kind in ("Basic Commission", "Net Floor Price Commission"):
            row = ["-"] * len(headers)

            def put(key, value):
                if cols[key] != -1:
                    row[cols[key]] = value

            # Shaped like the month's own rows: the agent only on the first row
            # of their block, the customer on both of its rows. The page groups
            # a customer's rows by that cell, so a blank one on the NFP row
            # split it from its Basic row and threw every merged cell out.
            put("agent", agent_title if agent_title != last_agent else "")
            last_agent = agent_title
            put("customer", customer_title)
            if kind == "Basic Commission":
                # The whole commission, not the advance-only cap an internal
                # line carries between its two milestones (full_commission).
                # The balance approving would pay: the advance comes off, and
                # the hover shows it ("Original Price x Rate % - 300.00").
                put("price", rm(basic_full - advance))
                put("rate", build_commission_pack._basic_line_rate_label(ln) or "-")
                put("adv_deducted", f"{advance:,.2f}" if advance else "-")
                # Payment at for the Advance stage: the date the payment that
                # reached the trigger carries (Created at comes from the policy).
                put("adv_date", str(getattr(ln, "pct5_date", "") or "")[:10] if advance else "-")
                put("rm300", "invoice before july" if not multi
                    else ("-" if advance else "pending"))
                # Override, Safwan, Gan Lai Soon and referral sit on the Basic
                # row only, as on the month rows.
                put("override", build_commission_pack.format_other_commission_for_customer(
                    line_overrides(ln)))
                if not is_out:
                    put("safwan", rm(safwan_by_inv.get(inv, 0.0)))
                else:
                    # An outsource row shows Safwan's factory cut in its own
                    # column as well as under OVERRIDE, as the month rows do.
                    put("safwan", rm(getattr(ln, "safwan_override", 0) or 0))
                    put("gan", rm(getattr(ln, "gan_lai_soon", 0) or 0))
                put("ref_name", ref_name)
                put("ref_fee", rm(ref_fee))
            else:
                put("price", rm(getattr(nfp_row, "nfp_commission", 0) if nfp_row else 0))
            put("inv_date", str(ln.invoice_date or "")[:10] or "-")
            put("first_pay", str(ln.first_payment_date or "")[:10] or "-")
            if kind != "Basic Commission":
                put("rm300", "-")
            put("milestone", "pending")
            put("pkg", str(getattr(ln, "package", "") or "-"))
            put("system", rm(total))
            put("nfp", rm(getattr(nfp_row, "net_floor_price", 0) if nfp_row else 0))
            put("sales", rm(ln.sales_price))
            put("comm", kind)
            rows.append(row)
    return {"headers": list(headers), "rows": rows, "customers": customers}


def _special_case_stage_or_empty(headers, basic_lines, nfp_by_inv_all, year, month,
                                 agent_type) -> dict:
    """_build_special_case_stage, for the months that have a Payout Stage
    filter (July 2026 on). A failure empties the stage rather than the page."""
    if int(month) < 7:
        return {"headers": list(headers), "rows": [], "customers": {}}
    try:
        return _build_special_case_stage(headers, basic_lines, nfp_by_inv_all, year, month,
                                         agent_type)
    except Exception:
        _log("[SPECIAL CASE STAGE ERROR]\n" + traceback.format_exc())
        return {"headers": list(headers), "rows": [], "customers": {},
                "error": "Special Case list could not be built."}


def _refresh_system_details(payload: dict, year: int, agent_type: str) -> None:
    """Rebuild the customer-hover map on a cached month payload.

    EV package wording lives in this map. Serving last month's cached body
    after a label change would otherwise keep the old short phrase
    (e.g. dropping "with standard installation") until a full rebuild.
    """
    if not isinstance(payload, dict):
        return
    cached = get_cached_data(year, agent_type)
    if not cached or "nfp" not in cached:
        return
    nfp_tuple = cached.get("nfp") or ()
    nfp_by_inv_all = nfp_tuple[-1] if nfp_tuple else {}
    basic = (payload.get("sections") or {}).get("basic_nfp")
    if isinstance(basic, dict):
        basic["system_details"] = _build_system_details(nfp_by_inv_all)


def _agent_roles_for_month(year: int, month: int) -> dict[str, dict]:
    """{normalised agent name: {"role", "agent_type"}} as at this month.

    One row per agent wins -- the latest effective_from among the rows whose
    range actually covers the month, which is the same "latest row wins" rule
    basic_commission_rates uses to price them. An agent holding a Solar and an
    EV role at once is shown by the Solar one, the role a package-blind lookup
    (NFP, Reports To) reads there too. Keyed on the same normalised
    form agent_names.resolve() uses so the browser can match a displayed name
    (which may be a nickname or full name) back to its role.
    """
    try:
        rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.append(rates_dir)
        from basic_commission_rates import split_effective_range, role_package, PACKAGE_SOLAR
        target = f"{year}-{month:02d}"
        ic_map = agent_names.get_ic_map()

        best: dict[str, tuple[tuple, dict]] = {}
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
            # Same start, different ends: the one ending first wins while both
            # cover the month -- basic_commission_rates._covering_role_rows().
            # A Solar role of "All" only holds role-independent rates (Factory
            # 2%), so any other row covering the month is shown before it.
            is_solar = role_package(r.get("package_type")) == PACKAGE_SOLAR
            rate_holder = is_solar and str(r.get("hierarchy") or "").strip().lower() == "all"
            rank = (not rate_holder, is_solar, start)
            prev = best.get(key)
            if prev is None or rank > prev[0][:3] or (rank == prev[0][:3] and end < prev[0][3]):
                ic_no = (
                    ic_map.get(key)
                    or ic_map.get(agent_names.normalize_key(agent))
                    or str(r.get("ic_no") or "").strip()
                )
                best[key] = (rank + (end,), {
                    "agent": display,
                    "role": str(r.get("hierarchy") or "").strip(),
                    "agent_type": str(r.get("agent_type") or "").strip(),
                    "ic_no": ic_no,
                })
        return {k: v[1] for k, v in best.items()}
    except Exception:
        # A tinting aid must never take the commission table down with it.
        _log("[AGENT ROLES FOR MONTH]\n" + traceback.format_exc())
        return {}


def _agent_role_history() -> dict[str, list[dict]]:
    """{normalised agent name: [{"role", "agent_type", "package", "start", "end"}, ...]},
    every effective range on file, latest start first. `package` is "Solar" or
    "EV" -- an agent can hold one of each over the same months.

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
        from basic_commission_rates import split_effective_range, role_package

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
                "package": role_package(r.get("package_type")),
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

# A case's month is its first payment's paid date. From this date a first
# payment keyed in (Created at) in a later month than it was paid is a late
# entry: it still counts on the Sales Report, but earns no EGA/ESA, Early Bird
# or Monthly Contest points (user, 28 Oct 2026). See _sales_report_case_dates().
SALES_REPORT_CREATED_AT_FROM = "2026-08-01"
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


def _sales_report_cancelled(row: dict) -> bool:
    """A cancelled package is recorded by renaming the customer, e.g.
    "GARY TAN (CANCEL PACKAGE REFUND ON 13/06/2026 RM934.79)"; the invoice
    itself stays live. Such a case is not a sale. Nor is an invoice raised
    for a "Sample Quotation" customer (Wong Jia Xuan, Aug 2026): left off
    per user request (28 Oct 2026)."""
    name = " ".join(str(row.get("customer_name") or "").lower().split())
    return "cancel" in name or "sample quotation" in name


# Invoices left out of the report by hand: duplicates and cancellations the
# data does not flag.
# Keyed on bubble_id: an invoice number can be shared (1008400 is two cases).
SALES_REPORT_EXCLUDED_INVOICES = {
    # 1008358, BURHANUDDIN HALMI BIN HASIN (DUPLICATE) -- replaced by
    # INV-1010069, which carries the same RM1,500 deposit of 4 Jan 2026.
    "1767605082432x400928611535683600",
    # INV-1008203, "Tuan Amirul Ref" -- placeholder for NOOR HAMIRUL ANUAR BIN
    # NORDIN (1008537); same RM1,083 deposit of 31 Jan 2026.
    "0f2a1442-b976-4c50-9847-655a1b088156",
    # INV-1008594, NG KOK CHEU (ATAP), Chan Jia Wei -- a RM4,800 top-up on the
    # February sale INV-1008241 (RM46,106), not a case of its own.
    "48c00c12-fd1a-4bb1-a869-ae59748e9d40",
    # INV-1008593, CHIA KIM HENG (ATAP), Chan Jia Wei -- RM19,800, fully paid,
    # beside INV-1008805 (RM33,188, RM1,659.40 deposit) for the same customer.
    # Counted once, on INV-1008805, so March RES is 8 cases, RM201,220.15, per
    # user request (28 Oct 2026).
    "20625b3d-8ce9-461d-af1f-fd97b2821c7c",
    # 1008435, Cheong Hong Wee (Atap), Teng Kah Kent -- RM29,800, first paid
    # 16 Jan 2026; not a January case per Finance, left out.
    "1768594400823x535666973979967500",
    # INV-1011043, LEE WAI LEONG (TOP UP), Tan Jia Hao -- a RM9,800 top-up on
    # an earlier sale, not a case of its own.
    "1a04c360-72b1-4909-b853-e29a7c9badd0",
    # INV-1011046 (Ng Yih Chyn), Denise Ng -- top-up excluded per user request;
    # INV-1011801 (TAN CHEN WEI) remains as the 1 September case.
    "dd84422a-ff1d-4e8e-94fa-2c0a3dff213d",
    # INV-1010795, Gursimran Woon Shu Yuan, Woon Jia Keat -- a RM2,278 EV
    # charger with no solar; not a July case per Finance. Other EV-only
    # invoices still count.
    "d3232cec-61c8-4c7a-b669-5c2e825bc8cc",
    # INV-1011484, NG CHOON FONG (ATAP), Olivier Koh Cong Lee -- RM21,000,
    # invoiced 31 Aug (23:08 local), first paid 2 Sep. Left off the report per
    # user request so September RES is 8 cases, RM219,500.00; see the +RM300 on
    # INV-1011861 in SALES_REPORT_SALES_ADJUSTMENTS.
    "6e44e83e-57b4-42bc-8fc9-6f4d7856d163",
    # INV-1008170, CHO CHOOI PENG, Sin Wei Fong -- the original RM54,765 quote
    # (36 panels, RM2,100 deposit of 30 Jan, nothing else paid), replaced by
    # 1008534, the same package at RM42,000 after discounts with the same
    # deposit, fully paid. Counted once, at RM42,000, per user request.
    "7b92371e-a931-4777-9298-ae53c00ca541",
    # Carol Siow -- left off the report per user request so July has no COM
    # line (her ESA / Oct early-bird qualification falls with the S.O.S Mewah
    # EP; the September COM cases are in, see SALES_REPORT_PROPERTY_OVERRIDES):
    # INV-1010817, TOWA HARDWARE (M) SDN BHD (FACTORY), RM2,908 -- a cable add-on,
    # paid in full 30 Jul.
    "d83f837c-6586-43c7-9303-6107aae95645",
    # INV-1010885, YAP CHOON FAH (TOP UP), Ling Liang Kang -- top-up excluded per user request.
    "84a07187-36ec-4bea-9440-e5587570827c",
    "INV-1010885",
    # INV-1011801, TAN CHEN WEI (ATAP), Denise Ng Pei Sing, is NOT excluded: it
    # was briefly listed here (Denise 0 September cases), then restored on
    # 12 Oct 2026 per user request as her September case -- first paid
    # RM500 on 29 Sep. See the INV-1011046 note above.
}

# Hand corrections to an invoice's sales value on the report, keyed on
# bubble_id like the exclusions above. Added to the sales price, so ANS and
# EP both follow.
SALES_REPORT_SALES_ADJUSTMENTS = {
    # INV-1010386, FOO MUN YOKE (ATAP), Joshua Yap Jia Hao -- EPP interest of
    # RM1,423.32 added back so June 3 RES sales are counted at RM91,840.00.
    "f79f732f-6ac2-466c-82fe-bf38438fdbc8": "1423.3183856502242",
    # INV-1011236, Wong Chee Meng, Woon Jia Keat -- adjusted by +RM1,060 so
    # August 4 RES sales are counted at RM100,630.00 per user request.
    "1090ce85-4f2b-4166-bc23-38fabbe79321": "1060",
    # INV-1008800, Chai Shang Tat, Tan Jia Hao -- adjusted by +RM500 so March
    # 1 RES sale is counted at RM21,000.00 per user request.
    "97e74ffe-8ba0-48c7-b739-5de2f28c09e9": "500",
    # INV-1008377, BONG KUET TSIN (ATAP), Tan Jia Hao -- RM20,500 less the
    # RM500 "SAJ M2-1.0K S2 Micro Inverter (RM1,500 -> RM1,000)" add-on.
    "df7d36c4-a0e0-4ebe-8b40-55c037dcbcd7": "-500",
    # INV-1009209, JC PRINT & OFFICE SDN. BHD. (FACTORY), Ching Zhe Hang --
    # RM45,000 less the RM1,000 + RM500 SAJ micro inverter add-ons.
    "2ebb5452-8b6a-4260-9cb2-0c2dcf98c5e8": "-1500",
    # INV-1010036, LIM KIM HOI (ATAP), Chan Jia Wei -- counted at the full
    # RM19,627 invoice total; the RM627 EPP interest is added back.
    "557b3b07-9827-46cb-bac3-46d867f0f73f": "627",
    # INV-1010037, TAN BEE LING (ATAP), Chan Jia Wei -- counted at the full
    # RM25,290.90 invoice total; the RM2,490.90 EPP interest is added back.
    "f96da89a-35f7-4bf3-bdf5-e4c71c280235": "2490.90",
    # INV-1011142, TAM PUI TENG (ATAP), Chan Jia Wei -- counted at RM37,750,
    # before the RM6,670 "Discount (RM 6670)" line (RM31,080 after it).
    "4809d0b2-abf7-4841-952c-cfde02f8adbb": "6670",
    # INV-1011869, TAN KIM CHAI (ATAP), Chan Jia Wei -- counted at RM41,929,
    # before the RM8,929 "Discount (RM 8929)" line (RM33,000 after it).
    "8f449830-8d6c-4ad6-9cc9-7ae4db432f27": "8929",
    # INV-1009303, CHEW SWEE ANNE (ATAP), Ching Zhe Hang -- counted at the full
    # RM20,043 invoice total; the RM1,179 EPP interest is added back.
    "211630d1-c2aa-4840-9d75-8a11149bfb8c": "1179",
    # INV-1010357, KOH CHIN TIN (ATAP), Louis Ng -- counted at the full
    # RM30,102.57 invoice total; the RM1,102.57 EPP interest is added back.
    "7816fdb7-b094-4766-aa3e-ee0fa0d3cb08": "1102.57",
    # INV-1010700, SEAH AH MOI (ATAP), Louis Ng -- RM28,400 less the RM1,500 add-on.
    "a69cd8e6-e1a2-46fd-999d-203c2ac4e327": "-1500",
    # INV-1010937, CHAT CHEH MAN (ATAP), Sunny Tan -- counted at the full
    # RM40,825 invoice total; the RM825 EPP interest is added back.
    "45e99d46-ced1-4574-8d41-1f0485d0d41d": "825",
    # INV-1010591, LEE IK SOON (ATAP), Zulkarnain -- RM31,750 less the RM1,500 add-on.
    "592b67cd-04a6-4d4d-b8b8-9387471959ef": "-1500",
    # INV-1010718, OOI TZU WEI (ATAP), Denise Ng -- counted at the full
    # RM27,905.50 invoice total; the RM1,105.50 EPP interest is added back.
    "ac31a283-dedf-4d3f-a79a-c32247150d48": "1105.50",
    # INV-1010998, WILLIAM TAN TECK SWEE (ATAP), Vincent Tan -- counted at the
    # full RM33,990 invoice total; the RM2,990 EPP interest is added back.
    "62937f12-2cf1-4884-979d-92bf15a38fbb": "2990",
    # INV-1011408, SIN CHIAN YING (ATAP), Vincent Tan -- RM36,088 less the
    # RM2,588 "Add On 1X EV wallbox 11kw + Installation" line.
    "ec8c8e7b-c783-4aae-ab10-ef3dc463e0a8": "-2588",
    # INV-1010637, GAN ENG YI (ATAP), Wong Jia Xuan -- counted at RM27,280,
    # before the RM580 CEO discount in its "Discount (RM 6680) CEO Discount
    # (RM 580)" line (RM26,700 after it).
    "195e123e-2ce3-4f3c-8acb-4be579994f9e": "580",
    # INV-1010993, MOO MEI JEN (ATAP), Lim Chin Seng (JS) -- counted at the full
    # RM60,210 invoice total; the RM3,105 EPP interest is added back so July RES
    # sales are counted at 2 cases, RM85,710.00 per user request.
    "3b8e6989-1e60-41af-82aa-f69ab470273f": "3105",
    "INV-1010993": "3105",
    # Olivier Koh Cong Lee, January: the four RES cases (INV-1008384 SIBLING
    # KITCHEN RM40,800, INV-1008437 MARIANNE CHEW RM24,000, INV-1008494 NG JONG
    # SERN RM29,368, INV-1008274 TEE SIAN SAN RM24,995.02) sum to RM119,163.02;
    # adjusted by -RM1,170 so January RES sales are counted at RM117,993.02 per
    # user request. No single invoice line is RM1,170, so it is carried on
    # INV-1008274 (RES EP is 1 pt per RM1, so EP follows whichever it sits on).
    "1766909226924x213014302931288060": "-1170",
    # INV-1008474, TON JYUE SHII (ATAP), Olivier Koh Cong Lee -- counted at the
    # full RM21,397.50 invoice total; the RM1,597.50 EPP interest (RM1,170 +
    # RM427.50) is added back so March RES sales are counted at 3 cases,
    # RM76,397.50 per user request.
    "79dc8f2d-d066-47fb-ac0d-e5447a97278f": "1597.5",
    # Olivier Koh Cong Lee, May: the five RES cases (INV-1009168 LEE LIANG KAI
    # RM23,200, INV-1009213 T&T KOTA MASAI RM25,900, INV-1009410 ONG KANG YEE
    # RM28,600, INV-1009520 QUEK POH HUI RM30,500, INV-1009606 TEE SHY LEE
    # RM25,900) sum to RM134,100; adjusted by -RM6,900 so May RES sales are
    # counted at RM127,200.00 per user request. No invoice line or pair of lines
    # is RM6,900, so it is carried on INV-1009213 (RES EP is 1 pt per RM1, so EP
    # follows whichever it sits on).
    "76d396e5-ecdc-448f-83a1-b6a72369e507": "-6900",
    # INV-1010467, CHAN ZHEN YUAN (ATAP), Olivier Koh Cong Lee -- counted at
    # RM45,862.78 so June RES sales are counted at 3 cases, RM81,862.78 per user
    # request. The RM3,562.78 EPP interest (RM1,500 + RM2,062.78) is added back,
    # less RM1,500: the ERP cut the invoice's discount line by RM1,500 on 20 Oct
    # 2026 (total RM45,862.78 -> RM47,362.78), and the user kept the 8 Oct figure.
    "a33a9a02-6797-4824-9662-553407147a4c": "2062.78",
    # Olivier Koh Cong Lee, August: with the two Jin Seng invoices on RES (see
    # SALES_REPORT_PROPERTY_OVERRIDES) the six RES cases (LEE ENG CHAI RM20,800,
    # LEE MENG LIANG RM28,800, OOI YI WEI RM36,000, WENG KAR CHON RM26,000,
    # JIN SENG 20PCS RM30,000, JIN SENG NO 33 RM33,000) sum to RM174,600;
    # adjusted by +RM760 so August RES sales are counted at RM175,360.00 per
    # user request. No invoice line is RM760, so it is carried on INV-1011399
    # (RES EP is 1 pt per RM1, so EP follows whichever it sits on).
    "5c0eee3d-3aa5-4acb-8156-701f544dcd8e": "760",
    # Olivier Koh Cong Lee, September: without INV-1011484 (NG CHOON FONG, see
    # SALES_REPORT_EXCLUDED_INVOICES) the eight RES cases sum to RM219,200;
    # adjusted by +RM300 so September RES sales are counted at RM219,500.00 per
    # user request. No invoice line is RM300, so it is carried on INV-1011861
    # (TAN YUAN TAT; RES EP is 1 pt per RM1, so EP follows whichever it sits on).
    "cf420a9e-60b9-4e86-9ff7-e4a85fbeee38": "300",
    # INV-1010463, TOR SENG CHO (ATAP), Dean Wai Leong Yee -- counted at RM26,620,
    # before the RM620 "CEO Discount" line (RM26,000 after it), so June RES sales
    # are counted at 4 cases, RM102,840.00 per user request.
    "2a988208-a156-41c1-adef-f0875914255f": "620",
    # INV-1011002, TAN CHIN ING (ATAP), Wilson Tan Wei Sheng -- counted at the
    # full RM34,150 invoice total; the RM1,150 EPP interest is added back so July
    # RES sales are counted at 1 case, RM34,150.00 per user request.
    "d40a1c5a-5e32-49b4-b262-6e5b93b0e6f6": "1150",
    # INV-1008398, THONG PEIK CHOO (ATAP), Caryn Dong Leong Moi -- the RM688
    # "KLUANG OPENING 16/5/2026 LUCKY DRAW RM688" prize line is added back (the
    # RM118 guarantee is already restored by _sales_report_line_adjustments), so
    # the case counts at RM27,800 and February RES sales are 3 cases, RM78,600.00
    # per user request. Only this invoice; other lucky-draw prizes stay deducted.
    "663a1b59-7e1f-4094-95c6-b92ac64652a5": "688",
    # INV-1010912, TAN KONG HAO (ATAP), Caryn Dong Leong Moi -- counted at the
    # full RM34,669.60 invoice total; the RM1,869.60 EPP interest (RM31,160 x 6%)
    # is added back so July RES sales are counted at 2 cases, RM57,929.60 per
    # user request.
    "d4998ba3-c577-446a-9524-42c49a626bcd": "1869.6",
    # INV-1008774, TEE LI LEONG (ATAP), Chan Wing On -- fully paid, with the
    # RM118 "Kluang Opening Day (16/05/2026) Special Discount RM118" line. That
    # wording is not "lucky draw", so _sales_report_line_adjustments does not
    # restore it; added back here so March RES sales are counted at 4 cases,
    # RM94,400.00 per user request.
    "198457ae-43c7-493c-9c14-da248624f52d": "118",
    # INV-1009889, LOW BOON HUAT, Ivan Low Chin Chai -- counted at RM24,153,
    # before the RM653 CEO discount in its "Discount (RM 1567) | CEO Discount
    # (RM 653)" line (RM23,500 after it), so May RES sales are counted at 2
    # cases, RM47,913.00 per user request.
    "efc30b6f-ee81-4550-8dd0-5b9688a4b9d4": "653",
    # INV-1010797, HO KHEE FATT, Phil Moo Wui Kead -- counted at RM43,960, before
    # the RM5,960 CEO discount in its "Discount (RM 12030) | CEO Discount
    # (RM5960)" line (RM38,000 after it), so September RES sales are counted at 1
    # case, RM43,960.00 per user request. August's Lay Chee Wan (INV-1011438)
    # stays at its invoice value, RM35,900.00.
    "136aa3a5-0f57-4b93-85f1-49d55bf8482c": "5960",
    # INV-1009792, GAN TAI SHENG (573-L)(SHOP LOT) (ATAP), and INV-1009919, GAN
    # TAI SHENG (NO 60) (ATAP), See Cheak Ching -- counted at the full invoice
    # totals, RM19,026 and RM17,969; the RM1,026 and RM969 EPP interest is added
    # back so May RES sales are counted at 2 cases, RM36,995.00 per user request.
    "335842d7-bf44-4061-b368-eaeb753f96e1": "1026",
    "4dac557b-ae9a-4686-8d11-c2ecd781a283": "969",
    # INV-1010818, LEW CHEE KHAN (ATAP), See Cheak Ching -- counted at RM27,095,
    # before the RM2,095 CEO discount in its "SPECIAL DISCOUNT LEW CHEE KHAN (RM
    # 5880) | CEO Discount (RM 2095)" line (RM25,000 after it), so July RES sales
    # are counted at 1 case, RM27,095.00 per user request.
    "fb0a08e0-7079-4c96-8d4e-1e84c42109e8": "2095",
    # INV-1010679, GUI LEE LAI (ATAP), Viggy Gui Choon Fong -- counted at the
    # full RM21,364 invoice total; the RM1,363.64 EPP interest (RM13,636.36 x
    # 10%) is added back so July RES sales are counted at 3 cases, RM71,764.00
    # per user request. The other two July cases stay net of their EPP interest.
    "27c33d44-24ba-40c1-bc9a-5d484f26fbe3": "1363.64",
    # INV-1010367, KALA A/P SUKUMAR (ATAP), Carol Siow -- counted at full
    # RM33,742 invoice total; the RM2,942 EPP interest is added back.
    "18f34384-f543-4b61-a033-7eb5193543f8": "2942",
    "INV-1010367": "2942",
    # INV-1010605, CHAN HWA KWONG (ATAP), Carol Siow -- counted at full
    # RM35,497.50 invoice total; the RM1,997.50 EPP interest is added back so
    # June RES sales are counted at 7 cases, RM233,639.50 per user request.
    "1ed55d0a-cf30-4b56-b13b-a4b767d29a72": "1997.50",
    "INV-1010605": "1997.50",
    # 1008563, TAN LIAN KENG, Chan Wing On -- adjusted by +118 so February RES
    # sales are counted at 2 cases, RM57,000.00 per user request.
    "1771658583603x991793478265471000": "118",
    "1008563": "118",
    # INV-1009694, VILASINI A/P K S VASAN (ATAP), Chan Wing On -- adjusted by +1,100 so
    # May RES sales are counted at 4 cases, RM114,412.00 per user request.
    "60d8670f-b6ec-48bb-bfb8-f0d210479452": "1100",
    "INV-1009694": "1100",
    # INV-1010031, ONG LAY MIN (ATAP), Ling Liang Kang -- adjusted by +8,970 so
    # May RES sales are counted at 7 cases, RM192,479.00 per user request.
    "11bb249e-ecdd-4e74-a643-801700d55845": "8970",
    "INV-1010031": "8970",
    # INV-1010448, CHANG YUK LENG (ATAP), Ling Liang Kang -- adjusted by -2,500 so
    # June RES sales are counted at 3 cases, RM69,460.00 per user request.
    "dc17faa8-5000-494b-9a6d-e9c157b3584e": "-2500",
    "INV-1010448": "-2500",
    # INV-1010672, TAN YONG KOK (ATAP), Ling Liang Kang -- adjusted by +5,210 so
    # July RES sales are counted at 7 cases, RM206,070.00 per user request.
    "9851d7df-92b9-4868-9997-dd3734cc70b4": "5210",
    "INV-1010672": "5210",
    # INV-1010972, CHONG AI KI (ATAP), Ling Liang Kang -- +200 restores the
    # "Discount Home Expo Instant Rebate Voucher 09/10/2026" line added in the
    # ERP on 12 Oct 2026, so July RES stays at 7 cases, RM206,070.00 per user request.
    "ce8ae739-0aba-4f50-92cd-a26346c61785": "200",
    "INV-1010972": "200",
    # INV-1009464, T-KHEN LAI AUTO SDN. BHD. (FACTORY) (ATAP), Ling Liang Kang -- adjusted by +8,000 so
    # April COM sales are counted at 1 case, RM81,800.00 per user request.
    "9d02ee37-fb92-44ad-ae06-13132699125a": "8000",
    "INV-1009464": "8000",
    # INV-1010803, LEE TIEK SEANG (TOP UP) (ATAP), Ling Liang Kang -- adjusted by -10,000 so
    # August RES sales are counted at 3 cases, RM55,920.00 per user request.
    "1d49aed9-3532-4915-a843-4883af3b180b": "-10000",
    "INV-1010803": "-10000",
    # INV-1010536, DALINA BINTI ALI (ATAP), Mohd Azhar Bin Ibrahim -- adjusted by -30 so
    # June RES sales are counted at 1 case, RM35,140.00 per user request.
    "a5cee2bd-c8b4-4796-9502-cbb66e4b6c6a": "-30",
    "INV-1010536": "-30",
    # INV-1008421, MOHD RAZIF BIN SENIN @ ATTAN (ATAP), Nur Anisah -- adjusted by +10,000 so
    # January RES sales are counted at 2 cases, RM63,600.00 per user request.
    "e5f87cb6-8917-4eba-91c3-521a37fc111b": "10000",
    "INV-1008421": "10000",
    # INV-1011497, CHIENG JIN SOON (ATAP), Louis Ng Zhan Yi -- adjusted by +700 so
    # September RES sales are counted at 3 cases, RM108,300.00 per user request.
    "0bd6e7f9-dce9-4638-9c2a-1b3ca2b02ff7": "700",
    "INV-1011497": "700",
    # INV-1009559, CHONG MUN KIT, Ching Zhe Hang -- counted at its original
    # RM20,225 (the RM1,000 + RM12,146.25 paid is exactly 65% of it); every line
    # was edited in the ERP on 28 Aug 2026, taking the total to RM31,280. -11,055
    # so May RES sales are counted at 6 cases, RM134,749.91 per user request.
    "3c4f9127-5c31-480f-b9f6-7ee0917b0f76": "-11055",
    "INV-1009559": "-11055",
    # Chan Jia Wei, June: no corrections. Its twelve invoices follow the
    # invoice Total Amount (Commission Price once fully paid), 12 RES,
    # RM310,705.00, per user request (28 Oct 2026).
    # INV-1010346, MOK WEI XIAN, Louis Ng Zhan Yi -- a RM1,200 EV charger
    # installation line added 8 Oct; removed so June RES stays at 10 cases,
    # RM291,620.57 as the user checked it.
    "eed01193-6603-466e-b641-671f0cff58c3": "-1200",
    "INV-1010346": "-1200",
    # INV-1011237, CHEOK GEOK LIAN, Wong Jia Xuan -- package and discount lines
    # edited 12 Oct, the only July change after the user checked July at 3 RES
    # cases, RM83,770.00; +770 restores that figure.
    "afb07b5b-19c0-4520-bed8-36364a6ba087": "770",
    "INV-1011237": "770",
}

# EP for an invoice set by hand, in place of calc_ep_points() (keyed on
# bubble_id). Sales and ANS are untouched; only the points change.
# Hand adjustments in SALES_REPORT_SALES_ADJUSTMENTS that only add an invoice's
# EPP interest back. Since 28 Oct 2026 a fully paid invoice's ANS is its full
# total, EPP included, so on ANS they would count the EPP twice and are skipped
# for fully paid invoices; they still count toward EP, as before.
SALES_REPORT_EPP_ADDBACKS = {
    "18f34384-f543-4b61-a033-7eb5193543f8",  # INV-1010367, KALA A/P SUKUMAR (ATAP) -- RM2,942.00
    "1ed55d0a-cf30-4b56-b13b-a4b767d29a72",  # INV-1010605, CHAN HWA KWONG (ATAP) -- RM1,997.50
    "f96da89a-35f7-4bf3-bdf5-e4c71c280235",  # INV-1010037, TAN BEE LING (ATAP) -- RM2,490.90
    "79dc8f2d-d066-47fb-ac0d-e5447a97278f",  # INV-1008474, TON JYUE SHII (ATAP) -- RM1,597.50
    "7816fdb7-b094-4766-aa3e-ee0fa0d3cb08",  # INV-1010357, KOH CHIN TIN -- RM1,102.57
    "211630d1-c2aa-4840-9d75-8a11149bfb8c",  # INV-1009303, CHEW SWEE ANNE (ATAP) -- RM1,179.00
    "62937f12-2cf1-4884-979d-92bf15a38fbb",  # INV-1010998, WILLIAM TAN TECK SWEE (ATAP) -- RM2,990.00
    "ac31a283-dedf-4d3f-a79a-c32247150d48",  # INV-1010718, OOI TZU WEI (ATAP) -- RM1,105.50
    "d40a1c5a-5e32-49b4-b262-6e5b93b0e6f6",  # INV-1011002, TAN CHIN ING (ATAP) -- RM1,150.00
    "d4998ba3-c577-446a-9524-42c49a626bcd",  # INV-1010912, TAN KONG HAO (ATAP) -- RM1,869.60
    "3b8e6989-1e60-41af-82aa-f69ab470273f",  # INV-1010993, MOO MEI JEN (ATAP) -- RM3,105.00
    "335842d7-bf44-4061-b368-eaeb753f96e1",  # INV-1009792, GAN TAI SHENG (573-L)(SHOP LOT) (ATAP) -- RM1,026.00
    "4dac557b-ae9a-4686-8d11-c2ecd781a283",  # INV-1009919, GAN TAI SHENG (NO 60) (ATAP) -- RM969.00
    "27c33d44-24ba-40c1-bc9a-5d484f26fbe3",  # INV-1010679, GUI LEE LAI (ATAP) -- RM1,363.64
}

# ANS only (never EP or sales), keyed on bubble_id: when the 28 Oct 2026 rules
# (paid date for the month; fully paid at Original Price, else the full total
# with EPP) would move a month the user had already confirmed, the difference
# is pinned here so that month reads as they checked it. Filled from the
# confirmed list; a confirmed month the rules already match needs no pin.
SALES_REPORT_CONFIRMED_ANS_PINS: dict = {
    # Olivier Koh Cong Lee, May RES -- confirmed RM127,200.00; the rules give
    # RM128,000.00. Carried on QUEK POH HUI (ATAP).
    "d076ddd0-9639-4afa-b505-b1cd827d212f": "-800.0",
    # Caryn Dong Leong Moi, Feb RES -- confirmed RM78,600.00; the rules give
    # RM79,288.00. Carried on CHEW CHYE CHOON (ATAP).
    "d73ada5b-f781-4546-9a0d-dfcc0a75da56": "-688.0",
    # Chan Wing On, Feb RES -- confirmed 2 cases, RM57,000.00; the new rules give RM58,824.00.
    # Carried on 1008563 (TAN LIAN KENG).
    "1771658583603x991793478265471000": "-1824.0",
    # Chan Wing On, May RES -- confirmed RM114,412.00; the rules give
    # RM115,900.00. Carried on SERIJIT KAUR @ SURJIT KAUR A/P PRAM SINGH.
    "4cb4ed2a-236f-4fee-9baa-5b71315453e9": "-1488.0",
    # Olivier Koh Cong Lee, Sep RES -- confirmed RM219,500.00; the rules give
    # RM222,752.44 (TAN YUAN TAT's invoice was raised from RM30,000 to
    # RM31,147.50 in the ERP after 27 Oct). Carried on Tan Teh Siang.
    "c7fadb70-caa8-4567-9453-a599483a2481": "-3252.44",
    # Chan Wing On, Mar RES -- confirmed RM94,400.00; the rules give
    # RM94,518.00. Carried on TEE LI LEONG (ATAP).
    "198457ae-43c7-493c-9c14-da248624f52d": "-118.0",
    # Louis Ng Zhan Yi, Jun RES -- confirmed RM291,620.57; the rules give
    # RM292,308.57. Carried on ANG SIONG HENG (ATAP).
    "d1f99d8f-bc47-447d-8f84-01bf14673708": "-688.0",
    # Olivier Koh Cong Lee, Jun RES -- confirmed RM81,862.78; the rules give
    # RM85,425.56. Carried on KOH CHONG LEE.
    "7d53dcc0-3ee6-48e4-9f78-983cc5198a44": "-3562.78",
    # Olivier Koh Cong Lee, Aug RES -- confirmed RM175,360.00; the rules give
    # RM176,530.00. Carried on LEE ENG CHAI.
    "c15c93af-4d7f-4154-8d4e-0a754b4df085": "-1170.0",
    # Ivan Low Chin Chai, May RES -- confirmed RM47,913.00; the rules give
    # RM50,802.18. Carried on LOW BOON HUAT.
    "efc30b6f-ee81-4550-8dd0-5b9688a4b9d4": "-2889.18",
    # Ling Liang Kang, Jul RES -- confirmed RM206,070.00; the rules give
    # RM209,558.58. Carried on TAN YONG KOK (ATAP).
    "9851d7df-92b9-4868-9997-dd3734cc70b4": "-3488.58",
    # Chan Jia Wei, Mar RES -- confirmed 8 cases, RM201,220.15: INV-1008804,
    # LOH KAH GUAN (ATAP), fully paid, counted at its RM35,436.40 total, without
    # the RM6,888 lucky draw prize the Commission Price adds back.
    "e5eb526a-2013-472b-8d89-ba99a0ee0d30": "-6888",
    # Louis Ng Zhan Yi, Sep RES -- confirmed RM108,300.00; the rules give
    # RM109,696.80. Carried on CHIENG JIN SOON (ATAP).
    "0bd6e7f9-dce9-4638-9c2a-1b3ca2b02ff7": "-1396.8",
}

SALES_REPORT_EP_OVERRIDES = {
    # Chan Jia Wei's two large commercial jobs, which the ERP leaves with no
    # panel count or site type, so calc_ep_points() gave them a point per
    # ringgit. The factory rule is applied with one RM40,000 full-rate block
    # across both, then 40%:
    # INV-1011588, YONG AN EVERCARE SDN BHD (SELCO), RM182,000 -- 40,000 +
    # 142,000 x 40%.
    "499cff9f-f29c-4c0d-9fce-e2dd165cc87d": "96800",
    # INV-1011849, SALAM INDERA SDN. BHD. (PERTOL STATION) (SELCO), RM136,000
    # -- all at 40%, the block already used above.
    "558d56d4-3bee-4c31-93ea-8b125ec5c526": "54400",
    # INV-1009623, GAIN MIRAGE (M) SDN BHD, Sunny Tan -- RM142,400: 40,000 + 102,400 x 40%.
    "66cc8021-4ea3-4d41-86df-9c6df03a4244": "80960",
    # Louis Ng Zhan Yi commercial jobs follow the commercial 40k + 40% rule like Chan Jia Wei:
    # INV-1009088, ENG ANN (M) SDN. BHD. (COMMERCIAL), RM96,000 -- 40,000 + 56,000 x 40%.
    "0e2734b3-1d2e-4964-bc8e-b5db46d41b50": "62400",
    "INV-1009088": "62400",
    # INV-1010710, KIEN HIN TRADING SDN. BHD. (FACTORY), RM100,000 -- adjusted so up to date EP is 1,245,388.07 per user request.
    "e894ce6a-9780-4069-92b6-e59b34be35ff": "40700",
    "INV-1010710": "40700",
}

# Cases placed in a month by hand, overriding the first-payment date (keyed
# on bubble_id; the value is the date that decides the month).
SALES_REPORT_DATE_OVERRIDES = {
    # Kept where the user confirmed them when the 28 Oct 2026 rules (a case's
    # month from its first payment's Created at from August) would move them
    # into or out of a confirmed month:
    # INV-1011420, WENG KAR CHON (ATAP) -- OLIVIER KOH CONG LEE RES m9: kept out (rules would move it in).
    "50d10202-caf0-4b60-ab91-88c85078eab2": "2026-08-31",
    # INV-1011838, CHIN YAN ZHANG (ATAP) -- OLIVIER KOH CONG LEE RES m9: stays (rules would move it).
    "32b3072d-2e8e-492b-8773-83eb429abbf9": "2026-09-30",
    # INV-1011887, TEO GEOK HUI (ATAP) -- OLIVIER KOH CONG LEE RES m9: stays (rules would move it).
    "cd77f06a-9067-45bd-9c96-95cc2753c80b": "2026-09-30",
    # INV-1011835, NGOO KEE HWA (ATAP) -- OLIVIER KOH CONG LEE RES m9: stays (rules would move it).
    "6ac26565-2b3e-433a-8b7c-c22af6c6411a": "2026-09-30",
    # INV-1010797, Ho Khee Fatt -- PHIL MOO WUI KEAD RES m9: stays (rules would move it).
    "136aa3a5-0f57-4b93-85f1-49d55bf8482c": "2026-09-29",
    # INV-1011396, HII KING SENG (ATAP) -- LING LIANG KANG RES m8: stays (rules would move it).
    "2b2e953a-90b3-4b80-a64d-d5161f6e77c1": "2026-08-31",
    # INV-1011483, YONG LEE HUAN (ATAP) -- LOUIS NG ZHAN YI RES m9: kept out (rules would move it in).
    "8e379d41-4a97-4d0a-8c3d-87fda4bf92f8": "2026-08-31",
    # INV-1011475, Chong Chee Fatt -- LOUIS NG ZHAN YI RES m9: kept out (rules would move it in).
    "912fedc6-a485-4e33-8305-fb4767091e5a": "2026-08-31",
    # INV-1011474, LEONG CHEE HAU -- HONGKI YOU TENG HOONG RES m8: stays (rules would move it).
    "591fb12e-0c31-4008-92dc-5110fac81aeb": "2026-08-31",
    # INV-1011470, TEY CHIN CHUAN (ATAP) -- HONGKI YOU TENG HOONG RES m8: stays (rules would move it).
    "a3f87028-fa69-4255-a36e-f46b009cb60b": "2026-08-31",
    # INV-1011316, MEGA SIGNCRAFT SDN BHD (FACTORY) -- HONGKI YOU TENG HOONG COM m8: stays (rules would move it).
    "2eb420cc-1ba4-4e52-94f3-881d45a7e36e": "2026-08-27",
    # INV-1009623, GAIN MIRAGE (M) SDN BHD, Sunny Tan -- paid 14 May 2026 but
    # the payment was only keyed in on 25 July, so it counts in July.
    "66cc8021-4ea3-4d41-86df-9c6df03a4244": "2026-07-25",
    # INV-1010896, CHAMPION PRECISION TECHNOLOGY SDN. BHD. (FACTORY), Olivier Koh
    # Cong Lee -- RM628,000, invoiced 23 Jul, 30% paid 3 Aug. Counted in July,
    # the invoice month, per user request, so August has no COM line and July
    # has 1 COM case, RM628,000.00.
    "f3bc3a72-1f48-4c1b-8015-6205cde96618": "2026-07-23",
    # INV-1008421, MOHD RAZIF BIN SENIN @ ATTAN (ATAP), Nur Anisah -- shifted to January
    # so January RES sales are counted at 2 cases, RM63,600.00 per user request.
    "e5f87cb6-8917-4eba-91c3-521a37fc111b": "2026-01-31",
    "INV-1008421": "2026-01-31",
    # INV-1009895, LIEW CHAN SANG (LIEW MIN HUI) (ATAP), and INV-1009932, GOH
    # HOCK LYE (ATAP), Ching Zhe Hang -- first paid 1 June but not June cases
    # per Finance; counted in May, on their invoice dates, per user request
    # (9 Oct 2026). Until then they were left out of the report.
    "afcec34c-16ed-4c5a-91d4-8a2dadd6dc6b": "2026-05-25",
    "INV-1009895": "2026-05-25",
    "e9807dd0-cadc-4b5a-a798-22a3bf05c746": "2026-05-26",
    "INV-1009932": "2026-05-26",
}

# Cases placed on a RES/COM line by hand, overriding _sales_report_property.
SALES_REPORT_PROPERTY_OVERRIDES = {
    # INV-1011854, INV-1011855, INV-1011856, S.O.S MEWAH SDN BHD (LOT 1236A, 1197,
    # 1194), Carol Siow -- RM184,000 + RM184,000 + RM78,000, invoiced 30 Sep,
    # 30% deposits paid 30 Sep. Counted on the September COM line per user
    # request (14 Oct 2026); the raw data classifies them as Residential.
    "88a4940d-4593-4f12-b641-ee4e4f87ee34": "COM",
    "1846aa9d-a038-4fcb-a114-a7b0a3c8d849": "COM",
    "6d41560c-1933-4ffc-b2ed-736cca73c770": "COM",
    # INV-1009087, ENG ANN (M) SDN. BHD. (COMMERCIAL) (PLO46) (ATAP), Louis Ng.
    "a93e1554-caf7-42db-b233-c3c4976ba756": "RES",
    # INV-1009209, JC PRINT & OFFICE SDN. BHD. (FACTORY), Ching Zhe Hang.
    "2ebb5452-8b6a-4260-9cb2-0c2dcf98c5e8": "RES",
    # INV-1010758, IEM CORPORATE SDN. BHD. (COMMERCIAL) (PERMAS JAYA), Denise.
    "5d802a36-6f08-4397-9871-f89939f1e730": "RES",
    # INV-1009623, GAIN MIRAGE (M) SDN BHD, Sunny Tan -- 120 panels, 50kW
    # 3-phase inverter and a capacitor bank; the name carries no site tag.
    "66cc8021-4ea3-4d41-86df-9c6df03a4244": "COM",
    # INV-1011588, YONG AN EVERCARE SDN BHD (PERTUBUHAN) (SELCO), Chan Jia Wei --
    # RM182,000: 100 x 650W panels, 2 x 18kW hybrid inverters and 4 x 16kWh
    # batteries. SEDA, referral and package type are all blank and panel_qty is
    # unset, so it fell back to Residential.
    "499cff9f-f29c-4c0d-9fce-e2dd165cc87d": "COM",
    # INV-1010583, TEE SOON SIEW (NO 3), Teng Kah Kent -- RM60,000: 45 x 625W
    # panels and a 20kW inverter. The name carries no site tag.
    "118e3946-2579-4131-966b-7fc008b2dcf5": "COM",
    # INV-1008558, DIESEL TRUCK SDN BHD (SELCO) (DIESEL), Olivier Koh Cong Lee --
    # RM62,800: 50 x 620W panels and a 25kW 3-phase inverter. The name carries
    # no site tag, so it fell back to Residential; moved to COM so February RES
    # is the other two cases, RM41,988.00 per user request.
    "1770626846451x595611071444156400": "COM",
    # INV-1008785, JMM HOMEMADE SDN BHD (SHOP-LOT) (ATAP), Olivier Koh Cong Lee --
    # RM68,960: 55 x 620W panels and a 25kW 3-phase inverter. The classifier
    # leaves a company shop lot on Residential; moved to COM so March RES is the
    # other three cases, RM76,397.50 per user request.
    "73299f48-3e8d-4929-942e-dbed470f0408": "COM",
    # INV-1011062, JIN SENG METAL WORKS (20PCS) (FACTORY), RM30,000, and
    # INV-1011109, JIN SENG METAL WORKS (NO 33) (FACTORY) (ATAP), RM33,000,
    # Olivier Koh Cong Lee -- 20 and 24 panels on 3-phase inverters, both under
    # the RM40,000 full-rate EP block. Moved to RES per user request so August
    # RES has 6 cases; Champion Precision stays COM.
    "90186345-d61c-4344-91c1-f32b38d74411": "RES",
    "fa52eced-ae8d-4f7b-b952-0ec48962486d": "RES",
    # INV-1010766, LEA COURT HOTEL SDN BHD 90A-B (SHOP-LOT) (ATAP), RM30,000, and
    # INV-1010895, LEA COURT HOTEL SDN BHD 92A-B (SHOP-LOT) (ATAP), RM30,000,
    # Koh Yi Jay -- 23 and 21 panels on 3-phase inverters, both under the
    # RM40,000 full-rate EP block. Moved to RES per user request so September
    # shows 2 RES cases, RM60,000.00, and no COM.
    "e280062c-a506-4bc9-9d73-9a419a5b90d0": "RES",
    "4fc343d0-f5f4-4ef9-9cf3-d190318c080d": "RES",
    # INV-1008371, T S BEACH RESORT SDN. BHD. (ATAP), Dean Wai Leong Yee --
    # RM119,800, first paid 9 Jan (10%): a resort on a commercial scale. The
    # classifier left it on Residential; moved to COM per user request so
    # January shows 1 COM case, RM119,800.00, and no RES. Invoice number 1008371
    # is shared with another case, hence the bubble_id.
    "1767781549782x938580067228319700": "COM",
    # INV-1010250, AIR TO AIR ENGINEERING SDN BHD (FACTORY), Carol Siow --
    # moved to RES per user request so June RES has 7 cases.
    "aba17398-0623-48a6-91ba-6834a5146c97": "RES",
    "INV-1010250": "RES",
    # 1008532, GOH LAY MUI (SHOP-LOT) (ATAP - SELCO), Chan Wing On --
    # moved to COM per user request.
    "1769926340720x294573873479811100": "COM",
    "1008532": "COM",
    # INV-1009339, MX FRESH MART (M) SDN BHD (SHOP-LOT), Winson Tan --
    # moved to COM so April has 2 RES (RM37,600.00) and 1 COM (RM58,000.00) per user request.
    "79937470-ca19-405e-9154-8238918a95df": "COM",
    "INV-1009339": "COM",
}

# Cases credited on the report to a different agent than the invoice names,
# keyed on bubble_id. The value is the agent's ERP name, so the case joins
# that agent's existing row.
SALES_REPORT_AGENT_OVERRIDES = {
    # INV-1008782, PANG CHONG PENG ("5ba7279e-efd8-467e-a419-984cbf5a9f5d"), RM19,800,
    # invoiced under Kenyou, used to be credited here to CHAN JIA WEI alone. It is
    # now a shared case instead (SALES_REPORT_SHARED_CASES below), so it counts
    # for Kenyou, the agent on the invoice, as well.
}
# Cases credited to a second agent IN ADDITION to the one the invoice names,
# keyed on bubble_id; the value lists the extra agents' ERP names. The invoice
# is then on the report once per agent, so the company-wide case and sales
# totals run higher than the real invoices by each shared case.
SALES_REPORT_SHARED_CASES = {
    # INV-1008782, PANG CHONG PENG, RM19,800, 31 Mar -- invoiced under Kenyou and
    # also Chan Jia Wei's case. Counted for both per user request: Kenyou's March
    # is 2 RES, RM51,800.00 and Chan Jia Wei's is 9 RES, RM221,020.15.
    "5ba7279e-efd8-467e-a419-984cbf5a9f5d": ("CHAN JIA WEI",),
}
# Cases counted by hand before any payment is on the invoice, keyed on
# bubble_id; the value is the date the case is placed in. Such invoices are
# left out of the EGA rows (no 1st payment) and out of the payments check, so
# they are read in by bubble_id here, see _sales_report_manual_rows.
SALES_REPORT_MANUAL_CASES = {
    # Caryn Dong, May RES -- per user request (14 Oct 2026). Each is a RM24,000
    # draft invoiced 8 May 2026 with no payment recorded yet.
    # INV-1009569, KOK YON LAN
    "6592d0b3-d3ff-4e2f-934d-a8213ff51b66": "2026-05-08",
    # INV-1009570, KOK MING CHOO (ATAP)
    "ad72813c-401d-485a-9da6-49788ba54ecf": "2026-05-08",
    # INV-1009573, KOK JIA XUAN (ATAP)
    "711ddc87-4de2-4d95-8eb7-0db57755fb8a": "2026-05-08",
}
SALES_REPORT_TARGETS = {"Internal": 600000.0, "Outsource": 720000.0}
# ESA is the higher award and runs to December, where EGA closes in June.
# Both are overridden per channel by the Data page; these are only the fallback
# for a year nobody has set up yet.
SALES_REPORT_ESA_TARGETS = {"Internal": 1300000.0, "Outsource": 1560000.0}


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
    """"Apr Qualifier", in the printed report's wording -- which spells
    September "Sept"."""
    if not month or not (1 <= int(month) <= 12):
        return "Qualifier"
    name = "Sept" if int(month) == 9 else SALES_REPORT_MONTHS[int(month) - 1]
    return f"{name} Qualifier"


# The early-bird and ESA columns name a qualifier by the month they cleared
# the bar, but HR counts anyone who had cleared it by 30 September as a
# "Sept Qualifier", however early (CHAN JIA WEI crossed in July, LOUIS NG in
# August; both read Sept). Later crossings read as their own month.
SALES_REPORT_EARLIEST_QUALIFIER_MONTH = 9


def _first_crossing_month(rows, bar, max_month: int):
    """The month these (date, EP) rows' running total first reaches `bar`,
    looking no further than `max_month`; None if it never does."""
    from decimal import Decimal as _D
    running, target = _D("0"), _D(str(bar))
    for when, ep in sorted(rows, key=lambda x: str(x[0])):
        try:
            mth = int(str(when)[5:7])
        except (TypeError, ValueError):
            continue
        if mth > max_month:
            break
        running += ep
        if running >= target:
            return mth
    return None


def _early_qualifier_label(rows, bar, max_month: int, latest: int) -> str:
    """"Sept Qualifier" / "Oct Qualifier" ... for an early-bird or ESA win:
    the month the bar was first cleared, no earlier than September and no
    later than `latest` (the rung the award was won on)."""
    crossed = _first_crossing_month(rows, bar, max_month) or latest
    return _qualifier_label(min(latest, max(crossed, SALES_REPORT_EARLIEST_QUALIFIER_MONTH)))


# The ESA early-bird bars for October and November, per channel. The Data page
# holds these as the Oct and Nov rungs of each channel's ESA ladder and wins
# when it has them; these are only the fallback, and they are the scheme's own
# numbers: Internal 1M / 1.2M, Outsource 1.36M / 1.46M.
SALES_REPORT_EARLY_BIRD_DEFAULTS = {
    10: {"Internal": 1000000.0, "Outsource": 1360000.0},
    11: {"Internal": 1200000.0, "Outsource": 1460000.0},
}


def _sales_report_early_bird_targets(ladders: dict) -> dict[int, dict[str, float]]:
    """{10: {channel: bar}, 11: {channel: bar}} read from each channel's ESA
    ladder (the Data page), falling back to SALES_REPORT_EARLY_BIRD_DEFAULTS
    for a rung the page does not carry."""
    out: dict[int, dict[str, float]] = {}
    for month, defaults in SALES_REPORT_EARLY_BIRD_DEFAULTS.items():
        bag = dict(defaults)
        for channel in bag:
            rung = ((ladders.get(channel) or ({}, {}))[1] or {}).get(month)
            rung = rung[0] if isinstance(rung, (tuple, list)) else rung
            try:
                if rung is not None:
                    bag[channel] = float(rung)
            except (TypeError, ValueError):
                pass
        out[month] = bag
    return out


def _sales_report_early_bird_stage(year: int, today=None) -> str:
    """Which early-bird column the report carries: "oct" until the end of
    October, "nov" from 1 November of `year`. One column at a time, the same
    way EGA gives way to ESA -- October's deadline has passed by then, and the
    November column still tells an October qualifier apart from the rest."""
    from datetime import date
    today = today or date.today()
    return "nov" if today >= date(int(year), 11, 1) else "oct"


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


def _first_paid_in_year_sql(alias: str, year: int) -> str:
    """SQL test: the invoice's first payment landed in `year`, Malaysia time.

    The first payment is the invoice's own 1st_payment_date when it is filled,
    otherwise its earliest payment row: KAREN WONG KAI LIN's INV-1008492 was
    paid on 28 February 2026 but the field was never set. Written as an
    uncorrelated IN so the payment table is aggregated once rather than probed
    invoice by invoice -- the correlated form took a query to ~30s."""
    y = int(year)
    return (f'(EXTRACT(YEAR FROM ({alias}."1st_payment_date" AT TIME ZONE '
            f"'Asia/Kuala_Lumpur'))::int = {y}"
            f' OR ({alias}."1st_payment_date" IS NULL AND {alias}.bubble_id IN ('
            f"SELECT fp.linked_invoice FROM payment fp GROUP BY fp.linked_invoice "
            f"HAVING EXTRACT(YEAR FROM (MIN(fp.payment_date) AT TIME ZONE "
            f"'Asia/Kuala_Lumpur'))::int = {y})))")


_CARRIED_ROWS_CACHE: dict[int, tuple[float, list]] = {}


def _sales_report_carried_rows(year: int, out_mod) -> list:
    """Cases first paid in `year` that the cached EGA rows miss.

    The cached rows hold only invoices dated in `year` with 1st_payment_date
    filled, yet the report places a case by its first payment, so two kinds
    belong here: invoiced in an earlier year (Vincent Tan's 1007996, invoiced
    20 November 2025, first paid 29 January 2026), and paid but with the date
    never set (his INV-1008492, paid 28 February 2026). Same query and row
    shape as the EGA rows, only the year filter differs. A failure returns
    nothing -- the report then reads as it did before."""
    key = int(year)
    hit = _CARRIED_ROWS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]
    try:
        rows = _pg_query(out_mod._invoices_sql(year, upto_month=None, carried_in=True)) or []
    except Exception:
        _log("[SALES REPORT] could not read cases carried in from last year")
        return hit[1] if hit else []
    rows = rows + _sales_report_manual_rows(year, out_mod)
    _CARRIED_ROWS_CACHE[key] = (time.time(), rows)
    return rows


def _sales_report_manual_rows(year: int, out_mod) -> list:
    """The invoices in SALES_REPORT_MANUAL_CASES, in the same row shape as the
    EGA rows. A failure returns nothing, so the report reads without them."""
    if not SALES_REPORT_MANUAL_CASES:
        return []
    try:
        return _pg_query(out_mod._invoices_sql(
            year, upto_month=None, only_bubbles=tuple(SALES_REPORT_MANUAL_CASES))) or []
    except Exception:
        _log("[SALES REPORT] could not read the manually counted cases")
        return []


_LINE_ADJUSTMENTS_CACHE: dict[int, tuple[float, dict]] = {}


def _sales_report_line_adjustments(year: int) -> dict[str, "Decimal"]:
    """invoice bubble_id -> correction to its sales value from invoice lines.

    Two kinds, netted per invoice:

    * +118, the Kluang Opening (16/5/2026) lucky draw. Every Feb & Mar customer
      was guaranteed RM118, keyed as a -118 line ("KLUANG OPENING 16/5/2026
      LUCKY DRAW GUARANTEED FOR ATTENDEES : FEB & MAR 2026 CUSTOMERS -
      RM118.00"). The report adds it back once the invoice is fully paid:
      LIM CHIT CHOON (Louis, paid up) counts RM25,500, while ADNAN BIN AB
      WAHAB (Zul) and James (Wong Jia Xuan), on deposit only, stay at their
      invoice totals. Paid-up is judged as of today, so a case gains the RM118
      when its last payment lands. Only the -118 lines -- the real prizes
      (RM688 and up, e.g. LOH KAH GUAN's RM6,888) stay deducted.
    * -600, a free robot. A customer could take a vacuum robot or RM600 off
      ("No IMILAB V1 Robot swap discount"). Most took the money; one who took
      the robot (an RM0 "Free Imilab V1 Vacuum Robot" or "FOC : 1x IMILAB V1
      Robot" line) is valued the same: SALLY LIM (Denise) counts RM17,400.

    Each distinct line is taken once per invoice, since revisions repeat their
    items. Retried like the case dates: an empty map would quietly drop every
    correction."""
    from decimal import Decimal as _D

    key = int(year)
    hit = _LINE_ADJUSTMENTS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]
    in_year = (f"COALESCE(i.is_deleted, FALSE) IS NOT TRUE "
               f"AND (EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)} "
               f"OR {_first_paid_in_year_sql('i', year)})")
    items_join = ("JOIN invoice_item ii ON (ii.linked_invoice = i.bubble_id "
                  "OR ii.bubble_id = ANY(i.linked_invoice_item))")
    sql = f"""
        SELECT bubble_id, SUM(amt) AS amt FROM (
          SELECT DISTINCT ON (i.bubble_id, TRIM(ii.description))
                 i.bubble_id, 118::numeric AS amt
            FROM invoice i {items_join}
           WHERE {in_year}
             AND ii.description ILIKE '%lucky draw%'
             AND COALESCE(ii.amount, ii.unit_price, 0) = -118
             AND COALESCE((SELECT SUM(p.amount) FROM payment p
                            WHERE p.linked_invoice = i.bubble_id), 0)
                 >= COALESCE(i.total_amount, 0) - 0.005
           ORDER BY i.bubble_id, TRIM(ii.description)
        ) lucky GROUP BY bubble_id
        UNION ALL
        SELECT DISTINCT i.bubble_id, -600::numeric AS amt
          FROM invoice i {items_join}
         WHERE {in_year}
           AND ii.description ~* '^\\s*(free|foc)\\M.*robot'
           AND COALESCE(ii.amount, ii.unit_price, 0) = 0
    """
    rows = None
    for attempt in range(3):
        try:
            rows = _pg_query(sql)
            break
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if rows is None:
        _log("[SALES REPORT] could not read invoice line corrections after 3 tries")
        return hit[1] if hit else {}
    out: dict[str, "Decimal"] = {}
    for r in rows or []:
        bid = str(r.get("bubble_id") or "").strip()
        if bid:
            out[bid] = out.get(bid, _D("0")) + _D(str(r.get("amt") or 0))
    _LINE_ADJUSTMENTS_CACHE[key] = (time.time(), out)
    return out


_SOLAR_EV_CACHE: dict[int, tuple[float, frozenset]] = {}


def _sales_report_solar_or_ev(year: int) -> frozenset | None:
    """bubble_ids of `year`'s invoices that sell solar panels or an EV charger.

    Only those are sales on this report. ZHU, HUAIZHAO's 1008497 (Woon Jia
    Keat, April) is a DC cable and conceal fee added to a 2025 solar job;
    capacitor banks, a lone inverter, a battery or a cleaning service are not
    solar packages either. Judged on the lines, not the package flag, which is
    missing on many real jobs (the factory and S.O.S MEWAH invoices): a panel
    line reads "<count> x <wattage | brand>", e.g. "50X Jinko Tiger Neo",
    "34 x ASTRONERGY580W", "(33+1)X Jinko".

    None when the lookup fails, so the caller can skip the filter rather than
    empty the report."""
    key = int(year)
    hit = _SOLAR_EV_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]
    sql = f"""
        SELECT DISTINCT i.bubble_id
          FROM invoice i
          JOIN invoice_item ii
            ON (ii.linked_invoice = i.bubble_id
                OR ii.bubble_id = ANY(i.linked_invoice_item))
         WHERE COALESCE(i.is_deleted, FALSE) IS NOT TRUE
           AND (EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
                OR {_first_paid_in_year_sql("i", year)})
           AND (ii.description ~* '\\m\\d+\\s*\\)?\\s*x\\s*(\\d{{3}}\\s*w\\M|jinko|astro|longi|trina|ja\\s*solar|canadian)'
                OR ii.description ~* '\\mev\\M.*charg|charger')
    """
    rows = None
    for attempt in range(3):
        try:
            rows = _pg_query(sql)
            break
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if rows is None:
        _log("[SALES REPORT] could not read solar/EV lines; not filtering on them")
        return hit[1] if hit else None
    out = frozenset(str(r.get("bubble_id") or "").strip() for r in rows if r.get("bubble_id"))
    _SOLAR_EV_CACHE[key] = (time.time(), out)
    return out


_DELETED_STATUS_CACHE: dict[int, tuple[float, frozenset]] = {}


def _sales_report_deleted_status(year: int) -> frozenset:
    """bubble_ids of `year`'s cases whose invoice status reads "deleted".

    The is_deleted flag is not always set when an invoice is deleted:
    LEE THIEN BOON's INV-1008402 has status "deleted" and is_deleted false.
    Judged on the same latest revision the case dates use, so an invoice that
    was deleted and then restored still counts. Retried like the case dates:
    an empty set would quietly put the deleted cases back."""
    key = int(year)
    hit = _DELETED_STATUS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]
    sql = f"""
        SELECT bubble_id FROM (
          SELECT i.bubble_id, i.status,
                 ROW_NUMBER() OVER (
                   PARTITION BY i.bubble_id
                   ORDER BY COALESCE(i.is_latest, FALSE) DESC,
                            i.invoice_date DESC NULLS LAST, i.id DESC) AS rn
            FROM invoice i
           WHERE i.invoice_date IS NOT NULL
             AND (EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
                  OR {_first_paid_in_year_sql("i", year)})
             AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
        ) z WHERE rn = 1 AND LOWER(BTRIM(COALESCE(status, ''))) = 'deleted'
    """
    rows = None
    for attempt in range(3):
        try:
            rows = _pg_query(sql)
            break
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if rows is None:
        _log("[SALES REPORT] could not read deleted invoice statuses after 3 tries")
        return hit[1] if hit else frozenset()
    out = frozenset(str(r.get("bubble_id") or "").strip() for r in rows if r.get("bubble_id"))
    _DELETED_STATUS_CACHE[key] = (time.time(), out)
    return out


def _sales_report_case_dates(year: int) -> dict[str, str]:
    """invoice bubble_id -> the date that decides which month a case falls in.

    One rule for every case: it belongs to the month its FIRST PAYMENT landed.
    Teo Kim Seng is invoiced 5 February but first paid 31 January, and the
    printed report counts him in January; Lim Lee Peng is invoiced 5 January but
    first paid 30 December, so he is not in 2026 at all.

    The paid date decides the month all year (user, 28 Oct 2026). From August
    2026 a first payment keyed in (Created at) in a later month than it was
    paid -- paid 29 August, keyed in 9 September -- is a late entry: still an
    August case here, but it earns no contest points. Those invoices are kept
    in _LATE_ENTRY_CACHE; see _sales_report_late_entries().

    Keyed on bubble_id, not invoice number: one number can carry several
    revisions with different dates -- 1008358 has first payments at 2026-01-03
    and 2025-12-30 -- so keying on the number can return a revision the report
    is not using. The same "latest revision" rule the report applies keeps the
    date and the money on one row.

    Dates are read in Malaysia time. Older rows store a Malaysian date as
    16:00 UTC the day before, so a plain ::date put NG CHEE HONG (1008310),
    first paid 1 January 2026, on 31 December and dropped him from the year.

    The invoice date does not limit the map either: Vincent Tan's 1007996 was
    invoiced 20 November 2025 and first paid 29 January 2026, so it is a
    January case. See _sales_report_carried_rows().
    """
    key = int(year)
    hit = _FIRST_PAYMENT_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _RESPONSE_CACHE_TTL_SECONDS:
        return hit[1]

    sql = f"""
        SELECT bubble_id, p1, c, late FROM (
          SELECT i.bubble_id,
                 COALESCE(fp.fd,
                          (i."1st_payment_date" AT TIME ZONE 'Asia/Kuala_Lumpur')::date) AS p1,
                 (fp.fd >= DATE '{SALES_REPORT_CREATED_AT_FROM}'
                  AND date_trunc('month', fp.fc) > date_trunc('month', fp.fd)) AS late,
                 (i.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS c,
                 ROW_NUMBER() OVER (
                   PARTITION BY i.bubble_id
                   ORDER BY COALESCE(i.is_latest, FALSE) DESC,
                            i.invoice_date DESC NULLS LAST, i.id DESC) AS rn
            FROM invoice i
            -- The invoice's first payment row (a real amount before a RM0
            -- one): its payment date, which places the case, and its Created
            -- at -- when Finance keyed it in, as the hover shows it -- which
            -- marks a late entry from August.
            LEFT JOIN (SELECT DISTINCT ON (linked_invoice) linked_invoice,
                              (payment_date AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS fd,
                              (COALESCE(created_date, created_at)
                                 AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS fc
                         FROM payment
                        WHERE payment_date IS NOT NULL
                        ORDER BY linked_invoice, (COALESCE(amount, 0) > 0) DESC,
                                 payment_date, created_at) fp
                   ON fp.linked_invoice = i.bubble_id
           WHERE i.invoice_date IS NOT NULL
             AND (EXTRACT(YEAR FROM i.invoice_date)::int = {int(year)}
                  OR {_first_paid_in_year_sql("i", year)})
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
    late: set = set()
    for r in rows or []:
        bid = str(r.get("bubble_id") or "").strip()
        if not bid:
            continue
        paid = str(r.get("p1") or "")[:10]
        if paid:
            out[bid] = paid
        if str(r.get("late")).lower() in ("true", "t", "1"):
            late.add(bid)
    _FIRST_PAYMENT_CACHE[key] = (time.time(), out)
    _LATE_ENTRY_CACHE[key] = frozenset(late)
    return out


_LATE_ENTRY_CACHE: dict[int, frozenset] = {}


def _sales_report_late_entries(year: int) -> frozenset:
    """bubble_ids whose first payment was keyed in a later month than it was
    paid, from August 2026: they earn no EGA/ESA, Early Bird or Monthly
    Contest points (user, 28 Oct 2026). Read alongside the case dates."""
    _sales_report_case_dates(year)
    return _LATE_ENTRY_CACHE.get(int(year), frozenset())


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
           WHERE ((invoice_date >= DATE '{year:04d}-01-01'
                   AND invoice_date <  DATE '{year + 1:04d}-01-01')
                  OR {_first_paid_in_year_sql("invoice", year)})
             AND COALESCE(is_deleted, FALSE) IS NOT TRUE
        )
        SELECT p.linked_invoice AS inv,
               COALESCE(p.amount, 0)::numeric AS a,
               (p.payment_date AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS d,
               (COALESCE(p.created_date, p.created_at)
                  AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS m
          FROM payment p
          JOIN inv ON inv.bubble_id = p.linked_invoice
         WHERE p.payment_date IS NOT NULL
           AND (p.payment_date AT TIME ZONE 'Asia/Kuala_Lumpur')
                 < DATE '{end_year:04d}-{end_month:02d}-01'
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


def _case_hover_entry(customer_name, payments, system_price=None, ans=None, fully_paid=None,
                      paid_pct=None):
    """One case for the hover panel, or None when there is no customer to name.

    An entry with no payments is still worth returning: the panel says "no
    payment recorded in this period", which is a real answer rather than a
    silently missing row.

    "s" is the invoice's System Price -- its total amount, what the customer
    is billed -- read by the customer list under the report; the hover panel
    ignores it. "v" is the ANS the case counts at and "f" whether the invoice
    is fully paid -- the hover names it Sales price or Original price. "pc" is
    the share of the invoice total paid to date, for the % Payment column.
    """
    name = str(customer_name or "").strip()
    if not name:
        return None
    entry = {"n": name, "p": list(payments or []), "s": float(system_price or 0)}
    if ans is not None:
        entry["v"] = float(ans)
        entry["f"] = bool(fully_paid)
    if paid_pct is not None:
        entry["pc"] = round(float(paid_pct), 1)
    return entry


# Sales staff tag non-residential sites in the customer name:
# "JC PRINT & OFFICE SDN. BHD. (FACTORY) (ATAP)". (ATAP)/(20PCS) describe the
# roof or package, not the property, so only these words count.
# Shop lots are not here: they go by panel count (SR_SHOP_LOT_MAX_PANELS).
_SR_SITE_TAG = re.compile(r"\b(?:FACTORY|KILANG|COMMERCIAL)\b", re.I)
_SR_SHOP_LOT = re.compile(r"\bSHOP[\s-]?LOTS?\b", re.I)
# A shop lot with this many panels or fewer is Residential; more is Commercial
# (user, 2026-10-27). It matches the hand overrides made before the rule: LEA
# COURT HOTEL 23 and 21 panels RES, GOH LAY MUI 67 and MX FRESH MART 48 COM.
SR_SHOP_LOT_MAX_PANELS = 36
_SR_BUSINESS_PREMISE = re.compile(
    r"\bHOTEL\b|\bRESORT\b|\b(?:PETROL|PERTOL)\s+STATION\b|\bCLINIC\b|\bKLINIK\b", re.I)
# A company can own a house, so these only ask for a look; they never decide.
_SR_COMPANY_NAME = re.compile(
    r"SDN\.?\s*BHD|\bBHD\b|\bENTERPRISE\b|\bTRADING\b|\bSYARIKAT\b|\bINDUSTRIES\b"
    r"|\bMETAL\s+WORKS\b|\bMANUFACTUR", re.I)


def _sales_report_property(row: dict, recorded: str,
                           panels: int | None = None) -> tuple[str, str | None]:
    """("RES" | "COM", review note or None) for one Sales Report invoice.

    `recorded` is classify_property_type()'s answer. Anything it calls
    non-residential (Factory, Government, ...) is COM -- except a shop lot,
    which goes by its panel count: SR_SHOP_LOT_MAX_PANELS or fewer is RES,
    more is COM. A shop lot is one the classifier recorded as such, or a
    Residential whose customer name says SHOP LOT (most of them: the ERP
    leaves the site type blank). The classifier also files plain "COMMERCIAL"
    under Shop Lot, so a case whose own fields say COMMERCIAL stays COM. Its
    Residential is often only a fallback, so the customer name gets a say: an
    explicit site tag or a business premise makes it COM. A Residential that
    SEDA or the referral actually recorded is kept, and a disagreeing name is
    flagged instead. EP points keep using `recorded`; this only places the case
    on a RES/COM line.
    """
    name = str(row.get("customer_name") or "")
    commercial = "COMMERCIAL" in " ".join(
        str(row.get(k) or "") for k in (
            "seda_nem_type", "referral_project_type", "package_type",
            "package_name_snapshot", "description", "customer_name")).upper()
    resid = recorded.lower().startswith("resid")
    if not commercial and (recorded == "Shop Lot" or (resid and _SR_SHOP_LOT.search(name))):
        if not panels:
            return "RES", "Shop lot with no panel count; counted as Residential"
        return ("RES" if panels <= SR_SHOP_LOT_MAX_PANELS else "COM"), None
    if not resid:
        return "COM", None
    explicit = (
        "RAKYAT" in str(row.get("seda_nem_type") or "").upper()
        or "RESIDENTIAL" in str(row.get("referral_project_type") or "").upper())
    named = _SR_SITE_TAG.search(name) or _SR_BUSINESS_PREMISE.search(name)
    if named and explicit:
        return "RES", (f"Customer name says {named.group(0).upper()} but SEDA/referral "
                       "records Residential")
    if named:
        return "COM", None
    if _SR_COMPANY_NAME.search(name) and not explicit:
        return "RES", "Company name with no property tag; site type not recorded"
    return "RES", None


def _sales_report_commission_prices(year: int) -> dict[str, "Decimal"]:
    """invoice number -> its Commission Price (Basic + NFP commission), from the
    cached commission runs.

    Each channel's NFP figure is read only for invoices in that channel's Basic
    lines: the two NFP maps can carry the same invoice number for different
    cases, and pairing across channels would mix them up.
    """
    from decimal import Decimal

    out: dict[str, Decimal] = {}
    for agent_type in ("internal", "outsource"):
        bundle = get_cached_data(year, agent_type) or {}
        basic, nfp = bundle.get("basic"), bundle.get("nfp")
        if not basic or not nfp:
            continue
        nfp_by_inv = nfp[-1] or {}
        prices: dict[str, Decimal] = {}
        for ln in basic[-1] or []:
            inv = str(getattr(ln, "invoice_number", "") or "").strip()
            if inv:
                prices[inv] = (prices.get(inv, Decimal("0"))
                               + Decimal(str(getattr(ln, "basic_commission", 0) or 0)))
        for inv, price in prices.items():
            nfp_row = nfp_by_inv.get(inv)
            nfp_comm = getattr(nfp_row, "nfp_commission", None) if nfp_row else None
            out[inv] = (price + Decimal(str(nfp_comm or 0))).quantize(Decimal("0.01"))
    return out


def _sales_report_price_records(year: int) -> dict:
    """(invoice number, customer lowercased) and invoice number -> the NFP
    engine's record for the invoice (EPP cost, invoice date, price lines), from
    which invoice_price_basis works out the Original Price. The customer name
    picks between channels that share an invoice number."""
    out: dict = {}
    for agent_type in ("internal", "outsource"):
        nfp = (get_cached_data(year, agent_type) or {}).get("nfp")
        for rec in ((nfp[-1] or {}).values() if nfp else ()):
            inv = str(getattr(rec, "invoice_number", "") or "").strip()
            if not inv:
                continue
            cust = str(getattr(rec, "customer_name", "") or "").strip().lower()
            out.setdefault((inv, cust), rec)
            out.setdefault(inv, rec)
    return out


def _sales_report_panel_counts(year: int) -> dict:
    """(invoice number, customer name lowercased) -> panel count, plus
    invoice number alone -> panel count, from the cached NFP runs.

    The EGA rows the report is built from carry invoice.panel_qty, which is
    blank on most jobs; the NFP engine also reads it off the package and item
    text. The two channels' maps can share an invoice number for different
    cases, so the customer name picks between them when it can.
    """
    out: dict = {}
    for agent_type in ("internal", "outsource"):
        nfp = (get_cached_data(year, agent_type) or {}).get("nfp")
        for inv_row in ((nfp[-1] or {}).values() if nfp else ()):
            qty = getattr(inv_row, "panel_qty", None)
            inv = str(getattr(inv_row, "invoice_number", "") or "").strip()
            if not qty or not inv:
                continue
            cust = str(getattr(inv_row, "customer_name", "") or "").strip().lower()
            out[(inv, cust)] = int(qty)
            out.setdefault(inv, int(qty))
    return out


def _paid_in_full_on_first_payment(payments: list | None, net_total) -> bool:
    """True when the money received on the first payment day covers the whole
    invoice. Paying in full later, over several payments, does not count."""
    if not payments:
        return False
    days = [p["d"] for p in payments if p.get("d")]
    if not days:
        return False
    first_day = min(days)
    first = sum(p["a"] for p in payments if p.get("d") == first_day)
    return net_total > 0 and first >= float(net_total) - 0.005


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
           WHERE ((invoice_date >= DATE '{year:04d}-01-01'
                   AND invoice_date <  DATE '{year + 1:04d}-01-01')
                  OR {_first_paid_in_year_sql("invoice", year)})
             AND COALESCE(is_deleted, FALSE) IS NOT TRUE
        )
        SELECT p.linked_invoice AS inv, SUM(COALESCE(p.amount, 0))::numeric AS paid
          FROM payment p
          JOIN inv ON inv.bubble_id = p.linked_invoice
         WHERE p.payment_date IS NOT NULL
           AND (p.payment_date AT TIME ZONE 'Asia/Kuala_Lumpur')
                 < DATE '{end_year:04d}-{end_month:02d}-01'
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
    late = _sales_report_late_entries(year) if first_paid else frozenset()
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
        # A late entry (paid in one month, keyed in a later one, from August)
        # is listed but earns no points, as on the Sales Report.
        if str(row.get("bubble_id") or "") in late:
            ep = _D("0")
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


# _SALES_REPORT_PAYLOAD_CACHE defined above


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
    # The early-bird stage is part of the key so the October column hands over
    # to November's at midnight on 1 November, not up to a TTL later.
    key = (int(year), None if through is None else int(through), scope,
           _sales_report_early_bird_stage(year))
    try:
        from flask import has_request_context, request
        if has_request_context() and (request.args.get("_t") or request.args.get("refresh") or request.args.get("nocache")):
            _SALES_REPORT_PAYLOAD_CACHE.clear()
    except Exception:
        pass
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
    if "sql_replay" in _data_cache and _data_cache["sql_replay"]:
        try:
            query_replay.restore(_data_cache["sql_replay"])
        except Exception:
            pass
    bundle = get_cached_data(year, "outsource")
    if not bundle or "ega_raw" not in bundle:
        load_disk_cache()
        bundle = get_cached_data(year, "outsource")
    if not bundle or "ega_raw" not in bundle:
        try:
            bundle = _fetch_commission_bundle(year, "outsource")
            if bundle:
                set_cached_data(year, "outsource", bundle)
        except Exception as err:
            _log(f"[SALES REPORT] failed to auto-fetch outsource bundle for {year}: {err}")
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
    # The October and November early-bird bars, per channel, from the same ESA
    # ladders -- so a bar edited on the Data page moves the column with it.
    early_targets = _sales_report_early_bird_targets(ladders)
    oct_targets, nov_targets = early_targets[10], early_targets[11]
    early_stage = _sales_report_early_bird_stage(year)

    # Ten independent lookups, fetched together rather than one after another.
    # Run in sequence they cost about 15 seconds, and profiling puts nearly all
    # of that in socket reads -- this is not Python being slow, it is ten round
    # trips to the proxy taken one at a time. In parallel the wait is the
    # slowest of them instead of their sum.
    #
    # Safe to thread: none of the ten touches the Flask request context, and
    # db.py takes its lock for writes only, so these reads do not serialise
    # against each other. The scope filter that DOES read the request is
    # applied further down, on this thread.
    def _with_replay(fn, *args):
        import contextlib
        with (query_replay.replaying() if "sql_replay" in _data_cache and _data_cache["sql_replay"] else contextlib.nullcontext()):
            return fn(*args)

    with ThreadPoolExecutor(max_workers=10) as ex:
        f_roles = ex.submit(_with_replay, _sales_report_role_index)
        f_payments = ex.submit(_with_replay, _sales_report_payments, year, last_month)
        f_payment_rows = ex.submit(_with_replay, _sales_report_payment_rows, year, last_month)
        f_case_dates = ex.submit(_with_replay, _sales_report_case_dates, year)
        f_carryover = ex.submit(_with_replay, _sales_report_carryover, year)
        f_targets = ex.submit(_with_replay, _sales_report_award_targets, year)
        f_carried = ex.submit(_with_replay, _sales_report_carried_rows, year, out_mod)
        f_deleted = ex.submit(_with_replay, _sales_report_deleted_status, year)
        f_lines = ex.submit(_with_replay, _sales_report_line_adjustments, year)
        f_solar = ex.submit(_with_replay, _sales_report_solar_or_ev, year)

        # .result() re-raises in this thread, so a failing lookup still fails
        # the request the way it did when these ran in a straight line.
        roles = f_roles.result()
        payments = f_payments.result()
        payment_rows = f_payment_rows.result()
        case_dates = f_case_dates.result()
        late_entries = _sales_report_late_entries(year)
        carryover = f_carryover.result()
        targets, esa_targets = f_targets.result()
        # A new list, not +=: `rows` is the cached bundle's own list.
        rows = rows + f_carried.result()
        deleted = f_deleted.result()
        line_adj = f_lines.result()
        solar_ev = f_solar.result()
    # The multiplier is a first-half device. A report that runs into July or
    # beyond applies none at all.
    multiplier_in_period = last_month <= SALES_REPORT_SPLIT_MONTH
    zero = Decimal("0")
    panel_counts = _sales_report_panel_counts(year)
    price_records = _sales_report_price_records(year)
    # Everything paid on the year's invoices so far, for "fully paid".
    paid_to_date = _sales_report_payments(year, 12)
    rates_dir = str(REPO_ROOT / "1. Basic Commission" / "3. Python Script")
    if rates_dir not in sys.path:
        sys.path.append(rates_dir)
    import invoice_price_basis as _ipb
    agents: dict[str, dict] = {}
    seen_bubble: set = set()
    prop_counts: dict[str, int] = {}

    def _credited(rows_):
        """Each row once for the agent it is credited to, then once more for
        every extra agent in SALES_REPORT_SHARED_CASES."""
        for row in rows_:
            yield row, None
            for extra in SALES_REPORT_SHARED_CASES.get(str(row.get("bubble_id") or ""), ()):
                yield row, extra

    for r, shared_with in _credited(rows):
        bubble = r.get("bubble_id")
        if (bubble, shared_with) in seen_bubble:
            continue
        seen_bubble.add((bubble, shared_with))

        # A case lands in the month its money first arrived. An invoice
        # raised in December and first paid in January is January's; one raised
        # in January but first paid last December belongs to last year and
        # drops out of this report entirely.
        placed_on = (SALES_REPORT_DATE_OVERRIDES.get(str(bubble or "").strip())
                     or SALES_REPORT_DATE_OVERRIDES.get(str(r.get("invoice_number") or "").strip())
                     or SALES_REPORT_MANUAL_CASES.get(str(bubble or "").strip())
                     or case_dates.get(str(bubble or "")))
        when = int_mod._parse_invoice_date(placed_on) if placed_on else None
        if when is None:
            # Fall back to invoice date if first payment date is not recorded.
            when = int_mod._parse_invoice_date(r.get("invoice_date"))
        if when is None or when.year != year or when.month > last_month:
            continue

        raw = shared_with or SALES_REPORT_AGENT_OVERRIDES.get(
            str(bubble or ""), str(r.get("agent_name") or "").strip())
        if (_sales_report_excluded(raw) or _sales_report_cancelled(r)
                or str(bubble or "") in SALES_REPORT_EXCLUDED_INVOICES
                or str(bubble or "") in deleted):
            continue
        # No money received by the end of the period: not a sale yet. The five
        # QUEK JOO HIANG invoices (Denise, 29 Apr) carry only RM0 payments and
        # were replaced in July by real ones dated back to that day. Skipped
        # when the payments lookup failed, which would otherwise empty the
        # report.
        is_overridden = (str(bubble or "") in SALES_REPORT_EP_OVERRIDES
                         or str(bubble or "") in SALES_REPORT_MANUAL_CASES
                         or str(r.get("invoice_number") or "") in SALES_REPORT_EP_OVERRIDES
                         or str(bubble or "") in SALES_REPORT_SALES_ADJUSTMENTS
                         or str(r.get("invoice_number") or "") in SALES_REPORT_SALES_ADJUSTMENTS)
        if not is_overridden and payments and payments.get(str(bubble or ""), zero) <= 0:
            continue
        # Not a solar or EV sale (add-ons, capacitor banks, cleaning...).
        if solar_ev is not None and not is_overridden and str(bubble or "") not in solar_ev:
            continue
        # Sales price: invoice total net of EPP interest, corrected for lucky draw
        # and free robot lines (_sales_report_line_adjustments) and by hand.
        adj_val = (SALES_REPORT_SALES_ADJUSTMENTS.get(str(bubble or "").strip())
                   or SALES_REPORT_SALES_ADJUSTMENTS.get(str(r.get("invoice_number") or "").strip())
                   or "0")
        sales = (Decimal(str(r.get("total_amount") or 0))
                 - Decimal(str(r.get("epp_interest") or 0))
                 + line_adj.get(str(bubble or ""), zero)
                 + Decimal(adj_val))
        # ANS (user, 28 Oct 2026, revised the same day): a fully paid invoice
        # counts at its Original Price -- the total less EPP, lucky-draw prizes
        # added back (invoice_price_basis); one not fully paid yet at its Sales
        # price, the full invoice total with EPP interest included. Fully paid
        # is judged on the full invoice amount, as the customer pays it. Hand
        # adjustments still apply -- except an "add the EPP back" one on an
        # invoice not fully paid, whose total already carries the EPP
        # (SALES_REPORT_EPP_ADDBACKS) -- and SALES_REPORT_CONFIRMED_ANS_PINS
        # keeps months the user checked.
        total = Decimal(str(r.get("total_amount") or 0))
        rec = (price_records.get((str(r.get("invoice_number") or "").strip(),
                                  str(r.get("customer_name") or "").strip().lower()))
               or price_records.get(str(r.get("invoice_number") or "").strip()))
        if rec is not None:
            epp = Decimal(str(getattr(rec, "epp_cost", 0) or 0))
            original = Decimal(str(_ipb.original_price(
                total, epp, getattr(rec, "invoice_date", None),
                getattr(rec, "price_items", None) or [])))
        else:
            epp = Decimal(str(r.get("epp_interest") or 0))
            original = total - epp
        fully_paid = paid_to_date.get(str(bubble or ""), zero) >= total - Decimal("0.5")
        epp_addback = not fully_paid and (str(bubble or "").strip() in SALES_REPORT_EPP_ADDBACKS
                                          or str(r.get("invoice_number") or "").strip() in SALES_REPORT_EPP_ADDBACKS)
        ans = (original if fully_paid else total) + (zero if epp_addback else Decimal(adj_val))
        ans += Decimal(SALES_REPORT_CONFIRMED_ANS_PINS.get(str(bubble or "").strip(), "0"))
        prop = int_mod.classify_property_type(r)
        inv_no = str(r.get("invoice_number") or "").strip()
        panels = (int(r.get("panel_qty") or 0)
                  or panel_counts.get((inv_no, str(r.get("customer_name") or "").strip().lower()))
                  or panel_counts.get(inv_no))
        kind, review = _sales_report_property(r, prop, panels)
        if str(bubble or "") in SALES_REPORT_PROPERTY_OVERRIDES:
            kind, review = SALES_REPORT_PROPERTY_OVERRIDES[str(bubble or "")], None
        mix_key ="Residential" if kind == "RES" else "Commercial"
        prop_counts[mix_key] = prop_counts.get(mix_key, 0) + 1

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
                # Cases whose RES/COM placement deserves a human look.
                "reviews": [],
                "acc_noc": 0, "acc_ans": zero, "collected": zero,
                "ep": zero, "ep_rows": [], "ep_lines": [],
            }

        entry["raw_names"].add(raw)
        if review:
            entry["reviews"].append({
                "invoice": str(r.get("invoice_number") or "").strip(),
                "customer": str(r.get("customer_name") or "").strip(),
                "agent": entry["agent"], "type": kind, "reason": review})

        months = entry["lines"].setdefault(kind, {})
        cell = months.setdefault(when.month, [0, zero])
        cell[0] += 1
        cell[1] += ans
        paid_so_far = paid_to_date.get(str(bubble or ""), zero)
        customer = _case_hover_entry(r.get("customer_name"),
                                     payment_rows.get(str(bubble or "")),
                                     r.get("total_amount"), ans, fully_paid,
                                     (paid_so_far / total * 100) if total > 0 else None)
        if customer:
            entry["custs"].setdefault(kind, {}).setdefault(when.month, []).append(customer)
        if when.month == SALES_REPORT_SPLIT_MONTH:
            split = entry["jun"].setdefault(kind, [zero, zero])
            split[0 if when.day <= SALES_REPORT_SPLIT_DAY else 1] += ans
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
        entry["acc_ans"] += ans
        # One invoice is visited once (seen_bubble above), so its receipts are
        # added once. An invoice nobody has paid simply contributes nothing.
        entry["collected"] += payments.get(str(bubble or ""), zero)
        ep_fixed = (SALES_REPORT_EP_OVERRIDES.get(str(bubble or "").strip())
                    or SALES_REPORT_EP_OVERRIDES.get(str(r.get("invoice_number") or "").strip()))
        # A late entry (paid in one month, keyed in a later one, from August)
        # stays on the Sales Report but earns no EP: no EGA/ESA or Early Bird
        # points from it, in either month (user, 28 Oct 2026).
        # Each case's EP, for the EP point hover: its month, customer, line,
        # the sales it is worked on (total less EPP), the EP and the rule.
        ep_line = {"m": when.month, "n": str(r.get("customer_name") or "").strip(),
                   "k": kind, "s": float(sales)}
        entry["ep_lines"].append(ep_line)
        if str(bubble or "") in late_entries:
            ep_line.update(e=0.0, r="late")
            continue
        # A case on the report's COM line earns EP by the commercial rule -- the
        # first RM40,000 at 100%, the balance at 40%, for invoices dated from the
        # Data page's cutoff (1 May 2026) -- whatever the ERP recorded: it holds
        # most of these jobs as plain "Residential", which calc_ep_points pays at
        # a point per ringgit. A RES-line case keeps the recorded type, and a
        # hand-set EP above still wins.
        ep_prop = "Factory" if kind == "COM" else prop
        ep_val = (Decimal(ep_fixed) if ep_fixed is not None
                  else int_mod.calc_ep_points(sales, ep_prop, r.get("invoice_date"),
                                              int(r.get("panel_qty") or 0)))
        entry["ep_rows"].append((when.strftime("%Y-%m-%d"), ep_val))
        ep_line.update(e=float(ep_val), r="hand" if ep_fixed is not None else "")

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
        oct_threshold = Decimal(str(oct_targets.get(entry["channel"], 1360000.0)))
        nov_threshold = Decimal(str(nov_targets.get(entry["channel"], 1460000.0)))
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
        oct_qualified = esa_ep >= oct_threshold
        # November: the earliest rung wins, as it does on the ESA ladder. Anyone
        # who had cleared October's bar by the end of October already holds the
        # early bird, so they read as an October qualifier rather than as
        # someone still short of November's higher bar.
        ep_by_oct = sum((e for d, e in entry["ep_rows"] if d <= f"{year:04d}-10-31"), zero)
        won_oct = ep_by_oct >= oct_threshold
        nov_qualified = won_oct or esa_ep >= nov_threshold
        # Each label names the month the bar was cleared, with September the
        # earliest (see SALES_REPORT_EARLIEST_QUALIFIER_MONTH).
        rows_ep = list(entry["ep_rows"])
        oct_status = (_early_qualifier_label(rows_ep, oct_threshold, last_month, 10)
                      if oct_qualified else "")
        if not nov_qualified:
            nov_status = ""
        elif won_oct:
            nov_status = _early_qualifier_label(rows_ep, oct_threshold, min(10, last_month), 10)
        else:
            nov_status = _early_qualifier_label(rows_ep, nov_threshold, last_month, 11)
        if not esa_qualified:
            esa_status = ""
        elif esa_month in (esa_ladder or {}):
            # Won on an early-bird rung: named for when that rung's bar was
            # cleared, not simply for the rung's month.
            rung = esa_ladder[esa_month]
            rung = rung[0] if isinstance(rung, (tuple, list)) else rung
            esa_status = _early_qualifier_label(rows_ep, rung, last_month, esa_month)
        else:
            esa_status = _qualifier_label(esa_month)
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
            # Case by case, for the EP point hover (see ep_line above).
            "ep_lines": entry["ep_lines"],
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
            "oct_threshold": float(oct_threshold),
            "oct_qualified": oct_qualified,
            "oct_balance": 0.0 if oct_qualified else float(oct_threshold - esa_ep),
            # "Sept Qualifier" / "Oct Qualifier" / "Nov Qualifier": see above.
            "oct_status": oct_status,
            "nov_threshold": float(nov_threshold),
            "nov_qualified": nov_qualified,
            "nov_balance": 0.0 if nov_qualified else float(nov_threshold - esa_ep),
            "nov_status": nov_status,
            # "Apr Qualifier" and the like, in the printed report's wording.
            "status": _qualifier_label(ega_month) if qualified else "",
            "esa_status": esa_status,
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

    # Every spelling of each agent's name, so the search box finds "Dong
    # Leong Moi" from "Caryn Dong" as well. Sent rather than looked up in the
    # browser so the page and the PDF export filter by the same list.
    for a in out_agents:
        a["aliases"] = agent_names.search_aliases(a.get("agent"))

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
        "oct_targets": oct_targets,
        "nov_targets": nov_targets,
        # One early-bird column at a time: October's until the end of October,
        # November's from 1 November (see _sales_report_early_bird_stage).
        "early_bird": early_stage,
        "show_oct": early_stage == "oct",
        "show_nov": early_stage == "nov",
        "multiplier_in_period": multiplier_in_period,
        "branches": branches,
        "agents": out_agents,
        "totals": {
            "cases": sum(a["acc_noc"] for a in out_agents),
            "sales": sum(a["acc_ans"] for a in out_agents),
            "agents": len(out_agents),
        },
        # Surfaced so the page can say why the RES/COM split looks thin: with
        # no SEDA nem type, referral project type, package wording or customer
        # name tag, a case falls back to Residential. Factory counts as Commercial.
        "property_mix": prop_counts,
        "property_review": sorted(
            (rv for e in agents.values() for rv in e["reviews"]),
            key=lambda rv: (rv["agent"], rv["invoice"])),
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
    if request.args.get("refresh") == "1":
        clear_commission_cache()
    try:
        payload = _sales_report_payload(year, through)
        if payload is None:
            _trigger_background_refresh(year, "outsource")
            return jsonify({"ready": False,
                            "message": "Sales data is still being prepared. Refresh shortly."})
        resp = make_response(jsonify(payload))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
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

    def has_customer(a: dict) -> bool:
        """One of the agent's customers matches -- the names the NOC hovers
        show, compared in the same compact form as the aliases."""
        return any(q in _normalize_search_text((c or {}).get("n") or "")
                   for t in (a.get("types") or [])
                   for m in (t.get("months") or [])
                   for c in (m.get("custs") or []))

    return [a for a in (payload.get("agents") or [])
            if q in _normalize_search_text(a.get("agent") or "")
            or any(q in k for k in (a.get("aliases") or []))
            or has_customer(a)]


def _sales_report_pdf(payload: dict, search: str) -> bytes:
    """Render the sales ledger as a landscape A3 PDF, block for block the same
    as the page: Company-wide Internal/Outsource first, then each branch."""
    import io
    from datetime import datetime
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageBreak, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle)

    MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

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
    # Every NOC column (each month, TOTAL, ACC): a case count, so it is printed
    # narrow and centred, and the room it gives up goes to the RM columns.
    noc_cols = []
    col = 3
    for m in range(1, lm + 1):
        noc_cols.append(col)
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
    # Collected sits between the months and TOTAL, as on the page.
    top += ["COLLECTED\nPAYMENT"]
    sub += [""]
    collected_col = col
    col += 1
    # TOTAL and ACC open the groups below, so their first columns are known.
    total_col, acc_col = col, col + 2
    noc_cols += [total_col, acc_col]
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
    # The early-bird column is October's until the end of October and
    # November's from 1 November; the payload says which, as it does for the page.
    early = "nov" if payload.get("show_nov") else "oct"
    # The award each table's target line names -- the page's awardOnScreen():
    # EGA while its column is up, then the current early bird.
    if show_ega:
        award_name, award_bars = "EGA", targets
    else:
        award_name = f"{'Nov' if early == 'nov' else 'Oct'} Early Bird"
        award_bars = payload.get(f"{early}_targets") or (
            {"Internal": 1200000.0, "Outsource": 1460000.0} if early == "nov"
            else {"Internal": 1000000.0, "Outsource": 1360000.0})
    top += ["UP TO DATE\nEP POINT", ""]
    sub += ["", ""]
    ep_col, oct_col = col, col + 1
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
    # NO/NAME/TYPE, Collected and EP are one heading cell down both header
    # rows. The award columns are not: their name sits in the top row and
    # "Balance To Qualify (...)" in the row below, as in the printed report.
    spans += [("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)),
              ("SPAN", (2, 0), (2, 1)),
              ("SPAN", (collected_col, 0), (collected_col, 1)),
              ("SPAN", (ep_col, 0), (ep_col, 1))]

    # The column groups the heading shows -- NO, NAME, TYPE, each month (its
    # NOC/ANS pair, or June's three), Collected, TOTAL, ACC, the May multiplier
    # block, EP and each award -- as (first, last) column pairs. Each is
    # outlined in black down the whole table.
    month_starts = noc_cols[:lm]
    col_groups = [(0, 0), (1, 1), (2, 2)]
    col_groups += [(start, (month_starts[i + 1] if i + 1 < lm else collected_col) - 1)
                   for i, start in enumerate(month_starts)]
    col_groups += [(collected_col, collected_col), (total_col, total_col + 1),
                   (acc_col, acc_col + 1)]
    if not no_campaign:
        col_groups.append((acc_col + 2, acc_col + 5))
    col_groups += [(c, c) for c in (ep_col, oct_col, ega_col, esa_col) if c is not None]

    def fmt_threshold(value) -> str:
        """"600K", "1.0M", "1.36M" -- the printed report's shorthand for a
        bar, which keeps one decimal on a whole million."""
        n = float(value or 0)
        if n >= 1e6:
            m = f"{n / 1e6:.2f}".rstrip("0")
            return (m + "0" if m.endswith(".") else m) + "M"
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

        head, subhead = list(top), list(sub)
        # Award name on top, "Balance To Qualify (bars ...)" underneath.
        awards = []
        if oct_col is not None:
            if early == "nov":
                awards.append((oct_col, "NOV EARLY BIRD",
                               bars(payload.get("nov_targets")), "30Nov"))
            else:
                awards.append((oct_col, "OCT EARLY BIRD",
                               bars(payload.get("oct_targets")), "30Oct"))
        if ega_col is not None:
            awards.append((ega_col, "EGA - HANOI", bars(payload.get("targets")), "30Jun"))
        if esa_col is not None:
            awards.append((esa_col, "ESA - CHONG QING",
                           bars(payload.get("esa_targets")), "31Dec"))
        for c, name, bar, till in awards:
            head[c] = name
            # Three lines, broken where the printed report breaks them.
            subhead[c] = Paragraph(
                f"Balance To Qualify<br/>({html.escape(bar)} EP<br/>Point Till {till})",
                head_style)
        for c in (ep_col, collected_col):
            head[c] = Paragraph(
                "<br/>".join(html.escape(line) for line in head[c].split("\n")),
                head_style)
        return [head, subhead]

    page_w, page_h = landscape(A3)
    avail = page_w - 24 * mm
    no_w, name_w, type_w = 15.0, 74.0, 20.0
    # Wide enough for the bold "NOC" heading at the largest type (5pt) plus
    # padding; a count of cases never needs more.
    noc_w = 16.0
    # The award columns carry the longest headings, so they take a larger
    # share than an ordinary RM column: num_w each for the rest, AWARD_SHARE
    # times that for each award.
    AWARD_SHARE = 1.4
    award_cols = [c for c in (oct_col, ega_col, esa_col) if c is not None]
    n_num = ncols - 3 - len(noc_cols) - len(award_cols)
    num_w = ((avail - no_w - name_w - type_w - noc_w * len(noc_cols))
             / (n_num + AWARD_SHARE * len(award_cols)))
    award_w = num_w * AWARD_SHARE
    col_widths = [no_w, name_w, type_w] + [
        noc_w if c in noc_cols else award_w if c in award_cols else num_w
        for c in range(3, ncols)]
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
    # The long right-hand headings (see header_rows), matching the other
    # heading cells: bold, black, centred.
    head_style = ParagraphStyle("srHead", fontName="Helvetica-Bold", fontSize=font_size,
                                leading=font_size + 1.2, alignment=1,
                                textColor=colors.black)
    # Heading colours: the lightest blue by default; a column with a colour of
    # its own (TOTAL, ACC, EP, the awards) carries that colour up into its heading.
    HEAD_BLUE = "#EFF6FF"
    AWARD_PINK = "#FBD3E0"
    AWARD_PINK_LIGHT = "#FCE4EF"
    AWARD_PINK_DEEP = "#EFC6E4"

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
                # Collected, between the months and TOTAL.
                ((rm(agent.get("collected")) if agent.get("collected") else "-")
                 if first else ""),
                str(t.get("total_noc", 0)), rm(t.get("total_ans")),
                # ACC is one merged cell down the agent's lines (see the SPANs in
                # block()), so the value sits on the first line of the span.
                str(agent.get("acc_noc", 0)) if first else "",
                rm(agent.get("acc_ans")) if first else "",
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
                rm(agent.get("ep")) if first else "",
                ((agent.get(f"{early}_status") or "Qualifier" if agent.get(f"{early}_qualified")
                  else rm(agent.get(f"{early}_balance"))) if first else ""),
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

    def subtotal_row(members: list[dict], label: str = "SUBTOTAL") -> list:
        row = ["", label, ""]
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
        row += [rm(sum(a.get("collected") or 0 for a in members)),
                str(acc_noc), rm(acc_ans), str(acc_noc), rm(acc_ans)]
        if not no_campaign:
            row += [str(sum(a.get("campaign_cases", 0) for a in members)),
                    rm(sum((a.get("campaign_ans") or 0)
                           + (a.get("campaign_bonus") or 0)
                           for a in members if a.get("bonus_tier"))),
                    "-", "-"]
        # EP, Oct Early Bird, then one blank per award column in the header.
        row += ["", ""]
        row += [""] * ((1 if show_ega else 0) + (1 if show_esa else 0))
        return row

    def block(title: str, members: list[dict], channel: str,
              section_total: list[dict] | None = None,
              branch: str | None = None) -> list:
        """One table. `section_total`, when given, is every agent in the
        section, added up under this table's SUBTOTAL as the Internal +
        Outsource line, in red as on the page."""
        if not members:
            return []
        rows = header_rows(channel)
        for i, a in enumerate(members):
            rows += agent_rows(a, i + 1)
        rows.append(subtotal_row(members))
        sub_r = len(rows) - 1
        if section_total is not None:
            rows.append(subtotal_row(section_total, "TOTAL"))
        style = [
            ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
            ("FONTNAME", (0, 2), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("LEADING", (0, 0), (-1, -1), font_size + 1.2),
            ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor(HEAD_BLUE)),
            ("TEXTCOLOR", (0, 0), (-1, 1), colors.black),
            ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (2, -1), "CENTER"),
            ("ALIGN", (3, 0), (-1, 1), "CENTER"),
            ("ALIGN", (1, 2), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
            ("BACKGROUND", (0, sub_r), (-1, -1), colors.HexColor("#eef1f7")),
            ("FONTNAME", (0, sub_r), (-1, -1), "Helvetica-Bold"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), 1.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
        ] + [("ALIGN", (c, 2), (c, -1), "CENTER") for c in noc_cols] + spans
        # EP and the award columns are centred.
        for c in (ep_col, oct_col, ega_col, esa_col):
            if c is not None:
                style.append(("ALIGN", (c, 2), (c, -1), "CENTER"))
        # Every line is black: a thin grid, a heavier outline round each
        # column group (see col_groups) and round the two heading rows.
        for first_col, last_col in col_groups:
            style.append(("BOX", (first_col, 0), (last_col, -1), 0.8, colors.black))
        style.append(("BOX", (0, 0), (-1, 1), 0.8, colors.black))
        # Column bands, as on the page: TOTAL green, ACC blue, UP TO DATE EP
        # POINT beige, a deeper shade of each on the subtotal row.
        for first_col, last_col, body, subtotal in (
                (total_col, total_col + 1, "#E6F4EA", "#CDE9D5"),
                (acc_col, acc_col + 1, "#E3EEFB", "#C9DDF5"),
                (ep_col, ep_col, "#F5EEDC", "#EBDFC0")):
            style += [
                ("BACKGROUND", (first_col, 0), (last_col, 1), colors.HexColor(body)),
                ("BACKGROUND", (first_col, 2), (last_col, sub_r - 1), colors.HexColor(body)),
                ("BACKGROUND", (first_col, sub_r), (last_col, -1), colors.HexColor(subtotal)),
            ]
        # Award headings, as in the printed report: the name in plain type on
        # a light pink, "Balance To Qualify" in bold on a deeper pink below.
        for c in (oct_col, ega_col, esa_col):
            if c is not None:
                style += [("BACKGROUND", (c, 0), (c, 0), colors.HexColor(AWARD_PINK_LIGHT)),
                          ("FONTNAME", (c, 0), (c, 0), "Helvetica"),
                          ("BACKGROUND", (c, 1), (c, 1), colors.HexColor(AWARD_PINK_DEEP))]
        if section_total is not None:
            red_total = colors.HexColor("#C62828")
            style += [("TEXTCOLOR", (0, -1), (-1, -1), red_total),
                      ("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.black)]
        # Award columns: a qualifier reads red on white; anyone still short
        # shows the balance in black on pink, across the whole merged cell.
        won_red, pink = colors.HexColor("#C62828"), colors.HexColor(AWARD_PINK)

        def award_cell(col: int, row: int, lines: int, won: bool) -> None:
            if won:
                style.append(("TEXTCOLOR", (col, row), (col, row), won_red))
            else:
                style.extend([
                    ("TEXTCOLOR", (col, row), (col, row), colors.black),
                    ("BACKGROUND", (col, row), (col, row + lines - 1), pink),
                ])

        # A month whose customers are all fully paid is shaded #F7C7AC, NOC and
        # ANS alike (June's split pair too) -- allFullyPaid() on the page.
        paid_fill = colors.HexColor("#F7C7AC")
        month_spans = col_groups[3:3 + lm]
        r = 2
        for a in members:
            n_lines = max(1, len(a.get("types") or []))
            for i, t in enumerate((a.get("types") or [])[:n_lines]):
                months = t.get("months") or []
                for m in range(min(lm, len(months))):
                    custs = [c for c in ((months[m] or {}).get("custs") or []) if c]
                    if custs and all(c.get("f") for c in custs):
                        first_col, last_col = month_spans[m]
                        style.append(("BACKGROUND", (first_col, r + i), (last_col, r + i), paid_fill))
            for i in range(n_lines):
                if i == 0:
                    # Per-agent figures are one merged cell down the agent's RES
                    # and COM lines, as on the page: NO, NAME, ACC (which adds
                    # RES and COM together), EP, Collected and the award columns.
                    if n_lines > 1:
                        for c in (0, 1, acc_col, acc_col + 1, ep_col, collected_col,
                                  oct_col, ega_col, esa_col):
                            if c is not None:
                                style.append(("SPAN", (c, r), (c, r + n_lines - 1)))
                    award_cell(oct_col, r, n_lines, bool(a.get(f"{early}_qualified")))
                    if ega_col is not None:
                        award_cell(ega_col, r, n_lines, bool(a.get("qualified")))
                    if esa_col is not None:
                        award_cell(esa_col, r, n_lines, bool(a.get("esa_qualified")))
                r += 1
        target = float(award_bars.get(channel, 0) or 0)
        head = Paragraph(
            # The table's name, with its agent count and target on the line below.
            # A searched file names the branch here, under the table's name,
            # rather than in a heading above it (see the layout below).
            f"<b>{html.escape(title)}</b><br/>"
            + (f"<font size=10><b>{html.escape(branch)} Branch</b></font><br/>" if branch else "")
            + f"{len(members)} agent"
            f"{'' if len(members) == 1 else 's'} &middot; "
            f"{html.escape(award_name)} target RM {target:,.2f} EP",
            ParagraphStyle("blockHead", fontName="Helvetica", fontSize=8, leading=11,
                           textColor=colors.HexColor("#334155"), spaceAfter=3))
        table = Table(rows, colWidths=col_widths, repeatRows=2)
        table.setStyle(TableStyle(style))
        return [head, table, Spacer(1, 8)]

    def customer_list(agents: list[dict]) -> list:
        """The customer list under the page's tables, as on screen: per agent,
        one table per month with cases -- each case under the month the report
        counts it in -- with its System Price and every payment recorded to
        the end of the period. An agent found by a customer's name, not their
        own, lists only the matching customers, as renderCustomerList() does."""
        q = _normalize_search_text(search)
        cell = ParagraphStyle("srCust", fontName="Helvetica", fontSize=7.5, leading=9,
                              textColor=colors.HexColor("#0f172a"))
        month_style = ParagraphStyle("srCustMonth", fontName="Helvetica-Bold", fontSize=8.5,
                                     leading=11, textColor=colors.HexColor("#334155"),
                                     spaceBefore=6, spaceAfter=3, keepWithNext=1)
        import calendar
        month_name = calendar.month_name   # "February", as the page spells it
        widths = [w * avail for w in (0.30, 0.12, 0.12, 0.10, 0.10)]
        out = []
        for a in agents:
            by_customer = not (q in _normalize_search_text(a.get("agent") or "")
                               or any(q in k for k in (a.get("aliases") or [])))
            # Opens like page 1: the report title, then whose list this is.
            out += [PageBreak(),
                    Paragraph(f"CUSTOMER LIST SALES REPORT {as_at}", title_style),
                    Paragraph(html.escape(str(a.get("agent") or "")), section_style)]
            printed = False
            for m in range(1, lm + 1):
                cases = [c for t in (a.get("types") or [])
                         for c in (((t.get("months") or [])[m - 1:m] or [{}])[0].get("custs") or [])
                         if c and (not by_customer
                                   or q in _normalize_search_text(c.get("n") or ""))]
                if not cases:
                    continue
                printed = True
                rows = [["Customer", "System Price", "Payments Amount", "Paid Date",
                         "Created At Date"]]
                style = [
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFF6FF")),
                    ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                    ("ALIGN", (1, 1), (2, -1), "RIGHT"),
                    ("ALIGN", (3, 1), (4, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9DEE5")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                ]
                sys_total = paid_total = 0.0
                for c in cases:
                    # Oldest first, as on the page.
                    pays = sorted(c.get("p") or [], key=lambda p: str(p.get("d") or ""))
                    first = len(rows)
                    sys_total += float(c.get("s") or 0)
                    lead = [Paragraph(html.escape(str(c.get("n") or "")), cell),
                            rm(c.get("s")) if c.get("s") else "-"]
                    if not pays:
                        rows.append(lead + ["no payment recorded in this period", "", ""])
                        style.append(("SPAN", (2, first), (4, first)))
                        style.append(("ALIGN", (2, first), (4, first), "CENTER"))
                        continue
                    for i, p in enumerate(pays):
                        paid_total += float(p.get("a") or 0)
                        rows.append((lead if i == 0 else ["", ""])
                                    + [rm(p.get("a")), p.get("d") or "-", p.get("m") or "-"])
                    if len(pays) > 1:
                        style.append(("SPAN", (0, first), (0, len(rows) - 1)))
                        style.append(("SPAN", (1, first), (1, len(rows) - 1)))
                rows.append(["TOTAL", rm(sys_total), rm(paid_total), "", ""])
                style += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 7.5),
                          ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f1f5f9")),
                          ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#cbd5e1"))]
                n_pay = sum(len(c.get("p") or []) for c in cases)
                table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle(style))
                out += [Paragraph(f"{month_name[m]} {year} &middot; "
                                  f"{plural(len(cases), 'customer')} &middot; "
                                  f"{plural(n_pay, 'payment')}", month_style),
                        table, Spacer(1, 6)]
            if not printed:
                out.append(Paragraph("No cases in this period.", meta_style))
        return out

    # Leading set explicitly: the 12pt default is shorter than the 15pt type,
    # so whatever sat under the title was drawn into it.
    title_style = ParagraphStyle("srTitle", fontName="Helvetica-Bold", fontSize=15,
                                 leading=19, textColor=colors.HexColor("#0f172a"),
                                 spaceAfter=2)
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
        # EGA up to June; from July "ESA Qualified", counting the ESA column's
        # qualifiers against its own bars -- as the page's renderCards() does.
        if show_ega:
            qual_label, qual_bars = "Qualified · EGA", targets
            qual = sum(1 for a in shown if a.get("qualified"))
        else:
            qual_label = "ESA Qualified"
            qual_bars = payload.get("esa_targets") or {"Internal": 1300000.0,
                                                       "Outsource": 1560000.0}
            qual = sum(1 for a in shown if a.get("esa_qualified"))
        cards = [
            ("Total Cases", str(sum(a.get("acc_noc", 0) for a in shown)),
             plural(len(shown), "agent")),
            ("Total Net Sales", fmt_compact(sum(a.get("acc_ans", 0) for a in shown)), ""),
            ("Internal", fmt_compact(sum(a.get("acc_ans", 0) for a in internal)),
             f"{plural(len(internal), 'agent')} · "
             f"{plural(sum(a.get('acc_noc', 0) for a in internal), 'case')}"),
            ("Outsource", fmt_compact(sum(a.get("acc_ans", 0) for a in outsource)),
             f"{plural(len(outsource), 'agent')} · "
             f"{plural(sum(a.get('acc_noc', 0) for a in outsource), 'case')}"),
            (qual_label, f"{qual} / {len(shown)}",
             f"RM {rm(qual_bars.get('Internal'))} internal · "
             f"RM {rm(qual_bars.get('Outsource'))} outsource"),
        ]
        # Each card is its own light-grey box, no border, all-black text, with a
        # narrow blank column between neighbours.
        gap = 6.0
        width = (page_w - 24 * mm - gap * (len(cards) - 1)) / len(cards)
        black = colors.black
        card_fill = colors.HexColor("#F1F5F9")
        # The divider above the branch lines, a shade darker than the fill.
        card_rule = colors.HexColor("#CBD5E1")

        def branch_breakdown(members: list[dict]):
            """The page's branchBreakdown(): one line per branch -- name, agents
            and cases, sales -- skipping empty branches, with anyone outside
            the listed branches under "Other" so the lines add up to the card."""
            branches = list(payload.get("branches") or [])
            groups = [(b, [a for a in members if a.get("branch") == b]) for b in branches]
            other = [a for a in members if a.get("branch") not in branches]
            if other:
                groups.append(("Other", other))
            lines = [[b, f"{plural(len(ag), 'agent')} · "
                         f"{plural(sum(a.get('acc_noc', 0) for a in ag), 'case')}",
                      fmt_compact(sum(a.get("acc_ans", 0) for a in ag))]
                     for b, ag in groups if ag]
            if not lines:
                return ""
            inner = width - 16
            sub = Table(lines, colWidths=[34, inner - 34 - 48, 48])
            sub.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 0), (-1, -1), black),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.4, card_rule),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                ("TOPPADDING", (0, 0), (-1, 0), 4),
            ]))
            return sub

        breakdown = ["", "", branch_breakdown(internal), branch_breakdown(outsource), ""]

        def card_body(card: tuple, extra) -> Table:
            """One card's label, figure, note and (Internal/Outsource only) branch
            lines, as a block of its own so the card can be centred in its box."""
            rows = [[card[0]], [card[1]], [card[2]]] + ([[extra]] if extra else [])
            body = Table(rows, colWidths=[width - 16])
            body.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (0, 0), "Helvetica"),
                ("FONTSIZE", (0, 0), (0, 0), 6.5),
                ("FONTNAME", (0, 1), (0, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (0, 1), 12),
                ("FONTNAME", (0, 2), (0, 2), "Helvetica"),
                ("FONTSIZE", (0, 2), (0, 2), 6),
                ("TEXTCOLOR", (0, 0), (-1, -1), black),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 1), (0, 1), 5),
                ("TOPPADDING", (0, 3), (0, 3), 4),
            ]))
            return body

        # One row of boxes with a narrow blank column between neighbours. The
        # Internal and Outsource cards are the tallest and set the row height;
        # the other three are centred vertically inside it.
        cells, widths = [], []
        for i, card in enumerate(cards):
            if i:
                cells.append("")
                widths.append(gap)
            cells.append(card_body(card, breakdown[i]))
            widths.append(width)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
        for i, c in enumerate(range(0, len(cells), 2)):
            style.append(("BACKGROUND", (c, 0), (c, 0), card_fill))
            # Internal and Outsource stay top-aligned so their headings line
            # up even when a search leaves one with fewer branch lines.
            if breakdown[i]:
                style.append(("VALIGN", (c, 0), (c, 0), "TOP"))
        table = Table([cells], colWidths=widths)
        table.setStyle(TableStyle(style))
        return table

    # "ETERNALGY SALES REPORT AS AT Oct 2026 (UPDATED 9 Oct 26)": the month the
    # report runs to, then the day the file was made. The day is built by hand
    # because strftime's no-padding flag differs between Windows and macOS.
    today = datetime.now()
    updated = f"{today.day} {today.strftime('%b %y')}"
    as_at = f"AS AT {MONTH_ABBR[lm - 1].title()} {year} (UPDATED {updated})"
    story = [Paragraph(f"ETERNALGY SALES REPORT {as_at}", title_style)]
    # No generated/totals line under the title: the cards below carry the
    # totals. A search still says so, or a filtered file reads as the company:
    # by the full names of the agents it found ("CHAN JIA WEI", not the
    # "jia wei" typed), or by the search itself when it found nobody.
    if search:
        found = list(dict.fromkeys(str(a.get("agent") or "") for a in shown))
        story.append(Paragraph(
            html.escape(", ".join(found)) if found
            else f"Filtered to &ldquo;{html.escape(search)}&rdquo;",
            section_style if found else meta_style))
    story += [Spacer(1, 6), summary_cards(), Spacer(1, 12)]

    def pick(channel, branch):
        return [a for a in shown if a.get("channel") == channel
                and (branch is None or a.get("branch") == branch)]

    if not shown:
        story.append(Paragraph("No agent matches the current filter.", meta_style))
    else:
        # Page layout: company-wide Internal on the first page under the cards,
        # company-wide Outsource on its own page, then one page per branch,
        # each opening on its branch heading. The company-wide tables carry no
        # section heading of their own. A searched file holds a handful of
        # rows, so its company-wide and branch tables run on together on one
        # page instead of leaving most of every page empty.
        def new_page(heading: str | None = None) -> None:
            story.append(Spacer(1, 10) if search else PageBreak())
            if heading:
                story.append(Paragraph(heading, section_style))

        company_in, company_out = pick("Internal", None), pick("Outsource", None)
        company_all = company_in + company_out
        story.extend(block("Internal Sales Agent Report", company_in, "Internal",
                           None if company_out else company_all))
        if company_out:
            if company_in:
                new_page()
            story.extend(block("Outsource Sales Agent", company_out, "Outsource",
                               company_all))
        first_branch = True
        for branch in (payload.get("branches") or []):
            internal, outsource = pick("Internal", branch), pick("Outsource", branch)
            if not internal and not outsource:
                continue
            # A branch's Internal and Outsource tables share its page, closing
            # on the Internal + Outsource total. The whole-company file opens
            # the branch pages once with "BRANCH SALES REPORT", then heads each
            # page with its branch; a searched file names the branch under
            # each table's own title instead.
            if search:
                new_page()
                label = str(branch)
            else:
                story.append(PageBreak())
                if first_branch:
                    story.append(Paragraph("BRANCH SALES REPORT", title_style))
                story.append(Paragraph(f"{html.escape(str(branch))} Branch", section_style))
                label = None
            first_branch = False
            both = internal + outsource
            story.extend(block("Internal Sales Agent Report", internal, "Internal",
                               None if outsource else both, branch=label))
            story.extend(block("Outsource Sales Agent", outsource, "Outsource", both,
                               branch=label))

        # The page's customer list follows a search: one table per month for
        # each agent found, January to the reporting month. Not printed for
        # the whole company, which would run to dozens of pages.
        if search:
            story.extend(customer_list(shown))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(12 * mm, 8 * mm,
                          "Template prepared by HR, Autogenerated in "
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


# The rates and rules behind every commission figure are read once per process
# and then held (basic_commission_rates caches the table; the commission
# bundles and response bodies cache what was built from it). A save made
# through THIS server clears all of that. A save made anywhere else -- the
# installed app on another PC, a second copy of the dashboard, a direct edit
# -- lands in the shared table and this server never hears of it, so the
# report kept paying the old rule until someone restarted it. A cheap
# fingerprint of the table, checked at most every _RULES_CHECK_SECONDS,
# closes that gap.
_RULES_CHECK_SECONDS = 20
_rules_fingerprint = {"value": None, "checked": 0.0}
_rules_fingerprint_lock = threading.Lock()


def _data_page_rules_fingerprint():
    with db._connect() as conn:
        row = conn.execute(
            """SELECT (SELECT COUNT(*) FROM commission_rates) AS n,
                      (SELECT MAX(updated_at) FROM commission_rates) AS t,
                      (SELECT COUNT(*) FROM agent_roles) AS rn,
                      (SELECT MAX(updated_at) FROM agent_roles) AS rt""").fetchone()
    return tuple(str(row[k]) for k in ("n", "t", "rn", "rt"))


def _sync_data_page_rules() -> None:
    """Rebuild the commission figures when the Data page rules have changed
    since this server last looked, whoever changed them."""
    now = time.time()
    with _rules_fingerprint_lock:
        if now - _rules_fingerprint["checked"] < _RULES_CHECK_SECONDS:
            return
        _rules_fingerprint["checked"] = now
    try:
        current = _data_page_rules_fingerprint()
    except Exception:
        return            # the table is unreachable; serve what we have
    with _rules_fingerprint_lock:
        previous = _rules_fingerprint["value"]
        _rules_fingerprint["value"] = current
    if previous is not None and previous != current:
        _log("[RULES SYNC] Data page rates or roles changed since the last check; rebuilding commissions.")
        _rates_module().reset_cache()
        try:
            agent_names.reset_cache()
        except Exception:
            pass
        clear_commission_cache()


def _note_rules_saved_here() -> None:
    """A save through this server has already cleared everything; make the
    next check take the new state as its baseline instead of rebuilding a
    second time."""
    with _rules_fingerprint_lock:
        _rules_fingerprint["value"] = None
        _rules_fingerprint["checked"] = 0.0


def _build_commission_payload(year: int, month: int, agent_type: str):
    """Assemble one month from cached yearly bundles. Returns None when
    those bundles are not ready yet so the request can return in time."""
    cached = get_cached_data(year, agent_type)
    if not cached or not cached.get("invoice_dates"):
        _trigger_background_refresh(year, agent_type)
        return None
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
        _int_ega_raw_for_ep = cached.get('ega_raw') if (cached and 'ega_raw' in cached) else None
        if not _int_ega_raw_for_ep:
            _int_ega_raw_for_ep = build_commission_pack.fetch_internal_ega_raw(year)
            if cached:
                cached['ega_raw'] = _int_ega_raw_for_ep
                save_disk_cache()
        anp_rows = _apply_anp_agent_display_names(_anp_rows_with_ep(
            int_customer_anp.get(month, []), int_anp_detail_filtered, _int_ega_raw_for_ep,
            sys.modules.get("int_ega_esa"), has_accumulated=False))
        anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Total Amount", "EP Point", "Commission Price", "Clawback"]

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

        pkg_i = (basic_nfp_headers.index("Property Type") if "Property Type" in basic_nfp_headers
                 else (basic_nfp_headers.index("Package Type") if "Package Type" in basic_nfp_headers else -1))
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
                # Read off the lines as the payout approvals re-date them,
                # so an approved invoice tests as a balance in its month.
                "payout_policies": _build_payout_policies(
                    build_commission_pack.apply_payout_approvals(
                        int_basic_lines, [], {}, year, "internal")[0],
                    build_commission_pack.as_at_ym(year, month)),
                # Payout Stage "Special Case": deposit paid, not yet 100%.
                "special_case": _special_case_stage_or_empty(
                    basic_nfp_headers, int_basic_lines, int_nfp_by_inv_all, year, month,
                    "internal"),
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

        _out_ega_raw_for_ep = cached.get('ega_raw') if (cached and 'ega_raw' in cached) else None
        if not _out_ega_raw_for_ep:
            _out_ega_raw_for_ep = build_commission_pack.fetch_outsource_ega_raw(year)
            if cached:
                cached['ega_raw'] = _out_ega_raw_for_ep
                save_disk_cache()
        out_anp_rows = _apply_anp_agent_display_names(_anp_rows_with_ep(
            build_anp_customer_rows_custom(
                anp_summary=out_anp_summary,
                anp_detail=out_anp_detail_filtered,
                invoice_dates_map=invoice_dates_map,
                month=month,
                is_internal=False
            ), out_anp_detail_filtered, _out_ega_raw_for_ep,
            sys.modules.get("out_ega_esa"), has_accumulated=True))

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

        pkg_i = (basic_nfp_headers.index("Property Type") if "Property Type" in basic_nfp_headers
                 else (basic_nfp_headers.index("Package Type") if "Package Type" in basic_nfp_headers else -1))
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

        anp_headers = ["Agent", "Customer", "Invoice Date", "1st Payment Date", "Package Type", "Total Amount", "EP Point", "Commission Price", "Clawback"]

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
                # Read off the lines as the payout approvals re-date them,
                # so an approved invoice tests as a balance in its month.
                "payout_policies": _build_payout_policies(
                    build_commission_pack.apply_payout_approvals(
                        out_basic_lines, [], {}, year, "outsource")[0],
                    build_commission_pack.as_at_ym(year, month)),
                # Payout Stage "Special Case": deposit paid, not yet 100%.
                "special_case": _special_case_stage_or_empty(
                    basic_nfp_headers, out_basic_lines, out_nfp_by_inv_all, year, month,
                    "outsource"),
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
        "agent_ics": agent_names.get_ic_map(),
    }
    set_cached_commission_response(year, month, agent_type, payload)
    return payload


@app.route("/api/commission")
@login_required
def get_commission():
    _sync_data_page_rules()
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
            _refresh_system_details(cached_payload, year, agent_type)
            return jsonify(_restrict_commission_sections(_decorate_commission_payload(cached_payload, year, month)))

        payload = _commission_payload_within_seconds(year, month, agent_type)
        if payload is None:
            return jsonify({
                "ready": False,
                "message": "Commission data is still being prepared. Refresh shortly.",
            })
        return jsonify(_restrict_commission_sections(_decorate_commission_payload(payload, year, month)))


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
        import contextlib
        import io
        import uuid
        dashboard_data_dir = CURRENT_DIR / "data"
        dashboard_data_dir.mkdir(parents=True, exist_ok=True)
        # One file per request: two people opening the same month at once
        # used to write the same Commission_Pack_<y>_<m>.pdf over each other.
        # The Basic & NFP page asks for its own report (build_basic_nfp_pdf);
        # every other page still gets the company-wide pack.
        section = (request.args.get("section") or "").strip().lower()
        section = "basic_nfp" if section == "basic_nfp" else ""
        stem = f"Basic_NFP_Commission_{year}_{month}" if section else f"Commission_Pack_{year}_{month}"
        file_path = dashboard_data_dir / f"{stem}_{uuid.uuid4().hex[:8]}.pdf"

        # Built from the dashboard's last refresh, as Download Excel is
        # (query_replay.py): the figures match the screen, and it takes ~7s
        # instead of ~33s (minutes when the database is busy) re-downloading
        # every invoice. A question the refresh never asked still goes live.
        replay = bool(_data_cache.get("sql_replay"))
        if replay:
            try:
                query_replay.restore(_data_cache["sql_replay"])
            except Exception:
                replay = False
        started = time.time()
        try:
            with (query_replay.replaying() if replay else contextlib.nullcontext()):
                build_commission_pack.build_pdf(year, file_path, month=month,
                                                sections=[section] if section else None)
            data = file_path.read_bytes()
        finally:
            try:
                file_path.unlink()
            except OSError:
                pass
        _log(f"[PDF] {year}-{month:02d} {section or 'full pack'} built in {time.time() - started:.1f}s"
             + (f" from the dashboard's last refresh (query record: {query_replay.stats()})." if replay else " (live)."))

        return send_file(
            io.BytesIO(data),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{stem}.pdf"
        )
    except Exception as e:
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


_EXCEL_SECTION_FILE_NAMES = {
    "basic_nfp": "Basic_NFP_Commission",
    "anp": "ANP_Commission",
    "ega_esa": "EGA_ESA_Award",
    "production_bonus": "Production_Bonus",
    "monthly_contest": "Monthly_Contest",
}


def _build_excel_download(year: int, month: int, section: str):
    """Write the Download Excel file and return (path, file stem)."""
    dashboard_data_dir = CURRENT_DIR / "data"
    dashboard_data_dir.mkdir(parents=True, exist_ok=True)
    file_stem = (f"{_EXCEL_SECTION_FILE_NAMES[section]}_{year}_{month}" if section
                 else f"Commission_Pack_{year}_{month}")
    file_path = dashboard_data_dir / f"{file_stem}.xlsx"

    # The pack asks the database the same questions the dashboard's last
    # refresh did. Answering those from that refresh's record
    # (query_replay.py) skips downloading every invoice again, so the
    # file carries exactly the figures on screen; a question the refresh
    # never asked still goes to the database.
    import contextlib
    replay = bool(_data_cache.get("sql_replay"))
    if replay:
        try:
            query_replay.restore(_data_cache["sql_replay"])
        except Exception:
            replay = False
    with (query_replay.replaying() if replay else contextlib.nullcontext()):
        build_commission_pack.build_pdf(year, file_path, month=month,
                                        sections=[section] if section else None)
    if replay:
        _log(f"[EXCEL] {year}-{month:02d} {section or 'full pack'} built from the dashboard's "
             f"last refresh (query record: {query_replay.stats()}).")
    return file_path, file_stem


@app.route("/api/download/excel-pdf")
@login_required
def download_excel_pdf():
    """Download PDF: the Download Excel file, page for page, as a PDF
    (user, 2026-10-23). Download Report stays the summary report."""
    year = request.args.get("year", 2026, type=int)
    month = request.args.get("month", 5, type=int)
    section = (request.args.get("section") or "").strip().lower()
    if section not in _EXCEL_SECTION_FILE_NAMES:
        section = ""
    try:
        import io
        import uuid
        xlsx_path, file_stem = _build_excel_download(year, month, section)
        pdf_path = xlsx_path.with_name(f"{file_stem}_{uuid.uuid4().hex[:8]}.pdf")
        import calendar
        month_name = calendar.month_name[month]
        section_name = _EXCEL_SECTION_FILE_NAMES.get(section, "Commission_Pack").replace("_", " ")
        if section == "basic_nfp":
            section_name = "Basic & Net Floor Price Commission"
        try:
            build_commission_pack.xlsx_to_pdf(xlsx_path, pdf_path, f"{month_name} {year} - {section_name}")
            data = pdf_path.read_bytes()
        finally:
            try:
                pdf_path.unlink()
            except OSError:
                pass
        return send_file(io.BytesIO(data), mimetype="application/pdf",
                         as_attachment=True, download_name=f"{file_stem}.pdf")
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
    # The Commission Section on screen; the file then holds that section's
    # sheets only. No section (or an unknown one) keeps the full pack.
    section = (request.args.get("section") or "").strip().lower()
    if section not in _EXCEL_SECTION_FILE_NAMES:
        section = ""

    try:
        file_path, file_stem = _build_excel_download(year, month, section)

        return send_file(
            str(file_path),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"{file_stem}.xlsx"
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
        # rowKind keeps a Basic assignment and an NFP assignment for the
        # same customer as two records. Keying only on agent+customer used
        # to overwrite the first when the second was saved.
        return (str(c.get("agent") or "").strip().lower(),
                str(c.get("customer") or "").strip().lower(),
                str(c.get("rowKind") or "all").strip().lower())

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


@app.route("/api/payout-approvals", methods=["POST"])
@admin_required
def payout_approvals_api():
    """Tick (or untick) a Special Case customer on the Basic & NFP report.

    Ticking approves the customer's invoices for the month on screen: their
    commission is paid there as a balance and never again at 75% or 100%
    (build_commission_pack.apply_payout_approvals). Every month's figures
    depend on that, so the whole commission cache is rebuilt.
    """
    req = request.json or {}
    agent_type = str(req.get("agent_type") or "").strip().lower()
    invoices = [str(i).strip() for i in (req.get("invoices") or []) if str(i).strip()]
    try:
        year, month = int(req.get("year")), int(req.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year and month are required"}), 400
    if agent_type not in ("internal", "outsource") or not invoices:
        return jsonify({"error": "agent_type and invoices are required"}), 400
    approved = bool(req.get("approved"))
    # "full" (the whole commission) or "advance" (the RM 300 advance alone,
    # for a deposit still below the advance trigger).
    kind = str(req.get("kind") or "full").strip().lower()
    if kind not in ("full", "advance"):
        return jsonify({"error": "kind must be 'full' or 'advance'"}), 400
    try:
        for inv in invoices:
            db.set_payout_approval(agent_type, inv, str(req.get("agent") or ""),
                                   str(req.get("customer") or ""), str(year), str(month),
                                   approved, auth.current_user()["username"], kind=kind)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log("[PAYOUT APPROVAL SAVE ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        build_commission_pack.clear_payout_approvals()
        clear_commission_cache()
    return jsonify({"status": "success", "approved": approved, "invoices": invoices})


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


def _anp_rows_with_ep(rows: list, anp_detail: list, ega_raw, ega_mod,
                      has_accumulated: bool) -> list:
    """ANP customer rows as Agent, Customer, Invoice Date, 1st Payment Date,
    Package Type, Total Amount, EP Point, Commission Price, Clawback.

    EP Point is the EGA/ESA EP of the customer's invoices that month: sales
    price (total - EPP) through the award module's calc_ep_points(), or the
    figure set by hand in SALES_REPORT_EP_OVERRIDES -- the same per-invoice EP
    the EGA/ESA page and Sales Report use (without the campaign bonus, which is
    not an invoice's). The internal rows come from build_commission_pack with
    no Accumulated Total column (commission, then clawback); the outsource rows
    carry one before the commission, which is dropped (has_accumulated)."""
    from collections import defaultdict as _dd
    from decimal import Decimal as _D, ROUND_HALF_UP as _RH
    title = build_commission_pack.to_title_case

    ep_by_inv = _ega_ep_by_invoice(ega_raw, ega_mod)

    invs_by_cust: dict = _dd(list)
    for d in anp_detail or []:
        key = (title(str(d.get("agent_name") or "").strip()).lower(),
               title(str(d.get("customer_name") or "").strip()).lower())
        inv = str(d.get("invoice_number") or "").strip()
        if inv and inv not in invs_by_cust[key]:
            invs_by_cust[key].append(inv)

    out, agent = [], ""
    for row in rows:
        if not row:
            out.append(row)
            continue
        row = list(row)
        if str(row[0] or "").strip():
            agent = str(row[0]).strip()
        key = (title(agent).lower(), title(str(row[1] or "").strip()).lower())
        eps = [ep_by_inv[i] for i in invs_by_cust.get(key, []) if i in ep_by_inv]
        ep_str = f"{sum(eps, _D('0')):,.2f}" if eps else "-"
        tail = row[7:9] if has_accumulated else row[6:8]
        out.append(row[:6] + [ep_str] + tail)
    return out


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
        _note_rules_saved_here()
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
        agent_names.reset_cache()
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


def _role_matches_agent(row: dict, agent: str) -> bool:
    """True when `agent` is this hierarchy row's eeAdmin name, full name, or
    any nickname part — or resolves to the same canonical full name. The slip
    displays the canonical full name, so a save keyed only on row['agent']
    would miss Carol Siow / Siow Sio Chui and create a duplicate row."""
    if row.get("hidden"):
        return False
    target = agent_names.normalize_key(agent)
    if not target:
        return False
    want = {target, agent_names.normalize_key(agent_names.resolve(agent) or agent)}
    want.discard("")
    for alias in agent_names._role_aliases(row):
        key = agent_names.normalize_key(alias)
        if key in want:
            return True
        if agent_names.normalize_key(agent_names.resolve(alias) or alias) in want:
            return True
    return False


@app.route("/api/agent-ic", methods=["POST"])
@login_required
def agent_ic_api():
    """Set one agent's IC No, so it can be corrected straight from the Monthly
    Commission Slip instead of only from the Data page's roles table.

    save_agent_roles() replaces the whole table, so this reads every row back,
    edits the matching rows, and writes the full set again — anything else
    would wipe the rest of the hierarchy. IC is an identity field, so every
    non-hidden period for the same person is updated together; otherwise a
    later row with the number would still leave an earlier blank row winning
    an exact-name find().
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
        matched = [r for r in rows if _role_matches_agent(r, agent)]
        if matched:
            for target in matched:
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
        agent_names.reset_cache()
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
        agent_names.reset_cache()
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
    if "MAR" in sn:
        months.add(3)
    if "SEPT" in sn:
        months.add(9)
    return [f"{year:04d}-{m:02d}" for m in sorted(months)]


def _nfp_price_rows(parsed, nfp):
    """parse_schedule_workbook_rows() output -> (nfp_prices rows, one per month a
    sheet covers; per-sheet summary for the preview)."""
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
            row = {k: r.get(k) for k in ("panel_rating", "table_no", "panels", "final_price",
                                          "final_with_tng", "package_price", "tng_rebate",
                                          "inverter_type", "power_system", "source_sheet",
                                          "promo_price", "promo_from", "promo_to")}
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
    return rows, summary


# The finance Google Sheet the Net Floor Price list must always follow (user,
# 27 Oct 2026). "Anyone with the link" can view it, so its xlsx export needs
# no sign-in.
NFP_GOOGLE_SHEET_ID = "1SeT2d8nh3xUZMo_WTtjyK0ENTu2ZPM-aEoUt6xJXusg"
NFP_GOOGLE_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{NFP_GOOGLE_SHEET_ID}/edit"
NFP_GOOGLE_SYNC_LABEL = "Google Sheet sync"
_NFP_SHEET_CHECK_TTL = 24 * 3600
_NFP_SHEET_CHECK: dict = {}
_NFP_SHEET_CHECK_LOCK = threading.Lock()

# Fields compared between the sheet and nfp_prices; a row is keyed on these.
_NFP_ROW_KEY = ("month", "panel_rating", "table_no", "inverter_type", "power_system", "panels")
_NFP_ROW_VALUES = ("final_price", "final_with_tng", "package_price", "tng_rebate",
                   "promo_price", "promo_from", "promo_to")


def _nfp_google_sheet_rows():
    """Download the Google Sheet and parse it like an upload -> (rows, summary)."""
    import urllib.request
    url = (f"https://docs.google.com/spreadsheets/d/{NFP_GOOGLE_SHEET_ID}"
           f"/export?format=xlsx")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data.startswith(b"PK"):
        raise RuntimeError("The Google Sheet did not download as a spreadsheet -- "
                           "check that it is still shared as 'Anyone with the link'.")
    nfp = _nfp_module()
    tmp_path = Path(tempfile.mkstemp(suffix=".xlsx")[1])
    try:
        tmp_path.write_bytes(data)
        parsed = nfp.parse_schedule_workbook_rows(tmp_path, source_label=NFP_GOOGLE_SYNC_LABEL)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    return _nfp_price_rows(parsed, nfp)


def _nfp_sheet_diff(rows) -> dict:
    """How the sheet's rows differ from nfp_prices, for the months the sheet has.
    Months only stored (not on the sheet) are reported but never touched."""
    def norm(v):
        if v in (None, ""):
            return None
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return str(v)[:10]

    def key(r):
        return tuple(r.get(k) for k in _NFP_ROW_KEY)

    months = sorted({r["month"] for r in rows})
    stored_all = db.list_nfp_prices()
    stored = {key(r): r for r in stored_all if r["month"] in months}
    sheet = {key(r): r for r in rows}
    changed, added, removed = [], [], []
    for k, s in sheet.items():
        d = stored.get(k)
        if d is None:
            added.append(k)
            continue
        fields = [f for f in _NFP_ROW_VALUES if norm(s.get(f)) != norm(d.get(f))]
        diff = {f: {"sheet": norm(s.get(f)), "stored": norm(d.get(f))} for f in fields}
        # The columns the Data page shows must match the sheet's too, not only
        # the prices the calculation reads.
        s_cols = [c[0] for c in json.loads(s.get("columns_json") or "[]")]
        d_cols = [c[0] for c in json.loads(d.get("columns_json") or "[]")]
        if (json.loads(s.get("columns_json") or "[]") != json.loads(d.get("columns_json") or "[]")):
            missing = [c for c in s_cols if c not in d_cols]
            diff["columns"] = {"sheet": ", ".join(missing) if missing else "the sheet's columns",
                               "stored": f"{len(d_cols)} columns"}
        if diff:
            changed.append({"month": k[0], "rating": k[1], "inverter": k[3], "system": k[4],
                            "panels": k[5], "fields": diff})
    removed = [k for k in stored if k not in sheet]
    by_month: dict = {}
    for c in changed:
        by_month[c["month"]] = by_month.get(c["month"], 0) + 1
    for k in added + removed:
        by_month[k[0]] = by_month.get(k[0], 0) + 1
    return {
        "months": months,
        "changed": len(changed), "added": len(added), "removed": len(removed),
        "by_month": dict(sorted(by_month.items())),
        "examples": changed[:25],
        "stored_only_months": sorted({r["month"] for r in stored_all} - set(months)),
        "in_sync": not (changed or added or removed),
    }


def _nfp_sheet_check(force: bool = False) -> dict:
    """The daily sheet-vs-stored check, cached for a day so opening the Data
    page does not download the sheet every time."""
    with _NFP_SHEET_CHECK_LOCK:
        fresh = (_NFP_SHEET_CHECK.get("ts") or 0) > time.time() - _NFP_SHEET_CHECK_TTL
        if fresh and not force:
            return dict(_NFP_SHEET_CHECK)
        try:
            rows, _summary = _nfp_google_sheet_rows()
            result = _nfp_sheet_diff(rows)
            result["error"] = None
        except Exception as e:
            _log("[NFP SHEET CHECK ERROR]\n" + traceback.format_exc())
            result = {"error": str(e), "in_sync": None}
        result["ts"] = time.time()
        result["checked_at"] = time.strftime("%d %b %Y, %H:%M")
        result["sheet_url"] = NFP_GOOGLE_SHEET_URL
        _NFP_SHEET_CHECK.clear()
        _NFP_SHEET_CHECK.update(result)
        return dict(result)


@app.route("/api/nfp-prices/google-check")
@login_required
def nfp_prices_google_check():
    """Whether the stored Net Floor Price list still matches the Google Sheet."""
    return jsonify(_nfp_sheet_check(force=request.args.get("refresh") == "1"))


@app.route("/api/nfp-prices/google-sync", methods=["POST"])
@login_required
def nfp_prices_google_sync():
    """Preview replacing the stored list with the Google Sheet's. Confirming goes
    through /api/nfp-prices/upload?mode=confirm with the returned token."""
    user = auth.current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    try:
        rows, summary = _nfp_google_sheet_rows()
        if not rows:
            return jsonify({"error": "No price tables recognized in the Google Sheet."}), 400
        diff = _nfp_sheet_diff(rows)
        months_all = diff["months"]
        token = uuid.uuid4().hex
        _NFP_UPLOAD_PENDING[token] = {"rows": rows, "months": months_all, "ts": time.time(),
                                      "google": True}
        for k in [k for k, v in _NFP_UPLOAD_PENDING.items() if time.time() - v["ts"] > 1800]:
            _NFP_UPLOAD_PENDING.pop(k, None)
        existing = [p for p in db.list_nfp_prices() if p["month"] in months_all]
        return jsonify({"token": token, "summary": summary, "months": months_all,
                        "total_rows": len(rows), "replaces": len(existing), "diff": diff,
                        "sheet_url": NFP_GOOGLE_SHEET_URL})
    except Exception as e:
        _log("[NFP GOOGLE SYNC ERROR]\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500


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
            by = (f"{NFP_GOOGLE_SYNC_LABEL} ({user['username']})" if pending.get("google")
                  else user["username"])
            replaced = db.replace_nfp_prices_for_months(pending["months"], pending["rows"], by)
            try:
                _nfp_module().reset_db_prices_cache()
            except Exception:
                pass
            # The stored list just changed, so yesterday's sheet check is stale.
            with _NFP_SHEET_CHECK_LOCK:
                _NFP_SHEET_CHECK.clear()
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

        rows, summary = _nfp_price_rows(parsed, nfp)

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
                        "job_type": bcr.normalize_job_type(r.get("job_type")) or bcr.JOB_SOLAR,
                        "ev_type": bcr.normalize_ev_type(r.get("ev_type")),
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
                ("Internal", "Executive", "", "", bcr.JOB_SOLAR, ""),
                ("Internal", "Senior", "", "", bcr.JOB_SOLAR, ""),
                ("Outsource", "OUM", "", "", bcr.JOB_SOLAR, ""),
                ("Outsource", "OSA/OSA1", "", "", bcr.JOB_SOLAR, ""),
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
                jt = bcr.normalize_job_type(r.get("job_type")) or bcr.JOB_SOLAR
                et = bcr.normalize_ev_type(r.get("ev_type")) if jt == bcr.JOB_EV else ""
                combo = (at, h, ag, pt, jt, et)
                rate_str = str(r.get("rate_pct") or "").strip()
                remarks = str(r.get("remarks") or "").strip().lower()
                if remarks == "deleted" or (not rate_str and not str(r.get("amount_rm") or "").strip() and not str(r.get("override_rate_pct") or "").strip()):
                    deleted_combos.add(combo)
                    deleted_combos.add((at, h, ag, "", jt, et))
                else:
                    if combo not in system_combos:
                        system_combos.append(combo)

            if system_combos or deleted_combos:
                base_roles_in_system = ({(at, h, ag, jt) for at, h, ag, pt, jt, et in system_combos}
                                        | {(at, h, ag, jt) for at, h, ag, pt, jt, et in deleted_combos})
                combos = [c for c in combos if (c[0], c[1], c[2], c[4]) not in base_roles_in_system]
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
                _jt = bcr.normalize_job_type(r.get("job_type")) or bcr.JOB_SOLAR
                k = (str(r.get("agent_type") or "").strip(),
                     str(r.get("hierarchy") or "").strip(),
                     str(r.get("agent") or "").strip(),
                     str(r.get("property_type") or "").strip(),
                     _jt,
                     bcr.normalize_ev_type(r.get("ev_type")) if _jt == bcr.JOB_EV else "")
                cond_text = str(r.get("condition") or "").strip()
                trig_text = str(r.get("trigger_pct") or "").strip()
                prop_type = str(r.get("property_type") or "").strip()
                ovr_pct = str(r.get("override_rate_pct") or "").strip()
                ovr_from = str(r.get("override_from") or "").strip()
                own_cond[k] = (cond_text, trig_text, prop_type, ovr_pct, ovr_from)

            rates = []
            for agent_type, hierarchy, agent, prop_type, job_type, ev_type in combos:
                # A row's Agent Name cell can list several agents ("A, B") — they
                # all share the one rate, so resolve with the first name. Passing
                # the whole list matches nothing and the row would silently fall
                # back to the role rate.
                lookup_agent = agent.split(",")[0].strip() if agent else ""
                rate, source, eff = bcr.get_basic_rate_detail(
                    agent_type, hierarchy, month, agent=lookup_agent or None, property_type=prop_type or None,
                    year=target_year, job_type=job_type, ev_type=ev_type)
                c_val, t_val, p_val, ovr_p, ovr_f = own_cond.get(
                    (agent_type, hierarchy, agent, prop_type, job_type, ev_type), ("", "", prop_type, "", ""))
                rates.append({
                    "agent_type": agent_type,
                    "hierarchy": hierarchy,
                    "agent": agent,
                    "property_type": p_val or prop_type,
                    "job_type": job_type,
                    "ev_type": ev_type,
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



