"""Sync job: company Postgres -> Supabase `invoices` table.

Run on the one machine that holds PG_PROXY_TOKEN — nowhere else needs it.
Reads invoices through the existing read-only proxy (same one
full_internal_basic_commission.py and friends use), then upserts them into
the Supabase project db.py already connects to (DATABASE_URL), using the
same BYPASSRLS role Flask uses — so this writes straight past RLS by design,
same as the dashboard does.

Intended to run on a schedule (cron / Windows Task Scheduler), e.g. hourly:
    python sync_invoices.py
    python sync_invoices.py --years 2025 2026

Env vars (repo-root .env, same file everything else already uses):
    PG_PROXY_URL, PG_PROXY_TOKEN, PG_PROXY_DB   (source: company Postgres)
    DATABASE_URL                                 (destination: Supabase)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent

try:
    from dotenv import load_dotenv
    for _candidate in (_SCRIPT_DIR / ".env", _REPO_ROOT / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

PG_PROXY_URL = os.environ.get("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
PG_PROXY_TOKEN = os.environ.get("PG_PROXY_TOKEN")
PG_PROXY_DB = os.environ.get("PG_PROXY_DB", "prod_main")
DATABASE_URL = os.environ.get("DATABASE_URL")


def _proxy_sql(*, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    if not PG_PROXY_TOKEN:
        raise RuntimeError("PG_PROXY_TOKEN not set. Add it to the repo-root .env.")
    body = json.dumps({"db_name": PG_PROXY_DB, "sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(
        PG_PROXY_URL,
        data=body,
        headers={"Authorization": f"Bearer {PG_PROXY_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 400 and "token expired" in detail.lower():
            raise RuntimeError(
                "Postgres proxy token expired. Request a new read-only JWT and "
                f"update PG_PROXY_TOKEN in {_REPO_ROOT / '.env'}"
            ) from e
        raise RuntimeError(f"HTTP {e.code} from proxy: {detail}") from e


_MILESTONE_FRACS_CACHE: dict[int, tuple[str, str]] = {}


def _milestone_fracs(year: int) -> tuple[str, str]:
    """(advance, balance) SQL fractions for the pct5/pct75 milestone CTEs.

    pct5_date / pct75_date are legacy COLUMN NAMES, fixed by the Supabase
    `invoices` schema the portal reads; what they mean is "the date the
    advance payout trigger was reached" / "the date the balance trigger was
    reached". The percentages behind them come from the dashboard Data page
    (basic_commission_rates.multi_stage_cutover — 4.99/75 as of July 2026),
    the same source the commission engines read, so an edit there changes
    the portal's milestone dates on the next sync instead of silently
    diverging from the reports. Years with no multi-stage rule — and any run
    where the dashboard DB is unreachable — keep the historic 5%/75%.
    """
    cached = _MILESTONE_FRACS_CACHE.get(year)
    if cached:
        return cached
    adv, bal = "0.05", "0.75"
    try:
        rates_dir = str(_REPO_ROOT / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in sys.path:
            sys.path.insert(0, rates_dir)
        import basic_commission_rates as bcr
        cut = bcr.multi_stage_cutover(year)
        if cut:
            pol = cut[1]
            adv = format((Decimal(pol.advance_trigger) / 100).normalize(), "f")
            bal = format((Decimal(pol.balance_trigger) / 100).normalize(), "f")
            print(f"  [thresholds] {year}: advance {pol.advance_trigger}%, "
                  f"balance {pol.balance_trigger}% (from Data page)")
    except Exception as e:
        print(f"  [thresholds] Data page unavailable ({e}); using legacy 5%/75% for {year}")
    _MILESTONE_FRACS_CACHE[year] = (adv, bal)
    return adv, bal


def _invoices_sql(*, year: int, month: int) -> str:
    # Adapted from full_internal_basic_commission.py's _invoices_sql:
    # same agent-resolution / pct5-75-100 / epp_interest joins (proven
    # correct there), but WITHOUT that script's commission-eligibility
    # WHERE clause or agent_type restriction — the portal should show an
    # agent everything on their own invoices, not just what already
    # qualifies for a commission run. Adds invoice_bubble_id and
    # agent_bubble_id, which that query computes internally but never
    # returns, and which RLS on the `invoices` table depends on.
    #
    # The pct5/pct75 thresholds follow the Data page's payout triggers via
    # _milestone_fracs(); the CTE names stay pct5/pct75 because the upsert
    # columns (and the portal's readers) are keyed on those names.
    #
    # Scoped to a single (year, month) rather than a whole year: the full
    # CTE (pct5/75/100 window functions + EPP-interest + referral-matching
    # lateral joins) costs ~150-300s+ for a full year through the read-only
    # proxy, which sits right at (and sometimes past) its ~300s gateway
    # timeout. A month at a time keeps each call comfortably fast and lets
    # one bad month fail/retry without losing the rest of the run. Looping
    # every month 1-12 covers exactly the same invoices the year-scoped
    # version would — an invoice just surfaces in whichever month(s) it has
    # an invoice_date or a payment in, which the upsert on invoice_bubble_id
    # handles fine even if that's more than one month.
    adv_frac, bal_frac = _milestone_fracs(year)
    return f"""
