using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One placed object in the world — a single line of the bundle's <c>instances.jsonl</c>.
    /// （世界中的單一放置物件，對應束中 <c>instances.jsonl</c> 的一行。）<br/><br/>
    /// Positions and rotations are raw CS2 world values: left-handed, Y-up, X east and Z north,
    /// in metres. The bundle contract deliberately keeps these unconverted so that a position here
    /// equals the position the game itself reports, which is what makes spot-checking possible.
    /// Only mesh payloads are converted, and only on write.<br/>
    /// （位置與旋轉為 CS2 世界座標的原始值：左手座標系、Y 軸向上、X 向東、Z 向北，單位為公尺。
    /// 束契約刻意不轉換這些值，使其與遊戲回報的位置一致，以便逐一核對。）
    /// </summary>
    public class InstanceRecord
    {
        /// <summary>
        /// The producer-local unique id, taken from the ECS entity index.
        /// （生產端的唯一識別碼，取自 ECS 實體索引。）
        /// </summary>
        public int Id { get; set; }

        /// <summary>
        /// The <see cref="Id"/> of the owning instance, when this object came from an owner's
        /// sub-object buffer. <c>null</c> for free-standing objects.<br/>
        /// （當此物件源自擁有者的子物件緩衝區時，其擁有者的 <see cref="Id"/>；獨立物件則為 <c>null</c>。）<br/><br/>
        /// A consumer may ignore this entirely: <see cref="Position"/> and <see cref="Rotation"/>
        /// are world-space regardless, so parentage is reassembly information, not placement
        /// information.<br/>
        /// （消費端可完全忽略此欄位：位置與旋轉皆為世界座標，父子關係僅供重新組裝，與擺放無關。）
        /// </summary>
        public int? Parent { get; set; }

        /// <summary>
        /// The sanitised prefab name, resolving to <c>meshes/&lt;AssetId&gt;.glb</c>.
        /// （淨化後的預製模板名稱，對應至 <c>meshes/&lt;AssetId&gt;.glb</c>。）
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The contract's coarse class: <c>building</c>, <c>prop</c>, <c>tree</c>, <c>fence</c>,
        /// <c>decal</c> or <c>poi</c>.<br/>
        /// （契約定義的粗分類。）
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The world position in metres.（世界座標位置，單位為公尺。）
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// The world rotation as a unit quaternion.（世界座標旋轉，以單位四元數表示。）
        /// </summary>
        public quaternion Rotation { get; set; }

        /// <summary>
        /// Semantic attributes. Consumers ignore keys they do not recognise.
        /// （語意屬性。消費端會忽略無法辨識的鍵。）
        /// </summary>
        public Dictionary<string, object> Attributes { get; set; } = new();
    }
}
