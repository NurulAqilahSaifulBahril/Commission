"""Generate 'docs/Commission-Portal-User-Guide.pdf' in the Eternalgy house style.

Reproduces the original guide design (dark cover band, blue accent, pill
badges, numbered chips, amber callout, green cards, red troubleshooting
table) and adds macOS instructions alongside the Windows ones.

Run:  python "10. Electron App/build_user_guide_pdf.py"
"""

from __future__ import annotations

import glob
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

VERSION = "1.2.28"
DATE_LINE = "September 2026"
RELEASES_URL = "https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest"

# ── Fonts (Segoe UI family + Consolas, as in the original) ───────────────────
# On Windows this resolves to exactly the original faces, so a build there is
# unchanged. It used to be able to resolve to nothing else: the path was
# hardcoded to C:\Windows\Fonts, which meant the guide could not be rebuilt on
# a Mac at all -- so a release cut from a Mac shipped install instructions that
# no longer matched the installer, which is worse than any typeface.
#
# Office for Mac ships Consolas but NOT Segoe UI (only Segoe Print, Script and
# Symbol), so Aptos stands in for the body face there -- Microsoft's own
# humanist UI sans, carrying the same four weights this document needs,
# semibold included. A Mac build is therefore legible and internally
# consistent, but NOT identical to a Windows one. Rebuild on Windows before a
# release if the exact house style matters; the substitution is announced on
# stdout so it cannot happen quietly.
FONT_DIRS = [
    r"C:\Windows\Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
] + sorted(glob.glob("/Applications/Microsoft */Contents/Resources/DFonts"))

# First entry is the house style; the rest are fallbacks, best first.
FONT_CANDIDATES = {
    "SegoeUI": ["segoeui.ttf", "Aptos.ttf"],
    "SegoeUI-Bold": ["segoeuib.ttf", "Aptos-Bold.ttf"],
    "SegoeUI-Italic": ["segoeuii.ttf", "Aptos-Italic.ttf"],
    "SegoeUI-Semibold": ["seguisb.ttf", "Aptos-SemiBold.ttf"],
    "Consolas": ["consola.ttf"],
}


def _register_fonts() -> None:
    chosen: dict[str, str] = {}
    for name, candidates in FONT_CANDIDATES.items():
        for filename in candidates:
            hit = next((os.path.join(d, filename) for d in FONT_DIRS
                        if os.path.isfile(os.path.join(d, filename))), None)
            if hit:
                pdfmetrics.registerFont(TTFont(name, hit))
                chosen[name] = filename
                break
        if name not in chosen:
            raise SystemExit(
                f"ERROR: no font file found for {name}.\n"
                f"       Tried {candidates} in:\n"
                + "\n".join(f"         {d}" for d in FONT_DIRS)
            )
    swapped = [n for n, f in chosen.items() if f != FONT_CANDIDATES[n][0]]
    if swapped:
        print("NOTE: Segoe UI is not installed here, so the guide was set in "
              + ", ".join(sorted({chosen[n] for n in swapped}))
              + ".\n      Content is correct; rebuild on Windows for the exact "
                "house style.")


_register_fonts()

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

