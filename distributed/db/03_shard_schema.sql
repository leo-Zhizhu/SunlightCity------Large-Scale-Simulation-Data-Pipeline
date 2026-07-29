-- =============================================================================
-- SunlightCity — shard schema (phase 3 of 6)          ***  DATA SHARD ONLY  ***
--
-- Run against EACH of the ten data shards. Nothing here goes on the coordinator.
--
--
-- THE SCHEMA IS THE V1 SCHEMA
-- ---------------------------
-- meo_exposure_samples keeps exactly the columns v1 gave it:
--
--     sample_point_id UUID, datetime TIMESTAMP, is_sunlit BOOLEAN
--
-- and every consumer written against v1 — SampleVisualization.cs,
-- plot_annual_exposure.py, db_sanity_checks.py, the Pareto router — keeps working
-- unchanged, because the name meo_exposure_samples still resolves to those three
-- columns and nothing else. The physical table adds two columns behind a view:
-- section_id (partition routing) and task_id (provenance). Neither is data; both
-- are addressing.
--
-- This is not conservatism. The per-sample series is the product:
--
--     The router treats an edge as DIRECTED. Walking a street eastward at 14:00
--     may mean 40 m of shade then 360 m of full sun; walking the same street
--     westward at 14:00 means 360 m of sun then 40 m of shade — and the second is
--     materially worse, because the pedestrian is already hot when they reach the
--     shade. Same edge, same instant, same total sunlit count. Different cost.
--
-- A per-edge sum cannot distinguish them. So sunlit_sum is a DERIVED convenience
-- index computed in the reduce phase (see 05), not the output, and
-- meo_edge_directional_cost() at the bottom of this file is what downstream
-- actually calls.
--
--
-- WHAT ONE ROW IS, PRECISELY
-- --------------------------
-- The table is FULLY NORMALISED — long and narrow, not wide. One row is ONE
-- (sample point, timestep) observation and it carries ONE BIT of information:
--
--     ('a1b2…'::uuid, '2026-06-15 15:00:00', true, 384, 91027)
--
-- There is no array, no column per timestep, nothing packed. So the row count IS
-- the observation count, by construction:
--
--     365,133 sample points x 360 timesteps x 60 dates = 7,886,872,800 rows
--
-- (v1's 12 dates gave 1,577,374,560 of the same rows.) That identity is worth
-- stating: "N billion observations" and "N billion rows" being the same number is a
-- property of THIS encoding rather than an inevitability.
--
-- The encoding is expensive: 68 bytes on the page to carry one bit, which is 0.18%
-- payload efficiency. A packed alternative — one row per (sample point, date)
-- holding a 360-bit bitmap — would be 21.9M rows and about 2 GB instead of 500 GB,
-- roughly 245x smaller, and the whole dataset would fit on a single instance.
--
-- It is not used because the v1 column set is a hard requirement and every v1
-- consumer selects those three columns by name. That is a real cost knowingly
-- accepted, not an oversight. Note the compatibility VIEWS below are what would make
-- a packed encoding introducible later without breaking any consumer: the view keeps
-- promising three columns while the storage underneath changes shape.
--
--
-- THE PARTITION SHAPE, AND WHY IT IS EXACTLY THIS
-- ----------------------------------------------
--     meo_exposure_samples_p
--       PARTITION BY LIST (section_id)          <- which square kilometre
--         └─ meo_exp_s<section>
--              PARTITION BY RANGE (datetime)    <- which date and 3 h window
--                   └─ meo_exp_s<section>_<yyyymmdd>_w<window>     ONE TASK
--
-- One task produces exactly one leaf relation. That single fact buys four things
-- at once:
--
--   1. NO EXTENSION-LOCK CONTENTION. Concurrent COPY into one heap serialises on
--      the relation extension lock — every backend needing a new 8 KB page queues
--      on the same lock, and that is THE bottleneck for parallel bulk load. Here
--      each writer extends a relation nobody else can see.
--
--   2. NO WAL AT ALL for the sample data. COPY into a relation created in the
--      SAME transaction skips WAL entirely under wal_level=minimal. Because the
--      leaf is built from scratch per task rather than appended to a pre-existing
--      partition, all ~500 GB is written without a WAL record. See 04.
--
--   3. COPY ... FREEZE is legal, for the same reason (the relation is new in this
--      transaction). Tuples land already frozen, so there is no hint-bit write on
--      first read and no freeze-vacuum of 7.89 billion rows to pay later.
--
--   4. IDEMPOTENT RETRY WITHOUT A DELETE. Replacing a task's output is DETACH +
--      DROP + rebuild, which is catalog work. The alternative — DELETE ... WHERE
--      task_id = N over 261k rows — would generate WAL, bloat, and vacuum debt on
--      every retry.
--
-- Leaf count per shard: ~8 sections x 60 dates x 6 windows = ~3,024, each ~261k
-- rows / ~17 MB.
--
-- Three thousand relations on one instance is a lot, and the TWO-LEVEL tree is what
-- keeps it cheap. Pruning resolves the LIST level over ~8 section values, then the
-- RANGE level over the ~360 datetime bounds inside the one section it selected —
-- never 3,024 bounds in a single flat list. A one-level design keyed on datetime
-- alone would have to consider every leaf on the instance for every query, and would
-- also give a task no relation of its own to COPY into, forfeiting all four
-- properties above.
--
-- Idempotent: safe to re-run.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Identity and guards.
--
-- A shard asserts which shard it is. Every orchestrator script checks this before
-- writing, because "ran 05_post_load_indexes against shard 3 twice and shard 7
-- never" is otherwise a silent, expensive mistake.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_shard_identity (
    lock_id     BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (lock_id),
    shard_index INTEGER NOT NULL,
    shard_count INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION meo_set_shard_identity(p_index INTEGER, p_count INTEGER)
RETURNS VOID AS $$
BEGIN
    INSERT INTO meo_shard_identity (lock_id, shard_index, shard_count)
    VALUES (TRUE, p_index, p_count)
    ON CONFLICT (lock_id) DO UPDATE
       SET shard_index = EXCLUDED.shard_index,
           shard_count = EXCLUDED.shard_count;
END;
$$ LANGUAGE plpgsql;


DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name = 'meo_sample_points') THEN
        RAISE EXCEPTION
            'meo_sample_points not found on this shard. Replicate the static geometry '
            '(waypoints, edges, sample_points, trees) from the coordinator first — '
            'see docs/DEPLOYMENT.md step 4.';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 1. Edge -> section map, replicated from the coordinator.
