import os
import sys
import re
from pathlib import Path
import json
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# Load token
basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"
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

try:
    # Find paid invoices in 2026 for ZUL
    invoices_query = """
        SELECT 
            i.bubble_id as invoice_id,
            i.invoice_number,
            i.invoice_date,
            i.full_payment_date,
            i.total_amount,
            i.paid_amount,
            i.paid,
            a.name as agent_name,
            c.name as customer_name
        FROM invoice i
        JOIN agent a ON i.linked_agent = a.bubble_id
        LEFT JOIN customer c ON c.customer_id = i.linked_customer
        WHERE a.name ILIKE '%zul%' 
          AND (i.full_payment_date IS NOT NULL OR i.paid = true)
          AND i.full_payment_date >= '2026-01-01'
    """
    invoices = query_sql(invoices_query)
    print("\nPaid Invoices found for 'zul' in 2026:")
    print(json.dumps(invoices, indent=2))
except Exception as e:
    print(f"Failed: {e}")
