using UnityEngine;
using System.Collections.Generic;
using System.IO;
using System;

/// <summary>
/// Derives a routable road graph from unstructured 3D city meshes.
///
/// The pipeline (see <see cref="Generate"/>) is: rasterise the mesh to a 2D walkability grid ->
/// dilate to close 1-pixel gaps -> BFS distance transform -> keep local maxima as a centreline
/// skeleton -> lift every skeleton pixel to a node -> then a sequence of topology-preserving
/// simplifications that collapse the oversampled pixel-graph down to intersections only.
///
/// Only degree-1 (stubs) and degree-2 (colinear) nodes are ever pruned, which is why real
/// junctions (degree >= 3) survive by construction and end up precisely at road centres.
///
/// Editor-time tool: run it from the component context menu, not at runtime.
/// </summary>
[ExecuteAlways]
public class RoadGraphExtractor : MonoBehaviour
{
    [Header("Input")]
    public GameObject root;

    [Header("Grid")]
    public float cellSize = 0.3f;
    public int gridWidth = 2000;
    public int gridHeight = 3000;

    [Header("Graph")]
    public float mergeRadius = 2.5f;
    public float minEdgeLength = 1f;

    private bool[,] grid;
    private float[,] distance;
    private bool[,] skeleton;

    private Vector2 origin;

    private List<Vector3> vertices = new List<Vector3>();
    private List<Edge> edges = new List<Edge>();

    struct Edge
    {
        public int a, b;
        public Edge(int a, int b) { this.a = a; this.b = b; }
    }

    [ContextMenu("Generate Graph + Export JSON")]
    public void Generate()
    {
        if (root == null)
        {
            Debug.LogError("Assign root!");
            return;
        }

        Debug.Log("Generating...");

        BuildGrid();                // mesh triangles -> boolean walkability grid
        DilateGrid(2);              // close hairline gaps between separate meshes
        ComputeDistanceTransform(); // distance-to-boundary "height map"
        ExtractRidgeSkeleton();     // ridge of that map == road centreline
        ExtractGraph();             // one node per skeleton pixel (heavily oversampled)
        ClusterNodes();             // merge pixel clumps at intersections into centroids
        RemoveShortEdges();
        DeduplicateEdges();
        RemoveCycles(3);            // first pass: kill 3-node raster triangles
        Connect();                  // bridge gaps between collinear dead ends
        RemoveCycles(8);            // second pass: larger micro-loops created by Connect()
        RemoveEndpoint();           // prune spurious degree-1 spurs
        RemoveFloatingNodes();      // drop nodes left with no edges
        RemoveStraightVertices();   // dissolve colinear degree-2 nodes
        ExportJson();

        Debug.Log("Done!");
    }

    // ================= GRID =================
    void BuildGrid()
    {
        grid = new bool[gridWidth, gridHeight];

        var renderers = root.GetComponentsInChildren<MeshRenderer>();
        if (renderers.Length == 0)
        {
            Debug.LogError($"[RoadGraphExtractor] '{root.name}' has no MeshRenderer in its hierarchy — nothing to rasterise.");
            return;
        }

        // Grid origin is the min corner of the combined XZ footprint, so ToGrid() maps
        // world space into [0, gridWidth) x [0, gridHeight).
        Bounds bounds = renderers[0].bounds;
        foreach (var r in renderers) bounds.Encapsulate(r.bounds);

        origin = new Vector2(bounds.min.x, bounds.min.z);

        foreach (var mf in root.GetComponentsInChildren<MeshFilter>())
        {
            var mesh = mf.sharedMesh;
            if (mesh == null || !mesh.isReadable) continue;

            var verts = mesh.vertices;
            var tris = mesh.triangles;

            for (int i = 0; i < tris.Length; i += 3)
            {
                Vector3 v0 = mf.transform.TransformPoint(verts[tris[i]]);
                Vector3 v1 = mf.transform.TransformPoint(verts[tris[i+1]]);
                Vector3 v2 = mf.transform.TransformPoint(verts[tris[i+2]]);
                RasterizeTriangle(v0, v1, v2);
            }
        }
    }

