-- =============================================================================
-- SunlightCity — work queue self-test
--
-- Asserts the coordination semantics that couple the Kubernetes fleet to the
-- database cluster. Self-contained: it builds a synthetic run with its own shards,
-- sections and tasks, so it needs no geometry and no planned run.
--
--   psql -d sunlit_selftest -f distributed/db/01_cluster_topology.sql
--   psql -d sunlit_selftest -f distributed/db/02_work_queue.sql
--   psql -d sunlit_selftest -f distributed/db/tests/queue_selftest.sql
--
-- (distributed/db/tests/run_selftest.sh does this in the right order.)
--
-- WHAT IT PROVES, and why each matters:
--
--   1. LPT ORDERING. The first claim is the most expensive pending task. Getting
--      this backwards would leave the long pole to be picked up last, with the
--      makespan set by one task while the rest of the fleet idles.
--   2. AFFINITY. A worker asking for more of its current (section, window) gets it,
--      in preference to LPT. This is what turns 30,240 collider/geometry working-set
--      loads into 504.
--   3. AFFINITY CANNOT STALL THE QUEUE. When the hinted group is exhausted the
--      claim falls through to LPT rather than returning nothing. Without this, a
--      worker would go idle beside a full queue.
--   4. ADMISSION CONTROL. A shard already running its cap of concurrent tasks stops
--      receiving them. This is the one predicate that stops a burst of retries in
--      one region from pointing thirty workers at one instance and collapsing its
--      throughput while nine peers idle.
--   5. SLOTS ARE RELEASED. Completing a task frees exactly one slot — not zero
--      (which would shrink the cluster's usable concurrency for the rest of the
--      run) and not more.
--   6. FENCING. A worker that has lost its lease cannot heartbeat or complete. This
--      is what makes lease expiry SAFE: without it the original and the replacement
--      would both build the same partition leaf.
--   7. REAPING frees the dead worker's admission slot as well as its task. Missing
--      this would mean every node failure permanently reduced write concurrency.
--   8. RETRIES ARE BOUNDED. A deterministically broken task parks in 'failed'
--      instead of spinning forever holding a worker AND one of its shard's slots.
-- =============================================================================

\set ON_ERROR_STOP on
\pset pager off

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'meo_claim_task') THEN
        RAISE EXCEPTION 'run 01_cluster_topology.sql and 02_work_queue.sql first';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION _t(p_name TEXT, p_ok BOOLEAN, p_detail TEXT DEFAULT NULL)
RETURNS VOID AS $$
BEGIN
    IF p_ok THEN RAISE NOTICE 'PASS  %', p_name;
    ELSE RAISE EXCEPTION 'FAIL  % %', p_name, COALESCE('— ' || p_detail, ''); END IF;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- Fixture: 3 shards, 9 sections, 2 dates, 6 windows = 108 tasks.
--
-- Costs are deliberately uneven — a fully-dark window really does cost ~1 step
-- while a midday one costs 60 — because a uniform fixture would make the LPT
-- assertion vacuous.
-- =============================================================================
DELETE FROM meo_tasks  WHERE run_id = 'qtest';
DELETE FROM meo_runs   WHERE run_id = 'qtest';
DELETE FROM meo_shards WHERE shard_index BETWEEN 90 AND 92;
DELETE FROM meo_sections WHERE section_id BETWEEN 9000 AND 9008;

INSERT INTO meo_shards (shard_index, host, port, dbname, state, vcpu, ram_gb)
SELECT 90 + i, format('qtest-shard-%s', i), 5432, format('qtest_%s', i), 'online', 16, 128
FROM generate_series(0, 2) i;

INSERT INTO meo_sections (section_id, grid_col, grid_row, edge_count, sample_count,
                          hilbert_index, shard_index)
SELECT 9000 + i, i, 0, 10, 4000 + i * 100, i, 90 + (i % 3)
FROM generate_series(0, 8) i;

INSERT INTO meo_runs (run_id, shard_count, section_count, max_tasks_per_shard, config)
VALUES ('qtest', 3, 9, 6, '{}'::JSONB);

INSERT INTO meo_tasks (run_id, section_id, sim_date, window_index,
                       start_minute, end_minute, step_minute, shard_index,
                       est_raycasts, max_attempts)
SELECT 'qtest',
       s.section_id,
       d.sim_date,
       w,
       180 + w * 180,
       360 + w * 180,
       3,
       s.shard_index,
       -- Windows 1..4 are full daylight; 0 and 5 are mostly below the horizon
       -- guard. June is longer than December.
       s.sample_count * CASE
           WHEN w BETWEEN 1 AND 4 THEN 60
           WHEN d.sim_date = DATE '2026-06-21' THEN 25
           ELSE 1 END,
       3
FROM meo_sections s
CROSS JOIN (VALUES (DATE '2026-06-21'), (DATE '2026-12-21')) d(sim_date)
CROSS JOIN generate_series(0, 5) w
WHERE s.section_id BETWEEN 9000 AND 9008;


