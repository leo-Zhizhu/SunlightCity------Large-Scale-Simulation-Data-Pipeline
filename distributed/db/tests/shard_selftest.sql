-- =============================================================================
-- SunlightCity — shard schema self-test
--
-- Asserts the invariants the pipeline's correctness rests on, against a real
-- PostgreSQL, using synthetic geometry it creates itself. No fleet, no Unity, no
-- city mesh required — it runs in about two seconds.
--
--   docker exec -i pg psql -U admin -d sunlit_selftest -f shard_selftest.sql
--
-- Or, from nothing:
--   createdb sunlit_selftest
--   psql -d sunlit_selftest -c 'CREATE EXTENSION postgis'
--   psql -d sunlit_selftest -f distributed/db/03_shard_schema.sql
--   psql -d sunlit_selftest -f distributed/db/tests/shard_selftest.sql
--
-- Exits non-zero on the first failed assertion, so it works as a CI gate and as
-- step 6 of docs/DEPLOYMENT.md.
--
-- WHAT IT PROVES, and why each one is worth a test:
--
--   1. v1 COMPATIBILITY. meo_exposure_samples and meo_exposure_edges expose
--      exactly v1's columns, in v1's order. Break this and every v1 consumer
--      breaks silently — `SELECT *` would start returning extra columns.
--   2. WINDOWS TILE THE DAY. 6 half-open windows cover [180, 1260) with no gap
--      and no overlap. A gap means timesteps with no partition to land in; an
--      overlap means rows written twice.
--   3. THE WRITE PATH. begin -> COPY -> attach produces an attached leaf whose
--      CHECK constraint implies its partition bounds (so ATTACH skipped
--      validation rather than scanning 261k rows under a lock).
--   4. PRUNING IS THE INDEX. A (section, datetime) query touches ONE leaf. This
--      is the justification for having no index on 7.89 billion rows.
--   5. IDEMPOTENT RETRY. reset -> rebuild leaves no duplicates.
--   6. THE ROLLUP IS EXACT. The derived per-edge sums equal a direct aggregate
--      over the samples.
--   7. DIRECTIONAL ASYMMETRY. Forward and reverse traversal of the same edge at
--      the same instant give different costs. This is the whole reason the
--      sample-level schema is retained, so it is the assertion that matters most.
-- =============================================================================

\set ON_ERROR_STOP on
\pset pager off
\timing off

CREATE EXTENSION IF NOT EXISTS postgis;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'meo_begin_leaf') THEN
        RAISE EXCEPTION 'run 03_shard_schema.sql against this database first';
    END IF;
END $$;

-- One assertion helper, so a failure names itself instead of printing a bare
-- "ERROR: assertion failed".
CREATE OR REPLACE FUNCTION _t(p_name TEXT, p_ok BOOLEAN, p_detail TEXT DEFAULT NULL)
RETURNS VOID AS $$
BEGIN
    IF p_ok THEN
        RAISE NOTICE 'PASS  %', p_name;
    ELSE
        RAISE EXCEPTION 'FAIL  % %', p_name, COALESCE('— ' || p_detail, '');
    END IF;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- Fixture: two 400 m streets in section 0, sampled every 2 m.
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_sample_points') THEN
        CREATE TABLE meo_sample_points (
            id UUID PRIMARY KEY, edge_id UUID, sequence_index INT,
            distance_from_start FLOAT, geom GEOMETRY(PointZ, 0), tree_value FLOAT DEFAULT 0);
    END IF;
END $$;

TRUNCATE meo_sample_points;
TRUNCATE meo_edge_sections;
TRUNCATE meo_shard_sections;

INSERT INTO meo_edge_sections (edge_id, section_id, sample_count) VALUES
    ('11111111-1111-1111-1111-111111111111', 0, 201),
    ('22222222-2222-2222-2222-222222222222', 0, 201);

INSERT INTO meo_sample_points (id, edge_id, sequence_index, distance_from_start, geom)
SELECT md5(format('%s-%s', es.edge_id, i))::uuid, es.edge_id, i, i * 2.0,
       ST_SetSRID(ST_MakePoint(i * 2.0, 100.0, -112.0), 0)
FROM meo_edge_sections es, generate_series(0, 200) i;

SELECT meo_set_shard_identity(0, 10);
SELECT meo_provision_sections(ARRAY[0]);
SELECT meo_provision_edge_partitions(2026, 2026);
UPDATE meo_shard_sections SET sample_count = 402 WHERE section_id = 0;


