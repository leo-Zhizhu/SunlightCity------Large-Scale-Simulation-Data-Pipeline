# Architecture — the distributed pipeline

Why v2 is shaped the way it is. For v1, which defines the schema and produced the
reference dataset, see [V1_PIPELINE.md](V1_PIPELINE.md). For the database cluster in
detail, [DB_CLUSTER.md](DB_CLUSTER.md). For the low-level work inside a worker,
[OPTIMIZATION.md](OPTIMIZATION.md). To deploy it, [DEPLOYMENT.md](DEPLOYMENT.md).

---

## 1. The constraint everything else follows from

v1 wrote 1,577,374,560 rows in **6 hours**, occupying 110 GB. The obvious read
is "raycasting is slow, add machines". That read is wrong, and getting it right
determines the whole design.

**The schema is fixed at sample-point resolution.** One row per (sample point,
timestamp), exactly as v1 wrote it. The table is **fully normalised** —
`(sample_point_id, datetime, is_sunlit)`, one row per observation carrying one bit — so
the row count *is* the observation count. There is no array and no column per timestep:

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/row_anatomy_dark.svg">
  <img src="assets/row_anatomy_light.svg" alt="Anatomy of one row: 68 bytes of page — 28 of tuple overhead, 24 of identity, 12 of bookkeeping, 3 of alignment — carrying a single bit of measurement, 0.18 percent payload. Beside it, the multiplication behind the row count: 365,133 sample points times 360 timesteps a day times 60 dates equals 7,886,872,800 rows, which is also exactly 7,886,872,800 observations." width="850">
</picture>
</div>

