import os
import re
import sys
import calendar
import datetime
import requests
import json
import pandas as pd
import numpy as np
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

# Ensure stdout uses UTF-8 to handle Chinese characters in agent/customer names
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Constants
PROXY_URL = "https://pg-proxy-production.up.railway.app/api/sql"
DB_NAME = "prod_main"
FALLBACK_TOKEN = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3ODQyNTEzMTcsImV4cCI6MTgyMDI1MjQyOCwiZGJfbmFtZSI6InByb2RfbWFpbiIsImFjY2VzcyI6InJlYWRfb25seSIsInByb3h5X3VybCI6Imh0dHBzOi8vcGctcHJveHktcHJvZHVjdGlvbi51cC5yYWlsd2F5LmFwcC8iLCJhcGlfZG9jc191cmwiOiJodHRwczovL3BnLXByb3h5LXByb2R1Y3Rpb24udXAucmFpbHdheS5hcHAvZG9jcyJ9.0phLfmZ3JhBMewbn9bCSndCE4yJ346KUw9-E4GproDo"

def _normalize_sheet_name(name):
    return re.sub(r'\s+', ' ', str(name)).strip().casefold()


def resolve_sheet_name(sheet_names, prefix, month):
    """Find the sheet for `month` whose name starts with `prefix`.

    Sheets are named inconsistently by hand ('Agent Close Case Jun' but
    'Agent Close Case July'), so both the abbreviated and full month spellings
    are accepted, and whitespace/case differences are ignored.
    """
    candidates = {
        _normalize_sheet_name(f"{prefix} {calendar.month_abbr[month]}"),
        _normalize_sheet_name(f"{prefix} {calendar.month_name[month]}"),
    }
    for name in sheet_names:
        if _normalize_sheet_name(name) in candidates:
            return name
    raise ValueError(
        f"No '{prefix}' sheet for {calendar.month_name[month]} in the Monthly Contest workbook. "
        f"Available sheets: {', '.join(repr(s) for s in sheet_names)}"
    )


TBC_PLACEHOLDER = "Name TBC"

# Same person spelled differently between the roster and case tables, where
# normalizing case/whitespace is not enough. Left side is the variant, right
# side the spelling the rest of the workbook uses.
#
# 'oliver' -> 'olivier' was removed (2026-07): Oliver and Olivier are two
# different agents — Oliver is Brendon Lew Chok Yew (Internal), Olivier is
# Olivier Koh Cong Lee (Outsource) — so folding them together merged two
# people's cases onto one name.
NICKNAME_ALIASES = {}


def agent_key(name):
    """Normalize a nickname to a single canonical lookup key.

    The roster and case tables are typed independently, so the same person
    appears as 'Elyn'/'ELYN', 'Jia Xuan'/'JiaXuan' and 'Olivier'/'Oliver'.
    """
    key = re.sub(r'\s+', '', str(name)).casefold()
    return NICKNAME_ALIASES.get(key, key)


CONTEST_TEAMS = ["England", "France", "Germany", "Brazil", "Spain", "Portugal"]

# The contest rules as they stood before they moved onto the dashboard Data
# page. Used whenever a month has no saved rule set, so any month calculated
# before the Data page existed keeps producing exactly the same figures.
DEFAULT_CONTEST_RULES = {
    "full_rate_cap": 40000.0,
    "above_cap_pct": 40.0,
    "activity_bonus_enabled": True,
    "activity_bonus_points": 80000.0,
    "target_bonus_points": 50000.0,
    "rank_awards": {1: 5000.0, 2: 3000.0, 3: 2000.0},
    "achievement_bonus": 2000.0,
    "gb_awards": {1: 3000.0, 2: 2000.0, 3: 1000.0},
    "gb_min_cases": 3,
    "gb_min_sales": 150000.0,
    "fast_start_gift": "Adidas Official FIFA World Cup Jersey (RM 320)",
    "fast_start_slots": None,          # None = uncapped, matching the old behaviour
    "fast_start_unpaid": "exclude",
    "teams": {
        "England": {"branch": "JB 1", "captain": "Vincent Tan", "original_target": 600000.0, "handicap": 0.0},
        "France": {"branch": "JB 2", "captain": "Sunny", "original_target": 600000.0, "handicap": 0.0},
        "Germany": {"branch": "JB 3", "captain": "Carol", "original_target": 600000.0, "handicap": 0.0},
        "Brazil": {"branch": "Klang", "captain": "Jiawei", "original_target": 600000.0, "handicap": 0.0},
        "Spain": {"branch": "Kluang 1", "captain": "Kent", "original_target": 450000.0, "handicap": 150000.0},
        "Portugal": {"branch": "Kluang 2", "captain": "Wingon", "original_target": 450000.0, "handicap": 150000.0},
    },
}


def _rule_float(value, default):
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _rule_int(value, default):
    result = _rule_float(value, None)
    return default if result is None else int(result)


def load_contest_rules(year, month):
    """The rule set for one month, as entered on the dashboard Data page.

    Falls back to DEFAULT_CONTEST_RULES when the dashboard database is
    unreachable or the month has not been set up, so this script keeps working
    standalone (and old months keep reproducing) without the dashboard.
    """
    key = f"{int(year):04d}-{int(month):02d}"
    dashboard_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "8. Web Dashboard",
    )
    try:
        if dashboard_dir not in sys.path:
            sys.path.insert(0, dashboard_dir)
        import db as dashboard_db
        payload = dashboard_db.get_contest_rules(key)
        if not payload.get("saved"):
            print(f"[Contest Rules] No rule set saved for {key}; using built-in defaults.")
            return dict(DEFAULT_CONTEST_RULES)
    except Exception as e:
        print(f"[Contest Rules] Warning: could not read rules from the dashboard DB "
              f"({type(e).__name__}: {e}); using built-in defaults.")
        return dict(DEFAULT_CONTEST_RULES)

    print(f"[Contest Rules] Loaded {key} from the dashboard Data page.")
    return normalize_contest_rules(payload)