-- =============================================================================
-- 1. v1 compatibility
-- =============================================================================
\echo ''
\echo '--- 1. v1 schema compatibility -------------------------------------------'
DO $$
DECLARE v_cols TEXT;
BEGIN
    SELECT string_agg(column_name, ',' ORDER BY ordinal_position) INTO v_cols
    FROM information_schema.columns WHERE table_name = 'meo_exposure_samples';
    PERFORM _t('meo_exposure_samples exposes v1 columns in v1 order',
               v_cols = 'sample_point_id,datetime,is_sunlit', 'got: ' || v_cols);

    SELECT string_agg(column_name, ',' ORDER BY ordinal_position) INTO v_cols
    FROM information_schema.columns WHERE table_name = 'meo_exposure_edges';
    PERFORM _t('meo_exposure_edges exposes v1 columns in v1 order',
               v_cols = 'edge_id,datetime,sunlit_sum', 'got: ' || v_cols);
END $$;


-- =============================================================================
-- 2. Windows tile the day
-- =============================================================================
\echo '--- 2. window tiling -----------------------------------------------------'
DO $$
DECLARE
    v_gaps INTEGER;
    v_span INTEGER;
BEGIN
    -- Every window's hi must equal the next window's lo: no gap, no overlap.
    SELECT count(*) INTO v_gaps
    FROM generate_series(0, 4) w,
         LATERAL meo_window_bounds(DATE '2026-06-15', w) a,
         LATERAL meo_window_bounds(DATE '2026-06-15', w + 1) b
    WHERE a.hi <> b.lo;
    PERFORM _t('6 windows tile [03:00,21:00) with no gap or overlap', v_gaps = 0,
               v_gaps || ' boundary mismatches');

    -- 6 windows x 60 steps = 360, the figure every capacity calculation uses.
    SELECT sum((last_minute - first_minute) / 3 + 1)::INTEGER INTO v_span
    FROM generate_series(0, 5) w, LATERAL meo_window_bounds(DATE '2026-06-15', w);
    PERFORM _t('windows contain 360 timesteps in total', v_span = 360,
               'got ' || v_span);

    -- A window count that does not divide the span must be refused, not rounded.
    BEGIN
        PERFORM * FROM meo_window_bounds(DATE '2026-06-15', 0, 180, 1260, 7);
        PERFORM _t('uneven window count rejected', FALSE, '7 windows was accepted');
    EXCEPTION WHEN others THEN
        PERFORM _t('uneven window count rejected', SQLERRM LIKE '%tile the day exactly%',
                   SQLERRM);
    END;
END $$;


-- =============================================================================
-- 3. The write path
--
-- A sweeping shadow boundary: the shade edge crosses the street over the window,
-- as it does when the sun rotates. Symmetric test data would hide exactly the
-- asymmetry test 7 exists to detect.
-- =============================================================================
\echo '--- 3. write path: begin -> COPY -> attach -------------------------------'
BEGIN;
SELECT meo_begin_leaf(0, lo, hi, 4, 4242) AS leaf
FROM meo_window_bounds(DATE '2026-06-15', 4) \gset

INSERT INTO :"leaf" (sample_point_id, datetime, is_sunlit, section_id, task_id)
SELECT sp.id,
       (DATE '2026-06-15')::timestamp + make_interval(mins => 900 + 3 * k),
       sp.distance_from_start > 400.0 * (k / 59.0),
       0, 4242
FROM meo_sample_points sp, generate_series(0, 59) k;

SELECT meo_attach_leaf(0, lo, hi, 4) AS attached
FROM meo_window_bounds(DATE '2026-06-15', 4) \gset
COMMIT;

DO $$
DECLARE
    v_leaf TEXT := meo_leaf_name(0, TIMESTAMP '2026-06-15 15:00:00', 4);
    v_ok   BOOLEAN;
    v_n    BIGINT;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                   WHERE c.relname = v_leaf) INTO v_ok;
    PERFORM _t('leaf is attached to its section parent', v_ok, v_leaf);

    -- Without a CHECK implying the bounds, ATTACH sequential-scans the leaf while
    -- holding a lock on the parent. Its presence is what makes attach O(1).
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = v_leaf::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%section_id = 0%'
          AND pg_get_constraintdef(oid) LIKE '%2026-06-15 15:00:00%'
    ) INTO v_ok;
    PERFORM _t('leaf CHECK implies partition bounds (ATTACH skipped validation)', v_ok);

    -- 402 samples x 60 steps
    SELECT count(*) INTO v_n FROM meo_exposure_samples;
    PERFORM _t('row count = samples x steps', v_n = 402 * 60, 'got ' || v_n);
