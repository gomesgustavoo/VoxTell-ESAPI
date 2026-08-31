using System;
using System.Collections.Generic;
using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;
using VoxTell_Interface.ViewModels;
using VoxTell_Interface.Views;

namespace VoxTell_Interface.Preview
{
    /// <summary>
    /// Renders the shipping <see cref="MainPanel"/> to a PNG, headless.
    ///
    /// Why this exists
    /// ---------------
    /// The panel's first real run inside Eclipse showed overlapping header cards and
    /// threw a cross-thread exception. Neither is catchable by a compiler or a unit
    /// test — they are properties of a laid-out visual tree. And the only loop
    /// available for fixing them was rebuild, redeploy, re-approve in the Eclipse UI,
    /// reopen a patient: minutes per attempt, for a margin.
    ///
    /// WPF can measure, arrange and rasterise a visual tree with no window and no
    /// display, so this runs over SSH on the build box and hands back an image.
    ///
    /// It renders the real panel with a real <see cref="MainViewModel"/> — only the
    /// <c>ScriptContext</c> is absent — so what appears here is what Eclipse shows.
    ///
    /// Usage: <c>VoxTell-Preview.exe [outdir] [--width 760] [--height 860]</c>
    /// </summary>
    public static class Program
    {
        [STAThread]
        public static int Main(string[] args)
        {
            string outDir = args.Length > 0 && !args[0].StartsWith("--") ? args[0] : ".";
            double width = ArgValue(args, "--width", 760);
            double height = ArgValue(args, "--height", 860);

            Directory.CreateDirectory(outDir);

            try
            {
                // Each state is a separate render, because the defects worth catching
                // are state-dependent: the header only overlaps once the account card
                // has content, and the review list only has rows after a job.
                Render(outDir, "01-signed-out", width, height, SignedOut());
                Render(outDir, "02-ready-autodetected", width, height, Ready());
                Render(outDir, "03-reviewing", width, height, Reviewing());
                Render(outDir, "04-no-catalog", width, height, NoCatalog());
                Render(outDir, "05-structures-mode", width, height, StructuresMode());
                Render(outDir, "06-protocol", width, height, ProtocolMode());
                // The window's MINIMUM, in both dimensions. This is the state that matters
                // now that the panel has no outer scroller: anything refusing to shrink
                // clips here instead of scrolling.
                Render(outDir, "07-minimum", 620, 720, ProtocolMode());
                Render(outDir, "08-minimum-review", 620, 720, Reviewing());
                // And wide, where fixed column widths used to leave dead space.
                Render(outDir, "09-wide-review", 1100, height, Reviewing());
                Console.WriteLine("OK");
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("FAILED:");
                for (Exception e = ex; e != null; e = e.InnerException)
                    Console.Error.WriteLine("  {0}: {1}", e.GetType().Name, e.Message);
                Console.Error.WriteLine(ex.StackTrace);
                return 1;
            }
        }

