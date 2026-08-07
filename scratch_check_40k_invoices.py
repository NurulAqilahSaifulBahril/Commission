import sys
import os
from pathlib import Path
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')

# Load database client from ANP Commission directory
sys.path.append("3. ANP Commission/3. Python Script")
from anp_commission import PostgresProxyClient, normalize_proxy_token

load_dotenv()
token = normalize_proxy_token(os.environ.get("PG_PROXY_TOKEN", ""))
client = PostgresProxyClient(
    base_url="https://pg-proxy-production.up.railway.app",
    token=token,
    db_name="prod_main"
)

# Fetch active 2026 invoices where Sales Price > 40000 and metadata is empty
query = """
SELECT 
    i.invoice_number,
    c.name AS db_customer_name,
    a.name AS agent_name,
    i.total_amount,
    i.effective_epp,
    i.invoice_date,
    i.full_payment_date,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
    ref.project_type AS referral_project_type
FROM invoice i
INNER JOIN agent a ON a.bubble_id = i.linked_agent
LEFT JOIN customer c ON c.customer_id = i.linked_customer
LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
WHERE i.is_deleted IS NOT TRUE
  AND (i.paid IS TRUE OR i."1st_payment_date" IS NOT NULL)
  AND (
    EXTRACT(YEAR FROM i.invoice_date) = 2026
    OR EXTRACT(YEAR FROM i.full_payment_date) = 2026
  )
  AND i.total_amount > 40000
"""

rows = client.query(query)
print(f"Total active 2026 invoices > 40k: {len(rows)}")

shop_lot_candidates = []
for r in rows:
    seda = str(r.get("seda_nem_type") or "").strip()
    ref = str(r.get("referral_project_type") or "").strip()
    pkg_type = str(r.get("package_type") or "").strip()
    pkg_name = str(r.get("package_name_snapshot") or "").strip()
    desc = str(r.get("description") or "").strip()
    
    # If all are empty, it fell back to the default
    if not (seda or ref or pkg_type or pkg_name or desc):
        shop_lot_candidates.append(r)

print(f"\nFound {len(shop_lot_candidates)} invoices > 40k that fell back to the default classification:")
for r in sorted(shop_lot_candidates, key=lambda x: float(x.get("total_amount") or 0), reverse=True):
    print(f"  Customer: {r.get('db_customer_name'):<50} Sales Price: {float(r.get('total_amount') or 0):<12,.2f} Invoice: {r.get('invoice_number'):<15}")