WITH target_invoices AS (
  SELECT *
  FROM invoice
  WHERE total_amount > 0
    AND (
      (extract(year from invoice_date) = {int(year)} AND extract(month from invoice_date) = {int(month)})
      OR bubble_id IN (
        SELECT linked_invoice FROM payment
        WHERE extract(year from payment_date) = {int(year)} AND extract(month from payment_date) = {int(month)}
      )
    )
),
pct5 AS (
  SELECT sub.linked_invoice, MIN(sub.payment_date) AS pct5_date
  FROM (
    SELECT p.linked_invoice, p.payment_date,
           SUM(p.amount) OVER (PARTITION BY p.linked_invoice ORDER BY p.payment_date ASC, p.id ASC) AS running_total,
           i.total_amount
    FROM payment p JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) sub
  WHERE sub.running_total >= sub.total_amount * {adv_frac}
  GROUP BY sub.linked_invoice
),
pct75 AS (
  SELECT sub.linked_invoice, MIN(sub.payment_date) AS pct75_date
  FROM (
    SELECT p.linked_invoice, p.payment_date,
           SUM(p.amount) OVER (PARTITION BY p.linked_invoice ORDER BY p.payment_date ASC, p.id ASC) AS running_total,
           i.total_amount
    FROM payment p JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) sub
  WHERE sub.running_total >= sub.total_amount * {bal_frac}
  GROUP BY sub.linked_invoice
),
pct100 AS (
  SELECT sub.linked_invoice, MIN(sub.payment_date) AS pct100_date
  FROM (
    SELECT p.linked_invoice, p.payment_date,
           SUM(p.amount) OVER (PARTITION BY p.linked_invoice ORDER BY p.payment_date ASC, p.id ASC) AS running_total,
           i.total_amount
    FROM payment p JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) sub
  WHERE sub.running_total >= sub.total_amount
  GROUP BY sub.linked_invoice
),
candidates AS (
  SELECT
    i.bubble_id AS invoice_bubble_id,
    i.invoice_number,
    i.invoice_date,
    a.bubble_id AS agent_bubble_id,
    i."1st_payment_date" AS first_payment_date,
    i.full_payment_date AS real_full_payment_date,
    pct5.pct5_date,
    pct75.pct75_date,
    pct100.pct100_date,
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
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
    ref.project_type AS referral_project_type,
    COALESCE(NULLIF(TRIM(c_referrer.name), ''), NULLIF(TRIM(i.referrer_name), '')) AS referral_name,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC, i.full_payment_date DESC NULLS LAST, i.id DESC
    ) AS rn
  FROM target_invoices i
  INNER JOIN (
    SELECT DISTINCT ON (au.bubble_id) au.bubble_id, au.name, au.agent_type
    FROM (
      SELECT u.bubble_id, u.name, u.agent_type, 1 AS pri FROM "user" u
       WHERE u.bubble_id IS NOT NULL AND COALESCE(BTRIM(u.agent_type), '') <> ''
      UNION ALL
      SELECT ag.bubble_id, ag.name, ag.agent_type, 2 FROM agent ag WHERE ag.bubble_id IS NOT NULL
      UNION ALL
      SELECT u2.bubble_id, u2.name, u2.agent_type, 3 FROM "user" u2 WHERE u2.bubble_id IS NOT NULL
    ) au
    ORDER BY au.bubble_id, au.pri
  ) a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
  LEFT JOIN pct5 ON pct5.linked_invoice = i.bubble_id
  LEFT JOIN pct75 ON pct75.linked_invoice = i.bubble_id
  LEFT JOIN pct100 ON pct100.linked_invoice = i.bubble_id
  LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
  LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
  LEFT JOIN customer c_ref ON LOWER(TRIM(c_ref.name)) = LOWER(TRIM(COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name)))
  LEFT JOIN referral ref ON (
      ref.linked_invoice = c_ref.customer_id
      OR LOWER(TRIM(ref.name)) = LOWER(TRIM(c_ref.name))
      OR (
        ref.mobile_number IS NOT NULL AND c_ref.phone IS NOT NULL
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
        CASE WHEN ref.linked_agent ~ '^[0-9]+$' THEN a_ref.id = CAST(ref.linked_agent AS integer)
             ELSE a_ref.bubble_id = ref.linked_agent END
      ) AND LOWER(TRIM(a_ref.name)) = LOWER(TRIM(a.name))
    )
  LEFT JOIN customer c_referrer ON c_referrer.customer_id = ref.linked_customer_profile
  LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(ii_dedup.epp_interest_amount), 0) AS epp_interest
    FROM (
      SELECT MAX(
        CASE
          WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%' OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
          THEN COALESCE(ii.amount, ii.unit_price, 0) ELSE 0
        END
      ) AS epp_interest_amount
      FROM invoice_item ii
      WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
      GROUP BY TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(COALESCE(ii.description, ''), 'moths', 'months', 'gi'), '(\\d+)\\s*months', '\\1months', 'gi'), '\\s+', ' ', 'g'))
    ) ii_dedup
  ) epp_items ON TRUE
  LEFT JOIN LATERAL (
    SELECT SUM(COALESCE(p.epp_cost, 0)) AS epp_sum
    FROM payment p WHERE p.linked_invoice = i.bubble_id AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)
  ) pay ON TRUE
)
SELECT
  invoice_bubble_id, invoice_number, agent_bubble_id, agent_name, agent_type,
  customer_name, invoice_date, first_payment_date, real_full_payment_date AS full_payment_date,
  pct5_date, pct75_date, pct100_date, total_amount, paid_amount, epp_interest,
  package_type, package_name_snapshot, description, seda_nem_type, referral_project_type, referral_name
