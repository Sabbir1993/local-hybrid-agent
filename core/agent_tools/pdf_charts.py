"""Charts for generated PDFs: inline SVG drawn from numbers we parsed ourselves (no script, no remote assets).

Same chart kinds and the same look as the chat `visual` blocks (static/js/markdown.js): bar, hbar, line, pie. Every label
is escaped and every coordinate is computed here, so model text can only ever appear as plain text inside the drawing.
"""
import html
import math

PALETTE = ["#2563EB", "#7C3AED", "#F59E0B", "#10B981", "#EC4899", "#06B6D4", "#84CC16", "#EF4444"]
_AX = "#64748B"
_GRID = "#E2E8F0"
_INK = "#0F172A"


def _fmt(n: float) -> str:
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if a >= 1e4:
        return f"{n / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n:.2f}".rstrip("0").rstrip(".") if a < 10 else f"{n:.1f}".rstrip("0").rstrip(".")


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(v))
    f = v / p
    return (1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10) * p


def _t(text: str, n: int) -> str:
    text = str(text)
    return html.escape(text if len(text) <= n else text[:n - 1] + "…")


def _text(x, y, s, anchor="middle", size=10.5, fill=_AX, weight="400"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" font-family="Segoe UI, Arial, sans-serif">{s}</text>')


def chart_svg(data: dict) -> str:
    """data = {kind: bar|hbar|line|pie, unit, labels, series:[{name, values}]} -> an <svg> string."""
    kind, labels, series, unit = data["kind"], data["labels"], data["series"], data.get("unit", "")
    u = (" " + html.escape(unit)) if unit else ""
    legend = ""
    ly = 14
    if len(series) > 1 and kind != "pie":                      # a pie lists its slices beside the chart instead
        names = [s["name"] or f"Series {i + 1}" for i, s in enumerate(series)]
        x = 40
        parts = []
        for i, n in enumerate(names[:8]):
            parts.append(f'<rect x="{x}" y="4" width="10" height="10" rx="2" fill="{PALETTE[i % len(PALETTE)]}"/>'
                         + _text(x + 15, 13, _t(n, 18), "start", 10.5, _INK))
            x += 28 + 6.2 * min(len(str(n)), 18)
        legend = "".join(parts)
        ly = 30
    vals = [v for s in series for v in s["values"]]
    body, h = "", 0
    if kind == "pie":
        vs = series[0]["values"]
        total = sum(vs) or 1.0
        cx, cy, r = 150, ly + 105, 95
        a0 = -math.pi / 2
        for i, v in enumerate(vs):
            a1 = a0 + 2 * math.pi * v / total
            large = 1 if (a1 - a0) > math.pi else 0
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            if len(vs) == 1:
                body += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{PALETTE[0]}"/>'
            else:
                body += (f'<path d="M{cx},{cy} L{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" '
                         f'fill="{PALETTE[i % len(PALETTE)]}" stroke="#fff" stroke-width="2"/>')
            am = (a0 + a1) / 2
            if v / total >= 0.05:
                body += _text(cx + (r * 0.62) * math.cos(am), cy + (r * 0.62) * math.sin(am) + 4,
                              f"{v / total * 100:.0f}%", "middle", 11, "#fff", "700")
            a0 = a1
        for i, (lab, v) in enumerate(zip(labels, vs)):
            y = ly + 30 + i * 22
            body += (f'<rect x="290" y="{y - 10}" width="11" height="11" rx="2" fill="{PALETTE[i % len(PALETTE)]}"/>'
                     + _text(308, y, f"{_t(lab, 26)} — {_fmt(v)}{u}", "start", 11.5, _INK))
        h = max(cy + r + 14, ly + 30 + len(vs) * 22 + 10)
        return _wrap(legend + body, 600, h)
    lo, hi = min(0.0, min(vals)), _nice_max(max(0.0, max(vals)))
    span = (hi - lo) or 1.0
    if kind == "hbar":
        s0 = series[0]
        row, lab_w = 30, 150
        x0, max_w = lab_w + 10, 590 - lab_w - 10 - 70
        h = len(labels) * row + ly + 8
        zero = x0 + (0 - lo) / span * max_w
        for i, (lab, v) in enumerate(zip(labels, s0["values"])):
            y = ly + 4 + i * row
            w = abs(v) / span * max_w
            x = zero if v >= 0 else zero - w
            body += _text(lab_w, y + 16, _t(lab, 24), "end", 11, _AX)
            body += f'<rect x="{x:.1f}" y="{y + 3}" width="{max(w, 1):.1f}" height="{row - 10}" rx="3" fill="{PALETTE[0]}"/>'
            body += _text((x + w + 6) if v >= 0 else (x - 6), y + 16, f"{_fmt(v)}{u}", "start" if v >= 0 else "end", 11, _INK, "600")
        return _wrap(legend + body, 600, h)
    left, right, top, bottom = 46, 590, ly + 6, ly + 180
    h = bottom + 28
    def y_of(v): return bottom - (v - lo) / span * (bottom - top)
    for k in range(5):
        v = lo + span * k / 4
        yy = y_of(v)
        body += f'<line x1="{left}" y1="{yy:.1f}" x2="{right}" y2="{yy:.1f}" stroke="{_GRID}" stroke-width="1"/>'
        body += _text(left - 6, yy + 3.5, _fmt(v), "end")
    n = len(labels)
    slot = (right - left) / n
    every = math.ceil(n / 8)
    x_bar = lambda i: left + slot * i + slot / 2   # noqa: E731
    pad = 22                                    # keeps the first / last point (and its label) off the axis text
    x_line = lambda i: (left + right) / 2 if n == 1 else left + pad + (right - left - 2 * pad) * i / (n - 1)   # noqa: E731
    x_at = x_bar if kind == "bar" else x_line
    for i, lab in enumerate(labels):
        if i % every == 0:
            body += _text(x_at(i), bottom + 16, _t(lab, 14))
    show = n <= 8 and len(series) <= 2
    if kind == "bar":
        k = len(series)
        bw = min(46, slot * 0.7 / k)
        y0 = y_of(0)
        for si, s in enumerate(series):
            for i, v in enumerate(s["values"]):
                x = x_bar(i) - bw * k / 2 + bw * si
                y = y_of(v)
                body += (f'<rect x="{x:.1f}" y="{min(y, y0):.1f}" width="{bw - 2:.1f}" height="{max(abs(y0 - y), 1):.1f}" '
                         f'rx="2" fill="{PALETTE[si % len(PALETTE)]}"/>')
                if show:
                    body += _text(x + (bw - 2) / 2, (y - 4) if v >= 0 else (y + 12), _fmt(v), "middle", 10.5, _INK, "600")
    else:
        for si, s in enumerate(series):
            col = PALETTE[si % len(PALETTE)]
            pts = " ".join(f"{x_line(i):.1f},{y_of(v):.1f}" for i, v in enumerate(s["values"]))
            body += f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round"/>'
            for i, v in enumerate(s["values"]):
                body += f'<circle cx="{x_line(i):.1f}" cy="{y_of(v):.1f}" r="3.5" fill="{col}"/>'
                if show:
                    body += _text(x_line(i), y_of(v) - 8, _fmt(v), "middle", 10.5, _INK, "600")
    if unit:
        body += _text(left, top - 4, html.escape(unit), "start", 10.5)
    return _wrap(legend + body, 600, h)


def _wrap(inner: str, w: int, h: float) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h:.0f}" width="100%" role="img">{inner}</svg>')
