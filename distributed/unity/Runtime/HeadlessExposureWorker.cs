using System;
using System.Collections;
using System.Diagnostics;
using System.Linq;
using System.Runtime;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// Entry point for the headless Linux worker. One instance per pod.
    ///
    /// LIFECYCLE
    /// ---------
    ///   boot -> validate config -> connect coordinator -> verify run
    ///        -> load section->shard routing
    ///        -> loop { claim (with affinity) | drain-exit }
    ///                 load section geometry (cached)
    ///                 sweep the window's timesteps, batched
    ///                 hand the result to the writer thread
    ///                 reap whatever the writer finished, complete those tasks
    ///        -> drain the writer -> exit 0
    ///
    /// THE PIPELINE IS THE POINT
    /// -------------------------
    /// Raycasting a window takes ~1.8 s and writing its 261k rows takes ~1.3 s. Run
    /// in sequence that is 3.1 s per task and 42% of the fleet's life is spent
    /// waiting on sockets. So a finished window is handed to
    /// <see cref="ExposureWriter"/>'s thread and the main thread immediately claims
    /// the next task — which is why a task is COMPLETED one iteration after it is
    /// computed, and why the loop below reaps completions separately from producing
    /// them.
    ///
    /// A task is only marked done once its rows are committed. Marking it earlier
    /// would let a crash between the two leave a task recorded as complete with no
    /// data — the one failure this design must not have, because the completeness
    /// check would pass and the gap would surface months later as a street with no
    /// shade.
    ///
    /// WHY A COROUTINE AND NOT A PLAIN LOOP
    /// ------------------------------------
    /// The sun light's transform must be committed before a raycast observes it, and
    /// yielding WaitForFixedUpdate is the supported way to guarantee that ordering.
    /// It also keeps the player responsive to SIGTERM, which matters for graceful
    /// preemption on spot nodes.
    /// </summary>
    public sealed class HeadlessExposureWorker : MonoBehaviour
    {
        [Header("Scene wiring (auto-resolved if left empty)")]
        [SerializeField] private Light sunLight;
        [SerializeField] private SolarDataLoader solarLoader;

        private WorkerConfig           _cfg;
        private WorkQueueClient        _queue;
        private ShardRouter            _router;
        private SectionGeometryCache   _geometry;
        private SectionExposureSampler _sampler;
        private ExposureWriter         _writer;

        // Affinity hints: what working set is currently warm.
        private int _warmSection = -1;
        private int _warmWindow  = -1;

        private int  _consecutiveEmptyPolls;
        private int  _tasksComputed;
        private int  _tasksCompleted;
        private int  _tasksFailed;
        private long _totalRaycasts;
        private readonly Stopwatch _uptime = Stopwatch.StartNew();

        /// <summary>
        /// Set by SIGTERM handling. Kubernetes sends SIGTERM then waits
        /// terminationGracePeriodSeconds before SIGKILL; we use that window to finish
        /// the current timestep, drain the writer, and release leases cleanly so
        /// tasks are immediately reclaimable rather than waiting out a 900 s lease.
        /// </summary>
        private volatile bool _shutdownRequested;

        private const string ReadyMarkerPath = "/tmp/sunlit-ready";

        // =====================================================================
        // BOOT
        // =====================================================================

        private void Start()
        {
            // Headless determinism: no rendering, and a fixed timestep so
            // WaitForFixedUpdate advances predictably regardless of host speed.
            Application.runInBackground = true;
            Time.fixedDeltaTime = 0.02f;

            // SustainedLowLatency asks the runtime to avoid blocking generation-2
            // collections. The sampler is allocation-free in its steady state, so
            // there should be nothing to collect at all — this is the belt to that
            // braces, and it costs nothing when there is no garbage. Server GC and
            // concurrent-GC-off are set via DOTNET_* env in the ConfigMap, because
            // they must be in place before the runtime starts.
            try { GCSettings.LatencyMode = GCLatencyMode.SustainedLowLatency; }
            catch (Exception e) { Debug.LogWarning($"[Worker] could not set GC latency mode: {e.Message}"); }

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

                _router = new ShardRouter(_cfg);
                _router.LoadRouting(_queue.Connection);
            }
            catch (Exception e)
            {
                Fatal("could not join run", e);
                return;
            }

            try
            {
                _geometry = new SectionGeometryCache(_cfg);
                _sampler  = new SectionExposureSampler(_cfg);
                _writer   = new ExposureWriter(_cfg, _router);
            }
            catch (Exception e)
            {
                Fatal("could not allocate worker buffers", e);
                return;
            }

            // Readiness marker for the Job's startupProbe. Written only after the
            // scene, the solar data, the coordinator connection and every persistent
            // buffer are all in place — so the probe distinguishes "still booting the
            // engine and building the BVH", which legitimately takes tens of seconds
            // on a cold page cache, from "hung".
            TouchReadyMarker();

            StartCoroutine(WorkerLoop());
        }

        private void TouchReadyMarker()
        {
            try
            {
                System.IO.File.WriteAllText(
                    ReadyMarkerPath,
                    $"{_cfg.WorkerId}\t{_cfg.RunId}\t{DateTime.UtcNow:O}\n");
                Debug.Log($"[Worker] readiness marker written to {ReadyMarkerPath}");
            }
            catch (Exception e)
            {
                // Non-fatal: the marker is an observability aid, not a correctness
                // requirement. Losing it only means the startupProbe never passes,
                // which the operator sees as a pod stuck in Running-not-Ready.
                Debug.LogWarning($"[Worker] could not write readiness marker: {e.Message}");
            }
        }

        /// <summary>
        /// Finds and sanity-checks everything the raycast loop depends on. Failing
        /// here rather than mid-task turns a subtle wrong-data bug into an obvious
        /// startup crash.
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

            // Assert colliders actually exist. A headless build shipped without baked
            // MeshColliders raycasts against nothing and would cheerfully report the
            // entire city as sunlit — the worst possible failure, because it is silent
            // and the data looks plausible.
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

            // The whole city mesh is held, not a section's worth — which is what makes
            // seam correctness automatic rather than something the sharding has to
            // enforce. What sectioning buys is ray COHERENCE: every ray in a task
            // starts inside the same square kilometre, so the traversal stays within a
            // working set bounded by the section plus its shadow halo.
            Debug.Log($"[Worker] whole-city collider set held; per-task BVH working set is " +
                      $"bounded by section + {_cfg.ShadowHaloMetres:F0} m halo");
            return true;
        }

        // =====================================================================
        // MAIN LOOP
        // =====================================================================

        private IEnumerator WorkerLoop()
        {
            while (!_shutdownRequested)
            {
                // Reap first: a task the writer finished during the last window can be
                // marked done before we go looking for more work, which releases its
                // admission slot on the shard as early as possible.
                ReapWriterCompletions();

                ExposureTask task = null;
                try
                {
                    task = _queue.TryClaim(_warmSection, _warmWindow);
                }
                catch (Exception e)
                {
                    // A claim failure is usually a transient coordinator blip. Back off
                    // and retry rather than killing a pod that could still do work.
                    Debug.LogWarning($"[Worker] claim failed, backing off: {e.Message}");
                    yield return new WaitForSecondsRealtime(_cfg.EmptyQueuePollSeconds);
                    continue;
                }

                if (task == null)
                {
                    // An empty claim means one of two very different things: the run is
                    // finished, or every shard is at its admission cap and this worker
                    // must wait its turn for database capacity. The second is normal, so
                    // we require several consecutive empties before exiting.
                    _consecutiveEmptyPolls++;

                    // Anything the writer still owes must be reported before we consider
                    // the queue truly dry.
                    if (_writer.Busy)
                    {
                        _writer.Drain(_cfg.TaskTimeoutSeconds * 1000);
                        ReapWriterCompletions();
                        _consecutiveEmptyPolls = 0;
                        continue;
                    }

                    if (_consecutiveEmptyPolls >= _cfg.MaxEmptyPolls)
                    {
                        Debug.Log($"[Worker] no claimable task for {_consecutiveEmptyPolls} " +
                                  "consecutive polls and nothing in flight — draining and exiting 0.");
                        break;
                    }

                    Debug.Log($"[Worker] nothing claimable ({_consecutiveEmptyPolls}/" +
                              $"{_cfg.MaxEmptyPolls}) — queue empty or every shard at its " +
                              $"admission cap. Sleeping {_cfg.EmptyQueuePollSeconds}s.");
                    yield return new WaitForSecondsRealtime(_cfg.EmptyQueuePollSeconds);
                    continue;
                }

                _consecutiveEmptyPolls = 0;
                yield return ProcessTask(task);
            }

            // Drain before exiting: abandoning a committed-but-unreported task would
            // leave the coordinator to expire its lease and re-run work that already
            // landed.
            if (_writer.Busy)
            {
                Debug.Log("[Worker] draining the writer before exit …");
                _writer.Drain(_cfg.TaskTimeoutSeconds * 1000);
            }
            ReapWriterCompletions();

            Shutdown(0);
        }

        /// <summary>
        /// Marks done (or failed) every task the writer thread has finished since the
        /// last check. Non-blocking.
        /// </summary>
        private void ReapWriterCompletions()
        {
            while (_writer.TryReapCompleted(out long taskId, out long rows, out long raycasts,
                                            out double seconds, out string error))
            {
                if (error == null)
                {
                    // raycasts is carried through the payload rather than passed as 0:
                    // meo_complete_task stores what it is given, so a 0 here would wipe
                    // the value the heartbeats had been reporting and make the run's
                    // throughput read as no work done.
                    try { _queue.Complete(taskId, rows, raycasts); }
                    catch (Exception e)
                    {
                        // The work is committed; only the bookkeeping failed. The lease
                        // expires, the task is retried, and because the retry rebuilds the
                        // same leaf from scratch it simply overwrites identical data.
                        Debug.LogWarning($"[Worker] could not mark task#{taskId} complete: {e.Message}");
                    }
                    _tasksCompleted++;
                    Debug.Log($"[Worker] WROTE task#{taskId}: {rows:N0} rows in {seconds:F2}s " +
                              $"({rows / Math.Max(0.001, seconds) / 1000.0:F0}k rows/s)");
                }
                else
                {
                    _tasksFailed++;
                    _queue.Fail(taskId, "flush: " + error);
                }
            }
        }

        /// <summary>
        /// Computes one task and hands it to the writer.
        ///
        /// Note the deliberate absence of try/catch around the whole body: C#
        /// iterators cannot yield inside a try that has a catch clause. Instead each
        /// fallible non-yielding section is wrapped individually and a failure sets
        /// `failure`, falling through to a single reporting path. This is the standard
        /// shape for error handling in a Unity coroutine.
        /// </summary>
        private IEnumerator ProcessTask(ExposureTask task)
        {
            Debug.Log($"[Worker] START {task}");
            var taskClock = Stopwatch.StartNew();
            string failure = null;

            // ---- 1. Section geometry (usually already warm) -----------------
            SectionGeometry geometry = null;
            try
            {
                geometry = _geometry.Load(_router.ReaderConnection(task.SectionId), task.SectionId);
                _sampler.BeginWindow(geometry, task.StepCount);
            }
            catch (Exception e)
            {
                failure = "geometry: " + e.Message;
            }

            if (failure == null && geometry.Count == 0)
            {
                // Not an error: a section can legitimately be empty if the topology was
                // rebuilt after the tasks were planned. Complete it so the run can finish.
                Debug.LogWarning($"[Worker] task#{task.TaskId} section {task.SectionId} is " +
                                 "empty; completing with 0 rows.");
                try { _queue.Complete(task.TaskId, 0, 0); } catch { /* lease will recover it */ }
                _warmSection = task.SectionId;
                _warmWindow  = task.WindowIndex;
                yield break;
            }

            // ---- 2. Sweep the window ---------------------------------------
            if (failure == null)
            {
                float nextHeartbeat = Time.realtimeSinceStartup + _cfg.HeartbeatSeconds;

                for (int step = 0; step < task.StepCount; step++)
                {
                    if (_shutdownRequested) { failure = "SIGTERM received mid-task"; break; }
                    if (_queue.LeaseLost)   { failure = "lease lost mid-task (task reassigned)"; break; }
                    if (taskClock.Elapsed.TotalSeconds > _cfg.TaskTimeoutSeconds)
                    {
                        failure = $"task exceeded SUNLIT_TASK_TIMEOUT_SECONDS " +
                                  $"({_cfg.TaskTimeoutSeconds}s)";
                        break;
                    }

                    // Point the sun. Elevation and azimuth come from the pre-baked
                    // ephemeris, interpolated to the exact minute.
                    DateTime ts = task.WindowStart.AddMinutes(step * task.StepMinute);
                    var (azimuth, elevation) = solarLoader.GetPositionLerped(ts, 0f);
                    sunLight.transform.rotation = Quaternion.Euler(elevation, azimuth + 180f, 0f);

                    // One physics tick so the new light transform is visible to the
                    // raycast batch. Without it the first rays of each step would use the
                    // previous step's sun direction.
                    yield return new WaitForFixedUpdate();

                    // Pure arithmetic plus a job-system batch; nothing here throws
                    // usefully, so it is not individually wrapped.
                    _sampler.AccumulateTimestep(step, sunLight);

                    // Heartbeat on a wall-clock schedule, not a step count: step cost
                    // varies by ~50x between a window at dawn and one at noon, so a
                    // step-based interval would heartbeat far too rarely at the expensive
                    // end of the day.
                    if (Time.realtimeSinceStartup >= nextHeartbeat)
                    {
                        nextHeartbeat = Time.realtimeSinceStartup + _cfg.HeartbeatSeconds;
                        _queue.Heartbeat(task.TaskId, _sampler.RaycastsDone);

                        if (_cfg.VerboseLogging)
                        {
                            float pct = 100f * (step + 1) / Math.Max(1, task.StepCount);
                            Debug.Log($"[Worker] task#{task.TaskId} {pct:F0}% " +
                                      $"({step + 1}/{task.StepCount} steps, " +
                                      $"{_sampler.RaycastsDone:N0} rays, " +
                                      $"{taskClock.Elapsed.TotalSeconds:F1}s)");
                        }
                    }
                }
            }

            // ---- 3. Invariant check before anything is written --------------
            if (failure == null)
            {
                long sunlit  = _sampler.TotalSunlit();
                long ceiling = _sampler.RowCount;

                // You cannot have more sunlit observations than observations. A breach
                // means the bitset indexing is wrong, which is the most dangerous class
                // of bug here because the output would still look like exposure data.
                if (sunlit > ceiling)
                    failure = $"INVARIANT VIOLATION: sunlit={sunlit:N0} exceeds " +
                              $"samples x steps={ceiling:N0}. Bitset indexing is broken.";
            }

            // ---- 4. Hand off to the writer ---------------------------------
            if (failure == null)
            {
                try
                {
                    // Blocks only if a previous flush is still in flight, which is
                    // backpressure and means the shard cannot keep up.
                    _writer.Enqueue(task, _sampler);
                }
                catch (Exception e)
                {
                    failure = "enqueue: " + e.Message;
                }
            }

            // ---- 5. Report -------------------------------------------------
            if (failure == null)
            {
                double secs = taskClock.Elapsed.TotalSeconds;
                double rate = _sampler.RaycastsDone / Math.Max(0.001, secs);
                long sunlit = _sampler.TotalSunlit();

                _tasksComputed++;
                _totalRaycasts += _sampler.RaycastsDone;

                // The steady state is supposed to be allocation-free; this makes that
                // checkable rather than aspirational.
                _sampler.AssertNoGarbageCollected();

                Debug.Log($"[Worker] COMPUTED task#{task.TaskId} in {secs:F2}s | " +
                          $"{_sampler.RaycastsDone:N0} rays ({rate / 1000.0:F0}k/s) | " +
                          $"{_sampler.StepsSkipped}/{task.StepCount} steps below horizon | " +
                          $"sunlit {100.0 * sunlit / Math.Max(1, _sampler.RowCount):F1}% | " +
                          $"flush queued");

                // Only now update the affinity hints: a failed task should not make the
                // worker ask for more of the same.
                _warmSection = task.SectionId;
                _warmWindow  = task.WindowIndex;
            }
            else
            {
                _tasksFailed++;
                Debug.LogError($"[Worker] FAILED task#{task.TaskId}: {failure}");
                _queue.Fail(task.TaskId, failure);
            }
        }

        // =====================================================================
        // SHUTDOWN
        // =====================================================================

        /// <summary>
        /// Kubernetes sends SIGTERM on scale-down, eviction and spot reclamation.
        /// Unity surfaces it as OnApplicationQuit. We flag it and let the loop unwind
        /// so leases are released promptly, instead of tasks sitting unclaimable
        /// until a 900 s lease expires.
        /// </summary>
        private void OnApplicationQuit()
        {
            if (!_shutdownRequested)
                Debug.Log("[Worker] SIGTERM / quit received — finishing the current step, " +
                          "draining the writer, releasing leases.");
            _shutdownRequested = true;
        }

        private void Fatal(string what, Exception e)
        {
            Debug.LogError($"[Worker] FATAL: {what}" + (e != null ? $"\n{e}" : ""));
            Shutdown(1);
        }

        private void Shutdown(int exitCode)
        {
            double mins = _uptime.Elapsed.TotalMinutes;
            Debug.Log(
                $"[Worker] shutting down: {_tasksComputed} computed / {_tasksCompleted} committed / " +
                $"{_tasksFailed} failed, {_totalRaycasts:N0} rays, " +
                $"{_writer?.TotalRowsWritten ?? 0:N0} rows, " +
                $"geometry cache {_geometry?.HitRate:P0} hit, " +
                $"{_router?.Reconnects ?? 0} shard reconnect(s), " +
                $"uptime {mins:F1} min, exit={exitCode}");

            // Order matters: the writer thread must stop before its connection is
            // disposed, and the sampler's native buffers must be released explicitly
            // because the GC does not own them.
            _writer?.Dispose();
            _router?.Dispose();
            _sampler?.Dispose();
            _queue?.Dispose();

            // Explicit exit code is how the Kubernetes Job distinguishes a drained
            // worker (0 -> completion) from a broken one (non-zero -> retry/backoff).
            Application.Quit(exitCode);
        }

        /// <summary>
        /// Last-resort teardown. OnApplicationQuit already runs Shutdown on the normal
        /// path; this covers a domain reload in the Editor, where skipping it would
        /// leak the persistent NativeArrays until the process exits.
        /// </summary>
        private void OnDestroy()
        {
            _writer?.Dispose();
            _sampler?.Dispose();
        }

        private static string BannerText() =>
            "\n" +
            "  ┌────────────────────────────────────────────────────────┐\n" +
            "  │  SunlightCity — distributed exposure worker            │\n" +
            "  │  headless Unity · batched raycasts · sharded PostGIS   │\n" +
            "  └────────────────────────────────────────────────────────┘";
    }
}
