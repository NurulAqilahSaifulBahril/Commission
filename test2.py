import sys
sys.path.append(r'C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script')
from api_client import query_sql
rows = query_sql("SELECT i.invoice_number, i.linked_package, ii.linked_package as ii_pkg, p.nett_price FROM invoice i LEFT JOIN invoice_item ii ON ii.linked_invoice = i.bubble_id AND ii.is_a_package = true LEFT JOIN package p ON p.bubble_id = COALESCE(i.linked_package, ii.linked_package) WHERE i.invoice_number IN ('1006733', '1008117', '1007637')")
for r in rows: print(r)
