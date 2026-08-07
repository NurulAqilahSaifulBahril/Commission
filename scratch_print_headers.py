import sys
import pandas as pd
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

excel_path = Path("2. NFP Commission/1. Excel/Net Floor Price String Inverter.xlsx")
xls = pd.ExcelFile(excel_path)

for sheet in xls.sheet_names:
    print(f"\n=================== Sheet: {sheet} ===================")
    df = pd.read_excel(xls, sheet, header=None)
    header_row_idx = None
    for r_idx in range(len(df)):
        row_text = " ".join(str(v).lower() for v in df.iloc[r_idx] if pd.notna(v))
        if "no.panels" in row_text:
            header_row_idx = r_idx
            break
    if header_row_idx is not None:
        print(f"Header row index: {header_row_idx}")
        row = df.iloc[header_row_idx]
        for col_idx, val in enumerate(row):
            if pd.notna(val) and str(val).strip():
                print(f"  Col {col_idx}: {str(val).strip()!r}")
        
        # Also print first 5 rows under header
        print("Data sample:")
        for r_offset in range(1, 4):
            if header_row_idx + r_offset < len(df):
                r_vals = df.iloc[header_row_idx + r_offset].tolist()
                print(f"  Row {header_row_idx + r_offset}: {r_vals}")
    else:
        print("Header row not found!")
