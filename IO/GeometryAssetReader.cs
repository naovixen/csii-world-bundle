using Carto.Domain;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using Unity.Collections;
using Unity.Mathematics;
using UnityEngine.Rendering;

namespace Carto.IO
{
    /// <summary>
    /// Reads full-detail geometry out of a <see cref="GeometryAsset"/>.
    /// （自 <see cref="GeometryAsset"/> 讀取完整細節的幾何。）<br/><br/>
    /// The ECS <c>MeshVertex</c>/<c>MeshIndex</c> buffers on a prefab entity hold only what the
    /// game needs on the CPU for raycasting and camera collision, which is a heavily reduced LOD —
    /// a building whose <c>MeshData.m_IndexCount</c> reports 194,724 indices exposes just 4,800
    /// through ECS. The authored mesh lives in the geometry asset behind
    /// <see cref="RenderPrefab.geometryAsset"/>, which also carries the sub-mesh descriptors that
    /// the ECS path lacks entirely.<br/>
    /// （預製模板實體上的 ECS 緩衝區僅存放遊戲於 CPU 端進行射線與碰撞所需者，屬大幅簡化的 LOD。
    /// 完整網格位於 <see cref="RenderPrefab.geometryAsset"/> 之後的幾何資產中，該處另有 ECS
    /// 路徑完全缺少的子網格描述。）<br/><br/>
    /// Unpacking is delegated to the game's own <c>Unpack</c> helpers, so vertex attribute formats
    /// (float32, float16, normalised integers, varying dimensions) are decoded exactly as the game
    /// decodes them rather than reimplemented here.<br/>
    /// （解包委由遊戲自身的 <c>Unpack</c> 輔助方法處理，使頂點屬性格式的解讀與遊戲完全一致。）
    /// </summary>
    public static class GeometryAssetReader
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// Describe the meshes a geometry asset contains, for diagnostics and for choosing one.
        /// （描述幾何資產所含的網格，供診斷與選取使用。）
        /// </summary>
        public struct MeshInfo
        {
            public int MeshIndex;
            public int VertexCount;
            public int IndexCount;
            public int SubMeshCount;
            public override string ToString() => $"mesh{MeshIndex}: {VertexCount} verts, {IndexCount / 3} tris, {SubMeshCount} submesh";
        }

        /// <summary>
        /// Enumerate the meshes held by a geometry asset.
        /// （列舉幾何資產所持有的網格。）<br/><br/>
        /// A geometry asset commonly holds several meshes — the LOD chain — so the caller picks
        /// rather than assuming index 0.<br/>
        /// （幾何資產通常持有多個網格（LOD 鏈），故由呼叫端選取而非假定索引 0。）
        /// </summary>
        /// <param name="asset">The geometry asset.（幾何資產。）</param>
        /// <param name="meshCount">The number of meshes reported by the render prefab.（繪製預製模板回報的網格數量。）</param>
        /// <returns>One entry per readable mesh.（每個可讀網格一筆。）</returns>
        public static List<MeshInfo> Describe(GeometryAsset asset, int meshCount)
        {
            List<MeshInfo> result = new();
            if (asset == null) return result;

            for (int i = 0; i < math.max(meshCount, 1); i++)
            {
                try
                {
                    int vertexCount = asset.GetVertexCount(i);
                    int indexCount = asset.GetIndicesCount(i);
                    if ((vertexCount <= 0) || (indexCount <= 0)) continue;

                    int subMeshCount;
                    try { subMeshCount = asset.GetSubMeshCount(i); }
                    catch { subMeshCount = 0; }

                    result.Add(new MeshInfo
                    {
                        MeshIndex = i,
                        VertexCount = vertexCount,
                        IndexCount = indexCount,
                        SubMeshCount = subMeshCount
                    });
                }
                catch (Exception)
                {
                    // A mesh index the asset does not actually hold; stop probing upward.
                    // （資產並未持有的網格索引，停止向上探測。）
                    break;
                }
            }

            return result;
        }

