using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using VoxTell_Interface.ViewModels;

namespace VoxTell_Interface.Views
{
    /// <summary>
    /// The four-step rail: Series, Segment, Review, VR.
    ///
    /// Renders <see cref="MainViewState.BuildSteps"/>, which decides the interesting
    /// questions — that Review is unreachable without rows, and that the workflow's phases
    /// are a state machine rather than a scroll position.
    ///
    /// Informational, with exactly one exception: **VR is a link.** It carries no step
    /// number in <c>BuildSteps</c> because it is a destination rather than a stage, and a
    /// destination that cannot be gone to is just a word. It is enabled only once the
    /// server has given us a comparison URL.
    /// </summary>
    internal sealed class StepRail
    {
        private readonly StackPanel _root;
        private readonly System.Action _onVr;

        private readonly List<KeyValuePair<StepKey, TextBlock>> _labels =
            new List<KeyValuePair<StepKey, TextBlock>>();
        private readonly List<KeyValuePair<StepKey, TextBlock>> _numbers =
            new List<KeyValuePair<StepKey, TextBlock>>();

        private Button _vr;
        private bool _vrEnabled;

        public StepRail(System.Action onVr = null)
        {
            _onVr = onVr;
            _root = Ui.Row(0);
        }

        public FrameworkElement Element { get { return _root; } }

        /// <summary>Enable the VR link. Off until <c>BaselineWebUrl</c> exists.</summary>
        public void SetVrEnabled(bool enabled)
        {
            _vrEnabled = enabled;
            if (_vr == null) return;
            _vr.IsEnabled = enabled;
            _vr.ToolTip = enabled
                ? "Open the comparison in the dashboard"
                : "Available once a job has been written and a QA baseline recorded";
            var caption = _vr.Content as TextBlock;
            if (caption != null) caption.Foreground = enabled ? Theme.Steel : Theme.InkFaint;
        }

        public void Render(WorkflowPhase phase, bool isSignedIn, bool hasRows)
        {
            IList<StepState> steps = MainViewState.BuildSteps(phase, isSignedIn, hasRows);

            if (_root.Children.Count == 0) Build(steps);

            foreach (StepState step in steps)
            {
                // Three weights rather than a colour per state: current is full ink, done or
                // available is muted, out of reach is faint. The rail must never compete
                // with a structure swatch.
                Brush brush = step.IsCurrent
                    ? Theme.Ink
                    : (step.IsReachable || step.IsComplete ? Theme.InkMuted : Theme.InkFaint);

                // The marker is written on every render, not only when complete: setting it
                // once left a "done" tick standing after the next job reset the phase.
                string marker = step.IsComplete ? "✓" : (step.Number ?? "·");

                Apply(_numbers, step.Key, brush, marker);
                Apply(_labels, step.Key, brush, null);
            }

            SetVrEnabled(_vrEnabled);
        }

        private void Build(IList<StepState> steps)
        {
            _root.Children.Clear();
            _labels.Clear();
            _numbers.Clear();

            foreach (StepState step in steps)
            {
                if (step.Key == StepKey.Connect)
                {
                    _vr = Controls.Ghost("VR ↗", (s, e) => { if (_onVr != null) _onVr(); });
                    _root.Append(_vr, Theme.Space3);
                    SetVrEnabled(_vrEnabled);
                    continue;
                }

                TextBlock number = Ui.Micro(step.Number ?? "·");
                TextBlock label = Ui.Small(step.Label);

                _numbers.Add(new KeyValuePair<StepKey, TextBlock>(step.Key, number));
                _labels.Add(new KeyValuePair<StepKey, TextBlock>(step.Key, label));

                _root.Append(Ui.Row(Theme.Space1, number, label), Theme.Space3);
            }
        }

        private static void Apply(
            List<KeyValuePair<StepKey, TextBlock>> targets,
            StepKey key, Brush brush, string marker)
        {
            foreach (var entry in targets)
            {
                if (!Equals(entry.Key, key)) continue;
                entry.Value.Foreground = brush;
                if (marker != null) entry.Value.Text = marker;
            }
        }
    }
}
