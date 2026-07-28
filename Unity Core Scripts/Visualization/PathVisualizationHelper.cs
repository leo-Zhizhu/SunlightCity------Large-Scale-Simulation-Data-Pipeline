using UnityEngine;

/// <summary>
/// Visualizes Start Point and End Point with distinctive 3D markers.
/// Attach this to the same GameObject that has ShadowAwarePathFinder.
/// Automatically retrieves start/end points from the path finder if not manually set.
/// </summary>
public class PathVisualizationHelper : MonoBehaviour
{
    [Header("Point References")]
    [Tooltip("Start position transform (auto-populated from ShadowAwarePathFinder if empty)")]
    public Transform startPoint;
    
    [Tooltip("End position transform (auto-populated from ShadowAwarePathFinder if empty)")]
    public Transform endPoint;

    [Header("Marker Settings")]
    [Tooltip("Scale multiplier for markers")]
    public float markerScale = 5f;
    
    [Tooltip("Color for start point marker")]
    public Color startColor = new Color(0.2f, 0.8f, 0.2f); // Green
    
    [Tooltip("Color for end point marker")]
    public Color endColor = new Color(0.9f, 0.2f, 0.2f); // Red
    
    [Tooltip("Height above ground to place markers")]
    public float markerHeight = 5f;

    [Header("Marker Icon")]
    [Tooltip("Use image icon marker (quad with transparent texture) when texture is assigned")]
    public bool useImageMarkerIcon = true;

    [Tooltip("Start marker icon texture (PNG with transparency)")]
    public Texture2D startMarkerIcon;

    [Tooltip("End marker icon texture (PNG with transparency)")]
    public Texture2D endMarkerIcon;

    [Tooltip("Icon width scale multiplier")]
    public float markerIconWidthScale = 1.8f;

    [Tooltip("Icon height scale multiplier")]
    public float markerIconHeightScale = 2.3f;

    [Header("Label Settings")]
    [Tooltip("Show START/END text labels above markers")]
    public bool showLabels = true;
    
    [Tooltip("Font size for labels")]
    public int labelFontSize = 58;

    [Header("Animation")]
    [Tooltip("Enable floating animation")]
    public bool animateMarkers = true;
    
    [Tooltip("Rotation speed for markers")]
    public float rotationSpeed = 30f;

    // Internal references for created marker objects
    private GameObject startMarkerRoot;
    private GameObject endMarkerRoot;
    private TextMesh startLabel;
    private TextMesh endLabel;
    private bool isVisible = true;

    public void SetVisible(bool visible)
    {
        isVisible = visible;
        if (startMarkerRoot != null) startMarkerRoot.SetActive(visible);
        if (endMarkerRoot != null) endMarkerRoot.SetActive(visible);
    }

    void Start()
    {
        // Auto-populate references from ShadowAwarePathFinder if not set
        if (startPoint == null || endPoint == null)
        {
            var pathFinder = GetComponent<ShadowAwarePathFinder>();
            if (pathFinder != null)
            {
                startPoint = pathFinder.startPoint;
                endPoint = pathFinder.endPoint;
            }
        }

        CreateMarkers();
    }

    void CreateMarkers()
    {
        // Create START marker - Arrow/Diamond shape pointing up
        if (startPoint != null)
        {
            startMarkerRoot = CreateStartMarker();
            startLabel = CreateWorldLabel(startMarkerRoot, "START", startColor);
        }

        // Create END marker - Flag/Target shape
        if (endPoint != null)
        {
            endMarkerRoot = CreateEndMarker();
            endLabel = CreateWorldLabel(endMarkerRoot, "END", endColor);
        }
    }

    private TextMesh CreateWorldLabel(GameObject root, string text, Color color)
    {
        GameObject lbl = new GameObject("Label");
        lbl.transform.SetParent(root.transform, false);
        // Position below the marker
        lbl.transform.localPosition = Vector3.down * 1.5f;
        
        TextMesh tm = lbl.AddComponent<TextMesh>();
        tm.text = text;
        tm.fontSize = 250; // Increased size
        tm.characterSize = 3.0f; // Increased size
        tm.anchor = TextAnchor.MiddleCenter;
        tm.alignment = TextAlignment.Center;
        tm.color = color;
        tm.fontStyle = FontStyle.Bold;
        
        // Static rotation: Face the sky (readable from top view)
        lbl.transform.localRotation = Quaternion.Euler(90, 0, 0);
        
        return tm;
    }

