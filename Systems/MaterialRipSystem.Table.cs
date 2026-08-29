using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// <c>materials.json</c>: the bundle-wide table, merged over whatever a previous stage left.
    /// </summary>
    /// <remarks>
    /// The table is bundle-wide but its producers are not — meshes and networks are separate
    /// buttons, each ripping the materials of what it touches. Overwriting would leave a bundle
    /// whose every building names a material that is no longer in the file; every glTF material
    /// name has to stay a key (§6).
    /// </remarks>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Drain the encoder, write <c>materials.json</c>, and report what came out.
        /// </summary>
        /// <param name="report">The report to append to.</param>
        /// <param name="warnings">Receives everything that produced no entry, for <c>warnings.jsonl</c>.</param>
        /// <returns>A one-line summary for the result dialog.</returns>
        public string Finish(StringBuilder report, List<RipWarning> warnings)
        {
            MaterialRun run = _run;
            _run = null;
            if (run == null) return null;

            while (run.Textures.Skipped.TryDequeue(out RipWarning skipped)) run.Skipped.Add(skipped);

            int written = WriteTable(run);

            report.AppendLine();
            // The table can hold more entries than there were surfaces: an unresolvable surface is
            // still declared, so that a mesh naming it does not dangle.
            //
            report.AppendLine($"Materials: {written} entries in materials.json from {run.Surfaces} surface(s), failed {run.Failed}, disambiguated {run.Disambiguated}.");
            report.AppendLine($"Textures: {run.TexturesRead} read back, {run.Textures.Written} file(s), {run.Textures.Deduped} duplicate(s) by content hash, failed {run.Textures.Failed}.");
            report.AppendLine($"Texture bytes: {run.Textures.Bytes / (1024d * 1024d):F1} MiB; readback held the main thread {run.ReadbackMilliseconds / 1000d:F1} s.");

            // The four buckets every distinct texture reference lands in. Read these before
            // concluding anything about the texture layer — they say which problem this is.
            //
            int references = run.TexturesBound + run.TexturesRebound + run.TexturesVirtual + run.TexturesUnbound;
            report.AppendLine($"Texture references: {references} distinct — {run.TexturesBound} loadable as given, "
                            + $"{run.TexturesRebound} rebound by guid, {run.TexturesVirtual} virtual-textured "
                            + $"({run.TexturesFromStack} recovered from a shared stack, "
                            + $"{run.TexturesFromSurfaceData} from the surface's own VT data), "
                            + $"{run.TexturesUnbound} with nothing behind them; "
                            + $"{run.TexturesFromMaterial} taken off a live material as a last resort.");
            report.AppendLine($"Surface VT data: {run.SurfaceDataLoads} file(s) opened, {run.SurfaceDataFailures} failed to decode."
                + (run.SurfaceDataGuarded > 0
                    ? $" {run.SurfaceDataGuarded} skipped by vt_crash_guard.txt (crashed the game in a previous rip)."
                    : ""));

            // The colour mask is what makes colors.json applicable, so say plainly whether it came
            // out. Zero here does not mean the city has no colour variation — it means this code is
            // looking in the wrong place, and the line below says where to look instead.
            //
            report.AppendLine($"Colour masks: {run.ColorMasks} of {written} material(s) carry one "
                            + "(the texture colors.json needs to be applicable at all).");

            if (run.TemplateProperties.Count > 0)
            {
                List<string> properties = new(run.TemplateProperties);
                properties.Sort(StringComparer.Ordinal);

                List<string> unexported = new();
                foreach (string property in properties)
                    if (Array.IndexOf(Maps, property) < 0) unexported.Add(property);

                report.AppendLine($"Surface template properties declared by the material library "
                                + $"({properties.Count}): {string.Join(", ", properties)}");
                if (unexported.Count > 0)
                {
                    report.AppendLine($"  not exported by the contract's five: {string.Join(", ", unexported)}");
                }
            }

            if (run.Skipped.Count > 0)
            {
                report.AppendLine($"Skipped {run.Skipped.Count}:");
                for (int i = 0; (i < 20) && (i < run.Skipped.Count); i++)
                    report.AppendLine($"  - {run.Skipped[i].Kind} {run.Skipped[i].AssetId}: {run.Skipped[i].Reason}");
            }

            warnings?.AddRange(run.Skipped);

            return $"{written} material(s), {run.Textures.Written} texture(s), {run.Textures.Bytes / (1024d * 1024d):F1} MiB.";
        }

        /// <summary>
        /// Write <c>materials.json</c>, resolving every texture key to the file its content hash
        /// named.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>The number of materials written.</returns>
        private int WriteTable(MaterialRun run)
        {
            string path = IOUtils.CombinePath(run.BundleRoot, "materials.json");

            // The table is bundle-wide but its producers are not: meshes and networks are separate
            // buttons, each ripping the materials of what it touches. Overwriting would mean a
            // network rip after a mesh rip leaves a bundle whose every building names a material
            // that is no longer in the file — every glTF material name has to stay a key (§6).
            // Keys this run resolved win, since they were read from the live game just now.
            //
            Dictionary<string, object> table = ReadTable(path);
            foreach (KeyValuePair<string, MaterialAsset> entry in run.Materials)
                table[entry.Key] = entry.Value.ToJson(run.Textures.Resolve);

            try
            {
                Carto.IO.JsonFile.Write(path, table);
            }
            catch (Exception ex)
            {
                _log.Error($"Could not write materials.json: {ex}");
                return 0;
            }

            return table.Count;
        }

        /// <summary>
        /// Read the material table a previous phase left on disk, or an empty one.
        /// A file that will not parse is treated as absent rather than fatal: losing a phase's
        /// entries is bad, but refusing to write this phase's would lose both.
        /// </summary>
        /// <param name="path">The <c>materials.json</c> path.</param>
        /// <returns>The entries already there.</returns>
        private static Dictionary<string, object> ReadTable(string path)
        {
            try
            {
                if (!System.IO.File.Exists(path)) return new Dictionary<string, object>();

                Dictionary<string, object> existing =
                    JsonConvert.DeserializeObject<Dictionary<string, object>>(System.IO.File.ReadAllText(path));

                return existing ?? new Dictionary<string, object>();
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the existing materials.json, starting a new table: {ex.Message}");
                return new Dictionary<string, object>();
            }
        }

        /// <summary>
        /// A stable, session-independent identity for an asset.
        /// The asset identifier is unique per asset; a runtime-generated asset has none, in which
        /// case its name plus type is the best available and still distinguishes it from every
        /// other asset that has one.
        /// </summary>
        /// <param name="asset">The asset.</param>
        /// <returns>The identity string.</returns>
        private static string Identity(AssetData asset)
        {
            try
            {
                string identifier = asset.identifier;
                if (!string.IsNullOrEmpty(identifier)) return identifier;
            }
            catch (Exception)
            {
                // An asset with no backing file throws rather than returning null.
                //
            }

            return $"{asset.GetType().Name}:{asset.name}";
        }

        /// <summary>
        /// A short, stable digest, matching <see cref="MeshRipSystem"/>'s so that both layers
        /// disambiguate the same way.
        /// </summary>
        /// <param name="value">The string to digest.</param>
        /// <returns>Eight lowercase hex digits.</returns>
        private static string Digest(string value)
        {
            uint hash = 2166136261u;
            foreach (char c in value)
            {
                hash ^= c;
                hash *= 16777619u;
            }

            return hash.ToString("x8");
        }
    }
}
