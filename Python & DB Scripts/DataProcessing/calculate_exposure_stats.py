import psycopg2
import sys
from datetime import datetime, timedelta

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

# Simulation window known from previous context
START_HOUR = 3
END_HOUR = 21

def print_exposure_stats():
    try:
        print("Connecting to database...")
        sys.stdout.flush()
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Get all distinct dates
        print("Fetching unique dates...")
        sys.stdout.flush()
        cur.execute("SELECT DISTINCT datetime::date FROM meo_exposure_samples ORDER BY datetime::date;")
        dates = [row[0] for row in cur.fetchall()]
        
        total_dates = len(dates)
        print(f"Found {total_dates} dates to process.")
        print("-" * 60)
        sys.stdout.flush()

        for d_idx, d in enumerate(dates):
            date_str = d.strftime('%Y-%m-%d')
            print(f"\n>>> [{d_idx + 1}/{total_dates}] Processing Date: {date_str}")
            sys.stdout.flush()
            
            # Instead of one big query per day, we process hour by hour to show progressive progress
            for h in range(START_HOUR, END_HOUR + 1):
                start_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=h)
                end_dt = start_dt + timedelta(hours=1)
                
                # Progress calculation for the day
                progress = ((h - START_HOUR) / (END_HOUR - START_HOUR + 1)) * 100
                
                # Query exposure for this specific hour
                # Using direct comparison with timestamp is much faster than ::date casts
                cur.execute("""
                    SELECT datetime, SUM(CASE WHEN is_sunlit THEN 1 ELSE 0 END)
                    FROM meo_exposure_samples
                    WHERE datetime >= %s AND datetime < %s
                    GROUP BY datetime
                    ORDER BY datetime;
                """, (start_dt, end_dt))
                
                rows = cur.fetchall()
                
                if rows:
                    for dt, count in rows:
                        print(f"  [{dt}] Sunlit Points: {count}")
                    
                # Print progress bar/percentage after each hour
                sys.stdout.write(f"\r  Day Progress: {progress:6.2f}% | Hour {h:02d}:00 processed...")
                sys.stdout.flush()
            
            print(f"\r  Day Progress: 100.00% | Date {date_str} completed.         ")
            sys.stdout.flush()
                
        print("\nAll dates processed successfully.")
        
    except Exception as e:
        print(f"\nError occurred: {e}")
        sys.stdout.flush()
    finally:
        if 'cur' in locals(): cur.close()
        if 'conn' in locals(): conn.close()

if __name__ == "__main__":
    print_exposure_stats()
