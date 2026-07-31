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
  8. failure_timeline   lease-based recovery of a killed worker.

WHY THE ROWS ARE KEPT — the pair that carries the README's schema argument.

  9. row_anatomy        one row's 68 bytes against its one bit of payload, and the
                        multiplication that turns 365,133 sample points into
                        7,886,872,800 rows.
 10. directional_cost   the same edge at the same instant, walked both ways, drawn
                        as a space-time field in which one cell IS one row. The two
                        walks are different diagonals through it, so they read
                        different cells — which is why the schema keeps per-sample
                        rows. Recomputed from the self-test fixture (see FIXTURE).

V1 — the pipeline that defined the schema. Both replaced ASCII sketches, so both
carry something the sketch could not: where the time actually goes.

 11. v1_dataflow       three precomputed static inputs, one Unity loop, one product.
                       For the README.
 12. v1_phases         the same pipeline as a six-stage ladder, with the tool and the
                       cost on every arrow and the one-time setup bracketed off from
                       the run. For V1_PIPELINE.md.

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


def arrow(s: "SVG", x1, y1, x2, y2, colour, sw=1.4, head=5.5, dash=None):
    """A straight connector with a barbed head at (x2, y2)."""
    s.line(x1, y1, x2, y2, colour, sw, dash=dash, cap="round")
    ang = math.atan2(y2 - y1, x2 - x1)
    for off in (math.radians(148), math.radians(-148)):
        s.line(x2, y2, x2 + head * math.cos(ang + off), y2 + head * math.sin(ang + off),
               colour, sw, cap="round")


def wrap(text: str, size: float, width: float) -> list[str]:
    """
    Greedy wrap to a pixel width. SVG.footer() has its own copy for the full-width
    case; the annotation columns inside the plots are 160-340 px wide and every one
    of them overran the panel the first time it was positioned by eye.
    """
    budget = max(8, int(width / (size * 0.505)))       # measured for this font stack
    lines, cur = [], ""
    for word in text.split():
        if len(cur) + len(word) + 1 > budget:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


# ===========================================================================
# THE SELF-TEST FIXTURE, RECOMPUTED — the input to fig_directional_cost.
#
# distributed/db/tests/shard_selftest.sql builds two 400 m edges sampled every 2 m
# and fills window 4 with
#
#     is_sunlit := distance_from_start > 400.0 * (k / 59.0)
#
# for k = 0..59 — a shadow whose edge starts 135.6 m along the street at 16:00 and
# steps 400/59 = 6.8 m further right at every 3-minute timestep. Section 7 of that
# file then walks the edge both ways at 0.5 m/s and asserts the two directions
# disagree.
#
# _walk() below reimplements meo_edge_directional_cost() over that fixture — same
# snap, same segment weighting, same gaps-and-islands run length — so the figure is
# drawn from the computation rather than from transcribed labels. _MEASURED holds
# what the test printed, and the assertion at the bottom fails the build if the two
# ever part company.
#
# 0.5 m/s is the fixture's walking speed, not a realistic one: it makes a 400 m edge
# span five timesteps, so the effect is visible in a test that runs in two seconds.
# ===========================================================================
FIXTURE = dict(
    length_m=400.0,
    samples=201,
    spacing_m=2.0,
    speed_mps=0.5,
    entry_minute=960,            # 16:00
    window_first_minute=900,     # window 4 opens at 15:00
    window_steps=60,             # k = 0..59
)


def _snap_step(minute: float) -> int:
    """meo_snap_timestep(), in minutes of the day. floor(x+0.5) rather than round()
    because PostgreSQL rounds halves away from zero and Python rounds them to even."""
    k = int((minute - model.START_MINUTE) / model.STEP_MINUTE + 0.5)
    k = max(0, min(model.STEPS_PER_DAY - 1, k))
    return model.START_MINUTE + k * model.STEP_MINUTE


def _shadow_edge(snapped_minute: int) -> float:
    """Distance along the edge at which the fixture's shadow ends, at one timestep."""
    k = (snapped_minute - FIXTURE["window_first_minute"]) // model.STEP_MINUTE
    return FIXTURE["length_m"] * k / (FIXTURE["window_steps"] - 1)


SHADOW_STEP_M = FIXTURE["length_m"] / (FIXTURE["window_steps"] - 1)   # 6.78 m / timestep


def _walk(reverse: bool) -> dict:
    """meo_edge_directional_cost(), over the fixture, for one direction."""
    f = FIXTURE
    n, L, sp, v = f["samples"], f["length_m"], f["spacing_m"], f["speed_mps"]
    seq = []
    for i in range(n):
        pos = i * sp                                   # distance_from_start
        travelled = (L - pos) if reverse else pos      # distance the walker has covered
        minute = f["entry_minute"] + travelled / v / 60.0
        step = _snap_step(minute)
        seq.append(dict(travelled=travelled, pos=pos, step=step,
                        sunlit=pos > _shadow_edge(step)))
    seq.sort(key=lambda r: r["travelled"])

    per_sample_s = sp / v                              # each sample stands for its segment
    n_sun = sum(1 for r in seq if r["sunlit"])

    # Gaps and islands, as the SQL does it: consecutive samples sharing is_sunlit
    # are one run, measured end to end plus one spacing.
    longest, i = 0.0, 0
    while i < len(seq):
        j = i
        while j + 1 < len(seq) and seq[j + 1]["sunlit"] == seq[i]["sunlit"]:
            j += 1
        if seq[i]["sunlit"]:
            longest = max(longest, seq[j]["travelled"] - seq[i]["travelled"] + sp)
        i = j + 1

    return dict(
        seq=seq,
        sun_s=n_sun * per_sample_s,
        shade_s=(n - n_sun) * per_sample_s,
        sun_n=n_sun,
        pct=round(100.0 * n_sun / n, 2),
        entered=seq[0]["sunlit"],
        exited=seq[-1]["sunlit"],
        run_m=longest,
        steps=len({r["step"] for r in seq}),
        traverse_s=L / v,
    )


