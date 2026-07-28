using System;
using System.Globalization;
using Npgsql;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// One leased unit of work: one section, one date, one 3-hour window.
    /// Immutable once claimed.
    /// </summary>
    public sealed class ExposureTask
    {
        public long     TaskId      { get; set; }
        public int      SectionId   { get; set; }
        public DateTime SimDate     { get; set; }
        public int      WindowIndex { get; set; }
        public int      StartMinute { get; set; }
        public int      EndMinute   { get; set; }
        public int      StepMinute  { get; set; }
        public int      ShardIndex  { get; set; }
        public int      Attempt     { get; set; }

        /// <summary>True if the coordinator matched this to the caller's warm working set.</summary>
        public bool     AffinityHit { get; set; }

        /// <summary>
        /// Timesteps in this window. HALF-OPEN, matching meo_window_bounds: the
        /// window covers [StartMinute, EndMinute) so six windows tile 03:00-21:00
        /// exactly, with no timestep written twice and none left without a
        /// partition to land in.
        ///
        /// v1's export loop used an inclusive endpoint and so ran 361 steps per day;
        /// half-open gives 360, which is also the figure every capacity calculation
        /// uses. The step it drops is 21:00, when the sun is below the horizon guard
        /// and every sample would have been recorded as shadowed anyway.
        /// </summary>
        public int StepCount => (EndMinute - StartMinute) / StepMinute;

        /// <summary>Leaf lower bound, inclusive.</summary>
        public DateTime WindowStart => SimDate.Date.AddMinutes(StartMinute);

        /// <summary>Leaf upper bound, exclusive.</summary>
        public DateTime WindowEnd => SimDate.Date.AddMinutes(EndMinute);

        public override string ToString() =>
            $"task#{TaskId} section {SectionId} shard {ShardIndex} {SimDate:yyyy-MM-dd} " +
            $"w{WindowIndex} [{StartMinute}..{EndMinute}) steps={StepCount} " +
            $"affinity={(AffinityHit ? "hit" : "miss")} attempt={Attempt}";
    }

    /// <summary>
    /// Thin client over the coordinator's SQL work queue (db/02_work_queue.sql).
    ///
    /// Deliberately synchronous and blocking. Unity's main thread already drives the
    /// physics loop, and these calls happen between bursts of work rather than
    /// during them — a few milliseconds each, a few times per task. The one thing
    /// that genuinely needed to be concurrent is the COPY, and that lives on its own
    /// thread in <see cref="ExposureWriter"/>.
    ///
    /// All queue semantics live in SQL functions rather than here, so the Python
    /// orchestrator and this C# worker share one implementation of
    /// claim/lease/reap/admit and cannot drift apart.
    /// </summary>
    public sealed class WorkQueueClient : IDisposable
    {
        private readonly WorkerConfig _cfg;
        private NpgsqlConnection _conn;

        /// <summary>
        /// Set when a heartbeat discovers this worker no longer owns its task (lease
        /// expired, task reassigned). The worker MUST stop and discard its buffers —
        /// this is the fencing check that prevents two workers building the same
        /// partition leaf.
        /// </summary>
        public bool LeaseLost { get; private set; }

        public NpgsqlConnection Connection => _conn;

        public WorkQueueClient(WorkerConfig cfg)
        {
            _cfg = cfg ?? throw new ArgumentNullException(nameof(cfg));
        }

        public void Connect()
        {
            _conn = new NpgsqlConnection(_cfg.CoordConnectionString);
            _conn.Open();

            // Every statement is deliberately inside its own implicit transaction, so
            // these are safe with PgBouncer in transaction mode. Session-level SET
            // would be lost when the pooler returns the backend — which is why
            // nothing here relies on session state surviving between calls.
            using (var cmd = new NpgsqlCommand(
                // Claim, heartbeat and complete are single-row updates; nothing here
                // sorts. Small on purpose: work_mem is per sort node and the
                // coordinator serves 50 clients.
                "SET work_mem = '16MB';" +
                // A claim that takes seconds means 50 workers are each waiting that
                // long. Failing fast surfaces it instead of hiding it as slow startup.
                "SET statement_timeout = '30s';",
                _conn))
            {
                cmd.ExecuteNonQuery();
            }

            Debug.Log($"[WorkQueue] connected to coordinator " +
                      $"{_cfg.CoordHost}:{_cfg.CoordPort}/{_cfg.CoordDb} " +
                      $"(server {_conn.PostgreSqlVersion})");
        }

        /// <summary>
        /// Verifies this worker's configuration matches the run's frozen config.
        ///
        /// This exists to prevent one specific, invisible corruption: a
        /// half-redeployed fleet writing two mutually inconsistent datasets into one
        /// run_id. Nothing would fail — the rows would all land, the counts would all
        /// check out, and the data would be wrong.
        ///
        /// The SECTION GRID is the most important thing checked. A worker whose grid
        /// origin differs by one metre computes different section ids for points near
        /// a boundary, and would write them into a neighbouring section's partition.
        /// That is undetectable after the fact.
        /// </summary>
        public void VerifyRunCompatibility()
        {
            const string sql = @"
                SELECT shard_count,
                       config->>'global_elevation',
                       config->>'sun_angle_threshold',
                       config->>'city',
                       config->>'section_origin_x',
                       config->>'section_origin_z',
                       config->>'section_meters',
                       config->>'section_cols'
                FROM meo_runs WHERE run_id = @run;";

            using var cmd = new NpgsqlCommand(sql, _conn);
            cmd.Parameters.AddWithValue("run", _cfg.RunId);
            using var r = cmd.ExecuteReader();

            if (!r.Read())
                throw new InvalidOperationException(
                    $"Run '{_cfg.RunId}' does not exist in meo_runs. " +
                    "Create it with orchestrator/plan_tasks.py before starting workers.");

            void Check(string name, int ordinal, string actual)
            {
                if (r.IsDBNull(ordinal)) return;         // run did not pin it
                string expected = r.GetString(ordinal);
                if (string.IsNullOrEmpty(expected)) return;

                if (!string.Equals(expected, actual, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException(
                        $"Run '{_cfg.RunId}' pins {name}={expected} but this worker has {actual}. " +
                        "Refusing to start: mixing these would produce an inconsistent dataset " +
                        "with no error and no way to tell afterwards which rows came from which.");
            }

            // "R" round-trip format on both sides so the comparison is exact rather
            // than a formatting coincidence, and InvariantCulture so a container with
            // a comma-decimal locale does not fail every check.
            var inv = CultureInfo.InvariantCulture;
            Check("global_elevation",    1, _cfg.GlobalElevation.ToString("R", inv));
            Check("sun_angle_threshold", 2, _cfg.SunAngleThreshold.ToString("R", inv));
            Check("city",                3, _cfg.CityName);
            Check("section_origin_x",    4, _cfg.SectionOriginX.ToString("G", inv));
            Check("section_origin_z",    5, _cfg.SectionOriginZ.ToString("G", inv));
            Check("section_meters",      6, _cfg.SectionMeters.ToString("G", inv));
            Check("section_cols",        7, _cfg.SectionCols.ToString(inv));

            Debug.Log($"[WorkQueue] run '{_cfg.RunId}' verified compatible " +
                      $"(shard_count={r.GetInt32(0)}, grid and simulation constants match).");
        }

        /// <summary>
        /// Claims the next task, or returns null when nothing is available.
        ///
        /// The two hints are the caller's currently-loaded working set. The
        /// coordinator will hand back a task matching them if one is available on an
        /// admissible shard, so the geometry and the BVH pages stay warm — 504
        /// working-set loads across the fleet instead of 6,048. If nothing matches it
        /// falls through to longest-processing-time-first, so affinity can never
        /// stall the queue or unbalance the cluster.
        ///
        /// NULL DOES NOT MEAN "RUN FINISHED". It also means every shard is at its
        /// admission cap, which is the healthy steady state whenever the fleet is
        /// larger than shards x cap. The caller must therefore keep polling rather
        /// than exiting on the first empty result — see MaxEmptyPolls.
        /// </summary>
        public ExposureTask TryClaim(int preferSection = -1, int preferWindow = -1)
        {
            const string sql =
                "SELECT * FROM meo_claim_task(@run, @worker, @lease, @sec, @win);";

            using var cmd = new NpgsqlCommand(sql, _conn);
            cmd.Parameters.AddWithValue("run", _cfg.RunId);
            cmd.Parameters.AddWithValue("worker", _cfg.WorkerId);
            cmd.Parameters.AddWithValue("lease", _cfg.LeaseSeconds);
            cmd.Parameters.AddWithValue("sec",
                preferSection >= 0 ? (object)preferSection : DBNull.Value);
            cmd.Parameters.AddWithValue("win",
                preferWindow >= 0 ? (object)preferWindow : DBNull.Value);

            using var r = cmd.ExecuteReader();
            if (!r.Read()) return null;

            LeaseLost = false;

            return new ExposureTask
            {
                TaskId      = r.GetInt64(0),
                SectionId   = r.GetInt32(1),
                SimDate     = r.GetDateTime(2),
                WindowIndex = r.GetInt32(3),
                StartMinute = r.GetInt32(4),
                EndMinute   = r.GetInt32(5),
                StepMinute  = r.GetInt32(6),
                ShardIndex  = r.GetInt32(7),
                Attempt     = r.GetInt32(8),
                AffinityHit = !r.IsDBNull(9) && r.GetBoolean(9),
            };
        }

        /// <summary>
        /// Renews the lease. Returns false if ownership was lost, in which case
        /// <see cref="LeaseLost"/> is latched and the caller must abandon the task.
        ///
        /// A DB error here is treated as "keep going": a transient network blip
        /// should not throw away a half-finished task. The lease absorbs ~29 failed
        /// heartbeats, and if the outage really does outlast it then reassignment is
        /// the correct outcome anyway.
        /// </summary>
        public bool Heartbeat(long taskId, long raycastsDone)
        {
            try
            {
                const string sql = "SELECT meo_heartbeat(@task, @worker, @lease, @rays);";
                using var cmd = new NpgsqlCommand(sql, _conn);
                cmd.Parameters.AddWithValue("task", taskId);
                cmd.Parameters.AddWithValue("worker", _cfg.WorkerId);
                cmd.Parameters.AddWithValue("lease", _cfg.LeaseSeconds);
                cmd.Parameters.AddWithValue("rays", raycastsDone);

                bool ok = cmd.ExecuteScalar() is bool b && b;

                if (!ok)
                {
                    LeaseLost = true;
                    Debug.LogError(
                        $"[WorkQueue] LEASE LOST for task#{taskId}. Another worker owns it now. " +
                        "Abandoning current work to avoid two workers building the same " +
                        "partition leaf.");
                }
                return ok;
            }
            catch (Exception e)
            {
                // Do NOT set LeaseLost: we do not know that we lost it, only that we
                // could not confirm. Erring toward continuing avoids discarding good
                // work over a momentary connection problem.
                Debug.LogWarning($"[WorkQueue] heartbeat failed (continuing): {e.Message}");
                return true;
            }
        }

        public void Complete(long taskId, long rowsWritten, long raycasts)
        {
            const string sql = "SELECT meo_complete_task(@task, @worker, @rows, @rays);";
            using var cmd = new NpgsqlCommand(sql, _conn);
            cmd.Parameters.AddWithValue("task", taskId);
            cmd.Parameters.AddWithValue("worker", _cfg.WorkerId);
            cmd.Parameters.AddWithValue("rows", rowsWritten);
            cmd.Parameters.AddWithValue("rays", raycasts);

            bool ok = cmd.ExecuteScalar() is bool b && b;
            if (!ok)
                Debug.LogWarning(
                    $"[WorkQueue] could not mark task#{taskId} done — its lease was probably " +
                    "reclaimed while the flush was in flight. The retry will rebuild the same " +
                    "leaf with the same data, so this is safe, only wasteful.");
        }

        /// <summary>
        /// Reports failure. The SQL side decides retry-versus-terminal from the
        /// attempt count, so that policy lives in exactly one place.
        /// </summary>
        public void Fail(long taskId, string error)
        {
            try
            {
                const string sql = "SELECT meo_fail_task(@task, @worker, @err);";
                using var cmd = new NpgsqlCommand(sql, _conn);
                cmd.Parameters.AddWithValue("task", taskId);
                cmd.Parameters.AddWithValue("worker", _cfg.WorkerId);
                cmd.Parameters.AddWithValue("err", error ?? "(no message)");
                cmd.ExecuteScalar();
            }
            catch (Exception e)
            {
                // Nothing useful left to do: if we cannot even report the failure, the
                // lease expires and the reaper requeues the task — which also frees the
                // admission slot this worker was holding on its shard.
                Debug.LogError($"[WorkQueue] could not report failure for task#{taskId}: {e.Message}");
            }
        }

        public void Dispose()
        {
            try { _conn?.Close(); } catch { /* shutting down anyway */ }
            _conn?.Dispose();
            _conn = null;
        }
    }
}
