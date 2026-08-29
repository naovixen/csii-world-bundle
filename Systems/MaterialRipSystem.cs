using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// The system that resolves CS2 materials and textures into the bundle's <c>materials.json</c>
    /// and <c>textures/</c> (PLAN Phase 4).
    /// A CS2 <see cref="RenderPrefab"/> holds one <see cref="SurfaceAsset"/> per material slot, and
    /// a surface is a property bag: dictionaries of textures, colours, floats and shader keywords,
    /// keyed by HDRP-style names (<c>_BaseColorMap</c>, <c>_NormalMap</c>, <c>_MaskMap</c>,
    /// <c>_EmissiveColorMap</c>, <c>_ControlMask</c>). Those five maps plus a handful of scalars are
    /// exactly the model the bundle contract froze in §7, so the reduction is mostly a lookup — the
    /// work is in getting the pixels out.
    /// <b>Note that not every map name ends in <c>Map</c>.</b> <c>_ControlMask</c> is the colour
    /// mask, and it went unnoticed because every sweep for CS2's texture properties had been
    /// written as a search for <c>_*Map</c> — so <c>colors.json</c> was built and shipped with a
    /// palette nothing could apply. See <see cref="MaterialMap.ColorMask"/>.
    /// <b>Deduplication is the whole economy of this phase.</b> A surface asset is shared by every
    /// prefab that uses it and a texture by every surface, so both are keyed by asset identity and
    /// resolved once. What lands on disk is deduped a second time by content hash, because two
    /// distinct assets can still hold the same image.
    /// </summary>
    public partial class MaterialRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        // The CS2 surface property names behind each of the contract's four maps. CS2 is HDRP-
        // derived, so these are the stock HDRP Lit names.
        //
        private const string BaseColorMap = "_BaseColorMap";
        private const string NormalMap = "_NormalMap";
        private const string MaskMap = "_MaskMap";
        private const string EmissiveColorMap = "_EmissiveColorMap";

        /// <summary>
        /// The colour mask. Not <c>*Map</c>-suffixed, which is exactly why it was missed.
        /// </summary>
        private const string ControlMask = "_ControlMask";

        /// <summary>
        /// The same five names in the contract's map order, for the passes that have to sweep every
        /// map rather than ask for one.
        /// </summary>
        private static readonly string[] Maps = { BaseColorMap, NormalMap, MaskMap, EmissiveColorMap, ControlMask };

        /// <summary>
        /// The materials ripped so far in the run, or <c>null</c> when idle.
        /// </summary>
        MaterialRun _run;


        /// <summary>
        /// Release a run in flight.
        /// </summary>
        public void Abandon()
        {
            CancelReadbacks(_run);
            _run = null;
        }

        /// <summary>
        /// Encode and write one queued texture. Called on the rip's writer thread.
        /// </summary>
        /// <param name="texture">The texture to write.</param>
        public void WriteTexture(PendingTexture texture) => _run?.Textures.Write(texture);

        /// <summary>
        /// Wait for every texture encode in flight. Called by the writer once its queue drains.
        /// </summary>
        public void DrainTextures() => _run?.Textures.Drain();


        /// <summary>
        /// Look up one of the contract's maps on a surface and make sure its pixels are on their
        /// way to disk.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <returns>The texture key, or <c>null</c> when the surface has no such map.</returns>
        private string Take(MaterialRun run, SurfaceAsset surface, string property, RenderPrefab renderPrefab,
                            int materialIndex, SurfaceData own)
        {
            // A negative cap means textures are deselected outright (the ripper tab's advanced
            // toggle): materials ship with their tints and scalars only, every map key resolves
            // to null, and ToJson omits the map — the same degradation a failed readback takes.
            //
            if (run.TextureSizeCap < 0) return null;

            if (!surface.TryGetTexture(property, out TextureAsset texture) || (texture == null)) return null;

            // Locate before claiming. For a virtual-textured map the identity that matters is the
            // preprocessed texture's, not the stub's — two surfaces sharing a packed layer hold
            // different stubs pointing at the same pixels, and keying on the stub reads those
            // pixels once per surface. A full-city rip made that concrete: 1,020 readbacks
            // produced 321 distinct images, so two thirds of the most expensive work in the phase
            // was spent re-decoding textures already in hand.
            //
            TextureSource source = Locate(run, surface, property, texture, renderPrefab, materialIndex, own);
            if (source.Key == null) return null;

            // A claim that goes on to fail is still worth keeping: it stops every later surface
            // retrying a lookup already known to lose.
            //
            if (!run.Textures.Claim(source.Key)) return source.Key;

            Stopwatch clock = Stopwatch.StartNew();
            try
            {
                PendingTexture pending;
                if (source.IsVirtual) pending = ReadBackVirtual(run, source, surface.name, property);
                else if (source.Live != null) pending = ReadBackLive(run, source, surface.name);
                else pending = ReadBack(source.Standalone, source.Key, run.TextureSizeCap);

                if (pending == null) return null;

                // Normal maps only: the slot decides, the packing signature merely confirms.
                // Content sniffing alone was measured to false-positive on five mask maps of a
                // full city (metallic-1, varying smoothness), which reconstruction would corrupt.
                // Run on the encoder thread once the pixels have landed, not here.
                //
                if (property == NormalMap) pending.PostProcess = ReconstructTwoChannelNormal;

                GpuReadback issued = _issued.Value;
                _issued.Value = null;
                if (issued != null) run.Readbacks.Add(issued);

                run.Enqueue(pending);
                run.TexturesRead++;
                return source.Key;
            }
            catch (Exception ex)
            {
                Skip(run, source.Key, $"{property}: {ex.GetType().Name} {ex.Message}");

                // Log the first few in full and count the rest. A failure that hits every texture
                // in the city writes the same stack trace thousands of times otherwise, which
                // buries whatever else the log had to say.
                //
                if (++run.ReadFailures <= 5) _log.Warn($"Failed to read texture `{source.Key}` ({property}): {ex}");
                return null;
            }
            finally
            {
                run.ReadbackMilliseconds += clock.Elapsed.TotalMilliseconds;
            }
        }


        /// <summary>
        /// Fill in the flags and face mode a surface's material template implies.
        /// CS2 builds every surface from a named template — <c>defaultCutoutDoubleSided</c>,
        /// <c>vegetationLeaves</c>, <c>glass</c> and so on — and the name is the only place that
        /// says whether back faces are drawn or what kind of surface this is meant to be. The
        /// contract's flags are hints a consumer may ignore, so only signals that are actually
        /// legible in the template name are emitted: a wrong hint is worse than a missing one.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <param name="material">The material being built.</param>
        private static void DescribeTemplate(SurfaceAsset surface, MaterialAsset material)
        {
            string template;
            try
            {
                template = surface.GetTemplateMaterial()?.name;
            }
            catch (Exception ex)
            {
                _log.Verbose($"No material template for `{surface.name}`: {ex.Message}");
                return;
            }

            if (string.IsNullOrEmpty(template)) return;

            string name = template.ToLowerInvariant();

            // The shader the game actually draws this surface with, shipped verbatim so a
            // consumer can judge on evidence instead of on a name the producer guessed at.
            //
            // The case that forced it: `Invisible_Pathway` serialises to `{}` — no maps, no
            // tint, no alpha mode, because the invisible shader binds none of the properties
            // the sweep reads — so a consumer has nothing to distinguish "a plain material" from
            // "geometry the game does not draw", and renders 91 taxiway segments as opaque slabs
            // across an airport. Before 2026-08-11 that was hidden by the piece being dropped
            // whenever its render cache was cold (§42.6); making the read deterministic made the
            // slabs deterministic too.
            //
            // `flags` and not a new key: contract §9's material object is closed
            // (`additionalProperties: false`), so a `template` key would take the schema to 3,
            // while `flags` is documented as an open list of hints a consumer may act on or
            // ignore. Adding a value to it is additive by construction.
            //
            material.Flags.Add($"template:{template}");

            // Double-sidedness is a material *property* before it is a naming convention: HDRP's
            // switch is the `_DoubleSidedEnable` float, which the surface can carry itself
            // (`SyncToUnityMaterial` copies every float the shader knows) and the template
            // material holds otherwise. The name check stays as the fallback for templates whose
            // shader hides the property.
            //
            if (surface.TryGetFloatProperty("_DoubleSidedEnable", out float doubleSided))
            {
                material.DoubleSided = doubleSided > 0.5f;
            }
            else
            {
                Material templateMaterial = null;
                try { templateMaterial = surface.GetTemplateMaterial(); } catch (Exception) { /* name fallback below */ }

                material.DoubleSided = ((templateMaterial != null) && templateMaterial.HasProperty("_DoubleSidedEnable"))
                    ? templateMaterial.GetFloat("_DoubleSidedEnable") > 0.5f
                    : name.Contains("doublesided");
            }

            if (name.Contains("vegetation") || name.Contains("grass") || name.Contains("leaves") || name.Contains("branches"))
                material.Flags.Add("foliage");

            if (name.Contains("glass")) material.Flags.Add("glass");
            if (name.Contains("decal")) material.Flags.Add("decal");

            // CS2 lights windows individually by looking up an id in the emissive map's companion
            // textures; a surface carrying those is the emissive-windows case the contract names.
            //
            if (!string.IsNullOrEmpty(material.MapKeys[(int)MaterialMap.Emissive])
                && surface.HasProperty("_EmissiveID0"))
                material.Flags.Add("emissiveWindows");
        }

        // ====================================================================================
        // Finishing.
        // ====================================================================================

    }
}
