"""Load Net Floor Price schedules from Excel (650W) and JSON (620W)."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from nfp_paths import get_620w_json_path, get_default_excel_path

SHEET_BY_MONTH: Dict[Tuple[int, int], str] = {
    (2025, 11): "NOV and DEC 2025",
    (2025, 12): "NOV and DEC 2025",
    (2026, 1): "JAN 2026",
    (2026, 2): "FEB 2026",
    (2026, 3): "MAC 2026",
    (2026, 4): "APR 2026",
    (2026, 5): "MAY 2026",
    (2026, 6): "JUN 2026",
    (2026, 7): "JUL 2026",
}

MONTH_NAMES = {
    1: "JAN", 2: "FEB", 3: "MAC", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"
}

NFP_CUTOFF = date(2025, 10, 1)
SCHEDULE_650W_FROM = date(2025, 11, 1)

# System price list (dashboard Data page) takes priority over the monthly Excel/
# JSON schedules. Rows live in the dashboard's SQLite DB, imported/uploaded there.
DASHBOARD_DB_PATH = Path(__file__).resolve().parent.parent.parent / "8. Web Dashboard" / "dashboard.db"
USE_DB_PRICES = True
_DB_PRICES = None  # {month: {rating: [table dicts keyed by panels, ordered by table_no]}}
# The dashboard builds the internal and outsource reports at the same time, in
# two threads sharing this cache. Loading used to publish an empty {} first and
# fill it from the database afterwards, so the second thread could arrive in
# between, read the empty list, miss every floor price and file the invoice as
# "missing_in_excel" (seen 12 Oct 2026: ~160 outsource invoices, Lim Lee Peng
# among them). The list is now built privately and published once complete.
_DB_PRICES_LOCK = threading.Lock()


def reset_db_prices_cache():
    global _DB_PRICES
    _DB_PRICES = None


def _load_db_prices():
    """Load nfp_prices into {month: {rating: [ {panels: PriceRow}, ... ]}}."""
    global _DB_PRICES
    if _DB_PRICES is not None:
        return _DB_PRICES
    with _DB_PRICES_LOCK:
        if _DB_PRICES is None:
            _DB_PRICES = _read_db_prices()
        return _DB_PRICES


def _read_db_prices():
    """The nfp_prices rows as {month: {rating: [tables]}}; {} if unreadable."""
    prices: dict = {}
    if not DASHBOARD_DB_PATH.exists():
        return prices
    _NFP_SQL = (
        # String-inverter rows only: hybrid rows are display data for the
        # dashboard and must never feed commission calculations.
        "SELECT month, panel_rating, table_no, panels, final_price, final_with_tng,"
        " promo_price, promo_from, promo_to, columns_json, source_sheet, power_system"
        " FROM nfp_prices"
        " WHERE inverter_type IS NULL OR inverter_type = '' OR LOWER(inverter_type) = 'string'"
        " ORDER BY month, panel_rating, table_no, panels"
    )
    # The dashboard writes prices to Supabase/Postgres; the local SQLite file
    # stopped being updated when that migration happened, so preferring it would
    # quietly price cases off a frozen list.
    rows = None
    try:
        import sys as _sys
        _dash = str(DASHBOARD_DB_PATH.parent)
        if _dash not in _sys.path:
            _sys.path.insert(0, _dash)
        import db as _dashboard_db
        with _dashboard_db._connect() as _conn:
            rows = [dict(r) for r in _conn.execute(_NFP_SQL).fetchall()]
    except Exception as _e:
        print(f"[NFP Prices] Warning: Postgres unavailable ({_e}); "
              f"falling back to the legacy local database.")

    if rows is None:
        try:
            import sqlite3
            conn = sqlite3.connect(str(DASHBOARD_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(_NFP_SQL).fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[NFP] Warning: could not read nfp_prices from dashboard DB: {e}")
            return prices
    for r in rows:
        month = str(r["month"])
        rating = int(r["panel_rating"])
        table_no = int(r["table_no"])
        tables = prices.setdefault(month, {}).setdefault(rating, {})
        promo_price = r["promo_price"] if "promo_price" in r.keys() else None
        # Keyed on the phase too: a single- and a three-phase table sharing a
        # table number must never merge (see parse_schedule_workbook_rows).
        # Three-phase sorts first, as pick_table_for_panels expects.
        phase = str(r["power_system"] or "") if "power_system" in r.keys() else ""
        tkey = (table_no, 0 if phase == "three" else 1)
        tables.setdefault(tkey, {})[int(r["panels"])] = PriceRow(
            int(r["panels"]), float(r["final_price"]),
            float(r["final_with_tng"]) if r["final_with_tng"] is not None else None,
            float(promo_price) if promo_price is not None else None,
            _parse_iso_date(r["promo_from"]) if promo_price is not None else None,
            _parse_iso_date(r["promo_to"]) if promo_price is not None else None,
            _row_columns(r["columns_json"] if "columns_json" in r.keys() else None),
            str(r["source_sheet"] or "") if "source_sheet" in r.keys() else "",
        )
    for month, ratings in prices.items():
        for rating in list(ratings.keys()):
            ratings[rating] = [ratings[rating][k] for k in sorted(ratings[rating].keys())]
    return prices


def _parse_iso_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _row_columns(raw) -> tuple:
    """A price row's stored sheet columns as ((label, amount), ...); () when the
    row carries none or they cannot be read."""
    if not raw:
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return ()
    out = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((" ".join(str(item[0]).split()), item[1]))
    return tuple(out)


# ---------------------------------------------------------------------------
# Rebates the invoice actually carries
# ---------------------------------------------------------------------------
# The price list's "Max amount after discount" takes EVERY rebate column off the
# price after the max discount, for every invoice. A customer who never received
# one of those rebates (no CNY promo line on the invoice, say) was still being
# measured against a floor that assumed it. Decided with Finance 13 Oct 2026:
# the floor takes off only the rebates the invoice itself carries, e.g. Sim Chee
# Geng, 25 panels, Feb 2026: 38,850.75 - 1,000 (Xiaomi 55" TV Cash Option) -
# 3,000 (Atap) - 900 (Ang Pow) = 33,950.75, without the sheet's 1,500 CNY promo.
#
# A rebate is "carried" when the invoice has a negative line worded like it. The
# amount taken off is the sheet's own (it already varies with the panel count).
_INVOICE_REBATE_PATTERNS = {
    # Free gift taken as cash: "No TV swap discount ...", "Xiaomi 55" TV. Cash
    # Option", "No IMILAB V1 Robot swap discount ...". Road-show "swap TNG" lines
    # are the TNG rebate, which has its own flag.
    "gift": re.compile(r"(?=.*\b(?:tv|robot|imilab|xiaomi)\b)(?=.*(?:swap|cash option))|no\s+xiaomi\s+robot",
                       re.I | re.S),
    # "Pre-Atap" and "Pre-order" rebates are the Nov-Dec 2025 Atap-type rebate.
    "atap": re.compile(r"atap|solaris|pre-?\s*order", re.I),
    # Billed as "Ang Pow rebate" (also spelt "angpao") and, in March, as "Duit
    # Raya rebate".
    "angpow": re.compile(r"ang\s*pow|ang\s*pao|duit\s*raya", re.I),
    # The sheet's "马上装 马上赚 CNY2026 Promo Reward" is billed as "CNY 2026
    # Promo Discount (Panel Qty: N)" in Feb-Mar and "Earn Now Rebate" later.
    "earnnow": re.compile(r"earn\s*now|cny\s*\d*\s*promo|马上装", re.I),
    # The sheet's "RAYA Promo Reward (1MAR~31MAR 2026)" is billed as "Holiday
    # Boost Reward (Panel Qty: N)".
    "raya": re.compile(r"holiday\s*boost|raya\s*promo", re.I),
    "earth": re.compile(r"earth\s*month|go\s*green|地球", re.I),
}

# Wordings that name no rebate but are one of two, told apart by the amount: the
# Ang Pow rebate is only ever RM500 or RM900, the Atap rebate is larger
# ("The RM900 rebate is limited to customers who sign ...", "Early Bird").
_AMBIGUOUS_REBATE = re.compile(r"rebate is limited|early\s*bird", re.I)
_ANG_POW_MAX = 1000.0


def _line_kind(description: str, amount: float) -> Optional[str]:
    """The rebate kind one negative invoice line stands for, or None."""
    for kind, pattern in _INVOICE_REBATE_PATTERNS.items():
        if pattern.search(description):
            return kind
    if _AMBIGUOUS_REBATE.search(description):
        return "angpow" if abs(amount) <= _ANG_POW_MAX else "atap"
    return None

# Sheets that label their rebate columns differently (the Nov-Dec 2025 and Jan
# 2026 620W table calls its Atap and Ang Pow columns SOLARIS and EARLY BIRD
# DISCOUNT) take every column between the price after max discount and the final
# price, whatever it is called.
_ALL_COLUMNS_SHEETS = ("nov and dec 2025", "jan 2026")

_REBATE_COLUMN_KEYS = ("freegift", "atap", "ang pow", "rebate", "promo", "reward",
                       "raya", "earth", "cny", "地球")
_NON_REBATE_COLUMN_WORDS = ("no.panels", "price package", "max discount", "price after max",
                            "final price", "roadshow tng", "with tng")


def _negative_lines(price_items):
    """(amount, description) for each negative invoice line, counting an exact
    repeat (same amount, same wording) once. Some invoices carry every line
    twice under different ids -- Lee Chui Yin's 1008554 -- and summing those
    would double her discount."""
    if isinstance(price_items, str):
        try:
            price_items = json.loads(price_items)
        except ValueError:
            price_items = []
    seen = set()
    out = []
    for item in price_items if isinstance(price_items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            amount = float(str(item.get("amount") if item.get("amount") is not None else 0).replace(",", ""))
        except ValueError:
            continue
        if amount >= 0:
            continue
        description = str(item.get("description") or "")
        key = (round(amount, 2), " ".join(description.split()).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((amount, description))
    return out


class RebateKinds(frozenset):
    """The rebate kinds an invoice carries, plus `plain_discount`: what its
    ordinary "Discount" lines come to (CEO / manager parts excluded). That
    discount stands in for sheet rebates the invoice does not name -- see
    invoice_floor_price."""
    plain_discount: float = 0.0


_PLAIN_DISCOUNT = re.compile(r"discount", re.I)
# How close an invoice's plain discount must be to the total of the sheet
# rebates it lacks to stand in for all of them ("the value range is not too
# far"), as a share of that total.
_DISCOUNT_COVER_TOLERANCE = 0.30


def invoice_rebate_kinds(price_items) -> frozenset:
    """Which rebates an invoice carries, from its negative lines. `price_items`
    is the invoice's lines as the reports select them (a JSON array of
    {amount, description, ...}, or the parsed list)."""
    kinds = set()
    plain = 0.0
    for amount, description in _negative_lines(price_items):
        kind = _line_kind(description, amount)
        if kind:
            kinds.add(kind)
        elif _PLAIN_DISCOUNT.search(description):
            plain += max(0.0, -amount - _approved_part(description, amount))
    result = RebateKinds(kinds)
    result.plain_discount = round(plain, 2)
    return result


# A discount the CEO or a manager approved is the company's own, so it lowers the
# floor by the same amount (an ordinary discount is the agent's and does not).
# Decided with Finance 13 Oct 2026, e.g. Liew Miu Yoong, 36 panels, Nov 2025:
# 50,931 - 1,000 (TV swap) - 900 (rebate) - 4,865 (CEO discount) = 44,166.
# Manager Discount (June 2026 on) is treated the same way.
_APPROVER_WORD = re.compile(r"ceo|manager", re.I)
_RM_AMOUNT = re.compile(r"rm\s*([\d,]+(?:\.\d+)?)", re.I)


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _approved_part(description: str, amount: float) -> float:
    """The CEO / manager discount inside one negative invoice line, as a positive
    amount.

    A line that is only that discount ("CEO Discount", "CEO Special Discount",
    "Manager Discount (RM 4000)") gives its whole amount. In a combined line the
    part naming the approver gives the RM figure written after it ("... CEO
    Discount (RM1969.40)", "MANAGER DISCOUNT (RM 4093) | Discount (RM 1876)");
    with no figure it gives whatever the other parts do not account for, and the
    ordinary "Discount (6% RM2340.60)" or Opening Day parts stay out of it."""
    if not _APPROVER_WORD.search(description):
        return 0.0
    lines = [p for p in re.split(r"[\r\n]+", description) if p.strip()]
    if len(lines) <= 1:
        cut = _APPROVER_WORD.search(description).start()
        lines = [p for p in (description[:cut], description[cut:]) if p.strip()]
    approved_text = " ".join(p for p in lines if _APPROVER_WORD.search(p))
    other_text = " ".join(p for p in lines if not _APPROVER_WORD.search(p))
    after_word = approved_text[_APPROVER_WORD.search(approved_text).start():]
    stated = _RM_AMOUNT.search(after_word)
    if stated:
        return min(_money(stated.group(1)), -amount)
    others = sum(_money(m.group(1)) for m in _RM_AMOUNT.finditer(other_text))
    return max(0.0, -amount - others)


def invoice_approved_discount(price_items) -> float:
    """The CEO / manager-approved discount an invoice carries, from its negative
    lines."""
    total = 0.0
    for amount, description in _negative_lines(price_items):
        total += _approved_part(description, amount)
    return round(total, 2)


def _column_kind(label: str) -> Optional[str]:
    """The rebate kind a sheet column stands for, or None when it is not one this
    module can recognise on an invoice (such a column is always taken off, as it
    always was)."""
    low = label.lower()
    if "freegift" in low:
        return "gift"
    if "atap" in low or "solaris" in low:
        return "atap"
    if "ang pow" in low or "early bird" in low:
        return "angpow"
    if "raya" in low:
        return "raya"
    if "earth" in low or "地球" in low:
        return "earth"
    if "cny" in low or "马上装" in low:
        return "earnnow"
    return None


def invoice_floor_price(row: "PriceRow", has_tng_rebate: bool, kinds) -> Optional[float]:
    """The floor price for one invoice: the row's price after the max discount,
    less only the rebate columns whose kind the invoice carries (and the TNG
    rebate when it has one). None when the row has no columns to rebuild from,
    in which case the caller keeps the stored price.

    With every rebate present this is exactly the stored price, rounded the way
    the sheet's own parser rounds it."""
    if kinds is None or not row.columns:
        return None
    labels = [lab for lab, _ in row.columns]
    low = [lab.lower() for lab in labels]
    i_after = next((i for i, l in enumerate(low) if "price after max" in l), None)
    if i_after is None:
        return None

    def is_final(l: str) -> bool:
        return (("final price" in l or "max amount after discount" in l or "nett price after discount" in l)
                and not any(w in l for w in ("tng", "with", "roadshow")))

    i_final = next((i for i, l in enumerate(low) if i > i_after and is_final(l)), None)
    if i_final is None:
        return None

    sheet = row.sheet or ""
    # To the cent, like the price list itself (19,565.75), not the 1-decimal
    # schedule rounding, which turned a rebuilt floor into 19,565.6.
    rnd = lambda v: round_rm(v, 2)
    try:
        after = rnd(row.columns[i_after][1])
    except (TypeError, ValueError):
        return None
    every_column = sheet.strip().lower() in _ALL_COLUMNS_SHEETS
    rebate_columns = []          # (kind, amount) for each rebate column on the row
    for j in range(i_after + 1, i_final):
        l = low[j]
        if not l or any(w in l for w in _NON_REBATE_COLUMN_WORDS):
            continue
        if not every_column and not any(k in l for k in _REBATE_COLUMN_KEYS):
            continue
        amount = row.columns[j][1]
        if not isinstance(amount, (int, float)):
            continue
        rebate_columns.append((_column_kind(labels[j]), amount))

    # An ordinary "Discount" on the invoice can stand in for sheet rebates it
    # does not name (decided with Finance 13 Oct 2026) -- but it is usually the
    # agent's percentage discount, which the price after max discount already
    # allows for. So it covers the missing rebate(s) only when its amount is
    # nearer to them than to the row's max percentage discount:
    #   Lee Chui Yin  Discount 1,284 vs CNY 1,500 / 6% 2,321  -> the CNY promo
    #   Sim Chee Geng Discount 2,575 vs CNY 1,500 / 7% 2,924  -> the 7%, CNY stays on
    # One missing rebate is matched on its own amount; several together when
    # the discount is near their total (within _DISCOUNT_COVER_TOLERANCE), else
    # only the one nearest it.
    covered = set()
    plain = float(getattr(kinds, "plain_discount", 0.0) or 0.0)
    missing = [(k, a) for k, a in rebate_columns if k is not None and k not in kinds]
    if plain > 0 and missing:
        missing_total = sum(a for _, a in missing)
        if len(missing) == 1 or abs(plain - missing_total) <= _DISCOUNT_COVER_TOLERANCE * missing_total:
            candidate, target = {k for k, _ in missing}, missing_total
        else:
            nearest = min(missing, key=lambda m: abs(m[1] - plain))
            candidate, target = {nearest[0]}, nearest[1]
        i_pkg = next((i for i, l in enumerate(low) if l.endswith("price package")), None)
        pkg = row.columns[i_pkg][1] if i_pkg is not None else None
        pct_discount = (pkg - row.columns[i_after][1]) if isinstance(pkg, (int, float)) else None
        if pct_discount is None or abs(plain - target) < abs(plain - pct_discount):
            covered = candidate

    # The invoice carries every discount the list allows for: the floor is the
    # list's own price, exactly as stored (rule 1, 13 Oct 2026). Only an invoice
    # short of some is rebuilt from the price after the max discount (rule 2).
    if not any(k is not None and k not in kinds and k not in covered for k, _ in rebate_columns):
        return None

    taken = 0.0
    for kind, amount in rebate_columns:
        if kind is None or kind in kinds or kind in covered:
            taken += rnd(amount)
    floor = rnd(after - taken)
    if has_tng_rebate:
        i_tng = next((i for i, l in enumerate(low)
                      if ("roadshow" in l and "tng" in l and "after" not in l) or "tng rebate" in l), None)
        tng = row.columns[i_tng][1] if i_tng is not None else None
        if isinstance(tng, (int, float)):
            floor = rnd(floor - rnd(tng))
        elif row.final_with_tng is not None:
            floor = rnd(floor - (row.final_price - row.final_with_tng))
    return floor


def _db_lookup_net_floor_price(
    invoice_date: date, panel_qty: int, panel_rating: int,
    has_tng_rebate: bool, three_phase: bool, rebate_kinds=None
) -> Tuple[Optional[float], Optional[str]]:
    """DB-first price lookup mirroring the Excel-schedule behavior exactly:
    invoice month first, then nearest earlier months, then later months.
    Returns (None, None) on any miss so callers fall back to the Excel path."""
    data = _load_db_prices()
    if not data:
        return None, None

    # Pre-Nov-2025 (Oct): only the 620W list applies, no month fallback
    if invoice_date < SCHEDULE_650W_FROM:
        if panel_rating and canonical_panel_rating(panel_rating) == 620:
            tables = data.get("2025-10", {}).get(620, [])
            for table in tables:
                if panel_qty in table:
                    return table[panel_qty].final_price, "db_620w_oct_2025"
        return None, None

    target_rating = canonical_panel_rating(panel_rating or 650)
    inv_ym = f"{invoice_date.year:04d}-{invoice_date.month:02d}"
    # Exclude the Oct-2025 JSON month from schedule fallback (the Excel path
    # never falls back to it either)
    months = sorted(m for m in data.keys() if m >= "2025-11")
    ordered = [inv_ym] if inv_ym in data else []
    for m in reversed([x for x in months if x < inv_ym]):
        if m not in ordered:
            ordered.append(m)
    for m in [x for x in months if x > inv_ym]:
        if m not in ordered:
            ordered.append(m)

    # A month that lists the panel's own wattage (e.g. 625W from Sep 2026)
    # prices it from that list; otherwise the 590/620/650 bucket as before.
    exact_rating = int(panel_rating) if panel_rating else None
    for m in ordered:
        month_data = data.get(m, {})
        rating_used = target_rating
        if exact_rating and exact_rating != target_rating and month_data.get(exact_rating):
            rating_used = exact_rating
        # The invoice's own month follows its Google Sheet tab even when the tab
        # has no list for this wattage (user, 27 Oct 2026): a 620W job in Feb-Aug
        # 2026, whose tabs list only 650W, is priced off that month's 650W list
        # rather than the last 620W list (Jan 2026).
        if m == inv_ym and not month_data.get(rating_used) and month_data.get(650):
            rating_used = 650
        tables = month_data.get(rating_used, [])
        if not tables:
            continue
        if len(tables) > 1 and (target_rating == 650 or rating_used != target_rating):
            table = pick_table_for_panels(tables, panel_qty, three_phase=three_phase)
        else:
            table = None
            for t in tables:
                if panel_qty in t:
                    table = t
                    break
            if table is None:
                table = tables[0]
        if not table or panel_qty not in table:
            continue
        row = table[panel_qty]
        price = (
            row.final_with_tng
            if has_tng_rebate and row.final_with_tng is not None
            else row.final_price
        )
        suffix = "_with_tng" if has_tng_rebate and row.final_with_tng is not None else ""
        # Only the rebates this invoice carries come off the price after the
        # max discount; a row with no sheet columns keeps its stored price.
        invoice_price = invoice_floor_price(row, has_tng_rebate, rebate_kinds)
        if invoice_price is not None:
            price = invoice_price
            suffix += "_invoice_rebates"
        if (m == inv_ym and row.promo_price is not None and row.promo_from and row.promo_to
                and row.promo_from <= invoice_date <= row.promo_to):
            price = row.promo_price
            suffix = f"_promo_{row.promo_from:%d%b}-{row.promo_to:%d%b}".lower()
        if rating_used != target_rating:
            suffix += f"_{rating_used}w"
        if m == inv_ym:
            source = f"db_{m}{suffix}"
        else:
            source = f"db_{m}_fallback_{target_rating}w"
        return price, source
    return None, None


@dataclass(frozen=True)
class PriceRow:
    panels: int
    final_price: float
    final_with_tng: Optional[float]
    # Dated promo inside the month: invoices dated promo_from..promo_to
    # (inclusive) use promo_price instead of final_price.
    promo_price: Optional[float] = None
    promo_from: Optional[date] = None
    promo_to: Optional[date] = None
    # The sheet row this price was built from, as ((label, amount), ...), and the
    # sheet's name. Lets the floor be rebuilt from only the rebates an invoice
    # actually carries (see invoice_floor_price); empty for rows stored without it.
    columns: tuple = ()
    sheet: str = ""


def _clean_money_val(value: Any) -> float | int | Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", "—"):
        return None
    s = s.replace("RM", "").replace("rm", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def round_rm(value: float | int | str | Decimal, places: int = 2) -> float:
    """Round money to N decimal places (half up). Used for commission amounts."""
    cleaned = _clean_money_val(value)
    if cleaned is None:
        return 0.0
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(cleaned)).quantize(q, rounding=ROUND_HALF_UP))


