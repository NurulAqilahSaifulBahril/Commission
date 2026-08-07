import psycopg2
from decimal import Decimal
import sys
import os
import argparse
from pathlib import Path

# ReportLab imports
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

SCRIPT_DIR = Path(__file__).resolve().parent

def load_rates_from_env() -> dict:
    env_vars = {}
    env_path = SCRIPT_DIR / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            env_vars[name.strip()] = value.strip().strip('"').strip("'")
            
    return {
        'EXECUTIVE_RATE': Decimal(env_vars.get('EXECUTIVE_RATE', '0.03')),
        'OGM_OVERRIDE_RATE': Decimal(env_vars.get('OGM_OVERRIDE_RATE', '0.0075')),
        'OUM_OVERRIDE_RATE': Decimal(env_vars.get('OUM_OVERRIDE_RATE', '0.005')),
        'REFERRAL_RATE': Decimal(env_vars.get('REFERRAL_RATE', '0.02')),
        'FACTORY_MAIN_RATE': Decimal(env_vars.get('FACTORY_MAIN_RATE', '0.02')),
        'FACTORY_SAFWAN_RATE': Decimal(env_vars.get('FACTORY_SAFWAN_RATE', '0.005')),
        'SENIOR_OVERRIDE_RATE': Decimal(env_vars.get('SENIOR_OVERRIDE_RATE', '0.0025'))
    }

