using UnityEngine;
using Npgsql;
using System;
using System.Collections.Generic;

/// <summary>
/// Holds the PostGIS connection settings for the scene and provides a couple of smoke-test
/// queries. Other components (ShadowAwarePathFinder, TreeEngine, SampleVisualization, ...)
/// locate this via FindFirstObjectByType and build their own connection string from it.
///
/// Axis convention used consistently across every query in the project:
///   PostGIS X = Unity X,  PostGIS Y = Unity Z,  PostGIS Z = Unity Y (vertical).
/// Points are stored as PointZ with SRID 0, i.e. raw Unity world units, not a geographic CRS.
///
/// NOTE: credentials here match the local docker-compose defaults and are stored in plain text
/// in the scene. Fine for a local container; do not point this at a shared database.
/// </summary>
public class PostGISClient : MonoBehaviour
{
    [Header("Connection Settings")]
    public string host = "localhost";
    public string port = "5432";
    public string database = "city_data";
    public string username = "admin";
    public string password = "password";

    private string ConnectionString => $"Host={host};Port={port};Database={database};Username={username};Password={password};";
    public string GetConnectionString() => ConnectionString;

    [ContextMenu("Test Connection")]
    public void TestConnection()
    {
        try
        {
            using (var conn = new NpgsqlConnection(ConnectionString))
            {
                conn.Open();
                Debug.Log("<color=green>PostGIS Connection Successful!</color>");
                
                using (var cmd = new NpgsqlCommand("SELECT postgis_full_version()", conn))
                {
                    string version = (string)cmd.ExecuteScalar();
                    Debug.Log("PostGIS Version: " + version);
                }
            }
        }
        catch (Exception e)
        {
            Debug.LogError("PostGIS Connection Failed: " + e.Message);
        }
    }

    /// <summary>
    /// Editor smoke test: finds the graph waypoint closest to this GameObject and draws a line
    /// to it. Uses the KNN operator (&lt;-&gt;) so the GiST index on geom does the work.
    /// </summary>
    [ContextMenu("Find Nearest Node")]
    public void FindNearestToPlayer()
    {
        Vector3 pos = transform.position;

        // Columns are derived from geom rather than stored separately, and the table is
        // meo_waypoints — the schema in db_pipeline_initializer.py has no `nodes` table.
        string query = @"
            SELECT id, ST_X(geom), ST_Y(geom), ST_Z(geom),
                   ST_Distance(geom, ST_MakePoint(@x, @z, @y)) AS dist
            FROM meo_waypoints
            ORDER BY geom <-> ST_MakePoint(@x, @z, @y)
            LIMIT 1;";

        try
        {
            using (var conn = new NpgsqlConnection(ConnectionString))
            {
                conn.Open();
                using (var cmd = new NpgsqlCommand(query, conn))
                {
                    cmd.Parameters.AddWithValue("x", (double)pos.x);
                    cmd.Parameters.AddWithValue("y", (double)pos.y);
                    cmd.Parameters.AddWithValue("z", (double)pos.z);

                    using (var reader = cmd.ExecuteReader())
                    {
                        if (reader.Read())
                        {
                            Guid id = reader.GetGuid(0);
                            // PostGIS (X, Y, Z) -> Unity (x, z, y)
                            float nx = (float)reader.GetDouble(1);
                            float nz = (float)reader.GetDouble(2);
                            float ny = (float)reader.GetDouble(3);
                            double dist = reader.GetDouble(4);

                            Debug.Log($"<color=cyan>Nearest Waypoint:</color> {id} at ({nx}, {ny}, {nz}). Distance: {dist:F2}");

                            // Visualize in Editor
                            Debug.DrawLine(pos, new Vector3(nx, ny, nz), Color.yellow, 5f);
                        }
                        else
                        {
                            Debug.LogWarning("No waypoints in meo_waypoints — has db_pipeline_initializer.py run?");
                        }
                    }
                }
            }
        }
        catch (Exception e)
        {
            Debug.LogError("Query Failed: " + e.Message);
        }
    }

    /// <summary>
    /// Returns every graph waypoint within <paramref name="radius"/> of this GameObject.
    /// Note ST_DWithin on plain GEOMETRY is a 2D test, so the vertical axis is ignored —
    /// which is what we want, since all graph nodes share one normalized elevation.
    /// </summary>
    public List<Vector3> GetNodesInRange(float radius)
    {
        List<Vector3> results = new List<Vector3>();
        Vector3 pos = transform.position;

        string query = @"
            SELECT ST_X(geom), ST_Y(geom), ST_Z(geom) FROM meo_waypoints
            WHERE ST_DWithin(geom, ST_MakePoint(@x, @z, @y), @radius)";

        try
        {
            using (var conn = new NpgsqlConnection(ConnectionString))
            {
                conn.Open();
                using (var cmd = new NpgsqlCommand(query, conn))
                {
                    cmd.Parameters.AddWithValue("x", (double)pos.x);
                    cmd.Parameters.AddWithValue("y", (double)pos.y);
                    cmd.Parameters.AddWithValue("z", (double)pos.z);
                    cmd.Parameters.AddWithValue("radius", (double)radius);

                    using (var reader = cmd.ExecuteReader())
                    {
                        while (reader.Read())
                        {
                            // PostGIS (X, Y, Z) -> Unity (x, z, y)
                            results.Add(new Vector3(
                                (float)reader.GetDouble(0),
                                (float)reader.GetDouble(2),
                                (float)reader.GetDouble(1)
                            ));
                        }
                    }
                }
            }
        }
        catch (Exception e)
        {
            Debug.LogError("Range Query Failed: " + e.Message);
        }
        return results;
    }
}
