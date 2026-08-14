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
    /// The same shape as <see cref="InstanceWriter"/> and for the same reason — the contract picks
    /// JSON Lines so neither side has to hold a whole city's networks at once.
    /// </summary>
    public sealed class NetworkWriter : IDisposable
    {
        /// <summary>
        /// The underlying writer.
        /// </summary>
        private readonly StreamWriter _writer;

        /// <summary>
        /// The number of records written so far.
        /// </summary>
        public int Count { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// </summary>
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.</param>
        public NetworkWriter(string path)
        {
            _writer = BundleFile.CreateText(path);
        }

        /// <summary>
        /// Write one record as a single line.
        /// </summary>
        /// <param name="record">The record to write.</param>
        public void Write(NetworkRecord record)
        {
            // A record is one geometry form or the other and never both, which the schema enforces
            // with a `oneOf`: the mesh form carries `assetId` + `pos` + `rot` and no `centerline`;
            // the centerline form carries `centerline` + `width` and no `assetId`. Branching here
            // is what keeps that guarantee in one place.
            //
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["kind"] = record.Kind
            };

            if (!string.IsNullOrEmpty(record.AssetId))
            {
                line["assetId"] = record.AssetId;
                line["pos"] = BundlePrecision.Vector(record.Position);
                line["rot"] = BundlePrecision.Rotation(record.Rotation);
                MeshForm++;
            }
            else
            {
                double[][] centerline = new double[record.Centerline.Count][];
                for (int i = 0; i < record.Centerline.Count; i++) centerline[i] = BundlePrecision.Vector(record.Centerline[i]);

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
        /// Optional keys are omitted rather than defaulted: a straight, uncut, unhung lane is
        /// the common case, and 300k records of <c>"spans":[[0,1]]</c> would be pure bulk.
        /// </summary>
        /// <param name="record">The record to write.</param>
        public void Write(LaneGeometryRecord record)
        {
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["prefab"] = record.Prefab,
                ["category"] = record.Category,
                ["curve"] = new[]
                {
                    BundlePrecision.Vector(record.CurveA), BundlePrecision.Vector(record.CurveB),
                    BundlePrecision.Vector(record.CurveC), BundlePrecision.Vector(record.CurveD)
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

            if (record.Wear != 0f) line["wear"] = Math.Round((double)record.Wear, 3);

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// How many records were written in the mesh form.
        /// </summary>
        public int MeshForm { get; private set; }

        /// <summary>
        /// How many records were written in the centerline form.
        /// </summary>
        public int CenterlineForm { get; private set; }


        /// <summary>
        /// Flush and close the file.
        /// </summary>
        public void Dispose() => _writer?.Dispose();
    }
}
