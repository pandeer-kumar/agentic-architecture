"""Minimal SVG diagram kit: groups, boxes, cylinders, orthogonal arrows, sequence diagrams.
Diagrams are authored with explicit coordinates so layout is deliberate and stable."""
from html import escape

TONES = {  # fill, stroke, text
    "agent":   ("#EEF2FF", "#4F46E5", "#1E1B4B"),
    "context": ("#F5F3FF", "#7C3AED", "#2E1065"),
    "memory":  ("#FDF4FF", "#C026D3", "#4A044E"),
    "data":    ("#ECFDF5", "#059669", "#064E3B"),
    "tool":    ("#FFFBEB", "#D97706", "#451A03"),
    "sec":     ("#FEF2F2", "#DC2626", "#450A0A"),
    "ext":     ("#F8FAFC", "#64748B", "#0F172A"),
    "user":    ("#F0F9FF", "#0284C7", "#082F49"),
    "plain":   ("#FFFFFF", "#94A3B8", "#0F172A"),
    "ok":      ("#F0FDF4", "#16A34A", "#052E16"),
    "warn":    ("#FFF7ED", "#EA580C", "#431407"),
}
GROUP = {k: (f, s) for k, (f, s, _) in TONES.items()}
FONT = "Inter, 'DejaVu Sans', Arial, sans-serif"


class Diagram:
    def __init__(self, w, h, title, subtitle=None):
        self.w, self.h, self.title, self.subtitle = w, h, title, subtitle
        self.back, self.mid, self.front = [], [], []
        self.boxes = {}

    # ---------- shapes ----------
    def group(self, x, y, w, h, title, tone="ext", dashed=False):
        f, s = GROUP[tone]
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.back.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{f}" fill-opacity="0.55" '
            f'stroke="{s}" stroke-opacity="0.55" stroke-width="1.5"{dash}/>'
            f'<text x="{x+14}" y="{y+22}" font-size="13" font-weight="700" fill="{s}" '
            f'letter-spacing="0.6">{escape(title.upper())}</text>')

    def box(self, bid, x, y, w, h, title, lines=(), tone="plain", shape="rect", bold=True, size=14):
        f, s, t = TONES[tone]
        self.boxes[bid] = (x, y, w, h)
        if shape == "cyl":
            e = 9
            self.mid.append(
                f'<path d="M{x},{y+e} a{w/2},{e} 0 0,0 {w},0 v{h-2*e} a{w/2},{e} 0 0,1 {-w},0 z" fill="{f}" stroke="{s}" stroke-width="1.6"/>'
                f'<ellipse cx="{x+w/2}" cy="{y+e}" rx="{w/2}" ry="{e}" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
            ty0 = y + e + 6
        elif shape == "pill":
            self.mid.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h/2}" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
            ty0 = y
        elif shape == "diamond":
            cx, cy = x + w / 2, y + h / 2
            self.mid.append(f'<polygon points="{cx},{y} {x+w},{cy} {cx},{y+h} {x},{cy}" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
            ty0 = y
        else:
            self.mid.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
            ty0 = y
        n = 1 + len(lines)
        lh_title, lh = size + 4, 15
        total = lh_title + lh * len(lines)
        top = ty0 + ((y + h) - ty0 - total) / 2 + size
        if shape == "cyl":
            top = ty0 + ((y + h - 4) - ty0 - total) / 2 + size
        self.mid.append(f'<text x="{x+w/2}" y="{top}" text-anchor="middle" font-size="{size}" '
                        f'font-weight="{700 if bold else 500}" fill="{t}">{escape(title)}</text>')
        for i, ln in enumerate(lines):
            self.mid.append(f'<text x="{x+w/2}" y="{top + lh_title - 2 + i*lh}" text-anchor="middle" '
                            f'font-size="11.5" fill="{t}" fill-opacity="0.82">{escape(ln)}</text>')

    def text(self, x, y, s, size=12, anchor="start", weight=400, color="#334155", italic=False):
        st = ' font-style="italic"' if italic else ""
        self.front.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
                          f'font-weight="{weight}" fill="{color}"{st}>{escape(s)}</text>')

    def badge(self, x, y, s, tone="agent"):
        f, st, t = TONES[tone]
        w = 9 + 7.2 * len(s)
        self.front.append(f'<rect x="{x}" y="{y-13}" width="{w}" height="19" rx="9.5" fill="{st}"/>'
                          f'<text x="{x+w/2}" y="{y+1}" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">{escape(s)}</text>')

    # ---------- arrows ----------
    def _anchor(self, bid, side, off=0.5):
        x, y, w, h = self.boxes[bid]
        return {"l": (x, y + h * off), "r": (x + w, y + h * off),
                "t": (x + w * off, y), "b": (x + w * off, y + h)}[side]

    def arrow(self, a, b, label=None, fs="r", ts="l", via=(), fo=0.5, to=0.5, dashed=False,
              color="#475569", lpos=0.5, ldx=0, ldy=-6, both=False, width=1.6, lseg=None):
        p0, p1 = self._anchor(a, fs, fo), self._anchor(b, ts, to)
        pts = [p0, *via, p1]
        if not via:  # auto elbow
            (x0, y0), (x1, y1) = p0, p1
            if fs in "lr" and ts in "lr" and abs(y0 - y1) > 1:
                mx = (x0 + x1) / 2
                pts = [p0, (mx, y0), (mx, y1), p1]
            elif fs in "tb" and ts in "tb" and abs(x0 - x1) > 1:
                my = (y0 + y1) / 2
                pts = [p0, (x0, my), (x1, my), p1]
            elif fs in "lr" and ts in "tb":
                pts = [p0, (x1, y0), p1]
            elif fs in "tb" and ts in "lr":
                pts = [p0, (x0, y1), p1]
        self.line(pts, label, dashed, color, lpos, ldx, ldy, both, width, lseg)

    def line(self, pts, label=None, dashed=False, color="#475569", lpos=0.5, ldx=0, ldy=-6, both=False, width=1.6, lseg=None):
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        mid = f' marker-start="url(#ah-{color[1:]})"' if both else ""
        self.front.insert(0, f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
                             f'marker-end="url(#ah-{color[1:]})"{mid}/>')
        self._colors.add(color)
        if label:
            # label on the longest segment
            segs = list(zip(pts, pts[1:]))
            (xa, ya), (xb, yb) = segs[lseg] if lseg is not None else max(segs, key=lambda s: abs(s[0][0]-s[1][0]) + abs(s[0][1]-s[1][1]))
            lx, ly = xa + (xb - xa) * lpos + ldx, ya + (yb - ya) * lpos + ldy
            w = 6.3 * len(label) + 10
            self.front.append(f'<rect x="{lx-w/2}" y="{ly-12}" width="{w}" height="17" rx="4" fill="#FFFFFF" fill-opacity="0.92"/>'
                              f'<text x="{lx}" y="{ly+1}" text-anchor="middle" font-size="11" fill="{color}" font-weight="600">{escape(label)}</text>')

    _colors = set()

    def svg(self):
        markers = "".join(
            f'<marker id="ah-{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for c in sorted(self._colors | {"#475569"}))
        head = (f'<text x="28" y="40" font-size="22" font-weight="800" fill="#0F172A">{escape(self.title)}</text>'
                + (f'<text x="28" y="62" font-size="13" fill="#475569">{escape(self.subtitle)}</text>' if self.subtitle else ""))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'font-family="{FONT}"><defs>{markers}</defs>'
                f'<rect width="100%" height="100%" fill="#FFFFFF"/>{head}'
                + "".join(self.back) + "".join(self.mid) + "".join(self.front) + "</svg>")


