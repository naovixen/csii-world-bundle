using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using Game.Simulation;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Unity.Entities;
using Unity.Mathematics;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// The authored appearance blocks that ride <c>rasters.json</c>: the ocean's own render
    /// settings, and the metres-per-repeat each ground tiles at.
    /// </summary>
    /// <remarks>
    /// Both are read off live prefabs rather than measured. They used to be hard-coded numbers,
    /// which is how the sea ended up the wrong colour with nothing to reflect, and how every
    /// consumer guessed its ground tiling.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Read the water appearance parameters — the authored numbers every water pixel's look
        /// depends on, which the level grid alone cannot carry.
        /// <c>WaterRenderSystem</c> resolves one <c>WaterRenderSettingsPrefab</c> per map
        /// (<c>currentWaterRenderSettings</c>, the same entity its <c>ApplyMaterialParams</c>
        /// reads) and pushes the fields verbatim onto every <c>WaterSurface</c>; the one bind-time
        /// transform is <c>endSmoothness = min(smoothnessDistant, smoothnessClose)</c>. Shipped as
        /// a <c>rasters.json</c> block like <c>splatMultiplier</c>/<c>splatPower</c>, so the
        /// manifest scan lifts it with the grids it colours.
        /// </summary>
        /// <param name="report">The report to note the outcome in.</param>
        /// <returns>The <c>rasters.json</c> "water" block, or <c>null</c>.</returns>
        private Dictionary<string, object> CaptureWaterSettings(StringBuilder report)
        {
            try
            {
                Game.Rendering.WaterRenderSystem render =
                    World.GetOrCreateSystemManaged<Game.Rendering.WaterRenderSystem>();
                Entity settingsEntity = render.currentWaterRenderSettings;
                if ((settingsEntity == Entity.Null)
                    || !Instance.Prefab.TryGetPrefab(settingsEntity,
                                                     out Game.Prefabs.Water.WaterRenderSettingsPrefab settings)
                    || (settings == null))
                {
                    report.AppendLine("Water appearance: no render settings prefab resolved; block omitted");
                    return null;
                }

                double[] Rgba(Color color) => new[]
                {
                    Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                    Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                };

                float seaLevel = Instance.Water.SeaLevel;
                var block = new Dictionary<string, object>
                {
                    // The old wording here claimed these were "applied verbatim ... (the one
                    // transform: endSmoothness = min(...))". Re-derived from
                    // `WaterRenderSystem.ApplyMaterialParams` 2026-08-17 (audit §56), that is
                    // wrong: FOUR more bind-time transforms exist, and they are all camera- or
                    // wind-dependent, which is why they cannot be shipped as values. Named here
                    // so a consumer knows which of these numbers reaches a shader unchanged.
                    ["semantics"] = "authored WaterRenderSettingsPrefab appearance parameters. "
                                  + "Colours, absorption, refraction, scattering, caustics, "
                                  + "ripples and the smoothness pair reach the shader unchanged; "
                                  + "endSmoothness = min(smoothnessDistant, smoothnessClose). "
                                  + "FOUR values do NOT, because WaterRenderSystem."
                                  + "ApplyMaterialParams makes them camera- or wind-dependent: "
                                  + "(1) wavesMultiplier is lerped toward 1 as the camera climbs, "
                                  + "t = saturate((cameraHeight - seaLevel - 750) / 250), so above "
                                  + "1000 m over the sea it is exactly 1; (2) the wave bands take "
                                  + "saturate(wavesMultiplier), so an authored value above 1 never "
                                  + "reaches the band amplitude at all; (3) foamAmount is scaled "
                                  + "by a camera-height fade over foamFadeRange; (4) the large "
                                  + "wind speed is lerp(0, distanceWindSpeed, wavesMultiplier^2) "
                                  + "and the large chaos comes from live wind strength. A "
                                  + "consumer replaying a static scene should take the "
                                  + "ground-level calm case: bands at saturate(wavesMultiplier), "
                                  + "foam at full foamAmount, no wind terms. "
                                  + "Ranges are [start, distance] camera metres. "
                                  + "seaLevel is WaterSystem.SeaLevel in world metres: the SEA "
                                  + "datum the sim and renderer work against (backdrop reset "
                                  + "level, the water compute shader's seaLevel uniform, the "
                                  + "plane camera depth is measured from, the editor's sea-level "
                                  + "slider). It is NOT the playable water surface and a map "
                                  + "with no sea leaves it far from one - read the water raster "
                                  + "for the surface, never seat water on this",
                    ["prefab"] = settings.name,
                    ["seaLevel"] = Math.Round((double)seaLevel, 3),
                    ["waterColor"] = Rgba(settings.m_WaterColor),
                    ["scatteringColor"] = Rgba(settings.m_WaterScatteringColor),
                    ["absorptionDistance"] = Math.Round((double)settings.m_AbsorptionDistance, 4),
                    ["maxRefractionDistance"] = Math.Round((double)settings.m_MaxRefractionDistance, 4),
                    ["smoothnessClose"] = Math.Round((double)settings.m_SmoothnessClose, 4),
                    ["smoothnessDistant"] = Math.Round((double)settings.m_SmoothnessDistant, 4),
                    ["smoothnessRange"] = new[]
                    {
                        Math.Round((double)settings.m_SmoothnessRange.x, 3),
                        Math.Round((double)settings.m_SmoothnessRange.y, 3)
                    },
                    ["ambientScattering"] = Math.Round((double)settings.m_AmbientScattering, 4),
                    ["heightScattering"] = Math.Round((double)settings.m_HeightScattering, 4),
                    ["displacementScattering"] = Math.Round((double)settings.m_DisplacementScattering, 4),
                    ["directLightTipScattering"] = Math.Round((double)settings.m_DirectLightTipScattering, 4),
                    ["directLightBodyScattering"] = Math.Round((double)settings.m_DirectLightBodyScattering, 4),
                    ["foamAmount"] = Math.Round((double)settings.m_FoamAmount, 4),
                    ["foamFadeRange"] = new[]
                    {
                        Math.Round((double)settings.m_FoamFadeRange.x, 3),
                        Math.Round((double)settings.m_FoamFadeRange.y, 3)
                    },
                    ["causticsIntensity"] = Math.Round((double)settings.m_CausticsIntensity, 4),
                    ["causticsPlaneDistance"] = Math.Round((double)settings.m_CausticsPlaneDistance, 4),
                    ["wavesMultiplier"] = Math.Round((double)settings.m_WavesMultiplier, 4),
                    ["lakeWavesMultiplier"] = Math.Round((double)settings.m_LakeWavesMultiplier, 4),
                    ["minWaterAmountForWaves"] = Math.Round((double)settings.m_MinWaterAmountForWaves, 4),
                    ["ripplesWindSpeed"] = Math.Round((double)settings.m_RipplesWindSpeed, 4),
                    ["ripplesChaos"] = Math.Round((double)settings.m_RipplesChaos, 4),
                    // The last field of the prefab, and the only one this block used to drop
                    // (audit §56). It gates whether sea wind steers the wave direction at all:
                    // when false, `ApplyMaterialParams` passes 0 for both the wind orientation
                    // and the wind strength, so the whole wind branch is inert and a consumer
                    // reproducing calm water is reproducing the game exactly rather than
                    // approximating it. Additive; schema unchanged.
                    ["seaWindAffectWavesDirection"] = settings.m_SeaWindAffectWavesDirection
                };

                report.AppendLine($"Water appearance: '{settings.name}' captured (sea level {seaLevel:F1} m)");
                return block;
            }
            catch (Exception ex)
            {
                _log.Warn($"Water appearance capture failed: {ex.Message}");
                report.AppendLine($"Water appearance: {ex.Message}; block omitted");
                return null;
            }
        }

        /// <summary>
        /// Read the theme's ground tiling — the authored metres-per-repeat behind every ground
        /// texture in <c>terrain/surfaces/</c>, which consumers otherwise guess (the §39 lesson).
        /// <c>TerrainMaterialSystem</c> resolves one <c>TerrainRenderSettingsPrefab</c> per map
        /// (<c>currentTerrainRenderSettings</c>) and binds each layer's
        /// <c>TerrainTilingData</c>/<c>TerrainLodBlendData</c> verbatim as
        /// <c>_Terrain*TextureTiling(Blend)</c>; the terrain shader's UV spans the playable area
        /// (<c>colossal_TerrainScale = 1 / playableArea</c>), so a tiling value is the repeat
        /// count across it. The extras tile with the grass layer
        /// (<c>_TerrainExtraTextureTiling</c> is bound from <c>m_TerrainGrassTiling</c>) and
        /// carry their four shading scalars instead. All FOUR extras ship: the claim that the
        /// user splatmap cannot weight extra3/extra4 was wrong, and SPLAT-6 disproved it — B and
        /// A each pack TWO 4-bit weights, so all four are paintable, and extra4 is the Sand the
        /// player paints beaches with. <c>TerrainRenderSettingsPrefab</c> carries identical
        /// <c>m_Extra3*</c>/<c>m_Extra4*</c> fields (verified by decompile).
        /// </summary>
        /// <param name="report">The report to note the outcome in.</param>
        /// <returns>The <c>rasters.json</c> "terrainTiling" block, or <c>null</c>.</returns>
        private Dictionary<string, object> CaptureTerrainTiling(StringBuilder report)
        {
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Entity settingsEntity = materials.currentTerrainRenderSettings;
                if ((settingsEntity == Entity.Null)
                    || !Instance.Prefab.TryGetPrefab(settingsEntity,
                                                     out Game.Prefabs.Terrain.TerrainRenderSettingsPrefab settings)
                    || (settings == null))
                {
                    report.AppendLine("Terrain tiling: no render settings prefab resolved; block omitted");
                    return null;
                }

                float2 area = Instance.Terrain.playableArea;

                // The game's fallback settings prefab (audit §5.4, P19). `TerrainMaterialSystem`
                // binds a layer's texture AND its tiling from `FallbackRenderSettingsPrefab`
                // whenever the theme's own diffuse is null, so reading only the current prefab
                // would report a tiling the shader is not using. That property is private, but it
                // is derived from the public `NewMapSettingsData` singleton, which is queried the
                // same way here. Null when the singleton is absent, which just means no fallback.
                //
                Game.Prefabs.Terrain.TerrainRenderSettingsPrefab fallback = null;
                if (World.EntityManager
                         .CreateEntityQuery(ComponentType.ReadOnly<Game.Prefabs.NewMapSettingsData>())
                         .TryGetSingleton(out Game.Prefabs.NewMapSettingsData mapSettings)
                    && (mapSettings.m_DefaultTerrainRenderSettings != Entity.Null))
                {
                    Instance.Prefab.TryGetPrefab(mapSettings.m_DefaultTerrainRenderSettings, out fallback);
                }

                // Per layer: which prefab the shader actually reads its tiling from. The game's
                // own test is a load attempt (`SetTexture` -> `LoadTexture`); an unset reference
                // is the case that fires in practice, so this asks whether the layer names a
                // texture at all. A named-but-unloadable texture is a broken install, and would
                // be reported here as the theme's own tiling rather than the fallback's.
                //
                Game.Prefabs.Terrain.TerrainRenderSettingsPrefab Source(
                    Colossal.IO.AssetDatabase.AssetReference diffuse)
                    => ((diffuse == null) || !diffuse.guid.isValid) && (fallback != null) ? fallback : settings;

                Dictionary<string, object> Ground(Game.Prefabs.Terrain.TerrainTilingData tiling,
                                                  Game.Prefabs.Terrain.TerrainLodBlendData blend)
                {
                    return new Dictionary<string, object>
                    {
                        ["tiling"] = new[]
                        {
                            Math.Round((double)tiling.m_NearTiling, 3),
                            Math.Round((double)tiling.m_MidTiling, 3),
                            Math.Round((double)tiling.m_FarTiling, 3)
                        },
                        ["metresPerRepeat"] = new[]
                        {
                            Math.Round(area.x / math.max(tiling.m_NearTiling, 0.001f), 4),
                            Math.Round(area.x / math.max(tiling.m_MidTiling, 0.001f), 4),
                            Math.Round(area.x / math.max(tiling.m_FarTiling, 0.001f), 4)
                        },
                        ["lodBlend"] = new[]
                        {
                            Math.Round((double)blend.m_MidBlendStart, 3),
                            Math.Round((double)blend.m_MidBlendEnd, 3),
                            Math.Round((double)blend.m_FarBlendStart, 3),
                            Math.Round((double)blend.m_FarBlendEnd, 3)
                        }
                    };
                }

                Dictionary<string, object> Extra(float dirtOverride, float splatRange,
                                                 float blurDepth, float heightOffset)
                {
                    return new Dictionary<string, object>
                    {
                        ["dirtOverride"] = Math.Round((double)dirtOverride, 4),
                        ["splatRange"] = Math.Round((double)splatRange, 4),
                        ["blurDepth"] = Math.Round((double)blurDepth, 4),
                        ["heightOffset"] = Math.Round((double)heightOffset, 4)
                    };
                }

                var block = new Dictionary<string, object>
                {
                    ["semantics"] = "authored TerrainRenderSettingsPrefab ground tiling. `tiling` "
                                  + "is [near, mid, far] texture repeats across the playable area "
                                  + "(the terrain shader's UV spans it: colossal_TerrainScale = "
                                  + "1/playableArea), bound as _Terrain*TextureTiling; "
                                  + "metresPerRepeat = playableArea.x / tiling, precomputed. "
                                  + "`lodBlend` is [midStart, midEnd, farStart, farEnd] camera "
                                  + "metres (_Terrain*TextureTilingBlend). Layer names key into "
                                  + "terrain/surfaces/; extras tile with the grass layer "
                                  + "(_TerrainExtraTextureTiling is bound from the grass tiling) "
                                  + "and their effective splatRange is max(splatRange, blurDepth) "
                                  + "at bind time",
                    ["prefab"] = settings.name,
                    ["playableArea"] = new[]
                    {
                        Math.Round((double)area.x, 3), Math.Round((double)area.y, 3)
                    },
                    ["layers"] = new SortedDictionary<string, object>
                    {
                        ["grass"] = Ground(Source(settings.m_GrassDiffuse).m_TerrainGrassTiling,
                                           Source(settings.m_GrassDiffuse).m_TerrainGrassLodBlend),
                        ["dirt"] = Ground(Source(settings.m_DirtDiffuse).m_TerrainDirtTiling,
                                          Source(settings.m_DirtDiffuse).m_TerrainDirtLodBlend),
                        ["rock"] = Ground(Source(settings.m_RockDiffuse).m_TerrainRockTiling,
                                          Source(settings.m_RockDiffuse).m_TerrainRockLodBlend)
                    },
                    ["extras"] = new SortedDictionary<string, object>
                    {
                        ["extra1"] = Extra(settings.m_Extra1DirtOverride, settings.m_Extra1SplatRange,
                                           settings.m_Extra1BlurDepth, settings.m_Extra1HeightOffset),
                        ["extra2"] = Extra(settings.m_Extra2DirtOverride, settings.m_Extra2SplatRange,
                                           settings.m_Extra2BlurDepth, settings.m_Extra2HeightOffset),
                        ["extra3"] = Extra(settings.m_Extra3DirtOverride, settings.m_Extra3SplatRange,
                                           settings.m_Extra3BlurDepth, settings.m_Extra3HeightOffset),
                        ["extra4"] = Extra(settings.m_Extra4DirtOverride, settings.m_Extra4SplatRange,
                                           settings.m_Extra4BlurDepth, settings.m_Extra4HeightOffset)
                    },
                    ["triplanarHeightMapBlending"] = Math.Round((double)settings.m_TriplanarHeightMapBlending, 4),
                    ["triplanarBlendStrengthY"] = Math.Round((double)settings.m_TriplanarBlendStrengthY, 4)
                };

                report.AppendLine($"Terrain tiling: '{settings.name}' captured "
                                + $"(grass near {area.x / math.max(settings.m_TerrainGrassTiling.m_NearTiling, 0.001f):F1} m/repeat)");
                return block;
            }
            catch (Exception ex)
            {
                _log.Warn($"Terrain tiling capture failed: {ex.Message}");
                report.AppendLine($"Terrain tiling: {ex.Message}; block omitted");
                return null;
            }
        }
    }
}
