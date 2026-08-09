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
    /// <b>Both hang off the sub-mesh, not the object.</b> That is not a guess: a census over 4,427
    /// referenced prefabs in a full city found the colour palette on 3,984 <i>sub-meshes</i>
    /// covering 1,132,295 instances and on <i>no</i> object prefab at all, and procedural lights on
    /// 1,184 sub-meshes covering 153,548 instances, likewise none at the object level. Building
    /// either against the object prefab would have silently produced an empty file.
    /// Keyed by the sub-mesh's asset id, which is the glTF node name inside the owning prefab's
    /// <c>.glb</c> — so a consumer joins a node to its palette and its lights by name.
    /// </summary>
    public partial class AppearanceRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed objects, matching the instance rip so the two agree on which
        /// prefabs the city actually uses.
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

            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);
            HashSet<Entity> seen = new();

            try
            {
                foreach (Entity entity in entities)
                {
                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !seen.Add(prefab)) continue;
                    if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                    DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefab, true);
                    for (int i = 0; i < subMeshes.Length; i++)
                    {
                        Entity mesh = subMeshes[i].m_SubMesh;
                        if ((mesh == Entity.Null) || !seen.Add(mesh)) continue;

                        string id = MeshRipSystem.SanitizeAssetId(Instance.Name.GetPrefabName(mesh));
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
            }
            finally
            {
                entities.Dispose();
            }

            // A deselected table is never written — an earlier rip's file stays as it was, by the
            // same rule the network halves follow.
            if (colors) Write(IOUtils.CombinePath(outputDirectory, "colors.json"), palettes);
            if (lightEmitters) Write(IOUtils.CombinePath(outputDirectory, "lights.json"), lights);

            report.AppendLine($"Wrote {(colors ? $"colors.json ({palettes.Count} palette(s))" : "no colors (deselected)")} "
                            + $"and {(lightEmitters ? $"lights.json ({lights.Count} emitter set(s))" : "no lights (deselected)")}");

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
        /// Read a sub-mesh's procedural lights.
        /// <b>What is here and what is not.</b> The colours, the response speed and the animation
        /// index are the light's own data and are exact. The <i>position</i> is not exported,
        /// because CS2 does not store one: a procedural light binds to a material slot
        /// (<c>EmissiveProperties.SingleLightMapping.materialId</c>) and its place in the world is
        /// wherever the geometry using that material is. A consumer places each emitter at the
        /// centroid of the primitive using the corresponding material in the same <c>.glb</c>.
        /// Inventing a transform here would be inventing data, on the same grounds as §5.1.
        /// </summary>
        /// <param name="mesh">The render prefab.</param>
        /// <returns>The emitters.</returns>
        private List<object> ReadLights(Entity mesh)
        {
            DynamicBuffer<ProceduralLight> buffer = EntityManager.GetBuffer<ProceduralLight>(mesh, true);
            List<object> emitters = new(buffer.Length);

            float? range = null, temperature = null;
            if (EntityManager.HasComponent<LightEffectData>(mesh))
            {
                LightEffectData effect = EntityManager.GetComponentData<LightEffectData>(mesh);
                range = effect.m_Range;
                temperature = effect.m_ColorTemperature;
            }

            for (int i = 0; i < buffer.Length; i++)
            {
                ProceduralLight light = buffer[i];
                Dictionary<string, object> emitter = new()
                {
                    // The slot is the join to the mesh: the light is wherever this material is.
                    //
                    ["slot"] = i,
                    ["color"] = Channel(light.m_Color),
                    ["colorOff"] = Channel(light.m_Color2),
                    ["purpose"] = light.m_Purpose.ToString(),
                    ["responseSpeed"] = System.Math.Round(light.m_ResponseSpeed, 4)
                };

                if (light.m_AnimationIndex >= 0) emitter["animation"] = light.m_AnimationIndex;
                if (range.HasValue) emitter["range"] = System.Math.Round(range.Value, 3);
                if (temperature.HasValue) emitter["temperature"] = System.Math.Round(temperature.Value, 1);

                emitters.Add(emitter);
            }

            return emitters;
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

            string directory = System.IO.Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory))
                System.IO.Directory.CreateDirectory(directory);

            System.IO.File.WriteAllText(path, JsonConvert.SerializeObject(table, Formatting.Indented));
        }
    }
}
