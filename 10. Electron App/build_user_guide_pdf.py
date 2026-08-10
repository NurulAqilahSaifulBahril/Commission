"""Generate 'Commission Desktop app_user_guide.pdf' in the Eternalgy house style.

Reproduces the original guide design (dark cover band, blue accent, pill
badges, numbered chips, amber callout, green cards, red troubleshooting
table) and adds macOS instructions alongside the Windows ones.

Run:  python "10. Electron App/build_user_guide_pdf.py"
"""

from __future__ import annotations

import os
import re

from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as _canvas

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "Commission Desktop app_user_guide.pdf")
LOGO = os.path.join(HERE, "assets", "logo-header.png")
SCREENSHOT = os.path.join(HERE, "update_panel_screenshot.png")

VERSION = "1.2.9"
DATE_LINE = "August 2026"
RELEASES_URL = "https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest"

# ── Fonts (Segoe UI family + Consolas, as in the original) ───────────────────
FONTS = r"C:\Windows\Fonts"
pdfmetrics.registerFont(TTFont("SegoeUI", os.path.join(FONTS, "segoeui.ttf")))
pdfmetrics.registerFont(TTFont("SegoeUI-Bold", os.path.join(FONTS, "segoeuib.ttf")))
pdfmetrics.registerFont(TTFont("SegoeUI-Italic", os.path.join(FONTS, "segoeuii.ttf")))
pdfmetrics.registerFont(TTFont("SegoeUI-Semibold", os.path.join(FONTS, "seguisb.ttf")))
pdfmetrics.registerFont(TTFont("Consolas", os.path.join(FONTS, "consola.ttf")))

# ── Palette (sampled from the original PDF) ──────────────────────────────────
DARK = HexColor("#0f1922")
ACCENT = HexColor("#2563eb")
LIGHT_BLUE = HexColor("#60a5fa")
BADGE_BLUE = HexColor("#93c5fd")
SUBTITLE = HexColor("#aec3de")
TEXT = HexColor("#0f172a")
MUTED = HexColor("#64748b")
NUM_BG = HexColor("#eaf1fe")
NUM_FG = HexColor("#1d4ed8")
CODE_BG = HexColor("#eff2f6")
RULE = HexColor("#dce3ec")
NOTE_BG = HexColor("#fff7e8")
NOTE_BAR = HexColor("#b45309")
GREEN_BG = HexColor("#edfbf2")
GREEN_TOP = HexColor("#bcead0")
GREEN = HexColor("#15803d")
RED = HexColor("#dc2626")
RED_ROW = HexColor("#fdf0ef")
RED_LINE = HexColor("#f6c9c4")
SHADOW = HexColor("#d7dee7")
WHITE = HexColor("#ffffff")

# ── Page geometry (A4, matching the original) ────────────────────────────────
PAGE_W, PAGE_H = 595.28, 841.89
M = 56.69                       # left margin
RIGHT = 538.58                  # right content edge
CW = RIGHT - M                  # content width
BOTTOM_LIMIT = 780              # last usable y (top-based)
LEADING = 14.2

# ── Rich text: **bold**  *italic*  `code`  [text](url) ───────────────────────
_TOKEN = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`.+?`|\[.+?\]\(.+?\))")


def _spans(text: str, base_font: str, size: float, color) -> list[tuple]:
    """[(word, font, size, color, is_code, url), ...] split into words.

    Spaces *inside* a styled run carry that run's attributes, so a multi-word
    link keeps one continuous underline instead of breaking at every gap.
    """
    out = []
    for part in _TOKEN.split(text):
        if not part:
            continue
        url = None
        if part.startswith("**") and part.endswith("**"):
            font, s, col, code, raw = "SegoeUI-Bold", size, color, False, part[2:-2]
        elif part.startswith("*") and part.endswith("*"):
            font, s, col, code, raw = "SegoeUI-Italic", size, color, False, part[1:-1]
        elif part.startswith("`") and part.endswith("`"):
            font, s, col, code, raw = "Consolas", size * 0.88, color, True, part[1:-1]
        elif part.startswith("["):
            m = re.match(r"\[(.+?)\]\((.+?)\)", part)
            raw, url = m.group(1), m.group(2)
            font = "SegoeUI-Italic" if base_font == "SegoeUI-Italic" else "SegoeUI-Bold"
            s, col, code = size, NUM_FG, False
        else:
            font, s, col, code, raw = base_font, size, color, False, part

        if raw.startswith(" ") and out:
            out.append((" ", base_font, size, color, False, None))
        words = [w for w in raw.split(" ") if w != ""]
        for i, word in enumerate(words):
            if i > 0:
                # inherit the run's style so links underline continuously
                out.append((" ", font, s, col, False, url))
            out.append((word, font, s, col, code, url))
        if raw.endswith(" ") and words:
            out.append((" ", base_font, size, color, False, None))
    return out


