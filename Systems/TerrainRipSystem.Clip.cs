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
    /// The §8c clip volumes: what the game hides rather than carves.
    /// </summary>
    /// <remarks>
    /// The game never carves tunnel portals or sunken trenches out of the heightmap. A Lowered
    /// composition's ground is merely clamped into <c>[lane, lane + ElevationLimit·3]</c> and a
    /// shallow tube's cover is shifted <i>up</i> — the covering terrain stays in the height
    /// cascade, and every frame the <c>RoadClip</c> stencil pass discards its pixels on screen
    /// instead. A consumer that rebuilds the ground from the heightmap alone therefore walls off
    /// every tunnel mouth; this walk collects the volumes it must delete inside.
    /// </remarks>
    public partial class TerrainRipSystem
    {
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
                                      EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain,
                                      composition.m_StartNode, netGeometry))
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
                                      EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain,
                                      composition.m_EndNode, netGeometry))
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
        /// Move a point toward another by <paramref name="amount"/> metres, measured in XZ —
        /// <c>CullRoadsJob.MoveTowards</c> verbatim. The normalisation is by the XZ length, so a
        /// climbing ring is trimmed by ground distance, not slope distance.
        /// </summary>
        private static void MoveTowards(ref float3 position, float3 other, float amount)
        {
            float3 delta = other - position;
            delta = Colossal.Mathematics.MathUtils.Normalize(delta, delta.xz);
            position += delta * amount;
        }

    }
}
