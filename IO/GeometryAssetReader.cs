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
