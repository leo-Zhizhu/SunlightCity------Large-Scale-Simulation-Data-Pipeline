using UnityEngine;
using UnityEditor;
using System.Collections.Generic;

public class PhysicsDebugger : EditorWindow
{
    [MenuItem("GameObject/Diagnostics/Analyze Physics (Upcity Fix)", false, 10)]
    public static void AnalyzePhysics()
    {
        GameObject obj = Selection.activeGameObject;
        if (obj == null)
        {
            Debug.LogError("[PhysicsDebugger] Select a GameObject first!");
            return;
        }

        Debug.Log($"<color=cyan>--- Analyzing Physics for: {obj.name} ---</color>");

        // 1. Basic Component Checks
        MeshFilter mf = obj.GetComponent<MeshFilter>();
        MeshRenderer mr = obj.GetComponent<MeshRenderer>();
        MeshCollider mc = obj.GetComponent<MeshCollider>();

        if (mf == null || mf.sharedMesh == null)
        {
            Debug.LogError("[PhysicsDebugger] Missing MeshFilter or Mesh Data.");
            return;
        }

        if (mc == null)
        {
            Debug.LogError("[PhysicsDebugger] Missing MeshCollider component.");
            return;
        }

        if (mc.sharedMesh == null)
        {
            Debug.LogError("[PhysicsDebugger] MeshCollider has NO MESH assigned.");
        }
        else if (mc.sharedMesh != mf.sharedMesh)
        {
            Debug.LogWarning($"[PhysicsDebugger] MeshCollider uses a DIFFERENT mesh than visual MeshFilter! (Physics: {mc.sharedMesh.name}, Visual: {mf.sharedMesh.name})");
        }

        // 2. Bounds Verification
        if (mr != null)
        {
            Bounds vBounds = mr.bounds;
            Bounds pBounds = mc.bounds;
            
            float dist = Vector3.Distance(vBounds.center, pBounds.center);
            Debug.Log($"Visual Bounds Center: {vBounds.center}, Size: {vBounds.size}");
            Debug.Log($"Physics Bounds Center: {pBounds.center}, Size: {pBounds.size}");

            if (dist > 1.0f)
            {
                Debug.LogError($"[PhysicsDebugger] BIG OFFSET DETECTED! Physics bounds are {dist:F1} units away from visual bounds. This usually breaks raycasting.");
            }
            
            if (pBounds.size.magnitude < 0.1f)
            {
                Debug.LogError("[PhysicsDebugger] Physics bounds are EMPTY or near-zero. The collider is effectively invisible.");
            }
        }

        // 3. Topology Scan (Degeneracy)
        Mesh mesh = mf.sharedMesh;
        if (!mesh.isReadable)
        {
            // Reading .vertices on a mesh imported without "Read/Write Enabled" throws.
            Debug.LogWarning($"[PhysicsDebugger] Mesh '{mesh.name}' is not readable; enable 'Read/Write Enabled' " +
                             "on its import settings to run the topology scan. Skipping.");
            return;
        }

        Vector3[] verts = mesh.vertices;
        int[] tris = mesh.triangles;
        int degenerateCount = 0;
        float totalArea = 0;
        int totalTris = tris.Length / 3;

        for (int i = 0; i < tris.Length; i += 3)
        {
            Vector3 v0 = obj.transform.TransformPoint(verts[tris[i]]);
            Vector3 v1 = obj.transform.TransformPoint(verts[tris[i+1]]);
            Vector3 v2 = obj.transform.TransformPoint(verts[tris[i+2]]);

            float area = Vector3.Cross(v1 - v0, v2 - v0).magnitude * 0.5f;
            totalArea += area;

            if (area < 0.00001f)
            {
                degenerateCount++;
            }
        }

        Debug.Log($"Total Triangles: {totalTris}, Total Surface Area: {totalArea:F2}");
        if (degenerateCount > 0)
        {
            float percent = (float)degenerateCount / totalTris * 100f;
            Debug.LogWarning($"[PhysicsDebugger] FOUND {degenerateCount} DEGENERATE TRIANGLES ({percent:F1}%)! Physics engine (PhysX) may ignore these surfaces.");
        }

        // 4. Layer Matrix Check
        int layer = obj.layer;
        string layerName = LayerMask.LayerToName(layer);
        Debug.Log($"Object Layer: {layerName} ({layer})");

        if (Physics.GetIgnoreLayerCollision(layer, layer)) // Check if it ignores itself... simple proxy for "is this a weird layer"
        {
            Debug.LogWarning($"[PhysicsDebugger] Layer '{layerName}' is configured to ignore collisions with itself in Physics Settings.");
        }
        
        Debug.Log("<color=cyan>--- Analysis Complete ---</color>");
    }
}
