# Low-level optimisation

Where the per-worker 4.05× comes from, and where the ~500 GB of WAL went. Each entry
states what it costs, what it buys, and how to tell whether it is working.

The cluster-level decisions are in [DB_CLUSTER.md](DB_CLUSTER.md); the two multiply.

---

## Summary

| # | change | effect | verify with |
|---:|---|---|---|
| 1 | `RaycastCommand.ScheduleBatch` instead of `Physics.Raycast` | **3.0×** raycast rate | log line: `k/s` per task |
| 2 | section-coherent BVH working set | **1.35×** raycast rate | `perf stat -e LLC-load-misses` |
| 3 | flush on a background thread, 2 connections | map phase = `max(ray, write)` not their sum | `26% idle` in the phase figure |
| 4 | binary `COPY` instead of CSV | ~170 GB less on the wire, no per-row string | `pg_stat_statements` bytes |
| 5 | allocation-free steady state | no GC pause in the raycast loop | `AssertNoGarbageCollected()` |
| 6 | results as a bitset | 33 KB per window instead of 264 KB | — |
| 7 | `colliderInstanceID` instead of `.collider` | 4.95e9 fewer managed lookups | — |
| 8 | struct-of-arrays sample layout | 5 rays per cache line | — |
| 9 | create-then-attach + `wal_level=minimal` | **~500 GB of WAL → ~0** | `pg_current_wal_lsn()` |
| 10 | `COPY ... FREEZE` | no hint-bit writes, no freeze-vacuum ever | `pg_visibility` |
| 11 | no index on 7.89e9 rows | ~300 GB and all load-time maintenance | `\d+ meo_exposure_samples_p` |
| 12 | `fillfactor = 100` on append-only leaves | ~50 GB, 10% fewer pages per scan | `pg_class.reloptions` |
| 13 | `fillfactor = 70` on the work queue | heartbeats become HOT, no index write | `pg_stat_user_tables.n_tup_hot_upd` |
| 14 | step-major row order | heap clustered on `datetime` for free | `pg_stats.correlation` |

---

# Worker side

## 1. Batched raycasts — 3.0×

v1 called `Physics.Raycast` once per sample, on Unity's main thread — 990 million times
for its 12 dates, once for each timestep the horizon guard did not already resolve.
`Physics.Raycast` **must** run on the main thread, so that loop used one core no matter
how many the machine had. A 16-core desktop ran it exactly as fast as a 4-core one.

```csharp
for (int i = 0; i < _sampleCount; i++)
    _commands[i] = new RaycastCommand(_origins[i], toSun, queryParams, maxDistance);

JobHandle handle = RaycastCommand.ScheduleBatch(
    _commands.GetSubArray(0, _sampleCount),
    _hits.GetSubArray(0, _sampleCount),
    minCommandsPerJob: 64,
    maxHits: 1);
handle.Complete();
```

**3.0× on 8 vCPU, not 8×**, and both reasons are ceilings rather than tuning
opportunities: the main thread still builds the command array and folds the results, and
BVH traversal saturates memory bandwidth before it saturates ALUs.

Three parameters matter:

- **`maxHits: 1`.** We only need to know *whether* anything blocks the sun, not what or
  how many. More hits would make the results array strided and cost proportionally more
  traversal.
- **`minCommandsPerJob: 64`.** Small enough that ~4,400 rays spread across every worker
  thread, large enough that per-job scheduling overhead stays negligible. At the default
  of 1 the job system creates thousands of jobs per timestep and spends longer
  scheduling than raycasting.
- **`QueryParameters(hitBackfaces: false, hitTriggers: Ignore)`.** Backfaces off because
  a building's interior faces point away from us and counting them would make a ray
  starting inside geometry report a hit at zero distance. Triggers explicitly ignored
  rather than `UseGlobal`, so whether a trigger blocks sunlight does not depend on a
  project-wide setting someone might change for unrelated reasons.

**Consequence for the deployment:** Unity sizes the job worker pool from the *visible*
core count, which inside a container is the cgroup quota. A fractional CPU request like
`3500m` collapses the pool and silently forfeits most of this — no error, no symptom
beyond being slower than the model says. Hence `cpu: "8"`, whole.

## 2. Section-coherent BVH traversal — 1.35×

Every ray in a task starts inside the same square kilometre, so the colliders they can
reach are bounded by section + the 2,286 m shadow halo — about 9 km² against the city's
59. The BVH is whole-city (built once at boot), but the *working set* is that fraction,
and it stays resident in L3 and the page cache where v1's city-wide sweep missed
constantly.

Reinforced by the load order:

```sql
ORDER BY sp.edge_id, sp.sequence_index
```

Consecutive array entries are consecutive points along the same street, so consecutive
rays in a batch start within 2 m of each other and traverse the same BVH nodes. Issuing
them in the database's physical order instead would scatter each batch across the whole
section — same total work, several times the memory traffic.

