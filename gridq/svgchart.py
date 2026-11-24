"""Dependency-free SVG line charts for result files (ported unchanged from
evsim/svgchart.py, BlokcChin@796b43c, where it drew the A5 break-even plots).
Here: safety and availability charts, and the C vs D common-cause boundary
(a line plus a band for the region where D is the safer choice).

Colors are the dataviz reference palette: categorical slots in fixed order,
stepped separately for light and dark (validated with validate_palette.js), so
a chart viewed in dark mode is its own selected palette, not an inversion.
Text wears ink tokens, never series colors. Every marker has a native <title>
tooltip on a hit target larger than the mark, and the CSV written next to each
chart is its table view.

chart(panels, ...) draws one or more panels side by side with shared axes:
  panel = {"title": str,
           "series": [{"label", "slot", "points": [(x, y, tip)], "band": [(x, lo, hi)] | None,
                       "dash": bool, "end_label": str | None}],
           "refs":   [{"label", "points": [(x, y)], "label_at": "auto" | "start" | "end"}],
           "extra":  [{"x", "y", "slot", "tip"}]}           # hollow diamonds
"""
from __future__ import annotations

import math
from html import escape
from typing import Callable, List, Optional, Sequence, Tuple

LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
         "grid": "#e1e0d9", "axis": "#c3c2b7",
         "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a", "s4": "#eda100"}
DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835",
        "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70", "s4": "#c98500"}
FONT = "system-ui, -apple-system, 'Segoe UI', 'PingFang TC', 'Noto Sans CJK TC', 'Microsoft JhengHei', sans-serif"


def _vars(d: dict) -> str:
    return "".join(f"--{k}:{v};" for k, v in d.items())


def _style() -> str:
    series = "".join(f".l{i}{{stroke:var(--s{i})}}.f{i}{{fill:var(--s{i})}}.b{i}{{fill:var(--s{i});fill-opacity:.14}}"
                     for i in range(1, 5))
    return ("<style>"
            f"svg.viz{{{_vars(LIGHT)}font-family:{FONT}}}"
            f"@media (prefers-color-scheme: dark){{svg.viz{{{_vars(DARK)}}}}}"
            ".bg{fill:var(--surface)}.t1{fill:var(--ink)}.t2{fill:var(--ink2)}.tm{fill:var(--muted)}"
            ".grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}"
            ".ref{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:5 4;fill:none}"
            ".line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}"
            ".dash{stroke-dasharray:6 4}"
            ".mk{stroke:var(--surface);stroke-width:2}"
            ".hollow{fill:var(--surface);stroke-width:2}"
            ".hit{fill:transparent;pointer-events:all}.hit:hover+.mk,.mk:hover{stroke:var(--ink)}"
            f"{series}"
            "text{font-size:12px}.title{font-size:15px;font-weight:600}.ptitle{font-size:13px;font-weight:600}"
            ".small{font-size:11px}"
            "</style>")


def nice_step(span: float, target: int = 5) -> float:
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def linear_ticks(lo: float, hi: float, target: int = 5) -> List[float]:
    step = nice_step(hi - lo, target)
    start = math.ceil(lo / step - 1e-9) * step
    out, t = [], start
    while t <= hi + 1e-9 * step:
        out.append(round(t, 10))
        t += step
    return out


def log_ticks(lo: float, hi: float) -> List[float]:
    return [10.0 ** e for e in range(math.ceil(math.log10(lo) - 1e-9), math.floor(math.log10(hi) + 1e-9) + 1)]


def fmt_count_zh(v: float) -> str:
    """1,000 / 1 萬 / 10 萬 (Chinese axis labels)."""
    if v >= 10_000:
        w = v / 10_000
        return f"{w:g} 萬"
    return f"{v:,.0f}" if v >= 1 else f"{v:g}"


def text_width(s: str, size: float = 12.0) -> float:
    """Rough rendered width: CJK and full-width characters ~1 em, the rest ~0.58 em."""
    return sum(size if ord(c) >= 0x2E80 else 0.58 * size for c in s)


