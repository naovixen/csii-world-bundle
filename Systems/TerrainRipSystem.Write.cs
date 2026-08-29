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
    /// Section 5 — encode and write everything this stage captured, off the main thread.
    /// </summary>
    /// <remarks>
    /// The row convention is contract §3's and is applied here, once per raster: the stamp
    /// arrays run south-to-north and the manifest says rows run toward -Z, so everything but
    /// the splat maps is flipped at encode time. The splat capture already returns rows
    /// top-first, so it is not.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Section 5 — encode and write everything off the main thread.
        /// </summary>
        /// <param name="capture">The finished capture.</param>
        /// <param name="rasters">The manifest block to write beside the rasters.</param>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        /// <param name="heights">Whether the height layers were selected.</param>
        /// <param name="ground">Whether the ground layers were selected.</param>
        /// <param name="clips">Whether the clip layer was selected.</param>
        /// <param name="report">The report to note the write outcome in.</param>
        private void WriteTerrain(TerrainCapture capture, SortedDictionary<string, object> rasters,
                                  string outputDirectory, string terrainDirectory,
                                  bool heights, bool ground, bool clips, StringBuilder report)
        {
            string clipVolumesNote = null;
            try
            {
                Task writer = Task.Run(() =>
                {
                    // The ~50k-record serialisation used to run on the main thread; the note it
                    // produces joins the report after the wait (a StringBuilder is not
                    // thread-safe, so the task never touches `report` itself).
                    //
                    if (clips) clipVolumesNote = WriteClipVolumes(outputDirectory);
                    WriteAreaTextures();
                    WriteRasters(capture, terrainDirectory, heights);
                    Carto.IO.JsonFile.Write(IOUtils.CombinePath(terrainDirectory, "rasters.json"), rasters);
                    if (ground) WriteSurfaceIndexJson(capture, terrainDirectory);
                });
                writer.Wait();
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Write failed: {ex.Message}");
            }

            if (clipVolumesNote != null) report.AppendLine(clipVolumesNote);
        }

        /// <summary>
        /// The area surface textures the area rip queued for this stage's writer task.
        /// </summary>
        private void WriteAreaTextures()
        {
            if (_pendingAreaTextures == null) return;
            foreach (KeyValuePair<string, PendingTexture> pending in _pendingAreaTextures)
            {
                File.WriteAllBytes(pending.Key,
                                   Png.EncodeRgba(pending.Value.Pixels,
                                                  pending.Value.Width, pending.Value.Height));
            }
            _pendingAreaTextures = null;
        }

        /// <summary>
        /// Every raster this run captured, as a PNG in the row convention contract §3 states.
        /// </summary>
        /// <param name="capture">The finished capture.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        /// <param name="heights">Whether the height layers were selected.</param>
        private static void WriteRasters(TerrainCapture capture, string terrainDirectory, bool heights)
        {
            if (heights)
            {
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "heightmap.png"),
                                   Png.EncodeGray16(FlipRows(capture.HeightSamples, capture.Width, capture.Height),
                                                    capture.Width, capture.Height));
            }
            if (capture.FineHeights != null)
            {
                // The game's own near-camera ground (the fine cascade capture): same encoding,
                // same row convention once flipped.
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "heightmap_fine.png"),
                                   Png.EncodeGray16(FlipRows(capture.FineHeights, capture.FineWidth, capture.FineHeight),
                                                    capture.FineWidth, capture.FineHeight));
            }
            if (capture.SurfaceIndex != null)
            {
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "surface_index.png"),
                                   Png.EncodeGray8(FlipRows(capture.SurfaceIndex, capture.Width, capture.Height),
                                                   capture.Width, capture.Height));
            }
            if (capture.WaterLevel != null)
            {
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "water_level.png"),
                                   Png.EncodeGray16(FlipRows(capture.WaterLevel, capture.Width, capture.Height),
                                                    capture.Width, capture.Height));
            }
            if (capture.SplatPixels != null)
            {
                // Capture already returns rows top-first, and the splat's V axis runs
                // south→north, so row 0 is the north edge — the rasters convention — with no
                // further flip.
                //
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "splatmap.png"),
                                   Png.EncodeRgba(capture.SplatPixels.Pixels, capture.SplatPixels.Width, capture.SplatPixels.Height));
            }
            if (capture.UserSplatPixels != null)
            {
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "user_splatmap.png"),
                                   Png.EncodeRgba(capture.UserSplatPixels.Pixels, capture.UserSplatPixels.Width, capture.UserSplatPixels.Height));
            }
            if (capture.WorldHeights != null)
            {
                // The GPU readback hands rows V=0-first (south), like the stamp arrays.
                //
                File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "world_heightmap.png"),
                                   Png.EncodeGray16(FlipRows(capture.WorldHeights, capture.WorldWidth, capture.WorldHeight),
                                                    capture.WorldWidth, capture.WorldHeight));
            }
        }

        /// <summary>
        /// Surface id → name and texture; contract §8.
        /// </summary>
        /// <remarks>
        /// Id 0 is the ground the game classifies procedurally (grass/dirt/rock by slope and
        /// noise); id 1 is road-flattened ground, which in-game reads as dirt under the lane
        /// decals' wear tint — so dirt is its representative texture.
        /// </remarks>
        /// <param name="capture">The finished capture.</param>
        /// <param name="terrainDirectory">The bundle's terrain directory.</param>
        private static void WriteSurfaceIndexJson(TerrainCapture capture, string terrainDirectory)
        {
            var index = new SortedDictionary<string, object>
            {
                ["0"] = new Dictionary<string, string>
                {
                    ["name"] = "natural",
                    ["texture"] = capture.SurfaceTextures.TryGetValue("grass", out string grass) ? grass : null
                },
                ["1"] = new Dictionary<string, string>
                {
                    ["name"] = "roadbed",
                    ["texture"] = capture.SurfaceTextures.TryGetValue("dirt", out string dirt) ? dirt : null
                }
            };
            string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
            Carto.IO.JsonFile.Write(IOUtils.CombinePath(surfacesDirectory, "index.json"), index);
        }
    }
}
