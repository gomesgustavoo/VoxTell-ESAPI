using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Windows.Forms;
using VMS.TPS.Common.Model.API;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// WinForms user control for VoxTell AI Segmentation Interface.
    /// Hosted inside Eclipse's WPF window via WindowsFormsHost.
    /// Dark flat theme, compact layout.
    /// </summary>
    public class MainControl : UserControl
    {
        // Color palette
        private static readonly Color BgDark = Color.FromArgb(30, 30, 30);
        private static readonly Color Surface = Color.FromArgb(45, 45, 45);
        private static readonly Color InputBg = Color.FromArgb(55, 55, 55);
        private static readonly Color Accent = Color.FromArgb(79, 195, 247);
        private static readonly Color TextPrimary = Color.FromArgb(240, 240, 240);
        private static readonly Color TextMuted = Color.FromArgb(140, 140, 140);
        private static readonly Color TextLabel = Color.FromArgb(210, 210, 210);
        private static readonly Color SuccessGreen = Color.FromArgb(102, 187, 106);
        private static readonly Color ErrorRed = Color.FromArgb(239, 83, 80);

        private readonly MainViewModel _viewModel;

        // Controls
        private TextBox txtBackendUrl;
        private Button btnCheckHealth;
        private Panel pnlHealthDot;
        private Label lblHealthText;
        private Label lblImageInfo;
        private Button btnStartSession;
        private Panel progressBarHost;
        private Label lblUploadStatus;
        private TextBox txtPrompts;
        private Button btnRunInference;
        private Button btnCancel;
        private TextBox txtResults;
        private Label lblStatus;

        // Progress tracking
        private double _progressValue;

        // Button hover/press tracking for owner-drawn rounded buttons
        private readonly Dictionary<Button, bool> _buttonHover = new Dictionary<Button, bool>();
        private readonly Dictionary<Button, bool> _buttonPressed = new Dictionary<Button, bool>();

        public MainControl(ScriptContext context)
        {
            InitializeComponents();
            _viewModel = new MainViewModel(context, this);
            BindViewModel();
        }

        private void InitializeComponents()
        {
            this.SetStyle(
                ControlStyles.OptimizedDoubleBuffer |
                ControlStyles.AllPaintingInWmPaint |
                ControlStyles.UserPaint, true);
            this.UpdateStyles();

            this.AutoScaleMode = AutoScaleMode.Dpi;
            this.Dock = DockStyle.Fill;
            this.BackColor = BgDark;
            this.ForeColor = TextPrimary;
            this.Font = new Font("Segoe UI", 9.75f, FontStyle.Regular, GraphicsUnit.Point);

            var main = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                Padding = new Padding(12),
                RowCount = 6,
                ColumnCount = 1,
                BackColor = BgDark
            };
            main.RowStyles.Add(new RowStyle(SizeType.AutoSize));  // header
            main.RowStyles.Add(new RowStyle(SizeType.AutoSize));  // backend
            main.RowStyles.Add(new RowStyle(SizeType.AutoSize));  // upload
            main.RowStyles.Add(new RowStyle(SizeType.AutoSize));  // prompts + inference
            main.RowStyles.Add(new RowStyle(SizeType.Percent, 100));  // results
            main.RowStyles.Add(new RowStyle(SizeType.AutoSize));  // status

            // === Row 0: Header ===
            lblImageInfo = new Label
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                ForeColor = TextMuted,
                Font = new Font("Consolas", 9f),
                Padding = new Padding(0, 0, 0, 6)
            };
            main.Controls.Add(lblImageInfo, 0, 0);

            // === Row 1: Backend row ===
            var backendPanel = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                ColumnCount = 4,
                RowCount = 1,
                Margin = new Padding(0, 0, 0, 8),
                BackColor = BgDark
            };
            backendPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            backendPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            backendPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            backendPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            var txtBackendUrlWrapper = CreateRoundedTextBox("http://localhost:8000", out txtBackendUrl);
            txtBackendUrlWrapper.Dock = DockStyle.Fill;
            backendPanel.Controls.Add(txtBackendUrlWrapper, 0, 0);

            btnCheckHealth = CreateButton("Connect");
            btnCheckHealth.Margin = new Padding(6, 0, 6, 0);
            backendPanel.Controls.Add(btnCheckHealth, 1, 0);

            pnlHealthDot = new Panel
            {
                Width = 10,
                Height = 10,
                Margin = new Padding(0, 8, 4, 0),
                BackColor = Color.Transparent
            };
            pnlHealthDot.Paint += HealthDot_Paint;
            backendPanel.Controls.Add(pnlHealthDot, 2, 0);

            lblHealthText = new Label
            {
                Text = "",
                AutoSize = true,
                ForeColor = TextPrimary,
                Font = new Font("Segoe UI", 8.5f),
                Margin = new Padding(0, 5, 0, 0)
            };
            backendPanel.Controls.Add(lblHealthText, 3, 0);

            main.Controls.Add(backendPanel, 0, 1);

            // === Row 2: Upload row ===
            var uploadPanel = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                ColumnCount = 2,
                RowCount = 2,
                Margin = new Padding(0, 0, 0, 8),
                BackColor = BgDark
            };
            uploadPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            uploadPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

            btnStartSession = CreateButton("Upload");
            btnStartSession.Margin = new Padding(0, 0, 8, 0);
            uploadPanel.Controls.Add(btnStartSession, 0, 0);

            // Slim 4px progress bar (custom-drawn)
            progressBarHost = new Panel
            {
                Dock = DockStyle.Fill,
                Height = 4,
                Margin = new Padding(0, 10, 0, 10),
                BackColor = InputBg
            };
            progressBarHost.Paint += ProgressBarHost_Paint;
            uploadPanel.Controls.Add(progressBarHost, 1, 0);

            lblUploadStatus = new Label
            {
                Text = "",
                AutoSize = true,
                ForeColor = TextPrimary,
                Font = new Font("Consolas", 8.5f),
                Margin = new Padding(0, 2, 0, 0),
                Dock = DockStyle.Right
            };
            uploadPanel.Controls.Add(lblUploadStatus, 1, 1);

            main.Controls.Add(uploadPanel, 0, 2);

            // === Row 3: Prompts + Inference ===
            var inferencePanel = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                ColumnCount = 1,
                RowCount = 3,
                Margin = new Padding(0, 0, 0, 8),
                BackColor = BgDark
            };

            var lblPrompts = new Label
            {
                Text = "Prompts (comma-separated):",
                AutoSize = true,
                ForeColor = TextLabel,
                Font = new Font("Segoe UI", 9.75f, FontStyle.Bold),
                Margin = new Padding(0, 0, 0, 4)
            };
            inferencePanel.Controls.Add(lblPrompts, 0, 0);

            var txtPromptsWrapper = CreateRoundedTextBox("", out txtPrompts);
            txtPrompts.Multiline = false;
            txtPrompts.ScrollBars = ScrollBars.None;
            txtPromptsWrapper.Dock = DockStyle.Top;
            inferencePanel.Controls.Add(txtPromptsWrapper, 0, 1);

            var infBtnPanel = new FlowLayoutPanel
            {
                AutoSize = true,
                Margin = new Padding(0, 6, 0, 0),
                BackColor = BgDark
            };
            btnRunInference = CreateButton("Run Inference");
            btnRunInference.Margin = new Padding(0, 0, 6, 0);
            btnCancel = CreateButton("Cancel");
            btnCancel.BackColor = Surface;
            btnCancel.FlatAppearance.MouseOverBackColor = Surface;
            btnCancel.FlatAppearance.MouseDownBackColor = Surface;
            btnCancel.Enabled = false;
            btnCancel.Margin = new Padding(0, 0, 10, 0);
            infBtnPanel.Controls.AddRange(new Control[] { btnRunInference, btnCancel });
            inferencePanel.Controls.Add(infBtnPanel, 0, 2);

            main.Controls.Add(inferencePanel, 0, 3);

            // === Row 4: Results ===
            var resultsLabel = new Label
            {
                Text = "Results:",
                AutoSize = true,
                ForeColor = TextLabel,
                Font = new Font("Segoe UI", 9.75f, FontStyle.Bold),
                Margin = new Padding(0, 0, 0, 2)
            };

            var resultsContainer = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                RowCount = 2,
                ColumnCount = 1,
                BackColor = BgDark
            };
            resultsContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            resultsContainer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            txtResults = new TextBox
            {
                Dock = DockStyle.Fill,
                ReadOnly = true,
                Multiline = true,
                ScrollBars = ScrollBars.None,
                Font = new Font("Consolas", 9f),
                BackColor = InputBg,
                ForeColor = TextPrimary,
                BorderStyle = BorderStyle.None
            };
            var txtResultsWrapper = new Panel
            {
                BackColor = InputBg,
                Padding = new Padding(6, 4, 6, 4),
                Dock = DockStyle.Fill
            };
            txtResultsWrapper.Paint += RoundedTextBoxWrapper_Paint;
            txtResultsWrapper.Click += (s, e) => txtResults.Focus();
            txtResultsWrapper.Controls.Add(txtResults);

            resultsContainer.Controls.Add(resultsLabel, 0, 0);
            resultsContainer.Controls.Add(txtResultsWrapper, 0, 1);

            main.Controls.Add(resultsContainer, 0, 4);

            // === Row 5: Status text ===
            lblStatus = new Label
            {
                Text = "Ready.",
                Dock = DockStyle.Bottom,
                AutoSize = true,
                ForeColor = TextMuted,
                Font = new Font("Segoe UI", 8.5f),
                Padding = new Padding(0, 2, 0, 0)
            };
            main.Controls.Add(lblStatus, 0, 5);

            this.Controls.Add(main);

            // Wire up events
            btnCheckHealth.Click += (s, e) => _viewModel.CheckHealthAsync();
            btnStartSession.Click += (s, e) => _viewModel.StartSessionAndUploadAsync();
            btnRunInference.Click += (s, e) => _viewModel.RunInferenceAsync();
            btnCancel.Click += (s, e) => _viewModel.Cancel();
            txtBackendUrl.TextChanged += (s, e) => _viewModel.BackendUrl = txtBackendUrl.Text;
            txtPrompts.TextChanged += (s, e) => _viewModel.PromptsText = txtPrompts.Text;
        }

        private Panel CreateRoundedTextBox(string text, out TextBox textBox)
        {
            textBox = new TextBox
            {
                Text = text,
                BackColor = InputBg,
                ForeColor = TextPrimary,
                BorderStyle = BorderStyle.None,
                Font = new Font("Segoe UI", 9.75f),
                Dock = DockStyle.Fill
            };

            int wrapperHeight = textBox.PreferredHeight + 8;
            var wrapper = new Panel
            {
                BackColor = InputBg,
                Padding = new Padding(6, 4, 6, 4),
                Dock = DockStyle.Top,
                Height = wrapperHeight
            };
            var tb = textBox;
            wrapper.Paint += RoundedTextBoxWrapper_Paint;
            wrapper.Click += (s, e) => tb.Focus();
            wrapper.Controls.Add(textBox);

            return wrapper;
        }

        private void RoundedTextBoxWrapper_Paint(object sender, PaintEventArgs e)
        {
            var panel = (Panel)sender;
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;

            Color parentBg = panel.Parent != null ? panel.Parent.BackColor : BgDark;
            g.Clear(parentBg);

            using (var path = CreateRoundedRectPath(new Rectangle(0, 0, panel.Width, panel.Height), 8))
            using (var fillBrush = new SolidBrush(InputBg))
            {
                g.FillPath(fillBrush, path);
            }

            using (var path = CreateRoundedRectPath(new Rectangle(0, 0, panel.Width, panel.Height), 8))
            using (var borderPen = new Pen(Color.FromArgb(70, 70, 70), 1f))
            {
                g.DrawPath(borderPen, path);
            }
        }

        private Button CreateButton(string text)
        {
            var btn = new Button
            {
                Text = text,
                AutoSize = true,
                FlatStyle = FlatStyle.Flat,
                BackColor = Accent,
                ForeColor = Color.White,
                Font = new Font("Segoe UI", 9.75f),
                Cursor = Cursors.Hand,
                Padding = new Padding(8, 2, 8, 2)
            };
            btn.FlatAppearance.BorderSize = 0;
            btn.FlatAppearance.MouseOverBackColor = btn.BackColor;
            btn.FlatAppearance.MouseDownBackColor = btn.BackColor;

            btn.Paint += RoundedButton_Paint;

            btn.MouseEnter += (s, e) => { _buttonHover[btn] = true; btn.Invalidate(); };
            btn.MouseLeave += (s, e) => { _buttonHover[btn] = false; _buttonPressed[btn] = false; btn.Invalidate(); };
            btn.MouseDown += (s, e) => { _buttonPressed[btn] = true; btn.Invalidate(); };
            btn.MouseUp += (s, e) => { _buttonPressed[btn] = false; btn.Invalidate(); };

            _buttonHover[btn] = false;
            _buttonPressed[btn] = false;

            return btn;
        }

        private static GraphicsPath CreateRoundedRectPath(Rectangle rect, int radius)
        {
            var path = new GraphicsPath();
            int d = radius * 2;
            var r = new Rectangle(rect.X, rect.Y, rect.Width - 1, rect.Height - 1);

            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        private void RoundedButton_Paint(object sender, PaintEventArgs e)
        {
            var btn = (Button)sender;
            var g = e.Graphics;

            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;

            Color parentBg = btn.Parent != null ? btn.Parent.BackColor : BgDark;
            g.Clear(parentBg);

            Color fillColor;
            Color textColor = btn.ForeColor;

            if (!btn.Enabled)
            {
                fillColor = Color.FromArgb(60, 60, 60);
                textColor = TextMuted;
            }
            else if (_buttonPressed.ContainsKey(btn) && _buttonPressed[btn])
            {
                fillColor = DarkenColor(btn.BackColor, 0.8);
            }
            else if (_buttonHover.ContainsKey(btn) && _buttonHover[btn])
            {
                fillColor = LightenColor(btn.BackColor, 1.15);
            }
            else
            {
                fillColor = btn.BackColor;
            }

            using (var path = CreateRoundedRectPath(new Rectangle(0, 0, btn.Width, btn.Height), 8))
            using (var brush = new SolidBrush(fillColor))
            {
                g.FillPath(brush, path);
            }

            var textRect = new Rectangle(
                btn.Padding.Left, btn.Padding.Top,
                btn.Width - btn.Padding.Horizontal,
                btn.Height - btn.Padding.Vertical);

            TextRenderer.DrawText(
                g, btn.Text, btn.Font, textRect, textColor,
                TextFormatFlags.HorizontalCenter |
                TextFormatFlags.VerticalCenter |
                TextFormatFlags.SingleLine |
                TextFormatFlags.NoPrefix);
        }

        private static Color DarkenColor(Color c, double factor)
        {
            return Color.FromArgb(c.A,
                (int)Math.Max(0, Math.Min(255, c.R * factor)),
                (int)Math.Max(0, Math.Min(255, c.G * factor)),
                (int)Math.Max(0, Math.Min(255, c.B * factor)));
        }

        private static Color LightenColor(Color c, double factor)
        {
            return Color.FromArgb(c.A,
                (int)Math.Max(0, Math.Min(255, c.R * factor)),
                (int)Math.Max(0, Math.Min(255, c.G * factor)),
                (int)Math.Max(0, Math.Min(255, c.B * factor)));
        }

        private void HealthDot_Paint(object sender, PaintEventArgs e)
        {
            var panel = (Panel)sender;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color parentBg = panel.Parent != null ? panel.Parent.BackColor : BgDark;
            e.Graphics.Clear(parentBg);
            Color dotColor = _viewModel != null && _viewModel.IsHealthy ? SuccessGreen : TextMuted;
            using (var brush = new SolidBrush(dotColor))
                e.Graphics.FillEllipse(brush, 0, 0, panel.Width - 1, panel.Height - 1);
        }

        private void ProgressBarHost_Paint(object sender, PaintEventArgs e)
        {
            var panel = (Panel)sender;
            e.Graphics.Clear(InputBg);
            if (_progressValue > 0)
            {
                int fillWidth = (int)(panel.Width * _progressValue);
                if (fillWidth > 0)
                {
                    using (var brush = new SolidBrush(Accent))
                        e.Graphics.FillRectangle(brush, 0, 0, fillWidth, panel.Height);
                }
            }
        }

        private void BindViewModel()
        {
            lblImageInfo.Text = _viewModel.ImageInfo;
            txtBackendUrl.Text = _viewModel.BackendUrl;
            _viewModel.PropertyChanged += OnViewModelPropertyChanged;
            UpdateButtonStates();
        }

        private void OnViewModelPropertyChanged(object sender, PropertyChangedEventArgs e)
        {
            if (InvokeRequired)
            {
                Invoke(new Action(() => OnViewModelPropertyChanged(sender, e)));
                return;
            }

            switch (e.PropertyName)
            {
                case "HealthStatus":
                case "IsHealthy":
                    lblHealthText.Text = "";
                    pnlHealthDot.Invalidate();
                    UpdateButtonStates();
                    break;
                case "UploadProgress":
                    _progressValue = _viewModel.UploadProgress;
                    progressBarHost.Invalidate();
                    break;
                case "UploadStatusText":
                    lblUploadStatus.Text = _viewModel.UploadStatusText;
                    break;
                case "ResultsDisplay":
                    txtResults.Text = _viewModel.ResultsDisplay;
                    break;
                case "StatusMessage":
                    lblStatus.Text = _viewModel.StatusMessage;
                    break;
                case "IsBusy":
                    btnCancel.Enabled = _viewModel.IsBusy;
                    UpdateButtonStates();
                    break;
                case "CanStartSession":
                case "CanRunInference":
                case "CanCheckHealth":
                    UpdateButtonStates();
                    break;
            }
        }

        private void UpdateButtonStates()
        {
            btnCheckHealth.Enabled = _viewModel.CanCheckHealth;
            btnStartSession.Enabled = _viewModel.CanStartSession;
            btnRunInference.Enabled = _viewModel.CanRunInference;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
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
