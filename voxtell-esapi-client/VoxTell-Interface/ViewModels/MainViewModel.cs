using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Windows.Threading;
using VMS.TPS.Common.Model.API;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;
using VoxTell_Interface.Services.Auth;

namespace VoxTell_Interface.ViewModels
{
    /// <summary>
    /// Orchestrates the v2 workflow: sign in, encode, upload, queue, poll, review, import.
    ///
    /// Threading: every public method is called on the ESAPI/UI thread and none of the awaits
    /// use <c>ConfigureAwait(false)</c>, so continuations come back to that thread and property
    /// setters need no marshalling. The two places work genuinely leaves it — the encoder and
    /// the HTTP calls, both inside <c>Task.Run</c> — marshal their progress callbacks through
    /// <see cref="EsapiGate"/>.
    /// </summary>
    public class MainViewModel : INotifyPropertyChanged, IDisposable, IMainCommands
    {
        private readonly ScriptContext _context;
        private readonly EsapiGate _gate;
        private readonly VoxTellApiClient _api;
        private readonly AuthService _auth;
        private readonly EsapiVolumeSource _volume;

        // One CTS per operation. v1 kept a single field that three operations reassigned, so a
        // health check during an upload replaced the upload's token and Cancel() cancelled
        // whichever had started last.
        private CancellationTokenSource _signInCts;
        private CancellationTokenSource _jobCts;

        private string _jobId;
        private List<InferenceResult> _results;

        /// <summary>
        /// The WPF constructor. Called from the view's constructor, which Eclipse reaches
        /// synchronously through <c>Script.Execute</c> — so we are on the ESAPI thread and
        /// <see cref="EsapiGate"/> captures the right one.
        /// </summary>
        public MainViewModel(ScriptContext context)
            : this(context, new EsapiGate(Dispatcher.CurrentDispatcher))
        {
        }

        /// <summary>
        /// The WinForms constructor, kept only until <c>MainForm</c> is deleted so that flipping
        /// the view over is a single, reversible change rather than two entangled ones.
        /// </summary>
        public MainViewModel(ScriptContext context, Control uiControl)
            : this(context, new EsapiGate(uiControl))
        {
        }

        private MainViewModel(ScriptContext context, EsapiGate gate)
        {
            _context = context;
            _gate = gate;

            // Read the geometry now, synchronously, while we are certainly on the ESAPI thread
            // and the context is certainly alive.
            if (context.Image != null)
            {
                _volume = new EsapiVolumeSource(context.Image, _gate);

                ImageInfo = string.Format("{0}x{1}x{2}  ·  {3:F2} x {4:F2} x {5:F2} mm",
                    _volume.XSize, _volume.YSize, _volume.ZSize,
                    _volume.XRes, _volume.YRes, _volume.ZRes);

                RescaleInfo = string.Format("HU = stored x {0:0.####} + {1:0.##}",
                    _volume.ScalingSlope, _volume.ScalingIntercept);

                VoxelVolumeMm3 = _volume.XRes * _volume.YRes * _volume.ZRes;

                StructureSetInfo = context.StructureSet != null
                    ? context.StructureSet.Id
                    : "none open";
            }
            else
            {
                ImageInfo = "No image. Open a plan or structure set with an image first.";
                RescaleInfo = "";
                StructureSetInfo = "none open";
            }

            _auth = new AuthService(() => _api == null ? null : _api.CachedAuthConfig);

            string baseUrl = _auth.SavedBaseUrl;
            _api = new VoxTellApiClient(
                string.IsNullOrEmpty(baseUrl) ? VoxTellApiClient.DefaultBaseUrl : baseUrl, _auth);

            BaseUrl = _api.BaseUrl;
            Phase = _auth.HasCredential ? WorkflowPhase.Ready : WorkflowPhase.SignInRequired;
            AccountName = _auth.CachedDisplayName;
        }

        // ------------------------------------------------------------------------------- //
        //  Bound state
        // ------------------------------------------------------------------------------- //

