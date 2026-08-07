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
    print("Fetching all invoices and their payments for Denise Ng Pei sing...")
    sql = """
    SELECT 
        i.invoice_number,
        i.invoice_date,
        i.total_amount,
        i.full_payment_date,
        i.customer_name_snapshot,
        c.name AS customer_name,
        p.id AS payment_id,
        p.amount AS payment_amount,
        p.payment_date AS payment_date
    FROM invoice i
    JOIN agent a ON a.bubble_id = i.linked_agent
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    LEFT JOIN payment p ON p.linked_invoice = i.bubble_id
    WHERE a.name ILIKE '%Denise%'
    ORDER BY i.invoice_number, p.payment_date, p.id;
    """
    rows = query_sql(sql)
    
    # Group by invoice
    invoices = {}
    for r in rows:
        inv_num = r['invoice_number']
        if inv_num not in invoices:
            invoices[inv_num] = {
                'invoice_number': inv_num,
                'invoice_date': r['invoice_date'] or '',
                'total_amount': r['total_amount'],
                'full_payment_date': r['full_payment_date'],
                'customer_name': r['customer_name_snapshot'] or r['customer_name'] or '(unknown)',
                'payments': []
            }
        if r['payment_id'] is not None:
            invoices[inv_num]['payments'].append({
                'id': r['payment_id'],
                'amount': r['payment_amount'],
                'date': r['payment_date']
            })
            
    for inv_num, inv in sorted(invoices.items()):
        total_amt = float(inv['total_amount'] or 0)
        if total_amt <= 0:
            continue
        # We only care about 2026 or if customer name matches Lee Chui Yin
        if 'Lee Chui Yin' not in inv['customer_name'] and not inv['invoice_date'].startswith('2026'):
            continue
            
        print(f"Invoice: {inv['invoice_number']} | Cust: {inv['customer_name']} | Date: {inv['invoice_date'][:10]} | Total: RM {total_amt:,.2f} | Full Pay Date (DB): {inv['full_payment_date']}")
        pay_sum = 0.0
        for p in inv['payments']:
            print(f"  Payment: ID={p['id']}, Amt=RM {float(p['amount'] or 0):,.2f}, Date={p['date'][:10]}")
            pay_sum += float(p['amount'] or 0)
        print(f"  Total Payments sum: RM {pay_sum:,.2f} ({(pay_sum / total_amt * 100):.1f}%)")
        print("-" * 80)

if __name__ == '__main__':
    main()
