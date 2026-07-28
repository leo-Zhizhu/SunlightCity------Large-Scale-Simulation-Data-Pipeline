#!/usr/bin/env python3
"""
Generates the month x hour sun-exposure heatmap used in the project README.

Reads sun_exposure_data.json (produced by plot_annual_exposure.py) and writes a pair of
self-contained SVGs — one stepped for a light surface, one for a dark surface — so the README
can serve the right one via <picture media="(prefers-color-scheme: dark)">.

Pure stdlib on purpose: the README assets must be regenerable without matplotlib/numpy.

Encoding choices:
  * Sunlit percentage is a magnitude, so it gets a SEQUENTIAL single-hue ramp (blue,
    light -> dark on light surface, dark -> light on dark surface). Never a rainbow.
  * Cells with exactly 0% are drawn in neutral gray, not as the lowest blue step. Zero here
    means "sun below the horizon" (no daylight to measure), which is categorically different
    from "daylight, but fully shadowed". Collapsing the two into one ramp would imply a
    continuum that isn't there.

Usage:
    python make_readme_heatmap.py [path/to/sun_exposure_data.json] [output_dir]
"""

import json
import os
import sys

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Hour window to render. The simulation covers 03:00-21:00, but the outer hours are night in
# every month, so the chart trims to a band that still shows empty margin on both sides.
HOUR_MIN, HOUR_MAX = 5, 20

# --- Sequential blue ramp (validated reference palette, steps 100-700) ----------------------
# Upper bound of each bin -> hex.
#
# Every cell carries a printed value, so each step is chosen so that ONE ink reaches >= 4.5:1
# against it. Ramp step 450 (#2a78d6) is deliberately skipped: it sits exactly at the
# black/white crossover (4.42 vs 4.46) where neither ink is comfortably legible.
LIGHT_BINS = [
    (15,  "#cde2fb"), (30,  "#9ec5f4"), (40,  "#6da7ec"), (50,  "#3987e5"),
    (60,  "#256abf"), (70,  "#1c5cab"), (80,  "#184f95"), (101, "#0d366b"),
]
# Dark surface: same hue family, ramp reversed so magnitude still reads as "more presence"
# against a dark background. Also skips step 450 for the same reason.
DARK_BINS = [
    (15,  "#184f95"), (30,  "#256abf"), (40,  "#3987e5"), (50,  "#5598e7"),
    (60,  "#86b6ef"), (70,  "#9ec5f4"), (80,  "#b7d3f6"), (101, "#cde2fb"),
]

DARK_INK = "#0b0b0b"
LIGHT_INK = "#ffffff"

# Cell-label ink, one entry per bin, picked for contrast against that bin's own fill rather
# than by a single threshold. The ramps run in opposite directions, so the ink does too: on the
# light surface the fills darken (ink turns white at the top end); on the dark surface they
# lighten (ink turns black almost immediately). Verified >= 4.5:1 for every pair.
LIGHT_INKS = [DARK_INK] * 4 + [LIGHT_INK] * 4
DARK_INKS = [LIGHT_INK] * 2 + [DARK_INK] * 6

THEMES = {
    "light": {
        "bins":       LIGHT_BINS,
        "inks":       LIGHT_INKS,
        "surface":    "#fcfcfb",
        "text":       "#0b0b0b",
        "muted":      "#898781",
        "zero_fill":  "#f0efec",   # neutral gray = sun below horizon, not "0% of daylight"
        "zero_text":  "#898781",
    },
    "dark": {
        "bins":       DARK_BINS,
        "inks":       DARK_INKS,
        "surface":    "#1a1a19",
        "text":       "#ffffff",
        "muted":      "#898781",
        "zero_fill":  "#282826",
        "zero_text":  "#898781",
    },
}

# Geometry
CELL_W, CELL_H = 46, 34
PAD_L, PAD_T = 54, 66
PAD_R, PAD_B = 18, 76
GAP = 2  # surface gap between fills, so adjacent cells never bleed together


