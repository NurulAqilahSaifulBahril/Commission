import sys
sys.path.append(r'C:\Users\User\OneDrive\Documents\Commission\2. NFP Commission\3. Python script')
from api_client import query_sql
rows = query_sql('SELECT package_name, price, nett_price FROM package WHERE nett_price IS NOT NULL ORDER BY package_name')
output_path = r'C:\Users\User\.gemini\antigravity-ide\brain\12f970bc-bf81-44ac-9a24-8aea432b3366\package_nett_prices.md'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write('# Package Nett Prices\n\n')
    f.write('Here is the full list of the packages from the database that have a nett_price populated.\n\n')
    f.write('| Package Name | Price | Nett Price |\n')
    f.write('|--------------|-------|------------|\n')
    for r in rows:
        pkg_name = str(r.get('package_name', '')).replace('|', '-')
        price = r.get('price') or ''
        nett = r.get('nett_price') or ''
        f.write(f'| {pkg_name} | {price} | {nett} |\n')
