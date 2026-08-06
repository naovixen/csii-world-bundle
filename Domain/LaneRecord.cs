using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One lane of the drivable, walkable or railed network.（可行駛、可步行或軌道路網中的一條車道。）
    /// <br/><br/>
    /// A bundle's <c>networks/</c> layer is the road <i>surface</i> — where the asphalt is. This is
    /// the road <i>graph</i> — where a vehicle may go, in which direction, at what speed. They are
    /// different questions and a level needs both: the surface makes a city look right, the graph
    /// makes it drivable. CS2 keeps the graph as ordinary ECS data with no readback risk, so the
    /// only reason it was missing is that nothing had asked for it.<br/>
    /// （束的 <c>networks/</c> 層是道路<i>表面</i>，即柏油所在之處；此處則是道路<i>圖</i>，
    /// 即車輛可往何處、朝哪個方向、以何種速度行進。兩者是不同的問題，而一個關卡兩者皆需：
    /// 表面使城市外觀正確，圖則使其可供行駛。）
    /// </summary>
    public class LaneRecord
    {
        /// <summary>The lane entity's index, unique in the bundle.（車道實體索引，於束中唯一。）</summary>
        public int Id { get; set; }

        /// <summary>
        /// The edge or node that owns this lane, matching a <c>networks.jsonl</c> <c>id</c> where
        /// the owner is an edge.（擁有此車道的邊或節點；當擁有者為邊時，對應 <c>networks.jsonl</c> 的 <c>id</c>。）
        /// </summary>
        public int? Owner { get; set; }

        /// <summary>
        /// What may use it: <c>car</c>, <c>track</c>, <c>pedestrian</c>, <c>parking</c>,
        /// <c>connection</c> or <c>other</c>.
        /// （何者可使用：<c>car</c>、<c>track</c>、<c>pedestrian</c>、<c>parking</c>、<c>connection</c> 或 <c>other</c>。）
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The lane's path, world space, already tessellated from CS2's bézier.
        /// （車道路徑，世界座標，已自 CS2 的貝茲曲線細分。）
        /// </summary>
        public List<float3> Path { get; set; } = new();

        /// <summary>The curve's length in metres.（曲線長度，單位為公尺。）</summary>
        public float Length { get; set; }

        /// <summary>Speed limit in km/h, where the lane type has one.（速限，單位為公里/小時，若該車道型別具備。）</summary>
        public float? SpeedLimit { get; set; }

        /// <summary>
        /// How much the lane bends, as CS2 scores it — its own measure, carried rather than
        /// recomputed.（車道彎曲程度，依 CS2 自身的評分方式；此處沿用而非重新計算。）
        /// </summary>
        public float? Curviness { get; set; }

        /// <summary>
        /// Whether this is a secondary lane — one CS2 derives from a master lane rather than
        /// placing in its own right.（是否為次要車道，即 CS2 自主車道推導而來、而非獨立擺放者。）
        /// </summary>
        public bool Secondary { get; set; }

        /// <summary>
        /// The lane-type flags, as names. Open: a consumer ignores what it does not know.
        /// （車道型別旗標，以名稱表示。此為開放集合，消費端會忽略無法辨識者。）
        /// </summary>
        public List<string> Flags { get; set; } = new();
    }
}
