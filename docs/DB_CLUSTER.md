# The database cluster

Eleven PostgreSQL instances: one coordinator and ten data shards. This document is the
sizing argument, the topology, and the operational consequences.

For why the row count that forces this exists at all, see
[ARCHITECTURE.md §1](ARCHITECTURE.md#1-the-constraint-everything-else-follows-from).
For server parameters, [TUNING.md](TUNING.md). To build it,
[DEPLOYMENT.md](DEPLOYMENT.md).

---

## 1. The arithmetic

Every number below is `python distributed/orchestrator/model.py`. It is executable
precisely so that this page cannot drift from it.

### Demand

```
sample points                365,133
timesteps per day                360      03:00-21:00, 3-minute steps, half-open
days                              12      one representative day per month
                          ────────────
rows                   1,577,374,560

per-worker raycast rate      295,758/s    73,027 x 3.0 batching x 1.35 locality
fleet of 50               14,787,900/s    ← what the database must absorb
```

### Supply, per instance

A `COPY` backend is **single-threaded server-side** — one busy CPU. So an instance's
ingest is bounded by its cores, not by its disk:

```
16 vCPU
 -4        WAL writer, checkpointer, background writer, OS
 ────
 12  productive COPY streams  x  200,000 rows/s  =  2,400,000 rows/s
```

200k rows/s per stream is a binary `COPY` of a 68-byte row into a WAL-skipped relation
— conservative, and the figure `reduce_finalize.py` reports against.

### The answer

```
14,787,900 / 2,400,000  =  6.2   →  minimum 7 shards
deployed                             10        +35% headroom
```

**Why headroom rather than the minimum.** At exactly the balance point, any hiccup on
any instance — a checkpoint, an autovacuum, a slow disk — propagates straight into
fleet stall time, because there is nowhere for the work to go. Ten instances means the
write side finishes in 1m 19s against the raycast side's 1m 47s, so the fleet is
compute-bound with 26% of the writer's time idle. That idle time is the buffer.

**Why not twenty.** Nine seconds, for double the database spend. And past ~25 it gets
*worse*: fifty workers cannot offer enough concurrent streams to keep that many
instances busy, so each one starves.

---

## 2. Topology

```
                        ┌───────────────────┐
      50 workers  ──────│    PgBouncer      │──────┐   control plane only
      (claim,           │  transaction mode │      │   thousands of tiny txns
       heartbeat,       │    2 replicas     │      │
       complete)       └───────────────────┘      │
                                                   ▼
                                        ┌──────────────────────┐
                                        │    COORDINATOR       │
                                        │   8 vCPU / 32 GB     │
                                        │                      │
                                        │  meo_tasks           │  the queue
                                        │  meo_runs            │
                                        │  meo_sections        │  topology
                                        │  meo_shards          │
                                        │  meo_edge_sections   │
                                        │  static geometry     │  authoritative
                                        │  postgres_fdw ───────┼──┐ analytics
                                        └──────────────────────┘  │
                                                                  │
      50 workers  ─────────── DIRECT, no pooler ──────────┐       │
      (binary COPY, 2 streams each)                       │       │
                                                          ▼       ▼
   ┌──────────────┬──────────────┬─────────────────────────────────────────┐
   │  shard 0     │  shard 1     │  …                          shard 9     │
   │ 16 vCPU      │              │                                          │
   │ 128 GB       │              │   ~8 sections each, Hilbert-contiguous   │
   │ 256 GB NVMe  │              │                                          │
   │              │              │                                          │
   │ ~10 GB samples in 576 leaves                                           │
   │ ~0.2 GB derived edge index                                             │
   │ full static geometry replica (~140 MB)                                 │
   └──────────────┴──────────────┴─────────────────────────────────────────┘
```

### Why the coordinator is separate

It holds ~200 MB and could physically live on any shard. Keeping it apart is the point:

> `meo_tasks` is a few thousand rows taking ~250,000 `UPDATE`s over a run — one claim,
> ~40 heartbeats and one completion per task — and **every one of them is on a worker's
> critical path**. The exposure leaves are 100 GB of append-only bulk.
>
> Co-locate them and every claim queues behind a checkpoint flushing a bulk load's
> dirty buffers. Claim latency degrades exactly when the fleet is busiest, which is the
> hardest failure mode to diagnose from outside: no errors, just a fleet that is slower
> than the model says.

Separate instance, separate page cache, separate WAL, separate checkpointer. The
control plane's latency becomes independent of the data plane's throughput.

It is also the only instance whose contents are **not reproducible**: `meo_tasks` *is*
the record of what has been computed. So it keeps full durability while the shards trade
it away — see [TUNING.md](TUNING.md#the-safety-argument-stated-plainly).

### Why 128 GB per shard

A shard holds ~10 GB of samples. 128 GB means the instance's entire slice of the dataset
sits in its page cache, so the reduce phase's aggregate over 158 million rows never
touches disk, and after a warm-up pass routing queries do not either.

That is also why the whole run costs only ~32 vCPU-hours despite 572 vCPU: the hardware
is wide, not held for long.

### Why no pooler in front of the shards

Two reasons, and the first is decisive:

1. **A pooler in a sustained bulk-COPY path is a single-threaded process relaying every
   byte.** At the fleet's ~700 MB/s it would become the bottleneck the cluster was built
   to remove.
2. **There is nothing to pool.** Pooling multiplexes many clients onto few backends;
   sharding has already reduced each instance to ten backends — five workers × two COPY
   streams. Ten is not a number that needs managing.

The reduce Job and the schema Job also connect direct, because their statements are long
and session-affine and would pin a pooled backend for minutes.

---

## 3. Section → shard assignment

Full reasoning in
[ARCHITECTURE.md §4](ARCHITECTURE.md#4-which-instance-owns-which-piece-of-the-city). The
short version: sections are ordered along a **Hilbert curve** and that sequence is cut
into ten **contiguous runs of equal sample count**, because any contiguous run of a
Hilbert curve is a compact connected region — so one cut satisfies both write balance
and read locality.

Inspect it without a database:

```bash
python distributed/orchestrator/cluster.py --show
python distributed/orchestrator/cluster.py --show --shards 14
```

```
  write imbalance      : 1.072x max/mean
  read contiguity      : 0.68  (a hash would give ~0.10)

  Shard layout (row = north/south, col = east/west):
     9 9 . .
     9 9 . .
     9 9 9 .
     8 9 9 .
     8 8 9 .
     8 8 9 9
     8 8 7 7
     ...
```

Each digit is the owning instance. The runs are compact blobs rather than stripes or
speckle, which is the whole point.

`plan_tasks.py` **refuses** a topology whose worst shard exceeds `--max-imbalance`
(default 1.25×), because the slowest instance sets the makespan and discovering that
from a graph three minutes into a run is worse than discovering it from an error.

### When the topology changes

Section ids are stable under re-extraction: the grid origin is snapped down to a
multiple of the section size, so adding a city block does not renumber everything.
`meo_rebuild_sections()` preserves existing `shard_index` values for sections that still
exist, so a re-plan after a small graph change does not move loaded data.

Changing the **shard count** does move data. There is no rebalancing tool, deliberately:
re-running the whole pipeline takes three minutes, which is faster than any migration
would be and leaves nothing to get subtly wrong.

---

## 4. Storage layout inside one shard

```
meo_exposure_samples_p                      partitioned, LIST (section_id)
├─ meo_exp_s384                             this shard owns ~8 sections
│  ├─ meo_exp_s384_20260101_w0              ← one task: 261k rows, ~20 MB
│  ├─ meo_exp_s384_20260101_w1
│  │  … 12 dates x 6 windows = 72 leaves
├─ meo_exp_s385
│  … 
└─ ~576 leaves total, ~10 GB

meo_exposure_edges_p                        partitioned, RANGE (datetime), monthly
└─ 12 partitions/year, ~2.9M rows, ~0.2 GB   ← DERIVED in the reduce phase

meo_sample_points, meo_edges, meo_waypoints  full replicas, ~140 MB
meo_edge_sections                            full map, 6,700 rows
meo_shard_sections                           this shard's sections + weights
meo_shard_identity                           which shard this is
```

**Every shard gets the FULL static geometry**, not only its own sections. It is 140 MB;
holding all of it means a shard can answer any directional query locally without a round
trip, and moving a section between shards moves exposure rows only, never geometry.

**Views named `meo_exposure_samples` and `meo_exposure_edges`** expose exactly v1's
column sets, in v1's order, so every v1 consumer works unchanged. That is asserted in
the self-test rather than assumed.

**576 leaves per shard** is comfortable for query planning. Cluster-wide there are
6,048, but no instance plans over more than its own.

---

## 5. The write path

```sql
-- own transaction, only when attempts > 1
SELECT meo_reset_leaf(section, lo, window);     -- DETACH + DROP the previous attempt

BEGIN;
  SELECT meo_begin_leaf(section, lo, hi, window, task);   -- CREATE, standalone
  COPY <leaf> (...) FROM STDIN (FORMAT BINARY, FREEZE);
  SELECT meo_attach_leaf(section, lo, hi, window);        -- ATTACH PARTITION
COMMIT;
```

Four properties fall out of that shape, and they are the reason the load is as fast as
it is — see
[ARCHITECTURE.md §5](ARCHITECTURE.md#5-one-task-one-partition-leaf): no extension-lock
contention, no WAL for the sample data, `FREEZE` legality, and idempotent retry without
a `DELETE`.

### Lock discipline

The part that is easy to get wrong:

| step | lock | held for |
|---|---|---|
| `meo_reset_leaf` | `ACCESS EXCLUSIVE` on the section parent | milliseconds, own transaction, retries only |
| `meo_begin_leaf` | **none on any parent** — the leaf is standalone | — |
| `COPY` | the leaf only | ~1.3 s |
| `meo_attach_leaf` | `SHARE UPDATE EXCLUSIVE` on the section parent | milliseconds |

Folding the `DETACH` into `begin_leaf` would hold `ACCESS EXCLUSIVE` on the section
parent for the whole minutes-long transaction, serialising every other worker touching
that section. Keeping it separate, and only on retries, is why first attempts pay
nothing for it.

`ATTACH` skips validation because the leaf carries a `CHECK` constraint implying its
bounds. Without that constraint it would sequential-scan 261k rows while holding the
parent lock.

### Atomicity

Until `COMMIT` the rows live in an unattached relation, invisible through the parent. A
task's output appears **all at once or not at all**, with no partially visible
intermediate state for a reader to trip over.

---

## 6. The read paths

### Routing — direct, and the coordinator is not in it

```sql
-- once, at service warm-up: cache the whole 6,700-row map
SELECT edge_id, shard_index, host, port, dbname FROM meo_edge_routing;

-- per request, on the owning shard
SELECT * FROM meo_edge_directional_cost(
    :edge_id, :entry_time, :reverse := false, :walk_speed_mps := 1.35);
```

Returns `sun_seconds`, `shade_seconds`, `pct_sun`, `entered_in_sun`, `exited_in_sun`,
`longest_sun_run_m` and `timesteps_spanned` — computed by walking the ordered sample
series and reading each sample at the timestep the walker is actually there.

For a whole candidate path:

```sql
SELECT * FROM meo_route_plan(ARRAY[...edge ids...]);
--  shard_index | host | port | dbname | edge_count | edge_ids
```

Because the assignment is Hilbert-contiguous, that normally returns **one or two rows**.
`meo_route_locality()` measures the distribution over sampled routes; on the reference
topology 85% of 12-edge routes touch a single shard.

### Analytics — federated

```sql
SELECT * FROM meo_network_snapshot('2026-07-15 11:00:00');
--  edges | samples | sunlit | pct_sunlit | shards_read
```

`postgres_fdw` over ten foreign-table partitions of `meo_exposure_edges_fed`, keyed on
`section_id` — which is why that column is carried in the shard table rather than left
implicit: it is the only thing that lets the coordinator prune to the right instance at
plan time.

Two optimisations, **complementary but never both on one plan node**:

| query shape | optimisation | plan |
|---|---|---|
| aggregate | partitionwise pushdown — each shard returns one row | `Append → Partial Aggregate → Foreign Scan` |
| row-returning | async append — the ten reads run concurrently | `Append → Async Foreign Scan` |
| filtered on `section_id` | pruning — one shard | `Foreign Scan` (no `Append`) |

All three verified. Do not "fix" the aggregate plan for lacking the word *Async* — the
pushdown is the better outcome, because almost no data crosses the network.

---

## 7. Operating it

### The one view to watch during a run

```sql
SELECT * FROM meo_shard_progress WHERE run_id = 'run-2026-annual';
```

```
 shard | state  | admission_cap | tasks_running | tasks_done | pct_done
     0 | online |             6 |             6 |        486 |     80.4
     1 | online |             6 |             6 |        492 |     81.3
 ...
```

`monitor.py --watch` renders the same thing with a per-shard load sparkline.

| symptom | meaning |
|---|---|
| some shards at `admission_cap`, others at 0 | the topology is unbalanced, or retries have clustered in one region. The makespan is being set by the busy ones. |
| every shard at cap | **healthy** — the steady state for a fleet larger than shards × cap |
| a shard at 0 with work pending | it is `draining`/`offline` in `meo_shards`, or unreachable |
| affinity hit rate below ~85% | dispatch is thrashing the geometry and BVH working sets; the map phase will run long |
| `hb` climbing past 300 s | a worker is a third of the way to being reaped |

### Replacing an instance mid-run

Workers resolve shard endpoints from `meo_shards` at boot, not from environment, which
is what makes this possible without redeploying 50 pods:

```sql
-- stop dispatching to it; tasks in flight finish rather than failing
UPDATE meo_shards SET state = 'draining' WHERE shard_index = 3;
```

Then replace the pod, re-apply its schema, and bring it back:

```bash
python apply_schema.py --phase load --only-shard 3
```
```sql
UPDATE meo_shards SET state = 'online' WHERE shard_index = 3;
```

Any of its tasks that were in flight fail their next heartbeat, are reaped, and
re-dispatch to the replacement. Nothing else is affected: the other nine instances never
knew.

### Losing a shard entirely

Its ~10 GB is reproducible from (mesh, ephemeris, section, date, window). Reset its
tasks and let the fleet redo them:

```sql
UPDATE meo_tasks SET state = 'pending', attempts = 0, worker_id = NULL
 WHERE run_id = 'run-2026-annual' AND shard_index = 3;
```

That is ~605 tasks — about 20 seconds of fleet time. Which is the whole reason the bulk
profile is allowed to trade durability for throughput.

---

## 8. What it costs

| | count | each | total |
|---|---:|---|---|
| map workers | 50 | 8 vCPU / 16 GB | 400 vCPU / 800 GB |
| data shards | 10 | 16 vCPU / 128 GB / 256 GB NVMe | 160 vCPU / 1280 GB / 2.5 TB |
| coordinator | 1 | 8 vCPU / 32 GB | 8 vCPU / 32 GB |
| PgBouncer | 2 | 2 vCPU / 1 GB | 4 vCPU / 2 GB |
| | | | **572 vCPU / 2114 GB** |

**~32 vCPU-hours** for a full annual run. Wide, not long.

The shards are most of the RAM, deliberately — see §2. If that is not available, the
model degrades gracefully: `model.py --sweep` shows four shards still reaching 76×, and
`pg_tune.py` will size a smaller instance correctly and warn if the fleet will be
waiting on it.

This workload is also an unusually good fit for **spot/preemptible** map workers:
lease-based recovery means a reclaimed task costs at most one task's work, and the
entrypoint releases the lease on `SIGTERM` so recovery is immediate rather than
lease-timeout bound. The *databases* should not be spot — losing one costs 20 seconds of
recompute, but losing several at once costs the run.
