"""
Pipeline phase 1 — create the MEO schema and load the static geometry.

WARNING: initialize_database() DROPs and recreates all six meo_* tables. Running this against a
populated database destroys every simulation result. It is the first step of a fresh build only.

Order of operations:
  1. Drop + recreate schema, enable PostGIS, create spatial/temporal/FK indexes up front (so the
     bulk loads below and the Unity export later both hit indexes rather than sequential scans).
  2. Import street trees from CSV.
  3. Import the road graph from road_graph.json (RoadGraphExtractor output), declustering
     near-coincident vertices into single waypoints before insert.

Axis convention used by every INSERT here — PostGIS ST_MakePoint(unity_x, unity_z, unity_y):
PostGIS X = Unity X, PostGIS Y = Unity Z, PostGIS Z = Unity Y (vertical). SRID 0 throughout,
i.e. raw Unity world units rather than a geographic CRS.

Elevation is deliberately forced to a single constant for waypoints and trees alike: the routing
graph is planar, and pinning one shared height keeps sample geometry consistent and makes the
2D ST_DWithin tree queries exact.
"""

import json
import psycopg2
import uuid
import math
import os
import csv
from collections import deque

# --- CONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

# Input Files
TREE_CSV = "NYC_tree_unity_coords.csv"
ROAD_JSON = "road_graph.json"

CLUSTER_DIST = 15.0    # metres: vertices closer than this collapse into one waypoint
GLOBAL_ELEVATION = -112.0  # shared planar height; must match ShadowAwarePathFinder.globalElevation

def get_distance(v1, v2):
    return math.sqrt((v1['x'] - v2['x'])**2 + (v1['y'] - v2['y'])**2 + (v1['z'] - v2['z'])**2)