That encoding is expensive and knowingly so: **68 bytes of page per bit of payload.** The
packed alternative — lossless, measured 225× smaller — and the reason it is not adopted
are in [DB_CLUSTER.md](DB_CLUSTER.md#what-one-row-is-precisely).

### The question an aggregate cannot answer

Nobody experiences a street at a single instant. A walker enters it, and by the time they
are halfway across **the shadow has moved** — so walking east and walking west sample
*different* (sample point, timestamp) pairs against the same advancing clock.

The figure below is one 400 m edge drawn as a **space-time field**: distance along the
street across, time upwards, and **one cell for every row of `meo_exposure_samples`**. The
shadow's edge steps 6.8 m to the right at each 3-minute timestep, so the two walks —
opposite diagonals through that field — cross it in different places and read different
cells.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/directional_cost_dark.svg">
  <img src="assets/directional_cost_light.svg" alt="A space-time field for one 400 metre street: distance across, time upwards, one cell per row of the samples table. The shadow's edge steps 6.8 metres right at every 3-minute timestep, so the forward walk crosses it at 150 metres and the reverse walk at 154 metres, giving 504 versus 492 seconds of sun and 252 versus 246 metres of continuous exposure. A frozen shadow — which is what a per-edge sum assumes — would report 532 seconds for both." width="850">
</picture>
</div>

[`meo_edge_directional_cost()`](../distributed/db/03_shard_schema.sql) is what reads one of
those diagonals. The table below **is** the figure above — same edge, same instant, both
directions, on the fixture that
[`shard_selftest.sql`](../distributed/db/tests/shard_selftest.sql) builds and asserts:

| | forward | reverse | |
|---|---:|---:|---|
| `sun_seconds` | **504.0** | **492.0** | 12 s more, walking with the sweep |
| `shade_seconds` | 300.0 | 312.0 | |
| `pct_sun` | 62.69 | 61.19 | |
| `entered_in_sun` → `exited_in_sun` | `f` → `t` | `t` → `f` | inverted |
| `longest_sun_run_m` | 252 | 246 | the number a shade-averse router wants |
| `timesteps_spanned` | 5 | 5 | the walk is not one instant |
| per-edge `sunlit_sum` at 16:00 | 133 / 201 | 133 / 201 | **identical — there is no direction in it** |

That last row is the whole argument. Both directions cross the same sample points, so a
per-edge sum is **the same number** for the two; the difference exists only in the
*ordered* series. `sunlit_sum` is therefore a **derived convenience index** — good for a
Pareto search's coarse objective, incapable of the directional query — and the 7.89
billion sample rows are the product.

So the row count stands, and the real problem appears:

**One PostgreSQL instance cannot absorb 7.89 billion rows from 54 concurrent
producers.** A COPY backend is one busy CPU, so a 16 vCPU instance sustains about
twelve productive streams — ~2.4M rows/s — while a 54-worker fleet produces 15.97M
rows/s. Six sevenths of the fleet would sit waiting.

Adding workers alone would have bought almost nothing — and not asymptotically, but
absolutely. Every curve below is a fixed shard count, and each one flattens at *its*
ingest ceiling rather than at anything to do with the fleet:

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/worker_ceiling_dark.svg">
  <img src="assets/worker_ceiling_light.svg" alt="Wall clock against fleet size for 4, 6, 9 and 14 shards. Every curve flattens at its own database ingest ceiling; the four-shard curve flattens above the 15-minute deadline and never crosses it." width="880">
</picture>
</div>

**Four shards cannot meet the deadline with any fleet at all.** That curve levels off at
18m 41s and stays there however many pods are added; two shards levels off at 36m 07s.
Past the ceiling each additional worker raycasts into a queue.

Where the two rates come from, and the gap between them:

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/bench_ladder_dark.svg">
  <img src="assets/bench_ladder_light.svg" alt="Two throughput chains built from individual benchmarks: the fleet produces 15.97M rows per second and the cluster absorbs 21.6M, a 35% headroom." width="880">
</picture>
</div>

One instance sustains `2,400,000 / 295,758 = 8.11` workers' output, so at two `COPY`
streams per worker it feeds **six**. That single ratio is where `W = 6S` comes from, and
with it the entire deployment shape — see
[PERFORMANCE.md §2](PERFORMANCE.md#2-step-1--measuring-the-pieces).

---

## 2. What the cluster is worth

Workers held fixed at 54; only the number of database instances varies.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/shard_scaling_dark.svg">
  <img src="assets/shard_scaling_light.svg" alt="Wall clock against shard count at fixed 54 workers: one instance takes 1 hour 11 minutes, nine take 11 minutes 9 seconds" width="850">
</picture>
</div>

| shards | wall clock | vs v1 | bound by |
|---:|---:|---:|---|
| 1 | 1.18 h | 25.4× | I/O |
| 4 | 18m 41s | 96.3× | I/O |
| 8 | 11m 21s | 158.6× | compute |
| **9** | **11m 09s** | **161.5×** | **compute** |
| 20 | 10m 14s | 176.0× | compute |
| 30 | 12m 42s | 141.7× | I/O again |

Three things to take from that table.

**The cluster is worth 6.4× of the 161.5× total.** The remaining 4.05× per worker comes
from batched raycasts and BVH locality ([OPTIMIZATION.md](OPTIMIZATION.md)); 54 pods
multiply it. Neither half alone gets close.

**Nine is derived, not maximal.** The bare minimum for this fleet is seven; nine gives
**+35% ingest headroom**, so a checkpoint or an autovacuum on one instance cannot stall
the fleet. Twenty would buy 55 seconds for more than double the database spend.

**Past ~27 it gets worse.** 54 workers cannot offer enough concurrent streams to keep
that many instances busy, so each one starves. More hardware is not monotonically
better, and the model says where the turn is.

Everything above is `python distributed/orchestrator/model.py --sweep`. The model is
executable so the docs cannot drift from it.

---

## 3. Why bounding-box sharding is correct here

The natural objection to spatial sharding is that a building *outside* a section casts
shadows *into* it, so sections are not independent. Earlier notes in this repository
called it a trap. That was wrong, and the reason is a property v1 already had:

> A building of height *H* casts a shadow reaching *H* / tan(θ) horizontally at sun
> elevation θ. Below `SUN_ANGLE_THRESHOLD` (5°) the worker declares shadow **without
> raycasting at all** — the horizon guard, which exists for
> [numerical reasons](V1_PIPELINE.md#the-shadow-test). So θ is bounded below, and the
> shadow reach is bounded above:
>
> **200 m / tan 5° = 2,286 m.**

Nothing more than 2,286 m outside a section can influence a sample inside it. That is
an **exact bound**, not a heuristic — which makes sections genuinely independent units
of work.

A worker holds the **whole city mesh** anyway (~6 GB; pods have 16), so seam
correctness is automatic rather than something the sharding has to enforce. The old
objection — "a spatially sharded worker must load the whole mesh, so it saves no
memory" — is correct and turns out not to matter: memory was never the constraint.

What sectioning actually buys is three things:

1. **The data mapping.** One task = one section-date-window = one partition leaf. See
   §5.
2. **Ray coherence.** Every ray in a task originates inside the same square kilometre,
   so the BVH working set is bounded by section + halo (~9 km² against the city's 59)
   and stays resident in cache. Worth 1.35× on the raycast rate.
3. **Read locality.** A pedestrian route is spatially local, so with a
   spatially-coherent shard assignment it touches one or two instances instead of ten.

### Sections own whole EDGES, not whole sample points

The assignment is by **edge midpoint**, and every sample point follows its edge. This
is the decision that keeps the reduce phase shard-local:

| assign by | consequence |
|---|---|
| **edge midpoint** ✅ | a section owns whole edges → `GROUP BY (edge_id, datetime)` completes inside one instance → no shuffle, and no routing query is ever a cross-shard join |
| sample-point position ❌ | ~12% of edges straddle a boundary → the rollup needs a distributed sum, and every routing query becomes a cross-shard join *for the life of the dataset* |

The cost is a ragged section boundary: an edge whose midpoint is just inside a section
may reach up to half its length outside it. Edges here are under 400 m, so the
overhang is bounded by ~200 m against a 2,286 m halo — absorbed by a bound already
respected.

---

## 4. Which instance owns which piece of the city

Two requirements pull against each other.

**Write balance.** During the map phase all ten instances must finish together.
Manhattan is not uniform — midtown has several times the road density of the northern
tip — so equal *area* per shard would mean wildly unequal *rows* per shard, and the
slowest instance would set the makespan.

**Read locality.** A 2 km walk touches a handful of adjacent sections. If adjacent
sections live on different instances, every route query fans out across the cluster and
pays the slowest one.

Hashing section ids gives balance and destroys locality. Ten contiguous stripes do the
reverse.

**The resolution: order the sections along a Hilbert curve, then cut that
one-dimensional sequence into k contiguous runs of equal weight.**

A Hilbert curve visits every cell of a grid such that consecutive positions are
adjacent, and — the property that matters — *any contiguous run of the curve maps to a
compact, connected region of the plane*. So a contiguous run is spatially local by
construction, while cutting by cumulative **sample count** rather than by length makes
the runs equal in rows. Both requirements, no compromise.

Measured on the reference topology:

| | Hilbert + balanced cut | a hash of section ids |
|---|---:|---:|
| write imbalance (max/mean) | **1.05×** | ~1.0× |
| read contiguity | **0.66** | ~0.11 |
| routes touching one shard | **85%** | ~30% |

The cut is **exact, not greedy**: minimising the heaviest of *k* contiguous runs is the
classic linear-partition problem, solved by binary search on the bound plus a greedy
feasibility test. `python distributed/orchestrator/cluster.py --show` prints the
layout.

It lives in Python and is written into `meo_sections.shard_index` rather than being
reimplemented in plpgsql, because having an exact algorithm in two languages is two
chances to make it subtly different.

---

## 5. One task, one partition leaf

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/partition_tree_dark.svg">
  <img src="assets/partition_tree_light.svg" alt="The two-level partition tree drawn as nested boxes. meo_exposure_samples_p is partitioned by LIST(section_id); each section such as meo_exp_s384 is partitioned by RANGE(datetime) into six 3-hour windows per date; each leaf, such as meo_exp_s384_20260101_w0, holds about 261,000 rows in 17 MB and is written by exactly one task. 84 sections times 60 dates times 6 windows gives 30,240 leaves and 30,240 tasks. Four consequences: no lock contention, zero WAL, COPY FREEZE is legal, and a retry needs no DELETE." width="880">
</picture>
</div>

30,240 tasks, 30,240 leaves, ~261k rows and ~20 MB each. That single correspondence buys
four things at once:

**No extension-lock contention.** Concurrent `COPY` into one heap serialises on the
relation extension lock — every backend needing a new 8 KB page queues on the same
lock, and that is *the* bottleneck for parallel bulk load. Here each writer extends a
relation nobody else can see.

**No WAL at all for the sample data.** `COPY` into a relation created in the **same
transaction** skips WAL entirely under `wal_level = minimal`. Because the leaf is built
from scratch per task rather than appended to a pre-existing partition, all ~500 GB is
written without a WAL record. Not reduced — skipped.

**`COPY ... FREEZE` is legal**, for the same reason. Tuples land already frozen, so
there is no hint-bit write on first read and no freeze-vacuum of 7.89 billion rows to
pay later.

**Idempotent retry without a `DELETE`.** Replacing a task's output is
`DETACH` + `DROP` + rebuild — catalog work. The alternative,
`DELETE ... WHERE task_id = N` over 261k rows, would generate WAL, bloat and vacuum
debt on every retry.

The lock discipline is the part that is easy to get wrong, and it is documented in
[`03_shard_schema.sql`](../distributed/db/03_shard_schema.sql): the leaf is created
**standalone** so the minutes-long `COPY` holds no lock on any parent; the `DETACH`
needed on a retry runs in its own short transaction; and the leaf carries a `CHECK`
constraint implying its bounds so `ATTACH` skips validation instead of sequential-
scanning 261k rows under a lock.

### No index on the sample table

7.89 billion rows, no index, deliberately. Partition pruning reaches one ~261k-row leaf
— verified in the self-test — and scanning that is cheaper than descending a B-tree
over 7.89e9 entries. Not building one also saves ~300 GB and all the load-time
maintenance. **Pruning is the index.**

---

## 6. Coordinating the fleet with the cluster

This is where the Kubernetes side and the database side meet, and it is one predicate.

A shard absorbs ~12 concurrent COPY streams before extra streams stop buying
throughput; each worker holds two. So **at most six workers should write to any one
shard at a time**. Nothing about the work distribution guarantees that: sections are
not claimed uniformly, and a burst of retries in one region would happily point thirty
workers at one instance, collapsing its throughput while nine peers idle.

So `meo_claim_task()` refuses to hand out a task whose shard is already at capacity:

```sql
AND t.shard_index = ANY (SELECT meo_admissible_shards(run_id))
```

At the deployed shape it is exact — 9 shards × 6 = 54 slots for 54 workers, because the
fleet size was derived as 6 × the shard count ([PERFORMANCE.md §2](PERFORMANCE.md#2-step-1--measuring-the-pieces)).
So it never binds at full health. Under
skew it is what keeps the cluster's aggregate ingest flat rather than concentrated.

Two more concerns share the same function, in priority order:

**Affinity.** A task's rays all originate in one section during one window, so its
geometry and BVH pages are a working set the worker already has. There are 84 × 6 = 504
such working sets but 30,240 tasks, so dispatching a task matching the caller's current
(section, window) reuses the warm set 60 times out of 60 — one per date. Without affinity the
fleet would fault in a fresh working set 30,240 times; with it, 504. The hints are
advisory — if nothing matches, the claim falls straight through to LPT, so affinity can
never stall the queue or unbalance the cluster.

**LPT.** Otherwise take the most expensive admissible task. Cost is estimated **per
window**, which matters far more than v1's per-day estimate would: a 03:00–06:00 window
in December is entirely below the horizon guard and costs ~1 timestep, while the same
window in June is most of a sunrise. The measured spread is **780×**. Longest-
processing-time-first bounds makespan at 4/3 of optimal.

All eight of those semantics are asserted in
[`queue_selftest.sql`](../distributed/db/tests/queue_selftest.sql).

---

## 7. Failure handling

Tasks are **leased**, not dequeued. A worker stamps its task *"mine until now + 120 s"*
and re-stamps it every 30 s. Stop re-stamping and the lease lapses; the next reaper sweep
returns the task to the queue.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/failure_timeline_dark.svg">
  <img src="assets/failure_timeline_light.svg" alt="Timeline of a worker killed mid-task: a 120 second lease stops being renewed, expires, and a reaper sweep within 60 seconds returns the task to the queue, where another worker reclaims and completes it. A 180 second recovery window inside an 8 minute 14 second map phase." width="850">
</picture>
</div>

### Sizing the lease — it has two bounds, and only one is local

| | bound | what happens outside it |
|---|---:|---|
| **floor** | 90 s (3 × heartbeat) | a worker that is merely slow — a database stall, a GC pause — is declared dead while still working, and two workers do the same task |
| **ceiling** | 433 s (map phase − reaper period) | the lease outlives the run. A dead worker is still holding its task when the queue drains, the survivors exit on empty polls, the Job reports Complete, and the run is one task short |

**120 s** sits between them: four missed heartbeats before death is declared, a
**180-second** worst-case recovery (lease + one sweep), and a death anywhere in the first
**63%** of the map phase is fully absorbed.

> **This was 900 s, and the ceiling is why that was a bug.** 900 s clears the floor by a
> factor of a thousand — which is the only bound the original reasoning considered — and
> exceeds the ceiling by a factor of eight. The recovery path could therefore never fire
> inside a run. Nothing shipped wrong, because `reduce_finalize`'s completeness check
> catches the gap and exits 2, but "the fleet absorbs a dead worker" had quietly become
> "re-run the whole thing".
>
> The ceiling is not local to a worker — a worker knows its lease but has no idea how long
> the run is — which is why nothing caught it. `model.lease_bounds()` states both bounds,
> `plan_tasks.py` refuses a run outside them, and `queue_selftest.sql` asserts the SQL
> default clears the floor and is far below any plausible run.

A death in the last 180 s of the map phase cannot be absorbed by *any* lease length. That
is a property of the problem rather than of the setting, and it is precisely why the
completeness check at the end of the reduce phase exists.

**An unrenewed lease IS the failure signal.** There is no coordinator to detect the
death and no cleanup path to get wrong. That is not merely less code — it is strictly
more correct than the alternatives:

| detector | misses |
|---|---|
| pod-death watch | network partition · frozen kernel · container running but wedged |
| liveness probe | a process alive but making no progress; adds false positives in long GC pauses |
| **lease expiry** | **nothing** — it observes progress being *reported*, the only thing that matters |

Which is why the map Job deliberately ships **no `livenessProbe`**: it could only add
false positives to a signal the heartbeat already covers better.

Three details make it safe rather than merely convenient.

**Fencing.** `meo_heartbeat()` returns a boolean. A worker that sees `false` has lost
ownership and abandons its work immediately — otherwise the original and its
replacement would both build the same partition leaf.

**Idempotency.** Output is one leaf keyed by (section, date, window), replaced
wholesale. At-least-once delivery is therefore sufficient, and exactly-once — which is
unachievable across a process boundary — is never needed.

**Reaping frees the admission slot, not just the task.** A dead worker holds one of its
shard's six slots until its lease expires. Without reaping, every node failure would
permanently shrink the cluster's usable write concurrency for the rest of the run — and
that degradation is invisible: no error, just a fleet gradually getting slower.

### Completion ordering

A task is marked done on the coordinator **only after its rows commit**. Marking it
earlier would let a crash in between leave a task recorded as complete with no data —
which the completeness check would pass, and which would surface months later as a
street with no shade at any hour.

That is why a task is completed one loop iteration after it is computed: the flush runs
on a background thread ([OPTIMIZATION.md](OPTIMIZATION.md#3-the-flush-overlaps-the-next-tasks-raycasting))
and the main loop reaps completions separately from producing them.

---

## 8. Where the time goes

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/phase_breakdown_dark.svg">
  <img src="assets/phase_breakdown_light.svg" alt="Breakdown of the 11 minute 9 second run: 45 seconds spin-up, 8 minutes 14 seconds map with writing fully overlapped, 2 minutes 10 seconds reduce" width="850">
</picture>
</div>

| phase | time | share | |
|---|---:|---:|---|
| fleet spin-up | 45 s | 7% | image pull (warm) + engine boot + scene load + BVH warm |
| **map** | **8m 14s** | **74%** | `max(raycast 8m 14s, write 6m 05s)` — compute-bound |
| reduce | 2m 10s | 19% | nine shards in parallel |
| **total** | **11m 09s** | | **161.5× v1** |

**Writing is free.** The map phase costs `max(raycast, write)`, not their sum, because
a finished window is handed to a writer thread on a second connection while the main
thread claims the next task. Done in sequence the fleet would spend 43% of its life on
sockets.

**Spin-up is counted, not waved away.** It is 7% of wall clock, and together with the
30 s `ANALYZE` it forms a **75-second floor that no amount of hardware beats** — which is
why the sizing problem is spending the other 825 s of the budget well
([PERFORMANCE.md §3](PERFORMANCE.md#3-step-2--assembling-the-pieces-into-one-formula)).
Pretending otherwise would make the model wrong in the one direction that flatters it.

**Reduce cannot overlap** — it needs the last row — but it is only 2 min because a
section owns whole edges, so all nine shards roll up locally with no shuffle.

Full numbers, including the honest accounting of the 161.5× achieved against a 218.7×
theoretical ceiling — and the derivation that produced 54 workers and 9 shards in the
first place: [PERFORMANCE.md](PERFORMANCE.md).

---

## 9. Two access paths, on purpose

Sharding is only a win if reads stay cheap, and the two kinds of read want opposite
things.

**Routing** — "what does edge E cost, walked this way, at 14:12?" — is thousands per
second, each touching one edge on one shard. The client asks the coordinator "which
shard?" once, caches the whole 6,700-row map, and thereafter **connects directly to the
owning shard**. Proxying that traffic through one instance would make it the bottleneck
for a workload that is otherwise perfectly parallel.

```sql
-- once, at warm-up, on the coordinator
SELECT * FROM meo_edge_shard(:edge_id);
-- then, per request, on that shard
SELECT * FROM meo_edge_directional_cost(:edge_id, :entry_time, :reverse, :walk_speed);
```

**Analytics** — "sunlit fraction of every street at 11:00 in July" — is rare and
genuinely spans the city, so one SQL statement over all nine shards is what you want.
That is `postgres_fdw`, and the two relevant optimisations are complementary but never
both on one plan node: an aggregate gets **partitionwise pushdown** (each shard returns
one row), a row-returning query gets **Async Foreign Scan** (the ten reads run
concurrently). Both plans are verified in
[`06_serving_federation.sql`](../distributed/db/06_serving_federation.sql).

Same reasoning fronts the coordinator with PgBouncer and leaves the shards bare: a
transaction pooler is right for thousands of tiny transactions and exactly wrong for a
sustained bulk byte stream, where it would be a single-threaded proxy relaying
~700 MB/s.

---

## 10. Component map

| Concern | Where |
|---|---|
| Batched shadow test, bitset results | [`SectionExposureSampler.cs`](../distributed/unity/Runtime/SectionExposureSampler.cs) |
| Binary COPY on a background thread | [`ExposureWriter.cs`](../distributed/unity/Runtime/ExposureWriter.cs) |
| Section → shard resolution, two connections | [`ShardRouter.cs`](../distributed/unity/Runtime/ShardRouter.cs) |
| The three-language section-id contract | [`SectionGrid.cs`](../distributed/unity/Runtime/SectionGrid.cs) |
| Claim / heartbeat / fence | [`WorkQueueClient.cs`](../distributed/unity/Runtime/WorkQueueClient.cs) + [`02_work_queue.sql`](../distributed/db/02_work_queue.sql) |
| Worker lifecycle, SIGTERM | [`HeadlessExposureWorker.cs`](../distributed/unity/Runtime/HeadlessExposureWorker.cs) + [`entrypoint.sh`](../distributed/docker/entrypoint.sh) |
| Headless build settings | [`HeadlessBuildScript.cs`](../distributed/unity/Editor/HeadlessBuildScript.cs) |
| Grid, sections, Hilbert, shard registry | [`01_cluster_topology.sql`](../distributed/db/01_cluster_topology.sql) |
| Admission control + affinity claim | [`02_work_queue.sql`](../distributed/db/02_work_queue.sql) |
| Partition tree, leaf lifecycle, directional API | [`03_shard_schema.sql`](../distributed/db/03_shard_schema.sql) |
| Per-shard rollup, integrity views | [`05_post_load_indexes.sql`](../distributed/db/05_post_load_indexes.sql) |
| Balanced cut, endpoint resolution | [`cluster.py`](../distributed/orchestrator/cluster.py) |
| Sizing — the source of every figure | [`model.py`](../distributed/orchestrator/model.py) |
| Schema bootstrap across 10 instances | [`apply_schema.py`](../distributed/orchestrator/apply_schema.py) |
| Topology derivation + provisioning | [`plan_tasks.py`](../distributed/orchestrator/plan_tasks.py) |
| Shard fan-out, completeness, federation | [`reduce_finalize.py`](../distributed/orchestrator/reduce_finalize.py) |

---

## 11. What is verified, and how

The schema and queue semantics are asserted against a real PostgreSQL 16 + PostGIS 3.4,
not argued for in prose. `distributed/db/tests/run_selftest.sh` builds a throwaway
database, applies the schema, and runs **48 assertions**:

| | |
|---|---|
| [`shard_selftest.sql`](../distributed/db/tests/shard_selftest.sql) | v1 column compatibility · window tiling · the create-then-attach write path · `ATTACH` skipping validation · pruning to one leaf · idempotent retry · rollup exactness against a direct aggregate · **directional asymmetry** · orphan sweep |
| [`queue_selftest.sql`](../distributed/db/tests/queue_selftest.sql) | LPT ordering · affinity beating LPT · affinity falling through rather than stalling · admission control admitting exactly its cap · completion releasing exactly one slot · fencing · reaping freeing the slot · bounded retries |

The orchestrator was exercised end to end against a live three-shard cluster: grid
derivation, edge-to-section assignment, the balanced cut, binary geometry replication
across instances, partition provisioning, task insertion, the monitor dashboard, and
the full reduce including the federation.

The three `postgresql.*.conf` reference profiles are checked to match `pg_tune.py`'s
output exactly (46, 48 and 48 settings), so the documented reasoning and the generator
cannot drift.

### Assumptions that only a real cluster can confirm

Stated so the first deployment knows what to watch:

1. **PhysX raycasting behaves identically in a headless Server-subtarget build.**
   `RaycastCommand` is a CPU BVH traversal with no GPU involvement. This is the
   assumption the entire containerisation rests on, which is why
   [DEPLOYMENT.md](DEPLOYMENT.md) puts a two-worker smoke run before the fleet.
2. **The job system sizes its worker pool from the cgroup quota.** If it reads the
   host's core count instead, a 54-pod node would oversubscribe badly. This is why the
   Job requests whole cores.
3. **`wal_level = minimal` + same-transaction `CREATE` + `COPY` skips WAL.** Documented
   PostgreSQL behaviour; verify with `pg_current_wal_lsn()` before and after one task.
4. **IL2CPP needs managed stripping disabled for Npgsql**, which resolves type handlers
   reflectively. The most likely cause of a "works in the Editor, breaks in the
   container" failure.
5. **The per-worker rate.** 3.0× from batching and 1.35× from BVH locality are the
   model's inputs; `reduce_finalize.py` reports the achieved figure against them so the
   first real run replaces the estimate.
