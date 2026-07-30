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
    /// Every value is validated once at startup and the resolved set is logged. A
    /// worker that cannot fully configure itself exits non-zero immediately rather
    /// than half-running: a pod that starts and then silently does the wrong work
    /// is far more expensive to debug than one that refuses to boot.
    ///
    /// Note which endpoints appear here and which do not. The COORDINATOR's address
    /// is configuration; the SHARDS' addresses are not — they are read from the
    /// coordinator's meo_shards registry at boot (see <see cref="ShardRouter"/>),
    /// so an instance can be replaced mid-run without redeploying 54 pods.
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

        // ---- Coordinator ----------------------------------------------------
        /// <summary>
        /// The control-plane instance: work queue and section -> shard routing.
        /// Normally PgBouncer's address rather than PostgreSQL's — the claim and
        /// heartbeat traffic is thousands of tiny transactions from 50 clients,
        /// which is precisely what a transaction pooler is for.
        /// </summary>
        public string CoordHost { get; private set; }
        public string CoordPort { get; private set; }
        public string CoordDb   { get; private set; }

        // ---- Credentials, shared by the coordinator and the shards -----------
        public string DbUser     { get; private set; }
        public string DbPassword { get; private set; }

        // ---- Queue behaviour ------------------------------------------------
        /// <summary>
        /// Lease duration. Must exceed the worst-case time to process one task, or a
        /// healthy worker's lease expires mid-task and its work is duplicated. The
        /// heartbeat is what actually keeps it alive; this is the grace period after
        /// a worker goes silent.
        /// </summary>
        public int LeaseSeconds { get; private set; }

        /// <summary>
        /// How often to renew. Must be comfortably below <see cref="LeaseSeconds"/>
        /// so a transient DB hiccup does not cost the lease.
        /// </summary>
        public int HeartbeatSeconds { get; private set; }

        /// <summary>Seconds to wait before re-polling an empty queue.</summary>
        public int EmptyQueuePollSeconds { get; private set; }

        /// <summary>
        /// Consecutive empty polls before the worker exits 0. This is how the fleet
        /// drains: once the queue is dry every pod exits cleanly and the Job
        /// completes.
        ///
        /// An empty poll does NOT only mean "no work left". It also happens when
        /// every shard is at its admission cap, which is the healthy steady state
        /// for a fleet larger than shards x cap. So this must be generous enough
        /// that a worker waiting its turn for database capacity does not mistake
        /// that for the end of the run.
        /// </summary>
        public int MaxEmptyPolls { get; private set; }

        // ---- Simulation -----------------------------------------------------
        /// <summary>Shared planar elevation for all graph geometry. Pinned by the run.</summary>
        public float GlobalElevation { get; private set; }

        /// <summary>
        /// Degrees from the horizon within which a sample is forced to "shadowed".
        ///
        /// A correctness parameter, not a performance one — see the horizon guard in
        /// <see cref="SectionExposureSampler.AccumulateTimestep"/>. It also bounds
        /// the shadow halo exactly, which is what makes per-section tasks
        /// independent, so changing it changes the sharding argument as well as the
        /// data. Pinned by the run for that reason.
        /// </summary>
        public float SunAngleThreshold { get; private set; }

        public string CityName { get; private set; }
        public int    SolarYear { get; private set; }

        /// <summary>Metres to lift a ray origin above the road, so a road never shadows itself.</summary>
        public float RayOriginLift { get; private set; }

        /// <summary>
        /// Ray length. Must exceed the longest possible sightline to the sun through
        /// the city — 10 km comfortably does for Manhattan. Too short would report
        /// a sample as sunlit because the ray stopped before reaching the building
        /// that shades it.
        /// </summary>
        public float MaxRayDistance { get; private set; }

        // ---- Section grid (verified against the run, never trusted blindly) --
        public double SectionOriginX { get; private set; }
        public double SectionOriginZ { get; private set; }
        public double SectionMeters  { get; private set; }
        public int    SectionCols    { get; private set; }

        public SectionGrid Grid =>
            new SectionGrid(SectionOriginX, SectionOriginZ, SectionMeters, SectionCols);

        /// <summary>
        /// Maximum horizontal reach of a shadow, and therefore the exact radius
        /// outside a section within which geometry can still affect it.
        ///
        /// A building of height H shadows H/tan(theta) horizontally at sun elevation
        /// theta; the horizon guard means theta is never below
        /// <see cref="SunAngleThreshold"/>, so this is a bound rather than an
        /// estimate. It is the correctness argument for bounding-box sharding, and
        /// it is logged at boot so the assumption is visible in every pod's log.
        /// </summary>
        public float ShadowHaloMetres =>
            MaxBuildingMetres / Mathf.Tan(SunAngleThreshold * Mathf.Deg2Rad);

        /// <summary>Tallest collider expected in the scene. Only used to report the halo.</summary>
        public float MaxBuildingMetres { get; private set; }

        // ---- Buffer sizing (drives every persistent allocation) --------------
        /// <summary>
        /// Largest section, in sample points. Every buffer in the sampler and the
        /// writer is sized from this ONCE at boot and reused for the pod's life, so
        /// raising it costs memory permanently and exceeding it fails the task with
        /// a clear message rather than reallocating.
        ///
        /// Manhattan's densest square kilometre holds ~4,400 points; 16,384 leaves
        /// room for a 3.7x denser city at ~2.6 MB of buffers.
        /// </summary>
        public int MaxSectionSamples { get; private set; }

        /// <summary>Timesteps in the longest window. 6 windows over 03:00-21:00 at 3 min = 60.</summary>
        public int MaxStepsPerWindow { get; private set; }

        /// <summary>
        /// Rays per job-system batch. Small enough that a section's rays spread
        /// across every worker thread, large enough that scheduling overhead stays
        /// negligible — at the default of 1 the job system would create thousands of
        /// jobs per timestep and spend longer scheduling than raycasting.
        /// </summary>
        public int MinCommandsPerJob { get; private set; }

        // ---- Layers ---------------------------------------------------------
        /// <summary>
        /// Layer mask (raw int) for geometry that casts shadows. Passed as an int
        /// because a headless build has no Inspector to pick layers in.
        /// </summary>
        public int ShadowCasterMask { get; private set; }
        public int GroundBlockerMask { get; private set; }

        // ---- Operational ----------------------------------------------------
        /// <summary>Abort a task exceeding this, so one pathological task cannot hold a slot — or a shard's admission slot — forever.</summary>
        public int TaskTimeoutSeconds { get; private set; }
        public bool VerboseLogging { get; private set; }

        /// <summary>
        /// Coordinator connection string. Pooling is off: the worker holds one
        /// long-lived connection, and Npgsql's own pool would add churn on top of
        /// PgBouncer, which is already doing the pooling that matters.
        /// </summary>
        public string CoordConnectionString =>
            $"Host={CoordHost};Port={CoordPort};Database={CoordDb};" +
            $"Username={DbUser};Password={DbPassword};" +
            "Keepalive=30;Timeout=30;CommandTimeout=0;Pooling=false;" +
            // PgBouncer in transaction mode cannot carry server-side prepared
            // statements across transactions. Npgsql only prepares implicitly when
            // Max Auto Prepare is set, which it is not — stated here because
            // enabling it later would break the pooler in a way that presents as
            // sporadic "prepared statement does not exist".
            "Max Auto Prepare=0;";

        // ---------------------------------------------------------------------

        private WorkerConfig() { }

        /// <summary>
        /// Builds and validates config from the environment. Throws
        /// <see cref="InvalidOperationException"/> listing every problem at once —
        /// reporting all errors beats making an operator fix them one redeploy at a
        /// time.
        /// </summary>
        public static WorkerConfig FromEnvironment()
        {
            var c = new WorkerConfig
            {
                WorkerId = Env("SUNLIT_WORKER_ID", DefaultWorkerId()),
                RunId    = Env("SUNLIT_RUN_ID", ""),

                CoordHost = Env("SUNLIT_COORD_HOST", "pgbouncer"),
                CoordPort = Env("SUNLIT_COORD_PORT", "6432"),
                CoordDb   = Env("SUNLIT_COORD_DB", "sunlit_coord"),

                DbUser     = Env("SUNLIT_DB_USER", "admin"),
                DbPassword = Env("SUNLIT_DB_PASSWORD", ""),

                LeaseSeconds          = EnvInt("SUNLIT_LEASE_SECONDS", 900),
                HeartbeatSeconds      = EnvInt("SUNLIT_HEARTBEAT_SECONDS", 30),
                EmptyQueuePollSeconds = EnvInt("SUNLIT_EMPTY_POLL_SECONDS", 10),
                MaxEmptyPolls         = EnvInt("SUNLIT_MAX_EMPTY_POLLS", 12),

                GlobalElevation   = EnvFloat("SUNLIT_GLOBAL_ELEVATION", -112.0f),
                SunAngleThreshold = EnvFloat("SUNLIT_SUN_ANGLE_THRESHOLD", 5.0f),
                CityName          = Env("SUNLIT_CITY", "Manhattan"),
                SolarYear         = EnvInt("SUNLIT_SOLAR_YEAR", 2026),
                RayOriginLift     = EnvFloat("SUNLIT_RAY_ORIGIN_LIFT", 3.0f),
                MaxRayDistance    = EnvFloat("SUNLIT_MAX_RAY_DISTANCE", 10000.0f),
                MaxBuildingMetres = EnvFloat("SUNLIT_MAX_BUILDING_METRES", 200.0f),

                SectionOriginX = EnvDouble("SUNLIT_SECTION_ORIGIN_X", 0.0),
                SectionOriginZ = EnvDouble("SUNLIT_SECTION_ORIGIN_Z", 0.0),
                SectionMeters  = EnvDouble("SUNLIT_SECTION_METERS", 1000.0),
                SectionCols    = EnvInt("SUNLIT_SECTION_COLS", 128),

                MaxSectionSamples = EnvInt("SUNLIT_MAX_SECTION_SAMPLES", 16384),
                MaxStepsPerWindow = EnvInt("SUNLIT_MAX_STEPS_PER_WINDOW", 60),
                MinCommandsPerJob = EnvInt("SUNLIT_MIN_COMMANDS_PER_JOB", 64),

                ShadowCasterMask  = EnvInt("SUNLIT_SHADOW_CASTER_MASK", ~0),
                GroundBlockerMask = EnvInt("SUNLIT_GROUND_BLOCKER_MASK", 0),

                TaskTimeoutSeconds = EnvInt("SUNLIT_TASK_TIMEOUT_SECONDS", 1800),
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
                    $"SUNLIT_LEASE_SECONDS ({LeaseSeconds}), so a transient DB stall does " +
                    "not cost a healthy worker its lease.");

            if (SunAngleThreshold <= 0f || SunAngleThreshold > 45f)
                errors.AppendLine(
                    $"  SUNLIT_SUN_ANGLE_THRESHOLD ({SunAngleThreshold}) outside 0..45. " +
                    "It must be strictly positive: at 0 the shadow halo is unbounded and " +
                    "per-section tasks would no longer be independent.");

            if (ShadowCasterMask == 0 && GroundBlockerMask == 0)
                errors.AppendLine(
                    "  Both layer masks are 0 — every raycast would miss and every point " +
                    "would read 'sunlit'. Refusing to produce plausible-looking wrong data.");

            if (MaxSectionSamples < 1024)
                errors.AppendLine(
                    $"  SUNLIT_MAX_SECTION_SAMPLES ({MaxSectionSamples}) is below any " +
                    "realistic section. Every persistent buffer is sized from it at boot.");

            if (MaxStepsPerWindow < 1)
                errors.AppendLine($"  SUNLIT_MAX_STEPS_PER_WINDOW ({MaxStepsPerWindow}) must be >= 1.");

            if (MinCommandsPerJob < 1)
                errors.AppendLine($"  SUNLIT_MIN_COMMANDS_PER_JOB ({MinCommandsPerJob}) must be >= 1.");

            if (SectionMeters <= 0.0)
                errors.AppendLine($"  SUNLIT_SECTION_METERS ({SectionMeters}) must be positive.");

            if (SectionCols <= 0)
                errors.AppendLine($"  SUNLIT_SECTION_COLS ({SectionCols}) must be positive.");

            if (MaxRayDistance <= 0f)
                errors.AppendLine($"  SUNLIT_MAX_RAY_DISTANCE ({MaxRayDistance}) must be positive.");

            // The halo is only meaningful if it exceeds a section: if a section were
            // wider than the longest shadow, the reasoning would still hold but the
            // locality argument for the BVH working set would not, so flag it.
            if (SunAngleThreshold > 0f && ShadowHaloMetres < SectionMeters)
                errors.AppendLine(
                    $"  shadow halo ({ShadowHaloMetres:F0} m) is smaller than a section " +
                    $"({SectionMeters:F0} m). Not wrong, but the section size is then doing " +
                    "no work — reduce SUNLIT_SECTION_METERS or raise the threshold.");

            if (errors.Length > 0)
                throw new InvalidOperationException("Worker configuration is invalid:\n" + errors);
        }

        /// <summary>
        /// Log the resolved configuration. Called once at startup so a pod's log
        /// begins with exactly what it believes it was told to do — the first thing
        /// anyone wants when a fleet misbehaves. The password is never logged.
        /// </summary>
        public string Describe()
        {
            var sb = new StringBuilder();
            sb.AppendLine("[WorkerConfig] resolved:");
            sb.AppendLine($"  worker_id         = {WorkerId}");
            sb.AppendLine($"  run_id            = {RunId}");
            sb.AppendLine($"  coordinator       = {DbUser}@{CoordHost}:{CoordPort}/{CoordDb}");
            sb.AppendLine($"  shards            = resolved from meo_shards at boot");
            sb.AppendLine($"  lease/heartbeat   = {LeaseSeconds}s / {HeartbeatSeconds}s");
            sb.AppendLine($"  drain after       = {MaxEmptyPolls} empty polls x {EmptyQueuePollSeconds}s");
            sb.AppendLine($"  city / solar year = {CityName} / {SolarYear}");
            sb.AppendLine($"  global elevation  = {GlobalElevation}");
            sb.AppendLine($"  sun threshold     = {SunAngleThreshold} deg");
            sb.AppendLine($"  shadow halo       = {ShadowHaloMetres:F0} m " +
                          $"({MaxBuildingMetres:F0} m / tan {SunAngleThreshold:F0} deg) " +
                          "— the exact bound making sections independent");
            sb.AppendLine($"  section grid      = {Grid}");
            sb.AppendLine($"  ray lift/length   = {RayOriginLift:F1} m / {MaxRayDistance:F0} m");
            sb.AppendLine($"  buffers           = {MaxSectionSamples:N0} samples x " +
                          $"{MaxStepsPerWindow} steps, allocated once");
            sb.AppendLine($"  batch grain       = {MinCommandsPerJob} rays/job");
            sb.AppendLine($"  caster/ground msk = 0x{ShadowCasterMask:X8} / 0x{GroundBlockerMask:X8}");
            sb.AppendLine($"  task timeout      = {TaskTimeoutSeconds}s");
            return sb.ToString();
        }

        /// <summary>
        /// Convenience for running the worker inside the Editor, where there is no
        /// container to inject env. Sets only variables that are not already set, so
        /// a partially-configured shell still wins.
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
            SetIfEmpty("SUNLIT_COORD_HOST", "localhost");
            SetIfEmpty("SUNLIT_COORD_PORT", "5432");
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

        private static double EnvDouble(string key, double fallback)
        {
            string v = Environment.GetEnvironmentVariable(key);
            if (string.IsNullOrWhiteSpace(v)) return fallback;
            // Double, not float, for the grid origin: see the note in SectionGrid on
            // why the section formula must not be computed in single precision.
            if (double.TryParse(v.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out double parsed))
                return parsed;
            Debug.LogWarning($"[WorkerConfig] {key}='{v}' is not a number; using {fallback}.");
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
