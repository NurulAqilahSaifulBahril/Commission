import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))
from api_client import query_sql
from dotenv import load_dotenv
for candidate in (REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env", REPO_ROOT / ".env"):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

rows = query_sql("""
    SELECT bubble_id, invoice_number, linked_customer, panel_rating, panel_qty,
           total_amount, description, invoice_date
    FROM invoice
    WHERE linked_customer = 'cust_47af3980' AND is_deleted IS NOT TRUE;
""")
print(f"Found {len(rows)} invoice(s)")
for r in rows:
    print("----")
    for k, v in r.items():
        print(f"  {k}: {v}")

ids = [r['bubble_id'] for r in rows]
if ids:
    items = query_sql("SELECT bubble_id, description, is_a_package, linked_invoice FROM invoice_item")
    for r in rows:
        print(f"\nItems for invoice {r['invoice_number']} ({r['bubble_id']}):")
        for it in items:
            if it['linked_invoice'] == r['bubble_id']:
                print(f"  is_a_package={it['is_a_package']} desc={it['description']}")
