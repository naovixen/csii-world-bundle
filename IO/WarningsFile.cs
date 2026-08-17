using Colossal.Logging;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The bundle's <c>warnings.jsonl</c> (contract §10), and the one rule that makes it safe for
    /// the rip stages to run in any order: <b>kinds are owned per pass</b>.
    /// <para>
    /// The file is shared by five writers — the instance pass owns <c>prefab</c> and
    /// <c>instance</c>, the mesh pass <c>mesh</c>/<c>material</c>/<c>texture</c>, the lane pass
    /// <c>lane</c>, the network pass every kind prefixed <c>network</c>, and the terrain pass
    /// every kind prefixed <c>terrain</c>. Each rewrite keeps
    /// every line it does not own and replaces every line it does, so re-running one stage
    /// neither doubles another's declarations nor erases them.
    /// </para>
    /// <para>
    /// <b>Both halves of that rule have already been broken, in opposite directions.</b>
    /// Truncating instead of merging made the file order-dependent: a bundle ripped meshes-first
    /// reported 118 declarations and shipped 87, and every erased line became an undeclared gap —
    /// a contract violation manufactured by the writer rather than by anything missing. Appending
    /// instead of replacing made a bundle ripped twice declare every gap twice, and left a gap
    /// since closed declared forever. This type exists so the protocol has one implementation
    /// rather than four copies that can drift apart one at a time.
    /// </para>
    /// <para>
    /// The network prefix is load-bearing in its own right: a network warning names a
    /// cross-section piece, which is not an <c>assetId</c> in <c>meshes/</c> at all, so filing it
    /// as <c>mesh</c> would both mislead a consumer and let the next mesh rip delete it.
    /// </para>
    /// </summary>
    public static class WarningsFile
    {
        /// <summary>The layer's file name at the bundle root.</summary>
        public const string FileName = "warnings.jsonl";

        /// <summary>The kinds the instance pass owns.</summary>
        public static readonly string[] InstanceKinds = { "prefab", "instance" };

        /// <summary>The kinds the mesh pass owns.</summary>
        public static readonly string[] MeshKinds = { "mesh", "material", "texture" };

        /// <summary>The kinds the lane pass owns.</summary>
        public static readonly string[] LaneKinds = { "lane" };

        /// <summary>The prefix every kind the network stage writes carries.</summary>
        public const string NetworkKindPrefix = "network";

        /// <summary>The prefix every kind the terrain stage writes carries (<c>terrainFine</c>).</summary>
        public const string TerrainKindPrefix = "terrain";

        private static readonly ILog _log = Instance.Log;

        /// <summary>
        /// Whether a line declares one of <paramref name="kinds"/>.
        /// Matched on the declared kind rather than by parsing the whole record: a line this
        /// process cannot read is someone else's and is kept.
        /// </summary>
        /// <param name="line">One line of the file.</param>
        /// <param name="kinds">The kinds to match.</param>
        /// <returns>Whether the line declares one of them.</returns>
        public static bool IsKind(string line, params string[] kinds)
        {
            string kind = ReadKind(line);
            if (kind == null) return false;

            foreach (string candidate in kinds)
            {
                if (kind == candidate) return true;
            }

            return false;
        }

        /// <summary>
        /// Whether a line's declared kind starts with <paramref name="prefix"/> — the network
        /// stage's ownership test.
        /// </summary>
        /// <param name="line">One line of the file.</param>
        /// <param name="prefix">The kind prefix.</param>
        /// <returns>Whether the line declares a kind under that prefix.</returns>
        public static bool IsKindPrefixed(string line, string prefix)
        {
            string kind = ReadKind(line);
            return (kind != null) && kind.StartsWith(prefix, StringComparison.Ordinal);
        }

        /// <summary>
        /// Rewrite the file, keeping every line the calling pass does not own and replacing every
        /// line it does.
        /// </summary>
        /// <param name="bundleRoot">The bundle root the file sits at.</param>
        /// <param name="owns">Whether a kept line belongs to the calling pass.</param>
        /// <param name="records">This pass's declarations, in write order.</param>
        /// <returns>How many foreign lines were kept, or <c>-1</c> if the file could not be
        /// written — the callers report the count and stay silent on failure, exactly as they did
        /// when each owned its own copy of this.</returns>
        public static int Rewrite(string bundleRoot, Func<string, bool> owns,
                                  IEnumerable<Dictionary<string, object>> records)
        {
            string path = Utils.IOUtils.CombinePath(bundleRoot, FileName);

            try
            {
                if (!string.IsNullOrEmpty(bundleRoot) && !Directory.Exists(bundleRoot))
                    Directory.CreateDirectory(bundleRoot);

                List<string> kept = new();
                if (File.Exists(path))
                {
                    foreach (string line in File.ReadAllLines(path))
                    {
                        if (string.IsNullOrWhiteSpace(line)) continue;
                        if (!owns(line)) kept.Add(line);
                    }
                }

                using (StreamWriter writer = new(path, false, new UTF8Encoding(false)) { NewLine = "\n" })
                {
                    foreach (string line in kept) writer.WriteLine(line);
                    foreach (Dictionary<string, object> record in records)
                    {
                        writer.WriteLine(JsonConvert.SerializeObject(record, Formatting.None));
                    }
                }

                return kept.Count;
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write {FileName}: {ex.Message}");
                return -1;
            }
        }

        /// <summary>
        /// Build one declaration naming an asset.
        /// </summary>
        /// <param name="kind">The warning kind.</param>
        /// <param name="assetId">The id or name a consumer would have looked for.</param>
        /// <param name="reason">Why it is not there.</param>
        /// <returns>The record.</returns>
        public static Dictionary<string, object> ByAsset(string kind, string assetId, string reason)
        {
            return new Dictionary<string, object>
            {
                ["kind"] = kind,
                ["assetId"] = assetId,
                ["reason"] = reason
            };
        }

        /// <summary>
        /// Build one declaration naming an entity rather than an asset — the two kinds
        /// (<c>instance</c>, <c>lane</c>) whose subject was removed by the simulation mid-rip and
        /// no longer resolves to a prefab to ask for (§10).
        /// </summary>
        /// <param name="kind">The warning kind.</param>
        /// <param name="id">The would-be record's <c>id</c>.</param>
        /// <param name="reason">Why it is not there.</param>
        /// <returns>The record.</returns>
        public static Dictionary<string, object> ById(string kind, int id, string reason)
        {
            return new Dictionary<string, object>
            {
                ["kind"] = kind,
                ["id"] = id,
                ["reason"] = reason
            };
        }

        /// <summary>
        /// The <c>kind</c> a line declares, or <c>null</c> when the line cannot be read.
        /// </summary>
        /// <param name="line">One line of the file.</param>
        /// <returns>The declared kind.</returns>
        private static string ReadKind(string line)
        {
            try
            {
                return JObject.Parse(line)["kind"]?.ToString();
            }
            catch (Exception)
            {
                return null;
            }
        }
    }
}
