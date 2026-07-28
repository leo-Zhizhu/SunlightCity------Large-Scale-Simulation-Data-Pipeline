using System;
using System.Collections.Generic;
using Npgsql;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>Where one data shard lives.</summary>
    public readonly struct ShardEndpoint
    {
        public readonly int    Index;
        public readonly string Host;
        public readonly int    Port;
        public readonly string Database;

        public ShardEndpoint(int index, string host, int port, string database)
        {
            Index = index; Host = host; Port = port; Database = database;
        }

        public override string ToString() => $"shard {Index} ({Host}:{Port}/{Database})";
    }

    /// <summary>
    /// Resolves "my section" to "my database instance", and owns this worker's two
    /// connections to it.
    ///
    ///
    /// WHY TWO CONNECTIONS, AND EXACTLY TWO
    /// ------------------------------------
    ///   * a READER, used by the main thread to load section geometry
    ///   * a WRITER, used by the writer thread to COPY finished windows
    ///
    /// Two because the flush must overlap the next task's raycasting (see
    /// <see cref="ExposureWriter"/>), and Npgsql connections are not thread-safe so
    /// the two threads cannot share one. Not more than two because the capacity
    /// model budgets exactly two streams per worker: five workers per shard x two
    /// streams = ten, against the ~twelve productive COPY streams a 16 vCPU
    /// instance sustains. A third connection per worker would push the cluster past
    /// what it can absorb and make ingest worse, not better.
    ///
    ///
    /// WHY THERE IS NO CONNECTION POOLER HERE
    /// -------------------------------------
    /// Workers reach the COORDINATOR through PgBouncer — thousands of tiny claim
    /// and heartbeat transactions from 50 clients is exactly what a transaction
    /// pooler is for. They reach the SHARDS directly, and that is deliberate:
    ///
    ///   A pooler in a sustained bulk-COPY path is a single-threaded process
    ///   relaying every byte. At the fleet's ~700 MB/s it would become the
    ///   bottleneck the cluster was built to remove.
    ///
    /// And it would be solving nothing. Pooling exists to multiplex many clients
    /// onto few backends; sharding already keeps each instance at ten backends.
    ///
    ///
    /// THREADING
    /// ---------
    /// The routing map is built once at boot and never mutated, so both threads read
    /// it without a lock. Beyond that the two threads share nothing: each touches
    /// only its own connection field. That is the entire concurrency argument — no
    /// lock, because there is no mutable shared state.
    /// </summary>
    public sealed class ShardRouter : IDisposable
    {
        private readonly WorkerConfig _cfg;

        // Immutable after Load(). Read from both threads without synchronisation.
        private Dictionary<int, ShardEndpoint> _sectionToShard;

        // Main thread only.
        private NpgsqlConnection _reader;
        private int _readerShard = -1;

        // Writer thread only.
        private NpgsqlConnection _writer;
        private int _writerShard = -1;

        public int Reconnects { get; private set; }

        public ShardRouter(WorkerConfig cfg)
        {
            _cfg = cfg ?? throw new ArgumentNullException(nameof(cfg));
        }

        // =====================================================================
        // BOOT
        // =====================================================================

        /// <summary>
        /// Fetches the whole section -> shard map from the coordinator, once.
        ///
        /// The map is a few hundred rows and never changes during a run, so caching
        /// it entirely removes the coordinator from the per-task path. A worker that
        /// resolved its shard on every task would add 6,048 round trips per fleet
        /// and make one small instance a dependency of every write.
        ///
        /// Shards in state 'draining' or 'offline' are excluded, which is what makes
        /// replacing an instance mid-run possible: the coordinator stops dispatching
        /// its sections and workers never learn its address.
        /// </summary>
        public void LoadRouting(NpgsqlConnection coordinator)
        {
            const string sql = @"
                SELECT section_id, shard_index, host, port, dbname
                FROM meo_section_routing
                WHERE shard_index IS NOT NULL
                  AND state = 'online';";

            var map = new Dictionary<int, ShardEndpoint>(1024);

            using (var cmd = new NpgsqlCommand(sql, coordinator))
            using (var r = cmd.ExecuteReader())
            {
                while (r.Read())
                {
                    int section = r.GetInt32(0);
                    map[section] = new ShardEndpoint(
                        r.GetInt32(1), r.GetString(2), r.GetInt32(3), r.GetString(4));
                }
            }

            if (map.Count == 0)
                throw new InvalidOperationException(
                    "meo_section_routing returned no online sections. Either plan_tasks.py " +
                    "has not assigned sections to shards yet, or every shard is marked " +
                    "'draining'/'offline' in meo_shards.");

            _sectionToShard = map;

            var shards = new HashSet<int>();
            foreach (var e in map.Values) shards.Add(e.Index);

            Debug.Log($"[Router] routing loaded: {map.Count} sections across " +
                      $"{shards.Count} online shard(s)");
        }

        public ShardEndpoint Resolve(int sectionId)
        {
            if (_sectionToShard == null)
                throw new InvalidOperationException("LoadRouting() has not been called.");

            if (!_sectionToShard.TryGetValue(sectionId, out ShardEndpoint e))
                throw new InvalidOperationException(
                    $"section {sectionId} has no online shard. The coordinator dispatched a " +
                    "task whose shard has since been taken offline — fail the task and let " +
                    "the queue re-dispatch it.");
            return e;
        }

        // =====================================================================
        // CONNECTIONS
        // =====================================================================

        /// <summary>Main-thread connection to the shard owning this section.</summary>
        public NpgsqlConnection ReaderConnection(int sectionId)
        {
            ShardEndpoint e = Resolve(sectionId);
            EnsureConnection(ref _reader, ref _readerShard, e, "reader");
            return _reader;
        }

        /// <summary>Writer-thread connection to the shard owning this section.</summary>
        public NpgsqlConnection WriterConnection(int sectionId)
        {
            ShardEndpoint e = Resolve(sectionId);
            EnsureConnection(ref _writer, ref _writerShard, e, "writer");
            return _writer;
        }

        /// <summary>
        /// Opens a connection to <paramref name="e"/>, reusing the existing one when
        /// it already points at the right shard and is still healthy.
        ///
        /// Affinity dispatch means a worker usually stays on one shard for many
        /// consecutive tasks, so this almost always short-circuits. The reconnect
        /// path exists for when affinity misses and the LPT fallback hands out a
        /// task on a different shard.
        /// </summary>
        private void EnsureConnection(ref NpgsqlConnection conn, ref int currentShard,
                                      ShardEndpoint e, string role)
        {
            if (conn != null && currentShard == e.Index &&
                conn.State == System.Data.ConnectionState.Open)
                return;

            if (conn != null)
            {
                try { conn.Close(); } catch { /* replacing it regardless */ }
                conn.Dispose();
                conn = null;
                Reconnects++;
            }

            conn = new NpgsqlConnection(BuildConnectionString(e));
            conn.Open();
            ApplySessionSettings(conn, role);
            currentShard = e.Index;

            Debug.Log($"[Router] {role} connected to {e}");
        }

        private string BuildConnectionString(ShardEndpoint e) =>
            $"Host={e.Host};Port={e.Port};Database={e.Database};" +
            $"Username={_cfg.DbUser};Password={_cfg.DbPassword};" +
            // Keepalives: a COPY of 261k rows plus the ATTACH can hold the socket for
            // seconds, and the geometry load can be quiet for minutes while the
            // sampler works. Without these a stateful firewall or load balancer
            // silently drops the connection and the next command fails with a
            // confusing "connection closed by peer".
            "Keepalive=30;Timeout=30;CommandTimeout=0;" +
            // No client-side pooling. A worker holds exactly one connection per role
            // for its whole life, so a pool would add reconnect churn — and worse, it
            // can hand back a different physical connection, which would break the
            // single-transaction requirement the WAL-skip depends on.
            "Pooling=false;" +
            // Binary transfer both ways. The COPY path is already binary; this covers
            // ordinary query results too, so the geometry load returns doubles as
            // 8 raw bytes rather than as decimal text to be parsed.
            "Binary Import Export=true;";

        private void ApplySessionSettings(NpgsqlConnection conn, string role)
        {
            // Session scope, not server config: it lets the worker be aggressive
            // without affecting the analytic clients that read the same instance.
            string sql =
                // The worker only ever writes reproducible data — a lost commit is a
                // lost task, which the queue re-runs — so an fsync per commit buys
                // nothing. Set here as well as in postgresql.shard.bulk.conf so it
                // holds even against an instance running the serving profile.
                "SET synchronous_commit = off; " +
                // Never let a stuck server-side statement outlive the lease silently;
                // the worker's own task timeout is the real bound.
                "SET statement_timeout = 0; " +
                // A transaction left open by a crashed worker holds ACCESS EXCLUSIVE on
                // nothing (the leaf is standalone until ATTACH) but it does pin the
                // xmin horizon, which blocks vacuum cluster-wide. Ten minutes is far
                // longer than any legitimate flush and far shorter than a run.
                "SET idle_in_transaction_session_timeout = '10min'; ";

            // The reader loads geometry with an ORDER BY over ~4,400 rows joined to
            // meo_edge_sections. Modest, but enough to keep the sort in memory.
            sql += role == "reader" ? "SET work_mem = '64MB';" : "SET work_mem = '32MB';";

            using var cmd = new NpgsqlCommand(sql, conn);
            cmd.ExecuteNonQuery();
        }

        // =====================================================================

        /// <summary>
        /// Closes both connections. Called on shutdown only — and the writer
        /// connection must not be disposed while the writer thread might still be
        /// using it, so <see cref="ExposureWriter.Dispose"/> runs first.
        /// </summary>
        public void Dispose()
        {
            foreach (var c in new[] { _reader, _writer })
            {
                if (c == null) continue;
                try { c.Close(); } catch { /* shutting down anyway */ }
                c.Dispose();
            }
            _reader = null; _writer = null;
            _readerShard = _writerShard = -1;
        }
    }
}
