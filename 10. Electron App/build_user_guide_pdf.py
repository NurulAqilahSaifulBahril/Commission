"""Generate 'docs/Commission-Portal-User-Guide.pdf' in the Eternalgy house style.

Keeps the original guide design (dark cover band, blue accent, pill badges,
numbered chips, green cards) and covers macOS alongside Windows.

The guide is deliberately a SINGLE A4 page -- people print it and pin it up,
so it has to fit. Everything here is sized against that budget: the cover band
is short, Windows and Mac install steps sit side by side in two columns, and
the build asserts the page count at the end. Adding a paragraph means taking
one out.

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
# The guide is published from docs/ so GitHub renders it and the README can
# link to a stable, space-free URL. Only the build inputs stay next to this
# script.
DOCS = os.path.join(os.path.dirname(HERE), "docs")
OUT = os.path.join(DOCS, "Commission-Portal-User-Guide.pdf")
# Page 1 rendered to PNG. A README cannot preview a PDF -- GitHub strips
# <iframe>/<object>/<embed> and renders images only -- so the cover thumbnail
# is what makes the guide visible there. Regenerated with the PDF so it cannot
# drift.
COVER = os.path.join(DOCS, "img", "user-guide-cover.png")
COVER_DPI = 150
LOGO = os.path.join(HERE, "assets", "logo-header.png")
SCREENSHOT = os.path.join(DOCS, "img", "update-panel.png")

VERSION = "1.2.26"
DATE_LINE = "September 2026"
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
SHADOW = HexColor("#d7dee7")
WHITE = HexColor("#ffffff")

# ── Page geometry (A4, matching the original) ────────────────────────────────
PAGE_W, PAGE_H = 595.28, 841.89
M = 56.69                       # left margin
RIGHT = 538.58                  # right content edge
CW = RIGHT - M                  # content width
BOTTOM_LIMIT = 792              # last usable y (top-based)
BODY = 9.0                      # body text size
LEADING = 11.9
COL_GAP = 20.0                  # gutter between the WINDOWS and MAC columns
COL_W = (CW - COL_GAP) / 2

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


def _wrap(text: str, width: float, base_font="SegoeUI", size=BODY, color=TEXT):
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
        c.rect(0, self.Y(104), PAGE_W, 104, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(0, self.Y(107), PAGE_W, 3, stroke=0, fill=1)
        if os.path.exists(LOGO):
            c.drawImage(LOGO, M, self.Y(45), width=38, height=19.4,
                        preserveAspectRatio=True, mask="auto")
        c.setFont("SegoeUI-Bold", 8.5); c.setFillColor(BADGE_BLUE)
        c.drawString(M + 48, self.Y(39.5), "E T E R N A L G Y")
        # Title and subtitle share one line. Stacked they read better but cost
        # 32pt, and the single-page budget does not have 32pt to give.
        c.setFont("SegoeUI-Semibold", 22); c.setFillColor(WHITE)
        c.drawString(M, self.Y(74), "Commission Portal")
        w = pdfmetrics.stringWidth("Commission Portal", "SegoeUI-Semibold", 22)
        c.setFillColor(LIGHT_BLUE); c.setFont("SegoeUI-Semibold", 15)
        c.drawString(M + w + 12, self.Y(73), "Install & Update Guide")
        c.setFont("SegoeUI", 9.2); c.setFillColor(SUBTITLE)
        c.drawString(M, self.Y(93),
                     "Finance Commission Dashboard  ·  Version %s"
                     "  ·  %s" % (VERSION, DATE_LINE))
        self._footer()
        self.y = 126

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

    def para(self, text, size=BODY, font="SegoeUI", color=TEXT,
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
        """Pill, icon and title on ONE line -- stacked they cost 40pt each."""
        self.ensure(46)
        w = self.pill(part_label, color, advance=False)
        x = M + w + 10
        if icon_kind:
            self.icon(icon_kind, x, self.y - 2.5, color)
            x += 27
        self.c.setFont("SegoeUI-Semibold", 14.5); self.c.setFillColor(DARK)
        self.c.drawString(x, self.Y(self.y + 12.6), title)
        self.y += 19
        self.c.setStrokeColor(color); self.c.setLineWidth(2.0)
        self.c.line(M, self.Y(self.y), M + 60, self.Y(self.y))
        self.y += 9

    def step_heading(self, icon_kind, title):
        self.ensure(48)
        self.spacer(4)
        self.icon(icon_kind, M, self.y - 2)
        self.c.setFont("SegoeUI-Semibold", 11.5); self.c.setFillColor(DARK)
        self.c.drawString(M + 27, self.Y(self.y + 12), title)
        self.y += 22

    def sub_heading(self, title, keep=0):
        """keep = height of the block that must stay with this heading."""
        self.ensure(32 + keep)
        self.spacer(6)
        self.c.setFont("SegoeUI-Semibold", 11.5); self.c.setFillColor(DARK)
        self.c.drawString(M, self.Y(self.y + 11.5), title)
        self.y += 18

    @staticmethod
    def _numbered_h(text, width=CW - 22):
        lines = _wrap(text, width, "SegoeUI", BODY, TEXT)
        return max(14.0, len(lines) * LEADING) + 6

    def _numbered_at(self, n, text, x, width, y):
        """Draw one numbered step at an absolute spot; return the y below it."""
        lines = _wrap(text, width, "SegoeUI", BODY, TEXT)
        h = max(14.0, len(lines) * LEADING)
        c = self.c
        c.setFillColor(NUM_BG)
        c.roundRect(x, self.Y(y + 14), 14, 14, 3, stroke=0, fill=1)
        c.setFillColor(NUM_FG); c.setFont("SegoeUI-Bold", 8.0)
        c.drawCentredString(x + 7, self.Y(y + 9.9), str(n))
        yy = y
        for ln in lines:
            yy += LEADING
            self._draw_line_tokens(ln, x + 22, yy - 3)
        return y + h + 6

    def numbered(self, n, text):
        self.ensure(self._numbered_h(text))
        self.y = self._numbered_at(n, text, M, CW - 22, self.y)

    def two_col(self, left, right, left_w=COL_W):
        """Two platform columns side by side, each (label, colour, [steps]).

        Stacked, the Windows and Mac lists ran most of a page on their own.
        Side by side they cost the height of the taller list only, which is
        what buys the room for Part 2 below them.

        left_w exists because an even split wastes the page: the Mac list is
        much longer, so equal columns leave a third of the Windows column
        blank while pushing Mac onto extra lines. Narrowing Windows to 170pt
        brings both columns out within a line of each other and gives back
        45pt -- most of this page's breathing room. Re-tune it if either
        list changes length.
        """
        widths = (left_w, CW - COL_GAP - left_w)
        cols = (left, right)
        heights = [22 + sum(self._numbered_h(t, w - 22) for t in items)
                   for (_label, _bg, items), w in zip(cols, widths)]
        self.ensure(max(heights) + 4)
        top = self.y
        x = M
        for (label, bg, items), w in zip(cols, widths):
            self.y = top
            self.pill(label, bg, x=x)
            self.spacer(7)
            for n, item in enumerate(items, 1):
                self.y = self._numbered_at(n, item, x, w - 22, self.y)
            x += w + COL_GAP
        self.y = top + max(heights) + 4

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

    def _screenshot(self, x, top, width):
        """Draw the framed update-panel shot at an absolute spot; return its
        height. Returns 0 if the image is missing, so a fresh checkout that
        has not fetched it still builds."""
        if not os.path.exists(SCREENSHOT):
            return 0.0
        pad = 6.0
        img_w = width - 2 * pad
        img_h = img_w * 903 / 888
        h = img_h + 2 * pad
        c = self.c
        c.setFillColor(SHADOW)
        c.roundRect(x + 1.5, self.Y(top + h + 2), width, h, 5, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setStrokeColor(ACCENT); c.setLineWidth(1.0)
        c.roundRect(x, self.Y(top + h), width, h, 5, stroke=1, fill=1)
        c.drawImage(SCREENSHOT, x + pad, self.Y(top + pad + img_h),
                    width=img_w, height=img_h)
        return h

    def steps_beside_figure(self, intro, steps, fig_w=140.0, gap=16.0):
        """Update steps on the left, the Software Update screenshot on the
        right.

        The screenshot is the one thing people actually recognise -- the box
        in the sidebar -- so it earns its place even on a one-page handout.
        Beside the steps it costs the height of the taller side; stacked
        under them, as it used to be, it cost a quarter of the page.
        """
        text_w = CW - fig_w - gap
        img_h = (fig_w - 12) * 903 / 888 + 12
        h_text = (len(_wrap(intro, text_w)) * LEADING + 5
                  + sum(self._numbered_h(t, text_w - 22) for t in steps))
        self.ensure(max(h_text, img_h) + 4)
        top = self.y
        self._screenshot(RIGHT - fig_w, top, fig_w)
        self.para(intro, width=text_w, gap=5)
        for n, step in enumerate(steps, 1):
            self.y = self._numbered_at(n, step, M, text_w - 22, self.y)
        self.y = max(self.y, top + img_h) + 8

    def green_cards(self, cards):
        col_w = 229.0
        gap = CW - 2 * col_w
        for row in range(0, len(cards), 2):
            pair = cards[row:row + 2]
            wrapped = [_wrap(t, col_w - 38 - 10, "SegoeUI", 8.9, TEXT) for t in pair]
            hts = [15 + len(w) * 11.6 + 7 for w in wrapped]
            row_h = max(hts)
            self.ensure(row_h + 6)
            # Both cards get the taller card's height -- a short card next to
            # a tall one reads as a rendering fault, not as a design.
            for i, w in enumerate(wrapped):
                x = M + i * (col_w + gap)
                c = self.c
                c.setFillColor(GREEN_BG)
                c.rect(x, self.Y(self.y + row_h), col_w, row_h, stroke=0, fill=1)
                c.setStrokeColor(GREEN_TOP); c.setLineWidth(0.8)
                c.line(x, self.Y(self.y), x + col_w, self.Y(self.y))
                # green check circle
                c.setFillColor(GREEN)
                c.circle(x + 16, self.Y(self.y + 14), 7, stroke=0, fill=1)
                c.setStrokeColor(WHITE); c.setLineWidth(1.4)
                c.setLineCap(1); c.setLineJoin(1)
                p = c.beginPath()
                p.moveTo(x + 13, self.Y(self.y + 14.2))
                p.lineTo(x + 15.1, self.Y(self.y + 16.3))
                p.lineTo(x + 19.1, self.Y(self.y + 11.7))
                c.drawPath(p, stroke=1, fill=0)
                yy = self.y + 6
                for ln in w:
                    yy += 11.6
                    self._draw_line_tokens(ln, x + 38, yy - 3)
            self.y += row_h + 6

    def save(self):
        self.c.save()


# ══ Build the document ════════════════════════════════════════════════════════
g = Guide()

g.para("A short guide for everyone using the Commission Portal, on **Windows "
       "or Mac**. You install it **once** — about 5 minutes, with nothing to "
       "install beforehand — and after that it keeps itself up to date. "
       "*(See also* [Using the Portal]"
       "(https://github.com/NurulAqilahSaifulBahril/Commission)*.)*")
g.rule(gap_before=2, gap_after=8)

# ── Part 1 ────────────────────────────────────────────────────────────────────
g.part_heading("PART 1", "Installing")

g.step_heading("download", "Step 1 — Download the Portal")
g.numbered(1, f"Open the [Commission Portal download page]({RELEASES_URL}) "
              "and scroll down to the **Assets** list.")
g.numbered(2, "**Windows:** download `CommissionDashboard-Setup-….exe`.")
g.numbered(3, "**Mac:** Apple menu → **About This Mac**. **Apple M1/M2/M3…** "
              "→ `…-macos-arm64.dmg`; **Intel** → `…-macos-intel.dmg`.")

g.step_heading("play", "Step 2 — Install")
g.two_col(
    ("WINDOWS", ACCENT, [
        "Open the file you just downloaded.",
        "If a blue **Windows protected your PC** box appears: **More info**, "
        "then **Run anyway**. Normal for any new app.",
        "Click **Next** through the screens — tick **Create a desktop "
        "shortcut** if you want one — then **Install**.",
    ]),
    ("MAC", DARK, [
        "Open the `.dmg`. Drag the **Commission Dashboard** folder onto "
        "**Applications** and wait — it is about 700 MB.",
        "Open it from **Applications**, never from the disk image.",
        "macOS blocks it the first time. Right-click **CommissionDashboard** "
        "→ **Open** → **Open**. On **Sequoia or newer**: **System Settings** "
        "→ **Privacy & Security** → **Open Anyway**.",
        "Drag refused (*you do not have permission*)? Double-click **Install "
        "Commission Dashboard** on the disk image instead.",
    ]),
    left_w=170.0,
)

g.step_heading("key", "Step 3 — Open the Portal and log in")
g.para("**Windows:** Start Menu → **Finance Commission Dashboard**. **Mac:** "
       "**Applications** → **Commission Dashboard**. Log in with the "
       "**username and password IT gave you** — your account already exists, "
       "there is nothing to create. The first load takes a moment while it "
       "fetches the year’s data. **You are done.**")
g.rule(gap_before=2, gap_after=8)

# ── Part 2 ────────────────────────────────────────────────────────────────────
g.part_heading("PART 2", "Updating", icon_kind="refresh")
g.steps_beside_figure(
    "**You do not have to do anything.** The Portal checks for new versions "
    "itself, on Windows and Mac alike. When one is ready, the **Software "
    "Update** box on the right appears at the **bottom of the left-hand "
    "menu**.",
    [
        "Click **Install Update**, then **OK**.",
        "Wait a minute or two — a progress bar runs, the Portal restarts and "
        "the page reloads on its own. **Do not close the window.**",
        "**No button?** The box says *Ask an admin to install it* — nothing "
        "for you to do. Let your admin know.",
    ],
)
g.green_cards([
    "**Nothing of yours is lost.** Logins, files and saved reports all stay.",
    "**You never download the installer again.** Your version sits "
    "bottom-left.",
])

# The guide is a one-page handout by design - people print it and pin it up.
# Fail the build rather than quietly shipping a second page nobody prints.
if g.page != 1:
    raise SystemExit(
        f"The guide has grown to {g.page} pages. It has to stay one page: "
        "trim copy or tighten spacing before rebuilding.")

g.save()
print("Wrote", OUT)


def write_cover_thumbnail() -> None:
    """Render page 1 of the finished PDF to COVER."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("PyMuPDF not installed - skipped the cover thumbnail. "
              "Install it with:  pip install pymupdf")
        return

    os.makedirs(os.path.dirname(COVER), exist_ok=True)
    with fitz.open(OUT) as doc:
        page = doc.load_page(0)
        page.get_pixmap(dpi=COVER_DPI).save(COVER)
    print("Wrote", COVER)


write_cover_thumbnail()
