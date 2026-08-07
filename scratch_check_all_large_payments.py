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
    sql = """
    SELECT 
        p.id, p.bubble_id, p.amount, p.payment_date, p.linked_invoice, p.created_at,
        i.invoice_number, i.total_amount,
        c.name as customer_name
    FROM payment p
    LEFT JOIN invoice i ON i.bubble_id = p.linked_invoice
    LEFT JOIN customer c ON i.linked_customer = c.customer_id
    WHERE p.amount IN ('22960', '9000')
    ORDER BY p.amount, p.payment_date;
    """
    rows = query_sql(sql)
    print("ALL PAYMENTS OF 22960 or 9000:")
    for r in rows:
        print(f"PayID: {r['id']} | Amt: {r['amount']} | Date: {r['payment_date']} | Inv#: {r['invoice_number']} | Cust: {r['customer_name']} | CreatedAt: {r['created_at']}")

if __name__ == '__main__':
    main()
