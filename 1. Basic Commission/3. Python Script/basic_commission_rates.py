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


def _canon_agent_key(name) -> str:
    """Comparison key for an agent name, alias-folded via the roles page."""
    text = str(name or "").strip()
    if not text:
        return ""
    mod = _agent_names_module()
    if mod:
        try:
            return mod.normalize_key(mod.resolve(text))
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
_ALL_PROPERTY_TYPES = {"residential", "commercial", "shop lot", "factory",
                       "ngo project", "government project"}


def _row_property_list(r) -> list:
    props = [p.strip() for p in _cell(r, "property_type").lower().split(",") if p.strip()]
    if not props or _ALL_PROPERTY_TYPES.issubset(set(props)):
        return []
    return props


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
                           agent: str = None, property_type: str = None):
    """Effective-dated rate from the unified table, or None when no row covers
    the case."""
    hit = get_unified_basic_rate_row(agent_type, hierarchy, month, year=year,
                                     agent=agent, property_type=property_type)
    return hit[0] if hit else None


def get_unified_basic_rate_row(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                               agent: str = None, property_type: str = None):
    """(rate, effective_from) of the winning unified row, or None. Blank
    agent_type/role/property on a row means "applies to all"; a row naming a
    specific agent beats a role-level row."""
    best = _best_unified_row(agent_type, hierarchy, month, year=year, agent=agent,
                             property_type=property_type)
    return (best[1], best[2]) if best else None


def _best_unified_row(agent_type: str, hierarchy: str, month: int, year: int = 2026,
                      agent: str = None, property_type: str = None):
    """(row, rate, effective_from) for the unified row that governs this case.

    The rate and the payout condition live on the SAME row, so both must be read
    off the one winner — resolving them independently would let an invoice take
    its rate from one row and its payout trigger from another.
    """
    target = f"{year:04d}-{month:02d}"
    agent_key = _canon_agent_key(agent)
    prop_key = str(property_type or "").strip().lower()
    # The most specific row wins, and only then the latest effective_from:
    # naming the agent (2) outranks naming the property (1), which outranks a
    # row that names neither. Without this the winner would depend on row order.
    best = None  # (specificity, effective_from, rate)
    for r in _unified_rows():
        if (_cell(r, "rate_type") or "Basic Commission") != "Basic Commission":
            continue
        if _is_rule_row(r):
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
        if row_props:
            if not prop_key:
                continue
            caller_props = [p.strip().lower() for p in prop_key.split(",") if p.strip()]
            if not any(cp in row_props for cp in caller_props):
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
        spec = (2 if row_agents else 0) + (1 if row_props else 0)
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
                      agent: str = None, property_type: str = None):
    """The payout condition governing this agent/role/property in this month, or
    None when the Data page has nothing to say about it."""
    agent_type = str(agent_type or "").strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    best = _best_unified_row(agent_type, hierarchy, month, year=year, agent=agent,
                             property_type=property_type)
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


def milestone_sql_parts(thresholds=None, exclusions: str = None):
    """(ctes, selects, joins) SQL fragments for the milestone dates.

    Each threshold gets one CTE finding the earliest payment date at which the
    running total reached that share of the invoice. 100% compares against the
    total directly rather than multiplying by 1.0, so an invoice paid to the cent
    is not pushed below the line by binary rounding.
    """
    thresholds = list(thresholds or payout_thresholds_in_use())
    if Decimal("100") not in thresholds:
        thresholds.append(Decimal("100"))
    excl = exclusions or MILESTONE_PAYMENT_EXCLUSIONS
    ctes, selects, joins = [], [], []
    for t in sorted(set(Decimal(x) for x in thresholds)):
        name = f"pct_{_threshold_slug(t)}"
        col = milestone_column(t)
        if t == Decimal("100"):
            cmp_sql = "sub.running_total >= sub.total_amount"
        else:
            frac = (Decimal(t) / Decimal("100")).normalize()
            cmp_sql = f"sub.running_total >= sub.total_amount * {format(frac, 'f')}"
        ctes.append(f"""-- earliest date each invoice reached {t}% of total_amount paid
{name} AS (
  SELECT sub.linked_invoice,
         MIN(sub.payment_date) AS {col}
  FROM (
    SELECT p.linked_invoice,
           p.payment_date,
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
        selects.append(f"    {name}.{col},")
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
                       year: int = 2026, agent: str = None, property_type: str = None):
    """(policy, advance_date, balance_date, full_payment_date) for one invoice.

    The dates come back already chosen by the policy, so callers keep treating
    them as "the date the advance is due" and "the date the balance is due"
    without knowing which percentages produced them.
    """
    policy = get_payout_policy(agent_type, hierarchy, month, year=year, agent=agent,
                              property_type=property_type)
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


def get_agent_role_row(agent_name: str, month: int, year: int = 2026,
                       agent_type: str = None):
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
    it does for someone never entered."""
    key = _canon_agent_key(agent_name)
    if not key:
        return None
    want_type = str(agent_type or "").strip().lower()
    target = f"{year:04d}-{month:02d}"
    best = None
    best_start = ""
    for r in _role_rows():
        if _canon_agent_key(r.get("agent", "")) != key:
            continue
        if want_type and str(r.get("agent_type", "") or "").strip().lower() != want_type:
            continue
        eff = str(r.get("effective_from", "") or "").strip()
        if not effective_covers(eff, target):
            continue
        start = effective_start(eff)
        if best is None or start > best_start:
            best, best_start = r, start
    return best


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
                   agent_type: str = None):
    """The agent's role (e.g. 'Senior', 'Executive'), or None. Pass agent_type
    to ignore roles recorded against the other agent type."""
    row = get_agent_role_row(agent_name, month, year=year, agent_type=agent_type)
    if not row:
        return None
    return str(row.get("hierarchy", "") or "").strip() or None


