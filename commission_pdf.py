"""
PDF export for commission reports (reportlab).

- write_commission_pdf: compact multi-section report
- write_finance_presentation_pdf: finance deck (cover, charts, one table per page)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph

try:
    pdfmetrics.registerFont(TTFont('Verdana', 'verdana.ttf'))
    pdfmetrics.registerFont(TTFont('Verdana-Bold', 'verdanab.ttf'))
    pdfmetrics.registerFont(TTFont('Verdana-Italic', 'verdanai.ttf'))
    pdfmetrics.registerFont(TTFont('Verdana-BoldItalic', 'verdanabi.ttf'))
    FONT_REGULAR = "Verdana"
    FONT_BOLD = "Verdana-Bold"
    FONT_ITALIC = "Verdana-Italic"
except Exception:
    FONT_REGULAR = "Helvetica"
    FONT_BOLD = "Helvetica-Bold"
    FONT_ITALIC = "Helvetica-Oblique"


# Shared agent full-name resolver (nickname -> canonical full name, Title Case),
# sourced from the dashboard's Agent Roles & Hierarchy page (agent_roles table).
# Applied to Agent columns at render time so upstream tier/hierarchy logic
# (keyed on nicknames) is unaffected.
import os as _os
import sys as _sys
_REPO_ROOT = _os.path.dirname(_os.path.abspath(__file__))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
try:
    import agent_names as _agent_names
except Exception:
    _agent_names = None


def _is_agent_header(header: Any) -> bool:
    return isinstance(header, str) and "agent" in header.lower()


def _resolve_agent_cells(headers: Sequence[Any], rows: list[list[Any]]) -> list[list[Any]]:
    """Return a copy of rows with Agent columns replaced by canonical full names."""
    if not _agent_names or not headers:
        return rows
    agent_cols = [i for i, h in enumerate(headers) if _is_agent_header(h)]
    if not agent_cols:
        return rows
    new_rows = []
    for row in rows:
        row = list(row)
        for i in agent_cols:
            if i < len(row):
                val = row[i]
                if isinstance(val, str) and val.strip() and val.strip() != "-":
                    row[i] = _agent_names.resolve(val.strip())
        new_rows.append(row)
    return new_rows


@dataclass(frozen=True)
class PdfSection:
    title: str
    headers: list[str]
    rows: list[list[str]]
    landscape: bool = False
    monthly_tables: dict[int, list[list[str]]] | None = None
    total_agents: int | None = None
    total_customers: int | None = None
    footer_text: list[str] | None = None


@dataclass(frozen=True)
class FinanceChartData:
    """Numbers for dashboard charts (Basic / ANP / NFP / grand total)."""

    basic_total: float
    anp_total: float
    nfp_total: float
    grand_total: float
    top3: list[tuple[str, float, float, float, float]]  # agent, basic, anp, nfp, combined

    # New optional fields for monthly analysis
    monthly_trend: list[dict[str, Any]] | None = None
    monthly_agent_customers: list[dict[str, int]] | None = None
    agent_names: list[str] | None = None
    monthly_commission_split: list[dict[str, float]] | None = None
    monthly_agent_commission_split: dict[int, dict[str, dict[str, float]]] | None = None
    total_agents: int | None = None
    total_customers: int | None = None
    monthly_agent_sales: list[dict[str, float]] | None = None
    full_payment_count: int | None = None
    pending_payment_count: int | None = None
    closed_agents_count: int | None = None
    pending_agents_count: int | None = None
    full_payment_sales: float | None = None


def _parse_amount(value: Any) -> float:
    try:
        return float(Decimal(str(value).replace(",", "").replace("RM", "").strip()))
    except Exception:
        return 0.0


def _escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("&lt;br/&gt;", "<br/>")
        .replace("&lt;br&gt;", "<br/>")
        .replace("&lt;br /&gt;", "<br/>")
    )


def _draw_monthly_trend_chart(
    monthly_data: list[dict[str, Any]],
    width: float = 500,
    height: float = 310
) -> Drawing:
    import math
    from reportlab.graphics.shapes import Drawing, Rect, Line, String, Circle
    from reportlab.lib import colors

    d = Drawing(width, height)
    
    # Background card styling
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#f8fafc"), strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=1, rx=8, ry=8))
    
    n_months = len(monthly_data)
    
    pad_left = 45
    pad_right = 55
    pad_bottom = 25
    pad_top = 25
    
    graph_w = width - pad_left - pad_right
    graph_h = height - pad_bottom - pad_top
    
    months = ["Jan", "Feb", "Mac", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:n_months]
    
    # Determine RM max dynamically based on Total Sales
    max_sales = 0.0
    for m_data in monthly_data:
        val = m_data.get("total_sales", 0.0)
        if val > max_sales:
            max_sales = val
    max_rm = max(100000.0, float(math.ceil(max_sales / 100000.0) * 100000.0))
    
    # Determine customer count max dynamically based on total_customers
    max_cust_val = 0
    for m_data in monthly_data:
        val = m_data.get("customer_count", 0)
        if val > max_cust_val:
            max_cust_val = val
    max_cust = max(50.0, float(math.ceil(max_cust_val / 25.0) * 25.0))
    
    # Grid lines & ticks (5 divisions)
    divisions = 5
    for i in range(divisions + 1):
        y = pad_bottom + (graph_h * i / divisions)
        if i > 0 and i < divisions:
            d.add(Line(pad_left, y, pad_left + graph_w, y, strokeColor=colors.HexColor("#cbd5e0"), strokeWidth=0.5, strokeDashArray=[2, 2]))
            
        # Left Y label (Customer Count)
        cust_val = int(max_cust * i / divisions)
        d.add(String(pad_left - 8, y - 3, str(cust_val), fontSize=8, fontName=FONT_REGULAR, textAnchor="end", fillColor=colors.HexColor("#dd6b20")))
        
        # Right Y label (RM)
        rm_val = max_rm * i / divisions
        rm_str = f"RM {rm_val/1000:,.0f}k" if rm_val >= 1000 else f"RM {rm_val:.0f}"
        d.add(String(pad_left + graph_w + 6, y - 3, rm_str, fontSize=8, fontName=FONT_REGULAR, textAnchor="start", fillColor=colors.HexColor("#1A365D")))
        
    # X labels and ticks
    x_coords = []
    graph_margin = 25  # Space between left/right Y-axes and boundary months
    for i, month_name in enumerate(months):
        if n_months > 1:
            x = pad_left + graph_margin + ((graph_w - 2 * graph_margin) * i / (n_months - 1))
        else:
            x = pad_left + graph_w / 2
        x_coords.append(x)
        d.add(String(x, pad_bottom - 13, month_name, fontSize=7.5, fontName=FONT_BOLD, textAnchor="middle", fillColor=colors.HexColor("#4a5568")))
        d.add(Line(x, pad_bottom, x, pad_bottom - 3, strokeColor=colors.HexColor("#cbd5e0"), strokeWidth=1))
        
    # 1. Plot Stacked Bars: Commission (Bottom) + Net Sales (Top)
    bar_w = 20.0
    for i in range(n_months):
        cx = x_coords[i]
        total_comm = monthly_data[i].get("total_commission", 0.0)
        total_sales = monthly_data[i].get("total_sales", 0.0)
        
        if total_sales > 0:
            h_comm = graph_h * (total_comm / max_rm)
            h_sales = graph_h * (total_sales / max_rm)
            h_net_sales = max(0, h_sales - h_comm)
            
            # Net Sales bar (light blue) at bottom
            if h_net_sales > 0:
                d.add(Rect(
                    cx - bar_w / 2,
                    pad_bottom,
                    bar_w,
                    h_net_sales,
                    fillColor=colors.HexColor("#4299E1"),
                    strokeColor=colors.white,
                    strokeWidth=0.25
                ))
                
            # Commission bar (dark blue) at top
            if h_comm > 0:
                d.add(Rect(
                    cx - bar_w / 2,
                    pad_bottom + h_net_sales,
                    bar_w,
                    h_comm,
                    fillColor=colors.HexColor("#1A365D"),
                    strokeColor=colors.white,
                    strokeWidth=0.25
                ))
                
    # 2. Plot Total Customers as Line Chart
    points_cust = []
    for i in range(n_months):
        cx = x_coords[i]
        val = monthly_data[i].get("customer_count", 0)
        cy = pad_bottom + (graph_h * (val / max_cust))
        points_cust.append((cx, cy))
        
    for i in range(n_months - 1):
        d.add(Line(points_cust[i][0], points_cust[i][1], points_cust[i+1][0], points_cust[i+1][1], strokeColor=colors.HexColor("#dd6b20"), strokeWidth=2.0))
        
    for i in range(n_months):
        cx, cy = points_cust[i]
        val = monthly_data[i].get("customer_count", 0)
        if val > 0:
            d.add(Circle(cx, cy, 3.5, fillColor=colors.HexColor("#dd6b20"), strokeColor=colors.white, strokeWidth=1.0))
                
    d.add(String(pad_left, height - 12, "Customer Count", fontSize=8, fontName=FONT_BOLD, fillColor=colors.HexColor("#dd6b20"), textAnchor="start"))
    d.add(String(pad_left + graph_w, height - 12, "Total Sales & Commission (RM)", fontSize=8, fontName=FONT_BOLD, fillColor=colors.HexColor("#1A365D"), textAnchor="end"))
    
    # Legend
    leg_x = pad_left + graph_w / 2 - 60
    leg_y = height - 12
    d.add(Rect(leg_x, leg_y - 2, 8, 8, fillColor=colors.HexColor("#4299E1"), strokeColor=None))
    d.add(String(leg_x + 12, leg_y, "Sales", fontSize=7, fontName=FONT_REGULAR, fillColor=colors.HexColor("#4a5568")))
    
    d.add(Rect(leg_x + 50, leg_y - 2, 8, 8, fillColor=colors.HexColor("#1A365D"), strokeColor=None))
    d.add(String(leg_x + 62, leg_y, "Commission", fontSize=7, fontName=FONT_REGULAR, fillColor=colors.HexColor("#4a5568")))
    
    return d


def _draw_monthly_agent_customer_chart(m_data: dict[str, int], agent_names: list[str], month_name: str, width: float = 255, height: float = 145) -> Drawing:
    import math
    from reportlab.graphics.shapes import Drawing, Rect, Line, String
    from reportlab.lib import colors

    d = Drawing(width, height)
    # Card background
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#f8fafc"), strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=1, rx=6, ry=6))
    
    # Month Title at top left
    d.add(String(10, height - 14, month_name, fontSize=9, fontName=FONT_BOLD, fillColor=colors.HexColor("#1a365d")))
    
    pad_left = 75
    pad_right = 20
    pad_bottom = 20
    pad_top = 22
    
    graph_w = width - pad_left - pad_right
    graph_h = height - pad_bottom - pad_top
    
    # Sort agents in descending order of customer count for this specific month
    active_agents = sorted(agent_names, key=lambda name: (m_data.get(name, 0), name.lower()), reverse=True)
    
    max_val = max(m_data.get(name, 0) for name in active_agents) if active_agents else 0
    max_val = max(5, int(math.ceil(max_val / 5.0) * 5))
    
    # Grid lines (5 divisions)
    divisions = 5
    for i in range(divisions + 1):
        x = pad_left + (graph_w * i / divisions)
        val = int(max_val * i / divisions)
        d.add(Line(x, pad_bottom, x, pad_bottom + graph_h, strokeColor=colors.HexColor("#cbd5e0"), strokeWidth=0.25, strokeDashArray=[1, 1]))
        d.add(String(x, pad_bottom - 10, str(val), fontSize=7, fontName=FONT_REGULAR, textAnchor="middle", fillColor=colors.HexColor("#718096")))
        
    n_agents = len(active_agents)
    bar_height = max(5, min(14, int(graph_h / n_agents * 0.5)))
    
    agent_palette = ["#1a365d", "#2b6cb0", "#319795", "#d69e2e", "#805ad5", "#dd6b20", "#38a169", "#e53e3e", "#4a5568", "#b83280", "#2c5282", "#2c7a7b", "#744210"]

    for idx, agent_name in enumerate(active_agents):
        if n_agents > 1:
            y_center = pad_bottom + graph_h - (graph_h * idx / (n_agents - 1))
        else:
            y_center = pad_bottom + graph_h / 2
            
        y = y_center - bar_height / 2
        cnt = m_data.get(agent_name, 0)
        bar_w = graph_w * (cnt / max_val)
        
        agent_idx = agent_names.index(agent_name) if agent_name in agent_names else 0
        color = agent_palette[agent_idx % len(agent_palette)]
        
        if bar_w > 0:
            d.add(Rect(pad_left, y, bar_w, bar_height, fillColor=colors.HexColor(color), strokeColor=colors.white, strokeWidth=0.25))
        
        # Label on the left
        display_name = agent_name[:12] + ".." if len(agent_name) > 14 else agent_name
        d.add(String(pad_left - 5, y_center - 2.5, display_name, fontSize=6.5, fontName=FONT_BOLD, textAnchor="end", fillColor=colors.HexColor("#2d3748")))
        
        # Value on the right of the bar
        d.add(String(pad_left + bar_w + 3, y_center - 2.5, str(cnt), fontSize=7, fontName=FONT_REGULAR, fillColor=colors.HexColor("#4a5568")))
        
    return d


def _draw_monthly_commission_split_chart(m_data: dict[str, dict[str, float]], agent_names: list[str], month_name: str, width: float = 255, height: float = 145) -> Drawing:
    import math
    from reportlab.graphics.shapes import Drawing, Rect, Line, String
    from reportlab.lib import colors

    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#f8fafc"), strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=1, rx=6, ry=6))
    
    # Month Title
    d.add(String(10, height - 14, month_name, fontSize=9, fontName=FONT_BOLD, fillColor=colors.HexColor("#1a365d")))
    
    pad_left = 75
    pad_right = 20
    pad_bottom = 20
    pad_top = 22
    
    graph_w = width - pad_left - pad_right
    graph_h = height - pad_bottom - pad_top
    
    # Sort agents in descending order of total commission for this specific month
    active_agents = sorted(agent_names, key=lambda name: (sum(m_data.get(name, {}).values()), name.lower()), reverse=True)
        
    max_val = max(sum(m_data.get(name, {}).values()) for name in active_agents) if active_agents else 0.0
    max_val = max(500.0, float(math.ceil(max_val / 500.0) * 500.0))
    
    # Grid lines (5 divisions)
    divisions = 5
    for i in range(divisions + 1):
        x = pad_left + (graph_w * i / divisions)
        val = int(max_val * i / divisions)
        val_str = f"{val/1000:.1f}k" if val >= 1000 else str(val)
        d.add(Line(x, pad_bottom, x, pad_bottom + graph_h, strokeColor=colors.HexColor("#cbd5e0"), strokeWidth=0.25, strokeDashArray=[1, 1]))
        d.add(String(x, pad_bottom - 10, val_str, fontSize=6.5, fontName=FONT_REGULAR, textAnchor="middle", fillColor=colors.HexColor("#718096")))
        
    type_colors = {
        "Basic": "#2c5282",
        "NFP": "#319795",
        "ANP": "#d69e2e"
    }
    
    n_agents = len(active_agents)
    bar_height = max(5, min(14, int(graph_h / n_agents * 0.5)))
    
    for idx, agent_name in enumerate(active_agents):
        if n_agents > 1:
            y_center = pad_bottom + graph_h - (graph_h * idx / (n_agents - 1))
        else:
            y_center = pad_bottom + graph_h / 2
            
        y = y_center - bar_height / 2
        splits = m_data.get(agent_name, {})
        total_comm = sum(splits.values())
        
        current_x = pad_left
        for comm_type in ["Basic", "NFP", "ANP"]:
            val = splits.get(comm_type, 0.0)
            if val <= 0:
                continue
            seg_w = graph_w * (val / max_val)
            color = type_colors[comm_type]
            d.add(Rect(current_x, y, seg_w, bar_height, fillColor=colors.HexColor(color), strokeColor=colors.white, strokeWidth=0.25))
            # Include segment value inside the bar if segment is wide enough
            if seg_w > 12:
                d.add(String(
                    current_x + seg_w / 2,
                    y_center - 2.2,
                    f"{val:.0f}",
                    fontSize=5.5,
                    fontName=FONT_BOLD,
                    textAnchor="middle",
                    fillColor=colors.white
                ))
            current_x += seg_w
            
        # Label on the left
        display_name = agent_name[:12] + ".." if len(agent_name) > 14 else agent_name
        d.add(String(pad_left - 5, y_center - 2.5, display_name, fontSize=6.5, fontName=FONT_BOLD, textAnchor="end", fillColor=colors.HexColor("#2d3748")))
        
        # Total value on the right
        d.add(String(current_x + 3, y_center - 2.5, f"{total_comm:.0f}", fontSize=6.5, fontName=FONT_REGULAR, fillColor=colors.HexColor("#4a5568")))
        
    return d


def compute_table_spans(rows: list[list[Any]], start_row: int = 2) -> tuple[list[list[Any]], list[tuple[str, tuple[int, int], tuple[int, int]]]]:
    cleaned_rows = [list(r) for r in rows]
    span_commands = []
    n_rows = len(cleaned_rows)
    if n_rows <= 1:
        return cleaned_rows, span_commands

    MAX_SPAN = 12  # Limit spans to 12 rows to allow ReportLab to split tables across pages

    agent_start = 0
    while agent_start < n_rows:
        agent_end = agent_start
        while (agent_end + 1 < n_rows and 
               cleaned_rows[agent_end + 1][0] == cleaned_rows[agent_start][0] and
               (agent_end - agent_start + 1) < MAX_SPAN):
            agent_end += 1

        if agent_end > agent_start:
            span_commands.append(('SPAN', (0, agent_start + start_row), (0, agent_end + start_row)))
            for r in range(agent_start + 1, agent_end + 1):
                cleaned_rows[r][0] = ""

        cust_start = agent_start
        while cust_start <= agent_end:
            cust_end = cust_start
            while (cust_end + 1 <= agent_end and 
                   cleaned_rows[cust_end + 1][1] == cleaned_rows[cust_start][1] and
                   (cust_end - cust_start + 1) < MAX_SPAN):
                cust_end += 1

            if cust_end > cust_start:
                span_commands.append(('SPAN', (1, cust_start + start_row), (1, cust_end + start_row)))
                span_commands.append(('SPAN', (2, cust_start + start_row), (2, cust_end + start_row)))
                for r in range(cust_start + 1, cust_end + 1):
                    cleaned_rows[r][1] = ""
                    cleaned_rows[r][2] = ""

            cust_start = cust_end + 1

        agent_start = agent_end + 1

    return cleaned_rows, span_commands


def build_stacked_table(month_title: str, headers: list[str], rows: list[list[str]], page_width: float, col_ratios: list[float] | None = None) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    # Agent columns -> canonical full names (Title Case) before layout/merging.
    rows = _resolve_agent_cells(headers, rows)

    styles = getSampleStyleSheet()

    cell_style_left = ParagraphStyle("CellLeft", parent=styles["Normal"], fontSize=7.5, leading=8.5, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_center = ParagraphStyle("CellCenter", parent=styles["Normal"], fontSize=7.5, leading=8.5, alignment=TA_CENTER, fontName=FONT_REGULAR)
    cell_style_right = ParagraphStyle("CellRight", parent=styles["Normal"], fontSize=7.5, leading=8.5, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    
    header_style = ParagraphStyle("StackedHeader", parent=styles["Normal"], fontSize=9.5, leading=10.5, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style = ParagraphStyle("StackedSubHeader", parent=styles["Normal"], fontSize=8, leading=9, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)

    ncols = len(headers)
    if col_ratios:
        total_ratio = sum(col_ratios)
        col_widths = [page_width * r / total_ratio for r in col_ratios]
    else:
        col_widths = [page_width / ncols] * ncols
        
    table_data = []
    table_data.append([Paragraph(month_title, header_style)] + [""] * (ncols - 1))
    table_data.append([Paragraph(h, sub_header_style) for h in headers])
    
    alignments = []
    for h in headers:
        hl = h.lower()
        if "rate" in hl or "%" in hl:
            alignments.append(cell_style_center)
        elif "price" in hl or "(rm)" in hl or "commission" in hl or "amount" in hl or "total" in hl or "interest" in hl:
            alignments.append(cell_style_right)
        elif "count" in hl or "number" in hl or "no." in hl or "date" in hl:
            alignments.append(cell_style_center)
        else:
            alignments.append(cell_style_left)
            
    for row in rows:
        formatted_row = []
        for i, val in enumerate(row):
            formatted_row.append(Paragraph(val, alignments[i]))
        table_data.append(formatted_row)
        
    table = Table(table_data, colWidths=col_widths, repeatRows=2)
    
    raw_data_rows = [list(r) for r in rows]
    cleaned_data, spans = compute_table_spans(raw_data_rows, start_row=2)
    
    for r_idx, clean_row in enumerate(cleaned_data):
        t_row_idx = r_idx + 2
        for c_idx, val in enumerate(clean_row):
            if val == "":
                table_data[t_row_idx][c_idx] = ""
            else:
                table_data[t_row_idx][c_idx] = Paragraph(val, alignments[c_idx])
                
    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A365D")),
        ("SPAN", (0, 0), (-1, 0)),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#2C5282")),
        ("ALIGN", (0, 1), (-1, 1), "CENTER"),
        ("VALIGN", (0, 1), (-1, 1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, 1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
        
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
        ("VALIGN", (0, 2), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 2), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 2), (-1, -1), 3),
    ]
    
    for r in range(2, len(table_data)):
        if r % 2 == 0:
            t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
            
    t_styles.extend(spans)
    table.setStyle(TableStyle(t_styles))
    return table


def _generate_monthly_trend_analysis(charts: FinanceChartData, intro_style, bullet_style) -> list[Paragraph]:
    if not charts.monthly_trend:
        return [Paragraph("No monthly trend data available.", intro_style)]
    
    peak_sales_val = -1.0
    peak_sales_month = 1
    peak_sales_comm = 0.0
    for idx, d in enumerate(charts.monthly_trend):
        val = d.get("total_sales", 0.0)
        if val > peak_sales_val:
            peak_sales_val = val
            peak_sales_month = idx + 1
            peak_sales_comm = d.get("total_commission", 0.0)
            
    avg_sales = sum(d.get("total_sales", 0.0) for d in charts.monthly_trend) / len(charts.monthly_trend)
    avg_comm = sum(d.get("total_commission", 0.0) for d in charts.monthly_trend) / len(charts.monthly_trend)
    
    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    
    intro_txt = f"This chart tracks the monthly trend of Total Sales alongside the Total Commission paid and Customer Count in {charts.monthly_trend[0].get('year', 2026)}."
    bullets = [
        f"The peak sales month was <b>{month_names[peak_sales_month - 1]}</b> at <b>RM {peak_sales_val:,.2f}</b>, generating RM {peak_sales_comm:,.2f} in commissions.",
        f"Over the course of the period, average monthly sales were <b>RM {avg_sales:,.2f}</b>, while average monthly commission was <b>RM {avg_comm:,.2f}</b>."
    ]
    concl_txt = f"The stacked bar visualization displays the proportion of Commission relative to the Total Sales volume, with Customer Count represented by the overlaid line trend."

    story_flow = []
    story_flow.append(Paragraph(intro_txt, intro_style))
    for b in bullets:
        story_flow.append(Paragraph(f"&bull;&nbsp;&nbsp;{b}", bullet_style))
    story_flow.append(Paragraph(concl_txt, intro_style))
    return story_flow


def _generate_agent_customer_analysis(charts: FinanceChartData, intro_style, bullet_style) -> list[Paragraph]:
    if not charts.monthly_agent_customers or not charts.agent_names:
        return [Paragraph("No agent performance data available.", intro_style)]
    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    
    agent_totals = {name: 0 for name in charts.agent_names}
    agent_peaks = {name: (0, 1) for name in charts.agent_names}
    
    for m_idx, m_data in enumerate(charts.monthly_agent_customers):
        for name in charts.agent_names:
            cnt = m_data.get(name, 0)
            agent_totals[name] += cnt
            if cnt > agent_peaks[name][0]:
                agent_peaks[name] = (cnt, m_idx + 1)
                
    sorted_agents = sorted(charts.agent_names, key=lambda x: agent_totals[x], reverse=True)
    top_agent = sorted_agents[0]
    top_agent_cust = agent_totals[top_agent]
    top_agent_peak_cust, top_agent_peak_month = agent_peaks[top_agent]
    
    other_agents = sorted_agents[1:4]
    other_agents_str = ", ".join(f"<b>{name}</b> ({agent_totals[name]} customers)" for name in other_agents if agent_totals[name] > 0)
    
    peak_acq_count = -1
    peak_acq_month = 1
    for m_idx, m_data in enumerate(charts.monthly_agent_customers):
        total = sum(m_data.values())
        if total > peak_acq_count:
            peak_acq_count = total
            peak_acq_month = m_idx + 1
            
    intro_txt = "This chart tracks customer acquisition performance by agent across each calendar month."
    bullets = [
        f"<b>{top_agent}</b> was the most active agent, securing a total of <b>{top_agent_cust} customers</b>, peaking with {top_agent_peak_cust} customers in {month_names[top_agent_peak_month - 1]}.",
        f"Other top performers by customer volume include: {other_agents_str}."
    ]
    concl_txt = f"Combined client acquisition reached its highest level in <b>{month_names[peak_acq_month - 1]}</b> with a total of <b>{peak_acq_count} active customers</b>."

    story_flow = []
    story_flow.append(Paragraph(intro_txt, intro_style))
    for b in bullets:
        story_flow.append(Paragraph(f"&bull;&nbsp;&nbsp;{b}", bullet_style))
    story_flow.append(Paragraph(concl_txt, intro_style))
    return story_flow


def _generate_commission_split_analysis(charts: FinanceChartData, intro_style, bullet_style) -> list[Paragraph]:
    if not charts.monthly_commission_split:
        return [Paragraph("No commission composition data available.", intro_style)]
    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    
    total_basic = sum(d.get("Basic", 0.0) for d in charts.monthly_commission_split)
    total_nfp = sum(d.get("NFP", 0.0) for d in charts.monthly_commission_split)
    total_anp = sum(d.get("ANP", 0.0) for d in charts.monthly_commission_split)
    grand = total_basic + total_nfp + total_anp
    
    pct_basic = (total_basic / grand * 100) if grand > 0 else 0.0
    pct_nfp = (total_nfp / grand * 100) if grand > 0 else 0.0
    pct_anp = (total_anp / grand * 100) if grand > 0 else 0.0
    
    peak_nfp_val = -1.0
    peak_nfp_month = 1
    for idx, d in enumerate(charts.monthly_commission_split):
        val = d.get("NFP", 0.0)
        if val > peak_nfp_val:
            peak_nfp_val = val
            peak_nfp_month = idx + 1
            
    intro_txt = "This chart illustrates the monthly composition of commission payouts across the three primary policies: Basic, Net Floor Price (NFP), and ANP."
    bullets = [
        f"<b>Basic Commission</b> remains the primary earnings driver, representing <b>RM {total_basic:,.2f}</b> ({pct_basic:.1f}% of total).",
        f"<b>NFP Commission</b> contributed <b>RM {total_nfp:,.2f}</b> ({pct_nfp:.1f}%), indicating strong performance in pricing optimization, peaking in <b>{month_names[peak_nfp_month - 1]}</b> at RM {peak_nfp_val:,.2f}.",
        f"<b>ANP Commission</b> contributed <b>RM {total_anp:,.2f}</b> ({pct_anp:.1f}%) from high-volume month tiers."
    ]
    concl_txt = f"Combined, the total consolidated commission paid out was <b>RM {grand:,.2f}</b>."

    story_flow = []
    story_flow.append(Paragraph(intro_txt, intro_style))
    for b in bullets:
        story_flow.append(Paragraph(f"&bull;&nbsp;&nbsp;{b}", bullet_style))
    story_flow.append(Paragraph(concl_txt, intro_style))
    return story_flow


def _create_kpi_card(title: str, value: str, width: float, height: float, title_style, value_style) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    cell_data = [[Paragraph(title, title_style)], [Paragraph(value, value_style)]]
    card = Table(cell_data, colWidths=[width], rowHeights=[height * 0.35, height * 0.65])
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#e2e8f0")),
                ("LINEABOVE", (0, 0), (-1, 0), 3, colors.HexColor("#1A365D")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return card


def format_cover_date(generated_at: str) -> str:
    from datetime import datetime
    raw = generated_at.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19] if " " in raw else raw, fmt).strftime("%d %B %Y")
        except ValueError:
            continue
    return raw


def build_matrix_table(
    title: str,
    comm_type: str,  # "Basic Commission" or "NFP Commission" or "ANP Commission"
    rows: list[list[str]],
    page_width: float,
    is_h2: bool = False,
) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    styles = getSampleStyleSheet()
    
    cell_style_left = ParagraphStyle("CellLeftMatrix", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_center = ParagraphStyle("CellCenterMatrix", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_CENTER, fontName=FONT_REGULAR)
    cell_style_right = ParagraphStyle("CellRightMatrix", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    
    header_style = ParagraphStyle("StackedHeaderMatrix", parent=styles["Normal"], fontSize=8, leading=9, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style = ParagraphStyle("StackedSubHeaderMatrix", parent=styles["Normal"], fontSize=7, leading=8, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)

    # NFP specific styles
    cell_style_left_nfp = ParagraphStyle("CellLeftMatrixNfp", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_center_nfp = ParagraphStyle("CellCenterMatrixNfp", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_CENTER, fontName=FONT_REGULAR)
    cell_style_right_nfp = ParagraphStyle("CellRightMatrixNfp", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    
    header_style_nfp = ParagraphStyle("StackedHeaderMatrixNfp", parent=styles["Normal"], fontSize=7.0, leading=8.0, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style_nfp = ParagraphStyle("StackedSubHeaderMatrixNfp", parent=styles["Normal"], fontSize=5.8, leading=6.8, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)

    rows = [list(r) for r in rows]
    # Agent column (col 0) -> canonical full name for display. Done before the
    # span logic below so identical agents still merge correctly.
    if _agent_names:
        for _r in rows:
            if _r and isinstance(_r[0], str) and _r[0].strip() and _r[0].strip() not in ("-", "Total"):
                _r[0] = _agent_names.resolve(_r[0].strip())
    has_customer = False
    if rows:
        if len(rows[0]) == 15 or len(rows[0]) == 28:
            has_customer = True
        elif len(rows[0]) == 14 and comm_type == "ANP Commission" and "Customer" in title:
            has_customer = True

    if has_customer and rows:
        current_agent = ""
        span_count = 0
        for r_idx in range(len(rows)):
            val = rows[r_idx][0]
            if val != "" and val is not None and val != "Total":
                current_agent = val
                span_count = 1
            elif val == "Total":
                current_agent = ""
                span_count = 0
            else:
                span_count += 1
                if span_count > 10:
                    rows[r_idx][0] = current_agent
                    span_count = 1

    if comm_type == "NFP Commission":
        if has_customer:
            # 28 columns (Agent, Customer, Invoice Date, Commission Rate, and 6 months of 4 columns each)
            col_ratios = [1.8, 1.8, 1.2, 0.8] + [0.9, 0.9, 0.9, 0.9] * 6
            total_ratio = sum(col_ratios)
            col_widths = [page_width * r / total_ratio for r in col_ratios]
            
            months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
            row1 = [
                Paragraph("Agent", header_style_nfp),
                Paragraph("Customer", header_style_nfp),
                Paragraph("Invoice Date", header_style_nfp),
                Paragraph("Commission Rate", header_style_nfp),
                Paragraph(months_names[0], header_style_nfp), "", "", "",
                Paragraph(months_names[1], header_style_nfp), "", "", "",
                Paragraph(months_names[2], header_style_nfp), "", "", "",
                Paragraph(months_names[3], header_style_nfp), "", "", "",
                Paragraph(months_names[4], header_style_nfp), "", "", "",
                Paragraph(months_names[5], header_style_nfp), "", "", ""
            ]
            row2 = [
                "", "", "", "",
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp)
            ]
            t_styles = [
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (1, 0), (1, 1)),
                ("SPAN", (2, 0), (2, 1)),
                ("SPAN", (3, 0), (3, 1)),
                ("SPAN", (4, 0), (7, 0)),
                ("SPAN", (8, 0), (11, 0)),
                ("SPAN", (12, 0), (15, 0)),
                ("SPAN", (16, 0), (19, 0)),
                ("SPAN", (20, 0), (23, 0)),
                ("SPAN", (24, 0), (27, 0)),
            ]
        else:
            # 26 columns (System Price and Net Floor Price added for every month)
            col_ratios = [1.8, 0.8] + [0.9, 0.9, 0.9, 0.9] * 6
            total_ratio = sum(col_ratios)
            col_widths = [page_width * r / total_ratio for r in col_ratios]
            
            months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
            row1 = [
                Paragraph("Agent Name", header_style_nfp),
                Paragraph("Commission Rate", header_style_nfp),
                Paragraph(months_names[0], header_style_nfp), "", "", "",
                Paragraph(months_names[1], header_style_nfp), "", "", "",
                Paragraph(months_names[2], header_style_nfp), "", "", "",
                Paragraph(months_names[3], header_style_nfp), "", "", "",
                Paragraph(months_names[4], header_style_nfp), "", "", "",
                Paragraph(months_names[5], header_style_nfp), "", "", ""
            ]
            row2 = [
                "", "",
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp),
                Paragraph("Sales Price", sub_header_style_nfp), Paragraph("System Price", sub_header_style_nfp), Paragraph("Net Floor Price", sub_header_style_nfp), Paragraph("NFP Commission", sub_header_style_nfp)
            ]
            t_styles = [
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (1, 0), (1, 1)),
                ("SPAN", (2, 0), (5, 0)),
                ("SPAN", (6, 0), (9, 0)),
                ("SPAN", (10, 0), (13, 0)),
                ("SPAN", (14, 0), (17, 0)),
                ("SPAN", (18, 0), (21, 0)),
                ("SPAN", (22, 0), (25, 0)),
            ]
    elif has_customer:
        if comm_type == "ANP Commission":
            # 14 columns: Agent, Customer, and 6 months of (Sales Price, ANP Commission)
            col_ratios = [1.8, 2.7] + [1.1, 1.1] * 6
            total_ratio = sum(col_ratios)
            col_widths = [page_width * r / total_ratio for r in col_ratios]
            
            months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
            row1 = [
                Paragraph("Agent", header_style),
                Paragraph("Customer", header_style),
                Paragraph(months_names[0], header_style), "",
                Paragraph(months_names[1], header_style), "",
                Paragraph(months_names[2], header_style), "",
                Paragraph(months_names[3], header_style), "",
                Paragraph(months_names[4], header_style), "",
                Paragraph(months_names[5], header_style), ""
            ]
            row2 = [
                "", "",
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style)
            ]
            t_styles = [
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (1, 0), (1, 1)),
                ("SPAN", (2, 0), (3, 0)),
                ("SPAN", (4, 0), (5, 0)),
                ("SPAN", (6, 0), (7, 0)),
                ("SPAN", (8, 0), (9, 0)),
                ("SPAN", (10, 0), (11, 0)),
                ("SPAN", (12, 0), (13, 0)),
            ]
        else:
            # Customer Basic Commission (15 columns)
            col_ratios = [1.8, 1.8, 0.9] + [1.1, 1.1] * 6
            total_ratio = sum(col_ratios)
            col_widths = [page_width * r / total_ratio for r in col_ratios]
            
            months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
            row1 = [
                Paragraph("Agent", header_style),
                Paragraph("Customer", header_style),
                Paragraph("Commission Rate", header_style),
                Paragraph(months_names[0], header_style), "",
                Paragraph(months_names[1], header_style), "",
                Paragraph(months_names[2], header_style), "",
                Paragraph(months_names[3], header_style), "",
                Paragraph(months_names[4], header_style), "",
                Paragraph(months_names[5], header_style), ""
            ]
            row2 = [
                "", "", "",
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style),
                Paragraph("Sales Price", sub_header_style), Paragraph(comm_type, sub_header_style)
            ]
            t_styles = [
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (1, 0), (1, 1)),
                ("SPAN", (2, 0), (2, 1)),
                ("SPAN", (3, 0), (4, 0)),
                ("SPAN", (5, 0), (6, 0)),
                ("SPAN", (7, 0), (8, 0)),
                ("SPAN", (9, 0), (10, 0)),
                ("SPAN", (11, 0), (12, 0)),
                ("SPAN", (13, 0), (14, 0)),
            ]
    elif comm_type == "ANP Commission":
        # 13 columns (ANP Tier removed)
        col_ratios = [2.5] + [1.3, 1.3] * 6
        total_ratio = sum(col_ratios)
        col_widths = [page_width * r / total_ratio for r in col_ratios]
        
        months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
        row1 = [
            Paragraph("Agent Name", header_style),
            Paragraph(months_names[0], header_style), "",
            Paragraph(months_names[1], header_style), "",
            Paragraph(months_names[2], header_style), "",
            Paragraph(months_names[3], header_style), "",
            Paragraph(months_names[4], header_style), "",
            Paragraph(months_names[5], header_style), ""
        ]
        row2 = [
            "",
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("ANP Commission", sub_header_style)
        ]
        t_styles = [
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (2, 0)),
            ("SPAN", (3, 0), (4, 0)),
            ("SPAN", (5, 0), (6, 0)),
            ("SPAN", (7, 0), (8, 0)),
            ("SPAN", (9, 0), (10, 0)),
            ("SPAN", (11, 0), (12, 0)),
        ]
    else:
        # Basic Commission (14 columns)
        col_ratios = [2.5, 1.1] + [1.3, 1.3] * 6
        total_ratio = sum(col_ratios)
        col_widths = [page_width * r / total_ratio for r in col_ratios]
        
        months_names = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if is_h2 else ["Jan", "Feb", "Mac", "Apr", "May", "Jun"]
        row1 = [
            Paragraph("Agent Name", header_style),
            Paragraph("Commission Rate", header_style),
            Paragraph(months_names[0], header_style), "",
            Paragraph(months_names[1], header_style), "",
            Paragraph(months_names[2], header_style), "",
            Paragraph(months_names[3], header_style), "",
            Paragraph(months_names[4], header_style), "",
            Paragraph(months_names[5], header_style), ""
        ]
        row2 = [
            "", "",
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("Basic Commission", sub_header_style)
        ]
        t_styles = [
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (1, 1)),
            ("SPAN", (2, 0), (3, 0)),
            ("SPAN", (4, 0), (5, 0)),
            ("SPAN", (6, 0), (7, 0)),
            ("SPAN", (8, 0), (9, 0)),
            ("SPAN", (10, 0), (11, 0)),
            ("SPAN", (12, 0), (13, 0)),
        ]
        
    table_data = [row1, row2]
    
    for row in rows:
        formatted_row = []
        is_total = (row[0] == "Total")
        for i, val in enumerate(row):
            if comm_type == "ANP Commission" and not has_customer and i == 1:
                continue # Skip the ANP Tier column
            
            # Determine cell style based on comm_type and column index
            if comm_type == "NFP Commission":
                if has_customer:
                    if i in (0, 1):
                        style = ParagraphStyle("BoldLeftMatrixNfpCust", parent=cell_style_left_nfp, fontName=FONT_BOLD) if is_total else cell_style_left_nfp
                    elif i in (2, 3):
                        style = cell_style_center_nfp
                    else:
                        style = ParagraphStyle("BoldRightMatrixNfpCust", parent=cell_style_right_nfp, fontName=FONT_BOLD) if is_total else cell_style_right_nfp
                else:
                    if i == 0:
                        style = ParagraphStyle("BoldLeftMatrixNfp", parent=cell_style_left_nfp, fontName=FONT_BOLD) if is_total else cell_style_left_nfp
                    elif i == 1:
                        style = cell_style_center_nfp
                    else:
                        style = ParagraphStyle("BoldRightMatrixNfp", parent=cell_style_right_nfp, fontName=FONT_BOLD) if is_total else cell_style_right_nfp
            else:
                if has_customer:
                    if comm_type == "ANP Commission":
                        if i in (0, 1):
                            style = ParagraphStyle("BoldLeftMatrixCust", parent=cell_style_left, fontName=FONT_BOLD) if is_total else cell_style_left
                        else:
                            style = ParagraphStyle("BoldRightMatrixCust", parent=cell_style_right, fontName=FONT_BOLD) if is_total else cell_style_right
                    else:
                        if i in (0, 1):
                            style = ParagraphStyle("BoldLeftMatrixCust", parent=cell_style_left, fontName=FONT_BOLD) if is_total else cell_style_left
                        elif i == 2:
                            style = cell_style_center
                        else:
                            style = ParagraphStyle("BoldRightMatrixCust", parent=cell_style_right, fontName=FONT_BOLD) if is_total else cell_style_right
                else:
                    if i == 0:
                        style = ParagraphStyle("BoldLeftMatrix", parent=cell_style_left, fontName=FONT_BOLD) if is_total else cell_style_left
                    elif (comm_type != "ANP Commission" and i == 1):
                        style = cell_style_center
                    else:
                        style = ParagraphStyle("BoldRightMatrix", parent=cell_style_right, fontName=FONT_BOLD) if is_total else cell_style_right
            
            formatted_row.append(Paragraph(val, style))
        table_data.append(formatted_row)
        
    table = Table(table_data, colWidths=col_widths, repeatRows=2)
    
    t_styles.extend([
        ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#1A365D")),
        ("ALIGN", (0, 0), (-1, 1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, 1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 1), 4),
        
        ("VALIGN", (0, 2), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 2), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 2), (-1, -1), 3),
    ])
    
    if has_customer:
        agent_start = 0
        while agent_start < len(rows):
            if rows[agent_start][0] == "Total":
                break
            agent_end = agent_start
            while (agent_end + 1 < len(rows) and 
                   (rows[agent_end + 1][0] == "" or rows[agent_end + 1][0] is None) and 
                   rows[agent_end + 1][0] != "Total"):
                agent_end += 1
            if agent_end > agent_start:
                # Add 2 for the header row offset
                t_styles.append(("SPAN", (0, agent_start + 2), (0, agent_end + 2)))
                t_styles.append(("VALIGN", (0, agent_start + 2), (0, agent_end + 2), "TOP"))
            agent_start = agent_end + 1
    
    # Alternating row colors for data rows (excluding headers and Total row)
    for r in range(2, len(table_data) - 1):
        if r % 2 == 0:
            t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
            
    # Highlight Total row
    t_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EDF2F7")))
    t_styles.append(("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.HexColor("#1A365D")))
    
    table.setStyle(TableStyle(t_styles))
    return table


def build_nfp_matrix_tables(rows: list[list[str]], page_width: float) -> list[Table]:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    # Agent column (col 0) -> canonical full name for display.
    rows = [list(r) for r in rows]
    if _agent_names:
        for _r in rows:
            if _r and isinstance(_r[0], str) and _r[0].strip() and _r[0].strip() not in ("-", "Total"):
                _r[0] = _agent_names.resolve(_r[0].strip())

    styles = getSampleStyleSheet()

    cell_style_left = ParagraphStyle("NfpLeft", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_center = ParagraphStyle("NfpCenter", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_CENTER, fontName=FONT_REGULAR)
    cell_style_right = ParagraphStyle("NfpRight", parent=styles["Normal"], fontSize=6.5, leading=7.5, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    
    header_style = ParagraphStyle("NfpHeader", parent=styles["Normal"], fontSize=7.5, leading=8.5, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style = ParagraphStyle("NfpSubHeader", parent=styles["Normal"], fontSize=6.5, leading=7.5, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)

    col_ratios = [2.2, 1.0] + [1.1, 1.1, 1.1, 1.1] * 2
    total_ratio = sum(col_ratios)
    col_widths = [page_width * r / total_ratio for r in col_ratios]

    def make_table(month1_name: str, month2_name: str, slice_start: int, slice_end: int) -> Table:
        row1 = [
            Paragraph("Agent Name", header_style),
            Paragraph("Commission Rate", header_style),
            Paragraph(month1_name, header_style), "", "", "",
            Paragraph(month2_name, header_style), "", "", ""
        ]
        row2 = [
            "", "",
            Paragraph("Sales Price", sub_header_style), Paragraph("System Price", sub_header_style), Paragraph("Net Floor Price", sub_header_style), Paragraph("NFP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("System Price", sub_header_style), Paragraph("Net Floor Price", sub_header_style), Paragraph("NFP Commission", sub_header_style)
        ]
        
        t_styles = [
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (1, 1)),
            ("SPAN", (2, 0), (5, 0)),
            ("SPAN", (6, 0), (9, 0)),
        ]
        
        table_data = [row1, row2]
        for row in rows:
            is_total = (row[0] == "Total")
            formatted_row = []
            
            # Agent Name
            style = ParagraphStyle("NfpBoldLeft", parent=cell_style_left, fontName=FONT_BOLD) if is_total else cell_style_left
            formatted_row.append(Paragraph(row[0], style))
            
            # Commission Rate
            rate_val = row[1]
            if not is_total and all(val == "-" or val == "" for val in row[slice_start:slice_end]):
                rate_val = "-"
            formatted_row.append(Paragraph(rate_val, cell_style_center))
            
            # Month slices
            for val in row[slice_start:slice_end]:
                style = ParagraphStyle("NfpBoldRight", parent=cell_style_right, fontName=FONT_BOLD) if is_total else cell_style_right
                formatted_row.append(Paragraph(val, style))
                
            table_data.append(formatted_row)
            
        t = Table(table_data, colWidths=col_widths, repeatRows=2)
        
        t_styles.extend([
            ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#1A365D")),
            ("ALIGN", (0, 0), (-1, 1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
            ("TOPPADDING", (0, 0), (-1, 1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, 1), 2.5),
            
            ("VALIGN", (0, 2), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 2), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 2), (-1, -1), 1.5),
        ])
        
        for r in range(2, len(table_data) - 1):
            if r % 2 == 0:
                t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
                
        t_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EDF2F7")))
        t_styles.append(("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.HexColor("#1A365D")))
        
        t.setStyle(TableStyle(t_styles))
        return t

    cols_count = len(rows[0]) if rows else 0
    is_full_year = (cols_count > 30)
    if is_full_year:
        slices = [
            ("Jan", "Feb", 2, 10),
            ("Mac", "Apr", 10, 18),
            ("May", "Jun", 18, 26),
            ("Jul", "Aug", 26, 34),
            ("Sep", "Oct", 34, 42),
            ("Nov", "Dec", 42, 50)
        ]
    else:
        slices = [
            ("Jan", "Feb", 2, 10),
            ("Mac", "Apr", 10, 18),
            ("May", "Jun", 18, 26)
        ]
    return [make_table(m1, m2, s_start, s_end) for m1, m2, s_start, s_end in slices]


def build_nfp_customer_matrix_tables(rows: list[list[str]], page_width: float) -> list[Table]:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    styles = getSampleStyleSheet()
    
    cell_style_left = ParagraphStyle("NfpCustLeft", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_center = ParagraphStyle("NfpCustCenter", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_CENTER, fontName=FONT_REGULAR)
    cell_style_right = ParagraphStyle("NfpCustRight", parent=styles["Normal"], fontSize=5.8, leading=6.8, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    
    header_style = ParagraphStyle("NfpCustHeader", parent=styles["Normal"], fontSize=7.0, leading=8.0, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style = ParagraphStyle("NfpCustSubHeader", parent=styles["Normal"], fontSize=5.8, leading=6.8, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)

    col_ratios = [1.8, 1.8, 1.2, 0.8] + [0.9, 0.9, 0.9, 0.9] * 2
    total_ratio = sum(col_ratios)
    col_widths = [page_width * r / total_ratio for r in col_ratios]

    rows = [list(r) for r in rows]
    # Agent column (col 0) -> canonical full name for display.
    if _agent_names:
        for _r in rows:
            if _r and isinstance(_r[0], str) and _r[0].strip() and _r[0].strip() not in ("-", "Total"):
                _r[0] = _agent_names.resolve(_r[0].strip())
    if rows:
        current_agent = ""
        span_count = 0
        for r_idx in range(len(rows)):
            val = rows[r_idx][0]
            if val != "" and val is not None and val != "Total":
                current_agent = val
                span_count = 1
            elif val == "Total":
                current_agent = ""
                span_count = 0
            else:
                span_count += 1
                if span_count > 10:
                    rows[r_idx][0] = current_agent
                    span_count = 1

    def make_table(month1_name: str, month2_name: str, slice_start: int, slice_end: int) -> Table:
        row1 = [
            Paragraph("Agent", header_style),
            Paragraph("Customer", header_style),
            Paragraph("Invoice Date", header_style),
            Paragraph("Commission Rate", header_style),
            Paragraph(month1_name, header_style), "", "", "",
            Paragraph(month2_name, header_style), "", "", ""
        ]
        row2 = [
            "", "", "", "",
            Paragraph("Sales Price", sub_header_style), Paragraph("System Price", sub_header_style), Paragraph("Net Floor Price", sub_header_style), Paragraph("NFP Commission", sub_header_style),
            Paragraph("Sales Price", sub_header_style), Paragraph("System Price", sub_header_style), Paragraph("Net Floor Price", sub_header_style), Paragraph("NFP Commission", sub_header_style)
        ]
        
        t_styles = [
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (1, 1)),
            ("SPAN", (2, 0), (2, 1)),
            ("SPAN", (3, 0), (3, 1)),
            ("SPAN", (4, 0), (7, 0)),
            ("SPAN", (8, 0), (11, 0)),
        ]
        
        table_data = [row1, row2]
        for row in rows:
            is_total = (row[0] == "Total")
            formatted_row = []
            
            # Agent Name
            style = ParagraphStyle("NfpCustBoldLeft", parent=cell_style_left, fontName=FONT_BOLD) if is_total else cell_style_left
            formatted_row.append(Paragraph(row[0], style))
            
            # Customer Name
            formatted_row.append(Paragraph(row[1], style))
            
            # Invoice Date
            formatted_row.append(Paragraph(row[2], cell_style_center))
            
            # Commission Rate
            rate_val = row[3]
            if not is_total and all(val == "-" or val == "" for val in row[slice_start:slice_end]):
                rate_val = "-"
            formatted_row.append(Paragraph(rate_val, cell_style_center))
            
            # Month slices
            for val in row[slice_start:slice_end]:
                style = ParagraphStyle("NfpCustBoldRight", parent=cell_style_right, fontName=FONT_BOLD) if is_total else cell_style_right
                formatted_row.append(Paragraph(val, style))
                
            table_data.append(formatted_row)
            
        t = Table(table_data, colWidths=col_widths, repeatRows=2)
        
        t_styles.extend([
            ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#1A365D")),
            ("ALIGN", (0, 0), (-1, 1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
            ("TOPPADDING", (0, 0), (-1, 1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, 1), 2.5),
            
            ("VALIGN", (0, 2), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 2), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 2), (-1, -1), 1.5),
        ])
        
        # Agent Name vertical span
        agent_start = 0
        while agent_start < len(rows):
            if rows[agent_start][0] == "Total":
                break
            agent_end = agent_start
            while (agent_end + 1 < len(rows) and 
                   (rows[agent_end + 1][0] == "" or rows[agent_end + 1][0] is None) and 
                   rows[agent_end + 1][0] != "Total"):
                agent_end += 1
            if agent_end > agent_start:
                t_styles.append(("SPAN", (0, agent_start + 2), (0, agent_end + 2)))
                t_styles.append(("VALIGN", (0, agent_start + 2), (0, agent_end + 2), "TOP"))
            agent_start = agent_end + 1

        for r in range(2, len(table_data) - 1):
            if r % 2 == 0:
                t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
                
        t_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EDF2F7")))
        t_styles.append(("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.HexColor("#1A365D")))
        
        t.setStyle(TableStyle(t_styles))
        return t

    cols_count = len(rows[0]) if rows else 0
    is_full_year = (cols_count > 30)
    if is_full_year:
        slices = [
            ("Jan", "Feb", 4, 12),
            ("Mac", "Apr", 12, 20),
            ("May", "Jun", 20, 28),
            ("Jul", "Aug", 28, 36),
            ("Sep", "Oct", 36, 44),
            ("Nov", "Dec", 44, 52)
        ]
    else:
        slices = [
            ("Jan", "Feb", 4, 12),
            ("Mac", "Apr", 12, 20),
            ("May", "Jun", 20, 28)
        ]
    return [make_table(m1, m2, s_start, s_end) for m1, m2, s_start, s_end in slices]



def _create_split_legend(width: float) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    
    styles = getSampleStyleSheet()
    style = ParagraphStyle("SplitLegendText", parent=styles["Normal"], fontSize=8, fontName=FONT_BOLD, textColor=colors.HexColor("#4a5568"))
    
    def make_item(color_hex, label):
        cell_table = Table([["", Paragraph(label, style)]], colWidths=[10, None])
        cell_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(color_hex)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (1, 0), (1, 0), 6),
        ]))
        return cell_table

    t = Table([[make_item("#2c5282", "Basic Commission"), 
                make_item("#319795", "Net Floor Price (NFP) Commission"), 
                make_item("#d69e2e", "ANP Commission")]], colWidths=[width/3, width/3, width/3])
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def build_trend_summary_table(charts: FinanceChartData, page_width: float) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    monthly_trend = charts.monthly_trend or []
    monthly_agent_customers = charts.monthly_agent_customers or []

    styles = getSampleStyleSheet()
    
    cell_style_left = ParagraphStyle("TrendTabLeft", parent=styles["Normal"], fontSize=5.0, leading=6.0, alignment=TA_LEFT, fontName=FONT_REGULAR)
    cell_style_right = ParagraphStyle("TrendTabRight", parent=styles["Normal"], fontSize=5.0, leading=6.0, alignment=TA_RIGHT, fontName=FONT_REGULAR)
    cell_style_center = ParagraphStyle("TrendTabCenter", parent=styles["Normal"], fontSize=5.0, leading=6.0, alignment=TA_CENTER, fontName=FONT_REGULAR)
    
    header_style = ParagraphStyle("TrendTabHeader", parent=styles["Normal"], fontSize=5.5, leading=6.5, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    sub_header_style = ParagraphStyle("TrendTabSubHeader", parent=styles["Normal"], fontSize=4.2, leading=5.2, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    cust_sub_header_style = ParagraphStyle("TrendTabCustSubHeader", parent=styles["Normal"], fontSize=3.8, leading=4.8, fontName=FONT_BOLD, textColor=colors.white, alignment=TA_CENTER)
    
    # 19 columns: Agent (1) + 6 months * 3 sub-columns (18)
    col_ratios = [2.0] + [1.1, 1.1, 1.3] * 6
    total_ratio = sum(col_ratios)
    col_widths = [page_width * r / total_ratio for r in col_ratios]
    
    row0 = [
        Paragraph("Agent", header_style),
        Paragraph("Jan", header_style), "", "",
        Paragraph("Feb", header_style), "", "",
        Paragraph("Mac", header_style), "", "",
        Paragraph("Apr", header_style), "", "",
        Paragraph("May", header_style), "", "",
        Paragraph("Jun", header_style), "", ""
    ]
    row1 = [
        "",
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style),
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style),
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style),
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style),
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style),
        Paragraph("Customer", cust_sub_header_style), Paragraph("Total Sales", sub_header_style), Paragraph("Total Commission", sub_header_style)
    ]
    table_data = [row0, row1]
    
    # Extract agent names with non-zero commission
    all_agents = set()
    for m_data in monthly_trend:
        all_agents.update(m_data.get("agent_commissions", {}).keys())
    
    # Calculate total commission for each agent to sort in descending order
    agent_totals = {}
    for agent in all_agents:
        total = 0.0
        for m_idx in range(6):
            if m_idx < len(monthly_trend):
                total += monthly_trend[m_idx].get("agent_commissions", {}).get(agent, 0.0)
        agent_totals[agent] = total
        
    sorted_agents = sorted(list(all_agents), key=lambda x: (-agent_totals[x], x.strip().lower()))
    agent_names_alpha = sorted(list(all_agents), key=lambda x: x.strip().lower())
    
    agent_palette = ["#1a365d", "#2b6cb0", "#319795", "#d69e2e", "#805ad5", "#dd6b20", "#38a169", "#e53e3e", "#4a5568", "#b83280", "#2c5282", "#2c7a7b", "#744210"]
    
    for idx, agent in enumerate(sorted_agents):
        color_idx = agent_names_alpha.index(agent)
        color = agent_palette[color_idx % len(agent_palette)]
        agent_text = f'<font color="{color}"><b>&#9632;</b></font>&nbsp;&nbsp;{agent}'
        row = [Paragraph(agent_text, cell_style_left)]
        for m_idx in range(6):
            cust_cnt = 0
            sales = 0.0
            comm = 0.0
            if m_idx < len(monthly_trend):
                comm = monthly_trend[m_idx].get("agent_commissions", {}).get(agent, 0.0)
            if monthly_agent_customers and m_idx < len(monthly_agent_customers):
                cust_cnt = monthly_agent_customers[m_idx].get(agent, 0)
            if charts.monthly_agent_sales and m_idx < len(charts.monthly_agent_sales):
                sales = charts.monthly_agent_sales[m_idx].get(agent, 0.0)
                
            cust_text = str(cust_cnt) if cust_cnt > 0 else "-"
            sales_text = f"{sales:,.0f}" if sales > 0 else "-"
            comm_text = f"{comm:,.0f}" if comm > 0 else "-"
            
            row.append(Paragraph(cust_text, cell_style_center))
            row.append(Paragraph(sales_text, cell_style_right))
            row.append(Paragraph(comm_text, cell_style_right))
        table_data.append(row)
        
    t = Table(table_data, colWidths=col_widths)
    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A365D")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#2C5282")),
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (1, 0), (3, 0)),
        ("SPAN", (4, 0), (6, 0)),
        ("SPAN", (7, 0), (9, 0)),
        ("SPAN", (10, 0), (12, 0)),
        ("SPAN", (13, 0), (15, 0)),
        ("SPAN", (16, 0), (18, 0)),
        ("ALIGN", (0, 0), (-1, 1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 1.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.0),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
    ]
    # Apply alternating colors to agent rows, starting at row index 2
    for r in range(2, len(table_data)):
        if r % 2 == 1:
            t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
            
    t.setStyle(TableStyle(t_styles))
    return t

def load_internal_hierarchy() -> list[dict[str, Any]]:
    """Senior -> executives groups from the dashboard's Agent Roles & Hierarchy
    page (agent_roles: Internal rows with Reports To filled). Falls back to a
    hardcoded snapshot when the table is unreachable or has no groups."""
    fallback = [
        {"senior": "Teng Kah Kent", "start_date": "01 Jul 2025", "executives": ["Louis Ng", "Anisah Najwa", "Najwa"]},
        {"senior": "Sunny Tan", "start_date": "01 Jul 2025", "executives": ["Jia Keat", "Zulkarnain", "Denise", "Jia Xuan"]},
        {"senior": "Zhe Hang", "start_date": "01 May 2025", "executives": ["Joshua Yap Jia Hao"]},
        {"senior": "Martin Hing", "start_date": "01 Feb 2026", "executives": ["Js"]}
    ]

    try:
        import sys as _sys
        rates_dir = str(Path(__file__).resolve().parent / "1. Basic Commission" / "3. Python Script")
        if rates_dir not in _sys.path:
            _sys.path.insert(0, rates_dir)
        from basic_commission_rates import _load_db_table, effective_start
        rows = _load_db_table("agent_roles")
    except Exception as e:
        print(f"Error loading internal hierarchy from agent_roles: {e}")
        return fallback

    def _fmt_month(eff: str) -> str | None:
        # A range ("2026-01 to 2026-08") prints as the month it started, which
        # is what "start_date" on the chart means.
        eff = effective_start(eff)
        try:
            from datetime import datetime as _dt
            return _dt.strptime(eff, "%Y-%m").strftime("01 %b %Y")
        except ValueError:
            return eff or None

    # Per agent: the latest non-hidden Internal row overall (for start dates),
    # and the latest one whose Reports To is filled (for grouping). Re-seeded
    # rows often arrive with a blank Reports To, so requiring the very latest
    # row to carry it would silently drop agents from the chart.
    latest: dict[str, dict] = {}
    latest_with_rt: dict[str, dict] = {}
    for r in rows:
        if r.get("hidden"):
            continue
        if str(r.get("agent_type") or "").strip().lower() != "internal":
            continue
        agent = str(r.get("agent") or "").strip()
        if not agent:
            continue
        key = agent.lower()
        # An effective month may be a closed range ("2026-01 to 2026-08"), so
        # rank on the start month — the raw cell sorts a range above the plain
        # month it starts in, on string length alone.
        eff = effective_start(r.get("effective_from"))
        prev = latest.get(key)
        if prev is None or eff > effective_start(prev.get("effective_from")):
            latest[key] = r
        if str(r.get("reports_to") or "").strip():
            prev = latest_with_rt.get(key)
            if prev is None or eff > effective_start(prev.get("effective_from")):
                latest_with_rt[key] = r

    display = (_agent_names.resolve if _agent_names else (lambda n: n))
    groups: dict[str, dict[str, Any]] = {}
    for r in latest_with_rt.values():
        senior = str(r.get("reports_to") or "").strip()
        if not senior:
            continue
        skey = senior.lower()
        if skey not in groups:
            senior_row = latest.get(skey)
            groups[skey] = {
                "senior": display(senior),
                "start_date": _fmt_month(senior_row.get("effective_from")) if senior_row else None,
                "executives": [],
            }
        groups[skey]["executives"].append(display(str(r.get("agent") or "").strip()))

    if not groups:
        return fallback
    out = sorted(groups.values(), key=lambda g: str(g["senior"]).lower())
    for g in out:
        g["executives"].sort(key=str.lower)
    return out
def load_outsource_hierarchy() -> list[dict]:
    """Returns outsource org groups for rendering. Each group has role, name, and executives."""
    return [
        {"role": "OGM", "name": "Sam", "executives": [
            {"name": "Chong Ka Seng", "sub": None},
            {"name": "Loo Chew Yin", "sub": "Lai Ka Kit"},
        ]},
        {"role": "OUM", "name": "Oliver Koh", "executives": [
            {"name": "Koh Yeong Cherng", "sub": None},
            {"name": "Ang Kok Xing", "sub": None},
            {"name": "Tey Zhi Yun", "sub": None},
            {"name": "Mohd Azhar Bin Ibrahim", "sub": "Mohd Hanis Bin Marjian"},
            {"name": "Lim Chin Seng", "sub": None},
        ]},
        {"role": "OUM", "name": "Carol Siow", "executives": [
            {"name": "Liew Lee Ching", "sub": "Tan Sue Cherk"},
            {"name": "Low Chin Chai", "sub": "Low Kim Swee"},
            {"name": "Chang Soon Huat", "sub": "Lai Siong Hing"},
        ]},
        {"role": "OUM", "name": "Dean Wai", "executives": [
            {"name": "Lam Wai Leng", "sub": None},
            {"name": "Tee Kok Kian", "sub": None},
        ]},
        {"role": "OUM", "name": "Chan Wing On", "executives": [
            {"name": "Kwong Jun Sheng", "sub": None},
            {"name": "Lee Yue Peng", "sub": None},
            {"name": "Too Pok Jen", "sub": None},
        ]},
        {"role": "OUM", "name": "Chan Jia Wei", "executives": [
            {"name": "Tay Hock Xiang", "sub": None},
            {"name": "Ho Wen Lin", "sub": None},
            {"name": "Ng Zhee Hao", "sub": None},
        ]},
        {"role": "OUM", "name": "Ling Liang Kang", "executives": [
            {"name": "Tan Wei Hung", "sub": None},
        ]},
        {"role": "OUM", "name": "Caryn Dong", "executives": [
            {"name": "Lim Kai Zhe", "sub": None},
            {"name": "Teoh Chun Xun", "sub": None},
            {"name": "Yew Jin Yuen", "sub": None},
        ]},
        {"role": "OUM", "name": "Dennis Ong", "executives": [
            {"name": "Oh Kai Wei", "sub": None},
        ]},
    ]

def make_internal_group_row(group: dict[str, Any], width: float, styles) -> Table:
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    
    senior_style = ParagraphStyle(
        "RowSenior_" + group['senior'].replace(" ", "_"),
        parent=styles["Normal"],
        fontSize=11,
        leading=13,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1A365D"),
    )
    exec_style = ParagraphStyle(
        "RowExec_" + group['senior'].replace(" ", "_"),
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        fontName=FONT_REGULAR,
        textColor=colors.HexColor("#2D3748")
    )
    
    right_flowables = []
    for ex in group["executives"]:
        right_flowables.append(Paragraph(f"&bull;&nbsp;&nbsp;{ex}", exec_style))
        
    if not right_flowables:
        right_flowables.append(Paragraph("&bull;&nbsp;&nbsp;(no executives)", exec_style))
        
    row_data = [[Paragraph(f"<b>{group['senior']}</b>", senior_style), right_flowables]]
    row_table = Table(row_data, colWidths=[width * 0.35, width * 0.65], hAlign='LEFT')
    row_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return row_table

def write_finance_presentation_pdf(
    path: Path,
    *,
    title: str,
    year: int,
    generated_at: str,
    period_subtitle: str = "for first half year",
    cover_date: str | None = None,
    logo_path: str | Path | None = None,
    meta_lines: Sequence[tuple[str, str]],
    sections: Sequence[PdfSection],
    charts: FinanceChartData,
    is_outsource: bool = False,
) -> Path:
    """Finance presentation PDF: cover, dashboard with charts, then one table per page."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT, TA_JUSTIFY
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            BaseDocTemplate,
            Frame,
            NextPageTemplate,
            PageBreak,
            PageTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
        )
        from reportlab.platypus.flowables import Flowable
        from reportlab.lib.utils import ImageReader

        class ChartFlowable(Flowable):
            def __init__(self, drawing, width, height):
                self.drawing = drawing
                self.width = width
                self.height = height

            def wrap(self, availWidth, availHeight):
                return self.width, self.height

            def draw(self):
                self.drawing.drawOn(self.canv, 0, 0)

    except ImportError as exc:
        raise ImportError(
            "PDF export requires reportlab. Install with: pip install reportlab"
        ) from exc

    NAVY = colors.HexColor("#1A365D")
    TEAL = colors.HexColor("#319795")
    PANEL_BG = colors.HexColor("#F7FAFC")
    RULE_CLR = colors.HexColor("#E2E8F0")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    margin = 0.55 * inch
    header_h = 0.55 * inch
    landscape_size = landscape(A4)
    lw, lh = landscape_size

    doc = BaseDocTemplate(
        str(path),
        pagesize=landscape_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )

    landscape_frame = Frame(
        margin,
        margin,
        lw - 2 * margin,
        lh - 2 * margin - header_h,
        id="landscape_frame",
    )
    doc.addPageTemplates(
        [
            PageTemplate(
                id="landscape",
                frames=[landscape_frame],
                pagesize=landscape_size,
            ),
        ]
    )

    styles = getSampleStyleSheet()
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Heading1"],
        fontSize=22,
        leading=26,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#1a365d"),
        fontName=FONT_BOLD,
        spaceAfter=12,
    )
    cover_sub = ParagraphStyle(
        "CoverSub",
        parent=styles["Normal"],
        fontSize=12,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4a5568"),
        fontName=FONT_REGULAR,
    )
    cover_meta_style = ParagraphStyle(
        "CoverMeta",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4a5568"),
        fontName=FONT_REGULAR,
        spaceAfter=4,
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontSize=14,
        leading=16,
        textColor=colors.HexColor("#2c5282"),
        fontName=FONT_BOLD,
        spaceBefore=6,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#2d3748"),
        fontName=FONT_REGULAR,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=9, leading=11, fontName=FONT_REGULAR)
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=6.5, leading=7.5, fontName=FONT_REGULAR)
    header_cell_style = ParagraphStyle(
        "HeaderCell",
        parent=styles["Normal"],
        fontSize=7.5,
        leading=8.5,
        textColor=colors.whitesmoke,
        fontName=FONT_BOLD,
        alignment=TA_CENTER
    )

    kpi_title_style = ParagraphStyle(
        "KpiTitle",
        parent=styles["Normal"],
        fontSize=8,
        leading=9,
        textColor=colors.HexColor("#718096"),
        alignment=TA_CENTER,
        fontName=FONT_BOLD,
    )
    kpi_value_style = ParagraphStyle(
        "KpiValue",
        parent=styles["Normal"],
        fontSize=12,
        leading=14,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1a365d"),
        alignment=TA_CENTER,
    )
    kpi_value_style_large = ParagraphStyle(
        "KpiValueLarge",
        parent=styles["Normal"],
        fontSize=18,
        leading=20,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1a365d"),
        alignment=TA_CENTER,
    )

    toc_left_style = ParagraphStyle(
        "TocLeft",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1a365d"),
    )
    toc_right_style = ParagraphStyle(
        "TocRight",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#4a5568"),
        alignment=TA_RIGHT,
    )
    toc_sub_style = ParagraphStyle(
        "TocSub",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
        fontName=FONT_REGULAR,
        textColor=colors.HexColor("#4a5568"),
        leftIndent=24,
    )
    toc_group_style = ParagraphStyle(
        "TocGroup",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=11.5,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1a365d"),
        leftIndent=12,
    )

    main_title_style = ParagraphStyle(
        "AnalysisMainTitle",
        parent=styles["Heading2"],
        fontSize=14,
        leading=16,
        textColor=colors.HexColor("#1a365d"),
        fontName=FONT_BOLD,
        spaceBefore=6,
        spaceAfter=2,
    )
    analysis_subtitle_style = ParagraphStyle(
        "AnalysisSubTitle",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#319795"),
        spaceAfter=8,
    )
    analysis_intro_style = ParagraphStyle(
        "AnalysisIntro",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#2d3748"),
        fontName=FONT_REGULAR,
        alignment=TA_JUSTIFY,
        spaceAfter=4,
    )
    analysis_bullet_style = ParagraphStyle(
        "AnalysisBullet",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#2d3748"),
        fontName=FONT_REGULAR,
        alignment=TA_JUSTIFY,
        leftIndent=15,
        spaceAfter=2,
    )

    def cell_para(text: str, *, header: bool = False) -> Paragraph:
        return Paragraph(_escape(text), header_cell_style if header else cell_style)

    def build_table(sect: PdfSection, page_width: float) -> Table:
        ncols = len(sect.headers)
        col_width = page_width / max(ncols, 1)
        col_widths = [col_width] * ncols
        table_data: list[list] = [[cell_para(h, header=True) for h in sect.headers]]
        for row in _resolve_agent_cells(sect.headers, sect.rows):
            padded = list(row) + [""] * (ncols - len(row))
            table_data.append([cell_para(c) for c in padded[:ncols]])
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        t_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A365D")),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E0")),
            ("TOPPADDING", (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
            ("TOPPADDING", (0, 1), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        
        for r in range(1, len(table_data) - 1):
            if r % 2 == 1:
                t_styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
                
        # Highlight Total row if last row starts with "Total"
        if sect.rows and sect.rows[-1] and sect.rows[-1][0] == "Total":
            t_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EDF2F7")))
            t_styles.append(("LINEABOVE", (0, -1), (-1, -1), 1.0, colors.HexColor("#1A365D")))
            
        table.setStyle(TableStyle(t_styles))
        return table

    logo_reader = None
    if logo_path:
        try:
            logo_reader = ImageReader(str(Path(logo_path)))
        except Exception:
            logo_reader = None

    def _decorate_page(canvas, doc_):
        canvas.saveState()
        try:
            page_w, page_h = canvas._pagesize
            canvas.setFont(FONT_REGULAR, 9)
            canvas.setFillColor(colors.HexColor("#4a5568"))
            canvas.drawRightString(page_w - margin, 0.35 * inch, str(canvas.getPageNumber()))

            if logo_reader is not None and canvas.getPageNumber() == 1:
                logo_h = 0.55 * inch
                logo_w = 1.80 * inch
                pad_x = margin
                pad_y = 0.22 * inch
                canvas.drawImage(
                    logo_reader,
                    pad_x,
                    page_h - pad_y - logo_h,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
        finally:
            canvas.restoreState()

    doc.pageTemplates = []
    doc.addPageTemplates(
        [
            PageTemplate(
                id="landscape",
                frames=[landscape_frame],
                pagesize=landscape_size,
                onPage=_decorate_page,
            ),
        ]
    )

    story: list = []

    # --- Page 1: Cover ---
    story.append(Spacer(1, 1.2 * inch))
    display_date = cover_date or format_cover_date(generated_at)
    story.append(Paragraph(_escape(title), cover_title))
    story.append(Paragraph(_escape(period_subtitle), cover_sub))
    story.append(Paragraph(_escape(display_date), cover_sub))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("<b>Prepared for:</b> Eternalgy HR and Finance Department", cover_meta_style))
    story.append(Paragraph("<b>Prepared by:</b> Nurul Aqilah", cover_meta_style))
    story.append(PageBreak())

    # --- Page 2: Table of Contents & Overview ---
    story.append(Paragraph("Table of Contents & Overview", section_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(
        Paragraph(
            "<b>About this document.</b> This document is the consolidated Finance Commission Pack "
            "for the 2026 calendar year. It combines transaction records and calculations for Basic, "
            "ANP, and NFP agent commission policies into a unified, audit-ready presentation to support "
            "reconciliation and payment processing.",
            body_style,
        )
    )
    story.append(Spacer(1, 0.15 * inch))

    agent_details_title = "Outsource Agent Details" if is_outsource else "Internal Agent Details"
    toc_data = [
        [Paragraph(agent_details_title, toc_left_style), Paragraph("Page 3", toc_right_style)],
        [Paragraph("Commission Highlights", toc_left_style), Paragraph("Page 4", toc_right_style)],
        [Paragraph("Commission Analysis", toc_left_style), Paragraph("Page 5", toc_right_style)],
    ]
    if is_outsource:
        toc_data.extend([
            [Paragraph("Commission Details", toc_left_style), Paragraph("", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Basic Commission", toc_sub_style), Paragraph("Page 8", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Referral Fee", toc_sub_style), Paragraph("Page 9", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;NFP Commission", toc_sub_style), Paragraph("Page 10", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;EGA/ESA Awards", toc_sub_style), Paragraph("Page 12", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Production Bonus", toc_sub_style), Paragraph("Page 13", toc_right_style)],
        ])
    else:
        toc_data.extend([
            [Paragraph("Commission Details", toc_left_style), Paragraph("", toc_right_style)],
            
            [Paragraph("Basic Commission", toc_group_style), Paragraph("", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Summary of Agent Basic Commission", toc_sub_style), Paragraph("Page 8", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Basic Commission by Customer (Res & Shop)", toc_sub_style), Paragraph("Page 9", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Basic Commission by Customer (Factory)", toc_sub_style), Paragraph("Page 11", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Referral Fee", toc_sub_style), Paragraph("Page 12", toc_right_style)],
            
            [Paragraph("NFP Commission", toc_group_style), Paragraph("", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Summary of Agent NFP Commission", toc_sub_style), Paragraph("Page 13", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;NFP Commission by Customer (Res & Shop)", toc_sub_style), Paragraph("Page 15", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;NFP Commission by Customer (Factory)", toc_sub_style), Paragraph("Page 18", toc_right_style)],
            
            [Paragraph("ANP Commission", toc_group_style), Paragraph("", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;Summary of Agent ANP Commission", toc_sub_style), Paragraph("Page 20", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;ANP Commission by Customer (Res & Shop)", toc_sub_style), Paragraph("Page 21", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;ANP Commission by Customer (Factory)", toc_sub_style), Paragraph("Page 25", toc_right_style)],
            
            [Paragraph("EGA/ESA Awards", toc_group_style), Paragraph("", toc_right_style)],
            [Paragraph("&bull;&nbsp;&nbsp;EGA/ESA Awards Summary", toc_sub_style), Paragraph("Page 26", toc_right_style)],
        ])
        if any("EGA" in s.title and "Factory" in s.title for s in sections):
            toc_data.append(
                [Paragraph("&bull;&nbsp;&nbsp;EGA/ESA Awards Factory", toc_sub_style), Paragraph("Page 27", toc_right_style)]
            )
    
    toc_table = Table(toc_data, colWidths=[doc.width - 1.2 * inch, 1.2 * inch])
    toc_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ]
        )
    )
    story.append(toc_table)
    story.append(PageBreak())

    # --- Page 3: Agent Details ---
    agent_details_header = "Outsource Agent Details" if is_outsource else "Internal Agent Details"
    story.append(Paragraph(agent_details_header, section_style))
    story.append(Spacer(1, 0.05 * inch))
    
    # Thin horizontal separator
    sep_line = Table([[""]], colWidths=[doc.width], rowHeights=[2])
    sep_line.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(sep_line)
    story.append(Spacer(1, 0.08 * inch))
    
    if is_outsource:
        story.append(Paragraph("<font size=10 color='#319795'><b>The following outlines the established reporting hierarchies for outsource sales personnel. Note that independent agents operating without senior oversight are excluded from this representation.</b></font>", body_style))
    else:
        story.append(Paragraph("<font size=10 color='#319795'><b>The following outlines the established reporting hierarchies for internal sales personnel. Note that independent agents operating without senior oversight are excluded from this representation.</b></font>", body_style))
    story.append(Spacer(1, 0.12 * inch))
    # Load and render agent details
    page_w = lw - 2 * margin
    if is_outsource:
        groups = load_outsource_hierarchy()

        # --- Build flat rows (ogm, oum, osa, osa1) ---
        flat_rows = []
        for grp in groups:
            role = grp['role']
            ogm_val = grp['name'] if role == 'OGM' else ''
            oum_val = grp['name'] if role == 'OUM' else ''
            for exec_info in grp['executives']:
                flat_rows.append({
                    'ogm': ogm_val,
                    'oum': oum_val,
                    'osa': exec_info['name'],
                    'osa1': exec_info['sub'] or '',
                })

        # --- Styles ---
        hdr_base = ParagraphStyle('OHdr', parent=styles['Normal'], fontSize=8.0,
            leading=9.5, textColor=colors.white, fontName=FONT_BOLD, alignment=TA_CENTER)
        name_style_tbl = ParagraphStyle('OName', parent=styles['Normal'], fontSize=7.2,
            leading=9.0, textColor=colors.HexColor('#1A202C'), fontName=FONT_REGULAR)
        name_bold_tbl = ParagraphStyle('ONameB', parent=styles['Normal'], fontSize=7.2,
            leading=9.0, textColor=colors.HexColor('#1A202C'), fontName=FONT_BOLD)
        sub_style_tbl = ParagraphStyle('OSub', parent=styles['Normal'], fontSize=7.0,
            leading=8.5, textColor=colors.HexColor('#744210'), fontName=FONT_REGULAR)

        HDR_COLORS = [
            colors.HexColor('#1A365D'),   # OGM - darkest navy
            colors.HexColor('#2B5F9E'),   # OUM - mid blue
            colors.HexColor('#2C7A7B'),   # OSA - teal
            colors.HexColor('#4A9EA0'),   # OSA 1 - lighter teal
        ]
        col_w = [page_w / 4] * 4   # equal width for all columns

        # --- Build header row ---
        hdr_row = [Paragraph(h, hdr_base) for h in ['OGM', 'OUM', 'OSA', 'OSA 1']]

        # --- Build data rows ---
        data_rows = []
        for r in flat_rows:
            data_rows.append([
                Paragraph(r['ogm'], name_bold_tbl) if r['ogm'] else '',
                Paragraph(r['oum'], name_bold_tbl) if r['oum'] else '',
                Paragraph(r['osa'], name_style_tbl),
                Paragraph(r['osa1'], sub_style_tbl) if r['osa1'] else '',
            ])

        all_rows = [hdr_row] + data_rows
        tbl = Table(all_rows, colWidths=col_w, repeatRows=1)

        # --- Base style: NO grid lines, clean ---
        tstyle = [
            # Header backgrounds (different shade per column)
            ('BACKGROUND', (0, 0), (0, 0), HDR_COLORS[0]),
            ('BACKGROUND', (1, 0), (1, 0), HDR_COLORS[1]),
            ('BACKGROUND', (2, 0), (2, 0), HDR_COLORS[2]),
            ('BACKGROUND', (3, 0), (3, 0), HDR_COLORS[3]),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, 0), 4),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
            ('TOPPADDING', (0, 1), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 1),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            # Very subtle row alternation
            *[('BACKGROUND', (0, r), (-1, r), colors.HexColor('#F0F4F8')) for r in range(2, len(all_rows), 2)],
        ]

        # --- Compute SPANs to remove duplicate OGM and OUM names ---
        for col_key, col_idx in [('ogm', 0), ('oum', 1)]:
            i = 0
            while i < len(flat_rows):
                val = flat_rows[i][col_key]
                if val:
                    j = i + 1
                    while j < len(flat_rows) and flat_rows[j][col_key] == val:
                        j += 1
                    if j - i > 1:
                        # SPAN rows i+1 to j (header is row 0, data starts at row 1)
                        tstyle.append(('SPAN', (col_idx, i + 1), (col_idx, j)))
                        tstyle.append(('VALIGN', (col_idx, i + 1), (col_idx, j), 'TOP'))
                    i = j
                else:
                    i += 1

        tbl.setStyle(TableStyle(tstyle))
        story.append(tbl)

    else:
        groups = load_internal_hierarchy()
        for idx, grp in enumerate(groups):
            group_row = make_internal_group_row(grp, page_w, styles)
            story.append(group_row)
            if idx < len(groups) - 1:
                sep = Table([['']], colWidths=[page_w], rowHeights=[1])
                sep.setStyle(TableStyle([
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                    ('TOPPADDING', (0, 0), (-1, -1), 2),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ]))
                story.append(sep)
    story.append(Spacer(1, 0.15 * inch))

    desc_style = ParagraphStyle(
        "AgentDetailsDesc",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#4D5562")
    )
    story.append(PageBreak())

    # --- Page 4: Highlights ---
    story.append(Paragraph("Commission Highlights", section_style))
    story.append(Spacer(1, 0.05 * inch))
    
    # Thin horizontal separator
    sep_line = Table([[""]], colWidths=[doc.width], rowHeights=[2])
    sep_line.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(sep_line)
    story.append(Spacer(1, 0.15 * inch))

    # KPIs
    total_agents_val = charts.total_agents if charts.total_agents is not None else len(charts.top3)
    total_cust_val = charts.total_customers if charts.total_customers is not None else 0
    total_sales_val = sum(x[4] for x in charts.top3) if charts.top3 else 0.0

    kpi_title_style = ParagraphStyle(
        "KpiTitleNew",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=10,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#718096")
    )
    kpi_value_style = ParagraphStyle(
        "KpiValueNew",
        parent=styles["Normal"],
        fontSize=22,
        leading=26,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#1A365D")
    )

    sub_value_style = ParagraphStyle(
        "SubValueStyle",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        fontName=FONT_REGULAR,
        textColor=colors.HexColor("#718096")
    )

    sales_cell_content = [
        Paragraph(f"RM {charts.full_payment_sales:,.2f}" if charts.full_payment_sales is not None else f"RM {total_sales_val:,.2f}", kpi_value_style),
        Spacer(1, 4),
        Paragraph("Full Payment", sub_value_style)
    ]

    kpi_data_row1 = [
        [
            Paragraph("TOTAL COMMISSION PAID", kpi_title_style),
            Paragraph("TOTAL SALES", kpi_title_style),
        ],
        [
            Paragraph(f"RM {charts.grand_total:,.2f}", kpi_value_style),
            sales_cell_content,
        ]
    ]
    kpi_table_row1 = Table(kpi_data_row1, colWidths=[doc.width * 0.5, doc.width * 0.5])
    kpi_table_row1.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))

    # Stats paragraph
    agents_sub_style = ParagraphStyle(
        "AgentsSubStyle",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11.5,
        fontName=FONT_BOLD,
        textColor=colors.HexColor("#718096"),
        alignment=TA_LEFT
    )
    
    agents_cell_content = [
        Paragraph(str(total_agents_val), kpi_value_style),
        Spacer(1, 4),
        Paragraph(f"{charts.closed_agents_count or 0} {'agents' if charts.closed_agents_count != 1 else 'agent'} with closed cases", agents_sub_style),
        Paragraph(f"{charts.pending_agents_count or 0} {'agents' if charts.pending_agents_count != 1 else 'agent'} with pending closed cases", agents_sub_style),
    ]

    customers_cell_content = [
        Paragraph(str(total_cust_val), kpi_value_style),
        Spacer(1, 4),
        Paragraph(f"{charts.full_payment_count or 0} cases with full payment", agents_sub_style),
        Paragraph(f"{charts.pending_payment_count or 0} cases with Pending Full Payment", agents_sub_style),
    ]

    kpi_data_row2 = [
        [
            Paragraph("TOTAL ACTIVE AGENTS", kpi_title_style),
            Paragraph("TOTAL CUSTOMERS SERVED", kpi_title_style)
        ],
        [
            agents_cell_content,
            customers_cell_content
        ]
    ]
    kpi_table_row2 = Table(kpi_data_row2, colWidths=[doc.width * 0.5, doc.width * 0.5])
    kpi_table_row2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    
    story.append(kpi_table_row1)
    story.append(Spacer(1, 0.12 * inch))
    story.append(sep_line)
    story.append(Spacer(1, 0.12 * inch))
    story.append(kpi_table_row2)
    story.append(Spacer(1, 0.12 * inch))
    story.append(sep_line)
    story.append(Spacer(1, 0.45 * inch))

    # Top Performing Agents Title
    story.append(Paragraph("<font size=11 color='#1A365D'><b>TOP 3 PERFORMING AGENTS BY SALES</b></font>", body_style))
    story.append(Spacer(1, 0.1 * inch))

    top3 = charts.top3[:3]
    top3_rows = []
    
    agent_highlight_style = ParagraphStyle(
        "AgentHighlight",
        parent=styles["Normal"],
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#2d3748"),
        fontName=FONT_REGULAR,
    )
    
    for i, item in enumerate(top3, start=1):
        name = item[0]
        total = item[4]
        cust_cnt = item[5] if len(item) > 5 else 0
        
        col1 = Paragraph(f"<font color='#1A365D'><b>{i}. {name}</b></font>", agent_highlight_style)
        col2 = Paragraph(f"<font color='#319795'><b>Total Sales: RM {total:,.2f}</b></font>", agent_highlight_style)
        col3 = Paragraph(f"<font color='#1A365D'><b>{cust_cnt} {'Customers' if cust_cnt != 1 else 'Customer'}</b></font>", agent_highlight_style)
        top3_rows.append([col1, col2, col3])

    if not top3_rows:
        top3_rows.append([Paragraph("No agent sales volume data.", body_style), "", ""])

    top3_table = Table(top3_rows, colWidths=[doc.width * 0.4, doc.width * 0.35, doc.width * 0.25])
    top3_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(top3_table)
    story.append(PageBreak())
    # --- Page 4: Monthly Trend ---
    if charts.monthly_trend:
        story.append(Paragraph("Commission Analysis", main_title_style))
        story.append(Paragraph("Monthly Trend", analysis_subtitle_style))
        story.append(Spacer(1, 0.04 * inch))
        trend_chart = _draw_monthly_trend_chart(charts.monthly_trend, width=doc.width, height=150)
        story.append(ChartFlowable(trend_chart, doc.width, 150))
        story.append(Spacer(1, 0.04 * inch))
        

        # Analysis summary text
        analysis_txt1 = _generate_monthly_trend_analysis(charts, analysis_intro_style, analysis_bullet_style)
        story.extend(analysis_txt1)
        story.append(PageBreak())

    # --- Page 5: Agent Customer Performance (Grids) ---
    if charts.monthly_agent_customers and charts.agent_names:
        months_full = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
        
        # Grid 1: Jan - Jun
        story.append(Paragraph("Commission Analysis", main_title_style))
        story.append(Paragraph("Agent Customer Count (Jan - Jun)", analysis_subtitle_style))
        story.append(Spacer(1, 0.05 * inch))
        drawings1 = []
        for m in range(1, 7):
            m_data = charts.monthly_agent_customers[m-1] if m-1 < len(charts.monthly_agent_customers) else {}
            chart = _draw_monthly_agent_customer_chart(m_data, charts.agent_names, months_full[m-1], width=250, height=145)
            drawings1.append(ChartFlowable(chart, 250, 145))
            
        grid_table1 = Table([
            [drawings1[0], drawings1[1], drawings1[2]],
            [drawings1[3], drawings1[4], drawings1[5]]
        ], colWidths=[doc.width/3, doc.width/3, doc.width/3], rowHeights=[150, 150])
        grid_table1.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(grid_table1)
        story.append(Spacer(1, 0.1 * inch))
        analysis_txt2 = _generate_agent_customer_analysis(charts, analysis_intro_style, analysis_bullet_style)
        story.extend(analysis_txt2)
        story.append(PageBreak())
        
        # Grid 2: Jul - Dec (only if we have more than 6 months of data)
        if len(charts.monthly_agent_customers) > 6:
            story.append(Paragraph("Commission Analysis", main_title_style))
            story.append(Paragraph("Agent Customer Count (Jul - Dec)", analysis_subtitle_style))
            story.append(Spacer(1, 0.05 * inch))
            drawings2 = []
            for m in range(7, 13):
                m_data = charts.monthly_agent_customers[m-1] if m-1 < len(charts.monthly_agent_customers) else {}
                chart = _draw_monthly_agent_customer_chart(m_data, charts.agent_names, months_full[m-1], width=250, height=145)
                drawings2.append(ChartFlowable(chart, 250, 145))
                
            grid_table2 = Table([
                [drawings2[0], drawings2[1], drawings2[2]],
                [drawings2[3], drawings2[4], drawings2[5]]
            ], colWidths=[doc.width/3, doc.width/3, doc.width/3], rowHeights=[150, 150])
            grid_table2.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(grid_table2)
            story.append(Spacer(1, 0.1 * inch))
            story.append(PageBreak())

    # --- Page 6: Commission Composition Split (Grids) ---
    if charts.monthly_agent_commission_split and charts.agent_names:
        months_full = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
        
        # Grid 1: Jan - Jun
        story.append(Paragraph("Commission Analysis", main_title_style))
        story.append(Paragraph("Agent Commission Split (Jan - Jun)", analysis_subtitle_style))
        story.append(Spacer(1, 0.05 * inch))
        drawings3 = []
        for m in range(1, 7):
            m_data = charts.monthly_agent_commission_split.get(m, {})
            chart = _draw_monthly_commission_split_chart(m_data, charts.agent_names, months_full[m-1], width=250, height=145)
            drawings3.append(ChartFlowable(chart, 250, 145))
            
        grid_table3 = Table([
            [drawings3[0], drawings3[1], drawings3[2]],
            [drawings3[3], drawings3[4], drawings3[5]]
        ], colWidths=[doc.width/3, doc.width/3, doc.width/3], rowHeights=[150, 150])
        grid_table3.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(grid_table3)
        story.append(Spacer(1, 0.05 * inch))
        story.append(_create_split_legend(doc.width))
        story.append(Spacer(1, 0.08 * inch))
        analysis_txt3 = _generate_commission_split_analysis(charts, analysis_intro_style, analysis_bullet_style)
        story.extend(analysis_txt3)
        
        # Grid 2: Jul - Dec (only if we have more than 6 months of data)
        if len(charts.monthly_agent_customers) > 6:
            story.append(PageBreak())
            story.append(Paragraph("Commission Analysis", main_title_style))
            story.append(Paragraph("Agent Commission Split (Jul - Dec)", analysis_subtitle_style))
            story.append(Spacer(1, 0.05 * inch))
            drawings4 = []
            for m in range(7, 13):
                m_data = charts.monthly_agent_commission_split.get(m, {})
                chart = _draw_monthly_commission_split_chart(m_data, charts.agent_names, months_full[m-1], width=250, height=145)
                drawings4.append(ChartFlowable(chart, 250, 145))
                
            grid_table4 = Table([
                [drawings4[0], drawings4[1], drawings4[2]],
                [drawings4[3], drawings4[4], drawings4[5]]
            ], colWidths=[doc.width/3, doc.width/3, doc.width/3], rowHeights=[150, 150])
            grid_table4.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(grid_table4)
            story.append(Spacer(1, 0.05 * inch))
            story.append(_create_split_legend(doc.width))

    # --- Pages 7+: Details Tables ---
    page_w = lw - 2 * margin

    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

    for section in sections:
        story.append(PageBreak())

        if " — " in section.title:
            main_title, sub_title = section.title.split(" — ", 1)
            story.append(Paragraph(_escape(main_title), section_style))
            subtitle_style = ParagraphStyle(
                "SectionSubTitle",
                parent=styles["Normal"],
                fontSize=9.5,
                leading=11.5,
                textColor=colors.HexColor("#319795"),
                fontName=FONT_REGULAR,
                spaceAfter=6,
            )
            story.append(Paragraph(_escape(sub_title), subtitle_style))
        else:
            story.append(Paragraph(_escape(section.title), section_style))
        
        if section.total_agents is not None or section.total_customers is not None:
            stats_style = ParagraphStyle(
                "SectionStats",
                parent=meta_style,
                textColor=colors.HexColor("#319795"),
                fontSize=9.5,
                leading=12,
                fontName=FONT_BOLD,
                spaceAfter=4,
            )
            stats_parts = []
            if section.total_agents is not None:
                stats_parts.append(f"Total Active Agents: {section.total_agents}")
            if section.total_customers is not None:
                stats_parts.append(f"Total Customers: {section.total_customers}")
            stats_text = " &nbsp;&nbsp;|&nbsp;&nbsp; ".join(stats_parts)
            story.append(Paragraph(stats_text, stats_style))
            story.append(Spacer(1, 0.08 * inch))
        else:
            story.append(Spacer(1, 0.12 * inch))

        if section.monthly_tables:
            # Grouped monthly tables
            has_data = False
            for m in range(1, 13):
                m_rows = section.monthly_tables.get(m)
                if not m_rows:
                    continue
                has_data = True
                
                # Determine column ratios based on headers length
                col_ratios = None
                if len(section.headers) == 7: # Basic
                    col_ratios = [1.8, 2.5, 0.8, 1.2, 1.0, 1.0, 1.2]
                elif len(section.headers) == 8: # NFP
                    col_ratios = [1.5, 2.0, 0.6, 1.0, 1.0, 1.0, 0.8, 1.0]
                elif len(section.headers) == 5: # ANP
                    col_ratios = [2.0, 2.5, 0.8, 1.5, 1.5]
                    
                month_title = f"{month_names[m-1]} - {section.title.split('—')[0].strip()}"
                m_table = build_stacked_table(month_title, section.headers, m_rows, page_w, col_ratios)
                story.append(m_table)
                story.append(Spacer(1, 0.2 * inch))
                
            if not has_data:
                story.append(Paragraph("(no rows)", meta_style))
        else:
            # Fallback to standard table or matrix table
            if not section.rows:
                story.append(Paragraph("(no rows)", meta_style))
            else:
                if section.landscape and "Details" in section.title:
                    comm_type = section.title.split("Details")[0].strip()
                    if comm_type == "NFP Commission":
                        has_customer = False
                        # 28 columns for H1, 52 columns for Full Year
                        if section.rows and len(section.rows[0]) in (28, 52):
                            has_customer = True
                        if has_customer:
                            tables = build_nfp_customer_matrix_tables(section.rows, page_w)
                        else:
                            tables = build_nfp_matrix_tables(section.rows, page_w)
                        for idx, t in enumerate(tables):
                            story.append(t)
                            if idx < len(tables) - 1:
                                if idx % 2 == 1:
                                    story.append(PageBreak())
                                else:
                                    story.append(Spacer(1, 0.08 * inch))
                    else:
                        cols_count = len(section.rows[0]) if section.rows else 0
                        is_full_year = (cols_count > 20)
                        if is_full_year:
                            if comm_type == "ANP Commission" and "Customer" in section.title:
                                prefix_cols = 2
                                h1_cols = prefix_cols + 12
                            elif "Customer" in section.title:
                                prefix_cols = 3
                                h1_cols = prefix_cols + 12
                            elif comm_type == "ANP Commission":
                                prefix_cols = 1
                                h1_cols = prefix_cols + 12
                            else:
                                prefix_cols = 2
                                h1_cols = prefix_cols + 12
                            
                            h1_rows = []
                            h2_rows = []
                            for row in section.rows:
                                h1_rows.append(row[:h1_cols])
                                h2_rows.append(list(row[:prefix_cols]) + row[h1_cols:])
                            
                            t1 = build_matrix_table(section.title + " (Jan - Jun)", comm_type, h1_rows, page_w, is_h2=False)
                            t2 = build_matrix_table(section.title + " (Jul - Dec)", comm_type, h2_rows, page_w, is_h2=True)
                            
                            story.append(t1)
                            story.append(PageBreak())
                            story.append(t2)
                        else:
                            story.append(build_matrix_table(section.title, comm_type, section.rows, page_w))
                    
                    # Add descriptive notes below the tables
                    if comm_type == "NFP Commission":
                        story.append(Spacer(1, 4.0))
                    else:
                        story.append(Spacer(1, 0.12 * inch))

                    note_style = ParagraphStyle(
                        "DetailNote",
                        parent=styles["Normal"],
                        fontSize=7.5,
                        leading=10.5,
                        fontName=FONT_REGULAR,
                        textColor=colors.HexColor("#4a5568")
                    )
                    note_title_style = ParagraphStyle(
                        "DetailNoteTitle",
                        parent=styles["Normal"],
                        fontSize=8,
                        leading=11,
                        fontName=FONT_BOLD,
                        textColor=colors.HexColor("#1A365D"),
                        spaceAfter=4
                    )
                    
                    cell_elements = []
                    if "Basic" in comm_type and "Summary of Agent Basic Commission" in section.title:
                        import sys
                        # Ensure 1. Basic Commission/3. Python Script is in path to load basic_commission_rates
                        script_dir = str(Path(__file__).resolve().parent / "1. Basic Commission" / "3. Python Script")
                        if script_dir not in sys.path:
                            sys.path.insert(0, script_dir)
                        try:
                            import basic_commission_rates
                            # Jan-May rates (month 5)
                            m5_exec = f"{basic_commission_rates.get_basic_rate('Internal', 'executive', 5)*100:.2f}%".replace(".00", "")
                            m5_senior = f"{basic_commission_rates.get_basic_rate('Internal', 'senior', 5)*100:.2f}%".replace(".00", "")
                            # June rates (month 6)
                            m6_exec = f"{basic_commission_rates.get_basic_rate('Internal', 'executive', 6)*100:.2f}%".replace(".00", "")
                            m6_senior = f"{basic_commission_rates.get_basic_rate('Internal', 'senior', 6)*100:.2f}%".replace(".00", "")
                            
                            # Outsource Jan-May
                            m5_oum = f"{basic_commission_rates.get_basic_rate('Outsource', 'oum', 5)*100:.2f}%".replace(".00", "")
                            m5_osa = f"{basic_commission_rates.get_basic_rate('Outsource', 'osa/osa1', 5)*100:.2f}%".replace(".00", "")
                            # Outsource June
                            m6_oum = f"{basic_commission_rates.get_basic_rate('Outsource', 'oum', 6)*100:.2f}%".replace(".00", "")
                            m6_osa = f"{basic_commission_rates.get_basic_rate('Outsource', 'osa/osa1', 6)*100:.2f}%".replace(".00", "")
                        except Exception:
                            # Fallback if import fails
                            m5_exec, m5_senior = "3%", "3.25%"
                            m6_exec, m6_senior = "4%", "4.25%"
                            m5_oum, m5_osa = "4.5%", "4.5%"
                            m6_oum, m6_osa = "5.5%", "5.5%"

                        run_month = None
                        month_names_lower = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
                        if meta_lines:
                            for k, v in meta_lines:
                                kl = str(k).lower()
                                vl = str(v).lower()
                                if "month" in kl:
                                    for idx, mname in enumerate(month_names_lower, start=1):
                                        if mname in vl or v == str(idx):
                                            run_month = idx
                                            break
                                if run_month:
                                    break
                        if not run_month:
                            for sec in sections:
                                title_l = sec.title.lower()
                                for idx, mname in enumerate(month_names_lower, start=1):
                                    if mname in title_l:
                                        run_month = idx
                                        break
                                if run_month:
                                    break

                        cell_elements.append(Paragraph("<b>Note:</b>", note_title_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Total Amount - EPP Price (if applicable) = Sales Price", note_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Sales Price x Rate % = Basic Commission", note_style))
                        
                        if run_month is not None and run_month >= 6:
                            # June or later
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Rate % :", note_style))
                            if is_outsource:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;OUM - {m6_oum}, OSA - {m6_osa}", note_style))
                            else:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Executive - {m6_exec}, Senior - {m6_senior}", note_style))
                                cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Senior Override - 0.25%", note_style))
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Basic Commission payout condition:", note_style))
                            cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Payment = 5% then Agent get RM300", note_style))
                            cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Payment = 75% then Agent get Balance Basic Commission", note_style))
                        elif run_month is not None and run_month <= 5:
                            # January to May
                            cell_elements.insert(1, Paragraph("&bull;&nbsp;&nbsp;Basic Commission is for every Full Payment", note_style))
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Rate % :", note_style))
                            if is_outsource:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;OUM - {m5_oum}, OSA - {m5_osa}", note_style))
                            else:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Executive - {m5_exec}, Senior - {m5_senior}", note_style))
                                cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Senior Override - 0.25%", note_style))
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Basic Commission payout condition: given out to Agent once Payment = 100%", note_style))
                        else:
                            # H1 / Annual (aggregate)
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Rate % :", note_style))
                            if is_outsource:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Jan - May: OUM - {m5_oum}, OSA - {m5_osa}", note_style))
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;From Jun: OUM - {m6_oum}, OSA - {m6_osa}", note_style))
                            else:
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Jan - May: Executive - {m5_exec}, Senior - {m5_senior}", note_style))
                                cell_elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;From Jun: Executive - {m6_exec}, Senior - {m6_senior}", note_style))
                                cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Senior Override - 0.25%", note_style))
                            cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Basic Commission payout condition:", note_style))
                            cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Jan - May: given out to Agent once Payment = 100%", note_style))
                            cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;From Jun: i. Payment = 5% then Agent get RM300, ii. Payment = 75% then Agent get Balance Basic Commission", note_style))

                    elif "ANP" in comm_type and "Summary" in section.title:
                        cell_elements.append(Paragraph("<b>Note:</b>", note_title_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Requires a minimum 5% payment", note_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;ANP Commission will be rewarded the next month of case issuance", note_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;EP Point Recognition Structure:", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;<b>Residence & Shop Lot:</b> 100% recognition rate", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;<b>Factory:</b> Prior to May 2026: 100% recognition. Effective May 2026 onwards: 100% recognition for the first RM 40,000, 40% recognition for the balance amount (unless factory has less than 36pcs, in which case it follows residence rate).", note_style))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Commission Tiers based on Accumulated Total Sales:", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;RM 0 - 59k qualifies for RM 0", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;RM 60k - 179k qualifies for RM 500", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;RM 180k - 359k qualifies for RM 1000", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Above RM 360k qualifies for RM 1500", note_style))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;Above RM 720k qualifies for RM 2000", note_style))
                        cell_elements.append(Spacer(1, 4))
                        cell_elements.append(Paragraph("<i>Reconciliation Note: Invoice 1005425 was issued in 2025 but fully paid in 2026. Consequently, it is excluded from H1 2026 ANP Commission Details (reconciling the section total to 138 invoices).</i>", note_style))

                    elif "NFP" in comm_type and "Summary" in section.title:
                        note_style_nfp = ParagraphStyle(
                            "DetailNoteNfp",
                            parent=styles["Normal"],
                            fontSize=7.0,
                            leading=9.0,
                            fontName=FONT_REGULAR,
                            textColor=colors.HexColor("#4a5568")
                        )
                        note_title_style_nfp = ParagraphStyle(
                            "DetailNoteTitleNfp",
                            parent=styles["Normal"],
                            fontSize=8.0,
                            leading=10.0,
                            fontName=FONT_BOLD,
                            textColor=colors.HexColor("#1A365D"),
                            spaceAfter=4
                        )
                        cell_elements.append(Paragraph("<b>Note:</b>", note_title_style_nfp))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;NFP Commission distributions are contingent upon the receipt of 100% full payment.", note_style_nfp))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Three types of Net Floor Price Commission:", note_style_nfp))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;<b>Sales above Net Floor Price:</b> Sales Price > Net Floor Price. Formula: (Sales Price - Net Floor Price) x 25% = NFP Commission", note_style_nfp))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;<b>Sales above System Price:</b> System Price > Net Floor Price. Formula: (System Price - Net Floor Price) x 100% = NFP Commission", note_style_nfp))
                        cell_elements.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;&bull;&nbsp;<b>Sales below Net Floor price:</b> Sales Price < Net Floor Price. Formula: (Sales Price - Net Floor Price) x bears 20% = NFP Commission", note_style_nfp))
                        cell_elements.append(Spacer(1, 4))
                        cell_elements.append(Paragraph("&bull;&nbsp;&nbsp;Effective October 1, 2025, NFP computations are applicable exclusively to invoices issued on or after this date. Invoices predating this period are structurally excluded from NFP allocations.", note_style_nfp))

                    if cell_elements:
                        note_table = Table([[cell_elements]], colWidths=[page_w])
                        note_table.setStyle(TableStyle([
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                            ("LINELEFT", (0, 0), (-1, -1), 3.0, colors.HexColor("#1A365D")),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ]))
                        story.append(note_table)
                else:
                    story.append(build_table(section, page_w))

                    if section.footer_text:
                        story.append(Spacer(1, 6))
                        note_title_style = ParagraphStyle(
                            "NoteTitle",
                            parent=styles["Normal"],
                            fontSize=8,
                            leading=10,
                            fontName=FONT_BOLD,
                            textColor=colors.HexColor("#1A365D"),
                            spaceAfter=4
                        )
                        note_text_style = ParagraphStyle(
                            "NoteText",
                            parent=styles["Normal"],
                            fontSize=7.5,
                            leading=9.5,
                            fontName=FONT_REGULAR,
                            textColor=colors.HexColor("#4a5568"),
                            spaceAfter=2
                        )
                        cell_elements = []
                        first = section.footer_text[0].strip() if section.footer_text else ""
                        if first.lower() in ("<b>note:</b>", "note:", "<b>note</b>:"):
                            cell_elements.append(Paragraph("<b>Note:</b>", note_title_style))
                            lines_to_add = section.footer_text[1:]
                        else:
                            cell_elements.append(Paragraph("<b>Note:</b>", note_title_style))
                            lines_to_add = section.footer_text
                        for text in lines_to_add:
                            cell_elements.append(Paragraph(text, note_text_style))
                        
                        note_table = Table([[cell_elements]], colWidths=[page_w - 72])
                        note_table.setStyle(TableStyle([
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                            ("LINELEFT", (0, 0), (-1, -1), 3.0, colors.HexColor("#1A365D")),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ]))
                        story.append(note_table)

    doc.build(story)
    return path


