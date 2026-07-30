# Deployment runbook

Step-by-step instructions to take this from a clone to a finished annual dataset. Every
command is meant to be run as written, in order, with the stated check before moving on.

For *why* any of it is shaped this way, see [ARCHITECTURE.md](ARCHITECTURE.md),
[DB_CLUSTER.md](DB_CLUSTER.md) and [TUNING.md](TUNING.md).

**Total time: ~4 hours, almost all of it building the Unity image.** The pipeline itself
runs in 11 min 38 s.

---

## Contents

| | phase | time |
|---|---|---|
| [0](#0-prerequisites) | Prerequisites | — |
| [1](#1-the-unity-project) | Get the Unity project | 20 min |
| [2](#2-build-the-two-images) | Build the worker and orchestrator images | ~3 h |
| [3](#3-bring-up-the-cluster) | Bring up the database cluster | 10 min |
| [4](#4-seed-the-coordinator-with-v1-geometry) | Seed the coordinator with geometry | 30 min |
| [5](#5-apply-the-schema-and-plan-the-run) | Apply the schema, plan, provision | 5 min |
| [6](#6-smoke-test--do-this-before-the-fleet) | **Smoke test — do this first** | 15 min |
| [7](#7-run-the-fleet) | Run the fleet | **3 min** |
| [8](#8-finalise) | Finalise | 1 min |
| [9](#9-switch-to-serving-and-back-up) | Switch to serving, back up | 20 min |
| [10](#10-query-the-result) | Query it | — |
| [11](#11-troubleshooting) | Troubleshooting | — |
| [12](#12-teardown) | Teardown | — |

---

## 0. Prerequisites

| | version | notes |
|---|---|---|
| Kubernetes | 1.27+ | `completionMode`, `ttlSecondsAfterFinished` GA |
| PostgreSQL | 16 + PostGIS 3.4 | 14+ works; 16 is what the self-tests run against |
| Unity | 2022.3 LTS | + Linux IL2CPP module + a licence for the build |
| Docker | BuildKit | `DOCKER_BUILDKIT=1`, for `--mount=type=secret` |
| Python | 3.9+ | `pip install psycopg2-binary` |
| Nodes | 11 × 16 vCPU / 128 Gi with local NVMe | for the databases |
| Nodes | 50 × 8 vCPU / 16 Gi | for the workers; spot is a good fit |

Total footprint **588 vCPU / 2,050 Gi**. If that is not available, see
[§11 running smaller](#running-smaller) — a four-shard cluster still reaches 76×.

Storage class matters more than usual: the shards want **local NVMe**. Network-attached
storage will work and will cost you most of the ingest rate.

```bash
git clone https://github.com/leo-Zhizhu/SunlightCity------Large-Scale-Simulation-Data-Pipeline.git
cd SunlightCity------Large-Scale-Simulation-Data-Pipeline/Unity
export REGISTRY=your.registry.example.com        # where images will be pushed
export PGPASSWORD='choose-a-real-one'
```

Sanity-check the sizing for your hardware before spending three hours on an image:

```bash
python distributed/orchestrator/model.py
python distributed/orchestrator/model.py --balance --workers 50    # -> 7
```

---

## 1. The Unity project

The project — city meshes, colliders, scenes, baked assets — is **~1 GB** and lives
outside git:

> **[⬇ Download SunlightCity Unity Package (Google Drive)](https://drive.google.com/file/d/11OBhZlgjIVjEviUmiUhCcQU3MPTxiVOW/view?usp=sharing)**

```bash
unzip 'SunlightCity Unity Package.zip' -d ~/SunlightCityUnityProject
```

That build lags the scripts in this repository slightly but is a **known-stable, working
configuration** — start there. `Unity Core Scripts/` mirrors its `Assets/Scripts/`;
`distributed/unity/` is what you add for the fleet.

### 1a. Copy the distributed sources in

```bash
cd ~/SunlightCityUnityProject
mkdir -p Assets/Scripts/Distributed Assets/Editor/Distributed
cp /path/to/repo/Unity/distributed/unity/Runtime/*.cs Assets/Scripts/Distributed/
cp /path/to/repo/Unity/distributed/unity/Editor/*.cs  Assets/Editor/Distributed/
```

### 1b. Bake the solar ephemeris

```bash
cd /path/to/repo/Unity
pip install pvlib pandas numpy
python "Python & DB Scripts/DataGeneration/generate_solar_positions.py" --year 2026
# -> Assets/StreamingAssets/SolarData/Manhattan/sun_pos_2026.bin
cp -r Assets/StreamingAssets ~/SunlightCityUnityProject/Assets/
```

> ✅ **Check:** the `.bin` is 4,204,816 bytes — a 16-byte header plus 525,600 minutes ×
> 8 bytes. A worker without its ephemeris starts fine and then fails every task.

### 1c. Confirm colliders are baked

Open the scene and run **Tools → Add MeshColliders to Selected** on the building root if
they are not already there.

> ✅ **Check:** this is not optional and not cosmetic. A collider-less scene reports the
> **entire city as sunlit** — plausible-looking, completely wrong data. The worker
> refuses to start rather than produce it, but discovering that after a three-hour image
> build is an annoying way to find out.

---

## 2. Build the two images

### 2a. The worker (needs a Unity licence)

Two stages, because the Editor image is ~10 GB and must not ship to 54 pods.

```bash
cd ~/SunlightCityUnityProject

DOCKER_BUILDKIT=1 docker build \
  -f /path/to/repo/Unity/distributed/docker/Dockerfile.build \
  --build-arg UNITY_VERSION=2022.3.62f1 \
  --secret id=unity_license,src=$HOME/.local/share/unity3d/Unity/Unity_lic.ulf \
  -t sunlightcity/builder:local .
```

Pin the editor version to your project's `ProjectVersion.txt`. A mismatch triggers an
interactive upgrade prompt that hangs a batchmode build **forever**.

Extract the built player:

```bash
docker create --name extract sunlightcity/builder:local
docker cp extract:/build ./build
docker rm extract
```

Then wrap it in the slim runtime image, **from the repo root** so `distributed/` is
visible:

```bash
cp -r ~/SunlightCityUnityProject/build /path/to/repo/Unity/build
cd /path/to/repo/Unity
docker build -f distributed/docker/Dockerfile.worker -t sunlightcity/worker:v2 .
```

> `distributed/docker/.dockerignore` excludes `build/` because the *build* image must not
> receive it. The *worker* image requires it. Docker reads one `.dockerignore` per context
> root, which is why the worker build runs from the repo root.

> ✅ **Check:** `docker images sunlightcity/worker:v2` shows **~400 MB**. If it is
> multiple GB, the Server subtarget did not take effect and the image is carrying a
> graphics stack it will never use.

### 2b. The orchestrator

```bash
docker build -f distributed/docker/Dockerfile.orchestrator \
    -t sunlightcity/orchestrator:v2 .
```

> ✅ **Check:** ~120 MB, and the build prints `SQL shipped:` with all six `.sql` files.
> The Dockerfile asserts their presence, because a missing file fails the schema Job
> *after* it has already touched the coordinator.

### 2c. Push

```bash
for i in worker orchestrator; do
  docker tag sunlightcity/$i:v2 $REGISTRY/sunlightcity/$i:v2
  docker push $REGISTRY/sunlightcity/$i:v2
done
```

Point `distributed/k8s/kustomization.yaml`'s `images:` block at `$REGISTRY`.

---

## 3. Bring up the cluster

```bash
kubectl apply -f distributed/k8s/00-namespace.yaml

# Real credentials. 10-config.yaml ships PLACEHOLDERS.
kubectl -n sunlightcity create secret generic sunlit-db-credentials \
    --from-literal=SUNLIT_DB_PASSWORD="$PGPASSWORD" \
    --from-literal=PGBOUNCER_AUTH_PASSWORD="$PGPASSWORD"

kubectl apply -f distributed/k8s/10-config.yaml
kubectl apply -f distributed/k8s/20-postgres-cluster.yaml

kubectl -n sunlightcity rollout status statefulset/sunlit-coordinator --timeout=10m
kubectl -n sunlightcity rollout status statefulset/sunlit-shard       --timeout=10m

kubectl apply -f distributed/k8s/25-pgbouncer.yaml
kubectl -n sunlightcity rollout status deploy/pgbouncer
```

> ✅ **Check** — eleven databases, each named from its pod ordinal:
> ```bash
> for i in $(seq 0 9); do
>   kubectl -n sunlightcity exec sunlit-shard-$i -- \
>     psql -U admin -d sunlit_shard_$i -tAc "select current_database(), postgis_version()"
> done
> kubectl -n sunlightcity exec sunlit-coordinator-0 -- \
>   psql -U admin -d sunlit_coord -tAc "select current_database()"
> ```
> All nine shards must return `sunlit_shard_<i>`. If one returns nothing, its `initdb`
> hook did not run — see [§11](#11-troubleshooting).

### 3a. Mount the real tuning profiles

The manifests inline a short profile so a smoke test works immediately. For a real run,
use the annotated ones:

```bash
kubectl -n sunlightcity create configmap sunlit-pg-profiles \
  --from-file=profile.conf=distributed/db/postgresql.shard.bulk.conf
# then add a volumeMount at /etc/postgresql/profile.conf on the shard StatefulSet
# (the inline config already has `include_if_exists` pointing there)
kubectl -n sunlightcity rollout restart statefulset/sunlit-shard
```

> ✅ **Check:** `wal_level` really is `minimal`. It needs a restart, not a reload, and
> without it the ~500 GB WAL-skip does not happen.
> ```bash
> kubectl -n sunlightcity exec sunlit-shard-0 -- psql -U admin -d sunlit_shard_0 \
>   -tAc "show wal_level; show synchronous_commit; show max_wal_size"
> # minimal / off / 32GB
> ```

---

## 4. Seed the coordinator with v1 geometry

The distributed pipeline builds on v1's schema; it does not replace it. The coordinator
needs the road graph, sample points and trees before anything else works.

**If you already have a v1 database**, restore it into the coordinator:

```bash
kubectl -n sunlightcity port-forward svc/sunlit-coordinator 5432:5432 &
pg_restore -h localhost -U admin -d sunlit_coord --no-owner 99_data_dump.dump
```

**If you are starting from the mesh**, run v1's phases 1–3 against the coordinator — see
[V1_PIPELINE.md §9](V1_PIPELINE.md#9-running-v1):

```bash
export PGHOST=localhost PGUSER=admin PGDATABASE=sunlit_coord

# phase 1+2: schema + road graph + trees   (destructive: DROPs all meo_* tables)
python "Python & DB Scripts/Database/db_pipeline_initializer.py"

# phase 3: sample points — open the scene, press Play, use the runtime panel:
#   Reload Data & Snap  ->  Export Sample Points to DB
# Place the two transforms at opposite corners of the city for full coverage.

# phase 4: static tree shade (a PostGIS spatial join, no Unity)
python "Python & DB Scripts/DataProcessing/process_tree_data.py"
```

> ✅ **Check** — the reference scene:
> ```sql
> SELECT (SELECT count(*) FROM meo_waypoints)     AS waypoints,   -- 4,168
>        (SELECT count(*) FROM meo_edges)         AS edges,       -- 6,700
>        (SELECT count(*) FROM meo_sample_points) AS samples,     -- 365,133
>        (SELECT count(*) FROM meo_trees)         AS trees;       -- 1,280,954
> ```
> and that the sample points carry their ordering — without `sequence_index` and
> `distance_from_start` the directional API cannot work:
> ```sql
> SELECT count(*) FROM meo_sample_points
>  WHERE sequence_index IS NULL OR distance_from_start IS NULL;   -- must be 0
> ```

---

## 5. Apply the schema and plan the run

One Job does both: `apply_schema.py` then `plan_tasks.py --provision`.

```bash
kubectl apply -f distributed/k8s/30-job-schema.yaml
kubectl -n sunlightcity logs -f job/sunlit-schema
kubectl -n sunlightcity wait --for=condition=complete job/sunlit-schema --timeout=20m
```

Or from a workstation, which is easier to iterate on:

```bash
export SUNLIT_COORD_HOST=localhost SUNLIT_COORD_PORT=5432 \
       SUNLIT_COORD_DB=sunlit_coord SUNLIT_DB_USER=admin \
       SUNLIT_DB_PASSWORD="$PGPASSWORD" \
       SUNLIT_SHARD_HOST_TEMPLATE='sunlit-shard-{i}.sunlit-shards' \
       SUNLIT_SHARD_PORT=5432

cd distributed/orchestrator
python apply_schema.py --phase load --dry-run     # see the plan first
python apply_schema.py --phase load
python plan_tasks.py --run-id run-2026-annual --shards 10 --workers 50 --provision
```

**Read the planner's output.** Two numbers decide whether the run will go well:

```
  write imbalance   : 1.072x max/mean
  read contiguity   : 0.70  (a hash of section ids would give ~0.10)

   shard  sections    edges     samples   share  vs mean
       0        11       82      41,203   11.3%   1.014x
       ...
  tasks             : 30,240  (84 sections x 60 dates x 6 windows)
  cost spread       : 780.0x cheapest to dearest window
```

- **Write imbalance** above ~1.25× and the planner refuses: the slowest instance would
  set the makespan. Fix it with a smaller `--section-meters`, which gives the balanced cut
  finer granularity to work with.
- **Cost spread** near 1.0× would mean the per-window estimate has stopped
  distinguishing windows and the LPT ordering is doing nothing.

> ✅ **Check** — the queue and the leaves are ready:
> ```sql
> -- on the coordinator
> SELECT tasks_total, tasks_pending FROM meo_run_progress WHERE run_id='run-2026-annual';
> SELECT * FROM meo_shard_balance;
> -- on any shard
> SELECT * FROM meo_shard_leaf_inventory;         -- sections listed, 0 leaves yet
> SELECT count(*) FROM meo_sample_points;         -- 365,133 (replicated)
> ```

---

## 6. Smoke test — do this before the fleet

**Two workers, one date.** The single riskiest assumption in the whole deployment is that
PhysX raycasting behaves identically in a headless Server-subtarget build; a small run
confirms it in minutes instead of after an hour of wasted cluster time.

```bash
python plan_tasks.py --run-id smoke --shards 2 --workers 2 --dates 6.21 --provision

sed -e 's/parallelism: 50/parallelism: 2/' \
    -e 's/completions: 50/completions: 2/' \
    -e 's/run-2026-annual/smoke/' \
    -e 's/name: sunlit-map/name: sunlit-smoke/' \
    distributed/k8s/40-job-map.yaml | kubectl apply -f -

kubectl -n sunlightcity logs -f job/sunlit-smoke
```

**What to confirm in the log, in order:**

1. `[WorkerConfig] resolved:` — every value as intended, and note the reported
   `shadow halo = 2286 m`, which is the bound making per-section tasks independent
2. `scene ready: sun=…, N colliders` — **N must be > 0** (see §1c)
3. `whole-city collider set held; per-task BVH working set is bounded by …`
4. `run 'smoke' verified compatible (shard_count=2, grid and simulation constants match)`
5. `[Router] routing loaded: N sections across 2 online shard(s)`
6. `[Geometry] loaded section 384: 4,347 samples across 12 edges in … ms`
7. `[Worker] COMPUTED task#… in 1.8s | 260,820 rays (145k/s) | 0/60 steps below horizon`
8. `[Worker] WROTE task#… : 260,820 rows in 1.31s (199k rows/s)`

Line 7's rate is the one to check against the model's 295k/s per worker. Line 8's is the
per-stream COPY rate against the model's 200k rows/s.

**Then confirm the data is sane:**

```sql
-- on a shard: the leaf exists, is attached, and has exactly samples x steps rows
SELECT * FROM meo_verify_leaf_sizes(60);      -- expect 0 rows
SELECT * FROM meo_integrity_edges;            -- expect all violations = 0

-- a plausible daily arc, peaking near solar noon
SELECT datetime, count(*) FILTER (WHERE is_sunlit) AS sunlit, count(*) AS total
FROM meo_exposure_samples GROUP BY datetime ORDER BY datetime LIMIT 20;

-- and the WAL-skip actually happened
SELECT pg_current_wal_lsn();   -- delta across one task should be KB, not MB
```

**And that the schema behaves as documented** — 45 assertions against a throwaway
database:

```bash
distributed/db/tests/run_selftest.sh
```

Cleanup: `kubectl -n sunlightcity delete job sunlit-smoke`

---

## 7. Run the fleet

```bash
kubectl apply -f distributed/k8s/40-job-map.yaml
kubectl apply -f distributed/k8s/50-job-reduce.yaml   # the reaper CronJob starts now
kubectl -n sunlightcity delete job sunlit-reduce      # not yet — after the map drains

python monitor.py --run-id run-2026-annual --watch
```

```
╭──────────────────────────────────────────────────────────────────────────╮
│ SunlightCity · run run-2026-annual                              14:22:07 │
├──────────────────────────────────────────────────────────────────────────┤
│ ████████████████████████████▎                  63.10%                    │
├──────────────────────────────────────────────────────────────────────────┤
│  done  3,816   running   50   pending  2,182   failed    0               │
│  workers  50   sections   84   shards  10   rows     995,249,160         │
│  affinity hit  92.4%                                                     │
│  raycasts    995.2M   rate    14.6M/s   vs 1 node  200.4x                │
│  elapsed        0:01:08   ETA      0:00:40   finish ~14:22               │
╰──────────────────────────────────────────────────────────────────────────╯

  shard load vs cap 6:  [██████████]  50/60 slots in use
   shard     state  run /cap   done  pending failed          rows
       0    online    5   /6    381      224      0    99,368,610
       ...
```

**Watch these four, in priority order:**

| symptom | meaning | action |
|---|---|---|
| some shards at cap, others at 0 | the topology is unbalanced, or retries have clustered. The makespan is being set by the busy ones. | re-plan with smaller `--section-meters` |
| **every** shard at cap | **healthy** — the steady state for a fleet larger than shards × cap | none |
| affinity below ~85% | dispatch is thrashing the working sets; the map phase will run long | check for widespread task failures forcing re-dispatch |
| `hb` climbing past 300 s | a worker is a third of the way to being reaped | check that pod's logs and node |
| `failed` > 0 | retries exhausted | read `last_error`; see [§11](#11-troubleshooting) |

```bash
kubectl -n sunlightcity wait --for=condition=complete job/sunlit-map --timeout=2h
```

---

## 8. Finalise

```bash
kubectl apply -f distributed/k8s/50-job-reduce.yaml
kubectl -n sunlightcity logs -f job/sunlit-reduce
```

Or directly:

```bash
python reduce_finalize.py --run-id run-2026-annual
```

It **refuses to finalise an incomplete run** (exit 2), because rolling up a partial
dataset is expensive and produces something that *looks* finished — a missing
section-window surfaces later as a street with no shade at any hour, indistinguishable
from a genuinely sunny street.

Exit codes: `0` finalised · `1` operational · `2` incomplete · `3` integrity failure.

The report ends with the numbers to compare against [PERFORMANCE.md](PERFORMANCE.md):

```
  map wall clock  : 0:01:47
  reduce          : 0:02:10  (9 shards in parallel)
  total           : 0:02:35
  throughput      : 14.7M raycasts/s
  per worker      : 294K/s  (v1 single-thread baseline 73K/s)
  vs v1 end-to-end: 161.5x  (model predicts 161.5x)
```

> ✅ **Check:**
> ```sql
> -- coordinator: nothing outstanding
> SELECT * FROM meo_run_gaps;                              -- expect 0 rows
> -- each shard
> SELECT * FROM meo_shard_summary;                         -- violations = 0
> SELECT * FROM meo_verify_leaf_sizes(60);                 -- expect 0 rows
> -- and the total is right: it must equal the sample count exactly, because every
> -- sample point is evaluated at every timestep of every date
> --   365,133 samples x 360 steps x 60 dates = 7,886,872,800
> SELECT sum(rows_written) FROM meo_tasks WHERE run_id='run-2026-annual';
> ```

---

## 9. Switch to serving, and back up

The bulk profile cannot produce a valid base backup. Until this is done the dataset is
only as safe as re-running the pipeline.

```bash
# repoint each shard's include at the serving profile, then restart
kubectl -n sunlightcity create configmap sunlit-pg-profiles \
  --from-file=profile.conf=distributed/db/postgresql.shard.serving.conf \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sunlightcity rollout restart statefulset/sunlit-shard
kubectl -n sunlightcity rollout status statefulset/sunlit-shard --timeout=15m

# bring up the analytics federation on the coordinator
python apply_schema.py --phase serve
python reduce_finalize.py --run-id run-2026-annual --no-rollup --no-indexes

# base backups
for i in $(seq 0 9); do
  kubectl -n sunlightcity exec sunlit-shard-$i -- \
    pg_basebackup -D /var/lib/postgresql/backup/$(date +%F) -Ft -z -P -U admin
done
```

> ✅ **Check:**
> ```sql
> SHOW wal_level;   -- replica on every shard
> SELECT * FROM meo_route_locality();   -- most routes should touch 1 shard
> ```

---

## 10. Query the result

### Routing — direct to the owning shard

```sql
-- once, at service warm-up, on the coordinator: cache the whole 6,700-row map
SELECT edge_id, shard_index, host, port, dbname FROM meo_edge_routing;

-- per request, on the owning shard
SELECT * FROM meo_edge_directional_cost(
    '…edge uuid…',
    '2026-07-15 16:00:00',
    p_reverse        := false,
    p_walk_speed_mps := 1.35);
```

```
 samples | edge_length_m | traverse_seconds | sun_seconds | shade_seconds | pct_sun
     201 |           400 |            296.3 |       207.4 |          90.4 |   69.65
 entered_in_sun | exited_in_sun | longest_sun_run_m | timesteps_spanned
 f              | t             |               280 |                 3
```

Flip `p_reverse` and the numbers change — that is the whole point of the schema. For a
whole candidate path:

```sql
SELECT * FROM meo_route_plan(ARRAY['…','…','…']::uuid[]);
-- shard_index | host | port | dbname | edge_count | edge_ids
-- normally ONE row, because the assignment is spatially contiguous
```

### The v1 API still works, unchanged

```sql
SELECT sample_point_id, datetime, is_sunlit FROM meo_exposure_samples LIMIT 5;
SELECT edge_id, datetime, sunlit_sum      FROM meo_exposure_edges    LIMIT 5;
```

Exactly v1's columns, in v1's order. Asserted in the self-test rather than assumed.

### Analytics — federated across all ten

```sql
-- on the coordinator
SELECT * FROM meo_network_snapshot('2026-07-15 11:00:00');
--  edges | samples | sunlit | pct_sunlit | shards_read
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| pod `CrashLoopBackOff`, `SUNLIT_RUN_ID is required` | ConfigMap/env missing | `kubectl describe pod`; check `envFrom` resolves |
| `run '…' does not exist in meo_runs` | fleet started before the schema Job | wait for `job/sunlit-schema` |
| `Refusing to start: mixing these would produce an inconsistent dataset` | ConfigMap disagrees with `meo_runs.config` | align `SUNLIT_SECTION_*` and `SUNLIT_GLOBAL_ELEVATION` with what the planner reported, or re-plan |
| `scene contains ZERO colliders` | colliders not baked into the build | §1c, then rebuild |
| every point reads sunlit | layer mask excludes buildings | recompute `SUNLIT_SHADOW_CASTER_MASK` = Σ `1 << layer` |
| `type not found` / `TypeInitializationException` on connect | IL2CPP stripped Npgsql's reflective types | `ManagedStrippingLevel.Disabled` — already set in `HeadlessBuildScript`; verify it took |
| `section N has no online shard` | a shard is `draining`/`offline` mid-dispatch | the task fails and re-dispatches; bring the shard back or reassign |
| `section parent … does not exist` | `plan_tasks.py --provision` not run for that shard | re-run it |
| `meo_sample_points not found on this shard` | geometry not replicated | `plan_tasks.py --provision` |
| worker rate far below 295k/s | fractional CPU request collapsed the job worker pool | request **whole** cores; see [OPTIMIZATION.md §1](OPTIMIZATION.md#1-batched-raycasts--30) |
| ingest below spec, waits on `extend` | the one-leaf-per-task design is not isolating writers | check leaves are being created, not appended to |
| WAL delta ≈ bytes written | the WAL-skip is not happening | `SHOW wal_level` — needs `minimal` **and a restart** |
| shards stall periodically | checkpoint storms | `log_checkpoints` output; raise `max_wal_size` ([TUNING.md](TUNING.md#max_wal_size--the-one-usually-tuned-backwards)) |
| `could not resize shared memory segment` during reduce | `/dev/shm` too small for parallel maintenance workers | already 2Gi in the manifest; raise if you raised `max_parallel_maintenance_workers` |
| `cl_waiting > 0` sustained in `SHOW POOLS` | pool too small | raise `default_pool_size` |
| tasks stuck `running`, no heartbeat | the reaper is not running | `kubectl get cronjob`; `monitor.py --reap --once` |
| pods `Pending` with a quota error | fleet larger than the ResourceQuota | raise it in `00-namespace.yaml` — the error is the intended behaviour |

### Resetting

```sql
-- retry only the failed tasks
UPDATE meo_tasks SET state='pending', attempts=0, worker_id=NULL
 WHERE run_id='run-2026-annual' AND state='failed';

-- discard and redo everything on one shard (~3,360 tasks, ~1 min of fleet time)
UPDATE meo_tasks SET state='pending', attempts=0, worker_id=NULL
 WHERE run_id='run-2026-annual' AND shard_index=3;

-- discard one section-date-window's output, on its shard
SELECT meo_reset_leaf(384, '2026-06-15 15:00:00'::timestamp, 4);

-- full restart of a run
DELETE FROM meo_tasks WHERE run_id='run-2026-annual';   -- coordinator
SELECT meo_drop_orphan_leaves();                        -- each shard
```

### Running smaller

The model degrades gracefully, and `pg_tune.py` will size a smaller instance correctly
and warn if the fleet will be waiting on it.

```bash
python distributed/orchestrator/model.py --sweep          # 4 shards still reach 76x
python distributed/orchestrator/model.py --workers-sweep  # min shards per fleet size
```

For a 25-worker / 4-shard deployment, change **four** places together — they are listed in
`kustomization.yaml`'s comment block, with a ready-to-uncomment patch:

| what | where |
|---|---|
| fleet size | `40-job-map.yaml` — `parallelism` **and** `completions` |
| | `30-job-schema.yaml` — `SUNLIT_WORKER_COUNT` |
| shard count | `20-postgres-cluster.yaml` — StatefulSet `replicas` |
| | `10-config.yaml` — `SUNLIT_SHARD_COUNT` |

Scaling workers without scaling shards converts a compute-bound pipeline into an
I/O-bound one — the exact failure this architecture exists to avoid.

---

## 12. Teardown

```bash
kubectl -n sunlightcity delete job sunlit-map sunlit-reduce sunlit-schema
kubectl -n sunlightcity delete cronjob sunlit-lease-reaper

# The PVCs are NOT deleted with the StatefulSet, and that is a Kubernetes default
# worth knowing: eleven volumes totalling ~2.5 TB outlive everything above.
kubectl -n sunlightcity delete statefulset sunlit-shard sunlit-coordinator
kubectl -n sunlightcity get pvc                    # look before deleting
kubectl -n sunlightcity delete pvc --all           # only if you mean it

kubectl delete namespace sunlightcity
```

## 13. Cost

At 588 vCPU for ~11 minutes including spin-up, an annual run is **~109 vCPU-hours**.

The map workers are an unusually good fit for **spot/preemptible** instances:
lease-based recovery means a reclaimed task costs at most one task's work, and the
entrypoint releases the lease on `SIGTERM` so recovery is immediate rather than
lease-timeout bound. The commented `nodeSelector`/`tolerations` block in
`40-job-map.yaml` enables it; typical discount is 60–80%.

The **databases should not be spot**. Losing one shard costs ~20 seconds of recompute;
losing several at once costs the run.
