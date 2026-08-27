using System.Collections.Generic;
using System.IO;

using Carto.IO;

using Xunit;

namespace Carto.Tests
{
    /// <summary>
    /// The JSON Lines contract the instance, lane and network layers are written under.
    /// </summary>
    /// <remarks>
    /// These three layers are the bulk of a bundle — 1.28 M instances, 680 k lanes, 27 k network
    /// edges on the reference city — and until refactor Phase 8 the code writing them was the
    /// same class three times, with no test on any copy. The invariant that matters to a consumer
    /// is that one line is one record and `Count` is how many there are: a reader streams the
    /// file and a manifest states the count, so a writer that counted a record it did not write
    /// produces a bundle whose manifest disagrees with its own layer.
    /// </remarks>
    public class JsonLinesWriterTests
    {
        /// <summary>A writer that puts whatever it is given on a line — the base, exercised.</summary>
        private sealed class Probe : JsonLinesWriter
        {
            public Probe(string path) : base(path) { }

            /// <summary>Write a record; returns false without counting when there is nothing to write.</summary>
            public bool Write(Dictionary<string, object> line)
            {
                if (line == null) return false;   // the shape LaneWriter uses to drop a short path
                WriteLine(line);
                return true;
            }
        }

        private static string TempPath() =>
            Path.Combine(Path.GetTempPath(), "carto-jsonl-" + Path.GetRandomFileName());

        [Fact]
        public void OneRecordPerLineAndCountMatches()
        {
            string path = TempPath();
            try
            {
                using (Probe writer = new(path))
                {
                    writer.Write(new Dictionary<string, object> { ["id"] = 1, ["kind"] = "tree" });
                    writer.Write(new Dictionary<string, object> { ["id"] = 2, ["kind"] = "prop" });
                    Assert.Equal(2, writer.Count);
                }

                string[] lines = File.ReadAllLines(path);
                Assert.Equal(2, lines.Length);
                Assert.Equal("{\"id\":1,\"kind\":\"tree\"}", lines[0]);
                Assert.Equal("{\"id\":2,\"kind\":\"prop\"}", lines[1]);
            }
            finally { File.Delete(path); }
        }

        [Fact]
        public void ARecordThatIsNotWrittenIsNotCounted()
        {
            // The reason `Count` lives in the base beside `WriteLine` rather than at the call
            // site: `LaneWriter` returns early on a path of fewer than two points, and a bundle
            // whose manifest counts that record has a manifest that disagrees with its layer.
            string path = TempPath();
            try
            {
                using (Probe writer = new(path))
                {
                    writer.Write(new Dictionary<string, object> { ["id"] = 1 });
                    Assert.False(writer.Write(null));
                    writer.Write(new Dictionary<string, object> { ["id"] = 2 });
                    Assert.Equal(2, writer.Count);
                }

                Assert.Equal(2, File.ReadAllLines(path).Length);
            }
            finally { File.Delete(path); }
        }

        [Fact]
        public void FieldOrderIsTheOrderTheRecordWasBuilt()
        {
            // Not cosmetic: the layers are diffed between rips (`tools/bundle_manifest.py`), and
            // a writer that reordered its keys would report every record as changed.
            string path = TempPath();
            try
            {
                using (Probe writer = new(path))
                {
                    writer.Write(new Dictionary<string, object>
                    {
                        ["id"] = 7,
                        ["kind"] = "segment",
                        ["assetId"] = "seg_7",
                    });
                }

                Assert.Equal("{\"id\":7,\"kind\":\"segment\",\"assetId\":\"seg_7\"}",
                             File.ReadAllText(path).TrimEnd('\r', '\n'));
            }
            finally { File.Delete(path); }
        }

        [Fact]
        public void EmptyLayerIsAnEmptyFileNotAMissingOne()
        {
            // A rip that found nothing must still leave the file: a consumer distinguishes "no
            // districts in this city" from "the districts stage did not run", and §10 says an
            // absent layer is the second.
            string path = TempPath();
            try
            {
                using (Probe writer = new(path))
                {
                    Assert.Equal(0, writer.Count);
                }

                Assert.True(File.Exists(path));
                Assert.Equal(0, new FileInfo(path).Length);
            }
            finally { File.Delete(path); }
        }

        [Fact]
        public void NoByteOrderMarkAndUnixLineEndings()
        {
            // The same rule `BundleFileTests` pins for the bundle's other text: a consumer
            // splitting on '\n' must not find a stray '\r', and a BOM is not JSON.
            string path = TempPath();
            try
            {
                using (Probe writer = new(path))
                {
                    writer.Write(new Dictionary<string, object> { ["id"] = 1 });
                    writer.Write(new Dictionary<string, object> { ["id"] = 2 });
                }

                byte[] bytes = File.ReadAllBytes(path);
                Assert.NotEqual(0xEF, bytes[0]);
                Assert.DoesNotContain((byte)'\r', bytes);
                Assert.Equal((byte)'\n', bytes[bytes.Length - 1]);
            }
            finally { File.Delete(path); }
        }
    }
}
