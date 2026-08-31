using System;
using System.Collections.Generic;
using System.Linq;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;

namespace VoxTell_Interface.Harness
{
    /// <summary>
    /// Offline checks on the plugin logic that fails <b>silently</b> in the field.
    ///
    /// Why these four and not others
    /// -----------------------------
    /// Everything tested here shares one property: when it is wrong, nothing throws,
    /// no status line changes, and the panel looks like it worked.
    ///
    /// * <b>Normalise</b> is one third of a contract written in three languages
    ///   (here, <c>voxtell_cloud/catalog.py</c>, <c>scripts/gen_catalog.py</c>). If the
    ///   C# and the Python ever disagree about what a structure name normalises to,
    ///   auto-detect simply stops matching and reports "0 recognised" on a series
    ///   full of contours.
    /// * <b>The skip rules</b> decide which existing structures are never offered.
    ///   A too-greedy prefix silently removes real organs from the planner's list —
    ///   the reason the bare-prefix version of this list was replaced.
    /// * <b>ContentHash</b> is the idempotency key. If it is unstable, every reopen
    ///   records a new baseline; if it is too coarse, a redrawn organ counts as
    ///   unchanged.
    /// * <b>LineageKeys</b> must never return a valid-looking key for absent input,
    ///   or every series without a UID collapses onto one baseline.
    ///
    /// Run: <c>VoxTell-Harness.exe --selftest</c>
    /// </summary>
    internal static class SelfTest
    {
        private static int _failures;
        private static int _checks;

        public static int Run()
        {
            Console.WriteLine("VoxTell plugin self-test (offline, no Eclipse, no server)");
            Console.WriteLine();

            NormaliseContract();
            SkipRules();
            ContentHashStability();
            LineageKeyRules();
            CatalogMatching();
            ProtocolRules();

            Console.WriteLine();
            Console.WriteLine("{0} checks, {1} failure(s)", _checks, _failures);
            return _failures == 0 ? 0 : 1;
        }

        // ------------------------------------------------------------------ //

        private static void NormaliseContract()
        {
            Section("Normalise - the cross-language match contract");

            // These pairs are the contract. The Python side must agree exactly; the
            // expected values are what `re.sub(r'[^a-z0-9]', '', s.lower())` produces.
            Check("Kidney_R", ModelCatalog.Normalise("Kidney_R") == "kidneyr");
            Check("Kidney R", ModelCatalog.Normalise("Kidney R") == "kidneyr");
            Check("kidney-r", ModelCatalog.Normalise("kidney-r") == "kidneyr");
            Check("  Kidney . R  ", ModelCatalog.Normalise("  Kidney . R  ") == "kidneyr");
            Check("PTV_7000", ModelCatalog.Normalise("PTV_7000") == "ptv7000");
            Check("Vertebra L5", ModelCatalog.Normalise("Vertebra L5") == "vertebral5");
            Check("empty string", ModelCatalog.Normalise("") == "");
            Check("null", ModelCatalog.Normalise(null) == "");
            Check("digits kept", ModelCatalog.Normalise("Rib-12 L") == "rib12l");

            // Non-ASCII is dropped, matching Python's [^a-z0-9] on an ASCII class.
            // Stated as a test because it is a real decision, not an accident: a clinic
            // using accented names gets no match rather than a wrong one.
            Check("non-ASCII dropped", ModelCatalog.Normalise("Oesophageé") == "oesophage");
        }

        private static void SkipRules()
        {
            Section("Auto-detect skip rules - must not swallow real anatomy");

            // The regression this exists for: a bare-prefix skip list matched "opt",
            // "temp" and "test", which silently removed optic nerves, temporal lobes
            // and testes from the planner's list.
            foreach (string name in new[]
            {
                "OpticNerve_L", "Optic_Chiasm", "Temporal_L", "Temp_Lobe_R",
                "Testes", "Testis_L", "Ring_Structure_Heart", "Dosimetrist_Liver",
            })
            {
                Check("kept: " + name, Skip(name, "ORGAN") == null);
            }

            // Target volumes are skipped, by DICOM type where it is set...
            Check("PTV by type", Skip("Prostate_Target", "PTV") != null);
            Check("GTV by type", Skip("Whatever", "GTV") != null);

            // ...and by name where a clinic typed the target as CONTROL, which is common.
            Check("ptv7000 by name", Skip("PTV_7000", "CONTROL") != null);
            Check("ctv1 by name", Skip("CTV1", "CONTROL") != null);
            Check("bare PTV by name", Skip("PTV", "CONTROL") != null);

            // A following letter means a different word, so it is shown rather than hidden.
            Check("PTV_High shown", Skip("PTV_High", "CONTROL") == null);

            // Exact scratch names only.
            Check("exact 'temp' skipped", Skip("temp", "CONTROL") != null);
            Check("'Temporal_L' kept", Skip("Temporal_L", "CONTROL") == null);
            Check("no id", Skip("", "ORGAN") != null);
        }

