using ICSharpCode.SharpZipLib.Checksum;
using ICSharpCode.SharpZipLib.Zip.Compression;
using ICSharpCode.SharpZipLib.Zip.Compression.Streams;
using System;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The one place a bulk bundle layer reaches the disk, and the one place it is compressed.
    /// （束的大宗資料層抵達磁碟的唯一位置，亦即其被壓縮的唯一位置。）<br/><br/>
    /// Half the bundle by volume — <c>meshes/</c>, <c>networks/</c>, <c>instances.jsonl</c>,
    /// <c>lanes.jsonl</c> — used to ship with no compression at all, while the other half
    /// (<c>textures/</c>, <c>terrain/</c>) was already PNG-compressed. Routing every <c>.glb</c>
    /// and every <c>.jsonl</c> through here closes that gap in one seam rather than in each
    /// writer.<br/>
    /// （束中約半數體積——<c>meshes/</c>、<c>networks/</c>、<c>instances.jsonl</c>、
    /// <c>lanes.jsonl</c>——過去完全未經壓縮，而另一半（<c>textures/</c>、<c>terrain/</c>）
    /// 早已是 PNG 壓縮。將所有 <c>.glb</c> 與 <c>.jsonl</c> 導經此處，可在單一接縫處
    /// 而非在每個寫出器中補上此缺口。）<br/><br/>
    /// <b>The suffix is the declaration.</b> A compressed layer is named <c>&lt;name&gt;.gz</c>,
    /// never the bare name with hidden compression — a consumer that has not been updated must
    /// fail to find the file, not succeed in reading gzip as JSON. <c>bundle.json</c>'s
    /// <c>layers</c> index carries the same fact in machine-readable form (contract §12.1).<br/>
    /// （<b>後綴即宣告。</b>壓縮過的層命名為 <c>&lt;name&gt;.gz</c>，絕不以原名隱藏壓縮——
    /// 未更新的消費端應該找不到檔案，而非成功地把 gzip 當成 JSON 讀取。）<br/><br/>
    /// gzip rather than zstd or lzma because <c>ICSharpCode.SharpZipLib</c> is already referenced
    /// and every consumer language decompresses gzip from its standard library. The measured gap
    /// to lzma is real but does not earn a new native dependency inside a game mod.<br/>
    /// （選用 gzip 而非 zstd 或 lzma，因為 <c>ICSharpCode.SharpZipLib</c> 已被參照，
    /// 且各消費端語言皆可用標準函式庫解壓 gzip。與 lzma 的差距雖確實存在，
    /// 但不值得在遊戲模組中引入新的原生相依。）
    /// </summary>
    public static class BundleFile
    {
        /// <summary>
        /// The suffix appended to a layer's logical name once it is compressed.
        /// （層的邏輯名稱在壓縮後所附加的後綴。）
        /// </summary>
        public const string CompressedSuffix = ".gz";

        /// <summary>
        /// The codec name written into <c>bundle.json</c>'s layer index.
        /// （寫入 <c>bundle.json</c> 層索引中的編碼器名稱。）
        /// </summary>
        public const string Codec = "gzip";

        /// <summary>
        /// Maximum deflate effort. The rip already spends minutes walking the city; the extra
        /// seconds buy hundreds of megabytes off every bundle ever read afterwards.<br/>
        /// （最大 deflate 強度。擷取本就要花數分鐘走訪整座城市，多花的數秒可為此後每一次
        /// 讀取的束省下數百 MB。）
        /// </summary>
        private const int Level = 9;

        /// <summary>
        /// The on-disk path for a layer's logical path.
        /// （層的邏輯路徑所對應的實際磁碟路徑。）
        /// </summary>
        /// <param name="logicalPath">The uncompressed name, e.g. <c>…/instances.jsonl</c>.（未壓縮的名稱。）</param>
        /// <returns>The path actually written.（實際寫出的路徑。）</returns>
        public static string OnDisk(string logicalPath) => logicalPath + CompressedSuffix;

        /// <summary>
        /// Open a compressed text layer for writing, with the line endings and encoding the
        /// contract requires of every JSON Lines layer.<br/>
        /// （開啟壓縮的文字層以供寫入，並套用契約對所有 JSON Lines 層要求的換行與編碼。）
        /// </summary>
        /// <param name="logicalPath">The uncompressed name of the layer.（層的未壓縮名稱。）</param>
        /// <returns>A writer over the gzip stream.（覆於 gzip 串流之上的寫出器。）</returns>
        public static StreamWriter CreateText(string logicalPath)
        {
            // LF and UTF-8 without a BOM: a BOM would corrupt the first record for any consumer
            // that reads the file line by line.
            // （LF 換行與無 BOM 的 UTF-8：BOM 會使逐行讀取的消費端無法解析第一筆紀錄。）
            return new StreamWriter(CreateStream(logicalPath), new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write a whole payload as a compressed layer.
        /// （將整份內容寫為壓縮的層。）
        /// </summary>
        /// <param name="logicalPath">The uncompressed name of the layer.（層的未壓縮名稱。）</param>
        /// <param name="payload">The bytes to compress.（待壓縮的位元組。）</param>
        /// <returns>The number of bytes actually written to disk.（實際寫至磁碟的位元組數。）</returns>
        public static long WriteAllBytes(string logicalPath, byte[] payload)
        {
            if (payload == null) throw new ArgumentNullException(nameof(payload));

            using (Stream stream = CreateStream(logicalPath))
            {
                stream.Write(payload, 0, payload.Length);
            }

            return new FileInfo(OnDisk(logicalPath)).Length;
        }

        /// <summary>
        /// Open the compressed byte stream for a layer, creating its directory if needed.
        /// （開啟層的壓縮位元組串流，並在需要時建立其目錄。）
        /// </summary>
        /// <param name="logicalPath">The uncompressed name of the layer.（層的未壓縮名稱。）</param>
        /// <returns>A stream that gzip-compresses everything written to it.（會將所寫入內容以 gzip 壓縮的串流。）</returns>
        public static Stream CreateStream(string logicalPath)
        {
            string path = OnDisk(logicalPath);

            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);

            return new GzipStream(File.Create(path));
        }

        /// <summary>
        /// A gzip container whose bytes depend only on its contents.
        /// （其位元組僅取決於內容的 gzip 容器。）<br/><br/>
        /// <c>SharpZipLib</c>'s own <see cref="ICSharpCode.SharpZipLib.GZip.GZipOutputStream"/>
        /// stamps <c>DateTime.Now</c> into the header's MTIME field, so two rips of the same city
        /// would differ byte-for-byte for no reason. The bundle treats determinism as a checkable
        /// property — it is what makes "the file name <i>is</i> the SHA-256 of the file" hold for
        /// textures and what lets an A/B run compare two exports directly — so the ten header
        /// bytes are written here instead, with MTIME zero.<br/>
        /// （<c>SharpZipLib</c> 自身的 <see cref="ICSharpCode.SharpZipLib.GZip.GZipOutputStream"/>
        /// 會將 <c>DateTime.Now</c> 寫入標頭的 MTIME 欄位，使同一座城市的兩次擷取無故產生不同位元組。
        /// 束將決定性視為可查核的性質，故此處自行寫出十個標頭位元組，並將 MTIME 設為零。）
        /// </summary>
        private sealed class GzipStream : Stream
        {
            /// <summary>
            /// The file being written.（正在寫出的檔案。）
            /// </summary>
            private readonly Stream _output;

            /// <summary>
            /// The raw deflate stream, without zlib framing of its own.
            /// （裸 deflate 串流，不含自身的 zlib 框架。）
            /// </summary>
            private readonly DeflaterOutputStream _deflate;

            /// <summary>
            /// The CRC-32 of the uncompressed bytes, which gzip's footer carries.
            /// （未壓縮位元組的 CRC-32，即 gzip 尾端所帶者。）
            /// </summary>
            private readonly Crc32 _crc = new();

            /// <summary>
            /// The uncompressed length, modulo 2^32, which gzip's footer also carries.
            /// （未壓縮長度對 2^32 取模，亦為 gzip 尾端所帶者。）
            /// </summary>
            private long _length;

            /// <summary>
            /// Whether the footer has already been written.（尾端是否已寫出。）
            /// </summary>
            private bool _finished;

            public GzipStream(Stream output)
            {
                _output = output;

                // 1f 8b — the gzip magic. 08 — deflate. 00 — no optional fields. Then MTIME as
                // four zero bytes ("no timestamp available", which is exactly the claim we want to
                // make). 02 — the compressor used maximum effort. ff — unknown source OS, so the
                // header says nothing about the machine that produced it.
                // （1f 8b 為 gzip 魔數；08 為 deflate；00 表示無選用欄位；接著是四個零位元組的
                //   MTIME（意為「無時間戳記」，正是我們要表達的主張）；02 表示壓縮器使用最大強度；
                //   ff 表示來源作業系統未知，使標頭不透露產生它的機器。）
                _output.Write(new byte[] { 0x1F, 0x8B, 0x08, 0x00, 0, 0, 0, 0, 0x02, 0xFF }, 0, 10);

                _deflate = new DeflaterOutputStream(_output, new Deflater(Level, noZlibHeaderOrFooter: true))
                {
                    IsStreamOwner = false
                };
            }

            public override bool CanRead => false;
            public override bool CanSeek => false;
            public override bool CanWrite => true;
            public override long Length => throw new NotSupportedException();

            public override long Position
            {
                get => throw new NotSupportedException();
                set => throw new NotSupportedException();
            }

            public override void Write(byte[] buffer, int offset, int count)
            {
                _deflate.Write(buffer, offset, count);
                _crc.Update(new ArraySegment<byte>(buffer, offset, count));
                _length += count;
            }

            public override void Flush() => _deflate.Flush();

            public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
            public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
            public override void SetLength(long value) => throw new NotSupportedException();

            protected override void Dispose(bool disposing)
            {
                if (!disposing) { base.Dispose(false); return; }

                try
                {
                    if (!_finished)
                    {
                        _finished = true;
                        _deflate.Finish();

                        // gzip's footer: CRC-32 then the uncompressed size, both little-endian,
                        // the size taken modulo 2^32 as the format defines.
                        // （gzip 的尾端：CRC-32 與未壓縮長度，皆為小端序，長度依格式定義對 2^32 取模。）
                        uint crc = (uint)_crc.Value;
                        uint size = (uint)_length;
                        _output.Write(new byte[]
                        {
                            (byte)crc, (byte)(crc >> 8), (byte)(crc >> 16), (byte)(crc >> 24),
                            (byte)size, (byte)(size >> 8), (byte)(size >> 16), (byte)(size >> 24)
                        }, 0, 8);
                    }
                }
                finally
                {
                    _deflate.Dispose();
                    _output.Dispose();
                    base.Dispose(true);
                }
            }
        }
    }
}
