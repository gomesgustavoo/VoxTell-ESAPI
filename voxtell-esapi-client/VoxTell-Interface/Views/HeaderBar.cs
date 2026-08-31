using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The panel's one header: what this is, where you are, and who you are — in two rows.
    ///
    /// What it replaces
    /// ----------------
    /// An uncontained step rail floating above a card whose entire content was the
    /// operator's email address at 20 px and a Sign-out button. The account line was the
    /// largest text on a screen whose job is to show what will be written into a patient,
    /// and nothing named the product, the VR destination or the two-run workflow.
    ///
    ///     VoxTell VR - segment, then verify                    gustavo@rt...  v
    ///     * Series   * Segment   3 Review   VR ^      13/200 jobs   RUN 1 OF 2
    ///
    /// The account shrinks to a 12 px chip whose menu absorbs sign-out and the server /
    /// API-key pane, so two permanently visible controls and a card leave the layout.
    ///
    /// It is a full-bleed <see cref="Border"/> with a bottom hairline rather than a rounded
    /// card, and it sits outside the scrolling region: a header that scrolls away takes
    /// "which patient am I in" with it.
    /// </summary>
    internal sealed class HeaderBar
    {
        private readonly Action _onSignIn;
        private readonly Action _onSignOut;
        private readonly Action _onSettings;

        private readonly Border _root;
        private readonly StepRail _rail;
        private readonly Grid _accountSlot;
        private readonly Button _signIn;
        private readonly TextBlock _quota;
        private readonly Border _runPill;
        private readonly TextBlock _runText;

        private string _accountShown;
        private bool _accountBuilt;
        private Button _accountChip;

        public HeaderBar(
            Action onSignIn, Action onSignOut, Action onSettings, Action onOpenVr)
        {
            _onSignIn = onSignIn;
            _onSignOut = onSignOut;
            _onSettings = onSettings;

            _rail = new StepRail(onOpenVr);

            TextBlock mark = Ui.Text("VoxTell VR");
            mark.FontFamily = Theme.UiSemiboldFamily;
            mark.FontWeight = Theme.SemiboldWeight;
            mark.FontSize = Theme.SizeWordmark;

            TextBlock tagline = Ui.Small("segment, then verify").Fg(Theme.InkMuted);
            tagline.ToolTip =
                "Run 1 segments and records a QA baseline. Run 2 reads the edited "
                + "structures back and reports what changed.";

            _signIn = Controls.Primary("Sign in", (s, e) => _onSignIn());
            _accountSlot = Ui.Grid("Auto", "Auto");
            _accountSlot.HorizontalAlignment = HorizontalAlignment.Right;

            _quota = Ui.Micro(string.Empty).Fg(Theme.InkFaint);
            _runText = Ui.Micro("RUN 1 OF 2").Fg(Theme.InkMuted);
            _runText.FontFamily = Theme.UiSemiboldFamily;
            _runText.FontWeight = Theme.SemiboldWeight;
            _runPill = Controls.Pill(string.Empty);
            _runPill.Child = _runText;

            var top = Ui.Grid("Auto", "Auto,Auto,*,Auto",
                mark.At(0, 0),
                Ui.Micro("·").Fg(Theme.InkFaint).At(0, 1).Gap(Theme.Space2, 0, Theme.Space2, 0),
                tagline.At(0, 2),
                _accountSlot.At(0, 3));

            var bottom = Ui.Grid("Auto", "Auto,*,Auto,Auto",
                _rail.Element.At(0, 0),
                _quota.At(0, 2).Gap(0, 0, Theme.Space2, 0),
                _runPill.At(0, 3));
            bottom.Margin = new Thickness(0, Theme.Space2, 0, 0);

            _root = new Border
            {
                Background = Theme.Panel,
                BorderBrush = Theme.Edge,
                BorderThickness = new Thickness(0, 0, 0, 1),
                Padding = new Thickness(Theme.CardInset, 10, Theme.CardInset, 10),
                SnapsToDevicePixels = true,
                Child = Ui.Stack(0, top, bottom),
            };
        }

        public FrameworkElement Element { get { return _root; } }

        public void Render(
            WorkflowPhase phase,
            bool signedIn,
            bool hasRows,
            string account,
            string quota,
            bool canRecordBaseline,
            bool vrAvailable,
            bool busy)
        {
            // The chip is rebuilt only when the address changes: its menu is a real
            // ContextMenu, and re-creating one per render would also re-create the menu
            // under a pointer that may be inside it.
            // `_accountBuilt` is not redundant. The first render of a signed-out panel
            // arrives with both `shown` and `_accountShown` null, and a bare comparison
            // reads that as "already built" — so the slot stayed empty and there was no way
            // to sign in at all. The same null-equals-null shape once left the review list
            // unbound.
            string shown = signedIn ? (account ?? "Signed in") : null;
            if (!_accountBuilt || !string.Equals(shown, _accountShown, StringComparison.Ordinal))
            {
                _accountBuilt = true;
                _accountShown = shown;
                _accountSlot.Children.Clear();
                if (shown == null)
                {
                    _accountSlot.Children.Add(_signIn);
                }
                else
                {
                    _accountChip = Chip(shown);
                    _accountSlot.Children.Add(_accountChip);
                }
            }

            _signIn.IsEnabled = !busy;

            _quota.Text = quota ?? string.Empty;
            _quota.Show(!string.IsNullOrEmpty(quota));

            // Says which half of the two-run workflow this is, and says plainly when
            // nothing will be recorded. A clinic that believes it is collecting QA data
            // and is not has the worst of both.
            _runText.Text = canRecordBaseline ? "RUN 1 OF 2" : "RUN 1 · NO QA";
            _runText.Foreground = canRecordBaseline ? Theme.InkMuted : Theme.Warn;
            _runPill.ToolTip = canRecordBaseline
                ? "Writing structures also records a QA baseline for this series, so a later "
                  + "run can report what was edited."
                : "No QA baseline can be recorded for this series, so no comparison will be "
                  + "available later.";

            // Nothing runs while signed out, so the run badge would be describing a job
            // that cannot be submitted.
            _runPill.Show(signedIn);

            _rail.SetVrEnabled(vrAvailable);
            _rail.Render(phase, signedIn, hasRows);
        }

        private Button Chip(string address)
        {
            string caption = Shorten(address) + "  ▾";

            var entries = new List<Controls.Entry>
            {
                new Controls.Entry(address, null),
                new Controls.Entry("Server & API key…", _onSettings),
                new Controls.Entry("Sign out", _onSignOut),
            };

            Button chip = Controls.MenuButton(caption, entries);
            chip.ToolTip = address;
            var text = chip.Content as TextBlock;
            if (text != null) text.Foreground = Theme.InkMuted;
            return chip;
        }

        /// <summary>
        /// Keep the local part and the domain's first label: an address is recognised by
        /// its local part, and a chip that says "gustavo.formento@rtmedical..." tells the
        /// operator nothing a shorter one would not. The full address is in the tooltip
        /// and in the menu.
        /// </summary>
        private static string Shorten(string address, int limit = 22)
        {
            if (string.IsNullOrEmpty(address) || address.Length <= limit) return address;

            int at = address.IndexOf('@');
            if (at > 0)
            {
                string local = address.Substring(0, at);
                string domain = address.Substring(at + 1);
                int dot = domain.IndexOf('.');
                string head = dot > 0 ? domain.Substring(0, dot) : domain;

                string candidate = local + "@" + head;
                if (candidate.Length <= limit) return candidate;

                // The local part alone, not a truncated address: "gustavo.formento" is
                // recognisable, "gustavo.formento@rtme…" is only longer.
                if (local.Length <= limit) return local;
                return local.Substring(0, limit - 1) + "…";
            }
            return address.Substring(0, limit - 1) + "…";
        }
    }
}
