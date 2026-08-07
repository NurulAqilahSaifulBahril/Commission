import os
import sys
from pathlib import Path

# Add paths to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "7. Presentation"))

import build_commission_pack
from dotenv import load_dotenv

# Load token
for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def main():
    print("Running fetch_internal_basic for 2026...")
    int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = build_commission_pack.fetch_internal_basic(2026)
    
    print("\nLooking for 'Lee Chui Yin' in lines...")
    found_any = False
    for ln in int_basic_lines:
        if 'Lee Chui Yin' in ln.customer_name or 'Chui Yin' in ln.customer_name:
            found_any = True
            print(f"Line: {ln}")
            
    print("\nLooking for 'Lee Chui Yin' in Table 2 (Residential and Shop Lot)...")
    for row in int_basic_t2:
        if any('Chui Yin' in str(cell) for cell in row):
            print(f"Table 2 Row: {row}")
            
    print("\nLooking for 'Lee Chui Yin' in Table 3 (Factory)...")
    for row in int_basic_t3:
        if any('Chui Yin' in str(cell) for cell in row):
            print(f"Table 3 Row: {row}")

if __name__ == '__main__':
    main()
