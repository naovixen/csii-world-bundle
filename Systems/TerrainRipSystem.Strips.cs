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
    /// The CPU rasteriser under both stamp walks: one strip of one bézier at a time, and the
    /// clip-volume record a strip becomes.
    /// </summary>
    /// <remarks>
    /// Self-contained on the height grid and the béziers — no terrain writer, no ECS query.
    /// Both <c>StampRoadbed</c> and <c>StampCutout</c> drive it; what differs between them is
    /// which compositions they walk and whether they fill a mask or collect a volume.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Stamp one node's strips — the flattened junction interior.
        /// The three cases are <c>CullRoadsJob.AddNode</c>'s: a roundabout rides its middle curve
        /// ring, a true junction (parted corners) covers both halves plus the two wedges to the
        /// middle curve — the petals — and a straight-through node spans half-to-half across the
        /// middle.
        /// </summary>
        /// <returns>Whether the node cleared the draw gate and was stamped.</returns>
        /// <remarks>
        /// The roundabout's second strip pair is the one place the game evaluates a section with
        /// <c>isStart: false</c> (its <c>AddNode</c> passes it for both <c>node.m_Right</c>
        /// segments), so those two strips take a band built from the composition's end-half edge
        /// heights. Every other strip, and both halves of every other node kind, is
        /// <c>isStart: true</c> and takes <paramref name="context"/>. That pair is also the one
        /// place the game feeds <c>AddSegment</c> a composition it has MUTATED — see
        /// <see cref="RoundaboutSecondPair"/>.
        /// </remarks>
        private bool StampNode(in StampContext context, in StampContext contextEnd,
                               TerrainHeightData heightData,
                               EdgeNodeGeometry node, TerrainComposition terrain,
                               Entity composition, NetGeometryData netGeometry)
        {
            // The game's own draw gate: nodes shorter than 5 cm on both sides draw nothing.
            //
            if (!(math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))) return false;

            Segment strip;
            if (node.m_MiddleRadius > 0f)
            {
                // Roundabout: outer half to the middle curve, middle curve to the inner half,
                // then the same for the right half against the middle curve's end point.
                //
                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Left.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);

                // The second pair rides a composition the game has shed the elevated/tunnel
                // transition state from, and a ring trimmed toward the middle by the sync
                // offsets. Both are AddNode's, and both matter: the mutated flags move the band
                // onto a different branch of AddSegment, and the trims shorten the stamped
                // footprint at an elevated or tunnel ramp.
                //
                StampContext pairContext = contextEnd;
                float trimLeft = 0f;
                float trimRight = 0f;
                if (RoundaboutSecondPair(composition, netGeometry, node,
                                         out NetCompositionData mutated, out trimLeft, out trimRight))
                {
                    if (context.Cut) _cutRoundaboutTransitions++;
                    if (TryGetClipBand(mutated, terrain, netGeometry, false,
                                       out float mutatedBottom, out float mutatedTop))
                    {
                        pairContext.CutBottom = mutatedBottom;
                        pairContext.CutTop = mutatedTop;
                    }
                }

                strip = node.m_Right;
                strip.m_Right = new Bezier4x3(node.m_Middle.d, node.m_Middle.d, node.m_Middle.d, node.m_Middle.d);
                if (trimLeft != 0f)
                {
                    MoveTowards(ref strip.m_Left.a, node.m_Middle.d, trimLeft);
                    MoveTowards(ref strip.m_Left.b, node.m_Middle.d, trimLeft);
                    MoveTowards(ref strip.m_Left.c, node.m_Middle.d, trimLeft);
                    MoveTowards(ref strip.m_Left.d, node.m_Middle.d, trimLeft);
                }
                StampSegment(in pairContext, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Right.m_Right;
                if (trimRight != 0f)
                {
                    MoveTowards(ref strip.m_Right.a, node.m_Middle.d, trimRight);
                    MoveTowards(ref strip.m_Right.b, node.m_Middle.d, trimRight);
                    MoveTowards(ref strip.m_Right.c, node.m_Middle.d, trimRight);
                    MoveTowards(ref strip.m_Right.d, node.m_Middle.d, trimRight);
                }
                StampSegment(in pairContext, heightData, strip, terrain, false, true);
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
