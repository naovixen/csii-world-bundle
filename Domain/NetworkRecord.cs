using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One network segment — a single line of the bundle's <c>networks.jsonl</c>.
    /// （單一路網區段，對應束中 <c>networks.jsonl</c> 的一行。）<br/><br/>
    /// The contract allows two geometry forms, a ripped mesh or a centerline the consumer extrudes.
    /// This producer writes the centerline form, because CS2 has no per-segment road mesh to rip:
    /// it draws a road by deforming a straight composition mesh along the edge's curve in the
    /// vertex shader. See <see cref="Carto.Systems.NetworkRipSystem"/> for the whole argument.<br/>
    /// （契約允許兩種幾何形式：擷取的網格，或由消費端擠出的中心線。本生產端寫出中心線形式，
    /// 因為 CS2 並無可供擷取的逐區段道路網格：遊戲繪製道路的方式，是在頂點著色器中
    /// 將一段筆直的組成網格沿邊的曲線變形。）<br/><br/>
    /// Coordinates are raw CS2 world values, exactly as in <see cref="InstanceRecord"/>:
    /// left-handed, Y-up, X east and Z north, in metres.<br/>
    /// （座標為 CS2 世界座標的原始值，與 <see cref="InstanceRecord"/> 相同：
    /// 左手座標系、Y 軸向上、X 向東、Z 向北，單位為公尺。）
    /// </summary>
    public class NetworkRecord
    {
        /// <summary>
        /// The producer-local unique id, taken from the ECS entity index.
        /// （生產端的唯一識別碼，取自 ECS 實體索引。）
        /// </summary>
        public int Id { get; set; }

        /// <summary>
        /// The contract's <c>kind</c>: <c>segment</c> or <c>node</c>.
        /// （契約定義的 <c>kind</c>：<c>segment</c> 或 <c>node</c>。）
        /// </summary>
        public string Kind { get; set; } = "segment";

        /// <summary>
        /// Mesh form only. The id of the assembled mesh under the bundle's network mesh layer.
        /// （僅用於網格形式。組裝後網格於束的路網網格層中的識別碼。）<br/><br/>
        /// When this is set the record carries no <see cref="Centerline"/>, which the schema
        /// enforces: a record is one form or the other, never both.<br/>
        /// （設定此值時，紀錄不帶 <see cref="Centerline"/>，此點由結構描述強制：
        /// 一筆紀錄只能是其中一種形式。）
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// Mesh form only. Where the mesh sits in the world, in metres.
        /// （僅用於網格形式。網格於世界中的位置，單位為公尺。）
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// Mesh form only. The mesh's world rotation.
        /// （僅用於網格形式。網格的世界旋轉。）<br/><br/>
        /// Always identity: a network mesh is assembled in world space and then shifted to its
        /// own origin, so there is no orientation left to express.<br/>
        /// （恆為單位旋轉：路網網格於世界座標中組裝後再平移至自身原點，故無方向可表示。）
        /// </summary>
        public quaternion Rotation { get; set; } = quaternion.identity;

        /// <summary>
        /// Centerline form only. The world-space polyline from the start node to the end node,
        /// already tessellated.<br/>
        /// （僅用於中心線形式。已細分的世界座標折線，自起點節點至終點節點。）
        /// </summary>
        public List<float3> Centerline { get; set; } = new();

        /// <summary>
        /// The total width in metres, to be extruded symmetrically about the centerline.
        /// （總寬度，單位為公尺，沿中心線對稱擠出。）
        /// </summary>
        public float Width { get; set; }

        /// <summary>
        /// Semantic attributes. Consumers ignore keys they do not recognise.
        /// （語意屬性。消費端會忽略無法辨識的鍵。）
        /// </summary>
        public Dictionary<string, object> Attributes { get; set; } = new();
    }
}
