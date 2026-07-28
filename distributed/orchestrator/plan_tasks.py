#!/usr/bin/env python3
"""
Plans a distributed run: provisions partitions, builds the edge->shard map, and
populates the work queue.

This is the "map phase planner". It runs ONCE before the fleet starts and is the
only component that needs a global view of the work.

USAGE
    python plan_tasks.py --run-id run-2026-annual --shard-count 50
    python plan_tasks.py --run-id smoke --shard-count 4 --dates 6.21 --dry-run

DESIGN NOTES
------------
Cost estimation drives dispatch order. Tasks are dispatched longest-first (LPT),
which bounds makespan at 4/3 of optimal for identical workers — and here the cost
spread is real: a June shard-day has roughly 15 daylight hours to raycast versus
~9 in December, because the worker skips whole timesteps whose sun is below the
horizon guard. Dispatching shortest-first would leave a 15-hour task starting last
while 49 workers watch.

The estimate does not need to be accurate, only correctly ORDERED.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")


# --- Reference dates: 1st and 15th of every month --------------------------
DEFAULT_DATES = [(m, d) for m in range(1, 13) for d in (1, 15)]

# Manhattan. Used only to estimate daylight length for cost ordering, so a
# low-precision model is fine — see estimate_daylight_hours().
DEFAULT_LATITUDE = 40.7826


def db_config(args) -> dict:
    return {
        "host": args.db_host or os.environ.get("SUNLIT_DB_HOST", "localhost"),
        "port": int(args.db_port or os.environ.get("SUNLIT_DB_PORT", 5432)),
        "database": args.db_name or os.environ.get("SUNLIT_DB_NAME", "city_data"),
        "user": args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        "password": args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    }


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


def estimate_daylight_hours(d: date, latitude: float = DEFAULT_LATITUDE) -> float:
    """
    Approximate daylight length, used purely to order tasks by cost.

    Standard solar-declination model. Deliberately NOT the pvlib ephemeris the
    simulation itself uses: this only needs to rank a June day above a December
    day, and pulling pvlib in would make the planner depend on the scientific
    stack for a sort key.
    """
    day_of_year = d.timetuple().tm_yday
    # Declination, Cooper's equation.
    decl = math.radians(23.45) * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0))
    lat = math.radians(latitude)

    cos_omega = -math.tan(lat) * math.tan(decl)
    # Polar day / polar night — impossible at Manhattan's latitude but the clamp
    # keeps the function total for other cities.
    cos_omega = max(-1.0, min(1.0, cos_omega))
    hour_angle = math.degrees(math.acos(cos_omega))
    return 2.0 * hour_angle / 15.0


def estimate_raycasts(samples_in_shard: int, d: date, start_min: int,
                      end_min: int, step_min: int) -> int:
    """
    Estimated raycasts for one shard-day.

    The key insight the planner must model: the worker's horizon guard SKIPS
    raycasting entirely for timesteps whose sun is below the threshold. So cost
    scales with DAYLIGHT steps inside the window, not with the window length.
    Ignoring that would make every date look identical and destroy the LPT
    ordering that keeps makespan low.
    """
    total_steps = (end_min - start_min) // step_min + 1
    daylight_minutes = estimate_daylight_hours(d) * 60.0
    window_minutes = end_min - start_min
    daylight_steps = min(total_steps,
                         int(total_steps * min(1.0, daylight_minutes / max(1, window_minutes))))
    return samples_in_shard * max(1, daylight_steps)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--shard-count", type=int, default=50,
                   help="number of edge shards; set equal to fleet size (default 50)")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--dates", default=None,
                   help="'M.D, M.D' list; default is 1st+15th of each month (24 dates)")
    p.add_argument("--start-minute", type=int, default=180, help="03:00")
    p.add_argument("--end-minute", type=int, default=1260, help="21:00")
    p.add_argument("--step-minute", type=int, default=3)
    p.add_argument("--emit-raw", action="store_true",
                   help="ALSO persist per-sample booleans (~53x more write volume)")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--global-elevation", type=float, default=-112.0)
    p.add_argument("--sun-angle-threshold", type=float, default=5.0)
    p.add_argument("--city", default="Manhattan")
    p.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    p.add_argument("--reset", action="store_true",
                   help="delete this run's existing tasks first (destructive)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan without writing to the database")

    p.add_argument("--db-host"); p.add_argument("--db-port")
    p.add_argument("--db-name"); p.add_argument("--db-user"); p.add_argument("--db-password")

    args = p.parse_args()

    if args.shard_count < 1:
        return fail("--shard-count must be >= 1")
    if args.start_minute >= args.end_minute:
        return fail("--start-minute must be < --end-minute")

    try:
        dates = parse_dates(args.dates, args.year)
    except ValueError as e:
        return fail(str(e))

    steps_per_day = (args.end_minute - args.start_minute) // args.step_minute + 1

    print("=" * 74)
    print(f"  Planning run: {args.run_id}")
    print("=" * 74)
    print(f"  shards          : {args.shard_count}")
    print(f"  dates           : {len(dates)}  ({dates[0]} .. {dates[-1]})")
    print(f"  window          : {args.start_minute//60:02d}:{args.start_minute%60:02d}"
          f"-{args.end_minute//60:02d}:{args.end_minute%60:02d} every {args.step_minute} min"
          f"  ({steps_per_day} steps/day)")
    print(f"  tasks           : {args.shard_count * len(dates)}")
    print(f"  emit raw        : {args.emit_raw}"
          + ("   <-- ~53x write volume" if args.emit_raw else "   (combiner only)"))
    print()

    conn = None
    try:
        conn = psycopg2.connect(**db_config(args))
        conn.autocommit = False
        cur = conn.cursor()

        # ---- 1. Preconditions ------------------------------------------------
        cur.execute("""
            SELECT
              (SELECT count(*) FROM meo_edges),
              (SELECT count(*) FROM meo_sample_points)
        """)
        n_edges, n_samples = cur.fetchone()
        print(f"  graph           : {n_edges:,} edges, {n_samples:,} sample points")

        if n_edges == 0 or n_samples == 0:
            return fail("meo_edges / meo_sample_points is empty. "
                        "Run db_pipeline_initializer.py and the sample-point export first.")

        if args.shard_count > n_edges:
            warn(f"--shard-count ({args.shard_count}) exceeds edge count ({n_edges}); "
                 f"{args.shard_count - n_edges} shards will be empty (harmless).")

        # ---- 2. Partitions ---------------------------------------------------
        # Must exist before any worker COPYs, or the insert fails with
        # "no partition of relation found for row".
        years = sorted({d.year for d in dates})
        if not args.dry_run:
            cur.execute("SELECT meo_provision_partitions(%s, %s);", (min(years), max(years)))
            made = cur.fetchone()[0]
            print(f"  partitions      : {made} created ({min(years)}..{max(years)})")

        # ---- 3. Shard map ----------------------------------------------------
        if not args.dry_run:
            cur.execute("SELECT meo_rebuild_edge_shards(%s);", (args.shard_count,))
            mapped = cur.fetchone()[0]
            print(f"  shard map       : {mapped:,} edges mapped to {args.shard_count} shards")

        # Per-shard sample counts, for the cost estimate. Queried rather than
        # assumed uniform: the hash distributes edges evenly, but edges have very
        # different sample counts (a 400 m avenue vs a 20 m alley), so shard
        # workloads are NOT uniform.
        if args.dry_run:
            per_shard = {i: n_samples // args.shard_count for i in range(args.shard_count)}
        else:
            cur.execute("""
                SELECT es.shard_index, count(sp.id)
                FROM meo_edge_shards es
                LEFT JOIN meo_sample_points sp ON sp.edge_id = es.edge_id
                WHERE es.shard_count = %s
                GROUP BY es.shard_index
                ORDER BY es.shard_index
            """, (args.shard_count,))
            per_shard = {r[0]: r[1] for r in cur.fetchall()}
            for i in range(args.shard_count):
                per_shard.setdefault(i, 0)

        counts = [per_shard[i] for i in range(args.shard_count)]
        if counts and max(counts) > 0:
            imbalance = max(counts) / max(1, (sum(counts) / len(counts)))
            print(f"  shard balance   : min={min(counts):,} max={max(counts):,} "
                  f"mean={sum(counts)//len(counts):,}  (max/mean = {imbalance:.2f}x)")
            if imbalance > 1.5:
                warn(f"shard imbalance {imbalance:.2f}x — the slowest shard sets the makespan. "
                     "Consider a higher --shard-count so the hash averages out.")

        # ---- 4. Run row ------------------------------------------------------
        run_config = {
            "global_elevation": f"{args.global_elevation:g}",
            "sun_angle_threshold": f"{args.sun_angle_threshold:g}",
            "city": args.city,
            "step_minute": args.step_minute,
            "start_minute": args.start_minute,
            "end_minute": args.end_minute,
            "emit_raw": args.emit_raw,
            "planned_at": datetime.now(timezone.utc).isoformat(),
        }

        if not args.dry_run:
            cur.execute("""
                INSERT INTO meo_runs (run_id, shard_count, config, notes, started_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE
                  SET shard_count = EXCLUDED.shard_count,
                      config      = EXCLUDED.config,
                      started_at  = COALESCE(meo_runs.started_at, now())
            """, (args.run_id, args.shard_count, json.dumps(run_config),
                  f"{len(dates)} dates x {args.shard_count} shards"))

            if args.reset:
                cur.execute("DELETE FROM meo_tasks WHERE run_id = %s", (args.run_id,))
                print(f"  reset           : deleted {cur.rowcount} existing task(s)")

        # ---- 5. Tasks --------------------------------------------------------
        rows = []
        for d in dates:
            for shard in range(args.shard_count):
                est = estimate_raycasts(per_shard[shard], d,
                                        args.start_minute, args.end_minute, args.step_minute)
                rows.append((
                    args.run_id, shard, args.shard_count, d,
                    args.start_minute, args.end_minute, args.step_minute,
                    args.emit_raw, est, args.max_attempts,
                ))

        total_est = sum(r[8] for r in rows)
        print(f"  est. raycasts   : {total_est:,}")
        print()

        # Show the LPT dispatch order — the first tasks out of the queue.
        top = sorted(rows, key=lambda r: -r[8])[:5]
        print("  dispatch order (longest-processing-time first):")
        for r in top:
            print(f"    shard {r[1]:>3}  {r[3]}  est {r[8]:>12,} raycasts")
        print()

        if args.dry_run:
            print("  DRY RUN — nothing written.")
            conn.rollback()
            return 0

        # ON CONFLICT DO NOTHING makes re-planning safe: an interrupted plan can be
        # re-run without duplicating tasks or resetting completed ones.
        psycopg2.extras.execute_values(cur, """
            INSERT INTO meo_tasks
              (run_id, shard_index, shard_count, sim_date,
               start_minute, end_minute, step_minute,
               emit_raw, est_raycasts, max_attempts)
            VALUES %s
            ON CONFLICT (run_id, shard_index, sim_date) DO NOTHING
        """, rows, page_size=1000)
        inserted = cur.rowcount

        conn.commit()

        cur.execute("""
            SELECT tasks_total, tasks_pending, tasks_done, tasks_failed
            FROM meo_run_progress WHERE run_id = %s
        """, (args.run_id,))
        total, pending, done, failed = cur.fetchone()

        print(f"  inserted        : {inserted:,} new task(s)")
        print(f"  queue state     : {total:,} total / {pending:,} pending / "
              f"{done:,} done / {failed:,} failed")
        print()
        print("=" * 74)
        print("  Ready. Start the fleet:")
        print(f"    kubectl -n sunlightcity apply -k distributed/k8s/")
        print(f"    kubectl -n sunlightcity logs -f job/sunlit-map")
        print(f"    python monitor.py --run-id {args.run_id} --watch")
        print("=" * 74)
        return 0

    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        return fail(f"database error: {e}")
    finally:
        if conn:
            conn.close()


def warn(msg: str) -> None:
    print(f"  WARNING: {msg}")


def fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
