using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The review table: one row per segmented structure, ticked to write it in.
    ///
    /// One grid, not one grid per row
    /// -----------------------------
    /// The previous version built a <see cref="Grid"/> per row, so every row resolved its
    /// own <c>Auto</c> and <c>*</c> columns against its own content: the write-as box and
    /// the type control landed at a different x on each row, and the table read as a pile
    /// of misaligned fragments. Pinning the widths to constants stopped the wobble and
    /// bought a new problem — clipping at the 620 px window minimum, dead space when wide.
    ///
    /// Here the header and every row are rows of **one** grid. Alignment is a property of
    /// the layout instead of a set of numbers to maintain, `*` columns work, and the
    /// structure and write-as columns grow with the window.
    ///
    /// Identity, not index
    /// -------------------
    /// Each row's <c>DataContext</c> is its <see cref="StructurePlan"/> and the tick and
    /// the id box are two-way bound to it, so the objects the import reads are the objects
    /// the operator edited. The WinForms version scraped its grid by row index, which
    /// silently attributed one row's decision to another as soon as anything reordered.
    ///
    /// Two things this table states that the previous one could not
    /// -----------------------------------------------------------
    /// * <b>"replaces X" stays true after a rename.</b> <see cref="StructurePlan.ExistingId"/>
    ///   used to be fixed when the plan was built while the write path resolved the edited
    ///   id — so renaming a row onto an existing structure showed "will create" and then
    ///   replaced that structure's contours. Every edit now asks the view model to
    ///   re-resolve the target.
    /// * <b>Two rows writing the same id are called out</b> on both rows. Warned, not
    ///   blocked: it is legal, and the planner may mean it.
    /// </summary>
    internal sealed class ReviewTable
    {
        private static readonly string[] TypeChoices =
            { "CONTROL", "ORGAN", "PTV", "AVOIDANCE" };

        private readonly Grid _table;
        private readonly ScrollViewer _scroller;
        private readonly TextBlock _empty;
        private readonly Grid _root;

        /// <summary>Asks the view model to re-resolve which existing structure a row targets.</summary>
        private readonly Action<StructurePlan> _onIdChanged;

        private readonly List<RowParts> _rows = new List<RowParts>();

        private IList<StructurePlan> _bound;
        private bool _boundOnce;

        private sealed class RowParts
        {
            public StructurePlan Plan;
            public TextBlock Replaces;
            public TextBlock Clash;
        }

        public ReviewTable(Action<StructurePlan> onIdChanged)
        {
            _onIdChanged = onIdChanged;

            _table = new Grid();
            Columns(_table);

            _empty = Ui.Small(
                "Nothing to review yet. Segment a series and the results appear here.")
                .Fg(Theme.InkFaint);
            _empty.Margin = new Thickness(0, Theme.Space2, 0, Theme.Space2);

            _scroller = Controls.Scroll(_table);
            _scroller.VerticalAlignment = VerticalAlignment.Top;

            _root = Ui.Grid("Auto,*", "*",
                _empty.At(0, 0),
                _scroller.At(1, 0));
        }

        public FrameworkElement Element { get { return _root; } }

        /// <summary>
        /// Column widths, in one place.
        ///
        /// Stars with minimums rather than constants: the structure name and the write-as
        /// id are the two columns worth giving space to, and the minimums are what keeps
        /// them legible at the window's 620 px floor. TYPE holds "AVOIDANCE" plus the
        /// dropdown's chevron; FOUND holds "214.7 cc" over "61 slices (50-110)".
        /// </summary>
        private static void Columns(Grid grid)
        {
            Add(grid, new GridLength(26), 0);
            Add(grid, new GridLength(18), 0);
            Add(grid, new GridLength(2, GridUnitType.Star), 130);
            // Capped: an id box is 16 characters wide at most, so beyond ~220 px the extra
            // space belongs to the structure name.
            Add(grid, new GridLength(1.3, GridUnitType.Star), 112, 220);
            Add(grid, GridLength.Auto, 104);
            Add(grid, GridLength.Auto, 106);
        }

        private static void Add(
            Grid grid, GridLength width, double min, double max = double.PositiveInfinity)
        {
            grid.ColumnDefinitions.Add(
                new ColumnDefinition { Width = width, MinWidth = min, MaxWidth = max });
        }

        /// <summary>
        /// Rebuild from a plan list.
        ///
        /// A full rebuild rather than a diff: tens of rows, once per job, and a rebuild
        /// cannot leave a stale row bound to a plan that no longer exists. Compared by
        /// reference because the view model replaces the list wholesale per job.
        ///
        /// <c>_boundOnce</c> is not redundant: the first call arrives with
        /// <paramref name="plans"/> null and <c>_bound</c> null, and a bare reference check
        /// reads that as "already rendered" — which is how the header once stood over an
        /// empty list.
        /// </summary>
        public void Bind(IList<StructurePlan> plans)
        {
            if (_boundOnce && ReferenceEquals(_bound, plans))
            {
                Reconcile();
                return;
            }

            Unhook();
            _bound = plans;
            _boundOnce = true;

            _table.Children.Clear();
            _table.RowDefinitions.Clear();
            _rows.Clear();

            bool any = plans != null && plans.Count > 0;
            _empty.Show(!any);
            _scroller.Show(any);
            if (!any) return;

            AppendHeader();
            for (int i = 0; i < plans.Count; i++) AppendRow(plans[i], i == plans.Count - 1);
            Reconcile();
        }

        private void Unhook()
        {
            foreach (RowParts row in _rows)
            {
                row.Plan.PropertyChanged -= OnPlanChanged;
            }
        }

        // --- rows ------------------------------------------------------------------- //

        private void AppendHeader()
        {
            int r = NewRow(Theme.SizeMicro + 8);

            Cell(Ui.Micro("STRUCTURE").Fg(Theme.InkMuted), r, 2);
            Cell(Ui.Micro("WRITE AS").Fg(Theme.InkMuted), r, 3);
            Cell(Ui.Micro("TYPE").Fg(Theme.InkMuted), r, 4);
            Cell(Ui.Micro("FOUND").Fg(Theme.InkMuted).Right(), r, 5);

            var rule = new Border
            {
                Height = 1,
                Background = Theme.Edge,
                VerticalAlignment = VerticalAlignment.Bottom,
                SnapsToDevicePixels = true,
            };
            Grid.SetRow(rule, r);
            Grid.SetColumn(rule, 0);
            Grid.SetColumnSpan(rule, 6);
            _table.Children.Add(rule);
        }

        private void AppendRow(StructurePlan plan, bool last)
        {
            int r = NewRow(Theme.RowHeight);

            // Background first, so it sits behind the cells and cannot swallow their
            // clicks. Hover is a Style trigger: the row is not a Control, and hand-wiring
            // MouseEnter/MouseLeave on a table is how a stuck highlight happens.
            var background = new Border
            {
                Background = Brushes.Transparent,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(0, 0, 0, last ? 0 : 1),
                SnapsToDevicePixels = true,
            };
            var hover = new Style(typeof(Border));
            var trigger = new Trigger
            {
                Property = UIElement.IsMouseOverProperty,
                Value = true,
            };
            trigger.Setters.Add(new Setter(Border.BackgroundProperty, Theme.Hover));
            hover.Triggers.Add(trigger);
            background.Style = hover;
            Grid.SetRow(background, r);
            Grid.SetColumn(background, 0);
            Grid.SetColumnSpan(background, 6);
            _table.Children.Add(background);

            var parts = new RowParts { Plan = plan };

            // --- tick + swatch
            var tick = Controls.Tick("Selected", "Write this structure into the structure set");
            tick.DataContext = plan;
            Cell(tick, r, 0);

            Border swatch = Controls.Swatch(plan.Color);
            Cell(swatch, r, 1);

            // --- name, and what it will do to the structure set
            TextBlock name = Ui.Text(plan.Prompt ?? string.Empty);
            name.TextTrimming = TextTrimming.CharacterEllipsis;
            name.ToolTip = plan.Prompt;

            parts.Replaces = Ui.Micro(string.Empty).Fg(Theme.Warn);
            parts.Clash = Ui.Micro(string.Empty).Fg(Theme.Warn);

            StackPanel label = Ui.Stack(0, name, parts.Replaces, parts.Clash);
            label.VerticalAlignment = VerticalAlignment.Center;
            Cell(label, r, 2);

            // --- write-as id
            TextBox idBox;
            Border idInput = Ui.Input(out idBox, maxLength: 16);
            idBox.DataContext = plan;
            idBox.SetBinding(TextBox.TextProperty,
                new Binding("StructureId")
                {
                    Mode = BindingMode.TwoWay,
                    UpdateSourceTrigger = UpdateSourceTrigger.PropertyChanged,
                });
            idBox.ToolTip = "Eclipse allows 16 characters.";
            idInput.VerticalAlignment = VerticalAlignment.Center;
            Cell(idInput, r, 3);

            // --- DICOM type: a real dropdown, defaulted by the plan rather than always
            //     CONTROL. It applies only when the structure is created.
            StructurePlan captured = plan;
            Sel type = Controls.Select(
                TypeChoices,
                plan.DicomType ?? "CONTROL",
                value => { captured.DicomType = value; },
                compact: true);
            type.Element.ToolTip =
                "DICOM structure type. Applied only when the structure is created.";
            type.Element.VerticalAlignment = VerticalAlignment.Center;
            Cell(type.Element, r, 4);

            // --- what came back
            Cell(Detail(plan), r, 5);

            plan.PropertyChanged += OnPlanChanged;
            _rows.Add(parts);
        }

        private int NewRow(double minHeight)
        {
            _table.RowDefinitions.Add(
                new RowDefinition { Height = GridLength.Auto, MinHeight = minHeight });
            return _table.RowDefinitions.Count - 1;
        }

        private void Cell(FrameworkElement element, int row, int column)
        {
            Thickness m = element.Margin;
            element.Margin = new Thickness(
                m.Left, m.Top + 3, m.Right + (column == 5 ? 0 : Theme.Space2), m.Bottom + 3);
            Grid.SetRow(element, row);
            Grid.SetColumn(element, column);
            _table.Children.Add(element);
        }

        private static FrameworkElement Detail(StructurePlan plan)
        {
            if (plan.IsEmpty)
            {
                // Not an error, and it must not be dressed as one — but it is the row a
                // planner most needs to notice, because ticking it would clear an existing
                // structure.
                TextBlock warn = Ui.Small(
                    plan.HasVoxelsButNoContours ? "voxels but no contours" : "nothing found")
                    .Fg(Theme.Warn).Right();
                warn.TextWrapping = TextWrapping.Wrap;
                warn.TextTrimming = TextTrimming.None;
                return warn;
            }

            string extent = plan.SeriesSliceCount > 0
                ? string.Format("{0} slices ({1}-{2})",
                    plan.OccupiedSlices != null ? plan.OccupiedSlices.Length : 0,
                    plan.FirstSlice, plan.LastSlice)
                : plan.ContourCount + " contours";

            StackPanel detail = Ui.Stack(0,
                Ui.Small(string.Format("{0:N1} cc", plan.VolumeCc)).Tabular().Right(),
                Ui.Micro(extent).Fg(Theme.InkFaint).Right());
            detail.HorizontalAlignment = HorizontalAlignment.Right;
            return detail;
        }

        // --- live state ------------------------------------------------------------- //

        private void OnPlanChanged(object sender, PropertyChangedEventArgs e)
        {
            if (e.PropertyName == "StructureId")
            {
                var plan = sender as StructurePlan;
                // Ask the view model which existing structure this id now hits, before
                // redrawing the line that tells the operator.
                if (plan != null && _onIdChanged != null) _onIdChanged(plan);
            }

            if (e.PropertyName == "StructureId"
                || e.PropertyName == "ExistingId"
                || e.PropertyName == "Selected")
            {
                Reconcile();
            }
        }

        /// <summary>Redraw the two per-row warnings from the current plan state.</summary>
        private void Reconcile()
        {
            var counts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            foreach (RowParts row in _rows)
            {
                if (!row.Plan.Selected) continue;
                string id = (row.Plan.StructureId ?? string.Empty).Trim();
                if (id.Length == 0) continue;
                int seen;
                counts[id] = counts.TryGetValue(id, out seen) ? seen + 1 : 1;
            }

            foreach (RowParts row in _rows)
            {
                bool replaces = !row.Plan.WillCreate;
                row.Replaces.Text = replaces ? "replaces " + row.Plan.ExistingId : string.Empty;
                row.Replaces.Show(replaces);

                string id = (row.Plan.StructureId ?? string.Empty).Trim();
                int seen;
                bool clash = row.Plan.Selected
                    && id.Length > 0
                    && counts.TryGetValue(id, out seen)
                    && seen > 1;
                row.Clash.Text = clash ? "another ticked row writes this id too" : string.Empty;
                row.Clash.Show(clash);
            }
        }
    }
}
