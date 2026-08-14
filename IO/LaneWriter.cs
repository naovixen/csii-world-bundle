using Carto.Domain;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Streams <see cref="LaneRecord"/>s to a JSON Lines file.
    /// The same shape as <see cref="NetworkWriter"/> and for the same reason: a full city has
    /// hundreds of thousands of lanes, and neither side should have to hold them all at once.
    /// </summary>
    public sealed class LaneWriter : IDisposable
    {
        /// <summary>The underlying writer.</summary>
        private readonly StreamWriter _writer;

        /// <summary>The number of records written so far.</summary>
        public int Count { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// </summary>
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.</param>
        public LaneWriter(string path)
        {
            _writer = BundleFile.CreateText(path);
        }

        /// <summary>
        /// Write one lane.
        /// </summary>
        /// <param name="record">The lane.</param>
        public void Write(LaneRecord record)
        {
            if ((record.Path == null) || (record.Path.Count < 2)) return;

            List<double[]> path = new(record.Path.Count);
            foreach (float3 point in record.Path) path.Add(BundlePrecision.Vector(point));

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

        /// <summary>Close the file.</summary>
        public void Dispose() => _writer?.Dispose();
    }
}
