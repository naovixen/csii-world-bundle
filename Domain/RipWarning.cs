namespace Carto.Domain
{
    /// <summary>
    /// One line of the bundle's <c>warnings.jsonl</c>: something the producer could not do, said
    /// out loud.<br/>
    /// （束的 <c>warnings.jsonl</c> 中的一行：產生端無法完成之事的明確聲明。）<br/><br/>
    /// The contract (§10) treats this file as how a producer <i>declares</i> a gap rather than
    /// hides it. An <c>assetId</c> that <c>instances.jsonl</c> references with no <c>.glb</c>
    /// behind it is normally invalid — declared here, it validates as a warning and a consumer
    /// substitutes a stand-in instead of silently losing the object.<br/>
    /// （契約 §10 將此檔案視為產生端<i>宣告</i>缺口而非掩蓋缺口的方式。
    /// 被 <c>instances.jsonl</c> 參照卻沒有 <c>.glb</c> 的 <c>assetId</c> 原本無效，
    /// 但於此宣告後即驗證為警告，消費端會以替代物取代，而非默默遺失該物件。）
    /// </summary>
    public struct RipWarning
    {
        /// <summary>
        /// What kind of thing is missing — <c>mesh</c>, <c>material</c> or <c>texture</c>.
        /// （缺少的事物類別：<c>mesh</c>、<c>material</c> 或 <c>texture</c>。）
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The id or name a consumer would have looked for.（消費端原本會尋找的 ID 或名稱。）
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// Why it is not there.（其不存在的原因。）
        /// </summary>
        public string Reason { get; set; }

        /// <summary>
        /// Build a warning.（建立一筆警告。）
        /// </summary>
        /// <param name="kind">The kind of thing missing.（缺少的事物類別。）</param>
        /// <param name="assetId">The id or name.（ID 或名稱。）</param>
        /// <param name="reason">Why it is not there.（其不存在的原因。）</param>
        public RipWarning(string kind, string assetId, string reason)
        {
            Kind = kind;
            AssetId = assetId;
            Reason = reason;
        }
    }
}