def _path(pts: Sequence[Tuple[float, float]]) -> str:
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def chart(panels: List[dict], *, title: str, subtitle: str = "", legend: Sequence[Tuple[str, int, str]] = (),
          x_label: str = "", y_label: str = "", ylog: bool = False,
          y_domain: Optional[Tuple[float, float]] = None, x_domain: Optional[Tuple[float, float]] = None,
          x_ticks: Optional[List[float]] = None, x_fmt: Callable[[float], str] = lambda v: f"{v:g}",
          y_fmt: Callable[[float], str] = fmt_count_zh, panel_w: int = 400, plot_h: int = 260,
          desc: str = "") -> str:
    """Return an SVG document. legend items: (label, slot, kind) with kind "line", "band" or "ref"."""
    end_w = max([text_width(s_.get("end_label") or "", 11) for p in panels for s_ in p.get("series", [])] + [0])
    ml, mr, gap = 72, max(24, int(end_w) + 16), max(36, int(end_w) + 24)
    legend_rows = 1 if legend else 0
    has_ptitle = any(p.get("title") for p in panels)
    sub_lines = subtitle.split("\n") if subtitle else []
    mt = 44 + 18 * len(sub_lines) + 22 * legend_rows + (26 if has_ptitle else 8)
    mb = 50
    width = ml + mr + len(panels) * panel_w + (len(panels) - 1) * gap
    height = mt + plot_h + mb
    pw = panel_w

    xs = [x for p in panels for s in p.get("series", []) for x, *_ in s["points"]]
    xs += [x for p in panels for r in p.get("refs", []) for x, _ in r["points"]]
    x0, x1 = x_domain or (min(xs), max(xs))
    if x1 == x0:
        x1 = x0 + 1
    padx = (x1 - x0) * 0.04
    x0, x1 = (x0 - padx, x1 + padx) if x_domain is None else (x0, x1)
    ys = [y for p in panels for s in p.get("series", []) for _, y, *_ in s["points"] if y is not None and math.isfinite(y)]
    ys += [v for p in panels for s in p.get("series", []) for _, lo, hi in (s.get("band") or [])
           for v in (lo, hi) if math.isfinite(v)]
    ys += [y for p in panels for r in p.get("refs", []) for _, y in r["points"]]
    ys += [e["y"] for p in panels for e in p.get("extra", []) if math.isfinite(e["y"])]
    if y_domain:
        y0, y1 = y_domain
    elif ylog:
        pos = [y for y in ys if y > 0]
        y0 = 10 ** math.floor(math.log10(min(pos))) if pos else 1.0
        y1 = 10 ** math.ceil(math.log10(max(pos))) if pos else 10.0
    else:
        y0, y1 = 0.0, max(ys) * 1.08 if ys else 1.0
        y1 = linear_ticks(0, y1)[-1] if linear_ticks(0, y1)[-1] >= y1 else y1 + nice_step(y1)

    def sx(i, x):
        return ml + i * (pw + gap) + (x - x0) / (x1 - x0) * pw

    def sy(y):
        if ylog:
            y = min(max(y, y0), y1)
            return mt + plot_h - (math.log10(y) - math.log10(y0)) / (math.log10(y1) - math.log10(y0)) * plot_h
        return mt + plot_h - (y - y0) / (y1 - y0) * plot_h

    yt = log_ticks(y0, y1) if ylog else linear_ticks(y0, y1)
    xt = x_ticks or linear_ticks(x0, x1)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" viewBox="0 0 {width} {height}" '
           f'width="{width}" height="{height}" role="img" aria-labelledby="t d">',
           _style(), f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc or subtitle)}</desc>',
           f'<rect class="bg" x="0" y="0" width="{width}" height="{height}" rx="8"/>',
           f'<text class="t1 title" x="{ml}" y="26">{escape(title)}</text>']
    for j, line in enumerate(sub_lines):
        out.append(f'<text class="t2" x="{ml}" y="{46 + 18 * j}">{escape(line)}</text>')
    # legend
    if legend:
        lx, ly = ml, 44 + 18 * len(sub_lines) + 14
        for label, slot, kind in legend:
            if kind == "band":
                out.append(f'<rect class="b{slot}" x="{lx}" y="{ly - 9}" width="22" height="12" rx="2"/>')
            elif kind == "ref":
                out.append(f'<path class="ref" d="M{lx},{ly - 3} h22"/>')
            else:
                out.append(f'<path class="line l{slot}" d="M{lx},{ly - 3} h22"/>'
                           f'<circle class="mk f{slot}" cx="{lx + 11}" cy="{ly - 3}" r="4"/>')
            out.append(f'<text class="t2" x="{lx + 28}" y="{ly + 1}">{escape(label)}</text>')
            lx += 28 + text_width(label) + 22
    for i, p in enumerate(panels):
        left, right = sx(i, x0), sx(i, x1)
        out.append(f'<text class="t1 ptitle" x="{left}" y="{mt - 12}">{escape(p.get("title", ""))}</text>')
        for t in yt:
            y = sy(t)
            out.append(f'<line class="grid" x1="{left:.1f}" x2="{right:.1f}" y1="{y:.1f}" y2="{y:.1f}"/>')
            if i == 0:
                out.append(f'<text class="tm" x="{left - 8:.1f}" y="{y + 4:.1f}" text-anchor="end">{escape(y_fmt(t))}</text>')
        out.append(f'<line class="axis" x1="{left:.1f}" x2="{right:.1f}" y1="{mt + plot_h}" y2="{mt + plot_h}"/>')
        for t in xt:
            if x0 - 1e-9 <= t <= x1 + 1e-9:
                x = sx(i, t)
                out.append(f'<line class="axis" x1="{x:.1f}" x2="{x:.1f}" y1="{mt + plot_h}" y2="{mt + plot_h + 4}"/>'
                           f'<text class="tm" x="{x:.1f}" y="{mt + plot_h + 18}" text-anchor="middle">{escape(x_fmt(t))}</text>')
        if x_label:
            out.append(f'<text class="t2" x="{(left + right) / 2:.1f}" y="{mt + plot_h + 40}" '
                       f'text-anchor="middle">{escape(x_label)}</text>')
        # bands first, then reference lines, then series lines and markers
        for s in p.get("series", []):
            band = [b for b in (s.get("band") or []) if math.isfinite(b[1]) and math.isfinite(b[2])]
            if len(band) >= 2:
                top = [(sx(i, x), sy(hi)) for x, lo, hi in band]
                bot = [(sx(i, x), sy(lo)) for x, lo, hi in reversed(band)]
                out.append(f'<path class="b{s["slot"]}" d="{_path(top + bot)} Z"/>')
        marks = []                                       # pixel points the reference labels should avoid
        for s_ in p.get("series", []):
            pts_ = [(sx(i, x), sy(y)) for x, y, *_ in s_["points"] if y is not None and math.isfinite(y)]
            marks += pts_
            for (a1, b1), (a2, b2) in zip(pts_, pts_[1:]):
                marks += [(a1 + (a2 - a1) * t / 6, b1 + (b2 - b1) * t / 6) for t in range(1, 6)]
        for r in p.get("refs", []):
            pts = [(sx(i, x), sy(y)) for x, y in r["points"]]
            out.append(f'<path class="ref" d="{_path(pts)}"><title>{escape(r["label"])}</title></path>')
            tw = text_width(r["label"], 11)
            cands = []
            for side in ("start", "end"):
                if r.get("label_at", "auto") not in ("auto", side):
                    continue
                ax, ay = pts[0] if side == "start" else pts[-1]
                x_lo = ax + 4 if side == "start" else ax - 4 - tw
                for dy in (-6, 15):                      # above or below the line
                    y_base = ay + dy
                    near = [abs(b - (y_base - 4)) for a, b in marks if x_lo - 6 <= a <= x_lo + tw + 6]
                    cands.append((min(near, default=99.0), side == "start", dy == -6, side, x_lo, y_base))
            _, _, _, side, x_lo, y_base = max(cands)
            out.append(f'<text class="tm small" x="{x_lo:.1f}" y="{y_base:.1f}">{escape(r["label"])}</text>')
        ends = []
        for s in p.get("series", []):
            pts = [(sx(i, x), sy(y), tip) for x, y, tip in s["points"] if y is not None and math.isfinite(y)]
            if len(pts) >= 2:
                cls = f'line l{s["slot"]}' + (" dash" if s.get("dash") else "")
                out.append(f'<path class="{cls}" d="{_path([(a, b) for a, b, _ in pts])}"/>')
            for a, b, tip in pts:
                out.append(f'<circle class="hit" cx="{a:.1f}" cy="{b:.1f}" r="11"><title>{escape(tip)}</title></circle>'
                           f'<circle class="mk f{s["slot"]}" cx="{a:.1f}" cy="{b:.1f}" r="4.5">'
                           f'<title>{escape(tip)}</title></circle>')
            if pts and s.get("end_label"):
                ends.append([pts[-1][0] + 8, pts[-1][1] + 4, s["end_label"]])
        for e in p.get("extra", []):
            if not math.isfinite(e["y"]):
                continue
            a, b = sx(i, e["x"]), sy(e["y"])
            d = f"M{a:.1f},{b - 6:.1f} L{a + 6:.1f},{b:.1f} L{a:.1f},{b + 6:.1f} L{a - 6:.1f},{b:.1f} Z"
            out.append(f'<path class="hollow l{e["slot"]}" d="{d}"><title>{escape(e["tip"])}</title></path>')
        ends.sort(key=lambda e: e[1])
        for j in range(1, len(ends)):                    # keep end labels 14 px apart
            ends[j][1] = max(ends[j][1], ends[j - 1][1] + 14)
        for a, b, label in ends:
            out.append(f'<text class="t2 small" x="{a:.1f}" y="{b:.1f}">{escape(label)}</text>')
    if y_label:
        out.append(f'<text class="t2" transform="translate(16 {mt + plot_h / 2:.1f}) rotate(-90)" '
                   f'text-anchor="middle">{escape(y_label)}</text>')
    out.append("</svg>")
    return "\n".join(out)
