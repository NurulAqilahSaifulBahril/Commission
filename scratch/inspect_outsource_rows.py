import os
import sys
from pathlib import Path

# Resolve directories
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(REPO_ROOT / "7. Presentation"))

import build_commission_pack

# Load env variables
build_commission_pack._load_env_files()

# Preload modules
basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"
build_commission_pack._load_module("out_basic_commission", basic_path)
nfp_dir = REPO_ROOT / "2. NFP Commission" / "3. Python script"
build_commission_pack._load_module("out_nfp_commission", nfp_dir / "outsource_nfp_commission.py")
build_commission_pack._load_module("out_nfp_paths", nfp_dir / "nfp_paths.py")
build_commission_pack._load_module("out_nfp_paths2", nfp_dir / "nfp_paths.py")

year = 2026
month = 5

basic_mod = sys.modules["out_basic_commission"]
invoice_dates_map = build_commission_pack.fetch_invoice_dates(year, basic_mod)

out_basic_t1, out_basic_invoices, out_basic_factory, out_basic_meta, out_basic_lines = build_commission_pack.fetch_outsource_basic(year)
out_anp_summary, out_anp_detail, out_anp_meta = build_commission_pack.fetch_outsource_anp(year)
out_nfp_agent, out_nfp_detail, out_nfp_meta, out_nfp_rows = build_commission_pack.fetch_outsource_nfp(year)

out_anp_detail_filtered = [r for r in out_anp_detail if build_commission_pack._parse_month(r.get("invoice_date")) == month]
out_basic_lines_filtered = [ln for ln in out_basic_lines if build_commission_pack._parse_month(ln.full_payment_date) == month]
out_nfp_rows_filtered = [r for r in out_nfp_rows if build_commission_pack._parse_month(r.full_payment_date) == month]

agent_summary, customer_summary, customer_anp_summary = build_commission_pack.build_outsource_summary_tables(
    basic_t1=out_basic_t1,
    basic_lines=out_basic_lines_filtered,
    basic_meta=out_basic_meta,
    nfp_agent_rows=out_nfp_agent,
    nfp_rows=out_nfp_rows_filtered,
    anp_summary_rows=[],
    anp_detail=out_anp_detail_filtered,
    year=year,
    invoice_dates_map=invoice_dates_map,
    month=month
)

print("Agent summary rows for month 5:")
for idx, row in enumerate(agent_summary.get(month, [])):
    print(f"Row {idx}: {row}")

print("\nCustomer summary rows for month 5:")
for idx, row in enumerate(customer_summary.get(month, [])):
    print(f"Row {idx}: {row}")
