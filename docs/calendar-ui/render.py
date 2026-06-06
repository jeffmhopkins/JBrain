"""Mockup renderer: compose JBrain-styled SVG screens -> PNG via cairosvg.
Palette mirrors web/src/styles.css (dark theme). Screens are filled in once the
hybrid Calendar UI plan is fixed; this module holds the reusable primitives."""
import html
import cairosvg

# Design tokens (dark theme) — kept in lockstep with styles.css :root.
BG = "#111315"; ELEV = "#161a1c"; ELEV2 = "#1c2123"; BORDER = "#2a2f31"
TEXT = "#d8dcde"; DIM = "#828a8e"; ACCENT = "#7f9aa6"; ACCENT_SOFT = "rgba(127,154,166,0.16)"
ENTRY = "#8fb89c"; MED = "#cf8d8d"; ASSIST = "#8aacba"; RESEARCH = "#c4ad6e"; DANGER = "#c08585"
FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"
R = 8

def esc(s): return html.escape(str(s), quote=True)

def rect(x, y, w, h, fill=ELEV, stroke=None, rx=R, sw=1, opacity=None):
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
    if stroke: s += f' stroke="{stroke}" stroke-width="{sw}"'
    if opacity is not None: s += f' opacity="{opacity}"'
    return s + "/>"

def text(x, y, s, size=14, fill=TEXT, weight=400, anchor="start", family=FONT):
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">{esc(s)}</text>')

def line(x1, y1, x2, y2, stroke=BORDER, sw=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"{d}/>'

def dot(cx, cy, r, fill):
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"/>'

def chip(x, y, w, label, fill=ACCENT_SOFT, txt=TEXT, h=18, size=11, bar=None):
    out = [rect(x, y, w, h, fill=fill, rx=5)]
    tx = x + 8
    if bar:
        out.append(rect(x, y, 3, h, fill=bar, rx=2)); tx = x + 9
    out.append(text(tx, y + h - 5, label, size=size, fill=txt))
    return "".join(out)

def svg_canvas(w, h, body, bg=BG):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="{bg}"/>{body}</svg>')

def to_png(svg, path, scale=2):
    cairosvg.svg2png(bytestring=svg.encode(), write_to=path,
                     output_width=None, scale=scale)
    return path

if __name__ == "__main__":
    print("render helpers ready")
