using System;
using System.Collections.Generic;
using UnityEngine;
using Npgsql;

namespace ShadowAware.Engines
{
    [Serializable]
    public class TreePoint
    {
        public string id;
        public Vector3 position;
        public float shadeNorm;
    }

    /// <summary>
    /// Loads street trees for a bounding box. Note that trees do NOT participate in the Unity
    /// raycast pass — their shading is resolved entirely in SQL by process_tree_data.py, which
    /// sums `shade_norm` of all trees within 5 m of each sample point. This engine exists only
    /// so the canopy can be inspected visually in the scene.
    /// </summary>
    public class TreeEngine
    {
        public List<TreePoint> LoadedTrees { get; private set; } = new List<TreePoint>();

        public void LoadTreesFromPostGIS(string connectionString, Bounds bbox, float elevation)
        {
            float startTime = Time.realtimeSinceStartup;
            LoadedTrees.Clear();

            int fetchedCount = 0;

            // Use the absolute world-space bounds
            Vector3 min = bbox.min;
            Vector3 max = bbox.max;

            Debug.Log($"<color=green>[TreeEngine]</color> Fetching Global Trees. Area: X({min.x:F0} to {max.x:F0}), Z({min.z:F0} to {max.z:F0})");

            try
            {
                using (var conn = new NpgsqlConnection(connectionString))
                {
                    conn.Open();

                    string query = @"
                        SELECT id, ST_X(geom), ST_Y(geom), shade_norm 
                        FROM meo_trees 
                        WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0);";

                    using (var cmd = new NpgsqlCommand(query, conn))
                    {
                        cmd.Parameters.AddWithValue("minX", (double)min.x);
                        cmd.Parameters.AddWithValue("minZ", (double)min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)max.x);
                        cmd.Parameters.AddWithValue("maxZ", (double)max.z);

                        using (var reader = cmd.ExecuteReader())
                        {
                            while (reader.Read())
                            {
                                fetchedCount++;
                                string id = reader.GetGuid(0).ToString();
                                // PostGIS X -> Unity X, PostGIS Y -> Unity Z (height comes from
                                // the caller's globalElevation, not from the row).
                                float ux = (float)reader.GetDouble(1);
                                float uz = (float)reader.GetDouble(2);
                                // shade_norm may be NULL for rows imported without a value.
                                float sn = reader.IsDBNull(3) ? 0f : (float)reader.GetDouble(3);

                                // POSITIONING: Use provided global elevation
                                Vector3 globalPos = new Vector3(ux, elevation, uz); 

                                LoadedTrees.Add(new TreePoint {
                                    id = id,
                                    position = globalPos,
                                    shadeNorm = sn
                                });
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Debug.LogError($"<color=red>[TreeEngine] DB Error:</color> {ex.Message}");
            }

            Debug.Log($"<color=green>[TreeEngine] Done.</color> Total Trees Loaded: <b>{fetchedCount}</b> (Time: {(Time.realtimeSinceStartup - startTime):F3}s)");
        }
    }
}
