#!/usr/bin/env python3
"""Render a PNG mockup of the proposed 'rendered-markdown inline diff' for JBrain.

Shows a staged UPDATE proposal rendered with FULL markdown formatting, with
red/green inline + block highlighting instead of the current raw-line diff.
"""
from PIL import Image, ImageDraw, ImageFont

S = 2  # supersample for crispness

# ---- theme ---------------------------------------------------------------
BG_PAGE   = (17, 19, 21)      # --bg
BG_ELEV   = (22, 26, 28)      # --bg-elev   (modal)
BG_ELEV2  = (28, 33, 35)      # --bg-elev-2 (card)
BORDER    = (42, 47, 49)      # --border
TEXT      = (216, 220, 222)   # --text
DIM       = (130, 138, 142)   # --text-dim
ACCENT    = (127, 154, 166)   # --accent (wiki-links)
DANGER    = (192, 133, 133)   # --danger
OK        = (132, 165, 145)   # --ok

# diff fills (dark tints) + accent bars
ADD_BG    = (26, 44, 35)
DEL_BG    = (48, 30, 33)
ADD_BAR   = (110, 170, 135)
DEL_BAR   = (190, 110, 110)
ADD_TEXT  = (150, 200, 168)
DEL_TEXT  = (208, 150, 150)

F = "/usr/share/fonts/truetype/dejavu/"
def font(name, sz): return ImageFont.truetype(F + name, sz * S)
reg   = lambda s: font("DejaVuSans.ttf", s)
bold  = lambda s: font("DejaVuSans-Bold.ttf", s)
ital  = lambda s: font("DejaVuSans-Oblique.ttf", s)
mono  = lambda s: font("DejaVuSansMono.ttf", s)
monob = lambda s: font("DejaVuSansMono-Bold.ttf", s)

# ---- canvas --------------------------------------------------------------
W = 920
PAD = 28
img = Image.new("RGB", (W * S, 10), BG_PAGE)
d = ImageDraw.Draw(img)

def tw(text, fnt): return d.textlength(text, font=fnt) / S  # logical px

class Run:
    __slots__ = ("t", "f", "color", "bg", "strike")
    def __init__(self, t, f, color=TEXT, bg=None, strike=False):
        self.t, self.f, self.color, self.bg, self.strike = t, f, color, bg, strike

# A block = list of runs + style. block_diff in {None,'add','del'}.
# kind in {'h1','h2','p','li'}. indent for list items.
blocks = []
def B(runs, kind="p", block_diff=None, indent=0, gap_before=0):
    blocks.append(dict(runs=runs, kind=kind, block_diff=block_diff,
                       indent=indent, gap_before=gap_before))

# Build a tokenised representation by laying out word by word with wrapping.
# We measure during a dry pass to compute total height, then draw.

def split_words(runs):
    """Explode runs into per-word tokens preserving style; keep spaces as own tokens."""
    toks = []
    for r in runs:
        parts = r.t.split(" ")
        for i, p in enumerate(parts):
            if p:
                toks.append(Run(p, r.f, r.color, r.bg, r.strike))
            if i < len(parts) - 1:
                toks.append(Run(" ", r.f, r.color, r.bg, r.strike))
    return toks

KIND_FONT = {"h1": bold(22), "h2": bold(17), "p": reg(15), "li": reg(15)}
KIND_LH   = {"h1": 34, "h2": 27, "p": 25, "li": 25}

def layout_block(b, x0, maxw):
    """Return list of lines; each line = list of (run, x, w). Uses block font as default size."""
    base = KIND_FONT[b["kind"]]
    toks = split_words(b["runs"])
    # ensure each token uses block base size if it was created with placeholder size
    lines, cur, x = [], [], 0
    for tk in toks:
        f = tk.f or base
        wpx = tw(tk.t, f)
        if tk.t == " " and not cur:
            continue
        if x + wpx > maxw and cur:
            lines.append(cur); cur, x = [], 0
            if tk.t == " ":
                continue
        cur.append((tk, x, wpx)); x += wpx
    if cur: lines.append(cur)
    return lines

# ---------------------------------------------------------------------------
# Content: a realistic note edit, rendered as it will look, with inline diff.
# ---------------------------------------------------------------------------
B([Run("Weekly upkeep keeps the ", reg(15)),
   Run("Gaggia Classic", reg(15), ACCENT),
   Run(" pulling clean shots. Descale every ", reg(15)),
   Run("two weeks", reg(15), DEL_TEXT, DEL_BG, strike=True),
   Run(" ", reg(15)),
   Run("month", reg(15), ADD_TEXT, ADD_BG),
   Run(" using ", reg(15)),
   Run("Citric Acid", reg(15), ACCENT),
   Run(".", reg(15))], kind="p", gap_before=6)

B([Run("Routine", bold(17))], kind="h2", gap_before=10)

B([Run("Backflush with ", reg(15)),
   Run("a blind basket and ", reg(15), ADD_TEXT, ADD_BG),
   Run("water after every session.", reg(15))], kind="li", indent=20, gap_before=6)

B([Run("Wipe the steam wand ", reg(15)),
   Run("immediately", reg(15), DEL_TEXT, DEL_BG, strike=True),
   Run(" ", reg(15)),
   Run("right after steaming", reg(15), ADD_TEXT, ADD_BG),
   Run(".", reg(15))], kind="li", indent=20)

B([Run("Replace the group gasket once a year.", reg(15), DEL_TEXT, strike=True)],
  kind="li", indent=20, block_diff="del")

