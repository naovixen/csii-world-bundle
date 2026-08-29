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
    /// The end of an instance rip: the report, the failure paths, and the §10 gaps.
    /// </summary>
    public partial class InstanceRipSystem
    {
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

            // A writer that died after the walk finished lost queued records, so the stage must
            // not read as a success. The orchestrator is told so by `RipOutcome.Failed`; the text
            // still says it because a person reads this report.
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

            // The two audiences get different text on purpose: the orchestrator folds the FULL
            // report into its combined summary, and a person reading a modal gets the one line
            // that matters. `RipReport.Deliver` keeps that choice here rather than picking for us.
            string dialogText = (writerFaulted ? "FAILED: the writer ended with an error — the file is incomplete.\n" : "")
                              + $"Wrote {instanceCount} instance record(s), {run.AssetIds.Count} distinct assetId(s), "
                              + $"in {run.Clock.Elapsed.TotalSeconds:F1} s.\n\nFull report: Logs/Carto.log";

            RipReport.Deliver(
                run.Completed,
                writerFaulted || (run.ProcessFailed > 0)
                    ? RipOutcome.Failure(report.ToString())
                    : RipOutcome.Ok(report.ToString()),
                dialogText,
                "instance rip",
                _log);
        }

        /// <summary>
        /// Abort a rip whose writer task has died mid-walk. Nothing more can ever be written —
        /// the queue will not drain again — so the only honest outcome is a failed stage:
        /// release what the run holds and hand back a <see cref="RipOutcome"/> that SAYS it
        /// failed. The summary still opens with the word FAILED because a person reads it; the
        /// orchestrator does not — it used to infer failure from that spelling, which meant a
        /// stage writing "Failed" was silently a success.
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

            RipReport.Deliver(run.Completed, RipOutcome.Failure(summary), summary, "instance rip", _log);
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
