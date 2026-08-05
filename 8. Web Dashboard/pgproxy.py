"""Postgres-over-HTTP transport for db.py.

NUrul_DB is only reachable through the pg-proxy REST endpoint -- there is no
postgres:// route -- so this module presents the small slice of the psycopg2
connection API that db.py actually uses (execute/executescript/fetchone/
fetchall inside a `with _connect()` block) on top of POSTed SQL.

Three proxy behaviours shape the design; each was measured, not assumed:

1. A request carrying parameters may contain exactly one statement. Postgres
   uses the extended protocol when parameters are present, and that protocol
   rejects multi-statement strings ("cannot insert multiple commands into a
   prepared statement"). Batched writes therefore inline their values as
   dollar-quoted literals and send no parameters at all.

2. A multi-statement request returns no rows -- only the last statement's
   status. So anything whose result is read has to travel alone.

3. Connections are pooled and reused between requests (the same backend_pid
   answers consecutive calls), so a `SET search_path` can leak into unrelated
   requests. Table names are schema-qualified explicitly instead.

Transactions: db.py's write paths are read-then-write -- they snapshot a table
for the audit "before" image, then DELETE and re-INSERT. Since each POST is its
own transaction, writes are buffered and flushed as a single BEGIN/COMMIT
request when the block exits, which keeps each DELETE+INSERT pair atomic. A
read is served immediately, and only forces an early flush if it touches a
table with writes still pending, so it never observes a stale row.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent

try:
    from dotenv import load_dotenv
    for _candidate in (CURRENT_DIR / ".env", REPO_ROOT / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

def _normalize_proxy_url(url: str) -> str:
    """Accept the proxy's origin with or without the /api/sql path.

    The repo carries more than one .env and they do not agree on which form
    PG_PROXY_URL takes; the commission engines normalize it, so a bare origin
    works for them and used to 404 only here. That failure is invisible — every
    dashboard-table read falls back to the stale local SQLite copy and serves
    rates that were edited weeks ago, with nothing but a warning on stderr.
    """
    base = (url or "").strip().rstrip("/")
    if not base or base.endswith("/api/sql"):
        return base
    return f"{base}/api/sql"


PROXY_URL = _normalize_proxy_url(
    os.environ.get("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql"))
PROXY_TOKEN = os.environ.get("PG_MIRROR_TOKEN")
PROXY_DB = os.environ.get("PG_MIRROR_DB", "NUrul_DB")
SCHEMA = os.environ.get("PG_MIRROR_SCHEMA", "dashboard")

# The dashboard's own tables. They live in their own schema because NUrul_DB
# already has an unrelated `users` and `audit_log` in public (the ERP's, with
# different columns); an unqualified name would hit the wrong one.
DASHBOARD_TABLES = frozenset({
    "agent_roles", "anp_rules", "anp_tiers", "audit_log", "basic_rates",
    "commission_entries", "commission_rates", "contest_roster", "contest_rules",
    "contest_team_month", "ega_month_thresholds", "ega_rules", "factory_rates",
    "invoices", "login_log", "nfp_prices", "portal_staff",
    "production_bonus_rules", "rule_settings", "special_cases", "users",
})

# Columns the proxy hands back as strings because they are bigint or a count()
# (int4 arrives as a JSON number, int8 and numeric as text). db.py and app.py
# treat these as Python ints -- user["id"] is passed straight into an integer
# column -- so they are coerced back. Deliberately narrow: most columns in this
# schema are TEXT holding digit-like values (rate_pct, effective_from) that
# must stay strings or audit diffs would start reporting spurious changes.
INT_COLUMNS = frozenset({"id", "user_id", "count"})

# ON is included for "CREATE INDEX ... ON <table>", which init_db uses for all
# eleven of its indexes. Unambiguous here only because db.py contains no JOIN;
# if one is ever added, "JOIN x ON <table>.col" would also match -- harmless,
# since it would qualify to the same table, but worth knowing.
_TABLE_RE = re.compile(
    r"(?i)\b(FROM|INTO|UPDATE|TABLE|EXISTS|ON)\s+(\"?)("
    + "|".join(sorted(DASHBOARD_TABLES, key=len, reverse=True))
    + r")\2(?!\w)"
)

_READ_RE = re.compile(r"(?is)^\s*(?:WITH\b.*?\)\s*)?SELECT\b")

_lock = threading.Lock()

_TRANSIENT = (
    "econnrefused", "database system is starting up", "connection terminated",
    "connection refused", "etimedout", "econnreset", "socket hang up",
    "too many connections", "application failed to respond",
)


class ProxyError(RuntimeError):
    """A statement failed at the proxy or the database."""


def qualify(sql: str) -> str:
    """Point bare dashboard table names at our schema."""
    return _TABLE_RE.sub(lambda m: f'{m.group(1)} {SCHEMA}."{m.group(3)}"', sql)


def is_read(sql: str) -> bool:
    return bool(_READ_RE.match(sql))


def tables_touched(sql: str) -> set[str]:
    return {m.group(3).lower() for m in _TABLE_RE.finditer(sql)}


def literal(value: Any) -> str:
    """Render a Python value as SQL text safe to inline.

    Dollar quoting is used rather than escaped single quotes: it needs no
    knowledge of standard_conforming_strings and leaves backslashes alone,
    which matters because before_json/after_json hold JSON with escapes.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    tag = "p"
    while f"${tag}$" in value:
        tag += "p"
    return f"${tag}${value}${tag}$"


