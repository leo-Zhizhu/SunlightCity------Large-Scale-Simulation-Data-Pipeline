# PostgreSQL tuning

Two instance roles, and one of them has two phases — so three profiles.

| | file | when |
|---|---|---|
| shard, bulk | [`postgresql.shard.bulk.conf`](../distributed/db/postgresql.shard.bulk.conf) | absorbing the load |
| shard, serving | [`postgresql.shard.serving.conf`](../distributed/db/postgresql.shard.serving.conf) | after the reduce phase |
| coordinator | [`postgresql.coordinator.conf`](../distributed/db/postgresql.coordinator.conf) | always — one profile |

Generate for your own hardware:

```bash
python distributed/orchestrator/pg_tune.py --role shard --profile bulk \
       --ram-gb 128 --cpus 16 --workers 50 --shards 10
python distributed/orchestrator/pg_tune.py --role shard --profile serving --detect
python distributed/orchestrator/pg_tune.py --role coordinator --detect --workers 50
```

The three checked-in profiles are verified to match the generator's output exactly — 46,
48 and 48 settings respectively — so the reasoning documented in the files and the tool
that produces them cannot drift apart.

---

## Why a shard's answer inverts between phases

| | bulk | serving |
|---|---|---|
| `wal_level` | `minimal` | `replica` |
| `synchronous_commit` | `off` | `on` |
| `max_wal_size` | 32 GB | 4 GB |
| `work_mem` | 256 MB | 64 MB |
| `maintenance_work_mem` | 16 GB | 2 GB |
| `autovacuum_vacuum_cost_delay` | 0 | 2 ms |
| `jit` | (default) | `off` |
| replication / PITR | **impossible** | available |

During the load a shard's contents are reproducible from (mesh, ephemeris, section, date,
window) in about three minutes, so WAL is pure overhead. Once loaded, the dataset is the
input to something else, and "restore from a replica" is seconds while "re-run the
pipeline" is a cluster and a Unity licence. WAL is how you avoid needing either.

That is not because regenerating became impossible — it is because the cost of
regenerating stopped being the relevant comparison.

---

## The safety argument, stated plainly

The bulk profile trades crash durability for throughput. That is correct **only** because
of a property of this specific workload:

> Every byte a shard writes is **reproducible**. Its output is a deterministic function
> of (city mesh, solar ephemeris, section, date, window); the coordinator's work queue
> records exactly which tasks completed; a lost task simply re-runs. Losing a shard to a
> power cut costs wall-clock time, not information — about 20 seconds of fleet time for
> one instance's 605 tasks.

Apply the bulk profile to a database where that is not true and you are simply running an
unsafe database.

**The coordinator never gets this trade**, and that asymmetry is the point of separating
it: `meo_tasks` **is** the record of what has been computed. Lose the last 200 ms of it
and the fleet re-runs tasks that already succeeded — or worse, a task marked done that
was rolled back leaves a gap the completeness check finds only at the end. So the
coordinator keeps `synchronous_commit = on`, `wal_level = replica` and full durability
throughout. It costs nothing worth measuring: the whole write volume is a few hundred
thousand tiny HOT updates.

`pg_tune.py --role coordinator` **ignores `--profile`** for exactly this reason, so
`--role coordinator --profile bulk` cannot silently produce a queue instance with
`synchronous_commit` off.

---

## `wal_level = minimal` — the big win, and it is bigger than it looks

This unlocks the optimisation the whole partition shape exists to claim:

> **`COPY` into a relation created in the SAME transaction skips WAL entirely.**

Every task builds its own partition leaf from scratch rather than appending to a
pre-existing partition, so the rule applies to all of it:

```sql
BEGIN;
  SELECT meo_begin_leaf(section, lo, hi, window, task);   -- CREATE TABLE
  COPY <leaf> (…) FROM STDIN (FORMAT BINARY, FREEZE);     -- not WAL-logged
  SELECT meo_attach_leaf(section, lo, hi, window);        -- ATTACH PARTITION
COMMIT;
```

**~100 GB of sample data written across the cluster without a WAL record.** Not reduced —
skipped. It is the single largest database-side effect in the pipeline.

`FREEZE` has the same precondition and removes two later costs: no hint-bit write on
first read (which would otherwise rewrite the whole table on the first query after the
load), and no freeze-vacuum of 1.58 billion rows ever.

