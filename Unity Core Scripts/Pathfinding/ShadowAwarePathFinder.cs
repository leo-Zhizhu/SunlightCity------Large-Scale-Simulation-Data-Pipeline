using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEngine;
using UnityEngine.AI;
using ShadowAware.Engines;
using Npgsql;

/// <summary>
/// Orchestrator for the whole in-editor pipeline. Despite the name this component does not
/// itself search for paths — the Pareto search runs in the Python/SQL backend. Its jobs are:
///
///   1. Load the road graph (waypoints + edges) for a bounding box around start/end from PostGIS.
///   2. Generate 2 m-spaced sample points along every edge and write them to `meo_sample_points`.
///   3. Sweep simulated time and raycast each sample point at the sun, streaming boolean
///      sunlit/shadowed results into `meo_exposure_samples` via COPY — THE PRODUCT, one row
///      per (sample point, timestamp) — and deriving the `meo_exposure_edges` convenience
///      index server-side every 3 simulated hours.
///   4. Render the backend's Pareto-optimal routes (read back from `backendOutputCsvPath`).
///
/// Heavy lifting is split across the engines in ShadowAwarePathFinder_Engines.cs.
///
/// This is the v1, single-node exporter: 1.577 billion raycasts in 6 hours on one thread,
/// with peak RAM flat at ~250 MB because the buffer is flushed every 3 simulated hours.
/// It remains supported and is the right tool for one neighbourhood. The distributed
/// pipeline in distributed/ writes the same rows into the same schema on 50 workers; see
/// docs/V1_PIPELINE.md for the full description and what v2 changes.
///
/// Two things here that v2 inherits rather than replaces:
///   * The bounding box. v1 already proved a spatial subset can be simulated
///     independently; v2 turns that into its unit of both parallelism and storage.
///   * The horizon guard in ShadowEngine.IsInShadow. Besides being a correctness fix, it
///     bounds the longest possible shadow at H/tan(threshold) — which is what makes v2's
///     per-section tasks exactly, rather than approximately, independent.
/// </summary>
public class ShadowAwarePathFinder : MonoBehaviour
{
    [Header("Input Points")]
    public Transform startPoint;
    public Transform endPoint;
    public Transform roadModelRoot;

    [Header("Data Source (New)")]
    public string backendOutputCsvPath = "backend_output.csv";

    [Header("Environment Settings")]
    public LayerMask roadLayer;
    public LayerMask shadowCasterMask;
    public LayerMask groundBlockerMask;

    [Header("Optimization Constraints")]
    public float samplePointSpacing = 5f;
    [Tooltip("Sun angle threshold (degrees) below which points are considered in shadow. Reduce for more gradual sunsets.")]
    public float sunAngleThreshold = 5.0f;
    public float edgeBBoxPadding = 100f;
    
    [Header("Global Constraints")]
    [Tooltip("Global Unity Y coordinate used for all nodes, edges, and sample points. Matches the DB normalized value.")]
    public float globalElevation = -112.0f;


    [Tooltip("Specific dates to export (format: M.D, e.g. 5.15, 8.1, 8.15). If empty, defaults to 1st and 15th of each month.")]
    public string exportTargetDates = "1.1, 1.15, 2.1, 2.15, 3.1, 3.15, 4.1, 4.15, 5.1, 5.15, 6.1, 6.15, 7.1, 7.15, 8.1, 8.15, 9.1, 9.15, 10.1, 10.15, 11.1, 11.15, 12.1, 12.15";

    /// <summary>
    /// Parses <see cref="exportTargetDates"/> ("M.D" pairs separated by commas/spaces) into
    /// (month, day) tuples. Malformed entries are silently skipped so a typo in the runtime
    /// text field can never abort a multi-hour export.
    /// </summary>
    public List<(int month, int day)> GetParsedTargetDates()
    {
        var result = new List<(int month, int day)>();
        if (string.IsNullOrWhiteSpace(exportTargetDates)) return result;

        string[] pairs = exportTargetDates.Split(new char[] { ',', ' ' }, StringSplitOptions.RemoveEmptyEntries);
        foreach (var p in pairs)
        {
            string[] parts = p.Split('.');
            if (parts.Length == 2 && int.TryParse(parts[0], out int m) && int.TryParse(parts[1], out int d))
            {
                result.Add((m, d));
            }
        }
        return result;
    }

    [Header("Visualization")]
    public int maxPathsToVisualize = 5;
    [SerializeField] private bool showWaypoints = true;
    [SerializeField] private bool showPaths = true;
    [SerializeField] private bool showBBox = false;
    private bool showUITable = false;
    
    // Engines

    private WaypointEngine waypointEngine = new();
    private ShadowEngine shadowEngine = new();
    private VisualizationEngine visualizationEngine = new();

    private TreeEngine treeEngine = new();
    private TreeVisualization treeVisualization;
    private SampleVisualization sampleVisualization;
    private Light sunLight;
    private SunController sunController;
    private PostGISClient postGISClient;

    private List<(List<Vector3> path, int sunExposureCount, float length)> displayedPaths = new();
    private List<Color> pathColors = new();

    // id -> world position, built lazily from waypointEngine.StoredPoints when resolving
    // the backend CSV's waypoint-id paths. Invalidated whenever the graph is reloaded.
    private Dictionary<string, Vector3> waypointLookup;
    private static Texture2D glassTex;
    private static Texture2D whiteTex;

    void Awake() 
    { 
        sunLight = FindFirstObjectByType<Light>(FindObjectsInactive.Exclude); 
        if (sunLight?.type != LightType.Directional) sunLight = FindObjectsByType<Light>(FindObjectsSortMode.None).FirstOrDefault(l => l.type == LightType.Directional);
        sunController = FindFirstObjectByType<SunController>();
        postGISClient = FindFirstObjectByType<PostGISClient>();
        treeVisualization = gameObject.GetComponent<TreeVisualization>();
        if (treeVisualization == null) treeVisualization = gameObject.AddComponent<TreeVisualization>();
        sampleVisualization = gameObject.GetComponent<SampleVisualization>();
        if (sampleVisualization == null) sampleVisualization = gameObject.AddComponent<SampleVisualization>();

        if (postGISClient == null)
            Debug.LogWarning("<color=yellow>[ShadowAwarePathFinder]</color> PostGISClient not found in scene. Simulation will fall back to local JSON.");
        
        if (roadModelRoot == null) roadModelRoot = transform; // Default to self/root if not assigned
    }
    
