-- =============================================================================
-- SunlightCity — work queue (phase 2 of 6)          ***  COORDINATOR ONLY  ***
--
-- The control plane lives on its own instance, apart from the ten data shards.
-- That separation is deliberate and it is not merely tidiness:
--
--   meo_tasks is a few thousand rows updated tens of times each (claim, ~40
--   heartbeats, complete) — a small, latency-sensitive, high-churn table. The
--   exposure partitions are a hundred gigabytes of append-only bulk. Putting
--   both on one instance means every claim competes with a checkpoint flushing
--   a bulk load's dirty buffers, and claim latency degrades exactly when the
--   fleet is busiest. Separate instances, separate page caches, separate WAL.
--
-- A task is one (section, date, 3 h window).
--
--
-- WHY A PULL QUEUE AND NOT A KUBERNETES INDEXED JOB
-- ------------------------------------------------
-- An Indexed Job hands pod N work item N. Simple, but it binds work statically,
-- and these tasks are not uniform: cost tracks DAYLIGHT inside the window, not
-- the window's length, because the worker's horizon guard skips whole timesteps
-- whose sun is below 5 deg. A 12:00-15:00 window in June is all daylight; the
-- same window in December is partly below the guard. Sections differ too — a
-- midtown square kilometre holds several times the road length of the northern
-- tip. Static assignment would leave the makespan set by the worst single task.
--
--
-- WHY POSTGRES AND NOT RABBITMQ / REDIS / SQS
-- -------------------------------------------
-- SELECT ... FOR UPDATE SKIP LOCKED makes Postgres a correct, efficient queue,
-- and the pipeline already depends on Postgres. A broker would add a second
-- failure domain for no gain at this scale (30,240 tasks, 50 consumers), and it
-- would cost the one thing that actually matters here: with an external broker,
-- claiming a task and recording its completion are two systems, and you inherit
-- the dual-write problem.
--
--
-- FAILURE MODEL
-- -------------
-- Tasks are LEASED, not dequeued. A worker renews by heartbeat. If the pod is
-- OOM-killed, preempted, or its node dies, the lease expires and the task
-- returns to the pool. There is no coordinator to detect the death and no
-- cleanup path to get wrong — absence of a heartbeat IS the failure signal, and
-- it behaves identically for OOM kills, spot preemption, kernel panics and
-- network partitions.
--
-- Every task is IDEMPOTENT: its output is one partition leaf keyed by
-- (section, date, window), and the worker replaces that leaf wholesale. So
-- at-least-once delivery is sufficient and exactly-once is never needed.
--
-- Idempotent: safe to re-run.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meo_task_state') THEN
        CREATE TYPE meo_task_state AS ENUM
            ('pending', 'running', 'done', 'failed', 'cancelled');
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- Run-level metadata.
--
-- config is a frozen copy of every parameter the fleet must agree on — the
-- section grid in particular. A worker whose environment disagrees refuses to
-- start, which is what stops a half-redeployed fleet writing two mutually
-- inconsistent datasets into one run_id. That corruption is otherwise invisible
-- until someone queries the result.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_runs (
    run_id       TEXT PRIMARY KEY,
    shard_count  INTEGER     NOT NULL,
    section_count INTEGER    NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    notes        TEXT,
    config       JSONB       NOT NULL DEFAULT '{}'::JSONB,

    -- Admission control, see meo_claim_task. Default 6 = 12 productive COPY
    -- streams on a 16 vCPU instance / 2 streams per worker.
    max_tasks_per_shard INTEGER NOT NULL DEFAULT 6
        CHECK (max_tasks_per_shard >= 1)
);


