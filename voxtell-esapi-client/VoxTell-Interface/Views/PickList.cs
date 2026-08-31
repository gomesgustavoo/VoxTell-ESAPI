using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Input;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The structure catalog as a list you can actually work in: filtered, grouped,
    /// collapsible and virtualised.
    ///
    /// What it replaces
    /// ----------------
    /// A <see cref="StackPanel"/> into which all 167 rows were materialised at
    /// construction, inside a 190 px scroller, with no filter and no way to fold a group
    /// away. Finding "Vertebra L4" meant scrolling a list of every structure the
    /// deployment can produce, and the whole list was built before the panel's first
    /// paint whether or not the planner ever opened that tab.
    ///
    /// Three deliberate choices
    /// ------------------------
    /// * **One flat item list, two row shapes.** A group header and a structure are the
    ///   same <see cref="Row"/> type with <see cref="Row.IsHeader"/> deciding which half
    ///   of the template shows. That keeps the item source flat, which is what lets a
    ///   <see cref="VirtualizingStackPanel"/> virtualise it — WPF grouping needs
    ///   <c>IsVirtualizingWhenGrouping</c> and brings a CollectionView with it, for a
    ///   hierarchy exactly two levels deep.
    /// * **Row objects are created once** and re-filtered, never rebuilt. A rebuild would
    ///   drop and re-wire a change handler per row on every keystroke in the filter box.
    /// * **The tick is two-way bound to the row**, as in the review list: the panel never
    ///   reads a selection back out of a control by index.
    /// </summary>
    internal sealed class PickList
    {
        /// <summary>One line: a group header, or a structure with a tick.</summary>
        public sealed class Row : INotifyPropertyChanged
        {
            private bool _selected;

            public bool IsHeader { get; set; }
            public bool IsStructure { get { return !IsHeader; } }

            /// <summary>Group name for a header, display name for a structure.</summary>
            public string Text { get; set; }

            /// <summary>Catalog id, null on a header. Also the tooltip: the same organ can
            /// come from two models and the id is what tells them apart.</summary>
            public string Id { get; set; }

            public string Group { get; set; }

            /// <summary>Collapse marker drawn on a header.</summary>
            public string Marker { get; set; }

            public bool Selected
            {
                get { return _selected; }
                set
                {
                    if (_selected == value) return;
                    _selected = value;
                    Raise("Selected");
                }
            }

            public event PropertyChangedEventHandler PropertyChanged;

            private void Raise(string name)
            {
                PropertyChangedEventHandler handler = PropertyChanged;
                if (handler != null) handler(this, new PropertyChangedEventArgs(name));
            }
        }

        private readonly ItemsControl _items;
        private readonly ScrollViewer _scroller;
        private readonly TextBlock _empty;
        private readonly Grid _root;

        private readonly Action<string, bool> _onToggle;
        private readonly Func<string, bool> _isSelected;

        private readonly List<Row> _all = new List<Row>();
        private readonly Dictionary<string, Row> _byId =
            new Dictionary<string, Row>(StringComparer.Ordinal);
        private readonly HashSet<string> _collapsed = new HashSet<string>(StringComparer.Ordinal);

        private string _filter = string.Empty;
        private bool _suppress;

        public PickList(Func<string, bool> isSelected, Action<string, bool> onToggle)
        {
            _isSelected = isSelected;
            _onToggle = onToggle;

            _items = new ItemsControl
            {
                ItemsPanel = VirtualPanel(),
                ItemTemplate = RowTemplate(),
                Focusable = false,
            };
            VirtualizingPanel.SetIsVirtualizing(_items, true);
            VirtualizingPanel.SetVirtualizationMode(_items, VirtualizationMode.Recycling);

            _scroller = Controls.Scroll(_items);
            // Item-based scrolling, which is what actually enables virtualisation: with
            // pixel scrolling the panel measures every item to know the extent.
            _scroller.CanContentScroll = true;

            _empty = Ui.Small("Nothing matches that filter.").Fg(Theme.InkFaint);
            _empty.Margin = new Thickness(0, Theme.Space2, 0, Theme.Space2);
            _empty.Show(false);

            // A minimum, because a `*` row squeezed by everything around it can otherwise
            // resolve to a sliver showing nothing but the column header — which is what a
            // 620x640 render caught. The window's own MinHeight keeps this satisfiable.
            _root = Ui.Grid("*,Auto", "*",
                _scroller.At(0, 0),
                _empty.At(1, 0));
            _root.MinHeight = 140;
        }

        public FrameworkElement Element { get { return _root; } }

        /// <summary>Rows currently listed, headers included.</summary>
        public int VisibleCount { get { return _items.Items.Count; } }

        /// <summary>
        /// Build the rows from the catalog's own grouping. Idempotent: calling it with the
        /// same catalog does nothing, so it is safe on the render path.
        /// </summary>
        public void Build(IList<KeyValuePair<string, IList<CatalogStructure>>> groups)
        {
            _all.Clear();
            _byId.Clear();

            foreach (var group in groups)
            {
                _all.Add(new Row
                {
                    IsHeader = true,
                    Text = group.Key.ToUpperInvariant(),
                    Group = group.Key,
                });

                foreach (CatalogStructure structure in group.Value)
                {
                    var row = new Row
                    {
                        IsHeader = false,
                        Text = structure.DisplayName,
                        Id = structure.Id,
                        Group = group.Key,
                    };
                    row.PropertyChanged += OnRowChanged;
                    _all.Add(row);
                    _byId[structure.Id] = row;
                }
            }

            Apply();
        }

        /// <summary>Filter on the structure's display name and id, case-insensitively.</summary>
        public void Filter(string text)
        {
            string next = (text ?? string.Empty).Trim();
            if (string.Equals(next, _filter, StringComparison.OrdinalIgnoreCase)) return;
            _filter = next;
            Apply();
        }

        /// <summary>Push the view model's selection into the ticks.</summary>
        public void RefreshTicks()
        {
            _suppress = true;
            try
            {
                foreach (Row row in _all)
                {
                    if (row.IsHeader || row.Id == null) continue;
                    row.Selected = _isSelected(row.Id);
                }
            }
            finally
            {
                _suppress = false;
            }
        }

        // ------------------------------------------------------------------- //

        private void OnRowChanged(object sender, PropertyChangedEventArgs e)
        {
            if (_suppress || e.PropertyName != "Selected") return;
            var row = sender as Row;
            if (row == null || row.Id == null || _onToggle == null) return;
            _onToggle(row.Id, row.Selected);
        }

        /// <summary>
        /// Rebuild the visible item list from the filter and the collapsed groups.
        ///
        /// A group header is listed only when the group has a matching structure, so
        /// filtering does not leave a column of empty headings — and while a filter is
        /// active, collapse is ignored: the planner asked to see matches.
        /// </summary>
        private void Apply()
        {
            bool filtering = _filter.Length > 0;
            var visible = new List<Row>();
            int matches = 0;

            for (int i = 0; i < _all.Count; i++)
            {
                Row row = _all[i];
                if (!row.IsHeader) continue;

                var kept = new List<Row>();
                for (int j = i + 1; j < _all.Count && !_all[j].IsHeader; j++)
                {
                    if (Matches(_all[j])) kept.Add(_all[j]);
                }
                if (kept.Count == 0) continue;

                matches += kept.Count;
                bool folded = !filtering && _collapsed.Contains(row.Group);
                // The count belongs on the header: it is what tells a planner whether a
                // folded group is worth opening.
                row.Marker = (folded ? "\u25b8  " : "\u25be  ")
                    + row.Text + "   " + kept.Count;
                visible.Add(row);
                if (!folded) visible.AddRange(kept);
            }

            _items.ItemsSource = visible;
            _empty.Show(matches == 0);
            _scroller.Show(matches > 0);
        }

        private bool Matches(Row row)
        {
            if (_filter.Length == 0) return true;
            return Contains(row.Text, _filter) || Contains(row.Id, _filter);
        }

        private static bool Contains(string haystack, string needle)
        {
            return haystack != null
                && haystack.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private void ToggleGroup(string group)
        {
            if (group == null) return;
            if (!_collapsed.Remove(group)) _collapsed.Add(group);
            Apply();
        }

        // --- the item template ------------------------------------------------------- //

        private static ItemsPanelTemplate VirtualPanel()
        {
            var panel = new FrameworkElementFactory(typeof(VirtualizingStackPanel));
            var template = new ItemsPanelTemplate(panel);
            template.Seal();
            return template;
        }

        private DataTemplate RowTemplate()
        {
            var visible = new BooleanToVisibilityConverter();

            var grid = new FrameworkElementFactory(typeof(Grid));

            // Two rows of the same shape rather than two templates: a DataTemplateSelector
            // would be a class and a cast for a two-case decision.
            var header = new FrameworkElementFactory(typeof(TextBlock), "Header");
            header.SetValue(TextBlock.FontFamilyProperty, Theme.UiFamily);
            header.SetValue(TextBlock.FontSizeProperty, Theme.SizeMicro);
            header.SetValue(TextBlock.ForegroundProperty, Theme.InkMuted);
            header.SetValue(FrameworkElement.MarginProperty,
                new Thickness(0, Theme.Space2, 0, 2));
            header.SetValue(FrameworkElement.CursorProperty, Cursors.Hand);
            header.SetBinding(TextBlock.TextProperty, new Binding("Marker"));
            header.SetBinding(UIElement.VisibilityProperty,
                new Binding("IsHeader") { Converter = visible });
            header.AddHandler(UIElement.MouseLeftButtonUpEvent,
                new MouseButtonEventHandler(OnHeaderClicked));
            grid.AppendChild(header);

            var body = new FrameworkElementFactory(typeof(Grid));
            body.SetBinding(UIElement.VisibilityProperty,
                new Binding("IsStructure") { Converter = visible });
            body.SetValue(FrameworkElement.HeightProperty, Theme.ListRowHeight);

            var tick = new FrameworkElementFactory(typeof(ToggleButton));
            tick.SetValue(Control.TemplateProperty, TemplateFactory.Tick);
            tick.SetValue(FrameworkElement.HorizontalAlignmentProperty,
                HorizontalAlignment.Left);
            tick.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            tick.SetValue(FrameworkElement.CursorProperty, Cursors.Hand);
            tick.SetBinding(ToggleButton.IsCheckedProperty,
                new Binding("Selected") { Mode = BindingMode.TwoWay });
            body.AppendChild(tick);

            var label = new FrameworkElementFactory(typeof(TextBlock));
            label.SetValue(TextBlock.FontFamilyProperty, Theme.UiFamily);
            label.SetValue(TextBlock.FontSizeProperty, Theme.SizeSmall);
            label.SetValue(TextBlock.ForegroundProperty, Theme.Ink);
            label.SetValue(TextBlock.TextTrimmingProperty, TextTrimming.CharacterEllipsis);
            label.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            label.SetValue(FrameworkElement.MarginProperty, new Thickness(24, 0, 0, 0));
            label.SetBinding(TextBlock.TextProperty, new Binding("Text"));
            label.SetBinding(FrameworkElement.ToolTipProperty, new Binding("Id"));
            body.AppendChild(label);

            grid.AppendChild(body);

            var template = new DataTemplate(typeof(Row)) { VisualTree = grid };
            template.Seal();
            return template;
        }

        private void OnHeaderClicked(object sender, MouseButtonEventArgs e)
        {
            var element = sender as FrameworkElement;
            if (element == null) return;
            var row = element.DataContext as Row;
            if (row != null && row.IsHeader) ToggleGroup(row.Group);
        }
    }
}
