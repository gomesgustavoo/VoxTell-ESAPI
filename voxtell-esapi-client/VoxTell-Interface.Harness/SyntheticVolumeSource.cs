using System;
using VoxTell_Interface.Services;

namespace VoxTell_Interface.Harness
{
    /// <summary>
    /// A phantom volume with the same shape as a real CT, for exercising the wire path without
    /// Eclipse.
    ///
    /// Values are deliberately in *stored* units, exactly as <c>Image.GetVoxels</c> returns
    /// them, with <see cref="ScalingSlope"/>/<see cref="ScalingIntercept"/> describing the
    /// rescale to HU. That is the whole point: it reproduces the condition the HU fix addresses,
    /// where air is 0 in stored values and about -1000 once rescaled.
    /// </summary>
    public sealed class SyntheticVolumeSource : IVolumeSource
    {
        // Stored values chosen so the HU they map to are physically sensible:
        //   air         0 -> -1024 HU
        //   soft tissue 1064 ->  40 HU
        //   bone        2024 -> 1000 HU
        private const short StoredAir = 0;
        private const short StoredSoftTissue = 1064;
        private const short StoredBone = 2024;

        public SyntheticVolumeSource(int xSize, int ySize, int zSize)
        {
            XSize = xSize;
            YSize = ySize;
            ZSize = zSize;

            XRes = 1.5;
            YRes = 1.5;
            ZRes = 2.5;

            // Head-first supine identity orientation, and an origin that puts the phantom's
            // centre near the patient origin — the usual arrangement, so nothing about the
            // affine is degenerate in a way that would hide a sign error.
            Origin = new double[]
            {
                -0.5 * (xSize - 1) * XRes,
                -0.5 * (ySize - 1) * YRes,
                -0.5 * (zSize - 1) * ZRes,
            };
            RowDirection = new double[] { 1, 0, 0 };
            ColumnDirection = new double[] { 0, 1, 0 };
            SliceDirection = new double[] { 0, 0, 1 };

            ScalingSlope = 1.0;
            ScalingIntercept = -1024.0;
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

        public long ClampedVoxelCount { get { return 0; } }

        public void ReadSlice(int z, short[] dest)
        {
            // A soft-tissue ellipsoid with a denser sphere inside it. Not anatomy — the model
            // will very likely segment nothing from this, which is fine and expected: the
            // harness verifies the protocol, not the segmentation. What matters is that the
            // volume has a body-shaped non-zero region so the server's crop_to_nonzero has
            // something plausible to find.
            double cx = (XSize - 1) / 2.0;
            double cy = (YSize - 1) / 2.0;
            double cz = (ZSize - 1) / 2.0;

            double bodyRx = XSize * 0.38, bodyRy = YSize * 0.28, bodyRz = ZSize * 0.45;
            double boneR = Math.Min(XSize, YSize) * 0.08;

            int i = 0;
            for (int y = 0; y < YSize; y++)
            {
                for (int x = 0; x < XSize; x++)
                {
                    double dx = (x - cx) / bodyRx;
                    double dy = (y - cy) / bodyRy;
                    double dz = (z - cz) / bodyRz;

                    short value = StoredAir;
                    if (dx * dx + dy * dy + dz * dz <= 1.0)
                    {
                        value = StoredSoftTissue;

                        double bx = x - cx, by = y - cy, bz = (z - cz) * (ZRes / XRes);
                        if (bx * bx + by * by + bz * bz <= boneR * boneR)
                            value = StoredBone;
                    }

                    dest[i++] = value;
                }
            }
        }
    }
}
