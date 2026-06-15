import sys
sys.path.append(r'C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script')
from api_client import query_sql
print(query_sql('SELECT * FROM invoice LIMIT 1'))
print(query_sql('SELECT * FROM invoice_item LIMIT 1'))
