using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;

namespace VoxTell_Interface.Services.Auth
{
    /// <summary>What is persisted between Eclipse sessions.</summary>
    internal sealed class StoredCredentials
    {
        [JsonProperty("base_url")] public string BaseUrl { get; set; }

        /// <summary>A <c>vxt_</c> key, when the site uses one instead of interactive sign-in.</summary>
        [JsonProperty("api_key")] public string ApiKey { get; set; }

        [JsonProperty("access_token")] public string AccessToken { get; set; }

        /// <summary>
        /// An offline refresh token. This is the whole reason anything is persisted: access
        /// tokens live 300 s and Eclipse launches the plugin fresh on every run, so without it
        /// the planner would sign in again every single time.
        /// </summary>
        [JsonProperty("refresh_token")] public string RefreshToken { get; set; }

        [JsonProperty("access_token_expires_at")] public DateTimeOffset? AccessTokenExpiresAt { get; set; }
    }

    /// <summary>
    /// Reads and writes the credential file under <c>%LOCALAPPDATA%\VoxTell</c>, encrypted with
    /// DPAPI at <see cref="DataProtectionScope.CurrentUser"/>.
    ///
    /// DPAPI rather than a hand-rolled cipher because the key is managed by Windows and bound to
    /// the logged-in user: another account on the same shared workstation — which is the normal
    /// case in a clinic — cannot read it, and there is no key for the plugin to hide. LocalAppData
    /// rather than AppData so the blob never follows a roaming profile onto another machine,
    /// where DPAPI could not decrypt it anyway.
    /// </summary>
    internal static class TokenStore
    {
        private const string DirectoryName = "VoxTell";
        private const string FileName = "credentials.dat";

        // Ties the ciphertext to this application. Not a secret — it only stops a blob from one
        // application being fed to another as valid input.
        private static readonly byte[] Entropy =
            Encoding.UTF8.GetBytes("VoxTell-ESAPI/credentials/v2");

        public static string FilePath
        {
            get
            {
                string root = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(root, DirectoryName, FileName);
            }
        }

        /// <summary>
        /// Never throws. A corrupt, truncated, or foreign-user credential file is not an error
        /// worth blocking the plugin on — the operator can just sign in again.
        /// </summary>
        public static StoredCredentials Load()
        {
            try
            {
                string path = FilePath;
                if (!File.Exists(path)) return new StoredCredentials();

                byte[] plain = ProtectedData.Unprotect(
                    File.ReadAllBytes(path), Entropy, DataProtectionScope.CurrentUser);

                return JsonConvert.DeserializeObject<StoredCredentials>(
                           Encoding.UTF8.GetString(plain)) ?? new StoredCredentials();
            }
            catch
            {
                return new StoredCredentials();
            }
        }

        /// <summary>
        /// Returns false rather than throwing when the profile is not writable — a locked-down
        /// workstation should degrade to "sign in each session", not refuse to run.
        /// </summary>
        public static bool Save(StoredCredentials credentials)
        {
            try
            {
                string path = FilePath;
                Directory.CreateDirectory(Path.GetDirectoryName(path));

                byte[] cipher = ProtectedData.Protect(
                    Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(credentials)),
                    Entropy, DataProtectionScope.CurrentUser);

                // Write-then-replace so an interrupted save cannot leave a half-written file
                // that reads as corrupt and silently discards a working refresh token.
                string temp = path + ".tmp";
                File.WriteAllBytes(temp, cipher);
                if (File.Exists(path)) File.Delete(path);
                File.Move(temp, path);
                return true;
            }
            catch
            {
                return false;
            }
        }

        public static void Clear()
        {
            try
            {
                if (File.Exists(FilePath)) File.Delete(FilePath);
            }
            catch
            {
                // Nothing useful to do; the in-memory session has already been dropped.
            }
        }
    }
}
