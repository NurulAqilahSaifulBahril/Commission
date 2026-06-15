import sys
sys.path.append(r'C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script')
from api_client import query_sql
rows = query_sql("SELECT invoice_number, invoice_date, full_payment_date FROM invoice WHERE invoice_number IN ('1006733', '1008117', '1006684')")
for r in rows: print(r)
