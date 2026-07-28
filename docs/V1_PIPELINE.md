# v1 — the single-node pipeline

v1 is not a prototype that v2 replaced. It is the reference implementation: it
defines the schema, it produced the dataset the seasonal analysis is drawn from, and
it is still the right tool for one neighbourhood or a quick check. v2 runs the same
simulation, writing the same rows, on more hardware.

Everything here is measured on the run that produced the published dataset.

| | |
|---|---|
| Hardware | one desktop, one PostgreSQL 14 + PostGIS 3 instance |
| Wall clock | **6 h 00 min** |
| Raycasts | **1,577,374,560** |
| Rate | 73,027 / s — one thread, `Physics.Raycast` on Unity's main thread |
| Written | **110 GB** to `meo_exposure_samples`, with two indexes maintained inline |
| Peak RAM | ~250 MB, flat, whether the run covered one day or the full year |
| Scene | 4,168 waypoints · 6,700 edges · 365,133 sample points · 1,280,954 tree canopies |
| Coverage | 24 dates (1st + 15th of each month) × 03:00–21:00 × 3-minute steps |

---

## 1. What the pipeline computes, and why it is offline

Ask a routing engine for a walk across Manhattan in July and it hands you the
shortest path. It has no idea that path is in full sun for twenty minutes while a
parallel street is shaded the whole way.

Making shade a routing objective needs a per-edge, per-time-of-day exposure cost.
Computing that at query time is hopeless — it means ray-mesh intersection against
millions of building triangles while a user waits. So the entire cost of that physics
moves **offline**: precompute it exhaustively, store it, and reduce the query to
arithmetic the database can serve instantly.

Unity is used as a **geometric oracle**, not as a game engine. Nothing renders. The
only thing wanted from it is `Physics.Raycast` against a BVH built over the city's
mesh colliders, which is a mature, well-optimised CPU ray-mesh intersection routine
that would otherwise have to be written from scratch.

---

## 2. The six phases

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 0.  city mesh  (OSM buildings + terrain, ~1 GB Unity project)        │
  └──────────────────────────────────────────────────────────────────────┘
                │  RoadGraphExtractor.cs        (Editor, once)
                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 1.  road_graph.json — 4,168 vertices + 6,700 edges                   │
  └──────────────────────────────────────────────────────────────────────┘
                │  db_pipeline_initializer.py   (once)
                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 2.  meo_waypoints · meo_edges · meo_trees      in PostGIS            │
  └──────────────────────────────────────────────────────────────────────┘
                │  ShadowAwarePathFinder — "Export Sample Points"
                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 3.  meo_sample_points — 365,133 points at 2 m spacing                │
  └──────────────────────────────────────────────────────────────────────┘
                │  generate_solar_positions.py  (pvlib, once per year)
                │  process_tree_data.py         (static canopy shade)
                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 4.  sun_pos_2026.bin — 525,600 minute-resolution positions           │
  │     meo_sample_points.tree_value · meo_edges.total_tree_value        │
  └──────────────────────────────────────────────────────────────────────┘
                │  ShadowAwarePathFinder — "Export Exposure"   ◀── 6 hours
                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 5.  meo_exposure_samples — 1.58e9 rows   ◀── THE PRODUCT             │
  │     meo_exposure_edges   — 28.9e6 rows   ◀── derived convenience     │
  └──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1 — mesh to routable graph

[`RoadGraphExtractor.cs`](../Unity%20Core%20Scripts/Tools/RoadGraphExtractor.cs)

The city model has no road network in it. It has buildings and ground. The road
surface is the *absence* of buildings, which is a shape, not a graph. Turning one
into the other:

1. **Rasterise** the mesh to a 2D walkability grid at `cellSize = 0.3 m` over a
   2000 × 3000 cell extent. A cell is walkable if no collider occupies it.
2. **Dilate** once, to close single-pixel gaps where a kerb mesh nearly touches a
   building and would otherwise sever a street.
