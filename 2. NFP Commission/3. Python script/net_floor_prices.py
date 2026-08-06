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


def reset_db_prices_cache():
    global _DB_PRICES
    _DB_PRICES = None


def _load_db_prices():
    """Load nfp_prices into {month: {rating: [ {panels: PriceRow}, ... ]}}."""
    global _DB_PRICES
    if _DB_PRICES is not None:
        return _DB_PRICES
    _DB_PRICES = {}
    if not DASHBOARD_DB_PATH.exists():
        return _DB_PRICES
    _NFP_SQL = (
        # String-inverter rows only: hybrid rows are display data for the
        # dashboard and must never feed commission calculations.
        "SELECT month, panel_rating, table_no, panels, final_price, final_with_tng"
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
            return _DB_PRICES
    for r in rows:
        month = str(r["month"])
        rating = int(r["panel_rating"])
        table_no = int(r["table_no"])
        tables = _DB_PRICES.setdefault(month, {}).setdefault(rating, {})
        tables.setdefault(table_no, {})[int(r["panels"])] = PriceRow(
            int(r["panels"]), float(r["final_price"]),
            float(r["final_with_tng"]) if r["final_with_tng"] is not None else None,
        )
    for month, ratings in _DB_PRICES.items():
        for rating in list(ratings.keys()):
            ratings[rating] = [ratings[rating][k] for k in sorted(ratings[rating].keys())]
    return _DB_PRICES


def _db_lookup_net_floor_price(
    invoice_date: date, panel_qty: int, panel_rating: int,
    has_tng_rebate: bool, three_phase: bool
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

    for m in ordered:
        tables = data.get(m, {}).get(target_rating, [])
        if not tables:
            continue
        if target_rating == 650 and len(tables) > 1:
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
    # First, round float inputs to 2 decimal places to avoid float precision loss (e.g. 31869.199999999997 -> 31869.20)
    rounded_val = round(float(value), 2)
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
        if "no.panels" not in row_text:
            i += 1
            continue
        headers = [str(v).strip() if pd.notna(v) else "" for v in row]
        panel_cols = [j for j, h in enumerate(headers) if "no.panels" in h.lower()]
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
                and reg_headers[after[0]].strip().lower() == reg_headers[after[1]].strip().lower()
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
                for panels, pr in sorted(table.items()):
                    extras = region["string"].get(panels, {}) if (region and aligned) else {}
                    rows.append({
                        "sheet": sheet, "panel_rating": rating, "table_no": t_idx + 1,
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
                            "sheet": sheet, "panel_rating": rating, "table_no": t_idx + 1,
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
    - For other months (FEB 2026 onwards):
      - If there are multiple tables, the last table is always 620W, and others are 650W.
    """
    sn = sheet_name.upper().strip()
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
) -> Tuple[Optional[float], str]:
    if invoice_date < NFP_CUTOFF:
        return None, "before_oct_2025_no_nfp"

    # System price list (dashboard DB) takes priority; any miss falls through
    # to the original Excel/JSON schedule logic below.
    if USE_DB_PRICES:
        db_price, db_source = _db_lookup_net_floor_price(
            invoice_date, panel_qty, panel_rating, has_tng_rebate, three_phase
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
