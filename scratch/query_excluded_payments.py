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
    print("Fetching details for excluded payment IDs...")
    sql = """
    SELECT id, amount, payment_date, payment_method, remark, linked_invoice, linked_customer
    FROM payment
    WHERE id IN (101334, 104412, 101333, 104413, 4899);
    """
    rows = query_sql(sql)
    for r in rows:
        print(r)

if __name__ == '__main__':
    main()
