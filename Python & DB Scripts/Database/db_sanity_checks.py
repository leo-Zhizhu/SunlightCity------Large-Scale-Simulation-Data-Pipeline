import psycopg2
import time

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def get_connection():
    return psycopg2.connect(**DB_CONFIG)

def check_data_volume():
    
    conn = get_connection()
    cursor = conn.cursor()
    start_time = time.time()
    print("Counting rows...")
    cursor.execute("SELECT count(*) FROM meo_exposure_samples;")
    count = cursor.fetchone()[0]
    end_time = time.time()
    print(f"Total rows: {count}")
    print(f"Time taken: {end_time - start_time:.2f} seconds")
    
    cursor.execute("SELECT datetime FROM meo_exposure_samples LIMIT 5;")
    print("Sample datetimes:")
    for row in cursor.fetchall():
        print(row)
    
    cursor.close()
    conn.close()

def check_date_range():
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT MIN(datetime), MAX(datetime) FROM meo_exposure_samples;")
    print(cursor.fetchone())
    cursor.close()
    conn.close()

def check_indices():
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            indexname,
            indexdef
        FROM
            pg_indexes
        WHERE
            tablename = 'meo_exposure_samples';
    """)
    for row in cursor.fetchall():
        print(row)
    cursor.close()
    conn.close()

def check_schema():
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'meo_exposure_samples';")
    for row in cursor.fetchall():
        print(row)
    cursor.close()
    conn.close()


if __name__ == "__main__":
    print("--- Running Database Sanity Checks ---")
    try: check_data_volume()
    except Exception as e: print("Data volume check missing/failed:", e)
    try: check_date_range()
    except Exception as e: print("Date range check missing/failed:", e)
    try: check_indices()
    except Exception as e: print("Indices check missing/failed:", e)
    try: check_schema()
    except Exception as e: print("Schema check missing/failed:", e)
    print("--- Checks Complete ---")
