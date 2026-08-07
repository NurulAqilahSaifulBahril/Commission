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
    sql = """
    SELECT 
        i.invoice_number, i.total_amount, i.paid_amount, i.percent_of_total_amount,
        i.linked_agent, a.name as agent_name, a.agent_type, c.name as customer_name,
        i.paid, i.full_payment_date
    FROM invoice i 
    LEFT JOIN agent a ON i.linked_agent = a.bubble_id 
    LEFT JOIN customer c ON i.linked_customer = c.customer_id 
    WHERE i.invoice_number IN ('1008093', '1008042')
    """
    rows = query_sql(sql)
    for r in rows:
        print(r)

if __name__ == '__main__':
    main()