    void Start() 
    { 
        InitializeSimulation(); 
    }

    private List<(int month, int day)> availableDbDates = new List<(int month, int day)>();

    /// <summary>
    /// Discovers which calendar dates already have exposure data inside <paramref name="area"/>.
    /// The visualisation snaps the current simulation date to the nearest of these, so the
    /// scene can show real data even when the sun is parked on a date that was never exported.
    /// </summary>
    private void FetchAvailableDatesFromDB(Bounds area)
    {
        availableDbDates.Clear();
        if (postGISClient == null) return;

        string connStr = $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};";
        try {
            using (var conn = new NpgsqlConnection(connStr))
            {
                conn.Open();
                Vector3 min = area.min;
                Vector3 max = area.max;

                using (var cmd = new NpgsqlCommand(@"
                    SELECT DISTINCT CAST(datetime AS DATE) 
                    FROM meo_exposure_samples es
                    JOIN meo_sample_points sp ON es.sample_point_id = sp.id
                    WHERE sp.geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0)
                ", conn))
                {
                    cmd.Parameters.AddWithValue("minX", (double)min.x);
                    cmd.Parameters.AddWithValue("minZ", (double)min.z);
                    cmd.Parameters.AddWithValue("maxX", (double)max.x);
                    cmd.Parameters.AddWithValue("maxZ", (double)max.z);
                    using (var reader = cmd.ExecuteReader())
                    {
                        while (reader.Read())
                        {
                            DateTime dt = reader.GetDateTime(0);
                            availableDbDates.Add((dt.Month, dt.Day));
                        }
                    }
                }
            }
        } catch(Exception e) {
            Debug.LogError($"[DB] Error fetching available dates: {e.Message}");
        }
    }

    private void InitializeSimulation()
    {
        Debug.Log("<color=cyan>[ShadowAwarePathFinder]</color> Starting Simulation Initialization...");

        // Every step below dereferences these, so bail out early with a readable message
        // rather than throwing a NullReferenceException from inside Start().
        if (startPoint == null || endPoint == null)
        {
            Debug.LogError("[ShadowAwarePathFinder] startPoint and endPoint must both be assigned in the Inspector.");
            return;
        }

        // 1. Calculate Bounds
        Vector3 minBounds = Vector3.Min(startPoint.position, endPoint.position) - Vector3.one * edgeBBoxPadding;
        Vector3 maxBounds = Vector3.Max(startPoint.position, endPoint.position) + Vector3.one * edgeBBoxPadding;
        Bounds area = new Bounds((minBounds + maxBounds) * 0.5f, maxBounds - minBounds);

        // Fetch valid dates from DB for this specific area
        FetchAvailableDatesFromDB(area);

        // Report initial sunlight data matching
        if (sunController != null)
        {
            DateTime simTime = sunController.GetCurrentDateTime();
            var targetDates = availableDbDates;

            int targetMonth = simTime.Month;
            int targetDay = 1;

            if (targetDates.Count > 0)
            {
                // Find globally closest date in the DB
                var closest = targetDates.OrderBy(dt => Math.Abs((new DateTime(2026, dt.month, dt.day) - new DateTime(2026, simTime.Month, simTime.Day)).TotalDays)).First();
                targetMonth = closest.month;
                targetDay = closest.day;
            }
            else
            {
                targetDay = (simTime.Day < 8) ? 1 : 15;
            }
            Debug.Log($"<color=yellow>[SunlightVisualization]</color> Initial simulation date: {simTime:MMM dd}. Closest DB match: {targetMonth}/{targetDay:D2}. Displaying exposure from {new DateTime(2026, targetMonth, targetDay):MMM dd}.");
        }

        // 2. Load & Snap Waypoints
        string connStr = postGISClient != null ? $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};" : "";
        
        if (!string.IsNullOrEmpty(connStr))
            waypointEngine.LoadWaypointsFromPostGIS(connStr, startPoint, endPoint, edgeBBoxPadding, globalElevation);
        else
            waypointEngine.LoadWaypointsFromJson(startPoint, endPoint, edgeBBoxPadding, globalElevation);

        // 3. Load & Snap Existing Sample Points
        if (!string.IsNullOrEmpty(connStr))
            waypointEngine.LoadSamplesFromPostGIS(connStr, area, globalElevation);

        // 5. Visualize
        visualizationEngine.UpdateMarkers(waypointEngine.StoredPoints, transform, showWaypoints);
        visualizationEngine.UpdateBBox(waypointEngine.CurrentBBox, transform, showBBox);
        
