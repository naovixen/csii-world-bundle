using ICSharpCode.SharpZipLib.Checksum;
using ICSharpCode.SharpZipLib.Zip.Compression;
using System;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// A minimal, deterministic PNG encoder for 8-bit RGBA images.
    /// （用於 8 位元 RGBA 影像的極簡決定性 PNG 編碼器。）<br/><br/>
    /// Written out rather than taken from Unity for three reasons. It runs off the main thread,
    /// which is where the bundle's textures are encoded. It works on any pixels handed to it,
    /// whereas Unity refuses to encode a texture whose source format is block-compressed — which
    /// shipped CS2 art always is. And it is deterministic: the same pixels always produce the same
    /// bytes, which is what makes the contract's "the file name <i>is</i> the SHA-256 of the file"
    /// invariant a property a validator can check rather than a claim it has to take on trust.<br/>
    /// （自行實作而非沿用 Unity，原因有三：其一，可於主執行緒之外執行，而束的貼圖正是在該處編碼；
    /// 其二，可處理任何交付給它的像素，而 Unity 會拒絕編碼來源格式為區塊壓縮的貼圖——
    /// CS2 隨遊戲附帶的美術資源恆為如此；其三，具決定性：相同像素恆產生相同位元組，
    /// 這使契約中「檔名<i>即</i>檔案的 SHA-256」成為驗證器可查核的性質，而非只能採信的宣稱。）<br/><br/>
    /// Deliberately not a general PNG library: colour type 6, bit depth 8, no interlacing, and the
    /// "None" filter on every scanline. Per-row filter selection would shrink the output somewhat,
    /// and is also where a hand-written PNG encoder stops being obviously correct.<br/>
    /// （刻意不做成通用 PNG 函式庫：色彩型別 6、位元深度 8、不交錯，且每條掃描線一律使用「None」濾波器。
    /// 逐列選擇濾波器可再縮小輸出，但那也正是手寫 PNG 編碼器不再顯然正確之處。）
    /// </summary>
    public static class Png
    {
        /// <summary>
        /// The eight bytes every PNG file starts with.（每個 PNG 檔案開頭的八個位元組。）
        /// </summary>
        private static readonly byte[] Signature = { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };

        /// <summary>
        /// Encode row-major <c>RGBA8</c> pixels as a PNG file.
        /// （將以列為主的 <c>RGBA8</c> 像素編碼為 PNG 檔案。）
        /// </summary>
        /// <param name="pixels">The <c>RGBA8</c> pixels, top row first.（<c>RGBA8</c> 像素，由上而下。）</param>
        /// <param name="width">The image width.（影像寬度。）</param>
        /// <param name="height">The image height.（影像高度。）</param>
        /// <returns>The complete PNG file, or <c>null</c> when there is nothing to encode.（完整的 PNG 檔案內容，若無可編碼者則為 <c>null</c>。）</returns>
        public static byte[] EncodeRgba(byte[] pixels, int width, int height)
        {
            if ((pixels == null) || (width <= 0) || (height <= 0)) return null;

            int stride = width * 4;
            if (pixels.Length < stride * height)
                throw new ArgumentException($"{width}x{height} RGBA needs {stride * height} bytes, got {pixels.Length}. {width}x{height} 的 RGBA 需要 {stride * height} 位元組。");

            // Each scanline is preceded by its filter byte, which is always 0 — "None".
            // （每條掃描線前有其濾波器位元組，此處恆為 0，即「None」。）
            byte[] raw = new byte[(stride + 1) * height];
            for (int y = 0; y < height; y++)
            {
                raw[y * (stride + 1)] = 0;
                Buffer.BlockCopy(pixels, y * stride, raw, (y * (stride + 1)) + 1, stride);
            }

            using MemoryStream stream = new();
            stream.Write(Signature, 0, Signature.Length);

            byte[] header = new byte[13];
            WriteBigEndian(header, 0, (uint)width);
            WriteBigEndian(header, 4, (uint)height);
            header[8] = 8;   // bit depth
            header[9] = 6;   // colour type: truecolour with alpha
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
        /// （以 zlib 壓縮緩衝區，此即 PNG <c>IDAT</c> 串流依定義應持有的內容。）
        /// </summary>
        /// <param name="data">The bytes to compress.（待壓縮的位元組。）</param>
        /// <returns>The zlib stream.（zlib 串流。）</returns>
        private static byte[] Deflate(byte[] data)
        {
            // noZlibHeaderOrFooter: false — PNG wants the header and the Adler-32, not raw deflate.
            // （noZlibHeaderOrFooter 為 false：PNG 需要 zlib 標頭與 Adler-32，而非裸的 deflate。）
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
        /// Write one chunk: length, type, payload, and a CRC-32 over the type and payload.
        /// （寫出一個區塊：長度、型別、內容，以及涵蓋型別與內容的 CRC-32。）
        /// </summary>
        /// <param name="stream">The file being built.（正在建構的檔案。）</param>
        /// <param name="type">The four-character chunk type.（四字元的區塊型別。）</param>
        /// <param name="payload">The chunk's contents.（區塊的內容。）</param>
        private static void WriteChunk(Stream stream, string type, byte[] payload)
        {
            byte[] length = new byte[4];
            WriteBigEndian(length, 0, (uint)payload.Length);
            stream.Write(length, 0, 4);

            byte[] name = Encoding.ASCII.GetBytes(type);
            stream.Write(name, 0, 4);
            stream.Write(payload, 0, payload.Length);

            // The length is deliberately outside the CRC; the spec covers type and data only.
            // （長度刻意不納入 CRC；規格僅涵蓋型別與資料。）
            Crc32 crc = new();
            crc.Update(name);
            crc.Update(payload);

            byte[] checksum = new byte[4];
            WriteBigEndian(checksum, 0, (uint)crc.Value);
            stream.Write(checksum, 0, 4);
        }

        /// <summary>
        /// PNG is big-endian throughout.（PNG 全篇採用大端序。）
        /// </summary>
        /// <param name="target">The buffer to write into.（要寫入的緩衝區。）</param>
        /// <param name="offset">Where to write.（寫入位置。）</param>
        /// <param name="value">The value to write.（要寫入的數值。）</param>
        private static void WriteBigEndian(byte[] target, int offset, uint value)
        {
            target[offset] = (byte)(value >> 24);
            target[offset + 1] = (byte)(value >> 16);
            target[offset + 2] = (byte)(value >> 8);
            target[offset + 3] = (byte)value;
        }
    }
}
