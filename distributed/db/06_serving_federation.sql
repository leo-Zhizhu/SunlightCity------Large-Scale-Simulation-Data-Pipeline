-- =============================================================================
-- SunlightCity — serving federation (phase 6 of 6)   ***  COORDINATOR ONLY  ***
--
-- Run on the coordinator after the shards have finished their reduce phase. This
-- is what turns ten independent instances back into one queryable dataset.
--
--
-- TWO ACCESS PATHS, ON PURPOSE
-- ----------------------------
-- Sharding is only a win if reads stay cheap, and the two kinds of read here want
-- opposite things:
--
--   ROUTING (the hot path). "What does edge E cost, walked this way, at 14:12?"
--   Thousands per second, each touching one edge on one shard. The right answer is
--   for the client to CONNECT DIRECTLY to the owning shard and call
--   meo_edge_directional_cost() there. The coordinator's job is to answer "which
--   shard?" once, at session start, from a 6,700-row lookup — and then get out of
--   the way. Proxying the traffic through here would make one instance the
--   bottleneck for a workload that is otherwise perfectly parallel, for no benefit
--   at all. (This is the same reasoning that keeps PgBouncer out of the bulk COPY
--   path during the map phase.)
--
--   ANALYTICS. "Sunlit fraction of every street at 11:00 in July." Rare, and it
--   genuinely spans the whole city — so here one SQL statement over all ten shards
--   IS what you want. That is what the federation below provides, via
--   postgres_fdw with asynchronous foreign scans so the ten shards are read
--   concurrently rather than one after another.
--
-- Routing is served by meo_edge_shard() and meo_route_plan().
-- Analytics is served by meo_exposure_edges_fed.
--
-- Idempotent: safe to re-run.
-- =============================================================================

\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'meo_shards') THEN
        RAISE EXCEPTION 'meo_shards not found — run 01_cluster_topology.sql first.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM meo_sections WHERE shard_index IS NOT NULL) THEN
        RAISE EXCEPTION
            'no section has a shard assignment yet. Run plan_tasks.py so '
            'meo_sections.shard_index is populated before building the federation.';
    END IF;
END $$;


-- =============================================================================
-- 1. ROUTING LOOKUP — the hot path's only dependency on the coordinator.
-- =============================================================================

-- Where does this edge live? One index lookup on 6,700 rows.
--
-- A routing service calls this once per edge at warm-up, caches the whole map
-- (it is a few hundred kilobytes), and thereafter never touches the coordinator
-- on a request path at all.
CREATE OR REPLACE VIEW meo_edge_routing AS
SELECT es.edge_id,
       es.section_id,
       s.shard_index,
       sh.host,
       sh.port,
       sh.dbname,
       sh.state AS shard_state
FROM meo_edge_sections es
JOIN meo_sections s USING (section_id)
LEFT JOIN meo_shards sh ON sh.shard_index = s.shard_index;

CREATE OR REPLACE FUNCTION meo_edge_shard(p_edge_id UUID)
RETURNS TABLE (shard_index INTEGER, host TEXT, port INTEGER, dbname TEXT) AS $$
    SELECT r.shard_index, r.host, r.port, r.dbname
    FROM meo_edge_routing r WHERE r.edge_id = p_edge_id;
$$ LANGUAGE sql STABLE STRICT;


-- Fan-out plan for a whole candidate path.
--
-- The Hilbert-ordered, contiguous section assignment is what makes this useful:
-- a pedestrian route is spatially local, so its edges land on one or two shards
-- rather than being scattered across all ten. shard_count in the result is
-- therefore normally 1 or 2 — and a client that sees a larger number knows its
-- route crosses a shard boundary and can decide to issue the queries
-- concurrently.
--
-- Ordering by edge_count DESC puts the shard holding most of the route first, so
-- a client that wants a fast partial answer gets the bulk of it from one round
-- trip.
CREATE OR REPLACE FUNCTION meo_route_plan(p_edge_ids UUID[])
RETURNS TABLE (
    shard_index INTEGER,
    host        TEXT,
    port        INTEGER,
    dbname      TEXT,
    edge_count  INTEGER,
    edge_ids    UUID[]
) AS $$
    SELECT r.shard_index, r.host, r.port, r.dbname,
           count(*)::INTEGER, array_agg(r.edge_id)
    FROM meo_edge_routing r
    WHERE r.edge_id = ANY (p_edge_ids)
    GROUP BY r.shard_index, r.host, r.port, r.dbname
    ORDER BY count(*) DESC;
