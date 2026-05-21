"""Load Net Floor Price schedules from Excel (650W) and JSON (620W)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from nfp_paths import get_620w_json_path, get_default_excel_path

SHEET_BY_MONTH: Dict[Tuple[int, int], str] = {
    (2026, 1): "FEB 2026",
    (2026, 2): "FEB 2026",
    (2026, 3): "MAC 2026",
    (2026, 4): "APR 2026",
    (2026, 5): "MAY 2026",
}

NFP_CUTOFF = date(2025, 10, 1)
SCHEDULE_650W_FROM = date(2026, 1, 1)


@dataclass(frozen=True)
class PriceRow:
    panels: int
    final_price: float
    final_with_tng: Optional[float]


def round_rm(value: float | int | str | Decimal, places: int = 2) -> float:
    """Round money to N decimal places (half up). Used for commission amounts."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _is_feb_schedule_sheet(sheet_name: str) -> bool:
    """650W Excel sheets whose name includes FEB (e.g. FEB 2026) use 2-decimal half-up."""
    return "feb" in sheet_name.lower()


def schedule_amount_round(value: float | int | str | Decimal, sheet_name: str) -> float:
    """
    Per-sheet NFP schedule rounding when rebuilding FINAL from Excel.

    - **FEB** sheets (e.g. FEB 2026): half-up to **2 decimal places** (sen), `round_rm`.
    - **Other** months: `round_nfp` (1-decimal business rule).
    """
    if _is_feb_schedule_sheet(sheet_name):
        return round_rm(value, 2)
    return round_nfp(value)


def round_nfp(value: float | int | str | Decimal) -> float:
    """
    Net floor price rounding: **1 decimal place** (RM 0.10), not 2 decimals.

    Example: 20,135.75 -> 20,135.6
    - Truncate to 1 decimal (20,135.7)
    - If the 2nd decimal (sen) is 5–9, reduce by RM 0.10 (20,135.6)
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    d = Decimal(str(value))
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
    i = 0
    while i < n:
        row = df.iloc[i]
        row_text = " ".join(str(v).lower() for v in row if pd.notna(v))
        if "no.panels" not in row_text:
            i += 1
            continue

        headers = [str(v).strip() if pd.notna(v) else "" for v in row]
        col_panels = _header_index(headers, "no.panels") or 0
        col_after_max = _header_index(headers, "price after max")
        col_final = _header_index(
            headers, "final price after discount", exclude=("tng", "with")
        )
        col_tng_rebate = _header_index(headers, "roadshow", "tng") or _header_index(
            headers, "tng rebate"
        )
        col_with_tng = _header_index(headers, "with tng")

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
    path = excel_path or get_default_excel_path()
    xls = pd.ExcelFile(path)
    out: Dict[str, List[Dict[int, PriceRow]]] = {}
    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet, header=None)
        out[sheet] = _parse_sheet_tables(df, sheet)
    return out


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


def lookup_net_floor_price(
    invoice_date: date,
    panel_qty: int,
    panel_rating: int,
    has_tng_rebate: bool,
    schedules_650: Dict[str, List[Dict[int, PriceRow]]],
    schedule_620: Dict[int, float],
    three_phase: bool = False,
) -> Tuple[Optional[float], str]:
    if invoice_date < NFP_CUTOFF:
        return None, "before_oct_2025_no_nfp"

    sheet = SHEET_BY_MONTH.get((invoice_date.year, invoice_date.month))
    if sheet or invoice_date >= SCHEDULE_650W_FROM:
        if not sheet:
            return None, f"no_650w_sheet_for_{invoice_date:%Y_%m}"
        tables = schedules_650.get(sheet, [])
        table = pick_table_for_panels(tables, panel_qty, three_phase=three_phase)
        if not table or panel_qty not in table:
            return None, f"650w_no_row_{sheet}_{panel_qty}_panels"
        row = table[panel_qty]
        if has_tng_rebate and row.final_with_tng is not None:
            return row.final_with_tng, f"{sheet}_with_tng"
        return row.final_price, sheet

    if panel_rating == 620 or (panel_rating and panel_rating < 640):
        price = schedule_620.get(panel_qty)
        if price is None:
            return None, f"620w_no_row_for_{panel_qty}_panels"
        return price, "620w_pdf_schedule_oct_dec_2025"

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