        /// <summary>
        /// Read <b>every</b> mesh a geometry asset holds into one <see cref="MeshAsset"/>, with
        /// each primitive's <see cref="MeshPrimitive.MaterialIndex"/> being the flat
        /// (mesh, sub-mesh) ordinal across the whole asset.<br/>
        /// （將幾何資產所持有的<b>每個</b>網格讀入單一 <see cref="MeshAsset"/>，
        /// 各 primitive 的 <see cref="MeshPrimitive.MaterialIndex"/> 為跨越整個資產的
        /// （網格、子網格）平坦序數。）<br/><br/>
        /// <b>Why all of them, and why that ordinal.</b> The meshes of one geometry asset are
        /// simultaneous parts, not an LOD chain — LODs are separate render prefabs behind
        /// <c>LodProperties</c>. The game draws one batch per flattened sub-mesh
        /// (<c>ManagedBatchSystem.CreateBatch</c> binds <c>GetSurfaceAsset(m_SubMeshIndex)</c>),
        /// and <c>RenderPrefab.ObtainMesh(materialIndex)</c> defines the mapping: walk the meshes
        /// in order, subtracting each one's sub-mesh count. Reading only the richest mesh — the
        /// previous behaviour — dropped every sibling part and, when the richest was not the
        /// first, resolved its surfaces at the wrong offset.<br/>
        /// （<b>為何全部讀取、為何採用該序數。</b>同一幾何資產中的網格是同時繪製的部件而非 LOD 鏈——
        /// LOD 是 <c>LodProperties</c> 之後的獨立繪製預製模板。遊戲為每個平坦化子網格各建一個批次，
        /// 且 <c>RenderPrefab.ObtainMesh(materialIndex)</c> 定義了對應方式：依序走訪各網格並
        /// 遞減其子網格數。先前「最豐富者為準」的行為會丟棄所有兄弟部件，
        /// 且當最豐富者非首個時，其表面將以錯誤的偏移解析。）
        /// </summary>
        /// <param name="asset">The geometry asset.（幾何資產。）</param>
        /// <param name="meshCount">The number of meshes the render prefab reports.（繪製預製模板回報的網格數量。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <param name="meshesMerged">Receives how many meshes contributed geometry.（接收貢獻了幾何的網格數量。）</param>
        /// <returns>The merged mesh, or <c>null</c>.（合併後的網格，或 <c>null</c>。）</returns>
        public static MeshAsset ReadAll(GeometryAsset asset, int meshCount, string assetId, string sourceName, out int meshesMerged)
        {
            meshesMerged = 0;
            if (asset == null) return null;

            List<MeshAsset> parts = new();
            int ordinal = 0;

            for (int meshIndex = 0; meshIndex < math.max(meshCount, 1); meshIndex++)
            {
                int subMeshCount;
                try { subMeshCount = math.max(1, asset.GetSubMeshCount(meshIndex)); }
                catch { subMeshCount = 1; }

                MeshAsset part = null;
                try
                {
                    part = Read(asset, meshIndex, assetId, sourceName);
                }
                catch (Exception ex)
                {
                    // The count comes from the asset's own header, so a throw here is geometry
                    // the bundle loses, not an over-probe — say so where the default log shows
                    // it, and keep counting ordinals so the surviving meshes still bind their
                    // surfaces at the right offsets.
                    // （數量取自資產自身的檔頭，故此處拋出即為束所丟失的幾何而非過度探測——
                    //   在預設記錄可見處言明，並繼續計數序數使倖存網格仍以正確偏移繫結表面。）
                    // The full exception, stack and all: `ArgumentOutOfRangeException` alone
                    // cannot say which accessor mis-sized (Highway_Lane_4_Mesh failed exactly
                    // this way in a full-city rip and the message was not enough to diagnose).
                    // （完整例外連同堆疊：僅憑 `ArgumentOutOfRangeException` 無法指出何者尺寸有誤
                    //   （全城擷取中 Highway_Lane_4_Mesh 正是如此失敗，訊息不足以診斷）。）
                    _log.Warn($"Mesh {meshIndex} of `{assetId}` unreadable: {ex}");
                }

                if (part != null)
                {
                    // Rebase this mesh's primitives onto the asset-wide ordinal, which is what
                    // `GetSurfaceAsset` is indexed by.
                    // （將此網格的 primitive 重定至資產層級的序數，該序數即 `GetSurfaceAsset` 的索引。）
                    foreach (MeshPrimitive primitive in part.Primitives)
                    {
                        primitive.MaterialIndex += ordinal;
                        primitive.MaterialId = $"{assetId}_mat{primitive.MaterialIndex}";
                    }

                    parts.Add(part);
                    meshesMerged++;
                }

                ordinal += subMeshCount;
            }

            if (parts.Count == 0) return null;
            if (parts.Count == 1) return parts[0];
            return Merge(parts, assetId, sourceName);
        }

