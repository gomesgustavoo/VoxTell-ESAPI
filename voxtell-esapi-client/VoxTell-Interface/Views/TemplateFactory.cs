using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Markup;
using System.Windows.Media;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The only file in the codebase that builds <see cref="ControlTemplate"/>s.
    ///
    /// Why templates are unavoidable
    /// -----------------------------
    /// Setting <c>Background</c> on a WPF <see cref="Button"/> does not restyle it — the
    /// theme template paints its own chrome over the top and reverts it on hover. So
    /// anything deriving from <see cref="Control"/> needs a template, and the review list
    /// needs three of them: a button, a tick box and a scrollbar.
    ///
    /// Why not hand-roll a "button" out of a Border and mouse handlers instead: that
    /// loses tab focus, Space/Enter activation, <c>IsEnabled</c> semantics and the
    /// automation peer. In a panel where a tick decides whether contours are written into
    /// a patient, keyboard reachability is not a nicety.
    ///
    /// Two mechanisms, and why the split is where it is
    /// -----------------------------------------------
    /// <see cref="FrameworkElementFactory"/> is the code-only way to give a template a
    /// visual tree, and it handles the button and the tick box cleanly — both are just
    /// nested elements with triggers.
    ///
    /// The scrollbar is different, and it is worth writing down rather than rediscovering.
    /// A scrollbar's thumb is not a *child* of its <see cref="Track"/>, it is the
    /// <c>Track.Thumb</c> **property**. <c>FrameworkElementFactory.AppendChild</c> can
    /// only add children, and there is no way to assign a factory-created element to a
    /// property, so the composition a scrollbar needs cannot be expressed in FEF at all.
    /// That one template is therefore parsed from a XAML string.
    ///
    /// This is a deliberately narrow exception. The string references only framework
    /// types, so it needs no <c>clr-namespace</c> mapping — which matters, because this
    /// assembly's name contains a hyphen and its output is renamed to
    /// <c>*.esapi.dll</c>, exactly the sort of thing that turns a runtime XAML reference
    /// into a puzzle. No build change, no BAML, no <c>Application.LoadComponent</c>.
    ///
    /// Templates are <c>Seal()</c>ed and shared as statics. Sealing makes them immutable
    /// and thread-safe, which is the same reasoning as freezing the brushes in
    /// <see cref="Theme"/>: a static touched from a later <c>Script.Execute</c>'s
    /// dispatcher must not throw.
    /// </summary>
    internal static class TemplateFactory
    {
        private static ControlTemplate _button;
        private static ControlTemplate _tick;
        private static ControlTemplate _scrollBar;

        /// <summary>Flat button: rounded border, centred content, state triggers.</summary>
        public static ControlTemplate Button
        {
            get { return _button ?? (_button = BuildButton()); }
        }

        /// <summary>Tick box: a small square that gains a checkmark when checked.</summary>
        public static ControlTemplate Tick
        {
            get { return _tick ?? (_tick = BuildTick()); }
        }

        /// <summary>Thin vertical scrollbar: a trough and a thumb, no arrow buttons.</summary>
        public static ControlTemplate ScrollBar
        {
            get { return _scrollBar ?? (_scrollBar = BuildScrollBar()); }
        }

        // --- button -------------------------------------------------------------------- //

        private static ControlTemplate BuildButton()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Theme.Raised);
            border.SetValue(Border.BorderBrushProperty, Theme.Edge);
            border.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            border.SetValue(Border.CornerRadiusProperty, Theme.ControlCorner);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(FrameworkElement.HorizontalAlignmentProperty,
                HorizontalAlignment.Center);
            content.SetValue(FrameworkElement.VerticalAlignmentProperty,
                VerticalAlignment.Center);
            // TemplateBinding has no code equivalent; a TemplatedParent RelativeSource
            // binding is the substitute, and FEF.SetBinding accepts it.
            content.SetBinding(FrameworkElement.MarginProperty,
                new Binding("Padding") { RelativeSource = RelativeSource.TemplatedParent });
            border.AppendChild(content);

            var template = new ControlTemplate(typeof(ButtonBase));
            template.VisualTree = border;
            template.Triggers.Add(Fill("Bd", UIElement.IsMouseOverProperty, true, Theme.Panel));
            template.Triggers.Add(Fill("Bd", ButtonBase.IsPressedProperty, true, Theme.Sunken));
            template.Triggers.Add(
                Fill("Bd", UIElement.IsEnabledProperty, false, Theme.ControlDisabled));
            // The only place the steel accent appears on a button.
            template.Triggers.Add(
                Stroke("Bd", UIElement.IsKeyboardFocusedProperty, true, Theme.Steel));
            template.Seal();
            return template;
        }

        // --- tick box ------------------------------------------------------------------ //

        // A checkmark, drawn once and frozen. Coordinates are in a 12x12 box.
        private static readonly Geometry CheckGeometry = FrozenCheck();

        private static Geometry FrozenCheck()
        {
            Geometry geometry = Geometry.Parse("M 2,6 L 4.6,8.8 L 10,2.6");
            geometry.Freeze();
            return geometry;
        }

        private static ControlTemplate BuildTick()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Theme.Raised);
            border.SetValue(Border.BorderBrushProperty, Theme.Edge);
            border.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            border.SetValue(Border.CornerRadiusProperty, new CornerRadius(2));
            border.SetValue(FrameworkElement.WidthProperty, 16.0);
            border.SetValue(FrameworkElement.HeightProperty, 16.0);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var check = new FrameworkElementFactory(typeof(System.Windows.Shapes.Path), "Check");
            check.SetValue(System.Windows.Shapes.Path.DataProperty, CheckGeometry);
            // Ink, not the accent: the tick has to be unmistakable, and per the palette
            // rule the only saturated things on screen are structure colours and state.
            check.SetValue(System.Windows.Shapes.Shape.StrokeProperty, Theme.Ink);
            check.SetValue(System.Windows.Shapes.Shape.StrokeThicknessProperty, 1.8);
            check.SetValue(System.Windows.Shapes.Shape.StrokeStartLineCapProperty,
                PenLineCap.Round);
            check.SetValue(System.Windows.Shapes.Shape.StrokeEndLineCapProperty,
                PenLineCap.Round);
            check.SetValue(FrameworkElement.HorizontalAlignmentProperty,
                HorizontalAlignment.Center);
            check.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            check.SetValue(UIElement.VisibilityProperty, Visibility.Collapsed);
            border.AppendChild(check);

            var template = new ControlTemplate(typeof(ToggleButton));
            template.VisualTree = border;

            var checked_ = new Trigger
            {
                Property = ToggleButton.IsCheckedProperty,
                Value = true,
            };
            checked_.Setters.Add(
                new Setter(UIElement.VisibilityProperty, Visibility.Visible, "Check"));
            checked_.Setters.Add(new Setter(Border.BorderBrushProperty, Theme.InkMuted, "Bd"));
            template.Triggers.Add(checked_);

            template.Triggers.Add(Stroke("Bd", UIElement.IsMouseOverProperty, true, Theme.InkFaint));
            template.Triggers.Add(
                Stroke("Bd", UIElement.IsKeyboardFocusedProperty, true, Theme.Steel));
            template.Triggers.Add(
                Fill("Bd", UIElement.IsEnabledProperty, false, Theme.ControlDisabled));
            template.Seal();
            return template;
        }

        // --- scrollbar ----------------------------------------------------------------- //

        // See the class remarks for why this one is a XAML string. Colours are inlined
        // rather than referenced, because a resource lookup from parsed XAML would search
        // Eclipse's application resources, which we do not own. They must match Theme:
        //   #26262A/#32373E = Theme.Raised / Theme.Edge, #6B7280 = Theme.InkFaint.
        private const string ScrollBarXaml =
            "<ControlTemplate TargetType='ScrollBar' " +
            "  xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'>" +
            "  <Grid Background='Transparent' Width='10'>" +
            "    <Track Name='PART_Track' IsDirectionReversed='True'" +
            "           Value='{TemplateBinding Value}'" +
            "           Minimum='{TemplateBinding Minimum}'" +
            "           Maximum='{TemplateBinding Maximum}'" +
            "           ViewportSize='{TemplateBinding ViewportSize}'>" +
            "      <Track.DecreaseRepeatButton>" +
            "        <RepeatButton Command='ScrollBar.PageUpCommand' Focusable='False'>" +
            "          <RepeatButton.Template>" +
            "            <ControlTemplate TargetType='RepeatButton'>" +
            "              <Border Background='Transparent'/>" +
            "            </ControlTemplate>" +
            "          </RepeatButton.Template>" +
            "        </RepeatButton>" +
            "      </Track.DecreaseRepeatButton>" +
            "      <Track.IncreaseRepeatButton>" +
            "        <RepeatButton Command='ScrollBar.PageDownCommand' Focusable='False'>" +
            "          <RepeatButton.Template>" +
            "            <ControlTemplate TargetType='RepeatButton'>" +
            "              <Border Background='Transparent'/>" +
            "            </ControlTemplate>" +
            "          </RepeatButton.Template>" +
            "        </RepeatButton>" +
            "      </Track.IncreaseRepeatButton>" +
            "      <Track.Thumb>" +
            "        <Thumb Focusable='False'>" +
            "          <Thumb.Template>" +
            "            <ControlTemplate TargetType='Thumb'>" +
            "              <Border Name='Bd' Background='#FF32373E' CornerRadius='3'" +
            "                      Margin='2,0,2,0' SnapsToDevicePixels='True'/>" +
            "              <ControlTemplate.Triggers>" +
            "                <Trigger Property='IsMouseOver' Value='True'>" +
            "                  <Setter TargetName='Bd' Property='Background' Value='#FF6B7280'/>" +
            "                </Trigger>" +
            "              </ControlTemplate.Triggers>" +
            "            </ControlTemplate>" +
            "          </Thumb.Template>" +
            "        </Thumb>" +
            "      </Track.Thumb>" +
            "    </Track>" +
            "  </Grid>" +
            "</ControlTemplate>";

        private static ControlTemplate BuildScrollBar()
        {
            var template = (ControlTemplate)XamlReader.Parse(ScrollBarXaml);
            template.Seal();
            return template;
        }

        // --- trigger helpers ----------------------------------------------------------- //

        private static Trigger Fill(
            string part, DependencyProperty on, object when, Brush fill)
        {
            var trigger = new Trigger { Property = on, Value = when };
            trigger.Setters.Add(new Setter(Border.BackgroundProperty, fill, part));
            return trigger;
        }

        private static Trigger Stroke(
            string part, DependencyProperty on, object when, Brush stroke)
        {
            var trigger = new Trigger { Property = on, Value = when };
            trigger.Setters.Add(new Setter(Border.BorderBrushProperty, stroke, part));
            return trigger;
        }
    }
}