**Verify it rather than trusting it:**

```sql
SELECT pg_current_wal_lsn();   -- before one task
-- run one task
SELECT pg_current_wal_lsn();   -- after
```

The delta should be **kilobytes** — the catalog entries for `CREATE` and `ATTACH` —
against the ~18 MB the task actually wrote. If it is ~18 MB, the WAL-skip is not
happening: check that `wal_level` really is `minimal` (it needs a restart, not a reload)
and that `max_wal_senders = 0`.

**Costs.** No streaming replication, no PITR, no standby, and **you cannot take a valid
base backup**. Switch to the serving profile and restart before considering the dataset
protected.

---

## `max_wal_size` — the one usually tuned backwards

The intuitive move is to *shrink* WAL. It makes throughput worse.

Shrinking `max_wal_size` does not reduce WAL work. It makes **checkpoints fire more
often**, and each checkpoint:

1. forces every dirty buffer to disk — an I/O spike that stalls every writer on that
   instance at once, and
2. re-arms full-page-image logging, so the *next* write to every page carries a full
   8 KB image instead of a small delta.

Under sustained bulk load a small `max_wal_size` produces a checkpoint storm that costs
more than the WAL writes it was meant to avoid.

The two goals only conflict if you conflate them onto one knob:

```
reduce WAL VOLUME per row   →   wal_level = minimal      (a different knob)
keep checkpoints RARE       →   max_wal_size = 32GB      (RAISE this one)
```

`pg_tune.py` scales it from the streams arriving at **that instance** — ten, not fifty.
Sizing it from the fleet was a real error in an earlier version: 50 workers only ever
produce ten connections to any one shard, so deriving anything per-shard from the fleet
size was wrong by an order of magnitude. The same correction applies to `work_mem` and
`max_connections`.

What `max_wal_size` actually has to absorb here is *not* the sample load (which emits
almost no WAL) but the reduce phase's edge rollup — a fully-logged `INSERT` of ~2.9M rows
— plus its indexes.

---

## The risk ledger

Ordered by risk. Read the third row carefully.

| setting | bulk value | what breaks on a crash | verdict |
|---|---|---|---|
| `synchronous_commit` | `off` | the last ~200 ms of **committed** transactions | **Safe here.** Structurally consistent — this is not `fsync=off`. Lost task completions re-run. Highest value per unit of risk in the file. |
| `wal_level` | `minimal` | no replication / PITR / base backup *during the load* | **Safe here.** Reversible by restart. |
| `full_page_writes` | `on` — **not disabled** | **torn page → corrupt relation**, not merely lost rows | **Conditional.** See below. |
| `fsync` | `on` — **not disabled** | — | Deliberately left on. |

### `full_page_writes = off`

The highest-risk line available. It skips writing a full page image on the first
modification after a checkpoint, saving substantial WAL volume and CPU.

The protection it removes is against a **torn page**: if the OS crashes mid-8 KB write,
the page is left half-old/half-new and recovery **cannot repair it**. The result is a
corrupt relation, not a few lost rows.

Safe to disable **only** if one of these holds:

- ZFS or btrfs — copy-on-write makes writes atomic by construction; or
- a storage stack guaranteeing atomic 8 KB writes (many NVMe drives, most enterprise
  arrays with battery-backed cache); or
- you accept "re-run the pipeline" as the recovery plan — which, for a shard, is
  genuinely reasonable at 20 seconds of fleet time.

`pg_tune.py` leaves it **on** unless you pass `--unsafe-torn-pages`, precisely because it
cannot detect any of the above.

Note the interaction with `--data-checksums`, which the StatefulSet enables at `initdb`:
with `synchronous_commit = off`, checksums are the only thing that would catch a silently
corrupted page before it is read back as exposure data. Worth the ~2% write cost here
specifically.

### Why `fsync = off` is *not* recommended

The one place where "aggressive" stops being a good trade. A single power loss risks
unrecoverable corruption of the whole cluster, and it buys little once
`synchronous_commit = off` has already removed the frequent-fsync path. Gated behind
`--unsafe-no-fsync` and part of no shipped profile.

---

## Memory

`work_mem` is the most misunderstood setting here: it is **per sort/hash node**, not per
query and not per connection. One query with three hash joins can use 3×.

