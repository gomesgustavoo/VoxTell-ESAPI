using System;
using System.IO;
using System.IO.Compression;
using System.Threading;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Builds the upload blob: <c>gzip( int16-LE voxels, C-order (Z, Y, X) )</c>, no header.
    ///
    /// Two structural changes from v1, both from PROTOCOL.md:
    /// <list type="bullet">
    /// <item>int16 rather than int32 — CT stored values and HU both fit, and it halves the wire
    /// size. No base64 either: v1 wrapped every slice in a JSON string, paying a further 33%.</item>
    /// <item>One stream for the whole volume rather than 200+ per-slice requests, because the
    /// server needs the exact compressed length up front (<c>upload_bytes</c>) and S3 multipart
    /// wants one object.</item>
    /// </list>
    ///
    /// The blob is built in memory rather than a temp file on purpose: a clinical workstation
    /// should not end up with patient voxels on its disk, and the Eclipse host is x64 so a
    /// 100-200 MB buffer is unremarkable.
    /// </summary>
    public static class VoxelEncoder
    {
        /// <summary>Reports slices completed out of the total, for a progress bar.</summary>
        public delegate void ProgressCallback(int slicesDone, int slicesTotal);

        /// <summary>
        /// Reads every slice through <paramref name="source"/> and returns the gzip blob.
        ///
        /// <paramref name="gate"/> is what makes this safe to call from a background thread:
        /// only the <c>GetVoxels</c> call and the int16 narrowing hop onto the ESAPI thread,
        /// while compression — the expensive part — stays off it. v1 ran gzip and base64 for
        /// the entire volume on the UI thread, which is why Eclipse froze during upload.
        /// </summary>
        public static byte[] BuildVolumeBlob(
            IVolumeSource source,
            IThreadGate gate,
            ProgressCallback progress,
            CancellationToken ct)
        {
            if (source == null) throw new ArgumentNullException("source");
            if (gate == null) throw new ArgumentNullException("gate");

            // Checked before doing the work, not after: the byte order is baked into the
            // BlockCopy below, so on a big-endian host every voxel would be swapped.
            if (!BitConverter.IsLittleEndian)
            {
                throw new PlatformNotSupportedException(
                    "The wire format is int16 little-endian and this host is big-endian.");
            }

            int xSize = source.XSize;
            int ySize = source.YSize;
            int zSize = source.ZSize;

            if (xSize <= 0 || ySize <= 0 || zSize <= 0)
                throw new InvalidOperationException("The image reports an empty voxel grid.");

            int voxelsPerSlice = xSize * ySize;
            long rawBytes = (long)voxelsPerSlice * zSize * 2L;

            // Pre-size the output so a 150 MB blob does not walk MemoryStream's doubling
            // ladder, reallocating and copying at every rung. CT gzips to roughly a quarter
            // of its raw size; an under-estimate only costs one growth.
            int initialCapacity = (int)Math.Min(rawBytes / 3L + (1 << 20), int.MaxValue);

            // Reused across slices. Two buffers, not one: the ESAPI thread fills `slice`
            // while nothing else touches it, and the gzip write happens on this thread.
            short[] slice = new short[voxelsPerSlice];
            byte[] sliceBytes = new byte[voxelsPerSlice * 2];

            using (var output = new MemoryStream(initialCapacity))
            {
                // Fastest, not Optimal: on a 150 MB volume Optimal costs seconds for a few
                // percent, and the bytes are about to cross a network that dwarfs the saving.
                using (var gzip = new GZipStream(output, CompressionLevel.Fastest, leaveOpen: true))
                {
                    for (int z = 0; z < zSize; z++)
                    {
                        ct.ThrowIfCancellationRequested();

                        int captured = z;
                        gate.Run(() => source.ReadSlice(captured, slice));

                        // int16 little-endian. Buffer.BlockCopy reinterprets the short[] as
                        // bytes in the platform's order, which on every Windows host Eclipse
                        // runs on is little-endian; the guard below states the assumption
                        // rather than leaving it implicit.
                        Buffer.BlockCopy(slice, 0, sliceBytes, 0, sliceBytes.Length);
                        gzip.Write(sliceBytes, 0, sliceBytes.Length);

                        if (progress != null)
                            progress(z + 1, zSize);
                    }
                }

                return output.ToArray();
            }
        }

        /// <summary>
        /// Sanity-checks a blob against the geometry the server will validate it with, so a
        /// mistake surfaces here rather than as a 400 after the upload — or worse, as a job
        /// that decodes garbage.
        /// </summary>
        public static void ValidateBlob(byte[] blob, IVolumeSource source)
        {
            if (blob == null || blob.Length == 0)
                throw new InvalidOperationException("The encoded volume is empty.");

            if (!BitConverter.IsLittleEndian)
            {
                throw new PlatformNotSupportedException(
                    "The wire format is int16 little-endian and this host is big-endian.");
            }

            long rawBytes = (long)source.XSize * source.YSize * source.ZSize * 2L;

            // The server rejects upload_bytes > rawBytes + 1 MiB as "upload_bytes_implausible",
            // its tripwire for a client that sent int32 by mistake. Gzip should land far under
            // that, so exceeding it means the encoding is wrong, not merely incompressible.
            if (blob.LongLength > rawBytes + (1L << 20))
            {
                throw new InvalidOperationException(string.Format(
                    "Encoded volume is {0:N0} bytes, which exceeds the {1:N0}-byte uncompressed " +
                    "size of a {2}x{3}x{4} int16 grid. The encoding is wrong.",
                    blob.LongLength, rawBytes, source.XSize, source.YSize, source.ZSize));
            }
        }
    }
}