def load_agent_display_names(year, month):
    """Map a contest nickname to the name to print on the report.

    Takes 'Full Name' from the dashboard Data page's agent roster, falling back
    to 'Agent Name (from eeAdmin)' when Full Name is blank. Rows are
    effective-dated, so a later row that has been left blank does not wipe out a
    name entered earlier. Returns {normalized nickname: display name}; anything
    not in the roster keeps its nickname.
    """
    key = f"{int(year):04d}-{int(month):02d}"
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dashboard_dir = os.path.join(repo_root, "8. Web Dashboard")
    rates_dir = os.path.join(repo_root, "1. Basic Commission", "3. Python Script")
    try:
        if dashboard_dir not in sys.path:
            sys.path.insert(0, dashboard_dir)
        if rates_dir not in sys.path:
            sys.path.insert(0, rates_dir)
        import db as dashboard_db
        # An effective month may be a closed range ("2026-01 to 2026-08"); the
        # shared helpers are the only thing that reads both spellings correctly.
        from basic_commission_rates import effective_covers, effective_start
        rows = dashboard_db.list_agent_roles()
    except Exception as e:
        print(f"[Agent Names] Warning: could not read the agent roster "
              f"({type(e).__name__}: {e}); report will show contest nicknames.")
        return {}

    buckets = {}
    for row in rows:
        # A role that starts after this month must not name it, and one whose
        # range has already ended must not either.
        if not effective_covers(row.get('effective_from') or '', key):
            continue
        nick = str(row.get('nick_name') or '').strip()
        if not nick:
            continue
        buckets.setdefault(agent_key(nick), []).append(row)

    names = {}
    for nick_key, group in buckets.items():
        group.sort(key=lambda r: effective_start(r.get('effective_from') or ''), reverse=True)
        full = next((str(r.get('full_name')).strip() for r in group
                     if str(r.get('full_name') or '').strip()), "")
        eeadmin = next((str(r.get('agent')).strip() for r in group
                        if str(r.get('agent') or '').strip()), "")
        resolved = full or eeadmin
        if resolved:
            names[nick_key] = resolved

    # The eeAdmin name is also worth indexing, for roster entries that carry no
    # nickname but whose agent name is what the contest sheet types. Never
    # overwrite a nickname match, which is the more specific signal.
    for row in rows:
        if not effective_covers(row.get('effective_from') or '', key):
            continue
        eeadmin = str(row.get('agent') or '').strip()
        if not eeadmin:
            continue
        names.setdefault(agent_key(eeadmin),
                         str(row.get('full_name') or '').strip() or eeadmin)

    print(f"[Agent Names] Resolved {len(names)} roster names for {key}.")
    return names


def load_contest_roster(year, month):
    """The month's eligible candidates and their case amounts, as entered on the
    dashboard Data page.

    Returns (df_left, df_cases) shaped exactly like the workbook blocks the rest
    of this script expects, or None when the month has no roster saved — in
    which case the caller falls back to the Excel workbook so older months keep
    reproducing.
    """
    key = f"{int(year):04d}-{int(month):02d}"
    dashboard_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "8. Web Dashboard",
    )
    try:
        if dashboard_dir not in sys.path:
            sys.path.insert(0, dashboard_dir)
        import db as dashboard_db
        rows = dashboard_db.get_contest_rules(key).get("roster") or []
    except Exception as e:
        print(f"[Contest Roster] Warning: could not read the roster "
              f"({type(e).__name__}: {e}); falling back to the workbook.")
        return None

    if not rows:
        return None

    left_rows, case_rows = [], []
    for row in rows:
        agent = str(row.get("agent") or "").strip()
        team = str(row.get("team") or "").strip()
        if not agent or not team:
            continue
        raw = row.get("sales_prices")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except ValueError:
                raw = []
        amounts = []
        for value in (raw or []):
            try:
                amounts.append(float(str(value).replace(",", "").strip()))
            except (TypeError, ValueError):
                continue
        # An agent with no cases still belongs on the roster: they are part of
        # the activity-bonus denominator.
        left_rows.append({"Team": team, "Agent": agent,
                          "Total Amount (RM)": sum(amounts), "Invoice Count": len(amounts)})
        for amount in amounts:
            case_rows.append({"Agent.1": agent, "Total Amount (RM).1": amount,
                              "Description/Source": ""})

    print(f"[Contest Roster] Loaded {len(left_rows)} candidates and "
          f"{len(case_rows)} cases for {key} from the dashboard Data page.")
    return pd.DataFrame(left_rows), pd.DataFrame(case_rows)


def normalize_contest_rules(payload):
    """Turn the Data page's all-text payload into typed rules, filling any blank
    field from the defaults so a half-completed month cannot produce nonsense."""
    d = DEFAULT_CONTEST_RULES
    raw = payload.get("rules") or {}

    slots_raw = str(raw.get("fast_start_slots") or "").strip()
    teams = {}
    for row in payload.get("teams") or []:
        name = str(row.get("team") or "").strip()
        if name not in d["teams"]:
            continue
        fallback = d["teams"][name]
        teams[name] = {
            "branch": row.get("branch") or fallback["branch"],
            "captain": row.get("captain") or fallback["captain"],
            "original_target": _rule_float(row.get("original_target"), fallback["original_target"]),
            "handicap": _rule_float(row.get("handicap"), fallback["handicap"]),
        }
    for name, fallback in d["teams"].items():
        teams.setdefault(name, dict(fallback))

    return {
        "full_rate_cap": _rule_float(raw.get("full_rate_cap"), d["full_rate_cap"]),
        "above_cap_pct": _rule_float(raw.get("above_cap_pct"), d["above_cap_pct"]),
        "activity_bonus_enabled": bool(int(raw.get("activity_bonus_enabled") or 0)),
        "activity_bonus_points": _rule_float(raw.get("activity_bonus_points"), d["activity_bonus_points"]),
        "target_bonus_points": _rule_float(raw.get("target_bonus_points"), d["target_bonus_points"]),
        "rank_awards": {
            1: _rule_float(raw.get("rank1_award"), d["rank_awards"][1]),
            2: _rule_float(raw.get("rank2_award"), d["rank_awards"][2]),
            3: _rule_float(raw.get("rank3_award"), d["rank_awards"][3]),
        },
        "achievement_bonus": _rule_float(raw.get("achievement_bonus"), d["achievement_bonus"]),
        "gb_awards": {
            1: _rule_float(raw.get("gb1_award"), d["gb_awards"][1]),
            2: _rule_float(raw.get("gb2_award"), d["gb_awards"][2]),
            3: _rule_float(raw.get("gb3_award"), d["gb_awards"][3]),
        },
        "gb_min_cases": _rule_int(raw.get("gb_min_cases"), d["gb_min_cases"]),
        "gb_min_sales": _rule_float(raw.get("gb_min_sales"), d["gb_min_sales"]),
        "fast_start_gift": (raw.get("fast_start_gift") or "").strip() or d["fast_start_gift"],
        # Blank means uncapped: every agent reaching two cases is a recipient,
        # which is how the award ran before the race was configurable.
        "fast_start_slots": _rule_int(slots_raw, None) if slots_raw else None,
        "fast_start_unpaid": (raw.get("fast_start_unpaid") or d["fast_start_unpaid"]).strip(),
        "teams": teams,
    }


