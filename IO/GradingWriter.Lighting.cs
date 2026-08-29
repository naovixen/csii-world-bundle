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
    /// The sun, the moon and the sky — the lighting block a consumer needs to place its own
    /// light rig where the game had one.
    /// </summary>
    public static partial class GradingWriter
    {
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
    }
}
