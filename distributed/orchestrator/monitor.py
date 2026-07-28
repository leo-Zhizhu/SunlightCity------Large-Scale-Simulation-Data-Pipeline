#!/usr/bin/env python3
"""
Fleet monitor and lease reaper.

Two jobs in one script because they share all their plumbing:

  1. REAP  — return expired leases to the pending pool. This IS the pipeline's
             node-failure recovery. Run it on a schedule (the k8s CronJob does).
  2. WATCH — live progress, throughput and ETA for a human.

USAGE
    python monitor.py --run-id run-2026-annual --watch          # live dashboard
    python monitor.py --run-id run-2026-annual --reap --once    # one reap pass (CronJob)
    python monitor.py --run-id run-2026-annual --once           # one-shot status

WHY LEASE EXPIRY RATHER THAN POD-DEATH WATCHING
-----------------------------------------------
A controller watching pod lifecycle events would be more code, need RBAC, and be
strictly worse at the job. Pod-death events do not fire for a network partition,
a frozen kernel, a hung container, or a worker stuck in an unbounded GC pause.
Lease expiry covers every one of those uniformly, because it observes the only
thing that actually matters: whether progress is still being reported.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")


def db_config(args) -> dict:
    return {
        "host": args.db_host or os.environ.get("SUNLIT_DB_HOST", "localhost"),
        "port": int(args.db_port or os.environ.get("SUNLIT_DB_PORT", 5432)),
        "database": args.db_name or os.environ.get("SUNLIT_DB_NAME", "city_data"),
        "user": args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        "password": args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    }


def human(n: float) -> str:
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.1f}{unit}".replace(".0", "")
        n /= 1000.0
    return f"{n:.1f}T"


def bar(pct: float, width: int = 44) -> str:
    """Progress bar using block glyphs, with 1/8-block sub-cell resolution."""
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = pct / 100.0 * width
    whole = int(filled)
    frac = filled - whole
    partials = " ▏▎▍▌▋▊▉"
    tail = partials[int(frac * 8)] if whole < width else ""
    return "█" * whole + tail + " " * max(0, width - whole - len(tail))


# ---------------------------------------------------------------------------
# REAP
# ---------------------------------------------------------------------------
def reap(cur, run_id: str | None, verbose: bool = True) -> int:
    """Returns tasks whose lease expired to 'pending' (or 'failed' if exhausted)."""
    cur.execute("SELECT * FROM meo_reap_expired_leases(%s)", (run_id,))
    rows = cur.fetchall()
    if rows and verbose:
        print(f"[reap] reclaimed {len(rows)} expired lease(s):")
        for task_id, detail, attempts, new_state in rows:
            print(f"  task#{task_id} -> {new_state} (attempt {attempts}) {detail}")
    elif verbose:
        print("[reap] no expired leases")
    return len(rows)


def drop_orphan_staging(cur, verbose: bool = True) -> int:
    """
    Drops staging tables left behind by workers killed between CREATE and promote.
    Harmless to leave (UNLOGGED tables are emptied on crash recovery) but they
    accumulate in the catalog, and a bloated catalog slows query planning.
    """
    cur.execute("SELECT meo_drop_orphan_staging()")
    n = cur.fetchone()[0]
    if verbose and n:
        print(f"[reap] dropped {n} orphaned staging table(s)")
    return n


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------
def fetch_status(cur, run_id: str) -> dict | None:
    cur.execute("""
        SELECT shard_count, created_at, started_at, finished_at,
               tasks_total, tasks_pending, tasks_running, tasks_done, tasks_failed,
               workers_active, rows_written, raycasts_done, raycasts_planned,
               pct_done, avg_task_seconds
        FROM meo_run_progress WHERE run_id = %s
    """, (run_id,))
    r = cur.fetchone()
    if not r:
        return None
    keys = ("shard_count", "created_at", "started_at", "finished_at",
            "total", "pending", "running", "done", "failed",
            "workers", "rows", "rays", "rays_planned", "pct", "avg_task_s")
    return dict(zip(keys, r))


def render(cur, run_id: str, st: dict, clear: bool) -> None:
    if clear:
        # Home cursor + clear below, rather than a full clear: avoids the flicker
        # of erasing and repainting the whole screen each second.
        sys.stdout.write("\033[H\033[J")

    now = datetime.now()
    pct = float(st["pct"] or 0)

    print("╭" + "─" * 72 + "╮")
    print(f"│ SunlightCity · run {run_id[:40]:<40}{now:%H:%M:%S}" + " " * 11 + "│")
    print("├" + "─" * 72 + "┤")
    print(f"│ {bar(pct)} {pct:6.2f}%  │")
    print("├" + "─" * 72 + "┤")
    print(f"│  done {st['done']:>6,}   running {st['running']:>4,}   "
          f"pending {st['pending']:>6,}   failed {st['failed']:>4,}" + " " * 6 + "│")
    print(f"│  workers active {st['workers'] or 0:>3} / {st['shard_count']:<3}"
          f"          rows written {st['rows'] or 0:>14,}" + " " * 6 + "│")

    # ---- throughput + ETA ----
    rays = st["rays"] or 0
    if st["started_at"]:
        elapsed = (now.astimezone() - st["started_at"]).total_seconds()
        if elapsed > 0 and rays:
            rate = rays / elapsed
            print(f"│  raycasts {human(rays):>10}   rate {human(rate):>8}/s"
                  f"   vs 1-node {rate/73000:>5.1f}x" + " " * 8 + "│")

            planned = st["rays_planned"] or 0
            if planned > rays:
                eta_s = (planned - rays) / max(1.0, rate)
                eta = timedelta(seconds=int(eta_s))
                print(f"│  elapsed {str(timedelta(seconds=int(elapsed))):>12}"
                      f"   ETA {str(eta):>12}"
                      f"   finish ~{(now + eta):%H:%M}" + " " * 5 + "│")

    print("╰" + "─" * 72 + "╯")

    # ---- per-worker detail: shows stragglers ----
    cur.execute("""
        SELECT worker_id, shard_index, sim_date,
               EXTRACT(EPOCH FROM (now() - started_at))::INT,
               EXTRACT(EPOCH FROM (now() - heartbeat_at))::INT,
               raycasts_done
        FROM meo_tasks
        WHERE run_id = %s AND state = 'running'
        ORDER BY started_at
        LIMIT 12
    """, (run_id,))
    running = cur.fetchall()
    if running:
        print(f"\n  {'worker':<26}{'shard':>6}{'date':>12}{'age':>7}{'hb':>6}{'raycasts':>12}")
        print("  " + "─" * 69)
        for w, shard, d, age, hb, rays_w in running:
            # Flag a worker whose heartbeat is going stale — the first visible sign
            # of a pod about to lose its lease.
            flag = " !" if (hb or 0) > 90 else ""
            print(f"  {(w or '?')[:26]:<26}{shard:>6}{str(d):>12}"
                  f"{age or 0:>6}s{hb or 0:>5}s{rays_w or 0:>12,}{flag}")

    # ---- failures ----
    if st["failed"]:
        cur.execute("""
            SELECT shard_index, sim_date, attempts, left(coalesce(last_error,''), 60)
            FROM meo_tasks
            WHERE run_id = %s AND state = 'failed'
            ORDER BY sim_date, shard_index LIMIT 8
        """, (run_id,))
        print(f"\n  FAILED tasks:")
        for shard, d, attempts, err in cur.fetchall():
            print(f"    shard {shard:>3} {d}  attempts {attempts}  {err}")


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--watch", action="store_true", help="live dashboard, refreshing")
    p.add_argument("--once", action="store_true", help="single pass then exit")
    p.add_argument("--interval", type=float, default=5.0, help="watch refresh seconds")
    p.add_argument("--reap", action="store_true", help="reclaim expired leases")
    p.add_argument("--drop-orphan-staging", action="store_true")
    p.add_argument("--quiet", action="store_true", help="suppress the dashboard (reap only)")
    p.add_argument("--db-host"); p.add_argument("--db-port")
    p.add_argument("--db-name"); p.add_argument("--db-user"); p.add_argument("--db-password")
    args = p.parse_args()

    conn = None
    try:
        conn = psycopg2.connect(**db_config(args))
        conn.autocommit = True
        cur = conn.cursor()

        iteration = 0
        while True:
            iteration += 1

            if args.reap:
                reap(cur, args.run_id, verbose=not args.watch)
            if args.drop_orphan_staging:
                drop_orphan_staging(cur, verbose=not args.watch)

            st = fetch_status(cur, args.run_id)
            if st is None:
                print(f"ERROR: run '{args.run_id}' not found. Has plan_tasks.py run?",
                      file=sys.stderr)
                return 1

            if not args.quiet:
                render(cur, args.run_id, st, clear=args.watch and iteration > 1)

            if args.once or not args.watch:
                # Exit code doubles as a machine-readable completion signal, so a
                # CI step can gate the reduce phase on `monitor.py --once`.
                if st["failed"]:
                    return 2
                if st["pending"] or st["running"]:
                    return 3
                return 0

            if st["pending"] == 0 and st["running"] == 0:
                print("\n  Run complete. Next:")
                print(f"    python reduce_finalize.py --run-id {args.run_id}")
                return 2 if st["failed"] else 0

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except psycopg2.Error as e:
        print(f"ERROR: database error: {e}", file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
