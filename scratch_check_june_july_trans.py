import os
import sys
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(r"c:\Users\User\OneDrive\Documents\Commission")
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from api_client import query_sql

for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    sql = """
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
          AND p.id NOT IN (101334, 104412, 101333, 104413, 4899)
      ) sub
      WHERE sub.running_total >= sub.total_amount * 0.75
      GROUP BY sub.linked_invoice
    )
    SELECT 
      i.bubble_id,
      i.invoice_number,
      i.invoice_date,
      i.full_payment_date,
      pct75.pct75_date,
      i.total_amount,
      i.paid,
      a.name AS agent_name,
      a.agent_type,
      c.name AS customer_name
    FROM invoice i
    INNER JOIN agent a ON a.bubble_id = i.linked_agent
    LEFT JOIN customer c ON c.customer_id = i.linked_customer
    LEFT JOIN pct75 ON pct75.linked_invoice = i.bubble_id
    WHERE COALESCE(i.is_deleted, FALSE) IS NOT TRUE
      AND pct75.pct75_date IS NOT NULL
      AND (
        -- case 1: pct75 in Jun 2026 or before, but full_payment_date in Jul 2026 or later (or null)
        (
          EXTRACT(YEAR FROM pct75.pct75_date) = 2026 AND EXTRACT(MONTH FROM pct75.pct75_date) <= 6
          AND (
            i.full_payment_date IS NULL
            OR (EXTRACT(YEAR FROM i.full_payment_date) = 2026 AND EXTRACT(MONTH FROM i.full_payment_date) >= 7)
            OR EXTRACT(YEAR FROM i.full_payment_date) > 2026
          )
        )
      )
    ORDER BY pct75.pct75_date ASC;
    """
    rows = query_sql(sql)
    print(f"Found {len(rows)} transition invoices:")
    for idx, r in enumerate(rows, 1):
        print(f"{idx}. Inv: {r['invoice_number']} | Cust: {r['customer_name']} | Agent: {r['agent_name']} | Type: {r['agent_type']}")
        print(f"   Amt: {r['total_amount']} | Paid Status: {r['paid']} | 75% Date: {r['pct75_date']} | 100% Date: {r['full_payment_date']}")
        print("-" * 50)

if __name__ == '__main__':
    main()
