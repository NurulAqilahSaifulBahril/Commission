import sys
from pathlib import Path

# Add presentation and dashboard to path
sys.path.append(str(Path(r"c:\Users\User\OneDrive\Documents\Commission\7. Presentation")))
import build_commission_pack

# Load NFP modules
build_commission_pack._load_env_files()
int_basic_t1, int_basic_t2, int_basic_t3, int_basic_t4, int_basic_meta, int_basic_lines = build_commission_pack.fetch_internal_basic(2026)
nfp_agent, nfp_detail, nfp_meta, nfp_rows = build_commission_pack.fetch_internal_nfp(2026)

print(f"Loaded {len(nfp_rows)} NFP rows.")
for i, r in enumerate(nfp_rows):
    pct75 = getattr(r, "pct75_date", None)
    full_pay = getattr(r, "full_payment_date", None)
    
    # Trace _effective_nfp_date logic
    try:
        eff_date = build_commission_pack._effective_nfp_date(r, int_basic_lines)
    except Exception as e:
        print(f"Error on row {i}: agent={getattr(r, 'agent_name', None)}, customer={getattr(r, 'customer_name', None)}")
        print(f"pct75={pct75} (type {type(pct75)}), full_pay={full_pay} (type {type(full_pay)})")
        import traceback
        traceback.print_exc()
        break
else:
    print("All rows processed successfully without throwing exceptions in _effective_nfp_date.")
