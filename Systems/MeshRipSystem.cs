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
        /// How long the reader may hold the main thread each frame.
        /// One prefab is always processed per frame even when it overruns, so a single very large
        /// mesh cannot deadlock the run.
        /// </summary>
        private const double FrameBudgetMilliseconds = 6d;

        /// <summary>
        /// How many items may wait for the writer.
        /// Every item holds a full uncompressed payload — a prefab's vertex arrays, or a texture's
        /// pixels at four bytes each — so an unbounded queue would simply move the memory problem
        /// rather than solve it. The reader pauses when the queue is full. Thirty-two rather than
        /// eight since the encoders went parallel: the writer drains textures faster than the
        /// reader can issue them, and a texture's pixels only land a frame or two after its item
        /// is queued.
        /// </summary>
        private const int QueueCapacity = 32;

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
            LodChains = 0;
            LodLevelsRead = 0;
            LodCap = 0;
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
            ResetTrafficSide();

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
            ForgetSystems();
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
            run.Report.AppendLine($"Authored LOD chains: {LodChains} mesh(es) carry one, {LodLevelsRead} level(s) read, deepest {LodCap}; written as <node>_LOD<n> nodes (lodCap {LodCap}).");
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


    }
}
