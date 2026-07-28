using System.Collections.Generic;
using UnityEngine;
using System;
using System.Linq;
using Npgsql;
using ShadowAware.Engines;

/// <summary>
/// Debug overlay that draws one sphere per DB sample point inside the current bounding box.
/// Cycles Off -> Tree (static canopy shade) -> Sunlight (dynamic exposure at the current
/// simulated time) -> Off.
/// </summary>
public class SampleVisualization : MonoBehaviour
{
    public enum VisualizationMode { Off, Tree, Sunlight }

    // sample_point_id -> its marker, so RefreshSunlightColors can recolour in place
    // instead of rebuilding thousands of GameObjects on every time step.
    private Dictionary<string, GameObject> markerDict = new Dictionary<string, GameObject>();
    public VisualizationMode CurrentMode { get; private set; } = VisualizationMode.Off;

    public void ToggleSamples(PostGISClient postGISClient, Bounds bbox, DateTime simTime, List<(int month, int day)> targetDates)
    {
        VisualizationMode oldMode = CurrentMode;
        if (CurrentMode == VisualizationMode.Off)
        {
            CurrentMode = VisualizationMode.Tree;
            LoadAndDrawSamples(postGISClient, bbox);
        }
        else if (CurrentMode == VisualizationMode.Tree)
        {
            CurrentMode = VisualizationMode.Sunlight;
            RefreshSunlightColors(postGISClient, simTime, targetDates);
        }
        else
        {
            CurrentMode = VisualizationMode.Off;
            ClearMarkers();
        }
        Debug.Log($"<color=cyan>[SampleVisualization]</color> Mode changed: {oldMode} -> {CurrentMode}");
    }

