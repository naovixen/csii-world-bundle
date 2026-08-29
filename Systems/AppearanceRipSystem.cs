using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// The system that writes the two things a render prefab knows about its own appearance that
    /// its geometry does not carry: the colours it may be painted, and the light it casts.
    /// <b>Palettes and procedural (emissive) lights hang off the sub-mesh, not the object.</b>
    /// That is not a guess: a census over 4,427 referenced prefabs in a full city found the colour
    /// palette on 3,984 <i>sub-meshes</i> covering 1,132,295 instances and on <i>no</i> object
    /// prefab at all, and procedural lights on 1,184 sub-meshes covering 153,548 instances,
    /// likewise none at the object level. Building either against the object prefab would have
    /// silently produced an empty file. Keyed by the sub-mesh's asset id, which is the glTF node
    /// name inside the owning prefab's <c>.glb</c> — so a consumer joins a node to its palette and
    /// its lights by name.
    /// <b>Punctual HDRP lights (street lamps, floodlights) are the opposite case</b> (audit P8):
    /// they live in the <i>object</i> prefab's <c>Effect</c> buffer, each entry placing a
    /// referenced EffectPrefab that carries the photometry. Those entries are keyed by the object
    /// prefab's asset id — the same id <c>instances.jsonl</c> references — and marked
    /// <c>kind: "punctual"</c> so the two families stay distinguishable in one file.
    /// </summary>
    public partial class AppearanceRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed objects.
        /// <para>
        /// <b>It is deliberately NOT <see cref="RipQueries.WorldObjects"/>, and the difference is
        /// visible in the output.</b> <i>(Recorded 2026-08-14, H12 — this comment used to claim it
        /// matched the instance rip, and it does not.)</i> This one excludes only
        /// <c>Deleted</c>/<c>Temp</c>/<c>Marker</c>; the instance and mesh passes also exclude
        /// <c>Moving</c>, <c>Creature</c> and <c>Vehicle</c>. So the prefab set here is a
        /// <b>superset</b>, and the tables it writes carry entries for geometry the bundle
        /// deliberately does not ship — measured on the reference city, <b>184 of 3,620</b>
        /// <c>colors.json</c> keys and <b>86 of 1,186</b> <c>lights.json</c> keys match no mesh
        /// file or glTF node name, almost all of them vehicles and tree <c>*Child*</c> states.
        /// They are harmless to a consumer (a lookup simply misses) and the validator reports the
        /// count as a note.
        /// </para>
        /// <para>
        /// <b>The latent hazard is the asset id, not the dead keys.</b>
        /// <see cref="RipAssetIds.BuildAssetIds"/> disambiguates a shared name only against the
        /// set it is handed, so a name shared between a building and a vehicle would be
        /// digest-suffixed here and left bare in the mesh layer, and the punctual-light key would
        /// stop joining. Not live: <b>zero</b> keys in either table are digest-suffixed today,
        /// against 7 mesh stems that are. Narrowing this query would close it — and would also
        /// drop those 270 keys, which is an output change, so it waits for a rip that can prove
        /// it rather than riding a refactor.
        /// </para>
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            _objectQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Objects.Object>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>(),
                    ComponentType.ReadOnly<Game.Objects.Marker>()
                }
            });

            base.OnCreate();
            _log.Debug("AppearanceRipSystem instance created.");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// </summary>
        protected override void OnDestroy() { base.OnDestroy(); }

        /// <summary>
        /// The event triggered when the system instance is updated.
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// Write <c>colors.json</c> and <c>lights.json</c>.
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.</param>
        /// <param name="colors">Whether <c>colors.json</c> is written (the ripper tab's advanced toggle).</param>
        /// <param name="lightEmitters">Whether <c>lights.json</c> is written.</param>
        /// <returns>A human-readable report.</returns>
        public string RipAppearance(string outputDirectory, bool colors = true, bool lightEmitters = true)
        {
            if (!colors && !lightEmitters) return "Colors and lights both deselected — nothing to rip.";

            StringBuilder report = new();
            Dictionary<string, object> palettes = new();
            Dictionary<string, object> lights = new();

            // The referenced prefab set, collected whole before any key is derived: asset ids
            // must be assigned over the entire set at once (prefab names are not unique), and
            // the punctual-light keys below must equal the assetIds instances.jsonl carries.
            //
            List<Entity> prefabs = new();
            HashSet<Entity> seen = new();

            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);
            try
            {
                foreach (Entity entity in entities)
                {
                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab != Entity.Null) && seen.Add(prefab)) prefabs.Add(prefab);
                }
            }
            finally
            {
                entities.Dispose();
            }

            Dictionary<Entity, RipAssetIds.AssetIdEntry> assetIds =
                RipAssetIds.BuildAssetIds(EntityManager, prefabs, out _);

            int punctualPrefabs = 0, punctualEmitters = 0;

            foreach (Entity prefab in prefabs)
            {
                // Punctual HDRP lights hang off the *object* prefab's Effect buffer, never off a
                // sub-mesh — see ReadPlacedLights. Keyed by the prefab's assetId, the join to
                // instances.jsonl and the meshes/ layer.
                //
                if (lightEmitters && EntityManager.HasBuffer<Effect>(prefab))
                {
                    List<object> placed = ReadPlacedLights(prefab);
                    if (placed.Count > 0)
                    {
                        string prefabId = assetIds[prefab].AssetId;
                        if (lights.TryGetValue(prefabId, out object existing) && (existing is List<object> merged))
                            merged.AddRange(placed);
                        else
                            lights[prefabId] = placed;

                        punctualPrefabs++;
                        punctualEmitters += placed.Count;
                    }
                }

                if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefab, true);
                for (int i = 0; i < subMeshes.Length; i++)
                {
                    Entity mesh = subMeshes[i].m_SubMesh;
                    if ((mesh == Entity.Null) || !seen.Add(mesh)) continue;

                    string id = RipAssetIds.SanitizeAssetId(Instance.Name.GetPrefabName(mesh));
                    if (string.IsNullOrEmpty(id)) continue;

                    if (colors && EntityManager.HasBuffer<ColorVariation>(mesh) && !palettes.ContainsKey(id))
                    {
                        List<object> variations = ReadPalette(mesh);
                        if (variations.Count > 0) palettes[id] = variations;
                    }

                    if (lightEmitters && EntityManager.HasBuffer<ProceduralLight>(mesh) && !lights.ContainsKey(id))
                    {
                        List<object> emitters = ReadLights(mesh);
                        if (emitters.Count > 0) lights[id] = emitters;
                    }
                }
            }

            // A deselected table is never written — an earlier rip's file stays as it was, by the
            // same rule the network halves follow.
            if (colors) Write(IOUtils.CombinePath(outputDirectory, "colors.json"), palettes);
            if (lightEmitters) Write(IOUtils.CombinePath(outputDirectory, "lights.json"), lights);

            report.AppendLine($"Wrote {(colors ? $"colors.json ({palettes.Count} palette(s))" : "no colors (deselected)")} "
                            + $"and {(lightEmitters ? $"lights.json ({lights.Count} emitter set(s))" : "no lights (deselected)")}");
            if (lightEmitters && (punctualEmitters > 0))
                report.AppendLine($"  {punctualEmitters} placed punctual light(s) on {punctualPrefabs} object prefab(s) (kind: punctual)");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Read a sub-mesh's colour variations.
        /// Each variation is CS2's <c>ColorSet</c>: three channels, which the material's mask map
        /// selects between. The probability is carried because it is what CS2 weights its own
        /// random choice by — a consumer re-colouring a city rather than reproducing this one
        /// needs it, and it costs one number.
        /// </summary>
        /// <param name="mesh">The render prefab.</param>
        /// <returns>The variations.</returns>
        private List<object> ReadPalette(Entity mesh)
        {
            DynamicBuffer<ColorVariation> buffer = EntityManager.GetBuffer<ColorVariation>(mesh, true);
            List<object> variations = new(buffer.Length);

            for (int i = 0; i < buffer.Length; i++)
            {
                ColorVariation variation = buffer[i];
                variations.Add(new Dictionary<string, object>
                {
                    ["channels"] = new[]
                    {
                        Channel(variation.m_ColorSet.m_Channel0),
                        Channel(variation.m_ColorSet.m_Channel1),
                        Channel(variation.m_ColorSet.m_Channel2)
                    },
                    ["probability"] = variation.m_Probability
                });
            }

            return variations;
        }


        /// <summary>
        /// A colour as the contract writes them: linear RGBA, four numbers.
        /// </summary>
        /// <param name="color">The colour.</param>
        /// <returns>The RGBA array.</returns>
        private static double[] Channel(Color color) => new double[]
        {
            System.Math.Round(color.r, 4), System.Math.Round(color.g, 4),
            System.Math.Round(color.b, 4), System.Math.Round(color.a, 4)
        };

        private static double[] Channel(Unity.Mathematics.float4 color) => new double[]
        {
            System.Math.Round(color.x, 4), System.Math.Round(color.y, 4),
            System.Math.Round(color.z, 4), System.Math.Round(color.w, 4)
        };

        /// <summary>
        /// Write a table, or remove a stale one when there is nothing to say.
        /// </summary>
        /// <param name="path">Where to write.</param>
        /// <param name="table">The table.</param>
        private static void Write(string path, Dictionary<string, object> table)
        {
            if (table.Count == 0)
            {
                if (System.IO.File.Exists(path)) System.IO.File.Delete(path);
                return;
            }

            Carto.IO.JsonFile.Write(path, table);
        }
    }
}
