-- =============================================================================
-- SunlightCity — reduce phase (phase 5 of 6)          ***  DATA SHARD ONLY  ***
--
-- Run on EACH shard once its tasks have drained. The ten shards run this
-- concurrently and independently — that is what makes the reduce phase ~2 minutes
-- instead of ~20.
--
--
-- WHY THE REDUCE PHASE IS THIS THIN
-- ---------------------------------
-- In a classic MapReduce, reduce is where the shuffle happens and where most of
-- the cost lives. Here there is no shuffle at all, and the reason is a single
-- decision made back in 01: a SECTION OWNS WHOLE EDGES, assigned by edge
-- midpoint.
--
-- Because every sample point of a given edge lives in the same section, and every
-- section lives on one shard, the rollup
--
--     GROUP BY (edge_id, datetime)
--
-- is complete within one instance. No cross-shard summation, no barrier, no
-- coordinator gathering partial sums. Ten instances each aggregate their own
-- sixth of a billion rows and stop.
--
-- Had sections been defined by sample-point position instead, edges would split
-- at every section boundary, ~12% of edges would have samples on two shards, and
-- this file would have needed a distributed sum — which would also have made
-- every routing query a cross-shard join for the rest of the dataset's life.
--
--
-- WHAT ACTUALLY HAPPENS HERE
--   1. verify the shard's leaves are all present and correctly sized
--   2. derive meo_exposure_edges_p — the convenience index, NOT the product
--   3. index and ANALYZE the derived table
--   4. report
--
-- The sample table is not touched. It is already in its final partitions, already
-- frozen by COPY FREEZE, and deliberately has no index.
--
-- Idempotent: the rollup upserts, so a re-run is safe.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_shard_identity') THEN
        RAISE EXCEPTION 'not a data shard (no meo_shard_identity). '
                        'The coordinator has no reduce phase.';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 1. Completeness — checked BEFORE anything expensive.
--
-- Building a rollup over an incomplete load is worse than useless: it costs real
-- time and produces a dataset that LOOKS finished. A missing leaf shows up much
-- later as a street with no shade at any hour, which is indistinguishable from a
-- genuinely sunny street.
--
-- Two checks, because they catch different failures:
--   * a MISSING leaf means a task never completed (or was never planned)
--   * a WRONG-SIZED leaf means a task completed but wrote partial output, which
--     the queue cannot detect on its own
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW meo_shard_leaf_inventory AS
SELECT
    ss.section_id,
    ss.sample_count                       AS section_samples,
    count(c.oid)                          AS leaves_attached,
    -- GREATEST(...,0): pg_class.reltuples is -1 for a relation that has never been
    -- analysed, which these have not been until step 4 below. Reporting -1 as a
    -- row estimate reads as a bug rather than as "unknown".
    COALESCE(sum(GREATEST(c.reltuples, 0))::BIGINT, 0) AS rows_estimated
FROM meo_shard_sections ss
LEFT JOIN pg_class parent ON parent.relname = meo_section_parent_name(ss.section_id)
LEFT JOIN pg_inherits i   ON i.inhparent = parent.oid
LEFT JOIN pg_class c      ON c.oid = i.inhrelid AND c.relkind = 'r'
GROUP BY ss.section_id, ss.sample_count
ORDER BY ss.section_id;


-- Exact per-leaf row counts against expectation. Costs a count(*) per leaf, which
-- is a sequential scan of ~20 MB — a few seconds for a whole shard, and worth it
-- to catch a partially-written task before indexing on top of it.
CREATE OR REPLACE FUNCTION meo_verify_leaf_sizes(p_steps_per_window INTEGER DEFAULT 60)
RETURNS TABLE (
    leaf         TEXT,
    section_id   INTEGER,
    rows_actual  BIGINT,
    rows_expected BIGINT,
    delta        BIGINT
) AS $$
DECLARE
    r RECORD;
    v_actual BIGINT;
    v_expect BIGINT;
BEGIN
    FOR r IN
        SELECT c.relname AS leaf, ss.section_id, ss.sample_count
        FROM meo_shard_sections ss
        JOIN pg_class parent ON parent.relname = meo_section_parent_name(ss.section_id)
        JOIN pg_inherits i   ON i.inhparent = parent.oid
        JOIN pg_class c      ON c.oid = i.inhrelid AND c.relkind = 'r'
        ORDER BY ss.section_id, c.relname
    LOOP
        EXECUTE format('SELECT count(*) FROM %I', r.leaf) INTO v_actual;
        v_expect := r.sample_count::BIGINT * p_steps_per_window;

        IF v_actual <> v_expect THEN
            RETURN QUERY SELECT r.leaf, r.section_id, v_actual, v_expect,
                                v_actual - v_expect;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 2. Derive the per-edge rollup.
