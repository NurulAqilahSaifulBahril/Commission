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
    # 1. Query customer records
    cust = query_sql("SELECT customer_id, name, email FROM customer WHERE name ILIKE '%Hosaini%';")
    print("CUSTOMER RECORDS:")
    for c in cust:
        print(c)
    print("\n")
    
    # 2. Query all invoice records
    invs = query_sql("""
    SELECT bubble_id, invoice_number, total_amount, paid_amount, percent_of_total_amount, paid, full_payment_date, is_deleted, created_date, linked_customer
    FROM invoice 
    WHERE linked_customer IN (SELECT customer_id FROM customer WHERE name ILIKE '%Hosaini%');
    """)
    print("INVOICE RECORDS:")
    for i in invs:
        print(i)
    print("\n")
    
    # 3. Query all payments linked to these invoices
    for i in invs:
        b_id = i['bubble_id']
        inv_num = i['invoice_number']
        payments = query_sql(f"""
        SELECT id, bubble_id, amount, payment_date, created_date, payment_method, remark 
        FROM payment 
        WHERE linked_invoice = '{b_id}';
        """)
        print(f"PAYMENTS FOR INVOICE {inv_num} ({b_id}):")
        for p in payments:
            print(p)
        print("\n")

if __name__ == '__main__':
    main()
