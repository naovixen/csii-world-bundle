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
    /// The sub-object exclusions that keep a rip to the city.
    /// </summary>
    /// <remarks>
    /// A utility marker and a vehicle's cargo are both sub-objects of something placed, and
    /// neither is scenery — an owner test drops the cargo props riding vehicles, which would
    /// otherwise arrive as buildings parked mid-road.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Give every primitive of a freshly read mesh a material id that resolves.
        /// The real id comes from the render prefab's surface assets, which are ripped — textures
        /// and all — the first time each is seen. Where a surface does not resolve, the synthetic
        /// <c>&lt;assetId&gt;_mat&lt;n&gt;</c> id stays and is <i>declared</i> in the material table
        /// as an entry with no maps. Neither dropping the material nor leaving the name dangling is
        /// acceptable: §6 of the contract requires a glTF material's name to be a key in
        /// <c>materials.json</c>, so an undeclared placeholder is exactly the unresolvable
        /// reference the validator is there to catch.
        /// </summary>
        /// <param name="mesh">The mesh whose primitives to relabel.</param>
        /// <param name="renderPrefab">The render prefab holding the surface assets.</param>
        private void AssignMaterials(MeshAsset mesh, RenderPrefab renderPrefab)
        {
            if (mesh == null) return;

            // A mesh the game reported no drawable ranges for still draws with slot 0, and glTF
            // still needs one primitive covering the whole index buffer. Making that explicit here
            // rather than leaving it to the writer is what lets its material be declared at all —
            // the writer runs on a background task, long after the material table is reachable.
            //
            if ((mesh.Primitives.Count == 0) && (mesh.Indices != null) && (mesh.Indices.Length >= 3))
            {
                mesh.Primitives.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = mesh.Indices.Length - (mesh.Indices.Length % 3),
                    StartVertex = 0,
                    VertexCount = mesh.Vertices?.Length ?? 0,
                    MaterialIndex = 0,
                    MaterialId = $"{mesh.AssetId}_mat0"
                });
            }

            if (!Instance.MaterialRip.IsRipping) return;

            foreach (MeshPrimitive primitive in mesh.Primitives)
            {
                string materialId = Instance.MaterialRip.Resolve(renderPrefab, primitive.MaterialIndex);

                if (!string.IsNullOrEmpty(materialId)) primitive.MaterialId = materialId;
                else Instance.MaterialRip.Declare(primitive.MaterialId,
                    (renderPrefab == null) ? "no render prefab behind the mesh entity"
                                           : $"surface asset {primitive.MaterialIndex} did not resolve");
            }
        }

        /// <summary>
        /// Resolve a prefab entity's name, or <c>null</c> when it cannot be resolved.
        /// </summary>
        /// <param name="entity">The prefab entity.</param>
        /// <returns>The prefab name.</returns>
        private string GetPrefabName(Entity entity)
        {
            if (!EntityManager.HasComponent<PrefabData>(entity)) return null;
            return Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(entity), out PrefabBase prefab) ? prefab?.name : null;
        }



        /// <summary>
        /// Whether an object is the node furniture of a buried utility network, and so is not part
        /// of the built environment the bundle describes.
        /// Every road lays hidden water, sewage and electricity along itself, as <i>marker</i>
        /// networks the player never places or sees, and each of their nodes hangs a prop off
        /// itself: <c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>.
        /// A full city has tens of thousands — the counts give it away, ~7,300 water pipe nodes
        /// against the 105 pipe segments a player actually built. The props themselves are ordinary
        /// objects, not <see cref="Marker"/>s, so the query's marker exclusion never reaches
        /// them.
        /// <b>The owner must be a <c>Game.Net.Marker</c>.</b> A real high-voltage line's nodes are
        /// not markers, and its pylons are node props exactly like these, so testing the network
        /// type alone would delete every pylon in the city.
        /// <b>Buildings hang the same furniture with themselves as owner</b>, so the owner test
        /// alone is not the whole story: every lot's water, sewage and cable connection nodes have
        /// the building as <c>Owner</c> and no marker net anywhere in the chain. Those are caught
        /// by <see cref="IsUndergroundOnlyPrefab"/> — the game's own draw-layer rule — which is
        /// checked first.
        /// <b>For the owner test itself, one exception.</b> An earlier version also required
        /// the owner's prefab to carry <c>PipelineData</c> or <c>PowerLineData</c>, which is what
        /// let the buried nodes through: those two components come only from
        /// <c>PipelinePrefab</c> and <c>PowerLinePrefab</c>
        /// (<c>GetPrefabComponents</c>), and the marker nets a road lays along itself are not
        /// those classes. The extra clause could only ever narrow a test that was already exact —
        /// <c>Game.Net.Marker</c> is added by <c>MarkerNet.GetArchetypeComponents</c> and by
        /// nothing else in the game, to the node archetype and the edge archetype alike, so an
        /// owner carrying it <i>is</i> a marker net by construction.
        /// <b>The exception is taxiways</b>, which are marker nets and yet are genuinely drawn —
        /// <see cref="NetworkRipSystem"/> already makes the same exception. They are named directly
        /// by <c>TaxiwayData</c>, which <c>TaxiwayPrefab.GetPrefabComponents</c> is the only source
        /// of, rather than indirectly by listing the network types that are not taxiways.
        /// <b>Both rip systems must apply this identically.</b> The asset ids are assigned over the
        /// prefab set as a whole, so a set that differs between the two layers can shift a
        /// disambiguating suffix and break the join — see <see cref="BuildAssetIds"/>.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether to leave it out of the bundle.</returns>
        public static bool IsUtilityMarkerSubObject(EntityManager entityManager, Entity entity)
        {
            // The owner test alone proved incomplete in the field: a full-city rip still carried
            // 29,648 pipe and cable node props whose owner is the *building* whose lot spawns
            // them, not a marker net — buildings hang their own utility connection furniture as
            // sub-objects, and those owners carry no `Game.Net.Marker`. The prefab test below is
            // the game's own draw rule and catches both parents.
            //
            if (IsUndergroundOnlyPrefab(entityManager, entity)) return true;

            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if ((owner == Entity.Null) || !entityManager.HasComponent<Game.Net.Marker>(owner)) return false;
            if (!entityManager.HasComponent<PrefabRef>(owner)) return false;

            Entity ownerPrefab = entityManager.GetComponentData<PrefabRef>(owner).m_Prefab;
            if (ownerPrefab == Entity.Null) return false;

            // Drawn, despite being a marker net. The only such case the game has.
            //
            return !entityManager.HasComponent<TaxiwayData>(ownerPrefab);
        }

        /// <summary>
        /// Whether an object is cargo riding a vehicle — a log pile on a truck trailer, crates on
        /// a flatbed — rather than scenery standing in the world.
        /// The queries already exclude vehicles and creatures themselves, but cargo props are
        /// separate <i>sub-object</i> entities owned by the vehicle, and nothing about the prop
        /// itself says "agent": it is an ordinary object with a transform and a prefab. The tell is
        /// its <c>Owner</c> — the same one-level link the utility-marker test reads. A full-city rip
        /// carried ~590 such records (<c>TruckTrailer03_LogPile01</c>, <c>CarTrailer01_Props01</c>, …)
        /// as world scenery before this test existed.
        /// Every vehicle archetype carries <c>Game.Vehicles.Vehicle</c>
        /// (<c>VehiclePrefab.GetArchetypeComponents</c> adds it unconditionally, and
        /// <c>CarTrailerPrefab</c> derives from it through <c>CarBasePrefab</c>), so the single
        /// component test covers trailers too; <c>CarTrailer</c> is tested as well only so the rule
        /// reads as what it means and survives an archetype reshuffle.
        /// <b>Both rip systems must apply this identically</b>, for the same asset-id join reason as
        /// <see cref="IsUtilityMarkerSubObject"/>.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether to leave it out of the bundle.</returns>
        public static bool IsVehicleCargoSubObject(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if (owner == Entity.Null) return false;

            return entityManager.HasComponent<Game.Vehicles.Vehicle>(owner)
                || entityManager.HasComponent<Game.Vehicles.CarTrailer>(owner);
        }

        /// <summary>
        /// Whether an object's prefab draws only on the underground utility layers, and so is never
        /// visible in normal rendering.
        /// The game's rule, from <c>BatchInstanceSystem.UpdateObjectInstances</c>: a sub-mesh whose
        /// <c>MeshData.m_DefaultLayers</c> is non-zero has its batch layer replaced by those
        /// defaults, and <c>Pipeline</c>/<c>SubPipeline</c> map to camera layers only the
        /// underground cutaway view renders. A prefab all of whose sub-meshes live there — the
        /// pipe and cable connection nodes — therefore draws nothing in the world view, exactly
        /// like the Pipeline-layer lane parts the lane catalogue already excludes. A prefab with
        /// even one ordinary sub-mesh keeps drawing and is kept: the test is every-part, not
        /// any-part.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether every sub-mesh is underground-only.</returns>
        private static bool IsUndergroundOnlyPrefab(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<PrefabRef>(entity)) return false;

            Entity prefab = entityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            if ((prefab == Entity.Null) || !entityManager.HasBuffer<SubMesh>(prefab)) return false;

            DynamicBuffer<SubMesh> subMeshes = entityManager.GetBuffer<SubMesh>(prefab, true);
            if (subMeshes.Length == 0) return false;

            const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
            foreach (SubMesh subMesh in subMeshes)
            {
                if (!entityManager.HasComponent<MeshData>(subMesh.m_SubMesh)) return false;

                MeshLayer layers = entityManager.GetComponentData<MeshData>(subMesh.m_SubMesh).m_DefaultLayers;
                if ((layers == 0) || ((layers & ~underground) != 0)) return false;
            }

            return true;
        }
    }
}
