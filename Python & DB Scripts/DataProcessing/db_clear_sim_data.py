import psycopg2

# Configuration matching initialize_meo_pipeline.py
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def clear_simulation_data():
    conn = None
    try:
        print("Connecting to database...")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        # Tables to TRUNCATE (Simulation data that needs to be discarded)
        tables_to_clear = [
            "meo_exposure_samples", 
            "meo_exposure_edges", 
            "meo_sample_points"
        ]
        
        print(f"Truncating tables: {', '.join(tables_to_clear)}...")
        # RESTART IDENTITY resets any serial sequences if they exist
        cur.execute(f"TRUNCATE TABLE {', '.join(tables_to_clear)} RESTART IDENTITY CASCADE;")
        
        # Reset tree value aggregations on edges to 'discard' tree value data
        print("Resetting total_tree_value on meo_edges to 0.0...")
        cur.execute("UPDATE meo_edges SET total_tree_value = 0.0;")
        
        conn.commit()
        print("\nSUCCESS: Simulation data cleared.")
        print("Preserved: meo_waypoints, meo_edges (metadata), and meo_trees (raw points).")
        print("The database is now back to the state immediately following initialization.")
        
    except Exception as e:
        print(f"Error clearing simulation data: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    clear_simulation_data()