    void RasterizeTriangle(Vector3 v0, Vector3 v1, Vector3 v2)
    {
        Vector2 p0 = ToGrid(v0);
        Vector2 p1 = ToGrid(v1);
        Vector2 p2 = ToGrid(v2);

        int minX = Mathf.Clamp(Mathf.FloorToInt(Mathf.Min(p0.x, Mathf.Min(p1.x, p2.x))),0,gridWidth-1);
        int maxX = Mathf.Clamp(Mathf.CeilToInt(Mathf.Max(p0.x, Mathf.Max(p1.x, p2.x))),0,gridWidth-1);
        int minY = Mathf.Clamp(Mathf.FloorToInt(Mathf.Min(p0.y, Mathf.Min(p1.y, p2.y))),0,gridHeight-1);
        int maxY = Mathf.Clamp(Mathf.CeilToInt(Mathf.Max(p0.y, Mathf.Max(p1.y, p2.y))),0,gridHeight-1);

        for (int x = minX; x <= maxX; x++)
        for (int y = minY; y <= maxY; y++)
        {
            if (PointInTriangle(new Vector2(x,y), p0,p1,p2))
                grid[x,y] = true;
        }
    }

    Vector2 ToGrid(Vector3 w)
    {
        return new Vector2(
            (w.x - origin.x) / cellSize,
            (w.z - origin.y) / cellSize
        );
    }

    // Barycentric inside-test via 2D cross products. Degenerate (zero-area) triangles are
    // rejected rather than dividing by ~0.
    bool PointInTriangle(Vector2 p, Vector2 a, Vector2 b, Vector2 c)
    {
        float area = Cross(b-a,c-a);
        if (Mathf.Abs(area) < 1e-5f) return false;

        float s = Cross(p-a,c-a)/area;
        float t = Cross(b-a,p-a)/area;

        return s>=0 && t>=0 && s+t<=1;
    }

    float Cross(Vector2 a, Vector2 b)
    {
        return a.x*b.y - a.y*b.x;
    }

    // ================= 🔥 DILATION =================
    void DilateGrid(int iter)
    {
        for (int k = 0; k < iter; k++)
        {
            bool[,] temp = (bool[,])grid.Clone();

            for (int x=1;x<gridWidth-1;x++)
            for (int y=1;y<gridHeight-1;y++)
            {
                if (!grid[x,y])
                {
                    foreach (var d in Directions8())
                    {
                        if (grid[x+d.x,y+d.y])
                        {
                            temp[x,y]=true;
                            break;
                        }
                    }
                }
            }

            grid = temp;
        }
    }

    // ================= DISTANCE =================
    // Multi-source BFS from every non-walkable cell inward. Each walkable cell ends up holding
    // its 8-connected distance to the nearest boundary, forming a "height map" whose ridge line
    // runs down the middle of every road corridor.
    void ComputeDistanceTransform()
    {
        distance = new float[gridWidth,gridHeight];
        Queue<Vector2Int> q = new Queue<Vector2Int>();

        for(int x=0;x<gridWidth;x++)
        for(int y=0;y<gridHeight;y++)
        {
            if(!grid[x,y])
            {
                distance[x,y]=0;
                q.Enqueue(new Vector2Int(x,y));
            }
            else distance[x,y]=float.MaxValue;
        }

        while(q.Count>0)
        {
            var p=q.Dequeue();
            foreach(var d in Directions8())
            {
                int nx=p.x+d.x, ny=p.y+d.y;
                if(InBounds(nx,ny))
                {
                    float nd=distance[p.x,p.y]+1;
                    if(nd<distance[nx,ny])
                    {
                        distance[nx,ny]=nd;
                        q.Enqueue(new Vector2Int(nx,ny));
                    }
                }
            }
        }
    }

