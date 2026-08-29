using Carto.Domain;
using Colossal.Mathematics;
using Game.Net;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The eight curves a composition is placed against, per geometry kind, and the bézier maths
    /// that reads them.
    /// </summary>
    /// <remarks>
    /// An edge, a node side and an orphan cap each present the same eight-curve frame to the
    /// generator, built from different geometry. Getting which curve is which wrong does not
    /// crash — it silently places the mesh somewhere plausible — so each builder names its
    /// matrices in the game's own order.
    /// </remarks>
    public partial class NetworkRipSystem
    {
        private static void MappingOffsets(Segment segment, out float4 left, out float4 right,
                                           float2 leftMap, float2 rightMap)
        {
            float2 a = new(math.distance(segment.m_Left.a, segment.m_Left.b),
                           math.distance(segment.m_Left.c, segment.m_Left.d));
            float2 b = new(math.distance(segment.m_Right.a, segment.m_Right.b),
                           math.distance(segment.m_Right.c, segment.m_Right.d));
            a *= (leftMap.y - leftMap.x) / math.max(1f, segment.m_Length.x);
            b *= (rightMap.y - rightMap.x) / math.max(1f, segment.m_Length.y);
            left = new float4(leftMap.x, leftMap.x + a.x, leftMap.y - a.y, leftMap.y);
            right = new float4(rightMap.x, rightMap.x + b.x, rightMap.y - b.y, rightMap.y);
        }

        private static Bezier4x3 StraightCurve(float3 start, float3 direction, float length)
        {
            direction = MathUtils.Normalize(direction, direction.xz);
            direction.y = math.clamp(direction.y, -1f, 1f);
            return new Bezier4x3(start,
                                 start + (direction * (length * (1f / 3f))),
                                 start + (direction * (length * (2f / 3f))),
                                 start + (direction * length));
        }

        /// <summary>`CalculateEdgeParameters`, curves kept absolute.</summary>
        private static CompositionCurves EdgeCurves(EdgeGeometry geometry, bool isRotated)
        {
            float2 total = geometry.m_Start.m_Length + geometry.m_End.m_Length;
            MappingOffsets(geometry.m_Start, out float4 leftA, out float4 rightA,
                           new float2(0f, geometry.m_Start.m_Length.x), new float2(0f, geometry.m_Start.m_Length.y));
            MappingOffsets(geometry.m_End, out float4 leftB, out float4 rightB,
                           new float2(geometry.m_Start.m_Length.x, total.x), new float2(geometry.m_Start.m_Length.y, total.y));

            CompositionCurves result = new()
            {
                Curves = new Bezier4x3[8],
                Maps = new float4[8],
                Sync = VertexSync.None
            };

            if (isRotated)
            {
                result.Curves[0] = MathUtils.Invert(geometry.m_End.m_Right); result.Maps[0] = rightB.wzyx;
                result.Curves[1] = MathUtils.Invert(geometry.m_Start.m_Right); result.Maps[1] = rightA.wzyx;
                result.Curves[2] = MathUtils.Invert(geometry.m_End.m_Left); result.Maps[2] = leftB.wzyx;
                result.Curves[3] = MathUtils.Invert(geometry.m_Start.m_Left); result.Maps[3] = leftA.wzyx;
            }
            else
            {
                result.Curves[0] = geometry.m_Start.m_Left; result.Maps[0] = leftA;
                result.Curves[1] = geometry.m_End.m_Left; result.Maps[1] = leftB;
                result.Curves[2] = geometry.m_Start.m_Right; result.Maps[2] = rightA;
                result.Curves[3] = geometry.m_End.m_Right; result.Maps[3] = rightB;
            }

            return result;
        }

        /// <summary>`CalculateNodeParameters`, both branches.</summary>
        private static CompositionCurves NodeCurves(EdgeNodeGeometry geometry, NetCompositionData data)
        {
            float4 corner = new float4(0f, 1f / 3f, 2f / 3f, 1f) * data.m_Width;
            CompositionCurves result = new()
            {
                Curves = new Bezier4x3[8],
                Maps = new float4[8],
                Sync = new VertexSync(data, geometry)
            };

            if (geometry.m_MiddleRadius > 0f)
            {
                float2 total = geometry.m_Left.m_Length + geometry.m_Right.m_Length;
                MappingOffsets(geometry.m_Left, out float4 leftA, out float4 rightA,
                               new float2(0f, geometry.m_Left.m_Length.x), new float2(0f, geometry.m_Left.m_Length.y));
                MappingOffsets(geometry.m_Right, out float4 leftB, out float4 rightB,
                               new float2(geometry.m_Left.m_Length.x, total.x), new float2(geometry.m_Left.m_Length.y, total.y));
                float3 tangentL = MathUtils.StartTangent(geometry.m_Left.m_Left);
                float3 tangentR = MathUtils.StartTangent(geometry.m_Left.m_Right);
                MathUtils.Divide(geometry.m_Middle, out Bezier4x3 middleA, out Bezier4x3 middleB, 0.99f);

                result.Curves[0] = geometry.m_Left.m_Left; result.Maps[0] = leftA;
                result.Curves[1] = geometry.m_Right.m_Left; result.Maps[1] = leftB;
                result.Curves[2] = geometry.m_Left.m_Right; result.Maps[2] = rightA;
                result.Curves[3] = geometry.m_Right.m_Right; result.Maps[3] = rightB;
                result.Curves[4] = middleA; result.Maps[4] = math.lerp(leftA, rightA, 0.5f);
                result.Curves[5] = middleB; result.Maps[5] = math.lerp(leftB, rightB, 0.5f);
                result.Curves[6] = StraightCurve(geometry.m_Left.m_Left.a, tangentL, data.m_Width); result.Maps[6] = corner;
                result.Curves[7] = StraightCurve(geometry.m_Left.m_Right.a, tangentR, data.m_Width); result.Maps[7] = corner;
            }
            else
            {
                MappingOffsets(geometry.m_Left, out float4 leftA, out float4 rightA,
                               new float2(0f, geometry.m_Left.m_Length.x), new float2(0f, geometry.m_Left.m_Length.y));
                MappingOffsets(geometry.m_Right, out float4 leftB, out float4 rightB,
                               new float2(0f, geometry.m_Right.m_Length.x), new float2(0f, geometry.m_Right.m_Length.y));
                float3 tangentL = MathUtils.StartTangent(geometry.m_Left.m_Left);
                float3 tangentR = MathUtils.StartTangent(geometry.m_Right.m_Right);

                result.Curves[0] = geometry.m_Left.m_Left; result.Maps[0] = leftA;
                result.Curves[1] = geometry.m_Right.m_Left; result.Maps[1] = leftB;
                result.Curves[2] = geometry.m_Left.m_Right; result.Maps[2] = rightA;
                result.Curves[3] = geometry.m_Right.m_Right; result.Maps[3] = rightB;
                result.Curves[4] = geometry.m_Middle; result.Maps[4] = math.lerp(leftB, rightA, 0.5f);
                result.Curves[5] = geometry.m_Middle; result.Maps[5] = result.Maps[4];
                result.Curves[6] = StraightCurve(geometry.m_Left.m_Left.a, tangentL, data.m_Width); result.Maps[6] = corner;
                result.Curves[7] = StraightCurve(geometry.m_Right.m_Right.a, tangentR, data.m_Width); result.Maps[7] = corner;
            }

            return result;
        }

        /// <summary>`CalculateOrphanParameters`.</summary>
        private static CompositionCurves OrphanCurves(float3 position, NetCompositionData data, bool isPrimary)
        {
            float side = isPrimary ? -1f : 1f;
            float half = data.m_Width * 0.5f;
            float control = data.m_Width * 0.2761424f;

            Segment segment = default;
            segment.m_Left = new Bezier4x3(
                new float3(position.x + (side * half), position.y, position.z),
                new float3(position.x + (side * half), position.y, position.z - (side * control)),
                new float3(position.x + (side * control), position.y, position.z - (side * half)),
                new float3(position.x, position.y, position.z - (side * half)));
            segment.m_Right = new Bezier4x3(position, position, position, position);
            segment.m_Length = new float2(data.m_Width * (math.PI / 2f), 0f);

            Bezier4x3 far = segment.m_Left;
            far.a.x -= side * data.m_Width;
            far.b.x -= side * data.m_Width;
            far.c.x = (position.x * 2f) - far.c.x;
            float3 direction = new(0f, 0f, -side);

            MappingOffsets(segment, out float4 left, out float4 right,
                           new float2(0f, segment.m_Length.x), new float2(0f, 0f));
            float4 corner = new float4(0f, 1f / 3f, 2f / 3f, 1f) * data.m_Width;

            CompositionCurves result = new()
            {
                Curves = new Bezier4x3[8],
                Maps = new float4[8],
                Sync = VertexSync.None
            };
            result.Curves[0] = segment.m_Left; result.Maps[0] = left;
            result.Curves[1] = segment.m_Right; result.Maps[1] = right;
            result.Curves[2] = segment.m_Right; result.Maps[2] = right;
            result.Curves[3] = far; result.Maps[3] = left;
            result.Curves[4] = segment.m_Right; result.Maps[4] = right;
            result.Curves[5] = segment.m_Right; result.Maps[5] = right;
            result.Curves[6] = StraightCurve(segment.m_Left.a, direction, data.m_Width); result.Maps[6] = corner;
            result.Curves[7] = StraightCurve(far.a, direction, data.m_Width); result.Maps[7] = corner;
            return result;
        }

        // ---- Evaluation ---------------------------------------------------------------------

        /// <summary>The Bernstein blend of one arc table — the `w` column of `BuildEdgeMatrix`.</summary>
        private static float EvalMap(float4 map, float t)
        {
            float u = 1f - t;
            return (u * u * u * map.x) + (3f * u * u * t * map.y)
                 + (3f * u * t * t * map.z) + (t * t * t * map.w);
        }

        /// <summary>
        /// The derivative of a cubic bezier — the quadratic through its control-point differences.
        /// The along-curve column of the shading frame: how far the evaluated point moves per unit
        /// of the curve parameter, which is the same quantity the position blend is built from.
        /// </summary>
        /// <param name="curve">The curve.</param>
        /// <param name="t">Where along it.</param>
        /// <returns>The derivative, in metres per unit parameter.</returns>
        private static float3 Derivative(Bezier4x3 curve, float t)
        {
            float u = 1f - t;
            return 3f * ((u * u * (curve.b - curve.a))
                       + (2f * u * t * (curve.c - curve.b))
                       + (t * t * (curve.d - curve.c)));
        }

        /// <summary>
        /// Normalize, or fall back where the vector has no length to normalize.
        /// A degenerate frame column is a real state — a collapsed flare, a pinned column — and a
        /// NaN there would spread through the whole assembled mesh's shading.
        /// </summary>
        /// <param name="value">The vector.</param>
        /// <param name="fallback">What to return when it is too short to normalize.</param>
        /// <returns>The unit vector.</returns>
        private static float3 NormalizeOr(float3 value, float3 fallback)
        {
            float length = math.length(value);
            return (length > 1e-9f) ? (value / length) : fallback;
        }
    }
}
