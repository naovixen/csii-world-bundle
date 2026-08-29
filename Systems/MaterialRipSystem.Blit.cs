using Carto.IO;
using System;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// Getting a texture's pixels off the GPU: the blit into a render target, the asynchronous
    /// request for its bytes, and the synchronous form the terrain stages use.
    /// </summary>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// One texture's readback in flight: the render target it was blitted into, the GPU
        /// request that will deliver its bytes, and the promise the encoder waits on.
        /// </summary>
        internal sealed class GpuReadback
        {
            public AsyncGPUReadbackRequest Request;
            public RenderTexture Target;

            /// <summary>
            /// Lets go of the SOURCE texture once the readback has landed — the blit that read it
            /// has executed by then. The three read paths used to unload their source the moment
            /// the blit was ISSUED, which <c>ReadPixels</c> tolerated because it forced the GPU
            /// to finish first; an asynchronous request does not.
            /// </summary>
            public Action Release;
            public int Width, Height;
            public readonly TaskCompletionSource<byte[]> Pixels = new(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        /// <summary>
        /// Blit a texture through a render target and ask the GPU for the result as <c>RGBA8</c> —
        /// asynchronously. The request is completed by <see cref="Pump"/> on a later frame; until
        /// then the render target stays alive for it.
        /// </summary>
        /// <remarks>
        /// This was <c>Texture2D.ReadPixels</c>: a synchronous pipeline flush per texture, which
        /// held the main thread for 411.8 s of the 2026-08-29 rip's 1,305 s mesh stage — with a
        /// <c>Texture2D</c> allocated and destroyed for every one of 4,102 readbacks. The same
        /// request the fine-terrain cascade already uses (<c>TerrainRipSystem.FineCascade</c>) lets
        /// the GPU deliver the bytes while the main thread reads the next prefab.
        /// </remarks>
        /// <param name="source">The loaded texture.</param>
        /// <param name="width">The output width.</param>
        /// <param name="height">The output height.</param>
        /// <param name="srgb">Whether the source stores sRGB-encoded values.</param>
        /// <returns>The readback in flight.</returns>
        private static GpuReadback Blit(Texture source, int width, int height, bool srgb)
        {
            RenderTexture target = RenderTexture.GetTemporary(width, height, 0, RenderTextureFormat.ARGB32,
                srgb ? RenderTextureReadWrite.sRGB : RenderTextureReadWrite.Linear);
            Graphics.Blit(source, target);
            return new GpuReadback
            {
                Target = target,
                Width = width,
                Height = height,
                Request = AsyncGPUReadback.Request(target, 0, TextureFormat.RGBA32),
            };
        }

        /// <summary>
        /// Rows come back bottom-up, which is Unity's convention and the opposite of PNG's, so
        /// they are flipped here. The glTF writer separately flips <c>V</c> on the mesh side; both
        /// are the same change of origin and the two must agree or every texture lands upside
        /// down.
        /// </summary>
        private static byte[] FlipRows(byte[] raw, int width, int height)
        {
            int stride = width * 4;
            if ((raw == null) || (raw.Length < stride * height)) return null;

            byte[] pixels = new byte[stride * height];
            for (int y = 0; y < height; y++)
                Buffer.BlockCopy(raw, (height - 1 - y) * stride, pixels, y * stride, stride);

            return pixels;
        }

        /// <summary>
        /// The readback the last <see cref="Capture"/> on this thread issued, for <c>Take</c> to
        /// register on the run — <c>Capture</c> is static and run-free, and the pending texture
        /// deliberately carries no Unity type.
        /// </summary>
        private static readonly System.Threading.ThreadLocal<GpuReadback> _issued = new();

        /// <summary>
        /// <see cref="Capture"/> with the pixels landed before it returns — for the synchronous
        /// stages (terrain surfaces, area textures, the splatmap) that encode on the spot and
        /// never pump. The main-thread stall this keeps is theirs alone, a hundred textures
        /// against the mesh stage's four thousand.
        /// </summary>
        /// <summary>
        /// Hand the source's release to the readback the last <see cref="Capture"/> issued, so it
        /// runs when the readback lands. False — release now — when no readback is in flight.
        /// </summary>
        private static bool DeferRelease(Action release)
        {
            GpuReadback issued = _issued.Value;
            if (issued == null) return false;
            issued.Release = release;
            return true;
        }

        internal static PendingTexture CaptureNow(Texture source, string key, string name, int cap)
        {
            PendingTexture pending = Capture(source, key, name, cap);
            GpuReadback issued = _issued.Value;
            _issued.Value = null;
            if ((pending == null) || (issued == null)) return pending;
            issued.Request.WaitForCompletion();
            Land(issued);
            pending.Pixels = issued.Pixels.Task.Result;
            pending.PixelsTask = null;
            return pending.Pixels == null ? null : pending;
        }
    }
}
