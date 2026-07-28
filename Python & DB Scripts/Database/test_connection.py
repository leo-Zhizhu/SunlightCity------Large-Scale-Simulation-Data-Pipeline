import psycopg2
try:
    conn = psycopg2.connect(
        host="localhost",
        port=5432,
        database="city_data",
        user="admin",
        password="password"
    )
    print("Connection successful!")
    cur = conn.cursor()
    cur.execute("SELECT postgis_full_version();")
    print("PostGIS Version:", cur.fetchone()[0])
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
    tables = cur.fetchall()
    print("Tables:", [t[0] for t in tables])
    conn.close()
except Exception as e:
    print("Connection failed:", e)
