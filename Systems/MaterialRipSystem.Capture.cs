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
    /// One texture off the GPU and onto disk: the capture, the two-channel normal rebuild, and
    /// the mip policy.
    /// </summary>
    /// <remarks>
    /// CS2 normal maps arrive AG- or RG-packed; read as RGB they shade BLACK, so the rebuild is
    /// not an enhancement but the difference between a lit surface and a hole (§7.1).
    /// </remarks>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Where one map's pixels actually live, and the identity they should be deduped under.
        /// </summary>
        private readonly struct TextureSource
        {
            /// <summary>The dedup key, or <c>null</c> when there are no pixels to be had.</summary>
            public string Key { get; }

            /// <summary>The loadable standalone asset, when there is one.</summary>
            public TextureAsset Standalone { get; }

            /// <summary>The preprocessed texture holding the pixels, when the map is virtual-textured.</summary>
            public VTTextureAsset Packed { get; }

            /// <summary>A texture already bound to a live material.</summary>
            public Texture Live { get; }

            /// <summary>The prefab whose materials must be released once <see cref="Live"/> is read.</summary>
            public RenderPrefab Owner { get; }

            /// <summary>Whether the pixels come out of a VT stack.</summary>
            public bool IsVirtual => Packed != null;

            private TextureSource(string key, TextureAsset standalone, VTTextureAsset packed, Texture live, RenderPrefab owner)
            {
                Key = key;
                Standalone = standalone;
                Packed = packed;
                Live = live;
                Owner = owner;
            }

            /// <summary>A map with its own texture asset.</summary>
            public static TextureSource FromAsset(string key, TextureAsset asset) => new(key, asset, null, null, null);

            /// <summary>A map packed into a virtual-texturing stack.</summary>
            public static TextureSource FromStack(string key, VTTextureAsset packed) => new(key, null, packed, null, null);

            /// <summary>A map read off a live material.</summary>
            public static TextureSource FromLive(string key, Texture live, RenderPrefab owner) => new(key, null, null, live, owner);

            /// <summary>No pixels anywhere.</summary>
            public static TextureSource None => default;
        }



        /// <summary>
        /// Copy a texture that is already bound to a live material.
        /// The texture belongs to the material, not to this rip, so it is read where it sits and
        /// only the prefab's hold on its materials is released afterwards.
        /// Must run on the main thread.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="source">The located texture.</param>
        /// <param name="surfaceName">A name for the record.</param>
        /// <returns>The pixels, or <c>null</c>.</returns>
        private static PendingTexture ReadBackLive(MaterialRun run, TextureSource source, string surfaceName)
        {
            try
            {
                return Capture(source.Live, source.Key, surfaceName, run.TextureSizeCap);
            }
            finally
            {
                try { source.Owner?.ReleaseMaterials(); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Record a texture that produced no file.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="key">The texture key.</param>
        /// <param name="reason">Why it produced nothing.</param>
        private static void Skip(MaterialRun run, string key, string reason)
        {
            if (run.Declared.Add(key)) run.Skipped.Add(new RipWarning("texture", key, reason));
        }

        /// <summary>
        /// Scale a loaded texture to the cap and copy its pixels out as <c>RGBA8</c>.
        /// </summary>
        /// <param name="source">The loaded texture.</param>
        /// <param name="key">Its texture key.</param>
        /// <param name="name">A name for the record.</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.</param>
        /// <returns>The pixels, or <c>null</c>.</returns>
        internal static PendingTexture Capture(Texture source, string key, string name, int cap)
        {
            if (source == null) return null;

            int width = source.width, height = source.height;
            if ((width <= 0) || (height <= 0)) return null;

            if (cap > 0)
            {
                // Scale the long edge to the cap and the short edge with it, so a non-square
                // texture keeps its aspect ratio and its UVs stay valid.
                //
                int longest = Mathf.Max(width, height);
                if (longest > cap)
                {
                    width = Mathf.Max(1, (width * cap) / longest);
                    height = Mathf.Max(1, (height * cap) / longest);
                }
            }

            // Take the colour space from the loaded texture, never from the asset that produced it.
            // A VTTextureAsset's own `format` field is not populated by reading its header — only
            // by paths that upload through the streaming system — so it reports whatever it last
            // held. Trusting it wrote every normal and mask map through an sRGB render target,
            // gamma-mangling linear data, and the output still rendered: exactly the silent failure
            // §7.1 of the contract warns about. The Texture that came back cannot be stale.
            //
            byte[] pixels = Blit(source, width, height, GraphicsFormatUtility.IsSRGBFormat(source.graphicsFormat));
            if (pixels == null) return null;

            return new PendingTexture
            {
                Key = key,
                SourceName = name,
                Width = width,
                Height = height,
                Pixels = pixels
            };
        }

        /// <summary>
        /// Rebuild a two-channel-packed normal map into a standard tangent normal.
        /// A two-channel normal ships X and Y and lets the shader reconstruct Z, and CS2's art
        /// arrives in both of the layouts Unity's own <c>UnpackNormalmapRGorAG</c> exists to
        /// straddle: <b>AG</b> (DXT5nm/BC3n — X in alpha, Y in green, red forced to 1 and blue
        /// left mirroring green) and <b>RG</b> (BC5 — X in red, Y in green, blue left at 0 and
        /// alpha at 1). Written out raw, a consumer reading either as an ordinary tangent normal
        /// map gets a nonsense normal: AG gives <c>normal.x = +1</c> for every texel — measured
        /// on a full city, every road surface shaded by its compass bearing, which is what "each
        /// road is a different colour" turned out to be — and RG gives <c>normal.z = -1</c>, a
        /// normal pointing into the surface, which shades black.
        /// Each layout has a signature that matches no legitimate colour or mask content: red
        /// pinned at white while alpha varies, or blue pinned at black while red and green both
        /// vary. They are detected here, where every readback route converges, and rebuilt to
        /// <c>(x, y, z, 1)</c> exactly as the shader would. Contract §7.1 promises consumers one
        /// convention for every normal map in the bundle, and this is the only place that keeps
        /// that promise.
        /// </summary>
        /// <param name="pixels">RGBA8 pixels, rebuilt in place.</param>
        internal static void ReconstructTwoChannelNormal(byte[] pixels)
        {
            // The fraction test rather than a strict minimum: block compression dents a few
            // texels of the constant channel (red at 247–249, and one map to 191), and a strict
            // minimum was measured to miss 235 of 784 packed normals in a full city. A standard
            // three-channel normal has red centred at 128 and blue up near white, so it passes
            // nowhere near 90 % on either test.
            //
            int count = pixels.Length / 4;
            if (count == 0) return;

            int highRed = 0, lowBlue = 0, lowRed = 0, minA = 255;
            for (int i = 0; i < pixels.Length; i += 4)
            {
                if (pixels[i] >= 250) highRed++;
                if (pixels[i] <= 5) lowRed++;
                if (pixels[i + 2] <= 5) lowBlue++;
                if (pixels[i + 3] < minA) minA = pixels[i + 3];
            }

            float threshold = count * 0.9f;

            // AG needs alpha to be carrying something — a map whose alpha is constant has no X
            // in it — while RG needs blue to be the pinned channel instead. The two are mutually
            // exclusive by construction, so the order of these tests never decides anything.
            //
            // Each test needs a POSITIVE signature as well as a pinned channel, and RG's was
            // missing one until 2026-08-14: `lowBlue >= threshold && highRed < threshold` says
            // only "blue is dark and red is not white", which a black or near-black placeholder
            // normal satisfies everywhere. Such a map was then "rebuilt" from x = y = -1 into a
            // uniform sideways normal — the very failure this method exists to repair, inflicted
            // on a map that never needed it. RG carries X in RED, so red must be carrying
            // something too: `lowRed < threshold` is the mirror of AG's `minA < 250`.
            //
            bool alphaPacked = (highRed >= threshold) && (minA < 250);
            bool redPacked = (lowBlue >= threshold) && (highRed < threshold) && (lowRed < threshold);
            if (!alphaPacked && !redPacked) return;

            for (int i = 0; i < pixels.Length; i += 4)
            {
                float x = ((alphaPacked ? pixels[i + 3] : pixels[i]) / 255f * 2f) - 1f;
                float y = (pixels[i + 1] / 255f * 2f) - 1f;
                float z = Mathf.Sqrt(Mathf.Max(0f, 1f - (x * x) - (y * y)));

                pixels[i] = (byte)Mathf.RoundToInt(((x * 0.5f) + 0.5f) * 255f);
                pixels[i + 2] = (byte)Mathf.RoundToInt(((z * 0.5f) + 0.5f) * 255f);
                pixels[i + 3] = 255;
            }
        }

        /// <summary>
        /// Blit a texture through a render target and read the result back as <c>RGBA8</c>.
        /// Rows come back bottom-up, which is Unity's convention and the opposite of PNG's, so they
        /// are flipped here. The glTF writer separately flips <c>V</c> on the mesh side; both are
        /// the same change of origin and the two must agree or every texture lands upside down.
        /// </summary>
        /// <param name="source">The loaded texture.</param>
        /// <param name="width">The output width.</param>
        /// <param name="height">The output height.</param>
        /// <param name="srgb">Whether the source stores sRGB-encoded values.</param>
        /// <returns>Row-major <c>RGBA8</c> pixels, top row first.</returns>
        private static byte[] Blit(Texture source, int width, int height, bool srgb)
        {
            RenderTexture target = RenderTexture.GetTemporary(width, height, 0, RenderTextureFormat.ARGB32,
                srgb ? RenderTextureReadWrite.sRGB : RenderTextureReadWrite.Linear);
            RenderTexture previous = RenderTexture.active;
            Texture2D readback = null;

            try
            {
                Graphics.Blit(source, target);
                RenderTexture.active = target;

                readback = new Texture2D(width, height, TextureFormat.RGBA32, false, linear: !srgb);
                readback.ReadPixels(new Rect(0f, 0f, width, height), 0, 0, false);
                readback.Apply(false, false);

                byte[] raw = readback.GetRawTextureData();
                int stride = width * 4;
                if ((raw == null) || (raw.Length < stride * height)) return null;

                byte[] pixels = new byte[stride * height];
                for (int y = 0; y < height; y++)
                    Buffer.BlockCopy(raw, (height - 1 - y) * stride, pixels, y * stride, stride);

                return pixels;
            }
            finally
            {
                RenderTexture.active = previous;
                RenderTexture.ReleaseTemporary(target);
                if (readback != null) UnityEngine.Object.Destroy(readback);
            }
        }

        /// <summary>
        /// The smallest mip level whose longest edge still meets the cap.
        /// </summary>
        /// <param name="width">The full-size width.</param>
        /// <param name="height">The full-size height.</param>
        /// <param name="mips">How many mip levels the asset has.</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.</param>
        /// <returns>The mip bias to load at.</returns>
        private static int MipBias(int width, int height, int mips, int cap)
        {
            if ((cap <= 0) || (mips <= 1)) return 0;

            int bias = 0, longest = Mathf.Max(width, height);
            while ((longest > cap) && (bias < mips - 1))
            {
                longest /= 2;
                bias++;
            }

            return bias;
        }

        /// <summary>
        /// Read the transparency mode out of the surface's shader keywords, falling back to the
        /// material template the surface is built from.
        /// The surface's own keyword set is only half the story: the runtime material is
        /// <c>new Material(template)</c> with the surface's properties overlaid
        /// (<c>SurfaceAsset.Load</c>), so a template that alpha-tests passes clipping to every
        /// surface built from it. Vegetation is the canonical case — the oak leaf surface
        /// serialises no keywords at all; its cutout lives entirely on the template.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <returns>The contract's alpha mode.</returns>
        private static string AlphaMode(SurfaceAsset surface)
        {
            // HDRP serialises the switches as floats and `HDMaterial.ValidateMaterial` — which
            // `SurfaceAsset.Load` ends on — re-derives the keywords from them, so a float the
            // surface carries overrides the template outright, in either direction.
            //
            bool surfaceAnswersType = false, surfaceAnswersClip = false;

            try
            {
                if (surface.HasKeyword("_SURFACE_TYPE_TRANSPARENT")) return "blend";
                if (surface.HasKeyword("_ALPHATEST_ON")) return "mask";

                if (surface.TryGetFloatProperty("_SurfaceType", out float surfaceType))
                {
                    surfaceAnswersType = true;
                    if (surfaceType > 0.5f) return "blend";
                }
                if (surface.TryGetFloatProperty("_AlphaCutoffEnable", out float clipEnable))
                {
                    surfaceAnswersClip = true;
                    if (clipEnable > 0.5f) return "mask";
                }
            }
            catch (Exception)
            {
                // A surface whose keywords did not load says nothing about transparency; the
                // template below still can.
            }

            try
            {
                UnityEngine.Material template = surface.GetTemplateMaterial();
                if (template != null)
                {
                    if (!surfaceAnswersType
                        && (template.IsKeywordEnabled("_SURFACE_TYPE_TRANSPARENT")
                            || (template.HasProperty("_SurfaceType") && (template.GetFloat("_SurfaceType") > 0.5f))))
                        return "blend";

                    if (!surfaceAnswersClip
                        && (template.IsKeywordEnabled("_ALPHATEST_ON")
                            || (template.HasProperty("_AlphaCutoffEnable") && (template.GetFloat("_AlphaCutoffEnable") > 0.5f))))
                        return "mask";
                }
            }
            catch (Exception)
            {
                // No template resolves; opaque is the contract's default.
                //
            }

            return "opaque";
        }
    }
}
