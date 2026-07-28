#!/usr/bin/env python3
"""
Capacity model — the single source of truth for the pipeline's sizing.

Every performance figure in the README, in docs/ and in the generated charts is
derived from this module, so they cannot drift apart. Run it to print the sizing
calculation and the benchmark tables:

    python model.py                    # full report + shard sweep
    python model.py --balance          # minimum shard count that keeps up
    python model.py --sweep            # shard-count sensitivity
    python model.py --workers 100      # re-derive for a different fleet

WHY THIS IS EXECUTABLE AND NOT A SPREADSHEET
--------------------------------------------
The central sizing question — "how many PostgreSQL instances does a 50-worker
fleet need?" — has an exact answer, not a guess. The fleet produces rows at a
known rate; one COPY stream ingests at a known rate; the cluster must match.
Writing that as arithmetic makes the sizing auditable and lets it be re-derived
for a different fleet size, a different city, or different hardware.

THE ONE CONSTRAINT THAT SHAPES EVERYTHING
-----------------------------------------
The schema is fixed: one row per (sample point, timestamp), exactly as v1 wrote
it. That is 1.58 billion rows, not the 29 million a per-edge sum would be. The
downstream router traverses an edge as an ORDERED, DIRECTIONAL sequence of sample
points — walking east through a colonnade is not the same exposure as walking
west through it — so the per-sample series is the product, not an intermediate.

Everything below follows from refusing to discard it: the sample rate sets the
ingest requirement, the ingest requirement sets the shard count, and the shard
count sets the reduce time.
"""

from __future__ import annotations

import argparse
import math

# ===========================================================================
# SCENE — Manhattan road graph and sampling
# ===========================================================================
SAMPLE_POINTS = 365_133      # 2 m spacing along every edge
WAYPOINTS     = 4_168
EDGES         = 6_700
TREES         = 1_280_954

# ===========================================================================
# SIMULATION WINDOW
# ===========================================================================
START_MINUTE  = 180          # 03:00 — earliest sunrise of the year, with margin
END_MINUTE    = 1260         # 21:00 — latest sunset of the year, with margin
STEP_MINUTE   = 3
STEPS_PER_DAY = (END_MINUTE - START_MINUTE) // STEP_MINUTE   # 360
DAYS          = 12           # one representative day per month

# The product. Retained at full sample resolution — see the module docstring.
EXPOSURE_ROWS = SAMPLE_POINTS * STEPS_PER_DAY * DAYS         # 1.577e9

# Derived convenience index: one row per (edge, timestamp), computed shard-locally
# in the reduce phase. Serves "how sunlit is this edge right now" in one lookup;
# the directional queries go to the sample rows.
EDGE_ROWS = EDGES * STEPS_PER_DAY * DAYS                     # 2.894e7

# ===========================================================================
# STORAGE
#
# Heap width, derived rather than guessed, because it is what turns a row rate
# into a byte rate:
#
#   24 B tuple header
#   16 B sample_point_id  uuid
#    8 B datetime         timestamp
#    1 B is_sunlit        boolean
#    4 B section_id       int4      (partition routing + provenance)
#    8 B task_id          int8      (provenance; makes a task's output removable)
#   ---
#   64 B MAXALIGN'd, + 4 B line pointer = 68 B per row on the page
#
# v1 measured 110 GB on disk for `meo_exposure_samples` after a full annual run,
# with two btree indexes maintained inline. v2 stores the same 1.58 billion rows
# in 100 GB and builds NO index on the sample table at all: partition pruning
# down to a (section, 3 h window) leaf of ~261k rows makes one redundant, and not
# building it removes both the load-time maintenance and the index heap.
#
# Sizes are binary GB (GiB), matching what pg_size_pretty reports.
# ===========================================================================
SAMPLE_ROW_BYTES = 68
RAW_SAMPLES_GB   = EXPOSURE_ROWS * SAMPLE_ROW_BYTES / 1024 ** 3   # ~100 GB
EDGE_AGG_GB      = EDGE_ROWS * SAMPLE_ROW_BYTES / 1024 ** 3       # ~1.8 GB heap
EDGE_INDEX_GB    = EDGE_ROWS * 48 / 1024 ** 3                     # covering index
V1_MEASURED_GB   = 110.0     # v1, same rows + 2 indexes maintained inline

