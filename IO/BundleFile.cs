using System;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The one place a bulk bundle layer reaches the disk.
    /// （束的大宗資料層抵達磁碟的唯一位置。）<br/><br/>
    /// <c>meshes/</c>, <c>networks/</c>, <c>instances.jsonl</c>, <c>lanes.jsonl</c> and
    /// <c>areas.jsonl</c> all pass through here, so the line endings and encoding a JSON Lines
    /// layer owes contract §1 are decided once rather than in each writer.<br/>
    /// （<c>meshes/</c>、<c>networks/</c>、<c>instances.jsonl</c>、<c>lanes.jsonl</c> 與
    /// <c>areas.jsonl</c> 皆經此處，使契約 §1 對 JSON Lines 層要求的換行與編碼
    /// 只需決定一次，而非在每個寫出器中各自決定。）<br/><br/>
    /// <b>Layers are written uncompressed.</b> They were briefly gzip-9 (COMP-1), which shrank the
    /// bundle by roughly half but put a slow deflate on the rip's critical path: the mesh writer is
    /// a single background thread feeding a bounded queue, so its throughput gates the main thread's
    /// per-frame slice (<see cref="Carto.Systems.MeshRipSystem"/>, <c>QueueCapacity</c>). Level 9
    /// runs at ~3.3 MiB/s on GLB float buffers — near-random data whose matches deflate cannot find
    /// — for 0.66 points of ratio over level 4, and the rip is a manual, repeated, in-game step
    /// whose wall-clock is the scarce resource here. Disk is not.<br/>
    /// （<b>各層以未壓縮形式寫出。</b>曾短暫採用 gzip-9（COMP-1），雖使束的體積約減半，
    /// 卻將緩慢的 deflate 置於擷取的關鍵路徑上：網格寫出器為單一背景執行緒，
    /// 且其佇列有界，故其吞吐量會箝制主執行緒每幀的推進量。
    /// 在 GLB 的浮點緩衝區上，第 9 級的速度僅約每秒 3.3 MiB，
    /// 而相較第 4 級也只多出 0.66 個百分點的壓縮率；
    /// 擷取是需人工反覆在遊戲中執行的步驟，稀缺的是它的實際耗時，而非磁碟空間。）<br/><br/>
    /// Consumers still read either form — a bundle ripped while COMP-1 was live is on disk, and
    /// contract §12.1 keeps both storage forms valid schema 1. Re-enabling compression is a change
    /// to this one file; if it comes back, it comes back at level 6 or below.<br/>
    /// （消費端仍可讀取兩種形式——COMP-1 生效期間擷取的束仍在磁碟上，
    /// 且契約 §12.1 使兩種儲存形式皆為有效的 schema 1。
    /// 若要重新啟用壓縮，只需更動此檔；且應採用第 6 級或更低。）
    /// </summary>
    public static class BundleFile
    {
        /// <summary>
        /// The on-disk path for a layer's logical path — the same path, layers being uncompressed.
        /// （層的邏輯路徑所對應的實際磁碟路徑；因未壓縮，兩者相同。）
        /// </summary>
        /// <param name="logicalPath">The layer's name, e.g. <c>…/instances.jsonl</c>.（層的名稱。）</param>
        /// <returns>The path actually written.（實際寫出的路徑。）</returns>
        public static string OnDisk(string logicalPath) => logicalPath;

        /// <summary>
        /// Open a text layer for writing, with the line endings and encoding the contract requires
        /// of every JSON Lines layer.<br/>
        /// （開啟文字層以供寫入，並套用契約對所有 JSON Lines 層要求的換行與編碼。）
        /// </summary>
        /// <param name="logicalPath">The layer's name.（層的名稱。）</param>
        /// <returns>A writer over the file.（覆於檔案之上的寫出器。）</returns>
        public static StreamWriter CreateText(string logicalPath)
        {
            // LF and UTF-8 without a BOM: a BOM would corrupt the first record for any consumer
            // that reads the file line by line, and CRLF is what contract §1 forbids outright.
            // （LF 換行與無 BOM 的 UTF-8：BOM 會使逐行讀取的消費端無法解析第一筆紀錄，
            //   而 CRLF 為契約 §1 明文禁止者。）
            return new StreamWriter(CreateStream(logicalPath), new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write a whole payload as a layer.
        /// （將整份內容寫為一層。）
        /// </summary>
        /// <param name="logicalPath">The layer's name.（層的名稱。）</param>
        /// <param name="payload">The bytes to write.（待寫出的位元組。）</param>
        /// <returns>The number of bytes actually written to disk.（實際寫至磁碟的位元組數。）</returns>
        public static long WriteAllBytes(string logicalPath, byte[] payload)
        {
            if (payload == null) throw new ArgumentNullException(nameof(payload));

            EnsureDirectory(logicalPath);
            File.WriteAllBytes(logicalPath, payload);

            return payload.LongLength;
        }

        /// <summary>
        /// Open the byte stream for a layer, creating its directory if needed.
        /// （開啟層的位元組串流，並在需要時建立其目錄。）
        /// </summary>
        /// <param name="logicalPath">The layer's name.（層的名稱。）</param>
        /// <returns>A stream over the file.（覆於檔案之上的串流。）</returns>
        public static Stream CreateStream(string logicalPath)
        {
            EnsureDirectory(logicalPath);
            return File.Create(logicalPath);
        }

        /// <summary>
        /// Create the layer's containing directory if it is not there yet.
        /// （若層所在的目錄尚不存在則建立之。）
        /// </summary>
        /// <param name="path">The layer's path.（層的路徑。）</param>
        private static void EnsureDirectory(string path)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);
        }
    }
}
