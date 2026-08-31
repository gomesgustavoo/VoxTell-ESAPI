using System;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
// Typography (the attached property that turns on tabular figures) lives in
// System.Windows.Documents, not System.Windows.
using System.Windows.Documents;
using System.Windows.Media;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// Small helpers that make a WPF tree built in C# read like the tree it builds.
    ///
    /// Why not XAML: this is a legacy non-SDK class library that Eclipse loads as a
    /// binary plugin, and adding WPF build targets, <c>&lt;Page&gt;</c> items and BAML
    /// resource loading is the one part of the pipeline that cannot be tested from
    /// outside the Eclipse workstation. Building the tree in C# needs no build change at
    /// all — the three WPF assemblies were already referenced for the <c>Window</c>
    /// parameter.
    ///
    /// Why not a fluent framework either: raw construction fails because
    /// <c>grid.Children.Add(x); Grid.SetRow(x, 1); Grid.SetColumn(x, 0);</c> buries the
    /// shape of the tree in bookkeeping, but a general DSL is a thing you then have to
    /// maintain. The middle ground is two rules:
    ///
    /// 1. **Structure comes from <c>params</c> factories**, so the call site is shaped
    ///    like the tree it produces.
    /// 2. **Modifiers are generic extension methods returning <c>T</c>**, so strong
    ///    typing and IntelliSense survive.
    ///
    /// One trap worth knowing before extending this. <c>Background</c>, <c>Padding</c>,
    /// <c>BorderBrush</c> and <c>BorderThickness</c> are **not** declared on a common
    /// base: <c>Panel.BackgroundProperty</c>, <c>Border.BackgroundProperty</c> and
    /// <c>Control.BackgroundProperty</c> are three distinct DependencyProperties. A
    /// single generic <c>Bg&lt;T&gt; where T : FrameworkElement</c> that reached for one
    /// of them would compile and then silently do nothing on the other two types. Hence
    /// the per-base overloads below.
    /// </summary>
    internal static class Ui
    {
        // --- structure ----------------------------------------------------------------- //

        /// <summary>
        /// A grid from row/column specs: <c>Grid("Auto,*,Auto", "*,120")</c>.
        /// Accepts <c>Auto</c>, <c>*</c>, <c>2*</c> and fixed numbers.
        /// </summary>
        public static Grid Grid(string rows, string cols, params UIElement[] kids)
        {
            var grid = new Grid();
            foreach (GridLength len in ParseLengths(rows))
                grid.RowDefinitions.Add(new RowDefinition { Height = len });
            foreach (GridLength len in ParseLengths(cols))
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = len });
            AddAll(grid, kids);
            return grid;
        }

        /// <summary>Vertical stack with a uniform gap between children.</summary>
        public static StackPanel Stack(double gap, params UIElement[] kids)
        {
            return Linear(Orientation.Vertical, gap, kids);
        }

        /// <summary>Horizontal stack with a uniform gap between children.</summary>
        public static StackPanel Row(double gap, params UIElement[] kids)
        {
            return Linear(Orientation.Horizontal, gap, kids);
        }

        private static StackPanel Linear(Orientation orientation, double gap, UIElement[] kids)
        {
            var panel = new StackPanel { Orientation = orientation };
            for (int i = 0; i < kids.Length; i++)
            {
                UIElement kid = kids[i];
                if (kid == null) continue;

                // StackPanel has no gap property, so the gap becomes a margin on every
                // child but the first. Applied here rather than at the call sites, which
                // is what keeps the spacing scale actually uniform.
                if (i > 0 && gap > 0)
                {
                    var fe = kid as FrameworkElement;
                    if (fe != null)
                    {
                        Thickness m = fe.Margin;
                        if (orientation == Orientation.Vertical)
                            fe.Margin = new Thickness(m.Left, m.Top + gap, m.Right, m.Bottom);
                        else
                            fe.Margin = new Thickness(m.Left + gap, m.Top, m.Right, m.Bottom);
                    }
                }
                panel.Children.Add(kid);
            }
            return panel;
        }

        /// <summary>
        /// A row that wraps onto the next line instead of running off the edge.
        ///
        /// For collections whose count is not known at design time — the preset
        /// buttons come from the server's catalog, so "how many fit" is not a
        /// question this code can answer. A horizontal <see cref="StackPanel"/>
        /// silently clips the overflow, which at the window's minimum width hid the
        /// last preset entirely.
        ///
        /// The gap is applied per child as a right/bottom margin rather than between
        /// children, because a wrap panel has no notion of which child starts a line.
        /// </summary>
        public static WrapPanel Wrap(params UIElement[] kids)
        {
            var panel = new WrapPanel { Orientation = Orientation.Horizontal };
            foreach (UIElement kid in kids)
            {
                if (kid != null) panel.Children.Add(kid);
            }
            return panel;
        }

        /// <summary>Add to a <see cref="WrapPanel"/> with the standard inter-item gap.</summary>
        public static T AppendWrapped<T>(this WrapPanel panel, T child, double gap)
            where T : FrameworkElement
        {
            if (child == null) return null;
            Thickness m = child.Margin;
            child.Margin = new Thickness(m.Left, m.Top, m.Right + gap, m.Bottom + gap);
            panel.Children.Add(child);
            return child;
        }

        /// <summary>A surface: rounded, inset, sitting above the window background.</summary>
        public static Border Card(UIElement child)
        {
            return new Border
            {
                Background = Theme.Panel,
                CornerRadius = Theme.CardCorner,
                Padding = Theme.CardPadding,
                SnapsToDevicePixels = true,
                Child = child,
            };
        }

        /// <summary>
        /// A titled surface: heading, hairline, then content.
        ///
        /// A <see cref="System.Windows.Controls.Grid"/> and not a
        /// <see cref="StackPanel"/>, and that is load-bearing rather than a preference. A
        /// StackPanel hands every child its *desired* height, so a `*` row or a scroller
        /// inside a section was measured at its full content height and then CLIPPED by the
        /// card — silently. That cost the protocol pane its last row and its button at the
        /// window's minimum height, with no scrollbar to hint that anything was missing.
        /// With a `*` content row the section passes the height it was given down, and a
        /// list inside it scrolls instead of disappearing.
        /// </summary>
        public static Border Section(string title, UIElement child)
        {
            TextBlock heading = Heading(title);
            Border rule = Divider();
            rule.Margin = new Thickness(0, Theme.Space2, 0, Theme.Space2);

            // Grid.SetRow rather than .At(): the child arrives as a UIElement, and the
            // modifier extensions are constrained to FrameworkElement.
            System.Windows.Controls.Grid.SetRow(child, 2);
            System.Windows.Controls.Grid.SetColumn(child, 0);

            return Card(Grid("Auto,Auto,*", "*",
                heading.At(0, 0),
                rule.At(1, 0),
                child));
        }

        /// <summary>Hairline rule. Low contrast on purpose — it separates, it does not decorate.</summary>
        public static Border Divider()
        {
            return new Border
            {
                Height = 1,
                Background = Theme.Edge,
                SnapsToDevicePixels = true,
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
        }

        /// <summary>Fills remaining space in a stack or grid cell.</summary>
        public static FrameworkElement Spacer()
        {
            return new FrameworkElement();
        }

        // --- text ---------------------------------------------------------------------- //

        public static TextBlock Text(string content)
        {
            return new TextBlock
            {
                Text = content ?? "",
                FontFamily = Theme.UiFamily,
                FontSize = Theme.SizeBody,
                Foreground = Theme.Ink,
                TextTrimming = TextTrimming.CharacterEllipsis,
                VerticalAlignment = VerticalAlignment.Center,
            };
        }

        public static TextBlock Heading(string content)
        {
            TextBlock t = Text(content);
            t.FontFamily = Theme.UiSemiboldFamily;
            t.FontWeight = Theme.SemiboldWeight;
            t.FontSize = Theme.SizeSection;
            return t;
        }

        public static TextBlock Display(string content)
        {
            TextBlock t = Text(content);
            t.FontFamily = Theme.UiSemiboldFamily;
            t.FontWeight = Theme.SemiboldWeight;
            t.FontSize = Theme.SizeDisplay;
            return t;
        }

        public static TextBlock Small(string content)
        {
            TextBlock t = Text(content);
            t.FontSize = Theme.SizeSmall;
            t.Foreground = Theme.InkMuted;
            return t;
        }

        /// <summary>Column headers and step numbers.</summary>
        public static TextBlock Micro(string content)
        {
            TextBlock t = Text(content);
            t.FontSize = Theme.SizeMicro;
            t.Foreground = Theme.InkMuted;
            return t;
        }

        /// <summary>Wrapping body copy — instructions and warnings, not data.</summary>
        public static TextBlock Paragraph(string content)
        {
            TextBlock t = Text(content);
            t.TextWrapping = TextWrapping.Wrap;
            t.TextTrimming = TextTrimming.None;
            t.VerticalAlignment = VerticalAlignment.Top;
            t.Foreground = Theme.InkMuted;
            return t;
        }

        public static TextBlock Mono(string content)
        {
            TextBlock t = Text(content);
            t.FontFamily = Theme.MonoFamily;
            t.FontSize = Theme.SizeMono;
            t.FontWeight = FontWeights.Bold;
            t.Foreground = Theme.Ink;
            return t;
        }

        // --- input --------------------------------------------------------------------- //

        /// <summary>
        /// A text box inside a rounded surface.
        ///
        /// No <c>ControlTemplate</c> needed: <see cref="TextBox"/> honours
        /// <c>Background</c>, <c>Foreground</c>, <c>CaretBrush</c> and
        /// <c>SelectionBrush</c> through its own template, so a borderless box inside a
        /// <see cref="Border"/> gets the rounded look for free. Returns the wrapper; the
        /// box itself comes back via <paramref name="box"/>.
        /// </summary>
        public static Border Input(out TextBox box, int maxLength = 0, bool multiline = false)
        {
            box = new TextBox
            {
                FontFamily = Theme.UiFamily,
                FontSize = Theme.SizeBody,
                Foreground = Theme.Ink,
                Background = Brushes.Transparent,
                CaretBrush = Theme.Ink,
                SelectionBrush = Theme.Steel,
                BorderThickness = new Thickness(0),
                Padding = new Thickness(0),
                AcceptsReturn = multiline,
                TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap,
                VerticalContentAlignment = multiline
                    ? VerticalAlignment.Top : VerticalAlignment.Center,
            };
            if (maxLength > 0) box.MaxLength = maxLength;
            if (multiline) box.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;

            return new Border
            {
                Background = Theme.Raised,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                CornerRadius = Theme.ControlCorner,
                Padding = Theme.InputPadding,
                SnapsToDevicePixels = true,
                Child = box,
            };
        }

        /// <summary>
        /// A password box in the same surface as <see cref="Input"/>.
        ///
        /// A real <see cref="System.Windows.Controls.PasswordBox"/>, not a TextBox: the API
        /// key field used to be an ordinary text box under a comment stating that a pasted
        /// key must not sit legible on a shared clinical workstation. It did.
        /// </summary>
        public static Border Password(out System.Windows.Controls.PasswordBox box)
        {
            box = new System.Windows.Controls.PasswordBox
            {
                FontFamily = Theme.UiFamily,
                FontSize = Theme.SizeBody,
                Foreground = Theme.Ink,
                Background = Brushes.Transparent,
                CaretBrush = Theme.Ink,
                SelectionBrush = Theme.Steel,
                BorderThickness = new Thickness(0),
                Padding = new Thickness(0),
            };

            return new Border
            {
                Background = Theme.Raised,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(1),
                CornerRadius = Theme.ControlCorner,
                Padding = Theme.InputPadding,
                SnapsToDevicePixels = true,
                Child = box,
            };
        }

        // --- modifiers ----------------------------------------------------------------- //

        public static T At<T>(this T element, int row, int col) where T : FrameworkElement
        {
            System.Windows.Controls.Grid.SetRow(element, row);
            System.Windows.Controls.Grid.SetColumn(element, col);
            return element;
        }

        public static T Span<T>(this T element, int rows, int cols) where T : FrameworkElement
        {
            if (rows > 1) System.Windows.Controls.Grid.SetRowSpan(element, rows);
            if (cols > 1) System.Windows.Controls.Grid.SetColumnSpan(element, cols);
            return element;
        }

        public static T W<T>(this T element, double width) where T : FrameworkElement
        {
            element.Width = width;
            return element;
        }

        public static T H<T>(this T element, double height) where T : FrameworkElement
        {
            element.Height = height;
            return element;
        }

        public static T MinW<T>(this T element, double width) where T : FrameworkElement
        {
            element.MinWidth = width;
            return element;
        }

        public static T MinH<T>(this T element, double height) where T : FrameworkElement
        {
            element.MinHeight = height;
            return element;
        }

        public static T Pad<T>(this T element, double l, double t, double r, double b)
            where T : Border
        {
            element.Padding = new Thickness(l, t, r, b);
            return element;
        }

        public static T Gap<T>(this T element, double l, double t, double r, double b)
            where T : FrameworkElement
        {
            element.Margin = new Thickness(l, t, r, b);
            return element;
        }

        public static T Left<T>(this T element) where T : FrameworkElement
        {
            element.HorizontalAlignment = HorizontalAlignment.Left;
            return element;
        }

        public static T Right<T>(this T element) where T : FrameworkElement
        {
            element.HorizontalAlignment = HorizontalAlignment.Right;
            return element;
        }

        public static T Stretch<T>(this T element) where T : FrameworkElement
        {
            element.HorizontalAlignment = HorizontalAlignment.Stretch;
            return element;
        }

        public static T Top<T>(this T element) where T : FrameworkElement
        {
            element.VerticalAlignment = VerticalAlignment.Top;
            return element;
        }

        public static T Fg<T>(this T element, Brush brush) where T : TextBlock
        {
            element.Foreground = brush;
            return element;
        }

        /// <summary>
        /// Tabular figures, right-aligned.
        ///
        /// The type decision that follows from this being an instrument panel: a column
        /// of volumes is only scannable if the digits line up, and proportional figures
        /// put a 1 in half the width of a 4. Applied per numeric block rather than
        /// globally so that prose keeps its proportional figures, where they read better.
        ///
        /// <c>NumeralAlignment</c> is a *request* — a font without the OpenType
        /// <c>tnum</c> feature ignores it silently. The right-alignment and the shared
        /// column definitions in <see cref="ReviewTable"/> are what guarantee the columns
        /// line up either way, so this is an improvement when available rather than a
        /// dependency.
        /// </summary>
        public static T Numeric<T>(this T element) where T : TextBlock
        {
            Typography.SetNumeralAlignment(element, FontNumeralAlignment.Tabular);
            element.TextAlignment = TextAlignment.Right;
            return element;
        }

        /// <summary>Tabular figures without forcing alignment — for inline readouts.</summary>
        public static T Tabular<T>(this T element) where T : TextBlock
        {
            Typography.SetNumeralAlignment(element, FontNumeralAlignment.Tabular);
            return element;
        }

        /// <summary>
        /// Append to a <see cref="StackPanel"/> built by <see cref="Stack"/> or
        /// <see cref="Row"/>, applying the same gap the constructor would have.
        ///
        /// Needed because <c>Linear</c> can only space the children it is handed. A
        /// panel filled later — the step rail, the preset row, the structure tree —
        /// got a uniform gap of zero, so its children sat flush against each other
        /// and read as one run-together block. That is a real defect, not a nicety:
        /// the whole point of the spacing scale is that nothing has to decide what
        /// "a gap" means at the call site.
        /// </summary>
        public static T Append<T>(this StackPanel panel, T child, double gap)
            where T : FrameworkElement
        {
            if (child == null) return null;

            if (panel.Children.Count > 0 && gap > 0)
            {
                Thickness m = child.Margin;
                child.Margin = panel.Orientation == Orientation.Vertical
                    ? new Thickness(m.Left, m.Top + gap, m.Right, m.Bottom)
                    : new Thickness(m.Left + gap, m.Top, m.Right, m.Bottom);
            }
            panel.Children.Add(child);
            return child;
        }

        public static T Show<T>(this T element, bool visible) where T : UIElement
        {
            // Collapsed rather than Hidden: Collapsed skips measure, so a hidden panel
            // costs nothing and cannot influence the layout of what is visible.
            element.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
            return element;
        }

        public static T Bind<T>(
            this T element,
            DependencyProperty property,
            string path,
            BindingMode mode = BindingMode.OneWay,
            string format = null)
            where T : FrameworkElement
        {
            var binding = new Binding(path) { Mode = mode };
            // No {} escape needed here — that is a XAML-only artefact.
            if (format != null) binding.StringFormat = format;
            element.SetBinding(property, binding);
            return element;
        }

        // --- internals ----------------------------------------------------------------- //

        private static void AddAll(Grid grid, UIElement[] kids)
        {
            foreach (UIElement kid in kids)
            {
                if (kid != null) grid.Children.Add(kid);
            }
        }

        private static GridLength[] ParseLengths(string spec)
        {
            if (string.IsNullOrEmpty(spec)) return new GridLength[0];

            string[] parts = spec.Split(',');
            var lengths = new GridLength[parts.Length];
            for (int i = 0; i < parts.Length; i++)
            {
                lengths[i] = ParseLength(parts[i].Trim());
            }
            return lengths;
        }

        private static GridLength ParseLength(string token)
        {
            if (string.Equals(token, "Auto", StringComparison.OrdinalIgnoreCase))
                return GridLength.Auto;

            if (token == "*")
                return new GridLength(1, GridUnitType.Star);

            if (token.EndsWith("*", StringComparison.Ordinal))
            {
                double weight;
                string head = token.Substring(0, token.Length - 1);
                if (double.TryParse(head, NumberStyles.Float, CultureInfo.InvariantCulture, out weight))
                    return new GridLength(weight, GridUnitType.Star);
                return new GridLength(1, GridUnitType.Star);
            }

            double fixedSize;
            if (double.TryParse(token, NumberStyles.Float, CultureInfo.InvariantCulture, out fixedSize))
                return new GridLength(fixedSize);

            throw new ArgumentException("Unrecognised grid length: '" + token + "'");
        }
    }
}
