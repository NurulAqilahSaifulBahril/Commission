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
    cols_sql = """
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_name = 'payment'
    ORDER BY column_name;
    """
    cols = query_sql(cols_sql)
    for c in cols:
        print(f"{c['column_name']}: {c['data_type']}")

if __name__ == '__main__':
    main()
