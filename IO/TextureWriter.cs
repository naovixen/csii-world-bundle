using System.Threading.Tasks;
using System.Linq;
using Carto.Domain;
using Colossal.Logging;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace Carto.IO
{
    /// <summary>
    /// One texture read back from the game and waiting to be encoded.
    /// Carries raw <c>RGBA8</c> pixels rather than a Unity <c>Texture</c>: reading the GPU has to
    /// happen on the main thread, but everything after it — compressing, hashing, writing — must
    /// not, and a plain byte array is the only form of the image that crosses that line safely.
    /// </summary>
    public class PendingTexture
    {
        /// <summary>
        /// The stable identity of the source texture asset. Materials refer to a texture by this
        /// key until the content hash — and therefore the file name — is known.
        /// </summary>
        public string Key { get; set; }

        /// <summary>The texture asset's name, for logging.</summary>
        public string SourceName { get; set; }

        /// <summary>Pixel dimensions of <see cref="Pixels"/>.</summary>
        public int Width { get; set; }

        /// <inheritdoc cref="Width"/>
        public int Height { get; set; }

        /// <summary>
        /// Row-major <c>RGBA8</c> pixels, top row first — PNG's row order, not Unity's. May be
        /// <c>null</c> while <see cref="AwaitPixels"/> has not yet delivered them.
        /// </summary>
        public byte[] Pixels { get; set; }

        /// <summary>
        /// The pixels once the GPU readback behind this texture has landed, in the same layout
        /// as <see cref="Pixels"/> — <c>null</c> when the readback failed. Set instead of
        /// <see cref="Pixels"/> by a capture that issued the readback asynchronously. Awaited,
        /// never blocked on: a thread parked in <c>.Result</c> per queued texture starved the
        /// pool and stalled the writer behind it (the 2026-08-29 rip hang).
        /// </summary>
        public Task<byte[]> PixelsTask { get; set; }

        /// <summary>
        /// A per-texel pass to run on the pixels before encoding (the normal-map reconstruction),
        /// or <c>null</c>. Runs on the encoder thread.
        /// </summary>
        public Action<byte[]> PostProcess { get; set; }
    }

    /// <summary>
    /// Encodes ripped textures to lossless PNG under <c>textures/&lt;sha256&gt;.png</c>, deduped by
    /// content hash, per §8 of <c>docs/bundle-schema.md</c>.
    /// Dedup happens twice, because there are two ways to be a duplicate. A texture <i>asset</i>
    /// shared by fifty surfaces is claimed once by key, so it is read back from the GPU once. Two
    /// distinct assets that happen to hold identical pixels are only discovered to be the same
    /// after encoding, and are then written once under the hash they share.
    /// Everything here is safe off the main thread; see <see cref="Png"/> for why the encoder is
    /// the mod's own.
    /// </summary>
    public class TextureWriter
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The <c>textures/</c> directory being written into.
        /// </summary>
        private readonly string _directory;

        /// <summary>
        /// The bundle-root-relative prefix every returned path carries.
        /// </summary>
        private readonly string _prefix;

        /// <summary>
        /// Texture key to bundle-relative path, for the material table to resolve against.
        /// </summary>
        private readonly ConcurrentDictionary<string, string> _paths = new();

        /// <summary>
        /// Content hashes already on disk, so an identical image is encoded once and written once.
        /// </summary>
        private readonly ConcurrentDictionary<string, bool> _hashes = new();

        /// <summary>Files written, images that turned out to be duplicates, and failures.</summary>
        public int Written, Deduped, Failed;

        /// <summary>Bytes written to disk.</summary>
        public long Bytes;

        /// <summary>
        /// Every texture that produced no file, and why.
        /// </summary>
        public ConcurrentQueue<RipWarning> Skipped = new();

        /// <summary>
        /// Create a writer over a bundle's texture directory.
        /// </summary>
        /// <param name="directory">The absolute <c>textures/</c> directory.</param>
        /// <param name="prefix">The bundle-relative prefix, e.g. <c>textures/</c>.</param>
        public TextureWriter(string directory, string prefix = "textures/")
        {
            _directory = directory;
            _prefix = prefix;
        }

        /// <summary>
        /// Encodes in flight, by texture key. <see cref="Resolve"/> waits on the one it is asked
        /// for; <see cref="Drain"/> waits on all of them.
        /// </summary>
        private readonly ConcurrentDictionary<string, Task> _pending = new();

        /// <summary>
        /// How many PNG encodes may run at once. One below the core count leaves the main
        /// thread its own core; never fewer than two, so a texture waiting on its readback does
        /// not stall the one behind it.
        /// </summary>
        private static readonly SemaphoreSlim _encoders = new(Math.Max(2, Environment.ProcessorCount - 1));

        /// <summary>
        /// Reserve a key before the pixels have been read, so two prefabs sharing a texture do not
        /// both queue it.
        /// </summary>
        /// <param name="key">The texture key.</param>
        /// <returns>Whether this caller is the one that must supply it.</returns>
        public bool Claim(string key) => _paths.TryAdd(key, null);

        /// <summary>
        /// Resolve a texture key to its bundle-relative path.
        /// </summary>
        /// <param name="key">The texture key.</param>
        /// <returns>The path, or <c>null</c> when the texture never reached disk.</returns>
        public string Resolve(string key)
        {
            if (key == null) return null;
            // A resolve is only ever asked on the writer thread for a mesh whose textures were
            // queued before it, or after `Drain` — so the wait here is on an encode already
            // running, never on one that has not been dispatched.
            if (_pending.TryRemove(key, out Task encode)) encode.Wait();
            return _paths.TryGetValue(key, out string path) ? path : null;
        }

        /// <summary>
        /// Wait for every encode in flight. Called once the queue has drained, before the
        /// material table is written.
        /// </summary>
        public void Drain()
        {
            Task[] pending = _pending.Values.ToArray();
            _pending.Clear();
            Task.WaitAll(pending);
        }

        /// <summary>
        /// Encode, hash and write one queued texture — on an encoder task, not the caller's
        /// thread. The caller is the rip's single ordered writer, and this is what stops it being
        /// the bottleneck it was: 3.8 GiB of PNG deflate and glb serialisation went through one
        /// thread behind an eight-deep queue while the main thread waited for room (refactor
        /// Phase 10, 2026-08-29). The writer still serialises meshes in order; it only waits, in
        /// <see cref="Resolve"/>, for the textures the mesh in hand actually names.
        /// </summary>
        /// <param name="texture">The texture to write.</param>
        public void Write(PendingTexture texture)
        {
            _pending[texture.Key] = Task.Run(async () =>
            {
                // The pixels may still be on the GPU: wait for them BEFORE taking an encoder
                // slot, so a readback that has not landed never holds a core idle.
                if ((texture.Pixels == null) && (texture.PixelsTask != null))
                    texture.Pixels = await texture.PixelsTask.ConfigureAwait(false);
                await _encoders.WaitAsync().ConfigureAwait(false);
                try { Encode(texture); }
                finally { _encoders.Release(); }
            });
        }

        /// <summary>
        /// The encode itself: hash the PNG, write it once.
        /// </summary>
        private void Encode(PendingTexture texture)
        {
            try
            {
                if (texture.Pixels == null)
                {
                    Fail(texture, "readback produced no pixels");
                    return;
                }
                texture.PostProcess?.Invoke(texture.Pixels);

                byte[] png = Png.EncodeRgba(texture.Pixels, texture.Width, texture.Height);
                texture.Pixels = null;
                if ((png == null) || (png.Length == 0))
                {
                    Fail(texture, "encoder produced nothing");
                    return;
                }

                string hash = Sha256(png);
                _paths[texture.Key] = $"{_prefix}{hash}.png";

                // The hash is of the file's own bytes, so an image already on disk under this name
                // is byte-identical and re-writing it would only cost time.
                //
                if (!_hashes.TryAdd(hash, true))
                {
                    Interlocked.Increment(ref Deduped);
                    return;
                }

                if (!Directory.Exists(_directory)) Directory.CreateDirectory(_directory);
                File.WriteAllBytes(Path.Combine(_directory, $"{hash}.png"), png);

                Interlocked.Increment(ref Written);
                Interlocked.Add(ref Bytes, png.LongLength);
            }
            catch (Exception ex)
            {
                Fail(texture, $"{ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to write texture `{texture.SourceName}`: {ex}");
            }
        }

        /// <summary>
        /// Record a texture that produced no file.
        /// </summary>
        private void Fail(PendingTexture texture, string reason)
        {
            Interlocked.Increment(ref Failed);
            _paths[texture.Key] = null;
            Skipped.Enqueue(new RipWarning("texture", texture.SourceName ?? texture.Key, reason));
        }

        /// <summary>
        /// The lowercase hex SHA-256 the contract names the file after.
        /// </summary>
        private static string Sha256(byte[] data)
        {
            using SHA256 sha = SHA256.Create();
            byte[] hash = sha.ComputeHash(data);

            StringBuilder builder = new(hash.Length * 2);
            foreach (byte b in hash) builder.Append(b.ToString("x2"));
            return builder.ToString();
        }
    }
}
