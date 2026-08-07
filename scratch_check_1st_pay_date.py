import os
import sys
from pathlib import Path

# Add paths to sys.path
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from api_client import query_sql
from dotenv import load_dotenv

# Load token
for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    # Query invoice details
    invoices = query_sql("SELECT bubble_id, invoice_number, total_amount FROM invoice WHERE invoice_number IN ('1008042', '1008093')")
    for inv in invoices:
        b_id = inv['bubble_id']
        inv_num = inv['invoice_number']
        print(f"\nINVOICE {inv_num} (BubbleID: {b_id}) Total: {inv['total_amount']}")
        # Query items in linked_invoice_item array
        items_sql = f"""
        SELECT bubble_id, description, amount, unit_price 
        FROM invoice_item 
        WHERE bubble_id IN (
            SELECT unnest(linked_invoice_item) 
            FROM invoice 
            WHERE bubble_id = '{b_id}'
        )
        """
        items = query_sql(items_sql)
        for it in items:
            print(f"  - Item: {it['bubble_id']}")
            print(f"    Desc: {it['description']!r}")
            print(f"    Amount: {it['amount']} | Unit Price: {it['unit_price']}")




if __name__ == '__main__':
    main()
