using System;
using System.Security.Cryptography;
using System.Text;

namespace VoxTell_Interface.Services.Auth
{
    /// <summary>
    /// PKCE (RFC 7636) code verifier and challenge, plus the base64url helpers the OAuth flows
    /// need. Hand-rolled rather than pulled from a library: the plugin ships as loose DLLs next
    /// to the .esapi.dll, so every added dependency is another file that must land in Eclipse's
    /// scripts directory, and this is thirty lines.
    /// </summary>
    internal sealed class Pkce
    {
        private Pkce(string verifier, string challenge)
        {
            Verifier = verifier;
            Challenge = challenge;
        }

        public string Verifier { get; private set; }
        public string Challenge { get; private set; }

        /// <summary>Always S256 — the realm advertises it and the client enforces it.</summary>
        public const string Method = "S256";

        public static Pkce Create()
        {
            // 32 bytes -> 43 base64url chars, inside RFC 7636's 43..128 range.
            byte[] entropy = new byte[32];
            using (var rng = new RNGCryptoServiceProvider())
                rng.GetBytes(entropy);

            string verifier = Base64Url(entropy);

            using (var sha = SHA256.Create())
            {
                // The hash is over the ASCII of the verifier string, not the raw entropy.
                byte[] digest = sha.ComputeHash(Encoding.ASCII.GetBytes(verifier));
                return new Pkce(verifier, Base64Url(digest));
            }
        }

        /// <summary>Cryptographically random base64url token, for <c>state</c> and <c>nonce</c>.</summary>
        public static string RandomToken(int bytes = 24)
        {
            byte[] buffer = new byte[bytes];
            using (var rng = new RNGCryptoServiceProvider())
                rng.GetBytes(buffer);
            return Base64Url(buffer);
        }

        public static string Base64Url(byte[] data)
        {
            return Convert.ToBase64String(data)
                .TrimEnd('=')
                .Replace('+', '-')
                .Replace('/', '_');
        }

        /// <summary>Decodes base64url, restoring the padding the encoding strips.</summary>
        public static byte[] FromBase64Url(string value)
        {
            string s = value.Replace('-', '+').Replace('_', '/');
            switch (s.Length % 4)
            {
                case 2: s += "=="; break;
                case 3: s += "="; break;
                case 1: throw new FormatException("Not a valid base64url string.");
            }
            return Convert.FromBase64String(s);
        }
    }
}
