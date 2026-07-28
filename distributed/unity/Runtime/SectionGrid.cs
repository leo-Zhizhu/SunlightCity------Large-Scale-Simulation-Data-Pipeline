using System;
using UnityEngine;

namespace SunlightCity.Distributed
{
    /// <summary>
    /// Maps Unity world coordinates to integer section ids.
    ///
    /// THIS IS A CONTRACT, NOT A CONVENIENCE
    /// -------------------------------------
    /// Three implementations compute section ids and they must agree bit for bit:
    ///
    ///   * SQL    — meo_section_id()      in db/01_cluster_topology.sql
    ///   * Python — SectionGrid.section_id in orchestrator/cluster.py
    ///   * C#     — this file
    ///
    /// If they ever disagree by one, a worker writes its rows into a neighbouring
    /// section's partition. Nothing fails. The data is simply wrong, and it stays
    /// wrong until somebody notices a street with two contradictory exposure
    /// profiles — by which point the run is finished and the cause is a
    /// three-line formula in three languages.
    ///
    /// So the formula uses only floor() and integer arithmetic. No rounding mode
    /// to differ between languages, no float comparison, no culture-sensitive
    /// parse. The parameters are not defaults in code: they are read from the
    /// coordinator's meo_grid row and pinned into meo_runs.config, and
    /// <see cref="WorkQueueClient.VerifyRunCompatibility"/> refuses to start a
    /// worker whose grid disagrees with the run's.
    /// </summary>
    public readonly struct SectionGrid : IEquatable<SectionGrid>
    {
        /// <summary>World X of the grid's west edge. A multiple of <see cref="Size"/>.</summary>
        public readonly double OriginX;

        /// <summary>World Z of the grid's south edge. A multiple of <see cref="Size"/>.</summary>
        public readonly double OriginZ;

        /// <summary>Section edge length in world units (metres).</summary>
        public readonly double Size;

        /// <summary>
        /// Row stride in the id formula. Fixed at the Hilbert lattice side (128)
        /// rather than at the city's actual width, so ids stay stable when the
        /// graph is re-extracted with a slightly different extent.
        /// </summary>
        public readonly int Cols;

        public SectionGrid(double originX, double originZ, double size, int cols)
        {
            if (size <= 0.0)
                throw new ArgumentOutOfRangeException(nameof(size), "section size must be positive");
            if (cols <= 0)
                throw new ArgumentOutOfRangeException(nameof(cols), "cols must be positive");

            OriginX = originX;
            OriginZ = originZ;
            Size    = size;
            Cols    = cols;
        }

        // ---------------------------------------------------------------------
        // THE FORMULA. Mirrors:
        //   floor((z - origin_z) / size) * cols + floor((x - origin_x) / size)
        //
        // Double precision throughout, matching PostgreSQL's DOUBLE PRECISION and
        // Python's float. Deliberately NOT float: a Unity world coordinate near
        // 20,000 has ~2 mm of float32 resolution, and a sample sitting within
        // 2 mm of a section boundary would land on either side depending on which
        // language did the arithmetic. Double gives ~4 nm at that magnitude,
        // which is far below any coordinate the mesh can express.
        // ---------------------------------------------------------------------

        public int ColOf(double worldX) => (int)Math.Floor((worldX - OriginX) / Size);

        public int RowOf(double worldZ) => (int)Math.Floor((worldZ - OriginZ) / Size);

        public int SectionId(double worldX, double worldZ)
            => RowOf(worldZ) * Cols + ColOf(worldX);

        /// <summary>Convenience for Unity positions. Note it is X and Z — Y is elevation.</summary>
        public int SectionId(Vector3 worldPosition)
            => SectionId(worldPosition.x, worldPosition.z);

        public int ColFromId(int sectionId) => sectionId % Cols;
        public int RowFromId(int sectionId) => sectionId / Cols;

        /// <summary>Centre of a section in world space, at the given elevation.</summary>
        public Vector3 Centre(int sectionId, float elevation)
        {
            int c = ColFromId(sectionId);
            int r = RowFromId(sectionId);
            return new Vector3(
                (float)(OriginX + (c + 0.5) * Size),
                elevation,
                (float)(OriginZ + (r + 0.5) * Size));
        }

        /// <summary>
        /// Axis-aligned world bounds of a section, expanded by <paramref name="halo"/>.
        ///
        /// The halo is the exact bound on how far outside a section geometry can
        /// still cast a shadow into it: a building of height H reaches
        /// H / tan(theta) horizontally at sun elevation theta, and the worker's
        /// horizon guard means theta is never below its threshold. See
        /// <see cref="WorkerConfig.ShadowHaloMetres"/>.
        ///
        /// Used for logging and for the working-set assertion, not for culling —
        /// the worker holds the whole city mesh, so seam correctness is automatic
        /// rather than something the halo has to enforce.
        /// </summary>
        public Bounds SectionBounds(int sectionId, float elevation, float halo = 0f)
        {
            Vector3 centre = Centre(sectionId, elevation);
            float extent = (float)Size + 2f * halo;
            return new Bounds(centre, new Vector3(extent, 1f, extent));
        }

        public override string ToString() =>
            $"grid origin=({OriginX:0.###}, {OriginZ:0.###}) size={Size:0.###} cols={Cols}";

        // ---- Equality, used by the run-compatibility check ------------------
        //
        // Exact comparison, not epsilon. Two grids that differ at all are two
        // different datasets, and "close enough" is precisely the judgement that
        // would let a half-redeployed fleet write inconsistent output.
        public bool Equals(SectionGrid other) =>
            OriginX == other.OriginX && OriginZ == other.OriginZ &&
            Size == other.Size && Cols == other.Cols;

        public override bool Equals(object obj) => obj is SectionGrid g && Equals(g);

        public override int GetHashCode() =>
            OriginX.GetHashCode() ^ OriginZ.GetHashCode() ^ Size.GetHashCode() ^ Cols;
    }
}
