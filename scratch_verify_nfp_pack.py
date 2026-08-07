import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# Load NFP script
nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
sys.path.insert(0, str(nfp_dir))
import nfp_commission

# Run report for 2026
rows, summary = nfp_commission.build_report(2026)
print(f"Total rows returned by nfp_commission.build_report(2026): {len(rows)}")

rows_with_full_payment = [r for r in rows if r.full_payment_date]
print(f"Rows with full_payment_date not None: {len(rows_with_full_payment)}")

rows_without_full_payment = [r for r in rows if not r.full_payment_date]
print(f"Rows with full_payment_date IS None: {len(rows_without_full_payment)}")

if rows_without_full_payment:
    print("Example rows without full_payment_date:")
    for r in rows_without_full_payment[:5]:
        print(f"  Invoice: {r.invoice_number} | Agent: {r.agent_name} | Customer: {r.customer_name} | Date: {r.invoice_date}")
else:
    print("No rows without full_payment_date!")