-- =============================================================================
\echo ''
\echo '--- 1. LPT ordering -------------------------------------------------------'
DO $$
DECLARE t RECORD; v_max BIGINT;
BEGIN
    SELECT max(est_raycasts) INTO v_max
      FROM meo_tasks WHERE run_id = 'qtest' AND state = 'pending';

    SELECT * INTO t FROM meo_claim_task('qtest', 'w-lpt', 900);
    PERFORM _t('the first claim is the dearest pending task',
        (SELECT est_raycasts FROM meo_tasks WHERE task_id = t.task_id) = v_max);
    PERFORM _t('with no hint, affinity_hit is false', t.affinity_hit = FALSE);
    PERFORM meo_complete_task(t.task_id, 'w-lpt', 1, 1);
END $$;


\echo '--- 2. affinity beats LPT -------------------------------------------------'
DO $$
DECLARE t1 RECORD; t2 RECORD;
BEGIN
    -- Pick the CHEAPEST group deliberately: if affinity is working, the hint must
    -- override LPT and hand back another task from this cheap group rather than the
    -- dearest one in the queue.
    SELECT section_id, window_index INTO t1 FROM meo_tasks
      WHERE run_id = 'qtest' AND state = 'pending'
      ORDER BY est_raycasts ASC LIMIT 1;

    SELECT * INTO t2 FROM meo_claim_task('qtest', 'w-aff', 900,
                                         t1.section_id, t1.window_index);
    PERFORM _t('affinity returns the hinted (section, window)',
        t2.section_id = t1.section_id AND t2.window_index = t1.window_index,
        format('asked %s/w%s, got %s/w%s',
               t1.section_id, t1.window_index, t2.section_id, t2.window_index));
    PERFORM _t('affinity_hit is recorded for the monitor', t2.affinity_hit = TRUE);
    PERFORM meo_complete_task(t2.task_id, 'w-aff', 1, 1);
END $$;


\echo '--- 3. affinity cannot stall the queue -----------------------------------'
DO $$
DECLARE t RECORD; v_sec INT; v_win INT;
BEGIN
    SELECT section_id, window_index INTO v_sec, v_win
      FROM meo_tasks WHERE run_id = 'qtest' AND state = 'pending' LIMIT 1;

    -- Exhaust that group entirely, then keep asking for it.
    UPDATE meo_tasks SET state = 'done'
     WHERE run_id = 'qtest' AND section_id = v_sec AND window_index = v_win;

    SELECT * INTO t FROM meo_claim_task('qtest', 'w-fall', 900, v_sec, v_win);
    PERFORM _t('an exhausted hint still yields a task via LPT fallback',
        t.task_id IS NOT NULL);
    PERFORM _t('and it is correctly marked a miss', t.affinity_hit = FALSE);
    PERFORM meo_complete_task(t.task_id, 'w-fall', 1, 1);
END $$;


\echo '--- 4. per-shard admission control ---------------------------------------'
UPDATE meo_runs SET max_tasks_per_shard = 2 WHERE run_id = 'qtest';
-- Leave one shard online so the cap is the only thing limiting claims.
UPDATE meo_shards SET state = 'offline' WHERE shard_index IN (91, 92);
DO $$
DECLARE t RECORD; n INT := 0;
BEGIN
    FOR i IN 1..8 LOOP
        SELECT * INTO t FROM meo_claim_task('qtest', 'w-adm-' || i, 900);
        IF t.task_id IS NOT NULL THEN
            n := n + 1;
            PERFORM _t(format('claim %s went to the only online shard', i),
                       t.shard_index = 90);
        END IF;
    END LOOP;

    PERFORM _t('a cap of 2 admits exactly 2 concurrent tasks', n = 2, n || ' admitted');
    PERFORM _t('and the shard then reports as inadmissible',
        NOT EXISTS (SELECT 1 FROM meo_admissible_shards('qtest')));
END $$;


\echo '--- 5. completion releases exactly one slot ------------------------------'
DO $$
DECLARE t RECORD; v_id BIGINT; v_w TEXT;
BEGIN
    SELECT task_id, worker_id INTO v_id, v_w FROM meo_tasks
      WHERE run_id = 'qtest' AND state = 'running' AND shard_index = 90 LIMIT 1;
    PERFORM meo_complete_task(v_id, v_w, 100, 100);

    PERFORM _t('the shard becomes admissible again',
        (SELECT count(*) FROM meo_admissible_shards('qtest')) = 1);

    SELECT * INTO t FROM meo_claim_task('qtest', 'w-refill', 900);
    PERFORM _t('exactly one more task is admitted', t.task_id IS NOT NULL);
    PERFORM _t('after which the shard is full again',
        NOT EXISTS (SELECT 1 FROM meo_admissible_shards('qtest')));
END $$;


