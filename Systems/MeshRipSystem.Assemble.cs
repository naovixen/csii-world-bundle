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
    /// One prefab's parts, assembled into the mesh states the bundle ships.
    /// </summary>
    /// <remarks>
    /// A prefab's SubMesh buffer mixes simultaneous PARTS with mutually exclusive STATE
    /// variants, and telling them apart is this file's whole job: the parts compose into one
    /// mesh, the variants each become their own file (§6.1/§9).
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Assemble a prefab entity's sub-meshes into one <see cref="PrefabMesh"/>.
        /// </summary>
        /// <param name="item">The prefab to assemble.</param>
        /// <param name="skipReason">Why nothing came out, when the result is <c>null</c>.</param>
        /// <param name="skirt">The foundation skirt to generate behind it, or <c>null</c>.</param>
        /// <param name="variants">The alternative-state files to write alongside it; never <c>null</c>.</param>
        /// <returns>The assembled prefab, or <c>null</c>.</returns>
        private PrefabMesh BuildPrefabMesh(WorkItem item, out string skipReason, out SkirtWork skirt, out List<PrefabMesh> variants)
        {
            skipReason = null;
            skirt = null;
            variants = new List<PrefabMesh>();

            if (!EntityManager.HasBuffer<SubMesh>(item.PrefabEntity))
            {
                skipReason = "no SubMesh buffer";
                return null;
            }

            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(item.PrefabEntity, true);
            if (buffer.Length == 0)
            {
                skipReason = "empty SubMesh buffer";
                return null;
            }

            // Copy out before reading geometry: obtaining a mesh loads an asset, and a dynamic
            // buffer is only valid until the entity's chunk moves.
            //
            SubMesh[] subMeshes = new SubMesh[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) subMeshes[i] = buffer[i];

            List<int> selected = SelectSubMeshes(subMeshes);

            PrefabMesh prefab = AssembleParts(item, subMeshes, selected, item.AssetId, out skirt);

            if (prefab == null)
            {
                skipReason = $"none of {subMeshes.Length} sub-mesh(es) yielded geometry";
                return null;
            }

            BuildStateVariants(item, subMeshes, variants);
            return prefab;
        }

        /// <summary>
        /// Write each alternative state the prefab authors as its own derived file.
        /// Only states with a sub-mesh entry flagged for them are built, so an ordinary prop adds
        /// nothing; a tree adds <c>__dead</c> and <c>__stump</c>, a stockpile its fullness steps.
        /// A derived file has no skirt of its own — the base band is a property of the prefab's
        /// footprint, not of which silhouette is standing on it.
        /// </summary>
        /// <param name="item">The prefab being ripped.</param>
        /// <param name="subMeshes">Its sub-mesh entries.</param>
        /// <param name="variants">Receives the assembled variants.</param>
        private void BuildStateVariants(WorkItem item, SubMesh[] subMeshes, List<PrefabMesh> variants)
        {
            foreach (StateVariant variant in StateVariants)
            {
                if (!Authors(subMeshes, variant)) continue;

                List<int> selected = SelectSubMeshes(subMeshes, variant.State, false);
                if (selected.Count == 0) continue;

                PrefabMesh mesh = AssembleParts(item, subMeshes, selected, item.AssetId + variant.Suffix, out SkirtWork _);
                if (mesh == null) continue;

                variants.Add(mesh);
                if (variant.Tree) TreeStateVariants++; else FullnessVariants++;
            }
        }

        /// <summary>
        /// Whether a prefab authors a given alternative state at all.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <param name="variant">The state to look for.</param>
        /// <returns>Whether any entry is flagged for it.</returns>
        private static bool Authors(SubMesh[] subMeshes, StateVariant variant)
        {
            for (int i = 0; i < subMeshes.Length; i++)
            {
                if (((subMeshes[i].m_Flags & variant.Required) != 0)
                    && ((subMeshes[i].m_Flags & SubMeshFlags.OutlineOnly) == 0)) return true;
            }

            return false;
        }

        /// <summary>
        /// Assemble one chosen set of sub-mesh entries into a <see cref="PrefabMesh"/>.
        /// </summary>
        /// <param name="item">The prefab being ripped.</param>
        /// <param name="subMeshes">Its sub-mesh entries.</param>
        /// <param name="selected">The entries to assemble.</param>
        /// <param name="assetId">The id the assembled file takes.</param>
        /// <param name="skirt">The foundation skirt gathered along the way, or <c>null</c>.</param>
        /// <returns>The assembled mesh, or <c>null</c> if nothing yielded geometry.</returns>
        private PrefabMesh AssembleParts(WorkItem item, SubMesh[] subMeshes, List<int> selected, string assetId, out SkirtWork skirt)
        {
            skirt = null;

            PrefabMesh prefab = new()
            {
                AssetId = assetId,
                SourceName = item.SourceName,
                InstanceCount = item.InstanceCount,
                SkippedVariants = subMeshes.Length - selected.Count
            };

            HashSet<string> nodeNames = new();

            foreach (int index in selected)
            {
                Entity meshEntity = subMeshes[index].m_SubMesh;
                if (meshEntity == Entity.Null) continue;

                string meshName = GetPrefabName(meshEntity) ?? $"{item.AssetId}_sub{index}";
                string meshAssetId = RipAssetIds.SanitizeAssetId(meshName);

                MeshAsset mesh = ReadMesh(meshEntity, meshAssetId, meshName);
                if (mesh == null) continue;

                // Node names must be unique within the file; two entries pointing at the same
                // render prefab (a mirrored pair, say) would otherwise collide.
                //
                string nodeName = meshAssetId;
                for (int suffix = 2; !nodeNames.Add(nodeName); suffix++) nodeName = $"{meshAssetId}_{suffix}";

                PrefabMeshPart part = new()
                {
                    Mesh = mesh,
                    Position = subMeshes[index].m_Position,
                    Rotation = subMeshes[index].m_Rotation,
                    NodeName = nodeName,
                    StackRole = PrefabMesh.RoleOf(subMeshes[index].m_Flags)
                };

                // The obsolete-content placeholder — the box a prefab gets when its asset went
                // missing — is not drawn at its authored size. The renderer stretches it over
                // `ObjectGeometryData.m_Bounds` (`BatchDataSystem.UpdateObjectData`,
                // `DefaultMissingMesh` branch: translate to the bounds' centre, scale to half
                // their size), so the exported node carries the same TRS.
                //
                if (((subMeshes[index].m_Flags & SubMeshFlags.DefaultMissingMesh) != 0)
                    && EntityManager.HasComponent<ObjectGeometryData>(item.PrefabEntity))
                {
                    Colossal.Mathematics.Bounds3 bounds = EntityManager.GetComponentData<ObjectGeometryData>(item.PrefabEntity).m_Bounds;
                    part.Position += (bounds.min + bounds.max) * 0.5f;
                    part.Scale = (bounds.max - bounds.min) * 0.5f;
                    PlaceholderParts++;
                }

                prefab.Parts.Add(part);

                // A part whose MeshData carries `MeshFlags.Base` draws one extra generated batch:
                // the foundation skirt between its floor and the terrain. `MeshSystem` sets the
                // flag for a render prefab declaring `BaseProperties`, and
                // `ObjectInitializeSystem` grants it to every building mesh even without one, so
                // the ECS state — not the managed component — is the authoritative gate. The
                // silhouette scan itself runs on the writer task; here only the inputs are
                // gathered, including the base surface, which must be resolved on the main thread
                // while the material window is open.
                //
                if (EntityManager.HasComponent<MeshData>(meshEntity))
                {
                    MeshData meshData = EntityManager.GetComponentData<MeshData>(meshEntity);
                    if ((meshData.m_State & MeshFlags.Base) != 0)
                    {
                        skirt ??= new SkirtWork
                        {
                            AssetId = $"{item.AssetId}__base",
                            SourceName = item.SourceName,
                            InstanceCount = item.InstanceCount
                        };

                        skirt.Parts.Add(new SkirtPart
                        {
                            Source = mesh,
                            BaseOffset = ((meshData.m_State & MeshFlags.MinBounds) != 0) ? meshData.m_Bounds.min.y : 0f,
                            Position = part.Position,
                            Rotation = part.Rotation,
                            NodeName = $"{nodeName}__base",
                            MaterialId = ResolveBaseMaterial(meshEntity)
                        });
                    }
                }
            }

            return (prefab.Parts.Count > 0) ? prefab : null;
        }
    }
}
