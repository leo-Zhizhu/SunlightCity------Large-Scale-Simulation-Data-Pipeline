using UnityEngine;
using System;
using TMPro;

// Drop-in replacement for SunMotion that reads pre-processed binary solar data
// instead of hitting the Flask API.
//
// Setup:
//   1. Run Tools → Solar Data → Preprocess CSV to Binary (one-time)
//   2. Add SolarDataLoader to this GameObject and set cityName
//   3. Swap SunMotion for SunMotion_LocalBinary on your Sun object

public class SunMotion_LocalBinary : MonoBehaviour
{
    [Header("Simulation Settings")]
    public bool isPlay = true;
    public float dayLengthSeconds = 60f;
    public float worldTiltY = 0f;

    [Header("Simulation Start Date")]
    public int startYear  = 2026;
    public int startMonth = 2;
    public int startDay   = 7;

    [Header("UI")]
    public TMP_Text timeDisplay;

    SolarDataLoader _loader;
    float           _elapsedTime = 0f;
    DateTime        _currentSimDate;

    void Start()
    {
        _loader = GetComponent<SolarDataLoader>();
        if (_loader == null)
        {
            Debug.LogError("[SunMotion_LocalBinary] SolarDataLoader component not found. " +
                           "Add it to this GameObject and set cityName.");
            enabled = false;
            return;
        }

        _currentSimDate = new DateTime(startYear, startMonth, startDay);

        if (!_loader.LoadYear(startYear))
        {
            Debug.LogError("[SunMotion_LocalBinary] Failed to load solar data binary. " +
                           "Run Tools → Solar Data → Preprocess CSV to Binary first.");
            enabled = false;
            return;
        }

        _elapsedTime = 0f;
        Debug.Log($"[SunMotion_LocalBinary] Ready. Simulation starts {_currentSimDate:yyyy-MM-dd}.");
    }

    void Update()
    {
        if (!isPlay) return;

        _elapsedTime += Time.deltaTime;

        // Day rollover
        if (_elapsedTime >= dayLengthSeconds)
        {
            _elapsedTime -= dayLengthSeconds;
            _currentSimDate = _currentSimDate.AddDays(1);

            // Year rollover — load next year's binary
            if (_currentSimDate.DayOfYear == 1)
                _loader.LoadYear(_currentSimDate.Year);
        }

        float dayProgress     = Mathf.Clamp01(_elapsedTime / dayLengthSeconds);
        float totalSimMinutes = dayProgress * 24f * 60f;           // 0 .. 1440
        int   minuteOfDay     = Mathf.FloorToInt(totalSimMinutes) % 1440;
        float minuteFraction  = totalSimMinutes - Mathf.Floor(totalSimMinutes);

        DateTime simTime = _currentSimDate.AddMinutes(minuteOfDay);
        var (azimuth, elevation) = _loader.GetPositionLerped(simTime, minuteFraction);

        float zenith = 90f - elevation;
        float sunAngle  = elevation;

        // Smooth transition at horizon (±5°)
        float blendRange  = 5f;
        float blendFactor = Mathf.InverseLerp(blendRange, -blendRange, elevation);
        float parkedAngle = -90f;
        float finalAngle  = Mathf.Lerp(parkedAngle, sunAngle, blendFactor);

        transform.rotation = Quaternion.Euler(finalAngle, azimuth + 180f + worldTiltY, 0f);

        RenderSettings.ambientIntensity = zenith >= 90f ? 0.1f : 1f;

        if (timeDisplay != null)
        {
            int hours   = minuteOfDay / 60;
            int minutes = minuteOfDay % 60;
            timeDisplay.text = $"{_currentSimDate:yyyy-MM-dd} {hours:00}:{minutes:00} | Z: {zenith:F1} A: {azimuth:F1}";
        }
    }
}
