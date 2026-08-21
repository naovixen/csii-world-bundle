namespace Carto.Systems
{
    /// <summary>
    /// What one rip stage has to say when it is done: whether it failed, and the report.
    /// </summary>
    /// <remarks>
    /// This used to be a <c>string</c>, and whether a stage had failed was decided by
    /// <c>RecordStage</c> looking for a line that <c>StartsWith("FAILED", Ordinal)</c>. Twelve
    /// sites across five systems produced that prefix independently and one site consumed it, so
    /// the contract between them was a spelling: a stage that wrote <c>"Failed"</c> — which every
    /// one of these systems already writes to its LOG — would have been recorded as a success,
    /// and nothing would have said otherwise. The orchestrator now reads a bool.
    ///
    /// <para>
    /// The prefix itself stays in the text. Users read these reports, and "FAILED: …" is how the
    /// mod has always said so; <see cref="Failure"/> is what puts it there, once, instead of
    /// twelve string literals agreeing by hand.
    /// </para>
    ///
    /// <para>
    /// A stage can fail PARTIALLY and must still report everything it did — the Clean stage's
    /// "FAILED to remove …" line comes after its "Removed …" line (audit I-F7). That is why this
    /// carries the whole summary rather than an error message: the flag says what happened, the
    /// summary says what was done.
    /// </para>
    /// </remarks>
    public readonly struct RipOutcome
    {
        private RipOutcome(bool failed, string summary)
        {
            Failed = failed;
            Summary = summary ?? string.Empty;
        }

        /// <summary>Whether this stage failed, in whole or in part.</summary>
        public bool Failed { get; }

        /// <summary>The stage's report, shown in the end-of-rip dialog and written to the log.</summary>
        public string Summary { get; }

        /// <summary>A stage that did what it was asked.</summary>
        /// <param name="summary">Its report.</param>
        /// <returns>The outcome.</returns>
        public static RipOutcome Ok(string summary) => new(false, summary);

        /// <summary>
        /// A stage that failed. The report is prefixed <c>FAILED:</c> unless it already says so —
        /// a partial failure builds its own multi-line report and puts the word where it belongs.
        /// </summary>
        /// <param name="summary">Its report, or the reason.</param>
        /// <returns>The outcome.</returns>
        public static RipOutcome Failure(string summary)
        {
            summary ??= "unknown error";
            bool announced = summary.StartsWith("FAILED", System.StringComparison.Ordinal)
                          || summary.Contains("\nFAILED");
            return new RipOutcome(true, announced ? summary : "FAILED: " + summary);
        }
    }
}
