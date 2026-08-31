using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
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

        // The catalog and the QA lineage material, both fetched from the server.
        private ModelCatalog _catalog;
        private string _lineageSecret;

        // Raw DICOM identifiers, read once in the constructor while the ESAPI
        // context is certainly alive. They stay in this process and are only ever
        // used as input to LineageKeys: nothing here is ever placed on a request,
        // and grep for these field names should only ever find LineageKeys calls.
        private readonly string _seriesUid;
        private readonly string _frameOfReferenceUid;
        private readonly string _deviceManufacturer;
        private readonly string _deviceModel;
        private readonly string _deviceSerial;

        /// <summary>
        /// The WPF constructor. Called from the view's constructor, which Eclipse reaches
        /// synchronously through <c>Script.Execute</c> — so we are on the ESAPI thread and
        /// <see cref="EsapiGate"/> captures the right one.
        /// </summary>
        public MainViewModel(ScriptContext context)
            : this(context, new EsapiGate(Dispatcher.CurrentDispatcher))
        {
        }

        private MainViewModel(ScriptContext context, EsapiGate gate)
        {
            _context = context;
            _gate = gate;

            // Read the geometry now, synchronously, while we are certainly on the ESAPI thread
            // and the context is certainly alive.
            //
            // `context == null` is the preview path (see CreatePreview): the layout harness
            // renders this exact view model and this exact panel with no Eclipse present, so
            // every ESAPI touch below is guarded rather than assumed.
            if (context != null && context.Image != null)
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

                // Series identity and the imaging device, for QA lineage and for
                // noticing a scanner or protocol change. Wrapped because a series
                // detached from its study throws rather than returning null, and a
                // missing UID must degrade to "no QA lineage", never to a crash on
                // a panel the planner only wanted to segment with.
                _seriesUid = TryRead(() => context.Image.Series != null
                    ? context.Image.Series.UID : null);
                _frameOfReferenceUid = TryRead(() => context.Image.FOR);
                _deviceManufacturer = TryRead(() => context.Image.Series != null
                    ? context.Image.Series.ImagingDeviceManufacturer : null);
                _deviceModel = TryRead(() => context.Image.Series != null
                    ? context.Image.Series.ImagingDeviceModel : null);
                _deviceSerial = TryRead(() => context.Image.Series != null
                    ? context.Image.Series.ImagingDeviceSerialNo : null);
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
        //  Preview seam
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// A view model with no Eclipse behind it, for the layout harness.
        ///
        /// Exists so the panel's layout can be rendered and looked at without a
        /// patient, a licence or a running server. That is not a convenience: the
        /// first real run of this UI surfaced a header whose cards overlapped and a
        /// cross-thread crash, neither of which any compile or unit test can catch,
        /// and iterating on them through "rebuild, redeploy, re-approve, reopen
        /// Eclipse" is a minutes-long loop for a spacing change.
        ///
        /// <c>internal</c> and reached only by the Preview project, which links these
        /// sources rather than referencing the shipped DLL — so nothing preview-only
        /// widens the plugin's public surface.
        /// </summary>
        internal static MainViewModel CreatePreview()
        {
            return new MainViewModel(null, new EsapiGate(Dispatcher.CurrentDispatcher));
        }

        /// <summary>Fill the view model with plausible content for a render.</summary>
        internal void SeedPreview(
            string accountName,
            string quota,
            string imageInfo,
            string rescaleInfo,
            string structureSetInfo,
            ModelCatalog catalog,
            StructureAutoDetect.Detection detection,
            List<StructurePlan> plans,
            WorkflowPhase phase,
            string status)
        {
            AccountName = accountName;
            // An account name is the harness's way of saying "signed in"; the alternative
            // is every state having to remember to say so.
            _previewSignedIn = !string.IsNullOrEmpty(accountName);
            QuotaInfo = quota;
            ImageInfo = imageInfo;
            RescaleInfo = rescaleInfo;
            StructureSetInfo = structureSetInfo;
            Catalog = catalog;
            Detection = detection;
            Plans = plans;
            Phase = phase;
            Status = status;
            if (detection != null) SelectStructures(detection.StructureIds);
            OnPropertyChanged("Plans");
        }

        /// <summary>True when there is no Eclipse behind this view model.</summary>
        internal bool IsPreview { get { return _context == null; } }

        // ------------------------------------------------------------------------------- //
        //  Bound state
        // ------------------------------------------------------------------------------- //

        /// <summary>The catalog, once fetched. Null until then; the view copes.</summary>
        public ModelCatalog Catalog
        {
            get { return _catalog; }
            private set
            {
                _catalog = value;
                OnPropertyChanged();
                OnPropertyChanged("HasCatalog");
                OnPropertyChanged("CanRun");
            }
        }

        public bool HasCatalog
        {
            get { return _catalog != null && _catalog.Models != null && _catalog.Models.Count > 0; }
        }

        private TargetMode _mode = TargetMode.Prompts;

        /// <summary>
        /// Prompts or catalog structures. Defaults to Prompts so the panel opens
        /// behaving exactly as the version planners already know; auto-detect
        /// flips it to Structures when it actually finds something to compare.
        /// </summary>
        public TargetMode Mode
        {
            get { return _mode; }
            set
            {
                if (_mode == value) return;
                _mode = value;
                OnPropertyChanged();
                OnPropertyChanged("CanRun");
                OnPropertyChanged("TargetSummary");
            }
        }

        private string _promptModelKey;

        /// <summary>Which prompt model to run. Null means the server's default.</summary>
        public string PromptModelKey
        {
            get { return _promptModelKey; }
            set { _promptModelKey = value; OnPropertyChanged(); }
        }

        private readonly HashSet<string> _selectedStructureIds =
            new HashSet<string>(StringComparer.Ordinal);

        /// <summary>Catalog structure ids ticked for this job, in catalog order.</summary>
        public IList<string> SelectedStructureIds
        {
            get
            {
                if (_catalog == null || _catalog.Structures == null)
                {
                    return _selectedStructureIds.ToList();
                }
                return _catalog.Structures
                    .Where(st => _selectedStructureIds.Contains(st.Id))
                    .Select(st => st.Id)
                    .ToList();
            }
        }

        public bool IsStructureSelected(string structureId)
        {
            return structureId != null && _selectedStructureIds.Contains(structureId);
        }

        public void SetStructureSelected(string structureId, bool selected)
        {
            if (string.IsNullOrEmpty(structureId)) return;

            bool changed = selected
                ? _selectedStructureIds.Add(structureId)
                : _selectedStructureIds.Remove(structureId);
            if (!changed) return;

            OnPropertyChanged("SelectedStructureIds");
            OnPropertyChanged("SelectedStructureCount");
            OnPropertyChanged("TargetSummary");
            OnPropertyChanged("CanRun");
        }

        public int SelectedStructureCount { get { return _selectedStructureIds.Count; } }

        /// <summary>Replace the selection wholesale — a preset, or auto-detect.</summary>
        public void SelectStructures(IEnumerable<string> structureIds)
        {
            _selectedStructureIds.Clear();
            foreach (string id in structureIds ?? Enumerable.Empty<string>())
            {
                if (!string.IsNullOrEmpty(id)) _selectedStructureIds.Add(id);
            }
            OnPropertyChanged("SelectedStructureIds");
            OnPropertyChanged("SelectedStructureCount");
            OnPropertyChanged("TargetSummary");
            OnPropertyChanged("CanRun");
        }

        private string _protocolKey;

        /// <summary>
        /// The clinic protocol in force, or null.
        ///
        /// A protocol is the same structure ids as any other selection plus the naming the
        /// clinic uses, so it changes nothing on the wire — see <see cref="TargetMode"/>.
        /// It is served, not compiled in: ESAPI 16.1 exposes no structure-template or
        /// clinical-protocol enumeration, and anything baked into this DLL costs a
        /// re-approval on every workstation to change.
        /// </summary>
        public string ProtocolKey
        {
            get { return _protocolKey; }
        }

        public CatalogProtocol CurrentProtocol
        {
            get { return _catalog == null ? null : _catalog.Protocol(_protocolKey); }
        }

        /// <summary>
        /// Apply a protocol: select everything it names that this deployment can produce.
        ///
        /// Entries no model produces are deliberately NOT selected and NOT hidden — the
        /// pane lists them, because a protocol that silently drops a structure produces a
        /// run that looks complete and is not.
        /// </summary>
        public void ApplyProtocol(string key)
        {
            _protocolKey = key;

            CatalogProtocol protocol = CurrentProtocol;
            if (protocol == null)
            {
                SelectStructures(new string[0]);
            }
            else
            {
                IList<ProtocolEntry> available;
                IList<ProtocolEntry> unavailable;
                _catalog.SplitEntries(protocol, out available, out unavailable);
                SelectStructures(available.Select(e => e.StructureId));
                Mode = TargetMode.Protocol;
            }

            OnPropertyChanged("ProtocolKey");
            OnPropertyChanged("CurrentProtocol");
            OnPropertyChanged("TargetSummary");
            OnPropertyChanged("CanRun");
        }

        /// <summary>The protocol's entry for a catalog structure, or null.</summary>
        public ProtocolEntry ProtocolEntryFor(string structureId)
        {
            CatalogProtocol protocol = CurrentProtocol;
            if (protocol == null || protocol.Entries == null || structureId == null) return null;

            return protocol.Entries.FirstOrDefault(
                e => e != null && string.Equals(e.StructureId, structureId, StringComparison.Ordinal));
        }

        // Every structure Id currently on the set, for resolving what a row will write into.
        // Cached rather than read per keystroke: the alternative is an ESAPI call on the UI
        // thread for every character typed into a write-as box.
        private readonly List<string> _existingIds = new List<string>();

        /// <summary>
        /// Re-resolve which existing structure a row targets, after its id was edited.
        ///
        /// <see cref="StructurePlan.ExistingId"/> was previously fixed when the plan was
        /// built while the write path resolved the edited id, so renaming a row onto an
        /// existing structure showed "will create" and then replaced that structure's
        /// contours on the affected slices. The row now tells the truth as it is typed.
        /// </summary>
        public void RetargetPlan(StructurePlan plan)
        {
            if (plan == null) return;

            string id = (plan.StructureId ?? string.Empty).Trim();
            string match = null;
            if (id.Length > 0)
            {
                foreach (string existing in _existingIds)
                {
                    if (string.Equals(existing, id, StringComparison.OrdinalIgnoreCase))
                    {
                        match = existing;
                        break;
                    }
                }
            }
            plan.ExistingId = match;
        }

        /// <summary>Seed the existing-id cache in the preview, where there is no ESAPI.</summary>
        internal void SeedExistingIds(IEnumerable<string> ids)
        {
            _existingIds.Clear();
            foreach (string id in ids ?? Enumerable.Empty<string>())
            {
                if (!string.IsNullOrEmpty(id)) _existingIds.Add(id);
            }
        }

        private StructureAutoDetect.Detection _detection;

        /// <summary>What is already contoured on this series. Null until scanned.</summary>
        public StructureAutoDetect.Detection Detection
        {
            get { return _detection; }
            private set
            {
                _detection = value;
                OnPropertyChanged();
                OnPropertyChanged("AutoDetectSummary");
            }
        }

        /// <summary>
        /// One factual line about the existing structure set. Always names the
        /// unrecognised count, because that is the number the planner can act on —
        /// and because silently skipping off-convention names is the commonest
        /// failure in the published audits of exactly this workflow.
        /// </summary>
        public string AutoDetectSummary
        {
            get { return _detection == null ? null : _detection.Summary; }
        }

        /// <summary>What this job will ask for, for the run button's caption area.</summary>
        public string TargetSummary
        {
            get
            {
                if (Mode == TargetMode.Prompts)
                {
                    int count = ParsePrompts().Count;
                    return count == 0
                        ? "No prompts yet."
                        : count + (count == 1 ? " prompt" : " prompts") + ".";
                }
                int n = _selectedStructureIds.Count;

                CatalogProtocol protocol = CurrentProtocol;
                string prefix = Mode == TargetMode.Protocol && protocol != null
                    ? protocol.DisplayName + ": "
                    : string.Empty;

                if (n == 0) return prefix.Length > 0 ? prefix + "nothing selected." : "No structures selected.";

                IList<string> models = _catalog != null
                    ? ModelsForSelection()
                    : new List<string>();
                string suffix = models.Count > 1
                    ? " across " + models.Count + " models"
                    : string.Empty;
                return prefix + n + (n == 1 ? " structure" : " structures") + suffix + ".";
            }
        }

        /// <summary>
        /// Whether QA baselines can be recorded at all. False when the deployment
        /// has no lineage secret, or the series has no UID to key on — in either
        /// case the panel says so rather than pretending to record.
        /// </summary>
        public bool CanRecordBaseline
        {
            get
            {
                return !string.IsNullOrEmpty(_lineageSecret)
                       && !string.IsNullOrEmpty(_seriesUid);
            }
        }

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

        /// <summary>
        /// Let the view report something the view model could not know about — a
        /// browser that would not launch, an empty tick list.
        ///
        /// Exists because <see cref="Status"/> has a private setter, and that is
        /// worth keeping: it forces every message through <c>Classify</c> so the
        /// colour follows the text automatically. The view gets a door, not the key.
        /// </summary>
        public void SetStatusFromView(string message)
        {
            Status = message;
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

        /// <summary>
        /// Whether there is a credential.
        ///
        /// <c>_previewSignedIn</c> exists because the layout harness runs on a real
        /// workstation and the token store is a real DPAPI store: a "signed out" render was
        /// coming out signed in, because the box happened to have a saved credential from
        /// testing. A render must not depend on the machine it renders on.
        /// </summary>
        public bool IsSignedIn
        {
            get { return _previewSignedIn ?? _auth.HasCredential; }
        }

        private bool? _previewSignedIn;

        public bool CanRun
        {
            get
            {
                if (IsBusy || _volume == null || !IsSignedIn) return false;

                return Mode == TargetMode.Prompts
                    ? ParsePrompts().Count > 0
                    : _selectedStructureIds.Count > 0;
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

            // Kept in memory only for the lifetime of the panel. Null when the
            // deployment has QA lineage switched off, which CanRecordBaseline reads
            // as "do not offer to record".
            _lineageSecret = me.LineageSecret;
            OnPropertyChanged("CanRecordBaseline");

            AccountName = me.DisplayName;
            QuotaInfo = me.MonthlyQuota.HasValue
                ? string.Format("{0} of {1} jobs used this month  ·  {2}/{3} in flight",
                    me.UsedThisMonth, me.MonthlyQuota.Value, me.Outstanding, me.MaxOutstanding)
                : string.Format("{0} jobs this month  ·  {1}/{2} in flight",
                    me.UsedThisMonth, me.Outstanding, me.MaxOutstanding);
        }

        // ------------------------------------------------------------------------------- //
        //  Catalog and auto-detect
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// Fetch the catalog and scan the structure set. Safe to call repeatedly.
        ///
        /// Both halves are best-effort. A deployment whose catalog cannot be
        /// reached must still be able to run free-text prompts, because that is the
        /// workflow planners depend on; losing the model picker is a degradation,
        /// not an outage. So a failure here sets a status line and returns.
        /// </summary>
        public async Task LoadCatalogAsync()
        {
            try
            {
                var cts = new CancellationTokenSource(TimeSpan.FromSeconds(45));
                Catalog = await _api.GetCatalogAsync(cts.Token);
                RefreshAutoDetect();

                // Auto-detect wins when it found something: a selection derived from this
                // patient is worth more than a template. With nothing on the series, a
                // protocol is a better opening move than an empty prompt box.
                if (_selectedStructureIds.Count == 0
                    && Mode == TargetMode.Prompts
                    && _catalog != null && _catalog.HasProtocols)
                {
                    Mode = TargetMode.Protocol;
                }
            }
            catch (Exception ex)
            {
                Status = "Model list unavailable (" + Describe(ex) +
                         "). Free-text prompts still work.";
            }
        }

        /// <summary>
        /// Match the structures already on this series against the catalog and
        /// pre-select every recognised one.
        ///
        /// Pre-selecting all of them is the point of compare-by-default: leaving the
        /// planner to choose guarantees the QA record is sparse and skewed toward
        /// whatever they happened to be interested in. It costs them one glance to
        /// untick something.
        ///
        /// Only flips <see cref="Mode"/> when there is actually something to select,
        /// so an empty structure set leaves the panel in its familiar prompt mode.
        /// </summary>
        public void RefreshAutoDetect()
        {
            if (_catalog == null || _context == null || _context.StructureSet == null) return;

            try
            {
                IList<StructureAutoDetect.Candidate> existing = _gate.Run(() =>
                {
                    var reader = new EsapiStructureReader(_context.StructureSet, _gate);
                    return reader.ReadCandidates();
                });

                SeedExistingIds(existing.Select(c => c.ExistingId));

                StructureAutoDetect.Detection detection =
                    StructureAutoDetect.Scan(existing, _catalog);
                Detection = detection;

                if (detection.StructureIds.Count > 0 && _selectedStructureIds.Count == 0)
                {
                    SelectStructures(detection.StructureIds);
                    Mode = TargetMode.Structures;
                }
            }
            catch (Exception ex)
            {
                Status = "Could not read the existing structures: " + ex.Message;
            }
        }

        /// <summary>
        /// Weights licences relevant to what this job will actually run.
        ///
        /// Mode-aware, which the structure-only version was not: in Prompts mode it
        /// was showing the licence of a *structure selection* the job would not use,
        /// so a free-text run advertised CADS's CC-BY-SA. Wrong provenance on screen
        /// is worse than none, because it looks authoritative.
        /// </summary>
        public IList<string> CurrentLicences()
        {
            if (_catalog == null) return new List<string>();

            if (Mode == TargetMode.Prompts)
            {
                CatalogModel model = _catalog.Model(PromptModelKey ?? "voxtell");
                if (model == null || string.IsNullOrEmpty(model.WeightsLicence))
                {
                    return new List<string>();
                }
                return new List<string> { model.WeightsLicence };
            }
            return LicencesForSelection();
        }

        /// <summary>Distinct models the current structure selection needs, in catalog order.</summary>
        public IList<string> ModelsForSelection()
        {
            if (_catalog == null || _catalog.Models == null) return new List<string>();

            var needed = new HashSet<string>(
                _selectedStructureIds
                    .Select(id => _catalog.Structure(id))
                    .Where(st => st != null)
                    .Select(st => st.SourceModel),
                StringComparer.Ordinal);

            return _catalog.Models
                .Where(m => needed.Contains(m.Key))
                .Select(m => m.Key)
                .ToList();
        }

        /// <summary>
        /// Distinct weights licences behind the current selection, for the panel.
        ///
        /// Shown rather than buried because CADS publishes three weight variants
        /// under three different licences and only one permits commercial use. Which
        /// one produced a given patient's contours is a question a clinic may have to
        /// answer later, so the planner should be able to see it at the time.
        /// </summary>
        public IList<string> LicencesForSelection()
        {
            if (_catalog == null) return new List<string>();

            return ModelsForSelection()
                .Select(key => _catalog.Model(key))
                .Where(m => m != null && !string.IsNullOrEmpty(m.WeightsLicence))
                .Select(m => m.WeightsLicence)
                .Distinct(StringComparer.Ordinal)
                .ToList();
        }

        // ------------------------------------------------------------------------------- //
        //  The job
        // ------------------------------------------------------------------------------- //

        public async Task RunAsync()
        {
            if (!CanRun) return;

            List<string> prompts = Mode == TargetMode.Prompts
                ? ParsePrompts()
                : new List<string>();

            if (Mode == TargetMode.Prompts)
            {
                string limitProblem = CheckPromptLimits(prompts);
                if (limitProblem != null)
                {
                    Status = limitProblem;
                    return;
                }
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
                    UploadBytes = blob.LongLength,
                    KeepLargest = false,
                    WantMask = false,
                    // Opaque, and null when this deployment has QA lineage off.
                    SeriesKey = LineageKeys.Series(_lineageSecret, _seriesUid),
                    ForKey = LineageKeys.Frame(_lineageSecret, _frameOfReferenceUid),
                    ScannerKey = LineageKeys.Scanner(
                        _lineageSecret, _deviceManufacturer, _deviceModel, _deviceSerial),
                    Baseline = CanRecordBaseline,
                };

                // Exactly one of the two, never both: the server rejects a request
                // carrying both, and NullValueHandling.Ignore keeps the unused one
                // off the wire entirely.
                if (Mode == TargetMode.Prompts)
                {
                    request.Prompts = prompts.ToArray();
                    request.Model = PromptModelKey;
                }
                else
                {
                    request.StructureIds = SelectedStructureIds.ToArray();
                }

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

            List<StructurePlan> plans = importer.BuildPlan(results, NameRow);

            // The set may have gained structures since the last scan, and the rows' targets
            // are resolved against this cache from here on.
            try
            {
                var reader = new EsapiStructureReader(_context.StructureSet, _gate);
                SeedExistingIds(reader.ReadCandidates().Select(c => c.ExistingId));
            }
            catch
            {
                // A failed re-read must not lose a completed job: the rows keep the targets
                // the importer resolved, they just stop updating as ids are edited.
            }

            return plans;
        }

        /// <summary>
        /// What one result should be called, and what it should write into.
        ///
        /// Three sources, in order: the clinic protocol (id, DICOM type, colour), the
        /// catalog (display name), then the result itself. The DICOM type falls back to
        /// CONTROL rather than being guessed from the structure's group — inventing
        /// clinical metadata from a group name is not this tool's call, and a protocol is
        /// exactly where a clinic states it.
        /// </summary>
        private PlanNaming NameRow(InferenceResult result)
        {
            if (result == null) return null;

            var naming = new PlanNaming();

            CatalogStructure structure = _catalog != null && result.StructureId != null
                ? _catalog.Structure(result.StructureId)
                : null;
            if (structure != null) naming.DisplayName = structure.DisplayName;

            ProtocolEntry entry = ProtocolEntryFor(result.StructureId);
            if (entry != null)
            {
                naming.WriteAs = entry.SafeWriteAs;
                naming.DicomType = entry.DicomType;

                byte r, g, b;
                if (entry.TryColour(out r, out g, out b))
                {
                    naming.Colour = System.Windows.Media.Color.FromRgb(r, g, b);
                }
            }

            return naming;
        }

        /// <summary>
        /// Writes the ticked structures. Synchronous and on the ESAPI thread by necessity —
        /// this is the only code that modifies the patient.
        /// </summary>
        /// <summary>
        /// Unlock the patient for writing, once, immediately before the first write.
        ///
        /// Deferred out of <c>Script.Execute</c> deliberately. Unlocking on open
        /// marks the patient modified in Eclipse before the planner has decided
        /// anything, so merely opening the panel to read a QA verdict could prompt
        /// to save. Gated on <c>CanModifyData()</c> so a read-only session gets a
        /// clear message instead of an exception from inside the importer.
        /// </summary>
        private bool EnsureWritable()
        {
            if (_writeUnlocked) return true;

            try
            {
                if (_context == null)
                {
                    Status = "No Eclipse context, so nothing can be written.";
                    return false;
                }

                if (!_context.Patient.CanModifyData())
                {
                    Status = "This patient is read-only in Eclipse, so nothing can be written.";
                    return false;
                }

                _context.Patient.BeginModifications();
                _writeUnlocked = true;
                return true;
            }
            catch (Exception ex)
            {
                Status = "Could not unlock the patient for writing: " + ex.Message +
                         " Close and re-run the script.";
                return false;
            }
        }

        private bool _writeUnlocked;

        public void ImportSelected()
        {
            if (Plans == null || _results == null) return;

            int selected = Plans.Count(p => p.Selected);
            if (selected == 0)
            {
                Status = "Nothing ticked, so nothing was imported.";
                return;
            }

            if (!EnsureWritable()) return;

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

        /// <summary>
        /// Write the ticked structures, then record what was written as the QA
        /// baseline for this series.
        ///
        /// The baseline is read back out of Eclipse rather than taken from the
        /// server's response, and that is deliberate: what matters for a later
        /// comparison is the geometry that actually landed in the patient, after
        /// ESAPI's own contour handling, not what we asked it to write. Reading it
        /// back also means a partially successful import records exactly the
        /// structures that succeeded.
        ///
        /// Recording is best-effort and never blocks or undoes the import. The
        /// structures are in the patient and useful whether or not QA recorded; a
        /// failed snapshot must read as "QA not recorded", not as a failed import.
        /// </summary>
        public async Task ImportAndRecordAsync()
        {
            ImportSelected();

            if (Phase != WorkflowPhase.Imported) return;
            if (!CanRecordBaseline) return;
            if (ImportSummary == null || ImportSummary.Count == 0) return;

            try
            {
                await RecordBaselineAsync(ImportSummary);
            }
            catch (Exception ex)
            {
                Status = ImportedMessage(ImportSummary.Count) +
                         " QA baseline not recorded (" + Describe(ex) + ").";
            }
        }

        /// <summary>
        /// Snapshot the named structures and post them as the run-1 baseline.
        ///
        /// The snapshot is contours and geometry, never voxels: metrics need those
        /// and nothing else, which is what keeps a baseline at kilobytes per patient
        /// and lets it outlive the volume's short retention window without keeping
        /// any image.
        /// </summary>
        private async Task RecordBaselineAsync(IList<string> structureIds)
        {
            Status = "Recording the QA baseline...";

            StructureSnapshot snapshot = _gate.Run(() =>
            {
                var reader = new EsapiStructureReader(_context.StructureSet, _gate);
                return reader.ReadSnapshot(
                    Geometry.FromVolumeSource(_volume),
                    _catalog,
                    structureIds,
                    StructureSnapshot.RoleBaseline,
                    null);
            });

            snapshot.SeriesKey = LineageKeys.Series(_lineageSecret, _seriesUid);
            snapshot.ForKey = LineageKeys.Frame(_lineageSecret, _frameOfReferenceUid);
            snapshot.ScannerKey = LineageKeys.Scanner(
                _lineageSecret, _deviceManufacturer, _deviceModel, _deviceSerial);

            if (string.IsNullOrEmpty(snapshot.SeriesKey))
            {
                Status = ImportedMessage(structureIds.Count) +
                         " No series UID, so no QA baseline was recorded.";
                return;
            }

            var cts = new CancellationTokenSource(TimeSpan.FromMinutes(3));
            BaselineResponse response = await _api.PostBaselineAsync(snapshot, _jobId, cts.Token);

            BaselineWebUrl = response.WebUrl;
            Status = ImportedMessage(structureIds.Count) + " " + (response.Created
                ? "QA baseline recorded for " + response.StructureCount + " structure(s)."
                : "QA baseline already recorded; nothing duplicated.");
        }

        private static string ImportedMessage(int count)
        {
            return string.Format(
                "Imported {0} structure(s). Save in Eclipse to keep them.", count);
        }

        private string _baselineWebUrl;

        /// <summary>Deep link to the coloured comparison, when the server offers one.</summary>
        public string BaselineWebUrl
        {
            get { return _baselineWebUrl; }
            private set { _baselineWebUrl = value; OnPropertyChanged(); }
        }

        public List<string> ImportSummary { get; private set; }
        public List<string> ImportWarnings { get; private set; }

        // ------------------------------------------------------------------------------- //
        //  Helpers
        // ------------------------------------------------------------------------------- //

        /// <summary>
        /// Read an ESAPI property that may throw rather than return null.
        ///
        /// Several of the identity properties throw when the object graph is not
        /// fully populated — a series detached from its study, an image opened
        /// without one. A missing UID has to degrade to "no QA lineage" and never to
        /// an exception on a panel the planner only opened to segment with.
        /// </summary>
        private static string TryRead(Func<string> read)
        {
            try { return read(); }
            catch (Exception) { return null; }
        }

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
