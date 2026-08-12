using System;
using System.Collections.Generic;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.ViewModels
{
    /// <summary>
    /// Everything the view needs to draw itself, in one immutable-by-convention object.
    ///
    /// Two problems it solves.
    ///
    /// The first is testability. <c>MainViewModel</c> cannot be *compiled* into a project without
    /// the Varian assemblies — it names <c>ScriptContext</c> in its constructor — so as long as
    /// the view read state directly off the ViewModel, the view could only ever be rendered
    /// inside Eclipse. A snapshot breaks that: the view is a pure function of this type, and the
    /// preview harness fabricates instances of it. No mocking, no fake HTTP layer, and — because
    /// the real ViewModel is never constructed in the preview — no need to reproduce its
    /// ESAPI-thread contract outside Eclipse.
    ///
    /// The second is the update path. The WinForms view re-read nine labels on *every*
    /// <c>PropertyChanged</c>, including every progress tick during an upload, and derived the
    /// status colour by string-matching in the view. Here the ViewModel hands over one object and
    /// the view rebinds to it wholesale; every derivation the view would otherwise need a
    /// converter for is precomputed below.
    ///
    /// Deliberately free of WPF types: <see cref="Severity"/> is an enum the view maps to a brush,
    /// not a brush. That keeps this file linkable into a plain test project later.
    /// </summary>
    public sealed class MainViewState
    {
        public WorkflowPhase Phase;

        public bool IsBusy;
        public bool CanRun;
        public bool CanCancel;
        public bool IsSignedIn;

        public string AccountName;
        public string QuotaInfo;
        public string BaseUrl;

        public string ImageInfo;
        public string RescaleInfo;
        public string StructureSetInfo;

        public string Status;
        public StatusSeverity Severity;

        /// <summary>The server's own message, shown verbatim — it distinguishes slow from stuck.</summary>
        public string ServerMessage;

        /// <summary>0..1 across the whole encode → upload → segment → download sequence.</summary>
        public double Progress;

        /// <summary>Jobs ahead of this one, when the server reports a queue position.</summary>
        public int? QueuePosition;

        // Flattened rather than carrying the SignInPrompt object: that type lives in
        // Services/Auth, which drags in the HTTP client, Newtonsoft and the token store. Three
        // strings keep the preview's dependency list to a handful of small files.
        public string SignInMessage;
        public string SignInVerificationUri;
        public string SignInUserCode;

        /// <summary>
        /// The ViewModel's LIVE plan list, by reference — not a copy.
        ///
        /// This is what lets the review rows bind two-way straight onto the model, so the
        /// operator's edits are already in the objects <c>ImportSelected</c> reads. The WinForms
        /// view instead scraped the grid and paired rows to plans by index, which silently wrote
        /// prompt A's contours under prompt B's name the moment anyone sorted the grid.
        /// </summary>
        public IList<StructurePlan> Rows;

        public IList<string> ImportSummary;
        public IList<string> ImportWarnings;

        /// <summary>The step rail, derived from <see cref="Phase"/> in exactly one place.</summary>
        public IList<StepState> Steps;

        /// <summary>True only for the device-code fallback; the PKCE path needs no instructions.</summary>
        public bool ShowSignInCard
        {
            get { return !string.IsNullOrEmpty(SignInUserCode); }
        }

        public bool ShowQueueBadge
        {
            get { return QueuePosition.HasValue; }
        }

        /// <summary>
        /// Builds the step rail.
        ///
        /// Reachability is the whole point. In the WinForms view <c>Phase</c> was consulted in
        /// exactly one place — whether the Import button was enabled — so <c>Uploading</c>,
        /// <c>Working</c> and <c>Imported</c> looked identical to <c>Ready</c>. Here the phase
        /// decides which steps the operator can reach, and an unreachable step renders as
        /// unreachable rather than as a live control that does nothing.
        /// </summary>
        public static IList<StepState> BuildSteps(
            WorkflowPhase phase, bool isSignedIn, bool hasRows)
        {
            bool imported = phase == WorkflowPhase.Imported;
            bool reviewing = phase == WorkflowPhase.Reviewing || imported;
            bool working = phase == WorkflowPhase.Working;
            bool uploading = phase == WorkflowPhase.Uploading;

            var steps = new List<StepState>();

            steps.Add(new StepState
            {
                Key = StepKey.Series,
                Number = "1",
                Label = "Series",
                // Reachable as soon as there is a credential: the operator may want to look at
                // the image facts before doing anything else.
                IsReachable = isSignedIn,
                IsCurrent = uploading || (isSignedIn && !reviewing && !working),
                IsComplete = reviewing || working,
            });

            steps.Add(new StepState
            {
                Key = StepKey.Segment,
                Number = "2",
                Label = "Segment",
                IsReachable = isSignedIn,
                IsCurrent = working,
                IsComplete = reviewing,
            });

            steps.Add(new StepState
            {
                Key = StepKey.Review,
                Number = "3",
                Label = "Review",
                // Never reachable without rows: an empty review step is a dead end that invites
                // the operator to look for results that are not there.
                IsReachable = hasRows,
                IsCurrent = reviewing,
                IsComplete = imported,
            });

            steps.Add(new StepState
            {
                Key = StepKey.Connect,
                Number = null,
                Label = "VR",
                IsReachable = isSignedIn,
                IsCurrent = false,
                IsComplete = false,
            });

            return steps;
        }
    }

    /// <summary>
    /// How a status line should read, decided by the ViewModel rather than by the view.
    ///
    /// The WinForms view inferred this by matching substrings of the status text ("failed",
    /// "could not", "expired"), which meant the ViewModel could change a message and silently
    /// change its colour. The heuristic still exists as a default, but it lives next to the
    /// messages now, and any site that knows better can state the severity outright.
    /// </summary>
    public enum StatusSeverity
    {
        Neutral,
        Working,
        Success,
        Warning,
        Error,
    }

    /// <summary>Which step a rail entry represents, so the view need not match on the label.</summary>
    public enum StepKey
    {
        Series,
        Segment,
        Review,
        Connect,
    }

    /// <summary>One entry in the step rail.</summary>
    public sealed class StepState
    {
        public StepKey Key;

        /// <summary>"1", "2", "3" — or null for a step that is not part of the sequence.</summary>
        public string Number;

        public string Label;

        /// <summary>The step the workflow is on now.</summary>
        public bool IsCurrent;

        /// <summary>Clickable. An unreachable step also leaves the tab order.</summary>
        public bool IsReachable;

        /// <summary>Already been through; shown as done rather than pending.</summary>
        public bool IsComplete;
    }
}
