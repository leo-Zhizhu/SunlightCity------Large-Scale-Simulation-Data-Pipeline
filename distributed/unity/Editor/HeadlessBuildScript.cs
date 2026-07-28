#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace SunlightCity.Distributed.EditorTools
{
    /// <summary>
    /// Produces the headless Linux worker binary that ships inside the container.
    ///
    /// Invoked non-interactively by docker/Dockerfile.build:
    ///
    ///   unity-editor -batchmode -nographics -quit \
    ///     -projectPath /project \
    ///     -executeMethod SunlightCity.Distributed.EditorTools.HeadlessBuildScript.BuildLinuxServer \
    ///     -logFile /dev/stdout
    ///
    /// KEY DECISION: StandaloneLinux64 with the *Server* build subtarget
    /// -----------------------------------------------------------------
    /// Unity's dedicated-server subtarget strips the renderer, audio, and input
    /// entirely — not merely disabled at runtime but excluded from the binary. That
    /// gives a smaller image, no GPU/X11/Vulkan dependency, and no risk of a driver
    /// probe on a headless node.
    ///
    /// Crucially, PhysX is UNAFFECTED. Raycasting is a CPU BVH traversal with no GPU
    /// involvement, so the measurement this whole pipeline depends on is
    /// bit-identical to a desktop build. That is the property that makes
    /// containerising this workload viable at all.
    ///
    /// The worker issues its rays through RaycastCommand.ScheduleBatch rather than
    /// Physics.Raycast, so the job system's worker threads matter as much as the
    /// main thread. Unity sizes that pool from the visible core count, which inside
    /// a container is the CGROUP quota — so a pod requesting 8 CPU gets 7 job worker
    /// threads, and one requesting 500m gets one. That is why the Job spec requests
    /// whole cores rather than a fraction: a fractional request would silently
    /// collapse the batching this pipeline's per-worker speedup depends on.
    /// </summary>
    public static class HeadlessBuildScript
    {
        private const string OutputDir  = "build/linux-server";
        private const string OutputName = "SunlightCityWorker";

        [MenuItem("Tools/Distributed/Build Linux Server (headless)")]
        public static void BuildLinuxServer()
        {
            try
            {
                Run();
            }
            catch (Exception e)
            {
                // In batchmode an uncaught exception still exits 0, which would let a
                // broken image be published. Log and force a non-zero exit.
                Console.Error.WriteLine($"[HeadlessBuild] FATAL: {e}");
                EditorApplication.Exit(1);
            }
        }

        private static void Run()
        {
            string root       = Directory.GetCurrentDirectory();
            string outputPath = Path.Combine(root, OutputDir, OutputName);
            Directory.CreateDirectory(Path.GetDirectoryName(outputPath));

            string[] scenes = ResolveScenes();
            Log($"building {scenes.Length} scene(s):");
            foreach (var s in scenes) Log($"    {s}");

            ConfigurePlayerSettings();

            var options = new BuildPlayerOptions
            {
                scenes           = scenes,
                locationPathName = outputPath,
                target           = BuildTarget.StandaloneLinux64,
                // Server subtarget: strips graphics/audio/input from the player.
                subtarget        = (int)StandaloneBuildSubtarget.Server,
                options          = BuildOptions.StrictMode,
                // StrictMode: treat any script compile error as a build failure
                // rather than silently shipping a player missing our worker code.
            };

            Log("starting BuildPipeline.BuildPlayer …");
            BuildReport report = BuildPipeline.BuildPlayer(options);
            BuildSummary summary = report.summary;

            Log($"result   : {summary.result}");
            Log($"size     : {summary.totalSize / (1024.0 * 1024.0):F1} MB");
            Log($"duration : {summary.totalTime.TotalSeconds:F0}s");
            Log($"errors   : {summary.totalErrors}, warnings: {summary.totalWarnings}");

            if (summary.result != BuildResult.Succeeded || summary.totalErrors > 0)
            {
                foreach (var step in report.steps)
                    foreach (var msg in step.messages.Where(m =>
                                 m.type == LogType.Error || m.type == LogType.Exception))
                        Console.Error.WriteLine($"[HeadlessBuild]   {step.name}: {msg.content}");

                Console.Error.WriteLine("[HeadlessBuild] BUILD FAILED");
                EditorApplication.Exit(1);
                return;
            }

            VerifyOutput(outputPath);

            Log("BUILD SUCCEEDED");
            EditorApplication.Exit(0);
        }

        /// <summary>
        /// Uses the scenes enabled in Build Settings, or falls back to discovering
        /// them so a fresh clone builds without anyone having curated the list.
        /// </summary>
        private static string[] ResolveScenes()
        {
            var enabled = EditorBuildSettings.scenes
                .Where(s => s.enabled)
                .Select(s => s.path)
                .Where(p => !string.IsNullOrEmpty(p) && File.Exists(p))
                .ToArray();

            if (enabled.Length > 0) return enabled;

            Log("no scenes enabled in Build Settings; discovering from Assets/ …");
            var found = AssetDatabase.FindAssets("t:Scene", new[] { "Assets" })
                .Select(AssetDatabase.GUIDToAssetPath)
                .OrderBy(p => p)
                .ToArray();

            if (found.Length == 0)
                throw new InvalidOperationException(
                    "No scenes found. The worker needs a scene containing the city mesh " +
                    "with baked MeshColliders, a directional Light, and a SolarDataLoader.");

            // Prefer a scene that looks like the simulation scene over, say, a menu.
            var preferred = found.FirstOrDefault(p =>
                p.IndexOf("sim", StringComparison.OrdinalIgnoreCase) >= 0 ||
                p.IndexOf("city", StringComparison.OrdinalIgnoreCase) >= 0 ||
                p.IndexOf("main", StringComparison.OrdinalIgnoreCase) >= 0);

            return new[] { preferred ?? found[0] };
        }

        /// <summary>
        /// Player settings that matter for a long-running batch container.
        /// </summary>
        private static void ConfigurePlayerSettings()
        {
            var group = BuildTargetGroup.Standalone;
            var named = UnityEditor.Build.NamedBuildTarget.Server;

            PlayerSettings.productName  = "SunlightCityWorker";
            PlayerSettings.companyName  = "SunlightCity";

            // IL2CPP over Mono: 2-3x faster on the tight loops that build the raycast
            // command array and fold results into the bitset, which is where the
            // worker's non-PhysX CPU time goes. Costs build time, not runtime.
            PlayerSettings.SetScriptingBackend(named, ScriptingImplementation.IL2CPP);
            PlayerSettings.SetIl2CppCompilerConfiguration(named, Il2CppCompilerConfiguration.Release);

            // Optimise the generated C++ for speed rather than for binary size. The
            // default balances the two, which is right for a game shipping to players
            // over a network and wrong for a batch worker whose image is pulled once
            // per node and then runs flat out for minutes.
            PlayerSettings.SetIl2CppCodeGeneration(named, Il2CppCodeGeneration.OptimizeSpeed);

            // Incremental GC OFF. It splits collection across frames by adding a write
            // barrier to every reference store — a real cost paid on every frame in
            // exchange for smoother frame times. The worker has no frame-time
            // requirement, and its hot path is deliberately allocation-free (see
            // SectionExposureSampler), so there is nothing for incremental collection
            // to smooth out and the write barriers are pure overhead.
            PlayerSettings.gcIncremental = false;

            // .NET Standard 2.1 keeps Npgsql 4.1.12 (netstandard2.0) loadable.
            PlayerSettings.SetApiCompatibilityLevel(named, ApiCompatibilityLevel.NET_Standard_2_0);

            // Managed stripping OFF. Npgsql resolves types reflectively (type
            // handlers, provider factories); IL2CPP's static analysis cannot see
            // those references and will strip them, producing a runtime
            // "type not found" the moment the worker opens a connection.
            // This is the single most common way a working Editor build becomes a
            // broken IL2CPP build.
            PlayerSettings.SetManagedStrippingLevel(named, ManagedStrippingLevel.Disabled);

            // Headless hygiene: never block on a dialog, never try to open a window.
            PlayerSettings.runInBackground = true;
            PlayerSettings.resizableWindow = false;
            PlayerSettings.fullScreenMode  = FullScreenMode.Windowed;
            PlayerSettings.defaultScreenWidth  = 64;
            PlayerSettings.defaultScreenHeight = 64;

            // Deterministic, low-overhead physics.
            //
            // autoSyncTransforms off: it forces a Transform -> PhysX sync before every
            // query, and the only transform the worker moves is the sun's — a Light,
            // which has no collider and nothing to sync. The city's colliders never
            // move at all. Leaving it on would pay for a scene-wide check 360 times
            // per section-day to synchronise nothing.
            //
            // queriesHitTriggers off as a project-wide default, though the worker does
            // not rely on it: SectionExposureSampler passes
            // QueryTriggerInteraction.Ignore explicitly, so whether a trigger blocks
            // sunlight does not depend on a global someone might change for unrelated
            // reasons.
            Physics.autoSyncTransforms = false;
            Physics.queriesHitTriggers = false;

            // Crash on an unhandled exception rather than limping on with bad state.
            PlayerSettings.SetStackTraceLogType(LogType.Error, StackTraceLogType.ScriptOnly);
            PlayerSettings.SetStackTraceLogType(LogType.Exception, StackTraceLogType.Full);

            Log("player settings: IL2CPP/Release, stripping DISABLED (Npgsql reflection), server subtarget");
        }

        /// <summary>
        /// Confirms the artifacts the container actually needs are present. A build
        /// can "succeed" yet omit StreamingAssets, which would leave every worker
        /// unable to load its ephemeris — better to fail the image build.
        /// </summary>
        private static void VerifyOutput(string outputPath)
        {
            if (!File.Exists(outputPath))
                throw new InvalidOperationException($"expected player binary at {outputPath}, not found");

            string dataDir = outputPath + "_Data";
            if (!Directory.Exists(dataDir))
                throw new InvalidOperationException($"expected data directory at {dataDir}, not found");

            string streaming = Path.Combine(dataDir, "StreamingAssets", "SolarData");
            if (!Directory.Exists(streaming))
            {
                throw new InvalidOperationException(
                    $"StreamingAssets/SolarData missing from the build at {streaming}. " +
                    "Workers load their solar ephemeris from there; generate it with " +
                    "generate_solar_positions.py and place it under Assets/StreamingAssets " +
                    "before building.");
            }

            var bins = Directory.GetFiles(streaming, "*.bin", SearchOption.AllDirectories);
            Log($"verified StreamingAssets: {bins.Length} solar binary file(s)");
            if (bins.Length == 0)
                throw new InvalidOperationException(
                    "StreamingAssets/SolarData contains no .bin files — the worker would " +
                    "start and then fail on every task.");
        }

        private static void Log(string msg) => Console.WriteLine($"[HeadlessBuild] {msg}");
    }
}
#endif