CREATE TABLE IF NOT EXISTS meo_tasks (
    task_id      BIGSERIAL      PRIMARY KEY,

    -- ---- Work definition. (run_id, section_id, sim_date, window_index) is the
    -- natural key, and it is exactly the identity of one partition leaf.
    run_id       TEXT           NOT NULL,
    section_id   INTEGER        NOT NULL,
    sim_date     DATE           NOT NULL,
    window_index INTEGER        NOT NULL,
    start_minute INTEGER        NOT NULL,
    end_minute   INTEGER        NOT NULL,
    step_minute  INTEGER        NOT NULL DEFAULT 3,

    -- Denormalised from meo_sections. The claim path must not join: it runs on
    -- every worker every few seconds and holds a row lock while it does.
    shard_index  INTEGER        NOT NULL,

    -- ---- Scheduling
    state        meo_task_state NOT NULL DEFAULT 'pending',
    priority     INTEGER        NOT NULL DEFAULT 0,   -- higher dispatches first
    -- Cost estimate in raycasts. Longest-processing-time-first bounds makespan at
    -- 4/3 of optimal for identical machines. The estimate need not be accurate,
    -- only correctly ORDERED.
    est_raycasts BIGINT         NOT NULL DEFAULT 0,

    -- ---- Leasing
    attempts     INTEGER        NOT NULL DEFAULT 0,
    max_attempts INTEGER        NOT NULL DEFAULT 3,
    worker_id    TEXT,
    leased_at        TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at     TIMESTAMPTZ,

    -- ---- Results / diagnostics
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    rows_written BIGINT,
    raycasts_done BIGINT,
    -- Whether the worker already had this (section, window) working set loaded.
    -- Aggregated by monitor.py into an affinity hit rate; a collapse in that rate
    -- is the earliest sign the dispatch order has gone wrong.
    affinity_hit BOOLEAN,
    last_error   TEXT,

    CONSTRAINT meo_tasks_uniq UNIQUE (run_id, section_id, sim_date, window_index),
    CONSTRAINT meo_tasks_minutes CHECK (start_minute < end_minute AND step_minute > 0)
);


-- The claim query only ever scans pending rows, so the index stays partial: it
-- remains small after hundreds of thousands of completed tasks, and claim
-- latency does not degrade over the life of the database.
CREATE INDEX IF NOT EXISTS idx_meo_tasks_claimable
    ON meo_tasks (run_id, shard_index, priority DESC, est_raycasts DESC, task_id)
    WHERE state = 'pending';

-- Drives the affinity fast path: "another task with my section and window".
CREATE INDEX IF NOT EXISTS idx_meo_tasks_affinity
    ON meo_tasks (run_id, section_id, window_index, est_raycasts DESC)
    WHERE state = 'pending';

-- Drives lease-expiry reclamation and per-shard admission counting.
CREATE INDEX IF NOT EXISTS idx_meo_tasks_running
    ON meo_tasks (shard_index, lease_expires_at)
    WHERE state = 'running';

CREATE INDEX IF NOT EXISTS idx_meo_tasks_run_state
    ON meo_tasks (run_id, state);


-- -----------------------------------------------------------------------------
-- Shards with a free admission slot.
--
-- An aggregate over the <= 50 rows in state 'running', served by
-- idx_meo_tasks_running, so it costs microseconds and is always current. Kept as
-- a separate function so the admission rule is stated once and can be inspected
-- directly during a run:
--
--   SELECT * FROM meo_admissible_shards('run-2026-annual');
--
-- An empty result means every instance is saturated, which is the healthy steady
-- state for a fleet larger than shards x cap. It is also visible in
-- meo_shard_progress as tasks_running = admission_cap across the board.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_admissible_shards(p_run_id TEXT)
RETURNS SETOF INTEGER AS $$
    SELECT sh.shard_index
    FROM meo_shards sh
    CROSS JOIN meo_runs r
    WHERE r.run_id = p_run_id
      AND sh.state = 'online'
      AND (SELECT count(*) FROM meo_tasks t
           WHERE t.state = 'running' AND t.shard_index = sh.shard_index)
          < r.max_tasks_per_shard;
$$ LANGUAGE sql STABLE STRICT;


