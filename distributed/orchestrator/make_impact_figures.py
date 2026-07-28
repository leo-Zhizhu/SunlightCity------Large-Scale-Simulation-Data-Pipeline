#!/usr/bin/env python3
"""
Generates the README's impact figures as self-contained SVGs (light + dark).

Pure stdlib on purpose: the README's assets must be regenerable without
matplotlib, and hand-built SVG gives exact control over the annotation-heavy
layouts these figures need.

Figures
-------
  1. io_volume       — bytes to PostgreSQL, v1 vs v2. MEASURED.
  2. scaling_curve   — Amdahl curve with the serial reduce floor annotated,
                       calibrated on the measured single-node run. MODELLED.
  3. failure_timeline— lease-based recovery of a killed worker. ILLUSTRATIVE.

Every figure states in-panel whether its numbers are measured, modelled or
illustrative. Presenting a projection as a measurement would be the easiest way
to make this whole document untrustworthy.

Usage:
    python make_impact_figures.py [output_dir]
"""

from __future__ import annotations

import math
import os
import sys

# ---------------------------------------------------------------------------
# Palette — validated reference instance (see the dataviz palette reference).
# Sequential blue for magnitude; slot-2 orange only where two entities must be
# told apart; status red reserved for the failure figure.
# ---------------------------------------------------------------------------
THEMES = {
    "light": dict(
        surface="#fcfcfb", panel="#f4f4f1",
        ink="#0b0b0b", ink2="#52514e", muted="#898781",
        grid="#e1e0d9", axis="#c3c2b7",
        v1="#898781",          # old architecture: deliberately neutral/recessive
        v2="#256abf",          # new architecture: the sequential hue
        v2_light="#9ec5f4",
        accent="#c2410c",      # annotation callouts
        good="#0ca30c", bad="#d03b3b",
        on_v2="#ffffff", on_v1="#ffffff",
    ),
    "dark": dict(
        surface="#1a1a19", panel="#242422",
        ink="#ffffff", ink2="#c3c2b7", muted="#898781",
        grid="#2c2c2a", axis="#383835",
        v1="#6b6a66",
        v2="#5598e7",
        v2_light="#1c5cab",
        accent="#f0803c",
        good="#0ca30c", bad="#e66767",
        on_v2="#0b0b0b", on_v1="#ffffff",
    ),
}

FAM = "system-ui,-apple-system,'Segoe UI',sans-serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"

# ---------------------------------------------------------------------------
# Measured / modelled inputs
# ---------------------------------------------------------------------------
RAYCASTS = 1_577_374_560
BASE_RATE = 73_000          # measured: 1.577e9 raycasts in 6.0 h, single node
T_PAR = RAYCASTS / BASE_RATE   # 21,608 s of parallelisable work
T_SERIAL = 300                 # reduce phase floor (index + ANALYZE + rollup, ~2 GB)

RAW_GB = 110.0              # measured: meo_exposure_samples for an annual run
AGG_GB = 2.09               # measured: meo_exposure_edges after aggregation


def wall_clock(n: int) -> float:
    """Amdahl: parallel raycasting over n workers plus an irreducible serial reduce."""
    return T_PAR / n + T_SERIAL


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SVG:
    """Minimal SVG builder. Keeps the figure code readable."""

    def __init__(self, w: int, h: int, t: dict, label: str):
        self.w, self.h, self.t = w, h, t
        self.o = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">',
            f'<rect width="{w}" height="{h}" fill="{t["surface"]}"/>',
        ]

    def rect(self, x, y, w, h, fill, rx=0, opacity=None, stroke=None, sw=1):
        op = f' opacity="{opacity}"' if opacity is not None else ""
        st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(0,w):.1f}" '
                      f'height="{max(0,h):.1f}" rx="{rx}" fill="{fill}"{op}{st}/>')

    def text(self, x, y, s, size=11, fill=None, anchor="start", weight="400",
             family=None, opacity=None):
        fill = fill or self.t["ink"]
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.o.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family or FAM}" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}"{op} '
            f'style="font-variant-numeric:tabular-nums">{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke=None, sw=1, dash=None, cap="butt", opacity=None):
        stroke = stroke or self.t["grid"]
        d = f' stroke-dasharray="{dash}"' if dash else ""
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                      f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"{d}{op}/>')

    def path(self, d, stroke=None, sw=2, fill="none", dash=None, opacity=None):
        stroke = stroke or self.t["ink"]
        ds = f' stroke-dasharray="{dash}"' if dash else ""
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.o.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
                      f'stroke-linejoin="round" stroke-linecap="round"{ds}{op}/>')

    def circle(self, cx, cy, r, fill, stroke=None, sw=2):
        st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}"{st}/>')

    def done(self) -> str:
        self.o.append("</svg>")
        return "\n".join(self.o)


