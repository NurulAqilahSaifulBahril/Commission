import os
import sys
import json
import urllib.request
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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"Error {e.code}: {e.read().decode(errors='replace')}")
        raise

# Find all invoices and their epp items, counting duplicates
sql = """
SELECT 
    i.invoice_number,
    a.name as agent_name,
    c.name as customer_name,
    count(ii.bubble_id) as total_epp_items,
    sum(coalesce(ii.amount, ii.unit_price, 0)) as raw_sum_epp,
    (
      SELECT coalesce(sum(ii_dedup.epp_interest_amount), 0)
      FROM (
        SELECT max(coalesce(ii2.amount, ii2.unit_price, 0)) as epp_interest_amount
        FROM invoice_item ii2
        WHERE (ii2.linked_invoice = i.bubble_id OR ii2.bubble_id = ANY(i.linked_invoice_item))
          AND (ii2.description ILIKE '%epp%interest%' OR ii2.description ILIKE '%epp_interest%')
        GROUP BY TRIM(REGEXP_REPLACE(COALESCE(ii2.description, ''), '\\s+', ' ', 'g'))
      ) ii_dedup
    ) as dedup_sum_epp
FROM invoice i
JOIN agent a ON a.bubble_id = i.linked_agent
LEFT JOIN customer c ON c.customer_id = i.linked_customer
JOIN invoice_item ii ON (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
WHERE (ii.description ILIKE '%epp%interest%' OR ii.description ILIKE '%epp_interest%')
GROUP BY i.bubble_id, i.invoice_number, a.name, c.name, i.linked_invoice_item
ORDER BY total_epp_items DESC
"""

res = query_sql(sql)
print("=== Invoices with EPP Interest Items ===")
rows = res.get("rows") or []
print(f"Found {len(rows)} invoices with EPP items.")
print(f"{'Inv Num':<12} | {'Agent':<20} | {'Customer':<25} | {'Count':<5} | {'Raw Sum':<12} | {'Dedup Sum':<12}")
print("-" * 92)
for r in rows:
    if int(r["total_epp_items"]) > 1:
        print(f"{r['invoice_number']:<12} | {r['agent_name']:<20} | {r['customer_name']:<25} | {r['total_epp_items']:<5} | {float(r['raw_sum_epp']):<12,.2f} | {float(r['dedup_sum_epp']):<12,.2f}")
