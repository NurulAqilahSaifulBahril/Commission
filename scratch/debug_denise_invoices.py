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
    print("Searching for all invoices of Denise Ng Pei sing...")
    invoice_rows = query_sql("""
        SELECT i.id, i.bubble_id, i.invoice_number, i.invoice_date, i.total_amount, i.full_payment_date, i.linked_customer, i.customer_name_snapshot, a.name as agent_name
        FROM invoice i
        JOIN agent a ON a.bubble_id = i.linked_agent
        WHERE a.name ILIKE '%Denise%'
        ORDER BY i.invoice_date;
    """)
    for r in invoice_rows:
        print(f"Invoice Number: {r['invoice_number']}, Date: {r['invoice_date']}, Total: {r['total_amount']}, Full Pay Date: {r['full_payment_date']}, Cust: {r['customer_name_snapshot'] or r['linked_customer']}")
        # Fetch payments
        inv_bubble_id = r['bubble_id']
        payments = query_sql(f"""
            SELECT id, amount, payment_date, payment_method, remark, epp_type
            FROM payment
            WHERE linked_invoice = '{inv_bubble_id}'
            ORDER BY payment_date, id;
        """)
        sum_payments = 0
        for p in payments:
            print(f"  Payment ID: {p['id']}, Amount: {p['amount']}, Date: {p['payment_date']}")
            sum_payments += float(p['amount'])
        print(f"Total Payments: RM {sum_payments:,.2f}")
        print("-" * 60)

if __name__ == '__main__':
    main()
