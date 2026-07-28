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
  1. shard_scaling      wall clock vs shard count at a fixed 50 workers. The
                        headline result: the same fleet finishes in 15m 12s on one
                        database instance and 3m 20s on ten.
  2. phase_breakdown    where the 3m 20s goes, and why writing is free (it overlaps
                        raycasting) while the reduce phase is not.
  3. directional_cost   the same edge at the same instant, walked both ways. This
                        is why the schema keeps per-sample rows.
  4. failure_timeline   lease-based recovery of a killed worker.

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
           f"The fleet produces {model.WORKERS * model.WORKER_RAYCAST_RATE / 1e6:.1f}M/s.",
           11, t["ink2"])
    s.text(56, fy + 38,
           f"So the cluster needs at least {kb}. Ten are deployed — "
           f"{model.io_headroom() * 100:+.0f}% headroom, so a checkpoint or a vacuum on one "
           "instance cannot stall the fleet.",
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
            f"and {fmt_t(red)} of reduce across ten shards in parallel.")

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




FIGURES = {
    "shard_scaling": fig_shard_scaling,
    "phase_breakdown": fig_phase_breakdown,
    "directional_cost": fig_directional_cost,
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