--
-- Every shard gets the FULL map, not only its own sections. It is 6,700 rows, and
-- holding all of it means a shard can answer "is this edge mine?" without a
-- round trip — which the directional query path needs on every call.
--
-- Same reasoning for the static geometry: 140 MB replicated everywhere is
-- cheaper than any scheme for fetching it, and it means reassigning a section
-- between shards moves exposure rows only, never geometry.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_edge_sections (
    edge_id      UUID    PRIMARY KEY,
    section_id   INTEGER NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_meo_edge_sections_section
    ON meo_edge_sections (section_id);


-- Which sections this shard is responsible for. Drives leaf provisioning and the
-- completeness check in 05.
CREATE TABLE IF NOT EXISTS meo_shard_sections (
    section_id   INTEGER PRIMARY KEY,
    sample_count INTEGER NOT NULL DEFAULT 0
);


-- -----------------------------------------------------------------------------
-- 2. Sample-level exposure — the PRIMARY output. v1's three columns, plus two
--    for addressing.
--
-- No PRIMARY KEY and no index, deliberately. A unique index over 7.89 billion
-- rows would dominate insert cost, cost ~300 GB, and buy nothing: the only lookups
-- are "this edge's samples at this timestamp", and partition pruning already
-- narrows that to one ~261k-row leaf that is cheaper to scan sequentially than to
-- descend a B-tree for. Pruning IS the index here.
--
-- Uniqueness is instead structural: a (sample_point, timestamp) pair belongs to
-- exactly one (section, date, window), so exactly one leaf, and a leaf is
-- rebuilt whole rather than appended to.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_exposure_samples_p (
    sample_point_id UUID      NOT NULL,
    datetime        TIMESTAMP NOT NULL,
    is_sunlit       BOOLEAN   NOT NULL,
    section_id      INTEGER   NOT NULL,
    task_id         BIGINT    NOT NULL
) PARTITION BY LIST (section_id);


-- -----------------------------------------------------------------------------
-- 3. Edge-level exposure — DERIVED in the reduce phase, not written by workers.
--
-- v1's three columns plus sample_count (so a consumer can compute a fraction
-- without joining meo_edges) and section_id (pruning). Monthly RANGE partitions:
-- 12 per year keeps planning cheap while still letting a month be retired with
-- DROP instead of a 2.4M-row DELETE.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_exposure_edges_p (
    edge_id      UUID      NOT NULL,
    datetime     TIMESTAMP NOT NULL,
    sunlit_sum   INTEGER   NOT NULL,
    sample_count INTEGER   NOT NULL,
    section_id   INTEGER   NOT NULL,
    PRIMARY KEY (edge_id, datetime)
) PARTITION BY RANGE (datetime);


