using Carto.Domain;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Streams <see cref="InstanceRecord"/>s to a JSON Lines file.
    /// （將 <see cref="InstanceRecord"/> 串流寫出為 JSON Lines 檔案。）<br/><br/>
    /// Streaming rather than building a document: a dense city runs to hundreds of thousands of
    /// instances, and the bundle contract picks JSON Lines precisely so neither producer nor
    /// consumer has to hold them all at once.<br/>
    /// （採用串流而非建構整份文件：密集城市可達數十萬個實例，束契約選用 JSON Lines 正是為了讓
    /// 生產端與消費端都不必一次載入全部內容。）
    /// </summary>
    public sealed class InstanceWriter : IDisposable
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
        /// The number of rotations that had to be normalised before writing.
        /// （寫出前必須正規化的旋轉數量。）
        /// </summary>
        public int NormalizedRotations { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// （開啟 JSON Lines 檔案以供寫入。）
        /// </summary>
        /// <param name="path">The destination path.（目標路徑。）</param>
        public InstanceWriter(string path)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);

            // The contract requires LF line endings and UTF-8 without a BOM: a BOM would corrupt
            // the first record for any consumer that reads the file line by line.
            // （契約要求使用 LF 換行與無 BOM 的 UTF-8：BOM 會使逐行讀取的消費端無法解析第一筆紀錄。）
            _writer = new StreamWriter(path, false, new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write one record as a single line.
        /// （將單一紀錄寫為一行。）
        /// </summary>
        /// <param name="record">The record to write.（待寫出的紀錄。）</param>
        public void Write(InstanceRecord record)
        {
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["assetId"] = record.AssetId,
                ["kind"] = record.Kind,
                ["pos"] = Round(record.Position),
                ["rot"] = Round(Normalize(record.Rotation))
            };

            if (record.Scale.HasValue) line["scale"] = Round(record.Scale.Value);
            if (record.Parent.HasValue) line["parent"] = record.Parent.Value;
            if ((record.Attributes != null) && (record.Attributes.Count > 0)) line["attrs"] = record.Attributes;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// Return a unit-length rotation, normalising if the game's value has drifted.
        /// （回傳單位長度的旋轉，若遊戲提供的值已偏移則予以正規化。）<br/><br/>
        /// The contract requires unit quaternions and a validator enforces it, so guaranteeing that
        /// here is the producer's job. A degenerate rotation is replaced with identity rather than
        /// written as NaN, which would make the whole line unparseable.<br/>
        /// （契約要求單位四元數並由驗證器檢查，故此處由生產端負責保證。退化的旋轉會以單位旋轉取代，
        /// 而非寫出會使整行無法解析的 NaN。）
        /// </summary>
        private quaternion Normalize(quaternion rotation)
        {
            float4 v = rotation.value;
            float lengthSquared = math.dot(v, v);

            if (math.abs(lengthSquared - 1f) <= 1e-6f) return rotation;

            NormalizedRotations++;
            if ((lengthSquared <= math.FLT_MIN_NORMAL) || !math.isfinite(lengthSquared)) return quaternion.identity;
            return new quaternion(v * math.rsqrt(lengthSquared));
        }

        /// <summary>
        /// Round a position to millimetres.
        /// （將位置四捨五入至公釐。）<br/><br/>
        /// Full float precision would add several bytes per number across hundreds of thousands of
        /// lines to express a precision the game does not meaningfully have.<br/>
        /// （完整浮點精度會在數十萬行中為每個數字增加數個位元組，卻只是表達遊戲本身並不具備的精度。）
        /// </summary>
        private static double[] Round(float3 value) => new[]
        {
            Math.Round((double)value.x, 3),
            Math.Round((double)value.y, 3),
            Math.Round((double)value.z, 3)
        };

        /// <summary>
        /// Round a quaternion, keeping enough precision that it stays a unit quaternion within the
        /// tolerance a validator checks.<br/>
        /// （四捨五入四元數，並保留足夠精度使其在驗證器容差內仍為單位四元數。）
        /// </summary>
        private static double[] Round(quaternion value) => new[]
        {
            Math.Round((double)value.value.x, 6),
            Math.Round((double)value.value.y, 6),
            Math.Round((double)value.value.z, 6),
            Math.Round((double)value.value.w, 6)
        };

        /// <summary>
        /// Flush and close the file.（清空緩衝並關閉檔案。）
        /// </summary>
        public void Dispose() => _writer?.Dispose();
    }
}
