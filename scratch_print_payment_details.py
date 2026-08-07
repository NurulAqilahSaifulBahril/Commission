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
    SELECT id, bubble_id, amount, payment_date, linked_invoice, payment_method, remark, epp_type, unique_id, created_at
    FROM payment
    WHERE linked_invoice IN ('1764499254224x283571210562830340', '1763853827076x456132977863426050')
    ORDER BY linked_invoice, payment_date;
    """
    rows = query_sql(sql)
    for r in rows:
        print("Payment Details:")
        for k, v in r.items():
            print(f"  {k}: {v}")
        print("-" * 50)

if __name__ == '__main__':
    main()