$$ LANGUAGE sql STABLE STRICT;


-- Locality as a measurement rather than a claim. Over a set of sampled routes,
-- reports how many shards each touched. plan_tasks.py prints the distribution so a
-- topology that has drifted (sections added, shard count changed) is visible.
CREATE OR REPLACE FUNCTION meo_route_locality(p_sample_routes INTEGER DEFAULT 200,
                                              p_route_edges INTEGER DEFAULT 12)
RETURNS TABLE (shards_touched INTEGER, routes INTEGER, pct NUMERIC) AS $$
WITH seeds AS (
    -- Deterministic pseudo-random walk seeds: take every Nth edge in Hilbert order
    -- so the sample is spread over the city rather than clustered.
    SELECT es.edge_id, s.hilbert_index,
           row_number() OVER (ORDER BY s.hilbert_index, es.edge_id) AS rn
    FROM meo_edge_sections es
    JOIN meo_sections s USING (section_id)
),
picked AS (
    SELECT rn FROM seeds
    WHERE rn % GREATEST(1, (SELECT count(*) FROM seeds) / p_sample_routes) = 0
    LIMIT p_sample_routes
),
routes AS (
    -- A route stands in as a contiguous run of edges in Hilbert order, which is
    -- the right shape: a real walk is spatially contiguous too.
    SELECT p.rn AS route_id,
           count(DISTINCT sh.shard_index)::INTEGER AS shards
    FROM picked p
    JOIN seeds s2 ON s2.rn >= p.rn AND s2.rn < p.rn + p_route_edges
    JOIN meo_edge_sections es2 ON es2.edge_id = s2.edge_id
    JOIN meo_sections sh USING (section_id)
    GROUP BY p.rn
)
SELECT shards, count(*)::INTEGER,
       round(100.0 * count(*) / NULLIF(sum(count(*)) OVER (), 0), 1)
FROM routes GROUP BY shards ORDER BY shards;
$$ LANGUAGE sql STABLE;


-- =============================================================================
-- 2. ANALYTICS FEDERATION — postgres_fdw over the ten shards.
-- =============================================================================

-- Creates (or updates) one foreign server per registered shard.
--
-- The options are the load-bearing part:
--
--   async_capable 'true'  — PostgreSQL 14+ runs foreign scans of an Append node
--     CONCURRENTLY. Without it, a query spanning ten shards issues ten remote
--     queries one after another and takes the SUM of their latencies instead of
--     the MAX. On a whole-network snapshot that is the difference between ~200 ms
--     and ~2 s, and it is one option.
--
--   fetch_size '50000' — the default 100 means a 14.5M-row scan makes ~145,000
--     round trips. At even 0.2 ms each that is 29 s of pure latency.
--
--   extensions 'postgis' — lets the planner push PostGIS operators down to the
--     shard instead of fetching rows and filtering locally.
CREATE OR REPLACE FUNCTION meo_setup_federation(p_password TEXT,
                                                p_local_user TEXT DEFAULT NULL)
RETURNS INTEGER AS $$
DECLARE
    r        RECORD;
    v_srv    TEXT;
    v_user   TEXT := COALESCE(p_local_user, current_user);
    v_made   INTEGER := 0;
