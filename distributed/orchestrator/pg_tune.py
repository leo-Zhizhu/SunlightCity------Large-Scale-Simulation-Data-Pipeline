#!/usr/bin/env python3
"""
Generates a PostgreSQL configuration tuned for THIS host and THIS fleet size.

The checked-in postgresql.bulk.conf documents the reasoning against a reference
16 vCPU / 64 GB node. This script derives the same profile for whatever hardware
you actually have, which matters because several of the values interact
non-linearly with core count and fleet size (work_mem in particular is a
per-sort-node allocation, so its safe ceiling depends on concurrency).

USAGE
    python pg_tune.py --ram-gb 64 --cpus 16 --workers 50 --profile bulk
    python pg_tune.py --detect --workers 50 --profile bulk -o postgresql.bulk.conf
    python pg_tune.py --detect --profile serving

SAFETY
    full_page_writes=off and fsync=off are NEVER emitted unless explicitly
    requested with --unsafe-torn-pages / --unsafe-no-fsync, because this script
    cannot detect whether your storage provides atomic 8 KB writes. See the
    printed warnings and TUNING.md.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone


def detect_ram_gb() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024.0 * 1024.0)
    except OSError:
        pass
    return None


def detect_cpus() -> int | None:
    try:
        # Respect a cgroup CPU quota if present: inside a container os.cpu_count()
        # reports the HOST's cores, which would size the config for hardware the
        # process cannot actually use.
        for quota_path, period_path in (
            ("/sys/fs/cgroup/cpu.max", None),                                  # cgroup v2
            ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
             "/sys/fs/cgroup/cpu/cpu.cfs_period_us"),                          # cgroup v1
        ):
            if not os.path.exists(quota_path):
                continue
            raw = open(quota_path).read().split()
            if period_path is None:
                if raw[0] == "max":
                    break
                quota, period = float(raw[0]), float(raw[1])
            else:
                quota = float(raw[0])
                if quota <= 0:
                    break
                period = float(open(period_path).read().strip())
            if period > 0:
                return max(1, int(quota / period))
    except (OSError, ValueError, IndexError):
        pass
    return os.cpu_count()


def mb(x: float) -> str:
    """Formats megabytes as the largest clean PostgreSQL unit."""
    x = int(x)
    if x >= 1024 and x % 1024 == 0:
        return f"{x // 1024}GB"
    return f"{max(1, x)}MB"


def compute(ram_gb: float, cpus: int, workers: int, profile: str,
            unsafe_torn: bool, unsafe_fsync: bool) -> tuple[dict, list[str]]:
    ram_mb = ram_gb * 1024
    notes: list[str] = []
    bulk = (profile == "bulk")

    cfg: dict[str, str] = {}

    # ---- Memory ------------------------------------------------------------
    # 25% is the long-standing default and remains right for a write-heavy load:
    # PostgreSQL still leans on the OS page cache, and an oversized buffer pool
    # lengthens checkpoint scans and enlarges each checkpoint's dirty set.
    shared = ram_mb * 0.25
    # Cap at 16 GB: beyond that the buffer-manager's clock sweep and checkpoint
    # cost grow faster than the hit-rate benefit for this access pattern.
    shared = min(shared, 16 * 1024)
    cfg["shared_buffers"] = mb(shared)

    cfg["effective_cache_size"] = mb(ram_mb * 0.75)

    # work_mem is PER SORT/HASH NODE, not per query or per connection. The worst
    # case is roughly (concurrent queries) x (nodes per query). Budget a quarter of
    # RAM to the aggregate and assume ~2 nodes per query.
    #
    # The floor of 16 matters: on a small host with a small fleet, deriving
    # concurrency from `workers` alone badly understates reality (ad-hoc queries,
    # monitor, autovacuum, psql sessions all add sort contexts). Without it an
    # 8 GB / 4-worker host was handed work_mem=256MB, where ~23 concurrent sort
    # nodes would exhaust RAM.
    concurrency = max(workers, cpus * 2, 16)
    budget_mb = ram_mb * 0.25
    per_node = budget_mb / max(1, concurrency * 2)

    # The budget is a CEILING, never a target to be overridden. An earlier version
    # floored work_mem at 64MB on the grounds that the reduce phase's GROUP BY
    # should stay in memory — but that floor multiplied by high concurrency
    # produced worst cases exceeding total RAM on undersized hosts (a sweep found
    # 34 such configurations, e.g. 8 GB with 100 workers projecting 12 GB).
    # On a host too small for the fleet you cannot have both; spilling to disk is
    # slow, whereas exceeding RAM is an OOM kill. So we take the budget and warn.
    if bulk:
        # Cap: no single sort node in this workload benefits beyond this.
        per_node = min(per_node, 256)
        if per_node < 64:
            notes.append(
                f"work_mem {mb(per_node)} is below 64MB — the reduce phase's GROUP BY "
                "may spill to disk, making finalisation slower. Add RAM or lower "
                "--workers if index/rollup time matters.")
    else:
        # Serving: many concurrent routing queries, so keep the ceiling low.
        per_node = min(per_node, 64)

    # Absolute floor. Below a few MB the planner starts choosing pathological
    # plans, and PostgreSQL's own minimum is 64kB.
    per_node = max(4, per_node)
    cfg["work_mem"] = mb(per_node)

    worst_case_gb = per_node * concurrency * 2 / 1024
    notes.append(
        f"work_mem {mb(per_node)} x ~{concurrency*2} concurrent sort nodes "
        f"= ~{worst_case_gb:.1f} GB worst case (RAM {ram_gb:.0f} GB)")

    # When the 4MB absolute floor binds, the budget can no longer be honoured —
    # the host is simply too small for the requested concurrency. Say so plainly
    # rather than emitting a config that looks fine and then OOMs under load.
    if worst_case_gb > ram_gb * 0.5:
        notes.append(
            f"  *** UNDERSIZED HOST: worst-case work_mem (~{worst_case_gb:.1f} GB) exceeds "
            f"half of RAM ({ram_gb:.0f} GB). {concurrency} concurrent sort contexts cannot "
            f"be served at PostgreSQL's practical minimum work_mem. Reduce --workers "
            f"(currently {workers}) or provision a larger host; this configuration can OOM.")

    # maintenance_work_mem decides post-load index-build time. Give it a lot
    # during the load, much less when serving.
    if bulk:
        cfg["maintenance_work_mem"] = mb(min(ram_mb * 0.15, 8 * 1024))
        cfg["max_parallel_maintenance_workers"] = str(max(2, min(cpus // 2, 8)))
    else:
        cfg["maintenance_work_mem"] = mb(min(ram_mb * 0.05, 2 * 1024))
        cfg["max_parallel_maintenance_workers"] = str(max(2, min(cpus // 4, 4)))

    cfg["autovacuum_work_mem"] = mb(min(ram_mb * 0.03, 1024))
    cfg["huge_pages"] = "try"
    cfg["temp_buffers"] = "64MB" if bulk else "16MB"

    # ---- WAL ---------------------------------------------------------------
    if bulk:
        # minimal: lets COPY into a same-transaction-created table skip WAL
        # entirely, which is the optimisation the worker's staging design exists
        # to claim.
        cfg["wal_level"] = "minimal"
        cfg["max_wal_senders"] = "0"
        notes.append("wal_level=minimal — no replication or PITR possible during the load")

        # RAISED, not lowered. A small max_wal_size makes checkpoints frequent, and
        # each checkpoint re-arms full-page-image logging for every page touched
        # afterwards. Under sustained bulk load that costs more than the WAL writes.
        # Scale with fleet size since concurrent writers set the instantaneous rate.
        wal_gb = max(16, min(128, workers * 1.5))
        cfg["max_wal_size"] = f"{int(wal_gb)}GB"
        cfg["min_wal_size"] = f"{max(2, int(wal_gb / 8))}GB"
        cfg["checkpoint_timeout"] = "30min"
        cfg["wal_compression"] = "off"
        cfg["wal_buffers"] = mb(min(256, max(64, workers * 2)))
        cfg["wal_writer_delay"] = "200ms"
        cfg["wal_writer_flush_after"] = "16MB"
        cfg["synchronous_commit"] = "off"
        notes.append("synchronous_commit=off — a crash loses the last ~200ms of "
                     "commits; lost tasks simply re-run from the queue")
    else:
        cfg["wal_level"] = "replica"
        cfg["max_wal_senders"] = "10"
        cfg["max_replication_slots"] = "10"
        cfg["max_wal_size"] = "4GB"
        cfg["min_wal_size"] = "1GB"
        cfg["checkpoint_timeout"] = "15min"
        cfg["wal_compression"] = "on"
        cfg["wal_buffers"] = "64MB"
        cfg["synchronous_commit"] = "on"

    cfg["checkpoint_completion_target"] = "0.9"
    cfg["checkpoint_flush_after"] = "2MB"

    # ---- Durability (guarded) ---------------------------------------------
    if unsafe_torn and bulk:
        cfg["full_page_writes"] = "off"
        notes.append("full_page_writes=off — REQUESTED. Torn pages are "
                     "UNRECOVERABLE without CoW/atomic-write storage.")
    else:
        cfg["full_page_writes"] = "on"
        if bulk:
            notes.append("full_page_writes left ON (safe default). Add "
                         "--unsafe-torn-pages only on ZFS/btrfs or storage with "
                         "guaranteed atomic 8KB writes.")

    if unsafe_fsync and bulk:
        cfg["fsync"] = "off"
        notes.append("fsync=off — REQUESTED. A single power loss can corrupt the "
                     "cluster beyond repair. Only for a fully disposable database.")
    else:
        cfg["fsync"] = "on"

    # ---- Concurrency ------------------------------------------------------
    # 2 connections per worker (COPY + control) plus orchestrator and headroom.
    # PgBouncer keeps actual backends far below this, but the ceiling must still
    # accommodate a direct-connect fleet.
    cfg["max_connections"] = str(max(100, workers * 2 + 50))
    cfg["superuser_reserved_connections"] = "5"
    cfg["max_worker_processes"] = str(max(8, cpus))
    cfg["max_parallel_workers"] = str(max(8, cpus))
    cfg["max_parallel_workers_per_gather"] = str(max(2, min(cpus // 4, 8)))

    # ---- Autovacuum -------------------------------------------------------
    cfg["autovacuum"] = "on"
    if bulk:
        cfg["autovacuum_max_workers"] = str(max(3, min(cpus // 4, 6)))
        cfg["autovacuum_naptime"] = "30s"
        # No throttling on NVMe: a throttled vacuum cannot keep up with the work
        # queue's churn (~40 row versions per task).
        cfg["autovacuum_vacuum_cost_delay"] = "0"
        notes.append("autovacuum left ON during the load — meo_tasks is high-churn "
                     "and its partial indexes bloat fast. Per-table thresholds in "
                     "03_bulk_load_tuning.sql keep it away from the big partitions.")
    else:
        cfg["autovacuum_max_workers"] = "3"
        cfg["autovacuum_naptime"] = "60s"
        cfg["autovacuum_vacuum_cost_delay"] = "2ms"

    # ---- Planner / IO -----------------------------------------------------
    cfg["random_page_cost"] = "1.1"
    cfg["effective_io_concurrency"] = "200"
    cfg["default_statistics_target"] = "100"
    if not bulk:
        cfg["enable_partitionwise_join"] = "on"
        cfg["enable_partitionwise_aggregate"] = "on"

    # ---- Observability ----------------------------------------------------
    cfg["log_min_duration_statement"] = "5000" if bulk else "1000"
    cfg["log_checkpoints"] = "on"
    cfg["log_lock_waits"] = "on"
    cfg["deadlock_timeout"] = "1s"
    cfg["log_temp_files"] = "10485760"
    cfg["log_autovacuum_min_duration"] = "10000"
    cfg["log_line_prefix"] = "'%m [%p] %q%u@%d/%a '"
    cfg["track_io_timing"] = "on"
    cfg["shared_preload_libraries"] = "'pg_stat_statements'"
    cfg["pg_stat_statements.max"] = "10000"
    cfg["pg_stat_statements.track"] = "all"

    return cfg, notes


SECTIONS = [
    ("Memory", ["shared_buffers", "effective_cache_size", "work_mem",
                "maintenance_work_mem", "max_parallel_maintenance_workers",
                "autovacuum_work_mem", "huge_pages", "temp_buffers"]),
    ("WAL", ["wal_level", "max_wal_senders", "max_replication_slots",
             "max_wal_size", "min_wal_size", "checkpoint_timeout",
             "checkpoint_completion_target", "checkpoint_flush_after",
             "wal_compression", "wal_buffers", "wal_writer_delay",
             "wal_writer_flush_after"]),
    ("Durability", ["synchronous_commit", "full_page_writes", "fsync"]),
    ("Concurrency", ["max_connections", "superuser_reserved_connections",
                     "max_worker_processes", "max_parallel_workers",
                     "max_parallel_workers_per_gather"]),
    ("Autovacuum", ["autovacuum", "autovacuum_max_workers",
                    "autovacuum_naptime", "autovacuum_vacuum_cost_delay"]),
    ("Planner / IO", ["random_page_cost", "effective_io_concurrency",
                      "default_statistics_target", "enable_partitionwise_join",
                      "enable_partitionwise_aggregate"]),
    ("Observability", ["log_min_duration_statement", "log_checkpoints",
                       "log_lock_waits", "deadlock_timeout", "log_temp_files",
                       "log_autovacuum_min_duration", "log_line_prefix",
                       "track_io_timing", "shared_preload_libraries",
                       "pg_stat_statements.max", "pg_stat_statements.track"]),
]


def render(cfg: dict, notes: list[str], ram_gb: float, cpus: int,
           workers: int, profile: str) -> str:
    out = [
        "# " + "=" * 76,
        f"# SunlightCity PostgreSQL configuration — profile: {profile.upper()}",
        f"# Generated by pg_tune.py on {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        f"#",
        f"#   host   : {ram_gb:.0f} GB RAM, {cpus} vCPU",
        f"#   fleet  : {workers} workers",
        "#",
        "# Include from postgresql.conf:   include = 'this-file.conf'",
        "#",
        "# NOTE: wal_level, max_wal_senders, shared_buffers, huge_pages and",
        "# shared_preload_libraries all require a RESTART, not a reload.",
        "# " + "=" * 76,
        "",
    ]

    if notes:
        out.append("# ---- Generator notes " + "-" * 55)
        for n in notes:
            out.append(f"# {n}")
        out.append("")

    for title, keys in SECTIONS:
        present = [k for k in keys if k in cfg]
        if not present:
            continue
        out.append(f"# ---- {title} " + "-" * max(0, 66 - len(title)))
        width = max(len(k) for k in present)
        for k in present:
            out.append(f"{k.ljust(width)} = {cfg[k]}")
        out.append("")

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ram-gb", type=float)
    p.add_argument("--cpus", type=int)
    p.add_argument("--workers", type=int, default=50)
    p.add_argument("--profile", choices=["bulk", "serving"], default="bulk")
    p.add_argument("--detect", action="store_true", help="read RAM/CPU from this host")
    p.add_argument("--unsafe-torn-pages", action="store_true",
                   help="emit full_page_writes=off (needs CoW or atomic-write storage)")
    p.add_argument("--unsafe-no-fsync", action="store_true",
                   help="emit fsync=off (disposable databases only)")
    p.add_argument("-o", "--output", help="write to a file instead of stdout")
    args = p.parse_args()

    ram = args.ram_gb
    cpus = args.cpus
    if args.detect:
        ram = ram or detect_ram_gb()
        cpus = cpus or detect_cpus()

    if not ram or not cpus:
        print("ERROR: need --ram-gb and --cpus (or --detect).", file=sys.stderr)
        return 1
    if args.workers < 1:
        print("ERROR: --workers must be >= 1", file=sys.stderr)
        return 1
    if ram < 4:
        print(f"ERROR: {ram:.1f} GB RAM is below the useful minimum (4 GB).", file=sys.stderr)
        return 1

    cfg, notes = compute(ram, cpus, args.workers, args.profile,
                         args.unsafe_torn_pages, args.unsafe_no_fsync)
    text = render(cfg, notes, ram, cpus, args.workers, args.profile)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.output}  ({ram:.0f} GB / {cpus} vCPU / "
              f"{args.workers} workers / {args.profile})")
        for n in notes:
            print(f"  note: {n}")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
