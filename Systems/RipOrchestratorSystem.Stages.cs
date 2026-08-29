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
    /// The stage list one Rip World press runs, and the two stages that are not a rip system.
    /// </summary>
    public partial class RipOrchestratorSystem
    {
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
    }
}
