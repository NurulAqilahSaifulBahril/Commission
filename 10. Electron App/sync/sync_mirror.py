"""Mirror job: company Postgres (prod_main) -> your own database (NUrul_DB).

Copies the raw ERP tables the commission scripts read, so those scripts can
run against a database you control instead of the company's read-only one.

This is a *mirror*, not the denormalised view sync_invoices.py builds:
it reproduces the source tables row-for-row, including deletions, so
full_internal_basic_commission.py and friends see exactly what they would
see against prod_main.

Both ends go through the same HTTP proxy — there is no postgres:// route to
either database — so every statement is a POST. Consequences that shape the
design below:

  * Each POST is its own connection and its own transaction. Anything that
    must be atomic has to travel in a single request.
  * The proxy throttles rapid-fire calls (it returns a transport error rather
    than a SQL error), hence the backoff in _sql().
  * There is a gateway timeout around 300s, so rows are paged rather than
    pulled in one shot.

Per table the sequence is: create the table if it is missing (referral was),
load every source row into a staging table, then swap staging into place in
one atomic request. A failed or half-finished run therefore leaves the old
data intact -- the destination is never observed empty.

Usage:
    python sync_mirror.py                      # mirror all tables
    python sync_mirror.py --tables invoice payment
    python sync_mirror.py --dry-run            # report row gaps, change nothing

Env vars (repo-root .env):
    PG_PROXY_URL     shared proxy endpoint
    PG_PROXY_TOKEN   source token  (prod_main, read-only)
    PG_PROXY_DB      source db name, default prod_main
    PG_MIRROR_TOKEN  destination token (NUrul_DB, full access)
    PG_MIRROR_DB     destination db name, default NUrul_DB
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent

try:
    from dotenv import load_dotenv
    for _candidate in (_SCRIPT_DIR / ".env", _REPO_ROOT / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

PROXY_URL = os.environ.get("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
SRC_TOKEN = os.environ.get("PG_PROXY_TOKEN")
SRC_DB = os.environ.get("PG_PROXY_DB", "prod_main")
DST_TOKEN = os.environ.get("PG_MIRROR_TOKEN")
DST_DB = os.environ.get("PG_MIRROR_DB", "NUrul_DB")

# The ERP tables referenced in SQL by the commission scripts and the dashboard.
# Derived by cross-referencing every table in prod_main against FROM/JOIN in
# "1. Basic Commission", "2. NFP Commission", "3. ANP Commission",
# "4. EGA ESA Awards", "5. Production Bonus" and "8. Web Dashboard".
# Keep this list in sync when a script starts reading a new table.
TABLES = [
    "agent",
    "customer",
    "invoice",
    "invoice_item",
    "invoice_payment_planning",
    "package",
    "payment",
    "referral",
    "seda_registration",
    "user",
    "voucher",
]

# Wide tables (invoice has 130 columns, seda_registration 115) produce large
# JSON payloads, so they move in smaller pages.
WIDE_COLUMN_THRESHOLD = 60
DEFAULT_BATCH = 1000
WIDE_BATCH = 200

# Proxy responses that mean "try again", not "your SQL is wrong". Both
# databases are hosted and idle down, so the first call after a quiet period
# often arrives while Postgres is still starting.
_TRANSIENT = (
    "econnrefused",
    "database system is starting up",
    "database system is shutting down",
    "connection terminated",
    "connection refused",
    "etimedout",
    "econnreset",
    "socket hang up",
    "too many connections",
)


def qi(name: str) -> str:
    """Quote an identifier. Required at minimum for "user", a reserved word."""
    return '"' + name.replace('"', '""') + '"'


def _sql(*, token: str | None, db: str, sql: str, params: list[Any] | None = None,
         timeout: int = 300, retries: int = 4) -> dict[str, Any]:
    if not token:
        raise RuntimeError(
            f"No token for {db}. Set PG_PROXY_TOKEN (source) and PG_MIRROR_TOKEN "
            f"(destination) in {_REPO_ROOT / '.env'}"
        )
    body = json.dumps({"db_name": db, "sql": sql, "params": params or []}).encode()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                PROXY_URL, data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            low = detail.lower()
            if "token expired" in low:
                raise RuntimeError(f"Proxy token for {db} has expired; request a new one.") from e
            # The proxy reports infrastructure trouble as HTTP 400 with a
            # connection error in the body, indistinguishable by status code
            # from a genuine SQL error. These databases sleep and cold-start,
            # so a first call after idle routinely lands here - retry those.
            if any(s in low for s in _TRANSIENT):
                last = e
                if attempt < retries:
                    wait = 5 * (attempt + 1)
                    print(f"    {db} unavailable ({detail[:60].strip()}), retrying in {wait}s...")
                    time.sleep(wait)
                    continue
            # A SQL error is deterministic - retrying just wastes time.
            raise RuntimeError(f"HTTP {e.code} from proxy ({db}): {detail[:400]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            # Transport-level: usually the proxy throttling or a cold start.
            last = e
            if attempt < retries:
                wait = 3 * (attempt + 1)
                print(f"    transport error ({type(e).__name__}), retrying in {wait}s...")
                time.sleep(wait)
    raise RuntimeError(f"giving up after {retries + 1} attempts against {db}") from last


def src(sql: str, params: list[Any] | None = None, **kw) -> dict[str, Any]:
    return _sql(token=SRC_TOKEN, db=SRC_DB, sql=sql, params=params, **kw)


def dst(sql: str, params: list[Any] | None = None, **kw) -> dict[str, Any]:
    return _sql(token=DST_TOKEN, db=DST_DB, sql=sql, params=params, **kw)


def columns(fetch, table: str) -> dict[str, str]:
    """Column name -> exact Postgres type, in physical order.

    format_type() is used rather than information_schema because it renders
    arrays and length-qualified types the way CREATE TABLE needs them
    ("character varying(255)", "text[]"), which information_schema does not.
    Returns {} when the table does not exist.
    """
    rows = fetch(
        """
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        [f"public.{table}"],
    ).get("rows") or []
    return {r["name"]: r["type"] for r in rows}


