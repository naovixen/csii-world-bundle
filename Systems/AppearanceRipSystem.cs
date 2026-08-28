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
        /// Read a sub-mesh's procedural (emissive) lights.
        /// <b>What is here and what is not.</b> The colours, the response speed and the animation
        /// index are the light's own data and are exact. The <i>position</i> is not exported,
        /// because CS2 does not store one for these: a procedural light binds to a material slot
        /// (<c>EmissiveProperties.SingleLightMapping.materialId</c>) and its place in the world is
        /// wherever the geometry using that material is. <b>That id is now shipped</b> as
        /// <c>materialId</c> (and <c>layerId</c> for the multi lights, which come first in the
        /// buffer), so a consumer joins a slot to its material outright instead of matching them
        /// by ordinal — the ordinal is exact on 639 of the reference city's 781 slot-carrying
        /// meshes and a guess on the other 142.
        /// Inventing a transform here would be inventing data, on the same grounds as §5.1.
        /// (A previous version also read <c>LightEffectData</c> off this mesh entity for a range
        /// and temperature — audit P8's latent bug: the component only ever exists on an
        /// EffectPrefab, so no entry ever carried either. Punctual lights, which do have a
        /// position and photometry, are <see cref="ReadPlacedLights"/>.)
        /// </summary>
        /// <param name="mesh">The render prefab.</param>
        /// <returns>The emitters.</returns>
        private List<object> ReadLights(Entity mesh)
        {
            DynamicBuffer<ProceduralLight> buffer = EntityManager.GetBuffer<ProceduralLight>(mesh, true);
            List<object> emitters = new(buffer.Length);

            // The authored mapping, which is where the JOIN lives and the ECS buffer does not
            // carry it. `MeshSystem` fills the buffer m_MultiLights FIRST and m_SingleLights
            // after, so slot `i` is `m_MultiLights[i]` while `i` is under the multi count and
            // `m_SingleLights[i - multiCount]` beyond it — and those two name different things:
            // a MultiLightMapping names a `layerId` (a material layer of the mesh), a
            // SingleLightMapping names a `materialId` (one material of the mesh, which is the
            // game's own answer to "where is this emissive light"). Without them a consumer can
            // only match slots to emissive materials by ordinal, which works on 81.8 % of the
            // reference city's slot-carrying meshes and is a guess on the rest.
            //
            List<EmissiveProperties.MultiLightMapping> multi = null;
            List<EmissiveProperties.SingleLightMapping> single = null;
            if (EntityManager.HasComponent<PrefabData>(mesh)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(mesh),
                                                out PrefabBase meshPrefab)
                && meshPrefab.TryGet(out EmissiveProperties emissive))
            {
                multi = emissive.m_MultiLights;
                single = emissive.m_SingleLights;
            }

            int multiCount = multi?.Count ?? 0;

            for (int i = 0; i < buffer.Length; i++)
            {
                ProceduralLight light = buffer[i];
                Dictionary<string, object> emitter = new()
                {
                    // The slot is the buffer index — the game's own light id, and what
                    // `LightState` is indexed by.
                    //
                    ["slot"] = i,
                    ["color"] = Channel(light.m_Color),
                    ["colorOff"] = Channel(light.m_Color2),
                    ["purpose"] = light.m_Purpose.ToString(),
                    ["responseSpeed"] = System.Math.Round(light.m_ResponseSpeed, 4)
                };

                if ((i < multiCount) && (multi[i] != null))
                {
                    emitter["layerId"] = multi[i].layerId;
                }
                else if ((single != null) && (i - multiCount < single.Count) && (single[i - multiCount] != null))
                {
                    emitter["materialId"] = single[i - multiCount].materialId;
                }

                if (light.m_AnimationIndex >= 0) emitter["animation"] = light.m_AnimationIndex;

                emitters.Add(emitter);
            }

            return emitters;
        }

        /// <summary>
        /// Read the placed punctual lights off an object prefab's <c>Effect</c> buffer (audit P8).
        /// <b>Where a punctual light actually lives.</b> A street lamp's cone is not sub-mesh
        /// data: <c>EffectSource</c> bakes each authored effect into the object prefab's
        /// <c>Effect</c> buffer — local position/rotation offset, per-placement intensity, parent
        /// sub-mesh index — and the photometry sits on the referenced <i>EffectPrefab</i>: range
        /// as <c>LightEffectData</c>, the resolved colour as <c>EffectColorData</c>, and the
        /// type, spot angle, temperature and unit only on the managed <c>LightEffect</c>
        /// component, which is exactly where <c>LightCullingSystem</c> reads them
        /// (<c>GetPrefab&lt;EffectPrefab&gt;().GetComponent&lt;LightEffect&gt;()</c>).
        /// <b>Offsets are in the object's own frame</b>, and <c>parentMesh</c> does not change
        /// that. <i>(Corrected 2026-08-14, H10 — this said "local to the sub-mesh named by
        /// <c>parentMesh</c> when that is ≥ 0", which is what contract §7.2 said too.)</i> A
        /// sweep of every reader of <c>Effect.m_ParentMesh</c> in the managed assemblies finds
        /// three, and not one of them places the light: <c>EffectTransformSystem</c> reads it
        /// only to pick which sub-mesh's <c>MeshColor</c> the light takes (the
        /// <c>colorChannel</c> case below), <c>SubObjectSystem</c> reads it as sub-object
        /// elevation flags, and <c>BuildingInitializeSystem</c> for fire SFX. What actually
        /// places the light is <c>GetEffectTransform</c>, which branches on
        /// <c>m_BoneIndex</c> — see <c>boneIndex</c> below.
        /// </summary>
        /// <param name="prefab">The object prefab.</param>
        /// <returns>The placed emitters, marked <c>kind: "punctual"</c>.</returns>
        private List<object> ReadPlacedLights(Entity prefab)
        {
            DynamicBuffer<Effect> effects = EntityManager.GetBuffer<Effect>(prefab, true);
            List<object> emitters = new();

            for (int i = 0; i < effects.Length; i++)
            {
                Effect effect = effects[i];
                if (effect.m_Effect == Entity.Null) continue;

                // Runtime-generated destruction effects (collapse dust, fire SFX) added by
                // BuildingInitializeSystem — not authored light placements.
                //
                if (effect.m_Procedural) continue;

                // The same buffer carries VFX and audio; only light effects are wanted here.
                //
                if (!EntityManager.HasComponent<LightEffectData>(effect.m_Effect)) continue;

                LightEffectData data = EntityManager.GetComponentData<LightEffectData>(effect.m_Effect);

                Dictionary<string, object> emitter = new()
                {
                    ["kind"] = "punctual",
                    ["effect"] = Instance.Name.GetPrefabName(effect.m_Effect),
                    ["position"] = new double[]
                    {
                        System.Math.Round((double)effect.m_Position.x, 4),
                        System.Math.Round((double)effect.m_Position.y, 4),
                        System.Math.Round((double)effect.m_Position.z, 4)
                    },
                    ["rotation"] = new double[]
                    {
                        System.Math.Round((double)effect.m_Rotation.value.x, 6),
                        System.Math.Round((double)effect.m_Rotation.value.y, 6),
                        System.Math.Round((double)effect.m_Rotation.value.z, 6),
                        System.Math.Round((double)effect.m_Rotation.value.w, 6)
                    },
                    ["parentMesh"] = effect.m_ParentMesh,
                    ["range"] = System.Math.Round((double)data.m_Range, 3)
                };

                // The one thing that changes what `position` MEANS, and the bundle carried no
                // trace of it. `EffectInitializeSystem` resolves `m_ParentMesh` through
                // `RenderingUtils.FindBoneIndex`, which — when it matches a ProceduralBone
                // connection id — transforms `m_Position`/`m_Rotation` into that bone's
                // bind-pose space in place and records the bone in `m_BoneIndex`. So an effect
                // with `m_BoneIndex.x >= 0` ships a position that is NOT object-frame, and a
                // consumer placing it in the object frame puts the light in the wrong spot with
                // nothing in the file to say so. Emitted only in that case, because `.x < 0` is
                // the ordinary object-frame light and an always-present -1 pair would imply the
                // bundle carries bone data it does not. At most 4 of 654 punctual lights on the
                // reference city.
                //
                if (effect.m_BoneIndex.x >= 0)
                    emitter["boneIndex"] = new[] { effect.m_BoneIndex.x, effect.m_BoneIndex.y };

                if (System.Math.Abs(effect.m_Intensity - 1f) > 1e-4f)
                    emitter["intensityScale"] = System.Math.Round((double)effect.m_Intensity, 4);

                // The resolved colour the renderer is handed — authored colour × photometric
                // intensity × temperature × dimmer, linear HDR. `colorChannel` marks lights that
                // take the owning instance's colorSet channel instead (EffectTransformSystem
                // swaps it in when EffectColorData.m_Source is set and parentMesh is valid).
                //
                if (EntityManager.HasComponent<EffectColorData>(effect.m_Effect))
                {
                    EffectColorData colorData = EntityManager.GetComponentData<EffectColorData>(effect.m_Effect);
                    emitter["finalColor"] = Channel(colorData.m_Color);
                    if (colorData.m_Source != 0) emitter["colorChannel"] = (int)colorData.m_Source - 1;
                }

                // The photometric parameters live only on the managed EffectPrefab — the same
                // resolution LightCullingSystem performs each frame.
                //
                if (EntityManager.HasComponent<PrefabData>(effect.m_Effect)
                    && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(effect.m_Effect),
                                                    out PrefabBase effectPrefab)
                    && effectPrefab.TryGet(out Game.Prefabs.Effects.LightEffect light))
                {
                    emitter["type"] = light.m_Type.ToString();
                    emitter["color"] = Channel(light.m_Color.linear);
                    emitter["intensity"] = System.Math.Round((double)light.m_Intensity, 3);
                    emitter["intensityUnit"] = light.m_LightUnit.ToString();
                    if (light.m_UseColorTemperature)
                        emitter["temperature"] = System.Math.Round((double)light.m_ColorTemperature, 1);

                    if (light.m_Type == Game.Rendering.LightType.Spot)
                    {
                        emitter["spotShape"] = light.m_SpotShape.ToString();
                        emitter["spotAngle"] = System.Math.Round((double)light.m_SpotAngle, 2);
                        emitter["innerSpotPercentage"] = System.Math.Round((double)light.m_InnerSpotPercentage, 2);
                    }
                    else if (light.m_Type == Game.Rendering.LightType.Area)
                    {
                        emitter["areaShape"] = light.m_AreaShape.ToString();
                    }
                }

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

            Carto.IO.JsonFile.Write(path, table);
        }
    }
}