def _is_feb_schedule_sheet(sheet_name: str) -> bool:
    """650W Excel sheets whose name includes FEB (e.g. FEB 2026) use 2-decimal half-up."""
    return "feb" in sheet_name.lower()


def schedule_amount_round(value: float | int | str | Decimal, sheet_name: str) -> float:
    """
    A schedule amount as the price list shows it: half-up to the sen.

    Every sheet keeps its cents (user, 27 Oct 2026: the finance Google Sheet
    is the source, so 15,827.55 stays 15,827.55). Until then only FEB sheets
    did; the others went through round_nfp's 1-decimal rule (15,827.4).
    """
    return round_rm(value, 2)


def round_nfp(value: float | int | str | Decimal) -> float:
    """
    Net floor price rounding: **1 decimal place** (RM 0.10), not 2 decimals.

    Example: 20,135.75 -> 20,135.6
    - Truncate to 1 decimal (20,135.7)
    - If the 2nd decimal (sen) is 5–9, reduce by RM 0.10 (20,135.6)
    """
    cleaned = _clean_money_val(value)
    if cleaned is None:
        return 0.0
    # First, round float inputs to 2 decimal places to avoid float precision loss (e.g. 31869.199999999997 -> 31869.20)
    rounded_val = round(float(cleaned), 2)
    d = Decimal(str(rounded_val))
    truncated = d.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    hundredths = int((abs(d) * 100) % 10)
    if hundredths >= 5:
        truncated -= Decimal("0.1")
    return float(truncated)


