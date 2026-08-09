namespace Carto.Domain
{
    /// <summary>
    /// One line of the bundle's <c>warnings.jsonl</c>: something the producer could not do, said
    /// out loud.
    /// The contract (§10) treats this file as how a producer <i>declares</i> a gap rather than
    /// hides it. An <c>assetId</c> that <c>instances.jsonl</c> references with no <c>.glb</c>
    /// behind it is normally invalid — declared here, it validates as a warning and a consumer
    /// substitutes a stand-in instead of silently losing the object.
    /// </summary>
    public struct RipWarning
    {
        /// <summary>
        /// What kind of thing is missing — <c>mesh</c>, <c>material</c> or <c>texture</c>.
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The id or name a consumer would have looked for.
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// Why it is not there.
        /// </summary>
        public string Reason { get; set; }

        /// <summary>
        /// Build a warning.
        /// </summary>
        /// <param name="kind">The kind of thing missing.</param>
        /// <param name="assetId">The id or name.</param>
        /// <param name="reason">Why it is not there.</param>
        public RipWarning(string kind, string assetId, string reason)
        {
            Kind = kind;
            AssetId = assetId;
            Reason = reason;
        }
    }
}
