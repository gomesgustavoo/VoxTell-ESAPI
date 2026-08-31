using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;

namespace VoxTell_Interface.Models
{
    /// <summary>
    /// The QA wire types: what the plugin ships up so a later run can be scored
    /// against it, and what comes back.
    ///
    /// The privacy rule these types encode
    /// -----------------------------------
    /// There is deliberately **no patient name, no patient id, no accession
    /// number, no DICOM UID and no study date** anywhere in this file. Identity
    /// travels only as the opaque HMAC keys from
    /// <see cref="Services.LineageKeys"/>. If a field is ever added here that
    /// could identify a patient, the product's central claim stops being true, so
    /// the absence is the design and not an oversight.
    ///
    /// The one exception is <see cref="StructureSnapshot.StructureSetUid"/>, which
    /// is an Eclipse structure-set UID rather than a DICOM instance UID: it names
    /// a *set of contours inside the TPS*, is not a DICOM identifier, and is what
    /// lets run 2 tell an edited set apart from a different set on the same
    /// series. Even so, it is optional.
    ///
    /// Geometry, not pixels
    /// --------------------
    /// A snapshot carries contours and geometry only. Metrics need those; they do
    /// not need voxels. That is what keeps a baseline at kilobytes per patient and
    /// lets it outlive the volume's 2-hour idle TTL without retaining any image.
    /// </summary>
    public sealed class StructureSnapshot
    {
        /// <summary>Wire version of the snapshot document itself.</summary>
        [JsonProperty("schema")]
        public int Schema { get; set; }

        /// <summary>HMAC of the series UID. Opaque; the lineage key.</summary>
        [JsonProperty("series_key")]
        public string SeriesKey { get; set; }

        [JsonProperty("for_key")]
        public string ForKey { get; set; }

        [JsonProperty("scanner_key")]
        public string ScannerKey { get; set; }

        /// <summary>Eclipse structure-set UID. Not a DICOM instance UID.</summary>
        [JsonProperty("structure_set_uid")]
        public string StructureSetUid { get; set; }

        /// <summary>
        /// sha256 over the canonical rendering of every structure in this
        /// snapshot — ids, DICOM types and contour coordinates. The idempotency
        /// key: reopening a patient without editing anything produces the same
        /// hash, so the server records nothing new.
        /// </summary>
        [JsonProperty("structure_set_sha256")]
        public string StructureSetSha256 { get; set; }

        /// <summary>
        /// Which run this is. <c>baseline</c> is the AI output from run 1;
        /// <c>clinical</c> is the planner's edited set read back in run 2.
        /// </summary>
        [JsonProperty("role")]
        public string Role { get; set; }

        [JsonProperty("geometry")]
        public Geometry Geometry { get; set; }

        [JsonProperty("structures")]
        public List<SnapshotStructure> Structures { get; set; }

        public const int CurrentSchema = 1;
        public const string RoleBaseline = "baseline";
        public const string RoleClinical = "clinical";

