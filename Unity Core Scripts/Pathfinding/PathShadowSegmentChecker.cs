using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.AI;

/// <summary>
/// LEGACY / EXPERIMENTAL prototype, kept for reference.
///
/// Walks a NavMesh path from A to B, samples it densely, and prints a crude 10-bit shade
/// "barcode" per <see cref="RoadSegment"/> it crosses. It predates the PostGIS pipeline and is
/// not part of it: production exposure comes from <see cref="ShadowAwarePathFinder"/> writing
/// per-sample booleans to `meo_exposure_samples`.
///
/// Requirements it does NOT create for you: a baked NavMesh, a LineRenderer on this object, and
/// <see cref="RoadSegment"/> components on the road meshes. Without those it logs and no-ops.
/// </summary>
public class PathShadowSegmentChecker : MonoBehaviour
{
    public Transform startPoint;
    public Transform endPoint;
    public LayerMask roadLayer;          // Layer where roads are located, e.g., "Road"
    public int pathSampleCount = 200;    // Path sampling density (higher is more accurate, 100~500 recommended)

    private LineRenderer lineRenderer;
    private Light cachedSun;

    void Start()
    {
        // Guard the whole setup: this component is optional tooling and must never break a
        // scene just because its dependencies aren't wired up.
        if (startPoint == null || endPoint == null)
        {
            Debug.LogWarning("[PathShadowSegmentChecker] startPoint/endPoint not assigned; skipping.");
            enabled = false;
            return;
        }

        lineRenderer = GetComponent<LineRenderer>();
        if (lineRenderer == null)
        {
            Debug.LogWarning("[PathShadowSegmentChecker] No LineRenderer on this GameObject; skipping.");
            enabled = false;
            return;
        }

        lineRenderer.startColor = Color.red;
        lineRenderer.endColor = Color.red;
        lineRenderer.widthMultiplier = 0.2f;

        // Resolved once — IsInShadow is called pathSampleCount times and a scene-wide
        // FindObjectOfType per sample would be needlessly expensive.
        cachedSun = FindObjectOfType<Light>();

        EncodePathShadowPattern();
    }

   public void EncodePathShadowPattern()
    {
        // 1. Calculate NavMesh path from A to B
        NavMeshPath navPath = new NavMeshPath();
        if (!NavMesh.CalculatePath(startPoint.position, endPoint.position, NavMesh.AllAreas, navPath) || navPath.corners.Length < 2)
        {
            Debug.LogError("Failed to calculate path! Please check:\n- Has NavMesh been baked?\n- Are start/end points in walkable areas?");
            return;
        }

        List<Vector3> pathCorners = new List<Vector3>(navPath.corners);
        DrawPath(pathCorners);

        // 2. Data structure: track (total samples, shadow samples) per road segment
        var segmentStats = new Dictionary<RoadSegment, (int total, int shadow)>();
        var orderedSegments = new List<RoadSegment>(); // In path order (deduplicated)
        var seenSegments = new HashSet<RoadSegment>();

        // 3. Dense sampling along the path
        for (int i = 0; i < pathSampleCount; i++)
        {
            float t = (float)i / (pathSampleCount - 1);
            Vector3 posOnPath = GetPointOnPath(pathCorners, t);
            Vector3 samplePos = posOnPath + Vector3.up * 0.4f; // Elevate to avoid ground collision

            // Detect which RoadSegment this point belongs to
            Collider[] hits = Physics.OverlapSphere(samplePos, 0.6f, roadLayer);
            RoadSegment currentSegment = null;

            foreach (var hit in hits)
            {
                currentSegment = hit.GetComponent<RoadSegment>();
                if (currentSegment != null) break;
            }

            if (currentSegment == null) continue; // Not on any road segment (skip)

            // Update stats
            if (!segmentStats.ContainsKey(currentSegment))
                segmentStats[currentSegment] = (0, 0);

            var (total, shadow) = segmentStats[currentSegment];
            bool inShadow = IsInShadow(samplePos);
            segmentStats[currentSegment] = (total + 1, shadow + (inShadow ? 1 : 0));

            // Record order (first appearance)
            if (!seenSegments.Contains(currentSegment))
            {
                seenSegments.Add(currentSegment);
                orderedSegments.Add(currentSegment);
            }
        }

        // 4. Output results: generate 10-bit code per segment
        if (orderedSegments.Count == 0)
        {
            Debug.LogWarning("Path does not cross any RoadSegment. Attach RoadSegment components " +
                             "to the road meshes and make sure they are on the configured roadLayer.");
            return;
        }

        foreach (var seg in orderedSegments)
        {
            var (total, shadow) = segmentStats[seg];
            float ratio = (float)shadow / total; // Shadow ratio

            // Convert to 10 bits: First N bits are '1', rest are '0'
            int onesCount = Mathf.RoundToInt(ratio * 10);
            string binaryCode = new string('1', onesCount) + new string('0', 10 - onesCount);

            Debug.Log($"{seg.name}: {binaryCode}");
        }
    }

    // Interpolate along the path to get any point at t in [0,1]
    Vector3 GetPointOnPath(List<Vector3> path, float t)
    {
        if (t <= 0) return path[0];
        if (t >= 1) return path[path.Count - 1];

        float totalLength = 0f;
        var lengths = new List<float>();
        for (int i = 0; i < path.Count - 1; i++)
        {
            float d = Vector3.Distance(path[i], path[i + 1]);
            lengths.Add(d);
            totalLength += d;
        }

        float targetDist = t * totalLength;
        float walked = 0f;

        for (int i = 0; i < lengths.Count; i++)
        {
            if (walked + lengths[i] >= targetDist)
            {
                float segmentT = (targetDist - walked) / lengths[i];
                return Vector3.Lerp(path[i], path[i + 1], segmentT);
            }
            walked += lengths[i];
        }

        return path[path.Count - 1];
    }

    // Shadow detection: Raycast towards the sun, ignoring the road/ground itself
    bool IsInShadow(Vector3 position)
    {
        Light sun = cachedSun;
        if (sun == null || sun.type != LightType.Directional)
            return false;

        Vector3 lightDir = -sun.transform.forward;
        float maxDistance = 100f;

        // Use QueryTriggerInteraction.Collide to ensure triggers don't affect raycast (if using Triggers)
        if (Physics.Raycast(position, lightDir, out RaycastHit hit, maxDistance))
        {
            // Key: If the hit object is in Road or Ground layer, it's considered "unoccluded"
            int roadGroundMask = LayerMask.GetMask("Road", "Ground");
            if ((roadGroundMask & (1 << hit.collider.gameObject.layer)) != 0)
            {
                return false; // Ground/Road -> Direct sunlight
            }

            // Otherwise, hit a building, tree, etc. -> Shadow
            return true;
        }

        return false; // Unoccluded -> Sunlight
    }

    // Visualize the path
    void DrawPath(List<Vector3> path)
    {
        lineRenderer.positionCount = path.Count;
        lineRenderer.SetPositions(path.ToArray());
    }
}
