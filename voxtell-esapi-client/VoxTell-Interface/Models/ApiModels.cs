using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace VoxTell_Interface.Models
{
    // Wire DTOs for the VoxTell-Cloud v2 protocol (voxtell-cloud/PROTOCOL.md).
    // Field names are snake_case to match the server's pydantic schemas verbatim.
    // Unknown response fields are ignored by Newtonsoft's defaults, which is what lets the
    // plugin keep working when the server grows a field.

    // ----------------------------------------------------------------------------------- //
    //  Auth
    // ----------------------------------------------------------------------------------- //

    /// <summary>
    /// <c>GET /v1/auth/config</c> — the only bootstrap call, and unauthenticated. Every realm
    /// URL comes from here so none is compiled into a DLL that ships to hospital workstations.
    /// </summary>
    public class AuthConfigResponse
    {
        [JsonProperty("issuer")] public string Issuer { get; set; }
        [JsonProperty("device_client_id")] public string ClientId { get; set; }
        [JsonProperty("device_authorization_endpoint")] public string DeviceAuthorizationEndpoint { get; set; }
        [JsonProperty("token_endpoint")] public string TokenEndpoint { get; set; }
        [JsonProperty("audience")] public string Audience { get; set; }
        [JsonProperty("authorization_endpoint")] public string AuthorizationEndpoint { get; set; }

        /// <summary>
        /// Applies to BOTH grants. Enforcing PKCE on the Keycloak client enforces it for every
        /// authorization request, so the device flow must send a challenge too — otherwise it
        /// fails with "Missing parameter: code_challenge_method".
        /// </summary>
        [JsonProperty("pkce_method")] public string PkceMethod { get; set; }

        [JsonProperty("scopes")] public string Scopes { get; set; }

        /// <summary>
        /// Loopback ports registered on the client, in preference order. Keycloak matches
        /// redirect URIs exactly (its wildcard is path-only), so an ephemeral port is rejected.
        /// </summary>
        [JsonProperty("redirect_ports")] public int[] RedirectPorts { get; set; }

        [JsonProperty("redirect_path")] public string RedirectPath { get; set; }
    }

    /// <summary>An OAuth2 token endpoint response, and its error shape.</summary>
    public class TokenResponse
    {
        [JsonProperty("access_token")] public string AccessToken { get; set; }
        [JsonProperty("refresh_token")] public string RefreshToken { get; set; }
        [JsonProperty("token_type")] public string TokenType { get; set; }
        [JsonProperty("expires_in")] public int ExpiresIn { get; set; }
        [JsonProperty("scope")] public string Scope { get; set; }

        [JsonProperty("error")] public string Error { get; set; }
        [JsonProperty("error_description")] public string ErrorDescription { get; set; }
    }

    /// <summary>RFC 8628 device authorization response.</summary>
    public class DeviceAuthorizationResponse
    {
        [JsonProperty("device_code")] public string DeviceCode { get; set; }
        [JsonProperty("user_code")] public string UserCode { get; set; }
        [JsonProperty("verification_uri")] public string VerificationUri { get; set; }
        [JsonProperty("verification_uri_complete")] public string VerificationUriComplete { get; set; }
        [JsonProperty("expires_in")] public int ExpiresIn { get; set; }

        /// <summary>Server-chosen poll interval in seconds; Keycloak sends 5.</summary>
        [JsonProperty("interval")] public int Interval { get; set; }
    }

    // ----------------------------------------------------------------------------------- //
    //  System
    // ----------------------------------------------------------------------------------- //

    public class HealthResponse
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("database")] public bool Database { get; set; }
        [JsonProperty("version")] public string Version { get; set; }
    }

    /// <summary><c>GET /v1/me</c> — also the probe that validates either credential kind.</summary>
    public class MeResponse
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("email")] public string Email { get; set; }
        [JsonProperty("username")] public string Username { get; set; }

        /// <summary>Null means unlimited.</summary>
        [JsonProperty("monthly_quota")] public int? MonthlyQuota { get; set; }

        [JsonProperty("used_this_month")] public int UsedThisMonth { get; set; }
        [JsonProperty("outstanding")] public int Outstanding { get; set; }
        [JsonProperty("max_outstanding")] public int MaxOutstanding { get; set; }

        /// <summary>Whatever identifies the caller in the UI, preferring the friendlier field.</summary>
        public string DisplayName
        {
            get
            {
                if (!string.IsNullOrEmpty(Email)) return Email;
                if (!string.IsNullOrEmpty(Username)) return Username;
                return Id;
            }
        }
    }

    // ----------------------------------------------------------------------------------- //
    //  Job creation
    // ----------------------------------------------------------------------------------- //

    /// <summary>
    /// The image grid, as ESAPI reports it. <c>origin</c> and the three direction vectors are
    /// in the DICOM patient frame (LPS mm); the server builds the 4x4 affine from them and
    /// returns contour points through that same affine, so this is what makes the round trip
    /// self-consistent.
    /// </summary>
    public class Geometry
    {
        [JsonProperty("x_size")] public int XSize { get; set; }
        [JsonProperty("y_size")] public int YSize { get; set; }
        [JsonProperty("z_size")] public int ZSize { get; set; }
        [JsonProperty("x_res")] public double XRes { get; set; }
        [JsonProperty("y_res")] public double YRes { get; set; }
        [JsonProperty("z_res")] public double ZRes { get; set; }
        [JsonProperty("origin")] public double[] Origin { get; set; }

        // ESAPI calls these XDirection / YDirection / ZDirection. PROTOCOL.md's inline
        // comments name them RowDirection/ColumnDirection/SliceDirection, which are not ESAPI
        // members — the wire names below are what actually matter.
        [JsonProperty("row_direction")] public double[] RowDirection { get; set; }
        [JsonProperty("col_direction")] public double[] ColDirection { get; set; }
        [JsonProperty("slice_direction")] public double[] SliceDirection { get; set; }

        /// <summary>Stored-value to HU rescale; the server applies it once after decoding.</summary>
        [JsonProperty("scaling_slope")] public double ScalingSlope { get; set; }

        [JsonProperty("scaling_intercept")] public double ScalingIntercept { get; set; }

        public long VoxelCount { get { return (long)XSize * YSize * ZSize; } }

        public static Geometry FromVolumeSource(Services.IVolumeSource src)
        {
            return new Geometry
            {
                XSize = src.XSize,
                YSize = src.YSize,
                ZSize = src.ZSize,
                XRes = src.XRes,
                YRes = src.YRes,
                ZRes = src.ZRes,
                Origin = src.Origin,
                RowDirection = src.RowDirection,
                ColDirection = src.ColumnDirection,
                SliceDirection = src.SliceDirection,
                ScalingSlope = src.ScalingSlope,
                ScalingIntercept = src.ScalingIntercept,
            };
        }
    }

    public class JobCreateRequest
    {
        [JsonProperty("geometry")] public Geometry Geometry { get; set; }
        [JsonProperty("prompts")] public string[] Prompts { get; set; }

        /// <summary>Exact byte length of the gzip stream about to be uploaded.</summary>
        [JsonProperty("upload_bytes")] public long UploadBytes { get; set; }

        [JsonProperty("keep_largest")] public bool KeepLargest { get; set; }
        [JsonProperty("want_mask")] public bool WantMask { get; set; }
    }

    public class UploadPart
    {
        [JsonProperty("part_number")] public int PartNumber { get; set; }
        [JsonProperty("url")] public string Url { get; set; }
    }

    public class JobCreatedResponse
    {
        [JsonProperty("job_id")] public string JobId { get; set; }
        [JsonProperty("state")] public string State { get; set; }
        [JsonProperty("upload")] public List<UploadPart> Upload { get; set; }
        [JsonProperty("part_size")] public int PartSize { get; set; }
        [JsonProperty("expires_in")] public int ExpiresIn { get; set; }
    }

    public class SubmitPart
    {
        [JsonProperty("part_number")] public int PartNumber { get; set; }

        /// <summary>Exactly as the PUT returned it, surrounding quotes included.</summary>
        [JsonProperty("etag")] public string ETag { get; set; }
    }

    public class JobSubmitRequest
    {
        [JsonProperty("parts")] public List<SubmitPart> Parts { get; set; }
    }

    // ----------------------------------------------------------------------------------- //
    //  Job status
    // ----------------------------------------------------------------------------------- //

    /// <summary>
    /// The seven server states. The success state is <c>done</c>, not "succeeded" — v1's poll
    /// loop only recognised "completed"/"failed" and would spin forever on anything else.
    /// </summary>
    public static class JobState
    {
        public const string AwaitingUpload = "awaiting_upload";
        public const string Queued = "queued";
        public const string Running = "running";
        public const string Done = "done";
        public const string Failed = "failed";
        public const string Cancelled = "cancelled";
        public const string Expired = "expired";

        public static bool IsTerminal(string state)
        {
            return state == Done || state == Failed || state == Cancelled || state == Expired;
        }
    }

    public class JobStatusResponse
    {
        [JsonProperty("job_id")] public string JobId { get; set; }
        [JsonProperty("state")] public string State { get; set; }

        /// <summary>
        /// 0..1, and NOT monotonic — "Waiting for the GPU" reports 0.16 and can follow 0.20.
        /// </summary>
        [JsonProperty("progress")] public double Progress { get; set; }

        /// <summary>
        /// Worth surfacing verbatim: it carries "Waiting for the GPU" and the engine's own
        /// notices (batch reduced to fit VRAM, and so on), which is the difference between
        /// "slow" and "stuck" for the operator.
        /// </summary>
        [JsonProperty("message")] public string Message { get; set; }

        [JsonProperty("error")] public string Error { get; set; }

        /// <summary>The server's normalised prompt list — match results against this, not the input.</summary>
        [JsonProperty("prompts")] public List<string> Prompts { get; set; }

        /// <summary>Jobs ahead in the global FIFO queue; null unless queued. 0 means next.</summary>
        [JsonProperty("queue_position")] public int? QueuePosition { get; set; }

        [JsonProperty("poll_after")] public int PollAfter { get; set; }
        [JsonProperty("has_mask")] public bool HasMask { get; set; }
        [JsonProperty("created_at")] public DateTimeOffset? CreatedAt { get; set; }
        [JsonProperty("started_at")] public DateTimeOffset? StartedAt { get; set; }
        [JsonProperty("finished_at")] public DateTimeOffset? FinishedAt { get; set; }

        public bool IsTerminal { get { return JobState.IsTerminal(State); } }
    }

    public class JobListResponse
    {
        [JsonProperty("jobs")] public List<JobStatusResponse> Jobs { get; set; }
    }

    // ----------------------------------------------------------------------------------- //
    //  Results
    // ----------------------------------------------------------------------------------- //

    /// <summary>
    /// One closed boundary on one slice. A slice legitimately appears in several entries — one
    /// per boundary — which is how ring-shaped and multi-lobed structures come back correctly.
    /// Do not collapse these by <c>z_index</c>.
    /// </summary>
    public class ContourSlice
    {
        [JsonProperty("z_index")] public int ZIndex { get; set; }

        /// <summary>Millimetres in the DICOM patient frame, ready for ESAPI.</summary>
        [JsonProperty("points_lps")] public List<double[]> PointsLps { get; set; }
    }

    public class InferenceResult
    {
        [JsonProperty("prompt")] public string Prompt { get; set; }

        /// <summary>Non-zero voxels in the whole 3-D mask, unaffected by the contour filter.</summary>
        [JsonProperty("voxel_count")] public long VoxelCount { get; set; }

        [JsonProperty("contours")] public List<ContourSlice> Contours { get; set; }
    }

    /// <summary>The un-gzipped <c>result.json.gz</c>.</summary>
    public class ResultEnvelope
    {
        [JsonProperty("schema")] public int Schema { get; set; }
        [JsonProperty("job_id")] public string JobId { get; set; }
        [JsonProperty("model")] public string Model { get; set; }
        [JsonProperty("prompts")] public List<string> Prompts { get; set; }
        [JsonProperty("results")] public List<InferenceResult> Results { get; set; }
    }

    // ----------------------------------------------------------------------------------- //
    //  Errors
    // ----------------------------------------------------------------------------------- //

    /// <summary>
    /// Stable machine-readable error codes. The server puts these in <c>detail.error</c> so a
    /// client can branch on a string instead of parsing prose.
    /// </summary>
    public static class ApiErrorCode
    {
        public const string VolumeTooLarge = "volume_too_large";
        public const string UploadTooLarge = "upload_too_large";
        public const string UploadBytesImplausible = "upload_bytes_implausible";
        public const string TooManyParts = "too_many_parts";
        public const string MonthlyQuotaExceeded = "monthly_quota_exceeded";
        public const string TooManyOutstandingJobs = "too_many_outstanding_jobs";
        public const string JobNotAwaitingUpload = "job_not_awaiting_upload";
        public const string NoUploadInProgress = "no_upload_in_progress";
        public const string PartCountMismatch = "part_count_mismatch";
        public const string UploadIncomplete = "upload_incomplete";
        public const string UploadSizeMismatch = "upload_size_mismatch";
        public const string JobNotCancellable = "job_not_cancellable";
        public const string JobRunning = "job_running";
        public const string JobNotDone = "job_not_done";
    }

    /// <summary>
    /// A failed API call, with the pieces the UI needs to react differently: a 402 is fatal for
    /// the month, a 429 just wants us to wait out <c>Retry-After</c>, a 401 wants a fresh token.
    /// </summary>
    public class VoxTellApiException : Exception
    {
        public VoxTellApiException(int statusCode, string errorCode, string message,
                                   int? retryAfterSeconds, string rawBody)
            : base(message)
        {
            StatusCode = statusCode;
            ErrorCode = errorCode;
            RetryAfterSeconds = retryAfterSeconds;
            RawBody = rawBody;
        }

        public int StatusCode { get; private set; }

        /// <summary>The <c>detail.error</c> code, or null when the server sent a bare string.</summary>
        public string ErrorCode { get; private set; }

        public int? RetryAfterSeconds { get; private set; }
        public string RawBody { get; private set; }

        public bool IsUnauthorized { get { return StatusCode == 401; } }
        public bool IsQuotaExceeded { get { return StatusCode == 402; } }
        public bool IsTooManyOutstanding { get { return StatusCode == 429; } }
    }
}
