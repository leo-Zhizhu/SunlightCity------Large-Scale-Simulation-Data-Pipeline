#!/usr/bin/env python3
"""
Capacity model — the single source of truth for the pipeline's sizing.

Every performance figure in the README, in docs/ and in the generated charts is
derived from this module, so they cannot drift apart.

    python model.py --derive           # THE SIZING ARGUMENT: target -> hardware
    python model.py                    # full report + sensitivity sweeps
    python model.py --bench            # the measurement ladder, with provenance
    python model.py --frontier         # cost/time Pareto frontier
    python model.py --db               # what actually ends up in the database

THE REQUIREMENT COMES FIRST, THE HARDWARE IS DERIVED
----------------------------------------------------
The amount of work is fixed and non-negotiable: 365,133 sample points x 360
timesteps x 60 dates = 7.89 billion observations, at v1's exact schema. The
DESIGN REQUIREMENT is also fixed:

    a full 60-date run must complete in under 15 minutes.

Nothing else is given. The fleet size and the database instance count are OUTPUTS
of that requirement, not inputs to it. This module derives them:

    1. measure the unit rates            (BENCHMARKS, below — one lever each)
    2. compose them into T(W, S)         (total_seconds)
    3. enumerate integer (W, S)          (frontier)
    4. keep those holding 900 s under a stated STRESS ENVELOPE
    5. take the cheapest                 (derive_shape)

The answer is 54 workers and 9 data shards. It is pinned in WORKERS / SHARDS
below so the rest of the module and the deployment manifests can read it, but
`--derive` re-runs the whole argument from scratch and will contradict those
constants if anything upstream changes.

WHY A STRESS ENVELOPE AND NOT JUST THE TARGET
---------------------------------------------
The cheapest shape that hits 900 s on paper is 40 workers / 6 shards, at 14m 51s
— 1.0% of margin. It is not deployable: losing one database instance puts it at
15m 21s. Sizing to the nominal number alone is how a capacity plan that is
arithmetically correct becomes operationally useless.

So the chosen shape must hold 900 s in three conditions, not one:

    NOMINAL       every rate as benchmarked
    PESSIMISTIC   every benchmarked rate 15% below bench. This is not an event,
                  it is a state of the world — the possibility that the model is
                  simply optimistic. It does not "clear up".
    PESSIMISTIC + FAILURE
                  that same world, minus one database instance and 10% of the
                  fleet. Because failures happen in the pessimistic world too,
                  and the conjunction is what an SLO actually has to survive.

The third condition is what separates 54/9 from the nominally-adequate 48/8.

THE ONE CONSTRAINT THAT SHAPES EVERYTHING ELSE
----------------------------------------------
The schema is fixed: one row per (sample point, timestamp), exactly as v1 wrote
it. At v2's 60 dates that is 7.89 billion rows, not the 145 million a per-edge sum
would be. The downstream router traverses an edge as an ORDERED, DIRECTIONAL
sequence of sample points — walking east through a colonnade is not the same
exposure as walking west through it — so the per-sample series is the product, not
an intermediate.

Everything follows from refusing to discard it: the row rate sets the ingest
requirement, the ingest requirement sets the shard count, and the shard count sets
the reduce time.

TWO NUMBERS THAT ARE EASY TO CONFLATE
-------------------------------------
ROWS are the full cross product of samples and timesteps. RAYCASTS are only the
daylight timesteps — the horizon guard resolves the rest without touching the BVH.
They differ by ~37%. Throughput is quoted in ROWS/s because that is what both the
compute and the I/O side sustain, and it is how v1's 6 hours was measured. See the
"ROWS ARE NOT RAYCASTS" section and `--db`.
"""

from __future__ import annotations

import argparse
import math
from datetime import date

# ===========================================================================
# SCENE — Manhattan road graph and sampling
# ===========================================================================
SAMPLE_POINTS = 365_133      # 2 m spacing along every edge
WAYPOINTS     = 4_168
EDGES         = 6_700
TREES         = 1_280_954

# ===========================================================================
# SIMULATION WINDOW
#
# 03:00 to 21:00 at 3-minute intervals, HALF-OPEN — so 360 timesteps per day, and
# six 3-hour windows tile the day exactly. (v1's export loop used an inclusive
# endpoint and so ran 361 steps, double-counting 21:00; see meo_window_bounds.)
# ===========================================================================
START_MINUTE  = 180          # 03:00 — earliest sunrise of the year, with margin
END_MINUTE    = 1260         # 21:00 — latest sunset of the year, with margin (exclusive)
STEP_MINUTE   = 3
STEPS_PER_DAY = (END_MINUTE - START_MINUTE) // STEP_MINUTE   # 360

# ---- Temporal coverage: the one number that differs between the versions ----
#
# v1's reference run covered 12 dates — one representative day per month. v2 covers
# 60 — five per month, roughly every six days — which resolves the solar declination
# cycle properly instead of sampling it twelve times.
#
# That is FIVE TIMES the work, and it is why every figure below is what it is. It
# also means a bare "v2 is N times faster" would compare different amounts of work
# and flatter v2 by 5x, so every speedup here is normalised — see
# v1_equivalent_seconds().
V1_DAYS = 12
DAYS    = 60

# The product. Retained at full sample resolution — see the module docstring.
V1_ROWS       = SAMPLE_POINTS * STEPS_PER_DAY * V1_DAYS      # 1.577e9
EXPOSURE_ROWS = SAMPLE_POINTS * STEPS_PER_DAY * DAYS         # 7.887e9

# Derived convenience index: one row per (edge, timestamp), computed shard-locally
# in the reduce phase. Serves "how sunlit is this edge right now" in one lookup;
# the directional queries go to the sample rows.
EDGE_ROWS = EDGES * STEPS_PER_DAY * DAYS                     # 1.447e8

# ===========================================================================
# ROWS ARE NOT RAYCASTS, and conflating them overstates the compute by ~40%
#
# Every (sample point, timestep) pair produces a ROW — including timesteps when the
# sun is below the horizon guard, whose samples are all recorded as shadowed. But
# those timesteps fire NO RAYCAST: SectionExposureSampler.AccumulateTimestep returns
# before building the batch, and v1's ShadowEngine.IsInShadow returns true before
# calling Physics.Raycast. The bits stay 0 and the writer emits them anyway, because
# downstream needs a value at every timestep, not a gap.
#
# So there are two different quantities, and the documentation now keeps them apart:
#
#   ROWS       what the database must absorb and store. The full cross product.
#   RAYCASTS   what the BVH actually traverses. Only the daylight timesteps.
#
# The pipeline's throughput is quoted in ROWS/s, because that is what both the
# compute and the I/O side have to sustain end to end — and because it is the figure
# v1's 6-hour run was measured as. The raycast count is derived below for honesty
# about how much geometry work is really involved.
# ===========================================================================
DEFAULT_LATITUDE = 40.7826          # Manhattan
SOLAR_NOON_MINUTE = 716             # ~11:56 local standard time, 4 deg west of 75W
SUN_ANGLE_THRESHOLD = 5.0           # the horizon guard, degrees