    private void LoadAndDrawSamples(PostGISClient postGISClient, Bounds bbox)
    {
        ClearMarkers();
        
        if (postGISClient == null) return;

        string connStr = postGISClient.GetConnectionString();
        
        Vector3 min = bbox.min;
        Vector3 max = bbox.max;

        int count = 0;

        try
        {
            using (var conn = new NpgsqlConnection(connStr))
            {
                conn.Open();
                
                string query = @"
                    SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom), tree_value
                    FROM meo_sample_points
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
                            count++;
                            string idStr = reader.GetGuid(0).ToString();
                            // PostGIS (X, Y, Z) -> Unity (x, z, y): columns 2 and 3 swap.
                            float x = (float)reader.GetDouble(1);
                            float z = (float)reader.GetDouble(2);
                            float y = (float)reader.GetDouble(3);
                            // tree_value is nullable until process_tree_data.py has run.
                            float treeVal = reader.IsDBNull(4) ? 0f : (float)reader.GetDouble(4);

                            Vector3 pos = new Vector3(x, y, z);
                            
                            GameObject marker = GameObject.CreatePrimitive(PrimitiveType.Sphere);
                            marker.name = $"Sample_{idStr}";
                            marker.transform.SetParent(this.transform);
                            marker.transform.position = pos;
                            marker.transform.localScale = Vector3.one * 3.0f;

                            if (marker.TryGetComponent<Renderer>(out var renderer))
                            {
                                // tree_value is clamped to [0,1] in the DB, but real values cluster
                                // near zero; the x2.5 gain spreads them across the visible ramp.
                                float amplifiedValue = Mathf.Clamp01(treeVal * 2.5f);
                                Color mapColor = Color.Lerp(Color.red, Color.blue, amplifiedValue);
                                renderer.material = new Material(Shader.Find("Unlit/Color")) { color = mapColor };
                            }

                            // Markers are display-only; a collider would interfere with the
                            // shadow raycasts and with the Editor's click-to-pick tooling.
                            Destroy(marker.GetComponent<SphereCollider>());
                            // Indexer, not Add(): a duplicate id would throw and abort the load.
                            markerDict[idStr] = marker;
                        }
                    }
                }
            }
            Debug.Log($"<color=green>[SampleVisualization]</color> Loaded {count} points (Mode: {CurrentMode}).");
        }
        catch (Exception ex)
        {
            Debug.LogError($"<color=red>[SampleVisualization] DB Error:</color> {ex.Message}");
        }
    }

    /// <summary>
    /// Recolours the loaded sample markers red/blue from `meo_exposure_samples` for the
    /// simulation time. Exposure only exists for exported dates, so the requested date is
    /// snapped to the nearest available one before querying.
    /// </summary>
    public void RefreshSunlightColors(PostGISClient postGISClient, DateTime simTime, List<(int month, int day)> targetDates)
    {
        if (CurrentMode != VisualizationMode.Sunlight) return;
        if (postGISClient == null) return; // called from Update(); must not throw

        // Find the closest target date from the provided list
        int targetMonth = simTime.Month;
        int targetDay = 1;

        if (targetDates != null && targetDates.Count > 0)
        {
            // Find globally closest date in the list by comparing full DateTime objects
            var closest = targetDates.OrderBy(dt => Math.Abs((new DateTime(2026, dt.month, dt.day) - new DateTime(2026, simTime.Month, simTime.Day)).TotalDays)).First();
            targetMonth = closest.month;
            targetDay = closest.day;
        }
        else
        {
            // Fallback to 1st or 15th logic if no list is provided
            targetDay = (simTime.Day < 8) ? 1 : 15;
            targetMonth = simTime.Month;
        }

        Debug.Log($"<color=yellow>[SunlightVisualization]</color> Current sim date: {simTime:MMM dd}. Closest DB match: {targetMonth}/{targetDay:D2}. Displaying exposure from {new DateTime(2026, targetMonth, targetDay):MMM dd}.");

        if (markerDict.Count == 0)
        {
            Debug.LogWarning("<color=orange>[SampleVisualization]</color> No markers loaded. Are you in an area with road network waypoints?");
            return;
        }

        // Snap down to the 3-minute grid the export loop wrote, so the equality match hits.
        // Year is pinned to 2026 to match the exported dataset.
        DateTime targetTime = new DateTime(2026, targetMonth, targetDay, simTime.Hour, (simTime.Minute / 3) * 3, 0);
        string connStr = postGISClient.GetConnectionString();

        try
        {
            using (var conn = new NpgsqlConnection(connStr))
            {
                conn.Open();
                
                // Fetch by timestamp only and discard ids we don't hold markers for. This rides
                // idx_meo_exposure_samples_time and avoids an IN clause with thousands of UUIDs,
                // at the cost of transferring every point for the timestamp (~365k city-wide).
                // For small viewports a bbox join against meo_sample_points would transfer less.
                string query = @"
                    SELECT sample_point_id, is_sunlit
                    FROM meo_exposure_samples
                    WHERE datetime = @targetTime;";

                using (var cmd = new NpgsqlCommand(query, conn))
                {
                    cmd.Parameters.AddWithValue("targetTime", targetTime);

                    using (var reader = cmd.ExecuteReader())
                    {
                        while (reader.Read())
                        {
                            string idStr = reader.GetGuid(0).ToString();
                            bool isSunlit = reader.GetBoolean(1);

                            if (markerDict.TryGetValue(idStr, out GameObject marker))
                            {
                                if (marker.TryGetComponent<Renderer>(out var renderer))
                                {
                                    // Sunlight: Exposed = Red, Shadow = Blue
                                    renderer.material.color = isSunlit ? Color.red : Color.blue;
                                }
                            }
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Debug.LogError($"<color=red>[SampleVisualization] Refresh Sunlight Error:</color> {ex.Message}");
        }
    }

    public void ClearMarkers()
    {
        foreach (var marker in markerDict.Values)
        {
            if (marker != null) Destroy(marker);
        }
        markerDict.Clear();
    }
    
    public bool AreSamplesVisible() => CurrentMode != VisualizationMode.Off;
}