--
-- Per section rather than all at once, for three reasons: progress is visible on
-- a multi-minute operation, a failure is localised to one section instead of
-- discarding the whole shard's work, and the orchestrator can drive several
-- sections concurrently on separate connections when the instance has cores to
-- spare.
--
-- The GROUP BY reads the section's leaves and hash-joins 365k sample points —
-- small enough to stay in work_mem, so no spill. Partition pruning on section_id
-- keeps it to that section's leaves.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_rollup_edges(p_section_id INTEGER)
RETURNS BIGINT AS $$
DECLARE
    v_rows BIGINT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meo_shard_sections WHERE section_id = p_section_id) THEN
        RAISE EXCEPTION 'section % is not owned by this shard', p_section_id;
    END IF;

    INSERT INTO meo_exposure_edges_p
        (edge_id, datetime, sunlit_sum, sample_count, section_id)
    SELECT sp.edge_id,
           e.datetime,
           count(*) FILTER (WHERE e.is_sunlit)::INTEGER,
           count(*)::INTEGER,
           p_section_id
    FROM meo_exposure_samples_p e
    JOIN meo_sample_points sp ON sp.id = e.sample_point_id
    WHERE e.section_id = p_section_id
    GROUP BY sp.edge_id, e.datetime
    -- Idempotent: a re-run recomputes rather than duplicating. DO UPDATE rather
    -- than DO NOTHING so a re-run after fixing a bad task actually corrects the
    -- rollup instead of silently keeping the stale value.
    ON CONFLICT (edge_id, datetime) DO UPDATE
        SET sunlit_sum   = EXCLUDED.sunlit_sum,
            sample_count = EXCLUDED.sample_count,
            section_id   = EXCLUDED.section_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$ LANGUAGE plpgsql;


-- Convenience: roll up every section this shard owns, reporting as it goes.
CREATE OR REPLACE FUNCTION meo_rollup_all_edges()
RETURNS TABLE (section_id INTEGER, rows_written BIGINT, seconds DOUBLE PRECISION) AS $$
DECLARE
    r  RECORD;
    t0 TIMESTAMPTZ;
    n  BIGINT;
BEGIN
    FOR r IN SELECT ss.section_id FROM meo_shard_sections ss ORDER BY ss.section_id LOOP
        t0 := clock_timestamp();
        n  := meo_rollup_edges(r.section_id);
        -- Explicit cast: EXTRACT(EPOCH FROM interval) returns numeric on
        -- PostgreSQL 14+, and RETURN QUERY does not coerce it to the declared
        -- double precision — it raises "structure of query does not match".
        RETURN QUERY SELECT r.section_id, n,
                            EXTRACT(EPOCH FROM (clock_timestamp() - t0))::DOUBLE PRECISION;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


COMMIT;


-- =============================================================================
-- 3. Indexes on the DERIVED table — after the load, never during it.
--
-- An index maintained during a bulk load does a B-tree descent and possibly a
-- page split per row. Building afterwards instead:
--   * replaces N random descents with one large sequential sort bounded by
--     maintenance_work_mem
--   * parallelises across max_parallel_maintenance_workers
--   * yields a dense, unfragmented tree rather than one ~70% full from splits
--
-- CONCURRENTLY is not used: it is disallowed on partitioned tables, and would be
-- the wrong choice anyway since nothing is reading this yet.
--
-- Note the scale. This indexes 14.5 million rows per shard, not 789 million,
-- because the sample table has no index at all. That asymmetry is why the reduce
-- phase is seconds rather than hours.
-- =============================================================================

SET maintenance_work_mem = '16GB';
SET max_parallel_maintenance_workers = 8;

-- Created on the PARENT: PostgreSQL propagates to every existing partition and to
-- any created later, so this is one statement rather than twelve.
--
-- The PK (edge_id, datetime) already serves "cost of edge E at time T", which is
-- the path-search hot path. This complements rather than duplicates it: leading
-- on datetime serves whole-network snapshots and the visualisation overlay.
--
-- INCLUDE makes it covering, so a snapshot query is answered from the index alone
-- with no heap access. ~8 bytes/row to save a random heap fetch per row.
CREATE INDEX IF NOT EXISTS idx_meo_exp_edges_p_time
    ON meo_exposure_edges_p (datetime, edge_id) INCLUDE (sunlit_sum, sample_count);

-- Section-scoped scans, used by the federation on the coordinator to push a
-- predicate down to exactly the shards that can answer.
CREATE INDEX IF NOT EXISTS idx_meo_exp_edges_p_section
    ON meo_exposure_edges_p (section_id, datetime);


