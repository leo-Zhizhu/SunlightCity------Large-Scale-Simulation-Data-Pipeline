#if UNITY_EDITOR
using UnityEngine;
using UnityEditor;
using System;
using System.IO;
using System.Globalization;

// Menu: Tools → Solar Data → Preprocess CSV to Binary
// Reads:  Assets/Resources/SolarData/[CITY]/sun_pos_YYYY.csv
// Writes: Assets/StreamingAssets/SolarData/[CITY]/sun_pos_YYYY.bin
//
// Binary format (little-endian):
//   Header  16 bytes: magic(4) + version(2) + year(2) + totalMinutes(4) + reserved(4)
//   Data  N×8 bytes: azimuth(float32) + elevation(float32)  — 1 entry per minute
//   Index: (dayOfYear-1) × 1440 + minuteOfDay,  dayOfYear is 1-based
//
// CSV columns: [datetime_index], azimuth, apparent_elevation

public class SolarDataPreprocessor
{
    const int VERSION = 1;
    const int MINUTES_PER_DAY = 1440;

    [MenuItem("Tools/Solar Data/Preprocess CSV to Binary")]
    public static void PreprocessAll()
    {
        string resourcesPath = Path.Combine(Application.dataPath, "Resources", "SolarData");
        string streamingPath = Path.Combine(Application.streamingAssetsPath, "SolarData");

        if (!Directory.Exists(resourcesPath))
        {
            Debug.LogError($"Solar data source folder not found: {resourcesPath}");
            return;
        }

        int converted = 0;
        int skipped = 0;

        foreach (string cityDir in Directory.GetDirectories(resourcesPath))
        {
            string cityName = Path.GetFileName(cityDir);
            string outDir = Path.Combine(streamingPath, cityName);
            Directory.CreateDirectory(outDir);

            foreach (string csvPath in Directory.GetFiles(cityDir, "sun_pos_*.csv"))
            {
                string stem = Path.GetFileNameWithoutExtension(csvPath); // "sun_pos_2026"
                string yearStr = stem.Replace("sun_pos_", "");
                if (!int.TryParse(yearStr, out int year))
                {
                    Debug.LogWarning($"Skipping unexpected filename: {csvPath}");
                    skipped++;
                    continue;
                }

                string outPath = Path.Combine(outDir, $"sun_pos_{year}.bin");
                try
                {
                    int written = ConvertCsvToBinary(csvPath, outPath, year, cityName);
                    Debug.Log($"[{cityName} {year}] {written:N0} entries → {outPath}");
                    converted++;
                }
                catch (Exception e)
                {
                    Debug.LogError($"Failed to convert {csvPath}: {e.Message}");
                    skipped++;
                }
            }
        }

        AssetDatabase.Refresh();
        EditorUtility.DisplayDialog(
            "Solar Data Preprocessor",
            $"Done.\n\nConverted: {converted} file(s)\nSkipped/Errors: {skipped}",
            "OK");
    }

    // Returns number of entries written.
    static int ConvertCsvToBinary(string csvPath, string outPath, int year, string cityName)
    {
        bool isLeap = DateTime.IsLeapYear(year);
        int expectedMinutes = (isLeap ? 366 : 365) * MINUTES_PER_DAY;

        using var reader = new StreamReader(csvPath);
        using var writer = new BinaryWriter(File.Open(outPath, FileMode.Create));

        // Provisional count; patched with the real row count after the data pass below.
        WriteHeader(writer, year, expectedMinutes);

        // Skip CSV header row
        string headerLine = reader.ReadLine();
        ValidateCsvHeader(headerLine, csvPath);

        int count = 0;
        float lastAzimuth = 0f;
        float lastElevation = -90f;
        string line;

        while ((line = reader.ReadLine()) != null)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;

            if (!TryParseCsvRow(line, out float azimuth, out float elevation))
            {
                Debug.LogWarning($"[{cityName} {year}] Parse error on row {count + 1}, padding with previous value.");
                azimuth = lastAzimuth;
                elevation = lastElevation;
            }

            writer.Write(azimuth);
            writer.Write(elevation);
            lastAzimuth = azimuth;
            lastElevation = elevation;
            count++;
        }

        if (count != expectedMinutes)
            Debug.LogWarning($"[{cityName} {year}] Expected {expectedMinutes} rows but got {count}. File may be incomplete.");

        // Rewrite totalMinutes with what we actually wrote. SolarDataLoader trusts this field to
        // size its array and reads exactly that many floats, so a short CSV combined with the
        // optimistic expectedMinutes would fail with EndOfStreamException at load time.
        writer.Flush();
        writer.BaseStream.Seek(8, SeekOrigin.Begin); // magic(4) + version(2) + year(2)
        writer.Write(count);

        return count;
    }

    static void WriteHeader(BinaryWriter writer, int year, int totalMinutes)
    {
        // Magic: "SLRD"
        writer.Write((byte)'S');
        writer.Write((byte)'L');
        writer.Write((byte)'R');
        writer.Write((byte)'D');
        writer.Write((short)VERSION);
        writer.Write((short)year);
        writer.Write(totalMinutes);
        writer.Write(0); // reserved
    }

    // CSV row: "2026-01-01 00:00:00-05:00,0.534,-72.217"
    // column 0 = datetime (index, ignored), col 1 = azimuth, col 2 = apparent_elevation
    static bool TryParseCsvRow(string line, out float azimuth, out float elevation)
    {
        azimuth = 0f;
        elevation = -90f;

        // The datetime may contain commas if quoted, but in practice it doesn't.
        // Splitting from the right is safer: last comma separates elevation.
        int lastComma = line.LastIndexOf(',');
        if (lastComma < 0) return false;

        int firstComma = line.IndexOf(',');
        if (firstComma == lastComma) return false; // only one comma = missing a column

        string azimuthStr = line.Substring(firstComma + 1, lastComma - firstComma - 1).Trim();
        string elevStr    = line.Substring(lastComma + 1).Trim();

        return float.TryParse(azimuthStr, NumberStyles.Float, CultureInfo.InvariantCulture, out azimuth)
            && float.TryParse(elevStr,    NumberStyles.Float, CultureInfo.InvariantCulture, out elevation);
    }

    static void ValidateCsvHeader(string headerLine, string csvPath)
    {
        if (headerLine == null || !headerLine.Contains("azimuth"))
            Debug.LogWarning($"Unexpected CSV header in {csvPath}: '{headerLine}'");
    }
}
#endif
