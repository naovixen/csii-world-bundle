using Carto.Domain;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
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
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.（目標路徑，未含 <see cref="BundleFile"/> 的後綴。）</param>
        public NetworkWriter(string path)
        {
            _writer = BundleFile.CreateText(path);
        }

        /// <summary>
        /// Write one record as a single line.
        /// （將單一紀錄寫為一行。）
        /// </summary>
        /// <param name="record">The record to write.（待寫出的紀錄。）</param>
        public void Write(NetworkRecord record)
        {
            // A record is one geometry form or the other and never both, which the schema enforces
            // with a `oneOf`: the mesh form carries `assetId` + `pos` + `rot` and no `centerline`;
            // the centerline form carries `centerline` + `width` and no `assetId`. Branching here
            // is what keeps that guarantee in one place.
            // （紀錄只能是其中一種幾何形式，絕不並存，此點由結構描述的 `oneOf` 強制：
            //   網格形式帶有 `assetId`、`pos` 與 `rot` 而無 `centerline`；
            //   中心線形式帶有 `centerline` 與 `width` 而無 `assetId`。）
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["kind"] = record.Kind
            };

            if (!string.IsNullOrEmpty(record.AssetId))
            {
                line["assetId"] = record.AssetId;
                line["pos"] = Round(record.Position);
                line["rot"] = Round(record.Rotation);
                MeshForm++;
            }
            else
            {
                double[][] centerline = new double[record.Centerline.Count][];
                for (int i = 0; i < record.Centerline.Count; i++) centerline[i] = Round(record.Centerline[i]);

                line["centerline"] = centerline;
                line["width"] = Math.Round((double)record.Width, 3);
                CenterlineForm++;
            }

            if ((record.Attributes != null) && (record.Attributes.Count > 0)) line["attrs"] = record.Attributes;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// Write one lane-geometry parameter record as a single line (contract §5B v2).
        /// （將一筆車道幾何參數紀錄寫為一行（契約 §5B v2）。）<br/><br/>
        /// Optional keys are omitted rather than defaulted: a straight, uncut, unhung lane is
        /// the common case, and 300k records of <c>"spans":[[0,1]]</c> would be pure bulk.
        /// （可省略的鍵直接省略而非填入預設值：未切割、未懸垂的直線車道是常態，
        /// 三十萬筆 <c>"spans":[[0,1]]</c> 只是徒增體積。）
        /// </summary>
        /// <param name="record">The record to write.（待寫出的紀錄。）</param>
        public void Write(LaneGeometryRecord record)
        {
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["prefab"] = record.Prefab,
                ["category"] = record.Category,
                ["curve"] = new[]
                {
                    Round(record.CurveA), Round(record.CurveB),
                    Round(record.CurveC), Round(record.CurveD)
                },
                ["parts"] = record.Parts
            };

            if (record.Owner >= 0) line["owner"] = record.Owner;

            if (record.Spans.Count > 0)
            {
                double[][] spans = new double[record.Spans.Count][];
                for (int i = 0; i < record.Spans.Count; i++)
                {
                    spans[i] = new[]
                    {
                        Math.Round((double)record.Spans[i].x, 5),
                        Math.Round((double)record.Spans[i].y, 5)
                    };
                }
                line["spans"] = spans;
            }

            if (!record.WidthScale.Equals(new float2(1f, 1f)))
            {
                line["widthScale"] = new[]
                {
                    Math.Round((double)record.WidthScale.x, 4),
                    Math.Round((double)record.WidthScale.y, 4)
                };
            }

            if (record.HasHang)
            {
                line["hang"] = new[]
                {
                    Math.Round((double)record.HangDistances.x, 4),
                    Math.Round((double)record.HangDistances.y, 4)
                };
            }

            if (record.ColorSet != null) line["colorSet"] = record.ColorSet;

            if (record.Tunnel) line["tunnel"] = true;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// How many records were written in the mesh form.（以網格形式寫出的紀錄數。）
        /// </summary>
        public int MeshForm { get; private set; }

        /// <summary>
        /// How many records were written in the centerline form.（以中心線形式寫出的紀錄數。）
        /// </summary>
        public int CenterlineForm { get; private set; }

        /// <summary>
        /// Round a rotation, keeping enough precision that it stays a unit quaternion within the
        /// tolerance a validator checks.<br/>
        /// （四捨五入旋轉，並保留足夠精度使其在驗證器容差內仍為單位四元數。）
        /// </summary>
        private static double[] Round(quaternion value) => new[]
        {
            Math.Round((double)value.value.x, 6),
            Math.Round((double)value.value.y, 6),
            Math.Round((double)value.value.z, 6),
            Math.Round((double)value.value.w, 6)
        };

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
