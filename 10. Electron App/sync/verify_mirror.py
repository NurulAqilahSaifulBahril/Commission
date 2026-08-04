"""Verify the mirror matches prod_main exactly, table by table.

Compares a checksum built from every shared column rendered as text, ordered
by id, so it is independent of physical column order (sync_mirror.py appends
columns it has to add, which changes their position but not their content).

    python verify_mirror.py                  # all tables
    python verify_mirror.py --tables invoice
    python verify_mirror.py --show-diff invoice   # list the first differing ids

Exit code is 1 if any table differs, so it can gate a scheduled run.
"""

from __future__ import annotations

import argparse
import sys
import time

from sync_mirror import TABLES, columns, count, dst, qi, src


def checksum(fetch, table: str, cols: list[str]) -> str | None:
    """md5 over all rows; chr(1) marks NULL so it cannot collide with ''.

    Postgres caps a function call at 100 arguments, and invoice has 130
    columns, so wide tables concatenate in chunks of 40 joined with ||.
    """
    chunks = []
    for i in range(0, len(cols), 40):
        parts = ", ".join(f"coalesce({qi(c)}::text, chr(1))" for c in cols[i:i + 40])
        chunks.append(f"concat_ws(chr(2), {parts})")
    row_expr = " || chr(2) || ".join(chunks)
    rows = fetch(
        f"SELECT md5(coalesce(string_agg({row_expr}, chr(3) ORDER BY id), '')) AS ck "
        f"FROM {qi(table)}"
    ).get("rows") or []
    return rows[0]["ck"] if rows else None


def compare(table: str) -> tuple[bool, str]:
    s_cols = columns(src, table)
    d_cols = columns(dst, table)
    if not d_cols:
        return False, "absent in mirror"

    shared = [c for c in s_cols if c in d_cols]
    missing = [c for c in s_cols if c not in d_cols]

    s_n, d_n = count(src, table), count(dst, table)
    if s_n != d_n:
        return False, f"row count {s_n:,} vs {d_n:,}"

    if checksum(src, table, shared) != checksum(dst, table, shared):
        return False, f"content differs across {len(shared)} columns"

    note = f"{s_n:,} rows, {len(shared)} columns"
    if missing:
        note += f"; {len(missing)} source column(s) not mirrored: {', '.join(missing[:4])}"
    return not missing, note


def show_diff(table: str, limit: int = 10) -> None:
    """Per-column checksums to isolate which column drifted, then sample ids."""
    s_cols, d_cols = columns(src, table), columns(dst, table)
    shared = [c for c in s_cols if c in d_cols]
    expr = ", ".join(
        f"md5(coalesce(string_agg(coalesce({qi(c)}::text, chr(1)), chr(3) ORDER BY id), '')) AS {qi(c)}"
        for c in shared
    )
    a = src(f"SELECT {expr} FROM {qi(table)}")["rows"][0]
    time.sleep(0.5)
    b = dst(f"SELECT {expr} FROM {qi(table)}")["rows"][0]
    bad = [c for c in shared if a[c] != b[c]]
    if not bad:
        print("  all columns match")
        return
    print(f"  {len(bad)} differing column(s): {', '.join(bad)}")
    for c in bad[:3]:
        time.sleep(0.5)
        q = f"SELECT id, {qi(c)}::text AS v FROM {qi(table)} ORDER BY id"
        sa = {r["id"]: r["v"] for r in src(q)["rows"]}
        time.sleep(0.5)
        sb = {r["id"]: r["v"] for r in dst(q)["rows"]}
        ids = [i for i in sa if sa[i] != sb.get(i)][:limit]
        print(f"    {c}: {len(ids)} shown")
        for i in ids:
            print(f"      id={i}: source={sa[i]!r} mirror={sb.get(i)!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", nargs="+", default=None)
    ap.add_argument("--show-diff", metavar="TABLE", default=None,
                    help="Isolate which column and rows differ for one table")
    args = ap.parse_args()

    if args.show_diff:
        print(f"{args.show_diff}:")
        show_diff(args.show_diff)
        return 0

    targets = args.tables or TABLES
    bad = []
    print(f"{'table':28}{'result':10} detail")
    print("-" * 78)
    for t in targets:
        try:
            ok, note = compare(t)
        except RuntimeError as e:
            ok, note = False, str(e)[:60]
        if not ok:
            bad.append(t)
        print(f"{t:28}{'MATCH' if ok else 'DIFFER':10} {note}")
        time.sleep(0.5)

    if bad:
        print(f"\n{len(bad)} table(s) differ: {', '.join(bad)}")
        print(f"Investigate with: python verify_mirror.py --show-diff {bad[0]}")
        return 1
    print(f"\nAll {len(targets)} table(s) match prod_main exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