# ===========================================================================
# V1 BASELINE — one desktop, main-thread Physics.Raycast, one PostgreSQL
# ===========================================================================
V1_SECONDS      = 6.0 * 3600
V1_RAYCAST_RATE = EXPOSURE_ROWS / V1_SECONDS                 # ~73k/s

# ===========================================================================
# V2 PER-WORKER RAYCAST RATE
#
# Two independent multipliers on the v1 single-thread rate:
#
#   BATCH_SPEEDUP — RaycastCommand.ScheduleBatch dispatches a whole timestep's
#     rays across the job system's worker threads instead of calling
#     Physics.Raycast serially on the main thread. On an 8 vCPU pod this yields
#     3.0x, not 8x: the main thread still schedules, completes and folds results,
#     and BVH traversal saturates memory bandwidth before it saturates ALUs.
#
#   LOCALITY_SPEEDUP — a task's rays all originate inside one 1 km section, so
#     they traverse the same region of the BVH over and over. The working set
#     (~9 km2 of colliders, the section plus its shadow halo) stays resident in
#     L3 and the page cache, where v1's city-wide sweep missed constantly.
#     This is why sectioning is a throughput decision and not only a data-mapping
#     one.
# ===========================================================================
BATCH_SPEEDUP       = 3.0
LOCALITY_SPEEDUP    = 1.35
WORKER_RAYCAST_RATE = V1_RAYCAST_RATE * BATCH_SPEEDUP * LOCALITY_SPEEDUP  # ~296k/s

# ===========================================================================
# DATABASE INGEST
#
# One PostgreSQL backend running BINARY COPY into a freshly created, WAL-skipped
# relation sustains ~200k of these rows/s. Binary rather than CSV matters here:
# it removes the server-side text parse and the client-side per-row string
# allocation, and puts ~30 B on the wire instead of ~52 B.
#
# That figure is per CONNECTION — COPY is single-threaded server-side — so an
# instance scales with concurrent streams provided they do not contend on one
# relation's extension lock. Which they cannot, because every task COPYs into a
# relation it created itself (see STREAMS_PER_WORKER).
# ===========================================================================
COPY_ROWS_PER_STREAM = 200_000
# Each worker holds two COPY connections to its shard and alternates between
# them, so the next timestep's raycasting overlaps the previous window's flush.
STREAMS_PER_WORKER   = 2

# Per-instance ceiling. A COPY backend is one busy CPU, so an instance cannot run
# more productive streams than it has cores, minus what the WAL writer,
# checkpointer, background writer and OS need. Without this cap the model would
# happily conclude that one instance can absorb the whole fleet, which is exactly
# the mistake the DB cluster exists to correct.
SHARD_STREAM_RESERVE = 4
def shard_max_streams(vcpu: int = None) -> int:
    return max(1, (vcpu if vcpu is not None else SHARD_VCPU) - SHARD_STREAM_RESERVE)

# ===========================================================================
# REDUCE — per shard, shards run in parallel
#
# Thin by construction. The samples are already in their final partitions, so
# there is no shuffle and nothing to move. What remains:
#   1. derive the per-edge rollup (a shard-local GROUP BY, because a section owns
#      whole edges — never half of one)
#   2. index the rollup (2.9M rows/shard, not 158M)
#   3. ANALYZE, run with vacuumdb --jobs so the leaf partitions go in parallel
#
# COPY FREEZE during the load means there is no freeze-vacuum to pay later
# either: the tuples are already visible to everyone.
# ===========================================================================
EDGE_ROLLUP_ROWS_PER_S = 12_000_000   # parallel aggregate, 8 workers, page-cache resident
INDEX_BUILD_ROWS_PER_S = 600_000      # B-tree, 8 parallel maint. workers, 16 GB maintenance_work_mem
ANALYZE_SECONDS        = 30           # 576 leaves per shard via vacuumdb --analyze --jobs 8

# ===========================================================================
# FLEET SPIN-UP
#
# Counted, not waved away, because at a 3-minute runtime it is 23% of wall clock.
# Warm image cache + engine boot + scene load + whole-city BVH warm.
# ===========================================================================
FLEET_STARTUP_SECONDS = 45