    // A cell belongs to the centreline iff it is a local maximum of the distance transform
    // over its 8 neighbours — i.e. it sits on the ridge of the height map above.
    void ExtractRidgeSkeleton()
    {
        skeleton = new bool[gridWidth,gridHeight];

        for(int x=1;x<gridWidth-1;x++)
        for(int y=1;y<gridHeight-1;y++)
        {
            if(!grid[x,y]) continue;

            float d=distance[x,y];
            bool max=true;

            foreach(var dir in Directions8())
                if(distance[x+dir.x,y+dir.y]>d) max=false;

            if(max) skeleton[x,y]=true;
        }
    }

    // ================= 🔥 GRAPH =================
    void ExtractGraph()
    {
        vertices.Clear();
        edges.Clear();

        Dictionary<Vector2Int,int> nodeMap = new Dictionary<Vector2Int,int>();

        // Step 1: create a vertex for EVERY skeleton pixel
        for(int x=0;x<gridWidth;x++)
        for(int y=0;y<gridHeight;y++)
        {
            if(!skeleton[x,y]) continue;

            Vector2Int key = new Vector2Int(x,y);
            int id = vertices.Count;

            nodeMap[key] = id;
            vertices.Add(ToWorld(x,y));
        }

        // Step 2: connect neighbors
        HashSet<string> seen = new HashSet<string>();

        foreach(var kv in nodeMap)
        {
            int x = kv.Key.x;
            int y = kv.Key.y;
            int a = kv.Value;

            foreach(var d in Directions8())
            {
                int nx = x + d.x;
                int ny = y + d.y;

                Vector2Int nk = new Vector2Int(nx,ny);

                if(!InBounds(nx,ny)) continue;
                if(!nodeMap.ContainsKey(nk)) continue;

                int b = nodeMap[nk];

                int min = Mathf.Min(a,b);
                int max = Mathf.Max(a,b);

                string key = min + "_" + max;
                if(seen.Contains(key)) continue;

                seen.Add(key);
                edges.Add(new Edge(min,max));
            }
        }
    }

    Vector3 ToWorld(int x,int y)
    {
        return new Vector3(x*cellSize+origin.x,0,y*cellSize+origin.y);
    }

    bool InBounds(int x,int y)
    {
        return x>=0&&y>=0&&x<gridWidth&&y<gridHeight;
    }

    // Moore (8-way) neighbourhood. Allocated once: this is read inside loops that run over
    // every grid cell (up to 6M for a 2000x3000 grid) across four separate passes, so
    // returning a fresh array per call generated tens of millions of throwaway allocations.
    static readonly Vector2Int[] DIRECTIONS_8 = {
        new Vector2Int(1,0),  new Vector2Int(-1,0),
        new Vector2Int(0,1),  new Vector2Int(0,-1),
        new Vector2Int(1,1),  new Vector2Int(1,-1),
        new Vector2Int(-1,1), new Vector2Int(-1,-1)
    };

    Vector2Int[] Directions8() => DIRECTIONS_8;

    // ================= CLEAN =================
    // Collapse every group of nodes within mergeRadius into a single centroid, then rewrite the
    // edge list through the old->new index map so external topology is preserved.
    //
    // Perf note: this is a deliberately simple O(n^2) sweep. It dominates runtime on large
    // grids; a uniform spatial hash keyed on mergeRadius would make it near-linear if the
    // extractor ever needs to run on a bigger city.
    void ClusterNodes()
    {
        List<Vector3> newV=new List<Vector3>();
        int[] map=new int[vertices.Count];
        bool[] used=new bool[vertices.Count];

        for(int i=0;i<vertices.Count;i++)
        {
            if(used[i]) continue;

            Vector3 sum=vertices[i];
            int count=1;
            used[i]=true;

            for(int j=i+1;j<vertices.Count;j++)
            {
                if(used[j]) continue;
                if(Vector3.Distance(vertices[i],vertices[j])<mergeRadius)
                {
                    sum+=vertices[j];
                    count++;
                    used[j]=true;
                    map[j]=newV.Count;
                }
            }

            map[i]=newV.Count;
            newV.Add(sum/count);
        }

        List<Edge> newE=new List<Edge>();
        foreach(var e in edges)
        {
            int a=map[e.a], b=map[e.b];
            if(a!=b) newE.Add(new Edge(a,b));
        }

        vertices=newV;
        edges=newE;
    }

