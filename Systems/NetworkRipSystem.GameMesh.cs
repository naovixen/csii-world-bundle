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
    /// （遊戲自身的組成網格產生流程，移植後於 CPU 上求值。）<br/><br/>
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
    /// V-per-metre density, exactly the quantity the game stores per vertex.<br/>
    /// （<b>何以移植而非重建。</b>逐部件重推導節點映射的每次嘗試——半幅座標系、楔形填補、
    /// 扇形面——皆僅漸近而從未達到遊戲的行為：路口內部依然開洞，其色調亦與各臂不符。
    /// 遊戲的做法在結構上不同：<c>GenerateCompositionMesh</c> 將每個頂點烘焙至<i>參數空間</i>，
    /// 頂點著色器再依 <c>BuildEdgeMatrix</c> 交付的兩條三次貝茲曲線混合求出世界位置。
    /// 本檔案即該管線的移植：曲線、映射與求值皆依遊戲自身的程式碼。）<br/><br/>
    /// Known deviations, deliberate and counted where they can fire: wind sway (not shape),
    /// terrain-conforming flags (<c>LowerBottomToTerrain</c>/<c>RaiseTopToTerrain</c> — a
    /// consumer rule once the terrain layer exists), rebuilt normals (contract §5.1), and the
    /// roundabout three-matrix blend weights (0x80 colour bits — shader-side, still
    /// approximated by the nearer pair).
    /// （已知且刻意的偏離：風力搖曳、順地形旗標、重建法線，與環形交叉口的三矩陣混合權重。）
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// The eight curves and arc tables one composition instance is evaluated against — the
        /// CPU form of <c>CompositionParameters</c>' matrices.
        /// （單一組成實例據以求值的八條曲線與弧長表——<c>CompositionParameters</c> 矩陣的 CPU 形式。）
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
        /// （單一部件的製作幾何，自遊戲產生網格所讀的同一批 ECS 緩衝區讀取一次；
        /// 各材質範圍的束識別碼經由與建築相同的讀取器解析，使表面進入材質表。）
        /// </summary>
        private class PieceGeometry
        {
            public float3[] Vertices;
            public float2[] Uvs;
            public int[] Indices;
            public int[] RangeStartVertex, RangeVertexCount, RangeStartIndex, RangeIndexCount;
            public string[] RangeMaterial;
        }

        private readonly Dictionary<Entity, PieceGeometry> m_PieceGeometry = new();

        // ---- CalculateMappingOffsets / Edge / Node / Orphan parameters, ported --------------
        // （CalculateMappingOffsets 與邊／節點／孤立節點參數的移植。）

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

        /// <summary>`CalculateEdgeParameters`, curves kept absolute.（`CalculateEdgeParameters`，曲線保持絕對座標。）</summary>
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

        /// <summary>`CalculateNodeParameters`, both branches.（`CalculateNodeParameters`，兩個分支皆移植。）</summary>
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

        /// <summary>`CalculateOrphanParameters`.（`CalculateOrphanParameters` 的移植。）</summary>
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

        /// <summary>The Bernstein blend of one arc table — the `w` column of `BuildEdgeMatrix`.（單一弧長表的 Bernstein 混合——`BuildEdgeMatrix` 的 `w` 行。）</summary>
        private static float EvalMap(float4 map, float t)
        {
            float u = 1f - t;
            return (u * u * u * map.x) + (3f * u * u * t * map.y)
                 + (3f * u * t * t * map.z) + (t * t * t * map.w);
        }

        // ---- The generator's vertex path, ported and evaluated ------------------------------

        /// <summary>
        /// Generate one composition instance exactly as `GenerateCompositionMesh` maps it, into
        /// the shared assembly lists.（將單一組成實例完全依 `GenerateCompositionMesh` 的映射產生，
        /// 寫入共用的組裝列表。）
        /// </summary>
        /// <summary>
        /// One node side of an edge: parameters from its own geometry, mesh from its composition.
        /// A rotated node composition points at its mirror twin's mesh; it is assembled with this
        /// node's parameters and counted, an approximation on asymmetric roads.
        /// （邊的單側節點：參數取自其自身幾何，網格取自其組成。旋轉的節點組成指向其鏡像雙生的
        /// 網格；以本節點的參數組裝並計數，於不對稱道路屬近似。）
        /// </summary>
        private void BuildNodeSide(Entity composition, EdgeNodeGeometry geometry, float3 center,
                                   string label, Dictionary<Entity, MeshAsset> cache,
                                   List<float3> vertices, List<float2> uvs, List<int> indices,
                                   List<MeshPrimitive> primitives, System.Text.StringBuilder report)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return;
            if (EntityManager.HasComponent<NetCompositionMeshRef>(composition)
                && EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Rotate) _rotatedNodeParts++;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            CompositionCurves curves = NodeCurves(geometry, data);
            BuildGameComposition(composition, curves, center, label, cache,
                                 vertices, uvs, indices, primitives, report);
        }

        private void BuildGameComposition(Entity composition, in CompositionCurves p,
                                          float3 center, string label, Dictionary<Entity, MeshAsset> cache,
                                          List<float3> vertices, List<float2> uvs, List<int> indices,
                                          List<MeshPrimitive> primitives, System.Text.StringBuilder report)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return;
            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return;
            if (!EntityManager.HasComponent<NetCompositionData>(composition)) return;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            bool isNode = (data.m_Flags.m_General & CompositionFlags.General.Node) != 0;
            bool roundabout = (data.m_Flags.m_General & CompositionFlags.General.Roundabout) != 0;
            if (roundabout) _roundaboutParts++;

            float middleOffset = data.m_MiddleOffset;
            float num7 = (data.m_Width * 0.5f) + middleOffset;   // left half width
            float num8 = (data.m_Width * 0.5f) - middleOffset;   // right half width

            DynamicBuffer<NetCompositionPiece> buffer = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);
            NetCompositionPiece[] pieces = new NetCompositionPiece[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) pieces[i] = buffer[i];

            foreach (NetCompositionPiece piece in pieces)
            {
                PieceGeometry source = GetPieceGeometry(piece.m_Piece, cache);
                if (source == null) continue;

                bool invert = (piece.m_SectionFlags & NetSectionFlags.Invert) != 0;
                bool flipMesh = (piece.m_SectionFlags & NetSectionFlags.FlipMesh) != 0;
                bool median = (piece.m_SectionFlags & NetSectionFlags.Median) != 0;
                bool rightSide = (piece.m_SectionFlags & NetSectionFlags.Right) != 0;
                bool halfLength = (piece.m_SectionFlags & NetSectionFlags.HalfLength) != 0;
                bool preserveShape = (piece.m_PieceFlags & NetPieceFlags.PreserveShape) != 0;
                bool disableTiling = (piece.m_PieceFlags & NetPieceFlags.DisableTiling) != 0;
                bool frontCut = (isNode && !roundabout && !preserveShape) || halfLength;
                bool backCut = (piece.m_PieceFlags & NetPieceFlags.SkipBottomHalf) != 0;
                bool zeroWidth = piece.m_Size.x == 0f;
                if (preserveShape) _railPieces++;
                if (backCut) _skipBottomPieces++;

                // The game's flip select — `select(float4(-1,1,-1,1), float4(1,1,1,-1),
                // bool4(Invert, false, FlipMesh, Invert != FlipMesh))`: authored pieces are
                // x- and z-negated by default; Invert restores x, FlipMesh restores z. The `w`
                // term flips tangent handedness, which the rebuilt normals make moot.
                // （遊戲的翻轉選擇：製作部件預設 x、z 皆取負；Invert 還原 x，FlipMesh 還原 z。
                //   `w` 項翻轉切線手性，因法線重建而無關。）
                float sx = invert ? 1f : -1f;
                float sz = flipMesh ? 1f : -1f;

                float3 offset = piece.m_Offset;
                float2 scale4 = new(1f / data.m_Width, 1f / (piece.m_Size.z * 0.5f));
                float2 scale5 = new(1f / num7, 1f / (piece.m_Size.z * 0.5f));
                float2 scale6 = new(1f / num8, 1f / (piece.m_Size.z * 0.5f));
                float2 bias7 = new(0.5f, 1f);
                float2 bias8 = new(1f - (middleOffset / num7), 1f);
                float2 bias9 = new((0f - middleOffset) / num8, 1f);
                float crossZ = (piece.m_Offset.x / data.m_Width) + 0.5f;
                float crossZ2 = 1f + ((piece.m_Offset.x - middleOffset) / num7);
                float crossZ3 = (piece.m_Offset.x - middleOffset) / num8;

                if (isNode && preserveShape)
                {
                    float num10 = 0.5f * piece.m_Size.z / data.m_Width;
                    scale4.y *= num10;
                    bias7.y *= num10;
                    crossZ = 0.5f;
                }
                else if (roundabout)
                {
                    crossZ2 = 0f;
                    crossZ3 = 1f;
                }
                else if (halfLength)
                {
                    sz *= 2f;
                    offset.z += 0.5f * piece.m_Size.z;
                }

                for (int r = 0; r < source.RangeMaterial.Length; r++)
                {
                    int startVertex = source.RangeStartVertex[r];
                    int vertexCount = source.RangeVertexCount[r];
                    int startIndex = source.RangeStartIndex[r];
                    int indexCount = source.RangeIndexCount[r];

                    // The game's per-range V density: |ΔV| over |Δz| across triangles whose V is
                    // wholly non-negative; negative-V triangles instead record the rebase ceiling.
                    // （遊戲的逐範圍 V 密度：於 V 全為非負的三角形上取 |ΔV|/|Δz|；
                    //   含負 V 者改記錄重定基準的上限。）
                    bool tiledV = !disableTiling;
                    float2 accum = default;
                    float rebase = 0f;
                    int3 order = math.select(new int3(0, 1, 2), new int3(2, 1, 0), invert != flipMesh);
                    for (int t = 0; t < indexCount; t += 3)
                    {
                        int ia = source.Indices[startIndex + t + order.x];
                        int ib = source.Indices[startIndex + t + order.y];
                        int ic = source.Indices[startIndex + t + order.z];
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
                    float density = (accum.y > 1e-6f) ? (accum.x / accum.y) : 0f;
                    rebase = tiledV ? 0f : math.ceil(rebase);

                    // Vertex filter and remap, as the generator's `nativeArray4`.
                    // （頂點過濾與重映射，即產生器的 `nativeArray4`。）
                    int[] remap = new int[vertexCount];
                    int baseVertex = vertices.Count;
                    int kept = 0;
                    for (int i = 0; i < vertexCount; i++)
                    {
                        float rawZ = source.Vertices[startVertex + i].z;
                        if ((frontCut && (rawZ < -0.01f)) || (backCut && (rawZ > 0.01f)))
                        {
                            remap[i] = -1;
                            continue;
                        }
                        remap[i] = baseVertex + kept++;

                        float3 vertex = source.Vertices[startVertex + i] * new float3(sx, 1f, sz) + offset;
                        float2 uv = source.Uvs[startVertex + i];
                        int2 pair;
                        float xParam, zParam;
                        bool halved = false, leftHalf = false;

                        if (isNode)
                        {
                            if (preserveShape)
                            {
                                pair = new int2(6, 7);
                                xParam = (vertex.x * scale4.x) + bias7.x;
                                zParam = (vertex.z * scale4.y) + bias7.y;
                            }
                            else if (median)
                            {
                                float dx = vertex.x - piece.m_Offset.x;
                                if (zeroWidth) vertex.x = piece.m_Offset.x;
                                if (zeroWidth || (math.abs(dx) < 0.01f))
                                {
                                    pair = roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                                      : new int2(4, 4);
                                    xParam = 0f;
                                    zParam = (vertex.z * scale4.y) + bias7.y;
                                    if (roundabout && (vertex.z >= 0f)) zParam -= 1f;
                                }
                                else if (dx > 0f)
                                {
                                    pair = roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                                      : new int2(1, 3);
                                    xParam = (vertex.x * scale6.x) + bias9.x;
                                    zParam = (vertex.z * scale6.y) + bias9.y;
                                    if (roundabout && (vertex.z >= 0f)) zParam -= 1f;
                                    halved = true; leftHalf = false;
                                }
                                else
                                {
                                    pair = roundabout ? math.select(new int2(0, 4), new int2(1, 5), vertex.z >= 0f)
                                                      : new int2(0, 2);
                                    xParam = (vertex.x * scale5.x) + bias8.x;
                                    zParam = (vertex.z * scale5.y) + bias8.y;
                                    if (roundabout && (vertex.z >= 0f)) zParam -= 1f;
                                    halved = true; leftHalf = true;
                                }
                            }
                            else if (rightSide)
                            {
                                pair = roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                                  : new int2(1, 3);
                                xParam = (vertex.x * scale6.x) + bias9.x;
                                zParam = (vertex.z * scale6.y) + bias9.y;
                                if (roundabout && (vertex.z >= 0f)) zParam -= 1f;
                                halved = true; leftHalf = false;
                            }
                            else
                            {
                                pair = roundabout ? math.select(new int2(0, 4), new int2(1, 5), vertex.z >= 0f)
                                                  : new int2(0, 2);
                                xParam = (vertex.x * scale5.x) + bias8.x;
                                zParam = (vertex.z * scale5.y) + bias8.y;
                                if (roundabout && (vertex.z >= 0f)) zParam -= 1f;
                                halved = true; leftHalf = true;
                            }
                        }
                        else
                        {
                            pair = math.select(new int2(0, 2), new int2(1, 3), vertex.z >= 0f);
                            xParam = (vertex.x * scale4.x) + bias7.x;
                            zParam = (vertex.z * scale4.y) + bias7.y;
                            if (vertex.z >= 0f) zParam -= 1f;
                        }

                        xParam = math.saturate(xParam);
                        zParam = math.saturate(zParam);

                        // The across-width sync the game hands the shader (§5.1); measured per
                        // half from the outer edge, which is the coordinate xParam runs in for
                        // the halved branches. The left half runs outer→inner as xParam grows;
                        // the right half runs inner→outer, so it is flipped through the remap.
                        // （遊戲交予著色器的橫向同步（§5.1）；以半幅為單位自外緣量測，
                        //   與 halved 分支的 xParam 座標一致。左半幅隨 xParam 增大由外而內；
                        //   右半幅由內而外，故經翻轉後重映射。）
                        if (halved && isNode)
                        {
                            xParam = leftHalf
                                ? p.Sync.Apply(xParam, true)
                                : 1f - p.Sync.Apply(1f - xParam, false);
                        }

                        float3 a = MathUtils.Position(p.Curves[pair.x], zParam);
                        float3 b = MathUtils.Position(p.Curves[pair.y], zParam);
                        float3 world = math.lerp(a, b, xParam);
                        world.y += vertex.y;
                        vertices.Add(world - center);

                        float metres = math.lerp(EvalMap(p.Maps[pair.x], zParam),
                                                 EvalMap(p.Maps[pair.y], zParam), xParam);
                        float v2 = (tiledV && (uv.y >= 0f)) ? (density * metres) : (uv.y - rebase);
                        uvs.Add(new float2(uv.x, v2));
                    }

                    // Triangles: the game's reversed read under Invert XOR FlipMesh, then only
                    // those whose three corners survived the cut.
                    // （三角形：Invert 互斥或 FlipMesh 時反向讀取，僅保留三角皆存活者。）
                    int primitiveStart = indices.Count;
                    for (int t = 0; t < indexCount; t += 3)
                    {
                        int ia = source.Indices[startIndex + t + order.x] - startVertex;
                        int ib = source.Indices[startIndex + t + order.y] - startVertex;
                        int ic = source.Indices[startIndex + t + order.z] - startVertex;
                        if ((ia < 0) || (ib < 0) || (ic < 0)
                            || (ia >= vertexCount) || (ib >= vertexCount) || (ic >= vertexCount)) continue;
                        if ((remap[ia] < 0) || (remap[ib] < 0) || (remap[ic] < 0)) continue;

                        indices.Add(remap[ia]);
                        indices.Add(remap[ib]);
                        indices.Add(remap[ic]);
                    }

                    if (indices.Count > primitiveStart)
                    {
                        primitives.Add(new MeshPrimitive
                        {
                            StartIndex = primitiveStart,
                            IndexCount = indices.Count - primitiveStart,
                            StartVertex = baseVertex,
                            VertexCount = kept,
                            MaterialIndex = primitives.Count,
                            MaterialId = source.RangeMaterial[r]
                        });
                    }
                }
            }
        }

        /// <summary>
        /// Read one piece's ECS geometry buffers — the exact arrays `GenerateCompositionMesh`
        /// reads — and resolve each material range's bundle id through the cached piece read.
        /// （讀取單一部件的 ECS 幾何緩衝區——`GenerateCompositionMesh` 所讀的同一批陣列——
        /// 並經由快取的部件讀取解析各材質範圍的束識別碼。）
        /// </summary>
        private PieceGeometry GetPieceGeometry(Entity piece, Dictionary<Entity, MeshAsset> cache)
        {
            if (piece == Entity.Null) return null;
            if (m_PieceGeometry.TryGetValue(piece, out PieceGeometry cached)) return cached;

            PieceGeometry result = null;

            if (EntityManager.HasBuffer<MeshVertex>(piece) && EntityManager.HasBuffer<MeshIndex>(piece)
                && EntityManager.HasBuffer<MeshMaterial>(piece))
            {
                DynamicBuffer<MeshVertex> vertexBuffer = EntityManager.GetBuffer<MeshVertex>(piece, true);
                DynamicBuffer<MeshIndex> indexBuffer = EntityManager.GetBuffer<MeshIndex>(piece, true);
                DynamicBuffer<MeshMaterial> materialBuffer = EntityManager.GetBuffer<MeshMaterial>(piece, true);
                DynamicBuffer<MeshUV0> uvBuffer = EntityManager.HasBuffer<MeshUV0>(piece)
                    ? EntityManager.GetBuffer<MeshUV0>(piece, true) : default;

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

                    // The bundle material ids come from the same cached read the previous
                    // assembly used — ordinal k of the MeshMaterial buffer is ordinal k of the
                    // read primitives, both being the surface-slot order (§3.4/§9.2).
                    // （束材質識別碼取自先前組裝所用的同一份快取讀取——`MeshMaterial` 緩衝區的
                    //   第 k 項即讀取結果的第 k 個 primitive，兩者皆為表面槽位順序（§3.4/§9.2）。）
                    if (!cache.TryGetValue(piece, out MeshAsset read))
                    {
                        string name = Instance.Name.GetPrefabName(piece);
                        read = Instance.MeshRip.ReadMesh(piece, SanitizeId(name), name);
                        cache[piece] = read;
                    }

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

            m_PieceGeometry[piece] = result;
            return result;
        }
    }
}
