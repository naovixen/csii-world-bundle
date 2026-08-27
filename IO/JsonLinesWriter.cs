using System;
using System.Collections.Generic;
using System.IO;

using Newtonsoft.Json;

namespace Carto.IO
{
    /// <summary>
    /// A bundle layer written as JSON Lines — one self-contained JSON object per line, streamed.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The format is what the big layers need: a full city has 1.28 M instances, 680 k lanes and
    /// 27 k network edges, and neither the producer nor a consumer should have to hold a layer in
    /// memory to write or read it. One line is one record; a reader can stop anywhere.
    /// </para>
    /// <para>
    /// <c>InstanceWriter</c>, <c>LaneWriter</c> and <c>NetworkWriter</c> were the same class three
    /// times, and they said so themselves — <c>LaneWriter</c>'s summary opened "The same shape as
    /// <see cref="NetworkWriter"/> and for the same reason". What was identical is exactly this:
    /// the stream, the count, opening through <see cref="BundleFile"/> so the layer shares the
    /// bundle's one on-disk policy, serialising with no formatting, and disposing. What differs is
    /// only which fields a record contributes, which is what each subclass is now.
    /// </para>
    /// <para>
    /// <c>IO/BundlePrecision.cs</c> acted on the same signal one level down: "a constant whose
    /// copies have to describe each other wants to be one constant".
    /// </para>
    /// </remarks>
    public abstract class JsonLinesWriter : IDisposable
    {
        /// <summary>The underlying writer.</summary>
        private readonly StreamWriter _writer;

        /// <summary>The number of records written so far.</summary>
        public int Count { get; private set; }

        /// <summary>
        /// Open a JSON Lines file for writing.
        /// </summary>
        /// <remarks>
        /// Through <see cref="BundleFile"/> so this layer shares the bundle's one on-disk policy —
        /// currently uncompressed; COMP-1 tried gzip here and was reverted (see
        /// <see cref="BundleFile"/> for why).
        /// </remarks>
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.</param>
        protected JsonLinesWriter(string path)
        {
            _writer = BundleFile.CreateText(path);
        }

        /// <summary>
        /// Serialise one record onto its own line and count it.
        /// </summary>
        /// <remarks>
        /// Counting here rather than at the call site is the point: a subclass that returns early
        /// — <c>LaneWriter</c> drops a path of fewer than two points — must not have counted, and
        /// one that wrote must. Tying the two together makes that impossible to get wrong.
        /// </remarks>
        /// <param name="line">The record's fields, in the order they should appear.</param>
        protected void WriteLine(Dictionary<string, object> line)
        {
            _writer.WriteLine(JsonConvert.SerializeObject(line, Formatting.None));
            Count++;
        }

        /// <summary>Flush and close the file.</summary>
        public void Dispose() => _writer?.Dispose();
    }
}