`pg_tune.py` budgets 25% of RAM across `max(expected_clients, cpus × 2, 16) × 2`
estimated concurrent sort nodes, where `expected_clients` is role-specific — see above.
The 25% is a **hard ceiling, never overridden**:

> An earlier version floored `work_mem` at 64 MB so the reduce phase's `GROUP BY` would
> stay in memory. An exhaustive sweep over 1,134 host/fleet combinations found 34 where
> that floor projected a worst case **above total RAM** — e.g. 8 GB of RAM projecting
> 12 GB. On a host too small for its concurrency you cannot have both; spilling to disk is
> slow, whereas exceeding RAM is an OOM kill. The tool now takes the budget and warns
> about the spill.

| setting | shard bulk | shard serving | coordinator | why |
|---|---|---|---|---|
| `shared_buffers` | 16 GB | 16 GB | 8 GB | 25% of RAM, capped. Larger is **not** better for writes: PostgreSQL still leans on the OS page cache, and an oversized pool lengthens checkpoint scans and enlarges each checkpoint's dirty set. |
| `effective_cache_size` | 96 GB | 96 GB | 24 GB | A shard's whole ~10 GB slice fits here many times over, which is why the reduce phase's aggregate never touches disk. |
| `work_mem` | 256 MB | 64 MB | 32 MB | Reduced 4× for serving (many small queries, not a few huge sorts) and again for the coordinator (single-row lookups). |
| `maintenance_work_mem` | 16 GB | 2 GB | 1 GB | The single largest lever on post-load index-build time, and during the load nothing else on the instance wants memory. |
| `huge_pages` | `try` | `try` | `try` | A 16 GB pool through 4 KB pages needs 4M page-table entries; 2 MB pages cut that to 8,192. `try` so startup survives a host with none reserved — but reserve them and check `SHOW huge_pages_status` says `on`. |

---

## Autovacuum: throttled and redirected, never disabled

Disabling autovacuum during a bulk load is a common recipe and a trap here, because the
two roles need opposite treatment and neither wants it off.

**On a shard**, per-table settings in
[`04_bulk_load_tuning.sql`](../distributed/db/04_bulk_load_tuning.sql) hold it away from
the exposure leaves while preserving the statistics the reduce phase depends on:

| table | `fillfactor` | autovacuum | rationale |
|---|---:|---|---|
| exposure sample leaves | **100** | **off** | Written once by `COPY`, never updated, never deleted. The default `fillfactor = 90` would reserve a tenth of every page for HOT updates that never come — ~10 GB wasted and 10% more pages on every scan, their only access pattern. Switching autovacuum off is safe **only because of `COPY ... FREEZE`**: the tuples arrive already frozen, so there is nothing for a freeze vacuum to do. |
| derived edge partitions | 100 | on, vacuum threshold 2×10⁹ | Also append-only, but this is the serving hot path and the planner's estimate for `WHERE datetime = …` decides between an index scan and a sequential scan of a 2.4M-row partition. `ANALYZE` stays responsive. |
| static geometry | 100 | default | Read by every directional query, never written after replication. |

**On the coordinator**, the most aggressive settings in the whole deployment, and they
belong there:

```
autovacuum_naptime = 15s
autovacuum_vacuum_cost_delay = 0        -- unthrottled
meo_tasks: fillfactor = 70, thresholds absolute (50 rows)
```

`meo_tasks` is a few thousand rows taking ~42 `UPDATE`s each — one claim, ~40 heartbeats,
one completion — so ~250,000 row versions over a run. Left at defaults it bloats within
minutes, its partial indexes degrade, and claim latency, which all 50 workers wait on,
becomes visible.

`fillfactor = 70` is the single most effective setting there: leaving room on the page
lets a heartbeat rewrite the row as a **HOT update, in place, without touching any
index**. Heartbeats are ~95% of the writes, so making them index-free is most of the win.

```sql
SELECT n_tup_upd, n_tup_hot_upd,
       round(100.0*n_tup_hot_upd/nullif(n_tup_upd,0),1) AS pct_hot
FROM pg_stat_user_tables WHERE relname = 'meo_tasks';
```

Want `pct_hot` above ~90. Below that, either the fillfactor did not apply or autovacuum
is not keeping up.

