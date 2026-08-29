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
    /// Finding the actual pixels behind a CS2 surface property.
    /// </summary>
    /// <remarks>
    /// Five routes, tried in order, because no one of them covers the asset set: the material's
    /// own property, the surface's stack, the pre-processed GUID, the per-surface
    /// <c>.VTSurface</c> data, and the virtual-texture read-back. The buildings are on route
    /// five, which is why it exists.
    /// </remarks>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Work out where a map's pixels are, trying each route the game itself uses.
        /// In order: the asset the surface handed over, if it is loadable; the same guid looked up
        /// in the global database, in case the reference was merely unbound; and failing both, the
        /// preprocessed virtual-texturing stack the map was packed into.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="texture">The reference the surface handed over.</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.</returns>
        private static TextureSource Locate(MaterialRun run, SurfaceAsset surface, string property, TextureAsset texture,
                                            RenderPrefab renderPrefab, int materialIndex, SurfaceData own)
        {
            string key = Identity(texture);

            // Locating happens per map slot, but the buckets describe textures. Count each
            // reference the first time it is seen and no more.
            //
            bool first = run.Counted.Add(key);

            if (!IsUnbound(texture))
            {
                if (first) run.TexturesBound++;
                return TextureSource.FromAsset(key, texture);
            }

            // Unbound, but the guid survives. If the standalone texture did ship, this finds it.
            //
            try
            {
                Colossal.Hash128 guid = texture.id.guid;
                if (guid.isValid
                    && AssetDatabase.global.TryGetAsset(guid, out TextureAsset bound)
                    && (bound != null) && !IsUnbound(bound))
                {
                    if (first) run.TexturesRebound++;
                    return TextureSource.FromAsset(key, bound);
                }
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Texture guid lookup failed for `{key}`: {ex.Message}");
            }

            string reason;
            if (IsVirtualTextured(surface, property, texture))
            {
                if (first) run.TexturesVirtual++;

                TextureSource packed = LocateInStack(run, surface, property, key, out reason);
                if (packed.Key != null) return packed;
            }
            else
            {
                if (first) run.TexturesUnbound++;
                reason = $"unbound texture reference ({property}): no asset in the database for this guid";
            }

            // The surface's own virtual-texturing data, which is the other half of the scheme and
            // where the buildings are. Tried after the shared stack because the stack's pixels are
            // shared between surfaces and so dedupe far better, and before the live material
            // because this is the actual source rather than a guess at one.
            //
            TextureSource inner = LocateInSurfaceData(run, own, property, ref reason);
            if (inner.Key != null) return inner;

            // Last resort: ask the render prefab for the material the game would actually draw
            // with. A surface whose stub does not resolve and whose VT header is empty has still
            // been rendering all along, so if anything is bound to this property at all, this is
            // where it shows up. It is deliberately last — it allocates a Unity material per
            // surface, and every route above is cheaper and more direct.
            //
            TextureSource live = LocateInMaterial(run, renderPrefab, materialIndex, property, key);
            if (live.Key != null) return live;

            Skip(run, key, reason);
            return TextureSource.None;
        }

        /// <summary>
        /// Read a map straight off the Unity material the render prefab would draw with.
        /// This asks the game the question at its own level — "what texture is bound to this
        /// property?" — instead of reconstructing the answer from asset records. It is the only
        /// route left for surfaces that have neither a loadable texture asset nor preprocessed
        /// virtual-texturing data, and it costs a material instantiation, so it runs only after
        /// everything else has failed.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="renderPrefab">The render prefab owning the material.</param>
        /// <param name="materialIndex">The material slot.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="key">The texture key.</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.</returns>
        private static TextureSource LocateInMaterial(MaterialRun run, RenderPrefab renderPrefab, int materialIndex,
                                                      string property, string key)
        {
            if (renderPrefab == null) return TextureSource.None;

            try
            {
                Material material = renderPrefab.ObtainMaterial(materialIndex, false);
                if ((material == null) || !material.HasProperty(property)) return TextureSource.None;

                Texture bound = material.GetTexture(property);
                if (bound == null) return TextureSource.None;

                run.TexturesFromMaterial++;
                return TextureSource.FromLive(key, bound, renderPrefab);
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Material lookup failed for `{key}` ({property}): {ex.Message}");
                return TextureSource.None;
            }
        }

        /// <summary>
        /// Find the preprocessed texture a virtual-textured map was packed into.
        /// The material template says where: <c>GetStackConfigIndex</c> and
        /// <c>GetLayerIndexInStack</c> turn a shader property name into the stack and layer its map
        /// occupies, and the surface's VT header holds a guid per slot.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="key">The stub's key, for reporting.</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.</returns>
        private static TextureSource LocateInStack(MaterialRun run, SurfaceAsset surface, string property, string key, out string reason)
        {
            try
            {
                // Qualified through MaterialLibrary: the bundle's own MaterialDescription is a
                // different type with the same short name, and Carto.Domain is in scope here.
                //
                MaterialLibrary.MaterialDescription description = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash);

                if (description == null)
                {
                    reason = $"virtual-textured ({property}): no material description for template {surface.materialTemplateHash}";
                    return TextureSource.None;
                }

                int stack = description.GetStackConfigIndex(property);
                int layer = (stack < 0) ? -1 : description.GetLayerIndexInStack(stack, property);
                if ((stack < 0) || (layer < 0))
                {
                    reason = $"virtual-textured ({property}): the material template packs no such map";
                    return TextureSource.None;
                }

                Colossal.Hash128 guid = PreProcessedGuid(surface, stack, layer);
                if (!guid.isValid)
                {
                    // These coordinates are not wrong, and neither is the empty header: this
                    // surface simply keeps its pixels in the other place. CS2 has two
                    // virtual-texturing schemes, and they are very nearly complementary — of the
                    // 5,832 surfaces the game ships, 2,014 use the shared preprocessed stack read
                    // above and 2,233 use a `.VTSurface` file of their own with a zeroed stack
                    // header. The second set is mostly buildings. `LocateInSurfaceData` reads it,
                    // so falling through here is a route change rather than a dead end.
                    //
                    reason = $"virtual-textured ({property}): no preprocessed VT data, and the "
                           + "surface's own VT data could not be read either";
                    return TextureSource.None;
                }

                if (!AssetDatabase.global.TryGetAsset(guid, out VTTextureAsset packed) || (packed == null))
                {
                    reason = $"virtual-textured ({property}): preprocessed texture {guid} is not in the database";
                    return TextureSource.None;
                }

                // Keyed by the preprocessed texture, so surfaces sharing a packed layer share the
                // readback rather than each paying for it.
                //
                reason = null;
                return TextureSource.FromStack($"vt:{guid}", packed);
            }
            catch (Exception ex)
            {
                reason = $"virtual-textured ({property}): {ex.GetType().Name} {ex.Message}";
                if (run.BindFailures++ < 5) _log.Warn($"VT stack lookup failed for `{key}` ({property}): {ex}");
                return TextureSource.None;
            }
        }

        /// <summary>
        /// The guid of the preprocessed texture at one stack and layer of a surface's VT header.
        /// <b>Do not replace this with <c>SurfaceAsset.GetPreProcessedTextureGuid(stack, layer)</c>,
        /// which is the obvious call and is wrong.</b> That method bounds-checks both arguments and
        /// then indexes the header with <c>stackIndex * layerIndex</c> where the flat index is
        /// <c>stackIndex * 4 + layerIndex</c>. Every coordinate with a zero in it — which is every
        /// coordinate a one-stack surface can produce, and layer 0 of any stack — therefore returns
        /// slot 0. Since stack 0 layer 0 is the base colour, asking for a surface's normal or mask
        /// hands back its base colour instead, and the caller cannot tell: the guid is valid, the
        /// asset loads, and the pixels are a real texture. A full-city rip wrote the base colour
        /// into the normal and mask of 1,428 of 1,926 materials this way, and the only visible
        /// symptom was a suspiciously high dedupe rate.
        /// The enumerator does not share the fault — it walks <c>stackCount * 4</c> slots by flat
        /// index, in order and unfiltered — so counting into it gives the value the accessor should
        /// have returned. At most eight slots, so the walk is not worth avoiding.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <param name="stack">The stack index.</param>
        /// <param name="layer">The layer index within that stack.</param>
        /// <returns>The guid, or an invalid one when the slot is past the end.</returns>
        private static Colossal.Hash128 PreProcessedGuid(SurfaceAsset surface, int stack, int layer)
        {
            int target = (stack * 4) + layer;
            int index = 0;

            foreach (Colossal.Hash128 guid in surface.preProcessedTextureGuids)
                if (index++ == target) return guid;

            return default;
        }

        /// <summary>
        /// Read a virtual-textured map out of the surface's own preprocessed data.
        /// A <see cref="SurfaceAsset"/> can hold its virtual-texturing data in either of two
        /// places. <see cref="LocateInStack"/> covers the first: layers packed into
        /// <see cref="VTTextureAsset"/>s shared between every surface that uses them. The second is
        /// a <c>.VTSurface</c> file belonging to this surface alone, resolved when the surface's
        /// header is read and reached through <c>hasVTSurfaceAsset</c> / <c>vtSurfaceAsset</c>. A
        /// surface using the second has a zeroed stack header, which is why it looks empty-handed
        /// from the first route's point of view.
        /// <c>VTSurfaceAsset.Load</c> does the whole job: it reassembles the file's zstd tiles,
        /// instantiates the material template the surface names, and binds each decoded layer under
        /// the shader property the template gives it. What comes back is an ordinary Unity material
        /// with ordinary textures on it, so this route ends where every other one does.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="own">The surface's own VT data, opened on demand.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="reason">Replaced only when this route is the one that failed.</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.</returns>
        private static TextureSource LocateInSurfaceData(MaterialRun run, SurfaceData own, string property, ref string reason)
        {
            if ((own?.Surface == null) || !own.Surface.hasVTSurfaceAsset) return TextureSource.None;

            Material material = Obtain(run, own);
            if (material == null)
            {
                reason = $"virtual-textured ({property}): the surface's own VT data could not be decoded";
                return TextureSource.None;
            }

            if (!material.HasProperty(property)) return TextureSource.None;

            Texture bound = material.GetTexture(property);
            if (bound == null) return TextureSource.None;

            run.TexturesFromSurfaceData++;

            // Keyed by the VT data's own guid, which is the opposite of the rule the shared stack
            // follows: these pixels belong to one surface and are not shared with any other, so
            // keying them any more coarsely would make two surfaces collide on one image.
            //
            string key = $"vts:{own.Surface.vtSurfaceAsset.id.guid}:{property}";

            if (run.SurfaceDataReports++ < 4)
                _log.Info($"VT surface recovery [{run.SurfaceDataReports}] `{own.Surface.name}` {property}: "
                        + $"{bound.width}x{bound.height} {bound.graphicsFormat}");

            return TextureSource.FromLive(key, bound, null);
        }
    }
}