    void RemoveShortEdges()
    {
        edges.RemoveAll(e =>
            Vector3.Distance(vertices[e.a],vertices[e.b])<minEdgeLength);
    }

    void RemoveFloatingNodes()
    {
        int n = vertices.Count;

        int[] degree = new int[n];
        foreach (var e in edges)
        {
            if (e.a < n && e.b < n)
            {
                degree[e.a]++;
                degree[e.b]++;
            }
        }

        int[] map = new int[n];
        List<Vector3> newVertices = new List<Vector3>();

        for (int i = 0; i < n; i++)
        {
            if (degree[i] == 0)
            {
                map[i] = -1;
            }
            else
            {
                map[i] = newVertices.Count;
                newVertices.Add(vertices[i]);
            }
        }

        List<Edge> newEdges = new List<Edge>();

        foreach (var e in edges)
        {
            int a = map[e.a];
            int b = map[e.b];

            if (a != -1 && b != -1 && a != b)
            {
                newEdges.Add(new Edge(a, b));
            }
        }

        vertices = newVertices;
        edges = newEdges;
    }

    void DeduplicateEdges()
    {
        HashSet<string> seen = new HashSet<string>();
        List<Edge> newEdges = new List<Edge>();

        foreach (var e in edges)
        {
            int a = Mathf.Min(e.a, e.b);
            int b = Mathf.Max(e.a, e.b);

            string key = a + "_" + b;

            if (!seen.Contains(key))
            {
                seen.Add(key);
                newEdges.Add(new Edge(a, b));
            }
        }

        edges = newEdges;
    }

    /// <summary>
    /// Breaks "micro-cycles" — the small redundant loops skeletonisation leaves at wide
    /// intersections — while preserving legitimate macro-cycles (real city blocks).
    ///
    /// Cycles are found by bounded DFS and only considered if their length is in
    /// [3, <paramref name="maxCycleLength"/>]; a real block far exceeds that and is untouched.
    /// Each flagged cycle is then thinned with a Kruskal MST over its own edges (Union-Find),
    /// which keeps the shortest structurally-necessary edges and discards the longest — almost
    /// always the artificial diagonal the raster grid introduced.
    /// </summary>
    void RemoveCycles(int maxCycleLength = 10)
    {
        int n = vertices.Count;

        // =========================
        // Build adjacency
        // =========================
        List<List<int>> adj = new List<List<int>>();
        for (int i = 0; i < n; i++) adj.Add(new List<int>());

        foreach (var e in edges)
        {
            adj[e.a].Add(e.b);
            adj[e.b].Add(e.a);
        }

        HashSet<string> edgesToRemove = new HashSet<string>();
        HashSet<string> visitedCycles = new HashSet<string>();

        // =========================
        // DFS to find cycles
        // =========================
        for (int start = 0; start < n; start++)
        {
            Stack<(int node, int parent, List<int> path)> stack =
                new Stack<(int, int, List<int>)>();

            stack.Push((start, -1, new List<int> { start }));

            while (stack.Count > 0)
            {
                var (cur, parent, path) = stack.Pop();

                foreach (int nb in adj[cur])
                {
                    if (nb == parent) continue;

                    int idx = path.IndexOf(nb);

                    if (idx != -1)
                    {
                        // found cycle
                        List<int> cycle = path.GetRange(idx, path.Count - idx);

                        if (cycle.Count < 3 || cycle.Count > maxCycleLength)
                            continue;

                        string key = string.Join(",", cycle);
                        if (visitedCycles.Contains(key)) continue;
                        visitedCycles.Add(key);
                        ProcessCycle(cycle, adj, edgesToRemove);
                    }
                    else
                    {
                        if (path.Count >= maxCycleLength) continue;

                        var newPath = new List<int>(path);
                        newPath.Add(nb);

                        stack.Push((nb, cur, newPath));
                    }
                }
            }
        }

        // =========================
        // Remove edges
        // =========================
        edges.RemoveAll(e =>
        {
            int a = Mathf.Min(e.a, e.b);
            int b = Mathf.Max(e.a, e.b);
            return edgesToRemove.Contains(a + "_" + b);
        });
    }

