#!/usr/bin/env python3
"""
THE REDUCE PHASE — finalises a distributed run.

Runs once after the map fleet drains. Deliberately thin, because the map-side
combiner already did the aggregation: sharding by EDGE means each worker's
per-(edge, timestep) sum is final, so there is no shuffle and nothing to merge.
What remains is verification and index/statistics work.

    verify completeness -> build indexes -> ANALYZE -> refresh rollups -> report

USAGE
    python reduce_finalize.py --run-id run-2026-annual --verify --build-indexes --refresh-rollups
    python reduce_finalize.py --run-id run-2026-annual --verify-only

EXIT CODES
    0  finalised successfully
    1  operational failure (DB unreachable, SQL error)
    2  run is INCOMPLETE — tasks still pending/running/failed
    3  run completed but failed a data-integrity check
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import timedelta

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")


EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE, EXIT_INTEGRITY = 0, 1, 2, 3


def db_config(args) -> dict:
    return {
        "host": args.db_host or os.environ.get("SUNLIT_DB_HOST", "localhost"),
        "port": int(args.db_port or os.environ.get("SUNLIT_DB_PORT", 5432)),
        "database": args.db_name or os.environ.get("SUNLIT_DB_NAME", "city_data"),
        "user": args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        "password": args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    }


def hr(title: str = "") -> None:
    print("\n" + "=" * 74)
    if title:
        print(f"  {title}")
        print("=" * 74)


def human(n: int) -> str:
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}"
        n /= 1000.0
    return f"{n:.1f}T"


# ---------------------------------------------------------------------------
# 1. COMPLETENESS
# ---------------------------------------------------------------------------
def verify_completeness(cur, run_id: str) -> tuple[bool, dict]:
    """
    A run is finalisable only when every task reached 'done'.

    Building indexes over an incomplete dataset is worse than useless: it is
    expensive, and it produces a dataset that LOOKS finished. Any gap here must
    stop the pipeline.
    """
    cur.execute("""
        SELECT tasks_total, tasks_pending, tasks_running, tasks_done, tasks_failed,
               rows_written, raycasts_done, raycasts_planned, avg_task_seconds
        FROM meo_run_progress WHERE run_id = %s
    """, (run_id,))
    row = cur.fetchone()
    if not row:
        print(f"  ERROR: run '{run_id}' not found in meo_runs.")
        return False, {}

    total, pending, running, done, failed, rows, rays, rays_planned, avg_s = row
    stats = dict(total=total, pending=pending, running=running, done=done,
                 failed=failed, rows=rows or 0, rays=rays or 0,
                 rays_planned=rays_planned or 0, avg_task_seconds=avg_s)

    print(f"  tasks           : {total:,} total")
    print(f"                    {done:,} done | {pending:,} pending | "
          f"{running:,} running | {failed:,} failed")
    print(f"  rows written    : {stats['rows']:,}")
    print(f"  raycasts        : {stats['rays']:,} "
          f"(planned {stats['rays_planned']:,})")
    if avg_s:
        print(f"  mean task time  : {avg_s:.1f}s")

    if total == 0:
        print("  ERROR: run has no tasks. Did plan_tasks.py run?")
        return False, stats

    if pending or running or failed:
        print()
        print(f"  RUN IS INCOMPLETE — refusing to finalise.")
        cur.execute("""
            SELECT shard_index, sim_date, state, attempts,
                   left(coalesce(last_error, ''), 90)
            FROM meo_tasks
            WHERE run_id = %s AND state <> 'done'
            ORDER BY state, sim_date, shard_index
            LIMIT 20
        """, (run_id,))
        outstanding = cur.fetchall()
        print(f"  outstanding (first {len(outstanding)}):")
        for shard, d, state, attempts, err in outstanding:
            print(f"    shard {shard:>3} {d} {state:<8} attempt {attempts}"
                  + (f"  {err}" if err else ""))
        print()
        if failed:
            print("  'failed' tasks exhausted max_attempts and need investigation.")
            print("  To retry them after fixing the cause:")
            print(f"    UPDATE meo_tasks SET state='pending', attempts=0")
            print(f"     WHERE run_id='{run_id}' AND state='failed';")
        return False, stats

    print("\n  COMPLETE — every task reached 'done'.")
    return True, stats


# ---------------------------------------------------------------------------
# 2. INTEGRITY
# ---------------------------------------------------------------------------
def verify_integrity(cur, run_id: str) -> bool:
    """
    Data-level checks that completeness alone cannot catch. Each is cheap and each
    corresponds to a specific way the pipeline could be subtly wrong.
    """
    ok = True

    # (a) sunlit_sum must never exceed sample_count. A breach means the
    #     combiner's accumulator indexing is wrong — the single most dangerous
    #     class of bug here, because the data would still look plausible.
    cur.execute("""
        SELECT count(*) FROM meo_exposure_edges_p
        WHERE sunlit_sum > sample_count
    """)
    bad = cur.fetchone()[0]
    if bad:
        print(f"  FAIL  {bad:,} row(s) with sunlit_sum > sample_count "
              "(combiner accumulator indexing is broken)")
        ok = False
    else:
        print("  OK    sunlit_sum <= sample_count on every row")

    # (b) No negative counts.
    cur.execute("SELECT count(*) FROM meo_exposure_edges_p WHERE sunlit_sum < 0")
    bad = cur.fetchone()[0]
    if bad:
        print(f"  FAIL  {bad:,} row(s) with negative sunlit_sum")
        ok = False
    else:
        print("  OK    no negative sunlit_sum")

    # (c) Every edge that has samples should have exposure rows. A missing edge
    #     means a shard silently produced nothing.
    cur.execute("""
        SELECT count(*) FROM (
            SELECT DISTINCT sp.edge_id
            FROM meo_sample_points sp
            WHERE NOT EXISTS (
                SELECT 1 FROM meo_exposure_edges_p e WHERE e.edge_id = sp.edge_id
            )
        ) missing
    """)
    missing = cur.fetchone()[0]
    if missing:
        print(f"  WARN  {missing:,} edge(s) with sample points but no exposure rows")
        # Warning, not failure: legitimately possible if the run covered a subset
        # of the graph.
    else:
        print("  OK    every sampled edge has exposure rows")

    # (d) Row count matches expectation: sum over tasks of
    #     edges_in_shard x timesteps. Catches a partially-flushed task.
    cur.execute("""
        WITH expect AS (
            SELECT t.sim_date,
                   sum(((t.end_minute - t.start_minute) / t.step_minute + 1)
                       * (SELECT count(*) FROM meo_edge_shards es
                          WHERE es.shard_count = t.shard_count
                            AND es.shard_index = t.shard_index)) AS expected
            FROM meo_tasks t
            WHERE t.run_id = %s AND t.state = 'done'
            GROUP BY t.sim_date
        ),
        actual AS (
            SELECT datetime::date AS sim_date, count(*) AS got
            FROM meo_exposure_edges_p
            GROUP BY datetime::date
        )
        SELECT e.sim_date, e.expected, coalesce(a.got, 0)
        FROM expect e LEFT JOIN actual a USING (sim_date)
        WHERE e.expected <> coalesce(a.got, 0)
        ORDER BY e.sim_date
        LIMIT 10
    """, (run_id,))
    mismatches = cur.fetchall()
    if mismatches:
        print(f"  WARN  row-count mismatch on {len(mismatches)} date(s):")
        for d, expected, got in mismatches:
            delta = got - expected
            print(f"          {d}  expected {expected:,}  got {got:,}  ({delta:+,})")
        print("        (a surplus is normal if an earlier run wrote the same dates)")
    else:
        print("  OK    row counts match expected edges x timesteps per date")

    return ok


# ---------------------------------------------------------------------------
# 3. INDEXES / STATS / ROLLUPS
# ---------------------------------------------------------------------------
def run_sql_file(cur, path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"SQL file not found: {path}")
    sql = open(path, encoding="utf-8").read()
    # psql meta-commands are not valid over a normal connection.
    lines = [l for l in sql.splitlines()
             if not l.strip().startswith("\\")]
    cur.execute("\n".join(lines))


def build_indexes(cur, sql_dir: str) -> None:
    """
    Delegates to 04_post_load_indexes.sql so the DDL lives in exactly one place —
    runnable by hand via psql, or here.

    Session-scoped memory bump: the index build is one large sort, and
    maintenance_work_mem is the single biggest lever on how long it takes.
    """
    cur.execute("SET maintenance_work_mem = '8GB'")
    cur.execute("SET max_parallel_maintenance_workers = 8")

    path = os.path.join(sql_dir, "04_post_load_indexes.sql")
    print(f"  running {path} …")
    t0 = time.time()
    run_sql_file(cur, path)
    print(f"  done in {timedelta(seconds=int(time.time() - t0))}")


def refresh_rollups(cur) -> None:
    t0 = time.time()
    # Not CONCURRENTLY: that requires a pre-existing unique index AND leaves the
    # old contents queryable, which we do not need since nothing is reading yet.
    # The plain form is substantially faster.
    cur.execute("REFRESH MATERIALIZED VIEW meo_edge_daily_exposure")
    cur.execute("SELECT count(*) FROM meo_edge_daily_exposure")
    n = cur.fetchone()[0]
    print(f"  meo_edge_daily_exposure: {n:,} rows "
          f"({timedelta(seconds=int(time.time() - t0))})")


def report_sizes(cur) -> None:
    cur.execute("""
        SELECT c.relname,
               pg_size_pretty(pg_total_relation_size(c.oid)),
               pg_total_relation_size(c.oid),
               COALESCE(s.n_live_tup, 0)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'm')
          AND c.relname LIKE 'meo_%'
          AND c.relname NOT LIKE 'meo_stage_%'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT 14
    """)
    print(f"  {'relation':<38} {'size':>10}  {'rows':>14}")
    print(f"  {'-'*38} {'-'*10}  {'-'*14}")
    for name, pretty, _raw, rows in cur.fetchall():
        print(f"  {name:<38} {pretty:>10}  {rows:>14,}")


def report_throughput(cur, run_id: str, stats: dict) -> None:
    cur.execute("""
        SELECT min(started_at), max(finished_at),
               EXTRACT(EPOCH FROM (max(finished_at) - min(started_at))),
               count(DISTINCT worker_id)
        FROM meo_tasks WHERE run_id = %s AND state = 'done'
    """, (run_id,))
    t0, t1, wall, workers = cur.fetchone()
    if not wall or wall <= 0:
        return

    rays = stats.get("rays", 0) or 0
    print(f"  wall clock      : {timedelta(seconds=int(wall))}  ({t0:%H:%M:%S} -> {t1:%H:%M:%S})")
    print(f"  distinct workers: {workers}")
    if rays:
        print(f"  throughput      : {human(rays / wall)} raycasts/s "
              f"({human(rays / wall * 3600)}/hour)")
        # Single-node reference: 1.577e9 raycasts in 6 h = ~73k/s.
        speedup = (rays / wall) / 73000.0
        print(f"  vs single node  : {speedup:.1f}x  (baseline 73k raycasts/s)")


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--verify", action="store_true", help="completeness + integrity checks")
    p.add_argument("--verify-only", action="store_true", help="check and exit; change nothing")
    p.add_argument("--build-indexes", action="store_true")
    p.add_argument("--refresh-rollups", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="finalise even if the run is incomplete (NOT recommended)")
    p.add_argument("--sql-dir",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db"))
    p.add_argument("--db-host"); p.add_argument("--db-port")
    p.add_argument("--db-name"); p.add_argument("--db-user"); p.add_argument("--db-password")
    args = p.parse_args()

    # Default to doing everything when no phase flag is given.
    if not any([args.verify, args.verify_only, args.build_indexes, args.refresh_rollups]):
        args.verify = args.build_indexes = args.refresh_rollups = True

    conn = None
    try:
        conn = psycopg2.connect(**db_config(args))
        conn.autocommit = True   # DDL + REFRESH manage their own transactions
        cur = conn.cursor()

        hr(f"REDUCE — run '{args.run_id}'")

        complete, stats = verify_completeness(cur, args.run_id)

        if not complete:
            if not args.force:
                print("\n  Pass --force to finalise anyway (you will get a partial dataset).")
                return EXIT_INCOMPLETE
            print("\n  --force given: continuing despite an incomplete run.")

        if args.verify or args.verify_only:
            hr("INTEGRITY")
            if not verify_integrity(cur, args.run_id):
                print("\n  Integrity checks FAILED — not finalising.")
                return EXIT_INTEGRITY

        if args.verify_only:
            hr("VERIFY-ONLY — no changes made")
            return EXIT_OK

        if args.build_indexes:
            hr("INDEXES + STATISTICS")
            build_indexes(cur, os.path.normpath(args.sql_dir))

        if args.refresh_rollups:
            hr("ROLLUPS")
            refresh_rollups(cur)

        hr("STORAGE")
        report_sizes(cur)

        hr("THROUGHPUT")
        report_throughput(cur, args.run_id, stats)

        cur.execute("UPDATE meo_runs SET finished_at = now() WHERE run_id = %s",
                    (args.run_id,))

        hr("DONE")
        print("  Dataset finalised.")
        print("  Switch PostgreSQL to the serving profile before exposing it:")
        print("    include 'postgresql.serving.conf'  &&  pg_ctl restart")
        print("  Then take a base backup — the bulk profile could not produce one.")
        print()
        return EXIT_OK

    except psycopg2.Error as e:
        print(f"\nERROR: database error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
