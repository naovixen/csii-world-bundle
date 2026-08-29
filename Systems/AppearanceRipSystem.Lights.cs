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
    /// The §7.3 light emitters: the prefab ones, and the ones placed in the city.
    /// </summary>
    public partial class AppearanceRipSystem
    {
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
    }
}
