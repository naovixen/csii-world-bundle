using Carto.IO;
using Colossal.Logging;
using Game;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;

namespace Carto.Systems
{
    /// <summary>
    /// The one-button rip: runs the whole extraction pipeline in order, asynchronously, and
    /// reports everything in a single end-of-rip dialog (PLAN Phase 8).
    /// <b>Stage order</b>: instances &amp; appearance → meshes/materials/textures → lane graph →
    /// networks &amp; lane geometry → terrain &amp; water → optional vector export →
    /// <b>manifest, always last and unconditionally</b> — it describes whatever the run left on
    /// disk, so it is also correct after a partial (layer-deselected) rip, and folding it in kills
    /// the "forgot to re-finalize" foot-gun.
    /// <b>Failure policy</b>: a stage that throws is recorded as failed and the pipeline moves on —
    /// the layers are independent by contract (absent layer = absent feature), and the manifest
    /// must still describe whatever did land.
    /// The two long stages (meshes, networks) already run a slice per frame against a bounded
    /// writer queue; the pipeline waits for their completion callback. Short synchronous stages run
    /// through <see cref="RipProgress.RunDeferred"/> so their caption reaches the screen before
    /// they take the main thread.
    /// </summary>
    public partial class RipOrchestratorSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// What a rip should cover, and with which caps. The toggles map 1:1 to pipeline stages
        /// (coarse by design — the contract already handles partial bundles).
        /// </summary>
        public sealed class RipPlan
        {
            /// <summary>The bundle root to write into.</summary>
            public string OutputDirectory;

            /// <summary>Buildings, props &amp; trees: instances + appearance, and the prefab meshes they reference.</summary>
            public bool Objects = true;

            /// <summary>The advanced kind split under <see cref="Objects"/>.</summary>
            public RipKindFilter Kinds = RipKindFilter.All;

            /// <summary>The advanced appearance splits under <see cref="Objects"/>: colour palettes and light emitters.</summary>
            public bool Colors = true;
            public bool Lights = true;

            /// <summary>Roads &amp; lanes: the lane graph, network surfaces and lane-drawn geometry.</summary>
            public bool Networks = true;

            /// <summary>The advanced splits under <see cref="Networks"/>.</summary>
            public bool NetworkSurfaces = true;
            public bool LaneGraph = true;
            public bool LaneGeometry = true;
            public bool LaneDecals = true;

            /// <summary>Terrain &amp; water: heightmaps, splat, surfaces and clip volumes.</summary>
            public bool Terrain = true;

            /// <summary>The advanced splits under <see cref="Terrain"/>.</summary>
            public bool TerrainHeights = true;
            public bool TerrainWater = true;
            public bool TerrainGround = true;
            public bool TerrainClips = true;
            public bool TerrainAreas = true;

            /// <summary>The vanilla vector/raster export, alongside.</summary>
            public bool Vector = true;

            /// <summary>The most prefabs the mesh stage rips, or <c>0</c> for all.</summary>
            public int MeshLimit;

            /// <summary>The longest edge a written texture may have, <c>0</c> for no cap, or negative to write no textures.</summary>
            public int TextureSizeCap;

            /// <summary>Whether the objects stages run at all.</summary>
            public bool RipsObjects => Objects && Kinds.Any;

            /// <summary>Whether the network stage runs at all.</summary>
            public bool RipsNetworkStage => Networks && (NetworkSurfaces || LaneGeometry);

            /// <summary>Whether the terrain stage runs at all.</summary>
            public bool RipsTerrain => Terrain && (TerrainHeights || TerrainWater || TerrainGround || TerrainClips || TerrainAreas);
        }

        /// <summary>
        /// One pipeline stage. There are exactly two kinds and they share only a title, so they
        /// are two types: <see cref="SyncStage"/> runs on the main thread and returns its outcome,
        /// <see cref="AsyncStage"/> is begun and reports through a completion callback later.
        /// </summary>
        /// <remarks>
        /// This was ONE class with a <c>Work</c> field and a <c>Begin</c>/<c>Running</c> pair,
        /// exactly one of which was ever set, and nothing but a null check said which — a type
        /// carrying two mutually exclusive contracts with no compiler help. Split by refactor
        /// Phase 7; <see cref="StartStage"/> now switches on the type instead of on a null.
        /// </remarks>
        private abstract class Stage
        {
            /// <summary>The heading the stage reports under.</summary>
            public string Title;
        }

        /// <summary>Work that runs on the main thread and is finished when it returns.</summary>
        private sealed class SyncStage : Stage
        {
            /// <summary>The caption while it runs.</summary>
            public RipProgress.Phase Phase;

            /// <summary>The work.</summary>
            public Func<RipOutcome> Work;
        }

        /// <summary>Work that is started here and reports through its completion callback.</summary>
        private sealed class AsyncStage : Stage
        {
            /// <summary>Start it, handing it the completion sink; returns the kickoff message.</summary>
            public Func<Action<RipOutcome>, string> Begin;

            /// <summary>Whether it actually started. False means there was nothing to do.</summary>
            public Func<bool> Running;
        }

        /// <summary>The stages of the rip in flight, or <c>null</c> when idle.</summary>
        private List<Stage> _stages;

        /// <summary>The stage currently running.</summary>
        private int _index;

        /// <summary>The plan the run was started with.</summary>
        private RipPlan _plan;

        /// <summary>Set (with <see cref="_stageOutcome"/>) when the current stage finishes.</summary>
        private volatile bool _stageDone;
        private RipOutcome _stageOutcome;

        /// <summary>How many stages recorded a failure.</summary>
        private int _stagesFailed;

        /// <summary>Timing for the current stage and the whole run.</summary>
        private Stopwatch _stageClock, _totalClock;

