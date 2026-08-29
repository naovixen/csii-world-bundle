using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// What KIND a placed object is, and what a decal record adds on top.
    /// </summary>
    /// <remarks>
    /// The kinds are the bundle's, not the game's: CS2 has no "tree" concept a query can ask
    /// for, so the classification is a rule this file owns and every consumer reads through
    /// <c>kind</c>.
    /// </remarks>
    public partial class InstanceRipSystem
    {
        internal string ClassifyKind(Entity entity, Entity prefabEntity)
        {
            if (_buildings.HasComponent(entity)) return "building";

            // Trees and plants are both vegetation; the contract's closed enum has no separate
            // value for shrubs, and a consumer treats them the same way.
            //
            if (_trees.HasComponent(entity) || _plants.HasComponent(entity)) return "tree";

            if (DecalOf(prefabEntity).IsDecal) return "decal";

            return "prop";
        }

        /// <summary>
        /// Whether a prefab is a decal — a road marking, a parking bay, a patch of grime.
        /// <b>Why this matters more than it looks.</b> A decal's mesh is not the marking, it is the
        /// <i>projector volume</i>: a twelve-triangle box, a metre tall, that CS2's shader projects
        /// the texture down onto whatever road surface lies inside it. A road arrow therefore
        /// arrives in a bundle as a transparent box floating over the carriageway. Left as
        /// <c>kind: "prop"</c>, a consumer building a drivable level turns thousands of those boxes
        /// into collision geometry and the road becomes impassable.
        /// The signal is <c>MeshData.m_State</c>, the same flag the renderer switches on, rather
        /// than the shader-name match the material layer uses. Cached per prefab: a city holds a
        /// million objects and a few thousand prefabs.
        /// </summary>
        /// <b>What it projects onto is the game's own answer, not a guess.</b>
        /// <c>MeshData.m_DecalLayer</c> is a <c>DecalLayers</c> mask — <c>Terrain</c>, <c>Roads</c>,
        /// <c>Buildings</c>, <c>Vehicles</c>, <c>Creatures</c>, <c>Other</c> — naming which surfaces
        /// receive the projection. Without it a consumer projecting "onto whatever is beneath"
        /// paints a road marking across the car parked on it and the wall behind it.
        /// </summary>
        /// <param name="prefabEntity">The object prefab.</param>
        /// <returns>Whether every mesh it draws is a decal, and onto what.</returns>
        private DecalInfo DecalOf(Entity prefabEntity)
        {
            if (prefabEntity == Entity.Null) return new DecalInfo(false, 0, 0f, int.MinValue);
            if (_decalPrefabs.TryGetValue(prefabEntity, out DecalInfo known)) return known;

            bool decal = false;
            Game.Rendering.DecalLayers layers = 0;
            float height = 0f;
            int priority = int.MinValue;

            if (EntityManager.HasBuffer<SubMesh>(prefabEntity))
            {
                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                int meshes = 0, decals = 0;

                for (int i = 0; i < buffer.Length; i++)
                {
                    Entity mesh = buffer[i].m_SubMesh;
                    if ((mesh == Entity.Null) || !EntityManager.HasComponent<MeshData>(mesh)) continue;

                    meshes++;
                    MeshData data = EntityManager.GetComponentData<MeshData>(mesh);
                    if ((data.m_State & MeshFlags.Decal) == 0) continue;

                    decals++;
                    layers |= data.m_DecalLayer;
                    height = math.max(height, data.m_Bounds.max.y - data.m_Bounds.min.y);

                    // `MeshData.m_DecalLayer` is *not* where the renderer reads the mask from. It
                    // is zero on every decal in a full city — all 17,370 of them — which is why
                    // `decalLayers` came out absent on every record while the field was being read
                    // and the audit called it fixed. The value the shader is actually handed comes
                    // off the managed `DecalProperties` component:
                    // `ManagedBatchSystem.CreateBatch` and
                    // `RenderPrefabRenderer.Instance.SetDecalProperties` both set `_DecalLayerMask`
                    // from `DecalProperties.m_LayerMask`. Keep the ECS field as a fallback rather
                    // than replacing it: it costs nothing and a future build may start populating it.
                    //
                    if (EntityManager.HasComponent<PrefabData>(mesh)
                        && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(mesh),
                                                        out PrefabBase meshPrefab)
                        && (meshPrefab is RenderPrefab renderPrefab))
                    {
                        try
                        {
                            if (renderPrefab.TryGet(out DecalProperties properties)
                                && (properties != null) && properties.active)
                            {
                                layers |= properties.m_LayerMask;
                                // Highest wins, so a prefab whose meshes disagree takes the
                                // topmost — that is the one that ends up on screen.
                                priority = math.max(priority, properties.m_RendererPriority);
                            }
                        }
                        catch (Exception ex)
                        {
                            _log.Warn($"Decal properties of `{renderPrefab.name}` did not resolve: {ex.Message}");
                        }
                    }
                }

                // Every drawn mesh, not merely one of them: a building with a grime decal on its
                // wall is still a building, and must keep its collision.
                //
                decal = (meshes > 0) && (decals == meshes);
            }

            DecalInfo info = new(decal, layers, height, priority);
            _decalPrefabs[prefabEntity] = info;
            return info;
        }
    }
}
