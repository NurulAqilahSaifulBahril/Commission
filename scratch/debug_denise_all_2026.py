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
    print("Listing 2026 invoices for Denise with payments or full payment date...")
    sql = """
    SELECT 
        i.invoice_number,
        i.invoice_date,
        i.total_amount,
        i.full_payment_date,
        i.customer_name_snapshot,
        c.name AS customer_name,
        COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)), 0)::numeric AS paid_sum
    FROM invoice i
    JOIN agent a ON a.bubble_id = i.linked_agent
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    WHERE a.name ILIKE '%Denise%'
      AND (extract(year from i.invoice_date) = 2026 OR i.customer_name_snapshot ILIKE '%Lee Chui Yin%')
      AND (
        i.full_payment_date IS NOT NULL 
        OR COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)), 0) > 0
      )
    ORDER BY i.invoice_number;
    """
    rows = query_sql(sql)
    for r in rows:
        total = float(r['total_amount'] or 0)
        paid = float(r['paid_sum'] or 0)
        pct = (paid / total * 100) if total > 0 else 0
        print(f"Invoice: {r['invoice_number']} | Cust: {r['customer_name_snapshot'] or r['customer_name']} | Total: RM {total:,.2f} | Paid: RM {paid:,.2f} ({pct:.1f}%) | DB Full Pay Date: {r['full_payment_date']}")

if __name__ == '__main__':
    main()