END $$;


-- =============================================================================
-- 4. Pruning is the index
-- =============================================================================
\echo '--- 4. partition pruning -------------------------------------------------'
DO $$
DECLARE
    v_rec   RECORD;
    v_scans INTEGER := 0;
    v_plan  TEXT := '';
BEGIN
    -- The claim under test: a (section, datetime) query reads ONE relation, which
    -- is why 7.89 billion rows need no index.
    --
    -- Iterated rather than `EXECUTE ... INTO`: EXPLAIN returns the plan as one row
    -- PER LINE, and INTO would silently capture only the first — which is how this
    -- test initially "found" zero scan nodes in a plan that plainly had one.
    FOR v_rec IN
        EXECUTE 'EXPLAIN (COSTS OFF) SELECT count(*) FROM meo_exposure_samples_p '
                'WHERE section_id = 0 AND datetime = ''2026-06-15 15:00:00'''
    LOOP
        v_plan := v_plan || v_rec."QUERY PLAN" || E'\n';
        IF v_rec."QUERY PLAN" LIKE '%Scan on%' THEN
            v_scans := v_scans + 1;
        END IF;
    END LOOP;

    PERFORM _t('a (section, datetime) query scans exactly one relation',
               v_scans = 1, v_scans || ' scan nodes in plan: ' || v_plan);

    -- And that relation is the specific leaf, not the parent scanned with a filter.
    PERFORM _t('the scanned relation is the (section, window) leaf',
               v_plan LIKE '%' || meo_leaf_name(0, TIMESTAMP '2026-06-15 15:00:00', 4) || '%',
               v_plan);
END $$;


-- =============================================================================
-- 5. Idempotent retry
-- =============================================================================
\echo '--- 5. idempotent retry --------------------------------------------------'
DO $$
DECLARE v_before BIGINT; v_after BIGINT;
BEGIN
    SELECT count(*) INTO v_before FROM meo_exposure_samples;

    PERFORM meo_reset_leaf(0, lo, 4) FROM meo_window_bounds(DATE '2026-06-15', 4);
    PERFORM _t('reset removed the leaf',
               (SELECT count(*) FROM meo_exposure_samples) = 0);

    -- Rebuild identically.
    DECLARE v_leaf TEXT;
    BEGIN
        SELECT meo_begin_leaf(0, lo, hi, 4, 4243) INTO v_leaf
        FROM meo_window_bounds(DATE '2026-06-15', 4);
        EXECUTE format($q$
            INSERT INTO %I (sample_point_id, datetime, is_sunlit, section_id, task_id)
            SELECT sp.id,
                   (DATE '2026-06-15')::timestamp + make_interval(mins => 900 + 3 * k),
                   sp.distance_from_start > 400.0 * (k / 59.0), 0, 4243
            FROM meo_sample_points sp, generate_series(0, 59) k $q$, v_leaf);
        PERFORM meo_attach_leaf(0, lo, hi, 4) FROM meo_window_bounds(DATE '2026-06-15', 4);
    END;

    SELECT count(*) INTO v_after FROM meo_exposure_samples;
    PERFORM _t('retry replaces rather than duplicates', v_after = v_before,
               v_before || ' -> ' || v_after);
END $$;


-- =============================================================================
-- 6. The rollup is exact
-- =============================================================================
\echo '--- 6. edge rollup exactness --------------------------------------------'
DO $$
DECLARE v_rollup BIGINT; v_direct BIGINT; v_bad BIGINT;
BEGIN
    PERFORM meo_rollup_edges(0);

    SELECT sum(sunlit_sum) INTO v_rollup FROM meo_exposure_edges_p;
    SELECT count(*) INTO v_direct FROM meo_exposure_samples WHERE is_sunlit;
    PERFORM _t('rollup total equals a direct aggregate over samples',
               v_rollup = v_direct, v_rollup || ' vs ' || v_direct);

    SELECT count(*) INTO v_bad FROM meo_exposure_edges_p WHERE sunlit_sum > sample_count;
    PERFORM _t('sunlit_sum <= sample_count everywhere', v_bad = 0, v_bad || ' violations');

    -- Re-running must correct, not duplicate.
    SELECT count(*) INTO v_direct FROM meo_exposure_edges_p;
    PERFORM meo_rollup_edges(0);
    SELECT count(*) INTO v_rollup FROM meo_exposure_edges_p;
    PERFORM _t('rollup is idempotent', v_rollup = v_direct,
               v_direct || ' -> ' || v_rollup);
