using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using Npgsql;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// A sample point belonging to this shard, resolved once per task.
    /// Struct + parallel arrays rather than a class list: the raycast loop touches
    /// these ~365k/50 entries 360 times per task, so layout matters.
    /// </summary>
    public struct ShardSample
    {
        public Guid    Id;
        public int     EdgeSlot;   // dense index into the edge arrays, not a Guid
        public Vector3 Position;
    }

    /// <summary>
    /// The map-side COMBINER — the single most consequential change in the
    /// distributed rewrite.
    ///
    /// THE INSIGHT
    /// -----------
    /// The single-node pipeline streamed 1.58 billion raw booleans into Postgres
    /// and then aggregated them server-side into ~2 GB of per-edge sums. Ninety-
    /// eight percent of what crossed the wire was discarded by the very next step.
    ///
    /// At 50x parallelism that is not merely wasteful, it is the binding
    /// constraint: 50 workers cannot push 110 GB through one database faster than
    /// one worker can, because the bottleneck was never the raycasting.
    ///
    /// So we aggregate BEFORE the wire. Each worker accumulates
    /// sunlit_count[edge][timestep] in RAM and ships only the aggregate. This is
    /// exactly Hadoop's combiner: reduce locally, then reduce globally.
    ///
    /// WHY THIS IS CORRECT — AND WHY SHARDING BY EDGE IS LOAD-BEARING
    /// --------------------------------------------------------------
    /// A combiner is only valid if the local reduction is complete. Because tasks
    /// are sharded by EDGE (not by sample point, and not by bounding box), every
    /// sample of a given edge lives in exactly one shard. The worker's
    /// per-(edge, timestep) sum is therefore FINAL, not partial — the global
    /// reduce is a concatenation, not a summation.
    ///
    /// Had we sharded by sample point, each worker would hold a fragment of many
    /// edges and the reduce step would need a real cross-worker SUM, reintroducing
    /// a synchronisation barrier and a shuffle. Sharding by bounding box is worse
    /// still: a building outside the box casts shadows into it, so a spatially
    /// sharded worker must load the whole city mesh anyway and gains nothing while
    /// making correctness at the seams delicate.
    ///
    /// MEMORY
    /// ------
    /// counts is edges_in_shard x timesteps of int. At Manhattan scale:
    /// (6,700 / 50) x 360 x 4 B ~= 193 KB. Utterly negligible, which is what makes
    /// the whole approach viable — the combiner costs nothing and removes 98% of
    /// the I/O.
    /// </summary>
    public sealed class ShardExposureCombiner
    {
        private readonly WorkerConfig _cfg;

        // ---- Edge dimension (dense slots) -----------------------------------
        private Guid[] _edgeIds;        // slot -> edge_id
        private int[]  _edgeSampleCount; // slot -> number of samples on that edge
        private int    _edgeCount;

        // ---- Sample dimension ----------------------------------------------
        private ShardSample[] _samples;
        private int _sampleCount;

        // ---- Accumulator: [edgeSlot * stepCount + stepIndex] ----------------
        // Flat, not jagged: one allocation, contiguous, and the inner loop's
        // access pattern is a simple stride.
        private int[] _counts;
        private int   _stepCount;

        // ---- Raw passthrough (only when EmitRaw) ----------------------------
        private List<string> _rawBuffer;

        public int    SampleCount  => _sampleCount;
        public int    EdgeCount    => _edgeCount;
        public long   RaycastsDone { get; private set; }

        public ShardExposureCombiner(WorkerConfig cfg)
        {
            _cfg = cfg ?? throw new ArgumentNullException(nameof(cfg));
        }

        // =====================================================================
        // LOAD
        // =====================================================================

        /// <summary>
        /// Loads this shard's sample points, joined to their parent edges.
        ///
        /// The shard predicate is a join against meo_edge_shards rather than an
        /// inline hash so the mapping is materialised once (see
        /// meo_rebuild_edge_shards) instead of recomputed per task, and so it is
        /// identical to what the planner and orchestrator see.
        ///
        /// Ordered by edge_id then sequence_index: this makes the returned rows
        /// contiguous per edge, so the dense-slot assignment below is a single
        /// pass with no dictionary lookups in the hot path.
        /// </summary>
        public void LoadShard(NpgsqlConnection conn, ExposureTask task)
        {
            const string sql = @"
                SELECT sp.id,
                       sp.edge_id,
                       ST_X(sp.geom), ST_Y(sp.geom), ST_Z(sp.geom)
                FROM meo_sample_points sp
                JOIN meo_edge_shards es
                     ON es.edge_id = sp.edge_id
                    AND es.shard_count = @shard_count
                    AND es.shard_index = @shard_index
                ORDER BY sp.edge_id, sp.sequence_index;";

            var ids     = new List<Guid>();
            var edges   = new List<Guid>();
            var samples = new List<ShardSample>();

            // Dense slot assignment. Because rows arrive grouped by edge_id we only
            // need to compare against the previous row.
            Guid currentEdge = Guid.Empty;
            int  currentSlot = -1;
            var  perEdgeCount = new List<int>();

            using (var cmd = new NpgsqlCommand(sql, conn))
            {
                cmd.Parameters.AddWithValue("shard_count", task.ShardCount);
                cmd.Parameters.AddWithValue("shard_index", task.ShardIndex);

                using var r = cmd.ExecuteReader();
                while (r.Read())
                {
                    Guid sampleId = r.GetGuid(0);
                    Guid edgeId   = r.GetGuid(1);

                    // PostGIS (X, Y, Z) -> Unity (x, z, y). Y and Z swap.
                    float x = (float)r.GetDouble(2);
                    float z = (float)r.GetDouble(3);
                    float y = (float)r.GetDouble(4);

                    if (currentSlot < 0 || edgeId != currentEdge)
                    {
                        currentEdge = edgeId;
                        currentSlot = edges.Count;
                        edges.Add(edgeId);
                        perEdgeCount.Add(0);
                    }
                    perEdgeCount[currentSlot]++;

                    samples.Add(new ShardSample
                    {
                        Id       = sampleId,
                        EdgeSlot = currentSlot,
                        // Normalise to the shared planar elevation. The DB value is
                        // authoritative for X/Z only; height is a pipeline constant.
                        Position = new Vector3(x, _cfg.GlobalElevation, z),
                    });
                }
            }

            _edgeIds         = edges.ToArray();
            _edgeSampleCount = perEdgeCount.ToArray();
            _edgeCount       = _edgeIds.Length;
            _samples         = samples.ToArray();
            _sampleCount     = _samples.Length;

            _stepCount = task.StepCount;
            _counts    = new int[Math.Max(1, _edgeCount) * _stepCount];

            if (task.EmitRaw)
            {
                // Pre-size to the flush threshold to avoid repeated List growth.
                _rawBuffer = new List<string>(_cfg.CopyBatchRows);
            }

            RaycastsDone = 0;

            long accumBytes = (long)_counts.Length * sizeof(int);
            Debug.Log($"[Combiner] shard {task.ShardIndex}/{task.ShardCount}: " +
                      $"{_sampleCount:N0} samples across {_edgeCount:N0} edges, " +
                      $"{_stepCount} timesteps, accumulator {accumBytes / 1024.0:F1} KB");

            if (_sampleCount == 0)
                Debug.LogWarning(
                    $"[Combiner] shard {task.ShardIndex} has NO sample points. Either " +
                    "meo_edge_shards was built for a different shard_count, or " +
                    "meo_sample_points is empty for these edges.");
        }

        // =====================================================================
        // ACCUMULATE
        // =====================================================================

        /// <summary>
        /// Evaluates every sample in this shard at one timestep and folds the
        /// result into the accumulator.
        ///
        /// <paramref name="stepIndex"/> is the dense index of the timestep within
        /// the task, which is what the accumulator is keyed on. Passing it in (as
        /// opposed to deriving it from a clock) keeps this method a pure function
        /// of its inputs and therefore trivially replayable.
        /// </summary>
        public void AccumulateTimestep(
            int stepIndex,
            DateTime timestamp,
            Light sun,
            bool emitRaw)
        {
            if (_samples == null || _sampleCount == 0) return;

            // Hoist everything invariant out of the inner loop. At 365k iterations
            // per call, a property access or a mask OR inside the loop is real time.
            int  layerMask   = _cfg.ShadowCasterMask | _cfg.GroundBlockerMask;
            float threshold  = _cfg.SunAngleThreshold;
            float elevAngle  = sun.transform.eulerAngles.x;
            Vector3 toSun    = -sun.transform.forward;

            // Horizon guard, evaluated ONCE per timestep instead of per sample.
            //
            // eulerAngles is [0, 360), so a sun 10 deg below the horizon reads as
            // 350: the `>= 180 - threshold` test covers the entire below-horizon
            // arc as well as dusk, while `<= threshold` covers dawn. Near the
            // horizon a ray must cross kilometres of city, where float precision
            // degrades and the ray can escape the mesh entirely and falsely report
            // sunlit — so declaring these steps shadowed is both cheaper and more
            // accurate.
            bool belowHorizon = elevAngle <= threshold || elevAngle >= 180f - threshold;

            int baseOffset = stepIndex;
            string tsText = emitRaw ? timestamp.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) : null;

            if (belowHorizon)
            {
                // Whole shard is dark. Nothing to accumulate (counts stay 0), and we
                // skip 365k/50 raycasts entirely — this is why the 03:00-21:00 window
                // costs far less than its length suggests in winter.
                if (emitRaw)
                {
                    for (int i = 0; i < _sampleCount; i++)
                        _rawBuffer.Add(RawRow(_samples[i].Id, tsText, false));
                }
                return;
            }

            for (int i = 0; i < _sampleCount; i++)
            {
                ref ShardSample s = ref _samples[i];

                // Lift 3 m so the road surface itself never shadows its own sample.
                Vector3 origin = new Vector3(s.Position.x,
                                             s.Position.y + 3.0f,
                                             s.Position.z);

                bool shadowed = Physics.Raycast(origin, toSun, 10000f, layerMask);

                if (!shadowed)
                    _counts[s.EdgeSlot * _stepCount + baseOffset]++;

                if (emitRaw)
                    _rawBuffer.Add(RawRow(s.Id, tsText, !shadowed));
            }

            RaycastsDone += _sampleCount;
        }

        /// <summary>True when the raw buffer should be flushed to keep memory bounded.</summary>
        public bool RawBufferFull => _rawBuffer != null && _rawBuffer.Count >= _cfg.CopyBatchRows;

        private static string RawRow(Guid id, string ts, bool sunlit) =>
            // CSV for COPY ... FROM STDIN CSV. No quoting needed: UUIDs, an
            // ISO timestamp and a bare boolean contain no commas or quotes.
            string.Concat(id.ToString(), ",", ts, ",", sunlit ? "true" : "false");

        // =====================================================================
        // FLUSH
        // =====================================================================

        /// <summary>
        /// Streams the buffered raw rows into the task's staging table and clears
        /// the buffer. Only called when EmitRaw is on.
        /// </summary>
        public long FlushRaw(NpgsqlConnection conn, long taskId)
        {
            if (_rawBuffer == null || _rawBuffer.Count == 0) return 0;

            string table = $"meo_stage_samples_{taskId}";
            long written = _rawBuffer.Count;

            using (var writer = conn.BeginTextImport(
                $"COPY {table} (sample_point_id, datetime, is_sunlit) FROM STDIN CSV"))
            {
                for (int i = 0; i < _rawBuffer.Count; i++)
                    writer.WriteLine(_rawBuffer[i]);
            }

            _rawBuffer.Clear();
            return written;
        }

        /// <summary>
        /// Streams the finished edge aggregate into the task's staging table.
        ///
        /// This is the ENTIRE network cost of the combiner path: edges x timesteps
        /// rows, ~48 K rows for a Manhattan shard-day, versus ~2.6 M raw booleans
        /// for the same work.
        ///
        /// COPY (not INSERT) even at this modest size: it avoids per-row statement
        /// parse/plan and is a single round trip.
        /// </summary>
        public long FlushEdgeAggregate(
            NpgsqlConnection conn,
            long taskId,
            DateTime simDate,
            int startMinute,
            int stepMinute)
        {
            if (_edgeCount == 0) return 0;

            string table = $"meo_stage_edges_{taskId}";
            long written = 0;

            var sb = new StringBuilder(64);

            using (var writer = conn.BeginTextImport(
                $"COPY {table} (edge_id, datetime, sunlit_sum, sample_count) FROM STDIN CSV"))
            {
                for (int slot = 0; slot < _edgeCount; slot++)
                {
                    string edgeText = _edgeIds[slot].ToString();
                    int    nSamples = _edgeSampleCount[slot];
                    int    rowBase  = slot * _stepCount;

                    for (int step = 0; step < _stepCount; step++)
                    {
                        DateTime ts = simDate.AddMinutes(startMinute + step * stepMinute);

                        sb.Clear();
                        sb.Append(edgeText).Append(',')
                          .Append(ts.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)).Append(',')
                          .Append(_counts[rowBase + step].ToString(CultureInfo.InvariantCulture)).Append(',')
                          .Append(nSamples.ToString(CultureInfo.InvariantCulture));

                        writer.WriteLine(sb.ToString());
                        written++;
                    }
                }
            }

            return written;
        }

        /// <summary>
        /// Total sunlit observations across the shard. Cheap consistency check:
        /// must never exceed samples x timesteps.
        /// </summary>
        public long TotalSunlit()
        {
            long total = 0;
            if (_counts == null) return 0;
            for (int i = 0; i < _counts.Length; i++) total += _counts[i];
            return total;
        }

        /// <summary>Releases the per-task buffers between tasks so a long-lived pod's RSS stays flat.</summary>
        public void Reset()
        {
            _samples = null; _sampleCount = 0;
            _edgeIds = null; _edgeSampleCount = null; _edgeCount = 0;
            _counts = null;
            _rawBuffer = null;
            RaycastsDone = 0;
        }
    }
}