-- -----------------------------------------------------------------------------
-- 4. v1 COMPATIBILITY VIEWS.
--
-- These are the contract. Their column lists are byte-for-byte what
-- db_pipeline_initializer.py created, in the same order, so `SELECT *` behaves
-- identically and every v1 consumer is unaffected by the partitioning underneath.
--
-- Created only if the v1 tables are absent, so a shard that somehow holds real
-- v1 tables is never shadowed by a view of the same name.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_exposure_samples') THEN
        CREATE VIEW meo_exposure_samples AS
            SELECT sample_point_id, datetime, is_sunlit FROM meo_exposure_samples_p;
        RAISE NOTICE 'created v1-compatible view meo_exposure_samples';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_exposure_edges') THEN
        CREATE VIEW meo_exposure_edges AS
            SELECT edge_id, datetime, sunlit_sum FROM meo_exposure_edges_p;
        RAISE NOTICE 'created v1-compatible view meo_exposure_edges';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 5. Window arithmetic.
--
-- HALF-OPEN intervals, [start, end), and this is a deliberate correction to v1.
-- v1's export loop ran `for minute = 180; minute <= 1260; minute += 3`, which is
-- 361 steps and includes 21:00 — so tiling the day into 3 h windows with an
-- inclusive endpoint would have written 21:00 twice, once as the end of window 5
-- and once as the start of a window 6 that does not exist.
--
-- Half-open windows tile [180, 1260) exactly: 6 windows x 60 steps = 360 steps,
-- which is also the figure every capacity calculation uses. It costs one timestep
-- at the very end of the day, when the sun is below the horizon guard anyway and
-- every sample would have been recorded as shadowed.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_window_bounds(
    p_sim_date     DATE,
    p_window_index INTEGER,
    p_start_minute INTEGER DEFAULT 180,
    p_end_minute   INTEGER DEFAULT 1260,
    p_windows      INTEGER DEFAULT 6
) RETURNS TABLE (lo TIMESTAMP, hi TIMESTAMP, first_minute INTEGER, last_minute INTEGER) AS $$
DECLARE
    v_span INTEGER := (p_end_minute - p_start_minute) / p_windows;
    v_lo   INTEGER := p_start_minute + p_window_index * v_span;
BEGIN
    IF p_window_index < 0 OR p_window_index >= p_windows THEN
        RAISE EXCEPTION 'window_index % outside 0..%', p_window_index, p_windows - 1;
    END IF;
    IF (p_end_minute - p_start_minute) % p_windows <> 0 THEN
        RAISE EXCEPTION
            'window count % does not divide the % minute simulation span evenly; '
            'windows must tile the day exactly or timesteps fall between partitions',
            p_windows, p_end_minute - p_start_minute;
    END IF;

    RETURN QUERY SELECT
        p_sim_date::TIMESTAMP + make_interval(mins => v_lo),
        p_sim_date::TIMESTAMP + make_interval(mins => v_lo + v_span),
        v_lo,
        v_lo + v_span - 1;
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- Deterministic relation names. Every component that touches a leaf derives its
-- name from this one function, so a rename is a one-line change.
CREATE OR REPLACE FUNCTION meo_leaf_name(p_section_id INTEGER, p_lo TIMESTAMP,
                                         p_window_index INTEGER)
RETURNS TEXT AS $$
    SELECT format('meo_exp_s%s_%s_w%s', p_section_id,
                  to_char(p_lo, 'YYYYMMDD'), p_window_index);
$$ LANGUAGE sql IMMUTABLE STRICT;

CREATE OR REPLACE FUNCTION meo_section_parent_name(p_section_id INTEGER)
RETURNS TEXT AS $$
    SELECT format('meo_exp_s%s', p_section_id);
$$ LANGUAGE sql IMMUTABLE STRICT;


