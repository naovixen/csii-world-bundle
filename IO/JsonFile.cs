using System.IO;
using System.Text;

using Newtonsoft.Json;

namespace Carto.IO
{
    /// <summary>
    /// A bundle layer written as ONE JSON document — the counterpart to
    /// <see cref="JsonLinesWriter"/>, which is for the layers too big to hold in memory.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Eight fork sites wrote a JSON document with their own <c>File.WriteAllText</c> +
    /// <c>JsonConvert.SerializeObject</c> pair: <c>materials.json</c>, <c>colors.json</c>,
    /// <c>lights.json</c>, <c>laneprefabs.json</c>, <c>areaprefabs.json</c>,
    /// <c>terrain/rasters.json</c>, <c>terrain/surfaces/index.json</c>,
    /// <c>terrain/surfaces/sources.json</c>, plus <c>grading.json</c> and the manifest. They
    /// agreed on the idiom by habit rather than by construction, and two of them did not:
    /// </para>
    /// <list type="bullet">
    /// <item><c>laneprefabs.json</c> passed no <c>Formatting</c> at all, so it was the one
    /// minified document in a bundle of indented ones — settled here, and declared in the
    /// commit that did it because it changes the bytes on disk.</item>
    /// <item>only <c>materials.json</c> named an encoding (<c>UTF8Encoding(false)</c>). The rest
    /// relied on <c>WriteAllText</c>'s default being BOM-less, which it is on .NET Core and is
    /// not a thing to leave to a default: a BOM in a bundle file breaks a consumer that reads
    /// bytes rather than text.</item>
    /// </list>
    /// </remarks>
    internal static class JsonFile
    {
        /// <summary>UTF-8 with no byte-order mark — stated, not inherited from a default.</summary>
        private static readonly UTF8Encoding Utf8NoBom = new UTF8Encoding(false);

        /// <summary>
        /// The bundle's one JSON-document idiom, as text.
        /// </summary>
        /// <remarks>
        /// Separate from <see cref="Write"/> for the one caller that cannot use it: the manifest
        /// is written to a temporary file and swapped in with <c>File.Replace</c>, so that a rip
        /// interrupted mid-write leaves the previous <c>bundle.json</c> intact rather than half
        /// of the new one.
        /// </remarks>
        /// <param name="value">The object to serialise.</param>
        /// <returns>The serialised document.</returns>
        public static string Serialize(object value) => JsonConvert.SerializeObject(value, Formatting.Indented);

        /// <summary>
        /// Write one JSON document, creating its directory if it is not there yet.
        /// </summary>
        /// <param name="path">The destination path.</param>
        /// <param name="value">The object to serialise.</param>
        public static void Write(string path, object value)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
                Directory.CreateDirectory(directory);

            File.WriteAllText(path, Serialize(value), Utf8NoBom);
        }
    }
}
