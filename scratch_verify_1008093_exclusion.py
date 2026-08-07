import os
import sys
from pathlib import Path

# Add paths to sys.path
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

import nfp_commission
from dotenv import load_dotenv

# Load token
for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    results, summary = nfp_commission.build_report(2026)
    found = [r for r in results if r.invoice_number == '1008093']
    print(f"Found invoice 1008093 in NFP report results: {len(found)}")
    for r in found:
        print(f"  Agent: {r.agent_name} | Customer: {r.customer_name} | Commission: {r.nfp_commission} | pct75: {r.pct75_date}")

if __name__ == '__main__':
    main()