-- -----------------------------------------------------------------------------
-- 6. Provisioning.
--
-- Section-level parents are created up front by the orchestrator. Leaves are NOT:
-- each task builds its own, which is the whole point (see the header).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_provision_sections(p_sections INTEGER[])
RETURNS INTEGER AS $$
DECLARE
    v_sid    INTEGER;
    v_parent TEXT;
    v_made   INTEGER := 0;
BEGIN
    FOREACH v_sid IN ARRAY p_sections LOOP
        v_parent := meo_section_parent_name(v_sid);

        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_parent) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF meo_exposure_samples_p '
                'FOR VALUES IN (%s) PARTITION BY RANGE (datetime)',
                v_parent, v_sid);
            v_made := v_made + 1;
        END IF;

        INSERT INTO meo_shard_sections (section_id, sample_count)
        VALUES (v_sid, 0) ON CONFLICT (section_id) DO NOTHING;
    END LOOP;

    RETURN v_made;
END;
$$ LANGUAGE plpgsql;


-- Monthly partitions for the derived edge table. No DEFAULT partition on purpose:
-- a row whose datetime falls outside every partition should fail loudly rather
-- than land in a catch-all that then blocks future ATTACH operations.
CREATE OR REPLACE FUNCTION meo_provision_edge_partitions(p_start_year INTEGER,
                                                         p_end_year INTEGER)
RETURNS INTEGER AS $$
DECLARE
    y     INTEGER;
    m     INTEGER;
    v_lo  DATE;
    v_hi  DATE;
    v_ch  TEXT;
    v_made INTEGER := 0;
BEGIN
    FOR y IN p_start_year..p_end_year LOOP
        FOR m IN 1..12 LOOP
            v_lo := make_date(y, m, 1);
            v_hi := (v_lo + INTERVAL '1 month')::DATE;
            v_ch := format('meo_exposure_edges_p_%s_%s', y, lpad(m::TEXT, 2, '0'));

            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_ch) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF meo_exposure_edges_p '
                    'FOR VALUES FROM (%L) TO (%L)', v_ch, v_lo, v_hi);
                v_made := v_made + 1;
            END IF;
        END LOOP;
    END LOOP;

    RETURN v_made;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 7. Leaf lifecycle — the write path.
--
-- The worker's critical section is exactly:
--
--     BEGIN;
--       SELECT meo_begin_leaf(section, lo, hi, window, task);   -- returns name
--       COPY <name> (sample_point_id, datetime, is_sunlit, section_id, task_id)
--            FROM STDIN (FORMAT binary, FREEZE);
--       SELECT meo_attach_leaf(section, lo, hi, window);
--     COMMIT;
--
-- LOCK DISCIPLINE, which is the part that is easy to get wrong:
--
--   * meo_begin_leaf creates a STANDALONE table. It takes NO lock on any parent,
--     so the long COPY that follows blocks nobody. This is why the detach path is
--     separate: folding DETACH into begin_leaf would hold ACCESS EXCLUSIVE on the
--     section parent for the whole minutes-long transaction, serialising every
--     other worker touching that section.
--
--   * meo_reset_leaf does the DETACH + DROP, in its OWN short transaction, and is
--     called only when attempts > 1. First attempts never pay for it.
--
--   * meo_attach_leaf takes SHARE UPDATE EXCLUSIVE on the parent for milliseconds.
--     It skips validation because the leaf carries a CHECK constraint that already
--     implies the partition bounds — without that constraint ATTACH would
--     sequential-scan 261k rows while holding the lock.
--
-- Until COMMIT the rows live in an unattached relation, invisible through the
-- parent. So a task's output appears atomically or not at all, with no partially
-- visible intermediate state for a reader to trip over.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_begin_leaf(
    p_section_id   INTEGER,
    p_lo           TIMESTAMP,
    p_hi           TIMESTAMP,
    p_window_index INTEGER,
    p_task_id      BIGINT
) RETURNS TEXT AS $$
DECLARE
    v_leaf   TEXT := meo_leaf_name(p_section_id, p_lo, p_window_index);
    v_parent TEXT := meo_section_parent_name(p_section_id);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_parent) THEN
        RAISE EXCEPTION
            'section parent %I does not exist. Call meo_provision_sections() for '
            'this shard''s sections before starting the fleet.', v_parent;
    END IF;

    -- A leftover from a worker killed between CREATE and ATTACH. Unattached, so
    -- dropping it needs no parent lock.
    IF EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.relname = v_leaf
          AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)
    ) THEN
        EXECUTE format('DROP TABLE %I', v_leaf);
    END IF;

    -- LIKE the top parent so column order and types match exactly; ATTACH rejects
    -- any divergence. The CHECK constraint is what makes ATTACH validation-free.
    EXECUTE format($fmt$
        CREATE TABLE %I (
            LIKE meo_exposure_samples_p INCLUDING DEFAULTS,
            CONSTRAINT %I CHECK (
                section_id = %s AND datetime >= %L AND datetime < %L)
        ) WITH (fillfactor = 100, autovacuum_enabled = off)
    $fmt$, v_leaf, v_leaf || '_bounds', p_section_id, p_lo, p_hi);

    RETURN v_leaf;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION meo_attach_leaf(
    p_section_id   INTEGER,
    p_lo           TIMESTAMP,
    p_hi           TIMESTAMP,
    p_window_index INTEGER
) RETURNS BIGINT AS $$
DECLARE
    v_leaf   TEXT := meo_leaf_name(p_section_id, p_lo, p_window_index);
    v_parent TEXT := meo_section_parent_name(p_section_id);
    v_rows   BIGINT;