def initialize_database():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    print("Creating MEO tables...")
    # Destructive: wipes all previous simulation output. Dropped child-first so the FK
    # dependency chain (exposure -> sample_points -> edges -> waypoints) unwinds cleanly.
    cur.execute("DROP TABLE IF EXISTS meo_exposure_samples CASCADE;")
    cur.execute("DROP TABLE IF EXISTS meo_exposure_edges CASCADE;")
    cur.execute("DROP TABLE IF EXISTS meo_sample_points CASCADE;")
    cur.execute("DROP TABLE IF EXISTS meo_edges CASCADE;")
    cur.execute("DROP TABLE IF EXISTS meo_waypoints CASCADE;")
    cur.execute("DROP TABLE IF EXISTS meo_trees CASCADE;")
    
    # Enable PostGIS if not enabled
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    
    # meo_waypoints
    cur.execute("""
        CREATE TABLE meo_waypoints (
            id UUID PRIMARY KEY,
            geom GEOMETRY(PointZ, 0)
        );
    """)
    
    # meo_edges
    cur.execute("""
        CREATE TABLE meo_edges (
            id UUID PRIMARY KEY,
            start_wp_id UUID REFERENCES meo_waypoints(id),
            end_wp_id UUID REFERENCES meo_waypoints(id),
            length FLOAT,
            sample_count INT DEFAULT 0,
            total_tree_value FLOAT DEFAULT 0,
            geom GEOMETRY(LineStringZ, 0)
        );
    """)
    
    # meo_trees
    cur.execute("""
        CREATE TABLE meo_trees (
            id UUID PRIMARY KEY,
            geom GEOMETRY(PointZ, 0),
            shade_norm FLOAT
        );
    """)

    # meo_sample_points
    #
    # sequence_index and distance_from_start are not bookkeeping. They are what make an
    # edge's samples an ORDERED SERIES WITH A DIRECTION, which is the whole basis of the
    # directional cost query downstream (meo_edge_directional_cost, see
    # distributed/db/03_shard_schema.sql). Without them meo_exposure_samples below would
    # be an unordered bag of booleans and a per-edge sum really would be sufficient.
    cur.execute("""
        CREATE TABLE meo_sample_points (
            id UUID PRIMARY KEY,
            edge_id UUID REFERENCES meo_edges(id),
            sequence_index INT,
            distance_from_start FLOAT,
            geom GEOMETRY(PointZ, 0),
            tree_value FLOAT DEFAULT 0.0
        );
    """)

    # meo_exposure_samples — THE PRODUCT of the whole pipeline, and by far the largest
    # table (~1.577 billion rows / ~110 GB for a 12-day annual sweep).
    #
    # It is not an intermediate. A walker entering a 400 m street at 16:00 reaches the
    # sample 200 m in some two and a half minutes later, by which time the shadow has
    # moved — so walking east and walking west sample DIFFERENT (sample, time) pairs
    # against the same advancing clock, and the two costs genuinely differ. Both
    # directions cross the same samples, so a per-edge sum is identical for them; the
    # difference exists only in the ordered series. See docs/V1_PIPELINE.md.
    #
    # v2 keeps this schema exactly, column for column, and spreads the same rows across
    # ten PostgreSQL instances rather than shrinking them. Keeping them is the constraint
    # that forces the entire distributed design.
    #
    # Intentionally has no primary key: it is append-only via COPY, and a unique index at
    # this volume would dominate insert cost. Resumability is handled by the exporter
    # querying existing ids per timestamp instead.
    cur.execute("""
        CREATE TABLE meo_exposure_samples (
            sample_point_id UUID REFERENCES meo_sample_points(id),
            datetime TIMESTAMP,
            is_sunlit BOOLEAN
        );
        CREATE INDEX IF NOT EXISTS idx_meo_exp_samples_sample_id ON meo_exposure_samples(sample_point_id);
    """)

    # meo_exposure_edges — a DERIVED CONVENIENCE INDEX over the above, collapsing
    # per-sample booleans into one sunlit_sum per (edge, timestamp). ~2 GB instead of
    # ~110 GB, which is what makes the Pareto search's coarse "how sunlit is this edge
    # right now" objective an O(1) lookup.
    #
    # It answers that question and no other. It cannot answer "walked eastward from this
    # end, entering at 14:12, how many seconds in sun and what is the longest unbroken
    # stretch", because summing threw away the order. Both questions have consumers, so
    # both tables exist — and this is the one that can always be rebuilt from the other.
    cur.execute("""
        CREATE TABLE meo_exposure_edges (
            edge_id UUID REFERENCES meo_edges(id),
            datetime TIMESTAMP,
            sunlit_sum INT
        );
    """)

    print("Creating indices for performance...")
    # Spatial Indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_waypoints_geom ON meo_waypoints USING GIST (geom);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_edges_geom ON meo_edges USING GIST (geom);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_trees_geom ON meo_trees USING GIST (geom);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_sample_points_geom ON meo_sample_points USING GIST (geom);")
    
    # Search & Traversal Indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_edges_nodes ON meo_edges (start_wp_id, end_wp_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_sample_points_edge_seq ON meo_sample_points (edge_id, sequence_index);")
    
    # Temporal Indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_exposure_samples_time ON meo_exposure_samples (datetime);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_exposure_edges_time ON meo_exposure_edges (datetime);")
    
    # Foreign Key Indices (if not already covered)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_exp_samples_sample_id ON meo_exposure_samples(sample_point_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_meo_exp_edges_edge_id ON meo_exposure_edges(edge_id);")
    
    
    conn.commit()
    return conn, cur

def import_trees(cur):
    if not os.path.exists(TREE_CSV):
        print(f"Warning: {TREE_CSV} not found. Skipping trees.")
        return

    print(f"Importing trees from {TREE_CSV}...")
    with open(TREE_CSV, 'r') as f:
        reader = csv.DictReader(f)
        tree_batch = []
        for row in reader:
            tree_id = str(uuid.uuid4())
            ux = float(row['unity_x'])
            uy = GLOBAL_ELEVATION  # planar graph: one shared height for all geometry
            uz = float(row['unity_z'])
            sn = float(row['shade_norm'])
            # Argument order is (x, z, y) to match ST_MakePoint's PostGIS axis convention.
            tree_batch.append((tree_id, ux, uz, uy, sn))
            
            if len(tree_batch) >= 1000:
                cur.executemany("""
                    INSERT INTO meo_trees (id, geom, shade_norm)
                    VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s, %s), 0), %s)
                """, tree_batch)
                tree_batch = []
        
        if tree_batch:
            cur.executemany("""
                INSERT INTO meo_trees (id, geom, shade_norm)
                VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s, %s), 0), %s)
            """, tree_batch)
    print("Trees imported successfully.")

