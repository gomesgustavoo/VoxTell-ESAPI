using System;
using System.Collections.Generic;
using System.Windows.Media;

namespace VoxTell_Interface.Models
{
    /// <summary>
    /// The colours new AI structures are created with.
    ///
    /// Replaces <c>StructureColors[_rng.Next(...)]</c>, which was picked at write time and had
    /// three problems the operator could see:
    /// <list type="bullet">
    /// <item>importing twelve structures could hand two of them the same colour, so the planner
    /// could not tell them apart on the CT — which is the entire job of a structure colour;</item>
    /// <item>re-running the same prompt produced a different colour every time, so a structure
    /// the planner had learned to recognise changed identity under them;</item>
    /// <item>the colour was invisible until after the write, so it could not be reviewed or
    /// changed beforehand.</item>
    /// </list>
    ///
    /// <see cref="Assign"/> fixes all three: it is a pure function of the prompt, so the same
    /// prompt lands on the same colour; and it takes the colours already used in this import, so
    /// no two structures in one batch collide.
    /// </summary>
    public static class StructurePalette
    {
        /// <summary>
        /// Fourteen hues, chosen against greyscale CT rather than against a white page.
        ///
        /// Two constraints drove the selection. Hues are spread roughly evenly around the wheel
        /// so adjacent entries stay tellable apart at contour-line width, which is one or two
        /// pixels of colour against grey. And every entry sits at mid luminance: pure yellow
        /// (the old palette's <c>255,241,118</c>) disappears against the bright soft-tissue and
        /// bone windows a planner actually works in, and anything much darker disappears against
        /// air. Amber replaces yellow for that reason.
        /// </summary>
        private static readonly Color[] Colors =
        {
            Rgb(0xE0, 0x52, 0x52),  //  0  red
            Rgb(0xE8, 0x84, 0x3C),  //  1  orange
            Rgb(0xE0, 0xB3, 0x3C),  //  2  amber
            Rgb(0xC0, 0xCE, 0x4A),  //  3  yellow-green
            Rgb(0x6F, 0xBF, 0x5A),  //  4  green
            Rgb(0x3F, 0xBF, 0x8F),  //  5  spring green
            Rgb(0x40, 0xBF, 0xC4),  //  6  cyan
            Rgb(0x4A, 0x9E, 0xE0),  //  7  azure
            Rgb(0x5A, 0x6F, 0xD4),  //  8  blue
            Rgb(0x8A, 0x5F, 0xD0),  //  9  violet
            Rgb(0xB8, 0x58, 0xC8),  // 10  purple
            Rgb(0xDB, 0x5A, 0xA8),  // 11  magenta
            Rgb(0xD9, 0x60, 0x7A),  // 12  rose
            Rgb(0xA8, 0x84, 0x6A),  // 13  tan
        };

        private static Color Rgb(byte r, byte g, byte b)
        {
            return Color.FromRgb(r, g, b);
        }

        /// <summary>Every colour, in order — for the swatch picker.</summary>
        public static IList<Color> All
        {
            get { return (Color[])Colors.Clone(); }
        }

        /// <summary>
        /// Picks a colour for <paramref name="prompt"/>, avoiding everything in
        /// <paramref name="used"/>.
        ///
        /// The prompt's hash chooses a starting point, then this walks forward to the first free
        /// entry. So a prompt run on its own is reproducible, and a batch of prompts is
        /// collision-free — up to fourteen, after which it has to reuse, which is the honest
        /// outcome for a palette of fourteen and a prompt cap of sixteen.
        /// </summary>
        public static Color Assign(string prompt, ICollection<Color> used)
        {
            int start = (int)(Fnv1A(prompt) % (uint)Colors.Length);

            if (used != null)
            {
                for (int step = 0; step < Colors.Length; step++)
                {
                    Color candidate = Colors[(start + step) % Colors.Length];
                    if (!used.Contains(candidate)) return candidate;
                }
            }

            return Colors[start];
        }

        /// <summary>
        /// FNV-1a over the lower-cased prompt.
        ///
        /// Deliberately not <see cref="string.GetHashCode"/>: that is documented as unstable
        /// across CLR versions and bitness, so "the same prompt gets the same colour" would hold
        /// only until someone changed the runtime. Spelled out here, it holds forever.
        /// </summary>
        private static uint Fnv1A(string text)
        {
            unchecked
            {
                uint hash = 2166136261u;
                if (text == null) return hash;

                for (int i = 0; i < text.Length; i++)
                {
                    hash ^= char.ToLowerInvariant(text[i]);
                    hash *= 16777619u;
                }
                return hash;
            }
        }
    }
}