    void ProcessCycle(List<int> cycle,
                  List<List<int>> adj,
                  HashSet<string> edgesToRemove)
    {
        if (cycle == null || cycle.Count < 3) return;

        HashSet<int> set = new HashSet<int>(cycle);

        List<(int a, int b, float w)> edgesInCycle = new List<(int, int, float)>();

        foreach (int a in cycle)
        {
            foreach (int b in adj[a])
            {
                if (!set.Contains(b)) continue;
                if (a < b) // avoid duplicates
                {
                    float w = Vector3.Distance(vertices[a], vertices[b]);
                    edgesInCycle.Add((a, b, w));
                }
            }
        }

        edgesInCycle.Sort((x, y) => x.w.CompareTo(y.w));

        Dictionary<int, int> parent = new Dictionary<int, int>();

        int Find(int x)
        {
            if (parent[x] != x)
                parent[x] = Find(parent[x]);
            return parent[x];
        }

        void Union(int a, int b)
        {
            parent[Find(a)] = Find(b);
        }

        foreach (int v in cycle)
            parent[v] = v;

        HashSet<string> keep = new HashSet<string>();

        foreach (var e in edgesInCycle)
        {
            if (Find(e.a) != Find(e.b))
            {
                Union(e.a, e.b);

                int min = Mathf.Min(e.a, e.b);
                int max = Mathf.Max(e.a, e.b);

                keep.Add(min + "_" + max);
            }
        }

        foreach (var e in edgesInCycle)
        {
            int min = Mathf.Min(e.a, e.b);
            int max = Mathf.Max(e.a, e.b);
            string key = min + "_" + max;

            if (!keep.Contains(key))
                edgesToRemove.Add(key);
        }
    }