def _header_index(headers: List[str], *needles: str, exclude: Tuple[str, ...] = ()) -> Optional[int]:
    for j, h in enumerate(headers):
        hl = h.lower()
        if any(ex in hl for ex in exclude):
            continue
        if all(n in hl for n in needles):
            return j
    return None


def _rebate_column_indices(
    headers: List[str], col_after_max: int, col_final: int
) -> List[int]:
    """Columns between Price after Max % Discount and FINAL (rebates / gifts)."""
    rebate_keys = (
        "freegift",
        "atap",
        "ang pow",
        "rebate",
        "promo",
        "reward",
        "raya",
        "earth",
        "cny",
        "地球",
        "cashback",
        "parent",
    )
    skip = (
        "no.panels",
        "price package",
        "max discount",
        "price after max",
        "final price",
        "roadshow tng",
        "with tng",
    )
    indices: List[int] = []
    lo, hi = min(col_after_max, col_final), max(col_after_max, col_final)
    for j in range(lo + 1, hi):
        hl = headers[j].lower()
        if not hl or any(s in hl for s in skip):
            continue
        if any(k in hl for k in rebate_keys):
            indices.append(j)
    return indices


def _parse_sheet_tables(df: pd.DataFrame, sheet_name: str) -> List[Dict[int, PriceRow]]:
    tables: List[Dict[int, PriceRow]] = []
    n = len(df)
    sn = sheet_name.upper().strip()

    # September 2026 layout (3P is Table 1, 1P is Table 2)
    if ("SEPT" in sn or "SEP 2026" in sn) and any(x in sn for x in ("HYB", "STR")):
        t1_3p: Dict[int, PriceRow] = {}
        t2_1p: Dict[int, PriceRow] = {}
        for r_idx in range(2, n):
            r = df.iloc[r_idx]
            if pd.isna(r.iloc[0]):
                continue
            try:
                panels = int(float(r.iloc[0]))
            except (ValueError, TypeError):
                continue
            pkg_name = str(r.iloc[1]) if pd.notna(r.iloc[1]) else ""
            final_price = schedule_amount_round(r.iloc[4], sheet_name)
            promo_val = r.iloc[6] if len(r) > 6 and pd.notna(r.iloc[6]) else None
            promo_price = schedule_amount_round(promo_val, sheet_name) if promo_val is not None else None
            pr = PriceRow(
                panels=panels, final_price=final_price, final_with_tng=None,
                promo_price=promo_price,
                promo_from=date(2026, 9, 18) if promo_price is not None else None,
                promo_to=date(2026, 9, 30) if promo_price is not None else None,
            )
            if "[1P]" in pkg_name or "1P" in pkg_name:
                t2_1p[panels] = pr
            else:
                t1_3p[panels] = pr
        res = []
        if t1_3p:
            res.append(t1_3p)
        if t2_1p:
            res.append(t2_1p)
        return res

    # 590W split layout (590W NOV 2025, 590W DEC 2025, 590W JAN 2026)
    if "590W" in sn and ("NOV" in sn or "DEC" in sn or "JAN" in sn):
        t_rows: Dict[int, PriceRow] = {}
        col_final = None
        for j in range(len(df.columns)):
            h = str(df.iloc[1, j]).lower() if pd.notna(df.iloc[1, j]) else ""
            if "final price" in h:
                col_final = j
                break
        if col_final is None:
            col_final = 6
        for r_idx in range(2, n):
            r = df.iloc[r_idx]
            if pd.isna(r.iloc[0]):
                continue
            panels = 8 + (r_idx - 2)
            if col_final < len(r) and pd.notna(r.iloc[col_final]):
                fp = schedule_amount_round(r.iloc[col_final], sheet_name)
                t_rows[panels] = PriceRow(panels=panels, final_price=fp, final_with_tng=None)
        if t_rows:
            return [t_rows]

    i = 0
    while i < n:
        row = df.iloc[i]
        row_text = " ".join(str(v).lower() for v in row if pd.notna(v))
        if "no.panels" not in row_text and "no panels" not in row_text:
            i += 1
            continue

        headers = [str(v).strip() if pd.notna(v) else "" for v in row]

        # Side-by-side layout for Nov-Dec 2025 and Jan 2026 (Table 2 shares panels col with Table 1)
        if sheet_name in ("NOV and DEC 2025", "JAN 2026"):
            t1_after_max = 3
            t1_rebate_cols = [4, 5, 6]
            
            t2_after_max = 17
            t2_rebate_cols = [18, 19, 20]
            
            t1_rows: Dict[int, PriceRow] = {}
            t2_rows: Dict[int, PriceRow] = {}
            
            i += 1
            while i < n:
                r = df.iloc[i]
                if pd.isna(r.iloc[0]):
                    break
                try:
                    panels = int(float(r.iloc[0]))
                except (TypeError, ValueError):
                    break
                    
                def rnd(v: float | int | str | Decimal) -> float:
                    return schedule_amount_round(v, sheet_name)
                    
                if pd.notna(r.iloc[t1_after_max]):
                    t1_after = rnd(r.iloc[t1_after_max])
                    t1_rebs = sum(rnd(r.iloc[j]) for j in t1_rebate_cols if j < len(r) and pd.notna(r.iloc[j]))
                    t1_price = rnd(t1_after - t1_rebs)
                    t1_rows[panels] = PriceRow(panels, t1_price, None)
                    
                if pd.notna(r.iloc[t2_after_max]):
                    t2_after = rnd(r.iloc[t2_after_max])
                    t2_rebs = sum(rnd(r.iloc[j]) for j in t2_rebate_cols if j < len(r) and pd.notna(r.iloc[j]))
                    t2_price = rnd(t2_after - t2_rebs)
                    t2_rows[panels] = PriceRow(panels, t2_price, None)
                    
                i += 1
                
            if t1_rows:
                tables.append(t1_rows)
            if t2_rows:
                tables.append(t2_rows)
            continue

        # Check for dynamic side-by-side layout (e.g. MAY 2026 which has a second 'no.panels' column)
        col_panels_2 = None
        for idx in range(10, len(headers)):
            if "no.panels" in headers[idx].lower():
                col_panels_2 = idx
                break

        if col_panels_2 is not None:
            col_panels_1 = 0
            col_after_max_1 = _header_index(headers[:col_panels_2], "price after max")
            col_final_1 = _header_index(headers[:col_panels_2], "final price", exclude=("tng", "with", "roadshow")) or \
                          _header_index(headers[:col_panels_2], "max amount after discount", exclude=("tng", "with", "roadshow")) or \
                          _header_index(headers[:col_panels_2], "nett price after discount")
            # JUN 2026+ layout ("Nett Price After Discount") has no separate
            # "price after max" column to subtract rebates from -- that column
            # already IS the final pre-TNG price, so treat it as both ends of
            # the (now empty) rebate range.
            if col_after_max_1 is None and col_final_1 is not None:
                col_after_max_1 = col_final_1
            col_tng_rebate_1 = _header_index(headers[:col_panels_2], "roadshow", "tng") or _header_index(headers[:col_panels_2], "tng rebate")
            col_with_tng_1 = _header_index(headers[:col_panels_2], "with tng") or \
                             _header_index(headers[:col_panels_2], "max amount after discount", "roadshow")

            rebate_cols_1 = []
            if col_after_max_1 is not None and col_final_1 is not None:
                rebate_cols_1 = _rebate_column_indices(headers[:col_panels_2], col_after_max_1, col_final_1)

            col_panels_2_rel = 0
            col_after_max_2_rel = _header_index(headers[col_panels_2:], "price after max")
            col_final_2_rel = _header_index(headers[col_panels_2:], "final price", exclude=("tng", "with", "roadshow")) or \
                              _header_index(headers[col_panels_2:], "max amount after discount", exclude=("tng", "with", "roadshow")) or \
                              _header_index(headers[col_panels_2:], "nett price after discount")
            if col_after_max_2_rel is None and col_final_2_rel is not None:
                col_after_max_2_rel = col_final_2_rel
            col_tng_rebate_2_rel = _header_index(headers[col_panels_2:], "roadshow", "tng") or _header_index(headers[col_panels_2:], "tng rebate")
            col_with_tng_2_rel = _header_index(headers[col_panels_2:], "with tng") or \
                                 _header_index(headers[col_panels_2:], "max amount after discount", "roadshow")

            rebate_cols_2_rel = []
            if col_after_max_2_rel is not None and col_final_2_rel is not None:
                rebate_cols_2_rel = _rebate_column_indices(headers[col_panels_2:], col_after_max_2_rel, col_final_2_rel)
                
            t1_rows: Dict[int, PriceRow] = {}
            t2_rows: Dict[int, PriceRow] = {}
            
            def rnd(v: float | int | str | Decimal) -> float:
                return schedule_amount_round(v, sheet_name)
                
            i += 1
            while i < n:
                r = df.iloc[i]
                
                p1_ok = False
                p2_ok = False
                
                # Parse Table 1 row
                try:
                    if col_panels_1 < len(r) and pd.notna(r.iloc[col_panels_1]):
                        panels_1 = int(float(r.iloc[col_panels_1]))
                        p1_ok = True
                        if col_after_max_1 is not None and col_after_max_1 < len(r) and pd.notna(r.iloc[col_after_max_1]):
                            after_max_1 = rnd(r.iloc[col_after_max_1])
                            rebs_1 = sum(rnd(r.iloc[j]) for j in rebate_cols_1 if j < len(r) and pd.notna(r.iloc[j]))
                            final_price_1 = rnd(after_max_1 - rebs_1)
                            
                            final_with_tng_1 = None
                            if col_tng_rebate_1 is not None and col_tng_rebate_1 < len(r) and pd.notna(r.iloc[col_tng_rebate_1]):
                                final_with_tng_1 = rnd(final_price_1 - rnd(r.iloc[col_tng_rebate_1]))
                            elif col_with_tng_1 is not None and col_with_tng_1 < len(r) and pd.notna(r.iloc[col_with_tng_1]):
                                final_with_tng_1 = rnd(r.iloc[col_with_tng_1])
                                
                            t1_rows[panels_1] = PriceRow(panels_1, final_price_1, final_with_tng_1)
                except (ValueError, TypeError):
                    pass
                        
                # Parse Table 2 row
                try:
                    if col_panels_2 < len(r) and pd.notna(r.iloc[col_panels_2]):
                        panels_2 = int(float(r.iloc[col_panels_2]))
                        p2_ok = True
                        if col_after_max_2_rel is not None:
                            idx_after_max_2 = col_panels_2 + col_after_max_2_rel
                            if idx_after_max_2 < len(r) and pd.notna(r.iloc[idx_after_max_2]):
                                after_max_2 = rnd(r.iloc[idx_after_max_2])
                                rebs_2 = sum(rnd(r.iloc[col_panels_2 + j]) for j in rebate_cols_2_rel if (col_panels_2 + j) < len(r) and pd.notna(r.iloc[col_panels_2 + j]))
                                final_price_2 = rnd(after_max_2 - rebs_2)
                                
                                final_with_tng_2 = None
                                if col_tng_rebate_2_rel is not None:
                                    idx_tng_2 = col_panels_2 + col_tng_rebate_2_rel
                                    if idx_tng_2 < len(r) and pd.notna(r.iloc[idx_tng_2]):
                                        final_with_tng_2 = rnd(final_price_2 - rnd(r.iloc[idx_tng_2]))
                                elif col_with_tng_2_rel is not None:
                                    idx_wtng_2 = col_panels_2 + col_with_tng_2_rel
                                    if idx_wtng_2 < len(r) and pd.notna(r.iloc[idx_wtng_2]):
                                        final_with_tng_2 = rnd(r.iloc[idx_wtng_2])
                                        
                                t2_rows[panels_2] = PriceRow(panels_2, final_price_2, final_with_tng_2)
                except (ValueError, TypeError):
                    pass
                
                if not p1_ok and not p2_ok:
                    break
                    
                i += 1
                
            if t1_rows:
                tables.append(t1_rows)
            if t2_rows:
                tables.append(t2_rows)
            continue

        col_panels = _header_index(headers, "no.panels") or 0
        col_after_max = _header_index(headers, "price after max")
        col_final = _header_index(headers, "final price", exclude=("tng", "with", "roadshow")) or \
                    _header_index(headers, "max amount after discount", exclude=("tng", "with", "roadshow")) or \
                    _header_index(headers, "nett price after discount")
        if col_after_max is None and col_final is not None:
            col_after_max = col_final
        col_tng_rebate = _header_index(headers, "roadshow", "tng") or _header_index(
            headers, "tng rebate"
        )
        col_with_tng = _header_index(headers, "with tng") or \
                       _header_index(headers, "max amount after discount", "roadshow")

        if col_after_max is None or col_final is None:
            i += 1
            continue

        rebate_cols = _rebate_column_indices(headers, col_after_max, col_final)

        rows: Dict[int, PriceRow] = {}
        i += 1
        while i < n:
            r = df.iloc[i]
            if pd.isna(r.iloc[col_panels]):
                break
            try:
                panels = int(float(r.iloc[col_panels]))
            except (TypeError, ValueError):
                break

            if pd.isna(r.iloc[col_after_max]):
                i += 1
                continue

            # FEB sheets: half-up to 2 decimals; other sheets: 1-decimal NFP rule.
            def rnd(v: float | int | str | Decimal) -> float:
                return schedule_amount_round(v, sheet_name)

            after_max = rnd(r.iloc[col_after_max])
            rebates = sum(
                rnd(r.iloc[j])
                for j in rebate_cols
                if j < len(r) and pd.notna(r.iloc[j])
            )
            final_price = rnd(after_max - rebates)

            final_with_tng: Optional[float] = None
            if col_tng_rebate is not None and col_tng_rebate < len(r):
                tng_val = r.iloc[col_tng_rebate]
                if pd.notna(tng_val):
                    final_with_tng = rnd(final_price - rnd(tng_val))
            elif col_with_tng is not None and pd.notna(r.iloc[col_with_tng]):
                final_with_tng = rnd(r.iloc[col_with_tng])

            rows[panels] = PriceRow(panels, final_price, final_with_tng)
            i += 1

        if rows:
            tables.append(rows)
    return tables