BEGIN
    FOR r IN SELECT * FROM meo_shards ORDER BY shard_index LOOP
        v_srv := format('sunlit_shard_%s', r.shard_index);

        IF EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = v_srv) THEN
            EXECUTE format(
                'ALTER SERVER %I OPTIONS (SET host %L, SET port %L, SET dbname %L)',
                v_srv, r.host, r.port::TEXT, r.dbname);
        ELSE
            EXECUTE format(
                'CREATE SERVER %I FOREIGN DATA WRAPPER postgres_fdw '
                'OPTIONS (host %L, port %L, dbname %L, '
                '         async_capable ''true'', fetch_size ''50000'', '
                '         extensions ''postgis'')',
                v_srv, r.host, r.port::TEXT, r.dbname);
            v_made := v_made + 1;
        END IF;

        IF EXISTS (SELECT 1 FROM pg_user_mappings
                   WHERE srvname = v_srv AND usename = v_user) THEN
            EXECUTE format('ALTER USER MAPPING FOR %I SERVER %I OPTIONS (SET password %L)',
                           v_user, v_srv, p_password);
        ELSE
            EXECUTE format('CREATE USER MAPPING FOR %I SERVER %I '
                           'OPTIONS (user %L, password %L)',
                           v_user, v_srv, v_user, p_password);
        END IF;
    END LOOP;

    RETURN v_made;
END;
$$ LANGUAGE plpgsql;


-- The federated edge table.
--
-- PARTITION BY LIST (section_id), with one foreign partition per shard listing
-- exactly the sections that shard owns. This is why section_id is carried in
-- meo_exposure_edges_p rather than being left implicit: it is the only column
-- that lets the coordinator prune to the right instance at PLAN time.
--
-- A query filtered on section_id (or on edge_id, resolved to a section first)
-- touches one foreign scan. An unfiltered snapshot touches all ten, concurrently.
CREATE TABLE IF NOT EXISTS meo_exposure_edges_fed (
    edge_id      UUID      NOT NULL,
    datetime     TIMESTAMP NOT NULL,
    sunlit_sum   INTEGER   NOT NULL,
    sample_count INTEGER   NOT NULL,
    section_id   INTEGER   NOT NULL
) PARTITION BY LIST (section_id);


-- (Re)builds the foreign partitions from the current section -> shard map.
-- Called after any change to the topology; the foreign partitions are pure
-- metadata, so this is cheap and safe to repeat.
CREATE OR REPLACE FUNCTION meo_refresh_federation()
RETURNS INTEGER AS $$
DECLARE
    r          RECORD;
    v_part     TEXT;
    v_srv      TEXT;
    v_sections TEXT;
    v_made     INTEGER := 0;
BEGIN
    -- Detach and drop first: a section may have moved between shards, and a
    -- foreign partition's LIST bounds cannot be altered in place.
    FOR v_part IN
        SELECT c.relname FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'meo_exposure_edges_fed'
    LOOP
        EXECUTE format('ALTER TABLE meo_exposure_edges_fed DETACH PARTITION %I', v_part);
        EXECUTE format('DROP FOREIGN TABLE IF EXISTS %I', v_part);
    END LOOP;

    FOR r IN
        SELECT shard_index, array_agg(section_id ORDER BY section_id) AS sections
        FROM meo_sections
        WHERE shard_index IS NOT NULL
        GROUP BY shard_index ORDER BY shard_index
    LOOP
        v_part := format('meo_exp_edges_fed_s%s', r.shard_index);
        v_srv  := format('sunlit_shard_%s', r.shard_index);

        IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = v_srv) THEN
            RAISE WARNING 'no foreign server % — skipping shard %. '
                          'Call meo_setup_federation() first.', v_srv, r.shard_index;
            CONTINUE;
        END IF;

        SELECT string_agg(s::TEXT, ', ') INTO v_sections
        FROM unnest(r.sections) s;

        EXECUTE format(
            'CREATE FOREIGN TABLE %I PARTITION OF meo_exposure_edges_fed '
            'FOR VALUES IN (%s) SERVER %I '
            'OPTIONS (schema_name ''public'', table_name ''meo_exposure_edges_p'')',
            v_part, v_sections, v_srv);

        v_made := v_made + 1;
    END LOOP;

    RETURN v_made;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- The analytics query the README's heatmap is built from.