-- -----------------------------------------------------------------------------
-- CLAIM — the heart of the queue, and where the fleet is coupled to the cluster.
--
-- Three concerns, in priority order.
--
-- 1. PER-SHARD ADMISSION CONTROL.
--    A shard absorbs about twelve concurrent COPY streams before extra streams
--    stop buying throughput; each worker holds two. So at most six workers should
--    be writing to any one shard at a time. Nothing about the work distribution
--    guarantees that on its own — sections are not claimed uniformly, and a burst
--    of retries in one region would happily point thirty workers at one instance
--    and collapse its throughput while nine peers idle.
--
--    So the claim refuses to hand out a task whose shard is already at capacity.
--    In the balanced case (10 shards x 6 = 60 slots for 50 workers) it never
--    binds; under skew it is what keeps the cluster's aggregate ingest flat
--    instead of concentrated. This is the coordination between the Kubernetes
--    fleet and the database cluster, and it is one predicate.
--
-- 2. AFFINITY.
--    A task's rays all originate inside one section during one 3 h window, so the
--    colliders they can reach — the section plus its 2,286 m shadow halo, swept
--    over that window's azimuth arc — are a working set the worker has already
--    paged in. There are 84 x 6 = 504 such working sets but 30,240 tasks, so
--    dispatching a task that matches the caller's current (section, window) reuses
--    the warm set twelve times out of twelve instead of once.
--
--    Without affinity the fleet would fault in a fresh working set for all 30,240
--    tasks. With it, 504 times. The parameters are hints: if no matching task is
--    admissible the function falls straight through to LPT, so affinity can never
--    stall the queue or unbalance it.
--
-- 3. LPT.
--    Otherwise take the most expensive admissible task.
--
-- `#variable_conflict use_column` makes ambiguous identifiers resolve to table
-- columns rather than the RETURNS TABLE output variables of the same name;
-- without it PostgreSQL raises "column reference is ambiguous" at runtime.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_claim_task(
    p_run_id         TEXT,
    p_worker_id      TEXT,
    p_lease_seconds  INTEGER DEFAULT 900,
    p_prefer_section INTEGER DEFAULT NULL,
    p_prefer_window  INTEGER DEFAULT NULL
)
RETURNS TABLE (
    task_id      BIGINT,
    section_id   INTEGER,
    sim_date     DATE,
    window_index INTEGER,
    start_minute INTEGER,
    end_minute   INTEGER,
    step_minute  INTEGER,
    shard_index  INTEGER,
    attempt      INTEGER,
    affinity_hit BOOLEAN
) AS $$
#variable_conflict use_column
DECLARE
    v_adm  INTEGER[];
    v_cid  BIGINT;
    v_hit  BOOLEAN := FALSE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meo_runs WHERE run_id = p_run_id) THEN
        RAISE EXCEPTION 'run % does not exist in meo_runs', p_run_id;
    END IF;

    -- Materialised into an array, once, rather than left as a subquery: an
    -- uncorrelated set-returning function inside IN() can be re-evaluated per
    -- candidate row, and this runs on every worker every few seconds.
    SELECT array_agg(s) INTO v_adm FROM meo_admissible_shards(p_run_id) s;
    IF v_adm IS NULL THEN
        RETURN;   -- every online shard is at its admission cap
    END IF;

    -- ---- 1. Affinity fast path -------------------------------------------
    IF p_prefer_section IS NOT NULL AND p_prefer_window IS NOT NULL THEN
        SELECT t.task_id INTO v_cid
        FROM meo_tasks t
        WHERE t.run_id = p_run_id
          AND t.state = 'pending'
          AND t.section_id = p_prefer_section
          AND t.window_index = p_prefer_window
          AND t.shard_index = ANY (v_adm)
        ORDER BY t.est_raycasts DESC, t.task_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1;

        v_hit := v_cid IS NOT NULL;
    END IF;

    -- ---- 2. LPT fallback --------------------------------------------------
    IF v_cid IS NULL THEN
        SELECT t.task_id INTO v_cid
        FROM meo_tasks t
        WHERE t.run_id = p_run_id
          AND t.state = 'pending'
          AND t.shard_index = ANY (v_adm)
        ORDER BY t.priority DESC, t.est_raycasts DESC, t.task_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1;
    END IF;

    IF v_cid IS NULL THEN
        RETURN;   -- nothing admissible: queue empty, or every shard is at capacity
    END IF;

    RETURN QUERY
    UPDATE meo_tasks t
    SET state            = 'running',
        worker_id        = p_worker_id,
        attempts         = t.attempts + 1,
        leased_at        = now(),
        heartbeat_at     = now(),
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        started_at       = COALESCE(t.started_at, now()),
        affinity_hit     = v_hit
    WHERE t.task_id = v_cid
    RETURNING t.task_id, t.section_id, t.sim_date, t.window_index,
              t.start_minute, t.end_minute, t.step_minute,
              t.shard_index, t.attempts, t.affinity_hit;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- HEARTBEAT — extends the lease. Called every ~30 s while a task runs.