def load_650w_schedules(
    excel_path: Optional[Path] = None,
) -> Dict[str, List[Dict[int, PriceRow]]]:
    import shutil
    import tempfile
    path = excel_path or get_default_excel_path()
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(path, tmp_path)
        xls = pd.ExcelFile(tmp_path)
        out: Dict[str, List[Dict[int, PriceRow]]] = {}
        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet, header=None)
            out[sheet] = _parse_sheet_tables(df, sheet)
        return out
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _find_all_headers(headers: List[str], *needles: str, exclude: Tuple[str, ...] = ()) -> List[int]:
    idxs: List[int] = []
    for j, h in enumerate(headers):
        hl = h.lower()
        if any(ex in hl for ex in exclude):
            continue
        if all(n in hl for n in needles):
            idxs.append(j)
    return idxs


def _display_scan_sheet(df: pd.DataFrame, sheet_name: str) -> List[dict]:
    """Best-effort supplementary scan of a schedule sheet for DISPLAY columns:
    package selling price, roadshow TNG rebate, hybrid-inverter sub-tables and
    the power system (single/three phase) named in the table title. One dict per
    table region, in the same scan order as _parse_sheet_tables. Sheets whose
    layout doesn't fit return regions that simply carry None values."""
    n = len(df)
    regions: List[dict] = []
    i = 0
    while i < n:
        row = df.iloc[i]
        row_text = " ".join(str(v).lower() for v in row if pd.notna(v))
        if "no.panels" not in row_text and "no panels" not in row_text:
            i += 1
            continue
        headers = [str(v).strip() if pd.notna(v) else "" for v in row]
        panel_cols = [j for j, h in enumerate(headers) if "no.panels" in h.lower() or "no panels" in h.lower()]
        bounds = panel_cols + [len(headers)]
        header_row = i

        region_specs = []
        for r_idx in range(len(panel_cols)):
            lo, hi = bounds[r_idx], bounds[r_idx + 1]
            reg_headers = headers[lo:hi]
            pkg = _find_all_headers(reg_headers, "package selling") or \
                  _find_all_headers(reg_headers, "price package") or \
                  _find_all_headers(reg_headers, "now 650")
            after = _find_all_headers(reg_headers, "nett price after discount", exclude=("tng", "with", "成本")) or \
                    _find_all_headers(reg_headers, "final price after discount", exclude=("tng", "with", "成本"))
            tngr = _find_all_headers(reg_headers, "roadshow", "tng")
            wtng = _find_all_headers(reg_headers, "with tng")
            # A genuine hybrid sub-table repeats the same nett-price header verbatim;
            # anything else (e.g. internal cost columns) is not hybrid data.
            hybrid_ok = (
                len(after) >= 2
                and (
                    reg_headers[after[0]].strip().lower() == reg_headers[after[1]].strip().lower()
                    or ("string" in reg_headers[after[0]].lower() and "hyb" in reg_headers[after[1]].lower())
                )
            )

            # Table title 1-3 rows above, within this region's columns
            power_system = None
            for up in range(1, 4):
                if header_row - up < 0:
                    break
                title = " ".join(
                    str(v).lower() for v in df.iloc[header_row - up, lo:hi] if pd.notna(v)
                )
                if "three phase" in title:
                    power_system = "three"
                    break
                if "single phase" in title:
                    power_system = "single"
                    break

            labels = [
                (j, " ".join(str(reg_headers[j]).split()))
                for j in range(1, len(reg_headers)) if reg_headers[j].strip()
            ]
            region_specs.append({
                "lo": lo, "pkg": pkg, "after": after, "tngr": tngr, "wtng": wtng,
                "power_system": power_system, "labels": labels, "hybrid_ok": hybrid_ok,
                "string": {}, "hybrid": {}, "raw": {},
            })

        # Walk data rows once for all regions of this header block
        i += 1
        while i < n:
            r = df.iloc[i]
            any_ok = False
            for spec in region_specs:
                lo = spec["lo"]
                try:
                    if lo >= len(r) or pd.isna(r.iloc[lo]):
                        continue
                    panels = int(float(r.iloc[lo]))
                    any_ok = True
                except (TypeError, ValueError):
                    continue

                def val(rel_idx_list, pos):
                    if pos >= len(rel_idx_list):
                        return None
                    j = lo + rel_idx_list[pos]
                    if j >= len(r) or pd.isna(r.iloc[j]):
                        return None
                    try:
                        return schedule_amount_round(r.iloc[j], sheet_name)
                    except Exception:
                        return None

                spec["string"][panels] = {
                    "package_price": val(spec["pkg"], 0),
                    "tng_rebate": val(spec["tngr"], 0),
                }
                raw_vals = {}
                for rel_idx, _label in spec["labels"]:
                    j = lo + rel_idx
                    if j >= len(r) or pd.isna(r.iloc[j]):
                        raw_vals[rel_idx] = None
                        continue
                    cell = r.iloc[j]
                    try:
                        raw_vals[rel_idx] = round(float(cell), 2)
                    except (TypeError, ValueError):
                        raw_vals[rel_idx] = " ".join(str(cell).split())
                spec["raw"][panels] = raw_vals
                if spec["hybrid_ok"]:
                    h_after = val(spec["after"], 1)
                    if h_after is not None:
                        spec["hybrid"][panels] = {
                            "package_price": val(spec["pkg"], 1),
                            "tng_rebate": val(spec["tngr"], 1),
                            "final_price": h_after,
                            "final_with_tng": val(spec["wtng"], 1),
                        }
            if not any_ok:
                break
            i += 1

        regions.extend(region_specs)
    return regions


