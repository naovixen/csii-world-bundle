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
    /// Several geometry assets merged into one mesh, keeping every material range separable.
    /// </summary>
    public static partial class GeometryAssetReader
    {
        /// <summary>
        /// Concatenate several read meshes into one, offsetting indices and primitive ranges.
        /// An attribute survives the merge only when every part carries it — a mesh without
        /// normals stitched to one with them would otherwise get fabricated values.
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
        /// Used when the geometry asset's own accessors stay empty after loading. Slower and more
        /// allocating than the native path, but it depends only on documented Unity API.
        /// </summary>
        /// <param name="source">The loaded mesh.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <returns>The mesh, or <c>null</c> when it holds nothing usable.</returns>
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
            //
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
        /// mapping applies.
        /// </summary>
        /// <param name="loaded">The loaded meshes.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <param name="meshesMerged">Receives how many meshes contributed geometry.</param>
        /// <returns>The merged mesh, or <c>null</c>.</returns>
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
    }
}
