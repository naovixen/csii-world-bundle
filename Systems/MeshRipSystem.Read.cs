using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// Reading one prefab's geometry: the full-detail route, and every fallback under it.
    /// </summary>
    /// <remarks>
    /// Full-detail meshes come from <c>RenderPrefab.geometryAsset</c> after <c>ObtainMeshes</c>.
    /// The ECS mesh buffers are only a reduced raycast LOD — reading those was the first
    /// attempt, and it shipped a city of low-poly proxies.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Read full-detail geometry through the render prefab's <c>GeometryAsset</c>.
        /// </summary>
        /// <param name="meshEntity">The sub-mesh (render prefab) entity.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <param name="renderPrefab">The render prefab behind the entity.</param>
        /// <param name="trace">Optional diagnostics sink.</param>
        /// <returns>The mesh, or <c>null</c> to fall back to the ECS buffers.</returns>
        private MeshAsset TryReadGeometryAsset(string assetId, string sourceName, RenderPrefab renderPrefab, List<string> trace)
        {
            try
            {
                if (renderPrefab == null)
                {
                    trace?.Add("geometry asset: entity does not resolve to a RenderPrefab");
                    return null;
                }

                if (!renderPrefab.hasGeometryAsset)
                {
                    trace?.Add("geometry asset: render prefab has none");
                    return null;
                }

                Colossal.IO.AssetDatabase.GeometryAsset asset = renderPrefab.geometryAsset;
                trace?.Add($"geometry asset: meshCount={renderPrefab.meshCount} vertexCount={renderPrefab.vertexCount} indexCount={renderPrefab.indexCount} materialCount={renderPrefab.materialCount}");

                // The asset's accessors stay empty until its data is resident, and there is no
                // public "ensure loaded" call — ObtainMeshes is the load trigger. Always release
                // again: a city has thousands of buildings and holding every mesh would balloon.
                //
                UnityEngine.Mesh[] loaded = null;
                try
                {
                    try
                    {
                        loaded = asset.ObtainMeshes(true);
                        trace?.Add($"geometry asset: ObtainMeshes returned {(loaded?.Length ?? 0)} mesh(es)");
                    }
                    catch (Exception ex)
                    {
                        trace?.Add($"geometry asset: ObtainMeshes failed — {ex.GetType().Name} {ex.Message}");
                    }

                    // Which count to trust: the asset's own file header, not the prefab's
                    // serialized `m_MeshCount`. They agree on vanilla content (`MeshSystem`
                    // warns whenever meshCount != materialCount and a vanilla load logs none),
                    // but the header is what `CreateMeshes` actually iterates, and after
                    // `ObtainMeshes` it is resident — so it is the ground truth at zero cost.
                    //
                    int meshCount = renderPrefab.meshCount;
                    try { if (asset.meshCount > 0) meshCount = asset.meshCount; }
                    catch (Exception) { /* header not resident; the prefab's declared count is the best available */ }

                    if (trace != null)
                        foreach (GeometryAssetReader.MeshInfo info in GeometryAssetReader.Describe(asset, meshCount))
                            trace.Add($"  {info}");

                    // `ObtainMeshes` loads the CPU-side data only when no instances exist yet.
                    // For a prefab the game is rendering right now — every drawn lane mesh, for
                    // one — `m_Instances` is already non-null, the call returns the live meshes
                    // without touching `m_Data`, and every native accessor then throws
                    // `ArgumentOutOfRangeException` off `data.meshCount == 0`. (Its
                    // `keepReadable` parameter is dead in this game build — the decompiled body
                    // never reads it.) That is not a failed read, it is a path choice: skip the
                    // native accessors entirely and read the live Unity meshes, which
                    // `CreateMeshes` built readable. A full-city rip warned 16 times over
                    // exactly this and every one of the 16 files was fine.
                    //
                    bool dataResident = asset.data.IsValid;
                    if (!dataResident)
                    {
                        trace?.Add("geometry asset: CPU data not resident (the game holds live instances); reading the Unity meshes");
                        _log.Debug($"`{assetId}`: geometry data not resident (live instances held by the game); reading Unity meshes instead.");
                    }

                    // Every mesh of the asset, not the richest one. The meshes are simultaneous
                    // parts — LODs live behind LodProperties as separate render prefabs — and the
                    // game binds surface i to the i-th (mesh, sub-mesh) pair in flat order
                    // (`RenderPrefab.ObtainMesh`), which is exactly the ordinal the reader assigns.
                    // Picking one mesh dropped its siblings and, off the first slot, mis-assigned
                    // their surfaces.
                    //
                    int meshesMerged = 0;
                    MeshAsset merged = dataResident
                        ? GeometryAssetReader.ReadAll(asset, meshCount, assetId, sourceName, out meshesMerged)
                        : null;
                    if (merged != null)
                    {
                        if (meshesMerged > 1) MultiMeshPrefabs++;
                        if (meshesMerged < meshCount)
                        {
                            // The meshes of one asset draw together, so coming up short is lost
                            // geometry, not a variant choice — counted, named in the log, and
                            // declared in warnings.jsonl: the .glb exists but is incomplete,
                            // which a consumer cannot see from the file alone (contract §10).
                            //
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: geometry asset holds {meshCount} mesh(es) but only {meshesMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {meshesMerged} of {meshCount} geometry-asset meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: merged {meshesMerged} of {meshCount} mesh(es) ({merged.TriangleCount} tris, {merged.Primitives.Count} primitives)");
                        return merged;
                    }

                    // Native accessors still empty: fall back to the loaded Unity meshes, merged
                    // under the same flat ordinal.
                    //
                    MeshAsset viaUnity = GeometryAssetReader.ReadUnityMeshes(loaded, assetId, sourceName, out int unityMerged);
                    if (viaUnity != null)
                    {
                        if (unityMerged > 1) MultiMeshPrefabs++;
                        if ((loaded != null) && (unityMerged < loaded.Length))
                        {
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: {loaded.Length} loaded Unity mesh(es) but only {unityMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {unityMerged} of {loaded.Length} Unity meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: via {unityMerged} Unity mesh(es) ({viaUnity.TriangleCount} tris, {viaUnity.Primitives.Count} primitives)");
                        return viaUnity;
                    }

                    trace?.Add("geometry asset: no readable meshes after loading");
                    return null;
                }
                finally
                {
                    try { asset.ReleaseMeshes(); } catch { /* nothing useful to do if release fails */ }
                }
            }
            catch (Exception ex)
            {
                trace?.Add($"geometry asset: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Geometry asset unavailable for `{assetId}`: {ex.Message}");
                return null;
            }
        }

        /// <summary>
        /// Find the entity that actually holds geometry buffers, starting from a sub-mesh entity.
        /// A render prefab entity is a container: it carries <c>MeshData</c>, a <c>LodMesh</c>
        /// buffer of per-LOD entities and a <c>SharedMeshData</c> pointer, but frequently no
        /// vertices of its own. Geometry hangs off one of those references, so follow them —
        /// <c>SharedMeshData</c> first, then LOD 0 — until a real vertex buffer turns up.
        /// </summary>
        /// <param name="entity">The entity to start from.</param>
        /// <param name="geometry">The entity holding the geometry buffers.</param>
        /// <param name="lod">The LOD level reached, <c>0</c> for full detail.</param>
        /// <param name="trace">Optional sink recording each hop, for diagnostics.</param>
        /// <param name="depth">The current recursion depth.</param>
        /// <returns>Whether geometry was found.</returns>
        private bool TryResolveGeometry(Entity entity, out Entity geometry, out int lod, List<string> trace, int depth = 0)
        {
            List<GeometryCandidate> candidates = new();
            Collect(entity, 0, "root", candidates, new HashSet<Entity>(), 0);

            if (trace != null)
            {
                trace.Add($"container {entity.Index} [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, entity))}]");
                if (EntityManager.HasComponent<MeshData>(entity))
                {
                    MeshData data = EntityManager.GetComponentData<MeshData>(entity);
                    // What the container *claims* the mesh holds. If the richest candidate falls
                    // well short of m_IndexCount, we are reading a reduced LOD, not the real mesh.
                    //
                    trace.Add($"MeshData: indexCount={data.m_IndexCount} subMeshCount={data.m_SubMeshCount} minLod={data.m_MinLod} state={data.m_State}");
                }
                foreach (GeometryCandidate candidate in candidates)
                    trace.Add($"candidate via {candidate.Path}: entity {candidate.Entity.Index}, {candidate.VertexCount} verts, {candidate.IndexCount / 3} tris, materials={candidate.MaterialCount} [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, candidate.Entity))}]");
            }

            if (candidates.Count == 0)
            {
                geometry = Entity.Null;
                lod = 0;
                return false;
            }

            // Pick the richest candidate. LOD 0 is by definition the one with the most geometry,
            // and picking by index count avoids depending on buffer ordering being detail-ordered.
            //
            GeometryCandidate best = candidates[0];
            foreach (GeometryCandidate candidate in candidates)
                if (candidate.IndexCount > best.IndexCount) best = candidate;

            geometry = best.Entity;
            lod = best.Lod;
            trace?.Add($"chosen: {best.Path} ({best.IndexCount / 3} tris)");
            return true;
        }

        /// <summary>
        /// A reachable entity that carries real geometry, and how it was reached.
        /// </summary>
        private struct GeometryCandidate
        {
            public Entity Entity;
            public int Lod;
            public string Path;
            public int VertexCount;
            public int IndexCount;
            public int MaterialCount;
        }

        /// <summary>
        /// Walk every geometry reference reachable from <paramref name="entity"/>, collecting each
        /// entity that carries non-empty vertex and index buffers.
        /// A render prefab entity is a container carrying <c>MeshData</c>, a <c>LodMesh</c> buffer
        /// and a <c>SharedMeshData</c> pointer, but often no vertices of its own. Taking the first
        /// match found risks silently settling for a reduced LOD, so gather all of them and let the
        /// caller choose.
        /// </summary>
        private void Collect(Entity entity, int lod, string path, List<GeometryCandidate> candidates, HashSet<Entity> visited, int depth)
        {
            if ((entity == Entity.Null) || (depth > 4) || !visited.Add(entity)) return;

            if (EntityManager.HasBuffer<MeshVertex>(entity) && EntityManager.HasBuffer<MeshIndex>(entity))
            {
                int vertexCount = EntityManager.GetBuffer<MeshVertex>(entity, true).Length;
                int indexCount = EntityManager.GetBuffer<MeshIndex>(entity, true).Length;
                if ((vertexCount > 0) && (indexCount > 0))
                {
                    candidates.Add(new GeometryCandidate
                    {
                        Entity = entity,
                        Lod = lod,
                        Path = path,
                        VertexCount = vertexCount,
                        IndexCount = indexCount,
                        MaterialCount = EntityManager.HasBuffer<MeshMaterial>(entity)
                            ? EntityManager.GetBuffer<MeshMaterial>(entity, true).Length : 0
                    });
                }
            }

            if (EntityManager.HasComponent<SharedMeshData>(entity))
                Collect(EntityManager.GetComponentData<SharedMeshData>(entity).m_Mesh, lod, $"{path}/shared", candidates, visited, depth + 1);

            if (EntityManager.HasBuffer<LodMesh>(entity))
            {
                DynamicBuffer<LodMesh> lods = EntityManager.GetBuffer<LodMesh>(entity, true);
                for (int i = 0; i < lods.Length; i++)
                    Collect(lods[i].m_LodMesh, i, $"{path}/lod{i}", candidates, visited, depth + 1);
            }
        }

        /// <summary>
        /// Read one mesh entity's geometry into a <see cref="MeshAsset"/>.
        /// </summary>
        /// <param name="meshEntity">The mesh (render prefab) entity.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <returns>The mesh, or <c>null</c> when the entity carries no geometry.</returns>
        public MeshAsset ReadMesh(Entity meshEntity, string assetId, string sourceName, List<string> trace = null)
        {
            // The render prefab is both where the authored geometry lives and where the material
            // slots live, so resolve it once and let both readers share it.
            //
            RenderPrefab renderPrefab = null;
            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Prefabs.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase))
                renderPrefab = prefabBase as RenderPrefab;

            // Prefer the geometry asset: the ECS buffers only ever hold the reduced LOD the game
            // keeps CPU-side for raycasting, and carry no sub-mesh ranges at all.
            //
            MeshAsset fromAsset = TryReadGeometryAsset(assetId, sourceName, renderPrefab, trace);
            if (fromAsset != null)
            {
                AssignMaterials(fromAsset, renderPrefab);
                TryFlattenDecal(fromAsset, renderPrefab, trace);
                return fromAsset;
            }

            if (!TryResolveGeometry(meshEntity, out Entity geometryEntity, out int lod, trace)) return null;

            DynamicBuffer<MeshVertex> vertices = EntityManager.GetBuffer<MeshVertex>(geometryEntity, true);
            DynamicBuffer<MeshIndex> indices = EntityManager.GetBuffer<MeshIndex>(geometryEntity, true);

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertices.Length],
                Indices = new int[indices.Length]
            };

            for (int i = 0; i < vertices.Length; i++) mesh.Vertices[i] = vertices[i].m_Vertex;
            for (int i = 0; i < indices.Length; i++) mesh.Indices[i] = indices[i].m_Index;

            if (EntityManager.HasBuffer<MeshNormal>(geometryEntity))
            {
                DynamicBuffer<MeshNormal> normals = EntityManager.GetBuffer<MeshNormal>(geometryEntity, true);
                if (normals.Length == vertices.Length)
                {
                    mesh.Normals = new float3[normals.Length];
                    for (int i = 0; i < normals.Length; i++) mesh.Normals[i] = normals[i].m_Normal;
                }
            }

            if (EntityManager.HasBuffer<MeshUV0>(geometryEntity))
            {
                DynamicBuffer<MeshUV0> uvs = EntityManager.GetBuffer<MeshUV0>(geometryEntity, true);
                if (uvs.Length == vertices.Length)
                {
                    mesh.Uvs = new float2[uvs.Length];
                    for (int i = 0; i < uvs.Length; i++) mesh.Uvs[i] = uvs[i].m_Uv;
                }
            }

            if (EntityManager.HasBuffer<MeshTangent>(geometryEntity))
            {
                DynamicBuffer<MeshTangent> tangents = EntityManager.GetBuffer<MeshTangent>(geometryEntity, true);
                if (tangents.Length == vertices.Length)
                {
                    mesh.Tangents = new float4[tangents.Length];
                    for (int i = 0; i < tangents.Length; i++) mesh.Tangents[i] = tangents[i].m_Tangent;
                }
            }

            if (EntityManager.HasBuffer<MeshMaterial>(geometryEntity))
            {
                // The material slot is the buffer *position*, never the stored `m_MaterialIndex`.
                // Nothing in the game ever writes authored ranges into this buffer; its one writer
                // is `MeshSystem.OnUpdate`, which runs at prefab creation, resizes the buffer to
                // one entry per surface asset *in order*, stores a session-global library id in
                // `m_MaterialIndex`, and leaves the range fields uninitialised. The game itself
                // resolves a surface from this buffer by position (`ManagedBatchSystem.CreateBatch`
                // matches the global id and then calls `GetSurfaceAsset(position)`), so position
                // is the only meaning the slot has — and the global id, used positionally, would
                // pick an arbitrary surface. The garbage ranges are dropped by the range check in
                // `GltfWriter.NormalizePrimitives` / `NetworkRipSystem.AppendPieceIndices`.
                //
                DynamicBuffer<MeshMaterial> materials = EntityManager.GetBuffer<MeshMaterial>(geometryEntity, true);
                for (int i = 0; i < materials.Length; i++)
                {
                    MeshMaterial material = materials[i];
                    mesh.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = material.m_StartIndex,
                        IndexCount = material.m_IndexCount,
                        StartVertex = material.m_StartVertex,
                        VertexCount = material.m_VertexCount,
                        MaterialIndex = i,
                        MaterialId = $"{assetId}_mat{i}"
                    });
                }
            }

            if (EntityManager.HasComponent<MeshData>(meshEntity))
            {
                MeshData data = EntityManager.GetComponentData<MeshData>(meshEntity);
                mesh.BoundsMin = data.m_Bounds.min;
                mesh.BoundsMax = data.m_Bounds.max;
            }

            AssignMaterials(mesh, renderPrefab);
            TryFlattenDecal(mesh, renderPrefab, trace);
            return mesh;
        }
    }
}
