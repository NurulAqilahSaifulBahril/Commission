"""Runtime agent-name resolver shared by the web dashboard and the export packs.

Resolves any stored agent name variant (eeAdmin name, nickname, compound
nickname, or full name) to the canonical FULL name entered on the dashboard's
Agent Roles & Hierarchy page (the agent_roles table), displayed in Title Case.
Names that are not in the table are kept but re-cased to Title Case.

The lookup is built from the agent_roles table read via
basic_commission_rates._load_db_table (Postgres first, legacy SQLite fallback),
so whatever is typed into the Data Page is what every report displays.
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_NAME_MAP = None
_IC_MAP = None


def normalize_key(name):
    """Collapse whitespace and case so lookups tolerate spacing/case variance."""
    return re.sub(r"\s+", "", str(name)).casefold()


def title_case(name):
    """Capitalise the first letter of each word, lowercasing the rest.

    Used for names that are not present in the roster so they still render in a
    consistent Title Case (e.g. "SUNNY tan" -> "Sunny Tan").
    """
    text = str(name).strip()
    if not text:
        return text
    return re.sub(
        r"[A-Za-z]+",
        lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(),
        text,
    )


# Some invoices carry the word "Referral" glued to, or sitting in front of, the
# referrer's actual name. Every spelling seen in the data so far; all are long
# enough that no real name can begin with one.
_REFERRAL_WORDS = (
    "refferral", "refferal", "referrer", "referrel", "referral",
    "referer", "referal",
)


def clean_referral_name(name):
    """The referrer's own name, with any "Referral"/"Referrer" prefix removed.

    Data entry writes the same person three ways -- "Referrer Sim Wei Ling",
    "Referrel Sim Wei Ling", "Referralyap Chin Choi" -- which makes one referrer
    look like several. Stripping the word first lets them collapse.

    Returns "" when the entry was the bare word with no name after it: that is
    a placeholder, not a person.
    """
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if not text:
        return ""
    low = text.casefold()
    for word in _REFERRAL_WORDS:
        if not low.startswith(word):
            continue
        rest = text[len(word):].lstrip(" .:;-_,/")
        if not re.search(r"[A-Za-z]", rest):
            return ""          # the word on its own
        # Only re-case what was glued on ("...yap Chin Choi"); a name that was
        # already separated keeps whatever capitalisation it was typed with.
        return title_case(rest) if rest[:1].islower() else rest
    return text


def _role_rows():
    rates_dir = os.path.join(REPO_ROOT, "1. Basic Commission", "3. Python Script")
    if rates_dir not in sys.path:
        sys.path.insert(0, rates_dir)
    from basic_commission_rates import _load_db_table
    return _load_db_table("agent_roles")


def _effective_start(eff):
    """Start month of an effective_from cell, which may be a closed range
    ("2026-01 to 2026-08"). Ranking on the raw cell would put a range above the
    plain month it starts in, on string length alone."""
    rates_dir = os.path.join(REPO_ROOT, "1. Basic Commission", "3. Python Script")
    if rates_dir not in sys.path:
        sys.path.insert(0, rates_dir)
    from basic_commission_rates import effective_start
    return effective_start(eff)


def _nick_parts(nick):
    """Compound nicknames are typed as "A / B" (and occasionally comma-separated)."""
    return [p.strip() for p in re.split(r"[/,;]", str(nick or "")) if p.strip()]


def _role_aliases(r):
    """Every spelling the Data page stores for this hierarchy row."""
    agent = str(r.get("agent") or "").strip()
    full = str(r.get("full_name") or "").strip()
    aliases = []
    display = title_case(full if full else agent)
    if display:
        aliases.append(display)
    if agent:
        aliases.append(agent)
    if full:
        aliases.append(full)
    aliases.extend(_nick_parts(r.get("nick_name")))
    return aliases


def _person_key(r):
    """Stable identity for one person across role-history rows.

    pg_bubble_id is preferred (it survives an eeAdmin rename). Otherwise the
    eeAdmin agent name, then Full Name / Nick Name for roster-only rows that
    never had an eeAdmin name.
    """
    bid = str(r.get("pg_bubble_id") or "").strip()
    if bid:
        return ("id", bid)
    agent = str(r.get("agent") or "").strip()
    if agent:
        return ("agent", normalize_key(agent))
    full = str(r.get("full_name") or "").strip()
    if full:
        return ("full", normalize_key(full))
    nick = str(r.get("nick_name") or "").strip()
    if nick:
        return ("nick", normalize_key(nick))
    return None


def _build_maps():
    """(name_map, ic_map) from the Agent Roles & Hierarchy page.

    name_map: alias -> canonical full name. One row per eeAdmin agent name
    wins: the latest effective_from among non-hidden rows.

    ic_map: alias -> IC No. IC is identity, not period-specific, so every
    non-hidden row for the same person contributes its name aliases, and the
    latest row that actually has an IC supplies the number. That is what the
    Monthly Commission Slip must print: every IC entered on the Data page,
    reachable from any spelling that page knows.
    """
    try:
        rows = _role_rows()
    except Exception:
        return {}, {}

    latest = {}  # agent key -> row
    people = {}  # person key -> {ic, ic_start, aliases}
    for r in rows:
        if r.get("hidden"):
            continue
        agent = str(r.get("agent") or "").strip()
        if agent:
            key = normalize_key(agent)
            eff = _effective_start(r.get("effective_from"))
            prev = latest.get(key)
            if prev is None or eff > _effective_start(prev.get("effective_from")):
                latest[key] = r

        person_key = _person_key(r)
        if not person_key:
            continue
        person = people.setdefault(person_key, {"ic": "", "ic_start": "", "aliases": set()})
        for alias in _role_aliases(r):
            k = normalize_key(alias)
            if k:
                person["aliases"].add(k)
        ic = str(r.get("ic_no") or "").strip()
        start = _effective_start(r.get("effective_from"))
        if ic and (not person["ic"] or start >= person["ic_start"]):
            person["ic"] = ic
            person["ic_start"] = start

    name_map = {}

    def register(alias, full_display):
        alias = str(alias or "").strip()
        if not alias:
            return
        k = normalize_key(alias)
        if k:
            name_map.setdefault(k, full_display)

    for r in latest.values():
        agent = str(r.get("agent") or "").strip()
        full = str(r.get("full_name") or "").strip()
        full_display = title_case(full if full else agent)
        # Full name first so it always resolves to itself, then the eeAdmin
        # name and every nickname variant.
        register(full_display, full_display)
        register(agent, full_display)
        for part in _nick_parts(r.get("nick_name")):
            register(part, full_display)
        if full:
            register(full, full_display)

    ic_map = {}
    for person in people.values():
        if not person["ic"]:
            continue
        for k in person["aliases"]:
            ic_map.setdefault(k, person["ic"])

    return name_map, ic_map


def _load_maps():
    global _NAME_MAP, _IC_MAP
    if _NAME_MAP is None or _IC_MAP is None:
        _NAME_MAP, _IC_MAP = _build_maps()
    return _NAME_MAP, _IC_MAP


def reset_cache():
    """Drop the cached maps so the next lookup re-reads the agent_roles table."""
    global _NAME_MAP, _IC_MAP, _ALIAS_INDEX
    _NAME_MAP = None
    _IC_MAP = None
    _ALIAS_INDEX = None


_ALIAS_INDEX = None


def _search_key(value):
    """Letters and digits only, lower case: "Caryn Dong" -> "caryndong"."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def search_aliases(name):
    """Every name the Data page stores for the person `name` belongs to, as
    search keys (see _search_key).

    The tables show one spelling -- the Full Name, "Dong Leong Moi" -- but
    people search by whichever they know, often the eeAdmin name or nickname,
    "Caryn Dong". Matching a search against all of these lets any of them
    find the row.
    """
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        index = {}
        for alias, canon in _load_maps()[0].items():
            ck = _search_key(canon)
            if not ck:
                continue
            group = index.setdefault(ck, {ck})
            ak = _search_key(alias)
            if ak:
                group.add(ak)
        _ALIAS_INDEX = index
    own = _search_key(name)
    canon = _search_key(resolve(name)) if own else ""
    keys = set(_ALIAS_INDEX.get(canon) or _ALIAS_INDEX.get(own) or ())
    if not keys and own:
        # Some screens join the nickname onto the full name -- the Sales
        # Report shows "CAROL SIOW SIO CHUI" for Siow Sio Chui -- which is no
        # stored spelling. Fall back to the person whose full name sits inside
        # it. Full names only, and long ones, so a short nickname can never
        # pull in a stranger.
        for ck, group in _ALIAS_INDEX.items():
            if len(ck) >= 8 and ck in own:
                keys |= group
    if own:
        keys.add(own)
    return sorted(keys)


def get_map():
    """Return the raw {normalized_key: full_name} lookup (for serving to clients)."""
    return dict(_load_maps()[0])


def get_ic_map():
    """Return {normalized_key: ic_no} from Agent Roles & Hierarchy, keyed on
    every name alias the Data page stores for that person."""
    return dict(_load_maps()[1])


def resolve(name):
    """Resolve a stored agent name to its canonical Title Case full name.

    Unknown names are preserved but re-cased to Title Case. Blank/None returns
    the input unchanged so totals/blank cells are never altered.
    """
    if name is None:
        return name
    text = str(name).strip()
    if not text:
        return name
    full = _load_maps()[0].get(normalize_key(text))
    return full if full else title_case(text)
