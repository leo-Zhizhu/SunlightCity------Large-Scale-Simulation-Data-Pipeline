#!/usr/bin/env python3
"""
Generates the README's figures as self-contained SVGs (light + dark).

Pure stdlib on purpose: the README's assets must be regenerable without matplotlib,
and hand-built SVG gives exact control over the annotation-heavy layouts these
figures need.

Every number comes from model.py, imported rather than copied, so a figure cannot
disagree with the capacity model or with the docs that quote it.

Figures
-------
THE SIZING ARGUMENT — read in this order; together they are the derivation that
docs/PERFORMANCE.md walks through in prose.

  1. bench_ladder       the two throughput chains, built from the individual
                        benchmarks, and the headroom between them. Where W = 6S
                        comes from.
  2. feasibility_map    every (workers, shards) pair against the 15-minute
                        deadline. Three nested regions: misses it, meets it
                        nominally, survives the stress envelope.
  3. cost_time          the cost/time Pareto frontier, and the fact that it has
                        no knee — so the deadline is what selects a point.
  4. stress_envelope    the four conditions, for the cheapest deadline-meeting
                        shape and for the deployed one. The cheapest fails three.
  5. worker_ceiling     wall clock vs fleet size at several shard counts. Why
                        "add nodes" stops working, and who decides when.

THE RESULT

  6. shard_scaling      wall clock vs shard count at a fixed 54 workers. The
                        headline: the same fleet finishes in 1.18 h on one
                        database instance and 11m 09s on nine.
  7. phase_breakdown    where the 11m 09s goes, and why writing is free (it
                        overlaps raycasting) while the reduce phase is not.
  8. directional_cost   the same edge at the same instant, walked both ways. This
                        is why the schema keeps per-sample rows.
  9. failure_timeline   lease-based recovery of a killed worker.

Every figure is emitted in light and dark. Check them RASTERISED after any edit —
overlapping labels and text running off the canvas are invisible in the SVG source
and were the only defects found in review both times.

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
# Inputs — imported, never copied.
#
# Every figure below is drawn from model.py, which is also what the README, the docs
# and reduce_finalize.py's throughput report read. Duplicating a single number here
# would be one more place for the documentation to drift out of agreement with the
# system it describes.
# ---------------------------------------------------------------------------
import model


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

    def vtext(self, x, y, s, size=11, fill=None, anchor="middle", weight="400"):
        """Rotated -90 degrees, for y-axis titles. A horizontal one collides with
        the tick labels at any margin narrow enough to be worth having."""
        fill = fill or self.t["ink"]
        self.o.append(
            f'<text x="0" y="0" transform="translate({x:.1f},{y:.1f}) rotate(-90)" '
            f'font-family="{FAM}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')

    def footer(self, text: str, size=11, pad=16, lead=15):
        """
        Bottom explanatory panel, wrapped to the panel's actual width and anchored
        to the bottom of the canvas.

        Written as a helper after hand-positioned footers overran the right edge in
        every one of these figures — the text is prose and its length changes
        whenever a model number does, so it cannot be laid out by eye.
        """
        avail = self.w - 80 - 2 * pad
        budget = int(avail / (size * 0.505))       # measured for this font stack
        lines, cur = [], ""
        for word in text.split():
            if len(cur) + len(word) + 1 > budget:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            lines.append(cur)
        h = 2 * pad + lead * (len(lines) - 1) + size
        top = self.h - h - 18
        self.rect(40, top, self.w - 80, h, self.t["panel"], rx=6)
        for i, ln in enumerate(lines):
            self.text(40 + pad, top + pad + size * 0.82 + i * lead, ln, size,
                      self.t["ink2"] if i == 0 else self.t["muted"])
        return top

    def done(self) -> str:
        self.o.append("</svg>")
        return "\n".join(self.o)




def fmt_t(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m {seconds % 60:02.0f}s"
    return f"{seconds:.0f} s"


# ===========================================================================
# FIGURE 1 — what the database cluster is worth
#
# The one figure that justifies the whole rewrite. Workers are held FIXED at 50 and
# only the shard count varies, so the curve isolates the database's contribution
# from the fleet's.
# ===========================================================================
def fig_shard_scaling(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 400
    t1 = model.total_seconds(model.WORKERS, 1)
    tN = model.total_seconds()

    s = SVG(W, H, t,
            f"Wall clock against database shard count at a fixed {model.WORKERS} workers. "
            f"One instance takes {fmt_t(t1)}; ten take {fmt_t(tN)}. Below eight shards "
            f"the fleet is I/O-bound and waiting on the database.")

    s.text(40, 34, "The bottleneck was never the raycasting", 16, t["ink"], weight="600")
    s.text(40, 55, f"Same {model.WORKERS} workers, same code. Only the number of "
                   "PostgreSQL instances changes.", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, py, pw, ph = 78, 88, W - 78 - 170, 216
    shards = list(range(1, 21))
    times = [model.total_seconds(model.WORKERS, k) for k in shards]
    t_max, t_min = max(times) * 1.06, 0.0

    def sx(k): return px + pw * (k - 1) / (len(shards) - 1)
    def sy(v): return py + ph * (1 - (v - t_min) / (t_max - t_min))

    # gridlines at round minute marks
    for mins in range(0, int(t_max // 60) + 1, 3):
        y = sy(mins * 60)
        if y < py: continue
        s.line(px, y, px + pw, y, t["grid"], 1)
        s.text(px - 10, y + 4, f"{mins}m", 9.5, t["muted"], anchor="end")
    for k in (1, 5, 10, 15, 20):
        s.text(sx(k), py + ph + 18, str(k), 9.5, t["muted"], anchor="middle")
    s.text(px + pw / 2, py + ph + 38, "database shards", 10.5, t["ink2"], anchor="middle")

    # I/O-bound region: shaded, because it is the point of the figure
    kb = model.balanced_shards()
    s.rect(px, py, sx(kb) - px, ph, t["bad"], rx=0, opacity=0.07)
    # Placed low in the band, not at the top: the curve's 1-shard endpoint label lives
    # up there and the two collided.
    s.text(px + (sx(kb) - px) / 2, py + ph * 0.72, "I/O-bound", 10.5, t["bad"],
           anchor="middle", weight="700")
    s.text(px + (sx(kb) - px) / 2, py + ph * 0.72 + 15, "fleet waits on the DB",
           9, t["muted"], anchor="middle")

    # the curve
    s.path("M " + " L ".join(f"{sx(k):.1f} {sy(v):.1f}" for k, v in zip(shards, times)),
           t["v2"], sw=2.5)

    # the two endpoints that matter
    for k, colour, label, sub in (
        (1, t["v1"], f"1 shard — {fmt_t(t1)}", f"{model.V1_SECONDS / t1:.0f}x vs v1"),
        (model.SHARDS, t["v2"], f"{model.SHARDS} shards — {fmt_t(tN)}",
         f"{model.V1_SECONDS / tN:.0f}x vs v1"),
    ):
        v = model.total_seconds(model.WORKERS, k)
        s.circle(sx(k), sy(v), 5, colour, stroke=t["surface"], sw=2)
        anchor = "start" if k == 1 else "start"
        s.text(sx(k) + 12, sy(v) - 6, label, 11.5, t["ink"], anchor=anchor, weight="700")
        s.text(sx(k) + 12, sy(v) + 8, sub, 9.5, t["muted"], anchor=anchor)

    # deployed marker
    s.line(sx(model.SHARDS), py, sx(model.SHARDS), py + ph, t["accent"], 1.5, dash="4 3")
    s.text(sx(model.SHARDS), py - 6, "deployed", 9.5, t["accent"], anchor="middle",
           weight="600")

    # right-hand summary
    bx = px + pw + 22
    s.rect(bx, py, 128, 96, t["panel"], rx=6)
    s.text(bx + 12, py + 22, f"{t1 / tN:.1f}x", 22, t["v2"], weight="700")
    s.text(bx + 12, py + 40, "from the cluster", 10, t["ink2"])
    s.text(bx + 12, py + 60, f"of the {model.V1_SECONDS / tN:.0f}x total", 9.5, t["muted"])
    s.text(bx + 12, py + 76, f"the other {model.BATCH_SPEEDUP * model.LOCALITY_SPEEDUP:.1f}x",
           9.5, t["muted"])
    s.text(bx + 12, py + 88, "is per-worker", 9.5, t["muted"])

    # footer
    fy = H - 74
    s.rect(40, fy, W - 80, 56, t["panel"], rx=6)
    s.text(56, fy + 20,
           f"One instance absorbs ~{model.shard_max_streams() * model.COPY_ROWS_PER_STREAM / 1e6:.1f}M rows/s "
           f"({model.shard_max_streams()} COPY streams on {model.SHARD_VCPU} vCPU, one busy CPU each). "
           f"The fleet produces {model.WORKERS * model.WORKER_ROW_RATE / 1e6:.1f}M rows/s.",
           11, t["ink2"])
    s.text(56, fy + 38,
           f"So the cluster needs at least {kb} to keep up at all. {model.SHARDS} are deployed — "
           f"{model.io_headroom() * 100:+.0f}% headroom, so a checkpoint, a vacuum, or "
           "losing an instance outright cannot stall the fleet.",
           11, t["muted"])
    return s.done()


# ===========================================================================
# FIGURE 2 — where the time goes
# ===========================================================================
def fig_phase_breakdown(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 330
    T = model.total_seconds()
    startup = model.FLEET_STARTUP_SECONDS
    ray = model.raycast_seconds()
    write = model.write_seconds()
    mapt = model.map_seconds()
    red = model.reduce_seconds()

    s = SVG(W, H, t,
            f"Breakdown of the {fmt_t(T)} run: {fmt_t(startup)} fleet spin-up, "
            f"{fmt_t(mapt)} map phase in which writing overlaps raycasting entirely, "
            f"and {fmt_t(red)} of reduce across {model.SHARDS} shards in parallel.")

    s.text(40, 34, f"Where the {fmt_t(T)} goes", 16, t["ink"], weight="600")
    s.text(40, 55, "Writing is free — it happens while the next window is being "
                   "raycast, on a second connection.", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, pw = 40, W - 80
    def sw_(sec): return pw * sec / T

    # ---- the timeline bar ----
    y = 96
    bh = 42
    x = px
    for label, sec, colour, ink in (
        ("spin-up", startup, t["v1"], t["on_v1"]),
        ("MAP", mapt, t["v2"], t["on_v2"]),
        ("REDUCE", red, t["v2_light"], t["ink"]),
    ):
        w = sw_(sec)
        s.rect(x, y, w, bh, colour, rx=4)
        s.text(x + w / 2, y + 20, label, 11.5, ink, anchor="middle", weight="700")
        s.text(x + w / 2, y + 34, fmt_t(sec), 10, ink, anchor="middle")
        s.text(x + w / 2, y - 8, f"{100 * sec / T:.0f}%", 9.5, t["muted"], anchor="middle")
        x += w

    # ---- the overlap, which is the interesting part ----
    oy = y + bh + 30
    s.text(px, oy, "inside the MAP phase — the two run concurrently:", 11, t["ink2"])

    mx = px + sw_(startup)
    mw = sw_(mapt)
    # Labels kept short enough to fit the narrower of the two bars. The COPY bar is
    # 74% of the MAP width, so anything longer than ~30 characters is clipped — and a
    # clipped label in a figure about I/O headroom is a poor advertisement.
    for i, (lbl, sec, colour) in enumerate((
        ("raycasting · 8 job threads/pod", ray, t["v2"]),
        ("binary COPY · 2 streams/pod", write, t["accent"]),
    )):
        ly = oy + 16 + i * 30
        w = mw * sec / mapt
        s.rect(mx, ly, w, 18, colour, rx=3, opacity=0.9 if i == 0 else 0.85)
        s.text(mx + 8, ly + 13, f"{lbl} — {fmt_t(sec)}", 9.5, t["on_v2"], weight="600")
        if i == 1:
            # The slack is deliberate — but it is the fraction of TIME the writer is
            # idle, which is not the same number as the cluster's spare INGEST
            # capacity (io_headroom, +35%). Using that one here would have been wrong.
            idle_pct = 100 * (1 - sec / mapt)
            s.rect(mx + w, ly, mw - w, 18, t["panel"], rx=3)
            s.text(mx + w + 8, ly + 13, f"{idle_pct:.0f}% idle", 9, t["muted"])

    s.line(mx, oy + 10, mx, oy + 78, t["axis"], 1, dash="2 2")
    s.line(mx + mw, oy + 10, mx + mw, oy + 78, t["axis"], 1, dash="2 2")

    # footer
    fy = H - 76
    s.rect(40, fy, W - 80, 58, t["panel"], rx=6)
    s.text(56, fy + 20,
           "MAP costs max(raycast, write), not their sum: a finished window goes to a "
           "writer thread while the main thread claims the next task.",
           11, t["ink2"])
    s.text(56, fy + 39,
           f"REDUCE cannot overlap — it needs the last row — but it is only {fmt_t(red)}: "
           f"a section owns whole edges, so all {model.SHARDS} shards roll up locally.",
           11, t["muted"])
    return s.done()


# ===========================================================================
# FIGURE 3 — why the schema keeps per-sample rows
#
# The numbers are the ones distributed/db/tests/shard_selftest.sql asserts, so this
# figure is a picture of a passing test rather than an illustration of an idea.
# ===========================================================================
def fig_directional_cost(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 400
    s = SVG(W, H, t,
            "The same 400 m street at the same instant, walked in opposite directions. "
            "Walking with the moving shadow gives 504 seconds of sun and a 252 m "
            "continuous exposure; walking against it gives 492 seconds and 246 m. A "
            "per-edge sum cannot tell the two apart.")

    s.text(40, 34, "Why a per-edge sum is not enough", 16, t["ink"], weight="600")
    s.text(40, 55, "Same street, same instant, opposite directions. The walker moves "
                   "through time, so the shadow moves too.", 11.5, t["muted"])
    s.text(W - 40, 34, "MEASURED · shard_selftest.sql", 9.5, t["muted"], anchor="end",
           weight="600")

    # ---- two street strips -------------------------------------------------
    # 210 of right margin, not 120: the two number blocks ("504 s / in sun" and
    # "252 m / longest run") need ~150 px and were running off the canvas.
    px, pw = 150, W - 150 - 210
    strip_h = 30
    N = 40                      # cells drawn; the real edge has 201 sample points

    rows = [
        ("with the sweep",  False, 504.0, 300.0, 62.69, 252, 140),
        ("against it",      True,  492.0, 312.0, 61.19, 246, 232),
    ]

    for label, reverse, sun_s, shade_s, pct, run_m, y in rows:
        s.text(px - 16, y + 12, label, 12, t["ink"], anchor="end", weight="600")
        s.text(px - 16, y + 27, "entry -> exit", 9.5, t["muted"], anchor="end")

        cw = pw / N
        for i in range(N):
            # The shadow boundary advances as the walker advances: at cell i the
            # walker is i/N of the way along AND i/N of the way through the traverse,
            # so the boundary has moved too. Reverse walks the cells from the far end
            # against the same advancing clock, which is why the pattern differs.
            frac = i / (N - 1)
            boundary = frac * 0.62 + 0.19
            pos = (1.0 - frac) if reverse else frac
            sunlit = pos > boundary
            s.rect(px + i * cw, y, cw - 0.6, strip_h,
                   t["accent"] if sunlit else t["v2_light"],
                   opacity=0.95 if sunlit else 0.85)

        s.rect(px, y, pw, strip_h, "none", stroke=t["axis"], sw=1, rx=2)
        # direction arrow
        ay = y + strip_h + 12
        s.line(px, ay, px + pw, ay, t["ink2"], 1)
        s.path(f"M {px + pw - 7:.1f} {ay - 3.5} L {px + pw:.1f} {ay} "
               f"L {px + pw - 7:.1f} {ay + 3.5}", t["ink2"], sw=1.4)

        # numbers
        bx = px + pw + 16
        s.text(bx, y + 13, f"{sun_s:.0f} s", 14, t["accent"], weight="700")
        s.text(bx, y + 27, "in sun", 9.5, t["muted"])
        s.text(bx + 56, y + 13, f"{run_m} m", 12, t["ink"], weight="600")
        s.text(bx + 56, y + 27, "longest run", 9.5, t["muted"])

    # Legend sits in the gap BETWEEN the two strips rather than under the first,
    # where it was competing with that strip's direction arrow.
    ly = 200
    s.rect(px, ly, 14, 10, t["accent"]); s.text(px + 20, ly + 9, "sun", 9.5, t["muted"])
    s.rect(px + 58, ly, 14, 10, t["v2_light"]); s.text(px + 78, ly + 9, "shade", 9.5, t["muted"])

    # ---- the point ---------------------------------------------------------
    cy = 296
    s.rect(40, cy, W - 80, 42, t["panel"], rx=6)
    s.text(56, cy + 18,
           "Both directions cross the SAME set of sample points and the same total "
           "sunlit count. A per-edge sunlit_sum is identical for the two.",
           11, t["ink"])
    s.text(56, cy + 34,
           "The 12-second difference and the 6 m difference in continuous exposure exist "
           "only in the ordered, per-sample series.",
           11, t["muted"])

    fy = H - 46
    s.text(40, fy + 12,
           f"This is why v2 keeps v1's {model.EXPOSURE_ROWS:,} rows instead of "
           f"collapsing them to {model.EDGE_ROWS:,} per-edge sums —", 11, t["ink2"])
    s.text(40, fy + 28,
           f"and therefore why it needs {model.SHARDS} database instances rather "
           "than one.", 11, t["ink2"])
    return s.done()


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


def _mix(a: str, b: str, f: float) -> str:
    """Linear blend of two #rrggbb colours. Used for the feasibility ramp."""
    f = max(0.0, min(1.0, f))
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(pa, pb))