END $$;


-- =============================================================================
-- 7. DIRECTIONAL ASYMMETRY — the assertion the schema exists for.
--
-- Same edge, same instant, opposite directions. If a per-edge sum were sufficient
-- these would be identical; they are not, and this test fails loudly if a future
-- change ever makes them so.
-- =============================================================================
\echo '--- 7. directional asymmetry --------------------------------------------'
DO $$
DECLARE
    f RECORD;
    r RECORD;
BEGIN
    SELECT * INTO f FROM meo_edge_directional_cost(
        '11111111-1111-1111-1111-111111111111', '2026-06-15 16:00:00', FALSE, 0.5);
    SELECT * INTO r FROM meo_edge_directional_cost(
        '11111111-1111-1111-1111-111111111111', '2026-06-15 16:00:00', TRUE, 0.5);

    PERFORM _t('both directions read every sample',
               f.samples = 201 AND r.samples = 201,
               format('%s / %s', f.samples, r.samples));

    PERFORM _t('traversal spans several timesteps (the walker moves through time)',
               f.timesteps_spanned > 1, f.timesteps_spanned::TEXT);

    -- The headline: sun exposure depends on which way you walk.
    --
    -- Note format() takes %s/%I/%L only — it is not printf, and %.1f raises
    -- "unrecognized format() type specifier". Rounding happens in round(), not in
    -- the format string.
    PERFORM _t('sun_seconds differs by direction',
               f.sun_seconds <> r.sun_seconds,
               format('fwd %ss, rev %ss — a per-edge sum could not tell these apart',
                      round(f.sun_seconds::NUMERIC, 1), round(r.sun_seconds::NUMERIC, 1)));

    -- Entering sun and leaving shade is not the same walk as the reverse, even
    -- when the totals happen to be close.
    PERFORM _t('entry/exit state is inverted between directions',
               f.entered_in_sun = r.exited_in_sun AND f.exited_in_sun = r.entered_in_sun,
               format('fwd %s->%s, rev %s->%s',
                      f.entered_in_sun, f.exited_in_sun, r.entered_in_sun, r.exited_in_sun));

    PERFORM _t('longest continuous sun run is reported and non-zero',
               f.longest_sun_run_m > 0 AND f.longest_sun_run_m <= f.edge_length_m,
               format('%s m of %s m', round(f.longest_sun_run_m::NUMERIC),
                      round(f.edge_length_m::NUMERIC)));

    -- The percent sign is appended to the argument rather than written in the
    -- format string: RAISE scans left to right, so '%%%' reads as a literal '%'
    -- followed by a placeholder and prints "%62.69" instead of "62.69%".
    RAISE NOTICE '';
    RAISE NOTICE '      forward: % s sun, % s shade, % sun, entered_in_sun=%, longest run % m',
        round(f.sun_seconds::NUMERIC, 1), round(f.shade_seconds::NUMERIC, 1),
        f.pct_sun || '%', f.entered_in_sun, round(f.longest_sun_run_m::NUMERIC);
    RAISE NOTICE '      reverse: % s sun, % s shade, % sun, entered_in_sun=%, longest run % m',
        round(r.sun_seconds::NUMERIC, 1), round(r.shade_seconds::NUMERIC, 1),
        r.pct_sun || '%', r.entered_in_sun, round(r.longest_sun_run_m::NUMERIC);
END $$;


-- =============================================================================
-- 8. Housekeeping: an unattached leaf is swept.
-- =============================================================================
\echo '--- 8. orphan sweep -----------------------------------------------------'
BEGIN;
SELECT meo_begin_leaf(0, lo, hi, 0, 9999) FROM meo_window_bounds(DATE '2026-06-15', 0);
COMMIT;   -- never attached, exactly as a worker killed mid-task would leave it

DO $$
DECLARE v_n INTEGER;
BEGIN
    SELECT meo_drop_orphan_leaves() INTO v_n;
    PERFORM _t('orphaned (unattached) leaf is dropped', v_n = 1, v_n || ' dropped');
    PERFORM _t('attached leaves survive the sweep',
               (SELECT count(*) FROM meo_exposure_samples) = 402 * 60);
END $$;


\echo ''
\echo '========================================================================='
\echo '  ALL ASSERTIONS PASSED'
\echo '========================================================================='
DROP FUNCTION _t(TEXT, BOOLEAN, TEXT);
