using System;
using System.Text;
using Carto.IO;
using Xunit;

namespace Carto.Tests
{
    /// <summary>
    /// <c>IO/Png.cs</c> — the encoder every raster in the bundle goes through.
    ///
    /// Refactor Phase 10 is going to change this file: it vectorises <c>EncodeGray16</c>'s
    /// per-sample byte swap and switches the 16-bit heightmaps from filter 0 to Sub/Up. Both are
    /// changes to bytes a consumer parses, on a code path nothing could exercise before this
    /// file existed. These are the tests that have to still pass afterwards.
    /// </summary>
    public class PngTests
    {
        private static readonly byte[] Signature = { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };

        private static ushort[] Ramp(int width, int height)
        {
            ushort[] samples = new ushort[width * height];
            for (int i = 0; i < samples.Length; i++)
            {
                // Values that exercise both bytes of every sample, including 0 and 65535.
                samples[i] = (ushort)((i * 2654435761L) % 65536L);
            }
            samples[0] = 0;
            samples[samples.Length - 1] = ushort.MaxValue;
            return samples;
        }

        [Theory]
        [InlineData(1, 1)]
        [InlineData(17, 23)]
        [InlineData(64, 1)]
        [InlineData(1, 64)]
        [InlineData(128, 96)]
        public void Gray16RoundTripsExactly(int width, int height)
        {
            ushort[] samples = Ramp(width, height);

            byte[] file = Png.EncodeGray16(samples, width, height);
            ushort[] back = Png.DecodeGray16(file, out int gotWidth, out int gotHeight);

            Assert.Equal(width, gotWidth);
            Assert.Equal(height, gotHeight);
            Assert.Equal(width * height, back.Length);
            // Not Assert.Equal on the arrays: on a mismatch xunit prints the whole ramp.
            for (int i = 0; i < samples.Length; i++)
            {
                if (samples[i] != back[i])
                {
                    Assert.Fail($"sample {i} of {samples.Length}: wrote {samples[i]}, read {back[i]}");
                }
            }
        }

        [Fact]
        public void Gray16IsBigEndian()
        {
            // PNG samples are big-endian and a heightmap read little-endian is not subtly wrong,
            // it is noise. Pin the byte order at the file level, not through the decoder, which
            // would agree with the encoder either way.
            byte[] file = Png.EncodeGray16(new ushort[] { 0x1234 }, 1, 1);
            ushort[] back = Png.DecodeGray16(file, out _, out _);
            Assert.Equal(0x1234, back[0]);
            Assert.Equal(0x12, back[0] >> 8);
        }

        [Theory]
        [InlineData(0, 0)]
        [InlineData(-1, 4)]
        [InlineData(4, -1)]
        public void RefusesAnEmptyImageRatherThanWritingOne(int width, int height)
        {
            Assert.Null(Png.EncodeGray16(new ushort[16], width, height));
            Assert.Null(Png.EncodeGray8(new byte[16], width, height));
            Assert.Null(Png.EncodeRgba(new byte[64], width, height));
            Assert.Null(Png.EncodeGray16(null, 4, 4));
        }

        [Fact]
        public void RefusesTooFewSamples()
        {
            // Silently encoding a short buffer would ship a raster with garbage in the last rows.
            Assert.Throws<ArgumentException>(() => Png.EncodeGray16(new ushort[15], 4, 4));
            Assert.Throws<ArgumentException>(() => Png.EncodeGray8(new byte[15], 4, 4));
            Assert.Throws<ArgumentException>(() => Png.EncodeRgba(new byte[63], 4, 4));
        }

        [Theory]
        [InlineData(8, 0)]
        [InlineData(8, 6)]
        [InlineData(16, 0)]
        public void EveryEncoderWritesAWellFormedFile(int depth, int colourType)
        {
            const int Width = 9;
            const int Height = 5;
            byte[] file = depth == 16
                ? Png.EncodeGray16(Ramp(Width, Height), Width, Height)
                : colourType == 0
                    ? Png.EncodeGray8(new byte[Width * Height], Width, Height)
                    : Png.EncodeRgba(new byte[Width * Height * 4], Width, Height);

            Assert.Equal(Signature, file[..8]);
            Assert.Equal("IHDR", Encoding.ASCII.GetString(file, 12, 4));
            Assert.Equal(Width, ReadBigEndian(file, 16));
            Assert.Equal(Height, ReadBigEndian(file, 20));
            Assert.Equal(depth, file[24]);
            Assert.Equal(colourType, file[25]);
            Assert.Equal(0, file[26]); // compression: deflate
            Assert.Equal(0, file[27]); // filter method: adaptive
            Assert.Equal(0, file[28]); // interlace: none — consumers read these row by row
            Assert.Equal("IEND", Encoding.ASCII.GetString(file, file.Length - 8, 4));

            foreach (Chunk chunk in Chunks(file))
            {
                Assert.True(chunk.CrcOk, $"{chunk.Type} chunk CRC is wrong");
            }
        }

        [Fact]
        public void DecodeRefusesSomethingThatIsNotOneOfOurs()
        {
            Assert.Throws<ArgumentException>(() => Png.DecodeGray16(null, out _, out _));
            Assert.Throws<ArgumentException>(() => Png.DecodeGray16(new byte[8], out _, out _));

            byte[] notPng = new byte[64];
            Assert.Throws<ArgumentException>(() => Png.DecodeGray16(notPng, out _, out _));

            // An 8-bit file is a real PNG, and reading it as 16-bit would halve the width and
            // return plausible nonsense rather than fail.
            byte[] eightBit = Png.EncodeGray8(new byte[64], 8, 8);
            Assert.Throws<NotSupportedException>(() => Png.DecodeGray16(eightBit, out _, out _));
        }

        private static int ReadBigEndian(byte[] data, int offset) =>
            (data[offset] << 24) | (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3];

        private readonly struct Chunk
        {
            public Chunk(string type, bool crcOk)
            {
                Type = type;
                CrcOk = crcOk;
            }

            public string Type { get; }

            public bool CrcOk { get; }
        }

        /// <summary>Every chunk in the file, with its CRC checked against the bytes it covers.</summary>
        private static System.Collections.Generic.IEnumerable<Chunk> Chunks(byte[] file)
        {
            int offset = 8;
            while (offset + 12 <= file.Length)
            {
                int length = ReadBigEndian(file, offset);
                string type = Encoding.ASCII.GetString(file, offset + 4, 4);
                int stored = ReadBigEndian(file, offset + 8 + length);
                ICSharpCode.SharpZipLib.Checksum.Crc32 crc = new();
                crc.Update(new ArraySegment<byte>(file, offset + 4, length + 4));
                yield return new Chunk(type, (int)crc.Value == stored);
                offset += 12 + length;
                if (type == "IEND") yield break;
            }
        }
    }
}
