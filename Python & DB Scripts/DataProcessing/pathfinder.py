"""
Scratch utility: pulls a short 5-node chain out of the road graph near a hardcoded location,
handy for picking start/end test points to drop into the Unity scene.

Not part of the production pipeline — the real Pareto-optimal search runs in the
multi-objective backend against the precomputed meo_exposure_edges costs.
"""

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def find_path():
    conn = None  # bound before the try so the finally block cannot raise NameError
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Probe location in PostGIS axes (PostGIS X = Unity X, PostGIS Y = Unity Z).
        # -112 is the shared normalized elevation used for every graph node.
        target_x = -3300
        target_y = -1000

        print(f"Searching for nodes near ({target_x}, {target_y})...")
        
        # Find a starting node near the target
        cur.execute("""
            SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom)
            FROM meo_waypoints
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s, -112), 0)
            LIMIT 20;
        """, (target_x, target_y))
        
        starting_nodes = cur.fetchall()
        if not starting_nodes:
            print("No nodes found.")
            return

        for start_node in starting_nodes:
            node_id, x, z, y = start_node
            print(f"Trying start node: {node_id} at ({x}, {z}, {y})")
            
            # Simple DFS to find a path of 5 nodes
            path = [start_node]
            visited = {node_id}
            
            if find_next_node(cur, path, visited, 5):
                print("\nFound a path of 5 nodes:")
                for i, (nid, nx, nz, ny) in enumerate(path):
                    print(f"Node {i+1}: ID={nid}, UnityCoords=({nx}, {ny}, {nz})")
                
                print("\nConnections:")
                for i in range(len(path) - 1):
                    u = path[i][0]
                    v = path[i+1][0]
                    cur.execute("SELECT id FROM meo_edges WHERE (start_wp_id = %s AND end_wp_id = %s) OR (start_wp_id = %s AND end_wp_id = %s)", (u, v, v, u))
                    row = cur.fetchone()
                    # Subscripting fetchone() directly raised TypeError whenever the edge was
                    # missing (possible if the graph and edge table are out of sync).
                    edge_id = row[0] if row else "<not found>"
                    print(f"Edge {i+1}: {u} -> {v} (Edge ID: {edge_id})")
                return

        print("Could not find a path of length 5 starting from the nearest nodes.")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if conn:
            conn.close()

def find_next_node(cur, path, visited, target_length):
    if len(path) == target_length:
        return True
    
    current_node_id = path[-1][0]
    
    # Find neighbors
    cur.execute("""
        SELECT CASE 
            WHEN start_wp_id = %s THEN end_wp_id 
            ELSE start_wp_id 
        END as neighbor_id
        FROM meo_edges
        WHERE start_wp_id = %s OR end_wp_id = %s
    """, (current_node_id, current_node_id, current_node_id))
    
    # fetchall() up front: the recursive call below reuses this same cursor, which would
    # otherwise invalidate an open result set mid-iteration.
    neighbors = cur.fetchall()
    for (neighbor_id,) in neighbors:
        if neighbor_id not in visited:
            cur.execute("SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_waypoints WHERE id = %s", (neighbor_id,))
            neighbor_data = cur.fetchone()
            if neighbor_data is None:
                continue  # dangling edge reference; skip rather than append None

            path.append(neighbor_data)
            visited.add(neighbor_id)
            
            if find_next_node(cur, path, visited, target_length):
                return True
            
            visited.remove(neighbor_id)
            path.pop()
            
    return False

if __name__ == "__main__":
    find_path()