BEGIN
    EXECUTE format('SELECT count(*) FROM %I', v_leaf) INTO v_rows;

    EXECUTE format(
        'ALTER TABLE %I ATTACH PARTITION %I FOR VALUES FROM (%L) TO (%L)',
        v_parent, v_leaf, p_lo, p_hi);

    RETURN v_rows;
END;
$$ LANGUAGE plpgsql;


-- Idempotent replace. Its own transaction, called only on retry.
CREATE OR REPLACE FUNCTION meo_reset_leaf(
    p_section_id   INTEGER,
    p_lo           TIMESTAMP,
    p_window_index INTEGER
) RETURNS BOOLEAN AS $$
DECLARE
    v_leaf   TEXT := meo_leaf_name(p_section_id, p_lo, p_window_index);
    v_parent TEXT := meo_section_parent_name(p_section_id);
    v_attached BOOLEAN;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_leaf) THEN
        RETURN FALSE;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE c.relname = v_leaf
    ) INTO v_attached;

    IF v_attached THEN
        EXECUTE format('ALTER TABLE %I DETACH PARTITION %I', v_parent, v_leaf);
    END IF;

    EXECUTE format('DROP TABLE %I', v_leaf);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;


-- Sweeps unattached leaves left by workers killed mid-task. They hold real disk,
-- unlike the UNLOGGED staging tables of an earlier design, so this matters:
-- called by the reaper alongside lease reclamation.
CREATE OR REPLACE FUNCTION meo_drop_orphan_leaves()
RETURNS INTEGER AS $$
DECLARE
    t       TEXT;
    dropped INTEGER := 0;
BEGIN
    FOR t IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND c.relname LIKE 'meo\_exp\_s%\_w%'
          AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I', t);
        dropped := dropped + 1;
    END LOOP;
    RETURN dropped;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- 8. THE DIRECTIONAL QUERY API — what the schema exists for.
--
-- Everything above is plumbing to keep these two functions answerable.
-- =============================================================================

-- Snaps a wall-clock instant onto the simulated 3-minute grid.
--
-- A pedestrian arrives at a sample at an arbitrary time; exposure was measured at
-- 3-minute steps. Nearest-step is the right rule rather than floor: at 1.35 m/s a
-- sample is crossed every 1.5 s, so rounding error is bounded by 90 s of sun
-- either way, and biasing consistently downward would systematically report the
-- morning's exposure for the whole traverse.
--
-- Times outside the simulated window clamp to its ends. That is correct rather
-- than merely convenient: outside 03:00-21:00 the sun is below the horizon guard,
-- so the boundary steps are already all-shadow.
CREATE OR REPLACE FUNCTION meo_snap_timestep(
    p_ts           TIMESTAMP,
    p_start_minute INTEGER DEFAULT 180,
    p_end_minute   INTEGER DEFAULT 1260,
    p_step_minute  INTEGER DEFAULT 3
) RETURNS TIMESTAMP AS $$
DECLARE
    v_day   TIMESTAMP := date_trunc('day', p_ts);
    v_min   DOUBLE PRECISION := EXTRACT(EPOCH FROM (p_ts - date_trunc('day', p_ts))) / 60.0;
    v_steps INTEGER := (p_end_minute - p_start_minute) / p_step_minute;
    v_k     INTEGER;
