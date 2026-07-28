using UnityEngine;
using System;

/// <summary>
/// Controls the sun's rotation to simulate a realistic day/night cycle.
/// Attach this to a Directional Light to create moving sun shadows.
/// 
/// Time-to-Sun Angle Mapping:
///   00:00 (midnight) = Sun below horizon (-90°)
///   06:00 (sunrise)  = Sun at eastern horizon (0°)
///   12:00 (noon)     = Sun directly overhead (90°)
///   18:00 (sunset)   = Sun at western horizon (180°)
///   24:00 (midnight) = Sun below horizon (270°/-90°)
/// </summary>
public class SunController : MonoBehaviour
{
    [Header("Playback Control")]
    [Tooltip("Whether the sun is currently moving through the day")]
    public bool isPlay = true;

    [Header("Time Settings")]
    [Tooltip("Starting hour of the day (0-24). Default 8 = 8:00 AM")]
    [Range(0f, 24f)]
    public float startingHour = 8f;

    [Tooltip("How many real-world seconds equal 24 simulation hours")]
    public float dayLengthSeconds = 60f;

    [Tooltip("Y-axis rotation offset (adjusts sun's east-west path, simulates latitude)")]
    public float worldTiltY = 0f;

    [Header("Date Settings")]
    [Tooltip("Current Year to simulate (2026/2027 etc)")]
    public int year = 2026;
    [Range(1, 12)]
    public int month = 6;
    [Range(1, 31)]
    public int day = 21;

    [Header("Data Source")]
    [Tooltip("Enable to use high-fidelity astronomical data from binary files")]
    public bool useHighFidelityData = false;
    [SerializeField] private SolarDataLoader dataLoader;

    [Header("Ground Plane")]
    [Tooltip("Create a visible ground plane to block sunlight from below")]
    public bool createGroundPlane = true;

    [Tooltip("Ground plane world position")]
    public Vector3 groundPlanePosition = new Vector3(-750.2874f, -124.2449f, 359.0026f);

    [Tooltip("Ground plane world rotation (Euler)")]
    public Vector3 groundPlaneRotation = Vector3.zero;

    [Tooltip("Ground plane scale (Plane is 10x10 units)")]
    public Vector3 groundPlaneScale = new Vector3(100000f, 1f, 100000f);

    [Tooltip("Ground plane color")]
    public Color groundPlaneColor = new Color(0.2f, 0.2f, 0.2f, 1f);


    // ==================== RUNTIME STATE ====================
    
    // Current simulation time in hours (0-24)
    private float currentHour;
    private DateTime currentSimDate;

    // UpdateSunRotation() runs every frame; this latch keeps the "no solar data" complaint
    // from flooding the console with thousands of identical lines.
    private bool warnedMissingSolarData;

    // ==================== PUBLIC PROPERTIES ====================

    /// <summary>
    /// Current time of day in hours (0-24).
    /// 0 = midnight, 6 = sunrise, 12 = noon, 18 = sunset
    /// </summary>
    public float CurrentHour => currentHour;

    /// <summary>
    /// Current time as normalized day progress (0-1).
    /// 0 = midnight, 0.5 = noon, 1 = midnight
    /// </summary>
    public float DayProgress => currentHour / 24f;

    /// <summary>
    /// Legacy linear approximation of sun elevation (15°/hour from a 06:00 sunrise).
    /// Kept only for editor tooling / rough gizmos — it does NOT reflect the
    /// high-fidelity dataset actually driving the light. For the real elevation read
    /// <c>transform.eulerAngles.x</c> or query <see cref="SolarDataLoader"/> directly.
    /// </summary>
    public float SunElevationAngle => (currentHour - 6f) * 15f;

    // ==================== LIFECYCLE ====================

    void Start()
    {
        // Initialize to starting hour and date
        currentHour = startingHour;
        try {
            currentSimDate = new DateTime(year, month, day);
        } catch {
            currentSimDate = new DateTime(year, 1, 1);
        }

        if (dataLoader == null) dataLoader = GetComponent<SolarDataLoader>();
        if (dataLoader != null) dataLoader.LoadYear(year);

        UpdateSunRotation();
        if (createGroundPlane)
        {
            EnsureGroundPlane();
        }
    }

    void Update()
    {
        // Sync internal date if inspector was manually edited
        if (currentSimDate.Year != year || currentSimDate.Month != month || currentSimDate.Day != day)
        {
            try { currentSimDate = new DateTime(year, month, day); } catch { }
        }

        if (!isPlay) return;

        // Calculate hours per second
        float hoursPerSecond = 24f / dayLengthSeconds;
        
        // Advance time
        currentHour += hoursPerSecond * Time.deltaTime;
        
        // Wrap around and increment date
        if (currentHour >= 24f)
        {
            currentHour -= 24f;
            currentSimDate = currentSimDate.AddDays(1);
            year = currentSimDate.Year;
            month = currentSimDate.Month;
            day = currentSimDate.Day;

            if (dataLoader != null && currentSimDate.DayOfYear == 1)
                dataLoader.LoadYear(year);
        }

        UpdateSunRotation();
    }

