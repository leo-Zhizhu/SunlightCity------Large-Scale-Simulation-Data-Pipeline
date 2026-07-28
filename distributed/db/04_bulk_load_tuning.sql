-- =============================================================================
-- SunlightCity — per-table storage tuning (phase 4 of 6)
--                                        ***  SHARD *AND* COORDINATOR  ***
--
-- pg_tune.py tunes the SERVER. This file tunes the TABLES, which is where the
-- remaining wins are: server settings are blunt, while storage parameters let the
-- append-only 100 GB of exposure leaves and the high-churn few-thousand-row work
-- queue be treated as the completely different workloads they are.
--
-- One file, two roles. It detects which instance it is on rather than making the
-- operator remember, because "ran the coordinator tuning on a data shard" is a
-- mistake with no error message and a real cost.
--
-- Run AFTER 01/02 (coordinator) or 03 (shard). Before starting the fleet.
-- Idempotent.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    v_is_shard BOOLEAN := EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_shard_identity');
    v_is_coord BOOLEAN := EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_tasks');
    v_rel      TEXT;
BEGIN
    IF NOT v_is_shard AND NOT v_is_coord THEN
        RAISE EXCEPTION
            'cannot tell whether this is a shard or the coordinator. Run '
            '03_shard_schema.sql (shard) or 01+02 (coordinator) first.';
    END IF;
    IF v_is_shard AND v_is_coord THEN
        RAISE EXCEPTION
            'this instance has BOTH meo_shard_identity and meo_tasks. The control '
            'plane must not share an instance with bulk data — that is the '
            'contention this architecture exists to remove.';
    END IF;

    -- =======================================================================
    -- DATA SHARD
    -- =======================================================================
    IF v_is_shard THEN
        RAISE NOTICE 'tuning as DATA SHARD (index %)',
                     (SELECT shard_index FROM meo_shard_identity);

        -- ---- Exposure sample partitions ----------------------------------
        --
        -- fillfactor = 100. The default 90 reserves a tenth of every page for
        -- future HOT updates that will never come — these leaves are written once
        -- by COPY and then never UPDATEd or DELETEd. On 100 GB that default would
        -- waste ~10 GB and add 10% more pages to every sequential scan, which is
        -- the only access pattern they have.
        --
        -- autovacuum_enabled = off, and this is safe ONLY because the load uses
        -- COPY ... FREEZE. Normally switching autovacuum off on a large table
        -- risks transaction-id wraparound; here the tuples arrive already frozen,
        -- so there is nothing for a freeze vacuum to do. (Anti-wraparound vacuum
        -- would still run regardless of this setting — PostgreSQL does not let you
        -- opt out of that — but it finds nothing to rewrite.) Nothing is ever
        -- deleted, so there is no dead space to reclaim either.
        --
        -- ANALYZE is handled explicitly in 05 via vacuumdb --jobs. Leaving 576
        -- leaves per shard to autovacuum's polling loop would mean it discovering
        -- them one at a time, slowly, while the reduce phase waits.
        -- relkind = 'r' matters: the tree is three deep
        -- (meo_exposure_samples_p -> meo_exp_s<section> -> leaf) and the middle
        -- level is itself partitioned ('p'). PostgreSQL rejects storage parameters
        -- on a partitioned table — they belong to the relations that hold pages.
        FOR v_rel IN
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE c.relkind = 'r'
              AND (p.relname = 'meo_exposure_samples_p'
                   OR p.relname LIKE 'meo\_exp\_s%')
        LOOP
            EXECUTE format(
                'ALTER TABLE %I SET (fillfactor = 100, autovacuum_enabled = off, '
                'parallel_workers = 4)', v_rel);
        END LOOP;

        -- ---- Derived edge partitions -------------------------------------
        --
        -- Also append-only, but this IS the serving hot path, so autovacuum stays
        -- on for its statistics: the planner's estimate for
        -- `WHERE datetime = ...` decides between an index scan and a sequential
        -- scan of a 2.4M-row partition.
        FOR v_rel IN
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE c.relkind = 'r' AND p.relname = 'meo_exposure_edges_p'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I SET (fillfactor = 100, autovacuum_enabled = on, '
                'autovacuum_vacuum_threshold = 2000000000, '
                'autovacuum_vacuum_scale_factor = 0, '
                'autovacuum_analyze_threshold = 50000, '
                'autovacuum_analyze_scale_factor = 0.02, '
                'parallel_workers = 4)', v_rel);
            -- 500 buckets instead of 100 on the partition key: worth it because
            -- this column drives every plan against the table.
            EXECUTE format('ALTER TABLE %I ALTER COLUMN datetime SET STATISTICS 500', v_rel);
        END LOOP;

        -- ---- Static reference data ---------------------------------------
        -- Read by every directional query, never written after replication.
        FOR v_rel IN
            SELECT unnest(ARRAY['meo_sample_points', 'meo_edges', 'meo_waypoints',
                                'meo_trees', 'meo_edge_sections', 'meo_shard_sections'])
        LOOP
            IF EXISTS (SELECT 1 FROM pg_class WHERE relname = v_rel AND relkind = 'r') THEN
                EXECUTE format('ALTER TABLE %I SET (fillfactor = 100)', v_rel);
            END IF;
        END LOOP;

        RAISE NOTICE 'shard tuning applied.';
    END IF;

    -- =======================================================================
    -- COORDINATOR — the opposite problem in every respect.
    -- =======================================================================
    IF v_is_coord THEN
        RAISE NOTICE 'tuning as COORDINATOR';

        -- meo_tasks is small (~6,000 rows) but every row is UPDATEd many times:
        -- one claim, ~40 heartbeats over a task's life, one completion. That is
        -- ~42 row versions per task, ~250,000 over a run, on a table of 6,000.
        -- Left at the default it bloats within minutes and its partial indexes
        -- degrade until claim latency is visible to every worker.
        --
        -- fillfactor = 70 is the single most effective setting here: leaving room
        -- on the page lets a heartbeat rewrite the row as a HOT update, IN PLACE,
        -- without touching any index at all. Heartbeats are ~95% of the writes to
        -- this table, so making them index-free is most of the win.
        --
        -- Thresholds are absolute rather than scale factors: a scale factor of
        -- 0.2 on a 6,000-row table means 1,200 dead rows before vacuum, which at
        -- 42 versions per task is only 28 tasks of churn.
        ALTER TABLE meo_tasks SET (
            fillfactor = 70,
            autovacuum_enabled = on,
            autovacuum_vacuum_threshold = 50,
            autovacuum_vacuum_scale_factor = 0.0,
            autovacuum_analyze_threshold = 50,
            autovacuum_analyze_scale_factor = 0.0,
            -- No cost delay. A throttled autovacuum cannot keep pace with this
            -- churn, and the coordinator has nothing else competing for its I/O —
            -- that is precisely why it is a separate instance.
            autovacuum_vacuum_cost_delay = 0
        );

        ALTER TABLE meo_runs SET (fillfactor = 80);

        -- Topology tables: written once per plan, read on every claim.
        ALTER TABLE meo_sections      SET (fillfactor = 100);
        ALTER TABLE meo_edge_sections SET (fillfactor = 100);
        ALTER TABLE meo_shards        SET (fillfactor = 90);   -- last_seen_at ticks

        -- The coordinator also holds the authoritative static geometry.
        FOR v_rel IN
            SELECT unnest(ARRAY['meo_sample_points', 'meo_edges',
                                'meo_waypoints', 'meo_trees'])
        LOOP
            IF EXISTS (SELECT 1 FROM pg_class WHERE relname = v_rel AND relkind = 'r') THEN
                EXECUTE format('ALTER TABLE %I SET (fillfactor = 100)', v_rel);
            END IF;
        END LOOP;

        RAISE NOTICE 'coordinator tuning applied.';
    END IF;
END $$;

COMMIT;

-- -----------------------------------------------------------------------------
-- Note what is NOT here: any index on meo_exposure_samples_p.
--
-- 04 is the last file that runs before the fleet starts, and the sample table
-- reaches 1.58 billion rows without ever acquiring one. That is not an omission:
--
--   * An index maintained during the load costs a B-tree descent and possibly a
--     page split per COPY'd row, and would turn the upper levels into a
--     contention point.
--   * It would occupy ~60 GB, more than half the data it indexes.
--   * It would answer no question that partition pruning does not already answer
--     better. The only lookup is "this edge's samples at this timestamp", and
--     pruning reduces that to one ~261k-row leaf — cheaper to scan sequentially
--     than to descend a B-tree over 1.58e9 entries for.
--
-- Indexes on the DERIVED edge table are built in 05, after the load, where they
-- cost one large sequential sort instead of a billion random descents.
-- -----------------------------------------------------------------------------

\echo '04_bulk_load_tuning.sql complete.'
