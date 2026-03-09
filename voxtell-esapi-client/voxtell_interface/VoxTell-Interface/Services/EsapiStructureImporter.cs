using System;
using System.Collections.Generic;
using System.Linq;
using VMS.TPS.Common.Model.API;
using VMS.TPS.Common.Model.Types;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Result of validating which inference structures exist in Eclipse.
    /// </summary>
    public class StructureValidation
    {
        /// <summary>Structures that already exist in Eclipse.</summary>
        public List<string> Available { get; set; } = new List<string>();

        /// <summary>Structures requested by inference that don't exist yet (will be auto-created).</summary>
        public List<string> Missing { get; set; } = new List<string>();

        /// <summary>Maps sanitized inference name -> matched Eclipse structure Id.</summary>
        public Dictionary<string, string> MatchMap { get; set; } = new Dictionary<string, string>();

        /// <summary>All structure Ids currently in the Eclipse StructureSet.</summary>
        public List<string> AllStructureIds { get; set; } = new List<string>();
    }

    /// <summary>
    /// Imports inference contour results into Eclipse structures using the
    /// TPS External Beam Planning API (VMS.TPS.Common.Model.API).
    ///
    /// Uses first-class, documented, write-enabled methods:
    ///   - structureSet.AddStructure(dicomType, name) to create structures
    ///   - structure.AddContourOnImagePlane(VVector[] points, zIndex) to write contours
    ///
    /// Missing structures are auto-created. No reflection needed.
    /// BeginModifications() must be called once in Script.cs before any writes.
    /// </summary>
    public class EsapiStructureImporter
    {
        private readonly ScriptContext _context;
        private readonly StructureSet _structureSet;
        private readonly Random _rng = new Random();

        private static readonly System.Windows.Media.Color[] StructureColors =
        {
            System.Windows.Media.Color.FromRgb(239,  83,  80),  // red
            System.Windows.Media.Color.FromRgb( 79, 195, 247),  // light blue
            System.Windows.Media.Color.FromRgb(102, 187, 106),  // green
            System.Windows.Media.Color.FromRgb(255, 167,  38),  // orange
            System.Windows.Media.Color.FromRgb(171, 71,  188),  // purple
            System.Windows.Media.Color.FromRgb(255, 241,  118), // yellow
            System.Windows.Media.Color.FromRgb( 38, 198, 218),  // teal
            System.Windows.Media.Color.FromRgb(240,  98, 146),  // pink
            System.Windows.Media.Color.FromRgb(129, 199, 132),  // light green
            System.Windows.Media.Color.FromRgb(144, 164, 174),  // blue grey
            System.Windows.Media.Color.FromRgb(255, 138, 101),  // deep orange
            System.Windows.Media.Color.FromRgb(121, 134, 203),  // indigo
        };

        public EsapiStructureImporter(ScriptContext context)
        {
            _context = context;
            _structureSet = context.StructureSet;
        }

        // ══════════════════════════════════════════════════════════════════
        //  Validation
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Checks which inference structures already exist in Eclipse.
        /// Missing structures will be auto-created during import.
        /// </summary>
        public StructureValidation ValidateStructures(
            List<InferenceResult> results, out List<string> warnings)
        {
            var validation = new StructureValidation();
            warnings = new List<string>();

            if (_structureSet == null)
            {
                warnings.Add("No StructureSet available in context.");
                foreach (var r in results)
                    validation.Missing.Add(SanitizeStructureName(r.Prompt));
                return validation;
            }

            // Collect all Eclipse structure Ids
            foreach (var s in _structureSet.Structures)
            {
                validation.AllStructureIds.Add(s.Id);
            }

            // Check each inference result
            foreach (var result in results)
            {
                string sanitizedId = SanitizeStructureName(result.Prompt);
                var match = FindStructure(sanitizedId, result.Prompt);

                if (match != null)
                {
                    validation.Available.Add(sanitizedId);
                    validation.MatchMap[sanitizedId] = match.Id;
                }
                else
                {
                    validation.Missing.Add(sanitizedId);
                }
            }

            return validation;
        }

        // ══════════════════════════════════════════════════════════════════
        //  Import
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Imports inference results into Eclipse structures.
        /// Existing structures are matched; missing ones are auto-created.
        /// Returns a summary of each structure written.
        /// </summary>
        public List<string> ProcessResults(
            List<InferenceResult> results, out List<string> warnings)
        {
            var imported = new List<string>();
            warnings = new List<string>();

            if (_structureSet == null)
            {
                warnings.Add("No StructureSet available in context.");
                return imported;
            }

            foreach (var result in results)
            {
                string structureId = SanitizeStructureName(result.Prompt);
                try
                {
                    if (result.Contours == null || result.Contours.Count == 0)
                    {
                        warnings.Add(string.Format(
                            "Structure '{0}': No contour data.", structureId));
                        continue;
                    }

                    var validSlices = result.Contours
                        .Where(c => c.PointsLps != null && c.PointsLps.Count >= 3)
                        .ToList();

                    if (validSlices.Count == 0)
                    {
                        warnings.Add(string.Format(
                            "Structure '{0}': No slices with >= 3 points.", structureId));
                        continue;
                    }

                    // Find existing structure or auto-create
                    var structure = FindStructure(structureId, result.Prompt);

                    if (structure == null)
                    {
                        structure = _structureSet.AddStructure("CONTROL", structureId);
                        structure.Color = StructureColors[_rng.Next(StructureColors.Length)];
                        warnings.Add(string.Format(
                            "Auto-created structure '{0}'.", structureId));
                    }

                    // Write contours slice by slice
                    int slicesWritten = 0;
                    foreach (var slice in validSlices)
                    {
                        var points = new VVector[slice.PointsLps.Count];
                        for (int i = 0; i < slice.PointsLps.Count; i++)
                        {
                            var p = slice.PointsLps[i];
                            points[i] = new VVector(p[0], p[1], p[2]);
                        }

                        structure.AddContourOnImagePlane(points, slice.ZIndex);
                        slicesWritten++;
                    }

                    int totalPoints = validSlices.Sum(s => s.PointsLps.Count);
                    imported.Add(string.Format(
                        "{0}: {1} slices, {2} points",
                        structureId, slicesWritten, totalPoints));
                }
                catch (Exception ex)
                {
                    warnings.Add(string.Format(
                        "Error writing '{0}': {1}", structureId, ex.Message));
                }
            }

            return imported;
        }

        // ══════════════════════════════════════════════════════════════════
        //  Helpers
        // ══════════════════════════════════════════════════════════════════

        /// <summary>
        /// Finds a structure by exact ID match, then by case-insensitive match,
        /// then by fuzzy match on the original prompt.
        /// </summary>
        private Structure FindStructure(string sanitizedId, string originalPrompt)
        {
            if (_structureSet == null) return null;

            // Exact match on sanitized ID
            var match = _structureSet.Structures
                .FirstOrDefault(s => string.Equals(s.Id, sanitizedId, StringComparison.Ordinal));
            if (match != null) return match;

            // Case-insensitive match
            match = _structureSet.Structures
                .FirstOrDefault(s => string.Equals(s.Id, sanitizedId, StringComparison.OrdinalIgnoreCase));
            if (match != null) return match;

            // Fuzzy: try matching against original prompt (case-insensitive)
            if (!string.IsNullOrEmpty(originalPrompt))
            {
                string promptLower = originalPrompt.ToLowerInvariant();
                match = _structureSet.Structures
                    .FirstOrDefault(s => string.Equals(
                        s.Id, originalPrompt, StringComparison.OrdinalIgnoreCase));
                if (match != null) return match;

                // Check if any structure ID is contained in the prompt or vice versa
                match = _structureSet.Structures
                    .FirstOrDefault(s =>
                        promptLower.Contains(s.Id.ToLowerInvariant()) ||
                        s.Id.ToLowerInvariant().Contains(promptLower));
            }

            return match;
        }

        /// <summary>
        /// Sanitizes a prompt string into a valid Eclipse structure ID.
        /// Eclipse structure IDs are limited to 16 characters.
        /// </summary>
        private string SanitizeStructureName(string prompt)
        {
            if (string.IsNullOrWhiteSpace(prompt))
                return "AI_Structure";

            var sanitized = new string(prompt
                .Where(c => char.IsLetterOrDigit(c) || c == '_' || c == '-')
                .ToArray());

            if (sanitized.Length > 16)
                sanitized = sanitized.Substring(0, 16);

            if (string.IsNullOrEmpty(sanitized))
                return "AI_Structure";

            return sanitized;
        }
    }
}