        private static void ContentHashStability()
        {
            Section("ContentHash - the idempotency key");

            StructureSnapshot a = Snapshot(120.0);
            StructureSnapshot b = Snapshot(120.0);
            Check("stable across identical reads",
                a.ComputeContentHash() == b.ComputeContentHash());

            // Below the 3-decimal quantisation: a re-read of an untouched structure
            // must not look edited because of a last-bit float difference.
            StructureSnapshot noise = Snapshot(120.00001);
            Check("ignores sub-micron float noise",
                a.ComputeContentHash() == noise.ComputeContentHash());

            // A real edit must change it. 0.5 mm is far smaller than any clinically
            // meaningful change, so if this passes, anything real does.
            StructureSnapshot moved = Snapshot(120.5);
            Check("detects a 0.5 mm move",
                a.ComputeContentHash() != moved.ComputeContentHash());

            // Emptying a structure is the "rejected" signal and must be visible even
            // though there is no geometry left to compare.
            StructureSnapshot emptied = Snapshot(120.0);
            emptied.Structures[0].Contours.Clear();
            emptied.Structures[0].IsEmpty = true;
            Check("detects an emptied structure",
                a.ComputeContentHash() != emptied.ComputeContentHash());

            // ESAPI's enumeration order is not a promise.
            StructureSnapshot two = Snapshot(120.0);
            two.Structures.Add(Structure("Liver", 200.0));
            StructureSnapshot twoReversed = Snapshot(120.0);
            twoReversed.Structures.Insert(0, Structure("Liver", 200.0));
            Check("independent of structure order",
                two.ComputeContentHash()
                == twoReversed.ComputeContentHash());

            // Renaming is an edit: the id is part of the hash.
            StructureSnapshot renamed = Snapshot(120.0);
            renamed.Structures[0].Id = "Kidney_Right";
            Check("detects a rename",
                a.ComputeContentHash() != renamed.ComputeContentHash());
        }

        private static void LineageKeyRules()
        {
            Section("LineageKeys - absent must stay absent");

            const string secret = "0123456789abcdef0123456789abcdef" +
                                  "0123456789abcdef0123456789abcdef";
            const string uid = "1.2.840.113619.2.55.3.604688119.868.1234567890.123";

            string key = LineageKeys.Series(secret, uid);
            Check("returns 64 hex chars", key != null && key.Length == 64);
            Check("lowercase hex", key != null && key.All(c => "0123456789abcdef".Contains(c)));
            Check("deterministic", key == LineageKeys.Series(secret, uid));
            Check("differs by UID", key != LineageKeys.Series(secret, uid + "9"));
            Check("differs by secret",
                key != LineageKeys.Series(secret.Replace('0', '1'), uid));

            // Domain separation: the same UID must not produce the same key for two
            // different kinds, or a frame-of-reference key could be mistaken for a
            // series key.
            Check("series and frame differ", key != LineageKeys.Frame(secret, uid));

            // The important one. A hash of "" is a valid-looking key that every series
            // without a UID would share, silently merging unrelated patients.
            Check("null on missing UID", LineageKeys.Series(secret, null) == null);
            Check("null on blank UID", LineageKeys.Series(secret, "   ") == null);
            Check("null on missing secret", LineageKeys.Series(null, uid) == null);
            Check("null on blank secret", LineageKeys.Series("", uid) == null);
            Check("null on odd-length secret", LineageKeys.Series("abc", uid) == null);
            Check("null on non-hex secret", LineageKeys.Series("zzzz", uid) == null);

            // Whitespace must not fork the key, or the same series recorded from two
            // workstations would produce two baselines.
            Check("trims the value", key == LineageKeys.Series(secret, " " + uid + " "));

            // Scanner triple: length-prefixed, so the fields cannot be confused.
            Check("scanner needs some content",
                LineageKeys.Scanner(secret, null, null, null) == null);
            Check("scanner ('AB','C') != ('A','BC')",
                LineageKeys.Scanner(secret, "AB", "C", "")
                != LineageKeys.Scanner(secret, "A", "BC", ""));
        }