    /// <summary>
    /// Prunes degree-1 nodes that are skeletonisation artefacts rather than real dead ends.
    /// Two cases are removed:
    ///   1. the neighbour is a junction (degree > 2)  -> a micro-stub hanging off an intersection;
    ///   2. the neighbour is a through-road (degree 2) and the branch turns > 75 deg with no
    ///      forward continuation within 5 m -> a spur into a driveway or wide sidewalk.
    /// </summary>
    void RemoveEndpoint()
    {
        int n = vertices.Count;

        int[] degree = new int[n];
        foreach (var e in edges)
        {
            if (e.a < n && e.b < n)
            {
                degree[e.a]++;
                degree[e.b]++;
            }
        }
        List<List<int>> adj = new List<List<int>>();
        for (int i = 0; i < n; i++)
            adj.Add(new List<int>());

        foreach (var e in edges)
        {
            adj[e.a].Add(e.b);
            adj[e.b].Add(e.a);
        }

        HashSet<int> removeNodes = new HashSet<int>();
        HashSet<string> removeEdges = new HashSet<string>();

        for (int i = 0; i < n; i++)
        {
            if (degree[i] != 1) continue;

            int neighbor = adj[i][0];
            bool shouldRemove = false;

            if (degree[neighbor] > 2) shouldRemove = true;
            else if (degree[neighbor] == 2)
            {
                Vector3 dir_in = (vertices[neighbor] - vertices[i]).normalized;
                foreach (int k in adj[neighbor])
                {
                    if (k == i) continue;

                    Vector3 dir_out = (vertices[k] - vertices[neighbor]).normalized;

                    float angle = Vector3.Angle(dir_in, dir_out);

                    if (angle > 75f)
                    {
                        bool hasForward = HasForwardCandidate(i, neighbor, 5f, 32f);

                        if (!hasForward)
                        {
                            shouldRemove = true;
                            break;
                        }
                    }
                }
            }
            if (shouldRemove)
            {
                removeNodes.Add(i);

                int a = Mathf.Min(i, neighbor);
                int b = Mathf.Max(i, neighbor);
                removeEdges.Add(a + "_" + b);
            }
        }

        edges.RemoveAll(e =>
        {
            int a = Mathf.Min(e.a, e.b);
            int b = Mathf.Max(e.a, e.b);
            return removeEdges.Contains(a + "_" + b);
        });

        int[] map = new int[n];
        List<Vector3> newVertices = new List<Vector3>();

        for (int i = 0; i < n; i++)
        {
            if (removeNodes.Contains(i))
            {
                map[i] = -1;
            }
            else
            {
                map[i] = newVertices.Count;
                newVertices.Add(vertices[i]);
            }
        }

        List<Edge> newEdges = new List<Edge>();

        foreach (var e in edges)
        {
            int a = map[e.a];
            int b = map[e.b];

            if (a != -1 && b != -1 && a != b)
            {
                newEdges.Add(new Edge(a, b));
            }
        }

        vertices = newVertices;
        edges = newEdges;
    }
    void Connect()
    {
        int n = vertices.Count;

        int[] degree = new int[n];
        foreach (var e in edges)
        {
            if (e.a < n && e.b < n)
            {
                degree[e.a]++;
                degree[e.b]++;
            }
        }

        for (int i = 0; i < n; i++)
        {
            if (degree[i] != 0) continue;

            float bestDist = float.MaxValue;
            int bestJ = -1;

            for (int j = 0; j < n; j++)
            {
                if (i == j) continue;

                if (degree[j] >= 2) continue;

                float d = Vector3.Distance(vertices[i], vertices[j]);

                if (d < bestDist)
                {
                    bestDist = d;
                    bestJ = j;
                }
            }

            if (bestJ != -1)
            {
                edges.Add(new Edge(i, bestJ));
                degree[i]++;
                degree[bestJ]++;
            }
        }

        int iter = 2;
        while (iter-- > 0)
        {
            RemoveEndpoint();
            n = vertices.Count;
            degree = new int[n];
            foreach (var e in edges)
            {
                if (e.a < n && e.b < n)
                {
                    degree[e.a]++;
                    degree[e.b]++;
                }
            }
        }
        
        float radius = 10f;
        float angleThreshold = 40f;

        HashSet<string> existing = new HashSet<string>();
        foreach (var e in edges)
        {
            int a = Mathf.Min(e.a, e.b);
            int b = Mathf.Max(e.a, e.b);
            existing.Add(a + "_" + b);
        }

        List<CandidateEdge> candidates = new List<CandidateEdge>();
        for (int i = 0; i < n; i++)
        {
            if (degree[i] != 1) continue;

            int neighbor = -1;
            foreach (var e in edges)
            {
                if (e.a == i) { neighbor = e.b; break; }
                if (e.b == i) { neighbor = e.a; break; }
            }

            if (neighbor == -1) continue;

            Vector3 dir0 = (vertices[i] - vertices[neighbor]).normalized;


            for (int j = 0; j < n; j++)
            {
                if (j == i || j == neighbor) continue;

                float dist = Vector3.Distance(vertices[i], vertices[j]);
                if (dist > radius) continue;

                int min = Mathf.Min(i, j);
                int max = Mathf.Max(i, j);
                if (existing.Contains(min + "_" + max)) continue;

                Vector3 dir1 = (vertices[j] - vertices[i]).normalized;
                float angle = Vector3.Angle(dir0, dir1);

                if (angle <= angleThreshold)
                {
                    candidates.Add(new CandidateEdge(i, j, dist, angle));
                }
            }
        }
        candidates.Sort();
        bool[] vis = new bool[n];
        foreach (var c in candidates)
        {
            int a = c.a;
            int b = c.b;

            if (vis[a]) continue;

            int min = Mathf.Min(a, b);
            int max = Mathf.Max(a, b);

            if (existing.Contains(min + "_" + max)) continue;

            edges.Add(new Edge(a, b));

            degree[a]++;
            degree[b]++;
            vis[a] = true;

            existing.Add(min + "_" + max);
        }

        List<List<int>> adj = new List<List<int>>();
        for (int i = 0; i < n; i++)
            adj.Add(new List<int>());

        foreach (var e in edges)
        {
            adj[e.a].Add(e.b);
            adj[e.b].Add(e.a);
        }

        for (int i = 0; i < n; i++)
        {
            if (degree[i] == 2)
            {
                int u = adj[i][0];
                int v = adj[i][1];
                if (Vector3.Angle(vertices[u] - vertices[i], vertices[v] - vertices[i]) < 135f)
                {
                    float minDist = 15f;
                    int w = -1;
                    for (int j = 0; j < n; j++)
                    {
                        if (j == i || j == u || j == v) continue;
                        float dist = Vector3.Distance(vertices[i], vertices[j]);
                        if ((degree[j] > 2 || (degree[j] == 2 && Vector3.Angle(vertices[adj[j][0]] - vertices[j], vertices[adj[j][1]] - vertices[j]) < 135f)) && dist < minDist)
                        {
                            minDist = dist;
                            w = j;
                        }
                    }
                    if (w == -1)
                    {
                        continue;
                    }
                    int min = Mathf.Min(i, w);
                    int max = Mathf.Max(i, w);
                    if (existing.Contains(min + "_" + max)) continue;
                    edges.Add(new Edge(i, w));
                    degree[i]++;
                    degree[w]++;
                    existing.Add(min + "_" + max);
                }
            }
        }
    }

