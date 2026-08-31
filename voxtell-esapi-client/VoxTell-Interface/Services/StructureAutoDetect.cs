using System;
using System.Collections.Generic;
using System.Linq;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Works out what is already contoured on this series, so the default action
    /// can be "segment everything that is here" rather than "type some prompts".
    ///
    /// Why this exists
    /// ---------------
    /// Compare-by-default only works if the plugin knows what to compare. Asking
    /// the planner to pick structures every time guarantees the QA data is sparse
    /// and biased toward whatever they happened to be interested in. Reading the
    /// structure set and offering the whole recognised set costs the planner one
    /// glance.
    ///
    /// Why unmatched names are surfaced rather than dropped
    /// ---------------------------------------------------
    /// The published clinical audit of exactly this workflow lost cases to
    /// off-convention structure names that its script silently skipped, and the
    /// authors' first recommendation was standardised naming. Silence is the
    /// dangerous behaviour: a planner seeing "18 of 18 matched" trusts it, while
    /// "15 matched, 3 not recognised: Liver_old, ptv1, BODY" tells them something
    /// true and lets them fix the template. So the result carries both lists and
    /// the UI is expected to show both.
    ///
    /// This class is deliberately ESAPI-free — it takes names and returns
    /// decisions — so the harness can test the matching without Eclipse.
    /// </summary>
    public static class StructureAutoDetect
    {
        /// <summary>
        /// DICOM structure types that are never model targets, whatever they are
        /// called. A PTV is drawn by a clinician from disease extent, not inferred
        /// from anatomy, so offering to "re-segment" one is meaningless; support
        /// and external types are equipment.
        /// </summary>
        private static readonly HashSet<string> ExcludedDicomTypes =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "PTV", "CTV", "GTV", "TREATED_VOLUME", "IRRAD_VOLUME",
                "SUPPORT", "FIXATION", "BOLUS", "MARKER", "REGISTRATION",
                "ISOCENTER", "DOSE_REGION", "CONTROL_POINT",
            };

        /// <summary>
        /// Target-volume name prefixes, matched on the normalised name and only
        /// where the next character is a digit or the name ends there.
        ///
        /// The boundary rule is not fussiness, it is the whole point. A bare
        /// prefix test silently deletes real anatomy from the planner's list:
        /// <c>"opt"</c> swallows <b>Optic nerve</b> and <b>Optic chiasm</b>,
        /// <c>"temp"</c> swallows <b>Temporal lobe</b>, and <c>"test"</c> swallows
        /// <b>Testes</b> — all genuine organs at risk. And it fails invisibly,
        /// which is the failure mode this class exists to avoid.
        ///
        /// So this list is short, boundary-checked, and covers only the clutter
        /// that <see cref="ExcludedDicomTypes"/> cannot catch (a target volume
        /// typed <c>CONTROL</c> instead of <c>PTV</c>, which is common). Anything
        /// else unrecognised is *shown* to the planner, not hidden.
        /// </summary>
        private static readonly string[] TargetPrefixes =
        {
            "ptv", "ctv", "gtv", "itv", "igtv", "ptvall",
        };

        /// <summary>
        /// Scratch names, matched <b>exactly</b> against the normalised name and
        /// never as prefixes — <c>"temp"</c> must not reach <c>Temporal_L</c>.
        /// </summary>
        private static readonly HashSet<string> ScratchNames =
            new HashSet<string>(StringComparer.Ordinal)
            {
                "ring", "zring", "ring1", "ring2", "dose", "dosering",
                "dummy", "temp", "tempstructure", "test", "scratch", "junk",
                "opt", "optstructure", "help", "helper", "push", "pull", "avoid",
            };

        /// <summary>One existing structure and what became of the match attempt.</summary>
        public sealed class Candidate
        {
            /// <summary>The ESAPI <c>Structure.Id</c> as the clinic wrote it.</summary>
            public string ExistingId { get; set; }

            /// <summary>ESAPI <c>Structure.DicomType</c>, read not assumed.</summary>
            public string DicomType { get; set; }

            /// <summary>Volume in cc from ESAPI, for showing an empty structure.</summary>
            public double VolumeCc { get; set; }

            public bool IsEmpty { get; set; }

            public bool IsApproved { get; set; }

            /// <summary>The catalog structure this maps to, or null.</summary>
            public CatalogStructure Matched { get; set; }

            /// <summary>Why it was not offered. Null when it was.</summary>
            public string SkipReason { get; set; }

            public bool IsMatched
            {
                get { return Matched != null && SkipReason == null; }
            }
        }

        /// <summary>The outcome of scanning a structure set.</summary>
        public sealed class Detection
        {
            public IList<Candidate> Candidates { get; set; }

            /// <summary>Catalog ids to pre-select, deduplicated, in catalog order.</summary>
            public IList<string> StructureIds { get; set; }

            /// <summary>Models those ids require — the minimal set, not all of them.</summary>
            public IList<string> Models { get; set; }

            public IEnumerable<Candidate> Matched
            {
                get { return Candidates.Where(c => c.IsMatched); }
            }

            public IEnumerable<Candidate> Unmatched
            {
                get { return Candidates.Where(c => !c.IsMatched && c.SkipReason == null); }
            }

            public IEnumerable<Candidate> Skipped
            {
                get { return Candidates.Where(c => c.SkipReason != null); }
            }

            /// <summary>
            /// One line for the panel. Factual counts, not reassurance — and it
            /// always names the unrecognised total, because that is the number the
            /// planner can act on.
            /// </summary>
            public string Summary
            {
                get
                {
                    int matched = Matched.Count();
                    int unmatched = Unmatched.Count();
                    int skipped = Skipped.Count();

                    if (Candidates.Count == 0) return "No structures on this series yet.";

                    string text = matched + (matched == 1 ? " structure" : " structures")
                                  + " recognised";
                    if (unmatched > 0) text += ", " + unmatched + " not recognised";
                    if (skipped > 0) text += ", " + skipped + " not a model target";
                    return text + ".";
                }
            }
        }

        /// <summary>
        /// Match existing structures against the catalog.
        ///
        /// <paramref name="existing"/> is built by the caller from ESAPI so this
        /// stays testable. Order of the returned ids follows the catalog, not the
        /// structure set, so two workstations produce the same request for the same
        /// patient and the server's dedup works.
        /// </summary>
        public static Detection Scan(IEnumerable<Candidate> existing, ModelCatalog catalog)
        {
            var candidates = (existing ?? Enumerable.Empty<Candidate>()).ToList();

            foreach (Candidate candidate in candidates)
            {
                candidate.SkipReason = ClassifySkip(candidate);
                if (candidate.SkipReason != null) continue;
                if (catalog == null) continue;

                candidate.Matched = catalog.Resolve(candidate.ExistingId);
            }

            // Catalog order, deduplicated: two existing structures can legitimately
            // resolve to the same catalog entry (a clinic with both "Kidney_R" and
            // "R Kidney" left over from a template change), and asking for it twice
            // would waste a channel.
            var wanted = new HashSet<string>(
                candidates.Where(c => c.IsMatched).Select(c => c.Matched.Id),
                StringComparer.Ordinal);

            var ids = new List<string>();
            if (catalog != null && catalog.Structures != null)
            {
                foreach (CatalogStructure s in catalog.Structures)
                {
                    if (wanted.Contains(s.Id)) ids.Add(s.Id);
                }
            }

            var models = new List<string>();
            if (catalog != null && catalog.Models != null)
            {
                var needed = new HashSet<string>(
                    ids.Select(i => catalog.Structure(i))
                       .Where(s => s != null)
                       .Select(s => s.SourceModel),
                    StringComparer.Ordinal);
                foreach (CatalogModel m in catalog.Models)
                {
                    if (needed.Contains(m.Key)) models.Add(m.Key);
                }
            }

            return new Detection
            {
                Candidates = candidates,
                StructureIds = ids,
                Models = models,
            };
        }

        /// <summary>
        /// Why this structure is not a model target, or null if it is.
        ///
        /// The DICOM type is checked first because it is the only signal the clinic
        /// cannot accidentally break by renaming.
        /// </summary>
        private static string ClassifySkip(Candidate candidate)
        {
            if (candidate == null || string.IsNullOrWhiteSpace(candidate.ExistingId))
            {
                return "no id";
            }

            if (!string.IsNullOrWhiteSpace(candidate.DicomType)
                && ExcludedDicomTypes.Contains(candidate.DicomType.Trim()))
            {
                return candidate.DicomType.Trim().ToUpperInvariant();
            }

            string normalised = ModelCatalog.Normalise(candidate.ExistingId);

            if (ScratchNames.Contains(normalised)) return "scratch";

            foreach (string prefix in TargetPrefixes)
            {
                if (!normalised.StartsWith(prefix, StringComparison.Ordinal)) continue;

                // Boundary: the prefix must be the whole name, or be followed by a
                // digit ("ptv7000", "ctv1"). A following letter means a different
                // word, so "gtvx" is not treated as a target and "opticnervel"
                // could never have reached here anyway.
                if (normalised.Length == prefix.Length) return "target volume";

                char next = normalised[prefix.Length];
                if (next >= '0' && next <= '9') return "target volume";
            }

            return null;
        }
    }
}
