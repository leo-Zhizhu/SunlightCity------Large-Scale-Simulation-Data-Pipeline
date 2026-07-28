#!/usr/bin/env bash
# =============================================================================
# Runs the schema and queue self-tests end to end against a throwaway database.
#
#   distributed/db/tests/run_selftest.sh                    # local postgres
#   PGHOST=... PGUSER=admin PGPASSWORD=... ./run_selftest.sh # remote
#   SUNLIT_DOCKER=my-pg-container ./run_selftest.sh          # inside a container
#
# Creates sunlit_selftest, applies the shard schema, asserts the invariants, and
# drops the database again. Nothing else is touched — in particular it never
# connects to a real shard, so it is safe to run against a production cluster's
# host.
#
# Exits 0 if every assertion passed, non-zero on the first failure.
#
# WHY THE ORDER MATTERS
# ---------------------
# 03_shard_schema.sql refuses to install on a shard that has no static geometry,
# because a shard without meo_sample_points cannot answer a directional query and
# failing at install time beats failing per-task later. So the geometry stub has to
# exist before the schema, which is the one ordering constraint this script
# encodes. (On a real shard the geometry arrives by pg_dump from the coordinator —
# see docs/DEPLOYMENT.md step 4.)
# =============================================================================
set -euo pipefail

DB="${SUNLIT_SELFTEST_DB:-sunlit_selftest}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="$(dirname "$HERE")"

# One indirection so the same script works against a local server or a container.
if [[ -n "${SUNLIT_DOCKER:-}" ]]; then
    psql_run()  { docker exec -i "$SUNLIT_DOCKER" psql -v ON_ERROR_STOP=1 -U "${PGUSER:-admin}" "$@"; }
    psql_file() { docker exec -i "$SUNLIT_DOCKER" psql -v ON_ERROR_STOP=1 -U "${PGUSER:-admin}" -d "$DB" -q -f - < "$1"; }
else
    psql_run()  { psql -v ON_ERROR_STOP=1 "$@"; }
    psql_file() { psql -v ON_ERROR_STOP=1 -d "$DB" -q -f "$1"; }
fi

cleanup() {
    psql_run -d postgres -qc "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> creating $DB"
cleanup
psql_run -d postgres -qc "CREATE DATABASE $DB" >/dev/null
psql_run -d "$DB" -qc "CREATE EXTENSION IF NOT EXISTS postgis" >/dev/null

# Geometry stub: the v1 tables, empty. Column definitions are copied verbatim from
# db_pipeline_initializer.py — if those ever diverge, this is where it shows.
echo "==> geometry stub (v1 tables, empty)"
psql_run -d "$DB" -q <<'SQL' >/dev/null
CREATE TABLE meo_waypoints (id UUID PRIMARY KEY, geom GEOMETRY(PointZ, 0));
CREATE TABLE meo_edges (
    id UUID PRIMARY KEY,
    start_wp_id UUID REFERENCES meo_waypoints(id),
    end_wp_id UUID REFERENCES meo_waypoints(id),
    length FLOAT, sample_count INT DEFAULT 0, total_tree_value FLOAT DEFAULT 0,
    geom GEOMETRY(LineStringZ, 0));
CREATE TABLE meo_trees (id UUID PRIMARY KEY, geom GEOMETRY(PointZ, 0), shade_norm FLOAT);
CREATE TABLE meo_sample_points (
    id UUID PRIMARY KEY, edge_id UUID REFERENCES meo_edges(id),
    sequence_index INT, distance_from_start FLOAT,
    geom GEOMETRY(PointZ, 0), tree_value FLOAT DEFAULT 0.0);
SQL

echo "==> 03_shard_schema.sql"
psql_file "$DB_DIR/03_shard_schema.sql" >/dev/null

echo "==> 05_post_load_indexes.sql"
psql_file "$DB_DIR/05_post_load_indexes.sql" >/dev/null

# The coordinator's topology and queue live in the same scratch database here. On a
# real deployment they are on a different INSTANCE from the shard schema — 04
# refuses to tune a database holding both, precisely to stop that happening by
# accident. Co-locating them is safe for a test that only calls functions.
echo "==> 01_cluster_topology.sql + 02_work_queue.sql"
psql_file "$DB_DIR/01_cluster_topology.sql" >/dev/null
psql_file "$DB_DIR/02_work_queue.sql" >/dev/null

echo "==> shard schema assertions"
# The shard self-test inserts sample points for synthetic edge ids that have no
# meo_edges row, so the foreign key has to go first. Real shards keep it; this is a
# fixture concession, stated rather than hidden.
psql_run -d "$DB" -qc \
    "ALTER TABLE meo_sample_points DROP CONSTRAINT meo_sample_points_edge_id_fkey" >/dev/null

psql_file "$HERE/shard_selftest.sql"

echo
echo "==> queue semantics assertions"
psql_file "$HERE/queue_selftest.sql"

echo
echo "==> self-test complete"
