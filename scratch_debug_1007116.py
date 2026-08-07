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

res = query_sql("""
  SELECT
    i.invoice_number,
    i.total_amount,
    i.effective_epp,
    (
      SELECT COALESCE(SUM(ii_dedup.epp_interest_amount), 0)
      FROM (
        SELECT MAX(
          CASE WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%' OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
               THEN COALESCE(ii.amount, ii.unit_price, 0) ELSE 0 END
        ) AS epp_interest_amount
        FROM invoice_item ii
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
        GROUP BY TRIM(REGEXP_REPLACE(COALESCE(ii.description, ''), '\\s+', ' ', 'g'))
      ) ii_dedup
    ) as epp_items_sum,
    (
      SELECT SUM(COALESCE(p.epp_cost, 0))
      FROM payment p
      WHERE p.linked_invoice = i.bubble_id
    ) as pay_epp_sum
  FROM invoice i
  WHERE i.invoice_number = '1007116'
""")
print(json.dumps(res, indent=2))
