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
        private static ControlTemplate _ghost;
        private static ControlTemplate _tick;
        private static ControlTemplate _scrollBar;
        private static ControlTemplate _select;
        private static ControlTemplate _selectItem;
        private static ControlTemplate _segment;
        private static ControlTemplate _menu;
        private static ControlTemplate _menuItem;

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

        /// <summary>
        /// A borderless button: no fill until hovered. For the step rail and the account
        /// chip, where a full button would put a box around every word in the header.
        /// </summary>
        public static ControlTemplate Ghost
        {
            get { return _ghost ?? (_ghost = BuildGhost()); }
        }

        /// <summary>
        /// A real dropdown: closed field with a chevron, list in a floating layer.
        ///
        /// This is what the review row's DICOM type used to be: a button that silently
        /// cycled four values on click. Nothing on it said so, and the four values were
        /// discoverable only by clicking three times.
        /// </summary>
        public static ControlTemplate Select
        {
            get { return _select ?? (_select = BuildSelect()); }
        }

        /// <summary>One row inside a <see cref="Select"/>'s list.</summary>
        public static ControlTemplate SelectItem
        {
            get { return _selectItem ?? (_selectItem = BuildSelectItem()); }
        }

        /// <summary>One tab of a segmented control: checked reads raised, unchecked recedes.</summary>
        public static ControlTemplate Segment
        {
            get { return _segment ?? (_segment = BuildSegment()); }
        }

        /// <summary>The account menu's surface.</summary>
        public static ControlTemplate Menu
        {
            get { return _menu ?? (_menu = BuildMenu()); }
        }

        /// <summary>One entry in the account menu.</summary>
        public static ControlTemplate MenuEntry
        {
            get { return _menuItem ?? (_menuItem = BuildMenuItem()); }
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
            template.Triggers.Add(Dim("Bd", Theme.ControlDisabled));
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
            template.Triggers.Add(Dim("Bd", Theme.ControlDisabled));
            template.Seal();
            return template;
        }

        // --- scrollbar ----------------------------------------------------------------- //

        // See the class remarks for why this one is a XAML string. Colours are inlined
        // rather than referenced, because a resource lookup from parsed XAML would search
        // Eclipse's application resources, which we do not own. They must match Theme:
        //   #32373E = Theme.Edge (the resting thumb), #6B7280 = Theme.InkFaint (hover).
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

        // --- ghost button -------------------------------------------------------------- //

        private static ControlTemplate BuildGhost()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            border.SetValue(Border.CornerRadiusProperty, Theme.ControlCorner);
            border.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            border.SetValue(Border.BorderBrushProperty, Brushes.Transparent);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            content.SetBinding(FrameworkElement.MarginProperty,
                new Binding("Padding") { RelativeSource = RelativeSource.TemplatedParent });
            border.AppendChild(content);

            var template = new ControlTemplate(typeof(ButtonBase));
            template.VisualTree = border;
            template.Triggers.Add(Fill("Bd", UIElement.IsMouseOverProperty, true, Theme.Raised));
            template.Triggers.Add(Fill("Bd", ButtonBase.IsPressedProperty, true, Theme.Sunken));
            template.Triggers.Add(
                Stroke("Bd", UIElement.IsKeyboardFocusedProperty, true, Theme.Steel));
            template.Triggers.Add(Dim("Bd", Brushes.Transparent));
            template.Seal();
            return template;
        }

        // --- segmented tab ------------------------------------------------------------- //

        private static ControlTemplate BuildSegment()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            border.SetValue(Border.BorderBrushProperty, Brushes.Transparent);
            border.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            border.SetValue(Border.CornerRadiusProperty, Theme.ControlCorner);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(FrameworkElement.HorizontalAlignmentProperty,
                HorizontalAlignment.Center);
            content.SetValue(FrameworkElement.VerticalAlignmentProperty,
                VerticalAlignment.Center);
            content.SetBinding(FrameworkElement.MarginProperty,
                new Binding("Padding") { RelativeSource = RelativeSource.TemplatedParent });
            border.AppendChild(content);

            var template = new ControlTemplate(typeof(ToggleButton));
            template.VisualTree = border;

            // Hover first, checked second: a trigger later in the list wins, so the
            // selected tab does not lose its fill when the pointer crosses it.
            template.Triggers.Add(Fill("Bd", UIElement.IsMouseOverProperty, true, Theme.Panel));

            var on = new Trigger { Property = ToggleButton.IsCheckedProperty, Value = true };
            on.Setters.Add(new Setter(Border.BackgroundProperty, Theme.Raised, "Bd"));
            on.Setters.Add(new Setter(Border.BorderBrushProperty, Theme.Edge, "Bd"));
            template.Triggers.Add(on);

            template.Triggers.Add(
                Stroke("Bd", UIElement.IsKeyboardFocusedProperty, true, Theme.Steel));
            template.Triggers.Add(Dim("Bd", Theme.ControlDisabled));
            template.Seal();
            return template;
        }

        // --- select (ComboBox) --------------------------------------------------------- //

        // XAML for the same reason as the scrollbar: a ComboBox composes by PROPERTY —
        // ContentPresenter for the selection box, PART_Popup for the list — and
        // FrameworkElementFactory can only append children. The part name PART_Popup is
        // load-bearing: ComboBox looks it up by name in OnApplyTemplate, and without it
        // the list opens but never closes on an outside click.
        //
        // Colours are inlined because a resource lookup from parsed XAML would walk up
        // into Eclipse's own application resources, which we do not own. They must match
        // Theme: #262A30 Raised, #32373E Edge, #2A2E35 Overlay, #E8EAED Ink,
        // #202328 ControlDisabled, #7C9CB8 Steel.
        private const string SelectXaml =
            "<ControlTemplate TargetType='ComboBox'" +
            "  xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'>" +
            "  <Grid>" +
            "    <ToggleButton Name='Toggle' Focusable='False' ClickMode='Press'" +
            "        IsChecked='{Binding IsDropDownOpen, Mode=TwoWay," +
            "                    RelativeSource={RelativeSource TemplatedParent}}'>" +
            "      <ToggleButton.Template>" +
            "        <ControlTemplate TargetType='ToggleButton'>" +
            "          <Border Name='Bd' Background='#FF262A30' BorderBrush='#FF32373E'" +
            "                  BorderThickness='1' CornerRadius='3' SnapsToDevicePixels='True'>" +
            "            <Path Name='Chevron' HorizontalAlignment='Right' VerticalAlignment='Center'" +
            "                  Margin='0,0,8,0' Data='M 0,0 L 4,4 L 8,0' Stroke='#FF9AA1AB'" +
            "                  StrokeThickness='1.4' StrokeStartLineCap='Round'" +
            "                  StrokeEndLineCap='Round'/>" +
            "          </Border>" +
            "          <ControlTemplate.Triggers>" +
            "            <Trigger Property='IsMouseOver' Value='True'>" +
            "              <Setter TargetName='Bd' Property='Background' Value='#FF1E2126'/>" +
            "              <Setter TargetName='Chevron' Property='Stroke' Value='#FFE8EAED'/>" +
            "            </Trigger>" +
            "            <Trigger Property='IsEnabled' Value='False'>" +
            "              <Setter TargetName='Bd' Property='Background' Value='#FF202328'/>" +
            "              <Setter TargetName='Chevron' Property='Stroke' Value='#FF6B7280'/>" +
            "            </Trigger>" +
            "          </ControlTemplate.Triggers>" +
            "        </ControlTemplate>" +
            "      </ToggleButton.Template>" +
            "    </ToggleButton>" +
            "    <ContentPresenter Name='Selection' IsHitTestVisible='False'" +
            "        Margin='9,3,24,3' VerticalAlignment='Center' HorizontalAlignment='Left'" +
            "        Content='{TemplateBinding SelectionBoxItem}'" +
            "        ContentTemplate='{TemplateBinding SelectionBoxItemTemplate}'" +
            "        ContentStringFormat='{TemplateBinding SelectionBoxItemStringFormat}'/>" +
            "    <Popup Name='PART_Popup' Placement='Bottom' AllowsTransparency='True'" +
            "           Focusable='False' PopupAnimation='None'" +
            "           IsOpen='{TemplateBinding IsDropDownOpen}'>" +
            "      <Border Background='#FF2A2E35' BorderBrush='#FF32373E' BorderThickness='1'" +
            "              CornerRadius='3' SnapsToDevicePixels='True' Padding='0,3,0,3'" +
            "              MinWidth='{Binding ActualWidth," +
            "                         RelativeSource={RelativeSource TemplatedParent}}'" +
            "              MaxHeight='{TemplateBinding MaxDropDownHeight}'>" +
            "        <ScrollViewer VerticalScrollBarVisibility='Auto'" +
            "                      HorizontalScrollBarVisibility='Disabled'>" +
            "          <ItemsPresenter KeyboardNavigation.DirectionalNavigation='Contained'/>" +
            "        </ScrollViewer>" +
            "      </Border>" +
            "    </Popup>" +
            "  </Grid>" +
            "</ControlTemplate>";

        private static ControlTemplate BuildSelect()
        {
            var template = (ControlTemplate)XamlReader.Parse(SelectXaml);
            template.Seal();
            return template;
        }

        private static ControlTemplate BuildSelectItem()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            border.SetValue(Border.PaddingProperty, Theme.MenuItemPadding);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            border.AppendChild(content);

            var template = new ControlTemplate(typeof(ComboBoxItem));
            template.VisualTree = border;
            template.Triggers.Add(Fill("Bd", UIElement.IsMouseOverProperty, true, Theme.Raised));
            // Highlight, not a tick: the closed field already shows what is selected, so a
            // second marker in the list is noise.
            template.Triggers.Add(
                Fill("Bd", ComboBoxItem.IsSelectedProperty, true, Theme.Panel));
            template.Seal();
            return template;
        }

        // --- context menu -------------------------------------------------------------- //

        private const string MenuXaml =
            "<ControlTemplate TargetType='ContextMenu'" +
            "  xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'>" +
            "  <Border Background='#FF2A2E35' BorderBrush='#FF32373E' BorderThickness='1'" +
            "          CornerRadius='3' Padding='0,3,0,3' SnapsToDevicePixels='True'>" +
            "    <StackPanel IsItemsHost='True' KeyboardNavigation.DirectionalNavigation='Cycle'/>" +
            "  </Border>" +
            "</ControlTemplate>";

        private static ControlTemplate BuildMenu()
        {
            var template = (ControlTemplate)XamlReader.Parse(MenuXaml);
            template.Seal();
            return template;
        }

        private static ControlTemplate BuildMenuItem()
        {
            var border = new FrameworkElementFactory(typeof(Border), "Bd");
            border.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            border.SetValue(Border.PaddingProperty, Theme.MenuItemPadding);
            border.SetValue(UIElement.SnapsToDevicePixelsProperty, true);

            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(ContentPresenter.ContentSourceProperty, "Header");
            content.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            border.AppendChild(content);

            var template = new ControlTemplate(typeof(MenuItem));
            template.VisualTree = border;
            template.Triggers.Add(
                Fill("Bd", MenuItem.IsHighlightedProperty, true, Theme.Raised));
            template.Triggers.Add(
                Fill("Bd", UIElement.IsEnabledProperty, false, Brushes.Transparent));
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

        /// <summary>
        /// The disabled look: recede AND fade.
        ///
        /// The fill change on its own is almost invisible against the card — two steps of a
        /// near-neutral ramp — and it left the *caption* at full ink, so a disabled Segment
        /// button read as available. The opacity is what makes the state legible, and it
        /// covers the content too, which a template setter on the border cannot.
        /// </summary>
        private static Trigger Dim(string part, Brush fill)
        {
            var trigger = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
            trigger.Setters.Add(new Setter(Border.BackgroundProperty, fill, part));
            trigger.Setters.Add(new Setter(UIElement.OpacityProperty, 0.5, part));
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