def inline(sql: str, params: Any) -> str:
    """Substitute %s placeholders with literals, for batched statements."""
    if not params:
        return sql
    values = list(params)
    out, idx, i = [], 0, 0
    while True:
        j = sql.find("%s", i)
        if j < 0:
            out.append(sql[i:])
            break
        out.append(sql[i:j])
        if idx >= len(values):
            raise ProxyError(f"not enough parameters for statement: {sql[:120]}")
        out.append(literal(values[idx]))
        idx += 1
        i = j + 2
    return "".join(out)


def numbered(sql: str) -> str:
    """Rewrite %s placeholders as $1..$n for a parameterised statement."""
    counter = 0

    def repl(_m: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return re.sub(r"%s", repl, sql)


def _coerce(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        for key, value in row.items():
            if key in INT_COLUMNS and isinstance(value, str) and value.lstrip("-").isdigit():
                row[key] = int(value)
    return rows


def post(sql: str, params: list[Any] | None = None, *, timeout: int = 120,
         retries: int = 3) -> dict[str, Any]:
    if not PROXY_TOKEN:
        raise ProxyError(
            f"PG_MIRROR_TOKEN not set. Add it to {REPO_ROOT / '.env'} "
            "(full-access token for NUrul_DB)."
        )
    body = json.dumps({"db_name": PROXY_DB, "sql": sql, "params": params or []},
                      default=str).encode()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                PROXY_URL, data=body,
                headers={"Authorization": f"Bearer {PROXY_TOKEN}",
                         "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            payload["rows"] = _coerce(payload.get("rows") or [])
            return payload
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            low = detail.lower()
            if "token expired" in low:
                raise ProxyError(f"Proxy token for {PROXY_DB} expired; request a new one.") from e
            # The proxy reports a sleeping database as HTTP 400/502 with a
            # connection error in the body, same status as a real SQL error.
            if any(s in low for s in _TRANSIENT) and attempt < retries:
                last = e
                time.sleep(2 * (attempt + 1))
                continue
            raise ProxyError(f"{detail[:300]} -- while running: {sql[:200]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise ProxyError(f"proxy unreachable after {retries + 1} attempts") from last


class Cursor:
    """Holds an already-executed statement's rows."""

    def __init__(self, rows: list[dict[str, Any]], rowcount: int = -1):
        self._rows = rows
        self.rowcount = rowcount if rowcount is not None else -1

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _DeferredCursor:
    """Returned for a buffered write.

    Nothing has run yet. If the caller reads from it (create_user does, for its
    RETURNING id) the connection flushes what came before and runs this one
    statement on its own so the proxy will return rows.
    """

    def __init__(self, conn: "Connection", sql: str, params: Any):
        self._conn = conn
        self._sql = sql
        self._params = params
        self._resolved: Cursor | None = None

    def _resolve(self) -> Cursor:
        if self._resolved is None:
            self._resolved = self._conn._materialize(self._sql, self._params)
        return self._resolved

    def fetchone(self):
        return self._resolve().fetchone()

    def fetchall(self):
        return self._resolve().fetchall()

    @property
    def rowcount(self) -> int:
        return self._resolve().rowcount


class Connection:
    """One `with _connect()` block. Writes accumulate; reads run immediately."""

    def __init__(self):
        self._pending: list[str] = []
        self._pending_tables: set[str] = set()

    def execute(self, sql: str, params: Any = ()) -> Any:
        qualified = qualify(sql)
        if is_read(qualified):
            # Only flush when this read could be affected by our own buffered
            # writes; otherwise the DELETE+INSERT pair stays in one transaction.
            if self._pending_tables & tables_touched(qualified):
                self.flush()
            result = post(numbered(qualified), list(params) if params else [])
            return Cursor(result.get("rows") or [], result.get("rowCount") or 0)

        self._pending.append(inline(qualified, params))
        self._pending_tables |= tables_touched(qualified)
        return _DeferredCursor(self, sql, params)

    def executescript(self, sql: str) -> Cursor:
        # Used once, for init_db's CREATE TABLE IF NOT EXISTS block.
        self.flush()
        post(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        post(qualify(sql), timeout=300)
        return Cursor([])

    def _materialize(self, sql: str, params: Any) -> Cursor:
        """Run one buffered statement alone so its RETURNING rows come back."""
        qualified = qualify(sql)
        if self._pending and self._pending[-1] == inline(qualified, params):
            self._pending.pop()
        self.flush()
        result = post(numbered(qualified), list(params) if params else [])
        return Cursor(result.get("rows") or [], result.get("rowCount") or 0)

    def flush(self) -> None:
        if not self._pending:
            return
        statements = self._pending
        self._pending = []
        self._pending_tables = set()
        if len(statements) == 1:
            post(statements[0], timeout=300)
            return
        body = "; ".join(s.rstrip().rstrip(";") for s in statements)
        post(f"BEGIN; {body}; COMMIT;", timeout=300)

    def rollback(self) -> None:
        """Nothing has been sent yet, so discarding the buffer is the rollback."""
        self._pending = []
        self._pending_tables = set()

    def commit(self) -> None:
        self.flush()


@contextmanager
def connect():
    conn = Connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ping() -> bool:
    return bool(post("SELECT 1 AS ok").get("rows"))
