using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using Game.Simulation;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Unity.Entities;
using Unity.Mathematics;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// What one terrain rip captured on the main thread, and the stage that captured each part.
    /// </summary>
    /// <remarks>
    /// One function per contract section, in the order <c>RipTerrain</c> calls them. The record
    /// exists because the write pass needs about fifteen of these at once, and a
    /// fifteen-argument writer would be a worse thing than the god-method they came out of. The
    /// flow is one way — no capture stage reads another's output — so the order they run in is
    /// the order they are called and nothing else.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Everything one terrain rip captured on the main thread, for the writer task to encode.
        /// </summary>
        /// <remarks>
        /// A record rather than a parameter list because section 5 needs about fifteen of these
        /// at once, and a fifteen-argument writer is a worse thing than the god-method it came
        /// out of. Every field is either a raster the writer encodes or a block that rides
        /// <c>rasters.json</c>; nothing here is read back by the capture stages, so the flow is
        /// one way and the order the stages run in is the order they are called.
        /// </remarks>
        private sealed class TerrainCapture
        {
            /// <summary>Base heightmap resolution, in samples.</summary>
            public int Width, Height;

            /// <summary>The cascade's world scale and offset, for the geo-reference.</summary>
            public float3 Scale, Offset;

            /// <summary>The final, road-flattened ground (§8).</summary>
            public ushort[] HeightSamples;

            /// <summary>The roadbed mask, or null when ground was deselected.</summary>
            public byte[] SurfaceIndex;

            /// <summary>The water SURFACE LEVEL grid, or null.</summary>
            public ushort[] WaterLevel;

            /// <summary>The theme's ground textures, id → relative path.</summary>
            public Dictionary<string, string> SurfaceTextures;

            /// <summary>The splat weight maps the terrain shader samples, or null.</summary>
            public PendingTexture SplatPixels, UserSplatPixels;

            /// <summary>The backdrop terrain beyond the playable border, or null.</summary>
            public ushort[] WorldHeights;

            /// <summary>Backdrop resolution, in samples.</summary>
            public int WorldWidth, WorldHeight;

            /// <summary>The near-camera fine cascade at 1.75 m, or null.</summary>
            public ushort[] FineHeights;

            /// <summary>Fine cascade resolution, in samples.</summary>
            public int FineWidth, FineHeight;

            /// <summary>The blocks that ride <c>rasters.json</c>, each null when not captured.</summary>
            public Dictionary<string, object> SplatBlock, UserSplatBlock, WorldBlock, WaterBlock,
                                              TilingBlock, FineBlock;
        }

        /// <summary>
        /// Section 1 — final heights: the cascade readback, waited for so the last road edit is in.
        /// </summary>
        /// <param name="heightData">The cascade readback.</param>
        /// <param name="report">The report to note the resolution in.</param>
        /// <returns>The capture, or null when no height data is resident.</returns>
        private static TerrainCapture CaptureHeights(TerrainHeightData heightData, StringBuilder report)
        {
            if (!heightData.isCreated || (heightData.resolution.x <= 2)) return null;

            TerrainCapture capture = new TerrainCapture
            {
                Width = heightData.resolution.x,
                Height = heightData.resolution.z,
                HeightSamples = heightData.heights.ToArray(),
                Scale = heightData.scale,
                Offset = heightData.offset
            };

            report.AppendLine($"Terrain: {capture.Width}x{capture.Height} height samples, cell {1f / capture.Scale.x:F3} m, "
                            + $"height range {-capture.Offset.y:F1}..{(65535f / capture.Scale.y) - capture.Offset.y:F1} m");
            return capture;
        }

        /// <summary>
        /// Section 2 — the roadbed mask, stamped from the same net segments the game flattens
        /// with. Part of the ground-textures selection: the mask exists to tell a consumer which
        /// ground texture to show under roads.
        /// </summary>
        /// <param name="capture">The capture to fill.</param>
        /// <param name="heightData">The cascade readback.</param>
        /// <param name="report">The report to note the stamp counts in.</param>
        private void CaptureRoadbed(TerrainCapture capture, TerrainHeightData heightData, StringBuilder report)
        {
            byte[] surfaceIndex = new byte[capture.Width * capture.Height];
            _edgesStamped = _nodesStamped = _orphansStamped = _skippedForm = _skippedNoFlatten = _skippedWaterway = 0;
            StampRoadbed(surfaceIndex, heightData);
            capture.SurfaceIndex = surfaceIndex;

            int roadbedCells = 0;
            foreach (byte cell in surfaceIndex) if (cell != 0) roadbedCells++;
            report.AppendLine($"Roadbed: {_edgesStamped} edge(s), {_nodesStamped} node strip(s), {_orphansStamped} orphan cap(s) stamped; "
                            + $"{roadbedCells} cell(s) = {roadbedCells * 100.0 / surfaceIndex.Length:F1}% of the map");
            report.AppendLine($"  skipped {_skippedNoFlatten} edge(s) whose prefab does not flatten terrain, "
                            + $"{_skippedForm} elevated/tunnel composition(s) (their ground is not pulled to the road), "
                            + $"{_skippedWaterway} waterway composition(s) (canal beds are shifted down, not roadbed)");
        }

        /// <summary>
        /// Section 2b — the clip volumes, collected from the game's clip sections. The game never
        /// carves these holes into the heightmap: every frame the RoadClip stencil pass discards
        /// the terrain pixels covering tunnel portals and sunken trenches, so the readback
        /// legitimately contains ground that blocks them. These are the volumes that pass tests
        /// against — a consumer intersects them with the heightmap itself.
        /// </summary>
        /// <remarks>
        /// The walk now <i>collects</i> the clip volumes instead of rasterising them (contract
        /// §8c). <c>cutout.png</c> used to be written here: the intersection of these volumes with
        /// this one heightmap, at 3.5 m, from directly above. It was retired because that
        /// intersection cannot express a volume open at one height and closed at another, and is
        /// stale the moment a consumer edits terrain — and there was no consumer of it in the
        /// field to keep it for. Passing a null mask skips the rasterisation entirely.
        /// </remarks>
        /// <param name="heightData">The cascade readback.</param>
        /// <param name="report">The report to note the collected counts in.</param>
        private void CollectClipVolumes(TerrainHeightData heightData, StringBuilder report)
        {
            _clipVolumes = new List<Dictionary<string, object>>();
            StampCutout(null, heightData);

            report.AppendLine($"Clip: {_cutEdges} edge(s), {_cutNodes} node strip(s), "
                            + $"{_cutOrphans} orphan cap(s), {_cutAreas} clip area(s) collected");
            if (_cutRoundaboutTransitions > 0)
                report.AppendLine($"  {_cutRoundaboutTransitions} roundabout node(s) took AddNode's shed composition "
                                + "state and trimmed ring for their second strip pair");
        }

        /// <summary>
        /// Sections 3 and 3a — the water surface LEVEL, built on the sim grid and resampled onto
        /// the terrain grid, plus the appearance parameters the level grid alone cannot say.
        /// </summary>
        /// <remarks>
        /// Depth was resampled directly once, and the river climbed its gorge: bilinear depth from
        /// the ~28 m sim grid, added to the much finer ground, reads a mid-channel depth halfway up
        /// a cliff face. Level (ground at the sim cell + depth) is what the water surface actually
        /// is, and it is smooth across the shoreline in the way depth is not.
        /// </remarks>
        /// <param name="capture">The capture to fill.</param>
        /// <param name="heightData">The cascade readback.</param>
        /// <param name="report">The report to note the wet-cell count in.</param>
        private void CaptureWater(TerrainCapture capture, TerrainHeightData heightData, StringBuilder report)
        {
            capture.WaterLevel = BuildWaterLevel(heightData, out int wetCells, out float maxDepth);
            report.AppendLine((capture.WaterLevel != null)
                ? $"Water: {wetCells} wet cell(s), max depth {maxDepth:F1} m (surface level grid, heightmap encoding)"
                : "Water: no simulation data resident; layer omitted");

            // Colour, absorption, smoothness, sea level. One managed prefab read.
            capture.WaterBlock = CaptureWaterSettings(report);
        }

        /// <summary>
        /// Sections 4, 4a and 4a2 — the theme's ground textures, the game's own splat weight maps,
        /// and the tiling rates.
        /// </summary>
        /// <remarks>
        /// Ripping the splatmap replaces the consumer's per-cell classification — the "square
        /// splotches" — with the game's smooth per-pixel mix, painted surfaces and noise included.
        /// GPU readback happens here on the main thread; the PNG encode joins the writer task.
        /// The tiling parameters are the metres-per-repeat the theme authors per layer, without
        /// which every consumer guesses (the §39 lesson, on terrain).
        /// </remarks>
        /// <param name="capture">The capture to fill.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        /// <param name="report">The report to note the ground layers in.</param>
        private void CaptureGround(TerrainCapture capture, string terrainDirectory, StringBuilder report)
        {
            capture.SurfaceTextures = CaptureSurfaceTextures(terrainDirectory, report);
            capture.SplatPixels = CaptureSplatmap(capture.SurfaceTextures, out Dictionary<string, object> splatBlock, report);
            capture.SplatBlock = splatBlock;
            capture.UserSplatPixels = CaptureUserSplatmap(out Dictionary<string, object> userSplatBlock, report);
            capture.UserSplatBlock = userSplatBlock;
            capture.TilingBlock = CaptureTerrainTiling(report);
        }

        /// <summary>
        /// Section 4b — the backdrop terrain that fills the horizon beyond the playable border.
        /// </summary>
        /// <param name="capture">The capture to fill.</param>
        /// <param name="report">The report to note the backdrop in.</param>
        private void CaptureBackdrop(TerrainCapture capture, StringBuilder report)
        {
            capture.WorldHeights = CaptureWorldHeightmap(out Dictionary<string, object> worldBlock,
                                                         out int worldWidth, out int worldHeight, report);
            capture.WorldBlock = worldBlock;
            capture.WorldWidth = worldWidth;
            capture.WorldHeight = worldHeight;
        }

        /// <summary>
        /// Section 4c — the fine cascade capture (<c>TerrainRipSystem.FineCascade.cs</c>), if the
        /// pipeline ran it: the ground the game draws near the camera, at 1.75 m. Written beside
        /// the base heightmap; the block rides <c>rasters.json</c> like worldTerrain's.
        /// </summary>
        /// <param name="capture">The capture to fill.</param>
        /// <param name="report">The report to note the fine layer in.</param>
        private void CaptureFine(TerrainCapture capture, StringBuilder report)
        {
            if (TryGetFineHeights(out ushort[] fineHeights, out int fineWidth, out int fineHeight,
                                  out double fineCell, out double2 _))
            {
                capture.FineHeights = fineHeights;
                capture.FineWidth = fineWidth;
                capture.FineHeight = fineHeight;
                capture.FineBlock = FineRastersBlock();
                report.AppendLine(_fineReport ?? $"Fine terrain: {fineWidth}x{fineHeight} @ {fineCell:F3} m");
                report.AppendLine(DescribeFineVersusBase(fineHeights, fineWidth, fineHeight,
                                                         capture.HeightSamples, capture.Width, capture.Height,
                                                         capture.Scale, capture.SurfaceIndex));
            }
            else
            {
                capture.FineHeights = null;
                report.AppendLine(_fineReport ?? "Fine terrain: not captured (the fine cascade stage did not run); layer omitted");
            }
        }

        /// <summary>
        /// Sections 4b and 4c of the AREA layers — the pavement patches nothing else carries, and
        /// the only place-names the city contains.
        /// </summary>
        /// <remarks>
        /// Neither has anything to do with terrain; they ride this stage because they share its
        /// writer task and its one toggle. Refactor Phase 9 has them named for extraction into an
        /// <c>AreaRipSystem</c>, which is a change to system registration and the texture hand-off
        /// rather than a move, so it is not folded in here.
        /// </remarks>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        /// <param name="areas">Whether painted surfaces were selected.</param>
        /// <param name="report">The report to note the results in.</param>
        private void RipAreaLayers(string outputDirectory, string terrainDirectory, bool areas, StringBuilder report)
        {
            if (areas) RipSurfaceAreas(outputDirectory, terrainDirectory, report);
            else report.AppendLine("Painted surfaces deselected — areas.jsonl and areaprefabs.json untouched.");

            if (areas) RipDistricts(outputDirectory, report);
            else report.AppendLine("Painted surfaces deselected — districts.jsonl untouched.");
        }

        /// <summary>
        /// The manifest <c>rasters</c> block (contract §3), merged over whatever a previous run
        /// left on disk.
        /// </summary>
        /// <remarks>
        /// The sidecar merges like <c>materials.json</c>: a block for a layer this run did not
        /// touch survives, so a partial terrain re-rip (one sub-toggle) cannot orphan the layers a
        /// previous run left on disk.
        /// </remarks>
        /// <param name="capture">The finished capture.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        /// <param name="heights">Whether the height layers were selected this run.</param>
        /// <returns>The block to write.</returns>
        private SortedDictionary<string, object> BuildRasters(TerrainCapture capture, string terrainDirectory, bool heights)
        {
            float cellSize = 1f / capture.Scale.x;
            var rasters = new SortedDictionary<string, object>
            {
                // Top-left cell centre, rows toward -Z. The stamp arrays run south-to-north, so
                // rows are flipped at encode time.
                //
                ["resolution"] = Math.Round(cellSize, 6),
                ["size"] = new[] { capture.Width, capture.Height },
                ["originWorld"] = new[]
                {
                    Math.Round(-capture.Offset.x, 3), 0.0,
                    Math.Round(((capture.Height - 1) / capture.Scale.z) - capture.Offset.z, 3)
                },
                ["elevationUnits"] = "m",
                ["elevationScale"] = 1.0 / capture.Scale.y,
                ["elevationOffset"] = (double)(-capture.Offset.y)
            };
            if (capture.SplatBlock != null) rasters["splatmap"] = capture.SplatBlock;
            if (capture.UserSplatBlock != null) rasters["userSplatmap"] = capture.UserSplatBlock;
            if (capture.WorldBlock != null) rasters["worldTerrain"] = capture.WorldBlock;
            if (capture.WaterBlock != null) rasters["water"] = capture.WaterBlock;
            if (capture.TilingBlock != null) rasters["terrainTiling"] = capture.TilingBlock;
            if (capture.FineBlock != null) rasters["terrainFine"] = capture.FineBlock;

            string rastersPath = IOUtils.CombinePath(terrainDirectory, "rasters.json");
            if (!File.Exists(rastersPath)) return rasters;

            try
            {
                var existing = JsonConvert.DeserializeObject<Dictionary<string, object>>(File.ReadAllText(rastersPath));
                if (existing != null)
                {
                    foreach (string block in new[] { "splatmap", "userSplatmap", "worldTerrain", "water", "terrainTiling", "terrainFine" })
                    {
                        // A heights re-rip that captured no fine map has had the old file
                        // removed by the Clean stage; its block must go with it.
                        if (block == "terrainFine" && heights) continue;
                        if (!rasters.ContainsKey(block) && existing.TryGetValue(block, out object kept))
                            rasters[block] = kept;
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not merge the existing rasters.json; writing fresh: {ex.Message}");
            }
            return rasters;
        }


        /// <summary>
        /// §10: this pass owns the <c>terrain*</c> warning kinds. One declaration when the fine
        /// layer was wanted and is not there, so a consumer reading <c>heightmap.png</c> alone
        /// knows it is not looking at a rip that never tried.
        /// </summary>
        /// <param name="capture">The finished capture.</param>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="report">The report to note the declaration in.</param>
        private void DeclareFineGap(TerrainCapture capture, string outputDirectory, StringBuilder report)
        {
            List<Dictionary<string, object>> declarations = new();
            if (capture.FineHeights == null && _fineWarning != null)
                declarations.Add(WarningsFile.ByAsset("terrainFine", "terrain/heightmap_fine.png", _fineWarning));
            WarningsFile.Rewrite(outputDirectory, line => WarningsFile.IsKindPrefixed(line, WarningsFile.TerrainKindPrefix), declarations);
            if (declarations.Count > 0) report.AppendLine($"Recorded {declarations.Count} gap(s) in {WarningsFile.FileName}");
        }

        /// <summary>
        /// One report line stating how the fine raster relates to the base one — the measurement a
        /// re-rip can be judged by: away from the flattening footprint the two agree to the
        /// quantum (the fine slice is the same heightmap blitted in), and the samples that differ
        /// are the ones the height adjustments touched.
        /// </summary>
        private static string DescribeFineVersusBase(ushort[] fine, int fineWidth, int fineHeight,
                                                     ushort[] baseSamples, int width, int height, float3 scale, byte[] surfaceIndex)
        {
            int ratio = fineWidth / Math.Max(1, width);
            if (ratio < 1 || (ratio * width != fineWidth) || (fineHeight != ratio * height))
                return $"  fine/base grids are not an integer ratio ({fineWidth}x{fineHeight} vs {width}x{height})";
            long compared = 0, differ = 0, differOffRoad = 0, differOnRoad = 0;
            double maxDiff = 0, sumAbs = 0;
            float quantum = 1f / scale.y;
            // The base sample (x, z) is a cell centre; the fine texel ratio*x + ratio/2 lies half a
            // fine texel off it, so a difference under one fine-texel slope is expected noise.
            int half = ratio / 2;
            for (int z = 0; z < height; z++)
            {
                for (int x = 0; x < width; x++)
                {
                    int fx = Math.Min(fineWidth - 1, (x * ratio) + half);
                    int fz = Math.Min(fineHeight - 1, (z * ratio) + half);
                    double d = (fine[(fz * fineWidth) + fx] - baseSamples[(z * width) + x]) * quantum;
                    compared++;
                    double a = Math.Abs(d);
                    sumAbs += a;
                    if (a > maxDiff) maxDiff = a;
                    if (a > 0.1)
                    {
                        differ++;
                        bool road = surfaceIndex != null && surfaceIndex[(z * width) + x] != 0;
                        if (road) differOnRoad++; else differOffRoad++;
                    }
                }
            }
            return $"  fine vs base at {compared:N0} base samples: mean |d| {sumAbs / Math.Max(1, compared):F3} m, max {maxDiff:F2} m, "
                 + $"{differ:N0} differ by > 0.1 m ({differ * 100.0 / Math.Max(1, compared):F2}%: {differOnRoad:N0} on roadbed, {differOffRoad:N0} off)";
        }
    }
}
