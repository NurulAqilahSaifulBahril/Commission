import csv
import re
import sqlite3
from pathlib import Path
from decimal import Decimal

CACHE_PATH = Path(__file__).parent / "basic_commission_rates_cache.csv"

# System-entered rates/rules from the dashboard "Data" page take priority over
# the Google Sheet / cached CSV. They live in the dashboard's SQLite database.
DASHBOARD_DB_PATH = Path(__file__).resolve().parent.parent.parent / "8. Web Dashboard" / "dashboard.db"

# Global loaded rates cache
_RATES_DATA = None
_DB_RATES = None
_DB_RULES = None
_DB_UNIFIED = None
_MONTH_MAPPING = {
    1: "jan",
    2: "feb",
    3: "mac",
    4: "mac",
    5: "mac",
    6: "jun",
    7: "jul",
    8: "jul",
    9: "jul",
    10: "jul",
    11: "jul",
    12: "jul",
}

def load_rates_data():
    global _RATES_DATA
    if _RATES_DATA is not None:
        return _RATES_DATA
        
    # Google Sheet fetching retired (2026-07): rates are entered on the dashboard
    # Data page and stored in dashboard.db. The local cache CSV is kept only as a
    # legacy fallback for month/role combos with no system entry.
    csv_text = None
    if not csv_text and CACHE_PATH.exists():
        try:
            csv_text = CACHE_PATH.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[Rates System] Error reading cached file: {e}")
            
    _RATES_DATA = {}
    if not csv_text:
        print("[Rates System] Warning: No online rates or local cache found. Falling back to default rates.")
        return _RATES_DATA
        
    try:
        import io
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        if len(rows) < 3:
            return _RATES_DATA
            
        headers = [h.strip() for h in rows[2]]
        header_month_indices = {}
        for idx, h in enumerate(headers):
            hl = h.lower().strip()
            if hl in ("jan", "feb", "mac", "jun", "jul"):
                header_month_indices[hl] = idx

        agent_type = "Internal"
        for r in rows[3:]:
            if not r or not any(r):
                continue
            if len(r) > 0 and r[0].strip():
                agent_type = r[0].strip()
            hierarchy = r[1].strip() if len(r) > 1 else ""
            override_rule = r[2].strip() if len(r) > 2 else ""
            if not hierarchy:
                continue
            
            month_rates = {}
            for m_name, idx in header_month_indices.items():
                val = r[idx].strip() if idx < len(r) else ""
                if val:
                    try:
                        month_rates[m_name] = Decimal(val.replace("%", "").strip()) / Decimal("100")
                    except Exception:
                        pass
            
            key = (agent_type.lower(), hierarchy.lower())
            _RATES_DATA[key] = {
                "rates": month_rates,
                "override_rule": override_rule
            }
    except Exception as e:
        print(f"[Rates System] Error parsing rates CSV: {e}. Falling back to default rates.")
        
    return _RATES_DATA

def reset_cache():
    """Drop all module-level caches so the next lookup re-reads sheet + database."""
    global _RATES_DATA, _DB_RATES, _DB_RULES, _DB_UNIFIED, _DB_ROLES
    _RATES_DATA = None
    _DB_RATES = None
    _DB_RULES = None
    _DB_UNIFIED = None
    _DB_ROLES = None
    if _AGENT_NAMES_MOD:
        _AGENT_NAMES_MOD.reset_cache()


# ── Agent-name canonicalisation ──────────────────────────────────────────────
# The engines identify agents by their ERP nickname ("Wilson Tan") while the
# Data page's Agent Name picker stores the roles-page full name ("Wilson Tan
# Wei Sheng"). A raw string compare between the two matches nobody, so every
# agent-scoped rate row was silently invisible to the engine and the agent
# dropped to the hardcoded default rate. Both sides of every name comparison
# must go through agent_names.resolve(), which folds nickname, eeAdmin name
# and full name onto one canonical key.

_AGENT_NAMES_MOD = None


def _agent_names_module():
    global _AGENT_NAMES_MOD
    if _AGENT_NAMES_MOD is None:
        try:
            import sys as _sys
            root = str(Path(__file__).resolve().parent.parent.parent)
            if root not in _sys.path:
                _sys.path.insert(0, root)
            import agent_names as _agent_names
            _AGENT_NAMES_MOD = _agent_names
        except Exception as e:
            print(f"[Rates System] Warning: agent_names resolver unavailable ({e}); "
                  f"agent-scoped rows match on the literal name only.")
            _AGENT_NAMES_MOD = False
    return _AGENT_NAMES_MOD


# The role and rate lookups compare every table row's name on every call, so a
# single Excel export asked for the same few hundred keys about five million
# times -- most of its run time. Keys are remembered per name map: when
# agent_names reloads (reset_cache after a Data page save) its map is a new
# object and the memo starts over.
_CANON_MEMO: dict = {}
_CANON_MEMO_MAP = None


def _canon_agent_key(name) -> str:
    """Comparison key for an agent name, alias-folded via the roles page."""
    global _CANON_MEMO, _CANON_MEMO_MAP
    text = str(name or "").strip()
    if not text:
        return ""
    mod = _agent_names_module()
    if mod:
        try:
            name_map = mod._load_maps()[0]
            if name_map is not _CANON_MEMO_MAP:
                _CANON_MEMO, _CANON_MEMO_MAP = {}, name_map
            key = _CANON_MEMO.get(text)
            if key is None:
                key = _CANON_MEMO[text] = mod.normalize_key(mod.resolve(text))
            return key
        except Exception:
            pass
    return "".join(text.split()).casefold()


# One tier, several spellings. The July-2026 role names replaced "Senior",
# "Executive" and "OSA/OSA1", but the rate rows are still keyed under the old
# labels and rewriting them would reprice months that have already paid out —
# so the new names resolve onto the old keys instead. Without this an agent
# saved as "Sales Senior" matches no rate row at all and silently drops to the
# hardcoded default.
_HIERARCHY_ALIASES = {
    "osa": "osa/osa1",
    "osa 1": "osa/osa1",
    "osa1": "osa/osa1",
    "sales senior": "senior",
    "sales executive": "executive",
}


def _normalize_hierarchy(hierarchy: str) -> str:
    hierarchy = hierarchy.strip().lower()
    return _HIERARCHY_ALIASES.get(hierarchy, hierarchy)


# ── Effective-month ranges ───────────────────────────────────────────────────
# An effective_from cell is either a single month ("2026-07", meaning "from this
# month onwards") or a closed range ("2026-07 to 2026-08", meaning those months
# and no others). Both spellings are entered from the dashboard, on the rates
# grid and on the Agent Roles & Hierarchy grid alike.
#
# Every reader has to go through here. A range value breaks a plain string
# comparison in the worst possible direction — "2026-07 to 2026-08" > "2026-07"
# is True, so an `if eff > target: continue` guard drops the row for exactly the
# months it covers, silently and only for the agents who have a range.

def split_effective_range(eff: str) -> tuple[str, str]:
    """(start_month, end_month) for an effective_from cell. A single month has
    no end, represented as "9999-12" so the same comparison works for both."""
    eff = str(eff or "").strip()
    if not eff:
        return "", ""
    sep = " to " if " to " in eff else (".." if ".." in eff else None)
    if sep is None:
        return eff, "9999-12"
    parts = eff.split(sep)
    start = parts[0].strip()
    end = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "9999-12"
    return start, end


def effective_covers(eff: str, target: str) -> bool:
    """Does this effective_from cell govern `target` ("YYYY-MM")?"""
    start, end = split_effective_range(eff)
    if not start:
        return False
    return start <= target <= end


def effective_start(eff: str) -> str:
    """The month a row starts applying — what "the latest row wins" sorts on.
    Comparing the raw cell would rank "2026-07 to 2026-08" above a plain
    "2026-07" purely because it is a longer string."""
    return split_effective_range(eff)[0]


def effective_end(eff: str) -> str:
    """The last month a row applies, or "9999-12" when it is open-ended."""
    return split_effective_range(eff)[1]


def _load_db_table(table: str) -> list:
    """Read a config table from the dashboard database.

    The dashboard writes to Supabase/Postgres, so that is the source of truth.
    The legacy local SQLite file is only consulted if Postgres is unreachable —
    it stops being updated the moment the dashboard is pointed at Postgres, so
    silently preferring it serves stale rates with no error.
    """
    try:
        import sys as _sys
        dash = str(DASHBOARD_DB_PATH.parent)
        if dash not in _sys.path:
            _sys.path.insert(0, dash)
        import db as _dashboard_db
        with _dashboard_db._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Rates System] Warning: Postgres unavailable for {table} ({e}); "
              f"falling back to the legacy local database.")

    if not DASHBOARD_DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DASHBOARD_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Rates System] Warning: could not read {table} from dashboard DB: {e}")
        return []


# ── Unified rates & rules table (dashboard "commission_rates") ───────────────
# One row can hold a rate AND its payout condition, so this table is read first.
# While it is empty every lookup returns None and the legacy basic_rates /
# rule_settings path below stays in charge — nothing changes until rows exist.

