import psycopg2

# Configuration matching your local database
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def reset_simulation_values():
    conn = None
    try:
        print("Connecting to database...")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        # 1. Clear high-volume exposure data
        print("Truncating exposure tables (samples and aggregated edges)...")
        cur.execute("TRUNCATE TABLE meo_exposure_samples RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE meo_exposure_edges RESTART IDENTITY CASCADE;")
        
        # 2. Reset tree values on sample points (Keep the points, just reset the value)
        print("Resetting tree_value on all meo_sample_points to 0.0...")
        cur.execute("UPDATE meo_sample_points SET tree_value = 0.0;")
        
        # 3. Reset tree values on edges
        print("Resetting total_tree_value on all meo_edges to 0.0...")
        cur.execute("UPDATE meo_edges SET total_tree_value = 0.0;")
        
        conn.commit()
        print("\nSUCCESS: Simulation values have been reset.")
        print("Geometry Preserved: meo_sample_points and meo_edges remain intact.")
        print("Values Cleared: All sun exposure history and tree scores are now 0.0.")
        
    except Exception as e:
        print(f"Error resetting simulation values: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    reset_simulation_values()
