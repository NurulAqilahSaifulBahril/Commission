import os
import sys
import json
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent

def _load_env():
    for candidate in (
        REPO_ROOT / "1. Basic Commission" / "3. Python Script" / ".env",
        REPO_ROOT / ".env",
    ):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

_load_env()
token = os.environ.get("PG_PROXY_TOKEN")
if not token:
    token_file = REPO_ROOT / "pg_proxy_token.txt"
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()

proxy_url = "https://pg-proxy-production.up.railway.app/api/sql"
db_name = "prod_main"

def query_sql(sql, params=[]):
    req_payload = {
        "db_name": db_name,
        "sql": sql,
        "params": params
    }
    req = urllib.request.Request(
        proxy_url,
        data=json.dumps(req_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"Error {e.code}: {e.read().decode(errors='replace')}")
        raise

# Query invoice details
sql_invoice = """
WITH candidates AS (
  SELECT
    i.invoice_number,
    i.invoice_date,
    i.full_payment_date,
    COALESCE(i.total_amount, 0)::numeric AS total_amount,
    COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0)::numeric AS paid_amount,
    COALESCE(
      NULLIF(epp_items.epp_interest, 0),
      NULLIF(pay.epp_sum, 0),
      NULLIF(
        CASE
          WHEN i.effective_epp > 1.0 AND i.effective_epp < 2.0 THEN (i.total_amount * (i.effective_epp - 1.0) / i.effective_epp)::numeric
          WHEN i.effective_epp >= 2.0 AND i.effective_epp <= 100.0 THEN (i.total_amount * (i.effective_epp / 100.0) / (1.0 + i.effective_epp / 100.0))::numeric
          ELSE 0
        END,
        0
      ),
      0
    )::numeric AS epp_interest,
    COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name, '(unknown)') AS customer_name,
    COALESCE(NULLIF(TRIM(a.name), ''), '(unknown)') AS agent_name,
    a.agent_type,
    i.package_type,
    i.package_name_snapshot,
    i.description,
    COALESCE(sr_link.nem_type, sr_back.nem_type) AS seda_nem_type,
    ref.project_type AS referral_project_type,
    COALESCE(NULLIF(TRIM(c_referrer.name), ''), NULLIF(TRIM(i.referrer_name), '')) AS referral_name,
    i.bubble_id as invoice_id,
    i.paid,
    ROW_NUMBER() OVER (
      PARTITION BY i.bubble_id
      ORDER BY COALESCE(i.is_latest, FALSE) DESC,
               i.full_payment_date DESC NULLS LAST,
               i.id DESC
    ) AS rn
  FROM invoice i
  INNER JOIN agent a ON a.bubble_id = i.linked_agent
  LEFT JOIN customer c ON c.customer_id = i.linked_customer
  LEFT JOIN SEDA_registration sr_link ON sr_link.bubble_id = i.linked_seda_registration
  LEFT JOIN SEDA_registration sr_back ON i.bubble_id = ANY(sr_back.linked_invoice)
  LEFT JOIN customer c_ref ON LOWER(TRIM(c_ref.name)) = LOWER(TRIM(COALESCE(NULLIF(TRIM(i.customer_name_snapshot), ''), c.name)))
  LEFT JOIN referral ref ON (
      ref.linked_invoice = c_ref.customer_id
      OR LOWER(TRIM(ref.name)) = LOWER(TRIM(c_ref.name))
      OR (
        ref.mobile_number IS NOT NULL
        AND c_ref.phone IS NOT NULL
        AND right(regexp_replace(ref.mobile_number, '\\D', '', 'g'), 9) = right(regexp_replace(c_ref.phone, '\\D', '', 'g'), 9)
      )
      OR ref.linked_customer_profile = c_ref.customer_id
    )
    AND EXISTS (
      SELECT 1 FROM agent a_ref
      WHERE (
        CASE
          WHEN ref.linked_agent ~ '^[0-9]+$' THEN a_ref.id = CAST(ref.linked_agent AS integer)
          ELSE a_ref.bubble_id = ref.linked_agent
        END
      ) AND LOWER(TRIM(a_ref.name)) = LOWER(TRIM(a.name))
    )
  LEFT JOIN customer c_referrer ON c_referrer.customer_id = ref.linked_customer_profile
  LEFT JOIN LATERAL (
    SELECT COALESCE(
      SUM(ii_dedup.epp_interest_amount),
      0
    ) AS epp_interest
    FROM (
      SELECT 
        MAX(
          CASE
            WHEN COALESCE(ii.description, '') ILIKE '%%epp%%interest%%'
                 OR COALESCE(ii.description, '') ILIKE '%%epp interest%%'
            THEN COALESCE(ii.amount, ii.unit_price, 0)
            ELSE 0
          END
        ) AS epp_interest_amount
      FROM invoice_item ii
      WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
      GROUP BY TRIM(REGEXP_REPLACE(COALESCE(ii.description, ''), '\\s+', ' ', 'g'))
    ) ii_dedup
  ) epp_items ON TRUE
  LEFT JOIN LATERAL (
    SELECT SUM(COALESCE(p.epp_cost, 0)) AS epp_sum
    FROM payment p
    WHERE p.linked_invoice = i.bubble_id
  ) pay ON TRUE
  WHERE i.invoice_number = '1008541' OR i.invoice_number = 'INV-1008541'
)
SELECT * FROM candidates WHERE rn = 1
"""

res = query_sql(sql_invoice)
print("=== Invoice Details ===")
print(json.dumps(res, indent=2))

if res.get("rows"):
    for row in res["rows"]:
        inv_id = row["invoice_id"]
        inv_num = row["invoice_number"]
        agent_name = row["agent_name"]
        customer_name = row["customer_name"]
        total_amount = float(row["total_amount"] or 0)
        
        print(f"\n==========================================")
        print(f"Details for Invoice {inv_num} (Agent: {agent_name}, Customer: {customer_name})")
        print(f"==========================================")
        print(f"Invoice Date: {row['invoice_date']}")
        print(f"Full Payment Date: {row['full_payment_date']}")
        print(f"Total Amount: RM {total_amount:,.2f}")
        print(f"Paid: {row['paid']}")
        print(f"Agent Type: {row['agent_type']}")

        # Fetch invoice raw arrays
        raw_inv = query_sql("SELECT linked_invoice_item FROM invoice WHERE bubble_id = '{}'".format(inv_id))
        inv_item_array = raw_inv["rows"][0]["linked_invoice_item"] if raw_inv.get("rows") else []
        print(f"Invoice linked_invoice_item array: {inv_item_array}")

        # Query invoice items
        sql_items = """
            SELECT 
                ii.bubble_id,
                ii.description,
                ii.amount,
                ii.unit_price,
                ii.is_a_package,
                ii.epp,
                ii.linked_invoice,
                p.nett_price as package_nett_price
            FROM invoice_item ii
            LEFT JOIN package p ON p.bubble_id = ii.linked_package
            WHERE ii.linked_invoice = '{}' OR ii.bubble_id IN (
                SELECT unnest(linked_invoice_item) FROM invoice WHERE bubble_id = '{}'
            )
        """.format(inv_id, inv_id)
        items_res = query_sql(sql_items)
        items = items_res.get("rows") or []
        print("\nInvoice Items:")
        for it in items:
            bubble_id = it.get("bubble_id")
            desc = it.get("description")
            amt = float(it.get("amount") or 0)
            uprice = float(it.get("unit_price") or 0)
            is_pkg = it.get("is_a_package")
            linked_inv_val = it.get("linked_invoice")
            in_array = bubble_id in inv_item_array if inv_item_array else False
            pkg_nett = float(it.get("package_nett_price") or 0) if it.get("package_nett_price") else 0.0
            print(f"  - ID: {bubble_id}")
            print(f"    Desc: {desc!r}")
            print(f"    Amount: RM {amt:,.2f} | Unit Price: RM {uprice:,.2f}")
            print(f"    linked_invoice: {linked_inv_val} | in linked_invoice_item array: {in_array}")
            print(f"    Is Package: {is_pkg} | Package Nett Price: RM {pkg_nett:,.2f}")

        # Check EPP interest sum
        epp_sum = 0.0
        for it in items:
            if it.get("epp") and float(it["epp"]) > 0:
                epp_sum += float(it["epp"])
            elif it.get("description") and ("epp interest" in it["description"].lower() or "epp_interest" in it["description"].lower()):
                val = float(it.get("amount") or it.get("unit_price") or 0)
                epp_sum += val
        
        sales_price = total_amount - epp_sum
        print(f"\nEPP Interest deducted: RM {epp_sum:,.2f}")
        print(f"Sales Price: RM {sales_price:,.2f}")

        # Determine Property Classify Type
        # Let's import basic commission helper
        try:
            import importlib.machinery
            import importlib.util
            loader = importlib.machinery.SourceFileLoader(
                "full_internal_basic_commission", 
                str(REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py")
            )
            spec = importlib.util.spec_from_loader("full_internal_basic_commission", loader)
            basic_script = importlib.util.module_from_spec(spec)
            sys.modules["full_internal_basic_commission"] = basic_script
            loader.exec_module(basic_script)

            # Add package type fields to row representation for classify_property_type
            row_dict = dict(row)
            row_dict["package_name_snapshot"] = items[0].get("package_name") if items else None
            row_dict["description"] = items[0].get("description") if items else None
            
            prop_type = basic_script.classify_property_type(row_dict)
            is_sr = basic_script.is_senior(agent_name)
            
            pay_dt = row.get("full_payment_date")
            pay_month = 5
            if pay_dt:
                pay_month = basic_script._parse_invoice_date(pay_dt).month
            
            sys.path.insert(0, str(REPO_ROOT / "1. Basic Commission" / "3. Python Script"))
            from basic_commission_rates import get_basic_rate
            hierarchy = "Senior" if is_sr else "Executive"
            rate = get_basic_rate("Internal", hierarchy, pay_month)
            comm = sales_price * float(rate)
            
            print(f"Classification: {prop_type}")
            print(f"Hierarchy Tier: {hierarchy}")
            print(f"Payout Month (from payment date): {pay_month}")
            print(f"Basic Commission Rate: {rate * 100:.2f}%")
            print(f"Calculated Basic Commission: RM {comm:,.2f}")
        except Exception as ex:
            import traceback
            print(f"Classification error: {ex}")
            traceback.print_exc()

