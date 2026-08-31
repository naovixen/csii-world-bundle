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
    /// The water surface LEVEL grid, and the authored appearance the grid alone cannot say.
    /// </summary>
    /// <remarks>
    /// Level, not depth. Depth was resampled directly once and the river climbed its gorge:
    /// bilinear depth from the ~28 m sim grid, added to the much finer ground, reads a
    /// mid-channel depth halfway up a cliff face. Level (ground at the sim cell + depth) is what
    /// the surface actually is, and it is smooth across the shoreline in the way depth is not.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Build the water <i>surface level</i> grid: per wet sim cell, terrain height at that
        /// cell plus depth; dilated one sim cell; resampled bilinearly onto the terrain grid.
        /// <b>Why level and not depth.</b> Depth lives on the ~28 m simulation grid; the ground it
        /// was measured against is that same coarse grid. Adding coarse depth to the ~3.5 m
        /// terrain grid's fine ground re-bases every sample onto ground the simulation never saw —
        /// against a gorge wall, mid-channel depth lands halfway up the cliff and the river floods
        /// its gorge. The surface level is the physical quantity that is actually smooth across a
        /// shoreline, so it is the one that survives bilinear resampling.
        /// <b>The one-cell dilation</b> keeps the shoreline honest: without it, a terrain cell
        /// between a wet sim cell and a dry one would lerp level against the dry cell's "no data"
        /// and drag the surface down right where the consumer needs it to cross the ground. The
        /// dilated ring copies its highest wet neighbour, so the surface extends flat past the
        /// shoreline and the ground plane cuts it where the game's own shoreline is.
        /// </summary>
        /// <param name="heightData">The terrain grid to sample ground from and resample onto.</param>
        /// <param name="wetCells">Receives how many terrain cells the level exceeds the ground in.</param>
        /// <param name="maxDepth">Receives the deepest level-over-ground.</param>
        /// <returns>The level grid in the heightmap's encoding (0 = dry), or <c>null</c> without data.</returns>
        private ushort[] BuildWaterLevel(TerrainHeightData heightData, out int wetCells, out float maxDepth)
        {
            wetCells = 0;
            maxDepth = 0f;

            WaterSurfaceData<SurfaceWater> water = Instance.Water.GetSurfaceData(out Unity.Jobs.JobHandle deps);
            deps.Complete();
            if (!water.depths.IsCreated || (water.resolution.x <= 0)) return null;

            int simWidth = water.resolution.x;
            int simHeight = water.resolution.z;

            // ---- Level per wet sim cell, on the ground the simulation itself sits on.
            //
            float[] level = new float[simWidth * simHeight];
            for (int i = 0; i < level.Length; i++) level[i] = NoLevel;

            for (int z = 0; z < simHeight; z++)
            {
                // Sim cell -> world, the inverse of the sampling map below
                // (world = cell / scale - offset).
                //
                float worldZ = (z / water.scale.z) - water.offset.z;
                for (int x = 0; x < simWidth; x++)
                {
                    // The game's own decode (`WaterUtils.ToWorldSpace(float surfaceDepth)`):
                    // metres = raw / scale.y − offset.y. The raw grid value is not metres.
                    //
                    float depth = (water.depths[(z * simWidth) + x].m_Depth / water.scale.y) - water.offset.y;
                    if (!(depth > 0f)) continue;

                    float worldX = (x / water.scale.x) - water.offset.x;

                    // The ground the depth is re-based onto is the MINIMUM fine height over the
                    // sim cell's footprint, not a corner point sample. A ~28 m cell straddling a
                    // dock basin and its quay put the point on the quay top, and quay + depth
                    // floated the sea's level over whole neighbourhoods of lower land behind it
                    // (the consumer's phantom flooded docklands, 2026-08-31). The depth belongs
                    // to the basin floor; floor + depth can only ever UNDERSTATE the surface at
                    // a mixed cell, which trims the shoreline inward - the safe side, since the
                    // ground plane cuts the surface there anyway.
                    level[(z * simWidth) + x] = MinTerrainHeightOverCell(heightData, worldX, worldZ, water.scale) + depth;
                }
            }

            // ---- Dilate one sim cell: each dry cell bordering water takes its highest wet
            // neighbour.
            float[] dilated = (float[])level.Clone();
            for (int z = 0; z < simHeight; z++)
            {
                for (int x = 0; x < simWidth; x++)
                {
                    if (level[(z * simWidth) + x] != NoLevel) continue;

                    float best = NoLevel;
                    for (int dz = -1; dz <= 1; dz++)
                    {
                        int nz = z + dz;
                        if ((nz < 0) || (nz >= simHeight)) continue;
                        for (int dx = -1; dx <= 1; dx++)
                        {
                            int nx = x + dx;
                            if ((nx < 0) || (nx >= simWidth)) continue;
                            best = math.max(best, level[(nz * simWidth) + nx]);
                        }
                    }
                    if (best != NoLevel) dilated[(z * simWidth) + x] = best;
                }
            }

            // ---- Resample onto the terrain grid. Corners without data drop out of the lerp by
            // weight, rather than dragging the surface toward a sentinel.
            //
            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            ushort[] grid = new ushort[width * height];

            for (int z = 0; z < height; z++)
            {
                float worldZ = (z / heightData.scale.z) - heightData.offset.z;
                float waterZ = (worldZ + water.offset.z) * water.scale.z;
                int z0 = math.clamp((int)math.floor(waterZ), 0, simHeight - 1);
                int z1 = math.min(z0 + 1, simHeight - 1);
                float tz = math.saturate(waterZ - z0);

                for (int x = 0; x < width; x++)
                {
                    float worldX = (x / heightData.scale.x) - heightData.offset.x;
                    float waterX = (worldX + water.offset.x) * water.scale.x;
                    int x0 = math.clamp((int)math.floor(waterX), 0, simWidth - 1);
                    int x1 = math.min(x0 + 1, simWidth - 1);
                    float tx = math.saturate(waterX - x0);

                    float sum = 0f, weightSum = 0f;
                    AddCorner(dilated[(z0 * simWidth) + x0], (1f - tx) * (1f - tz), ref sum, ref weightSum);
                    AddCorner(dilated[(z0 * simWidth) + x1], tx * (1f - tz), ref sum, ref weightSum);
                    AddCorner(dilated[(z1 * simWidth) + x0], (1f - tx) * tz, ref sum, ref weightSum);
                    AddCorner(dilated[(z1 * simWidth) + x1], tx * tz, ref sum, ref weightSum);
                    if (!(weightSum > 0f)) continue;

                    float cellLevel = sum / weightSum;

                    // The heightmap's own encoding, with 0 reserved for dry — a real level that
                    // would encode to 0 sits at the map's absolute height floor and cannot hold
                    // water anyway.
                    //
                    grid[(z * width) + x] = (ushort)math.clamp(
                        math.round((cellLevel + heightData.offset.y) * heightData.scale.y), 1f, 65535f);

                    float ground = heightData.heights[(z * width) + x] / heightData.scale.y - heightData.offset.y;
                    if (cellLevel > ground)
                    {
                        wetCells++;
                        maxDepth = math.max(maxDepth, cellLevel - ground);
                    }
                }
            }

            return grid;
        }

        /// <summary>
        /// Accumulate one bilinear corner into the weighted sum, unless it carries no data.
        /// </summary>
        private static void AddCorner(float value, float weight, ref float sum, ref float weightSum)
        {
            if (value == NoLevel) return;
            sum += value * weight;
            weightSum += weight;
        }

        /// <summary>
        /// Terrain height in metres at a world position, bilinear over the height samples —
        /// <c>TerrainUtils.SampleHeight</c>'s mapping.
        /// </summary>
        /// <summary>
        /// The lowest fine terrain height inside one water sim cell's footprint (the cell spans
        /// <c>1 / scale</c> metres from its origin). 4x4 samples: the sim cell is ~28 m and the
        /// fine grid ~3.5-14 m, so that is at or below the fine spacing.
        /// </summary>
        private static float MinTerrainHeightOverCell(TerrainHeightData heightData, float worldX, float worldZ, float3 simScale)
        {
            float cellW = 1f / simScale.x;
            float cellH = 1f / simScale.z;
            float lowest = float.MaxValue;
            for (int sz = 0; sz < 4; sz++)
            {
                for (int sx = 0; sx < 4; sx++)
                {
                    float h = SampleTerrainHeight(
                        heightData,
                        worldX + (cellW * ((sx + 0.5f) / 4f)),
                        worldZ + (cellH * ((sz + 0.5f) / 4f)));
                    lowest = math.min(lowest, h);
                }
            }
            return lowest;
        }

        private static float SampleTerrainHeight(TerrainHeightData heightData, float worldX, float worldZ)
        {
            float cellX = (worldX + heightData.offset.x) * heightData.scale.x;
            float cellZ = (worldZ + heightData.offset.z) * heightData.scale.z;

            int x0 = math.clamp((int)math.floor(cellX), 0, heightData.resolution.x - 1);
            int x1 = math.min(x0 + 1, heightData.resolution.x - 1);
            float tx = math.saturate(cellX - x0);
            int z0 = math.clamp((int)math.floor(cellZ), 0, heightData.resolution.z - 1);
            int z1 = math.min(z0 + 1, heightData.resolution.z - 1);
            float tz = math.saturate(cellZ - z0);

            float raw = math.lerp(
                math.lerp(heightData.heights[(z0 * heightData.resolution.x) + x0],
                          heightData.heights[(z0 * heightData.resolution.x) + x1], tx),
                math.lerp(heightData.heights[(z1 * heightData.resolution.x) + x0],
                          heightData.heights[(z1 * heightData.resolution.x) + x1], tx), tz);

            return (raw / heightData.scale.y) - heightData.offset.y;
        }
    }
}
