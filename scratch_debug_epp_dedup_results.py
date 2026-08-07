import os
import sys
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
REPO_ROOT = Path(__file__).resolve().parent

def _load_env():
    for candidate in (
        REPO_ROOT / "1. Basic Commission" / "3. Python Script" / ".env",
        REPO_ROOT / ".env",
    ):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

_load_env()
token = os.environ.get("PG_PROXY_TOKEN")
if not token:
    token_file = REPO_ROOT / "pg_proxy_token.txt"
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()

proxy_url = "https://pg-proxy-production.up.railway.app/api/sql"
db_name = "prod_main"

def query_sql(sql, params=[]):
    req_payload = {
        "db_name": db_name,
        "sql": sql,
        "params": params
    }
    req = urllib.request.Request(
        proxy_url,
        data=json.dumps(req_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

# Query all invoices that have EPP interest using the exact candidates SQL query
sql = """
WITH candidates AS (
  SELECT
    i.invoice_number,
    i.invoice_date,
    i.full_payment_date,
    COALESCE(i.total_amount, 0)::numeric AS total_amount,
    COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0)::numeric AS paid_amount,
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
    i.bubble_id as invoice_id,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC,
               i.full_payment_date DESC NULLS LAST,
               i.id DESC
    ) AS rn
  FROM invoice i
  INNER JOIN agent a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
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
      SELECT 1 FROM agent a_ref
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
  ) pay ON TRUE
  WHERE i.paid IS TRUE
    AND i.full_payment_date IS NOT NULL
    AND (
      EXTRACT(YEAR FROM i.invoice_date)::int = 2026
      OR EXTRACT(YEAR FROM i.full_payment_date)::int = 2026
    )
)
SELECT * FROM candidates WHERE rn = 1 AND epp_interest > 0
ORDER BY invoice_number
"""

candidates = query_sql(sql).get("rows") or []
print(f"Found {len(candidates)} paid 2026 invoices with EPP interest > 0.")

# For each candidate, let's query the raw invoice items and display them
for cand in candidates:
    inv_id = cand["invoice_id"]
    inv_num = cand["invoice_number"]
    agent_name = cand["agent_name"]
    total_amount = float(cand["total_amount"])
    epp_interest = float(cand["epp_interest"])
    
    # Query invoice items
    sql_items = """
        SELECT ii.bubble_id, ii.description, ii.amount, ii.unit_price, ii.linked_invoice
        FROM invoice_item ii
        WHERE (ii.linked_invoice = '{}' OR ii.bubble_id IN (
            SELECT unnest(linked_invoice_item) FROM invoice WHERE bubble_id = '{}'
        )) AND (ii.description ILIKE '%epp%interest%' OR ii.description ILIKE '%epp_interest%')
    """.format(inv_id, inv_id)
    items = query_sql(sql_items).get("rows") or []
    
    # Calculate simple sum
    simple_sum = sum(float(it["amount"] or it["unit_price"] or 0) for it in items)
    
    # If the calculated EPP interest in the query doesn't match the simple sum, we print details
    print(f"\nInvoice: {inv_num} | Agent: {agent_name} | Total: RM {total_amount:,.2f}")
    print(f"  SQL Deduplicated EPP Interest: RM {epp_interest:,.2f}")
    print(f"  Simple sum of EPP items:       RM {simple_sum:,.2f}")
    print(f"  Difference:                    RM {simple_sum - epp_interest:,.2f}")
    print("  Associated EPP Items:")
    for it in items:
        print(f"    - ID: {it['bubble_id']}")
        print(f"      Desc: {it['description']!r}")
        print(f"      Val:  RM {float(it['amount'] or it['unit_price'] or 0):,.2f}")
