using System;
using System.IO;
using System.IO.Compression;
using VMS.TPS.Common.Model.API;

namespace VoxTell_Interface.Services
{
    public static class VoxelEncoder
    {
        /// <summary>
        /// Extracts a single Z-plane from the TPS Image, converts to
        /// int32 LE bytes, gzips, and base64-encodes.
        /// MUST be called on the main (STA) thread.
        /// </summary>
        public static string ExtractAndEncodeSlice(Image image, int zIndex)
        {
            int xSize = image.XSize;
            int ySize = image.YSize;

            // TPS Image.GetVoxels uses int[xSize, ySize]
            int[,] voxels = new int[xSize, ySize];
            image.GetVoxels(zIndex, voxels);

            // Convert int voxels to int32 bytes (4 bytes per voxel, little-endian)
            // Flatten in row-major order (y outer, x inner) to match backend expectations
            byte[] rawBytes = new byte[xSize * ySize * 4];
            int byteIndex = 0;
            for (int y = 0; y < ySize; y++)
            {
                for (int x = 0; x < xSize; x++)
                {
                    int val = voxels[x, y];
                    rawBytes[byteIndex++] = (byte)(val & 0xFF);
                    rawBytes[byteIndex++] = (byte)((val >> 8) & 0xFF);
                    rawBytes[byteIndex++] = (byte)((val >> 16) & 0xFF);
                    rawBytes[byteIndex++] = (byte)((val >> 24) & 0xFF);
                }
            }

            // GZip compress
            byte[] compressedBytes;
            using (var outputStream = new MemoryStream())
            {
                using (var gzipStream = new GZipStream(outputStream, CompressionMode.Compress))
                {
                    gzipStream.Write(rawBytes, 0, rawBytes.Length);
                }
                compressedBytes = outputStream.ToArray();
            }

            return Convert.ToBase64String(compressedBytes);
        }
    }
}
