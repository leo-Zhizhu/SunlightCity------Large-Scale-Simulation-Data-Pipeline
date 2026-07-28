using UnityEngine;
using UnityEditor;

public class AddCollidersToChildren : EditorWindow
{
    [MenuItem("Tools/Add MeshColliders to Selected")]
    public static void AddColliders()
    {
        GameObject[] selectedObjects = Selection.gameObjects;
        if (selectedObjects.Length == 0)
        {
            Debug.LogWarning("Please select at least one GameObject in the Hierarchy first.");
            return;
        }

        int addedCount = 0;
        
        foreach (GameObject selected in selectedObjects)
        {
            MeshFilter[] filters = selected.GetComponentsInChildren<MeshFilter>(true);

            foreach (MeshFilter f in filters)
            {
                if (f.gameObject.GetComponent<MeshCollider>() == null)
                {
                    MeshCollider mc = f.gameObject.AddComponent<MeshCollider>();
                    mc.sharedMesh = f.sharedMesh;
                    addedCount++;
                }
            }
        }

        Debug.Log($"Successfully added {addedCount} MeshColliders to the selected objects and their children.");
    }
}
