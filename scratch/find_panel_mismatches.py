import os
import sys
import re
from pathlib import Path

# Add paths to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from api_client import query_sql
from dotenv import load_dotenv

for candidate in (
    REPO_ROOT / "2. NFP Commission" / "3. Python script" / ".env",
    REPO_ROOT / ".env",
):
    if candidate.is_file():
        load_dotenv(candidate, override=True)

def extract_rating(text):
    if not text:
        return None
    text_lower = text.lower()
    # Check 590, 620, 650
    if "590" in text_lower:
        return 590
    if "620" in text_lower or "66hl4" in text_lower:
        return 620
    if "650" in text_lower:
        return 650
    return None

def main():
    print("Finding all active invoices with panel ratings and package descriptions...")
    sql = """
    SELECT 
        i.invoice_number,
        i.customer_name_snapshot,
        i.panel_rating,
        i.panel_qty,
        pkg.package_description,
        items.all_item_text
    FROM invoice i
    LEFT JOIN LATERAL (
        SELECT ii.description AS package_description
        FROM invoice_item ii
        LEFT JOIN package p ON p.bubble_id = ii.linked_package
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
          AND ii.is_a_package IS TRUE
        ORDER BY ii.id
        LIMIT 1
    ) pkg ON TRUE
    LEFT JOIN LATERAL (
        SELECT string_agg(COALESCE(ii.description, ''), ' | ') AS all_item_text
        FROM invoice_item ii
        WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
    ) items ON TRUE
    WHERE i.is_deleted IS NOT TRUE
      AND i.total_amount > 0
    ORDER BY i.invoice_number;
    """
    rows = query_sql(sql)
    print(f"Total invoices: {len(rows)}")
    
    mismatches = 0
    for r in rows:
        inv_num = r['invoice_number']
        cust_name = r['customer_name_snapshot']
        db_rating = r['panel_rating']
        pkg_desc = r['package_description'] or ""
        all_text = r['all_item_text'] or ""
        
        # Determine package rating
        pkg_rating = extract_rating(pkg_desc)
        
        # Determine actual rating
        # 1. from database panel_rating column
        actual_rating = db_rating
        # 2. if empty, from non-package items in all_item_text
        if not actual_rating:
            # remove package description from all_text to avoid matching package text
            non_pkg_text = all_text.replace(pkg_desc, "")
            actual_rating = extract_rating(non_pkg_text)
            
        # 3. fallback to package rating if still None
        if not actual_rating:
            actual_rating = pkg_rating
            
        if actual_rating and pkg_rating and actual_rating != pkg_rating:
            mismatches += 1
            print(f"Mismatch found on Invoice {inv_num} ({cust_name}):")
            print(f"  DB panel_rating column: {db_rating}")
            print(f"  Package description:   '{pkg_desc}' -> {pkg_rating}W")
            print(f"  All items text:        '{all_text[:100]}...'")
            print(f"  Resolved actual rating: {actual_rating}W")
            print("-" * 50)
            
    print(f"Total mismatches found: {mismatches}")

if __name__ == '__main__':
    main()
