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
    print("Checking details of payments for invoice 1770256085368x884236426547298300...")
    sql = """
    SELECT *
    FROM payment
    WHERE linked_invoice = '1770256085368x884236426547298300'
    ORDER BY id;
    """
    rows = query_sql(sql)
    for r in rows:
        print(f"Payment ID: {r['id']}")
        for k, v in sorted(r.items()):
            print(f"  {k}: {v}")
        print("-" * 60)

if __name__ == '__main__':
    main()
