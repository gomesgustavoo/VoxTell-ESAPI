using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Media;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The panel's whole visual language: palette, type, spacing, metrics.
    ///
    /// The governing rule, and the reason the palette looks understated
    /// -------------------------------------------------------------
    /// **Chrome is chromatically quiet, because structure colours are clinical data.**
    ///
    /// A planner reads this panel to decide what gets written into a patient, and the
    /// colours in the review list are the ones they will then look for on the CT. If
    /// the UI also has a bright accent, a coloured progress bar and tinted buttons,
    /// the swatches stop being the salient thing on screen and start competing with
    /// decoration. So the ramp below is near-neutral, <see cref="Steel"/> is
    /// deliberately too desaturated to compete, and every saturated pixel in the
    /// panel belongs either to a structure or to a state (ok / warn / bad).
    ///
    /// The previous WinForms palette used <c>Accent = (79,195,247)</c>, a bright cyan
    /// applied to links, focus, the progress bar and the device code. That accent is
    /// the one accessory removed here.
    ///
    /// Why every brush is frozen
    /// -------------------------
    /// Not only for speed. An unfrozen <see cref="Freezable"/> is a
    /// <see cref="System.Windows.Threading.DispatcherObject"/> with thread affinity,
    /// so a static brush created during one <c>Script.Execute</c> and touched from a
    /// later invocation's dispatcher throws a cross-thread exception. Eclipse can run
    /// a plugin more than once per session, so freezing is what makes these statics
    /// genuinely shareable rather than a latent crash.
    ///
    /// No <c>ResourceDictionary</c>, no <c>DynamicResource</c>: lookups would walk up
    /// into Eclipse's own application resources, which we neither own nor control.
    /// </summary>
    internal static class Theme
    {
        // --- palette ------------------------------------------------------------------- //
        // A cool-neutral charcoal ramp. Cool rather than pure grey so it sits with
        // Eclipse's own dark workspace instead of looking like a lighter patch on it.

        /// <summary>Behind the cards.</summary>
        public static readonly Brush Void = Frozen(0x16, 0x18, 0x1B);

        /// <summary>Card surface.</summary>
        public static readonly Brush Panel = Frozen(0x1E, 0x21, 0x26);

        /// <summary>Inputs, row hover, the raised step.</summary>
        public static readonly Brush Raised = Frozen(0x26, 0x2A, 0x30);

        /// <summary>Pressed control fill — recedes rather than lights up.</summary>
        public static readonly Brush Sunken = Frozen(0x1A, 0x1D, 0x21);

        /// <summary>Hairlines, borders, the unfilled part of a track.</summary>
        public static readonly Brush Edge = Frozen(0x32, 0x37, 0x3E);

        /// <summary>Primary text.</summary>
        public static readonly Brush Ink = Frozen(0xE8, 0xEA, 0xED);

        /// <summary>Secondary text, captions, column headers.</summary>
        public static readonly Brush InkMuted = Frozen(0x9A, 0xA1, 0xAB);

        /// <summary>Disabled text, unreachable steps.</summary>
        public static readonly Brush InkFaint = Frozen(0x6B, 0x72, 0x80);

        /// <summary>
        /// Focus rings and links, and nothing else.
        ///
        /// Desaturated on purpose. A focus ring has to be findable, not loud, and this
        /// one must never read as more important than a structure swatch beside it.
        /// </summary>
        public static readonly Brush Steel = Frozen(0x7C, 0x9C, 0xB8);

        // State. Muted versions of the obvious hues — enough to read at a glance,
        // not enough to pull the eye away from the review list.
        public static readonly Brush Ok = Frozen(0x6F, 0xBF, 0x73);
        public static readonly Brush Warn = Frozen(0xD9, 0xA3, 0x43);
        public static readonly Brush Bad = Frozen(0xE0, 0x64, 0x5C);

        /// <summary>Disabled control fill — below the card surface, so it recedes.</summary>
        public static readonly Brush ControlDisabled = Frozen(0x20, 0x23, 0x28);

        // --- pens ---------------------------------------------------------------------- //

        /// <summary>1 unit hairline for dividers and track outlines.</summary>
        public static readonly Pen EdgePen = FrozenPen(Edge, 1.0);
        public static readonly Pen FocusPen = FrozenPen(Steel, 1.0);

        // --- type ---------------------------------------------------------------------- //
        // Segoe UI stays the family. An embedded panel that looks alien inside its host
        // is a real cost, and Eclipse is a Segoe UI application.
        //
        // Sizes are WPF device-independent units, not points: 13 DIP is about 9.75pt at
        // 96 DPI, which is what the typography pass settled on for body text.

        public static readonly FontFamily UiFamily = ResolveFamily(
            new[] { "Segoe UI", "Tahoma" });

        public static readonly FontFamily UiSemiboldFamily = ResolveFamily(
            new[] { "Segoe UI Semibold", "Segoe UI", "Tahoma" });

        /// <summary>
        /// The pairing/device code. Monospaced and large because it is read off this
        /// screen and typed into a phone, where a misread character costs a retry.
        /// </summary>
        public static readonly FontFamily MonoFamily = ResolveFamily(
            new[] { "Consolas", "Cascadia Mono", "Lucida Console" });

        public const double SizeDisplay = 20;   // signed-in identity, step titles
        public const double SizeSection = 13;   // section headings (semibold)
        public const double SizeBody = 13;      // everything else
        public const double SizeSmall = 12;     // captions, secondary detail
        public const double SizeMicro = 11;     // column headers, step numbers
        public const double SizeMono = 19;      // device code

        /// <summary>
        /// "Segoe UI Semibold" is its own family on Windows and reads better than
        /// faux-bolding Segoe UI at these sizes. When it is missing, the fallback family
        /// is plain Segoe UI, so this weight is what supplies the emphasis.
        /// </summary>
        public static readonly FontWeight SemiboldWeight =
            UiSemiboldFamily.Source == "Segoe UI Semibold" ? FontWeights.Normal : FontWeights.SemiBold;

        // --- spacing ------------------------------------------------------------------- //
        // A 4/8/16/24 scale. Every margin comes from here so panels stop disagreeing
        // about what "a gap" means.
        //
        // Unlike the WinForms version these need no DPI scaling of any kind: WPF lays
        // out in device-independent units, so the Px()/AutoScaleMode machinery — and the
        // double-scaling trap that came with it — is simply gone.

        public const double Space1 = 4;
        public const double Space2 = 8;
        public const double Space3 = 16;
        public const double Space4 = 24;

        /// <summary>Left/right inset inside a card. Content aligns on this column.</summary>
        public const double CardInset = 14;

        public static readonly Thickness CardPadding = new Thickness(CardInset, 12, CardInset, 12);
        public static readonly Thickness ButtonPadding = new Thickness(14, 6, 14, 6);
        public static readonly Thickness InputPadding = new Thickness(8, 5, 8, 5);
        public static readonly CornerRadius CardCorner = new CornerRadius(4);
        public static readonly CornerRadius ControlCorner = new CornerRadius(3);

        /// <summary>Review row height. Tall enough that descenders clear the divider.</summary>
        public const double RowHeight = 34;

        public const double ButtonHeight = 30;

        // --- helpers ------------------------------------------------------------------- //

        /// <summary>Maps a status severity onto a brush. Replaces an IValueConverter.</summary>
        public static Brush StatusBrush(StatusSeverity severity)
        {
            switch (severity)
            {
                case StatusSeverity.Success: return Ok;
                case StatusSeverity.Warning: return Warn;
                case StatusSeverity.Error: return Bad;
                case StatusSeverity.Working: return Ink;
                default: return InkMuted;
            }
        }

        private static SolidColorBrush Frozen(byte r, byte g, byte b)
        {
            var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
            brush.Freeze();
            return brush;
        }

        /// <summary>A frozen brush over an arbitrary colour — used for structure swatches.</summary>
        public static SolidColorBrush BrushFor(Color colour)
        {
            SolidColorBrush cached;
            if (_swatchCache.TryGetValue(colour, out cached)) return cached;

            var brush = new SolidColorBrush(colour);
            brush.Freeze();
            _swatchCache[colour] = brush;
            return brush;
        }

        // Swatch brushes are created per structure and per palette entry, and the review
        // list rebuilds on every result. Caching keeps that from allocating a fresh
        // brush per row per rebuild.
        private static readonly Dictionary<Color, SolidColorBrush> _swatchCache =
            new Dictionary<Color, SolidColorBrush>();

        private static Pen FrozenPen(Brush brush, double thickness)
        {
            var pen = new Pen(brush, thickness);
            pen.Freeze();
            return pen;
        }

        /// <summary>
        /// First installed family from <paramref name="candidates"/>, else the OS default.
        ///
        /// <c>new FontFamily("NoSuchFamily")</c> does not throw — it silently resolves to
        /// a fallback at render time, which is the exact quiet-degradation the typography
        /// pass existed to eliminate. On a locale-specific or hardened Windows install
        /// "Segoe UI" is not a certainty, so it is resolved explicitly here.
        /// </summary>
        private static FontFamily ResolveFamily(string[] candidates)
        {
            foreach (string name in candidates)
            {
                if (IsInstalled(name)) return new FontFamily(name);
            }
            return SystemFonts.MessageFontFamily;
        }

        private static bool IsInstalled(string name)
        {
            try
            {
                foreach (FontFamily family in Fonts.SystemFontFamilies)
                {
                    if (string.Equals(family.Source, name, StringComparison.OrdinalIgnoreCase))
                        return true;
                }
            }
            catch
            {
                // Enumerating the font collection can fail on a locked-down install;
                // treat that as "not found" and fall through to the OS default.
            }
            return false;
        }
    }
}
