import sys

with open('build_finance_commission_pack(outsource).py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace basic import and variables
content = content.replace(
    'basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "full_internal_basic_commission.py"\n    basic = _load_module("basic_commission", basic_path)',
    'basic_path = REPO_ROOT / "1. Basic Commission" / "3. Python Script" / "outsource_basic_commission.py"\n    basic = _load_module("outsource_basic_commission", basic_path)'
)

# ANP Block 1 - remove call
content = content.replace(
    'anp_summary, anp_detail, anp_meta = fetch_anp(year)',
    'anp_summary, anp_detail, anp_meta = [], [], {"agents":0, "invoices":0, "total_commission":0, "filter":""}'
)
content = content.replace(
    'print(f"  ANP: {anp_meta[\'agents\']} agents, {anp_meta[\'invoices\']} invoices")',
    ''
)

# Replace NFP import
content = content.replace(
    'nfp_path = nfp_dir / "nfp_commission.py"\n    nfp = _load_module("nfp_commission", nfp_path)',
    'nfp_path = nfp_dir / "outsource_nfp_commission.py"\n    nfp = _load_module("outsource_nfp_commission", nfp_path)'
)

# Replace EGA ESA import
content = content.replace(
    'ega_path = ega_dir / "full_internal_EGA_ESA_Awards.py"\n    ega = _load_module("ega_esa", ega_path)',
    'ega_path = ega_dir / "outsource_EGA_ESA_Awards.py"\n    ega = _load_module("outsource_EGA_ESA_Awards", ega_path)'
)

# Remove ANP in build_workbook
remove_wb_1 = """    if not MOCK_MODE:
        anp_mod = sys.modules["anp_commission"]
        anp_summary_table = anp_mod.summary_table_rows(anp_summary)
        wb.remove(wb["ANP - By Agent"])
        _append_sheet(
            wb,
            "ANP - By Agent",
            ["Agent Name", "Invoice Count", "Accumulated Total (RM)", "ANP Commission (RM)"],
            anp_summary_table,
            {3, 4},
        )"""
content = content.replace(remove_wb_1, "")

remove_wb_2 = """    ws_anp_det = wb.create_sheet("ANP - By Invoice")
    write_excel_stacked_details(
        ws_anp_det,
        "ANP Commission",
        ["Agent Name", "Customer Name", "Invoice Count", "Total Sales (RM)", "Commission Rate", "ANP Commission"],
        get_anp_monthly_tables(anp_detail),
        {4, 6}
    )"""
content = content.replace(remove_wb_2, "")

# Remove ANP in build_pdf call
content = content.replace(
    'anp_summary, anp_detail, anp_meta = fetch_anp(year, h1_only=True)',
    'anp_summary, anp_detail, anp_meta = [], [], {"agents":0, "invoices":0, "total_commission":0, "filter":""}'
)

remove_pdf_1 = """    anp_summary_table = []
    if MOCK_MODE:
        anp_summary_table = [[r["agent_name"], str(r["invoice_count"]), f"{r['accumulated_total_amount']:.2f}", f"{r['anp_commission']:.2f}"] for r in anp_summary]
    else:
        anp_mod = sys.modules["anp_commission"]
        anp_summary_table = _rows_to_str(anp_mod.summary_table_rows(anp_summary))"""
content = content.replace(remove_pdf_1, "    anp_summary_table = []")

# ANP pdf sections
remove_pdf_2 = """        PdfSection(
            "ANP Commission Details",
            [],
            anp_matrix_rows,
            landscape=True,
            total_agents=anp_meta["agents"],
            total_customers=len(anp_detail),
        ),"""
content = content.replace(remove_pdf_2, "")

# Change Title
content = content.replace('Commission_Pack_', 'Outsource_Commission_Pack_')

with open('build_finance_commission_pack(outsource).py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch applied.")
