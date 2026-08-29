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
    /// What the instance stage says it did: the census, the profile, and the §10 gaps.
    /// </summary>
    public partial class InstanceRipSystem
    {
        /// <summary>
        /// Count what the world is made of, by the flags CS2 renders it with.
        /// This exists because "is anything else like the decals?" is a question that should be
        /// answered with numbers rather than by reading names and guessing. A decal's mesh is a
        /// projector volume, not a marking, and nothing in the bundle said so until it was looked
        /// for; the same could be true of impostors, of animated or skinned meshes, or of lights.
        /// Each is one flag, and a census over the prefab set is a few thousand lookups — nothing
        /// against a rip, and it turns the next question into a table.
        /// Counted by instance as well as by prefab: a flag on one prefab used 34,000 times is a
        /// different problem from the same flag on a prefab used twice.
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.
        ///</param>
        /// <param name="report">The report to append the table to.</param>
        /// <summary>
        /// Print, for the prefabs the city holds most of, what a placed one and its prefab are
        /// actually made of.
        /// <b>Why this exists.</b> "Stop these things being ripped" is a filter, and a filter needs
        /// a component to test. Three separate attempts to exclude the buried utility hookups
        /// (<c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>) were
        /// written against components those objects turned out not to have — the last one tested
        /// the <i>owner</i> for <c>Game.Net.Marker</c>, and their owner is the building whose lot
        /// they sit under, not a net at all. Each attempt looked plausible, compiled, ran, reported
        /// a count, and changed nothing.
        /// The list of component names on one instance and on its prefab is what would have ended
        /// that on the first attempt, and it costs two lookups per prefab printed.
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.</param>
        /// <param name="prefabSample">One placed instance per prefab.</param>
        /// <param name="report">The report to append the profile to.</param>
        private void Profile(Dictionary<Entity, int> prefabUse, Dictionary<Entity, Entity> prefabSample,
                             StringBuilder report)
        {
            const int Top = 12;

            List<KeyValuePair<Entity, int>> ranked = new(prefabUse);
            ranked.Sort((a, b) => b.Value.CompareTo(a.Value));

            report.AppendLine($"What the {Math.Min(Top, ranked.Count)} most-placed prefab(s) are made of "
                            + "— the component names a filter can test:");

            for (int i = 0; (i < Top) && (i < ranked.Count); i++)
            {
                Entity prefab = ranked[i].Key;
                string name = Instance.Name.GetPrefabName(prefab);
                report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {prefab.Index}" : name)} × {ranked[i].Value}");

                if (prefabSample.TryGetValue(prefab, out Entity sample))
                    report.AppendLine($"    instance: {string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, sample))}");

                report.AppendLine($"    prefab:   {string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, prefab))}");

                // The owner is where the last filter went wrong, so name what it actually is.
                //
                if (prefabSample.TryGetValue(prefab, out Entity owned)
                    && EntityManager.HasComponent<Game.Common.Owner>(owned))
                {
                    Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(owned).m_Owner;
                    if (owner != Entity.Null)
                    {
                        string ownerName = EntityManager.HasComponent<PrefabRef>(owner)
                            ? Instance.Name.GetPrefabName(EntityManager.GetComponentData<PrefabRef>(owner).m_Prefab)
                            : null;

                        report.AppendLine($"    owner:    {(string.IsNullOrEmpty(ownerName) ? "?" : ownerName)}"
                                        + $" [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, owner))}]");
                    }
                }
            }
        }

        private void Census(Dictionary<Entity, int> prefabUse, StringBuilder report)
        {
            (string Name, MeshFlags Flag)[] meshFlags =
            {
                ("decal", MeshFlags.Decal), ("impostor", MeshFlags.Impostor),
                ("animated", MeshFlags.Animated), ("skeleton", MeshFlags.Skeleton),
                ("character", MeshFlags.Character), ("prop", MeshFlags.Prop),
                ("stackX", MeshFlags.StackX), ("stackY", MeshFlags.StackY), ("stackZ", MeshFlags.StackZ),

                // The generated foundation skirt (`BaseProperties`): the game draws one extra
                // batch past the last sub-mesh for these, which the bundle does not yet carry —
                // the census is what says how much of the city that omission touches.
                //
                ("base", MeshFlags.Base)
            };

            (string Name, SubMeshFlags Flag)[] subMeshFlags =
            {
                ("outlineOnly", SubMeshFlags.OutlineOnly),
                ("stackStart", SubMeshFlags.IsStackStart),
                ("stackMiddle", SubMeshFlags.IsStackMiddle),
                ("stackEnd", SubMeshFlags.IsStackEnd),
                ("defaultMissingMesh", SubMeshFlags.DefaultMissingMesh)
            };

            Dictionary<string, (int Prefabs, int Instances)> tally = new();

            void Note(string name, int instances)
            {
                tally.TryGetValue(name, out (int Prefabs, int Instances) had);
                tally[name] = (had.Prefabs + 1, had.Instances + instances);
            }

            foreach (KeyValuePair<Entity, int> pair in prefabUse)
            {
                Entity prefab = pair.Key;
                int instances = pair.Value;

                // Light sources are components on the object prefab, not mesh flags: a lamp's
                // housing is ordinary geometry and the light it casts is separate data, carried
                // by lights.json (procedural per sub-mesh, punctual per object prefab).
                //
                // Per-instance colour: the prefab holds a palette of variations
                // (colors.json) and the game resolves each placed object's paint into its
                // `MeshColor` buffer, which `AddAttributes` exports as `attrs.colorSet`.
                //
                // Checked on the object prefab and on its sub-meshes, because which of the two
                // holds the palette decides where a colour export would read from, and guessing
                // is how a whole feature gets built against the wrong entity.
                //
                if (EntityManager.HasBuffer<ColorVariation>(prefab)) Note("color:palette@prefab", instances);

                if (EntityManager.HasBuffer<ProceduralLight>(prefab)) Note("light:procedural", instances);
                if (EntityManager.HasComponent<LightEffectData>(prefab)) Note("light:effect", instances);
                if (EntityManager.HasComponent<StreetLightData>(prefab)) Note("light:street", instances);
                if (EntityManager.HasComponent<TrafficLightData>(prefab)) Note("light:traffic", instances);
                if (EntityManager.HasBuffer<LightAnimation>(prefab)) Note("light:animated", instances);

                if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefab, true);
                MeshFlags meshSeen = 0;
                SubMeshFlags subSeen = 0;
                bool paletteOnMesh = false, lightOnMesh = false;

                for (int i = 0; i < buffer.Length; i++)
                {
                    subSeen |= buffer[i].m_Flags;
                    Entity mesh = buffer[i].m_SubMesh;
                    if (mesh == Entity.Null) continue;

                    if (EntityManager.HasComponent<MeshData>(mesh))
                        meshSeen |= EntityManager.GetComponentData<MeshData>(mesh).m_State;

                    if (EntityManager.HasBuffer<ColorVariation>(mesh)) paletteOnMesh = true;
                    if (EntityManager.HasBuffer<ProceduralLight>(mesh)) lightOnMesh = true;
                }

                if (paletteOnMesh) Note("color:palette@submesh", instances);
                if (lightOnMesh) Note("light:procedural@submesh", instances);

                foreach ((string name, MeshFlags flag) in meshFlags)
                    if ((meshSeen & flag) != 0) Note("mesh:" + name, instances);

                foreach ((string name, SubMeshFlags flag) in subMeshFlags)
                    if ((subSeen & flag) != 0) Note("sub:" + name, instances);
            }

            if (tally.Count == 0) return;

            report.AppendLine($"Flag census over {prefabUse.Count} referenced prefab(s) — "
                            + "what else might need separating:");

            List<KeyValuePair<string, (int Prefabs, int Instances)>> rows = new(tally);
            rows.Sort((a, b) => b.Value.Instances.CompareTo(a.Value.Instances));
            foreach (KeyValuePair<string, (int Prefabs, int Instances)> row in rows)
                report.AppendLine($"  {row.Key,-24} {row.Value.Prefabs,6} prefab(s) {row.Value.Instances,9} instance(s)");
        }
    }
}
