using System;
using System.Globalization;
using System.Text;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// All worker configuration, read from environment variables.
    ///
    /// Env vars rather than a config file or CLI args because that is what a
    /// container orchestrator natively supplies: a Kubernetes ConfigMap and Secret
    /// both project as env, so the same image runs unchanged in a Job, a local
    /// docker run, and the Unity Editor (via <see cref="ApplyEditorDefaults"/>).
    ///
    /// Every value is validated once at startup and the resolved set is logged.
    /// A worker that cannot fully configure itself exits non-zero immediately
    /// rather than half-running — a pod that starts and then silently does the
    /// wrong work is far more expensive to debug than one that refuses to boot.
    /// </summary>
    public sealed class WorkerConfig
    {
        // ---- Identity -------------------------------------------------------
        /// <summary>
        /// Unique per pod. Defaults to the Kubernetes pod name (injected via the
        /// downward API), which is already unique and, unlike a random GUID,
        /// correlates a leased task with a specific pod's logs.
        /// </summary>
        public string WorkerId { get; private set; }

        /// <summary>Groups tasks belonging to one logical execution of the pipeline.</summary>
        public string RunId { get; private set; }

        // ---- Database -------------------------------------------------------
        public string DbHost { get; private set; }
        public string DbPort { get; private set; }
        public string DbName { get; private set; }
        public string DbUser { get; private set; }
        public string DbPassword { get; private set; }

        // ---- Queue behaviour ------------------------------------------------
        /// <summary>
        /// Lease duration. Must exceed the worst-case time to process one task,
        /// or a healthy worker's lease expires mid-task and its work is duplicated.
        /// The heartbeat interval is what actually keeps it alive; this is the
        /// grace period after a worker goes silent.
        /// </summary>
        public int LeaseSeconds { get; private set; }

        /// <summary>
        /// How often to renew the lease. Must be comfortably below
        /// <see cref="LeaseSeconds"/> so a transient DB hiccup doesn't cost the lease.
        /// </summary>
        public int HeartbeatSeconds { get; private set; }

        /// <summary>Seconds to wait before re-polling an empty queue.</summary>
        public int EmptyQueuePollSeconds { get; private set; }

        /// <summary>
        /// Consecutive empty polls before the worker exits 0. This is how the
        /// fleet drains: once the queue is dry every pod exits cleanly and the
        /// K8s Job completes. Too low and a worker quits during a brief lull
        /// while peers still hold tasks that might fail back to pending.
        /// </summary>
        public int MaxEmptyPolls { get; private set; }

        // ---- Simulation -----------------------------------------------------
        /// <summary>Shared planar elevation for all graph geometry. Must match the DB.</summary>
        public float GlobalElevation { get; private set; }

        /// <summary>Degrees from the horizon within which a sample is forced to "shadowed".</summary>
        public float SunAngleThreshold { get; private set; }

        public string CityName { get; private set; }
        public int SolarYear { get; private set; }

        /// <summary>
        /// Persist per-sample booleans in addition to edge aggregates. Off by
        /// default: this is the difference between writing ~2 GB and ~110 GB.
        /// The task row can override per-task.
        /// </summary>
        public bool EmitRaw { get; private set; }

        /// <summary>
        /// Rows buffered before a COPY flush. Larger amortises round-trips but
        /// raises peak memory and lengthens the window a crash discards.
        /// </summary>
        public int CopyBatchRows { get; private set; }

        // ---- Layers ---------------------------------------------------------
        /// <summary>
        /// Layer mask (raw int) for geometry that casts shadows. Passed as an int
        /// because a headless build has no Inspector to pick layers in.
        /// </summary>
        public int ShadowCasterMask { get; private set; }
        public int GroundBlockerMask { get; private set; }

        // ---- Operational ----------------------------------------------------
        /// <summary>Abort a task that exceeds this, so one pathological task can't hold a slot forever.</summary>
        public int TaskTimeoutSeconds { get; private set; }
        public bool VerboseLogging { get; private set; }

        public string ConnectionString =>
            $"Host={DbHost};Port={DbPort};Database={DbName};Username={DbUser};Password={DbPassword};" +
            // Keepalives: the reduce/promote step can run for minutes server-side, and
            // without these a stateful firewall or LB silently drops the idle socket.
            "Keepalive=30;Timeout=30;CommandTimeout=0;" +
            // No pooling: a worker holds exactly one long-lived connection. Npgsql's
            // pool would add reconnect churn and can hand back a connection whose
            // session-scoped temp/staging tables have vanished.
            "Pooling=false;";

        // ---------------------------------------------------------------------

        private WorkerConfig() { }

        /// <summary>
        /// Builds and validates config from the environment. Throws
        /// <see cref="InvalidOperationException"/> listing every problem at once —
        /// reporting all errors beats making an operator fix them one redeploy
        /// at a time.
        /// </summary>
        public static WorkerConfig FromEnvironment()
        {
            var c = new WorkerConfig
            {
                WorkerId = Env("SUNLIT_WORKER_ID", DefaultWorkerId()),
                RunId    = Env("SUNLIT_RUN_ID", ""),

                DbHost     = Env("SUNLIT_DB_HOST", "localhost"),
                DbPort     = Env("SUNLIT_DB_PORT", "5432"),
                DbName     = Env("SUNLIT_DB_NAME", "city_data"),
                DbUser     = Env("SUNLIT_DB_USER", "admin"),
                DbPassword = Env("SUNLIT_DB_PASSWORD", ""),

                LeaseSeconds          = EnvInt("SUNLIT_LEASE_SECONDS", 900),
                HeartbeatSeconds      = EnvInt("SUNLIT_HEARTBEAT_SECONDS", 30),
                EmptyQueuePollSeconds = EnvInt("SUNLIT_EMPTY_POLL_SECONDS", 10),
                MaxEmptyPolls         = EnvInt("SUNLIT_MAX_EMPTY_POLLS", 6),

                GlobalElevation   = EnvFloat("SUNLIT_GLOBAL_ELEVATION", -112.0f),
                SunAngleThreshold = EnvFloat("SUNLIT_SUN_ANGLE_THRESHOLD", 5.0f),
                CityName          = Env("SUNLIT_CITY", "Manhattan"),
                SolarYear         = EnvInt("SUNLIT_SOLAR_YEAR", 2026),
                EmitRaw           = EnvBool("SUNLIT_EMIT_RAW", false),
                CopyBatchRows     = EnvInt("SUNLIT_COPY_BATCH_ROWS", 250000),

                ShadowCasterMask  = EnvInt("SUNLIT_SHADOW_CASTER_MASK", ~0),
                GroundBlockerMask = EnvInt("SUNLIT_GROUND_BLOCKER_MASK", 0),

                TaskTimeoutSeconds = EnvInt("SUNLIT_TASK_TIMEOUT_SECONDS", 3600),
                VerboseLogging     = EnvBool("SUNLIT_VERBOSE", false),
            };

            c.Validate();
            return c;
        }

        private void Validate()
        {
            var errors = new StringBuilder();

            if (string.IsNullOrWhiteSpace(RunId))
                errors.AppendLine("  SUNLIT_RUN_ID is required (identifies which run's tasks to claim).");

            if (string.IsNullOrWhiteSpace(DbPassword))
                errors.AppendLine("  SUNLIT_DB_PASSWORD is required (mount from a Kubernetes Secret).");

            if (string.IsNullOrWhiteSpace(WorkerId))
                errors.AppendLine("  SUNLIT_WORKER_ID resolved empty.");

            // A heartbeat that cannot complete several times inside the lease window
            // makes lease expiry a matter of luck rather than of actual failure.
            if (HeartbeatSeconds * 3 > LeaseSeconds)
                errors.AppendLine(
                    $"  SUNLIT_HEARTBEAT_SECONDS ({HeartbeatSeconds}) must be < 1/3 of " +
                    $"SUNLIT_LEASE_SECONDS ({LeaseSeconds}), so a transient DB stall " +
                    "does not cost a healthy worker its lease.");

            if (CopyBatchRows < 1000)
                errors.AppendLine($"  SUNLIT_COPY_BATCH_ROWS ({CopyBatchRows}) is too small to amortise COPY overhead; use >= 1000.");

            if (SunAngleThreshold < 0f || SunAngleThreshold > 45f)
                errors.AppendLine($"  SUNLIT_SUN_ANGLE_THRESHOLD ({SunAngleThreshold}) outside sane range 0..45.");

            if (ShadowCasterMask == 0 && GroundBlockerMask == 0)
                errors.AppendLine("  Both layer masks are 0 — every raycast would miss and every point would read 'sunlit'.");

            if (errors.Length > 0)
                throw new InvalidOperationException(
                    "Worker configuration is invalid:\n" + errors);
        }

        /// <summary>
        /// Log the resolved configuration. Called once at startup so a pod's log
        /// begins with exactly what it believes it was told to do — the first
        /// thing anyone wants when a fleet misbehaves. The password is never logged.
        /// </summary>
        public string Describe()
        {
            var sb = new StringBuilder();
            sb.AppendLine("[WorkerConfig] resolved:");
            sb.AppendLine($"  worker_id         = {WorkerId}");
            sb.AppendLine($"  run_id            = {RunId}");
            sb.AppendLine($"  db                = {DbUser}@{DbHost}:{DbPort}/{DbName}");
            sb.AppendLine($"  lease/heartbeat   = {LeaseSeconds}s / {HeartbeatSeconds}s");
            sb.AppendLine($"  drain after       = {MaxEmptyPolls} empty polls x {EmptyQueuePollSeconds}s");
            sb.AppendLine($"  city / solar year = {CityName} / {SolarYear}");
            sb.AppendLine($"  global elevation  = {GlobalElevation}");
            sb.AppendLine($"  sun threshold     = {SunAngleThreshold} deg");
            sb.AppendLine($"  emit raw samples  = {EmitRaw}");
            sb.AppendLine($"  copy batch rows   = {CopyBatchRows:N0}");
            sb.AppendLine($"  caster/ground msk = 0x{ShadowCasterMask:X8} / 0x{GroundBlockerMask:X8}");
            sb.AppendLine($"  task timeout      = {TaskTimeoutSeconds}s");
            return sb.ToString();
        }

        /// <summary>
        /// Convenience for running the worker inside the Editor, where there is no
        /// container to inject env. Sets only variables that are not already set,
        /// so a partially-configured shell still wins.
        /// </summary>
        public static void ApplyEditorDefaults(string runId = "editor-local")
        {
            void SetIfEmpty(string k, string v)
            {
                if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(k)))
                    Environment.SetEnvironmentVariable(k, v);
            }

            SetIfEmpty("SUNLIT_RUN_ID", runId);
            SetIfEmpty("SUNLIT_WORKER_ID", "editor-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            SetIfEmpty("SUNLIT_DB_PASSWORD", "password");
            SetIfEmpty("SUNLIT_VERBOSE", "true");
        }

        // ---- env helpers ----------------------------------------------------

        private static string DefaultWorkerId()
        {
            // HOSTNAME is the pod name inside Kubernetes; falls back to a random
            // suffix so two local runs never collide.
            string host = Environment.GetEnvironmentVariable("HOSTNAME");
            if (!string.IsNullOrWhiteSpace(host)) return host;
            return "worker-" + Guid.NewGuid().ToString("N").Substring(0, 8);
        }

        private static string Env(string key, string fallback)
        {
            string v = Environment.GetEnvironmentVariable(key);
            return string.IsNullOrWhiteSpace(v) ? fallback : v.Trim();
        }

        private static int EnvInt(string key, int fallback)
        {
            string v = Environment.GetEnvironmentVariable(key);
            if (string.IsNullOrWhiteSpace(v)) return fallback;
            if (int.TryParse(v.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int parsed))
                return parsed;
            Debug.LogWarning($"[WorkerConfig] {key}='{v}' is not an integer; using {fallback}.");
            return fallback;
        }

        private static float EnvFloat(string key, float fallback)
        {
            string v = Environment.GetEnvironmentVariable(key);
            if (string.IsNullOrWhiteSpace(v)) return fallback;
            // InvariantCulture: a container inheriting a comma-decimal locale would
            // otherwise parse "-112.0" as -1120.
            if (float.TryParse(v.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out float parsed))
                return parsed;
            Debug.LogWarning($"[WorkerConfig] {key}='{v}' is not a float; using {fallback}.");
            return fallback;
        }

        private static bool EnvBool(string key, bool fallback)
        {
            string v = Environment.GetEnvironmentVariable(key);
            if (string.IsNullOrWhiteSpace(v)) return fallback;
            switch (v.Trim().ToLowerInvariant())
            {
                case "1": case "true":  case "yes": case "y": case "on":  return true;
                case "0": case "false": case "no":  case "n": case "off": return false;
                default:
                    Debug.LogWarning($"[WorkerConfig] {key}='{v}' is not a boolean; using {fallback}.");
                    return fallback;
            }
        }
    }
}
