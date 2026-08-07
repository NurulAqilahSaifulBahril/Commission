import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from api_client import query_sql
from dotenv import load_dotenv

for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    sql = """
    SELECT i.invoice_number, i.invoice_date, a.name AS agent_name, a.agent_type, i.package_type, i.total_amount
    FROM invoice i
    JOIN agent a ON i.linked_agent = a.bubble_id
    WHERE i.invoice_number IN ('INV-1008772', 'INV-1008773');
    """
    rows = query_sql(sql)
    for r in rows:
        print(r)

if __name__ == '__main__':
    main()
