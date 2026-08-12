using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using VMS.TPS.Common.Model.API;
using VMS.TPS.Common.Model.Types;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Writes inference results into Eclipse structures through the External Beam Planning API.
    ///
    /// The write path is deliberately conservative, because everything it does lands in a
    /// patient's structure set:
    /// <list type="bullet">
    /// <item>Nothing is written until the operator has reviewed a <see cref="StructurePlan"/>
    /// per prompt and ticked it.</item>
    /// <item>Structure matching is exact, then case-insensitive, and stops there. v1 also did a
    /// substring match, which silently appended AI contours into an existing <c>Liver_PRV</c>
    /// or <c>PTV_Liver</c> when the prompt was "liver".</item>
    /// <item>An existing structure's affected planes are cleared before writing, so re-running a
    /// prompt replaces its contours instead of superimposing a second copy.</item>
    /// </list>
    ///
    /// Every method here touches the Eclipse object model and must run on the ESAPI thread;
    /// <see cref="EsapiGate"/> enforces that rather than trusting the call site.
    /// <c>BeginModifications()</c> must already have been called (Script.cs does it).
    /// </summary>
    public class EsapiStructureImporter
    {
        /// <summary>Eclipse structure Ids are capped at 16 characters.</summary>
        private const int MaxStructureIdLength = 16;

        /// <summary>
        /// Server-side, contours below 10 points are dropped as marching-squares speckle.
        /// Eclipse also rejects degenerate contours, so the floor is re-applied here.
        /// </summary>
        private const int MinPointsPerContour = 3;

        private readonly StructureSet _structureSet;
        private readonly IThreadGate _gate;
        private readonly int _zSize;
        private readonly double _voxelVolumeMm3;

        public EsapiStructureImporter(
            StructureSet structureSet, int zSize, double voxelVolumeMm3, IThreadGate gate)
        {
            if (gate == null) throw new ArgumentNullException("gate");

            _gate = gate;
            _gate.AssertOnEsapiThread("Reading the structure set");

            _structureSet = structureSet;
            _zSize = zSize;
            _voxelVolumeMm3 = voxelVolumeMm3;
        }

        public bool HasStructureSet { get { return _structureSet != null; } }

        public string StructureSetId
        {
            get
            {
                _gate.AssertOnEsapiThread("Reading StructureSet.Id");
                return _structureSet == null ? null : _structureSet.Id;
            }
        }

        // ══════════════════════════════════════════════════════════════════
        //  Planning — what WOULD be written
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Works out, without writing anything, what each result would become. This is what the
        /// review list binds to; the operator can rename, retype and untick rows before import.
        /// </summary>
        public List<StructurePlan> BuildPlan(List<InferenceResult> results)
        {
            _gate.AssertOnEsapiThread("Enumerating structures");

            var plans = new List<StructurePlan>();
            if (results == null) return plans;

            // Reserve Ids as we go so two prompts that sanitise to the same 16 characters —
            // "superior vena cava" and "superior vena cava anterior", say — do not collide and
            // make the second AddStructure throw.
            var taken = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (_structureSet != null)
            {
                foreach (Structure s in _structureSet.Structures)
                    taken.Add(s.Id);
            }

            // Colours already handed out in THIS batch, so no two new structures come back the
            // same. Seeded with nothing: an existing structure keeps its own colour and does not
            // reserve it, because we never write over it anyway.
            var usedColors = new List<System.Windows.Media.Color>();

            foreach (InferenceResult result in results)
            {
                var valid = (result.Contours ?? new List<ContourSlice>())
                    .Where(c => c.PointsLps != null && c.PointsLps.Count >= MinPointsPerContour)
                    .ToList();

                int[] occupied = valid
                    .Select(c => c.ZIndex).Distinct().OrderBy(z => z).ToArray();

                Structure existing = _structureSet == null ? null : FindStructure(result.Prompt);

                string id = existing != null
                    ? existing.Id
                    : UniqueStructureId(result.Prompt, taken);

                if (existing == null)
                    taken.Add(id);

                // Show the truth in the review list: an overwrite keeps the structure's current
                // colour, so display that rather than a colour we are not going to apply.
                System.Windows.Media.Color color;
                if (existing != null)
                {
                    color = existing.Color;
                }
                else
                {
                    color = StructurePalette.Assign(result.Prompt, usedColors);
                    usedColors.Add(color);
                }

                var plan = new StructurePlan
                {
                    Prompt = result.Prompt,
                    StructureId = id,
                    DicomType = "CONTROL",
                    ExistingId = existing == null ? null : existing.Id,
                    Color = color,
                    VoxelCount = result.VoxelCount,
                    ContourCount = valid.Count,
                    FirstSlice = occupied.Length > 0 ? occupied[0] : 0,
                    LastSlice = occupied.Length > 0 ? occupied[occupied.Length - 1] : 0,
                    OccupiedSlices = occupied,
                    SeriesSliceCount = _zSize,
                    VoxelVolumeMm3 = _voxelVolumeMm3,
                    // Default to importing anything the model actually found, so the common
                    // case is one click — but never pre-tick an empty result.
                    Selected = valid.Count > 0,
                };

                if (valid.Count == 0)
                {
                    // Distinguish "the model found nothing" from "the model found something but
                    // every boundary was too small to survive the server's speckle filter". The
                    // second creates a named, empty structure in the patient if it is ticked,
                    // which is a worse outcome than an obvious failure.
                    plan.Note = result.VoxelCount > 0
                        ? string.Format(
                            "Found {0:N0} voxels but no contour large enough to write — " +
                            "importing this would create an empty structure.", result.VoxelCount)
                        : "Nothing segmented for this prompt.";
                }
                else if (existing != null)
                {
                    plan.Note = string.Format(
                        "Replaces the contours on {0} slice(s) of the existing '{1}'.",
                        occupied.Length, existing.Id);
                }
                else if (_structureSet != null && !CanCreate(plan.DicomType, id))
                {
                    plan.Selected = false;
                    plan.Note = "Eclipse will not allow a new structure with this Id " +
                                "(the structure set may be approved).";
                }

                plans.Add(plan);
            }

            return plans;
        }

        private bool CanCreate(string dicomType, string id)
        {
            try
            {
                // Pre-flight rather than catching the throw: an approved structure set is a
                // normal clinical state, not an exceptional one.
                return _structureSet.CanAddStructure(dicomType, id);
            }
            catch
            {
                return false;
            }
        }

        // ══════════════════════════════════════════════════════════════════
        //  Import — the only code that writes to the patient
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Writes the ticked plans. Returns a per-structure summary; <paramref name="warnings"/>
        /// collects anything the operator should see afterwards.
        /// </summary>
        public List<string> Import(
            List<StructurePlan> plans, List<InferenceResult> results, out List<string> warnings)
        {
            _gate.AssertOnEsapiThread("Writing structures");

            var imported = new List<string>();
            warnings = new List<string>();

            if (_structureSet == null)
            {
                warnings.Add("No structure set is open, so nothing was imported.");
                return imported;
            }

            var byPrompt = new Dictionary<string, InferenceResult>(StringComparer.Ordinal);
            foreach (InferenceResult r in results)
                byPrompt[r.Prompt] = r;

            foreach (StructurePlan plan in plans.Where(p => p.Selected))
            {
                InferenceResult result;
                if (!byPrompt.TryGetValue(plan.Prompt, out result))
                {
                    warnings.Add(string.Format("No result found for '{0}'.", plan.Prompt));
                    continue;
                }

                // Per-structure isolation: one bad prompt must not abandon the rest of a study
                // half-imported.
                try
                {
                    imported.Add(ImportOne(plan, result, warnings));
                }
                catch (Exception ex)
                {
                    warnings.Add(string.Format(
                        "Failed to write '{0}': {1}", plan.StructureId, ex.Message));
                }
            }

            return imported;
        }

        private string ImportOne(StructurePlan plan, InferenceResult result, List<string> warnings)
        {
            var slices = (result.Contours ?? new List<ContourSlice>())
                .Where(c => c.PointsLps != null && c.PointsLps.Count >= MinPointsPerContour)
                .ToList();

            // A z_index outside the grid would throw deep inside ESAPI with nothing to act on.
            // It should not happen — the server derives it from the geometry we sent — so if it
            // does, say so loudly rather than dropping contours quietly.
            var outOfRange = slices.Where(c => c.ZIndex < 0 || c.ZIndex >= _zSize).ToList();
            if (outOfRange.Count > 0)
            {
                warnings.Add(string.Format(
                    "'{0}': {1} contour(s) name a slice outside this image's {2} planes and were skipped.",
                    plan.StructureId, outOfRange.Count, _zSize));
                slices = slices.Except(outOfRange).ToList();
            }

            if (slices.Count == 0)
                return string.Format("{0}: nothing to write", plan.StructureId);

            // Resolve by the Id as it stands NOW rather than trusting the handle captured when
            // the plan was built: the review list lets the operator rename a row, and a rename
            // onto a structure that already exists must clear and replace it, not fail on a
            // duplicate AddStructure. Conversely, renaming away from a match must create a new
            // structure instead of quietly overwriting the one originally matched.
            Structure structure = _structureSet.Structures.FirstOrDefault(
                s => string.Equals(s.Id, plan.StructureId, StringComparison.OrdinalIgnoreCase));

            if (structure == null)
            {
                if (!CanCreate(plan.DicomType, plan.StructureId))
                {
                    warnings.Add(string.Format(
                        "'{0}': Eclipse will not allow a new structure with this Id — the " +
                        "structure set may be approved, or the Id may be invalid.",
                        plan.StructureId));
                    return string.Format("{0}: not created", plan.StructureId);
                }

                structure = _structureSet.AddStructure(plan.DicomType, plan.StructureId);
                structure.Color = plan.Color;
            }
            else
            {
                // Note what is NOT touched here: the structure's colour and DICOM type. The
                // planner may already have built a plan around both, and changing either because
                // an AI re-run happened to pick differently is not this tool's call. The review
                // list shows the existing colour for the same reason — so the row tells the truth
                // about what will happen.
                //
                // Clear the planes we are about to write. Without this, re-running a prompt
                // leaves both the old and the new boundary on every slice — two contours where
                // the planner sees one structure, and a volume that is quietly wrong.
                foreach (int z in slices.Select(c => c.ZIndex).Distinct())
                {
                    try
                    {
                        structure.ClearAllContoursOnImagePlane(z);
                    }
                    catch (Exception ex)
                    {
                        warnings.Add(string.Format(
                            "'{0}': could not clear slice {1} before writing ({2}). The new " +
                            "contour was added alongside whatever was already there.",
                            plan.StructureId, z, ex.Message));
                    }
                }
            }

            int written = 0;
            int totalPoints = 0;

            // One AddContourOnImagePlane call per contour ENTRY, not per slice. A slice
            // legitimately appears several times — one entry per closed boundary — which is how
            // ring-shaped and multi-lobed structures come back correctly. Collapsing these by
            // z_index would merge a ring into its outer boundary.
            foreach (ContourSlice slice in slices)
            {
                VVector[] points = ToVVectors(slice, plan.StructureId, warnings);
                if (points == null) continue;

                structure.AddContourOnImagePlane(points, slice.ZIndex);
                written++;
                totalPoints += points.Length;
            }

            return string.Format("{0}: {1} contour(s), {2:N0} points, {3:N0} voxels",
                plan.StructureId, written, totalPoints, plan.VoxelCount);
        }

        private static VVector[] ToVVectors(
            ContourSlice slice, string structureId, List<string> warnings)
        {
            var points = new VVector[slice.PointsLps.Count];

            for (int i = 0; i < slice.PointsLps.Count; i++)
            {
                double[] p = slice.PointsLps[i];

                // v1 indexed p[0..2] unconditionally; a short array from a future schema change
                // would have surfaced as an IndexOutOfRangeException swallowed into a warning.
                if (p == null || p.Length < 3)
                {
                    warnings.Add(string.Format(
                        "'{0}': a contour point on slice {1} was malformed; the contour was skipped.",
                        structureId, slice.ZIndex));
                    return null;
                }

                // points_lps are millimetres in the DICOM patient frame, which is exactly what
                // AddContourOnImagePlane expects — no conversion, by design.
                points[i] = new VVector(p[0], p[1], p[2]);
            }

            return points;
        }

        // ══════════════════════════════════════════════════════════════════
        //  Matching and naming
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Finds an existing structure for a prompt: exact match on the sanitised Id, then
        /// case-insensitive, then case-insensitive against the raw prompt. No substring
        /// matching — see the class remarks for why.
        /// </summary>
        private Structure FindStructure(string prompt)
        {
            string id = SanitizeStructureName(prompt);

            Structure match = _structureSet.Structures
                .FirstOrDefault(s => string.Equals(s.Id, id, StringComparison.Ordinal));
            if (match != null) return match;

            match = _structureSet.Structures
                .FirstOrDefault(s => string.Equals(s.Id, id, StringComparison.OrdinalIgnoreCase));
            if (match != null) return match;

            if (!string.IsNullOrEmpty(prompt))
            {
                match = _structureSet.Structures
                    .FirstOrDefault(s => string.Equals(s.Id, prompt, StringComparison.OrdinalIgnoreCase));
            }

            return match;
        }

        /// <summary>
        /// Turns a prompt into an Eclipse structure Id: separators become underscores, invalid
        /// characters are dropped, and the result is truncated to 16 characters.
        /// </summary>
        public static string SanitizeStructureName(string prompt)
        {
            if (string.IsNullOrWhiteSpace(prompt))
                return "AI_Structure";

            // Replace separators rather than deleting them: v1 stripped spaces outright, so
            // "left kidney" became "leftkidney" — legal, but not what anyone reads for.
            var sb = new StringBuilder(prompt.Length);
            foreach (char c in prompt)
            {
                if (char.IsLetterOrDigit(c) || c == '_' || c == '-')
                    sb.Append(c);
                else if (char.IsWhiteSpace(c) || c == ',' || c == '.' || c == '/')
                    sb.Append('_');
            }

            string sanitized = sb.ToString().Trim('_');
            while (sanitized.Contains("__"))
                sanitized = sanitized.Replace("__", "_");

            if (sanitized.Length > MaxStructureIdLength)
                sanitized = sanitized.Substring(0, MaxStructureIdLength).TrimEnd('_');

            return string.IsNullOrEmpty(sanitized) ? "AI_Structure" : sanitized;
        }

        /// <summary>
        /// Sanitises, then makes the Id unique against <paramref name="taken"/> by appending a
        /// numeric suffix inside the 16-character budget.
        /// </summary>
        private static string UniqueStructureId(string prompt, HashSet<string> taken)
        {
            string basis = SanitizeStructureName(prompt);
            if (!taken.Contains(basis)) return basis;

            for (int n = 2; n < 100; n++)
            {
                string suffix = "_" + n;
                string trimmed = basis.Length + suffix.Length > MaxStructureIdLength
                    ? basis.Substring(0, MaxStructureIdLength - suffix.Length).TrimEnd('_')
                    : basis;

                string candidate = trimmed + suffix;
                if (!taken.Contains(candidate)) return candidate;
            }

            // 98 collisions on one name is not a real scenario, but returning something
            // colliding would be worse than a clear failure at AddStructure.
            return basis;
        }
    }
}
