"""The two prices a Basic & NFP commission is calculated on, from 2026.

  Original Price  (Basic Commission row)
      = Total Amount - EPP interest + lucky draw
      Basic Commission = Original Price x Rate %

  Cash Price  (Net Floor Price Commission row)
      = Total Amount - every add-on line - EPP interest  (EPP from 20 Oct 2026)
      NFP Commission = (Cash Price - Net Floor Price) x Rate  (see nfp_commission())

Until this split both rows used one Sales Price, Total Amount - EPP interest.
Decided with Finance on 12 Oct 2026, for every invoice dated 2026 onwards;
invoices dated earlier keep the old Sales Price on both rows.

Lucky draw: the Kluang Opening (16 May 2026) lucky draw prizes were given as
deductions on the invoice. They are added back, so the prize does not shrink
the agent's Basic Commission. A prize that was paid out in cash instead sits
on the invoice at RM 0 and adds nothing. One invoice (INV-1009694) carries the
voucher inside a combined discount line; the Opening Day Special Discount on
that line is added back with it, a plain "Discount (RMx)" on it is not.
The "Kluang Opening Day (16/05/2026) Special Discount" line is added back the
same way even when it never says "lucky draw" (13 Oct 2026, Tan Lian Keng's
RM118 and the RM800 / RM1,200 ones): alone it gives back its whole amount, and
inside a combined line only the part naming it -- the ordinary "Discount" and
"CEO Discount" parts stay deducted.

Add-on: any invoice line with a positive amount other than the package, e.g.
battery, ATS, extra cable, string-to-hybrid inverter upgrade, markup. Lines at
RM 0 or below (discounts, vouchers, FOC notes) are left alone. Two kinds of
positive line are not add-ons:
  * EPP interest and the bank's EPP processing fee -- the EPP comes off
    separately, as the same EPP interest figure the Original Price deducts.
  * The main system written as a plain line instead of a package line
    ("16X 650W JinkoSolar TIGER NEO ... | 1X SAJ ... Inverter"). It is the
    system itself; deducting it would leave a Cash Price of next to nothing.
    An invoice with neither a package line nor such a panel line takes its
    largest line as the system.

The invoice lines reach this module as the `price_items` column the Basic and
NFP queries select through PRICE_ITEMS_JOIN_SQL.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Invoices dated on or after this use Original Price / Cash Price.
PRICE_BASIS_FROM = date(2026, 1, 1)

# The invoice's lines as one JSON array, for a query whose invoice row is
# aliased `i`. Kept free of `%` and braces: the Basic queries go through
# %-formatting and the NFP query through str.format().
PRICE_ITEMS_JOIN_SQL = """
  LEFT JOIN LATERAL (
    SELECT json_agg(json_build_object(
             'amount', COALESCE(x.amount, x.unit_price, 0),
             'description', COALESCE(x.description, ''),
             'is_package', COALESCE(x.is_a_package, FALSE),
             'item_type', COALESCE(x.inv_item_type, '')
           ) ORDER BY x.id) AS price_items
    FROM (
      SELECT DISTINCT ii.id, ii.amount, ii.unit_price, ii.description,
             ii.is_a_package, ii.inv_item_type
      FROM invoice_item ii
      WHERE ii.linked_invoice = i.bubble_id
         OR ii.bubble_id = ANY(i.linked_invoice_item)
    ) x
  ) price_items_lat ON TRUE
"""

_EPP_RE = re.compile(r"epp.*interest|processing\s+fee", re.I | re.S)
_PANEL_RE = re.compile(
    r"\d+\s*X\s*[^|\n]*?(panel|\d{3}\s*W\b|jinko|astro|longi|trina)", re.I)
_LUCKY_RE = re.compile(r"lucky\s*draw", re.I)
# "Kluang Opening Day (16/05/2026) Special Discount RM118" / "... (RM1200)": the
# Opening Day special discount, keyed without the words "lucky draw". The part
# after the name carries its own amount when the line is a combined one.
_OPENING_DAY_RE = re.compile(r"kluang\s+opening\s+day.*?special\s+discount", re.I)
_OPENING_DAY_AMOUNT_RE = re.compile(
    r"special\s+discount\s*\(?\s*RM\s*([\d,]+(?:\.\d+)?)", re.I)
# A "Discount (RM1275)" part of a combined lucky-draw line: an ordinary
# discount, not part of the prize.
_PLAIN_DISCOUNT_RE = re.compile(
    r"^\s*discount\s*\(\s*RM\s*([\d,]+(?:\.\d+)?)\s*\)\s*$", re.I)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0).replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _items(raw: Any) -> list[dict]:
    """The `price_items` column, whether the proxy returned it parsed or as text."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [it for it in raw if isinstance(it, dict)] if isinstance(raw, list) else []


def applies(invoice_date: Any) -> bool:
    """Whether this invoice is priced as Original Price / Cash Price."""
    if isinstance(invoice_date, datetime):
        d = invoice_date.date()
    elif isinstance(invoice_date, date):
        d = invoice_date
    else:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(invoice_date or ""))
        if not m:
            return False
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return d >= PRICE_BASIS_FROM


