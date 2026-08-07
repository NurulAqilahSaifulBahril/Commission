import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(REPO_ROOT / "7. Presentation"))

import build_commission_pack

build_commission_pack._load_env_files()

year = 2026
month = 7
print(f"Fetching outsource data for year {year}...")
try:
    basic_mod = build_commission_pack._load_module("out_basic_commission", REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py")
    invoice_dates_map = build_commission_pack.fetch_invoice_dates(year, basic_mod)
    
    out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = build_commission_pack.fetch_outsource_basic(year, h1_only=False)
    out_anp_summary, out_anp_detail, out_anp_meta = build_commission_pack.fetch_outsource_anp(year, h1_only=False)
    out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows = build_commission_pack.fetch_outsource_nfp(year, h1_only=False)
    
    print(f"Building summary tables for month {month}...")
    agent_summary, customer_summary, customer_anp_summary = build_commission_pack.build_outsource_summary_tables(
        basic_t1=out_basic_t1,
        basic_lines=out_basic_lines,
        basic_meta=out_basic_meta,
        nfp_agent_rows=out_nfp_agent,
        nfp_rows=out_nfp_rows,
        anp_summary_rows=[],
        anp_detail=out_anp_detail,
        year=year,
        invoice_dates_map=invoice_dates_map,
        month=month
    )
    
    rows = customer_summary.get(month, [])
    print(f"Customer summary rows for month {month}: {len(rows)} rows found")
    for r in rows:
        print(r)
        
except Exception as e:
    import traceback
    traceback.print_exc()