def _unified_rows() -> list:
    global _DB_UNIFIED
    if _DB_UNIFIED is None:
        _DB_UNIFIED = _load_db_table("commission_rates")
    return _DB_UNIFIED


def _cell(row, key) -> str:
    return str(row.get(key, "") or "").strip()


# Every property type the Data page offers. A row that lists them all is not
# scoped to anything — it means "all types" — so it must behave exactly like a
# row that leaves the column blank. Without this, ticking every box makes a row
# stricter than ticking none, which is the opposite of what it reads like.
_ALL_PROPERTY_TYPES = {"residential", "commercial", "shop lot", "shoplot =< 36",
                       "factory", "ngo project", "government project"}


def normalize_prop_name(s: str) -> str:
    """Canonical lower-case property type name."""
    v = str(s or "").strip().lower()
    if v in ("shoplot =< 36", "shoplot <= 36", "shop lot =< 36", "shop lot <= 36"):
        return "shoplot =< 36"
    if v == "ngo":
        return "ngo project"
    if v in ("goverment", "government"):
        return "government project"
    return v


def _row_property_list(r) -> list:
    props = [normalize_prop_name(p) for p in _cell(r, "property_type").split(",") if p.strip()]
    primary_props = {"residential", "commercial", "shop lot", "factory", "ngo project", "government project"}
    if not props or primary_props.issubset(set(props)):
        return []
    return props


