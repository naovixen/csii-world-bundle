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
    /// Getting a live <c>Material</c>, reading its texture back off the GPU, and letting go.
    /// </summary>
    /// <remarks>
    /// A read-back is a main-thread stall and a GPU allocation, so both ends matter: obtain
    /// exactly what is needed, and release it whether the read succeeded or threw.
    /// </remarks>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Open a surface's own virtual-texturing data, at most once per surface.
        /// The attempt is recorded rather than the result, so a surface whose data will not decode
        /// is not asked again for each of its remaining maps.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="own">The surface's own VT data.</param>
        /// <returns>The decoded material, or <c>null</c>.</returns>
        private static Material Obtain(MaterialRun run, SurfaceData own)
        {
            if (own.Attempted) return own.Material;
            own.Attempted = true;

            // The decode is a native-crash risk (see LoadCrashGuard), so the fence brackets it:
            // skip surfaces a previous crash convicted, and name the one in flight on disk so
            // that a crash here convicts it for the next run.
            //
            string marker = $"{own.Surface.name} ({own.Surface.vtSurfaceAsset?.id.guid})";
            if (run.CrashGuard.Contains(marker))
            {
                run.SurfaceDataGuarded++;
                run.Skipped.Add(new RipWarning("texture", own.Surface.name,
                    "own VT data not decoded: it crashed the game in a previous rip (vt_crash_guard.txt); "
                    + "maps fall back to the live material where possible"));
                return null;
            }

            try
            {
                run.SurfaceDataLoads++;
                try { File.WriteAllText(InflightPath, marker); } catch { /* fence unavailable; proceed */ }
                own.Material = own.Surface.vtSurfaceAsset.Load();
            }
            catch (Exception ex)
            {
                run.SurfaceDataFailures++;
                if (run.BindFailures++ < 5)
                    _log.Warn($"VT surface data failed to load for `{own.Surface.name}`: {ex}");
            }
            finally
            {
                // Reached on success and on managed failure alike; only a native crash leaves the
                // marker behind, which is precisely the signal it exists to give.
                //
                try { File.Delete(InflightPath); } catch { /* nothing useful to do */ }
            }

            return own.Material;
        }

        /// <summary>
        /// Close a surface's own virtual-texturing data and destroy what decoding it created.
        /// <c>VTSurfaceAsset.Unload</c> destroys the material and nothing else, but the decode also
        /// built a texture per layer, each held only by that material and marked so that Unity will
        /// not collect it. Left alone they accumulate for the whole rip: a city's worth of building
        /// surfaces at up to eight layers apiece, several megabytes each, is measured in gigabytes
        /// and ends the run long before the city does. So the textures are taken off the material
        /// and destroyed here, after their pixels have been read.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="own">The surface's own VT data.</param>
        private static void Release(MaterialRun run, SurfaceData own)
        {
            if ((own == null) || !own.Attempted) return;

            Material material = own.Material;
            own.Material = null;

            try
            {
                if (material != null) foreach (string property in DecodedProperties(own.Surface))
                {
                    if (!material.HasProperty(property)) continue;

                    Texture texture = material.GetTexture(property);
                    if (texture == null) continue;

                    material.SetTexture(property, null);
                    UnityEngine.Object.DestroyImmediate(texture, true);
                }
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Releasing VT surface data for `{own.Surface?.name}` failed: {ex}");
            }
            finally
            {
                try { own.Surface?.vtSurfaceAsset?.Unload(false); } catch { /* nothing useful to do */ }
            }
        }


        /// <summary>
        /// Copy a texture asset's pixels out of the GPU as <c>RGBA8</c>.
        /// Shipped CS2 art is block-compressed (BC5, BC7 and friends), and there is no CPU decoder
        /// for those formats anywhere in the game's libraries — the asset database's own
        /// <c>ToImageData</c> handles uncompressed surfaces only. Letting the GPU do the decoding is
        /// therefore not an optimisation but the only path: blit through a render target of the
        /// right colour space and read the result back.
        /// The render target's colour space matches the source's, so the bytes pass through
        /// unchanged: an sRGB base colour stays sRGB and a linear normal or mask map is not
        /// gamma-mangled on the way out. That is what §7.1 of the contract promises.
        /// Must run on the main thread.
        /// </summary>
        /// <param name="asset">The texture asset.</param>
        /// <param name="key">Its stable identity.</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.</param>
        /// <returns>The pixels, or <c>null</c> when nothing came back.</returns>
        internal static PendingTexture ReadBack(TextureAsset asset, string key, int cap)
        {
            // Loading a smaller mip is cheaper than loading the full image and shrinking it, so ask
            // for the smallest one that still satisfies the cap and let the blit do the remainder.
            //
            int mipBias = MipBias(asset.width, asset.height, asset.mipsCount, cap);

            Texture source = null;
            try
            {
                source = asset.Load(mipBias);
                PendingTexture pending = Capture(source, key, asset.name, cap);
                if (DeferRelease(() => asset.Unload(false))) source = null;
                return pending;
            }
            finally
            {
                // The texture was loaded for this one readback. Holding every texture a city uses
                // would defeat the point of streaming them in the first place.
                //
                if (source != null) { try { asset.Unload(false); } catch { /* nothing useful to do */ } }
            }
        }

        /// <summary>
        /// Read a virtual-textured map out of the preprocessed stack that replaced it.
        /// Most CS2 surfaces have no standalone texture assets: their maps were preprocessed into
        /// virtual-texturing stacks at build time, and what remains in
        /// <c>SurfaceAsset.textures</c> is an unloadable stub. The pixels are still shipped, just
        /// addressed differently — by stack and layer rather than by asset — and the route to them
        /// is entirely public:
        /// <c>MaterialDescription.GetStackConfigIndex(property)</c> and
        /// <c>GetLayerIndexInStack(stack, property)</c> turn a shader property name into the
        /// coordinates its map was packed at; <c>SurfaceAsset.GetPreProcessedTextureGuid</c> turns
        /// those into a guid; and the <see cref="VTTextureAsset"/> behind it reassembles its own
        /// tiles into an ordinary <c>Texture</c> when asked. From there it is the same blit and
        /// readback as any other texture.
        /// Must run on the main thread.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface the map belongs to.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="key">The texture key.</param>
        /// <returns>The pixels, or <c>null</c> when the stack could not be read.</returns>
        private static PendingTexture ReadBackVirtual(MaterialRun run, TextureSource source, string surfaceName, string property)
        {
            VTTextureAsset packed = source.Packed;
            bool deferred = false;
            try
            {
                Texture texture = packed.Load();
                PendingTexture pending = Capture(texture, source.Key, surfaceName, run.TextureSizeCap);
                deferred = DeferRelease(() => packed.Unload(false));
                if (pending == null)
                {
                    Skip(run, source.Key, $"virtual-textured ({property}): preprocessed texture yielded no pixels");
                    return null;
                }

                run.TexturesFromStack++;

                // The reassembled texture ought to come out at the size its own header declares.
                // A mismatch would mean the tile layout is leaking through — the one thing about
                // this path that cannot be established without the game running — so say so on the
                // first few rather than let it be discovered as odd-looking output.
                //
                if (run.StackReports++ < 4)
                    _log.Info($"VT recovery [{run.StackReports}] `{surfaceName}` {property}: "
                            + $"header {packed.width}x{packed.height}, loaded {texture.width}x{texture.height} "
                            + $"{texture.graphicsFormat}, written {pending.Width}x{pending.Height}");

                return pending;
            }
            finally
            {
                if (!deferred) { try { packed.Unload(false); } catch { /* nothing useful to do */ } }
            }
        }
    }
}
