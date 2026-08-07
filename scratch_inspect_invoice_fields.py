import sys
sys.path.append("3. ANP Commission/3. Python Script")
import os
from dotenv import load_dotenv
load_dotenv()

from anp_commission import PostgresProxyClient, normalize_proxy_token

token = normalize_proxy_token(os.environ.get("PG_PROXY_TOKEN", ""))
client = PostgresProxyClient(
    base_url="https://pg-proxy-production.up.railway.app",
    token=token,
    db_name="prod_main"
)

# ── 1. What values exist in package_type? ────────────────────────────────────
print("=== package_type DISTINCT VALUES ===")
rows = client.query("""
    SELECT package_type, COUNT(*) as cnt
    FROM invoice
    WHERE package_type IS NOT NULL AND package_type != ''
    GROUP BY package_type
    ORDER BY cnt DESC
    LIMIT 30
""")
for r in rows:
    print(f"  {str(r['package_type']):<40} count={r['cnt']}")

# ── 2. Sample of customer_address_snapshot ───────────────────────────────────
print("\n=== customer_address_snapshot SAMPLES (first 15) ===")
rows = client.query("""
    SELECT invoice_number, package_type, customer_address_snapshot
    FROM invoice
    WHERE customer_address_snapshot IS NOT NULL
      AND customer_address_snapshot != ''
      AND is_deleted IS NOT TRUE
    LIMIT 15
""")
for r in rows:
    print(f"  {r['invoice_number']} | pkg={r['package_type']} | addr={r['customer_address_snapshot'][:80]}")

# ── 3. How many invoices have package_type filled vs NULL? ────────────────────
print("\n=== package_type coverage ===")
rows = client.query("""
    SELECT
      COUNT(*) FILTER (WHERE package_type IS NOT NULL AND package_type != '') AS has_package_type,
      COUNT(*) FILTER (WHERE package_type IS NULL OR package_type = '')       AS no_package_type,
      COUNT(*) as total
    FROM invoice
    WHERE is_deleted IS NOT TRUE
""")
for r in rows:
    print(f"  has_package_type: {r['has_package_type']} / {r['total']}")
    print(f"  no_package_type:  {r['no_package_type']} / {r['total']}")
