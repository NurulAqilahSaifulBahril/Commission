import sys
from pathlib import Path

# Add presentation folder to path
sys.path.append(str(Path(r"c:\Users\User\OneDrive\Documents\Commission\7. Presentation")))
import build_commission_pack

build_commission_pack._load_env_files()
print("Fetching basic lines...")
_, _, _, _, _, basic_lines = build_commission_pack.fetch_internal_basic(2026, h1_only=False)

print(f"Found {len(basic_lines)} total basic lines.")
for ln in basic_lines:
    if "qin" in ln.customer_name.lower():
        print("-" * 60)
        print(f"Customer Name: {ln.customer_name}")
        print(f"Invoice Number: {ln.invoice_number}")
        print(f"ln.package: '{ln.package}'")
        print(f"get_invoice_package output: '{build_commission_pack.get_invoice_package(ln.invoice_number, ln)}'")
        print(f"Has attributes: {ln.__dict__}")
