using System;
using System.Collections.Generic;
using System.Linq;
using VMS.TPS.Common.Model.API;
using VMS.TPS.Common.Model.Types;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Reads the structure set back out of Eclipse: the missing half of the
    /// two-run QA workflow.
    ///
    /// Until now this plugin only ever *wrote* contours. Run 2 needs the opposite
    /// direction — the planner's edited structures, shipped up as the "after" so
    /// the server can score them against the AI baseline. This is the mirror of
    /// <see cref="EsapiStructureImporter"/> and deliberately mirrors its shape,
    /// including the thread discipline and the one-structure-at-a-time error
    /// isolation.
    ///
    /// Cost, and why it is paid this way
    /// ---------------------------------
    /// ESAPI exposes no z-extent for a structure, and no "give me every contour"
    /// call. The only way to read geometry is
    /// <c>GetContoursOnImagePlane(z)</c>, one slice at a time, so reading N
    /// structures over a Z-slice series is N x Z calls: twenty structures on a
    /// 200-slice CT is 4,000 calls. Two things keep that acceptable:
    ///
    /// * Empty structures are skipped outright via <c>Structure.IsEmpty</c>, which
    ///   is one property read instead of Z calls. In a real structure set a good
    ///   fraction of rows are empty placeholders.
    /// * Progress is reported per structure, so the panel stays honest about a
    ///   read that takes a few seconds rather than appearing hung.
    ///
    /// <c>MeshGeometry.Bounds</c> would narrow the z range, but building the mesh
    /// is itself expensive and it is not obviously a win; if this ever becomes a
    /// bottleneck, measure that before adding it.
    ///
    /// Defensive property reads
    /// ------------------------
    /// Nearly every property here is wrapped. ESAPI throws from property getters
    /// in states that are perfectly normal in a clinic — <c>Volume</c> on a
    /// structure with no segment, <c>GetNumberOfSeparateParts()</c> on odd
    /// geometry, approval history on an object the user cannot see. One awkward
    /// structure must not abandon the read of the other nineteen, because a
    /// partial snapshot is still a useful one and a thrown exception is not.
    /// </summary>
    internal sealed class EsapiStructureReader
    {
        /// <summary>Fewer points than this is not a polygon; ESAPI can return them.</summary>
        private const int MinPointsPerContour = 3;

        private readonly StructureSet _structureSet;
        private readonly IThreadGate _gate;

        public EsapiStructureReader(StructureSet structureSet, IThreadGate gate)
        {
            if (structureSet == null) throw new ArgumentNullException("structureSet");
            if (gate == null) throw new ArgumentNullException("gate");

            _structureSet = structureSet;
            _gate = gate;
        }

        /// <summary>
        /// Every structure's identity and status, without touching contours.
        ///
        /// Cheap — no per-slice calls — so this is what the panel uses to populate
        /// auto-detect the moment it opens. The expensive geometry read only
        /// happens when a snapshot is actually being sent.
        /// </summary>
        public IList<StructureAutoDetect.Candidate> ReadCandidates()
        {
            _gate.AssertOnEsapiThread("StructureSet.Structures");

            var candidates = new List<StructureAutoDetect.Candidate>();
            foreach (Structure structure in Enumerate())
            {
                candidates.Add(new StructureAutoDetect.Candidate
                {
                    ExistingId = Safe(() => structure.Id, null),
                    DicomType = Safe(() => structure.DicomType, null),
                    VolumeCc = Safe(() => structure.Volume, 0.0),
                    IsEmpty = Safe(() => structure.IsEmpty, true),
                    IsApproved = Safe(() => structure.IsApproved, false),
                });
            }
            return candidates;
        }

        /// <summary>
        /// Read the full snapshot: identity, status and contour geometry.
        ///
        /// <paramref name="onlyIds"/> restricts the read to specific structure ids
        /// — run 2 only needs the ones a baseline exists for, so there is no reason
        /// to pay for the rest. Null reads everything non-empty.
        ///
        /// <paramref name="progress"/> is called as (done, total).
        /// </summary>
        public StructureSnapshot ReadSnapshot(
            Geometry geometry,
            ModelCatalog catalog,
            ICollection<string> onlyIds,
            string role,
            Action<int, int> progress)
        {
            _gate.AssertOnEsapiThread("Structure.GetContoursOnImagePlane");

            int zSize = geometry != null ? geometry.ZSize : 0;

            var wanted = Enumerate()
                .Where(s => onlyIds == null || IsWanted(s, onlyIds))
                .ToList();

            var structures = new List<SnapshotStructure>();
            for (int i = 0; i < wanted.Count; i++)
            {
                Structure structure = wanted[i];
                try
                {
                    SnapshotStructure read = ReadOne(structure, zSize, catalog);
                    if (read != null) structures.Add(read);
                }
                catch (Exception)
                {
                    // One unreadable structure does not invalidate the snapshot.
                    // Deliberately swallowed rather than surfaced per-structure:
                    // the server compares by id, so an absent structure simply has
                    // no "after" and is reported as not_comparable, which is the
                    // honest outcome.
                }

                if (progress != null) progress(i + 1, wanted.Count);
            }

            var snapshot = new StructureSnapshot
            {
                Schema = StructureSnapshot.CurrentSchema,
                Role = role,
                Geometry = geometry,
                StructureSetUid = Safe(() => _structureSet.UID, null),
                Structures = structures,
            };
            snapshot.StructureSetSha256 = snapshot.ComputeContentHash();
            return snapshot;
        }

        private SnapshotStructure ReadOne(Structure structure, int zSize, ModelCatalog catalog)
        {
            string id = Safe(() => structure.Id, null);
            if (string.IsNullOrWhiteSpace(id)) return null;

            bool isEmpty = Safe(() => structure.IsEmpty, true);
            CatalogStructure matched = catalog != null ? catalog.Resolve(id) : null;

            var read = new SnapshotStructure
            {
                Id = id,
                Name = Safe(() => structure.Name, null),
                DicomType = Safe(() => structure.DicomType, null),
                RoiNumber = Safe(() => structure.ROINumber, 0),
                StructureId = matched != null ? matched.Id : null,
                IsEmpty = isEmpty,
                IsHighResolution = Safe(() => structure.IsHighResolution, false),
                IsApproved = Safe(() => structure.IsApproved, false),
                LastModifiedBy = Safe(() => structure.HistoryUserDisplayName, null),
                LastModifiedAt = SafeNullable(() => structure.HistoryDateTime),
                StructureCodes = ReadCodes(structure),
                Contours = new List<ContourSlice>(),
            };

            // Volume throws on a structure with no segment in some releases, so it
            // is read only where it is meaningful.
            read.VolumeCc = isEmpty ? (double?)null : SafeNullable(() => structure.Volume);

            if (isEmpty)
            {
                // An emptied structure is the strongest QA signal there is: the
                // planner rejected the AI contour outright. It has no geometry to
                // measure, so it is recorded and the slice scan is skipped.
                return read;
            }

            read.SeparateParts = SafeNullable(() => structure.GetNumberOfSeparateParts());

            for (int z = 0; z < zSize; z++)
            {
                VVector[][] rings;
                try
                {
                    rings = structure.GetContoursOnImagePlane(z);
                }
                catch (Exception)
                {
                    continue;
                }
                if (rings == null) continue;

                foreach (VVector[] ring in rings)
                {
                    if (ring == null || ring.Length < MinPointsPerContour) continue;

                    var points = new List<double[]>(ring.Length);
                    foreach (VVector point in ring)
                    {
                        points.Add(new[] { point.x, point.y, point.z });
                    }
                    read.Contours.Add(new ContourSlice
                    {
                        ZIndex = z,
                        PointsLps = points,
                    });
                }
            }

            return read;
        }

        private List<string> ReadCodes(Structure structure)
        {
            try
            {
                var codes = new List<string>();
                IEnumerable<StructureCodeInfo> infos = structure.StructureCodeInfos;
                if (infos == null) return null;

                foreach (StructureCodeInfo info in infos)
                {
                    string code = Safe(() => info.Code, null);
                    if (!string.IsNullOrWhiteSpace(code)) codes.Add(code);
                }
                return codes.Count > 0 ? codes : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private IEnumerable<Structure> Enumerate()
        {
            IEnumerable<Structure> structures;
            try
            {
                structures = _structureSet.Structures;
            }
            catch (Exception)
            {
                return new List<Structure>();
            }
            return structures ?? new List<Structure>();
        }

        private static bool IsWanted(Structure structure, ICollection<string> ids)
        {
            string id;
            try { id = structure.Id; }
            catch (Exception) { return false; }

            if (string.IsNullOrEmpty(id)) return false;
            foreach (string wanted in ids)
            {
                if (string.Equals(wanted, id, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        // --- defensive reads ------------------------------------------------- //

        private static T Safe<T>(Func<T> read, T fallback)
        {
            try { return read(); }
            catch (Exception) { return fallback; }
        }

        private static T? SafeNullable<T>(Func<T> read) where T : struct
        {
            try { return read(); }
            catch (Exception) { return null; }
        }
    }
}