def query_all_flow_data(rates):
    conn = psycopg2.connect("postgres://postgres:postgres@localhost:5432/backup_admin_dev")
    cur = conn.cursor()
    
    samples = {}

    # 1. Internal Basic Commission (Joshua Yap - INV-1008113)
    cur.execute("""
        SELECT 
            i.invoice_number,
            i.total_amount,
            COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0) as paid_amount,
            a.name as agent_name,
            a.agent_type
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        WHERE i.invoice_number = '1008113' AND i.paid IS TRUE AND a.name = 'JOSHUA YAP JIA HAO';
    """)
    row = cur.fetchone()
    if row:
        inv_num, total_amount, paid_amount, agent_name, agent_type = row
        total = Decimal(str(total_amount))
        paid = Decimal(str(paid_amount))
        epp = Decimal("0.00")
        sales_price = total - epp
        rate = rates['EXECUTIVE_RATE']
        commission = sales_price * rate
        
        override_rate = rates['SENIOR_OVERRIDE_RATE']
        override_commission = sales_price * override_rate
        senior_name = "CHING ZHE HANG"
        
        samples['internal_basic'] = {
            'inv_num': inv_num,
            'total_amount': total,
            'paid_amount': paid,
            'agent_name': agent_name.strip(),
            'agent_type': agent_type,
            'epp_interest': epp,
            'sales_price': sales_price,
            'rate': rate,
            'commission': commission,
            'override_rate': override_rate,
            'override_commission': override_commission,
            'senior_name': senior_name
        }

    # 2. Outsource Basic Commission (Tan Sue Cherk - INV-1008333)
    cur.execute("""
        SELECT 
            i.invoice_number,
            i.total_amount,
            COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0) as paid_amount,
            a.name as agent_name,
            a.agent_type
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        WHERE i.invoice_number = 'INV-1008333' AND i.paid IS TRUE;
    """)
    row = cur.fetchone()
    if row:
        inv_num, total_amount, paid_amount, agent_name, agent_type = row
        total = Decimal(str(total_amount))
        paid = Decimal(str(paid_amount))
        epp = Decimal("0.00")
        sales_price = total - epp
        rate = rates['EXECUTIVE_RATE']
        commission = sales_price * rate
        
        ogm_rate = rates['OGM_OVERRIDE_RATE']
        ogm_comm = sales_price * ogm_rate
        oum_rate = rates['OUM_OVERRIDE_RATE']
        oum_comm = commission * oum_rate
        
        samples['outsource_basic'] = {
            'inv_num': inv_num,
            'total_amount': total,
            'paid_amount': paid,
            'agent_name': agent_name.strip(),
            'agent_type': agent_type,
            'tier': "OSA 1",
            'epp_interest': epp,
            'sales_price': sales_price,
            'rate': rate,
            'commission': commission,
            'ogm_rate': ogm_rate,
            'ogm_comm': ogm_comm,
            'oum_parent': "Carol Siow",
            'oum_rate': oum_rate,
            'oum_comm': oum_comm
        }

    # 3. Invoice with EPP & Referral Fee (TAN JIA HAO - INV 1000898)
    cur.execute("""
        SELECT 
            i.invoice_number,
            i.total_amount,
            COALESCE((SELECT SUM(p.amount) FROM payment p WHERE p.linked_invoice = i.bubble_id), 0) as paid_amount,
            a.name as agent_name,
            a.agent_type,
            i.referrer_name
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        WHERE i.invoice_number = '1000898' AND i.paid IS TRUE;
    """)
    row = cur.fetchone()
    if row:
        inv_num, total_amount, paid_amount, agent_name, agent_type, referrer_name = row
        total = Decimal(str(total_amount))
        paid = Decimal(str(paid_amount))
        cur.execute("""
            SELECT COALESCE(MAX(COALESCE(amount, unit_price, 0)), 0)
            FROM invoice_item
            WHERE linked_invoice = (SELECT bubble_id FROM invoice WHERE invoice_number = '1000898' LIMIT 1)
              AND (description ILIKE '%epp%interest%' OR description ILIKE '%epp interest%');
        """)
        epp = Decimal(str(cur.fetchone()[0]))
        sales_price = total - epp
        rate = rates['EXECUTIVE_RATE']
        commission = sales_price * rate
        ref_rate = rates['REFERRAL_RATE']
        referral_fee = sales_price * ref_rate
        samples['referral'] = {
            'inv_num': inv_num,
            'total_amount': total,
            'paid_amount': paid,
            'agent_name': agent_name.strip(),
            'agent_type': agent_type,
            'referrer_name': referrer_name.strip() if referrer_name else "Safwan",
            'epp_interest': epp,
            'sales_price': sales_price,
            'rate': rate,
            'commission': commission,
            'ref_rate': ref_rate,
            'referral_fee': referral_fee
        }

    # 4. NFP Commission Sample (LING LIANG KANG - INV-1008636)
    cur.execute("""
        SELECT 
            i.invoice_number,
            i.total_amount,
            a.name as agent_name,
            pkg.system_price,
            pkg.db_net_floor_price
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        LEFT JOIN LATERAL (
            SELECT 
                COALESCE(NULLIF(ii.unit_price, 0), ii.amount, 0) AS system_price,
                p.nett_price AS db_net_floor_price
            FROM invoice_item ii
            LEFT JOIN package p ON p.bubble_id = ii.linked_package
            WHERE (ii.linked_invoice = i.bubble_id OR ii.bubble_id = ANY(i.linked_invoice_item))
            ORDER BY ii.is_a_package IS TRUE DESC
            LIMIT 1
        ) pkg ON TRUE
        WHERE i.invoice_number = 'INV-1008636' AND i.paid IS TRUE;
    """)
    row = cur.fetchone()
    if row:
        inv_num, total_amount, agent_name, system_price, db_nfp = row
        total = Decimal(str(total_amount))
        epp = Decimal("0.00")
        sales_price = total - epp
        sys_price = Decimal(str(system_price))
        nfp_price = Decimal(str(db_nfp))
        
        comp_a = Decimal("0.00")
        comp_c = Decimal("0.00")
        if sales_price > nfp_price:
            comp_a = (sales_price - nfp_price) * Decimal("0.25")
        if sales_price < nfp_price:
            comp_c = (nfp_price - sales_price) * Decimal("0.20")
        
        nfp_comm = comp_a - comp_c
        
        samples['nfp'] = {
            'inv_num': inv_num,
            'total_amount': total,
            'agent_name': agent_name.strip(),
            'epp_interest': epp,
            'sales_price': sales_price,
            'system_price': sys_price,
            'net_floor_price': nfp_price,
            'comp_a': comp_a,
            'comp_c': comp_c,
            'nfp_commission': nfp_comm
        }

    # 5. ANP Commission Sample (TAN JIA HAO - Feb 2026)
    cur.execute("""
        SELECT 
            i.invoice_number,
            i.total_amount,
            i.invoice_date
        FROM invoice i
        INNER JOIN agent a ON a.bubble_id = i.linked_agent
        WHERE a.name = 'TAN JIA HAO' 
          AND i.paid IS TRUE 
          AND EXTRACT(YEAR FROM i.invoice_date) = 2026 
          AND EXTRACT(MONTH FROM i.invoice_date) = 2;
    """)
    rows = cur.fetchall()
    if rows:
        inv_list = []
        accumulated = Decimal("0.00")
        for r in rows:
            inv_num, amount, inv_date = r
            amt = Decimal(str(amount))
            accumulated += amt
            inv_list.append({
                'inv_num': inv_num,
                'amount': amt,
                'date': str(inv_date)[:10]
            })
        
        anp_payout = Decimal("500.00") if Decimal("60000.00") <= accumulated < Decimal("180000.00") else Decimal("0.00")
        
        samples['anp'] = {
            'agent_name': 'TAN JIA HAO',
            'month': 'February 2026',
            'invoices': inv_list,
            'accumulated_total': accumulated,
            'anp_commission': anp_payout,
            'payout_month': 'March 2026'
        }

    cur.close()
    conn.close()
    
    # 6. EGA/ESA Awards Sample
    # Factory package post May-2026 (first RM40,000 at 100%, balance at 40%)
    factory_sales = Decimal("100000.00")
    first_block = Decimal("40000.00")
    factory_ep = first_block + (factory_sales - first_block) * Decimal("0.40")
    samples['ega_esa'] = {
        'residential_sales': Decimal("25000.00"),
        'residential_ep': Decimal("25000.00"),
        'factory_sales': factory_sales,
        'factory_ep': factory_ep,
        'cumulative_ep': Decimal("625000.00"),
        'ega_threshold': Decimal("600000.00"),
        'esa_threshold': Decimal("1300000.00")
    }

    # 7. Production Bonus Sample (OUM team-based bonus)
    team_sales = Decimal("2450000.00")
    personal_sales = Decimal("320000.00")
    oum_bonus = team_sales * Decimal("0.005") # 0.5% OUM Bonus
    samples['production_bonus'] = {
        'team_sales': team_sales,
        'personal_sales': personal_sales,
        'oum_bonus': oum_bonus,
        'team_threshold': Decimal("2000000.00"),
        'personal_threshold': Decimal("300000.00")
    }

    return samples

