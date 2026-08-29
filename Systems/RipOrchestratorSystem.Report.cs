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
    /// What one Rip World press reports when it ends, however it ended.
    /// </summary>
    public partial class RipOrchestratorSystem
    {
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
                Game.UI.MessageDialog message = new("Options.SECTION[Carto.Carto.Mod]", RipReport.Fit(dialog.ToString()), "Common.OK");
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
