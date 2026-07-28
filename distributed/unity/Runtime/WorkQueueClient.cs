using System;
using Npgsql;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>One leased unit of work. Immutable once claimed.</summary>
    public sealed class ExposureTask
    {
        public long   TaskId       { get; set; }
        public int    ShardIndex   { get; set; }
        public int    ShardCount   { get; set; }
        public DateTime SimDate    { get; set; }
        public int    StartMinute  { get; set; }
        public int    EndMinute    { get; set; }
        public int    StepMinute   { get; set; }
        public bool   EmitRaw      { get; set; }
        public int    Attempt      { get; set; }

        /// <summary>Number of simulated timesteps this task will evaluate.</summary>
        public int StepCount => ((EndMinute - StartMinute) / StepMinute) + 1;

        public override string ToString() =>
            $"task#{TaskId} shard {ShardIndex}/{ShardCount} {SimDate:yyyy-MM-dd} " +
            $"[{StartMinute}..{EndMinute}/{StepMinute}] steps={StepCount} " +
            $"raw={EmitRaw} attempt={Attempt}";
    }

    /// <summary>
    /// Thin client over the SQL work queue (see distributed/db/02_work_queue.sql).
    ///
    /// Deliberately synchronous and blocking. Unity's main thread already drives
    /// the physics loop; introducing async here would mean marshalling raycast
    /// results across threads for no benefit, since Physics.Raycast is main-thread
    /// only and the DB calls happen between bursts of work, not during them.
    ///
    /// All queue semantics live in SQL functions rather than here, so a Python
    /// orchestrator and a C# worker share one implementation of claim/lease/reap
    /// and cannot drift apart.
    /// </summary>
    public sealed class WorkQueueClient : IDisposable
    {
        private readonly WorkerConfig _cfg;
        private NpgsqlConnection _conn;

        /// <summary>
        /// Set when a heartbeat discovers this worker no longer owns its task
        /// (lease expired, task reassigned). The worker MUST stop and discard its
        /// buffers — this is the fencing check that prevents two workers writing
        /// output for the same shard/date.
        /// </summary>
        public bool LeaseLost { get; private set; }

        public WorkQueueClient(WorkerConfig cfg)
        {
            _cfg = cfg ?? throw new ArgumentNullException(nameof(cfg));
        }

        public void Connect()
        {
            _conn = new NpgsqlConnection(_cfg.ConnectionString);
            _conn.Open();

            // Per-session tuning. Session scope keeps it out of the server config
            // and lets the worker be aggressive without affecting query clients.
            using (var cmd = new NpgsqlCommand(
                // The worker only ever writes reproducible data, so an fsync per
                // commit buys nothing: a lost commit is a lost task, which the
                // queue re-runs. This is the single highest-value client-side knob.
                "SET synchronous_commit = off;" +
                // Sized for the promote step's INSERT..SELECT sort.
                "SET work_mem = '128MB';" +
                // Never let a stuck server-side statement hold a lease forever.
                "SET statement_timeout = 0;" +
                "SET idle_in_transaction_session_timeout = '10min';",
                _conn))
            {
                cmd.ExecuteNonQuery();
            }

            Debug.Log($"[WorkQueue] connected to {_cfg.DbHost}:{_cfg.DbPort}/{_cfg.DbName} " +
                      $"(server {_conn.PostgreSqlVersion})");
        }

        /// <summary>
        /// Verifies this worker's configuration matches the run's frozen config,
        /// and that the run exists. Prevents a half-redeployed fleet from writing
        /// two mutually inconsistent datasets into one run_id — a corruption that
        /// is invisible until someone queries the result.
        /// </summary>
        public void VerifyRunCompatibility()
        {
            const string sql = @"
                SELECT shard_count,
                       config->>'global_elevation',
                       config->>'sun_angle_threshold',
                       config->>'city'
                FROM meo_runs WHERE run_id = @run;";

            using var cmd = new NpgsqlCommand(sql, _conn);
            cmd.Parameters.AddWithValue("run", _cfg.RunId);
            using var r = cmd.ExecuteReader();

            if (!r.Read())
                throw new InvalidOperationException(
                    $"Run '{_cfg.RunId}' does not exist in meo_runs. " +
                    "Create it with orchestrator/plan_tasks.py before starting workers.");

            // Compare only what would actually corrupt the dataset if it differed.
            string elev = r.IsDBNull(1) ? null : r.GetString(1);
            string thr  = r.IsDBNull(2) ? null : r.GetString(2);
            string city = r.IsDBNull(3) ? null : r.GetString(3);

            void Check(string name, string expected, string actual)
            {
                if (string.IsNullOrEmpty(expected)) return; // run didn't pin it
                if (!string.Equals(expected, actual, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException(
                        $"Run '{_cfg.RunId}' pins {name}={expected} but this worker has {actual}. " +
                        "Refusing to start: mixing these would produce an inconsistent dataset.");
            }

            Check("global_elevation", elev,
                  _cfg.GlobalElevation.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
            Check("sun_angle_threshold", thr,
                  _cfg.SunAngleThreshold.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
            Check("city", city, _cfg.CityName);

            Debug.Log($"[WorkQueue] run '{_cfg.RunId}' verified compatible.");
        }

        /// <summary>
        /// Claims the next task, or returns null if the queue is empty.
        /// Atomic server-side (FOR UPDATE SKIP LOCKED), so concurrent callers
        /// never receive the same task.
        /// </summary>
        public ExposureTask TryClaim()
        {
            const string sql = "SELECT * FROM meo_claim_task(@run, @worker, @lease);";

            using var cmd = new NpgsqlCommand(sql, _conn);
            cmd.Parameters.AddWithValue("run", _cfg.RunId);
            cmd.Parameters.AddWithValue("worker", _cfg.WorkerId);
            cmd.Parameters.AddWithValue("lease", _cfg.LeaseSeconds);

            using var r = cmd.ExecuteReader();
            if (!r.Read()) return null;

            LeaseLost = false;

            return new ExposureTask
            {
                TaskId      = r.GetInt64(0),
                ShardIndex  = r.GetInt32(1),
                ShardCount  = r.GetInt32(2),
                SimDate     = r.GetDateTime(3),
                StartMinute = r.GetInt32(4),
                EndMinute   = r.GetInt32(5),
                StepMinute  = r.GetInt32(6),
                EmitRaw     = r.GetBoolean(7),
                Attempt     = r.GetInt32(8),
            };
        }

        /// <summary>
        /// Renews the lease. Returns false if ownership was lost, in which case
        /// <see cref="LeaseLost"/> is latched and the caller must abandon the task.
        ///
        /// A DB error here is treated as "keep going": a transient network blip
        /// should not throw away a half-finished task. The lease is long enough to
        /// absorb several failed heartbeats, and if the outage really does outlast
        /// the lease then reassignment is the correct outcome anyway.
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

                object result = cmd.ExecuteScalar();
                bool ok = result is bool b && b;

                if (!ok)
                {
                    LeaseLost = true;
                    Debug.LogError(
                        $"[WorkQueue] LEASE LOST for task#{taskId}. Another worker owns it now. " +
                        "Abandoning current work to avoid duplicate output.");
                }
                return ok;
            }
            catch (Exception e)
            {
                // Do not set LeaseLost: we don't know that we lost it, only that we
                // couldn't confirm. Erring toward continuing avoids discarding good
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
                    $"[WorkQueue] could not mark task#{taskId} done — lease was probably " +
                    "reclaimed while it finished. Its output will be overwritten by the retry.");
        }

        /// <summary>
        /// Reports failure. The SQL side decides retry-vs-terminal based on attempts,
        /// so that policy lives in exactly one place.
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
                // Nothing useful left to do: if we cannot even report the failure,
                // the lease will expire and the reaper will requeue the task.
                Debug.LogError($"[WorkQueue] could not report failure for task#{taskId}: {e.Message}");
            }
        }

        public NpgsqlConnection Connection => _conn;

        public void Dispose()
        {
            try { _conn?.Close(); } catch { /* shutting down anyway */ }
            _conn?.Dispose();
            _conn = null;
        }
    }
}
