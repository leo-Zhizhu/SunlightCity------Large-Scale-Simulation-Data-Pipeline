using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEngine;
using UnityEngine.AI;
using Npgsql;

namespace ShadowAware.Engines
{
    [Serializable]
    public class RoadGraphData
    {
        public List<RoadVertex> vertices;
        public List<RoadEdge> edges;
    }

    [Serializable]
    public class RoadVertex
    {
        public string id;
        public float x, y, z;
    }

    [Serializable]
    public class RoadEdge
    {
        public string from;
        public string to;
    }

    public class WaypointEngine
    {
        public List<(string id, Vector3 pos)> StoredPoints { get; private set; } = new();
        public List<(string from, string to)> StoredEdges { get; private set; } = new();
        public List<(Guid id, Vector3 pos)> LoadedSamples { get; private set; } = new();
        public Bounds CurrentBBox { get; private set; }

        public void LoadWaypointsFromPostGIS(string connectionString, Transform start, Transform end, float bboxPadding, float elevation)
        {
            float startTime = Time.realtimeSinceStartup;
            StoredPoints.Clear(); StoredEdges.Clear();
            
            Vector3 min = Vector3.Min(start.position, end.position) - Vector3.one * bboxPadding;
            Vector3 max = Vector3.Max(start.position, end.position) + Vector3.one * bboxPadding;
            CurrentBBox = new Bounds((min + max) * 0.5f, max - min);

            Debug.Log($"<color=orange>[WaypointEngine]</color> Initializing PostGIS load for area: {CurrentBBox.min} to {CurrentBBox.max}");

            List<RoadVertex> dbVertices = new List<RoadVertex>();
            List<RoadEdge> dbEdges = new List<RoadEdge>();

            try {
                using (var conn = new NpgsqlConnection(connectionString)) {
                    conn.Open();
                    string nodeQuery = @"SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_waypoints 
                                       WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0);";
                    using (var cmd = new NpgsqlCommand(nodeQuery, conn)) {
                        cmd.Parameters.AddWithValue("minX", (double)min.x); cmd.Parameters.AddWithValue("minZ", (double)min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)max.x); cmd.Parameters.AddWithValue("maxZ", (double)max.z);
                        using (var reader = cmd.ExecuteReader()) {
                            while (reader.Read()) {
                                dbVertices.Add(new RoadVertex { id = reader.GetGuid(0).ToString(), x = (float)reader.GetDouble(1), z = (float)reader.GetDouble(2), y = (float)reader.GetDouble(3) });
                            }
                        }
                    }
                    string edgeQuery = @"SELECT start_wp_id, end_wp_id FROM meo_edges 
                                       WHERE start_wp_id IN (SELECT id FROM meo_waypoints WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0))
                                         AND end_wp_id IN (SELECT id FROM meo_waypoints WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0));";
                    using (var cmd = new NpgsqlCommand(edgeQuery, conn)) {
                        cmd.Parameters.AddWithValue("minX", (double)min.x); cmd.Parameters.AddWithValue("minZ", (double)min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)max.x); cmd.Parameters.AddWithValue("maxZ", (double)max.z);
                        using (var reader = cmd.ExecuteReader()) {
                            while (reader.Read()) dbEdges.Add(new RoadEdge { from = reader.GetGuid(0).ToString(), to = reader.GetGuid(1).ToString() });
                        }
                    }
                }
            }
            catch (Exception ex) {
                Debug.LogError($"<color=red>[WaypointEngine] DB Error:</color> {ex.Message}");
                return;
            }

            if (dbVertices.Count > 0) {
                ProcessGraphData(dbVertices, dbEdges, start.position, end.position, elevation);
                ConnectEndpoint("START", start.position, StoredPoints.Where(p => p.id != "START" && p.id != "END").ToList());
                ConnectEndpoint("END", end.position, StoredPoints.Where(p => p.id != "START" && p.id != "END").ToList());
            }
            Debug.Log($"<color=orange>[WaypointEngine]</color> Load complete. Nodes: {StoredPoints.Count}, Edges: {StoredEdges.Count}");
        }

        public void LoadSamplesFromPostGIS(string connectionString, Bounds bbox, float elevation)
        {
            LoadedSamples.Clear();
            try {
                using (var conn = new NpgsqlConnection(connectionString)) {
                    conn.Open();
                    string query = @"SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_sample_points 
                                   WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0);";
                    using (var cmd = new NpgsqlCommand(query, conn)) {
                        cmd.Parameters.AddWithValue("minX", (double)bbox.min.x); cmd.Parameters.AddWithValue("minZ", (double)bbox.min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)bbox.max.x); cmd.Parameters.AddWithValue("maxZ", (double)bbox.max.z);
                        using (var reader = cmd.ExecuteReader()) {
                            while (reader.Read()) {
                                Guid id = reader.GetGuid(0);
                                Vector3 pos = new Vector3((float)reader.GetDouble(1), (float)reader.GetDouble(3), (float)reader.GetDouble(2));
                                // Overwrite height with global elevation for consistency
                                pos.y = elevation + 0.1f;
                                LoadedSamples.Add((id, pos));
                            }
                        }
                    }
                }
                Debug.Log($"<color=green>[WaypointEngine]</color> Loaded {LoadedSamples.Count} sample points from DB.");
            } catch (Exception ex) { Debug.LogError($"[WaypointEngine] Sample Load Error: {ex.Message}"); }
        }

        private void ProcessGraphData(List<RoadVertex> vertices, List<RoadEdge> edges, Vector3 sPos_raw, Vector3 ePos_raw, float elevation)
        {
            HashSet<string> pIds = new();
            foreach (var v in vertices) {
                Vector3 pos = new Vector3(v.x, elevation + 0.1f, v.z);
                StoredPoints.Add((v.id, pos)); pIds.Add(v.id);
            }

            Vector3 sPos = sPos_raw;
            sPos.y = elevation + 0.1f;

            Vector3 ePos = ePos_raw;
            ePos.y = elevation + 0.1f;

            StoredPoints.Add(("START", sPos)); StoredPoints.Add(("END", ePos));
            pIds.Add("START"); pIds.Add("END");

            // Deduplicate undirected edges via a canonical (min, max) key. The previous
            // StoredEdges.Any(...) scan was O(E^2) — ~45M comparisons at 6,700 edges.
            var seen = new HashSet<(string, string)>();
            foreach (var e in edges) {
                if (!pIds.Contains(e.from) || !pIds.Contains(e.to)) continue;

                var key = string.CompareOrdinal(e.from, e.to) <= 0 ? (e.from, e.to) : (e.to, e.from);
                if (seen.Add(key)) StoredEdges.Add((e.from, e.to));
            }
        }

        /// <summary>
        /// Offline fallback used when no PostGIS connection is available. Reads the
        /// road_graph.json produced by <c>RoadGraphExtractor</c>.
        /// </summary>
        public void LoadWaypointsFromJson(Transform start, Transform end, float bboxPadding, float elevation)
        {
            StoredPoints.Clear(); StoredEdges.Clear();
            Vector3 min = Vector3.Min(start.position, end.position) - Vector3.one * bboxPadding;
            Vector3 max = Vector3.Max(start.position, end.position) + Vector3.one * bboxPadding;
            CurrentBBox = new Bounds((min + max) * 0.5f, max - min);

            // RoadGraphExtractor writes into Assets/ (Application.dataPath); check there too,
            // otherwise a freshly generated graph is silently never found.
            string path = Path.Combine(Application.persistentDataPath, "road_graph.json");
            if (!File.Exists(path)) path = Path.Combine(Application.dataPath, "road_graph.json");
            if (!File.Exists(path))
            {
                Debug.LogWarning("[WaypointEngine] No road_graph.json found in persistentDataPath or dataPath. " +
                                 "Generate one via RoadGraphExtractor -> 'Generate Graph + Export JSON'.");
                return;
            }

            RoadGraphData data = JsonUtility.FromJson<RoadGraphData>(File.ReadAllText(path));
            if (data?.vertices != null) {
                var bboxVertices = data.vertices.Where(v => CurrentBBox.Contains(new Vector3(v.x, v.y, v.z))).ToList();
                ProcessGraphData(bboxVertices, data.edges, start.position, end.position, elevation);
                ConnectEndpoint("START", start.position, StoredPoints.Where(p => p.id != "START" && p.id != "END").ToList());
                ConnectEndpoint("END", end.position, StoredPoints.Where(p => p.id != "START" && p.id != "END").ToList());
            }
        }

        private void ConnectEndpoint(string endId, Vector3 endPos, List<(string id, Vector3 pos)> targets)
        {
            if (targets.Count == 0) return;
            var closest = targets.OrderBy(t => Vector3.Distance(endPos, t.pos)).First();
            StoredEdges.Add((endId, closest.id));
            Debug.Log($"[WaypointEngine] Connected ID {endId} to closest waypoint {closest.id}.");
        }

        public void WritePointsCsv()
        {
            string path = Path.Combine(Application.persistentDataPath, "path_points.csv");
            using (var sw = new StreamWriter(path, false))
            {
                sw.WriteLine("point_id,x,y,z");
                foreach (var p in StoredPoints) sw.WriteLine($"{p.id},{p.pos.x:F4},{p.pos.y:F4},{p.pos.z:F4}");
            }
        }
    }

    public class ShadowEngine
    {
        /// <summary>
        /// The single physics test behind the whole dataset: is <paramref name="pos"/> lit, or
        /// occluded by geometry on the caster/ground layers?
        ///
        /// Two things are worth knowing:
        ///
        /// * <b>Shallow-angle guard.</b> Unity reports rotations through eulerAngles in [0, 360),
        ///   so a sun 10° below the horizon reads as 350°, not -10°. The single
        ///   `>= 180 - threshold` test therefore covers the whole below-horizon arc (roughly
        ///   270°–360°) as well as dusk, while `<= threshold` covers dawn. Near the horizon a
        ///   ray would have to travel kilometres down a street canyon, where float precision
        ///   degrades and the ray can escape the mesh entirely and report a false "sunlit".
        ///   Declaring those steps shadowed is both cheaper and more correct.
        ///
        /// * <b>Boolean early-out.</b> We only need "hit anything?", so the raycast returns on
        ///   the first BVH intersection instead of sorting all hits.
        /// </summary>
        public bool IsInShadow(Vector3 pos, Light sun, LayerMask caster, LayerMask ground, float elevation, float threshold)
        {
            if (!sun) return false;
            float elevation_angle = sun.transform.eulerAngles.x;
            if (elevation_angle <= threshold || elevation_angle >= 180f - threshold) return true;

            // Normalise to the shared planar elevation, then lift 3 m to clear the road mesh
            // itself so the ground never shadows its own sample point.
            Vector3 surfacePos = new Vector3(pos.x, elevation + 0.1f, pos.z);

            // -forward points from the surface back toward the sun.
            return Physics.Raycast(surfacePos + Vector3.up * 3.0f, -sun.transform.forward, 10000f, caster.value | ground.value);
        }

        public int EvaluatePath(List<Vector3> path, float len, LayerMask caster, LayerMask ground, Light sun, float spacing, float elevation, float threshold)
        {
            if (path == null || path.Count < 2 || !sun) return 0;
            float el = sun.transform.eulerAngles.x;
            if (el <= threshold || el >= 180f - threshold) return 0;

            int n = Mathf.Max(1, Mathf.RoundToInt(len / Mathf.Max(0.01f, spacing)));
            int sunlit = 0;
            for (int i = 0; i < n; i++)
            {
                Vector3 p = GetPointOnPath(path, n > 1 ? (float)i / (n - 1) : 0f);
                Vector3 surfacePos = new Vector3(p.x, elevation + 0.1f, p.z);
                if (!Physics.Raycast(surfacePos + Vector3.up * 3.0f, -sun.transform.forward, 10000f, caster.value | ground.value)) sunlit++;
            }
            return sunlit;
        }

        public List<(string startId, string endId)> FilterRedundantEdges(Dictionary<(string, string), float> dists, List<(string id, Vector3 pos)> pts)
        {
            var basic = new List<(string, string)>();
            foreach (var entry in dists)
            {
                string idA = entry.Key.Item1, idB = entry.Key.Item2; float dAB = entry.Value; bool redundant = false;
                foreach (var p in pts)
                {
                    string idK = p.id; if (idK == idA || idK == idB) continue;
                    if (dists.ContainsKey((idA, idK)) && dists.ContainsKey((idK, idB)))
                        if (dAB >= (dists[(idA, idK)] + dists[(idK, idB)] - 0.1f)) { redundant = true; break; }
                }
                if (!redundant) basic.Add((idA, idB));
            }
            return basic;
        }

        public Vector3 GetPointOnPath(List<Vector3> path, float t)
        {
            if (path == null || path.Count == 0) return Vector3.zero;
            if (t <= 0) return path[0]; if (t >= 1) return path[path.Count - 1];
            float total = 0f; for (int i = 0; i < path.Count - 1; i++) total += Vector3.Distance(path[i], path[i + 1]);
            float target = t * total, walked = 0f;
            for (int i = 0; i < path.Count - 1; i++)
            {
                float d = Vector3.Distance(path[i], path[i + 1]);
                if (walked + d >= target) return Vector3.Lerp(path[i], path[i + 1], (target - walked) / d);
                walked += d;
            }
            return path[path.Count - 1];
        }
    }

    public class VisualizationEngine
    {
        private List<GameObject> paths = new();
        private List<GameObject> markers = new();
        private GameObject bboxVisual;

        public void ClearPaths() 
        { 
            foreach (var o in paths) if (o) UnityEngine.Object.Destroy(o); 
            paths.Clear(); 
        }

        public void UpdateBBox(Bounds b, Transform t, bool v)
        {
            if (bboxVisual) UnityEngine.Object.Destroy(bboxVisual);
            if (!v) return;

            bboxVisual = new GameObject("BBoxVisual");
            bboxVisual.transform.SetParent(t, false);
            LineRenderer lr = bboxVisual.AddComponent<LineRenderer>();
            lr.useWorldSpace = true;
            lr.loop = true;
            lr.positionCount = 4;
            Vector3 center = b.center;
            Vector3 extents = b.extents;
            float y = center.y;
            lr.SetPositions(new Vector3[] {
                new Vector3(center.x - extents.x, y, center.z - extents.z),
                new Vector3(center.x + extents.x, y, center.z - extents.z),
                new Vector3(center.x + extents.x, y, center.z + extents.z),
                new Vector3(center.x - extents.x, y, center.z + extents.z)
            });
            lr.startColor = lr.endColor = new Color(0f, 1f, 0f, 0.6f);
            lr.widthMultiplier = 2f;
            lr.material = new Material(Shader.Find("Unlit/Color")) { color = new Color(0f, 1f, 0f, 0.6f) };
        }

        public void DrawPath(List<Vector3> p, Color c, Transform t)
        {
            GameObject obj = new GameObject("Path_" + paths.Count); 
            obj.transform.SetParent(t, false);
            LineRenderer lr = obj.AddComponent<LineRenderer>(); 
            lr.positionCount = p.Count; 
            lr.SetPositions(p.ToArray());
            lr.startColor = lr.endColor = c; 
            lr.widthMultiplier = 8.0f; 
            lr.material = new Material(Shader.Find("Unlit/Color")) { color = c };
            paths.Add(obj);
        }

        public void UpdateMarkers(List<(string id, Vector3 pos)> pts, Transform t, bool v)
        {
            foreach (var o in markers) if (o) UnityEngine.Object.Destroy(o); 
            markers.Clear(); 
            if (!v) return;

            foreach (var p in pts)
            {
                GameObject m = GameObject.CreatePrimitive(PrimitiveType.Sphere);
                m.name = "Marker_" + p.id;
                m.transform.SetParent(t, false);
                m.transform.position = p.pos + Vector3.up * 0.5f;
                m.transform.localScale = (p.id == "START" || p.id == "END") ? Vector3.one * 30f : Vector3.one * 4.5f;

                // Track immediately: the START/END branch below `continue`s, and marker cleanup
                // is driven entirely by this list — untracked objects leaked into the scene on
                // every reload.
                markers.Add(m);

                if (m.TryGetComponent<Renderer>(out var r))
                    r.material = new Material(Shader.Find("Unlit/Color")) { color = (p.id == "START" || p.id == "END" ? Color.red : Color.cyan) };

                // --- Add ID Label ---
                // Skip START/END labels here as they are handled by PathVisualizationHelper to avoid duplicates
                if (p.id == "START" || p.id == "END") continue;

                GameObject lbl = new GameObject("Label");
                lbl.transform.SetParent(m.transform, false);
                lbl.transform.localPosition = Vector3.down * 1.5f; // Moved below the point
                lbl.transform.localRotation = Quaternion.Euler(90, 0, 0); // Face UP
                TextMesh tm = lbl.AddComponent<TextMesh>();
                
                // Only take first 6 chars of UUID for visual cleanliness
                string shortId = p.id;
                if (shortId.Length > 8) shortId = shortId.Substring(0, 6) + "..";
                
                tm.text = shortId;
                tm.fontSize = 60;
                tm.anchor = TextAnchor.MiddleCenter;
                tm.alignment = TextAlignment.Center;
                tm.color = (p.id == "START") ? Color.yellow : ((p.id == "END") ? Color.green : Color.white);
                tm.characterSize = 0.2f;
            }
        }

        public void SetPathsVisible(bool v) 
        { 
            foreach (var o in paths) if (o) o.SetActive(v); 
        }
    }



    // ElevationEngine removed as per refactor to global elevation.
}
