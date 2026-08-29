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
    /// One material range of one piece: the surviving vertices, their shading frame, and the
    /// triangles that reference them.
    /// </summary>
    /// <remarks>
    /// Every rule here is the game's generator, ported: the vertex filter and remap
    /// (<c>nativeArray4</c>), the per-range V density measured only over surviving triangles,
    /// the frame rotated by the very map that placed the vertex, and the reversed index read
    /// under Invert XOR FlipMesh. The one declared APPROXIMATION is on the frame — see
    /// <c>ShadeVertex</c>.
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// One material range of one piece: the surviving vertices, their triangles, and the
        /// primitive they become.
        /// </summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="range">The range's window into the piece's arrays.</param>
        /// <param name="r">The range index, for its material id.</param>
        /// <param name="p">The composition's curves.</param>
        /// <param name="center">The mesh origin to rebase onto.</param>
        /// <param name="assembly">The shared assembly lists.</param>
        private void BuildRange(CompositionSetup setup, PieceSetup ps, in PieceRange range, int r,
                                in CompositionCurves p, float3 center, MeshAssembly assembly)
        {
            int baseVertex = assembly.Vertices.Count;
            int[] remap = BuildRemap(ps, range, baseVertex, out int kept);
            MeasureDensity(ps, range, remap, out float density, out float rebase, out bool tiledV);

            EmitVertices(setup, ps, range, remap, density, rebase, tiledV, p, center, assembly);

            int primitiveStart = assembly.Indices.Count;
            EmitTriangles(ps, range, remap, assembly);

            if (assembly.Indices.Count > primitiveStart)
            {
                assembly.Primitives.Add(new MeshPrimitive
                {
                    StartIndex = primitiveStart,
                    IndexCount = assembly.Indices.Count - primitiveStart,
                    StartVertex = baseVertex,
                    VertexCount = kept,
                    MaterialIndex = assembly.Primitives.Count,
                    MaterialId = ps.Source.RangeMaterial[r]
                });
            }
        }

        /// <summary>
        /// The vertex filter and remap, as the generator's <c>nativeArray4</c> — built before the
        /// V density because the game measures that density only over triangles that survive the
        /// cut (its accumulation sits inside <c>math.all(int7 &gt;= 0)</c>).
        /// </summary>
        /// <param name="ps">The piece.</param>
        /// <param name="range">The range window.</param>
        /// <param name="baseVertex">Where this range's survivors start in the assembly.</param>
        /// <param name="kept">How many survived.</param>
        /// <returns>Local vertex index to assembly index, or -1 for a cut vertex.</returns>
        private static int[] BuildRemap(PieceSetup ps, in PieceRange range, int baseVertex, out int kept)
        {
            int[] remap = new int[range.VertexCount];
            kept = 0;
            for (int i = 0; i < range.VertexCount; i++)
            {
                float rawZ = ps.Source.Vertices[range.StartVertex + i].z;
                remap[i] = ((ps.FrontCut && (rawZ < -0.01f)) || (ps.BackCut && (rawZ > 0.01f)))
                    ? -1
                    : baseVertex + kept++;
            }
            return remap;
        }

        /// <summary>
        /// The game's per-range V density: |ΔV| over |Δz| across surviving triangles whose V is
        /// wholly non-negative; the others instead record the rebase ceiling. A front-cut piece's
        /// removed half must not weigh the ratio.
        /// </summary>
        /// <param name="ps">The piece.</param>
        /// <param name="range">The range window.</param>
        /// <param name="remap">The vertex filter.</param>
        /// <param name="density">V per metre along.</param>
        /// <param name="rebase">The ceiling a non-tiled range subtracts instead.</param>
        /// <param name="tiledV">Whether this range tiles V at all.</param>
        private static void MeasureDensity(PieceSetup ps, in PieceRange range, int[] remap,
                                           out float density, out float rebase, out bool tiledV)
        {
            PieceGeometry source = ps.Source;
            tiledV = !ps.DisableTiling;
            float2 accum = default;
            rebase = 0f;
            for (int t = 0; t < range.IndexCount; t += 3)
            {
                int ia = source.Indices[range.StartIndex + t + ps.Order.x];
                int ib = source.Indices[range.StartIndex + t + ps.Order.y];
                int ic = source.Indices[range.StartIndex + t + ps.Order.z];
                int la = ia - range.StartVertex, lb = ib - range.StartVertex, lc = ic - range.StartVertex;
                if ((la < 0) || (lb < 0) || (lc < 0)
                    || (la >= range.VertexCount) || (lb >= range.VertexCount) || (lc >= range.VertexCount)) continue;
                if ((remap[la] < 0) || (remap[lb] < 0) || (remap[lc] < 0)) continue;

                float3 v = new(source.Uvs[ia].y, source.Uvs[ib].y, source.Uvs[ic].y);
                if (tiledV && math.all(v >= 0f))
                {
                    float3 z = new(source.Vertices[ia].z, source.Vertices[ib].z, source.Vertices[ic].z);
                    accum += new float2(math.csum(math.abs(v.yzx - v)), math.csum(math.abs(z.yzx - z)));
                }
                else
                {
                    rebase = math.max(rebase, math.cmax(v));
                }
            }
            density = (accum.y > 1e-6f) ? (accum.x / accum.y) : 0f;
            rebase = tiledV ? 0f : math.ceil(rebase);
        }

        /// <summary>
        /// Every surviving vertex of one range: classified, mapped onto the composition's curves,
        /// conformed, shaded and emitted.
        /// </summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="range">The range window.</param>
        /// <param name="remap">The vertex filter.</param>
        /// <param name="density">V per metre along.</param>
        /// <param name="rebase">The ceiling a non-tiled range subtracts.</param>
        /// <param name="tiledV">Whether this range tiles V.</param>
        /// <param name="p">The composition's curves.</param>
        /// <param name="center">The mesh origin to rebase onto.</param>
        /// <param name="assembly">The shared assembly lists.</param>
        private void EmitVertices(CompositionSetup setup, PieceSetup ps, in PieceRange range, int[] remap,
                                  float density, float rebase, bool tiledV,
                                  in CompositionCurves p, float3 center, MeshAssembly assembly)
        {
            PieceGeometry source = ps.Source;
            for (int i = 0; i < range.VertexCount; i++)
            {
                if (remap[i] < 0) continue;

                float3 raw = source.Vertices[range.StartVertex + i];
                float3 normal = ps.Authored ? ps.Normals[range.StartVertex + i] : default;
                float4 tangent = (ps.Tangents != null)
                    ? ps.Tangents[range.StartVertex + i] : new float4(0f, 0f, 0f, 1f);

                int conform = ClassifyConform(setup, ps, raw, ref normal);

                float3 vertex = raw * new float3(ps.Sx, 1f, ps.Sz) + ps.Offset;
                normal *= new float3(ps.Sx, 1f, ps.Sz);
                tangent *= new float4(ps.Sx, 1f, ps.Sz, ps.Sw);
                float2 uv = source.Uvs[range.StartVertex + i];

                VertexMap map = MapVertex(setup, ps, ref vertex);
                map.XParam = math.saturate(map.XParam);
                map.ZParam = math.saturate(map.ZParam);

                // The across-width sync the game hands the shader (§5.1). Each half's
                // knots are stored in that half's own xParam coordinate (left: 0 at the
                // outer edge; right: 0 at the middle — `GeometrySystem`'s
                // `1 − offsetsLeft.wzyx` twin conversion and `CalculateVertexSyncTarget`'s
                // right-branch anchoring both say so), so both halves apply directly.
                // The earlier `1 − Apply(1 − xParam)` right-half flip mirrored every
                // right-side curb knot — kerbs jutted into the roadway at flares (§21).
                //
                if (map.Halved && setup.IsNode)
                {
                    map.XParam = p.Sync.Apply(map.XParam, map.LeftHalf);
                }

                float3 a = MathUtils.Position(p.Curves[map.Pair.x], map.ZParam);
                float3 b = MathUtils.Position(p.Curves[map.Pair.y], map.ZParam);
                float3 world = math.lerp(a, b, map.XParam);
                world.y += vertex.y;
                if (conform != 0) ApplyConform(ps, conform, ref world);

                RecordSectionExtent(ps.Section, world, center, map.XParam);
                assembly.Vertices.Add(world - center);
                ShadeVertex(ps, map, a, b, normal, tangent, p, assembly);

                float metres = math.lerp(EvalMap(p.Maps[map.Pair.x], map.ZParam),
                                         EvalMap(p.Maps[map.Pair.y], map.ZParam), map.XParam);
                float v2 = (tiledV && (uv.y >= 0f)) ? (density * metres) : (uv.y - rebase);
                assembly.Uvs.Add(new float2(uv.x, v2));
            }
        }

        /// <summary>
        /// Which terrain-conform class this vertex belongs to, and the SmoothTopNormal
        /// replacement that shares the same bound.
        /// </summary>
        /// <remarks>
        /// The game classifies on the AUTHORED vertex, before the flip and offset.
        /// SmoothTopNormal discards the authored normal of the top row for straight up, so a
        /// kerb's or a wall's cap shades with the surface beside it instead of keeping the crease
        /// the tile was authored with — same bound and same epsilon as the RaiseTop test, but a
        /// separate flag and no terrain involved.
        /// </remarks>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="raw">The authored vertex.</param>
        /// <param name="normal">The authored normal, replaced where SmoothTopNormal applies.</param>
        /// <returns>0 for no conform, 1 for lower-bottom, 2 for raise-top.</returns>
        private int ClassifyConform(CompositionSetup setup, PieceSetup ps, float3 raw, ref float3 normal)
        {
            if (ps.HeightBounds == null) return 0;

            int conform = 0;
            int slice = math.clamp((int)math.round(raw.z * ps.BoundsScaleZ) + 128, 0, 256);
            if (ps.Conforming && ps.LowerBottom && (raw.y <= ps.HeightBounds.Min[slice] + 0.01f)) conform = 1;
            if (ps.Conforming && ps.RaiseTop && !setup.ElevatedAwayFromGround
                && (raw.y >= ps.HeightBounds.Max[slice] - 0.01f)) conform = 2;

            if (ps.SmoothTop && ps.Authored && (raw.y >= ps.HeightBounds.Max[slice] - 0.01f))
            {
                normal = new float3(0f, 1f, 0f);
                _smoothTopVerts++;
            }

            if (ps.Probe != null)
            {
                ps.Probe.Vertices++;
                if (conform == 1) ps.Probe.Class1++;
                else if (conform == 2) ps.Probe.Class2++;
            }
            return conform;
        }


        /// <summary>
        /// Triangles: the game's reversed read under Invert XOR FlipMesh, then only those whose
        /// three corners survived the cut.
        /// </summary>
        /// <param name="ps">The piece.</param>
        /// <param name="range">The range window.</param>
        /// <param name="remap">The vertex filter.</param>
        /// <param name="assembly">The shared assembly lists.</param>
        private void EmitTriangles(PieceSetup ps, in PieceRange range, int[] remap, MeshAssembly assembly)
        {
            PieceGeometry source = ps.Source;
            for (int t = 0; t < range.IndexCount; t += 3)
            {
                int ia = source.Indices[range.StartIndex + t + ps.Order.x] - range.StartVertex;
                int ib = source.Indices[range.StartIndex + t + ps.Order.y] - range.StartVertex;
                int ic = source.Indices[range.StartIndex + t + ps.Order.z] - range.StartVertex;
                if ((ia < 0) || (ib < 0) || (ic < 0)
                    || (ia >= range.VertexCount) || (ib >= range.VertexCount) || (ic >= range.VertexCount))
                {
                    // Data-contract guard: every scanned `.Geometry` asset keeps its
                    // ranges self-contained (§38: 0 of 10,414 violate), so a triangle
                    // reaching outside its window is corrupt input, counted so it
                    // surfaces in the report instead of vanishing.
                    //
                    _conformOutOfWindowTris++;
                    if (ps.Probe != null) ps.Probe.OutOfWindowTris++;
                    continue;
                }
                if ((remap[ia] < 0) || (remap[ib] < 0) || (remap[ic] < 0))
                {
                    if (ps.Probe != null)
                    {
                        int culprit = (remap[ia] < 0) ? ia : ((remap[ib] < 0) ? ib : ic);
                        if (ps.FrontCut && (source.Vertices[range.StartVertex + culprit].z < -0.01f)) ps.Probe.FrontCutTris++;
                        else ps.Probe.BackCutTris++;
                    }
                    continue;
                }

                assembly.Indices.Add(remap[ia]);
                assembly.Indices.Add(remap[ib]);
                assembly.Indices.Add(remap[ic]);
            }
        }
    }
}