# macOS system-dialog chrome, for the two Gatekeeper mocks below. Sampled
# against light-mode System Settings / alert panels, not this document's own
# palette, since the point is to look like what actually appears on screen.
OS_BG = HexColor("#f0f0f2")
OS_BORDER = HexColor("#d2d2d6")
OS_TEXT = HexColor("#1c1c1e")
OS_SUBTEXT = HexColor("#5c5c60")
OS_BTN_BG = HexColor("#e2e2e5")

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
        self._panel_caption(top, panel_h, caption)

    # ── Gatekeeper mocks ─────────────────────────────────────────────────────
    # Drawn rather than screenshotted. The exact wording has already changed
    # once across macOS versions -- the "unidentified developer" dialog with
    # an Open button became "Apple could not verify..." with no in-dialog
    # bypass at all -- so a real screenshot goes stale at the next OS
    # redesign while a redrawn mock of the substance still reads correctly.
    def gatekeeper_blocked_panel(self, caption):
        w, pad, icon_sz = 300.0, 20.0, 42.0
        text_x = pad + icon_sz + 14
        text_w = w - text_x - pad
        title = "“CommissionDashboard” Not Opened"
        body = ("Apple could not verify “CommissionDashboard” is "
                "free of malware that may harm your Mac or compromise your "
                "privacy.")
        body_lines = _wrap(body, text_w, "SegoeUI", 9.0, OS_SUBTEXT)
        block_h = max(icon_sz, 14 + 6 + len(body_lines) * 12.2)
        btn_h = 23.0
        h = pad + block_h + 14 + 12 + btn_h + 18
        self.ensure(h + 40)
        c = self.c
        x = (PAGE_W - w) / 2
        top = self.y
        c.setFillColor(SHADOW)
        c.roundRect(x + 1.5, self.Y(top + h + 2), w, h, 9, stroke=0, fill=1)
        c.setFillColor(OS_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(1)
        c.roundRect(x, self.Y(top + h), w, h, 9, stroke=1, fill=1)
        # app-icon stand-in
        iy = top + pad
        c.setFillColor(ACCENT)
        c.roundRect(x + pad, self.Y(iy + icon_sz), icon_sz, icon_sz, 9,
                    stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 17)
        c.drawCentredString(x + pad + icon_sz / 2, self.Y(iy + icon_sz / 2 + 6), "C")
        tx = x + text_x
        c.setFillColor(OS_TEXT); c.setFont("SegoeUI-Semibold", 10.6)
        c.drawString(tx, self.Y(iy + 13), title)
        yy = iy + 13 + 6
        for ln in body_lines:
            yy += 12.2
            self._draw_line_tokens(ln, tx, yy - 3)
        by_top = top + pad + block_h + 14
        c.setStrokeColor(RULE); c.setLineWidth(0.6)
        c.line(x + pad, self.Y(by_top), x + w - pad, self.Y(by_top))
        label = "Done"
        bw = pdfmetrics.stringWidth(label, "SegoeUI-Semibold", 9.5) + 26
        bx = x + w - pad - bw
        by = by_top + 12
        c.setFillColor(OS_BTN_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.8)
        c.roundRect(bx, self.Y(by + btn_h), bw, btn_h, 6, stroke=1, fill=1)
        c.setFillColor(OS_TEXT); c.setFont("SegoeUI-Semibold", 9.5)
        c.drawCentredString(bx + bw / 2, self.Y(by + btn_h / 2 + 3.3), label)
        self._panel_caption(top, h, caption)

    def gatekeeper_settings_panel(self, caption):
        w, pad = 300.0, 18.0
        row_pad, btn_w, btn_h = 14.0, 96.0, 26.0
        row_w = w - 2 * pad
        text_w = row_w - 2 * row_pad - btn_w - 10
        l1 = _wrap("“CommissionDashboard” was blocked to protect "
                    "your Mac.", text_w, "SegoeUI-Semibold", 9.2, OS_TEXT)
        l2 = _wrap("Click Open Anyway to trust this app and run it.",
                    text_w, "SegoeUI", 8.5, OS_SUBTEXT)
        row_h = max(btn_h + 2 * row_pad,
                    row_pad * 2 + len(l1) * 12.4 + len(l2) * 11.2 + 4)
        h = pad + 20 + row_h + pad
        self.ensure(h + 40)
        c = self.c
        x = (PAGE_W - w) / 2
        top = self.y
        c.setFillColor(SHADOW)
        c.roundRect(x + 1.5, self.Y(top + h + 2), w, h, 9, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setStrokeColor(OS_BORDER); c.setLineWidth(1)
        c.roundRect(x, self.Y(top + h), w, h, 9, stroke=1, fill=1)
        c.setFillColor(MUTED); c.setFont("SegoeUI-Bold", 8)
        c.drawString(x + pad, self.Y(top + pad + 7), "S E C U R I T Y")
        row_top = top + pad + 20
        c.setFillColor(OS_BG)
        c.roundRect(x + pad, self.Y(row_top + row_h), row_w, row_h, 7,
                    stroke=0, fill=1)
        ty = row_top + row_pad
        for ln in l1:
            ty += 12.4
            self._draw_line_tokens(ln, x + pad + row_pad, ty - 3)
        ty += 3
        for ln in l2:
            ty += 11.2
            self._draw_line_tokens(ln, x + pad + row_pad, ty - 3)
        # the button, ringed in the house accent so it reads as "click here"
        # without claiming macOS itself highlights it -- it does not.
        bx = x + w - pad - row_pad - btn_w
        by = row_top + (row_h - btn_h) / 2
        c.setStrokeColor(ACCENT); c.setLineWidth(1.4)
        c.roundRect(bx - 2.5, self.Y(by + btn_h + 2.5), btn_w + 5, btn_h + 5,
                    8, stroke=1, fill=0)
        c.setFillColor(OS_BTN_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.8)
        c.roundRect(bx, self.Y(by + btn_h), btn_w, btn_h, 6, stroke=1, fill=1)
        c.setFillColor(OS_TEXT); c.setFont("SegoeUI-Semibold", 9.5)
        c.drawCentredString(bx + btn_w / 2, self.Y(by + btn_h / 2 + 3.3), "Open Anyway")
        self._panel_caption(top, h, caption)

    # ── compact side-by-side variants, for the one-page layout ──────────────
    # Same substance as the full-size panels above, at a size two can sit
    # side by side. Draw at an explicit (x, top) rather than the flowing
    # self.y, since two of these share one row.
    def _mini_blocked_panel(self, x, top, w):
        pad, icon_sz = 11.0, 24.0
        text_x = pad + icon_sz + 7
        text_w = w - text_x - pad
        title_lines = _wrap("“CommissionDashboard” Not Opened",
                            text_w, "SegoeUI-Semibold", 8.2, OS_TEXT)
        body_lines = _wrap("Apple could not verify this app is "
                           "malware-free.", text_w, "SegoeUI", 7.2, OS_SUBTEXT)
        block_h = max(icon_sz, len(title_lines) * 10.2 + 4 + len(body_lines) * 9.4)
        btn_h = 17.0
        h = pad + block_h + 8 + 8 + btn_h + 10
        c = self.c
        c.setFillColor(SHADOW)
        c.roundRect(x + 1, self.Y(top + h + 1.5), w, h, 7, stroke=0, fill=1)
        c.setFillColor(OS_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.9)
        c.roundRect(x, self.Y(top + h), w, h, 7, stroke=1, fill=1)
        iy = top + pad
        c.setFillColor(ACCENT)
        c.roundRect(x + pad, self.Y(iy + icon_sz), icon_sz, icon_sz, 6,
                    stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 11)
        c.drawCentredString(x + pad + icon_sz / 2, self.Y(iy + icon_sz / 2 + 4), "C")
        tx = x + text_x
        yy = iy
        for ln in title_lines:
            yy += 10.2
            self._draw_line_tokens(ln, tx, yy - 2.6)
        yy += 4
        for ln in body_lines:
            yy += 9.4
            self._draw_line_tokens(ln, tx, yy - 2.2)
        by_top = top + pad + block_h + 8
        c.setStrokeColor(RULE); c.setLineWidth(0.5)
        c.line(x + pad, self.Y(by_top), x + w - pad, self.Y(by_top))
        label = "Done"
        bw = pdfmetrics.stringWidth(label, "SegoeUI-Semibold", 7.4) + 18
        bx = x + w - pad - bw
        by = by_top + 8
        c.setFillColor(OS_BTN_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.7)
        c.roundRect(bx, self.Y(by + btn_h), bw, btn_h, 5, stroke=1, fill=1)
        c.setFillColor(OS_TEXT); c.setFont("SegoeUI-Semibold", 7.4)
        c.drawCentredString(bx + bw / 2, self.Y(by + btn_h / 2 + 2.6), label)
        return h

    def _mini_settings_panel(self, x, top, w):
        pad, row_pad, btn_w, btn_h = 10.0, 9.0, 66.0, 18.0
        row_w = w - 2 * pad
        text_w = row_w - 2 * row_pad - btn_w - 8
        l1 = _wrap("“CommissionDashboard” blocked.", text_w,
                   "SegoeUI-Semibold", 7.6, OS_TEXT)
        l2 = _wrap("Click Open Anyway to trust it.", text_w,
                   "SegoeUI", 7.0, OS_SUBTEXT)
        row_h = max(btn_h + 2 * row_pad,
                    row_pad * 2 + len(l1) * 9.6 + len(l2) * 8.6 + 2)
        h = pad + 15 + row_h + pad
        c = self.c
        c.setFillColor(SHADOW)
        c.roundRect(x + 1, self.Y(top + h + 1.5), w, h, 7, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.9)
        c.roundRect(x, self.Y(top + h), w, h, 7, stroke=1, fill=1)
        c.setFillColor(MUTED); c.setFont("SegoeUI-Bold", 6.6)
        c.drawString(x + pad, self.Y(top + pad + 6), "S E C U R I T Y")
        row_top = top + pad + 15
        c.setFillColor(OS_BG)
        c.roundRect(x + pad, self.Y(row_top + row_h), row_w, row_h, 6,
                    stroke=0, fill=1)
        ty = row_top + row_pad
        for ln in l1:
            ty += 9.6
            self._draw_line_tokens(ln, x + pad + row_pad, ty - 2.6)
        ty += 2
        for ln in l2:
            ty += 8.6
            self._draw_line_tokens(ln, x + pad + row_pad, ty - 2.2)
        bx = x + w - pad - row_pad - btn_w
        by = row_top + (row_h - btn_h) / 2
        c.setStrokeColor(ACCENT); c.setLineWidth(1.1)
        c.roundRect(bx - 2, self.Y(by + btn_h + 2), btn_w + 4, btn_h + 4, 7,
                    stroke=1, fill=0)
        c.setFillColor(OS_BTN_BG); c.setStrokeColor(OS_BORDER); c.setLineWidth(0.7)
        c.roundRect(bx, self.Y(by + btn_h), btn_w, btn_h, 5, stroke=1, fill=1)
        c.setFillColor(OS_TEXT); c.setFont("SegoeUI-Semibold", 7.2)
        c.drawCentredString(bx + btn_w / 2, self.Y(by + btn_h / 2 + 2.6),
                            "Open Anyway")
        return h

    def gatekeeper_mini_row(self, caption):
        w, gap = 218.0, 20.0
        total = w * 2 + gap
        x0 = M + (CW - total) / 2
        self.ensure(130)
        top = self.y
        h1 = self._mini_blocked_panel(x0, top, w)
        h2 = self._mini_settings_panel(x0 + w + gap, top, w)
        self._panel_caption(top, max(h1, h2), caption)

    # Smaller sibling of screenshot_panel, sized to fit the one-page layout's
    # leftover space rather than a full page of its own.
    def mini_screenshot_panel(self, caption):
        pad, pill_h, gap = 8.0, 13.0, 6.0
        img_w = 118.0
        img_h = img_w * 903 / 888
        panel_w = img_w + 2 * pad
        panel_h = pad + pill_h + gap + img_h + pad
        self.ensure(panel_h + 40)
        c = self.c
        x = (PAGE_W - panel_w) / 2
        top = self.y
        c.setFillColor(SHADOW)
        c.roundRect(x + 1.5, self.Y(top + panel_h + 2), panel_w, panel_h, 6,
                    stroke=0, fill=1)
        c.setFillColor(WHITE); c.setStrokeColor(ACCENT); c.setLineWidth(1.0)
        c.roundRect(x, self.Y(top + panel_h), panel_w, panel_h, 6,
                    stroke=1, fill=1)
        label = "WHAT YOU'LL SEE"
        pw = pdfmetrics.stringWidth(label, "SegoeUI-Bold", 6.6) + 14
        c.setFillColor(ACCENT)
        c.roundRect(x + pad, self.Y(top + pad + pill_h), pw, pill_h, 6.5,
                    stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("SegoeUI-Bold", 6.6)
        c.drawCentredString(x + pad + pw / 2, self.Y(top + pad + 9.4), label)
        if os.path.exists(SCREENSHOT):
            c.drawImage(SCREENSHOT, x + pad,
                        self.Y(top + pad + pill_h + gap + img_h),
                        width=img_w, height=img_h)
        self._panel_caption(top, panel_h, caption)

    def mini_heading(self, text):
        self.ensure(24)
        self.spacer(3)
        self.c.setFont("SegoeUI-Semibold", 11); self.c.setFillColor(DARK)
        self.c.drawString(M, self.Y(self.y + 11), text)
        self.y += 15

    def _panel_caption(self, top, h, caption):
        self.y = top + h + 14
        lines = _wrap(caption, CW, "SegoeUI-Italic", 9, MUTED)
        for ln in lines:
            total = sum(pdfmetrics.stringWidth(t[0], t[1], t[2]) for t in ln)
            self.y += 12.5
            self._draw_line_tokens(ln, (PAGE_W - total) / 2, self.y - 3)
        self.y += 10

    def save(self):
        self.c.save()


# ══ Build the document — one page, install + update only ════════════════════
# Troubleshooting deliberately left out: this is the quick version, not the
# full reference. The spacious multi-page components above (platform pill +
# numbered steps, the update screenshot, the green reassurance cards) are
# condensed to inline "Windows:"/"Mac:" sentences so the whole thing fits on
# the cover page.
g = Guide()

g.para("A quick guide for **Windows** and **Mac**. Install once — the "
       "Portal keeps itself updated after that.", size=9.2, leading=11.6,
       gap=6)
g.rule(gap_before=2, gap_after=8)

g.mini_heading("1 · Download")
g.para(f"Go to the [Commission Portal download page]({RELEASES_URL}) → "
       "**Assets**. **Windows:** download the `.exe`. **Mac:** check your "
       "chip first (Apple menu → **About This Mac**) — download the "
       "**arm64** build for Apple Silicon (M1 and newer), or **intel** for "
       "an Intel Mac.", size=9.2, leading=11.6, gap=6)

g.mini_heading("2 · Install")
g.para("**Windows:** open the file. If you see \"Windows protected your "
       "PC\", click **More info** → **Run anyway**, then **Next** → "
       "**Install**.", size=9.2, leading=11.6, gap=5)
g.para("**Mac:** open the `.dmg`, drag **CommissionDashboard** onto "
       "**Applications**, then open it from there.", size=9.2, leading=11.6,
       gap=5)
g.para("**macOS will refuse the first time** — expected, not an error. "
       "**Sequoia/Tahoe** (no Open button in the message): **System "
       "Settings → Privacy & Security → Open Anyway**, shown below. "
       "**Older macOS:** right-click the app → **Open** → **Open**.",
       size=9.2, leading=11.6, gap=6)
g.gatekeeper_mini_row(
    "Left: what macOS shows. Right: Privacy & Security → Open Anyway.")

g.mini_heading("3 · Open & sign in")
g.para("**Windows:** Start Menu → **Finance Commission Dashboard**. "
       "**Mac:** **Applications** → **CommissionDashboard**. Sign in with "
       "the **username and password IT gave you** — there is no account "
       "to create. **You're done** — steps 1–3 are one time only.",
       size=9.2, leading=11.6, gap=6)

g.mini_heading("Updating")
g.para("The Portal checks for updates itself. When one is ready, a "
       "**Software Update** box appears at the bottom of the left-hand "
       "menu — click **Install Update** and wait; it restarts on its own. "
       "Nothing of yours (login, files, edited rates) is touched. "
       "Questions? Ask **IT**.", size=9.2, leading=11.6, gap=4)
g.mini_screenshot_panel("The Software Update panel — click Install Update")

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