This is also the honest answer to the old objection that spatial sharding "saves no
memory": correct, it does not. It buys **locality**, not footprint.

## 3. The flush overlaps the next task's raycasting

Raycasting a window takes ~0.88 s. Writing its 261k rows takes ~1.30 s down a *single*
`COPY` stream — but each worker alternates between **two**, so the amortised write cost is
**~0.65 s** per task. (Both numbers are real and neither is a typo: 1.30 s is the latency a
`WROTE` log line reports, 0.65 s is the throughput the capacity model uses.) In sequence
that is ~1.53 s per task and the fleet spends **42% of its life on sockets**.

So a finished window is handed to a writer thread on a **second connection**, and the
main thread immediately claims the next task:

```
main    │ ray(N) ──────────│ ray(N+1) ─────────│ ray(N+2) ────────
writer  │        │ COPY(N) ─────────│ COPY(N+1) ────────│
```

This is why the capacity model costs the map phase as `max(raycast, write)` rather than
their sum, and why each worker holds exactly two COPY streams.

**Strictly one flush in flight.** With write time below raycast time the writer is never
the constraint, so a second queued window would only ever mean the shard has stalled —
and then applying backpressure is the correct response, not buffering deeper. `Enqueue`
blocks in that case, which is the signal.

**One payload buffer, not two.** Copy-on-enqueue already provides the second buffer: the
sampler's own bitset is free to be overwritten the instant `Enqueue` returns, because its
contents are already in the payload. The copy is 33 KB of memmove against the 1.3 s of
flush it decouples.

**The WAL-skip survives this**, which is not obvious. PostgreSQL only skips WAL for a
`COPY` into a relation created in the *same transaction* — and that whole transaction
(begin, create, copy, attach, commit) happens on the writer thread's own connection.
Two threads, two connections, no shared transaction. Npgsql connections are not
thread-safe and this design never shares one.

**Ordering consequence.** A task is completed on the coordinator only *after* its rows
commit, so a task is marked done one loop iteration after it is computed. Marking it
earlier would let a crash in between leave a task recorded as complete with no data —
which the completeness check would pass.

## 4. Binary COPY

v1 built a CSV line per row: an interpolated string with a 36-character UUID, a
19-character timestamp and `"true"`/`"false"`. ~52 bytes on the wire, one heap-allocated
string per row, and a text parse on the server for every field.

```csharp
writer.StartRow();
writer.Write(p.Ids[i],    NpgsqlDbType.Uuid);        // 16 raw bytes
writer.Write(ts,          NpgsqlDbType.Timestamp);   // int64 microseconds
writer.Write(sunlit,      NpgsqlDbType.Boolean);     // 1 byte
writer.Write(p.SectionId, NpgsqlDbType.Integer);
writer.Write(p.TaskId,    NpgsqlDbType.Bigint);
```

~30 bytes on the wire, no client allocation, no server-side parse. At 7.89 billion rows
that is **~170 GB less network traffic** and ~7.89 billion strings never created.

> `writer.Complete()` is mandatory. Without it, the importer's `Dispose` treats the
> import as cancelled and the rows are **silently discarded**. It is the classic binary-
> COPY bug and it presents as an empty leaf with no error anywhere.

## 5. Allocation-free steady state

Every buffer is allocated **once at worker startup** with `Allocator.Persistent`, sized
to the largest permitted section, and reused for every task for the pod's whole life. A
task uses a *prefix* rather than a right-sized allocation.

```csharp
_commands = new NativeArray<RaycastCommand>(_capacity, Allocator.Persistent,
                                           NativeArrayOptions.UninitializedMemory);
_hits     = new NativeArray<RaycastHit>(_capacity, Allocator.Persistent, …);
_origins  = new NativeArray<Vector3>(_capacity, Allocator.Persistent, …);
_sampleIds = new Guid[_capacity];
_bits      = new ulong[BitWords(_capacity, _maxSteps)];
```

At `SUNLIT_MAX_SECTION_SAMPLES = 16384` that is ~2.6 MB, for the life of the pod. A
pod's RSS is therefore **constant in run length**, not merely bounded.

`Allocator.Persistent`, not `TempJob`: `TempJob` is asserted to live at most four frames
and Unity logs a leak warning past that. These outlive thousands of frames by design.

**What matters more than the allocation cost is that the GC never runs during
raycasting.** A generation-0 collection mid-window would stall the job system's worker
threads together, and the resulting pause shows up as a heartbeat gap on a 900 s lease —
so the symptom is "unexplained lease loss", not "memory pressure".

v1 allocated, per timestep, a `List<Tuple<Guid, string, bool>>` plus an interpolated
string per sample: for v1's 12 dates that was ~1.58 billion strings and ~4.7 billion
heap objects; at v2's 60 it would have been five times more, all
garbage.

