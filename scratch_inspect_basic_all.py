import openpyxl
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding='utf-8')

file_path = Path(r"C:\Users\User\OneDrive\Documents\Commission\7. Presentation\Finance Output\Commission_Pack_2026_20260702_103015.xlsx")
wb = openpyxl.load_workbook(file_path, data_only=True)

for sheet_name in ['Basic - By Agent', 'Basic - By Invoice', 'Basic - Residential & Shop Lot']:
    ws = wb[sheet_name]
    print(f"\nSheet: {sheet_name}")
    for r_idx, row in enumerate(ws.iter_rows(values_only=True), 1):
        row_str = " ".join(str(cell) for cell in row if cell is not None)
        if "hosaini" in row_str.lower() or "1008772" in row_str.lower():
            print(f"  Line {r_idx}: {[str(c)[:25] for c in row if c is not None]}")
