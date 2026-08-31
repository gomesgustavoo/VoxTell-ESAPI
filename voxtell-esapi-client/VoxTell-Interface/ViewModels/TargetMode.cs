namespace VoxTell_Interface.ViewModels
{
    /// <summary>
    /// How this job names what to segment.
    ///
    /// Not cosmetic: the two are mutually exclusive on the wire, because a model
    /// addressed by catalog structure ids derives its own model set and a request
    /// naming both a model and structure ids could contradict itself. The server
    /// enforces the exclusivity; this enum is how the panel stays on the right
    /// side of it.
    /// </summary>
    public enum TargetMode
    {
        /// <summary>Free text for a prompt model. What the plugin has always done.</summary>
        Prompts,

        /// <summary>Catalog structure ids, ticked one at a time or from auto-detect.</summary>
        Structures,

        /// <summary>
        /// A clinic protocol: the same structure ids as <see cref="Structures"/>, plus the
        /// naming the clinic uses — write-as id, DICOM type and colour per structure.
        ///
        /// On the wire it is indistinguishable from <see cref="Structures"/>; the
        /// difference is entirely in what the review rows come out named.
        /// </summary>
        Protocol,
    }
}