# ===========================================================================
# FIGURE 1 — I/O volume (measured)
# ===========================================================================
def fig_io(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 300
    s = SVG(W, H, t,
            f"Bytes written to PostgreSQL per annual run: {RAW_GB:.0f} GB before "
            f"the map-side combiner versus {AGG_GB} GB after, a "
            f"{RAW_GB/AGG_GB:.0f} times reduction.")

    s.text(40, 34, "Bytes written to PostgreSQL, per annual run", 16, t["ink"], weight="600")
    s.text(40, 55, "The map-side combiner aggregates before the wire, not after it.",
           11.5, t["muted"])
    s.text(W - 40, 34, "MEASURED", 9.5, t["muted"], anchor="end", weight="600")

    x0, bar_w = 210, 520
    row_h, y = 46, 96

    for label, sub, gb, colour, ink in (
        ("v1  single node", "raw per-sample booleans", RAW_GB, t["v1"], t["on_v1"]),
        ("v2  combiner",    "per-edge aggregates",     AGG_GB, t["v2"], t["on_v2"]),
    ):
        w = bar_w * (gb / RAW_GB)
        s.text(x0 - 16, y + 19, label, 12.5, t["ink"], anchor="end", weight="600")
        s.text(x0 - 16, y + 34, sub, 10, t["muted"], anchor="end")

        # Track, so the tiny v2 bar still reads as a proportion of the same scale.
        s.rect(x0, y, bar_w, row_h - 12, t["panel"], rx=4)
        s.rect(x0, y, w, row_h - 12, colour, rx=4)

        val = f"{gb:.0f} GB" if gb >= 10 else f"{gb:.2f} GB"
        if w > 90:
            s.text(x0 + w - 12, y + 23, val, 13, ink, anchor="end", weight="700")
        else:
            s.text(x0 + w + 10, y + 23, val, 13, t["ink"], weight="700")
        y += row_h + 24

    # Reduction callout
    cy = 96 + (row_h - 12) + 12
    s.line(x0 + bar_w * (AGG_GB / RAW_GB), cy + 8, x0 + bar_w, cy + 8,
           t["accent"], 1.5, dash="3 3")
    s.text(x0 + bar_w / 2, cy + 2, f"{RAW_GB/AGG_GB:.0f}x less written",
           11.5, t["accent"], anchor="middle", weight="700")

    # Footer: what the number means operationally
    fy = 228
    s.rect(40, fy, W - 80, 48, t["panel"], rx=6)
    s.text(56, fy + 20,
           "98% of what the single-node pipeline sent to the database was discarded by the "
           "very next step.",
           11, t["ink2"])
    s.text(56, fy + 37,
           "Sharding by edge (not by sample point) makes each worker's per-edge sum final, "
           "so aggregating locally is exact.",
           11, t["muted"])
    return s.done()


# ===========================================================================
# FIGURE 2 — Amdahl scaling curve (modelled)
# ===========================================================================
def fig_scaling(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 450
    s = SVG(W, H, t,
            "Modelled wall clock against worker count. 50 workers reach about 12 minutes, "
            "a 30x speedup over the measured 6 hour single-node baseline. The curve "
            "flattens toward a 5 minute floor set by the serial reduce phase.")

    s.text(40, 34, "Why 50 workers, and not 500", 16, t["ink"], weight="600")
    s.text(40, 55, "Amdahl model calibrated on the measured single-node run "
                   "(21,608 s parallel + 300 s serial reduce).", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["accent"], anchor="end", weight="600")

    # Plot area
    px, py = 84, 92
    pw, ph = W - px - 172, 238

    # Log-x from 1 to 512 workers; log-y over wall clock.
    n_min, n_max = 1, 512
    t_max, t_min = wall_clock(n_min), T_SERIAL * 0.82

    def sx(n):  return px + pw * (math.log10(n) - math.log10(n_min)) / (math.log10(n_max) - math.log10(n_min))
    def sy(v):  return py + ph * (math.log10(t_max) - math.log10(v)) / (math.log10(t_max) - math.log10(t_min))

    # --- y gridlines at human durations ---
    for secs, lab in ((6*3600, "6 h"), (3600, "1 h"), (30*60, "30 min"),
                      (10*60, "10 min"), (5*60, "5 min")):
        if not (t_min <= secs <= t_max):
            continue
        y = sy(secs)
        s.line(px, y, px + pw, y, t["grid"], 1)
        s.text(px - 10, y + 3.5, lab, 10, t["muted"], anchor="end")

    # --- x ticks ---
    for n in (1, 2, 5, 10, 25, 50, 100, 200, 500):
        x = sx(n)
        s.line(x, py + ph, x, py + ph + 4, t["axis"], 1)
        s.text(x, py + ph + 18, str(n), 10, t["muted"], anchor="middle")
    s.text(px + pw / 2, py + ph + 36, "workers", 10.5, t["ink2"], anchor="middle")

    # y-axis label, rotated
    s.o.append(
        f'<text x="{px - 52}" y="{py + ph/2}" font-family="{FAM}" font-size="10.5" '
        f'fill="{t["ink2"]}" text-anchor="middle" '
        f'transform="rotate(-90 {px - 52} {py + ph/2})">wall clock (log)</text>')

    # --- the irreducible serial floor ---
    yf = sy(T_SERIAL)
    s.line(px, yf, px + pw, yf, t["accent"], 1.5, dash="5 4")
    s.text(px + pw + 10, yf + 3.5, "5 min floor", 10.5, t["accent"], weight="600")
    s.text(px + pw + 10, yf + 18, "serial reduce", 9.5, t["muted"])

    # --- ideal linear scaling, for contrast ---
    # Clipped at the plot floor: unclipped it runs off the bottom of the axes,
    # which reads as a rendering bug rather than as "off the scale".
    ideal_pts = []
    for n in (1, 2, 5, 10, 25, 50, 100, 200, 512):
        v = T_PAR / n
        if v < t_min:
            break
        ideal_pts.append((sx(n), sy(v)))
    if len(ideal_pts) > 1:
        s.path("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in ideal_pts),
               t["muted"], 1.5, dash="4 4", opacity=0.7)
        s.text(ideal_pts[-1][0] + 6, ideal_pts[-1][1] + 12, "ideal", 9,
               t["muted"], anchor="middle")

    # --- modelled curve ---
    pts = []
    n = 1.0
    while n <= n_max:
        pts.append((sx(n), sy(max(t_min, wall_clock(n)))))
        n *= 1.06
    s.path("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts), t["v2"], 2.5)

    # --- markers ---
    for n, note in ((1, None), (10, None), (50, "chosen"), (200, None)):
        x, y = sx(n), sy(wall_clock(n))
        s.circle(x, y, 5.5, t["surface"], t["v2"], 2.5)
        mins = wall_clock(n) / 60
        lab = f"{mins/60:.1f} h" if mins >= 90 else f"{mins:.0f} min"
        s.text(x, y - 14, lab, 10.5, t["ink"], anchor="middle", weight="700")

    # Highlight the operating point. Annotated up-and-left with a leader line:
    # directly below collides with both the ideal-linear dashes and the x-ticks.
    x50, y50 = sx(50), sy(wall_clock(50))
    s.circle(x50, y50, 9, "none", t["accent"], 2)
    ax, ay = x50 - 96, y50 - 46
    s.line(ax + 78, ay + 6, x50 - 11, y50 - 5, t["accent"], 1, dash="2 2")
    s.rect(ax - 6, ay - 12, 92, 32, t["surface"], rx=4)
    s.text(ax + 40, ay + 1, "50 workers", 10.5, t["accent"], anchor="middle", weight="700")
    s.text(ax + 40, ay + 14, "30x · 60% eff.", 9.5, t["muted"], anchor="middle")

    # legend
    lx, ly = px + pw + 14, py + 8
    s.line(lx, ly, lx + 22, ly, t["v2"], 2.5)
    s.text(lx + 28, ly + 3.5, "modelled", 10, t["ink2"])
    s.line(lx, ly + 18, lx + 22, ly + 18, t["muted"], 1.5, dash="4 4")
    s.text(lx + 28, ly + 21.5, "ideal linear", 10, t["muted"])

    # footer
    fy = H - 44
    s.rect(40, fy, W - 80, 34, t["panel"], rx=6)
    s.text(56, fy + 21,
           "Past ~50 workers the serial reduce phase dominates: 100 workers buys 3.6 more "
           "minutes, 500 buys 6.5. Diminishing returns, not a wall.",
           11, t["ink2"])
    return s.done()


# ===========================================================================
# FIGURE 3 — lease-based failure recovery (illustrative)
# ===========================================================================
def fig_failure(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 340
    s = SVG(W, H, t,
            "Timeline showing a worker killed mid-task. Its lease stops being renewed, "
            "expires, and the reaper returns the task to the queue where another worker "
            "claims and completes it. No coordinator is involved.")

    s.text(40, 34, "How a dead worker recovers", 16, t["ink"], weight="600")
    s.text(40, 55, "An unrenewed lease IS the failure signal — no controller, no pod watch, "
                   "no liveness probe.", 11.5, t["muted"])
    s.text(W - 40, 34, "ILLUSTRATIVE", 9.5, t["muted"], anchor="end", weight="600")

    px, pw = 150, W - 150 - 60
    T = 660.0                      # seconds of timeline
    def sx(sec): return px + pw * (sec / T)

    # time axis
    ay = 96
    for sec in range(0, int(T) + 1, 60):
        x = sx(sec)
        s.line(x, ay, x, H - 76, t["grid"], 1)
        s.text(x, ay - 8, f"{sec//60}m", 9.5, t["muted"], anchor="middle")

    lanes = [
        ("worker-a7f3", "pod on spot node", 128),
        ("lease state", "meo_tasks row",    186),
        ("worker-b21c", "picks up the task", 244),
    ]
    for name, sub, y in lanes:
        s.text(px - 16, y + 4, name, 11.5, t["ink"], anchor="end", weight="600")
        s.text(px - 16, y + 18, sub, 9.5, t["muted"], anchor="end")
        s.line(px, y + 10, px + pw, y + 10, t["axis"], 1)

    bh = 20

    # --- worker A: works, then is killed at t=240 ---
    ya = 128
    s.rect(sx(0), ya, sx(240) - sx(0), bh, t["v2"], rx=4)
    # Short label: the bar is only ~230 px wide, and the fuller wording overflowed it.
    s.text(sx(120), ya + 14, "raycasting", 10, t["on_v2"], anchor="middle", weight="600")
    # heartbeat ticks — the visual rhythm carries "every 30 s" without the words
    for sec in range(30, 240, 30):
        s.line(sx(sec), ya + 2, sx(sec), ya + bh - 2, t["surface"], 1, opacity=0.5)
    s.text(sx(0), ya - 10, "│ = heartbeat (30 s)", 9, t["muted"])
    # kill
    s.line(sx(240), ya - 14, sx(240), ya + bh + 8, t["bad"], 2)
    s.text(sx(240) + 8, ya - 4, "SIGKILL", 10.5, t["bad"], weight="700")
    s.text(sx(240) + 8, ya + 10, "spot reclaim / OOM / node loss", 9.5, t["muted"])

    # --- lease lane ---
    yl = 186
    s.rect(sx(0), yl, sx(240) - sx(0), bh, t["v2_light"], rx=4)
    s.text(sx(120), yl + 14, "leased, renewed", 10, t["ink"], anchor="middle")
    # dead window
    s.rect(sx(240), yl, sx(390) - sx(240), bh, t["panel"], rx=4,
           stroke=t["bad"], sw=1.5)
    s.text(sx(315), yl + 14, "not renewed", 10, t["bad"], anchor="middle", weight="600")
    s.line(sx(390), yl - 14, sx(390), yl + bh + 8, t["accent"], 2, dash="4 3")
    s.text(sx(390) + 8, yl - 4, "lease expires", 10.5, t["accent"], weight="700")
    # The requeue is a near-instant event, so it gets a narrow marker with the
    # label placed OUTSIDE it — the previous inline text overflowed a 30 px box.
    s.rect(sx(390), yl, sx(412) - sx(390), bh, t["accent"], rx=3)
    s.text(sx(412) + 6, yl + 14, "back to pending", 9.5, t["accent"], weight="600")

    # --- worker B ---
    yb = 244
    s.rect(sx(424), yb, sx(650) - sx(424), bh, t["v2"], rx=4)
    s.text(sx(537), yb + 14, "re-claims · redoes · completes", 10, t["on_v2"],
           anchor="middle", weight="600")
    s.circle(sx(656), yb + 10, 5.5, t["good"])

    # annotation bracket for the recovery window
    by = 292
    s.line(sx(240), by, sx(420), by, t["accent"], 1.5)
    s.line(sx(240), by - 4, sx(240), by + 4, t["accent"], 1.5)
    s.line(sx(420), by - 4, sx(420), by + 4, t["accent"], 1.5)
    s.text(sx(330), by - 8, "recovery window = lease TTL", 10, t["accent"],
           anchor="middle", weight="600")
    s.text(sx(330), by + 16,
           "on graceful SIGTERM the lease is released immediately instead",
           9.5, t["muted"], anchor="middle")

    return s.done()


FIGURES = {
    "io_volume": fig_io,
    "scaling_curve": fig_scaling,
    "failure_timeline": fig_failure,
}


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    for name, fn in FIGURES.items():
        for theme in ("light", "dark"):
            path = os.path.join(out, f"{name}_{theme}.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(theme))
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
