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
    /// The ECS <c>MeshVertex</c>/<c>MeshIndex</c> buffers on a prefab entity hold only what the
    /// game needs on the CPU for raycasting and camera collision, which is a heavily reduced LOD —
    /// a building whose <c>MeshData.m_IndexCount</c> reports 194,724 indices exposes just 4,800
    /// through ECS. The authored mesh lives in the geometry asset behind
    /// <see cref="RenderPrefab.geometryAsset"/>, which also carries the sub-mesh descriptors that
    /// the ECS path lacks entirely.
    /// Unpacking is delegated to the game's own <c>Unpack</c> helpers, so vertex attribute formats
    /// (float32, float16, normalised integers, varying dimensions) are decoded exactly as the game
    /// decodes them rather than reimplemented here.
    /// </summary>
    public static partial class GeometryAssetReader
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// Describe the meshes a geometry asset contains, for diagnostics and for choosing one.
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
        /// A geometry asset commonly holds several meshes — the LOD chain — so the caller picks
        /// rather than assuming index 0.
        /// </summary>
        /// <param name="asset">The geometry asset.</param>
        /// <param name="meshCount">The number of meshes reported by the render prefab.</param>
        /// <returns>One entry per readable mesh.</returns>
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
                    //
                    break;
                }
            }

            return result;
        }

        /// <summary>
        /// Read <b>every</b> mesh a geometry asset holds into one <see cref="MeshAsset"/>, with
        /// each primitive's <see cref="MeshPrimitive.MaterialIndex"/> being the flat
        /// (mesh, sub-mesh) ordinal across the whole asset.
        /// <b>Why all of them, and why that ordinal.</b> The meshes of one geometry asset are
        /// simultaneous parts, not an LOD chain — LODs are separate render prefabs behind
        /// <c>LodProperties</c>. The game draws one batch per flattened sub-mesh
        /// (<c>ManagedBatchSystem.CreateBatch</c> binds <c>GetSurfaceAsset(m_SubMeshIndex)</c>),
        /// and <c>RenderPrefab.ObtainMesh(materialIndex)</c> defines the mapping: walk the meshes
        /// in order, subtracting each one's sub-mesh count. Reading only the richest mesh — the
        /// previous behaviour — dropped every sibling part and, when the richest was not the
        /// first, resolved its surfaces at the wrong offset.
        /// </summary>
        /// <param name="asset">The geometry asset.</param>
        /// <param name="meshCount">The number of meshes the render prefab reports.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <param name="meshesMerged">Receives how many meshes contributed geometry.</param>
        /// <returns>The merged mesh, or <c>null</c>.</returns>
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
                    //
                    // The full exception, stack and all: `ArgumentOutOfRangeException` alone
                    // cannot say which accessor mis-sized (Highway_Lane_4_Mesh failed exactly
                    // this way in a full-city rip and the message was not enough to diagnose).
                    //
                    _log.Warn($"Mesh {meshIndex} of `{assetId}` unreadable: {ex}");
                }

                if (part != null)
                {
                    // Rebase this mesh's primitives onto the asset-wide ordinal, which is what
                    // `GetSurfaceAsset` is indexed by.
                    //
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
        /// Decode one vertex attribute into a managed array, or <c>null</c> when the mesh does not
        /// carry it.
        /// </summary>
        /// <remarks>
        /// <para>
        /// This was <c>ReadFloat3</c>, <c>ReadFloat2</c> and <c>ReadFloat4</c> — twenty-one lines
        /// three times, differing only in four types, and the second and third carried
        /// <c>&lt;inheritdoc cref="ReadFloat3"/&gt;</c> as an admission that they were the same
        /// method. What they share is not the shape but the RULES: ask the asset for the
        /// attribute's format, treat a dimension of zero as "this mesh has no such attribute"
        /// rather than as an error, and treat a throw from the native decode as the same thing,
        /// because a prefab that cannot be read must cost its own mesh and not the whole rip.
        /// Three copies is three chances to get one of those wrong.
        /// </para>
        /// <para>
        /// <paramref name="select"/> runs per vertex, which is a delegate call on a hot loop —
        /// about 20.7 M triangles across a full-city rip. It is the right trade anyway: at a few
        /// nanoseconds a call that is well under a tenth of a second inside a mesh stage that
        /// takes 1,683.
        /// </para>
        /// </remarks>
        /// <typeparam name="TPacked">The packed struct the game decodes the attribute into.</typeparam>
        /// <typeparam name="TValue">The value type this returns per vertex.</typeparam>
        /// <param name="asset">The geometry asset being read.</param>
        /// <param name="meshIndex">Which mesh within the asset.</param>
        /// <param name="attribute">The attribute to decode.</param>
        /// <param name="count">How many vertices the mesh has.</param>
        /// <param name="unpack">The game's unpacker for <typeparamref name="TPacked"/>.</param>
        /// <param name="select">The field of the packed struct that carries the value.</param>
        /// <returns>One value per vertex, or <c>null</c> when the attribute is absent or unreadable.</returns>
        private static TValue[] ReadAttribute<TPacked, TValue>(
            GeometryAsset asset,
            int meshIndex,
            VertexAttribute attribute,
            int count,
            Unpacker<TPacked> unpack,
            Func<TPacked, TValue> select) where TPacked : struct
        {
            NativeArray<TPacked> buffer = new(count, Allocator.Temp);
            try
            {
                asset.GetAttributeFormat(meshIndex, attribute, out VertexAttributeFormat format, out int dimension);
                if (dimension <= 0) return null;
                unpack(asset.GetAttributeData(meshIndex, attribute), buffer, count, format, dimension);

                TValue[] result = new TValue[count];
                for (int i = 0; i < count; i++) result[i] = select(buffer[i]);
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
