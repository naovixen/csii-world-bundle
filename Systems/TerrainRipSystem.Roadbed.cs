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
    /// The roadbed mask, stamped from the same net segments the game flattens the ground with.
    /// </summary>
    /// <remarks>
    /// The terrain material has no roadbed texture and no roadbed mask — its grounds are grass,
    /// dirt, rock and four paintable extras, classified per-pixel on the GPU. What makes
    /// flattened ground read as roadbed in-game is the flattening itself (plus the ~13%-alpha
    /// lane decals). The truthful mask is therefore the same footprint the road job draws:
    /// every edge segment, junction-node strip and orphan cap of a Flatten/ClipTerrain net,
    /// rasterised on the CPU from the same <c>Segment</c> béziers
    /// (<c>CullRoadsJob.AddEdge/AddNode/AddOrphans</c>, decompiled).
    /// </remarks>
    public partial class TerrainRipSystem
    {
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
                                     EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain,
                                     composition.m_StartNode, netGeometry))
                    {
                        _nodesStamped++;
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_EndNode, netGeometry, out TerrainComposition endTerrain)
                        && StampNode(in context, in context, heightData,
                                     EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain,
                                     composition.m_EndNode, netGeometry))
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

            return TryGetClipBand(data, terrain, netGeometry, isStart, out cutBottom, out cutTop);
        }

        /// <summary>
        /// The band for a composition already in hand — the half of
        /// <see cref="TryGetClipBand(Entity, NetGeometryData, bool, out TerrainComposition, out float, out float)"/>
        /// that does the arithmetic. Split out because <c>AddNode</c> feeds the roundabout's
        /// second strip pair a composition it has MUTATED (see <see cref="StampNode"/>), which
        /// is not the one stored on any entity.
        /// </summary>
        private bool TryGetClipBand(NetCompositionData data, TerrainComposition terrain,
                                    NetGeometryData netGeometry, bool isStart,
                                    out float cutBottom, out float cutTop)
        {
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
    }
}
