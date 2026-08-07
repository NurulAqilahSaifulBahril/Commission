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
    print("Fetching invoices...")
    invoices = query_sql("""
        SELECT bubble_id, invoice_number, customer_name_snapshot, panel_rating, panel_qty, linked_invoice_item, description 
        FROM invoice 
        WHERE is_deleted IS NOT TRUE AND total_amount > 0;
    """)
    print(f"Fetched {len(invoices)} invoices.")
    
    print("Fetching invoice items...")
    items = query_sql("""
        SELECT bubble_id, description, is_a_package, linked_invoice 
        FROM invoice_item;
    """)
    print(f"Fetched {len(items)} invoice items.")
    
    # Index items
    items_by_invoice = {}
    item_by_id = {}
    for it in items:
        item_by_id[it['bubble_id']] = it
        inv_id = it['linked_invoice']
        if inv_id:
            items_by_invoice.setdefault(inv_id, []).append(it)
            
    mismatches = []
    for i in invoices:
        inv_id = i['bubble_id']
        inv_num = i['invoice_number']
        cust_name = i['customer_name_snapshot']
        db_rating = i['panel_rating']
        
        # Gather all item descriptions for this invoice
        inv_items = items_by_invoice.get(inv_id, [])
        # Also resolve linked items from array
        linked_ids = i['linked_invoice_item'] or []
        for lid in linked_ids:
            if lid in item_by_id and item_by_id[lid] not in inv_items:
                inv_items.append(item_by_id[lid])
                
        # Find package description
        pkg_desc = None
        for it in inv_items:
            if it['is_a_package']:
                pkg_desc = it['description']
                break
                
        # If no package item marked is_a_package, check invoice description
        if not pkg_desc:
            pkg_desc = i['description']
            
        # Get all non-package item descriptions text
        non_pkg_texts = []
        for it in inv_items:
            if not it['is_a_package'] and it['description'] != pkg_desc:
                non_pkg_texts.append(it['description'] or "")
        all_non_pkg_text = " | ".join(non_pkg_texts)
        
        pkg_rating = extract_rating(pkg_desc)
        
        # Determine actual rating
        actual_rating = db_rating
        if not actual_rating:
            actual_rating = extract_rating(all_non_pkg_text)
        if not actual_rating:
            actual_rating = pkg_rating
            
        if actual_rating and pkg_rating and actual_rating != pkg_rating:
            mismatches.append({
                "invoice_number": inv_num,
                "customer_name": cust_name,
                "db_rating": db_rating,
                "pkg_desc": pkg_desc,
                "pkg_rating": pkg_rating,
                "actual_rating": actual_rating,
                "all_items": " | ".join((it['description'] or "") for it in inv_items)
            })
            
    print(f"\nFound {len(mismatches)} mismatches:")
    for m in mismatches:
        print(f"Invoice {m['invoice_number']} ({m['customer_name']}):")
        print(f"  DB panel_rating: {m['db_rating']}")
        print(f"  Package desc:    '{m['pkg_desc']}' -> {m['pkg_rating']}W")
        print(f"  Resolved actual: {m['actual_rating']}W")
        print(f"  All items:       {m['all_items'][:120]}...")
        print("-" * 60)

if __name__ == '__main__':
    main()
