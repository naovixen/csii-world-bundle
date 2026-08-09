using System;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The one place a bulk bundle layer reaches the disk.
    /// <c>meshes/</c>, <c>networks/</c>, <c>instances.jsonl</c>, <c>lanes.jsonl</c> and
    /// <c>areas.jsonl</c> all pass through here, so the line endings and encoding a JSON Lines
    /// layer owes contract §1 are decided once rather than in each writer.
    /// <b>Layers are written uncompressed.</b> They were briefly gzip-9 (COMP-1), which shrank the
    /// bundle by roughly half but put a slow deflate on the rip's critical path: the mesh writer is
    /// a single background thread feeding a bounded queue, so its throughput gates the main thread's
    /// per-frame slice (<see cref="Carto.Systems.MeshRipSystem"/>, <c>QueueCapacity</c>). Level 9
    /// runs at ~3.3 MiB/s on GLB float buffers — near-random data whose matches deflate cannot find
    /// — for 0.66 points of ratio over level 4, and the rip is a manual, repeated, in-game step
    /// whose wall-clock is the scarce resource here. Disk is not.
    /// Consumers still read either form — a bundle ripped while COMP-1 was live is on disk, and
    /// contract §12.1 keeps both storage forms valid schema 1. Re-enabling compression is a change
    /// to this one file; if it comes back, it comes back at level 6 or below.
    /// </summary>
    public static class BundleFile
    {
        /// <summary>
        /// The on-disk path for a layer's logical path — the same path, layers being uncompressed.
        /// </summary>
        /// <param name="logicalPath">The layer's name, e.g. <c>…/instances.jsonl</c>.</param>
        /// <returns>The path actually written.</returns>
        public static string OnDisk(string logicalPath) => logicalPath;

        /// <summary>
        /// Open a text layer for writing, with the line endings and encoding the contract requires
        /// of every JSON Lines layer.
        /// </summary>
        /// <param name="logicalPath">The layer's name.</param>
        /// <returns>A writer over the file.</returns>
        public static StreamWriter CreateText(string logicalPath)
        {
            // LF and UTF-8 without a BOM: a BOM would corrupt the first record for any consumer
            // that reads the file line by line, and CRLF is what contract §1 forbids outright.
            //
            return new StreamWriter(CreateStream(logicalPath), new UTF8Encoding(false)) { NewLine = "\n" };
        }

        /// <summary>
        /// Write a whole payload as a layer.
        /// </summary>
        /// <param name="logicalPath">The layer's name.</param>
        /// <param name="payload">The bytes to write.</param>
        /// <returns>The number of bytes actually written to disk.</returns>
        public static long WriteAllBytes(string logicalPath, byte[] payload)
        {
            if (payload == null) throw new ArgumentNullException(nameof(payload));

            EnsureDirectory(logicalPath);
            File.WriteAllBytes(logicalPath, payload);

            return payload.LongLength;
        }

        /// <summary>
        /// Open the byte stream for a layer, creating its directory if needed.
        /// </summary>
        /// <param name="logicalPath">The layer's name.</param>
        /// <returns>A stream over the file.</returns>
        public static Stream CreateStream(string logicalPath)
        {
            EnsureDirectory(logicalPath);
            return File.Create(logicalPath);
        }

        /// <summary>
        /// Create the layer's containing directory if it is not there yet.
        /// </summary>
        /// <param name="path">The layer's path.</param>
        private static void EnsureDirectory(string path)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);
        }
    }
}
