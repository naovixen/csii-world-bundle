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
    /// A stockpile's fill state, expanded into the sub-records the bundle ships.
    /// </summary>
    public partial class InstanceRipSystem
    {
        /// <summary>
        /// Classify an entity into one of the contract's closed <c>kind</c> values.
        /// </summary>
        /// <param name="entity">The object entity.</param>
        /// <returns>The kind.</returns>
        /// <summary>
        /// Emit one record per section of a stacked object, or <c>0</c> if it is not stacked.
        /// <b>What was wrong without this.</b> CS2 stores a pillar, a high wall or a long fence as
        /// a foot, one repeating section and a cap, and repeats the middle at draw time to span
        /// whatever the instance needs. <c>Vertical_Pillar</c>'s shaft is authored two metres tall;
        /// a viaduct pillar is ten or more. The bundle previously kept the single authored section,
        /// so every stacked object came out as a stub — visibly, highway pillars did not reach the
        /// ground.
        /// <b>The arithmetic is the game's, not a reimplementation.</b>
        /// <c>BatchDataHelpers.CalculateStackSubMeshData</c> is public, so the counts, offsets and
        /// per-section scales come from the same code the renderer uses. Reproducing them from a
        /// reading of the IL would have been a guess at exactly the kind of scaling term that is
        /// easy to get subtly wrong.
        /// Each section becomes its own instance record with its world transform baked, which is
        /// what contract §4.1 already does for sub-objects — so this needs no schema change and no
        /// cooperation from a consumer.
        /// </summary>
        /// <param name="entity">The placed object.</param>
        /// <param name="prefabEntity">Its prefab.</param>
        /// <param name="template">The record built for it, used as the pattern.</param>
        /// <param name="emit">Where records go.</param>
        /// <param name="assetIds">The set of referenced asset ids.</param>
        /// <returns>How many records were written.</returns>
        private int ExpandStack(Entity entity, Entity prefabEntity, InstanceRecord template,
                                Action<InstanceRecord> emit, HashSet<string> assetIds)
        {
            // The first test fires on every object in the city and almost never passes, so it is
            // the one that has to be cheap.
            //
            if (!_stacks.TryGetComponent(entity, out Game.Objects.Stack stack)) return 0;
            if (!EntityManager.HasComponent<Game.Prefabs.StackData>(prefabEntity)) return 0;
            if (!_prefabSubMeshes.HasBuffer(prefabEntity)) return 0;

            Game.Prefabs.StackData data = EntityManager.GetComponentData<Game.Prefabs.StackData>(prefabEntity);

            Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                stack, data, out int3 counts, out float3 offsets, out float3 scales);

            // Which sections this prefab actually has, and where each is authored relative to the
            // object's origin. The buffer is copied because reading it is all we need it for.
            //
            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
            List<(StackRole Role, float3 Position, quaternion Rotation)> sections = new();
            for (int i = 0; i < buffer.Length; i++)
            {
                StackRole? role = PrefabMesh.RoleOf(buffer[i].m_Flags);
                if (role.HasValue) sections.Add((role.Value, buffer[i].m_Position, buffer[i].m_Rotation));
            }

            if (sections.Count == 0) return 0;

            int written = 0;

            // Parts with no stack role are drawn once at the instance transform — the game's
            // sub-mesh test passes them regardless of the `IsStack*` bits — and they live in the
            // `__stack_rest` file the mesh pass ships, because the whole-prefab file is never
            // referenced for a stacked prefab and the stack part files must not tile them.
            //
            bool hasRest = false;
            for (int i = 0; i < buffer.Length; i++)
            {
                if (!PrefabMesh.RoleOf(buffer[i].m_Flags).HasValue
                    && MeshRipSystem.ExportsSubMesh(buffer[i].m_Flags)) { hasRest = true; break; }
            }

            if (hasRest)
            {
                string restId = PrefabMesh.StackRestId(template.AssetId);
                InstanceRecord rest = new()
                {
                    Id = template.Id,
                    AssetId = restId,
                    Kind = template.Kind,
                    Position = template.Position,
                    Rotation = template.Rotation,
                    Parent = template.Parent
                };

                foreach (KeyValuePair<string, object> attribute in template.Attributes)
                    rest.Attributes[attribute.Key] = attribute.Value;

                emit(rest);
                assetIds.Add(restId);
                written++;
            }
            foreach ((StackRole role, float3 localPosition, quaternion localRotation) in sections)
            {
                int tiles = role switch
                {
                    StackRole.First => counts.x,
                    StackRole.Middle => counts.y,
                    _ => counts.z
                };

                Game.Prefabs.SubMeshFlags flags = role switch
                {
                    StackRole.First => Game.Prefabs.SubMeshFlags.IsStackStart,
                    StackRole.Middle => Game.Prefabs.SubMeshFlags.IsStackMiddle,
                    _ => Game.Prefabs.SubMeshFlags.IsStackEnd
                };

                for (int tile = 0; tile < tiles; tile++)
                {
                    float3 position = localPosition;
                    float3 scale = new(1f);
                    Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                        data, offsets, scales, tile, flags, ref position, ref scale);

                    // The section is authored in the object's local frame, so its world placement
                    // is the object's transform applied to it.
                    //
                    string partId = PrefabMesh.StackPartId(template.AssetId, role);
                    InstanceRecord section = new()
                    {
                        Id = template.Id,
                        AssetId = partId,
                        Kind = template.Kind,
                        Position = template.Position + math.mul(template.Rotation, position),
                        Rotation = math.mul(template.Rotation, localRotation),
                        Parent = template.Parent
                    };

                    foreach (KeyValuePair<string, object> attribute in template.Attributes)
                        section.Attributes[attribute.Key] = attribute.Value;

                    // Only where the game scales: an unscaled section must not acquire a scale of
                    // exactly one that a consumer then has to trust.
                    //
                    if (math.any(math.abs(scale - new float3(1f)) > 1e-4f)) section.Scale = scale;

                    emit(section);
                    assetIds.Add(partId);
                    written++;
                }
            }

            return written;
        }
    }
}