The claim is made **checkable rather than aspirational**:

```csharp
public int AssertNoGarbageCollected()
{
    int collections = GC.CollectionCount(0) - _gcBaseline;
    if (collections > 0)
        Debug.LogWarning($"[Sampler] {collections} gen-0 collection(s) during a window. …");
    return collections;
}
```

Supporting settings: server GC and concurrent GC off via `DOTNET_gcServer=1` /
`DOTNET_gcConcurrent=0` (they must precede runtime start, hence env not code);
`GCSettings.LatencyMode = SustainedLowLatency`; and `PlayerSettings.gcIncremental =
false` in the build script, because incremental GC adds a write barrier to every
reference store in exchange for smoother frame times this worker does not need.

## 6. Results as a bitset

`is_sunlit` is one bit of information, so it is stored as one bit:

```csharp
int bit = stepIndex * _sampleCount + sampleIndex;
_bits[bit >> 6] |= 1UL << (bit & 63);
```

A whole window is `samples × steps / 8` bytes — **33 KB** for the Manhattan case against
264 KB as bytes. Small enough that the writer can hold a copy and flush it while the
sampler refills, which is what makes §3 possible at all.

**Step-major layout**, `bit(step × sampleCount + sample)`, for two reasons: accumulation
writes it sequentially, one contiguous run per timestep; and the writer then emits rows
grouped by `datetime`, which is the leaf's range key — so the heap ends up physically
clustered on the column queries filter by, for free, with no `CLUSTER` pass (§14).

The whole-window total is a popcount over ~4,100 words instead of 264,000 bit tests.
`System.Numerics.BitOperations.PopCount` would be one instruction but is .NET Core 3.0+;
IL2CPP targets netstandard2.1, so the SWAR fallback is used.

## 7. `colliderInstanceID`, not `.collider`

```csharp
bool sunlit = _hits[i].colliderInstanceID == 0;     // no hit
```

The obvious `_hits[i].collider == null` costs an instance-id → managed-object lookup per
sample — 4,400 dictionary probes per timestep, **4.95 billion over a run** — purely to
compare the result against null. Reading the raw id skips all of it.

## 8. Struct-of-arrays

```csharp
public readonly Vector3[] Positions;   // the raycast loop reads these 60x per task
public readonly Guid[]    Ids;         // the raycast loop never reads these
```

Split, so a cache line of `Positions` holds five consecutive rays' worth of useful data
and nothing else. As an array of class instances it would be 4,400 pointer dereferences
per timestep into scattered heap locations, with the ids — which the hot loop never
touches — sharing cache lines with the positions it does.

Ray origins are also computed **once per task** rather than per timestep: only the
direction changes across a window.

---

# Database side

## 9. ~500 GB of WAL, skipped entirely

The headline database result. PostgreSQL skips WAL for a `COPY` into a relation created
in the **same transaction**, under `wal_level = minimal`. Because every task builds its
own partition leaf from scratch rather than appending to a pre-existing partition, the
whole sample dataset qualifies:

```sql
BEGIN;
  SELECT meo_begin_leaf(section, lo, hi, window, task);   -- CREATE TABLE
  COPY <leaf> (…) FROM STDIN (FORMAT BINARY, FREEZE);     -- not WAL-logged
  SELECT meo_attach_leaf(section, lo, hi, window);        -- ATTACH PARTITION
COMMIT;
```

Not *reduced*. **Skipped.** Verify rather than trust:

```sql
SELECT pg_current_wal_lsn();   -- before one task
-- run one task
SELECT pg_current_wal_lsn();   -- after
```

The delta should be kilobytes — the catalog entries for `CREATE` and `ATTACH` — against
the ~18 MB the task actually wrote.

This is also *why* the design is create-then-attach rather than the more obvious
`COPY` into a shared partition. The partition shape was chosen to make this optimisation
available, not the reverse.

