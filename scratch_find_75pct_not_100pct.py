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

# Query invoices
SQL = """
WITH pct75 AS (
  SELECT sub.linked_invoice,
         MIN(sub.payment_date) AS pct75_date
  FROM (
    SELECT p.linked_invoice,
           p.payment_date,
           SUM(p.amount) OVER (
             PARTITION BY p.linked_invoice
             ORDER BY p.payment_date ASC, p.id ASC
           ) AS running_total,
           i.total_amount
    FROM payment p
    JOIN invoice i ON i.bubble_id = p.linked_invoice
    WHERE i.total_amount > 0
  ) sub
  WHERE sub.running_total >= sub.total_amount * 0.75
  GROUP BY sub.linked_invoice
)
SELECT 
    i.invoice_number,
    COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name) AS customer_name,
    a.name AS agent_name,
    a.agent_type,
    i.total_amount,
    pct75.pct75_date,
    i.full_payment_date
FROM invoice i
INNER JOIN agent a ON a.bubble_id = i.linked_agent
LEFT JOIN customer c ON c.customer_id = i.linked_customer
LEFT JOIN pct75 ON pct75.linked_invoice = i.bubble_id
WHERE pct75.pct75_date IS NOT NULL
  AND i.full_payment_date IS NULL
  AND COALESCE(i.is_deleted, FALSE) IS NOT TRUE
ORDER BY pct75.pct75_date ASC;
"""

def main():
    token = os.getenv("PG_PROXY_TOKEN") or os.getenv("POSTGRES_PROXY_TOKEN")
    if not token:
        print("Error: POSTGRES_PROXY_TOKEN / PG_PROXY_TOKEN not found.")
        return
        
    print(f"Using Token starting with: {token[:10]}...")
    rows = query_sql(SQL)
    print(f"\nFound {len(rows)} invoices that are >= 75% paid but NOT 100% paid:")
    print("-" * 100)
    print(f"{'Invoice #':<12} | {'Customer':<25} | {'Agent':<20} | {'Type':<12} | {'Amount':<10} | {'75% Date':<10}")
    print("-" * 100)
    for r in rows:
        print(f"{str(r.get('invoice_number')):<12} | {str(r.get('customer_name'))[:25]:<25} | {str(r.get('agent_name'))[:20]:<20} | {str(r.get('agent_type')):<12} | {float(r.get('total_amount', 0)):<10,.2f} | {str(r.get('pct75_date'))[:10]:<10}")

if __name__ == '__main__':
    main()
