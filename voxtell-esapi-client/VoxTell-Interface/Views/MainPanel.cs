using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using VMS.TPS.Common.Model.API;
using VoxTell_Interface.Models;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The panel Eclipse shows: sign in, choose targets, segment, review, write.
    ///
    /// The shell, and why it is not a scroller
    /// --------------------------------------
    /// Everything used to sit in one <see cref="ScrollViewer"/>, with the review table's own
    /// scroller nested inside it. The table could therefore never be taller than the strip
    /// of content it happened to land in, and the header scrolled away with it — taking
    /// "which patient am I in" off screen.
    ///
    /// Now: a fixed header, then a content grid where exactly one region owns the leftover
    /// height, and each list scrolls itself. Which region that is follows the workflow —
    /// the picker while there is nothing to review, the review table afterwards, with the
    /// picker collapsed to one line the planner can reopen. That is the two-run workflow
    /// made literal rather than described.
    ///
    /// Rendering rule
    /// --------------
    /// The panel is a function of the view model. Every handler mutates the view model and
    /// then calls <see cref="Sync"/>; no handler updates a control directly. That is what
    /// keeps "what the screen says" and "what will be written into the patient" from
    /// drifting apart, which in a panel like this is a safety property, not a style.
    /// </summary>
    internal sealed class MainPanel : UserControl
    {
        private readonly MainViewModel _vm;
        private readonly HeaderBar _header;
        private readonly TargetPicker _picker;
        private readonly ReviewTable _review;

        // Device-code prompt
        private readonly Border _signInCard;
        private readonly TextBlock _signInMessage;
        private readonly TextBlock _verificationLink;
        private readonly TextBox _userCode;

        // Series
        private readonly Border _contextStrip;
        private readonly TextBlock _seriesLine;

        // Targets
        private readonly Border _pickerCard;
        private readonly Border _pickerSummary;
        private readonly TextBlock _pickerSummaryText;
        private bool _pickerExpanded;

        // Run
        private readonly Button _run;
        private readonly Button _cancel;
        private readonly Track _progress;
        private readonly TextBlock _serverMessage;

        // Review
        private readonly Border _reviewCard;
        private readonly Button _import;
        private readonly TextBlock _baselineNote;

        // Settings
        private readonly Border _settingsPane;
        private readonly TextBox _baseUrlBox;
        private readonly PasswordBox _apiKeyBox;

        private readonly TextBlock _status;

        // The two rows that trade the leftover height, and the review table's own.
        private readonly RowDefinition _pickerRow;
        private readonly RowDefinition _reviewRow;
        private readonly RowDefinition _reviewTableRow;

        /// <summary>The Eclipse entry point. Builds its own view model.</summary>
        public MainPanel(ScriptContext context)
            : this(new MainViewModel(context), loadCatalog: true)
        {
        }

        /// <summary>
        /// The real constructor. Takes the view model so the layout harness can render this
        /// exact panel with a preview view model and no Eclipse — see
        /// <see cref="MainViewModel.CreatePreview"/>. Zero drift: the harness renders the
        /// shipping panel, not a copy of it.
        /// </summary>
        internal MainPanel(MainViewModel vm, bool loadCatalog)
        {
            // Constructed on the ESAPI thread, synchronously from Script.Execute, so
            // EsapiGate captures the right dispatcher.
            _vm = vm;

            _header = new HeaderBar(
                onSignIn: () => Fire(_vm.SignInAsync()),
                onSignOut: () => { _vm.SignOut(); Sync(); },
                onSettings: ToggleSettings,
                onOpenVr: OpenWebComparison);

            _picker = new TargetPicker(_vm, Sync, () => Fire(_vm.LoadCatalogAsync()));
            _review = new ReviewTable(plan => _vm.RetargetPlan(plan));

            // --- device code ------------------------------------------------ //
            _signInMessage = Ui.Small(string.Empty);
            _verificationLink = Controls.Link(string.Empty, OpenVerificationUri);
            // Read-only rather than a label so the planner can select and copy it: this
            // code is read off the screen and typed into a phone, and a misread character
            // costs a whole retry.
            _userCode = new TextBox
            {
                IsReadOnly = true,
                FontFamily = Theme.MonoFamily,
                FontSize = Theme.SizeMono,
                Foreground = Theme.Ink,
                Background = System.Windows.Media.Brushes.Transparent,
                BorderThickness = new Thickness(0),
                HorizontalContentAlignment = HorizontalAlignment.Center,
            };
            _signInCard = Ui.Section("Sign in on another device", Ui.Stack(Theme.Space2,
                _signInMessage, _verificationLink, _userCode));

            // --- series ----------------------------------------------------- //
            //
            // One line, not a card of three: the geometry is context, and the HU mapping —
            // which matters only when it is wrong — is in the tooltip.
            _seriesLine = Ui.Small(string.Empty).Fg(Theme.InkMuted);
            _contextStrip = new Border
            {
                Background = Theme.Panel,
                CornerRadius = Theme.CardCorner,
                Padding = new Thickness(Theme.CardInset, Theme.Space2, Theme.CardInset, Theme.Space2),
                SnapsToDevicePixels = true,
                Child = _seriesLine,
            };

            // --- targets ---------------------------------------------------- //
            _pickerCard = Ui.Section("What to segment", _picker.Element);

            _pickerSummaryText = Ui.Small(string.Empty).Fg(Theme.InkMuted);
            _pickerSummary = new Border
            {
                Background = Theme.Panel,
                CornerRadius = Theme.CardCorner,
                Padding = new Thickness(Theme.CardInset, Theme.Space2, Theme.CardInset, Theme.Space2),
                SnapsToDevicePixels = true,
                Child = Ui.Grid("Auto", "*,Auto",
                    _pickerSummaryText.At(0, 0),
                    Controls.Ghost("Change", (s, e) => { _pickerExpanded = true; Sync(); })
                        .At(0, 1)),
            };
            _pickerSummary.Show(false);

            // --- run -------------------------------------------------------- //
            _run = Controls.Primary("Segment", (s, e) => Fire(_vm.RunAsync()));
            _cancel = Controls.Button("Cancel", (s, e) => { _vm.Cancel(); Sync(); });
            _progress = Controls.Progress();
            _serverMessage = Ui.Small(string.Empty).Fg(Theme.InkMuted);

            // On its own surface rather than loose on the window background: the primary
            // action of the panel should not read as an orphan between two cards.
            Border actions = Ui.Card(Ui.Stack(Theme.Space2,
                Ui.Row(Theme.Space2, _run, _cancel),
                _progress.Element,
                _serverMessage));

            // --- review ----------------------------------------------------- //
            _import = Controls.Primary("Write ticked structures", (s, e) => Fire(ImportAsync()));
            _baselineNote = Ui.Micro(string.Empty).Fg(Theme.InkFaint);

            // The table's row is starred only when there IS a table. Left starred while
            // empty, it pushed the write button to the bottom of a tall empty card, half a
            // window away from the message explaining why there is nothing to write.
            _reviewTableRow = new RowDefinition { Height = GridLength.Auto };
            var reviewContent = new Grid();
            reviewContent.RowDefinitions.Add(_reviewTableRow);
            reviewContent.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            reviewContent.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            reviewContent.ColumnDefinitions.Add(new ColumnDefinition());
            reviewContent.VerticalAlignment = VerticalAlignment.Top;
            reviewContent.Children.Add(_review.Element.At(0, 0));
            reviewContent.Children.Add(_import.Left().At(1, 0).Gap(0, Theme.Space2, 0, 0));
            reviewContent.Children.Add(_baselineNote.At(2, 0).Gap(0, Theme.Space1, 0, 0));

            _reviewCard = Ui.Section("Review", reviewContent);

            // --- settings --------------------------------------------------- //
            Border baseUrlInput = Ui.Input(out _baseUrlBox);
            Border apiKeyInput = Ui.Password(out _apiKeyBox);
            _settingsPane = Ui.Card(Ui.Stack(Theme.Space2,
                Ui.Micro("SERVER").Fg(Theme.InkMuted), baseUrlInput,
                Ui.Micro("API KEY").Fg(Theme.InkMuted), apiKeyInput,
                Ui.Row(Theme.Space2,
                    Controls.Button("Apply", (s, e) => ApplySettings()),
                    Controls.Button("Close", (s, e) => { _settingsPane.Show(false); }))));
            _settingsPane.Show(false);

            _status = Ui.Small(string.Empty);

            // --- shell ------------------------------------------------------ //
            _pickerRow = new RowDefinition { Height = new GridLength(1, GridUnitType.Star) };
            _reviewRow = new RowDefinition { Height = GridLength.Auto };

            var content = new Grid();
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // sign-in
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // settings
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // series
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // summary
            content.RowDefinitions.Add(_pickerRow);
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // actions
            content.RowDefinitions.Add(_reviewRow);
            content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // status
            content.ColumnDefinitions.Add(new ColumnDefinition());

            Place(content, _signInCard, 0);
            Place(content, _settingsPane, 1);
            Place(content, _contextStrip, 2);
            Place(content, _pickerSummary, 3);
            Place(content, _pickerCard, 4);
            Place(content, actions, 5);
            Place(content, _reviewCard, 6);
            Place(content, _status, 7);

            var body = new Border
            {
                Padding = new Thickness(Theme.Space3),
                Child = content,
            };

            var root = Ui.Grid("Auto,*", "*",
                _header.Element.At(0, 0),
                body.At(1, 0));

            Content = root;
            Background = Theme.Void;
            UseLayoutRounding = true;
            SnapsToDevicePixels = true;

            // Ctrl+Enter runs the job from anywhere in the panel, including from inside the
            // prompt box, where Enter itself has to mean "new line".
            InputBindings.Add(new KeyBinding(
                new RelayCommand(() => { if (_vm.CanRun) Fire(_vm.RunAsync()); }),
                Key.Enter, ModifierKeys.Control));

            _vm.PropertyChanged += OnViewModelChanged;
            Sync();

            // Fetch the catalog and scan the structure set without blocking the window's
            // first paint. Eclipse has already handed us the window, so a synchronous fetch
            // here would show the planner an empty frame.
            if (loadCatalog) Fire(_vm.LoadCatalogAsync());
        }

        private static void Place(Grid grid, FrameworkElement element, int row)
        {
            Grid.SetRow(element, row);
            Grid.SetColumn(element, 0);
            Thickness m = element.Margin;
            element.Margin = new Thickness(m.Left, m.Top + (row == 0 ? 0 : Theme.Space2),
                m.Right, m.Bottom);
            grid.Children.Add(element);
        }

        // ------------------------------------------------------------------- //
        //  Rendering
        // ------------------------------------------------------------------- //

        private void OnViewModelChanged(object sender, PropertyChangedEventArgs e)
        {
            // MARSHAL. A previous version asserted the view model always raises on the UI
            // thread and skipped the marshalling the WinForms view had, as a toolkit
            // artefact. That was wrong, and it threw "The calling thread cannot access this
            // object because a different thread owns it" on the first real run.
            //
            // The view model's own awaits do come back to the captured context, but not
            // everything reaching a property setter is one of them: a faulted continuation,
            // a cancellation callback, or an HTTP call that completed while no context was
            // captured all raise PropertyChanged from a pool thread. WPF is stricter about
            // this than WinForms — every DependencyObject has hard thread affinity — so the
            // check lives here, once, rather than being reasoned about per call site.
            if (!Dispatcher.CheckAccess())
            {
                Dispatcher.BeginInvoke(
                    new Action<object, PropertyChangedEventArgs>(OnViewModelChanged),
                    sender, e);
                return;
            }

            // No per-property special cases: Sync renders everything, including the review
            // table, so what the screen shows cannot depend on which notification happened
            // to arrive. An earlier version bound the table only from a "Plans"
            // notification, and a panel built when rows already existed showed "Nothing to
            // review yet" over five real rows.
            Sync();
        }

        /// <summary>Render the whole panel from the view model. Idempotent.</summary>
        private void Sync()
        {
            bool signedIn = _vm.IsSignedIn;
            bool hasRows = _vm.Plans != null && _vm.Plans.Count > 0;

            _header.Render(
                _vm.Phase, signedIn, hasRows,
                _vm.AccountName, _vm.QuotaInfo,
                _vm.CanRecordBaseline,
                !string.IsNullOrEmpty(_vm.BaselineWebUrl),
                _vm.IsBusy);

            Services.Auth.SignInPrompt prompt = _vm.CurrentSignInPrompt;
            bool showCode = prompt != null && !string.IsNullOrEmpty(prompt.UserCode);
            _signInCard.Show(showCode);
            _signInMessage.Text = prompt != null ? (prompt.Message ?? string.Empty) : string.Empty;
            string uri = prompt != null ? prompt.VerificationUri : null;
            _verificationLink.Text = uri ?? string.Empty;
            _verificationLink.Show(!string.IsNullOrEmpty(uri));
            _userCode.Text = prompt != null ? (prompt.UserCode ?? string.Empty) : string.Empty;

            _seriesLine.Text = SeriesLine();
            _seriesLine.ToolTip = string.IsNullOrEmpty(_vm.RescaleInfo) ? null : _vm.RescaleInfo;

            // Which region owns the leftover height. Before there is anything to review the
            // picker gets it; afterwards the table does, and the picker becomes one line.
            bool collapsePicker = hasRows && !_pickerExpanded;
            _pickerCard.Show(!collapsePicker);
            _pickerSummary.Show(collapsePicker);
            // "Next run", not "segmenting": the strip is the collapsed picker, and what it
            // describes is what pressing Segment again would ask for.
            _pickerSummaryText.Text = "Next run: " + (_vm.TargetSummary ?? string.Empty);

            // Only the panes with a list in them want the leftover height. The prompt box
            // is two lines, so in Prompts mode stretching the picker would just be a large
            // empty card.
            bool pickerWantsHeight = !collapsePicker && _vm.Mode != TargetMode.Prompts;
            _pickerRow.Height = pickerWantsHeight
                ? new GridLength(1, GridUnitType.Star)
                : GridLength.Auto;
            _reviewRow.Height = pickerWantsHeight
                ? GridLength.Auto
                : new GridLength(1, GridUnitType.Star);

            _run.IsEnabled = _vm.CanRun;
            _cancel.IsEnabled = _vm.CanCancel;
            _cancel.Show(_vm.CanCancel);

            bool running = _vm.Progress > 0 && _vm.Progress < 1;
            _progress.Element.Show(running);
            _progress.Set(_vm.Progress);

            _serverMessage.Text = _vm.ServerMessage ?? string.Empty;
            _serverMessage.Show(!string.IsNullOrEmpty(_vm.ServerMessage));

            _review.Bind(_vm.Plans);
            _reviewTableRow.Height = hasRows
                ? new GridLength(1, GridUnitType.Star)
                : GridLength.Auto;

            // Hidden rather than disabled while there is nothing to write: a permanently
            // greyed primary action is chrome, and the empty state already says what to do.
            _import.Show(hasRows);
            _import.IsEnabled = hasRows && !_vm.IsBusy;

            // Say plainly whether QA will record. A panel that silently does not record is
            // how a clinic discovers months later that it has no data.
            _baselineNote.Show(hasRows);
            if (hasRows)
            {
                _baselineNote.Text = _vm.CanRecordBaseline
                    ? "Writing also records a QA baseline for this series."
                    : "QA baseline not available for this series, so nothing will be recorded.";
            }

            if (_baseUrlBox.Text != (_vm.BaseUrl ?? string.Empty)
                && !_baseUrlBox.IsKeyboardFocusWithin)
            {
                _baseUrlBox.Text = _vm.BaseUrl ?? string.Empty;
            }

            _status.Text = _vm.Status ?? string.Empty;
            _status.Foreground = Theme.StatusBrush(_vm.Severity);
            _status.Show(!string.IsNullOrEmpty(_vm.Status));

            _picker.Refresh();
        }

        private string SeriesLine()
        {
            string set = "Structure set: " + (_vm.StructureSetInfo ?? "none open");
            return string.IsNullOrEmpty(_vm.ImageInfo)
                ? set
                : _vm.ImageInfo + "   ·   " + set;
        }

        // ------------------------------------------------------------------- //
        //  Actions
        // ------------------------------------------------------------------- //

        private void ToggleSettings()
        {
            _settingsPane.Show(_settingsPane.Visibility != Visibility.Visible);
        }

        private async Task ImportAsync()
        {
            IList<StructurePlan> plans = _vm.Plans;
            if (plans == null) return;

            int ticked = plans.Count(p => p.Selected);
            if (ticked == 0)
            {
                _vm.SetStatusFromView("Nothing ticked, so nothing was written.");
                Sync();
                return;
            }

            // The last confirmation before the patient is modified. It names the structure
            // set and counts the rows that will REPLACE an existing structure, because
            // "which set am I writing into, and what am I overwriting" is the mistake this
            // dialog exists to catch.
            int replacing = plans.Count(p => p.Selected && !p.WillCreate);
            string overwrite = replacing == 0
                ? "All of them are new structures."
                : replacing + " of them replace the contours of an existing structure.";

            MessageBoxResult answer = MessageBox.Show(
                string.Format(
                    "Write {0} structure(s) into '{1}'?\n\n{2}",
                    ticked, _vm.StructureSetInfo, overwrite),
                "VoxTell", MessageBoxButton.OKCancel, MessageBoxImage.Warning);

            if (answer != MessageBoxResult.OK) return;

            await _vm.ImportAndRecordAsync();
            Sync();

            if (_vm.ImportWarnings != null && _vm.ImportWarnings.Count > 0)
            {
                MessageBox.Show(
                    string.Join(Environment.NewLine, _vm.ImportWarnings.ToArray()),
                    "VoxTell - warnings", MessageBoxButton.OK, MessageBoxImage.Information);
            }
        }

        private void ApplySettings()
        {
            _vm.ApplyBaseUrl(_baseUrlBox.Text);
            if (!string.IsNullOrWhiteSpace(_apiKeyBox.Password))
            {
                Fire(_vm.ApplyApiKeyAsync(_apiKeyBox.Password));
                _apiKeyBox.Clear();
            }
            Sync();
        }

        private void OpenVerificationUri()
        {
            Services.Auth.SignInPrompt prompt = _vm.CurrentSignInPrompt;
            Launch(prompt != null ? prompt.VerificationUri : null);
        }

        private void OpenWebComparison()
        {
            Launch(_vm.BaselineWebUrl);
        }

        private void Launch(string url)
        {
            if (string.IsNullOrWhiteSpace(url)) return;
            try
            {
                Process.Start(url);
            }
            catch (Exception ex)
            {
                // A hardened clinical workstation may have no default browser, and that must
                // not take the panel down mid-review.
                _vm.SetStatusFromView("Could not open the browser: " + ex.Message);
                Sync();
            }
        }

        /// <summary>
        /// Run a task without <c>async void</c>.
        ///
        /// An unobserved exception in an <c>async void</c> handler is raised on the thread
        /// pool and takes the Eclipse process with it, not just the panel.
        /// </summary>
        private void Fire(Task task)
        {
            if (task == null) return;
            task.ContinueWith(t =>
            {
                string message = t.Exception.GetBaseException().Message;

                // ExecuteSynchronously used to be set here, which ran this body on whichever
                // thread faulted the task — a pool thread — and then called Sync(), touching
                // WPF from off the UI thread. The fix is not to drop the flag and hope: the
                // continuation is explicitly marshalled, so it is correct regardless of
                // which thread completes the task.
                Action report = () =>
                {
                    _vm.SetStatusFromView("Unexpected error: " + message);
                    Sync();
                };
                if (Dispatcher.CheckAccess()) report();
                else Dispatcher.BeginInvoke(report);
            },
            TaskContinuationOptions.OnlyOnFaulted);
        }
    }

    /// <summary>
    /// The smallest possible <see cref="ICommand"/>, for the keyboard shortcut.
    ///
    /// A <see cref="KeyBinding"/> needs a command; there is no event to hang a shortcut on.
    /// </summary>
    internal sealed class RelayCommand : ICommand
    {
        private readonly Action _run;

        public RelayCommand(Action run) { _run = run; }

        /// <summary>
        /// Forwarded to <see cref="CommandManager.RequerySuggested"/> rather than left as an
        /// unraised event: WPF asks a command whether it can execute through this, and an
        /// event nothing ever raises is also a compiler warning.
        /// </summary>
        public event EventHandler CanExecuteChanged
        {
            add { CommandManager.RequerySuggested += value; }
            remove { CommandManager.RequerySuggested -= value; }
        }

        public bool CanExecute(object parameter) { return true; }

        public void Execute(object parameter) { if (_run != null) _run(); }
    }
}
