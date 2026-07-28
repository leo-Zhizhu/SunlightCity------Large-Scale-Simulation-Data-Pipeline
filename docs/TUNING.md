# PostgreSQL tuning

Two profiles, switched around the load. They exist because the right answer
genuinely **inverts** between the phases: during the load, WAL is pure overhead on
reproducible data; once the data is expensive to regenerate and someone is querying
it, WAL is how you avoid regenerating it.

| | [`postgresql.bulk.conf`](../distributed/db/postgresql.bulk.conf) | [`postgresql.serving.conf`](../distributed/db/postgresql.serving.conf) |
|---|---|---|
| `wal_level` | `minimal` | `replica` |
| `synchronous_commit` | `off` | `on` |
| `max_wal_size` | 64 GB | 4 GB |
| `work_mem` | 256 MB | 64 MB |
| `maintenance_work_mem` | 8 GB | 2 GB |
| replication / PITR | **impossible** | available |

Generate for your own host: `python distributed/orchestrator/pg_tune.py --detect
--workers 50 --profile bulk`

---

## The safety argument

The bulk profile trades crash durability for throughput. That is correct **only**
because of a property of this specific workload:

> Every byte it writes is **reproducible**. The output is a deterministic function
> of (city mesh, solar ephemeris, shard, date); the work queue records exactly which
> tasks completed; any lost task simply re-runs. Losing the database to a power cut
> costs wall-clock time, not information.

Apply the bulk profile to a database where that is not true and you are simply
running an unsafe database.

---

## `max_wal_size` — the one that is usually tuned backwards

The intuitive move is to *shrink* WAL. It makes throughput worse.

Shrinking `max_wal_size` does not reduce WAL work. It makes **checkpoints fire more
often**, and each checkpoint:

1. forces every dirty buffer to disk — an I/O spike that stalls all 50 writers at
   once, and
2. re-arms full-page-image logging, so the *next* write to every page carries a
   full 8 KB image instead of a small delta.

Under a sustained bulk load a small `max_wal_size` produces a checkpoint storm that
costs more than the WAL writes it was meant to avoid.

The goals only conflict if you conflate them onto one knob:

```
reduce WAL VOLUME per row   →  wal_level = minimal      (a different knob)
keep checkpoints RARE       →  max_wal_size = 64GB      (RAISE this one)
```

`pg_tune.py` scales `max_wal_size` with fleet size (`workers × 1.5`, clamped to
16–128 GB), because concurrent writers set the instantaneous WAL rate. The only cost
is disk space in `pg_wal`.

---

## `wal_level = minimal` — the big win

This unlocks the optimisation the worker's staging design exists to claim:

> **`COPY` into a relation created or truncated in the SAME transaction skips WAL
> entirely.**

The rule is precise about "same transaction", which is why the worker does:

```sql
BEGIN;
  SELECT meo_create_staging_edges(<task_id>);   -- CREATE UNLOGGED TABLE …
  COPY meo_stage_edges_<task_id> … FROM STDIN;  -- not WAL-logged
  SELECT meo_promote_staging(<task_id>, …);     -- INSERT … SELECT into the partition
COMMIT;
```

`UNLOGGED` on the staging table is belt-and-braces: it guarantees no WAL even if
`wal_level` is later raised, and it also skips free-space-map and visibility-map
writes.

**Costs.** No streaming replication, no PITR, no standby, and **you cannot take a
valid base backup**. Requires `max_wal_senders = 0`. Switch to the serving profile
and restart before considering the dataset protected.

**Verify it is actually working:**

```sql
SELECT pg_current_wal_lsn();   -- before a task
-- run one task
SELECT pg_current_wal_lsn();   -- after
-- The delta should be a small fraction of the bytes COPY'd.
```

---

## The risk ledger

Ordered by risk. Read the third row carefully.

| setting | value | what breaks on a crash | verdict |
|---|---|---|---|
| `synchronous_commit` | `off` | last ~200 ms of **committed** transactions | **Safe here.** Structurally consistent — this is not `fsync=off`. Lost task completions re-run. Highest value-per-risk in the file. |
| `wal_level` | `minimal` | no replication/PITR/base-backup *during the load* | **Safe here.** Reversible by restart. |
| `full_page_writes` | `off` | **torn pages → corrupt relation**, not just lost rows | **Conditional.** See below. |
| `fsync` | `on` — **not disabled** | — | Deliberately left on. |

### `full_page_writes = off`

The highest-risk line available. It skips writing a full page image on first
modification after a checkpoint, saving substantial WAL volume and CPU.

The protection it removes is against a **torn page**: if the OS crashes mid-8 KB
write, the page is left half-old/half-new and recovery *cannot repair it*. The
result is a corrupt relation.

Safe to disable **only** if one of these holds:

- ZFS or btrfs — copy-on-write makes writes atomic by construction; or
- a storage stack guaranteeing atomic 8 KB writes (many NVMe drives, most
  enterprise arrays with battery-backed cache); or
- you accept "restore from dump / re-run the pipeline" as the recovery plan.

`pg_tune.py` leaves it **on** unless you pass `--unsafe-torn-pages`, precisely
because it cannot detect any of the above.

### Why `fsync = off` is *not* recommended

It is the one place where "aggressive" stops being a good trade. A single power loss
risks unrecoverable corruption, and it buys little once `synchronous_commit = off`
and `full_page_writes = off` have already removed the frequent-fsync paths. Gated
behind `--unsafe-no-fsync` and not part of either shipped profile.

---

## Memory

