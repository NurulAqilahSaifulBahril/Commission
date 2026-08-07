import os
import sys
import json
from pathlib import Path

REPO_ROOT = Path("c:/Users/User/OneDrive/Documents/Commission")

def _load_env():
    for candidate in (REPO_ROOT / ".env",):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

_load_env()
token = os.environ.get("PG_PROXY_TOKEN")

sys.path.insert(0, str(REPO_ROOT / "1. Basic Commission" / "3. Python Script"))
import outsource_basic_commission

print("Fetching raw outsource records...")
records = outsource_basic_commission.fetch_raw_outsource_records(2026)
qin_records = [r for r in records if "qin tcm" in str(r.get("customer_name") or "").lower()]

if qin_records:
    print(f"Found {len(qin_records)} records:")
    for r in qin_records:
        print("\nRecord:")
        print(json.dumps(r, indent=2))
        p_type = outsource_basic_commission.classify_property_type(r)
        print(f"Classified Property Type: {p_type}")
else:
    print("QIN TCM record not found in 2026!")
