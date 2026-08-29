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


        private readonly Dictionary<PieceKey, SectionProbe> _sectionProbes = new();

        private ushort[] _conformHeights;
        private int2 _conformResolution;
        private float3 _conformScale;
        private float3 _conformOffset;
        /// <summary>Which raster the conform samples: "fine cascade (1.750 m)" or "base heightmap (3.500 m)".</summary>
        private string _conformSource;


        // ---- CalculateMappingOffsets / Edge / Node / Orphan parameters, ported --------------
        //


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