        private static double ArgValue(string[] args, string name, double fallback)
        {
            for (int i = 0; i < args.Length - 1; i++)
            {
                if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase))
                {
                    double v;
                    if (double.TryParse(args[i + 1], out v)) return v;
                }
            }
            return fallback;
        }

        private static void Render(
            string outDir, string name, double width, double height, MainViewModel vm)
        {
            var panel = new MainPanel(vm, loadCatalog: false);

            // The same size Script.cs gives Eclipse's window.
            panel.Width = width;
            panel.Height = height;
            panel.Background = Theme.Void;

            panel.Measure(new Size(width, height));
            panel.Arrange(new Rect(0, 0, width, height));
            panel.UpdateLayout();

            // 96 DPI: the panel lays out in device-independent units, so this is 1:1
            // with what a 100%-scaling workstation shows.
            var bitmap = new RenderTargetBitmap(
                (int)Math.Ceiling(width), (int)Math.Ceiling(height), 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(panel);

            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));

            string path = Path.Combine(outDir, name + ".png");
            using (var stream = File.Create(path)) encoder.Save(stream);

            Console.WriteLine("  wrote {0}", path);

            // Report anything that overflowed its slot. This is the machine-readable
            // half: an overlap usually shows up first as a child wider or taller than
            // the parent that is meant to contain it.
            ReportOverflow(panel, name);
        }

        /// <summary>
        /// Walk the tree and print elements whose rendered size exceeds their parent's.
        ///
        /// Cheap, and it catches the class of defect a screenshot makes obvious but a
        /// human has to notice. Reported rather than thrown: some overflow is
        /// legitimate (a scroll viewer's content is meant to be taller).
        /// </summary>
        private static void ReportOverflow(FrameworkElement root, string state)
        {
            var offenders = new List<string>();
            Walk(root, null, offenders, 0);
            if (offenders.Count == 0) return;

            Console.WriteLine("  overflow in {0}:", state);
            foreach (string line in offenders) Console.WriteLine("    " + line);
        }

        private static void Walk(
            FrameworkElement element, FrameworkElement parent, List<string> found, int depth)
        {
            if (element == null || depth > 24) return;

            // A scroll host's content is MEANT to be taller than the host, so neither the
            // ScrollViewer nor the presenter inside its template counts as an offender.
            // Without the presenter, every scrollable list in the panel reported itself and
            // buried the one real clip among them.
            if (parent != null
                && element.ActualHeight > parent.ActualHeight + 0.5
                && parent.ActualHeight > 0
                && !(parent is System.Windows.Controls.ScrollViewer)
                && !(parent is System.Windows.Controls.ScrollContentPresenter))
            {
                found.Add(string.Format(
                    "{0} h={1:F0} inside {2} h={3:F0}",
                    element.GetType().Name, element.ActualHeight,
                    parent.GetType().Name, parent.ActualHeight));
            }

            int count = VisualTreeHelper.GetChildrenCount(element);
            for (int i = 0; i < count; i++)
            {
                Walk(VisualTreeHelper.GetChild(element, i) as FrameworkElement,
                     element, found, depth + 1);
            }
        }

        // ---------------------------------------------------------------- states

        private static MainViewModel SignedOut()
        {
            MainViewModel vm = MainViewModel.CreatePreview();
            vm.SeedPreview(
                null, null,
                "No image. Open a plan or structure set with an image first.",
                null, "none open",
                null, null, null,
                WorkflowPhase.SignInRequired,
                "Sign in to continue.");
            return vm;
        }

        private static MainViewModel Ready()
        {
            MainViewModel vm = MainViewModel.CreatePreview();
            vm.SeedPreview(
                "gustavo.formento@rtmedical.com.br",
                "12 of 200 jobs used this month  ·  0/6 in flight",
                "512x512x180  ·  0.98 x 0.98 x 3.00 mm",
                "HU = stored x 1 + -1024",
                "CT_1 Pelvis",
                Catalog(), Detection(), null,
                WorkflowPhase.Ready,
                "Ready.");
            return vm;
        }

        /// <summary>
        /// Every state here is seeded BEFORE the panel is constructed, which is the
        /// order that used to render "Nothing to review yet" over five real rows: the
        /// panel bound its list only from a PropertyChanged notification, and the
        /// notification had already fired. Keep it this way — it is the harder order.
        /// </summary>
        private static MainViewModel Reviewing()
        {
            MainViewModel vm = MainViewModel.CreatePreview();
            vm.SeedPreview(
                "gustavo.formento@rtmedical.com.br",
                "13 of 200 jobs used this month  ·  1/6 in flight",
                "512x512x180  ·  0.98 x 0.98 x 3.00 mm",
                "HU = stored x 1 + -1024",
                "CT_1 Pelvis",
                Catalog(), Detection(), Plans(),
                WorkflowPhase.Reviewing,
                "Review 5 structure(s), then import.");
            // What the structure set already holds, so "replaces X" behaves as it does in
            // Eclipse when a write-as id is edited.
            vm.SeedExistingIds(new[]
                { "Rectum", "Bladder", "Femur_L", "PTV_7000", "Liver_old", "Kidney_R" });
            return vm;
        }

        private static MainViewModel StructuresMode()
        {
            MainViewModel vm = Ready();
            vm.Mode = TargetMode.Structures;
            return vm;
        }

        private static MainViewModel ProtocolMode()
        {
            MainViewModel vm = Ready();
            vm.ApplyProtocol("rt_pelvis_male");
            return vm;
        }

        private static MainViewModel NoCatalog()
        {
            MainViewModel vm = MainViewModel.CreatePreview();
            vm.SeedPreview(
                "gustavo.formento@rtmedical.com.br",
                "12 of 200 jobs used this month  ·  0/6 in flight",
                "512x512x180  ·  0.98 x 0.98 x 3.00 mm",
                "HU = stored x 1 + -1024",
                "CT_1 Pelvis",
                null, null, null,
                WorkflowPhase.Ready,
                "Model list unavailable (connection refused). Free-text prompts still work.");
            return vm;
        }

        // ---------------------------------------------------------------- fixtures

        private static ModelCatalog Catalog()
        {
            return new ModelCatalog
            {
                Version = 1,
                GroupOrder = new List<string>
                {
                    "Radiotherapy structures", "Pelvic organs", "Abdominal organs", "Vertebrae",
                },
                Models = new List<CatalogModel>
                {
                    new CatalogModel { Key = "voxtell", Kind = "prompt",
                        DisplayName = "VoxTell (free-text prompts)",
                        WeightsLicence = "CC-BY-NC-SA-4.0" },
                    new CatalogModel { Key = "cads_551", Kind = "cads", Task = "551",
                        DisplayName = "CADS 551 · Abdominal organs & lung lobes",
                        Count = 17, WeightsVariant = "open", WeightsLicence = "CC-BY-SA-4.0" },
                    new CatalogModel { Key = "cads_552", Kind = "cads", Task = "552",
                        DisplayName = "CADS 552 · Vertebrae",
                        Count = 24, WeightsVariant = "open", WeightsLicence = "CC-BY-SA-4.0" },
                    new CatalogModel { Key = "cads_556", Kind = "cads", Task = "556",
                        DisplayName = "CADS 556 · Radiotherapy structures",
                        Count = 15, WeightsVariant = "open", WeightsLicence = "CC-BY-SA-4.0" },
                },
                Structures = Structures(),
                Protocols = new List<CatalogProtocol>
                {
                    new CatalogProtocol
                    {
                        Key = "rt_pelvis_male", DisplayName = "Prostate (male pelvis)",
                        Site = "Pelvis", Modality = "CT",
                        Models = new List<string> { "cads_556", "cads_553" },
                        Entries = new List<ProtocolEntry>
                        {
                            E("cads_556.rectum", "Rectum", "ORGAN", "#8B5A2B"),
                            E("cads_556.prostate", "Prostate", "ORGAN", "#C77CFF"),
                            E("cads_553.urinary_bladder", "Bladder", "ORGAN", "#F2C14E"),
                            E("cads_556.spinal_canal", "SpinalCanal", "ORGAN", "#5AA9E6"),
                            // Deliberately not in the fixture catalog: this is the row a
                            // clinic must be able to see, and the reason the pane lists
                            // unavailable entries instead of dropping them.
                            E("cads_556.femur_l", "Femur_L", "ORGAN", "#8FBF6F"),
                        },
                    },
                    new CatalogProtocol
                    {
                        Key = "rt_thorax", DisplayName = "Lung (thorax)",
                        Site = "Thorax", Modality = "CT",
                        Models = new List<string> { "cads_556" },
                        Entries = new List<ProtocolEntry>
                        {
                            E("cads_556.heart", "Heart", "ORGAN", "#E06C5C"),
                        },
                    },
                },
                Presets = new List<CatalogPreset>
                {
                    new CatalogPreset { Key = "rt_pelvis", DisplayName = "RT pelvis",
                        StructureIds = new List<string> { "cads_556.rectum", "cads_556.prostate" },
                        Models = new List<string> { "cads_556" } },
                    new CatalogPreset { Key = "rt_thorax", DisplayName = "RT thorax",
                        StructureIds = new List<string> { "cads_556.heart" },
                        Models = new List<string> { "cads_556" } },
                    new CatalogPreset { Key = "rt_abdomen", DisplayName = "RT abdomen",
                        StructureIds = new List<string> { "cads_551.liver" },
                        Models = new List<string> { "cads_551" } },
                },
            };
        }

        private static List<CatalogStructure> Structures()
        {
            return new List<CatalogStructure>
            {
                S("cads_556.rectum", "Rectum", "Radiotherapy structures", "cads_556", "rectum"),
                S("cads_556.prostate", "Prostate", "Radiotherapy structures", "cads_556", "prostate"),
                S("cads_556.heart", "Heart", "Radiotherapy structures", "cads_556", "heart"),
                S("cads_556.spinal_canal", "Spinal canal", "Radiotherapy structures", "cads_556", "spinalcanal"),
                S("cads_556.bowel_space", "Bowel space", "Radiotherapy structures", "cads_556", "bowelspace"),
                S("cads_553.urinary_bladder", "Urinary bladder", "Pelvic organs", "cads_553", "bladder"),
                S("cads_551.liver", "Liver", "Abdominal organs", "cads_551", "liver"),
                S("cads_551.kidney_l", "Kidney L", "Abdominal organs", "cads_551", "kidneyl"),
                S("cads_551.kidney_r", "Kidney R", "Abdominal organs", "cads_551", "kidneyr"),
                S("cads_552.vertebra_l5", "Vertebra L5", "Vertebrae", "cads_552", "l5"),
                S("cads_552.vertebra_l4", "Vertebra L4", "Vertebrae", "cads_552", "l4"),
            };
        }

        private static ProtocolEntry E(
            string structureId, string writeAs, string dicomType, string colour)
        {
            return new ProtocolEntry
            {
                StructureId = structureId,
                WriteAs = writeAs,
                DicomType = dicomType,
                Colour = colour,
                Required = true,
            };
        }

        private static CatalogStructure S(
            string id, string display, string group, string model, string alias)
        {
            return new CatalogStructure
            {
                Id = id, DisplayName = display, Group = group,
                Modality = "CT", SourceModel = model,
                Aliases = new List<string> { alias },
            };
        }

        private static StructureAutoDetect.Detection Detection()
        {
            var existing = new List<StructureAutoDetect.Candidate>
            {
                C("Rectum", "ORGAN", 78.4),
                C("Bladder", "ORGAN", 210.9),
                C("Femur_L", "ORGAN", 190.2),       // unrecognised on purpose
                C("PTV_7000", "PTV", 640.0),        // skipped: a target volume
                C("Liver_old", "CONTROL", 1520.0),  // unrecognised on purpose
                C("Kidney_R", "ORGAN", 160.1),
            };
            return StructureAutoDetect.Scan(existing, Catalog());
        }

        private static StructureAutoDetect.Candidate C(string id, string type, double cc)
        {
            return new StructureAutoDetect.Candidate
            {
                ExistingId = id, DicomType = type, VolumeCc = cc, IsEmpty = false,
            };
        }

        private static List<StructurePlan> Plans()
        {
            // One shared "used" list, exactly as EsapiStructureImporter does it. Passing
            // a fresh empty list per call let three structures collide on the same hue,
            // which misrepresents the palette: the whole point of Assign is that the
            // colours in one review list are distinct.
            var used = new List<Color>();
            return new List<StructurePlan>
            {
                P("Rectum", "Rectum", 78.4, 42, 96, false, used),
                P("Prostate", "Prostate", 46.2, 55, 84, true, used),
                P("Urinary bladder", "urinary_bladder", 214.7, 50, 110, true, used),
                P("Kidney R", "Kidney_R", 160.1, 8, 62, false, used),
                // The row a planner most needs to notice: nothing was segmented, and
                // ticking it would clear an existing structure.
                P("Spinal canal", "SpinalCanal", 0, 0, 0, true, used),
            };
        }

        private static StructurePlan P(
            string prompt, string id, double cc, int first, int last, bool willCreate,
            List<Color> used)
        {
            var occupied = new List<int>();
            for (int z = first; z <= last; z += 1) occupied.Add(z);

            return new StructurePlan
            {
                Prompt = prompt,
                StructureId = id,
                ExistingId = willCreate ? null : id,
                DicomType = "CONTROL",
                Color = Remember(StructurePalette.Assign(prompt, used), used),
                Selected = cc > 0,
                VoxelCount = (long)(cc * 1000 / 2.87),
                VoxelVolumeMm3 = 2.87,
                ContourCount = cc > 0 ? occupied.Count : 0,
                FirstSlice = first,
                LastSlice = last,
                OccupiedSlices = cc > 0 ? occupied.ToArray() : new int[0],
                SeriesSliceCount = 180,
                Note = cc > 0 ? null : "nothing found",
            };
        }
        private static Color Remember(Color colour, List<Color> used)
        {
            used.Add(colour);
            return colour;
        }

    }
}