`work_mem` is the one most often misunderstood: it is **per sort/hash node**, not
per query and not per connection. One query with three hash joins can use 3×.

`pg_tune.py` budgets 25% of RAM across `max(workers, cpus × 2, 16) × 2` estimated
concurrent sort nodes. The 25% is a **hard ceiling**, never overridden:

> An earlier version floored `work_mem` at 64 MB so the reduce phase's `GROUP BY`
> would stay in memory. An exhaustive sweep over 1,134 host/fleet combinations found
> 34 where that floor projected a worst case **above total RAM** — e.g. 8 GB RAM with
> 100 workers projecting 12 GB. On a host too small for the fleet you cannot have
> both; spilling to disk is slow, whereas exceeding RAM is an OOM kill. The tool now
> takes the budget and warns about the spill.

| setting | bulk | serving | why |
|---|---|---|---|
| `shared_buffers` | 25% RAM, cap 16 GB | same | Larger is **not** better for writes: Postgres still uses the OS page cache, and an oversized pool lengthens checkpoint scans and enlarges each checkpoint's dirty set. |
| `work_mem` | ~256 MB | ~64 MB | Reduced 4× for serving: many concurrent queries instead of a few huge sorts. |
| `maintenance_work_mem` | 8 GB | 2 GB | The single largest lever on post-load index-build time. |
| `huge_pages` | `try` | `try` | TLB relief for a 16 GB pool; `try` so startup survives a host with none reserved. |

---

## Autovacuum: throttled, **not** disabled

Disabling autovacuum during a bulk load is a common recipe and a trap here. The
work queue (`meo_tasks`) is high-churn — claim + ~40 heartbeats + complete per task
— so it bloats fast and its partial indexes degrade until claim latency becomes
visible.

The goal is autovacuum that **ignores the giant append-only partitions** and stays
attentive to the small hot tables. That is a per-table property, so it lives in
[`03_bulk_load_tuning.sql`](../distributed/db/03_bulk_load_tuning.sql), not the
server config:

| table | `fillfactor` | vacuum threshold | rationale |
|---|---:|---|---|
| exposure partitions | **100** | 2×10⁹ (effectively never) | Append-only, never updated. The default `fillfactor = 90` reserves a tenth of every page for HOT updates that never come — ~11 GB wasted on a 110 GB table, and 10% more pages to read on every scan. `ANALYZE` stays responsive because the planner needs it. |
| `meo_tasks` | **70** | 50 rows (absolute) | Room for HOT updates so a heartbeat rewrites the row **in place** without touching indexes. The single most effective setting for a queue table. Absolute thresholds because scale factors are meaningless on a few-hundred-row table. |
| `meo_edge_shards`, `meo_sample_points` | 100 | default | Static reference data, read constantly, never written. |

---

## Indexes are built **after** the load

An index present during a bulk load must be maintained per row: every `COPY`'d tuple
does a B-tree descent and possibly a page split, and 50 concurrent writers turn the
upper B-tree levels into a contention point.

Building afterwards instead:

- replaces *N* random descents with one large sequential sort bounded by
  `maintenance_work_mem`;
- parallelises across `max_parallel_maintenance_workers`;
- yields a **dense, unfragmented** tree instead of one ~70% full from splits.

The load is faster *and* the resulting index is smaller and faster to scan.

`CONCURRENTLY` is not used: it is disallowed on partitioned tables, and would be the
wrong choice anyway since nothing is reading yet.

### `ANALYZE` is not optional

The partitions went from empty to ~10⁸ rows with autovacuum held away from them, so
the planner's statistics still say "empty". Until `ANALYZE` runs, every query plans
as if the tables were tiny and picks catastrophically wrong plans.
[`04_post_load_indexes.sql`](../distributed/db/04_post_load_indexes.sql) runs it.

---

## Switching to serving

```bash
# 1. point postgresql.conf at the serving profile
#      include = 'postgresql.serving.conf'
# 2. RESTART — wal_level, full_page_writes, shared_buffers and
#    shared_preload_libraries are postmaster-level
pg_ctl restart
# 3. take a base backup — the bulk profile could not produce one
pg_basebackup -D /backup/$(date +%F) -Ft -z -P
```

Per-table settings from `03_bulk_load_tuning.sql` **persist** across the switch —
they live in `pg_class.reloptions`, not `postgresql.conf`. That is correct: the
append-only partitions are still append-only. Reset explicitly if that ever changes.

---

## Verifying the tuning did anything

```sql
-- Checkpoints: should be rare. Frequent 'requested' checkpoints mean
-- max_wal_size is still too small.
SELECT num_timed, num_requested, write_time, sync_time FROM pg_stat_bgwriter;

-- Lock contention. Waits on 'extend' mean partitioning is not isolating writers.
SELECT wait_event_type, wait_event, count(*)
FROM pg_stat_activity WHERE wait_event IS NOT NULL
GROUP BY 1, 2 ORDER BY 3 DESC;

-- work_mem spills. Non-zero temp_bytes during reduce = raise work_mem.
SELECT datname, temp_files, pg_size_pretty(temp_bytes) FROM pg_stat_database
WHERE datname = 'city_data';

-- Pool health (PgBouncer admin console)
--   psql -p 6432 -U admin pgbouncer -c 'SHOW POOLS;'
-- cl_waiting > 0 sustained means default_pool_size is too small.
```

`log_checkpoints = on` is enabled in both profiles for exactly this reason: if the
fleet stalls periodically, the log tells you immediately whether checkpoints are why.
