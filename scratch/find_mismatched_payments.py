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
    print("Finding mismatched invoices where full payment date exists but total payment amount < invoice total amount...")
    # Fetch all 2026 invoices where full_payment_date is set in the database, or where we compute full payment
    # Let's join invoice, agent, payment
    sql = """
    SELECT 
        i.invoice_number,
        i.invoice_date,
        i.total_amount,
        i.full_payment_date,
        i.customer_name_snapshot,
        c.name AS customer_name,
        a.name AS agent_name,
        COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)), 0)::numeric AS paid_sum
    FROM invoice i
    JOIN agent a ON a.bubble_id = i.linked_agent
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    WHERE i.total_amount > 0 
      AND (i.full_payment_date IS NOT NULL OR EXISTS (
          SELECT 1 FROM payment p WHERE p.linked_invoice = i.bubble_id
      ))
      AND (extract(year from i.invoice_date) = 2026 OR i.customer_name_snapshot ILIKE '%Lee Chui Yin%')
    ORDER BY a.name, i.invoice_number;
    """
    rows = query_sql(sql)
    
    mismatches = []
    for r in rows:
        total = float(r['total_amount'] or 0)
        paid = float(r['paid_sum'] or 0)
        diff = total - paid
        pct = (paid / total) * 100 if total > 0 else 0
        
        # Check if full_payment_date is set in DB, or if we want to check for Lee Chui Yin
        db_has_full = r['full_payment_date'] is not None
        
        # If paid < 99% but DB thinks it has full_payment_date
        if db_has_full and pct < 99.0:
            mismatches.append({
                'invoice_number': r['invoice_number'],
                'invoice_date': r['invoice_date'],
                'customer_name': r['customer_name_snapshot'] or r['customer_name'] or '(unknown)',
                'agent_name': r['agent_name'],
                'total_amount': total,
                'paid_sum': paid,
                'pct': pct,
                'full_payment_date': r['full_payment_date']
            })
            
    print(f"\nFound {len(mismatches)} mismatched invoices:")
    for m in mismatches:
        print(f"Agent: {m['agent_name']} | Invoice: {m['invoice_number']} | Cust: {m['customer_name']}")
        print(f"  Invoice Date: {m['invoice_date'][:10]} | Full Pay Date (DB): {m['full_payment_date']}")
        print(f"  Total Amount: RM {m['total_amount']:,.2f} | Paid Sum: RM {m['paid_sum']:,.2f} ({m['pct']:.1f}%)")
        print("-" * 60)

if __name__ == '__main__':
    main()
