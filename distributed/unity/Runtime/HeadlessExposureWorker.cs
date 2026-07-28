using System;
using System.Collections;
using System.Diagnostics;
using System.Linq;
using Npgsql;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// Entry point for the headless Linux worker. One instance per pod.
    ///
    /// LIFECYCLE
    /// ---------
    ///   boot -> validate config -> connect -> verify run
    ///        -> loop { claim task | drain-exit }
    ///                  load shard -> sweep time -> flush -> promote -> complete
    ///        -> exit 0
    ///
    /// Any unhandled failure inside a task reports the task failed and continues to
    /// the next one. The pod only exits non-zero for problems that would affect
    /// every task (bad config, unreachable DB, missing scene) — retrying those in
    /// the same pod would just spin, so we let Kubernetes restart or fail the Job.
    ///
    /// WHY A COROUTINE AND NOT A PLAIN LOOP
    /// ------------------------------------
    /// Physics.Raycast requires Unity's main thread, and the transform of the sun
    /// light must be committed before a raycast observes it. Yielding
    /// WaitForFixedUpdate between timesteps is the supported way to guarantee that
    /// ordering. It also keeps the player responsive to SIGTERM, which matters for
    /// graceful preemption.
    /// </summary>
    public sealed class HeadlessExposureWorker : MonoBehaviour
    {
        [Header("Scene wiring (auto-resolved if left empty)")]
        [SerializeField] private Light sunLight;
        [SerializeField] private SolarDataLoader solarLoader;

        private WorkerConfig          _cfg;
        private WorkQueueClient       _queue;
        private ShardExposureCombiner _combiner;

        private int  _consecutiveEmptyPolls;
        private int  _tasksCompleted;
        private long _totalRaycasts;
        private readonly Stopwatch _uptime = Stopwatch.StartNew();

        /// <summary>
        /// Set by SIGTERM handling. Kubernetes sends SIGTERM then waits
        /// terminationGracePeriodSeconds before SIGKILL; we use that window to
        /// finish the current timestep, flush, and release the lease cleanly so the
        /// task is immediately reclaimable rather than waiting out its lease.
        /// </summary>
        private volatile bool _shutdownRequested;

        // =====================================================================
        // BOOT
        // =====================================================================

        private void Start()
        {
            // Headless determinism: no rendering, and a fixed timestep so
            // WaitForFixedUpdate advances predictably regardless of host speed.
            Application.runInBackground = true;
            Time.fixedDeltaTime = 0.02f;

            try
            {
                _cfg = WorkerConfig.FromEnvironment();
            }
            catch (Exception e)
            {
                Fatal("configuration invalid", e);
                return;
            }

            Debug.Log(BannerText());
            Debug.Log(_cfg.Describe());

            if (!ResolveSceneDependencies()) return;

            try
            {
                _queue = new WorkQueueClient(_cfg);
                _queue.Connect();
                _queue.VerifyRunCompatibility();
            }
            catch (Exception e)
            {
                Fatal("could not join run", e);
                return;
            }

            _combiner = new ShardExposureCombiner(_cfg);

            StartCoroutine(WorkerLoop());
        }

        /// <summary>
        /// Finds and sanity-checks everything the raycast loop depends on.
        /// Failing here rather than mid-task turns a subtle wrong-data bug into an
        /// obvious startup crash.
        /// </summary>
        private bool ResolveSceneDependencies()
        {
            if (sunLight == null)
            {
                sunLight = UnityEngine.Object
                    .FindObjectsByType<Light>(FindObjectsSortMode.None)
                    .FirstOrDefault(l => l.type == LightType.Directional);
            }

            if (sunLight == null)
            {
                Fatal("no directional Light in the scene — raycasts would have no sun direction", null);
                return false;
            }

            if (solarLoader == null)
                solarLoader = sunLight.GetComponent<SolarDataLoader>()
                              ?? UnityEngine.Object.FindFirstObjectByType<SolarDataLoader>();

            if (solarLoader == null)
            {
                Fatal("no SolarDataLoader in the scene", null);
                return false;
            }

            solarLoader.cityName = _cfg.CityName;
            if (!solarLoader.LoadYear(_cfg.SolarYear))
            {
                Fatal($"could not load solar binary for {_cfg.CityName} {_cfg.SolarYear}. " +
                      "Is StreamingAssets/SolarData baked into the image?", null);
                return false;
            }

            // Assert colliders actually exist. A headless build that shipped without
            // baked MeshColliders raycasts against nothing and would cheerfully
            // report the entire city as sunlit — the worst possible failure, because
            // it is silent and the data looks plausible.
            int colliderCount = UnityEngine.Object
                .FindObjectsByType<Collider>(FindObjectsSortMode.None).Length;
            if (colliderCount == 0)
            {
                Fatal("scene contains ZERO colliders. Every raycast would miss and every " +
                      "sample would be recorded as sunlit. Refusing to produce bad data.", null);
                return false;
            }

            Debug.Log($"[Worker] scene ready: sun='{sunLight.name}', {colliderCount:N0} colliders, " +
                      $"solar data {_cfg.CityName} {_cfg.SolarYear}");
            return true;
        }

        // =====================================================================
        // MAIN LOOP
        // =====================================================================

        private IEnumerator WorkerLoop()
        {
            while (!_shutdownRequested)
            {
                ExposureTask task = null;

                try
                {
                    task = _queue.TryClaim();
                }
                catch (Exception e)
                {
                    // A claim failure is usually a transient DB blip. Back off and
                    // retry rather than killing a pod that could still do work.
                    Debug.LogWarning($"[Worker] claim failed, backing off: {e.Message}");
                    yield return new WaitForSecondsRealtime(_cfg.EmptyQueuePollSeconds);
                    continue;
                }

                if (task == null)
                {
                    _consecutiveEmptyPolls++;

                    // Drain condition. Requiring several consecutive empties (rather
                    // than exiting on the first) avoids a pod quitting during a brief
                    // lull while peers still hold tasks that could yet fail back to
                    // pending and need picking up.
                    if (_consecutiveEmptyPolls >= _cfg.MaxEmptyPolls)
                    {
                        Debug.Log($"[Worker] queue empty for {_consecutiveEmptyPolls} consecutive polls " +
                                  "— draining and exiting 0.");
                        break;
                    }

                    Debug.Log($"[Worker] queue empty ({_consecutiveEmptyPolls}/{_cfg.MaxEmptyPolls}), " +
                              $"sleeping {_cfg.EmptyQueuePollSeconds}s");
                    yield return new WaitForSecondsRealtime(_cfg.EmptyQueuePollSeconds);
                    continue;
                }

                _consecutiveEmptyPolls = 0;
                yield return ProcessTask(task);
            }

            Shutdown(0);
        }

        /// <summary>
        /// Executes one task end-to-end.
        ///
        /// Note the deliberate absence of try/catch around the whole body: C#
        /// iterators cannot yield inside a try that has a catch clause. Instead each
        /// fallible non-yielding section is wrapped individually, and a failure sets
        /// `failure` and falls through to a single cleanup path. This is the standard
        /// shape for error handling in a Unity coroutine.
        /// </summary>
        private IEnumerator ProcessTask(ExposureTask task)
        {
            Debug.Log($"[Worker] START {task}");
            var taskClock = Stopwatch.StartNew();
            string failure = null;
            long rowsWritten = 0;

            // ---- 1. Load this shard's geometry -----------------------------
            try
            {
                _combiner.LoadShard(_queue.Connection, task);
            }
            catch (Exception e)
            {
                failure = "LoadShard: " + e.Message;
            }

            if (failure == null && _combiner.SampleCount == 0)
            {
                // Not an error: a shard can legitimately be empty if shard_count
                // exceeds the number of edges. Complete it so the run can finish.
                Debug.LogWarning($"[Worker] task#{task.TaskId} shard is empty; completing with 0 rows.");
                SafeComplete(task, 0, 0);
                _combiner.Reset();
                yield break;
            }

            // ---- 2. Create staging tables ----------------------------------
            //
            // Inside the same transaction as the COPY below, which is what lets
            // wal_level=minimal skip WAL for the bulk of the data. See
            // 03_bulk_load_tuning.sql for the exact rule being exploited.
            NpgsqlTransaction tx = null;
            if (failure == null)
            {
                try
                {
                    tx = _queue.Connection.BeginTransaction();
                    ExecScalar($"SELECT meo_create_staging_edges({task.TaskId});", tx);
                    if (task.EmitRaw)
                        ExecScalar($"SELECT meo_create_staging_samples({task.TaskId});", tx);
                }
                catch (Exception e)
                {
                    failure = "staging setup: " + e.Message;
                    SafeRollback(ref tx);
                }
            }

            // ---- 3. Sweep simulated time -----------------------------------
            if (failure == null)
            {
                float nextHeartbeat = Time.realtimeSinceStartup + _cfg.HeartbeatSeconds;
                int stepIndex = 0;

                for (int minute = task.StartMinute; minute <= task.EndMinute; minute += task.StepMinute)
                {
                    if (_shutdownRequested)
                    {
                        failure = "SIGTERM received mid-task";
                        break;
                    }
                    if (_queue.LeaseLost)
                    {
                        failure = "lease lost mid-task (task reassigned)";
                        break;
                    }
                    if (taskClock.Elapsed.TotalSeconds > _cfg.TaskTimeoutSeconds)
                    {
                        failure = $"task exceeded SUNLIT_TASK_TIMEOUT_SECONDS ({_cfg.TaskTimeoutSeconds}s)";
                        break;
                    }

                    // Point the sun. Elevation/azimuth come from the pre-baked
                    // ephemeris, interpolated to the exact minute.
                    DateTime ts = task.SimDate.AddMinutes(minute);
                    var (azimuth, elevation) = solarLoader.GetPositionLerped(ts, 0f);
                    sunLight.transform.rotation = Quaternion.Euler(elevation, azimuth + 180f, 0f);

                    // One physics tick so the new light transform is visible to
                    // Physics.Raycast. Without this the first raycast of each step
                    // would use the previous step's sun direction.
                    yield return new WaitForFixedUpdate();

                    // The accumulate call itself cannot throw usefully (it is pure
                    // arithmetic + raycasts), so it is not individually wrapped.
                    _combiner.AccumulateTimestep(stepIndex, ts, sunLight, task.EmitRaw);
                    stepIndex++;

                    // Bounded raw buffer. Flushing mid-sweep is what keeps peak RSS
                    // flat even for a full raw run.
                    if (task.EmitRaw && _combiner.RawBufferFull)
                    {
                        try
                        {
                            rowsWritten += _combiner.FlushRaw(_queue.Connection, task.TaskId);
                        }
                        catch (Exception e)
                        {
                            failure = "FlushRaw: " + e.Message;
                            break;
                        }
                    }

                    // Heartbeat on a wall-clock schedule, not a step count: step cost
                    // varies ~50x between midnight and noon, so a step-based interval
                    // would heartbeat far too rarely at the expensive end of the day.
                    if (Time.realtimeSinceStartup >= nextHeartbeat)
                    {
                        nextHeartbeat = Time.realtimeSinceStartup + _cfg.HeartbeatSeconds;
                        _queue.Heartbeat(task.TaskId, _combiner.RaycastsDone);

                        if (_cfg.VerboseLogging)
                        {
                            float pct = 100f * stepIndex / Math.Max(1, task.StepCount);
                            Debug.Log($"[Worker] task#{task.TaskId} {pct:F1}% " +
                                      $"({stepIndex}/{task.StepCount} steps, " +
                                      $"{_combiner.RaycastsDone:N0} raycasts, " +
                                      $"{taskClock.Elapsed.TotalSeconds:F0}s)");
                        }
                    }
                }
            }

            // ---- 4. Final flush + promote ----------------------------------
            if (failure == null)
            {
                try
                {
                    if (task.EmitRaw)
                        rowsWritten += _combiner.FlushRaw(_queue.Connection, task.TaskId);

                    rowsWritten += _combiner.FlushEdgeAggregate(
                        _queue.Connection, task.TaskId, task.SimDate,
                        task.StartMinute, task.StepMinute);

                    // Move staged rows into the partitioned tables. Idempotent: it
                    // deletes this task's prior output first, so a retry replaces
                    // rather than duplicates.
                    using (var cmd = new NpgsqlCommand(
                        "SELECT meo_promote_staging(@task, @date, @raw);", _queue.Connection, tx))
                    {
                        cmd.Parameters.AddWithValue("task", task.TaskId);
                        cmd.Parameters.AddWithValue("date", task.SimDate.Date);
                        cmd.Parameters.AddWithValue("raw", task.EmitRaw);
                        cmd.CommandTimeout = 0;
                        cmd.ExecuteScalar();
                    }

                    tx.Commit();
                    tx.Dispose();
                    tx = null;
                }
                catch (Exception e)
                {
                    failure = "flush/promote: " + e.Message;
                    SafeRollback(ref tx);
                }
            }
            else
            {
                SafeRollback(ref tx);
            }

            // ---- 5. Report -------------------------------------------------
            if (failure == null)
            {
                long sunlit = _combiner.TotalSunlit();
                long ceiling = (long)_combiner.SampleCount * task.StepCount;

                // Invariant: you cannot have more sunlit observations than
                // observations. A breach means the accumulator indexing is wrong.
                if (sunlit > ceiling)
                {
                    failure = $"INVARIANT VIOLATION: sunlit={sunlit:N0} exceeds " +
                              $"samples x steps={ceiling:N0}. Accumulator indexing is broken.";
                    Debug.LogError("[Worker] " + failure);
                    _queue.Fail(task.TaskId, failure);
                }
                else
                {
                    double secs = taskClock.Elapsed.TotalSeconds;
                    double rate = _combiner.RaycastsDone / Math.Max(0.001, secs);
                    _tasksCompleted++;
                    _totalRaycasts += _combiner.RaycastsDone;

                    SafeComplete(task, rowsWritten, _combiner.RaycastsDone);

                    Debug.Log($"[Worker] DONE task#{task.TaskId} in {secs:F1}s | " +
                              $"{_combiner.RaycastsDone:N0} raycasts ({rate / 1000.0:F1}k/s) | " +
                              $"{rowsWritten:N0} rows | " +
                              $"sunlit {100.0 * sunlit / Math.Max(1, ceiling):F1}%");
                }
            }
            else
            {
                Debug.LogError($"[Worker] FAILED task#{task.TaskId}: {failure}");
                _queue.Fail(task.TaskId, failure);
            }

            _combiner.Reset();
        }

        // =====================================================================
        // HELPERS
        // =====================================================================

        private void ExecScalar(string sql, NpgsqlTransaction tx)
        {
            using var cmd = new NpgsqlCommand(sql, _queue.Connection, tx);
            cmd.CommandTimeout = 0;
            cmd.ExecuteScalar();
        }

        private void SafeRollback(ref NpgsqlTransaction tx)
        {
            if (tx == null) return;
            try { tx.Rollback(); }
            catch (Exception e) { Debug.LogWarning($"[Worker] rollback failed: {e.Message}"); }
            finally { tx.Dispose(); tx = null; }
        }

        private void SafeComplete(ExposureTask task, long rows, long rays)
        {
            try { _queue.Complete(task.TaskId, rows, rays); }
            catch (Exception e)
            {
                // The work is committed; only the bookkeeping failed. The lease will
                // expire and the task will be retried, and because promote is
                // idempotent the retry simply overwrites identical data.
                Debug.LogWarning($"[Worker] could not mark task#{task.TaskId} complete: {e.Message}");
            }
        }

        /// <summary>
        /// Kubernetes sends SIGTERM on scale-down, eviction and spot reclamation.
        /// Unity surfaces it as OnApplicationQuit. We flag it and let the loop
        /// unwind so the lease is released promptly, instead of the task sitting
        /// unclaimable until its lease expires.
        /// </summary>
        private void OnApplicationQuit()
        {
            if (!_shutdownRequested)
                Debug.Log("[Worker] SIGTERM / quit received — finishing current step and releasing lease.");
            _shutdownRequested = true;
        }

        private void Fatal(string what, Exception e)
        {
            Debug.LogError($"[Worker] FATAL: {what}" + (e != null ? $"\n{e}" : ""));
            Shutdown(1);
        }

        private void Shutdown(int exitCode)
        {
            Debug.Log($"[Worker] shutting down: {_tasksCompleted} task(s), " +
                      $"{_totalRaycasts:N0} raycasts, uptime {_uptime.Elapsed.TotalMinutes:F1} min, " +
                      $"exit={exitCode}");
            _queue?.Dispose();

            // Explicit exit code is how the Kubernetes Job distinguishes a drained
            // worker (0 -> completion) from a broken one (non-zero -> retry/backoff).
            Application.Quit(exitCode);
        }

        private static string BannerText() =>
            "\n" +
            "  ┌────────────────────────────────────────────────────┐\n" +
            "  │  SunlightCity — distributed exposure worker        │\n" +
            "  │  headless Unity · map-side combiner · lease queue  │\n" +
            "  └────────────────────────────────────────────────────┘";
    }
}
