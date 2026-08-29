using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The Phase 7 finalize step: assemble <c>bundle.json</c> from the bundle directory.
    /// The manifest is derived from the <b>bytes on disk</b>, not from the game: the rip runs as
    /// separate steps (instances, meshes, networks, terrain) pressed at different times, and the
    /// manifest's counts must equal what those steps actually left behind — that equality is how a
    /// consumer detects a truncated or stale bundle (contract §3). Only identity and the geo
    /// anchor come from the running game, via <see cref="BundleManifest.Capture"/>.
    /// Runs synchronously on the main thread inside <see cref="RipProgress.RunDeferred"/>, like
    /// the vanilla vector export: it reads a few hundred megabytes of JSON Lines and every mesh's
    /// GLB header, roughly half a minute on a full city. It writes exactly one file and never
    /// touches the layers.
    /// </summary>
    public static partial class BundleWriter
    {

        /// <summary>
        /// Assemble and write <c>bundle.json</c> for the bundle at <paramref name="outputDirectory"/>.
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <returns>A human-readable report.</returns>
        public static string WriteManifest(string outputDirectory)
        {
            return WriteManifest(outputDirectory, BundleManifest.Capture());
        }

        /// <summary>
        /// The directory-scanning half of <see cref="WriteManifest(string)"/>: everything here is
        /// file I/O over what the rip left on disk, so it may run on any thread — the pipeline
        /// captures the <paramref name="snapshot"/> on the main thread (game state is main-thread
        /// affair) and runs this half under <c>Task.Run</c> (PLAN Phase 8).
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="snapshot">The game-state metadata, captured on the main thread.</param>
        /// <returns>A human-readable report.</returns>
        public static string WriteManifest(string outputDirectory, BundleManifest snapshot)
        {
            if (!Directory.Exists(outputDirectory))
                throw new DirectoryNotFoundException($"No bundle at {outputDirectory}.");

            StringBuilder report = new();

            // ---- 1. Which layers exist.
            Dictionary<string, string> layers = new();
            foreach (LayerDef layer in Layers)
            {
                string path = Utils.IOUtils.CombinePath(outputDirectory, layer.Path.TrimEnd('/'));
                bool present = layer.IsDirectory
                    ? Directory.Exists(path) && (Directory.GetFileSystemEntries(path).Length > 0)
                    : File.Exists(path);
                if (present) layers[layer.Key] = layer.Path;
            }
            if (layers.Count == 0) throw new InvalidOperationException("The directory holds no bundle layer at all — rip something first.");

            // ---- 2. Mesh extents, for bounds. The slowest part of the scan — thousands of glb
            // headers — so it reports real progress; the record scans after it fall back to the
            // indeterminate caption. Without this the finalize step read as a hang.
            //
            string meshDirectory = Utils.IOUtils.CombinePath(outputDirectory, "meshes");
            string networkDirectory = Utils.IOUtils.CombinePath(outputDirectory, "networks");
            int scanned = 0;
            int totalGlbs = CountFiles(meshDirectory, "*.glb") + CountFiles(networkDirectory, "*.glb");

            Dictionary<string, Aabb> meshBounds = ReadMeshBounds(meshDirectory, report, ref scanned, totalGlbs);
            Dictionary<string, Aabb> networkMeshBounds = ReadMeshBounds(networkDirectory, report, ref scanned, totalGlbs);

            RipProgress.Report(RipProgress.Phase.Manifest);

            // ---- 3. Record layers: counts and bounds in one pass each.
            Aabb bounds = Aabb.Empty;
            long instanceCount = 0, networkCount = 0, areaCount = 0, clipCount = 0, districtCount = 0;

            if (layers.ContainsKey("instances"))
            {
                instanceCount = ScanPlacements(Utils.IOUtils.CombinePath(outputDirectory, "instances.jsonl"), meshBounds, ref bounds);
            }
            if (layers.ContainsKey("networks"))
            {
                networkCount = ScanPlacements(Utils.IOUtils.CombinePath(outputDirectory, "networks.jsonl"), networkMeshBounds, ref bounds);
            }
            if (layers.ContainsKey("areas"))
            {
                areaCount = ScanAreas(Utils.IOUtils.CombinePath(outputDirectory, "areas.jsonl"), ref bounds);
            }
            if (layers.ContainsKey("clipVolumes"))
            {
                clipCount = ScanClipVolumes(Utils.IOUtils.CombinePath(outputDirectory, "clipvolumes.jsonl"), ref bounds);
            }
            if (layers.ContainsKey("districts"))
            {
                // Same record shape as an area's, so the same scanner. A district is drawn on the
                // ground the city already covers, so it cannot grow the bounds in practice — it
                // goes through Encapsulate anyway rather than being trusted not to (§3).
                districtCount = ScanAreas(Utils.IOUtils.CombinePath(outputDirectory, "districts.jsonl"), ref bounds);
            }

            // The lane layers carry world-space geometry too, and must contribute to bounds —
            // §3's enclose-all rule, and what makes a lane-only rip able to state bounds at all.
            //
            long laneCount = 0, laneGeometryCount = 0;
            if (layers.ContainsKey("lanes"))
            {
                laneCount = ScanPointRows<LaneRow>(Utils.IOUtils.CombinePath(outputDirectory, "lanes.jsonl"), row => row.path, ref bounds);
            }
            if (layers.ContainsKey("laneGeometry"))
            {
                laneGeometryCount = ScanPointRows<LaneGeometryRow>(Utils.IOUtils.CombinePath(outputDirectory, "lanemeshes.jsonl"), row => row.curve, ref bounds);
            }
            long warningCount = layers.ContainsKey("warnings") ? CountLines(Utils.IOUtils.CombinePath(outputDirectory, "warnings.jsonl")) : 0;

            int materialCount = 0;
            if (layers.ContainsKey("materials"))
            {
                JObject materials = JObject.Parse(File.ReadAllText(Utils.IOUtils.CombinePath(outputDirectory, "materials.json")));
                materialCount = materials.Count;
            }

            int meshFileCount = CountFiles(Utils.IOUtils.CombinePath(outputDirectory, "meshes"), "*.glb");
            int textureFileCount = CountFiles(Utils.IOUtils.CombinePath(outputDirectory, "textures"), "*.png");

            // ---- 4. The rasters block, lifted verbatim from the terrain rip's sidecar; the
            // playable rasters also contribute to bounds (contract §3 — the world backdrop is
            // deliberately excluded).
            JObject rasters = null;
            bool anyRaster = layers.ContainsKey("terrain") || layers.ContainsKey("worldTerrain")
                          || layers.ContainsKey("surfaceIndex") || layers.ContainsKey("water");
            if (anyRaster)
            {
                string sidecar = Utils.IOUtils.CombinePath(outputDirectory, "terrain", "rasters.json");
                if (!File.Exists(sidecar))
                    throw new FileNotFoundException("Raster layers exist but terrain/rasters.json does not — re-run the terrain rip.");
                rasters = JObject.Parse(File.ReadAllText(sidecar));

                EncapsulateRasters(outputDirectory, rasters, layers.ContainsKey("terrain"),
                                   layers.ContainsKey("water"), ref bounds);
            }

            if (!bounds.Any)
                throw new InvalidOperationException("No layer contributed any geometry, so bounds cannot be stated.");

            // ---- 5. Assemble.
            Dictionary<string, object> counts = new()
            {
                ["meshes"] = meshFileCount,
                ["materials"] = materialCount,
                ["textures"] = textureFileCount,
                ["instances"] = instanceCount,
                ["networkSegments"] = networkCount
            };
            if (layers.ContainsKey("warnings")) counts["warnings"] = warningCount;
            if (layers.ContainsKey("lanes")) counts["lanes"] = laneCount;
            if (layers.ContainsKey("laneGeometry")) counts["laneGeometry"] = laneGeometryCount;
            if (layers.ContainsKey("areas")) counts["areas"] = areaCount;
            if (layers.ContainsKey("districts")) counts["districts"] = districtCount;
            if (layers.ContainsKey("clipVolumes")) counts["clipVolumes"] = clipCount;

            Dictionary<string, object> manifest = new()
            {
                ["schema"] = 2,
                ["generator"] = snapshot.Generator,
                ["generatedAt"] = snapshot.GeneratedAt,
                ["game"] = snapshot.Game,
                ["city"] = snapshot.City
            };
            if (!string.IsNullOrEmpty(snapshot.Map)) manifest["map"] = snapshot.Map;

            manifest["frame"] = new Dictionary<string, object>
            {
                ["units"] = "m",
                ["handedness"] = "left",
                ["up"] = "Y",
                ["axes"] = "X=east,Y=up,Z=north",
                ["meshFrame"] = new Dictionary<string, object>
                {
                    ["handedness"] = "right",
                    ["conversion"] = "negate-x",
                    ["windingReversed"] = true
                }
            };
            manifest["geo"] = snapshot.Geo;
            manifest["bounds"] = new Dictionary<string, object>
            {
                // Rounded outward, so the stated bounds still enclose after rounding.
                //
                ["min"] = new[] { Floor3(bounds.MinX), Floor3(bounds.MinY), Floor3(bounds.MinZ) },
                ["max"] = new[] { Ceil3(bounds.MaxX), Ceil3(bounds.MaxY), Ceil3(bounds.MaxZ) }
            };
            manifest["mapSize"] = snapshot.MapSize;
            // Which side the city drives on. Additive, so it stays schema 2 (§12.1's closed-object
            // amnesty is over for CLOSED objects; the manifest root is open).
            manifest["leftHandTraffic"] = snapshot.LeftHandTraffic;
            manifest["counts"] = counts;
            manifest["layers"] = layers;
            if (rasters != null) manifest["rasters"] = rasters;
            manifest["ripOptions"] = new Dictionary<string, object>
            {
                ["meshSource"] = "runtime",
                ["lodCap"] = Instance.MeshRip.LodCap,
                ["meshLimit"] = snapshot.MeshLimit,
                ["textureSizeCap"] = snapshot.TextureSizeCap,
                ["includeSubObjects"] = true,
                ["includeVector"] = layers.ContainsKey("vector")
            };

            // Atomic write: a crash mid-write must never leave a truncated bundle.json — the
            // consumer's first read of a bundle is the manifest, and a corrupt one poisons every
            // conclusion after it. Write beside it, then rename into place.
            //
            string manifestPath = Utils.IOUtils.CombinePath(outputDirectory, "bundle.json");
            string temporaryPath = manifestPath + ".tmp";
            File.WriteAllText(temporaryPath, JsonFile.Serialize(manifest));
            if (File.Exists(manifestPath)) File.Replace(temporaryPath, manifestPath, null);
            else File.Move(temporaryPath, manifestPath);

            // ---- 6. Report.
            // Size by layer and the dedup ratios — the Phase 8 acceptance numbers. Cheap here:
            // this half already runs off the main thread and has read every layer back anyway.
            //
            List<KeyValuePair<string, long>> layerSizes = new();
            long totalBytes = 0;
            foreach (LayerDef layer in Layers)
            {
                if (!layers.ContainsKey(layer.Key)) continue;

                string path = Utils.IOUtils.CombinePath(outputDirectory, layer.Path.TrimEnd('/'));
                long bytes = 0;
                try
                {
                    if (layer.IsDirectory)
                    {
                        foreach (string file in Directory.GetFiles(path, "*", SearchOption.AllDirectories))
                            bytes += new FileInfo(file).Length;
                    }
                    else
                    {
                        bytes = new FileInfo(path).Length;
                    }
                }
                catch (Exception ex)
                {
                    Instance.Log.Warn($"Could not size layer `{layer.Key}`: {ex.Message}");
                }

                layerSizes.Add(new KeyValuePair<string, long>(layer.Key, bytes));
                totalBytes += bytes;
            }

            layerSizes.Sort((a, b) => b.Value.CompareTo(a.Value));
            report.AppendLine($"Size by layer ({totalBytes / (1024d * 1024d):F1} MiB total):");
            foreach (KeyValuePair<string, long> layer in layerSizes)
                report.AppendLine($"  {layer.Key,-14} {layer.Value / (1024d * 1024d),8:F1} MiB");

            if (meshFileCount > 0)
                report.AppendLine($"Dedup: {instanceCount} instance(s) over {meshFileCount} mesh file(s) "
                                + $"— {instanceCount / (double)meshFileCount:F1} placements per mesh; "
                                + $"{materialCount} material(s) share {textureFileCount} texture file(s).");
            report.Insert(0, $"Wrote bundle.json — {layers.Count} layers.\n"
                           + $"{instanceCount} instances, {networkCount} network records, {laneCount} lanes,\n"
                           + $"{meshFileCount} meshes, {materialCount} materials, {textureFileCount} textures,\n"
                           + $"{areaCount} areas, {clipCount} clip volumes, {warningCount} warnings.\n"
                           + $"Bounds [{Floor3(bounds.MinX)}, {Floor3(bounds.MinY)}, {Floor3(bounds.MinZ)}] .. "
                           + $"[{Ceil3(bounds.MaxX)}, {Ceil3(bounds.MaxY)}, {Ceil3(bounds.MaxZ)}].\n");
            Instance.Log.Info($"BundleWriter: {report}");
            return report.ToString();
        }

        /// <summary>Round down to millimetres.</summary>
        private static double Floor3(double value) => Math.Floor(value * 1000.0) / 1000.0;

        /// <summary>Round up to millimetres.</summary>
        private static double Ceil3(double value) => Math.Ceiling(value * 1000.0) / 1000.0;


        /// <summary>
        /// Read a GLB container's JSON chunk as text, without touching the binary chunk.
        /// </summary>
        private static string ReadGlbJsonChunk(string path)
        {
            using FileStream stream = File.OpenRead(path);
            using BinaryReader reader = new(stream);

            if (reader.ReadUInt32() != 0x46546C67) throw new InvalidDataException("Bad GLB magic.");
            if (reader.ReadUInt32() != 2) throw new InvalidDataException("Not a GLB v2 container.");
            reader.ReadUInt32(); // Total length; unused here.

            while (stream.Position + 8 <= stream.Length)
            {
                uint chunkLength = reader.ReadUInt32();
                uint chunkType = reader.ReadUInt32();
                if (chunkType == 0x4E4F534A) // "JSON"
                {
                    return Encoding.UTF8.GetString(reader.ReadBytes((int)chunkLength));
                }

                stream.Seek(chunkLength + ((4 - (chunkLength % 4)) % 4), SeekOrigin.Current);
            }

            throw new InvalidDataException("No JSON chunk.");
        }
    }
}
