import psycopg2

# Configuration from initialize_meo_pipeline.py
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def clear_data():
    conn = None
    try:
        print("Connecting to database...")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        tables = [
            "meo_exposure_samples", 
            "meo_exposure_edges", 
            "meo_sample_points", 
            "meo_edges", 
            "meo_waypoints", 
            "meo_trees"
        ]
        print(f"Clearing tables: {', '.join(tables)}...")
        
        # Using CASCADE to handle any unexpected foreign key constraints
        cur.execute(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE;")
        
        conn.commit()
        print("Successfully cleared all requested tables.")
        
    except Exception as e:
        print(f"Error clearing data: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    clear_data()
