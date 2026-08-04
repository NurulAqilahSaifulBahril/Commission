"""Move the dashboard's own tables from Supabase into NUrul_DB.

The commission scripts' source data is handled by sync_mirror.py; this covers
the other half -- the tables the dashboard *writes*: users, rates, special
cases, contest/ANP/EGA rules, audit log.

Destination is the "dashboard" schema, not public. NUrul_DB already has its
own `users` and `audit_log` in public (the ERP's, with entirely different
columns), and dropping ours on top would destroy them. A separate schema also
keeps it obvious which tables are the dashboard's and which came from the ERP.

Supabase is read over a real postgres:// connection, so it can be introspected
normally. NUrul_DB is only reachable over the HTTP proxy, so writes reuse
sync_mirror's lossless text-cast transfer.

    python migrate_dashboard.py --backup-only   # dump Supabase, write nothing
    python migrate_dashboard.py --dry-run       # print the DDL it would run
    python migrate_dashboard.py                 # back up, create, copy, verify

A backup is always written first; --dry-run and --backup-only never write to
NUrul_DB. Re-running is safe: each table is recreated and refilled from
Supabase, which stays the source of truth until step 4 repoints db.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from sync_mirror import dst, qi, read_expr  # noqa: E402

try:
    from dotenv import load_dotenv
    for _c in (_SCRIPT_DIR / ".env", _REPO_ROOT / ".env"):
        if _c.is_file():
            load_dotenv(_c, override=False)
except ImportError:
    pass

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL")
SCHEMA = os.environ.get("PG_MIRROR_SCHEMA", "dashboard")
BACKUP_DIR = _REPO_ROOT / "backups"

# invoices is rebuildable output from sync_invoices.py rather than dashboard
# state, but it is copied anyway: it is what the Electron portal reads, and
# leaving it behind would mean the portal still depends on Supabase.
SKIP_TABLES: set[str] = set()


def sb_connect():
    if not DATABASE_URL:
        raise RuntimeError(f"DATABASE_URL not set in {_REPO_ROOT / '.env'}")
    return psycopg2.connect(DATABASE_URL, sslmode="require",
                            cursor_factory=psycopg2.extras.RealDictCursor)


def sb_tables(cur) -> list[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
        """
    )
    return [r["table_name"] for r in cur.fetchall() if r["table_name"] not in SKIP_TABLES]


