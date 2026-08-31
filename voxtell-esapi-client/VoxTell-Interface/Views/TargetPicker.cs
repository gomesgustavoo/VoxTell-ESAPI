using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// What to segment: a clinic protocol, individual catalog structures, or free text.
    ///
    /// The shape follows one decision: the plugin holds no hard-coded list of models,
    /// structures or protocols. Everything here is built from what <c>GET /v1/models</c>
    /// returned, so adding a CADS task or changing a clinic's naming is a server deployment
    /// rather than a DLL that has to be re-approved on every workstation. There is
    /// deliberately no <c>case "cads_556"</c> anywhere in this file.
    ///
    /// Why protocol first
    /// ------------------
    /// A preset only ever said *what* to segment, so the write-as id, the DICOM type and
    /// the colour were typed per row on every run. Naming drift is the failure the
    /// published audits of this workflow blame for silently losing cases, and a protocol is
    /// where a physicist fixes it once for everyone. Auto-detect still wins when the series
    /// already has contours — a selection derived from this patient beats a template, and
    /// it is what makes comparison the default rather than an extra step.
    /// </summary>
    internal sealed class TargetPicker
    {
        private const int TabProtocol = 0;
        private const int TabStructures = 1;
        private const int TabPrompts = 2;

        private readonly MainViewModel _vm;
        private readonly Action _onChanged;
        private readonly Action _reload;

        private readonly Grid _root;
        private readonly Segments _tabs;

        // Protocol pane
        private readonly Border _protocolPane;
        private readonly Sel _protocolSelect;
        private readonly Grid _entries;
        private readonly TextBlock _protocolNote;
        private readonly Button _addContoured;
        private string _entriesFor;
        private object _protocolOptionsFrom;

        // Structures pane
        private readonly Border _structuresPane;
        private readonly PickList _list;
        private readonly WrapPanel _chips;
        private readonly TextBlock _detected;
        private readonly Button _unmatchedButton;
        private readonly StackPanel _unmatchedPanel;
        private bool _showUnmatched;
        private bool _listBuilt;
        private string _chipsFor;

        // Prompts pane
        private readonly Border _promptsPane;
        private readonly TextBox _promptsBox;
        private readonly TextBlock _promptsHint;

        // Shared footer
        private readonly TextBlock _catalogProblem;
        private readonly Button _retry;
        private readonly TextBlock _selection;
        private readonly TextBlock _licence;

        public TargetPicker(MainViewModel vm, Action onChanged, Action reload)
        {
            _vm = vm;
            _onChanged = onChanged;
            _reload = reload;

            _tabs = Controls.Segmented(OnTab);
            _tabs.Add("Protocol");
            _tabs.Add("Structures");
            _tabs.Add("Prompts");

            // ---- protocol ------------------------------------------------------- //

            _protocolSelect = Controls.Select(
                new List<Controls.Option>(), null,
                value => { _vm.ApplyProtocol(value as string); Refresh(); Changed(); });
            _protocolSelect.Element.MinWidth = 260;

            _protocolNote = Ui.Small(string.Empty).Fg(Theme.InkMuted);
            _entries = new Grid();
            EntryColumns(_entries);

            _addContoured = Controls.Button("Add what is already on this series", (s, e) =>
            {
                StructureAutoDetect.Detection detection = _vm.Detection;
                if (detection == null) return;
                foreach (string id in detection.StructureIds) _vm.SetStructureSelected(id, true);
                Refresh();
                Changed();
            });

            // The entries scroll: head & neck is sixteen of them, and at the window's
            // minimum height a card that merely clips takes the button below it with no
            // scrollbar to say so.
            //
            // The button is INSIDE the scroller with the table rather than pinned under it.
            // Under it, the table's row had to stretch to fill the card, which left a band
            // of dead space between a short protocol and its button.
            _protocolPane = Ui.Card(Ui.Grid("Auto,*", "*",
                Ui.Row(Theme.Space2, _protocolSelect.Element, _protocolNote).At(0, 0),
                Controls.Scroll(Ui.Stack(Theme.Space3, _entries, _addContoured.Left()))
                    .MinH(120).At(1, 0).Gap(0, Theme.Space2, 0, 0)));

            // ---- structures ----------------------------------------------------- //

            _list = new PickList(
                id => _vm.IsStructureSelected(id),
                (id, on) => { _vm.SetStructureSelected(id, on); RefreshChips(force: true); Changed(); });

            TextBox searchBox;
            Border search = Controls.Search(out searchBox, "Filter structures", text =>
            {
                _list.Filter(text);
            });

            _detected = Ui.Small(string.Empty).Fg(Theme.InkMuted);
            _unmatchedButton = Controls.Button("Show unrecognised", (s, e) =>
            {
                _showUnmatched = !_showUnmatched;
                Refresh();
            });

            _chips = Ui.Wrap();
            _unmatchedPanel = new StackPanel { Orientation = Orientation.Vertical };
            _unmatchedPanel.Show(false);

            var listArea = Ui.Grid("Auto,Auto,Auto,Auto,*", "*",
                search.At(0, 0),
                Ui.Wrap(_detected.Gap(0, Theme.Space1, Theme.Space2, Theme.Space1),
                        _unmatchedButton).At(1, 0),
                _unmatchedPanel.At(2, 0),
                _chips.At(3, 0),
                _list.Element.At(4, 0));

            _structuresPane = Ui.Card(listArea);

            // ---- prompts -------------------------------------------------------- //
            //
            // Two lines instead of six, and the instruction inside the box instead of on a
            // caption line above it: the pane used to spend about 140 px of an 860 px
            // window on a box holding two words.

            Border promptsInput = Ui.Input(out _promptsBox, multiline: true);
            _promptsBox.MinHeight = 40;
            _promptsBox.MaxHeight = 120;
            _promptsBox.TextChanged += (s, e) =>
            {
                _vm.PromptsText = _promptsBox.Text;
                _promptsHint.Show(string.IsNullOrEmpty(_promptsBox.Text));
                Refresh();
            };

            _promptsHint = Ui.Small("One structure per line, e.g. liver, left kidney")
                .Fg(Theme.InkFaint);
            _promptsHint.IsHitTestVisible = false;
            _promptsHint.VerticalAlignment = VerticalAlignment.Top;
            _promptsHint.Margin = Theme.InputPadding;

            var promptsStack = Ui.Grid("Auto", "*",
                promptsInput.At(0, 0),
                _promptsHint.At(0, 0));
            _promptsPane = Ui.Card(promptsStack);

            // ---- shared --------------------------------------------------------- //

            _catalogProblem = Ui.Small(
                "The model list could not be reached, so only free-text prompts are "
                + "available. Protocols and CADS need the server.")
                .Fg(Theme.Warn);
            _catalogProblem.TextWrapping = TextWrapping.Wrap;
            _catalogProblem.TextTrimming = TextTrimming.None;

            _retry = Controls.Button("Retry model list", (s, e) => _reload());

            _selection = Ui.Small(string.Empty).Fg(Theme.InkMuted);
            _licence = Ui.Micro(string.Empty).Fg(Theme.InkFaint);

            _root = Ui.Grid("Auto,Auto,Auto,*,Auto", "*",
                _tabs.Element.At(0, 0),
                _catalogProblem.At(1, 0).Gap(0, Theme.Space2, 0, 0),
                _retry.Left().At(2, 0).Gap(0, Theme.Space2, 0, 0),
                Panes().At(3, 0).Gap(0, Theme.Space2, 0, 0),
                Ui.Stack(0, _selection, _licence).At(4, 0).Gap(0, Theme.Space2, 0, 0));
        }

        private Grid Panes()
        {
            // All three panes live in the same cell and only one is visible, so switching
            // tabs cannot change the card's height by more than the panes differ.
            return Ui.Grid("*", "*",
                _protocolPane.At(0, 0),
                _structuresPane.At(0, 0),
                _promptsPane.At(0, 0));
        }

        public FrameworkElement Element { get { return _root; } }

        // ------------------------------------------------------------------------- //

        private void Changed()
        {
            if (_onChanged != null) _onChanged();
        }

        private void OnTab(int index)
        {
            _vm.Mode = index == TabPrompts
                ? TargetMode.Prompts
                : (index == TabProtocol ? TargetMode.Protocol : TargetMode.Structures);
            Refresh();
            Changed();
        }

        /// <summary>Re-read everything from the view model. Cheap; call freely.</summary>
        public void Refresh()
        {
            bool hasCatalog = _vm.HasCatalog;
            bool hasProtocols = hasCatalog && _vm.Catalog.HasProtocols;

            TargetMode mode = _vm.Mode;
            if (!hasCatalog && mode != TargetMode.Prompts)
            {
                // Nothing else can work without the catalog, and a pane that cannot work
                // must not be the one on screen.
                mode = TargetMode.Prompts;
                _vm.Mode = mode;
            }
            else if (mode == TargetMode.Protocol && !hasProtocols)
            {
                // A deployment can serve a catalog and no protocols. Leaving the panel on
                // the protocol pane would show an empty pane behind a disabled tab.
                mode = TargetMode.Structures;
                _vm.Mode = mode;
            }

            _tabs.SetEnabled(TabProtocol, hasProtocols,
                hasCatalog
                    ? "This deployment serves no protocols yet."
                    : "The model list could not be reached, so only prompts are available.");
            _tabs.SetEnabled(TabStructures, hasCatalog,
                "The model list could not be reached, so only prompts are available.");

            int tab = mode == TargetMode.Prompts
                ? TabPrompts
                : (mode == TargetMode.Protocol ? TabProtocol : TabStructures);
            _tabs.Select(tab, notify: false);

            _protocolPane.Show(mode == TargetMode.Protocol);
            _structuresPane.Show(mode == TargetMode.Structures);
            _promptsPane.Show(mode == TargetMode.Prompts);

            // Say why in the pane, not only in a tooltip nobody hovers: a dead tab is the
            // most confusing thing this panel can do, and with the server down it is the
            // first thing a planner meets.
            _catalogProblem.Show(!hasCatalog);
            _retry.Show(!hasCatalog);

            if (_promptsBox.Text != (_vm.PromptsText ?? string.Empty))
            {
                _promptsBox.Text = _vm.PromptsText ?? string.Empty;
            }
            _promptsHint.Show(string.IsNullOrEmpty(_promptsBox.Text));

            RefreshProtocols();
            RefreshStructures();

            _selection.Text = _vm.TargetSummary;

            IList<string> licences = _vm.CurrentLicences();
            _licence.Text = licences.Count == 0
                ? string.Empty
                : "Weights: " + string.Join(", ", licences.ToArray());
            _licence.Show(licences.Count > 0);
        }

        // --- protocol pane ------------------------------------------------------- //

        private static void EntryColumns(Grid grid)
        {
            grid.ColumnDefinitions.Add(new ColumnDefinition
                { Width = new GridLength(1.4, GridUnitType.Star), MinWidth = 110 });
            grid.ColumnDefinitions.Add(new ColumnDefinition
                { Width = new GridLength(1, GridUnitType.Star), MinWidth = 96 });
            grid.ColumnDefinitions.Add(new ColumnDefinition
                { Width = GridLength.Auto, MinWidth = 74 });
            grid.ColumnDefinitions.Add(new ColumnDefinition
                { Width = GridLength.Auto, MinWidth = 128 });
        }

        private void RefreshProtocols()
        {
            ModelCatalog catalog = _vm.Catalog;
            if (catalog == null || !catalog.HasProtocols)
            {
                _protocolNote.Text = string.Empty;
                return;
            }

            // Rebuild the option list only when the catalog changes; a ComboBox rebuilt on
            // the render path would close itself while the planner is choosing.
            if (!ReferenceEquals(_protocolOptionsFrom, catalog))
            {
                var options = new List<Controls.Option>();
                foreach (CatalogProtocol protocol in catalog.Protocols
                    .OrderBy(p => p.Site ?? string.Empty, StringComparer.OrdinalIgnoreCase)
                    .ThenBy(p => p.DisplayName ?? string.Empty, StringComparer.OrdinalIgnoreCase))
                {
                    string site = string.IsNullOrEmpty(protocol.Site)
                        ? string.Empty
                        : protocol.Site + "  ·  ";
                    // No count in the label: the note beside the box states how many are
                    // producible and how many are not, and two different numbers for one
                    // protocol reads as a contradiction.
                    options.Add(new Controls.Option(site + protocol.DisplayName, protocol.Key));
                }
                _protocolSelect.Reset(options, _vm.ProtocolKey);
                _protocolOptionsFrom = catalog;
            }
            else
            {
                _protocolSelect.Pick(_vm.ProtocolKey);
            }

            CatalogProtocol current = _vm.CurrentProtocol;
            StructureAutoDetect.Detection detection = _vm.Detection;
            int contoured = detection == null ? 0 : detection.StructureIds.Count;

            _addContoured.Show(contoured > 0);
            var caption = _addContoured.Content as TextBlock;
            if (caption != null)
            {
                caption.Text = "Also segment the " + contoured
                    + " already on this series, for comparison";
            }

            if (current == null)
            {
                _protocolNote.Text = "Choose a protocol.";
                _entries.Children.Clear();
                _entries.RowDefinitions.Clear();
                _entriesFor = null;
                return;
            }

            IList<ProtocolEntry> available;
            IList<ProtocolEntry> unavailable;
            _vm.Catalog.SplitEntries(current, out available, out unavailable);

            _protocolNote.Text = unavailable.Count == 0
                ? available.Count + " structures"
                : available.Count + " structures, " + unavailable.Count + " unavailable";
            _protocolNote.Foreground = unavailable.Count == 0 ? Theme.InkMuted : Theme.Warn;

            string signature = current.Key + "|" + contoured;
            if (!string.Equals(signature, _entriesFor, StringComparison.Ordinal))
            {
                BuildEntries(current, available, unavailable, detection);
                _entriesFor = signature;
            }
        }

        private void BuildEntries(
            CatalogProtocol protocol,
            IList<ProtocolEntry> available,
            IList<ProtocolEntry> unavailable,
            StructureAutoDetect.Detection detection)
        {
            _entries.Children.Clear();
            _entries.RowDefinitions.Clear();

            var onSeries = new HashSet<string>(
                detection == null ? new List<string>() : detection.StructureIds,
                StringComparer.Ordinal);

            int row = AddEntryRow(Theme.SizeMicro + 6);
            Put(Ui.Micro("STRUCTURE").Fg(Theme.InkMuted), row, 0);
            Put(Ui.Micro("WRITE AS").Fg(Theme.InkMuted), row, 1);
            Put(Ui.Micro("TYPE").Fg(Theme.InkMuted), row, 2);
            Put(Ui.Micro("").Fg(Theme.InkMuted), row, 3);

            foreach (ProtocolEntry entry in available)
            {
                CatalogStructure structure = _vm.Catalog.Structure(entry.StructureId);
                row = AddEntryRow(Theme.ListRowHeight);

                Put(Ui.Small(structure != null ? structure.DisplayName : entry.StructureId),
                    row, 0);

                string writeAs = entry.SafeWriteAs;
                TextBlock id = Ui.Small(writeAs ?? "?").Fg(writeAs == null ? Theme.Bad : Theme.Ink);
                if (writeAs == null)
                {
                    id.ToolTip = "The served id is empty or longer than Eclipse's 16 "
                        + "characters, so this row will fall back to the structure name.";
                }
                Put(id, row, 1);

                Put(Ui.Small(string.IsNullOrEmpty(entry.DicomType) ? "CONTROL" : entry.DicomType)
                    .Fg(Theme.InkMuted), row, 2);

                bool present = onSeries.Contains(entry.StructureId);
                Put(Ui.Small(present ? "on the series" : "will be segmented")
                    .Fg(present ? Theme.Ok : Theme.InkMuted).Right(), row, 3);
            }

            // Listed, not hidden. A protocol entry no model can produce is exactly the case
            // a clinic has to see: dropping it silently gives a run that looks complete.
            foreach (ProtocolEntry entry in unavailable)
            {
                row = AddEntryRow(Theme.ListRowHeight);
                Put(Ui.Small(entry.StructureId).Fg(Theme.InkFaint), row, 0);
                Put(Ui.Small(entry.SafeWriteAs ?? string.Empty).Fg(Theme.InkFaint), row, 1);
                Put(Ui.Small(entry.DicomType ?? string.Empty).Fg(Theme.InkFaint), row, 2);
                TextBlock why = Ui.Small("no model produces this").Fg(Theme.Warn).Right();
                why.ToolTip = "No model in this deployment's catalog produces "
                    + entry.StructureId + ".";
                Put(why, row, 3);
            }
        }

        private int AddEntryRow(double minHeight)
        {
            _entries.RowDefinitions.Add(
                new RowDefinition { Height = GridLength.Auto, MinHeight = minHeight });
            return _entries.RowDefinitions.Count - 1;
        }

        private void Put(FrameworkElement element, int row, int column)
        {
            Thickness m = element.Margin;
            element.Margin = new Thickness(
                m.Left, m.Top, m.Right + (column == 3 ? 0 : Theme.Space2), m.Bottom);
            Grid.SetRow(element, row);
            Grid.SetColumn(element, column);
            _entries.Children.Add(element);
        }

        // --- structures pane ----------------------------------------------------- //

        private void RefreshStructures()
        {
            ModelCatalog catalog = _vm.Catalog;
            if (catalog != null && !_listBuilt)
            {
                _list.Build(catalog.Grouped());
                _listBuilt = true;
            }
            _list.RefreshTicks();

            _detected.Text = _vm.AutoDetectSummary ?? "No structure set open.";

            StructureAutoDetect.Detection detection = _vm.Detection;
            int unmatched = detection == null ? 0 : detection.Unmatched.Count();
            _unmatchedButton.Show(unmatched > 0);
            var caption = _unmatchedButton.Content as TextBlock;
            if (caption != null)
            {
                caption.Text = _showUnmatched
                    ? "Hide unrecognised"
                    : "Show " + unmatched + " unrecognised";
            }

            RefreshUnmatched();
            RefreshChips(force: false);
        }

        /// <summary>
        /// The current selection as removable chips.
        ///
        /// Worth the space: the alternative is scrolling a 167-row list to find out what a
        /// run will actually ask for.
        /// </summary>
        private void RefreshChips(bool force)
        {
            IList<string> selected = _vm.SelectedStructureIds;
            string signature = string.Join("|", selected.ToArray());
            if (!force && string.Equals(signature, _chipsFor, StringComparison.Ordinal)) return;
            _chipsFor = signature;

            _chips.Children.Clear();
            if (selected.Count == 0)
            {
                _chips.AppendWrapped(
                    Ui.Small("Nothing selected.").Fg(Theme.InkFaint), Theme.Space1);
                return;
            }

            ModelCatalog catalog = _vm.Catalog;
            foreach (string id in selected)
            {
                string captured = id;
                CatalogStructure structure = catalog == null ? null : catalog.Structure(id);
                _chips.AppendWrapped(
                    Controls.Chip(
                        structure != null ? structure.DisplayName : id,
                        () =>
                        {
                            _vm.SetStructureSelected(captured, false);
                            RefreshChips(force: true);
                            _list.RefreshTicks();
                            Changed();
                        }),
                    Theme.Space1);
            }

            StructureAutoDetect.Detection detection = _vm.Detection;
            if (detection != null && detection.StructureIds.Count > 0)
            {
                _chips.AppendWrapped(Controls.Button("Already contoured ("
                    + detection.StructureIds.Count + ")", (s, e) =>
                {
                    _vm.SelectStructures(detection.StructureIds);
                    _list.RefreshTicks();
                    RefreshChips(force: true);
                    Changed();
                }), Theme.Space1);
            }

            _chips.AppendWrapped(Controls.Button("Clear", (s, e) =>
            {
                _vm.SelectStructures(new List<string>());
                _list.RefreshTicks();
                RefreshChips(force: true);
                Changed();
            }), Theme.Space1);
        }

        /// <summary>
        /// The names that matched nothing, listed on request.
        ///
        /// Worth the code: the published audit of this exact workflow lost cases to
        /// off-convention names its script skipped in silence, and the authors' first
        /// recommendation was standardised naming. A planner who can see the list can fix
        /// the template; one who cannot, cannot.
        /// </summary>
        private void RefreshUnmatched()
        {
            _unmatchedPanel.Show(_showUnmatched);
            if (!_showUnmatched) return;

            StructureAutoDetect.Detection detection = _vm.Detection;
            _unmatchedPanel.Children.Clear();
            if (detection == null) return;

            _unmatchedPanel.Children.Add(Ui.Micro(
                "NOT RECOGNISED - these are not compared, and no model is run for them")
                .Fg(Theme.Warn));

            foreach (StructureAutoDetect.Candidate candidate in detection.Unmatched)
            {
                _unmatchedPanel.Children.Add(
                    Ui.Small(candidate.ExistingId).Fg(Theme.InkFaint));
            }
        }
    }
}
