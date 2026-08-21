using System;
using System.IO;
using System.Text;
using Carto.Domain;
using Carto.IO;
using Xunit;

namespace Carto.Tests
{
    /// <summary>
    /// <c>IO/BundleFile.cs</c> — the one place every layer is opened, and the only thing keeping
    /// contract §1's two hard rules true: LF line endings and UTF-8 with no BOM.
    ///
    /// Both are invisible until a consumer breaks. A BOM corrupts the first record of every
    /// JSONL layer for anything that reads line by line; CRLF the contract forbids outright. The
    /// producer has no test that would notice either, and refactor Phase 8 moves six layers that
    /// currently bypass this class INTO it — so this is the check that the move preserved them.
    /// </summary>
    public class BundleFileTests : IDisposable
    {
        private readonly string _root =
            Path.Combine(Path.GetTempPath(), "carto-tests-" + Guid.NewGuid().ToString("N"));

        public void Dispose()
        {
            if (Directory.Exists(_root)) Directory.Delete(_root, recursive: true);
            GC.SuppressFinalize(this);
        }

        private string Path_(params string[] parts) =>
            Path.Combine(new[] { _root }.Concat_(parts));

        [Fact]
        public void TextLayersAreUtf8WithNoBomAndLfEndings()
        {
            string path = Path_("layer.jsonl");
            using (StreamWriter writer = BundleFile.CreateText(path))
            {
                writer.WriteLine("{\"id\":1}");
                writer.WriteLine("{\"id\":2,\"name\":\"é中\"}");
            }

            byte[] bytes = File.ReadAllBytes(path);

            Assert.False(
                bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF,
                "a BOM would corrupt the first record for every line-by-line consumer");
            Assert.DoesNotContain((byte)0x0D, bytes);
            Assert.Equal(2, System.Linq.Enumerable.Count(bytes, b => b == 0x0A));
            // Non-ASCII survives as UTF-8, not as '?' from an ANSI fallback.
            Assert.Contains("中", Encoding.UTF8.GetString(bytes));
        }

        [Fact]
        public void ADirectoryIsCreatedForANestedLayer()
        {
            // meshes/, networks/, textures/ and terrain/ are all written this way, and the rip
            // starts against a directory that does not exist yet.
            string path = Path_("meshes", "deep", "asset.glb");
            long written = BundleFile.WriteAllBytes(path, new byte[] { 1, 2, 3, 4 });

            Assert.Equal(4, written);
            Assert.True(File.Exists(path));
            Assert.Equal(new byte[] { 1, 2, 3, 4 }, File.ReadAllBytes(path));
        }

        [Fact]
        public void CreateStreamAlsoCreatesTheDirectory()
        {
            string path = Path_("terrain", "surfaces", "x.png");
            using (Stream stream = BundleFile.CreateStream(path))
            {
                stream.WriteByte(7);
            }

            Assert.Equal(new byte[] { 7 }, File.ReadAllBytes(path));
        }

        [Fact]
        public void WriteAllBytesRefusesNullRatherThanWritingAnEmptyLayer()
        {
            Assert.Throws<ArgumentNullException>(() => BundleFile.WriteAllBytes(Path_("x"), null));
        }

        [Fact]
        public void ARipWarningCarriesTheThreeThingsAConsumerNeeds()
        {
            // Contract §10: a declared gap validates, an undeclared one does not. The consumer
            // matches on all three fields, so a constructor that dropped one would produce a
            // warning that cannot be joined to the record that referenced the missing asset.
            RipWarning warning = new("mesh", "asset_1234", "prefab has no RenderPrefab");

            Assert.Equal("mesh", warning.Kind);
            Assert.Equal("asset_1234", warning.AssetId);
            Assert.Equal("prefab has no RenderPrefab", warning.Reason);
        }
    }

    internal static class PathParts
    {
        /// <summary>`Path.Combine(root, parts...)` without allocating a list at every call site.</summary>
        internal static string[] Concat_(this string[] head, string[] tail)
        {
            string[] all = new string[head.Length + tail.Length];
            head.CopyTo(all, 0);
            tail.CopyTo(all, head.Length);
            return all;
        }
    }
}
