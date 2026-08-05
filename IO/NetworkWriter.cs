using Carto.Domain;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Streams <see cref="NetworkRecord"/>s to a JSON Lines file.
    /// （將 <see cref="NetworkRecord"/> 串流寫出為 JSON Lines 檔案。）<br/><br/>
    /// The same shape as <see cref="InstanceWriter"/> and for the same reason — the contract picks
    /// JSON Lines so neither side has to hold a whole city's networks at once.<br/>
    /// （與 <see cref="InstanceWriter"/> 結構相同，理由亦同：契約選用 JSON Lines，
    /// 使兩端皆無須一次載入整座城市的路網。）
    /// </summary>
    public sealed class NetworkWriter : IDisposable
    {
        /// <summary>
        /// The underlying writer.（底層的寫出器。）
        /// </summary>
        private readonly StreamWriter _writer;

        /// <summary>
        /// The number of records written so far.（目前已寫出的紀錄數量。）
        /// </summary>
        public int Count { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// （開啟 JSON Lines 檔案以供寫入。）
        /// </summary>
        /// <param name="path">The destination path.（目標路徑。）</param>
        public NetworkWriter(string path)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);

            // LF and UTF-8 without a BOM, as the contract requires of every JSON Lines layer.
            // （依契約要求，所有 JSON Lines 層皆使用 LF 換行與無 BOM 的 UTF-8。）
            _writer = new StreamWriter(path, false, new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write one record as a single line.
        /// （將單一紀錄寫為一行。）
        /// </summary>
        /// <param name="record">The record to write.（待寫出的紀錄。）</param>
        public void Write(NetworkRecord record)
        {
            double[][] centerline = new double[record.Centerline.Count][];
            for (int i = 0; i < record.Centerline.Count; i++) centerline[i] = Round(record.Centerline[i]);

            // Key order follows the contract's own example so a hand-read line looks familiar; the
            // centerline form is `centerline` + `width` and must carry neither `assetId` nor `pos`,
            // which the schema enforces with a `oneOf`.
            // （鍵的順序沿用契約自身的範例；中心線形式為 `centerline` 與 `width`，
            //   且不得帶有 `assetId` 或 `pos`，此點由結構描述的 `oneOf` 強制。）
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["kind"] = record.Kind,
                ["centerline"] = centerline,
                ["width"] = Math.Round((double)record.Width, 3)
            };

            if ((record.Attributes != null) && (record.Attributes.Count > 0)) line["attrs"] = record.Attributes;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// Round a position to millimetres, as <see cref="InstanceWriter"/> does.
        /// （將位置四捨五入至公釐，與 <see cref="InstanceWriter"/> 相同。）
        /// </summary>
        private static double[] Round(float3 value) => new[]
        {
            Math.Round((double)value.x, 3),
            Math.Round((double)value.y, 3),
            Math.Round((double)value.z, 3)
        };

        /// <summary>
        /// Flush and close the file.（清空緩衝並關閉檔案。）
        /// </summary>
        public void Dispose() => _writer?.Dispose();
    }
}