def import_roads(cur):
    path = ROAD_JSON
    if not os.path.exists(path):
        path = "road_map.json"
    
    if not os.path.exists(path):
        print(f"Error: Could not find {ROAD_JSON} or road_map.json")
        return

    print(f"Importing roads from {path}...")
    with open(path, 'r') as f:
        data = json.load(f)
    
    vertices = data.get('vertices', [])
    edges = data.get('edges', [])

    # 1. Decluster: flood-fill groups of vertices within CLUSTER_DIST of each other and keep the
    #    member nearest the group centroid as the single representative waypoint. Picking a real
    #    member rather than the centroid itself keeps the node on the road surface.
    #
    #    Perf note: the inner `for other in vertices` scan makes this O(n^2) (worse with the BFS
    #    queue), which is minutes-scale on a city-sized graph. A grid-based spatial hash keyed on
    #    CLUSTER_DIST would make it near-linear — worth doing before scaling to another city.
    processed_ids = set()
    representative_nodes = []
    old_to_new_uuid_map = {} 
    
    for v in vertices:
        if v['id'] in processed_ids:
            continue
            
        cluster = []
        queue = deque([v])
        processed_ids.add(v['id'])
        
        while queue:
            curr = queue.popleft()
            cluster.append(curr)
            for other in vertices:
                if other['id'] in processed_ids:
                    continue
                if get_distance(curr, other) < CLUSTER_DIST:
                    processed_ids.add(other['id'])
                    queue.append(other)
        
        avg_x = sum(c['x'] for c in cluster) / len(cluster)
        avg_y = sum(c['y'] for c in cluster) / len(cluster)
        avg_z = sum(c['z'] for c in cluster) / len(cluster)
        centroid = {'x': avg_x, 'y': avg_y, 'z': avg_z}
        
        best_node = cluster[0]
        min_dist = get_distance(best_node, centroid)
        for c in cluster:
            d = get_distance(c, centroid)
            if d < min_dist:
                min_dist = d
                best_node = c
        
        node_uuid = str(uuid.uuid4())
        representative_nodes.append({
            'uuid': node_uuid,
            'x': best_node['x'],
            'y': GLOBAL_ELEVATION,  # planar graph: one shared height for all nodes
            'z': best_node['z']
        })
        
        for c in cluster:
            old_to_new_uuid_map[c['id']] = node_uuid

    print(f"Created {len(representative_nodes)} declustered waypoints.")
    
    # Insert waypoints
    for vn in representative_nodes:
        cur.execute("""
            INSERT INTO meo_waypoints (id, geom)
            VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s, %s), 0))
        """, (vn['uuid'], vn['x'], vn['z'], vn['y']))

    # 2. Rewrite edges onto the declustered waypoints. sorted() canonicalises each undirected
    #    pair and the set dedupes, so edges that collapsed onto the same pair merge; edges whose
    #    endpoints collapsed onto the *same* waypoint become self-loops and are dropped.
    clean_edges = set()
    for e in edges:
        u_old, v_old = e['from'], e['to']
        if u_old in old_to_new_uuid_map and v_old in old_to_new_uuid_map:
            u_uuid = old_to_new_uuid_map[u_old]
            v_uuid = old_to_new_uuid_map[v_old]
            if u_uuid != v_uuid:
                edge_tuple = tuple(sorted((u_uuid, v_uuid)))
                clean_edges.add(edge_tuple)

    print(f"Created {len(clean_edges)} unique edges.")
    
    # Insert edges
    for e in clean_edges:
        edge_uuid = str(uuid.uuid4())
        u_uuid, v_uuid = e
        # Calculate length
        cur.execute("SELECT ST_X(geom), ST_Z(geom), ST_Y(geom) FROM meo_waypoints WHERE id = %s", (u_uuid,))
        p1 = cur.fetchone()
        cur.execute("SELECT ST_X(geom), ST_Z(geom), ST_Y(geom) FROM meo_waypoints WHERE id = %s", (v_uuid,))
        p2 = cur.fetchone()
        
        length = math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2 + (p1[2]-p2[2])**2)
        
        cur.execute("""
            INSERT INTO meo_edges (id, start_wp_id, end_wp_id, length, geom)
            VALUES (%s, %s, %s, %s, ST_MakeLine(
                (SELECT geom FROM meo_waypoints WHERE id = %s),
                (SELECT geom FROM meo_waypoints WHERE id = %s)
            ))
        """, (edge_uuid, u_uuid, v_uuid, length, u_uuid, v_uuid))

    print("Roads imported successfully.")

def main():
    conn = None
    try:
        conn, cur = initialize_database()
        import_trees(cur)
        import_roads(cur)
        conn.commit()
        print("\n--- PIPELINE PHASE 1 COMPLETE ---")
    except Exception as e:
        print(f"Critical error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()
