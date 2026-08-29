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
    /// One §8b area prefab as a catalogue entry: its key, its rendered material, and the
    /// textures that material names.
    /// </summary>
    /// <remarks>
    /// The catalogue is what makes <c>areas.jsonl</c> legible — a record names a prefab key and
    /// nothing else. A prefab the rip session could not resolve is recorded as unresolved rather
    /// than silently dropped: the game drew those areas grey too, and the cure is enabling the
    /// pack in-game, not anything on the consumer side.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Add one area prefab to the catalogue: its name, everything <c>AreaBatchSystem</c> pushes
        /// onto the batch material for it, and each authored map captured into
        /// <c>terrain/surfaces/</c>.
        /// <b>Only what the game actually pushes is written.</b> The scalar PBR and edge parameters
        /// live on <c>RenderedArea</c> but reach the shader only under
        /// <c>Version.OverrideMaterial</c> (<c>AreaBatchSystem</c> 2103-2113); for an
        /// <c>Original</c> prefab the baked material already carries them, so they are read back
        /// off it instead. <c>m_LodBias</c> is deliberately absent: it lands on
        /// <c>AreaGeometryData</c>, not on the <c>RenderedAreaData.m_LodBias</c> field that looks
        /// like its home and is never assigned, and all it does there is bias the per-triangle
        /// <c>m_MinLod</c> distance cull — the bundle ships every triangle, so it changes nothing a
        /// consumer draws.
        /// </summary>
        /// <param name="prefab">The area prefab entity.</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory.</param>
        /// <param name="usedKeys">Keys taken so far, for uniqueness over the run.</param>
        /// <param name="catalogue">The catalogue to add the entry to.</param>
        /// <returns>The catalogue key the records reference, or <c>null</c> for a prefab the game
        /// structurally never paints (no <c>RenderedArea</c> — the Clip Surface); the caller skips
        /// those areas.</returns>
        private string CatalogueAreaPrefab(Entity prefab, string terrainDirectory,
                                           HashSet<string> usedKeys, Dictionary<string, object> catalogue)
        {
            string name = Instance.Name.GetPrefabName(prefab);
            if (string.IsNullOrEmpty(name)) name = $"area_{prefab.Index}";

            string key = RipAssetIds.SanitizeAssetId(name);
            if (!usedKeys.Add(key))
            {
                key = $"{key}_{prefab.Index}";
                usedKeys.Add(key);
            }

            var entry = new Dictionary<string, object> { ["name"] = name };
            catalogue[key] = entry;

            try
            {
                // A negative `PrefabData.m_Index` means the save references this surface by
                // PrefabID but the asset — an asset pack, in every observed case — was not
                // loaded this session: `PrefabSystem.TryGetPrefab` refuses it, and the game
                // itself draws those areas with `MeshSettingsData.m_MissingRenderedArea`
                // (grey paint). Colourless is therefore game-true for the session that ripped,
                // but flag it so the report and a consumer can tell "pack missing" apart from
                // "authored colourless" — the cure is enabling the pack and re-ripping.
                //
                if (!EntityManager.HasComponent<PrefabData>(prefab)
                    || !Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefab),
                                                     out PrefabBase prefabBase)
                    || (prefabBase == null))
                {
                    entry["unresolved"] = true;

                    // The game's full identity for the missing asset: `SurfacePrefab:Name (hash)`,
                    // where the hash is `Hash128.Calculate` over the source mod's PDX platform ID.
                    // Not reversible, but it convicts a candidate pack: hash its mod ID and compare.
                    //
                    if (EntityManager.HasComponent<PrefabData>(prefab))
                    {
                        PrefabData prefabData = EntityManager.GetComponentData<PrefabData>(prefab);
                        if (prefabData.m_Index < 0)
                            entry["prefabId"] = Instance.Prefab.GetObsoleteID(prefabData).ToString();
                    }
                    return key;
                }

                if (!prefabBase.TryGet(out RenderedArea rendered) || (rendered == null))
                {
                    // Resolved, but no `RenderedArea`: only that component's archetype hook puts
                    // `Game.Areas.Batch` on an area entity, and every AreaBatchSystem draw/cull
                    // query requires `Batch` — the game structurally never paints these. The one
                    // case in the wild is the map editor's Clip Surface, whose triangles already
                    // ship through clipvolumes.jsonl (§8c); painting them would draw boxes over
                    // every tunnel portal that the game does not draw. Return null so the caller
                    // skips the areas and counts them.
                    //
                    catalogue.Remove(key);
                    return null;
                }

                {
                    // The two authored numbers a consumer otherwise has to invent. `m_UVScale`
                    // goes straight to the shader as `colossal_UVScale` (AreaBatchSystem) and
                    // multiplies the WORLD position — the area's texture is world-planar, not
                    // UV-mapped, so without it every consumer guesses a metres-per-repeat.
                    // `RenderedAreaData.m_HeightOffset` expands the projection prism beyond the
                    // terrain's own departure from the triangle (contract §8b, audit §30).
                    //
                    entry["uvScale"] = Math.Round((double)rendered.m_UVScale, 5);
                    if (EntityManager.HasComponent<RenderedAreaData>(prefab))
                    {
                        RenderedAreaData data = EntityManager.GetComponentData<RenderedAreaData>(prefab);
                        entry["heightOffset"] = Math.Round((double)data.m_HeightOffset, 4);

                        // The game's own derived edge-rounding radius in metres:
                        // `clamp(m_Roundness, 0.01, 0.99) * GetMinNodeDistance(type) * 0.5`, set
                        // beside `m_HeightOffset` in the same batch build. The shader gets twice
                        // it as `colossal_AreaParameters.x`. Shipping the derived metres saves a
                        // consumer having to know the per-area-type node distance.
                        //
                        entry["expandAmount"] = Math.Round((double)data.m_ExpandAmount, 4);
                    }

                    // Pushed onto every area batch whatever `m_Version` says
                    // (`AreaBatchSystem` ~2088-2093): the authored edge roundness the radius above
                    // derives from, the mask deciding which decal layers project onto this
                    // surface, and the draw-order tiebreak — without which a consumer z-fights
                    // every overlap of two painted areas.
                    //
                    // The mask ships as flag names rather than as the raw bits, the way
                    // `instances.jsonl` already ships a decal's own `decalLayers` — one vocabulary
                    // for the same concept across the bundle.
                    //
                    entry["roundness"] = Math.Round((double)rendered.m_Roundness, 4);
                    entry["rendererPriority"] = rendered.m_RendererPriority;

                    string layers = rendered.m_DecalLayerMask.ToString();
                    if (!string.IsNullOrEmpty(layers) && !char.IsDigit(layers[0]))
                    {
                        List<string> names = new();
                        foreach (string layer in layers.Split(','))
                        {
                            string trimmed = layer.Trim();
                            if (trimmed.Length > 0) names.Add(trimmed);
                        }

                        if (names.Count > 0) entry["decalLayers"] = names.ToArray();
                    }

                    // The PNG encode is the expensive half; the capture (GPU readback) must stay
                    // on the main thread, so the pixels are queued and the writer task encodes.
                    //
                    void WriteAreaTexture(PendingTexture captured, string field, string suffix)
                    {
                        if (captured?.Pixels == null) return;

                        // Area normal maps are two-channel-packed exactly as object and ground
                        // normals are, and this path was the one that never unpacked them —
                        // measured on the reference city, 20 of 35 shipped with red pinned to
                        // white, so a consumer reading them as ordinary tangent normals got
                        // `normal.x = +1` for every texel and every painted surface shaded
                        // black. Contract §7.1 promises ONE convention for every normal map in
                        // the bundle; this is where the area half of that promise is kept.
                        //
                        if (field == "normalMap") MaterialRipSystem.ReconstructTwoChannelNormal(captured.Pixels);

                        string fileName = $"area_{key}{suffix}.png";
                        string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
                        if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);
                        _pendingAreaTextures.Add(new KeyValuePair<string, PendingTexture>(
                            IOUtils.CombinePath(surfacesDirectory, fileName), captured));
                        entry[field] = $"terrain/surfaces/{fileName}";
                    }

                    // The branch keys on `m_Version`, as the game's batch build effectively
                    // does: the base material is `m_Material` when present (else the global
                    // `AreasConfigurationPrefab.m_RenderedAreaMaterial` template, with
                    // `m_Version` forced to `OverrideMaterial`), and the authored fields are
                    // pushed onto the clone whenever the version says `OverrideMaterial` —
                    // which a surface can carry WITH a baked material. Keying on
                    // `material == null` alone missed that case (audit T-F13b).
                    //
                    Material material = rendered.m_Material;
                    bool overridden = (rendered.m_Version == RenderedArea.Version.OverrideMaterial)
                                   || (material == null);
                    if (!overridden)
                    {
                        // `RenderedArea.Version.Original` — vanilla surfaces: the baked material
                        // carries colour and base map directly.
                        //
                        if (material.HasProperty("_BaseColor"))
                        {
                            Color color = material.GetColor("_BaseColor");
                            entry["color"] = new[]
                            {
                                Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                                Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                            };
                        }

                        Texture texture = material.mainTexture;
                        if ((texture == null) && material.HasProperty("_BaseColorMap"))
                            texture = material.GetTexture("_BaseColorMap");

                        if (texture != null)
                            WriteAreaTexture(MaterialRipSystem.CaptureNow(texture, key, name, 1024), "texture", "");

                        // The rest of the surface's PBR, which the batch build never touches for
                        // an `Original` prefab precisely because the baked material already
                        // carries it: the clone inherits `_NormalMap`/`_MaskMap` and the four
                        // scalar weights, and the shader reads them at draw. Only what the
                        // material actually declares is shipped — `HasProperty` is the authored
                        // test here, the way `m_Version` is in the override branch below.
                        //
                        WriteMaterialTexture("_NormalMap", "normalMap", "_n");
                        WriteMaterialTexture("_MaskMap", "maskMap", "_m");
                        WriteMaterialFloat("_Metallic", "metallic");
                        WriteMaterialFloat("_Smoothness", "smoothness");
                        WriteMaterialFloat("_NormalOpacity", "normalOpacity");
                        WriteMaterialFloat("_MetallicOpacity", "metallicOpacity");
                        WriteMaterialFloat("_NormalAlphaSource", "normalAlphaSource");
                        WriteMaterialFloat("_MetallicAlphaSource", "metallicAlphaSource");

                        void WriteMaterialFloat(string property, string field)
                        {
                            if (material.HasProperty(property))
                                entry[field] = Math.Round((double)material.GetFloat(property), 4);
                        }

                        void WriteMaterialTexture(string property, string field, string suffix)
                        {
                            if (!material.HasProperty(property)) return;
                            Texture map = material.GetTexture(property);
                            if (map != null)
                                WriteAreaTexture(MaterialRipSystem.CaptureNow(map, $"{key}{suffix}", name, 1024), field, suffix);
                        }
                    }
                    else
                    {
                        // `OverrideMaterial` — editor-authored surfaces, which is every
                        // asset-pack surface. The game pushes `_BaseColor` := `m_BaseColor`
                        // always. Maps follow `Initialize`'s rule: when ANY of the three
                        // override maps is authored, `_BaseColorMap` := `m_BaseColorMap` (a
                        // null asset CLEARS the slot, not a fallback); with none authored, the
                        // base material keeps its own map — the template has none, but a baked
                        // `m_Material` may.
                        //
                        Color color = rendered.m_BaseColor;
                        entry["color"] = new[]
                        {
                            Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                            Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                        };

                        // The scalar half of the override push, all of it unconditional inside
                        // this branch (`AreaBatchSystem` 2103-2113) and all of it read by the
                        // area shader. The two opacities and the two alpha sources come with the
                        // maps rather than after them: they are what decides how much of the
                        // normal map bends the surface and which channel of the mask drives
                        // metallic, so a consumer given `normalMap` without them is guessing.
                        //
                        entry["metallic"] = Math.Round((double)rendered.m_Metallic, 4);
                        entry["smoothness"] = Math.Round((double)rendered.m_Smoothness, 4);
                        entry["normalOpacity"] = Math.Round((double)rendered.m_NormalOpacity, 4);
                        entry["metallicOpacity"] = Math.Round((double)rendered.m_MetallicOpacity, 4);
                        entry["normalAlphaSource"] = Math.Round((double)rendered.m_NormalAlphaSource, 4);
                        entry["metallicAlphaSource"] = Math.Round((double)rendered.m_MetallicAlphaSource, 4);

                        // How the paint meets its own boundary: the fade and normal-bend ramps
                        // across the expanded edge band, the noise breaking that boundary up, and
                        // the normal strength at it. Without them a consumer draws a hard-edged
                        // polygon where the game draws a soft, ragged one.
                        //
                        entry["edgeNormal"] = Math.Round((double)rendered.m_EdgeNormal, 4);
                        entry["edgeFadeRange"] = Round2(rendered.m_EdgeFadeRange);
                        entry["edgeNormalRange"] = Round2(rendered.m_EdgeNormalRange);
                        entry["edgeNoise"] = Round2(rendered.m_EdgeNoise);

                        TextureAsset baseColorMap = null;
                        if (rendered.m_BaseColorMap != null) baseColorMap = rendered.m_BaseColorMap;

                        TextureAsset normalMap = null;
                        if (rendered.m_NormalMap != null) normalMap = rendered.m_NormalMap;

                        TextureAsset maskMap = null;
                        if (rendered.m_MaskMap != null) maskMap = rendered.m_MaskMap;

                        // `Initialize` runs for all three maps or for none, so an authored normal
                        // or mask map ships here on exactly the same gate as the base colour —
                        // and an unauthored one is a CLEARED slot, not a fallback, so writing no
                        // field for it is game-true.
                        //
                        if (normalMap != null)
                            WriteAreaTexture(MaterialRipSystem.ReadBack(normalMap, $"{key}_n", 1024), "normalMap", "_n");
                        if (maskMap != null)
                            WriteAreaTexture(MaterialRipSystem.ReadBack(maskMap, $"{key}_m", 1024), "maskMap", "_m");

                        if (baseColorMap != null)
                        {
                            WriteAreaTexture(MaterialRipSystem.ReadBack(baseColorMap, key, 1024), "texture", "");
                        }
                        else if ((material != null) && (normalMap == null) && (maskMap == null))
                        {
                            Texture texture = material.mainTexture;
                            if ((texture == null) && material.HasProperty("_BaseColorMap"))
                                texture = material.GetTexture("_BaseColorMap");

                            if (texture != null)
                                WriteAreaTexture(MaterialRipSystem.CaptureNow(texture, key, name, 1024), "texture", "");

                            // None of the three overrides authored, so the base material keeps its
                            // own maps too — the same inheritance the base colour above relies on.
                            //
                            if (material.HasProperty("_NormalMap"))
                            {
                                Texture map = material.GetTexture("_NormalMap");
                                if (map != null)
                                    WriteAreaTexture(MaterialRipSystem.CaptureNow(map, $"{key}_n", name, 1024), "normalMap", "_n");
                            }

                            if (material.HasProperty("_MaskMap"))
                            {
                                Texture map = material.GetTexture("_MaskMap");
                                if (map != null)
                                    WriteAreaTexture(MaterialRipSystem.CaptureNow(map, $"{key}_m", name, 1024), "maskMap", "_m");
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Area prefab `{name}` material did not rip: {ex.Message}");
            }

            return key;
        }

        /// <summary>
        /// One of the area shader's <c>float2</c> edge parameters, rounded for the catalogue.
        /// The game widens each to a <c>float4</c> with two zeroes on the way to the shader; the
        /// two authored components are the whole of it.
        /// </summary>
        /// <param name="value">The authored pair.</param>
        /// <returns>The rounded pair.</returns>
        private static double[] Round2(float2 value)
            => new[] { Math.Round((double)value.x, 4), Math.Round((double)value.y, 4) };
    }
}
