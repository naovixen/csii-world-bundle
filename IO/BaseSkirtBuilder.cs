using Carto.Domain;
using System;
using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Generates the foundation skirt CS2 draws under a building — the extra batch past a mesh's
    /// last sub-mesh when its <c>MeshData</c> carries <c>MeshFlags.Base</c>.
    /// （產生 CS2 於建築下方繪製的基座裙板——當網格的 <c>MeshData</c> 帶有 <c>MeshFlags.Base</c> 時，
    /// 於最後一個子網格之後多繪製的那一批。）<br/><br/>
    /// <b>What the game builds.</b> <c>BatchMeshHelpers.GenerateObjectMesh</c> walks the mesh's own
    /// triangles and collects every edge lying in a ±0.01 m band at the base level whose triangle
    /// continues <i>upward</i> — the silhouette of the walls where they meet the ground — excluding
    /// edges that also bound downward-continuing geometry (interior floor seams). Each silhouette
    /// point keeps its vertex normal's horizontal direction and an accumulated perimeter distance,
    /// and the batch is a quad strip: a top ring on the base level (UV v=1) and a bottom ring one
    /// metre below it (UV v=0), with u the perimeter distance in metres. At draw time the vertex
    /// shader drops the v=0 ring to the terrain surface, which is what fills the gap between a
    /// building's flat floor and sloping ground.<br/>
    /// （<b>遊戲所建者。</b><c>GenerateObjectMesh</c> 走訪網格自身的三角形，收集位於基準面 ±0.01 公尺
    /// 帶內、且其三角形<i>向上</i>延續的每條邊——即牆體與地面相接處的輪廓——並排除同時鄰接向下延續
    /// 幾何的邊（室內地板接縫）。每個輪廓點保留其頂點法線的水平方向與累計周長距離；
    /// 該批次為四邊形帶：基準面上的上環（UV v=1）與其下一公尺的下環（UV v=0），u 為以公尺計的周長。
    /// 繪製時頂點著色器將 v=0 環下拉至地形表面，以此填補建築平坦地板與傾斜地面之間的縫隙。）<br/><br/>
    /// This is a faithful managed port of that generator, run once per prefab on the writer task.
    /// The linear triangle scan replaces the game's BVH walk — the BVH only accelerates the same
    /// visit, and a rip does this once per prefab, not per batch rebuild.<br/>
    /// （此為該產生器忠實的受控移植，於寫出工作中每個預製模板執行一次。線性三角形掃描取代遊戲的
    /// BVH 走訪——BVH 僅加速同一走訪，且擷取每個預製模板僅執行一次，而非每次批次重建。）
    /// </summary>
    public static class BaseSkirtBuilder
    {
        /// <summary>How far below the base level the authored band reaches, matching the game's generated batch.（帶狀幾何自基準面向下延伸的距離，與遊戲產生的批次一致。）</summary>
        public const float BandDepth = 1f;

        private struct BasePoint
        {
            public float2 Position;
            public float2 Direction;
            public float Distance;
        }

        private struct BaseLine
        {
            public int Start;
            public int End;
        }

        /// <summary>
        /// Build the skirt band for one source mesh, or <c>null</c> when the mesh presents no
        /// silhouette at the base level.
        /// （為單一來源網格建構裙板帶；若該網格於基準面無輪廓則回傳 <c>null</c>。）
        /// </summary>
        /// <param name="source">The part's full geometry, in prefab-local space.（部件的完整幾何，預製模板區域座標。）</param>
        /// <param name="baseOffset">The base level: <c>0</c>, or the mesh bounds' minimum Y under <c>MeshFlags.MinBounds</c>.（基準面：<c>0</c>，或 <c>MeshFlags.MinBounds</c> 下的網格邊界最小 Y。）</param>
        /// <param name="assetId">The asset id the band is written under.（帶狀幾何寫出時使用的資產 ID。）</param>
        /// <param name="sourceName">The source prefab name, for diagnostics.（來源預製模板名稱，供診斷。）</param>
        /// <param name="materialId">The bundle material id of the base surface.（基座表面的束材質 ID。）</param>
        /// <returns>The band mesh, or <c>null</c>.（帶狀網格，或 <c>null</c>。）</returns>
        public static MeshAsset Build(MeshAsset source, float baseOffset, string assetId, string sourceName, string materialId)
        {
            if ((source?.Vertices == null) || (source.Indices == null) || (source.Indices.Length < 3)) return null;

            List<BasePoint> points = new();
            List<BaseLine> lines = new();
            CollectBaseLines(source, baseOffset, points, lines);
            if (lines.Count == 0) return null;

            ResolveDistances(points, lines);
            if (lines.Count == 0) return null;

            MeshAsset band = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[points.Count * 2],
                Normals = new float3[points.Count * 2],
                Tangents = new float4[points.Count * 2],
                Uvs = new float2[points.Count * 2],
                Indices = new int[lines.Count * 6]
            };

            for (int i = 0; i < points.Count; i++)
            {
                BasePoint point = points[i];
                float3 normal = math.normalizesafe(new float3(point.Direction.x, 0f, point.Direction.y), new float3(0f, 0f, 1f));
                float4 tangent = new(normal.z, 0f, -normal.x, 1f);

                band.Vertices[i * 2] = new float3(point.Position.x, baseOffset, point.Position.y);
                band.Vertices[(i * 2) + 1] = new float3(point.Position.x, baseOffset - BandDepth, point.Position.y);
                band.Normals[i * 2] = normal;
                band.Normals[(i * 2) + 1] = normal;
                band.Tangents[i * 2] = tangent;
                band.Tangents[(i * 2) + 1] = tangent;
                band.Uvs[i * 2] = new float2(point.Distance, 1f);
                band.Uvs[(i * 2) + 1] = new float2(point.Distance, 0f);
            }

            // The game's own winding (`AddBaseVertices`), kept verbatim so the band faces outward
            // under the same mirroring every other mesh goes through on write.
            // （沿用遊戲自身的繞序，使帶狀幾何在寫出時與其他網格經過相同鏡射後仍朝外。）
            int indexCount = 0;
            foreach (BaseLine line in lines)
            {
                int start = line.Start * 2;
                int end = line.End * 2;
                band.Indices[indexCount++] = start;
                band.Indices[indexCount++] = start + 1;
                band.Indices[indexCount++] = end;
                band.Indices[indexCount++] = end;
                band.Indices[indexCount++] = start + 1;
                band.Indices[indexCount++] = end + 1;
            }

            band.Primitives.Add(new MeshPrimitive
            {
                StartIndex = 0,
                IndexCount = band.Indices.Length,
                StartVertex = 0,
                VertexCount = band.Vertices.Length,
                MaterialIndex = 0,
                MaterialId = materialId
            });

            float3 min = band.Vertices[0], max = band.Vertices[0];
            foreach (float3 vertex in band.Vertices)
            {
                min = math.min(min, vertex);
                max = math.max(max, vertex);
            }
            band.BoundsMin = min;
            band.BoundsMax = max;

            return band;
        }

        /// <summary>
        /// The silhouette scan — <c>BatchMeshHelpers.AddBaseLines</c>, ported. Two passes: the first
        /// records edges whose triangle continues downward (interior seams to exclude), the second
        /// keeps edges whose triangle continues upward, deduplicating shared points and chaining
        /// them so the perimeter distance can accumulate.
        /// （輪廓掃描——移植自 <c>AddBaseLines</c>。兩回合：首回合記錄三角形向下延續的邊
        /// （應排除的室內接縫），次回合保留三角形向上延續的邊，去除共用點的重複並將其串鏈，
        /// 使周長距離得以累計。）
        /// </summary>
        private static void CollectBaseLines(MeshAsset source, float baseOffset, List<BasePoint> points, List<BaseLine> lines)
        {
            float2 band = new(baseOffset - 0.01f, baseOffset + 0.01f);
            HashSet<float4> excluded = new();
            Dictionary<float2, int> byPosition = new();

            bool hasNormals = (source.Normals != null) && (source.Normals.Length == source.Vertices.Length);

            for (int i = 0; (i + 2) < source.Indices.Length; i += 3)
            {
                int3 tri = new(source.Indices[i], source.Indices[i + 1], source.Indices[i + 2]);
                float3 a = source.Vertices[tri.x], b = source.Vertices[tri.y], c = source.Vertices[tri.z];
                float3 y = new(a.y, b.y, c.y);

                bool3 inBand = (y > band.x) & (y < band.y);
                bool3 below = (y < band.y) & inBand.yzx & inBand.zxy;

                if (below.x) excluded.Add(new float4(b.xz, c.xz));
                if (below.y) excluded.Add(new float4(c.xz, a.xz));
                if (below.z) excluded.Add(new float4(a.xz, b.xz));
            }

            for (int i = 0; (i + 2) < source.Indices.Length; i += 3)
            {
                int3 tri = new(source.Indices[i], source.Indices[i + 1], source.Indices[i + 2]);
                float3 a = source.Vertices[tri.x], b = source.Vertices[tri.y], c = source.Vertices[tri.z];
                float3 y = new(a.y, b.y, c.y);

                bool3 inBand = (y > band.x) & (y < band.y);
                bool3 above = (y >= band.y) & inBand.yzx & inBand.zxy;
                if (!math.any(above)) continue;

                BasePoint start, end;
                if (above.x)
                {
                    start = MakePoint(source, tri.y, hasNormals, a, b, c);
                    end = MakePoint(source, tri.z, hasNormals, a, b, c);
                }
                else if (above.y)
                {
                    start = MakePoint(source, tri.z, hasNormals, a, b, c);
                    end = MakePoint(source, tri.x, hasNormals, a, b, c);
                }
                else
                {
                    start = MakePoint(source, tri.x, hasNormals, a, b, c);
                    end = MakePoint(source, tri.y, hasNormals, a, b, c);
                }

                if (excluded.Contains(new float4(start.Position, end.Position))
                    || excluded.Contains(new float4(end.Position, start.Position))) continue;

                start.Distance = -1f;
                end.Distance = -1f;

                // The map's value encodes two things at once, exactly as the game's does: the point
                // index, and — negated to `-1 - index` — whether a line already arrives at it. A
                // point where the silhouette turns a corner with a different normal is split so the
                // band gets a hard edge there.
                // （映射的值一如遊戲同時編碼兩件事：點的索引，以及（取 `-1 - index` 負值）是否已有
                /// 線段抵達該點。輪廓於法線不同處轉角的點會被拆分，使帶狀幾何在該處保有硬邊。）
                int startIndex;
                if (byPosition.TryGetValue(start.Position, out int found))
                {
                    startIndex = (found < 0) ? (-1 - found) : found;
                    if (!points[startIndex].Direction.Equals(start.Direction))
                    {
                        startIndex = points.Count;
                        points.Add(start);
                    }
                }
                else
                {
                    startIndex = points.Count;
                    byPosition.Add(start.Position, startIndex);
                    points.Add(start);
                }

                int endIndex;
                if (byPosition.TryGetValue(end.Position, out int foundEnd))
                {
                    endIndex = (foundEnd < 0) ? (-1 - foundEnd) : foundEnd;
                    if (!points[endIndex].Direction.Equals(end.Direction))
                    {
                        endIndex = points.Count;
                        points.Add(end);
                    }
                    byPosition[end.Position] = -1 - endIndex;
                }
                else
                {
                    endIndex = points.Count;
                    byPosition.Add(end.Position, -1 - endIndex);
                    points.Add(end);
                }

                lines.Add(new BaseLine { Start = startIndex, End = endIndex });
            }
        }

        private static BasePoint MakePoint(MeshAsset source, int vertexIndex, bool hasNormals, float3 a, float3 b, float3 c)
        {
            float2 direction;
            if (hasNormals)
            {
                direction = source.Normals[vertexIndex].xz;
            }
            else
            {
                // No authored normals: the face normal's horizontal part stands in, which only
                // costs the smoothing the vertex normal would carry.
                // （無製作法線時以面法線的水平分量代替，僅損失頂點法線本應帶有的平滑。）
                float3 face = math.cross(b - a, c - a);
                direction = math.normalizesafe(face.xz, new float2(0f, 1f));
            }

            return new BasePoint { Position = source.Vertices[vertexIndex].xz, Direction = direction };
        }

        /// <summary>
        /// Accumulate perimeter distances along the silhouette chains, to the same outcome as the
        /// game's backward walk: u runs continuously along each connected run of edges, starts at 0
        /// on a chain's head, and a closed loop's seam point is split so the closing quad carries
        /// the full perimeter on one side and 0 on the other instead of smearing between them.
        /// （沿輪廓鏈累計周長距離，結果與遊戲的回溯走訪相同：u 沿每段相連的邊連續遞增、
        /// 於鏈首為 0；封閉迴圈的接縫點會被拆分，使收尾四邊形一側帶全周長、另一側帶 0，
        /// 而非在兩者之間拖污。）
        /// </summary>
        private static void ResolveDistances(List<BasePoint> points, List<BaseLine> lines)
        {
            // Forward adjacency and incoming counts, by point index.
            // （以點索引建立前向鄰接與入邊計數。）
            int[] outgoing = new int[points.Count];
            int[] incoming = new int[points.Count];
            for (int i = 0; i < points.Count; i++) outgoing[i] = -1;
            foreach (BaseLine line in lines)
            {
                if (outgoing[line.Start] < 0) outgoing[line.Start] = line.End;
                incoming[line.End]++;
            }

            // Chains first: walk forward from every head (a point no line arrives at).
            // （先處理開放鏈：自每個鏈首（無線段抵達的點）向前走訪。）
            for (int i = 0; i < points.Count; i++)
            {
                if ((incoming[i] != 0) || (outgoing[i] < 0)) continue;
                WalkChain(points, outgoing, i);
            }

            // Whatever still has no distance sits on a closed loop: cut it at an arbitrary seam.
            // （仍無距離者位於封閉迴圈上：於任意接縫處切開。）
            for (int i = 0; i < points.Count; i++)
            {
                if ((points[i].Distance < 0f) && (outgoing[i] >= 0)) WalkChain(points, outgoing, i);
            }

            // The seam split: a line whose end already carries a smaller distance (the loop's
            // closing edge, or two chains merging) gets a duplicate end point carrying this
            // chain's own accumulated distance, exactly as the game splits it.
            // （接縫拆分：終點已帶較小距離的線段（迴圈的收尾邊，或兩鏈匯流處）
            /// 獲得一個帶本鏈累計距離的複製終點，與遊戲的拆分方式一致。）
            for (int k = 0; k < lines.Count; k++)
            {
                BaseLine line = lines[k];
                float start = points[line.Start].Distance;
                if (start < 0f) continue;

                float distance = start + math.distance(points[line.Start].Position, points[line.End].Position);
                if (math.abs(points[line.End].Distance - distance) > 1e-4f)
                {
                    BasePoint split = points[line.End];
                    split.Distance = distance;
                    line.End = points.Count;
                    points.Add(split);
                    lines[k] = line;
                }
            }

            // Guard the leftovers: a point on no resolvable chain keeps 0 so its UV stays valid.
            // （防護剩餘者：不在任何可解析鏈上的點以 0 為距離，使其 UV 有效。）
            for (int i = 0; i < points.Count; i++)
            {
                if (points[i].Distance < 0f)
                {
                    BasePoint point = points[i];
                    point.Distance = 0f;
                    points[i] = point;
                }
            }
        }

        /// <summary>
        /// Assign accumulated distances forward from <paramref name="head"/> until the chain ends,
        /// revisits a point, or loops back to its start.
        /// （自 <paramref name="head"/> 向前指派累計距離，直至鏈結束、重訪某點或繞回起點。）
        /// </summary>
        private static void WalkChain(List<BasePoint> points, int[] outgoing, int head)
        {
            BasePoint point = points[head];
            point.Distance = 0f;
            points[head] = point;

            int current = head;
            for (int guard = 0; guard <= points.Count; guard++)
            {
                int next = outgoing[current];
                if ((next < 0) || (next == head)) break;

                BasePoint nextPoint = points[next];
                if (nextPoint.Distance >= 0f) break;

                nextPoint.Distance = points[current].Distance
                                   + math.distance(points[current].Position, nextPoint.Position);
                points[next] = nextPoint;
                current = next;
            }
        }
    }
}
