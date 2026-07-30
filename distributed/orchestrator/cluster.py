#!/usr/bin/env python3
"""
Cluster topology — sections, the Hilbert ordering, and the section -> shard map.

This module answers one question: given a city, a section size and k database
instances, WHICH instance owns WHICH piece of the city?

Run it to inspect the topology it derives:

    python cluster.py --show                 # sections, weights, shard assignment
    python cluster.py --show --shards 14     # what a different cluster looks like
    python cluster.py --dsn 3                # print shard 3's connection string

THE TWO REQUIREMENTS THAT PULL AGAINST EACH OTHER
-------------------------------------------------
1. WRITE BALANCE. During the map phase all ten instances must finish at about the
   same time. Manhattan is not uniform — midtown has several times the road
   density of the northern tip — so equal AREA per shard would mean wildly
   unequal ROWS per shard, and the slowest instance would set the makespan.

2. READ LOCALITY. After the load, a pedestrian route is a spatially local object:
   a 2 km walk touches a handful of adjacent sections. If adjacent sections live
   on different instances, every route query fans out across the whole cluster
   and pays the slowest one. We want a route to touch one or two shards.

Hashing section ids gives (1) and destroys (2). Cutting the city into ten
contiguous stripes gives (2) and destroys (1).

THE RESOLUTION: order the sections along a HILBERT CURVE, then cut that
one-dimensional sequence into k contiguous runs of equal weight.

A Hilbert curve visits every cell of a 2D grid such that consecutive positions
are always adjacent, and — the property that matters here — any contiguous run of
the curve maps to a compact, connected region of the plane. So a contiguous run
is spatially local by construction, while cutting the sequence by cumulative
WEIGHT rather than by length makes the runs equal in rows. Both requirements, no
compromise.

Cutting a weighted sequence into k contiguous runs minimising the heaviest run is
the classic linear-partition problem, and it has an exact solution (binary search
on the bound plus a greedy feasibility test) — so this is optimal, not a
heuristic. See balanced_runs().

THE SECTION ID FORMULA IS A CONTRACT
------------------------------------
Python (this file), SQL (db/01_cluster_topology.sql) and C#
(unity/Runtime/SectionGrid.cs) all compute section ids. They MUST agree
bit-for-bit, or a worker writes its rows into another section's partition. The
formula is therefore integer-only, stated once here, and pinned into
meo_runs.config so a re-plan cannot silently shift the grid under existing data.
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field

# Grid order for the Hilbert mapping: a 2^HILBERT_ORDER square covers the grid.
# 7 gives a 128x128 lattice — comfortably larger than Manhattan's 22x5 sections,
# with room for a city an order of magnitude bigger before it needs raising.
HILBERT_ORDER = 7
HILBERT_SIDE = 1 << HILBERT_ORDER

DEFAULT_SECTION_METERS = 1000.0


# ===========================================================================
# Section grid
# ===========================================================================
@dataclass(frozen=True)
class SectionGrid:
    """
    Maps Unity world coordinates to integer section ids.

    origin_x / origin_z are snapped DOWN to a multiple of `size`, so the grid
    lines fall on round world coordinates and a slightly different graph extent
    (a new city block, a re-extraction) does not renumber every section.
    """
    origin_x: float
    origin_z: float
    size: float = DEFAULT_SECTION_METERS
    cols: int = HILBERT_SIDE

    @staticmethod
    def from_bounds(min_x: float, min_z: float, max_x: float, max_z: float,
                    size: float = DEFAULT_SECTION_METERS) -> "SectionGrid":
        ox = math.floor(min_x / size) * size
        oz = math.floor(min_z / size) * size
        cols = max(1, int(math.ceil((max_x - ox) / size)))
        if cols > HILBERT_SIDE:
            raise ValueError(
                f"graph spans {cols} sections in X, above the {HILBERT_SIDE}-cell "
                f"Hilbert lattice. Raise HILBERT_ORDER (and the SQL constant) or "
                f"the section size.")
        return SectionGrid(origin_x=ox, origin_z=oz, size=size, cols=HILBERT_SIDE)

    # ---- The contract. Mirrored in SQL and C#. -----------------------------
    def col(self, x: float) -> int:
        return int(math.floor((x - self.origin_x) / self.size))

    def row(self, z: float) -> int:
        return int(math.floor((z - self.origin_z) / self.size))

    def section_id(self, x: float, z: float) -> int:
        """section_id = row * 128 + col. Integer-only, so all three languages agree."""
        return self.row(z) * self.cols + self.col(x)

    def col_of(self, section_id: int) -> int:
        return section_id % self.cols

    def row_of(self, section_id: int) -> int:
        return section_id // self.cols

    def centre(self, section_id: int) -> tuple[float, float]:
        c, r = self.col_of(section_id), self.row_of(section_id)
        return (self.origin_x + (c + 0.5) * self.size,
                self.origin_z + (r + 0.5) * self.size)

    def as_config(self) -> dict:
        """Frozen into meo_runs.config; workers refuse to start on a mismatch."""
        return {
            "section_origin_x": f"{self.origin_x:g}",
            "section_origin_z": f"{self.origin_z:g}",
            "section_meters": f"{self.size:g}",
            "section_cols": str(self.cols),
        }


# ===========================================================================
# Hilbert curve
# ===========================================================================
def hilbert_index(x: int, y: int, order: int = HILBERT_ORDER) -> int:
    """
    Position of cell (x, y) along a Hilbert curve of the given order.

    The standard iterative xy->d conversion. Walks from the coarsest quadrant to
    the finest, rotating and reflecting the local frame at each level so that the
    curve stays continuous across quadrant boundaries — which is precisely the
    property that makes a contiguous run of indices a connected region.
    """
    side = 1 << order
    if not (0 <= x < side and 0 <= y < side):
        raise ValueError(f"cell ({x},{y}) outside a {side}x{side} lattice")

    rx = ry = 0
    d = 0
    s = side >> 1
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        # Rotate the quadrant so the next level's frame is correct.
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


# ===========================================================================
# Balanced contiguous partition
# ===========================================================================
def balanced_runs(weights: list[int], k: int) -> list[int]:
    """
    Cuts a sequence into at most k contiguous runs minimising the heaviest run.
    Returns the run index for each position.

    Exact, not greedy-approximate. `feasible(C)` is monotone in C — if a bound C
    can be met with k runs then so can any larger bound — so binary searching C
    over [max(weights), sum(weights)] and greedily packing left to right finds the
    optimum. O(n log(sum)).

    A single element heavier than the bound is why the search starts at
    max(weights): no cut can split one section across two instances.
    """
    n = len(weights)
    if k >= n:
        return list(range(n))
    if k <= 1:
        return [0] * n

    def runs_needed(cap: int) -> int:
        runs, load = 1, 0
        for w in weights:
            if load + w > cap:
                runs += 1
                load = w
            else:
                load += w
        return runs

    lo, hi = max(weights), sum(weights)
    while lo < hi:
        mid = (lo + hi) // 2
        if runs_needed(mid) <= k:
            hi = mid
        else:
            lo = mid + 1

    # Re-pack at the optimal capacity to emit the actual assignment.
    cap = lo
    out, run, load = [], 0, 0
    for w in weights:
        if load + w > cap:
            run += 1
            load = w
        else:
            load += w
        out.append(run)
    return out


# ===========================================================================
# Topology
# ===========================================================================
@dataclass
class Section:
    section_id: int
    col: int
    row: int
    centre_x: float
    centre_z: float
    edges: int = 0
    samples: int = 0          # the weight that matters: rows this section produces
    hilbert: int = 0
    shard_index: int = -1


@dataclass
class Topology:
    grid: SectionGrid
    shard_count: int
    sections: list[Section] = field(default_factory=list)

    # ---- Derivation -------------------------------------------------------
    @staticmethod
    def build(grid: SectionGrid, shard_count: int,
              section_weights: dict[int, tuple[int, int]]) -> "Topology":
        """
        section_weights maps section_id -> (edge_count, sample_count). Only
        non-empty sections are given shards: an empty tile is not a unit of work
        and would otherwise dilute the balance.
        """
        secs = []
        for sid, (n_edges, n_samples) in section_weights.items():
            if n_samples <= 0:
                continue
            cx, cz = grid.centre(sid)
            secs.append(Section(
                section_id=sid,
                col=grid.col_of(sid), row=grid.row_of(sid),
                centre_x=cx, centre_z=cz,
                edges=n_edges, samples=n_samples,
                hilbert=hilbert_index(grid.col_of(sid), grid.row_of(sid)),
            ))

        if not secs:
            raise ValueError("no non-empty sections — is meo_sample_points populated?")

        secs.sort(key=lambda s: s.hilbert)
        for s, run in zip(secs, balanced_runs([s.samples for s in secs], shard_count)):
            s.shard_index = run

        return Topology(grid=grid, shard_count=shard_count, sections=secs)

    # ---- Queries ----------------------------------------------------------
    def shard_of(self, section_id: int) -> int:
        for s in self.sections:
            if s.section_id == section_id:
                return s.shard_index
        raise KeyError(f"section {section_id} is not in this topology")

    def shard_loads(self) -> list[int]:
        loads = [0] * self.shard_count
        for s in self.sections:
            loads[s.shard_index] += s.samples
        return loads

    def imbalance(self) -> float:
        """max/mean over shards. This is the number that sets the makespan."""
        loads = [l for l in self.shard_loads() if l > 0]
        if not loads:
            return 0.0
        return max(loads) / (sum(loads) / len(loads))

    def shards_used(self) -> int:
        return len({s.shard_index for s in self.sections})

    def contiguity(self) -> float:
        """
        Fraction of orthogonally-adjacent section pairs that land on the SAME
        shard. This is the read-locality metric: higher means a route crossing
        that boundary stays on one instance.

        A pure hash gives ~1/k (0.11 at the deployed k=9). Contiguous Hilbert runs
        give ~0.66, and it RISES as k falls — fewer, longer runs mean fewer
        boundaries. That is the read-side reason not to over-shard, and it is
        independent of the write-throughput argument that sets the lower bound.
        """
        by_cell = {(s.col, s.row): s.shard_index for s in self.sections}
        same = total = 0
        for (c, r), shard in by_cell.items():
            for dc, dr in ((1, 0), (0, 1)):
                other = by_cell.get((c + dc, r + dr))
                if other is None:
                    continue
                total += 1
                same += (other == shard)
        return same / total if total else 1.0


# ===========================================================================
# Connection strings
# ===========================================================================
@dataclass(frozen=True)
class ClusterEndpoints:
    """
    Where the instances live.

    The coordinator is reached THROUGH PgBouncer: the control plane is thousands
    of tiny transactions (claim, heartbeat, complete) from 54 clients, which is
    exactly what a transaction pooler is for.

    Data shards are reached DIRECTLY. A pooler in a sustained bulk COPY path would
    be a single-threaded proxy relaying ~700 MB/s — it would become the
    bottleneck the cluster exists to remove. And it is unnecessary: sharding
    already keeps each instance's backend count at ten.
    """
    coord_host: str = "pgbouncer"
    coord_port: int = 6432
    coord_db: str = "sunlit_coord"
    shard_host_template: str = "sunlit-shard-{i}.sunlit-shards"
    shard_port: int = 5432
    shard_db_template: str = "sunlit_shard_{i}"
    user: str = "admin"
    password: str = ""

    @staticmethod
    def from_environment() -> "ClusterEndpoints":
        e = os.environ.get
        return ClusterEndpoints(
            coord_host=e("SUNLIT_COORD_HOST", "pgbouncer"),
            coord_port=int(e("SUNLIT_COORD_PORT", "6432")),
            coord_db=e("SUNLIT_COORD_DB", "sunlit_coord"),
            shard_host_template=e("SUNLIT_SHARD_HOST_TEMPLATE",
                                  "sunlit-shard-{i}.sunlit-shards"),
            shard_port=int(e("SUNLIT_SHARD_PORT", "5432")),
            shard_db_template=e("SUNLIT_SHARD_DB_TEMPLATE", "sunlit_shard_{i}"),
            user=e("SUNLIT_DB_USER", "admin"),
            password=e("SUNLIT_DB_PASSWORD", ""),
        )

    def coordinator(self) -> dict:
        return dict(host=self.coord_host, port=self.coord_port,
                    dbname=self.coord_db, user=self.user, password=self.password)

    def shard(self, index: int) -> dict:
        return dict(host=self.shard_host_template.format(i=index),
                    port=self.shard_port,
                    dbname=self.shard_db_template.format(i=index),
                    user=self.user, password=self.password)

    def dsn(self, conf: dict) -> str:
        # Space-separated keyword form: psycopg2 and libpq both take it, and
        # unlike a URI it needs no percent-encoding of a password.
        parts = [f"{k}={v}" for k, v in conf.items() if v != ""]
        return " ".join(parts)


# ===========================================================================
# CLI
# ===========================================================================
def _synthetic_weights(grid: SectionGrid, cols: int, rows: int) -> dict:
    """
    Stand-in weights for --show without a database: a north-south island whose
    density peaks in the middle, which is close enough to Manhattan's shape to
    make the balance and contiguity metrics meaningful.
    """
    out = {}
    for r in range(rows):
        # Island narrows at both ends.
        width = max(1, int(cols * (0.45 + 0.55 * math.sin(math.pi * (r + 0.5) / rows))))
        for c in range(width):
            density = 0.5 + 1.5 * math.exp(-((r - rows * 0.45) ** 2) / (2 * (rows * 0.22) ** 2))
            sid = r * grid.cols + c
            out[sid] = (int(80 * density), int(4400 * density))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--show", action="store_true", help="derive and print a topology")
    p.add_argument("--shards", type=int, default=9,
                   help="default is the derived deployment shape; see model.py --derive")
    p.add_argument("--section-meters", type=float, default=DEFAULT_SECTION_METERS)
    p.add_argument("--dsn", type=int, metavar="SHARD",
                   help="print a shard's DSN (use -1 for the coordinator)")
    a = p.parse_args()

    if a.dsn is not None:
        ep = ClusterEndpoints.from_environment()
        conf = ep.coordinator() if a.dsn < 0 else ep.shard(a.dsn)
        # Never print the password.
        conf = {k: ("***" if k == "password" and v else v) for k, v in conf.items()}
        print(ep.dsn(conf))
        return 0

    if not a.show:
        p.print_help()
        return 0

    grid = SectionGrid.from_bounds(0.0, 0.0, 5000.0, 22000.0, a.section_meters)
    weights = _synthetic_weights(grid, cols=5, rows=22)
    topo = Topology.build(grid, a.shards, weights)

    print("=" * 74)
    print(f"  Topology — {a.shards} shards, {a.section_meters:g} m sections")
    print("  (synthetic weights; plan_tasks.py derives the real ones from PostGIS)")
    print("=" * 74)
    print(f"  sections (non-empty) : {len(topo.sections)}")
    print(f"  shards used          : {topo.shards_used()} / {a.shards}")
    print(f"  write imbalance      : {topo.imbalance():.3f}x max/mean")
    print(f"  read contiguity      : {topo.contiguity():.2f}  "
          f"(a hash would give ~{1 / a.shards:.2f})")

    print(f"\n  {'shard':>6} {'sections':>9} {'samples':>12} {'share':>7}")
    print("  " + "-" * 38)
    loads = topo.shard_loads()
    total = sum(loads)
    for i, load in enumerate(loads):
        n = sum(1 for s in topo.sections if s.shard_index == i)
        print(f"  {i:>6} {n:>9} {load:>12,} {100 * load / total:>6.1f}%")

    print("\n  Shard layout (row = north/south, col = east/west):")
    by_cell = {(s.col, s.row): s.shard_index for s in topo.sections}
    max_c = max(c for c, _ in by_cell)
    max_r = max(r for _, r in by_cell)
    for r in range(max_r, -1, -1):
        line = "".join(
            f"{by_cell[(c, r)]:>2}" if (c, r) in by_cell else " ." for c in range(max_c + 1))
        print(f"    {line}")
    print("\n  Each digit is the owning instance. Runs are contiguous because the")
    print("  Hilbert order makes them so — that is what keeps route queries local.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