        private WorkflowPhase _phase;
        public WorkflowPhase Phase
        {
            get { return _phase; }
            private set { _phase = value; OnPropertyChanged(); OnPropertyChanged("CanRun"); OnPropertyChanged("CanCancel"); }
        }

        private bool _busy;
        public bool IsBusy
        {
            get { return _busy; }
            private set { _busy = value; OnPropertyChanged(); OnPropertyChanged("CanRun"); OnPropertyChanged("CanCancel"); }
        }

        public string ImageInfo { get; private set; }
        public string RescaleInfo { get; private set; }
        public string StructureSetInfo { get; private set; }

        /// <summary>
        /// Volume of one voxel in mm³, so the review list can report cm³ rather than a raw voxel
        /// count. Fixed for the life of the window, like the three above.
        /// </summary>
        public double VoxelVolumeMm3 { get; private set; }

        private string _baseUrl;
        public string BaseUrl
        {
            get { return _baseUrl; }
            set { _baseUrl = value; OnPropertyChanged(); }
        }

        private string _accountName;
        public string AccountName
        {
            get { return _accountName; }
            private set { _accountName = value; OnPropertyChanged(); }
        }

        private string _quotaInfo;
        public string QuotaInfo
        {
            get { return _quotaInfo; }
            private set { _quotaInfo = value; OnPropertyChanged(); }
        }

        private string _promptsText = "";
        public string PromptsText
        {
            get { return _promptsText; }
            set { _promptsText = value; OnPropertyChanged(); OnPropertyChanged("CanRun"); }
        }

        private string _status = "";
        public string Status
        {
            get { return _status; }
            // Assigning Status also classifies it, so the ~25 existing `Status = ...` sites need
            // no edit. The view used to do this classification itself by substring-matching the
            // message, which meant rewording a message could silently change its colour from
            // across the codebase. SetStatus overrides the guess where a site knows better.
            private set { _status = value; Severity = Classify(value); OnPropertyChanged(); }
        }

        /// <summary>How <see cref="Status"/> should read. Derived, never assigned directly.</summary>
        private StatusSeverity _severity = StatusSeverity.Neutral;
        public StatusSeverity Severity
        {
            get { return _severity; }
            private set { _severity = value; OnPropertyChanged(); }
        }

        /// <summary>Sets the status text and states its severity outright.</summary>
        private void SetStatus(string text, StatusSeverity severity)
        {
            Status = text;          // classifies, then...
            Severity = severity;    // ...is overridden by the caller's better knowledge.
        }