        Debug.Log("<color=green>[ShadowAwarePathFinder]</color> Initialization Complete.");
    }
    
    private int lastTimeStepMinute = -1;

    void Update()
    {
        // Only re-query the DB while the sunlight overlay is actually on screen.
        if (sunController != null && sampleVisualization != null
            && sampleVisualization.CurrentMode == SampleVisualization.VisualizationMode.Sunlight)
        {
            DateTime current = sunController.GetCurrentDateTime();
            // Data is exported in 3-minute steps. Check if we've moved to a new 3rd minute.
            int currentStep = (current.Hour * 60 + current.Minute) / 3;
            
            if (currentStep != lastTimeStepMinute)
            {
                lastTimeStepMinute = currentStep;
                sampleVisualization.RefreshSunlightColors(postGISClient, current, availableDbDates);
            }
        }
    }



    [ContextMenu("1. Reload Data & Snap")]
    public void ManualReload() => InitializeSimulation();

    private void LoadTreesFromDB()
    {
        if (postGISClient != null && startPoint != null && endPoint != null)
        {
            Vector3 min = Vector3.Min(startPoint.position, endPoint.position) - Vector3.one * edgeBBoxPadding;
            Vector3 max = Vector3.Max(startPoint.position, endPoint.position) + Vector3.one * edgeBBoxPadding;
            // Adaptive Y range for trees
            min.y = Mathf.Min(min.y, Mathf.Min(startPoint.position.y, endPoint.position.y) - 50f);
            max.y = Mathf.Max(max.y, Mathf.Max(startPoint.position.y, endPoint.position.y) + 50f);
            Bounds treeBBox = new Bounds((min + max) * 0.5f, max - min);

            string connStr = $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};";
            treeEngine.LoadTreesFromPostGIS(connStr, treeBBox, globalElevation);
            treeVisualization.UpdateTreeMarkers(treeEngine.LoadedTrees, null, showTrees); 
        }
    }

    [Header("Tree Visualization")]
    [SerializeField] private bool showTrees = false;
    public void ToggleTrees()
    {
        showTrees = !showTrees;

        if (showTrees)
        {
            LoadTreesFromDB();
        }
        else if (treeVisualization != null)
        {
            treeVisualization.SetVisible(false);
        }
        
        Debug.Log($"<color=green>[ShadowAwarePathFinder]</color> Tree visibility: {showTrees}");
    }

    public bool AreTreesVisible() => showTrees;

    public void RunOptimization()
    {
        if (showPaths)
        {
            // If paths are currently shown, hide them and the data table.
            TogglePaths();
            showUITable = false;
        }
        else
        {
            // If paths are currently hidden
            if (displayedPaths.Count == 0)
            {
                // First time load: Perform simulation and parse CSV
                InitializeSimulation();
                LoadPathsFromCSV();
                showPaths = true; // LoadPathsFromCSV clears and draws paths, so we ensure visibility is true
            }
            else
            {
                // Reuse cached paths
                TogglePaths();
            }
            showUITable = true;
        }
    }

    private void LoadPathsFromCSV()
    {
        Debug.Log($"<color=cyan>[ShadowAwarePathFinder]</color> Starting CSV Load: {backendOutputCsvPath}");
        
        string fullPath = Path.Combine(Application.dataPath, "..", backendOutputCsvPath);
        if (!File.Exists(fullPath))
        {
            Debug.LogError($"[ShadowAwarePathFinder] CSV file not found at: {fullPath}");
            return;
        }

        string[] lines = File.ReadAllLines(fullPath);
        if (lines.Length < 2)
        {
            Debug.LogError("[ShadowAwarePathFinder] CSV file is empty or missing data.");
            return;
        }

        // Parse header to find column indices. Trim, because writers often emit "a, b, c".
        string[] headers = lines[0].Split(',');
        for (int i = 0; i < headers.Length; i++) headers[i] = headers[i].Trim();

        int pathIdx = Array.IndexOf(headers, "path");
        int dateIdx = Array.IndexOf(headers, "date");
        int timeIdx = Array.IndexOf(headers, "start_time_hhmm");
        int exposureIdx = Array.IndexOf(headers, "exposure");
        int lengthIdx = Array.IndexOf(headers, "length_m");

        if (pathIdx == -1 || dateIdx == -1 || timeIdx == -1)
        {
            Debug.LogError("[ShadowAwarePathFinder] CSV missing required columns (path, date, or start_time_hhmm).");
            return;
        }

        // exposure/length are optional; they are read via these indices below, so a missing
        // column must degrade to 0 rather than indexing parts[-1].
        if (exposureIdx == -1 || lengthIdx == -1)
        {
            Debug.LogWarning("[ShadowAwarePathFinder] CSV has no 'exposure' and/or 'length_m' column. " +
                             "Those metrics will display as 0.");
        }

        displayedPaths.Clear();
        visualizationEngine.ClearPaths();
        pathColors.Clear();
        waypointLookup = null; // rebuilt lazily below; StoredPoints may have changed

        bool sunSet = false;

        for (int i = 1; i < lines.Length; i++)
        {
            string line = lines[i];
            if (string.IsNullOrWhiteSpace(line)) continue;

            // Handle quoted path column
            // We expect the path to be the last column or enclosed in quotes.
            // A simple regex-free way to handle the CSV if it's strictly formatted:
            string[] parts = ParseCsvLine(line);

            // Guard every index we are about to read, not just `path` — a short/ragged row
            // would otherwise throw IndexOutOfRangeException mid-load.
            if (parts.Length <= Mathf.Max(pathIdx, Mathf.Max(dateIdx, timeIdx))) continue;

            // Sync Sun on first valid row
            if (!sunSet)
            {
                SyncSun(parts[dateIdx], parts[timeIdx]);
                sunSet = true;
            }

            string pathStr = parts[pathIdx].Trim('"');
            string[] waypointIds = pathStr.Split(new char[] { ',' }, StringSplitOptions.RemoveEmptyEntries);

            List<Vector3> pathPoints = new List<Vector3>();

            // Index the waypoints once instead of a linear scan per id — a Manhattan-scale
            // bounding box holds thousands of waypoints and a path holds dozens of ids.
            if (waypointLookup == null)
            {
                waypointLookup = new Dictionary<string, Vector3>();
                foreach (var p in waypointEngine.StoredPoints) waypointLookup[p.id] = p.pos;
            }

            foreach (string id in waypointIds)
            {
                if (waypointLookup.TryGetValue(id.Trim(), out Vector3 pos))
                {
                    pathPoints.Add(pos);
                }
            }

            if (pathPoints.Count > 0)
            {
                float exposure = 0;
                float length = 0;
                if (exposureIdx >= 0 && exposureIdx < parts.Length) float.TryParse(parts[exposureIdx], out exposure);
                if (lengthIdx   >= 0 && lengthIdx   < parts.Length) float.TryParse(parts[lengthIdx],   out length);

                displayedPaths.Add((pathPoints, (int)exposure, length));
                
                Color c = i == 1 ? Color.red : Color.HSVToRGB((float)(i - 1) / (lines.Length - 1), 0.8f, 0.9f);
                pathColors.Add(c);
                visualizationEngine.DrawPath(pathPoints, c, transform);
                
                Debug.Log($"<color=green>[ShadowAwarePathFinder]</color> Path {i} loaded: {pathPoints.Count} points, Exposure: {exposure}, Length: {length:F2}m");
            }
            else
            {
                Debug.LogWarning($"[ShadowAwarePathFinder] Path {i} has 0 valid waypoints found in simulation.");
            }
        }

        Debug.Log($"<color=cyan>[ShadowAwarePathFinder]</color> CSV Load Complete. Displaying {displayedPaths.Count} paths.");
    }

    private string[] ParseCsvLine(string line)
    {
        List<string> parts = new List<string>();
        bool inQuotes = false;
        string current = "";
        for (int i = 0; i < line.Length; i++)
        {
            char c = line[i];
            if (c == '\"') inQuotes = !inQuotes;
            else if (c == ',' && !inQuotes)
            {
                parts.Add(current);
                current = "";
            }
            else current += c;
        }
        parts.Add(current);
        return parts.ToArray();
    }

    /// <summary>
    /// Parks the sun on the date/time the backend used when it scored these routes, so the
    /// shadows on screen match the exposure numbers in the table.
    /// </summary>
    private void SyncSun(string dateStr, string timeStr)
    {
        if (sunController == null) return;

        // Parse date yyyy-MM-dd
        if (DateTime.TryParse(dateStr, out DateTime dt))
        {
            sunController.year = dt.Year;
            sunController.month = dt.Month;
            sunController.day = dt.Day;
        }

        // Parse time HH:mm
        string[] tParts = timeStr.Split(':');
        if (tParts.Length == 2 && float.TryParse(tParts[0], out float h) && float.TryParse(tParts[1], out float m))
        {
            sunController.SetHour(h + m / 60f);
        }
        
        Debug.Log($"<color=yellow>[ShadowAwarePathFinder]</color> Sun synchronized to: {dateStr} {timeStr}");
    }

    public float CalculatePathLength(List<Vector3> path) { float l = 0; for (int i = 0; i < path.Count - 1; i++) l += Vector3.Distance(path[i], path[i + 1]); return l; }



    // --- DB EXPORT (SAMPLES) ---
    private bool isExportingSamples = false;
    [ContextMenu("2. Export Sample Points to DB")]
    public void StartSampleExport()
    {
        if (isExportingSamples) return;
        if (postGISClient == null || waypointEngine.StoredEdges.Count == 0)
        {
            Debug.LogWarning("[ShadowAwarePathFinder] Cannot export samples: No PostGIS connection or empty graph.");
            return;
        }
        StartCoroutine(ExportSamplesRoutine());
    }

    private IEnumerator ExportSamplesRoutine()
    {
        isExportingSamples = true;
        Debug.Log("<color=cyan>[SamplePointExporter]</color> Starting Sample Points Export...");

        string connStr = $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};";

        var points = waypointEngine.StoredPoints;
        var edges = waypointEngine.StoredEdges;

        // id -> position, so the inner loop doesn't do a linear scan of StoredPoints per edge.
        var posById = new Dictionary<string, Vector3>();
        foreach (var p in points) posById[p.id] = p.pos;

        // try/finally guarantees isExportingSamples is released. Without it, a DB error would
        // leave the flag latched true and silently ignore every later export request.
        try
        {
        using (var conn = new NpgsqlConnection(connStr))
        {
            conn.Open();
            Debug.Log("<color=green>[SamplePointExporter]</color> DB Connected.");

            Debug.Log($"<color=cyan>[SamplePointExporter]</color> Beginning export for {edges.Count} edges in memory. roadLayer mask: {roadLayer.value}");

            int totalSamples = 0;
            int skippedEdges = 0;
            int missingEdgeIds = 0;

                foreach (var edge in edges)
                {
                    // START/END are synthetic connector nodes injected by WaypointEngine for the
                    // current query; they have no row in meo_edges and must not be sampled.
                    if (edge.from == "START" || edge.from == "END" || edge.to == "START" || edge.to == "END") continue;

                    // Real graph ids are UUIDs. Anything else (e.g. a JSON-fallback integer id)
                    // cannot be matched against the DB, so skip instead of throwing.
                    if (!Guid.TryParse(edge.from, out Guid fromGuid) || !Guid.TryParse(edge.to, out Guid toGuid))
                    {
                        skippedEdges++;
                        continue;
                    }

                    // Edges are undirected in the graph but stored with a fixed orientation,
                    // so match both directions.
                    string edgeIdQuery = @"
                        SELECT id FROM meo_edges
                        WHERE (start_wp_id = @fromId AND end_wp_id = @toId)
                           OR (start_wp_id = @toId AND end_wp_id = @fromId) LIMIT 1;";

                    Guid? edgeId = null;
                    using (var cmd = new NpgsqlCommand(edgeIdQuery, conn))
                    {
                        cmd.Parameters.AddWithValue("fromId", fromGuid);
                        cmd.Parameters.AddWithValue("toId", toGuid);
                        var result = cmd.ExecuteScalar();
                        if (result != null && result != DBNull.Value) edgeId = (Guid)result;
                    }

                    if (edgeId == null)
                    {
                        missingEdgeIds++;
                        continue;
                    }

                    // Check if this edge already has sample points
                    bool hasSamples = false;
                    using (var checkCmd = new NpgsqlCommand("SELECT 1 FROM meo_sample_points WHERE edge_id = @eid LIMIT 1;", conn))
                    {
                        checkCmd.Parameters.AddWithValue("eid", edgeId.Value);
                        if (checkCmd.ExecuteScalar() != null) hasSamples = true;
                    }

                    if (hasSamples) continue; // Skip edge if it already has data

                    // Both endpoints must be in the loaded window; .First() would throw if an
                    // edge straddles the bounding box and one endpoint was filtered out.
                    if (!posById.TryGetValue(edge.from, out Vector3 p1) || !posById.TryGetValue(edge.to, out Vector3 p2))
                    {
                        skippedEdges++;
                        continue;
                    }

                    float len = Vector3.Distance(p1, p2);
                    // n points spread inclusively from p1 to p2, so the realised gap is
                    // len/(n-1) — very slightly wider than samplePointSpacing. Kept as-is
                    // because the published 365,133-point dataset was generated this way.
                    int n = Mathf.Max(1, Mathf.RoundToInt(len / Mathf.Max(0.01f, samplePointSpacing)));

                    // List of (ID, SequenceIndex, DistanceFromStart, Position)
                    var samplesToInsert = new List<(Guid id, int idx, float dist, Vector3 pos)>();

                    for (int i = 0; i < n; i++)
                    {
                        float t = n > 1 ? (float)i / (n - 1) : 0f;
                        Vector3 samplePos = Vector3.Lerp(p1, p2, t);
                        float dist = t * len;
                        
                        samplePos.y = globalElevation;
                        samplesToInsert.Add((Guid.NewGuid(), i, dist, samplePos));
                    }
                    
                    if (samplesToInsert.Count > 0)
                    {
                        using (var cmd = new NpgsqlCommand())
                        {
                            cmd.Connection = conn;
                            // Include distance_from_start in the INSERT
                            string queryValues = string.Join(",", samplesToInsert.Select((s, i) => $"(@id{i}, @eid{i}, @idx{i}, @dist{i}, ST_SetSRID(ST_MakePoint(@x{i}, @y{i}, @z{i}), 0))"));
                            cmd.CommandText = $"INSERT INTO meo_sample_points (id, edge_id, sequence_index, distance_from_start, geom) VALUES {queryValues};";
                            
                            for (int i = 0; i < samplesToInsert.Count; i++)
                            {
                                var s = samplesToInsert[i];
                                cmd.Parameters.AddWithValue($"id{i}", s.id);
                                cmd.Parameters.AddWithValue($"eid{i}", edgeId.Value);
                                cmd.Parameters.AddWithValue($"idx{i}", s.idx);
                                cmd.Parameters.AddWithValue($"dist{i}", (double)s.dist);
                                // Axis convention throughout the DB: PostGIS (X, Y, Z) maps to
                                // Unity (x, z, y) — PostGIS Y is the horizontal Unity Z, and
                                // PostGIS Z carries the Unity vertical.
                                cmd.Parameters.AddWithValue($"x{i}", (double)s.pos.x);
                                cmd.Parameters.AddWithValue($"y{i}", (double)s.pos.z);
                                cmd.Parameters.AddWithValue($"z{i}", (double)s.pos.y);
                            }
                            cmd.ExecuteNonQuery();

                            // Update sample_count in meo_edges
                            using (var updateCmd = new NpgsqlCommand("UPDATE meo_edges SET sample_count = @count WHERE id = @eid", conn))
                            {
                                updateCmd.Parameters.AddWithValue("count", samplesToInsert.Count);
                                updateCmd.Parameters.AddWithValue("eid", edgeId.Value);
                                updateCmd.ExecuteNonQuery();
                            }

                            totalSamples += samplesToInsert.Count;
                        }
                    }
                    // One edge per frame keeps the Editor responsive during a long export.
                    yield return null;
                }

                Debug.Log($"<color=green>[SamplePointExporter]</color> SUCCESS! {totalSamples} sample points successfully inserted.");
                Debug.Log($"<color=cyan>[SamplePointExporter]</color> Final diagnostics: Inserted: {totalSamples}, " +
                          $"Edges with no DB row: {missingEdgeIds}, Edges skipped (non-UUID id or endpoint outside window): {skippedEdges}");

                if (totalSamples == 0)
                {
                    Debug.LogWarning("<color=orange>[SamplePointExporter]</color> No points were inserted. Either every edge already " +
                                     "had samples, or the graph loaded for this bounding box has no matching rows in meo_edges.");
                }
            }
        }
        finally
        {
            isExportingSamples = false;
        }
    }

    private bool isExportingExposure = false;
    [ContextMenu("3. Export Exposure to DB")]
    public void StartExposureExport()
    {
        if (isExportingExposure) return;
        StartCoroutine(ExportExposureRoutine());
    }

    /// <summary>
    /// The core producer loop. For each target date it steps simulated time in 3-minute
    /// increments from 03:00 to 21:00 (360 steps/day), raycasts every sample point at the sun,
    /// and buffers boolean results in RAM. Every 3 simulated hours the buffer is streamed to
    /// PostgreSQL with COPY and immediately aggregated to edge level server-side, then cleared.
    /// That bounded flush is what keeps peak memory flat regardless of how long the run is.
    /// </summary>
    private IEnumerator ExportExposureRoutine()
    {
        isExportingExposure = true;

        // try/finally so a dropped connection can't leave the export permanently latched.
        try
        {
        if (postGISClient == null) { Debug.LogError("[ExposureExporter] No PostGIS Client in scene."); yield break; }
        if (sunController == null) { Debug.LogError("[ExposureExporter] No SunController in scene."); yield break; }
        if (sunLight == null)      { Debug.LogError("[ExposureExporter] No directional Light found; raycasts would have no sun direction."); yield break; }

        bool wasPlay = sunController.isPlay;
        sunController.isPlay = false; // time is driven manually below

        // Keepalive guards against the server closing an idle connection during long
        // server-side aggregation steps.
        string connStr = $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};Keepalive=30;";
        // Guid (not string) so the inner loop never re-parses: at full scale this loop body runs
        // ~1.5 billion times, and Guid.Parse per iteration was pure overhead.
        List<Tuple<Guid, Vector3>> samplePoints = new List<Tuple<Guid, Vector3>>();

        using (var conn = new NpgsqlConnection(connStr))
        {
            conn.Open();

            // No initial cleanup: the export is resumable and appends only missing rows,
            // so an interrupted multi-hour run can be restarted without losing work.

            // Load every sample point inside the current bounding box once, up front.
            Vector3 min = waypointEngine.CurrentBBox.min;
            Vector3 max = waypointEngine.CurrentBBox.max;

            using (var cmd = new NpgsqlCommand("SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_sample_points WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0);", conn))
            {
                cmd.Parameters.AddWithValue("minX", (double)min.x);
                cmd.Parameters.AddWithValue("minZ", (double)min.z);
                cmd.Parameters.AddWithValue("maxX", (double)max.x);
                cmd.Parameters.AddWithValue("maxZ", (double)max.z);

                using (var reader = cmd.ExecuteReader())
                {
                    while (reader.Read())
                    {
                        // PostGIS (X, Y, Z) -> Unity (x, z, y): note columns 2 and 3 swap.
                        Guid id = reader.GetGuid(0);
                        float x = (float)reader.GetDouble(1);
                        float z = (float)reader.GetDouble(2);
                        float y = (float)reader.GetDouble(3);
                        samplePoints.Add(new Tuple<Guid, Vector3>(id, new Vector3(x, y, z)));
                    }
                }
            }

            Debug.Log($"<color=cyan>[ExposureExporter]</color> Successfully loaded {samplePoints.Count} points. Starting incremental export...");

            // 3. Seasonal Simulation Loops with 3-Hour Incremental Buffering
            int startMinute = 3 * 60;    // 03:00 — earliest sunrise of the year, with margin
            int endMinute = 21 * 60;     // 21:00 — latest sunset of the year, with margin
            int stepMinute = 3;          // temporal resolution: 360 steps per simulated day
            int bufferInterval = 180;    // flush to DB every 3 simulated hours
            var targetList = GetParsedTargetDates();
            if (targetList.Count == 0) // Default fallback if string is empty
            {
                for (int m = 1; m <= 12; m++) { targetList.Add((m, 1)); targetList.Add((m, 15)); }
            }
            int totalInserted = 0;

            foreach (var datePair in targetList)
            {
                int month = datePair.month;
                int day = datePair.day;
                sunController.month = month;
                sunController.day = day;
                DateTime simDate = new DateTime(2026, month, day);

                List<Tuple<Guid, string, bool>> buffer = new List<Tuple<Guid, string, bool>>();
                int blockStartMinute = startMinute;

                for (int minute = startMinute; minute <= endMinute; minute += stepMinute)
                {
                    float hour = minute / 60f;
                    sunController.SetHour(hour);
                    // Wait one physics tick so the light's new transform is visible to Physics.Raycast.
                    yield return new WaitForFixedUpdate();

                    DateTime currentSimTime = simDate.AddMinutes(minute);
                    string timeStr = currentSimTime.ToString("yyyy-MM-dd HH:mm:ss");

                    // Resumability: skip points already recorded at this exact timestamp.
                    // NOTE: this reads every row for the timestamp, not just those inside the
                    // current bounding box. It is cheap thanks to idx_meo_exposure_samples_time,
                    // but on a full-city table it still returns ~365k ids per step — worth
                    // narrowing with a bbox join if you ever export sub-regions in parallel.
                    HashSet<Guid> existingGuids = new HashSet<Guid>();
                    using (var checkCmd = new NpgsqlCommand("SELECT sample_point_id FROM meo_exposure_samples WHERE datetime = @dt;", conn))
                    {
                        checkCmd.Parameters.AddWithValue("dt", currentSimTime);
                        using (var reader = checkCmd.ExecuteReader())
                        {
                            while (reader.Read()) existingGuids.Add(reader.GetGuid(0));
                        }
                    }

                    foreach (var sp in samplePoints)
                    {
                        if (existingGuids.Contains(sp.Item1)) continue; // already processed

                        bool shadow = shadowEngine.IsInShadow(sp.Item2, sunLight, shadowCasterMask, groundBlockerMask, globalElevation, sunAngleThreshold);
                        buffer.Add(new Tuple<Guid, string, bool>(sp.Item1, timeStr, !shadow));
                    }

                    // Determine if we should flush the buffer (every 3 hours or at end of day)
                    bool isEndOfDay = (minute + stepMinute > endMinute);
                    bool isEndOfBlock = (minute - blockStartMinute >= bufferInterval);

                    if (isEndOfBlock || isEndOfDay)
                    {
                        DateTime blockStartTime = simDate.AddMinutes(blockStartMinute);
                        DateTime blockEndTime = currentSimTime;

                        // A. Stream the buffer with COPY FROM STDIN — bypasses per-row INSERT
                        //    parsing and is roughly an order of magnitude faster at this volume.
                        if (buffer.Count > 0)
                        {
                            using (var writer = conn.BeginTextImport("COPY meo_exposure_samples (sample_point_id, datetime, is_sunlit) FROM STDIN CSV"))
                            {
                                foreach (var row in buffer)
                                {
                                    writer.WriteLine($"{row.Item1},{row.Item2},{row.Item3.ToString().ToLower()}");
                                }
                            }
                        }

                        // B. Derive per-edge sums *inside* the database. The NOT EXISTS clause
                        //    makes this idempotent, so re-running an interrupted export never
                        //    double-counts.
                        //
                        //    Note this DERIVES a convenience index; it does not replace the
                        //    samples. sunlit_sum answers "how sunlit is this edge right now" in
                        //    ~2 GB instead of ~110 GB, which is what makes the Pareto search's
                        //    coarse objective an O(1) lookup. It cannot answer the directional
                        //    question — walked from WHICH end, entering WHEN — because summing
                        //    threw away the order. The samples above remain the product.
                        using (var cmd = new NpgsqlCommand(@"
                            INSERT INTO meo_exposure_edges (edge_id, datetime, sunlit_sum)
                            SELECT sp.edge_id, es.datetime, SUM(CAST(es.is_sunlit AS INT))
                            FROM meo_exposure_samples es
                            JOIN meo_sample_points sp ON sp.id = es.sample_point_id
                            WHERE es.datetime BETWEEN @startTime AND @endTime
                                AND NOT EXISTS (
                                    SELECT 1 FROM meo_exposure_edges mee 
                                    WHERE mee.edge_id = sp.edge_id AND mee.datetime = es.datetime
                                )
                            GROUP BY sp.edge_id, es.datetime;
                        ", conn))
                        {
                            cmd.Parameters.AddWithValue("startTime", blockStartTime);
                            cmd.Parameters.AddWithValue("endTime", blockEndTime);
                            cmd.CommandTimeout = 0; // Infinite timeout for safety
                            cmd.ExecuteNonQuery();
                        }

                        totalInserted += buffer.Count;
                        Debug.Log($"<color=white>[ExposureExporter]</color> <b>{simDate:MMM dd} {blockStartTime:HH:mm}-{blockEndTime:HH:mm}</b>: Uploaded {buffer.Count} samples & Aggregated edges.");

                        // Releasing the buffer here is what bounds peak RAM (~250 MB) whether
                        // the run covers one day or a full year.
                        buffer.Clear();
                        blockStartMinute = minute + stepMinute;
                    }
                    // Yield each time step so the Editor stays interactive and the DB can work
                    // on its aggregation concurrently with the next batch of raycasts.
                    yield return null;
                }
            }

            Debug.Log($"<color=green>[ExposureExporter]</color> <b>ALL COMPLETE!</b> {targetList.Count} specific dates fully processed and aggregated. Total records: {totalInserted}");
        }

        sunController.isPlay = wasPlay;
        }
        finally
        {
            isExportingExposure = false;
        }
    }

    [ContextMenu("3. Clear DB Exposure For Target Dates")]
    public void ClearExposureData()
    {
        if (postGISClient == null || waypointEngine.CurrentBBox.size.sqrMagnitude == 0)
        {
            Debug.LogWarning("[ShadowAwarePathFinder] Cannot clear exposure: No PostGIS connection or Bounding Box not initialized. Click '1. Reload Data & Snap' first.");
            return;
        }

        var targetList = GetParsedTargetDates();
        if (targetList.Count == 0)
        {
            Debug.LogWarning("[ShadowAwarePathFinder] No target dates specified in exportTargetDates string.");
            return;
        }

        Debug.Log($"<color=red>[ExposureCleaner]</color> Starting exposure clearance for {targetList.Count} dates within bounding box...");
        string connStr = $"Host={postGISClient.host};Port={postGISClient.port};Database={postGISClient.database};Username={postGISClient.username};Password={postGISClient.password};";

        int samplesDeleted = 0;
        int edgesDeleted = 0;

        try
        {
            using (var conn = new NpgsqlConnection(connStr))
            {
                conn.Open();
                Vector3 min = waypointEngine.CurrentBBox.min;
                Vector3 max = waypointEngine.CurrentBBox.max;

                foreach (var datePair in targetList)
                {
                    DateTime simDate = new DateTime(2026, datePair.month, datePair.day);
                    
                    // 1. Delete from meo_exposure_samples
                    using (var cmd = new NpgsqlCommand(@"
                        DELETE FROM meo_exposure_samples 
                        WHERE datetime::date = @targetDate
                        AND sample_point_id IN (
                            SELECT id FROM meo_sample_points 
                            WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0)
                        );
                    ", conn))
                    {
                        cmd.Parameters.AddWithValue("targetDate", simDate.Date);
                        cmd.Parameters.AddWithValue("minX", (double)min.x);
                        cmd.Parameters.AddWithValue("minZ", (double)min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)max.x);
                        cmd.Parameters.AddWithValue("maxZ", (double)max.z);
                        
                        int deletedSamples = cmd.ExecuteNonQuery();
                        samplesDeleted += deletedSamples;
                        Debug.Log($"<color=orange>[ExposureCleaner]</color> Cleared {deletedSamples} sample records for {simDate:MMM dd}.");
                    }

                    // 2. Delete from meo_exposure_edges
                    using (var cmd = new NpgsqlCommand(@"
                        DELETE FROM meo_exposure_edges
                        WHERE datetime::date = @targetDate
                        AND edge_id IN (
                            SELECT edge_id FROM meo_sample_points 
                            WHERE geom && ST_MakeEnvelope(@minX, @minZ, @maxX, @maxZ, 0)
                        );
                    ", conn))
                    {
                        cmd.Parameters.AddWithValue("targetDate", simDate.Date);
                        cmd.Parameters.AddWithValue("minX", (double)min.x);
                        cmd.Parameters.AddWithValue("minZ", (double)min.z);
                        cmd.Parameters.AddWithValue("maxX", (double)max.x);
                        cmd.Parameters.AddWithValue("maxZ", (double)max.z);
                        
                        int deletedEdges = cmd.ExecuteNonQuery();
                        edgesDeleted += deletedEdges;
                    }
                }
            }
            Debug.Log($"<color=green>[ExposureCleaner]</color> <b>CLEARANCE COMPLETE!</b> Successfully wiped {samplesDeleted} sample records and {edgesDeleted} edge records for the targeted area/dates.");
        }
        catch (Exception e)
        {
            Debug.LogError($"<color=red>[ExposureCleaner]</color> Error clearing data: {e.Message}");
        }
    }

    /// <summary>
    /// Diagnostic for "this point is always in shadow" reports: prints the true ground height
    /// under a hardcoded probe position and names whatever collider is blocking the sun there.
    /// The probe coordinates are a specific spot that misbehaved during development — edit
    /// <c>testPos</c> to investigate somewhere else.
    /// </summary>
    [ContextMenu("4. Debug Permanent Shadow Issue")]
    public void DebugRaycastBlocker()
    {
        if (sunController == null) { Debug.LogError("[Debugger] No SunController in scene."); return; }

        Debug.Log("<color=magenta>[Debugger]</color> Firing debug raycasts in the problematic area...");
        Vector3 testPos = new Vector3(-1690.67f, globalElevation + 0.1f, -2433.74f);
        
        // 1. Check true ground elevation
        RaycastHit groundHit;
        if (Physics.Raycast(new Vector3(testPos.x, 1000f, testPos.z), Vector3.down, out groundHit, 2000f, roadLayer.value | groundBlockerMask.value))
        {
            Debug.Log($"<color=yellow>True Ground Elevation</color> at {testPos.x}, {testPos.z} is <b>{groundHit.point.y}</b> (Hit: {groundHit.collider.name}). Global is set to {globalElevation}");
        }
        else
        {
            Debug.Log("<color=red>Warning:</color> Could not find any ground mesh below Y=1000 at this position!");
        }

        // 2. Check what blocks the sun at the spike time
        sunController.SetHour(19.2f);
        sunController.month = 7;
        sunController.day = 1;
        Vector3 rayOrigin = testPos + Vector3.up * 15.0f;
        RaycastHit sunHit;
        
        // GetSunDirection() is -transform.forward (points UP to the sun)
        Vector3 sunDir = sunController.GetSunDirection(); 
        float sunAngle = sunController.transform.eulerAngles.x;
        Debug.Log($"<color=cyan>Sun Angle at 19:12:</color> {sunAngle} degrees.");
        
        if (Physics.Raycast(rayOrigin, sunDir, out sunHit, 10000f, shadowCasterMask.value | groundBlockerMask.value))
        {
            Debug.Log($"<color=orange>Sun Blocked By:</color> <b>{sunHit.collider.name}</b> at height {sunHit.point.y}. Ray started at {rayOrigin.y}. Distance to hit: {sunHit.distance}. Ray direction: {sunDir}");
        }
        else
        {
            Debug.Log("<color=green>Clear Sky!</color> The raycast hit nothing at noon. The point should be sunlit.");
        }
    }

    // --- UI CONTROLS ---
    public bool AreGeneratedPointsVisible() => showWaypoints;
    public void ToggleGeneratedPoints() { showWaypoints = !showWaypoints; visualizationEngine.UpdateMarkers(waypointEngine.StoredPoints, transform, showWaypoints); }
    public bool ArePathsVisible() => showPaths;
    public void TogglePaths() { showPaths = !showPaths; visualizationEngine.SetPathsVisible(showPaths); }
    public bool IsBBoxVisible() => showBBox;
    public void ToggleBBox() { showBBox = !showBBox; visualizationEngine.UpdateBBox(waypointEngine.CurrentBBox, transform, showBBox); }
    public void SetTableVisible(bool v) => showUITable = v;

    public void ToggleSamplePoints()
    {
        Debug.Log("<color=cyan>[ShadowAwarePathFinder]</color> ToggleSamplePoints requested.");
        if (postGISClient != null && sunController != null)
        {
            sampleVisualization.ToggleSamples(postGISClient, waypointEngine.CurrentBBox, sunController.GetCurrentDateTime(), availableDbDates);
            lastTimeStepMinute = -1; // Force immediate update on toggle
        }
    }
    public bool AreSamplePointsVisible()
    {
        return sampleVisualization != null && sampleVisualization.AreSamplesVisible();
    }
    public string GetSampleVisualizationMode()
    {
        if (sampleVisualization == null) return "OFF";
        return sampleVisualization.CurrentMode.ToString().ToUpper();
    }

    // --- ONGUI ---
    void OnGUI()
    {
        if (!showUITable || displayedPaths.Count == 0) return;
        
        // Compact panel width and header height for a single-line "EXPOSURE"
        float w = 1000f, h = 60f, head = 110f, m = 32f;
        Rect r = new Rect(Screen.width - w - m, Screen.height - (head + h * displayedPaths.Count + 12f) - m, w, head + h * displayedPaths.Count + 12f);
        if (!glassTex) { glassTex = new Texture2D(1, 1); glassTex.SetPixel(0, 0, new Color(0.04f, 0.1f, 0.22f, 0.85f)); glassTex.Apply(); }
        GUI.color = new Color(1, 1, 1, 0.8f); GUI.DrawTexture(r, glassTex); GUI.color = Color.white;
        
        // Font size for the actual contents
        GUIStyle s = new GUIStyle(GUI.skin.label) { fontSize = 32, fontStyle = FontStyle.Bold, normal = { textColor = Color.white }, alignment = TextAnchor.MiddleCenter };
        
        GUIStyle titleStyle = new GUIStyle(GUI.skin.label) { 
            fontSize = 40, // Title size
            fontStyle = FontStyle.Bold, 
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f) }, // Yellow
            alignment = TextAnchor.MiddleLeft 
        };
        GUI.Label(new Rect(r.x + 30f, r.y + 12f, 600f, 50f), "O P T I M A L   P A T H S", titleStyle);
        
        // Font size for the column headings
        GUIStyle headerStyle = new GUIStyle(GUI.skin.label) { 
            fontSize = 38, 
            fontStyle = FontStyle.Bold, 
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 0.8f) }, // Yellow
            alignment = TextAnchor.MiddleCenter 
        };
        
        float headerY = r.y + 66f;
        // Compact column positions with "EXPOSURE" single-line label
        GUI.Label(new Rect(r.x + 40f, headerY, 160f, 40f), "LABEL", headerStyle);
        GUI.Label(new Rect(r.x + 210f, headerY, 260f, 40f), "DISTANCE", headerStyle);
        GUI.Label(new Rect(r.x + 480f, headerY, 200f, 40f), "COLOR", headerStyle);
        GUI.Label(new Rect(r.x + 700f, headerY, 260f, 40f), "EXPOSURE", headerStyle);

        float ry = r.y + head + 4f, maxEx = Mathf.Max(1f, displayedPaths.Max(p => p.sunExposureCount));
        for (int i = 0; i < displayedPaths.Count; i++) {
            var p = displayedPaths[i]; Color c = pathColors[i];
            
            GUI.Label(new Rect(r.x + 40f, ry, 160f, h), ((char)('A' + i)).ToString(), s);
            GUI.Label(new Rect(r.x + 210f, ry, 260f, h), $"{p.length:F1}m", s);
            
            // Compact color bar
            Rect bar = new Rect(r.x + 490f, ry + h * 0.45f, 180f, 10f);
            if (!whiteTex) { whiteTex = new Texture2D(1, 1); whiteTex.SetPixel(0, 0, Color.white); whiteTex.Apply(); }
            GUI.color = new Color(0.12f, 0.23f, 0.38f, 0.4f); GUI.DrawTexture(bar, whiteTex);
            GUI.color = c; GUI.DrawTexture(new Rect(bar.x, bar.y, (p.sunExposureCount / maxEx) * 180f, bar.height), whiteTex);
            
            GUI.color = Color.white; GUI.Label(new Rect(r.x + 700f, ry, 260f, h), p.sunExposureCount.ToString(), s);
            ry += h;
        }
    }
}
