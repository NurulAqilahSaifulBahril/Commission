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
    # 1. Query invoice table details
    invoice_sql = """
    SELECT bubble_id, id, invoice_number, total_amount, paid_amount, percent_of_total_amount, paid, full_payment_date, is_deleted
    FROM invoice
    WHERE invoice_number = '1008093' OR bubble_id = '1008093';
    """
    invoices = query_sql(invoice_sql)
    print("INVOICE TABLE RECORDS:")
    print("-" * 100)
    for inv in invoices:
        print(inv)
    print("\n")
    
    if not invoices:
        print("No invoice found with number/id 1008093")
        return
        
    bubble_id = invoices[0]['bubble_id']
    
    # 2. Query payments linked to this bubble_id
    payments_sql = f"""
    SELECT id, amount, payment_date, linked_invoice, epp_cost
    FROM payment
    WHERE linked_invoice = '{bubble_id}';
    """
    payments = query_sql(payments_sql)
    print("PAYMENTS LINKED TO INVOICE:")
    print("-" * 100)
    total_payments = 0
    for pay in payments:
        print(pay)
        total_payments += float(pay.get('amount') or 0)
    print("-" * 100)
    print(f"Sum of Payment Amounts: RM {total_payments:,.2f}")

if __name__ == '__main__':
    main()