    // ==================== SUN ROTATION ====================

    /// <summary>
    /// Update the sun's rotation based on current hour.
    /// </summary>
    private void UpdateSunRotation()
    {
        if (dataLoader != null && dataLoader.IsLoaded)
        {
            // Use High-Fidelity Binary Data
            int minutes = Mathf.FloorToInt(currentHour * 60f);
            float fraction = (currentHour * 60f) - minutes;
            DateTime simTime = currentSimDate.Date.AddMinutes(minutes % 1440);
            
            var (azimuth, elevation) = dataLoader.GetPositionLerped(simTime, fraction);
            
            // Convert to elevation angle (X) and azimuth (Y)
            // Solar elevation is angle above horizon. 
            // In Unity, X rotation: 0 = horizon, 90 = overhead.
            // Add 180 to azimuth because transform.forward is the direction the light travels (away from the sun)
            transform.rotation = Quaternion.Euler(elevation, azimuth + 180f + worldTiltY, 0f);
            warnedMissingSolarData = false;
        }
        else if (!warnedMissingSolarData)
        {
            // Warn once per data-outage instead of once per frame.
            warnedMissingSolarData = true;
            Debug.LogWarning("[SunController] High-fidelity solar data is not loaded, so the sun will not move. " +
                             "Add a SolarDataLoader with a valid StreamingAssets/SolarData/<city>/sun_pos_<year>.bin " +
                             "(generate one via Tools -> Solar Data -> Preprocess CSV to Binary).");
        }
    }
    
    public DateTime GetCurrentDateTime()
    {
        try
        {
            // Construct from current fields to stay in sync with manual Inspector changes
            return new DateTime(year, month, day, 0, 0, 0).AddHours(currentHour);
        }
        catch
        {
            // Fallback to internal tracker if fields are invalid (e.g. Feb 30)
            return currentSimDate.Date.AddHours(currentHour);
        }
    }


    // ==================== PUBLIC METHODS ====================

    /// <summary>
    /// Set the current hour directly (0-24 range).
    /// </summary>
    /// <param name="hour">Hour of day (0=midnight, 12=noon, 18=sunset)</param>
    public void SetHour(float hour)
    {
        currentHour = Mathf.Repeat(hour, 24f);
        UpdateSunRotation();
    }

    /// <summary>
    /// Adjust the current time by a specific number of hours. 
    /// Automatically pauses playback to allow manual inspection.
    /// </summary>
    public void AddHours(float hoursToAdd)
    {
        isPlay = false;
        currentHour = Mathf.Repeat(currentHour + hoursToAdd, 24f);
        UpdateSunRotation();
    }

    /// <summary>
    /// Set time using normalized day progress (0-1 range).
    /// </summary>
    /// <param name="progress">0=midnight, 0.5=noon, 1=midnight</param>
    public void SetDayProgress(float progress)
    {
        currentHour = Mathf.Repeat(progress * 24f, 24f);
        UpdateSunRotation();
    }

    /// <summary>
    /// Get the direction vector pointing toward the sun.
    /// </summary>
    public Vector3 GetSunDirection()
    {
        return -transform.forward;
    }

    /// <summary>
    /// Get formatted time string (HH:MM format).
    /// </summary>
    public string GetTimeString()
    {
        int hours = Mathf.FloorToInt(currentHour);
        int minutes = Mathf.FloorToInt((currentHour - hours) * 60f);
        return $"{hours:D2}:{minutes:D2}";
    }

    /// <summary>
    /// Check if it's currently daytime (sun above horizon).
    /// Roughly 6:00 to 18:00.
    /// </summary>
    public bool IsDaytime()
    {
        return currentHour >= 5f && currentHour < 19f;
    }

    private void EnsureGroundPlane()
    {
        GameObject existing = GameObject.Find("CityGroundPlane");
        if (existing != null)
        {
            return;
        }

        GameObject ground = GameObject.CreatePrimitive(PrimitiveType.Plane);
        ground.name = "CityGroundPlane";
        ground.layer = 0; // Default
        ground.transform.position = groundPlanePosition;
        ground.transform.rotation = Quaternion.Euler(groundPlaneRotation);
        ground.transform.localScale = groundPlaneScale;

        Renderer renderer = ground.GetComponent<Renderer>();
        if (renderer != null && renderer.sharedMaterial != null)
        {
            // Use .material (per-renderer instance), not .sharedMaterial: primitives created at
            // runtime share Unity's built-in default material, and writing to it would recolour
            // every other object using that material — and persist the change in the Editor.
            renderer.material.color = groundPlaneColor;
        }
    }
}
