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
    /// The terrain beyond the playable border, and the metres-per-repeat each ground tiles at.
    /// </summary>
    /// <remarks>
    /// Neither is the playable ground: one fills the horizon, the other is the parameter without
    /// which every consumer guesses its tiling (the §39 lesson, on terrain).
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Read back the backdrop (world) heightmap — the terrain beyond the playable border
        /// that fills the horizon.
        /// <c>TerrainSystem.worldHeightmap</c> is a public <c>R16_UNorm</c> texture spanning
        /// <c>worldOffset … worldOffset + worldSize</c> (the playable area is a sub-rect of it).
        /// The height cascade blits this map and the playable map into one seamless surface, so
        /// they share the height encoding — the same <c>elevationScale</c>/<c>Offset</c> decode,
        /// which the importer can verify by continuity across the playable border. Read through
        /// <c>AsyncGPUReadback</c> (the game's own readback route for these maps), which returns
        /// the 16-bit samples losslessly where an 8-bit blit would quantise them.
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "worldTerrain" block, or <c>null</c>.</param>
        /// <param name="width">Receives the map width.</param>
        /// <param name="height">Receives the map height.</param>
        /// <param name="report">The report.</param>
        /// <returns>South-to-north rows of 16-bit samples, or <c>null</c>.</returns>
        private ushort[] CaptureWorldHeightmap(out Dictionary<string, object> block,
                                               out int width, out int height, StringBuilder report)
        {
            block = null;
            width = height = 0;
            try
            {
                Texture world = Instance.Terrain.worldHeightmap;
                if (world == null)
                {
                    report.AppendLine("World terrain: no backdrop heightmap resident; layer omitted");
                    return null;
                }

                UnityEngine.Rendering.AsyncGPUReadbackRequest request =
                    UnityEngine.Rendering.AsyncGPUReadback.Request(world, 0);
                request.WaitForCompletion();
                if (request.hasError)
                {
                    report.AppendLine("World terrain: GPU readback failed; layer omitted");
                    return null;
                }

                Unity.Collections.NativeArray<ushort> data = request.GetData<ushort>();
                width = world.width;
                height = world.height;
                if (data.Length < width * height)
                {
                    report.AppendLine($"World terrain: readback returned {data.Length} of {width * height} samples; layer omitted");
                    return null;
                }

                ushort[] samples = new ushort[width * height];
                Unity.Collections.NativeArray<ushort>.Copy(data, samples, samples.Length);

                float2 size = Instance.Terrain.worldSize;
                float2 origin = Instance.Terrain.worldOffset;
                double cellX = size.x / width;
                double cellZ = size.y / height;
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/world_heightmap.png",
                    ["size"] = new[] { width, height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + size.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "backdrop terrain; same elevation decode as the main heightmap "
                                  + "(the cascade blits both into one seamless surface); the playable "
                                  + "area is a sub-rect and its samples are authoritative there"
                };

                report.AppendLine($"World terrain: {width}x{height} backdrop heightmap captured "
                                + $"({size.x:F0}x{size.y:F0} m)");
                return samples;
            }
            catch (Exception ex)
            {
                _log.Warn($"World heightmap capture failed: {ex.Message}");
                report.AppendLine($"World terrain: {ex.Message}; layer omitted");
                return null;
            }
        }
    }
}
