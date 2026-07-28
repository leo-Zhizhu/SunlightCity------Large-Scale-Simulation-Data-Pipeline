import psycopg2
import sys
import uuid

# --- CONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def find_node_coordinates(node_uuid):
    try:
        # Validate UUID format
        val = uuid.UUID(node_uuid)
    except ValueError:
        print(f"Error: '{node_uuid}' is not a valid UUID.")
        return

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Tables to search in
        tables = ["meo_waypoints", "meo_trees", "meo_sample_points"]
        
        found = False
        for table in tables:
            # Query for X, Y, Z coordinates
            # Note: Based on initialize_meo_pipeline.py:
            # PostGIS X = Unity X
            # PostGIS Y = Unity Z
            # PostGIS Z = Unity Y (Elevation)
            query = f"SELECT ST_X(geom), ST_Z(geom), ST_Y(geom) FROM {table} WHERE id = %s"
            cur.execute(query, (node_uuid,))
            result = cur.fetchone()

            if result:
                x, y, z = result
                print(f"\nNode found in table: {table}")
                print(f"UUID: {node_uuid}")
                print("-" * 30)
                print(f"Unity X (PostGIS X): {x}")
                print(f"Unity Y (PostGIS Z): {y}")
                print(f"Unity Z (PostGIS Y): {z}")
                print("-" * 30)
                print(f"Vector3({x}, {y}, {z})")
                found = True
                break
        
        if not found:
            print(f"Node with UUID {node_uuid} not found in database.")

    except Exception as e:
        print(f"Database error: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python find_node_coords.py <UUID>")
        print("Example: python find_node_coords.py 550e8400-e29b-41d4-a716-446655440000")
    else:
        node_id = sys.argv[1]
        find_node_coordinates(node_id)
