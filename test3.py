import sys
sys.path.append(r'C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script')
from nfp_commission import build_report
results, summary = build_report(2026)
for r in results:
    if r.invoice_number == '1008294':
        print(f'{r.invoice_number} | NFP: {r.net_floor_price} | Source: {r.nfp_source} | Package: {r.panel_qty} panels {r.panel_rating}W')
