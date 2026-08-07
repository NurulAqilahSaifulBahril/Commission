import os
import sys
from pathlib import Path

# Add paths to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
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
    print("Broad search for 'Chui Yin' in customer...")
    customers = query_sql("SELECT * FROM customer WHERE name ILIKE '%Chui Yin%';")
    for c in customers:
        print(f"Customer: {c}")
        
    print("\nBroad search for 'Chui Yin' in invoice (customer_name_snapshot)...")
    invoices = query_sql("SELECT * FROM invoice WHERE customer_name_snapshot ILIKE '%Chui Yin%';")
    for i in invoices:
        print(f"Invoice: {i}")

    print("\nBroad search for 'Chui Yin' in payments (remark/payment_method)...")
    payments = query_sql("SELECT * FROM payment WHERE remark ILIKE '%Chui Yin%' OR payment_method ILIKE '%Chui Yin%';")
    for p in payments:
        print(f"Payment: {p}")

    print("\nSearch for all invoices and payments linked to customer LEE CHUI YIN...")
    # Let's search all customer records that match
    for c in customers:
        cid = c['customer_id']
        invs = query_sql(f"SELECT id, bubble_id, invoice_number, total_amount, full_payment_date FROM invoice WHERE linked_customer = '{cid}';")
        for inv in invs:
            print(f"  Linked Invoice: {inv}")
            # get payments
            pay = query_sql(f"SELECT * FROM payment WHERE linked_invoice = '{inv['bubble_id']}';")
            for p in pay:
                print(f"    Payment: {p}")

if __name__ == '__main__':
    main()
