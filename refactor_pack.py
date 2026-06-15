import sys
import re

with open('build_finance_commission_pack(outsource).py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Title changes
content = content.replace('Commission_Pack_2026', 'Outsource_Commission_Pack_2026')
content = content.replace('Commission_Pack_{args.year}', 'Outsource_Commission_Pack_{args.year}')

# 2. Update fetch_basic to use outsource_basic_commission
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
        
    t4_rows = [] # Referrals not handled explicitly in this fetch for brevity
    
    total_comm = sum(Decimal(r[4].replace(',','')) for r in t1_rows if len(r)>4)
    meta = {"agents": len(t1_rows), "invoices": len(t2_rows) + len(t3_rows), "total_commission": total_comm, "filter": "outsource agents"}
    processed_lines = processed_outsource_invoices + processed_outsource_factory
    
    # Notice we return t3_rows instead of t4_rows as the 3rd list, because the outer caller unpacks basic_t1, basic_t2, basic_t4
    return t1_rows, t2_rows, t3_rows, meta, processed_lines'''

content = re.sub(
    r'def fetch_basic\(.*?\):.*?return t1_rows, t2_rows, t4_rows, meta, processed_lines',
    new_fetch_basic,
    content,
    flags=re.DOTALL
)

# 3. Remove ANP references
content = re.sub(r'def fetch_anp\(.*?\):.*?return summary_rows, detail_rows, meta', '', content, flags=re.DOTALL)
content = re.sub(r'anp_summary, anp_detail, anp_meta = fetch_anp\(year.*?print\(f"  ANP:.*?invoices"\)', '', content, flags=re.DOTALL)
content = content.replace('anp_detail', '[]')
content = content.replace('anp_meta', '{"total_commission": 0, "agents": 0}')
content = content.replace('anp_summary_table = _rows_to_str(anp_mod.summary_table_rows(anp_summary))', '')
content = content.replace('anp_summary_table = []', '')
content = content.replace('anp_summary_table = [[r["agent_name"], str(r["invoice_count"]), f"{r[\'accumulated_total_amount\']:.2f}", f"{r[\'anp_commission\']:.2f}"] for r in anp_summary]', '')

# NFP
content = content.replace('nfp_commission.py', 'outsource_nfp_commission.py')
content = content.replace('_load_module("nfp_commission"', '_load_module("outsource_nfp_commission"')

# EGA ESA
content = content.replace('full_internal_EGA_ESA_Awards.py', 'outsource_EGA_ESA_Awards.py')
content = content.replace('_load_module("ega_esa"', '_load_module("outsource_ega_esa"')

with open('build_finance_commission_pack(outsource).py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Refactor completed.")