\echo '--- 6. fencing -----------------------------------------------------------'
DO $$
DECLARE v_id BIGINT; v_w TEXT;
BEGIN
    SELECT task_id, worker_id INTO v_id, v_w FROM meo_tasks
      WHERE run_id = 'qtest' AND state = 'running' LIMIT 1;

    PERFORM _t('the lease owner can heartbeat', meo_heartbeat(v_id, v_w, 900, 5));
    PERFORM _t('a worker that lost the lease cannot heartbeat',
        meo_heartbeat(v_id, 'impostor', 900, 5) = FALSE);
    PERFORM _t('nor complete — so it can never write over the new owner',
        meo_complete_task(v_id, 'impostor', 1, 1) = FALSE);
END $$;


\echo '--- 7. reaping frees the task AND the slot -------------------------------'
DO $$
DECLARE v_id BIGINT; n INT;
BEGIN
    SELECT task_id INTO v_id FROM meo_tasks
      WHERE run_id = 'qtest' AND state = 'running' LIMIT 1;
    UPDATE meo_tasks SET lease_expires_at = now() - interval '1 s' WHERE task_id = v_id;

    SELECT count(*) INTO n FROM meo_reap_expired_leases('qtest');
    PERFORM _t('the expired lease is reclaimed', n = 1, n || ' reaped');
    PERFORM _t('the task is claimable again',
        (SELECT state FROM meo_tasks WHERE task_id = v_id) = 'pending');
    PERFORM _t('and the dead worker''s admission slot is released',
        (SELECT count(*) FROM meo_admissible_shards('qtest')) = 1);
END $$;


\echo '--- 8. retries are bounded ------------------------------------------------'
DO $$
DECLARE t RECORD; v_id BIGINT;
BEGIN
    SELECT * INTO t FROM meo_claim_task('qtest', 'w-fail', 900);
    v_id := t.task_id;

    UPDATE meo_tasks SET attempts = max_attempts WHERE task_id = v_id;
    PERFORM meo_fail_task(v_id, 'w-fail', 'deterministically broken');

    PERFORM _t('a task past max_attempts parks in failed',
        (SELECT state FROM meo_tasks WHERE task_id = v_id) = 'failed');
    PERFORM _t('and stops being claimable, freeing the worker and the slot',
        NOT EXISTS (SELECT 1 FROM meo_tasks WHERE task_id = v_id AND state = 'pending'));
END $$;


\echo '--- 9. the lease default is inside its bounds -----------------------------'
-- The bound nobody wrote down. A lease longer than the run means a dead worker is
-- still holding its task when the queue drains: survivors exit on empty polls, the
-- Job reports Complete, and the run is one task short. The previous 900 s default was
-- 8x past the ceiling, so this path was dead code and no test noticed.
--
-- The exact ceiling depends on the run (model.lease_bounds()), which SQL cannot see.
-- What SQL CAN assert is the shape: the default must clear 3 heartbeats and must be
-- far below any plausible run length. plan_tasks.py does the exact check at plan time.
DO $$
DECLARE
    v_default INTEGER;
    v_heartbeat CONSTANT INTEGER := 30;     -- SUNLIT_HEARTBEAT_SECONDS
BEGIN
    SELECT pg_get_function_arg_default(p.oid, 3)::INTEGER INTO v_default
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.proname = 'meo_claim_task' AND n.nspname = current_schema();

    PERFORM _t('meo_claim_task has a lease default at all', v_default IS NOT NULL);
    PERFORM _t('lease default clears the floor (3 x heartbeat)',
        v_default >= 3 * v_heartbeat,
        v_default || 's vs floor ' || (3 * v_heartbeat) || 's');
    PERFORM _t('lease default is far below any plausible run length',
        v_default <= 600,
        v_default || 's -- a lease past the run makes the reaper unable to fire in time');
END $$;


\echo '--- 10. progress views ---------------------------------------------------'
UPDATE meo_shards SET state = 'online' WHERE shard_index BETWEEN 90 AND 92;
SELECT tasks_total, tasks_done, tasks_running, tasks_failed, pct_affinity_hit
FROM meo_run_progress WHERE run_id = 'qtest';

SELECT shard_index, admission_cap, tasks_total, tasks_running, tasks_done
FROM meo_shard_progress WHERE run_id = 'qtest' ORDER BY shard_index;

-- Fixture cleanup: leave the database as it was found.
DELETE FROM meo_tasks    WHERE run_id = 'qtest';
DELETE FROM meo_runs     WHERE run_id = 'qtest';
DELETE FROM meo_sections WHERE section_id BETWEEN 9000 AND 9008;
DELETE FROM meo_shards   WHERE shard_index BETWEEN 90 AND 92;
DROP FUNCTION _t(TEXT, BOOLEAN, TEXT);

\echo ''
\echo '========================================================================='
\echo '  QUEUE SEMANTICS: ALL ASSERTIONS PASSED'
\echo '========================================================================='
