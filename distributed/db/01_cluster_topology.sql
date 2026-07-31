-- =============================================================================
-- SunlightCity — cluster topology (phase 1 of 6)   ***  COORDINATOR ONLY  ***
--
-- Run against the COORDINATOR instance, which holds the control plane and the
-- authoritative copy of the static geometry. Nothing in this file goes on a data
-- shard.
--
--
-- WHAT PROBLEM THIS FILE SOLVES
-- -----------------------------
-- The v1 pipeline wrote one row per (sample point, timestamp) — 1,577,374,560 of
-- them for an annual run — into a single PostgreSQL instance. That schema is
-- fixed and is not negotiable: the downstream router traverses an edge as an
-- ORDERED, DIRECTIONAL sequence of sample points, because walking an edge from
-- one end enters sun and leaves shade while walking it from the other does the
-- reverse. A per-edge sum cannot express that. So the row count stands.
--
-- One instance cannot absorb 7.89 billion rows from 54 concurrent producers in
-- any reasonable time. A COPY backend is one busy CPU, so a 16 vCPU instance
-- sustains about twelve productive streams — roughly 2.4M rows/s — while the
-- fleet produces 15.97M rows/s. Six sevenths of the fleet would sit waiting.
--
-- The fix is more instances, and the whole question becomes: which instance owns
-- which piece of the city, and how does a worker know?
--
--
-- THE ANSWER, IN THREE STEPS
-- --------------------------
--   1. Cut the city into 1 km SECTIONS. A section owns whole EDGES (assigned by
--      edge midpoint), never half of one — so a section's rows can be rolled up
--      into per-edge costs without consulting any other section.
--   2. Order the sections along a HILBERT CURVE and cut that sequence into k
--      contiguous runs of equal sample count. Contiguous runs of a Hilbert curve
--      are compact connected regions, so this balances writes AND keeps a
--      pedestrian route — a spatially local object — on one or two instances.
--   3. Record the result here. One authority, read by the planner, by every
--      worker, and by the serving federation.
--
--
-- WHY BOUNDING-BOX SHARDING IS CORRECT HERE
-- -----------------------------------------
-- The obvious objection to spatial sharding is that a building OUTSIDE a section
-- casts shadows INTO it, so sections are not independent. That objection is
-- answered exactly, not approximately:
--
--   A building of height H casts a shadow reaching H/tan(theta) horizontally at
--   sun elevation theta. Below SUN_ANGLE_THRESHOLD (5 deg) the worker declares
--   shadow WITHOUT raycasting, so theta is bounded below and the shadow reach is
--   bounded above: 200 m / tan(5 deg) = 2,286 m.
--
-- Nothing more than 2,286 m outside a section can influence a sample inside it.
-- The sections are therefore genuinely independent units of work, and a worker
-- that holds the whole city mesh (which it does — 6 GB, and pods have 16) gets
-- seam correctness for free. What sectioning buys is not reduced memory; it is
-- the data mapping, and the ray coherence that comes from every ray in a task
-- originating inside the same square kilometre.
--
-- Idempotent: safe to re-run.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Guard. The static geometry must already exist — it comes from the v1
--    initialiser, unchanged, because the v1 schema is the schema.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name = 'meo_edges') THEN
        RAISE EXCEPTION
            'meo_edges not found. Run "Python & DB Scripts/Database/db_pipeline_initializer.py" '
            'against the coordinator first — v2 builds on the v1 schema, it does not replace it.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name = 'meo_sample_points') THEN
        RAISE EXCEPTION 'meo_sample_points not found. Export sample points before planning a run.';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 1. The section grid — a pinned contract, not a convenience.
