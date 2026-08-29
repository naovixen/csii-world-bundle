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
    /// The two splat weight maps: the game's own per-pixel ground blend, and the player's paint.
    /// </summary>
    /// <remarks>
    /// Read, never calibrated. A calibration used to run here and got three of four channels
    /// wrong; <c>csiibundle.splat</c> carries the two measurements that fix R=dirt / G=grass and
    /// the nibble packing of B and A.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Read back the game's splat weight map — the per-pixel ground classification the
        /// terrain shader blends grass/dirt/rock (and the painted surfaces) with.
        /// <c>TerrainMaterialSystem.UpdateSplatmap</c> renders it on the GPU from the modified
        /// height cascade, water, noise and the user brush (<c>_SplatHeightVariance</c>,
        /// <c>_SplatRockLimit</c>, <c>_SplatGrassLimit</c>), into a 4096² RGBA8 render texture
        /// bound globally as <c>colossal_Splatmap</c> and exposed as the public
        /// <c>splatmap</c> property. It spans exactly the playable area:
        /// <c>world.xz = playableOffset + uv × playableArea</c> (its own <c>UpdateSplatmap</c>
        /// viewport math). Shipping it verbatim replaces the consumer-side per-cell ground
        /// classification with the game's own smooth per-pixel rule.
        /// <b>The channel-to-ground assignment is published, not left to the consumer.</b> The map
        /// is <c>R = dirt, G = grass, B = extra1, A = extra2</c>, with <b>rock as the implicit
        /// base</b> — the ground that renders where R falls away, which is what a cliff face is.
        /// The grounds composite in channel order over that base, each at
        /// <c>saturate(w · splatMultiplier)^splatPower</c>; they are <b>not</b> a partition to be
        /// normalised.
        /// <para>
        /// <b>R and G were the wrong way round until 2026-08-10 (audit SPLAT-3), and the fix is
        /// the reason this doc-comment is long.</b> The earlier reading called R grass because R
        /// "falls monotonically with slope"; so does every soil weight, because rock is R's own
        /// residual, so that observation never distinguished them. G was then assigned "by
        /// elimination" — the one inference in this block that was never measured, and the one
        /// that was wrong. Two measurements settle it, and neither is a curve fit:
        /// </para>
        /// <list type="bullet">
        /// <item><c>G = grass</c> — G is <b>exactly 0 on every texel under 1 m or more of
        /// water</b> (mean 0.0 across 58% of the map) and averages 201.7 on flat land, falling to
        /// 0.5 on 45°+ cliffs. A weight that is identically zero on every lakebed and every cliff
        /// is a vegetation mask; nothing else in the ground set behaves that way.</item>
        /// <item><c>R = dirt</c> — R averages <b>249.9 underwater</b>, rising with depth, and 251.6
        /// on flat land. A weight that blankets the seabed is sediment, not grass. Its slope
        /// falloff (251.6 flat → 9.4 at 45°+) is what uncovers the rock base.</item>
        /// <item><c>B</c>, <c>A</c> — measured exactly: non-zero on precisely the pixels the
        /// player painted with Extra1/Extra2, a biconditional with zero counterexamples over all
        /// 16,777,216 pixels. Note that <c>ApplyToSplatmap</c> writes the <i>user</i> splatmap's
        /// R/G, not these channels, so it corroborates the pairing but does not name the index.</item>
        /// </list>
        /// <para>
        /// The old map inverted the whole city: it painted the land dirt-brown and the lakebed
        /// grass-green, which is exactly what the reference consumer rendered. The channel
        /// semantics live in HLSL that ships compiled, so this remains an empirical derivation —
        /// but one resting on physical facts about where grass can grow rather than on a curve fit.
        /// </para>
        /// </summary>
        /// <param name="grounds">The ground textures the theme actually bound, from <see cref="CaptureSurfaceTextures"/>.</param>
        /// <param name="block">Receives the <c>rasters.json</c> "splatmap" block, or <c>null</c>.</param>
        /// <param name="report">The report to note results in.</param>
        /// <returns>The captured pixels for the writer task, or <c>null</c>.</returns>
        private PendingTexture CaptureSplatmap(Dictionary<string, string> grounds,
                                               out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture splat = materials?.splatmap;
                if (splat == null)
                {
                    report.AppendLine("Splatmap: not resident; consumers fall back to per-cell classification");
                    return null;
                }

                PendingTexture captured = MaterialRipSystem.Capture(splat, "splatmap", "terrain splatmap", 0);
                if (captured?.Pixels == null)
                {
                    report.AppendLine("Splatmap: readback failed; consumers fall back to per-cell classification");
                    return null;
                }

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;

                // Each channel is named only if the theme actually bound the ground it weights.
                // A theme that binds no grass ground cannot have a grass channel, and shipping the
                // name anyway would send a consumer to a texture that is not there.
                //
                bool dirtBound = (grounds != null) && grounds.ContainsKey("dirt");
                bool grassBound = (grounds != null) && grounds.ContainsKey("grass");
                bool rockBound = (grounds != null) && grounds.ContainsKey("rock");

                // Texel-centre geo-referencing, same convention as the terrain grid: top-left
                // texel centre, rows toward −Z.
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "GPU-generated ground-blend weights (height variance, rock/grass "
                                  + "limits, noise, user brush). Channels are named in `channels` "
                                  + "below — do not calibrate them from slope, which cannot tell "
                                  + "dirt from grass: rock is the dirt channel's own residual, so "
                                  + "both soil weights fall with slope together (schema §8)",

                    // The names are `terrain/surfaces/index.json` keys, so `channels.R` joins
                    // straight to a ground texture with no lookup table on the consumer's side.
                    //
                    ["channels"] = new Dictionary<string, object>
                    {
                        ["R"] = dirtBound ? "dirt" : "unknown",
                        ["G"] = grassBound ? "grass" : "unknown",

                        // B and A are NOT single weights. Each packs two 4-bit weights, which is
                        // how four paintable extras fit in the R8G8 user splatmap. Reading either
                        // as one 0-255 opacity silently merges two different grounds into one and
                        // drops the other two entirely (audit SPLAT-6).
                        ["B"] = "extra1:extra2 (packed, low:high nibble)",
                        ["A"] = "extra3:extra4 (packed, low:high nibble)",
                        ["packed"] = new Dictionary<string, object>
                        {
                            ["B.low"] = "extra1",
                            ["B.high"] = "extra2",
                            ["A.low"] = "extra3",
                            ["A.high"] = "extra4",
                            ["decode"] = "extraN weight = (channel & 15) or (channel >> 4), "
                                       + "each 0..15; normalise by 15, NOT by 255"
                        },

                        // Rock is drawn where the dirt weight falls away — a cliff face — so it is
                        // not a channel at all. It is the residual of R ALONE: R and G routinely
                        // sum well past 255 (both are near-saturated on flat land), so a consumer
                        // that treats the four weights as a partition and normalises them turns
                        // every meadow half brown.
                        //
                        ["base"] = rockBound ? "rock" : "unknown",
                        ["baseWeight"] = "max(0, 1 - min(1, R / 255 * splatMultiplier))"
                    },

                    // The composite the terrain shader's own multiplier/power imply, stated
                    // explicitly because the natural misreading — normalise the four weights — is
                    // the one that inverts the map.
                    //
                    ["blend"] = "composite over `base` in order: R (dirt), G (grass), then the "
                              + "four packed extras extra1..extra4 unpacked from B and A. The two "
                              + "soil channels use opacity saturate(w / 255 * splatMultiplier)"
                              + "^splatPower; each extra uses saturate(nibble / 15 * "
                              + "splatMultiplier)^splatPower. Not a partition: do not normalise",

                    // Per-channel, because these four are not equally well established and a
                    // consumer deserves to know which one to doubt.
                    //
                    ["channelProvenance"] = new Dictionary<string, object>
                    {
                        ["R"] = dirtBound
                              ? "measured — averages 249.9 under water (rising with depth) and "
                              + "251.6 on flat land; a weight that blankets the seabed is sediment. "
                              + "Falls 251.6 → 9.4 from flat to 45°+ cliff, uncovering the rock base"
                              : "unknown — this theme binds no dirt ground, so R is not named",
                        ["G"] = grassBound
                              ? "measured — exactly 0 on every texel under 1 m or more of water and "
                              + "0.5 on 45°+ cliffs, against 201.7 on flat land; a weight that is "
                              + "identically zero on every lakebed and every cliff is a vegetation mask"
                              : "unknown — this theme binds no grass ground, so G is not named",
                        ["B"] = "packed pair — TerrainMaterialSystem.PackToBAChannels writes the "
                              + "four brush components at bit offsets 0, 4, 8, 12 across B and A, "
                              + "so B carries extra1 (low nibble) and extra2 (high). Each nibble "
                              + "is a separate coherent painted region in a full-city rip",
                        ["A"] = "packed pair — extra3 (low nibble) and extra4 (high). The game's "
                              + "locale names the four paint tools Dry / Lush / Dirt / Sand for "
                              + "extra1..extra4, and extra4 traces the shorelines in a real city, "
                              + "which is where a player paints beaches",
                        ["base"] = "measured — the ground that renders where R falls to ~0, which is "
                              + "what a cliff face is"
                    },

                    ["splatMultiplier"] = Math.Round((double)materials.SplatMultiplier, 4),
                    ["splatPower"] = Math.Round((double)materials.SplatPower, 4)
                };

                report.AppendLine($"Splatmap: {captured.Width}x{captured.Height} ground-blend weights "
                                + $"captured (multiplier {materials.SplatMultiplier:F2}, power {materials.SplatPower:F2})");
                report.AppendLine($"  channels: R={(dirtBound ? "dirt" : "unknown")} "
                                + $"G={(grassBound ? "grass" : "unknown")} B=extra1 A=extra2, "
                                + $"{(rockBound ? "rock" : "unknown")} as the base under R");
                if (!dirtBound || !grassBound || !rockBound)
                {
                    List<string> missing = new List<string>();
                    if (!dirtBound) missing.Add("dirt");
                    if (!grassBound) missing.Add("grass");
                    if (!rockBound) missing.Add("rock");
                    report.AppendLine($"  note: this theme binds no {string.Join("/", missing)} ground, "
                                    + "so those channels ship unnamed rather than guessed");
                }
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"Splatmap capture failed: {ex.Message}");
                report.AppendLine($"Splatmap: {ex.Message}; consumers fall back to per-cell classification");
                return null;
            }
        }

        /// <summary>
        /// Read back the user's painted-surface weights — the brush strokes that weight the
        /// paintable extra grounds (<c>terrain/surfaces/extra*.png</c>).
        /// <c>TerrainMaterialSystem.UserSplatmap</c> (public; R8G8, playable extent, same
        /// mapping as the splatmap). Captured only when it exists and any texel is non-zero —
        /// a city with no painted surfaces ships nothing.
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "userSplatmap" block, or <c>null</c>.</param>
        /// <param name="report">The report.</param>
        /// <returns>The captured pixels, or <c>null</c>.</returns>
        private PendingTexture CaptureUserSplatmap(out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture user = materials?.UserSplatmap;
                if (user == null) return null;

                PendingTexture captured = MaterialRipSystem.Capture(user, "user_splatmap", "user splatmap", 0);
                if (captured?.Pixels == null) return null;

                bool painted = false;
                for (int i = 0; (i < captured.Pixels.Length) && !painted; i += 4)
                {
                    painted = (captured.Pixels[i] != 0) || (captured.Pixels[i + 1] != 0);
                }
                if (!painted) return null;

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/user_splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "player-painted surface weights for the paintable extra grounds "
                                  + "in terrain/surfaces/; channels are named in `channels` below",

                    // Straight off the paint path: ApplyToSplatmap switches on TerrainMaterialType
                    // and sets one component of the brush vector, and the target is R8G8 — so the
                    // enum's own ordering *is* the channel map, with no inference involved.
                    //
                    // Two channels, four grounds: each byte packs two 4-bit weights. Reading R or
                    // G as a single 0-255 weight merges two grounds and loses two (audit SPLAT-6).
                    //
                    ["channels"] = new Dictionary<string, object>
                    {
                        ["R"] = "extra1:extra2 (packed, low:high nibble)",
                        ["G"] = "extra3:extra4 (packed, low:high nibble)",
                        ["packed"] = new Dictionary<string, object>
                        {
                            ["R.low"] = "extra1",
                            ["R.high"] = "extra2",
                            ["G.low"] = "extra3",
                            ["G.high"] = "extra4",
                            ["decode"] = "extraN weight = (channel & 15) or (channel >> 4), "
                                       + "each 0..15; normalise by 15, NOT by 255"
                        }
                    },

                    ["channelProvenance"] = "measured — TerrainMaterialSystem.PackToBAChannels "
                                          + "writes the four brush components at bit offsets 0, 4, "
                                          + "8 and 12 across the two bytes, which is how four "
                                          + "extras fit an R8G8_UNorm target; each nibble is a "
                                          + "separate coherent painted region in a full-city rip"
                };

                report.AppendLine($"User splatmap: painted surfaces present, {captured.Width}x{captured.Height} captured");
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"User splatmap capture failed: {ex.Message}");
                return null;
            }
        }
    }
}
