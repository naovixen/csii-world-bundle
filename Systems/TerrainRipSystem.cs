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
    /// The system that rips the terrain, water and ground-surface layers (PLAN Phase 6).
    /// <b>The heightmap is the final, road-flattened ground.</b> <c>TerrainSystem</c>'s road job
    /// flattens the height cascade under every net whose prefab carries
    /// <c>GeometryFlags.FlattenTerrain</c>/<c>ClipTerrain</c>, and <c>GetHeightData()</c> reads
    /// back exactly that cascade slice (<c>baseLod</c>) — the base heightmap is blitted in and
    /// the lot/lane/area height adjustments are drawn over it before the readback is requested.
    /// The elevation this system writes therefore already contains the flattened final ground.
    /// (It is the ground <i>beneath</i> junctions, not their visible surface — the arms' node
    /// meshes pave the interior; audit §18. An earlier revision claimed the heightmap closed the
    /// junctions; that was the §18 misreading.)
    /// <b>Why the roadbed mask is stamped from net geometry.</b> The terrain material has no
    /// roadbed texture and no roadbed mask — its grounds are grass, dirt, rock and four paintable
    /// extras, classified per-pixel on the GPU. What makes flattened ground read as roadbed
    /// in-game is the flattening itself (plus the ~13%-alpha lane decals). The truthful mask is
    /// therefore the same footprint the road job draws: every edge segment, junction-node strip
    /// and orphan cap of a Flatten/ClipTerrain net, which this system rasterises on the CPU from
    /// the same <c>Segment</c> béziers (<c>CullRoadsJob.AddEdge/AddNode/AddOrphans</c>,
    /// decompiled).
    /// <b>Why a clip layer exists at all.</b> The game never carves tunnel portals or sunken
    /// trenches out of the heightmap. A Lowered composition's ground is merely clamped into
    /// <c>[lane, lane + ElevationLimit·3]</c> and a shallow tube's cover is shifted <i>up</i> —
    /// the covering terrain stays in the height cascade, and every frame the <c>RoadClip</c>
    /// stencil pass (<c>GenerateClipDataJob</c> → <c>TerrainSurface.RenderClipAreas</c>) discards
    /// its pixels on screen instead. A consumer that rebuilds the ground from the heightmap alone
    /// therefore walls off every tunnel mouth and trench; <c>clipvolumes.jsonl</c> (§8c) carries
    /// the volumes that pass clips with, and consumers must delete what falls inside them.
    /// </summary>
    public partial class TerrainRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The no-data sentinel of the sim-grid water level scratch array. A dry cell carries no
        /// surface elevation at all — <c>0f</c> would be a real height on maps whose terrain
        /// crosses zero metres.
        /// </summary>
        const float NoLevel = float.MinValue;

        /// <summary>
        /// The clip band's expansion at both ends, matching <c>GenerateClipDataJob</c> exactly:
        /// the game widens every band by 0.3 m downward and upward, so the floor reaches 0.3 m
        /// <i>below</i> the lane and clips the roadbed shell itself — the road and tunnel meshes
        /// cover that hole, in-game and in every consumer (collision comes from the network
        /// meshes). An earlier +0.5 m "drivable lip" floor was a producer invention; it left a
        /// ring of terrain proud of the lane at every portal mouth and disabled the everyday
        /// ±0.3 m shell clip that keeps crests out of surface roads.
        /// </summary>
        const float ClipBandExpansion = 0.3f;

        /// <summary>The query for placed network edges.</summary>
        EntityQuery _edgeQuery;

        /// <summary>The query for orphan nodes.</summary>
        EntityQuery _orphanQuery;

        /// <summary>The query for rendered surface areas.</summary>
        EntityQuery _surfaceAreaQuery;

        /// <summary>The query for terrain-clipping areas.</summary>
        EntityQuery _clipAreaQuery;

        // Stamp tallies for the report.
        int _edgesStamped, _nodesStamped, _orphansStamped, _skippedForm, _skippedNoFlatten, _skippedWaterway;
        int _cutEdges, _cutNodes, _cutOrphans, _cutAreas, _cutRoundaboutApprox;

        /// <summary>
        /// The clip volumes collected during the clip walk, for <c>clipvolumes.jsonl</c>.
        /// </summary>
        List<Dictionary<string, object>> _clipVolumes;

        /// <summary>
        /// Area prefab textures captured during the surface-area walk, PNG-encoded and written by
        /// the writer task instead of on the main thread (absolute path → captured pixels).
        /// </summary>
        List<KeyValuePair<string, PendingTexture>> _pendingAreaTextures;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            _edgeQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Composition>(),
                    ComponentType.ReadOnly<Edge>(),
                    ComponentType.ReadOnly<EdgeGeometry>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            _orphanQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Net.Orphan>(),
                    ComponentType.ReadOnly<Node>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Net.OutsideConnection>(),
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            _surfaceAreaQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Areas.Surface>(),
                    ComponentType.ReadOnly<Game.Areas.Node>(),
                    ComponentType.ReadOnly<Game.Areas.Triangle>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            // Areas whose prefab carries ClipArea (a SurfacePrefab add-on): the terrain job draws
            // them into the same RoadClip stencil (its areaClipMesh path) instead of flattening.
            //
            _clipAreaQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Areas.Clip>(),
                    ComponentType.ReadOnly<Game.Areas.Node>(),
                    ComponentType.ReadOnly<Game.Areas.Triangle>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            base.OnCreate();
            _log.Debug("TerrainRipSystem instance created.");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// </summary>
        protected override void OnDestroy() { base.OnDestroy(); }

        /// <summary>
        /// The event triggered when the system instance is updated.
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// Rip the terrain layers into <c>terrain/</c>: the final (road-flattened) heightmap, the
        /// roadbed surface index, the clip volumes (what the game's clip pass hides — tunnel
        /// portals, sunken trenches), the water surface level grid, the theme's ground textures
        /// and the geo-referencing sidecar. Contract §8 of <c>docs/bundle-schema.md</c>.
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.</param>
        /// <param name="heights">Whether to write the heightmaps — playable and backdrop.</param>
        /// <param name="water">Whether to write the water surface grid.</param>
        /// <param name="ground">Whether to write the ground textures — theme images, blend maps and the roadbed mask.</param>
        /// <param name="clips">Whether to collect the tunnel-opening clip volumes.</param>
        /// <param name="areas">Whether to rip the painted surface areas.</param>
        /// <returns>A human-readable report.</returns>
        public string RipTerrain(string outputDirectory, bool heights = true, bool water = true,
                                 bool ground = true, bool clips = true, bool areas = true)
        {
            StringBuilder report = new();
            string terrainDirectory = IOUtils.CombinePath(outputDirectory, "terrain");
            if (!Directory.Exists(terrainDirectory)) Directory.CreateDirectory(terrainDirectory);

            // ---- 1. Final heights: the cascade readback, waited for so the last road edit is in.
            //
            TerrainHeightData heightData = Instance.Terrain.GetHeightData(waitForPending: true);
            if (!heightData.isCreated || (heightData.resolution.x <= 2))
            {
                return "Terrain rip failed: no height data is resident (is a city loaded?).";
            }

            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            ushort[] heightSamples = heightData.heights.ToArray();
            float3 scale = heightData.scale;
            float3 offset = heightData.offset;

            report.AppendLine($"Terrain: {width}x{height} height samples, cell {1f / scale.x:F3} m, "
                            + $"height range {-offset.y:F1}..{(65535f / scale.y) - offset.y:F1} m");

            // ---- 2. The roadbed mask, stamped from the same net segments the game flattens with.
            // Part of the ground-textures selection: the mask exists to tell a consumer which
            // ground texture to show under roads.
            byte[] surfaceIndex = null;
            if (ground)
            {
                surfaceIndex = new byte[width * height];
                _edgesStamped = _nodesStamped = _orphansStamped = _skippedForm = _skippedNoFlatten = _skippedWaterway = 0;
                StampRoadbed(surfaceIndex, heightData);

                int roadbedCells = 0;
                foreach (byte cell in surfaceIndex) if (cell != 0) roadbedCells++;
                report.AppendLine($"Roadbed: {_edgesStamped} edge(s), {_nodesStamped} node strip(s), {_orphansStamped} orphan cap(s) stamped; "
                                + $"{roadbedCells} cell(s) = {roadbedCells * 100.0 / surfaceIndex.Length:F1}% of the map");
                report.AppendLine($"  skipped {_skippedNoFlatten} edge(s) whose prefab does not flatten terrain, "
                                + $"{_skippedForm} elevated/tunnel composition(s) (their ground is not pulled to the road), "
                                + $"{_skippedWaterway} waterway composition(s) (canal beds are shifted down, not roadbed)");
            }

            // ---- 2b. The clip volumes, collected from the game's clip sections. The game never
            // carves these holes into the heightmap: every frame the RoadClip stencil pass
            // discards the terrain pixels covering tunnel portals and sunken trenches, so the
            // readback legitimately contains ground that blocks them. These are the volumes that
            // pass tests against — a consumer intersects them with the heightmap itself.
            //
            _cutEdges = _cutNodes = _cutOrphans = _cutAreas = _cutRoundaboutApprox = 0;

            if (clips)
            {
                // The walk now *collects* the clip volumes instead of rasterising them (contract
                // §8c). `cutout.png` used to be written here: the intersection of these volumes
                // with this one heightmap, at 3.5 m, from directly above. It was retired because
                // that intersection cannot express a volume open at one height and closed at
                // another, and is stale the moment a consumer edits terrain — and there was no
                // consumer of it in the field to keep it for. Passing a null mask skips the
                // rasterisation entirely.
                //
                _clipVolumes = new List<Dictionary<string, object>>();
                StampCutout(null, heightData);

                report.AppendLine($"Clip: {_cutEdges} edge(s), {_cutNodes} node strip(s), "
                                + $"{_cutOrphans} orphan cap(s), {_cutAreas} clip area(s) collected");
                if (_cutRoundaboutApprox > 0)
                    report.AppendLine($"  APPROXIMATED: {_cutRoundaboutApprox} roundabout node(s) cut with raw composition "
                                    + "flags (the game's AddNode state-shedding and ring trims omitted — can over-cut, never under)");
            }

            // ---- 3. Water surface LEVEL, built on the sim grid and resampled onto the terrain
            // grid. Depth was resampled directly once, and the river climbed its gorge: bilinear
            // depth from the ~28 m sim grid, added to the much finer ground, reads a mid-channel
            // depth halfway up a cliff face. Level (ground at the sim cell + depth) is what the
            // water surface actually is, and it is smooth across the shoreline in the way depth
            // is not.
            //
            ushort[] waterLevel = null;
            if (water)
            {
                waterLevel = BuildWaterLevel(heightData, out int wetCells, out float maxDepth);
                report.AppendLine((waterLevel != null)
                    ? $"Water: {wetCells} wet cell(s), max depth {maxDepth:F1} m (surface level grid, heightmap encoding)"
                    : "Water: no simulation data resident; layer omitted");
            }

            // ---- 3a. The water appearance parameters — what the level grid alone cannot say:
            // colour, absorption, smoothness, sea level. One managed prefab read.
            //
            Dictionary<string, object> waterBlock = water ? CaptureWaterSettings(report) : null;

            // ---- 4. The theme's ground textures, read off the live global bindings.
            //
            Dictionary<string, string> surfaceTextures = ground ? CaptureSurfaceTextures(terrainDirectory, report) : null;

            // ---- 4a. The game's own ground blend: the splat weight map the terrain shader
            // samples per pixel. Ripping it replaces the consumer's per-cell classification —
            // the "square splotches" — with the game's smooth per-pixel mix, painted surfaces
            // and noise included. GPU readback happens here on the main thread; the PNG encode
            // joins the writer task below.
            //
            PendingTexture splatPixels = null, userSplatPixels = null;
            Dictionary<string, object> splatBlock = null, userSplatBlock = null;
            if (ground)
            {
                splatPixels = CaptureSplatmap(surfaceTextures, out splatBlock, report);
                userSplatPixels = CaptureUserSplatmap(out userSplatBlock, report);
            }

            // ---- 4a2. The ground tiling parameters — the metres-per-repeat the theme authors
            // per layer, without which every consumer guesses (the §39 lesson, on terrain).
            //
            Dictionary<string, object> tilingBlock = ground ? CaptureTerrainTiling(report) : null;

            // ---- 4b. The backdrop terrain that fills the horizon beyond the playable border.
            //
            ushort[] worldHeights = null;
            Dictionary<string, object> worldBlock = null;
            int worldWidth = 0, worldHeight = 0;
            if (heights)
            {
                worldHeights = CaptureWorldHeightmap(out worldBlock, out worldWidth, out worldHeight, report);
            }

            // ---- 4b. Surface areas — the pavement patches nothing else carries.
            //
            if (areas) RipSurfaceAreas(outputDirectory, terrainDirectory, report);
            else report.AppendLine("Painted surfaces deselected — areas.jsonl and areaprefabs.json untouched.");

            // ---- 5. Encode and write everything off the main thread.
            //
            float cellSize = 1f / scale.x;
            var rasters = new SortedDictionary<string, object>
            {
                // The manifest `rasters` block, verbatim (contract §3): top-left cell centre,
                // rows toward -Z. The stamp arrays run south-to-north, so rows are flipped at
                // encode time below.
                //
                ["resolution"] = Math.Round(cellSize, 6),
                ["size"] = new[] { width, height },
                ["originWorld"] = new[]
                {
                    Math.Round(-offset.x, 3), 0.0,
                    Math.Round(((height - 1) / scale.z) - offset.z, 3)
                },
                ["elevationUnits"] = "m",
                ["elevationScale"] = 1.0 / scale.y,
                ["elevationOffset"] = (double)(-offset.y)
            };
            if (splatBlock != null) rasters["splatmap"] = splatBlock;
            if (userSplatBlock != null) rasters["userSplatmap"] = userSplatBlock;
            if (worldBlock != null) rasters["worldTerrain"] = worldBlock;
            if (waterBlock != null) rasters["water"] = waterBlock;
            if (tilingBlock != null) rasters["terrainTiling"] = tilingBlock;

            // The sidecar merges like materials.json: a block for a layer this run did not touch
            // survives, so a partial terrain re-rip (one sub-toggle) cannot orphan the layers a
            // previous run left on disk.
            string rastersPath = IOUtils.CombinePath(terrainDirectory, "rasters.json");
            if (File.Exists(rastersPath))
            {
                try
                {
                    var existing = JsonConvert.DeserializeObject<Dictionary<string, object>>(File.ReadAllText(rastersPath));
                    if (existing != null)
                    {
                        foreach (string block in new[] { "splatmap", "userSplatmap", "worldTerrain", "water", "terrainTiling" })
                        {
                            if (!rasters.ContainsKey(block) && existing.TryGetValue(block, out object kept))
                                rasters[block] = kept;
                        }
                    }
                }
                catch (Exception ex)
                {
                    _log.Warn($"Could not merge the existing rasters.json; writing fresh: {ex.Message}");
                }
            }

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

                    if (_pendingAreaTextures != null)
                    {
                        foreach (KeyValuePair<string, PendingTexture> pending in _pendingAreaTextures)
                        {
                            File.WriteAllBytes(pending.Key,
                                               Png.EncodeRgba(pending.Value.Pixels,
                                                              pending.Value.Width, pending.Value.Height));
                        }
                        _pendingAreaTextures = null;
                    }

                    if (heights)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "heightmap.png"),
                                           Png.EncodeGray16(FlipRows(heightSamples, width, height), width, height));
                    }
                    if (surfaceIndex != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "surface_index.png"),
                                           Png.EncodeGray8(FlipRows(surfaceIndex, width, height), width, height));
                    }
                    if (waterLevel != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "water_level.png"),
                                           Png.EncodeGray16(FlipRows(waterLevel, width, height), width, height));
                    }
                    if (splatPixels != null)
                    {
                        // Capture already returns rows top-first, and the splat's V axis runs
                        // south→north, so row 0 is the north edge — the rasters convention —
                        // with no further flip.
                        //
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "splatmap.png"),
                                           Png.EncodeRgba(splatPixels.Pixels, splatPixels.Width, splatPixels.Height));
                    }
                    if (userSplatPixels != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "user_splatmap.png"),
                                           Png.EncodeRgba(userSplatPixels.Pixels, userSplatPixels.Width, userSplatPixels.Height));
                    }
                    if (worldHeights != null)
                    {
                        // The GPU readback hands rows V=0-first (south), like the stamp arrays.
                        //
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "world_heightmap.png"),
                                           Png.EncodeGray16(FlipRows(worldHeights, worldWidth, worldHeight),
                                                            worldWidth, worldHeight));
                    }

                    File.WriteAllText(IOUtils.CombinePath(terrainDirectory, "rasters.json"),
                                      JsonConvert.SerializeObject(rasters, Formatting.Indented));

                    // Surface id → name and texture; contract §8. Id 0 is the ground the game
                    // classifies procedurally (grass/dirt/rock by slope and noise); id 1 is
                    // road-flattened ground, which in-game reads as dirt under the lane decals'
                    // wear tint — so dirt is its representative texture.
                    //
                    if (ground)
                    {
                        var index = new SortedDictionary<string, object>
                        {
                            ["0"] = new Dictionary<string, string>
                            {
                                ["name"] = "natural",
                                ["texture"] = surfaceTextures.TryGetValue("grass", out string grass) ? grass : null
                            },
                            ["1"] = new Dictionary<string, string>
                            {
                                ["name"] = "roadbed",
                                ["texture"] = surfaceTextures.TryGetValue("dirt", out string dirt) ? dirt : null
                            }
                        };
                        string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
                        if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);
                        File.WriteAllText(IOUtils.CombinePath(surfacesDirectory, "index.json"),
                                          JsonConvert.SerializeObject(index, Formatting.Indented));
                    }
                });
                writer.Wait();
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Write failed: {ex.Message}");
            }

            if (clipVolumesNote != null) report.AppendLine(clipVolumesNote);
            report.AppendLine($"Output: {terrainDirectory}");
            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Reverse the row order of a raster — the stamp arrays run south-to-north (the game's
        /// <c>z</c>), the contract's rasters run north-to-south (row 0 toward −Z).
        /// </summary>
        private static T[] FlipRows<T>(T[] grid, int width, int height)
        {
            T[] flipped = new T[grid.Length];
            for (int y = 0; y < height; y++)
            {
                Array.Copy(grid, y * width, flipped, (height - 1 - y) * width, width);
            }
            return flipped;
        }

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
                    level[(z * simWidth) + x] = SampleTerrainHeight(heightData, worldX, worldZ) + depth;
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

        /// <summary>
        /// Rip every rendered surface area — driveways, parking-lot pavement, park paths, the
        /// painted patches under meshless service buildings — into <c>areas.jsonl</c> +
        /// <c>areaprefabs.json</c>. Contract §8b.
        /// <b>Why this layer exists.</b> A parking lot is a <i>meshless</i> building prefab: its
        /// instance record places nothing visible, its markings are §4.2a decal instances, and the
        /// pavement itself is a <c>SurfacePrefab</c> area the building spawns as a sub-area —
        /// triangulated on the area grid (<c>Game.Areas.Node</c>/<c>Triangle</c> buffers) and drawn
        /// by <c>AreaBatchSystem</c> with the prefab's <c>RenderedArea.m_Material</c> (falling back
        /// to the global <c>AreasConfigurationPrefab.m_RenderedAreaMaterial</c>). House driveways
        /// are the same mechanism. Without this layer those surfaces simply do not exist in the
        /// bundle, which is why every reconstruction showed grass where the game shows pavement.
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory, for textures.</param>
        /// <param name="report">The report to note results in.</param>
        private void RipSurfaceAreas(string outputDirectory, string terrainDirectory, StringBuilder report)
        {
            var areas = _surfaceAreaQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            int written = 0, skippedEmpty = 0, skippedUnpainted = 0;
            Dictionary<Entity, string> prefabKeys = new();
            HashSet<string> usedKeys = new();
            Dictionary<string, object> catalogue = new();
            _pendingAreaTextures = new List<KeyValuePair<string, PendingTexture>>();

            try
            {
                using StreamWriter writer = Carto.IO.BundleFile.CreateText(
                    IOUtils.CombinePath(outputDirectory, "areas.jsonl"));

                foreach (Entity entity in areas)
                {
                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0))
                    {
                        skippedEmpty++;
                        continue;
                    }

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (!prefabKeys.TryGetValue(prefab, out string key))
                    {
                        key = CatalogueAreaPrefab(prefab, terrainDirectory, usedKeys, catalogue);
                        prefabKeys[prefab] = key;
                    }
                    if (key == null)
                    {
                        skippedUnpainted++;
                        continue;
                    }

                    var record = new Dictionary<string, object>
                    {
                        ["id"] = entity.Index,
                        ["prefab"] = key
                    };

                    if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                    {
                        Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                        if (owner != Entity.Null) record["owner"] = owner.Index;
                    }

                    double[][] nodeArray = new double[nodes.Length][];
                    for (int i = 0; i < nodes.Length; i++)
                    {
                        float3 position = nodes[i].m_Position;
                        nodeArray[i] = new[]
                        {
                            Math.Round((double)position.x, 3),
                            Math.Round((double)position.y, 3),
                            Math.Round((double)position.z, 3)
                        };
                    }
                    record["nodes"] = nodeArray;

                    int[][] triangleArray = new int[triangles.Length][];
                    double[][] rangeArray = new double[triangles.Length][];
                    bool anyRange = false;
                    for (int i = 0; i < triangles.Length; i++)
                    {
                        Game.Areas.Triangle triangle = triangles[i];
                        int3 indices = triangle.m_Indices;
                        triangleArray[i] = new[] { indices.x, indices.y, indices.z };

                        // The game's own answer to "how far does the ground depart from this
                        // flat triangle": GeometrySystem.GetHeightRange walks every heightmap
                        // cell the triangle covers and takes min/max RELATIVE TO ITS PLANE,
                        // water included. AreaRenderSystem then draws the triangle as a prism
                        // spanning it and projects the surface material onto the terrain
                        // inside — the area is paint, not a plane (contract §8b, audit §30).
                        // Free to carry: the buffer is already in hand.
                        //
                        Bounds1 range = triangle.m_HeightRange;
                        rangeArray[i] = new[]
                        {
                            Math.Round((double)range.min, 3),
                            Math.Round((double)range.max, 3)
                        };
                        anyRange |= (range.min != 0f) || (range.max != 0f);
                    }
                    record["triangles"] = triangleArray;
                    if (anyRange) record["heightRanges"] = rangeArray;

                    writer.WriteLine(JsonConvert.SerializeObject(record));
                    written++;
                }
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Surface areas failed: {ex.Message}");
                return;
            }
            finally
            {
                areas.Dispose();
            }

            try
            {
                File.WriteAllText(IOUtils.CombinePath(outputDirectory, "areaprefabs.json"),
                                  JsonConvert.SerializeObject(catalogue, Formatting.Indented));
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Could not write areaprefabs.json: {ex.Message}");
            }

            int unresolvedPrefabs = 0, normalMaps = 0, maskMaps = 0, prioritised = 0;
            foreach (object value in catalogue.Values)
            {
                if (!(value is Dictionary<string, object> catalogued)) continue;
                if (catalogued.ContainsKey("unresolved")) unresolvedPrefabs++;
                if (catalogued.ContainsKey("normalMap")) normalMaps++;
                if (catalogued.ContainsKey("maskMap")) maskMaps++;
                if (catalogued.TryGetValue("rendererPriority", out object priority)
                    && (priority is int order) && (order != 0)) prioritised++;
            }

            report.AppendLine($"Area materials: {normalMaps} normal map(s), {maskMaps} mask map(s), "
                            + $"{prioritised} prefab(s) with a non-zero rendererPriority (overlap draw order).");

            report.AppendLine($"Surface areas: {written} area(s) over {catalogue.Count} prefab(s); "
                            + $"{_pendingAreaTextures.Count} texture(s) queued for the writer task"
                            + ((skippedEmpty > 0) ? $"; {skippedEmpty} empty skipped" : "")
                            + ((skippedUnpainted > 0)
                                ? $"; {skippedUnpainted} unpainted area(s) excluded (prefab has no RenderedArea — "
                                + "the game never draws them; their triangles are in clipvolumes.jsonl)"
                                : "")
                            + ((unresolvedPrefabs > 0)
                                ? $"; {unresolvedPrefabs} prefab(s) UNRESOLVED — asset not loaded this session, "
                                + "the game drew those areas grey (Missing Area); enable the packs and re-rip"
                                : ""));
        }

        /// <summary>
        /// Add one area prefab to the catalogue: its name, everything <c>AreaBatchSystem</c> pushes
        /// onto the batch material for it, and each authored map captured into
        /// <c>terrain/surfaces/</c>.
        /// <b>Only what the game actually pushes is written.</b> The scalar PBR and edge parameters
        /// live on <c>RenderedArea</c> but reach the shader only under
        /// <c>Version.OverrideMaterial</c> (<c>AreaBatchSystem</c> 2103-2113); for an
        /// <c>Original</c> prefab the baked material already carries them, so they are read back
        /// off it instead. <c>m_LodBias</c> is deliberately absent: it lands on
        /// <c>AreaGeometryData</c>, not on the <c>RenderedAreaData.m_LodBias</c> field that looks
        /// like its home and is never assigned, and all it does there is bias the per-triangle
        /// <c>m_MinLod</c> distance cull — the bundle ships every triangle, so it changes nothing a
        /// consumer draws.
        /// </summary>
        /// <param name="prefab">The area prefab entity.</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory.</param>
        /// <param name="usedKeys">Keys taken so far, for uniqueness over the run.</param>
        /// <param name="catalogue">The catalogue to add the entry to.</param>
        /// <returns>The catalogue key the records reference, or <c>null</c> for a prefab the game
        /// structurally never paints (no <c>RenderedArea</c> — the Clip Surface); the caller skips
        /// those areas.</returns>
        private string CatalogueAreaPrefab(Entity prefab, string terrainDirectory,
                                           HashSet<string> usedKeys, Dictionary<string, object> catalogue)
        {
            string name = Instance.Name.GetPrefabName(prefab);
            if (string.IsNullOrEmpty(name)) name = $"area_{prefab.Index}";

            string key = MeshRipSystem.SanitizeAssetId(name);
            if (!usedKeys.Add(key))
            {
                key = $"{key}_{prefab.Index}";
                usedKeys.Add(key);
            }

            var entry = new Dictionary<string, object> { ["name"] = name };
            catalogue[key] = entry;

            try
            {
                // A negative `PrefabData.m_Index` means the save references this surface by
                // PrefabID but the asset — an asset pack, in every observed case — was not
                // loaded this session: `PrefabSystem.TryGetPrefab` refuses it, and the game
                // itself draws those areas with `MeshSettingsData.m_MissingRenderedArea`
                // (grey paint). Colourless is therefore game-true for the session that ripped,
                // but flag it so the report and a consumer can tell "pack missing" apart from
                // "authored colourless" — the cure is enabling the pack and re-ripping.
                //
                if (!EntityManager.HasComponent<PrefabData>(prefab)
                    || !Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefab),
                                                     out PrefabBase prefabBase)
                    || (prefabBase == null))
                {
                    entry["unresolved"] = true;

                    // The game's full identity for the missing asset: `SurfacePrefab:Name (hash)`,
                    // where the hash is `Hash128.Calculate` over the source mod's PDX platform ID.
                    // Not reversible, but it convicts a candidate pack: hash its mod ID and compare.
                    //
                    if (EntityManager.HasComponent<PrefabData>(prefab))
                    {
                        PrefabData prefabData = EntityManager.GetComponentData<PrefabData>(prefab);
                        if (prefabData.m_Index < 0)
                            entry["prefabId"] = Instance.Prefab.GetObsoleteID(prefabData).ToString();
                    }
                    return key;
                }

                if (!prefabBase.TryGet(out RenderedArea rendered) || (rendered == null))
                {
                    // Resolved, but no `RenderedArea`: only that component's archetype hook puts
                    // `Game.Areas.Batch` on an area entity, and every AreaBatchSystem draw/cull
                    // query requires `Batch` — the game structurally never paints these. The one
                    // case in the wild is the map editor's Clip Surface, whose triangles already
                    // ship through clipvolumes.jsonl (§8c); painting them would draw boxes over
                    // every tunnel portal that the game does not draw. Return null so the caller
                    // skips the areas and counts them.
                    //
                    catalogue.Remove(key);
                    return null;
                }

                {
                    // The two authored numbers a consumer otherwise has to invent. `m_UVScale`
                    // goes straight to the shader as `colossal_UVScale` (AreaBatchSystem) and
                    // multiplies the WORLD position — the area's texture is world-planar, not
                    // UV-mapped, so without it every consumer guesses a metres-per-repeat.
                    // `RenderedAreaData.m_HeightOffset` expands the projection prism beyond the
                    // terrain's own departure from the triangle (contract §8b, audit §30).
                    //
                    entry["uvScale"] = Math.Round((double)rendered.m_UVScale, 5);
                    if (EntityManager.HasComponent<RenderedAreaData>(prefab))
                    {
                        RenderedAreaData data = EntityManager.GetComponentData<RenderedAreaData>(prefab);
                        entry["heightOffset"] = Math.Round((double)data.m_HeightOffset, 4);

                        // The game's own derived edge-rounding radius in metres:
                        // `clamp(m_Roundness, 0.01, 0.99) * GetMinNodeDistance(type) * 0.5`, set
                        // beside `m_HeightOffset` in the same batch build. The shader gets twice
                        // it as `colossal_AreaParameters.x`. Shipping the derived metres saves a
                        // consumer having to know the per-area-type node distance.
                        //
                        entry["expandAmount"] = Math.Round((double)data.m_ExpandAmount, 4);
                    }

                    // Pushed onto every area batch whatever `m_Version` says
                    // (`AreaBatchSystem` ~2088-2093): the authored edge roundness the radius above
                    // derives from, the mask deciding which decal layers project onto this
                    // surface, and the draw-order tiebreak — without which a consumer z-fights
                    // every overlap of two painted areas.
                    //
                    // The mask ships as flag names rather than as the raw bits, the way
                    // `instances.jsonl` already ships a decal's own `decalLayers` — one vocabulary
                    // for the same concept across the bundle.
                    //
                    entry["roundness"] = Math.Round((double)rendered.m_Roundness, 4);
                    entry["rendererPriority"] = rendered.m_RendererPriority;

                    string layers = rendered.m_DecalLayerMask.ToString();
                    if (!string.IsNullOrEmpty(layers) && !char.IsDigit(layers[0]))
                    {
                        List<string> names = new();
                        foreach (string layer in layers.Split(','))
                        {
                            string trimmed = layer.Trim();
                            if (trimmed.Length > 0) names.Add(trimmed);
                        }

                        if (names.Count > 0) entry["decalLayers"] = names.ToArray();
                    }

                    // The PNG encode is the expensive half; the capture (GPU readback) must stay
                    // on the main thread, so the pixels are queued and the writer task encodes.
                    //
                    void WriteAreaTexture(PendingTexture captured, string field, string suffix)
                    {
                        if (captured?.Pixels == null) return;
                        string fileName = $"area_{key}{suffix}.png";
                        string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
                        if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);
                        _pendingAreaTextures.Add(new KeyValuePair<string, PendingTexture>(
                            IOUtils.CombinePath(surfacesDirectory, fileName), captured));
                        entry[field] = $"terrain/surfaces/{fileName}";
                    }

                    // The branch keys on `m_Version`, as the game's batch build effectively
                    // does: the base material is `m_Material` when present (else the global
                    // `AreasConfigurationPrefab.m_RenderedAreaMaterial` template, with
                    // `m_Version` forced to `OverrideMaterial`), and the authored fields are
                    // pushed onto the clone whenever the version says `OverrideMaterial` —
                    // which a surface can carry WITH a baked material. Keying on
                    // `material == null` alone missed that case (audit T-F13b).
                    //
                    Material material = rendered.m_Material;
                    bool overridden = (rendered.m_Version == RenderedArea.Version.OverrideMaterial)
                                   || (material == null);
                    if (!overridden)
                    {
                        // `RenderedArea.Version.Original` — vanilla surfaces: the baked material
                        // carries colour and base map directly.
                        //
                        if (material.HasProperty("_BaseColor"))
                        {
                            Color color = material.GetColor("_BaseColor");
                            entry["color"] = new[]
                            {
                                Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                                Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                            };
                        }

                        Texture texture = material.mainTexture;
                        if ((texture == null) && material.HasProperty("_BaseColorMap"))
                            texture = material.GetTexture("_BaseColorMap");

                        if (texture != null)
                            WriteAreaTexture(MaterialRipSystem.Capture(texture, key, name, 1024), "texture", "");

                        // The rest of the surface's PBR, which the batch build never touches for
                        // an `Original` prefab precisely because the baked material already
                        // carries it: the clone inherits `_NormalMap`/`_MaskMap` and the four
                        // scalar weights, and the shader reads them at draw. Only what the
                        // material actually declares is shipped — `HasProperty` is the authored
                        // test here, the way `m_Version` is in the override branch below.
                        //
                        WriteMaterialTexture("_NormalMap", "normalMap", "_n");
                        WriteMaterialTexture("_MaskMap", "maskMap", "_m");
                        WriteMaterialFloat("_Metallic", "metallic");
                        WriteMaterialFloat("_Smoothness", "smoothness");
                        WriteMaterialFloat("_NormalOpacity", "normalOpacity");
                        WriteMaterialFloat("_MetallicOpacity", "metallicOpacity");
                        WriteMaterialFloat("_NormalAlphaSource", "normalAlphaSource");
                        WriteMaterialFloat("_MetallicAlphaSource", "metallicAlphaSource");

                        void WriteMaterialFloat(string property, string field)
                        {
                            if (material.HasProperty(property))
                                entry[field] = Math.Round((double)material.GetFloat(property), 4);
                        }

                        void WriteMaterialTexture(string property, string field, string suffix)
                        {
                            if (!material.HasProperty(property)) return;
                            Texture map = material.GetTexture(property);
                            if (map != null)
                                WriteAreaTexture(MaterialRipSystem.Capture(map, $"{key}{suffix}", name, 1024), field, suffix);
                        }
                    }
                    else
                    {
                        // `OverrideMaterial` — editor-authored surfaces, which is every
                        // asset-pack surface. The game pushes `_BaseColor` := `m_BaseColor`
                        // always. Maps follow `Initialize`'s rule: when ANY of the three
                        // override maps is authored, `_BaseColorMap` := `m_BaseColorMap` (a
                        // null asset CLEARS the slot, not a fallback); with none authored, the
                        // base material keeps its own map — the template has none, but a baked
                        // `m_Material` may.
                        //
                        Color color = rendered.m_BaseColor;
                        entry["color"] = new[]
                        {
                            Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                            Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                        };

                        // The scalar half of the override push, all of it unconditional inside
                        // this branch (`AreaBatchSystem` 2103-2113) and all of it read by the
                        // area shader. The two opacities and the two alpha sources come with the
                        // maps rather than after them: they are what decides how much of the
                        // normal map bends the surface and which channel of the mask drives
                        // metallic, so a consumer given `normalMap` without them is guessing.
                        //
                        entry["metallic"] = Math.Round((double)rendered.m_Metallic, 4);
                        entry["smoothness"] = Math.Round((double)rendered.m_Smoothness, 4);
                        entry["normalOpacity"] = Math.Round((double)rendered.m_NormalOpacity, 4);
                        entry["metallicOpacity"] = Math.Round((double)rendered.m_MetallicOpacity, 4);
                        entry["normalAlphaSource"] = Math.Round((double)rendered.m_NormalAlphaSource, 4);
                        entry["metallicAlphaSource"] = Math.Round((double)rendered.m_MetallicAlphaSource, 4);

                        // How the paint meets its own boundary: the fade and normal-bend ramps
                        // across the expanded edge band, the noise breaking that boundary up, and
                        // the normal strength at it. Without them a consumer draws a hard-edged
                        // polygon where the game draws a soft, ragged one.
                        //
                        entry["edgeNormal"] = Math.Round((double)rendered.m_EdgeNormal, 4);
                        entry["edgeFadeRange"] = Round2(rendered.m_EdgeFadeRange);
                        entry["edgeNormalRange"] = Round2(rendered.m_EdgeNormalRange);
                        entry["edgeNoise"] = Round2(rendered.m_EdgeNoise);

                        TextureAsset baseColorMap = null;
                        if (rendered.m_BaseColorMap != null) baseColorMap = rendered.m_BaseColorMap;

                        TextureAsset normalMap = null;
                        if (rendered.m_NormalMap != null) normalMap = rendered.m_NormalMap;

                        TextureAsset maskMap = null;
                        if (rendered.m_MaskMap != null) maskMap = rendered.m_MaskMap;

                        // `Initialize` runs for all three maps or for none, so an authored normal
                        // or mask map ships here on exactly the same gate as the base colour —
                        // and an unauthored one is a CLEARED slot, not a fallback, so writing no
                        // field for it is game-true.
                        //
                        if (normalMap != null)
                            WriteAreaTexture(MaterialRipSystem.ReadBack(normalMap, $"{key}_n", 1024), "normalMap", "_n");
                        if (maskMap != null)
                            WriteAreaTexture(MaterialRipSystem.ReadBack(maskMap, $"{key}_m", 1024), "maskMap", "_m");

                        if (baseColorMap != null)
                        {
                            WriteAreaTexture(MaterialRipSystem.ReadBack(baseColorMap, key, 1024), "texture", "");
                        }
                        else if ((material != null) && (normalMap == null) && (maskMap == null))
                        {
                            Texture texture = material.mainTexture;
                            if ((texture == null) && material.HasProperty("_BaseColorMap"))
                                texture = material.GetTexture("_BaseColorMap");

                            if (texture != null)
                                WriteAreaTexture(MaterialRipSystem.Capture(texture, key, name, 1024), "texture", "");

                            // None of the three overrides authored, so the base material keeps its
                            // own maps too — the same inheritance the base colour above relies on.
                            //
                            if (material.HasProperty("_NormalMap"))
                            {
                                Texture map = material.GetTexture("_NormalMap");
                                if (map != null)
                                    WriteAreaTexture(MaterialRipSystem.Capture(map, $"{key}_n", name, 1024), "normalMap", "_n");
                            }

                            if (material.HasProperty("_MaskMap"))
                            {
                                Texture map = material.GetTexture("_MaskMap");
                                if (map != null)
                                    WriteAreaTexture(MaterialRipSystem.Capture(map, $"{key}_m", name, 1024), "maskMap", "_m");
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Area prefab `{name}` material did not rip: {ex.Message}");
            }

            return key;
        }

        /// <summary>
        /// One of the area shader's <c>float2</c> edge parameters, rounded for the catalogue.
        /// The game widens each to a <c>float4</c> with two zeroes on the way to the shader; the
        /// two authored components are the whole of it.
        /// </summary>
        /// <param name="value">The authored pair.</param>
        /// <returns>The rounded pair.</returns>
        private static double[] Round2(float2 value)
            => new[] { Math.Round((double)value.x, 4), Math.Round((double)value.y, 4) };

        /// <summary>
        /// Read the theme's ground textures off the live global shader bindings and write them
        /// under <c>terrain/surfaces/</c>.
        /// <c>TerrainMaterialSystem.ApplyRenderSettings</c> binds them as
        /// <c>colossal_Terrain*Diffuse</c> whenever a map is loaded, so reading the binding takes
        /// the exact textures the terrain is being drawn with — no asset-database round trip.
        /// </summary>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory.</param>
        /// <param name="report">The report to note results in.</param>
        /// <returns>Surface name to bundle-relative texture path.</returns>
        /// <summary>
        /// Per-ground capture provenance, filled by <see cref="CaptureSurfaceTextures"/> and
        /// emitted as the <c>rasters.groundSources</c> block. See the note at its write site.
        /// </summary>
        private readonly Dictionary<string, object> _groundSources = new Dictionary<string, object>();

        private Dictionary<string, string> CaptureSurfaceTextures(string terrainDirectory, StringBuilder report)
        {
            Dictionary<string, string> written = new();
            string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
            if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);

            // How many paintable extras the user splatmap can actually weight. `ApplyToSplatmap`
            // maps Extra1..4 onto the x..w components of the brush vector, but the target texture
            // is what decides how many of those components exist — and it is R8G8_UNorm, two
            // channels, so the Extra3 and Extra4 writes land nowhere. Their ground textures are
            // therefore unreferenceable by any consumer, and capturing them cost 8.29 MiB of a
            // full-city bundle for weights no pixel can carry.
            //
            // Gated on the format rather than by name, so a game patch that widens the texture
            // re-enables them here automatically instead of silently shipping dead files again.
            //
            int paintableExtras = CountPaintableExtras(report);

            // The theme binds a diffuse AND a tangent normal per ground, plus up to four
            // paintable extras (`colossal_TerrainExtra*`) most themes leave unbound — those
            // skip silently. Normal maps go through the same two-channel-packing rebuild the
            // material rip applies (§24.4); the extras carry the surfaces a player paints,
            // weighted by the user splatmap.
            //
            foreach (string[] entry in new[]
            {
                new[] { "grass", "colossal_TerrainGrassDiffuse", "core" },
                new[] { "grass_normal", "colossal_TerrainGrassNormal", "core" },
                new[] { "dirt", "colossal_TerrainDirtDiffuse", "core" },
                new[] { "dirt_normal", "colossal_TerrainDirtNormal", "core" },
                new[] { "rock", "colossal_TerrainRockDiffuse", "core" },
                new[] { "rock_normal", "colossal_TerrainRockNormal", "core" },
                new[] { "extra1", "colossal_TerrainExtra1Diffuse", "extra" },
                new[] { "extra1_normal", "colossal_TerrainExtra1Normal", "extra" },
                new[] { "extra2", "colossal_TerrainExtra2Diffuse", "extra" },
                new[] { "extra2_normal", "colossal_TerrainExtra2Normal", "extra" },
                new[] { "extra3", "colossal_TerrainExtra3Diffuse", "extra" },
                new[] { "extra3_normal", "colossal_TerrainExtra3Normal", "extra" },
                new[] { "extra4", "colossal_TerrainExtra4Diffuse", "extra" },
                new[] { "extra4_normal", "colossal_TerrainExtra4Normal", "extra" }
            })
            {
                string name = entry[0];
                bool core = entry[2] == "core";

                // An extra whose weight channel does not exist is skipped even when the theme
                // binds it — the theme is allowed to bind a ground the splatmap cannot select.
                //
                if (!core && (ExtraIndex(name) > paintableExtras)) continue;

                try
                {
                    Texture texture = Shader.GetGlobalTexture(entry[1]);
                    if (texture == null)
                    {
                        if (core) report.AppendLine($"Surface texture '{name}': not bound; skipped");
                        continue;
                    }

                    // Unbound slots are filled with Unity's built-in 4×4 placeholders
                    // (`ApplyRenderSettings` falls back to `Texture2D.grayTexture`); a real
                    // ground texture is never that small.
                    if (!core && ((texture.width <= 8) || (texture.height <= 8))) continue;

                    PendingTexture captured = MaterialRipSystem.Capture(texture, name, name, 1024);
                    if (captured?.Pixels == null)
                    {
                        report.AppendLine($"Surface texture '{name}': readback failed; skipped");
                        continue;
                    }

                    if (name.EndsWith("_normal", StringComparison.Ordinal))
                        MaterialRipSystem.ReconstructTwoChannelNormal(captured.Pixels);

                    byte[] png = Png.EncodeRgba(captured.Pixels, captured.Width, captured.Height);
                    File.WriteAllBytes(IOUtils.CombinePath(surfacesDirectory, $"{name}.png"), png);
                    written[name] = $"terrain/surfaces/{name}.png";

                    // Record WHICH asset this ground came from. Without it a bundle says only
                    // "this is extra2", never "extra2 was captured from <asset>", and a ground
                    // that looks wrong cannot be told from a ground that was captured wrong.
                    // Without it, painted beaches rendering as moss could not be diagnosed from
                    // the bundle at all: it named the ground but never the asset behind it.
                    _groundSources[name] = new Dictionary<string, object>
                    {
                        ["texture"] = texture.name ?? "(unnamed)",
                        ["boundAs"] = entry[1],
                        ["sourceSize"] = new[] { texture.width, texture.height },
                        ["writtenSize"] = new[] { captured.Width, captured.Height }
                    };
                }
                catch (Exception ex)
                {
                    _log.Warn($"Surface texture '{name}' failed: {ex.Message}");
                    report.AppendLine($"Surface texture '{name}': {ex.Message}");
                }
            }

            // Provenance goes in its own file, NOT into `rasters`: that object is closed in the
            // schema, so a key there would force a schema bump for what is pure diagnostics.
            // A new file is additive (contract §12).
            if (_groundSources.Count > 0)
            {
                Dictionary<string, object> sources = new Dictionary<string, object>
                {
                    ["semantics"] = "which texture asset each terrain/surfaces/<ground>.png was "
                                  + "captured from, and the shader global it was bound as. "
                                  + "Provenance only — nothing joins to it. It exists so a ground "
                                  + "that RENDERS wrong can be told apart from a ground that was "
                                  + "CAPTURED wrong; without it neither question is answerable "
                                  + "from a bundle at all",
                    ["grounds"] = new Dictionary<string, object>(_groundSources)
                };
                File.WriteAllText(IOUtils.CombinePath(surfacesDirectory, "sources.json"),
                                  JsonConvert.SerializeObject(sources, Formatting.Indented));
                foreach (KeyValuePair<string, object> pair in _groundSources)
                {
                    Dictionary<string, object> d = (Dictionary<string, object>)pair.Value;
                    report.AppendLine($"  {pair.Key} <- '{d["texture"]}' ({d["boundAs"]})");
                }
            }

            report.AppendLine($"Surfaces: {written.Count} ground texture(s) written");
            return written;
        }

        /// <summary>
        /// Which of <c>extra1..extra4</c> a surface name refers to, or 0 when it is not an extra.
        /// </summary>
        /// <param name="name">The surface name, e.g. <c>extra3_normal</c>.</param>
        /// <returns>1..4, or 0.</returns>
        private static int ExtraIndex(string name)
        {
            if ((name == null) || !name.StartsWith("extra", StringComparison.Ordinal)) return 0;
            return ((name.Length > 5) && (name[5] >= '1') && (name[5] <= '4')) ? name[5] - '0' : 0;
        }

        /// <summary>
        /// How many paintable extra grounds the user splatmap can actually weight — <b>all four</b>.
        /// <para>
        /// <b>This used to return 2, and that was the bug that lost the player's sand
        /// (audit SPLAT-6).</b> It reasoned "one weight per channel", read the
        /// <c>R8G8_UNorm</c> target's component count, and concluded that only Extra1 and
        /// Extra2 could carry weight — so <c>extra3</c> and <c>extra4</c> shipped no texture at
        /// all. The premise is wrong: the game packs <b>two 4-bit weights per channel</b>, which
        /// is how four extras fit in two bytes. <c>TerrainMaterialSystem.PackToBAChannels</c>
        /// writes exactly that layout — the four brush components at bit offsets 0, 4, 8 and 12
        /// across the two bytes.
        /// </para>
        /// <para>
        /// Confirmed against the game and against a real city: the locale names the four tools
        /// <c>Texture Paint Tool 01..04</c> = Dry / Lush / Dirt / <b>Sand</b>, and in a full-city
        /// rip each of the four nibbles is a separate, coherent, brush-shaped painted region —
        /// with Extra4 (Sand) tracing the shorelines, which is exactly where a player paints
        /// beaches. Two of the four grounds a city can paint were simply absent from the bundle.
        /// </para>
        /// <para>
        /// The count is now a constant, deliberately. Deriving it from the texture format is what
        /// went wrong, and no format inspection can reveal a packing convention.
        /// </para>
        /// </summary>
        /// <param name="report">The report to note the finding in.</param>
        /// <returns>4 — every extra the paint tools can write.</returns>
        private int CountPaintableExtras(StringBuilder report)
        {
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture user = materials?.UserSplatmap;
                if (user == null) return 4;

                // Reported, not used as a gate: the component count is real information about the
                // target, it just does not bound how many weights that target carries.
                int channels = (int)UnityEngine.Experimental.Rendering.GraphicsFormatUtility
                                    .GetComponentCount(user.graphicsFormat);
                report.AppendLine($"Surfaces: user splatmap is {channels}-channel, carrying 4 "
                                + "paintable extras as 4-bit nibble pairs (two per channel)");
                return 4;
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the user splatmap's format: {ex.Message}");
                report.AppendLine($"Surfaces: user splatmap format unreadable ({ex.Message}); "
                                + "capturing all four paintable extras");
                return 4;
            }
        }

        /// <summary>
        /// Read back the game's splat weight map — the per-pixel ground classification the
        /// terrain shader blends grass/dirt/rock (and the painted surfaces) with.
        /// <c>TerrainMaterialSystem.UpdateSplatmap</c> renders it on the GPU from the modified
        /// height cascade, water, noise and the user brush (<c>_SplatHeightVariance</c>,
        /// <c>_SplatRockLimit</c>, <c>_SplatGrassLimit</c>), into a 4096² RGBA8 render texture
        /// bound globally as <c>colossal_Splatmap</c> and exposed as the public
        /// <c>splatmap</c> property. It spans exactly the playable area:
        /// <c>world.xz = playableOffset + uv × playableArea</c> (its own <c>UpdateSplatmap</c>
        /// viewport math). Shipping it verbatim replaces the consumer-side per-cell ground
        /// classification with the game's own smooth per-pixel rule.
        /// <b>The channel-to-ground assignment is published, not left to the consumer.</b> The map
        /// is <c>R = dirt, G = grass, B = extra1, A = extra2</c>, with <b>rock as the implicit
        /// base</b> — the ground that renders where R falls away, which is what a cliff face is.
        /// The grounds composite in channel order over that base, each at
        /// <c>saturate(w · splatMultiplier)^splatPower</c>; they are <b>not</b> a partition to be
        /// normalised.
        /// <para>
        /// <b>R and G were the wrong way round until 2026-08-10 (audit SPLAT-3), and the fix is
        /// the reason this doc-comment is long.</b> The earlier reading called R grass because R
        /// "falls monotonically with slope"; so does every soil weight, because rock is R's own
        /// residual, so that observation never distinguished them. G was then assigned "by
        /// elimination" — the one inference in this block that was never measured, and the one
        /// that was wrong. Two measurements settle it, and neither is a curve fit:
        /// </para>
        /// <list type="bullet">
        /// <item><c>G = grass</c> — G is <b>exactly 0 on every texel under 1 m or more of
        /// water</b> (mean 0.0 across 58% of the map) and averages 201.7 on flat land, falling to
        /// 0.5 on 45°+ cliffs. A weight that is identically zero on every lakebed and every cliff
        /// is a vegetation mask; nothing else in the ground set behaves that way.</item>
        /// <item><c>R = dirt</c> — R averages <b>249.9 underwater</b>, rising with depth, and 251.6
        /// on flat land. A weight that blankets the seabed is sediment, not grass. Its slope
        /// falloff (251.6 flat → 9.4 at 45°+) is what uncovers the rock base.</item>
        /// <item><c>B</c>, <c>A</c> — measured exactly: non-zero on precisely the pixels the
        /// player painted with Extra1/Extra2, a biconditional with zero counterexamples over all
        /// 16,777,216 pixels. Note that <c>ApplyToSplatmap</c> writes the <i>user</i> splatmap's
        /// R/G, not these channels, so it corroborates the pairing but does not name the index.</item>
        /// </list>
        /// <para>
        /// The old map inverted the whole city: it painted the land dirt-brown and the lakebed
        /// grass-green, which is exactly what the reference consumer rendered. The channel
        /// semantics live in HLSL that ships compiled, so this remains an empirical derivation —
        /// but one resting on physical facts about where grass can grow rather than on a curve fit.
        /// </para>
        /// </summary>
        /// <param name="grounds">The ground textures the theme actually bound, from <see cref="CaptureSurfaceTextures"/>.</param>
        /// <param name="block">Receives the <c>rasters.json</c> "splatmap" block, or <c>null</c>.</param>
        /// <param name="report">The report to note results in.</param>
        /// <returns>The captured pixels for the writer task, or <c>null</c>.</returns>
        private PendingTexture CaptureSplatmap(Dictionary<string, string> grounds,
                                               out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture splat = materials?.splatmap;
                if (splat == null)
                {
                    report.AppendLine("Splatmap: not resident; consumers fall back to per-cell classification");
                    return null;
                }

                PendingTexture captured = MaterialRipSystem.Capture(splat, "splatmap", "terrain splatmap", 0);
                if (captured?.Pixels == null)
                {
                    report.AppendLine("Splatmap: readback failed; consumers fall back to per-cell classification");
                    return null;
                }

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;

                // Each channel is named only if the theme actually bound the ground it weights.
                // A theme that binds no grass ground cannot have a grass channel, and shipping the
                // name anyway would send a consumer to a texture that is not there.
                //
                bool dirtBound = (grounds != null) && grounds.ContainsKey("dirt");
                bool grassBound = (grounds != null) && grounds.ContainsKey("grass");
                bool rockBound = (grounds != null) && grounds.ContainsKey("rock");

                // Texel-centre geo-referencing, same convention as the terrain grid: top-left
                // texel centre, rows toward −Z.
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "GPU-generated ground-blend weights (height variance, rock/grass "
                                  + "limits, noise, user brush). Channels are named in `channels` "
                                  + "below — do not calibrate them from slope, which cannot tell "
                                  + "dirt from grass: rock is the dirt channel's own residual, so "
                                  + "both soil weights fall with slope together (schema §8)",

                    // The names are `terrain/surfaces/index.json` keys, so `channels.R` joins
                    // straight to a ground texture with no lookup table on the consumer's side.
                    //
                    ["channels"] = new Dictionary<string, object>
                    {
                        ["R"] = dirtBound ? "dirt" : "unknown",
                        ["G"] = grassBound ? "grass" : "unknown",

                        // B and A are NOT single weights. Each packs two 4-bit weights, which is
                        // how four paintable extras fit in the R8G8 user splatmap. Reading either
                        // as one 0-255 opacity silently merges two different grounds into one and
                        // drops the other two entirely (audit SPLAT-6).
                        ["B"] = "extra1:extra2 (packed, low:high nibble)",
                        ["A"] = "extra3:extra4 (packed, low:high nibble)",
                        ["packed"] = new Dictionary<string, object>
                        {
                            ["B.low"] = "extra1",
                            ["B.high"] = "extra2",
                            ["A.low"] = "extra3",
                            ["A.high"] = "extra4",
                            ["decode"] = "extraN weight = (channel & 15) or (channel >> 4), "
                                       + "each 0..15; normalise by 15, NOT by 255"
                        },

                        // Rock is drawn where the dirt weight falls away — a cliff face — so it is
                        // not a channel at all. It is the residual of R ALONE: R and G routinely
                        // sum well past 255 (both are near-saturated on flat land), so a consumer
                        // that treats the four weights as a partition and normalises them turns
                        // every meadow half brown.
                        //
                        ["base"] = rockBound ? "rock" : "unknown",
                        ["baseWeight"] = "max(0, 1 - min(1, R / 255 * splatMultiplier))"
                    },

                    // The composite the terrain shader's own multiplier/power imply, stated
                    // explicitly because the natural misreading — normalise the four weights — is
                    // the one that inverts the map.
                    //
                    ["blend"] = "composite over `base` in order: R (dirt), G (grass), then the "
                              + "four packed extras extra1..extra4 unpacked from B and A. The two "
                              + "soil channels use opacity saturate(w / 255 * splatMultiplier)"
                              + "^splatPower; each extra uses saturate(nibble / 15 * "
                              + "splatMultiplier)^splatPower. Not a partition: do not normalise",

                    // Per-channel, because these four are not equally well established and a
                    // consumer deserves to know which one to doubt.
                    //
                    ["channelProvenance"] = new Dictionary<string, object>
                    {
                        ["R"] = dirtBound
                              ? "measured — averages 249.9 under water (rising with depth) and "
                              + "251.6 on flat land; a weight that blankets the seabed is sediment. "
                              + "Falls 251.6 → 9.4 from flat to 45°+ cliff, uncovering the rock base"
                              : "unknown — this theme binds no dirt ground, so R is not named",
                        ["G"] = grassBound
                              ? "measured — exactly 0 on every texel under 1 m or more of water and "
                              + "0.5 on 45°+ cliffs, against 201.7 on flat land; a weight that is "
                              + "identically zero on every lakebed and every cliff is a vegetation mask"
                              : "unknown — this theme binds no grass ground, so G is not named",
                        ["B"] = "packed pair — TerrainMaterialSystem.PackToBAChannels writes the "
                              + "four brush components at bit offsets 0, 4, 8, 12 across B and A, "
                              + "so B carries extra1 (low nibble) and extra2 (high). Each nibble "
                              + "is a separate coherent painted region in a full-city rip",
                        ["A"] = "packed pair — extra3 (low nibble) and extra4 (high). The game's "
                              + "locale names the four paint tools Dry / Lush / Dirt / Sand for "
                              + "extra1..extra4, and extra4 traces the shorelines in a real city, "
                              + "which is where a player paints beaches",
                        ["base"] = "measured — the ground that renders where R falls to ~0, which is "
                              + "what a cliff face is"
                    },

                    ["splatMultiplier"] = Math.Round((double)materials.SplatMultiplier, 4),
                    ["splatPower"] = Math.Round((double)materials.SplatPower, 4)
                };

                report.AppendLine($"Splatmap: {captured.Width}x{captured.Height} ground-blend weights "
                                + $"captured (multiplier {materials.SplatMultiplier:F2}, power {materials.SplatPower:F2})");
                report.AppendLine($"  channels: R={(dirtBound ? "dirt" : "unknown")} "
                                + $"G={(grassBound ? "grass" : "unknown")} B=extra1 A=extra2, "
                                + $"{(rockBound ? "rock" : "unknown")} as the base under R");
                if (!dirtBound || !grassBound || !rockBound)
                {
                    List<string> missing = new List<string>();
                    if (!dirtBound) missing.Add("dirt");
                    if (!grassBound) missing.Add("grass");
                    if (!rockBound) missing.Add("rock");
                    report.AppendLine($"  note: this theme binds no {string.Join("/", missing)} ground, "
                                    + "so those channels ship unnamed rather than guessed");
                }
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"Splatmap capture failed: {ex.Message}");
                report.AppendLine($"Splatmap: {ex.Message}; consumers fall back to per-cell classification");
                return null;
            }
        }

        /// <summary>
        /// Read back the user's painted-surface weights — the brush strokes that weight the
        /// paintable extra grounds (<c>terrain/surfaces/extra*.png</c>).
        /// <c>TerrainMaterialSystem.UserSplatmap</c> (public; R8G8, playable extent, same
        /// mapping as the splatmap). Captured only when it exists and any texel is non-zero —
        /// a city with no painted surfaces ships nothing.
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "userSplatmap" block, or <c>null</c>.</param>
        /// <param name="report">The report.</param>
        /// <returns>The captured pixels, or <c>null</c>.</returns>
        private PendingTexture CaptureUserSplatmap(out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture user = materials?.UserSplatmap;
                if (user == null) return null;

                PendingTexture captured = MaterialRipSystem.Capture(user, "user_splatmap", "user splatmap", 0);
                if (captured?.Pixels == null) return null;

                bool painted = false;
                for (int i = 0; (i < captured.Pixels.Length) && !painted; i += 4)
                {
                    painted = (captured.Pixels[i] != 0) || (captured.Pixels[i + 1] != 0);
                }
                if (!painted) return null;

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/user_splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "player-painted surface weights for the paintable extra grounds "
                                  + "in terrain/surfaces/; channels are named in `channels` below",

                    // Straight off the paint path: ApplyToSplatmap switches on TerrainMaterialType
                    // and sets one component of the brush vector, and the target is R8G8 — so the
                    // enum's own ordering *is* the channel map, with no inference involved.
                    //
                    // Two channels, four grounds: each byte packs two 4-bit weights. Reading R or
                    // G as a single 0-255 weight merges two grounds and loses two (audit SPLAT-6).
                    //
                    ["channels"] = new Dictionary<string, object>
                    {
                        ["R"] = "extra1:extra2 (packed, low:high nibble)",
                        ["G"] = "extra3:extra4 (packed, low:high nibble)",
                        ["packed"] = new Dictionary<string, object>
                        {
                            ["R.low"] = "extra1",
                            ["R.high"] = "extra2",
                            ["G.low"] = "extra3",
                            ["G.high"] = "extra4",
                            ["decode"] = "extraN weight = (channel & 15) or (channel >> 4), "
                                       + "each 0..15; normalise by 15, NOT by 255"
                        }
                    },

                    ["channelProvenance"] = "measured — TerrainMaterialSystem.PackToBAChannels "
                                          + "writes the four brush components at bit offsets 0, 4, "
                                          + "8 and 12 across the two bytes, which is how four "
                                          + "extras fit an R8G8_UNorm target; each nibble is a "
                                          + "separate coherent painted region in a full-city rip"
                };

                report.AppendLine($"User splatmap: painted surfaces present, {captured.Width}x{captured.Height} captured");
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"User splatmap capture failed: {ex.Message}");
                return null;
            }
        }

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

        /// <summary>
        /// Read the water appearance parameters — the authored numbers every water pixel's look
        /// depends on, which the level grid alone cannot carry.
        /// <c>WaterRenderSystem</c> resolves one <c>WaterRenderSettingsPrefab</c> per map
        /// (<c>currentWaterRenderSettings</c>, the same entity its <c>ApplyMaterialParams</c>
        /// reads) and pushes the fields verbatim onto every <c>WaterSurface</c>; the one bind-time
        /// transform is <c>endSmoothness = min(smoothnessDistant, smoothnessClose)</c>. Shipped as
        /// a <c>rasters.json</c> block like <c>splatMultiplier</c>/<c>splatPower</c>, so the
        /// manifest scan lifts it with the grids it colours.
        /// </summary>
        /// <param name="report">The report to note the outcome in.</param>
        /// <returns>The <c>rasters.json</c> "water" block, or <c>null</c>.</returns>
        private Dictionary<string, object> CaptureWaterSettings(StringBuilder report)
        {
            try
            {
                Game.Rendering.WaterRenderSystem render =
                    World.GetOrCreateSystemManaged<Game.Rendering.WaterRenderSystem>();
                Entity settingsEntity = render.currentWaterRenderSettings;
                if ((settingsEntity == Entity.Null)
                    || !Instance.Prefab.TryGetPrefab(settingsEntity,
                                                     out Game.Prefabs.Water.WaterRenderSettingsPrefab settings)
                    || (settings == null))
                {
                    report.AppendLine("Water appearance: no render settings prefab resolved; block omitted");
                    return null;
                }

                double[] Rgba(Color color) => new[]
                {
                    Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                    Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                };

                float seaLevel = Instance.Water.SeaLevel;
                var block = new Dictionary<string, object>
                {
                    ["semantics"] = "authored WaterRenderSettingsPrefab appearance parameters, "
                                  + "applied verbatim by WaterRenderSystem.ApplyMaterialParams "
                                  + "(the one transform: endSmoothness = min(smoothnessDistant, "
                                  + "smoothnessClose)); seaLevel is WaterSystem.SeaLevel in "
                                  + "world metres; ranges are [start, distance] camera metres",
                    ["prefab"] = settings.name,
                    ["seaLevel"] = Math.Round((double)seaLevel, 3),
                    ["waterColor"] = Rgba(settings.m_WaterColor),
                    ["scatteringColor"] = Rgba(settings.m_WaterScatteringColor),
                    ["absorptionDistance"] = Math.Round((double)settings.m_AbsorptionDistance, 4),
                    ["maxRefractionDistance"] = Math.Round((double)settings.m_MaxRefractionDistance, 4),
                    ["smoothnessClose"] = Math.Round((double)settings.m_SmoothnessClose, 4),
                    ["smoothnessDistant"] = Math.Round((double)settings.m_SmoothnessDistant, 4),
                    ["smoothnessRange"] = new[]
                    {
                        Math.Round((double)settings.m_SmoothnessRange.x, 3),
                        Math.Round((double)settings.m_SmoothnessRange.y, 3)
                    },
                    ["ambientScattering"] = Math.Round((double)settings.m_AmbientScattering, 4),
                    ["heightScattering"] = Math.Round((double)settings.m_HeightScattering, 4),
                    ["displacementScattering"] = Math.Round((double)settings.m_DisplacementScattering, 4),
                    ["directLightTipScattering"] = Math.Round((double)settings.m_DirectLightTipScattering, 4),
                    ["directLightBodyScattering"] = Math.Round((double)settings.m_DirectLightBodyScattering, 4),
                    ["foamAmount"] = Math.Round((double)settings.m_FoamAmount, 4),
                    ["foamFadeRange"] = new[]
                    {
                        Math.Round((double)settings.m_FoamFadeRange.x, 3),
                        Math.Round((double)settings.m_FoamFadeRange.y, 3)
                    },
                    ["causticsIntensity"] = Math.Round((double)settings.m_CausticsIntensity, 4),
                    ["causticsPlaneDistance"] = Math.Round((double)settings.m_CausticsPlaneDistance, 4),
                    ["wavesMultiplier"] = Math.Round((double)settings.m_WavesMultiplier, 4),
                    ["lakeWavesMultiplier"] = Math.Round((double)settings.m_LakeWavesMultiplier, 4),
                    ["minWaterAmountForWaves"] = Math.Round((double)settings.m_MinWaterAmountForWaves, 4),
                    ["ripplesWindSpeed"] = Math.Round((double)settings.m_RipplesWindSpeed, 4),
                    ["ripplesChaos"] = Math.Round((double)settings.m_RipplesChaos, 4)
                };

                report.AppendLine($"Water appearance: '{settings.name}' captured (sea level {seaLevel:F1} m)");
                return block;
            }
            catch (Exception ex)
            {
                _log.Warn($"Water appearance capture failed: {ex.Message}");
                report.AppendLine($"Water appearance: {ex.Message}; block omitted");
                return null;
            }
        }

        /// <summary>
        /// Read the theme's ground tiling — the authored metres-per-repeat behind every ground
        /// texture in <c>terrain/surfaces/</c>, which consumers otherwise guess (the §39 lesson).
        /// <c>TerrainMaterialSystem</c> resolves one <c>TerrainRenderSettingsPrefab</c> per map
        /// (<c>currentTerrainRenderSettings</c>) and binds each layer's
        /// <c>TerrainTilingData</c>/<c>TerrainLodBlendData</c> verbatim as
        /// <c>_Terrain*TextureTiling(Blend)</c>; the terrain shader's UV spans the playable area
        /// (<c>colossal_TerrainScale = 1 / playableArea</c>), so a tiling value is the repeat
        /// count across it. The extras tile with the grass layer
        /// (<c>_TerrainExtraTextureTiling</c> is bound from <c>m_TerrainGrassTiling</c>) and
        /// carry their four shading scalars instead. All FOUR extras ship: the claim that the
        /// user splatmap cannot weight extra3/extra4 was wrong, and SPLAT-6 disproved it — B and
        /// A each pack TWO 4-bit weights, so all four are paintable, and extra4 is the Sand the
        /// player paints beaches with. <c>TerrainRenderSettingsPrefab</c> carries identical
        /// <c>m_Extra3*</c>/<c>m_Extra4*</c> fields (verified by decompile).
        /// </summary>
        /// <param name="report">The report to note the outcome in.</param>
        /// <returns>The <c>rasters.json</c> "terrainTiling" block, or <c>null</c>.</returns>
        private Dictionary<string, object> CaptureTerrainTiling(StringBuilder report)
        {
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Entity settingsEntity = materials.currentTerrainRenderSettings;
                if ((settingsEntity == Entity.Null)
                    || !Instance.Prefab.TryGetPrefab(settingsEntity,
                                                     out Game.Prefabs.Terrain.TerrainRenderSettingsPrefab settings)
                    || (settings == null))
                {
                    report.AppendLine("Terrain tiling: no render settings prefab resolved; block omitted");
                    return null;
                }

                float2 area = Instance.Terrain.playableArea;

                Dictionary<string, object> Ground(Game.Prefabs.Terrain.TerrainTilingData tiling,
                                                  Game.Prefabs.Terrain.TerrainLodBlendData blend)
                {
                    return new Dictionary<string, object>
                    {
                        ["tiling"] = new[]
                        {
                            Math.Round((double)tiling.m_NearTiling, 3),
                            Math.Round((double)tiling.m_MidTiling, 3),
                            Math.Round((double)tiling.m_FarTiling, 3)
                        },
                        ["metresPerRepeat"] = new[]
                        {
                            Math.Round(area.x / math.max(tiling.m_NearTiling, 0.001f), 4),
                            Math.Round(area.x / math.max(tiling.m_MidTiling, 0.001f), 4),
                            Math.Round(area.x / math.max(tiling.m_FarTiling, 0.001f), 4)
                        },
                        ["lodBlend"] = new[]
                        {
                            Math.Round((double)blend.m_MidBlendStart, 3),
                            Math.Round((double)blend.m_MidBlendEnd, 3),
                            Math.Round((double)blend.m_FarBlendStart, 3),
                            Math.Round((double)blend.m_FarBlendEnd, 3)
                        }
                    };
                }

                Dictionary<string, object> Extra(float dirtOverride, float splatRange,
                                                 float blurDepth, float heightOffset)
                {
                    return new Dictionary<string, object>
                    {
                        ["dirtOverride"] = Math.Round((double)dirtOverride, 4),
                        ["splatRange"] = Math.Round((double)splatRange, 4),
                        ["blurDepth"] = Math.Round((double)blurDepth, 4),
                        ["heightOffset"] = Math.Round((double)heightOffset, 4)
                    };
                }

                var block = new Dictionary<string, object>
                {
                    ["semantics"] = "authored TerrainRenderSettingsPrefab ground tiling. `tiling` "
                                  + "is [near, mid, far] texture repeats across the playable area "
                                  + "(the terrain shader's UV spans it: colossal_TerrainScale = "
                                  + "1/playableArea), bound as _Terrain*TextureTiling; "
                                  + "metresPerRepeat = playableArea.x / tiling, precomputed. "
                                  + "`lodBlend` is [midStart, midEnd, farStart, farEnd] camera "
                                  + "metres (_Terrain*TextureTilingBlend). Layer names key into "
                                  + "terrain/surfaces/; extras tile with the grass layer "
                                  + "(_TerrainExtraTextureTiling is bound from the grass tiling) "
                                  + "and their effective splatRange is max(splatRange, blurDepth) "
                                  + "at bind time",
                    ["prefab"] = settings.name,
                    ["playableArea"] = new[]
                    {
                        Math.Round((double)area.x, 3), Math.Round((double)area.y, 3)
                    },
                    ["layers"] = new SortedDictionary<string, object>
                    {
                        ["grass"] = Ground(settings.m_TerrainGrassTiling, settings.m_TerrainGrassLodBlend),
                        ["dirt"] = Ground(settings.m_TerrainDirtTiling, settings.m_TerrainDirtLodBlend),
                        ["rock"] = Ground(settings.m_TerrainRockTiling, settings.m_TerrainRockLodBlend)
                    },
                    ["extras"] = new SortedDictionary<string, object>
                    {
                        ["extra1"] = Extra(settings.m_Extra1DirtOverride, settings.m_Extra1SplatRange,
                                           settings.m_Extra1BlurDepth, settings.m_Extra1HeightOffset),
                        ["extra2"] = Extra(settings.m_Extra2DirtOverride, settings.m_Extra2SplatRange,
                                           settings.m_Extra2BlurDepth, settings.m_Extra2HeightOffset),
                        ["extra3"] = Extra(settings.m_Extra3DirtOverride, settings.m_Extra3SplatRange,
                                           settings.m_Extra3BlurDepth, settings.m_Extra3HeightOffset),
                        ["extra4"] = Extra(settings.m_Extra4DirtOverride, settings.m_Extra4SplatRange,
                                           settings.m_Extra4BlurDepth, settings.m_Extra4HeightOffset)
                    },
                    ["triplanarHeightMapBlending"] = Math.Round((double)settings.m_TriplanarHeightMapBlending, 4),
                    ["triplanarBlendStrengthY"] = Math.Round((double)settings.m_TriplanarBlendStrengthY, 4)
                };

                report.AppendLine($"Terrain tiling: '{settings.name}' captured "
                                + $"(grass near {area.x / math.max(settings.m_TerrainGrassTiling.m_NearTiling, 0.001f):F1} m/repeat)");
                return block;
            }
            catch (Exception ex)
            {
                _log.Warn($"Terrain tiling capture failed: {ex.Message}");
                report.AppendLine($"Terrain tiling: {ex.Message}; block omitted");
                return null;
            }
        }

        // ------------------------------------------------------------------------------------
        // The roadbed stamp — the game's own flattening footprint, rasterised.
        //
        // ------------------------------------------------------------------------------------

        /// <summary>
        /// Stamp every net segment the game's road job flattens with into the surface index.
        /// The walk is <c>CullRoadsJob</c>'s, decompiled: edges contribute their two
        /// geometry halves, each connected node its strip set (four strips at a junction, two on
        /// a straight-through node, the ring pieces on a roundabout), and orphan nodes two
        /// quarter-circle cap pairs. Only compositions whose ground is actually pulled to the
        /// road surface are stamped — elevated decks and tunnels adjust terrain too, but
        /// downward/away, and their ground is not roadbed.
        /// </summary>
        /// <param name="surfaceIndex">The mask to stamp into, south-to-north rows.</param>
        /// <param name="heightData">The terrain grid, for the world-to-cell mapping.</param>
        private void StampRoadbed(byte[] surfaceIndex, TerrainHeightData heightData)
        {
            StampContext context = new() { Mask = surfaceIndex, Value = 1 };
            var edges = _edgeQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in edges)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    // The gate the game's job applies per lane section, applied per prefab here:
                    // FlattenTerrain becomes ShiftTerrain, ClipTerrain stays ClipTerrain.
                    //
                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & (Game.Net.GeometryFlags.FlattenTerrain | Game.Net.GeometryFlags.ClipTerrain)) == 0)
                    {
                        _skippedNoFlatten++;
                        continue;
                    }

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);

                    if (TryGetGroundComposition(composition.m_Edge, netGeometry, out TerrainComposition edgeTerrain))
                    {
                        EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                        StampSegment(in context, heightData, geometry.m_Start, edgeTerrain, true, true);
                        StampSegment(in context, heightData, geometry.m_End, edgeTerrain, true, true);
                        _edgesStamped++;
                    }

                    if (EntityManager.HasComponent<StartNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_StartNode, netGeometry, out TerrainComposition startTerrain)
                        && StampNode(in context, in context, heightData,
                                     EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain))
                    {
                        _nodesStamped++;
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_EndNode, netGeometry, out TerrainComposition endTerrain)
                        && StampNode(in context, in context, heightData,
                                     EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain))
                    {
                        _nodesStamped++;
                    }
                }
            }
            finally
            {
                edges.Dispose();
            }

            var orphans = _orphanQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in orphans)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & (Game.Net.GeometryFlags.FlattenTerrain | Game.Net.GeometryFlags.ClipTerrain)) == 0) continue;

                    Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
                    if (!TryGetGroundComposition(composition, netGeometry, out TerrainComposition terrain)) continue;

                    float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
                    if (!(width > 0f)) continue;

                    StampOrphan(in context, heightData,
                                EntityManager.GetComponentData<Node>(entity).m_Position, width, terrain);
                    _orphansStamped++;
                }
            }
            finally
            {
                orphans.Dispose();
            }
        }

        /// <summary>
        /// Whether a composition's ground is pulled to the road surface, and its terrain offsets.
        /// The game's <c>AddSegment</c> gives elevated compositions a downward-only clamp and
        /// tunnels an upward-only one — their terrain is shifted <i>away</i> from the road, not
        /// onto it — unless a side carries a transition, in which case the node handling sheds the
        /// elevated/tunnel state (the portal is ground). <c>HasSurface</c> excludes nets with no
        /// surface at all (power lines), whose clamp is clearance, not roadbed. Waterway nets
        /// (canals) share the elevated branch in the game's <c>AddSegment</c> —
        /// <c>Elevated || (m_MergeLayers &amp; Layer.Waterway)</c> — so their beds are shifted
        /// down and away, not pulled to a roadbed.
        /// </summary>
        /// <param name="composition">The composition entity.</param>
        /// <param name="netGeometry">The prefab's geometry data, for the waterway merge layer.</param>
        /// <param name="terrain">Receives the terrain offsets, or default.</param>
        /// <returns>Whether to stamp this composition.</returns>
        private bool TryGetGroundComposition(Entity composition, NetGeometryData netGeometry,
                                             out TerrainComposition terrain)
        {
            terrain = default;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return false;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            if ((data.m_State & CompositionState.HasSurface) == 0) return false;

            bool waterway = (netGeometry.m_MergeLayers & Layer.Waterway) != 0;
            if (waterway
                || (data.m_Flags.m_General & (CompositionFlags.General.Elevated | CompositionFlags.General.Tunnel)) != 0)
            {
                CompositionFlags.Side sides = data.m_Flags.m_Left | data.m_Flags.m_Right;
                if ((sides & (CompositionFlags.Side.LowTransition | CompositionFlags.Side.HighTransition)) == 0)
                {
                    if (waterway) _skippedWaterway++;
                    else _skippedForm++;
                    return false;
                }
            }

            if (EntityManager.HasComponent<TerrainComposition>(composition))
            {
                terrain = EntityManager.GetComponentData<TerrainComposition>(composition);
            }
            return true;
        }

        /// <summary>
        /// One stamping pass's target and rule, threaded through the shared segment walk.
        /// With <see cref="Cut"/> unset the fill is unconditional (the roadbed mask). With it
        /// set, a cell is written only where the terrain stands between
        /// <c>lane + CutBottom</c> and <c>lane + CutTop</c> — the clip band test.
        /// </summary>
        private struct StampContext
        {
            /// <summary>The mask, south-to-north rows.</summary>
            public byte[] Mask;

            /// <summary>The value written into covered cells.</summary>
            public byte Value;

            /// <summary>Whether the clip band test applies.</summary>
            public bool Cut;

            /// <summary>Band floor, metres above the lane surface.</summary>
            public float CutBottom;

            /// <summary>Band ceiling, metres above the lane surface.</summary>
            public float CutTop;

            /// <summary>
            /// Where to record each strip's curves and band, or <c>null</c> to record nothing.
            /// The parametric form of the very thing the mask flattens (contract §8c): the mask is
            /// the intersection of these volumes with <i>one</i> heightmap at 3.5 m, top-down, and
            /// that intersection cannot express a volume open at one height and closed at another,
            /// nor survive a consumer editing the terrain. Collected here rather than in a second
            /// walk because every clipping section in the game already passes through
            /// <see cref="StampSegment"/> with its trimmed curves and band in hand.
            /// </summary>
            public List<Dictionary<string, object>> Volumes;

            /// <summary>What produced this strip — <c>edge</c>, <c>node</c> or <c>orphan</c>.</summary>
            public string Kind;

            /// <summary>The net entity this strip belongs to, joining to <c>networks.jsonl</c>.</summary>
            public int Owner;
        }

        /// <summary>
        /// A composition's clip band, per the game's <c>CullRoadsJob.AddSegment</c> and
        /// <c>GenerateClipDataJob</c>.
        /// The base band is the composition's edge-height extremes (the start or end half, like
        /// the game's <c>isStart</c> select) plus <c>TerrainComposition.m_ClipHeightOffset</c>.
        /// Tunnel portals (Tunnel with a transition side) and open trenches (a Lowered side, not
        /// elevated) raise the top to <c>ElevationLimit·3</c> — 12 m for roads, 6 m for pathways
        /// (<c>m_ElevationLimit</c> is 4 m and 2 m respectively; read live, and asserted by the
        /// rip log's band-class lines). The raise follows the game's <c>AddSegment</c> branch
        /// chain: a surface-less composition (<c>HasSurface == 0</c>, power lines) and an
        /// elevated-or-waterway one (<c>Elevated || (m_MergeLayers &amp; Layer.Waterway)</c>)
        /// are dispatched <i>before</i> the tunnel and lowered branches, so their top is never
        /// raised. The job's own ±0.3 m expansion is applied last, taking
        /// the floor 0.3 m <i>below</i> the lane — the everyday shell that keeps crests out of
        /// surface roads. Deep tube sections keep the small base band: the terrain covering them
        /// sits far above it, so the band test cuts nothing there, which is the game's behaviour
        /// too (their cover is never clipped from above).
        /// </summary>
        /// <param name="composition">The composition entity.</param>
        /// <param name="netGeometry">The prefab's geometry data — <c>ElevationLimit·3</c> is the
        /// raised top, and the waterway merge layer gates the raise.</param>
        /// <param name="isStart">Which edge-height half applies.</param>
        /// <param name="terrain">Receives the terrain offsets, or default.</param>
        /// <param name="cutBottom">Receives the band floor.</param>
        /// <param name="cutTop">Receives the band ceiling.</param>
        /// <returns>Whether the band can cut anything at all.</returns>
        private bool TryGetClipBand(Entity composition, NetGeometryData netGeometry, bool isStart,
                                    out TerrainComposition terrain, out float cutBottom, out float cutTop)
        {
            terrain = default;
            cutBottom = 0f;
            cutTop = 0f;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return false;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            if (EntityManager.HasComponent<TerrainComposition>(composition))
            {
                terrain = EntityManager.GetComponentData<TerrainComposition>(composition);
            }

            float raisedTop = netGeometry.m_ElevationLimit * 3f;
            float3 baseHeights = isStart
                ? new float3(data.m_EdgeHeights.x, data.m_SurfaceHeight.min, data.m_EdgeHeights.y)
                : new float3(data.m_EdgeHeights.z, data.m_SurfaceHeight.min, data.m_EdgeHeights.w);
            cutBottom = math.cmin(baseHeights) + terrain.m_ClipHeightOffset.x;
            cutTop = math.cmax(baseHeights) + terrain.m_ClipHeightOffset.y;

            bool hasSurface = (data.m_State & CompositionState.HasSurface) != 0;
            bool waterway = (netGeometry.m_MergeLayers & Layer.Waterway) != 0;
            bool tunnel = (data.m_Flags.m_General & CompositionFlags.General.Tunnel) != 0;
            bool elevated = (data.m_Flags.m_General & CompositionFlags.General.Elevated) != 0;
            CompositionFlags.Side sides = data.m_Flags.m_Left | data.m_Flags.m_Right;
            bool transition = (sides & (CompositionFlags.Side.LowTransition | CompositionFlags.Side.HighTransition)) != 0;
            bool lowered = (sides & CompositionFlags.Side.Lowered) != 0;
            if (hasSurface && !elevated && !waterway
                && ((tunnel && transition) || (!tunnel && lowered)))
            {
                cutTop = math.max(cutTop, raisedTop);
            }
            cutBottom -= ClipBandExpansion;
            cutTop += ClipBandExpansion;

            LogClipBandClass(terrain, raisedTop, cutBottom, cutTop,
                             hasSurface, waterway, tunnel, elevated, transition, lowered);

            // An empty band cannot mark a cell; skip the walk.
            //
            return cutTop > cutBottom;
        }

        /// <summary>
        /// Distinct clip band classes already logged this rip, keyed by their rounded numbers.
        /// </summary>
        readonly HashSet<string> _clipBandClasses = new();

        /// <summary>
        /// Log each distinct clip band class once per rip — the live assertion that the shipped
        /// band arithmetic matches the game's (<c>ElevationLimit·3</c>, the per-piece
        /// <c>TerrainComposition</c> offsets, the ±0.3 m expansion).
        /// </summary>
        private void LogClipBandClass(TerrainComposition terrain, float raisedTop,
                                      float cutBottom, float cutTop, bool hasSurface, bool waterway,
                                      bool tunnel, bool elevated, bool transition, bool lowered)
        {
            string shape = !hasSurface ? "surfaceless"
                         : waterway ? "waterway"
                         : tunnel ? (transition ? "portal" : "tube")
                         : lowered ? "trench" : elevated ? "elevated" : "surface";
            string key = FormattableString.Invariant(
                $"clip band class {shape}: bottom {cutBottom:0.###} top {cutTop:0.###} (L*3 {raisedTop:0.###}, clipHeightOffset {terrain.m_ClipHeightOffset.x:0.###}/{terrain.m_ClipHeightOffset.y:0.###}, widthOffset {terrain.m_WidthOffset.x:0.###}/{terrain.m_WidthOffset.y:0.###})");
            if (_clipBandClasses.Add(key)) _log.Info(key);
        }

        /// <summary>
        /// Walk the game's clip sections, collecting each one's volume (contract §8c).
        /// Called with a null mask, which makes it collect-only: this used to rasterise
        /// <c>terrain/cutout.png</c>, the intersection of these volumes with one heightmap, and
        /// that mask was retired on 2026-08-08 in favour of shipping the volumes themselves.
        /// The walk is <c>CullRoadsJob</c>'s once more, but through <c>GenerateClipDataJob</c>'s
        /// gate: every edge segment, node strip and orphan cap of a net whose prefab carries
        /// <c>GeometryFlags.ClipTerrain</c> — tunnels and trenches <i>included</i> this time,
        /// because the clip is exactly what opens them. Areas whose prefab carries
        /// <c>ClipArea</c> join at the end (the pass's <c>areaClipMesh</c>). The band per
        /// composition is <see cref="TryGetClipBand"/>'s.
        /// </summary>
        /// <param name="cutout">The mask to stamp into, or <c>null</c> to collect only.</param>
        /// <param name="heightData">The terrain grid.</param>
        private void StampCutout(byte[] cutout, TerrainHeightData heightData)
        {
            StampContext context = new()
            {
                Mask = cutout, Value = 255, Cut = true,
                Volumes = _clipVolumes
            };
            _clipBandClasses.Clear();

            var edges = _edgeQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in edges)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & Game.Net.GeometryFlags.ClipTerrain) == 0) continue;

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);
                    bool stamped = false;

                    context.Owner = entity.Index;
                    context.Kind = "edge";

                    // The edge halves take the composition's start/end edge heights respectively,
                    // like the game's isStart select.
                    if (TryGetClipBand(composition.m_Edge, netGeometry, true, out TerrainComposition edgeTerrain,
                                       out context.CutBottom, out context.CutTop))
                    {
                        StampSegment(in context, heightData,
                                     EntityManager.GetComponentData<EdgeGeometry>(entity).m_Start, edgeTerrain, true, true);
                        stamped = true;
                    }
                    if (TryGetClipBand(composition.m_Edge, netGeometry, false, out edgeTerrain,
                                       out context.CutBottom, out context.CutTop))
                    {
                        StampSegment(in context, heightData,
                                     EntityManager.GetComponentData<EdgeGeometry>(entity).m_End, edgeTerrain, true, true);
                        stamped = true;
                    }
                    if (stamped) _cutEdges++;

                    context.Kind = "node";

                    // The roundabout right pair rides the end-half band (`AddNode` passes
                    // isStart: false there); a band that cannot cut gets a zero top, which the
                    // band test rejects everywhere.
                    if (EntityManager.HasComponent<StartNodeGeometry>(entity)
                        && TryGetClipBand(composition.m_StartNode, netGeometry, true, out TerrainComposition startTerrain,
                                          out context.CutBottom, out context.CutTop))
                    {
                        StampContext endContext = context;
                        if (!TryGetClipBand(composition.m_StartNode, netGeometry, false, out _,
                                            out endContext.CutBottom, out endContext.CutTop))
                        {
                            endContext.CutBottom = 0f;
                            endContext.CutTop = 0f;
                        }
                        if (StampNode(in context, in endContext, heightData,
                                      EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain))
                        {
                            _cutNodes++;
                        }
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetClipBand(composition.m_EndNode, netGeometry, true, out TerrainComposition endTerrain,
                                          out context.CutBottom, out context.CutTop))
                    {
                        StampContext endContext = context;
                        if (!TryGetClipBand(composition.m_EndNode, netGeometry, false, out _,
                                            out endContext.CutBottom, out endContext.CutTop))
                        {
                            endContext.CutBottom = 0f;
                            endContext.CutTop = 0f;
                        }
                        if (StampNode(in context, in endContext, heightData,
                                      EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain))
                        {
                            _cutNodes++;
                        }
                    }
                }
            }
            finally
            {
                edges.Dispose();
            }

            var orphans = _orphanQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in orphans)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & Game.Net.GeometryFlags.ClipTerrain) == 0) continue;

                    Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
                    if (!TryGetClipBand(composition, netGeometry, true,
                                        out TerrainComposition terrain, out context.CutBottom, out context.CutTop)) continue;

                    float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
                    if (!(width > 0f)) continue;

                    context.Owner = entity.Index;
                    context.Kind = "orphan";

                    StampOrphan(in context, heightData,
                                EntityManager.GetComponentData<Node>(entity).m_Position, width, terrain);
                    _cutOrphans++;
                }
            }
            finally
            {
                orphans.Dispose();
            }

            StampClipAreas(in context, heightData);
        }

        /// <summary>
        /// Stamp the areas whose prefab carries <c>ClipArea</c> — the terrain job draws their
        /// triangles into the same stencil (<c>areaClipMesh</c>). The volume is a CLOSED prism:
        /// <c>GenerateAreaClipMeshJob</c> duplicates the area ring at
        /// <c>min(0, aggMin) − 0.3</c> and <c>max(0, aggMax) + 0.3</c> (the aggregates run over
        /// every triangle's <c>m_HeightRange</c>, both starting at zero) and indexes a bottom
        /// cap, a top cap and the side walls. An earlier revision read the job as capping
        /// nothing above; the top ring and cap are right there in its index writes.
        /// </summary>
        /// <param name="baseContext">The clip pass's context.</param>
        /// <param name="heightData">The terrain grid.</param>
        private void StampClipAreas(in StampContext baseContext, TerrainHeightData heightData)
        {
            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            StampContext context = baseContext;

            // An area's geometry is triangles, not boundary curves — and since 2026-08-09 the
            // record carries them itself. It used to hold only a join key into `areas.jsonl`,
            // but §36 removed clip areas from that layer (the game never paints them — no
            // `RenderedArea`, no `Batch`), which left every clip-area volume referencing a
            // record that no longer exists; the first full rip after both changes failed 229
            // validator joins. The `area` id stays as provenance (schema 2, §12.2).
            //
            context.Volumes = null;

            var areas = _clipAreaQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in areas)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0)) continue;

                    // The prism band, per area: both aggregates start at zero exactly like the
                    // job's accumulators, so a flat area still gets the everyday ±0.3 m shell.
                    // Relative to the surface, the same plane-relative semantics as every strip
                    // band — the cut test hangs both offsets off each triangle's plane.
                    //
                    float aggMin = 0f, aggMax = 0f;
                    for (int i = 0; i < triangles.Length; i++)
                    {
                        Bounds1 range = triangles[i].m_HeightRange;
                        aggMin = math.min(aggMin, range.min);
                        aggMax = math.max(aggMax, range.max);
                    }
                    context.CutBottom = aggMin - ClipBandExpansion;
                    context.CutTop = aggMax + ClipBandExpansion;

                    if (_clipVolumes != null)
                    {
                        // Same encoding as areas.jsonl (§8b): world positions, millimetre-rounded.
                        //
                        double[][] nodeArray = new double[nodes.Length][];
                        for (int i = 0; i < nodes.Length; i++)
                        {
                            float3 position = nodes[i].m_Position;
                            nodeArray[i] = new[]
                            {
                                Math.Round((double)position.x, 3),
                                Math.Round((double)position.y, 3),
                                Math.Round((double)position.z, 3)
                            };
                        }

                        int[][] triangleArray = new int[triangles.Length][];
                        for (int i = 0; i < triangles.Length; i++)
                        {
                            int3 indices = triangles[i].m_Indices;
                            triangleArray[i] = new[] { indices.x, indices.y, indices.z };
                        }

                        _clipVolumes.Add(new Dictionary<string, object>
                        {
                            ["id"] = entity.Index,
                            ["kind"] = "area",
                            ["area"] = entity.Index,
                            ["bottom"] = Math.Round((double)context.CutBottom, 3),
                            ["top"] = Math.Round((double)context.CutTop, 3),
                            ["nodes"] = nodeArray,
                            ["triangles"] = triangleArray
                        });
                    }
                    _cutAreas++;

                    if (context.Mask == null) continue;

                    for (int i = 0; i < triangles.Length; i++)
                    {
                        int3 indices = triangles[i].m_Indices;
                        if ((math.cmin(indices) < 0) || (math.cmax(indices) >= nodes.Length)) continue;

                        float3 a = nodes[indices.x].m_Position;
                        float3 b = nodes[indices.y].m_Position;
                        float3 c = nodes[indices.z].m_Position;

                        // A triangle is a quad with one doubled corner to the scanline fill.
                        //
                        FillQuad(in context, heightData, width, height,
                                 ToCell(a, heightData), ToCell(b, heightData),
                                 ToCell(c, heightData), ToCell(c, heightData),
                                 (a.y + b.y + c.y) / 3f);
                    }
                }
            }
            finally
            {
                areas.Dispose();
            }
        }

        /// <summary>
        /// Stamp one node's strips — the flattened junction interior.
        /// The three cases are <c>CullRoadsJob.AddNode</c>'s: a roundabout rides its middle curve
        /// ring, a true junction (parted corners) covers both halves plus the two wedges to the
        /// middle curve — the petals — and a straight-through node spans half-to-half across the
        /// middle. The roundabout's elevation-transition ring trims (<c>MoveTowards</c> by the
        /// sync offsets) are omitted: they shave a few metres off the ring start at elevated
        /// ramps, so omitting them over-stamps slightly, never under.
        /// </summary>
        /// <returns>Whether the node cleared the draw gate and was stamped.</returns>
        /// <remarks>
        /// The roundabout's second strip pair is the one place the game evaluates a section with
        /// <c>isStart: false</c> (its <c>AddNode</c> passes it for both <c>node.m_Right</c>
        /// segments), so those two strips take <paramref name="contextEnd"/> — the band built
        /// from the composition's end-half edge heights. Every other strip, and both halves of
        /// every other node kind, is <c>isStart: true</c> and takes <paramref name="context"/>.
        /// </remarks>
        private bool StampNode(in StampContext context, in StampContext contextEnd,
                               TerrainHeightData heightData,
                               EdgeNodeGeometry node, TerrainComposition terrain)
        {
            // The game's own draw gate: nodes shorter than 5 cm on both sides draw nothing.
            //
            if (!(math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))) return false;

            Segment strip;
            if (node.m_MiddleRadius > 0f)
            {
                // Roundabout: outer half to the middle curve, middle curve to the inner half,
                // then the same for the right half against the middle curve's end point.
                // Approximation, counted for cut passes: the game's AddNode sheds the
                // elevated/tunnel + transition state (and MoveTowards-trims the ring ends) for
                // this second pair before building its band; the port keeps the raw flags, which
                // can only widen the band (over-cut), never narrow it.
                //
                if (context.Cut) _cutRoundaboutApprox++;

                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Left.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);

                strip = node.m_Right;
                strip.m_Right = new Bezier4x3(node.m_Middle.d, node.m_Middle.d, node.m_Middle.d, node.m_Middle.d);
                StampSegment(in contextEnd, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(in contextEnd, heightData, strip, terrain, false, true);
            }
            else if (math.lengthsq(node.m_Left.m_Right.d - node.m_Right.m_Left.d) > 0.0001f)
            {
                // A junction arm: both halves as authored, plus each half's wedge to the middle
                // curve — the same strips the game's terrain job flattens (its AddNode does
                // exactly this). The node MESH paves the same wedges above (audit §18); this is
                // the ground beneath them.
                //
                strip = node.m_Left;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, false, false);

                strip = node.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);
                strip.m_Right = strip.m_Left;
                strip.m_Left = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, false, false);
            }
            else
            {
                // Straight through: left half to the middle, middle to the right half.
                //
                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);
            }

            return true;
        }

        /// <summary>
        /// Stamp an orphan node's caps: two quarter-circle segment pairs per side, the game's own
        /// synthetic construction (<c>CullRoadsJob.AddOrphans</c>, world-axis aligned).
        /// </summary>
        private void StampOrphan(in StampContext context, TerrainHeightData heightData,
                                 float3 position, float width, TerrainComposition terrain)
        {
            const float kappa = 0.2761424f; // the game's own circle constant
            float half = width * 0.5f;

            Segment segment = default;
            Bezier4x3 centre = new(position, position, position, position);

            // +z half, left quarter.
            segment.m_Left = new Bezier4x3(
                new float3(position.x - half, position.y, position.z),
                new float3(position.x - half, position.y, position.z + (width * kappa)),
                new float3(position.x - (width * kappa), position.y, position.z + half),
                new float3(position.x, position.y, position.z + half));
            segment.m_Right = centre;
            segment.m_Length = new float2(width * (math.PI / 2f), 0f);
            StampSegment(in context, heightData, segment, terrain, true, false);

            // +z half, right quarter — the left curve mirrored about the node.
            //
            Segment mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x += width;
            mirrored.m_Right.b.x += width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(in context, heightData, mirrored, terrain, false, true);

            // -z half, both quarters.
            segment.m_Left = new Bezier4x3(
                new float3(position.x + half, position.y, position.z),
                new float3(position.x + half, position.y, position.z - (width * kappa)),
                new float3(position.x + (width * kappa), position.y, position.z - half),
                new float3(position.x, position.y, position.z - half));
            segment.m_Right = centre;
            StampSegment(in context, heightData, segment, terrain, true, false);

            mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x -= width;
            mirrored.m_Right.b.x -= width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(in context, heightData, mirrored, terrain, false, true);
        }

        /// <summary>
        /// Stamp the area between one segment's boundary béziers into the mask.
        /// The width trim is <c>AddSegment</c>'s: <c>TerrainComposition.m_WidthOffset</c> narrows
        /// each side by lerping its boundary toward the other, per control point, skipped on a
        /// side that is the shared middle of a node strip. The smoothing margin
        /// (<c>GetTerrainSmoothingWidth</c>) is deliberately not stamped: it is the blend apron
        /// where terrain <i>slopes</i> to meet the road, not the flattened bed itself.
        /// </summary>
        /// <param name="context">The target mask and stamp rule.</param>
        /// <param name="heightData">The terrain grid.</param>
        /// <param name="segment">The boundary curves.</param>
        /// <param name="terrain">The composition's terrain offsets.</param>
        /// <param name="trimLeft">Whether the left side takes its width offset.</param>
        /// <param name="trimRight">Whether the right side takes its width offset.</param>
        private static void StampSegment(in StampContext context, TerrainHeightData heightData,
                                         Segment segment, TerrainComposition terrain,
                                         bool trimLeft, bool trimRight)
        {
            if (math.any(math.isnan(segment.m_Left.a)) || math.any(math.isnan(segment.m_Right.a))) return;

            if (math.any(terrain.m_WidthOffset != 0f))
            {
                Segment authored = segment;
                float4 inverseWidths = 1f / math.max(0.001f, new float4(
                    math.distance(authored.m_Left.a.xz, authored.m_Right.a.xz),
                    math.distance(authored.m_Left.b.xz, authored.m_Right.b.xz),
                    math.distance(authored.m_Left.c.xz, authored.m_Right.c.xz),
                    math.distance(authored.m_Left.d.xz, authored.m_Right.d.xz)));

                if ((terrain.m_WidthOffset.x != 0f) && trimLeft)
                {
                    float4 t = terrain.m_WidthOffset.x * inverseWidths;
                    segment.m_Left = LerpCurve(authored.m_Left, authored.m_Right, t);
                }
                if ((terrain.m_WidthOffset.y != 0f) && trimRight)
                {
                    float4 t = terrain.m_WidthOffset.y * inverseWidths;
                    segment.m_Right = LerpCurve(authored.m_Right, authored.m_Left, t);
                }
            }

            // Recorded AFTER the width trim, so the curves published bound exactly the ribbon
            // this walk covers — a consumer rasterising them gets the same strip, not the
            // authored one.
            context.Volumes?.Add(ClipVolumeRecord(in context, segment));

            // A null mask means collect-only: the clip pass has no raster to fill since
            // `cutout.png` was retired, and the scanline below is the expensive half of the walk.
            // The roadbed pass still passes a real mask.
            //
            if (context.Mask == null) return;

            int width = heightData.resolution.x;
            int height = heightData.resolution.z;

            // One station per terrain cell of arc length keeps the fill within half a cell of the
            // true curve.
            float length = math.max(math.cmax(segment.m_Length), 1f);
            int steps = math.clamp((int)math.ceil(length * heightData.scale.x), 1, 1024);

            float3 previousLeft = segment.m_Left.a;
            float3 previousRight = segment.m_Right.a;

            for (int i = 1; i <= steps; i++)
            {
                float t = (float)i / steps;
                float3 currentLeft = Colossal.Mathematics.MathUtils.Position(segment.m_Left, t);
                float3 currentRight = Colossal.Mathematics.MathUtils.Position(segment.m_Right, t);

                // The quad spans one cell of arc; its corner mean is the lane height reference
                // the cut band hangs from.
                float laneY = (previousLeft.y + previousRight.y + currentLeft.y + currentRight.y) * 0.25f;
                FillQuad(in context, heightData, width, height,
                         ToCell(previousLeft, heightData), ToCell(previousRight, heightData),
                         ToCell(currentRight, heightData), ToCell(currentLeft, heightData), laneY);

                previousLeft = currentLeft;
                previousRight = currentRight;
            }
        }

        /// <summary>
        /// Write the collected clip volumes as <c>clipvolumes.jsonl</c> (contract §8c).
        /// <b>This layer costs bytes rather than saving them</b> — on a full city it is roughly
        /// 4× the <c>cutout.png</c> it replaces. It is shipped for fidelity: that mask was a 3.5 m
        /// top-down intersection against <i>one</i> heightmap, so it could not express a volume
        /// open at one height and closed at another, and it went stale the moment a consumer
        /// edited terrain. Nothing else in the bundle carries these curves — <c>networks.jsonl</c>
        /// has no curve field and <c>lanes.jsonl</c> carries lane polylines, not composition
        /// boundaries.
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <returns>The report line describing the outcome — returned rather than appended
        /// because this runs on the writer task, and the report belongs to the main thread.</returns>
        private string WriteClipVolumes(string outputDirectory)
        {
            if ((_clipVolumes == null) || (_clipVolumes.Count == 0))
            {
                return "Clip volumes: none; clipvolumes.jsonl omitted";
            }

            try
            {
                using StreamWriter writer = Carto.IO.BundleFile.CreateText(
                    IOUtils.CombinePath(outputDirectory, "clipvolumes.jsonl"));

                foreach (Dictionary<string, object> record in _clipVolumes)
                {
                    writer.WriteLine(JsonConvert.SerializeObject(record, Formatting.None));
                }

                return $"Clip volumes: {_clipVolumes.Count} strip(s) written to clipvolumes.jsonl "
                     + "(what the game's RoadClip pass hides; §8c)";
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write clipvolumes.jsonl: {ex.Message}");
                return $"Clip volumes: {ex.Message}; layer omitted";
            }
            finally
            {
                _clipVolumes = null;
            }
        }

        /// <summary>
        /// One clipping strip as a <c>clipvolumes.jsonl</c> record (contract §8c).
        /// The two boundary béziers, plus the band as a pair of offsets <b>relative to the lane
        /// surface</b> — which is not shipped separately because it is recoverable from the curves
        /// themselves: at parameter <c>t</c> the lane height is the mean of the two boundaries'
        /// <c>y</c>, exactly as the stamp below takes it from the four corners of each quad.
        /// </summary>
        /// <param name="context">The pass in progress, for the band and the strip's provenance.</param>
        /// <param name="segment">The trimmed boundary curves.</param>
        /// <returns>The record.</returns>
        private static Dictionary<string, object> ClipVolumeRecord(in StampContext context, Segment segment)
        {
            return new Dictionary<string, object>
            {
                ["id"] = context.Owner,
                ["kind"] = context.Kind,
                ["left"] = Curve(segment.m_Left),
                ["right"] = Curve(segment.m_Right),
                ["bottom"] = Math.Round((double)context.CutBottom, 3),
                ["top"] = Math.Round((double)context.CutTop, 3)
            };
        }

        /// <summary>
        /// A boundary bézier as its four control points, millimetre-rounded like every other
        /// coordinate in the bundle.
        /// </summary>
        /// <param name="curve">The curve.</param>
        /// <returns>Four <c>[x, y, z]</c> triples.</returns>
        private static double[][] Curve(Bezier4x3 curve)
        {
            return new[] { Point(curve.a), Point(curve.b), Point(curve.c), Point(curve.d) };
        }

        /// <summary>
        /// One control point, millimetre-rounded.
        /// </summary>
        /// <param name="value">The point.</param>
        /// <returns>The rounded triple.</returns>
        private static double[] Point(float3 value) => new[]
        {
            Math.Round((double)value.x, 3),
            Math.Round((double)value.y, 3),
            Math.Round((double)value.z, 3)
        };

        /// <summary>
        /// Lerp one boundary bézier toward another by a per-control-point parameter — the game's
        /// <c>MathUtils.Lerp(Bezier4x3, Bezier4x3, Bezier4x1)</c>.
        /// </summary>
        private static Bezier4x3 LerpCurve(Bezier4x3 from, Bezier4x3 to, float4 t)
        {
            return new Bezier4x3(
                math.lerp(from.a, to.a, t.x),
                math.lerp(from.b, to.b, t.y),
                math.lerp(from.c, to.c, t.z),
                math.lerp(from.d, to.d, t.w));
        }

        /// <summary>
        /// World position to (fractional) terrain cell — <c>TerrainUtils.ToHeightmapSpace</c>.
        /// </summary>
        private static float2 ToCell(float3 world, TerrainHeightData heightData)
        {
            return ((world + heightData.offset) * heightData.scale).xz;
        }

        /// <summary>
        /// Fill one convex quad in cell space, even-odd scanline.
        /// </summary>
        /// <remarks>
        /// The scratch buffers are static because the stamp runs single-threaded on the main
        /// thread; per-quad allocation over a city's ~10⁶ quads is the only alternative.
        /// </remarks>
        private static readonly float2[] _quadScratch = new float2[4];
        private static readonly float[] _crossingScratch = new float[4];

        private static void FillQuad(in StampContext context, TerrainHeightData heightData,
                                     int width, int height,
                                     float2 a, float2 b, float2 c, float2 d, float laneY)
        {
            float2[] poly = _quadScratch;
            poly[0] = a; poly[1] = b; poly[2] = c; poly[3] = d;
            float[] crossings = _crossingScratch;

            float minY = math.max(0f, math.min(math.min(a.y, b.y), math.min(c.y, d.y)));
            float maxY = math.min(height - 1, math.max(math.max(a.y, b.y), math.max(c.y, d.y)));

            for (int row = (int)math.floor(minY); row <= (int)math.ceil(maxY); row++)
            {
                if ((row < 0) || (row >= height)) continue;
                // ToCell puts a texel's CENTRE at integer coordinates (TerrainUtils
                // .ToHeightmapSpace), which the column span's round() already assumes; sampling
                // the scanline at `row + 0.5` was a half-cell southward bias on this one axis.
                //
                float y = row;

                int count = 0;
                for (int e = 0; e < 4; e++)
                {
                    float2 p = poly[e];
                    float2 q = poly[(e + 1) & 3];
                    if ((p.y <= y) == (q.y <= y)) continue;
                    crossings[count++] = p.x + ((y - p.y) / (q.y - p.y) * (q.x - p.x));
                }

                if (count < 2) continue;

                // At most four crossings; a tiny insertion sort keeps the spans paired.
                //
                for (int i = 1; i < count; i++)
                {
                    float value = crossings[i];
                    int j = i - 1;
                    while ((j >= 0) && (crossings[j] > value)) { crossings[j + 1] = crossings[j]; j--; }
                    crossings[j + 1] = value;
                }

                for (int pair = 0; (pair + 1) < count; pair += 2)
                {
                    int from = math.max(0, (int)math.round(crossings[pair]));
                    int to = math.min(width - 1, (int)math.round(crossings[pair + 1]));
                    for (int x = from; x <= to; x++)
                    {
                        int cellIndex = (row * width) + x;
                        if (context.Cut)
                        {
                            // The clip band test: only terrain standing over the lane, within
                            // the composition's band, is cut away.
                            float terrainY = (heightData.heights[cellIndex] / heightData.scale.y) - heightData.offset.y;
                            float overLane = terrainY - laneY;
                            if ((overLane <= context.CutBottom) || (overLane >= context.CutTop)) continue;
                        }
                        context.Mask[cellIndex] = context.Value;
                    }
                }
            }
        }
    }
}