--
-- Returns FALSE if this worker no longer owns the task: its lease expired and a
-- peer took over. A worker seeing FALSE must ABANDON its work immediately rather
-- than finish and write, because otherwise the original and the replacement would
-- both build the same partition leaf. The heartbeat therefore doubles as a
-- FENCING TOKEN, and that is what makes lease expiry safe rather than merely
-- convenient.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_heartbeat(
    p_task_id       BIGINT,
    p_worker_id     TEXT,
    p_lease_seconds INTEGER DEFAULT 900,
    p_raycasts_done BIGINT  DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    n INTEGER;
BEGIN
    UPDATE meo_tasks
    SET heartbeat_at     = now(),
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        raycasts_done    = COALESCE(p_raycasts_done, raycasts_done)
    WHERE task_id = p_task_id
      AND worker_id = p_worker_id
      AND state = 'running';

    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n = 1;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION meo_complete_task(
    p_task_id      BIGINT,
    p_worker_id    TEXT,
    p_rows_written BIGINT DEFAULT NULL,
    p_raycasts     BIGINT DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    n INTEGER;
BEGIN
    UPDATE meo_tasks
    SET state            = 'done',
        finished_at      = now(),
        rows_written     = p_rows_written,
        raycasts_done    = COALESCE(p_raycasts, raycasts_done),
        last_error       = NULL,
        lease_expires_at = NULL
    WHERE task_id = p_task_id
      AND worker_id = p_worker_id
      AND state = 'running';

    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n = 1;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- FAIL — retry until max_attempts, then park in 'failed'.
--
-- Terminal 'failed' rather than infinite retry is deliberate: a deterministically
-- broken task (a corrupt mesh region, a date outside the ephemeris) would
-- otherwise spin forever, burning a worker slot and — worse — permanently holding
-- one of its shard's six admission slots.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_fail_task(
    p_task_id   BIGINT,
    p_worker_id TEXT,
    p_error     TEXT
)
RETURNS BOOLEAN AS $$
DECLARE
    n INTEGER;
BEGIN
    UPDATE meo_tasks
    SET state = CASE WHEN attempts >= max_attempts THEN 'failed'::meo_task_state
                     ELSE 'pending'::meo_task_state END,
        last_error       = left(p_error, 4000),
        worker_id        = NULL,
        lease_expires_at = NULL,
        finished_at      = CASE WHEN attempts >= max_attempts THEN now() END
    WHERE task_id = p_task_id
      AND worker_id = p_worker_id
      AND state = 'running';

    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n = 1;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- REAP — return expired leases to the pool.
--
-- This is the entire node-failure recovery mechanism: no liveness probe, no
-- controller watching pods. Call periodically (the k8s CronJob does, every
-- minute). Safe to run concurrently with itself.
--
-- Reaping also frees the dead worker's admission slot on its shard, which matters
-- more than it looks: without it, a node failure would permanently shrink the
-- cluster's usable write concurrency.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_reap_expired_leases(p_run_id TEXT DEFAULT NULL)
RETURNS TABLE (
    reaped_task_id BIGINT,
    shard_index    INTEGER,
    lost_worker    TEXT,
    attempt_count  INTEGER,
    new_state      meo_task_state
) AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    UPDATE meo_tasks t
    SET state = CASE WHEN t.attempts >= t.max_attempts THEN 'failed'::meo_task_state
                     ELSE 'pending'::meo_task_state END,
        last_error = format('lease expired (last heartbeat %s, worker %s)',
                            COALESCE(t.heartbeat_at::TEXT, 'never'),
                            COALESCE(t.worker_id, 'unknown')),
        worker_id  = NULL,
        lease_expires_at = NULL
    WHERE t.state = 'running'
      AND t.lease_expires_at < now()
      AND (p_run_id IS NULL OR t.run_id = p_run_id)
    RETURNING t.task_id, t.shard_index, t.last_error, t.attempts, t.state;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- Progress views.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW meo_run_progress AS
SELECT
    r.run_id,
    r.shard_count,
    r.section_count,
    r.created_at,
    r.started_at,
    r.finished_at,
    count(t.task_id)                                               AS tasks_total,
    count(*) FILTER (WHERE t.state = 'pending')                    AS tasks_pending,
    count(*) FILTER (WHERE t.state = 'running')                    AS tasks_running,
    count(*) FILTER (WHERE t.state = 'done')                       AS tasks_done,
    count(*) FILTER (WHERE t.state = 'failed')                     AS tasks_failed,
    count(DISTINCT t.worker_id) FILTER (WHERE t.state = 'running') AS workers_active,
    COALESCE(sum(t.rows_written), 0)                               AS rows_written,
    COALESCE(sum(t.raycasts_done), 0)                              AS raycasts_done,
    COALESCE(sum(t.est_raycasts), 0)                               AS raycasts_planned,
    round(100.0 * count(*) FILTER (WHERE t.state = 'done')
          / NULLIF(count(t.task_id), 0), 2)                        AS pct_done,
    round(avg(EXTRACT(EPOCH FROM (t.finished_at - t.started_at)))
          FILTER (WHERE t.state = 'done')::NUMERIC, 2)             AS avg_task_seconds,
    -- Fraction of completed tasks that reused a warm working set. Designed to sit
    -- near (tasks - section_windows) / tasks = 92%; a collapse means dispatch is
    -- thrashing the collider set and the map phase will run long.
    round(100.0 * count(*) FILTER (WHERE t.state = 'done' AND t.affinity_hit)
          / NULLIF(count(*) FILTER (WHERE t.state = 'done'), 0), 1) AS pct_affinity_hit
FROM meo_runs r
LEFT JOIN meo_tasks t USING (run_id)
GROUP BY r.run_id, r.shard_count, r.section_count,
         r.created_at, r.started_at, r.finished_at;


-- Per-shard view. This is the one to watch during a run: a shard sitting at its
-- admission cap while others are idle means the topology is unbalanced, and a
-- shard with zero running tasks while work remains means it is 'draining' or
-- unreachable.
CREATE OR REPLACE VIEW meo_shard_progress AS
SELECT
    t.run_id,
    t.shard_index,
    sh.host,
    sh.state                                        AS shard_state,
    r.max_tasks_per_shard                           AS admission_cap,
    count(*)                                        AS tasks_total,
    count(*) FILTER (WHERE t.state = 'running')     AS tasks_running,
    count(*) FILTER (WHERE t.state = 'done')        AS tasks_done,
    count(*) FILTER (WHERE t.state = 'pending')     AS tasks_pending,
    count(*) FILTER (WHERE t.state = 'failed')      AS tasks_failed,
    COALESCE(sum(t.rows_written), 0)                AS rows_written,
    round(100.0 * count(*) FILTER (WHERE t.state = 'done')
          / NULLIF(count(*), 0), 1)                 AS pct_done
FROM meo_tasks t
JOIN meo_runs r USING (run_id)
LEFT JOIN meo_shards sh ON sh.shard_index = t.shard_index
GROUP BY t.run_id, t.shard_index, sh.host, sh.state, r.max_tasks_per_shard
ORDER BY t.run_id, t.shard_index;


-- Outstanding work, for reduce_finalize.py's completeness gate.
CREATE OR REPLACE VIEW meo_run_gaps AS
SELECT t.run_id, t.shard_index, t.section_id, t.sim_date, t.window_index,
       t.state, t.attempts, t.last_error
FROM meo_tasks t
WHERE t.state <> 'done'
ORDER BY t.run_id, t.shard_index, t.sim_date, t.section_id, t.window_index;


COMMIT;

\echo '02_work_queue.sql complete (coordinator).'