# Internal margin/cost columns that must never appear on the dashboard
_INTERNAL_LABEL_TOKENS = ("成本", "利润", "ceo", "承担")


def _split_label_blocks(labels):
    """Split a region's labels into (block_a, block_b) where a second table
    starts: either the label sequence repeats (string/hybrid side by side) or
    there is a wide column gap (620/590 tables sharing one panels column)."""
    seen = set()
    for pos in range(len(labels)):
        rel, label = labels[pos]
        if pos > 0 and rel - labels[pos - 1][0] > 3:
            return labels[:pos], labels[pos:]
        key = label.lower()
        if key in seen:
            return labels[:pos], labels[pos:]
        if pos > 0 and ("add-on" in key or "hybird" in key or "hybrid" in key) and not any("hyb" in l[1].lower() for l in labels[:pos]):
            return labels[:pos], labels[pos:]
        seen.add(key)
    return labels, []


def _clean_label_block(labels):
    """Drop internal cost columns, then truncate after the last pricing column."""
    out = [(rel, lab) for rel, lab in labels
           if not any(tok in lab.lower() for tok in _INTERNAL_LABEL_TOKENS)]
    last = -1
    for pos, (rel, lab) in enumerate(out):
        ll = lab.lower()
        if ("final price" in ll or "with tng" in ll
                or "max amount after discount" in ll or "nett price" in ll):
            last = pos
    return out[:last + 1] if last >= 0 else out


