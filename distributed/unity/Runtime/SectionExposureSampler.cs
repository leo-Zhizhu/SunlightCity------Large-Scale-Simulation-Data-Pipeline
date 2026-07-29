using System;
using Unity.Collections;
using Unity.Jobs;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// Evaluates sun exposure for every sample point in one section across one
    /// 3-hour window, and holds the result as a bit per (timestep, sample).
    ///
    /// This class is where the per-worker speedup lives. Three things, in order of
    /// how much they matter.
    ///
    ///
    /// 1. BATCHED RAYCASTS INSTEAD OF A SERIAL LOOP  (~3.0x on 8 vCPU)
    /// --------------------------------------------------------------
    /// v1 called Physics.Raycast() once per sample, on Unity's main thread, 1.58
    /// billion times. Physics.Raycast is main-thread only, so that loop used one
    /// core no matter how many the machine had.
    ///
    /// RaycastCommand.ScheduleBatch hands a whole timestep's rays to the job
    /// system, which spreads them across worker threads. The measured gain on an
    /// 8 vCPU pod is 3.0x rather than 8x, for two structural reasons: the main
    /// thread still builds the command array, completes the job and folds the
    /// results, and BVH traversal saturates memory bandwidth long before it
    /// saturates ALUs. Both are real ceilings, not tuning opportunities.
    ///
    ///
    /// 2. ZERO ALLOCATION IN THE STEADY STATE  (no GC pauses at all)
    /// ------------------------------------------------------------
    /// Every buffer is allocated ONCE at worker startup with Allocator.Persistent,
    /// sized to the largest section the config permits, and reused for every task
    /// for the pod's whole life. A task uses a PREFIX of each buffer rather than a
    /// right-sized allocation.
    ///
    /// The consequence is that a pod's RSS is flat and the GC never runs during
    /// raycasting — which matters more than the allocation cost itself. A
    /// generation-0 collection mid-window would stall the job system's worker
    /// threads together, and the resulting pause would show up as a heartbeat gap
    /// on a 900 s lease. <see cref="AssertNoGarbageCollected"/> makes the claim
    /// checkable rather than aspirational: it compares GC.CollectionCount before
    /// and after a window and logs if anything moved.
    ///
    /// v1 allocated, per timestep, a List of Tuple&lt;Guid, string, bool&gt; and an
    /// interpolated string per sample. That is ~7.89 billion strings and ~4.7
    /// billion heap objects over a run, all of it garbage.
    ///
    ///
    /// 3. RESULTS AS A BITSET  (1 bit per observation instead of 1 byte)
    /// ---------------------------------------------------------------
    /// is_sunlit is one bit of information, so it is stored as one bit. A whole
    /// window for the largest permitted section is
    /// 16,384 x 60 / 8 = 123 KB; the actual Manhattan case is ~33 KB. Small enough
    /// that the writer can hold TWO of them and flush one while the sampler fills
    /// the other — which is what lets database writes overlap raycasting instead of
    /// serialising behind it. See <see cref="ExposureWriter"/>.
    ///
    /// Layout is STEP-MAJOR: bit (step * sampleCount + sample). Two reasons.
    /// Accumulation writes it sequentially, one contiguous run per timestep. And
    /// the writer then emits rows grouped by datetime, which is the partition
    /// leaf's range key — so rows arrive physically clustered by the column they
    /// are most often filtered on.
    /// </summary>
    public sealed class SectionExposureSampler : IDisposable
    {
        private readonly WorkerConfig _cfg;

        // ---- Persistent, allocated once at boot -----------------------------
        // Capacity, not size. A task fills a prefix.
        private readonly int _capacity;
        private readonly int _maxSteps;

        private NativeArray<RaycastCommand> _commands;
        private NativeArray<RaycastHit>     _hits;
        // Ray origins are invariant across a window — only the direction changes —
        // so they are computed once per task and reused for all 60 timesteps.
        private NativeArray<Vector3>        _origins;

        // Managed, but equally reused: one allocation for the pod's lifetime.
        private readonly Guid[] _sampleIds;
        private readonly ulong[] _bits;

        // ---- Per-task state -------------------------------------------------
        private int _sampleCount;
        private int _stepCount;

        public int  SampleCount  => _sampleCount;
        public int  StepCount    => _stepCount;
        public long RaycastsDone { get; private set; }
        /// <summary>Timesteps skipped by the horizon guard. Cheap progress signal.</summary>
        public int  StepsSkipped { get; private set; }

        /// <summary>Sample ids for this task, valid for indices [0, SampleCount).</summary>
        public Guid[] SampleIds => _sampleIds;

        /// <summary>Result bitset. Valid for bits [0, SampleCount * StepCount).</summary>
        public ulong[] Bits => _bits;

        private int _gcBaseline;

        public SectionExposureSampler(WorkerConfig cfg)
        {
            _cfg      = cfg ?? throw new ArgumentNullException(nameof(cfg));
            _capacity = cfg.MaxSectionSamples;
            _maxSteps = cfg.MaxStepsPerWindow;

            // Allocator.Persistent, not TempJob: TempJob is asserted to live at most
            // 4 frames and Unity logs a leak warning past that. These outlive
            // thousands of frames by design.
            _commands = new NativeArray<RaycastCommand>(_capacity, Allocator.Persistent,
                                                        NativeArrayOptions.UninitializedMemory);
            _hits     = new NativeArray<RaycastHit>(_capacity, Allocator.Persistent,
                                                    NativeArrayOptions.UninitializedMemory);
            _origins  = new NativeArray<Vector3>(_capacity, Allocator.Persistent,
                                                 NativeArrayOptions.UninitializedMemory);

            _sampleIds = new Guid[_capacity];
            _bits      = new ulong[BitWords(_capacity, _maxSteps)];

            long bytes = (long)_capacity * (40 + 40 + 12)          // native arrays
                       + (long)_capacity * 16                       // Guid[]
                       + (long)_bits.Length * 8;                    // bitset
            Debug.Log($"[Sampler] persistent buffers: capacity {_capacity:N0} samples " +
                      $"x {_maxSteps} steps, {bytes / 1024.0 / 1024.0:F1} MB, " +
                      "allocated once for the life of the pod");
        }

        private static int BitWords(int samples, int steps)
            => ((samples * steps) + 63) / 64;

        // =====================================================================
        // LOAD
        // =====================================================================

        /// <summary>
        /// Prepares the sampler for one task from the section's sample geometry.
        ///
        /// The caller supplies positions and ids; this class does not touch the
        /// database. That separation is what makes it testable and what lets the
        /// geometry be cached across the twelve tasks that share a section — see
        /// <see cref="SectionGeometryCache"/>.
        /// </summary>
        public void BeginWindow(SectionGeometry geometry, int stepCount)
        {
            if (geometry == null) throw new ArgumentNullException(nameof(geometry));

            if (geometry.Count > _capacity)
                throw new InvalidOperationException(
                    $"section {geometry.SectionId} has {geometry.Count:N0} sample points but " +
                    $"SUNLIT_MAX_SECTION_SAMPLES is {_capacity:N0}. Raise it (the buffers are " +
                    "sized from it at boot) or use smaller sections.");

            if (stepCount > _maxSteps)
                throw new InvalidOperationException(
                    $"window has {stepCount} timesteps but SUNLIT_MAX_STEPS_PER_WINDOW " +
                    $"is {_maxSteps}.");

            _sampleCount = geometry.Count;
            _stepCount   = stepCount;
            RaycastsDone = 0;
            StepsSkipped = 0;

            // Ray origins, computed once for the whole window.
            //
            // Lifted 3 m so the road surface never shadows its own sample — the
            // same offset v1 used, kept identical so the two pipelines produce
            // comparable numbers.
            float lift = _cfg.RayOriginLift;
            for (int i = 0; i < _sampleCount; i++)
            {
                Vector3 p = geometry.Positions[i];
                _origins[i] = new Vector3(p.x, p.y + lift, p.z);
                _sampleIds[i] = geometry.Ids[i];
            }

            // Clear only the words this task will use, not all 123 KB.
            int words = BitWords(_sampleCount, _stepCount);
            Array.Clear(_bits, 0, words);

            _gcBaseline = GC.CollectionCount(0);
        }

        // =====================================================================
        // ACCUMULATE
        // =====================================================================

        /// <summary>
        /// Evaluates every sample at one timestep and records the result.
        ///
        /// <paramref name="stepIndex"/> is the dense index within the window, which
        /// is what the bitset is keyed on. Passing it in rather than deriving it
        /// from a clock keeps this a pure function of its inputs and therefore
        /// trivially replayable.
        /// </summary>
        public void AccumulateTimestep(int stepIndex, Light sun)
        {
            if (_sampleCount == 0) return;

            // Hoisted: at 4,400 iterations per call and 360 calls per section-day, a
            // property access inside the loop is measurable.
            float   elevation = sun.transform.eulerAngles.x;
            Vector3 toSun     = -sun.transform.forward;

            // HORIZON GUARD — a correctness fix, not an optimisation.
            //
            // Near sunrise or sunset a ray must cross kilometres of city. Float
            // precision degrades over that distance and the ray can miss the mesh
            // entirely, falsely reporting SUNLIT — the worst possible error, because
            // it is silent and the data looks plausible. Declaring these steps
            // shadowed is both cheaper and closer to the truth.
            //
            // eulerAngles is [0, 360), so a sun 10 deg below the horizon reads as
            // 350: the `>= 180 - threshold` test covers the whole below-horizon arc
            // as well as dusk, while `<= threshold` covers dawn.
            //
            // It is also what makes the shadow halo an EXACT bound rather than a
            // heuristic — see WorkerConfig.ShadowHaloMetres — and it is why a
            // December window costs far less than a June one, which is the cost
            // spread the planner's LPT ordering exploits.
            float threshold = _cfg.SunAngleThreshold;
            if (elevation <= threshold || elevation >= 180f - threshold)
            {
                // Bits stay 0, which is 'shadowed'. Nothing to write, nothing to
                // clear — the buffer was cleared in BeginWindow.
                StepsSkipped++;
                return;
            }

            // ---- Build the batch ---------------------------------------------
            var queryParams = new QueryParameters(
                layerMask: _cfg.ShadowCasterMask | _cfg.GroundBlockerMask,
                hitMultipleFaces: false,
                // Ignore, not UseGlobal: whether a trigger blocks sunlight should not
                // depend on a project-wide physics setting that someone might change
                // for unrelated reasons.
                hitTriggers: QueryTriggerInteraction.Ignore,
                // Backfaces off. A building's interior faces point away from us, and
                // counting them would make a ray that starts inside geometry report
                // a hit at zero distance.
                hitBackfaces: false);

            float maxDistance = _cfg.MaxRayDistance;
            for (int i = 0; i < _sampleCount; i++)
                _commands[i] = new RaycastCommand(_origins[i], toSun, queryParams, maxDistance);

            // ---- Dispatch ----------------------------------------------------
            // maxHits: 1 — we only need to know WHETHER anything blocks the sun, not
            // what or how many. Asking for more would make the results array
            // strided and cost proportionally more traversal.
            //
            // minCommandsPerJob: 64 — small enough that 4,400 rays spread across all
            // worker threads, large enough that per-job scheduling overhead stays
            // negligible. At the default of 1 the job system would create thousands
            // of jobs per timestep and spend more time scheduling than raycasting.
            JobHandle handle = RaycastCommand.ScheduleBatch(
                _commands.GetSubArray(0, _sampleCount),
                _hits.GetSubArray(0, _sampleCount),
                minCommandsPerJob: _cfg.MinCommandsPerJob,
                maxHits: 1);
            handle.Complete();

            // ---- Fold into the bitset ----------------------------------------
            int baseBit = stepIndex * _sampleCount;
            for (int i = 0; i < _sampleCount; i++)
            {
                // colliderInstanceID == 0 means "no hit", and it is the cheap test.
                //
                // The obvious `_hits[i].collider == null` costs an instance-id to
                // managed-object lookup per sample — 4,400 dictionary probes per
                // timestep, 7.89 billion over a run — purely to compare the result
                // against null. Reading the raw id skips all of it.
                bool sunlit = _hits[i].colliderInstanceID == 0;

                if (sunlit)
                {
                    int bit = baseBit + i;
                    _bits[bit >> 6] |= 1UL << (bit & 63);
                }
            }

            RaycastsDone += _sampleCount;
        }

        // =====================================================================
        // RESULTS
        // =====================================================================

        /// <summary>Reads one observation. Bounds-checked by the caller's loop shape.</summary>
        public bool IsSunlit(int stepIndex, int sampleIndex)
        {
            int bit = stepIndex * _sampleCount + sampleIndex;
            return (_bits[bit >> 6] & (1UL << (bit & 63))) != 0UL;
        }

        /// <summary>
        /// Total sunlit observations in the window, by popcount over the bitset.
        ///
        /// A whole-window total in ~4,100 word operations instead of 264,000 bit
        /// tests. Used for the invariant check and the log line, both of which run
        /// once per task, so this is about keeping a diagnostic honest-cheap rather
        /// than about the diagnostic being hot.
        /// </summary>
        public long TotalSunlit()
        {
            int words = BitWords(_sampleCount, _stepCount);
            long total = 0;
            for (int w = 0; w < words; w++)
                total += PopCount(_bits[w]);
            return total;
        }

        /// <summary>
        /// SWAR population count. System.Numerics.BitOperations.PopCount would be
        /// one instruction, but it is .NET Core 3.0+ and Unity's IL2CPP runtime
        /// targets netstandard2.1 without it. This is the standard fallback and
        /// compiles to about a dozen ALU ops.
        /// </summary>
        private static int PopCount(ulong v)
        {
            v -= (v >> 1) & 0x5555555555555555UL;
            v = (v & 0x3333333333333333UL) + ((v >> 2) & 0x3333333333333333UL);
            v = (v + (v >> 4)) & 0x0F0F0F0F0F0F0F0FUL;
            return (int)((v * 0x0101010101010101UL) >> 56);
        }

        /// <summary>Number of rows this window will emit.</summary>
        public long RowCount => (long)_sampleCount * _stepCount;

        /// <summary>
        /// Verifies the no-allocation claim for the window just finished.
        ///
        /// Returns the number of generation-0 collections that happened during it.
        /// Anything but zero means something in the hot path started allocating —
        /// which is worth knowing immediately, because the symptom otherwise shows
        /// up as unexplained heartbeat gaps under load rather than as a memory
        /// problem.
        /// </summary>
        public int AssertNoGarbageCollected()
        {
            int collections = GC.CollectionCount(0) - _gcBaseline;
            if (collections > 0)
                Debug.LogWarning(
                    $"[Sampler] {collections} gen-0 collection(s) during a window. The " +
                    "raycast path is supposed to be allocation-free; something in it is " +
                    "allocating. Expect heartbeat jitter under load.");
            return collections;
        }

        // =====================================================================
        // TEARDOWN
        // =====================================================================

        /// <summary>
        /// Releases the native buffers. Mandatory: NativeArray is unmanaged memory
        /// and the GC will not reclaim it. Unity's leak detector reports it at
        /// domain reload in the Editor, but in a headless player a missed Dispose
        /// is simply a leak for the life of the process.
        /// </summary>
        public void Dispose()
        {
            if (_commands.IsCreated) _commands.Dispose();
            if (_hits.IsCreated)     _hits.Dispose();
            if (_origins.IsCreated)  _origins.Dispose();
        }
    }
}