# ===========================================================================
# FIGURE 5 — the feasibility map
#
# The figure the sizing decision is actually made on. Every cell is one candidate
# (workers, shards). Three NESTED regions, and the nesting is the argument:
#
#   grey        misses 15 minutes outright
#   pale blue   meets it nominally — and this is the region a naive capacity plan
#               would choose from, which is why it is drawn at all
#   solid blue  survives the full stress envelope, which is far smaller
#
# The chosen shape is the cheapest cell in the innermost region. Showing the two
# outer regions is what makes "cheapest" mean something.
# ===========================================================================
def fig_feasibility(theme: str) -> str:
    t = THEMES[theme]
    W, H = 900, 545
    shard_range = list(range(2, 17))
    worker_range = list(range(18, 116, 6))

    s = SVG(W, H, t,
            f"Feasibility of every (workers, shards) pair against the 15-minute "
            f"deadline. {model.WORKERS} workers and {model.SHARDS} shards is the "
            f"cheapest pair that survives the full stress envelope.")

    s.text(40, 34, "Where the 15-minute deadline can actually be met", 16, t["ink"],
           weight="600")
    s.text(40, 55, "Each cell is one candidate deployment. The deadline alone admits "
                   "far more shapes than the stress envelope does.", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, py = 84, 108
    cw, ch = 34, 17
    pw, ph = cw * len(shard_range), ch * len(worker_range)

    # Fastest feasible time, for the colour ramp's dark end.
    feas = [model.total_seconds(w, k) for w in worker_range for k in shard_range
            if model.total_seconds(w, k) <= model.TARGET_SECONDS]
    t_fast = min(feas) if feas else 0.0

    for iw, w in enumerate(worker_range):
        for ik, k in enumerate(shard_range):
            x, y = px + ik * cw, py + (len(worker_range) - 1 - iw) * ch
            tt = model.total_seconds(w, k)
            env = model.envelope(w, k)
            if tt > model.TARGET_SECONDS:
                fill, op = t["muted"], 0.13
            elif env["passes"]:
                # Darker = faster, across the innermost region only.
                f = 1.0 - (tt - t_fast) / max(1.0, model.TARGET_SECONDS - t_fast)
                fill, op = _mix(t["v2_light"], t["v2"], f), 1.0
            else:
                fill, op = t["v2_light"], 0.40
            s.rect(x + 0.6, y + 0.6, cw - 1.2, ch - 1.2, fill, rx=2, opacity=op)

    # axes
    for ik, k in enumerate(shard_range):
        if k % 2 == 0:
            s.text(px + ik * cw + cw / 2, py + ph + 16, str(k), 9.5, t["muted"],
                   anchor="middle")
    s.text(px + pw / 2, py + ph + 34, "database shards", 10.5, t["ink2"], anchor="middle")
    for iw, w in enumerate(worker_range):
        if w % 12 == 6 or iw == 0:
            y = py + (len(worker_range) - 1 - iw) * ch + ch / 2 + 3.5
            s.text(px - 10, y, str(w), 9.5, t["muted"], anchor="end")
    s.vtext(px - 46, py + ph / 2, "map workers", 10.5, t["ink2"])

    # The chosen cell, ringed rather than filled so its colour still reads.
    ci = shard_range.index(model.SHARDS)
    cj = worker_range.index(min(worker_range, key=lambda v: abs(v - model.WORKERS)))
    cx = px + ci * cw + cw / 2
    cy = py + (len(worker_range) - 1 - cj) * ch + ch / 2
    s.circle(cx, cy, 7.5, "none", stroke=t["accent"], sw=2.5)
    # Leader up into the margin above the grid: every direction inside the grid
    # lands the label on top of other cells.
    s.line(cx, cy - 9, cx, py - 12, t["accent"], 1.5)
    s.text(cx, py - 18, f"{model.WORKERS} / {model.SHARDS}  ·  {model.vcpu()} vCPU  ·  "
           f"{fmt_t(model.total_seconds())}", 11, t["accent"], anchor="middle",
           weight="700")

    # The shape a deadline-only analysis picks. Marked as rejected, because the
    # whole point of the figure is that it lies in the pale region.
    nc, nw, ns, nt = model.cheapest_nominal()
    if ns in shard_range:
        ni = shard_range.index(ns)
        nj = min(range(len(worker_range)), key=lambda i: abs(worker_range[i] - nw))
        nx = px + ni * cw + cw / 2
        ny = py + (len(worker_range) - 1 - nj) * ch + ch / 2
        s.circle(nx, ny, 5, "none", stroke=t["bad"], sw=2)
        s.line(nx - 7, ny, nx - 26, ny + 20, t["bad"], 1.5)
        s.text(nx - 30, ny + 20, f"{nw}/{ns} — rejected", 10, t["bad"], anchor="end",
               weight="700")
        s.text(nx - 30, ny + 33, f"{fmt_t(nt)}, 1% margin", 9, t["muted"], anchor="end")

    # legend
    lx, ly = px + pw + 30, py - 4
    for fill, op, head, sub in (
        (t["muted"], 0.13, "misses 15 min", "not a candidate at all"),
        (t["v2_light"], 0.40, "meets it nominally", "a deadline-only plan"),
        (t["v2"], 1.0, "survives the envelope", "the real candidates"),
    ):
        s.rect(lx, ly, 16, 16, fill, rx=3, opacity=op)
        s.text(lx + 24, ly + 8, head, 10, t["ink"], weight="600")
        s.text(lx + 24, ly + 21, sub, 9, t["muted"])
        ly += 44

    s.text(lx, ly + 6, "The envelope:", 10, t["ink"], weight="600")
    for i, line in enumerate((
            "· nominal under 15 min",
            f"· every rate {model.STRESS_RATE_SHORTFALL:.0%} low",
            "· that, minus 1 shard",
            f"  and {model.STRESS_WORKER_LOSS:.0%} of the fleet",
            "· still compute-bound",
            "  after losing a shard",
            "· all 12 streams used")):
        s.text(lx, ly + 22 + i * 13, line, 9, t["muted"])

    d = model.derive_shape()
    s.footer(
        f"{d['deadline_only_count']:,} of the shapes searched meet the deadline; only "
        f"{d['feasible_count']:,} meet the whole envelope. The cost band is flat to a few percent, "
        f"so the deadline alone leaves a dozen indistinguishable choices — the two "
        f"STRUCTURAL conditions are what actually decide it. The dark band runs along "
        f"W = 6S, where every instance is saturated and none contended.")
    return s.done()


# ===========================================================================
# FIGURE 6 — why buying workers stops working
#
# Answers the question the whole architecture turns on: if the run is too slow, why
# not just add nodes? Each curve is a fixed shard count. Every one of them flattens,
# and where it flattens has nothing to do with the fleet — it is the cluster's
# ingest ceiling. The horizontal tails are workers being paid for and idle.
# ===========================================================================
def fig_worker_ceiling(theme: str) -> str:
    t = THEMES[theme]
    W, H = 880, 470
    s = SVG(W, H, t,
            "Wall clock against fleet size for several shard counts. Each curve "
            "flattens at its database ingest ceiling, so adding workers past that "
            "point buys nothing.")

    s.text(40, 34, "Adding nodes stops helping, and the database decides when",
           16, t["ink"], weight="600")
    s.text(40, 55, "Every curve is a fixed number of PostgreSQL instances. The flat "
                   "tails are workers running and waiting.", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, py, pw, ph = 78, 90, W - 78 - 210, 236
    wmax, tmax = 120, 30 * 60.0

    def sx(w): return px + pw * w / wmax
    def sy(v): return py + ph * (1 - min(v, tmax) / tmax)

    for mins in range(0, 31, 5):
        y = sy(mins * 60)
        s.line(px, y, px + pw, y, t["grid"], 1)
        s.text(px - 10, y + 4, f"{mins}m", 9.5, t["muted"], anchor="end")
    for w in (0, 20, 40, 60, 80, 100, 120):
        s.text(sx(w), py + ph + 18, str(w), 9.5, t["muted"], anchor="middle")
    s.text(px + pw / 2, py + ph + 36, "map workers", 10.5, t["ink2"], anchor="middle")
    s.vtext(px - 46, py + ph / 2, "wall clock", 10.5, t["ink2"])

    # the deadline
    s.line(px, sy(model.TARGET_SECONDS), px + pw, sy(model.TARGET_SECONDS),
           t["accent"], 2, dash="6 4")
    s.text(px + 8, sy(model.TARGET_SECONDS) - 8,
           f"{model.TARGET_SECONDS / 60:.0f}-minute deadline", 10.5, t["accent"],
           weight="700")

    curves = [(4, 0.42), (6, 0.62), (model.SHARDS, 1.0), (14, 0.78)]
    for k, strength in curves:
        pts = [(w, v) for w in range(2, wmax + 1, 2)
               if (v := model.total_seconds(w, k)) <= tmax]
        if not pts:
            continue
        colour = t["v2"] if k == model.SHARDS else _mix(t["surface"], t["v2"], strength)
        s.path("M " + " L ".join(f"{sx(w):.1f} {sy(v):.1f}" for w, v in pts),
               colour, sw=3 if k == model.SHARDS else 1.8)
        # Label at the curve's right edge, where it has already flattened.
        vend = model.total_seconds(wmax, k)
        s.text(sx(wmax) + 8, sy(vend) + 4, f"{k} shards", 10,
               t["ink"] if k == model.SHARDS else t["muted"],
               weight="700" if k == model.SHARDS else "400")

    # Where each curve turns over: the last fleet size still compute-bound. Skipped
    # when that point is off-scale, so no dot is left floating without its curve.
    for k, _ in curves:
        knee = max((w for w in range(2, wmax + 1)
                    if model.bound_by(w, k) == "compute-bound"), default=2)
        vk = model.total_seconds(knee, k)
        if vk <= tmax:
            s.circle(sx(knee), sy(vk), 3.5, t["bad"])

    s.circle(sx(model.WORKERS), sy(model.total_seconds()), 6, t["accent"],
             stroke=t["surface"], sw=2)

    bx = px + pw + 76
    s.rect(bx, py, 124, 118, t["panel"], rx=6)
    s.text(bx + 12, py + 22, "deployed", 10, t["ink2"], weight="600")
    s.text(bx + 12, py + 42, f"{model.WORKERS} / {model.SHARDS}", 18, t["accent"],
           weight="700")
    s.text(bx + 12, py + 60, fmt_t(model.total_seconds()), 11, t["ink"])
    s.text(bx + 12, py + 80, "red dots mark", 9, t["muted"])
    s.text(bx + 12, py + 92, "where each curve", 9, t["muted"])
    s.text(bx + 12, py + 104, "goes I/O-bound", 9, t["muted"])

    s.footer(
        f"Four shards cannot meet the deadline with ANY fleet — that curve flattens at "
        f"{fmt_t(model.total_seconds(wmax, 4))}, above the line, and stays there. (Two "
        f"shards is off the top of this axis entirely, at "
        f"{fmt_t(model.total_seconds(wmax, 2))}.) This is why the answer to 'it is too slow' "
        f"is not always 'add nodes': past the ceiling every extra pod raycasts into a "
        f"queue. The {model.SHARDS}-shard curve is still bending at {model.WORKERS} workers, "
        f"which is what it means for the pipeline to be compute-bound — and why the fleet "
        f"was sized to sit there.")
    return s.done()


# ===========================================================================
# FIGURE 7 — cost against time, and the absence of a knee
#
# The honest version of a diminishing-returns chart. There is no knee: the curve is
# ~1/W, so marginal return decays smoothly and no amount of staring at it yields a
# natural stopping point. The deadline is what stops it. Saying so is the figure's
# whole content — a chart implying an inflection that is not there would be worse
# than no chart.
# ===========================================================================
def fig_cost_time(theme: str) -> str:
    t = THEMES[theme]
    W, H = 880, 460
    pf = [(c, w, k, v) for c, w, k, v in model.frontier() if c <= 1250]

    s = SVG(W, H, t,
            "Cost/time Pareto frontier. The curve decays smoothly with no knee, so "
            "the deadline and the stress envelope are what select a point on it.")

    s.text(40, 34, "There is no knee to find", 16, t["ink"], weight="600")
    s.text(40, 55, "Fastest run achievable at each hardware budget. Returns fall off "
                   "smoothly — so the deadline has to be the thing that stops you.",
           11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, py, pw, ph = 78, 90, W - 78 - 200, 232
    cmax, tmax = 1250, 40 * 60.0

    def sx(c): return px + pw * c / cmax
    def sy(v): return py + ph * (1 - min(v, tmax) / tmax)

    for mins in range(0, 41, 10):
        y = sy(mins * 60)
        s.line(px, y, px + pw, y, t["grid"], 1)
        s.text(px - 10, y + 4, f"{mins}m", 9.5, t["muted"], anchor="end")
    for c in (0, 250, 500, 750, 1000, 1250):
        s.text(sx(c), py + ph + 18, str(c), 9.5, t["muted"], anchor="middle")
    s.text(px + pw / 2, py + ph + 36, "total provisioned vCPU", 10.5, t["ink2"],
           anchor="middle")
    s.vtext(px - 46, py + ph / 2, "wall clock", 10.5, t["ink2"])

    # Everything above the deadline is not a choice at all.
    s.rect(px, py, pw, sy(model.TARGET_SECONDS) - py, t["bad"], opacity=0.06)
    s.line(px, sy(model.TARGET_SECONDS), px + pw, sy(model.TARGET_SECONDS),
           t["accent"], 2, dash="6 4")
    s.text(px + pw - 6, sy(model.TARGET_SECONDS) - 9,
           f"{model.TARGET_SECONDS / 60:.0f} min", 10.5, t["accent"], anchor="end",
           weight="700")

    # Only the on-scale part of the frontier: clamping would draw a flat line along
    # the top edge that looks like a real plateau and is not one.
    on = [(c, v) for c, _, _, v in pf if v <= tmax]
    s.path("M " + " L ".join(f"{sx(c):.1f} {sy(v):.1f}" for c, v in on), t["v2"], sw=2.5)

    # Marginal return at three budgets, to show it decaying rather than breaking.
    for target_c in (470, 780, 1140):
        near = min(pf, key=lambda r: abs(r[0] - target_c))
        i = pf.index(near)
        if i == 0:
            continue
        c0, _, _, v0 = pf[i - 1]
        c1, _, _, v1 = near
        rate = (v0 - v1) / max(1e-9, (c1 - c0) / 100)
        s.circle(sx(c1), sy(v1), 4, t["v2"], stroke=t["surface"], sw=1.5)
        # Labels hang BELOW the curve. Above it they land on the deadline line, which
        # is where the interesting part of the frontier happens to run.
        s.line(sx(c1), sy(v1) + 6, sx(c1), sy(v1) + 22, t["axis"], 1)
        s.text(sx(c1), sy(v1) + 36, f"{rate:.0f} s", 11, t["ink"], anchor="middle",
               weight="700")
        s.text(sx(c1), sy(v1) + 48, "per +100 vCPU", 8.5, t["muted"], anchor="middle")

    d = model.derive_shape()
    s.circle(sx(d["vcpu"]), sy(d["envelope"]["nominal"]), 6.5, t["accent"],
             stroke=t["surface"], sw=2)
    s.text(sx(d["vcpu"]) + 11, sy(d["envelope"]["nominal"]) - 6,
           f"{d['workers']}/{d['shards']}", 11, t["accent"], weight="700")

    nc, nw, ns, nt = model.cheapest_nominal()
    s.circle(sx(nc), sy(nt), 4.5, "none", stroke=t["bad"], sw=2)
    s.text(sx(nc) - 8, sy(nt) + 4, f"{nw}/{ns}", 9.5, t["bad"], anchor="end", weight="600")

    bx = px + pw + 26
    s.rect(bx, py, 152, 150, t["panel"], rx=6)
    s.text(bx + 12, py + 22, "asymptote", 10, t["ink2"], weight="600")
    s.text(bx + 12, py + 44, f"{model.FLEET_STARTUP_SECONDS + model.ANALYZE_SECONDS} s", 20,
           t["ink"], weight="700")
    for i, line in enumerate((
            "spin-up + ANALYZE.",
            "Neither shrinks with",
            "hardware, so no budget",
            "beats it.",
            "",
            f"Past ~{model.vcpu(103, 14)} vCPU an extra",
            f"100 buys less than the",
            f"{model.ANALYZE_SECONDS} s floor itself.")):
        s.text(bx + 12, py + 66 + i * 12, line, 9, t["muted"])

    s.footer(
        "The frontier is a STAIRCASE: workers climb at a fixed shard count until the "
        "shape goes I/O-bound, then a shard is added and workers resume. Every tread is "
        "compute-bound, which is the cluster doing its job. Marginal return halves "
        "roughly every +200 vCPU with no inflection anywhere — so 'diminishing returns' "
        "cannot pick a point on this curve, and did not. The deadline did.")
    return s.done()


# ===========================================================================
# FIGURE 8 — the stress envelope, and what it rejects
#
# Four bars per shape, one per condition. The naive shape clears the deadline and
# fails everything after it; the deployed shape clears all four. This is the figure
# that shows WHY the extra 160 vCPU is bought, which a nominal-only chart cannot.
# ===========================================================================
def fig_stress_envelope(theme: str) -> str:
    t = THEMES[theme]
    W, H = 880, 480
    nc, nw, ns, _ = model.cheapest_nominal()
    groups = [
        (f"{nw} workers / {ns} shards", nc, model.envelope(nw, ns),
         "cheapest that meets the deadline"),
        (f"{model.WORKERS} workers / {model.SHARDS} shards", model.vcpu(),
         model.envelope(model.WORKERS, model.SHARDS), "deployed"),
    ]
    conds = [("nominal", "every rate as benchmarked"),
             ("pessimistic", f"every rate {model.STRESS_RATE_SHORTFALL:.0%} below bench"),
             ("failure", "one shard + 10% of the fleet lost"),
             ("pessimistic_failure", "both at once")]

    s = SVG(W, H, t,
            "The four stress conditions for the cheapest deadline-meeting shape and "
            "for the deployed shape. The cheapest fails three of four.")

    s.text(40, 34, "Why the deployed shape costs more than the deadline requires",
           16, t["ink"], weight="600")
    s.text(40, 55, "Same deadline, four conditions. Meeting it on paper is not the "
                   "same as meeting it.", 11.5, t["muted"])
    s.text(W - 40, 34, "MODELLED", 9.5, t["muted"], anchor="end", weight="600")

    px, pw = 300, W - 300 - 92
    tmax = 19 * 60.0

    def sx(v): return px + pw * min(v, tmax) / tmax

    y = 92
    for title, cost, env, note in groups:
        s.text(40, y + 4, title, 12.5, t["ink"], weight="700")
        s.text(40, y + 20, f"{cost} vCPU · {note}", 10, t["muted"])
        y += 34
        for key, label in conds:
            v = env[key]
            over = v > model.TARGET_SECONDS
            s.rect(px, y - 9, sx(v) - px, 18, t["bad"] if over else t["v2"], rx=3,
                   opacity=1.0 if over else 0.88)
            s.text(px - 12, y + 4, label, 10, t["ink2"], anchor="end")
            s.text(sx(v) + 8, y + 4, fmt_t(v), 10,
                   t["bad"] if over else t["ink"], weight="700")
            if over:
                s.text(sx(v) + 52, y + 4, "OVER", 9, t["bad"], weight="700")
            y += 25
        y += 22

    # the deadline, drawn over the bars
    s.line(sx(model.TARGET_SECONDS), 118, sx(model.TARGET_SECONDS), y - 34,
           t["accent"], 2, dash="5 3")
    s.text(sx(model.TARGET_SECONDS), 112, f"{model.TARGET_SECONDS / 60:.0f} min",
           10.5, t["accent"], anchor="middle", weight="700")

    for mins in (0, 5, 10, 15):
        s.text(sx(mins * 60), y - 26, f"{mins}m", 9, t["muted"], anchor="middle")

    s.footer(
        f"The pessimistic row is not a failure scenario — it is the possibility that the "
        f"benchmarks are simply optimistic, which does not clear up. Failures happen in "
        f"that world too, so the bottom row is the one an SLO actually has to survive. "
        f"{model.vcpu() - nc} extra vCPU ({100 * (model.vcpu() / nc - 1):.0f}%) buys the "
        f"difference between passing one condition and passing all four.")
    return s.done()


# ===========================================================================
# FIGURE 9 — the measurement ladder
#
# Where the model's numbers come from. Two independent chains that have to meet:
# the fleet's production rate and the cluster's ingest rate. Sizing is the act of
# making the second exceed the first, and the figure is drawn so the gap between
# them — the ingest headroom — is the thing you see.
# ===========================================================================
def fig_bench_ladder(theme: str) -> str:
    t = THEMES[theme]
    W, H = 880, 450
    s = SVG(W, H, t,
            "How the per-worker and per-shard rates are built from individual "
            "benchmarks, and the headroom between the fleet's output and the "
            "cluster's ingest capacity.")

    s.text(40, 34, "The two rates the sizing turns on, and where they came from",
           16, t["ink"], weight="600")
    s.text(40, 55, "Two chains that have to meet: sizing is making the lower one exceed "
                   "the upper one. (B6-B9 are reduce-phase rates, not on this axis.)",
           11.5, t["muted"])
    s.text(W - 40, 34, "MEASURED", 9.5, t["muted"], anchor="end", weight="600")

    px, pw = 210, W - 210 - 210
    rmax = 24e6

    def sx(v): return px + pw * (v / rmax) ** 0.5   # sqrt: 73k and 21.6M on one axis

    chains = [
        ("THE FLEET PRODUCES", t["v2"], [
            ("B1", "v1, one thread, end to end", model.V1_ROW_RATE, "measured on v1's own run"),
            ("B2", f"x {model.BATCH_SPEEDUP} batched raycasts",
             model.V1_ROW_RATE * model.BATCH_SPEEDUP, "RaycastCommand.ScheduleBatch"),
            ("B3", f"x {model.LOCALITY_SPEEDUP} section-local BVH",
             model.WORKER_ROW_RATE, "one worker: 296k rows/s"),
            ("", f"x {model.WORKERS} workers",
             model.WORKERS * model.WORKER_ROW_RATE, "the fleet: 15.97M rows/s"),
        ]),
        ("THE CLUSTER ABSORBS", t["accent"], [
            ("B4", "one binary COPY stream", model.COPY_ROWS_PER_STREAM,
             "into a WAL-skipped relation"),
            ("B5", f"x {model.shard_max_streams()} streams per instance",
             model.shard_max_streams() * model.COPY_ROWS_PER_STREAM,
             f"one shard: 2.4M rows/s on {model.SHARD_VCPU} vCPU"),
            ("", f"x {model.SHARDS} shards", model.cluster_ingest_rate(),
             "the cluster: 21.6M rows/s"),
        ]),
    ]

    y = 96
    for title, colour, rows in chains:
        s.text(40, y, title, 10.5, t["ink"], weight="700")
        y += 20
        for bid, label, rate, note in rows:
            final = note.startswith("the ")
            s.rect(px, y - 8, sx(rate) - px, 16, colour, rx=3,
                   opacity=1.0 if final else 0.45)
            s.text(px - 12, y + 4, label, 10.5, t["ink"] if final else t["ink2"],
                   anchor="end", weight="700" if final else "400")
            if bid:
                s.text(44, y + 4, bid, 9, t["muted"], anchor="start", family=MONO)
            s.text(sx(rate) + 8, y + 4,
                   f"{rate / 1e6:.2f}M" if rate >= 1e6 else f"{rate / 1000:.0f}k",
                   10, t["ink"], weight="700" if final else "400")
            s.text(sx(rate) + 56, y + 4, note, 9, t["muted"])
            y += 23
        y += 16

    # The gap between the two chain endpoints IS the sizing margin.
    fleet = model.WORKERS * model.WORKER_ROW_RATE
    clus = model.cluster_ingest_rate()
    gy = y + 4
    s.line(sx(fleet), gy - 118, sx(fleet), gy, t["v2"], 1.2, dash="3 3")
    s.line(sx(clus), gy - 62, sx(clus), gy, t["accent"], 1.2, dash="3 3")
    s.line(sx(fleet), gy, sx(clus), gy, t["good"], 2)
    s.line(sx(fleet), gy - 4, sx(fleet), gy + 4, t["good"], 2)
    s.line(sx(clus), gy - 4, sx(clus), gy + 4, t["good"], 2)
    s.text(sx(clus) + 10, gy + 4, f"{model.io_headroom() * 100:+.0f}% headroom",
           10.5, t["good"], weight="700")

    s.footer(
        f"One shard sustains "
        f"{model.shard_max_streams() * model.COPY_ROWS_PER_STREAM / model.WORKER_ROW_RATE:.2f} "
        f"workers' output, so at {model.STREAMS_PER_WORKER} COPY streams each it feeds "
        f"{model.shard_max_streams() // model.STREAMS_PER_WORKER}. That single ratio is where "
        f"W = 6S comes from, and with it the whole deployment shape: {model.SHARDS} shards x "
        f"{model.shard_max_streams() // model.STREAMS_PER_WORKER} = {model.WORKERS} workers, "
        f"every instance saturated and none contended. The horizontal axis is sqrt-scaled "
        f"to hold 73k and 21.6M at once. python model.py --bench prints each method.")
    return s.done()


FIGURES = {
    "shard_scaling": fig_shard_scaling,
    "phase_breakdown": fig_phase_breakdown,
    "directional_cost": fig_directional_cost,
    "failure_timeline": fig_failure,
    "feasibility_map": fig_feasibility,
    "worker_ceiling": fig_worker_ceiling,
    "cost_time": fig_cost_time,
    "stress_envelope": fig_stress_envelope,
    "bench_ladder": fig_bench_ladder,
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