FWD, REV = _walk(False), _walk(True)

_MEASURED = {          # printed by shard_selftest.sql section 7
    "forward": dict(sun_s=504.0, shade_s=300.0, pct=62.69, run_m=252.0,
                    entered=False, exited=True, steps=5),
    "reverse": dict(sun_s=492.0, shade_s=312.0, pct=61.19, run_m=246.0,
                    entered=True, exited=False, steps=5),
}
for _name, _w in (("forward", FWD), ("reverse", REV)):
    for _k, _want in _MEASURED[_name].items():
        assert _w[_k] == _want, f"{_name}.{_k}: recomputed {_w[_k]}, test printed {_want}"

# What a per-edge sunlit_sum knows: the shadow frozen at the entry instant. Both
# directions get the same answer from it, and that answer is neither of the real two.
STATIC_EDGE_M = _shadow_edge(_snap_step(FIXTURE["entry_minute"]))         # 135.6 m
STATIC_SUN_N = sum(1 for i in range(FIXTURE["samples"])
                   if i * FIXTURE["spacing_m"] > STATIC_EDGE_M)           # 133 of 201
STATIC_SUN_S = STATIC_SUN_N * FIXTURE["spacing_m"] / FIXTURE["speed_mps"]  # 532 s


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
# FIGURE 9 — what one row is, and where 7.89 billion of them come from
#
# The README's claim "the row count IS the observation count" is a statement about
# an encoding, and prose makes it look like a tautology. The left half is the row
# to scale — 68 bytes of page against one bit of payload — and the right half is
# the multiplication, so both halves of the claim are visible at once.
# ===========================================================================

# PostgreSQL 16 heap layout for (UUID, TIMESTAMP, BOOLEAN, INTEGER, BIGINT), all
# NOT NULL: a 4 B line pointer and a 24 B tuple header, then the columns at their
# alignments — uuid is char-aligned so it sits at 24, timestamp needs 8 so it lands
# at 40, is_sunlit takes a whole byte at 48, and section_id's 4-byte alignment
# strands 3 bytes of padding. 64 B of tuple, 68 B of page. The measured heap in
# DB_CLUSTER.md is 68.3 B/row; the 0.3 is page slack.
ROW_LAYOUT = [
    ("page + tuple header", 28, "overhead"),
    ("sample_point_id + datetime", 24, "identity"),
    ("is_sunlit", 1, "payload"),
    ("alignment", 3, "pad"),
    ("section_id + task_id", 12, "bookkeeping"),
]
assert sum(b for _, b, _ in ROW_LAYOUT) == model.SAMPLE_ROW_BYTES

PACKED_RATIO = 225      # measured, DB_CLUSTER.md: BIT(360) per (sample point, date)


