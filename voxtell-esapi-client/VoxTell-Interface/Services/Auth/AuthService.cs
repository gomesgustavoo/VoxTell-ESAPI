using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services.Auth
{
    /// <summary>How the plugin is currently authenticated.</summary>
    public enum CredentialKind
    {
        None,
        ApiKey,
        Sso,
    }

    /// <summary>Progress from an interactive sign-in, so the UI can show what to do next.</summary>
    public class SignInPrompt
    {
        /// <summary>Human instruction, e.g. "Approve the sign-in in your browser".</summary>
        public string Message { get; set; }

        /// <summary>Device flow only: the URL to visit.</summary>
        public string VerificationUri { get; set; }

        /// <summary>Device flow only: the short code to type.</summary>
        public string UserCode { get; set; }
    }

    /// <summary>
    /// Acquires and renews the bearer token the API client sends.
    ///
    /// Two interactive grants, in preference order:
    /// <list type="number">
    /// <item><b>Authorization Code + PKCE</b> with a loopback redirect. One click in the
    /// browser, nothing to type.</item>
    /// <item><b>Device code</b>, when no loopback port binds or no browser is registered —
    /// which is a real state for a locked-down clinical workstation.</item>
    /// </list>
    ///
    /// Both send a PKCE challenge. That is not optional for the device flow either: enforcing
    /// PKCE on the Keycloak client enforces it for every authorization request the client makes,
    /// so a device authorization without a challenge fails with
    /// "invalid_request: Missing parameter: code_challenge_method".
    /// </summary>
    public sealed class AuthService : IDisposable
    {
        // Renew this far before expiry so a token cannot die mid-request. The realm issues
        // 300 s access tokens, so this is a fifth of their life.
        private static readonly TimeSpan RenewalMargin = TimeSpan.FromSeconds(60);

        private readonly HttpClient _http;
        private readonly Func<AuthConfigResponse> _configProvider;
        private readonly SemaphoreSlim _renewLock = new SemaphoreSlim(1, 1);

        private StoredCredentials _credentials;

        /// <param name="configProvider">
        /// Returns <c>/v1/auth/config</c>, fetched lazily through the API client so no realm URL
        /// is compiled in and the plugin follows a realm move without a rebuild.
        /// </param>
        public AuthService(Func<AuthConfigResponse> configProvider)
        {
            if (configProvider == null) throw new ArgumentNullException("configProvider");

            _configProvider = configProvider;
            _credentials = TokenStore.Load();

            // A dedicated client: these calls go to Keycloak, not to the VoxTell API, and must
            // never inherit the API's bearer header.
            _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
        }

        /// <summary>The persisted base URL, if the operator has overridden the default.</summary>
        public string SavedBaseUrl { get { return _credentials.BaseUrl; } }

        public CredentialKind Kind
        {
            get
            {
                if (!string.IsNullOrEmpty(_credentials.ApiKey)) return CredentialKind.ApiKey;
                if (!string.IsNullOrEmpty(_credentials.RefreshToken) ||
                    !string.IsNullOrEmpty(_credentials.AccessToken)) return CredentialKind.Sso;
                return CredentialKind.None;
            }
        }

        /// <summary>True when a call can be attempted without an interactive sign-in first.</summary>
        public bool HasCredential { get { return Kind != CredentialKind.None; } }

        /// <summary>Best-effort display name from the cached token; null for an API key.</summary>
        public string CachedDisplayName
        {
            get
            {
                return string.IsNullOrEmpty(_credentials.AccessToken)
                    ? null
                    : JwtPeek.GetDisplayName(_credentials.AccessToken);
            }
        }

        public void SaveBaseUrl(string baseUrl)
        {
            _credentials.BaseUrl = baseUrl;
            TokenStore.Save(_credentials);
        }

        public void SetApiKey(string apiKey)
        {
            // An API key and an SSO session are mutually exclusive: keeping both would make
            // which one authenticated a given call depend on precedence rules nobody can see.
            _credentials.ApiKey = string.IsNullOrWhiteSpace(apiKey) ? null : apiKey.Trim();
            if (_credentials.ApiKey != null)
            {
                _credentials.AccessToken = null;
                _credentials.RefreshToken = null;
                _credentials.AccessTokenExpiresAt = null;
            }
            TokenStore.Save(_credentials);
        }

        /// <summary>Drops every credential, locally. Keycloak's own session is left alone.</summary>
        public void SignOut()
        {
            string baseUrl = _credentials.BaseUrl;
            _credentials = new StoredCredentials { BaseUrl = baseUrl };
            TokenStore.Save(_credentials);
        }

        /// <summary>
        /// The value for the <c>Authorization: Bearer</c> header, refreshing first if the access
        /// token is close to expiry. Returns null when there is no usable credential and the
        /// caller must run an interactive sign-in.
        /// </summary>
        public async Task<string> GetBearerTokenAsync(CancellationToken ct)
        {
            if (!string.IsNullOrEmpty(_credentials.ApiKey))
                return _credentials.ApiKey;

            bool usable = !string.IsNullOrEmpty(_credentials.AccessToken)
                          && _credentials.AccessTokenExpiresAt.HasValue
                          && _credentials.AccessTokenExpiresAt.Value - RenewalMargin > DateTimeOffset.UtcNow;

            if (usable)
                return _credentials.AccessToken;

            if (!string.IsNullOrEmpty(_credentials.RefreshToken))
            {
                await _renewLock.WaitAsync(ct).ConfigureAwait(false);
                try
                {
                    // Re-check inside the lock: several calls can find the token stale at once,
                    // and a refresh token may be single-use, so a second exchange would fail
                    // and log the operator out for no reason.
                    if (!string.IsNullOrEmpty(_credentials.AccessToken)
                        && _credentials.AccessTokenExpiresAt.HasValue
                        && _credentials.AccessTokenExpiresAt.Value - RenewalMargin > DateTimeOffset.UtcNow)
                    {
                        return _credentials.AccessToken;
                    }

                    if (await TryRefreshAsync(ct).ConfigureAwait(false))
                        return _credentials.AccessToken;
                }
                finally
                {
                    _renewLock.Release();
                }
            }

            return null;
        }

        /// <summary>
        /// Forces a renewal after the API returned 401 — e.g. the token was revoked, or the
        /// clock drifted past what the server's 30 s leeway forgives.
        /// </summary>
        public async Task<bool> TryRenewAfterUnauthorizedAsync(CancellationToken ct)
        {
            if (!string.IsNullOrEmpty(_credentials.ApiKey))
                return false;   // A rejected API key will not become valid by retrying.

            if (string.IsNullOrEmpty(_credentials.RefreshToken))
                return false;

            await _renewLock.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                return await TryRefreshAsync(ct).ConfigureAwait(false);
            }
            finally
            {
                _renewLock.Release();
            }
        }

        // ------------------------------------------------------------------------------- //
        //  Interactive sign-in
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// Signs the operator in, preferring the browser redirect and falling back to the device
        /// code. <paramref name="report"/> is called with instructions to show; it may be invoked
        /// from a background thread, so the caller marshals.
        /// </summary>
        public async Task SignInAsync(Action<SignInPrompt> report, CancellationToken ct)
        {
            AuthConfigResponse cfg = _configProvider();
            if (cfg == null)
                throw new InvalidOperationException("The server did not return an auth configuration.");

            IEnumerable<int> ports = cfg.RedirectPorts ?? new[] { 47653, 47654, 47655 };

            using (LoopbackListener listener = LoopbackListener.TryBind(ports, cfg.RedirectPath))
            {
                if (listener != null)
                {
                    try
                    {
                        await SignInWithPkceAsync(cfg, listener, report, ct).ConfigureAwait(false);
                        return;
                    }
                    catch (OperationCanceledException)
                    {
                        throw;
                    }
                    catch (BrowserLaunchException)
                    {
                        // No registered browser. Fall through: the device flow can be completed
                        // from a phone or another machine entirely.
                    }
                }

                if (report != null)
                {
                    report(new SignInPrompt
                    {
                        Message = listener == null
                            ? "No local port was available for the browser redirect — using a sign-in code instead."
                            : "No browser could be opened here — using a sign-in code instead."
                    });
                }
            }

            await SignInWithDeviceCodeAsync(cfg, report, ct).ConfigureAwait(false);
        }

        private async Task SignInWithPkceAsync(
            AuthConfigResponse cfg, LoopbackListener listener,
            Action<SignInPrompt> report, CancellationToken ct)
        {
            Pkce pkce = Pkce.Create();
            string state = Pkce.RandomToken();

            string authUrl = cfg.AuthorizationEndpoint
                + "?client_id=" + Uri.EscapeDataString(cfg.ClientId)
                + "&response_type=code"
                + "&redirect_uri=" + Uri.EscapeDataString(listener.RedirectUri)
                + "&scope=" + Uri.EscapeDataString(cfg.Scopes ?? "openid profile email offline_access")
                + "&state=" + Uri.EscapeDataString(state)
                + "&code_challenge=" + pkce.Challenge
                + "&code_challenge_method=" + (cfg.PkceMethod ?? Pkce.Method);

            // Start listening before the browser opens, so a fast redirect cannot beat us.
            Task<Dictionary<string, string>> callback = listener.WaitForCallbackAsync(ct);

            OpenBrowser(authUrl);

            if (report != null)
            {
                report(new SignInPrompt
                {
                    Message = "Waiting for you to finish signing in, in your browser..."
                });
            }

            Dictionary<string, string> query = await callback.ConfigureAwait(false);

            string error;
            if (query.TryGetValue("error", out error))
            {
                string description;
                query.TryGetValue("error_description", out description);
                throw new AuthenticationFailedException(
                    string.Format("Sign-in was refused: {0}{1}", error,
                        string.IsNullOrEmpty(description) ? "" : " - " + description));
            }

            string returnedState;
            query.TryGetValue("state", out returnedState);

            // Constant-time-ish comparison is unnecessary here, but the check itself is not:
            // without it, an attacker who can reach the loopback port could inject their own
            // authorization code and bind the plugin to their account.
            if (returnedState != state)
                throw new AuthenticationFailedException("Sign-in state did not match; the response was discarded.");

            string code;
            if (!query.TryGetValue("code", out code) || string.IsNullOrEmpty(code))
                throw new AuthenticationFailedException("The sign-in returned no authorization code.");

            var form = new Dictionary<string, string>
            {
                { "grant_type", "authorization_code" },
                { "client_id", cfg.ClientId },
                { "code", code },
                { "redirect_uri", listener.RedirectUri },
                { "code_verifier", pkce.Verifier },
            };

            TokenResponse token = await PostTokenAsync(cfg.TokenEndpoint, form, ct).ConfigureAwait(false);
            Store(token);
        }

        private async Task SignInWithDeviceCodeAsync(
            AuthConfigResponse cfg, Action<SignInPrompt> report, CancellationToken ct)
        {
            Pkce pkce = Pkce.Create();

            var startForm = new Dictionary<string, string>
            {
                { "client_id", cfg.ClientId },
                { "scope", cfg.Scopes ?? "openid profile email offline_access" },
                // Required even here — see the class remarks.
                { "code_challenge", pkce.Challenge },
                { "code_challenge_method", cfg.PkceMethod ?? Pkce.Method },
            };

            string body;
            using (var content = new FormUrlEncodedContent(startForm))
            using (HttpResponseMessage response =
                       await _http.PostAsync(cfg.DeviceAuthorizationEndpoint, content, ct).ConfigureAwait(false))
            {
                body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                if (!response.IsSuccessStatusCode)
                {
                    throw new AuthenticationFailedException(
                        "Could not start the sign-in: " + Summarise(body));
                }
            }

            DeviceAuthorizationResponse device =
                JsonConvert.DeserializeObject<DeviceAuthorizationResponse>(body);

            if (report != null)
            {
                report(new SignInPrompt
                {
                    Message = "Open this address and enter the code to sign in:",
                    VerificationUri = device.VerificationUri,
                    UserCode = device.UserCode,
                });
            }

            // Offer the browser, but do not depend on it: the whole point of this fallback is
            // that the code can be entered on any device.
            if (!string.IsNullOrEmpty(device.VerificationUriComplete))
            {
                try { OpenBrowser(device.VerificationUriComplete); }
                catch (BrowserLaunchException) { }
            }

            int interval = device.Interval > 0 ? device.Interval : 5;
            DateTimeOffset deadline = DateTimeOffset.UtcNow.AddSeconds(
                device.ExpiresIn > 0 ? device.ExpiresIn : 600);

            var pollForm = new Dictionary<string, string>
            {
                { "grant_type", "urn:ietf:params:oauth:grant-type:device_code" },
                { "client_id", cfg.ClientId },
                { "device_code", device.DeviceCode },
                { "code_verifier", pkce.Verifier },
            };

            while (true)
            {
                ct.ThrowIfCancellationRequested();

                if (DateTimeOffset.UtcNow > deadline)
                    throw new AuthenticationFailedException("The sign-in code expired. Try again.");

                await Task.Delay(TimeSpan.FromSeconds(interval), ct).ConfigureAwait(false);

                TokenResponse token;
                try
                {
                    token = await PostTokenAsync(cfg.TokenEndpoint, pollForm, ct).ConfigureAwait(false);
                }
                catch (OAuthErrorException ex)
                {
                    switch (ex.Error)
                    {
                        case "authorization_pending":
                            continue;
                        case "slow_down":
                            // RFC 8628: back off by 5 s and keep the new interval.
                            interval += 5;
                            continue;
                        case "expired_token":
                            throw new AuthenticationFailedException("The sign-in code expired. Try again.");
                        case "access_denied":
                            throw new AuthenticationFailedException("The sign-in was declined.");
                        default:
                            throw new AuthenticationFailedException("Sign-in failed: " + ex.Message);
                    }
                }

                Store(token);
                return;
            }
        }

        // ------------------------------------------------------------------------------- //
        //  Token plumbing
        // ------------------------------------------------------------------------------- //

        private async Task<bool> TryRefreshAsync(CancellationToken ct)
        {
            AuthConfigResponse cfg;
            try
            {
                cfg = _configProvider();
            }
            catch
            {
                return false;   // Server unreachable; the caller will surface that itself.
            }
            if (cfg == null) return false;

            var form = new Dictionary<string, string>
            {
                { "grant_type", "refresh_token" },
                { "client_id", cfg.ClientId },
                { "refresh_token", _credentials.RefreshToken },
            };

            try
            {
                TokenResponse token = await PostTokenAsync(cfg.TokenEndpoint, form, ct).ConfigureAwait(false);
                Store(token);
                return true;
            }
            catch (OAuthErrorException)
            {
                // invalid_grant: revoked, or the offline session was ended in Keycloak. The
                // credential is dead, so drop it and make the UI ask for a fresh sign-in
                // rather than retrying a token that will never work again.
                _credentials.AccessToken = null;
                _credentials.RefreshToken = null;
                _credentials.AccessTokenExpiresAt = null;
                TokenStore.Save(_credentials);
                return false;
            }
            catch (HttpRequestException)
            {
                // Transient. Keep the refresh token; the next attempt may succeed.
                return false;
            }
        }

        private async Task<TokenResponse> PostTokenAsync(
            string endpoint, Dictionary<string, string> form, CancellationToken ct)
        {
            using (var content = new FormUrlEncodedContent(form))
            using (HttpResponseMessage response =
                       await _http.PostAsync(endpoint, content, ct).ConfigureAwait(false))
            {
                string body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);

                TokenResponse token = null;
                try { token = JsonConvert.DeserializeObject<TokenResponse>(body); }
                catch { }

                if (token != null && !string.IsNullOrEmpty(token.Error))
                    throw new OAuthErrorException(token.Error, token.ErrorDescription);

                if (!response.IsSuccessStatusCode)
                    throw new AuthenticationFailedException("Token request failed: " + Summarise(body));

                if (token == null || string.IsNullOrEmpty(token.AccessToken))
                    throw new AuthenticationFailedException("The token response contained no access token.");

                return token;
            }
        }

        private void Store(TokenResponse token)
        {
            _credentials.ApiKey = null;
            _credentials.AccessToken = token.AccessToken;

            // Keycloak returns a fresh refresh token on each exchange; keep the old one if it
            // ever omits it, so a rotation-free response does not silently end the session.
            if (!string.IsNullOrEmpty(token.RefreshToken))
                _credentials.RefreshToken = token.RefreshToken;

            // Trust the token's own exp over expires_in where available: it is what the server
            // will actually enforce, and it sidesteps clock skew between issue and receipt.
            DateTimeOffset? exp = JwtPeek.GetExpiry(token.AccessToken);
            _credentials.AccessTokenExpiresAt = exp
                ?? DateTimeOffset.UtcNow.AddSeconds(token.ExpiresIn > 0 ? token.ExpiresIn : 300);

            TokenStore.Save(_credentials);
        }

        private static void OpenBrowser(string url)
        {
            try
            {
                // UseShellExecute is the default on .NET Framework, which is what makes this
                // hand off to the user's registered browser rather than trying to exec a URL.
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                throw new BrowserLaunchException(
                    "No web browser could be opened on this workstation.", ex);
            }
        }

        private static string Summarise(string body)
        {
            if (string.IsNullOrEmpty(body)) return "(no response body)";
            body = body.Trim();
            return body.Length <= 300 ? body : body.Substring(0, 300) + "...";
        }

        public void Dispose()
        {
            _http.Dispose();
            _renewLock.Dispose();
        }
    }

    /// <summary>An interactive sign-in that cannot be completed; the message is user-facing.</summary>
    public class AuthenticationFailedException : Exception
    {
        public AuthenticationFailedException(string message) : base(message) { }
        public AuthenticationFailedException(string message, Exception inner) : base(message, inner) { }
    }

    /// <summary>A structured OAuth2 error response, so the device-flow poll can branch on it.</summary>
    internal class OAuthErrorException : Exception
    {
        public OAuthErrorException(string error, string description)
            : base(string.IsNullOrEmpty(description) ? error : error + " - " + description)
        {
            Error = error;
        }

        public string Error { get; private set; }
    }

    /// <summary>No browser could be launched, so the redirect flow is unavailable here.</summary>
    internal class BrowserLaunchException : Exception
    {
        public BrowserLaunchException(string message, Exception inner) : base(message, inner) { }
    }
}