    /// <summary>
    /// Create a distinctive start marker (upward arrow/diamond shape)
    /// </summary>
    private GameObject CreateStartMarker()
    {
        if (useImageMarkerIcon && startMarkerIcon != null)
        {
            return CreateIconMarker("StartPointMarker", startMarkerIcon);
        }

        GameObject root = new GameObject("StartPointMarker");
        
        // Main diamond/arrow body (rotated cube)
        GameObject diamond = GameObject.CreatePrimitive(PrimitiveType.Cube);
        diamond.name = "Diamond";
        diamond.transform.SetParent(root.transform);
        diamond.transform.localScale = Vector3.one * markerScale * 0.8f;
        diamond.transform.localRotation = Quaternion.Euler(45f, 0f, 45f);
        diamond.transform.localPosition = Vector3.zero;
        diamond.GetComponent<Renderer>().material = CreateGlowMaterial(startColor);
        diamond.GetComponent<Collider>().enabled = false;

        // Small sphere on top (like a pin head)
        GameObject sphere = GameObject.CreatePrimitive(PrimitiveType.Sphere);
        sphere.name = "PinHead";
        sphere.transform.SetParent(root.transform);
        sphere.transform.localScale = Vector3.one * markerScale * 0.5f;
        sphere.transform.localPosition = Vector3.up * markerScale * 0.8f;
        sphere.GetComponent<Renderer>().material = CreateGlowMaterial(Color.white);
        sphere.GetComponent<Collider>().enabled = false;

        // Ring around the base
        GameObject ring = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        ring.name = "Ring";
        ring.transform.SetParent(root.transform);
        ring.transform.localScale = new Vector3(markerScale * 1.5f, markerScale * 0.1f, markerScale * 1.5f);
        ring.transform.localPosition = Vector3.down * markerScale * 0.5f;
        ring.GetComponent<Renderer>().material = CreateGlowMaterial(startColor);
        ring.GetComponent<Collider>().enabled = false;

        return root;
    }

    /// <summary>
    /// Create a distinctive end marker (flag/target shape)
    /// </summary>
    private GameObject CreateEndMarker()
    {
        if (useImageMarkerIcon && endMarkerIcon != null)
        {
            return CreateIconMarker("EndPointMarker", endMarkerIcon);
        }

        GameObject root = new GameObject("EndPointMarker");

        // Flag pole (thin cylinder)
        GameObject pole = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        pole.name = "FlagPole";
        pole.transform.SetParent(root.transform);
        pole.transform.localScale = new Vector3(markerScale * 0.15f, markerScale * 1.2f, markerScale * 0.15f);
        pole.transform.localPosition = Vector3.up * markerScale * 0.5f;
        pole.GetComponent<Renderer>().material = CreateGlowMaterial(Color.white);
        pole.GetComponent<Collider>().enabled = false;

        // Flag (cube stretched into flag shape)
        GameObject flag = GameObject.CreatePrimitive(PrimitiveType.Cube);
        flag.name = "Flag";
        flag.transform.SetParent(root.transform);
        flag.transform.localScale = new Vector3(markerScale * 1.2f, markerScale * 0.6f, markerScale * 0.1f);
        flag.transform.localPosition = new Vector3(markerScale * 0.5f, markerScale * 1.2f, 0f);
        flag.GetComponent<Renderer>().material = CreateGlowMaterial(endColor);
        flag.GetComponent<Collider>().enabled = false;

        // Target ring at base
        GameObject ring = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        ring.name = "TargetRing";
        ring.transform.SetParent(root.transform);
        ring.transform.localScale = new Vector3(markerScale * 1.5f, markerScale * 0.1f, markerScale * 1.5f);
        ring.transform.localPosition = Vector3.down * markerScale * 0.3f;
        ring.GetComponent<Renderer>().material = CreateGlowMaterial(endColor);
        ring.GetComponent<Collider>().enabled = false;

        // Inner target circle
        GameObject innerRing = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        innerRing.name = "InnerRing";
        innerRing.transform.SetParent(root.transform);
        innerRing.transform.localScale = new Vector3(markerScale * 0.8f, markerScale * 0.12f, markerScale * 0.8f);
        innerRing.transform.localPosition = Vector3.down * markerScale * 0.25f;
        innerRing.GetComponent<Renderer>().material = CreateGlowMaterial(Color.white);
        innerRing.GetComponent<Collider>().enabled = false;

        return root;
    }

