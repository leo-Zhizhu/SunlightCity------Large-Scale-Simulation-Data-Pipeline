"""
Dumps every public table to CSV, rendering geometry columns as WKT via ST_AsText.

Intended for the small metadata tables (waypoints, edges, trees, sample points). NOT suitable
for meo_exposure_samples: the fetchall() below materialises the whole result set in memory and
that table holds ~1.57 billion rows. Use db_robust_export.ps1 (parallel pg_dump) for a full
backup instead.
"""

import psycopg2
import csv
import os

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

EXPORT_DIR = "db_export"

def export_all_tables():
    if not os.path.exists(EXPORT_DIR):
        os.makedirs(EXPORT_DIR)
        print(f"Created directory: {EXPORT_DIR}")

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Get all tables in the public schema
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    tables = [row[0] for row in cur.fetchall() if row[0] != 'spatial_ref_sys']

    for table in tables:
        print(f"Exporting table: {table}...")
        
        # Get columns and types
        cur.execute(f"""
            SELECT column_name, udt_name 
            FROM information_schema.columns 
            WHERE table_name = '{table}' AND table_schema = 'public'
            ORDER BY ordinal_position
        """)
        cols_info = cur.fetchall()
        
        select_parts = []
        headers = []
        for col, dtype in cols_info:
            headers.append(col)
            if dtype == 'geometry':
                select_parts.append(f"ST_AsText({col}) as {col}")
            else:
                select_parts.append(col)
        
        select_clause = ", ".join(select_parts)
        csv_file = os.path.join(EXPORT_DIR, f"{table}.csv")
        
        try:
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                
                cur.execute(f"SELECT {select_clause} FROM {table}")
                for row in cur.fetchall():
                    writer.writerow(row)
            
            print(f"  Successfully exported to {csv_file}")
        except Exception as e:
            print(f"  Error exporting {table}: {e}")

    conn.close()
    print("\nExport complete!")

if __name__ == "__main__":
    export_all_tables()
