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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"Error {e.code}: {e.read().decode(errors='replace')}")
        raise

# Step 1: Fetch all EPP items from database
epp_items_payload = query_sql("""
    SELECT bubble_id, linked_invoice, description, amount, unit_price, epp
    FROM invoice_item
    WHERE description ILIKE '%epp%interest%' OR description ILIKE '%epp_interest%'
""")
epp_items = epp_items_payload.get("rows") or []
print(f"Found {len(epp_items)} total EPP interest items in database.")

# Step 2: Fetch all invoices from database
invoices_payload = query_sql("""
    SELECT bubble_id, invoice_number, linked_invoice_item, total_amount, paid,
           (SELECT name FROM agent WHERE agent.bubble_id = invoice.linked_agent) as agent_name,
           (SELECT name FROM customer WHERE customer.customer_id = invoice.linked_customer) as customer_name
    FROM invoice
""")
invoices = invoices_payload.get("rows") or []
print(f"Found {len(invoices)} total invoices in database.")

# Map invoice bubble_id -> invoice dict
inv_map = {inv["bubble_id"]: inv for inv in invoices}

# Track EPP items per invoice bubble_id
inv_epp_items = defaultdict(list)

# Associate items linked via linked_invoice
for item in epp_items:
    l_inv = item.get("linked_invoice")
    if l_inv and l_inv in inv_map:
        inv_epp_items[l_inv].append(item)

# Associate items linked via linked_invoice_item array
# (avoiding duplicates if they are already added)
for inv in invoices:
    arr = inv.get("linked_invoice_item") or []
    for item_id in arr:
        # Find the item in epp_items
        matching_item = next((x for x in epp_items if x["bubble_id"] == item_id), None)
        if matching_item:
            if matching_item not in inv_epp_items[inv["bubble_id"]]:
                inv_epp_items[inv["bubble_id"]].append(matching_item)

# Print invoices that have more than 1 associated EPP item
print("\n=== Invoices with multiple EPP items associated ===")
dupe_count = 0
for inv_id, items in inv_epp_items.items():
    if len(items) > 1:
        inv = inv_map[inv_id]
        dupe_count += 1
        print(f"\nInvoice Number: {inv['invoice_number']} | Agent: {inv['agent_name']} | Customer: {inv['customer_name']}")
        print(f"Total Associated EPP items: {len(items)}")
        for idx, it in enumerate(items, 1):
            print(f"  Item {idx}:")
            print(f"    ID: {it['bubble_id']}")
            print(f"    Description: {it['description']!r}")
            print(f"    Amount: RM {float(it['amount'] or it['unit_price'] or 0):,.2f}")
            print(f"    linked_invoice: {it['linked_invoice']}")

print(f"\nTotal invoices with duplicate/multiple EPP items: {dupe_count}")