# ===========================================================================
# DEPLOYMENT SHAPE
# ===========================================================================
WORKERS        = 50
SHARDS         = 10           # data instances; + 1 coordinator, see cluster.py
SECTION_METERS = 1000
TIME_WINDOWS   = 6            # 6 x 3 h spans 03:00-21:00
SECTIONS       = 84           # non-empty 1 km tiles over the Manhattan graph

# Shadow halo. A building of height H casts a shadow up to H/tan(theta)
# horizontally at sun elevation theta. Below SUN_ANGLE_THRESHOLD the worker
# declares shadow without raycasting at all, so that threshold bounds the worst
# case EXACTLY rather than approximately.
#
# This is the correctness argument for per-section tasks: nothing outside
# section + halo can affect a sample inside the section, so the sections are
# genuinely independent units of work. It is also the working-set bound the
# LOCALITY_SPEEDUP above rests on.
MAX_BUILDING_M      = 200.0
SUN_ANGLE_THRESHOLD = 5.0
# Within one 3 h window the sun's azimuth sweeps a bounded arc, so the halo is a
# sector rather than a full annulus. 45 deg covers the widest window at the
# summer solstice.
WINDOW_AZIMUTH_ARC_DEG = 45.0

# Per-pod resources
WORKER_VCPU, WORKER_GB = 8,  16
SHARD_VCPU,  SHARD_GB  = 16, 128
COORD_VCPU,  COORD_GB  = 8,  32
POOLER_VCPU, POOLER_GB = 2,  1
POOLERS                = 2


# ---------------------------------------------------------------------------
# Geometry of a task
# ---------------------------------------------------------------------------
def halo_meters() -> float:
    """Horizontal reach of the longest possible shadow. An exact bound."""
    return MAX_BUILDING_M / math.tan(math.radians(SUN_ANGLE_THRESHOLD))


def working_set_km2() -> float:
    """
    Colliders a task's rays can actually touch: the Minkowski sum of the section
    square with the halo sector, bounded by its enclosing rectangle.

    Compared against the whole-city mesh, this is the ratio that drives
    LOCALITY_SPEEDUP.
    """
    r = halo_meters() / 1000.0
    s = SECTION_METERS / 1000.0
    half_arc = math.radians(WINDOW_AZIMUTH_ARC_DEG) / 2.0
    return (s + 2.0 * r * math.sin(half_arc)) * (s + r)


def annulus_km2() -> float:
    """The same bound if the halo had to cover every azimuth — for comparison."""
    return (SECTION_METERS / 1000.0 + 2.0 * halo_meters() / 1000.0) ** 2


def steps_per_window() -> int:
    return STEPS_PER_DAY // TIME_WINDOWS


def tasks() -> int:
    return SECTIONS * DAYS * TIME_WINDOWS


def section_window_pairs() -> int:
    """Distinct collider working sets. What affinity dispatch keeps warm."""
    return SECTIONS * TIME_WINDOWS


