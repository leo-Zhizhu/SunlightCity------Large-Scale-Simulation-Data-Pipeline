using UnityEngine;
using UnityEditor;

/// <summary>
/// A utility tool to pick and copy global coordinates in the Unity Scene View.
/// Especially useful for merged meshes where individual object pivots don't represent specific locations.
/// </summary>
public class CoordinatePicker : EditorWindow
{
    private Vector3 pickedPosition = Vector3.zero;
    private bool pickingMode = false;
    private float markerSize = 2f;

    [MenuItem("Window/Custom/Coordinate Picker")]
    public static void ShowWindow()
    {
        CoordinatePicker window = GetWindow<CoordinatePicker>("Coordinate Picker");
        window.minSize = new Vector2(300, 250);
    }

    private void OnGUI()
    {
        // Styling
        GUIStyle headerStyle = new GUIStyle(EditorStyles.boldLabel);
        headerStyle.fontSize = 14;
        headerStyle.margin = new RectOffset(0, 0, 10, 5);

        GUILayout.Label("Global Coordinate Picker", headerStyle);
        EditorGUILayout.HelpBox("Use this tool to find exact XYZ coordinates of buildings and landmarks.", MessageType.None);

        EditorGUILayout.Space();

        // --- SECTION 1: SELECTION ---
        GUILayout.Label("Selection Info", EditorStyles.boldLabel);
        GameObject current = Selection.activeGameObject;
        if (current != null)
        {
            EditorGUILayout.BeginVertical("box");
            EditorGUILayout.LabelField("Selected:", current.name);
            EditorGUILayout.Vector3Field("Pivot (World)", current.transform.position);
            
            if (GUILayout.Button("Copy Selection Position"))
            {
                CopyPosition(current.transform.position);
            }
            EditorGUILayout.EndVertical();
        }
        else
        {
            EditorGUILayout.HelpBox("Select an object in Hierarchy to see its pivot position.", MessageType.Info);
        }

        EditorGUILayout.Space();

        // --- SECTION 2: PRECISION PICKER ---
        GUILayout.Label("Precision Point Picker", EditorStyles.boldLabel);
        
        EditorGUILayout.BeginVertical("box");
        pickingMode = EditorGUILayout.ToggleLeft("Enable Click-to-Pick Mode", pickingMode, EditorStyles.boldLabel);
        
        if (pickingMode)
        {
            EditorGUILayout.HelpBox("SHIFT + LEFT CLICK in the Scene View to capture a specific point on a mesh.", MessageType.Warning);
        }
        else
        {
            EditorGUILayout.HelpBox("Toggle 'Enable Click-to-Pick' to start capturing points.", MessageType.Info);
        }

        EditorGUI.BeginDisabledGroup(pickedPosition == Vector3.zero);
        EditorGUILayout.Vector3Field("Captured Point", pickedPosition);
        
        EditorGUILayout.BeginHorizontal();
        if (GUILayout.Button("Copy Captured Point"))
        {
            CopyPosition(pickedPosition);
        }
        if (GUILayout.Button("Clear", GUILayout.Width(60)))
        {
            pickedPosition = Vector3.zero;
        }
        EditorGUILayout.EndHorizontal();
        EditorGUI.EndDisabledGroup();

        markerSize = EditorGUILayout.Slider("Marker Size", markerSize, 0.1f, 10f);
        EditorGUILayout.EndVertical();
    }

    private void CopyPosition(Vector3 pos)
    {
        string formatted = $"{pos.x:F3}, {pos.y:F3}, {pos.z:F3}";
        EditorGUIUtility.systemCopyBuffer = formatted;
        Debug.Log($"<color=green>Coordinates Copied to Clipboard:</color> {formatted}");
    }

    private void OnEnable()
    {
        // Hooks into the Scene View GUI drawing
        SceneView.duringSceneGui += OnSceneGUI;
    }

    private void OnDisable()
    {
        SceneView.duringSceneGui -= OnSceneGUI;
    }

    private void OnSceneGUI(SceneView sceneView)
    {
        if (!pickingMode)
        {
            // If not picking, still draw the marker if it exists
            DrawMarker();
            return;
        }

        Event e = Event.current;

        // Listen for Shift + Left Mouse Button
        if (e.type == EventType.MouseDown && e.button == 0 && e.shift)
        {
            // Raycast from the mouse position into the world
            Ray ray = HandleUtility.GUIPointToWorldRay(e.mousePosition);
            
            // Note: This requires the mesh to have a Collider.
            if (Physics.Raycast(ray, out RaycastHit hit))
            {
                pickedPosition = hit.point;
                Repaint(); // Refresh the window UI
                e.Use();   // Consume the event so it doesn't select other objects
                
                // Visual confirmation in console
                Debug.Log($"Captured Point: {pickedPosition}");
            }
        }

        DrawMarker();
    }

    private void DrawMarker()
    {
        if (pickedPosition != Vector3.zero)
        {
            Handles.color = Color.red;
            // Draw a sphere and a label at the picked position
            Handles.SphereHandleCap(0, pickedPosition, Quaternion.identity, markerSize, EventType.Repaint);
            
            GUIStyle labelStyle = new GUIStyle();
            labelStyle.normal.textColor = Color.yellow;
            labelStyle.fontSize = 14;
            labelStyle.fontStyle = FontStyle.Bold;

            Handles.Label(pickedPosition + Vector3.up * (markerSize * 1.5f), $"Captured: {pickedPosition}", labelStyle);
            
            // Force the SceneView to redraw to keep the marker visible
            SceneView.RepaintAll();
        }
    }
}
