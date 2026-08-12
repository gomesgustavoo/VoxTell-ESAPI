using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using VMS.TPS.Common.Model.API;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;
using VoxTell_Interface.Services.Auth;
using VoxTell_Interface.ViewModels;
// Brings the palette, type scale and spacing in unqualified, so BgDark/Surface/Accent read
// exactly as they did when they were fields on MainControl.
using static VoxTell_Interface.Views.UiTheme;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The plugin's whole interface: hand-drawn WinForms hosted in Eclipse's WPF window through
    /// WindowsFormsHost. No designer file, by choice — an ESAPI plugin is easier to review and
    /// diff as code than as a generated InitializeComponent.
    ///
    /// The v1 panel opened on a backend-URL textbox that defaulted to localhost and was never
    /// persisted, so a planner retyped it every session. That is gone: the server address lives
    /// in settings with the production default, and the first thing on screen is who you are
    /// signed in as.
    /// </summary>
    public class MainControl : UserControl
    {
        // Palette, type scale and spacing all live in UiTheme.cs — see the `using static`
        // above. They were fields here until the typography pass; sharing them is what lets
        // the grid, the buttons and the labels agree on a single look.

        private readonly MainViewModel _viewModel;

        // Account
        private Label lblAccount;
        private Label lblQuota;
        private Button btnSignIn;
        private Button btnSignOut;

        // Device-code fallback
        private Panel pnlSignInPrompt;
        private Label lblSignInMessage;
        private TextBox txtUserCode;
        private LinkLabel lnkVerification;

        // Image
        private Label lblImageInfo;
        private Label lblRescale;

        // Prompts + run
        private TextBox txtPrompts;
        private Button btnRun;
        private Button btnCancel;

        // Progress
        private Panel progressBarHost;
        private Label lblStatus;
        private Label lblServerMessage;

        // Review
        private DataGridView gridResults;
        private Button btnImport;

        // Settings
        private Panel pnlSettings;
        private TextBox txtBaseUrl;
        private TextBox txtApiKey;
        private Button btnSettingsToggle;
        private Button btnSaveSettings;

        private double _progressValue;
        private readonly Dictionary<Button, bool> _buttonHover = new Dictionary<Button, bool>();
        private readonly Dictionary<Button, bool> _buttonPressed = new Dictionary<Button, bool>();

        public MainControl(ScriptContext context)
        {
            BackColor = BgDark;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);

            // MUST be set before BuildLayout(), and it is the single most visible fix in the
            // typography pass. Without it every control that does not name a Font inherits
            // Control.DefaultFont — Microsoft Sans Serif 8.25pt — which is what made the review
            // grid and the prompts box look like a Windows 95 dialog.
            Font = Body;

            // Declarative layout below is written in logical pixels at 96 DPI. Stating that
            // baseline lets WinForms rescale every child's bounds on a 125 %/150 % display,
            // instead of laying out a 96-DPI panel and letting the compositor stretch it.
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;

            BuildLayout();

            // Constructed here, on the ESAPI thread, so the ViewModel's EsapiGate captures it.
            _viewModel = new MainViewModel(context, this);

            BindViewModel();
            SyncFromViewModel();
            UpdateEnabledState();
        }

        // ------------------------------------------------------------------------------- //
        //  Layout
        // ------------------------------------------------------------------------------- //

        private void BuildLayout()
        {
            Dock = DockStyle.Fill;
            Padding = new Padding(14, 12, 14, 12);

            var root = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                BackColor = BgDark,
                ColumnCount = 1,
                RowCount = 8,
            };
            root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 0 account
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 1 sign-in prompt
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 2 image
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 3 prompts
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 4 progress
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));  // 5 results
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 6 settings
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));      // 7 status

            root.Controls.Add(BuildAccountPanel(), 0, 0);
            root.Controls.Add(BuildSignInPromptPanel(), 0, 1);
            root.Controls.Add(BuildImagePanel(), 0, 2);
            root.Controls.Add(BuildPromptsPanel(), 0, 3);
            root.Controls.Add(BuildProgressPanel(), 0, 4);
            root.Controls.Add(BuildResultsPanel(), 0, 5);
            root.Controls.Add(BuildSettingsPanel(), 0, 6);
            root.Controls.Add(BuildStatusPanel(), 0, 7);

            Controls.Add(root);
        }

        private Control BuildAccountPanel()
        {
            // No section title here: the signed-in identity IS the heading, and in a 780 px
            // window a redundant "Account" label costs a row that the review grid needs.
            Panel card = Card();
            card.Height = 66;

            lblAccount = new Label
            {
                Text = "Not signed in",
                ForeColor = TextPrimary,
                Font = H1,
                AutoSize = true,
                Location = new Point(CardInset, 11),
            };

            lblQuota = new Label
            {
                Text = "",
                ForeColor = TextMuted,
                Font = Small,
                AutoSize = true,
                Location = new Point(CardInset, 38),
            };

            btnSignIn = CreateButton("Sign in");
            btnSignIn.Size = new Size(92, 30);

            btnSignOut = CreateButton("Sign out");
            btnSignOut.Size = new Size(92, 30);
            btnSignOut.Visible = false;

            card.Controls.Add(lblAccount);
            card.Controls.Add(lblQuota);
            card.Controls.Add(btnSignIn);
            card.Controls.Add(btnSignOut);

            card.Resize += (s, e) =>
            {
                int inset = Px(CardInset);
                btnSignIn.Location = new Point(
                    Math.Max(inset, card.Width - btnSignIn.Width - inset),
                    (card.Height - btnSignIn.Height) / 2);
                btnSignOut.Location = btnSignIn.Location;
            };

            return card;
        }

        private Control BuildSignInPromptPanel()
        {
            // Only shown for the device-code fallback — a workstation where no loopback port
            // bound or no browser is registered. The PKCE path needs no instructions.
            pnlSignInPrompt = TitledCard("Sign in on another device");
            pnlSignInPrompt.Height = 116;
            pnlSignInPrompt.Visible = false;

            lblSignInMessage = new Label
            {
                Text = "",
                ForeColor = TextLabel,
                Font = Body,
                AutoSize = false,
                Location = new Point(CardInset, ContentTop),
                Size = new Size(400, 19),
            };

            lnkVerification = new LinkLabel
            {
                Text = "",
                LinkColor = Accent,
                ActiveLinkColor = Accent,
                VisitedLinkColor = Accent,
                Font = Body,
                AutoSize = true,
                Location = new Point(CardInset, ContentTop + 22),
            };
            lnkVerification.LinkClicked += (s, e) =>
            {
                try { System.Diagnostics.Process.Start(lnkVerification.Text); }
                catch { /* No browser here — the code can be entered on another device. */ }
            };

            txtUserCode = new TextBox
            {
                Text = "",
                ReadOnly = true,
                BorderStyle = BorderStyle.None,
                BackColor = InputBg,
                ForeColor = Accent,
                // A read-only TextBox rather than a Label: the point of the code is to be
                // selected and copied somewhere else.
                Font = Mono,
                TextAlign = HorizontalAlignment.Center,
                Location = new Point(CardInset, ContentTop + 44),
                Size = new Size(190, 26),
            };

            pnlSignInPrompt.Controls.Add(lblSignInMessage);
            pnlSignInPrompt.Controls.Add(lnkVerification);
            pnlSignInPrompt.Controls.Add(txtUserCode);

            pnlSignInPrompt.Resize += (s, e) =>
                lblSignInMessage.Width =
                    Math.Max(80, pnlSignInPrompt.Width - Px(CardInset) * 2);

            return pnlSignInPrompt;
        }

        private Control BuildImagePanel()
        {
            Panel card = TitledCard("Image");
            card.Height = 88;

            lblImageInfo = new Label
            {
                Text = "",
                ForeColor = TextLabel,
                Font = Body,
                AutoSize = false,
                Location = new Point(CardInset, ContentTop),
                Size = new Size(460, 19),
            };

            // Shown deliberately: it is the visible evidence that the HU rescale is being sent.
            // A CT should read an intercept of about -1024; a flat 0 means something is wrong.
            lblRescale = new Label
            {
                Text = "",
                ForeColor = TextMuted,
                Font = Small,
                AutoSize = false,
                Location = new Point(CardInset, ContentTop + 22),
                Size = new Size(460, 18),
            };

            card.Controls.Add(lblImageInfo);
            card.Controls.Add(lblRescale);

            card.Resize += (s, e) =>
            {
                lblImageInfo.Width = Math.Max(80, card.Width - Px(CardInset) * 2);
                lblRescale.Width = lblImageInfo.Width;
            };

            return card;
        }

        private Control BuildPromptsPanel()
        {
            Panel card = TitledCard("Prompts — one per line, up to 16");
            card.Height = 156;

            Panel wrapper = CreateRoundedTextBox("", out txtPrompts);
            wrapper.Location = new Point(CardInset, ContentTop);
            wrapper.Size = new Size(300, 62);
            txtPrompts.Multiline = true;
            txtPrompts.ScrollBars = ScrollBars.Vertical;

            btnRun = CreateButton("Segment");
            btnRun.Size = new Size(104, 32);
            btnRun.Location = new Point(CardInset, ContentTop + 72);

            btnCancel = CreateButton("Cancel");
            btnCancel.Size = new Size(90, 32);
            btnCancel.Location = new Point(CardInset + 112, ContentTop + 72);
            btnCancel.Enabled = false;

            card.Controls.Add(wrapper);
            card.Controls.Add(btnRun);
            card.Controls.Add(btnCancel);

            card.Resize += (s, e) =>
                wrapper.Width = Math.Max(120, card.Width - Px(CardInset) * 2);

            return card;
        }

        private Control BuildProgressPanel()
        {
            Panel card = Card();
            card.Height = 52;

            progressBarHost = new Panel
            {
                Location = new Point(CardInset, 14),
                Size = new Size(300, 5),
                BackColor = InputBg,
            };
            progressBarHost.Paint += ProgressBarHost_Paint;

            // The server's own message, verbatim: it carries "Waiting for the GPU" and the
            // engine's notices, which is the difference between "slow" and "stuck".
            lblServerMessage = new Label
            {
                Text = "",
                ForeColor = TextMuted,
                Font = Small,
                AutoSize = false,
                Location = new Point(CardInset, 27),
                Size = new Size(460, 18),
            };

            card.Controls.Add(progressBarHost);
            card.Controls.Add(lblServerMessage);

            card.Resize += (s, e) =>
            {
                progressBarHost.Width = Math.Max(60, card.Width - Px(CardInset) * 2);
                lblServerMessage.Width = progressBarHost.Width;
            };

            return card;
        }

        private Control BuildResultsPanel()
        {
            Panel card = TitledCard("Results — tick what to write into the structure set");
            card.Dock = DockStyle.Fill;

            // A grid, not v1's append-only text box: that had ScrollBars.None so anything past
            // the visible area was unreachable, and it offered no way to decline a structure
            // before it was written into the patient.
            gridResults = new DataGridView
            {
                Location = new Point(CardInset, ContentTop),
                BackgroundColor = Surface,
                BorderStyle = BorderStyle.None,
                AllowUserToAddRows = false,
                AllowUserToDeleteRows = false,
                AllowUserToResizeRows = false,
                RowHeadersVisible = false,
                SelectionMode = DataGridViewSelectionMode.CellSelect,
                EditMode = DataGridViewEditMode.EditOnEnter,
                MultiSelect = false,
                GridColor = GridLine,
                Font = Body,
            };
            StyleGrid(gridResults);

            gridResults.Columns.Add(new DataGridViewCheckBoxColumn
            {
                Name = "Import", HeaderText = "", Width = 32,
            });
            // Widths are tuned for the 9.75pt body font. They were set for 8.5pt and the larger
            // face pushed "1,284 cm3, 210 contours" into an ellipsis — a number a planner is
            // meant to sanity-check before writing anything into a patient, so it must fit.
            gridResults.Columns.Add(new DataGridViewTextBoxColumn
            {
                Name = "Prompt", HeaderText = "Prompt", ReadOnly = true, Width = 112,
            });
            gridResults.Columns.Add(new DataGridViewTextBoxColumn
            {
                // 16 characters is Eclipse's own cap on a structure Id.
                Name = "StructureId", HeaderText = "Structure", Width = 104, MaxInputLength = 16,
            });
            var typeColumn = new DataGridViewComboBoxColumn
            {
                Name = "DicomType", HeaderText = "Type", Width = 86, FlatStyle = FlatStyle.Flat,
            };
            // CONTROL stays the default because it is the safe choice. ORGAN is offered because
            // CONTROL structures are restricted in optimisation and DVH contexts, which is the
            // first thing a planner runs into after importing.
            typeColumn.Items.AddRange(new object[] { "CONTROL", "ORGAN", "PTV", "AVOIDANCE" });
            gridResults.Columns.Add(typeColumn);
            gridResults.Columns.Add(new DataGridViewTextBoxColumn
            {
                Name = "Detail", HeaderText = "Found", ReadOnly = true, Width = 176,
            });
            gridResults.Columns.Add(new DataGridViewTextBoxColumn
            {
                Name = "Note", HeaderText = "Note", ReadOnly = true,
                AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill,
            });

            btnImport = CreateButton("Import ticked structures");
            btnImport.Size = new Size(210, 32);
            btnImport.Enabled = false;

            card.Controls.Add(gridResults);
            card.Controls.Add(btnImport);

            card.Resize += (s, e) =>
            {
                int inset = Px(CardInset);
                int top = Px(ContentTop);
                // Reserve the button strip plus a gap beneath the grid.
                int footer = btnImport.Height + Px(Space3);
                gridResults.Width = Math.Max(120, card.Width - inset * 2);
                gridResults.Height = Math.Max(60, card.Height - top - footer);
                btnImport.Location = new Point(
                    inset, Math.Max(top, card.Height - btnImport.Height - Px(Space2)));
            };

            return card;
        }

        private static void StyleGrid(DataGridView grid)
        {
            grid.EnableHeadersVisualStyles = false;
            grid.ColumnHeadersDefaultCellStyle.BackColor = GridHeaderBg;
            grid.ColumnHeadersDefaultCellStyle.ForeColor = TextMuted;
            grid.ColumnHeadersDefaultCellStyle.Font = GridHeader;
            grid.ColumnHeadersDefaultCellStyle.SelectionBackColor = GridHeaderBg;
            grid.ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None;
            grid.ColumnHeadersHeight = Px(30);

            // DefaultCellStyle.Font was never set, so every CELL in the review grid — the most
            // read part of the panel — fell back to Microsoft Sans Serif 8.25pt regardless of
            // the Font set on the grid itself. Setting the grid's Font alone is not enough:
            // DataGridViewCellStyle resolves its own font independently.
            grid.DefaultCellStyle.Font = Body;
            grid.DefaultCellStyle.BackColor = Surface;
            grid.DefaultCellStyle.ForeColor = TextPrimary;
            grid.DefaultCellStyle.SelectionBackColor = GridSelection;
            grid.DefaultCellStyle.SelectionForeColor = TextPrimary;
            grid.DefaultCellStyle.Padding = new Padding(Space1, Space1 / 2, Space1, Space1 / 2);

            // 24 px was tight enough that descenders touched the gridlines at 9.75pt.
            grid.RowTemplate.Height = Px(30);
        }

        private Control BuildSettingsPanel()
        {
            const int toggleStrip = 38;
            var container = new Panel
            {
                Dock = DockStyle.Top, BackColor = BgDark, Height = toggleStrip,
            };

            btnSettingsToggle = CreateButton("Server & API key");
            btnSettingsToggle.Size = new Size(150, 28);
            btnSettingsToggle.Location = new Point(0, Space1);

            pnlSettings = Card();
            pnlSettings.Dock = DockStyle.None;
            pnlSettings.Location = new Point(0, toggleStrip);
            pnlSettings.Height = 122;
            pnlSettings.Visible = false;

            btnSettingsToggle.Click += (s, e) =>
            {
                pnlSettings.Visible = !pnlSettings.Visible;
                container.Height = pnlSettings.Visible
                    ? Px(toggleStrip) + pnlSettings.Height + Px(Space2)
                    : Px(toggleStrip);
            };

            var urlLabel = new Label
            {
                Text = "Server", ForeColor = TextMuted, Font = Small,
                AutoSize = true, Location = new Point(CardInset, 10),
            };
            Panel urlWrapper = CreateRoundedTextBox("", out txtBaseUrl);
            urlWrapper.Location = new Point(CardInset, 30);
            urlWrapper.Size = new Size(300, 28);

            var keyLabel = new Label
            {
                Text = "API key — optional, for unattended workstations",
                ForeColor = TextMuted, Font = Small,
                AutoSize = true, Location = new Point(CardInset, 64),
            };
            Panel keyWrapper = CreateRoundedTextBox("", out txtApiKey);
            keyWrapper.Location = new Point(CardInset, 84);
            keyWrapper.Size = new Size(200, 28);
            // A bearer credential; do not leave it legible on a clinical screen.
            txtApiKey.UseSystemPasswordChar = true;

            btnSaveSettings = CreateButton("Apply");
            btnSaveSettings.Size = new Size(80, 28);
            btnSaveSettings.Location = new Point(CardInset + 208, 84);

            pnlSettings.Controls.Add(urlLabel);
            pnlSettings.Controls.Add(urlWrapper);
            pnlSettings.Controls.Add(keyLabel);
            pnlSettings.Controls.Add(keyWrapper);
            pnlSettings.Controls.Add(btnSaveSettings);

            pnlSettings.Resize += (s, e) =>
            {
                int inset = Px(CardInset);
                urlWrapper.Width = Math.Max(120, pnlSettings.Width - inset * 2);
                keyWrapper.Width = Math.Max(
                    80, pnlSettings.Width - inset * 2 - btnSaveSettings.Width - Px(Space2));
                btnSaveSettings.Location =
                    new Point(keyWrapper.Right + Px(Space2), keyWrapper.Top);
            };

            container.Controls.Add(btnSettingsToggle);
            container.Controls.Add(pnlSettings);
            container.Resize += (s, e) => pnlSettings.Width = container.Width;

            return container;
        }

        private Control BuildStatusPanel()
        {
            lblStatus = new Label
            {
                Text = "",
                ForeColor = TextMuted,
                Font = Body,
                Dock = DockStyle.Top,
                Height = 40,
                Padding = new Padding(Space1 / 2, Space2, Space1 / 2, 0),
            };
            return lblStatus;
        }

        // ------------------------------------------------------------------------------- //
        //  Binding
        // ------------------------------------------------------------------------------- //

        private void BindViewModel()
        {
            _viewModel.PropertyChanged += OnViewModelPropertyChanged;

            txtPrompts.TextChanged += (s, e) => _viewModel.PromptsText = txtPrompts.Text;

            btnSignIn.Click += (s, e) => Fire(_viewModel.SignInAsync());
            btnSignOut.Click += (s, e) => _viewModel.SignOut();
            btnRun.Click += (s, e) => Fire(_viewModel.RunAsync());
            btnCancel.Click += (s, e) => _viewModel.Cancel();
            btnImport.Click += (s, e) => ImportClicked();

            btnSaveSettings.Click += (s, e) =>
            {
                if (!string.IsNullOrWhiteSpace(txtBaseUrl.Text))
                    _viewModel.ApplyBaseUrl(txtBaseUrl.Text);

                if (!string.IsNullOrWhiteSpace(txtApiKey.Text))
                {
                    Fire(_viewModel.ApplyApiKeyAsync(txtApiKey.Text));
                    // Do not keep the plaintext in a control that lives as long as the window.
                    txtApiKey.Clear();
                }
            };
        }

        /// <summary>
        /// Runs a ViewModel task without making the handler <c>async void</c>. v1's three
        /// handlers were <c>async void</c>, so anything escaping their catch blocks would have
        /// torn down the entire Eclipse process instead of showing an error.
        /// </summary>
        private void Fire(Task task)
        {
            task.ContinueWith(t =>
            {
                Exception ex = t.Exception != null ? t.Exception.GetBaseException() : null;
                if (ex == null) return;

                if (InvokeRequired) BeginInvoke(new Action(() => ShowFatal(ex)));
                else ShowFatal(ex);
            }, TaskContinuationOptions.OnlyOnFaulted);
        }

        private void ShowFatal(Exception ex)
        {
            lblStatus.ForeColor = ErrorRed;
            lblStatus.Text = "Unexpected error: " + ex.Message;
        }

        private void ImportClicked()
        {
            List<StructurePlan> plans = _viewModel.Plans;
            if (plans == null) return;

            // Read the operator's edits back out of the grid before anything is written.
            for (int i = 0; i < gridResults.Rows.Count && i < plans.Count; i++)
            {
                DataGridViewRow row = gridResults.Rows[i];

                plans[i].Selected = Convert.ToBoolean(row.Cells["Import"].Value ?? false);

                string id = Convert.ToString(row.Cells["StructureId"].Value ?? "");
                if (!string.IsNullOrWhiteSpace(id))
                    plans[i].StructureId = id.Trim();

                string type = Convert.ToString(row.Cells["DicomType"].Value ?? "");
                if (!string.IsNullOrWhiteSpace(type))
                    plans[i].DicomType = type;
            }

            int count = 0;
            foreach (StructurePlan p in plans) if (p.Selected) count++;

            if (count == 0)
            {
                lblStatus.ForeColor = WarnAmber;
                lblStatus.Text = "Nothing ticked.";
                return;
            }

            // One explicit confirmation before the point of no return for the patient's
            // structure set — the operator may well have been away while the job ran.
            DialogResult answer = MessageBox.Show(
                string.Format(
                    "Write {0} structure(s) into '{1}'?\n\n" +
                    "An existing structure with the same Id will have its contours replaced on " +
                    "the affected slices. Nothing is saved until you save in Eclipse.",
                    count, _viewModel.StructureSetInfo),
                "VoxTell — confirm import",
                MessageBoxButtons.OKCancel, MessageBoxIcon.Warning);

            if (answer != DialogResult.OK) return;

            _viewModel.ImportSelected();
            ShowImportOutcome();
        }

        /// <summary>
        /// Puts the importer's per-structure result and warnings in front of the operator.
        ///
        /// These matter clinically — "could not clear slice 42 before writing" means a structure
        /// now has two boundaries on that slice — and computing them only to drop them is exactly
        /// the mistake v1 made with its health label.
        /// </summary>
        private void ShowImportOutcome()
        {
            List<string> summary = _viewModel.ImportSummary;
            List<string> warnings = _viewModel.ImportWarnings;

            // Write what was actually done back into each row, so the grid stops describing
            // intentions and starts describing outcomes.
            //
            // Matched by structure Id, NOT by row index: the summary has one entry per *ticked*
            // plan while the grid has one row per result, so the two lists are the same length
            // only when everything was ticked.
            if (summary != null)
            {
                foreach (DataGridViewRow row in gridResults.Rows)
                {
                    string id = Convert.ToString(row.Cells["StructureId"].Value ?? "");
                    if (string.IsNullOrEmpty(id)) continue;

                    string line = summary.FirstOrDefault(
                        s => s.StartsWith(id + ":", StringComparison.Ordinal));

                    if (line != null)
                        row.Cells["Note"].Value = line.Substring(id.Length + 1).Trim();
                }
            }

            if (warnings == null || warnings.Count == 0) return;

            MessageBox.Show(
                string.Join(Environment.NewLine + Environment.NewLine, warnings.ToArray()),
                string.Format("VoxTell — {0} import warning(s)", warnings.Count),
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }

        private void OnViewModelPropertyChanged(object sender, PropertyChangedEventArgs e)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action(() => OnViewModelPropertyChanged(sender, e)));
                return;
            }

            switch (e.PropertyName)
            {
                case "Plans":
                    PopulateGrid();
                    break;
                case "Progress":
                    _progressValue = _viewModel.Progress;
                    progressBarHost.Invalidate();
                    break;
                case "CurrentSignInPrompt":
                    SyncSignInPrompt();
                    break;
                default:
                    SyncFromViewModel();
                    break;
            }

            UpdateEnabledState();
        }

        private void SyncFromViewModel()
        {
            lblAccount.Text = string.IsNullOrEmpty(_viewModel.AccountName)
                ? "Not signed in"
                : _viewModel.AccountName;
            lblAccount.ForeColor = _viewModel.IsSignedIn ? TextPrimary : TextMuted;

            lblQuota.Text = _viewModel.QuotaInfo ?? "";
            lblImageInfo.Text = _viewModel.ImageInfo;
            lblRescale.Text = _viewModel.RescaleInfo;
            lblServerMessage.Text = _viewModel.ServerMessage;

            if (txtBaseUrl.Text != _viewModel.BaseUrl && !txtBaseUrl.Focused)
                txtBaseUrl.Text = _viewModel.BaseUrl;

            // v1 blanked this label unconditionally, throwing away every error it had computed.
            lblStatus.Text = _viewModel.Status;
            lblStatus.ForeColor = StatusColour(_viewModel.Status);

            btnSignIn.Visible = !_viewModel.IsSignedIn;
            btnSignOut.Visible = _viewModel.IsSignedIn;

            SyncSignInPrompt();
        }

        private void SyncSignInPrompt()
        {
            SignInPrompt prompt = _viewModel.CurrentSignInPrompt;

            bool showCode = prompt != null && !string.IsNullOrEmpty(prompt.UserCode);
            pnlSignInPrompt.Visible = showCode;
            if (!showCode) return;

            lblSignInMessage.Text = prompt.Message ?? "";
            lnkVerification.Text = prompt.VerificationUri ?? "";
            txtUserCode.Text = prompt.UserCode;
        }

        private static Color StatusColour(string status)
        {
            if (string.IsNullOrEmpty(status)) return TextMuted;

            if (status.StartsWith("Imported", StringComparison.OrdinalIgnoreCase))
                return SuccessGreen;

            if (status.IndexOf("failed", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("could not", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("expired", StringComparison.OrdinalIgnoreCase) >= 0 ||
                status.IndexOf("error", StringComparison.OrdinalIgnoreCase) >= 0)
                return ErrorRed;

            if (status.StartsWith("Warning", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("Cancel", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("Nothing", StringComparison.OrdinalIgnoreCase))
                return WarnAmber;

            return TextMuted;
        }

        private void PopulateGrid()
        {
            gridResults.Rows.Clear();

            List<StructurePlan> plans = _viewModel.Plans;
            if (plans == null) return;

            foreach (StructurePlan p in plans)
            {
                string detail = p.ContourCount == 0
                    ? "nothing"
                    : string.Format("{0:N0} voxels, slices {1}-{2}",
                        p.VoxelCount, p.FirstSlice, p.LastSlice);

                int index = gridResults.Rows.Add(
                    p.Selected, p.Prompt, p.StructureId, p.DicomType, detail, p.Note ?? "");

                DataGridViewRow row = gridResults.Rows[index];

                if (p.ContourCount == 0)
                {
                    row.DefaultCellStyle.ForeColor = TextMuted;
                }
                else if (p.WillCreate)
                {
                    row.Cells["Note"].Style.ForeColor = SuccessGreen;
                    if (string.IsNullOrEmpty(p.Note))
                        row.Cells["Note"].Value = "New structure.";
                }
                else
                {
                    // Amber, because this one overwrites contours that are already there.
                    row.Cells["Note"].Style.ForeColor = WarnAmber;
                }
            }
        }

        private void UpdateEnabledState()
        {
            btnRun.Enabled = _viewModel.CanRun;
            btnCancel.Enabled = _viewModel.CanCancel;
            btnSignIn.Enabled = !_viewModel.IsBusy;
            btnImport.Enabled = _viewModel.Phase == WorkflowPhase.Reviewing
                                && gridResults.Rows.Count > 0;
        }

        // ------------------------------------------------------------------------------- //
        //  Chrome
        // ------------------------------------------------------------------------------- //

        private static Panel Card()
        {
            var panel = new Panel
            {
                Dock = DockStyle.Top,
                BackColor = Surface,
                Margin = new Padding(0, 0, 0, 8),
            };
            panel.Paint += (s, e) =>
            {
                var p = (Panel)s;
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (GraphicsPath path = CreateRoundedRectPath(
                           new Rectangle(0, 0, p.Width - 1, p.Height - 1), 6))
                using (var brush = new SolidBrush(Surface))
                {
                    e.Graphics.FillPath(brush, path);
                }
            };
            return panel;
        }

        /// <summary>
        /// A card carrying a section title and a hairline beneath it. Content below the rule
        /// starts at <see cref="UiTheme.ContentTop"/>, so every panel shares one vertical
        /// rhythm instead of each choosing its own offsets.
        /// </summary>
        private Panel TitledCard(string title)
        {
            Panel card = Card();

            card.Controls.Add(new Label
            {
                Text = title,
                ForeColor = TextLabel,
                Font = Section,
                AutoSize = true,
                Location = new Point(CardInset, TitleTop),
            });

            var rule = new Panel
            {
                BackColor = Divider,
                Location = new Point(CardInset, DividerTop),
                Size = new Size(200, 1),
            };
            card.Controls.Add(rule);
            card.Resize += (s, e) =>
                rule.Width = Math.Max(0, card.Width - Px(CardInset) * 2);

            return card;
        }

        private Panel CreateRoundedTextBox(string text, out TextBox textBox)
        {
            var wrapper = new Panel { BackColor = Surface, Padding = new Padding(8, 4, 8, 4) };
            textBox = new TextBox
            {
                Text = text,
                BorderStyle = BorderStyle.None,
                BackColor = InputBg,
                ForeColor = TextPrimary,
                Font = Body,
                Dock = DockStyle.Fill,
            };
            wrapper.Controls.Add(textBox);
            wrapper.Paint += RoundedTextBoxWrapper_Paint;
            return wrapper;
        }

        private void RoundedTextBoxWrapper_Paint(object sender, PaintEventArgs e)
        {
            var panel = (Panel)sender;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (GraphicsPath path = CreateRoundedRectPath(
                       new Rectangle(0, 0, panel.Width - 1, panel.Height - 1), 4))
            using (var brush = new SolidBrush(InputBg))
            {
                e.Graphics.FillPath(brush, path);
            }
        }

        private Button CreateButton(string text)
        {
            var button = new Button
            {
                Text = text,
                FlatStyle = FlatStyle.Flat,
                ForeColor = TextPrimary,
                BackColor = InputBg,
                Font = Body,
                Cursor = Cursors.Hand,
                UseVisualStyleBackColor = false,
            };
            button.FlatAppearance.BorderSize = 0;
            button.FlatAppearance.MouseOverBackColor = Color.Transparent;
            button.FlatAppearance.MouseDownBackColor = Color.Transparent;

            _buttonHover[button] = false;
            _buttonPressed[button] = false;

            button.MouseEnter += (s, e) => { _buttonHover[button] = true; button.Invalidate(); };
            button.MouseLeave += (s, e) => { _buttonHover[button] = false; button.Invalidate(); };
            button.MouseDown += (s, e) => { _buttonPressed[button] = true; button.Invalidate(); };
            button.MouseUp += (s, e) => { _buttonPressed[button] = false; button.Invalidate(); };
            button.Paint += RoundedButton_Paint;

            return button;
        }

        private void RoundedButton_Paint(object sender, PaintEventArgs e)
        {
            var button = (Button)sender;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            e.Graphics.Clear(Surface);

            bool hover = _buttonHover.ContainsKey(button) && _buttonHover[button];
            bool pressed = _buttonPressed.ContainsKey(button) && _buttonPressed[button];

            Color fill = button.Enabled
                ? (pressed ? DarkenColor(InputBg, 0.85) : hover ? LightenColor(InputBg, 1.25) : InputBg)
                : ButtonDisabledBg;

            using (GraphicsPath path = CreateRoundedRectPath(
                       new Rectangle(0, 0, button.Width - 1, button.Height - 1), 5))
            using (var brush = new SolidBrush(fill))
            {
                e.Graphics.FillPath(brush, path);
            }

            // GDI, not GDI+. Graphics.DrawString anti-aliases in greyscale and looked washed
            // out next to every other control on screen, all of which draw through GDI.
            // TextRenderer uses GDI and picks up ClearType, which is what actually made the
            // button labels look sharp.
            DrawCentredText(
                e.Graphics, button.Text, button.Font,
                button.Enabled ? TextPrimary : TextMuted, button.ClientRectangle);
        }

        private static GraphicsPath CreateRoundedRectPath(Rectangle rect, int radius)
        {
            var path = new GraphicsPath();
            int d = radius * 2;
            if (rect.Width <= d || rect.Height <= d)
            {
                path.AddRectangle(rect);
                return path;
            }
            path.AddArc(rect.X, rect.Y, d, d, 180, 90);
            path.AddArc(rect.Right - d, rect.Y, d, d, 270, 90);
            path.AddArc(rect.Right - d, rect.Bottom - d, d, d, 0, 90);
            path.AddArc(rect.X, rect.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        private static Color DarkenColor(Color c, double factor)
        {
            return Color.FromArgb(c.A,
                (int)Math.Max(0, c.R * factor),
                (int)Math.Max(0, c.G * factor),
                (int)Math.Max(0, c.B * factor));
        }

        private static Color LightenColor(Color c, double factor)
        {
            return Color.FromArgb(c.A,
                (int)Math.Min(255, c.R * factor),
                (int)Math.Min(255, c.G * factor),
                (int)Math.Min(255, c.B * factor));
        }

        private void ProgressBarHost_Paint(object sender, PaintEventArgs e)
        {
            var panel = (Panel)sender;
            e.Graphics.Clear(InputBg);
            if (_progressValue > 0)
            {
                int fillWidth = (int)(panel.Width * Math.Max(0, Math.Min(1, _progressValue)));
                using (var brush = new SolidBrush(Accent))
                {
                    e.Graphics.FillRectangle(brush, 0, 0, fillWidth, panel.Height);
                }
            }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(BgDark);
            base.OnPaint(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && _viewModel != null)
            {
                _viewModel.PropertyChanged -= OnViewModelPropertyChanged;
                _viewModel.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