**Cost:** no replication, no PITR, and no valid base backup during the load. Legitimate
only because every byte is reproducible in ~3 minutes — see
[TUNING.md](TUNING.md#the-safety-argument-stated-plainly).

## 10. `COPY ... FREEZE`

Same precondition as §9, and it removes two later costs:

- **No hint-bit writes on first read.** Ordinarily the first reader of a tuple sets its
  visibility hint bits, which dirties the page — so the first query after a bulk load
  rewrites the whole table. With `FREEZE` the tuples are already visible to everyone.
- **No freeze-vacuum, ever.** 7.89 billion rows would otherwise eventually need an
  anti-wraparound vacuum to freeze them.

It is also what makes `autovacuum_enabled = off` on the leaves *safe*. Normally
disabling autovacuum on a large table risks transaction-id wraparound; here there is
nothing to freeze. (Anti-wraparound vacuum still runs regardless — PostgreSQL does not
let you opt out — but it finds nothing to rewrite.)

## 11. No index on 7.89 billion rows

```sql
CREATE TABLE meo_exposure_samples_p (…) PARTITION BY LIST (section_id);
-- no PRIMARY KEY, no index. Deliberately.
```

An index maintained during the load would cost a B-tree descent and possibly a page
split per `COPY`'d row, turn the upper levels into a contention point, and occupy ~300 GB
— more than half the data it indexes.

And it would answer no question that pruning does not answer better. The only lookup is
"this edge's samples at this timestamp", and pruning reaches one ~261k-row leaf that is
cheaper to scan sequentially than to descend a B-tree over 7.89e9 entries for.

```
EXPLAIN SELECT count(*) FROM meo_exposure_samples_p
 WHERE section_id = 0 AND datetime = '2026-06-15 15:00:00';

 Aggregate
   ->  Seq Scan on meo_exp_s0_20260615_w4      ← one relation, out of 576
```

Asserted in the self-test, including that the relation scanned is the specific leaf
rather than the parent with a filter. **Pruning is the index.**

Uniqueness is structural rather than enforced: a (sample point, timestamp) pair belongs
to exactly one (section, date, window), so exactly one leaf, and a leaf is rebuilt whole
rather than appended to.

Indexes on the *derived* edge table are built after the load, where they cost one large
sequential sort bounded by `maintenance_work_mem` instead of a billion random descents —
and over 16.1M rows per shard rather than 876M.

## 12 & 13. `fillfactor`, both directions

The same parameter, opposite values, for opposite reasons.

**Append-only exposure leaves: `fillfactor = 100`.** The default 90 reserves a tenth of
every page for future HOT updates that will never come — these are written once by
`COPY` and never updated. On 500 GB that is ~50 GB wasted and 10% more pages on every
sequential scan, which is their only access pattern.

**The work queue: `fillfactor = 70`.** `meo_tasks` is a few thousand rows taking ~42
`UPDATE`s each (one claim, ~40 heartbeats, one completion) — ~250,000 row versions over
a run on a table of 6,000. Leaving room on the page lets a heartbeat rewrite the row as
a **HOT update, in place, without touching any index**. Heartbeats are ~95% of the
writes to this table, so making them index-free is most of the win.

```sql
SELECT n_tup_upd, n_tup_hot_upd,
       round(100.0*n_tup_hot_upd/nullif(n_tup_upd,0),1) AS pct_hot
FROM pg_stat_user_tables WHERE relname = 'meo_tasks';
```

Want `pct_hot` above ~90. Below that, either the fillfactor did not apply or autovacuum
is not keeping up.

Autovacuum thresholds on `meo_tasks` are **absolute** rather than scale factors: a scale
factor of 0.2 on a 6,000-row table means 1,200 dead rows before vacuum, which at 42
versions per task is only 28 tasks of churn.

## 14. Step-major row order, and free clustering

Because the bitset is step-major, the writer emits rows grouped by `datetime` — the
leaf's range key. The heap therefore arrives physically ordered on the column queries
filter by, at no cost and with no `CLUSTER` pass.

```sql
SELECT attname, correlation FROM pg_stats
 WHERE tablename = 'meo_exp_s384_20260615_w4' AND attname = 'datetime';
```

Should be ~1.0.

---

## What was considered and rejected

**Burst-compiled jobs for the fold loop.** The fold is ~4,400 bit sets per timestep,
already memory-bound and a small fraction of traversal time. Burst would add a package
dependency and a compilation step to the build for a few percent of a few percent.

**GPU raycasting.** Compute-shader BVH traversal would be faster per ray, but it needs
the mesh uploaded, a GPU in every pod, and a graphics-capable image — undoing the Server
subtarget that makes the image 400 MB. The CPU path is already ahead of what the
database can absorb, which is the actual constraint.

**`GC.TryStartNoGCRegion`.** Would guarantee no collection during a window, but it
throws if the budget cannot be satisfied and requires guessing an allocation ceiling.
Being allocation-free makes it unnecessary; `AssertNoGarbageCollected` catches a
regression more cheaply.

**A deeper flush pipeline.** See §3: with write time below raycast time, depth beyond one
only masks a stalled shard, where backpressure is the correct response.

**`full_page_writes = off`.** Real WAL and CPU savings, but the protection it removes is
against a torn page — a *corrupt relation*, not lost rows. Safe only on copy-on-write
storage or with guaranteed atomic 8 KB writes, and `pg_tune.py` cannot detect either. It
requires `--unsafe-torn-pages`. See [TUNING.md](TUNING.md#full_page_writes--off).

**Per-shard credentials.** More rigorous, and buys nothing: every instance holds a slice
of the same dataset with the same sensitivity, while it would make the FDW user mappings
eleven secrets to rotate in lockstep instead of one.