        /// <summary>
        /// The heuristic moved verbatim from the WinForms view, so behaviour is unchanged at the
        /// flip. It is a default, not a contract — see <see cref="SetStatus"/>.
        /// </summary>
        private static StatusSeverity Classify(string status)
        {
            if (string.IsNullOrEmpty(status)) return StatusSeverity.Neutral;

            if (status.StartsWith("Imported", StringComparison.OrdinalIgnoreCase))
                return StatusSeverity.Success;

            if (status.IndexOf("failed", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("could not", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("expired", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("error", StringComparison.OrdinalIgnoreCase) >= 0)
                return StatusSeverity.Error;

            if (status.StartsWith("Warning", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("Cancel", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("Nothing", StringComparison.OrdinalIgnoreCase))
                return StatusSeverity.Warning;

            // A trailing ellipsis is this codebase's own convention for "in flight" — every
            // progress message uses it and no terminal one does.
            if (status.EndsWith("...", StringComparison.Ordinal))
                return StatusSeverity.Working;

            return StatusSeverity.Neutral;
        }

        /// <summary>The server's own message, shown verbatim — it distinguishes slow from stuck.</summary>
        private string _serverMessage = "";
        public string ServerMessage
        {
            get { return _serverMessage; }
            private set { _serverMessage = value; OnPropertyChanged(); }
        }

        private double _progress;
        public double Progress
        {
            get { return _progress; }
            private set { _progress = value; OnPropertyChanged(); }
        }

        /// <summary>
        /// Jobs ahead of this one while queued, or null when the server is not reporting a
        /// position. Already available in the poll loop; surfaced so the view can show it as its
        /// own element rather than only inside a sentence.
        /// </summary>
        private int? _queuePosition;
        public int? QueuePosition
        {
            get { return _queuePosition; }
            private set { _queuePosition = value; OnPropertyChanged(); }
        }

        /// <summary>Device-flow instructions, when that fallback is in play.</summary>
        private SignInPrompt _signInPrompt;
        public SignInPrompt CurrentSignInPrompt
        {
            get { return _signInPrompt; }
            private set { _signInPrompt = value; OnPropertyChanged(); }
        }

        /// <summary>The review list. Nothing reaches the patient until rows here are ticked.</summary>
        public List<StructurePlan> Plans { get; private set; }

        public bool IsSignedIn { get { return _auth.HasCredential; } }

        public bool CanRun
        {
            get
            {
                return !IsBusy
                       && _volume != null
                       && _auth.HasCredential
                       && ParsePrompts().Count > 0;
            }
        }

        public bool CanCancel { get { return IsBusy && _jobCts != null; } }

        /// <summary>
        /// One consistent picture of everything the view draws.
        ///
        /// Cheap enough to call on every <c>PropertyChanged</c> — it is field copies and a
        /// four-element list — and calling it that way is the point: the view rebinds to a whole
        /// new state object instead of hand-updating individual labels, which is what let the old
        /// view drift out of sync with the phase it was supposedly showing.
        ///
        /// <see cref="MainViewState.Rows"/> hands over the live <see cref="Plans"/> list by
        /// reference, deliberately. The review rows bind two-way onto those objects, so the
        /// operator's edits are already present in the very list <see cref="ImportSelected"/>
        /// reads — no scraping the view, and no pairing rows to plans by index.
        /// </summary>
        public MainViewState Snapshot()
        {
            SignInPrompt prompt = CurrentSignInPrompt;

            return new MainViewState
            {
                Phase = Phase,
                IsBusy = IsBusy,
                CanRun = CanRun,
                CanCancel = CanCancel,
                IsSignedIn = IsSignedIn,

                AccountName = AccountName,
                QuotaInfo = QuotaInfo,
                BaseUrl = BaseUrl,

                ImageInfo = ImageInfo,
                RescaleInfo = RescaleInfo,
                StructureSetInfo = StructureSetInfo,

                Status = Status,
                Severity = Severity,
                ServerMessage = ServerMessage,
                Progress = Progress,
                QueuePosition = QueuePosition,

                SignInMessage = prompt == null ? null : prompt.Message,
                SignInVerificationUri = prompt == null ? null : prompt.VerificationUri,
                SignInUserCode = prompt == null ? null : prompt.UserCode,

                Rows = Plans,
                ImportSummary = ImportSummary,
                ImportWarnings = ImportWarnings,

                Steps = MainViewState.BuildSteps(
                    Phase, IsSignedIn, Plans != null && Plans.Count > 0),
            };
        }

        // ------------------------------------------------------------------------------- //
        //  Sign in
        // ------------------------------------------------------------------------------- //

        public async Task SignInAsync()
        {
            if (IsBusy) return;

            IsBusy = true;
            CurrentSignInPrompt = null;
            Status = "Connecting...";

            _signInCts = new CancellationTokenSource(TimeSpan.FromMinutes(10));
            try
            {
                // Config first: the auth service reads its endpoints from the cached copy, and
                // fetching it here keeps the token-refresh path from re-entering this client.
                await _api.GetAuthConfigAsync(_signInCts.Token);

                Status = "Waiting for sign-in...";
                await _auth.SignInAsync(
                    prompt => _gate.Run(() =>
                    {
                        CurrentSignInPrompt = prompt;
                        if (!string.IsNullOrEmpty(prompt.Message)) Status = prompt.Message;
                    }),
                    _signInCts.Token);

                CurrentSignInPrompt = null;
                await RefreshAccountAsync(_signInCts.Token);

                Phase = WorkflowPhase.Ready;
                OnPropertyChanged("IsSignedIn");
            }
            catch (OperationCanceledException)
            {
                Status = "Sign-in cancelled.";
            }
            catch (Exception ex)
            {
                Status = Describe(ex);
            }
            finally
            {
                CurrentSignInPrompt = null;
                DisposeCts(ref _signInCts);
                IsBusy = false;
            }
        }

        public void SignOut()
        {
            _auth.SignOut();
            AccountName = null;
            QuotaInfo = null;
            Phase = WorkflowPhase.SignInRequired;
            Status = "Signed out.";
            OnPropertyChanged("IsSignedIn");
        }

        /// <summary>Applies an API key for unattended workstations, then validates it.</summary>
        public async Task ApplyApiKeyAsync(string apiKey)
        {
            _auth.SetApiKey(apiKey);
            OnPropertyChanged("IsSignedIn");

            if (string.IsNullOrWhiteSpace(apiKey))
            {
                Phase = WorkflowPhase.SignInRequired;
                return;
            }

            using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60)))
            {
                try
                {
                    await RefreshAccountAsync(cts.Token);
                    Phase = WorkflowPhase.Ready;
                }
                catch (Exception ex)
                {
                    Status = Describe(ex);
                }
            }
        }

        public void ApplyBaseUrl(string baseUrl)
        {
            _api.SetBaseUrl(baseUrl);
            BaseUrl = _api.BaseUrl;
            _auth.SaveBaseUrl(_api.BaseUrl);
            Status = "Server set to " + _api.BaseUrl;
        }

        /// <summary><c>GET /v1/me</c> — validates the credential and reports quota.</summary>
        private async Task RefreshAccountAsync(CancellationToken ct)
        {
            MeResponse me = await _api.GetMeAsync(ct);

            AccountName = me.DisplayName;
            QuotaInfo = me.MonthlyQuota.HasValue
                ? string.Format("{0} of {1} jobs used this month  ·  {2}/{3} in flight",
                    me.UsedThisMonth, me.MonthlyQuota.Value, me.Outstanding, me.MaxOutstanding)
                : string.Format("{0} jobs this month  ·  {1}/{2} in flight",
                    me.UsedThisMonth, me.Outstanding, me.MaxOutstanding);
        }

        // ------------------------------------------------------------------------------- //
        //  The job
        // ------------------------------------------------------------------------------- //

        public async Task RunAsync()
        {
            if (!CanRun) return;

            List<string> prompts = ParsePrompts();
            string limitProblem = CheckPromptLimits(prompts);
            if (limitProblem != null)
            {
                Status = limitProblem;
                return;
            }

            IsBusy = true;
            Plans = null;
            _results = null;
            _jobId = null;
            QueuePosition = null;
            OnPropertyChanged("Plans");

            _jobCts = new CancellationTokenSource();
            CancellationToken ct = _jobCts.Token;

            try
            {
                await _api.GetAuthConfigAsync(ct);

                Phase = WorkflowPhase.Uploading;
                Progress = 0;
                Status = "Reading the image...";

                byte[] blob = await Task.Run(() => VoxelEncoder.BuildVolumeBlob(
                    _volume, _gate,
                    (done, total) => _gate.Run(() =>
                    {
                        Progress = 0.4 * done / total;
                        Status = string.Format("Reading slice {0} of {1}...", done, total);
                    }),
                    ct), ct);

                VoxelEncoder.ValidateBlob(blob, _volume);

                if (_volume.ClampedVoxelCount > 0)
                {
                    Status = string.Format(
                        "Warning: {0:N0} voxels fell outside the 16-bit range and were clamped.",
                        _volume.ClampedVoxelCount);
                }

                var request = new JobCreateRequest
                {
                    Geometry = Geometry.FromVolumeSource(_volume),
                    Prompts = prompts.ToArray(),
                    UploadBytes = blob.LongLength,
                    KeepLargest = false,
                    WantMask = false,
                };

                Status = string.Format("Submitting {0:N1} MB...", blob.LongLength / 1e6);
                JobCreatedResponse created = await _api.CreateJobAsync(request, ct);
                _jobId = created.JobId;

                List<SubmitPart> parts = await _api.UploadPartsAsync(
                    blob, created,
                    (part, total, sent, all) => _gate.Run(() =>
                    {
                        Progress = 0.4 + 0.4 * sent / all;
                        Status = string.Format("Uploading part {0} of {1}...", part, total);
                    }),
                    ct);

                // Release the blob before the job runs: holding 150 MB through a multi-minute
                // segmentation costs the Eclipse process for no reason.
                blob = null;

                Status = "Queueing...";
                JobStatusResponse status = await _api.SubmitJobAsync(_jobId, parts, ct);

                Phase = WorkflowPhase.Working;
                status = await PollUntilTerminalAsync(status, ct);

                if (status.State != JobState.Done)
                {
                    Status = DescribeTerminal(status);
                    Phase = WorkflowPhase.Ready;
                    return;
                }

                Progress = 0.95;
                Status = "Downloading contours...";
                ResultEnvelope envelope = await _api.GetResultAsync(_jobId, ct);

                _results = envelope.Results ?? new List<InferenceResult>();

                // Building the plan reads the structure set, so it has to be on the ESAPI
                // thread. Re-read the structure set rather than trusting the handle captured at
                // construction: minutes have passed and the operator may have changed it.
                Plans = _gate.Run(() => BuildPlan(_results));
                OnPropertyChanged("Plans");

                Progress = 1;
                Phase = WorkflowPhase.Reviewing;

                int found = _results.Count(r => r.Contours != null && r.Contours.Count > 0);
                Status = found == 0
                    ? "Finished, but nothing was segmented for any prompt."
                    : string.Format("Review {0} structure(s), then import.", found);
            }
            catch (OperationCanceledException)
            {
                Status = "Cancelled.";
                Phase = WorkflowPhase.Ready;
            }
            catch (Exception ex)
            {
                Status = Describe(ex);
                Phase = WorkflowPhase.Ready;
            }
            finally
            {
                DisposeCts(ref _jobCts);
                IsBusy = false;
            }
        }

        private async Task<JobStatusResponse> PollUntilTerminalAsync(
            JobStatusResponse status, CancellationToken ct)
        {
            while (!status.IsTerminal)
            {
                int wait = status.PollAfter > 0 ? status.PollAfter : 5;
                await Task.Delay(TimeSpan.FromSeconds(wait), ct);

                status = await _api.GetJobAsync(_jobId, ct);

                // The server's progress is not monotonic — "Waiting for the GPU" reports 0.16
                // and can follow 0.20 — so this maps it into the tail of the bar as-is rather
                // than trying to enforce an increase.
                Progress = 0.8 + 0.15 * Math.Max(0, Math.Min(1, status.Progress));
                ServerMessage = status.Message ?? "";

                if (status.State == JobState.Queued && status.QueuePosition.HasValue)
                {
                    int ahead = status.QueuePosition.Value;
                    QueuePosition = ahead;
                    SetStatus(ahead == 0
                        ? "Queued — next in line."
                        : string.Format("Queued — {0} job(s) ahead.", ahead),
                        StatusSeverity.Working);
                }
                else
                {
                    QueuePosition = null;
                    Status = string.Format("{0}... {1:P0}", Capitalise(status.State), status.Progress);
                }
            }

            return status;
        }

        public void Cancel()
        {
            if (_jobCts != null)
            {
                Status = "Cancelling...";

                // Tell the server too, not just the local token: an abandoned job would keep
                // holding one of the six outstanding slots until the sweeper reaped it.
                string jobId = _jobId;
                if (!string.IsNullOrEmpty(jobId))
                {
                    Task.Run(async () =>
                    {
                        using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30)))
                        {
                            try { await _api.CancelJobAsync(jobId, cts.Token); }
                            catch { /* Best effort; the local cancel below is what the user sees. */ }
                        }
                    });
                }

                _jobCts.Cancel();
            }

