import os
import json
import urllib.request
from pathlib import Path

def load_env():
    p = Path(r"C:\Users\User\OneDrive\Documents\Commission\.env")
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

load_env()
token = os.environ.get("PG_PROXY_TOKEN")
proxy_url = "https://pg-proxy-production.up.railway.app/api/sql"

def run_query(sql):
    body = json.dumps({"db_name": "prod_main", "sql": sql}).encode()
    req = urllib.request.Request(
        proxy_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

sql = """
SELECT
  i.invoice_number,
  count(*)
FROM invoice i
INNER JOIN agent a ON a.bubble_id = i.linked_agent
WHERE i.paid IS TRUE
  AND i.full_payment_date IS NOT NULL
  AND EXTRACT(YEAR FROM i.full_payment_date)::int = 2026
  AND LOWER(btrim(COALESCE(a.agent_type, ''))) IN ('internal', 'full time')
  AND COALESCE(i.percent_of_total_amount, 0) >= 100.0
GROUP BY i.invoice_number
HAVING count(*) > 1;
"""

payload = run_query(sql)
print(payload)
