using Carto.Utils;
using Colossal.Logging;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
// Aliased because this namespace declares both a `System` enum (IO/Enums.cs) and a `FieldInfo`
// (IO/FieldInfo.cs), either of which would capture the unqualified name.
using Reflect = global::System.Reflection;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace Carto.IO
{
    /// <summary>
    /// Captures the game's colour grading — <c>grading.json</c>.
    /// <para>
    /// <b>Why this layer exists.</b> CS2 authors its ground albedo very desaturated: the shipped
    /// rock basecolor measures sRGB saturation 0.111 and sand 0.143 — very nearly grey. The vivid
    /// on-screen palette is not in the textures at all, it is HDRP post-processing applied on top.
    /// A consumer drawing the shipped albedo faithfully therefore gets a grey world and has no way,
    /// from the bundle, to know what the game did to it. Every attempt to guess the difference from
    /// screenshots produced a plausible fit to the wrong thing.
    /// </para>
    /// <para>
    /// So the bundle ships the game's own numbers instead. The values are read from
    /// <c>VolumeManager.instance.stack</c>, which is the <b>resolved, post-blend</b> state — the
    /// weather stack has already interpolated between weather prefabs by the time we read it, so
    /// what lands here is literally what the camera was grading with at rip time, not a prefab's
    /// authored default.
    /// </para>
    /// <para>
    /// <b>This is a snapshot of one moment.</b> Grading is weather- and time-of-day-driven; a rip
    /// taken at dusk in rain records dusk-in-rain. The block says so, and the <c>lighting.time</c>
    /// entry records the in-game clock and map position, so a consumer can tell what it is looking
    /// at — and so a reference screenshot taken at another hour can be recognised as describing a
    /// different grade rather than contradicting this one.
    /// </para>
    /// <para>
    /// <b>The grading alone is not the whole story, which is why <c>lighting</c> is here.</b>
    /// Replayed faithfully against three in-game surfaces, these values land within a few percent
    /// on red and green and leave a deficit on blue. The illumination turned out to be white
    /// (<c>sun.color</c> (1,1,1), <c>useColorTemperature</c> false), which <i>refuted</i> the
    /// leading explanation — but only because the block exists to be checked. A consumer that
    /// lights its scene by guesswork has no way to tell a lighting mistake from a grading one.
    /// </para>
    /// </summary>
    public static class GradingWriter
    {
        private static readonly ILog _log = LogManager.GetLogger($"{nameof(Carto)}.{nameof(GradingWriter)}");

        /// <summary>
        /// Read the resolved grading off the HDRP volume stack. <b>Main thread only</b> — it
        /// touches Unity render state.
        /// </summary>
        /// <returns>The <c>grading.json</c> document, or <c>null</c> if HDRP has no stack yet.</returns>
        public static Dictionary<string, object> Capture()
        {
            try
            {
                VolumeManager manager = VolumeManager.instance;
                VolumeStack stack = manager?.stack;
                if (stack == null) return null;

                // EVALUATE THE STACK FIRST. Reading `VolumeManager.instance.stack` straight out
                // returns whatever it last held, and outside the render loop that is every
                // component at its DEFAULT — the first rip of this layer came back with
                // saturation 0, colorFilter (1,1,1) and tonemapping "None", which is not a city
                // that tonemaps. `Update` blends the registered volumes into the stack for a
                // given trigger and layer mask, which is what the camera itself does each frame.
                //
                // `Camera.main` only answers for a camera tagged MainCamera, which the game is
                // under no obligation to do — so fall through to the current render camera, then
                // to any enabled camera at all. A null trigger would silently mean "no evaluation"
                // and ship defaults again, which is the failure this whole block exists to stop.
                //
                Camera camera = Camera.main ?? Camera.current;
                if (camera == null)
                {
                    Camera[] cameras = Camera.allCameras;
                    if ((cameras != null) && (cameras.Length > 0)) camera = cameras[0];
                }

                Transform trigger = (camera != null) ? camera.transform : null;
                LayerMask mask = ~0;
                string cameraName = (camera != null) ? camera.name : "(none)";

                // HDRP cameras carry their own volume mask and can anchor blending somewhere
                // other than the camera; honour both when they are there.
                if (camera != null)
                {
                    HDAdditionalCameraData data = camera.GetComponent<HDAdditionalCameraData>();
                    if (data != null)
                    {
                        mask = data.volumeLayerMask;
                        if (data.volumeAnchorOverride != null) trigger = data.volumeAnchorOverride;
                    }
                }

                // Last resort: a throwaway transform. The weather volumes are global, so the
                // trigger's POSITION only matters for volumes with a collider — evaluating
                // against a scratch object still blends every global volume correctly, and is
                // strictly better than shipping an unevaluated stack.
                bool evaluated = false;
                GameObject scratch = null;
                try
                {
                    if (trigger == null)
                    {
                        scratch = new GameObject("carto_volume_trigger");
                        trigger = scratch.transform;
                        cameraName = "(none - scratch trigger)";
                    }

                    manager.Update(trigger, mask);
                    evaluated = true;
                }
                finally
                {
                    if (scratch != null) UnityEngine.Object.DestroyImmediate(scratch);
                }

                int volumeCount = 0;
                try { volumeCount = manager.GetVolumes(mask)?.Length ?? 0; }
                catch (Exception) { /* diagnostics only */ }

                Dictionary<string, object> document = new Dictionary<string, object>
                {
                    ["semantics"] = "the game's resolved HDRP colour grading at rip time, read from "
                                  + "VolumeManager's post-blend volume stack. CS2's ground albedo is "
                                  + "authored near-grey (rock sRGB saturation 0.111, sand 0.143) and "
                                  + "this is what turns it into the on-screen palette. Optional: a "
                                  + "consumer that wants the raw albedo should ignore this block "
                                  + "entirely; one that wants the game's look should apply it as a "
                                  + "display transform, NOT bake it into the shipped textures",
                    ["order"] = "exposure -> white balance -> color adjustments (filter, contrast, "
                              + "hue, saturation) -> shadows/midtones/highlights -> tonemapping",
                    ["weatherDependent"] = true,
                    ["note"] = "a snapshot of one moment: grading follows weather and time of day, "
                             + "so a rip taken at dusk records dusk",

                    // Enough to tell "the game really does grade this way" from "the capture read
                    // an unevaluated stack" WITHOUT another rip. An all-default block with
                    // `stackEvaluated: false` or `volumesFound: 0` is a failed read, not a
                    // neutral grade — the first cut of this layer shipped exactly that.
                    //
                    ["diagnostics"] = new Dictionary<string, object>
                    {
                        ["stackEvaluated"] = evaluated,
                        ["volumesFound"] = volumeCount,
                        ["camera"] = cameraName,
                        ["volumeLayerMask"] = mask.value
                    }
                };

                Add(document, "exposure", CaptureExposure(stack));
                Add(document, "whiteBalance", CaptureWhiteBalance(stack));
                Add(document, "colorAdjustments", CaptureColorAdjustments(stack));
                Add(document, "shadowsMidtonesHighlights", CaptureSmh(stack));
                Add(document, "splitToning", CaptureSplitToning(stack));
                Add(document, "tonemapping", CaptureTonemapping(stack));
                Add(document, "vignette", CaptureVignette(stack));
                Add(document, "fog", CaptureFog(stack));
                Add(document, "lighting", CaptureLighting(stack));

                // What is in the stack that nothing above reads (audit §5.2, P17). The reason
                // this exists rather than a longer list of capture methods: the previous sweep
                // had to enumerate `Game.Prefabs.Climate.*Properties` in a decompiler to learn
                // that `Fog` was being missed, and that answer goes stale the next time the game
                // patches. A rip that names its own blind spot cannot hide one for three months.
                //
                ((Dictionary<string, object>)document["diagnostics"])["unread"] = UnreadComponents(stack);

                return document;
            }
            catch (Exception ex)
            {
                _log.Warn($"Colour grading capture failed: {ex.Message}");
                return null;
            }
        }

        /// <summary>
        /// Whether a volume component contributes to the stack at all.
        /// </summary>
        /// <remarks>
        /// Null means the volume profile does not carry it; <c>active</c> false means it carries
        /// it switched off. Either way the game is not applying it, and a capture that shipped it
        /// would describe a grading the player never sees.
        ///
        /// Written out at eight capture sites before refactor Phase 8, which is eight chances to
        /// check one and forget the other. Two of those sites add a further condition of their
        /// own — <c>SplitToning.IsActive()</c>, <c>Fog.enabled.value</c> — and keep it beside this
        /// call, because it is theirs and not the general rule.
        /// </remarks>
        /// <typeparam name="T">The volume component type.</typeparam>
        /// <param name="component">The component, or null.</param>
        /// <returns>True when the game is applying it.</returns>
        private static bool Applied<T>(T component) where T : VolumeComponent =>
            (component != null) && component.active;

        private static void Add(Dictionary<string, object> document, string key, object block)
        {
            if (block != null) document[key] = block;
        }

        private static object CaptureExposure(VolumeStack stack)
        {
            Exposure exposure = stack.GetComponent<Exposure>();
            if (!Applied(exposure)) return null;
            return new Dictionary<string, object>
            {
                ["mode"] = exposure.mode.value.ToString(),
                ["fixedExposure"] = Round(exposure.fixedExposure.value),
                ["compensation"] = Round(exposure.compensation.value),

                // The metered value itself is NOT reachable: HDRP keeps it in a 1x1 render
                // texture behind internal API. These are the clamps the game drives from
                // DayNightCycleData (LightingSystem sets them per time of day), so they bound
                // the one term a consumer has to choose for itself instead of leaving it open.
                ["limitMin"] = Round(exposure.limitMin.value),
                ["limitMax"] = Round(exposure.limitMax.value),
                ["units"] = "EV100; `compensation` is stops and maps straight onto Blender's "
                          + "Color Management exposure. In Automatic* modes the game meters every "
                          + "frame and the metered value is not exposed by HDRP, so absolute "
                          + "brightness cannot be replayed - `limitMin`/`limitMax` bracket it"
            };
        }

        private static object CaptureVignette(VolumeStack stack)
        {
            Vignette vignette = stack.GetComponent<Vignette>();
            if (!Applied(vignette)) return null;
            if (vignette.intensity.value <= 0f) return null;

            UnityEngine.Color color = vignette.color.value;
            return new Dictionary<string, object>
            {
                ["intensity"] = Round(vignette.intensity.value),
                ["smoothness"] = Round(vignette.smoothness.value),
                ["roundness"] = Round(vignette.roundness.value),
                ["color"] = new[] { Round(color.r), Round(color.g), Round(color.b) },
                ["units"] = "screen-space darkening toward the frame edge. Not part of the colour "
                          + "chain - it is a function of WHERE a pixel is, not what colour it is - "
                          + "but it is why a screenshot's corners read darker than its centre, "
                          + "which matters if a reference colour is sampled off-centre"
            };
        }

        /// <summary>
        /// The split-toning tint: one colour into the shadows, another into the highlights.
        /// <para>
        /// <b>How this was found, because the method matters more than the block.</b> It was not
        /// found by reading the game: a sweep of the decompiled assemblies for `SplitToning`
        /// returns *nothing*, because the override lives in an authored volume profile, not in
        /// code that names the type. It was found by `diagnostics.unread` on its very first rip
        /// (audit §5.2) — the blind-spot report added to catch the *next* `Fog`, which caught one
        /// immediately.
        /// </para>
        /// <para>
        /// <b>Why it matters.</b> HDRP applies it inside the grading chain, and it is the only
        /// operator in that chain that can shift hue *differently by luminance* — which is the
        /// exact shape of the residual left after white illumination, tonemapper choice, working
        /// space and now fog were all eliminated: +6.9 deg on sand and +6.2 deg on pavement, both
        /// bright, against +1.2 deg on grass, which is dark. `IsActive` is `shadows != grey ||
        /// highlights != grey`, so a neutral pair means the operator does nothing and this block
        /// is omitted.
        /// </para>
        /// </summary>
        /// <param name="stack">The resolved volume stack.</param>
        /// <returns>The <c>splitToning</c> block, or <c>null</c> when it is neutral.</returns>
        private static object CaptureSplitToning(VolumeStack stack)
        {
            SplitToning splitToning = stack.GetComponent<SplitToning>();
            if (!Applied(splitToning) || !splitToning.IsActive()) return null;

            UnityEngine.Color shadows = splitToning.shadows.value;
            UnityEngine.Color highlights = splitToning.highlights.value;
            return new Dictionary<string, object>
            {
                ["shadows"] = new[] { Round(shadows.r), Round(shadows.g), Round(shadows.b) },
                ["highlights"] = new[] { Round(highlights.r), Round(highlights.g), Round(highlights.b) },
                ["balance"] = Round(splitToning.balance.value),
                ["units"] = "each colour is LDR rgb around neutral grey (0.5, 0.5, 0.5); grey means "
                          + "no tint on that end. `balance` is -100..100 and moves the split point "
                          + "between the two. Applied INSIDE the colour chain, after color "
                          + "adjustments and before tonemapping - it is the one operator here that "
                          + "shifts hue by luminance, so it lands differently on a bright surface "
                          + "than a dark one"
            };
        }

        /// <summary>
        /// The atmospheric fog the scene is seen through.
        /// <para>
        /// <b>Why it is here (audit §5.2, P17).</b> `Game.Prefabs.Climate.FogProperties` binds
        /// HDRP's <c>Fog</c> from the weather prefab, carrying an <b>HDR tint</b> alongside the
        /// attenuation distance, and <c>ClimateRenderSystem.UpdateFogOverride</c> forces
        /// <c>enabled</c> on whenever a fog override is active, lerping the mean free path from
        /// 10 km down to 1 m. So this is a tinted, <i>distance-dependent</i> term sitting between
        /// every surface the bundle ships and the camera — and the bundle carried no trace of it
        /// while SPLAT-4's unexplained residual was a hue error on two warm surfaces measured at
        /// different camera distances, after white illumination and tonemapper choice had both
        /// been eliminated. Whether it is the cause is now a number to read rather than an
        /// argument to have.
        /// </para>
        /// <para>
        /// Unlike the grading blocks this is <b>not</b> a display transform a consumer replays on
        /// a texture: it is a per-pixel function of distance from the camera, so it belongs in the
        /// consumer's own atmosphere, and a consumer reconstructing albedo should ignore it.
        /// </para>
        /// </summary>
        /// <param name="stack">The resolved volume stack.</param>
        /// <returns>The <c>fog</c> block, or <c>null</c> when fog is off.</returns>
        private static object CaptureFog(VolumeStack stack)
        {
            Fog fog = stack.GetComponent<Fog>();
            if (!Applied(fog) || !fog.enabled.value) return null;

            UnityEngine.Color tint = fog.tint.value;
            return new Dictionary<string, object>
            {
                ["meanFreePath"] = Round(fog.meanFreePath.value),
                ["baseHeight"] = Round(fog.baseHeight.value),
                ["maximumHeight"] = Round(fog.maximumHeight.value),
                ["maxFogDistance"] = Round(fog.maxFogDistance.value),
                ["tint"] = new[] { Round(tint.r), Round(tint.g), Round(tint.b) },
                ["units"] = "`meanFreePath` is the distance in metres over which the fog "
                          + "attenuates to 1/e, `baseHeight`/`maximumHeight` bound it vertically "
                          + "in world metres, `maxFogDistance` clamps it. `tint` is an HDR colour "
                          + "and may exceed 1. NOT part of the colour chain above: fog is a "
                          + "function of distance from the camera, so it belongs in a consumer's "
                          + "atmosphere, not in its textures"
            };
        }

        /// <summary>
        /// Every volume component resolved in the stack that this writer does not read, <b>with
        /// the values some volume overrode on it</b>.
        /// <para>
        /// The values are the point. The first version of this reported type names only, and its
        /// first rip named `SplitToning` — a colour-grading operator, and a live suspect for a
        /// months-old residual — which then cost a whole second rip to read three numbers off.
        /// Reporting the overridden parameters means a blind spot arrives already answered.
        /// Parameter names come from the component's own public fields, which is where HDRP puts
        /// them and how its own `FindParameters` walks them.
        /// </para>
        /// </summary>
        /// <param name="stack">The resolved volume stack.</param>
        /// <returns>Sorted <c>"Component.field = value"</c> entries for what is overridden and unread.</returns>
        private static List<string> UnreadComponents(VolumeStack stack)
        {
            HashSet<string> read = new HashSet<string>
            {
                nameof(Exposure), nameof(WhiteBalance), nameof(ColorAdjustments),
                nameof(ShadowsMidtonesHighlights), nameof(SplitToning), nameof(Tonemapping),
                nameof(Vignette), nameof(Fog), nameof(PhysicallyBasedSky),
                nameof(IndirectLightingController)
            };

            List<string> unread = new List<string>();
            try
            {
                // `AnyPropertiesIsOverridden` is the right filter, not `active`: every registered
                // component type is present in a resolved stack, almost all of them at their
                // defaults. `VolumeManager.ReplaceData` clears every parameter's `overrideState`
                // at the top of each `Update`, and `VolumeComponent.Override` sets it as each
                // volume is blended in - so an overridden parameter means some volume in THIS
                // scene set it, which is exactly the set worth reporting.
                //
                foreach (Type type in VolumeManager.instance.baseComponentTypeArray)
                {
                    if ((type == null) || read.Contains(type.Name)) continue;

                    VolumeComponent component = stack.GetComponent(type);
                    if ((component == null) || !component.active) continue;
                    if (!component.AnyPropertiesIsOverridden()) continue;

                    bool named = false;
                    foreach (Reflect.FieldInfo field
                             in type.GetFields(Reflect.BindingFlags.Public
                                               | Reflect.BindingFlags.Instance))
                    {
                        if (!typeof(VolumeParameter).IsAssignableFrom(field.FieldType)) continue;
                        if (!(field.GetValue(component) is VolumeParameter parameter)) continue;
                        if (!parameter.overrideState) continue;

                        // VolumeParameter<T> overrides ToString to print its value, so this reads
                        // every parameter type without a switch over HDRP's parameter zoo.
                        unread.Add($"{type.Name}.{field.Name} = {parameter}");
                        named = true;
                    }

                    // Overridden, but through a field this walk cannot see - still worth naming.
                    if (!named) unread.Add(type.Name);
                }
            }
            catch (Exception)
            {
                // Diagnostics only - never fail a rip over a blind-spot report.
                return unread;
            }

            unread.Sort(StringComparer.Ordinal);
            return unread;
        }

        /// <summary>
        /// The light the grading was applied on top of: sun, sky and indirect.
        /// <para>
        /// <b>Why this belongs here.</b> Measured against three in-game surfaces, the grading in
        /// this file reproduces the game's on-screen colour to within a few percent on red and
        /// green, and leaves a consistent deficit on blue. The clincher was a near-neutral
        /// pavement (albedo linear 0.1019, 0.1018, 0.1010) that still renders warm beige in game:
        /// no property of an albedo can do that, so the chroma is coming from the illumination.
        /// A consumer that lights its scene with a white sun therefore cannot match the game no
        /// matter how faithfully it replays the grading, and nothing in the bundle told it so.
        /// </para>
        /// </summary>
        private static object CaptureLighting(VolumeStack stack)
        {
            Dictionary<string, object> block = new Dictionary<string, object>
            {
                ["semantics"] = "the illumination the grading above sits on top of. A consumer "
                              + "that reproduces the albedo and the grading but lights the scene "
                              + "white will still miss the game's colour: the sun is not white"
            };
            bool any = false;

            try
            {
                Game.Simulation.PlanetarySystem planetary = Unity.Entities.World
                    .DefaultGameObjectInjectionWorld
                    ?.GetExistingSystemManaged<Game.Simulation.PlanetarySystem>();
                if (planetary != null)
                {
                    Add(block, "sun", CaptureLight(planetary.SunLight, "sun"));
                    Add(block, "moon", CaptureLight(planetary.MoonLight, "moon"));
                    any |= block.ContainsKey("sun");

                    block["time"] = new Dictionary<string, object>
                    {
                        ["hour"] = planetary.hour,
                        ["minute"] = planetary.minute,
                        ["day"] = planetary.day,
                        ["year"] = planetary.year,
                        ["latitude"] = Round(planetary.latitude),
                        ["longitude"] = Round(planetary.longitude),
                        ["units"] = "in-game clock and map position at rip time. The sun's colour "
                                  + "and angle follow these, so a reference screenshot taken at "
                                  + "another hour is not describing this grading"
                    };
                    any = true;
                }
            }
            catch (Exception ex)
            {
                block["sunError"] = ex.Message;
            }

            PhysicallyBasedSky sky = stack.GetComponent<PhysicallyBasedSky>();
            if (Applied(sky))
            {
                UnityEngine.Color zenith = sky.zenithTint.value;
                UnityEngine.Color horizon = sky.horizonTint.value;
                UnityEngine.Color ground = sky.groundTint.value;
                block["sky"] = new Dictionary<string, object>
                {
                    ["exposure"] = Round(sky.exposure.value),
                    ["zenithTint"] = new[] { Round(zenith.r), Round(zenith.g), Round(zenith.b) },
                    ["horizonTint"] = new[] { Round(horizon.r), Round(horizon.g), Round(horizon.b) },
                    ["groundTint"] = new[] { Round(ground.r), Round(ground.g), Round(ground.b) },
                    ["units"] = "HDRP PhysicallyBasedSky. `exposure` is EV on the sky's own "
                              + "contribution; the tints multiply it. This is the ambient half of "
                              + "the illumination and is what fills shadow"
                };
                any = true;
            }

            IndirectLightingController indirect = stack.GetComponent<IndirectLightingController>();
            if (Applied(indirect))
            {
                block["indirect"] = new Dictionary<string, object>
                {
                    ["diffuseMultiplier"] = Round(indirect.indirectDiffuseLightingMultiplier.value),
                    ["reflectionMultiplier"] = Round(indirect.reflectionLightingMultiplier.value),
                    ["units"] = "multipliers on bounced light; the game raises these heavily at "
                              + "night (DayNightCycleData.NightIndirectDiffuseMultiplier)"
                };
                any = true;
            }

            return any ? block : null;
        }

        private static object CaptureLight(Game.Simulation.PlanetarySystem.LightData data,
                                           string which)
        {
            if (!data.isValid) return null;
            UnityEngine.Light light = data.light;
            if (light == null) return null;

            Dictionary<string, object> block = new Dictionary<string, object>
            {
                ["color"] = new[] { Round(light.color.r), Round(light.color.g),
                                    Round(light.color.b) },
                ["useColorTemperature"] = light.useColorTemperature,
                ["colorTemperature"] = Round(light.colorTemperature),
                ["units"] = "`color` is the light's own filter as authored. When "
                          + "`useColorTemperature` is set, HDRP multiplies it by the blackbody "
                          + "colour of `colorTemperature` Kelvin, so BOTH terms are needed to "
                          + "reproduce the light: the effective colour is the product"
            };

            if (data.additionalData != null)
            {
                block["intensity"] = Round(data.additionalData.intensity);
                block["initialIntensity"] = Round(data.initialIntensity);
                block["intensityUnits"] = "lux for a directional light. The game scales the sun's "
                                        + "intensity down as it approaches the horizon "
                                        + "(PlanetarySystem multiplies initialIntensity by a "
                                        + "smoothstep on the sun's height)";
            }

            if (data.transform != null)
            {
                UnityEngine.Vector3 forward = data.transform.forward;
                // The direction the light TRAVELS. Elevation is measured from the horizon, so a
                // sun straight overhead is +90 and one at the horizon is 0 - the number a
                // consumer needs to place its own sun, without having to guess a handedness.
                float elevation = -UnityEngine.Mathf.Asin(
                    UnityEngine.Mathf.Clamp(forward.y, -1f, 1f)) * UnityEngine.Mathf.Rad2Deg;
                float azimuth = UnityEngine.Mathf.Atan2(-forward.x, -forward.z)
                              * UnityEngine.Mathf.Rad2Deg;
                block["direction"] = new[] { Round(forward.x), Round(forward.y), Round(forward.z) };
                block["elevationDegrees"] = Round(elevation);
                block["azimuthDegrees"] = Round((azimuth + 360f) % 360f);
                block["directionUnits"] = "`direction` is the unit vector the light travels along "
                                        + "in the game's left-handed Y-up frame; `elevation` is "
                                        + "degrees above the horizon and `azimuth` is degrees "
                                        + "clockwise from -Z toward the light";
            }

            block["shadows"] = light.shadows.ToString();
            block["enabled"] = light.enabled && light.gameObject.activeInHierarchy;
            block["which"] = which;
            return block;
        }

        private static object CaptureWhiteBalance(VolumeStack stack)
        {
            WhiteBalance balance = stack.GetComponent<WhiteBalance>();
            if (!Applied(balance)) return null;
            return new Dictionary<string, object>
            {
                ["temperature"] = Round(balance.temperature.value),
                ["tint"] = Round(balance.tint.value),
                ["units"] = "both -100..100, HDRP's own scale (temperature: negative cooler, "
                          + "positive warmer)"
            };
        }

        private static object CaptureColorAdjustments(VolumeStack stack)
        {
            ColorAdjustments adjustments = stack.GetComponent<ColorAdjustments>();
            if (!Applied(adjustments)) return null;

            UnityEngine.Color filter = adjustments.colorFilter.value;
            return new Dictionary<string, object>
            {
                ["postExposure"] = Round(adjustments.postExposure.value),
                ["contrast"] = Round(adjustments.contrast.value),
                ["colorFilter"] = new[] { Round(filter.r), Round(filter.g), Round(filter.b) },
                ["hueShift"] = Round(adjustments.hueShift.value),
                ["saturation"] = Round(adjustments.saturation.value),
                ["units"] = "postExposure in EV/stops; contrast, hueShift and saturation on "
                          + "HDRP's -100..100 scale (saturation -100 = greyscale, 0 = unchanged, "
                          + "+100 = double); colorFilter is a linear HDR multiplier; hueShift is "
                          + "degrees /180"
            };
        }

        private static object CaptureSmh(VolumeStack stack)
        {
            ShadowsMidtonesHighlights smh = stack.GetComponent<ShadowsMidtonesHighlights>();
            if (!Applied(smh)) return null;
            return new Dictionary<string, object>
            {
                ["shadows"] = Vector(smh.shadows.value),
                ["midtones"] = Vector(smh.midtones.value),
                ["highlights"] = Vector(smh.highlights.value),
                ["shadowsStart"] = Round(smh.shadowsStart.value),
                ["shadowsEnd"] = Round(smh.shadowsEnd.value),
                ["highlightsStart"] = Round(smh.highlightsStart.value),
                ["highlightsEnd"] = Round(smh.highlightsEnd.value),
                ["units"] = "each is [r, g, b, w]: rgb a per-channel multiplier around 1.0, w an "
                          + "overall offset. Equivalent to a lift/gamma/gain colour balance"
            };
        }

        private static object CaptureTonemapping(VolumeStack stack)
        {
            Tonemapping tonemapping = stack.GetComponent<Tonemapping>();
            if (!Applied(tonemapping)) return null;

            Dictionary<string, object> block = new Dictionary<string, object>
            {
                ["mode"] = tonemapping.mode.value.ToString(),
                ["note"] = "the display transform the grading above was authored against. "
                         + "Blender 5.2 ships ACES 1.3 and ACES 2.0 as view transforms, so `ACES` "
                         + "maps onto one directly; `Neutral` is closest to Khronos PBR Neutral "
                         + "and `None` to Standard"
            };

            // THE LUT PATH IS NOT HYPOTHETICAL. Game.Rendering.LightingSystem builds a
            // priority-1000 volume whose Tonemapping is `External` with a 3D LUT blended per
            // time of day (DayLUT / SunriseAndSunsetLUT / NightLUT, mixed by a compute shader),
            // gated on DayNightCycleData.UseLUT. When that gate is off the mode override does
            // not apply and the weather stack's ACES wins - which is what every rip has read so
            // far. But a bundle that recorded only "ACES" could not tell a consumer whether it
            // got lucky, and a consumer replaying ACES against a LUT-graded city would be wrong
            // with no way to know. So the LUT fields ship whether or not they are in effect.
            //
            Texture lut = tonemapping.lutTexture.value;
            block["lutContribution"] = Round(tonemapping.lutContribution.value);
            block["hasLut"] = lut != null;
            if (lut != null)
            {
                block["lut"] = new Dictionary<string, object>
                {
                    ["name"] = lut.name,
                    ["width"] = lut.width,
                    ["height"] = lut.height,
                    ["type"] = lut.GetType().Name,
                    ["note"] = "NOT exported. The blended result lives in a 3D render texture on "
                             + "the GPU, and reading it back needs a compute dispatch this layer "
                             + "does not do. If `mode` is External and this is present, the "
                             + "grading above is INCOMPLETE - the LUT is doing part of the work"
                };
            }
            return block;
        }

        private static double[] Vector(UnityEngine.Vector4 value)
        {
            return new[] { Round(value.x), Round(value.y), Round(value.z), Round(value.w) };
        }

        private static double Round(float value)
        {
            return Math.Round((double)value, 4);
        }

        /// <summary>
        /// Write <c>grading.json</c> at the bundle root. Safe off the main thread.
        /// </summary>
        /// <param name="directory">The bundle root.</param>
        /// <param name="document">The captured document, or <c>null</c> to write nothing.</param>
        /// <returns>A one-line summary for the rip report.</returns>
        public static string Write(string directory, Dictionary<string, object> document)
        {
            if (document == null) return "Grading: no HDRP volume stack resident; layer omitted";

            try
            {
                File.WriteAllText(IOUtils.CombinePath(directory, "grading.json"),
                                  JsonConvert.SerializeObject(document, Formatting.Indented));

                List<string> blocks = new List<string>();
                foreach (string key in new[] { "exposure", "whiteBalance", "colorAdjustments",
                                               "shadowsMidtonesHighlights", "tonemapping" })
                {
                    if (document.ContainsKey(key)) blocks.Add(key);
                }
                return $"Grading: captured {string.Join(", ", blocks)}";
            }
            catch (Exception ex)
            {
                _log.Warn($"Colour grading write failed: {ex.Message}");
                return $"Grading: {ex.Message}";
            }
        }
    }
}
