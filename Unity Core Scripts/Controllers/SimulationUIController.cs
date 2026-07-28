using UnityEngine;
using UnityEngine.Networking;
using System;
using System.Collections;

/// <summary>
/// Draws status/sun panels and removes legacy runtime/scene control buttons.
/// </summary>
public class SimulationUIController : MonoBehaviour
{
    [Header("Simulation Components")]
    [SerializeField] private SunController sunController;
    [SerializeField] private ShadowAwarePathFinder pathFinder;
    [SerializeField] private FreeCameraController freeCameraController;
    private PathVisualizationHelper[] visualizationHelpers;

    [Header("Status Panel")]
    [SerializeField] private bool showStatusPanel = true;

    [Header("Date API")]
    // Optional wall-clock lookup. Off by default: the panel always renders the *simulation*
    // date from SunController (see OnGUI), so a fetched real-world date would be overwritten
    // on the next frame anyway. Kept for scenes that want to display "today" instead.
    [SerializeField] private bool useDateApi = false;
    [SerializeField] private string dateApiUrl = "https://worldtimeapi.org/api/timezone/America/New_York";
    [SerializeField] private float dateRefreshSeconds = 300f;
    private string datesInput = "5.15, 8.1, 8.15";

    private static Texture2D panelTexture;
    private static Texture2D solidTexture;
    private static Texture2D roundedRectTexture;
    private string displayDate = "";

    private void Start()
    {
        RemoveLegacyButtonsAndCanvas();

        if (sunController == null)
        {
            sunController = FindObjectOfType<SunController>();
        }
        if (pathFinder == null)
        {
            pathFinder = FindObjectOfType<ShadowAwarePathFinder>();
        }
        if (freeCameraController == null)
        {
            freeCameraController = FindObjectOfType<FreeCameraController>();
        }

        if (sunController != null && !sunController.enabled)
        {
            sunController.enabled = true;
        }

        if (useDateApi && !string.IsNullOrWhiteSpace(dateApiUrl))
        {
            StartCoroutine(RefreshDateRoutine());
        }

        visualizationHelpers = FindObjectsByType<PathVisualizationHelper>(FindObjectsSortMode.None);

        if (pathFinder != null && !string.IsNullOrEmpty(pathFinder.exportTargetDates))
        {
            datesInput = pathFinder.exportTargetDates;
        }
    }

    private static void RemoveLegacyButtonsAndCanvas()
    {
        string[] legacyNames =
        {
            "RunButton",
            "PauseButton",
            "CalculateButton",
            "SwitchViewButton",
            "Switch ViewButton",
            "Run",
            "Pause",
            "Calculate",
            "Switch View",
            "RuntimeButtons"
        };

        for (int i = 0; i < legacyNames.Length; i++)
        {
            GameObject found = GameObject.Find(legacyNames[i]);
            if (found != null)
            {
                Destroy(found);
            }
        }

        GameObject runtimeCanvas = GameObject.Find("RuntimeCanvas");
        if (runtimeCanvas != null)
        {
            Destroy(runtimeCanvas);
        }
    }

