namespace VoxTell_Interface.ViewModels
{
    /// <summary>
    /// Which step of the workflow the UI should be showing.
    ///
    /// Extracted from <c>MainViewModel.cs</c> so that the view — and the preview harness that
    /// renders it without Eclipse — can reason about the workflow without dragging in the Varian
    /// assemblies that file needs.
    /// </summary>
    public enum WorkflowPhase
    {
        /// <summary>No credential yet.</summary>
        SignInRequired,

        /// <summary>Signed in, waiting for prompts.</summary>
        Ready,

        /// <summary>Encoding and uploading the volume.</summary>
        Uploading,

        /// <summary>Submitted; the server is queueing or segmenting.</summary>
        Working,

        /// <summary>Results are in and awaiting the operator's review.</summary>
        Reviewing,

        /// <summary>Structures written into the structure set.</summary>
        Imported,
    }
}