            if (_signInCts != null)
                _signInCts.Cancel();
        }

        // ------------------------------------------------------------------------------- //
        //  Import
        // ------------------------------------------------------------------------------- //

        private List<StructurePlan> BuildPlan(List<InferenceResult> results)
        {
            _gate.AssertOnEsapiThread("Building the import plan");
            var importer = new EsapiStructureImporter(
                _context.StructureSet, _volume.ZSize, VoxelVolumeMm3, _gate);
            return importer.BuildPlan(results);
        }

        /// <summary>
        /// Writes the ticked structures. Synchronous and on the ESAPI thread by necessity —
        /// this is the only code that modifies the patient.
        /// </summary>
        public void ImportSelected()
        {
            if (Plans == null || _results == null) return;

            int selected = Plans.Count(p => p.Selected);
            if (selected == 0)
            {
                Status = "Nothing ticked, so nothing was imported.";
                return;
            }

            try
            {
                var importer = new EsapiStructureImporter(
                    _context.StructureSet, _volume.ZSize, VoxelVolumeMm3, _gate);

                if (!importer.HasStructureSet)
                {
                    Status = "No structure set is open, so nothing was imported.";
                    return;
                }

                List<string> warnings;
                List<string> imported = importer.Import(Plans, _results, out warnings);

                Phase = WorkflowPhase.Imported;
                ImportSummary = imported;
                ImportWarnings = warnings;
                OnPropertyChanged("ImportSummary");
                OnPropertyChanged("ImportWarnings");

                Status = string.Format(
                    "Imported {0} structure(s). Save in Eclipse to keep them.", imported.Count);
            }
            catch (Exception ex)
            {
                // A stale ScriptContext is the realistic cause: Execute returned long ago and
                // the window has been open across a multi-minute job.
                Status = "Could not write to the structure set: " + ex.Message +
                         " Close and re-run the script, then import again.";
            }
        }

