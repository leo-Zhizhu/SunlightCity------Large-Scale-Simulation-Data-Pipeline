# Performance

How the deployment was sized, what it achieves, and where the speedup stops.

This document is a **derivation**, not a report. The hardware was not chosen and then
measured — a deadline was set, the unit rates were benchmarked, and the fleet and cluster
sizes fall out of the two. Read §1–§6 in order and the numbers in §7 onward are already
justified.

Every figure here is `python distributed/orchestrator/model.py`. That module is the single
source for this file, the README, the generated charts and `reduce_finalize.py`'s
throughput report, so none of them can disagree. The derivation itself is executable:

```bash
python distributed/orchestrator/model.py --derive
```

---

## 1. The requirement

Two things are fixed before any hardware is discussed.

**The work is non-negotiable.** 365,133 sample points × 360 timesteps × 60 dates =
**7,886,872,800 observations**, at v1's exact schema — one row per (sample point,
timestamp). The downstream router traverses an edge as an ordered, *directional* sequence
of sample points, so the per-sample series is the product, not an intermediate. Nothing
here is aggregated away to make the numbers easier. See
[DB_CLUSTER.md](DB_CLUSTER.md#what-one-row-is-precisely).

**The deadline is 15 minutes.** A full 60-date run, end to end, spin-up to `ANALYZE`.

At v1's measured sustained rate the same 60 dates would take **30.0 hours**, so the
pipeline has to be **120× faster**, work-normalised. That is the whole problem statement.

Everything below — 54 workers, 9 database instances, 588 vCPU — is an *output*.

---

## 2. Step 1 — the measurement ladder

Nine benchmarks, each isolating one lever. `python model.py --bench` prints them with
their methods; a rate with no provenance is a guess with a decimal point.

| | lever | measured | sets |
|---|---|---:|---|
| **B1** | v1, one thread, end to end | 73,027 rows/s | `V1_ROW_RATE` |
| **B2** | batched raycast dispatch | ×3.00 | `BATCH_SPEEDUP` |
| **B3** | section-local BVH locality | ×1.35 | `LOCALITY_SPEEDUP` |
| **B4** | one binary `COPY` stream | 200,000 rows/s | `COPY_ROWS_PER_STREAM` |
| **B5** | streams per instance before contention | 12 | `shard_max_streams()` |
| **B6** | edge rollup aggregate | 12,000,000 rows/s | `EDGE_ROLLUP_ROWS_PER_S` |
| **B7** | rollup index build | 600,000 rows/s | `INDEX_BUILD_ROWS_PER_S` |
| **B8** | fleet spin-up | 45 s | `FLEET_STARTUP_SECONDS` |
| **B9** | `ANALYZE` the leaf tree | 30 s | `ANALYZE_SECONDS` |

Three of them deserve their reasoning stated, because each is a place where the obvious
number is wrong.

**B1 is the only whole-pipeline measurement.** It is v1's own reference run —
1,577,374,560 rows in 6.00 h on one desktop, main-thread `Physics.Raycast`, one
PostgreSQL. Everything else in the table is a microbenchmark, so B1 is what anchors them
to reality. See [V1_PIPELINE.md](V1_PIPELINE.md).

**B2 is 3.0×, not 8×, on an 8 vCPU pod.** `RaycastCommand.ScheduleBatch` fans a whole
timestep's rays across the job system's threads, but the main thread still schedules,
completes and folds results, and BVH traversal saturates memory bandwidth well before it
saturates ALUs. Claiming linear scaling here would have propagated a 2.7× error into every
figure downstream.

**B5 is the constraint that produces the cluster.** Sweeping 1→20 concurrent `COPY`
streams into *distinct* relations on one 16 vCPU instance scales linearly to 12, then
flattens: a `COPY` backend is one busy CPU, and the WAL writer, checkpointer, bgwriter and
OS need the rest. *Distinct* relations is what makes it linear at all — streams into one
relation serialise on its extension lock, which is why every task builds its own partition
leaf.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/bench_ladder_dark.svg">
  <img src="assets/bench_ladder_light.svg" alt="Two throughput chains built from individual benchmarks: the fleet produces 15.97M rows per second and the cluster absorbs 21.6M, a 35% headroom" width="880">
</picture>
</div>

Composed, the two chains that have to meet:

```
fleet    B1 × B2 × B3        73,027 × 3.00 × 1.35  =    295,758 rows/s per worker
cluster  B4 × B5            200,000 × 12          =  2,400,000 rows/s per instance

one instance sustains  2,400,000 / 295,758  =  8.11 workers' output
at 2 COPY streams per worker, it therefore feeds  12 / 2  =  6 workers
```

That last ratio is the single most consequential number in the design. **W = 6S** — and
every structural decision downstream, including the queue's admission arithmetic, follows
from it.

---

## 3. Step 2 — the composed model, and the floor

```
T(W,S)  =   45   +   max( 26,667/W , 3,286/S )   +   898/S + 30
            ^B8          ^B1-B3      ^B4-B5           ^B6-B7  ^B9
            spin-up      raycast     write            reduce
                         \____ overlap: max() ____/
```

The map phase costs `max`, not `sum`, because a finished window goes to a writer thread on
a second connection while the main thread claims the next task. Run in sequence the fleet
would spend 42% of its life on sockets.

**The irreducible floor is 75 s.** Spin-up (B8) and `ANALYZE` (B9) shrink with neither
workers nor shards. They are 8% of the budget at any hardware, so the entire sizing problem
is spending the remaining **825 s** well. No configuration, at any price, beats 75 s.

---

## 4. Step 3 — the naive answer, and why it is rejected

Minimise vCPU subject to `T ≤ 900 s` and you get **40 workers / 6 shards, 428 vCPU,
14m 51s**. A 1.0% margin. It is the cheapest correct answer to the question as literally
posed, and it is not deployable:

| condition | 40 / 6 | |
|---|---:|---|
| every rate as benchmarked | 14m 51s | ok |
| lose one database instance + 10% of the fleet | 16m 35s | **over** |
| every rate 15% below bench | 17m 29s | **over** |
| both at once | 19m 31s | **over** |

`--derive` prints this shape specifically so it can be rejected. Sizing to the nominal
number alone is how a capacity plan that is arithmetically correct becomes operationally
useless.

---

## 5. Step 4 — the envelope

Five conditions, not one. Three are deadlines; two are structural.

**Deadlines**, all against 900 s:

- **Nominal** — every rate exactly as benchmarked.
- **Pessimistic** — every benchmarked rate 15% below bench. This is *not a failure
  scenario*. It is the possibility that the model is simply optimistic, which is a state
  of the world rather than an event: it does not clear up, and no retry escapes it.
- **Pessimistic + failure** — that same world, minus one database instance and 10% of the
  fleet. Failures happen in the pessimistic world too, so this conjunction is what an SLO
  actually has to survive.

**Structural:**

- **Survives shard loss** — still compute-bound, with ≥10% ingest headroom, at `S−1`. The
  claim "the fleet never waits on the database" is not being made only for healthy
  clusters. Without this condition the deadlines alone happily select an 8-shard shape
  whose cluster is 1% from becoming the bottleneck the moment anything hiccups.
- **Saturated** — the fleet offers each shard the full 12 streams B5 says it sustains.
  Provisioning nine instances and then feeding each of them ten streams out of twelve is
  paying for ingest capacity with no way to use it.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/stress_envelope_dark.svg">
  <img src="assets/stress_envelope_light.svg" alt="Four stress conditions for 40 workers over 6 shards and for 54 over 9. The cheapest shape fails three of the four; the deployed shape passes all four." width="880">
</picture>
</div>

---

## 6. Step 5 — the search, and the result

Exhaustive over integer `(W, S)`. The continuous optimum — Lagrange on `8W + 16S` subject
to `A/W + B/S = C` — gives `W/S = 7.7` and is useful for intuition, but `streams_per_shard`
steps at `W/S = 6` and the answer must be integral, so the true frontier is a staircase
and search is both simpler and exact.

**4,981 shapes meet the three deadlines. Only 448 meet the whole envelope.**

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/feasibility_map_dark.svg">
  <img src="assets/feasibility_map_light.svg" alt="Grid of workers against shards. Three nested regions: shapes that miss 15 minutes, shapes that meet it nominally, and the much smaller set that survives the stress envelope, running along the diagonal W = 6S." width="900">
</picture>
</div>

The cheapest survivors, and why each loser lost:

| vCPU | W | S | W/S | streams | nominal | −15% | −15%+fail | why not |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 580 | 53 | 9 | 5.9 | 10 | 11m 18s | 13m 18s | 14m 48s | uses 10 of 12 streams |
| 580 | 55 | 8 | 6.9 | 12 | 11m 12s | 13m 11s | 14m 40s | S−1 headroom +3% |
| 588 | 50 | 11 | 4.5 | 8 | 11m 30s | 13m 32s | 14m 51s | uses 8 of 12 streams |
| 588 | 52 | 10 | 5.2 | 10 | 11m 18s | 13m 17s | 14m 48s | uses 10 of 12 streams |
| **588** | **54** | **9** | **6.0** | **12** | **11m 09s** | **13m 07s** | **14m 34s** | **chosen** |
| 588 | 56 | 8 | 7.0 | 12 | 11m 04s | 13m 01s | 14m 27s | S−1 headroom +1% |

**The structural conditions decide it, not cost.** The cost band is flat to within a few
percent — a dozen shapes sit inside 3% of each other — so the deadlines alone leave nothing
to choose between. Requiring the cluster to survive an instance loss eliminates every
8-shard shape. Requiring it to be saturated eliminates 53/9, which buys nine instances and
feeds each of them ten streams out of twelve.

### The result

| | |
|---|---:|
| map workers | **54** × 8 vCPU / 16 GB |
| data shards | **9** × 16 vCPU / 128 GB |
| coordinator | 1 × 8 vCPU / 32 GB |
| pgbouncer | 2 × 2 vCPU / 1 GB |
| **total** | **588 vCPU / 2,050 GB** |
| wall clock | **11m 09s** — 26% under the deadline |
| under the full envelope | 14m 34s — 2.9% still in hand |
| cost | **109 vCPU-hours** |

`W = 6S` exactly, which is why the queue's admission arithmetic comes out even: 9 shards ×
6 slots = 54 workers. Every worker has a slot; every slot has a worker. That is not a
coincidence, it is the constraint from §2 showing up in
[`02_work_queue.sql`](../distributed/db/02_work_queue.sql).

---

## 7. Headline, against v1

| | v1 — one desktop, one database | v2 — 54 workers, 10 instances |
|---|---:|---:|
| dates covered | 12 | **60** |
| rows written | 1,577,374,560 | **7,886,872,800** |
| raycasts fired | 990,240,696 | 4,952,298,879 |
| **wall clock** | **6 h 00 min** | **11 min 09 s** |
| row rate | 73,027 / s | 15,970,917 / s |
| per worker | 73,027 / s | 295,758 / s |
| sample storage | 110 GB (2 inline indexes) | 499 GB (no index) |
| WAL for the sample load | ~110 GB | **~0** |
| failure recovery | restart the export | per-task, automatic |
| infrastructure | one desktop | 588 vCPU / 2,050 GB |
| cost | 6 desktop-hours | **~109 vCPU-hours** |

**161.5× end to end, work-normalised.** v2 covers 60 dates against v1's 12, so a bare
wall-clock ratio would compare different amounts of work and flatter v2 by 5×. At v1's
measured rate the same 60 dates would take 30.0 h; v2 does them in 11m 09s.

v2 is faster hardware and better I/O discipline applied to the *same computation per row* —
not a cheaper computation, and not a smaller one.

### Against the throughput ceiling

```
                        54 workers
                      ×  3.00  RaycastCommand batching across job threads
                      ×  1.35  section-coherent BVH working set
                      ───────
                        218.7×  raw throughput ceiling

                        161.5×  achieved end to end
                      ───────
                          74%   efficiency against the ceiling
```

The missing 26% is not waste. It is the two phases that do not parallelise with the fleet:

| phase | time | share | scales with |
|---|---:|---:|---|
| fleet spin-up | 45 s | 7% | nothing — a fixed cost |
| map | 8m 14s | 74% | worker count |
| reduce | 2m 10s | 19% | shard count |
| **total** | **11m 09s** | | |

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/phase_breakdown_dark.svg">
  <img src="assets/phase_breakdown_light.svg" alt="Breakdown of the 11 minute 9 second run: 45 seconds spin-up, 8 minutes 14 seconds map with writing fully overlapped, 2 minutes 10 seconds reduce" width="850">
</picture>
</div>

### The map phase costs `max`, not `sum`

```
raycast   8m 14s   ████████████████████████████
write     6m 05s   ████████████████████         ← overlapped; writer idle 26% of the phase
          ───────
MAP       8m 14s   compute-bound
```

Two distinct quantities that are easy to conflate: the **writer is idle 26% of the map
phase** (6m 05s of work inside an 8m 14s window), and the **cluster has +35% spare ingest
capacity** (21.6M rows/s against a 15.97M demand). The first is time; the second is rate.

### Reduce is 2m 10s because of a schema decision, not hardware

Sections own **whole edges**, so `GROUP BY (edge_id, datetime)` completes inside one
instance. Nine shards each aggregate their own ninth of the dataset in parallel — no
shuffle, no barrier, no coordinator gathering partial sums.

```
per shard:  876M rows aggregated          73.0 s
            16.1M rows indexed            26.8 s
            ANALYZE (vacuumdb --jobs 8)   30.0 s
                                         ──────
                                          129.8 s
```

Had sections been defined by sample-point position, ~12% of edges would straddle a boundary
and this would have needed a distributed sum — and every routing query would have become a
cross-shard join for the life of the dataset.

---

## 8. What the database cluster is worth

Workers held fixed at 54. Only the number of PostgreSQL instances varies, so the curve
isolates the database's contribution from the fleet's.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/shard_scaling_dark.svg">
  <img src="assets/shard_scaling_light.svg" alt="Wall clock against shard count at fixed 54 workers: one instance takes 1 hour 11 minutes, nine take 11 minutes 9 seconds" width="850">
</picture>
</div>

| shards | ingest | map | reduce | total | vs v1 | bound by |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 2.4M/s | 54m 46s | 15m 28s | **1.18 h** | 25.4× | I/O |
| 2 | 4.8M/s | 27m 23s | 7m 59s | 36m 07s | 49.8× | I/O |
| 4 | 9.6M/s | 13m 42s | 4m 15s | 18m 41s | 96.3× | I/O |
| 6 | 14.4M/s | 9m 08s | 3m 00s | 12m 52s | 139.8× | I/O |
| 8 | 19.2M/s | 8m 14s | 2m 22s | 11m 21s | 158.6× | compute |
| **9** | **21.6M/s** | **8m 14s** | **2m 10s** | **11m 09s** | **161.5×** | **compute** |
| 10 | 20.0M/s | 8m 14s | 2m 00s | 10m 59s | 164.0× | compute |
| 14 | 16.8M/s | 8m 14s | 1m 34s | 10m 33s | 170.6× | compute |
| 20 | 16.0M/s | 8m 14s | 1m 15s | 10m 14s | 176.0× | compute |
| 30 | 12.0M/s | 10m 57s | 60 s | 12m 42s | 141.7× | I/O again |

**The cluster is worth 6.4× of the 161× total.** The same 54 workers against one instance
reach 25.4×; against nine, 161×. Adding workers alone would have bought almost none of it,
which is the central finding of the rewrite.

**Three shard counts are interesting for different reasons.**

*Eight* is where the pipeline crosses from I/O-bound to compute-bound — the ingest rate
finally exceeds what the fleet produces. It is also the shape the envelope rejects, because
at `S−1` it has 1% headroom.

*Nine* is deployed. Seven is the bare minimum; nine gives **+35% ingest headroom**, keeps
all 12 streams per instance in use, and stays compute-bound after losing one instance
outright.

*Thirty is worse than nine.* 54 workers over 30 shards is fewer than two workers per shard,
offering four `COPY` streams where the instance could run twelve. Each instance starves and
the *map* phase goes back to being I/O-bound. Note the ingest column peaks at nine and then
**falls** — more hardware is not monotonically better, and the model says where the turn is.

---

## 9. Why "add more nodes" stops working

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/worker_ceiling_dark.svg">
  <img src="assets/worker_ceiling_light.svg" alt="Wall clock against fleet size for 4, 6, 9 and 14 shards. Every curve flattens at its own database ingest ceiling; the four-shard curve flattens above the 15-minute deadline and never crosses it." width="880">
</picture>
</div>

Every curve flattens, and where it flattens has nothing to do with the fleet. **Four shards
cannot meet the deadline with any fleet at all** — that curve levels off at 18m 41s and
stays there; two shards levels off at 36m 07s. Past the ceiling each additional pod
raycasts into a queue.

Sizing the cluster to each fleet instead:

| workers | min shards | map | reduce | total | vs v1 | efficiency vs ceiling |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 7.41 h | 15m 28s | 7.68 h | 3.9× | 96% |
| 5 | 1 | 1.48 h | 15m 28s | 1.75 h | 17.1× | 85% |
| 10 | 2 | 44m 27s | 7m 59s | 53m 11s | 33.8× | 84% |
| 25 | 4 | 17m 47s | 4m 15s | 22m 46s | 79.0× | 78% |
| **54** | **7** | **8m 14s** | **2m 38s** | **11m 37s** | **154.9×** | **71%** |
| 100 | 13 | 4m 27s | 1m 39s | 6m 51s | 262.9× | 65% |
| 200 | 25 | 2m 13s | 1m 06s | 4m 04s | 442.1× | 55% |

Efficiency falls because the 75-second floor is fixed: at 200 workers it is 31% of the run.
The deployment runs **nine** shards against the seven this table calls the minimum, which is
why the deployed figure (11m 09s) beats this table's 54-worker row (11m 37s).

**There is no knee.** The cost/time frontier decays as `W⁻²` — smoothly, with no inflection
anywhere — so "diminishing returns" cannot select a point on it, and did not. The deadline
did.

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/cost_time_dark.svg">
  <img src="assets/cost_time_light.svg" alt="Cost against time Pareto frontier. Marginal return falls from 168 seconds per 100 vCPU at 470 vCPU to 31 seconds at 1140, with no inflection point, asymptoting at the 75-second floor." width="880">
</picture>
</div>

Marginal return halves roughly every +200 vCPU: **168 s** per extra 100 vCPU at 470 vCPU,
**58 s** at 780, **31 s** at 1,140. Past ~1,060 vCPU an extra 100 buys less than the 30 s
`ANALYZE` floor itself — the point at which more hardware stops being an engineering answer
and becomes a rounding error.

---

## 10. Storage

| | rows | on disk | notes |
|---|---:|---:|---|
| `meo_exposure_samples` (v1, 12 dates) | 1,577,374,560 | 110 GB | two indexes maintained inline |
| `meo_exposure_samples_p` (v2, 60 dates) | 7,886,872,800 | **499 GB** | **no index** — pruning replaces it |
| `meo_exposure_edges_p` | 144,720,000 | 9.2 GB heap + 6.5 GB index | derived in the reduce phase |
| per shard | 876,319,200 | ~56 GB | fits the 128 GB page cache many times over |
| static geometry | — | ~140 MB | replicated to every shard |
| **cluster total** | | **515 GB** | |

**Rows are not raycasts.** 7,886,872,800 rows are stored; 4,952,298,879 raycasts are fired.
The horizon guard resolves the other 37.2% without touching the BVH, but they are still
written, because downstream needs a value at every timestep rather than a gap. Compute
scales with daylight; storage scales with the full cross product.

Row width, derived rather than guessed, because it is what turns a row rate into a byte
rate:

```
 24 B  tuple header
 16 B  sample_point_id  uuid
  8 B  datetime         timestamp
  1 B  is_sunlit        boolean
  4 B  section_id       int4      partition routing + provenance
  8 B  task_id          int8      makes a task's output removable
 ────
 64 B  MAXALIGN'd  +  4 B line pointer  =  68 B per row on the page
```

v2 stores five times v1's rows in under five times the space despite two extra columns,
because it builds no index on them. Sizes are binary GB, matching `pg_size_pretty`.

**WAL for the sample load: ~0.** Every task creates its own partition leaf and `COPY`s into
it in the same transaction, which PostgreSQL skips WAL for entirely under
`wal_level = minimal`. The ~499 GB is written without a WAL record. See
[OPTIMIZATION.md §9](OPTIMIZATION.md#9-500-gb-of-wal-skipped-entirely).

**The 499 GB is a consequence of the encoding, not of the physics.** A packed
representation measures 48× smaller and is provably lossless; it is not adopted because the
v1 column set is a hard requirement. That trade is priced explicitly in
[DB_CLUSTER.md](DB_CLUSTER.md#what-one-row-is-precisely).

---

## 11. Sectioning and task granularity

| | |
|---|---:|
| sections (non-empty 1 km tiles) | 84 |
| time windows per day | 6 × 3 h |
| tasks | **30,240** (84 × 60 × 6) |
| rows per task (mean) | 260,809 |
| tasks per worker | 560 |
| leaf partitions per shard | 3,360 (~17 MB each) |
| distinct collider working sets | 504 (84 × 6) |
| shadow halo | 2,286 m (200 m / tan 5°) — an exact bound |
| BVH working set | 9.0 km² against 31.0 km² for an omnidirectional halo |

**Tail imbalance is negligible.** 560 tasks per worker at ~0.9 s each means the final round
costs under 1.5 s even for the most expensive all-daylight task, against a 494-second map
phase — under 0.3%. Coarser tasks would leave workers idle at the end; finer ones would
spend more time claiming than computing.

**Cost spread: 780×.** Cost is estimated per *window*, not per day: a 03:00–06:00 window in
December sits entirely below the horizon guard and costs ~1 timestep, while the same window
in June is most of a sunrise. Estimating per day would have made all six of a date's windows
look identical and thrown away nearly all the ordering LPT exploits.

**Affinity turns 30,240 working-set loads into 504.** Every task in a (section, window)
group shares its geometry and BVH pages, and there are 60 dates per group. The coordinator
dispatches a matching task when one is admissible, so the warm set is reused. `monitor.py`
reports the hit rate; it should sit near 92%.

---

## 12. Read performance

| query | path | cost |
|---|---|---|
| edge cost at a timestamp | shard PK on `(edge_id, datetime)` | index lookup |
| **directional cost for a traverse** | one leaf, ~261k rows, pruned | one sequential scan |
| per-sample series for one edge | same leaf | same scan |
| whole-network snapshot | federated, partitionwise pushdown | 9 concurrent foreign scans, one row each |
| route of 12 edges | 1 shard, 85% of the time | 1–2 round trips |

The directional query is the one the schema exists for, and it is cheap because pruning
reaches exactly one leaf:

```
EXPLAIN SELECT count(*) FROM meo_exposure_samples_p
 WHERE section_id = 0 AND datetime = '2026-06-15 15:00:00';

 Aggregate
   ->  Seq Scan on meo_exp_s0_20260615_w4      ← one relation out of 3,360
```

**Route locality: 85% of sampled 12-edge routes touch a single shard**, 15% touch two, none
touch more. That is what the Hilbert-ordered contiguous assignment buys — a hash of section
ids would put most routes across five or more instances. Contiguity measures **0.66** at
nine shards (`cluster.py --show --shards 9`) against ~0.11 for a hash, and it *rises* as
the shard count falls, since fewer, longer runs mean fewer boundaries: the read path is a
reason not to over-shard, independent of cost. Measure it on
your own topology with `SELECT * FROM meo_route_locality();`.

---

## 13. What the model omits, and which way it errs

Stated because a model that only lists the effects flattering it is not a model. This is
also why the envelope's pessimistic condition exists: the point of §5 is that the sizing
holds even if this section's optimistic entries all bite at once.

**Pushing the real number up (the model is optimistic):**

- **Cold image pull.** B8 assumes a warm image cache. A genuinely cold 54-node pull of a
  400 MB image adds 30–60 s depending on registry bandwidth, nearly doubling the spin-up
  term.
- **Shard imbalance.** Modelled as zero; measured at 1.06× on the reference topology, so the
  slowest instance takes ~6% longer than the mean. `plan_tasks.py` refuses above 1.25×.
- **Straggler tasks.** LPT bounds makespan at 4/3 of optimal in the worst case; the model
  assumes perfect packing.

**Pushing it down (the model is pessimistic):**

- **B4's 200k rows/s** is conservative for a 68-byte binary row into a WAL-skipped relation.
- **B9's 30 s `ANALYZE`** assumes `vacuumdb --jobs 8`; a shard with its data in page cache
  does it faster.
- **B3's 1.35×** is measured against a city-wide sweep, not against the theoretical best
  locality a smaller section would give.

Net, the errors are of similar magnitude and opposite sign, and the honest statement is that
11m 09s is the right order of magnitude rather than a stopwatch reading. **The deployment is
sized so that being wrong by 15% on every one of them at once still meets the deadline.**

`reduce_finalize.py` prints the achieved figures against the modelled ones at the end of
every run, so the first thing a real deployment does is replace these with its own:

```
  map wall clock  : 0:08:13  (14:17:00 -> 14:25:13)
  reduce          : 0:02:09  (9 shards in parallel)
  observed        : 0:10:23  (first claim -> finalised; excludes spin-up)
  + spin-up       : 0:00:45  (modelled; the queue cannot see it)
  end-to-end      : 0:11:08  (model predicts 11m 08.7s)
  vs the deadline : 15 min target, +25.7% margin
  distinct workers: 54
  throughput      : 16M rows/s (57B/hour)
  per worker      : 296K rows/s  (v1 single-thread baseline 73K/s, model 296K/s)
  raycast rate    : 10M/s  (62.8% of rows touched the BVH)
  vs v1           : 161.5x work-normalised  (v1 would need 30.00 h for these 7,886,872,800 rows; model predicts 161.5x)
```

Note what that report separates, because all three are easy to conflate: the queue's wall
clock **excludes** spin-up (it cannot see a pod that has not claimed anything yet), rows
and raycasts differ by 37.2%, and the speedup is **work-normalised** against what v1 would
have needed for *these* rows rather than against its own 12-date run.

---

## 14. Reproducing these numbers

```bash
# THE SIZING ARGUMENT — §1 through §6 of this document
python distributed/orchestrator/model.py --derive

# the measurement ladder, with each benchmark's method
python distributed/orchestrator/model.py --bench

# the cost/time Pareto frontier
python distributed/orchestrator/model.py --frontier

# the whole model, plus both sensitivity sweeps
python distributed/orchestrator/model.py

# what actually ends up in the database, table by table
python distributed/orchestrator/model.py --db

# re-derive for a different deadline or a different city
python distributed/orchestrator/model.py --sweep
python distributed/orchestrator/model.py --workers-sweep
python distributed/orchestrator/model.py --balance --workers 100

# the topology's balance and locality, no database needed
python distributed/orchestrator/cluster.py --show --shards 9

# regenerate every figure in these docs
python distributed/orchestrator/make_impact_figures.py docs/assets
```

And to confirm the schema behaves as described, against a real PostgreSQL:

```bash
distributed/db/tests/run_selftest.sh        # 45 assertions
```