def fig_row_anatomy(theme: str) -> str:
    t = THEMES[theme]
    W, H = 860, 430
    rows = model.EXPOSURE_ROWS
    bits = model.SAMPLE_ROW_BYTES * 8

    s = SVG(W, H, t,
            f"Anatomy of one row: {model.SAMPLE_ROW_BYTES} bytes of page carrying a "
            f"single bit of measurement, 0.18% payload. And the multiplication behind "
            f"the row count: {model.SAMPLE_POINTS:,} sample points x "
            f"{model.STEPS_PER_DAY} timesteps a day x {model.DAYS} dates = {rows:,} "
            f"rows, which is also exactly {rows:,} observations.")

    s.text(40, 34, f"One row, one bit — and where {rows:,} of them come from",
           16, t["ink"], weight="600")
    s.text(40, 55, "The table is fully normalised, so the row count IS the observation "
                   "count. That identity is a property of this encoding, not an "
                   "inevitability.", 11.5, t["muted"])
    s.text(W - 40, 34, "SCHEMA · 03_shard_schema.sql", 9.5, t["muted"], anchor="end",
           weight="600")

    # ---- left: the row, to scale ------------------------------------------
    s.rect(40, 76, 398, 246, t["panel"], rx=6)
    s.text(56, 98, "WHAT ONE ROW IS", 10, t["ink2"], weight="700")

    cols = [("sample_point_id", "UUID", "'a1b2…'", 116),
            ("datetime", "TIMESTAMP", "'2026-06-15 15:00:00'", 152),
            ("is_sunlit", "BOOLEAN", "true", 86)]
    x = 56
    for name, typ, val, cw in cols:
        payload = name == "is_sunlit"
        s.rect(x, 108, cw, 36, t["surface"], rx=4,
               stroke=t["accent"] if payload else t["axis"], sw=1.5 if payload else 1)
        s.text(x + 8, 123, name, 9.5, t["accent"] if payload else t["ink"],
               weight="700" if payload else "600", family=MONO)
        s.text(x + 8, 136, typ, 8.5, t["muted"], family=MONO)
        s.text(x + 8, 159, val, 9.5, t["ink2"], family=MONO)
        x += cw + 6
    for j, ln in enumerate(wrap("The v1 contract, and the whole of it — plus section_id "
                                "and task_id, bookkeeping that the v1 view hides.",
                                9, 366)):
        s.text(56, 175 + j * 11, ln, 9, t["muted"])

    # the same row as bytes on the page
    s.text(56, 203, "The same row as bytes on the page", 11, t["ink"], weight="600")
    bx, bw, by, bh = 56, 366, 210, 26
    ppb = bw / model.SAMPLE_ROW_BYTES
    fills = {"overhead": t["v1"], "identity": t["v2"], "payload": t["accent"],
             "pad": t["grid"], "bookkeeping": t["v2_light"]}
    inks = {"overhead": t["on_v1"], "identity": t["on_v2"], "bookkeeping": t["ink"]}
    x = bx
    payload_cx = None
    for label, nbytes, kind in ROW_LAYOUT:
        w = nbytes * ppb
        s.rect(x, by, max(w - 0.8, 1.2), bh, fills[kind])
        if kind == "payload":
            payload_cx = x + w / 2
        elif w > 44:
            s.text(x + 6, by + 13, f"{nbytes} B", 10, inks[kind], weight="700")
            s.text(x + 6, by + 23, kind, 8.5, inks[kind], opacity=0.85)
        x += w
    s.rect(bx, by, bw, bh, "none", stroke=t["axis"], sw=1, rx=2)

    # The payload is 5 px wide at this scale, which is the point — so it gets a
    # leader down to a magnified byte rather than a label it has no room for. The
    # lit bit is the 5th of the 8 so that it lands under the leader.
    zy = 258
    s.line(payload_cx, by + bh, payload_cx, zy - 6, t["accent"], 1.2, dash="2 2")
    cell, lit = 11, 4
    zx = payload_cx - cell * lit
    for i in range(8):
        s.rect(zx + i * cell, zy, cell - 1.5, 14,
               t["accent"] if i == lit else t["surface"],
               stroke=None if i == lit else t["axis"], sw=1)
    s.text(zx - 8, zy + 11, "is_sunlit", 8.5, t["muted"], anchor="end", family=MONO)
    s.text(payload_cx, zy + 28, "1 bit of measurement", 9.5, t["accent"],
           anchor="middle", weight="700")
    s.text(56, zy + 48,
           f"{bits} bits stored per row · 1 measured · 0.18% payload", 11, t["ink"])
    s.text(56, zy + 63,
           f"At {rows:,} rows that is ~{model.RAW_SAMPLES_GB:.0f} GB of heap.",
           9.5, t["muted"])

    # ---- right: the multiplication ----------------------------------------
    s.rect(452, 76, 368, 246, t["panel"], rx=6)
    s.text(468, 98, "WHERE THE ROW COUNT COMES FROM", 10, t["ink2"], weight="700")

    gx, gw = 646, 158            # glyph gutter
    factors = [
        (f"{model.SAMPLE_POINTS:,}", "sample points, 2 m apart", "points"),
        (f"× {model.STEPS_PER_DAY}", "timesteps a day · 03:00-21:00, every 3 min", "steps"),
        (f"× {model.DAYS}", f"dates · 5 a month ({model.V1_DAYS} in v1)", "dates"),
    ]
    for i, (num, label, glyph) in enumerate(factors):
        y = 118 + i * 44
        s.text(468, y + 14, num, 15, t["ink"], weight="700", family=MONO)
        for j, ln in enumerate(wrap(label, 9, 172)):
            s.text(468, y + 28 + j * 11, ln, 9, t["muted"])
        cy = y + 12
        if glyph == "points":
            # a street, sampled: the dots are what the row count counts. They are
            # interpolated along the same vertex list the street is drawn from,
            # because eyeballing them off it left them floating beside the line.
            v = [(gx, cy + 8), (gx + gw / 3, cy - 7),
                 (gx + 2 * gw / 3, cy + 6), (gx + gw, cy - 5)]
            s.path("M " + " L ".join(f"{a:.1f} {b:.1f}" for a, b in v), t["axis"], sw=6)
            for k in range(19):
                fr = k / 18 * (len(v) - 1)
                i0 = min(int(fr), len(v) - 2)
                u = fr - i0
                s.circle(v[i0][0] + (v[i0 + 1][0] - v[i0][0]) * u,
                         v[i0][1] + (v[i0 + 1][1] - v[i0][1]) * u, 2.2, t["v2"])
        elif glyph == "steps":
            for k in range(30):
                tx = gx + gw * k / 29
                s.line(tx, cy - 8, tx, cy + 8, t["v2"], 1.6, opacity=0.85)
        else:
            for k in range(model.DAYS):
                s.rect(gx + (k % 20) * 8, cy - 11 + (k // 20) * 8, 6, 6, t["v2"], rx=1)

    s.line(468, 254, 804, 254, t["axis"], 1)
    s.text(468, 280, f"= {rows:,}", 20, t["v2"], weight="700", family=MONO)
    s.text(468, 298, "rows — and, identically, observations.", 10.5, t["ink"])
    s.text(468, 313, "No array. No column per timestep. Nothing packed.", 9.5, t["muted"])

    # ---- bottom: the two runs, and the encoding not taken -----------------
    s.rect(40, 336, 780, 74, t["panel"], rx=6)
    barx, barw = 138, 116
    for i, (tag, n, gb, colour) in enumerate((
        (f"v1 · {model.V1_DAYS} dates", model.V1_ROWS, model.V1_MEASURED_GB, t["v1"]),
        (f"v2 · {model.DAYS} dates", rows, model.RAW_SAMPLES_GB, t["v2"]),
    )):
        y = 352 + i * 24
        s.text(56, y + 11, tag, 9.5, t["ink2"], weight="600")
        s.rect(barx, y + 1, barw * n / rows, 12, colour, rx=2)
        s.text(barx + barw + 10, y + 11, f"{n:,} rows · ~{gb:.0f} GB", 9.5, t["ink"],
               family=MONO)

    s.line(460, 346, 460, 400, t["axis"], 1)
    s.text(476, 358, "THE COST, KNOWINGLY ACCEPTED", 9, t["ink2"], weight="700")
    note = (f"Let the bit's position carry the timestamp and the same {rows / 1e9:.2f} "
            f"billion observations fit in {model.RAW_SAMPLES_GB / PACKED_RATIO:.1f} GB "
            f"— {PACKED_RATIO}× smaller, and lossless. It is not adopted because v1's "
            f"three columns are the contract; DB_CLUSTER.md measures what that costs.")
    for j, ln in enumerate(wrap(note, 9.5, 328)):
        s.text(476, 373 + j * 12, ln, 9.5, t["muted"])
    return s.done()


# ===========================================================================
# FIGURE 10 — why the schema keeps per-sample rows
#
# Drawn from the fixture recomputation above, which is checked against what
# distributed/db/tests/shard_selftest.sql printed — so this figure is a picture of
# a passing test rather than an illustration of an idea.
#
# The space-time field is the load-bearing part. Prose has to assert that the two
# walks "sample different (sample, time) pairs"; a plot of position against time
# shows it, because one cell of the field is one row of the table and the two walks
# are visibly different diagonals through it.
# ===========================================================================
def fig_directional_cost(theme: str) -> str:
    t = THEMES[theme]
    f = FIXTURE
    L, T = f["length_m"], FWD["traverse_s"]

    # The closing paragraph is wrapped before the canvas is sized, so editing its
    # wording cannot push it off the bottom of the figure.
    W = 860
    CY = 436
    closing = wrap(
        f"It is also the frozen answer, {STATIC_SUN_S:.0f} s: the moving shadow costs "
        f"the forward walker {STATIC_SUN_S - FWD['sun_s']:.0f} s of that and the reverse "
        f"walker {STATIC_SUN_S - REV['sun_s']:.0f} s. The "
        f"{FWD['sun_s'] - REV['sun_s']:.0f} s between them exists only in the ordered, "
        f"per-sample series — which is why v2 keeps v1's {model.EXPOSURE_ROWS:,} sample "
        f"rows rather than {model.EDGE_ROWS:,} per-edge sums.",
        11, W - 80 - 32)
    H = CY + 44 + 16 * len(closing)

    s = SVG(W, H, t,
            "A space-time field for one 400 metre street: distance along the street "
            "across, time upwards, and one cell for every row of meo_exposure_samples. "
            "The shadow's edge steps 6.8 metres right at every 3-minute timestep, so "
            "the two walks — opposite diagonals through the same field — cross it in "
            f"different places: {FWD['sun_s']:.0f} seconds of sun one way against "
            f"{REV['sun_s']:.0f} the other, and {FWD['run_m']:.0f} against "
            f"{REV['run_m']:.0f} metres of continuous exposure. A per-edge sum reports "
            f"{STATIC_SUN_S:.0f} seconds for both.")

    s.text(40, 34, "A walk is a diagonal cut through the table", 16, t["ink"],
           weight="600")
    s.text(40, 55, "One cell = one row of meo_exposure_samples (2 m × 3 min). Walking "
                   "east and walking west read different cells.", 11.5, t["muted"])
    s.text(W - 40, 34, "MEASURED · shard_selftest.sql", 9.5, t["muted"], anchor="end",
           weight="600")

    # ---- the space-time field ---------------------------------------------
    PX0, PX1, PY0, PY1 = 92, 636, 92, 300

    def X(m):
        return PX0 + (PX1 - PX0) * m / L

    def Y(sec):
        return PY1 - (PY1 - PY0) * sec / T

    # Which timestep each second of the traverse snaps to. Walked rather than
    # derived: the first and last bands are half-bands because meo_snap_timestep()
    # rounds to the nearest step, and that is exactly why timesteps_spanned is 5
    # for a walk that takes four steps' worth of time.
    bands = []
    for sec in range(int(T) + 1):
        step = _snap_step(f["entry_minute"] + sec / 60.0)
        if bands and bands[-1][0] == step:
            bands[-1][2] = sec
        else:
            bands.append([step, sec, sec])

    for step, lo, hi in bands:
        y0, y1 = Y(min(hi + 1, T)), Y(lo)
        edge = _shadow_edge(step)
        s.rect(PX0, y0, X(edge) - PX0, y1 - y0, t["v2_light"], opacity=0.55)
        s.rect(X(edge), y0, PX1 - X(edge), y1 - y0, t["accent"], opacity=0.30)
        s.line(PX0, y0, PX1, y0, t["surface"], 1, opacity=0.6)
        s.text(PX0 - 10, (y0 + y1) / 2 + 3.5,
               f"{step // 60}:{step % 60:02d}", 9, t["muted"], anchor="end")

    # 20 m gridlines: a hint of the cell structure. One cell is 2 m wide — 2.7 px
    # here — so drawing all 201 of them is a grey wash, not a grid.
    for m in range(20, int(L), 20):
        s.line(X(m), PY0, X(m), PY1, t["surface"], 0.6, opacity=0.35)
    s.rect(PX0, PY0, PX1 - PX0, PY1 - PY0, "none", stroke=t["axis"], sw=1)

    for m in (0, 100, 200, 300, 400):
        s.line(X(m), PY1, X(m), PY1 + 4, t["axis"], 1)
        s.text(X(m), PY1 + 17, f"{m}", 9.5, t["muted"], anchor="middle")
    s.text((PX0 + PX1) / 2, PY1 + 33, "distance along the 400 m edge (m)", 10, t["ink2"],
           anchor="middle")
    s.vtext(PY0 - 46, (PY0 + PY1) / 2, "timestep", 10, t["ink2"])

    # What sunlit_sum sees: the shadow frozen at the entry instant.
    s.line(X(STATIC_EDGE_M), PY0, X(STATIC_EDGE_M), PY1, t["ink2"], 1.4, dash="4 3")

    # ---- the two walks ----------------------------------------------------
    for w, reverse, dash in ((FWD, False, None), (REV, True, "7 4")):
        def pos(sec):
            return (L - f["speed_mps"] * sec) if reverse else f["speed_mps"] * sec

        # The break is where the walk crosses the shadow's edge — taken from the
        # sample series, so the line and the second counts agree exactly.
        t_break = w["sun_s"] if w["entered"] else w["shade_s"]
        for a, b in ((0.0, t_break), (t_break, T)):
            d = f"M {X(pos(a)):.1f} {Y(a):.1f} L {X(pos(b)):.1f} {Y(b):.1f}"
            s.path(d, t["surface"], sw=5.5, dash=dash)
            s.path(d, t["ink"], sw=2.4, dash=dash)
        s.circle(X(pos(t_break)), Y(t_break), 4.2, t["ink"], stroke=t["surface"], sw=2)
        # Both callouts go up-and-right of their crossing: up-left of the reverse
        # crossing is the reverse line itself, and the text sat on top of it.
        s.text(X(pos(t_break)) + 12, Y(t_break) + (-8 if reverse else 14),
               f"crosses at {pos(t_break):.0f} m", 9.5, t["ink"], weight="600")

        # Which end each walk starts from, on a chip so it is readable over the
        # field it sits on.
        s.circle(X(pos(0)), Y(0), 3.2, t["ink"])
        chw = 50
        chx = (X(pos(0)) - 8 - chw) if reverse else (X(pos(0)) + 8)
        s.rect(chx, PY1 - 20, chw, 14, t["surface"], rx=3, opacity=0.88)
        s.text(chx + chw / 2, PY1 - 9.5, "reverse" if reverse else "forward", 9,
               t["ink2"], anchor="middle", weight="600")

    # ---- legend and the point of the dashed line --------------------------
    ax = 660
    s.text(ax, PY0 + 8, "the field, one timestep per band", 9, t["ink2"], weight="600")
    s.rect(ax, PY0 + 16, 30, 10, t["accent"], opacity=0.30)
    s.text(ax + 36, PY0 + 25, "sun", 9.5, t["muted"])
    s.rect(ax + 70, PY0 + 16, 30, 10, t["v2_light"], opacity=0.55)
    s.text(ax + 106, PY0 + 25, "shade", 9.5, t["muted"])

    for i, (lbl, dash, secs) in enumerate((
            ("forward", None, FWD["sun_s"]), ("reverse", "7 4", REV["sun_s"]))):
        y = PY0 + 50 + i * 22
        s.line(ax, y, ax + 26, y, t["ink"], 2.4, dash=dash)
        s.text(ax + 34, y + 3.5, f"{lbl} — {secs:.0f} s of sun", 9.5, t["ink"])

    ky = PY0 + 98
    s.line(ax + 12, ky - 8, ax + 12, ky + 8, t["ink2"], 1.4, dash="4 3")
    s.text(ax + 24, ky + 3.5, "the shadow, frozen at 16:00", 9.5, t["ink2"])
    for j, ln in enumerate(wrap(
            f"Its edge steps {SHADOW_STEP_M:.1f} m right every 3 min. Freeze it and "
            f"both directions read {STATIC_SUN_S:.0f} s — which is exactly what a "
            f"per-edge sunlit_sum reports.", 9.5, 160)):
        s.text(ax, PY0 + 126 + j * 12, ln, 9.5, t["muted"])

    # ---- what each walker experiences, in order ---------------------------
    s.text(40, 352, "The same field, read along each diagonal — what each walker "
                    "experiences, in order", 11, t["ink"], weight="600")
    sx, sw_ = 150, 550
    cw = sw_ / f["samples"]
    for i, (w, lbl) in enumerate(((FWD, "forward"), (REV, "reverse"))):
        y = 360 + i * 26
        s.text(sx - 12, y + 13, lbl, 10.5, t["ink"], anchor="end", weight="600")
        for j, r in enumerate(w["seq"]):
            s.rect(sx + j * cw, y, cw + 0.4, 18,
                   t["accent"] if r["sunlit"] else t["v2_light"],
                   opacity=0.95 if r["sunlit"] else 0.85)
        s.rect(sx, y, sw_, 18, "none", stroke=t["axis"], sw=1, rx=2)

        # where a frozen shadow would have put this direction's boundary
        static_m = (L - STATIC_EDGE_M) if lbl == "reverse" else STATIC_EDGE_M
        gx = sx + sw_ * static_m / L
        s.line(gx, y - 2, gx, y + 20, t["ink"], 1.4, dash="4 3")

        s.text(sx + sw_ + 12, y + 8, f"{w['sun_s']:.0f} s sun", 10.5, t["accent"],
               weight="700")
        s.text(sx + sw_ + 12, y + 19, f"{w['run_m']:.0f} m longest run", 9, t["muted"])
    s.text(40, 424, "Each strip runs from its walker's entry. Dashed: where a frozen "
                    "shadow would put the boundary — the real one has moved by the "
                    "time the walker reaches it.", 9, t["muted"])

    # ---- the point --------------------------------------------------------
    s.rect(40, CY, W - 80, 26 + 16 * len(closing), t["panel"], rx=6)
    s.text(56, CY + 18,
           f"Both directions cross the same {f['samples']} samples, so the per-edge "
           f"sunlit_sum — {STATIC_SUN_N} of {f['samples']} at 16:00 — is identical for "
           f"the two.", 11, t["ink"])
    for j, ln in enumerate(closing):
        s.text(56, CY + 36 + j * 16, ln, 11, t["muted"])
    return s.done()

# ===========================================================================
# FIGURE 11 — v1 in one pass, for the README
#
# Three static inputs, one loop, one product. The point the ASCII sketch this
# replaced could not make: the geometry, the ephemeris and the canopy shade are all
# precomputed, so the 6 hours is spent entirely inside the middle box.
# ===========================================================================
V1_EDGE_ROWS = model.EDGES * model.STEPS_PER_DAY * model.V1_DAYS      # 28,944,000
V1_WALL = (f"{int(model.V1_SECONDS // 3600)} h "
           f"{int(model.V1_SECONDS % 3600 // 60):02d} min")
MINUTES_PER_YEAR = 365 * 1440                                        # 525,600


def fig_v1_dataflow(theme: str) -> str:
    t = THEMES[theme]
    W = 860
    IN_X, IN_W = 40, 226
    OR_X, OR_W = 322, 244
    OU_X, OU_W = 622, 198
    card_h, gap = 92, 12
    OR_Y, OR_H = 96, 3 * card_h + 2 * gap      # the oracle spans all three inputs
    FY = OR_Y + OR_H + 20                      # footer panel
    H = FY + 50 + 18

    s = SVG(W, H, t,
            f"v1's dataflow: three precomputed static inputs — the road graph and its "
            f"{model.SAMPLE_POINTS:,} sample points, {MINUTES_PER_YEAR:,} minute-"
            f"resolution solar positions, and {model.TREES:,} tree canopies joined in 2D "
            f"— all feed one Unity loop that sweeps time and raycasts, writing "
            f"{model.V1_ROWS:,} sample rows in {V1_WALL} on one thread, plus a derived "
            f"per-edge table of {V1_EDGE_ROWS:,} rows.")

    s.text(40, 34, "v1 in one pass: three static inputs, one oracle, one product",
           16, t["ink"], weight="600")
    s.text(40, 55, "Everything on the left is computed once. All six hours are spent "
                   "inside the middle box.", 11.5, t["muted"])
    s.text(W - 40, 34, f"MEASURED · one desktop, {V1_WALL}", 9.5, t["muted"],
           anchor="end", weight="600")

    # ---- the three static inputs -------------------------------------------
    # The tool line is a list: at 8.5 px mono, two script names on one line run off
    # the card, and the card is as narrow as it is on purpose.
    inputs = [
        ("GEOMETRY · once", "road_graph.json → PostGIS",
         [f"{model.WAYPOINTS:,} waypoints · {model.EDGES:,} edges",
          f"{model.SAMPLE_POINTS:,} sample points at 2 m"],
         ["RoadGraphExtractor.cs", "  → db_pipeline_initializer.py"]),
        ("SUN · once per year", "pvlib ephemeris",
         [f"{MINUTES_PER_YEAR:,} minute positions",
          "indexed in local standard time, never local time"],
         ["generate_solar_positions.py"]),
        ("TREES · once", f"{model.TREES:,} canopies",
         ["time-invariant, so a 2D PostGIS spatial join",
          "— canopy geometry never enters a ray"],
         ["process_tree_data.py"]),
    ]
    for i, (tag, title, details, tools) in enumerate(inputs):
        y = 96 + i * (card_h + gap)
        mid = y + card_h / 2
        s.rect(IN_X, y, IN_W, card_h, t["panel"], rx=6)
        s.rect(IN_X, y, 3, card_h, t["v2"], rx=1.5)
        s.text(IN_X + 14, y + 16, tag, 8.5, t["muted"], weight="700")
        s.text(IN_X + 14, y + 33, title, 11, t["ink"], weight="600")
        for j, d in enumerate(details):
            s.text(IN_X + 14, y + 48 + j * 12, d, 9, t["ink2"])
        for j, tool in enumerate(tools):
            s.text(IN_X + 14, y + 76 + j * 11, tool, 8.5, t["muted"], family=MONO)
        arrow(s, IN_X + IN_W + 6, mid, OR_X - 6, mid, t["muted"], 1.4)

    # ---- the oracle --------------------------------------------------------
    s.rect(OR_X, OR_Y, OR_W, OR_H, t["panel"], rx=6, stroke=t["v2"], sw=1.5)
    cx = OR_X + 16
    s.text(cx, OR_Y + 20, "UNITY AS A GEOMETRIC ORACLE", 9, t["v2"], weight="700")
    for j, ln in enumerate(wrap("Nothing renders. The only thing wanted from it is "
                                "Physics.Raycast against a BVH over the city's mesh "
                                "colliders.", 9, OR_W - 32)):
        s.text(cx, OR_Y + 36 + j * 11, ln, 9, t["ink2"])

    loop = [
        f"for date in {model.V1_DAYS} dates:",
        f"  for step in {model.STEPS_PER_DAY} timesteps:",
        f"    if sun_altitude > {model.SUN_ANGLE_THRESHOLD:.0f}°:",
        "      raycast every sample point",
        "    else: shadowed, no ray cast",
        "    binary COPY the window",
    ]
    ly = OR_Y + 76
    s.rect(cx - 6, ly - 12, OR_W - 20, 11 * len(loop) + 14, t["surface"], rx=4)
    for j, ln in enumerate(loop):
        s.text(cx, ly + j * 11, ln, 8.5,
               t["accent"] if "raycast" in ln else t["ink2"], family=MONO,
               weight="700" if "raycast" in ln else "400")

    gy = ly + 11 * len(loop) + 22
    s.text(cx, gy, f"{model.V1_RAYCASTS:,} raycasts", 9.5, t["ink"], weight="600")
    frac = model.V1_RAYCASTS / model.V1_ROWS
    bw = OR_W - 32
    s.rect(cx, gy + 8, bw * frac, 10, t["accent"], rx=2)
    s.rect(cx + bw * frac, gy + 8, bw * (1 - frac), 10, t["v2_light"], rx=2)
    for j, ln in enumerate(wrap(
            f"{100 * frac:.0f}% of timesteps are above the horizon guard; the other "
            f"{100 * (1 - frac):.0f}% are declared shadowed without a ray.",
            8.5, OR_W - 32)):
        s.text(cx, gy + 32 + j * 11, ln, 8.5, t["muted"])
    # What it cost, anchored to the bottom of the box rather than floated under the
    # bar — it is the summary, and a rule separates it from what the loop does.
    sy = OR_Y + OR_H - 52
    s.line(cx, sy - 14, OR_X + OR_W - 16, sy - 14, t["axis"], 1)
    s.text(cx, sy + 8, f"{V1_WALL} · one thread", 12, t["ink"], weight="700")
    s.text(cx, sy + 24, f"{model.V1_ROW_RATE:,.0f} rows/s · ~250 MB RAM, flat", 9,
           t["ink2"])

    # ---- what comes out ----------------------------------------------------
    # Both cards size themselves from their wrapped notes; the product's note ran
    # past the card's rounded corner when the height was fixed.
    a_note = wrap("Written straight out of the loop by binary COPY. One row per (sample "
                  f"point, timestep), carrying one bit — {model.V1_MEASURED_GB:.0f} GB "
                  "with two indexes maintained inline.", 9, OU_W - 28)
    a_y, a_h = 96, 108 + 11 * len(a_note)
    s.rect(OU_X, a_y, OU_W, a_h, t["panel"], rx=6, stroke=t["accent"], sw=1.5)
    s.rect(OU_X + 14, a_y + 12, 74, 15, t["accent"], rx=7.5)
    s.text(OU_X + 51, a_y + 23, "THE PRODUCT", 8.5, t["on_v2"], anchor="middle",
           weight="700")
    s.text(OU_X + 14, a_y + 46, "meo_exposure_samples", 10.5, t["ink"], weight="700",
           family=MONO)
    s.text(OU_X + 14, a_y + 68, f"{model.V1_ROWS:,}", 16, t["v2"], weight="700",
           family=MONO)
    s.text(OU_X + 14, a_y + 82, "rows", 9, t["muted"])
    for j, ln in enumerate(a_note):
        s.text(OU_X + 14, a_y + 100 + j * 11, ln, 9, t["ink2"])

    b_note = wrap("Derived afterwards by a SQL rollup, and regenerable from the samples "
                  "at any time.", 9, OU_W - 28)
    b_y, b_h = a_y + a_h + 18, 62 + 11 * len(b_note)
    s.rect(OU_X, b_y, OU_W, b_h, t["panel"], rx=6)
    s.text(OU_X + 14, b_y + 22, "meo_exposure_edges", 10.5, t["ink2"], weight="700",
           family=MONO)
    s.text(OU_X + 14, b_y + 42, f"{V1_EDGE_ROWS:,}", 13, t["v1"], weight="700",
           family=MONO)
    s.text(OU_X + 14, b_y + 54, "rows", 9, t["muted"])
    for j, ln in enumerate(b_note):
        s.text(OU_X + 14, b_y + 68 + j * 11, ln, 9, t["muted"])

    # One split connector, so it reads as one loop producing both. The two branches
    # are labelled inside the cards, not on the elbows — there are 22 px of gap here
    # and a label put in it landed on top of the product card.
    jx = OR_X + OR_W + 26
    s.line(OR_X + OR_W + 6, OR_Y + OR_H / 2, jx, OR_Y + OR_H / 2, t["muted"], 1.4)
    s.line(jx, a_y + a_h / 2, jx, b_y + b_h / 2, t["muted"], 1.4)
    arrow(s, jx, a_y + a_h / 2, OU_X - 6, a_y + a_h / 2, t["muted"], 1.4)
    arrow(s, jx, b_y + b_h / 2, OU_X - 6, b_y + b_h / 2, t["muted"], 1.4)

    # ---- footer ------------------------------------------------------------
    fy = FY
    s.rect(40, fy, W - 80, 50, t["panel"], rx=6)
    s.text(56, fy + 19,
           f"The ephemeris and the {model.TREES / 1e6:.2f} million canopies are both "
           f"time-invariant, so both are precomputed and joined — neither is ever "
           f"raycast.", 11, t["ink2"])
    s.text(56, fy + 36,
           "And the loop streams one timestep at a time, which is why peak RAM is flat "
           "at ~250 MB whether the run covers one day or a year.", 11, t["muted"])
    return s.done()


# ===========================================================================
# FIGURE 12 — the six phases, for V1_PIPELINE.md
#
# Same ladder the ASCII diagram drew, plus the two things it could not: which
# transition is the expensive one, and why each artifact exists. Row heights are
# computed from the wrapped text so editing a note cannot overlap the next row.
# ===========================================================================
V1_FLOW = [
    dict(box=["city mesh", "OSM buildings + terrain · ~1 GB Unity project"],
         note="No road network in it. The street surface is the absence of buildings, "
              "which is a shape, not a graph."),
    dict(phase=1, tool="RoadGraphExtractor.cs", when="Unity Editor · once",
         box=["road_graph.json",
              f"{model.WAYPOINTS:,} vertices · {model.EDGES:,} edges"],
         note="Rasterise, dilate, BFS distance transform, keep the ridge line. Only "
              "degree-1 and degree-2 nodes are ever removed, so junctions survive by "
              "construction."),
    dict(phase=2, tool="db_pipeline_initializer.py", when="once",
         box=["meo_waypoints · meo_edges · meo_trees",
              f"in PostGIS · {model.TREES:,} tree canopies"],
         note="Six tables. This is the schema, and v2 does not change it."),
    dict(phase=3, tool='ShadowAwarePathFinder — "Export Sample Points"', when="minutes",
         box=["meo_sample_points",
              f"{model.SAMPLE_POINTS:,} points at 2 m spacing"],
         note="sequence_index and distance_from_start make an edge an ordered series "
              "with a direction — what meo_edge_directional_cost() is built on."),
    dict(phase=4, tool="generate_solar_positions.py · process_tree_data.py",
         when="once per year",
         box=["sun_pos_2026.bin",
              f"{MINUTES_PER_YEAR:,} minute positions, local standard time",
              "meo_sample_points.tree_value · meo_edges.total_tree_value"],
         note="Both are time-invariant, so both are precomputed. Canopy shade becomes a "
              "2D spatial join rather than geometry in the raycast path."),
    dict(phase=5, tool='ShadowAwarePathFinder — "Export Exposure"',
         when=f"{V1_WALL} — the run", run=True,
         box=[f"meo_exposure_samples — {model.V1_ROWS:,} rows",
              f"meo_exposure_edges — {V1_EDGE_ROWS:,} rows · derived"],
         note=f"{model.V1_MEASURED_GB:.0f} GB with two indexes maintained inline. "
              f"{model.V1_RAYCASTS:,} raycasts at {model.V1_ROW_RATE:,.0f} rows/s on one "
              f"thread. Peak RAM ~250 MB, flat."),
]


def fig_v1_phases(theme: str) -> str:
    t = THEMES[theme]
    W = 860
    BOX_X, BOX_W = 96, 452
    NOTE_X, NOTE_W = 572, 248
    ARROW_H, TOP = 38, 84

    # Lay the ladder out first: every row is as tall as the taller of its two
    # columns, so a longer note pushes the next stage down instead of colliding.
    rows, y = [], TOP
    for st in V1_FLOW:
        notes = wrap(st["note"], 9, NOTE_W)
        if "phase" in st:
            y += ARROW_H
        box_h = 22 + 15 * len(st["box"])
        h = max(box_h, 10 + 11 * len(notes))
        rows.append(dict(st=st, y=y, box_h=box_h, h=h, notes=notes))
        y += h + 6

    foot = wrap(f"Four of the five phases run once and are measured in minutes. The fifth "
                f"is the simulation: {model.V1_RAYCASTS:,} raycasts against the city's "
                f"BVH, {V1_WALL} on one thread. v2 changes only that last arrow — same "
                f"inputs, same schema, same rows, on {model.WORKERS} workers and "
                f"{model.SHARDS} database instances instead of one desktop.",
                11, W - 80 - 32)
    FY = y + 22
    H = FY + 26 + 16 * len(foot)

    s = SVG(W, H, t,
            "v1's six phases as a ladder: the city mesh becomes a road graph, the graph "
            "becomes PostGIS geometry and 365,133 sample points, a year of solar "
            "positions and the static tree shade are precomputed, and only the last "
            f"transition — {V1_WALL} of sweeping and raycasting — produces the "
            f"{model.V1_ROWS:,} sample rows. Every earlier step runs once.")

    s.text(40, 34, "The six phases, and which one costs six hours", 16, t["ink"],
           weight="600")
    s.text(40, 55, "Boxes are artifacts. Arrows are the phases that produce them, "
                   "labelled with the tool that runs and what it costs.", 11.5,
           t["muted"])
    s.text(W - 40, 34, "V1_PIPELINE.md", 9.5, t["muted"], anchor="end", weight="600")

    # setup / run brackets, the grouping the ASCII ladder could not show
    setup_top, setup_bot = rows[0]["y"], rows[-2]["y"] + rows[-2]["h"]
    run_top, run_bot = rows[-1]["y"] - ARROW_H, rows[-1]["y"] + rows[-1]["h"]
    for top, bot, label, colour in ((setup_top, setup_bot, "ONE-TIME SETUP", t["v1"]),
                                    (run_top, run_bot, "THE RUN", t["accent"])):
        s.rect(56, top, 4, bot - top, colour, rx=2)
        s.vtext(48, (top + bot) / 2, label, 8.5, colour, weight="700")

    for i, r in enumerate(rows):
        st, y = r["st"], r["y"]
        run = st.get("run", False)

        if "phase" in st:
            ax = BOX_X + 26
            colour = t["accent"] if run else t["axis"]
            arrow(s, ax, y - ARROW_H + 6, ax, y - 5, colour, 2.4 if run else 1.4, 6.5)
            s.text(ax + 16, y - ARROW_H + 18, f"phase {st['phase']}", 9,
                   t["accent"] if run else t["ink2"], weight="700")
            s.text(ax + 66, y - ARROW_H + 18, st["tool"], 9,
                   t["ink"] if run else t["ink2"], family=MONO)
            s.text(BOX_X + BOX_W, y - ARROW_H + 18, st["when"], 9.5,
                   t["accent"] if run else t["muted"], anchor="end",
                   weight="700" if run else "400")

        s.rect(BOX_X, y, BOX_W, r["box_h"], t["panel"], rx=6,
               stroke=t["accent"] if run else None, sw=1.5)
        s.rect(BOX_X, y, 3, r["box_h"], t["accent"] if run else t["v2"], rx=1.5)
        s.circle(BOX_X + 26, y + 20, 11, t["accent"] if run else t["v2"])
        s.text(BOX_X + 26, y + 24, str(i), 11, t["on_v2"], anchor="middle",
               weight="700")
        for j, ln in enumerate(st["box"]):
            s.text(BOX_X + 48, y + 24 + j * 15, ln, 11 if j == 0 else 9.5,
                   t["ink"] if j == 0 else t["ink2"],
                   weight="700" if j == 0 else "400",
                   family=MONO if j == 0 or "meo_" in ln or ".bin" in ln else None)
        if run:
            s.rect(BOX_X + BOX_W - 88, y + 10, 78, 15, t["accent"], rx=7.5)
            s.text(BOX_X + BOX_W - 49, y + 21, "THE PRODUCT", 8.5, t["on_v2"],
                   anchor="middle", weight="700")
        for j, ln in enumerate(r["notes"]):
            s.text(NOTE_X, y + 18 + j * 11, ln, 9, t["muted"])

    s.rect(40, FY, W - 80, 26 + 16 * len(foot), t["panel"], rx=6)
    for j, ln in enumerate(foot):
        s.text(56, FY + 20 + j * 16, ln, 11, t["ink2"] if j == 0 else t["muted"])
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
    "row_anatomy": fig_row_anatomy,
    "directional_cost": fig_directional_cost,
    "v1_dataflow": fig_v1_dataflow,
    "v1_phases": fig_v1_phases,
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