FROM candidates
WHERE rn = 1
ORDER BY agent_name ASC, invoice_bubble_id ASC
""".strip()


_UPSERT_COLUMNS = [
    "invoice_bubble_id", "invoice_number", "agent_bubble_id", "agent_name", "agent_type",
    "customer_name", "invoice_date", "first_payment_date", "full_payment_date",
    "pct5_date", "pct75_date", "pct100_date", "total_amount", "paid_amount", "epp_interest",
    "package_type", "package_name_snapshot", "description", "seda_nem_type",
    "referral_project_type", "referral_name",
]

_UPSERT_SQL = f"""
INSERT INTO invoices ({", ".join(_UPSERT_COLUMNS)}, synced_at)
VALUES ({", ".join(f"%({c})s" for c in _UPSERT_COLUMNS)}, now())
ON CONFLICT (invoice_bubble_id) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLUMNS if c != "invoice_bubble_id")},
    synced_at = now()
"""


def fetch_invoices(year: int, month: int, *, retries: int = 2) -> list[dict[str, Any]]:
    sql = _invoices_sql(year=year, month=month)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            payload = _proxy_sql(sql=sql)
            return payload.get("rows", [])
        except (RuntimeError, TimeoutError, urllib.error.URLError) as e:
            last_error = e
            if attempt < retries:
                wait = 5 * (attempt + 1)
                print(f"  {year}-{month:02d} attempt {attempt + 1} failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
    raise RuntimeError(f"giving up on {year}-{month:02d} after {retries + 1} attempts") from last_error


def upsert_invoices(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set. Add it to the repo-root .env (Supabase connection string).")
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, _UPSERT_SQL, [{c: row.get(c) for c in _UPSERT_COLUMNS} for row in rows]
                )
    finally:
        conn.close()
    print(f"  upserted {len(rows)} invoices into Supabase")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years", type=int, nargs="+", default=None,
        help="Years to sync (default: current year and previous year, to catch late payments near a year boundary)",
    )
    parser.add_argument(
        "--current-month", action="store_true",
        help="Sync only the current calendar month (fast incremental run for frequent scheduling)",
    )
    args = parser.parse_args()

    now = datetime.now()
    current_year = now.year
    if args.current_month:
        plan = [(current_year, now.month)]
    else:
        years = args.years or [current_year - 1, current_year]
        plan = [
            (year, month)
            for year in years
            for month in range(1, 13)
            # don't bother querying months that haven't happened yet
            if not (year == current_year and month > now.month)
        ]

    total_synced = 0
    failed_months: list[str] = []
    for year, month in plan:
        label = f"{year}-{month:02d}"
        try:
            rows = fetch_invoices(year, month)
        except RuntimeError as e:
            print(f"  {label}: FAILED — {e}")
            failed_months.append(label)
            continue
        upsert_invoices(rows)
        total_synced += len(rows)
        print(f"  {label}: {len(rows)} invoices synced")

    print(f"Done. {total_synced} invoice-rows synced across {len(plan)} month(s).")
    if failed_months:
        print(f"Months that failed and need a rerun: {', '.join(failed_months)}")


if __name__ == "__main__":
    main()