    private GameObject CreateIconMarker(string rootName, Texture2D iconTexture)
    {
        GameObject root = new GameObject(rootName);

        GameObject quad = GameObject.CreatePrimitive(PrimitiveType.Quad);
        quad.name = "IconQuad";
        quad.transform.SetParent(root.transform);
        quad.transform.localPosition = Vector3.zero;
        quad.transform.localRotation = Quaternion.identity;
        quad.transform.localScale = new Vector3(
            markerScale * markerIconWidthScale,
            markerScale * markerIconHeightScale,
            1f
        );

        Renderer renderer = quad.GetComponent<Renderer>();
        Material mat = new Material(Shader.Find("Sprites/Default"));
        mat.mainTexture = iconTexture;
        mat.color = Color.white;
        renderer.material = mat;

        Collider col = quad.GetComponent<Collider>();
        if (col != null) col.enabled = false;

        return root;
    }

    /// <summary>
    /// Create a bright, visible material
    /// </summary>
    Material CreateGlowMaterial(Color color)
    {
        Material mat = new Material(Shader.Find("Unlit/Color"));
        mat.color = color;
        return mat;
    }

    void Update()
    {
        // Update start marker
        if (startMarkerRoot != null && startPoint != null)
        {
            Vector3 basePos = startPoint.position + Vector3.up * markerHeight;
            
            if (animateMarkers)
            {
                // Floating animation
                float bob = Mathf.Sin(Time.time * 2f) * 0.3f;
                startMarkerRoot.transform.position = basePos + Vector3.up * bob;
                
                // Slow rotation for geometric marker only
                if (!IsIconMarker(startMarkerRoot))
                {
                    startMarkerRoot.transform.Rotate(Vector3.up, rotationSpeed * Time.deltaTime);
                }
            }
            else
            {
                startMarkerRoot.transform.position = basePos;
            }

            FaceIconToCamera(startMarkerRoot);
        }

        // Update end marker
        if (endMarkerRoot != null && endPoint != null)
        {
            Vector3 basePos = endPoint.position + Vector3.up * markerHeight;
            
            if (animateMarkers)
            {
                // Floating animation (offset phase)
                float bob = Mathf.Sin(Time.time * 2f + Mathf.PI) * 0.3f;
                endMarkerRoot.transform.position = basePos + Vector3.up * bob;
                
                // Slight sway for geometric marker only
                if (!IsIconMarker(endMarkerRoot))
                {
                    float wave = Mathf.Sin(Time.time * 3f) * 5f;
                    endMarkerRoot.transform.rotation = Quaternion.Euler(0f, wave, 0f);
                }
            }
            else
            {
                endMarkerRoot.transform.position = basePos;
            }

            FaceIconToCamera(endMarkerRoot);
        }

        // Billboarding for labels
        if (isVisible)
        {
            if (startLabel != null) FaceLabelToCamera(startLabel.transform);
            if (endLabel != null) FaceLabelToCamera(endLabel.transform);
        }
    }

    // Intentionally a no-op. Labels are pinned face-up (Euler 90,0,0) in CreateWorldLabel so
    // they stay readable in the top-down view used for exposure inspection; billboarding them
    // toward the camera made them illegible from overhead. Kept as a hook in case a future
    // free-camera mode wants real billboarding back.
    private void FaceLabelToCamera(Transform labelTransform)
    {
    }

    private static bool IsIconMarker(GameObject root)
    {
        return root != null && root.transform.Find("IconQuad") != null;
    }

    private static void FaceIconToCamera(GameObject root)
    {
        if (!IsIconMarker(root) || Camera.main == null) return;

        Transform quad = root.transform.Find("IconQuad");
        if (quad == null) return;

        Vector3 lookDir = Camera.main.transform.position - quad.position;
        lookDir.y = 0f;
        if (lookDir.sqrMagnitude < 0.0001f) return;

        quad.rotation = Quaternion.LookRotation(-lookDir.normalized, Vector3.up);
    }



    void OnDestroy()
    {
        // Clean up created objects
        if (startMarkerRoot != null) Destroy(startMarkerRoot);
        if (endMarkerRoot != null) Destroy(endMarkerRoot);
    }
}
