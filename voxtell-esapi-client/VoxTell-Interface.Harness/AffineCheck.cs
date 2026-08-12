using System;
using System.Collections.Generic;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Harness
{
    /// <summary>Outcome of projecting one structure's contours back onto the source grid.</summary>
    public class AffineCheckResult
    {
        public string Prompt { get; set; }
        public int ContourCount { get; set; }
        public int PointCount { get; set; }
        public long VoxelCount { get; set; }
        public int FirstSlice { get; set; }
        public int LastSlice { get; set; }

        /// <summary>Points that landed outside the voxel grid.</summary>
        public int OutOfBounds { get; set; }

        /// <summary>
        /// Largest disagreement between a point's projected z and the <c>z_index</c> its contour
        /// claims. Should be zero to floating-point noise.
        /// </summary>
        public double MaxZError { get; set; }

        public bool Passed { get { return OutOfBounds == 0 && MaxZError <= 1e-6; } }
    }

    /// <summary>
    /// Re-projects returned contour points through the inverse of the affine the client sent, and
    /// checks they land where they claim to.
    ///
    /// This mirrors <c>verify()</c> in voxtell-cloud/scripts/e2e_client.py, and it is the check
    /// that would catch a transposed axis, a flipped slice direction, or an off-by-one origin —
    /// the class of bug that otherwise shows up as contours drawn on the wrong slice inside
    /// Eclipse, where it is far harder to attribute.
    ///
    /// The plugin itself never needs the inverse: <c>points_lps</c> are already patient
    /// millimetres and go straight into <c>AddContourOnImagePlane</c>.
    /// </summary>
    public static class AffineCheck
    {
        /// <summary>Tolerance on the grid bounds, matching the reference client.</summary>
        private const double BoundsSlack = 1.0;

        public static List<AffineCheckResult> Verify(ResultEnvelope envelope, Geometry geometry)
        {
            double[,] inverse = InvertAffine(geometry);
            var results = new List<AffineCheckResult>();

            foreach (InferenceResult result in envelope.Results ?? new List<InferenceResult>())
            {
                var check = new AffineCheckResult
                {
                    Prompt = result.Prompt,
                    VoxelCount = result.VoxelCount,
                    FirstSlice = int.MaxValue,
                    LastSlice = int.MinValue,
                };

                foreach (ContourSlice contour in result.Contours ?? new List<ContourSlice>())
                {
                    check.ContourCount++;
                    check.FirstSlice = Math.Min(check.FirstSlice, contour.ZIndex);
                    check.LastSlice = Math.Max(check.LastSlice, contour.ZIndex);

                    foreach (double[] p in contour.PointsLps)
                    {
                        check.PointCount++;

                        double vx = inverse[0, 0] * p[0] + inverse[0, 1] * p[1] + inverse[0, 2] * p[2] + inverse[0, 3];
                        double vy = inverse[1, 0] * p[0] + inverse[1, 1] * p[1] + inverse[1, 2] * p[2] + inverse[1, 3];
                        double vz = inverse[2, 0] * p[0] + inverse[2, 1] * p[1] + inverse[2, 2] * p[2] + inverse[2, 3];

                        // The slack absorbs marching squares' half-voxel overshoot at the edges,
                        // which is legitimate: contours trace the 0.5 iso-level, not voxel centres.
                        if (vx < -BoundsSlack || vx > geometry.XSize + BoundsSlack ||
                            vy < -BoundsSlack || vy > geometry.YSize + BoundsSlack ||
                            vz < -BoundsSlack || vz > geometry.ZSize + BoundsSlack)
                        {
                            check.OutOfBounds++;
                        }

                        // Every point of a contour must sit on the plane its z_index names. This
                        // is the assertion that a flipped or offset slice axis fails.
                        check.MaxZError = Math.Max(check.MaxZError, Math.Abs(vz - contour.ZIndex));
                    }
                }

                if (check.ContourCount == 0)
                {
                    check.FirstSlice = 0;
                    check.LastSlice = 0;
                }

                results.Add(check);
            }

            return results;
        }

        /// <summary>
        /// Inverts the 4x4 LPS affine the server builds from the geometry:
        /// columns are the direction cosines scaled by the spacings, translation is the origin.
        /// </summary>
        private static double[,] InvertAffine(Geometry g)
        {
            // M = [ row*x_res | col*y_res | slice*z_res ], t = origin
            double[,] m =
            {
                { g.RowDirection[0] * g.XRes, g.ColDirection[0] * g.YRes, g.SliceDirection[0] * g.ZRes },
                { g.RowDirection[1] * g.XRes, g.ColDirection[1] * g.YRes, g.SliceDirection[1] * g.ZRes },
                { g.RowDirection[2] * g.XRes, g.ColDirection[2] * g.YRes, g.SliceDirection[2] * g.ZRes },
            };

            double det =
                m[0, 0] * (m[1, 1] * m[2, 2] - m[1, 2] * m[2, 1]) -
                m[0, 1] * (m[1, 0] * m[2, 2] - m[1, 2] * m[2, 0]) +
                m[0, 2] * (m[1, 0] * m[2, 1] - m[1, 1] * m[2, 0]);

            if (Math.Abs(det) < 1e-12)
            {
                throw new InvalidOperationException(
                    "The image geometry is singular -- the direction cosines are not independent, " +
                    "or a spacing is zero.");
            }

            // Adjugate over determinant. Only 3x3, so writing it out beats pulling in a
            // linear-algebra dependency for one call.
            double[,] inv = new double[3, 4];
            inv[0, 0] = (m[1, 1] * m[2, 2] - m[1, 2] * m[2, 1]) / det;
            inv[0, 1] = (m[0, 2] * m[2, 1] - m[0, 1] * m[2, 2]) / det;
            inv[0, 2] = (m[0, 1] * m[1, 2] - m[0, 2] * m[1, 1]) / det;
            inv[1, 0] = (m[1, 2] * m[2, 0] - m[1, 0] * m[2, 2]) / det;
            inv[1, 1] = (m[0, 0] * m[2, 2] - m[0, 2] * m[2, 0]) / det;
            inv[1, 2] = (m[0, 2] * m[1, 0] - m[0, 0] * m[1, 2]) / det;
            inv[2, 0] = (m[1, 0] * m[2, 1] - m[1, 1] * m[2, 0]) / det;
            inv[2, 1] = (m[0, 1] * m[2, 0] - m[0, 0] * m[2, 1]) / det;
            inv[2, 2] = (m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]) / det;

            // Translation of the inverse is -M^-1 * t.
            for (int r = 0; r < 3; r++)
            {
                inv[r, 3] = -(inv[r, 0] * g.Origin[0] +
                              inv[r, 1] * g.Origin[1] +
                              inv[r, 2] * g.Origin[2]);
            }

            return inv;
        }

        /// <summary>
        /// Round-trips voxel (0,0,0) and a far corner through the forward and inverse affine, so
        /// a broken inverse is caught before it is used to judge the server's output.
        /// </summary>
        public static void SelfTest(Geometry g)
        {
            double[,] inv = InvertAffine(g);

            var corners = new[]
            {
                new[] { 0.0, 0.0, 0.0 },
                new[] { (double)g.XSize - 1, g.YSize - 1, g.ZSize - 1 },
                new[] { g.XSize / 2.0, g.YSize / 3.0, g.ZSize / 4.0 },
            };

            foreach (double[] v in corners)
            {
                double x = g.Origin[0] + g.RowDirection[0] * g.XRes * v[0]
                         + g.ColDirection[0] * g.YRes * v[1] + g.SliceDirection[0] * g.ZRes * v[2];
                double y = g.Origin[1] + g.RowDirection[1] * g.XRes * v[0]
                         + g.ColDirection[1] * g.YRes * v[1] + g.SliceDirection[1] * g.ZRes * v[2];
                double z = g.Origin[2] + g.RowDirection[2] * g.XRes * v[0]
                         + g.ColDirection[2] * g.YRes * v[1] + g.SliceDirection[2] * g.ZRes * v[2];

                double bx = inv[0, 0] * x + inv[0, 1] * y + inv[0, 2] * z + inv[0, 3];
                double by = inv[1, 0] * x + inv[1, 1] * y + inv[1, 2] * z + inv[1, 3];
                double bz = inv[2, 0] * x + inv[2, 1] * y + inv[2, 2] * z + inv[2, 3];

                double error = Math.Max(Math.Abs(bx - v[0]),
                               Math.Max(Math.Abs(by - v[1]), Math.Abs(bz - v[2])));

                if (error > 1e-9)
                {
                    throw new InvalidOperationException(string.Format(
                        "The affine inverse is wrong: voxel ({0},{1},{2}) round-tripped with " +
                        "error {3:E2}.", v[0], v[1], v[2], error));
                }
            }
        }
    }
}
