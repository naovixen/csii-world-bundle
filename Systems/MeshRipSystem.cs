using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that reads real mesh geometry out of the game and writes it as glTF.
    /// CS2 keeps mesh geometry in plain ECS dynamic buffers on the prefab entity —
    /// <c>MeshVertex</c>, <c>MeshNormal</c>, <c>MeshUV0</c>, <c>MeshTangent</c>, <c>MeshIndex</c> —
    /// alongside a <c>MeshData</c> component and a <c>MeshMaterial</c> buffer describing each
    /// drawable range. The game itself reads these every frame for mouse picking
    /// (<c>Game.Common.RaycastSystem</c>) and camera collision
    /// (<c>Game.Rendering.CameraCollisionSystem</c>), so the data is CPU-resident for the whole
    /// session and needs no GPU readback.
    /// Traversal is: object prefab entity → <c>SubMesh</c> buffer → mesh entity → geometry buffers.
    /// </summary>
    public partial class MeshRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for every placed object, matching <see cref="InstanceRipSystem"/>'s exactly so
        /// the prefab set ripped here is the prefab set <c>instances.jsonl</c> references.
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// The query for the game's <c>MeshSettings</c> singleton, which names the default base
        /// mesh a building's foundation skirt draws with when its prefab declares no
        /// <c>BaseProperties</c> of its own.
        /// </summary>
        EntityQuery _meshSettingsQuery;

        /// <summary>
        /// The run in progress, or <c>null</c> when idle.
        /// </summary>
        RipRun _run;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            // The instance pass's query, shared: the two layers must describe the same world.
            _objectQuery = GetEntityQuery(RipQueries.WorldObjects());

            _meshSettingsQuery = GetEntityQuery(ComponentType.ReadOnly<MeshSettingsData>());

            base.OnCreate();
            _log.Debug("MeshRipSystem instance created.");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// A rip in flight must be released here. The writer task blocks on the queue until the
        /// reader declares it complete, and once this system stops updating — the user quit to the
        /// menu mid-rip — nothing else ever would.
        /// </summary>
        protected override void OnDestroy()
        {
            RipRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Mesh rip abandoned at {run.Index}/{run.Items.Count} prefab(s); the world was unloaded.");
                try { run.Queue.CompleteAdding(); } catch { /* already completed */ }
                Instance.MaterialRip.Abandon();
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Advance any rip in progress by one frame's worth of work. (The progress indicator is
        /// pumped by <c>RipOrchestratorSystem</c>, which owns the rip's UX.)
        /// This system is registered at <see cref="SystemUpdatePhase.MainLoop"/> rather than with
        /// the simulation, because a rip is started from the options page — which pauses the
        /// simulation, so a simulation-phase system would never tick while the user is looking at
        /// it.
        /// </summary>
        protected override void OnUpdate()
        {
            RipRun run = _run;
            if (run != null) Advance(run);
        }
        // ====================================================================================
        // PLAN Phase 3 — mesh dump, one .glb per unique prefab.
        //
        // ====================================================================================

        /// <summary>
        /// One prefab waiting to be ripped.
        /// </summary>
        private struct WorkItem
        {
            public Entity PrefabEntity;
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
        }

        /// <summary>
        /// One thing waiting to be written: either an assembled prefab or a read-back texture.
        /// <b>Both travel in one queue, drained by one writer, in the order they were produced.</b>
        /// That ordering is not incidental — it is what lets a <c>.glb</c> reference its textures.
        /// A texture's bundle path is the content hash of the PNG it becomes, which is only known
        /// after encoding; the reader resolves and queues a prefab's textures before it queues the
        /// prefab, so a single ordered writer always encodes them first and the mesh can name the
        /// files by the time it is serialised. Two queues, or two writers, would race.
        /// </summary>
        private struct RipItem
        {
            /// <summary>The prefab to serialise, or <c>null</c>.</summary>
            public PrefabMesh Prefab;

            /// <summary>The texture to encode, or <c>null</c>.</summary>
            public PendingTexture Texture;

            /// <summary>A foundation skirt to generate and serialise, or <c>null</c>.</summary>
            public SkirtWork Skirt;
        }

        /// <summary>
        /// One prefab's foundation-skirt job, generated on the writer task so the silhouette scan
        /// costs the main thread nothing.
        /// A prefab part whose <c>MeshData</c> carries <c>MeshFlags.Base</c> draws one extra
        /// generated batch — the skirt between its floor and the terrain (see
        /// <see cref="IO.BaseSkirtBuilder"/>). The band is written as
        /// <c>&lt;assetId&gt;__base.glb</c>, separate from the prefab's own file, because it obeys
        /// a draw rule of its own: a consumer drops its bottom ring to the terrain surface, and
        /// skips it entirely for instances not resting on the ground.
        /// </summary>
        private class SkirtWork
        {
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
            public List<SkirtPart> Parts = new();
        }

        /// <inheritdoc cref="SkirtWork"/>
        private struct SkirtPart
        {
            public MeshAsset Source;
            public float BaseOffset;
            public float3 Position;
            public quaternion Rotation;
            public string NodeName;
            public string MaterialId;
        }

        /// <summary>
        /// The mutable state of a rip in progress.
        /// A full rip reads thousands of prefabs and writes gigabytes, which cannot happen inside a
        /// button handler without freezing the game for minutes. The work is therefore split in two:
        /// reading geometry stays on the main thread — <c>EntityManager</c> and the asset database
        /// permit nothing else — but only for a few milliseconds per frame, while glTF serialisation
        /// and disk writes run on a background task fed through <see cref="Queue"/>.
        /// </summary>
        private class RipRun
        {
            /// <summary>The <c>meshes/</c> directory being written into.</summary>
            public string OutputDirectory;

            /// <summary>The prefabs to rip, richest-covering first.</summary>
            public List<WorkItem> Items;

            /// <summary>How far through <see cref="Items"/> the reader has got.</summary>
            public int Index;

            /// <summary>Assembled prefabs and read-back textures waiting to be written.</summary>
            public BlockingCollection<RipItem> Queue;

            /// <summary>The background serialiser.</summary>
            public Task Writer;

            /// <summary>
            /// The material system, resolved on the main thread and held for the writer.
            /// Looking a system up goes through the ECS world, which is a main-thread affair; the
            /// writer needs the reference but must not be the one to obtain it.
            /// </summary>
            public MaterialRipSystem Materials;

            /// <summary>Whether the reader has finished handing work over.</summary>
            public bool ReadingDone;

            /// <summary>
            /// Counters. The first three belong to the reader on the main thread, the rest to the
            /// writer task — kept apart so neither needs to synchronise with the other.
            /// </summary>
            public int Read, Skipped, ReadFailed;
            public int Written, WriteFailed;
            public long Bytes;

            /// <summary>
            /// Foundation skirts written, and Base-flagged parts whose mesh presented no
            /// silhouette at the base level (a valid outcome — the game generates an empty batch
            /// for those too). Writer-task counters, read after it drains.
            /// </summary>
            public int SkirtsWritten, SkirtsEmpty;

            /// <summary>Everything that did not become a file.</summary>
            public int Failed => ReadFailed + WriteFailed;

            /// <summary>Lines the writer wants in the report.</summary>
            public ConcurrentQueue<string> WriterNotes = new();

            /// <summary>Per-file sizes, for the size breakdown.</summary>
            public ConcurrentBag<KeyValuePair<string, long>> Sizes = new();

            /// <summary>
            /// Every prefab that produced no file, and why.
            /// These asset ids are still referenced by <c>instances.jsonl</c>, so the bundle must
            /// account for them rather than leave a consumer to discover the gap.
            /// </summary>
            public List<RipWarning> SkippedAssets = new();

            /// <summary>The report accumulated on the main thread.</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.</summary>
            public Stopwatch Clock = Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// </summary>
            public Action<RipOutcome> Completed;
        }

        /// <summary>
        /// How long the reader may hold the main thread each frame.
        /// One prefab is always processed per frame even when it overruns, so a single very large
        /// mesh cannot deadlock the run.
        /// </summary>
        private const double FrameBudgetMilliseconds = 6d;

        /// <summary>
        /// How many items may wait for the writer.
        /// Every item holds a full uncompressed payload — a prefab's vertex arrays, or a texture's
        /// pixels at four bytes each — so an unbounded queue would simply move the memory problem
        /// rather than solve it. The reader pauses when the queue is full.
        /// </summary>
        private const int QueueCapacity = 8;

        /// <summary>
        /// Whether a rip is currently running.
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How many prefabs merged more than one geometry-asset mesh since the rip began.
        /// Zero on a vanilla city means every placed prefab keeps one mesh per asset; a non-zero
        /// count is the rule <c>RenderPrefab.ObtainMesh</c> flattens actually firing, and the
        /// report states it rather than leaving the merge unobserved.
        /// </summary>
        public int MultiMeshPrefabs { get; private set; }

        /// <summary>
        /// How many prefabs yielded fewer meshes than their geometry asset holds.
        /// A geometry asset's meshes are simultaneous parts, so a partial merge is dropped
        /// geometry — the adult oak's leaves are mesh 1 behind the same asset as its trunk, and
        /// losing them silently is how every tree in a bundle came out dead. Non-zero demands a
        /// look at the log, where each drop names its prefab.
        /// </summary>
        public int PartialMeshPrefabs { get; private set; }

        /// <summary>
        /// Sub-mesh entries skipped because every layer they default to is an underground cutaway
        /// layer — the per-part form of the §17.5 prefab rule, for prefabs that mix visible parts
        /// with utility furniture.
        /// </summary>
        public int PipelinePartsSkipped { get; private set; }

        /// <summary>
        /// How many sub-mesh entries were the obsolete-content placeholder, stretched to the
        /// prefab's bounds the way the renderer stretches it.
        /// </summary>
        public int PlaceholderParts { get; private set; }

        /// <summary>
        /// How many stacked prefabs also carried parts with no stack role, shipped as the
        /// <c>__stack_rest</c> file the instance pass places once per stacked instance.
        /// </summary>
        public int StackRestParts { get; private set; }

        /// <summary>
        /// How many derived tree-state files (<c>__dead</c>, <c>__stump</c>) were written.
        /// A tree prefab authors its dead and stump silhouettes as extra <c>SubMesh</c> entries the
        /// renderer swaps in from <c>Tree.m_State</c>; without them a bundle draws every dead tree
        /// in full leaf.
        /// </summary>
        public int TreeStateVariants { get; private set; }

        /// <summary>
        /// How many derived stockpile-fullness files (<c>__partial1</c>, <c>__partial2</c>,
        /// <c>__full</c>) were written. The empty state is the base file, so it has no suffix.
        /// </summary>
        public int FullnessVariants { get; private set; }

        /// <summary>
        /// Where a partial-read declaration goes when no mesh rip is in flight.
        /// The network rip reads lane part meshes through <see cref="ReadMesh"/>; a mesh that
        /// comes up short there was declared only when a <i>mesh</i> run existed — a full-city
        /// rip logged <c>Highway_Lane_4_Mesh</c> unreadable and its bundle's
        /// <c>warnings.jsonl</c> said nothing. The active network run registers its own warning
        /// list here for the duration (contract §10: incompleteness is declared, never silent).
        /// </summary>
        public List<RipWarning> WarningSink;

        /// <summary>
        /// Start ripping every unique prefab placed in the city into <c>meshes/&lt;assetId&gt;.glb</c>.
        /// Returns as soon as the work list is built; the rip itself proceeds a slice per frame in
        /// <see cref="OnUpdate"/> and reports its result in a dialog when it finishes.
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write <c>meshes/</c> under.</param>
        /// <param name="maxPrefabs">The most prefabs to rip, or <c>0</c> for all of them.</param>
        /// <param name="textureSizeCap">The longest edge a written texture may have, <c>0</c> for no cap, or negative to write no textures at all.</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.</param>
        /// <param name="kinds">Which instance kinds' prefabs to rip; <c>null</c> for all — must match the instance pass so the two layers stay 1:1.</param>
        /// <returns>A short description of what was queued.</returns>
        public string BeginRip(string outputDirectory, int maxPrefabs = 0, int textureSizeCap = 2048, Action<RipOutcome> completed = null, RipKindFilter? kinds = null)
        {
            if (_run != null) return "A mesh rip is already running.";

            MultiMeshPrefabs = 0;
            PartialMeshPrefabs = 0;
            PipelinePartsSkipped = 0;
            PlaceholderParts = 0;
            StackRestParts = 0;
            TreeStateVariants = 0;
            FullnessVariants = 0;

            RipProgress.Report(RipProgress.Phase.Scan);

            RipRun run = new()
            {
                OutputDirectory = IOUtils.CombinePath(outputDirectory, "meshes"),
                Queue = new BlockingCollection<RipItem>(QueueCapacity),
                Materials = Instance.MaterialRip,
                Completed = completed
            };

            run.Items = CollectWorkItems(run.Report, run.SkippedAssets, maxPrefabs, kinds ?? RipKindFilter.All, out int distinctPrefabs, out int placedObjects);
            run.Report.AppendLine($"Placed objects: {placedObjects}, distinct prefabs: {distinctPrefabs}, queued: {run.Items.Count}");

            if (run.Items.Count == 0)
            {
                RipProgress.Clear();
                return "Nothing to rip: no placed objects resolved to a prefab.";
            }

            // Materials ride along with the meshes: a prefab's surface assets are reached through
            // the very render prefab its geometry came from, so resolving them in a second pass
            // would mean walking the whole prefab set twice for data already in hand.
            //
            run.Materials.Begin(outputDirectory, textureSizeCap,
                texture => Enqueue(run, new RipItem { Texture = texture }));

            run.Writer = Task.Run(() => WriteQueued(run));
            _run = run;

            RipProgress.Report(RipProgress.Phase.Meshes, 0, run.Items.Count);

            // Be specific about where to look. The indicator draws in the game HUD, which this
            // options page is covering — told only to "watch the corner", the obvious thing to do
            // is stare at the menu and conclude nothing is happening.
            //
            return $"Ripping {run.Items.Count} prefab(s) in the background.\n\n"
                 + "Close this options page to watch the progress indicator — it appears in the top-right of the game view, not over the menu.\n\n"
                 + "A dialog reports the result when it finishes.";
        }

        /// <summary>
        /// Build the work list: every distinct prefab referenced by a placed object, ordered by how
        /// many objects use it.
        /// Ordering matters only when the run is capped — ripping the most-used prefabs first means
        /// a capped run still covers most of the city rather than an arbitrary corner of it.
        /// </summary>
        /// <param name="report">The report to note discovery counts in.</param>
        /// <param name="skippedAssets">Receives a §10 declaration for every prefab the cap drops.</param>
        /// <param name="maxPrefabs">The cap, or <c>0</c> for none.</param>
        /// <param name="kinds">Which instance kinds' prefabs to keep.</param>
        /// <param name="distinctPrefabs">Receives the number of distinct prefabs found.</param>
        /// <param name="placedObjects">Receives the number of placed objects walked.</param>
        /// <returns>The work list.</returns>
        private List<WorkItem> CollectWorkItems(StringBuilder report, List<RipWarning> skippedAssets, int maxPrefabs, RipKindFilter kinds, out int distinctPrefabs, out int placedObjects)
        {
            Dictionary<Entity, int> counts = new();

            // The instance pass filters kinds per instance, and one prefab's placements can span
            // kinds — the building/tree kinds hang off the placed entity, not the prefab. Judging
            // a prefab by a single sampled instance therefore breaks the join under a filter that
            // excludes anything: an instance kept by the instance pass could reference a mesh
            // this pass dropped. Collect the union of kinds actually placed for each prefab and
            // keep the prefab when any of them is allowed. Only built when the filter can exclude
            // at all, so the default all-kinds rip pays nothing per instance.
            //
            Dictionary<Entity, HashSet<string>> kindsByPrefab = kinds.Everything ? null : new();
            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                placedObjects = entities.Length;
                int utilityNodes = 0, vehicleCargo = 0;

                foreach (Entity entity in entities)
                {
                    // Dropped before it is counted, so the prefab set here matches the one
                    // InstanceRipSystem builds and the two layers assign the same ids.
                    //
                    if (IsUtilityMarkerSubObject(EntityManager, entity))
                    {
                        utilityNodes++;
                        continue;
                    }

                    if (IsVehicleCargoSubObject(EntityManager, entity))
                    {
                        vehicleCargo++;
                        continue;
                    }

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity == Entity.Null) continue;

                    counts.TryGetValue(prefabEntity, out int count);
                    counts[prefabEntity] = count + 1;

                    if (kindsByPrefab != null)
                    {
                        if (!kindsByPrefab.TryGetValue(prefabEntity, out HashSet<string> kindSet))
                            kindsByPrefab[prefabEntity] = kindSet = new HashSet<string>();
                        kindSet.Add(Instance.InstanceRip.ClassifyKind(entity, prefabEntity));
                    }
                }

                if (utilityNodes > 0) report.AppendLine($"Skipped {utilityNodes} buried utility-network node object(s).");
                if (vehicleCargo > 0) report.AppendLine($"Skipped {vehicleCargo} cargo prop(s) riding vehicles.");
            }
            finally
            {
                entities.Dispose();
            }

            distinctPrefabs = counts.Count;

            // Ids come from the shared assignment, not from each name in isolation, so that a
            // duplicated prefab name cannot make two prefabs write to the same file. The
            // assignment runs over the FULL prefab set even when a kind filter is active —
            // ids must not depend on the selection, or partial re-rips stop joining up.
            //
            Dictionary<Entity, RipAssetIds.AssetIdEntry> assetIds = RipAssetIds.BuildAssetIds(EntityManager, counts.Keys, out int disambiguated);
            if (disambiguated > 0) report.AppendLine($"Disambiguated {disambiguated} prefab(s) sharing a name.");

            int filtered = 0;
            List<WorkItem> items = new(counts.Count);
            foreach (KeyValuePair<Entity, int> kvp in counts)
            {
                RipAssetIds.AssetIdEntry entry = assetIds[kvp.Key];

                // The advanced kind filter, judged over every kind the prefab was actually
                // placed as — kept whenever any instance of it survives the instance pass's
                // per-instance filter, so no kept record references a dropped mesh.
                //
                if (kindsByPrefab != null)
                {
                    bool allowed = false;
                    foreach (string kind in kindsByPrefab[kvp.Key])
                    {
                        if (kinds.Allows(kind)) { allowed = true; break; }
                    }

                    if (!allowed)
                    {
                        filtered++;
                        continue;
                    }
                }

                // An unresolvable prefab still gets ripped under the same synthetic id
                // InstanceRipSystem gives its instances, so the two layers keep joining up.
                //
                items.Add(new WorkItem
                {
                    PrefabEntity = kvp.Key,
                    AssetId = entry.AssetId,
                    SourceName = GetPrefabName(kvp.Key) ?? entry.AssetId,
                    InstanceCount = kvp.Value
                });
            }

            if (filtered > 0) report.AppendLine($"Excluded {filtered} prefab(s) by the layer selection (advanced kind toggles).");

            items.Sort((a, b) => b.InstanceCount.CompareTo(a.InstanceCount));

            if ((maxPrefabs > 0) && (items.Count > maxPrefabs))
            {
                // Every capped-off prefab's asset id is still referenced by instances.jsonl, so
                // each must be declared in warnings.jsonl for a capped rip to stay contract-valid
                // (§10: a missing mesh declared is a warning; undeclared, a validator error).
                // A capped stacked prefab's instances reference its per-role part files rather
                // than the whole-prefab file (the derivation ExpandStack mirrors), so those ids
                // are declared alongside it — as are the alternative-state files an uncapped rip
                // would have derived, so a consumer looking for `__dead` on a dead tree finds a
                // declared gap rather than a silent one.
                //
                string reason = $"skipped by the mesh cap ({maxPrefabs} most-used prefabs); instances kept with placement only";
                for (int i = maxPrefabs; i < items.Count; i++)
                {
                    WorkItem item = items[i];
                    skippedAssets.Add(new RipWarning("mesh", item.AssetId, reason));

                    if (!EntityManager.HasBuffer<SubMesh>(item.PrefabEntity)) continue;

                    DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(item.PrefabEntity, true);
                    SubMesh[] subMeshes = new SubMesh[buffer.Length];
                    for (int j = 0; j < buffer.Length; j++) subMeshes[j] = buffer[j];

                    if (EntityManager.HasComponent<StackData>(item.PrefabEntity))
                    {
                        HashSet<StackRole> roles = new();
                        bool rest = false;

                        for (int j = 0; j < subMeshes.Length; j++)
                        {
                            StackRole? role = PrefabMesh.RoleOf(subMeshes[j].m_Flags);
                            if (role.HasValue) roles.Add(role.Value);
                            else if (ExportsSubMesh(subMeshes[j].m_Flags)) rest = true;
                        }

                        foreach (StackRole role in roles)
                            skippedAssets.Add(new RipWarning("mesh", PrefabMesh.StackPartId(item.AssetId, role), reason));
                        if (rest) skippedAssets.Add(new RipWarning("mesh", PrefabMesh.StackRestId(item.AssetId), reason));
                    }

                    foreach (StateVariant variant in StateVariants)
                    {
                        if (Authors(subMeshes, variant))
                            skippedAssets.Add(new RipWarning("mesh", item.AssetId + variant.Suffix, reason));
                    }
                }

                report.AppendLine($"Capped to the {maxPrefabs} most-used prefab(s) of {items.Count}; "
                                + $"{items.Count - maxPrefabs} capped-off prefab(s) declared in warnings.jsonl.");
                items.RemoveRange(maxPrefabs, items.Count - maxPrefabs);
            }

            return items;
        }

        /// <summary>
        /// Read one frame's worth of prefabs, then finish the run once the writer has drained.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        private void Advance(RipRun run)
        {
            if (!run.ReadingDone)
            {
                // A faulted writer never drains the queue: the reader would pause on
                // back-pressure for the rest of the session, ReadingDone would never turn true,
                // and a pipeline driving the rip would wait on this stage forever. Abort instead.
                //
                if (run.Writer.IsFaulted)
                {
                    Fail(run);
                    return;
                }

                Stopwatch budget = Stopwatch.StartNew();

                while (run.Index < run.Items.Count)
                {
                    // Back-pressure: pause reading rather than pile assembled meshes up in memory
                    // faster than they can be written.
                    if (run.Queue.Count >= QueueCapacity) break;

                    ReadOne(run, run.Items[run.Index]);
                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
                }

                RipProgress.Report(RipProgress.Phase.Meshes, run.Index, run.Items.Count);

                if (run.Index >= run.Items.Count)
                {
                    run.ReadingDone = true;
                    run.Queue.CompleteAdding();
                }

                return;
            }

            RipProgress.Report(RipProgress.Phase.Write, System.Threading.Volatile.Read(ref run.Written), run.Read);
            if (run.Writer.IsCompleted) Finish(run);
        }

        /// <summary>
        /// Assemble one prefab and hand it to the writer.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="item">The prefab to read.</param>
        private void ReadOne(RipRun run, WorkItem item)
        {
            try
            {
                PrefabMesh prefab = BuildPrefabMesh(item, out string skipReason, out SkirtWork skirt, out List<PrefabMesh> variants);

                if (prefab == null)
                {
                    run.Skipped++;
                    run.SkippedAssets.Add(new RipWarning("mesh", item.AssetId, skipReason));
                    if (run.Skipped <= 20) run.Report.AppendLine($"  - {item.AssetId}: {skipReason}");
                    return;
                }

                run.Read++;
                Enqueue(run, new RipItem { Prefab = prefab });

                // The skirt rides behind the prefab that owns it. Its base surface was resolved —
                // and its textures queued — while the prefab was assembled, so the ordered writer
                // encodes them before the band's .glb needs their names.
                //
                if (skirt != null) Enqueue(run, new RipItem { Skirt = skirt });

                // The alternative-state files a prefab authors — a dead tree, a stump, a part-full
                // stockpile. Each is a whole replacement for the prefab's own file, chosen by the
                // consumer from `attrs.treeState` / `attrs.fullness`, and exists only where the
                // prefab flags the state, so absence means "the game has no such variant either".
                //
                foreach (PrefabMesh variant in variants) Enqueue(run, new RipItem { Prefab = variant });

                // A stacked prefab also ships one file per stack role, so that the instance pass
                // can place a foot once, a repeating section as many times as that instance needs,
                // and a cap once. The whole-prefab file above stays, and is still the right thing
                // for an instance of the same prefab that has no `Stack` component — a pillar used
                // as a standalone prop rather than under a viaduct.
                //
                bool stacked = false;
                foreach (StackRole role in prefab.StackRoles())
                {
                    stacked = true;
                    PrefabMesh part = new()
                    {
                        AssetId = PrefabMesh.StackPartId(prefab.AssetId, role),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == role) part.Parts.Add(p);

                    Enqueue(run, new RipItem { Prefab = part });
                }

                // Parts with no stack role on a stacked prefab are drawn once, untiled, at the
                // instance transform — the game's sub-mesh test grants `IsStack*` bits through the
                // instance's `Stack` component and an unflagged entry passes regardless. They get
                // their own file because the instance pass never references the whole-prefab file
                // for a stacked prefab (every instance of one carries `Stack` by archetype), and
                // the stack part files must not double them per tile.
                //
                if (stacked)
                {
                    PrefabMesh rest = new()
                    {
                        AssetId = PrefabMesh.StackRestId(prefab.AssetId),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == null) rest.Parts.Add(p);

                    if (rest.Parts.Count > 0)
                    {
                        StackRestParts++;
                        Enqueue(run, new RipItem { Prefab = rest });
                    }
                }
            }
            catch (Exception ex)
            {
                run.ReadFailed++;
                run.Report.AppendLine($"  ! {item.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to assemble prefab `{item.AssetId}`: {ex}");
            }
        }

        /// <summary>
        /// Hand one item to the bounded queue without wedging on a dead writer.
        /// A plain <c>Add</c> blocks until the writer makes room, and a writer task that has
        /// faulted never will — several items can be queued per prefab between
        /// <see cref="Advance"/>'s capacity checks, so the reader could block the main thread
        /// forever mid-<see cref="ReadOne"/>. Throwing instead lets <see cref="ReadOne"/>'s
        /// catch count the prefab failed, and the next <see cref="Advance"/> aborts the pass.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="item">The item to queue.</param>
        private static void Enqueue(RipRun run, RipItem item)
        {
            while (!run.Queue.TryAdd(item, 200))
            {
                if (run.Writer.IsFaulted)
                    throw new InvalidOperationException("the mesh writer task has faulted; the queue will never drain");
            }
        }

        /// <summary>
        /// Abort a rip whose writer task has died mid-read. Nothing more can ever be written —
        /// the queue will not drain again — so the only honest outcome is a failed stage:
        /// release what the run holds and hand back a <see cref="RipOutcome"/> that SAYS it
        /// failed. The summary still opens with the word FAILED because a person reads it; the
        /// orchestrator does not — it used to infer failure from that spelling, which meant a
        /// stage writing "Failed" was silently a success.
        /// </summary>
        /// <param name="run">The rip whose writer faulted.</param>
        private void Fail(RipRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.Writer.Exception?.GetBaseException();
            _log.Error($"Mesh writer faulted at {run.Index}/{run.Items.Count} prefab(s); aborting the pass: {run.Writer.Exception}");

            // The same release OnDestroy performs for a rip in flight: unblock anything waiting
            // on the queue and let the material rip drop its half-built state.
            //
            try { run.Queue.CompleteAdding(); } catch { /* already completed */ }
            Instance.MaterialRip.Abandon();

            string summary = $"FAILED: the mesh writer faulted at {run.Index}/{run.Items.Count} prefab(s) — "
                           + $"meshes/ is incomplete ({System.Threading.Volatile.Read(ref run.Written)} file(s) before the fault): "
                           + $"{cause?.Message ?? "unknown error"}\n\nFull details: Logs/Carto.log";

            RipReport.Deliver(run.Completed, RipOutcome.Failure(summary), summary, "mesh rip", _log);
        }

        /// <summary>
        /// Serialise queued prefabs to disk. Runs on a background task for the whole rip.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        private static void WriteQueued(RipRun run)
        {
            MaterialRipSystem materials = run.Materials;

            foreach (RipItem item in run.Queue.GetConsumingEnumerable())
            {
                // Textures first, always: they arrive before the prefab that named them, and
                // encoding one is what gives it the content hash the prefab's .glb writes down.
                //
                if (item.Texture != null)
                {
                    materials.WriteTexture(item.Texture);
                    continue;
                }

                if (item.Skirt != null)
                {
                    WriteSkirt(run, item.Skirt);
                    continue;
                }

                PrefabMesh prefab = item.Prefab;
                if (prefab == null) continue;

                try
                {
                    string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                    long size = GltfWriter.Write(path, prefab, materials.Describe);

                    System.Threading.Interlocked.Increment(ref run.Written);
                    System.Threading.Interlocked.Add(ref run.Bytes, size);
                    run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
                }
                catch (Exception ex)
                {
                    System.Threading.Interlocked.Increment(ref run.WriteFailed);
                    run.WriterNotes.Enqueue($"  ! {prefab.AssetId}: {ex.GetType().Name} {ex.Message}");
                    _log.Warn($"Failed to write `{prefab.AssetId}.glb`: {ex}");
                }
            }
        }

        /// <summary>
        /// Generate one prefab's foundation-skirt band and write it as
        /// <c>&lt;assetId&gt;__base.glb</c>. Runs on the writer task.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="skirt">The gathered inputs.</param>
        private static void WriteSkirt(RipRun run, SkirtWork skirt)
        {
            try
            {
                PrefabMesh prefab = new()
                {
                    AssetId = skirt.AssetId,
                    SourceName = skirt.SourceName,
                    InstanceCount = skirt.InstanceCount
                };

                foreach (SkirtPart part in skirt.Parts)
                {
                    MeshAsset band = BaseSkirtBuilder.Build(part.Source, part.BaseOffset,
                                                            skirt.AssetId, skirt.SourceName, part.MaterialId);
                    if (band == null) continue;

                    prefab.Parts.Add(new PrefabMeshPart
                    {
                        Mesh = band,
                        Position = part.Position,
                        Rotation = part.Rotation,
                        NodeName = part.NodeName
                    });
                }

                // No silhouette at the base level is a valid outcome — the game generates an empty
                // batch there too — so it is counted, not warned about.
                //
                if (prefab.Parts.Count == 0)
                {
                    System.Threading.Interlocked.Increment(ref run.SkirtsEmpty);
                    return;
                }

                string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                long size = GltfWriter.Write(path, prefab, run.Materials.Describe);

                System.Threading.Interlocked.Increment(ref run.SkirtsWritten);
                System.Threading.Interlocked.Add(ref run.Bytes, size);
                run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
            }
            catch (Exception ex)
            {
                System.Threading.Interlocked.Increment(ref run.WriteFailed);
                run.WriterNotes.Enqueue($"  ! {skirt.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to write `{skirt.AssetId}.glb`: {ex}");
            }
        }

        /// <summary>
        /// Assemble a prefab entity's sub-meshes into one <see cref="PrefabMesh"/>.
        /// </summary>
        /// <param name="item">The prefab to assemble.</param>
        /// <param name="skipReason">Why nothing came out, when the result is <c>null</c>.</param>
        /// <param name="skirt">The foundation skirt to generate behind it, or <c>null</c>.</param>
        /// <param name="variants">The alternative-state files to write alongside it; never <c>null</c>.</param>
        /// <returns>The assembled prefab, or <c>null</c>.</returns>
        private PrefabMesh BuildPrefabMesh(WorkItem item, out string skipReason, out SkirtWork skirt, out List<PrefabMesh> variants)
        {
            skipReason = null;
            skirt = null;
            variants = new List<PrefabMesh>();

            if (!EntityManager.HasBuffer<SubMesh>(item.PrefabEntity))
            {
                skipReason = "no SubMesh buffer";
                return null;
            }

            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(item.PrefabEntity, true);
            if (buffer.Length == 0)
            {
                skipReason = "empty SubMesh buffer";
                return null;
            }

            // Copy out before reading geometry: obtaining a mesh loads an asset, and a dynamic
            // buffer is only valid until the entity's chunk moves.
            //
            SubMesh[] subMeshes = new SubMesh[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) subMeshes[i] = buffer[i];

            List<int> selected = SelectSubMeshes(subMeshes);

            PrefabMesh prefab = AssembleParts(item, subMeshes, selected, item.AssetId, out skirt);

            if (prefab == null)
            {
                skipReason = $"none of {subMeshes.Length} sub-mesh(es) yielded geometry";
                return null;
            }

            BuildStateVariants(item, subMeshes, variants);
            return prefab;
        }

        /// <summary>
        /// Write each alternative state the prefab authors as its own derived file.
        /// Only states with a sub-mesh entry flagged for them are built, so an ordinary prop adds
        /// nothing; a tree adds <c>__dead</c> and <c>__stump</c>, a stockpile its fullness steps.
        /// A derived file has no skirt of its own — the base band is a property of the prefab's
        /// footprint, not of which silhouette is standing on it.
        /// </summary>
        /// <param name="item">The prefab being ripped.</param>
        /// <param name="subMeshes">Its sub-mesh entries.</param>
        /// <param name="variants">Receives the assembled variants.</param>
        private void BuildStateVariants(WorkItem item, SubMesh[] subMeshes, List<PrefabMesh> variants)
        {
            foreach (StateVariant variant in StateVariants)
            {
                if (!Authors(subMeshes, variant)) continue;

                List<int> selected = SelectSubMeshes(subMeshes, variant.State, false);
                if (selected.Count == 0) continue;

                PrefabMesh mesh = AssembleParts(item, subMeshes, selected, item.AssetId + variant.Suffix, out SkirtWork _);
                if (mesh == null) continue;

                variants.Add(mesh);
                if (variant.Tree) TreeStateVariants++; else FullnessVariants++;
            }
        }

        /// <summary>
        /// Whether a prefab authors a given alternative state at all.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <param name="variant">The state to look for.</param>
        /// <returns>Whether any entry is flagged for it.</returns>
        private static bool Authors(SubMesh[] subMeshes, StateVariant variant)
        {
            for (int i = 0; i < subMeshes.Length; i++)
            {
                if (((subMeshes[i].m_Flags & variant.Required) != 0)
                    && ((subMeshes[i].m_Flags & SubMeshFlags.OutlineOnly) == 0)) return true;
            }

            return false;
        }

        /// <summary>
        /// Assemble one chosen set of sub-mesh entries into a <see cref="PrefabMesh"/>.
        /// </summary>
        /// <param name="item">The prefab being ripped.</param>
        /// <param name="subMeshes">Its sub-mesh entries.</param>
        /// <param name="selected">The entries to assemble.</param>
        /// <param name="assetId">The id the assembled file takes.</param>
        /// <param name="skirt">The foundation skirt gathered along the way, or <c>null</c>.</param>
        /// <returns>The assembled mesh, or <c>null</c> if nothing yielded geometry.</returns>
        private PrefabMesh AssembleParts(WorkItem item, SubMesh[] subMeshes, List<int> selected, string assetId, out SkirtWork skirt)
        {
            skirt = null;

            PrefabMesh prefab = new()
            {
                AssetId = assetId,
                SourceName = item.SourceName,
                InstanceCount = item.InstanceCount,
                SkippedVariants = subMeshes.Length - selected.Count
            };

            HashSet<string> nodeNames = new();

            foreach (int index in selected)
            {
                Entity meshEntity = subMeshes[index].m_SubMesh;
                if (meshEntity == Entity.Null) continue;

                string meshName = GetPrefabName(meshEntity) ?? $"{item.AssetId}_sub{index}";
                string meshAssetId = RipAssetIds.SanitizeAssetId(meshName);

                MeshAsset mesh = ReadMesh(meshEntity, meshAssetId, meshName);
                if (mesh == null) continue;

                // Node names must be unique within the file; two entries pointing at the same
                // render prefab (a mirrored pair, say) would otherwise collide.
                //
                string nodeName = meshAssetId;
                for (int suffix = 2; !nodeNames.Add(nodeName); suffix++) nodeName = $"{meshAssetId}_{suffix}";

                PrefabMeshPart part = new()
                {
                    Mesh = mesh,
                    Position = subMeshes[index].m_Position,
                    Rotation = subMeshes[index].m_Rotation,
                    NodeName = nodeName,
                    StackRole = PrefabMesh.RoleOf(subMeshes[index].m_Flags)
                };

                // The obsolete-content placeholder — the box a prefab gets when its asset went
                // missing — is not drawn at its authored size. The renderer stretches it over
                // `ObjectGeometryData.m_Bounds` (`BatchDataSystem.UpdateObjectData`,
                // `DefaultMissingMesh` branch: translate to the bounds' centre, scale to half
                // their size), so the exported node carries the same TRS.
                //
                if (((subMeshes[index].m_Flags & SubMeshFlags.DefaultMissingMesh) != 0)
                    && EntityManager.HasComponent<ObjectGeometryData>(item.PrefabEntity))
                {
                    Colossal.Mathematics.Bounds3 bounds = EntityManager.GetComponentData<ObjectGeometryData>(item.PrefabEntity).m_Bounds;
                    part.Position += (bounds.min + bounds.max) * 0.5f;
                    part.Scale = (bounds.max - bounds.min) * 0.5f;
                    PlaceholderParts++;
                }

                prefab.Parts.Add(part);

                // A part whose MeshData carries `MeshFlags.Base` draws one extra generated batch:
                // the foundation skirt between its floor and the terrain. `MeshSystem` sets the
                // flag for a render prefab declaring `BaseProperties`, and
                // `ObjectInitializeSystem` grants it to every building mesh even without one, so
                // the ECS state — not the managed component — is the authoritative gate. The
                // silhouette scan itself runs on the writer task; here only the inputs are
                // gathered, including the base surface, which must be resolved on the main thread
                // while the material window is open.
                //
                if (EntityManager.HasComponent<MeshData>(meshEntity))
                {
                    MeshData meshData = EntityManager.GetComponentData<MeshData>(meshEntity);
                    if ((meshData.m_State & MeshFlags.Base) != 0)
                    {
                        skirt ??= new SkirtWork
                        {
                            AssetId = $"{item.AssetId}__base",
                            SourceName = item.SourceName,
                            InstanceCount = item.InstanceCount
                        };

                        skirt.Parts.Add(new SkirtPart
                        {
                            Source = mesh,
                            BaseOffset = ((meshData.m_State & MeshFlags.MinBounds) != 0) ? meshData.m_Bounds.min.y : 0f,
                            Position = part.Position,
                            Rotation = part.Rotation,
                            NodeName = $"{nodeName}__base",
                            MaterialId = ResolveBaseMaterial(meshEntity)
                        });
                    }
                }
            }

            return (prefab.Parts.Count > 0) ? prefab : null;
        }

        /// <summary>
        /// Resolve the surface a part's foundation skirt draws with: surface 0 of the render
        /// prefab's <c>BaseProperties.m_BaseType</c>, or of the game-wide default base mesh when
        /// the prefab declares none (<c>ManagedBatchSystem.CreateBatch</c>, the
        /// <c>GeneratedType.ObjectBase</c> branch).
        /// </summary>
        /// <param name="meshEntity">The part's mesh entity.</param>
        /// <returns>The bundle material id.</returns>
        private string ResolveBaseMaterial(Entity meshEntity)
        {
            RenderPrefab baseType = null;

            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase)
                && (prefabBase is RenderPrefab renderPrefab))
            {
                BaseProperties properties = renderPrefab.GetComponent<BaseProperties>();
                if ((properties != null) && (properties.m_BaseType != null)) baseType = properties.m_BaseType;
            }

            if ((baseType == null) && (_meshSettingsQuery.CalculateEntityCount() == 1))
            {
                MeshSettingsData settings = _meshSettingsQuery.GetSingleton<MeshSettingsData>();
                if ((settings.m_DefaultBaseMesh != Entity.Null)
                    && EntityManager.HasComponent<PrefabData>(settings.m_DefaultBaseMesh)
                    && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(settings.m_DefaultBaseMesh), out PrefabBase defaultBase))
                    baseType = defaultBase as RenderPrefab;
            }

            string materialId = ((baseType != null) && Instance.MaterialRip.IsRipping)
                              ? Instance.MaterialRip.Resolve(baseType, 0) : null;

            if (string.IsNullOrEmpty(materialId))
            {
                materialId = "base_default_mat0";
                if (Instance.MaterialRip.IsRipping)
                    Instance.MaterialRip.Declare(materialId, "base surface did not resolve");
            }

            return materialId;
        }

        /// <summary>
        /// Every <c>Require*</c> bit, i.e. every flag that makes a sub-mesh conditional.
        /// </summary>
        private const SubMeshFlags RequireFlags =
            SubMeshFlags.RequireSafe | SubMeshFlags.RequireLevelCrossing | SubMeshFlags.RequireChild
            | SubMeshFlags.RequireTeen | SubMeshFlags.RequireAdult | SubMeshFlags.RequireElderly
            | SubMeshFlags.RequireDead | SubMeshFlags.RequireStump | SubMeshFlags.RequireEmpty
            | SubMeshFlags.RequireFull | SubMeshFlags.RequireEditor | SubMeshFlags.RequireClear
            | SubMeshFlags.RequireTrack | SubMeshFlags.RequireLeftHandTraffic
            | SubMeshFlags.RequireRightHandTraffic | SubMeshFlags.RequirePartial1
            | SubMeshFlags.RequirePartial2 | SubMeshFlags.RequireForward | SubMeshFlags.RequireBackward;

        /// <summary>
        /// The state a ripped prefab is exported in.
        /// A <c>SubMesh</c> buffer mixes two different things: parts drawn <i>together</i> (a
        /// building and its roof units) and <i>alternatives</i> the game picks between at draw time
        /// (a sapling, an adult tree, a dead tree and a stump are four entries on one tree prefab).
        /// Exporting all of them would stack every alternative into the same file. One state is
        /// therefore fixed and documented for the prefab's own file: a healthy adult, unloaded,
        /// undamaged, forward-facing. Traffic side is not a choice — it is read from the city.
        /// The alternatives an instance can actually be in — see <see cref="StateVariants"/> — get
        /// a derived file each rather than being lost.
        /// </summary>
        public static SubMeshFlags DefaultState => SubMeshFlags.RequireSafe | SubMeshFlags.RequireAdult
            | SubMeshFlags.RequireEmpty | SubMeshFlags.RequireClear | SubMeshFlags.RequireForward
            | (Instance.City.leftHandTraffic ? SubMeshFlags.RequireLeftHandTraffic : SubMeshFlags.RequireRightHandTraffic);

        /// <summary>
        /// Whether one sub-mesh entry belongs in the exported state.
        /// Public and static so <see cref="InstanceRipSystem"/> can apply the identical test when
        /// deciding whether a stacked prefab has untiled rest parts — the two layers must agree on
        /// which entries exist or the <c>__stack_rest</c> reference dangles.
        /// </summary>
        /// <param name="flags">The entry's flags.</param>
        /// <returns>Whether the entry is exported.</returns>
        public static bool ExportsSubMesh(SubMeshFlags flags) => ExportsSubMesh(flags, DefaultState);

        /// <summary>
        /// Whether one sub-mesh entry belongs in a given exported state.
        /// Mirrors the renderer's own test — <c>BatchInstanceSystem.BatchInstanceJob</c> line 522
        /// keeps an entry when <c>(subMesh.m_Flags &amp; state) == subMesh.m_Flags</c>, i.e. the
        /// state must satisfy every bit the entry asks for.
        /// </summary>
        /// <param name="flags">The entry's flags.</param>
        /// <param name="state">The state being exported.</param>
        /// <returns>Whether the entry is exported.</returns>
        private static bool ExportsSubMesh(SubMeshFlags flags, SubMeshFlags state)
            => ((flags & SubMeshFlags.OutlineOnly) == 0) && (((flags & RequireFlags) & ~state) == 0);

        /// <summary>
        /// One alternative state exported as a derived file alongside the prefab's own.
        /// The renderer picks a tree's silhouette from <c>Tree.m_State</c> and a stockpile's from
        /// <c>Quantity.m_Fullness</c> (<c>BatchDataHelpers.CalculateTreeSubMeshData</c> /
        /// <c>CalculateQuantitySubMeshData</c>), and a prefab that authors those alternatives loses
        /// them entirely when only <see cref="DefaultState"/> ships — every dead tree in the city
        /// draws in full leaf. Each variant is a <b>complete replacement</b> for the base file, not
        /// an overlay: its selection also picks up every unconditional part the prefab draws
        /// alongside the swapped one.
        /// </summary>
        private struct StateVariant
        {
            /// <summary>The suffix the derived <c>assetId</c> takes.</summary>
            public string Suffix;

            /// <summary>The flag an entry must carry for this variant to be authored at all.</summary>
            public SubMeshFlags Required;

            /// <summary>The default-state flag this variant stands in for, cleared from the state.</summary>
            public SubMeshFlags Replaces;

            /// <summary>Whether this is a tree state rather than a stockpile fullness step.</summary>
            public bool Tree;

            /// <summary>The state a sub-mesh entry is tested against for this variant.</summary>
            public SubMeshFlags State => (DefaultState & ~Replaces) | Required;
        }

        /// <summary>
        /// Every alternative state shipped as a derived file.
        /// The states the base file already covers are absent by construction: a healthy adult tree
        /// and an empty stockpile <i>are</i> <see cref="DefaultState"/>, so they need no suffix.
        /// <para>
        /// All four non-adult tree states ship (audit §5.1, P18). <c>Teen</c> and <c>Elderly</c>
        /// were left out when <c>__dead</c>/<c>__stump</c> were added, on the reasoning that the
        /// growth steps could wait — but <c>CalculateTreeSubMeshData</c> returns a distinct
        /// sub-mesh requirement for every one of the five states, and the two that were skipped are
        /// the two the city is full of: 119,237 Elderly and 33,581 Teen against 1,173 Dead and no
        /// Stumps at all, so 59.5% of stated trees were drawing in a silhouette the game would not
        /// draw. <c>RequireChild</c> is deliberately still absent, and is not a fourth row: the
        /// game's own switch has no Child case — a child tree is a <c>Teen</c> entry below growth
        /// 128, scaled by <c>m_ChildSize / m_TeenSize</c>, which the per-instance
        /// <c>growthScale</c> already carries.
        /// </para>
        /// </summary>
        private static readonly StateVariant[] StateVariants =
        {
            new() { Suffix = "__teen", Required = SubMeshFlags.RequireTeen, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__elderly", Required = SubMeshFlags.RequireElderly, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__dead", Required = SubMeshFlags.RequireDead, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__stump", Required = SubMeshFlags.RequireStump, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__partial1", Required = SubMeshFlags.RequirePartial1, Replaces = SubMeshFlags.RequireEmpty },
            new() { Suffix = "__partial2", Required = SubMeshFlags.RequirePartial2, Replaces = SubMeshFlags.RequireEmpty },
            new() { Suffix = "__full", Required = SubMeshFlags.RequireFull, Replaces = SubMeshFlags.RequireEmpty }
        };

        /// <summary>
        /// Choose which sub-mesh entries belong in the exported file.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <returns>The indices to export.</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes) => SelectSubMeshes(subMeshes, DefaultState, true);

        /// <summary>
        /// Choose which sub-mesh entries belong in one exported state.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <param name="state">The state being exported.</param>
        /// <param name="fallback">Whether an empty selection falls back to the first entry — right for the prefab's own file, wrong for a derived one, where an empty selection simply means the variant does not exist.</param>
        /// <returns>The indices to export.</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes, SubMeshFlags state, bool fallback)
        {
            List<int> selected = new();

            for (int i = 0; i < subMeshes.Length; i++)
            {
                // An `OutlineOnly` entry is never world geometry. The game clears every layer but
                // `Outline` for it (`BatchInstanceJob.UpdateObjectInstances` does
                // `layers &= ~Outline; if (OutlineOnly) layers &= Outline;`), so outside a tool's
                // selection highlight its layer mask is empty and it draws nothing at all. It is
                // the silhouette shell a highlight is drawn from, and exporting it buries the real
                // mesh inside a second copy of itself.
                //
                // Unconditional entries are always drawn; conditional ones only when everything
                // they ask for holds in the exported state.
                //
                if (!ExportsSubMesh(subMeshes[i].m_Flags, state)) continue;

                // The game replaces a part's batch layer with its `m_DefaultLayers` when set
                // (`UpdateObjectInstances`), so a part defaulting only to Pipeline/SubPipeline —
                // utility hookup furniture inside an otherwise visible prefab — never draws in
                // the world view. The per-part form of the §17.5 rule, which drops whole
                // instances when *every* part is such.
                //
                if (EntityManager.HasComponent<MeshData>(subMeshes[i].m_SubMesh))
                {
                    const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
                    MeshLayer layers = EntityManager.GetComponentData<MeshData>(subMeshes[i].m_SubMesh).m_DefaultLayers;
                    if ((layers != 0) && ((layers & ~underground) == 0))
                    {
                        PipelinePartsSkipped++;
                        continue;
                    }
                }

                selected.Add(i);
            }

            // A prefab whose every entry is conditional — some props exist only as state variants —
            // would otherwise produce no file at all, breaking the contract's promise that every
            // referenced assetId resolves. Fall back to its first entry.
            //
            // The fallback skips outline shells for the same reason the loop does: falling back to
            // one would hand the bundle a silhouette in place of the object.
            //
            if (fallback && (selected.Count == 0))
            {
                for (int i = 0; i < subMeshes.Length; i++)
                {
                    if ((subMeshes[i].m_Flags & SubMeshFlags.OutlineOnly) != 0) continue;
                    selected.Add(i);
                    break;
                }
            }

            return selected;
        }

        /// <summary>
        /// Record every prefab that produced no mesh in the bundle's <c>warnings.jsonl</c>.
        /// A skipped prefab leaves an <c>assetId</c> that <c>instances.jsonl</c> still references
        /// with no <c>.glb</c> behind it. The contract treats an unresolvable reference as invalid,
        /// and rightly — but the honest fix is to <i>declare</i> the gap, not to hide it by dropping
        /// the instances, which would cost their placement for nothing.
        /// Appends, because <see cref="InstanceRipSystem"/> owns the first pass over this file.
        /// Ripping instances before meshes is the order the bundle is assembled in.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void WriteMeshWarnings(RipRun run)
        {
            // No early return on an empty list: a rip that closed every gap still has to clear the
            // declarations the previous one left, or the bundle keeps claiming losses it no longer
            // has.

            // This stage's OutputDirectory is `meshes/`; the layer sits at the bundle root, one
            // level above it.
            string root = System.IO.Path.GetDirectoryName(run.OutputDirectory);

            List<Dictionary<string, object>> records = new(run.SkippedAssets.Count);
            foreach (RipWarning skipped in run.SkippedAssets)
            {
                records.Add(WarningsFile.ByAsset(skipped.Kind, skipped.AssetId, skipped.Reason));
            }

            int kept = WarningsFile.Rewrite(
                root,
                line => WarningsFile.IsKind(line, WarningsFile.MeshKinds),
                records);
            if (kept < 0) return;

            run.Report.AppendLine($"Recorded {run.SkippedAssets.Count} gap(s) in {WarningsFile.FileName}, "
                                + $"keeping {kept} from the instance pass");
        }

        /// <summary>
        /// Write the report, clear the indicator and tell the user what came out.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void Finish(RipRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            bool writerFaulted = false;
            try
            {
                run.Writer.Wait();
            }
            catch (Exception ex)
            {
                writerFaulted = true;
                _log.Warn($"Mesh writer ended with an error: {ex}");
            }

            while (run.WriterNotes.TryDequeue(out string note)) run.Report.AppendLine(note);

            // Materials last: the table can only name a texture by the content hash of the file it
            // became, so it is written once the encoder has drained.
            //
            string materials = null;
            try
            {
                materials = Instance.MaterialRip.Finish(run.Report, run.SkippedAssets);
            }
            catch (Exception ex)
            {
                _log.Error($"Material rip failed to finish: {ex}");
            }

            WriteMeshWarnings(run);

            // Sizes are the point of a capped run: they are what says whether the uncapped rip is
            // 3 GB or 30. Report the total, the mean and the worst offenders.
            //
            List<KeyValuePair<string, long>> sizes = new(run.Sizes);
            sizes.Sort((a, b) => b.Value.CompareTo(a.Value));

            run.Report.AppendLine();
            run.Report.AppendLine($"Wrote {run.Written} .glb, skipped {run.Skipped}, failed {run.Failed}, in {run.Clock.Elapsed.TotalSeconds:F1} s.");

            // The rules the game applies that a vanilla city may never exercise — counted so a rip
            // states how often each fired instead of leaving them unobservable.
            //
            if (MultiMeshPrefabs > 0) run.Report.AppendLine($"Merged multi-mesh geometry assets: {MultiMeshPrefabs} prefab(s).");
            if (PartialMeshPrefabs > 0) run.Report.AppendLine($"Prefabs that lost meshes their geometry asset holds: {PartialMeshPrefabs} — see the log.");
            if (PipelinePartsSkipped > 0) run.Report.AppendLine($"Skipped {PipelinePartsSkipped} underground-only sub-mesh entr(ies) inside otherwise visible prefabs (Pipeline layers).");
            if (PlaceholderParts > 0) run.Report.AppendLine($"Missing-asset placeholder parts stretched to prefab bounds: {PlaceholderParts}.");
            if (StackRestParts > 0) run.Report.AppendLine($"Stacked prefabs with untiled rest parts (__stack_rest): {StackRestParts}.");
            if (TreeStateVariants > 0) run.Report.AppendLine($"Tree-state variant files (__teen/__elderly/__dead/__stump): {TreeStateVariants}; join on attrs.treeState.");
            if (FullnessVariants > 0) run.Report.AppendLine($"Stockpile fullness variant files (__partial1/__partial2/__full): {FullnessVariants}; join on attrs.fullness — the empty state is the base file.");
            if ((run.SkirtsWritten > 0) || (run.SkirtsEmpty > 0))
                run.Report.AppendLine($"Foundation skirts (__base): {run.SkirtsWritten} file(s); "
                                    + $"{run.SkirtsEmpty} Base-flagged prefab(s) presented no silhouette at the base level.");
            run.Report.AppendLine($"Total {run.Bytes / (1024d * 1024d):F1} MiB, mean {(run.Written > 0 ? run.Bytes / (double)run.Written / 1024d : 0d):F0} KiB per prefab.");
            run.Report.AppendLine("Largest:");
            for (int i = 0; (i < 15) && (i < sizes.Count); i++)
                run.Report.AppendLine($"  {sizes[i].Value / 1024d:F0} KiB  {sizes[i].Key}");
            run.Report.AppendLine($"Output: {run.OutputDirectory}");

            _log.Info(run.Report.ToString());

            StringBuilder summary = new();

            // A writer that died after the reader finished lost queued files, so the stage must
            // not read as a success — the outcome this builds carries `Failed`, and the word in
            // the text is for the person reading the report, not for the orchestrator.
            //
            if (writerFaulted) summary.AppendLine("FAILED: the mesh writer ended with an error — meshes/ is incomplete; see the log.");
            summary.AppendLine($"Wrote {run.Written} mesh file(s), skipped {run.Skipped}, failed {run.Failed}.");
            summary.AppendLine($"{run.Bytes / (1024d * 1024d):F1} MiB in {run.Clock.Elapsed.TotalSeconds:F0} s.");
            if (!string.IsNullOrEmpty(materials)) summary.AppendLine(materials);
            summary.AppendLine();
            summary.Append("Full report: Logs/Carto.log");

            // A pipeline-driven run reports into the pipeline's combined summary; only a run the
            // user started by hand gets its own dialog.
            RipReport.Deliver(
                run.Completed,
                writerFaulted || (run.Failed > 0)
                    ? RipOutcome.Failure(summary.ToString())
                    : RipOutcome.Ok(summary.ToString()),
                summary.ToString(),
                "mesh rip",
                _log);
        }

        /// <summary>
        /// Read full-detail geometry through the render prefab's <c>GeometryAsset</c>.
        /// </summary>
        /// <param name="meshEntity">The sub-mesh (render prefab) entity.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <param name="renderPrefab">The render prefab behind the entity.</param>
        /// <param name="trace">Optional diagnostics sink.</param>
        /// <returns>The mesh, or <c>null</c> to fall back to the ECS buffers.</returns>
        private MeshAsset TryReadGeometryAsset(string assetId, string sourceName, RenderPrefab renderPrefab, List<string> trace)
        {
            try
            {
                if (renderPrefab == null)
                {
                    trace?.Add("geometry asset: entity does not resolve to a RenderPrefab");
                    return null;
                }

                if (!renderPrefab.hasGeometryAsset)
                {
                    trace?.Add("geometry asset: render prefab has none");
                    return null;
                }

                Colossal.IO.AssetDatabase.GeometryAsset asset = renderPrefab.geometryAsset;
                trace?.Add($"geometry asset: meshCount={renderPrefab.meshCount} vertexCount={renderPrefab.vertexCount} indexCount={renderPrefab.indexCount} materialCount={renderPrefab.materialCount}");

                // The asset's accessors stay empty until its data is resident, and there is no
                // public "ensure loaded" call — ObtainMeshes is the load trigger. Always release
                // again: a city has thousands of buildings and holding every mesh would balloon.
                //
                UnityEngine.Mesh[] loaded = null;
                try
                {
                    try
                    {
                        loaded = asset.ObtainMeshes(true);
                        trace?.Add($"geometry asset: ObtainMeshes returned {(loaded?.Length ?? 0)} mesh(es)");
                    }
                    catch (Exception ex)
                    {
                        trace?.Add($"geometry asset: ObtainMeshes failed — {ex.GetType().Name} {ex.Message}");
                    }

                    // Which count to trust: the asset's own file header, not the prefab's
                    // serialized `m_MeshCount`. They agree on vanilla content (`MeshSystem`
                    // warns whenever meshCount != materialCount and a vanilla load logs none),
                    // but the header is what `CreateMeshes` actually iterates, and after
                    // `ObtainMeshes` it is resident — so it is the ground truth at zero cost.
                    //
                    int meshCount = renderPrefab.meshCount;
                    try { if (asset.meshCount > 0) meshCount = asset.meshCount; }
                    catch (Exception) { /* header not resident; the prefab's declared count is the best available */ }

                    if (trace != null)
                        foreach (GeometryAssetReader.MeshInfo info in GeometryAssetReader.Describe(asset, meshCount))
                            trace.Add($"  {info}");

                    // `ObtainMeshes` loads the CPU-side data only when no instances exist yet.
                    // For a prefab the game is rendering right now — every drawn lane mesh, for
                    // one — `m_Instances` is already non-null, the call returns the live meshes
                    // without touching `m_Data`, and every native accessor then throws
                    // `ArgumentOutOfRangeException` off `data.meshCount == 0`. (Its
                    // `keepReadable` parameter is dead in this game build — the decompiled body
                    // never reads it.) That is not a failed read, it is a path choice: skip the
                    // native accessors entirely and read the live Unity meshes, which
                    // `CreateMeshes` built readable. A full-city rip warned 16 times over
                    // exactly this and every one of the 16 files was fine.
                    //
                    bool dataResident = asset.data.IsValid;
                    if (!dataResident)
                    {
                        trace?.Add("geometry asset: CPU data not resident (the game holds live instances); reading the Unity meshes");
                        _log.Debug($"`{assetId}`: geometry data not resident (live instances held by the game); reading Unity meshes instead.");
                    }

                    // Every mesh of the asset, not the richest one. The meshes are simultaneous
                    // parts — LODs live behind LodProperties as separate render prefabs — and the
                    // game binds surface i to the i-th (mesh, sub-mesh) pair in flat order
                    // (`RenderPrefab.ObtainMesh`), which is exactly the ordinal the reader assigns.
                    // Picking one mesh dropped its siblings and, off the first slot, mis-assigned
                    // their surfaces.
                    //
                    int meshesMerged = 0;
                    MeshAsset merged = dataResident
                        ? GeometryAssetReader.ReadAll(asset, meshCount, assetId, sourceName, out meshesMerged)
                        : null;
                    if (merged != null)
                    {
                        if (meshesMerged > 1) MultiMeshPrefabs++;
                        if (meshesMerged < meshCount)
                        {
                            // The meshes of one asset draw together, so coming up short is lost
                            // geometry, not a variant choice — counted, named in the log, and
                            // declared in warnings.jsonl: the .glb exists but is incomplete,
                            // which a consumer cannot see from the file alone (contract §10).
                            //
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: geometry asset holds {meshCount} mesh(es) but only {meshesMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {meshesMerged} of {meshCount} geometry-asset meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: merged {meshesMerged} of {meshCount} mesh(es) ({merged.TriangleCount} tris, {merged.Primitives.Count} primitives)");
                        return merged;
                    }

                    // Native accessors still empty: fall back to the loaded Unity meshes, merged
                    // under the same flat ordinal.
                    //
                    MeshAsset viaUnity = GeometryAssetReader.ReadUnityMeshes(loaded, assetId, sourceName, out int unityMerged);
                    if (viaUnity != null)
                    {
                        if (unityMerged > 1) MultiMeshPrefabs++;
                        if ((loaded != null) && (unityMerged < loaded.Length))
                        {
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: {loaded.Length} loaded Unity mesh(es) but only {unityMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {unityMerged} of {loaded.Length} Unity meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: via {unityMerged} Unity mesh(es) ({viaUnity.TriangleCount} tris, {viaUnity.Primitives.Count} primitives)");
                        return viaUnity;
                    }

                    trace?.Add("geometry asset: no readable meshes after loading");
                    return null;
                }
                finally
                {
                    try { asset.ReleaseMeshes(); } catch { /* nothing useful to do if release fails */ }
                }
            }
            catch (Exception ex)
            {
                trace?.Add($"geometry asset: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Geometry asset unavailable for `{assetId}`: {ex.Message}");
                return null;
            }
        }

        /// <summary>
        /// Find the entity that actually holds geometry buffers, starting from a sub-mesh entity.
        /// A render prefab entity is a container: it carries <c>MeshData</c>, a <c>LodMesh</c>
        /// buffer of per-LOD entities and a <c>SharedMeshData</c> pointer, but frequently no
        /// vertices of its own. Geometry hangs off one of those references, so follow them —
        /// <c>SharedMeshData</c> first, then LOD 0 — until a real vertex buffer turns up.
        /// </summary>
        /// <param name="entity">The entity to start from.</param>
        /// <param name="geometry">The entity holding the geometry buffers.</param>
        /// <param name="lod">The LOD level reached, <c>0</c> for full detail.</param>
        /// <param name="trace">Optional sink recording each hop, for diagnostics.</param>
        /// <param name="depth">The current recursion depth.</param>
        /// <returns>Whether geometry was found.</returns>
        private bool TryResolveGeometry(Entity entity, out Entity geometry, out int lod, List<string> trace, int depth = 0)
        {
            List<GeometryCandidate> candidates = new();
            Collect(entity, 0, "root", candidates, new HashSet<Entity>(), 0);

            if (trace != null)
            {
                trace.Add($"container {entity.Index} [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, entity))}]");
                if (EntityManager.HasComponent<MeshData>(entity))
                {
                    MeshData data = EntityManager.GetComponentData<MeshData>(entity);
                    // What the container *claims* the mesh holds. If the richest candidate falls
                    // well short of m_IndexCount, we are reading a reduced LOD, not the real mesh.
                    //
                    trace.Add($"MeshData: indexCount={data.m_IndexCount} subMeshCount={data.m_SubMeshCount} minLod={data.m_MinLod} state={data.m_State}");
                }
                foreach (GeometryCandidate candidate in candidates)
                    trace.Add($"candidate via {candidate.Path}: entity {candidate.Entity.Index}, {candidate.VertexCount} verts, {candidate.IndexCount / 3} tris, materials={candidate.MaterialCount} [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, candidate.Entity))}]");
            }

            if (candidates.Count == 0)
            {
                geometry = Entity.Null;
                lod = 0;
                return false;
            }

            // Pick the richest candidate. LOD 0 is by definition the one with the most geometry,
            // and picking by index count avoids depending on buffer ordering being detail-ordered.
            //
            GeometryCandidate best = candidates[0];
            foreach (GeometryCandidate candidate in candidates)
                if (candidate.IndexCount > best.IndexCount) best = candidate;

            geometry = best.Entity;
            lod = best.Lod;
            trace?.Add($"chosen: {best.Path} ({best.IndexCount / 3} tris)");
            return true;
        }

        /// <summary>
        /// A reachable entity that carries real geometry, and how it was reached.
        /// </summary>
        private struct GeometryCandidate
        {
            public Entity Entity;
            public int Lod;
            public string Path;
            public int VertexCount;
            public int IndexCount;
            public int MaterialCount;
        }

        /// <summary>
        /// Walk every geometry reference reachable from <paramref name="entity"/>, collecting each
        /// entity that carries non-empty vertex and index buffers.
        /// A render prefab entity is a container carrying <c>MeshData</c>, a <c>LodMesh</c> buffer
        /// and a <c>SharedMeshData</c> pointer, but often no vertices of its own. Taking the first
        /// match found risks silently settling for a reduced LOD, so gather all of them and let the
        /// caller choose.
        /// </summary>
        private void Collect(Entity entity, int lod, string path, List<GeometryCandidate> candidates, HashSet<Entity> visited, int depth)
        {
            if ((entity == Entity.Null) || (depth > 4) || !visited.Add(entity)) return;

            if (EntityManager.HasBuffer<MeshVertex>(entity) && EntityManager.HasBuffer<MeshIndex>(entity))
            {
                int vertexCount = EntityManager.GetBuffer<MeshVertex>(entity, true).Length;
                int indexCount = EntityManager.GetBuffer<MeshIndex>(entity, true).Length;
                if ((vertexCount > 0) && (indexCount > 0))
                {
                    candidates.Add(new GeometryCandidate
                    {
                        Entity = entity,
                        Lod = lod,
                        Path = path,
                        VertexCount = vertexCount,
                        IndexCount = indexCount,
                        MaterialCount = EntityManager.HasBuffer<MeshMaterial>(entity)
                            ? EntityManager.GetBuffer<MeshMaterial>(entity, true).Length : 0
                    });
                }
            }

            if (EntityManager.HasComponent<SharedMeshData>(entity))
                Collect(EntityManager.GetComponentData<SharedMeshData>(entity).m_Mesh, lod, $"{path}/shared", candidates, visited, depth + 1);

            if (EntityManager.HasBuffer<LodMesh>(entity))
            {
                DynamicBuffer<LodMesh> lods = EntityManager.GetBuffer<LodMesh>(entity, true);
                for (int i = 0; i < lods.Length; i++)
                    Collect(lods[i].m_LodMesh, i, $"{path}/lod{i}", candidates, visited, depth + 1);
            }
        }

        /// <summary>
        /// Read one mesh entity's geometry into a <see cref="MeshAsset"/>.
        /// </summary>
        /// <param name="meshEntity">The mesh (render prefab) entity.</param>
        /// <param name="assetId">The sanitised asset id.</param>
        /// <param name="sourceName">The original prefab name.</param>
        /// <returns>The mesh, or <c>null</c> when the entity carries no geometry.</returns>
        public MeshAsset ReadMesh(Entity meshEntity, string assetId, string sourceName, List<string> trace = null)
        {
            // The render prefab is both where the authored geometry lives and where the material
            // slots live, so resolve it once and let both readers share it.
            //
            RenderPrefab renderPrefab = null;
            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase))
                renderPrefab = prefabBase as RenderPrefab;

            // Prefer the geometry asset: the ECS buffers only ever hold the reduced LOD the game
            // keeps CPU-side for raycasting, and carry no sub-mesh ranges at all.
            //
            MeshAsset fromAsset = TryReadGeometryAsset(assetId, sourceName, renderPrefab, trace);
            if (fromAsset != null)
            {
                AssignMaterials(fromAsset, renderPrefab);
                TryFlattenDecal(fromAsset, renderPrefab, trace);
                return fromAsset;
            }

            if (!TryResolveGeometry(meshEntity, out Entity geometryEntity, out int lod, trace)) return null;

            DynamicBuffer<MeshVertex> vertices = EntityManager.GetBuffer<MeshVertex>(geometryEntity, true);
            DynamicBuffer<MeshIndex> indices = EntityManager.GetBuffer<MeshIndex>(geometryEntity, true);

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertices.Length],
                Indices = new int[indices.Length]
            };

            for (int i = 0; i < vertices.Length; i++) mesh.Vertices[i] = vertices[i].m_Vertex;
            for (int i = 0; i < indices.Length; i++) mesh.Indices[i] = indices[i].m_Index;

            if (EntityManager.HasBuffer<MeshNormal>(geometryEntity))
            {
                DynamicBuffer<MeshNormal> normals = EntityManager.GetBuffer<MeshNormal>(geometryEntity, true);
                if (normals.Length == vertices.Length)
                {
                    mesh.Normals = new float3[normals.Length];
                    for (int i = 0; i < normals.Length; i++) mesh.Normals[i] = normals[i].m_Normal;
                }
            }

            if (EntityManager.HasBuffer<MeshUV0>(geometryEntity))
            {
                DynamicBuffer<MeshUV0> uvs = EntityManager.GetBuffer<MeshUV0>(geometryEntity, true);
                if (uvs.Length == vertices.Length)
                {
                    mesh.Uvs = new float2[uvs.Length];
                    for (int i = 0; i < uvs.Length; i++) mesh.Uvs[i] = uvs[i].m_Uv;
                }
            }

            if (EntityManager.HasBuffer<MeshTangent>(geometryEntity))
            {
                DynamicBuffer<MeshTangent> tangents = EntityManager.GetBuffer<MeshTangent>(geometryEntity, true);
                if (tangents.Length == vertices.Length)
                {
                    mesh.Tangents = new float4[tangents.Length];
                    for (int i = 0; i < tangents.Length; i++) mesh.Tangents[i] = tangents[i].m_Tangent;
                }
            }

            if (EntityManager.HasBuffer<MeshMaterial>(geometryEntity))
            {
                // The material slot is the buffer *position*, never the stored `m_MaterialIndex`.
                // Nothing in the game ever writes authored ranges into this buffer; its one writer
                // is `MeshSystem.OnUpdate`, which runs at prefab creation, resizes the buffer to
                // one entry per surface asset *in order*, stores a session-global library id in
                // `m_MaterialIndex`, and leaves the range fields uninitialised. The game itself
                // resolves a surface from this buffer by position (`ManagedBatchSystem.CreateBatch`
                // matches the global id and then calls `GetSurfaceAsset(position)`), so position
                // is the only meaning the slot has — and the global id, used positionally, would
                // pick an arbitrary surface. The garbage ranges are dropped by the range check in
                // `GltfWriter.NormalizePrimitives` / `NetworkRipSystem.AppendPieceIndices`.
                //
                DynamicBuffer<MeshMaterial> materials = EntityManager.GetBuffer<MeshMaterial>(geometryEntity, true);
                for (int i = 0; i < materials.Length; i++)
                {
                    MeshMaterial material = materials[i];
                    mesh.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = material.m_StartIndex,
                        IndexCount = material.m_IndexCount,
                        StartVertex = material.m_StartVertex,
                        VertexCount = material.m_VertexCount,
                        MaterialIndex = i,
                        MaterialId = $"{assetId}_mat{i}"
                    });
                }
            }

            if (EntityManager.HasComponent<MeshData>(meshEntity))
            {
                MeshData data = EntityManager.GetComponentData<MeshData>(meshEntity);
                mesh.BoundsMin = data.m_Bounds.min;
                mesh.BoundsMax = data.m_Bounds.max;
            }

            AssignMaterials(mesh, renderPrefab);
            TryFlattenDecal(mesh, renderPrefab, trace);
            return mesh;
        }

        /// <summary>
        /// Replace a decal's projector volume with the quad it actually paints.
        /// <b>Why the volume alone is useless.</b> A decal's mesh is not its artwork — it is the
        /// box the game projects artwork down through, and every variant shares one box. Measured
        /// in a real city: <c>NA_RoadArrow_Forward</c>, <c>NA_RoadArrow_Forward_Left90</c> and
        /// <c>NA_RoadArrow_Left180</c> are byte-identical 3 × 1 × 6 m boxes with byte-identical
        /// UVs and one shared material, and their mesh UVs are a default box unwrap — six faces
        /// across six unrelated atlas rects, one of them with a zero normal. Nothing in the mesh
        /// says which arrow it is.
        /// <b>What does say.</b> <c>DecalProperties.m_TextureArea</c> — a <c>Bounds2</c> the game
        /// hands the shader as <c>_TextureArea</c> alongside <c>_MeshSize</c> and
        /// <c>_DecalLayerMask</c> (see <c>ManagedBatchSystem.CreateBatch</c> and
        /// <c>RenderPrefabRenderer.Instance.SetDecalProperties</c>). It is the sub-rect of the
        /// atlas this prefab draws, and the arrow sheet is a 8 × 4 grid of 0.125 × 0.25 cells whose
        /// aspect matches the 3 × 6 m footprint exactly. Baking that rect across the footprint
        /// turns a shared box into the marking it stands for, and shrinks the mesh from 24
        /// vertices to 4.
        /// The quad sits at the volume's mid-height, not its floor: a projector is centred on the
        /// surface it paints so that it catches ground a little above and below, so the midpoint is
        /// the best estimate of the carriageway and the floor would bury the marking half a metre
        /// under it.
        /// </summary>
        /// <param name="mesh">The mesh to rewrite in place.</param>
        /// <param name="renderPrefab">The render prefab that may carry decal properties.</param>
        /// <param name="trace">Optional sink recording the decision.</param>
        /// <returns>Whether the mesh was replaced.</returns>
        /// <summary>
        /// The import pipeline's own up-facing vertex test (`AssetImportPipeline.
        /// SetupDecalComponent`): a real normal (`SqrMagnitude > 0.01`) within 0.01° of straight
        /// up. cos(0.01°) ≈ 0.99999998.
        /// </summary>
        private static bool IsExactlyUp(float3 normal)
        {
            float lengthSq = math.lengthsq(normal);
            if (lengthSq <= 0.01f) return false;
            return (normal.y / math.sqrt(lengthSq)) >= 0.99999998f;
        }

        private bool TryFlattenDecal(MeshAsset mesh, RenderPrefab renderPrefab, List<string> trace)
        {
            if ((mesh == null) || (renderPrefab == null)) return false;
            if ((mesh.Vertices == null) || (mesh.Vertices.Length == 0)) return false;

            DecalProperties decal;
            try
            {
                if (!renderPrefab.TryGet(out decal) || (decal == null) || !decal.active) return false;
            }
            catch (Exception ex)
            {
                trace?.Add($"decal: properties did not resolve — {ex.GetType().Name} {ex.Message}");
                return false;
            }

            float3 min = mesh.Vertices[0], max = mesh.Vertices[0];
            for (int i = 1; i < mesh.Vertices.Length; i++)
            {
                min = math.min(min, mesh.Vertices[i]);
                max = math.max(max, mesh.Vertices[i]);
            }

            // A volume with no footprint projects nothing; leaving it alone keeps a degenerate
            // prefab visible as itself rather than as a zero-area quad.
            //
            if (!((max.x - min.x) > 1e-4f) || !((max.z - min.z) > 1e-4f)) return false;

            float y = (min.y + max.y) * 0.5f;

            float3[] corners =
            {
                new float3(min.x, y, min.z), new float3(max.x, y, min.z),
                new float3(max.x, y, max.z), new float3(min.x, y, max.z)
            };

            // The UV PAIRING is taken from the authored mesh, never rebuilt from `m_TextureArea`
            // alone: reconstruction from the bounds has to guess which way `u` and `v` run
            // across the quad. An earlier version guessed `+x → +u` and pre-flipped `v`; the
            // guess was mirrored, and every road-marking text in the city read backwards
            // ("TAXI" as "IXAT", a backwards S in "BUS"), while the pre-flip cancelled the
            // writer's Unity→glTF flip and cropped some markings to the wrong band of their
            // atlas outright. Sampling each corner's nearest up-facing authored vertex keeps
            // orientation exactly as the game renders it, and the writer's usual flip applies
            // as it does to every other mesh. (The RECT is a different matter — see below.)
            //
            // The same up-facing selection the game's derivation uses; a projector volume whose
            // top face carries no upward normal (none measured, but authored content varies)
            // falls back to every vertex rather than shipping an unmapped quad.
            //
            // Pass 0 is the import pipeline's own vertex test (`SetupDecalComponent`:
            // `SqrMagnitude > 0.01` and `Angle(up, n) < 0.01°` — exactly-up, not merely upward),
            // so that for DefaultDecal objects the rect below reproduces `m_TextureArea` by
            // construction and the rescale is a true identity. Pass 1 keeps the previously
            // validated ≥ 0.9 tolerance for authored content the strict test misses; pass 2
            // takes everything rather than shipping an unmapped quad.
            //
            float2[] cornerUvs = new float2[4];
            for (int c = 0; c < 4; c++)
            {
                float best = float.MaxValue;
                for (int pass = 0; (pass < 3) && (best == float.MaxValue); pass++)
                {
                    for (int i = 0; i < mesh.Vertices.Length; i++)
                    {
                        if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                            && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;

                        float2 offset = mesh.Vertices[i].xz - corners[c].xz;
                        float distance = math.lengthsq(offset);
                        if (distance < best)
                        {
                            best = distance;
                            cornerUvs[c] = ((mesh.Uvs != null) && (i < mesh.Uvs.Length)) ? mesh.Uvs[i] : default;
                        }
                    }
                }
            }

            // The authored pairing gives the ORIENTATION; `m_TextureArea` gives the RECT. The
            // shader never samples the mesh UVs of a decal — it is handed `_TextureArea`
            // (ManagedBatchSystem.CreateBatch) and maps the projector footprint into that rect.
            // For DefaultDecal imports the pipeline derives the area from these same UVs, so the
            // rescale below is the identity and the road-marking text keeps rendering exactly as
            // validated. But the LANE decal parts — centre lines, crosswalks, lane lines — ship
            // authored box UVs that all crop one generic, mostly-transparent slab of the LaneLine
            // atlas ([0.625, 0.25]–[0.875, 0.5], measured: mean α 27/255), while their prefabs'
            // `m_TextureArea` selects the actual opaque marking band. Keeping the authored rect
            // is why every crosswalk and centre line in the first full import was invisible.
            // Rescaling the authored rect into the area keeps the pairing (the mirrored-text
            // lesson above) and takes the crop from where the game actually takes it.
            //
            float2 rectMin = new(float.MaxValue), rectMax = new(float.MinValue);
            for (int pass = 0; (pass < 3) && (rectMin.x == float.MaxValue); pass++)
            {
                for (int i = 0; i < mesh.Vertices.Length; i++)
                {
                    if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                        && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;
                    if ((mesh.Uvs == null) || (i >= mesh.Uvs.Length)) continue;

                    rectMin = math.min(rectMin, mesh.Uvs[i]);
                    rectMax = math.max(rectMax, mesh.Uvs[i]);
                }
            }

            float2 rectSize = rectMax - rectMin;
            float2 areaMin = decal.m_TextureArea.min;
            float2 areaSize = decal.m_TextureArea.max - decal.m_TextureArea.min;
            bool remapped = false;
            if (math.all(rectSize > 1e-6f) && math.all(areaSize > 0f)
                && (math.any(math.abs(rectMin - areaMin) > 1e-4f)
                    || math.any(math.abs(rectSize - areaSize) > 1e-4f)))
            {
                for (int c = 0; c < 4; c++)
                {
                    cornerUvs[c] = areaMin + (((cornerUvs[c] - rectMin) / rectSize) * areaSize);
                }
                remapped = true;
            }

            mesh.Vertices = corners;
            mesh.Uvs = cornerUvs;

            mesh.Normals = new[] { math.up(), math.up(), math.up(), math.up() };
            mesh.Tangents = null;

            // Same rule the network layer's generated strips use: take the winding the quad
            // actually produces rather than assume one, and reverse it when it faces the ground.
            //
            bool flip = math.cross(mesh.Vertices[1] - mesh.Vertices[0],
                                   mesh.Vertices[2] - mesh.Vertices[0]).y < 0f;

            mesh.Indices = flip ? new[] { 0, 2, 1, 0, 3, 2 } : new[] { 0, 1, 2, 0, 2, 3 };

            string materialId = ((mesh.Primitives.Count > 0) && !string.IsNullOrEmpty(mesh.Primitives[0].MaterialId))
                ? mesh.Primitives[0].MaterialId : $"{mesh.AssetId}_mat0";

            mesh.Primitives.Clear();
            mesh.Primitives.Add(new MeshPrimitive
            {
                StartIndex = 0,
                IndexCount = 6,
                StartVertex = 0,
                VertexCount = 4,
                MaterialIndex = 0,
                MaterialId = materialId
            });

            mesh.BoundsMin = new float3(min.x, y, min.z);
            mesh.BoundsMax = new float3(max.x, y, max.z);

            trace?.Add($"decal: flattened to a {max.x - min.x:F2} × {max.z - min.z:F2} m quad at y {y:F2}, "
                     + $"corner uvs [{cornerUvs[0].x:F4}, {cornerUvs[0].y:F4}]–[{cornerUvs[2].x:F4}, {cornerUvs[2].y:F4}]"
                     + (remapped ? $" (authored rect rescaled into textureArea [{areaMin.x:F4}, {areaMin.y:F4}]+"
                                 + $"[{areaSize.x:F4}, {areaSize.y:F4}])" : ""));
            return true;
        }

        /// <summary>
        /// Give every primitive of a freshly read mesh a material id that resolves.
        /// The real id comes from the render prefab's surface assets, which are ripped — textures
        /// and all — the first time each is seen. Where a surface does not resolve, the synthetic
        /// <c>&lt;assetId&gt;_mat&lt;n&gt;</c> id stays and is <i>declared</i> in the material table
        /// as an entry with no maps. Neither dropping the material nor leaving the name dangling is
        /// acceptable: §6 of the contract requires a glTF material's name to be a key in
        /// <c>materials.json</c>, so an undeclared placeholder is exactly the unresolvable
        /// reference the validator is there to catch.
        /// </summary>
        /// <param name="mesh">The mesh whose primitives to relabel.</param>
        /// <param name="renderPrefab">The render prefab holding the surface assets.</param>
        private void AssignMaterials(MeshAsset mesh, RenderPrefab renderPrefab)
        {
            if (mesh == null) return;

            // A mesh the game reported no drawable ranges for still draws with slot 0, and glTF
            // still needs one primitive covering the whole index buffer. Making that explicit here
            // rather than leaving it to the writer is what lets its material be declared at all —
            // the writer runs on a background task, long after the material table is reachable.
            //
            if ((mesh.Primitives.Count == 0) && (mesh.Indices != null) && (mesh.Indices.Length >= 3))
            {
                mesh.Primitives.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = mesh.Indices.Length - (mesh.Indices.Length % 3),
                    StartVertex = 0,
                    VertexCount = mesh.Vertices?.Length ?? 0,
                    MaterialIndex = 0,
                    MaterialId = $"{mesh.AssetId}_mat0"
                });
            }

            if (!Instance.MaterialRip.IsRipping) return;

            foreach (MeshPrimitive primitive in mesh.Primitives)
            {
                string materialId = Instance.MaterialRip.Resolve(renderPrefab, primitive.MaterialIndex);

                if (!string.IsNullOrEmpty(materialId)) primitive.MaterialId = materialId;
                else Instance.MaterialRip.Declare(primitive.MaterialId,
                    (renderPrefab == null) ? "no render prefab behind the mesh entity"
                                           : $"surface asset {primitive.MaterialIndex} did not resolve");
            }
        }

        /// <summary>
        /// Resolve a prefab entity's name, or <c>null</c> when it cannot be resolved.
        /// </summary>
        /// <param name="entity">The prefab entity.</param>
        /// <returns>The prefab name.</returns>
        private string GetPrefabName(Entity entity)
        {
            if (!EntityManager.HasComponent<PrefabData>(entity)) return null;
            return Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(entity), out PrefabBase prefab) ? prefab?.name : null;
        }



        /// <summary>
        /// Whether an object is the node furniture of a buried utility network, and so is not part
        /// of the built environment the bundle describes.
        /// Every road lays hidden water, sewage and electricity along itself, as <i>marker</i>
        /// networks the player never places or sees, and each of their nodes hangs a prop off
        /// itself: <c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>.
        /// A full city has tens of thousands — the counts give it away, ~7,300 water pipe nodes
        /// against the 105 pipe segments a player actually built. The props themselves are ordinary
        /// objects, not <see cref="Marker"/>s, so the query's marker exclusion never reaches
        /// them.
        /// <b>The owner must be a <c>Game.Net.Marker</c>.</b> A real high-voltage line's nodes are
        /// not markers, and its pylons are node props exactly like these, so testing the network
        /// type alone would delete every pylon in the city.
        /// <b>Buildings hang the same furniture with themselves as owner</b>, so the owner test
        /// alone is not the whole story: every lot's water, sewage and cable connection nodes have
        /// the building as <c>Owner</c> and no marker net anywhere in the chain. Those are caught
        /// by <see cref="IsUndergroundOnlyPrefab"/> — the game's own draw-layer rule — which is
        /// checked first.
        /// <b>For the owner test itself, one exception.</b> An earlier version also required
        /// the owner's prefab to carry <c>PipelineData</c> or <c>PowerLineData</c>, which is what
        /// let the buried nodes through: those two components come only from
        /// <c>PipelinePrefab</c> and <c>PowerLinePrefab</c>
        /// (<c>GetPrefabComponents</c>), and the marker nets a road lays along itself are not
        /// those classes. The extra clause could only ever narrow a test that was already exact —
        /// <c>Game.Net.Marker</c> is added by <c>MarkerNet.GetArchetypeComponents</c> and by
        /// nothing else in the game, to the node archetype and the edge archetype alike, so an
        /// owner carrying it <i>is</i> a marker net by construction.
        /// <b>The exception is taxiways</b>, which are marker nets and yet are genuinely drawn —
        /// <see cref="NetworkRipSystem"/> already makes the same exception. They are named directly
        /// by <c>TaxiwayData</c>, which <c>TaxiwayPrefab.GetPrefabComponents</c> is the only source
        /// of, rather than indirectly by listing the network types that are not taxiways.
        /// <b>Both rip systems must apply this identically.</b> The asset ids are assigned over the
        /// prefab set as a whole, so a set that differs between the two layers can shift a
        /// disambiguating suffix and break the join — see <see cref="BuildAssetIds"/>.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether to leave it out of the bundle.</returns>
        public static bool IsUtilityMarkerSubObject(EntityManager entityManager, Entity entity)
        {
            // The owner test alone proved incomplete in the field: a full-city rip still carried
            // 29,648 pipe and cable node props whose owner is the *building* whose lot spawns
            // them, not a marker net — buildings hang their own utility connection furniture as
            // sub-objects, and those owners carry no `Game.Net.Marker`. The prefab test below is
            // the game's own draw rule and catches both parents.
            //
            if (IsUndergroundOnlyPrefab(entityManager, entity)) return true;

            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if ((owner == Entity.Null) || !entityManager.HasComponent<Game.Net.Marker>(owner)) return false;
            if (!entityManager.HasComponent<PrefabRef>(owner)) return false;

            Entity ownerPrefab = entityManager.GetComponentData<PrefabRef>(owner).m_Prefab;
            if (ownerPrefab == Entity.Null) return false;

            // Drawn, despite being a marker net. The only such case the game has.
            //
            return !entityManager.HasComponent<TaxiwayData>(ownerPrefab);
        }

        /// <summary>
        /// Whether an object is cargo riding a vehicle — a log pile on a truck trailer, crates on
        /// a flatbed — rather than scenery standing in the world.
        /// The queries already exclude vehicles and creatures themselves, but cargo props are
        /// separate <i>sub-object</i> entities owned by the vehicle, and nothing about the prop
        /// itself says "agent": it is an ordinary object with a transform and a prefab. The tell is
        /// its <c>Owner</c> — the same one-level link the utility-marker test reads. A full-city rip
        /// carried ~590 such records (<c>TruckTrailer03_LogPile01</c>, <c>CarTrailer01_Props01</c>, …)
        /// as world scenery before this test existed.
        /// Every vehicle archetype carries <c>Game.Vehicles.Vehicle</c>
        /// (<c>VehiclePrefab.GetArchetypeComponents</c> adds it unconditionally, and
        /// <c>CarTrailerPrefab</c> derives from it through <c>CarBasePrefab</c>), so the single
        /// component test covers trailers too; <c>CarTrailer</c> is tested as well only so the rule
        /// reads as what it means and survives an archetype reshuffle.
        /// <b>Both rip systems must apply this identically</b>, for the same asset-id join reason as
        /// <see cref="IsUtilityMarkerSubObject"/>.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether to leave it out of the bundle.</returns>
        public static bool IsVehicleCargoSubObject(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if (owner == Entity.Null) return false;

            return entityManager.HasComponent<Game.Vehicles.Vehicle>(owner)
                || entityManager.HasComponent<Game.Vehicles.CarTrailer>(owner);
        }

        /// <summary>
        /// Whether an object's prefab draws only on the underground utility layers, and so is never
        /// visible in normal rendering.
        /// The game's rule, from <c>BatchInstanceSystem.UpdateObjectInstances</c>: a sub-mesh whose
        /// <c>MeshData.m_DefaultLayers</c> is non-zero has its batch layer replaced by those
        /// defaults, and <c>Pipeline</c>/<c>SubPipeline</c> map to camera layers only the
        /// underground cutaway view renders. A prefab all of whose sub-meshes live there — the
        /// pipe and cable connection nodes — therefore draws nothing in the world view, exactly
        /// like the Pipeline-layer lane parts the lane catalogue already excludes. A prefab with
        /// even one ordinary sub-mesh keeps drawing and is kept: the test is every-part, not
        /// any-part.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The placed object.</param>
        /// <returns>Whether every sub-mesh is underground-only.</returns>
        private static bool IsUndergroundOnlyPrefab(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<PrefabRef>(entity)) return false;

            Entity prefab = entityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            if ((prefab == Entity.Null) || !entityManager.HasBuffer<SubMesh>(prefab)) return false;

            DynamicBuffer<SubMesh> subMeshes = entityManager.GetBuffer<SubMesh>(prefab, true);
            if (subMeshes.Length == 0) return false;

            const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
            foreach (SubMesh subMesh in subMeshes)
            {
                if (!entityManager.HasComponent<MeshData>(subMesh.m_SubMesh)) return false;

                MeshLayer layers = entityManager.GetComponentData<MeshData>(subMesh.m_SubMesh).m_DefaultLayers;
                if ((layers == 0) || ((layers & ~underground) != 0)) return false;
            }

            return true;
        }
    }
}