def sb_columns(cur, table: str) -> list[dict[str, Any]]:
    """Columns with exact type, nullability and default, in physical order."""
    cur.execute(
        """
        SELECT a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull AS notnull,
               a.attidentity AS identity,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (f"public.{table}",),
    )
    return list(cur.fetchall())


def sb_constraints(cur, table: str) -> list[str]:
    """Primary key and unique constraint definitions (no FKs between these)."""
    cur.execute(
        """
        SELECT pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = to_regclass(%s) AND contype IN ('p', 'u')
        ORDER BY contype
        """,
        (f"public.{table}",),
    )
    return [r["def"] for r in cur.fetchall()]


def sb_indexes(cur, table: str) -> list[str]:
    """Secondary indexes, excluding those backing a constraint."""
    cur.execute(
        """
        SELECT indexdef FROM pg_indexes
        WHERE schemaname='public' AND tablename=%s
          AND indexname NOT IN (
            SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s)
          )
        """,
        (table, f"public.{table}"),
    )
    return [r["indexdef"] for r in cur.fetchall()]


def build_ddl(cur, table: str) -> tuple[str, list[str], list[str]]:
    """Return (CREATE TABLE, index statements, identity/serial column names)."""
    cols = sb_columns(cur, table)
    serials: list[str] = []
    parts: list[str] = []

    for c in cols:
        frag = f"    {qi(c['name'])} {c['type']}"
        default = c["default_expr"]
        identity = (c.get("identity") or "").strip()
        if identity:
            # Identity columns carry no pg_attrdef default -- they are flagged
            # on the column itself. Missing this leaves "id bigint NOT NULL"
            # with no way to generate a value, so every dashboard insert fails.
            kind = "ALWAYS" if identity == "a" else "BY DEFAULT"
            frag += f" GENERATED {kind} AS IDENTITY"
            serials.append(c["name"])
        elif default and "nextval(" in default:
            frag += " GENERATED BY DEFAULT AS IDENTITY"
            serials.append(c["name"])
        elif default and "auth." in default:
            # Supabase-only helper (auth.uid()); no such schema in NUrul_DB.
            print(f"    note: dropping Supabase-specific default on "
                  f"{table}.{c['name']} ({default})")
        elif default:
            frag += f" DEFAULT {default}"
        if c["notnull"]:
            frag += " NOT NULL"
        parts.append(frag)

    for con in sb_constraints(cur, table):
        parts.append(f"    {con}")

    ddl = f"CREATE TABLE {qi(SCHEMA)}.{qi(table)} (\n" + ",\n".join(parts) + "\n)"

    idx = []
    for stmt in sb_indexes(cur, table):
        # Retarget "ON public.x" at our schema; index names are global per
        # schema so they carry over unchanged.
        idx.append(stmt.replace(" ON public.", f" ON {SCHEMA}.")
                       .replace("INDEX ", f"INDEX ", 1))
    return ddl, idx, serials


def backup(cur, tables: list[str]) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"supabase-dashboard-{stamp}.json"
    dump: dict[str, Any] = {"taken_at": stamp, "source": "supabase", "tables": {}}
    for t in tables:
        cols = sb_columns(cur, t)
        names = [c["name"] for c in cols]
        sel = ", ".join(f'{qi(c["name"])}::text AS {qi(c["name"])}' for c in cols)
        cur.execute(f'SELECT {sel} FROM {qi(t)}')
        rows = [dict(r) for r in cur.fetchall()]
        dump["tables"][t] = {"columns": names, "rows": rows}
        print(f"    backed up {t}: {len(rows):,} rows")
    path.write_text(json.dumps(dump, indent=1, default=str), encoding="utf-8")
    return path


# The proxy accepts a request body up to somewhere between 768 KB and 1 MB
# (measured); past that it returns 502 "Application failed to respond".
# Batches are packed to stay well under that, and a row too big to send even
# alone is streamed in pieces.
MAX_PAYLOAD = 400_000
CHUNK = 250_000


def pk_column(cur, table: str) -> str | None:
    """Single-column primary key, needed to address a row for chunked writes."""
    cur.execute(
        """
        SELECT a.attname AS name
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p'
        """,
        (f"public.{table}",),
    )
    rows = cur.fetchall()
    return rows[0]["name"] if len(rows) == 1 else None


def insert_large_row(table: str, row: dict[str, Any], names: list[str], pk: str) -> None:
    """Insert one row whose JSON exceeds the request limit.

    The row goes in with its biggest text columns blank, then each is appended
    in chunks. audit_log has a single row carrying 764 KB of before/after JSON
    from an nfp_price bulk edit, which cannot be sent in one request at all.
    """
    target = f"{qi(SCHEMA)}.{qi(table)}"
    big = sorted(
        (c for c in names if isinstance(row.get(c), str) and len(row[c]) > CHUNK),
        key=lambda c: len(row[c]), reverse=True,
    )
    stub = dict(row)
    for c in big:
        stub[c] = ""

    col_list = ", ".join(qi(c) for c in names)
    dst(
        f"INSERT INTO {target} ({col_list}) "
        f"SELECT {col_list} FROM json_populate_recordset(null::{target}, $1::json)",
        [json.dumps([stub], default=str)],
    )
    for c in big:
        value = row[c]
        for i in range(0, len(value), CHUNK):
            dst(
                f"UPDATE {target} SET {qi(c)} = {qi(c)} || $1 WHERE {qi(pk)} = $2",
                [value[i:i + CHUNK], row[pk]],
            )
            time.sleep(0.2)
        print(f"    {table}: streamed {c} ({len(value):,} chars) for {pk}={row[pk]}")


def copy_table(cur, table: str, batch: int = 500) -> int:
    cols = sb_columns(cur, table)
    names = [c["name"] for c in cols]
    col_list = ", ".join(qi(c) for c in names)
    sel = ", ".join(read_expr(c["name"], c["type"]) for c in cols)

    cur.execute(f"SELECT {sel} FROM {qi(table)}")
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return 0

    target = f"{qi(SCHEMA)}.{qi(table)}"
    insert = (
        f"INSERT INTO {target} ({col_list}) "
        f"SELECT {col_list} FROM json_populate_recordset(null::{target}, $1::json)"
    )
    # audit_log rows vary from a few hundred bytes to 764 KB (before/after JSON
    # of a bulk edit), so batching by row count either wastes requests or
    # overshoots the proxy's body limit. Pack by serialised size instead.
    pk = pk_column(cur, table)
    pending: list[dict[str, Any]] = []
    pending_bytes = 0
    done = 0

    def flush() -> None:
        nonlocal pending, pending_bytes
        if pending:
            dst(insert, [json.dumps(pending, default=str)])
            pending, pending_bytes = [], 0
            time.sleep(0.3)

    for row in rows:
        size = len(json.dumps(row, default=str))
        if size > MAX_PAYLOAD:
            flush()
            if not pk:
                raise RuntimeError(
                    f"{table} has a {size:,} byte row too large for one request "
                    f"and no single-column primary key to stream it with"
                )
            insert_large_row(table, row, names, pk)
            done += 1
            continue
        if pending_bytes + size > MAX_PAYLOAD:
            flush()
        pending.append(row)
        pending_bytes += size
        done += 1
    flush()
    return done


def resync_identity(table: str, serials: list[str]) -> None:
    """Point each identity sequence past the largest id we just inserted,
    otherwise the next insert collides with a copied row."""
    for col in serials:
        dst(
            f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.{table}', '{col}'), "
            f"COALESCE((SELECT MAX({qi(col)}) FROM {qi(SCHEMA)}.{qi(table)}), 0) + 1, false)"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Print DDL, write nothing")
    ap.add_argument("--tables", nargs="+", default=None)
    args = ap.parse_args()

    conn = sb_connect()
    cur = conn.cursor()
    tables = args.tables or sb_tables(cur)
    print(f"{len(tables)} table(s) in Supabase\n")

    if args.dry_run:
        for t in tables:
            ddl, idx, serials = build_ddl(cur, t)
            print(ddl + ";")
            for s in idx:
                print(s + ";")
            if serials:
                print(f"-- identity columns: {', '.join(serials)}")
            print()
        conn.close()
        return 0

    print("Backing up Supabase first:")
    path = backup(cur, tables)
    print(f"  -> {path}\n")
    if args.backup_only:
        conn.close()
        return 0

    dst(f"CREATE SCHEMA IF NOT EXISTS {qi(SCHEMA)}")
    print(f"Copying into NUrul_DB schema {SCHEMA!r}:")

    failures: list[str] = []
    results: list[tuple[str, int, int]] = []
    for t in tables:
        try:
            ddl, idx, serials = build_ddl(cur, t)
            dst(f"DROP TABLE IF EXISTS {qi(SCHEMA)}.{qi(t)} CASCADE")
            dst(ddl)
            for s in idx:
                try:
                    dst(s)
                except RuntimeError as e:
                    print(f"    note: index skipped on {t} ({str(e)[:80]})")
            n = copy_table(cur, t)
            if serials:
                resync_identity(t, serials)
            landed = int(dst(f"SELECT count(*) AS n FROM {qi(SCHEMA)}.{qi(t)}")["rows"][0]["n"])
            results.append((t, n, landed))
            flag = "" if landed == n else "  <-- MISMATCH"
            print(f"    {t:30} {n:>6,} -> {landed:>6,}{flag}")
            if landed != n:
                failures.append(t)
        except RuntimeError as e:
            print(f"    {t:30} FAILED: {str(e)[:160]}")
            failures.append(t)

    conn.close()
    total = sum(r[2] for r in results)
    print(f"\n{total:,} rows in {SCHEMA!r} across {len(results)} table(s).")
    if failures:
        print(f"Problems with: {', '.join(failures)}")
        return 1
    print(f"Supabase is untouched and still authoritative; backup at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
