"""Regenerates app/web/logo.svg and favicon.svg (run in scripts/, then copy). Generate meshbot logo + icon SVGs in the fwamesh.net style (charcoal plate, cream + orange blocky type,
orange mesh nodes joined by a dashed link)."""
CH, CR, OR, BL, GR = "#282727", "#f6ecd9", "#e87d30", "#6cb4db", "#6ab458"

def letter(c, x, y, w=44, h=52, s=12, fill=CR):
    R = lambda a, b, ww, hh: f'<rect x="{a}" y="{b}" width="{ww}" height="{hh}"/>'
    m = h / 2
    if c == "M":
        body = (R(x, y, s, h) + R(x + w - s, y, s, h) +
                f'<polygon points="{x},{y} {x+s+3},{y} {x+w/2},{y+21} {x+w-s-3},{y} {x+w},{y} {x+w/2},{y+36}"/>')
    elif c == "E":
        body = R(x, y, s, h) + R(x, y, w, s) + R(x, y + m - s / 2, w - 8, s) + R(x, y + h - s, w, s)
    elif c == "S":
        body = (f'<path d="M{x+8},{y} H{x+w} V{y+s} H{x+s} V{y+m-s/2} H{x+w-8} L{x+w},{y+m-s/2+8} V{y+h-8} '
                f'L{x+w-8},{y+h} H{x} V{y+h-s} H{x+w-s} V{y+m+s/2} H{x+8} L{x},{y+m+s/2-8} V{y+8} Z"/>')
    elif c == "H":
        body = R(x, y, s, h) + R(x + w - s, y, s, h) + R(x, y + m - s / 2, w, s)
    elif c == "B":
        body = (f'<path fill-rule="evenodd" d="M{x},{y} H{x+w-9} L{x+w-3},{y+6} V{y+m-5} L{x+w},{y+m-2} V{y+h-7} '
                f'L{x+w-7},{y+h} H{x} Z M{x+s},{y+s} V{y+m-s/2} H{x+w-s-3} V{y+s} Z '
                f'M{x+s},{y+m+s/2} V{y+h-s} H{x+w-s} V{y+m+s/2} Z"/>')
    elif c == "O":
        body = (f'<path fill-rule="evenodd" d="M{x+9},{y} H{x+w-9} L{x+w},{y+9} V{y+h-9} L{x+w-9},{y+h} H{x+9} '
                f'L{x},{y+h-9} V{y+9} Z M{x+s},{y+s} V{y+h-s} H{x+w-s} V{y+s} Z"/>')
    elif c == "T":
        body = R(x, y, w, s) + R(x + w / 2 - s / 2, y, s, h)
    return f'<g fill="{fill}">{body}</g>'

def node(cx, cy, r=8):
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{OR}"/><circle cx="{cx}" cy="{cy}" r="{r*0.45:.1f}" fill="{CR}"/>'

def windows(x0, y0, cols, rows, w=5, h=7, gx=4, gy=5):
    return "".join(f'<rect x="{x0+i*(w+gx)}" y="{y0+j*(h+gy)}" width="{w}" height="{h}" fill="{CR}"/>'
                   for i in range(cols) for j in range(rows))