    private void OnGUI()
    {
        if (sunController == null) return;

        if (!showStatusPanel)
        {
            // Small button in the bottom right to bring back the UI
            if (DrawStyledButton(new Rect(Screen.width - 240f, Screen.height - 120f, 200f, 80f), "SHOW UI", new Color(0.12f, 0.23f, 0.38f, 0.8f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
            {
                showStatusPanel = true;
                ToggleAllAssociatedUI(true);
            }
            return;
        }

        DrawControlsPanel();

        float panelX = 60f;
        float panelY = 60f;
        float panelWidth = 1140f;
        float shadowPanelHeight = 300f;
        float arcPanelHeight = 380f;
        float panelGap = 22f;

        Rect shadowRect = new Rect(panelX, panelY, panelWidth, shadowPanelHeight);
        Rect arcRect = new Rect(panelX, panelY + shadowPanelHeight + panelGap, panelWidth, arcPanelHeight);

        DrawPanel(shadowRect, new Color(1f, 1f, 1f, 0.75f));
        DrawPanel(arcRect, new Color(1f, 1f, 1f, 0.75f));

        bool isLocked = !sunController.isPlay;
        // Shadow Locked: Green, Moving: Yellow
        Color lockColor = isLocked ? new Color(0.20f, 0.92f, 0.52f, 1f) : new Color(0.96f, 0.82f, 0.12f, 1f);
        string lockText = isLocked ? "S H A D O W   L O C K E D" : "S H A D O W   M O V I N G";

        GUIStyle headerStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 40,
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = lockColor }
        };
        GUI.Label(new Rect(shadowRect.x + 46f, shadowRect.y + 24f, shadowRect.width - 80f, 50f), lockText, headerStyle);

        bool colonOn = Mathf.FloorToInt(Time.unscaledTime * 2f) % 2 == 0;
        string[] hm = sunController.GetTimeString().Split(':');
        string hh = hm.Length > 0 ? hm[0] : "00";
        string mm = hm.Length > 1 ? hm[1] : "00";

        GUIStyle timeStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 138,
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.89f, 0.93f, 0.98f, 1f) }
        };
        GUI.Label(new Rect(shadowRect.x + 46f, shadowRect.y + 62f, shadowRect.width - 90f, 150f), $"{hh}{(colonOn ? ":" : " ")}{mm}", timeStyle);

        GUIStyle periodStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 38,
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.52f, 0.62f, 0.82f, 1f) }
        };
        GUI.Label(new Rect(shadowRect.x + 46f, shadowRect.y + 210f, shadowRect.width - 90f, 44f), GetTimePeriodText(sunController.CurrentHour), periodStyle);

        GUIStyle arcTitle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 44, // Increased size
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f) } // Yellow
        };
        // Simulation date wins over any value fetched by RefreshDateRoutine.
        if (sunController != null) displayDate = sunController.GetCurrentDateTime().ToString("yyyy/MM/dd");
        GUI.Label(new Rect(arcRect.x + 46f, arcRect.y + 24f, arcRect.width - 80f, 40f),  $"S O L A R   A R C   -   {displayDate}" , arcTitle);

        Rect graphRect = new Rect(arcRect.x + 46f, arcRect.y + 76f, arcRect.width - 92f, arcRect.height - 112f);
        float horizonY = graphRect.yMax - 28f;
        DrawSolidRect(new Rect(graphRect.x, horizonY, graphRect.width, 2f), new Color(0.12f, 0.23f, 0.38f, 1f));

        Vector2 p0 = new Vector2(graphRect.x + 6f, horizonY);
        Vector2 p1 = new Vector2(graphRect.center.x, graphRect.y + 6f);
        Vector2 p2 = new Vector2(graphRect.xMax - 6f, horizonY);
        DrawQuadraticArc(p0, p1, p2);

        float tSun = Mathf.Clamp01(sunController.CurrentHour / 24f);
        Vector2 sunPos = QuadraticPoint(p0, p1, p2, tSun);
        DrawSolidRect(new Rect(sunPos.x - 10f, sunPos.y - 10f, 20f, 20f), new Color(0.96f, 0.82f, 0.12f, 1f));
        DrawSolidRect(new Rect(sunPos.x - 2f, sunPos.y + 10f, 4f, 14f), new Color(0.96f, 0.82f, 0.12f, 0.35f));

        GUIStyle sunTime = new GUIStyle(GUI.skin.label)
        {
            fontSize = 42,
            alignment = TextAnchor.MiddleCenter,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f) }
        };
        GUI.Label(new Rect(sunPos.x - 85f, sunPos.y + 24f, 170f, 44f), sunController.GetTimeString(), sunTime);

        GUIStyle riseSet = new GUIStyle(GUI.skin.label)
        {
            fontSize = 40,
            alignment = TextAnchor.MiddleLeft,
            normal = { textColor = new Color(0.24f, 0.47f, 0.69f, 1f) }
        };
        // Decorative sunrise/sunset endpoints for the arc graphic. These are static reference
        // values for Manhattan around the equinox, not per-date values from the solar dataset.
        GUI.Label(new Rect(graphRect.x + 8f, graphRect.yMax - 30f, 120f, 34f), "05:48", riseSet);
        riseSet.alignment = TextAnchor.MiddleRight;
        GUI.Label(new Rect(graphRect.xMax - 128f, graphRect.yMax - 30f, 120f, 34f), "18:12", riseSet);
    }

    private void DrawControlsPanel()
    {
        float panelX = 60f;
        float panelWidth = 1100f;
        float panelHeight = 1260f; // Slightly larger panel
        float panelY = Screen.height - panelHeight - 40f;
        Rect controlsRect = new Rect(panelX, panelY, panelWidth, panelHeight);
        float buttonX = controlsRect.x + 36f;
        float buttonWidth = controlsRect.width - 72f;
        float buttonHeight = 76f; // Smaller button height
        DrawPanel(controlsRect, new Color(1f, 1f, 1f, 0.75f));

        GUIStyle titleStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 40, // Increased size
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f) } // Yellow
        };
        GUI.Label(new Rect(controlsRect.x + 36f, controlsRect.y + 16f, controlsRect.width - 72f, 34f), "C O N T R O L S", titleStyle);

        float startY = controlsRect.y + 70f;
        float gap = 12f; // Smaller gap

        bool showingPaths = pathFinder != null && pathFinder.ArePathsVisible();
        string pathBtnLabel = showingPaths ? "HIDE OPTIMAL PATHS" : "SHOW OPTIMAL PATHS";
        if (DrawStyledButton(new Rect(buttonX, startY, buttonWidth, buttonHeight), pathBtnLabel, new Color(0.39f, 0.70f, 0.93f, 0.26f), new Color(0.89f, 0.92f, 0.96f, 1f), true, 30))
        {
            sunController.isPlay = false;
            if (pathFinder != null)
            {
                pathFinder.RunOptimization();
            }
        }

        bool isSunPlaying = sunController.isPlay;
        string sunBtnLabel = isSunPlaying ? "PAUSE SUN" : "RESUME SUN";
        Color sunBtnFill = isSunPlaying ? new Color(1f, 0.24f, 0.24f, 0.24f) : new Color(0.00f, 0.90f, 0.48f, 0.24f);
        Color sunBtnColor = isSunPlaying ? new Color(1f, 0.24f, 0.24f, 1f) : new Color(0.00f, 0.90f, 0.48f, 1f);

        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap), buttonWidth, buttonHeight), sunBtnLabel, sunBtnFill, sunBtnColor, false, 30))
        {
            sunController.isPlay = !isSunPlaying;
        }

        bool overhead = freeCameraController != null && freeCameraController.isOverheadView;
        string viewLabel = overhead ? "FREE VIEW" : "TOP VIEW";
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 2f, buttonWidth, buttonHeight), viewLabel, new Color(0.24f, 0.47f, 0.69f, 0.24f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            if (freeCameraController != null)
            {
                freeCameraController.SetOverheadView(!overhead);
            }
        }

        bool showPoints = pathFinder != null && pathFinder.AreGeneratedPointsVisible();
        string pointsLabel = showPoints ? "HIDE POINTS" : "SHOW POINTS";
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 3f, buttonWidth, buttonHeight), pointsLabel, new Color(0.93f, 0.70f, 0.39f, 0.24f), new Color(0.96f, 0.92f, 0.89f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.ToggleGeneratedPoints();
            }
        }



        bool showBBox = pathFinder != null && pathFinder.IsBBoxVisible();
        string bboxLabel = showBBox ? "HIDE BBOX" : "SHOW BBOX";
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 4f, buttonWidth, buttonHeight), bboxLabel, new Color(0.39f, 0.93f, 0.45f, 0.24f), new Color(0.92f, 0.96f, 0.89f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.ToggleBBox();
            }
        }


        
        bool showTrees = pathFinder != null && pathFinder.AreTreesVisible();
        string treesLabel = showTrees ? "HIDE TREES" : "SHOW TREES";
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 5f, buttonWidth, buttonHeight), treesLabel, new Color(0.1f, 0.4f, 0.1f, 0.24f), new Color(0.89f, 0.96f, 0.92f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.ToggleTrees();
            }
        }

        string sampleMode = pathFinder != null ? pathFinder.GetSampleVisualizationMode() : "OFF";
        string samplesLabel = $"SAMPLES: {sampleMode}";
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 6f, buttonWidth, buttonHeight), samplesLabel, new Color(0.8f, 0.4f, 0.1f, 0.24f), new Color(0.96f, 0.92f, 0.89f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.ToggleSamplePoints();
            }
        }

        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 7f, buttonWidth, buttonHeight), "EXPORT SAMPLES DB", new Color(0.8f, 0.2f, 0.5f, 0.24f), new Color(0.9f, 0.9f, 0.9f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.StartSampleExport();
            }
        }

        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 8f, buttonWidth, buttonHeight), "EXPORT EXPOSURE DB", new Color(0.9f, 0.6f, 0.2f, 0.24f), new Color(0.96f, 0.92f, 0.89f, 1f), false, 30))
        {
            if (pathFinder != null)
            {
                pathFinder.StartExposureExport();
            }
        }

        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 9f, buttonWidth, buttonHeight), "HIDE UI", new Color(0.52f, 0.62f, 0.82f, 0.24f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            showStatusPanel = false;
            ToggleAllAssociatedUI(false);
        }

        // --- EXPORT SETTINGS ---
        float labelWidth = 320f;
        float inputWidth = buttonWidth - labelWidth - gap;
        GUIStyle labelStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 34,
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f) } // Yellow
        };
        GUI.Label(new Rect(buttonX, startY + (buttonHeight + gap) * 10f, labelWidth, buttonHeight), "EXPORT DATES :", labelStyle);

        Rect inputRect = new Rect(buttonX + labelWidth + gap, startY + (buttonHeight + gap) * 10f, inputWidth, buttonHeight);
        DrawRoundedRect(inputRect, new Color(1f, 1f, 1f, 0.1f));
        GUIStyle tfStyle = new GUIStyle(GUI.skin.textField)
        {
            fontSize = 44, // Larger for readability
            alignment = TextAnchor.MiddleLeft,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f), background = GetSolidTexture() }, // Yellow text
            focused = { textColor = new Color(0.96f, 0.82f, 0.12f, 1f), background = GetSolidTexture() },
            border = new RectOffset(0, 0, 0, 0),
            padding = new RectOffset(18, 18, 0, 0)
        };
        
        Color oldColor = GUI.color;
        GUI.color = new Color(1f, 1f, 1f, 0.05f); // Very subtle background for input
        string newDates = GUI.TextField(inputRect, datesInput, tfStyle);
        GUI.color = oldColor;
        if (newDates != datesInput)
        {
            datesInput = newDates;
            SyncDatesToPathFinder();
        }

        // --- NEW TIME CONTROLS ---
        float timeBtnWidth = (buttonWidth - gap) * 0.5f;

        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 11f, timeBtnWidth, buttonHeight), "-1 HOUR", new Color(0.12f, 0.23f, 0.38f, 0.4f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            sunController.AddHours(-1f);
        }

        if (DrawStyledButton(new Rect(buttonX + timeBtnWidth + gap, startY + (buttonHeight + gap) * 11f, timeBtnWidth, buttonHeight), "+1 HOUR", new Color(0.12f, 0.23f, 0.38f, 0.4f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            sunController.AddHours(1f);
        }

        // --- MINUTE CONTROLS ---
        if (DrawStyledButton(new Rect(buttonX, startY + (buttonHeight + gap) * 12f, timeBtnWidth, buttonHeight), "-1 MIN", new Color(0.12f, 0.23f, 0.38f, 0.4f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            sunController.AddHours(-1f / 60f);
        }

        if (DrawStyledButton(new Rect(buttonX + timeBtnWidth + gap, startY + (buttonHeight + gap) * 12f, timeBtnWidth, buttonHeight), "+1 MIN", new Color(0.12f, 0.23f, 0.38f, 0.4f), new Color(0.89f, 0.92f, 0.96f, 1f), false, 30))
        {
            sunController.AddHours(1f / 60f);
        }
    }

    private void SyncDatesToPathFinder()
    {
        if (pathFinder == null) return;
        pathFinder.exportTargetDates = datesInput;
    }

    private void ToggleAllAssociatedUI(bool visible)
    {
        var finders = FindObjectsByType<ShadowAwarePathFinder>(FindObjectsSortMode.None);
        foreach (var f in finders) if (f != null) f.SetTableVisible(visible);

        var helpers = FindObjectsByType<PathVisualizationHelper>(FindObjectsSortMode.None);
        foreach (var h in helpers) if (h != null) h.SetVisible(visible);
    }

    private static bool DrawStyledButton(Rect rect, string text, Color fillColor, Color textColor, bool primary, int fontSize)
    {
        DrawRoundedRect(rect, fillColor);

        bool clicked = GUI.Button(rect, GUIContent.none, GUIStyle.none);

        GUIStyle style = new GUIStyle(GUI.skin.label)
        {
            fontSize = fontSize,
            fontStyle = FontStyle.Bold,
            alignment = TextAnchor.MiddleCenter,
            normal = { textColor = textColor },
            border = new RectOffset(0, 0, 0, 0),
            margin = new RectOffset(0, 0, 0, 0),
            padding = new RectOffset(0, 0, 0, 0)
        };

        GUI.Label(rect, text, style);
        return clicked;
    }

    // Coarse, purely cosmetic label for the status panel. The buckets are hardcoded for
    // Manhattan's approximate solar day and are NOT derived from the solar dataset —
    // the authoritative sun position always comes from SolarDataLoader.
    private static string GetTimePeriodText(float hour)
    {
        if (hour < 5f) return "NIGHT - PRE-DAWN";
        if (hour < 6f) return "DAWN - PRE-SUNRISE";
        if (hour < 8f) return "EARLY MORNING - SUNRISE";
        if (hour < 12f) return "MORNING - DAYLIGHT";
        if (hour < 13f) return "NOON - HIGH SUN";
        if (hour < 17f) return "AFTERNOON - DAYLIGHT";
        if (hour < 18f) return "LATE AFTERNOON - SUNSET SOON";
        if (hour < 19f) return "DUSK - POST-SUNSET";
        if (hour < 21f) return "EVENING - NIGHTFALL";
        return "NIGHT - LATE";
    }

    private IEnumerator RefreshDateRoutine()
    {
        while (true)
        {
            using (UnityWebRequest req = UnityWebRequest.Get(dateApiUrl))
            {
                req.timeout = 8;
                yield return req.SendWebRequest();

                if (req.result == UnityWebRequest.Result.Success && !string.IsNullOrEmpty(req.downloadHandler.text))
                {
                    string parsed = TryParseDateFromJson(req.downloadHandler.text);
                    if (!string.IsNullOrEmpty(parsed))
                    {
                        displayDate = parsed;
                    }
                }
            }

            if (dateRefreshSeconds <= 0f)
            {
                yield break;
            }

            yield return new WaitForSecondsRealtime(dateRefreshSeconds);
        }
    }

    private static string TryParseDateFromJson(string json)
    {
        string dateValue = TryExtractJsonString(json, "date");
        if (!string.IsNullOrEmpty(dateValue) && DateTime.TryParse(dateValue, out DateTime dateOnly))
        {
            return dateOnly.ToString("yyyy/MM/dd");
        }

        string dateTimeValue = TryExtractJsonString(json, "datetime");
        if (!string.IsNullOrEmpty(dateTimeValue) && DateTime.TryParse(dateTimeValue, out DateTime dt))
        {
            return dt.ToString("yyyy/MM/dd");
        }

        return "";
    }

    private static string TryExtractJsonString(string json, string key)
    {
        string token = $"\"{key}\"";
        int keyPos = json.IndexOf(token, StringComparison.OrdinalIgnoreCase);
        if (keyPos < 0) return "";

        int colonPos = json.IndexOf(':', keyPos);
        if (colonPos < 0) return "";

        int firstQuote = json.IndexOf('"', colonPos + 1);
        if (firstQuote < 0) return "";

        int secondQuote = json.IndexOf('"', firstQuote + 1);
        if (secondQuote < 0) return "";

        return json.Substring(firstQuote + 1, secondQuote - firstQuote - 1).Trim();
    }
    private static void DrawPanel(Rect rect, Color tint)
    {
        Color old = GUI.color;
        GUI.color = new Color(0f, 0f, 0f, 0.28f);
        GUI.DrawTexture(new Rect(rect.x + 3f, rect.y + 4f, rect.width, rect.height), GetPanelTexture(), ScaleMode.StretchToFill, true);
        GUI.color = old;

        old = GUI.color;
        GUI.color = tint;
        GUI.DrawTexture(rect, GetPanelTexture(), ScaleMode.StretchToFill, true);
        GUI.color = old;

        DrawSolidRect(new Rect(rect.x, rect.y, rect.width, 1f), new Color(0.39f, 0.70f, 0.93f, 0.18f));
        DrawSolidRect(new Rect(rect.x, rect.yMax - 1f, rect.width, 1f), new Color(0.39f, 0.70f, 0.93f, 0.18f));
        DrawSolidRect(new Rect(rect.x, rect.y, 1f, rect.height), new Color(0.39f, 0.70f, 0.93f, 0.18f));
        DrawSolidRect(new Rect(rect.xMax - 1f, rect.y, 1f, rect.height), new Color(0.39f, 0.70f, 0.93f, 0.18f));
    }

    private static Texture2D GetPanelTexture()
    {
        if (panelTexture != null) return panelTexture;

        const int width = 256;
        const int height = 128;
        panelTexture = new Texture2D(width, height, TextureFormat.RGBA32, false);
        panelTexture.wrapMode = TextureWrapMode.Clamp;
        panelTexture.filterMode = FilterMode.Bilinear;

        Color baseColor = new Color(0.03f, 0.07f, 0.14f, 1f);
        Color leftBlue = new Color(0.08f, 0.22f, 0.46f, 1f);
        Color bottomCyan = new Color(0.03f, 0.10f, 0.22f, 1f);
        Color rightPurple = new Color(0.10f, 0.09f, 0.26f, 1f);

        for (int y = 0; y < height; y++)
        {
            float v = y / (height - 1f);
            for (int x = 0; x < width; x++)
            {
                float u = x / (width - 1f);
                Color c = baseColor;

                float leftWeight = Mathf.Exp(-((u - 0.18f) * (u - 0.18f) / 0.02f + (v - 0.55f) * (v - 0.55f) / 0.05f));
                float cyanWeight = Mathf.Exp(-((u - 0.5f) * (u - 0.5f) / 0.08f + (v - 0.10f) * (v - 0.10f) / 0.02f));
                float purpleWeight = Mathf.Exp(-((u - 0.82f) * (u - 0.82f) / 0.03f + (v - 0.55f) * (v - 0.55f) / 0.06f));

                c = Color.Lerp(c, leftBlue, Mathf.Clamp01(leftWeight * 0.50f));
                c = Color.Lerp(c, bottomCyan, Mathf.Clamp01(cyanWeight * 0.25f));
                c = Color.Lerp(c, rightPurple, Mathf.Clamp01(purpleWeight * 0.18f));

                float topGloss = Mathf.Clamp01((v - 0.78f) * 2.3f) * 0.05f;
                c = Color.Lerp(c, Color.white, topGloss);

                float px = x + 0.5f;
                float py = y + 0.5f;
                float radius = 12f;
                float dx = Mathf.Max(0f, Mathf.Max(radius - px, px - (width - radius)));
                float dy = Mathf.Max(0f, Mathf.Max(radius - py, py - (height - radius)));
                float cornerDist = Mathf.Sqrt(dx * dx + dy * dy);
                float alpha = cornerDist <= radius ? 1f : 0f;
                c.a = alpha;

                panelTexture.SetPixel(x, y, c);
            }
        }

        panelTexture.Apply();
        panelTexture.hideFlags = HideFlags.HideAndDontSave;
        return panelTexture;
    }

    private static Texture2D GetSolidTexture()
    {
        if (solidTexture != null) return solidTexture;
        solidTexture = new Texture2D(1, 1, TextureFormat.RGBA32, false);
        solidTexture.SetPixel(0, 0, Color.white);
        solidTexture.Apply();
        solidTexture.hideFlags = HideFlags.HideAndDontSave;
        return solidTexture;
    }

    private static Texture2D GetRoundedRectTexture()
    {
        if (roundedRectTexture != null) return roundedRectTexture;

        const int size = 64;
        const float radius = 12f;
        roundedRectTexture = new Texture2D(size, size, TextureFormat.RGBA32, false);
        roundedRectTexture.wrapMode = TextureWrapMode.Clamp;
        roundedRectTexture.filterMode = FilterMode.Bilinear;

        for (int y = 0; y < size; y++)
        {
            for (int x = 0; x < size; x++)
            {
                float px = x + 0.5f;
                float py = y + 0.5f;
                float dx = Mathf.Max(0f, Mathf.Max(radius - px, px - (size - radius)));
                float dy = Mathf.Max(0f, Mathf.Max(radius - py, py - (size - radius)));
                float cornerDist = Mathf.Sqrt(dx * dx + dy * dy);
                float alpha = Mathf.Clamp01(1f - Mathf.Max(0f, cornerDist - radius + 1.5f) / 1.5f);
                roundedRectTexture.SetPixel(x, y, new Color(1f, 1f, 1f, alpha));
            }
        }

        roundedRectTexture.Apply();
        roundedRectTexture.hideFlags = HideFlags.HideAndDontSave;
        return roundedRectTexture;
    }

    private static void DrawSolidRect(Rect rect, Color color)
    {
        Color old = GUI.color;
        GUI.color = color;
        GUI.DrawTexture(rect, GetSolidTexture());
        GUI.color = old;
    }

    private static void DrawRoundedRect(Rect rect, Color color)
    {
        float radius = Mathf.Clamp(14f, 2f, Mathf.Min(rect.width, rect.height) * 0.5f - 1f);
        if (rect.width <= radius * 2f || rect.height <= radius * 2f)
        {
            DrawSolidRect(rect, color);
            return;
        }

        Color old = GUI.color;
        GUI.color = color;

        // 9-slice style fill: center + edges + corner masks to avoid non-uniform stretch distortion.
        DrawSolidRect(new Rect(rect.x + radius, rect.y + radius, rect.width - 2f * radius, rect.height - 2f * radius), color);
        DrawSolidRect(new Rect(rect.x + radius, rect.y, rect.width - 2f * radius, radius), color);
        DrawSolidRect(new Rect(rect.x + radius, rect.yMax - radius, rect.width - 2f * radius, radius), color);
        DrawSolidRect(new Rect(rect.x, rect.y + radius, radius, rect.height - 2f * radius), color);
        DrawSolidRect(new Rect(rect.xMax - radius, rect.y + radius, radius, rect.height - 2f * radius), color);

        Texture2D rounded = GetRoundedRectTexture();
        GUI.DrawTextureWithTexCoords(new Rect(rect.x, rect.y, radius, radius), rounded, new Rect(0f, 0f, 0.5f, 0.5f), true);
        GUI.DrawTextureWithTexCoords(new Rect(rect.xMax - radius, rect.y, radius, radius), rounded, new Rect(0.5f, 0f, 0.5f, 0.5f), true);
        GUI.DrawTextureWithTexCoords(new Rect(rect.x, rect.yMax - radius, radius, radius), rounded, new Rect(0f, 0.5f, 0.5f, 0.5f), true);
        GUI.DrawTextureWithTexCoords(new Rect(rect.xMax - radius, rect.yMax - radius, radius, radius), rounded, new Rect(0.5f, 0.5f, 0.5f, 0.5f), true);

        GUI.color = old;
    }


    private static void DrawLine(Vector2 start, Vector2 end, Color color, float thickness)
    {
        Vector2 d = end - start;
        float len = d.magnitude;
        if (len <= 0.001f) return;
        float angle = Mathf.Atan2(d.y, d.x) * Mathf.Rad2Deg;
        Matrix4x4 old = GUI.matrix;
        GUIUtility.RotateAroundPivot(angle, start);
        DrawSolidRect(new Rect(start.x, start.y - thickness * 0.5f, len, thickness), color);
        GUI.matrix = old;
    }

    private static Vector2 QuadraticPoint(Vector2 p0, Vector2 p1, Vector2 p2, float t)
    {
        float omt = 1f - t;
        return omt * omt * p0 + 2f * omt * t * p1 + t * t * p2;
    }

    private static void DrawQuadraticArc(Vector2 p0, Vector2 p1, Vector2 p2)
    {
        Vector2 prev = p0;
        const int steps = 72;
        for (int i = 1; i <= steps; i++)
        {
            float t = i / (float)steps;
            Vector2 cur = QuadraticPoint(p0, p1, p2, t);
            Color c = Color.Lerp(new Color(0.10f, 0.62f, 1f, 0.65f), new Color(0.96f, 0.82f, 0.12f, 0.85f), Mathf.Sin(t * Mathf.PI));
            if (t > 0.6f)
            {
                c = Color.Lerp(c, new Color(0.95f, 0.45f, 0.18f, 0.6f), (t - 0.6f) / 0.4f);
            }

            DrawLine(prev, cur, c, 5f);
            prev = cur;
        }
    }
}