def _columns_for(region, block, panels):
    raw = region["raw"].get(panels, {})
    return [[label, raw.get(rel)] for rel, label in block]


def parse_schedule_workbook_rows(
    excel_path: Optional[Path] = None, source_label: Optional[str] = None
) -> List[dict]:
    """Parse a schedule workbook into flat dashboard price-list rows.

    Engine prices (final_price / final_with_tng) come from _parse_sheet_tables —
    the exact parser lookup_net_floor_price uses — so imported string-inverter
    prices always match engine behavior. Display extras (package price, TNG
    rebate, hybrid tables, power system) are best-effort and None when a sheet's
    layout doesn't carry them. Months are NOT expanded here; each row carries its
    sheet name and the caller maps sheets to months."""
    import shutil
    import tempfile as _tempfile
    path = excel_path or get_default_excel_path()
    with _tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    rows: List[dict] = []
    try:
        shutil.copy2(path, tmp_path)
        xls = pd.ExcelFile(tmp_path)
        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet, header=None)
            sn = sheet.upper().strip()

            # September 2026 format (HYB and STR sheets for 650W and 625W)
            if ("SEPT" in sn or "SEP 2026" in sn) and any(x in sn for x in ("HYB", "STR")):
                rating = 625 if "625" in sn else 650
                inverter_type = "hybrid" if "HYB" in sn else "string"
                src = f"{source_label}: {sheet}" if source_label else sheet
                # The Data page shows the sheet's own columns and headings
                # (Package Type and Mid Autumn Disc included), not a renamed few.
                sept_heads = [" ".join(str(v).split()) if pd.notna(v) else ""
                              for v in df.iloc[1]]
                for r_idx in range(2, len(df)):
                    r = df.iloc[r_idx]
                    if pd.isna(r.iloc[0]):
                        continue
                    try:
                        panels = int(float(r.iloc[0]))
                    except (ValueError, TypeError):
                        continue
                    pkg_name = " ".join(str(r.iloc[1]).split()) if pd.notna(r.iloc[1]) else ""
                    power_system = "single" if "[1P]" in pkg_name else "three"
                    table_no = 2 if power_system == "single" else 1
                    pkg_price = _clean_money_val(r.iloc[3]) if len(r) > 3 and pd.notna(r.iloc[3]) else None
                    final_price = schedule_amount_round(r.iloc[4], sheet) if len(r) > 4 and pd.notna(r.iloc[4]) else None
                    promo_price = schedule_amount_round(r.iloc[6], sheet) if len(r) > 6 and pd.notna(r.iloc[6]) else None
                    cols = []
                    for c in range(1, min(len(sept_heads), len(r))):
                        if not sept_heads[c] or pd.isna(r.iloc[c]):
                            continue
                        money = _clean_money_val(r.iloc[c])
                        cols.append([sept_heads[c],
                                     " ".join(str(r.iloc[c]).split()) if money is None
                                     else round_rm(money, 2)])
                    rows.append({
                        "sheet": sheet,
                        "panel_rating": rating,
                        "table_no": table_no,
                        "panels": panels,
                        "final_price": final_price,
                        "final_with_tng": None,
                        "package_price": float(pkg_price) if pkg_price is not None else None,
                        "tng_rebate": None,
                        "inverter_type": inverter_type,
                        "power_system": power_system,
                        "columns": cols,
                        "source_sheet": src,
                        "promo_price": promo_price,
                        "promo_from": "2026-09-18",
                        "promo_to": "2026-09-30",
                    })
                continue

            # 590W split format (590W NOV 2025, 590W DEC 2025, 590W JAN 2026)
            if "590W" in sn and ("NOV" in sn or "DEC" in sn or "JAN" in sn):
                headers = [" ".join(str(v).split()) for v in df.iloc[1] if pd.notna(v)]
                col_final = None
                for idx, h in enumerate(headers):
                    if "final price" in h.lower():
                        col_final = idx
                        break
                if col_final is None:
                    col_final = min(6, len(headers) - 1)
                src = f"{source_label}: {sheet}" if source_label else sheet
                for r_idx in range(2, len(df)):
                    r = df.iloc[r_idx]
                    if pd.isna(r.iloc[0]):
                        continue
                    panels = 8 + (r_idx - 2)
                    pkg_price = _clean_money_val(r.iloc[0]) if pd.notna(r.iloc[0]) else None
                    final_price = schedule_amount_round(r.iloc[col_final], sheet) if col_final < len(r) and pd.notna(r.iloc[col_final]) else None
                    cols = [[headers[c], float(_clean_money_val(r.iloc[c])) if _clean_money_val(r.iloc[c]) is not None else r.iloc[c]]
                            for c in range(min(len(headers), len(r))) if pd.notna(r.iloc[c])]
                    rows.append({
                        "sheet": sheet,
                        "panel_rating": 590,
                        "table_no": 2,
                        "panels": panels,
                        "final_price": final_price,
                        "final_with_tng": None,
                        "package_price": float(pkg_price) if pkg_price is not None else None,
                        "tng_rebate": None,
                        "inverter_type": "string",
                        "power_system": None,
                        "columns": cols,
                        "source_sheet": src,
                        "promo_price": None,
                        "promo_from": None,
                        "promo_to": None,
                    })
                continue

            # 620W split layout (620W OCT 2025, 620W NOV 2025, etc.)
            if "620W" in sn and ("OCT" in sn or "NOV" in sn or "DEC" in sn or "JAN" in sn):
                headers = [" ".join(str(v).split()) for v in df.iloc[1] if pd.notna(v)]
                # The floor is "Max amount after discount". Column B is headed
                # "FINAL PRICE" but holds the package price -- matching it first
                # stored RM36,414 as the 20-panel floor instead of RM29,329.
                final_col = next((i for i, h in enumerate(headers)
                                  if "max amount after" in h.lower()), None)
                for idx, h in enumerate(headers):
                    if final_col is not None:
                        break
                    if idx > 1 and "final price" in h.lower():
                        final_col = idx
                        break
                if final_col is None:
                    final_col = len(headers) - 1
                src = f"{source_label}: {sheet}" if source_label else sheet
                for r_idx in range(2, len(df)):
                    r = df.iloc[r_idx]
                    if pd.isna(r.iloc[0]):
                        continue
                    try:
                        panels = int(float(r.iloc[0]))
                    except (ValueError, TypeError):
                        continue
                    pkg_price = _clean_money_val(r.iloc[1]) if len(r) > 1 and pd.notna(r.iloc[1]) else None
                    final_price = schedule_amount_round(r.iloc[final_col], sheet) if final_col < len(r) and pd.notna(r.iloc[final_col]) else None
                    # Column A is the panel count, which the page already shows.
                    cols = [[headers[c], float(_clean_money_val(r.iloc[c])) if _clean_money_val(r.iloc[c]) is not None else r.iloc[c]]
                            for c in range(1, min(len(headers), len(r))) if pd.notna(r.iloc[c])]
                    rows.append({
                        "sheet": sheet,
                        "panel_rating": 620,
                        "table_no": 1,
                        "panels": panels,
                        "final_price": final_price,
                        "final_with_tng": None,
                        "package_price": float(pkg_price) if pkg_price is not None else None,
                        "tng_rebate": None,
                        "inverter_type": "string",
                        "power_system": "single" if "OCT" in sn else None,
                        "columns": cols,
                        "source_sheet": src,
                        "promo_price": None,
                        "promo_from": None,
                        "promo_to": None,
                    })
                continue

            engine_tables = _parse_sheet_tables(df, sheet)
            if not engine_tables:
                continue
            display_regions = _display_scan_sheet(df, sheet)
            aligned = len(display_regions) == len(engine_tables)
            # Nov/Jan layout: one shared panels column carrying two tables side
            # by side (620W block then 590W block) — split the single region.
            shared_region_split = (
                not aligned and len(display_regions) == 1 and len(engine_tables) == 2
            )
            for t_idx, table in enumerate(engine_tables):
                rating = infer_table_panel_rating(sheet, t_idx, len(engine_tables))
                region = None
                string_cols = []
                hybrid_cols = []
                if aligned:
                    region = display_regions[t_idx]
                    blk_a, blk_b = _split_label_blocks(region["labels"])
                    string_cols = _clean_label_block(blk_a)
                    hybrid_cols = _clean_label_block(blk_b) if region["hybrid_ok"] else []
                elif shared_region_split:
                    region = display_regions[0]
                    blk_a, blk_b = _split_label_blocks(region["labels"])
                    string_cols = _clean_label_block(blk_a if t_idx == 0 else blk_b)
                power_system = region["power_system"] if region else None
                src = f"{source_label}: {sheet}" if source_label else sheet
                # A single-phase tab ("650W 1P MAY 2026") is the month's second
                # table, after the 3P tab. Stored as table 1 too (26 Oct sync),
                # the two merged into one and the price for a panel count both
                # list depended on which row the database returned last.
                table_no = t_idx + 1 + (1 if re.search(r"\b1P\b", sn) else 0)
                for panels, pr in sorted(table.items()):
                    extras = region["string"].get(panels, {}) if (region and aligned) else {}
                    rows.append({
                        "sheet": sheet, "panel_rating": rating, "table_no": table_no,
                        "panels": panels, "final_price": pr.final_price,
                        "final_with_tng": pr.final_with_tng,
                        "package_price": extras.get("package_price"),
                        "tng_rebate": extras.get("tng_rebate"),
                        "inverter_type": "string", "power_system": power_system,
                        "columns": _columns_for(region, string_cols, panels) if region else None,
                        "source_sheet": src,
                    })
                if region and aligned:
                    for panels, h in sorted(region["hybrid"].items()):
                        rows.append({
                            "sheet": sheet, "panel_rating": rating, "table_no": table_no,
                            "panels": panels, "final_price": h["final_price"],
                            "final_with_tng": h["final_with_tng"],
                            "package_price": h["package_price"],
                            "tng_rebate": h["tng_rebate"],
                            "inverter_type": "hybrid", "power_system": power_system,
                            "columns": _columns_for(region, hybrid_cols, panels),
                            "source_sheet": src,
                        })
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    return rows