BEGIN
    v_k := round((v_min - p_start_minute) / p_step_minute)::INTEGER;
    -- v_steps - 1 because the window is half-open: the last measured step is
    -- start + (steps-1)*step, i.e. 20:57 rather than 21:00.
    v_k := greatest(0, least(v_steps - 1, v_k));
    RETURN v_day + make_interval(mins => p_start_minute + v_k * p_step_minute);
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- -----------------------------------------------------------------------------
-- The per-sample series for one edge at one instant, in traversal order.
--
-- p_reverse flips the order AND recomputes distance from the other endpoint, so
-- the caller always reads a monotonically increasing "metres travelled". Handing
-- back a reversed sequence_index and letting the caller re-derive distance is how
-- direction bugs get written.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_edge_profile(
    p_edge_id  UUID,
    p_datetime TIMESTAMP,
    p_reverse  BOOLEAN DEFAULT FALSE
) RETURNS TABLE (
    step_index      INTEGER,
    metres_from_entry DOUBLE PRECISION,
    is_sunlit       BOOLEAN
) AS $$
DECLARE
    v_section INTEGER;
    v_length  DOUBLE PRECISION;
BEGIN
    -- Resolved into a constant first so the LIST partition is pruned at PLAN time.
    -- Left as a join predicate it would rely on runtime pruning, which works but
    -- costs a per-execution re-plan of the subtree.
    SELECT es.section_id INTO v_section
      FROM meo_edge_sections es WHERE es.edge_id = p_edge_id;

    IF v_section IS NULL THEN
        RAISE EXCEPTION
            'edge % is not in meo_edge_sections. Either the map is stale on this '
            'shard, or this edge belongs to another shard.', p_edge_id;
    END IF;

    SELECT max(sp.distance_from_start) INTO v_length
      FROM meo_sample_points sp WHERE sp.edge_id = p_edge_id;

    RETURN QUERY
    SELECT (row_number() OVER (ORDER BY d.travelled))::INTEGER - 1,
           d.travelled,
           d.lit
    FROM (
        SELECT CASE WHEN p_reverse THEN v_length - sp.distance_from_start
                    ELSE sp.distance_from_start END AS travelled,
               e.is_sunlit AS lit
        FROM meo_sample_points sp
        JOIN meo_exposure_samples_p e
          ON e.sample_point_id = sp.id
         AND e.datetime   = p_datetime
         AND e.section_id = v_section
        WHERE sp.edge_id = p_edge_id
    ) d
    ORDER BY d.travelled;
END;
$$ LANGUAGE plpgsql STABLE;


-- -----------------------------------------------------------------------------
-- THE DIRECTIONAL COST. This is the function the router calls.
--
-- The pedestrian is not at the whole edge at once. Entering at p_entry_time and
-- walking at p_walk_speed_mps, they reach the sample d metres in at
-- p_entry_time + d/v — so each sample must be read at the timestep the walker is
-- actually THERE, not at the entry time.
--
-- That is the entire reason the sample series is retained. Two consequences that
-- a per-edge sunlit_sum cannot express:
--
--   * ASYMMETRY. Forward and reverse traversal visit the same samples in opposite
--     order against the SAME advancing clock, so they sample different (sample,
--     time) pairs. sun_seconds genuinely differs between the two directions.
--
--   * RUN LENGTH. longest_sun_run_m is the longest unbroken stretch in sun along
--     the direction of travel. For thermal comfort this dominates the total: 200 m
--     of continuous sun is far worse than ten 20 m patches interleaved with shade,
--     and both have identical sums.
--
-- Returns one row. NULLs if the edge has no exposure data for the traverse.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_edge_directional_cost(
    p_edge_id        UUID,
    p_entry_time     TIMESTAMP,
    p_reverse        BOOLEAN DEFAULT FALSE,
    p_walk_speed_mps DOUBLE PRECISION DEFAULT 1.35
) RETURNS TABLE (
    samples           INTEGER,
    edge_length_m     DOUBLE PRECISION,
    traverse_seconds  DOUBLE PRECISION,
    sun_seconds       DOUBLE PRECISION,
    shade_seconds     DOUBLE PRECISION,
    pct_sun           NUMERIC,
    entered_in_sun    BOOLEAN,
    exited_in_sun     BOOLEAN,
    longest_sun_run_m DOUBLE PRECISION,
    timesteps_spanned INTEGER
) AS $$
DECLARE
    v_section INTEGER;
    v_length  DOUBLE PRECISION;
    v_n       INTEGER;
    v_spacing DOUBLE PRECISION;