--
-- Aggregates every edge in the city at one instant. Spans all ten shards by
-- construction, which is exactly when the federation earns its keep.
--
-- TWO OPTIMISATIONS, AND THEY ARE MUTUALLY EXCLUSIVE PER PLAN NODE — worth
-- knowing, because it determines which plan is the "good" one to look for:
--
--   enable_partitionwise_aggregate pushes the count/sum INTO each shard, so the
--   plan is `Append -> Partial Aggregate -> Foreign Scan` and every shard returns
--   ONE row instead of 290,000. This is what an aggregate query gets, and it is
--   the better outcome — almost no data crosses the network.
--
--   enable_async_append makes the foreign scans run CONCURRENTLY, so total latency
--   is the max over shards rather than the sum. The plan reads `Async Foreign
--   Scan`. But async only applies to a foreign scan sitting DIRECTLY under an
--   Append — inserting a Partial Aggregate above it disqualifies it.
--
-- So an aggregate gets pushdown (not async) and a row-returning query gets async
-- (not pushdown). Both are correct; do not "fix" the aggregate plan for lacking
-- the word Async. Both settings are pinned at database scope at the bottom of this
-- file, because getting either wrong silently multiplies latency with no error to
-- notice.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_network_snapshot(p_datetime TIMESTAMP)
RETURNS TABLE (
    edges        BIGINT,
    samples      BIGINT,
    sunlit       BIGINT,
    pct_sunlit   NUMERIC,
    shards_read  INTEGER
) AS $$
    SELECT count(*)::BIGINT,
           sum(sample_count)::BIGINT,
           sum(sunlit_sum)::BIGINT,
           round(100.0 * sum(sunlit_sum) / NULLIF(sum(sample_count), 0), 3),
           (SELECT count(DISTINCT shard_index)::INTEGER
            FROM meo_sections WHERE shard_index IS NOT NULL)
    FROM meo_exposure_edges_fed
    WHERE datetime = p_datetime;
$$ LANGUAGE sql STABLE STRICT;


COMMIT;

-- -----------------------------------------------------------------------------
-- Settings the federation depends on, pinned at DATABASE scope so every session
-- gets them — a client that connects without them silently reads the ten shards
-- serially, which looks like "the federation is slow" rather than like a
-- misconfiguration. Also present in postgresql.coordinator.conf.
--
-- Dynamic SQL because ALTER DATABASE takes a literal name, not an expression:
-- `ALTER DATABASE current_database ...` looks for a database actually called
-- "current_database".
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET enable_async_append = on', current_database());
    EXECUTE format('ALTER DATABASE %I SET enable_partitionwise_aggregate = on', current_database());
    EXECUTE format('ALTER DATABASE %I SET enable_partitionwise_join = on', current_database());
END $$;

\echo ''
\echo '06_serving_federation.sql complete (coordinator).'
\echo 'To bring the federation up:'
\echo '  SELECT meo_setup_federation(:''pgpassword'');'
\echo '  SELECT meo_refresh_federation();'
\echo 'Then verify:'
\echo '  SELECT * FROM meo_route_locality();  -- most routes should touch 1 shard'
\echo '  SELECT * FROM meo_network_snapshot(''2026-07-15 11:00:00'');'
\echo ''
\echo 'Expected plans (see the note above meo_network_snapshot):'
\echo '  -- aggregate: pushdown, one row per shard'
\echo '  EXPLAIN SELECT count(*) FROM meo_exposure_edges_fed'
\echo '   WHERE datetime = ''2026-07-15 11:00:00'';'
\echo '       Append -> Partial Aggregate -> Foreign Scan'
\echo '  -- rows: concurrent shard reads'
\echo '  EXPLAIN SELECT edge_id FROM meo_exposure_edges_fed'
\echo '   WHERE datetime = ''2026-07-15 11:00:00'';'
\echo '       Append -> Async Foreign Scan'
\echo '  -- one section: pruned to a single shard'
\echo '  EXPLAIN SELECT sunlit_sum FROM meo_exposure_edges_fed'
\echo '   WHERE section_id = 0 AND datetime = ''2026-07-15 11:00:00'';'
\echo '       Foreign Scan (one, no Append)'