        /// <summary>
        /// Canonical sha256 of this snapshot's contents - the idempotency key.
        ///
        /// A planner reopening a patient five minutes later, having changed nothing,
        /// must not create a second baseline or a second billable comparison. So "the
        /// same structure set" has to mean the same <i>contours</i>, not merely the
        /// same names: hashing the ids alone would call a fully redrawn organ
        /// unchanged.
        ///
        /// Two details carry the weight:
        ///
        /// * <b>Coordinates are formatted to three decimals.</b> That is one micron,
        ///   far below any clinical or acquisition relevance, and it makes the hash
        ///   stable against the last-bit float differences that would otherwise make
        ///   a re-read of an untouched structure look edited.
        /// * <b>Ordering is by structure id, then slice.</b> ESAPI's enumeration order
        ///   is not a promise, and two workstations must agree on the hash for the
        ///   same set.
        ///
        /// Lives here rather than on the reader because it is a property of the wire
        /// document, which also means it can be tested without Eclipse -- see the
        /// harness's <c>--selftest</c>.
        /// </summary>
        public string ComputeContentHash()
        {
            var sb = new StringBuilder();
            sb.Append("voxtell/snapshot/v1\n");

            IEnumerable<SnapshotStructure> ordered =
                (Structures ?? new List<SnapshotStructure>())
                .OrderBy(s => s.Id, StringComparer.Ordinal);

            foreach (SnapshotStructure structure in ordered)
            {
                sb.Append(structure.Id).Append('|')
                  .Append(structure.DicomType ?? string.Empty).Append('|')
                  .Append(structure.IsEmpty ? "empty" : "filled").Append('\n');

                IEnumerable<ContourSlice> slices =
                    (structure.Contours ?? new List<ContourSlice>())
                    .OrderBy(c => c.ZIndex);

                foreach (ContourSlice slice in slices)
                {
                    sb.Append("  ").Append(
                        slice.ZIndex.ToString(CultureInfo.InvariantCulture)).Append(':');
                    foreach (double[] point in slice.PointsLps ?? new List<double[]>())
                    {
                        if (point == null || point.Length < 3) continue;
                        sb.Append(point[0].ToString("F3", CultureInfo.InvariantCulture)).Append(',')
                          .Append(point[1].ToString("F3", CultureInfo.InvariantCulture)).Append(',')
                          .Append(point[2].ToString("F3", CultureInfo.InvariantCulture)).Append(';');
                    }
                    sb.Append('\n');
                }
            }

            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(sb.ToString()));
                var hex = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash) hex.Append(b.ToString("x2"));
                return hex.ToString();
            }
        }
    }

    /// <summary>
    /// One structure as it exists in Eclipse right now.
    ///
    /// Several fields here are read from ESAPI rather than assumed, and each
    /// earns its place in the verdict:
    ///
    /// * <see cref="IsEmpty"/> plus a matching baseline entry is how a
    ///   <c>rejected</c> structure is detected — the planner deleted the AI
    ///   contour outright. That is the most valuable single QA signal and it is
    ///   invisible to any geometric metric, because there is no geometry left.
    /// * <see cref="SeparateParts"/> catches stray islands, a common auto-contour
    ///   artefact, without the server needing the mask.
    /// * <see cref="IsApproved"/>, <see cref="LastModifiedBy"/> and
    ///   <see cref="LastModifiedAt"/> come straight from Eclipse's own history.
    ///   Approval is what promotes a baseline out of <c>provisional</c> — reading
    ///   the real state beats waiting an arbitrary number of days for it.
    /// * <see cref="IsHighResolution"/> matters because a high-resolution
    ///   structure is contoured on a finer grid, so comparing its point density
    ///   against a normal one without knowing that would be misleading.
    /// </summary>
    public sealed class SnapshotStructure
    {
        /// <summary>ESAPI <c>Structure.Id</c>, exactly as the clinic wrote it.</summary>
        [JsonProperty("id")]
        public string Id { get; set; }

        [JsonProperty("name")]
        public string Name { get; set; }

        /// <summary>Read from ESAPI, not guessed.</summary>
        [JsonProperty("dicom_type")]
        public string DicomType { get; set; }

        [JsonProperty("roi_number")]
        public int RoiNumber { get; set; }

        /// <summary>Catalog structure id this maps to, or null if unrecognised.</summary>
        [JsonProperty("structure_id")]
        public string StructureId { get; set; }

        /// <summary>Volume in cc, from ESAPI's own calculation.</summary>
        [JsonProperty("volume_cc")]
        public double? VolumeCc { get; set; }

        [JsonProperty("is_empty")]
        public bool IsEmpty { get; set; }

        [JsonProperty("is_high_resolution")]
        public bool IsHighResolution { get; set; }

        [JsonProperty("separate_parts")]
        public int? SeparateParts { get; set; }

        [JsonProperty("is_approved")]
        public bool IsApproved { get; set; }

        [JsonProperty("last_modified_by")]
        public string LastModifiedBy { get; set; }

        [JsonProperty("last_modified_at")]
        public DateTime? LastModifiedAt { get; set; }

        /// <summary>TG-263 style structure codes, when the clinic uses them.</summary>
        [JsonProperty("structure_codes")]
        public List<string> StructureCodes { get; set; }

        [JsonProperty("contours")]
        public List<ContourSlice> Contours { get; set; }
    }

    /// <summary>What the server did with a snapshot.</summary>
    public sealed class BaselineResponse
    {
        [JsonProperty("baseline_id")]
        public string BaselineId { get; set; }

        [JsonProperty("state")]
        public string State { get; set; }

        /// <summary>
        /// False when an identical structure set was already recorded. Not an
        /// error: the planner reopening a patient before editing is the normal
        /// case, and it must not create a second baseline or bill twice.
        /// </summary>
        [JsonProperty("created")]
        public bool Created { get; set; }

        [JsonProperty("superseded")]
        public bool Superseded { get; set; }

        [JsonProperty("structure_count")]
        public int StructureCount { get; set; }

        /// <summary>Where the coloured comparison lives, once there is one.</summary>
        [JsonProperty("web_url")]
        public string WebUrl { get; set; }

        [JsonProperty("message")]
        public string Message { get; set; }
    }
}
