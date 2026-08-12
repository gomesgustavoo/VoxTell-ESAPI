using System;
using VMS.TPS.Common.Model.API;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// The only place voxels and image geometry are read out of Eclipse.
    ///
    /// Everything here must run on the ESAPI thread, which <see cref="EsapiGate"/> enforces
    /// rather than trusts. Keeping it in one small class means the rest of the upload path is
    /// ordinary C# — and means an Eclipse version change has exactly one file to review.
    /// </summary>
    public sealed class EsapiVolumeSource : IVolumeSource
    {
        private readonly Image _image;
        private readonly IThreadGate _gate;

        // GetVoxels wants int[XSize, YSize]. Allocated once: v1 allocated a fresh 1 MB array
        // per slice, which for a 300-slice study was 300 MB of pure GC churn on the UI thread.
        private readonly int[,] _buffer;

        private long _clamped;

        public EsapiVolumeSource(Image image, IThreadGate gate)
        {
            if (image == null) throw new ArgumentNullException("image");
            if (gate == null) throw new ArgumentNullException("gate");

            _gate = gate;
            _gate.AssertOnEsapiThread("Reading image geometry");

            _image = image;
            XSize = image.XSize;
            YSize = image.YSize;
            ZSize = image.ZSize;
            XRes = image.XRes;
            YRes = image.YRes;
            ZRes = image.ZRes;

            // Image.Origin and the direction cosines are in the DICOM patient frame (LPS mm),
            // which is also the frame Structure.AddContourOnImagePlane expects. Neither this
            // client nor the server ever converts to Eclipse's user coordinates, so the affine
            // we send is the affine the returned contour points come back in. Do not introduce
            // DicomToUser/UserToDicom on one side of that round trip only.
            Origin = new[] { image.Origin.x, image.Origin.y, image.Origin.z };
            RowDirection = new[] { image.XDirection.x, image.XDirection.y, image.XDirection.z };
            ColumnDirection = new[] { image.YDirection.x, image.YDirection.y, image.YDirection.z };
            SliceDirection = new[] { image.ZDirection.x, image.ZDirection.y, image.ZDirection.z };

            // GetVoxels returns *stored* values, not Hounsfield units. VoxelToDisplayValue is
            // the linear rescale to the display unit, so two probes recover its coefficients.
            // This matters beyond tidiness: the server's crop_to_nonzero thresholds at exactly
            // 0, and in HU air is about -1000 (crop keeps the body) while in stored values air
            // is often 0 (crop lands somewhere else). v1 sent stored values with no rescale
            // metadata at all, so the model saw the wrong intensities.
            double atZero = image.VoxelToDisplayValue(0);
            double atOne = image.VoxelToDisplayValue(1);
            ScalingIntercept = atZero;
            ScalingSlope = atOne - atZero;

            // A degenerate slope would silently flatten the volume server-side. Fall back to
            // the identity, which is what the server assumes when the fields are omitted.
            if (ScalingSlope == 0.0 || double.IsNaN(ScalingSlope) || double.IsInfinity(ScalingSlope))
            {
                ScalingSlope = 1.0;
                ScalingIntercept = 0.0;
            }

            _buffer = new int[XSize, YSize];
        }

        public int XSize { get; private set; }
        public int YSize { get; private set; }
        public int ZSize { get; private set; }
        public double XRes { get; private set; }
        public double YRes { get; private set; }
        public double ZRes { get; private set; }
        public double[] Origin { get; private set; }
        public double[] RowDirection { get; private set; }
        public double[] ColumnDirection { get; private set; }
        public double[] SliceDirection { get; private set; }
        public double ScalingSlope { get; private set; }
        public double ScalingIntercept { get; private set; }

        public long ClampedVoxelCount { get { return _clamped; } }

        public void ReadSlice(int z, short[] dest)
        {
            _gate.AssertOnEsapiThread("Image.GetVoxels");

            if (z < 0 || z >= ZSize)
                throw new ArgumentOutOfRangeException("z", z, "Slice index is outside the image.");
            if (dest == null) throw new ArgumentNullException("dest");
            if (dest.Length < XSize * YSize)
                throw new ArgumentException("Destination buffer is too small for one slice.", "dest");

            _image.GetVoxels(z, _buffer);

            // GetVoxels indexes [x, y]; the wire format wants y outer and x inner. This
            // transpose is the same one v1 did, just narrowing to int16 instead of int32.
            int i = 0;
            for (int y = 0; y < YSize; y++)
            {
                for (int x = 0; x < XSize; x++)
                {
                    int v = _buffer[x, y];

                    // Narrow deliberately rather than letting a cast wrap around: a wrapped
                    // value turns dense bone into vacuum and would segment plausibly wrong.
                    // CT and MR stored values fit in int16; count anything that does not so
                    // the UI can warn instead of the operator trusting a distorted volume.
                    if (v > short.MaxValue) { v = short.MaxValue; _clamped++; }
                    else if (v < short.MinValue) { v = short.MinValue; _clamped++; }

                    dest[i++] = (short)v;
                }
            }
        }
    }
}
