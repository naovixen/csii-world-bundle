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

        /// <summary>
        /// Release a run in flight.
        /// </summary>
        public void Abandon() => _run = null;

        /// <summary>
        /// Encode and write one queued texture. Called on the rip's writer thread.
        /// </summary>
        /// <param name="texture">The texture to write.</param>
        public void WriteTexture(PendingTexture texture) => _run?.Textures.Write(texture);

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
            string id = MeshRipSystem.SanitizeAssetId(surface.name);
            if (!run.Materials.ContainsKey(id)) return id;

            run.Disambiguated++;
            return MeshRipSystem.SanitizeAssetId($"{id}_{Digest(identity)}");
        }

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
                //
                if (property == NormalMap) ReconstructTwoChannelNormal(pending.Pixels);

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
        /// Record what the material library says this surface's decode binds.
        /// The material template is the authority — it is what the decode itself consults to decide
        /// where each layer goes — so the stacks it declares name every texture the surface has,
        /// including any beyond the five the contract keeps. Accumulating them over a run turns
        /// "is there a map we are still missing?" from an offline question into a line in the
        /// report — which is what the colour mask needed, since that question had only ever been
        /// put to a regex.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        private static void DeclaredProperties(MaterialRun run, SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // A surface whose template will not resolve says nothing about the others.
                //
                return;
            }

            if (stacks != null)
            {
                foreach (MaterialStackProperties stack in stacks)
                {
                    if (stack?.m_PropertyNames == null) continue;
                    foreach (string property in stack.m_PropertyNames)
                        if (!string.IsNullOrEmpty(property)) run.TemplateProperties.Add(property);
                }
            }

            // The stacks only name the virtual-textured maps. A non-VT surface binds whatever is
            // in its own texture dictionary (`SyncToUnityMaterial` walks `m_Textures` directly),
            // so those keys go into the same census — otherwise a map that exists only outside
            // the VT scheme could still ship unnoticed, which is the §4.2 failure shape again.
            //
            try
            {
                foreach (KeyValuePair<string, TextureAsset> texture in surface.textures)
                    if (!string.IsNullOrEmpty(texture.Key)) run.TemplateProperties.Add(texture.Key);
            }
            catch (Exception)
            {
                // A surface whose dictionaries did not load adds nothing.
                //
            }
        }

        /// <summary>
        /// Every shader property the decode could have bound a texture to, so that the sweep in
        /// <see cref="Release"/> destroys all of them and not merely the ones the contract keeps.
        /// If the template cannot be had, the contract's own maps are still better than leaking
        /// every one.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <returns>The property names to sweep.</returns>
        private static IEnumerable<string> DecodedProperties(SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks = null;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // Fall through to the contract's four.
            }

            if (stacks == null) return Maps;

            List<string> properties = new();
            foreach (MaterialStackProperties stack in stacks)
            {
                if (stack?.m_PropertyNames == null) continue;
                foreach (string property in stack.m_PropertyNames)
                    if (!string.IsNullOrEmpty(property)) properties.Add(property);
            }

            return (properties.Count > 0) ? properties : (IEnumerable<string>)Maps;
        }

        /// <summary>
        /// Whether a map is one the game reads out of a virtual-texturing stack rather than from a
        /// texture asset of its own.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="texture">The reference the surface handed over.</param>
        /// <returns>Whether virtual texturing owns it.</returns>
        private static bool IsVirtualTextured(SurfaceAsset surface, string property, TextureAsset texture)
        {
            try
            {
                return surface.IsHandledByVirtualTexturing(
                    new KeyValuePair<string, TextureAsset>(property, texture));
            }
            catch (Exception)
            {
                // The material library could not say. Unbound-and-unknown is still unbound.
                //
                return false;
            }
        }

        /// <summary>
        /// Whether an asset reference has nothing behind it to read.
        /// The database is checked first and separately: <c>isDummy</c> is itself defined in terms
        /// of the database, so asking a reference that has none is not obviously safe.
        /// </summary>
        /// <param name="asset">The asset to test.</param>
        /// <returns>Whether loading it would fail.</returns>
        private static bool IsUnbound(AssetData asset)
        {
            if (asset == null) return true;

            try
            {
                if (asset.database == null) return true;
                return asset.isDummy;
            }
            catch (Exception)
            {
                return true;
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
                return Capture(source, key, asset.name, cap);
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
            try
            {
                Texture texture = packed.Load();
                PendingTexture pending = Capture(texture, source.Key, surfaceName, run.TextureSizeCap);
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
                try { packed.Unload(false); } catch { /* nothing useful to do */ }
            }
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

        /// <summary>
        /// Drain the encoder, write <c>materials.json</c>, and report what came out.
        /// </summary>
        /// <param name="report">The report to append to.</param>
        /// <param name="warnings">Receives everything that produced no entry, for <c>warnings.jsonl</c>.</param>
        /// <returns>A one-line summary for the result dialog.</returns>
        public string Finish(StringBuilder report, List<RipWarning> warnings)
        {
            MaterialRun run = _run;
            _run = null;
            if (run == null) return null;

            while (run.Textures.Skipped.TryDequeue(out RipWarning skipped)) run.Skipped.Add(skipped);

            int written = WriteTable(run);

            report.AppendLine();
            // The table can hold more entries than there were surfaces: an unresolvable surface is
            // still declared, so that a mesh naming it does not dangle.
            //
            report.AppendLine($"Materials: {written} entries in materials.json from {run.Surfaces} surface(s), failed {run.Failed}, disambiguated {run.Disambiguated}.");
            report.AppendLine($"Textures: {run.TexturesRead} read back, {run.Textures.Written} file(s), {run.Textures.Deduped} duplicate(s) by content hash, failed {run.Textures.Failed}.");
            report.AppendLine($"Texture bytes: {run.Textures.Bytes / (1024d * 1024d):F1} MiB; readback held the main thread {run.ReadbackMilliseconds / 1000d:F1} s.");

            // The four buckets every distinct texture reference lands in. Read these before
            // concluding anything about the texture layer — they say which problem this is.
            //
            int references = run.TexturesBound + run.TexturesRebound + run.TexturesVirtual + run.TexturesUnbound;
            report.AppendLine($"Texture references: {references} distinct — {run.TexturesBound} loadable as given, "
                            + $"{run.TexturesRebound} rebound by guid, {run.TexturesVirtual} virtual-textured "
                            + $"({run.TexturesFromStack} recovered from a shared stack, "
                            + $"{run.TexturesFromSurfaceData} from the surface's own VT data), "
                            + $"{run.TexturesUnbound} with nothing behind them; "
                            + $"{run.TexturesFromMaterial} taken off a live material as a last resort.");
            report.AppendLine($"Surface VT data: {run.SurfaceDataLoads} file(s) opened, {run.SurfaceDataFailures} failed to decode."
                + (run.SurfaceDataGuarded > 0
                    ? $" {run.SurfaceDataGuarded} skipped by vt_crash_guard.txt (crashed the game in a previous rip)."
                    : ""));

            // The colour mask is what makes colors.json applicable, so say plainly whether it came
            // out. Zero here does not mean the city has no colour variation — it means this code is
            // looking in the wrong place, and the line below says where to look instead.
            //
            report.AppendLine($"Colour masks: {run.ColorMasks} of {written} material(s) carry one "
                            + "(the texture colors.json needs to be applicable at all).");

            if (run.TemplateProperties.Count > 0)
            {
                List<string> properties = new(run.TemplateProperties);
                properties.Sort(StringComparer.Ordinal);

                List<string> unexported = new();
                foreach (string property in properties)
                    if (Array.IndexOf(Maps, property) < 0) unexported.Add(property);

                report.AppendLine($"Surface template properties declared by the material library "
                                + $"({properties.Count}): {string.Join(", ", properties)}");
                if (unexported.Count > 0)
                {
                    report.AppendLine($"  not exported by the contract's five: {string.Join(", ", unexported)}");
                }
            }

            if (run.Skipped.Count > 0)
            {
                report.AppendLine($"Skipped {run.Skipped.Count}:");
                for (int i = 0; (i < 20) && (i < run.Skipped.Count); i++)
                    report.AppendLine($"  - {run.Skipped[i].Kind} {run.Skipped[i].AssetId}: {run.Skipped[i].Reason}");
            }

            warnings?.AddRange(run.Skipped);

            return $"{written} material(s), {run.Textures.Written} texture(s), {run.Textures.Bytes / (1024d * 1024d):F1} MiB.";
        }

        /// <summary>
        /// Write <c>materials.json</c>, resolving every texture key to the file its content hash
        /// named.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>The number of materials written.</returns>
        private int WriteTable(MaterialRun run)
        {
            string path = IOUtils.CombinePath(run.BundleRoot, "materials.json");

            // The table is bundle-wide but its producers are not: meshes and networks are separate
            // buttons, each ripping the materials of what it touches. Overwriting would mean a
            // network rip after a mesh rip leaves a bundle whose every building names a material
            // that is no longer in the file — every glTF material name has to stay a key (§6).
            // Keys this run resolved win, since they were read from the live game just now.
            //
            Dictionary<string, object> table = ReadTable(path);
            foreach (KeyValuePair<string, MaterialAsset> entry in run.Materials)
                table[entry.Key] = entry.Value.ToJson(run.Textures.Resolve);

            try
            {
                string directory = System.IO.Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory))
                    System.IO.Directory.CreateDirectory(directory);

                System.IO.File.WriteAllText(path,
                    JsonConvert.SerializeObject(table, Formatting.Indented), new UTF8Encoding(false));
            }
            catch (Exception ex)
            {
                _log.Error($"Could not write materials.json: {ex}");
                return 0;
            }

            return table.Count;
        }

        /// <summary>
        /// Read the material table a previous phase left on disk, or an empty one.
        /// A file that will not parse is treated as absent rather than fatal: losing a phase's
        /// entries is bad, but refusing to write this phase's would lose both.
        /// </summary>
        /// <param name="path">The <c>materials.json</c> path.</param>
        /// <returns>The entries already there.</returns>
        private static Dictionary<string, object> ReadTable(string path)
        {
            try
            {
                if (!System.IO.File.Exists(path)) return new Dictionary<string, object>();

                Dictionary<string, object> existing =
                    JsonConvert.DeserializeObject<Dictionary<string, object>>(System.IO.File.ReadAllText(path));

                return existing ?? new Dictionary<string, object>();
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the existing materials.json, starting a new table: {ex.Message}");
                return new Dictionary<string, object>();
            }
        }

        /// <summary>
        /// A stable, session-independent identity for an asset.
        /// The asset identifier is unique per asset; a runtime-generated asset has none, in which
        /// case its name plus type is the best available and still distinguishes it from every
        /// other asset that has one.
        /// </summary>
        /// <param name="asset">The asset.</param>
        /// <returns>The identity string.</returns>
        private static string Identity(AssetData asset)
        {
            try
            {
                string identifier = asset.identifier;
                if (!string.IsNullOrEmpty(identifier)) return identifier;
            }
            catch (Exception)
            {
                // An asset with no backing file throws rather than returning null.
                //
            }

            return $"{asset.GetType().Name}:{asset.name}";
        }

        /// <summary>
        /// A short, stable digest, matching <see cref="MeshRipSystem"/>'s so that both layers
        /// disambiguate the same way.
        /// </summary>
        /// <param name="value">The string to digest.</param>
        /// <returns>Eight lowercase hex digits.</returns>
        private static string Digest(string value)
        {
            uint hash = 2166136261u;
            foreach (char c in value)
            {
                hash ^= c;
                hash *= 16777619u;
            }

            return hash.ToString("x8");
        }
    }
}
