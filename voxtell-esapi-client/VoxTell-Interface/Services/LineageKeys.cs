using System;
using System.Security.Cryptography;
using System.Text;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Turns DICOM identifiers into opaque lineage keys, so run 2 can find run 1
    /// without any identifier ever reaching the cloud.
    ///
    /// The problem
    /// -----------
    /// The two-run QA workflow has to answer "is this the same series I segmented
    /// last week?". The obvious key is the DICOM Series Instance UID, and sending
    /// it would break the product's central privacy claim: a series UID is
    /// patient-linkable, so anyone holding it with access to the hospital archive
    /// can name the patient.
    ///
    /// The approach
    /// ------------
    /// Send <c>HMAC-SHA256(series UID)</c> under a secret scoped to the
    /// organisation. Two workstations in the same clinic derive the same key for
    /// the same series, so lineage works across them; the value is not an
    /// identifier, is not reversible by inspection, and cannot be joined against
    /// an external DICOM record without the secret.
    ///
    /// What this is and is not
    /// -----------------------
    /// A *pseudonym*, not encryption and not anonymity. Stated plainly so nobody
    /// over-claims it later:
    ///
    /// * The keying material is issued by the server, which therefore could
    ///   recompute a key, but it is never sent a UID to recompute one from.
    /// * The UID space is structured rather than uniformly random, so the strength
    ///   rests on the 256-bit secret, not on a UID being hard to guess.
    /// * If the secret rotates, existing baselines become unlinkable. A deliberate
    ///   trade, not a bug (the alternative is storing identifiers), but it makes
    ///   rotation a decision with a cost rather than routine hygiene.
    ///
    /// None of this is a security boundary against a hostile server. It is the
    /// mechanism that lets an honest server hold no identifiers at all.
    /// </summary>
    internal static class LineageKeys
    {
        // Domain separators, so the three kinds of key are independent: a series
        // UID and a frame-of-reference UID that happened to be equal must not
        // produce the same key.
        private const string SeriesLabel = "voxtell/lineage/series/v1";
        private const string FrameLabel = "voxtell/lineage/frame/v1";
        private const string ScannerLabel = "voxtell/lineage/scanner/v1";

        /// <summary>HMAC of a DICOM series instance UID (ESAPI <c>Series.UID</c>).</summary>
        public static string Series(string secretHex, string seriesUid)
        {
            return Compute(secretHex, SeriesLabel, seriesUid);
        }

        /// <summary>HMAC of a frame-of-reference UID (ESAPI <c>Image.FOR</c>).</summary>
        public static string Frame(string secretHex, string frameOfReferenceUid)
        {
            return Compute(secretHex, FrameLabel, frameOfReferenceUid);
        }

        /// <summary>
        /// HMAC of the imaging device triple (manufacturer, model, serial).
        ///
        /// Not for lineage: for noticing that the scanner or acquisition protocol
        /// changed since the model was commissioned, which the literature calls out
        /// as the failure a one-off validation study cannot catch. Hashed rather
        /// than sent in the clear only because a device serial number identifies a
        /// hospital's equipment and we have no use for that.
        /// </summary>
        public static string Scanner(
            string secretHex, string manufacturer, string model, string serial)
        {
            string a = Trim(manufacturer), b = Trim(model), c = Trim(serial);

            // A series with no device information at all gets no key. Hashing three
            // empty fields would hand every such series the *same* key, silently
            // collapsing unrelated patients onto one scanner identity, which is
            // worse than having none.
            if (a.Length == 0 && b.Length == 0 && c.Length == 0) return null;

            return Compute(secretHex, ScannerLabel, Join(a, b, c));
        }

        /// <summary>
        /// Lowercase hex HMAC-SHA256, or null when either input is missing.
        ///
        /// Returning null rather than hashing an empty string matters: the hash of
        /// "" is a perfectly valid-looking 64-character key that every series
        /// lacking a UID would share. Absent has to stay absent.
        /// </summary>
        public static string Compute(string secretHex, string label, string value)
        {
            if (string.IsNullOrWhiteSpace(secretHex)) return null;
            if (string.IsNullOrWhiteSpace(value)) return null;

            byte[] key = FromHex(secretHex);
            if (key == null || key.Length == 0) return null;

            using (var hmac = new HMACSHA256(key))
            {
                byte[] message = Encoding.UTF8.GetBytes(Join(label, value.Trim()));
                return ToHex(hmac.ComputeHash(message));
            }
        }

        /// <summary>
        /// Length-prefixed concatenation, so the parts cannot be confused.
        ///
        /// Netstring-style rather than delimiter-separated because a device model
        /// string may legally contain any character, so there is no delimiter that
        /// is safely absent from the inputs. Length prefixes make the encoding
        /// injective without banning anything: <c>("AB", "C")</c> and
        /// <c>("A", "BC")</c> produce different messages, so they cannot collide.
        /// </summary>
        private static string Join(params string[] parts)
        {
            var sb = new StringBuilder();
            foreach (string part in parts)
            {
                string value = part ?? string.Empty;
                sb.Append(value.Length.ToString(System.Globalization.CultureInfo.InvariantCulture));
                sb.Append(':');
                sb.Append(value);
            }
            return sb.ToString();
        }

        private static string Trim(string text)
        {
            return string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
        }

        private static byte[] FromHex(string hex)
        {
            hex = hex.Trim();
            if (hex.Length == 0 || hex.Length % 2 != 0) return null;

            var bytes = new byte[hex.Length / 2];
            for (int i = 0; i < bytes.Length; i++)
            {
                int hi = HexDigit(hex[i * 2]);
                int lo = HexDigit(hex[i * 2 + 1]);
                if (hi < 0 || lo < 0) return null;
                bytes[i] = (byte)((hi << 4) | lo);
            }
            return bytes;
        }

        private static int HexDigit(char c)
        {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return -1;
        }

        private static string ToHex(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes) sb.Append(b.ToString("x2"));
            return sb.ToString();
        }
    }
}
