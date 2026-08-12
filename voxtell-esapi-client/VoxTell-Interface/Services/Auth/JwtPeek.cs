using System;
using System.Text;
using Newtonsoft.Json.Linq;

namespace VoxTell_Interface.Services.Auth
{
    /// <summary>
    /// Reads the unverified claims out of a JWT payload.
    ///
    /// The plugin deliberately does NOT validate signatures. That is the API's job — it checks
    /// RS256 against the realm JWKS, the issuer, and the <c>voxtell-api</c> audience, and a
    /// client-side check would add no security while pulling in the whole
    /// <c>Microsoft.IdentityModel</c> stack as extra DLLs to deploy. All the plugin needs is
    /// <c>exp</c>, so it can refresh before a call fails, and a name to show in the UI.
    ///
    /// Treat everything this returns as display-only, never as an authorisation decision.
    /// </summary>
    internal static class JwtPeek
    {
        /// <summary>
        /// Expiry as a UTC instant, or null if the token is unparseable or has no <c>exp</c>.
        /// Null means "assume expired" at every call site.
        /// </summary>
        public static DateTimeOffset? GetExpiry(string jwt)
        {
            JObject claims = TryReadPayload(jwt);
            if (claims == null) return null;

            JToken exp = claims["exp"];
            if (exp == null) return null;

            try
            {
                return DateTimeOffset.FromUnixTimeSeconds(exp.Value<long>());
            }
            catch
            {
                return null;
            }
        }

        /// <summary>The friendliest available identity claim, for the account panel.</summary>
        public static string GetDisplayName(string jwt)
        {
            JObject claims = TryReadPayload(jwt);
            if (claims == null) return null;

            foreach (string name in new[] { "email", "preferred_username", "name", "sub" })
            {
                JToken value = claims[name];
                if (value != null && value.Type == JTokenType.String)
                {
                    string s = value.Value<string>();
                    if (!string.IsNullOrEmpty(s)) return s;
                }
            }
            return null;
        }

        private static JObject TryReadPayload(string jwt)
        {
            if (string.IsNullOrEmpty(jwt)) return null;

            // An opaque vxt_ API key is not a JWT and has no claims — callers pass whatever
            // credential they hold, so this must not throw on one.
            string[] parts = jwt.Split('.');
            if (parts.Length < 2) return null;

            try
            {
                byte[] payload = Pkce.FromBase64Url(parts[1]);
                return JObject.Parse(Encoding.UTF8.GetString(payload));
            }
            catch
            {
                return null;
            }
        }
    }
}
