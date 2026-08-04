using Carto.Domain;
using Colossal.Logging;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace Carto.IO
{
    /// <summary>
    /// One texture read back from the game and waiting to be encoded.
    /// （已自遊戲回讀、等待編碼的單一貼圖。）<br/><br/>
    /// Carries raw <c>RGBA8</c> pixels rather than a Unity <c>Texture</c>: reading the GPU has to
    /// happen on the main thread, but everything after it — compressing, hashing, writing — must
    /// not, and a plain byte array is the only form of the image that crosses that line safely.<br/>
    /// （攜帶原始 <c>RGBA8</c> 像素而非 Unity <c>Texture</c>：回讀 GPU 必須於主執行緒進行，
    /// 但其後的壓縮、雜湊與寫檔皆不應如此，而純位元組陣列是唯一能安全跨越該界線的影像形式。）
    /// </summary>
    public class PendingTexture
    {
        /// <summary>
        /// The stable identity of the source texture asset. Materials refer to a texture by this
        /// key until the content hash — and therefore the file name — is known.<br/>
        /// （來源貼圖資產的穩定識別。在內容雜湊（亦即檔名）確定前，材質皆以此鍵參照貼圖。）
        /// </summary>
        public string Key { get; set; }

        /// <summary>The texture asset's name, for logging.（貼圖資產名稱，供記錄使用。）</summary>
        public string SourceName { get; set; }

        /// <summary>Pixel dimensions of <see cref="Pixels"/>.（<see cref="Pixels"/> 的像素尺寸。）</summary>
        public int Width { get; set; }

        /// <inheritdoc cref="Width"/>
        public int Height { get; set; }

        /// <summary>
        /// Row-major <c>RGBA8</c> pixels, top row first — PNG's row order, not Unity's.
        /// （以列為主、由上而下的 <c>RGBA8</c> 像素，採 PNG 而非 Unity 的列順序。）
        /// </summary>
        public byte[] Pixels { get; set; }
    }

    /// <summary>
    /// Encodes ripped textures to lossless PNG under <c>textures/&lt;sha256&gt;.png</c>, deduped by
    /// content hash, per §8 of <c>docs/bundle-schema.md</c>.<br/>
    /// （依 <c>docs/bundle-schema.md</c> §8，將擷取的貼圖編碼為無損 PNG 並寫至
    /// <c>textures/&lt;sha256&gt;.png</c>，以內容雜湊去重。）<br/><br/>
    /// Dedup happens twice, because there are two ways to be a duplicate. A texture <i>asset</i>
    /// shared by fifty surfaces is claimed once by key, so it is read back from the GPU once. Two
    /// distinct assets that happen to hold identical pixels are only discovered to be the same
    /// after encoding, and are then written once under the hash they share.<br/>
    /// （去重進行兩次，因為成為重複的方式有兩種。由五十個表面共用的同一貼圖<i>資產</i>
    /// 以索引鍵取用一次，故僅自 GPU 回讀一次。兩個恰好持有相同像素的相異資產則須編碼後方能發現其相同，
    /// 屆時以其共同的雜湊寫出一次。）<br/><br/>
    /// Everything here is safe off the main thread; see <see cref="Png"/> for why the encoder is
    /// the mod's own.<br/>
    /// （此處的一切皆可安全地於主執行緒之外執行；編碼器為何由模組自行實作，
    /// 請見 <see cref="Png"/>。）
    /// </summary>
    public class TextureWriter
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The <c>textures/</c> directory being written into.（正在寫入的 <c>textures/</c> 目錄。）
        /// </summary>
        private readonly string _directory;

        /// <summary>
        /// The bundle-root-relative prefix every returned path carries.
        /// （所有回傳路徑所帶的束根目錄相對前綴。）
        /// </summary>
        private readonly string _prefix;

        /// <summary>
        /// Texture key to bundle-relative path, for the material table to resolve against.
        /// （貼圖索引鍵對應束相對路徑，供材質表解析使用。）
        /// </summary>
        private readonly ConcurrentDictionary<string, string> _paths = new();

        /// <summary>
        /// Content hashes already on disk, so an identical image is encoded once and written once.
        /// （已寫入磁碟的內容雜湊，使相同影像只編碼與寫出一次。）
        /// </summary>
        private readonly ConcurrentDictionary<string, bool> _hashes = new();

        /// <summary>Files written, images that turned out to be duplicates, and failures.（已寫出的檔案數、重複的影像數與失敗數。）</summary>
        public int Written, Deduped, Failed;

        /// <summary>Bytes written to disk.（寫入磁碟的位元組數。）</summary>
        public long Bytes;

        /// <summary>
        /// Every texture that produced no file, and why.（每個未產出檔案的貼圖及其原因。）
        /// </summary>
        public ConcurrentQueue<RipWarning> Skipped = new();

        /// <summary>
        /// Create a writer over a bundle's texture directory.
        /// （為某個束的貼圖目錄建立寫出器。）
        /// </summary>
        /// <param name="directory">The absolute <c>textures/</c> directory.（<c>textures/</c> 的絕對路徑。）</param>
        /// <param name="prefix">The bundle-relative prefix, e.g. <c>textures/</c>.（束相對前綴，例如 <c>textures/</c>。）</param>
        public TextureWriter(string directory, string prefix = "textures/")
        {
            _directory = directory;
            _prefix = prefix;
        }

        /// <summary>
        /// Reserve a key before the pixels have been read, so two prefabs sharing a texture do not
        /// both queue it.<br/>
        /// （於讀取像素前先保留索引鍵，使共用同一貼圖的兩個預製模板不會重複排入佇列。）
        /// </summary>
        /// <param name="key">The texture key.（貼圖索引鍵。）</param>
        /// <returns>Whether this caller is the one that must supply it.（此呼叫端是否為應提供該貼圖者。）</returns>
        public bool Claim(string key) => _paths.TryAdd(key, null);

        /// <summary>
        /// Resolve a texture key to its bundle-relative path.
        /// （將貼圖索引鍵解析為束相對路徑。）
        /// </summary>
        /// <param name="key">The texture key.（貼圖索引鍵。）</param>
        /// <returns>The path, or <c>null</c> when the texture never reached disk.（路徑，若該貼圖未寫入磁碟則為 <c>null</c>。）</returns>
        public string Resolve(string key)
            => (key != null) && _paths.TryGetValue(key, out string path) ? path : null;

        /// <summary>
        /// Encode, hash and write one queued texture. Safe to call off the main thread.
        /// （編碼、雜湊並寫出一張排入佇列的貼圖。可於主執行緒之外呼叫。）
        /// </summary>
        /// <param name="texture">The texture to write.（待寫出的貼圖。）</param>
        public void Write(PendingTexture texture)
        {
            try
            {
                byte[] png = Png.EncodeRgba(texture.Pixels, texture.Width, texture.Height);
                if ((png == null) || (png.Length == 0))
                {
                    Fail(texture, "encoder produced nothing");
                    return;
                }

                string hash = Sha256(png);
                _paths[texture.Key] = $"{_prefix}{hash}.png";

                // The hash is of the file's own bytes, so an image already on disk under this name
                // is byte-identical and re-writing it would only cost time.
                // （雜湊取自檔案自身的位元組，因此磁碟上同名的影像必然位元組相同，重寫僅是浪費時間。）
                if (!_hashes.TryAdd(hash, true))
                {
                    Interlocked.Increment(ref Deduped);
                    return;
                }

                if (!Directory.Exists(_directory)) Directory.CreateDirectory(_directory);
                File.WriteAllBytes(Path.Combine(_directory, $"{hash}.png"), png);

                Interlocked.Increment(ref Written);
                Interlocked.Add(ref Bytes, png.LongLength);
            }
            catch (Exception ex)
            {
                Fail(texture, $"{ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to write texture `{texture.SourceName}`: {ex}");
            }
        }

        /// <summary>
        /// Record a texture that produced no file.（記錄未產出檔案的貼圖。）
        /// </summary>
        private void Fail(PendingTexture texture, string reason)
        {
            Interlocked.Increment(ref Failed);
            _paths[texture.Key] = null;
            Skipped.Enqueue(new RipWarning("texture", texture.SourceName ?? texture.Key, reason));
        }

        /// <summary>
        /// The lowercase hex SHA-256 the contract names the file after.
        /// （契約用以命名檔案的小寫十六進位 SHA-256。）
        /// </summary>
        private static string Sha256(byte[] data)
        {
            using SHA256 sha = SHA256.Create();
            byte[] hash = sha.ComputeHash(data);

            StringBuilder builder = new(hash.Length * 2);
            foreach (byte b in hash) builder.Append(b.ToString("x2"));
            return builder.ToString();
        }
    }
}
