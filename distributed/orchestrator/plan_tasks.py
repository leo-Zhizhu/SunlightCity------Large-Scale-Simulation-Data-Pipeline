#!/usr/bin/env python3
"""
Plans a distributed run: derives the section grid, assigns sections to shards,
provisions every instance, and fills the work queue.

Runs ONCE before the fleet starts, and is the only component that needs a global
view of the work.

USAGE
    # full plan, including provisioning the ten shards
    python plan_tasks.py --run-id run-2026-annual --shards 10 --provision

    # see what it would do, touching nothing
    python plan_tasks.py --run-id run-2026-annual --shards 10 --dry-run

    # a two-shard, one-date smoke run
    python plan_tasks.py --run-id smoke --shards 2 --dates 6.21 --provision

WHAT IT DOES, IN ORDER
    1. derive the section grid from the graph's extent          (coordinator)
    2. assign every edge to a section by its midpoint           (coordinator)
    3. weight sections by sample count                          (coordinator)
    4. cut the Hilbert-ordered sections into k balanced runs     (cluster.py)
    5. report write balance and read locality, refuse if bad
    6. provision each shard: identity, geometry, partitions       (shards)
    7. insert tasks in longest-processing-time order            (coordinator)

DESIGN NOTES
------------
COST ESTIMATION drives dispatch order, and here it is per WINDOW rather than per
day — which matters far more than it did in v1. A 03:00-06:00 window in December
is entirely below the horizon guard and costs nothing; the same window in June is
most of a sunrise. Estimating per day would have made all six of a date's windows
look identical and thrown away most of the spread that LPT exploits.

The estimate does not need to be accurate, only correctly ORDERED.

WHY THE SHARD ASSIGNMENT IS COMPUTED HERE AND NOT IN SQL
-------------------------------------------------------
The balanced cut is an exact algorithm (binary search on the bound plus a greedy
feasibility test — see cluster.balanced_runs). Implementing it twice, once in
plpgsql and once in Python, would be two chances to get it subtly different. So it
lives in Python and the result is written into meo_sections.shard_index, which is
then the single authority that workers and the federation both read.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")

from cluster import ClusterEndpoints, SectionGrid, Topology, hilbert_index
import model


# --- Reference dates: 1st and 15th of every month ---------------------------
DEFAULT_DATES = [(m, d) for m in range(1, 13) for d in (1, 15)]

# Manhattan. Used only to estimate daylight for cost ordering, so a low-precision
# model is fine — see estimate_daylight_window().
DEFAULT_LATITUDE = 40.7826

# Local solar noon, in minutes past local midnight. Manhattan sits ~4 deg west of
# the 75 deg W standard meridian, so solar noon falls near 11:56 rather than 12:00.
# Only used to place the daylight window for the cost estimate.
SOLAR_NOON_MINUTE = 716


# ===========================================================================
# Daylight model
# ===========================================================================
def daylight_hours(d: date, latitude: float = DEFAULT_LATITUDE) -> float:
    """
    Approximate daylight length. Standard solar-declination model (Cooper's
    equation for declination, then the sunrise hour angle).

    Deliberately NOT the pvlib ephemeris the simulation itself uses: this only
    needs to rank a June window above a December one, and pulling pvlib in would
    make the orchestrator image depend on the scientific stack.
    """
    doy = d.timetuple().tm_yday
    decl = math.radians(23.45) * math.sin(math.radians(360.0 * (284 + doy) / 365.0))
    lat = math.radians(latitude)

    cos_omega = -math.tan(lat) * math.tan(decl)
    # Polar day / polar night — impossible at Manhattan's latitude, but the clamp
    # keeps the function total for other cities.
    cos_omega = max(-1.0, min(1.0, cos_omega))
    return 2.0 * math.degrees(math.acos(cos_omega)) / 15.0


def estimate_daylight_steps(d: date, lo_minute: int, hi_minute: int, step: int,
                            sun_threshold_deg: float,
                            latitude: float = DEFAULT_LATITUDE) -> int:
    """
    Timesteps inside [lo_minute, hi_minute) whose sun is above the horizon guard.

    This is the whole cost estimate, and the guard is why it is not simply the step
    count: the worker skips a timestep entirely when the sun is below
    SUN_ANGLE_THRESHOLD, so cost tracks USABLE daylight inside the window.

    The threshold is converted to minutes at 15 deg/hour, the sun's apparent rate.
    That over-estimates the trim near the solstices (the sun climbs more slowly
    when it rises far from due east) and under-estimates it at the equinoxes, which
    is well inside the accuracy an ordering needs.
    """
    dm = daylight_hours(d, latitude) * 60.0
    guard_minutes = (sun_threshold_deg / 15.0) * 60.0

    sunrise = SOLAR_NOON_MINUTE - dm / 2.0 + guard_minutes
    sunset  = SOLAR_NOON_MINUTE + dm / 2.0 - guard_minutes

    overlap = max(0.0, min(hi_minute, sunset) - max(lo_minute, sunrise))
    return int(overlap // step)


def parse_dates(spec: str | None, year: int) -> list[date]:
    """Parses 'M.D, M.D, ...' into dates, or returns the 24 defaults."""
    if not spec:
        pairs = DEFAULT_DATES
    else:
        pairs = []
        for tok in spec.replace(",", " ").split():
            if "." not in tok:
                raise ValueError(f"bad date token {tok!r}; expected M.D e.g. 6.21")
            m, d = tok.split(".", 1)
            pairs.append((int(m), int(d)))

    out = []
    for m, d in pairs:
        try:
            out.append(date(year, m, d))
        except ValueError as e:
            raise ValueError(f"invalid date {year}-{m}-{d}: {e}") from e
    return sorted(set(out))


# ===========================================================================
# Geometry replication
# ===========================================================================
GEOMETRY_TABLES = [
    # Order matters: foreign keys point backwards along this list.
    ("meo_waypoints",     "id, geom"),
    ("meo_edges",         "id, start_wp_id, end_wp_id, length, sample_count, "
                          "total_tree_value, geom"),
    ("meo_sample_points", "id, edge_id, sequence_index, distance_from_start, "
                          "geom, tree_value"),
    ("meo_edge_sections", "edge_id, section_id, sample_count"),
]


def replicate_geometry(coord_conn, shard_conn, shard_index: int,
                       include_trees: bool = False) -> dict[str, int]:
    """
    Copies the static geometry from the coordinator to one shard.

    Every shard gets the FULL geometry, not only its own sections. It is ~140 MB
    and holding all of it means a shard can answer any directional query locally,
    and that moving a section between shards moves exposure rows only — never
    geometry.

    Streamed through a spooled temp file in PostgreSQL's own binary COPY format
    rather than assembled in Python. Binary keeps PostGIS geometries as their
    native EWKB instead of round-tripping them through hex text, and the spool
    means meo_sample_points' 365k rows never all sit in memory at once.

    meo_trees is skipped by default: v2 never reads it. It belongs on the
    coordinator, where the v1 tree-shade join runs.
    """
    tables = list(GEOMETRY_TABLES)
    if include_trees:
        tables.insert(1, ("meo_trees", "id, geom, shade_norm"))

    counts: dict[str, int] = {}
    src = coord_conn.cursor()
    dst = shard_conn.cursor()

    for table, cols in tables:
        # TRUNCATE, not DELETE: re-running the provisioning step must be idempotent,
        # and these tables have no dependents on a shard beyond each other.
        dst.execute(f"TRUNCATE {table} CASCADE")

        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as spool:
            src.copy_expert(f"COPY (SELECT {cols} FROM {table}) TO STDOUT (FORMAT binary)",
                            spool)
            spool.seek(0)
            dst.copy_expert(f"COPY {table} ({cols}) FROM STDIN (FORMAT binary)", spool)

        dst.execute(f"SELECT count(*) FROM {table}")
        counts[table] = dst.fetchone()[0]

    shard_conn.commit()
    return counts


# ===========================================================================
def db_config(args, override: dict | None = None) -> dict:
    cfg = {
        "host": args.coord_host or os.environ.get("SUNLIT_COORD_HOST", "localhost"),
        "port": int(args.coord_port or os.environ.get("SUNLIT_COORD_PORT", 5432)),
        "database": args.coord_db or os.environ.get("SUNLIT_COORD_DB", "sunlit_coord"),
        "user": args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        "password": args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    }
    if override:
        cfg.update(override)
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--shards", type=int, default=model.SHARDS,
                   help=f"data instances (default {model.SHARDS})")
    p.add_argument("--workers", type=int, default=model.WORKERS,
                   help="fleet size; only used to sanity-check the shard count")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--dates", default=None,
                   help="'M.D, M.D' list; default is 1st+15th of each month (24 dates)")
    p.add_argument("--section-meters", type=float, default=model.SECTION_METERS)
    p.add_argument("--windows", type=int, default=model.TIME_WINDOWS,
                   help="time windows per day; must divide the simulation span evenly")
    p.add_argument("--start-minute", type=int, default=model.START_MINUTE, help="03:00")
    p.add_argument("--end-minute", type=int, default=model.END_MINUTE,
                   help="21:00, EXCLUSIVE — windows are half-open so they tile exactly")
    p.add_argument("--step-minute", type=int, default=model.STEP_MINUTE)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--max-tasks-per-shard", type=int, default=None,
                   help="admission cap; default = (shard vCPU - 4) / 2 streams per worker")
    p.add_argument("--global-elevation", type=float, default=-112.0)
    p.add_argument("--sun-angle-threshold", type=float, default=model.SUN_ANGLE_THRESHOLD)
    p.add_argument("--city", default="Manhattan")
    p.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    p.add_argument("--max-imbalance", type=float, default=1.25,
                   help="refuse a topology whose worst shard exceeds this x the mean")

    p.add_argument("--provision", action="store_true",
                   help="also set up each shard: identity, geometry, partitions")
    p.add_argument("--with-trees", action="store_true",
                   help="replicate meo_trees to the shards too (v2 does not read it)")
    p.add_argument("--reset", action="store_true",
                   help="delete this run's existing tasks first (destructive)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan without writing anything")

    p.add_argument("--coord-host"); p.add_argument("--coord-port")
    p.add_argument("--coord-db"); p.add_argument("--db-user"); p.add_argument("--db-password")
    args = p.parse_args()

    # ---- Argument sanity, before touching anything -------------------------
    if args.shards < 1:
        return fail("--shards must be >= 1")
    if args.start_minute >= args.end_minute:
        return fail("--start-minute must be < --end-minute")
    span = args.end_minute - args.start_minute
    if span % args.windows != 0:
        return fail(f"--windows ({args.windows}) does not divide the {span}-minute "
                    "simulation span evenly. Windows must tile the day exactly, or "
                    "timesteps fall between partitions with nowhere to land.")
    if (span // args.windows) % args.step_minute != 0:
        return fail(f"a {span // args.windows}-minute window is not a whole number of "
                    f"{args.step_minute}-minute steps.")

    try:
        dates = parse_dates(args.dates, args.year)
    except ValueError as e:
        return fail(str(e))

    steps_per_window = (span // args.windows) // args.step_minute
    cap = args.max_tasks_per_shard or max(1, model.shard_max_streams() // model.STREAMS_PER_WORKER)

    print("=" * 78)
    print(f"  Planning run: {args.run_id}")
    print("=" * 78)
    print(f"  shards            : {args.shards}   (fleet {args.workers})")
    print(f"  dates             : {len(dates)}  ({dates[0]} .. {dates[-1]})")
    print(f"  window            : {args.start_minute // 60:02d}:{args.start_minute % 60:02d}"
          f"-{args.end_minute // 60:02d}:{args.end_minute % 60:02d} exclusive, "
          f"{args.windows} x {span // args.windows} min, "
          f"{steps_per_window} steps each ({span // args.step_minute}/day)")
    print(f"  admission cap     : {cap} concurrent tasks per shard "
          f"({cap * model.STREAMS_PER_WORKER} COPY streams)")

    # A shard count below the model's minimum means the fleet will wait on I/O.
    # Worth saying up front rather than leaving it to be discovered from a graph.
    minimum = model.balanced_shards(args.workers)
    if args.shards < minimum:
        warn(f"--shards {args.shards} is below the {minimum} that {args.workers} workers "
             f"need to stay compute-bound. Expect the fleet to wait on the database; "
             f"see `python model.py --sweep`.")
    print()

    coord = None
    try:
        coord = psycopg2.connect(**db_config(args))
        coord.autocommit = False
        cur = coord.cursor()

        # ---- 1. Preconditions ---------------------------------------------
        cur.execute("SELECT (SELECT count(*) FROM meo_edges), "
                    "(SELECT count(*) FROM meo_sample_points)")
        n_edges, n_samples = cur.fetchone()
        print(f"  graph             : {n_edges:,} edges, {n_samples:,} sample points")

        if n_edges == 0 or n_samples == 0:
            return fail("meo_edges / meo_sample_points is empty. Run "
                        "db_pipeline_initializer.py and export sample points first.")

        # ---- 2. Section grid ------------------------------------------------
        cur.execute("SELECT * FROM meo_init_grid(%s)", (args.section_meters,))
        ox, oz, smeters, ncols, nrows = cur.fetchone()
        print(f"  section grid      : origin ({ox:g}, {oz:g}), {smeters:g} m, "
              f"graph spans {ncols} x {nrows} sections")

        grid = SectionGrid(origin_x=ox, origin_z=oz, size=smeters)

        # ---- 3. Sections ----------------------------------------------------
        cur.execute("SELECT meo_assign_edge_sections()")
        assigned = cur.fetchone()[0]
        cur.execute("SELECT meo_rebuild_sections()")
        n_sections = cur.fetchone()[0]
        print(f"  sections          : {n_sections} non-empty "
              f"({assigned:,} edges assigned by midpoint)")

        if args.shards > n_sections:
            return fail(f"--shards ({args.shards}) exceeds the {n_sections} non-empty "
                        "sections. Some instances would own nothing; use larger --shards "
                        "granularity or smaller --section-meters.")

        # ---- 4. Balanced Hilbert cut ---------------------------------------
        cur.execute("SELECT section_id, edge_count, sample_count FROM meo_sections")
        weights = {sid: (ec, sc) for sid, ec, sc in cur.fetchall()}
        topo = Topology.build(grid, args.shards, weights)

        print(f"  write imbalance   : {topo.imbalance():.3f}x max/mean")
        print(f"  read contiguity   : {topo.contiguity():.2f}  "
              f"(a hash of section ids would give ~{1 / args.shards:.2f})")

        if topo.imbalance() > args.max_imbalance:
            return fail(
                f"write imbalance {topo.imbalance():.3f}x exceeds --max-imbalance "
                f"{args.max_imbalance}. The slowest instance would set the makespan. "
                "Use smaller --section-meters so the cut has finer granularity to "
                "work with, or raise --max-imbalance to accept it.")

        if not args.dry_run:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE meo_sections s SET shard_index = v.shard "
                "FROM (VALUES %s) AS v(section, shard) WHERE s.section_id = v.section",
                [(s.section_id, s.shard_index) for s in topo.sections])

        print()
        print(f"  {'shard':>6} {'sections':>9} {'edges':>8} {'samples':>11} {'share':>7} "
              f"{'vs mean':>8}")
        print("  " + "-" * 56)
        loads = topo.shard_loads()
        total = sum(loads)
        for i, load in enumerate(loads):
            secs = [s for s in topo.sections if s.shard_index == i]
            print(f"  {i:>6} {len(secs):>9} {sum(s.edges for s in secs):>8} "
                  f"{load:>11,} {100 * load / total:>6.1f}% "
                  f"{load / (total / len(loads)):>7.3f}x")
        print()

        # ---- 5. Shard registry ---------------------------------------------
        endpoints = ClusterEndpoints.from_environment()
        if not args.dry_run:
            for i in range(args.shards):
                sh = endpoints.shard(i)
                cur.execute("SELECT meo_register_shard(%s, %s, %s, %s, %s, %s)",
                            (i, sh["host"], sh["port"], sh["dbname"],
                             model.SHARD_VCPU, model.SHARD_GB))
            print(f"  registered        : {args.shards} shard(s), "
                  f"{endpoints.shard_host_template.format(i='N')}")

        # ---- 6. Run row ----------------------------------------------------
        run_config = {
            "global_elevation": f"{args.global_elevation:g}",
            "sun_angle_threshold": f"{args.sun_angle_threshold:g}",
            "city": args.city,
            "step_minute": args.step_minute,
            "start_minute": args.start_minute,
            "end_minute": args.end_minute,
            "windows": args.windows,
            "planned_at": datetime.now(timezone.utc).isoformat(),
        }
        # The grid is pinned into the run so a worker whose own grid disagrees
        # refuses to start. A one-metre origin difference would put samples near a
        # boundary into a neighbouring section's partition, silently.
        run_config.update(grid.as_config())

        if not args.dry_run:
            cur.execute("""
                INSERT INTO meo_runs (run_id, shard_count, section_count, config, notes,
                                      started_at, max_tasks_per_shard)
                VALUES (%s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (run_id) DO UPDATE
                  SET shard_count = EXCLUDED.shard_count,
                      section_count = EXCLUDED.section_count,
                      config = EXCLUDED.config,
                      max_tasks_per_shard = EXCLUDED.max_tasks_per_shard,
                      started_at = COALESCE(meo_runs.started_at, now())
            """, (args.run_id, args.shards, len(topo.sections), json.dumps(run_config),
                  f"{len(dates)} dates x {len(topo.sections)} sections x {args.windows} windows",
                  cap))

            if args.reset:
                cur.execute("DELETE FROM meo_tasks WHERE run_id = %s", (args.run_id,))
                print(f"  reset             : deleted {cur.rowcount} existing task(s)")

        # ---- 7. Tasks -------------------------------------------------------
        rows = []
        for d in dates:
            for w in range(args.windows):
                lo = args.start_minute + w * (span // args.windows)
                hi = lo + (span // args.windows)
                live_steps = estimate_daylight_steps(
                    d, lo, hi, args.step_minute, args.sun_angle_threshold, args.latitude)

                for s in topo.sections:
                    # max(1, ...) so a fully-dark window still sorts, and still gets a
                    # task: its rows are all 'shadowed' and downstream needs them
                    # present rather than missing.
                    est = s.samples * max(1, live_steps)
                    rows.append((args.run_id, s.section_id, d, w,
                                 lo, hi, args.step_minute, s.shard_index,
                                 est, args.max_attempts))

        total_est = sum(r[8] for r in rows)
        print(f"  tasks             : {len(rows):,}  "
              f"({len(topo.sections)} sections x {len(dates)} dates x {args.windows} windows)")
        print(f"  est. raycasts     : {total_est:,}")
        print(f"  tasks per worker  : {len(rows) / max(1, args.workers):,.0f}")

        # The spread LPT exploits. Reported because if it is ~1.0x the estimate has
        # stopped distinguishing windows and the ordering is doing nothing.
        by_est = sorted(rows, key=lambda r: r[8])
        spread = by_est[-1][8] / max(1, by_est[0][8])
        print(f"  cost spread       : {spread:.1f}x cheapest to dearest window")
        print()
        print("  dispatch order (longest-processing-time first):")
        for r in sorted(rows, key=lambda r: -r[8])[:5]:
            print(f"    section {r[1]:>5}  {r[2]}  w{r[3]}  "
                  f"{r[4] // 60:02d}:00-{r[5] // 60:02d}:00  est {r[8]:>10,} rays")
        print()

        if args.dry_run:
            print("  DRY RUN — nothing written.")
            coord.rollback()
            return 0

        # ON CONFLICT DO NOTHING makes re-planning safe: an interrupted plan can be
        # re-run without duplicating tasks or resetting completed ones.
        psycopg2.extras.execute_values(cur, """
            INSERT INTO meo_tasks
              (run_id, section_id, sim_date, window_index,
               start_minute, end_minute, step_minute, shard_index,
               est_raycasts, max_attempts)
            VALUES %s
            ON CONFLICT (run_id, section_id, sim_date, window_index) DO NOTHING
        """, rows, page_size=2000)
        inserted = cur.rowcount

        coord.commit()

        # ---- 8. Provision the shards ---------------------------------------
        if args.provision:
            print("=" * 78)
            print("  Provisioning shards")
            print("=" * 78)
            years = sorted({d.year for d in dates})

            for i in range(args.shards):
                sections = sorted(s.section_id for s in topo.sections if s.shard_index == i)
                sh_cfg = endpoints.shard(i)
                sh_cfg["password"] = db_config(args)["password"]

                try:
                    shard = psycopg2.connect(
                        host=sh_cfg["host"], port=sh_cfg["port"],
                        dbname=sh_cfg["dbname"], user=sh_cfg["user"],
                        password=sh_cfg["password"])
                except psycopg2.Error as e:
                    return fail(f"shard {i} at {sh_cfg['host']}:{sh_cfg['port']} "
                                f"is unreachable: {e}")

                with shard:
                    sc = shard.cursor()
                    sc.execute("SELECT meo_set_shard_identity(%s, %s)", (i, args.shards))

                    geo = replicate_geometry(coord, shard, i, args.with_trees)

                    sc.execute("SELECT meo_provision_sections(%s)", (sections,))
                    parents = sc.fetchone()[0]
                    sc.execute("SELECT meo_provision_edge_partitions(%s, %s)",
                               (min(years), max(years)))
                    parts = sc.fetchone()[0]

                    # Section weights, so the shard's own completeness check in
                    # 05_post_load_indexes.sql knows how many rows to expect.
                    psycopg2.extras.execute_values(
                        sc,
                        "INSERT INTO meo_shard_sections (section_id, sample_count) "
                        "VALUES %s ON CONFLICT (section_id) DO UPDATE "
                        "SET sample_count = EXCLUDED.sample_count",
                        [(s.section_id, s.samples)
                         for s in topo.sections if s.shard_index == i])

                shard.close()
                print(f"  shard {i:>2} : {len(sections):>3} sections, "
                      f"{parents} parent(s) + {parts} edge partition(s), "
                      f"geometry {geo['meo_sample_points']:,} samples / "
                      f"{geo['meo_edge_sections']:,} edge map rows")
            print()

        cur.execute("""
            SELECT tasks_total, tasks_pending, tasks_done, tasks_failed
            FROM meo_run_progress WHERE run_id = %s
        """, (args.run_id,))
        total_t, pending, done, failed = cur.fetchone()
        coord.commit()

        print(f"  inserted          : {inserted:,} new task(s)")
        print(f"  queue state       : {total_t:,} total / {pending:,} pending / "
              f"{done:,} done / {failed:,} failed")
        print()
        print("=" * 78)
        print("  Ready. Start the fleet:")
        print("    kubectl -n sunlightcity apply -k distributed/k8s/")
        print(f"    python monitor.py --run-id {args.run_id} --watch")
        if not args.provision:
            print()
            print("  NOTE: --provision was not given, so the shards have no partitions")
            print("  and no geometry. Workers will fail every task until you run it.")
        print("=" * 78)
        return 0

    except psycopg2.Error as e:
        if coord:
            coord.rollback()
        return fail(f"database error: {e}")
    finally:
        if coord:
            coord.close()


def warn(msg: str) -> None:
    print(f"  WARNING: {msg}")


def fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