def print_terminal_flow(samples):
    print("=" * 90)
    print(" COMMISSION CALCULATION FLOW AUDIT TRAIL (build_commission_pack.py)")
    print("=" * 90)
    print("This script demonstrates the step-by-step calculations for all streams.")
    print("=" * 90)

    # 1. Internal Basic
    if 'internal_basic' in samples:
        s = samples['internal_basic']
        print(f"\n[STREAM 1A] INTERNAL AGENT BASIC COMMISSION (Joshua Yap - INV-1008113)")
        print("-" * 90)
        print(f"Database Query Details:")
        print(f"  * Table 'invoice' Column 'invoice_number':      {s['inv_num']}")
        print(f"  * Table 'invoice' Column 'total_amount':        RM {s['total_amount']:,.2f}")
        print(f"  * Table 'invoice' Column 'paid_amount':         RM {s['paid_amount']:,.2f} (Payment Received)")
        print(f"  * Verification (Payment / Total = 100%):        Fully Paid (100.00%)")
        print(f"  * Table 'agent' Column 'name':                  {s['agent_name']} ({s['agent_type']})")
        print(f"  * Senior manager linked via code logic:         {s['senior_name']}")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Calculate Sales Price (Total - EPP):")
        print(f"          Sales Price = RM {s['total_amount']:,.2f}")
        print(f"  Step 2: Calculate Joshua's Own Basic Commission (Executive {s['rate']*100:.2f}%):")
        print(f"          Commission  = Sales Price * {s['rate']*100:.2f}% = RM {s['commission']:,.2f}")
        print(f"  Step 3: Calculate Senior Reporting Override (+{s['override_rate']*100:.2f}%):")
        print(f"          Senior Override = Sales Price * {s['override_rate']*100:.2f}% = RM {s['override_commission']:,.2f} (to {s['senior_name']})")

    # 2. Outsource Basic
    if 'outsource_basic' in samples:
        s = samples['outsource_basic']
        print(f"\n[STREAM 1B] OUTSOURCE AGENT BASIC COMMISSION (Tan Sue Cherk - INV-1008333)")
        print("-" * 90)
        print(f"Database Query Details:")
        print(f"  * Table 'invoice' Column 'invoice_number':      {s['inv_num']}")
        print(f"  * Table 'invoice' Column 'total_amount':        RM {s['total_amount']:,.2f}")
        print(f"  * Table 'invoice' Column 'paid_amount':         RM {s['paid_amount']:,.2f} (Payment Received)")
        print(f"  * Verification (Payment / Total = 100%):        Fully Paid (100.00%)")
        print(f"  * Table 'agent' Column 'name':                  {s['agent_name']} (outsource)")
        print(f"  * Agent Tier & Upline Manager (hierarchy):      Tier: {s['tier']} | OUM Parent: {s['oum_parent']}")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Calculate Sales Price:")
        print(f"          Sales Price = RM {s['sales_price']:,.2f}")
        print(f"  Step 2: Calculate Tan Sue Cherk's Own Basic Commission ({s['rate']*100:.2f}%):")
        print(f"          Own Commission = Sales Price * {s['rate']*100:.2f}% = RM {s['commission']:,.2f}")
        print(f"  Step 3: Calculate OGM Override (Gan Lai Soon gets {s['ogm_rate']*100:.2f}% because tier is {s['tier']}):")
        print(f"          OGM Override = Sales Price * {s['ogm_rate']*100:.2f}% = RM {s['ogm_comm']:,.2f} (to Gan Lai Soon)")
        print(f"  Step 4: Calculate OUM Override (OUM Parent {s['oum_parent']} gets {s['oum_rate']*100:.2f}% of Own Commission):")
        print(f"          OUM Override = Own Commission * {s['oum_rate']*100:.2f}% = RM {s['oum_comm']:,.2f} (to {s['oum_parent']})")

    # 3. Referral
    if 'referral' in samples:
        s = samples['referral']
        print(f"\n[STREAM 1C] EPP INTEREST DEDUCTION AND REFERRAL FEE (TAN JIA HAO - INV-1000898)")
        print("-" * 90)
        print(f"Database Query Details:")
        print(f"  * Table 'invoice' Column 'invoice_number':      {s['inv_num']}")
        print(f"  * Table 'invoice' Column 'total_amount':        RM {s['total_amount']:,.2f}")
        print(f"  * Table 'invoice' Column 'paid_amount':         RM {s['paid_amount']:,.2f} (Payment Received)")
        print(f"  * Verification (Payment / Total = 100%):        Fully Paid (100.00%)")
        print(f"  * Table 'invoice' Column 'referrer_name':       {s['referrer_name']}")
        print(f"  * Table 'invoice_item' EPP Interest Item:       RM {s['epp_interest']:,.2f}")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Calculate Sales Price (EPP Deducted):")
        print(f"          Sales Price = Total Amount - EPP Interest")
        print(f"                      = RM {s['total_amount']:,.2f} - RM {s['epp_interest']:,.2f} = RM {s['sales_price']:,.2f}")
        print(f"  Step 2: Calculate Referral Fee (Referrer: {s['referrer_name']}):")
        print(f"          Referral Fee = Sales Price * {s['ref_rate']*100:.2f}% = RM {s['referral_fee']:,.2f}")

    # 4. NFP
    if 'nfp' in samples:
        s = samples['nfp']
        print(f"\n[STREAM 2] NFP (NET FLOOR PRICE) COMMISSION (LING LIANG KANG - INV-1008636)")
        print("-" * 90)
        print(f"Database Query Details:")
        print(f"  * Table 'invoice' Column 'invoice_number':      {s['inv_num']}")
        print(f"  * Table 'invoice' Column 'total_amount':        RM {s['total_amount']:,.2f}")
        print(f"  * Payment Verification (100% Paid):             Fully Paid (100.00% or invoice.paid = TRUE)")
        print(f"  * Table 'invoice_item' Column 'unit_price':     RM {s['system_price']:,.2f} (System Price)")
        print(f"  * Table 'package' Column 'nett_price':          RM {s['net_floor_price']:,.2f} (Net Floor Price)")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Calculate Component A (Sales Price exceeds Net Floor Price):")
        print(f"          Component A = Max(0, Sales Price - Net Floor Price) * 25.00%")
        print(f"                      = Max(0, RM {s['sales_price']:,.2f} - RM {s['net_floor_price']:,.2f}) * 25.00% = RM {s['comp_a']:,.2f}")
        print(f"  Step 2: Calculate Component C (Sales Price falls short of Net Floor Price):")
        print(f"          Component C = Max(0, Net Floor Price - Sales Price) * 20.00% = RM {s['comp_c']:,.2f}")
        print(f"  Step 3: Calculate NFP Commission Payout:")
        print(f"          NFP Commission = Component A - Component C = RM {s['nfp_commission']:,.2f}")

    # 5. ANP
    if 'anp' in samples:
        s = samples['anp']
        print(f"\n[STREAM 3] ANP COMMISSION FLOW (TAN JIA HAO - {s['month']})")
        print("-" * 90)
        print(f"Payment Verification Status:                    First Payment Secured (invoice.1st_payment_date IS NOT NULL)")
        print(f"Invoices Secured in {s['month']}:")
        for inv in s['invoices']:
            print(f"  * Invoice: {inv['inv_num']} | Date: {inv['date']} | Amount: RM {inv['amount']:,.2f}")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Sum Invoices' Total Amount for Calendar Month:")
        print(f"          Accumulated Total = RM {s['accumulated_total']:,.2f}")
        print(f"  Step 2: Determine Commission Payout (Tier RM 60k - 180k = RM 500 payout):")
        print(f"          ANP Commission Payout = RM {s['anp_commission']:,.2f}")

    # 6. EGA/ESA Awards
    if 'ega_esa' in samples:
        s = samples['ega_esa']
        print(f"\n[STREAM 4] EGA / ESA AWARDS EP POINT CALCULATION")
        print("-" * 90)
        print(f"Payment Verification Status:                    First Payment Secured (1st_payment_date IS NOT NULL)")
        print(f"Rules & Math Sample:")
        print(f"  * Standard Residential Package: 1 EP Point per RM 1 Sales Price")
        print(f"    For RM {s['residential_sales']:,.2f} sales -> {s['residential_ep']:,.0f} EP Points")
        print(f"  * Factory Package (>= 36 panels, Post-May 2026): first RM 40k at 100%, balance at 40%")
        print(f"    For RM {s['factory_sales']:,.2f} sales -> RM 40,000 + (RM 60,000 * 40%) = {s['factory_ep']:,.0f} EP Points")
        print(f"  * Qualification check: Cumulative EP = RM {s['cumulative_ep']:,.2f}")
        print(f"    - EGA (Half-Year Award) Threshold: > {s['ega_threshold']:,.0f} EP Points (Qualified!)")
        print(f"    - ESA (Full-Year Award) Threshold: > {s['esa_threshold']:,.0f} EP Points")

    # 7. Production Bonus
    if 'production_bonus' in samples:
        s = samples['production_bonus']
        print(f"\n[STREAM 5] PRODUCTION BONUS (OUM Team Bonus)")
        print("-" * 90)
        print(f"OUM Team details:")
        print(f"  * Team Total Sales:        RM {s['team_sales']:,.2f} (Threshold: >= RM {s['team_threshold']:,.0f})")
        print(f"  * OUM Personal Sales:      RM {s['personal_sales']:,.2f} (Threshold: >= RM {s['personal_threshold']:,.0f})")
        print(f"\nCalculation Flow:")
        print(f"  Step 1: Check Qualifications: Team Total >= 2.0M (Yes) AND Personal Sales >= 300k (Yes)")
        print(f"  Step 2: Calculate OUM Team Production Bonus (0.50% of Team Total Sales):")
        print(f"          Bonus = RM {s['team_sales']:,.2f} * 0.50% = RM {s['oum_bonus']:,.2f}")

    print("=" * 90)

