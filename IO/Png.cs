using ICSharpCode.SharpZipLib.Checksum;
using ICSharpCode.SharpZipLib.Zip.Compression;
using System;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// A minimal, deterministic PNG encoder for 8-bit RGBA images.
    /// Written out rather than taken from Unity for three reasons. It runs off the main thread,
    /// which is where the bundle's textures are encoded. It works on any pixels handed to it,
    /// whereas Unity refuses to encode a texture whose source format is block-compressed — which
    /// shipped CS2 art always is. And it is deterministic: the same pixels always produce the same
    /// bytes, which is what makes the contract's "the file name <i>is</i> the SHA-256 of the file"
    /// invariant a property a validator can check rather than a claim it has to take on trust.
    /// Deliberately not a general PNG library: colour type 6, bit depth 8, no interlacing, and the
    /// "None" filter on every scanline. Per-row filter selection would shrink the output somewhat,
    /// and is also where a hand-written PNG encoder stops being obviously correct.
    /// </summary>
    public static class Png
    {
        /// <summary>
        /// The eight bytes every PNG file starts with.
        /// </summary>
        private static readonly byte[] Signature = { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };

        /// <summary>
        /// Encode row-major <c>RGBA8</c> pixels as a PNG file.
        /// </summary>
        /// <param name="pixels">The <c>RGBA8</c> pixels, top row first.</param>
        /// <param name="width">The image width.</param>
        /// <param name="height">The image height.</param>
        /// <returns>The complete PNG file, or <c>null</c> when there is nothing to encode.</returns>
        public static byte[] EncodeRgba(byte[] pixels, int width, int height)
        {
            if ((pixels == null) || (width <= 0) || (height <= 0)) return null;

            int stride = width * 4;
            if (pixels.Length < stride * height)
                throw new ArgumentException($"{width}x{height} RGBA needs {stride * height} bytes, got {pixels.Length}.");

            // Each scanline is preceded by its filter byte, which is always 0 — "None".
            //
            byte[] raw = new byte[(stride + 1) * height];
            for (int y = 0; y < height; y++)
            {
                raw[y * (stride + 1)] = 0;
                Buffer.BlockCopy(pixels, y * stride, raw, (y * (stride + 1)) + 1, stride);
            }

            return Assemble(raw, width, height, bitDepth: 8, colourType: 6);
        }

        /// <summary>
        /// Encode row-major 8-bit grayscale pixels as a PNG file.
        /// Colour type 0 at depth 8 — the contract's index-raster format (`surface_index.png`),
        /// where the pixel value is an id, not a colour, and any palette or channel expansion
        /// would invite resamplers to blend ids that must never blend.
        /// </summary>
        /// <param name="pixels">The 8-bit values, top row first.</param>
        /// <param name="width">The image width.</param>
        /// <param name="height">The image height.</param>
        /// <returns>The complete PNG file, or <c>null</c> when there is nothing to encode.</returns>
        public static byte[] EncodeGray8(byte[] pixels, int width, int height)
        {
            if ((pixels == null) || (width <= 0) || (height <= 0)) return null;
            if (pixels.Length < width * height)
                throw new ArgumentException($"{width}x{height} gray8 needs {width * height} bytes, got {pixels.Length}.");

            byte[] raw = new byte[(width + 1) * height];
            for (int y = 0; y < height; y++)
            {
                raw[y * (width + 1)] = 0;
                Buffer.BlockCopy(pixels, y * width, raw, (y * (width + 1)) + 1, width);
            }

            return Assemble(raw, width, height, bitDepth: 8, colourType: 0);
        }

        /// <summary>
        /// Encode row-major 16-bit grayscale samples as a PNG file.
        /// Colour type 0 at depth 16 — the contract's elevation-raster format. The game stores
        /// terrain heights as exactly these 16-bit samples, so this encoding is a lossless
        /// passthrough of what the game itself resolves, not a quantisation of it.
        /// </summary>
        /// <param name="samples">The 16-bit values, top row first.</param>
        /// <param name="width">The image width.</param>
        /// <param name="height">The image height.</param>
        /// <returns>The complete PNG file, or <c>null</c> when there is nothing to encode.</returns>
        public static byte[] EncodeGray16(ushort[] samples, int width, int height)
        {
            if ((samples == null) || (width <= 0) || (height <= 0)) return null;
            if (samples.Length < width * height)
                throw new ArgumentException($"{width}x{height} gray16 needs {width * height} samples, got {samples.Length}.");

            // PNG samples are big-endian; the filter byte precedes each scanline as in EncodeRgba.
            //
            int stride = width * 2;
            byte[] raw = new byte[(stride + 1) * height];
            for (int y = 0; y < height; y++)
            {
                int row = y * (stride + 1);
                raw[row] = 0;
                for (int x = 0; x < width; x++)
                {
                    ushort value = samples[y * width + x];
                    raw[row + 1 + x * 2] = (byte)(value >> 8);
                    raw[row + 2 + x * 2] = (byte)value;
                }
            }

            return Assemble(raw, width, height, bitDepth: 16, colourType: 0);
        }

        /// <summary>
        /// Wrap filtered scanlines into a complete PNG file.
        /// </summary>
        /// <param name="raw">The scanlines, each preceded by its filter byte.</param>
        /// <param name="width">The image width.</param>
        /// <param name="height">The image height.</param>
        /// <param name="bitDepth">Bits per sample.</param>
        /// <param name="colourType">The PNG colour type.</param>
        /// <returns>The complete PNG file.</returns>
        private static byte[] Assemble(byte[] raw, int width, int height, byte bitDepth, byte colourType)
        {
            using MemoryStream stream = new();
            stream.Write(Signature, 0, Signature.Length);

            byte[] header = new byte[13];
            WriteBigEndian(header, 0, (uint)width);
            WriteBigEndian(header, 4, (uint)height);
            header[8] = bitDepth;
            header[9] = colourType;
            header[10] = 0;  // compression method: deflate
            header[11] = 0;  // filter method: adaptive
            header[12] = 0;  // interlace method: none
            WriteChunk(stream, "IHDR", header);

            WriteChunk(stream, "IDAT", Deflate(raw));
            WriteChunk(stream, "IEND", Array.Empty<byte>());

            return stream.ToArray();
        }

        /// <summary>
        /// zlib-compress a buffer, which is what a PNG <c>IDAT</c> stream is defined to hold.
        /// </summary>
        /// <param name="data">The bytes to compress.</param>
        /// <returns>The zlib stream.</returns>
        private static byte[] Deflate(byte[] data)
        {
            // noZlibHeaderOrFooter: false — PNG wants the header and the Adler-32, not raw deflate.
            //
            Deflater deflater = new(Deflater.DEFAULT_COMPRESSION, false);
            deflater.SetInput(data);
            deflater.Finish();

            using MemoryStream output = new(1 + (data.Length / 2));
            byte[] buffer = new byte[64 * 1024];
            while (!deflater.IsFinished)
            {
                int count = deflater.Deflate(buffer);
                if (count <= 0) break;
                output.Write(buffer, 0, count);
            }

            return output.ToArray();
        }

        /// <summary>
        /// Decode a 16-bit grayscale PNG this encoder itself produced, returning the samples and
        /// image size.
        /// This is not a general PNG reader, for the same reason the encoder is not a general PNG
        /// writer: it accepts exactly the files <see cref="EncodeGray16"/> emits — colour type 0,
        /// bit depth 16, no interlacing, the "None" filter on every scanline — and refuses anything
        /// else rather than misread it. The manifest's bounds computation reads the bundle's own
        /// heightmap and water rasters back through this.
        /// </summary>
        /// <param name="file">The PNG file's bytes.</param>
        /// <param name="width">Receives the image width.</param>
        /// <param name="height">Receives the image height.</param>
        /// <returns>The 16-bit samples, top row first.</returns>
        public static ushort[] DecodeGray16(byte[] file, out int width, out int height)
        {
            if ((file == null) || (file.Length < Signature.Length + 25)) throw new ArgumentException("Not a PNG file.");
            for (int i = 0; i < Signature.Length; i++)
            {
                if (file[i] != Signature[i]) throw new ArgumentException("Not a PNG file.");
            }

            width = height = 0;
            using MemoryStream idat = new();
            int offset = Signature.Length;
            while (offset + 8 <= file.Length)
            {
                int length = (file[offset] << 24) | (file[offset + 1] << 16) | (file[offset + 2] << 8) | file[offset + 3];
                string type = Encoding.ASCII.GetString(file, offset + 4, 4);
                offset += 8;
                if ((length < 0) || (offset + length > file.Length)) throw new ArgumentException($"Chunk {type} overruns the file.");

                if (type == "IHDR")
                {
                    width = (file[offset] << 24) | (file[offset + 1] << 16) | (file[offset + 2] << 8) | file[offset + 3];
                    height = (file[offset + 4] << 24) | (file[offset + 5] << 16) | (file[offset + 6] << 8) | file[offset + 7];
                    if ((file[offset + 8] != 16) || (file[offset + 9] != 0) || (file[offset + 12] != 0))
                        throw new NotSupportedException("Only this encoder's own non-interlaced gray16 PNGs are readable. gray16 PNG。");
                }
                else if (type == "IDAT")
                {
                    idat.Write(file, offset, length);
                }
                else if (type == "IEND")
                {
                    break;
                }

                offset += length + 4; // Skip the payload and its CRC.
            }

            if ((width <= 0) || (height <= 0)) throw new ArgumentException("No IHDR chunk.");

            int stride = width * 2;
            byte[] raw = new byte[(stride + 1) * height];
            Inflater inflater = new(noHeader: false);
            inflater.SetInput(idat.ToArray());
            int total = 0;
            while ((total < raw.Length) && !inflater.IsFinished)
            {
                int count = inflater.Inflate(raw, total, raw.Length - total);
                if (count <= 0) break;
                total += count;
            }
            if (total != raw.Length) throw new ArgumentException($"IDAT held {total} of {raw.Length} expected bytes. {raw.Length}。");

            ushort[] samples = new ushort[width * height];
            for (int y = 0; y < height; y++)
            {
                int row = y * (stride + 1);
                if (raw[row] != 0) throw new NotSupportedException("Only the \"None\" scanline filter is readable.");
                for (int x = 0; x < width; x++)
                {
                    samples[y * width + x] = (ushort)((raw[row + 1 + x * 2] << 8) | raw[row + 2 + x * 2]);
                }
            }

            return samples;
        }

        /// <summary>
        /// Write one chunk: length, type, payload, and a CRC-32 over the type and payload.
        /// </summary>
        /// <param name="stream">The file being built.</param>
        /// <param name="type">The four-character chunk type.</param>
        /// <param name="payload">The chunk's contents.</param>
        private static void WriteChunk(Stream stream, string type, byte[] payload)
        {
            byte[] length = new byte[4];
            WriteBigEndian(length, 0, (uint)payload.Length);
            stream.Write(length, 0, 4);

            byte[] name = Encoding.ASCII.GetBytes(type);
            stream.Write(name, 0, 4);
            stream.Write(payload, 0, payload.Length);

            // The length is deliberately outside the CRC; the spec covers type and data only.
            //
            Crc32 crc = new();
            crc.Update(name);
            crc.Update(payload);

            byte[] checksum = new byte[4];
            WriteBigEndian(checksum, 0, (uint)crc.Value);
            stream.Write(checksum, 0, 4);
        }

        /// <summary>
        /// PNG is big-endian throughout.
        /// </summary>
        /// <param name="target">The buffer to write into.</param>
        /// <param name="offset">Where to write.</param>
        /// <param name="value">The value to write.</param>
        private static void WriteBigEndian(byte[] target, int offset, uint value)
        {
            target[offset] = (byte)(value >> 24);
            target[offset + 1] = (byte)(value >> 16);
            target[offset + 2] = (byte)(value >> 8);
            target[offset + 3] = (byte)value;
        }
    }
}