def write_commission_pdf(
    path: Path,
    *,
    title: str,
    meta_lines: Sequence[tuple[str, str]] | None = None,
    sections: Sequence[PdfSection],
) -> Path:
    """Compact multi-section PDF (legacy layout)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise ImportError(
            "PDF export requires reportlab. Install with: pip install reportlab"
        ) from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    page_size = landscape(A4) if any(s.landscape for s in sections) else A4
    doc = SimpleDocTemplate(
        str(path),
        pagesize=page_size,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Heading1Legacy", parent=styles["Heading1"], fontName=FONT_BOLD)
    section_style = ParagraphStyle("Heading2Legacy", parent=styles["Heading2"], fontName=FONT_BOLD)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=9, leading=11, fontName=FONT_REGULAR)
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=7, leading=8, fontName=FONT_REGULAR)
    header_cell_style = ParagraphStyle(
        "HeaderCell",
        parent=styles["Normal"],
        fontSize=8,
        leading=9,
        textColor=colors.whitesmoke,
        fontName=FONT_BOLD,
    )

    def cell_para(text: str, *, header: bool = False) -> Paragraph:
        return Paragraph(_escape(text), header_cell_style if header else cell_style)

    story: list = [Paragraph(title, title_style), Spacer(1, 0.15 * inch)]

    if meta_lines:
        meta_data = [
            [Paragraph(str(k), meta_style), Paragraph(str(v), meta_style)]
            for k, v in meta_lines
        ]
        meta_table = Table(meta_data, colWidths=[2.2 * inch, doc.width - 2.2 * inch])
        meta_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ]
            )
        )
        story.extend([meta_table, Spacer(1, 0.2 * inch)])

    usable_width = doc.width

    for section in sections:
        story.append(Paragraph(section.title, section_style))
        
        if section.total_agents is not None or section.total_customers is not None:
            stats_style = ParagraphStyle(
                "SectionStatsLegacy",
                parent=meta_style,
                textColor=colors.HexColor("#319795"),
                fontSize=9,
                leading=11,
                fontName=FONT_BOLD,
                spaceAfter=4,
            )
            stats_parts = []
            if section.total_agents is not None:
                stats_parts.append(f"Total Active Agents: {section.total_agents}")
            if section.total_customers is not None:
                stats_parts.append(f"Total Customers: {section.total_customers}")
            stats_text = " &nbsp;&nbsp;|&nbsp;&nbsp; ".join(stats_parts)
            story.append(Paragraph(stats_text, stats_style))
            story.append(Spacer(1, 0.06 * inch))
        else:
            story.append(Spacer(1, 0.08 * inch))

        if not section.rows:
            story.append(Paragraph("(no rows)", meta_style))
            story.append(Spacer(1, 0.15 * inch))
            continue

        ncols = len(section.headers)
        col_width = usable_width / max(ncols, 1)
        col_widths = [col_width] * ncols

        table_data: list[list] = [[cell_para(h, header=True) for h in section.headers]]
        for row in _resolve_agent_cells(section.headers, section.rows):
            padded = list(row) + [""] * (ncols - len(row))
            table_data.append([cell_para(c) for c in padded[:ncols]])

        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c5282")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("FONTSIZE", (0, 1), (-1, -1), 7),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f7fafc")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 0.2 * inch))

    doc.build(story)
    return path