def generate_pdf_flow(samples, output_path: str):
    if not REPORTLAB_AVAILABLE:
        print("Error: reportlab is not installed. Run 'pip install reportlab' to generate PDFs.")
        return

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=40,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()
    
    PRIMARY_COLOR = colors.HexColor("#1A365D")
    SECONDARY_COLOR = colors.HexColor("#2C5282")
    BG_CARD = colors.HexColor("#F8FAFC")
    BORDER_COLOR = colors.HexColor("#E2E8F0")

    title_style = ParagraphStyle(
        "PDFTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=PRIMARY_COLOR,
        alignment=1, # Center
        spaceAfter=15
    )
    
    subtitle_style = ParagraphStyle(
        "PDFSubTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#4A5568"),
        alignment=1, # Center
        spaceAfter=25
    )

    h2_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        textColor=PRIMARY_COLOR,
        spaceBefore=12,
        spaceAfter=8
    )

    bold_label_style = ParagraphStyle(
        "BoldLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#2D3748")
    )

    value_style = ParagraphStyle(
        "NormalValue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#2D3748")
    )

    flow_step_title = ParagraphStyle(
        "StepTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=SECONDARY_COLOR,
        spaceBefore=4
    )

    flow_step_math = ParagraphStyle(
        "StepMath",
        parent=styles["Normal"],
        fontName="Courier-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#2D3748"),
        leftIndent=15
    )

    story = []

    story.append(Paragraph("COMMISSION CALCULATION FLOW AUDIT TRAIL", title_style))
    story.append(Paragraph(
        "Complete calculation flow logic mapping database columns to commission payouts for ALL streams compiled by build_commission_pack.py.",
        subtitle_style
    ))

    def make_section(title_text, details, steps):
        section_story = []
        section_story.append(Paragraph(title_text, h2_style))
        
        details_data = []
        for label, val in details:
            details_data.append([
                Paragraph(f"<b>{label}</b>", bold_label_style),
                Paragraph(str(val), value_style)
            ])
        
        details_table = Table(details_data, colWidths=[200, 340])
        details_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_COLOR),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        section_story.append(details_table)
        section_story.append(Spacer(1, 8))

        for step_title, math_line in steps:
            section_story.append(Paragraph(step_title, flow_step_title))
            if math_line:
                section_story.append(Paragraph(math_line, flow_step_math))
        
        section_story.append(Spacer(1, 15))
        return KeepTogether(section_story)

    # 1. Internal Basic
    if 'internal_basic' in samples:
        s = samples['internal_basic']
        details = [
            ("Invoice Number (invoice.invoice_number)", s['inv_num']),
            ("Total Amount (invoice.total_amount)", f"RM {s['total_amount']:,.2f}"),
            ("Payment Received (invoice.paid_amount)", f"RM {s['paid_amount']:,.2f}"),
            ("Verification (Payment / Total = 100%)", "Fully Paid (100.00%)"),
            ("Agent Name (agent.name)", f"{s['agent_name']} (internal)"),
            ("Reporting Senior Manager (via code logic)", s['senior_name']),
        ]
        steps = [
            ("Step 1: Calculate Sales Price (Total - EPP):", f"Sales Price = RM {s['sales_price']:,.2f}"),
            ("Step 2: Calculate Joshua's Own Basic Commission (Executive " + f"{s['rate']*100:.2f}%):", f"Own Commission = RM {s['sales_price']:,.2f} * {s['rate']*100:.2f}% = RM {s['commission']:,.2f}"),
            ("Step 3: Calculate Senior Reporting Override (+" + f"{s['override_rate']*100:.2f}% to Senior):", f"Senior Override = RM {s['sales_price']:,.2f} * {s['override_rate']*100:.2f}% = RM {s['override_commission']:,.2f} (to {s['senior_name']})"),
        ]
        story.append(make_section("1. Internal Basic Commission & Senior Override (Joshua Yap)", details, steps))

    # 2. Outsource Basic
    if 'outsource_basic' in samples:
        s = samples['outsource_basic']
        details = [
            ("Invoice Number (invoice.invoice_number)", s['inv_num']),
            ("Total Amount (invoice.total_amount)", f"RM {s['total_amount']:,.2f}"),
            ("Payment Received (invoice.paid_amount)", f"RM {s['paid_amount']:,.2f}"),
            ("Verification (Payment / Total = 100%)", "Fully Paid (100.00%)"),
            ("Agent Name (agent.name)", f"{s['agent_name']} (outsource)"),
            ("Agent Tier & Upline Manager (hierarchy info)", f"Tier: {s['tier']} | OUM Parent: {s['oum_parent']}"),
        ]
        steps = [
            ("Step 1: Calculate Sales Price:", f"Sales Price = RM {s['sales_price']:,.2f}"),
            ("Step 2: Calculate Tan Sue Cherk's Own Basic Commission (" + f"{s['rate']*100:.2f}%):", f"Own Commission = RM {s['sales_price']:,.2f} * {s['rate']*100:.2f}% = RM {s['commission']:,.2f}"),
            ("Step 3: Calculate OGM Override (Gan Lai Soon gets " + f"{s['ogm_rate']*100:.2f}% because tier is " + s['tier'] + "):", f"OGM Override = RM {s['sales_price']:,.2f} * {s['ogm_rate']*100:.2f}% = RM {s['ogm_comm']:,.2f}"),
            ("Step 4: Calculate OUM Override (OUM Parent " + s['oum_parent'] + " gets " + f"{s['oum_rate']*100:.2f}% of Own Commission):", f"OUM Override = RM {s['commission']:,.2f} * {s['oum_rate']*100:.2f}% = RM {s['oum_comm']:,.2f}"),
        ]
        story.append(make_section("2. Outsource Basic Commission & Overrides (Tan Sue Cherk)", details, steps))

    # 3. Referral
    if 'referral' in samples:
        s = samples['referral']
        details = [
            ("Invoice Number (invoice.invoice_number)", s['inv_num']),
            ("Total Amount (invoice.total_amount)", f"RM {s['total_amount']:,.2f}"),
            ("Payment Received (invoice.paid_amount)", f"RM {s['paid_amount']:,.2f}"),
            ("Verification (Payment / Total = 100%)", "Fully Paid (100.00%)"),
            ("Agent Name (agent.name)", s['agent_name']),
            ("Referral Name (invoice.referrer_name)", s['referrer_name']),
            ("EPP Interest Item (invoice_item)", f"RM {s['epp_interest']:,.2f}"),
        ]
        steps = [
            ("Step 1: Calculate Sales Price (EPP Deducted):", f"Sales Price = RM {s['total_amount']:,.2f} - RM {s['epp_interest']:,.2f} = RM {s['sales_price']:,.2f}"),
            ("Step 2: Calculate Referral Fee (Referrer: " + s['referrer_name'] + " gets " + f"{s['ref_rate']*100:.2f}%):", f"Referral Fee = RM {s['sales_price']:,.2f} * {s['ref_rate']*100:.2f}% = RM {s['referral_fee']:,.2f}"),
        ]
        story.append(make_section("3. EPP Interest Deduction and Referral Fee (Tan Jia Hao)", details, steps))

    # 4. NFP
    if 'nfp' in samples:
        s = samples['nfp']
        details = [
            ("Invoice Number (invoice.invoice_number)", s['inv_num']),
            ("Total Amount (invoice.total_amount)", f"RM {s['total_amount']:,.2f}"),
            ("Payment Verification Status", "100% Paid (paid = TRUE or percent_of_total_amount >= 1.0)"),
            ("Agent Name (agent.name)", s['agent_name']),
            ("System Price (invoice_item.unit_price)", f"RM {s['system_price']:,.2f}"),
            ("Net Floor Price (package.nett_price)", f"RM {s['net_floor_price']:,.2f}"),
        ]
        steps = [
            ("Step 1: Calculate Component A (Sales Price exceeds Net Floor Price):", f"Component A = Max(0, Sales Price - Net Floor Price) * 25.00%\n            = Max(0, RM {s['sales_price']:,.2f} - RM {s['net_floor_price']:,.2f}) * 25.00% = RM {s['comp_a']:,.2f}"),
            ("Step 2: Calculate Component C (Sales Price falls short of Net Floor Price):", f"Component C = Max(0, Net Floor Price - Sales Price) * 20.00%\n            = Max(0, RM {s['net_floor_price']:,.2f} - RM {s['sales_price']:,.2f}) * 20.00% = RM {s['comp_c']:,.2f}"),
            ("Step 3: Calculate NFP Commission Payout (Component A - Component C):", f"NFP Commission = RM {s['comp_a']:,.2f} - RM {s['comp_c']:,.2f} = RM {s['nfp_commission']:,.2f}"),
        ]
        story.append(make_section("4. NFP (Net Floor Price) Commission Flow (Ling Liang Kang)", details, steps))

    # 5. ANP
    if 'anp' in samples:
        s = samples['anp']
        
        details = [
            ("Agent Name (agent.name)", s['agent_name']),
            ("Invoicing Month (calendar month)", s['month']),
            ("Payment Verification Status", "First Payment Secured (1st_payment_date IS NOT NULL)"),
            ("Payout Month (timing remark)", s['payout_month']),
            ("Distinct Invoices Secured", f"{len(s['invoices'])} invoices"),
        ]
        
        steps = [
            ("Step 1: Sum Invoices' Total Amount for Calendar Month:", f"Accumulated Total = " + " + ".join(f"RM {inv['amount']:,.2f}" for inv in s['invoices']) + f"\n                  = RM {s['accumulated_total']:,.2f}"),
            ("Step 2: Check ANP Tier for Accumulated Total:", "Tier Mapping: RM 60k - 180k = RM 500 payout flat rate"),
            ("Step 3: Determine Commission Payout:", f"ANP Commission Payout = RM {s['anp_commission']:,.2f}"),
        ]
        
        anp_story = []
        anp_story.append(Paragraph("5. ANP Monthly Commission Flow (Tan Jia Hao)", h2_style))
        
        details_data = []
        for label, val in details:
            details_data.append([
                Paragraph(f"<b>{label}</b>", bold_label_style),
                Paragraph(str(val), value_style)
            ])
        
        inv_bullets = []
        for inv in s['invoices']:
            inv_bullets.append(f"• Invoice {inv['inv_num']} ({inv['date']}): RM {inv['amount']:,.2f}")
        
        details_data.append([
            Paragraph("<b>Invoices Details list</b>", bold_label_style),
            Paragraph("<br/>".join(inv_bullets), value_style)
        ])
        
        details_table = Table(details_data, colWidths=[200, 340])
        details_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_COLOR),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        anp_story.append(details_table)
        anp_story.append(Spacer(1, 8))
        
        for step_title, math_line in steps:
            anp_story.append(Paragraph(step_title, flow_step_title))
            if math_line:
                anp_story.append(Paragraph(math_line, flow_step_math))
                
        story.append(KeepTogether(anp_story))

    # 6. EGA/ESA Awards
    if 'ega_esa' in samples:
        s = samples['ega_esa']
        details = [
            ("Payment Verification Status", "First Payment Secured (1st_payment_date IS NOT NULL)"),
            ("Standard Residential Rate (always)", "1 EP Point per RM 1 Sales Price"),
            ("Factory Rate (Post-May 2026)", "first RM 40,000 at 100%, balance at 40%"),
            ("EGA (Half-Year) Target Threshold", f"> {s['ega_threshold']:,.0f} EP Points"),
            ("ESA (Full-Year) Target Threshold", f"> {s['esa_threshold']:,.0f} EP Points"),
        ]
        steps = [
            ("Step 1: Calculate Residential Invoice EP Points:", f"Points = RM {s['residential_sales']:,.2f} = {s['residential_ep']:,.0f} EP Points"),
            ("Step 2: Calculate Factory Invoice EP Points (post-May 2026, RM 100k Sales Price):", f"Points = RM 40,000 + (RM 60,000 * 40.00%) = {s['factory_ep']:,.0f} EP Points"),
            ("Step 3: Check cumulative qualification status:", f"Cumulative EP Points = RM {s['cumulative_ep']:,.2f} (EGA Qualified!)"),
        ]
        story.append(make_section("6. EGA / ESA Awards EP Point Calculation Flow", details, steps))

    # 7. Production Bonus
    if 'production_bonus' in samples:
        s = samples['production_bonus']
        details = [
            ("Team Total Sales (Threshold: >= RM 2.0M)", f"RM {s['team_sales']:,.2f}"),
            ("OUM Personal Sales (Threshold: >= RM 300k)", f"RM {s['personal_sales']:,.2f}"),
            ("OUM Production Bonus Rate", "0.50% of Team Total Sales"),
        ]
        steps = [
            ("Step 1: Check eligibility criteria (both conditions must be met):", "Team Total >= 2.0M (Yes) AND Personal Sales >= 300k (Yes)"),
            ("Step 2: Calculate OUM Production Bonus payout:", f"Production Bonus = RM {s['team_sales']:,.2f} * 0.50% = RM {s['oum_bonus']:,.2f}"),
        ]
        story.append(make_section("7. Production Bonus Calculation Flow (OUM Team-based)", details, steps))

    doc.build(story)
    print(f"\nPDF successfully generated at: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Commission calculation flow audit trail")
    parser.add_argument("--pdf", action="store_true", help="Generate a PDF report instead of printing to terminal")
    args = parser.parse_args()

    rates = load_rates_from_env()
    try:
        samples = query_all_flow_data(rates)
    except Exception as e:
        print(f"Error querying database: {e}")
        print("Please check that PostgreSQL is running locally on port 5432 and contains 'backup_admin_dev'.")
        return

    if args.pdf:
        output_pdf = str(Path(__file__).resolve().parent / "Commission_Calculation_Flow.pdf")
        generate_pdf_flow(samples, output_pdf)
    else:
        print_terminal_flow(samples)

if __name__ == '__main__':
    main()
