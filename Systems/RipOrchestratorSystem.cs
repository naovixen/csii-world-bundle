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
        /// Assemble the stage list for a plan. The manifest is appended unconditionally.
        /// </summary>
        /// <param name="plan">What to rip.</param>
        /// <returns>The stages, in execution order.</returns>
        private static List<Stage> BuildStages(RipPlan plan)
        {
            string directory = plan.OutputDirectory;
            List<Stage> stages = new();

            // A stage's outputs are truncated or overwritten by name when it re-runs — except the
            // content-hash-named textures/, whose files from an earlier run at a different cap are
            // orphans no record references, silently inflating the bundle's counts and sizes. So a
            // rip first deletes the outputs OWNED by its selected stages (a deselected layer's
            // files stay, by the partial-bundle contract). The shared texture store and material
            // table are cleared only when ALL THREE of their producers run (objects, network
            // surfaces AND lane geometry): only then is the rebuilt table complete, while any
            // partial rip must keep them — the merge semantics are what keep the untouched layers'
            // material references valid (MaterialRipSystem.WriteTable).
            //
            List<string> staleFiles = new();
            List<string> staleDirectories = new();
            if (plan.RipsObjects)
            {
                staleFiles.Add("instances.jsonl");
                staleDirectories.Add("meshes");
            }
            // Appearance depends on the Objects master and its own sub-toggles, not on the kind
            // filter — mirrored by the stage condition below.
            //
            if (plan.Objects && plan.Colors) staleFiles.Add("colors.json");
            if (plan.Objects && plan.Lights) staleFiles.Add("lights.json");
            if (plan.Networks && plan.NetworkSurfaces)
            {
                staleFiles.Add("networks.jsonl");
                staleDirectories.Add("networks");
            }
            if (plan.Networks && plan.LaneGraph)
            {
                staleFiles.Add("lanes.jsonl");
            }
            if (plan.Networks && plan.LaneGeometry)
            {
                staleFiles.AddRange(new[] { "lanemeshes.jsonl", "laneprefabs.json" });
                staleDirectories.Add("lanemeshes");
            }
            // Terrain cleans per sub-toggle: rasters.json is never deleted (it merges), and the
            // whole terrain/ directory goes only when every sub is selected — a partial terrain
            // re-rip must not delete the layers it is not rebuilding.
            //
            if (plan.Terrain && plan.TerrainHeights && plan.TerrainWater && plan.TerrainGround
                && plan.TerrainClips && plan.TerrainAreas)
            {
                staleDirectories.Add("terrain");
            }
            else if (plan.Terrain)
            {
                if (plan.TerrainHeights) staleFiles.AddRange(new[] { "terrain/heightmap.png", "terrain/heightmap_fine.png", "terrain/world_heightmap.png" });
                if (plan.TerrainWater) staleFiles.Add("terrain/water_level.png");
                if (plan.TerrainGround)
                {
                    staleFiles.AddRange(new[] { "terrain/surface_index.png", "terrain/splatmap.png", "terrain/user_splatmap.png" });
                    staleDirectories.Add("terrain/surfaces");
                }
            }
            if (plan.Terrain && plan.TerrainClips)
            {
                staleFiles.Add("clipvolumes.jsonl");
            }
            if (plan.Terrain && plan.TerrainAreas)
            {
                staleFiles.AddRange(new[] { "areas.jsonl", "areaprefabs.json", "districts.jsonl" });
            }
            if (plan.Vector)
            {
                staleDirectories.Add("vector");
            }
            if (plan.RipsObjects && plan.Networks && plan.NetworkSurfaces && plan.LaneGeometry)
            {
                staleFiles.Add("materials.json");
                staleDirectories.Add("textures");
            }

            // Whenever any producer stage is selected (every one of them contributes at least one
            // entry above), the previous rip's manifest is about to go stale: an aborted run must
            // not leave the old bundle.json describing mixed layers. The Manifest stage, always
            // last, rebuilds it from whatever the run leaves on disk.
            //
            if ((staleFiles.Count > 0) || (staleDirectories.Count > 0))
            {
                staleFiles.Add("bundle.json");
            }

            if ((staleFiles.Count > 0) || (staleDirectories.Count > 0))
            {
                stages.Add(new AsyncStage
                {
                    Title = "Clean",
                    Begin = completed =>
                    {
                        RipProgress.Report(RipProgress.Phase.Clean);

                        // Off the main thread: meshes/ and textures/ can be gigabytes, and
                        // deleting them is pure file I/O.
                        System.Threading.Tasks.Task.Run(() => completed(CleanStaleOutputs(directory, staleFiles, staleDirectories)));

                        return "Removing the previous rip's outputs of the selected layers on a background thread.";
                    },
                    Running = () => true
                });
            }

            if (plan.RipsObjects)
            {
                stages.Add(new AsyncStage
                {
                    Title = "Instances",
                    Begin = completed => Instance.InstanceRip.BeginRip(directory, completed, plan.Kinds),
                    Running = () => Instance.InstanceRip.IsRipping
                });
            }

            // Colours and lights are per-render-prefab tables resolved from the same prefab
            // set the instances reference; a quick synchronous pass. Gated on the Objects master
            // and its own sub-toggles, NOT on the kind filter: all kinds off with Colors or
            // Lights on must still run Appearance (audit I-F9).
            //
            if (plan.Objects && (plan.Colors || plan.Lights))
            {
                stages.Add(new SyncStage
                {
                    Title = "Appearance",
                    Phase = RipProgress.Phase.Instances,
                    Work = () => RipOutcome.Ok(Instance.AppearanceRip.RipAppearance(directory, plan.Colors, plan.Lights))
                });
            }

            if (plan.RipsObjects)
            {
                stages.Add(new AsyncStage
                {
                    Title = "Meshes, materials & textures",
                    Begin = completed => Instance.MeshRip.BeginRip(directory, plan.MeshLimit, plan.TextureSizeCap, completed, plan.Kinds),
                    Running = () => Instance.MeshRip.IsRipping
                });
            }

            if (plan.Networks && plan.LaneGraph)
            {
                stages.Add(new AsyncStage
                {
                    Title = "Lane graph",
                    Begin = completed => Instance.LaneRip.BeginRip(directory, completed),
                    Running = () => Instance.LaneRip.IsRipping
                });
            }

            // The fine cascade capture steers the game camera over the map for a few frames per
            // window and reads the 1.75 m cascade slice back (TerrainRipSystem.FineCascade.cs). It
            // runs BEFORE the network stage so the terrain conforming can sample the same ground
            // the game shows (§34), and its raster is written by the terrain stage below.
            //
            if (plan.Terrain && plan.TerrainHeights)
            {
                stages.Add(new AsyncStage
                {
                    Title = "Terrain (fine cascade)",
                    Begin = completed => Instance.TerrainRip.BeginFineCascade(completed),
                    Running = () => Instance.TerrainRip.IsCapturingFine
                });
            }

            if (plan.RipsNetworkStage)
            {
                stages.Add(new AsyncStage
                {
                    Title = "Networks & lane geometry",
                    Begin = completed => Instance.NetworkRip.BeginRip(directory, plan.TextureSizeCap, completed,
                                                                      plan.NetworkSurfaces, plan.LaneGeometry, plan.LaneDecals),
                    Running = () => Instance.NetworkRip.IsRipping
                });
            }

            if (plan.RipsTerrain)
            {
                stages.Add(new SyncStage
                {
                    Title = "Terrain & water",
                    Phase = RipProgress.Phase.Terrain,
                    Work = () => RipOutcome.Ok(Instance.TerrainRip.RipTerrain(directory, plan.TerrainHeights, plan.TerrainWater,
                                                                              plan.TerrainGround, plan.TerrainClips, plan.TerrainAreas))
                });
            }

            if (plan.Vector)
            {
                stages.Add(new SyncStage
                {
                    Title = "Vector export",
                    Phase = RipProgress.Phase.Export,
                    Work = () => RunVectorExport(directory)
                });
            }

            // Always last, unconditionally: the manifest describes whatever the run left on disk,
            // including the layers earlier runs left there and this one did not touch. The
            // game-state snapshot is captured on the main thread (cheap); the directory scan —
            // reading every record layer back — runs on a worker, and the pipeline waits on the
            // completion callback before reporting (PLAN Phase 8).
            //
            stages.Add(new AsyncStage
            {
                Title = "Manifest",
                Begin = completed =>
                {
                    RipProgress.Report(RipProgress.Phase.Manifest);
                    BundleManifest snapshot = BundleManifest.Capture();

                    // The game's resolved colour grading, captured HERE because it must be read on
                    // the main thread (HDRP render state) and written on the worker with the
                    // manifest. It is what turns CS2's near-grey ground albedo into its on-screen
                    // palette; without it a consumer cannot tell the two apart. See GradingWriter.
                    //
                    Dictionary<string, object> grading = GradingWriter.Capture();

                    // ripOptions must describe THIS run, not the live settings at finalize time —
                    // the user may have flipped a toggle while the rip ran (audit I-F8). The
                    // plan's caps are what the stages actually ran with, including the negative
                    // textures-off encoding of TextureSizeCap.
                    //
                    snapshot.TextureSizeCap = plan.TextureSizeCap;
                    snapshot.MeshLimit = plan.MeshLimit;

                    System.Threading.Tasks.Task.Run(() =>
                    {
                        string summary;
                        try
                        {
                            // Grading first: the manifest's layer index is a scan of what is on
                            // disk, so a file written after it is a layer the manifest cannot
                            // declare. The report still reads manifest-then-grading.
                            //
                            string gradingSummary = GradingWriter.Write(directory, grading);
                            summary = BundleWriter.WriteManifest(directory, snapshot);
                            summary += Environment.NewLine + gradingSummary;
                        }
                        catch (Exception ex)
                        {
                            Instance.Log.Error($"World rip stage `Manifest` failed: {ex}");
                            completed(RipOutcome.Failure(ex.Message));
                            return;
                        }

                        completed(RipOutcome.Ok(summary));
                    });

                    return "Scanning the bundle on a background thread.";
                },
                Running = () => true
            });

            return stages;
        }

        /// <summary>
        /// Delete the previous rip's outputs of the selected stages. Runs on a worker thread.
        /// </summary>
        /// <param name="directory">The bundle root.</param>
        /// <param name="files">Layer files to remove, relative to the root.</param>
        /// <param name="directories">Layer directories to remove, relative to the root.</param>
        /// <returns>What was removed.</returns>
        private static RipOutcome CleanStaleOutputs(string directory, List<string> files, List<string> directories)
        {
            List<string> removed = new();
            List<string> failed = new();

            foreach (string file in files)
            {
                string path = Utils.IOUtils.CombinePath(directory, file);
                try
                {
                    if (!System.IO.File.Exists(path)) continue;
                    System.IO.File.Delete(path);
                    removed.Add(file);
                }
                catch (Exception ex)
                {
                    failed.Add(file);
                    _log.Warn($"Could not remove stale `{file}`: {ex.Message}");
                }
            }

            foreach (string dir in directories)
            {
                string path = Utils.IOUtils.CombinePath(directory, dir);
                try
                {
                    if (!System.IO.Directory.Exists(path)) continue;
                    System.IO.Directory.Delete(path, recursive: true);
                    removed.Add(dir + "/");
                }
                catch (Exception ex)
                {
                    failed.Add(dir + "/");
                    _log.Warn($"Could not remove stale `{dir}/`: {ex.Message}");
                }
            }

            if ((removed.Count == 0) && (failed.Count == 0)) return RipOutcome.Ok("Nothing stale to remove.");

            string summary = (removed.Count > 0)
                ? $"Removed the previous rip's {string.Join(", ", removed)}."
                : "";
            if (failed.Count > 0)
                summary += $"{(summary.Length > 0 ? "\n" : "")}FAILED to remove {string.Join(", ", failed)} — stale files may inflate the bundle; see the log.";

            // Partial: some layers went, some did not, and the report says both. `Failed` is
            // what the orchestrator counts; the text is what the person reads.
            return (failed.Count > 0) ? RipOutcome.Failure(summary) : RipOutcome.Ok(summary);
        }

        /// <summary>
        /// Run the vanilla vector/raster export into the bundle's <c>vector/</c> layer.
        /// The vanilla path writes under the user's configured <c>CustomDirectory</c>, outside
        /// the bundle — so the manifest's <c>vector/</c> layer could never exist (audit I-F2).
        /// A one-off <see cref="IO.Options"/> instance redirects the export without ever touching
        /// the saved setting, and suppresses the vanilla completion dialog and sound — the rip
        /// reports once, at the end.
        /// </summary>
        /// <param name="bundleDirectory">The bundle root.</param>
        /// <returns>The stage summary.</returns>
        private static RipOutcome RunVectorExport(string bundleDirectory)
        {
            IO.Options options = Instance.Settings.GetOptions();

            // The vanilla button refuses to export on invalid projection inputs (with a dialog);
            // here the refusal becomes a failed stage line instead.
            //
            if (options.Errors.Count > 0)
                return RipOutcome.Failure($"invalid export settings ({string.Join(", ", options.Errors.Keys)}) — fix them on the Carto options pages.");

            options.CustomDirectory = Utils.IOUtils.CombinePath(bundleDirectory, "vector");
            options.CompletionDialog = false;
            options.CompletionSound = false;

            IO.ExportResult result = IO.IO.Export(options);
            return result.Success
                ? RipOutcome.Ok($"Vector/raster export completed — {result.FilesWritten.Length} file(s) into vector/.")
                : RipOutcome.Failure(result.ErrorMessage);
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

        /// <summary>
        /// Append one finished stage to the end-of-rip summary.
        /// </summary>
        /// <param name="stage">The stage that finished.</param>
        /// <param name="outcome">What it reported, and whether it failed.</param>
        /// <param name="seconds">How long it took.</param>
        private void RecordStage(Stage stage, RipOutcome outcome, double seconds)
        {
            // The stage says whether it failed. It used to be inferred here by looking for a line
            // beginning "FAILED", which twelve sites across five systems produced independently —
            // so the contract was a spelling, and a stage writing "Failed" was silently a success.
            //
            if (outcome.Failed) _stagesFailed++;

            string summary = outcome.Summary;
            string[] lines = summary.Replace("\r\n", "\n").Split('\n');

            _summary.AppendLine($"{stage.Title} — {seconds:F1} s");

            if (!string.IsNullOrEmpty(summary))
            {
                // A few lines per stage keeps the one dialog readable; every stage's full report
                // is already in the log.
                int shown = 0;
                foreach (string line in lines)
                {
                    if (string.IsNullOrWhiteSpace(line) || line.StartsWith("Full report:")) continue;
                    _summary.AppendLine($"    {line.Trim()}");
                    if (++shown >= 3) break;
                }
            }

            _log.Info($"World rip stage `{stage.Title}` finished in {seconds:F1} s:\n{summary}");
        }

        /// <summary>
        /// Close out the run: log, clear, and show the single end-of-rip dialog.
        /// </summary>
        private void Finish()
        {
            _totalClock.Stop();

            StringBuilder dialog = new();
            dialog.AppendLine(_stagesFailed == 0
                ? $"World rip finished in {_totalClock.Elapsed.TotalSeconds:F0} s."
                : $"World rip finished in {_totalClock.Elapsed.TotalSeconds:F0} s — {_stagesFailed} stage(s) FAILED.");
            dialog.AppendLine();
            dialog.Append(_summary);

            string unresolved = DescribeUnresolved(_plan.OutputDirectory);
            if (unresolved != null)
            {
                dialog.AppendLine();
                dialog.AppendLine(unresolved);
            }

            dialog.AppendLine();
            dialog.AppendLine($"Output: {_plan.OutputDirectory}");
            dialog.Append("Full report: Logs/Carto.log");

            _log.Info($"World rip finished.\n{dialog}");
            Teardown();

            try
            {
                Game.UI.MessageDialog message = new("Options.SECTION[Carto.Carto.Mod]", Settings.Fit(dialog.ToString()), "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(message, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the world rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// The one line of the completion dialog the user can <i>act</i> on, or <c>null</c> when
        /// there is nothing to act on.
        /// <para>
        /// A prefab the save references but whose asset was not loaded this session is not a
        /// producer failure and not a bundle defect: the game itself drew those objects as
        /// nothing and those surfaces with its grey Missing Area fallback, so the bundle
        /// faithfully records the session it ripped. But it is the one shortfall a
        /// <b>re-rip fixes</b>, and nothing on screen said so — the declarations reached only
        /// <c>warnings.jsonl</c> and <c>areaprefabs.json</c>, which nobody reads after a rip.
        /// </para>
        /// <para>
        /// Counted from the bundle on disk rather than from this run's own state, so it stays
        /// true after a partial rip: it describes what the bundle a consumer will read actually
        /// declares, whichever stage last wrote it. Diagnostics only — an unreadable or absent
        /// file simply reports nothing, and no failure here may reach the user as a rip failure.
        /// </para>
        /// </summary>
        /// <param name="directory">The bundle root.</param>
        /// <returns>The advisory line, or <c>null</c>.</returns>
        private static string DescribeUnresolved(string directory)
        {
            int prefabs = 0;
            int surfaces = 0;

            try
            {
                // §10 `prefab` warnings cover two different things and only one of them is an
                // asset-pack miss: `unresolved_<index>` is a managed prefab that would not
                // resolve, while a named one is a prefab that resolved and simply has no
                // SubMesh buffer. Enabling a pack cannot help the second, so matching the
                // whole kind would promise a cure that does not exist.
                //
                string warnings = Utils.IOUtils.CombinePath(directory, "warnings.jsonl");
                if (System.IO.File.Exists(warnings))
                {
                    foreach (string line in System.IO.File.ReadAllLines(warnings))
                    {
                        if (string.IsNullOrWhiteSpace(line)) continue;
                        Newtonsoft.Json.Linq.JObject record = Newtonsoft.Json.Linq.JObject.Parse(line);
                        if ((record["kind"]?.ToString() == "prefab")
                            && (record["assetId"]?.ToString() ?? string.Empty)
                                .StartsWith("unresolved_", StringComparison.Ordinal))
                        {
                            prefabs++;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not count unresolved prefabs for the rip dialog: {ex.Message}");
            }

            try
            {
                string catalogue = Utils.IOUtils.CombinePath(directory, "areaprefabs.json");
                if (System.IO.File.Exists(catalogue))
                {
                    Newtonsoft.Json.Linq.JObject entries = Newtonsoft.Json.Linq.JObject.Parse(
                        System.IO.File.ReadAllText(catalogue));
                    foreach (Newtonsoft.Json.Linq.JProperty entry in entries.Properties())
                    {
                        if (entry.Value["unresolved"]?.ToObject<bool>() == true) surfaces++;
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not count unresolved surfaces for the rip dialog: {ex.Message}");
            }

            if ((prefabs == 0) && (surfaces == 0)) return null;

            List<string> parts = new();
            if (prefabs > 0) parts.Add($"{prefabs} prefab(s)");
            if (surfaces > 0) parts.Add($"{surfaces} ground surface(s)");
            return $"{string.Join(" and ", parts)} in this city come from asset packs that are "
                 + "not enabled — the game drew them as nothing, and so does this bundle. Enable "
                 + "them and re-rip for full fidelity.";
        }

        /// <summary>
        /// Drop all run state and fence off stale callbacks.
        /// </summary>
        private void Teardown()
        {
            _generation++;
            _stages = null;
            _plan = null;
            _stageDone = false;
            _stageOutcome = default;
            RipProgress.Clear();
        }
    }
}