    bool HasForwardCandidate(int i, int neighbor, float radius, float maxAngle)
    {
        Vector3 forward = (vertices[i] - vertices[neighbor]).normalized;

        for (int j = 0; j < vertices.Count; j++)
        {
            if (j == i || j == neighbor) continue;

            float dist = Vector3.Distance(vertices[i], vertices[j]);
            if (dist > radius) continue;

            Vector3 dir = (vertices[j] - vertices[i]).normalized;
            float angle = Vector3.Angle(forward, dir);

            if (angle < maxAngle)
            {
                return true; // found a forward continuation
            }
        }

        return false;
    }

    struct CandidateEdge : IComparable<CandidateEdge>
    {
        public int a, b;
        public float dist;
        public float angle;

        public CandidateEdge(int a, int b, float dist, float angle)
        {
            this.a = a;
            this.b = b;
            this.dist = dist;
            this.angle = angle;
        }

        public int CompareTo(CandidateEdge other)
        {
            return dist.CompareTo(other.dist);
        }

    }

    /// <summary>
    /// Dissolves degree-2 nodes lying on a near-straight line (interior angle above
    /// <paramref name="angleThreshold"/>), stitching their two neighbours directly together.
    /// This is what shrinks the pixel-graph to intersections: it runs as a worklist, re-queueing
    /// neighbours that become degree-2 as a result, so long straight runs collapse in one pass.
    /// Junctions (degree >= 3) are never candidates, so they anchor the resulting edges.
    /// </summary>
    void RemoveStraightVertices(float angleThreshold = 165f)
    {
        int n = vertices.Count;

        List<HashSet<int>> adj = new List<HashSet<int>>();
        int[] degree = new int[n];

        for (int i = 0; i < n; i++)
            adj.Add(new HashSet<int>());

        foreach (var e in edges)
        {
            adj[e.a].Add(e.b);
            adj[e.b].Add(e.a);
            degree[e.a]++;
            degree[e.b]++;
        }

        Queue<int> q = new Queue<int>();

        for (int i = 0; i < n; i++)
            if (degree[i] == 2)
                q.Enqueue(i);

        bool[] removed = new bool[n];

        while (q.Count > 0)
        {
            int u = q.Dequeue();

            if (removed[u] || degree[u] != 2) continue;

            var it = adj[u].GetEnumerator();
            it.MoveNext();
            int v = it.Current;
            it.MoveNext();
            int w = it.Current;

            Vector3 d1 = (vertices[v] - vertices[u]).normalized;
            Vector3 d2 = (vertices[w] - vertices[u]).normalized;

            float angle = Vector3.Angle(d1, d2);

            if (angle <= angleThreshold) continue;

            removed[u] = true;

            adj[v].Remove(u);
            adj[w].Remove(u);

            degree[v]--;
            degree[w]--;

            if (v != w && !adj[v].Contains(w))
            {
                adj[v].Add(w);
                adj[w].Add(v);

                degree[v]++;
                degree[w]++;
            }

            if (degree[v] == 2) q.Enqueue(v);
            if (degree[w] == 2) q.Enqueue(w);
        }

        int[] map = new int[n];
        List<Vector3> newVertices = new List<Vector3>();

        for (int i = 0; i < n; i++)
        {
            if (removed[i]) map[i] = -1;
            else
            {
                map[i] = newVertices.Count;
                newVertices.Add(vertices[i]);
            }
        }

        List<Edge> newEdges = new List<Edge>();

        for (int i = 0; i < n; i++)
        {
            if (removed[i]) continue;

            foreach (int j in adj[i])
            {
                if (i < j && !removed[j])
                {
                    int a = map[i];
                    int b = map[j];

                    if (a != b)
                        newEdges.Add(new Edge(a, b));
                }
            }
        }

        vertices = newVertices;
        edges = newEdges;
    }