# ── Agent type routing (dashboard "agent_roles") ─────────────────────────────
# Which report an agent belongs to, and therefore which half of the rate table
# applies to them. Postgres' user.agent_type is blank for a number of people,
# and every engine reads blank as "outsource", so an internal agent with no
# agent_type silently landed in the outsource report and was paid the outsource
# rate no matter what was entered on the Data page. The Agent Roles & Hierarchy
# page is where agent type is actually maintained, so a row there wins.

_ROUTABLE_AGENT_TYPES = {"internal": "internal", "outsource": "outsource"}


def _routing_rows() -> list:
    """Role rows that say something about routing. Tombstones (hidden) are rows
    the roles grid deleted, and values like 'Sales' or blank are not a report,
    so neither can override Postgres."""
    out = []
    for r in _role_rows():
        if r.get("hidden"):
            continue
        atype = _ROUTABLE_AGENT_TYPES.get(str(r.get("agent_type") or "").strip().lower())
        if not atype:
            continue
        name = str(r.get("agent") or "").strip().lower()
        eff = str(r.get("effective_from") or "").strip()
        if name and eff:
            out.append((name, eff, atype))
    return out


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
    for name, eff, atype in _routing_rows():
        if name != key:
            continue
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
    return sorted({name for name, _eff, _atype in _routing_rows()})


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
                      agent: str = None, property_type: str = None):
    """Effective-dated rate entered on the dashboard Data page, or None if no
    entry covers the given month. Rows store rate_pct as a percentage string
    and effective_from as 'YYYY-MM'; the row with the latest effective_from
    that is <= the target month wins. A row naming a specific agent beats the
    role-level (blank agent) row for that agent.

    The unified commission_rates table is consulted first; basic_rates is the
    fallback for anything it does not cover."""
    unified = get_unified_basic_rate(agent_type, hierarchy, month, year=year, agent=agent,
                                     property_type=property_type)
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
      'sheet'   — the cached sheet CSV / hardcoded fallback
    Keeping these apart is what makes "is my input wired in?" answerable."""
    rate, source, _eff = get_basic_rate_detail(agent_type, hierarchy, month,
                                               agent=agent, property_type=property_type)
    return rate, source


def get_basic_rate_detail(agent_type: str, hierarchy: str, month: int, agent: str = None,
                          property_type: str = None, year: int = 2026):
    """(rate, source, effective_from) — as get_basic_rate_info, plus the month
    the winning entry takes effect from ('' when a fallback answered, since
    fallbacks are not effective-dated)."""
    agent_type = agent_type.strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)
    hit = get_unified_basic_rate_row(agent_type, hierarchy, month, year=year, agent=agent,
                                     property_type=property_type)
    if hit is not None:
        return hit[0], "unified", hit[1]
    legacy = get_db_basic_rate(agent_type, hierarchy, month, agent=agent,
                               property_type=property_type)
    if legacy is not None:
        return legacy, "legacy", ""
    return get_basic_rate(agent_type, hierarchy, month), "sheet", ""


def get_basic_rate(agent_type: str, hierarchy: str, month: int, agent: str = None,
                   property_type: str = None, year: int = 2026) -> Decimal:
    """Resolve basic commission rate: dashboard Data page entries first,
    then legacy cached sheet data, then hardcoded defaults."""
    # Normalize inputs
    agent_type = agent_type.strip().lower()
    hierarchy = _normalize_hierarchy(hierarchy)

    # System-entered rates (dashboard Data page) take priority
    db_rate = get_db_basic_rate(agent_type, hierarchy, month, year=year, agent=agent,
                                property_type=property_type)
    if db_rate is not None:
        return db_rate

    rates_data = load_rates_data()
    key = (agent_type, hierarchy)
    if not rates_data or key not in rates_data:
        # Fallback to hardcoded defaults
        if agent_type == "internal":
            return Decimal("0.0325") if hierarchy == "senior" else Decimal("0.03")
        else:
            return Decimal("0.05") if hierarchy == "ogm" else Decimal("0.045")
            
    info = rates_data[key]
    target_month_name = _MONTH_MAPPING.get(month, "mac")
    
    # Look up direct rate for target month name
    rate = info["rates"].get(target_month_name)
    if rate is not None:
        return rate
        
    # If not found (blank cell), check if it is "senior" and has override rule
    if hierarchy == "senior" and "from executive" in info["override_rule"].lower():
        exec_rate = get_basic_rate(agent_type, "executive", month)
        return exec_rate + Decimal("0.0025")
        
    # Otherwise fallback to the most recent month in chronological order
    month_order = ["jan", "feb", "mac", "jun", "jul"]
    try:
        target_idx = month_order.index(target_month_name)
    except ValueError:
        target_idx = len(month_order) - 1
        
    for idx in range(target_idx, -1, -1):
        m_name = month_order[idx]
        rate = info["rates"].get(m_name)
        if rate is not None:
            return rate
            
    # Default fallback if all fails
    if agent_type == "internal":
        return Decimal("0.0325") if hierarchy == "senior" else Decimal("0.03")
    else:
        return Decimal("0.05") if hierarchy == "ogm" else Decimal("0.045")
