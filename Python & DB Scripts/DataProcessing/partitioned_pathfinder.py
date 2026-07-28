"""
Splits the city's waypoints into 20 spatial blocks with k-means and picks one representative
5-node path near the centre of each. Used to generate an evenly distributed set of test routes
for benchmarking, so results aren't all drawn from the same neighbourhood.

Writes a plain-text report to scratch/partition_report.txt.
"""

import psycopg2
import random
import math
import os
import collections

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

NUM_BLOCKS = 20
PATH_LENGTH = 5
REPORT_PATH = os.path.join("scratch", "partition_report.txt")

# Fixed seed so successive runs choose the same blocks and the benchmark set is reproducible.
RANDOM_SEED = 20260728


def get_distance(p1, p2):
    """2D distance on the horizontal plane (PostGIS X/Y == Unity X/Z)."""
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

def kmeans(points, k, iterations=20):
    # Plain Lloyd's algorithm, fixed iteration count, no convergence early-exit.
    # Initialize centroids randomly from points
    centroids = random.sample(points, k)
    clusters = [[] for _ in range(k)]
    
    for i in range(iterations):
        clusters = [[] for _ in range(k)]
        # Assignment step
        for p in points:
            distances = [get_distance(p, c) for c in centroids]
            cluster_idx = distances.index(min(distances))
            clusters[cluster_idx].append(p)
        
        # Update step
        new_centroids = []
        for j in range(k):
            if not clusters[j]:
                # If a cluster is empty, re-initialize its centroid
                new_centroids.append(random.choice(points))
                continue
            
            avg_x = sum(p[0] for p in clusters[j]) / len(clusters[j])
            avg_y = sum(p[1] for p in clusters[j]) / len(clusters[j])
            new_centroids.append((avg_x, avg_y))
        
        # If centroids didn't change much, we could break early
        centroids = new_centroids
        print(f"K-means iteration {i+1}/{iterations} complete...")
        
    return centroids, clusters

def find_path_of_length_5(adj, start_nodes, target_length=5):
    """Simple DFS to find a simple path of `target_length` nodes within one cluster.

    Note the per-branch `visited.copy()`: this is an exhaustive search over simple paths, so
    cost grows with branching factor. It is only tractable because target_length is tiny (5)
    and returns on the first hit."""
    for start_node in start_nodes:
        stack = [(start_node, [start_node], {start_node})]
        while stack:
            u, path, visited = stack.pop()
            if len(path) == target_length:
                return path
            
            # Neighbors that are in the cluster and not visited
            for v in adj.get(u, []):
                if v not in visited:
                    new_visited = visited.copy()
                    new_visited.add(v)
                    stack.append((v, path + [v], new_visited))
    return None

def main():
    conn = None
    try:
        random.seed(RANDOM_SEED)

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        print("Fetching waypoints...")
        cur.execute("SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_waypoints")
        waypoint_rows = cur.fetchall()
        if not waypoint_rows:
            print("No waypoints found — run db_pipeline_initializer.py first.")
            return

        # Storage: {id: (x, z, y)} - PostGIS X -> Unity X, PostGIS Y -> Unity Z, PostGIS Z -> Unity Y
        waypoints = {row[0]: (row[1], row[2], row[3]) for row in waypoint_rows}

        # Points for K-means: (x, z, id) — clustering happens on the 2D horizontal plane only.
        points = [(row[1], row[2], row[0]) for row in waypoint_rows]

        # random.sample() inside kmeans() needs at least k distinct points.
        k = min(NUM_BLOCKS, len(points))

        print(f"Partitioning {len(points)} waypoints into {k} blocks using K-means...")
        centroids, clusters_raw = kmeans([(p[0], p[1]) for p in points], k=k)

        # Re-assign full point data (with IDs) to clusters
        clusters = [[] for _ in range(k)]
        for p_full in points:
            p_pos = (p_full[0], p_full[1])
            distances = [get_distance(p_pos, c) for c in centroids]
            cluster_idx = distances.index(min(distances))
            clusters[cluster_idx].append(p_full)
            
        print("Fetching edges...")
        cur.execute("SELECT start_wp_id, end_wp_id FROM meo_edges")
        edges = cur.fetchall()
        
        # Build global adjacency
        full_adj = collections.defaultdict(list)
        for u, v in edges:
            full_adj[u].append(v)
            full_adj[v].append(u)
            
        report = []
        
        for i, cluster in enumerate(clusters):
            if not cluster:
                continue
            
            centroid = centroids[i]
            # Range
            min_x = min(p[0] for p in cluster)
            max_x = max(p[0] for p in cluster)
            min_z = min(p[1] for p in cluster)
            max_z = max(p[1] for p in cluster)
            
            # Find path centered in block
            # Sort nodes by distance to centroid
            sorted_nodes = sorted(cluster, key=lambda p: get_distance((p[0], p[1]), centroid))
            cluster_node_ids = {p[2] for p in cluster}
            
            # Adjacency restricted to cluster
            cluster_adj = collections.defaultdict(list)
            for node_id in cluster_node_ids:
                for neighbor in full_adj.get(node_id, []):
                    if neighbor in cluster_node_ids:
                        cluster_adj[node_id].append(neighbor)
            
            # Try to find a path of 5 starting from nodes closest to center
            start_ids = [p[2] for p in sorted_nodes]
            path_ids = find_path_of_length_5(cluster_adj, start_ids, PATH_LENGTH)
            
            block_data = {
                "block_id": i + 1,
                "range": {"min_x": min_x, "max_x": max_x, "min_z": min_z, "max_z": max_z},
                "centroid": centroid,
                "path": []
            }
            
            if path_ids:
                for pid in path_ids:
                    x, z, y = waypoints[pid]
                    block_data["path"].append({
                        "id": pid,
                        "unity_coords": (x, y, z)
                    })
            
            report.append(block_data)
            
        # Create the output directory first — writing straight to "scratch/..." raised
        # FileNotFoundError on a fresh checkout after all the clustering work was done.
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)

        with open(REPORT_PATH, "w") as f:
            f.write("=== WAYPOINT PARTITION REPORT ===\n\n")
            for b in report:
                f.write(f"Block {b['block_id']}:\n")
                f.write(f"  Range: X[{b['range']['min_x']:.2f}, {b['range']['max_x']:.2f}], Z[{b['range']['min_z']:.2f}, {b['range']['max_z']:.2f}]\n")
                f.write(f"  Centroid: ({b['centroid'][0]:.2f}, {b['centroid'][1]:.2f})\n")
                if b['path']:
                    f.write(f"  Selected Path ({PATH_LENGTH} Nodes near center):\n")
                    for node in b['path']:
                         f.write(f"    - ID: {node['id']}, Coords: {node['unity_coords']}\n")
                else:
                    f.write(f"  No path of length {PATH_LENGTH} found within this block.\n")
                f.write("\n")
                
        print(f"Success! Report written to {REPORT_PATH}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    main()
