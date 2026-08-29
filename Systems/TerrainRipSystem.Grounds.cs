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
    /// The theme's ground textures and the paintable-extra bookkeeping under them — what the
    /// splat weights are weights OF.
    /// </summary>
    public partial class TerrainRipSystem
    {
        private Dictionary<string, string> CaptureSurfaceTextures(string terrainDirectory, StringBuilder report)
        {
            Dictionary<string, string> written = new();
            string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
            if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);

            // How many paintable extras the user splatmap can actually weight. `ApplyToSplatmap`
            // maps Extra1..4 onto the x..w components of the brush vector, but the target texture
            // is what decides how many of those components exist — and it is R8G8_UNorm, two
            // channels, so the Extra3 and Extra4 writes land nowhere. Their ground textures are
            // therefore unreferenceable by any consumer, and capturing them cost 8.29 MiB of a
            // full-city bundle for weights no pixel can carry.
            //
            // Gated on the format rather than by name, so a game patch that widens the texture
            // re-enables them here automatically instead of silently shipping dead files again.
            //
            int paintableExtras = CountPaintableExtras(report);

            // Every other tally in this system is per-run; this one is an instance field and was
            // not. Rip, change the map theme or disable a pack, rip again in the same session:
            // `CleanStaleOutputs` wipes terrain/surfaces/, this pass writes only the grounds now
            // bound — and `sources.json` still declared the previous theme's, naming a
            // `.png` that is no longer in the bundle. Provenance that outlives what it describes
            // is worse than none.
            //
            _groundSources.Clear();

            // The theme binds a diffuse AND a tangent normal per ground, plus up to four
            // paintable extras (`colossal_TerrainExtra*`) most themes leave unbound — those
            // skip silently. Normal maps go through the same two-channel-packing rebuild the
            // material rip applies (§24.4); the extras carry the surfaces a player paints,
            // weighted by the user splatmap.
            //
            foreach (string[] entry in new[]
            {
                new[] { "grass", "colossal_TerrainGrassDiffuse", "core" },
                new[] { "grass_normal", "colossal_TerrainGrassNormal", "core" },
                new[] { "dirt", "colossal_TerrainDirtDiffuse", "core" },
                new[] { "dirt_normal", "colossal_TerrainDirtNormal", "core" },
                new[] { "rock", "colossal_TerrainRockDiffuse", "core" },
                new[] { "rock_normal", "colossal_TerrainRockNormal", "core" },
                new[] { "extra1", "colossal_TerrainExtra1Diffuse", "extra" },
                new[] { "extra1_normal", "colossal_TerrainExtra1Normal", "extra" },
                new[] { "extra2", "colossal_TerrainExtra2Diffuse", "extra" },
                new[] { "extra2_normal", "colossal_TerrainExtra2Normal", "extra" },
                new[] { "extra3", "colossal_TerrainExtra3Diffuse", "extra" },
                new[] { "extra3_normal", "colossal_TerrainExtra3Normal", "extra" },
                new[] { "extra4", "colossal_TerrainExtra4Diffuse", "extra" },
                new[] { "extra4_normal", "colossal_TerrainExtra4Normal", "extra" }
            })
            {
                string name = entry[0];
                bool core = entry[2] == "core";

                // An extra whose weight channel does not exist is skipped even when the theme
                // binds it — the theme is allowed to bind a ground the splatmap cannot select.
                //
                if (!core && (ExtraIndex(name) > paintableExtras)) continue;

                try
                {
                    Texture texture = Shader.GetGlobalTexture(entry[1]);
                    if (texture == null)
                    {
                        if (core) report.AppendLine($"Surface texture '{name}': not bound; skipped");
                        continue;
                    }

                    // Unbound slots are filled with Unity's built-in 4×4 placeholders
                    // (`ApplyRenderSettings` falls back to `Texture2D.grayTexture`); a real
                    // ground texture is never that small.
                    if (!core && ((texture.width <= 8) || (texture.height <= 8))) continue;

                    PendingTexture captured = MaterialRipSystem.CaptureNow(texture, name, name, 1024);
                    if (captured?.Pixels == null)
                    {
                        report.AppendLine($"Surface texture '{name}': readback failed; skipped");
                        continue;
                    }

                    if (name.EndsWith("_normal", StringComparison.Ordinal))
                        MaterialRipSystem.ReconstructTwoChannelNormal(captured.Pixels);

                    byte[] png = Png.EncodeRgba(captured.Pixels, captured.Width, captured.Height);
                    File.WriteAllBytes(IOUtils.CombinePath(surfacesDirectory, $"{name}.png"), png);
                    written[name] = $"terrain/surfaces/{name}.png";

                    // Record WHICH asset this ground came from. Without it a bundle says only
                    // "this is extra2", never "extra2 was captured from <asset>", and a ground
                    // that looks wrong cannot be told from a ground that was captured wrong.
                    // Without it, painted beaches rendering as moss could not be diagnosed from
                    // the bundle at all: it named the ground but never the asset behind it.
                    _groundSources[name] = new Dictionary<string, object>
                    {
                        ["texture"] = texture.name ?? "(unnamed)",
                        ["boundAs"] = entry[1],
                        ["sourceSize"] = new[] { texture.width, texture.height },
                        ["writtenSize"] = new[] { captured.Width, captured.Height }
                    };
                }
                catch (Exception ex)
                {
                    _log.Warn($"Surface texture '{name}' failed: {ex.Message}");
                    report.AppendLine($"Surface texture '{name}': {ex.Message}");
                }
            }

            // Provenance goes in its own file, NOT into `rasters`: that object is closed in the
            // schema, so a key there would force a schema bump for what is pure diagnostics.
            // A new file is additive (contract §12).
            if (_groundSources.Count > 0)
            {
                Dictionary<string, object> sources = new Dictionary<string, object>
                {
                    ["semantics"] = "which texture asset each terrain/surfaces/<ground>.png was "
                                  + "captured from, and the shader global it was bound as. "
                                  + "Provenance only — nothing joins to it. It exists so a ground "
                                  + "that RENDERS wrong can be told apart from a ground that was "
                                  + "CAPTURED wrong; without it neither question is answerable "
                                  + "from a bundle at all",
                    ["grounds"] = new Dictionary<string, object>(_groundSources)
                };
                // The only unguarded write in this method, and the code above calls this file
                // "provenance only — nothing joins to it". `RipTerrain` has no outer try, so a
                // locked or unwritable sources.json threw straight out of the terrain stage and
                // took everything after it: splatmap, user splatmap, tiling, backdrop, clip
                // volumes, areas and rasters.json. A diagnostic may not cost a consumer the
                // layers it actually needs.
                //
                try
                {
                    Carto.IO.JsonFile.Write(IOUtils.CombinePath(surfacesDirectory, "sources.json"), sources);
                }
                catch (Exception ex)
                {
                    report.AppendLine($"Surface sources: {ex.Message}; sources.json omitted");
                    _log.Warn($"Could not write sources.json: {ex.Message}");
                }

                foreach (KeyValuePair<string, object> pair in _groundSources)
                {
                    Dictionary<string, object> d = (Dictionary<string, object>)pair.Value;
                    report.AppendLine($"  {pair.Key} <- '{d["texture"]}' ({d["boundAs"]})");
                }
            }

            report.AppendLine($"Surfaces: {written.Count} ground texture(s) written");
            return written;
        }

        /// <summary>
        /// Which of <c>extra1..extra4</c> a surface name refers to, or 0 when it is not an extra.
        /// </summary>
        /// <param name="name">The surface name, e.g. <c>extra3_normal</c>.</param>
        /// <returns>1..4, or 0.</returns>
        private static int ExtraIndex(string name)
        {
            if ((name == null) || !name.StartsWith("extra", StringComparison.Ordinal)) return 0;
            return ((name.Length > 5) && (name[5] >= '1') && (name[5] <= '4')) ? name[5] - '0' : 0;
        }

        /// <summary>
        /// How many paintable extra grounds the user splatmap can actually weight — <b>all four</b>.
        /// <para>
        /// <b>This used to return 2, and that was the bug that lost the player's sand
        /// (audit SPLAT-6).</b> It reasoned "one weight per channel", read the
        /// <c>R8G8_UNorm</c> target's component count, and concluded that only Extra1 and
        /// Extra2 could carry weight — so <c>extra3</c> and <c>extra4</c> shipped no texture at
        /// all. The premise is wrong: the game packs <b>two 4-bit weights per channel</b>, which
        /// is how four extras fit in two bytes. <c>TerrainMaterialSystem.PackToBAChannels</c>
        /// writes exactly that layout — the four brush components at bit offsets 0, 4, 8 and 12
        /// across the two bytes.
        /// </para>
        /// <para>
        /// Confirmed against the game and against a real city: the locale names the four tools
        /// <c>Texture Paint Tool 01..04</c> = Dry / Lush / Dirt / <b>Sand</b>, and in a full-city
        /// rip each of the four nibbles is a separate, coherent, brush-shaped painted region —
        /// with Extra4 (Sand) tracing the shorelines, which is exactly where a player paints
        /// beaches. Two of the four grounds a city can paint were simply absent from the bundle.
        /// </para>
        /// <para>
        /// The count is now a constant, deliberately. Deriving it from the texture format is what
        /// went wrong, and no format inspection can reveal a packing convention.
        /// </para>
        /// </summary>
        /// <param name="report">The report to note the finding in.</param>
        /// <returns>4 — every extra the paint tools can write.</returns>
        private int CountPaintableExtras(StringBuilder report)
        {
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture user = materials?.UserSplatmap;
                if (user == null) return 4;

                // Reported, not used as a gate: the component count is real information about the
                // target, it just does not bound how many weights that target carries.
                int channels = (int)UnityEngine.Experimental.Rendering.GraphicsFormatUtility
                                    .GetComponentCount(user.graphicsFormat);
                report.AppendLine($"Surfaces: user splatmap is {channels}-channel, carrying 4 "
                                + "paintable extras as 4-bit nibble pairs (two per channel)");
                return 4;
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the user splatmap's format: {ex.Message}");
                report.AppendLine($"Surfaces: user splatmap format unreadable ({ex.Message}); "
                                + "capturing all four paintable extras");
                return 4;
            }
        }
    }
}
