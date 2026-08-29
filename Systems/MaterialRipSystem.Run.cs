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
    /// The state one material rip carries, and the crash guard that survives a session ending
    /// mid-read-back.
    /// </summary>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// The mutable state of a material rip. Lives exactly as long as the mesh rip that drives
        /// it, because a material is only ever discovered by way of the prefab that uses it.
        /// </summary>
        private class MaterialRun
        {
            /// <summary>The bundle root.</summary>
            public string BundleRoot;

            /// <summary>The longest edge a written texture may have.</summary>
            public int TextureSizeCap;

            /// <summary>The texture encoder and its dedup tables.</summary>
            public TextureWriter Textures;

            /// <summary>
            /// Hands a read-back texture to the rip's single writer.
            /// </summary>
            public Action<PendingTexture> Enqueue;

            /// <summary>
            /// The material table, keyed by bundle material id.
            /// Concurrent because the writer thread reads it — to resolve a mesh's texture
            /// references — while the main thread is still adding to it.
            /// </summary>
            public ConcurrentDictionary<string, MaterialAsset> Materials = new();

            /// <summary>Surface asset identity to the material id it was given.</summary>
            public Dictionary<string, string> BySurface = new();

            /// <summary>Surfaces seen, and how many produced no material.</summary>
            public int Surfaces, Failed;

            /// <summary>Textures handed to the encoder.</summary>
            public int TexturesRead;

            /// <summary>
            /// Materials that came out with a colour mask.
            /// Reported rather than assumed: if this is zero on a real city then the colour masks
            /// are not where this code looks for them, and <c>colors.json</c> is still decoration.
            /// A count is the difference between knowing that and believing it.
            /// </summary>
            public int ColorMasks;

            /// <summary>
            /// Every shader property the material library says a surface decode binds, across the
            /// whole run.
            /// The game's own answer to "what maps are there", so the next one does not need an
            /// offline sweep — and so a map outside the contract's five is visible in the report
            /// instead of silently unexported.
            /// </summary>
            public HashSet<string> TemplateProperties = new();

            /// <summary>
            /// How each distinct texture reference turned out.
            /// These four are the diagnosis, not decoration: they separate "the surface handed us a
            /// loadable asset", "it did not, but the database had one under the same guid", "the
            /// pixels are in a VT stack" and "there is nothing anywhere". Only the third calls for
            /// a VT decoder; the others would be different bugs entirely.
            /// </summary>
            public int TexturesBound, TexturesRebound, TexturesVirtual, TexturesUnbound;

            /// <summary>How many failures have been logged in full.</summary>
            public int ReadFailures, BindFailures;

            /// <summary>Virtual-textured maps recovered from their preprocessed stack.</summary>
            public int TexturesFromStack;

            /// <summary>Virtual-textured maps recovered from the surface's own VT data.</summary>
            public int TexturesFromSurfaceData;

            /// <summary>Surfaces whose own VT data was opened, and those whose load threw.</summary>
            public int SurfaceDataLoads, SurfaceDataFailures;

            /// <summary>Surfaces skipped because a previous rip's crash marker names them.</summary>
            public int SurfaceDataGuarded;

            /// <summary>The surfaces <c>vt_crash_guard.txt</c> lists as having crashed the game.</summary>
            public HashSet<string> CrashGuard = new();

            /// <summary>Maps recovered off a live material, once every other route failed.</summary>
            public int TexturesFromMaterial;

            /// <summary>How many stack recoveries have been described in the log.</summary>
            public int StackReports;

            /// <summary>How many surface-data recoveries have been described in the log.</summary>
            public int SurfaceDataReports;

            /// <summary>
            /// Texture references already counted.
            /// Locating runs once per map slot, not once per texture, so the buckets have to be
            /// told apart from the slots that led to them — otherwise they report the number of
            /// times a texture was asked for rather than the number of textures.
            /// </summary>
            public HashSet<string> Counted = new();

            /// <summary>How long the main thread spent reading pixels back.</summary>
            public double ReadbackMilliseconds;

            /// <summary>The GPU readbacks issued and not yet landed (main thread only).</summary>
            public List<GpuReadback> Readbacks = new();

            /// <summary>How many material ids needed a suffix to stay distinct.</summary>
            public int Disambiguated;

            /// <summary>Everything that produced no entry, and why.</summary>
            public List<RipWarning> Skipped = new();

            /// <summary>
            /// Texture keys already declared missing.
            /// Locating a map runs once per surface that references it, but a gap is a property of
            /// the texture, not of each surface that wanted it — without this a map shared by two
            /// hundred surfaces would be declared two hundred times.
            /// </summary>
            public HashSet<string> Declared = new();
        }

        /// <summary>
        /// One surface's own virtual-texturing data, opened at most once and only if something
        /// needs it.
        /// <see cref="VTSurfaceAsset.Load"/> decodes the whole file — every stack and layer — and
        /// memoises the material it builds, so four map lookups cost one decode. That is also why
        /// this is deferred: of the 2,447 surfaces that ship a <c>.VTSurface</c>, 2,228 are already
        /// served by the shared stack, and their files run to tens of GiB in total. Opening one
        /// before the cheaper routes have failed would be paying the whole cost for nothing.
        /// </summary>
        private class SurfaceData
        {
            /// <summary>The surface being ripped.</summary>
            public SurfaceAsset Surface;

            /// <summary>Whether the load has been tried, successfully or not.</summary>
            public bool Attempted;

            /// <summary>The material the decode produced, with its maps bound.</summary>
            public Material Material;
        }

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            base.OnCreate();
            _log.Debug("MaterialRipSystem instance created.");
        }

        /// <summary>
        /// This system does no per-frame work of its own — it is driven entirely by
        /// <see cref="MeshRipSystem"/>, which resolves a prefab's materials at the moment it reads
        /// that prefab's geometry.
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// </summary>
        protected override void OnDestroy()
        {
            Abandon();
            base.OnDestroy();
        }

        /// <summary>
        /// Whether a material rip is in progress.
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// Start collecting materials and textures for a bundle.
        /// </summary>
        /// <param name="bundleRoot">The bundle root to write <c>textures/</c> and <c>materials.json</c> under.</param>
        /// <param name="textureSizeCap">The longest edge a written texture may have, or <c>0</c> for no cap.</param>
        /// <param name="enqueue">Hands a read-back texture to the rip's writer.</param>
        public void Begin(string bundleRoot, int textureSizeCap, Action<PendingTexture> enqueue)
        {
            Abandon();

            _run = new MaterialRun
            {
                BundleRoot = bundleRoot,
                TextureSizeCap = textureSizeCap,
                Textures = new TextureWriter(IOUtils.CombinePath(bundleRoot, "textures")),
                Enqueue = enqueue
            };

            LoadCrashGuard(_run);
        }

        /// <summary>The list of surfaces whose VT decode has crashed the game; survives restarts.</summary>
        private static string GuardPath => IOUtils.CombinePath(Instance.CartoDataPath, "vt_crash_guard.txt");

        /// <summary>The marker naming the surface whose VT decode is in flight right now.</summary>
        private static string InflightPath => IOUtils.CombinePath(Instance.CartoDataPath, "vt_inflight.txt");

        /// <summary>
        /// Arm the crash fence around <c>VTSurfaceAsset.Load</c>.
        /// The game's decoder (<c>VTSurfaceDataHandler.FillDebugMaterial</c>) slices its
        /// decompressed tile stream by header-derived offsets with no bounds checks in the release
        /// build, so a header/stream mismatch is a <b>native</b> crash — no managed catch ever
        /// sees it, and one bad surface ends the whole rip and the game with it. The fence turns
        /// that into evidence: a marker file names each surface before its decode and is deleted
        /// after, so a marker still on disk at the next rip <i>is</i> the culprit, gets appended
        /// to the guard list, and is never decoded again — its maps fall through to the
        /// live-material route and the skip is declared in the report and warnings.
        /// </summary>
        /// <param name="run">The run to arm.</param>
        private static void LoadCrashGuard(MaterialRun run)
        {
            try
            {
                if (File.Exists(InflightPath))
                {
                    string victim = File.ReadAllText(InflightPath).Trim();
                    File.Delete(InflightPath);
                    if (victim.Length > 0)
                    {
                        _log.Warn($"A previous rip crashed while decoding VT surface data for `{victim}`; "
                                + $"it is now on the skip list at {GuardPath}.");
                        File.AppendAllText(GuardPath, victim + Environment.NewLine);
                    }
                }

                if (File.Exists(GuardPath))
                {
                    foreach (string line in File.ReadAllLines(GuardPath))
                    {
                        if (line.Trim().Length > 0) run.CrashGuard.Add(line.Trim());
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Crash guard unavailable: {ex.Message}");
            }
        }
    }
}