def ensure_table(table: str) -> list[str]:
    """Make the destination table match the source's shape; return shared columns."""
    s_cols = columns(src, table)
    if not s_cols:
        raise RuntimeError(f"source table {table} does not exist in {SRC_DB}")
    d_cols = columns(dst, table)

    if not d_cols:
        # referral hit this path: present in prod_main, absent in NUrul_DB.
        defs = ", ".join(f"{qi(n)} {t}" for n, t in s_cols.items())
        print(f"    creating missing table {table} ({len(s_cols)} columns)")
        dst(f"CREATE TABLE {qi(table)} ({defs})")
        if "id" in s_cols:
            dst(f"ALTER TABLE {qi(table)} ADD PRIMARY KEY (id)")
        # Replay the source's secondary indexes; the commission queries join on
        # bubble_id / linked_* and would otherwise seq-scan.
        for r in src("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=$1",
                     [table]).get("rows") or []:
            ddl = r["indexdef"]
            if " UNIQUE " in ddl.upper() and "_pkey" in ddl:
                continue
            try:
                dst(ddl)
            except RuntimeError as e:
                print(f"    note: could not replay index ({e})")
        d_cols = columns(dst, table)
    else:
        for name, typ in s_cols.items():
            if name not in d_cols:
                print(f"    adding missing column {table}.{name} {typ}")
                dst(f"ALTER TABLE {qi(table)} ADD COLUMN {qi(name)} {typ}")
            elif d_cols[name] != typ:
                # The clone drifted: several columns that are numeric upstream
                # were integer here (invoice_item.qty, payment.bank_charges,
                # seda_registration.system_size...). Loading a numeric into
                # them either errors or silently drops the decimals, so widen
                # the destination to the source's type before copying.
                print(f"    retyping {table}.{name}: {d_cols[name]} -> {typ}")
                dst(
                    f"ALTER TABLE {qi(table)} ALTER COLUMN {qi(name)} TYPE {typ} "
                    f"USING {qi(name)}::{typ}"
                )
        d_cols = columns(dst, table)

    shared = {c: t for c, t in s_cols.items() if c in d_cols}
    dropped = [c for c in d_cols if c not in s_cols]
    if dropped:
        # Left in place and simply not written to - harmless, but worth saying.
        print(f"    note: {table} has {len(dropped)} column(s) not in source: {', '.join(dropped[:5])}")
    return shared


