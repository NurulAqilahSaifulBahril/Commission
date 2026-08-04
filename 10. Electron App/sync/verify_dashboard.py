"""Verify the dashboard schema in NUrul_DB matches Supabase exactly.

Same idea as verify_mirror.py but across two different transports: Supabase
over psycopg2, NUrul_DB over the HTTP proxy. Both sides render every column
as text and hash the result, so the comparison does not depend on how each
transport happens to serialise a type.

    python verify_dashboard.py
    python verify_dashboard.py --show-diff audit_log

Exit code 1 if anything differs.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from migrate_dashboard import SCHEMA, pk_column, sb_columns, sb_connect, sb_tables  # noqa: E402
from sync_mirror import dst, qi  # noqa: E402


def hash_sql(cols: list[str], table_ref: str, order_by: str) -> str:
    """Chunked to stay under Postgres' 100-argument limit on concat_ws."""
    chunks = []
    for i in range(0, len(cols), 40):
        parts = ", ".join(f"coalesce({qi(c)}::text, chr(1))" for c in cols[i:i + 40])
        chunks.append(f"concat_ws(chr(2), {parts})")
    expr = " || chr(2) || ".join(chunks)
    return (f"SELECT md5(coalesce(string_agg({expr}, chr(3) ORDER BY {order_by}), '')) AS ck, "
            f"count(*) AS n FROM {table_ref}")


def compare(cur, table: str) -> tuple[bool, str]:
    cols = [c["name"] for c in sb_columns(cur, table)]
    pk = pk_column(cur, table) or cols[0]

    exists = dst(
        "SELECT 1 AS ok FROM information_schema.tables "
        f"WHERE table_schema='{SCHEMA}' AND table_name=$1", [table]
    ).get("rows")
    if not exists:
        return False, "absent in NUrul_DB"

    sql_src = hash_sql(cols, qi(table), qi(pk))
    sql_dst = hash_sql(cols, f"{qi(SCHEMA)}.{qi(table)}", qi(pk))

    cur.execute(sql_src)
    a = cur.fetchone()
    b = dst(sql_dst)["rows"][0]

    if int(a["n"]) != int(b["n"]):
        return False, f"row count {a['n']:,} vs {b['n']:,}"
    if a["ck"] != b["ck"]:
        return False, f"content differs across {len(cols)} columns"
    return True, f"{a['n']:,} rows, {len(cols)} columns"


def show_diff(cur, table: str, limit: int = 5) -> None:
    cols = [c["name"] for c in sb_columns(cur, table)]
    pk = pk_column(cur, table) or cols[0]
    bad = []
    for c in cols:
        cur.execute(
            f"SELECT md5(coalesce(string_agg(coalesce({qi(c)}::text, chr(1)), "
            f"chr(3) ORDER BY {qi(pk)}), '')) AS ck FROM {qi(table)}"
        )
        x = cur.fetchone()["ck"]
        y = dst(
            f"SELECT md5(coalesce(string_agg(coalesce({qi(c)}::text, chr(1)), "
            f"chr(3) ORDER BY {qi(pk)}), '')) AS ck FROM {qi(SCHEMA)}.{qi(table)}"
        )["rows"][0]["ck"]
        if x != y:
            bad.append(c)
        time.sleep(0.2)
    if not bad:
        print("  all columns match")
        return
    print(f"  {len(bad)} differing column(s): {', '.join(bad)}")
    for c in bad[:3]:
        cur.execute(f"SELECT {qi(pk)} AS k, {qi(c)}::text AS v FROM {qi(table)}")
        sa = {str(r["k"]): r["v"] for r in cur.fetchall()}
        sb_rows = dst(f"SELECT {qi(pk)} AS k, {qi(c)}::text AS v "
                      f"FROM {qi(SCHEMA)}.{qi(table)}")["rows"]
        sb_ = {str(r["k"]): r["v"] for r in sb_rows}
        ids = [k for k in sa if sa[k] != sb_.get(k)][:limit]
        print(f"    {c}: {len(ids)} differing row(s) shown")
        for k in ids:
            va, vb = sa[k], sb_.get(k)
            if va and len(va) > 80:
                va = f"<{len(va)} chars>"
            if vb and len(vb) > 80:
                vb = f"<{len(vb)} chars>"
            print(f"      {pk}={k}: supabase={va!r} nuruldb={vb!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show-diff", metavar="TABLE", default=None)
    ap.add_argument("--tables", nargs="+", default=None)
    args = ap.parse_args()

    conn = sb_connect()
    cur = conn.cursor()

    if args.show_diff:
        print(f"{args.show_diff}:")
        show_diff(cur, args.show_diff)
        conn.close()
        return 0

    tables = args.tables or sb_tables(cur)
    bad = []
    print(f"{'table':30}{'result':10} detail")
    print("-" * 72)
    for t in tables:
        try:
            ok, note = compare(cur, t)
        except Exception as e:  # noqa: BLE001 - report, do not abort the sweep
            ok, note = False, str(e)[:60]
        if not ok:
            bad.append(t)
        print(f"{t:30}{'MATCH' if ok else 'DIFFER':10} {note}")
        time.sleep(0.3)
    conn.close()

    if bad:
        print(f"\n{len(bad)} table(s) differ: {', '.join(bad)}")
        print(f"Investigate with: python verify_dashboard.py --show-diff {bad[0]}")
        return 1
    print(f"\nAll {len(tables)} table(s) match Supabase exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
