"""Render the proposed hybrid Calendar UI as a single PNG spec sheet.
Six phone screens styled with the JBrain dark tokens (mirrors styles.css)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render import (BG, ELEV, ELEV2, BORDER, TEXT, DIM, ACCENT, ACCENT_SOFT, DANGER,
                    R, FONT, esc, rect, text, line, dot, svg_canvas, to_png)

PW, PH = 380, 768          # phone frame
PAD = 14                   # content inset
parts = []

def push(*xs): parts.extend(xs)

def phone(ox, oy, label):
    """Device bezel + Shell top bar + the shared tool-bar (seg + nav). Returns body y."""
    push(rect(ox, oy, PW, PH, fill=BG, stroke=BORDER, rx=22, sw=1.5))
    cx, cw = ox + PAD, PW - 2 * PAD
    # Shell top bar: ‹  Calendar            ● ⚡
    push(text(cx, oy + 30, "‹", size=22, fill=DIM))
    push(text(cx + 26, oy + 30, "Calendar", size=17, fill=TEXT, weight=600))
    push(dot(ox + PW - 44, oy + 24, 4, ACCENT))
    push(text(ox + PW - 30, oy + 30, "⚡", size=16, fill=DIM))
    push(line(ox, oy + 46, ox + PW, oy + 46, stroke=BORDER))
    # label under the phone
    push(text(ox + PW / 2, oy + PH + 24, label, size=15, fill=TEXT, weight=600, anchor="middle"))
    return oy + 46

def toolbar(ox, by, active, navlabel):
    """Segmented Day/Week/Month/List + ‹ navlabel ›  Today. Returns content-body y."""
    cx, cw = ox + PAD, PW - 2 * PAD
    seg_y, seg_h = by + 12, 32
    push(rect(cx, seg_y, cw, seg_h, fill=ELEV, stroke=BORDER, rx=8))
    segs = ["Day", "Week", "Month", "List"]
    sw = cw / 4
    for i, s in enumerate(segs):
        sx = cx + i * sw
        if s == active:
            push(rect(sx + 2, seg_y + 2, sw - 4, seg_h - 4, fill=ACCENT_SOFT, rx=6))
        push(text(sx + sw / 2, seg_y + 21, s, size=13,
                  fill=(ACCENT if s == active else DIM),
                  weight=(600 if s == active else 400), anchor="middle"))
    nav_y = seg_y + seg_h + 22
    push(text(cx + 2, nav_y, "‹", size=18, fill=DIM))
    push(text(cx + 24, nav_y, navlabel, size=15, fill=TEXT, weight=600))
    push(text(cx + 24 + 9.0 * len(navlabel), nav_y, "›", size=18, fill=DIM))
    push(rect(ox + PW - PAD - 60, nav_y - 16, 60, 24, fill=ELEV, stroke=BORDER, rx=7))
    push(text(ox + PW - PAD - 30, nav_y, "Today", size=12, fill=DIM, anchor="middle"))
    return nav_y + 18

def ev_row(ox, y, when, title, tag=None, recurring=False, deadline=False, cancelled=False, chevron=True):
    cx, cw = ox + PAD, PW - 2 * PAD
    tfill = DIM if cancelled else TEXT
    push(text(cx + 6, y, when, size=12, fill=DIM, family=FONT))
    push(text(cx + 62, y, title, size=14, fill=tfill, weight=(600 if deadline else 400)))
    if cancelled:
        push(line(cx + 62, y - 4, cx + 62 + 7.2 * len(title), y - 4, stroke=DIM))
    rx = ox + PW - PAD - 12
    if chevron:
        push(text(rx + 4, y, "›", size=15, fill=DIM, anchor="middle"))
        rx -= 14
    if recurring:
        push(text(rx, y, "↻", size=13, fill=DIM, anchor="end")); rx -= 18
    if tag:
        push(text(rx, y, tag, size=11, fill=(DANGER if cancelled else DIM), anchor="end"))

def group_hdr(ox, y, s):
    push(text(ox + PAD, y, s, size=11, fill=DIM, weight=600))

# ---------- Screen builders ----------

def screen_list(ox, oy):
    by = toolbar(ox, phone(ox, oy, "List  ·  default view"), "List", "Upcoming")
    cx, cw = ox + PAD, PW - 2 * PAD
    # sub-toggle Upcoming / Past
    push(rect(cx, by, 150, 26, fill=ELEV, stroke=BORDER, rx=7))
    push(rect(cx + 2, by + 2, 73, 22, fill=ACCENT_SOFT, rx=6))
    push(text(cx + 38, by + 17, "Upcoming", size=11, fill=ACCENT, anchor="middle"))
    push(text(cx + 112, by + 17, "Past", size=11, fill=DIM, anchor="middle"))
    y = by + 52
    def card(rows_h):
        push(rect(cx, y - 14, cw, rows_h, fill=ELEV, stroke=BORDER, rx=8))
    group_hdr(ox, y - 24, "TODAY · SAT JUN 6"); card(70)
    ev_row(ox, y + 6, "09:00", "Dentist — Dr. Lee", tag="appt")
    push(line(cx + 10, y + 20, cx + cw - 10, y + 20, stroke=BORDER))
    ev_row(ox, y + 42, "—", "Pay rent", tag="deadline", deadline=True)
    y += 86
    group_hdr(ox, y - 24, "TOMORROW · SUN JUN 7"); card(40)
    ev_row(ox, y + 6, "—", "Anniversary", recurring=True)
    y += 56
    group_hdr(ox, y - 24, "MON · JUN 8"); card(70)
    ev_row(ox, y + 6, "08:30", "Standup", tag="appt", recurring=True)
    push(line(cx + 10, y + 20, cx + cw - 10, y + 20, stroke=BORDER))
    ev_row(ox, y + 42, "18:00", "Flight to NYC", tag="appt")
    y += 86
    group_hdr(ox, y - 24, "THU · JUN 11"); card(40)
    ev_row(ox, y + 6, "—", "Project review", tag="cancelled", cancelled=True)

def screen_month(ox, oy):
    by = toolbar(ox, phone(ox, oy, "Month  ·  tap a day → agenda strip"), "Month", "June 2026")
    cx, cw = ox + PAD, PW - 2 * PAD
    cols = "SMTWTFS"
    colw = cw / 7
    for i, c in enumerate(cols):
        push(text(cx + colw * i + colw / 2, by + 6, c, size=10, fill=DIM, anchor="middle"))
    gy = by + 16
    ch = 34
    # 6x7 grid. June 2026: Jun 1 = Monday → 1 leading (May 31), 30 days, 11 trailing.
    nums = [31] + list(range(1, 31)) + list(range(1, 12))
    dim_idx = set([0] + list(range(31, 42)))   # leading/trailing
    today_n, sel_n = 9, 3
    dots = {2: 1, 4: 2, 9: 3, 11: 3, 13: 1, 15: 2, 19: 1, 24: 1}  # day->count (in-month)
    for r in range(6):
        for c in range(7):
            idx = r * 7 + c
            n = nums[idx]
            x, yc = cx + c * colw, gy + r * ch
            inmonth = idx not in dim_idx
            if inmonth and n == sel_n:
                push(rect(x + 3, yc + 2, colw - 6, ch - 4, fill=ACCENT_SOFT, stroke=ACCENT, rx=6, sw=1))
            if inmonth and n == today_n:
                push(rect(x + colw/2 - 11, yc + 4, 22, 16, fill="none", stroke=ACCENT, rx=8, sw=1.3))
            push(text(x + colw / 2, yc + 16, str(n), size=11,
                      fill=(DIM if not inmonth else (ACCENT if n == today_n else TEXT)),
                      anchor="middle"))
            if inmonth and n in dots:
                k = min(dots[n], 3)
                total = k * 6 - 2
                for d in range(k):
                    push(dot(x + colw/2 - total/2 + d * 6, yc + 26, 1.7, DIM))
                if dots[n] > 3:
                    push(text(x + colw/2 + total/2 + 6, yc + 29, f"+{dots[n]-3}", size=8, fill=DIM, anchor="middle"))
    # agenda strip for selected day
    ay = gy + 6 * ch + 12
    group_hdr(ox, ay, "TUE · JUN 3        Open day →")
    push(rect(cx, ay + 8, cw, 64, fill=ELEV, stroke=BORDER, rx=8))
    ev_row(ox, ay + 28, "09:00", "Dentist", tag="appt", chevron=False)
    push(line(cx + 10, ay + 42, cx + cw - 10, ay + 42, stroke=BORDER))
    ev_row(ox, ay + 62, "—", "Pay rent", tag="deadline", deadline=True, chevron=False)

def screen_day(ox, oy):
    by = toolbar(ox, phone(ox, oy, "Day  ·  time-axis grid"), "Day", "Thu Jun 12")
    cx, cw = ox + PAD, PW - 2 * PAD
    # all-day lane
    push(text(cx, by + 8, "ALL DAY", size=9, fill=DIM, weight=600))
    push(rect(cx + 56, by, cw - 56, 22, fill=ACCENT_SOFT, rx=6))
    push(text(cx + 64, by + 15, "◇  Mortgage due", size=12, fill=TEXT))
    # hour grid
    gy = by + 34
    rowh = 40
    hours = ["8 AM", "9", "10", "11", "12 PM", "1", "2"]
    gx = cx + 40
    for i, h in enumerate(hours):
        yy = gy + i * rowh
        push(text(cx + 34, yy + 4, h, size=10, fill=DIM, anchor="end"))
        push(line(gx, yy, cx + cw, yy, stroke=BORDER))
    def block(hr, dur, label, sub, x0, w, fill=ACCENT_SOFT, rec=False):
        yy = gy + hr * rowh
        push(rect(x0, yy + 2, w, dur * rowh - 4, fill=fill, stroke=ACCENT, rx=6, sw=1))
        push(text(x0 + 8, yy + 17, label, size=11.5, fill=TEXT, weight=600))
        if sub:
            push(text(x0 + 8, yy + 31, sub, size=9.5, fill=DIM))
        if rec:
            push(text(x0 + w - 8, yy + 17, "↻", size=11, fill=DIM, anchor="end"))
    fullw = cx + cw - gx - 4
    block(1, 1, "Standup", "9:00–9:15", gx + 2, fullw)               # 9:00
    # overlap 10:00 — two columns
    half = (fullw - 6) / 2
    block(2, 1, "1:1 w/ Sam", "10:00", gx + 2, half)
    block(2, 1, "Client call", "10:30", gx + 6 + half, half)
    block(6, 1, "Dentist — Dr Lee", "appointment", gx + 2, fullw, rec=True)  # 2 PM
    # now line ~ 11:25 (row 3 + .4)
    ny = gy + 3 * rowh + 0.4 * rowh
    push(dot(gx, ny, 3, DANGER))
    push(line(gx, ny, cx + cw, ny, stroke=DANGER, sw=1.5))

def screen_week(ox, oy):
    by = toolbar(ox, phone(ox, oy, "Week  ·  stacked day-sections"), "Week", "Jun 1 – 7")
    cx, cw = ox + PAD, PW - 2 * PAD
    y = by + 6
    def dayhdr(lbl, today=False):
        nonlocal y
        push(text(cx, y, lbl, size=11, fill=(ACCENT if today else DIM), weight=600))
        push(line(cx + 9 * len(lbl) + 8, y - 4, cx + cw, y - 4, stroke=(ACCENT if today else BORDER)))
        y += 18
    def row(when, title, tag=None, rec=False, dim=False):
        nonlocal y
        ev_row(ox, y, when, title, tag=tag, recurring=rec, chevron=False)
        y += 22
    dayhdr("SUN  Jun 1")
    push(text(cx + 6, y, "nothing", size=12, fill=DIM)); push(text(cx + cw - 6, y, "+", size=15, fill=DIM, anchor="end")); y += 24
    dayhdr("MON  Jun 2")
    row("08:30", "Standup", tag="appt"); row("—", "Q2 report due", tag="deadline")
    y += 4
    dayhdr("TUE  Jun 3")
    row("09:00", "Dentist", tag="appt")
    y += 4
    dayhdr("WED  Jun 4")
    row("—", "Bins out", rec=True); row("—", "Lunch w/ Sam")
    push(text(cx + 6, y, "+3 more", size=12, fill=ACCENT)); y += 26
    dayhdr("THU  Jun 5   · today", today=True)
    row("12:00", "Flight to NYC", tag="appt")

def sheet(ox, oy, builder):
    """Dim the phone body and draw a bottom sheet drawn by builder(sx, sy, sw, sh)."""
    push(rect(ox + 1, oy + 47, PW - 2, PH - 48, fill="#000000", rx=20, opacity=0.45))
    sh = 326
    sy = oy + PH - sh
    sx, sw = ox + 6, PW - 12
    push(rect(sx, sy, sw, sh, fill=ELEV, stroke=BORDER, rx=18))
    push(rect(ox + PW/2 - 18, sy + 10, 36, 4, fill=BORDER, rx=2))
    builder(sx, sy, sw, sh)

def screen_detail(ox, oy):
    toolbar(ox, phone(ox, oy, "Event detail  ·  bottom sheet"), "Month", "June 2026")
    def b(sx, sy, sw, sh):
        ix = sx + 18
        push(text(ix, sy + 44, "Dentist — Dr. Lee", size=18, fill=TEXT, weight=600))
        push(text(ix, sy + 72, "Thu, Jun 12 · 2:00 PM", size=13, fill=DIM))
        push(text(ix, sy + 96, "appointment", size=12, fill=DIM))
        push(text(ix, sy + 118, "◇  Office, 3rd floor", size=12, fill=DIM))
        # action buttons
        ay = sy + 150
        push(rect(ix, ay, sw - 36, 40, fill=ACCENT_SOFT, stroke=ACCENT, rx=8))
        push(text(sx + sw/2, ay + 25, "Open note", size=14, fill=ACCENT, anchor="middle"))
        bw = (sw - 36 - 12) / 2
        push(rect(ix, ay + 50, bw, 40, fill=ELEV2, stroke=BORDER, rx=8))
        push(text(ix + bw/2, ay + 75, "Reschedule", size=13, fill=TEXT, anchor="middle"))
        push(rect(ix + bw + 12, ay + 50, bw, 40, fill=ELEV2, stroke=BORDER, rx=8))
        push(text(ix + bw + 12 + bw/2, ay + 75, "Cancel", size=13, fill=DANGER, anchor="middle"))
        push(text(ix, ay + 116, "Edits write a note — the calendar re-derives.", size=10.5, fill=DIM))
    sheet(ox, oy, b)

def screen_quickadd(ox, oy):
    toolbar(ox, phone(ox, oy, "Quick-add  ·  writes a dated note"), "Day", "Thu Jun 12")
    def b(sx, sy, sw, sh):
        ix = sx + 18; fw = sw - 36
        push(text(ix, sy + 40, "New event", size=17, fill=TEXT, weight=600))
        def field(yy, label, val, dim=True, w=fw):
            push(text(ix, yy, label, size=10, fill=DIM, weight=600))
            push(rect(ix, yy + 6, w, 32, fill=ELEV2, stroke=BORDER, rx=7))
            push(text(ix + 10, yy + 27, val, size=13, fill=(DIM if dim else TEXT)))
        field(sy + 64, "TITLE", "Dentist — Dr. Lee", dim=False)
        half = (fw - 12) / 2
        field(sy + 112, "DATE", "2026-06-12", w=half)
        push(text(ix + half + 12, sy + 112, "TIME", size=10, fill=DIM, weight=600))
        push(rect(ix + half + 12, sy + 118, half, 32, fill=ELEV2, stroke=BORDER, rx=7))
        push(text(ix + half + 22, sy + 139, "14:00", size=13, fill=TEXT))
        push(text(ix, sy + 160, "KIND", size=10, fill=DIM, weight=600))
        push(rect(ix, sy + 166, fw, 32, fill=ELEV2, stroke=BORDER, rx=7))
        push(text(ix + 10, sy + 187, "appointment", size=13, fill=TEXT))
        push(text(ix + fw - 14, sy + 187, "⌄", size=14, fill=DIM, anchor="middle"))
        push(rect(ix, sy + 212, fw, 40, fill=ACCENT, rx=8))
        push(text(sx + sw/2, sy + 237, "Add", size=14, fill="#10242b", weight=600, anchor="middle"))
        push(text(ix, sy + 278, "Writes notes/2026/06/12 — your notes stay the source of truth.", size=10, fill=DIM))
    sheet(ox, oy, b)

# ---------- compose canvas ----------
MARGIN, GX, GY, TITLE_H, LBL = 44, 36, 70, 78, 30
cols, rows = 3, 2
CW = MARGIN * 2 + cols * PW + (cols - 1) * GX
CH = TITLE_H + MARGIN + rows * PH + (rows - 1) * (GY + LBL) + LBL + MARGIN

push(text(MARGIN, 44, "JBrain · Calendar — proposed hybrid UI", size=26, fill=TEXT, weight=700))
push(text(MARGIN, 64, "List default · Month with day-agenda · Day time-grid · Week stacked-sections · sheets for detail & quick-add. "
                      "Monochrome kinds (glyph + label), accent reserved for today/selection.", size=12.5, fill=DIM))

def cell(i, j):
    return (MARGIN + i * (PW + GX), TITLE_H + MARGIN + j * (PH + GY + LBL))

builders = [screen_list, screen_month, screen_day, screen_week, screen_detail, screen_quickadd]
for k, fn in enumerate(builders):
    i, j = k % cols, k // cols
    ox, oy = cell(i, j)
    fn(ox, oy)

svg = svg_canvas(CW, CH, "".join(parts))
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar-mockup.png")
to_png(svg, out, scale=3)
print("wrote", out, f"({CW}x{CH} @2x)")
