import sys
sys.path.append("3. ANP Commission/3. Python Script")
import os
from dotenv import load_dotenv
load_dotenv()

from anp_commission import PostgresProxyClient, normalize_proxy_token, classify_property_type

token = normalize_proxy_token(os.environ.get("PG_PROXY_TOKEN", ""))
client = PostgresProxyClient(
    base_url="https://pg-proxy-production.up.railway.app",
    token=token,
    db_name="prod_main"
)

# customer_ids from previous search
target_cust_ids = [
    'cust_7ae31a77',                         # AW MUN LAI (ATAP)
    '1745393849459x685291549531832300',       # HO LEE FONG
    '1764749658816x436086054365691900',       # HO LEE KIAN (ATAP)
    '1733465946530x329802689425965060',       # HO LEE KING
]
ids_sql = ", ".join(f"'{c}'" for c in target_cust_ids)

print("=== INVOICES FOR TARGET CUSTOMERS ===")
rows = client.query(f"""
    SELECT
        i.invoice_number,
        i.linked_customer,
        i.customer_name_snapshot,
        i.package_type,
        i.package_name_snapshot,
        i.description,
        i.customer_address_snapshot,
        COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
        ref.project_type AS referral_project_type
    FROM invoice i
    LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
    LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
    LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
    WHERE i.is_deleted IS NOT TRUE
      AND i.linked_customer IN ({ids_sql})
    ORDER BY i.linked_customer, i.invoice_date
""")

print(f"Found {len(rows)} invoices\n")

# Also check all ANP detail rows to understand what's producing "Shop Lot"
# Load internal ANP commission data
sys.path.append("7. Presentation")
import build_commission_pack
build_commission_pack._load_env_files()
_, det_rows, _ = build_commission_pack.fetch_internal_anp(2026)

# Find rows for these customers
target_names = ['aw mun lai', 'ho lee fong', 'ho lee kian', 'ho lee king']
print("=== ANP DETAIL ROWS MATCHING TARGET CUSTOMERS ===")
matches = [r for r in det_rows if any(t in (r.get('customer_name') or '').lower() for t in target_names)]
print(f"Found {len(matches)} ANP detail rows")
for r in matches:
    print(f"  Agent={r['agent_name']}  Customer={r['customer_name']}  Invoice={r['invoice_number']}  prop_type={r['prop_type']}")

# ── Check what raw invoice data looks like for those customers ────────────────
print("\n=== RAW INVOICE CLASSIFICATION TRACE ===")
for r in rows:
    classified = classify_property_type(r)
    print(f"\n  Invoice:  {r['invoice_number']}")
    print(f"  Customer: {r['customer_name_snapshot']}")
    print(f"  Address:  {(r['customer_address_snapshot'] or '')[:70]}")
    print(f"  pkg_type: '{r['package_type']}'")
    print(f"  pkg_name: '{r['package_name_snapshot']}'")
    print(f"  desc:     '{(r['description'] or '')[:70]}'")
    print(f"  seda_nem: '{r['seda_nem_type']}'")
    print(f"  ref_proj: '{r['referral_project_type']}'")
    print(f"  >>> CLASSIFIED AS: {classified}")
