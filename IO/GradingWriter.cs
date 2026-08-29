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
    public static partial class GradingWriter
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
        /// <remarks>
        /// <b>NOT generic, and that is the whole point.</b> <c>VolumeComponent</c> is a
        /// <c>ScriptableObject</c>, so <c>== null</c> on it is Unity's OVERLOADED operator, which
        /// answers true for an object that has been destroyed but whose reference is still
        /// non-null. C# does not consider user-defined operators for a type PARAMETER: written as
        /// <c>Applied&lt;T&gt;(T component) where T : VolumeComponent</c> this compiles to a plain
        /// reference comparison and a destroyed component passes it. Taking the base type keeps
        /// the overload, which is what the eight sites it replaced were getting.
        /// </remarks>
        /// <param name="component">The component, or null.</param>
        /// <returns>True when the game is applying it.</returns>
        private static bool Applied(VolumeComponent component) => (component != null) && component.active;

        private static void Add(Dictionary<string, object> document, string key, object block)
        {
            if (block != null) document[key] = block;
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
                JsonFile.Write(IOUtils.CombinePath(directory, "grading.json"), document);

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
