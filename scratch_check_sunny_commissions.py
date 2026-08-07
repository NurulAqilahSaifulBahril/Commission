import os
import sys
import json
import urllib.request
from pathlib import Path

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
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# Let's inspect any basic commission calculations or records for Sunny Tan and Ching Chin Min.
# We will check if there are records in the database or how they are computed in Python.
# First, let's run a query to get basic commission records if any, or check invoices linked to Sunny Tan.
invoices = query_sql("""
    SELECT 
        i.invoice_number, i.total_amount, i.invoice_date, i.full_payment_date,
        c.name as customer_name, a.name as agent_name
    FROM invoice i
    JOIN agent a ON i.linked_agent = a.bubble_id
    JOIN customer c ON c.customer_id = i.linked_customer
    WHERE a.name ILIKE '%sunny%' AND c.name ILIKE '%ching chin min%'
""")
print("Invoices for Sunny Tan / Ching Chin Min:")
print(json.dumps(invoices, indent=2))
