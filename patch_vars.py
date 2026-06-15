with open('build_finance_commission_pack(outsource).py', 'r', encoding='utf-8') as f:
    content = f.read()
    
# get_basic_monthly_tables
content = content.replace('ln.net_base', 'ln.sales_price')
content = content.replace('ln.commission_rate', 'ln.rate')
content = content.replace('f"{ln.override_commission_rate * 100:g}%" if ln.override_commission_rate else "-"', '"-"')

with open('build_finance_commission_pack(outsource).py', 'w', encoding='utf-8') as f:
    f.write(content)