        public List<string> ImportSummary { get; private set; }
        public List<string> ImportWarnings { get; private set; }

        // ------------------------------------------------------------------------------- //
        //  Helpers
        // ------------------------------------------------------------------------------- //

        /// <summary>One prompt per line, commas also accepted.</summary>
        public List<string> ParsePrompts()
        {
            if (string.IsNullOrWhiteSpace(PromptsText)) return new List<string>();

            return PromptsText
                .Split(new[] { '\n', '\r', ',' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(p => p.Trim())
                .Where(p => p.Length > 0)
                .ToList();
        }

        /// <summary>
        /// Mirrors the server's limits so an over-long prompt list fails here rather than as a
        /// 422 after the volume has been read.
        /// </summary>
        private static string CheckPromptLimits(List<string> prompts)
        {
            const int maxPrompts = 16;
            const int maxChars = 200;

            if (prompts.Count > maxPrompts)
                return string.Format("At most {0} prompts per job; you have {1}.", maxPrompts, prompts.Count);

            string tooLong = prompts.FirstOrDefault(p => p.Length > maxChars);
            if (tooLong != null)
            {
                return string.Format("A prompt is {0} characters; the limit is {1}.",
                    tooLong.Length, maxChars);
            }

            return null;
        }

        private static string DescribeTerminal(JobStatusResponse status)
        {
            switch (status.State)
            {
                case JobState.Cancelled:
                    return "Cancelled.";
                case JobState.Expired:
                    return "The result expired before it was downloaded.";
                case JobState.Failed:
                    return "Segmentation failed: " +
                           (string.IsNullOrEmpty(status.Error) ? "no reason given." : status.Error);
                default:
                    return "Finished with state " + status.State + ".";
            }
        }

        /// <summary>Turns an exception into something a planner can act on.</summary>
        private static string Describe(Exception ex)
        {
            var api = ex as VoxTellApiException;
            if (api != null)
            {
                if (api.IsQuotaExceeded)
                    return api.Message + " The quota resets at the start of next month.";

                if (api.IsTooManyOutstanding)
                {
                    return string.Format("{0} Try again in {1} seconds.",
                        api.Message, api.RetryAfterSeconds ?? 30);
                }

                if (api.ErrorCode == ApiErrorCode.VolumeTooLarge ||
                    api.ErrorCode == ApiErrorCode.UploadTooLarge)
                {
                    return api.Message + " This image is too large for the service.";
                }

                return api.Message;
            }

            if (ex is AuthenticationFailedException)
                return ex.Message;

            string network = DescribeNetworkFailure(ex);
            if (network != null)
                return network;

            return ex.Message;
        }

        /// <summary>
        /// Unwraps a transport failure into something a planner can act on, or null if this is
        /// not one.
        ///
        /// HttpClient reports every transport problem as the same sentence — "An error occurred
        /// while sending the request." — and hides the cause one or two levels down in
        /// InnerException. A DNS failure and an expired certificate are indistinguishable at the
        /// top level, so the raw message tells a planner nothing and tells us nothing when it is
        /// read back to us over the phone.
        /// </summary>
        private static string DescribeNetworkFailure(Exception ex)
        {
            var web = FindInner<WebException>(ex);
            if (web != null)
            {
                switch (web.Status)
                {
                    case WebExceptionStatus.NameResolutionFailure:
                        // Seen for real on a workstation whose resolver had cached the NXDOMAIN
                        // from before the DNS record existed; Windows keeps negative answers for
                        // 15 minutes, so this outlives the fix that caused it.
                        return "Cannot look up the server's address. The network cannot resolve " +
                               "it, or a stale negative DNS entry is cached — run " +
                               "'ipconfig /flushdns' and try again.";

                    case WebExceptionStatus.TrustFailure:
                    case WebExceptionStatus.SecureChannelFailure:
                        return "The secure connection to the server failed. A proxy may be " +
                               "inspecting TLS, or TLS 1.2 is disabled on this machine.";

                    case WebExceptionStatus.ProxyNameResolutionFailure:
                        return "The configured web proxy cannot be reached.";

                    case WebExceptionStatus.ConnectFailure:
                    case WebExceptionStatus.Timeout:
                        return "Cannot reach the server. Check the network connection, or the " +
                               "server address in settings.";
                }

                return "Network error: " + web.Message;
            }

            if (ex is HttpRequestException)
            {
                // Not a WebException underneath, but still a transport failure: report the
                // innermost message, which is the only one that says anything specific.
                Exception inner = ex;
                while (inner.InnerException != null) inner = inner.InnerException;
                return inner == ex
                    ? "Cannot reach the server. " + ex.Message
                    : "Cannot reach the server: " + inner.Message;
            }

            return null;
        }

        private static T FindInner<T>(Exception ex) where T : Exception
        {
            for (Exception e = ex; e != null; e = e.InnerException)
            {
                var hit = e as T;
                if (hit != null) return hit;
            }
            return null;
        }

        private static string Capitalise(string s)
        {
            if (string.IsNullOrEmpty(s)) return s;
            return char.ToUpperInvariant(s[0]) + s.Substring(1).Replace('_', ' ');
        }

        private static void DisposeCts(ref CancellationTokenSource cts)
        {
            if (cts == null) return;
            try { cts.Dispose(); } catch { }
            cts = null;
        }

        public event PropertyChangedEventHandler PropertyChanged;

        private void OnPropertyChanged([CallerMemberName] string name = null)
        {
            PropertyChangedEventHandler handler = PropertyChanged;
            if (handler != null)
                handler(this, new PropertyChangedEventArgs(name));
        }

        public void Dispose()
        {
            // Cancel, but never block the UI thread waiting on network teardown: v1 did a
            // .Wait(3000) here, which can deadlock while the same thread is being asked to
            // service an Invoke from the work it is waiting on.
            if (_jobCts != null) { try { _jobCts.Cancel(); } catch { } }
            if (_signInCts != null) { try { _signInCts.Cancel(); } catch { } }

            DisposeCts(ref _jobCts);
            DisposeCts(ref _signInCts);

            _api.Dispose();
            _auth.Dispose();
        }
    }
}