3. **BFS distance transform** — each walkable cell gets its distance to the nearest
   obstacle.
4. **Skeletonise** by keeping local maxima of that distance field. The ridge line of
   "furthest from any building" is the road centreline, which is what you want a
   pedestrian graph to follow.
5. **Lift** every skeleton pixel to a node, then simplify.

The simplification is the part that matters, and it is deliberately conservative:

> **Only degree-1 and degree-2 nodes are ever removed.** Degree-1 nodes are stubs
> (rasterisation noise). Degree-2 nodes are points along a straight run, which
> collapse into a single edge. Any node of degree ≥ 3 is a real junction and survives
> **by construction** — so intersections cannot be lost, and they end up precisely at
> road centres rather than at whatever the mesh happened to provide.

Parameters, fitted to a dense orthogonal grid: `mergeRadius = 2.5 m`,
`minEdgeLength = 1 m`, plus a 75° angle threshold for stub pruning and 165° for
colinear dissolution. These are Manhattan numbers — see
[Known limitations](#8-known-limitations).

---

## 4. Phase 2 — the schema

[`db_pipeline_initializer.py`](../Python%20&%20DB%20Scripts/Database/db_pipeline_initializer.py)

Six tables. This is the schema, and v2 does not change it.

```sql
meo_waypoints      (id, geom)                            -- 4,168 graph nodes
meo_edges          (id, start_wp_id, end_wp_id, length,
                    sample_count, total_tree_value, geom) -- 6,700 streets
meo_trees          (id, geom, shade_norm)                 -- 1,280,954 canopies
meo_sample_points  (id, edge_id, sequence_index,
                    distance_from_start, geom, tree_value) -- 365,133 @ 2 m
meo_exposure_samples (sample_point_id, datetime, is_sunlit) -- 1.58e9  THE PRODUCT
meo_exposure_edges   (edge_id, datetime, sunlit_sum)        -- 28.9e6  derived
```

Three details in `meo_sample_points` are load-bearing and easy to skim past:
`sequence_index` and `distance_from_start` make an edge's samples an **ordered
series with a direction**, and that is what
[`meo_edge_directional_cost()`](../distributed/db/03_shard_schema.sql) is built on.
Without them `meo_exposure_samples` would be an unordered bag of booleans and the
per-edge sum really would be sufficient.

### Declustering, and its cost

`road_graph.json` contains near-coincident vertices where the skeleton branched and
rejoined. The importer flood-fills groups within `CLUSTER_DIST = 15 m` and keeps the
member nearest the group centroid — a real member rather than the centroid itself, so
the node stays on the road surface.

The inner scan is `for other in vertices` inside a BFS, which makes it **O(n²)**.
That is minutes at Manhattan scale and it is a one-time cost, so it was left alone; a
grid-based spatial hash keyed on `CLUSTER_DIST` would make it near-linear and is the
fix before scaling to another city.

### Elevation, and the axis convention

Every waypoint, sample point and tree is pinned to `GLOBAL_ELEVATION = -112.0`. The
routing graph is planar: Manhattan is flat enough, one shared height keeps sample
geometry consistent, and it makes the 2D `ST_DWithin` tree join exact rather than
approximate.

> **Axis convention, consistent across every file in the repository:**
> `PostGIS (X, Y, Z) = Unity (x, z, y)`. PostGIS Y carries the horizontal Unity Z;
> PostGIS Z carries the vertical. SRID 0 — raw Unity world units, not a geographic
> CRS.

---

## 5. Phase 4 — the ephemeris, and one bug worth the space

[`generate_solar_positions.py`](../Python%20&%20DB%20Scripts/DataGeneration/generate_solar_positions.py)
→ [`SolarDataLoader.cs`](../Unity%20Core%20Scripts/SolarData/SolarDataLoader.cs)

`pvlib` computes azimuth and apparent elevation for **every minute of the year** —
525,600 positions — written as a flat binary:

```
Header  16 B:  magic "SLRD" + version(i16) + year(i16) + totalMinutes(i32) + reserved(i32)
Data   N×8 B:  azimuth(f32) + elevation(f32)
Index:         (dayOfYear - 1) × 1440 + minuteOfDay
```

A flat array with a computed index rather than a lookup structure, because the
simulation asks for a position 8.6 million times and the answer must be a single
memory read. `GetPositionLerped` interpolates between adjacent minutes.

**The index is why the ephemeris runs in local STANDARD time, not local time.**
That indexing requires exactly 1,440 labelled minutes per day. A DST zone gives 1,380
on one day and 1,500 on another — so the array silently shifts by one hour from the
spring transition onward. That is not a hypothetical: it shifted the sun by an hour
for eight months of the year until it was found and fixed. Using standard time
throughout makes the bug impossible rather than merely absent.

### Tree shade is not raycast

[`process_tree_data.py`](../Python%20&%20DB%20Scripts/DataProcessing/process_tree_data.py)

Canopy shade is time-invariant, so it is a 2D spatial join in PostGIS rather than
geometry in the simulation loop: for each sample point, sum `shade_norm` of all trees
within the search radius, clamped to 1.0 so overlapping canopies in a dense park
cannot exceed full coverage; then roll up to `meo_edges.total_tree_value`.

That single decision keeps **1.28 million trees out of the Unity loop entirely**. Had
canopies been raycast, the mesh would have been several times larger and the run
several times longer, for a value that never changes with time of day.

---

## 6. Phase 5 — the simulation loop

[`ShadowAwarePathFinder.cs`](../Unity%20Core%20Scripts/Pathfinding/ShadowAwarePathFinder.cs)
· [`ShadowAwarePathFinder_Engines.cs`](../Unity%20Core%20Scripts/Pathfinding/ShadowAwarePathFinder_Engines.cs)

Despite the name, this component does not search for paths. It orchestrates the
export. Structure:

```
for each of 24 target dates:
    for minute in 03:00 .. 21:00 step 3:          # 361 steps
        point the sun from the ephemeris
        yield WaitForFixedUpdate                  # commit the light transform
        for each of 365,133 sample points:
            buffer (id, timestamp, !IsInShadow(point))
        every 3 simulated hours:
            COPY buffer -> meo_exposure_samples
            INSERT .. SELECT SUM -> meo_exposure_edges   (server-side)
            clear the buffer
```

### The shadow test

```csharp
float elevation_angle = sun.transform.eulerAngles.x;
if (elevation_angle <= threshold || elevation_angle >= 180f - threshold)
    return true;                                   // horizon guard: shadowed

Vector3 surfacePos = new Vector3(pos.x, elevation + 0.1f, pos.z);
return Physics.Raycast(surfacePos + Vector3.up * 3.0f,
                       -sun.transform.forward, 10000f, caster | ground);
```

Three things in five lines:

- **`-sun.transform.forward`** points from the surface back toward the sun. A hit
  means something blocks it.
- **The 3 m lift** keeps the road mesh from shadowing its own sample point.
- **The horizon guard** is a *correctness* fix, not an optimisation. `eulerAngles` is
  `[0, 360)`, so a sun 10° below the horizon reads as 350 — hence the two-sided test.
  Near the horizon a ray must cross kilometres of city, where float precision
  degrades and the ray can escape the mesh entirely and falsely report **sunlit**.
  That is the worst possible failure: silent, and the data looks plausible.
  Declaring those steps shadowed is both cheaper and closer to the truth.

The guard also has a consequence v2 depends on heavily: it bounds the longest
possible shadow at `H / tan(threshold)`, which is what makes spatial sharding exactly
correct rather than approximately correct. See
[ARCHITECTURE.md](ARCHITECTURE.md#3-why-bounding-box-sharding-is-correct-here).

### Why RAM stays flat at 250 MB

The buffer is flushed every 3 **simulated** hours and cleared. Peak memory is
therefore set by one block, not by the run length — one day or a full year costs the
same. That is the whole reason a 1.58-billion-row export fits on a desktop.

### Why it is resumable

Before each timestep the exporter reads back the sample ids already recorded at that
exact timestamp and skips them, so an interrupted multi-hour run restarts without
losing or duplicating work. The server-side aggregation is made idempotent the same
way, with a `NOT EXISTS` guard against `meo_exposure_edges`.

That resumability has a cost worth naming: the check reads **every** row for the
timestamp, not just those inside the current bounding box — ~365k ids per step. It is
cheap against `idx_meo_exposure_samples_time` and it was the right trade for a
six-hour desktop run. It is also one of the reasons v2 replaced per-timestep
resumability with per-task partition replacement.

### Bounding boxes in v1

v1 works over a box around start/end, padded by `edgeBBoxPadding = 100 m`, so a
neighbourhood can be simulated without the whole city. Running the full city means
placing the two transforms at opposite corners.

**This is the seed of v2's sectioning** — v1 already proved that a spatial subset can
be simulated independently. What v1 did not have was a reason to be *sure* of it, or
a way to run the subsets concurrently. v2 supplies both: the halo bound makes
independence exact, and sections become the unit of both parallelism and storage.

---

## 7. The derived edge table, and what it is for

```sql
INSERT INTO meo_exposure_edges (edge_id, datetime, sunlit_sum)
SELECT sp.edge_id, es.datetime, SUM(CAST(es.is_sunlit AS INT))
FROM meo_exposure_samples es
JOIN meo_sample_points sp ON sp.id = es.sample_point_id
WHERE es.datetime BETWEEN @startTime AND @endTime
  AND NOT EXISTS (SELECT 1 FROM meo_exposure_edges mee
                  WHERE mee.edge_id = sp.edge_id AND mee.datetime = es.datetime)
GROUP BY sp.edge_id, es.datetime;
```

110 GB of samples collapse to ~2 GB of per-edge sums, and the router's O(1) edge-cost
lookup reads the small table.

**It is an index, not a summary.** `sunlit_sum` answers "how sunlit is this edge right
now" — good enough for a Pareto search's coarse objective. It cannot answer "walked
eastward from this end, entering at 14:12, how many seconds in sun and what is the
longest unbroken stretch", because it has thrown away the order. Both questions have
consumers, so both tables exist and the sample table is the one that cannot be
regenerated from the other.

---

## 8. Known limitations

Stated plainly, because they are visible in the published data.

**Physical model**

- **Diffuse and reflected light are ignored.** `is_sunlit` is a binary direct-beam
  test. A north-facing street under open sky and a sealed courtyard both read
  "shadowed".
- **Near-horizon artifacts survive the guard.** The 08:00 column in October and
  November, and the abrupt 14:00→15:00 cliff in winter, are numerical rather than
  physical.
  [`db_correct_spikes.py`](../Python%20&%20DB%20Scripts/DataProcessing/db_correct_spikes.py)
  zeroes them post hoc, but its sunset heuristic — *any* increase after 14:00 is an
  artifact — is aggressive: in a real city the sun can legitimately clear a tall
  building and re-light a street. **Review its output before trusting it**, and note
  it rewrites both tables and cannot be undone.
- **The graph is planar.** Every node sits at `-112.0`. A hilly city needs per-node
  elevation and a revisit of the 2D `ST_DWithin` tree join.
- **Local standard time, not DST** — deliberate, see §5.

**Engineering**

- **Single-threaded raycasting.** `Physics.Raycast` must run on Unity's main thread,
  so the 6-hour run used one core regardless of how many the machine had. This is the
  single largest thing v2 changes, via `RaycastCommand.ScheduleBatch`.
- **One row per sample allocated a string.** The export built a
  `Tuple<Guid, string, bool>` and an interpolated CSV line per row: ~1.58 billion
  strings and ~4.7 billion heap objects over a run, all garbage. v2 writes binary
  from a bitset and allocates nothing in the hot path.
- **`O(n²)` preprocessing** in the node declustering (§4) and in
  `RoadGraphExtractor`'s node clustering.
- **Credentials are plaintext** in the v1 scripts. Fine for a local container;
  supply real ones from outside the repository.
- **Extraction thresholds are tuned for Manhattan** — `mergeRadius`, the 75° pruning
  and 165° dissolution angles were fitted to a dense orthogonal grid.

---

## 9. Running v1

Still supported, and the right choice for one neighbourhood or a quick check.

```bash
# 1. database (place 99_data_dump.sql.gz in db/ FIRST)
docker compose up -d
python "Python & DB Scripts/Database/test_connection.py"

# 2. verify what landed
python "Python & DB Scripts/Database/db_sanity_checks.py"
```

Then open the scene, press **Play**, and use the runtime panel in order:

| Button | What it does |
|---|---|
| `Reload Data & Snap` | loads waypoints + edges for the bounding box, snaps the sun to a date that has data |
| `Export Sample Points to DB` | generates 2 m samples along every edge in the box |
| `Export Exposure to DB` | the 6-hour sweep — resumable, safe to interrupt |
| `Clear DB Exposure For Target Dates` | wipes exposure for the box and dates, for a clean re-run |

Full setup, including the Unity project download: [`../SetUp Guide.md`](../SetUp%20Guide.md).

### The supporting scripts

| Script | Purpose |
|---|---|
| [`db_sanity_checks.py`](../Python%20&%20DB%20Scripts/Database/db_sanity_checks.py) | row counts, datetime range, table size, column types |
| [`calculate_exposure_stats.py`](../Python%20&%20DB%20Scripts/DataProcessing/calculate_exposure_stats.py) | per-month, per-hour sunlit percentages — the heatmap's source |
| [`validate_cumulative_cells.py`](../Python%20&%20DB%20Scripts/DataProcessing/validate_cumulative_cells.py) | plots exposed vs total cells per month; a flat month means the export missed it |
| [`db_correct_spikes.py`](../Python%20&%20DB%20Scripts/DataProcessing/db_correct_spikes.py) | zeroes near-horizon false positives (read §8 first) |
| [`partitioned_pathfinder.py`](../Python%20&%20DB%20Scripts/DataProcessing/partitioned_pathfinder.py) | k-means the graph into 20 blocks and pick one test route per block, so benchmarks are not all from one neighbourhood |
| [`plot_annual_exposure.py`](../Python%20&%20DB%20Scripts/Visualization/plot_annual_exposure.py) | the annual exposure curve |
| [`db_export_to_csv.py`](../Python%20&%20DB%20Scripts/Database/db_export_to_csv.py) · [`db_export_to_sql.py`](../Python%20&%20DB%20Scripts/Database/db_export_to_sql.py) | dumps. Both materialise the result set in memory — do not point them at `meo_exposure_samples` |

---

## 10. What v2 changes, and what it does not

| | v1 | v2 |
|---|---|---|
| **Schema** | `meo_exposure_samples` (sample_point_id, datetime, is_sunlit) | **identical**, behind a view; +section_id/task_id for addressing |
| **Rows** | 1,577,374,560 | **1,577,374,560** |
| Raycast call | `Physics.Raycast`, main thread, one at a time | `RaycastCommand.ScheduleBatch`, job system, a timestep at a time |
| Work unit | a bounding box, run to completion | (section, date, 3 h window) — 6,048 leased tasks |
| Databases | 1 | 1 coordinator + 10 data shards |
| Wire format | CSV text, one string per row | binary COPY from a bitset, no allocation |
| Wall clock | 6 h 00 min | 3 min 20 s |
| Failure recovery | restart the export (it resumes) | per-task, automatic, no coordinator |

The row count is the point of that table. v2 is faster hardware and better I/O
discipline applied to the same computation — not a different, cheaper computation.

Continue to [ARCHITECTURE.md](ARCHITECTURE.md).
