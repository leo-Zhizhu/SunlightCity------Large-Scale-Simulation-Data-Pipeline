using System;
using System.Diagnostics;
using Npgsql;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// One section's sample points, in ray-issue order.
    ///
    /// Struct-of-arrays rather than an array of objects. The sampler walks
    /// Positions sequentially 60 times per task; as an array of class instances
    /// that would be 4,400 pointer dereferences per timestep into scattered heap
    /// locations, and the ids — which the raycast loop never reads — would share
    /// cache lines with the positions it does. Split, a cache line of Positions
    /// holds five consecutive rays' worth of useful data and nothing else.
    /// </summary>
    public sealed class SectionGeometry
    {
        public int SectionId { get; internal set; }
        public int Count     { get; internal set; }

        /// <summary>Sample positions, indices [0, Count). Capacity-sized, reused.</summary>
        public readonly Vector3[] Positions;

        /// <summary>Sample ids, parallel to Positions.</summary>
        public readonly Guid[] Ids;

        /// <summary>Distinct edges in this section. Logged; also a sanity signal.</summary>
        public int EdgeCount { get; internal set; }

        internal SectionGeometry(int capacity)
        {
            Positions = new Vector3[capacity];
            Ids       = new Guid[capacity];
            SectionId = -1;
        }
    }

    /// <summary>
    /// Loads and caches the current section's sample geometry.
    ///
    /// WHY A CACHE AT ALL, AND WHY ONE ENTRY
    /// -------------------------------------
    /// A section's geometry is identical for all twelve of its dates and all six of
    /// its windows — 72 tasks share one load. The coordinator's claim function
    /// dispatches with AFFINITY on (section, window) precisely so a worker keeps
    /// receiving tasks it already has the geometry for, which turns 30,240 loads
    /// into 504 across the fleet.
    ///
    /// One entry, not an LRU, because affinity makes the access pattern strictly
    /// sequential: a worker drains a (section, window) group before moving on. A
    /// multi-entry cache would hold memory for sections it will not see again and
    /// add a lookup to a path that has exactly one candidate. When affinity does
    /// miss — the group is exhausted, or the shard is at its admission cap — the
    /// single entry is replaced, which is the correct behaviour anyway.
    ///
    /// WHERE IT READS FROM
    /// -------------------
    /// The SHARD, not the coordinator. Every shard holds the full static geometry
    /// (~140 MB replicated, cheaper than any scheme for fetching it on demand), so
    /// the per-task path never touches the coordinator except to claim, heartbeat
    /// and complete. That is what keeps one small instance able to serve 50
    /// workers.
    /// </summary>
    public sealed class SectionGeometryCache
    {
        private readonly WorkerConfig _cfg;
        private readonly SectionGeometry _slot;

        public int  Loads  { get; private set; }
        public int  Hits   { get; private set; }
        public long LoadMillis { get; private set; }

        public SectionGeometryCache(WorkerConfig cfg)
        {
            _cfg  = cfg ?? throw new ArgumentNullException(nameof(cfg));
            // Allocated once, at capacity, for the life of the pod — same discipline
            // as the sampler's native buffers.
            _slot = new SectionGeometry(cfg.MaxSectionSamples);
        }

        public bool IsCached(int sectionId) => _slot.SectionId == sectionId;

        /// <summary>
        /// Returns the geometry for <paramref name="sectionId"/>, loading it if the
        /// cached section differs.
        ///
        /// ORDER BY edge_id, sequence_index is load-bearing, not cosmetic. It makes
        /// consecutive entries in the array consecutive points along the same
        /// street, so consecutive rays in a batch start within 2 m of each other and
        /// traverse the same BVH nodes. Issuing them in the database's physical
        /// order instead would scatter each batch across the whole section and turn
        /// a cache-resident traversal into a stream of misses — the same total work,
        /// several times the memory traffic.
        /// </summary>
        public SectionGeometry Load(NpgsqlConnection shard, int sectionId)
        {
            if (_slot.SectionId == sectionId)
            {
                Hits++;
                return _slot;
            }

            const string sql = @"
                SELECT sp.id,
                       ST_X(sp.geom), ST_Y(sp.geom), ST_Z(sp.geom),
                       sp.edge_id
                FROM meo_sample_points sp
                JOIN meo_edge_sections es ON es.edge_id = sp.edge_id
                WHERE es.section_id = @section
                ORDER BY sp.edge_id, sp.sequence_index;";

            var clock = Stopwatch.StartNew();
            int n = 0;
            int edges = 0;
            Guid lastEdge = Guid.Empty;
            float elevation = _cfg.GlobalElevation;

            using (var cmd = new NpgsqlCommand(sql, shard))
            {
                cmd.Parameters.AddWithValue("section", sectionId);
                cmd.CommandTimeout = 0;

                using var r = cmd.ExecuteReader();
                while (r.Read())
                {
                    if (n >= _slot.Positions.Length)
                        throw new InvalidOperationException(
                            $"section {sectionId} has more than SUNLIT_MAX_SECTION_SAMPLES " +
                            $"({_slot.Positions.Length:N0}) sample points. Raise it or use " +
                            "smaller sections — the sampler's buffers are sized from it at boot.");

                    // PostGIS (X, Y, Z) -> Unity (x, z, y). Y and Z swap: PostGIS Y
                    // carries the horizontal Unity Z, PostGIS Z carries the vertical.
                    float x = (float)r.GetDouble(1);
                    float z = (float)r.GetDouble(2);

                    // Elevation comes from config, not from the row. The graph is
                    // planar by construction and the DB value is authoritative for
                    // X/Z only; taking Y from the row would let a single mis-set
                    // waypoint move rays off the road surface. The run-compatibility
                    // check pins the constant, so this cannot drift between workers.
                    _slot.Positions[n] = new Vector3(x, elevation, z);
                    _slot.Ids[n] = r.GetGuid(0);

                    Guid edge = r.GetGuid(4);
                    if (edge != lastEdge) { edges++; lastEdge = edge; }

                    n++;
                }
            }

            _slot.SectionId = sectionId;
            _slot.Count     = n;
            _slot.EdgeCount = edges;

            Loads++;
            LoadMillis += clock.ElapsedMilliseconds;

            Debug.Log($"[Geometry] loaded section {sectionId}: {n:N0} samples across " +
                      $"{edges:N0} edges in {clock.ElapsedMilliseconds} ms " +
                      $"(cache: {Hits} hit / {Loads} load)");

            if (n == 0)
                Debug.LogWarning(
                    $"[Geometry] section {sectionId} has NO sample points. Either " +
                    "meo_edge_sections is stale on this shard, or this section was " +
                    "assigned to a different shard than the one this worker connected to.");

            return _slot;
        }

        /// <summary>
        /// Affinity hit rate. Designed to sit near
        /// (tasks - section_windows) / tasks = 92%; a collapse means dispatch is
        /// thrashing the geometry and the map phase will run long. monitor.py shows
        /// the same figure from the coordinator's side, so the two can be compared.
        /// </summary>
        public double HitRate => (Hits + Loads) == 0 ? 0.0 : (double)Hits / (Hits + Loads);

        public void Invalidate() => _slot.SectionId = -1;
    }
}
