using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One rigid part of a <see cref="PrefabMesh"/>: a mesh plus its fixed offset within the prefab.
    /// （<see cref="PrefabMesh"/> 的單一剛性部件：一個網格加上其在預製模板內的固定偏移。）<br/><br/>
    /// This comes from one entry of the prefab entity's <c>Game.Prefabs.SubMesh</c> buffer. The
    /// transform is <b>still in CS2's left-handed frame</b>; <see cref="Carto.IO.GltfWriter"/>
    /// mirrors it on write, exactly as it does the vertices.<br/>
    /// （來源為預製模板實體 <c>Game.Prefabs.SubMesh</c> 緩衝區中的一筆條目。此變換<b>仍為 CS2 的左手座標系</b>，
    /// 由 <see cref="Carto.IO.GltfWriter"/> 於寫出時鏡射，與頂點的處理方式相同。）
    /// </summary>
    public class PrefabMeshPart
    {
        /// <summary>
        /// The part's geometry.（部件的幾何。）
        /// </summary>
        public MeshAsset Mesh { get; set; }

        /// <summary>
        /// The part's offset from the prefab origin, in metres.
        /// （部件相對於預製模板原點的偏移，單位為公尺。）
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// The part's rotation about its own origin.（部件繞自身原點的旋轉。）
        /// </summary>
        public quaternion Rotation { get; set; } = quaternion.identity;

        /// <summary>
        /// The glTF node name for this part, unique within the prefab.
        /// （此部件的 glTF 節點名稱，於預製模板內唯一。）
        /// </summary>
        public string NodeName { get; set; }

        /// <summary>
        /// The part's per-axis scale, or <c>null</c> for none.（部件的逐軸縮放；無縮放時為 <c>null</c>。）<br/><br/>
        /// Only the obsolete-content placeholder uses this: the renderer draws that box stretched
        /// to the prefab's <c>ObjectGeometryData.m_Bounds</c>
        /// (<c>BatchDataSystem.UpdateObjectData</c>, the <c>DefaultMissingMesh</c> branch), and the
        /// stretch is a property of the prefab, so it bakes into the part's node.<br/>
        /// （僅過時內容的佔位網格使用此欄位：繪製器將該方盒拉伸至預製模板的
        /// <c>ObjectGeometryData.m_Bounds</c>，且該拉伸屬於預製模板本身，故烘焙至部件節點。）
        /// </summary>
        public float3? Scale { get; set; }

        /// <summary>
        /// Which role this part plays in a stacked prefab, or <c>null</c> for an ordinary part.
        /// （此部件於堆疊式預製模板中所扮演的角色；一般部件為 <c>null</c>。）<br/><br/>
        /// CS2 does not store a tall pillar, a high wall or a long fence — it stores a foot, one
        /// repeating section and a cap, and repeats the middle at draw time to span whatever the
        /// instance needs (<c>Game.Objects.Stack.m_Range</c>). A prefab carrying these is therefore
        /// not one mesh but three, and the bundle has to be able to place them independently.<br/>
        /// （CS2 不儲存高聳的橋墩、牆或長籬——它儲存一個基座、一段可重複的區段與一個頂蓋，
        /// 並於繪製時重複中段以填滿該實例所需的跨距。帶有這些部件的預製模板因而並非一個網格
        /// 而是三個，束必須能夠獨立擺放它們。）
        /// </summary>
        public StackRole? StackRole { get; set; }
    }

    /// <summary>
    /// The three parts of a stacked prefab, mirroring <c>SubMeshFlags.IsStack*</c>.
    /// （堆疊式預製模板的三個部分，對應 <c>SubMeshFlags.IsStack*</c>。）
    /// </summary>
    public enum StackRole
    {
        /// <summary>The foot, drawn once at the bottom.（基座，於底部繪製一次。）</summary>
        First,

        /// <summary>The repeating section.（可重複的區段。）</summary>
        Middle,

        /// <summary>The cap, drawn once at the top.（頂蓋，於頂部繪製一次。）</summary>
        Last
    }

    /// <summary>
    /// One unique object prefab, assembled from every sub-mesh it draws, ready to be written as the
    /// single <c>&lt;assetId&gt;.glb</c> the bundle contract requires.<br/>
    /// （單一唯一物件預製模板，由其繪製的所有子網格組裝而成，可寫出為束契約所要求的單一 <c>&lt;assetId&gt;.glb</c>。）<br/><br/>
    /// <b>The prefab, not the mesh, is the unit of the bundle.</b> <c>instances.jsonl</c> references
    /// <c>NA_ResidentialMediumSignature01</c>, so that is the file name, and the render prefabs
    /// hanging off its <c>SubMesh</c> buffer become nodes inside it (contract §6). Phase 1's spike
    /// wrote one file per mesh entity instead, which is why its output did not join up.<br/>
    /// （<b>束的單位是預製模板而非網格。</b><c>instances.jsonl</c> 參照的是物件預製模板名稱，
    /// 因此檔名以其為準，而掛在其 <c>SubMesh</c> 緩衝區下的繪製預製模板則成為其中的節點。）<br/><br/>
    /// Sub-<i>objects</i> — a building's wings, street trees on a road — are a different thing and
    /// are <b>not</b> here: they are separate entities with their own world transforms and are
    /// emitted as their own instance records (contract §4.1).<br/>
    /// （子<i>物件</i>——建築的側翼、道路上的行道樹——則屬另一回事，<b>不</b>在此處：
    /// 它們是帶有自身世界變換的獨立實體，會輸出為各自的實例紀錄。）
    /// </summary>
    public class PrefabMesh
    {
        /// <summary>
        /// The sanitised prefab name, and the bundle's join key. Becomes
        /// <c>meshes/&lt;AssetId&gt;.glb</c>.<br/>
        /// （淨化後的預製模板名稱，亦即束的連結鍵，將成為 <c>meshes/&lt;AssetId&gt;.glb</c>。）
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The <c>assetId</c> of one part of a stacked prefab.（堆疊式預製模板中某一部分的 <c>assetId</c>。）
        /// <br/><br/>
        /// A pure function of the prefab's id and the role, so that <see cref="Systems.MeshRipSystem"/>
        /// and <see cref="Systems.InstanceRipSystem"/> agree on the name without having to share
        /// state or ordering — the instance pass runs first and never sees the mesh pass's output.
        /// The result stays inside the contract's <c>assetId</c> pattern.<br/>
        /// （此為預製模板 ID 與角色的純函數，使網格擷取與實例擷取無須共享狀態或次序即可對名稱取得一致
        /// ——實例階段先執行，且從未見過網格階段的輸出。結果仍符合契約的 <c>assetId</c> 樣式。）
        /// </summary>
        /// <param name="assetId">The prefab's own id.（預製模板自身的 ID。）</param>
        /// <param name="role">Which part.（哪一部分。）</param>
        /// <returns>The part's asset id.（該部分的資產 ID。）</returns>
        public static string StackPartId(string assetId, StackRole role) => role switch
        {
            StackRole.First => $"{assetId}__stack_first",
            StackRole.Middle => $"{assetId}__stack_mid",
            _ => $"{assetId}__stack_last"
        };

        /// <summary>
        /// The <c>assetId</c> of a stacked prefab's parts that carry no stack role, drawn once per
        /// instance rather than per tile. Same purity contract as <see cref="StackPartId"/>.<br/>
        /// （堆疊式預製模板中不帶堆疊角色部件的 <c>assetId</c>，每個實例繪製一次而非隨鋪排重複。
        /// 純函數性質與 <see cref="StackPartId"/> 相同。）
        /// </summary>
        /// <param name="assetId">The prefab's own id.（預製模板自身的 ID。）</param>
        /// <returns>The rest-parts asset id.（其餘部件的資產 ID。）</returns>
        public static string StackRestId(string assetId) => $"{assetId}__stack_rest";

        /// <summary>
        /// The stack role a <c>SubMesh</c>'s flags declare, or <c>null</c> if it is an ordinary
        /// part.（子網格旗標所宣告的堆疊角色；若為一般部件則為 <c>null</c>。）
        /// </summary>
        /// <param name="flags">The sub-mesh flags.（子網格旗標。）</param>
        /// <returns>The role, or <c>null</c>.（角色，或 <c>null</c>。）</returns>
        public static StackRole? RoleOf(Game.Prefabs.SubMeshFlags flags)
        {
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackStart) != 0) return StackRole.First;
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackMiddle) != 0) return StackRole.Middle;
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackEnd) != 0) return StackRole.Last;
            return null;
        }

        /// <summary>
        /// The prefab's original, unsanitised name.（預製模板未經淨化的原始名稱。）
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// The parts drawn together to make this prefab.（共同繪製以構成此預製模板的部件。）
        /// </summary>
        public List<PrefabMeshPart> Parts { get; set; } = new();

        /// <summary>
        /// The distinct stack roles among this prefab's parts, in first-middle-last order, or
        /// empty if it is not a stacked prefab.
        /// （此預製模板部件中出現的相異堆疊角色，依基座、中段、頂蓋排序；非堆疊式時為空。）
        /// </summary>
        /// <returns>The roles present.（出現的角色。）</returns>
        public IEnumerable<StackRole> StackRoles()
        {
            foreach (StackRole role in new[] { StackRole.First, StackRole.Middle, StackRole.Last })
            {
                foreach (PrefabMeshPart part in Parts)
                {
                    if (part.StackRole == role) { yield return role; break; }
                }
            }
        }

        /// <summary>
        /// How many placed objects in the city use this prefab. Used to rip the prefabs that cover
        /// the most of the city first when the run is capped.<br/>
        /// （城市中使用此預製模板的已放置物件數量。用於在限制擷取數量時，優先擷取覆蓋城市最多的預製模板。）
        /// </summary>
        public int InstanceCount { get; set; }

        /// <summary>
        /// The number of sub-mesh entries skipped because they are alternative state variants
        /// (a dead tree, a left-hand-traffic marking) rather than parts drawn together.<br/>
        /// （因屬替代狀態變體（枯樹、左駕標線）而非共同繪製的部件，而被略過的子網格條目數量。）
        /// </summary>
        public int SkippedVariants { get; set; }

        /// <summary>
        /// The total triangles across every part.（所有部件的三角形總數。）
        /// </summary>
        public int TriangleCount
        {
            get
            {
                int total = 0;
                foreach (PrefabMeshPart part in Parts) total += part.Mesh?.TriangleCount ?? 0;
                return total;
            }
        }
    }
}