    // ================= EXPORT =================
    // Emits Assets/road_graph.json, consumed by:
    //   * WaypointEngine.LoadWaypointsFromJson (Unity offline fallback), and
    //   * db_pipeline_initializer.py (declusters and loads into meo_waypoints/meo_edges).
    //
    // ids are written as JSON *strings*. RoadVertex.id / RoadEdge.from / RoadEdge.to are all
    // declared `string`, and Unity's JsonUtility will not coerce a bare number into one — the
    // fallback loader silently produced an empty graph while these were emitted unquoted.
    // Invariant culture keeps the decimal separator a '.' on locales that default to ','.
    void ExportJson()
    {
        string path=Application.dataPath+"/road_graph.json";
        var ci = System.Globalization.CultureInfo.InvariantCulture;

        using(StreamWriter sw=new StreamWriter(path))
        {
            sw.Write("{\"vertices\":[");
            for(int i=0;i<vertices.Count;i++)
            {
                var v=vertices[i];
                sw.Write($"{{\"id\":\"{i}\",\"x\":{v.x.ToString(ci)},\"y\":{v.y.ToString(ci)},\"z\":{v.z.ToString(ci)}}}");
                if(i<vertices.Count-1) sw.Write(",");
            }
            sw.Write("],\"edges\":[");

            for(int i=0;i<edges.Count;i++)
            {
                var e=edges[i];
                sw.Write($"{{\"from\":\"{e.a}\",\"to\":\"{e.b}\"}}");
                if(i<edges.Count-1) sw.Write(",");
            }

            sw.Write("]}");
        }

        Debug.Log($"JSON exported: {vertices.Count} vertices, {edges.Count} edges -> {path}");
    }

    void OnDrawGizmos()
    {
        if(vertices==null || edges==null) return;

        Gizmos.color=Color.red;
        foreach(var v in vertices)
            Gizmos.DrawSphere(v,0.3f);

        // [ExecuteAlways] means gizmos can be drawn while Generate() is midway through
        // rebuilding these lists, so indices are bounds-checked rather than trusted.
        Gizmos.color=Color.green;
        foreach(var e in edges)
        {
            if(e.a<0||e.a>=vertices.Count||e.b<0||e.b>=vertices.Count) continue;
            Gizmos.DrawLine(vertices[e.a],vertices[e.b]);
        }
    }
}
