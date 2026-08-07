import os
import sys
from pathlib import Path
from datetime import date

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# Adjust path to net_floor_prices.py
sys.path.insert(0, str(REPO_ROOT / "2. NFP Commission" / "3. Python script"))

from net_floor_prices import load_650w_schedules, load_620w_schedule, lookup_net_floor_price, get_default_excel_path

schedules_650 = load_650w_schedules(get_default_excel_path())
schedule_620 = load_620w_schedule(REPO_ROOT / "2. NFP Commission" / "4. data" / "nfp_620w_schedule.json")

# Let's check lookup for INV-1008343:
# Date: Feb 12, 2026
# Qty: 18 panels
# Rating: 650W
# Phase: 3-phase (since SEDA phase_type is '3')
# TNG rebate: False

for phase in [False, True]:
    nfp, sheet = lookup_net_floor_price(
        invoice_date=date(2026, 2, 12),
        panel_qty=18,
        panel_rating=650,
        has_tng_rebate=False,
        schedules_650=schedules_650,
        schedule_620=schedule_620,
        three_phase=phase
    )
    print(f"Phase: {'3-phase' if phase else '1-phase'}, NFP: {nfp}, Sheet: {sheet}")
