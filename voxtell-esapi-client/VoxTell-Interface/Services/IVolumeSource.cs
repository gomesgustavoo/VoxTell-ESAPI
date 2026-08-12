namespace VoxTell_Interface.Services
{
    /// <summary>
    /// The image geometry and voxels the wire format needs, with no Eclipse types in sight.
    ///
    /// This is the seam that keeps the upload path testable. Everything below it — the
    /// encoder, the API client, the auth stack — compiles and runs without ESAPI, so the
    /// console harness can exercise the whole protocol against a synthetic volume instead of
    /// needing Eclipse, a patient, a CT and script approval. <see cref="EsapiVolumeSource"/>
    /// is the only implementation that touches <c>VMS.TPS.Common.Model.API</c>.
    /// </summary>
    public interface IVolumeSource
    {
        /// <summary>Columns. ESAPI <c>Image.XSize</c>.</summary>
        int XSize { get; }

        /// <summary>Rows. ESAPI <c>Image.YSize</c>.</summary>
        int YSize { get; }

        /// <summary>Slices. ESAPI <c>Image.ZSize</c>.</summary>
        int ZSize { get; }

        /// <summary>mm per column. ESAPI <c>Image.XRes</c>.</summary>
        double XRes { get; }

        /// <summary>mm per row. ESAPI <c>Image.YRes</c>.</summary>
        double YRes { get; }

        /// <summary>mm per slice. ESAPI <c>Image.ZRes</c>.</summary>
        double ZRes { get; }

        /// <summary>LPS mm of the centre of voxel (0,0,0). ESAPI <c>Image.Origin</c>.</summary>
        double[] Origin { get; }

        /// <summary>Unit direction of increasing x. ESAPI <c>Image.XDirection</c>.</summary>
        double[] RowDirection { get; }

        /// <summary>Unit direction of increasing y. ESAPI <c>Image.YDirection</c>.</summary>
        double[] ColumnDirection { get; }

        /// <summary>Unit direction of increasing z. ESAPI <c>Image.ZDirection</c>.</summary>
        double[] SliceDirection { get; }

        /// <summary>
        /// Linear rescale from stored voxel values to the display unit (HU for CT), such that
        /// <c>display = stored * ScalingSlope + ScalingIntercept</c>. Sent to the server, which
        /// applies it once — the client never rescales 100 MB of voxels itself.
        /// </summary>
        double ScalingSlope { get; }

        /// <summary>See <see cref="ScalingSlope"/>. A CT typically reports about -1024.</summary>
        double ScalingIntercept { get; }

        /// <summary>
        /// Copies slice <paramref name="z"/> into <paramref name="dest"/> as int16, row-major
        /// with y outer and x inner — i.e. <c>dest[y * XSize + x]</c>, which is exactly the
        /// innermost-x ordering the <c>(Z, Y, X)</c> C-order wire format wants.
        /// <paramref name="dest"/> must be at least <c>XSize * YSize</c> long and is reused
        /// across slices, so implementations must not retain it.
        /// </summary>
        void ReadSlice(int z, short[] dest);

        /// <summary>
        /// How many voxels so far did not fit in an int16 and were clamped. Stays 0 for CT and
        /// for any modality whose stored values fit; a non-zero count means the volume the
        /// server segments is not quite the volume Eclipse holds, so the UI must say so.
        /// </summary>
        long ClampedVoxelCount { get; }
    }
}
