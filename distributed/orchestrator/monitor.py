#!/usr/bin/env python3
"""
Fleet monitor and lease reaper.

Two jobs in one script because they share all their plumbing:

  1. REAP  — return expired leases to the pending pool, and sweep orphaned
             partition leaves off the shards. This IS the pipeline's node-failure
             recovery. Run it on a schedule (the k8s CronJob does, every minute).
  2. WATCH — live progress, per-shard load, throughput and ETA for a human.

USAGE
    python monitor.py --run-id run-2026-annual --watch          # live dashboard
    python monitor.py --run-id run-2026-annual --reap --once    # one reap pass
    python monitor.py --run-id run-2026-annual --once           # one-shot status

WHY LEASE EXPIRY RATHER THAN POD-DEATH WATCHING
-----------------------------------------------
A controller watching pod lifecycle events would be more code, need RBAC, and be
strictly worse at the job. Pod-death events do not fire for a network partition, a
frozen kernel, a hung container, or a worker stuck in an unbounded GC pause. Lease
expiry covers every one of those uniformly, because it observes the only thing that
actually matters: whether progress is still being reported.

Reaping also frees the dead worker's ADMISSION SLOT on its shard, which is easy to
overlook and matters more than it looks: without it every node failure would
permanently shrink the cluster's usable write concurrency for the rest of the run.

WHAT TO WATCH, IN PRIORITY ORDER
--------------------------------
  * per-shard `run` at the admission cap on some instances and 0 on others — the
    topology is unbalanced and the makespan is being set by the busy ones
  * affinity hit rate falling below ~85% — dispatch is thrashing the geometry and
    collider working sets, and the map phase will run long
  * `hb` climbing past a third of the lease — a worker is about to be reaped
  * failed > 0 — retries exhausted; read last_error
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

from cluster import ClusterEndpoints
import model


def coord_config(args) -> dict:
    return {
        "host": args.coord_host or os.environ.get("SUNLIT_COORD_HOST", "localhost"),
        "port": int(args.coord_port or os.environ.get("SUNLIT_COORD_PORT", 5432)),
        "database": args.coord_db or os.environ.get("SUNLIT_COORD_DB", "sunlit_coord"),
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


def spark(values: list[int], cap: int) -> str:
    """
    Per-shard load as one glyph each, against the admission cap.
    A full row of the top glyph means every instance is saturated, which is the
    healthy steady state. A ragged row means the topology is unbalanced.
    """
    glyphs = " ▁▂▃▄▅▆▇█"
    out = []
    for v in values:
        idx = 0 if cap <= 0 else min(len(glyphs) - 1, int(round(v / cap * (len(glyphs) - 1))))
        out.append(glyphs[idx])
    return "".join(out)


# ---------------------------------------------------------------------------
# REAP
# ---------------------------------------------------------------------------
def reap(cur, run_id: str | None, verbose: bool = True) -> int:
    """Returns tasks whose lease expired to 'pending' (or 'failed' if exhausted)."""
    cur.execute("SELECT * FROM meo_reap_expired_leases(%s)", (run_id,))
    rows = cur.fetchall()
    if rows and verbose:
        print(f"[reap] reclaimed {len(rows)} expired lease(s):")
        for task_id, shard, detail, attempts, new_state in rows:
            print(f"  task#{task_id} shard {shard} -> {new_state} "
                  f"(attempt {attempts}) {detail}")
    elif verbose:
        print("[reap] no expired leases")
    return len(rows)


def sweep_orphan_leaves(args, run_id: str, verbose: bool = True) -> int:
    """
    Drops unattached partition leaves left by workers killed between CREATE and
    ATTACH.

    Unlike the UNLOGGED staging tables of an earlier design, these hold REAL DISK —
    a killed worker can leave 20 MB behind, and a flapping node could leave
    gigabytes over a run. They are also invisible through the parent, so nothing
    else would ever notice them.

    Runs on every shard, and a shard being unreachable is logged rather than fatal:
    the reaper's main job (returning leases) has already succeeded by this point and
    must not be undone by a housekeeping failure.
    """
    endpoints = ClusterEndpoints.from_environment()
    total = 0

    conn = None
    try:
        conn = psycopg2.connect(**coord_config(args))
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT shard_index FROM meo_shards WHERE state <> 'offline' "
                    "ORDER BY shard_index")
        shards = [r[0] for r in cur.fetchall()]
    finally:
        if conn:
            conn.close()

    for i in shards:
        cfg = endpoints.shard(i)
        cfg["password"] = coord_config(args)["password"]
        try:
            with psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
                                  user=cfg["user"], password=cfg["password"]) as sh:
                sh.autocommit = True
                c = sh.cursor()
                c.execute("SELECT meo_drop_orphan_leaves()")
                n = c.fetchone()[0]
                total += n
                if n and verbose:
                    print(f"[reap] shard {i}: dropped {n} orphaned leaf/leaves")
        except psycopg2.Error as e:
            print(f"[reap] WARNING shard {i} unreachable for leaf sweep: "
                  f"{str(e).splitlines()[0]}", file=sys.stderr)

    return total


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------
def fetch_status(cur, run_id: str) -> dict | None:
    cur.execute("""
        SELECT shard_count, section_count, created_at, started_at, finished_at,
               tasks_total, tasks_pending, tasks_running, tasks_done, tasks_failed,
               workers_active, rows_written, raycasts_done, raycasts_planned,
               pct_done, avg_task_seconds, pct_affinity_hit
        FROM meo_run_progress WHERE run_id = %s
    """, (run_id,))
    r = cur.fetchone()
    if not r:
        return None
    keys = ("shard_count", "section_count", "created_at", "started_at", "finished_at",
            "total", "pending", "running", "done", "failed",
            "workers", "rows", "rays", "rays_planned", "pct", "avg_task_s", "affinity")
    st = dict(zip(keys, r))

    # SUM() over a bigint column returns NUMERIC, which psycopg2 maps to
    # decimal.Decimal — and Decimal does not mix with float in arithmetic. Coerced
    # here, once, rather than at each of the several places that divide by elapsed
    # time. (An unguarded `rays / elapsed` is a TypeError that only appears once a
    # run has actually done some work, i.e. never during a dry test.)
    for k in ("rows", "rays", "rays_planned"):
        st[k] = int(st[k] or 0)
    for k in ("pct", "avg_task_s", "affinity"):
        st[k] = float(st[k]) if st[k] is not None else None
    return st


BOX_WIDTH = 74


def render(cur, run_id: str, st: dict, clear: bool) -> None:
    if clear:
        # Home cursor + clear below, rather than a full clear: avoids the flicker of
        # erasing and repainting the whole screen each second.
        sys.stdout.write("\033[H\033[J")

    now = datetime.now()
    pct = st["pct"] or 0.0

    # Padded rather than hand-counted. Every previous attempt at aligning this by
    # appending a fixed number of spaces drifted the moment a field's width changed,
    # and a dashboard with ragged borders reads as broken.
    def row(text: str) -> None:
        print("│" + text[:BOX_WIDTH].ljust(BOX_WIDTH) + "│")

    print("╭" + "─" * BOX_WIDTH + "╮")
    row(f" SunlightCity · run {run_id[:42]}".ljust(BOX_WIDTH - 9) + f"{now:%H:%M:%S}")
    print("├" + "─" * BOX_WIDTH + "┤")
    row(f" {bar(pct)} {pct:6.2f}%")
    print("├" + "─" * BOX_WIDTH + "┤")
    row(f"  done {st['done']:>6,}   running {st['running']:>4,}   "
        f"pending {st['pending']:>6,}   failed {st['failed']:>4,}")
    row(f"  workers {st['workers'] or 0:>3}   sections {st['section_count']:>4}   "
        f"shards {st['shard_count']:>3}   rows {st['rows']:>15,}")

    # Affinity is the earliest warning that the map phase will overrun: it collapses
    # long before throughput visibly drops.
    aff = st["affinity"]
    if aff is not None:
        flag = "  <-- LOW, dispatch is thrashing working sets" if aff < 85 else ""
        row(f"  affinity hit {aff:5.1f}%{flag}")

    rays = st["rays"]
    if st["started_at"]:
        elapsed = (now.astimezone() - st["started_at"]).total_seconds()
        if elapsed > 0 and rays:
            rate = rays / elapsed
            # Compared against V1_BVH_RATE, not V1_RAYCAST_RATE: the numerator here is
            # a RAYCAST count, and V1_RAYCAST_RATE is v1's ROW rate (the names are a
            # trap -- see model.py). v1 fired 990,240,696 raycasts in 6.0 h on one
            # desktop main thread, so ~45.8k/s.
            row(f"  raycasts {human(rays):>10}   rate {human(rate):>8}/s"
                f"   vs 1 node {rate / model.V1_BVH_RATE:>6.1f}x")

            planned = st["rays_planned"]
            if planned > rays:
                eta = timedelta(seconds=int((planned - rays) / max(1.0, rate)))
                row(f"  elapsed {str(timedelta(seconds=int(elapsed))):>12}"
                    f"   ETA {str(eta):>12}   finish ~{(now + eta):%H:%M}")

    print("╰" + "─" * BOX_WIDTH + "╯")

    # ---- Per-shard load: the coordination view -------------------------------
    cur.execute("""
        SELECT shard_index, shard_state, admission_cap,
               tasks_running, tasks_done, tasks_pending, tasks_failed, rows_written
        FROM meo_shard_progress WHERE run_id = %s ORDER BY shard_index
    """, (run_id,))
    shards = cur.fetchall()
    if shards:
        cap = shards[0][2]
        running = [s[3] for s in shards]
        print(f"\n  shard load vs cap {cap}:  [{spark(running, cap)}]  "
              f"{sum(running)}/{len(shards) * cap} slots in use")
        print(f"  {'shard':>6}{'state':>10}{'run':>5}{'/cap':>5}{'done':>7}"
              f"{'pending':>9}{'failed':>7}{'rows':>14}")
        print("  " + "─" * 63)
        for idx, state, c, run, done, pend, fail, rows in shards:
            # A shard pinned at its cap while others idle is the signal that the
            # Hilbert cut has drifted or that retries have clustered in one region.
            flag = " !" if (run >= c and min(running) == 0) else ""
            print(f"  {idx:>6}{state:>10}{run:>5}{'/' + str(c):>5}{done:>7}"
                  f"{pend:>9}{fail:>7}{rows or 0:>14,}{flag}")

    # ---- Per-worker detail: shows stragglers ---------------------------------
    cur.execute("""
        SELECT worker_id, section_id, window_index, sim_date, shard_index,
               EXTRACT(EPOCH FROM (now() - started_at))::INT,
               EXTRACT(EPOCH FROM (now() - heartbeat_at))::INT,
               raycasts_done, affinity_hit
        FROM meo_tasks
        WHERE run_id = %s AND state = 'running'
        ORDER BY started_at
        LIMIT 12
    """, (run_id,))
    running_tasks = cur.fetchall()
    if running_tasks:
        print(f"\n  {'worker':<24}{'sect':>6}{'w':>3}{'date':>12}{'sh':>4}"
              f"{'age':>7}{'hb':>6}{'rays':>12}{'aff':>5}")
        print("  " + "─" * 79)
        for w, sec, win, d, sh, age, hb, rays_w, aff_hit in running_tasks:
            # Flag a worker whose heartbeat is going stale — the first visible sign of
            # a pod about to lose its lease.
            flag = " !" if (hb or 0) > 90 else ""
            print(f"  {(w or '?')[:24]:<24}{sec:>6}{win:>3}{str(d):>12}{sh:>4}"
                  f"{age or 0:>6}s{hb or 0:>5}s{rays_w or 0:>12,}"
                  f"{'hit' if aff_hit else 'miss':>5}{flag}")

    if st["failed"]:
        cur.execute("""
            SELECT section_id, window_index, sim_date, shard_index, attempts,
                   left(coalesce(last_error, ''), 54)
            FROM meo_tasks
            WHERE run_id = %s AND state = 'failed'
            ORDER BY sim_date, section_id LIMIT 8
        """, (run_id,))
        print("\n  FAILED tasks:")
        for sec, win, d, sh, attempts, err in cur.fetchall():
            print(f"    section {sec:>5} w{win} {d} shard {sh}  "
                  f"attempts {attempts}  {err}")


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True)
    p.add_argument("--watch", action="store_true", help="live dashboard, refreshing")
    p.add_argument("--once", action="store_true", help="single pass then exit")
    p.add_argument("--interval", type=float, default=5.0, help="watch refresh seconds")
    p.add_argument("--reap", action="store_true", help="reclaim expired leases")
    p.add_argument("--sweep-leaves", action="store_true",
                   help="also drop orphaned partition leaves on every shard")
    p.add_argument("--quiet", action="store_true", help="suppress the dashboard (reap only)")
    p.add_argument("--coord-host"); p.add_argument("--coord-port")
    p.add_argument("--coord-db"); p.add_argument("--db-user"); p.add_argument("--db-password")
    args = p.parse_args()

    conn = None
    try:
        conn = psycopg2.connect(**coord_config(args))
        conn.autocommit = True
        cur = conn.cursor()

        iteration = 0
        while True:
            iteration += 1

            if args.reap:
                reap(cur, args.run_id, verbose=not args.watch)
            if args.sweep_leaves:
                sweep_orphan_leaves(args, args.run_id, verbose=not args.watch)

            st = fetch_status(cur, args.run_id)
            if st is None:
                print(f"ERROR: run '{args.run_id}' not found. Has plan_tasks.py run?",
                      file=sys.stderr)
                return 1

            if not args.quiet:
                render(cur, args.run_id, st, clear=args.watch and iteration > 1)

            if args.once or not args.watch:
                # Exit code doubles as a machine-readable completion signal, so a CI
                # step can gate the reduce phase on `monitor.py --once`.
                if st["failed"]:
                    return 2
                if st["pending"] or st["running"]:
                    return 3
                return 0

            if st["pending"] == 0 and st["running"] == 0:
                print("\n  Map phase complete. Next:")
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
