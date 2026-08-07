import os
import sys
from pathlib import Path
import json

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from api_client import query_sql
from dotenv import load_dotenv

for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    print("Checking HO LEE KING records:")
    print("=" * 60)
    
    # 1. Customers matching 'Ho Lee King'
    custs = query_sql("SELECT customer_id, name, email FROM customer WHERE name ILIKE '%Ho Lee King%';")
    print("Customers:")
    for c in custs:
        print(c)
    print()
    
    if not custs:
        return
        
    cust_ids = [c['customer_id'] for c in custs]
    ids_placeholder = ", ".join(f"'{cid}'" for cid in cust_ids)
    
    # 2. Invoices matching these customers
    invoices = query_sql(f"""
        SELECT bubble_id, id, invoice_number, total_amount, paid_amount, paid, full_payment_date, is_deleted, linked_agent, invoice_date, package_type, package_name_snapshot, description
        FROM invoice
        WHERE linked_customer IN ({ids_placeholder}) OR customer_name_snapshot ILIKE '%Ho Lee King%';
    """)
    print("Invoices:")
    for i in invoices:
        print(i)
    print()
    
    # 3. Payments matching these invoices
    for i in invoices:
        b_id = i['bubble_id']
        inv_num = i['invoice_number']
        payments = query_sql(f"SELECT id, amount, payment_date, payment_method, remark FROM payment WHERE linked_invoice = '{b_id}';")
        print(f"Payments for Invoice {inv_num} ({b_id}):")
        for p in payments:
            print(p)
        print()

if __name__ == '__main__':
    main()
