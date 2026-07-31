using System;
using System.Diagnostics;
using System.Threading;
using Npgsql;
using NpgsqlTypes;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// Everything one finished window needs in order to be written, detached from
    /// the sampler so the sampler can immediately start refilling its own buffers.
    ///
    /// The bitset is COPIED in here rather than referenced, and that copy is what
    /// buys the overlap: ~33 KB of memmove against the ~1.3 s of COPY it lets run
    /// concurrently with the next task's raycasting.
    ///
    /// ONE of these, not two. The obvious design double-buffers the payload, but
    /// copy-on-enqueue already provides the second buffer — the sampler's own bitset
    /// is free to be overwritten the instant Enqueue returns, because its contents
    /// are already here. A second payload would only help if two flushes could be in
    /// flight, and they deliberately cannot: with write time below raycast time the
    /// writer is never the constraint, so a queued second window would only ever mean
    /// the shard has stalled — and then applying backpressure is the right response,
    /// not buffering deeper.
    /// </summary>
    internal sealed class WindowPayload
    {
        public long   TaskId;
        public int    SectionId;
        public int    WindowIndex;
        public DateTime WindowStart;    // leaf lower bound, inclusive
        public DateTime WindowEnd;      // leaf upper bound, exclusive
        public int    StepMinute;
        public int    StepCount;
        public int    SampleCount;
        /// <summary>
        /// Carried across the handoff because the task is completed on the
        /// coordinator AFTER the flush, and meo_complete_task records whatever it is
        /// given. Passing 0 here would zero out raycasts_done for every task and make
        /// the run's throughput report read as no work done at all.
        /// </summary>
        public long   Raycasts;

        public readonly Guid[]  Ids;    // capacity-sized, reused
        public readonly ulong[] Bits;   // capacity-sized, reused

        public WindowPayload(int capacity, int maxSteps)
        {
            Ids  = new Guid[capacity];
            Bits = new ulong[((capacity * maxSteps) + 63) / 64];
        }

        public long RowCount => (long)SampleCount * StepCount;
    }

    /// <summary>
    /// Streams a finished window into its own partition leaf on the owning shard.
    ///
    /// This class exists for two reasons that are worth separating.
    ///
    ///
    /// 1. BINARY COPY INSTEAD OF CSV
    /// -----------------------------
    /// v1 built a CSV line per row: an interpolated string containing a
    /// 36-character UUID, a 19-character timestamp and "true"/"false". About 52
    /// bytes on the wire, one heap-allocated string per row, and a text parse on
    /// the server for every field.
    ///
    /// Binary COPY writes the UUID as its 16 raw bytes, the timestamp as an int64
    /// of microseconds, the boolean as one byte — ~30 bytes on the wire, no client
    /// allocation, and no server-side parse at all. At 7.89 billion rows that is
    /// ~170 GB less network traffic and ~7.89 billion strings never created.
    ///
    ///
    /// 2. THE FLUSH RUNS ON A BACKGROUND THREAD
    /// ---------------------------------------
    /// Raycasting a window takes ~0.88 s. One task's 261k rows take ~1.30 s down a
    /// SINGLE COPY stream, which is what a WROTE log line reports — but two streams
    /// alternate, so the amortised write cost is ~0.65 s per task. Done in sequence
    /// that is ~1.53 s per task and the fleet spends 43% of its life waiting on
    /// sockets. So the flush is handed to a writer thread on a SECOND
    /// connection while the main thread claims the next task and starts raycasting
    /// — which is exactly why the capacity model gives each worker two COPY streams
    /// and why the map phase costs max(raycast, write) rather than their sum.
    ///
    /// Strictly ONE flush in flight, enforced by a single handoff slot. Deeper
    /// pipelining would need N payload buffers for a bound that is already met:
    /// with write time below raycast time the writer is never the constraint, so a
    /// second queued payload would only ever mean the shard has stalled — and in
    /// that case applying backpressure is the correct response, not buffering more.
    ///
    /// WHY THIS DOES NOT BREAK THE WAL-SKIP
    /// -----------------------------------
    /// The leaf must be created and COPYed into inside ONE transaction for
    /// PostgreSQL to skip WAL. That whole transaction happens on the writer
    /// thread's own connection — begin, create, copy, attach, commit — so it is
    /// self-contained and the main thread's connection is never involved. Two
    /// threads, two connections, no shared transaction. (Npgsql connections are not
    /// thread-safe; this design never shares one.)
    /// </summary>
    public sealed class ExposureWriter : IDisposable
    {
        private readonly ShardRouter _router;
        private readonly WindowPayload _payload;

        // ---- Handoff --------------------------------------------------------
        private readonly object _gate = new object();
        private WindowPayload _pending;          // waiting to be written
        private Thread _thread;
        private volatile bool _stop;

        // ---- Outcome of the last completed flush ----------------------------
        private long   _doneTaskId  = -1;
        private long   _doneRows;
        private long   _doneRaycasts;
        private double _doneSeconds;
        private string _doneError;

        public long TotalRowsWritten { get; private set; }
        public double TotalWriteSeconds { get; private set; }

        public ExposureWriter(WorkerConfig cfg, ShardRouter router)
        {
            if (cfg == null) throw new ArgumentNullException(nameof(cfg));
            _router = router ?? throw new ArgumentNullException(nameof(router));

            // cfg is used for sizing only and deliberately not retained: the payload is
            // allocated here, once, and nothing later in this class needs config.
            _payload = new WindowPayload(cfg.MaxSectionSamples, cfg.MaxStepsPerWindow);

            _thread = new Thread(WriterLoop)
            {
                IsBackground = true,   // never keeps the player alive on quit
                Name = "sunlit-exposure-writer",
            };
            _thread.Start();
        }

        /// <summary>True when a flush is in flight and Enqueue would block.</summary>
        public bool Busy { get { lock (_gate) return _pending != null; } }

        // =====================================================================
        // MAIN THREAD SIDE
        // =====================================================================

        /// <summary>
        /// Copies the sampler's results into a payload and queues them.
        ///
        /// Blocks if a previous flush is still in flight — which is backpressure,
        /// and is the right behaviour: it means the shard cannot keep up, and
        /// racing further ahead would only deepen the queue. In the balanced
        /// deployment (9 shards, 35% ingest headroom) this never blocks.
        /// </summary>
        public void Enqueue(ExposureTask task, SectionExposureSampler sampler)
        {
            // Wait for the single handoff slot. Note this is about the SLOT, not about
            // the buffer: the writer thread reads _payload, so it must be finished
            // with it before we overwrite it, and both conditions are the same wait.
            lock (_gate)
            {
                while (_pending != null && !_stop)
                    Monitor.Wait(_gate);
            }

            WindowPayload p = _payload;
            p.TaskId      = task.TaskId;
            p.SectionId   = task.SectionId;
            p.WindowIndex = task.WindowIndex;
            p.WindowStart = task.WindowStart;
            p.WindowEnd   = task.WindowEnd;
            p.StepMinute  = task.StepMinute;
            p.StepCount   = sampler.StepCount;
            p.SampleCount = sampler.SampleCount;
            p.Raycasts    = sampler.RaycastsDone;

            // Block copies rather than element loops: Array.Copy is a memmove, and
            // this is the one point where the main thread does work proportional to
            // the payload — 33 KB, against the ~1.3 s of flush it decouples.
            Array.Copy(sampler.SampleIds, p.Ids, sampler.SampleCount);
            int words = ((sampler.SampleCount * sampler.StepCount) + 63) / 64;
            Array.Copy(sampler.Bits, p.Bits, words);

            lock (_gate)
            {
                _pending = p;
                Monitor.PulseAll(_gate);
            }
        }

        /// <summary>
        /// Non-blocking poll for a finished flush.
        ///
        /// Returns false if nothing has completed since the last call. On true,
        /// <paramref name="error"/> is null on success — the caller then marks the
        /// task done on the coordinator, which must happen only AFTER the rows are
        /// committed, or a crash in between would leave a task recorded as complete
        /// with no data.
        /// </summary>
        public bool TryReapCompleted(out long taskId, out long rows, out long raycasts,
                                     out double seconds, out string error)
        {
            lock (_gate)
            {
                if (_doneTaskId < 0)
                {
                    taskId = -1; rows = 0; raycasts = 0; seconds = 0; error = null;
                    return false;
                }

                taskId   = _doneTaskId;
                rows     = _doneRows;
                raycasts = _doneRaycasts;
                seconds  = _doneSeconds;
                error    = _doneError;

                _doneTaskId = -1;
                _doneError  = null;
                return true;
            }
        }

        /// <summary>
        /// Waits for any in-flight flush to finish. Called before exiting, and on
        /// SIGTERM: abandoning a committed-but-unreported task would leave the
        /// coordinator to expire its lease and re-run work that already landed.
        /// </summary>
        public bool Drain(int timeoutMs = 120000)
        {
            var clock = Stopwatch.StartNew();
            lock (_gate)
            {
                while (_pending != null && clock.ElapsedMilliseconds < timeoutMs)
                    Monitor.Wait(_gate, 250);
                return _pending == null;
            }
        }

        // =====================================================================
        // WRITER THREAD SIDE
        // =====================================================================

        private void WriterLoop()
        {
            while (!_stop)
            {
                WindowPayload p;
                lock (_gate)
                {
                    while (_pending == null && !_stop)
                        Monitor.Wait(_gate, 250);
                    if (_stop) return;
                    p = _pending;
                }

                var clock = Stopwatch.StartNew();
                string error = null;
                long rows = 0;

                try
                {
                    rows = WritePayload(p);
                }
                catch (Exception e)
                {
                    // Reported back to the main thread rather than thrown: an
                    // exception escaping a background thread in a Unity player
                    // terminates the process, which would lose the other task's
                    // progress too. The main thread fails the task on the
                    // coordinator and carries on.
                    error = e.Message;
                    Debug.LogError($"[Writer] task#{p.TaskId} flush failed: {e}");
                }

                lock (_gate)
                {
                    _doneTaskId   = p.TaskId;
                    _doneRows     = rows;
                    _doneRaycasts = p.Raycasts;
                    _doneSeconds  = clock.Elapsed.TotalSeconds;
                    _doneError    = error;

                    if (error == null)
                    {
                        TotalRowsWritten  += rows;
                        TotalWriteSeconds += clock.Elapsed.TotalSeconds;
                    }

                    _pending = null;
                    Monitor.PulseAll(_gate);
                }
            }
        }

        /// <summary>
        /// The whole critical section, on the writer thread's own connection:
        ///
        ///     BEGIN
        ///       meo_begin_leaf   -> CREATE TABLE (standalone, no parent lock)
        ///       COPY ... BINARY, FREEZE
        ///       meo_attach_leaf  -> ATTACH PARTITION (brief lock, no validation)
        ///     COMMIT
        ///
        /// One transaction is not a style choice. PostgreSQL skips WAL for a COPY
        /// only when the target relation was created in the SAME transaction, so
        /// splitting this would silently reintroduce ~500 GB of WAL across the
        /// cluster. FREEZE has the same requirement, and it is why the tuples land
        /// already visible to everyone — no hint-bit write on first read, no
        /// freeze-vacuum of 7.89 billion rows ever.
        /// </summary>
        private long WritePayload(WindowPayload p)
        {
            NpgsqlConnection conn = _router.WriterConnection(p.SectionId);

            // Retry only on the FIRST attempt's behalf: a leaf left behind by a
            // previous attempt must be detached and dropped before it can be
            // rebuilt, and that takes ACCESS EXCLUSIVE on the parent — so it runs in
            // its own short transaction, outside the long one below.
            using (var reset = new NpgsqlCommand(
                "SELECT meo_reset_leaf(@section, @lo, @window);", conn))
            {
                reset.Parameters.AddWithValue("section", p.SectionId);
                reset.Parameters.AddWithValue("lo", p.WindowStart);
                reset.Parameters.AddWithValue("window", p.WindowIndex);
                reset.CommandTimeout = 0;
                reset.ExecuteScalar();
            }

            long rows = 0;

            using (NpgsqlTransaction tx = conn.BeginTransaction())
            {
                string leaf;
                using (var begin = new NpgsqlCommand(
                    "SELECT meo_begin_leaf(@section, @lo, @hi, @window, @task);", conn, tx))
                {
                    begin.Parameters.AddWithValue("section", p.SectionId);
                    begin.Parameters.AddWithValue("lo", p.WindowStart);
                    begin.Parameters.AddWithValue("hi", p.WindowEnd);
                    begin.Parameters.AddWithValue("window", p.WindowIndex);
                    begin.Parameters.AddWithValue("task", p.TaskId);
                    begin.CommandTimeout = 0;
                    leaf = (string)begin.ExecuteScalar();
                }

                // The leaf name comes from a server-side function over integers and a
                // timestamp, so it cannot contain anything needing quoting — but it is
                // still interpolated into DDL, so the shape is worth stating: the
                // value is derived, never client-supplied.
                string copySql =
                    $"COPY {leaf} (sample_point_id, datetime, is_sunlit, section_id, task_id) " +
                    "FROM STDIN (FORMAT BINARY, FREEZE)";

                using (var writer = conn.BeginBinaryImport(copySql))
                {
                    // STEP-MAJOR, matching the sampler's bitset layout. Rows therefore
                    // arrive grouped by datetime — the leaf's range key — so the heap
                    // ends up physically clustered on the column queries filter by,
                    // for free, without a CLUSTER pass.
                    for (int step = 0; step < p.StepCount; step++)
                    {
                        DateTime ts = p.WindowStart.AddMinutes(step * p.StepMinute);
                        int baseBit = step * p.SampleCount;

                        for (int i = 0; i < p.SampleCount; i++)
                        {
                            int bit = baseBit + i;
                            bool sunlit = (p.Bits[bit >> 6] & (1UL << (bit & 63))) != 0UL;

                            writer.StartRow();
                            writer.Write(p.Ids[i],    NpgsqlDbType.Uuid);
                            writer.Write(ts,          NpgsqlDbType.Timestamp);
                            writer.Write(sunlit,      NpgsqlDbType.Boolean);
                            writer.Write(p.SectionId, NpgsqlDbType.Integer);
                            writer.Write(p.TaskId,    NpgsqlDbType.Bigint);
                            rows++;
                        }
                    }

                    // MANDATORY. Without Complete() the importer's Dispose treats the
                    // import as cancelled and the rows are silently discarded — the
                    // classic binary-COPY bug, and it presents as an empty leaf with
                    // no error anywhere.
                    writer.Complete();
                }

                using (var attach = new NpgsqlCommand(
                    "SELECT meo_attach_leaf(@section, @lo, @hi, @window);", conn, tx))
                {
                    attach.Parameters.AddWithValue("section", p.SectionId);
                    attach.Parameters.AddWithValue("lo", p.WindowStart);
                    attach.Parameters.AddWithValue("hi", p.WindowEnd);
                    attach.Parameters.AddWithValue("window", p.WindowIndex);
                    attach.CommandTimeout = 0;
                    attach.ExecuteScalar();
                }

                tx.Commit();
            }

            return rows;
        }

        // =====================================================================

        public void Dispose()
        {
            lock (_gate)
            {
                _stop = true;
                Monitor.PulseAll(_gate);
            }

            // Bounded join: a writer wedged on a dead socket must not stop the pod
            // from exiting, because Kubernetes will SIGKILL it anyway and the lease
            // will recover the task.
            try { _thread?.Join(5000); } catch { /* shutting down */ }
            _thread = null;
        }
    }
}