B([Run("Run a full descaling cycle ", reg(15), ADD_TEXT),
   Run("monthly", monob(14), ADD_TEXT),
   Run(" and log it in ", reg(15), ADD_TEXT),
   Run("Maintenance Log", reg(15), ACCENT)],
  kind="li", indent=20, block_diff="add")

# ---- measure total height -------------------------------------------------
content_x = PAD
content_w = W - PAD * 2

# header band heights
HEAD_H = 86       # title row + subtitle
LEGEND_H = 40
body_top = HEAD_H + 22

y = body_top
laid = []
for b in blocks:
    y += b["gap_before"]
    lines = layout_block(b, content_x, content_w - b["indent"] - (14 if b["block_diff"] else 0))
    lh = KIND_LH[b["kind"]]
    h = lh * len(lines)
    pad_v = 6 if b["block_diff"] else 0
    laid.append((b, lines, lh, h, pad_v))
    y += h + pad_v * 2 + 2

body_bottom = y + 10
total_h = body_bottom + LEGEND_H + 16

# ---- real canvas ----------------------------------------------------------
img = Image.new("RGB", (W * S, int(total_h) * S), BG_PAGE)
d = ImageDraw.Draw(img)

def rrect(box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle([c * S for c in box], radius=r * S, fill=fill,
                        outline=outline, width=width * S)

# modal panel
rrect((12, 12, W - 12, total_h - 12), 12, fill=BG_ELEV, outline=BORDER, width=1)

# ---- header ---------------------------------------------------------------
hx = PAD
hy = 26
# UPDATE pill
pill_w = tw("UPDATE", bold(12)) + 22
rrect((hx, hy, hx + pill_w, hy + 22), 6, fill=(40, 46, 44), outline=(70, 92, 80), width=1)
d.text(((hx + 10) * S, (hy + 4) * S), "UPDATE", font=bold(12), fill=OK)
d.text(((hx + pill_w + 12) * S, (hy + 2) * S), "Espresso Machine Maintenance",
       font=bold(16), fill=TEXT)
# subtitle
d.text((hx * S, (hy + 32) * S),
       "Assisted proposed  ·  +3 / −1 lines  ·  rendered preview",
       font=reg(13), fill=DIM)
# divider
d.line([(PAD * S, (HEAD_H) * S), ((W - PAD) * S, (HEAD_H) * S)], fill=BORDER, width=1 * S)

# ---- body blocks ----------------------------------------------------------
y = body_top
for b, lines, lh, h, pad_v in laid:
    y += b["gap_before"]
    x_indent = content_x + b["indent"]
    block_top = y
    block_h = h + pad_v * 2

    if b["block_diff"]:
        fill = ADD_BG if b["block_diff"] == "add" else DEL_BG
        bar  = ADD_BAR if b["block_diff"] == "add" else DEL_BAR
        rrect((content_x, block_top, W - PAD, block_top + block_h), 6, fill=fill)
        d.rectangle([content_x * S, block_top * S,
                     (content_x + 3) * S, (block_top + block_h) * S], fill=bar)
        x_indent = content_x + 14 + b["indent"]

    ty = y + pad_v
    # list bullet
    for li_idx, line in enumerate(lines):
        if b["kind"] == "li" and li_idx == 0:
            bullet_col = ADD_TEXT if b["block_diff"] == "add" else (
                         DEL_TEXT if b["block_diff"] == "del" else DIM)
            d.ellipse([(x_indent - 13) * S, (ty + lh/2 - 3) * S,
                       (x_indent - 7) * S, (ty + lh/2 + 3) * S], fill=bullet_col)
        for (tk, lx, wpx) in line:
            f = tk.f or KIND_FONT[b["kind"]]
            px = x_indent + lx
            # inline highlight bg
            if tk.bg is not None:
                d.rectangle([(px - 1) * S, (ty + 2) * S,
                             (px + wpx + 1) * S, (ty + lh - 3) * S], fill=tk.bg)
            d.text((px * S, ty * S), tk.t, font=f, fill=tk.color)
            if tk.strike:
                sy = ty + lh / 2 + 1
                d.line([(px * S, sy * S), ((px + wpx) * S, sy * S)],
                       fill=tk.color, width=max(1, S))
        ty += lh
    y += block_h + 2

# ---- legend ---------------------------------------------------------------
ly = total_h - LEGEND_H - 6
d.line([(PAD * S, ly * S), ((W - PAD) * S, ly * S)], fill=BORDER, width=1 * S)
ly += 12
lx = PAD
def swatch(x, fill, bar, label):
    rrect((x, ly, x + 26, ly + 16), 4, fill=fill)
    d.rectangle([x * S, ly * S, (x + 3) * S, (ly + 16) * S], fill=bar)
    d.text(((x + 34) * S, (ly + 1) * S), label, font=reg(13), fill=DIM)
    return x + 34 + tw(label, reg(13)) + 30
lx = swatch(lx, ADD_BG, ADD_BAR, "added")
lx = swatch(lx, DEL_BG, DEL_BAR, "removed / struck through")
d.text((lx * S, (ly + 1) * S), "wiki-link", font=reg(13), fill=ACCENT)

# ---- downscale + save -----------------------------------------------------
out = img.resize((W, int(total_h)), Image.LANCZOS)
out.save("/home/user/JBrain/docs/diff-markup-mockup.png")
print("wrote docs/diff-markup-mockup.png", out.size)
