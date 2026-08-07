import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))
from api_client import query_sql
from dotenv import load_dotenv
for candidate in (REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env", REPO_ROOT / ".env"):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

rows = query_sql("SELECT bubble_id, name FROM customer WHERE name ILIKE '%chuah%';")
print(f"Found {len(rows)} customers matching 'chuah'")
for r in rows:
    print(r)