def parse_amount(value):
    """Coerce a hand-entered amount cell to float.

    Amounts are sometimes typed as text with thousands separators ('50, 300'),
    which Excel stores verbatim rather than as a number.
    """
    if isinstance(value, str):
        cleaned = re.sub(r'[,\s]', '', value)
        if not re.fullmatch(r'-?\d*\.?\d+', cleaned):
            raise ValueError(f"Cannot read amount {value!r} from the Monthly Contest workbook")
        return float(cleaned)
    return float(value)


def load_auth_token():
    """Load token from environment variables, sibling directory file, or fallback to default."""
    token = os.environ.get("POSTGRES_PROXY_TOKEN") or os.environ.get("PG_PROXY_TOKEN")
    if token:
        print("Loaded token from environment variable.")
        return token if token.startswith("Bearer ") else f"Bearer {token}"
        
    # Try reading from relative file path to NFP commission data
    relative_path = os.path.join("..", "2. NFP Commission", "4. data", "pg_proxy_token.txt")
    if os.path.exists(relative_path):
        try:
            with open(relative_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    print(f"Loaded token from file: {relative_path}")
                    return token if token.startswith("Bearer ") else f"Bearer {token}"
        except Exception as e:
            print(f"Warning: Failed to read token file: {e}")
            
    print("Using hardcoded fallback token.")
    return FALLBACK_TOKEN

def fetch_invoices_from_db(token):
    """Query all invoices from database with linked package, agent, and customer data."""
    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }
    
    # We query invoices since December 2025 to cover cases that were invoiced earlier but paid/closed in June 2026.
    sql = """
    SELECT 
        inv.id,
        inv.invoice_number,
        inv.invoice_date,
        inv."1st_payment_date",
        inv.total_amount as inv_total_amount,
        pkg.price as pkg_price,
        pkg.nett_price as pkg_nett_price,
        a.name as agent_name,
        c.name as customer_name
    FROM invoice inv
    LEFT JOIN package pkg ON inv.linked_package = pkg.bubble_id
    LEFT JOIN (
      SELECT DISTINCT ON (au.bubble_id) au.bubble_id, au.name
      FROM (
        SELECT u.bubble_id, u.name, 1 AS pri FROM "user" u
         WHERE u.bubble_id IS NOT NULL AND COALESCE(BTRIM(u.agent_type), '') <> ''
        UNION ALL
        SELECT ag.bubble_id, ag.name, 2 FROM agent ag
         WHERE ag.bubble_id IS NOT NULL
        UNION ALL
        SELECT u2.bubble_id, u2.name, 3 FROM "user" u2
         WHERE u2.bubble_id IS NOT NULL
      ) au
      ORDER BY au.bubble_id, au.pri
    ) a ON inv.linked_agent = a.bubble_id
    LEFT JOIN customer c ON inv.linked_customer = c.customer_id
    WHERE inv.invoice_date >= '2025-12-01'
    -- Ordered explicitly: case-to-invoice matching is greedy, so it consumes
    -- invoices in the order they arrive. Without ORDER BY, Postgres may return
    -- the same rows in a different sequence on each run and the same month can
    -- produce different award figures.
    ORDER BY inv.id
    """
    
    payload = {
        "db_name": DB_NAME,
        "sql": sql,
        "params": []
    }
    
    try:
        response = requests.post(PROXY_URL, json=payload, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Error connecting to Postgres Proxy API: Status Code {response.status_code}. Response: {response.text}")
            
        res_json = response.json()
        if 'error' in res_json:
            raise RuntimeError(f"API Error: {res_json['error']}")
            
        rows = res_json.get('rows', [])
        df = pd.DataFrame(rows)
        # Convert total amount to numeric float
        df['inv_total_amount'] = pd.to_numeric(df['inv_total_amount'], errors='coerce')
        df['pkg_price'] = pd.to_numeric(df['pkg_price'], errors='coerce')
        return df
        
    except Exception as e:
        raise RuntimeError(f"Exception during DB query: {e}")

def match_excel_cases_to_db(df_excel_cases, df_db, agent_map):
    """
    Perform greedy nearest-neighbor matching to align Excel closed cases with database invoices.
    Prioritizes agent matches, then tries global searches for amounts.
    """
    matched_ids = set()
    matches_list = []
    
    for idx, row in df_excel_cases.iterrows():
        ex_agent = row['Agent.1']
        ex_amount = row['Total Amount (RM).1']
        ex_desc = row['Description/Source']
        
        db_agent_name = agent_map.get(agent_key(ex_agent))
        selected_invoice = None

        # Agents not yet created in the database: keep the Excel figures so team
        # revenue stays right, but do not fall through to amount-only matching,
        # which would attach another agent's invoice to them.
        if db_agent_name is None:
            matches_list.append({
                'Excel Agent': ex_agent,
                'Excel Amount': ex_amount,
                'Excel Desc': ex_desc,
                'DB Inv Num': TBC_PLACEHOLDER,
                'DB Cust': TBC_PLACEHOLDER,
                'DB Inv Date': None,
                'DB 1stPay': None,
                'DB PkgPrice': float(ex_amount),
                'DB SalesPrice': float(ex_amount),
                'DB Agent': TBC_PLACEHOLDER
            })
            continue

        if db_agent_name:
            # 1. Search for this specific agent's invoices
            agent_invoices = df_db[df_db['agent_name'] == db_agent_name]
            unmatched_in_db = agent_invoices[~agent_invoices['id'].isin(matched_ids)].copy()
            
            if not unmatched_in_db.empty:
                # Find nearest amount mismatch
                unmatched_in_db['diff'] = abs(unmatched_in_db['inv_total_amount'] - ex_amount)
                unmatched_in_db = unmatched_in_db.sort_values(by='diff')
                best_match = unmatched_in_db.iloc[0]
                # Accept if difference is reasonable (under 5,000 RM)
                if best_match['diff'] < 5000:
                    selected_invoice = best_match
                    matched_ids.add(selected_invoice['id'])
                    
        # 2. If no agent match, search globally for unmatched invoices with the exact same amount
        if selected_invoice is None:
            global_matches = df_db[abs(df_db['inv_total_amount'] - ex_amount) < 1.0]
            unmatched_global = global_matches[~global_matches['id'].isin(matched_ids)].copy()
            if not unmatched_global.empty:
                # Sort to prioritize those with payments
                unmatched_global['has_payment'] = unmatched_global['1st_payment_date'].notnull()
                unmatched_global = unmatched_global.sort_values(by=['has_payment', 'invoice_date'], ascending=[False, True])
                selected_invoice = unmatched_global.iloc[0]
                matched_ids.add(selected_invoice['id'])
                print(f"Matched {ex_agent} ({ex_amount}) globally to agent: {selected_invoice['agent_name']}")
            else:
                # 3. Last fallback: search globally with loose threshold (under 2,000 RM difference)
                global_loose = df_db[abs(df_db['inv_total_amount'] - ex_amount) < 2000.0]
                unmatched_global_loose = global_loose[~global_loose['id'].isin(matched_ids)].copy()
                if not unmatched_global_loose.empty:
                    unmatched_global_loose['has_payment'] = unmatched_global_loose['1st_payment_date'].notnull()
                    unmatched_global_loose = unmatched_global_loose.sort_values(by=['has_payment', 'invoice_date'], ascending=[False, True])
                    selected_invoice = unmatched_global_loose.iloc[0]
                    matched_ids.add(selected_invoice['id'])
                    print(f"Matched {ex_agent} ({ex_amount}) globally (loose) to agent: {selected_invoice['agent_name']} (amount: {selected_invoice['inv_total_amount']})")
                else:
                    print(f"CRITICAL WARNING: No database invoice match found for {ex_agent} (RM {ex_amount})")
                    
        if selected_invoice is not None:
            # Fallback for standard package price
            pkg_price = selected_invoice['pkg_price']
            if pd.isnull(pkg_price) or pkg_price == 0:
                pkg_price = selected_invoice['inv_total_amount']
                
            matches_list.append({
                'Excel Agent': ex_agent,
                'Excel Amount': ex_amount,
                'Excel Desc': ex_desc,
                'DB Inv Num': selected_invoice['invoice_number'],
                'DB Cust': selected_invoice['customer_name'] or "N/A",
                'DB Inv Date': selected_invoice['invoice_date'],
                'DB 1stPay': selected_invoice['1st_payment_date'],
                'DB PkgPrice': float(pkg_price),
                'DB SalesPrice': float(selected_invoice['inv_total_amount']),
                'DB Agent': selected_invoice['agent_name']
            })
            
    return pd.DataFrame(matches_list)

def calc_counted_revenue(sales_price, full_rate_cap=40000.0, above_cap_pct=40.0):
    """Apply Case Revenue Recognition: 100% of the amount up to the cap, then a
    reduced rate above it. Cap and rate come from the month's rule set."""
    if sales_price <= full_rate_cap:
        return sales_price
    return full_rate_cap + (sales_price - full_rate_cap) * (above_cap_pct / 100.0)

def style_excel_sheet(ws, title, columns_count):
    """Apply premium formatting to the worksheet headers, gridlines, and fonts."""
    # Show Gridlines
    ws.views.sheetView[0].showGridLines = True
    
    # Fonts
    font_family = "Segoe UI"
    title_font = Font(name=font_family, size=16, bold=True, color="1F4E78")
    header_font = Font(name=font_family, size=11, bold=True, color="FFFFFF")
    
    # Fills
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    
    # Title Row
    ws.insert_rows(1, 2)
    ws.cell(row=1, column=1, value=title).font = title_font
    ws.row_dimensions[1].height = 25
    ws.row_dimensions[2].height = 10
    
    # Header Row is now row 3
    header_row_idx = 3
    ws.row_dimensions[header_row_idx].height = 24
    
    # Style Header Cells
    thin_border = Border(
        left=Side(style='thin', color='BFBFBF'),
        right=Side(style='thin', color='BFBFBF'),
        top=Side(style='thin', color='BFBFBF'),
        bottom=Side(style='thin', color='BFBFBF')
    )
    
    for col in range(1, columns_count + 1):
        cell = ws.cell(row=header_row_idx, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border

def auto_fit_columns(ws, start_row=3):
    """Auto-adjust worksheet column widths to prevent truncation and '###' errors."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col[start_row-1:]:
            val = str(cell.value or '')
            if cell.number_format and ('0.00' in cell.number_format or '$' in cell.number_format):
                # format approximation length
                val += "   "
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

def calc_fast_start(df_matches, df_agent_totals, rules):
    """Fast Start is a race, not a threshold: agents are ranked by when they
    completed their SECOND qualified case (its 1st payment date) and the first N
    finishers win. A blank slot count means uncapped, which is how the award ran
    before it became configurable.

    Cases with no 1st payment date have not completed, so by default they are
    excluded from the race; 'last' instead sorts them behind every dated case.
    """
    slots = rules.get('fast_start_slots')
    gift = rules['fast_start_gift']
    exclude_unpaid = rules.get('fast_start_unpaid', 'exclude') != 'last'

    finish = {}
    for agent, grp in df_matches.groupby('Excel Agent'):
        dates = pd.to_datetime(grp['DB 1stPay'], errors='coerce')
        dated = sorted(d for d in dates if pd.notnull(d))
        undated = int(dates.isnull().sum())
        if exclude_unpaid:
            qualified = dated
        else:
            # Undated cases still count towards the two, but only after the
            # dated ones, so they can never win the race outright.
            qualified = dated + [pd.Timestamp.max] * undated
        if len(qualified) < 2:
            continue
        finish[agent] = qualified[1]

    sales = dict(zip(df_agent_totals['Agent'], df_agent_totals['Sales Sum']))
    # Payment dates are day-granular, so ties are likely: the larger seller
    # takes the earlier place, and a genuine dead heat awards everyone tied
    # rather than dropping someone arbitrarily.
    ranked = sorted(finish.items(), key=lambda kv: (kv[1], -float(sales.get(kv[0], 0) or 0)))

    winners = []
    for agent, when in ranked:
        if slots is None or len(winners) < slots:
            winners.append(agent)
            continue
        last_agent = winners[-1]
        if finish[last_agent] == when and float(sales.get(last_agent, 0) or 0) == float(sales.get(agent, 0) or 0):
            winners.append(agent)
        else:
            break

    if slots is not None and len(winners) > slots:
        print(f"[Fast Start] {len(winners)} recipients for {slots} slots — tied on both "
              f"date and sales, so all tied agents are included.")

    return {agent: (gift if agent in set(winners) else "") for agent in df_agent_totals['Agent']}


def calculate_monthly_contest(token=None, base_path=None, month=None, year=None, rules=None):
    if month is None:
        month = datetime.date.today().month
    month = int(month)
    if year is None:
        year = datetime.date.today().year
    year = int(year)
    # The caller (the dashboard) can hand the rules over directly, since it
    # already holds a database connection; the CLI resolves them itself.
    if rules is None:
        rules = load_contest_rules(year, month)
    if base_path is None:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    else:
        base_path = str(base_path)
    if token is None:
        token = load_auth_token()
    if token and not token.startswith("Bearer "):
        token = f"Bearer {token}"
    df_db = fetch_invoices_from_db(token)

    # The roster and its case amounts come from the dashboard Data page. The
    # workbook is only read when a month has not been entered there yet, so
    # months predating the Data page still reproduce.
    roster = load_contest_roster(year, month)
    if roster is not None:
        df_left, df_cases = roster
    elif os.environ.get("CONTEST_ALLOW_WORKBOOK") == "1":
        # Escape hatch only. The contest reads its roster from Supabase; the
        # workbook path is kept behind this flag so a month that predates the
        # Data page can still be reproduced if it is ever needed.
        print("[Contest Roster] CONTEST_ALLOW_WORKBOOK=1: reading the workbook.")
        excel_path = os.path.join(base_path, "1. Excel", "1. Monthly Contest.xlsx")
        if not os.path.exists(excel_path):
            raise FileNotFoundError(
                f"No roster saved for {year}-{month:02d} on the Data page, and no "
                f"workbook to fall back on at: {excel_path}")
        sheet_name = resolve_sheet_name(pd.ExcelFile(excel_path).sheet_names, "Agent Close Case", month)
        # Sheets have been re-laid-out by hand before (June 2026 gained a
        # "Table 1"/"Table 2" title row), so find the header rather than
        # assuming it is the first row.
        raw = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)
        header_row = 0
        for i in range(min(6, len(raw))):
            values = [str(v).strip() for v in raw.iloc[i].tolist()]
            if "Team" in values and "Agent" in values:
                header_row = i
                break
        df_excel_agent = pd.read_excel(excel_path, sheet_name=sheet_name, header=header_row)
        df_left_raw = df_excel_agent[['Team', 'Agent', 'Total Amount (RM)', 'Invoice Count']].dropna(subset=['Agent'])
        df_left = df_left_raw[df_left_raw['Agent'] != 'GRAND TOTAL'].copy()
        df_cases_raw = df_excel_agent[['Agent.1', 'Total Amount (RM).1', 'Description/Source']].dropna(subset=['Agent.1', 'Total Amount (RM).1'])
        df_cases = df_cases_raw[df_cases_raw['Agent.1'] != 'GRAND TOTAL'].copy()
        df_cases['Total Amount (RM).1'] = df_cases['Total Amount (RM).1'].apply(parse_amount)
    else:
        raise ValueError(
            f"No Monthly Contest roster saved for {year}-{month:02d}. Enter the "
            f"eligible candidates on the dashboard Data page (Monthly Contest). "
            f"To reproduce a month from the old workbook instead, set "
            f"CONTEST_ALLOW_WORKBOOK=1."
        )
    agent_map = {
        'ZH': 'CHING ZHE HANG', 'Vincent Tan': 'Vincent Tan', 'Jiawei': 'CHAN JIA WEI', 'Dean': 'DEAN WAI LEONG YEE',
        'Carol': 'Carol Siow', 'Wingon': 'CHAN WING ON', 'Jiahao': 'TAN JIA HAO', 'Martin Hing': 'MARTIN HING ',
        'Ye Hong': 'CHOONG YE HONG', 'Jia Xuan': 'wong jia xuan', 'Olivier': 'OLIVIER KOH CONG LEE', 'Elyn': 'Pua Yee Ling ',
        'Louis': 'Ng Zhan Yi', 'ZUL': 'ZUL', 'Joshua': 'JOSHUA YAP JIA HAO', 'Winson Tan': 'Tan Sue Cherk',
        'LK': 'LING LIANG KANG', 'CJ LOO': 'LOO CHEW YIN', 'Wilson': 'Wilson Tan Wei Sheng', 'Denise Ng': 'Denise Ng Pei sing ',
        'Sunny': 'Sunny Tan', 'ANISAH': 'NUR ANISAH BINTI MOHD AMIRUDDIN @ CHUMATI', 'Jia keat': 'WOON JIA KEAT',
        'Jerry': 'Lim Kok Tong', 'Kent': 'Teng Kah Kent', 'Azhar': 'Mohd Azhar Bin Ibrahim', 'Ivan': 'LOW CHIN CHAI',
        'Wei Feng': 'KHOO WEI FENG', 'NAJWA': 'NURULATHIRAH NAJWA BINTI MOHD ADIP',
        # July 2026 roster. Full names verified against the database agent
        # table (exact casing/spacing matters).
        'Jay': 'Koh Yi Jay', 'Phil Moo': 'Phil Moo',
        # 'Oliver' deliberately absent: it used to point at OLIVIER KOH CONG LEE,
        # but Oliver (Brendon Lew Chok Yew) is a different agent from Olivier.
        # Without an entry an 'Oliver' case is reported as "Name TBC" rather than
        # being attributed to the wrong person.
        'Denise': 'Denise Ng Pei sing ',
        'JS': 'Lim Chin Seng ', 'Hongki': 'YOU TENG HOONG', 'Viggy': 'Gui choon fong',
    }
    agent_map = {agent_key(k): v for k, v in agent_map.items()}

    df_matches = match_excel_cases_to_db(df_cases, df_db, agent_map)
    df_matches['Counted Revenue'] = df_matches['DB SalesPrice'].apply(
        lambda p: calc_counted_revenue(p, rules['full_rate_cap'], rules['above_cap_pct']))
    agent_to_team = {agent_key(a): t for a, t in zip(df_left['Agent'], df_left['Team'])}
    # Agents can appear in the cases column without being on the team roster
    # (e.g. Zul/Joshua on the July sheet). They must not crash the report:
    # they are shown with team "-" and excluded from the team championship.
    def team_of(a):
        return agent_to_team.get(agent_key(a))
    def team_label(a):
        t = team_of(a)
        return f"{t} Team" if t else "-"
    df_matches['Team'] = df_matches['Excel Agent'].map(lambda a: team_of(a) or "-")
    team_rules = rules['teams']
    agent_totals = []
    for agent, grp in df_matches.groupby('Excel Agent'):
        team = team_of(agent) or "-"
        agent_totals.append({'Agent': agent, 'Team': team, 'Cases': len(grp), 'Sales Sum': grp['DB SalesPrice'].sum(), 'System Sum': grp['DB PkgPrice'].sum()})
    df_agent_totals = pd.DataFrame(agent_totals)
    team_stats = []
    for team, grp in df_matches.groupby('Team'):
        if team == "-":
            continue
        actual_sales = grp['DB SalesPrice'].sum()
        actual_counted = grp['Counted Revenue'].sum()
        team_rule = team_rules.get(team, {})
        handicap = team_rule.get('handicap', 0.0)
        # The baseline the team must reach is its own target plus its handicap:
        # the handicap exists to lift every team to a common baseline.
        baseline = team_rule.get('original_target', 0.0) + handicap
        registered_members = df_left[df_left['Team'] == team]['Agent'].unique()
        completed_members = grp['Excel Agent'].unique()
        all_active = len(completed_members) == len(registered_members)
        activity_bonus = rules['activity_bonus_points'] if (all_active and rules['activity_bonus_enabled']) else 0
        achieved = (actual_counted + handicap + activity_bonus) >= baseline
        target_bonus = rules['target_bonus_points'] if achieved else 0
        team_stats.append({'Team': team, 'Actual Sales': actual_sales, 'Actual Counted': actual_counted,
                           'Handicap': handicap, 'Original Target': team_rule.get('original_target', 0.0),
                           'Ranking Baseline': baseline,
                           'Activity Bonus': activity_bonus, 'Target Bonus': target_bonus,
                           'Final Score': (actual_counted + handicap + activity_bonus) + target_bonus,
                           'Achieved Target': achieved, 'All Active': all_active})
    df_team_stats = pd.DataFrame(team_stats).sort_values(by='Final Score', ascending=False).reset_index(drop=True)
    df_team_stats['Rank'] = df_team_stats.index + 1
    rank_awards = rules['rank_awards']
    team_award_val_map = {t: rank_awards.get(r, 0) for t, r in dict(zip(df_team_stats['Team'], df_team_stats['Rank'])).items()}
    team_ach_bonus_map = {row['Team']: (rules['achievement_bonus'] if (row['Achieved Target'] and row['All Active']) else 0)
                          for _, row in df_team_stats.iterrows()}
    df_agent_totals['Golden Boot Eligible'] = ((df_agent_totals['Cases'] >= rules['gb_min_cases'])
                                               & (df_agent_totals['Sales Sum'] >= rules['gb_min_sales']))
    gb_awards = {i - 1: amount for i, amount in rules['gb_awards'].items()}
    df_gb_eligible = df_agent_totals[df_agent_totals['Golden Boot Eligible'] == True].sort_values(by='Sales Sum', ascending=False).reset_index(drop=True)
    gb_award_map = {agent: gb_awards.get(df_gb_eligible[df_gb_eligible['Agent'] == agent].index[0], 0) if agent in df_gb_eligible['Agent'].values else 0 for agent in df_agent_totals['Agent']}
    fast_start_map = calc_fast_start(df_matches, df_agent_totals, rules)
    captains = {f"{name} Team": rule['captain'] for name, rule in team_rules.items()}
    roster_names = load_agent_display_names(year, month)
    def get_rank_str(r): return "🥇 1" if r==1 else ("🥈 2" if r==2 else ("🥉 3" if r==3 else str(r)))
    tbc_agents = {agent_key(a) for a in df_matches.loc[df_matches['DB Agent'] == TBC_PLACEHOLDER, 'Excel Agent']}

    def display_name(name):
        """Sentence-case a display name: 'ELYN' -> 'Elyn', 'GOH HOCK GIN' ->
        'Goh Hock Gin'. Keeps 1-2 letter initialisms (ZH, LK, JS) as typed and
        uppercases A/L / A/P in Malaysian customer names. Display only — all
        lookups keep using the raw names."""
        s = str(name or "").strip()
        if not s or s == TBC_PLACEHOLDER:
            return s
        out = []
        for w in s.split():
            if w.isupper() and len(w) <= 2:
                out.append(w)
            elif "/" in w:
                out.append("/".join(p.upper() for p in w.split("/")))
            else:
                # Capitalize the first LETTER, even after a leading bracket
                # or quote: '(atap)' -> '(Atap)', '(shop' -> '(Shop'.
                lowered = w.lower()
                for i, ch in enumerate(lowered):
                    if ch.isalpha():
                        lowered = lowered[:i] + ch.upper() + lowered[i + 1:]
                        break
                out.append(lowered)
        return " ".join(out)

    def label_agent(a):
        """Name to print: 'Full Name' from the Data page roster, falling back to
        the eeAdmin agent name, and finally to the contest nickname itself."""
        roster_name = roster_names.get(agent_key(a))
        shown = display_name(roster_name) if roster_name else display_name(a)
        return f"{shown} ({TBC_PLACEHOLDER})" if agent_key(a) in tbc_agents else shown

    def is_captain(a):
        """Captains are typed on the Data page ('Wing On') while the contest
        sheet uses the nickname ('Wingon'), so compare on the normalized key."""
        team = team_of(a)
        if not team:
            return False
        captain = captains.get(f"{team} Team")
        return bool(captain) and agent_key(captain) == agent_key(a)

    def label_agent_marked(a):
        """Same name, with the trailing '*' the report uses to flag a captain.
        The dashboard turns the marked row gold and hides the asterisk itself."""
        return label_agent(a) + (" *" if is_captain(a) else "")

    # Table 1 is per team, not per agent: rank, ranking award and achievement
    # bonus are all team-level, so an agent breakdown repeated the same award on
    # every member row and made the column total meaningless.
    t1_rows = []
    for _, t_row in df_team_stats.iterrows():
        members = df_agent_totals[df_agent_totals['Team'] == t_row['Team']]
        t1_rows.append({
            'Team Name': display_name(t_row['Team']) + " Team",
            'Accumulated System Price (RM)': members['System Sum'].sum(),
            'Accumulated Sales Price (RM)': members['Sales Sum'].sum(),
            'Rank': get_rank_str(t_row['Rank']),
            'Team Ranking Award (RM)': team_award_val_map[t_row['Team']],
            'Team Achievement Bonus (RM)': team_ach_bonus_map[t_row['Team']],
        })

    # Golden Boot placing, so table 3 can show a medal next to the award. Only
    # the paid places get one.
    gb_rank_by_agent = {row['Agent']: i + 1 for i, row in df_gb_eligible.iterrows()
                        if gb_award_map.get(row['Agent'], 0)}
    gb_medals = {1: "🥇 1st", 2: "🥈 2nd", 3: "🥉 3rd"}

    def gb_award_label(agent):
        """Winners read '🥇 1st 3,000' so the placing is shown the same way
        the Rank column shows it on the Team Championship table. Everyone else
        keeps the plain amount."""
        amount = gb_award_map[agent]
        medal = gb_medals.get(gb_rank_by_agent.get(agent))
        return f"{medal} {amount:,.0f}" if medal else amount

    t2_rows = [{'Agent Name': label_agent(a), 'Team Name': display_name(team_label(a)), 'System Price': df_agent_totals.loc[df_agent_totals['Agent']==a, 'System Sum'].values[0], 'Sales Price': df_agent_totals.loc[df_agent_totals['Agent']==a, 'Sales Sum'].values[0], 'Award': gb_award_map[a]} for a in df_agent_totals.sort_values(by='Sales Sum', ascending=False)['Agent']]
    t3_rows = [{'Agent Name': label_agent_marked(row['Excel Agent']), 'Team Name': display_name(team_label(row['Excel Agent'])), 'Customer Name': display_name(row['DB Cust']), 'Invoice Date': pd.to_datetime(row['DB Inv Date']).strftime('%Y-%m-%d') if pd.notnull(row['DB Inv Date']) else "", '1st Payment Date': pd.to_datetime(row['DB 1stPay']).strftime('%Y-%m-%d') if pd.notnull(row['DB 1stPay']) else "Special Case", 'System Price (RM)': row['DB PkgPrice'], 'Sales Price (RM)': row['DB SalesPrice'], 'Golden Boot Award (Top Individual Performance) (RM)': gb_award_label(row['Excel Agent']), 'Fast Start Award': fast_start_map[row['Excel Agent']]} for _, row in df_matches.iterrows()]
    return pd.DataFrame(t1_rows), pd.DataFrame(t2_rows), pd.DataFrame(t3_rows), df_team_stats, df_gb_eligible, fast_start_map, team_award_val_map, team_ach_bonus_map, gb_award_map, df_matches, agent_to_team

def main():
    today = datetime.date.today()
    month = int(sys.argv[1]) if len(sys.argv) > 1 else today.month
    period = f"{calendar.month_name[month]} {today.year}"
    print("====================================================")
    print(f"Starting Monthly Contest Awards Calculation ({period})")
    print("====================================================")
    token = load_auth_token()
    (df_table1, df_table2, df_table3, df_team_stats, df_gb_eligible,
     fast_start_map, team_award_val_map, team_ach_bonus_map, gb_award_map, df_matches, agent_to_team) = calculate_monthly_contest(token, month=month, year=today.year)

    print("\n[5/5] Generating formatted Excel workbook...")
    output_dir = "2. Output"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"Monthly Contest Awards {period}.xlsx")
    
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    
    font_family = "Segoe UI"
    thin_border = Border(
        left=Side(style='thin', color='E0E0E0'),
        right=Side(style='thin', color='E0E0E0'),
        top=Side(style='thin', color='E0E0E0'),
        bottom=Side(style='thin', color='E0E0E0')
    )
    double_bottom_border = Border(
        top=Side(style='thin', color='000000'),
        bottom=Side(style='double', color='000000')
    )
    
    # ================= Sheet 1: Table 1 =================
    ws1 = wb.create_sheet("Table 1 - Team Championship")
    for r in dataframe_to_rows(df_table1, index=False, header=True):
        ws1.append(r)
        
    style_excel_sheet(ws1, "Table 1: Team Championship and Team Achievement Bonus", len(df_table1.columns))
    
    # Columns: 1 Team Name | 2 System | 3 Sales | 4 Rank | 5 Ranking Award | 6 Achievement Bonus
    for row in range(4, ws1.max_row + 1):
        ws1.cell(row=row, column=1).alignment = Alignment(horizontal="left")
        
        for col in [2, 3]:
            c = ws1.cell(row=row, column=col)
            c.number_format = '"RM " #,##0.00'
            c.alignment = Alignment(horizontal="right")
            
        ws1.cell(row=row, column=4).alignment = Alignment(horizontal="center")
        
        for col in [5, 6]:
            c = ws1.cell(row=row, column=col)
            c.number_format = '"RM " #,##0.00'
            c.alignment = Alignment(horizontal="right")
            
        for col in range(1, len(df_table1.columns) + 1):
            ws1.cell(row=row, column=col).border = thin_border
            ws1.cell(row=row, column=col).font = Font(name=font_family, size=11)
            
    tot_row = ws1.max_row + 1
    ws1.cell(row=tot_row, column=1, value="Total").font = Font(name=font_family, size=11, bold=True)
    for col in [2, 3, 5, 6]:
        col_letter = get_column_letter(col)
        c = ws1.cell(row=tot_row, column=col, value=f"=SUM({col_letter}4:{col_letter}{tot_row-1})")
        c.font = Font(name=font_family, size=11, bold=True)
        c.number_format = '"RM " #,##0.00'
        c.alignment = Alignment(horizontal="right")
        
    for col in range(1, len(df_table1.columns) + 1):
        ws1.cell(row=tot_row, column=col).border = double_bottom_border
        
    auto_fit_columns(ws1)
    
    # ================= Sheet 3: Table 3 =================
    ws3 = wb.create_sheet("Table 3 - Summary by Agent")
    for r in dataframe_to_rows(df_table3, index=False, header=True):
        ws3.append(r)
        
    style_excel_sheet(ws3, "Table 3: Summary of Cases and Awards by Agent", len(df_table3.columns))
    
    for row in range(4, ws3.max_row + 1):
        ws3.cell(row=row, column=1).alignment = Alignment(horizontal="left")
        ws3.cell(row=row, column=2).alignment = Alignment(horizontal="left")
        ws3.cell(row=row, column=3).alignment = Alignment(horizontal="left")
        ws3.cell(row=row, column=4).alignment = Alignment(horizontal="center")
        ws3.cell(row=row, column=5).alignment = Alignment(horizontal="center")
        
        # Columns: 1 Agent | 2 Team | 3 Customer | 4 Invoice Date |
        # 5 1st Payment | 6 System Price | 7 Sales Price | 8 Golden Boot | 9 Fast Start
        for col in [6, 7]:
            c = ws3.cell(row=row, column=col)
            c.number_format = '"RM " #,##0.00'
            c.alignment = Alignment(horizontal="right")
            
        # Golden Boot carries a medal and placing for the top three, so it is a
        # label rather than a number and is centred instead of currency-formatted.
        ws3.cell(row=row, column=8).alignment = Alignment(horizontal="center")
        ws3.cell(row=row, column=9).alignment = Alignment(horizontal="left")
        
        for col in range(1, len(df_table3.columns) + 1):
            ws3.cell(row=row, column=col).border = thin_border
            ws3.cell(row=row, column=col).font = Font(name=font_family, size=11)
            
    tot_row = ws3.max_row + 1
    ws3.cell(row=tot_row, column=1, value="Total").font = Font(name=font_family, size=11, bold=True)
    # Only System and Sales Price are summable now. Golden Boot is a medal
    # label, and it never made sense to total it anyway — the award repeats on
    # each of an agent's case rows, so the old total multiplied it by the case
    # count.
    for col in [6, 7]:
        col_letter = get_column_letter(col)
        c = ws3.cell(row=tot_row, column=col, value=f"=SUM({col_letter}4:{col_letter}{tot_row-1})")
        c.font = Font(name=font_family, size=11, bold=True)
        c.number_format = '"RM " #,##0.00'
        c.alignment = Alignment(horizontal="right")
        
    for col in range(1, len(df_table3.columns) + 1):
        ws3.cell(row=tot_row, column=col).border = double_bottom_border

    # Captains carry a trailing '*' on this sheet now that table 1 is per team.
    legend_row = tot_row + 2
    ws3.cell(row=legend_row, column=1, value="* denotes Team Captain").font = Font(
        name=font_family, size=10, italic=True)

    auto_fit_columns(ws3)
    
    # Save Workbook
    wb.save(output_path)
    print(f"\nExcel workbook successfully written to: {output_path}")
    
    # 10. Display Summary Report to console
    print("\n" + "="*50)
    print("                CONTEST AWARDS SUMMARY")
    print("="*50)
    
    print("\n1. Team Standings and Rewards:")
    for idx, row in df_team_stats.iterrows():
        team_name = row['Team'] + " Team"
        bonus_status = "Qualified (RM 2k/member)" if team_ach_bonus_map[row['Team']] > 0 else "Not Qualified"
        print(f"   Rank {row['Rank']}: {team_name:<15} | Score: {row['Final Score']:,.2f} | Team Ranking Reward: RM {team_award_val_map[row['Team']]:,} | Team Achievement Bonus: {bonus_status}")
        
    print("\n2. Golden Boot Winners:")
    winners = df_gb_eligible.head(3)
    for idx, row in winners.iterrows():
        agent = row['Agent']
        award = f"RM {gb_award_map[agent]:,}"
        print(f"   Rank {idx+1}: {agent:<15} | Cases: {row['Cases']} | Sales: RM {row['Sales Sum']:,.2f} | Award: {award}")
        
    print("\n3. Fast Start Watch Recipients (>= 2 deals):")
    fs_recipients = [agent for agent, txt in fast_start_map.items() if txt]
    print(f"   Total recipients: {len(fs_recipients)}")
    print(f"   Recipients: {', '.join(sorted(fs_recipients))}")
    print("="*50)

if __name__ == "__main__":
    main()
