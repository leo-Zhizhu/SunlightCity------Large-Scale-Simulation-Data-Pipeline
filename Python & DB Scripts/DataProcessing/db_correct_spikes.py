"""
Post-process pass that removes false-positive sunlight at the edges of the day.

Even with the shallow-angle guard in ShadowEngine.IsInShadow, near-horizon rays occasionally
escape the city mesh entirely and register as "sunlit" — producing an implausible burst of
exposure right at sunrise or sunset. Rather than re-running six hours of simulation, these
two passes detect the burst in the aggregated time series and zero it in place.

Both passes rewrite meo_exposure_samples AND the derived meo_exposure_edges so the two stay
consistent. Destructive and not idempotent-safe in the sense that it cannot be undone — take a
dump first if the data matters.
"""

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

def fix_sunrise_spikes():
    """Morning heuristic: find the first non-zero time step, look ahead 15 steps (45 min) for a
    local minimum, and if the opening value exceeds that minimum by more than 1000 sunlit points
    treat everything before the minimum as a spike. A genuine sunrise ramps *up*, so a large
    immediate drop can only be numerical noise."""
    print("Connecting to database...")
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    try:
        cur.execute("SELECT DISTINCT datetime::date FROM meo_exposure_samples ORDER BY datetime::date;")
        dates = [row[0] for row in cur.fetchall()]
        
        total_samples_zeroed = 0
        total_edges_zeroed = 0

        for d in dates:
            print(f"\nAnalyzing date: {d}")
            # Fetch time series of sunlit counts for the morning (before 12:00)
            cur.execute("""
                SELECT datetime::time, SUM(CASE WHEN is_sunlit THEN 1 ELSE 0 END)
                FROM meo_exposure_samples
                WHERE datetime::date = %s AND datetime::time <= '12:00:00'
                GROUP BY datetime::time
                ORDER BY datetime::time;
            """, (d,))
            
            timeseries = cur.fetchall()
            if not timeseries:
                continue
                
            # Detect sunrise spikes:
            # 1. Find the first time step where count > 0
            # 2. Find the local minimum in the next 15 time steps (45 minutes)
            # 3. If the initial count is significantly higher than the local minimum (e.g. > 1000 diff),
            #    then all time steps before the local minimum are abnormal spikes.
            
            first_nonzero_idx = -1
            for i, (t, cnt) in enumerate(timeseries):
                if cnt > 0:
                    first_nonzero_idx = i
                    break
            
            if first_nonzero_idx == -1:
                print("  No sunlight in the morning.")
                continue
                
            # Look ahead for a local minimum
            search_end = min(len(timeseries), first_nonzero_idx + 15)
            
            local_min_val = timeseries[first_nonzero_idx][1]
            local_min_idx = first_nonzero_idx
            
            for i in range(first_nonzero_idx + 1, search_end):
                if timeseries[i][1] < local_min_val:
                    local_min_val = timeseries[i][1]
                    local_min_idx = i
            
            initial_val = timeseries[first_nonzero_idx][1]
            spikes_to_zero = []
            
            if initial_val - local_min_val > 1000:
                # We found a massive drop after the initial burst!
                # Everything from first_nonzero_idx up to (but not including) local_min_idx is a spike.
                for i in range(first_nonzero_idx, local_min_idx):
                    if timeseries[i][1] > 0:
                        spikes_to_zero.append(timeseries[i][0])
            
            if spikes_to_zero:
                print(f"  Detected abnormal morning spikes at times: {[str(t) for t in spikes_to_zero]}")
                print(f"  (Initial burst: {initial_val}, Dropped to local minimum: {local_min_val} at {timeseries[local_min_idx][0]})")
                
                for t in spikes_to_zero:
                    print(f"  -> Zeroing out data for {d} {t}...")
                    
                    # 1. Update meo_exposure_samples
                    cur.execute("""
                        UPDATE meo_exposure_samples 
                        SET is_sunlit = false 
                        WHERE datetime::date = %s AND datetime::time = %s AND is_sunlit = true;
                    """, (d, t))
                    samples_updated = cur.rowcount
                    total_samples_zeroed += samples_updated
                    
                    # 2. Update meo_exposure_edges
                    cur.execute("""
                        UPDATE meo_exposure_edges
                        SET sunlit_sum = 0
                        WHERE datetime::date = %s AND datetime::time = %s AND sunlit_sum > 0;
                    """, (d, t))
                    edges_updated = cur.rowcount
                    total_edges_zeroed += edges_updated
                    
                    print(f"     Updated {samples_updated} sample points and {edges_updated} edges.")
                    
            else:
                print("  No abnormal spikes detected.")

        conn.commit()
        print(f"\nSUCCESS! Fixed all morning spikes. Total sample points zeroed: {total_samples_zeroed}. Total edges zeroed: {total_edges_zeroed}")

    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

