import sys
import pandas as pd
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent
excel_path = REPO_ROOT / "2. NFP Commission" / "1. Excel" / "Net Floor Price String Inverter.xlsx"

xls = pd.ExcelFile(excel_path)
df = pd.read_excel(xls, "MAY 2026", header=None)

# Headers row is at index 1
headers = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[1]]

for r_idx in range(15):
    row = df.iloc[r_idx]
    # Print Table 1 cols (0 to 10) and Table 2 cols (14 to 24)
    t1_vals = [row.iloc[c] for c in range(11)]
    t2_vals = [row.iloc[c] for c in range(14, 25)] if len(row) > 24 else []
    print(f"Row {r_idx:2d}:")
    print(f"  T1: {t1_vals}")
    print(f"  T2: {t2_vals}")