# v2's 60 dates: the 1st, 7th, 13th, 19th and 25th of every month — roughly every
# six days, which resolves the solar declination cycle rather than sampling it 12
# times. v1's 12 were one per month.
V2_DATE_DAYS = (1, 7, 13, 19, 25)
V1_DATE_DAYS = (1,)


def daylight_hours(d: date, latitude: float = DEFAULT_LATITUDE) -> float:
    """
    Daylight length from the solar declination (Cooper's equation) and the sunrise
    hour angle. Deliberately not the pvlib ephemeris the simulation itself uses:
    this only needs to be accurate enough to count timesteps.
    """
    doy = d.timetuple().tm_yday
    decl = math.radians(23.45) * math.sin(math.radians(360.0 * (284 + doy) / 365.0))
    lat = math.radians(latitude)
    cos_omega = max(-1.0, min(1.0, -math.tan(lat) * math.tan(decl)))
    return 2.0 * math.degrees(math.acos(cos_omega)) / 15.0


def live_steps(d: date, lo_minute: int = START_MINUTE, hi_minute: int = END_MINUTE,
               step: int = STEP_MINUTE, threshold: float = SUN_ANGLE_THRESHOLD,
               latitude: float = DEFAULT_LATITUDE) -> int:
    """
    Timesteps in [lo, hi) whose sun is ABOVE the horizon guard — i.e. the ones that
    actually raycast. The guard is converted to minutes at the sun's apparent rate
    of 15 deg/hour.
    """
    dm = daylight_hours(d, latitude) * 60.0
    guard = (threshold / 15.0) * 60.0
    sunrise = SOLAR_NOON_MINUTE - dm / 2.0 + guard
    sunset  = SOLAR_NOON_MINUTE + dm / 2.0 - guard
    return int(max(0.0, min(hi_minute, sunset) - max(lo_minute, sunrise)) // step)


def dates(year: int = 2026, days=V2_DATE_DAYS) -> list[date]:
    return [date(year, m, d) for m in range(1, 13) for d in days]


def raycasts(days=V2_DATE_DAYS) -> int:
    """Actual BVH traversals: samples x daylight timesteps, summed over the dates."""
    return SAMPLE_POINTS * sum(live_steps(d) for d in dates(days=days))


V1_RAYCASTS = raycasts(V1_DATE_DAYS)
RAYCASTS    = raycasts(V2_DATE_DAYS)

# The fraction of evaluations that touch geometry at all. The rest are the horizon
# guard's, and they are why a December window costs a fraction of a June one — the
# cost spread the planner's LPT ordering exploits.
RAYCAST_FRACTION = RAYCASTS / EXPOSURE_ROWS


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
# v1's 12-date run occupied 110 GB measured — the 1.577e9 sample rows together with
# the aggregated per-edge sums, and the indexes maintained on both inline.
#
# v2 builds NO index on the sample table at all: partition pruning down to a
# (section, 3 h window) leaf of ~261k rows makes one redundant, and not building it
# removes both the load-time maintenance and the index heap. The per-row cost is
# therefore the heap width alone.
#
# Sizes are binary GB (GiB), matching what pg_size_pretty reports.
# ===========================================================================
SAMPLE_ROW_BYTES = 68
RAW_SAMPLES_GB   = EXPOSURE_ROWS * SAMPLE_ROW_BYTES / 1024 ** 3   # ~500 GB
EDGE_AGG_GB      = EDGE_ROWS * SAMPLE_ROW_BYTES / 1024 ** 3       # ~9 GB heap
EDGE_INDEX_GB    = EDGE_ROWS * 48 / 1024 ** 3                     # covering index
TOTAL_STORAGE_GB = RAW_SAMPLES_GB + EDGE_AGG_GB + EDGE_INDEX_GB

# v1, measured: 12 dates, samples + edge aggregate + their indexes.
V1_MEASURED_GB   = 110.0

# ===========================================================================
# V1 BASELINE — one desktop, main-thread Physics.Raycast, one PostgreSQL
#
# Measured: 1,577,374,560 ROWS in about 6 hours, a sustained end-to-end rate
# (compute AND I/O together) of ~262.9 million per hour.
# ===========================================================================
V1_SECONDS   = 6.0 * 3600
# ROWS per second, not raycasts — see the note above. This is the end-to-end
# pipeline rate: raycasting, guard evaluation, buffering and COPY together.
V1_ROW_RATE  = V1_ROWS / V1_SECONDS                          # ~73k/s
# Retained under the old name because a great deal of prose and several scripts
# refer to it; it is the same number, more precisely described.
# CAREFUL: this is a ROW rate, not a raycast rate. The name predates the
# rows-vs-raycasts distinction and is kept because prose and scripts refer to it, but
# dividing a measured RAYCAST rate by it compares two different quantities and
# overstates the result by 1/RAYCAST_FRACTION (~1.6x). monitor.py did exactly that.
V1_RAYCAST_RATE = V1_ROW_RATE

# The genuine v1 raycast rate: BVH traversals actually fired per second. Use this one
# when the numerator is a raycast count.
V1_BVH_RATE = V1_RAYCASTS / V1_SECONDS                       # ~45.8k/s

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
WORKER_ROW_RATE = V1_ROW_RATE * BATCH_SPEEDUP * LOCALITY_SPEEDUP          # ~296k/s
WORKER_RAYCAST_RATE = WORKER_ROW_RATE      # alias, see V1_RAYCAST_RATE above

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
#   2. index the rollup (16.1M rows/shard, not 876M)
#   3. ANALYZE, run with vacuumdb --jobs so the leaf partitions go in parallel
#
# COPY FREEZE during the load means there is no freeze-vacuum to pay later
# either: the tuples are already visible to everyone.
# ===========================================================================
EDGE_ROLLUP_ROWS_PER_S = 12_000_000   # parallel aggregate, 8 workers, page-cache resident
INDEX_BUILD_ROWS_PER_S = 600_000      # B-tree, 8 parallel maint. workers, 16 GB maintenance_work_mem
ANALYZE_SECONDS        = 30           # 3,360 leaves per shard via vacuumdb --analyze --jobs 8

# ===========================================================================
# FLEET SPIN-UP
#
# Counted, not waved away: it is 5% of the 15-minute BUDGET and 7% of the achieved
# 11m 09s RUNTIME, and together with ANALYZE_SECONDS it forms the 75 s floor no
# hardware beats. (Those are two different denominators; the docs keep them apart.)
# Warm image cache + engine boot + scene load + whole-city BVH warm.
# ===========================================================================
FLEET_STARTUP_SECONDS = 45

# ===========================================================================
# THE DESIGN REQUIREMENT
#
# This is the input. Everything in DEPLOYMENT SHAPE below is derived from it.
# ===========================================================================
TARGET_SECONDS = 15 * 60      # a full 60-date run, end to end

# The stress envelope the chosen shape must hold TARGET_SECONDS under. See the
# module docstring for why the third condition exists.
STRESS_RATE_SHORTFALL = 0.15   # every benchmarked rate this far below bench
STRESS_SHARD_LOSS     = 1      # database instances lost mid-run
STRESS_WORKER_LOSS    = 0.10   # fraction of the fleet evicted and not replaced

# ===========================================================================
# THE MEASUREMENT LADDER
#
# Each row is one benchmark that isolates ONE lever, run on the reference
# hardware, in the order they were run. The composed model is only as good as
# these, so each records what it measured and on what — a rate with no
# provenance is a guess with a decimal point.
#
#   python model.py --bench
#
# Fields: (id, lever, measured value, unit, method, constant it sets)
# ===========================================================================
BENCHMARKS = [
    ("B1", "v1 single-thread end-to-end", 73_027, "rows/s",
     "v1's own 12-date reference run, 1.577e9 rows in 6.00 h on one desktop: "
     "main-thread Physics.Raycast, one PostgreSQL, no batching. The only rate "
     "here that is a whole-pipeline measurement rather than a microbenchmark.",
     "V1_ROW_RATE"),
    ("B2", "batched raycast dispatch", 3.0, "x",
     "One 8 vCPU pod, one section, one window. Physics.Raycast in a loop vs "
     "RaycastCommand.ScheduleBatch over the whole timestep. 3.0x and not 8x "
     "because the main thread still schedules, completes and folds results, and "
     "BVH traversal saturates memory bandwidth before ALUs.",
     "BATCH_SPEEDUP"),
    ("B3", "section-local BVH locality", 1.35, "x",
     "Same pod, same ray count: rays drawn from one 1 km section vs drawn "
     "city-wide. Isolates cache behaviour alone — the working set (section + "
     "shadow halo) stays resident where a city-wide sweep misses constantly. "
     "This is why sectioning is a throughput decision, not only a data-mapping one.",
     "LOCALITY_SPEEDUP"),
    ("B4", "one binary COPY stream", 200_000, "rows/s",
     "One backend, BINARY COPY into a freshly created WAL-skipped relation on "
     "16 vCPU / NVMe. Binary rather than CSV removes the server-side text parse "
     "and puts ~30 B on the wire instead of ~52 B.",
     "COPY_ROWS_PER_STREAM"),
    ("B5", "streams per instance before contention", 12, "streams",
     "Swept 1..20 concurrent COPY streams into DISTINCT relations on one 16 vCPU "
     "instance. Scales linearly to 12, then flattens: a COPY backend is one busy "
     "CPU, and the WAL writer, checkpointer, bgwriter and OS need the rest. "
     "Distinct relations is what keeps it linear at all — same-relation streams "
     "serialise on the extension lock.",
     "shard_max_streams()"),
    ("B6", "edge rollup aggregate", 12_000_000, "rows/s",
     "GROUP BY (edge_id, datetime) over one shard's leaves, 8 parallel workers, "
     "page-cache resident. Shard-local because a section owns whole edges.",
     "EDGE_ROLLUP_ROWS_PER_S"),
    ("B7", "rollup index build", 600_000, "rows/s",
     "B-tree on the 16.1M-row rollup, 8 parallel maintenance workers, "
     "16 GB maintenance_work_mem.",
     "INDEX_BUILD_ROWS_PER_S"),
    ("B8", "fleet spin-up", 45, "s",
     "Pod scheduled to first task claimed, warm image cache: engine boot, scene "
     "load, whole-city BVH warm. Counted rather than waved away: against the "
     "15-minute budget it is 5% of the whole allowance, it is 7% of the achieved "
     "11m 09s runtime, and it shrinks with nothing at all.",
     "FLEET_STARTUP_SECONDS"),
    ("B9", "ANALYZE the leaf tree", 30, "s",
     "vacuumdb --analyze --jobs 8 over one shard's 3,360 leaves. A floor: it does "
     "not shrink with more shards, so it sets the reduce phase's asymptote.",
     "ANALYZE_SECONDS"),
]

# ===========================================================================
# DEPLOYMENT SHAPE — DERIVED, not chosen
#
# These two numbers are the output of derive_shape() against TARGET_SECONDS and
# the stress envelope above. They are pinned as constants because the k8s
# manifests, pg_tune.py and the docs all read them — but `python model.py
# --derive` re-runs the argument and asserts it still lands here.
#
#   54 workers = 6 per shard exactly, so each shard runs precisely the 12
#   concurrent COPY streams B5 says it can sustain. W = 6S is the matched shape:
#   one fewer worker per shard wastes ingest capacity, one more contends for it.
# ===========================================================================
WORKERS        = 54
SHARDS         = 9            # data instances; + 1 coordinator, see cluster.py
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


# Every timing function below takes `scale`: a multiplier on every BENCHMARKED
# rate, 1.0 meaning "exactly as measured". It exists so the sizing can be tested
# against the model being WRONG — see envelope() and STRESS_RATE_SHORTFALL. Rates
# multiply; the spin-up cost is a duration, so it divides.
def raycast_seconds(workers: int = WORKERS, scale: float = 1.0) -> float:
    return EXPOSURE_ROWS / (max(1, workers) * WORKER_RAYCAST_RATE * scale)


def cluster_ingest_rate(shards: int = SHARDS, workers: int = WORKERS,
                        scale: float = 1.0) -> float:
    return (max(1, shards) * streams_per_shard(workers, shards)
            * COPY_ROWS_PER_STREAM * scale)


def write_seconds(shards: int = SHARDS, workers: int = WORKERS,
                  scale: float = 1.0) -> float:
    return EXPOSURE_ROWS / cluster_ingest_rate(shards, workers, scale)


def reduce_seconds(shards: int = SHARDS, scale: float = 1.0) -> float:
    s = max(1, shards)
    return (EXPOSURE_ROWS / s) / (EDGE_ROLLUP_ROWS_PER_S * scale) \
         + (EDGE_ROWS / s) / (INDEX_BUILD_ROWS_PER_S * scale) \
         + ANALYZE_SECONDS / scale


def map_seconds(workers: int = WORKERS, shards: int = SHARDS,
                scale: float = 1.0) -> float:
    """
    A worker streams rows as it computes them, so writing overlaps raycasting and
    the map phase costs max() rather than the sum. Whichever side is larger is
    the binding constraint; the shard count is chosen to keep it the compute side.
    """
    return max(raycast_seconds(workers, scale), write_seconds(shards, workers, scale))


def total_seconds(workers: int = WORKERS, shards: int = SHARDS,
                  scale: float = 1.0) -> float:
    # Reduce cannot begin until the last row lands, so it adds rather than overlaps.
    return (FLEET_STARTUP_SECONDS / scale
            + map_seconds(workers, shards, scale)
            + reduce_seconds(shards, scale))


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def vcpu(workers: int = WORKERS, shards: int = SHARDS) -> int:
    """Total provisioned vCPU. The objective the derivation minimises."""
    return (workers * WORKER_VCPU + shards * SHARD_VCPU
            + COORD_VCPU + POOLERS * POOLER_VCPU)


def ram_gb(workers: int = WORKERS, shards: int = SHARDS) -> int:
    return (workers * WORKER_GB + shards * SHARD_GB
            + COORD_GB + POOLERS * POOLER_GB)


# ---------------------------------------------------------------------------
# THE DERIVATION — requirement in, hardware out
# ---------------------------------------------------------------------------
# A degraded instance must not turn the pipeline I/O-bound. This is the
# architecture's central thesis expressed as a test rather than an aspiration:
# the cluster exists so the fleet never waits on the database, and a claim that
# only holds while every instance is healthy is not the claim being made.
MIN_IO_HEADROOM_DEGRADED = 0.10


def envelope(workers: int, shards: int) -> dict:
    """
    Every condition the chosen shape must satisfy. Three are deadlines under
    stress; two are structural.

    DEADLINES (all against TARGET_SECONDS)
      nominal              every rate as benchmarked
      pessimistic          every rate STRESS_RATE_SHORTFALL below bench
      pessimistic_failure  that world, minus one instance and 10% of the fleet

    STRUCTURAL
      survives_shard_loss  still compute-bound, with headroom, at S-1. Without
          this the deadline conditions alone happily select an 8-shard shape whose
          cluster is 1% from becoming the bottleneck the moment anything hiccups.
      saturated            the fleet offers each shard the full complement of
          COPY streams B5 says it sustains. A shape that provisions nine
          instances and then feeds each of them ten streams out of twelve is
          paying for ingest capacity it has no way to use.

    `failure` (nominal rates, one failure) is computed for reporting but is
    implied by pessimistic_failure and does not gate.
    """
    good = 1.0 - STRESS_RATE_SHORTFALL
    degraded_w = max(1, int(workers * (1.0 - STRESS_WORKER_LOSS)))
    degraded_s = max(1, shards - STRESS_SHARD_LOSS)

    e = {
        "nominal":       total_seconds(workers, shards),
        "failure":       total_seconds(degraded_w, degraded_s),
        "pessimistic":   total_seconds(workers, shards, good),
        "pessimistic_failure": total_seconds(degraded_w, degraded_s, good),
    }
    e["deadlines_ok"] = all(e[k] <= TARGET_SECONDS
                            for k in ("nominal", "pessimistic", "pessimistic_failure"))
    e["survives_shard_loss"] = (
        shards > STRESS_SHARD_LOSS
        and io_headroom(workers, degraded_s) >= MIN_IO_HEADROOM_DEGRADED)
    e["saturated"] = streams_per_shard(workers, shards) >= shard_max_streams()
    e["passes"] = (e["deadlines_ok"] and e["survives_shard_loss"] and e["saturated"])
    e["slack"] = 1.0 - e["pessimistic_failure"] / TARGET_SECONDS
    return e


def frontier(max_workers: int = 200, max_shards: int = 40) -> list[tuple]:
    """
    Cost/time Pareto frontier over integer (workers, shards): for each vCPU
    budget, the fastest achievable nominal run, keeping only strict improvements.

    This is the curve the deployment shape is chosen ON, and the one the
    cost_time figure draws.
    """
    best: dict[int, tuple] = {}
    for w in range(1, max_workers + 1):
        for s in range(1, max_shards + 1):
            c, t = vcpu(w, s), total_seconds(w, s)
            if c not in best or t < best[c][0]:
                best[c] = (t, w, s)
    out, floor = [], float("inf")
    for c in sorted(best):
        t, w, s = best[c]
        if t < floor - 1e-9:
            out.append((c, w, s, t))
            floor = t
    return out


def derive_shape(target: float = TARGET_SECONDS,
                 max_workers: int = 200, max_shards: int = 40) -> dict:
    """
    THE sizing answer: the cheapest integer (workers, shards) that holds `target`
    across the whole stress envelope.

    Exhaustive rather than analytic. The continuous optimum (Lagrange on
    8W + 16S subject to A/W + B/S = C) gives W/S = 7.7 and is useful for
    intuition, but streams_per_shard() steps at W/S = 6 and the answer must be
    integral, so the true frontier is a staircase and search is both simpler and
    exact. See docs/PERFORMANCE.md.
    """
    feasible, deadline_only = [], []
    for w in range(1, max_workers + 1):
        for s in range(1, max_shards + 1):
            e = envelope(w, s)
            if not (e["deadlines_ok"] and e["nominal"] <= target):
                continue
            deadline_only.append((vcpu(w, s), w, s))
            if e["passes"]:
                feasible.append((vcpu(w, s), -e["slack"], w, s, e))
    if not feasible:
        raise ValueError(f"no shape within {max_workers}x{max_shards} holds {target}s")
    feasible.sort()
    # Ties on cost are broken by envelope slack, which is why 54/9 wins its band.
    cost, _, w, s, e = feasible[0]

    # The band shown by --derive deliberately includes shapes that clear the three
    # DEADLINES but fail a structural condition — those are the interesting rows,
    # because they are the ones a deadline-only analysis would have selected.
    deadline_only.sort()
    band = [(c, ww, ss) for c, ww, ss in deadline_only if c <= cost][-6:]
    band += [(c, ww, ss) for c, ww, ss in deadline_only if c > cost][:3]
    return {"workers": w, "shards": s, "vcpu": cost, "ram_gb": ram_gb(w, s),
            "envelope": e, "feasible_count": len(feasible),
            "deadline_only_count": len(deadline_only), "band": band}


def cheapest_nominal(target: float = TARGET_SECONDS,
                     max_workers: int = 200, max_shards: int = 40) -> tuple:
    """
    The cheapest shape that hits `target` with NO stress allowance — i.e. the
    answer you get by sizing to the headline number alone.

    Reported by --derive specifically to be rejected: it is 40/6, and one lost
    database instance puts it over.
    """
    out = None
    for w in range(1, max_workers + 1):
        for s in range(1, max_shards + 1):
            if total_seconds(w, s) <= target:
                c = vcpu(w, s)
                if out is None or c < out[0]:
                    out = (c, w, s, total_seconds(w, s))
    return out


def v1_equivalent_seconds(rows: float = None) -> float:
    """
    What v1 would take for the SAME work, at its measured sustained rate.

    v2 covers 60 dates against v1's 12, so a bare wall-clock ratio would compare
    different amounts of work and flatter v2 by 5x. Every speedup figure in this
    module and in the documentation is against this number instead.

    It comes out at exactly 30.0 h, which is the arithmetic being explicit rather
    than a coincidence: v2's row count is 5x v1's, and v1 took 6 h.
    """
    return (EXPOSURE_ROWS if rows is None else rows) / V1_RAYCAST_RATE


def speedup(workers: int = WORKERS, shards: int = SHARDS) -> float:
    """End-to-end, work-normalised."""
    return v1_equivalent_seconds() / total_seconds(workers, shards)


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
    print(f"    timesteps per day        {STEPS_PER_DAY:>15,}   "
          f"{START_MINUTE // 60:02d}:00-{END_MINUTE // 60:02d}:00 every {STEP_MINUTE} min")

    print("\n  V1 — 12 dates, one desktop, one PostgreSQL")
    print(f"    sample-level rows        {V1_ROWS:>15,}   "
          f"{SAMPLE_POINTS:,} x {STEPS_PER_DAY} x {V1_DAYS}")
    print(f"    wall clock               {fmt(V1_SECONDS):>15}   measured")
    print(f"    sustained rate           {V1_RAYCAST_RATE / 1000:>12.1f}k/s  "
          f"= {V1_RAYCAST_RATE * 3600 / 1e6:.1f}M/hour, compute AND I/O")
    print(f"    storage                  {V1_MEASURED_GB:>12.1f} GB   "
          f"samples + edge sums + indexes")

    print(f"\n  V2 — {DAYS} dates, {DAYS // V1_DAYS}x the temporal coverage")
    print(f"    sample-level rows        {EXPOSURE_ROWS:>15,}   "
          f"{SAMPLE_POINTS:,} x {STEPS_PER_DAY} x {DAYS}")
    print(f"    derived edge rows        {EDGE_ROWS:>15,}")
    print(f"    sample-level storage     {RAW_SAMPLES_GB:>12.1f} GB   "
          f"{SAMPLE_ROW_BYTES} B/row, no index")
    print(f"    derived edge index       {EDGE_AGG_GB:>12.2f} GB heap "
          f"+ {EDGE_INDEX_GB:.2f} GB index")
    print(f"    total                    {TOTAL_STORAGE_GB:>12.1f} GB")
    print(f"    v1 would need            {fmt(v1_equivalent_seconds()):>15}   "
          f"for the same {DAYS} dates, at its measured rate")

    tr, tw, tm, td = (raycast_seconds(), write_seconds(),
                      map_seconds(), reduce_seconds())
    T = total_seconds()

    print(f"\n  V2 TIMING — {WORKERS} workers, {SHARDS} data shards + 1 coordinator")
    print(f"    per-worker rate          {WORKER_RAYCAST_RATE / 1000:>12.1f}k/s  "
          f"{BATCH_SPEEDUP}x batching x {LOCALITY_SPEEDUP}x locality")
    print(f"    fleet row rate           {WORKERS * WORKER_ROW_RATE / 1e6:>12.2f}M/s   what the cluster must absorb")
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
    print(f"    end-to-end speedup       {speedup():>12.1f}x   vs {fmt(v1_equivalent_seconds())} of v1 for the SAME 60 dates")
    print(f"    raw throughput ceiling   {ceiling:>12.1f}x  "
          f"{WORKERS} pods x {BATCH_SPEEDUP * LOCALITY_SPEEDUP:.2f}x per pod")
    print(f"    efficiency vs ceiling    {100 * speedup() / ceiling:>11.0f}%  "
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
    print(f"    leaves per shard         {tasks() // SHARDS:>15,}   "
          f"~{RAW_SAMPLES_GB * 1024 / tasks():.0f} MB each")

    # The single most important number in this file: what the cluster is worth.
    # Same 54 workers, same code, one database instead of nine.
    t1 = total_seconds(WORKERS, 1)
    print(f"\n    the same {WORKERS} workers on ONE instance: {fmt(t1)} "
          f"({v1_equivalent_seconds() / t1:.1f}x) — {bound_by(WORKERS, 1)}")
    print(f"    the DB cluster is worth   {t1 / T:>12.1f}x  "
          f"of the {speedup():.0f}x total")

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
    for name, n, cpu, gb in rows:
        print(f"    {name:<14}{n:>3} x {cpu:>3} vCPU / {gb:>4} GB"
              f"  = {n * cpu:>4} vCPU / {n * gb:>5} GB")
    tot_cpu, tot_mem = vcpu(), ram_gb()
    print(f"    {'TOTAL':<14}                           "
          f"= {tot_cpu:>4} vCPU / {tot_mem:>5} GB")
    print(f"    storage        {SHARDS:>3} x {RAW_SAMPLES_GB / SHARDS:>5.1f} GB data"
          f"  = {RAW_SAMPLES_GB:>4.0f} GB + index/WAL headroom")
    print(f"    cost           {tot_cpu * T / 3600:>6.1f} vCPU-hours for the {DAYS}-date run")

    # The requirement this shape exists to satisfy, and its margin. Printed last
    # because it is the number the whole file is accountable to.
    e = envelope(WORKERS, SHARDS)
    print(f"\n  AGAINST THE {TARGET_SECONDS / 60:.0f}-MINUTE REQUIREMENT")
    for label, key in (("nominal", "nominal"),
                       ("one shard + 10% of fleet lost", "failure"),
                       (f"every rate {STRESS_RATE_SHORTFALL:.0%} below bench", "pessimistic"),
                       ("both at once", "pessimistic_failure")):
        v = e[key]
        print(f"    {label:<32}{fmt(v):>10}   "
              f"{100 * (1 - v / TARGET_SECONDS):>+5.1f}%  "
              f"{'OVER' if v > TARGET_SECONDS else 'ok'}")
    print(f"    derived by      python model.py --derive")
    print(bar)


def inventory() -> None:
    """
    Exactly what ends up in the database, table by table.

    Written because "N billion rows in the database" is not a precise statement
    about this system. The sample table is the largest object but it is not the only
    one, it is not on one instance, and its row count is not the raycast count.
    """
    bar = "=" * 74
    print(bar)
    print(f"  Database inventory — {SHARDS} data shards + 1 coordinator, {DAYS} dates")
    print(bar)

    geom = [
        ("meo_waypoints",      WAYPOINTS,     "graph nodes"),
        ("meo_edges",          EDGES,         "streets"),
        ("meo_sample_points",  SAMPLE_POINTS, "2 m spacing, ORDERED per edge"),
        ("meo_edge_sections",  EDGES,         "edge -> owning section"),
        ("meo_trees",          TREES,         "canopies; coordinator only by default"),
    ]
    ctrl = [
        ("meo_tasks",     tasks(),        "one per (section, date, window)"),
        ("meo_sections",  SECTIONS,       "+ Hilbert index + shard assignment"),
        ("meo_shards",    SHARDS,         "registry: host, port, dbname, state"),
        ("meo_runs",      1,              "frozen config the fleet must agree on"),
        ("meo_grid",      1,              "the pinned section-id contract"),
    ]

    print("\n  COORDINATOR  (sunlit_coord)  — control plane + authoritative geometry")
    print(f"    {'table':<26}{'rows':>14}   note")
    print("    " + "-" * 68)
    coord_rows = 0
    for name, n, note in ctrl + geom:
        coord_rows += n
        print(f"    {name:<26}{n:>14,}   {note}")
    print(f"    {'TOTAL':<26}{coord_rows:>14,}   ~200 MB, dominated by trees + GiST indexes")

    per_shard_samples = EXPOSURE_ROWS // SHARDS
    per_shard_edges   = EDGE_ROWS // SHARDS
    per_shard_geom    = sum(n for name, n, _ in geom if name != "meo_trees")
    per_shard_rows    = per_shard_samples + per_shard_edges + per_shard_geom

    print(f"\n  EACH DATA SHARD  (sunlit_shard_0 .. {SHARDS - 1})")
    print(f"    {'table':<26}{'rows':>14}{'size':>10}   note")
    print("    " + "-" * 78)
    print(f"    {'meo_exposure_samples_p':<26}{per_shard_samples:>14,}"
          f"{RAW_SAMPLES_GB / SHARDS:>8.1f} GB   {tasks() // SHARDS:,} leaves, NO INDEX")
    print(f"    {'meo_exposure_edges_p':<26}{per_shard_edges:>14,}"
          f"{(EDGE_AGG_GB + EDGE_INDEX_GB) / SHARDS:>8.2f} GB   12 monthly partitions, derived")
    print(f"    {'static geometry (x4)':<26}{per_shard_geom:>14,}{0.14:>8.2f} GB   "
          f"full replica, read-only")
    print(f"    {'TOTAL per shard':<26}{per_shard_rows:>14,}"
          f"{TOTAL_STORAGE_GB / SHARDS + 0.14:>8.1f} GB")

    cluster_rows = EXPOSURE_ROWS + EDGE_ROWS + SHARDS * per_shard_geom + coord_rows
    print("\n  CLUSTER TOTAL")
    print(f"    sample-level rows        {EXPOSURE_ROWS:>16,}   "
          f"{100 * EXPOSURE_ROWS / cluster_rows:.1f}% of all rows")
    print(f"    derived edge rows        {EDGE_ROWS:>16,}")
    print(f"    geometry (replicated)    {SHARDS * per_shard_geom:>16,}   "
          f"{per_shard_geom:,} x {SHARDS} shards")
    print(f"    control plane            {coord_rows:>16,}")
    print(f"    {'ALL ROWS':<24} {cluster_rows:>16,}")
    print(f"    {'ALL STORAGE':<24} {TOTAL_STORAGE_GB + SHARDS * 0.14:>13.1f} GB")

    print("\n  WHAT ONE ROW IS")
    print("    meo_exposure_samples is FULLY NORMALISED — long and narrow, not wide:")
    print("        (sample_point_id UUID, datetime TIMESTAMP, is_sunlit BOOLEAN)")
    print("    One row = ONE (sample point, timestep) observation carrying ONE BIT.")
    print("    So the row count IS the observation count, by construction — there is no")
    print("    array, no per-timestep column, nothing packed. That is why 365,133 x 360 x")
    print(f"    {DAYS} evaluations and {EXPOSURE_ROWS:,} rows are the same number.")
    print()
    print(f"    The cost of that encoding: {SAMPLE_ROW_BYTES} B on the page to carry 1 bit "
          f"of payload,")
    print(f"    = {100 / (SAMPLE_ROW_BYTES * 8):.2f}% payload efficiency.")
    # Measured bytes/row on PostgreSQL 16, not estimated. See docs/DB_CLUSTER.md.
    b_rows, b_gb = SAMPLE_POINTS * DAYS, SAMPLE_POINTS * DAYS * 109.3 / 1024 ** 3
    c_rows = SAMPLE_POINTS * DAYS * TIME_WINDOWS
    c_gb = c_rows * 84.5 / 1024 ** 3
    print("    Letting the bit's POSITION carry the timestamp (bit k = minute 180+3k)")
    print("    removes both the timestamp and the repeated UUID. Measured:")
    print(f"      BIT({STEPS_PER_DAY}) per (sample, date)     "
          f"{b_rows:>15,} rows  {b_gb:>6.1f} GB  {RAW_SAMPLES_GB / b_gb:>4.0f}x smaller")
    print(f"      BIT({steps_per_window()}) per (sample, date, window) "
          f"{c_rows:>13,} rows  {c_gb:>6.1f} GB  {RAW_SAMPLES_GB / c_gb:>4.0f}x smaller")
    print("    Lossless — a bijection; a view over generate_series rebuilds v1's three")
    print("    columns exactly. The per-window variant is the one compatible with")
    print("    one-task-one-relation (a per-day row would span six tasks).")
    print(f"    At that encoding the fleet would produce {c_rows / map_seconds():,.0f} rows/s")
    print(f"    against {shard_max_streams() * COPY_ROWS_PER_STREAM:,}/s per instance — "
          f"ONE instance, {shard_max_streams() * COPY_ROWS_PER_STREAM / (c_rows / map_seconds()):.0f}x over.")
    print("    NOT adopted: the v1 column set is a hard requirement. The cluster is a")
    print("    consequence of the encoding, not of the physics — docs/DB_CLUSTER.md.")

    print("\n  ROWS vs RAYCASTS — not the same number")
    print(f"    rows written             {EXPOSURE_ROWS:>16,}   every (sample, timestep)")
    print(f"    raycasts fired           {RAYCASTS:>16,}   "
          f"{100 * RAYCAST_FRACTION:.1f}% — only daylight timesteps")
    print(f"    resolved by the guard    {EXPOSURE_ROWS - RAYCASTS:>16,}   "
          f"recorded shadowed without touching the BVH")
    print(f"    live steps per date      "
          f"{min(live_steps(d) for d in dates()):>7}..{max(live_steps(d) for d in dates()):<8}"
          f"of {STEPS_PER_DAY}   winter .. summer")
    print(bar)


def bench_report() -> None:
    """The measurement ladder, with what each benchmark isolated and on what."""
    bar = "=" * 78
    print(bar)
    print("  The measurement ladder — every rate the model composes")
    print(bar)
    print("\n  Each benchmark isolates ONE lever. The composed prediction is only as")
    print("  good as these, so each records its method; a rate with no provenance is")
    print("  a guess with a decimal point.\n")
    for bid, lever, val, unit, method, const in BENCHMARKS:
        v = f"{val:,.0f}" if isinstance(val, int) or val >= 100 else f"{val:.2f}"
        print(f"  {bid}  {lever}")
        print(f"      {v} {unit:<9} -> {const}")
        for line in _wrap(method, 68):
            print(f"      {line}")
        print()
    print("  COMPOSED")
    print(f"    per-worker rate   B1 x B2 x B3 = {V1_ROW_RATE:,.0f} x {BATCH_SPEEDUP} x "
          f"{LOCALITY_SPEEDUP} = {WORKER_ROW_RATE:,.0f} rows/s")
    print(f"    per-shard ingest  B4 x B5      = {COPY_ROWS_PER_STREAM:,} x "
          f"{shard_max_streams()} = {shard_max_streams() * COPY_ROWS_PER_STREAM:,.0f} rows/s")
    print(f"    workers per shard = {shard_max_streams() * COPY_ROWS_PER_STREAM:,.0f} / "
          f"{WORKER_ROW_RATE:,.0f} = {shard_max_streams() * COPY_ROWS_PER_STREAM / WORKER_ROW_RATE:.2f}"
          f"  -> {shard_max_streams() // STREAMS_PER_WORKER} at {STREAMS_PER_WORKER} streams each")
    print(f"\n    T(W,S) =   {FLEET_STARTUP_SECONDS}"
          f"   +   max( {EXPOSURE_ROWS / WORKER_ROW_RATE:,.0f}/W , "
          f"{EXPOSURE_ROWS / (shard_max_streams() * COPY_ROWS_PER_STREAM):,.0f}/S )"
          f"   +   {EXPOSURE_ROWS / EDGE_ROLLUP_ROWS_PER_S + EDGE_ROWS / INDEX_BUILD_ROWS_PER_S:,.0f}/S + {ANALYZE_SECONDS}")
    print(f"             ^B8            ^B1-B3     ^B4-B5           ^B6-B7   ^B9")
    print(f"             spin-up        raycast    write            reduce")
    print(f"                            \\____ overlap: max() ____/")
    print(f"\n    Irreducible floor, at any hardware: "
          f"{FLEET_STARTUP_SECONDS + ANALYZE_SECONDS} s "
          f"({100 * (FLEET_STARTUP_SECONDS + ANALYZE_SECONDS) / TARGET_SECONDS:.0f}% of the "
          f"{TARGET_SECONDS / 60:.0f}-minute budget).")
    print("    Neither term shrinks with more workers or more shards, so the whole")
    print(f"    sizing problem is spending the remaining "
          f"{TARGET_SECONDS - FLEET_STARTUP_SECONDS - ANALYZE_SECONDS:.0f} s well.")
    print(bar)


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def derive_report() -> None:
    """
    The sizing argument, start to finish: requirement -> benchmarks -> frontier ->
    stress envelope -> chosen shape. This is the report docs/PERFORMANCE.md follows.
    """
    bar = "=" * 78
    print(bar)
    print(f"  SIZING DERIVATION — how many workers, how many database instances?")
    print(bar)

    print(f"\n  STEP 0 — THE REQUIREMENT (the only input)")
    print(f"    work            {EXPOSURE_ROWS:>16,} rows   "
          f"{SAMPLE_POINTS:,} x {STEPS_PER_DAY} x {DAYS}, non-negotiable")
    print(f"    deadline        {TARGET_SECONDS:>16,.0f} s     "
          f"= {TARGET_SECONDS / 60:.0f} min, end to end")
    print(f"    v1 would need   {v1_equivalent_seconds():>16,.0f} s     "
          f"= {fmt(v1_equivalent_seconds())} at its measured rate")
    print(f"    so the pipeline must be {v1_equivalent_seconds() / TARGET_SECONDS:.0f}x "
          f"faster than v1, work-normalised.")

    print(f"\n  STEP 1 — UNIT RATES (python model.py --bench)")
    print(f"    per worker      {WORKER_ROW_RATE:>16,.0f} rows/s  "
          f"B1 x B2 x B3 = {V1_ROW_RATE / 1000:.0f}k x {BATCH_SPEEDUP} x {LOCALITY_SPEEDUP}")
    print(f"    per shard       {shard_max_streams() * COPY_ROWS_PER_STREAM:>16,.0f} rows/s  "
          f"B4 x B5 = {COPY_ROWS_PER_STREAM // 1000}k x {shard_max_streams()}")
    print(f"    one shard feeds {shard_max_streams() // STREAMS_PER_WORKER:>16,} workers  "
          f"at {STREAMS_PER_WORKER} COPY streams each")

    print(f"\n  STEP 2 — THE COMPOSED MODEL")
    A = EXPOSURE_ROWS / WORKER_ROW_RATE
    B = EXPOSURE_ROWS / (shard_max_streams() * COPY_ROWS_PER_STREAM)
    C = EXPOSURE_ROWS / EDGE_ROLLUP_ROWS_PER_S + EDGE_ROWS / INDEX_BUILD_ROWS_PER_S
    print(f"    T(W,S) = {FLEET_STARTUP_SECONDS} + max({A:,.0f}/W, {B:,.0f}/S) "
          f"+ {C:,.0f}/S + {ANALYZE_SECONDS}")
    print(f"    floor  = {FLEET_STARTUP_SECONDS + ANALYZE_SECONDS} s at ANY hardware "
          f"(spin-up + ANALYZE neither shrink)")
    print(f"    budget = {TARGET_SECONDS - FLEET_STARTUP_SECONDS - ANALYZE_SECONDS:.0f} s "
          f"to spend on the two terms that do")

    print(f"\n  STEP 3 — THE NAIVE ANSWER, AND WHY IT IS REJECTED")
    c0, w0, s0, t0 = cheapest_nominal()
    e0 = envelope(w0, s0)
    print(f"    cheapest shape hitting {TARGET_SECONDS / 60:.0f} min: "
          f"{w0} workers / {s0} shards, {c0} vCPU, {fmt(t0)}")
    print(f"    margin {100 * (1 - t0 / TARGET_SECONDS):.1f}% — and then:")
    print(f"      lose one database instance         {fmt(e0['failure']):>10}   "
          f"{'OVER' if e0['failure'] > TARGET_SECONDS else 'ok'}")
    print(f"      rates 15% below bench              {fmt(e0['pessimistic']):>10}   "
          f"{'OVER' if e0['pessimistic'] > TARGET_SECONDS else 'ok'}")
    print(f"    Sizing to the nominal number is how an arithmetically correct plan")
    print(f"    becomes operationally useless.")

    print(f"\n  STEP 4 — THE ENVELOPE (what the shape must actually satisfy)")
    print(f"    three deadlines, all against {TARGET_SECONDS / 60:.0f} min:")
    print(f"      NOMINAL               every rate as benchmarked")
    print(f"      PESSIMISTIC           every rate {STRESS_RATE_SHORTFALL:.0%} below bench. Not an event —")
    print(f"                            a state of the world. It does not clear up.")
    print(f"      PESSIMISTIC + FAILURE that world, minus {STRESS_SHARD_LOSS} instance and "
          f"{STRESS_WORKER_LOSS:.0%} of the fleet,")
    print(f"                            because failures happen in it too")
    print(f"    and two structural conditions:")
    print(f"      SURVIVES SHARD LOSS   still compute-bound with "
          f">={MIN_IO_HEADROOM_DEGRADED:.0%} ingest headroom at S-1.")
    print(f"                            The thesis 'the fleet never waits on the database'")
    print(f"                            is not being claimed only for healthy clusters.")
    print(f"      SATURATED             the fleet offers each shard all "
          f"{shard_max_streams()} streams B5 sustains.")
    print(f"                            Provisioning an instance and feeding it 10 of 12")
    print(f"                            is paying for capacity with no way to use it.")

    print(f"\n  STEP 5 — CHEAPEST SHAPE SATISFYING ALL FIVE")
    d = derive_shape()
    print(f"    {'vCPU':>6} {'W':>4} {'S':>3} {'W/S':>5} {'strm':>5} {'nominal':>10} "
          f"{'-15%':>10} {'-15%+fail':>10} {'io@S-1':>7}  why not")
    for c, w, s in d["band"]:
        e = envelope(w, s)
        if (w, s) == (d["workers"], d["shards"]):
            why = "<== chosen"
        elif not e["survives_shard_loss"]:
            why = f"S-1 headroom {io_headroom(w, s - 1) * 100:+.0f}% — too thin"
        elif not e["saturated"]:
            why = f"uses {streams_per_shard(w, s)} of {shard_max_streams()} streams"
        elif not e["deadlines_ok"]:
            why = "misses a deadline"
        else:
            why = "dearer"
        print(f"    {c:>6} {w:>4} {s:>3} {w / s:>5.1f} {streams_per_shard(w, s):>5} "
              f"{fmt(e['nominal']):>10} {fmt(e['pessimistic']):>10} "
              f"{fmt(e['pessimistic_failure']):>10} {io_headroom(w, s - 1) * 100:>+6.0f}%  {why}")
    print(f"\n    {d['deadline_only_count']:,} shapes meet the three deadlines; only "
          f"{d['feasible_count']:,} also meet")
    print(f"    both structural conditions. Those two are what decide it, not cost: the")
    print(f"    cost band is flat to within a few percent, so the deadlines alone")
    print(f"    leave a dozen indistinguishable shapes. Requiring the cluster to")
    print(f"    survive an instance loss eliminates every 8-shard shape; requiring it")
    print(f"    to be saturated eliminates 53/9, which buys nine instances and then")
    print(f"    feeds each of them ten streams out of twelve.")

    print(f"\n  RESULT")
    print(f"    {d['workers']} map workers  x  {WORKER_VCPU} vCPU / {WORKER_GB} GB")
    print(f"    {d['shards']} data shards  x  {SHARD_VCPU} vCPU / {SHARD_GB} GB"
          f"   (+1 coordinator, {POOLERS} poolers)")
    print(f"    {d['vcpu']} vCPU / {d['ram_gb']:,} GB total"
          f"   —   {fmt(d['envelope']['nominal'])} nominal, "
          f"{100 * (1 - d['envelope']['nominal'] / TARGET_SECONDS):.0f}% under target")
    print(f"    W = 6S exactly: each shard runs precisely the {shard_max_streams()} COPY streams")
    print(f"    B5 says it sustains. One worker fewer per shard wastes ingest capacity;")
    print(f"    one more contends for it.")
    if (d["workers"], d["shards"]) != (WORKERS, SHARDS):
        print(f"\n    !! DISAGREES with the pinned WORKERS={WORKERS} / SHARDS={SHARDS}."
              f" Update them, the k8s")
        print(f"       manifests and the docs — or explain the deviation here.")
    else:
        print(f"\n    Matches the pinned WORKERS={WORKERS} / SHARDS={SHARDS}. "
              f"Manifests and docs agree.")
    print(bar)


def frontier_report() -> None:
    """The cost/time Pareto frontier — the curve the shape is chosen on."""
    bar = "=" * 78
    print(bar)
    print("  Cost / time Pareto frontier")
    print(bar)
    print("\n  For each vCPU budget, the fastest achievable run. Marginal return is")
    print("  what matters: the curve is ~1/W, so it decays as W^-2 and there is NO")
    print("  knee to find. The deadline plus the stress envelope is what picks a")
    print("  point; 'diminishing returns' does not, because they diminish smoothly.\n")
    pf = frontier()
    print(f"  {'vCPU':>6} {'W':>4} {'S':>3} {'total':>10} {'s per +100 vCPU':>17}  bound by")
    prev = None
    shown = 0
    for c, w, s, t in pf:
        # Thin the low-cost tail, which is dozens of near-identical single-shard rows.
        if c < 100 and shown > 4:
            prev = (c, t)
            continue
        d = f"{(prev[1] - t) / ((c - prev[0]) / 100):>14,.0f} s" if prev else "".rjust(16)
        mark = ""
        if prev and prev[1] > TARGET_SECONDS >= t:
            mark = f"  <== {TARGET_SECONDS / 60:.0f}-min line"
        if (w, s) == (WORKERS, SHARDS):
            mark = "  <== deployed"
        print(f"  {c:>6} {w:>4} {s:>3} {fmt(t):>10} {d:>17}  {bound_by(w, s)}{mark}")
        prev = (c, t)
        shown += 1
        if c > vcpu() * 1.5:
            break
    floor = FLEET_STARTUP_SECONDS + ANALYZE_SECONDS
    print(f"\n  The staircase: W climbs at fixed S until W/S passes "
          f"{shard_max_streams() * COPY_ROWS_PER_STREAM / WORKER_ROW_RATE:.1f} and the")
    print(f"  shape goes I/O-bound, then S increments and W resumes. Every tread is")
    print(f"  compute-bound by construction — that is the cluster doing its job.")
    print(f"\n  Asymptote: {floor} s. Even infinite hardware cannot beat spin-up +")
    print(f"  ANALYZE, so past ~{vcpu(103, 14)} vCPU an extra 100 vCPU buys less than the "
          f"{ANALYZE_SECONDS} s")
    print(f"  ANALYZE floor itself — the point where more hardware stops being an")
    print(f"  engineering answer and starts being a rounding error.")
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
              f"{fmt(tot):>10} {v1_equivalent_seconds() / tot:>8.1f}x  {bound_by(shards=s)}{mark}")
    print("\n  Below the minimum the fleet waits on the database. Above it the extra")
    print("  shards only shorten the reduce phase, at linear cost — 20 shards buys")
    print(f"  {total_seconds(shards=SHARDS) - total_seconds(shards=20):.0f} s "
          f"for double the database spend.")
    print(f"  Past ~{WORKERS // STREAMS_PER_WORKER} shards it gets WORSE: {WORKERS} workers "
          f"cannot offer enough concurrent")
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
              f"{fmt(tot):>10} {v1_equivalent_seconds() / tot:>8.1f}x  {bound_by(w, s)}{mark}")
    print(f"\n  'min sh' is the MINIMUM shard count for that fleet; the deployment runs")
    print(f"  {SHARDS} rather than {balanced_shards()} so a vacuum, a checkpoint or a slow")
    print("  instance cannot make the fleet wait. Scaling workers without scaling the")
    print("  database converts a compute-bound pipeline into an I/O-bound one — which")
    print("  is exactly the failure mode v2 exists to avoid.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--derive", action="store_true",
                   help="THE sizing argument: requirement -> hardware")
    p.add_argument("--bench", action="store_true",
                   help="the measurement ladder, with provenance")
    p.add_argument("--frontier", action="store_true",
                   help="cost/time Pareto frontier")
    p.add_argument("--balance", action="store_true",
                   help="print the minimum viable shard count only")
    p.add_argument("--db", action="store_true",
                   help="what ends up in the database, table by table")
    p.add_argument("--sweep", action="store_true", help="shard-count sensitivity table")
    p.add_argument("--workers-sweep", action="store_true", help="fleet-size sensitivity table")
    p.add_argument("--workers", type=int, default=WORKERS)
    a = p.parse_args()

    if a.derive:
        derive_report()
    elif a.bench:
        bench_report()
    elif a.frontier:
        frontier_report()
    elif a.balance:
        print(balanced_shards(a.workers))
    elif a.db:
        inventory()
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
