import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Load from anp directory helper
sys.path.append("3. ANP Commission/3. Python Script")
from anp_commission import PostgresProxyClient, normalize_proxy_token, classify_property_type

load_dotenv()
token = normalize_proxy_token(os.environ.get("PG_PROXY_TOKEN", ""))
client = PostgresProxyClient(
    base_url="https://pg-proxy-production.up.railway.app",
    token=token,
    db_name="prod_main"
)

query = """
SELECT 
    i.invoice_number,
    i.customer_name_snapshot,
    c.name AS db_customer_name,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
    ref.project_type AS referral_project_type
FROM invoice i
LEFT JOIN customer c ON c.customer_id = i.linked_customer
LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
LEFT JOIN referral ref ON ref.bubble_id = i.linked_referral
WHERE i.is_deleted IS NOT TRUE
  AND (
    LOWER(i.customer_name_snapshot) LIKE '%aw mun lai%'
    OR LOWER(c.name) LIKE '%aw mun lai%'
    OR LOWER(i.customer_name_snapshot) LIKE '%ho lee fong%'
    OR LOWER(c.name) LIKE '%ho lee fong%'
    OR LOWER(i.customer_name_snapshot) LIKE '%ho lee%'
    OR LOWER(c.name) LIKE '%ho lee%'
  )
"""

rows = client.query(query)
print(f"Found {len(rows)} matching invoices in DB:")
for r in rows:
    print("-" * 50)
    print(f"Invoice: {r.get('invoice_number')}")
    print(f"Customer snapshot: {r.get('customer_name_snapshot')}")
    print(f"DB customer name: {r.get('db_customer_name')}")
    print(f"package_type: {r.get('package_type')}")
    print(f"package_name_snapshot: {r.get('package_name_snapshot')}")
    print(f"description: {r.get('description')}")
    print(f"seda_nem_type: {r.get('seda_nem_type')}")
    print(f"referral_project_type: {r.get('referral_project_type')}")
    print(f"classify_property_type: {classify_property_type(r)}")