def _wrap(text: str, width: float, base_font="SegoeUI", size=10.0, color=TEXT):
    words = _spans(text, base_font, size, color)
    lines, line, w = [], [], 0.0
    for tok in words:
        tw = pdfmetrics.stringWidth(tok[0], tok[1], tok[2]) + (5 if tok[4] else 0)
        if tok[0] == " ":
            line.append(tok); w += tw
            continue
        if line and w + tw > width:
            while line and line[-1][0] == " ":
                line.pop()
            lines.append(line)
            line, w = [], 0.0
        line.append(tok); w += tw
    if line:
        while line and line[-1][0] == " ":
            line.pop()
        lines.append(line)
    return lines


class Guide:
    def __init__(self):
        self.c = _canvas.Canvas(OUT, pagesize=(PAGE_W, PAGE_H))
        self.c.setTitle("Commission Portal — Install & Update Guide")
        self.page = 1
        self.y = 0.0
        self._cover_header()

    # top-based y -> pdf y
    def Y(self, y):
        return PAGE_H - y

    # ── headers / footers ────────────────────────────────────────────────────
    def _footer(self):
        c = self.c
        c.setStrokeColor(RULE); c.setLineWidth(0.6)
        c.line(M, self.Y(799.4), RIGHT, self.Y(799.4))
        c.setFont("SegoeUI", 7.6); c.setFillColor(MUTED)
        c.drawString(M, self.Y(812), "Install & Update Guide")
        c.drawRightString(RIGHT, self.Y(812), str(self.page))

    def _cover_header(self):
        c = self.c
        c.setFillColor(DARK)
        c.rect(0, self.Y(153.1), PAGE_W, 153.1, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(0, self.Y(156.1), PAGE_W, 3, stroke=0, fill=1)
        if os.path.exists(LOGO):
            c.drawImage(LOGO, M, self.Y(59.1), width=42.5, height=21.7,
                        preserveAspectRatio=True, mask="auto")
        c.setFont("SegoeUI-Bold", 8.5); c.setFillColor(BADGE_BLUE)
        c.drawString(M + 53, self.Y(52.5), "E T E R N A L G Y")
        c.setFont("SegoeUI-Semibold", 25); c.setFillColor(WHITE)
        c.drawString(M, self.Y(90), "Commission Portal")
        c.setFillColor(LIGHT_BLUE)
        c.drawString(M, self.Y(122), "Install & Update Guide")
        c.setFont("SegoeUI", 10); c.setFillColor(SUBTITLE)
        c.drawString(M, self.Y(140.5),
                     f"Finance Commission Dashboard  \u00b7  Version {VERSION}"
                     f"  \u00b7  {DATE_LINE}")
        self._footer()
        self.y = 186

    def _cont_header(self):
        c = self.c
        c.setFillColor(DARK)
        c.rect(0, self.Y(23), PAGE_W, 23, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(0, self.Y(24), PAGE_W, 1, stroke=0, fill=1)
        c.setFont("SegoeUI-Bold", 8)
        c.setFillColor(BADGE_BLUE); c.drawString(M, self.Y(15.5), "ETERNALGY")
        c.setFillColor(WHITE); c.drawRightString(RIGHT, self.Y(15.5), "COMMISSION PORTAL")
        self._footer()
        self.y = 56

    def new_page(self):
        self.c.showPage()
        self.page += 1
        self._cont_header()

    def ensure(self, needed: float):
        if self.y + needed > BOTTOM_LIMIT:
            self.new_page()

    def spacer(self, h: float):
        self.y += h

    # ── primitives ───────────────────────────────────────────────────────────
    def _draw_line_tokens(self, tokens, x, baseline_top):
        c = self.c
        by = self.Y(baseline_top)
        for word, font, size, color, is_code, url in tokens:
            w = pdfmetrics.stringWidth(word, font, size)
            if is_code and word.strip():
                c.setFillColor(CODE_BG)
                c.roundRect(x - 0.5, by - 2.4, w + 4, 11.6, 2, stroke=0, fill=1)
                c.setFillColor(TEXT)
                c.setFont(font, size)
                c.drawString(x + 1.5, by, word)
                x += w + 3.5
                continue
            c.setFillColor(color)
            c.setFont(font, size)
            c.drawString(x, by, word)
            if url:
                c.setStrokeColor(color); c.setLineWidth(0.7)
                c.line(x, by - 1.6, x + w, by - 1.6)
                c.linkURL(url, (x, by - 3, x + w, by + size), relative=0)
            x += w

    def para(self, text, size=10.0, font="SegoeUI", color=TEXT,
             width=None, x=None, leading=None, gap=8.0):
        width = CW if width is None else width
        x = M if x is None else x
        leading = leading or LEADING
        lines = _wrap(text, width, font, size, color)
        self.ensure(len(lines) * leading + 2)
        for ln in lines:
            self.y += leading
            self._draw_line_tokens(ln, x, self.y - 3)
        self.y += gap

    def rule(self, gap_before=8, gap_after=14):
        self.y += gap_before
        self.ensure(6)
        self.c.setStrokeColor(RULE); self.c.setLineWidth(0.7)
        self.c.line(M, self.Y(self.y), RIGHT, self.Y(self.y))
        self.y += gap_after

    def pill(self, label, bg, x=None, advance=True):
        x = M if x is None else x
        w = pdfmetrics.stringWidth(label, "SegoeUI-Bold", 8.2) + 18
        self.ensure(20)
        c = self.c
        c.setFillColor(bg)
        c.roundRect(x, self.Y(self.y + 15), w, 15, 7.5, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 8.2)
        c.drawCentredString(x + w / 2, self.Y(self.y + 10.8), label)
        if advance:
            self.y += 15
        return w

    # ── icon glyphs (white, inside a 20x20 badge at top-left x,y) ────────────
    def _badge(self, x, y, color, shape="circle"):
        c = self.c
        c.setFillColor(color)
        if shape == "circle":
            c.circle(x + 10, self.Y(y + 10), 10, stroke=0, fill=1)
        else:
            c.roundRect(x, self.Y(y + 20), 20, 20, 5, stroke=0, fill=1)

    def icon(self, kind, x, y, color=ACCENT):
        c = self.c
        self._badge(x, y, color, "rect" if kind in ("lock",) else "circle")
        cx, cy = x + 10, y + 10
        c.setStrokeColor(WHITE); c.setFillColor(WHITE)
        c.setLineWidth(1.6); c.setLineCap(1); c.setLineJoin(1)
        if kind == "download":
            c.line(cx, self.Y(cy - 4.5), cx, self.Y(cy + 1.5))
            p = c.beginPath()
            p.moveTo(cx - 3, self.Y(cy + 0.2)); p.lineTo(cx + 3, self.Y(cy + 0.2))
            p.lineTo(cx, self.Y(cy + 3.6)); p.close()
            c.drawPath(p, stroke=0, fill=1)
            c.line(cx - 4.5, self.Y(cy + 5.2), cx + 4.5, self.Y(cy + 5.2))
        elif kind == "play":
            p = c.beginPath()
            p.moveTo(cx - 2.6, self.Y(cy - 4.4)); p.lineTo(cx + 4.2, self.Y(cy))
            p.lineTo(cx - 2.6, self.Y(cy + 4.4)); p.close()
            c.drawPath(p, stroke=0, fill=1)
        elif kind == "person":
            c.circle(cx, self.Y(cy - 2.6), 2.5, stroke=1, fill=0)
            c.wedge(cx - 4.2, self.Y(cy + 9.4), cx + 4.2, self.Y(cy + 1),
                    0, 180, stroke=0, fill=1)
        elif kind == "key":
            # bow on the left, blade running right with two teeth
            c.setLineWidth(1.5)
            c.circle(cx - 3.4, self.Y(cy), 3.2, stroke=1, fill=0)
            c.line(cx - 0.2, self.Y(cy), cx + 5.6, self.Y(cy))
            c.line(cx + 2.0, self.Y(cy), cx + 2.0, self.Y(cy + 2.8))
            c.line(cx + 4.6, self.Y(cy), cx + 4.6, self.Y(cy + 2.4))
        elif kind == "check":
            p = c.beginPath()
            p.moveTo(cx - 4, self.Y(cy + 0.4)); p.lineTo(cx - 1.2, self.Y(cy + 3.2))
            p.lineTo(cx + 4.4, self.Y(cy - 3))
            c.drawPath(p, stroke=1, fill=0)
        elif kind == "refresh":
            c.setLineWidth(1.7)
            p = c.beginPath()
            p.arc(cx - 4.6, self.Y(cy + 4.6), cx + 4.6, self.Y(cy - 4.6), 200, 250)
            c.drawPath(p, stroke=1, fill=0)
            p = c.beginPath()
            p.moveTo(cx + 1.0, self.Y(cy - 6.2)); p.lineTo(cx + 6.2, self.Y(cy - 4.0))
            p.lineTo(cx + 1.2, self.Y(cy - 1.6)); p.close()
            c.drawPath(p, stroke=0, fill=1)
        elif kind == "lock":
            c.roundRect(cx - 4.4, self.Y(cy + 5.4), 8.8, 6.4, 1.2, stroke=0, fill=1)
            c.setLineWidth(1.5)
            c.arc(cx - 2.8, self.Y(cy + 1.2), cx + 2.8, self.Y(cy - 4.6), 0, 180)
        elif kind == "warning":
            c.setLineWidth(1.5)
            p = c.beginPath()
            p.moveTo(cx, self.Y(cy - 4.4)); p.lineTo(cx + 5, self.Y(cy + 4))
            p.lineTo(cx - 5, self.Y(cy + 4)); p.close()
            c.drawPath(p, stroke=1, fill=0)
            c.setLineWidth(1.3)
            c.line(cx, self.Y(cy - 1.2), cx, self.Y(cy + 1.4))

    # ── composed blocks ──────────────────────────────────────────────────────
    def part_heading(self, part_label, title, icon_kind=None, color=ACCENT):
        self.ensure(110)
        self.pill(part_label, color)
        self.spacer(10)
        x = M
        if icon_kind:
            self.icon(icon_kind, M, self.y + 2, color)
            x = M + 36
        self.c.setFont("SegoeUI-Semibold", 17); self.c.setFillColor(DARK)
        self.c.drawString(x, self.Y(self.y + 19), title)
        self.y += 26
        self.c.setStrokeColor(color); self.c.setLineWidth(2.2)
        self.c.line(M, self.Y(self.y), M + 73.7, self.Y(self.y))
        self.y += 16

    def step_heading(self, icon_kind, title):
        self.ensure(76)
        self.spacer(12)
        self.icon(icon_kind, M, self.y)
        self.c.setFont("SegoeUI-Semibold", 13); self.c.setFillColor(DARK)
        self.c.drawString(M + 36, self.Y(self.y + 14.4), title)
        self.y += 30

    def sub_heading(self, title, keep=0):
        """keep = height of the block that must stay with this heading."""
        self.ensure(40 + keep)
        self.spacer(10)
        self.c.setFont("SegoeUI-Semibold", 12.5); self.c.setFillColor(DARK)
        self.c.drawString(M, self.Y(self.y + 13), title)
        self.y += 22

    @staticmethod
    def _numbered_h(text):
        lines = _wrap(text, CW - 24, "SegoeUI", 10, TEXT)
        return max(15.0, len(lines) * LEADING) + 10

    def numbered(self, n, text):
        lines = _wrap(text, CW - 24, "SegoeUI", 10, TEXT)
        h = max(15.0, len(lines) * LEADING)
        self.ensure(h + 10)
        c = self.c
        c.setFillColor(NUM_BG)
        c.roundRect(M, self.Y(self.y + 15), 15, 15, 3, stroke=0, fill=1)
        c.setFillColor(NUM_FG); c.setFont("SegoeUI-Bold", 8.4)
        c.drawCentredString(M + 7.5, self.Y(self.y + 10.4), str(n))
        yy = self.y
        for ln in lines:
            yy += LEADING
            self._draw_line_tokens(ln, M + 24, yy - 3)
        self.y += h + 10

    def platform_block(self, label, bg, items):
        # Widow control: the WINDOWS/MAC pill must not end a page on its own,
        # so require room for the pill plus its first two steps. Splitting a
        # long list across a page turn is fine; stranding the label is not.
        head_h = 2 + 15 + 9 + sum(self._numbered_h(t) for t in items[:2])
        self.ensure(head_h)
        self.spacer(2)
        self.pill(label, bg)
        self.spacer(9)
        for i, item in enumerate(items, 1):
            self.numbered(i, item)
        self.spacer(2)

    def callout(self, paragraphs):
        wrapped = [_wrap(p, CW - 46 - 16, "SegoeUI", 10, TEXT) for p in paragraphs]
        n_lines = sum(len(w) for w in wrapped)
        h = n_lines * LEADING + (len(wrapped) - 1) * 6 + 24
        self.ensure(h + 8)
        c = self.c
        top = self.y
        c.setFillColor(NOTE_BG)
        c.rect(M, self.Y(top + h), CW, h, stroke=0, fill=1)
        c.setStrokeColor(NOTE_BAR); c.setLineWidth(3)
        c.line(M, self.Y(top), M, self.Y(top + h))
        self.icon("lock", M + 12, top + 11, NOTE_BAR)
        yy = top + 12
        for wl in wrapped:
            for ln in wl:
                yy += LEADING
                self._draw_line_tokens(ln, M + 46, yy - 3)
            yy += 6
        self.y = top + h + 12

    def green_cards(self, cards):
        col_w = 229.0
        gap = CW - 2 * col_w
        for row in range(0, len(cards), 2):
            pair = cards[row:row + 2]
            wrapped = [_wrap(t, col_w - 42 - 12, "SegoeUI", 9.7, TEXT) for t in pair]
            hts = [22 + len(w) * 13.4 + 10 for w in wrapped]
            row_h = max(hts)
            self.ensure(row_h + 10)
            for i, (w, h) in enumerate(zip(wrapped, hts)):
                x = M + i * (col_w + gap)
                c = self.c
                c.setFillColor(GREEN_BG)
                c.rect(x, self.Y(self.y + h), col_w, h, stroke=0, fill=1)
                c.setStrokeColor(GREEN_TOP); c.setLineWidth(0.8)
                c.line(x, self.Y(self.y), x + col_w, self.Y(self.y))
                # green check circle
                c.setFillColor(GREEN)
                c.circle(x + 18, self.Y(self.y + 19), 8, stroke=0, fill=1)
                c.setStrokeColor(WHITE); c.setLineWidth(1.5)
                c.setLineCap(1); c.setLineJoin(1)
                p = c.beginPath()
                p.moveTo(x + 14.6, self.Y(self.y + 19.2))
                p.lineTo(x + 17, self.Y(self.y + 21.6))
                p.lineTo(x + 21.6, self.Y(self.y + 16.4))
                c.drawPath(p, stroke=1, fill=0)
                yy = self.y + 11
                for ln in w:
                    yy += 13.4
                    self._draw_line_tokens(ln, x + 42, yy - 3)
            self.y += row_h + 10

    def trouble_table(self, rows):
        col2_x = 260.0
        header_h = 29.0
        self.ensure(header_h + 40)
        c = self.c
        # header
        c.setFillColor(RED)
        c.rect(M, self.Y(self.y + header_h), CW, header_h, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 8)
        c.drawString(M + 18, self.Y(self.y + 18.6), "WHAT YOU SEE")
        c.drawString(col2_x, self.Y(self.y + 18.6), "WHAT TO DO")
        self.y += header_h
        for i, (what, todo) in enumerate(rows):
            w1 = _wrap(what, col2_x - (M + 18) - 12, "SegoeUI", 10, TEXT)
            w2 = _wrap(todo, RIGHT - col2_x - 10, "SegoeUI", 10, TEXT)
            h = max(len(w1), len(w2)) * LEADING + 16
            self.ensure(h + 4)
            if i % 2 == 0:
                c.setFillColor(RED_ROW)
                c.rect(M, self.Y(self.y + h), CW, h, stroke=0, fill=1)
            yy = self.y + 8
            for ln in w1:
                yy += LEADING
                self._draw_line_tokens(ln, M + 18, yy - 3)
            yy = self.y + 8
            for ln in w2:
                yy += LEADING
                self._draw_line_tokens(ln, col2_x, yy - 3)
            self.y += h
            c.setStrokeColor(RED_LINE); c.setLineWidth(0.5)
            c.line(M, self.Y(self.y), RIGHT, self.Y(self.y))
        c.setStrokeColor(RED); c.setLineWidth(1.3)
        c.line(M, self.Y(self.y), RIGHT, self.Y(self.y))
        self.y += 14

    def screenshot_panel(self, caption):
        panel_w, img_w = 196.0, 176.0
        img_h = img_w * 903 / 888
        panel_h = 10 + 15 + 8 + img_h + 10
        self.ensure(panel_h + 30)
        c = self.c
        x = (PAGE_W - panel_w) / 2
        top = self.y
        c.setFillColor(SHADOW)
        c.roundRect(x + 1.5, self.Y(top + panel_h + 2), panel_w, panel_h, 6,
                    stroke=0, fill=1)
        c.setFillColor(WHITE); c.setStrokeColor(ACCENT); c.setLineWidth(1.1)
        c.roundRect(x, self.Y(top + panel_h), panel_w, panel_h, 6,
                    stroke=1, fill=1)
        # pill
        label = "WHAT YOU'LL SEE"
        pw = pdfmetrics.stringWidth(label, "SegoeUI-Bold", 8.2) + 18
        c.setFillColor(ACCENT)
        c.roundRect(x + 10, self.Y(top + 10 + 15), pw, 15, 7.5, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 8.2)
        c.drawCentredString(x + 10 + pw / 2, self.Y(top + 10 + 10.8), label)
        if os.path.exists(SCREENSHOT):
            c.drawImage(SCREENSHOT, x + 10, self.Y(top + 33 + img_h),
                        width=img_w, height=img_h)
        self.y = top + panel_h + 14
        # caption
        lines = _wrap(caption, CW, "SegoeUI-Italic", 9, MUTED)
        for ln in lines:
            total = sum(pdfmetrics.stringWidth(t[0], t[1], t[2]) for t in ln)
            self.y += 12.5
            self._draw_line_tokens(ln, (PAGE_W - total) / 2, self.y - 3)
        self.y += 10

    def save(self):
        self.c.save()


# ══ Build the document ════════════════════════════════════════════════════════
g = Guide()

g.para("A short guide for everyone using the Commission Portal — on "
       "**Windows or Mac**. You install it **once**. After that it keeps "
       "itself up to date.")
g.para("*(Once it is running, see* [Using the Commission Portal]"
       "(https://github.com/NurulAqilahSaifulBahril/Commission)*.)*", gap=4)
g.rule()

# ── Part 1 ────────────────────────────────────────────────────────────────────
g.part_heading("PART 1", "Part 1 — Installing")
g.para("Set aside about 5 minutes. There is nothing to install beforehand — "
       "the Portal brings everything it needs with it.")

g.step_heading("download", "Step 1 — Download the Portal")
g.numbered(1, f"Go to the [Commission Portal download page]({RELEASES_URL}).")
g.numbered(2, "Scroll down to the **Assets** list.")
g.numbered(3, "**Windows:** click `CommissionDashboard-Setup-….exe` to "
              "download it.")
g.numbered(4, "**Mac:** click `CommissionDashboard-Setup-…-macos.zip` to "
              "download it.")

g.step_heading("play", "Step 2 — Install")
g.platform_block("WINDOWS", ACCENT, [
    "Open the file you just downloaded.",
    "**If Windows shows a blue \"Windows protected your PC\" box:** click "
    "**More info**, then **Run anyway**. This is normal — Windows shows it "
    "for any app it has not seen before.",
    "Click **Next** through the screens. Tick **Create a desktop shortcut** "
    "if you would like one.",
    "Click **Install**.",
])
g.platform_block("MAC", DARK, [
    "Open the `.zip` file you just downloaded (it usually unzips by itself).",
    "Drag **Commission Portal.app** into your **Applications** folder.",
    "**If macOS says the app is from an unidentified developer:** "
    "right-click the app, choose **Open**, then click **Open** again. This "
    "is normal for apps outside the App Store.",
])

g.step_heading("key", "Step 3 — Add your access keys")
g.para("The Portal needs keys to reach the company data. **Ask IT for your "
       "access keys** — they will send you a few lines of text that look "
       "like `SOMETHING=a-long-code`.")
g.platform_block("WINDOWS", ACCENT, [
    "Open the Portal's folder. The installer shows you where it is — the "
    "folder is called **Commission Dashboard**.",
    "Find the file named `.env` and open it with Notepad (right-click → "
    "**Open with** → **Notepad**).",
    "Go to the end of the file and paste in the lines IT sent you, each on "
    "its own line.",
    "Save (**Ctrl+S**) and close Notepad.",
])
g.platform_block("MAC", DARK, [
    "Open **Finder** → **Applications**.",
    "Right-click **Commission Portal.app** → **Show Package Contents**.",
    "Go to `Contents/Resources/` and open the **Commission Dashboard** folder.",
    "Open `.env` with **TextEdit** (right-click → **Open With** → "
    "**TextEdit**).",
    "Go to the end of the file and paste in the lines IT sent you, each on "
    "its own line.",
    "Save (**Cmd+S**) and close TextEdit.",
])
g.callout([
    "Keep these keys private. Do not email them or send them in a chat "
    "message.",
    "**Note for IT:** accounts live in the shared database and are issued "
    "before the install, so the installer never asks for one. Use "
    "`create_admin.py` to add or reset an account, and seed `.env` during the "
    "install rather than leaving the keys to the user.",
])

g.step_heading("check", "Step 4 — Open the Portal")
g.para("**Windows:** Start Menu → **Finance Commission Dashboard** (or the "
       "desktop shortcut).")
g.para("**Mac:** **Applications** → **Commission Portal**.")
g.para("Log in with the **username and password IT gave you**. There is no "
       "account to create — yours already exists. The first load takes a "
       "moment while it fetches the year's data. **You are done.**")
g.rule()

# ── Part 2 ────────────────────────────────────────────────────────────────────
g.part_heading("PART 2", "Part 2 — Updating", icon_kind="refresh")
g.para("**Short version: you don't have to do anything.** The Portal checks "
       "for new versions by itself and tells you when one is ready. This "
       "works the same on Windows and Mac.")
g.sub_heading("When an update is ready")
g.para("A **Software Update** box appears at the **bottom of the left-hand "
       "menu**, showing the new version number.")
g.screenshot_panel("The Software Update panel, showing a new version "
                   "available with an Install Update button")
g.para("**If you see an \"Install Update\" button:**", gap=6)
g.numbered(1, "Click **Install Update**.")
g.numbered(2, "Click **OK** on the confirmation message.")
g.numbered(3, "Wait. A progress bar runs, the Portal restarts itself, and "
              "the page reloads on its own. This takes a minute or two.")
g.numbered(4, "**Do not close the window while it is working.**")
g.para("**If you do not see a button**, the box says \"*Ask an admin to "
       "install it.*\" — there is nothing for you to do. Let your admin know.")
g.sub_heading("Things worth knowing", keep=130)
g.green_cards([
    "**Nothing of yours is lost.** Your login, your access key, your Excel "
    "files, saved reports and any rates you edited all stay exactly as they "
    "are. An update only replaces the program itself.",
    "**You never download the installer again.** Steps 1–4 above are one "
    "time only.",
    "**To check your version:** look at the bottom-left corner of the menu.",
    "**If an update fails**, the Portal puts the old version back by itself "
    "and keeps working. Tell IT so they can look into it.",
])
g.rule()

# ── Troubleshooting ───────────────────────────────────────────────────────────
g.part_heading("TROUBLESHOOTING", "If something goes wrong",
               icon_kind="warning", color=RED)
g.trouble_table([
    ("\"Token expired\"", "Ask IT for a new access key, then redo Step 3"),
    ("The Portal will not open",
     "Tell IT — ask them to check `dashboard.log` in the Portal's folder"),
    ("**Mac:** \"cannot be opened because it is from an unidentified "
     "developer\"",
     "Right-click **Commission Portal.app** and choose **Open**, then click "
     "**Open** again. You only need to do this the first time."),
    ("**Mac:** \"Commission Portal is damaged and can't be opened\"",
     "The download did not finish cleanly. Delete the app, download the "
     "`-macos.zip` again from the release page, and open it with "
     "right-click → **Open**."),
    ("An update failed", "Tell IT — ask them to check `dashboard.log`"),
    ("Numbers look out of date",
     "Wait a few minutes, or click **Sync Data** at the top right"),
])

g.save()
print("Wrote", OUT)