def bin_index(value, bins):
    for i, (upper, _) in enumerate(bins):
        if value < upper:
            return i
    return len(bins) - 1


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_svg(matrix, theme_name):
    t = THEMES[theme_name]
    bins = t["bins"]
    hours = list(range(HOUR_MIN, HOUR_MAX + 1))

    grid_w = len(hours) * CELL_W
    grid_h = len(MONTHS) * CELL_H
    width = PAD_L + grid_w + PAD_R
    height = PAD_T + grid_h + PAD_B

    o = []
    o.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Heatmap of sunlit street percentage by month and hour of day. Exposure '
        f'peaks at 90 percent in July at 11:00; December\'s best hour reaches only 51 percent.">'
    )
    o.append(f'<rect width="{width}" height="{height}" fill="{t["surface"]}"/>')

    # Single quotes around the multi-word family: these strings are interpolated into
    # double-quoted SVG attributes, and nested double quotes would terminate the attribute
    # and produce an unparseable document.
    fam = "system-ui,-apple-system,'Segoe UI',sans-serif"

    # Title + subtitle. A single-measure chart needs no legend box for identity, but a
    # sequential ramp does need a scale — added at the bottom.
    o.append(
        f'<text x="{PAD_L}" y="26" font-family="{fam}" font-size="15" font-weight="600" '
        f'fill="{t["text"]}">Sunlit street surface, by month and hour</text>'
    )
    o.append(
        f'<text x="{PAD_L}" y="45" font-family="{fam}" font-size="11.5" '
        f'fill="{t["muted"]}">Share of 365,133 sample points in direct sun &#183; Manhattan &#183; '
        f'3-minute simulation, sampled on the hour</text>'
    )

    # Hour axis (top)
    for c, h in enumerate(hours):
        x = PAD_L + c * CELL_W + CELL_W / 2
        o.append(
            f'<text x="{x:.1f}" y="{PAD_T - 8}" font-family="{fam}" font-size="10.5" '
            f'fill="{t["muted"]}" text-anchor="middle" '
            f'style="font-variant-numeric:tabular-nums">{h:02d}</text>'
        )

    # Cells
    for r, month in enumerate(MONTHS):
        y = PAD_T + r * CELL_H
        o.append(
            f'<text x="{PAD_L - 12}" y="{y + CELL_H / 2 + 3.5:.1f}" font-family="{fam}" '
            f'font-size="11.5" fill="{t["text"]}" text-anchor="end">{month}</text>'
        )
        for c, h in enumerate(hours):
            x = PAD_L + c * CELL_W
            val = matrix[r].get(h, 0.0)

            if val <= 0.0:
                fill, ink, label = t["zero_fill"], t["zero_text"], "·"
            else:
                bi = bin_index(val, bins)
                fill = bins[bi][1]
                ink = t["inks"][bi]
                label = f"{val:.0f}"

            o.append(
                f'<rect x="{x + GAP / 2:.1f}" y="{y + GAP / 2:.1f}" '
                f'width="{CELL_W - GAP}" height="{CELL_H - GAP}" rx="4" fill="{fill}"/>'
            )
            o.append(
                f'<text x="{x + CELL_W / 2:.1f}" y="{y + CELL_H / 2 + 3.5:.1f}" '
                f'font-family="{fam}" font-size="10.5" fill="{ink}" text-anchor="middle" '
                f'style="font-variant-numeric:tabular-nums">{label}</text>'
            )

    # Sequential scale legend
    ly = PAD_T + grid_h + 26
    o.append(
        f'<text x="{PAD_L}" y="{ly + 11}" font-family="{fam}" font-size="10.5" '
        f'fill="{t["muted"]}">% sunlit</text>'
    )
    sx = PAD_L + 56
    sw = 40
    o.append(f'<rect x="{sx}" y="{ly}" width="{sw}" height="14" rx="3" fill="{t["zero_fill"]}"/>')
    o.append(
        f'<text x="{sx + sw / 2:.1f}" y="{ly + 28}" font-family="{fam}" font-size="9.5" '
        f'fill="{t["muted"]}" text-anchor="middle">night</text>'
    )
    sx += sw + 10
    lo = 0
    for i, (upper, hexv) in enumerate(bins):
        o.append(f'<rect x="{sx}" y="{ly}" width="{sw}" height="14" rx="3" fill="{hexv}"/>')
        o.append(
            f'<text x="{sx:.1f}" y="{ly + 28}" font-family="{fam}" font-size="9.5" '
            f'fill="{t["muted"]}" text-anchor="middle" '
            f'style="font-variant-numeric:tabular-nums">{lo}</text>'
        )
        lo = upper
        sx += sw + GAP
    o.append(
        f'<text x="{sx - GAP:.1f}" y="{ly + 28}" font-family="{fam}" font-size="9.5" '
        f'fill="{t["muted"]}" text-anchor="middle" '
        f'style="font-variant-numeric:tabular-nums">100</text>'
    )

    o.append('</svg>')
    return "\n".join(o)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "sun_exposure_data.json"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    if not os.path.exists(src):
        sys.exit(f"Error: {src} not found. Run plot_annual_exposure.py first.")

    with open(src) as f:
        data = json.load(f)

    # matrix[monthIndex][hour] = percentage
    matrix = []
    for m in range(1, 13):
        md = data.get(str(m), {})
        matrix.append({int(h): v["percentage"] for h, v in md.items()})

    os.makedirs(out_dir, exist_ok=True)
    for theme in ("light", "dark"):
        path = os.path.join(out_dir, f"exposure_heatmap_{theme}.svg")
        with open(path, "w") as f:
            f.write(build_svg(matrix, theme))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
