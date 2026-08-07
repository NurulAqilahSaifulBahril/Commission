import os
import sys
import json
import urllib.request
from pathlib import Path

REPO_ROOT = Path(r"c:\Users\User\OneDrive\Documents\Commission")
sys.path.insert(0, str(REPO_ROOT))

# Load dotenv
def _load_env():
    candidate = REPO_ROOT / ".env"
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
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

res = query_sql("""
    SELECT 
        ii.id,
        ii.description,
        ii.linked_package,
        p.package_name,
        p.nett_price as package_nett_price
    FROM invoice_item ii
    LEFT JOIN package p ON p.bubble_id = ii.linked_package
    JOIN invoice i ON ii.linked_invoice = i.bubble_id
    WHERE i.invoice_number = 'INV-1010634'
""")
print(json.dumps(res, indent=2))