def rows_per_task() -> float:
    return EXPOSURE_ROWS / tasks()


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------
def streams_per_shard(workers: int = WORKERS, shards: int = SHARDS) -> int:
    """
    Concurrent COPY streams arriving at one shard. Every worker writes only to
    the shard owning its current section, so this is (workers per shard) x
    (streams per worker), capped by what the instance's cores can actually run.

    Note it is small by construction — 10 at the deployed shape — which is why
    the bulk path needs no connection pooler. Pooling exists to multiplex many
    clients onto few backends; here there are already few.
    """
    offered = max(1, workers // max(1, shards)) * STREAMS_PER_WORKER
    return min(shard_max_streams(), offered)


def raycast_seconds(workers: int = WORKERS) -> float:
    return EXPOSURE_ROWS / (workers * WORKER_RAYCAST_RATE)


def cluster_ingest_rate(shards: int = SHARDS, workers: int = WORKERS) -> float:
    return shards * streams_per_shard(workers, shards) * COPY_ROWS_PER_STREAM


def write_seconds(shards: int = SHARDS, workers: int = WORKERS) -> float:
    return EXPOSURE_ROWS / cluster_ingest_rate(shards, workers)


def reduce_seconds(shards: int = SHARDS) -> float:
    return (EXPOSURE_ROWS / shards) / EDGE_ROLLUP_ROWS_PER_S \
         + (EDGE_ROWS / shards) / INDEX_BUILD_ROWS_PER_S \
         + ANALYZE_SECONDS


def map_seconds(workers: int = WORKERS, shards: int = SHARDS) -> float:
    """
    A worker streams rows as it computes them, so writing overlaps raycasting and
    the map phase costs max() rather than the sum. Whichever side is larger is
    the binding constraint; the shard count is chosen to keep it the compute side.
    """
    return max(raycast_seconds(workers), write_seconds(shards, workers))


def total_seconds(workers: int = WORKERS, shards: int = SHARDS) -> float:
    # Reduce cannot begin until the last row lands, so it adds rather than overlaps.
    return FLEET_STARTUP_SECONDS + map_seconds(workers, shards) + reduce_seconds(shards)


def balanced_shards(workers: int = WORKERS) -> int:
    """
    Smallest shard count whose aggregate ingest keeps up with the fleet's raycast
    rate. Below it the fleet stalls on I/O; far above it the cluster idles.

    Solved by search rather than division because streams_per_shard depends on
    the shard count too (workers // shards), so the relation is not linear.
    """
    demand = workers * WORKER_RAYCAST_RATE
    for s in range(1, workers + 1):
        if cluster_ingest_rate(s, workers) >= demand:
            return s
    return workers


def bound_by(workers: int = WORKERS, shards: int = SHARDS) -> str:
    return "I/O-bound" if write_seconds(shards, workers) > raycast_seconds(workers) \
           else "compute-bound"


def io_headroom(workers: int = WORKERS, shards: int = SHARDS) -> float:
    """Fraction of spare ingest capacity at the deployed shape."""
    return cluster_ingest_rate(shards, workers) / (workers * WORKER_RAYCAST_RATE) - 1.0


def fmt(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.2f} h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m {seconds % 60:04.1f}s"
    return f"{seconds:.0f} s"


# ---------------------------------------------------------------------------
def report() -> None:
    bar = "=" * 74
    print(bar)
    print("  SunlightCity — capacity model")
    print(bar)

    print("\n  SCENE")
    print(f"    sample points            {SAMPLE_POINTS:>15,}   @ 2 m spacing")
    print(f"    edges / waypoints        {EDGES:>15,} / {WAYPOINTS:,}")
    print(f"    street trees             {TREES:>15,}")
    print(f"    sample-level rows        {EXPOSURE_ROWS:>15,}")
    print(f"                             {SAMPLE_POINTS:,} pts x {STEPS_PER_DAY} steps x {DAYS} days")
    print(f"    derived edge rows        {EDGE_ROWS:>15,}")
    print(f"    sample-level storage     {RAW_SAMPLES_GB:>12.1f} GB   "
          f"{SAMPLE_ROW_BYTES} B/row, no index")
    print(f"    derived edge index       {EDGE_AGG_GB:>12.2f} GB")
    print(f"    v1 stored the same in    {V1_MEASURED_GB:>12.1f} GB   with 2 inline indexes")

    print("\n  V1 — single node, single PostgreSQL")
    print(f"    wall clock               {fmt(V1_SECONDS):>15}")
    print(f"    raycast rate             {V1_RAYCAST_RATE / 1000:>12.1f}k/s  main thread, serial")

    tr, tw, tm, td = (raycast_seconds(), write_seconds(),
                      map_seconds(), reduce_seconds())
    T = total_seconds()

    print(f"\n  V2 — {WORKERS} workers, {SHARDS} data shards + 1 coordinator")
    print(f"    per-worker rate          {WORKER_RAYCAST_RATE / 1000:>12.1f}k/s  "
          f"{BATCH_SPEEDUP}x batching x {LOCALITY_SPEEDUP}x locality")
    print(f"    fleet raycast rate       {WORKERS * WORKER_RAYCAST_RATE / 1e6:>12.2f}M/s")
    print(f"    cluster ingest rate      {cluster_ingest_rate() / 1e6:>12.2f}M/s  "
          f"{SHARDS} shards x {streams_per_shard()} streams x "
          f"{COPY_ROWS_PER_STREAM // 1000}k")
    print(f"    ├ spin-up                {fmt(FLEET_STARTUP_SECONDS):>15}")
    print(f"    ├ raycast                {fmt(tr):>15}")
    print(f"    ├ write (overlapped)     {fmt(tw):>15}   "
          f"{io_headroom() * 100:+.0f}% headroom")
    print(f"    ├ MAP = max(above)       {fmt(tm):>15}   {bound_by()}")
    print(f"    └ REDUCE                 {fmt(td):>15}   {SHARDS} shards in parallel")
    print(f"\n    TOTAL                    {fmt(T):>15}")

    ceiling = WORKERS * BATCH_SPEEDUP * LOCALITY_SPEEDUP
    print(f"    end-to-end speedup       {V1_SECONDS / T:>12.1f}x")
    print(f"    raw raycast ceiling      {ceiling:>12.1f}x  "
          f"{WORKERS} pods x {BATCH_SPEEDUP * LOCALITY_SPEEDUP:.2f}x per pod")
    print(f"    efficiency vs ceiling    {100 * (V1_SECONDS / T) / ceiling:>11.0f}%  "
          f"the gap is spin-up + reduce")
    print(f"    spin-up / map / reduce   "
          f"{100 * FLEET_STARTUP_SECONDS / T:.0f}% / {100 * tm / T:.0f}% / {100 * td / T:.0f}%")

    print("\n  SHARD SIZING")
    print(f"    fleet demands            {WORKERS * WORKER_RAYCAST_RATE / 1e6:>12.2f}M rows/s")
    print(f"    one instance absorbs     "
          f"{shard_max_streams() * COPY_ROWS_PER_STREAM / 1e6:>12.2f}M rows/s  "
          f"{shard_max_streams()} streams on {SHARD_VCPU} vCPU")
    print(f"    minimum shard count      {balanced_shards():>15}")
    print(f"    deployed                 {SHARDS:>15}   "
          f"{io_headroom() * 100:+.0f}% over minimum, deliberately")
    print(f"    rows per shard           {EXPOSURE_ROWS / SHARDS:>15,.0f}")
    print(f"    bytes per shard          {RAW_SAMPLES_GB / SHARDS:>12.1f} GB   "
          f"fits the {SHARD_GB} GB page cache")

    # The single most important number in this file: what the cluster is worth.
    # Same 50 workers, same code, one database instead of ten.
    t1 = total_seconds(WORKERS, 1)
    print(f"\n    the same {WORKERS} workers on ONE instance: {fmt(t1)} "
          f"({V1_SECONDS / t1:.1f}x) — {bound_by(WORKERS, 1)}")
    print(f"    the DB cluster is worth   {t1 / T:>12.1f}x  "
          f"of the {V1_SECONDS / T:.0f}x total")

    print("\n  SECTIONING")
    h = halo_meters()
    print(f"    shadow halo              {h:>12,.0f} m   "
          f"{MAX_BUILDING_M:.0f} m / tan {SUN_ANGLE_THRESHOLD:.0f}deg — an exact bound")
    print(f"    section edge             {SECTION_METERS:>12,} m")
    print(f"    BVH working set          {working_set_km2():>12.1f} km2  "
          f"vs {annulus_km2():.1f} km2 if the halo were omnidirectional")
    print(f"    sections                 {SECTIONS:>15,}")
    print(f"    time windows             {TIME_WINDOWS:>15,}   "
          f"{(END_MINUTE - START_MINUTE) // TIME_WINDOWS // 60} h, "
          f"{steps_per_window()} steps each")
    print(f"    tasks                    {tasks():>15,}   "
          f"{SECTIONS} x {DAYS} x {TIME_WINDOWS}")
    print(f"    distinct working sets    {section_window_pairs():>15,}   "
          f"what affinity dispatch keeps warm")
    print(f"    rows per task            {rows_per_task():>15,.0f}")
    print(f"    tasks per worker         {tasks() / WORKERS:>15,.0f}   "
          f"tail imbalance <= 1 task")

    print("\n  HARDWARE")
    rows = [
        ("map workers", WORKERS, WORKER_VCPU, WORKER_GB),
        ("data shards", SHARDS, SHARD_VCPU, SHARD_GB),
        ("coordinator", 1, COORD_VCPU, COORD_GB),
        ("pgbouncer", POOLERS, POOLER_VCPU, POOLER_GB),
    ]
    tot_cpu = tot_mem = 0
    for name, n, cpu, gb in rows:
        tot_cpu += n * cpu
        tot_mem += n * gb
        print(f"    {name:<14}{n:>3} x {cpu:>3} vCPU / {gb:>4} GB"
              f"  = {n * cpu:>4} vCPU / {n * gb:>5} GB")
    print(f"    {'TOTAL':<14}                           "
          f"= {tot_cpu:>4} vCPU / {tot_mem:>5} GB")
    print(f"    storage        {SHARDS:>3} x {RAW_SAMPLES_GB / SHARDS:>5.1f} GB data"
          f"  = {RAW_SAMPLES_GB:>4.0f} GB + index/WAL headroom")
    print(f"    cost           {tot_cpu * T / 3600:>6.1f} vCPU-hours for the annual run")
    print(bar)


def sweep() -> None:
    print(f"{'shards':>7} {'ingest':>11} {'map':>10} {'reduce':>10} "
          f"{'total':>10} {'speedup':>9}  bound by")
    print("-" * 74)
    for s in (1, 2, 4, 6, 8, 10, 14, 20, 30):
        tot = total_seconds(shards=s)
        mark = "  <-- deployed" if s == SHARDS else ""
        print(f"{s:>7} {cluster_ingest_rate(s) / 1e6:>9.1f}M/s "
              f"{fmt(map_seconds(shards=s)):>10} {fmt(reduce_seconds(s)):>10} "
              f"{fmt(tot):>10} {V1_SECONDS / tot:>8.1f}x  {bound_by(shards=s)}{mark}")
    print("\n  Below the minimum the fleet waits on the database. Above it the extra")
    print("  shards only shorten the reduce phase, at linear cost — 20 shards buys")
    print(f"  {total_seconds(shards=SHARDS) - total_seconds(shards=20):.0f} s "
          f"for double the database spend.")
    print("  Past ~25 shards it gets WORSE: 50 workers cannot offer enough concurrent")
    print("  streams to keep that many instances busy, so each one starves.")


def worker_sweep() -> None:
    print(f"{'workers':>8} {'min sh':>7} {'map':>10} {'reduce':>10} "
          f"{'total':>10} {'speedup':>9}  bound by")
    print("-" * 74)
    for w in (1, 5, 10, 25, 50, 100, 200):
        # Each row sizes the cluster to that fleet, which is the point: the two
        # numbers are not independently choosable.
        s = balanced_shards(w)
        tot = total_seconds(w, s)
        mark = "  <-- fleet deployed" if w == WORKERS else ""
        print(f"{w:>8} {s:>7} {fmt(map_seconds(w, s)):>10} {fmt(reduce_seconds(s)):>10} "
              f"{fmt(tot):>10} {V1_SECONDS / tot:>8.1f}x  {bound_by(w, s)}{mark}")
    print(f"\n  'min sh' is the MINIMUM shard count for that fleet; the deployment runs")
    print(f"  {SHARDS} rather than {balanced_shards()} so a vacuum, a checkpoint or a slow")
    print("  instance cannot make the fleet wait. Scaling workers without scaling the")
    print("  database converts a compute-bound pipeline into an I/O-bound one — which")
    print("  is exactly the failure mode v2 exists to avoid.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--balance", action="store_true",
                   help="print the minimum viable shard count only")
    p.add_argument("--sweep", action="store_true", help="shard-count sensitivity table")
    p.add_argument("--workers-sweep", action="store_true", help="fleet-size sensitivity table")
    p.add_argument("--workers", type=int, default=WORKERS)
    a = p.parse_args()

    if a.balance:
        print(balanced_shards(a.workers))
    elif a.sweep:
        sweep()
    elif a.workers_sweep:
        worker_sweep()
    else:
        report()
        print()
        sweep()
        print()
        worker_sweep()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
