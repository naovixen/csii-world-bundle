using Carto.Domain;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Streams <see cref="InstanceRecord"/>s to a JSON Lines file.
    /// Streaming rather than building a document: a dense city runs to hundreds of thousands of
    /// instances, and the bundle contract picks JSON Lines precisely so neither producer nor
    /// consumer has to hold them all at once.
    /// </summary>
    public sealed class InstanceWriter : IDisposable
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
        /// The number of rotations that had to be normalised before writing.
        /// </summary>
        public int NormalizedRotations { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// </summary>
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.</param>
        public InstanceWriter(string path)
        {
            // Through BundleFile so this layer shares the bundle's one on-disk policy — currently
            // uncompressed; COMP-1 tried gzip here and was reverted (see BundleFile for why).
            _writer = BundleFile.CreateText(path);
        }

        /// <summary>
        /// Write one record as a single line.
        /// </summary>
        /// <param name="record">The record to write.</param>
        public void Write(InstanceRecord record)
        {
            Dictionary<string, object> line = new()
            {
                ["id"] = record.Id,
                ["assetId"] = record.AssetId,
                ["kind"] = record.Kind,
                ["pos"] = BundlePrecision.Vector(record.Position),
                ["rot"] = BundlePrecision.Rotation(Normalize(record.Rotation))
            };

            if (record.Scale.HasValue) line["scale"] = BundlePrecision.Vector(record.Scale.Value);
            if (record.Parent.HasValue) line["parent"] = record.Parent.Value;
            if ((record.Attributes != null) && (record.Attributes.Count > 0)) line["attrs"] = record.Attributes;

            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>
        /// Return a unit-length rotation, normalising if the game's value has drifted.
        /// The contract requires unit quaternions and a validator enforces it, so guaranteeing that
        /// here is the producer's job. A degenerate rotation is replaced with identity rather than
        /// written as NaN, which would make the whole line unparseable.
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
        /// Flush and close the file.
        /// </summary>
        public void Dispose() => _writer?.Dispose();
    }
}
