using Carto.Domain;
using Game.Prefabs;
using System;
using Unity.Entities;

namespace Carto.Systems
{
    /// <summary>
    /// The game's own LOD chain behind a mesh — read whole, so a consumer never has to decimate.
    /// </summary>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Read the game's own coarser levels of a mesh — the <c>LodMesh</c> buffer
        /// <c>MeshSystem</c> fills from <c>LodProperties.m_LodMeshes</c>, richest first — each
        /// from its own render prefab's geometry asset with its own materials, onto
        /// <see cref="MeshAsset.Lods"/>.
        /// </summary>
        /// <remarks>
        /// Until refactor Phase 10 the ripper walked this buffer only to pick the RICHEST
        /// candidate and threw the rest away (<c>lodCap 0</c>), and the consumers decimated the
        /// full mesh themselves. On a CS2 facade — unwelded window quads over an open wall —
        /// every decimator collapses to a see-through lattice (the 2026-08-29 screenshots), and
        /// no distance metric sees the hole. The artist-made levels have none, and the game's
        /// own switch rule (<c>RequiredBatchesSystem</c>) picks between them; both are what a
        /// consumer should use, and this is where they enter the bundle.
        /// </remarks>
        private void ReadLodChain(Entity meshEntity, RenderPrefab renderPrefab, MeshAsset mesh, string assetId, string sourceName)
        {
            if (renderPrefab != null && renderPrefab.TryGet(out LodProperties lodProperties))
                mesh.LodBias = lodProperties.m_Bias;

            if (!EntityManager.HasBuffer<LodMesh>(meshEntity)) return;
            DynamicBuffer<LodMesh> lods = EntityManager.GetBuffer<LodMesh>(meshEntity, true);
            int count = lods.Length;
            for (int i = 0; i < count; i++)
            {
                Entity lodEntity = lods[i].m_LodMesh;
                if ((lodEntity == Entity.Null) || !EntityManager.HasComponent<PrefabData>(lodEntity)) continue;
                if (!Prefabs.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(lodEntity), out PrefabBase lodBase)
                    || lodBase is not RenderPrefab lodPrefab) continue;

                // The game's IMPOSTOR level — a card its own shader textures procedurally
                // (`MeshFlags.Impostor`; every vanilla tree's LOD 1) — is not a mesh a consumer
                // can draw: no UVs, no normals. It is counted and left out.
                if (EntityManager.HasComponent<MeshData>(lodEntity)
                    && ((EntityManager.GetComponentData<MeshData>(lodEntity).m_State & MeshFlags.Impostor) != 0))
                {
                    ImpostorLevelsSkipped++;
                    continue;
                }

                int level = mesh.Lods.Count + 1;
                MeshAsset lod = TryReadGeometryAsset($"{assetId}_LOD{level}", $"{sourceName} LOD{level}", lodPrefab, null);
                if (lod == null) continue;
                if ((lod.Uvs == null) || (lod.Uvs.Length == 0) || (lod.Normals == null))
                {
                    ImpostorLevelsSkipped++;
                    continue;
                }
                lod.Lod = level;
                AssignMaterials(lod, lodPrefab);
                mesh.Lods.Add(lod);
            }

            if (mesh.Lods.Count > 0)
            {
                LodChains++;
                LodLevelsRead += mesh.Lods.Count;
                LodCap = Math.Max(LodCap, mesh.Lods.Count);
            }
        }

        /// <summary>Authored levels left out because they are impostor cards (no UVs), this run.</summary>
        public int ImpostorLevelsSkipped { get; private set; }

        /// <summary>Meshes whose authored LOD chain was read, this run.</summary>
        public int LodChains { get; private set; }

        /// <summary>Authored LOD levels read, this run.</summary>
        public int LodLevelsRead { get; private set; }

        /// <summary>The deepest authored chain seen this run — what <c>ripOptions.lodCap</c> reports.</summary>
        public int LodCap { get; private set; }
    }
}