--
-- Three implementations compute section ids: this file, cluster.py, and
-- SectionGrid.cs in the worker. If they ever disagree by one, a worker writes
-- its rows into a neighbouring section's partition and the error is invisible
-- until someone notices a street with two exposure profiles.
--
-- So the formula is integer-only (no float rounding to diverge on), stated once,
-- and the parameters live in a single row that plan_tasks.py freezes into
-- meo_runs.config. A worker whose grid disagrees with the run refuses to start.
--
-- The origin is snapped DOWN to a multiple of section_meters so grid lines fall
-- on round world coordinates: re-extracting the graph with a slightly different
-- extent then does not renumber every section.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_grid (
    -- Single-row table. The CHECK is the enforcement.
    lock_id        BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (lock_id),
    origin_x       DOUBLE PRECISION NOT NULL,
    origin_z       DOUBLE PRECISION NOT NULL,
    section_meters DOUBLE PRECISION NOT NULL CHECK (section_meters > 0),
    -- Row stride for section_id = row * cols + col. Fixed at the Hilbert lattice
    -- side so ids are stable even if the city's extent changes.
    cols           INTEGER NOT NULL DEFAULT 128 CHECK (cols > 0),
    hilbert_order  INTEGER NOT NULL DEFAULT 7,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Derives and stores the grid from the graph's own extent. Call once per city.
CREATE OR REPLACE FUNCTION meo_init_grid(p_section_meters DOUBLE PRECISION DEFAULT 1000.0)
RETURNS TABLE (origin_x DOUBLE PRECISION, origin_z DOUBLE PRECISION,
               section_meters DOUBLE PRECISION, span_cols INTEGER, span_rows INTEGER) AS $$
DECLARE
    min_x DOUBLE PRECISION;
    min_z DOUBLE PRECISION;
    max_x DOUBLE PRECISION;
    max_z DOUBLE PRECISION;
    ox    DOUBLE PRECISION;
    oz    DOUBLE PRECISION;
    nc    INTEGER;
    nr    INTEGER;
BEGIN
    -- PostGIS Y carries the horizontal Unity Z. See the axis note at the bottom.
    SELECT min(ST_X(geom)), min(ST_Y(geom)), max(ST_X(geom)), max(ST_Y(geom))
      INTO min_x, min_z, max_x, max_z
      FROM meo_sample_points;

    IF min_x IS NULL THEN
        RAISE EXCEPTION 'meo_sample_points is empty; cannot derive a section grid.';
    END IF;

    ox := floor(min_x / p_section_meters) * p_section_meters;
    oz := floor(min_z / p_section_meters) * p_section_meters;
    nc := ceil((max_x - ox) / p_section_meters)::INTEGER;
    nr := ceil((max_z - oz) / p_section_meters)::INTEGER;

    IF nc > 128 OR nr > 128 THEN
        RAISE EXCEPTION
            'graph spans %x% sections, above the 128-cell Hilbert lattice. '
            'Raise hilbert_order here, in cluster.py and in SectionGrid.cs together, '
            'or use larger sections.', nc, nr;
    END IF;

    INSERT INTO meo_grid (lock_id, origin_x, origin_z, section_meters)
    VALUES (TRUE, ox, oz, p_section_meters)
    ON CONFLICT (lock_id) DO UPDATE
       SET origin_x = EXCLUDED.origin_x,
           origin_z = EXCLUDED.origin_z,
           section_meters = EXCLUDED.section_meters;

    RETURN QUERY SELECT ox, oz, p_section_meters, nc, nr;
END;
$$ LANGUAGE plpgsql;


-- THE CONTRACT. Mirrored exactly in cluster.py::SectionGrid.section_id and
-- SectionGrid.cs::SectionId. floor() then integer arithmetic — no rounding mode
-- to differ between languages.
CREATE OR REPLACE FUNCTION meo_section_id(p_x DOUBLE PRECISION, p_z DOUBLE PRECISION)
RETURNS INTEGER AS $$
    SELECT (floor((p_z - g.origin_z) / g.section_meters)::INTEGER * g.cols)
         + (floor((p_x - g.origin_x) / g.section_meters)::INTEGER)
    FROM meo_grid g WHERE g.lock_id;
$$ LANGUAGE sql STABLE STRICT;


-- -----------------------------------------------------------------------------
-- 2. Hilbert index.
--
-- Position of grid cell (col, row) along a Hilbert curve. The iterative xy->d
-- conversion: descend from the coarsest quadrant to the finest, rotating and
-- reflecting the local frame at each level so the curve stays continuous across
-- quadrant boundaries.
--
-- That continuity is the whole point. It is what makes any contiguous run of
-- indices a compact, connected region of the plane — and therefore what lets one
-- cut satisfy both write balance and read locality.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meo_hilbert_index(p_x INTEGER, p_y INTEGER,
                                             p_order INTEGER DEFAULT 7)
RETURNS BIGINT AS $$
DECLARE
    side BIGINT := 1::BIGINT << p_order;
    x    BIGINT := p_x;
    y    BIGINT := p_y;
    rx   INTEGER;
    ry   INTEGER;
    d    BIGINT := 0;
    s    BIGINT;
    t    BIGINT;
BEGIN
    IF x < 0 OR y < 0 OR x >= side OR y >= side THEN
        RAISE EXCEPTION 'cell (%,%) outside a %x% Hilbert lattice', p_x, p_y, side, side;
    END IF;

    s := side >> 1;
    WHILE s > 0 LOOP
        rx := CASE WHEN (x & s) > 0 THEN 1 ELSE 0 END;
        ry := CASE WHEN (y & s) > 0 THEN 1 ELSE 0 END;
        d  := d + s * s * ((3 * rx) # ry);          -- '#' is XOR in PostgreSQL

        -- Rotate the quadrant into the canonical frame for the next level down.
        IF ry = 0 THEN
            IF rx = 1 THEN
                x := s - 1 - x;
                y := s - 1 - y;
            END IF;
            t := x; x := y; y := t;
        END IF;

        s := s >> 1;
    END LOOP;

    RETURN d;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;


-- -----------------------------------------------------------------------------
-- 3. Edge -> section assignment.
--
-- Assignment is by edge MIDPOINT, and every sample point follows its edge. This
-- is the decision that keeps the reduce phase shard-local:
--
--   * A section owns whole edges, so the per-edge rollup
--     GROUP BY (edge_id, datetime) can run inside one instance with no
--     cross-shard summation, no shuffle, no barrier.
--   * An edge is never split, so no edge's cost is assembled from two instances
--     — which would make every routing query a distributed join.
--
-- Assigning by point position instead (each sample to the section containing it)
-- would split edges at every section boundary and forfeit both properties, for
-- no gain: the geometric bound in the header means a worker holds the whole mesh
-- regardless.
--
-- The cost is a ragged section boundary — an edge whose midpoint is just inside
-- a section may reach up to half its length outside it. Edges here are under
-- 400 m, so the overhang is bounded by ~200 m against a 2,286 m halo, i.e. it is
-- absorbed by a bound we already respect.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_edge_sections (
    edge_id    UUID    PRIMARY KEY REFERENCES meo_edges(id),
    section_id INTEGER NOT NULL,
    -- Denormalised so the planner can weight sections without re-joining 365k
    -- sample rows on every plan.
    sample_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_meo_edge_sections_section
    ON meo_edge_sections (section_id);


CREATE OR REPLACE FUNCTION meo_assign_edge_sections()
RETURNS INTEGER AS $$
DECLARE
    n INTEGER;
BEGIN
    TRUNCATE meo_edge_sections;

    -- ST_LineInterpolatePoint(geom, 0.5) is the midpoint by ARC LENGTH, which for
    -- a two-vertex edge is the geometric midpoint and for a polyline is the
    -- correct notion anyway. ST_Centroid would be pulled toward a dense cluster
    -- of vertices.
    INSERT INTO meo_edge_sections (edge_id, section_id, sample_count)
    SELECT e.id,
           meo_section_id(ST_X(mid.p), ST_Y(mid.p)),
           COALESCE(sc.n, 0)
    FROM meo_edges e
    CROSS JOIN LATERAL (
        SELECT ST_LineInterpolatePoint(ST_Force2D(e.geom), 0.5) AS p
    ) mid
    LEFT JOIN (
        SELECT edge_id, count(*)::INTEGER AS n
        FROM meo_sample_points GROUP BY edge_id
    ) sc ON sc.edge_id = e.id;

    SELECT count(*) INTO n FROM meo_edge_sections;
    RETURN n;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 4. Sections, with the weight that actually matters.
--
-- The weight is sample_count, not area and not edge count. Sections produce rows
-- in proportion to their sample points, and it is rows that the database has to
-- absorb. Weighting by area would hand midtown and the northern tip the same
-- budget for several times the work.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meo_sections (
    section_id    INTEGER PRIMARY KEY,
    grid_col      INTEGER NOT NULL,
    grid_row      INTEGER NOT NULL,
    centre        GEOMETRY(PointZ, 0),
    edge_count    INTEGER NOT NULL DEFAULT 0,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    hilbert_index BIGINT  NOT NULL,
    -- Filled in by plan_tasks.py from cluster.py's exact partition. NULL until
    -- then, so a half-planned run cannot dispatch work to nowhere.
    shard_index   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_meo_sections_hilbert ON meo_sections (hilbert_index);
CREATE INDEX IF NOT EXISTS idx_meo_sections_shard   ON meo_sections (shard_index);


-- Note the v_ prefix on every local. A plpgsql variable named `g` would shadow a
-- table alias `g` inside the embedded SQL below, and PostgreSQL reports that as
-- "column reference is ambiguous" only at RUNTIME — a landmine in a function that
-- is called once per plan. The prefix convention makes the collision impossible.
CREATE OR REPLACE FUNCTION meo_rebuild_sections()
RETURNS INTEGER AS $$
DECLARE
    v_grid  meo_grid;
    v_elev  DOUBLE PRECISION;
    v_count INTEGER;
BEGIN
    SELECT * INTO v_grid FROM meo_grid WHERE lock_id;
    IF v_grid IS NULL THEN
        RAISE EXCEPTION 'no section grid. Call meo_init_grid() first.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM meo_edge_sections) THEN
        RAISE EXCEPTION 'meo_edge_sections is empty. Call meo_assign_edge_sections() first.';
    END IF;

    SELECT COALESCE(min(ST_Z(geom)), 0) INTO v_elev FROM meo_waypoints;

    -- Preserve any existing shard assignment for sections that still exist, so a
    -- re-derivation after adding a few edges does not move data that is already
    -- loaded. plan_tasks.py overwrites deliberately when the shard count changes.
    CREATE TEMP TABLE _prev_shards ON COMMIT DROP AS
        SELECT section_id, shard_index FROM meo_sections WHERE shard_index IS NOT NULL;

    DELETE FROM meo_sections;

    INSERT INTO meo_sections
        (section_id, grid_col, grid_row, centre,
         edge_count, sample_count, hilbert_index, shard_index)
    SELECT s.section_id,
           s.grid_col,
           s.grid_row,
           ST_SetSRID(ST_MakePoint(
               v_grid.origin_x + (s.grid_col + 0.5) * v_grid.section_meters,
               v_grid.origin_z + (s.grid_row + 0.5) * v_grid.section_meters,
               -- Planar graph: one shared elevation, matching the v1 constant.
               v_elev), 0),
           s.edge_count,
           s.sample_count,
           meo_hilbert_index(s.grid_col, s.grid_row, v_grid.hilbert_order),
           p.shard_index
    FROM (
        SELECT es.section_id,
               (es.section_id % v_grid.cols) AS grid_col,
               (es.section_id / v_grid.cols) AS grid_row,
               count(*)::INTEGER             AS edge_count,
               sum(es.sample_count)::INTEGER AS sample_count
        FROM meo_edge_sections es
        GROUP BY es.section_id
    ) s
    LEFT JOIN _prev_shards p USING (section_id);

    SELECT count(*) INTO v_count FROM meo_sections;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 5. Shard registry.
--
-- Where the instances are, and what state they are in. Workers resolve their
-- target instance through this rather than through environment variables, so a
-- shard can be replaced mid-run without redeploying 54 pods.
--
-- 'draining' exists for exactly that: it stops new tasks being dispatched to a
-- shard while the ones in flight finish, rather than failing them.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meo_shard_state') THEN
        CREATE TYPE meo_shard_state AS ENUM ('online', 'draining', 'offline');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS meo_shards (
    shard_index INTEGER PRIMARY KEY,
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL DEFAULT 5432,
    dbname      TEXT    NOT NULL,
    state       meo_shard_state NOT NULL DEFAULT 'online',
    -- Reported by the shard itself so the planner can size streams to real
    -- hardware instead of an assumption.
    vcpu        INTEGER,
    ram_gb      INTEGER,
    notes       TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ
);


CREATE OR REPLACE FUNCTION meo_register_shard(
    p_index  INTEGER,
    p_host   TEXT,
    p_port   INTEGER DEFAULT 5432,
    p_dbname TEXT    DEFAULT NULL,
    p_vcpu   INTEGER DEFAULT NULL,
    p_ram_gb INTEGER DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO meo_shards (shard_index, host, port, dbname, vcpu, ram_gb, last_seen_at)
    VALUES (p_index, p_host, p_port,
            COALESCE(p_dbname, format('sunlit_shard_%s', p_index)),
            p_vcpu, p_ram_gb, now())
    ON CONFLICT (shard_index) DO UPDATE
       SET host = EXCLUDED.host,
           port = EXCLUDED.port,
           dbname = EXCLUDED.dbname,
           vcpu = COALESCE(EXCLUDED.vcpu, meo_shards.vcpu),
           ram_gb = COALESCE(EXCLUDED.ram_gb, meo_shards.ram_gb),
           last_seen_at = now();
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 6. Resolution view — what a worker asks for.
--
-- One query answers "given my section, where do I write?". Joining in the worker
-- would mean shipping the whole map to 54 pods and keeping it fresh.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW meo_section_routing AS
SELECT s.section_id,
       s.shard_index,
       sh.host,
       sh.port,
       sh.dbname,
       sh.state,
       s.edge_count,
       s.sample_count
FROM meo_sections s
LEFT JOIN meo_shards sh ON sh.shard_index = s.shard_index;


-- Balance and locality, as a query rather than a claim. plan_tasks.py prints
-- this and refuses a badly-balanced topology.
CREATE OR REPLACE VIEW meo_shard_balance AS
WITH per_shard AS (
    SELECT shard_index,
           count(*)                AS sections,
           sum(sample_count)::BIGINT AS samples,
           sum(edge_count)::BIGINT   AS edges
    FROM meo_sections
    WHERE shard_index IS NOT NULL
    GROUP BY shard_index
)
SELECT shard_index,
       sections,
       edges,
       samples,
       round(100.0 * samples / NULLIF(sum(samples) OVER (), 0), 2) AS pct_of_total,
       round(samples::NUMERIC / NULLIF(avg(samples) OVER (), 0), 3) AS load_vs_mean
FROM per_shard
ORDER BY shard_index;


COMMIT;

-- -----------------------------------------------------------------------------
-- AXIS CONVENTION, consistent across every file in this repository:
--
--     PostGIS (X, Y, Z)  =  Unity (x, z, y)
--
-- PostGIS Y carries the horizontal Unity Z; PostGIS Z carries the vertical.
-- SRID 0 — raw Unity world units, not a geographic CRS. Section columns are
-- therefore indexed on PostGIS X and rows on PostGIS Y.
-- -----------------------------------------------------------------------------

\echo ''
\echo '01_cluster_topology.sql complete (coordinator).'
\echo 'Next, in order:'
\echo '  SELECT * FROM meo_init_grid(1000.0);'
\echo '  SELECT meo_assign_edge_sections();'
\echo '  SELECT meo_rebuild_sections();'
\echo '  SELECT meo_register_shard(i, format(''sunlit-shard-%s.sunlit-shards'', i), 5432)'
\echo '    FROM generate_series(0, 9) i;'
\echo 'Then plan_tasks.py computes and writes meo_sections.shard_index.'
