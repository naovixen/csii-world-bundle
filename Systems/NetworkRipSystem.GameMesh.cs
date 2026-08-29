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
    /// The game's own composition-mesh generation, ported and evaluated on the CPU.
    /// <b>Why a port and not a reconstruction.</b> Every attempt to re-derive the node mapping
    /// piece-by-piece — half frames, wedge fills, fans — converged on the game's behaviour
    /// asymptotically and never reached it: junction interiors stayed open, and their shading
    /// never matched the arms. The game does something structurally different:
    /// <c>GenerateCompositionMesh</c> bakes every vertex into <i>parameter space</i> — x across
    /// [0,1], z along [0,1], a matrix pair in the vertex colour — and the vertex shader
    /// evaluates world positions by blending two cubic bézier curves handed over as
    /// <c>m_CompositionMatrix0..7</c>, whose columns are the curve's control points with a
    /// Bernstein-weighted arc table in <c>w</c> (<c>BuildEdgeMatrix</c>). This file is that
    /// pipeline: <c>CalculateEdgeParameters</c> / <c>CalculateNodeParameters</c> /
    /// <c>CalculateOrphanParameters</c> ported for the curves, the generator's vertex path
    /// ported for the mapping, and the shader's evaluation done here — position from the
    /// Bernstein blend, V from the same blend of the arc tables times the piece's measured
    /// V-per-metre density, exactly the quantity the game stores per vertex.
    /// Known deviations, deliberate and counted where they can fire: the roundabout three-matrix
    /// blend weights (0x80 colour bits — shader-side, still approximated by the nearer pair), the
    /// <i>orthonormalization choice</i> in the shading frame below, and the two per-vertex shader
    /// inputs a baked-geometry producer has no use for (<c>UV0.z</c>/<c>UV0.w</c>, noted at the
    /// branch that would compute them).
    /// <b>Wind sway is NOT one of them</b> (re-derived from source 2026-08-17, audit §53): a
    /// composition batch is handed <i>no</i> wind input at all — <c>NetProperty</c> is eight
    /// composition matrices, four syncs, an infoview colour, an outline colour and a LOD fade, and
    /// nothing else. What sways is per-<i>material</i> (<c>_WindRangeLvlB</c>/
    /// <c>_WindElasticityLvlB</c>, driven by the global wind field), which is animation over a
    /// static bundle rather than a network deviation. The one wind term that does move net-domain
    /// geometry is <c>LaneProperty.HangingDistances</c>, and it lives in the lane path.
    /// The terrain-conforming flags (<c>LowerBottomToTerrain</c>/<c>RaiseTopToTerrain</c>) are no
    /// longer a deviation: they are applied at rip time (§34 raise, §38 clamp — see the
    /// conforming section below). Neither are normals (audit #40 P6): the piece's authored
    /// <c>MeshNormal</c>/<c>MeshTangent</c> buffers are read, flipped exactly as
    /// <c>BatchMeshHelpers</c> flips them (<c>n *= float3.xyz</c>, <c>t *= float3</c>, the
    /// <c>w</c> term flipping tangent handedness), overridden by <c>SmoothTopNormal</c> — a
    /// distinct piece flag, <c>0x800</c>: the authored normal of every vertex at the piece's top
    /// height bound (<c>y &gt;= max − 0.01</c>) is replaced by <c>(0,1,0)</c> before the flip, so
    /// a kerb's or wall's top row shades flat with the surface beside it — and then rotated into
    /// the bend by the Jacobian of the very map that placed the vertex. Only pieces that carry no
    /// normal buffer at all fall back to the face rebuild, and both are counted.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// The eight curves and arc tables one composition instance is evaluated against — the
        /// CPU form of <c>CompositionParameters</c>' matrices.
        /// </summary>
        private struct CompositionCurves
        {
            public Bezier4x3[] Curves;   // matrices 0..7, absolute world coordinates
            public float4[] Maps;        // their mapping-offset tables (metres along, Bernstein-blended)
            public VertexSync Sync;
        }

        /// <summary>
        /// One piece's authored geometry, read once from the ECS buffers the game itself
        /// generates from (<c>MeshVertex</c>/<c>MeshUV0</c>/<c>MeshIndex</c>/<c>MeshMaterial</c>),
        /// with each material range's bundle id resolved through the same reader every building
        /// goes through so surfaces land in the material table.
        /// </summary>
        private class PieceGeometry
        {
            public float3[] Vertices;
            public float2[] Uvs;
            public int[] Indices;

            /// <summary>
            /// The authored shading frame (<c>MeshNormal</c>/<c>MeshTangent</c>), or <c>null</c>
            /// when the piece carries no such buffer — the game indexes both unconditionally, so
            /// a piece without them is an asset the game itself could not draw, and its vertices
            /// fall back to the face rebuild.
            /// </summary>
            public float3[] Normals;
            public float4[] Tangents;

            public int[] RangeStartVertex, RangeVertexCount, RangeStartIndex, RangeIndexCount;
            public string[] RangeMaterial;
        }

        private readonly Dictionary<Entity, PieceGeometry> _pieceGeometry = new();

        // ---- terrain conforming (LowerBottomToTerrain / RaiseTopToTerrain) ------------------
        // The game classifies each vertex of a flagged piece against per-z-slice height bounds
        // (`BatchMeshHelpers.InitializeHeightBounds`, 257 slices) and writes the class into the
        // vertex colour; its vertex shader then applies the terrain move per class. The two
        // moves differ (§38): class 1 (LowerBottomToTerrain) is a downward CLAMP — the bottom
        // edge is buried where the terrain is below it and left at road level where it is not,
        // which is what keeps a trench wall standing; class 2 (RaiseTopToTerrain) is an
        // ASSIGNMENT — the top edge is pinned to the terrain surface in both directions, which
        // is how one portal-face asset closes a trench of any depth (§34). Applied here at rip
        // time against the same flattened heightmap the bundle ships, so the meshes arrive
        // closed.
        //

        /// <summary>Per-slice min/max heights of one piece's authored geometry.</summary>
        private sealed class PieceHeightBounds
        {
            public float SizeZ;
            public float[] Min;
            public float[] Max;
        }

        private readonly Dictionary<Entity, PieceHeightBounds> _pieceHeightBounds = new();

        /// <summary>
        /// Per-prefab aggregate of the conforming pass — the §38 probe. One row per flagged
        /// piece in the rip report: how its vertices classified, where the move sent them, and
        /// what the triangle cuts dropped.
        /// </summary>
        private sealed class ConformPieceProbe
        {
            public string Flags;
            public float3 Size;
            public int Vertices;
            public int Class1, Class2;
            public int Lowered, Kept, Raised;
            public int FrontCutTris, BackCutTris, OutOfWindowTris;
        }

        private readonly Dictionary<string, ConformPieceProbe> _conformProbes = new();

        /// <summary>
        /// Per piece, what the cross-section walk actually BUILT — the §42 probe.
        /// <para>"Which piece was read" is already reported; what is missing is how far across
        /// the frame its vertices reached and how big the geometry it produced is. A piece that
        /// is read, placed, and collapses to a ribbon looks identical to a healthy one in every
        /// other count the walk keeps, and that is how an elevated tram came to ship its
        /// structure with no running surface (§42).</para>
        /// <para>The companion half-frame reach probe is gone: §41.9's short flare was never a
        /// piece-to-frame mapping fault, and the reach it measured is answered offline by
        /// `tools/test_node_fan.py` against the shipped meshes (§43).</para>
        /// <para><b>This one stays permanently</b> (the open PROBE question, settled 2026-08-12).
        /// It was kept where its companion was dropped because the two numbers it reports —
        /// authored piece size against the extent that piece produced — are not derivable from
        /// the bundle: the authored size never ships, so no offline gate can ask the question at
        /// all. What it costs was the real objection, and that is now paid once per distinct
        /// piece rather than once per placement; see <see cref="PieceKey"/>.</para>
        /// </summary>
        private sealed class SectionProbe
        {
            public string Name;                  // the piece prefab's name
            public string Where;                 // "node" or "edge"
            public float3 Size;                  // authored piece size
            public float OffsetX;                // authored cross-section offset
            public int Composition;              // the composition entity this row is for
            public int Placements, Vertices;
            public float XMin = float.MaxValue, XMax = float.MinValue;
            public float3 Lo, Hi;                // emitted extent, relative to composition centre
            public bool HasBounds;

            /// <summary>The report's row label. Carries the composition because one piece now
            /// gets one row PER composition — without it two rows would print identically and a
            /// per-composition collapse would read as a duplicate rather than as a finding.</summary>
            public string Label => $"{Name}|{Where}|comp{Composition}";
        }

        /// <summary>
        /// What identifies a probe row: the piece prefab, <b>the composition it was placed in</b>,
        /// and which half of the network. <b>Keyed by entity, not by name, and that is the
        /// point</b> — the probe runs inside the per-placement loop of the mesh stage, the longest
        /// stage of the rip, so resolving a prefab name and interpolating a string there charged
        /// the whole city for a diagnostic that only ever needed the name once per distinct piece.
        /// <para>
        /// <b>The composition is in the key because the defect it hunts is per-composition</b>
        /// <i>(added 2026-08-14)</i>. The extents accumulate as `world - centre` against the
        /// CURRENT segment's centre, so unioning a piece's placements across compositions unions
        /// different local frames: a piece that collapses to a ribbon on the elevated tram deck
        /// and builds normally on 200 at-grade segments produced a healthy union extent and a
        /// full `xParam` span, and neither clause fired. That is exactly the §42 shape — same
        /// piece, different composition — the probe exists to catch.
        /// </para>
        /// </summary>
        private readonly struct PieceKey : IEquatable<PieceKey>
        {
            private readonly Entity _piece;
            private readonly Entity _composition;
            private readonly bool _isNode;

            public PieceKey(Entity piece, Entity composition, bool isNode)
            {
                _piece = piece;
                _composition = composition;
                _isNode = isNode;
            }

            public bool Equals(PieceKey other) => (_piece == other._piece)
                                               && (_composition == other._composition)
                                               && (_isNode == other._isNode);
            public override bool Equals(object obj) => (obj is PieceKey other) && Equals(other);
            public override int GetHashCode()
            {
                int hash = _piece.GetHashCode() * 397;
                hash = (hash ^ _composition.GetHashCode()) * 397;
                return hash ^ (_isNode ? 1 : 0);
            }
        }

        private readonly Dictionary<PieceKey, SectionProbe> _sectionProbes = new();

        private ushort[] _conformHeights;
        private int2 _conformResolution;
        private float3 _conformScale;
        private float3 _conformOffset;
        /// <summary>Which raster the conform samples: "fine cascade (1.750 m)" or "base heightmap (3.500 m)".</summary>
        private string _conformSource;

        /// <summary>
        /// Lazily capture the terrain heightmap for conforming. The game's shader conforms per
        /// frame against the finest cascade slice covering the vertex (0.875–1.75 m near the
        /// camera); the fine cascade capture (<c>TerrainRipSystem.FineCascade.cs</c>, run before
        /// this stage) is that ground at 1.75 m, so it is sampled when present and the base
        /// 3.5 m readback — the same one the terrain layer ships — otherwise (§34's counted
        /// approximation, now only when the capture did not run).
        /// </summary>
        private bool EnsureConformTerrain()
        {
            if (_conformHeights != null) return _conformHeights.Length > 0;
            try
            {
                Game.Simulation.TerrainHeightData heightData = Instance.Terrain.GetHeightData(true);
                if (!heightData.isCreated)
                {
                    _conformHeights = Array.Empty<ushort>();
                    return false;
                }
                if (Instance.TerrainRip.TryGetFineHeights(out ushort[] fine, out int fineWidth, out int fineHeight,
                                                          out double fineCell, out double2 fineOriginSW))
                {
                    // Same bilinear read: texel (i, j) is centred at originSW + (i, j)·cell, so
                    // heightmap-space = (world − originSW) / cell — the base map's own
                    // (world + offset)·scale form with offset = −originSW and scale = 1/cell; the
                    // elevation encoding is the cascade's, i.e. the base map's.
                    _conformHeights = fine;
                    _conformResolution = new int2(fineWidth, fineHeight);
                    _conformScale = new float3((float)(1.0 / fineCell), heightData.scale.y, (float)(1.0 / fineCell));
                    _conformOffset = new float3((float)-fineOriginSW.x, heightData.offset.y, (float)-fineOriginSW.y);
                    _conformSource = $"fine cascade ({fineCell:F3} m)";
                    return true;
                }
                _conformHeights = heightData.heights.ToArray();
                _conformResolution = new int2(heightData.resolution.x, heightData.resolution.z);
                _conformScale = heightData.scale;
                _conformOffset = heightData.offset;
                _conformSource = $"base heightmap ({1f / heightData.scale.x:F3} m)";
                return true;
            }
            catch (Exception)
            {
                _conformHeights = Array.Empty<ushort>();
                return false;
            }
        }

        /// <summary>`TerrainUtils.SampleHeight`'s bilinear read, over the captured copy.
        ///</summary>
        private float SampleConformHeight(float3 world)
        {
            float2 xz = ((world + _conformOffset) * _conformScale).xz;
            int2 lo = (int2)math.floor(xz);
            int2 hi = lo + 1;
            lo = math.clamp(lo, 0, _conformResolution - 1);
            hi = math.clamp(hi, 0, _conformResolution - 1);
            float h00 = _conformHeights[(lo.y * _conformResolution.x) + lo.x];
            float h10 = _conformHeights[(lo.y * _conformResolution.x) + hi.x];
            float h01 = _conformHeights[(hi.y * _conformResolution.x) + lo.x];
            float h11 = _conformHeights[(hi.y * _conformResolution.x) + hi.x];
            float2 f = math.saturate(xz - math.floor(xz));
            float height = math.lerp(math.lerp(h00, h10, f.x), math.lerp(h01, h11, f.x), f.y);
            return (height / _conformScale.y) - _conformOffset.y;
        }

        /// <summary>
        /// The per-slice height bounds of one piece — `InitializeHeightBounds`/`AddHeightBounds`
        /// verbatim, cached per piece entity.
        /// </summary>
        private PieceHeightBounds GetPieceHeightBounds(Entity piece, PieceGeometry source, float sizeZ)
        {
            if (_pieceHeightBounds.TryGetValue(piece, out PieceHeightBounds cached)
                && (math.abs(cached.SizeZ - sizeZ) < 1e-3f))
            {
                return cached;
            }

            const int slices = 256;
            PieceHeightBounds bounds = new()
            {
                SizeZ = sizeZ,
                Min = new float[slices + 1],
                Max = new float[slices + 1]
            };
            for (int i = 0; i <= slices; i++)
            {
                bounds.Min[i] = float.MaxValue;
                bounds.Max[i] = float.MinValue;
            }

            float scale = slices / math.max(sizeZ, 0.001f);
            void AddEdge(float3 a, float3 b, int ia, int ib)
            {
                if (ia > ib)
                {
                    (a, b) = (b, a);
                    (ia, ib) = (ib, ia);
                }
                float step = 1f / (ib - ia + 1);
                for (int i = ia; i <= ib; i++)
                {
                    float y = math.lerp(a.y, b.y, (i - ia) * step);
                    bounds.Min[i] = math.min(bounds.Min[i], y);
                    bounds.Max[i] = math.max(bounds.Max[i], y);
                }
            }

            for (int t = 0; t + 2 < source.Indices.Length; t += 3)
            {
                float3 a = source.Vertices[source.Indices[t]];
                float3 b = source.Vertices[source.Indices[t + 1]];
                float3 c = source.Vertices[source.Indices[t + 2]];
                int ia = math.clamp((int)math.round(a.z * scale) + (slices >> 1), 0, slices);
                int ib = math.clamp((int)math.round(b.z * scale) + (slices >> 1), 0, slices);
                int ic = math.clamp((int)math.round(c.z * scale) + (slices >> 1), 0, slices);
                AddEdge(a, b, ia, ib);
                AddEdge(b, c, ib, ic);
                AddEdge(c, a, ic, ia);
            }

            _pieceHeightBounds[piece] = bounds;
            return bounds;
        }

        // ---- CalculateMappingOffsets / Edge / Node / Orphan parameters, ported --------------
        //

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

        // ---- The generator's vertex path, ported and evaluated ------------------------------

        /// <summary>
        /// Generate one composition instance exactly as `GenerateCompositionMesh` maps it, into
        /// the shared assembly lists.
        /// </summary>
        /// <summary>
        /// One node side of an edge: parameters from its own geometry, mesh from its composition.
        /// </summary>
        private void BuildNodeSide(Entity composition, EdgeNodeGeometry geometry, float3 center,
                                   Dictionary<Entity, MeshAsset> cache, MeshAssembly assembly)
        {
            // Node compositions are never rotate-shared: `NetCompositionMeshRefSystem.
            // TryFindComposition` only attempts the mirrored match when the Node general flag is
            // absent, so a node's `m_Rotate` is always false and needs no handling here.
            //
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            CompositionCurves curves = NodeCurves(geometry, data);
            BuildGameComposition(composition, curves, center, cache, assembly);
        }

        /// <summary>
        /// Everything one COMPOSITION contributes to every piece it holds, read once.
        /// </summary>
        /// <remarks>
        /// The generator's own preamble, as a record rather than eight locals threaded through a
        /// 550-line loop. Nothing here changes once the composition is read, which is what makes
        /// the piece and range walks below safe to read in isolation.
        /// </remarks>
        private sealed class CompositionSetup
        {
            /// <summary>The composition entity, for the §42 probe key.</summary>
            public Entity Composition;

            /// <summary>The composition's own data — width and offsets the maps are built from.</summary>
            public NetCompositionData Data;

            /// <summary>Whether this composition is a node's, and whether it is a roundabout's.</summary>
            public bool IsNode, Roundabout;

            /// <summary>An elevated composition standing clear of the ground; see the note below.</summary>
            public bool ElevatedAwayFromGround;

            /// <summary>The lateral offset of the middle curve from the centre line.</summary>
            public float MiddleOffset;

            /// <summary>
            /// The two half widths, either side of the middle curve. The decompiler called these
            /// <c>num7</c> and <c>num8</c>; the trailing comments in the original said what they
            /// are, and now the names do.
            /// </summary>
            public float LeftHalfWidth, RightHalfWidth;

            /// <summary>The composition's pieces, copied out of the ECS buffer.</summary>
            public NetCompositionPiece[] Pieces;
        }

        /// <summary>
        /// Everything one PIECE contributes to every range and vertex it holds, read once.
        /// </summary>
        private sealed class PieceSetup
        {
            /// <summary>The piece itself, and its authored geometry.</summary>
            public NetCompositionPiece Piece;

            /// <summary>The authored buffers this piece was read from.</summary>
            public PieceGeometry Source;

            /// <summary>The section flags, decoded.</summary>
            public bool Invert, FlipMesh, Median, RightSide, HalfLength;

            /// <summary>The piece flags, decoded.</summary>
            public bool PreserveShape, DisableTiling, LowerBottom, RaiseTop, SmoothTop;

            /// <summary>Which half of the authored tile the cuts remove, and whether x is pinned.</summary>
            public bool FrontCut, BackCut, ZeroWidth;

            /// <summary>The authored shading frame, or null where the piece carries none.</summary>
            public float3[] Normals;

            /// <summary>The authored tangents, or null.</summary>
            public float4[] Tangents;

            /// <summary>Whether this piece ships a shading frame at all.</summary>
            public bool Authored;

            /// <summary>The per-slice height bounds, when any conform or smoothing rule wants them.</summary>
            public PieceHeightBounds HeightBounds;

            /// <summary>Slices per authored metre along the piece.</summary>
            public float BoundsScaleZ;

            /// <summary>Whether terrain conforming is both wanted and available this run.</summary>
            public bool Conforming;

            /// <summary>The §42 section probe row for this piece in this composition.</summary>
            public SectionProbe Section;

            /// <summary>The conform probe row, when this piece conforms.</summary>
            public ConformPieceProbe Probe;

            /// <summary>The game's flip select, as three scalars.</summary>
            public float Sx, Sz, Sw;

            /// <summary>The piece's placement offset, after the half-length shift.</summary>
            public float3 Offset;

            /// <summary>The three map scales — full frame, left half, right half.</summary>
            public float2 Scale4, Scale5, Scale6;

            /// <summary>Their biases.</summary>
            public float2 Bias7, Bias8, Bias9;

            /// <summary>The index read order, reversed under Invert XOR FlipMesh.</summary>
            public int3 Order;
        }

        /// <summary>One material range's window into the piece's arrays.</summary>
        private readonly struct PieceRange
        {
            /// <summary>Where the range starts and how long it runs, in vertices and in indices.</summary>
            public readonly int StartVertex, VertexCount, StartIndex, IndexCount;

            /// <summary>Read one range out of the piece's parallel arrays.</summary>
            /// <param name="source">The piece's authored geometry.</param>
            /// <param name="r">The range index.</param>
            public PieceRange(PieceGeometry source, int r)
            {
                StartVertex = source.RangeStartVertex[r];
                VertexCount = source.RangeVertexCount[r];
                StartIndex = source.RangeStartIndex[r];
                IndexCount = source.RangeIndexCount[r];
            }
        }

        /// <summary>Where one vertex lands in the composition's frame, before the curves are read.</summary>
        private struct VertexMap
        {
            /// <summary>Which two of the composition's curves this vertex lerps between.</summary>
            public int2 Pair;

            /// <summary>The lerp coordinate across, and the curve parameter along.</summary>
            public float XParam, ZParam;

            /// <summary>
            /// How much of the piece's own across/along metre each parameter is worth — the
            /// diagonal of the map's Jacobian, kept per branch because each half frame has its
            /// own width. <c>MapScale.x == 0</c> marks a pinned column, whose across direction
            /// the map collapses.
            /// </summary>
            public float2 MapScale;

            /// <summary>Whether this vertex went through a half frame, and which one.</summary>
            public bool Halved, LeftHalf;
        }

        private void BuildGameComposition(Entity composition, in CompositionCurves p,
                                          float3 center, Dictionary<Entity, MeshAsset> cache,
                                          MeshAssembly assembly)
        {
            CompositionSetup setup = ReadComposition(composition);
            if (setup == null) return;

            foreach (NetCompositionPiece piece in setup.Pieces)
            {
                PieceGeometry source = GetPieceGeometry(piece.m_Piece, cache);
                if (source == null)
                {
                    RecordEmptyPiece(piece);
                    continue;
                }

                PieceSetup pieceSetup = ReadPiece(setup, piece, source);
                for (int r = 0; r < source.RangeMaterial.Length; r++)
                {
                    BuildRange(setup, pieceSetup, new PieceRange(source, r), r, p, center, assembly);
                }
            }
        }

        /// <summary>
        /// The composition's own preamble: the guards, the world-layer skip, and the eight values
        /// every piece is placed against. Null when this composition contributes no mesh.
        /// </summary>
        /// <param name="composition">The composition entity.</param>
        /// <returns>The setup, or null.</returns>
        private CompositionSetup ReadComposition(Entity composition)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return null;
            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return null;
            if (!EntityManager.HasComponent<NetCompositionData>(composition)) return null;
            if (SkipUnworldlyLayers(meshEntity)) return null;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            bool roundabout = (data.m_Flags.m_General & CompositionFlags.General.Roundabout) != 0;
            if (roundabout) _roundaboutParts++;

            DynamicBuffer<NetCompositionPiece> buffer = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);
            NetCompositionPiece[] pieces = new NetCompositionPiece[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) pieces[i] = buffer[i];

            return new CompositionSetup
            {
                Composition = composition,
                Data = data,
                IsNode = (data.m_Flags.m_General & CompositionFlags.General.Node) != 0,
                Roundabout = roundabout,
                ElevatedAwayFromGround = IsElevatedAwayFromGround(data),
                MiddleOffset = data.m_MiddleOffset,
                LeftHalfWidth = (data.m_Width * 0.5f) + data.m_MiddleOffset,
                RightHalfWidth = (data.m_Width * 0.5f) - data.m_MiddleOffset,
                Pieces = pieces
            };
        }

        /// <summary>
        /// Whether this composition's mesh draws on no world camera, and so is skipped.
        /// </summary>
        /// <remarks>
        /// The game replaces the Default batch layer with the mesh's own default layers when
        /// they name any (<c>UpdateNetInstances</c>) — and then still draws the mesh on those
        /// layers. <c>CommonUtils.GetBoundsMask</c> names the world-view set: Default, Moving,
        /// Tunnel and Marker are <c>NormalLayers</c>; only Pipeline/SubPipeline/Waterway select
        /// the utility-cutaway and overlay cameras. So the skip is for compositions whose
        /// layers hold no world bit — underground pipe and cable runs — and NOT for Tunnel
        /// compositions, which are the tunnel walls and decks themselves. (An earlier
        /// revision skipped every Default-less composition, which silenced all tunnels.)
        /// </remarks>
        /// <param name="meshEntity">The composition's mesh entity.</param>
        /// <returns>Whether to skip it.</returns>
        private bool SkipUnworldlyLayers(Entity meshEntity)
        {
            if (!EntityManager.HasComponent<NetCompositionMeshData>(meshEntity)) return false;

            const MeshLayer world = MeshLayer.Default | MeshLayer.Moving | MeshLayer.Tunnel | MeshLayer.Marker;
            MeshLayer defaultLayers = EntityManager.GetComponentData<NetCompositionMeshData>(meshEntity).m_DefaultLayers;
            if ((defaultLayers != 0) && ((defaultLayers & world) == 0))
            {
                _pipelineCompositions++;
                return true;
            }
            return false;
        }

        /// <summary>
        /// Whether this composition is an elevated deck standing clear of the ground, whose
        /// pieces must NOT be conformed to it.
        /// </summary>
        /// <remarks>
        /// A deck standing 17 m above grade must not be dragged down to it. Terrain conforming
        /// was gated on PIECE flags alone (LowerBottomToTerrain / RaiseTopToTerrain), and
        /// RaiseTopToTerrain assigns <c>world.y = terrain</c> unconditionally — so any elevated
        /// composition whose pieces carry that flag would have its top edge pinned to the
        /// ground far below. The §38 downward clamp is no protection here: for a deck above
        /// grade <c>terrain &lt; world.y</c> always holds, so clamp and assignment agree.
        /// <para>
        /// Dormant in the city ripped 2026-08-10 (0.07% of elevated vertices sit within 2 cm
        /// of the heightmap, per-segment median 0.000%), so this changes no shipped geometry
        /// there — it closes a latent trap.
        /// </para>
        /// <para>
        /// The exemption is the roadbed walk's own rule, verbatim (<c>TryGetGroundComposition</c>):
        /// an elevated composition with a Low/HighTransition side is where the deck MEETS the
        /// ground, and that end still has to conform or the ramp separates from its approach.
        /// </para>
        /// <para>
        /// <b>This is a producer CHOICE, not a port</b> (stated plainly 2026-08-14, after a
        /// review found no counterpart for it in the game). <c>BatchMeshHelpers</c> sets the
        /// conform classification from <c>NetPieceFlags</c> alone — there is no <c>Elevated</c>
        /// test anywhere in that path — but it does not conform on the CPU at all: it writes the
        /// class into a vertex <c>Color32.b</c> and the SHADER decides what to do with it. CS2
        /// ships its shaders compiled, so what the GPU does with a raise-top vertex 17 m above
        /// its terrain sample is not readable, and the producer has to pick. It picks "leave the
        /// deck where it is", which cannot separate a deck from its own ramp because the
        /// transition sides are exempt. Recorded as a choice so the next person does not read it
        /// as a faithful port of something.
        /// </para>
        /// </remarks>
        /// <param name="data">The composition's data.</param>
        /// <returns>Whether its pieces stand clear of the ground.</returns>
        private static bool IsElevatedAwayFromGround(NetCompositionData data)
        {
            bool elevated = (data.m_Flags.m_General & CompositionFlags.General.Elevated) != 0;
            CompositionFlags.Side conformSides = data.m_Flags.m_Left | data.m_Flags.m_Right;
            return elevated
                && ((conformSides & (CompositionFlags.Side.LowTransition
                                     | CompositionFlags.Side.HighTransition)) == 0);
        }

        /// <summary>
        /// Name a piece whose geometry came back empty.
        /// </summary>
        /// <remarks>
        /// The one exclusion in this walk that said nothing. A composition whose running surface
        /// is dropped here still ships its RoadParts, so the segment looks present and is hollow
        /// — measured on the 2026-08-11 rip, an elevated tram deck carries 12% of the up-facing
        /// area its width and length imply against 72% for an elevated road or train. Name the
        /// piece so the next rip says which one, instead of leaving it to be found by rendering
        /// a viaduct.
        /// </remarks>
        /// <param name="piece">The piece with no geometry.</param>
        private void RecordEmptyPiece(NetCompositionPiece piece)
        {
            _emptyPieces++;
            if (_emptyPieceNames.Count < 64)
            {
                _emptyPieceNames.Add(Instance.Prefab.TryGetPrefab(piece.m_Piece, out PrefabBase emptyPiece)
                                     && (emptyPiece != null)
                    ? emptyPiece.name : $"<entity {piece.m_Piece.Index}>");
            }
        }

        /// <summary>
        /// One piece's flags, shading frame, conform decision and coordinate maps, read once.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <param name="source">Its authored geometry.</param>
        /// <returns>The piece setup every range and vertex below is placed against.</returns>
        private PieceSetup ReadPiece(CompositionSetup setup, NetCompositionPiece piece, PieceGeometry source)
        {
            PieceSetup ps = ReadPieceFlags(setup, piece, source);
            ps.Section = ProbeSection(setup, piece);
            ResolveConform(setup, ps, piece);
            ComputeMaps(setup, ps, piece);
            return ps;
        }

        /// <summary>
        /// The section and piece flags, and the authored shading frame.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <param name="source">Its authored geometry.</param>
        /// <returns>A partly-filled setup; the caller adds the probes and the maps.</returns>
        private PieceSetup ReadPieceFlags(CompositionSetup setup, NetCompositionPiece piece, PieceGeometry source)
        {
            PieceSetup ps = new PieceSetup
            {
                Piece = piece,
                Source = source,
                Invert = (piece.m_SectionFlags & NetSectionFlags.Invert) != 0,
                FlipMesh = (piece.m_SectionFlags & NetSectionFlags.FlipMesh) != 0,
                Median = (piece.m_SectionFlags & NetSectionFlags.Median) != 0,
                RightSide = (piece.m_SectionFlags & NetSectionFlags.Right) != 0,
                HalfLength = (piece.m_SectionFlags & NetSectionFlags.HalfLength) != 0,
                PreserveShape = (piece.m_PieceFlags & NetPieceFlags.PreserveShape) != 0,
                DisableTiling = (piece.m_PieceFlags & NetPieceFlags.DisableTiling) != 0,
                BackCut = (piece.m_PieceFlags & NetPieceFlags.SkipBottomHalf) != 0,
                ZeroWidth = piece.m_Size.x == 0f,
                LowerBottom = (piece.m_PieceFlags & NetPieceFlags.LowerBottomToTerrain) != 0,
                RaiseTop = (piece.m_PieceFlags & NetPieceFlags.RaiseTopToTerrain) != 0,
                SmoothTop = (piece.m_PieceFlags & NetPieceFlags.SmoothTopNormal) != 0
            };
            ps.FrontCut = (setup.IsNode && !setup.Roundabout && !ps.PreserveShape) || ps.HalfLength;
            ps.Order = math.select(new int3(0, 1, 2), new int3(2, 1, 0), ps.Invert != ps.FlipMesh);

            if (ps.PreserveShape) _railPieces++;
            if (ps.BackCut) _skipBottomPieces++;
            if (ps.LowerBottom) _lowerBottomPieces++;
            if (ps.RaiseTop) _raiseTopPieces++;
            if (ps.SmoothTop) _smoothTopPieces++;

            // Authored shading frame, or the face rebuild for a piece that carries none.
            //
            ps.Normals = source.Normals;
            ps.Tangents = source.Tangents;
            ps.Authored = ps.Normals != null;
            if (ps.Authored) _authoredFramePieces++;
            else _rebuiltFramePieces++;
            return ps;
        }

        /// <summary>
        /// The §42 probe row for this piece in this composition: one row per piece, keyed before
        /// the vertex loop so the loop only accumulates. Resolving the name is the expensive half
        /// and happens on first sight of a piece, never per placement.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <returns>The probe row, with this placement already counted.</returns>
        private SectionProbe ProbeSection(CompositionSetup setup, NetCompositionPiece piece)
        {
            PieceKey sectionKey = new PieceKey(piece.m_Piece, setup.Composition, setup.IsNode);
            if (!_sectionProbes.TryGetValue(sectionKey, out SectionProbe section))
            {
                string sectionName = Instance.Prefab.TryGetPrefab(piece.m_Piece, out PrefabBase sectionPiece)
                                     && (sectionPiece != null)
                    ? sectionPiece.name : $"<entity {piece.m_Piece.Index}>";
                _sectionProbes[sectionKey] = section = new SectionProbe
                {
                    Name = sectionName,
                    Where = setup.IsNode ? "node" : "edge",
                    Composition = setup.Composition.Index,
                    Size = piece.m_Size,
                    OffsetX = piece.m_Offset.x
                };
            }
            section.Placements++;
            return section;
        }

        /// <summary>
        /// Whether this piece conforms to the terrain, and the height bounds the classification
        /// needs.
        /// </summary>
        /// <remarks>
        /// The per-slice height bounds serve two rules, so they are built for either
        /// (<c>BatchMeshHelpers</c> line 682: <c>flag17 || flag18 || flag19</c>). Terrain
        /// conforming classifies each vertex against them and applies the vertex shader's terrain
        /// move at rip time — the portal face over every tunnel mouth is a RaiseTopToTerrain
        /// piece, and without it the mouth stands open above the arch. SmoothTopNormal needs no
        /// terrain at all: it only replaces the authored normal at the top bound.
        /// </remarks>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="ps">The piece setup to fill.</param>
        /// <param name="piece">The piece.</param>
        private void ResolveConform(CompositionSetup setup, PieceSetup ps, NetCompositionPiece piece)
        {
            if (ps.LowerBottom || ps.RaiseTop || ps.SmoothTop)
            {
                ps.HeightBounds = GetPieceHeightBounds(piece.m_Piece, ps.Source, piece.m_Size.z);
                ps.BoundsScaleZ = 256f / math.max(piece.m_Size.z, 0.001f);
            }

            // The gate applies to `RaiseTop` ONLY (narrowed 2026-08-14). The argument for
            // it is about a TOP being dragged down to a ground the deck stands clear of; it
            // says nothing about a bottom reaching down to meet the ground, and the game
            // tags the two independently. Suppressing both left an elevated composition's
            // abutment and retaining pieces floating.
            //
            bool wantsConform = ps.LowerBottom || (ps.RaiseTop && !setup.ElevatedAwayFromGround);
            if (wantsConform)
            {
                // N-F2: a heightmap that cannot be captured used to leave the piece silently
                // unconformed while the report still claimed it was conformed. Counted here,
                // declared in warnings.jsonl and stated honestly in the report.
                //
                ps.Conforming = EnsureConformTerrain();
                if (!ps.Conforming) _unconformedPieces++;
            }

            if (ps.RaiseTop && setup.ElevatedAwayFromGround)
            {
                // Deliberately not raised: an elevated composition clear of the ground.
                // Counted separately from `_unconformedPieces`, which means "we wanted to
                // and could not" — this one means "we must not".
                _elevatedUnconformedPieces++;
            }

            if (!ps.Conforming) return;

            string probeName = Instance.Name.GetPrefabName(piece.m_Piece);
            if (!_conformProbes.TryGetValue(probeName, out ConformPieceProbe probe))
            {
                _conformProbes[probeName] = probe = new ConformPieceProbe
                {
                    Flags = (ps.LowerBottom && ps.RaiseTop) ? "LowerBottom+RaiseTop"
                          : ps.LowerBottom ? "LowerBottom" : "RaiseTop",
                    Size = piece.m_Size
                };
            }
            ps.Probe = probe;
        }

        /// <summary>
        /// The flip select, the placement offset, and the three coordinate maps.
        /// </summary>
        /// <remarks>
        /// <para>
        /// The game's flip select — <c>select(float4(-1,1,-1,1), float4(1,1,1,-1),
        /// bool4(Invert, false, FlipMesh, Invert != FlipMesh))</c>: authored pieces are x- and
        /// z-negated by default; Invert restores x, FlipMesh restores z. The <c>w</c> term flips
        /// tangent handedness, and now that the authored frame is shipped it is applied as the
        /// game applies it (<c>tangent *= float3</c>, all four components).
        /// </para>
        /// <para>
        /// The generator computes two more per-vertex quantities here that this port does not:
        /// <c>v.z</c> — the piece's OWN cross-anchor in the same xParam coordinate the position
        /// lerp uses (<c>offset.x/width + 0.5</c> on an edge or a rail, the half-frame forms
        /// <c>1 + (offset.x − middleOffset)/leftHalf</c> and
        /// <c>(offset.x − middleOffset)/rightHalf</c> at a node, 0 and 1 at a roundabout's island
        /// and outer edge) — and <c>v.w</c>, the pre-rebase along coordinate scaled by 0.5 on an
        /// edge, 20 at a node and 40 at a roundabout. Both go into the vertex's <c>UV0.zw</c> and
        /// are read only by the compiled vertex shader, which is not decompilable, so what they
        /// drive is unknown. They cannot be a position term: <c>m_Position</c> is written from
        /// <c>vertex</c> alone, and this port evaluates the same map on the CPU and bakes the
        /// result. So they are deliberately not computed — a value assigned to nothing reads as a
        /// half-finished port, and this comment is the honest form of it. (Re-derived from
        /// <c>GenerateCompositionMesh</c> 2026-08-17, audit §53.)
        /// </para>
        /// </remarks>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="ps">The piece setup to fill.</param>
        /// <param name="piece">The piece.</param>
        private static void ComputeMaps(CompositionSetup setup, PieceSetup ps, NetCompositionPiece piece)
        {
            ps.Sx = ps.Invert ? 1f : -1f;
            ps.Sz = ps.FlipMesh ? 1f : -1f;
            ps.Sw = (ps.Invert != ps.FlipMesh) ? -1f : 1f;

            ps.Offset = piece.m_Offset;
            ps.Scale4 = new(1f / setup.Data.m_Width, 1f / (piece.m_Size.z * 0.5f));
            ps.Scale5 = new(1f / setup.LeftHalfWidth, 1f / (piece.m_Size.z * 0.5f));
            ps.Scale6 = new(1f / setup.RightHalfWidth, 1f / (piece.m_Size.z * 0.5f));
            ps.Bias7 = new(0.5f, 1f);
            ps.Bias8 = new(1f - (setup.MiddleOffset / setup.LeftHalfWidth), 1f);
            ps.Bias9 = new((0f - setup.MiddleOffset) / setup.RightHalfWidth, 1f);

            if (setup.IsNode && ps.PreserveShape)
            {
                float railScale = 0.5f * piece.m_Size.z / setup.Data.m_Width;
                ps.Scale4.y *= railScale;
                ps.Bias7.y *= railScale;
            }
            else if (setup.Roundabout)
            {
                // Deliberately empty, and load-bearing. The game's branch here sets only the
                // `v.z` cross-anchor described above — but the branch itself must stay,
                // because being an `else if` is what stops a HalfLength piece inside a
                // roundabout composition from taking the halving below. That is the game's own
                // else-if chain, and deleting the branch would silently change which pieces
                // get `Sz *= 2`.
                //
            }
            else if (ps.HalfLength)
            {
                ps.Sz *= 2f;
                ps.Offset.z += 0.5f * piece.m_Size.z;
            }
        }

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
        /// Which two curves this vertex lerps between, and where along and across them.
        /// </summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex; a median piece may pin its x.</param>
        /// <returns>The map.</returns>
        private static VertexMap MapVertex(CompositionSetup setup, PieceSetup ps, ref float3 vertex)
        {
            VertexMap map = default;
            if (!setup.IsNode)
            {
                map.Pair = math.select(new int2(0, 2), new int2(1, 3), vertex.z >= 0f);
                map.XParam = (vertex.x * ps.Scale4.x) + ps.Bias7.x;
                map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                if (vertex.z >= 0f) map.ZParam -= 1f;
                map.MapScale = ps.Scale4;
                return map;
            }

            if (ps.PreserveShape)
            {
                map.Pair = new int2(6, 7);
                map.XParam = (vertex.x * ps.Scale4.x) + ps.Bias7.x;
                map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                map.MapScale = ps.Scale4;
                return map;
            }

            if (ps.Median)
            {
                // dx keeps the AUTHORED spread even for a zero-width piece — the game computes it
                // before pinning x. The pin only moves the lerp coordinate, so a zero-width
                // intersection piece routes its side columns into the half frames where the
                // pinned x lands them ON the inner boundary curves, while its centre column rides
                // the middle to the junction centre: a three-rail ruled surface that paves the
                // centre wedge. Treating zero width as all-centre — this port's previous reading,
                // and audit §8's — collapsed the piece to a zero-area ribbon and left every
                // median-less-looking junction wedge open (audit §18; the `* Intersection Piece 0`
                // sections are exactly these).
                //
                float dx = vertex.x - ps.Piece.m_Offset.x;
                if (ps.ZeroWidth) vertex.x = ps.Piece.m_Offset.x;
                if (math.abs(dx) < 0.01f)
                {
                    // The game does NOT rebase the centre column's front half (its roundabout
                    // side branches subtract `int8 - base`, this one has no subtraction): the
                    // final saturate pins those vertices at the middle curve's end. Matched
                    // exactly.
                    //
                    map.Pair = setup.Roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                                : new int2(4, 4);
                    map.XParam = 0f;
                    map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                    map.MapScale = new float2(0f, ps.Scale4.y);
                    return map;
                }
                if (dx > 0f) return RightHalfMap(setup, ps, vertex);
                return LeftHalfMap(setup, ps, vertex);
            }

            return ps.RightSide ? RightHalfMap(setup, ps, vertex) : LeftHalfMap(setup, ps, vertex);
        }

        /// <summary>The right half frame — the map a Right or positive-dx node vertex takes.</summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex.</param>
        /// <returns>The map.</returns>
        private static VertexMap RightHalfMap(CompositionSetup setup, PieceSetup ps, float3 vertex)
        {
            VertexMap map = default;
            map.Pair = setup.Roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                        : new int2(1, 3);
            map.XParam = (vertex.x * ps.Scale6.x) + ps.Bias9.x;
            map.ZParam = (vertex.z * ps.Scale6.y) + ps.Bias9.y;
            if (setup.Roundabout && (vertex.z >= 0f)) map.ZParam -= 1f;
            map.MapScale = ps.Scale6;
            map.Halved = true;
            map.LeftHalf = false;
            return map;
        }

        /// <summary>The left half frame — the map every other node vertex takes.</summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex.</param>
        /// <returns>The map.</returns>
        private static VertexMap LeftHalfMap(CompositionSetup setup, PieceSetup ps, float3 vertex)
        {
            VertexMap map = default;
            map.Pair = setup.Roundabout ? math.select(new int2(0, 4), new int2(1, 5), vertex.z >= 0f)
                                        : new int2(0, 2);
            map.XParam = (vertex.x * ps.Scale5.x) + ps.Bias8.x;
            map.ZParam = (vertex.z * ps.Scale5.y) + ps.Bias8.y;
            if (setup.Roundabout && (vertex.z >= 0f)) map.ZParam -= 1f;
            map.MapScale = ps.Scale5;
            map.Halved = true;
            map.LeftHalf = true;
            return map;
        }

        /// <summary>
        /// Move a classified vertex onto the terrain.
        /// </summary>
        /// <remarks>
        /// LowerBottomToTerrain buries the bottom edge: the shader move is a downward clamp,
        /// never a lift — a trench wall's base stays at road level (§38: assignment collapsed
        /// 10 m walls onto the terrain lip). RaiseTopToTerrain pins the top edge to the terrain
        /// surface in both directions — the §34-validated portal stretch, unchanged.
        /// </remarks>
        /// <param name="ps">The piece.</param>
        /// <param name="conform">1 for lower-bottom, 2 for raise-top.</param>
        /// <param name="world">The placed vertex, moved in place.</param>
        private void ApplyConform(PieceSetup ps, int conform, ref float3 world)
        {
            float terrain = SampleConformHeight(world);
            if (conform == 1)
            {
                if (terrain < world.y) { world.y = terrain; _conformLoweredVerts++; ps.Probe.Lowered++; }
                else { _conformKeptVerts++; ps.Probe.Kept++; }
            }
            else
            {
                world.y = terrain;
                _conformRaisedVerts++;
                ps.Probe.Raised++;
            }
        }

        /// <summary>
        /// What this piece actually built, in the frame's own coordinate and in metres.
        /// </summary>
        /// <remarks>
        /// A collapsed piece has a healthy vertex count and a degenerate extent; a short half
        /// frame has a healthy extent and an xParam that never reaches 1.
        /// </remarks>
        /// <param name="section">The §42 probe row.</param>
        /// <param name="world">The placed vertex.</param>
        /// <param name="center">The mesh origin.</param>
        /// <param name="xParam">The across coordinate this vertex reached.</param>
        private static void RecordSectionExtent(SectionProbe section, float3 world, float3 center, float xParam)
        {
            float3 relative = world - center;
            section.Vertices++;
            section.XMin = math.min(section.XMin, xParam);
            section.XMax = math.max(section.XMax, xParam);
            if (section.HasBounds)
            {
                section.Lo = math.min(section.Lo, relative);
                section.Hi = math.max(section.Hi, relative);
            }
            else
            {
                section.Lo = section.Hi = relative;
                section.HasBounds = true;
            }
        }

        /// <summary>
        /// The shading frame, rotated by the very map that placed the vertex.
        /// </summary>
        /// <remarks>
        /// Position is <c>lerp(A(z), B(z), x) + (0, y, 0)</c>, so the Jacobian in the piece's own
        /// metres is [across, up, along]: across = (B − A) per across-metre, up = (0,1,0),
        /// along = the two curves' derivatives blended at the same x, per along-metre. The normal
        /// takes the inverse transpose — the cofactor, which is that matrix times a determinant a
        /// normalize removes — and the tangent takes the plain Jacobian with <c>w</c> passed
        /// through, which is what keeps a stretched kerb's bevel at its authored angle where a
        /// rotation-only frame would shear it. APPROXIMATION (counted): the game's vertex shader
        /// is handed only these curves, so it can build no other frame, but whether it
        /// orthonormalizes before rotating is shader-side and unknowable from the C# side; the two
        /// coincide wherever the frame is orthogonal, which is everywhere the road is not both
        /// flared and banked.
        /// </remarks>
        /// <param name="ps">The piece.</param>
        /// <param name="map">Where this vertex landed.</param>
        /// <param name="a">The first curve's point at this z.</param>
        /// <param name="b">The second curve's point at this z.</param>
        /// <param name="normal">The flipped authored normal.</param>
        /// <param name="tangent">The flipped authored tangent.</param>
        /// <param name="p">The composition's curves.</param>
        /// <param name="assembly">The shared assembly lists.</param>
        private void ShadeVertex(PieceSetup ps, in VertexMap map, float3 a, float3 b,
                                 float3 normal, float4 tangent, in CompositionCurves p,
                                 MeshAssembly assembly)
        {
            if (!ps.Authored)
            {
                // No authored buffer: leave the zero the finisher rebuilds from faces.
                //
                assembly.Normals.Add(default);
                assembly.Tangents.Add(default);
                assembly.Rebuilt++;
                return;
            }

            float3 across = (b - a) * map.MapScale.x;
            float3 up = new(0f, 1f, 0f);
            float3 along = math.lerp(Derivative(p.Curves[map.Pair.x], map.ZParam),
                                     Derivative(p.Curves[map.Pair.y], map.ZParam), map.XParam) * map.MapScale.y;

            float3 rotated = (normal.x * math.cross(up, along))
                           + (normal.y * math.cross(along, across))
                           + (normal.z * math.cross(across, up));
            float3 rotatedTangent = (tangent.x * across) + (tangent.y * up) + (tangent.z * along);

            // A pinned column (the median centre, and any zero-width piece) maps its whole across
            // direction onto one curve, so the frame has no across column to take a cofactor of.
            // Fall back to the rotation the remaining two columns determine, and count it.
            //
            if (math.lengthsq(rotated) < 1e-12f)
            {
                float3 alongDirection = NormalizeOr(along, new float3(0f, 0f, 1f));
                float3 acrossDirection = NormalizeOr(math.cross(up, alongDirection), new float3(1f, 0f, 0f));
                rotated = (normal.x * acrossDirection) + (normal.y * up) + (normal.z * alongDirection);
                rotatedTangent = (tangent.x * acrossDirection) + (tangent.y * up) + (tangent.z * alongDirection);
                _pinnedFrameVerts++;
            }

            float3 shaded = NormalizeOr(rotated, new float3(0f, 1f, 0f));
            assembly.Normals.Add(shaded);

            // A piece with normals but no tangent buffer is normal-mapped by nothing, so any
            // perpendicular serves — the same one a consumer would generate for itself, chosen
            // here so the attribute is never a zero vector a renderer would divide by.
            //
            assembly.Tangents.Add((ps.Tangents != null)
                ? new float4(NormalizeOr(rotatedTangent, new float3(1f, 0f, 0f)), tangent.w)
                : AnyTangent(shaded));
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

        /// <summary>
        /// Read one piece's ECS geometry buffers — the exact arrays `GenerateCompositionMesh`
        /// reads — and resolve each material range's bundle id through the cached piece read.
        /// </summary>
        private PieceGeometry GetPieceGeometry(Entity piece, Dictionary<Entity, MeshAsset> cache)
        {
            if (piece == Entity.Null) return null;
            if (_pieceGeometry.TryGetValue(piece, out PieceGeometry cached)) return cached;

            PieceGeometry result = null;

            // The piece's own geometry, read through the reader every building goes through —
            // which PREFERS the geometry asset and falls back to the ECS buffers, not the other
            // way round. Hoisted out of the buffer branch below because it is now both the
            // material source and the geometry fallback.
            if (!cache.TryGetValue(piece, out MeshAsset read))
            {
                string pieceName = Instance.Name.GetPrefabName(piece);
                read = Instance.MeshRip.ReadMesh(piece, RipAssetIds.SanitizeAssetId(pieceName), pieceName);
                cache[piece] = read;
            }

            if (EntityManager.HasBuffer<MeshVertex>(piece) && EntityManager.HasBuffer<MeshIndex>(piece)
                && EntityManager.HasBuffer<MeshMaterial>(piece))
            {
                DynamicBuffer<MeshVertex> vertexBuffer = EntityManager.GetBuffer<MeshVertex>(piece, true);
                DynamicBuffer<MeshIndex> indexBuffer = EntityManager.GetBuffer<MeshIndex>(piece, true);
                DynamicBuffer<MeshMaterial> materialBuffer = EntityManager.GetBuffer<MeshMaterial>(piece, true);
                DynamicBuffer<MeshUV0> uvBuffer = EntityManager.HasBuffer<MeshUV0>(piece)
                    ? EntityManager.GetBuffer<MeshUV0>(piece, true) : default;
                DynamicBuffer<MeshNormal> normalBuffer = EntityManager.HasBuffer<MeshNormal>(piece)
                    ? EntityManager.GetBuffer<MeshNormal>(piece, true) : default;
                DynamicBuffer<MeshTangent> tangentBuffer = EntityManager.HasBuffer<MeshTangent>(piece)
                    ? EntityManager.GetBuffer<MeshTangent>(piece, true) : default;

                if ((vertexBuffer.Length > 0) && (indexBuffer.Length > 0) && (materialBuffer.Length > 0))
                {
                    result = new PieceGeometry
                    {
                        Vertices = new float3[vertexBuffer.Length],
                        Uvs = new float2[vertexBuffer.Length],
                        Indices = new int[indexBuffer.Length],
                        RangeStartVertex = new int[materialBuffer.Length],
                        RangeVertexCount = new int[materialBuffer.Length],
                        RangeStartIndex = new int[materialBuffer.Length],
                        RangeIndexCount = new int[materialBuffer.Length],
                        RangeMaterial = new string[materialBuffer.Length]
                    };

                    for (int i = 0; i < vertexBuffer.Length; i++) result.Vertices[i] = vertexBuffer[i].m_Vertex;
                    bool hasUv = uvBuffer.IsCreated && (uvBuffer.Length == vertexBuffer.Length);
                    for (int i = 0; i < vertexBuffer.Length; i++) result.Uvs[i] = hasUv ? uvBuffer[i].m_Uv : default;
                    for (int i = 0; i < indexBuffer.Length; i++) result.Indices[i] = indexBuffer[i].m_Index;

                    // The authored shading frame, read only when it covers every vertex: a short
                    // buffer would silently shade part of the piece from another vertex's normal,
                    // which is worse than the face rebuild it would replace.
                    //
                    if (normalBuffer.IsCreated && (normalBuffer.Length == vertexBuffer.Length))
                    {
                        result.Normals = new float3[vertexBuffer.Length];
                        for (int i = 0; i < vertexBuffer.Length; i++) result.Normals[i] = normalBuffer[i].m_Normal;
                    }

                    if (tangentBuffer.IsCreated && (tangentBuffer.Length == vertexBuffer.Length))
                    {
                        result.Tangents = new float4[vertexBuffer.Length];
                        for (int i = 0; i < vertexBuffer.Length; i++) result.Tangents[i] = tangentBuffer[i].m_Tangent;
                    }

                    // The bundle material ids come from the same cached read the previous
                    // assembly used — ordinal k of the MeshMaterial buffer is ordinal k of the
                    // read primitives, both being the surface-slot order (§3.4/§9.2).
                    //
                    for (int k = 0; k < materialBuffer.Length; k++)
                    {
                        MeshMaterial range = materialBuffer[k];
                        result.RangeStartVertex[k] = range.m_StartVertex;
                        result.RangeVertexCount[k] = range.m_VertexCount;
                        result.RangeStartIndex[k] = range.m_StartIndex;
                        result.RangeIndexCount[k] = range.m_IndexCount;
                        result.RangeMaterial[k] = ((read != null) && (k < read.Primitives.Count)
                                                   && !string.IsNullOrEmpty(read.Primitives[k].MaterialId))
                            ? read.Primitives[k].MaterialId
                            : $"piece_{piece.Index}_mat{k}";
                    }
                }
            }

            // No buffers, or empty ones — and that is not "this piece has no geometry". The
            // buffers are a RENDER CACHE (`BatchMeshSystem.LoadMeshes`): the game fills them on
            // demand for compositions it is batching, under a memory budget, a cap of 30
            // concurrent async loads, and eviction. So which pieces carry them is a property of
            // the session, not of the asset — measured on 2026-08-11, the elevated tram deck was
            // absent in two rips and present in a third, with `Tram Track Piece 3` moving in and
            // out of the DROPPED list to match, 13% up-facing area against 102% (§42.4-42.6).
            //
            // The asset itself is always readable, and the object path has read it that way
            // since Phase 3 — `MeshRipSystem.TryReadGeometryAsset` obtains the geometry asset,
            // reads it, and releases it. `read` above is that reader's output, already fetched
            // for the material ids, so the fix is to build from it rather than give up.
            if ((result == null) && (read != null) && (read.Vertices != null) && (read.Indices != null)
                && (read.Vertices.Length > 0) && (read.Indices.Length > 0))
            {
                result = new PieceGeometry
                {
                    Vertices = read.Vertices,
                    Uvs = new float2[read.Vertices.Length],
                    Indices = read.Indices,
                    Normals = (read.Normals != null) && (read.Normals.Length == read.Vertices.Length)
                        ? read.Normals : null,
                    Tangents = (read.Tangents != null) && (read.Tangents.Length == read.Vertices.Length)
                        ? read.Tangents : null
                };
                if ((read.Uvs != null) && (read.Uvs.Length == read.Vertices.Length)) result.Uvs = read.Uvs;

                // A primitive is a material range, in the same surface-slot order the buffers use.
                int ranges = math.max(read.Primitives.Count, 1);
                result.RangeStartVertex = new int[ranges];
                result.RangeVertexCount = new int[ranges];
                result.RangeStartIndex = new int[ranges];
                result.RangeIndexCount = new int[ranges];
                result.RangeMaterial = new string[ranges];
                if (read.Primitives.Count == 0)
                {
                    result.RangeStartVertex[0] = 0;
                    result.RangeVertexCount[0] = read.Vertices.Length;
                    result.RangeStartIndex[0] = 0;
                    result.RangeIndexCount[0] = read.Indices.Length;
                    result.RangeMaterial[0] = $"piece_{piece.Index}_mat0";
                }
                else
                {
                    for (int k = 0; k < read.Primitives.Count; k++)
                    {
                        MeshPrimitive primitive = read.Primitives[k];
                        result.RangeStartVertex[k] = primitive.StartVertex;
                        result.RangeVertexCount[k] = primitive.VertexCount;
                        result.RangeStartIndex[k] = primitive.StartIndex;
                        result.RangeIndexCount[k] = primitive.IndexCount;
                        result.RangeMaterial[k] = string.IsNullOrEmpty(primitive.MaterialId)
                            ? $"piece_{piece.Index}_mat{k}" : primitive.MaterialId;
                    }
                }

                _piecesFromAsset++;
                if (_pieceFromAssetNames.Count < 64)
                {
                    _pieceFromAssetNames.Add(Instance.Prefab.TryGetPrefab(piece, out PrefabBase assetPiece)
                                             && (assetPiece != null)
                        ? assetPiece.name : $"<entity {piece.Index}>");
                }
            }

            _pieceGeometry[piece] = result;
            return result;
        }
    }
}