        private static void CatalogMatching()
        {
            Section("Catalog matching");

            var catalog = new ModelCatalog
            {
                Version = 1,
                GroupOrder = new List<string> { "Pelvic organs", "Abdominal organs" },
                Models = new List<CatalogModel>
                {
                    new CatalogModel { Key = "voxtell", Kind = "prompt", DisplayName = "VoxTell" },
                    new CatalogModel { Key = "cads_551", Kind = "cads", DisplayName = "CADS 551" },
                    new CatalogModel { Key = "cads_556", Kind = "cads", DisplayName = "CADS 556" },
                },
                Structures = new List<CatalogStructure>
                {
                    new CatalogStructure
                    {
                        Id = "cads_551.kidney_r", DisplayName = "Kidney R",
                        Group = "Abdominal organs", SourceModel = "cads_551",
                        Aliases = new List<string> { "kidneyr", "rkidney", "rightkidney" },
                    },
                    new CatalogStructure
                    {
                        Id = "cads_556.rectum", DisplayName = "Rectum",
                        Group = "Pelvic organs", SourceModel = "cads_556",
                        Aliases = new List<string> { "rectum" },
                    },
                },
            };

            Check("resolves an alias",
                catalog.Resolve("Kidney_R") != null
                && catalog.Resolve("Kidney_R").Id == "cads_551.kidney_r");
            Check("resolves the display name", catalog.Resolve("Rectum") != null);
            Check("unmatched returns null, never a guess", catalog.Resolve("PTV_7000") == null);
            Check("prompt kind flagged", catalog.Model("voxtell").TakesPrompts);
            Check("cads kind not prompt", !catalog.Model("cads_556").TakesPrompts);

            // Groups render in the server's order, not alphabetically.
            var grouped = catalog.Grouped();
            Check("group order honoured",
                grouped.Count == 2 && grouped[0].Key == "Pelvic organs");

            // Auto-detect end to end.
            var existing = new List<StructureAutoDetect.Candidate>
            {
                new StructureAutoDetect.Candidate { ExistingId = "Kidney_R", DicomType = "ORGAN" },
                new StructureAutoDetect.Candidate { ExistingId = "Rectum", DicomType = "ORGAN" },
                new StructureAutoDetect.Candidate { ExistingId = "Liver_old", DicomType = "ORGAN" },
                new StructureAutoDetect.Candidate { ExistingId = "PTV_7000", DicomType = "PTV" },
            };
            StructureAutoDetect.Detection detection =
                StructureAutoDetect.Scan(existing, catalog);

            Check("two matched", detection.Matched.Count() == 2);
            Check("one unmatched and visible", detection.Unmatched.Count() == 1);
            Check("unmatched is the off-convention name",
                detection.Unmatched.First().ExistingId == "Liver_old");
            Check("target volume skipped, not 'unmatched'", detection.Skipped.Count() == 1);

            // Catalog order, so two workstations build the same request.
            Check("ids in catalog order",
                detection.StructureIds.SequenceEqual(
                    new[] { "cads_551.kidney_r", "cads_556.rectum" }));
            Check("minimal model set",
                detection.Models.SequenceEqual(new[] { "cads_551", "cads_556" }));
            Check("summary names the unrecognised count",
                detection.Summary.Contains("1 not recognised"));

            // Two legacy names for the same organ must not ask for it twice.
            var duplicated = new List<StructureAutoDetect.Candidate>
            {
                new StructureAutoDetect.Candidate { ExistingId = "Kidney_R", DicomType = "ORGAN" },
                new StructureAutoDetect.Candidate { ExistingId = "R Kidney", DicomType = "ORGAN" },
            };
            Check("duplicate matches deduplicated",
                StructureAutoDetect.Scan(duplicated, catalog).StructureIds.Count == 1);
        }

        // ------------------------------------------------------------------ //
        //  Fixtures and reporting
        // ------------------------------------------------------------------ //

        private static string Skip(string id, string dicomType)
        {
            var candidate = new StructureAutoDetect.Candidate
            {
                ExistingId = id,
                DicomType = dicomType,
            };
            StructureAutoDetect.Scan(new[] { candidate }, null);
            return candidate.SkipReason;
        }

        private static StructureSnapshot Snapshot(double firstX)
        {
            return new StructureSnapshot
            {
                Schema = StructureSnapshot.CurrentSchema,
                Role = StructureSnapshot.RoleBaseline,
                Structures = new List<SnapshotStructure> { Structure("Kidney_R", firstX) },
            };
        }