        /// <summary>
        /// Concatenate several read meshes into one, offsetting indices and primitive ranges.
        /// （將數個已讀取的網格串接為一，並平移其索引與 primitive 範圍。）<br/><br/>
        /// An attribute survives the merge only when every part carries it — a mesh without
        /// normals stitched to one with them would otherwise get fabricated values.<br/>
        /// （唯有每個部件皆具備的屬性才能在合併後保留——否則缺少法線的網格
        /// 與具備法線者縫合時將被填入捏造的數值。）
        /// </summary>
        private static MeshAsset Merge(List<MeshAsset> parts, string assetId, string sourceName)
        {
            int vertexTotal = 0, indexTotal = 0;
            bool allNormals = true, allUvs = true, allTangents = true;

            foreach (MeshAsset part in parts)
            {
                vertexTotal += part.Vertices.Length;
                indexTotal += part.Indices.Length;
                allNormals &= part.Normals != null;
                allUvs &= part.Uvs != null;
                allTangents &= part.Tangents != null;
            }

            MeshAsset merged = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertexTotal],
                Indices = new int[indexTotal],
                Normals = allNormals ? new float3[vertexTotal] : null,
                Uvs = allUvs ? new float2[vertexTotal] : null,
                Tangents = allTangents ? new float4[vertexTotal] : null
            };

            int vertexBase = 0, indexBase = 0;
            float3 min = new(float.MaxValue), max = new(float.MinValue);

            foreach (MeshAsset part in parts)
            {
                Array.Copy(part.Vertices, 0, merged.Vertices, vertexBase, part.Vertices.Length);
                if (allNormals) Array.Copy(part.Normals, 0, merged.Normals, vertexBase, part.Normals.Length);
                if (allUvs) Array.Copy(part.Uvs, 0, merged.Uvs, vertexBase, part.Uvs.Length);
                if (allTangents) Array.Copy(part.Tangents, 0, merged.Tangents, vertexBase, part.Tangents.Length);

                for (int i = 0; i < part.Indices.Length; i++)
                    merged.Indices[indexBase + i] = part.Indices[i] + vertexBase;

                foreach (MeshPrimitive primitive in part.Primitives)
                {
                    merged.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = primitive.StartIndex + indexBase,
                        IndexCount = primitive.IndexCount,
                        StartVertex = primitive.StartVertex + vertexBase,
                        VertexCount = primitive.VertexCount,
                        MaterialIndex = primitive.MaterialIndex,
                        MaterialId = primitive.MaterialId
                    });
                }

                min = math.min(min, part.BoundsMin);
                max = math.max(max, part.BoundsMax);
                vertexBase += part.Vertices.Length;
                indexBase += part.Indices.Length;
            }

            merged.BoundsMin = min;
            merged.BoundsMax = max;
            return merged;
        }

        /// <summary>
        /// Read a mesh straight from a loaded Unity <see cref="UnityEngine.Mesh"/>.
        /// （直接自已載入的 Unity <see cref="UnityEngine.Mesh"/> 讀取網格。）<br/><br/>
        /// Used when the geometry asset's own accessors stay empty after loading. Slower and more
        /// allocating than the native path, but it depends only on documented Unity API.<br/>
        /// （當幾何資產本身的存取器於載入後仍為空時使用。較慢且配置較多，但僅依賴有文件的 Unity API。）
        /// </summary>
        /// <param name="source">The loaded mesh.（已載入的網格。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The mesh, or <c>null</c> when it holds nothing usable.（網格，若無可用內容則為 <c>null</c>。）</returns>
        public static MeshAsset ReadUnityMesh(UnityEngine.Mesh source, string assetId, string sourceName)
        {
            if ((source == null) || (source.vertexCount == 0)) return null;

            UnityEngine.Vector3[] positions = source.vertices;
            if ((positions == null) || (positions.Length == 0)) return null;

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[positions.Length]
            };
            for (int i = 0; i < positions.Length; i++) mesh.Vertices[i] = positions[i];

            UnityEngine.Vector3[] normals = source.normals;
            if ((normals != null) && (normals.Length == positions.Length))
            {
                mesh.Normals = new float3[normals.Length];
                for (int i = 0; i < normals.Length; i++) mesh.Normals[i] = normals[i];
            }

            UnityEngine.Vector2[] uvs = source.uv;
            if ((uvs != null) && (uvs.Length == positions.Length))
            {
                mesh.Uvs = new float2[uvs.Length];
                for (int i = 0; i < uvs.Length; i++) mesh.Uvs[i] = uvs[i];
            }

            UnityEngine.Vector4[] tangents = source.tangents;
            if ((tangents != null) && (tangents.Length == positions.Length))
            {
                mesh.Tangents = new float4[tangents.Length];
                for (int i = 0; i < tangents.Length; i++) mesh.Tangents[i] = tangents[i];
            }

            // Concatenate the sub-meshes into one index buffer, recording each as a primitive.
            // applyBaseVertex keeps every index absolute, matching the native path.
            // （將各子網格串接為單一索引緩衝區並記錄為 primitive。applyBaseVertex 使索引維持絕對值。）
            List<int> indices = new();
            for (int i = 0; i < source.subMeshCount; i++)
            {
                int[] subIndices = source.GetIndices(i, applyBaseVertex: true);
                if ((subIndices == null) || (subIndices.Length == 0)) continue;

                mesh.Primitives.Add(new MeshPrimitive
                {
                    StartIndex = indices.Count,
                    IndexCount = subIndices.Length,
                    StartVertex = 0,
                    VertexCount = positions.Length,
                    MaterialIndex = i,
                    MaterialId = $"{assetId}_mat{i}"
                });
                indices.AddRange(subIndices);
            }

            if (indices.Count == 0) return null;
            mesh.Indices = indices.ToArray();

            float3 min = new(float.MaxValue), max = new(float.MinValue);
            for (int i = 0; i < mesh.Vertices.Length; i++)
            {
                min = math.min(min, mesh.Vertices[i]);
                max = math.max(max, mesh.Vertices[i]);
            }
            mesh.BoundsMin = min;
            mesh.BoundsMax = max;

            return mesh;
        }

        /// <summary>
        /// Read and merge <b>every</b> loaded Unity mesh, with the same flat material ordinal as
        /// <see cref="ReadAll"/> — this is the same asset seen through Unity's API, so the same
        /// mapping applies.<br/>
        /// （讀取並合併<b>每個</b>已載入的 Unity 網格，材質序數的平坦化方式與
        /// <see cref="ReadAll"/> 相同——這是同一資產經由 Unity API 的呈現，故適用同一對應。）
        /// </summary>
        /// <param name="loaded">The loaded meshes.（已載入的網格。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <param name="meshesMerged">Receives how many meshes contributed geometry.（接收貢獻了幾何的網格數量。）</param>
        /// <returns>The merged mesh, or <c>null</c>.（合併後的網格，或 <c>null</c>。）</returns>
        public static MeshAsset ReadUnityMeshes(UnityEngine.Mesh[] loaded, string assetId, string sourceName, out int meshesMerged)
        {
            meshesMerged = 0;
            if ((loaded == null) || (loaded.Length == 0)) return null;

            List<MeshAsset> parts = new();
            int ordinal = 0;

            foreach (UnityEngine.Mesh source in loaded)
            {
                int subMeshCount = math.max(1, source != null ? source.subMeshCount : 1);
                MeshAsset part = ReadUnityMesh(source, assetId, sourceName);

                if (part != null)
                {
                    foreach (MeshPrimitive primitive in part.Primitives)
                    {
                        primitive.MaterialIndex += ordinal;
                        primitive.MaterialId = $"{assetId}_mat{primitive.MaterialIndex}";
                    }

                    parts.Add(part);
                    meshesMerged++;
                }

                ordinal += subMeshCount;
            }

            if (parts.Count == 0) return null;
            if (parts.Count == 1) return parts[0];
            return Merge(parts, assetId, sourceName);
        }

        /// <summary>
        /// Read one mesh out of a geometry asset into a <see cref="MeshAsset"/>.
        /// （自幾何資產讀取單一網格至 <see cref="MeshAsset"/>。）
        /// </summary>
        /// <param name="asset">The geometry asset.（幾何資產。）</param>
        /// <param name="meshIndex">Which mesh to read.（要讀取的網格。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The mesh, or <c>null</c> when nothing readable was found.（網格，若無可讀資料則為 <c>null</c>。）</returns>
        public static MeshAsset Read(GeometryAsset asset, int meshIndex, string assetId, string sourceName)
        {
            if (asset == null) return null;

            int vertexCount = asset.GetVertexCount(meshIndex);
            int indexCount = asset.GetIndicesCount(meshIndex);
            if ((vertexCount <= 0) || (indexCount <= 0)) return null;

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertexCount],
                Indices = new int[indexCount]
            };

            // -- positions -------------------------------------------------------------------
            NativeArray<MeshVertex> vertices = new(vertexCount, Allocator.Temp);
            try
            {
                asset.GetAttributeFormat(meshIndex, VertexAttribute.Position, out VertexAttributeFormat format, out int dimension);
                MeshVertex.Unpack(asset.GetAttributeData(meshIndex, VertexAttribute.Position), vertices, vertexCount, format, dimension);
                for (int i = 0; i < vertexCount; i++) mesh.Vertices[i] = vertices[i].m_Vertex;
            }
            finally
            {
                vertices.Dispose();
            }

            // -- indices ---------------------------------------------------------------------
            NativeArray<MeshIndex> indices = new(indexCount, Allocator.Temp);
            try
            {
                MeshIndex.Unpack(asset.GetIndices(meshIndex), indices, indexCount, asset.GetIndexFormat(meshIndex), 0);
                for (int i = 0; i < indexCount; i++) mesh.Indices[i] = indices[i].m_Index;
            }
            finally
            {
                indices.Dispose();
            }

            // -- optional attributes ----------------------------------------------------------
            mesh.Normals = ReadFloat3(asset, meshIndex, VertexAttribute.Normal, vertexCount);
            mesh.Uvs = ReadFloat2(asset, meshIndex, VertexAttribute.TexCoord0, vertexCount);
            mesh.Tangents = ReadFloat4(asset, meshIndex, VertexAttribute.Tangent, vertexCount);

            // -- sub-meshes -------------------------------------------------------------------
            // These are the drawable ranges the ECS path never exposed, which is why every mesh
            // ripped through it came out as a single primitive covering the whole index buffer.
            // （這些即 ECS 路徑從未提供的可繪製範圍，也是先前每個網格都成為單一 primitive 的原因。）
            try
            {
                int subMeshCount = asset.GetSubMeshCount(meshIndex);
                for (int i = 0; i < subMeshCount; i++)
                {
                    SubMeshDescriptor descriptor = asset.GetSubMeshDesc(meshIndex, i);
                    if (descriptor.indexCount <= 0) continue;

                    // glTF has no baseVertex, so fold it into the indices now and keep everything
                    // downstream working on plain absolute indices.
                    // （glTF 沒有 baseVertex，故此處先併入索引，使下游一律使用絕對索引。）
                    if (descriptor.baseVertex != 0)
                    {
                        int end = math.min(descriptor.indexStart + descriptor.indexCount, indexCount);
                        for (int k = descriptor.indexStart; k < end; k++) mesh.Indices[k] += (int)descriptor.baseVertex;
                    }

                    mesh.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = descriptor.indexStart,
                        IndexCount = descriptor.indexCount,
                        StartVertex = descriptor.firstVertex,
                        VertexCount = descriptor.vertexCount,
                        MaterialIndex = i,
                        MaterialId = $"{assetId}_mat{i}"
                    });
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Sub-mesh descriptors unavailable for `{assetId}`: {ex.Message}");
            }

            float3 min = new(float.MaxValue), max = new(float.MinValue);
            for (int i = 0; i < vertexCount; i++)
            {
                min = math.min(min, mesh.Vertices[i]);
                max = math.max(max, mesh.Vertices[i]);
            }
            mesh.BoundsMin = min;
            mesh.BoundsMax = max;

            return mesh;
        }

        /// <summary>
        /// Read a three-component vertex attribute, or <c>null</c> when the mesh lacks it.
        /// （讀取三分量的頂點屬性，若網格缺少該屬性則回傳 <c>null</c>。）
        /// </summary>
        private static float3[] ReadFloat3(GeometryAsset asset, int meshIndex, VertexAttribute attribute, int count)
        {
            NativeArray<MeshNormal> buffer = new(count, Allocator.Temp);
            try
            {
                asset.GetAttributeFormat(meshIndex, attribute, out VertexAttributeFormat format, out int dimension);
                if (dimension <= 0) return null;
                MeshNormal.Unpack(asset.GetAttributeData(meshIndex, attribute), buffer, count, format, dimension);

                float3[] result = new float3[count];
                for (int i = 0; i < count; i++) result[i] = buffer[i].m_Normal;
                return result;
            }
            catch (Exception)
            {
                return null;
            }
            finally
            {
                buffer.Dispose();
            }
        }

        /// <inheritdoc cref="ReadFloat3"/>
        private static float2[] ReadFloat2(GeometryAsset asset, int meshIndex, VertexAttribute attribute, int count)
        {
            NativeArray<MeshUV0> buffer = new(count, Allocator.Temp);
            try
            {
                asset.GetAttributeFormat(meshIndex, attribute, out VertexAttributeFormat format, out int dimension);
                if (dimension <= 0) return null;
                MeshUV0.Unpack(asset.GetAttributeData(meshIndex, attribute), buffer, count, format, dimension);

                float2[] result = new float2[count];
                for (int i = 0; i < count; i++) result[i] = buffer[i].m_Uv;
                return result;
            }
            catch (Exception)
            {
                return null;
            }
            finally
            {
                buffer.Dispose();
            }
        }

        /// <inheritdoc cref="ReadFloat3"/>
        private static float4[] ReadFloat4(GeometryAsset asset, int meshIndex, VertexAttribute attribute, int count)
        {
            NativeArray<MeshTangent> buffer = new(count, Allocator.Temp);
            try
            {
                asset.GetAttributeFormat(meshIndex, attribute, out VertexAttributeFormat format, out int dimension);
                if (dimension <= 0) return null;
                MeshTangent.Unpack(asset.GetAttributeData(meshIndex, attribute), buffer, count, format, dimension);

                float4[] result = new float4[count];
                for (int i = 0; i < count; i++) result[i] = buffer[i].m_Tangent;
                return result;
            }
            catch (Exception)
            {
                return null;
            }
            finally
            {
                buffer.Dispose();
            }
        }
    }
}
