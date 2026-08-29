using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Net;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The end of a lane rip: the report, the failure paths, and the §10 gaps.
    /// </summary>
    public partial class LaneRipSystem
    {
        /// <summary>
        /// Close the file, report, and hand the summary to whoever is listening.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void Finish(LaneRun run)
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
                _log.Warn($"Lane writer ended with an error: {ex}");
            }

            StringBuilder report = run.Report;

            // A writer that died after the walk finished lost queued records, so the stage must
            // not read as a success — the outcome this builds carries `Failed`, and the word in
            // the text is for the person reading the report, not for the orchestrator.
            //
            if (writerFaulted) report.Insert(0, "FAILED: the lane writer ended with an error — lanes.jsonl is incomplete; see the log.\n");
            report.AppendLine($"Wrote {run.Writer.Count} lane(s) to lanes.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
            foreach (KeyValuePair<string, int> pair in run.ByKind) report.AppendLine($"  {pair.Key}: {pair.Value}");
            report.AppendLine($"  {run.Secondary} derived from a master lane");
            if (run.Degenerate > 0) report.AppendLine($"Skipped {run.Degenerate} lane(s) with no usable curve");
            if (run.ProcessFailed > 0) report.AppendLine($"FAILED to record {run.ProcessFailed} lane(s) — see the log");
            if (run.SkippedStale > 0) report.AppendLine($"Skipped {run.SkippedStale} lane(s) the simulation removed mid-rip");

            if (run.DrawnLanes > 0)
            {
                int drawnPrefabs = 0;
                foreach (KeyValuePair<Entity, bool> pair in run.PrefabHasGeometry) if (pair.Value) drawnPrefabs++;

                report.AppendLine($"Lane geometry cross-check: {run.DrawnLanes} lane(s) over "
                                + $"{drawnPrefabs} prefab(s) draw a mesh along their curve "
                                + "(baked by the network rip into lanemeshes/, minus its counted skips). Largest:");

                List<KeyValuePair<Entity, int>> ranked = new(run.DrawnLanesByPrefab);
                ranked.Sort((a, b) => b.Value.CompareTo(a.Value));
                for (int i = 0; (i < 10) && (i < ranked.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(ranked[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {ranked[i].Key.Index}" : name),-40} {ranked[i].Value,8}");
                }
            }

            int laneCount = run.Writer.Count;
            Abandon(run);

            // Unconditional, matching the instance pass: a rip with nothing to declare still
            // has to clear the lane declarations a previous one left.
            //
            WriteWarnings(run, report);

            _log.Info(report.ToString());

            // As in the instance rip: the orchestrator takes the full report, the modal takes
            // the one line. See `RipReport.Deliver`.
            string dialogText = (writerFaulted ? "FAILED: the writer ended with an error — the file is incomplete.\n" : "")
                              + $"Wrote {laneCount} lane record(s) in {run.Clock.Elapsed.TotalSeconds:F1} s.\n\nFull report: Logs/Carto.log";

            RipReport.Deliver(
                run.Completed,
                writerFaulted || (run.ProcessFailed > 0)
                    ? RipOutcome.Failure(report.ToString())
                    : RipOutcome.Ok(report.ToString()),
                dialogText,
                "lane rip",
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
        private void Fail(LaneRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.WriterTask.Exception?.GetBaseException();
            _log.Error($"Lane writer faulted at {run.Index}/{run.Entities.Length} lane(s); aborting the pass: {run.WriterTask.Exception}");

            int written = run.Writer?.Count ?? 0;
            Abandon(run);

            string summary = $"FAILED: the lane writer faulted at {run.Index}/{run.Entities.Length} lane(s) — "
                           + $"lanes.jsonl is incomplete ({written} record(s) before the fault): "
                           + $"{cause?.Message ?? "unknown error"}\n\nFull details: Logs/Carto.log";

            RipReport.Deliver(run.Completed, RipOutcome.Failure(summary), summary, "lane rip", _log);
        }

        /// <summary>
        /// Release what a run holds open — the record file and the entity array.
        /// </summary>
        /// <param name="run">The run to release.</param>
        private static void Abandon(LaneRun run)
        {
            // Let the writer drain what it holds before the file is closed under it.
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
                _log.Warn("Lane writer still running after 10 s; leaving lanes.jsonl open rather than closing it mid-write.");
            }

            run.Writer = null;
            if (run.Entities.IsCreated) run.Entities.Dispose();
        }

        /// <summary>
        /// Declare this pass's gaps in the bundle's <c>warnings.jsonl</c> — one line per lane
        /// the simulation removed mid-rip or that failed to record, so a missing graph edge is
        /// explicable rather than mysterious (contract §10). The same ownership-merge scheme as
        /// the instance and mesh passes: only <c>kind: "lane"</c> lines are this pass's to
        /// replace, so the passes can run in any order without erasing each other.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <param name="report">The report to note the outcome in.</param>
        private void WriteWarnings(LaneRun run, StringBuilder report)
        {
            int kept = WarningsFile.Rewrite(
                run.OutputDirectory,
                line => WarningsFile.IsKind(line, WarningsFile.LaneKinds),
                LaneDeclarations(run));
            if (kept < 0) return;

            report.AppendLine($"Wrote {WarningsFile.FileName} ({run.StaleEntities.Count + run.FailedEntities.Count} lane entries: "
                            + $"{run.StaleEntities.Count} removed mid-rip, {run.FailedEntities.Count} failed), "
                            + $"keeping {kept} from the other passes");
        }

        /// <summary>
        /// This pass's §10 declarations, in write order. Both kinds name an ENTITY rather than an
        /// asset: a lane the simulation removed mid-rip no longer resolves to a prefab to ask for.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>One record per gap.</returns>
        private static IEnumerable<Dictionary<string, object>> LaneDeclarations(LaneRun run)
        {
            foreach (int entityId in run.StaleEntities)
            {
                yield return WarningsFile.ById("lane", entityId,
                    "removed by the simulation mid-rip; no record written");
            }

            foreach ((int entityId, string error) in run.FailedEntities)
            {
                yield return WarningsFile.ById("lane", entityId,
                    $"failed to record ({error}); no record written — see the log");
            }
        }
    }
}
