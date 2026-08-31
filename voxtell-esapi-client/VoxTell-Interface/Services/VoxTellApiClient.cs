using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services.Auth;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// The VoxTell-Cloud v2 client (voxtell-cloud/PROTOCOL.md).
    ///
    /// Two <see cref="HttpClient"/>s on purpose. <c>_api</c> carries the bearer token;
    /// <c>_storage</c> carries no headers at all, because a presigned S3 URL already contains
    /// its own SigV4 signature and an extra <c>Authorization</c> — or a rewritten <c>Host</c> —
    /// makes the object store reject it.
    /// </summary>
    public sealed class VoxTellApiClient : IDisposable
    {
        public const string DefaultBaseUrl = "https://voxtell.dicomsegvr.com/v1";

        // Cloudflare's 52x/530 family joins the usual gateway errors: results are fetched from
        // the object store through the CF edge, and a tunnel hiccup surfaces as 530 rather than
        // a normal 502. Seen in testing against this deployment.
        //
        // 408 and 499 are here because a reverse proxy that gives up reading a request body
        // reports one or the other. That is not hypothetical: Traefik v3 defaults
        // respondingTimeouts.readTimeout to 60 s and that clock covers the body, so a slow part
        // upload was cut mid-stream and came back 499. Both are transient and a part PUT is
        // idempotent, so retrying is safe and usually succeeds.
        private static readonly HashSet<int> RetryableStatuses = new HashSet<int>
        {
            408, 499, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530
        };

        /// <summary>
        /// Statuses that mean "the connection was cut", as opposed to "the server said no".
        /// Worth naming, because the bare number tells a planner nothing.
        /// </summary>
        private static bool IsConnectionCut(int status)
        {
            return status == 408 || status == 499 || status == 524;
        }

        private const int MaxAttempts = 5;

        private readonly HttpClient _api;
        private readonly HttpClient _storage;
        private readonly AuthService _auth;

        private string _baseUrl;
        private AuthConfigResponse _authConfig;

        // Cached like _authConfig, and invalidated by SetBaseUrl for the same
        // reason: a different deployment can offer a different set of models, and
        // a stale catalog would let the panel offer a model the server will reject.
        private ModelCatalog _catalog;

        /// <summary>
        /// Set to reach Traefik directly before the public DNS name exists. Applies ONLY to API
        /// calls — putting it on a presigned PUT would break the signature, since SigV4 signs
        /// the Host header.
        /// </summary>
        public string HostHeaderOverride { get; set; }

        public VoxTellApiClient(string baseUrl, AuthService auth)
        {
            _baseUrl = NormaliseBaseUrl(baseUrl);
            _auth = auth;

            // TLS 1.2 explicitly. It is already the default on 4.6.2, but a machine-level
            // registry policy can disable it, and the failure then looks like a network
            // outage rather than a protocol mismatch.
            try
            {
                ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            }
            catch (NotSupportedException)
            {
                // An older platform without the enum value; the default will have to do.
            }

            // 100-continue costs a round trip per part PUT for nothing, and the default of two
            // connections per endpoint throttles the multipart upload.
            ServicePointManager.Expect100Continue = false;
            if (ServicePointManager.DefaultConnectionLimit < 8)
                ServicePointManager.DefaultConnectionLimit = 8;

            _api = new HttpClient(CreateHandler(followRedirects: false))
            {
                // Per-call timeouts are applied with linked CancellationTokens; this is only a
                // backstop so a wedged socket cannot hang the plugin forever.
                Timeout = Timeout.InfiniteTimeSpan,
            };
            _api.DefaultRequestHeaders.UserAgent.ParseAdd("VoxTell-ESAPI/2.0");

            _storage = new HttpClient(CreateHandler(followRedirects: false))
            {
                Timeout = Timeout.InfiniteTimeSpan,
            };
        }

        private static HttpClientHandler CreateHandler(bool followRedirects)
        {
            var handler = new HttpClientHandler
            {
                // Never follow automatically. GET /jobs/{id}/result redirects to another origin,
                // and .NET Framework is not consistent about dropping the Authorization header
                // on that hop — a leaked header makes S3 reject the presigned signature. The
                // redirect is followed explicitly, on the header-free client.
                AllowAutoRedirect = followRedirects,
                AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate,
            };

            // Hospital networks routinely sit behind an authenticating proxy; without this a
            // 407 surfaces as an opaque failure with nothing actionable in it.
            try
            {
                handler.Proxy = WebRequest.GetSystemWebProxy();
                handler.UseProxy = true;
                handler.UseDefaultCredentials = true;
            }
            catch
            {
                // No system proxy configuration; a direct connection is the right assumption.
            }

            return handler;
        }

        public string BaseUrl { get { return _baseUrl; } }

        public void SetBaseUrl(string baseUrl)
        {
            _baseUrl = NormaliseBaseUrl(baseUrl);
            _authConfig = null;     // Endpoints belong to the old host.
            _catalog = null;        // So do the models it offers.
        }

        private static string NormaliseBaseUrl(string baseUrl)
        {
            if (string.IsNullOrWhiteSpace(baseUrl)) return DefaultBaseUrl;

            // A trailing slash matters: FastAPI registers POST /v1/jobs, and POST /v1/jobs/
            // answers with a 307 that .NET may replay as a GET.
            return baseUrl.Trim().TrimEnd('/');
        }

        // ------------------------------------------------------------------------------- //
        //  System and auth
        // ------------------------------------------------------------------------------- //

        public async Task<HealthResponse> GetHealthAsync(CancellationToken ct)
        {
            return await SendJsonAsync<HealthResponse>(
                HttpMethod.Get, "/health", null, authenticated: false,
                timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);
        }

        /// <summary>
        /// Fetches and caches <c>/v1/auth/config</c>. Unauthenticated by design — it is the
        /// bootstrap that tells the plugin where to sign in.
        /// </summary>
        public async Task<AuthConfigResponse> GetAuthConfigAsync(CancellationToken ct)
        {
            if (_authConfig != null) return _authConfig;

            _authConfig = await SendJsonAsync<AuthConfigResponse>(
                HttpMethod.Get, "/auth/config", null, authenticated: false,
                timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);

            return _authConfig;
        }

        /// <summary>
        /// The config from the last <see cref="GetAuthConfigAsync"/>, or null if it has not been
        /// fetched for this base URL yet.
        ///
        /// This is what <see cref="AuthService"/> reads, and it is deliberately synchronous:
        /// the token refresh path needs the token endpoint, and having it re-enter this client
        /// to fetch config — while that same client is mid-request waiting for a token — is a
        /// cycle. The caller fetches config once up front instead.
        /// </summary>
        public AuthConfigResponse CachedAuthConfig { get { return _authConfig; } }

        /// <summary>Validates whichever credential is held, and returns quota and in-flight counts.</summary>
        public async Task<MeResponse> GetMeAsync(CancellationToken ct)
        {
            return await SendJsonAsync<MeResponse>(
                HttpMethod.Get, "/me", null, authenticated: true,
                timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);
        }

        // ------------------------------------------------------------------------------- //
        //  Catalog
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// Fetches and caches <c>/v1/models</c>: which models and structures this
        /// deployment offers.
        ///
        /// Unauthenticated, like <c>/auth/config</c> — it holds no patient data and
        /// no tenant-specific information, and the panel needs it to render its
        /// model picker before the planner has necessarily signed in.
        ///
        /// Retried, unlike <see cref="CreateJobAsync"/>: a GET is idempotent, and a
        /// panel that cannot list models is useless, so it is worth waiting out a
        /// transient 503.
        /// </summary>
        public async Task<ModelCatalog> GetCatalogAsync(CancellationToken ct)
        {
            if (_catalog != null) return _catalog;

            _catalog = await SendJsonAsync<ModelCatalog>(
                HttpMethod.Get, "/models", null, authenticated: false,
                timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);

            return _catalog;
        }

        /// <summary>The catalog from the last fetch, or null. Synchronous, for the view.</summary>
        public ModelCatalog CachedCatalog { get { return _catalog; } }

        // ------------------------------------------------------------------------------- //
        //  QA baselines
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// Records a structure snapshot as the QA baseline for its series.
        ///
        /// Safe to retry, and that is a property of the server contract rather than
        /// an accident: the record is deduplicated on
        /// <c>(series_key, structure_set_sha256)</c>, so sending the same snapshot
        /// twice returns the same baseline with <c>created: false</c> instead of
        /// creating a second one. It must never bill twice for one structure set,
        /// because the planner reopening a patient before editing anything is the
        /// normal case, not an edge case.
        /// </summary>
        public async Task<BaselineResponse> PostBaselineAsync(
            StructureSnapshot snapshot, string jobId, CancellationToken ct)
        {
            string path = "/qa/baselines";
            if (!string.IsNullOrEmpty(jobId))
            {
                path += "?job_id=" + Uri.EscapeDataString(jobId);
            }

            return await SendJsonAsync<BaselineResponse>(
                HttpMethod.Post, path, snapshot, authenticated: true,
                timeout: TimeSpan.FromSeconds(120), ct: ct).ConfigureAwait(false);
        }

        // ------------------------------------------------------------------------------- //
        //  Jobs
        // ------------------------------------------------------------------------------- //

        public async Task<JobCreatedResponse> CreateJobAsync(JobCreateRequest request, CancellationToken ct)
        {
            // Deliberately not retried. POST /v1/jobs is not idempotent: a retry that the
            // server actually received leaves an orphan awaiting_upload job holding one of the
            // caller's six outstanding slots and a whole month-quota unit.
            return await SendJsonAsync<JobCreatedResponse>(
                HttpMethod.Post, "/jobs", request, authenticated: true,
                timeout: TimeSpan.FromSeconds(60), ct: ct, retry: false).ConfigureAwait(false);
        }

        /// <summary>
        /// Uploads the blob to the presigned part URLs and returns the ETags for
        /// <see cref="SubmitJobAsync"/>. Sequential rather than parallel: a clinical LAN is
        /// rarely the bottleneck, and one part at a time keeps the progress meaningful.
        /// </summary>
        public async Task<List<SubmitPart>> UploadPartsAsync(
            byte[] blob, JobCreatedResponse job,
            Action<int, int, long, long> progress, CancellationToken ct)
        {
            var parts = new List<SubmitPart>();
            int total = job.Upload.Count;
            long uploaded = 0;

            for (int i = 0; i < total; i++)
            {
                ct.ThrowIfCancellationRequested();

                UploadPart part = job.Upload[i];

                // Computed in long: (part_number - 1) * 32 MiB overflows int at 64 parts, and
                // while the 1 GiB upload cap keeps real jobs at 32 or fewer, a silently
                // negative offset is not the way to discover that a limit moved.
                long offset64 = (long)(part.PartNumber - 1) * job.PartSize;
                if (offset64 < 0 || offset64 >= blob.LongLength)
                {
                    throw new InvalidOperationException(string.Format(
                        "Part {0} starts at byte {1:N0}, outside a {2:N0}-byte blob. The " +
                        "declared upload_bytes did not match what was encoded.",
                        part.PartNumber, offset64, blob.LongLength));
                }

                int offset = (int)offset64;
                int length = (int)Math.Min(job.PartSize, blob.LongLength - offset64);

                string etag = await PutPartAsync(part.Url, blob, offset, length, ct).ConfigureAwait(false);

                parts.Add(new SubmitPart { PartNumber = part.PartNumber, ETag = etag });
                uploaded += length;

                if (progress != null)
                    progress(i + 1, total, uploaded, blob.LongLength);
            }

            return parts;
        }

        private async Task<string> PutPartAsync(
            string url, byte[] blob, int offset, int length, CancellationToken ct)
        {
            for (int attempt = 1; ; attempt++)
            {
                using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
                // A fresh request and content per attempt — neither can be resent once a
                // failed try has consumed the content stream.
                using (var request = new HttpRequestMessage(HttpMethod.Put, url))
                {
                    // Generous: a part is 32 MiB and a hospital uplink can be slow.
                    cts.CancelAfter(TimeSpan.FromMinutes(10));

                    // No Authorization and no Host override here: the presigned URL carries its
                    // own SigV4 signature, which covers the host, so either would break it.
                    request.Content = new ByteArrayContent(blob, offset, length);
                    request.Content.Headers.ContentType =
                        new MediaTypeHeaderValue("application/octet-stream");

                    try
                    {
                        using (HttpResponseMessage response =
                                   await _storage.SendAsync(request, cts.Token).ConfigureAwait(false))
                        {
                            if (response.IsSuccessStatusCode)
                            {
                                // The ETag identifies the stored part and must be echoed back
                                // to /submit exactly, quotes included. ETag.Tag keeps them.
                                string etag = response.Headers.ETag != null
                                    ? response.Headers.ETag.Tag
                                    : null;

                                if (string.IsNullOrEmpty(etag))
                                {
                                    throw new InvalidOperationException(
                                        "The object store accepted a part but returned no ETag, " +
                                        "so the upload cannot be completed.");
                                }
                                return etag;
                            }

                            if (attempt < MaxAttempts && RetryableStatuses.Contains((int)response.StatusCode))
                            {
                                await BackoffAsync(attempt, ct).ConfigureAwait(false);
                                continue;
                            }

                            string body = await SafeReadAsync(response).ConfigureAwait(false);
                            int status = (int)response.StatusCode;

                            // A cut connection is the failure mode this path actually hits, and
                            // "HTTP 499" on its own sends a planner nowhere. Name the cause.
                            string message = IsConnectionCut(status)
                                ? string.Format(
                                    "The connection was cut while uploading the image (HTTP {0}), " +
                                    "and {1} attempts all failed. A proxy or gateway between this " +
                                    "workstation and the server is timing the upload out. The " +
                                    "network may be too slow for the current part size — report " +
                                    "this, it is fixable on the server.", status, MaxAttempts)
                                : string.Format("Uploading part failed with HTTP {0}. {1}",
                                    status, Summarise(body));

                            throw new VoxTellApiException(status, null, message, null, body);
                        }
                    }
                    catch (HttpRequestException) when (attempt < MaxAttempts)
                    {
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException) when (!ct.IsCancellationRequested && attempt < MaxAttempts)
                    {
                        // The per-attempt timeout fired, not the caller's cancellation.
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                    }
                }
            }
        }

        /// <summary>Assembles the object and enqueues the job.</summary>
        public async Task<JobStatusResponse> SubmitJobAsync(
            string jobId, List<SubmitPart> parts, CancellationToken ct)
        {
            var body = new JobSubmitRequest { Parts = parts };

            // Safe to retry: submitting the same completed part set twice either succeeds or
            // returns a 409 that says the job already moved on.
            return await SendJsonAsync<JobStatusResponse>(
                HttpMethod.Post, "/jobs/" + Uri.EscapeDataString(jobId) + "/submit", body,
                authenticated: true, timeout: TimeSpan.FromSeconds(120), ct: ct).ConfigureAwait(false);
        }

        public async Task<JobStatusResponse> GetJobAsync(string jobId, CancellationToken ct)
        {
            return await SendJsonAsync<JobStatusResponse>(
                HttpMethod.Get, "/jobs/" + Uri.EscapeDataString(jobId), null,
                authenticated: true, timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);
        }

        public async Task<JobStatusResponse> CancelJobAsync(string jobId, CancellationToken ct)
        {
            return await SendJsonAsync<JobStatusResponse>(
                HttpMethod.Post, "/jobs/" + Uri.EscapeDataString(jobId) + "/cancel", null,
                authenticated: true, timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);
        }

        public async Task DeleteJobAsync(string jobId, CancellationToken ct)
        {
            await SendJsonAsync<object>(
                HttpMethod.Delete, "/jobs/" + Uri.EscapeDataString(jobId), null,
                authenticated: true, timeout: TimeSpan.FromSeconds(30), ct: ct).ConfigureAwait(false);
        }

        /// <summary>
        /// Downloads and un-gzips <c>result.json.gz</c>.
        ///
        /// Two hops: the API answers 307 with a presigned URL, which is then fetched on the
        /// header-free client so no <c>Authorization</c> can reach the object store.
        /// </summary>
        public async Task<ResultEnvelope> GetResultAsync(string jobId, CancellationToken ct)
        {
            string path = "/jobs/" + Uri.EscapeDataString(jobId) + "/result";

            Uri location;
            using (HttpResponseMessage redirect = await SendAsync(
                       HttpMethod.Get, path, null, authenticated: true,
                       timeout: TimeSpan.FromSeconds(60), ct: ct).ConfigureAwait(false))
            {
                if ((int)redirect.StatusCode == 307 || (int)redirect.StatusCode == 302)
                {
                    location = redirect.Headers.Location;
                    if (location == null)
                        throw new InvalidOperationException("The result redirect carried no Location header.");
                }
                else
                {
                    await ThrowIfFailedAsync(redirect).ConfigureAwait(false);
                    throw new InvalidOperationException(string.Format(
                        "Expected a redirect to the result object, got HTTP {0}.",
                        (int)redirect.StatusCode));
                }
            }

            byte[] gzipped = await GetStorageBytesAsync(location, ct).ConfigureAwait(false);
            string json = Gunzip(gzipped);

            return JsonConvert.DeserializeObject<ResultEnvelope>(json);
        }

        private async Task<byte[]> GetStorageBytesAsync(Uri url, CancellationToken ct)
        {
            for (int attempt = 1; ; attempt++)
            {
                using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
                {
                    cts.CancelAfter(TimeSpan.FromMinutes(5));

                    try
                    {
                        using (HttpResponseMessage response = await _storage
                                   .GetAsync(url, HttpCompletionOption.ResponseHeadersRead, cts.Token)
                                   .ConfigureAwait(false))
                        {
                            if (response.IsSuccessStatusCode)
                                return await response.Content.ReadAsByteArrayAsync().ConfigureAwait(false);

                            if (attempt < MaxAttempts && RetryableStatuses.Contains((int)response.StatusCode))
                            {
                                await BackoffAsync(attempt, ct).ConfigureAwait(false);
                                continue;
                            }

                            string body = await SafeReadAsync(response).ConfigureAwait(false);
                            throw new VoxTellApiException(
                                (int)response.StatusCode, null,
                                string.Format("Downloading the result failed with HTTP {0}. {1}",
                                    (int)response.StatusCode, Summarise(body)),
                                null, body);
                        }
                    }
                    catch (HttpRequestException) when (attempt < MaxAttempts)
                    {
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException) when (!ct.IsCancellationRequested && attempt < MaxAttempts)
                    {
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                    }
                }
            }
        }

        private static string Gunzip(byte[] data)
        {
            using (var input = new MemoryStream(data))
            using (var gzip = new GZipStream(input, CompressionMode.Decompress))
            using (var reader = new StreamReader(gzip, Encoding.UTF8))
            {
                return reader.ReadToEnd();
            }
        }

        // ------------------------------------------------------------------------------- //
        //  Transport
        // ------------------------------------------------------------------------------- //

        private async Task<T> SendJsonAsync<T>(
            HttpMethod method, string path, object body, bool authenticated,
            TimeSpan timeout, CancellationToken ct, bool retry = true)
        {
            using (HttpResponseMessage response = await SendAsync(
                       method, path, body, authenticated, timeout, ct, retry).ConfigureAwait(false))
            {
                await ThrowIfFailedAsync(response).ConfigureAwait(false);

                // 204 No Content — DELETE /jobs/{id}.
                if ((int)response.StatusCode == 204)
                    return default(T);

                string json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                if (string.IsNullOrWhiteSpace(json))
                    return default(T);

                return JsonConvert.DeserializeObject<T>(json);
            }
        }

        private async Task<HttpResponseMessage> SendAsync(
            HttpMethod method, string path, object body, bool authenticated,
            TimeSpan timeout, CancellationToken ct, bool retry = true)
        {
            bool renewed = false;

            for (int attempt = 1; ; attempt++)
            {
                using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
                // A fresh request per attempt: an HttpRequestMessage cannot be resent, and its
                // content stream is consumed even by a try that failed at the socket.
                using (var request = new HttpRequestMessage(method, _baseUrl + path))
                {
                    cts.CancelAfter(timeout);

                    if (body != null)
                    {
                        request.Content = new StringContent(
                            JsonConvert.SerializeObject(body), Encoding.UTF8, "application/json");
                    }

                    if (authenticated)
                    {
                        string token = await _auth.GetBearerTokenAsync(cts.Token).ConfigureAwait(false);
                        if (string.IsNullOrEmpty(token))
                        {
                            throw new VoxTellApiException(401, null,
                                "Not signed in. Sign in, or paste an API key in settings.", null, null);
                        }
                        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                    }

                    if (!string.IsNullOrEmpty(HostHeaderOverride))
                        request.Headers.Host = HostHeaderOverride;

                    HttpResponseMessage response;
                    try
                    {
                        response = await _api.SendAsync(request, cts.Token).ConfigureAwait(false);
                    }
                    catch (HttpRequestException) when (retry && attempt < MaxAttempts)
                    {
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                        continue;
                    }
                    catch (OperationCanceledException) when (!ct.IsCancellationRequested && retry && attempt < MaxAttempts)
                    {
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                        continue;
                    }

                    // One silent renew-and-retry on 401: an access token can expire between
                    // the refresh check and the server reading it, and re-prompting a planner
                    // mid-job for a clock-skew blip would be indefensible.
                    if ((int)response.StatusCode == 401 && authenticated && !renewed)
                    {
                        renewed = true;
                        response.Dispose();
                        if (await _auth.TryRenewAfterUnauthorizedAsync(ct).ConfigureAwait(false))
                            continue;

                        throw new VoxTellApiException(401, null,
                            "Your session has expired. Sign in again.", null, null);
                    }

                    if (retry && attempt < MaxAttempts && RetryableStatuses.Contains((int)response.StatusCode))
                    {
                        response.Dispose();
                        await BackoffAsync(attempt, ct).ConfigureAwait(false);
                        continue;
                    }

                    return response;
                }
            }
        }

        /// <summary>
        /// Turns a failure response into a <see cref="VoxTellApiException"/> carrying the
        /// server's stable <c>detail.error</c> code, so the UI can treat a 402 (fatal for the
        /// month) differently from a 429 (wait and resubmit).
        /// </summary>
        private static async Task ThrowIfFailedAsync(HttpResponseMessage response)
        {
            if (response.IsSuccessStatusCode) return;

            string body = await SafeReadAsync(response).ConfigureAwait(false);

            string code = null;
            string message = null;
            int? retryAfter = null;

            try
            {
                JToken detail = JObject.Parse(body)["detail"];
                if (detail != null)
                {
                    if (detail.Type == JTokenType.String)
                    {
                        message = detail.Value<string>();
                    }
                    else if (detail.Type == JTokenType.Object)
                    {
                        code = (string)detail["error"];
                        message = (string)detail["message"];
                        if (detail["retryAfter"] != null)
                            retryAfter = (int?)detail["retryAfter"];

                        // 402 reports used/limit instead of a message.
                        if (string.IsNullOrEmpty(message) && detail["limit"] != null)
                        {
                            message = string.Format(
                                "Monthly job quota exhausted ({0} of {1} used).",
                                detail["used"], detail["limit"]);
                        }
                    }
                    else if (detail.Type == JTokenType.Array)
                    {
                        // FastAPI validation errors: one entry per offending field.
                        var parts = new List<string>();
                        foreach (JToken item in detail)
                        {
                            string msg = (string)item["msg"];
                            if (!string.IsNullOrEmpty(msg)) parts.Add(msg);
                        }
                        message = parts.Count > 0
                            ? string.Join("; ", parts.ToArray())
                            : "The request was rejected as invalid.";
                    }
                }
            }
            catch
            {
                // Not JSON — e.g. an HTML error page from an intermediary. Fall through.
            }

            if (response.Headers.RetryAfter != null && response.Headers.RetryAfter.Delta.HasValue)
                retryAfter = (int)response.Headers.RetryAfter.Delta.Value.TotalSeconds;

            if (string.IsNullOrEmpty(message))
            {
                message = string.Format("HTTP {0} {1}. {2}",
                    (int)response.StatusCode, response.ReasonPhrase, Summarise(body));
            }

            throw new VoxTellApiException((int)response.StatusCode, code, message, retryAfter, body);
        }

        private static async Task<string> SafeReadAsync(HttpResponseMessage response)
        {
            try { return await response.Content.ReadAsStringAsync().ConfigureAwait(false); }
            catch { return ""; }
        }

        private static Task BackoffAsync(int attempt, CancellationToken ct)
        {
            // 1s, 2s, 4s, 8s — a job runs for minutes and is polled throughout, so a reset
            // connection, a recycled load balancer or a pod rollout must not lose the job.
            return Task.Delay(TimeSpan.FromSeconds(Math.Pow(2, attempt - 1)), ct);
        }

        private static string Summarise(string body)
        {
            if (string.IsNullOrEmpty(body)) return "";
            body = body.Trim();
            return body.Length <= 300 ? body : body.Substring(0, 300) + "...";
        }

        public void Dispose()
        {
            _api.Dispose();
            _storage.Dispose();
        }
    }
}
