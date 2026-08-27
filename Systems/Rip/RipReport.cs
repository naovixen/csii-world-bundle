using System;

using Colossal.Logging;

namespace Carto.Systems
{
    /// <summary>
    /// How a rip stage hands its result back: to whoever started it, or to the user.
    /// </summary>
    /// <remarks>
    /// <para>
    /// A stage is started one of two ways, and it must not care which. <c>RipOrchestratorSystem</c>
    /// starts it with a completion callback and folds the result into one combined report at the
    /// end of the run; a user pressing a single stage's own button starts it with no callback, and
    /// then the result has nowhere to go but a dialog.
    /// </para>
    /// <para>
    /// That "callback, else dialog" shape was written out EIGHT times — a success path and a
    /// failure path in each of <c>InstanceRipSystem</c>, <c>LaneRipSystem</c>,
    /// <c>MeshRipSystem</c> and <c>NetworkRipSystem</c> — and every copy carried the same two
    /// try/catch blocks for the same two reasons: a callback that throws must not take the stage
    /// down with it, and <c>ShowMessageDialog</c> throws when the UI bindings are not up (a rip
    /// started from the map editor, or during a load).
    /// </para>
    /// <para>
    /// <b>The two audiences get different text, and which text is the CALLER's decision.</b> That
    /// is why <paramref name="dialogText"/> is a separate argument rather than
    /// <c>outcome.Summary</c>: the instance and lane rips hand the orchestrator their full report
    /// and the modal a single line, the network rip does the exact opposite, and the mesh rip
    /// gives both the same thing. Three stages, three answers. Collapsing them here would have
    /// picked one and silently changed what two stages report, so the choice stays at each call
    /// site where it is visible and can be settled later on purpose.
    /// </para>
    /// </remarks>
    public static class RipReport
    {
        /// <summary>The dialog's title key — the mod's own options section.</summary>
        private const string DialogTitle = "Options.SECTION[Carto.Carto.Mod]";

        /// <summary>
        /// Deliver <paramref name="outcome"/> to <paramref name="completed"/> if there is one,
        /// otherwise put <paramref name="dialogText"/> on screen.
        /// </summary>
        /// <remarks>
        /// The dialog text goes through <see cref="Settings.Fit"/>, which caps it at 18 lines and
        /// points at the log for the rest. A modal cannot scroll, so a stage that handed its full
        /// report straight to one was showing a wall of text with the end cut off — the network
        /// rip and the orchestrator already fitted theirs and the other three did not.
        /// </remarks>
        /// <param name="completed">The starter's callback, or <c>null</c> for a hand-started run.</param>
        /// <param name="outcome">What the stage is reporting to whoever started it.</param>
        /// <param name="dialogText">What a person should read instead, when nobody is listening.</param>
        /// <param name="what">The stage, for the log line when a delivery fails ("mesh rip").</param>
        /// <param name="log">Where to note a delivery that could not be made.</param>
        public static void Deliver(
            Action<RipOutcome> completed,
            RipOutcome outcome,
            string dialogText,
            string what,
            ILog log)
        {
            // A pipeline-driven run reports into the pipeline's combined summary; only a run the
            // user started by hand gets its own dialog.
            if (completed != null)
            {
                try { completed(outcome); }
                catch (Exception ex) { log?.Warn($"{what} completion callback failed: {ex}"); }
                return;
            }

            try
            {
                Game.UI.MessageDialog dialog = new(DialogTitle, Settings.Fit(dialogText ?? outcome.Summary), "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                log?.Warn($"Could not show the {what} result dialog: {ex.Message}");
            }
        }
    }
}