Thresholds are **absolute** rather than scale factors: 0.2 on a 6,000-row table means
1,200 dead rows before vacuum, which at 42 versions per task is only 28 tasks of churn.

The coordinator can afford to be unthrottled precisely *because* it is a separate
instance — there is no bulk load there to protect from vacuum I/O.

---

## Indexes are built after the load, and only on the small table

An index present during a bulk load must be maintained per row: every `COPY`'d tuple does
a B-tree descent and possibly a page split, and concurrent writers turn the upper levels
into a contention point.

Building afterwards instead:

- replaces *N* random descents with one large sequential sort bounded by
  `maintenance_work_mem`;
- parallelises across `max_parallel_maintenance_workers`;
- yields a **dense, unfragmented** tree instead of one ~70% full from splits.

**And the scale is different from what you might expect.** The reduce phase indexes 2.9
million rows per shard, not 158 million, because the sample table gets **no index at
all** — partition pruning reaches one ~261k-row leaf, which is cheaper to scan than a
B-tree descent over 1.58e9 entries, and not building one saves ~60 GB. That asymmetry is
why the reduce phase is seconds rather than hours. See
[OPTIMIZATION.md §11](OPTIMIZATION.md#11-no-index-on-158-billion-rows).

`CONCURRENTLY` is not used: it is disallowed on partitioned tables, and would be the
wrong choice anyway since nothing is reading yet.

### `ANALYZE` is not optional

The leaves went from empty to ~10⁸ rows with autovacuum held away from them, so the
planner's statistics still say "empty". Until `ANALYZE` runs, every query against them
plans as though the tables were tiny and picks catastrophically wrong plans — a nested
loop over 158 million rows, for instance.

`reduce_finalize.py` runs it per shard. For the 576 leaves, `vacuumdb --analyze --jobs 8`
is ~8× faster than doing them serially from one session.

---

## Switching to serving

```bash
# per shard: point the include at the serving profile, then RESTART
#   include_if_exists = '/etc/postgresql/profile.conf'   ->  shard-serving.conf
kubectl -n sunlightcity rollout restart statefulset/sunlit-shard

# then a base backup — the bulk profile could not produce one
for i in $(seq 0 9); do
  kubectl -n sunlightcity exec sunlit-shard-$i -- \
    pg_basebackup -D /backup/$(date +%F) -Ft -z -P -U admin
done
```

`wal_level`, `full_page_writes`, `shared_buffers`, `huge_pages` and
`shared_preload_libraries` are postmaster-level and need a **restart**, not a reload.

**Per-table settings from `04_bulk_load_tuning.sql` persist across the switch** — they
live in `pg_class.reloptions`, not in `postgresql.conf`. That is correct: the exposure
leaves are still append-only and still want `fillfactor = 100`. Reset them explicitly if
that ever changes.

---

## Verifying the tuning did anything

```sql
-- Checkpoints should be RARE. Frequent 'requested' checkpoints mean max_wal_size is
-- still too small — the mistake this file spends the most words on.
SELECT num_timed, num_requested, write_time, sync_time FROM pg_stat_checkpointer;
--  PostgreSQL 16 moved these out of pg_stat_bgwriter.

-- Lock contention. Waits on 'extend' would mean the one-leaf-per-task design is NOT
-- isolating writers — the first thing to check if ingest is below spec.
SELECT wait_event_type, wait_event, count(*)
FROM pg_stat_activity WHERE wait_event IS NOT NULL
GROUP BY 1, 2 ORDER BY 3 DESC;

-- work_mem spills. Non-zero temp_bytes during reduce means raise work_mem.
SELECT datname, temp_files, pg_size_pretty(temp_bytes) FROM pg_stat_database
WHERE datname LIKE 'sunlit%';

-- The WAL-skip, per §wal_level. Kilobytes, not megabytes.
SELECT pg_current_wal_lsn();

-- HOT update ratio on the queue. Want > 90%.
SELECT relname, n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables
WHERE relname IN ('meo_tasks', 'meo_runs');

-- Pool health, on the coordinator's pooler only.
--   psql -h pgbouncer -p 6432 -U admin pgbouncer -c 'SHOW POOLS;'
-- Sustained cl_waiting > 0 means default_pool_size is too small.
```

`log_checkpoints = on` is enabled in every profile for exactly one reason: if the fleet
stalls periodically, the log tells you immediately whether checkpoints are why.
