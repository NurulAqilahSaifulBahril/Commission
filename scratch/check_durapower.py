import os
import sys
import json
import urllib.request
from pathlib import Path

REPO_ROOT = Path("c:/Users/User/OneDrive/Documents/Commission")

def _load_env():
    for candidate in (REPO_ROOT / ".env",):
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
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# Query invoices for Durapower
sql = """
    SELECT 
        i.invoice_number,
        i.invoice_date,
        i."1st_payment_date",
        i.full_payment_date,
        i.total_amount,
        i.status,
        a.name as agent_name,
        c.name as customer_name
    FROM invoice i
    JOIN agent a ON i.linked_agent = a.bubble_id
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    WHERE c.name ILIKE '%durapower%'
"""
print("Invoices for Durapower:")
print(json.dumps(query_sql(sql), indent=2))
