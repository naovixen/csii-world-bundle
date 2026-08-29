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

        /// <summary>The query for the player's named districts (§8d).</summary>
        EntityQuery _districtQuery;

        // Stamp tallies for the report.
        int _edgesStamped, _nodesStamped, _orphansStamped, _skippedForm, _skippedNoFlatten, _skippedWaterway;
        int _cutEdges, _cutNodes, _cutOrphans, _cutAreas, _cutRoundaboutTransitions;

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

            // The player's own districts. A district IS an area — the same Node/Triangle grid
            // buffers as a surface — but it paints nothing and is never rendered, so it has never
            // reached `areas.jsonl` (whose query demands `Game.Areas.Surface`). What it carries
            // that nothing else in the bundle does is a NAME the player typed, which is the only
            // place-name a CS2 city contains (contract §8d).
            //
            _districtQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Areas.District>(),
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
        /// The event triggered when the system instance is destroyed. A fine capture in flight
        /// gives the camera back and drops its raster.
        /// </summary>
        protected override void OnDestroy()
        {
            FineRun run = _fine;
            if (run != null) Finish(run, "Fine terrain: the world was unloaded mid-capture; layer omitted", warn: true);
            ReleaseFine();
            base.OnDestroy();
        }

        /// <summary>
        /// Advance a fine cascade capture in flight (<c>TerrainRipSystem.FineCascade.cs</c>);
        /// nothing else here runs per frame.
        /// </summary>
        protected override void OnUpdate()
        {
            FineRun run = _fine;
            if (run != null) AdvanceFine(run);
        }

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

            TerrainHeightData heightData = Instance.Terrain.GetHeightData(waitForPending: true);
            TerrainCapture capture = CaptureHeights(heightData, report);
            if (capture == null)
            {
                return "Terrain rip failed: no height data is resident (is a city loaded?).";
            }

            if (ground) CaptureRoadbed(capture, heightData, report);
            _cutEdges = _cutNodes = _cutOrphans = _cutAreas = _cutRoundaboutTransitions = 0;
            if (clips) CollectClipVolumes(heightData, report);
            if (water) CaptureWater(capture, heightData, report);
            if (ground) CaptureGround(capture, terrainDirectory, report);
            if (heights) CaptureBackdrop(capture, report);
            if (heights) CaptureFine(capture, report);
            RipAreaLayers(outputDirectory, terrainDirectory, areas, report);

            SortedDictionary<string, object> rasters = BuildRasters(capture, terrainDirectory, heights);
            WriteTerrain(capture, rasters, outputDirectory, terrainDirectory, heights, ground, clips, report);
            if (heights) DeclareFineGap(capture, outputDirectory, report);
            ReleaseFine();

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





        // ------------------------------------------------------------------------------------
        // The roadbed stamp — the game's own flattening footprint, rasterised.
        //
        // ------------------------------------------------------------------------------------



    }
}
