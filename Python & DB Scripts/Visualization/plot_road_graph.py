"""
Renders the extracted road graph as a top-down wireframe (SVG + PNG + JPG).

Input is the road_graph.json emitted by RoadGraphExtractor. Useful for eyeballing the result
of the simplification passes — real streets should read as clean single lines, and leftover
hairballs at intersections mean the cycle-removal thresholds need tuning.
"""

import json
import os
import sys
import matplotlib.pyplot as plt

# =========================
# CONFIG
# =========================
INPUT_FILE = "road_graph.json"
OUTPUT_NAME = "road_graph"   # no extension

DRAW_NODES = False           # set True if you want points
NODE_SIZE = 2
EDGE_WIDTH = 0.5


# =========================
# LOAD JSON
# =========================
if not os.path.exists(INPUT_FILE):
    sys.exit(f"Error: {INPUT_FILE} not found. Copy it out of Assets/ after running "
             f"RoadGraphExtractor -> 'Generate Graph + Export JSON'.")

with open(INPUT_FILE, "r") as f:
    data = json.load(f)

vertices = data["vertices"]
edges = data["edges"]

# id -> position. Project onto the x/z plane: Unity y is the vertical axis and every graph
# node shares one normalized elevation, so it carries no information here.
pos = {}
for v in vertices:
    pos[v["id"]] = (v["x"], v["z"])


# =========================
# PLOT
# =========================
fig, ax = plt.subplots(figsize=(10, 10))

# draw edges. Accepts both key styles: {"a","b"} from older exports and {"from","to"} from
# the current RoadGraphExtractor output.
skipped = 0
for e in edges:
    u = e.get("a") if "a" in e else e.get("from")
    v = e.get("b") if "b" in e else e.get("to")
    if u not in pos or v not in pos:
        # An edge referencing a vertex that isn't in the file would raise KeyError and abort
        # the whole render; count and continue instead.
        skipped += 1
        continue
    x1, y1 = pos[u]
    x2, y2 = pos[v]
    ax.plot([x1, x2], [y1, y2], linewidth=EDGE_WIDTH)

if skipped:
    print(f"Warning: skipped {skipped} edge(s) referencing unknown vertex ids.")

# draw nodes (optional)
if DRAW_NODES:
    xs = [pos[v["id"]][0] for v in vertices]
    ys = [pos[v["id"]][1] for v in vertices]
    ax.scatter(xs, ys, s=NODE_SIZE)

# clean look
ax.set_aspect('equal')
ax.axis('off')

# =========================
# EXPORT
# =========================
plt.savefig(f"{OUTPUT_NAME}.svg", format="svg", bbox_inches='tight')
plt.savefig(f"{OUTPUT_NAME}.png", dpi=300, bbox_inches='tight')
plt.savefig(f"{OUTPUT_NAME}.jpg", dpi=300, bbox_inches='tight')

print("Exported SVG, PNG, JPG successfully!")