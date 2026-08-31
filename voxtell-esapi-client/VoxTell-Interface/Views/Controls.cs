using System;
using System.Collections;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Media;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The interactive half of the design system: buttons, tick boxes and scroll
    /// hosts, built on the templates in <see cref="TemplateFactory"/>.
    ///
    /// <see cref="Ui"/> covers layout and text — things with no behaviour. This file
    /// covers the controls, and it exists separately for one reason: everything here
    /// is a real WPF <see cref="Control"/>, which means it carries keyboard focus,
    /// Space/Enter activation, <c>IsEnabled</c> semantics and an automation peer.
    /// In a panel where a tick decides whether contours are written into a patient,
    /// none of that is optional, and it is exactly what hand-rolling a "button" out
    /// of a <see cref="Border"/> plus mouse handlers silently throws away.
    /// </summary>
    internal static class Controls
    {
        /// <summary>A flat button. <paramref name="onClick"/> may be null.</summary>
        public static Button Button(string caption, RoutedEventHandler onClick = null)
        {
            var button = new Button
            {
                Content = Ui.Text(caption),
                Template = TemplateFactory.Button,
                Padding = Theme.ButtonPadding,
                MinHeight = Theme.ButtonHeight,
                Focusable = true,
                Cursor = System.Windows.Input.Cursors.Hand,
            };
            if (onClick != null) button.Click += onClick;
            return button;
        }

        /// <summary>
        /// The primary action of a card. Same template as any other button — the
        /// emphasis is weight and position, not colour.
        ///
        /// Deliberately not tinted. The palette rule is that the only saturated
        /// pixels on screen belong to a structure colour or a state, because the
        /// planner reads the swatches here and then looks for those colours on the
        /// CT. A coloured button competes with clinical data for attention.
        /// </summary>
        public static Button Primary(string caption, RoutedEventHandler onClick = null)
        {
            Button button = Button(caption, onClick);
            var content = button.Content as TextBlock;
            if (content != null)
            {
                content.FontFamily = Theme.UiSemiboldFamily;
                content.FontWeight = Theme.SemiboldWeight;
            }
            return button;
        }

        /// <summary>
        /// A tick box, optionally two-way bound to a property on the DataContext.
        ///
        /// The binding is the point. The WinForms review list this replaces scraped
        /// its grid back by row index to discover what the operator had ticked,
        /// which silently attributes one row's decision to another the moment the
        /// rows are reordered — in a panel that writes contours into a patient. A
        /// two-way binding to the row's own object makes that class of bug
        /// unrepresentable rather than merely absent.
        /// </summary>
        public static ToggleButton Tick(string bindingPath = null, string tooltip = null)
        {
            var tick = new ToggleButton
            {
                Template = TemplateFactory.Tick,
                Focusable = true,
                VerticalAlignment = VerticalAlignment.Center,
                Cursor = System.Windows.Input.Cursors.Hand,
            };
            if (bindingPath != null)
            {
                tick.SetBinding(ToggleButton.IsCheckedProperty,
                    new Binding(bindingPath) { Mode = BindingMode.TwoWay });
            }
            if (tooltip != null) tick.ToolTip = tooltip;
            return tick;
        }

        /// <summary>
        /// A label that behaves like a link. Used for the verification URL and the
        /// web comparison, and nothing else.
        /// </summary>
        public static TextBlock Link(string caption, Action onClick)
        {
            TextBlock text = Ui.Text(caption).Fg(Theme.Steel);
            text.Cursor = System.Windows.Input.Cursors.Hand;
            text.TextDecorations = TextDecorations.Underline;
            if (onClick != null)
            {
                text.MouseLeftButtonUp += (s, e) => onClick();
            }
            return text;
        }

        /// <summary>
        /// A vertical scroll host with the themed scrollbar.
        ///
        /// The horizontal scrollbar is off rather than automatic: the review list
        /// wraps and elides instead of scrolling sideways, because a row whose note
        /// is off-screen to the right is a row nobody reads.
        /// </summary>
        public static ScrollViewer Scroll(UIElement child)
        {
            var scroller = new ScrollViewer
            {
                Content = child,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Background = Brushes.Transparent,
                Focusable = false,
                Padding = new Thickness(0),
            };

            // The scrollbar template is applied through an implicit style rather than
            // per-instance, because a ScrollViewer creates its own scrollbars inside
            // its template and they are not reachable from here.
            var style = new Style(typeof(ScrollBar));
            style.Setters.Add(new Setter(ScrollBar.TemplateProperty, TemplateFactory.ScrollBar));
            scroller.Resources.Add(typeof(ScrollBar), style);

            return scroller;
        }

        /// <summary>
        /// A small square of a structure's colour.
        ///
        /// This is clinical data, not decoration: it is the colour the planner will
        /// look for on the CT, which is the whole reason the surrounding chrome is
        /// kept chromatically quiet.
        /// </summary>
        public static Border Swatch(Color colour, double size = 12)
        {
            return new Border
            {
                Width = size,
                Height = size,
                CornerRadius = new CornerRadius(2),
                Background = Theme.BrushFor(colour),
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
                SnapsToDevicePixels = true,
            };
        }

        /// <summary>
        /// A one-line progress track that survives a resize.
        ///
        /// The previous version set the fill's <c>Width</c> from
        /// <c>track.ActualWidth * progress</c> inside the render pass. That is 0 on the first
        /// layout — so the first progress tick drew nothing — and stale after every resize,
        /// because nothing recomputes it when the width changes. Two star-weighted grid
        /// columns state the fraction directly and let the layout system do the arithmetic.
        /// </summary>
        public static Track Progress()
        {
            return new Track();
        }

        // --- new in 2.2: the components the panel was missing -------------------------- //

        /// <summary>
        /// A borderless button. For the step rail and the account chip, where a bordered
        /// button would put a box around every word in the header.
        /// </summary>
        public static Button Ghost(string caption, RoutedEventHandler onClick = null)
        {
            var button = new Button
            {
                Content = Ui.Small(caption),
                Template = TemplateFactory.Ghost,
                Padding = new Thickness(Theme.Space1, 2, Theme.Space1, 2),
                MinHeight = 0,
                Focusable = true,
                Cursor = System.Windows.Input.Cursors.Hand,
            };
            if (onClick != null) button.Click += onClick;
            return button;
        }

        /// <summary>One choice in a <see cref="Select"/>. <c>ToString</c> is what the
        /// list and the closed field render, so no DataTemplate is needed.</summary>
        public sealed class Option
        {
            public Option(string text, object value)
            {
                Text = text;
                Value = value;
            }

            public string Text { get; private set; }
            public object Value { get; private set; }

            public override string ToString() { return Text ?? string.Empty; }
        }

        /// <summary>
        /// A real dropdown.
        ///
        /// <paramref name="onChanged"/> fires only for a user's choice: setting
        /// <see cref="Sel.Value"/> from the render path suppresses it, so re-rendering the
        /// panel cannot look like the operator changing a DICOM type.
        /// </summary>
        public static Sel Select(
            IEnumerable<Option> options, object selected, Action<object> onChanged,
            bool compact = false)
        {
            return new Sel(options, selected, onChanged, compact);
        }

        /// <summary>A dropdown over plain strings.</summary>
        public static Sel Select(
            IEnumerable<string> options, string selected, Action<string> onChanged,
            bool compact = false)
        {
            var wrapped = new List<Option>();
            foreach (string text in options) wrapped.Add(new Option(text, text));
            return new Sel(wrapped, selected, v => onChanged(v as string), compact);
        }

        /// <summary>
        /// A mutually exclusive tab strip.
        ///
        /// <see cref="RadioButton"/>-style semantics from <see cref="ToggleButton"/>s in a
        /// group: real checked state, keyboard focus and an automation peer, which two
        /// plain buttons whose caption changed colour did not have.
        /// </summary>
        public static Segments Segmented(Action<int> onPick)
        {
            return new Segments(onPick);
        }

        /// <summary>
        /// A filter box with the hint inside it.
        ///
        /// The hint is an overlay rather than a caption above the box, which is what makes
        /// this one line instead of two — the reason the free-text pane used to cost 140 px
        /// to hold two words.
        /// </summary>
        public static Border Search(out TextBox box, string hint, Action<string> onChanged)
        {
            Border shell = Ui.Input(out box);
            TextBox inner = box;
            shell.Padding = new Thickness(Theme.Space2, 3, Theme.Space1, 3);

            // Ui.Input has already parented the box in the border, and WPF refuses to add
            // an element that still has a logical parent. Detach before re-hosting it in
            // the grid that carries the watermark and the clear button.
            shell.Child = null;

            TextBlock watermark = Ui.Small(hint).Fg(Theme.InkFaint);
            watermark.IsHitTestVisible = false;
            watermark.VerticalAlignment = VerticalAlignment.Center;

            Button clear = Ghost("\u00d7", (s, e) => { inner.Clear(); inner.Focus(); });
            clear.ToolTip = "Clear the filter";
            clear.Show(false);

            var grid = Ui.Grid("Auto", "*,Auto",
                inner.At(0, 0),
                watermark.At(0, 0),
                clear.At(0, 1));
            shell.Child = grid;

            inner.TextChanged += (s, e) =>
            {
                bool empty = string.IsNullOrEmpty(inner.Text);
                watermark.Show(empty);
                clear.Show(!empty);
                if (onChanged != null) onChanged(inner.Text);
            };
            return shell;
        }

        /// <summary>
        /// A removable token. Used for the current selection, so "what will run" reads
        /// without scrolling a 167-row list.
        /// </summary>
        public static Border Chip(string text, Action onRemove = null)
        {
            TextBlock label = Ui.Small(text).Fg(Theme.Ink);
            label.TextTrimming = TextTrimming.CharacterEllipsis;
            label.MaxWidth = 190;
            label.ToolTip = text;

            StackPanel content = Ui.Row(Theme.Space1, label);
            if (onRemove != null)
            {
                Button drop = Ghost("\u00d7", (s, e) => onRemove());
                drop.ToolTip = "Remove " + text;
                drop.Padding = new Thickness(2, 0, 2, 0);
                content.Append(drop, Theme.Space1);
            }

            return new Border
            {
                Background = Theme.Raised,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(9),
                Padding = new Thickness(Theme.Space2, 1, Theme.Space1, 1),
                SnapsToDevicePixels = true,
                Child = content,
            };
        }

        /// <summary>
        /// A small uppercase pill. States a fact about the run — which half of the
        /// two-run workflow this is — and is never a status colour.
        /// </summary>
        public static Border Pill(string text, Brush ink = null)
        {
            TextBlock label = Ui.Micro(text.ToUpperInvariant()).Fg(ink ?? Theme.InkMuted);
            label.FontFamily = Theme.UiSemiboldFamily;
            label.FontWeight = Theme.SemiboldWeight;

            return new Border
            {
                Background = Theme.Raised,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(3),
                Padding = Theme.PillPadding,
                SnapsToDevicePixels = true,
                VerticalAlignment = VerticalAlignment.Center,
                Child = label,
            };
        }

        /// <summary>One entry of a <see cref="MenuButton"/>.</summary>
        public sealed class Entry
        {
            public Entry(string text, Action onClick, bool enabled = true)
            {
                Text = text;
                OnClick = onClick;
                Enabled = enabled;
            }

            public string Text { get; private set; }
            public Action OnClick { get; private set; }
            public bool Enabled { get; private set; }
        }

        /// <summary>
        /// A caption that opens a menu.
        ///
        /// This is what shrinks the header: sign-out, the server/API-key pane and the
        /// account address were three permanently visible controls in a card of their own.
        /// A real <see cref="ContextMenu"/> brings keyboard navigation and dismissal with
        /// it, which a hand-rolled panel would not.
        /// </summary>
        public static Button MenuButton(string caption, IEnumerable<Entry> entries)
        {
            var menu = new ContextMenu
            {
                Template = TemplateFactory.Menu,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                HasDropShadow = false,
                Placement = PlacementMode.Bottom,
            };
            menu.Effect = Theme.OverlayShadow;

            foreach (Entry entry in entries)
            {
                Entry captured = entry;
                var item = new MenuItem
                {
                    Header = Ui.Text(captured.Text),
                    Template = TemplateFactory.MenuEntry,
                    IsEnabled = captured.Enabled && captured.OnClick != null,
                };
                if (captured.OnClick != null)
                {
                    item.Click += (s, e) => captured.OnClick();
                }
                else
                {
                    // A label, not a command: the address it shows is the point.
                    var header = item.Header as TextBlock;
                    if (header != null) header.Foreground = Theme.InkFaint;
                }
                menu.Items.Add(item);
            }

            Button button = Ghost(caption);
            button.ContextMenu = menu;
            button.Click += (s, e) =>
            {
                menu.PlacementTarget = button;
                menu.IsOpen = true;
            };
            return button;
        }
    }

    /// <summary>A progress track whose fill is a grid weight rather than a pixel width.</summary>
    internal sealed class Track
    {
        private readonly ColumnDefinition _done;
        private readonly ColumnDefinition _todo;
        private readonly Border _root;

        public Track()
        {
            _done = new ColumnDefinition { Width = new GridLength(0, GridUnitType.Star) };
            _todo = new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) };

            var fill = new Border
            {
                Background = Theme.InkMuted,
                CornerRadius = new CornerRadius(2),
            };
            Grid.SetColumn(fill, 0);

            var grid = new Grid();
            grid.ColumnDefinitions.Add(_done);
            grid.ColumnDefinitions.Add(_todo);
            grid.Children.Add(fill);

            _root = new Border
            {
                Height = 4,
                Background = Theme.Edge,
                CornerRadius = new CornerRadius(2),
                SnapsToDevicePixels = true,
                Child = grid,
            };
        }

        public Border Element { get { return _root; } }

        public void Set(double fraction)
        {
            double p = fraction < 0 ? 0 : (fraction > 1 ? 1 : fraction);
            _done.Width = new GridLength(p, GridUnitType.Star);
            _todo.Width = new GridLength(1 - p, GridUnitType.Star);
        }
    }

    /// <summary>A themed dropdown. See <see cref="Controls.Select(IEnumerable{Controls.Option}, object, Action{object}, bool)"/>.</summary>
    internal sealed class Sel
    {
        private readonly ComboBox _combo;
        private readonly Action<object> _onChanged;
        private bool _suppress;

        internal Sel(
            IEnumerable<Controls.Option> options, object selected, Action<object> onChanged,
            bool compact)
        {
            _onChanged = onChanged;

            _combo = new ComboBox
            {
                Template = TemplateFactory.Select,
                FontFamily = Theme.UiFamily,
                FontSize = compact ? Theme.SizeSmall : Theme.SizeBody,
                Foreground = Theme.Ink,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                MinHeight = compact ? Theme.ControlHeightSmall : Theme.ButtonHeight,
                MaxDropDownHeight = 220,
                Cursor = System.Windows.Input.Cursors.Hand,
                HorizontalContentAlignment = HorizontalAlignment.Left,
            };

            // The item chrome is applied through an implicit style rather than per item,
            // because the containers are generated inside the popup and are not reachable
            // from here — the same reason the scrollbar style is done this way.
            var itemStyle = new Style(typeof(ComboBoxItem));
            itemStyle.Setters.Add(
                new Setter(ComboBoxItem.TemplateProperty, TemplateFactory.SelectItem));
            itemStyle.Setters.Add(new Setter(Control.ForegroundProperty, Theme.Ink));
            itemStyle.Setters.Add(new Setter(Control.FontFamilyProperty, Theme.UiFamily));
            itemStyle.Setters.Add(
                new Setter(Control.FontSizeProperty, compact ? Theme.SizeSmall : Theme.SizeBody));
            itemStyle.Setters.Add(
                new Setter(FrameworkElement.CursorProperty, System.Windows.Input.Cursors.Hand));
            _combo.Resources.Add(typeof(ComboBoxItem), itemStyle);

            var scrollStyle = new Style(typeof(ScrollBar));
            scrollStyle.Setters.Add(
                new Setter(ScrollBar.TemplateProperty, TemplateFactory.ScrollBar));
            _combo.Resources.Add(typeof(ScrollBar), scrollStyle);

            Reset(options, selected);

            _combo.SelectionChanged += (s, e) =>
            {
                if (_suppress || _onChanged == null) return;
                var option = _combo.SelectedItem as Controls.Option;
                _onChanged(option == null ? null : option.Value);
            };
        }

        public ComboBox Element { get { return _combo; } }

        /// <summary>Replace the options without firing <c>onChanged</c>.</summary>
        public void Reset(IEnumerable<Controls.Option> options, object selected)
        {
            _suppress = true;
            try
            {
                _combo.Items.Clear();
                Controls.Option match = null;
                foreach (Controls.Option option in options)
                {
                    _combo.Items.Add(option);
                    if (match == null && Equals(option.Value, selected)) match = option;
                }
                _combo.SelectedItem = match;
            }
            finally
            {
                _suppress = false;
            }
        }

        /// <summary>Select a value without firing <c>onChanged</c>. Unknown values clear it.</summary>
        public void Pick(object value)
        {
            _suppress = true;
            try
            {
                foreach (object item in _combo.Items)
                {
                    var option = item as Controls.Option;
                    if (option != null && Equals(option.Value, value))
                    {
                        _combo.SelectedItem = option;
                        return;
                    }
                }
                _combo.SelectedItem = null;
            }
            finally
            {
                _suppress = false;
            }
        }
    }

    /// <summary>A tab strip built from real toggle buttons.</summary>
    internal sealed class Segments
    {
        private readonly StackPanel _root;
        private readonly Border _shell;
        private readonly List<ToggleButton> _tabs = new List<ToggleButton>();
        private readonly Action<int> _onPick;
        private bool _suppress;

        internal Segments(Action<int> onPick)
        {
            _onPick = onPick;
            _root = Ui.Row(0);

            // Built once, not per property read: a StackPanel cannot be the child of two
            // Borders, and a get-only property that constructs is a trap waiting for the
            // second caller.
            _shell = new Border
            {
                Background = Theme.Sunken,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                CornerRadius = Theme.ControlCorner,
                Padding = new Thickness(2),
                HorizontalAlignment = HorizontalAlignment.Left,
                SnapsToDevicePixels = true,
                Child = _root,
            };
        }

        public FrameworkElement Element { get { return _shell; } }

        public ToggleButton Add(string caption, string unavailableReason = null)
        {
            int index = _tabs.Count;
            var tab = new ToggleButton
            {
                Content = Ui.Text(caption),
                Template = TemplateFactory.Segment,
                Padding = new Thickness(Theme.Space3, 4, Theme.Space3, 4),
                MinHeight = Theme.ControlHeightSmall,
                Focusable = true,
                Cursor = System.Windows.Input.Cursors.Hand,
            };
            tab.Checked += (s, e) =>
            {
                if (_suppress) return;
                Select(index, notify: true);
            };
            // A checked tab cannot be unchecked by clicking it again: one of these is
            // always the current pane.
            tab.Unchecked += (s, e) => { if (!_suppress && !AnyChecked()) tab.IsChecked = true; };
            if (unavailableReason != null) tab.ToolTip = unavailableReason;

            _tabs.Add(tab);
            _root.Append(tab, 0);
            return tab;
        }

        public void Select(int index, bool notify)
        {
            _suppress = true;
            try
            {
                for (int i = 0; i < _tabs.Count; i++)
                {
                    bool on = i == index;
                    if (_tabs[i].IsChecked != on) _tabs[i].IsChecked = on;
                    var caption = _tabs[i].Content as TextBlock;
                    if (caption != null)
                    {
                        caption.Foreground = !_tabs[i].IsEnabled
                            ? Theme.InkFaint
                            : (on ? Theme.Ink : Theme.InkMuted);
                    }
                }
            }
            finally
            {
                _suppress = false;
            }
            if (notify && _onPick != null) _onPick(index);
        }

        public void SetEnabled(int index, bool enabled, string reason)
        {
            if (index < 0 || index >= _tabs.Count) return;
            _tabs[index].IsEnabled = enabled;
            _tabs[index].ToolTip = enabled ? null : reason;
            var caption = _tabs[index].Content as TextBlock;
            if (caption != null && !enabled) caption.Foreground = Theme.InkFaint;
        }

        private bool AnyChecked()
        {
            foreach (ToggleButton tab in _tabs)
            {
                if (tab.IsChecked == true) return true;
            }
            return false;
        }
    }
}
