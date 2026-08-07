import os
import sys
import re
from pathlib import Path
import json
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# Load token
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
    # Query with exact invoice logic
    sql = """
        SELECT 
            i.invoice_number,
            i.invoice_date,
            i.full_payment_date,
            i.total_amount,
            a.name AS agent_name,
            c.name AS customer_name,
            sr_link.phase_type AS link_phase,
            sr_back.phase_type AS back_phase,
            COALESCE(epp_items.epp_cost, 0) AS line_epp_cost,
            pkg.system_price,
            pkg.package_description,
            pkg.db_net_floor_price,
            items.all_item_text
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        LEFT JOIN customer c ON c.customer_id = i.linked_customer
        LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
        LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                NULLIF(SUM(CASE WHEN COALESCE(ii.epp, 0) > 0 THEN ii.epp ELSE 0 END), 0),
                SUM(
                    CASE
                        WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%'
                             OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
                        THEN COALESCE(ii.amount, ii.unit_price, 0)
                        ELSE 0
                    END
                ),
                0
            ) AS epp_cost
            FROM invoice_item ii
            WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
        ) epp_items ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(NULLIF(ii.unit_price, 0), ii.amount, 0) AS system_price,
                ii.description AS package_description,
                p.nett_price AS db_net_floor_price
            FROM invoice_item ii
            LEFT JOIN package p ON p.bubble_id = ii.linked_package
            WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
            ORDER BY
                CASE WHEN ii.is_a_package IS TRUE THEN 0 ELSE 1 END,
                COALESCE(NULLIF(ii.unit_price, 0), ii.amount, 0) DESC,
                ii.id
            LIMIT 1
        ) pkg ON TRUE
        LEFT JOIN LATERAL (
            SELECT string_agg(COALESCE(ii.description, ''), ' | ') AS all_item_text
            FROM invoice_item ii
            WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
        ) items ON TRUE
        WHERE a.name ILIKE '%sunny%' AND c.name ILIKE '%ching chin min%'
    """
    
    res = query_sql(sql)
    print("Results:")
    print(json.dumps(res, indent=2))
except Exception as e:
    print(f"Failed: {e}")
