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
        /// Start enumerating every placed object into <c>instances.jsonl</c>, returning as soon as
        /// the asset-id table is built; the walk itself proceeds a slice per frame in
        /// <see cref="OnUpdate"/> (PLAN Phase 8 — the pass used to block the main thread whole).
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.</param>
        /// <param name="kinds">Which kinds to cover; <c>null</c> for all of them.</param>
        /// <returns>A short description of what was queued.</returns>
        public string BeginRip(string outputDirectory, Action<RipOutcome> completed = null, RipKindFilter? kinds = null)
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


    }
}
