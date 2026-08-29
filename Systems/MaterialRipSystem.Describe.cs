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
    /// One CS2 surface as a bundle material: its id, its description, and the texture references
    /// it resolves to.
    /// </summary>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Resolve a material id to what a glTF writer needs, texture paths and all.
        /// Safe from the writer thread, and correct there because of the order the rip enqueues
        /// work in: a prefab's surfaces are resolved and its textures queued <i>before</i> the
        /// prefab itself is queued, and one writer drains that queue in order. So by the time a
        /// mesh is serialised, every texture it names has already been encoded and has a hash.
        /// </summary>
        /// <param name="materialId">The bundle material id.</param>
        /// <returns>The description, with <c>Found</c> false when the id is not in the table.</returns>
        public MaterialDescription Describe(string materialId)
        {
            MaterialRun run = _run;
            if ((run == null) || string.IsNullOrEmpty(materialId)) return default;
            return run.Materials.TryGetValue(materialId, out MaterialAsset material)
                ? material.Describe(run.Textures.Resolve) : default;
        }

        /// <summary>
        /// Resolve one material slot of a render prefab to its bundle material id, ripping the
        /// surface and its textures the first time they are seen.
        /// Returns <c>null</c> when the slot resolves to nothing, which leaves the caller free to
        /// fall back to a synthetic id rather than write a glTF primitive with no material at all.
        /// </summary>
        /// <param name="renderPrefab">The render prefab the mesh came from.</param>
        /// <param name="materialIndex">The material slot.</param>
        /// <returns>The bundle material id, or <c>null</c>.</returns>
        public string Resolve(RenderPrefab renderPrefab, int materialIndex)
        {
            MaterialRun run = _run;
            if ((run == null) || (renderPrefab == null)) return null;
            if ((materialIndex < 0) || (materialIndex >= renderPrefab.materialCount)) return null;

            SurfaceAsset surface;
            try
            {
                surface = renderPrefab.GetSurfaceAsset(materialIndex);
            }
            catch (Exception ex)
            {
                _log.Warn($"Surface asset {materialIndex} of `{renderPrefab.name}` did not resolve: {ex.Message}");
                return null;
            }

            if (surface == null) return null;

            string identity = Identity(surface);
            if (run.BySurface.TryGetValue(identity, out string existing)) return existing;

            run.Surfaces++;

            try
            {
                string materialId = Rip(run, surface, identity, renderPrefab, materialIndex);
                run.BySurface[identity] = materialId;
                return materialId;
            }
            catch (Exception ex)
            {
                run.Failed++;
                run.BySurface[identity] = null;
                run.Skipped.Add(new RipWarning("material", surface.name ?? identity, $"{ex.GetType().Name} {ex.Message}"));
                _log.Warn($"Failed to rip surface `{surface.name}`: {ex}");
                return null;
            }
        }

        /// <summary>
        /// Declare a material id the meshes use but no surface asset stood behind, so that it is a
        /// key in <c>materials.json</c> with no maps rather than a dangling reference.
        /// §6 of the contract makes a glTF material's name a key into the table, so the honest
        /// response to an unresolvable surface is an empty entry plus a line in
        /// <c>warnings.jsonl</c> — the same "declare the gap" rule the mesh layer already follows.
        /// A consumer sees a material with no maps and substitutes its own default, which is
        /// precisely what §7 says it should do.
        /// </summary>
        /// <param name="materialId">The id the meshes reference.</param>
        /// <param name="reason">Why no surface stood behind it.</param>
        public void Declare(string materialId, string reason)
        {
            MaterialRun run = _run;
            if ((run == null) || string.IsNullOrEmpty(materialId)) return;
            if (run.Materials.ContainsKey(materialId)) return;

            run.Materials[materialId] = new MaterialAsset { MaterialId = materialId, SourceName = materialId };
            run.Skipped.Add(new RipWarning("material", materialId, reason));
        }

        /// <summary>
        /// Reduce one surface asset to a <see cref="MaterialAsset"/> and queue its textures.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        /// <param name="identity">Its stable identity.</param>
        /// <returns>The bundle material id.</returns>
        private string Rip(MaterialRun run, SurfaceAsset surface, string identity, RenderPrefab renderPrefab, int materialIndex)
        {
            // The property dictionaries are empty until the asset's data is read off disk. The flag
            // is not "load more" — it lands in `m_UsingVT = m_IsVTMaterial && flag`, so passing
            // false tells a virtual-textured surface it is not one, which is a state the game never
            // puts it in and which `IsHandledByVirtualTexturing` then reads back.
            //
            surface.LoadProperties(true);

            // Shared by all four map lookups so that a surface which needs its own VT data decodes
            // it once instead of four times. Nothing is opened unless a map actually asks.
            //
            SurfaceData own = new() { Surface = surface };

            try
            {
                MaterialAsset material = new()
                {
                    MaterialId = AssignId(run, surface, identity),
                    SourceName = surface.name
                };

                material.MapKeys[(int)MaterialMap.BaseColor] = Take(run, surface, BaseColorMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Normal] = Take(run, surface, NormalMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Mask] = Take(run, surface, MaskMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Emissive] = Take(run, surface, EmissiveColorMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.ColorMask] = Take(run, surface, ControlMask, renderPrefab, materialIndex, own);

                if (!string.IsNullOrEmpty(material.MapKeys[(int)MaterialMap.ColorMask])) run.ColorMasks++;

                // What the material library says this surface's decode binds, recorded so that
                // "is there a map we are still not exporting?" is answered by the run's own report
                // rather than by another offline sweep. The colour mask was missing because that
                // question had only ever been put to a regex, never to the game.
                //
                DeclaredProperties(run, surface);

                if (surface.TryGetColorProperty("_BaseColor", out Color tint))
                    material.Tint = new Unity.Mathematics.float4(tint.r, tint.g, tint.b, tint.a);

                if (surface.TryGetColorProperty("_EmissiveColor", out Color emissive))
                    material.EmissiveFactor = new Unity.Mathematics.float3(emissive.r, emissive.g, emissive.b);

                material.AlphaMode = AlphaMode(surface);
                if (surface.TryGetFloatProperty("_AlphaCutoff", out float cutoff))
                {
                    material.AlphaCutoff = cutoff;
                }
                else if (material.AlphaMode == "mask")
                {
                    // A surface that clips through its template alone carries no cutoff of its
                    // own either — the leaf surfaces serialise only wind floats — so the value
                    // the game actually tests against is the template's.
                    //
                    try
                    {
                        UnityEngine.Material template = surface.GetTemplateMaterial();
                        if ((template != null) && template.HasProperty("_AlphaCutoff"))
                            material.AlphaCutoff = template.GetFloat("_AlphaCutoff");
                    }
                    catch (Exception) { /* glTF's own default of 0.5 applies */ }
                }

                DescribeTemplate(surface, material);

                // A decal is alpha compositing by definition — the shader projects it over
                // whatever is beneath and blends by the base colour's alpha — but decal shaders
                // carry neither of the lit-shader keywords the sweep above reads, so the mode
                // comes out `opaque` and a consumer renders the marking's whole texture rectangle
                // as a solid box. The template names the truth the keywords cannot.
                //
                if ((material.AlphaMode == "opaque") && material.Flags.Contains("decal"))
                    material.AlphaMode = "blend";

                run.Materials[material.MaterialId] = material;
                return material.MaterialId;
            }
            finally
            {
                Release(run, own);

                // Every surface in a city held open at once is a large amount of managed state for
                // dictionaries we have already copied out of.
                //
                try { surface.UnloadProperties(false); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Give a surface asset its bundle material id, keeping ids distinct across the bundle.
        /// The id is a join key, so a collision is silent corruption in exactly the way a colliding
        /// <c>assetId</c> is: two surfaces sharing an id means one entry in <c>materials.json</c>
        /// overwrites the other and every primitive naming it gets whichever won. CS2 does not
        /// require surface names to be unique, and generic names like <c>Default</c> are common.
        /// Unlike the prefab set, surfaces are discovered one at a time as the rip walks prefabs —
        /// there is no point at which the whole set is in hand to assign over. The first claimant
        /// therefore keeps the readable name and later ones take a suffix, which is collision-free
        /// within a bundle. That is all the contract needs: a material id only ever has to join
        /// <c>meshes/*.glb</c> to the <c>materials.json</c> written beside them in the same run.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        /// <param name="identity">Its stable identity.</param>
        /// <returns>An id no other material in this bundle holds.</returns>
        private static string AssignId(MaterialRun run, SurfaceAsset surface, string identity)
        {
            string id = RipAssetIds.SanitizeAssetId(surface.name);
            if (!run.Materials.ContainsKey(id)) return id;

            run.Disambiguated++;
            return RipAssetIds.SanitizeAssetId($"{id}_{Digest(identity)}");
        }
    }
}