class Sequence:
    """Sequence diagram: participants across the top, messages downwards."""
    def __init__(self, title, participants, subtitle=None, col=170, top=120, step=40, width=None):
        self.title, self.subtitle, self.p = title, subtitle, participants
        self.col, self.top, self.step = col, top, step
        self.items = []
        self.width = width or (60 + col * len(participants))

    def x(self, name):
        return 60 + self.col * [p[0] for p in self.p].index(name) + self.col / 2 - 30

    def msg(self, a, b, label, ret=False, tone="#334155"):
        self.items.append(("msg", a, b, label, ret, tone))

    def note(self, a, b, label, tone="context"):
        self.items.append(("note", a, b, label, tone))

    def phase(self, label):
        self.items.append(("phase", label))

    def build(self):
        h = self.top + 70 + self.step * len(self.items) + 40
        d = Diagram(self.width, h, self.title, self.subtitle)
        for name, label, tone in self.p:
            cx = self.x(name)
            d.box(name, cx - 70, self.top - 46, 140, 44, label, tone=tone, size=13)
            d.back.append(f'<line x1="{cx}" y1="{self.top}" x2="{cx}" y2="{h-30}" stroke="#CBD5E1" stroke-width="1.5" stroke-dasharray="4 4"/>')
        y = self.top + 30
        for it in self.items:
            if it[0] == "msg":
                _, a, b, label, ret, tone = it
                xa, xb = self.x(a), self.x(b)
                if a == b:
                    d.line([(xa, y - 8), (xa + 40, y - 8), (xa + 40, y + 8), (xa + 4, y + 8)], None, ret, tone)
                    d.text(xa + 48, y + 4, label, 11.5, color=tone, weight=600)
                else:
                    d.line([(xa, y), (xb - (6 if xb > xa else -6), y)], None, ret, tone)
                    d.text((xa + xb) / 2, y - 7, label, 11.5, "middle", 600, tone)
            elif it[0] == "note":
                _, a, b, label, tone = it
                xa, xb = sorted([self.x(a), self.x(b)])
                f, s, t = TONES[tone]
                d.front.append(f'<rect x="{xa-60}" y="{y-16}" width="{xb-xa+120}" height="26" rx="6" fill="{f}" stroke="{s}"/>')
                d.text((xa + xb) / 2, y + 2, label, 11.5, "middle", 600, t)
            else:
                d.front.append(f'<rect x="20" y="{y-15}" width="{self.width-40}" height="24" rx="5" fill="#F1F5F9"/>')
                d.text(32, y + 2, it[1], 12, weight=800, color="#334155")
            y += self.step
        return d