-- =============================================================================
-- 4. Statistics.
--
-- Not optional. The exposure leaves went from empty to ~10^8 rows with autovacuum
-- switched off (see 04), so the planner's statistics still say "empty". Until
-- ANALYZE runs, every query against them plans as though the tables were tiny and
-- picks catastrophically wrong plans — a nested loop over 789 million rows, for
-- instance.
--
-- ANALYZE here covers the derived table and the geometry. The 576 sample leaves
-- are analysed by the orchestrator with `vacuumdb --analyze --jobs 8`, which is
-- ~8x faster than doing them serially from one session.
-- =============================================================================
ANALYZE meo_exposure_edges_p;
ANALYZE meo_edge_sections;
ANALYZE meo_sample_points;


-- =============================================================================
-- 5. Integrity views.
--
-- Each corresponds to a specific way the pipeline could be subtly wrong while
-- still producing plausible-looking data — which is the dangerous kind of wrong.
-- =============================================================================

-- sunlit_sum > sample_count is arithmetically impossible and means the worker's
-- accumulator indexing is broken. The most dangerous class of bug here, because
-- the output would still look like exposure data.
CREATE OR REPLACE VIEW meo_integrity_edges AS
SELECT 'sunlit_sum > sample_count' AS check_name,
       count(*) AS violations
FROM meo_exposure_edges_p WHERE sunlit_sum > sample_count
UNION ALL
SELECT 'negative sunlit_sum', count(*)
FROM meo_exposure_edges_p WHERE sunlit_sum < 0
UNION ALL
SELECT 'zero sample_count', count(*)
FROM meo_exposure_edges_p WHERE sample_count <= 0
UNION ALL
-- An edge this shard owns that produced no rows means a section silently wrote
-- nothing — the leaf inventory should also have caught it, but this catches the
-- case where the leaf exists and is the right size yet holds the wrong edges.
SELECT 'owned edge with no exposure rows', count(*)
FROM meo_edge_sections es
JOIN meo_shard_sections ss USING (section_id)
WHERE NOT EXISTS (
    SELECT 1 FROM meo_exposure_edges_p e WHERE e.edge_id = es.edge_id
)
UNION ALL
-- Unattached leaves: a worker died between CREATE and ATTACH. They hold real disk
-- and are invisible through the parent, so they are pure loss until swept.
SELECT 'orphaned (unattached) leaves', count(*)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND c.relname LIKE 'meo\_exp\_s%\_w%'
  AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid);


-- Total size of a partitioned tree.
--
-- pg_total_relation_size() on a partitioned PARENT returns the parent's own size,
-- which is always zero — a partitioned table holds no pages itself. Calling it
-- directly would report "0 bytes" for 500 GB of exposure data, in exactly the
-- summary an operator uses to confirm the load worked. pg_partition_tree walks
-- the whole hierarchy; the relkind filter keeps intermediate levels (which are
-- also zero) from being double-counted as leaves.
CREATE OR REPLACE FUNCTION meo_tree_size(p_parent REGCLASS)
RETURNS BIGINT AS $$
    SELECT COALESCE(sum(pg_total_relation_size(t.relid)), 0)
    FROM pg_partition_tree(p_parent) t
    JOIN pg_class c ON c.oid = t.relid
    WHERE c.relkind IN ('r', 'i');
$$ LANGUAGE sql STABLE STRICT;


-- Per-shard summary, the one line reduce_finalize.py prints per instance.
CREATE OR REPLACE VIEW meo_shard_summary AS
SELECT
    (SELECT shard_index FROM meo_shard_identity)                  AS shard_index,
    (SELECT count(*) FROM meo_shard_sections)                     AS sections,
    (SELECT count(*) FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r'
        AND c.relname LIKE 'meo\_exp\_s%\_w%')                    AS leaves,
    pg_size_pretty(meo_tree_size('meo_exposure_samples_p'))       AS samples_size,
    pg_size_pretty(meo_tree_size('meo_exposure_edges_p'))         AS edges_size,
    (SELECT count(*) FROM meo_exposure_edges_p)                   AS edge_rows,
    (SELECT sum(violations) FROM meo_integrity_edges)             AS violations;


\echo ''
\echo '05_post_load_indexes.sql complete (data shard).'
\echo 'Verify:'
\echo '  SELECT * FROM meo_shard_leaf_inventory;   -- leaves per section'
\echo '  SELECT * FROM meo_verify_leaf_sizes();    -- expect 0 rows'
\echo '  SELECT * FROM meo_integrity_edges;        -- expect all violations = 0'
\echo '  SELECT * FROM meo_shard_summary;'
\echo ''
\echo 'The rollup itself is driven by the orchestrator:'
\echo '  SELECT * FROM meo_rollup_all_edges();'
