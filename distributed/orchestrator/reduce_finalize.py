#!/usr/bin/env python3
"""
THE REDUCE PHASE — finalises a distributed run.

Runs once after the map fleet drains. Deliberately thin, and it is worth being
explicit about why, because in a classic MapReduce this is where the shuffle lives
and where most of the cost is:

    A SECTION OWNS WHOLE EDGES, assigned by edge midpoint. So every sample point of
    a given edge is in one section, every section is on one shard, and the rollup
    GROUP BY (edge_id, datetime) is COMPLETE within a single instance.

There is therefore nothing to merge across shards: no shuffle, no barrier, no
coordinator gathering partial sums. Ten instances each aggregate their own sixth of
a billion rows, in parallel, and stop. Had sections been defined by sample-point
position instead, ~12% of edges would straddle a boundary and this script would have
needed a distributed sum — and every routing query would have become a cross-shard
join for the rest of the dataset's life.

    verify completeness -> per-shard rollup + index + ANALYZE -> federation -> report

USAGE
    python reduce_finalize.py --run-id run-2026-annual
    python reduce_finalize.py --run-id run-2026-annual --verify-only
    python reduce_finalize.py --run-id run-2026-annual --shards 3,7   # just these two

EXIT CODES
    0  finalised successfully
    1  operational failure (DB unreachable, SQL error)
    2  run is INCOMPLETE — tasks still pending/running/failed
    3  run completed but failed a data-integrity check
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
from datetime import timedelta

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")

from cluster import ClusterEndpoints
import model


EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE, EXIT_INTEGRITY = 0, 1, 2, 3


def coord_config(args) -> dict:
    return {
        "host": args.coord_host or os.environ.get("SUNLIT_COORD_HOST", "localhost"),
        "port": int(args.coord_port or os.environ.get("SUNLIT_COORD_PORT", 5432)),
        "database": args.coord_db or os.environ.get("SUNLIT_COORD_DB", "sunlit_coord"),
        "user": args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        "password": args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    }


def hr(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(f"  {title}")
        print("=" * 78)


def human(n: float) -> str:
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}"
        n /= 1000.0
    return f"{n:.1f}T"


# ---------------------------------------------------------------------------
# 1. COMPLETENESS — checked on the coordinator, before anything expensive
# ---------------------------------------------------------------------------
def verify_completeness(cur, run_id: str) -> tuple[bool, dict]:
    """
    A run is finalisable only when every task reached 'done'.

    Rolling up an incomplete dataset is worse than useless: it costs real time and
    produces something that LOOKS finished. A missing section-window shows up much
    later as a street with no shade at any hour, which is indistinguishable from a
    genuinely sunny street.
    """
    cur.execute("""
        SELECT tasks_total, tasks_pending, tasks_running, tasks_done, tasks_failed,
               rows_written, raycasts_done, raycasts_planned, avg_task_seconds,
               pct_affinity_hit, shard_count, section_count
        FROM meo_run_progress WHERE run_id = %s
    """, (run_id,))
    row = cur.fetchone()
    if not row:
        print(f"  ERROR: run '{run_id}' not found in meo_runs.")
        return False, {}

    (total, pending, running, done, failed, rows, rays, rays_planned,
     avg_s, affinity, shard_count, section_count) = row

    # int()/float() are not decoration. SUM() over a bigint column returns NUMERIC,
    # which psycopg2 hands back as decimal.Decimal, and Decimal refuses to divide by
    # a float — so an unguarded `rays / elapsed` in the throughput report is a
    # TypeError that only appears once a run has actually done work.
    stats = dict(total=total, pending=pending, running=running, done=done, failed=failed,
                 rows=int(rows or 0), rays=int(rays or 0),
                 rays_planned=int(rays_planned or 0),
                 avg_task_seconds=float(avg_s) if avg_s is not None else None,
                 affinity=float(affinity) if affinity is not None else None,
                 shard_count=shard_count, section_count=section_count)

    print(f"  topology        : {shard_count} shards, {section_count} sections")
    print(f"  tasks           : {total:,} total")
    print(f"                    {done:,} done | {pending:,} pending | "
          f"{running:,} running | {failed:,} failed")
    print(f"  rows written    : {stats['rows']:,}")
    print(f"  raycasts        : {stats['rays']:,} (planned {stats['rays_planned']:,})")
    if avg_s:
        print(f"  mean task time  : {stats['avg_task_seconds']:.2f}s")
    if affinity is not None:
        print(f"  affinity hits   : {stats['affinity']:.1f}%  "
              f"(working sets loaded {100 - stats['affinity']:.0f}% of the time)")

    if total == 0:
        print("  ERROR: run has no tasks. Did plan_tasks.py run?")
        return False, stats

    if pending or running or failed:
        print()
        print("  RUN IS INCOMPLETE — refusing to finalise.")
        cur.execute("""
            SELECT shard_index, section_id, window_index, sim_date, state, attempts,
                   left(coalesce(last_error, ''), 70)
            FROM meo_tasks
            WHERE run_id = %s AND state <> 'done'
            ORDER BY state, shard_index, sim_date, section_id
            LIMIT 20
        """, (run_id,))
        outstanding = cur.fetchall()
        print(f"  outstanding (first {len(outstanding)}):")
        for sh, sec, win, d, state, attempts, err in outstanding:
            print(f"    shard {sh:>3} section {sec:>5} w{win} {d} {state:<8} "
                  f"attempt {attempts}" + (f"  {err}" if err else ""))
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
# 2. PER-SHARD WORK — the parallel part
# ---------------------------------------------------------------------------
def finalise_shard(shard_index: int, dsn: dict, sql_dir: str,
                   steps_per_window: int, do_rollup: bool,
                   do_indexes: bool) -> dict:
    """
    Verifies, rolls up, indexes and analyses ONE shard.

    Called concurrently for all ten. Each shard is a separate PostgreSQL instance
    with its own cores, page cache and WAL, so there is no contention between them —
    which is precisely why the reduce phase costs one shard's work rather than ten.

    Returns a result dict rather than raising, so one unreachable instance reports
    itself instead of aborting the other nine.
    """
    out = {"shard": shard_index, "ok": False, "error": None,
           "leaf_problems": [], "violations": [], "rollup_rows": 0,
           "seconds": 0.0, "samples_size": "?", "edges_size": "?", "edge_rows": 0}
    t0 = time.time()

    try:
        conn = psycopg2.connect(**dsn)
    except psycopg2.Error as e:
        out["error"] = f"unreachable: {str(e).splitlines()[0]}"
        return out

    try:
        conn.autocommit = True
        cur = conn.cursor()

        # Guard: refuse to operate on the wrong instance. "Ran 05 against shard 3
        # twice and shard 7 never" is otherwise a silent, expensive mistake.
        cur.execute("SELECT shard_index FROM meo_shard_identity")
        actual = cur.fetchone()
        if not actual or actual[0] != shard_index:
            out["error"] = (f"identity mismatch: expected shard {shard_index}, "
                            f"instance says {actual[0] if actual else 'unset'}")
            return out

        # ---- Leaf inventory: exact row counts against expectation -----------
        # Catches a task that completed but wrote partial output, which the queue
        # cannot detect on its own.
        cur.execute("SELECT * FROM meo_verify_leaf_sizes(%s)", (steps_per_window,))
        out["leaf_problems"] = cur.fetchall()

        # ---- Rollup ---------------------------------------------------------
        if do_rollup:
            cur.execute("SET work_mem = '512MB'")
            cur.execute("SELECT coalesce(sum(rows_written), 0) FROM meo_rollup_all_edges()")
            out["rollup_rows"] = cur.fetchone()[0]

        # ---- Indexes, statistics --------------------------------------------
        if do_indexes:
            path = os.path.join(sql_dir, "05_post_load_indexes.sql")
            if not os.path.exists(path):
                out["error"] = f"SQL file not found: {path}"
                return out
            sql = open(path, encoding="utf-8").read()
            # psql meta-commands (\echo, \set, SET at file scope is fine) are not
            # valid over a normal connection.
            sql = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("\\"))
            cur.execute(sql)

        # ---- Integrity ------------------------------------------------------
        cur.execute("SELECT check_name, violations FROM meo_integrity_edges")
        out["violations"] = [(n, v) for n, v in cur.fetchall() if v]

        cur.execute("SELECT samples_size, edges_size, edge_rows FROM meo_shard_summary")
        s = cur.fetchone()
        if s:
            out["samples_size"], out["edges_size"], out["edge_rows"] = s

        out["ok"] = out["error"] is None
        return out

    except psycopg2.Error as e:
        out["error"] = str(e).splitlines()[0]
        return out
    finally:
        out["seconds"] = time.time() - t0
        conn.close()


# ---------------------------------------------------------------------------
# 3. FEDERATION
# ---------------------------------------------------------------------------
def refresh_federation(cur, password: str) -> None:
    """
    Points the coordinator's postgres_fdw servers at the shards and rebuilds the
    foreign partitions of meo_exposure_edges_fed.

    Analytics only. The routing hot path connects DIRECTLY to the owning shard — see
    the header of 06_serving_federation.sql for why putting thousands of per-request
    queries through one instance would be the wrong shape.
    """
    # Guard with an actionable message. Without it the failure is
    # `function meo_setup_federation(unknown) does not exist`, which is accurate and
    # tells the operator nothing about which of six SQL files they skipped — and it
    # arrives AFTER the expensive per-shard work has already succeeded.
    cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'meo_setup_federation'")
    if cur.fetchone()[0] == 0:
        raise RuntimeError(
            "the coordinator has no federation functions — 06_serving_federation.sql "
            "has not been applied. Run it, or pass --no-federation to finalise the "
            "shards without building the analytics view (the routing path does not "
            "depend on it).")

    cur.execute("SELECT meo_setup_federation(%s)", (password,))
    created = cur.fetchone()[0]
    cur.execute("SELECT meo_refresh_federation()")
    parts = cur.fetchone()[0]
    print(f"  foreign servers : {created} created, {parts} partition(s) attached")

    cur.execute("SELECT shards_touched, routes, pct FROM meo_route_locality(200, 12)")
    rows = cur.fetchall()
    if rows:
        print("  route locality  : shards touched by a sampled 12-edge route")
        for touched, routes, pct in rows:
            print(f"                    {touched} shard(s): {routes:>4} routes ({pct}%)")


def report_throughput(cur, run_id: str, stats: dict, reduce_seconds: float) -> None:
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
    rows = stats.get("rows", 0) or 0

    # THREE distinctions this report used to get wrong, all of which the docs make a
    # point of, so a report contradicting them is worse than no report:
    #
    #   1. `wall` is measured from the FIRST task claim to the LAST task finish, so it
    #      does NOT include fleet spin-up. The modelled end-to-end figure does. Adding
    #      the modelled spin-up is what makes the two comparable.
    #   2. ROWS are not RAYCASTS (they differ by ~37%: the horizon guard resolves the
    #      rest without touching the BVH). Quoting a raycast rate against v1's ROW rate
    #      compared two different quantities.
    #   3. The speedup must be WORK-NORMALISED. v1's 6 h covered 12 dates; this run
    #      covers 60. Dividing by V1_SECONDS compared different amounts of work and
    #      understated the result by 5x.
    observed = float(wall) + reduce_seconds
    end_to_end = observed + model.FLEET_STARTUP_SECONDS

    print(f"  map wall clock  : {timedelta(seconds=int(wall))}  "
          f"({t0:%H:%M:%S} -> {t1:%H:%M:%S})")
    print(f"  reduce          : {timedelta(seconds=int(reduce_seconds))}  "
          f"({stats['shard_count']} shards in parallel)")
    print(f"  observed        : {timedelta(seconds=int(observed))}  "
          f"(first claim -> finalised; excludes spin-up)")
    print(f"  + spin-up       : {timedelta(seconds=int(model.FLEET_STARTUP_SECONDS))}  "
          f"(modelled; the queue cannot see it)")
    print(f"  end-to-end      : {timedelta(seconds=int(end_to_end))}  "
          f"(model predicts {model.fmt(model.total_seconds())})")
    print(f"  vs the deadline : {model.TARGET_SECONDS / 60:.0f} min target, "
          f"{100 * (1 - end_to_end / model.TARGET_SECONDS):+.1f}% margin"
          f"{'' if end_to_end <= model.TARGET_SECONDS else '   *** OVER ***'}")
    print(f"  distinct workers: {workers}")

    if rows:
        row_rate = rows / float(wall)
        print(f"  throughput      : {human(row_rate)} rows/s "
              f"({human(row_rate * 3600)}/hour)")
        print(f"  per worker      : {human(row_rate / max(1, workers))} rows/s  "
              f"(v1 single-thread baseline {human(model.V1_ROW_RATE)}/s, "
              f"model {human(model.WORKER_ROW_RATE)}/s)")
    if rays:
        print(f"  raycast rate    : {human(rays / float(wall))}/s  "
              f"({100 * rays / max(1, rows):.1f}% of rows touched the BVH)")
    # Work-normalised: what v1 would have needed for THIS run's rows, at its measured
    # sustained rate. See model.v1_equivalent_seconds().
    equiv = model.v1_equivalent_seconds(rows or model.EXPOSURE_ROWS)
    print(f"  vs v1           : {equiv / end_to_end:.1f}x work-normalised  "
          f"(v1 would need {model.fmt(equiv)} for these {rows:,} rows; "
          f"model predicts {model.speedup():.1f}x)")


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--verify-only", action="store_true", help="check and exit; change nothing")
    p.add_argument("--no-rollup", action="store_true", help="skip deriving meo_exposure_edges")
    p.add_argument("--no-indexes", action="store_true", help="skip index + ANALYZE")
    p.add_argument("--no-federation", action="store_true", help="skip the postgres_fdw setup")
    p.add_argument("--shards", default=None,
                   help="comma-separated subset to finalise (default: all registered)")
    p.add_argument("--jobs", type=int, default=0,
                   help="concurrent shards (default: all of them — they are separate hosts)")
    p.add_argument("--force", action="store_true",
                   help="finalise even if the run is incomplete (NOT recommended)")
    p.add_argument("--sql-dir",
                   default=os.environ.get("SUNLIT_SQL_DIR") or
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db"))
    p.add_argument("--coord-host"); p.add_argument("--coord-port")
    p.add_argument("--coord-db"); p.add_argument("--db-user"); p.add_argument("--db-password")
    args = p.parse_args()

    sql_dir = os.path.normpath(args.sql_dir)
    conn = None
    try:
        conn = psycopg2.connect(**coord_config(args))
        conn.autocommit = True
        cur = conn.cursor()

        hr(f"REDUCE — run '{args.run_id}'")
        complete, stats = verify_completeness(cur, args.run_id)

        if not complete:
            if not args.force:
                print("\n  Pass --force to finalise anyway (you will get a partial dataset).")
                return EXIT_INCOMPLETE
            print("\n  --force given: continuing despite an incomplete run.")

        # Window geometry, read from the run rather than assumed, so a run planned
        # with different windows still verifies against the right expectation.
        cur.execute("SELECT config FROM meo_runs WHERE run_id = %s", (args.run_id,))
        cfg = cur.fetchone()[0] or {}
        span = int(cfg.get("end_minute", model.END_MINUTE)) - \
               int(cfg.get("start_minute", model.START_MINUTE))
        windows = int(cfg.get("windows", model.TIME_WINDOWS))
        step = int(cfg.get("step_minute", model.STEP_MINUTE))
        steps_per_window = (span // windows) // step

        # ---- Which shards ---------------------------------------------------
        if args.shards:
            wanted = [int(x) for x in args.shards.split(",")]
        else:
            cur.execute("SELECT shard_index FROM meo_shards ORDER BY shard_index")
            wanted = [r[0] for r in cur.fetchall()]

        if not wanted:
            print("\n  ERROR: no shards registered. Did plan_tasks.py run?")
            return EXIT_ERROR

        endpoints = ClusterEndpoints.from_environment()
        password = coord_config(args)["password"]
        dsns = {}
        for i in wanted:
            d = endpoints.shard(i)
            dsns[i] = dict(host=d["host"], port=d["port"], dbname=d["dbname"],
                           user=d["user"], password=password)

        if args.verify_only:
            hr("VERIFY ONLY — no changes")
            do_rollup = do_indexes = False
        else:
            do_rollup = not args.no_rollup
            do_indexes = not args.no_indexes
            hr(f"PER-SHARD REDUCE — {len(wanted)} shard(s) in parallel")
            print(f"  expecting {steps_per_window} timesteps per (section, window) leaf")

        # All shards at once by default. They are separate hosts, so the only thing
        # this consumes locally is one thread and one socket each — and doing them
        # serially would multiply the reduce phase by the shard count, which is the
        # whole cost the cluster exists to divide.
        jobs = args.jobs or len(wanted)
        t0 = time.time()
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(finalise_shard, i, dsns[i], sql_dir,
                            steps_per_window, do_rollup, do_indexes): i
                for i in wanted
            }
            for fut in concurrent.futures.as_completed(futures):
                r = fut.result()
                results.append(r)
                if r["ok"]:
                    print(f"  shard {r['shard']:>2} : OK   {r['seconds']:>6.1f}s  "
                          f"rollup {r['rollup_rows']:>10,} rows  "
                          f"samples {r['samples_size']:>10}  edges {r['edges_size']:>9}")
                else:
                    print(f"  shard {r['shard']:>2} : FAIL {r['seconds']:>6.1f}s  {r['error']}")
        reduce_seconds = time.time() - t0
        results.sort(key=lambda r: r["shard"])

        # ---- Integrity ------------------------------------------------------
        hr("INTEGRITY")
        bad = False
        for r in results:
            if r["error"]:
                print(f"  shard {r['shard']:>2}  UNREACHABLE / MISMATCH — {r['error']}")
                bad = True
                continue
            if r["leaf_problems"]:
                print(f"  shard {r['shard']:>2}  FAIL  {len(r['leaf_problems'])} leaf/leaves "
                      "with the wrong row count (a task wrote partial output):")
                for leaf, sec, actual, expect, delta in r["leaf_problems"][:5]:
                    print(f"            {leaf}  got {actual:,} expected {expect:,} ({delta:+,})")
                bad = True
            if r["violations"]:
                for name, n in r["violations"]:
                    # An owned edge with no exposure rows is a warning, not a failure:
                    # it is legitimate when a run deliberately covered a subset.
                    severe = "no exposure rows" not in name
                    print(f"  shard {r['shard']:>2}  {'FAIL' if severe else 'WARN'}  "
                          f"{name}: {n:,}")
                    bad = bad or severe
            if not r["leaf_problems"] and not r["violations"]:
                print(f"  shard {r['shard']:>2}  OK    leaves correctly sized, "
                      f"no integrity violations")

        if bad:
            print("\n  Integrity checks FAILED — not finalising.")
            return EXIT_INTEGRITY

        if args.verify_only:
            hr("VERIFY-ONLY — no changes made")
            return EXIT_OK

        # ---- Federation -----------------------------------------------------
        if not args.no_federation:
            hr("SERVING FEDERATION")
            refresh_federation(cur, password)

        # ---- Report ---------------------------------------------------------
        hr("STORAGE")
        total_edge_rows = sum(r["edge_rows"] for r in results)
        print(f"  {'shard':>6} {'samples':>12} {'edge index':>12} {'edge rows':>12}")
        print("  " + "-" * 46)
        for r in results:
            print(f"  {r['shard']:>6} {r['samples_size']:>12} {r['edges_size']:>12} "
                  f"{r['edge_rows']:>12,}")
        print(f"  {'total':>6} {'':>12} {'':>12} {total_edge_rows:>12,}")

        hr("THROUGHPUT")
        report_throughput(cur, args.run_id, stats, reduce_seconds)

        cur.execute("UPDATE meo_runs SET finished_at = now() WHERE run_id = %s",
                    (args.run_id,))

        hr("DONE")
        print("  Dataset finalised.")
        print("  Switch every shard to the serving profile before exposing it:")
        print("    include 'postgresql.shard.serving.conf'  &&  pg_ctl restart")
        print("  Then take base backups — the bulk profile could not produce one.")
        print()
        print("  The routing contract downstream should use:")
        print("    coordinator:  SELECT * FROM meo_edge_shard(:edge_id);")
        print("    that shard:   SELECT * FROM meo_edge_directional_cost(")
        print("                    :edge_id, :entry_time, :reverse, :walk_speed);")
        print()
        return EXIT_OK

    except psycopg2.Error as e:
        print(f"\nERROR: database error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