def load_620w_schedule(json_path: Optional[Path] = None) -> Dict[int, float]:
    path = json_path or get_620w_json_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): round_nfp(v) for k, v in data["prices"].items()}


def pick_table_for_panels(
    tables: List[Dict[int, PriceRow]], panels: int, three_phase: bool = False
) -> Optional[Dict[int, PriceRow]]:
    if not tables:
        return None
    if len(tables) == 1:
        return tables[0]

    if three_phase:
        for t in tables:
            if panels in t and min(t.keys()) >= 10:
                return t
        return tables[0]
    for t in reversed(tables):
        if panels in t:
            return t
    return tables[-1]


def canonical_panel_rating(panel_rating: int) -> int:
    """Map panel wattage to schedule bucket: 590W, 620W, or 650W."""
    if panel_rating < 600:
        return 590
    if panel_rating < 640:
        return 620
    return 650


def _sheet_sort_key(sheet_name: str) -> Tuple[int, int]:
    sn = sheet_name.upper().strip()
    year_match = re.search(r"(\d{4})", sn)
    year = int(year_match.group(1)) if year_match else 9999
    if "NOV" in sn or "DEC" in sn:
        return year, 11
    for month_num, name in MONTH_NAMES.items():
        if name in sn:
            return year, month_num
    return year, 99


