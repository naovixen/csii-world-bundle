using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One drawn lane in <c>lanemeshes.jsonl</c> (contract §5B v2): the parameters a consumer
    /// needs to bend the prefab's straight part meshes along the lane's curve, rather than the
    /// baked result.<br/>
    /// （<c>lanemeshes.jsonl</c> 中的一條被繪製車道（契約 §5B v2）：消費端沿車道曲線彎折
    /// 預製模板直線部件網格所需的參數，而非烘焙後的結果。）<br/><br/>
    /// <b>Why parameters, not geometry.</b> The first shipped form baked every lane into its own
    /// <c>.glb</c>: a full city produced 33,500 files and 8.6 GiB — 112 unique source meshes
    /// duplicated hundreds of times each, in breach of the repo's dedupe-by-prefab rule. The
    /// game itself stores exactly this parameter set and bends in the vertex shader; the bundle
    /// now mirrors that, with the bend algorithm documented in the contract and reference-
    /// implemented in the Blender harness.<br/>
    /// （<b>何以是參數而非幾何。</b>初版將每條車道烘焙為獨立的 <c>.glb</c>：全城產生 33,500 個
    /// 檔案、8.6 GiB——112 個相異來源網格各自重複數百次，違反本倉庫「依預製模板去重」的
    /// 準則。遊戲本身儲存的正是這組參數並於頂點著色器中彎折；束現依循之，
    /// 彎折演算法載於契約並於 Blender 匯入器中提供參考實作。）
    /// </summary>
    public class LaneGeometryRecord
    {
        /// <summary>The producer-local unique id, from the ECS entity index.（生產端唯一識別碼，取自 ECS 實體索引。）</summary>
        public int Id;

        /// <summary>The key into <c>laneprefabs.json</c>.（<c>laneprefabs.json</c> 的鍵。）</summary>
        public string Prefab;

        /// <summary>The catalogue entry's category, copied down for filtering without the join.（目錄項目的類別，為免除連接查詢而複寫於此。）</summary>
        public string Category;

        /// <summary>The owning edge or building's id, or <c>-1</c>.（所屬邊或建築的識別碼，無則為 <c>-1</c>。）</summary>
        public int Owner = -1;

        /// <summary>The lane's cubic bézier, four world-space control points.（車道的三次貝茲曲線，四個世界座標控制點。）</summary>
        public float3 CurveA, CurveB, CurveC, CurveD;

        /// <summary>Indices into the catalogue entry's <c>parts</c> this lane draws.（此車道所繪之目錄項目 <c>parts</c> 索引。）</summary>
        public List<int> Parts = new();

        /// <summary>The uncut curve-parameter spans; empty means the whole curve.（未被切除的曲線參數區間；空值代表整條曲線。）</summary>
        public List<float2> Spans = new();

        /// <summary>Width scale at the curve's start and end.（曲線起點與終點的寬度縮放。）</summary>
        public float2 WidthScale = new(1f, 1f);

        /// <summary>Whether the lane hangs, and the end-drop distances when it does.（車道是否懸垂，及懸垂時兩端的下垂距離。）</summary>
        public bool HasHang;
        public float2 HangDistances;

        /// <summary>The resolved paint, as instances carry it (contract §7.2), or <c>null</c>.（解析後的塗裝，與實例相同（契約 §7.2），或 <c>null</c>。）</summary>
        public object ColorSet;

        /// <summary>Whether the owning network runs below ground — the game draws the lane on its Tunnel cutaway layer.（所屬路網是否位於地下——遊戲將該車道繪製於其隧道剖視層。）</summary>
        public bool Tunnel;
    }
}
