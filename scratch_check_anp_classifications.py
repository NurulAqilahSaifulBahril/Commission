import sys
import os
from pathlib import Path
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')

sys.path.append("7. Presentation")
import build_commission_pack

build_commission_pack._load_env_files()
_, anp_det_rows, _ = build_commission_pack.fetch_internal_anp(2026)

target_names = [
    "hong chin jia",
    "cho chooi peng",
    "soh sing chon",
    "prema",
    "mandave singh gill",
    "mohamad anas najmi",
    "wong wei wei",
    "loh kah guan",
]

print("=== ANP CLASSIFICATIONS ===")
for name in target_names:
    matches = [r for r in anp_det_rows if name in str(r.get("customer_name")).lower()]
    if matches:
        for r in matches:
            print(f"  Customer: {r['customer_name']:<40} Invoice: {r['invoice_number']:<15} prop_type in ANP: {r.get('prop_type')}")
    else:
        print(f"  Customer: {name:<40} No match found in ANP details!")
