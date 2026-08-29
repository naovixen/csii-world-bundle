using Carto.Utils;
using Colossal.Logging;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using Reflect = global::System.Reflection;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.HighDefinition;

namespace Carto.IO
{
    /// <summary>
    /// One capture per HDRP volume component the game resolves, in the game's own order.
    /// </summary>
    /// <remarks>
    /// Each is "null, or carried but switched off, means the game is not applying it" — the
    /// guard <c>Applied</c> states once. What each block CONTAINS is the game's resolved
    /// volume-stack value, not a default: the point of the layer is that a consumer replays real
    /// numbers rather than approximating a screenshot.
    /// </remarks>
    public static partial class GradingWriter
    {
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
    }
}
