import urllib.request
import csv
from pathlib import Path
from decimal import Decimal

CACHE_PATH = Path(__file__).parent / "basic_commission_rates_cache.csv"
GSHEET_URL = "https://docs.google.com/spreadsheets/d/14v6OIxx6xLwm5KKzKY_TNhqPhYBhxEzE0o6iglurC3Q/export?format=csv&gid=0"


# Global loaded rates cache
_RATES_DATA = None
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
        
    csv_text = None
    try:
        # Attempt to download from Google Sheet
        req = urllib.request.Request(GSHEET_URL, headers={'User-Agent': 'Mozilla/5.0 Antigravity'})
        with urllib.request.urlopen(req, timeout=5) as response:
            csv_text = response.read().decode('utf-8')
            # Cache it
            with open(CACHE_PATH, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text)
    except Exception as e:
        print(f"[Rates System] Warning: Could not fetch online rates: {e}. Trying local cache...")
        
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
        reader = csv.reader(csv_text.splitlines())
        rows = list(reader)
        if len(rows) < 3:
            return _RATES_DATA
            
        headers = [h.strip() for h in rows[1]]
        header_month_indices = {}
        for idx, h in enumerate(headers):
            hl = h.lower().strip()
            if hl in ("jan", "feb", "mac", "jun", "jul"):
                header_month_indices[hl] = idx
                
        agent_type = "Internal"
        for r in rows[2:7]:
            if r[0].strip():
                agent_type = r[0].strip()
            hierarchy = r[1].strip()
            override_rule = r[2].strip()
            
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

def get_basic_rate(agent_type: str, hierarchy: str, month: int) -> Decimal:
    """Resolve basic commission rate from Google Sheet data."""
    rates_data = load_rates_data()
    
    # Normalize inputs
    agent_type = agent_type.strip().lower()
    hierarchy = hierarchy.strip().lower()
    if hierarchy in ("osa", "osa 1", "osa/osa1"):
        hierarchy = "osa/osa1"
        
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