def _resolve_sheet_name(
    invoice_date: date, schedules_650: Dict[str, List[Dict[int, PriceRow]]]
) -> Optional[str]:
    sheet = SHEET_BY_MONTH.get((invoice_date.year, invoice_date.month))
    if not sheet:
        sheet_name = f"{MONTH_NAMES.get(invoice_date.month, '')} {invoice_date.year}"
        for key in schedules_650.keys():
            if key.strip().upper() == sheet_name.upper():
                return key
        return None
    if sheet in schedules_650:
        return sheet
    for key in schedules_650.keys():
        if key.strip().upper() == sheet.upper():
            return key
    return None


def _ordered_sheets_for_lookup(
    invoice_date: date, schedules_650: Dict[str, List[Dict[int, PriceRow]]]
) -> List[str]:
    """Invoice month first, then nearest other months when inverter type is missing."""
    invoice_sheet = _resolve_sheet_name(invoice_date, schedules_650)
    invoice_key = _sheet_sort_key(invoice_sheet) if invoice_sheet else (
        invoice_date.year,
        invoice_date.month,
    )
    all_sorted = sorted(schedules_650.keys(), key=_sheet_sort_key)

    ordered: List[str] = []
    if invoice_sheet:
        ordered.append(invoice_sheet)

    for sheet in reversed([s for s in all_sorted if _sheet_sort_key(s) < invoice_key]):
        if sheet not in ordered:
            ordered.append(sheet)
    for sheet in [s for s in all_sorted if _sheet_sort_key(s) > invoice_key]:
        if sheet not in ordered:
            ordered.append(sheet)
    return ordered


def infer_table_panel_rating(sheet_name: str, table_index: int, num_tables: int) -> int:
    """
    Infer inverter wattage for a parsed table within a monthly Excel sheet.

    - NOV-DEC 2025 / JAN 2026 side-by-side: table 0 = 620W, table 1 = 590W.
    - If sheet name explicitly has 590W / 620W / 625W / 650W, respect it.
    - For other months (FEB 2026 onwards):
      - If there are multiple tables, the last table is always 620W, and others are 650W.
    """
    sn = sheet_name.upper().strip()
    if "590" in sn and "620" not in sn:
        return 590
    if "625" in sn:
        return 625
    if "620" in sn and "590" not in sn:
        return 620
    if "NOV" in sn or "DEC" in sn or "JAN" in sn:
        if num_tables >= 2:
            return 620 if table_index == 0 else 590
        return 620
    if num_tables >= 2 and table_index == num_tables - 1:
        return 620
    return 650


def pick_table_for_rating(
    sheet_name: str,
    tables: List[Dict[int, PriceRow]],
    panel_rating: int,
    panel_qty: int,
    three_phase: bool = False,
) -> Optional[Dict[int, PriceRow]]:
    target = canonical_panel_rating(panel_rating)
    matching: List[Dict[int, PriceRow]] = []
    for idx, table in enumerate(tables):
        if infer_table_panel_rating(sheet_name, idx, len(tables)) == target:
            matching.append(table)
    if not matching:
        return None
    if target == 650 and len(matching) > 1:
        return pick_table_for_panels(matching, panel_qty, three_phase=three_phase)
    for table in matching:
        if panel_qty in table:
            return table
    return matching[0]


def lookup_net_floor_price(
    invoice_date: date,
    panel_qty: int,
    panel_rating: int,
    has_tng_rebate: bool,
    schedules_650: Dict[str, List[Dict[int, PriceRow]]],
    schedule_620: Dict[int, float],
    three_phase: bool = False,
    rebate_kinds=None,
    approved_discount: float = 0.0,
) -> Tuple[Optional[float], str]:
    """The floor price for an invoice.

    `rebate_kinds` (invoice_rebate_kinds of the invoice's lines) makes the price
    list's floor take off only the rebates that invoice carries; left as None the
    stored floor, with every rebate off, is returned as before.

    `approved_discount` is accepted but no longer used: a CEO / manager discount
    does not lower the floor (decided with Finance 13 Oct 2026, replacing the
    rule that took it off -- Lim Rui Yeong's RM865.75 CEO discount stays on him)."""
    price, source = _lookup_net_floor_price_base(
        invoice_date, panel_qty, panel_rating, has_tng_rebate,
        schedules_650, schedule_620, three_phase, rebate_kinds)
    return price, source


def _lookup_net_floor_price_base(
    invoice_date: date,
    panel_qty: int,
    panel_rating: int,
    has_tng_rebate: bool,
    schedules_650: Dict[str, List[Dict[int, PriceRow]]],
    schedule_620: Dict[int, float],
    three_phase: bool = False,
    rebate_kinds=None,
) -> Tuple[Optional[float], str]:
    if invoice_date < NFP_CUTOFF:
        return None, "before_oct_2025_no_nfp"

    # System price list (dashboard DB) takes priority; any miss falls through
    # to the original Excel/JSON schedule logic below.
    if USE_DB_PRICES:
        db_price, db_source = _db_lookup_net_floor_price(
            invoice_date, panel_qty, panel_rating, has_tng_rebate, three_phase,
            rebate_kinds=rebate_kinds,
        )
        if db_price is not None:
            return db_price, db_source

    if invoice_date >= SCHEDULE_650W_FROM:
        target_rating = canonical_panel_rating(panel_rating or 650)
        invoice_sheet = _resolve_sheet_name(invoice_date, schedules_650)

        for sheet in _ordered_sheets_for_lookup(invoice_date, schedules_650):
            tables = schedules_650.get(sheet, [])
            table = pick_table_for_rating(
                sheet, tables, target_rating, panel_qty, three_phase=three_phase
            )
            if not table or panel_qty not in table:
                continue

            row = table[panel_qty]
            price = (
                row.final_with_tng
                if has_tng_rebate and row.final_with_tng is not None
                else row.final_price
            )
            if sheet == invoice_sheet:
                source = f"{sheet}_with_tng" if has_tng_rebate and row.final_with_tng is not None else sheet
            else:
                source = f"{sheet}_fallback_{target_rating}w"
            return price, source

        return None, f"no_{target_rating}w_schedule_for_{invoice_date:%Y_%m}"

    if panel_rating == 620 or (panel_rating and panel_rating < 640):
        price = schedule_620.get(panel_qty)
        if price is None:
            return None, f"620w_no_row_for_{panel_qty}_panels"
        return price, "620w_pdf_schedule_oct_dec_2025"

    if panel_rating == 650 or canonical_panel_rating(panel_rating or 650) == 650:
        sheet = _resolve_sheet_name(invoice_date, schedules_650)
        if not sheet:
            return None, f"no_650w_sheet_for_{invoice_date:%Y_%m}"
        tables = schedules_650.get(sheet, [])
        table = pick_table_for_rating(sheet, tables, 650, panel_qty, three_phase=three_phase)
        if not table or panel_qty not in table:
            return None, f"650w_no_row_{sheet}_{panel_qty}_panels"
        row = table[panel_qty]
        if has_tng_rebate and row.final_with_tng is not None:
            return row.final_with_tng, f"{sheet}_with_tng"
        return row.final_price, sheet

    return None, "unknown_panel_rating"


def infer_panel_qty_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"panel\s*qty\s*:\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*[xX×]\s*", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*panels?", text, re.I)
    if m:
        return int(m.group(1))
    return None


def infer_panel_rating_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d{3})\s*w", text, re.I)
    if m:
        return int(m.group(1))
    if "66hl4" in text.lower() or "620" in text:
        return 620
    if "650" in text:
        return 650
    return None