BEGIN
    IF p_walk_speed_mps <= 0 THEN
        RAISE EXCEPTION 'p_walk_speed_mps must be positive, got %', p_walk_speed_mps;
    END IF;

    SELECT es.section_id INTO v_section
      FROM meo_edge_sections es WHERE es.edge_id = p_edge_id;
    IF v_section IS NULL THEN
        RAISE EXCEPTION 'edge % is not on this shard', p_edge_id;
    END IF;

    SELECT count(*), max(sp.distance_from_start)
      INTO v_n, v_length
      FROM meo_sample_points sp WHERE sp.edge_id = p_edge_id;

    IF v_n IS NULL OR v_n = 0 THEN
        RETURN;
    END IF;

    -- Each sample stands for the segment around it. Derived from the geometry
    -- rather than assuming the 2 m nominal spacing, because the last interval on
    -- an edge whose length is not a multiple of the spacing is short.
    v_spacing := v_length / greatest(v_n - 1, 1);

    RETURN QUERY
    WITH walk AS (
        SELECT sp.id,
               CASE WHEN p_reverse THEN v_length - sp.distance_from_start
                    ELSE sp.distance_from_start END AS travelled
        FROM meo_sample_points sp
        WHERE sp.edge_id = p_edge_id
    ),
    timed AS (
        -- Where the walker is, and hence WHEN they are there.
        SELECT w.id,
               w.travelled,
               meo_snap_timestep(
                   p_entry_time + make_interval(secs => w.travelled / p_walk_speed_mps)
               ) AS ts
        FROM walk w
    ),
    lit AS (
        SELECT t.travelled, t.ts, e.is_sunlit
        FROM timed t
        JOIN meo_exposure_samples_p e
          ON e.sample_point_id = t.id
         AND e.datetime   = t.ts
         AND e.section_id = v_section
    ),
    -- Gaps and islands: consecutive samples sharing is_sunlit form one run,
    -- identified by the difference of two row numbers.
    runs AS (
        SELECT l.travelled, l.is_sunlit,
               row_number() OVER (ORDER BY l.travelled)
             - row_number() OVER (PARTITION BY l.is_sunlit ORDER BY l.travelled) AS grp
        FROM lit l
    ),
    run_lengths AS (
        SELECT is_sunlit,
               max(travelled) - min(travelled) + v_spacing AS run_m
        FROM runs GROUP BY is_sunlit, grp
    ),
    ends AS (
        SELECT (SELECT is_sunlit FROM lit ORDER BY travelled ASC  LIMIT 1) AS first_lit,
               (SELECT is_sunlit FROM lit ORDER BY travelled DESC LIMIT 1) AS last_lit
    )
    SELECT count(*)::INTEGER,
           v_length,
           v_length / p_walk_speed_mps,
           (count(*) FILTER (WHERE l.is_sunlit) * v_spacing / p_walk_speed_mps),
           (count(*) FILTER (WHERE NOT l.is_sunlit) * v_spacing / p_walk_speed_mps),
           round(100.0 * count(*) FILTER (WHERE l.is_sunlit) / NULLIF(count(*), 0), 2),
           (SELECT first_lit FROM ends),
           (SELECT last_lit  FROM ends),
           COALESCE((SELECT max(run_m) FROM run_lengths WHERE is_sunlit), 0),
           count(DISTINCT l.ts)::INTEGER
    FROM lit l;
END;
$$ LANGUAGE plpgsql STABLE;


COMMIT;

\echo ''
\echo '03_shard_schema.sql complete (data shard).'
\echo 'Next, per shard:'
\echo '  SELECT meo_set_shard_identity(<index>, <count>);'
\echo '  SELECT meo_provision_sections(ARRAY[...this shard''s sections...]);'
\echo '  SELECT meo_provision_edge_partitions(2026, 2026);'
\echo 'The orchestrator (plan_tasks.py --provision) does all three.'