def robot(cx, top, scale=1.0, eyes=True):
    """Cream robot head with node eyes and an antenna topped by a mesh node; top = antenna tip y."""
    k = scale
    hw, hh = 36 * k, 46 * k
    hy = top + 26 * k
    out = (f'<rect x="{cx-2.5*k}" y="{top+6*k}" width="{5*k}" height="{22*k}" fill="{CR}"/>'
           f'<rect x="{cx-hw}" y="{hy}" width="{2*hw}" height="{hh}" rx="{9*k}" fill="{CR}"/>'
           f'<rect x="{cx-hw-6*k}" y="{hy+14*k}" width="{6*k}" height="{16*k}" rx="{2*k}" fill="{CR}"/>'
           f'<rect x="{cx+hw}" y="{hy+14*k}" width="{6*k}" height="{16*k}" rx="{2*k}" fill="{CR}"/>')
    if eyes:
        out += (f'<circle cx="{cx-15*k}" cy="{hy+19*k}" r="{9*k}" fill="{CH}"/>{node(cx-15*k, hy+19*k, 6.5*k)}'
                f'<circle cx="{cx+15*k}" cy="{hy+19*k}" r="{9*k}" fill="{CH}"/>{node(cx+15*k, hy+19*k, 6.5*k)}'
                f'<rect x="{cx-12*k}" y="{hy+34*k}" width="{24*k}" height="{5*k}" rx="{2*k}" fill="{CH}"/>')
    out += node(cx, top + 4 * k, 8 * k)
    return out

# ---------- full logo ----------
W, H = 480, 236
cx = W / 2
sky = (  # charcoal skyline blocks, bottoms run into the plate
    f'<rect x="38" y="104" width="62" height="60" fill="{CH}"/>' + windows(48, 114, 5, 2) +
    f'<rect x="100" y="124" width="44" height="40" fill="{CH}"/>' +
    f'<rect x="144" y="114" width="30" height="50" fill="{CH}"/>' +
    f'<rect x="{cx-58}" y="70" width="116" height="94" fill="{CH}"/>' +
    "".join(f'<rect x="{cx-44+i*16}" y="110" width="6" height="32" fill="{CR}"/>' for i in range(6)) +
    f'<rect x="306" y="120" width="30" height="44" fill="{CH}"/>' +
    f'<rect x="336" y="98" width="70" height="66" fill="{CH}"/>' + windows(346, 108, 6, 2, w=5, gx=5) +
    f'<rect x="406" y="126" width="38" height="38" fill="{CH}"/>'
    # flagpole-ish mast on the left tower, like the fwamesh dome/flag details
)
link = (f'<path d="M69 92 Q 150 26 {cx} 22 Q 330 26 371 86" fill="none" stroke="{OR}" stroke-width="4.5" '
        f'stroke-dasharray="11 8" stroke-linecap="round"/>')
plate = f'<path d="M14 150 H466 L474 158 V222 L466 230 H14 L6 222 V158 Z" fill="{CH}"/>'
word, lw, gap = "MESHBOT", 44, 9
x0 = cx - (len(word) * lw + (len(word) - 1) * gap) / 2
letters = "".join(letter(c, x0 + i * (lw + gap), 160, fill=OR if i < 4 else CR) for i, c in enumerate(word))
rule = f'<rect x="26" y="219" width="428" height="3" fill="{CR}" opacity=".9"/>'
dots = "".join(f'<circle cx="{cx+dx}" cy="220.5" r="5" fill="{c}" stroke="{CH}" stroke-width="3"/>'
               for dx, c in ((-16, OR), (0, BL), (16, GR)))
logo = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="meshbot">'
        f'<title>meshbot</title>{sky}{plate}{link}{node(69, 92)}{node(371, 86)}{robot(cx, 14, 1.15)}'
        f'{letters}{rule}{dots}</svg>')
open("meshbot-logo.svg", "w").write(logo)

# ---------- icon / favicon: robot head on a charcoal tile ----------
def icon(size_hint):
    eyes = size_hint > 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" rx="12" fill="{CH}"/>'
            + (f'<path d="M8 27 Q 32 3 56 27" fill="none" stroke="{OR}" stroke-width="2.6" '
               f'stroke-dasharray="4.5 3.5" stroke-linecap="round"/>' + node(8, 27, 4.4) + node(56, 27, 4.4)
               if eyes else "")
            + (robot(32, 11, 0.54) if eyes else robot(32, 3, 0.78)) + '</svg>')
open("favicon.svg", "w").write(icon(64))
open("favicon-small.svg", "w").write(icon(16))
