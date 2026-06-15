import sys
import shutil

# Copy fresh
shutil.copyfile('build_finance_commission_pack.py', 'build_finance_commission_pack(outsource).py')

with open('build_finance_commission_pack(outsource).py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace Titles
content = content.replace('Commission_Pack_2026', 'Outsource_Commission_Pack_2026')
content = content.replace('Commission_Pack_{args.year}', 'Outsource_Commission_Pack_{args.year}')
content = content.replace('Commission Pack —', 'Outsource Commission Pack —')

# Replace modules
content = content.replace('nfp_commission.py', 'outsource_nfp_commission.py')
content = content.replace('_load_module("nfp_commission"', '_load_module("outsource_nfp_commission"')
content = content.replace('full_internal_EGA_ESA_Awards.py', 'outsource_EGA_ESA_Awards.py')
content = content.replace('_load_module("ega_esa"', '_load_module("outsource_ega_esa"')

# Extract parts using find
idx_fetch_basic_start = content.find('def fetch_basic(')
idx_fetch_anp_start = content.find('def fetch_anp(')
idx_fetch_nfp_start = content.find('def fetch_nfp(')

new_fetch_basic = '''def fetch_basic(year: int, h1_only: bool = False) -> tuple[list[list[str]], list[list[str]], list[list[str]], dict[str, Any], list[Any]]:
    if MOCK_MODE:
        return [], [], [], {"agents":0, "invoices":0, "total_commission":0, "filter":""}, []

    out_dir = REPO_ROOT / "1. Basic Commission" / "3. Python Script"
    out_path = out_dir / "outsource_basic_commission.py"
    basic = _load_module("outsource_basic_commission", out_path)
    
    token = os.environ.get("PG_PROXY_TOKEN", "").strip() or basic._env("PG_PROXY_TOKEN")
    if not token:
        raise RuntimeError(_token_help_message())

    proxy_url = basic._normalize_proxy_url(
        basic._env("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
    )
    db_name = basic._env("PG_PROXY_DB") or basic._env("PG_DB_NAME", "prod_main")

    payload = basic._proxy_sql(
        proxy_url=proxy_url,
        db_name=db_name,
        token=token,
        sql=basic._invoices_sql(year=year),
        params=[]
    )
    raw_rows = list(payload.get("rows") or [])
    
    from decimal import Decimal
    from collections import defaultdict
    processed_outsource_invoices = []
    processed_outsource_factory = []
    agent_own_commissions = defaultdict(Decimal)
    agent_sales = defaultdict(Decimal)
    override_commissions = defaultdict(Decimal)
    
    # Process rows similar to outsource_basic_commission
    for r in raw_rows:
        agent_name = str(r.get("agent_name") or "(unknown)").strip()
        agent_comm_field = r.get("agent_comm_field")
        
        info = basic.get_agent_hierarchy_info(agent_name)
        is_db_outsource = "outsource" in str(r.get("agent_type") or "").lower()
        if not info and not is_db_outsource:
            continue
            
        if not info:
            info = {"canonical_name": agent_name, "tier": "OSA", "osa_parent": None, "oum_parent": None}
            
        canonical_name = info["canonical_name"]
        customer_name = str(r.get("customer_name") or "(unknown)").strip()
        invoice_num = str(r.get("invoice_number") or "").strip()
        prop_type = basic.classify_property_type(r)
        
        total = basic._to_decimal(r.get("total_amount"))
        epp = basic._to_decimal(r.get("epp_interest"))
        sales_price = total - epp
        
        inv_dt = str(r.get("invoice_date") or "")[:10]
        pay_dt = str(r.get("full_payment_date") or "")[:10]
        
        if h1_only:
            m = _parse_month(pay_dt)
            if not m or m > 6:
                continue
        
        if prop_type == "Factory":
            rate = Decimal("0.02")
            sharing = basic.factory_rates.get(invoice_num, Decimal("0"))
            
            osa_sharing = sharing
            if info["tier"] in ("OSA", "OSA 1") and sharing > 0:
                osa_sharing = sharing * Decimal("0.70")
                oum_p = info.get("oum_parent")
                if oum_p:
                    override_commissions[oum_p] += sales_price * sharing * Decimal("0.20")
                override_commissions["OGM Pool"] += sales_price * sharing * Decimal("0.10")
                
            comm = sales_price * (rate + osa_sharing)
            
            processed_outsource_factory.append(
                basic.OutsourceFactoryInvoiceLine(
                    agent_name=canonical_name,
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    epp=epp,
                    sales_price=sales_price,
                    rate=rate,
                    profit_sharing=osa_sharing,
                    basic_commission=comm,
                    referral_name=None
                )
            )
        else:
            rate = basic.get_own_commission_rate(info, agent_comm_field)
            comm = sales_price * rate
            
            processed_outsource_invoices.append(
                basic.OutsourceInvoiceLine(
                    agent_name=canonical_name,
                    customer_name=customer_name,
                    invoice_number=invoice_num,
                    package=prop_type,
                    invoice_date=inv_dt,
                    full_payment_date=pay_dt,
                    total_amount=total,
                    epp=epp,
                    sales_price=sales_price,
                    rate=rate,
                    basic_commission=comm,
                    referral_name=None
                )
            )
            
        agent_sales[canonical_name] += total
        agent_own_commissions[canonical_name] += comm
        
        tier = info["tier"]
        if tier == "OSA 1":
            oum_p = info.get("oum_parent")
            if oum_p: override_commissions[oum_p] += total * Decimal("0.005")
        elif tier == "OSA":
            oum_p = info.get("oum_parent")
            internal_senior = info.get("internal_senior_parent")
            if oum_p: override_commissions[oum_p] += total * Decimal("0.005")
            if internal_senior: override_commissions[internal_senior] += total * Decimal("0.005")
            
    all_payout_agents = set(agent_own_commissions.keys()) | set(override_commissions.keys())
    t1_rows = []
    for agent in sorted(all_payout_agents):
        own = agent_own_commissions.get(agent, Decimal("0"))
        ovr = override_commissions.get(agent, Decimal("0"))
        sales = agent_sales.get(agent, Decimal("0"))
        total_payout = own + ovr
        t1_rows.append([agent, f"{sales:,.2f}", f"{own:,.2f}", f"{ovr:,.2f}", f"{total_payout:,.2f}"])
        
    t2_rows = []
    for inv in processed_outsource_invoices:
        t2_rows.append([inv.agent_name, inv.customer_name, inv.invoice_number, inv.package, inv.invoice_date, inv.full_payment_date, f"{inv.total_amount:,.2f}", f"{inv.epp:,.2f}", f"{inv.sales_price:,.2f}", f"{(inv.rate * 100):.2f}%", f"{inv.basic_commission:,.2f}"])
        
    t3_rows = []
    for inv in processed_outsource_factory:
        t3_rows.append([inv.agent_name, inv.customer_name, inv.invoice_number, inv.package, inv.invoice_date, inv.full_payment_date, f"{inv.total_amount:,.2f}", f"{inv.epp:,.2f}", f"{inv.sales_price:,.2f}", f"{(inv.rate * 100):.2f}%", f"{(getattr(inv, 'profit_sharing', 0) * 100):.2f}%", f"{inv.basic_commission:,.2f}"])
        
    total_comm = sum(Decimal(r[4].replace(',','')) for r in t1_rows if len(r)>4)
    meta = {"agents": len(t1_rows), "invoices": len(t2_rows) + len(t3_rows), "total_commission": total_comm, "filter": "outsource agents"}
    processed_lines = processed_outsource_invoices + processed_outsource_factory
    
    return t1_rows, t2_rows, t3_rows, meta, processed_lines

def fetch_anp(year: int, h1_only: bool = False) -> tuple[list[Any], list[Any], dict[str, Any]]:
    return [], [], {"agents": 0, "invoices": 0, "total_commission": Decimal("0"), "filter": "N/A for Outsource"}

'''

content = content[:idx_fetch_basic_start] + new_fetch_basic + content[idx_fetch_nfp_start:]

# Fix tuple unpacking for basic
content = content.replace('basic_t1, basic_t2, basic_t4, basic_meta, basic_lines = fetch_basic(year)', 'basic_t1, basic_t2, basic_t3, basic_meta, basic_lines = fetch_basic(year)')
content = content.replace('basic_t1, basic_t2, basic_t4, basic_meta, basic_lines = fetch_basic(year, h1_only=True)', 'basic_t1, basic_t2, basic_t3, basic_meta, basic_lines = fetch_basic(year, h1_only=True)')

# Nullify ANP logic inside build_workbook
content = content.replace('anp_summary_table = anp_mod.summary_table_rows(anp_summary)', 'anp_summary_table = []')
content = content.replace('wb.remove(wb["ANP - By Agent"])', 'if "ANP - By Agent" in wb: wb.remove(wb["ANP - By Agent"])')
# Nullify ANP logic inside build_pdf
content = content.replace('anp_summary_table = _rows_to_str(anp_mod.summary_table_rows(anp_summary))', 'anp_summary_table = []')

# Remove ANP and Referral sections from build_pdf manually line by line
lines = content.split('\\n')
new_lines = []
skip = False
for line in lines:
    if 'PdfSection(' in line and '"Referral Fee Details"' in line:
        skip = True
    if 'PdfSection(' in line and '"ANP Commission Details"' in line:
        skip = True
        
    if skip:
        if '),' in line or '])' in line:
            if '])' in line:
                new_lines.append('    ])') # Close extend
            skip = False
        continue
    
    new_lines.append(line)

# Add factory section before extend
final_lines = []
for line in new_lines:
    if 'sections.extend([' in line:
        final_lines.append('    if basic_t3:')
        final_lines.append('        sections.append(PdfSection("Outsource Factory Details", ["Agent Name", "Customer Name", "Invoice Number", "Package", "Invoice Date", "Payment Date", "Total (RM)", "EPP (RM)", "Sales (RM)", "Rate", "Profit Share", "Commission"], basic_t3, landscape=True))')
    final_lines.append(line)

with open('build_finance_commission_pack(outsource).py', 'w', encoding='utf-8') as f:
    f.write('\\n'.join(final_lines))

print("Refactor safely completed.")
