using System;
using System.Collections.Generic;
using System.Diagnostics;
using UnityEngine;
using UnityEngine.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// Landing the GPU readbacks the captures issued: the main-thread half of an asynchronous
    /// texture read.
    /// </summary>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Land every readback that has completed since the last call, and release its render
        /// target. Called once per frame by the mesh walk, and with <paramref name="force"/> when
        /// the walk is blocked on a full queue — a writer waiting for pixels that only the main
        /// thread can deliver must never be waited on by that main thread.
        /// </summary>
        /// <param name="force">Wait for every request in flight rather than only the finished ones.</param>
        public void Pump(bool force = false)
        {
            MaterialRun run = _run;
            if ((run == null) || (run.Readbacks.Count == 0)) return;

            Stopwatch clock = Stopwatch.StartNew();
            List<GpuReadback> still = null;
            foreach (GpuReadback readback in run.Readbacks)
            {
                if (force && !readback.Request.done) readback.Request.WaitForCompletion();
                if (!readback.Request.done)
                {
                    (still ??= new List<GpuReadback>()).Add(readback);
                    continue;
                }
                Land(readback);
            }
            run.Readbacks = still ?? new List<GpuReadback>();
            run.ReadbackMilliseconds += clock.Elapsed.TotalMilliseconds;
        }

        /// <summary>
        /// Copy one finished readback's bytes out, flip them into PNG's row order, and release
        /// the render target; a failed request delivers <c>null</c> and the encoder records it.
        /// </summary>
        private static void Land(GpuReadback readback)
        {
            byte[] pixels = null;
            try
            {
                if (!readback.Request.hasError)
                {
                    pixels = FlipRows(readback.Request.GetData<byte>().ToArray(), readback.Width, readback.Height);
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Texture readback failed to land: {ex.Message}");
            }
            finally
            {
                if (readback.Target != null) RenderTexture.ReleaseTemporary(readback.Target);
                readback.Target = null;
                readback.Pixels.TrySetResult(pixels);
            }
        }

        /// <summary>
        /// Fail every readback still in flight, so an encoder waiting on one is released rather
        /// than left waiting for a frame that will not come. Part of abandoning a run.
        /// </summary>
        private static void CancelReadbacks(MaterialRun run)
        {
            if (run == null) return;
            foreach (GpuReadback readback in run.Readbacks)
            {
                if (readback.Target != null) RenderTexture.ReleaseTemporary(readback.Target);
                readback.Target = null;
                readback.Pixels.TrySetResult(null);
            }
            run.Readbacks = new List<GpuReadback>();
        }
    }
}
