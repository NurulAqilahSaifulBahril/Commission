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


def _build_map():
    """alias -> canonical full name, from the Agent Roles & Hierarchy page.

    One row per agent wins: the latest effective_from among non-hidden rows.
    Canonical display is the row's Full Name, falling back to its Agent Name
    (from eeAdmin) when Full Name is blank. Aliases registered per agent: the
    eeAdmin name, each nickname part (compound nicks split on "/"), and the
    full name itself.
    """
    try:
        rows = _role_rows()
    except Exception:
        return {}

    latest = {}  # agent key -> row
    for r in rows:
        if r.get("hidden"):
            continue
        agent = str(r.get("agent") or "").strip()
        if not agent:
            continue
        key = normalize_key(agent)
        eff = _effective_start(r.get("effective_from"))
        prev = latest.get(key)
        if prev is None or eff > _effective_start(prev.get("effective_from")):
            latest[key] = r

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
        nick = str(r.get("nick_name") or "").strip()
        full_display = title_case(full if full else agent)
        # Full name first so it always resolves to itself, then the eeAdmin
        # name and every nickname variant.
        register(full_display, full_display)
        register(agent, full_display)
        for part in nick.split("/"):
            register(part, full_display)
        if full:
            register(full, full_display)

    return name_map


def _load_map():
    global _NAME_MAP
    if _NAME_MAP is None:
        _NAME_MAP = _build_map()
    return _NAME_MAP


def reset_cache():
    """Drop the cached map so the next lookup re-reads the agent_roles table."""
    global _NAME_MAP
    _NAME_MAP = None


def get_map():
    """Return the raw {normalized_key: full_name} lookup (for serving to clients)."""
    return dict(_load_map())


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
    full = _load_map().get(normalize_key(text))
    return full if full else title_case(text)