def get_invoice_panel_qty(row: dict) -> int:
    """Return the integer panel quantity for an invoice row, or 0 if unknown."""
    if not isinstance(row, dict):
        return 0
    raw_qty = row.get("panel_qty")
    if raw_qty is not None:
        try:
            qty = int(raw_qty)
            if qty > 0:
                return qty
        except (ValueError, TypeError):
            pass

    import re
    for field in ("package_name_snapshot", "description", "package_type"):
        text = str(row.get(field) or "").strip()
        if not text:
            continue
        m = re.search(r'\b(\d+)\s*[xX×]\s*\d{3,4}\s*[wW]?\b', text)
        if m:
            try:
                val = int(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
        m = re.search(r'\b(\d+)\s*[xX×]\s*(?:JINKO|LONGI|TRINA|JA\s+SOLAR|CANADIAN|PANELS?)\b', text, re.I)
        if m:
            try:
                val = int(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
        m = re.search(r'\b(?:panel\s*qty|qty|panels?)\s*[:=]?\s*(\d+)\b', text, re.I)
        if m:
            try:
                val = int(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
        m = re.search(r'\b(\d+)\s*(?:pcs|panels?)\b', text, re.I)
        if m:
            try:
                val = int(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                pass
    return 0


def resolve_invoice_property_type(row: dict, base_prop_type: str) -> tuple:
    """Return (display_property_type, rate_property_type) for an invoice row.

    If classified as 'Shop Lot' and the invoice has 36 or fewer panels:
    - display_property_type is 'Shoplot =< 36'
    - rate_property_type is 'Shoplot =< 36, Shop Lot' (matches Shoplot =< 36 first, falling back to Shop Lot)
    Otherwise:
    - display_property_type is base_prop_type (e.g. 'Shop Lot', 'Residential', 'Factory')
    - rate_property_type is base_prop_type
    """
    prop = base_prop_type or "Residential"
    if prop == "Shop Lot":
        panel_count = get_invoice_panel_qty(row)
        if 0 < panel_count <= 36:
            return "Shoplot =< 36", "Shoplot =< 36, Shop Lot"
    return prop, prop


# Job Type splits the business into the solar installs it was built on and
# the EV charger jobs added in July 2026. A rate row names the job it prices;
# a row that names none is a solar row, because every row written before the
# column existed was written for solar.
JOB_SOLAR = "Solar Services"
JOB_EV = "EV Services"
JOB_TYPES = (JOB_SOLAR, JOB_EV)


def normalize_job_type(value) -> str:
    """JOB_SOLAR or JOB_EV for anything that names one, else ''."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    if v.startswith("ev") or v == JOB_EV.lower():
        return JOB_EV
    if v.startswith("solar") or v == JOB_SOLAR.lower():
        return JOB_SOLAR
    return ""


def _row_job_type(r) -> str:
    return normalize_job_type(_cell(r, "job_type")) or JOB_SOLAR


# EV Type further splits an EV Services row by which EV service it prices --
# the charger unit, the installation labour, or both together. Blank means
# "any EV subtype": the general EV rate a subtype-specific row can override.
# Only meaningful when Job Type is EV Services; Solar rows ignore it.
EV_CHARGER = "EV Charger"
EV_INSTALLATION = "EV Installation"
EV_CHARGER_INSTALLATION = "EV Charger + EV Installation"
EV_TYPES = (EV_CHARGER, EV_INSTALLATION, EV_CHARGER_INSTALLATION)


def normalize_ev_type(value) -> str:
    """One of EV_TYPES for anything that names one, else ''."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    for t in EV_TYPES:
        if v == t.lower():
            return t
    return ""


def _row_ev_type(r) -> str:
    return normalize_ev_type(_cell(r, "ev_type"))


def _truthy_flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("t", "true", "1", "yes", "y")
    if value is None:
        return False
    return bool(value)


def classify_ev_invoice(row: dict):
    """(is_ev_only, is_mixed, ev_item_amount) for one invoice row.

    EV-only: the invoice is EV Services and has no JinkoSolar / Tiger Neo.
    Mixed: JinkoSolar / Tiger Neo is on the invoice and EV line items have a
    positive amount — solar Basic stays, EV commission is added on the same row.
    """
    try:
        ev_amt = Decimal(str(row.get("ev_item_amount") or 0))
    except Exception:
        ev_amt = Decimal("0")
    if ev_amt < 0:
        ev_amt = Decimal("0")
    has_jinko = _truthy_flag(row.get("has_jinko"))
    job = normalize_job_type(row.get("job_type"))
    is_ev_only = (job == JOB_EV) and not has_jinko
    is_mixed = has_jinko and ev_amt > 0
    return is_ev_only, is_mixed, ev_amt


# Shown in place of the rate when the Data page has none for the sale: the
# rate is then 0 and the commission RM 0 (get_basic_rate), and this says why.
MISSING_RATE_LABEL = "No rate on Data page"


def _rate_label(rate) -> str:
    if rate is not None and Decimal(str(rate)) == 0:
        return MISSING_RATE_LABEL
    return f"{float(rate) * 100:g}%"


def format_basic_rate_display(solar_rate, ev_rate=None, *, is_mixed=False) -> str:
    """Basic Rate % label. Mixed invoices show both pieces so 4.5% + 8% is
    not mistaken for one combined rate."""
    if is_mixed and solar_rate is not None and ev_rate is not None:
        return f"{_rate_label(solar_rate)} + {_rate_label(ev_rate)} EV"
    rate = ev_rate if solar_rate is None else solar_rate
    if rate is None:
        return ""
    return _rate_label(rate)


def basic_commission_for_row(row: dict, sales_price, rate_fn):
    """(commission, primary_rate, ev_rate, ev_commission, ev_item_amount, rate_display).

    ``rate_fn(job_type, ev_type)`` returns the Data page rate as a Decimal.
    EV-only uses the EV rate on sales. Mixed keeps today's solar Basic and
    adds EV line-item amount × EV rate. Solar-only is unchanged.
    """
    sales_price = Decimal(str(sales_price or 0))
    is_ev_only, is_mixed, ev_amt = classify_ev_invoice(row)
    ev_type = row.get("ev_type")
    if is_ev_only:
        rate = rate_fn(JOB_EV, ev_type)
        comm = sales_price * rate
        return comm, rate, rate, Decimal("0"), ev_amt, format_basic_rate_display(rate)
    if is_mixed:
        solar_rate = rate_fn(JOB_SOLAR, None)
        ev_rate = rate_fn(JOB_EV, ev_type)
        ev_comm = ev_amt * ev_rate
        comm = (sales_price * solar_rate) + ev_comm
        return (comm, solar_rate, ev_rate, ev_comm, ev_amt,
                format_basic_rate_display(solar_rate, ev_rate, is_mixed=True))
    rate = rate_fn(row.get("job_type"), ev_type)
    comm = sales_price * rate
    return comm, rate, None, Decimal("0"), ev_amt, format_basic_rate_display(rate)


def _row_agent_list(r) -> list:
    """A row's Agent Name cell, as a list. The Data page's Agent Name picker is
    multi-select and stores one row per rate holding every agent it applies to
    ("Chan Jia Wei, Carol Siow"), so a plain string compare would match none of
    them. Blank means the row is role-level and applies to everybody."""
    return [a.strip() for a in _cell(r, "agent").lower().split(",") if a.strip()]


def _is_rule_row(r) -> bool:
    """A row carrying no rate of any kind is a standalone rule (the global payout
    / advance mechanics). A row with a rate is a rate row, even when it also
    carries its own payment condition (Safwan, Gan Lai Soon, Referral Fee)."""
    return not (_cell(r, "rate_pct") or _cell(r, "override_rate_pct")
                or _cell(r, "profit_sharing_rate_pct"))


def get_unified_basic_rate(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                           agent: str = None, property_type: str = None,
                           job_type: str = None, ev_type: str = None):
    """Effective-dated rate from the unified table, or None when no row covers
    the case."""
    hit = get_unified_basic_rate_row(agent_type, hierarchy, month, year=year,
                                     agent=agent, property_type=property_type,
                                     job_type=job_type, ev_type=ev_type)
    return hit[0] if hit else None


def get_unified_basic_rate_row(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                               agent: str = None, property_type: str = None,
                               job_type: str = None, ev_type: str = None):
    """(rate, effective_from) of the winning unified row, or None. Blank
    agent_type/role/property on a row means "applies to all"; a row naming a
    specific agent beats a role-level row."""
    best = _best_unified_row(agent_type, hierarchy, month, year=year, agent=agent,
                             property_type=property_type, job_type=job_type,
                             ev_type=ev_type)
    return (best[1], best[2]) if best else None


def _best_unified_row(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                      agent: str = None, property_type: str = None,
                      job_type: str = None, ev_type: str = None):
    """(row, rate, effective_from) for the unified row that governs this case.

    The rate and the payout condition live on the SAME row, so both must be read
    off the one winner — resolving them independently would let an invoice take
    its rate from one row and its payout trigger from another.

    Only rows for the invoice's Job Type are considered. An invoice whose job
    is unknown is priced as solar, as every invoice was before the column
    existed. An EV invoice falls back to the solar rows when no EV row covers
    it, so EV jobs keep the rate they had until an EV rate is actually entered,
    rather than dropping to the sheet default the moment the column appears.

    An EV invoice further scoped to an EV Type (charger / installation / both)
    first looks for a row scoped to that exact subtype, then falls back to a
    row with no EV Type at all (a general EV rate), before falling back to
    solar — so entering one subtype's rate does not zero out the others.
    """
    want_job = normalize_job_type(job_type) or JOB_SOLAR
    want_ev = normalize_ev_type(ev_type) if want_job == JOB_EV else ""
    best = _scan_unified_rows(agent_type, hierarchy, month, year, agent,
                              property_type, want_job, want_ev)
    if best is None and want_ev:
        best = _scan_unified_rows(agent_type, hierarchy, month, year, agent,
                                  property_type, want_job, "")
    if best is None and want_job != JOB_SOLAR:
        best = _scan_unified_rows(agent_type, hierarchy, month, year, agent,
                                  property_type, JOB_SOLAR, "")
    return best


def _scan_unified_rows(agent_type, hierarchy, month, year, agent, property_type,
                       job_type, ev_type=""):
    """_best_unified_row() for one Job Type (and, for EV Services, one EV Type)."""
    target = f"{year:04d}-{month:02d}"
    agent_key = _canon_agent_key(agent)
    prop_key = str(property_type or "").strip().lower()
    agent_type = str(agent_type or "").strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    # The most specific row wins, and only then the latest effective_from:
    # naming the agent (2) outranks naming the property (1), which outranks a
    # row that names neither. Without this the winner would depend on row order.
    best = None  # (specificity, effective_from, rate)
    for r in _unified_rows():
        if (_cell(r, "rate_type") or "Basic Commission") != "Basic Commission":
            continue
        if _is_rule_row(r):
            continue
        if _row_job_type(r) != job_type:
            continue
        if job_type == JOB_EV and _row_ev_type(r) != ev_type:
            continue
        row_atype = _cell(r, "agent_type").lower()
        if row_atype and row_atype != agent_type:
            continue
        row_role = _cell(r, "hierarchy")
        if row_role and _normalize_hierarchy(row_role) != hierarchy:
            continue
        # property_type is a comma-separated list ("Residential, Shop Lot");
        # blank (or every type ticked) means the row applies to all of them.
        # When the caller passes no property at all it cannot honour scoping,
        # so a scoped row is skipped rather than applied blindly — without
        # this, a tie between e.g. a Factory row and a Residential row at the
        # same effective_from is resolved by silent row order, not by rule.
        row_props = _row_property_list(r)
        prop_score = 0
        if row_props:
            if not prop_key:
                continue
            caller_props = [normalize_prop_name(p) for p in prop_key.split(",") if p.strip()]
            matches = [len(caller_props) - i for i, cp in enumerate(caller_props) if cp in row_props]
            if not matches:
                continue
            prop_score = max(matches)
        eff = _cell(r, "effective_from")
        if not effective_covers(eff, target):
            continue
        row_agents = _row_agent_list(r)
        if row_agents and agent_key not in {_canon_agent_key(a) for a in row_agents}:
            continue
        try:
            rate = Decimal(_cell(r, "rate_pct").replace("%", "")) / Decimal("100")
        except Exception:
            continue
        spec = (10 if row_agents else 0) + prop_score
        # Rank on the start month — the raw cell would sort a range above the
        # plain month it starts in, purely on string length — but hand back the
        # cell as written, because callers display it and "2026-07 to 2026-08"
        # says something "2026-07" does not.
        cand = (spec, effective_start(eff), r, rate, eff)
        if best is None or (cand[0], cand[1]) > (best[0], best[1]):
            best = cand
    return (best[2], best[3], best[4]) if best else None


# ── Net Floor Price tier rates (dashboard "commission_rates") ────────────────
# The NFP engine hardcoded 25% / 100% / 20%. Those three numbers are now entered
# on the Data page's Net Floor Price section, one row per tier, with the tier
# itself written in the row's `condition` cell (its payout stages go in `label`,
# because `condition` is spoken for). Nothing is entered by default, so while the
# table holds no NFP rows every lookup returns the engine's original constants
# and not a single payout moves.

NFP_TIER_SALES_ABOVE = "sales_above"
NFP_TIER_SYSTEM_ABOVE = "system_above"
NFP_TIER_SALES_BELOW = "sales_below"

NFP_DEFAULT_TIER_RATES = {
    NFP_TIER_SALES_ABOVE: Decimal("0.25"),
    NFP_TIER_SYSTEM_ABOVE: Decimal("1.00"),
    NFP_TIER_SALES_BELOW: Decimal("0.20"),
}


def nfp_tier_key(condition: str):
    """Which tier a stored condition names, or None.

    Read off the comparison itself rather than the wording around it. These
    labels have already carried "i." / "ii." / "iii." numerals and a trailing
    explanation ("— pays (Sales − NFP) × rate"), and rows saved under either
    spelling have to keep resolving to the same tier.
    """
    text = re.sub(r"^\s*(iii|ii|i)\.\s*", "", str(condition or "").lower())
    head = re.split(r"[—(]", text)[0]
    # "system price > net floor price" also contains ">", so the system tier has
    # to be recognised before the sales-above one.
    if "system" in head:
        return NFP_TIER_SYSTEM_ABOVE
    if "<" in head or "below" in head:
        return NFP_TIER_SALES_BELOW
    if ">" in head or "above" in head:
        return NFP_TIER_SALES_ABOVE
    return None


def get_nfp_tier_rates(agent_type: str, month: int, year: int = 2026,
                       agent: str = None, hierarchy: str = None) -> dict:
    """The three NFP tier rates for this case, as fractions of 1.

    Same precedence as every other Data page rate: a row naming the agent beats
    a role-level row, and among equals the latest effective month wins. Each
    tier resolves on its own, so entering one does not disturb the other two —
    a tier nobody has entered keeps the engine's built-in rate.
    """
    target = f"{year:04d}-{month:02d}"
    atype = str(agent_type or "").strip().lower()
    role = _normalize_hierarchy(hierarchy) if hierarchy else ""
    agent_key = _canon_agent_key(agent)

    best = {}   # tier -> (specificity, effective_start, rate)
    for r in _unified_rows():
        if _cell(r, "rate_type") != "Net Floor Price Rate":
            continue
        if _cell(r, "remarks").lower() == "deleted":
            continue
        tier = nfp_tier_key(_cell(r, "condition"))
        if tier is None:
            continue
        row_atype = _cell(r, "agent_type").lower()
        if row_atype and row_atype != atype:
            continue
        row_role = _cell(r, "hierarchy")
        if row_role and _normalize_hierarchy(row_role) != role:
            continue
        eff = _cell(r, "effective_from")
        if not effective_covers(eff, target):
            continue
        row_agents = _row_agent_list(r)
        if row_agents and agent_key not in {_canon_agent_key(a) for a in row_agents}:
            continue
        try:
            rate = Decimal(_cell(r, "rate_pct").replace("%", "")) / Decimal("100")
        except Exception:
            continue
        cand = (2 if row_agents else 0, effective_start(eff), rate)
        prev = best.get(tier)
        if prev is None or (cand[0], cand[1]) > (prev[0], prev[1]):
            best[tier] = cand

    rates = dict(NFP_DEFAULT_TIER_RATES)
    for tier, cand in best.items():
        rates[tier] = cand[2]
    return rates


def _best_nfp_rows(agent_type: str, month: int, year: int = 2026,
                   agent: str = None, hierarchy: str = None) -> list:
    """The NFP rows governing this case, one per tier."""
    target = f"{year:04d}-{month:02d}"
    atype = str(agent_type or "").strip().lower()
    role = _normalize_hierarchy(hierarchy) if hierarchy else ""
    agent_key = _canon_agent_key(agent)

    best = {}
    for r in _unified_rows():
        if _cell(r, "rate_type") != "Net Floor Price Rate":
            continue
        if _cell(r, "remarks").lower() == "deleted":
            continue
        tier = nfp_tier_key(_cell(r, "condition"))
        if tier is None:
            continue
        row_atype = _cell(r, "agent_type").lower()
        if row_atype and row_atype != atype:
            continue
        row_role = _cell(r, "hierarchy")
        if row_role and _normalize_hierarchy(row_role) != role:
            continue
        eff = _cell(r, "effective_from")
        if not effective_covers(eff, target):
            continue
        row_agents = _row_agent_list(r)
        if row_agents and agent_key not in {_canon_agent_key(a) for a in row_agents}:
            continue
        cand = (2 if row_agents else 0, effective_start(eff), r)
        prev = best.get(tier)
        if prev is None or (cand[0], cand[1]) > (prev[0], prev[1]):
            best[tier] = cand
    return [c[2] for c in best.values()]


NFP_DEFAULT_PAYOUT_TRIGGER = Decimal("100")


def get_nfp_payout_trigger(agent_type: str, month: int, year: int = 2026,
                           agent: str = None, hierarchy: str = None) -> Decimal:
    """The payment percentage at which NFP commission is recognised.

    NFP pays as ONE figure per invoice (tier a minus tier c), so it recognises
    at one moment, not once per tier. When the governing tiers disagree the
    latest of their triggers wins — money is recognised when every condition
    attached to it has been met, never before.

    Advance stages are ignored: an advance is a part-payment of a commission,
    and the NFP report carries no such split to pay one against.

    Falls back to 100% -- what this report has always done -- when no governing
    row states a trigger.
    """
    triggers = []
    for r in _best_nfp_rows(agent_type, month, year=year, agent=agent,
                            hierarchy=hierarchy):
        pol = policy_from_row(r)
        if pol and pol.balance_trigger is not None:
            triggers.append(pol.balance_trigger)
    return max(triggers) if triggers else NFP_DEFAULT_PAYOUT_TRIGGER


def nfp_payout_thresholds_in_use() -> list:
    """Every payment percentage an NFP row asks for, so the NFP query knows
    which milestone dates it has to compute. 100 is always in the set: it is
    what an unconfigured tier falls back to."""
    out = {NFP_DEFAULT_PAYOUT_TRIGGER}
    for r in _unified_rows():
        if _cell(r, "rate_type") != "Net Floor Price Rate":
            continue
        if _cell(r, "remarks").lower() == "deleted":
            continue
        pol = policy_from_row(r)
        if pol and pol.balance_trigger is not None:
            out.add(pol.balance_trigger)
    return sorted(out)


# ── Payout policy (when a commission is recognised) ──────────────────────────
# The rate row carries its own payout condition, so "how much" and "when it is
# paid" always come from the same Data page row:
#
#   rule_type "Payout"      trigger_pct "100"        -> one stage: the whole
#                                                       commission lands in the
#                                                       month payment reaches
#                                                       100% of the invoice.
#   rule_type "Multi-stage" trigger_pct "4.99, 75"   -> two stages: an advance
#                           amount_rm  "300"            of RM300 in the month
#                                                       payment reaches 4.99%,
#                                                       the balance in the month
#                                                       it reaches 75%.
#
# The trigger list is read by VALUE, not by position: the smaller percentage is
# the advance and the larger is the balance. The Data page has both orderings in
# it ("4.99, 75" and "100, 4.99"), and an advance that triggers after the
# balance is not a thing, so position cannot be the signal.

_POLICY_SINGLE = "single"
_POLICY_MULTI = "multi"


class PayoutPolicy:
    """When a basic commission is recognised, as entered on the Data page."""

    __slots__ = ("mode", "advance_trigger", "advance_amount", "balance_trigger",
                 "effective_from")

    def __init__(self, mode, balance_trigger, advance_trigger=None,
                 advance_amount=None, effective_from=""):
        self.mode = mode
        self.balance_trigger = balance_trigger
        self.advance_trigger = advance_trigger
        self.advance_amount = advance_amount
        self.effective_from = effective_from

    @property
    def is_multi_stage(self) -> bool:
        return self.mode == _POLICY_MULTI

    def thresholds(self) -> list:
        out = [self.balance_trigger]
        if self.advance_trigger is not None:
            out.append(self.advance_trigger)
        return sorted(set(t for t in out if t is not None))

    def __repr__(self):
        if self.is_multi_stage:
            return (f"PayoutPolicy(multi, advance {self.advance_trigger}% = RM"
                    f"{self.advance_amount}, balance {self.balance_trigger}%)")
        return f"PayoutPolicy(single, {self.balance_trigger}%)"


def _parse_pct_list(text: str) -> list:
    out = []
    for part in str(text or "").replace("%", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(Decimal(part))
        except Exception:
            pass
    return out


def policy_from_row(row) -> "PayoutPolicy | None":
    """The payout condition carried on one unified row, or None when it says
    nothing — then the caller keeps whatever default it already applied."""
    if row is None:
        return None
    rule_type = _cell(row, "rule_type").strip().lower()
    triggers = _parse_pct_list(_cell(row, "trigger_pct"))
    eff = _cell(row, "effective_from")
    if not triggers:
        return None
    if rule_type == "multi-stage" and len(triggers) >= 2:
        try:
            amount = Decimal(_cell(row, "amount_rm").replace("RM", "").replace(",", ""))
        except Exception:
            return None
        return PayoutPolicy(_POLICY_MULTI, balance_trigger=max(triggers),
                            advance_trigger=min(triggers), advance_amount=amount,
                            effective_from=eff)
    if rule_type in ("payout", "multi-stage"):
        return PayoutPolicy(_POLICY_SINGLE, balance_trigger=max(triggers),
                            effective_from=eff)
    return None


def get_payout_policy(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                      agent: str = None, property_type: str = None,
                      job_type: str = None, ev_type: str = None):
    """The payout condition governing this agent/role/property in this month, or
    None when the Data page has nothing to say about it.

    An EV invoice reads the payment rule of its own EV row. Before 2026-10-20
    the EV Type never reached this lookup, so an EV invoice found no EV row
    (every agent's EV rows name a type -- Charger / Installation / both) and
    fell back to the Solar row, taking its "Advance RM 300" although the EV
    rows say "Pays at >= 100%". An EV invoice with no EV Type recorded takes any
    of the agent's EV rows before the Solar one."""
    agent_type = str(agent_type or "").strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    best = _best_unified_row(agent_type, hierarchy, month, year=year, agent=agent,
                             property_type=property_type, job_type=job_type,
                             ev_type=ev_type)
    if (normalize_job_type(job_type) == JOB_EV and not normalize_ev_type(ev_type)
            and (best is None or _row_job_type(best[0]) != JOB_EV)):
        for t in EV_TYPES:
            hit = _scan_unified_rows(agent_type, hierarchy, month, year, agent,
                                     property_type, JOB_EV, t)
            if hit:
                best = hit
                break
    if not best:
        return None
    return policy_from_row(best[0])


def payout_thresholds_in_use() -> list:
    """Every payment-milestone percentage any Data page row asks for.

    The milestone dates are computed in SQL, one pass per threshold, so the query
    has to know the whole set up front. 100 is always included: it is what an
    unconfigured row falls back to.
    """
    out = {Decimal("100")}
    for r in _unified_rows():
        pol = policy_from_row(r)
        if pol:
            out.update(pol.thresholds())
    return sorted(out)


# ── Milestone-date SQL ───────────────────────────────────────────────────────
# "The month payment reached N% of the invoice" is computed in Postgres, one
# pass per threshold. The thresholds used to be literals in the query (0.05 /
# 0.75 / 1.0), which is why editing a trigger on the Data page changed nothing:
# the dates the report buckets on were decided before any rule was read. The
# query now asks for whatever set of thresholds the Data page actually uses.

# Payments that must not count toward any milestone (reversals / duplicates).
MILESTONE_PAYMENT_EXCLUSIONS = "101334, 104412, 101333, 104413, 4899"


def _threshold_slug(t: Decimal) -> str:
    return format(Decimal(t).normalize(), "f").replace(".", "_").replace("-", "neg")


def milestone_column(t) -> str:
    """The result column holding the date this threshold was reached."""
    return f"pct_{_threshold_slug(t)}_date"


def milestone_created_column(t) -> str:
    """The result column holding the Created at date (Malaysia time) of the
    payment that reached this threshold -- when Finance entered it, which can
    be days after the payment date typed on it."""
    return f"pct_{_threshold_slug(t)}_created"


def milestone_sql_parts(thresholds=None, exclusions: str = None):
    """(ctes, selects, joins) SQL fragments for the milestone dates.

    Each threshold gets one CTE finding the earliest payment date at which the
    running total reached that share of the invoice. 100% compares against the
    total directly rather than multiplying by 1.0, so an invoice paid to the cent
    is not pushed below the line by binary rounding.

    That 100% comparison also allows one cent of slack: total_amount is a
    numeric column carrying float-conversion noise on some rows (e.g.
    25593.440000000002), which made a handful of invoices paid to the cent
    fail a strict >= by a fraction of a sen and sit unrecognised forever.
    """
    thresholds = list(thresholds or payout_thresholds_in_use())
    if Decimal("100") not in thresholds:
        thresholds.append(Decimal("100"))
    excl = exclusions or MILESTONE_PAYMENT_EXCLUSIONS
    ctes, selects, joins = [], [], []
    for t in sorted(set(Decimal(x) for x in thresholds)):
        name = f"pct_{_threshold_slug(t)}"
        col = milestone_column(t)
        ccol = milestone_created_column(t)
        if t == Decimal("100"):
            cmp_sql = "sub.running_total >= sub.total_amount - 0.01"
        else:
            frac = (Decimal(t) / Decimal("100")).normalize()
            cmp_sql = f"sub.running_total >= sub.total_amount * {format(frac, 'f')}"
        ctes.append(f"""-- earliest date each invoice reached {t}% of total_amount paid
{name} AS (
  SELECT sub.linked_invoice,
         MIN(sub.payment_date) AS {col},
         -- the same crossing payment's Created at, in Malaysia time
         (ARRAY_AGG((sub.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date
                    ORDER BY sub.payment_date, sub.id))[1] AS {ccol}
  FROM (
    SELECT p.linked_invoice,
           p.id,
           p.payment_date,
           p.created_at,
           SUM(p.amount) OVER (
             PARTITION BY p.linked_invoice
             ORDER BY p.payment_date ASC, p.id ASC
           ) AS running_total,
           i.total_amount
    FROM payment p
    JOIN target_invoices i ON i.bubble_id = p.linked_invoice
    WHERE p.id NOT IN ({excl})
  ) sub
  WHERE {cmp_sql}
  GROUP BY sub.linked_invoice
)""")
        selects.append(f"    {name}.{col}, {name}.{ccol},")
        joins.append(f"  LEFT JOIN {name} ON {name}.linked_invoice = i.bubble_id")
    return ",\n".join(ctes), "\n".join(selects), "\n".join(joins)


def multi_stage_cutover(year: int = 2026):
    """(month, policy) for the first month of `year` governed by a multi-stage
    payout, or None when the whole year is single-stage.

    This is the "new mechanism starts here" boundary that used to be written as
    `>= (2026, 7)` in the engines and the pack builder. It is the effective_from
    of the multi-stage rows, so moving the policy on the Data page moves it
    everywhere instead of only in the rate column.
    """
    best = None
    for r in _unified_rows():
        pol = policy_from_row(r)
        if not pol or not pol.is_multi_stage:
            continue
        start = effective_start(_cell(r, "effective_from"))
        if not start.startswith(f"{int(year):04d}-"):
            continue
        try:
            m = int(start.split("-")[1])
        except (IndexError, ValueError):
            continue
        if best is None or m < best[0]:
            best = (m, pol)
    return best


def milestone_date(row: dict, threshold) -> str:
    """The milestone date for one threshold off a raw invoice row."""
    if threshold is None:
        return ""
    return str(row.get(milestone_column(threshold)) or "")[:10]


def milestone_created(row: dict, threshold) -> str:
    """Created at date of the payment that reached one threshold, or ""."""
    if threshold is None:
        return ""
    return str(row.get(milestone_created_column(threshold)) or "")[:10]


def invoice_milestone_created(row: dict, policy) -> tuple:
    """(advance_created, balance_created) for the stages `policy` names."""
    if policy is None:
        return "", ""
    advance = milestone_created(row, policy.advance_trigger) if policy.is_multi_stage else ""
    return advance, milestone_created(row, policy.balance_trigger)


def default_payout_policy(month: int, year: int = 2026) -> "PayoutPolicy":
    """What governs a month the Data page says nothing about.

    From the cutover month onwards this is that month's multi-stage rule;
    before it, full payment. This is the behaviour the engines hardcoded, so a
    role with no row keeps being reported exactly as it is today rather than
    silently switching policy the moment this became configurable.
    """
    cutover = multi_stage_cutover(year)
    if cutover and int(month) >= cutover[0]:
        return cutover[1]
    return PayoutPolicy(_POLICY_SINGLE, balance_trigger=Decimal("100"))


def invoice_milestones(row: dict, agent_type: str, hierarchy: str, month: int,
                       year: int = 2026, agent: str = None, property_type: str = None,
                       job_type: str = None, ev_type: str = None):
    """(policy, advance_date, balance_date, full_payment_date) for one invoice.

    The dates come back already chosen by the policy, so callers keep treating
    them as "the date the advance is due" and "the date the balance is due"
    without knowing which percentages produced them. The Job Type defaults to
    the one the invoice query classified, so the payout condition comes off
    the same row that priced the invoice.
    """
    if job_type is None and isinstance(row, dict):
        job_type = row.get("job_type")
    # An EV job's payment rule is on its EV row, found by EV Type.
    if ev_type is None and isinstance(row, dict) and normalize_job_type(job_type) == JOB_EV:
        ev_type = row.get("ev_type")
    policy = get_payout_policy(agent_type, hierarchy, month, year=year, agent=agent,
                              property_type=property_type, job_type=job_type,
                              ev_type=ev_type)
    if policy is None:
        policy = default_payout_policy(month, year=year)
    advance = milestone_date(row, policy.advance_trigger) if policy.is_multi_stage else ""
    balance = milestone_date(row, policy.balance_trigger)
    full = milestone_date(row, Decimal("100"))
    return policy, advance, balance, full


# How the legacy rule keys are expressed as unified rows. Each entry is
# (rule_type, needs_invoice_date, value_column).
_UNIFIED_RULE_MAP = {
    "basic_commission_cap":          ("advance", None,  "amount_rm"),
    "basic_balance_payout_trigger":  ("payout",  True,  "trigger_pct"),
    "basic_payout_pre_july":         ("payout",  False, "trigger_pct"),
}


def get_unified_rule_amount(rule_key: str, month: int, year: int = 2026):
    """Resolve one of the global payout/advance rules from the unified table."""
    spec = _UNIFIED_RULE_MAP.get(rule_key)
    if spec is None:
        return None
    want_type, needs_invoice_date, value_col = spec
    target = f"{year:04d}-{month:02d}"
    best = None
    for r in _unified_rows():
        if not _is_rule_row(r):
            continue
        if _cell(r, "rule_type").lower() != want_type:
            continue
        if needs_invoice_date is not None:
            if bool(_cell(r, "invoice_date_from")) != needs_invoice_date:
                continue
        eff = _cell(r, "effective_from")
        if not effective_covers(eff, target):
            continue
        start = effective_start(eff)
        if best is None or start > best[0]:
            try:
                best = (start, Decimal(_cell(r, value_col).replace("%", "").replace("RM", "")))
            except Exception:
                pass
    return best[1] if best else None


# ── Agent role hierarchy (dashboard "agent_roles") ───────────────────────────
# Who holds which role and who they report to. Effective-dated, so recalculating
# an old month uses the hierarchy as it stood then. While the table is empty
# every lookup returns None and callers keep their previous hardcoded mapping.

_DB_ROLES = None


def _role_rows() -> list:
    global _DB_ROLES
    if _DB_ROLES is None:
        _DB_ROLES = _load_db_table("agent_roles")
    return _DB_ROLES


# A role row is a Solar or an EV role (its `package_type`), so one agent can
# hold a Solar role and an EV role over the same months. Blank is Solar: every
# row saved before the column existed was a solar role. An EV role may name one
# EV Type ("EV Charger", ...) to cover only that service, like a rate row does.
PACKAGE_SOLAR = "Solar"
PACKAGE_EV = "EV"

# A Role of "All" holds for every role, Internal and Outsource alike. It names
# no role to price under: its own rates are saved with a blank Role and a blank
# Agent Type, which every rate lookup already reads as "all".
ROLE_ALL = "all"


def role_package(value, ev_type=None) -> str:
    """A role's package_type, or an invoice's job type (plus EV Type), as one of
    PACKAGE_SOLAR, PACKAGE_EV or an EV_TYPES subtype. Anything starting "EV" is
    EV; everything else -- blank included -- is Solar."""
    v = str(value or "").strip()
    if not v.lower().startswith("ev"):
        return PACKAGE_SOLAR
    return normalize_ev_type(v) or normalize_ev_type(ev_type) or PACKAGE_EV


def invoice_role_package(row: dict) -> str:
    """Which of the agent's roles an invoice is priced under. An EV-only invoice
    reads the EV role; a mixed invoice (JinkoSolar plus EV line items) is a solar
    sale first, so its Basic and payout read the Solar role -- its EV add-on is
    priced separately by job type. Everything else is Solar."""
    is_ev_only, is_mixed, _ = classify_ev_invoice(row)
    if is_mixed:
        return PACKAGE_SOLAR
    return role_package(normalize_job_type(row.get("job_type")), row.get("ev_type"))


def _package_fit(row_pkg: str, want: str) -> int:
    """How well a role of package `row_pkg` fits an invoice of package `want`:
    3 is the package itself. An EV invoice of one EV Type next takes a general
    EV role (2), then -- as an agent with no EV role always has -- the Solar
    role (1); an EV invoice of no known type takes an EV role of any type (2)
    before the Solar one. Anything else (0) is only the last resort, so a lone
    role of either package still governs every invoice, as it always has."""
    if row_pkg == want:
        return 3
    if want == PACKAGE_SOLAR:
        return 0
    if row_pkg == PACKAGE_SOLAR:
        return 1
    if row_pkg == PACKAGE_EV or want == PACKAGE_EV:
        return 2
    return 0


_ROLE_ROWS_BY_AGENT = (None, None, {})


def _role_rows_by_agent() -> dict:
    """{_canon_agent_key: [role rows]} in table order, rebuilt whenever the
    roles table or the agent name map is reloaded. Role lookups run for every
    invoice line and every override chain step; scanning the whole table each
    time was most of an export's run time."""
    global _ROLE_ROWS_BY_AGENT
    rows = _role_rows()
    mod = _agent_names_module()
    name_map = mod._load_maps()[0] if mod else None
    if _ROLE_ROWS_BY_AGENT[0] is rows and _ROLE_ROWS_BY_AGENT[1] is name_map:
        return _ROLE_ROWS_BY_AGENT[2]
    index = {}
    for r in rows:
        k = _canon_agent_key(r.get("agent", ""))
        if k:
            index.setdefault(k, []).append(r)
    _ROLE_ROWS_BY_AGENT = (rows, name_map, index)
    return index


def _covering_role_rows(agent_name, month, year, agent_type, package):
    """The agent's role rows covering the month, best first: by how well their
    package fits (see _package_fit), and within that the latest start first.

    Two rows of one package can start the same month and end differently (the
    Data page allows "Jul 2026 to Sep 2026" beside "Jul 2026 to Present").
    The one ending first wins while both cover the month: the bounded role is
    the deliberate exception to the open-ended one, and once it ends the other
    takes over by itself. Rows identical in all of that keep their table order,
    so the first one stored still wins as it always has.

    A Solar role of "All" only holds rates that apply whatever the role, like
    Factory 2% across an agent's whole history (2026-10-20), so every other
    row covering the month comes before it. Otherwise it wins wherever it fits
    the package better -- a month whose only other roles are EV -- and hands
    callers its blank Reports To and "All" as the seller's role, dropping the
    overrides above them. EV roles of "All" are agents' actual EV roles and
    keep their place."""
    key = _canon_agent_key(agent_name)
    if not key:
        return []
    want_type = str(agent_type or "").strip().lower()
    want_pkg = role_package(package)
    target = f"{year:04d}-{month:02d}"
    hits = []
    for r in _role_rows_by_agent().get(key, ()):
        if want_type and str(r.get("agent_type", "") or "").strip().lower() != want_type:
            continue
        eff = str(r.get("effective_from", "") or "").strip()
        if not effective_covers(eff, target):
            continue
        fit = _package_fit(role_package(r.get("package_type")), want_pkg)
        real = 0 if (str(r.get("hierarchy", "") or "").strip().lower() == ROLE_ALL
                     and role_package(r.get("package_type")) == PACKAGE_SOLAR) else 1
        hits.append((fit, effective_start(eff), effective_end(eff), r, real))
    # Python's sort is stable, so table order survives both passes.
    hits.sort(key=lambda h: h[2])
    hits.sort(key=lambda h: (h[4], h[0], h[1]), reverse=True)
    return [h[3] for h in hits]


def get_agent_role_row(agent_name: str, month: int, year: int = 2026,
                       agent_type: str = None, package: str = None):
    """The agent_roles row governing the month, or None. Matching is on the
    full name, case-insensitive — never on substrings, which is what made the
    old hardcoded mapping assign every 'J' name to the same senior.

    `agent_type` restricts the search to rows of that type. The roles table can
    hold both an Internal and an Outsource row for the same person (a transfer,
    or a mis-set Agent Type), and the latest one wins by date regardless of
    type. Handing an Internal role like "Branch Sales Manager" to the Outsource
    rate lookup matches no rate row at all, so the agent silently drops to the
    hardcoded default instead of their Data page rate.

    A row's effective month may be a closed range ("2026-01 to 2026-08"), in
    which case it governs those months only — after the end month the agent has
    no row here at all and the caller falls back to its own mapping, exactly as
    it does for someone never entered.

    `package` (see role_package / invoice_role_package) picks between a Solar
    and an EV role covering the same month. None means Solar -- NFP, Reports To
    and anything else that is not an EV sale. A package with no role of its own
    that month reads the closest other one (_package_fit), so an agent with a
    single role reads it for every invoice, as before the column existed."""
    rows = _covering_role_rows(agent_name, month, year, agent_type, package)
    return rows[0] if rows else None


def get_reporting_senior_from_table(agent_name: str, month: int, year: int = 2026):
    """(listed, reports_to) — `listed` says whether the role table has a row for
    this agent at all. The two cases must stay distinct: an agent listed with a
    blank Reports To reports to NOBODY (a top-level Senior), which is different
    from an agent nobody has entered yet, where the caller should fall back to
    its legacy mapping. Collapsing them would silently re-apply the old
    name-prefix guess to someone you had deliberately given no senior."""
    row = get_agent_role_row(agent_name, month, year=year)
    if not row:
        return False, None
    return True, (str(row.get("reports_to", "") or "").strip() or None)


def get_agent_role(agent_name: str, month: int, year: int = 2026,
                   agent_type: str = None, package: str = None):
    """The agent's role (e.g. 'Senior', 'Executive'), or None. Pass agent_type
    to ignore roles recorded against the other agent type, and package to read
    the Solar or EV role (see get_agent_role_row).

    A Role of "All" is no role to price under, so it is passed over for the
    next covering row -- the agent's role in the other package, if any -- and
    with none left this returns None and the caller prices them the way it
    prices an agent with no role. The "All" role's own rates still apply: they
    are saved against the agent with a blank Role, and a row naming the agent
    outranks any role-level row whatever role the lookup is made under."""
    for row in _covering_role_rows(agent_name, month, year, agent_type, package):
        role = str(row.get("hierarchy", "") or "").strip()
        if role.lower() == ROLE_ALL:
            continue
        return role or None
    return None


# ── Overrides (Data page Override Rate % / From Role + Reports To) ───────────
# Since 2026-10-20 every Basic override comes from the Data page. An agent's
# own rate rows carry Override Rate % and From Role pairs ("0.5" / "OSA", or
# several comma-joined: "0.5, 0.75" / "Sales Team Manager, Sales Consultant");
# a From Role may name one person, stored as "Role (Name)", and either may end
# in "(group)" to take in everyone under them (see _parse_override_from).
# Whoever sells, the people above them in the Reports To chain are checked in
# turn, and each one whose override takes in the seller earns that rate on the
# sale -- from July 2026 only; before that only the seller's own Reports To
# earns on a role line (TEAM_OVERRIDES_FROM). A line naming a person earns
# wherever its owner sits. Nobody else earns an override: the old hardcoded
# 0.25% / 0.5% and the hardcoded tier map are gone.
#
# Gan Lai Soon's 0.75% OGM override, Safwan's 0.5% and the Factory split stay
# hardcoded in the engines (user, 2026-10-20), so Gan Lai Soon is skipped here
# rather than paid twice.

_OVERRIDE_SKIP = {"gan lai soon"}
_OVERRIDE_CHAIN_LIMIT = 8


_GROUP_SUFFIX = re.compile(r"\s*\(group\)\s*$", re.I)

# Overrides from July 2026 are a team's: anyone in the leader's Reports To
# chain can earn. Before that they were individual (user, 27 Oct 2026): only
# the seller's own Reports To earns, plus anyone whose override names the
# seller as a person.
TEAM_OVERRIDES_FROM = (2026, 7)


def _parse_override_from(value: str) -> tuple[str, str, bool]:
    """("Role", "Person", group) for a From Role cell:
      ""                                  everyone below (all roles)
      "Sales Team Manager"                anyone holding that role below
      "Sales Team Manager (group)"        those people and everyone under them
      "Sales Team Manager (Ng Zhan Yi)"   that one person
      "Sales Team Manager (Ng Zhan Yi) (group)"  that person and everyone under them"""
    text = str(value or "").strip()
    group = bool(_GROUP_SUFFIX.search(text))
    if group:
        text = _GROUP_SUFFIX.sub("", text).strip()
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip(), group
    return text, "", group


def _split_override_from(value: str) -> tuple[str, str]:
    """("Role", "Person") for a From Role cell; "Role (Person)" names one
    person, a plain "Role" names everyone holding it, blank names everyone.
    A "(group)" suffix is set aside -- see _parse_override_from."""
    role, person, _group = _parse_override_from(value)
    return role, person


def _override_pairs(row) -> list:
    """[(rate as a fraction, From Role cell)] for one rate row."""
    pcts = [p.strip() for p in _cell(row, "override_rate_pct").split(",")]
    froms = [f.strip() for f in str(row.get("override_from", "") or "").split(",")]
    out = []
    for i, p in enumerate(pcts):
        if not p:
            continue
        try:
            rate = Decimal(p.replace("%", "")) / Decimal("100")
        except Exception:
            continue
        if rate <= 0:
            continue
        out.append((rate, froms[i] if i < len(froms) else ""))
    return out


def _override_row_for(agent_name: str, role_row, month: int, year: int,
                      package: str, property_type: str = None):
    """The Data page rate row whose overrides `agent_name` earns with in this
    month: their own rows (naming them) before role-level rows for their role,
    a row for the property before a general one, the latest start first. Only
    rows that carry an override are considered, so an override-only line
    counts as much as a full rate line."""
    target = f"{year:04d}-{month:02d}"
    agent_key = _canon_agent_key(agent_name)
    role = _normalize_hierarchy(str((role_row or {}).get("hierarchy", "") or ""))
    atype = str((role_row or {}).get("agent_type", "") or "").strip().lower()
    want_job = JOB_EV if role_package(package) != PACKAGE_SOLAR else JOB_SOLAR
    prop_key = str(property_type or "").strip().lower()
    best = None
    for r in _unified_rows():
        if (_cell(r, "rate_type") or "Basic Commission") != "Basic Commission":
            continue
        if _cell(r, "remarks").lower() == "deleted":
            continue
        if not _cell(r, "override_rate_pct"):
            continue
        if not effective_covers(_cell(r, "effective_from"), target):
            continue
        row_agents = _row_agent_list(r)
        if row_agents:
            if agent_key not in {_canon_agent_key(a) for a in row_agents}:
                continue
        else:
            row_role = _cell(r, "hierarchy")
            if not role or (row_role and _normalize_hierarchy(row_role) != role):
                continue
            row_atype = _cell(r, "agent_type").lower()
            if row_atype and atype and row_atype != atype:
                continue
        row_props = _row_property_list(r)
        prop_score = 0
        if row_props:
            if not prop_key:
                continue
            caller_props = [normalize_prop_name(p) for p in prop_key.split(",") if p.strip()]
            matches = [len(caller_props) - i for i, cp in enumerate(caller_props) if cp in row_props]
            if not matches:
                continue
            prop_score = max(matches)
        job_fit = 1 if _row_job_type(r) == want_job else 0
        cand = ((10 if row_agents else 0) + prop_score, job_fit,
                effective_start(_cell(r, "effective_from")))
        if best is None or cand > best[0]:
            best = (cand, r)
    return best[1] if best else None


def override_name_map(names) -> dict:
    """{comparison key: name} for the names a report already lists, so an
    override lands on the same row as the recipient's own commission."""
    out = {}
    for n in names:
        k = _canon_agent_key(n)
        if k and k not in out:
            out[k] = n
    return out


def get_data_page_overrides(agent_name: str, month: int, year: int = 2026,
                            agent_type: str = None, package: str = None,
                            property_type: str = None) -> list:
    """[{"name", "key", "rate", "from_role"}] -- who earns an override on a
    sale by `agent_name` dated in this month, at what rate, read only from the
    Data page. "name" is the Reports To cell as typed and "key" its alias-folded
    comparison key (_canon_agent_key), so callers can credit the override to
    the same name the person's own sales are listed under. Empty when the
    seller has no role, no one above them has an override naming them, or the
    chain is not filled in."""
    seller_row = get_agent_role_row(agent_name, month, year=year,
                                    agent_type=agent_type, package=package)
    if seller_row is None and agent_type:
        seller_row = get_agent_role_row(agent_name, month, year=year, package=package)
    if seller_row is None:
        return []
    seller_role = _normalize_hierarchy(str(seller_row.get("hierarchy", "") or ""))
    seller_key = _canon_agent_key(agent_name)
    team = (year, month) >= TEAM_OVERRIDES_FROM
    skip = {_canon_agent_key(s) for s in _OVERRIDE_SKIP}

    # The Reports To chain above the seller: [(name, key, role row)], nearest first.
    chain = []
    seen = {seller_key}
    current = seller_row
    for _ in range(_OVERRIDE_CHAIN_LIMIT):
        boss = str(current.get("reports_to", "") or "").strip()
        boss_key = _canon_agent_key(boss)
        if not boss or boss_key in seen:
            break
        seen.add(boss_key)
        boss_row = get_agent_role_row(boss, month, year=year, package=package)
        chain.append((boss, boss_key, boss_row))
        if boss_row is None:
            break
        current = boss_row

    def _role_of(row):
        return _normalize_hierarchy(str((row or {}).get("hierarchy", "") or ""))

    # Who sits between the seller and someone in the chain (the seller too):
    # the people a "(group)" line can reach the seller through.
    below = [(seller_key, seller_role)]

    def _match(from_cell, between, in_chain):
        """How strongly one From Role line takes in this sale, 0 = not at all.
        A person beats a person's group, which beats a role's group, then a
        role, then all roles; a leader earns on the best line only."""
        role, person, group = _parse_override_from(from_cell)
        if person:
            pkey = _canon_agent_key(person)
            if pkey == seller_key:
                return 5
            if group and team and any(k == pkey for k, _r in between):
                return 4
            return 0
        if not in_chain:
            return 0
        if not role or role.lower() == ROLE_ALL:
            return 1
        want = _normalize_hierarchy(role)
        if group:
            return 3 if any(r == want for _k, r in between) else 0
        return 2 if want == seller_role else 0

    out = []
    earned = set()
    for depth, (boss, boss_key, boss_row) in enumerate(chain):
        direct = depth == 0
        if boss_key not in skip:
            row = _override_row_for(boss, boss_row, month, year, package, property_type)
            best = (0, None, None)
            for rate, from_cell in (_override_pairs(row) if row else []):
                score = _match(from_cell, below, True)
                # Before July only the direct Reports To earns on role lines;
                # anyone further up needs a line naming the seller.
                if not team and not direct and score != 5:
                    score = 0
                if score > best[0]:
                    best = (score, rate, from_cell)
            if best[0]:
                out.append({"name": boss, "key": boss_key, "rate": best[1],
                            "from_role": best[2]})
                earned.add(boss_key)
        below.append((boss_key, _role_of(boss_row)))

    # A line naming a person (or a person's group) earns wherever its owner
    # sits -- a same-level override such as Teng Kah Kent's 0.1% on Ng Zhan Yi
    # needs no Reports To between them.
    chain_keys = {k for _n, k, _r in chain}
    for owner in dict.fromkeys(o for o, _r in _person_override_rows(month, year)):
        okey = _canon_agent_key(owner)
        if okey in earned or okey in chain_keys or okey == seller_key or okey in skip:
            continue
        owner_row = get_agent_role_row(owner, month, year=year, package=package)
        own = _override_row_for(owner, owner_row, month, year, package, property_type)
        if own is None:
            continue
        best = (0, None, None)
        for rate, from_cell in _override_pairs(own):
            score = _match(from_cell, below, False)
            if score > best[0]:
                best = (score, rate, from_cell)
        if best[0]:
            out.append({"name": owner, "key": okey, "rate": best[1], "from_role": best[2]})
            earned.add(okey)
    return out


_PERSON_ROWS_INDEX = (None, [])


def _person_override_rows(month: int, year: int) -> list:
    """[(owner name, rate row)] for rate rows naming one owner whose overrides
    include a person ("Role (Name)") line and cover the month. Built once per
    load of the rate table: it is asked for every invoice line."""
    global _PERSON_ROWS_INDEX
    rows = _unified_rows()
    if _PERSON_ROWS_INDEX[0] is not rows:
        index = []
        for r in rows:
            if (_cell(r, "rate_type") or "Basic Commission") != "Basic Commission":
                continue
            if _cell(r, "remarks").lower() == "deleted" or not _cell(r, "override_rate_pct"):
                continue
            owners = _row_agent_list(r)
            if len(owners) != 1:
                continue
            if not any(_parse_override_from(c)[1] for _rate, c in _override_pairs(r)):
                continue
            index.append((owners[0], r))
        _PERSON_ROWS_INDEX = (rows, index)
    target = f"{year:04d}-{month:02d}"
    return [(o, r) for o, r in _PERSON_ROWS_INDEX[1]
            if effective_covers(_cell(r, "effective_from"), target)]


# ── Agent type routing (dashboard "agent_roles") ─────────────────────────────
# Which report an agent belongs to, and therefore which half of the rate table
# applies to them. Postgres' user.agent_type is blank for a number of people,
# and every engine reads blank as "outsource", so an internal agent with no
# agent_type silently landed in the outsource report and was paid the outsource
# rate no matter what was entered on the Data page. The Agent Roles & Hierarchy
# page is where agent type is actually maintained, so a row there wins.

_ROUTABLE_AGENT_TYPES = {"internal": "internal", "outsource": "outsource"}


_ROUTING_INDEX = (None, {})


def _routing_index() -> dict:
    """{lower-cased agent name: [(effective_from, type)]} from the role rows
    that say something about routing. Tombstones (hidden) are rows the roles
    grid deleted, and values like 'Sales' or blank are not a report, so neither
    can override Postgres. Built once per load of the roles table: it is asked
    for every invoice line, and rescanning the whole table each time was a
    large share of an export's run time."""
    global _ROUTING_INDEX
    rows = _role_rows()
    if _ROUTING_INDEX[0] is rows:
        return _ROUTING_INDEX[1]
    index = {}
    for r in rows:
        if r.get("hidden"):
            continue
        atype = _ROUTABLE_AGENT_TYPES.get(str(r.get("agent_type") or "").strip().lower())
        if not atype:
            continue
        name = str(r.get("agent") or "").strip().lower()
        eff = str(r.get("effective_from") or "").strip()
        if name and eff:
            index.setdefault(name, []).append((eff, atype))
    _ROUTING_INDEX = (rows, index)
    return index


def get_agent_type_override(agent_name: str, month: int = None, year: int = None):
    """'internal' / 'outsource' as set on the Agent Roles & Hierarchy page, or
    None when nothing there covers the agent — then the caller keeps Postgres'
    answer. Effective-dated on the invoice's own month, exactly like the rates,
    so a transfer mid-year leaves earlier invoices under the type that applied
    at the time."""
    key = str(agent_name or "").strip().lower()
    if not key:
        return None
    target = f"{int(year or 2026):04d}-{int(month):02d}" if month else None
    best = None
    for eff, atype in _routing_index().get(key, ()):
        # With no month to test against, "which type applies now" is the best
        # this can mean, so a row is only ranked, never filtered.
        if target and not effective_covers(eff, target):
            continue
        start = effective_start(eff)
        if best is None or start > best[0]:
            best = (start, atype)
    return best[1] if best else None


def get_agent_display_name(agent_name: str, month: int = None, year: int = None):
    """Full Name from the Agent Roles & Hierarchy page for this agent, falling
    back to that page's own Agent Name (from eeAdmin) field when Full Name is
    blank, or None when nothing there covers the agent — then the caller keeps
    whatever name it already has. Effective-dated exactly like
    get_agent_type_override: with no month given, the agent's latest entry
    decides."""
    key = str(agent_name or "").strip().lower()
    if not key:
        return None
    target = f"{int(year or 2026):04d}-{int(month):02d}" if month else None
    best = None
    best_start = ""
    for r in _role_rows():
        if r.get("hidden"):
            continue
        if str(r.get("agent", "") or "").strip().lower() != key:
            continue
        eff = str(r.get("effective_from", "") or "").strip()
        if not eff:
            continue
        if target and not effective_covers(eff, target):
            continue
        start = effective_start(eff)
        if best is None or start > best_start:
            best, best_start = r, start
    if not best:
        return None
    full_name = str(best.get("full_name") or "").strip()
    if full_name:
        return full_name
    return str(best.get("agent") or "").strip() or None


def agent_type_override_names() -> list:
    """Every agent name the roles page assigns a report to. The internal engines
    filter on Postgres' agent_type inside SQL, which drops an overridden agent
    before Python ever sees the row; they widen that filter with this list and
    then let resolve_agent_type() make the real call per invoice."""
    return sorted(_routing_index())


def resolve_agent_type(agent_name: str, pg_agent_type: str, month: int = None,
                       year: int = None) -> str:
    """Which report an invoice belongs to: 'internal' or 'outsource'. The roles
    page answers when it has a row covering the month; otherwise Postgres does,
    where 'internal'/'full time' mean internal and anything else — blank
    included — means outsource."""
    override = get_agent_type_override(agent_name, month=month, year=year)
    if override:
        return override
    pg = str(pg_agent_type or "").strip().lower()
    return "internal" if pg in ("internal", "full time") else "outsource"


def get_db_basic_rate(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                      agent: str = None, property_type: str = None,
                      job_type: str = None, ev_type: str = None):
    """Effective-dated rate entered on the dashboard Data page, or None if no
    entry covers the given month. Rows store rate_pct as a percentage string
    and effective_from as 'YYYY-MM'; the row with the latest effective_from
    that is <= the target month wins. A row naming a specific agent beats the
    role-level (blank agent) row for that agent.

    The unified commission_rates table is consulted first; basic_rates is the
    fallback for anything it does not cover."""
    unified = get_unified_basic_rate(agent_type, hierarchy, month, year=year, agent=agent,
                                     property_type=property_type, job_type=job_type,
                                     ev_type=ev_type)
    if unified is not None:
        return unified

    global _DB_RATES
    if _DB_RATES is None:
        _DB_RATES = _load_db_table("basic_rates")
    target = f"{year:04d}-{month:02d}"
    agent_key = _canon_agent_key(agent)
    best_agent = None   # row naming this specific agent
    best_role = None    # role-level row (blank agent)
    for r in _DB_RATES:
        rate_type = str(r.get("rate_type", "") or "Basic Commission").strip()
        if rate_type != "Basic Commission":
            continue
        if str(r.get("agent_type", "")).strip().lower() != agent_type:
            continue
        if _normalize_hierarchy(str(r.get("hierarchy", ""))) != hierarchy:
            continue
        eff = str(r.get("effective_from", "")).strip()
        if not eff or eff > target:
            continue
        row_agents = _row_agent_list(r)
        if row_agents and agent_key not in {_canon_agent_key(a) for a in row_agents}:
            continue
        try:
            rate = Decimal(str(r.get("rate_pct", "")).replace("%", "").strip()) / Decimal("100")
        except Exception:
            continue
        if row_agents:
            if best_agent is None or eff > best_agent[0]:
                best_agent = (eff, rate)
        else:
            if best_role is None or eff > best_role[0]:
                best_role = (eff, rate)
    if best_agent:
        return best_agent[1]
    return best_role[1] if best_role else None


def get_rule_amount(rule_key: str, month: int, year: int = 2026, default=None):
    """Effective-dated rule value (e.g. 'basic_commission_cap' = 300) entered on
    the dashboard Data page; falls back to `default` when no entry covers the month.
    The unified commission_rates table wins over the legacy rule_settings rows."""
    unified = get_unified_rule_amount(rule_key, month, year=year)
    if unified is not None:
        return unified

    global _DB_RULES
    if _DB_RULES is None:
        _DB_RULES = _load_db_table("rule_settings")
    target = f"{year:04d}-{month:02d}"
    best = None
    for r in _DB_RULES:
        if str(r.get("rule_key", "")).strip() != rule_key:
            continue
        eff = str(r.get("effective_from", "")).strip()
        if not eff or eff > target:
            continue
        if best is None or eff > best[0]:
            try:
                best = (eff, Decimal(str(r.get("value", "")).strip()))
            except Exception:
                pass
    return best[1] if best else default


def get_basic_rate_info(agent_type: str, hierarchy: str, month: int, agent: str = None,
                        property_type: str = None):
    """(rate, source) naming which of the four sources actually answered:
      'unified' — a row entered on the dashboard Data page (the intended source)
      'legacy'  — a leftover basic_rates row (should be empty after the merge)
      'missing' — nothing on the Data page covers it; the rate is 0 (since 2026-10-20)
    Keeping these apart is what makes "is my input wired in?" answerable."""
    rate, source, _eff = get_basic_rate_detail(agent_type, hierarchy, month,
                                               agent=agent, property_type=property_type)
    return rate, source


def get_basic_rate_detail(agent_type: str, hierarchy: str, month: int, agent: str = None,
                          property_type: str = None, year: int = 2026,
                          job_type: str = None, ev_type: str = None):
    """(rate, source, effective_from) — as get_basic_rate_info, plus the month
    the winning entry takes effect from ('' when a fallback answered, since
    fallbacks are not effective-dated)."""
    agent_type = agent_type.strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    hit = get_unified_basic_rate_row(agent_type, hierarchy, month, year=year, agent=agent,
                                     property_type=property_type, job_type=job_type,
                                     ev_type=ev_type)
    if hit is not None:
        return hit[0], "unified", hit[1]
    legacy = get_db_basic_rate(agent_type, hierarchy, month, agent=agent,
                               property_type=property_type)
    if legacy is not None:
        return legacy, "legacy", ""
    return Decimal("0"), "missing", ""


def get_factory_base_rate(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                          agent: str = None) -> Decimal:
    """The base rate of a Factory solar job: the agent's Factory rate on the
    Data page (a Solar row with Property = Factory), or 0 when none covers the
    month. The profit sharing negotiated on the panels is added on top by the
    caller; it is still typed per customer on the report. Before 2026-10-20 the
    base was a hardcoded 2% whatever the Data page said."""
    return get_basic_rate(agent_type, hierarchy, month, agent=agent,
                          property_type="Factory", year=year, job_type=JOB_SOLAR)


def get_basic_rate(agent_type: str, hierarchy: str, month: int, agent: str = None,
                   property_type: str = None, year: int = 2026,
                   job_type: str = None, ev_type: str = None) -> Decimal:
    """The basic commission rate entered on the dashboard Data page, or 0 when
    nothing there covers the case.

    Since 2026-10-20 the Data page is the only source (user's decision): the
    cached sheet copy, the hardcoded 3% / 3.25% / 4.5% / 5% defaults and the
    "Senior = Executive + 0.25%" rule no longer answer. A sale with no rate
    entered pays RM 0 and the report flags it (MISSING_RATE_LABEL), so the gap
    is seen and filled in rather than paid from a number nobody entered."""
    agent_type = agent_type.strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    db_rate = get_db_basic_rate(agent_type, hierarchy, month, year=year, agent=agent,
                                property_type=property_type, job_type=job_type,
                                ev_type=ev_type)
    if db_rate is not None:
        return db_rate
    return Decimal("0")
