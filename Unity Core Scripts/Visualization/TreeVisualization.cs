using System.Collections.Generic;
using UnityEngine;
using ShadowAware.Engines;

public class TreeVisualization : MonoBehaviour
{
    // The full dataset holds ~1.28M trees. Spawning a GameObject per tree would freeze the
    // Editor and exhaust memory, so the debug overlay is capped; raise only if you have
    // narrowed the bounding box first.
    private const int MaxMarkers = 50000;

    private List<GameObject> treeMarkers = new List<GameObject>();
    private bool isVisible = false;

    public void UpdateTreeMarkers(List<TreePoint> trees, Transform parent, bool visible)
    {
        ClearMarkers();
        isVisible = visible;

        if (!visible || trees == null) return;

        if (trees.Count > MaxMarkers)
        {
            Debug.LogWarning($"[TreeVisualization] {trees.Count:N0} trees in range; only the first " +
                             $"{MaxMarkers:N0} will be drawn. Shrink edgeBBoxPadding to see a full region.");
        }

        int drawn = 0;
        foreach (var tree in trees)
        {
            if (drawn++ >= MaxMarkers) break;

            // Use standard ball (sphere) like waypoints
            GameObject marker = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            marker.name = $"Tree_{tree.id}";
            
            // STRICT GLOBAL COORDINATES: No parent, absolute size.
            marker.transform.SetParent(null); 
            marker.transform.position = tree.position;
            marker.transform.localScale = Vector3.one * 5.0f; // Clear visibility without being "huge"

            if (marker.TryGetComponent<Renderer>(out var renderer))
            {
                // Vibrant green to differentiate from path waypoints
                renderer.material = new Material(Shader.Find("Unlit/Color")) 
                { 
                    color = Color.green 
                };
            }

            // Remove sphere collider to avoid interference
            Destroy(marker.GetComponent<SphereCollider>());
            
            treeMarkers.Add(marker);
        }
    }

    public void SetVisible(bool visible)
    {
        isVisible = visible;
        foreach (var marker in treeMarkers)
        {
            if (marker != null) marker.SetActive(visible);
        }
    }

    public void ClearMarkers()
    {
        foreach (var marker in treeMarkers)
        {
            if (marker != null) Destroy(marker);
        }
        treeMarkers.Clear();
    }
}
