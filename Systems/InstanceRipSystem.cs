using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// Which instance kinds a rip covers — the "show advanced" split of the ripper tab's
    /// <b>Buildings, props &amp; trees</b> toggle. The kinds are the contract's closed
    /// <c>kind</c> vocabulary; the mesh rip filters by the same mask so the two layers stay 1:1.
    /// Asset ids are still assigned over the <i>whole</i> prefab set regardless of the mask, so a
    /// filtered rip's ids match an unfiltered one's and partial re-rips keep joining up.
    /// </summary>
    public readonly struct RipKindFilter
    {
        public readonly bool Buildings, Props, Trees, Decals;

        public RipKindFilter(bool buildings, bool props, bool trees, bool decals)
        {
            Buildings = buildings;
            Props = props;
            Trees = trees;
            Decals = decals;
        }

        /// <summary>Everything — the dev buttons' and the default plan's mask.</summary>
        public static RipKindFilter All => new(true, true, true, true);

        /// <summary>Whether any kind is included at all.</summary>
        public bool Any => Buildings || Props || Trees || Decals;

        /// <summary>Whether no kind is excluded — the mask under which filtering is a no-op.</summary>
        public bool Everything => Buildings && Props && Trees && Decals;

        /// <summary>Whether records of this contract kind are ripped.</summary>
        public bool Allows(string kind) => kind switch
        {
            "building" => Buildings,
            "tree" => Trees,
            "decal" => Decals,
            _ => Props
        };
    }

    /// <summary>
    /// The system that enumerates every placed object in the world into <c>instances.jsonl</c>.
    /// Unlike mesh extraction, this carries no feasibility risk: a transform is plain ECS component
    /// data. Full placement is therefore guaranteed even where a mesh cannot be read.
    /// <b>Sub-objects need no recursion.</b> <c>Game.Objects.SubObject</c> holds only an
    /// <c>Entity</c>, and that entity carries its own world-space <c>Transform</c> — street trees
    /// on a road and landscaping on a lot are ordinary entities. A single query over
    /// <c>Object + Transform + PrefabRef</c> therefore reaches all of them, and recursing the
    /// buffers as well would double-count. <c>Attached.m_Parent</c> still supplies the contract's
    /// <c>parent</c> link.
    /// </summary>
    public partial class InstanceRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed, static objects.
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// What a prefab's meshes are, as far as decals are concerned.
        /// </summary>
        private readonly struct DecalInfo
        {
            /// <summary>Whether every mesh it draws is a decal.</summary>
            public bool IsDecal { get; }

            /// <summary>
            /// Which surfaces the game projects onto, across all of its decal meshes.
            /// </summary>
            public Game.Rendering.DecalLayers Layers { get; }

            /// <summary>
            /// How tall the projector volume was before it was flattened to its painted quad.
            /// The mesh layer ships the quad, which is what anyone wants to draw; this is the one
            /// number that flattening discards, and it is what a consumer needs to re-project the
            /// marking onto a surface that is not flat.
            /// </summary>
            public float Height { get; }

            /// <summary>
            /// <c>DecalProperties.m_RendererPriority</c> — the game's own sort key for two decals
            /// projecting onto the same surface, highest last (audit §57).
            /// <para>A decal prefab carries no blend mode at all: blending is the shader
            /// template's, projection targets are <see cref="Layers"/>, and ORDER is this. It is
            /// the only thing the game uses to decide which of two overlapping markings wins.</para>
            /// <para><b>And on a real city it says almost nothing — measured the day it shipped
            /// (audit §58).</b> On the 2026-08-17 22:25 rip this reads <b>0 on 17,721 of 17,722
            /// decals</b>, with a single instance at 2. Ship it anyway: it is the game's own
            /// answer, "absent" is now distinguishable from "zero", and a modded or DLC asset may
            /// use the range. But do NOT expect it to order
            /// <c>csiibundle.decals.LIFT_LADDER</c>'s `instance_decal` rung — there is no
            /// per-decal ordering here to port. Areas are the opposite case and are why the other
            /// half of that ladder could be ported: <c>RenderedArea.m_RendererPriority</c> takes
            /// <b>13 distinct values from -122 to 42</b> on the same city.</para>
            /// <c>int.MinValue</c> when the prefab carries no decal properties at all.
            /// </summary>
            public int Priority { get; }

            public DecalInfo(bool isDecal, Game.Rendering.DecalLayers layers, float height,
                             int priority = int.MinValue)
            {
                IsDecal = isDecal;
                Layers = layers;
                Height = height;
                Priority = priority;
            }
        }

        /// <summary>
        /// Which prefabs draw nothing but decals, and onto what, cached for one rip.
        /// </summary>
        readonly Dictionary<Entity, DecalInfo> _decalPrefabs = new();

        /// <summary>
        /// Counters for the resolved-paint and growth-scale attributes, per rip.
        /// </summary>
        int _colorSets, _customColors, _divergentColorSets, _growthScales;

        /// <summary>
        /// Counters for the semantic attributes (audit P9/P12), per rip.
        /// </summary>
        int _customNames, _brands, _levels, _zones, _themes, _addresses, _fullness;

        /// <summary>
        /// What a zone prefab exports as (name and coarse class), cached for one rip —
        /// a city has dozens of zone prefabs and hundreds of thousands of zoned buildings.
        /// </summary>
        readonly Dictionary<Entity, (string Zone, string Zoning)> _zoneTypes = new();

        /// <summary>
        /// Which theme each object prefab belongs to, cached for one rip.
        /// </summary>
        readonly Dictionary<Entity, string> _prefabThemes = new();

        /// <summary>
        /// The game's name system — the custom-name table <c>attrs.name</c> reads.
        /// </summary>
        Game.UI.NameSystem _nameSystem;

        /// <summary>
        /// The per-entity component reads the walk repeats over the whole city, hoisted into
        /// lookups the way every other collector in this repo does its per-entity work.
        /// <b>The instance walk is main-thread component reads and nothing else</b> — the audit
        /// measured 57.6 s of it on the full spike city with the writer never once the
        /// bottleneck, ten to twenty <c>EntityManager.HasComponent</c>/<c>GetComponentData</c>
        /// calls per entity across 1.27 M entities. Each of those resolves the type index and the
        /// entity's chunk from scratch; a <see cref="ComponentLookup{T}"/> caches the type and
        /// hands back a typed accessor. Only the probes that fire on <i>every</i> entity are here:
        /// the semantic attributes added later (level, zone, brand, address, theme, fullness) fire
        /// on subsets and stay on <c>EntityManager</c>, where they read more plainly.
        /// Compare the next rip report's instance-stage seconds against that 57.6 s baseline.
        /// </summary>
        ComponentLookup<PrefabRef> _prefabRefs;
        ComponentLookup<Game.Objects.Transform> _transforms;
        ComponentLookup<Game.Common.Owner> _owners;
        ComponentLookup<Attached> _attachments;
        ComponentLookup<Game.Buildings.Building> _buildings;
        ComponentLookup<Tree> _trees;
        ComponentLookup<Plant> _plants;
        ComponentLookup<Game.Common.Overridden> _overridden;
        ComponentLookup<Game.Objects.Elevation> _elevations;
        ComponentLookup<Game.Objects.Stack> _stacks;
        BufferLookup<Game.Rendering.MeshColor> _meshColors;
        BufferLookup<Game.Rendering.CustomMeshColor> _customMeshColors;
        BufferLookup<SubMesh> _prefabSubMeshes;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            // Shared with the mesh pass, and shared on purpose — see RipQueries.WorldObjects for
            // what each exclusion is for and why the two layers cannot be allowed to disagree.
            _objectQuery = GetEntityQuery(RipQueries.WorldObjects());

            _nameSystem = World.GetOrCreateSystemManaged<Game.UI.NameSystem>();

            _prefabRefs = GetComponentLookup<PrefabRef>(true);
            _transforms = GetComponentLookup<Game.Objects.Transform>(true);
            _owners = GetComponentLookup<Game.Common.Owner>(true);
            _attachments = GetComponentLookup<Attached>(true);
            _buildings = GetComponentLookup<Game.Buildings.Building>(true);
            _trees = GetComponentLookup<Tree>(true);
            _plants = GetComponentLookup<Plant>(true);
            _overridden = GetComponentLookup<Game.Common.Overridden>(true);
            _elevations = GetComponentLookup<Game.Objects.Elevation>(true);
            _stacks = GetComponentLookup<Game.Objects.Stack>(true);
            _meshColors = GetBufferLookup<Game.Rendering.MeshColor>(true);
            _customMeshColors = GetBufferLookup<Game.Rendering.CustomMeshColor>(true);
            _prefabSubMeshes = GetBufferLookup<SubMesh>(true);

            base.OnCreate();
            _log.Debug("InstanceRipSystem instance created.");
        }

        /// <summary>
        /// Refresh every hoisted lookup against the current frame's world state.
        /// A lookup holds a safety handle and a cached chunk index, both of which go stale once
        /// structural change happens — which it does constantly while a rip runs beside a live
        /// simulation — so this is called before each slice, and once more before the synchronous
        /// pre-pass that runs ahead of the first slice.
        /// </summary>
        private void UpdateLookups()
        {
            // A lookup read on the main thread is not synchronised for us the way an
            // `EntityManager` read is, and the walk now reaches its first lookup before it reaches
            // any `EntityManager` call. Settle the jobs touching these types once per slice.
            //
            CompleteDependency();

            _prefabRefs.Update(this);
            _transforms.Update(this);
            _owners.Update(this);
            _attachments.Update(this);
            _buildings.Update(this);
            _trees.Update(this);
            _overridden.Update(this);
            _plants.Update(this);
            _elevations.Update(this);
            _stacks.Update(this);
            _meshColors.Update(this);
            _customMeshColors.Update(this);
            _prefabSubMeshes.Update(this);
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// A run in flight holds a persistent native array and an open file — both must be
        /// released when the world unloads mid-rip.
        /// </summary>
        protected override void OnDestroy()
        {
            InstanceRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Instance rip abandoned at {run.Index}/{run.Entities.Length} object(s); the world was unloaded.");
                Abandon(run);
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Advance any rip in progress by one frame's worth of work.
        /// </summary>
        protected override void OnUpdate()
        {
            InstanceRun run = _run;
            if (run == null) return;

            UpdateLookups();
            Advance(run);
        }

        /// <summary>
        /// One instance rip in flight.
        /// </summary>
        private class InstanceRun
        {
            /// <summary>The bundle root, and the record file being written.</summary>
            public string OutputDirectory, Path;

            /// <summary>The placed objects to walk, held across frames (persistent allocator).</summary>
            public NativeArray<Entity> Entities;

            /// <summary>
            /// The two exclusion verdicts, decided once in the pre-pass and indexed by position in
            /// <see cref="Entities"/>.
            /// The pre-pass has to run both tests over every entity to build the asset-id table,
            /// and the walk then needed the same two answers again — 2.6 M redundant probes, each
            /// one an owner read plus a prefab read plus a sub-mesh buffer scan. Caching a bit
            /// each is the whole fix; the walk's own test order is unchanged.
            /// </summary>
            public NativeBitArray ExcludedUtility, ExcludedCargo;

            /// <summary>How far the walk has got.</summary>
            public int Index;

            /// <summary>The record writer, touched only by <see cref="WriterTask"/> once the run starts.</summary>
            public InstanceWriter Writer;

            /// <summary>
            /// Records waiting to be serialised, and the task draining them. Newtonsoft
            /// serialisation dominates the pass — several times the cost of the ECS reads — so
            /// keeping it inside the frame budget multiplied the wall time by the frame rate;
            /// off-thread it costs the main thread nothing (the mesh rip's own pattern).
            /// </summary>
            public System.Collections.Concurrent.BlockingCollection<InstanceRecord> Queue;
            public System.Threading.Tasks.Task WriterTask;

            /// <summary>Hands one record to the writer; held once so the per-entity path allocates no closure.</summary>
            public Action<InstanceRecord> Emit;

            /// <summary>Whether the walk has handed over its last record.</summary>
            public bool ReadingDone;

            /// <summary>Which kinds this run covers.</summary>
            public RipKindFilter Kinds;

            /// <summary>Records excluded by <see cref="Kinds"/>.</summary>
            public int SkippedFiltered;

            /// <summary>The shared asset-id assignment (see <see cref="BeginRip"/>).</summary>
            public Dictionary<Entity, RipAssetIds.AssetIdEntry> AssetIdTable;

            /// <summary>The distinct asset ids referenced.</summary>
            public HashSet<string> AssetIds = new();

            // The tallies the walk keeps.
            public Dictionary<string, int> ByKind = new();
            public int SkippedNoPrefab, WithParent, SkippedUtilityNodes, SkippedVehicleCargo;
            public Dictionary<Entity, int> UtilityNodesByPrefab = new();
            public Dictionary<Entity, int> VehicleCargoByPrefab = new();
            public int StackExpanded, StackRecords;
            public int ProcessFailed, SkippedStale;

            /// <summary>
            /// The entities behind those two counters. Each one is a record the bundle is
            /// missing — mid-rip churn or a per-entity failure — and §10 wants such gaps
            /// declared in <c>warnings.jsonl</c> rather than left to a log line.
            /// </summary>
            public List<int> StaleEntities = new();
            public List<(int Id, string Error)> FailedEntities = new();
            public List<string> SkipSamples = new();
            public HashSet<int> UnresolvedPrefabs = new();
            public HashSet<string> GeometrylessPrefabs = new();

            /// <summary>The report accumulated as the run goes.</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.</summary>
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// </summary>
            public Action<string> Completed;
        }

        /// <summary>The rip in flight, or <c>null</c>.</summary>
        private InstanceRun _run;

        /// <summary>
        /// Whether a rip is currently running.
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How long the walk may hold the main thread each frame. At least one object is always
        /// processed, so an expensive one cannot stall the run. Higher than the mesh rip's 6 ms:
        /// with serialisation off-thread this is pure component reads, and the rip usually runs
        /// behind a paused options page — the smoke test showed 6 ms stretching a seconds-long
        /// pass into minutes.
        /// </summary>
        private const double FrameBudgetMilliseconds = 12d;

        /// <summary>
        /// How many records may wait for the writer before the walk pauses. Records are small;
        /// this bounds memory without ever making the main thread block on the queue.
        /// </summary>
        private const int QueueBackpressure = 8192;

        /// <summary>
        /// Start enumerating every placed object into <c>instances.jsonl</c>, returning as soon as
        /// the asset-id table is built; the walk itself proceeds a slice per frame in
        /// <see cref="OnUpdate"/> (PLAN Phase 8 — the pass used to block the main thread whole).
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.</param>
        /// <param name="kinds">Which kinds to cover; <c>null</c> for all of them.</param>
        /// <returns>A short description of what was queued.</returns>
        public string BeginRip(string outputDirectory, Action<string> completed = null, RipKindFilter? kinds = null)
        {
            if (_run != null) return "An instance rip is already running.";

            RipProgress.Report(RipProgress.Phase.Scan);
            UpdateLookups();
            _colorSets = 0; _customColors = 0; _divergentColorSets = 0; _growthScales = 0;
            _customNames = 0; _brands = 0; _levels = 0; _zones = 0; _themes = 0; _addresses = 0; _fullness = 0;
            _decalPrefabs.Clear();
            _zoneTypes.Clear();
            _prefabThemes.Clear();

            InstanceRun run = new()
            {
                OutputDirectory = outputDirectory,
                Path = IOUtils.CombinePath(outputDirectory, "instances.jsonl"),
                Entities = _objectQuery.ToEntityArray(Allocator.Persistent),
                Kinds = kinds ?? RipKindFilter.All,
                Completed = completed
            };

            try
            {
                run.Report.AppendLine($"Placed static objects: {run.Entities.Length}");

                // Asset ids have to be assigned over the whole prefab set at once: CS2 does not
                // require prefab names to be unique, and two prefabs sharing an id would have the
                // mesh layer overwrite one with the other. Collect the distinct prefabs first, then
                // assign — MeshRipSystem walks the identical query and so derives identical ids.
                // This pre-pass stays synchronous, like the mesh rip's work-list build: the table
                // must be whole before the first record is written.
                //
                HashSet<Entity> prefabEntities = new();
                Dictionary<Entity, int> prefabUse = new();
                Dictionary<Entity, Entity> prefabSample = new();

                // The verdicts are kept rather than recomputed: ProcessOne asks the identical two
                // questions of the identical entity a few seconds later, and the tests are not
                // cheap ones.
                //
                run.ExcludedUtility = new NativeBitArray(run.Entities.Length, Allocator.Persistent, NativeArrayOptions.ClearMemory);
                run.ExcludedCargo = new NativeBitArray(run.Entities.Length, Allocator.Persistent, NativeArrayOptions.ClearMemory);

                for (int i = 0; i < run.Entities.Length; i++)
                {
                    Entity entity = run.Entities[i];

                    if (MeshRipSystem.IsUtilityMarkerSubObject(EntityManager, entity))
                    {
                        run.ExcludedUtility.Set(i, true);
                        continue;
                    }

                    if (MeshRipSystem.IsVehicleCargoSubObject(EntityManager, entity))
                    {
                        run.ExcludedCargo.Set(i, true);
                        continue;
                    }

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity != Entity.Null)
                    {
                        prefabEntities.Add(prefabEntity);
                        prefabUse.TryGetValue(prefabEntity, out int used);
                        prefabUse[prefabEntity] = used + 1;
                        if (used == 0) prefabSample[prefabEntity] = entity;
                    }
                }

                Census(prefabUse, run.Report);
                Profile(prefabUse, prefabSample, run.Report);

                run.AssetIdTable = RipAssetIds.BuildAssetIds(EntityManager, prefabEntities, out int disambiguated);
                if (disambiguated > 0) run.Report.AppendLine($"Disambiguated {disambiguated} prefab(s) sharing a name.");

                run.Writer = new InstanceWriter(run.Path);
                run.Queue = new System.Collections.Concurrent.BlockingCollection<InstanceRecord>();
                run.Emit = record => run.Queue.Add(record);
                run.WriterTask = System.Threading.Tasks.Task.Run(() =>
                {
                    foreach (InstanceRecord record in run.Queue.GetConsumingEnumerable())
                        run.Writer.Write(record);
                });
            }
            catch
            {
                Abandon(run);
                RipProgress.Clear();
                throw;
            }

            _run = run;
            RipProgress.Report(RipProgress.Phase.Instances, 0, run.Entities.Length);

            return $"Enumerating {run.Entities.Length} placed object(s) in the background.\n\n"
                 + "Close this options page to watch the progress indicator — it appears in the top-right of the game view, not over the menu.";
        }

        /// <summary>
        /// Advance a rip by one frame's budget.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        private void Advance(InstanceRun run)
        {
            if (!run.ReadingDone)
            {
                // A faulted writer never drains the queue: the walk would pause on back-pressure
                // for the rest of the session, ReadingDone would never turn true, and a pipeline
                // driving the rip would wait on this stage forever. Abort the pass instead.
                //
                if (run.WriterTask.IsFaulted)
                {
                    Fail(run);
                    return;
                }

                System.Diagnostics.Stopwatch budget = System.Diagnostics.Stopwatch.StartNew();

                while (run.Index < run.Entities.Length)
                {
                    // Back-pressure: pause the walk rather than pile records up faster than the
                    // writer serialises them.
                    if (run.Queue.Count >= QueueBackpressure) break;

                    // One bad object must not cost the other million their records.
                    //
                    try
                    {
                        ProcessOne(run, run.Index, run.Entities[run.Index]);
                    }
                    catch (Exception ex)
                    {
                        run.ProcessFailed++;
                        run.FailedEntities.Add((run.Entities[run.Index].Index, ex.GetType().Name));
                        if (run.ProcessFailed <= 5) _log.Warn($"Failed to record object entity {run.Entities[run.Index].Index}: {ex}");
                    }

                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
                }

                RipProgress.Report(RipProgress.Phase.Instances, run.Index, run.Entities.Length);

                if (run.Index >= run.Entities.Length)
                {
                    run.ReadingDone = true;
                    run.Queue.CompleteAdding();
                }

                return;
            }

            if (run.WriterTask.IsCompleted) Finish(run);
        }

        /// <summary>
        /// Handle one placed object: classify, resolve, record.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="index">Its position in the snapshot, which the pre-pass's cached verdicts are keyed by.</param>
        /// <param name="entity">The object entity.</param>
        private void ProcessOne(InstanceRun run, int index, Entity entity)
        {
            // The walk holds its snapshot across frames while the simulation may run, so an
            // entity can be gone by the time its turn comes — the same mid-rip churn the mesh
            // and network rips already count. The acceptance rip hit 97 of these as raw NREs.
            //
            if (!EntityManager.Exists(entity))
            {
                run.SkippedStale++;
                run.StaleEntities.Add(entity.Index);
                return;
            }

            // Buried utility-network furniture: excluded here and in the mesh rip alike, so
            // neither its placements nor its meshes reach the bundle. The verdict is the
            // pre-pass's own — the same test on the same entity, decided once.
            //
            if (run.ExcludedUtility.IsSet(index))
            {
                run.SkippedUtilityNodes++;

                // Tallied by prefab, and named in the report. A silent count says the
                // filter ran; it does not say it caught the right things, and the previous
                // version of this filter ran happily while catching none of them. Naming
                // what was dropped is what makes both failures visible — the nodes still
                // getting through, and anything drawn that should not have been dropped.
                //
                Entity nodePrefab = _prefabRefs[entity].m_Prefab;
                if (nodePrefab != Entity.Null)
                {
                    run.UtilityNodesByPrefab.TryGetValue(nodePrefab, out int seen);
                    run.UtilityNodesByPrefab[nodePrefab] = seen + 1;
                }

                return;
            }

            // Cargo riding a vehicle — a log pile on a trailer — is part of the agent it
            // rides, not of the built environment, and the vehicle it belongs to is itself
            // excluded. Same treatment as the utility nodes: dropped in both rip systems
            // alike, tallied by prefab, and named in the report.
            //
            if (run.ExcludedCargo.IsSet(index))
            {
                run.SkippedVehicleCargo++;

                Entity cargoPrefab = _prefabRefs[entity].m_Prefab;
                if (cargoPrefab != Entity.Null)
                {
                    run.VehicleCargoByPrefab.TryGetValue(cargoPrefab, out int seen);
                    run.VehicleCargoByPrefab[cargoPrefab] = seen + 1;
                }

                return;
            }

            Entity prefabEntity = _prefabRefs[entity].m_Prefab;
            if (prefabEntity == Entity.Null)
            {
                run.SkippedNoPrefab++;
                return;
            }

            // The advanced kind filter, before any bookkeeping: an excluded object contributes
            // no record, no warning and no asset id — the bundle describes the selection.
            //
            string kind = ClassifyKind(entity, prefabEntity);
            if (!run.Kinds.Allows(kind))
            {
                run.SkippedFiltered++;
                return;
            }

            // An object whose managed prefab will not resolve is still a real object
            // standing in the city, so keep its placement under a synthetic id rather than
            // dropping it. PLAN §3 is explicit that placement must survive even when the
            // geometry cannot: a consumer substitutes a stand-in mesh and every instance
            // still lands in the right place. Dropping them silently is what breaks the
            // contract's 1:1 fidelity requirement.
            //
            RipAssetIds.AssetIdEntry idEntry = run.AssetIdTable[prefabEntity];
            string assetId = idEntry.AssetId;
            bool unresolved = !idEntry.Resolved;

            // A prefab with no SubMesh buffer has no geometry to rip, and the mesh rip
            // will never say so: its query requires SubMesh, so such a prefab is not
            // enumerated at all and cannot be skipped with a reason. This is the only
            // place both facts are in hand — the reference is being written here, and the
            // prefab entity is right there to ask — so the gap is declared here. Parking
            // lots, helipad airspace, area placeholders and intersections are all real
            // objects with real placements and no mesh of their own.
            //
            if (!unresolved && !_prefabSubMeshes.HasBuffer(prefabEntity))
                run.GeometrylessPrefabs.Add(assetId);

            if (unresolved)
            {
                run.SkippedNoPrefab++;
                run.UnresolvedPrefabs.Add(prefabEntity.Index);

                if (run.SkipSamples.Count < 5)
                {
                    run.SkipSamples.Add($"entity {entity.Index} prefab {prefabEntity.Index} "
                        + $"hasPrefabData={EntityManager.HasComponent<PrefabData>(prefabEntity)} "
                        + $"[{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, entity))}]");
                }
            }

            Game.Objects.Transform transform = _transforms[entity];

            InstanceRecord record = new()
            {
                Id = entity.Index,
                AssetId = assetId,
                Kind = kind,
                Position = transform.m_Position,
                Rotation = transform.m_Rotation
            };

            // Owner is the sub-object relationship (a prop on a lot, a wing of a building).
            // Attached is a different thing — an object bound to a network at a curve
            // position — and using it as the parent link found only 98 of 1.4M objects.
            //
            Entity parent = Entity.Null;
            if (_owners.TryGetComponent(entity, out Game.Common.Owner owner)) parent = owner.m_Owner;
            if ((parent == Entity.Null) && _attachments.TryGetComponent(entity, out Attached attached))
                parent = attached.m_Parent;

            if (parent != Entity.Null)
            {
                record.Parent = parent.Index;
                run.WithParent++;
            }

            AddAttributes(entity, prefabEntity, kind, record);

            // Flag it in the record too, so a consumer can tell a stand-in from a real
            // asset without cross-referencing warnings.jsonl.
            //
            if (unresolved) record.Attributes["unresolved"] = true;

            // A stacked object is not one placement but several, and how many is a property
            // of this instance rather than of its prefab.
            //
            int expanded = ExpandStack(entity, prefabEntity, record, run.Emit, run.AssetIds);
            if (expanded > 0)
            {
                run.StackExpanded++;
                run.StackRecords += expanded;
                run.ByKind.TryGetValue(kind, out int stacked);
                run.ByKind[kind] = stacked + expanded;
                return;
            }

            run.Queue.Add(record);
            run.AssetIds.Add(assetId);
            run.ByKind.TryGetValue(kind, out int count);
            run.ByKind[kind] = count + 1;
        }

        /// <summary>
        /// Close the file, report, and hand the summary to whoever is listening.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void Finish(InstanceRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            bool writerFaulted = false;
            try
            {
                run.WriterTask.Wait();
            }
            catch (Exception ex)
            {
                writerFaulted = true;
                _log.Warn($"Instance writer ended with an error: {ex}");
            }

            StringBuilder report = run.Report;

            // The orchestrator counts a stage failed by its summary's FAILED prefix. A writer
            // that died after the walk finished lost queued records, so the stage must not
            // read as a success.
            //
            if (writerFaulted) report.Insert(0, "FAILED: the instance writer ended with an error — instances.jsonl is incomplete; see the log.\n");
            report.AppendLine($"Wrote {run.Writer.Count} instance(s) to instances.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s"
                            + " (audit P16 baseline for the same walk before the lookup hoist and the cached exclusion"
                            + " verdicts: 57.6 s on the full spike city)");
            if (_colorSets > 0)
            {
                report.AppendLine($"  {_colorSets} instance(s) carry the game's resolved paint (attrs.colorSet), "
                                + $"{_customColors} of them player-painted; "
                                + $"{_divergentColorSets} had per-part sets that differ (first part exported)");
            }
            if (_growthScales > 0) report.AppendLine($"  {_growthScales} tree(s) carry the game's growth scale (attrs.growthScale)");
            if (_customNames + _brands + _levels + _zones + _themes + _addresses + _fullness > 0)
            {
                report.AppendLine($"  semantic attrs (audit P9/P12): {_customNames} custom name(s), {_brands} brand(s), "
                                + $"{_levels} building level(s), {_zones} zoned (zone/zoning), {_themes} themed, "
                                + $"{_addresses} address(es), {_fullness} stockpile fullness");
            }
            if (run.StackExpanded > 0)
            {
                report.AppendLine($"Expanded {run.StackExpanded} stacked object(s) into {run.StackRecords} "
                                + "section record(s) — pillars, walls and fences reach their full height");
            }
            if (run.Writer.NormalizedRotations > 0) report.AppendLine($"Normalised {run.Writer.NormalizedRotations} non-unit rotation(s)");
            if (run.ProcessFailed > 0) report.AppendLine($"FAILED to record {run.ProcessFailed} object(s) — see the log");
            if (run.SkippedStale > 0) report.AppendLine($"Skipped {run.SkippedStale} object(s) the simulation removed mid-rip");
            if (run.SkippedFiltered > 0) report.AppendLine($"Excluded {run.SkippedFiltered} object(s) by the layer selection (advanced kind toggles)");

            int instanceCount = run.Writer.Count;
            Abandon(run);

            foreach (KeyValuePair<string, int> kvp in run.ByKind) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine($"Sub-objects with a parent link: {run.WithParent}");
            if (run.SkippedUtilityNodes > 0)
            {
                report.AppendLine($"Skipped {run.SkippedUtilityNodes} buried utility-network node object(s), "
                                + $"{run.UtilityNodesByPrefab.Count} distinct prefab(s):");

                List<KeyValuePair<Entity, int>> nodes = new(run.UtilityNodesByPrefab);
                nodes.Sort((a, b) => b.Value.CompareTo(a.Value));

                for (int i = 0; (i < 12) && (i < nodes.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(nodes[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {nodes[i].Key.Index}" : name),-40} {nodes[i].Value,8}");
                }

                if (nodes.Count > 12) report.AppendLine($"  … and {nodes.Count - 12} more");
            }
            if (run.SkippedVehicleCargo > 0)
            {
                report.AppendLine($"Skipped {run.SkippedVehicleCargo} cargo prop(s) riding vehicles, "
                                + $"{run.VehicleCargoByPrefab.Count} distinct prefab(s):");

                List<KeyValuePair<Entity, int>> cargo = new(run.VehicleCargoByPrefab);
                cargo.Sort((a, b) => b.Value.CompareTo(a.Value));

                for (int i = 0; (i < 12) && (i < cargo.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(cargo[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {cargo[i].Key.Index}" : name),-40} {cargo[i].Value,8}");
                }

                if (cargo.Count > 12) report.AppendLine($"  … and {cargo.Count - 12} more");
            }
            report.AppendLine($"Distinct assetIds referenced: {run.AssetIds.Count}");
            if (run.SkippedNoPrefab > 0)
            {
                report.AppendLine($"Unresolved prefabs: {run.UnresolvedPrefabs.Count} distinct, {run.SkippedNoPrefab} instance(s) kept under synthetic ids");
                foreach (string sample in run.SkipSamples) report.AppendLine($"  e.g. {sample}");
            }

            // Unconditional, matching the mesh pass: gating this on unresolved prefabs alone
            // shipped geometryless-prefab, stale and per-entity-failure gaps undeclared whenever
            // every prefab happened to resolve — and a rip with nothing to declare still has to
            // clear the declarations a previous one left.
            //
            WriteWarnings(run, report);
            report.AppendLine($"Output: {run.Path}");

            _log.Info(report.ToString());

            if (run.Completed != null)
            {
                try { run.Completed(report.ToString()); }
                catch (Exception ex) { _log.Warn($"Instance rip completion callback failed: {ex}"); }
                return;
            }

            try
            {
                string summary = (writerFaulted ? "FAILED: the writer ended with an error — the file is incomplete.\n" : "")
                               + $"Wrote {instanceCount} instance record(s), {run.AssetIds.Count} distinct assetId(s), "
                               + $"in {run.Clock.Elapsed.TotalSeconds:F1} s.\n\nFull report: Logs/Carto.log";
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary, "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the instance rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Abort a rip whose writer task has died mid-walk. Nothing more can ever be written —
        /// the queue will not drain again — so the only honest outcome is a failed stage:
        /// release what the run holds and hand a summary with the <c>FAILED</c> prefix, which
        /// is what the orchestrator counts a stage failure by, to whoever is listening.
        /// </summary>
        /// <param name="run">The rip whose writer faulted.</param>
        private void Fail(InstanceRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.WriterTask.Exception?.GetBaseException();
            _log.Error($"Instance writer faulted at {run.Index}/{run.Entities.Length} object(s); aborting the pass: {run.WriterTask.Exception}");

            int written = run.Writer?.Count ?? 0;
            Abandon(run);

            string summary = $"FAILED: the instance writer faulted at {run.Index}/{run.Entities.Length} object(s) — "
                           + $"instances.jsonl is incomplete ({written} record(s) before the fault): "
                           + $"{cause?.Message ?? "unknown error"}\n\nFull details: Logs/Carto.log";

            if (run.Completed != null)
            {
                try { run.Completed(summary); }
                catch (Exception ex) { _log.Warn($"Instance rip completion callback failed: {ex}"); }
                return;
            }

            try
            {
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary, "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the instance rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Release what a run holds open — the record file and the entity array.
        /// </summary>
        /// <param name="run">The run to release.</param>
        private static void Abandon(InstanceRun run)
        {
            // Let the writer drain what it holds before the file is closed under it; a run
            // abandoned mid-walk has at most the back-pressure window in flight.
            //
            bool drained = true;
            try { run.Queue?.CompleteAdding(); } catch { /* already completed */ }
            try { drained = run.WriterTask?.Wait(TimeSpan.FromSeconds(10)) ?? true; }
            catch { /* completed with an error — done either way; logged by the Finish paths */ }

            // A writer that has not stopped may still be inside a WriteLine, and closing the
            // stream under it risks a torn record. On timeout the stream is deliberately left
            // to the finalizer instead of being disposed under a live thread.
            //
            if (drained)
            {
                try { run.Writer?.Dispose(); } catch { /* already closed */ }
            }
            else
            {
                _log.Warn("Instance writer still running after 10 s; leaving instances.jsonl open rather than closing it mid-write.");
            }

            run.Writer = null;
            if (run.Entities.IsCreated) run.Entities.Dispose();
            if (run.ExcludedUtility.IsCreated) run.ExcludedUtility.Dispose();
            if (run.ExcludedCargo.IsCreated) run.ExcludedCargo.Dispose();
        }

        /// <summary>
        /// Classify an entity into one of the contract's closed <c>kind</c> values.
        /// </summary>
        /// <param name="entity">The object entity.</param>
        /// <returns>The kind.</returns>
        /// <summary>
        /// Emit one record per section of a stacked object, or <c>0</c> if it is not stacked.
        /// <b>What was wrong without this.</b> CS2 stores a pillar, a high wall or a long fence as
        /// a foot, one repeating section and a cap, and repeats the middle at draw time to span
        /// whatever the instance needs. <c>Vertical_Pillar</c>'s shaft is authored two metres tall;
        /// a viaduct pillar is ten or more. The bundle previously kept the single authored section,
        /// so every stacked object came out as a stub — visibly, highway pillars did not reach the
        /// ground.
        /// <b>The arithmetic is the game's, not a reimplementation.</b>
        /// <c>BatchDataHelpers.CalculateStackSubMeshData</c> is public, so the counts, offsets and
        /// per-section scales come from the same code the renderer uses. Reproducing them from a
        /// reading of the IL would have been a guess at exactly the kind of scaling term that is
        /// easy to get subtly wrong.
        /// Each section becomes its own instance record with its world transform baked, which is
        /// what contract §4.1 already does for sub-objects — so this needs no schema change and no
        /// cooperation from a consumer.
        /// </summary>
        /// <param name="entity">The placed object.</param>
        /// <param name="prefabEntity">Its prefab.</param>
        /// <param name="template">The record built for it, used as the pattern.</param>
        /// <param name="emit">Where records go.</param>
        /// <param name="assetIds">The set of referenced asset ids.</param>
        /// <returns>How many records were written.</returns>
        private int ExpandStack(Entity entity, Entity prefabEntity, InstanceRecord template,
                                Action<InstanceRecord> emit, HashSet<string> assetIds)
        {
            // The first test fires on every object in the city and almost never passes, so it is
            // the one that has to be cheap.
            //
            if (!_stacks.TryGetComponent(entity, out Game.Objects.Stack stack)) return 0;
            if (!EntityManager.HasComponent<Game.Prefabs.StackData>(prefabEntity)) return 0;
            if (!_prefabSubMeshes.HasBuffer(prefabEntity)) return 0;

            Game.Prefabs.StackData data = EntityManager.GetComponentData<Game.Prefabs.StackData>(prefabEntity);

            Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                stack, data, out int3 counts, out float3 offsets, out float3 scales);

            // Which sections this prefab actually has, and where each is authored relative to the
            // object's origin. The buffer is copied because reading it is all we need it for.
            //
            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
            List<(StackRole Role, float3 Position, quaternion Rotation)> sections = new();
            for (int i = 0; i < buffer.Length; i++)
            {
                StackRole? role = PrefabMesh.RoleOf(buffer[i].m_Flags);
                if (role.HasValue) sections.Add((role.Value, buffer[i].m_Position, buffer[i].m_Rotation));
            }

            if (sections.Count == 0) return 0;

            int written = 0;

            // Parts with no stack role are drawn once at the instance transform — the game's
            // sub-mesh test passes them regardless of the `IsStack*` bits — and they live in the
            // `__stack_rest` file the mesh pass ships, because the whole-prefab file is never
            // referenced for a stacked prefab and the stack part files must not tile them.
            //
            bool hasRest = false;
            for (int i = 0; i < buffer.Length; i++)
            {
                if (!PrefabMesh.RoleOf(buffer[i].m_Flags).HasValue
                    && MeshRipSystem.ExportsSubMesh(buffer[i].m_Flags)) { hasRest = true; break; }
            }

            if (hasRest)
            {
                string restId = PrefabMesh.StackRestId(template.AssetId);
                InstanceRecord rest = new()
                {
                    Id = template.Id,
                    AssetId = restId,
                    Kind = template.Kind,
                    Position = template.Position,
                    Rotation = template.Rotation,
                    Parent = template.Parent
                };

                foreach (KeyValuePair<string, object> attribute in template.Attributes)
                    rest.Attributes[attribute.Key] = attribute.Value;

                emit(rest);
                assetIds.Add(restId);
                written++;
            }
            foreach ((StackRole role, float3 localPosition, quaternion localRotation) in sections)
            {
                int tiles = role switch
                {
                    StackRole.First => counts.x,
                    StackRole.Middle => counts.y,
                    _ => counts.z
                };

                Game.Prefabs.SubMeshFlags flags = role switch
                {
                    StackRole.First => Game.Prefabs.SubMeshFlags.IsStackStart,
                    StackRole.Middle => Game.Prefabs.SubMeshFlags.IsStackMiddle,
                    _ => Game.Prefabs.SubMeshFlags.IsStackEnd
                };

                for (int tile = 0; tile < tiles; tile++)
                {
                    float3 position = localPosition;
                    float3 scale = new(1f);
                    Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                        data, offsets, scales, tile, flags, ref position, ref scale);

                    // The section is authored in the object's local frame, so its world placement
                    // is the object's transform applied to it.
                    //
                    string partId = PrefabMesh.StackPartId(template.AssetId, role);
                    InstanceRecord section = new()
                    {
                        Id = template.Id,
                        AssetId = partId,
                        Kind = template.Kind,
                        Position = template.Position + math.mul(template.Rotation, position),
                        Rotation = math.mul(template.Rotation, localRotation),
                        Parent = template.Parent
                    };

                    foreach (KeyValuePair<string, object> attribute in template.Attributes)
                        section.Attributes[attribute.Key] = attribute.Value;

                    // Only where the game scales: an unscaled section must not acquire a scale of
                    // exactly one that a consumer then has to trust.
                    //
                    if (math.any(math.abs(scale - new float3(1f)) > 1e-4f)) section.Scale = scale;

                    emit(section);
                    assetIds.Add(partId);
                    written++;
                }
            }

            return written;
        }

        /// <summary>
        /// Count what the world is made of, by the flags CS2 renders it with.
        /// This exists because "is anything else like the decals?" is a question that should be
        /// answered with numbers rather than by reading names and guessing. A decal's mesh is a
        /// projector volume, not a marking, and nothing in the bundle said so until it was looked
        /// for; the same could be true of impostors, of animated or skinned meshes, or of lights.
        /// Each is one flag, and a census over the prefab set is a few thousand lookups — nothing
        /// against a rip, and it turns the next question into a table.
        /// Counted by instance as well as by prefab: a flag on one prefab used 34,000 times is a
        /// different problem from the same flag on a prefab used twice.
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.
        ///</param>
        /// <param name="report">The report to append the table to.</param>
        /// <summary>
        /// Print, for the prefabs the city holds most of, what a placed one and its prefab are
        /// actually made of.
        /// <b>Why this exists.</b> "Stop these things being ripped" is a filter, and a filter needs
        /// a component to test. Three separate attempts to exclude the buried utility hookups
        /// (<c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>) were
        /// written against components those objects turned out not to have — the last one tested
        /// the <i>owner</i> for <c>Game.Net.Marker</c>, and their owner is the building whose lot
        /// they sit under, not a net at all. Each attempt looked plausible, compiled, ran, reported
        /// a count, and changed nothing.
        /// The list of component names on one instance and on its prefab is what would have ended
        /// that on the first attempt, and it costs two lookups per prefab printed.
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.</param>
        /// <param name="prefabSample">One placed instance per prefab.</param>
        /// <param name="report">The report to append the profile to.</param>
        private void Profile(Dictionary<Entity, int> prefabUse, Dictionary<Entity, Entity> prefabSample,
                             StringBuilder report)
        {
            const int Top = 12;

            List<KeyValuePair<Entity, int>> ranked = new(prefabUse);
            ranked.Sort((a, b) => b.Value.CompareTo(a.Value));

            report.AppendLine($"What the {Math.Min(Top, ranked.Count)} most-placed prefab(s) are made of "
                            + "— the component names a filter can test:");

            for (int i = 0; (i < Top) && (i < ranked.Count); i++)
            {
                Entity prefab = ranked[i].Key;
                string name = Instance.Name.GetPrefabName(prefab);
                report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {prefab.Index}" : name)} × {ranked[i].Value}");

                if (prefabSample.TryGetValue(prefab, out Entity sample))
                    report.AppendLine($"    instance: {string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, sample))}");

                report.AppendLine($"    prefab:   {string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, prefab))}");

                // The owner is where the last filter went wrong, so name what it actually is.
                //
                if (prefabSample.TryGetValue(prefab, out Entity owned)
                    && EntityManager.HasComponent<Game.Common.Owner>(owned))
                {
                    Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(owned).m_Owner;
                    if (owner != Entity.Null)
                    {
                        string ownerName = EntityManager.HasComponent<PrefabRef>(owner)
                            ? Instance.Name.GetPrefabName(EntityManager.GetComponentData<PrefabRef>(owner).m_Prefab)
                            : null;

                        report.AppendLine($"    owner:    {(string.IsNullOrEmpty(ownerName) ? "?" : ownerName)}"
                                        + $" [{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, owner))}]");
                    }
                }
            }
        }

        private void Census(Dictionary<Entity, int> prefabUse, StringBuilder report)
        {
            (string Name, MeshFlags Flag)[] meshFlags =
            {
                ("decal", MeshFlags.Decal), ("impostor", MeshFlags.Impostor),
                ("animated", MeshFlags.Animated), ("skeleton", MeshFlags.Skeleton),
                ("character", MeshFlags.Character), ("prop", MeshFlags.Prop),
                ("stackX", MeshFlags.StackX), ("stackY", MeshFlags.StackY), ("stackZ", MeshFlags.StackZ),

                // The generated foundation skirt (`BaseProperties`): the game draws one extra
                // batch past the last sub-mesh for these, which the bundle does not yet carry —
                // the census is what says how much of the city that omission touches.
                //
                ("base", MeshFlags.Base)
            };

            (string Name, SubMeshFlags Flag)[] subMeshFlags =
            {
                ("outlineOnly", SubMeshFlags.OutlineOnly),
                ("stackStart", SubMeshFlags.IsStackStart),
                ("stackMiddle", SubMeshFlags.IsStackMiddle),
                ("stackEnd", SubMeshFlags.IsStackEnd),
                ("defaultMissingMesh", SubMeshFlags.DefaultMissingMesh)
            };

            Dictionary<string, (int Prefabs, int Instances)> tally = new();

            void Note(string name, int instances)
            {
                tally.TryGetValue(name, out (int Prefabs, int Instances) had);
                tally[name] = (had.Prefabs + 1, had.Instances + instances);
            }

            foreach (KeyValuePair<Entity, int> pair in prefabUse)
            {
                Entity prefab = pair.Key;
                int instances = pair.Value;

                // Light sources are components on the object prefab, not mesh flags: a lamp's
                // housing is ordinary geometry and the light it casts is separate data, carried
                // by lights.json (procedural per sub-mesh, punctual per object prefab).
                //
                // Per-instance colour: the prefab holds a palette of variations
                // (colors.json) and the game resolves each placed object's paint into its
                // `MeshColor` buffer, which `AddAttributes` exports as `attrs.colorSet`.
                //
                // Checked on the object prefab and on its sub-meshes, because which of the two
                // holds the palette decides where a colour export would read from, and guessing
                // is how a whole feature gets built against the wrong entity.
                //
                if (EntityManager.HasBuffer<ColorVariation>(prefab)) Note("color:palette@prefab", instances);

                if (EntityManager.HasBuffer<ProceduralLight>(prefab)) Note("light:procedural", instances);
                if (EntityManager.HasComponent<LightEffectData>(prefab)) Note("light:effect", instances);
                if (EntityManager.HasComponent<StreetLightData>(prefab)) Note("light:street", instances);
                if (EntityManager.HasComponent<TrafficLightData>(prefab)) Note("light:traffic", instances);
                if (EntityManager.HasBuffer<LightAnimation>(prefab)) Note("light:animated", instances);

                if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefab, true);
                MeshFlags meshSeen = 0;
                SubMeshFlags subSeen = 0;
                bool paletteOnMesh = false, lightOnMesh = false;

                for (int i = 0; i < buffer.Length; i++)
                {
                    subSeen |= buffer[i].m_Flags;
                    Entity mesh = buffer[i].m_SubMesh;
                    if (mesh == Entity.Null) continue;

                    if (EntityManager.HasComponent<MeshData>(mesh))
                        meshSeen |= EntityManager.GetComponentData<MeshData>(mesh).m_State;

                    if (EntityManager.HasBuffer<ColorVariation>(mesh)) paletteOnMesh = true;
                    if (EntityManager.HasBuffer<ProceduralLight>(mesh)) lightOnMesh = true;
                }

                if (paletteOnMesh) Note("color:palette@submesh", instances);
                if (lightOnMesh) Note("light:procedural@submesh", instances);

                foreach ((string name, MeshFlags flag) in meshFlags)
                    if ((meshSeen & flag) != 0) Note("mesh:" + name, instances);

                foreach ((string name, SubMeshFlags flag) in subMeshFlags)
                    if ((subSeen & flag) != 0) Note("sub:" + name, instances);
            }

            if (tally.Count == 0) return;

            report.AppendLine($"Flag census over {prefabUse.Count} referenced prefab(s) — "
                            + "what else might need separating:");

            List<KeyValuePair<string, (int Prefabs, int Instances)>> rows = new(tally);
            rows.Sort((a, b) => b.Value.Instances.CompareTo(a.Value.Instances));
            foreach (KeyValuePair<string, (int Prefabs, int Instances)> row in rows)
                report.AppendLine($"  {row.Key,-24} {row.Value.Prefabs,6} prefab(s) {row.Value.Instances,9} instance(s)");
        }

        internal string ClassifyKind(Entity entity, Entity prefabEntity)
        {
            if (_buildings.HasComponent(entity)) return "building";

            // Trees and plants are both vegetation; the contract's closed enum has no separate
            // value for shrubs, and a consumer treats them the same way.
            //
            if (_trees.HasComponent(entity) || _plants.HasComponent(entity)) return "tree";

            if (DecalOf(prefabEntity).IsDecal) return "decal";

            return "prop";
        }

        /// <summary>
        /// Whether a prefab is a decal — a road marking, a parking bay, a patch of grime.
        /// <b>Why this matters more than it looks.</b> A decal's mesh is not the marking, it is the
        /// <i>projector volume</i>: a twelve-triangle box, a metre tall, that CS2's shader projects
        /// the texture down onto whatever road surface lies inside it. A road arrow therefore
        /// arrives in a bundle as a transparent box floating over the carriageway. Left as
        /// <c>kind: "prop"</c>, a consumer building a drivable level turns thousands of those boxes
        /// into collision geometry and the road becomes impassable.
        /// The signal is <c>MeshData.m_State</c>, the same flag the renderer switches on, rather
        /// than the shader-name match the material layer uses. Cached per prefab: a city holds a
        /// million objects and a few thousand prefabs.
        /// </summary>
        /// <b>What it projects onto is the game's own answer, not a guess.</b>
        /// <c>MeshData.m_DecalLayer</c> is a <c>DecalLayers</c> mask — <c>Terrain</c>, <c>Roads</c>,
        /// <c>Buildings</c>, <c>Vehicles</c>, <c>Creatures</c>, <c>Other</c> — naming which surfaces
        /// receive the projection. Without it a consumer projecting "onto whatever is beneath"
        /// paints a road marking across the car parked on it and the wall behind it.
        /// </summary>
        /// <param name="prefabEntity">The object prefab.</param>
        /// <returns>Whether every mesh it draws is a decal, and onto what.</returns>
        private DecalInfo DecalOf(Entity prefabEntity)
        {
            if (prefabEntity == Entity.Null) return new DecalInfo(false, 0, 0f, int.MinValue);
            if (_decalPrefabs.TryGetValue(prefabEntity, out DecalInfo known)) return known;

            bool decal = false;
            Game.Rendering.DecalLayers layers = 0;
            float height = 0f;
            int priority = int.MinValue;

            if (EntityManager.HasBuffer<SubMesh>(prefabEntity))
            {
                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                int meshes = 0, decals = 0;

                for (int i = 0; i < buffer.Length; i++)
                {
                    Entity mesh = buffer[i].m_SubMesh;
                    if ((mesh == Entity.Null) || !EntityManager.HasComponent<MeshData>(mesh)) continue;

                    meshes++;
                    MeshData data = EntityManager.GetComponentData<MeshData>(mesh);
                    if ((data.m_State & MeshFlags.Decal) == 0) continue;

                    decals++;
                    layers |= data.m_DecalLayer;
                    height = math.max(height, data.m_Bounds.max.y - data.m_Bounds.min.y);

                    // `MeshData.m_DecalLayer` is *not* where the renderer reads the mask from. It
                    // is zero on every decal in a full city — all 17,370 of them — which is why
                    // `decalLayers` came out absent on every record while the field was being read
                    // and the audit called it fixed. The value the shader is actually handed comes
                    // off the managed `DecalProperties` component:
                    // `ManagedBatchSystem.CreateBatch` and
                    // `RenderPrefabRenderer.Instance.SetDecalProperties` both set `_DecalLayerMask`
                    // from `DecalProperties.m_LayerMask`. Keep the ECS field as a fallback rather
                    // than replacing it: it costs nothing and a future build may start populating it.
                    //
                    if (EntityManager.HasComponent<PrefabData>(mesh)
                        && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(mesh),
                                                        out PrefabBase meshPrefab)
                        && (meshPrefab is RenderPrefab renderPrefab))
                    {
                        try
                        {
                            if (renderPrefab.TryGet(out DecalProperties properties)
                                && (properties != null) && properties.active)
                            {
                                layers |= properties.m_LayerMask;
                                // Highest wins, so a prefab whose meshes disagree takes the
                                // topmost — that is the one that ends up on screen.
                                priority = math.max(priority, properties.m_RendererPriority);
                            }
                        }
                        catch (Exception ex)
                        {
                            _log.Warn($"Decal properties of `{renderPrefab.name}` did not resolve: {ex.Message}");
                        }
                    }
                }

                // Every drawn mesh, not merely one of them: a building with a grime decal on its
                // wall is still a building, and must keep its collision.
                //
                decal = (meshes > 0) && (decals == meshes);
            }

            DecalInfo info = new(decal, layers, height, priority);
            _decalPrefabs[prefabEntity] = info;
            return info;
        }

        /// <summary>
        /// Attach the semantic attributes that are cheap and reliable to read.
        /// </summary>
        /// <param name="entity">The object entity.</param>
        /// <param name="prefabEntity">Its prefab.</param>
        /// <param name="kind">The classification already decided for it.</param>
        /// <param name="record">The record to populate.</param>
        private void AddAttributes(Entity entity, Entity prefabEntity, string kind, InstanceRecord record)
        {
            // How far the object stands *above* the ground, not the ground's own height — the two
            // were confused in the contract's wording for a while, and the difference is the whole
            // value of the field. The game computes it as
            // `max(0, transform.y - SampleHeight(terrain, pos))` in
            // `ObjectToolBaseSystem.CreateDefinitionsJob`, so it is zero for anything sitting on
            // the ground and the component is simply absent for most objects.
            //
            if (_elevations.TryGetComponent(entity, out Game.Objects.Elevation elevation))
            {
                record.Attributes["elevation"] = Math.Round((double)elevation.m_Elevation, 3);

                // The stored component is *not* clamped at zero — a full city measured 29,610
                // negative values (audit §4.1's own correction); only the placement-definition
                // path clamps. The flags still matter: `Lowered` is how the game marks a
                // deliberate below-ground placement, which a consumer cutting terrain needs.
                //
                string[] flags = FlagNames(elevation.m_Flags.ToString());
                if (flags.Length > 0) record.Attributes["elevationFlags"] = flags;
            }

            // A decal is painted onto something, and which something is not a guess a consumer
            // should have to make — see the contract's §4.2a.
            //
            if (kind == "decal")
            {
                DecalInfo info = DecalOf(prefabEntity);

                string[] onto = FlagNames(info.Layers.ToString());
                if (onto.Length > 0) record.Attributes["decalLayers"] = onto;

                // The mesh is the painted quad, so the volume it was projected through is the one
                // thing the mesh layer no longer says. See `MeshRipSystem.TryFlattenDecal`.
                //
                if (info.Height > 0f) record.Attributes["decalHeight"] = math.round(info.Height * 1000f) / 1000f;
                // The game's own draw order for overlapping decals (audit §57). Additive attr;
                // omitted where the prefab carries no `DecalProperties`, so its absence means
                // "the game states no order here" rather than "priority zero".
                if (info.Priority != int.MinValue) record.Attributes["decalPriority"] = info.Priority;
            }

            // The paint this particular object was drawn with — the game's own resolved answer,
            // not a key to look it up by. `Game.Objects.Color` is **not** that answer: its only
            // writer is `ObjectColorSystem`, which fills it from the active *infoview* — the value
            // is UI-overlay state that changes with whichever info mode was last open, and a
            // previous version of this attribute exported it as the paint choice (357,176 records
            // of infoview state in one full city). The real choice is computed by
            // `MeshColorSystem` from `PseudoRandomSeed`, the palette's probabilities and the
            // season filters, and stored per instance in the `MeshColor` buffer — recomputed for
            // every entity on load, so it is always populated by the time a rip can run.
            // `CustomMeshColor` overrides it wholesale when the player painted the thing
            // (`SetCustomColor` applies entry 0 to every part).
            //
            if (_customMeshColors.TryGetBuffer(entity, out DynamicBuffer<Game.Rendering.CustomMeshColor> custom)
                && (custom.Length > 0))
            {
                record.Attributes["colorSet"] = ColorSetJson(custom[0].m_ColorSet);
                record.Attributes["colorCustom"] = true;
                _colorSets++;
                _customColors++;
            }
            else if (_meshColors.TryGetBuffer(entity, out DynamicBuffer<Game.Rendering.MeshColor> colors))
            {
                if (colors.Length > 0)
                {
                    record.Attributes["colorSet"] = ColorSetJson(colors[0].m_ColorSet);
                    _colorSets++;

                    // Parts of one instance can resolve different sets (per-part random seeds).
                    // The first part's set is exported; count how often that approximates.
                    //
                    for (int i = 1; i < colors.Length; i++)
                    {
                        if (!colors[i].m_ColorSet.Equals(colors[0].m_ColorSet)) { _divergentColorSets++; break; }
                    }
                }
            }

            // THE GAME DOES NOT DRAW AN OVERRIDDEN OBJECT. `Game.Common.Overridden` is what CS2
            // marks a tree, prop or lane that something else was built over, and
            // `Game.Rendering.PreCullingSystem` leaves such an entity's culling mask at
            // `BoundsMask.Debug` — it never ORs in the prefab's real mesh layers, at either of
            // the two sites that test it. `BoundsMask` even carries a `NotOverridden` bit. The
            // object still exists, still has its transform, and is simply not rendered.
            //
            // Shipped as an attribute rather than dropped from the rip: the bundle describes the
            // city, and `csiibundle.instances.should_draw` is where a consumer asks whether to
            // draw a record — the same seam `state: "construction"` already uses.
            //
            if (_overridden.HasComponent(entity)) record.Attributes["overridden"] = true;

            if (_trees.TryGetComponent(entity, out Tree tree))
            {
                // m_Growth is a byte ramp; the contract defines growth as 0..1.
                //
                record.Attributes["growth"] = Math.Round(tree.m_Growth / 255d, 3);

                // The size the game actually draws this tree at. The renderer multiplies the
                // instance transform by a scale from `BatchDataHelpers.CalculateTreeSubMeshData` —
                // a √-lerp between the prefab's per-stage sizes (`GrowthScaleData`) — and a
                // consumer cannot reproduce it from `growth` alone because the per-stage sizes are
                // prefab data the bundle does not carry. Uniform, so one number.
                //
                if (EntityManager.HasComponent<GrowthScaleData>(prefabEntity))
                {
                    GrowthScaleData growthScale = EntityManager.GetComponentData<GrowthScaleData>(prefabEntity);
                    Game.Rendering.BatchDataHelpers.CalculateTreeSubMeshData(tree, growthScale, out float3 treeScale);
                    if (math.abs(treeScale.x - 1f) > 1e-3f)
                    {
                        record.Attributes["growthScale"] = Math.Round((double)treeScale.x, 4);
                        _growthScales++;
                    }
                }

                // Not `state`: the contract reserves that key for the building lifecycle enum, and
                // a tree's vocabulary (Teen/Adult/Elderly/Dead/Stump/Collected) is a different one.
                // A sapling has no flag set at all, where ToString() yields "0" — omit rather than
                // emit a meaningless value, since `growth` already carries the information.
                //
                string treeState = tree.m_State.ToString();
                if (!string.IsNullOrEmpty(treeState) && !char.IsDigit(treeState[0]))
                    record.Attributes["treeState"] = treeState;
            }

            if (_buildings.HasComponent(entity))
            {
                if (EntityManager.HasComponent<Game.Common.Destroyed>(entity)) record.Attributes["state"] = "destroyed";
                else if (EntityManager.HasComponent<Game.Buildings.Abandoned>(entity)) record.Attributes["state"] = "abandoned";
                else if (EntityManager.HasComponent<Game.Buildings.Condemned>(entity)) record.Attributes["state"] = "condemned";
                else if (EntityManager.HasComponent<Game.Objects.UnderConstruction>(entity)) record.Attributes["state"] = "construction";
                else record.Attributes["state"] = "normal";

                // The zoned-building metadata the contract documents and audit P12 found never
                // produced. `level` and the zone link are one prefab-side component
                // (SpawnableBuildingData); the zone strings are cached per zone prefab.
                //
                if (EntityManager.HasComponent<SpawnableBuildingData>(prefabEntity))
                {
                    SpawnableBuildingData spawnable = EntityManager.GetComponentData<SpawnableBuildingData>(prefabEntity);
                    record.Attributes["level"] = (int)spawnable.m_Level;
                    _levels++;

                    if (spawnable.m_ZonePrefab != Entity.Null)
                    {
                        (string zone, string zoning) = ZoneOf(spawnable.m_ZonePrefab);
                        if (zone != null) record.Attributes["zone"] = zone;
                        if (zoning != null) record.Attributes["zoning"] = zoning;
                        if ((zone != null) || (zoning != null)) _zones++;
                    }
                }

                // The brand of the company renting the building — `CompanyData.m_Brand` on a
                // renter, resolved to the brand prefab's name. Household renters carry no
                // CompanyData; the first branded renter wins, matching the signage the game
                // actually draws for the building.
                //
                if (EntityManager.HasBuffer<Game.Buildings.Renter>(entity))
                {
                    DynamicBuffer<Game.Buildings.Renter> renters = EntityManager.GetBuffer<Game.Buildings.Renter>(entity, true);
                    for (int i = 0; i < renters.Length; i++)
                    {
                        Entity renter = renters[i].m_Renter;
                        if ((renter == Entity.Null) || !EntityManager.HasComponent<Game.Companies.CompanyData>(renter)) continue;

                        Entity brand = EntityManager.GetComponentData<Game.Companies.CompanyData>(renter).m_Brand;
                        string brandName = (brand != Entity.Null) ? Instance.Name.GetPrefabName(brand) : null;
                        if (!string.IsNullOrEmpty(brandName))
                        {
                            record.Attributes["brand"] = brandName;
                            _brands++;
                        }

                        break;
                    }
                }

                // The postal address, by the game's own resolver (`BuildingUtils.GetAddress`,
                // the walk along the road-edge aggregate the info panel uses) plus the district
                // the simulation keeps on the building. The contract's documented shape:
                // [district, street, number], district null outside every district.
                //
                if (Game.Buildings.BuildingUtils.GetAddress(EntityManager, entity, out Entity street, out int number)
                    && (street != Entity.Null))
                {
                    Entity district = EntityManager.HasComponent<Game.Areas.CurrentDistrict>(entity)
                        ? EntityManager.GetComponentData<Game.Areas.CurrentDistrict>(entity).m_District
                        : Entity.Null;

                    record.Attributes["address"] = new object[]
                    {
                        (district != Entity.Null) ? Instance.Name.GetLabelName(district) : null,
                        Instance.Name.GetLabelName(street),
                        number
                    };
                    _addresses++;
                }
            }

            // The player's own name for the object, when one was given. The generated display
            // name is locale output derived from prefab and address and is not repeated here;
            // the custom name is the one piece of naming that is city data (the NameSystem's
            // custom-name table — one dictionary lookup per object).
            //
            if ((_nameSystem != null) && _nameSystem.TryGetCustomName(entity, out string customName)
                && !string.IsNullOrEmpty(customName))
            {
                record.Attributes["name"] = customName;
                _customNames++;
            }

            // The content theme the prefab belongs to (European / North American, seasonal
            // packs): `ThemeObject` bakes it into the prefab's requirement buffer as the entry
            // whose target carries `ThemeData`. Cached per prefab — one walk each.
            //
            string theme = ThemeOf(prefabEntity);
            if (theme != null)
            {
                record.Attributes["theme"] = theme;
                _themes++;
            }

            // Stockpile fullness (garbage heaps, coal and log piles): serialized sim state, the
            // value `BatchDataHelpers.CalculateQuantitySubMeshData` selects the Empty / Partial1 /
            // Partial2 / Full sub-mesh variants by. The game stores a byte percentage 0..100
            // (QuantityUpdateSystem rounds fraction × 100) and it is shipped raw.
            //
            if (EntityManager.HasComponent<Quantity>(entity))
            {
                record.Attributes["fullness"] = (int)EntityManager.GetComponentData<Quantity>(entity).m_Fullness;
                _fullness++;
            }
        }

        /// <summary>
        /// A zone prefab's exported strings, cached: <c>zone</c> is the prefab's own name (the
        /// specific zoned type, e.g. <c>NA Residential Low</c>); <c>zoning</c> is the coarse class
        /// from <c>ZoneData</c> — residential/commercial/industrial by <c>m_AreaType</c>, office
        /// carved out of industrial by the <c>Office</c> flag, exactly the split the game's own
        /// zoning UI makes.
        /// </summary>
        /// <param name="zonePrefab">The zone prefab.</param>
        /// <returns>The strings, either possibly <c>null</c>.</returns>
        private (string Zone, string Zoning) ZoneOf(Entity zonePrefab)
        {
            if (_zoneTypes.TryGetValue(zonePrefab, out (string Zone, string Zoning) known)) return known;

            string name = Instance.Name.GetPrefabName(zonePrefab);
            string area = null;

            if (EntityManager.HasComponent<ZoneData>(zonePrefab))
            {
                ZoneData zoneData = EntityManager.GetComponentData<ZoneData>(zonePrefab);
                area = zoneData.m_AreaType switch
                {
                    Game.Zones.AreaType.Residential => "residential",
                    Game.Zones.AreaType.Commercial => "commercial",
                    Game.Zones.AreaType.Industrial => ((zoneData.m_ZoneFlags & ZoneFlags.Office) != 0) ? "office" : "industrial",
                    _ => null
                };
            }

            (string, string) result = (string.IsNullOrEmpty(name) ? null : name, area);
            _zoneTypes[zonePrefab] = result;
            return result;
        }

        /// <summary>
        /// The theme an object prefab belongs to, or <c>null</c>, cached. <c>ThemeObject</c> has
        /// no marker of its own on the object prefab: it adds an <c>ObjectRequirementElement</c>
        /// whose requirement entity is the theme prefab (the entity carrying <c>ThemeData</c>),
        /// so that entry is what identifies the theme.
        /// </summary>
        /// <param name="prefabEntity">The object prefab.</param>
        /// <returns>The theme prefab's name, or <c>null</c>.</returns>
        private string ThemeOf(Entity prefabEntity)
        {
            if (_prefabThemes.TryGetValue(prefabEntity, out string known)) return known;

            string theme = null;
            if (EntityManager.HasBuffer<ObjectRequirementElement>(prefabEntity))
            {
                DynamicBuffer<ObjectRequirementElement> requirements = EntityManager.GetBuffer<ObjectRequirementElement>(prefabEntity, true);
                for (int i = 0; i < requirements.Length; i++)
                {
                    Entity requirement = requirements[i].m_Requirement;
                    if ((requirement == Entity.Null) || !EntityManager.HasComponent<ThemeData>(requirement)) continue;

                    string themeName = Instance.Name.GetPrefabName(requirement);
                    if (!string.IsNullOrEmpty(themeName))
                    {
                        theme = themeName;
                        break;
                    }
                }
            }

            _prefabThemes[prefabEntity] = theme;
            return theme;
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// Names rather than a bit field, for the same reason <c>lanes.jsonl</c> does it: the
        /// numeric values are CS2 internals that can change meaning between game versions, where a
        /// name that disappears is a name a consumer simply stops seeing. An enum with no bits set
        /// renders as <c>"0"</c>, which is not a name and is dropped.
        /// </summary>
        /// <param name="flags">The enum's <c>ToString</c>.</param>
        /// <returns>The names, possibly empty.</returns>
        /// <summary>
        /// A resolved <c>ColorSet</c> as the contract writes colours: three linear RGBA channels.
        /// </summary>
        /// <param name="set">The set.</param>
        /// <returns>Three RGBA arrays.</returns>
        internal static double[][] ColorSetJson(Game.Rendering.ColorSet set) => new[]
        {
            ChannelJson(set.m_Channel0), ChannelJson(set.m_Channel1), ChannelJson(set.m_Channel2)
        };

        /// <inheritdoc cref="ColorSetJson"/>
        private static double[] ChannelJson(UnityEngine.Color color) => new double[]
        {
            Math.Round(color.r, 4), Math.Round(color.g, 4), Math.Round(color.b, 4), Math.Round(color.a, 4)
        };

        private static string[] FlagNames(string flags)
        {
            if (string.IsNullOrEmpty(flags) || char.IsDigit(flags[0])) return Array.Empty<string>();

            List<string> names = new();
            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) names.Add(trimmed);
            }

            return names.ToArray();
        }

        /// <summary>
        /// Write the bundle's <c>warnings.jsonl</c> layer, one line per thing the producer could
        /// not fully deliver.
        /// The contract defines this layer as advisory so a consumer may ignore it — its purpose is
        /// to make a stand-in asset explicable rather than mysterious.
        /// </summary>
        /// <param name="run">The finished run, holding every declaration set.</param>
        /// <param name="report">The report to note the outcome in.</param>
        private void WriteWarnings(InstanceRun run, StringBuilder report)
        {
            int kept = WarningsFile.Rewrite(
                run.OutputDirectory,
                line => WarningsFile.IsKind(line, WarningsFile.InstanceKinds),
                InstanceDeclarations(run));
            if (kept < 0) return;

            int total = run.UnresolvedPrefabs.Count + run.GeometrylessPrefabs.Count
                      + run.StaleEntities.Count + run.FailedEntities.Count;
            report.AppendLine($"Wrote {WarningsFile.FileName} ({total} entries: "
                            + $"{run.UnresolvedPrefabs.Count} unresolved, {run.GeometrylessPrefabs.Count} without geometry, "
                            + $"{run.StaleEntities.Count} removed mid-rip, {run.FailedEntities.Count} failed), "
                            + $"keeping {kept} from the other passes");
        }

        /// <summary>
        /// This pass's §10 declarations, in write order.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>One record per gap.</returns>
        private static IEnumerable<Dictionary<string, object>> InstanceDeclarations(InstanceRun run)
        {
            foreach (int prefabIndex in run.UnresolvedPrefabs)
            {
                yield return WarningsFile.ByAsset("prefab", $"unresolved_{prefabIndex}",
                    "managed prefab did not resolve; instances kept with placement only");
            }

            foreach (string assetId in run.GeometrylessPrefabs)
            {
                yield return WarningsFile.ByAsset("prefab", assetId,
                    "prefab has no SubMesh buffer, so no mesh is produced; "
                    + "instances kept with placement only");
            }

            // Mid-rip churn and per-entity failures: each is a record instances.jsonl is
            // missing, and §10 wants a missing object explicable rather than mysterious.
            // These lines carry the entity id (the record's `id` domain) instead of an
            // assetId — the stale ones no longer exist to ask for a prefab at all.
            //
            foreach (int entityId in run.StaleEntities)
            {
                yield return WarningsFile.ById("instance", entityId,
                    "removed by the simulation mid-rip; no record written");
            }

            foreach ((int entityId, string error) in run.FailedEntities)
            {
                yield return WarningsFile.ById("instance", entityId,
                    $"failed to record ({error}); no record written — see the log");
            }
        }
    }
}
