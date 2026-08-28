using System.Collections.Generic;

namespace Carto.IO
{
    /// <summary>
    /// A JSON Lines layer whose records are already dictionaries.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <see cref="JsonLinesWriter"/>'s three subclasses each build a record from a typed domain
    /// object, which is why it is abstract. Three layers have no such type: <c>districts.jsonl</c>,
    /// <c>areas.jsonl</c> and <c>clipvolumes.jsonl</c> assemble a
    /// <c>Dictionary&lt;string, object&gt;</c> inline and used to write it with their own
    /// <c>BundleFile.CreateText</c> + <c>JsonConvert.SerializeObject</c> pair — the IO layer
    /// bypassed, three more times.
    /// </para>
    /// <para>
    /// Routing them through here buys the same three things the typed writers get: the bundle's
    /// one on-disk policy through <see cref="BundleFile"/>, one place where the line format is
    /// decided, and <see cref="JsonLinesWriter.Count"/> counting a line that was WRITTEN rather
    /// than a record that was considered. The third matters because the manifest reports these
    /// layers' record counts, and a count kept by hand beside a loop with two <c>continue</c>s in
    /// it is a count that will eventually disagree with the file.
    /// </para>
    /// </remarks>
    public sealed class RecordWriter : JsonLinesWriter
    {
        /// <summary>
        /// Open a JSON Lines layer for writing.
        /// </summary>
        /// <param name="path">The destination path, before <see cref="BundleFile"/>'s suffix.</param>
        public RecordWriter(string path) : base(path) { }

        /// <summary>
        /// Write one record on its own line and count it.
        /// </summary>
        /// <param name="record">The record's fields, in the order they should appear.</param>
        public void Write(Dictionary<string, object> record) => WriteLine(record);
    }
}
