# Deployment

End-to-end runbook for the distributed pipeline. Assumes a Kubernetes cluster, a
PostgreSQL 14+ instance with PostGIS, and the Unity project from the
[Drive link](../README.md#1--unity-project).

> **Smoke-test first.** Run §6 with 2 workers and one date before attempting 50.
> The single riskiest assumption is that PhysX raycasting behaves identically in a
> headless Server-subtarget build; a 4-shard run confirms it in minutes.

---

## 0. Prerequisites

| | |
|---|---|
| Kubernetes | 1.25+ (`completionMode`, `ttlSecondsAfterFinished` GA) |
| PostgreSQL | 14+ with PostGIS 3.x |
| Unity | 2022.3 LTS + Linux IL2CPP module + a licence for the build |
| Docker | BuildKit enabled (`DOCKER_BUILDKIT=1`) for `--mount=type=secret` |
| Node shape | ≥ 4 vCPU / 8 Gi per worker |

Total fleet footprint at 50 workers: **200 vCPU / 400 Gi**. The `ResourceQuota` in
`00-namespace.yaml` caps at ~1.3× that.

---

## 1. Database schema

Order matters — each file depends on the previous.

```bash
export PGHOST=... PGUSER=admin PGDATABASE=city_data

# The single-node schema must already exist (waypoints, edges, sample_points, trees).
python "Python & DB Scripts/Database/db_pipeline_initializer.py"

# Distributed additions
psql -f distributed/db/01_distributed_schema.sql     # partitioned tables, shard map
psql -f distributed/db/02_work_queue.sql             # lease queue + functions
psql -f distributed/db/03_bulk_load_tuning.sql       # per-table storage params
```

`04_post_load_indexes.sql` runs **after** the fleet drains — not now.

## 2. PostgreSQL bulk profile

```bash
python distributed/orchestrator/pg_tune.py --detect --workers 50 \
    --profile bulk -o /etc/postgresql/postgresql.bulk.conf

# add to postgresql.conf:   include = 'postgresql.bulk.conf'
pg_ctl restart      # wal_level and shared_buffers need a restart, not a reload
```

Read [TUNING.md](TUNING.md) before applying — the bulk profile disables replication
and PITR, which is safe only because the data is reproducible.

Verify:

```sql
SHOW wal_level;            -- minimal
SHOW synchronous_commit;   -- off
SHOW max_wal_size;         -- 64GB (or as generated)
```

---

## 3. Build the images

### 3a. Worker (needs a Unity licence)

Two steps, because the Editor image is ~10 GB and must not ship to 50 pods.

```bash
cd /path/to/SunlightCityUnityProject     # the extracted Drive package

# Copy the distributed sources in (they live in this repo, not the package)
mkdir -p Assets/Scripts/Distributed Assets/Editor/Distributed
cp /path/to/repo/distributed/unity/Runtime/*.cs Assets/Scripts/Distributed/
cp /path/to/repo/distributed/unity/Editor/*.cs  Assets/Editor/Distributed/

# Build the headless player
DOCKER_BUILDKIT=1 docker build \
  -f /path/to/repo/distributed/docker/Dockerfile.build \
  --build-arg UNITY_VERSION=2022.3.62f1 \
  --secret id=unity_license,src=$HOME/.local/share/unity3d/Unity/Unity_lic.ulf \
  -t sunlightcity/builder:local .

# Extract the artifact
docker create --name extract sunlightcity/builder:local
docker cp extract:/build ./build
docker rm extract
```

Then wrap it in the slim runtime image, **from the repo root** so `distributed/` is
visible, with `build/` copied in:

```bash
cd /path/to/repo
cp -r /path/to/SunlightCityUnityProject/build ./build
docker build -f distributed/docker/Dockerfile.worker -t sunlightcity/worker:v1 .
```

> `distributed/docker/.dockerignore` excludes `build/` because the *build* image must
> not receive it. The *worker* image requires it. Docker reads one `.dockerignore`
> per context root, so run the worker build from the repo root where the root
> `.gitignore`-style rules apply, or temporarily remove the `build/` line.

Expected: **~400 MB**. If it is multiple GB the Server subtarget did not take effect.

### 3b. Orchestrator

```bash
docker build -f distributed/docker/Dockerfile.orchestrator \
    -t sunlightcity/orchestrator:v1 .
```

### 3c. Push

```bash
for i in worker orchestrator; do
  docker tag sunlightcity/$i:v1 $REGISTRY/sunlightcity/$i:v1
  docker push $REGISTRY/sunlightcity/$i:v1
done
```

Update the `images:` block in `distributed/k8s/kustomization.yaml`.

---

## 4. Cluster configuration

```bash
kubectl apply -f distributed/k8s/00-namespace.yaml

# Real credentials — never commit these. 10-config.yaml ships PLACEHOLDERS.
kubectl -n sunlightcity create secret generic sunlit-db-credentials \
    --from-literal=SUNLIT_DB_PASSWORD="$PGPASSWORD" \
    --from-literal=PGBOUNCER_AUTH_PASSWORD="$PGPASSWORD"

kubectl apply -f distributed/k8s/10-config.yaml
kubectl apply -f distributed/k8s/20-pgbouncer.yaml
kubectl -n sunlightcity rollout status deploy/pgbouncer
```

**Critical:** `SUNLIT_GLOBAL_ELEVATION` in the ConfigMap must equal
`GLOBAL_ELEVATION` in `db_pipeline_initializer.py` (`-112.0`). Workers refuse to
start on a mismatch — `meo_runs.config` pins it and `VerifyRunCompatibility()`
checks it, which is what stops a half-redeployed fleet writing two incompatible
datasets into one run.

---

## 5. Plan the run

```bash
python distributed/orchestrator/plan_tasks.py \
    --run-id run-2026-annual --shard-count 50 --year 2026
```

This provisions monthly partitions, materialises the edge→shard map, and inserts
50 × 24 = 1,200 tasks. Add `--dry-run` to preview.

Watch the reported **shard balance**: `max/mean` above ~1.5× means the slowest shard
will set your makespan — raise `--shard-count` so the hash averages out.

> `--emit-raw` also persists per-sample booleans: **~53× more write volume** and the
> fastest way to turn a 12-minute run into a multi-hour one. Only for analyses that
> genuinely need sub-edge resolution.

---

## 6. Smoke test — do this first

```bash
python distributed/orchestrator/plan_tasks.py \
    --run-id smoke --shard-count 4 --dates 6.21

kubectl -n sunlightcity create job smoke --from=cronjob/sunlit-lease-reaper --dry-run=client -o yaml >/dev/null
sed -e 's/parallelism: 50/parallelism: 2/' -e 's/completions: 50/completions: 2/' \
    -e 's/run-2026-annual/smoke/' -e 's/name: sunlit-map/name: sunlit-smoke/' \
    distributed/k8s/30-job-map.yaml | kubectl apply -f -

kubectl -n sunlightcity logs -f job/sunlit-smoke
```

**What to confirm in the log, in order:**

1. `[WorkerConfig] resolved:` — every value as intended
2. `scene ready: sun=…, N colliders` — **N must be > 0.** A collider-less scene
   reports the whole city as sunlit: plausible-looking, completely wrong data. The
   worker refuses to start rather than produce it.
3. `run 'smoke' verified compatible`
4. `[Combiner] shard k/4: … samples across … edges, accumulator … KB`
5. `[Worker] DONE task#… | … raycasts (…k/s) | … rows | sunlit …%`

Then check the data is sane:

```sql
SELECT * FROM meo_run_progress WHERE run_id = 'smoke';
-- Must be zero: the combiner invariant
SELECT count(*) FROM meo_exposure_edges_p WHERE sunlit_sum > sample_count;
-- Should trace a plausible daily arc, peaking near solar noon
SELECT datetime, sum(sunlit_sum) FROM meo_exposure_edges_p
GROUP BY datetime ORDER BY datetime LIMIT 30;
```

Cleanup: `kubectl -n sunlightcity delete job sunlit-smoke`

---

## 7. Launch the fleet

```bash
kubectl apply -k distributed/k8s/

kubectl -n sunlightcity get pods -l app.kubernetes.io/component=map-worker -w
python distributed/orchestrator/monitor.py --run-id run-2026-annual --watch
```

The lease-reaper CronJob starts automatically and is what recovers failed workers.

Watch for, in `monitor.py`:

| symptom | meaning |
|---|---|
| `hb` column climbing past 90 s | a worker is about to lose its lease |
| `workers active` < fleet size | pods pending, or the queue is draining |
| tasks bouncing pending→running repeatedly | lease TTL shorter than task duration — raise `SUNLIT_LEASE_SECONDS` |
| `failed` > 0 | exhausted retries; inspect `last_error` |

---

## 8. Finalise

```bash
kubectl -n sunlightcity wait --for=condition=complete job/sunlit-map --timeout=4h

python distributed/orchestrator/reduce_finalize.py \
    --run-id run-2026-annual --verify --build-indexes --refresh-rollups
```

Exit codes: `0` finalised · `2` incomplete · `3` integrity failure.

It **refuses** to finalise an incomplete run, because indexing a partial dataset is
expensive and produces something that *looks* finished.

Then switch to the serving profile and back it up — see
[TUNING.md § Switching to serving](TUNING.md#switching-to-serving).

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CrashLoopBackOff`, log shows `SUNLIT_RUN_ID is required` | ConfigMap/env missing | Check `envFrom` resolves; `kubectl describe pod` |
| `run '…' does not exist in meo_runs` | fleet started before planning | Run `plan_tasks.py` first |
| `Refusing to start: mixing these would produce an inconsistent dataset` | ConfigMap disagrees with `meo_runs.config` | Align `SUNLIT_GLOBAL_ELEVATION` etc., or re-plan |
| `scene contains ZERO colliders` | colliders not baked into the build | Run `Tools → Add MeshColliders to Selected` before building |
| Every point reads sunlit | layer mask excludes buildings | Recompute `SUNLIT_SHADOW_CASTER_MASK` = Σ `1 << layer` |
| `type not found` / `TypeInitializationException` on connect | IL2CPP stripped Npgsql's reflective types | `ManagedStrippingLevel.Disabled` — already set in `HeadlessBuildScript`; verify it took |
| Throughput far below 73k/s per worker | CPU limit throttling, or workers packed onto one node | Check `cpu` limits; confirm `topologySpreadConstraints` applied |
| Fleet stalls periodically | checkpoint storms | `log_checkpoints` output; raise `max_wal_size` |
| `cl_waiting > 0` in `SHOW POOLS` | pool too small | Raise `default_pool_size` |
| Tasks stuck `running`, no heartbeat | reaper not running | `kubectl -n sunlightcity get cronjob`; run `monitor.py --reap --once` |
| `no partition of relation … found for row` | partitions not provisioned for that year | `SELECT meo_provision_partitions(2026, 2026);` |

### Resetting

```sql
-- Retry only the failed tasks
UPDATE meo_tasks SET state='pending', attempts=0, worker_id=NULL
 WHERE run_id='run-2026-annual' AND state='failed';

-- Discard one date's output (partition pruning makes this cheap)
DELETE FROM meo_exposure_edges_p
 WHERE datetime >= '2026-06-21' AND datetime < '2026-06-22';

-- Full restart of a run
DELETE FROM meo_tasks WHERE run_id='run-2026-annual';
SELECT meo_drop_orphan_staging();
```

---

## 10. Cost sketch

At 200 vCPU for ~15 minutes (including pull and boot overhead), an annual run is
roughly **50 vCPU-hours**.

This workload is an unusually good fit for **spot/preemptible** instances: lease-based
recovery means a reclaimed task costs at most one task's work, and the entrypoint
releases the lease on SIGTERM so recovery is immediate rather than lease-timeout
bound. The commented `nodeSelector`/`tolerations` block in `30-job-map.yaml` enables
it. Typical spot discount is 60–80%.
