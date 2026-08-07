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
    SELECT bubble_id, invoice_number, customer_name_snapshot, panel_rating, panel_qty,
           total_amount, description, invoice_date
    FROM invoice
    WHERE is_deleted IS NOT TRUE AND customer_name_snapshot ILIKE '%chuah%';
""")
print(f"Found {len(rows)} invoice(s) matching 'chuah'")
for r in rows:
    print(r.get('customer_name_snapshot'), '|', r.get('invoice_number'), '|', r.get('panel_rating'), '|', r.get('invoice_date'))