        /// <summary>The per-stage lines of the end-of-rip dialog.</summary>
        private StringBuilder _summary;

        /// <summary>
        /// Bumped whenever a run starts or is torn down, so a completion callback from an
        /// abandoned run cannot advance a later one. Volatile: the fence is read on worker
        /// threads (stage completion callbacks) while the main thread bumps it.
        /// </summary>
        private volatile int _generation;

        /// <summary>
        /// Whether a one-button rip is in flight.
        /// </summary>
        public bool IsRipping => _stages != null;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            base.OnCreate();
            _log.Debug("RipOrchestratorSystem instance created.");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// A pipeline abandoned by a world unload is simply dropped: the async stages release
        /// themselves in their own <c>OnDestroy</c>, and every completion callback is fenced by
        /// <see cref="_generation"/>.
        /// </summary>
        protected override void OnDestroy()
        {
            if (_stages != null)
            {
                _log.Info($"World rip abandoned at stage {_index + 1}/{_stages.Count}; the world was unloaded.");
                Teardown();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Drive the progress indicator, and advance the pipeline: start the next stage once the
        /// current one has reported.
        /// </summary>
        /// <remarks>
        /// <see cref="RipProgress.Pump"/> lives here rather than in a rip stage. It has to run on
        /// the main thread every frame — it is what pushes the caption to the UI AND what runs
        /// the work queued by <see cref="RipProgress.RunDeferred"/>, which includes the vanilla
        /// vector export (<c>Settings.cs:431</c>) and the manifest. It used to be called from
        /// <c>MeshRipSystem.OnUpdate</c> and <c>NetworkRipSystem.OnUpdate</c>, twice a frame,
        /// which worked only because those two systems happen to be registered unconditionally at
        /// <c>MainLoop</c> and declare no <c>RequireForUpdate</c>. That is an accident of
        /// registration, not a design: this system is the one whose job the rip's UX is.
        /// </remarks>
        protected override void OnUpdate()
        {
            RipProgress.Pump();

            if (_stages == null || !_stageDone) return;

            _stageClock.Stop();
            RecordStage(_stages[_index], _stageOutcome, _stageClock.Elapsed.TotalSeconds);

            _index++;
            if (_index < _stages.Count) StartStage(_stages[_index]);
            else Finish();
        }

        /// <summary>
        /// Start the whole pipeline, returning at once.
        /// </summary>
        /// <param name="plan">What to rip.</param>
        /// <returns>What the caller should tell the user.</returns>
        public string BeginRip(RipPlan plan)
        {
            if (IsRipping) return "A world rip is already running.";
            if (Instance.InstanceRip.IsRipping) return "An instance rip is already running — wait for it to finish.";
            if (Instance.MeshRip.IsRipping) return "A mesh rip is already running — wait for it to finish.";
            if (Instance.LaneRip.IsRipping) return "A lane rip is already running — wait for it to finish.";
            if (Instance.NetworkRip.IsRipping) return "A network rip is already running — wait for it to finish.";

            List<Stage> stages = BuildStages(plan);
            System.IO.Directory.CreateDirectory(plan.OutputDirectory);

            _plan = plan;
            _stages = stages;
            _index = 0;
            _stagesFailed = 0;
            _summary = new StringBuilder();
            _totalClock = Stopwatch.StartNew();

            StringBuilder titles = new();
            foreach (Stage stage in stages) titles.Append(titles.Length == 0 ? "" : ", ").Append(stage.Title);
            _log.Info($"World rip started: {stages.Count} stage(s) — {titles} — into {plan.OutputDirectory}.");

            StartStage(stages[0]);

            return $"Ripping the world in the background ({stages.Count} stage(s)).\n\n"
                 + "Close this options page to watch the progress indicator — it appears in the top-right of the game view, not over the menu.\n\n"
                 + "A single dialog reports every stage when the rip finishes.";
        }


        /// <summary>
        /// Start one stage. Synchronous work is deferred a couple of frames so its caption paints
        /// first; asynchronous work is begun and awaited through its completion callback.
        /// </summary>
        /// <param name="stage">The stage to start.</param>
        private void StartStage(Stage stage)
        {
            _stageDone = false;
            _stageOutcome = default;
            _stageClock = Stopwatch.StartNew();

            int generation = _generation;
            void Done(RipOutcome outcome)
            {
                if (generation != _generation) return;
                _stageOutcome = outcome;
                _stageDone = true;
            }

            if (stage is SyncStage sync)
            {
                // The exception is caught here, not left to RunDeferred's own catch, because the
                // pipeline must hear about the stage either way — a swallowed throw would leave it
                // waiting forever.
                RipProgress.RunDeferred(sync.Phase, () =>
                {
                    RipOutcome outcome;
                    try
                    {
                        outcome = sync.Work();
                    }
                    catch (Exception ex)
                    {
                        _log.Error($"World rip stage `{stage.Title}` failed: {ex}");
                        outcome = RipOutcome.Failure(ex.Message);
                    }

                    Done(outcome);
                });

                return;
            }

            AsyncStage async = (AsyncStage)stage;
            string kickoff;
            try
            {
                kickoff = async.Begin(Done);
            }
            catch (Exception ex)
            {
                _log.Error($"World rip stage `{stage.Title}` failed to start: {ex}");
                Done(RipOutcome.Failure($"FAILED to start: {ex.Message}"));
                return;
            }

            _log.Info($"World rip stage `{stage.Title}`: {kickoff}");

            // Not started means there was nothing to do (or a refusal) — the kickoff message is
            // the whole story, and no completion callback will ever come.
            //
            if (!async.Running()) Done(RipOutcome.Ok(kickoff));
        }

    }
}
