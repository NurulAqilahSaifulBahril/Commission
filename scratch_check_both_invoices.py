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
    # Query invoice details for 1008093 and 1008042
    invoice_sql = """
    SELECT bubble_id, id, invoice_number, total_amount, paid_amount, percent_of_total_amount, paid, full_payment_date, is_deleted, customer_name_snapshot
    FROM invoice
    WHERE invoice_number IN ('1008093', '1008042') OR bubble_id IN ('1008093', '1008042');
    """
    invoices = query_sql(invoice_sql)
    print("INVOICE RECORDS FOR 1008093 and 1008042:")
    print("-" * 120)
    for inv in invoices:
        print(f"BubbleID: {inv['bubble_id']} | Inv#: {inv['invoice_number']} | Cust: {inv['customer_name_snapshot']} | Total: {inv['total_amount']} | Paid: {inv['paid_amount']} | Pct: {inv['percent_of_total_amount']} | PaidState: {inv['paid']} | FullPayDt: {inv['full_payment_date']}")
    print("\n")
    
    # Query payments for each BubbleID
    for inv in invoices:
        b_id = inv['bubble_id']
        inv_num = inv['invoice_number']
        payments_sql = f"""
        SELECT id, amount, payment_date, linked_invoice, epp_cost
        FROM payment
        WHERE linked_invoice = '{b_id}';
        """
        payments = query_sql(payments_sql)
        print(f"PAYMENTS FOR Invoice {inv_num} (BubbleID: {b_id}):")
        print("-" * 120)
        total_p = 0.0
        for p in payments:
            print(p)
            total_p += float(p.get('amount') or 0)
        print(f"Total Payments for Invoice {inv_num}: RM {total_p:,.2f}\n")

if __name__ == '__main__':
    main()