def _is_epp(item: dict) -> bool:
    return (str(item.get("item_type") or "").lower() == "epp_fee"
            or bool(_EPP_RE.search(str(item.get("description") or ""))))


def _is_system(item: dict) -> bool:
    return (bool(item.get("is_package"))
            or str(item.get("item_type") or "").lower() == "package"
            or bool(_PANEL_RE.search(str(item.get("description") or ""))))


def addon_total(price_items: Any) -> Decimal:
    """What the invoice's add-on lines add to its Total Amount."""
    positive = [it for it in _items(price_items)
                if _dec(it.get("amount")) > 0 and not _is_epp(it)]
    system = [it for it in positive if _is_system(it)]
    if not system and positive:
        system = [max(positive, key=lambda it: _dec(it.get("amount")))]
    return sum((_dec(it.get("amount")) for it in positive
                if not any(it is s for s in system)), Decimal("0"))


def _opening_day_part(desc: str, amount: Decimal) -> Decimal:
    """The Opening Day special discount inside a deduction line, as a positive
    amount. A line that is only that discount gives its whole amount back; a
    combined line ("Discount (RM1550)" / "Kluang Opening Day ... Special
    Discount (RM800)" / "CEO Discount") gives back just the part naming it, so
    the ordinary and CEO discounts stay deducted."""
    parts = [p for p in desc.splitlines() if p.strip()]
    total = Decimal("0")
    for part in parts:
        if not _OPENING_DAY_RE.search(part):
            continue
        named = _OPENING_DAY_AMOUNT_RE.search(part)
        if named:
            total += _dec(named.group(1))
        elif len(parts) == 1:
            total += -amount
    return min(total, -amount)


def lucky_draw_addback(price_items: Any) -> Decimal:
    """The lucky draw prizes and Opening Day special discounts deducted on the
    invoice, as a positive amount."""
    total = Decimal("0")
    for it in _items(price_items):
        amount = _dec(it.get("amount"))
        desc = str(it.get("description") or "")
        if amount >= 0:
            continue
        if _LUCKY_RE.search(desc):
            plain = sum((_dec(m.group(1)) for m in
                         (_PLAIN_DISCOUNT_RE.match(part) for part in desc.splitlines())
                         if m), Decimal("0"))
            total += max(Decimal("0"), -amount - plain)
        elif _OPENING_DAY_RE.search(desc):
            total += _opening_day_part(desc, amount)
    return total


def original_price(total: Any, epp: Any, invoice_date: Any,
                   price_items: Any) -> Decimal:
    """The Basic Commission row's price."""
    price = _dec(total) - _dec(epp)
    if applies(invoice_date):
        price += lucky_draw_addback(price_items)
    return price


def nfp_commission(cash: Decimal, package_price: Decimal, net_floor: Decimal,
                   rate_above: Decimal, rate_package: Decimal,
                   rate_below: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Net Floor Price commission on the Cash Price, from 2026. The rate is
    applied to the Cash Price's distance from the Net Floor Price (corrected
    with Finance 13 Oct 2026; 12 Oct had it on the whole Cash Price):

        Cash Price > package price      Rate = 100%  (rate_package)
        Cash Price > Net Floor Price    Rate = 25%   (rate_above)
        Cash Price < Net Floor Price    Rate = 20%, a deduction (rate_below)
        NFP commission = (Cash Price - Net Floor Price) x Rate

    Checked in that order, so a Cash Price above the package price takes 100%
    even though it is also above the floor. Below the floor the difference is
    negative, so the same formula yields the deduction. The rates are the Data
    page's NFP tiers. Returns (a, b, c, total) in the shape the NFP reports
    already carry: a = the 25% figure, b = the 100% figure, c = the deduction
    (as a positive amount), total = what is paid.
    """
    zero = Decimal("0")
    a = b = c = zero
    diff = cash - net_floor
    # A floor above the package price would make this negative; that is a
    # shortfall against the floor like any other, not a 100% tier.
    if package_price > 0 and cash > package_price and diff > 0:
        b = diff * rate_package
        return a, b, c, b
    if diff > 0:
        a = diff * rate_above
    elif diff < 0:
        c = -diff * rate_below
    return a, b, c, a - c


def cash_price(total: Any, epp: Any, invoice_date: Any,
               price_items: Any) -> Decimal:
    """The Net Floor Price Commission row's price. Before PRICE_BASIS_FROM it is
    the old Sales Price, Total Amount - EPP interest.

    From 2026 it also takes the add-ons off, and since 20 Oct 2026 the EPP
    interest too (user; it had been kept in since 12 Oct): the same `epp`
    figure the Original Price deducts. Applied as at that date it lowered 76
    invoices' NFP commission by RM 31,746.55 over Mar-Oct 2026."""
    if not applies(invoice_date):
        return _dec(total) - _dec(epp)
    return _dec(total) - addon_total(price_items) - _dec(epp)
