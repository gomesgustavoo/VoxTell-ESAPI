using System;
using System.Drawing;
using System.Drawing.Text;
using System.Windows.Forms;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The panel's whole visual language in one place: palette, type scale, spacing.
    ///
    /// It exists because the first Eclipse run looked unprofessional for reasons that were
    /// concrete rather than aesthetic:
    ///
    /// * <c>MainControl</c> never set its own <c>Font</c> and the results grid never set
    ///   <c>DefaultCellStyle.Font</c>, so those controls inherited
    ///   <see cref="Control.DefaultFont"/> — **Microsoft Sans Serif 8.25pt**, a legacy raster-era
    ///   face. Nine of twenty-six controls rendered in it, including the review grid and the
    ///   prompts box, which are the two things a planner actually reads.
    /// * The rest picked from six ad-hoc sizes (8, 8.5, 9, 10, 13pt) with no relationship
    ///   between them, so nothing established a hierarchy.
    /// * Every <c>new Font(...)</c> was per-control and never disposed.
    ///
    /// <c>MainForm</c> pulls these in with <c>using static</c>, so the colour names resolve
    /// unqualified exactly as they did when they were fields on that class.
    ///
    /// Fonts here are process-lifetime singletons, deliberately: they live as long as the
    /// plugin window and are shared by every control, which is both cheaper and tidier than
    /// allocating one per label.
    /// </summary>
    internal static class UiTheme
    {
        // --- palette ------------------------------------------------------------------- //

        public static readonly Color BgDark = Color.FromArgb(30, 30, 30);
        public static readonly Color Surface = Color.FromArgb(45, 45, 45);
        public static readonly Color InputBg = Color.FromArgb(55, 55, 55);
        public static readonly Color Accent = Color.FromArgb(79, 195, 247);
        public static readonly Color TextPrimary = Color.FromArgb(240, 240, 240);
        public static readonly Color TextMuted = Color.FromArgb(140, 140, 140);
        public static readonly Color TextLabel = Color.FromArgb(210, 210, 210);
        public static readonly Color SuccessGreen = Color.FromArgb(102, 187, 106);
        public static readonly Color ErrorRed = Color.FromArgb(239, 83, 80);
        public static readonly Color WarnAmber = Color.FromArgb(255, 167, 38);

        /// <summary>Hairline under a section title. Low contrast on purpose.</summary>
        public static readonly Color Divider = Color.FromArgb(62, 62, 62);

        /// <summary>Fill for a disabled button — recedes below the card surface.</summary>
        public static readonly Color ButtonDisabledBg = Color.FromArgb(42, 42, 42);
        public static readonly Color GridLine = Color.FromArgb(60, 60, 60);
        public static readonly Color GridHeaderBg = Color.FromArgb(38, 38, 38);
        public static readonly Color GridSelection = Color.FromArgb(64, 64, 64);

        // --- DPI ----------------------------------------------------------------------- //

        /// <summary>
        /// Device pixels per logical pixel. 1.0 at 100 % scaling, 1.5 at 150 %.
        ///
        /// Fonts do NOT need this — point sizes are already density-independent, and the OS
        /// renders 9.75pt larger on a high-DPI monitor by itself. Only hand-computed pixel
        /// geometry does, which is what <see cref="Px"/> is for.
        ///
        /// Note this reads 96 in a DPI-*unaware* process even on a scaled monitor, because
        /// Windows lies to such processes and bitmap-stretches their windows afterwards. That
        /// is Eclipse's manifest to decide, not ours; the panel stays self-consistent either
        /// way.
        /// </summary>
        public static readonly float Scale = ResolveScale();

        private static float ResolveScale()
        {
            try
            {
                // Screen DC: no control needs to exist yet, so this is safe to run from a
                // static initialiser during MainControl's constructor.
                using (Graphics g = Graphics.FromHwnd(IntPtr.Zero))
                {
                    float scale = g.DpiX / 96f;
                    return scale > 0.5f && scale < 8f ? scale : 1f;
                }
            }
            catch
            {
                return 1f;
            }
        }

        /// <summary>Scales a logical pixel measurement for the current display.</summary>
        public static int Px(int logical)
        {
            return (int)Math.Round(logical * Scale);
        }

        // --- spacing ------------------------------------------------------------------- //
        // A 4/8/16/24 scale. Every margin comes from here so panels stop disagreeing about
        // what "a gap" means.
        //
        // These are LOGICAL pixels at 96 DPI and must NOT be pre-scaled. MainControl sets
        // AutoScaleMode.Dpi, so WinForms already multiplies the bounds of everything placed
        // during layout; running Px() over them as well would scale twice and blow the panel
        // apart at 150 %. Use them raw when building the layout, and wrap them in Px() only
        // inside Resize handlers, which run afterwards against already-scaled widths.

        public const int Space1 = 4;
        public const int Space2 = 8;
        public const int Space3 = 16;
        public const int Space4 = 24;

        /// <summary>Left/right inset inside a card. Content aligns on this column.</summary>
        public const int CardInset = 12;

        /// <summary>Y of a card's section title.</summary>
        public const int TitleTop = 9;

        /// <summary>Y of the hairline under the title.</summary>
        public const int DividerTop = 30;

        /// <summary>Y where a card's real content starts, below title + hairline.</summary>
        public const int ContentTop = 38;

        // --- type scale ---------------------------------------------------------------- //

        /// <summary>Signed-in identity — the largest thing on the panel.</summary>
        public static readonly Font H1 = Semibold(12f);

        /// <summary>Section titles.</summary>
        public static readonly Font Section = Semibold(9.75f);

        /// <summary>
        /// The default for everything. 9.75pt Segoe UI is what Visual Studio and Office use for
        /// body text on Windows; 8.25pt was simply too small to read comfortably.
        /// </summary>
        public static readonly Font Body = Regular(9.75f);

        /// <summary>Muted captions and secondary detail.</summary>
        public static readonly Font Small = Regular(9f);

        /// <summary>Grid column headers.</summary>
        public static readonly Font GridHeader = Semibold(9f);

        /// <summary>
        /// The device-code display. Monospaced and large because it is read off the screen and
        /// typed into a phone or another machine, where a misread character costs a retry.
        /// </summary>
        public static readonly Font Mono = Monospace(14f);

        // --- font construction --------------------------------------------------------- //

        private static Font Regular(float points)
        {
            return Build(new[] { "Segoe UI" }, points, FontStyle.Regular);
        }

        private static Font Semibold(float points)
        {
            // "Segoe UI Semibold" is its own family on Windows and reads better than faux-bold
            // Segoe UI at these sizes. Fall back to bolding the regular family.
            Font semi = Find(new[] { "Segoe UI Semibold" }, points, FontStyle.Regular);
            return semi ?? Build(new[] { "Segoe UI" }, points, FontStyle.Bold);
        }

        private static Font Monospace(float points)
        {
            return Build(new[] { "Consolas", "Cascadia Mono", "Lucida Console" },
                         points, FontStyle.Bold);
        }

        /// <summary>
        /// Resolves the first installed family, or the OS UI font as a last resort.
        ///
        /// The fallback matters: <c>new Font("NoSuchFamily", 9)</c> does not throw, it silently
        /// substitutes Microsoft Sans Serif — the exact ugly outcome this file exists to
        /// prevent. On a locale-specific or hardened Windows install, "Segoe UI" is not a
        /// certainty, so resolve explicitly and land on <see cref="SystemFonts.MessageBoxFont"/>
        /// instead, which is whatever that system actually uses for UI text.
        /// </summary>
        private static Font Build(string[] families, float points, FontStyle style)
        {
            Font found = Find(families, points, style);
            if (found != null) return found;

            Font ui = SystemFonts.MessageBoxFont;
            try
            {
                return new Font(ui.FontFamily, points, style);
            }
            catch (ArgumentException)
            {
                // The family does not support the style (rare, but real for some faces).
                return new Font(ui.FontFamily, points, FontStyle.Regular);
            }
        }

        private static Font Find(string[] families, float points, FontStyle style)
        {
            foreach (string name in families)
            {
                if (!IsInstalled(name)) continue;
                try
                {
                    var font = new Font(name, points, style);
                    // Font silently substitutes on a miss, so confirm we got what we asked for.
                    if (string.Equals(font.Name, name, StringComparison.OrdinalIgnoreCase))
                        return font;
                    font.Dispose();
                }
                catch (ArgumentException)
                {
                    // Style unsupported by this family; try the next candidate.
                }
            }
            return null;
        }

        private static bool IsInstalled(string family)
        {
            try
            {
                using (var f = new FontFamily(family))
                    return f != null;
            }
            catch (ArgumentException)
            {
                return false;
            }
        }

        // --- text rendering ------------------------------------------------------------ //

        /// <summary>
        /// Draws owner-drawn control text the crisp way.
        ///
        /// <see cref="Graphics.DrawString"/> is GDI+, which anti-aliases in greyscale and looks
        /// noticeably soft next to every other control on screen — those use GDI.
        /// <see cref="TextRenderer"/> is GDI and picks up ClearType, so button labels stop
        /// looking washed out. This was the single biggest sharpness complaint.
        /// </summary>
        public static void DrawCentredText(
            Graphics graphics, string text, Font font, Color colour, Rectangle bounds)
        {
            TextRenderer.DrawText(
                graphics, text, font, bounds, colour,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
                TextFormatFlags.SingleLine | TextFormatFlags.EndEllipsis |
                TextFormatFlags.NoPadding);
        }

        /// <summary>
        /// For the GDI+ text that has to stay GDI+. Grid-fitted ClearType is the closest GDI+
        /// gets to GDI's output.
        /// </summary>
        public static void PreferSharpText(Graphics graphics)
        {
            graphics.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        }
    }
}
