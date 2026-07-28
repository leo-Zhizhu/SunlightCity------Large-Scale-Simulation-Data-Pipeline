"""
Pipeline phase 3 — resolve static tree shade entirely inside PostgreSQL.

Unlike dynamic solar exposure (which needs Unity's physics engine), canopy shade is
time-invariant, so it is computed as a 2D spatial join instead of by raycasting. That keeps
1.28M trees out of the Unity simulation loop completely.

Two passes:
  1. For every sample point, sum shade_norm of all trees within SEARCH_RADIUS, clamped to 1.0
     so overlapping canopies in a dense park cannot inflate the value past full coverage.
  2. Roll those per-point values up into one total_tree_value per routing edge, which the
     multi-objective search then uses as a deterministic cost modifier.

Both statements are set-based whole-table UPDATEs; they rely on idx_meo_trees_geom and
idx_meo_sample_points_edge_seq and take a while on the full dataset. Re-running is safe —
each pass recomputes from scratch rather than accumulating.
"""

import psycopg2
import time

# --- CONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "city_data",
    "user": "admin",
    "password": "password"
}

SEARCH_RADIUS = 5.0  # meters — canopy influence radius around each sample point

def process_phase3():
    conn = None
    cur = None  # bound before the try: the finally block closes it unconditionally
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        print("Starting Phase 3: Fast Static Processing (Tree Values)...")

        # Indexes are created by db_pipeline_initializer.py during initialization,
        # for performance and consistency.

        # 1. Update Sample Points Tree Value (2D Distance)
        print(f"Calculating tree values for sample points within {SEARCH_RADIUS}m radius...")
        start_time = time.time()
        
        # ST_DWithin and ST_Distance on GEOMETRY perform 2D Cartesian calculations, 
        # effectively ignoring the Z/Y vertical height coordinate as requested.
        update_samples_query = f"""
            UPDATE meo_sample_points s
            SET tree_value = LEAST(COALESCE((
                SELECT SUM(t.shade_norm)
                FROM meo_trees t
                WHERE ST_DWithin(s.geom, t.geom, %s)
            ), 0.0), 1.0);
        """
        cur.execute(update_samples_query, (SEARCH_RADIUS,))
        conn.commit()
        print(f"Sample points updated in {time.time() - start_time:.2f}s")
        
        # 2. Aggregate Total Tree Value to Edges
        print("Aggregating tree values from sample points into edges...")
        start_time = time.time()
        update_edges_query = """
            UPDATE meo_edges e
            SET total_tree_value = COALESCE((
                SELECT SUM(s.tree_value)
                FROM meo_sample_points s
                WHERE s.edge_id = e.id
            ), 0.0);
        """
        cur.execute(update_edges_query)
        conn.commit()
        print(f"Edges aggregated in {time.time() - start_time:.2f}s")
        
        print("\n--- PHASE 3 COMPLETE ---")
        
    except Exception as e:
        print(f"Error during Phase 3 processing: {e}")
        if conn:
            conn.rollback()
    finally:
        # Close each independently: if psycopg2.connect() itself failed, `cur` was never
        # assigned and the old `if conn: cur.close()` raised NameError, masking the real error.
        if cur:
            cur.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    process_phase3()