def read_expr(col: str, typ: str) -> str:
    """How to SELECT a column so it survives the JSON hop unchanged.

    The proxy serialises values through JavaScript, which silently truncates
    timestamps to millisecond precision (prod_main stores microseconds) and
    can round floats. Postgres' own ::text rendering is exact for every type
    and json_populate_recordset parses it back via the type's input function,
    so text is the lossless wire format here.

    json/jsonb are the exception: they are already JSON, and passing them as
    text makes json_populate_recordset store the *string* rather than parse
    it, so those stay native.
    """
    if typ in ("json", "jsonb"):
        return qi(col)
    return f"{qi(col)}::text AS {qi(col)}"


def count(fetch, table: str) -> int:
    rows = fetch(f"SELECT count(*) AS n FROM {qi(table)}").get("rows") or [{"n": 0}]
    return int(rows[0]["n"])


def mirror_table(table: str, *, batch: int | None = None) -> int:
    shared = ensure_table(table)
    if "id" not in shared:
        raise RuntimeError(f"{table} has no id column to page by")

    if batch is None:
        batch = WIDE_BATCH if len(shared) > WIDE_COLUMN_THRESHOLD else DEFAULT_BATCH

    col_list = ", ".join(qi(c) for c in shared)
    select_list = ", ".join(read_expr(c, t) for c, t in shared.items())
    staging = f"_sync_{table}"

    dst(f"DROP TABLE IF EXISTS {qi(staging)}")
    dst(f"CREATE TABLE {qi(staging)} (LIKE {qi(table)} INCLUDING DEFAULTS)")

    insert_sql = (
        f"INSERT INTO {qi(staging)} ({col_list}) "
        f"SELECT {col_list} FROM json_populate_recordset(null::{qi(staging)}, $1::json)"
    )

    moved = 0
    cursor = 0
    size = batch
    while True:
        # id is qualified on both sides deliberately. The select list aliases
        # id::text back to "id", and an unqualified ORDER BY resolves to that
        # output column -- i.e. a *text* sort, where '10000' < '2'. Paging
        # then walks the keyspace in the wrong order and stops early, which
        # silently truncated customer to 3,318 of 7,346 rows. Qualifying it
        # forces the integer table column.
        page = src(
            f"SELECT {select_list} FROM {qi(table)} "
            f"WHERE {qi(table)}.id > $1 ORDER BY {qi(table)}.id LIMIT {int(size)}",
            [cursor],
        ).get("rows") or []
        if not page:
            break
        try:
            dst(insert_sql, [json.dumps(page, default=str)])
        except RuntimeError:
            # Usually an oversized payload; halve and let the same cursor retry.
            if size > 25:
                size = max(25, size // 2)
                print(f"    payload rejected, reducing page size to {size}")
                continue
            raise
        moved += len(page)
        cursor = max(int(r["id"]) for r in page)
        print(f"    {table}: staged {moved} rows (id <= {cursor})", end="\r", flush=True)
        time.sleep(0.4)  # stay under the proxy's rate limit

    print(" " * 70, end="\r")

    # Refuse to swap in a short load. Paging bugs are silent by nature -- the
    # loop just ends early and every row it did copy is valid -- so compare
    # against the source before the staging table replaces real data.
    #
    # A few rows of slack: prod_main is live, and the two counts are separate
    # requests, so a row written between them is normal. The bug this guards
    # against was 4,028 rows short, nowhere near the tolerance.
    staged = count(dst, staging)
    expected = count(src, table)
    slack = max(5, expected // 1000)
    if expected - staged > slack:
        dst(f"DROP TABLE IF EXISTS {qi(staging)}")
        raise RuntimeError(
            f"staged only {staged:,} of {expected:,} source rows "
            f"({expected - staged:,} short); mirror left untouched"
        )
    if staged != expected:
        print(f"    note: staged {staged:,} vs source {expected:,} "
              f"(within tolerance; source is live)")

    # Single request so the destination is never seen empty. The replica role
    # suppresses FK triggers for the swap (invoice_new.customer_id references
    # customer, which is otherwise deleted out from under it mid-transaction).
    dst(
        "BEGIN; "
        "SET LOCAL session_replication_role = replica; "
        f"DELETE FROM {qi(table)}; "
        f"INSERT INTO {qi(table)} ({col_list}) SELECT {col_list} FROM {qi(staging)}; "
        f"DROP TABLE {qi(staging)}; "
        "COMMIT;"
    )

    final = count(dst, table)
    if final != staged:
        raise RuntimeError(f"swap landed {final:,} rows, expected {staged:,}")
    return final


def ensure_state_table() -> None:
    dst(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            table_name  text PRIMARY KEY,
            row_count   bigint,
            synced_at   timestamptz,
            ok          boolean,
            error       text
        )
        """
    )


def record_state(table: str, rows: int, ok: bool, error: str | None) -> None:
    dst(
        """
        INSERT INTO sync_state (table_name, row_count, synced_at, ok, error)
        VALUES ($1, $2, now(), $3, $4)
        ON CONFLICT (table_name) DO UPDATE SET
            row_count = EXCLUDED.row_count, synced_at = EXCLUDED.synced_at,
            ok = EXCLUDED.ok, error = EXCLUDED.error
        """,
        [table, rows, ok, error],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", nargs="+", default=None,
                        help=f"Subset to mirror (default: all {len(TABLES)})")
    parser.add_argument("--batch", type=int, default=None,
                        help="Rows per page (default: 1000, or 200 for wide tables)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report source vs destination row counts, change nothing")
    args = parser.parse_args()

    targets = args.tables or TABLES
    unknown = [t for t in targets if t not in TABLES]
    if unknown:
        print(f"Unknown table(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Known: {', '.join(TABLES)}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"{'table':28}{'source':>10}{'mirror':>10}   gap")
        print("-" * 60)
        for t in targets:
            s = count(src, t)
            d_cols = columns(dst, t)
            d = count(dst, t) if d_cols else None
            shown = "ABSENT" if d is None else f"{d:,}"
            gap = "table missing" if d is None else ("in sync" if d == s else f"{s - d:+,}")
            print(f"{t:28}{s:>10,}{shown:>10}   {gap}")
            time.sleep(0.6)
        return 0

    ensure_state_table()
    # A run killed mid-load leaves _sync_* behind; they are pure scratch.
    for r in dst(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name LIKE '\\_sync\\_%'"
    ).get("rows") or []:
        print(f"  clearing stale staging table {r['table_name']}")
        dst(f"DROP TABLE IF EXISTS {qi(r['table_name'])}")

    started = time.time()
    failures: list[str] = []
    total = 0

    for t in targets:
        print(f"  {t} ...")
        try:
            n = mirror_table(t, batch=args.batch)
            record_state(t, n, True, None)
            total += n
            print(f"    {t}: {n:,} rows mirrored")
        except RuntimeError as e:
            msg = str(e)[:500]
            print(f"    {t}: FAILED - {msg}")
            failures.append(t)
            try:
                record_state(t, 0, False, msg)
            except RuntimeError:
                pass

    elapsed = int(time.time() - started)
    print(f"\nDone in {elapsed}s. {total:,} rows across {len(targets) - len(failures)}/{len(targets)} tables.")
    if failures:
        print(f"Failed (previous data left intact): {', '.join(failures)}")
        print(f"Rerun with: python sync_mirror.py --tables {' '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