def fix_sunset_spikes():
    """Afternoon heuristic: after 14:00 the sunlit count should decrease monotonically as the sun
    descends, so ANY increase is treated as an artefact.

    Caveat: this is deliberately aggressive. In a real city an increase can be legitimate (the sun
    clearing a tall building and re-lighting a street). Review the printed timestamps before
    trusting the result on a new dataset."""
    print("Connecting to database...")
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    try:
        # Get all distinct dates
        cur.execute("SELECT DISTINCT datetime::date FROM meo_exposure_samples ORDER BY datetime::date;")
        dates = [row[0] for row in cur.fetchall()]
        
        total_samples_zeroed = 0
        total_edges_zeroed = 0

        for d in dates:
            print(f"\nAnalyzing date: {d}")
            # Fetch time series of sunlit counts for the afternoon (after 14:00)
            cur.execute("""
                SELECT datetime::time, SUM(CASE WHEN is_sunlit THEN 1 ELSE 0 END)
                FROM meo_exposure_samples
                WHERE datetime::date = %s AND datetime::time >= '14:00:00'
                GROUP BY datetime::time
                ORDER BY datetime::time;
            """, (d,))
            
            timeseries = cur.fetchall()
            if not timeseries:
                continue
                
            # Detect spikes:
            # We look for times where the count goes UP compared to the previous time, 
            # and it's shortly before it drops to 0.
            # Normal afternoon behavior: counts should strictly decrease.
            
            spikes_to_zero = []
            
            for i in range(1, len(timeseries)):
                prev_time, prev_count = timeseries[i-1]
                curr_time, curr_count = timeseries[i]
                
                # If sunlight increases in the late afternoon, it's a raycast edge-of-world bug
                if curr_count > prev_count and curr_count > 0:
                    spikes_to_zero.append(curr_time)
            
            if spikes_to_zero:
                print(f"  Detected abnormal spikes at times: {[str(t) for t in spikes_to_zero]}")
                
                for t in spikes_to_zero:
                    print(f"  -> Zeroing out data for {d} {t}...")
                    
                    # 1. Update meo_exposure_samples
                    cur.execute("""
                        UPDATE meo_exposure_samples 
                        SET is_sunlit = false 
                        WHERE datetime::date = %s AND datetime::time = %s AND is_sunlit = true;
                    """, (d, t))
                    samples_updated = cur.rowcount
                    total_samples_zeroed += samples_updated
                    
                    # 2. Update meo_exposure_edges
                    cur.execute("""
                        UPDATE meo_exposure_edges
                        SET sunlit_sum = 0
                        WHERE datetime::date = %s AND datetime::time = %s AND sunlit_sum > 0;
                    """, (d, t))
                    edges_updated = cur.rowcount
                    total_edges_zeroed += edges_updated
                    
                    print(f"     Updated {samples_updated} sample points and {edges_updated} edges.")
                    
            else:
                print("  No abnormal spikes detected.")

        conn.commit()
        print(f"\nSUCCESS! Fixed all spikes. Total sample points zeroed: {total_samples_zeroed}. Total edges zeroed: {total_edges_zeroed}")

    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# Single entry point. The file previously carried three separate `if __name__` blocks, so
# running it executed fix_sunrise_spikes() twice and fix_sunset_spikes() twice — the second
# pass re-scanned the already-corrected data and reported misleading "0 spikes" totals.
if __name__ == "__main__":
    print("Running spike corrections...")
    fix_sunrise_spikes()
    fix_sunset_spikes()
    print("Done.")
