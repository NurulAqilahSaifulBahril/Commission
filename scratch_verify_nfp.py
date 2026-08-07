import sys
import pandas as pd
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from net_floor_prices import load_650w_schedules, round_nfp, schedule_amount_round

excel_path = REPO_ROOT / "2. NFP Commission" / "1. Excel" / "Net Floor Price String Inverter.xlsx"
xls = pd.ExcelFile(excel_path)

schedules = load_650w_schedules(excel_path)

print("Verifying NFP calculations against Excel sheet values...")
print("=" * 80)

for sheet in xls.sheet_names:
    print(f"\nSheet: {sheet}")
    df = pd.read_excel(xls, sheet, header=None)
    
    # Find the header row
    header_row_idx = None
    for r_idx in range(len(df)):
        row_text = " ".join(str(v).lower() for v in df.iloc[r_idx] if pd.notna(v))
        if "no.panels" in row_text:
            header_row_idx = r_idx
            break
            
    if header_row_idx is None:
        print("  Could not find header row (no.panels)!")
        continue
        
    parsed_tables = schedules.get(sheet, [])
    mismatches = 0
    
    # Configure columns based on the sheet name
    if sheet in ("NOV - DEC 2025", "JAN 2026"):
        # Side-by-side layout 1:
        # Table 1: Panels in Col 0, Final Price in Col 7. No TNG.
        # Table 2: Panels in Col 0, Final Price in Col 21. No TNG.
        t1_panels_col = 0
        t1_final_col = 7
        t1_tng_col = None
        
        t2_panels_col = 0
        t2_final_col = 21
        t2_tng_col = None
        
        has_t2 = True
    elif sheet == "MAY 2026":
        # Side-by-side layout 2:
        # Table 1: Panels in Col 0, Final Price in Col 8, With TNG in Col 10.
        # Table 2: Panels in Col 14, Final Price in Col 22, With TNG in Col 24.
        t1_panels_col = 0
        t1_final_col = 8
        t1_tng_col = 10
        
        t2_panels_col = 14
        t2_final_col = 22
        t2_tng_col = 24
        
        has_t2 = True
    elif sheet == "FEB 2026":
        # Single Table: Panels in Col 0, Final Price in Col 8, With TNG in Col 10.
        t1_panels_col = 0
        t1_final_col = 8
        t1_tng_col = 10
        has_t2 = False
    elif sheet == "MAC 2026":
        # Single Table: Panels in Col 0, Final Price in Col 9, With TNG in Col 11.
        t1_panels_col = 0
        t1_final_col = 9
        t1_tng_col = 11
        has_t2 = False
    elif sheet == "APR 2026":
        # Single Table: Panels in Col 0, Final Price in Col 8, With TNG in Col 10.
        t1_panels_col = 0
        t1_final_col = 8
        t1_tng_col = 10
        has_t2 = False
    else:
        print(f"  Unknown sheet: {sheet}")
        continue

    t1_parsed = parsed_tables[0] if len(parsed_tables) > 0 else {}
    t2_parsed = parsed_tables[1] if len(parsed_tables) > 1 else {}
    
    # Iterate through rows
    r_idx = header_row_idx + 1
    while r_idx < len(df):
        r = df.iloc[r_idx]
        
        # Verify Table 1
        if pd.notna(r.iloc[t1_panels_col]):
            try:
                panels_1 = int(float(r.iloc[t1_panels_col]))
                
                # Check Final Price
                excel_t1_final = r.iloc[t1_final_col]
                if pd.notna(excel_t1_final):
                    calc_val = t1_parsed.get(panels_1).final_price if t1_parsed.get(panels_1) else None
                    if calc_val is not None:
                        excel_rounded = schedule_amount_round(excel_t1_final, sheet)
                        if calc_val != excel_rounded:
                            print(f"  T1 Mismatch for {panels_1} panels: Excel={excel_t1_final} (rounded={excel_rounded}), Calculated={calc_val}")
                            mismatches += 1
                            
                # Check With TNG
                if t1_tng_col is not None and pd.notna(r.iloc[t1_tng_col]):
                    excel_t1_tng = r.iloc[t1_tng_col]
                    calc_tng = t1_parsed.get(panels_1).final_with_tng if t1_parsed.get(panels_1) else None
                    if calc_tng is not None:
                        excel_tng_rounded = schedule_amount_round(excel_t1_tng, sheet)
                        if calc_tng != excel_tng_rounded:
                            print(f"  T1 TNG Mismatch for {panels_1} panels: Excel={excel_t1_tng} (rounded={excel_tng_rounded}), Calculated={calc_tng}")
                            mismatches += 1
            except (ValueError, TypeError):
                pass
                
        # Verify Table 2 (if present)
        if has_t2 and pd.notna(r.iloc[t2_panels_col]):
            try:
                panels_2 = int(float(r.iloc[t2_panels_col]))
                
                # Check Final Price
                excel_t2_final = r.iloc[t2_final_col]
                if pd.notna(excel_t2_final):
                    calc_val = t2_parsed.get(panels_2).final_price if t2_parsed.get(panels_2) else None
                    if calc_val is not None:
                        excel_rounded = schedule_amount_round(excel_t2_final, sheet)
                        if calc_val != excel_rounded:
                            print(f"  T2 Mismatch for {panels_2} panels: Excel={excel_t2_final} (rounded={excel_rounded}), Calculated={calc_val}")
                            mismatches += 1
                            
                # Check With TNG
                if t2_tng_col is not None and pd.notna(r.iloc[t2_tng_col]):
                    excel_t2_tng = r.iloc[t2_tng_col]
                    calc_tng = t2_parsed.get(panels_2).final_with_tng if t2_parsed.get(panels_2) else None
                    if calc_tng is not None:
                        excel_tng_rounded = schedule_amount_round(excel_t2_tng, sheet)
                        if calc_tng != excel_tng_rounded:
                            print(f"  T2 TNG Mismatch for {panels_2} panels: Excel={excel_t2_tng} (rounded={excel_tng_rounded}), Calculated={calc_tng}")
                            mismatches += 1
            except (ValueError, TypeError):
                pass

        r_idx += 1
        
    if mismatches == 0:
        if has_t2:
            print("  Table 1 & 2 verified: OK!")
        else:
            print("  Standard Table verified: OK!")
