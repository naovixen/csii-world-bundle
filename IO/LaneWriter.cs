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
    /// Streams <see cref="LaneRecord"/>s to a JSON Lines file.
    /// （將 <see cref="LaneRecord"/> 串流寫出為 JSON Lines 檔案。）<br/><br/>
    /// The same shape as <see cref="NetworkWriter"/> and for the same reason: a full city has
    /// hundreds of thousands of lanes, and neither side should have to hold them all at once.<br/>
    /// （與 <see cref="NetworkWriter"/> 結構相同，理由亦同：完整城市有數十萬條車道，
    /// 兩端皆不應被迫一次全部載入。）
    /// </summary>
    public sealed class LaneWriter : IDisposable
    {
        /// <summary>The underlying writer.（底層的寫出器。）</summary>
        private readonly StreamWriter _writer;

        /// <summary>The number of records written so far.（目前已寫出的紀錄數量。）</summary>
        public int Count { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.（開啟 JSON Lines 檔案以供寫入。）
        /// </summary>
        /// <param name="path">The destination path.（目標路徑。）</param>
        public LaneWriter(string path)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);
            _writer = new StreamWriter(path, false, new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write one lane.（寫出一條車道。）
        /// </summary>
        /// <param name="record">The lane.（車道。）</param>
        public void Write(LaneRecord record)
        {
            if ((record.Path == null) || (record.Path.Count < 2)) return;

            List<double[]> path = new(record.Path.Count);
            foreach (float3 point in record.Path) path.Add(Round(point));

            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["kind"] = record.Kind,
                ["path"] = path,
                ["length"] = Math.Round(record.Length, 3)
            };

            if (record.Owner.HasValue) line["owner"] = record.Owner.Value;
            if (record.SpeedLimit.HasValue) line["limit"] = Math.Round(record.SpeedLimit.Value, 2);
            if (record.Curviness.HasValue) line["curviness"] = Math.Round(record.Curviness.Value, 4);
            if (record.Secondary) line["secondary"] = true;
            if ((record.Flags != null) && (record.Flags.Count > 0)) line["flags"] = record.Flags;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// Round a position to millimetres, matching every other coordinate in the bundle.
        /// （將座標四捨五入至公釐，與束中其他所有座標一致。）
        /// </summary>
        /// <param name="v">The position.（座標。）</param>
        /// <returns>The rounded triple.（四捨五入後的三元組。）</returns>
        private static double[] Round(float3 v) =>
            new double[] { Math.Round(v.x, 3), Math.Round(v.y, 3), Math.Round(v.z, 3) };

        /// <summary>Close the file.（關閉檔案。）</summary>
        public void Dispose() => _writer?.Dispose();
    }
}
