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
    /// Reading one vertex attribute out of a native geometry buffer, whatever it is packed as.
    /// </summary>
    /// <remarks>
    /// Three rules, shared by every attribute: ask the asset for the attribute's format, treat
    /// dimension zero as "no such attribute" rather than an error, and treat a native throw the
    /// same way — so a bad prefab costs its own mesh and not the rip.
    /// </remarks>
    public static partial class GeometryAssetReader
    {
        /// <summary>
        /// Read one mesh out of a geometry asset into a <see cref="MeshAsset"/>.
        /// </summary>
        /// <param name="asset">The geometry asset.</param>
        /// <param name="meshIndex">Which mesh to read.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <returns>The mesh, or <c>null</c> when nothing readable was found.</returns>
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
            mesh.Normals = ReadAttribute<MeshNormal, float3>(
                asset, meshIndex, VertexAttribute.Normal, vertexCount, MeshNormal.Unpack, p => p.m_Normal);
            mesh.Uvs = ReadAttribute<MeshUV0, float2>(
                asset, meshIndex, VertexAttribute.TexCoord0, vertexCount, MeshUV0.Unpack, p => p.m_Uv);
            mesh.Tangents = ReadAttribute<MeshTangent, float4>(
                asset, meshIndex, VertexAttribute.Tangent, vertexCount, MeshTangent.Unpack, p => p.m_Tangent);

            // -- sub-meshes -------------------------------------------------------------------
            // These are the drawable ranges the ECS path never exposed, which is why every mesh
            // ripped through it came out as a single primitive covering the whole index buffer.
            //
            try
            {
                int subMeshCount = asset.GetSubMeshCount(meshIndex);
                for (int i = 0; i < subMeshCount; i++)
                {
                    SubMeshDescriptor descriptor = asset.GetSubMeshDesc(meshIndex, i);
                    if (descriptor.indexCount <= 0) continue;

                    // glTF has no baseVertex, so fold it into the indices now and keep everything
                    // downstream working on plain absolute indices.
                    //
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
        /// </summary>
        /// <summary>
        /// The game's own unpacker for one vertex-attribute struct: <c>MeshNormal.Unpack</c> and
        /// friends. A delegate rather than an interface because these are static methods on
        /// unrelated structs, with nothing in the game's types to constrain them by.
        /// </summary>
        /// <typeparam name="TPacked">The packed struct the attribute decodes into.</typeparam>
        private delegate void Unpacker<TPacked>(
            NativeSlice<byte> data,
            NativeArray<TPacked> buffer,
            int count,
            VertexAttributeFormat format,
            int dimension) where TPacked : struct;
    }
}