        private static SnapshotStructure Structure(string id, double firstX)
        {
            return new SnapshotStructure
            {
                Id = id,
                DicomType = "ORGAN",
                IsEmpty = false,
                Contours = new List<ContourSlice>
                {
                    new ContourSlice
                    {
                        ZIndex = 42,
                        PointsLps = new List<double[]>
                        {
                            new[] { firstX, 10.0, 5.0 },
                            new[] { firstX + 1, 11.0, 5.0 },
                            new[] { firstX + 1, 12.0, 5.0 },
                        },
                    },
                },
            };
        }

        /// <summary>
        /// Clinic protocols: the parts that fail quietly.
        ///
        /// A protocol decides the Id a structure is written under, so a served entry that
        /// breaks Eclipse's 16-character limit or names a structure no model produces must
        /// be visible before a write, not during one. The server refuses to serve either,
        /// which makes these the second line of defence rather than the only one.
        /// </summary>
        private static void ProtocolRules()
        {
            Section("Clinic protocols");

            var catalog = new ModelCatalog
            {
                Version = 1,
                GroupOrder = new List<string> { "Pelvic organs" },
                Models = new List<CatalogModel>
                {
                    new CatalogModel { Key = "cads_556", Kind = "cads", DisplayName = "CADS 556" },
                },
                Structures = new List<CatalogStructure>
                {
                    new CatalogStructure
                    {
                        Id = "cads_556.rectum", DisplayName = "Rectum",
                        Group = "Pelvic organs", SourceModel = "cads_556",
                        Aliases = new List<string> { "rectum" },
                    },
                },
                Protocols = new List<CatalogProtocol>
                {
                    new CatalogProtocol
                    {
                        Key = "pelvis", DisplayName = "Prostate", Site = "Pelvis",
                        Models = new List<string> { "cads_556" },
                        Entries = new List<ProtocolEntry>
                        {
                            new ProtocolEntry
                            {
                                StructureId = "cads_556.rectum", WriteAs = "Rectum",
                                DicomType = "ORGAN", Colour = "#8B5A2B", Required = true,
                            },
                            new ProtocolEntry
                            {
                                StructureId = "cads_556.femur_l", WriteAs = "Femur_L",
                                DicomType = "ORGAN", Required = true,
                            },
                        },
                    },
                },
            };

            Check("catalog reports protocols", catalog.HasProtocols);
            Check("protocol by key", catalog.Protocol("pelvis") != null);
            Check("unknown protocol is null, not a guess", catalog.Protocol("nope") == null);

            IList<ProtocolEntry> available;
            IList<ProtocolEntry> unavailable;
            catalog.SplitEntries(catalog.Protocol("pelvis"), out available, out unavailable);

            Check("producible entry available", available.Count == 1
                && available[0].StructureId == "cads_556.rectum");
            Check("entry with no model is surfaced, not dropped", unavailable.Count == 1
                && unavailable[0].StructureId == "cads_556.femur_l");

            var longId = new ProtocolEntry { WriteAs = "ThisIdIsWayTooLongForEclipse" };
            Check("over 16 characters is rejected, not truncated", longId.SafeWriteAs == null);
            Check("16 characters exactly is fine",
                new ProtocolEntry { WriteAs = "1234567890123456" }.SafeWriteAs != null);
            Check("blank write-as is null",
                new ProtocolEntry { WriteAs = "   " }.SafeWriteAs == null);
            Check("write-as is trimmed",
                new ProtocolEntry { WriteAs = "  Rectum " }.SafeWriteAs == "Rectum");

            byte r, g, b;
            Check("hex colour parses",
                new ProtocolEntry { Colour = "#8B5A2B" }.TryColour(out r, out g, out b)
                && r == 0x8B && g == 0x5A && b == 0x2B);
            Check("hex without the hash parses",
                new ProtocolEntry { Colour = "8B5A2B" }.TryColour(out r, out g, out b));
            Check("bad colour degrades to the palette, never throws",
                !new ProtocolEntry { Colour = "octarine" }.TryColour(out r, out g, out b));
            Check("absent colour degrades to the palette",
                !new ProtocolEntry().TryColour(out r, out g, out b));
            Check("short hex rejected",
                !new ProtocolEntry { Colour = "#FFF" }.TryColour(out r, out g, out b));
        }

        private static void Section(string title)
        {
            Console.WriteLine("-- " + title);
        }

        private static void Check(string what, bool ok)
        {
            _checks++;
            if (!ok)
            {
                _failures++;
                Console.WriteLine("   FAIL  " + what);
            }
            else
            {
                Console.WriteLine("   ok    " + what);
            }
        }
    }
}
