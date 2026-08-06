using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that enumerates every placed object in the world into <c>instances.jsonl</c>.
    /// （將世界中所有已放置物件列舉至 <c>instances.jsonl</c> 的系統。）<br/><br/>
    /// Unlike mesh extraction, this carries no feasibility risk: a transform is plain ECS component
    /// data. Full placement is therefore guaranteed even where a mesh cannot be read.<br/>
    /// （不同於網格擷取，此處沒有可行性風險：變換僅是一般的 ECS 元件資料。因此即使網格無法讀取，
    /// 仍能保證取得完整的擺放資訊。）<br/><br/>
    /// <b>Sub-objects need no recursion.</b> <c>Game.Objects.SubObject</c> holds only an
    /// <c>Entity</c>, and that entity carries its own world-space <c>Transform</c> — street trees
    /// on a road and landscaping on a lot are ordinary entities. A single query over
    /// <c>Object + Transform + PrefabRef</c> therefore reaches all of them, and recursing the
    /// buffers as well would double-count. <c>Attached.m_Parent</c> still supplies the contract's
    /// <c>parent</c> link.<br/>
    /// （<b>子物件無須遞迴。</b><c>SubObject</c> 僅持有一個 <c>Entity</c>，而該實體帶有自身的世界座標
    /// <c>Transform</c>。單一查詢即可涵蓋全部，額外遞迴反而會重複計算。）
    /// </summary>
    public partial class InstanceRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed, static objects.（已放置的靜態物件查詢。）
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// What a prefab's meshes are, as far as decals are concerned.
        /// （就貼花而言，預製模板的網格屬於何種性質。）
        /// </summary>
        private readonly struct DecalInfo
        {
            /// <summary>Whether every mesh it draws is a decal.（其繪製的每個網格是否皆為貼花。）</summary>
            public bool IsDecal { get; }

            /// <summary>
            /// Which surfaces the game projects onto, across all of its decal meshes.
            /// （遊戲將其投影至哪些表面，涵蓋其所有貼花網格。）
            /// </summary>
            public Game.Rendering.DecalLayers Layers { get; }

            /// <summary>
            /// How tall the projector volume was before it was flattened to its painted quad.
            /// （投影體積於被壓平為所繪四邊形之前的高度。）<br/><br/>
            /// The mesh layer ships the quad, which is what anyone wants to draw; this is the one
            /// number that flattening discards, and it is what a consumer needs to re-project the
            /// marking onto a surface that is not flat.<br/>
            /// （網格層輸出的是四邊形，那是任何人想繪製的東西；此為壓平時唯一被捨棄的數值，
            /// 也是消費端若要將標線重新投影至非平坦表面時所需者。）
            /// </summary>
            public float Height { get; }

            public DecalInfo(bool isDecal, Game.Rendering.DecalLayers layers, float height)
            {
                IsDecal = isDecal;
                Layers = layers;
                Height = height;
            }
        }

        /// <summary>
        /// Which prefabs draw nothing but decals, and onto what, cached for one rip.
        /// （僅繪製貼花的預製模板及其投影對象，於單次擷取內快取。）
        /// </summary>
        readonly Dictionary<Entity, DecalInfo> _decalPrefabs = new();

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _objectQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Objects.Object>(),
                    ComponentType.ReadOnly<Game.Objects.Transform>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    // Moving things are vehicles and citizens, not world geometry; Deleted and Temp
                    // are mid-edit bookkeeping that must never reach the bundle.
                    // （會移動者為車輛與市民，並非世界幾何；Deleted 與 Temp 為編輯中的暫存狀態。）
                    ComponentType.ReadOnly<Moving>(),
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>(),

                    // Markers — spawn points, pedestrian access, pipe and cable nodes — are editor
                    // gizmos the game never draws. They are numerous enough to reach the top of the
                    // prefab usage table, so keeping them would scatter stand-in boxes across the
                    // whole reconstruction in exchange for nothing anyone can see.
                    // （標記物——生成點、行人出入口、管線與電纜節點——是遊戲從不繪製的編輯器示意物。
                    //   其數量足以躋身預製模板使用量前列，保留它們只會使重建成果佈滿無人可見的替代方塊。）
                    ComponentType.ReadOnly<Marker>(),

                    // Agents, not scenery. `Moving` only excludes the ones in motion at the instant
                    // of export — a citizen waiting at a stop and a parked car have no `Moving`, so
                    // they were being written into the bundle as world geometry. A level is the
                    // built environment; the traffic and pedestrians passing through it at the
                    // moment someone pressed the button are not part of it.
                    // （這些是行為者而非場景。`Moving` 僅排除輸出當下正在移動者：等車的市民與停放的車輛
                    //   皆無 `Moving`，因而被當作世界幾何寫入束中。關卡是既有的建成環境，
                    //   按下按鈕當下恰好經過的車流與行人並不屬於它。）
                    ComponentType.ReadOnly<Game.Creatures.Creature>(),
                    ComponentType.ReadOnly<Game.Vehicles.Vehicle>()
                }
            });

            base.OnCreate();
            _log.Debug("InstanceRipSystem instance created. 實例擷取系統實例創造完成。");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）
        /// </summary>
        protected override void OnDestroy() { base.OnDestroy(); }

        /// <summary>
        /// The event triggered when the system instance is updated.
        /// （當系統實例被更新時觸發的事件。）
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// Enumerate every placed object into <c>instances.jsonl</c>, recording the set of prefabs
        /// referenced so the mesh rip knows exactly what it must produce.<br/>
        /// （將所有已放置物件列舉至 <c>instances.jsonl</c>，並記錄所參照的預製模板集合，
        /// 使網格擷取確知必須產出哪些內容。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <param name="assetIds">Receives the distinct asset ids referenced.（接收所參照的相異資產 ID。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipInstances(string outputDirectory, HashSet<string> assetIds = null)
        {
            assetIds ??= new HashSet<string>();

            string path = IOUtils.CombinePath(outputDirectory, "instances.jsonl");
            StringBuilder report = new();
            Dictionary<string, int> byKind = new();
            int skippedNoPrefab = 0, withParent = 0, skippedUtilityNodes = 0;
            Dictionary<Entity, int> utilityNodesByPrefab = new();
            int stackExpanded = 0, stackRecords = 0, withColor = 0;
            List<string> skipSamples = new();
            HashSet<int> unresolvedPrefabs = new();
            HashSet<string> geometrylessPrefabs = new();
            _decalPrefabs.Clear();

            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                report.AppendLine($"Placed static objects: {entities.Length}");

                // Asset ids have to be assigned over the whole prefab set at once: CS2 does not
                // require prefab names to be unique, and two prefabs sharing an id would have the
                // mesh layer overwrite one with the other. Collect the distinct prefabs first, then
                // assign — MeshRipSystem walks the identical query and so derives identical ids.
                // （資產 ID 必須就整個預製模板集合一次指派：CS2 並不要求預製模板名稱唯一，
                //   而兩個共用 ID 的模板會使網格層以其中之一覆寫另一個。先收集相異模板再行指派——
                //   MeshRipSystem 走訪完全相同的查詢，因而推導出完全相同的 ID。）
                HashSet<Entity> prefabEntities = new();
                Dictionary<Entity, int> prefabUse = new();
                Dictionary<Entity, Entity> prefabSample = new();
                foreach (Entity entity in entities)
                {
                    if (MeshRipSystem.IsUtilityMarkerSubObject(EntityManager, entity)) continue;

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity != Entity.Null)
                    {
                        prefabEntities.Add(prefabEntity);
                        prefabUse.TryGetValue(prefabEntity, out int used);
                        prefabUse[prefabEntity] = used + 1;
                        if (used == 0) prefabSample[prefabEntity] = entity;
                    }
                }

                Census(prefabUse, report);
                Profile(prefabUse, prefabSample, report);

                Dictionary<Entity, MeshRipSystem.AssetIdEntry> assetIdTable =
                    MeshRipSystem.BuildAssetIds(EntityManager, prefabEntities, out int disambiguated);
                if (disambiguated > 0) report.AppendLine($"Disambiguated {disambiguated} prefab(s) sharing a name.");

                using InstanceWriter writer = new(path);

                foreach (Entity entity in entities)
                {
                    // Buried utility-network furniture: excluded here and in the mesh rip alike, so
                    // neither its placements nor its meshes reach the bundle.
                    // （埋於地下的管線網路配件：此處與網格擷取一併排除，
                    //   使其擺放與網格皆不會進入束中。）
                    if (MeshRipSystem.IsUtilityMarkerSubObject(EntityManager, entity))
                    {
                        skippedUtilityNodes++;

                        // Tallied by prefab, and named in the report. A silent count says the
                        // filter ran; it does not say it caught the right things, and the previous
                        // version of this filter ran happily while catching none of them. Naming
                        // what was dropped is what makes both failures visible — the nodes still
                        // getting through, and anything drawn that should not have been dropped.
                        // （依預製模板統計並於報告中具名。單純的計數只能說明篩選器執行過，
                        //   卻無法說明它攔下了正確的東西；而此篩選器的前一版正是一邊順利執行、
                        //   一邊一個也沒攔到。具名列出被丟棄者，才能使兩種失敗都顯現出來：
                        //   仍在漏網的節點，以及本不該被丟棄的可見物件。）
                        Entity nodePrefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                        if (nodePrefab != Entity.Null)
                        {
                            utilityNodesByPrefab.TryGetValue(nodePrefab, out int seen);
                            utilityNodesByPrefab[nodePrefab] = seen + 1;
                        }

                        continue;
                    }

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity == Entity.Null)
                    {
                        skippedNoPrefab++;
                        continue;
                    }

                    // An object whose managed prefab will not resolve is still a real object
                    // standing in the city, so keep its placement under a synthetic id rather than
                    // dropping it. PLAN §3 is explicit that placement must survive even when the
                    // geometry cannot: a consumer substitutes a stand-in mesh and every instance
                    // still lands in the right place. Dropping them silently is what breaks the
                    // contract's 1:1 fidelity requirement.
                    // （無法解析受管預製模板的物件，仍是城市中真實存在的物件，因此以合成 ID 保留其
                    //   擺放而非丟棄。PLAN §3 明確要求即使幾何無法取得，擺放仍須保留。）
                    MeshRipSystem.AssetIdEntry idEntry = assetIdTable[prefabEntity];
                    string assetId = idEntry.AssetId;
                    bool unresolved = !idEntry.Resolved;

                    // A prefab with no SubMesh buffer has no geometry to rip, and the mesh rip
                    // will never say so: its query requires SubMesh, so such a prefab is not
                    // enumerated at all and cannot be skipped with a reason. This is the only
                    // place both facts are in hand — the reference is being written here, and the
                    // prefab entity is right there to ask — so the gap is declared here. Parking
                    // lots, helipad airspace, area placeholders and intersections are all real
                    // objects with real placements and no mesh of their own.
                    // （沒有 SubMesh 緩衝區的預製模板無幾何可擷取，而網格擷取永遠不會言明此事：
                    //   其查詢要求 SubMesh，故此類預製模板根本不會被列舉，也就無法附帶原因跳過。
                    //   唯有此處同時掌握兩項事實——參照正於此寫出，而預製模板實體就在手邊——
                    //   因此於此宣告該缺口。停車場、停機坪空域、區域佔位物與交叉路口
                    //   皆是具有真實擺放、卻無自身網格的真實物件。）
                    if (!unresolved && !EntityManager.HasBuffer<SubMesh>(prefabEntity))
                        geometrylessPrefabs.Add(assetId);

                    if (unresolved)
                    {
                        skippedNoPrefab++;
                        unresolvedPrefabs.Add(prefabEntity.Index);

                        if (skipSamples.Count < 5)
                        {
                            skipSamples.Add($"entity {entity.Index} prefab {prefabEntity.Index} "
                                + $"hasPrefabData={EntityManager.HasComponent<PrefabData>(prefabEntity)} "
                                + $"[{string.Join(", ", DescribeComponents(entity))}]");
                        }
                    }

                    Game.Objects.Transform transform = EntityManager.GetComponentData<Game.Objects.Transform>(entity);
                    string kind = ClassifyKind(entity, prefabEntity);
                    if (EntityManager.HasComponent<Game.Objects.Color>(entity)) withColor++;

                    InstanceRecord record = new()
                    {
                        Id = entity.Index,
                        AssetId = assetId,
                        Kind = kind,
                        Position = transform.m_Position,
                        Rotation = transform.m_Rotation
                    };

                    // Owner is the sub-object relationship (a prop on a lot, a wing of a building).
                    // Attached is a different thing — an object bound to a network at a curve
                    // position — and using it as the parent link found only 98 of 1.4M objects.
                    // （Owner 才是子物件關係；Attached 表示物件綁定於路網上的某個曲線位置，
                    //   以其作為父子連結時，140 萬個物件中僅有 98 個成立。）
                    Entity parent = Entity.Null;
                    if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                        parent = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                    if ((parent == Entity.Null) && EntityManager.HasComponent<Attached>(entity))
                        parent = EntityManager.GetComponentData<Attached>(entity).m_Parent;

                    if (parent != Entity.Null)
                    {
                        record.Parent = parent.Index;
                        withParent++;
                    }

                    AddAttributes(entity, prefabEntity, kind, record);

                    // Flag it in the record too, so a consumer can tell a stand-in from a real
                    // asset without cross-referencing warnings.jsonl.
                    // （於紀錄中一併標記，使消費端無須交叉比對 warnings.jsonl 即可辨識替代資產。）
                    if (unresolved) record.Attributes["unresolved"] = true;

                    // A stacked object is not one placement but several, and how many is a property
                    // of this instance rather than of its prefab.
                    // （堆疊式物件並非單一擺放而是數個，且其數量取決於此實例而非其預製模板。）
                    int expanded = ExpandStack(entity, prefabEntity, record, writer, assetIds);
                    if (expanded > 0)
                    {
                        stackExpanded++;
                        stackRecords += expanded;
                        byKind.TryGetValue(kind, out int stacked);
                        byKind[kind] = stacked + expanded;
                        continue;
                    }

                    writer.Write(record);
                    assetIds.Add(assetId);
                    byKind.TryGetValue(kind, out int count);
                    byKind[kind] = count + 1;
                }

                report.AppendLine($"Wrote {writer.Count} instance(s) to instances.jsonl");
                if (withColor > 0)
                {
                    report.AppendLine($"  {withColor} instance(s) carry a per-instance colour choice "
                                    + "the bundle does not yet record");
                }
                if (stackExpanded > 0)
                {
                    report.AppendLine($"Expanded {stackExpanded} stacked object(s) into {stackRecords} "
                                    + "section record(s) — pillars, walls and fences reach their full height");
                }
                if (writer.NormalizedRotations > 0) report.AppendLine($"Normalised {writer.NormalizedRotations} non-unit rotation(s)");
            }
            finally
            {
                entities.Dispose();
            }

            foreach (KeyValuePair<string, int> kvp in byKind) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine($"Sub-objects with a parent link: {withParent}");
            if (skippedUtilityNodes > 0)
            {
                report.AppendLine($"Skipped {skippedUtilityNodes} buried utility-network node object(s), "
                                + $"{utilityNodesByPrefab.Count} distinct prefab(s):");

                List<KeyValuePair<Entity, int>> nodes = new(utilityNodesByPrefab);
                nodes.Sort((a, b) => b.Value.CompareTo(a.Value));

                for (int i = 0; (i < 12) && (i < nodes.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(nodes[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {nodes[i].Key.Index}" : name),-40} {nodes[i].Value,8}");
                }

                if (nodes.Count > 12) report.AppendLine($"  … and {nodes.Count - 12} more");
            }
            report.AppendLine($"Distinct assetIds referenced: {assetIds.Count}");
            if (skippedNoPrefab > 0)
            {
                report.AppendLine($"Unresolved prefabs: {unresolvedPrefabs.Count} distinct, {skippedNoPrefab} instance(s) kept under synthetic ids");
                WriteWarnings(outputDirectory, unresolvedPrefabs, geometrylessPrefabs, report);
                foreach (string sample in skipSamples) report.AppendLine($"  e.g. {sample}");
            }
            report.AppendLine($"Output: {path}");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Classify an entity into one of the contract's closed <c>kind</c> values.
        /// （將實體歸類為契約中封閉的 <c>kind</c> 值之一。）
        /// </summary>
        /// <param name="entity">The object entity.（物件實體。）</param>
        /// <returns>The kind.（分類。）</returns>
        /// <summary>
        /// Emit one record per section of a stacked object, or <c>0</c> if it is not stacked.
        /// （為堆疊式物件的每個區段輸出一筆紀錄；若非堆疊式則回傳 <c>0</c>。）<br/><br/>
        /// <b>What was wrong without this.</b> CS2 stores a pillar, a high wall or a long fence as
        /// a foot, one repeating section and a cap, and repeats the middle at draw time to span
        /// whatever the instance needs. <c>Vertical_Pillar</c>'s shaft is authored two metres tall;
        /// a viaduct pillar is ten or more. The bundle previously kept the single authored section,
        /// so every stacked object came out as a stub — visibly, highway pillars did not reach the
        /// ground.<br/>
        /// （<b>若無此處理則錯在何處。</b>CS2 將橋墩、高牆或長籬儲存為基座、一段可重複區段與頂蓋，
        /// 並於繪製時重複中段以填滿該實例所需的跨距。<c>Vertical_Pillar</c> 的柱身製作高度僅兩公尺，
        /// 而高架橋墩高逾十公尺。束先前只保留了單一製作區段，故所有堆疊式物件都成了殘段——
        /// 明顯可見的是，高速公路橋墩並未觸及地面。）<br/><br/>
        /// <b>The arithmetic is the game's, not a reimplementation.</b>
        /// <c>BatchDataHelpers.CalculateStackSubMeshData</c> is public, so the counts, offsets and
        /// per-section scales come from the same code the renderer uses. Reproducing them from a
        /// reading of the IL would have been a guess at exactly the kind of scaling term that is
        /// easy to get subtly wrong.<br/>
        /// （<b>此處的算術取自遊戲本身，而非重新實作。</b>
        /// <c>BatchDataHelpers.CalculateStackSubMeshData</c> 為公開方法，故數量、偏移與逐區段縮放
        /// 皆來自繪製器所用的同一段程式。由 IL 推導重寫，正是最容易在縮放項上出現細微錯誤之處。）<br/><br/>
        /// Each section becomes its own instance record with its world transform baked, which is
        /// what contract §4.1 already does for sub-objects — so this needs no schema change and no
        /// cooperation from a consumer.<br/>
        /// （每個區段成為帶有已烘焙世界變換的獨立實例紀錄，此即契約 §4.1 對子物件既有的處理方式，
        /// 故無須變更綱要，也無須消費端配合。）
        /// </summary>
        /// <param name="entity">The placed object.（已放置的物件。）</param>
        /// <param name="prefabEntity">Its prefab.（其預製模板。）</param>
        /// <param name="template">The record built for it, used as the pattern.（為其建立的紀錄，作為範本。）</param>
        /// <param name="writer">Where records go.（紀錄的輸出對象。）</param>
        /// <param name="assetIds">The set of referenced asset ids.（被參照的資產 ID 集合。）</param>
        /// <returns>How many records were written.（已寫出的紀錄筆數。）</returns>
        private int ExpandStack(Entity entity, Entity prefabEntity, InstanceRecord template,
                                InstanceWriter writer, HashSet<string> assetIds)
        {
            if (!EntityManager.HasComponent<Game.Objects.Stack>(entity)) return 0;
            if (!EntityManager.HasComponent<Game.Prefabs.StackData>(prefabEntity)) return 0;
            if (!EntityManager.HasBuffer<SubMesh>(prefabEntity)) return 0;

            Game.Objects.Stack stack = EntityManager.GetComponentData<Game.Objects.Stack>(entity);
            Game.Prefabs.StackData data = EntityManager.GetComponentData<Game.Prefabs.StackData>(prefabEntity);

            Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                stack, data, out int3 counts, out float3 offsets, out float3 scales);

            // Which sections this prefab actually has, and where each is authored relative to the
            // object's origin. The buffer is copied because reading it is all we need it for.
            // （此預製模板實際具備哪些區段，以及各區段相對於物件原點的製作位置。）
            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
            List<(StackRole Role, float3 Position, quaternion Rotation)> sections = new();
            for (int i = 0; i < buffer.Length; i++)
            {
                StackRole? role = PrefabMesh.RoleOf(buffer[i].m_Flags);
                if (role.HasValue) sections.Add((role.Value, buffer[i].m_Position, buffer[i].m_Rotation));
            }

            if (sections.Count == 0) return 0;

            int written = 0;
            foreach ((StackRole role, float3 localPosition, quaternion localRotation) in sections)
            {
                int tiles = role switch
                {
                    StackRole.First => counts.x,
                    StackRole.Middle => counts.y,
                    _ => counts.z
                };

                Game.Prefabs.SubMeshFlags flags = role switch
                {
                    StackRole.First => Game.Prefabs.SubMeshFlags.IsStackStart,
                    StackRole.Middle => Game.Prefabs.SubMeshFlags.IsStackMiddle,
                    _ => Game.Prefabs.SubMeshFlags.IsStackEnd
                };

                for (int tile = 0; tile < tiles; tile++)
                {
                    float3 position = localPosition;
                    float3 scale = new(1f);
                    Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData(
                        data, offsets, scales, tile, flags, ref position, ref scale);

                    // The section is authored in the object's local frame, so its world placement
                    // is the object's transform applied to it.
                    // （區段以物件的區域座標製作，故其世界擺放為物件變換作用於其上的結果。）
                    string partId = PrefabMesh.StackPartId(template.AssetId, role);
                    InstanceRecord section = new()
                    {
                        Id = template.Id,
                        AssetId = partId,
                        Kind = template.Kind,
                        Position = template.Position + math.mul(template.Rotation, position),
                        Rotation = math.mul(template.Rotation, localRotation),
                        Parent = template.Parent
                    };

                    foreach (KeyValuePair<string, object> attribute in template.Attributes)
                        section.Attributes[attribute.Key] = attribute.Value;

                    // Only where the game scales: an unscaled section must not acquire a scale of
                    // exactly one that a consumer then has to trust.
                    // （僅於遊戲確實縮放時寫出：未經縮放的區段不應被賦予一個消費端還得信任的 1。）
                    if (math.any(math.abs(scale - new float3(1f)) > 1e-4f)) section.Scale = scale;

                    writer.Write(section);
                    assetIds.Add(partId);
                    written++;
                }
            }

            return written;
        }

        /// <summary>
        /// Count what the world is made of, by the flags CS2 renders it with.
        /// （依 CS2 繪製時所用的旗標，統計世界的組成。）<br/><br/>
        /// This exists because "is anything else like the decals?" is a question that should be
        /// answered with numbers rather than by reading names and guessing. A decal's mesh is a
        /// projector volume, not a marking, and nothing in the bundle said so until it was looked
        /// for; the same could be true of impostors, of animated or skinned meshes, or of lights.
        /// Each is one flag, and a census over the prefab set is a few thousand lookups — nothing
        /// against a rip, and it turns the next question into a table.<br/>
        /// （此處存在的理由是：「還有沒有其他東西像貼花一樣？」這個問題應以數字回答，
        /// 而非讀名稱猜測。貼花的網格是投影體積而非標線，而在有人去找之前，束中沒有任何地方說明這點；
        /// 對於替身廣告板、動畫或蒙皮網格、以及燈光，情況可能相同。
        /// 每一項都只是一個旗標，而對預製模板集合做普查僅需數千次查詢——相對於一次擷取微不足道，
        /// 卻能把下一個問題化為一張表。）<br/><br/>
        /// Counted by instance as well as by prefab: a flag on one prefab used 34,000 times is a
        /// different problem from the same flag on a prefab used twice.<br/>
        /// （同時依實例與預製模板計數：某旗標出現在一個被使用 34,000 次的預製模板上，
        /// 與出現在只用兩次的模板上，是不同性質的問題。）
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.
        /// （每個被參照的預製模板及其實例數。）</param>
        /// <param name="report">The report to append the table to.（用以附加此表的報告。）</param>
        /// <summary>
        /// Print, for the prefabs the city holds most of, what a placed one and its prefab are
        /// actually made of.
        /// （針對城市中數量最多的預製模板，印出已放置實例與其預製模板究竟由哪些元件構成。）<br/><br/>
        /// <b>Why this exists.</b> "Stop these things being ripped" is a filter, and a filter needs
        /// a component to test. Three separate attempts to exclude the buried utility hookups
        /// (<c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>) were
        /// written against components those objects turned out not to have — the last one tested
        /// the <i>owner</i> for <c>Game.Net.Marker</c>, and their owner is the building whose lot
        /// they sit under, not a net at all. Each attempt looked plausible, compiled, ran, reported
        /// a count, and changed nothing.<br/>
        /// （<b>此處存在的理由。</b>「別再擷取這些東西」是一道篩選，而篩選需要可供測試的元件。
        /// 為排除埋設的公用設施接點，先後三次嘗試皆針對這些物件其實並不具備的元件而寫——
        /// 最後一次檢查<i>擁有者</i>是否為 <c>Game.Net.Marker</c>，
        /// 而其擁有者是它們所在地塊的建築，根本不是路網。
        /// 每次嘗試都看似合理、能編譯、會執行、也回報了計數，卻毫無作用。）<br/><br/>
        /// The list of component names on one instance and on its prefab is what would have ended
        /// that on the first attempt, and it costs two lookups per prefab printed.<br/>
        /// （單一實例與其預製模板的元件名稱清單，本可在第一次嘗試時就終結此事，
        /// 而每個印出的預製模板僅需兩次查詢。）
        /// </summary>
        /// <param name="prefabUse">Each referenced prefab and how many instances use it.（每個被參照的預製模板及其實例數。）</param>
        /// <param name="prefabSample">One placed instance per prefab.（每個預製模板的一個已放置實例。）</param>
        /// <param name="report">The report to append the profile to.（用以附加此剖析的報告。）</param>
        private void Profile(Dictionary<Entity, int> prefabUse, Dictionary<Entity, Entity> prefabSample,
                             StringBuilder report)
        {
            const int Top = 12;

            List<KeyValuePair<Entity, int>> ranked = new(prefabUse);
            ranked.Sort((a, b) => b.Value.CompareTo(a.Value));

            report.AppendLine($"What the {Math.Min(Top, ranked.Count)} most-placed prefab(s) are made of "
                            + "— the component names a filter can test:");

            for (int i = 0; (i < Top) && (i < ranked.Count); i++)
            {
                Entity prefab = ranked[i].Key;
                string name = Instance.Name.GetPrefabName(prefab);
                report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {prefab.Index}" : name)} × {ranked[i].Value}");

                if (prefabSample.TryGetValue(prefab, out Entity sample))
                    report.AppendLine($"    instance: {string.Join(", ", DescribeComponents(sample))}");

                report.AppendLine($"    prefab:   {string.Join(", ", DescribeComponents(prefab))}");

                // The owner is where the last filter went wrong, so name what it actually is.
                // （擁有者正是上一道篩選出錯之處，故明確指出它究竟是什麼。）
                if (prefabSample.TryGetValue(prefab, out Entity owned)
                    && EntityManager.HasComponent<Game.Common.Owner>(owned))
                {
                    Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(owned).m_Owner;
                    if (owner != Entity.Null)
                    {
                        string ownerName = EntityManager.HasComponent<PrefabRef>(owner)
                            ? Instance.Name.GetPrefabName(EntityManager.GetComponentData<PrefabRef>(owner).m_Prefab)
                            : null;

                        report.AppendLine($"    owner:    {(string.IsNullOrEmpty(ownerName) ? "?" : ownerName)}"
                                        + $" [{string.Join(", ", DescribeComponents(owner))}]");
                    }
                }
            }
        }

        private void Census(Dictionary<Entity, int> prefabUse, StringBuilder report)
        {
            (string Name, MeshFlags Flag)[] meshFlags =
            {
                ("decal", MeshFlags.Decal), ("impostor", MeshFlags.Impostor),
                ("animated", MeshFlags.Animated), ("skeleton", MeshFlags.Skeleton),
                ("character", MeshFlags.Character), ("prop", MeshFlags.Prop),
                ("stackX", MeshFlags.StackX), ("stackY", MeshFlags.StackY), ("stackZ", MeshFlags.StackZ)
            };

            (string Name, SubMeshFlags Flag)[] subMeshFlags =
            {
                ("outlineOnly", SubMeshFlags.OutlineOnly),
                ("stackStart", SubMeshFlags.IsStackStart),
                ("stackMiddle", SubMeshFlags.IsStackMiddle),
                ("stackEnd", SubMeshFlags.IsStackEnd),
                ("defaultMissingMesh", SubMeshFlags.DefaultMissingMesh)
            };

            Dictionary<string, (int Prefabs, int Instances)> tally = new();

            void Note(string name, int instances)
            {
                tally.TryGetValue(name, out (int Prefabs, int Instances) had);
                tally[name] = (had.Prefabs + 1, had.Instances + instances);
            }

            foreach (KeyValuePair<Entity, int> pair in prefabUse)
            {
                Entity prefab = pair.Key;
                int instances = pair.Value;

                // Light sources are components on the object prefab, not mesh flags: a lamp's
                // housing is ordinary geometry and the light it casts is separate data the bundle
                // does not carry at all.
                // （光源是物件預製模板上的元件而非網格旗標：燈具外殼為一般幾何，
                //   而其投射的光是束完全未承載的另一份資料。）
                // Per-instance colour: the prefab holds a palette of variations and each placed
                // object picks one. The bundle carries neither, so every copy of a prefab comes
                // out the same colour where the city shows a street of different ones.
                // （逐實例顏色：預製模板持有一組變化色盤，各放置物件從中擇一。
                //   束兩者皆未承載，故同一預製模板的每個複本顏色相同，而城市中實為一整排不同顏色。）
                // Checked on the object prefab and on its sub-meshes, because which of the two
                // holds the palette decides where a colour export would read from, and guessing
                // is how a whole feature gets built against the wrong entity.
                // （於物件預製模板及其子網格上皆檢查：兩者中何者持有色盤，
                //   決定顏色輸出應自何處讀取，而臆測正是整個功能建錯對象的成因。）
                if (EntityManager.HasBuffer<ColorVariation>(prefab)) Note("color:palette@prefab", instances);

                if (EntityManager.HasBuffer<ProceduralLight>(prefab)) Note("light:procedural", instances);
                if (EntityManager.HasComponent<LightEffectData>(prefab)) Note("light:effect", instances);
                if (EntityManager.HasComponent<StreetLightData>(prefab)) Note("light:street", instances);
                if (EntityManager.HasComponent<TrafficLightData>(prefab)) Note("light:traffic", instances);
                if (EntityManager.HasBuffer<LightAnimation>(prefab)) Note("light:animated", instances);

                if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefab, true);
                MeshFlags meshSeen = 0;
                SubMeshFlags subSeen = 0;
                bool paletteOnMesh = false, lightOnMesh = false;

                for (int i = 0; i < buffer.Length; i++)
                {
                    subSeen |= buffer[i].m_Flags;
                    Entity mesh = buffer[i].m_SubMesh;
                    if (mesh == Entity.Null) continue;

                    if (EntityManager.HasComponent<MeshData>(mesh))
                        meshSeen |= EntityManager.GetComponentData<MeshData>(mesh).m_State;

                    if (EntityManager.HasBuffer<ColorVariation>(mesh)) paletteOnMesh = true;
                    if (EntityManager.HasBuffer<ProceduralLight>(mesh)) lightOnMesh = true;
                }

                if (paletteOnMesh) Note("color:palette@submesh", instances);
                if (lightOnMesh) Note("light:procedural@submesh", instances);

                foreach ((string name, MeshFlags flag) in meshFlags)
                    if ((meshSeen & flag) != 0) Note("mesh:" + name, instances);

                foreach ((string name, SubMeshFlags flag) in subMeshFlags)
                    if ((subSeen & flag) != 0) Note("sub:" + name, instances);
            }

            if (tally.Count == 0) return;

            report.AppendLine($"Flag census over {prefabUse.Count} referenced prefab(s) — "
                            + "what else might need separating:");

            List<KeyValuePair<string, (int Prefabs, int Instances)>> rows = new(tally);
            rows.Sort((a, b) => b.Value.Instances.CompareTo(a.Value.Instances));
            foreach (KeyValuePair<string, (int Prefabs, int Instances)> row in rows)
                report.AppendLine($"  {row.Key,-24} {row.Value.Prefabs,6} prefab(s) {row.Value.Instances,9} instance(s)");
        }

        private string ClassifyKind(Entity entity, Entity prefabEntity)
        {
            if (EntityManager.HasComponent<Game.Buildings.Building>(entity)) return "building";

            // Trees and plants are both vegetation; the contract's closed enum has no separate
            // value for shrubs, and a consumer treats them the same way.
            // （樹木與植栽同屬植被；契約的封閉枚舉沒有灌木的獨立值，消費端亦以相同方式處理。）
            if (EntityManager.HasComponent<Tree>(entity) || EntityManager.HasComponent<Plant>(entity)) return "tree";

            if (DecalOf(prefabEntity).IsDecal) return "decal";

            return "prop";
        }

        /// <summary>
        /// Whether a prefab is a decal — a road marking, a parking bay, a patch of grime.
        /// （預製模板是否為貼花：道路標線、停車格、污漬。）<br/><br/>
        /// <b>Why this matters more than it looks.</b> A decal's mesh is not the marking, it is the
        /// <i>projector volume</i>: a twelve-triangle box, a metre tall, that CS2's shader projects
        /// the texture down onto whatever road surface lies inside it. A road arrow therefore
        /// arrives in a bundle as a transparent box floating over the carriageway. Left as
        /// <c>kind: "prop"</c>, a consumer building a drivable level turns thousands of those boxes
        /// into collision geometry and the road becomes impassable.<br/>
        /// （<b>此事的重要性超乎其表。</b>貼花的網格並非標線本身，而是<i>投影體積</i>：
        /// 一個十二個三角形、高一公尺的方盒，CS2 的著色器將貼圖沿其向下投影至其內的路面。
        /// 因此道路箭頭在束中呈現為浮於車道上方的透明方盒。若仍標為 <c>kind: "prop"</c>，
        /// 建構可行駛關卡的消費端會將數千個這樣的方盒轉為碰撞幾何，道路將無法通行。）<br/><br/>
        /// The signal is <c>MeshData.m_State</c>, the same flag the renderer switches on, rather
        /// than the shader-name match the material layer uses. Cached per prefab: a city holds a
        /// million objects and a few thousand prefabs.<br/>
        /// （判準取自 <c>MeshData.m_State</c>，即繪製器所依據的同一旗標，而非材質層所用的著色器名稱比對。
        /// 依預製模板快取：城市有百萬物件，但僅數千種預製模板。）
        /// </summary>
        /// <b>What it projects onto is the game's own answer, not a guess.</b>
        /// <c>MeshData.m_DecalLayer</c> is a <c>DecalLayers</c> mask — <c>Terrain</c>, <c>Roads</c>,
        /// <c>Buildings</c>, <c>Vehicles</c>, <c>Creatures</c>, <c>Other</c> — naming which surfaces
        /// receive the projection. Without it a consumer projecting "onto whatever is beneath"
        /// paints a road marking across the car parked on it and the wall behind it.<br/>
        /// （<b>其投影對象取自遊戲本身的答案，而非臆測。</b><c>MeshData.m_DecalLayer</c> 是一個
        /// <c>DecalLayers</c> 遮罩，指名哪些表面會接受該投影。若無此項，消費端「投影至下方之物」
        /// 將把道路標線畫到停在其上的車輛與其後的牆面上。）
        /// </summary>
        /// <param name="prefabEntity">The object prefab.（物件預製模板。）</param>
        /// <returns>Whether every mesh it draws is a decal, and onto what.（其繪製的每個網格是否皆為貼花，以及投影對象。）</returns>
        private DecalInfo DecalOf(Entity prefabEntity)
        {
            if (prefabEntity == Entity.Null) return new DecalInfo(false, 0, 0f);
            if (_decalPrefabs.TryGetValue(prefabEntity, out DecalInfo known)) return known;

            bool decal = false;
            Game.Rendering.DecalLayers layers = 0;
            float height = 0f;

            if (EntityManager.HasBuffer<SubMesh>(prefabEntity))
            {
                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                int meshes = 0, decals = 0;

                for (int i = 0; i < buffer.Length; i++)
                {
                    Entity mesh = buffer[i].m_SubMesh;
                    if ((mesh == Entity.Null) || !EntityManager.HasComponent<MeshData>(mesh)) continue;

                    meshes++;
                    MeshData data = EntityManager.GetComponentData<MeshData>(mesh);
                    if ((data.m_State & MeshFlags.Decal) == 0) continue;

                    decals++;
                    layers |= data.m_DecalLayer;
                    height = math.max(height, data.m_Bounds.max.y - data.m_Bounds.min.y);

                    // `MeshData.m_DecalLayer` is *not* where the renderer reads the mask from. It
                    // is zero on every decal in a full city — all 17,370 of them — which is why
                    // `decalLayers` came out absent on every record while the field was being read
                    // and the audit called it fixed. The value the shader is actually handed comes
                    // off the managed `DecalProperties` component:
                    // `ManagedBatchSystem.CreateBatch` and
                    // `RenderPrefabRenderer.Instance.SetDecalProperties` both set `_DecalLayerMask`
                    // from `DecalProperties.m_LayerMask`. Keep the ECS field as a fallback rather
                    // than replacing it: it costs nothing and a future build may start populating it.
                    // （`MeshData.m_DecalLayer` 並非繪製器讀取遮罩之處。全城 17,370 個貼花的該欄位
                    //   皆為零，此即該欄位確有被讀取、稽核亦稱已修正，而每筆紀錄的 `decalLayers`
                    //   卻仍付之闕如的緣故。著色器實際取得的值來自受管理的 `DecalProperties` 組件。
                    //   保留 ECS 欄位作為後備而非取而代之：此舉毫無成本，且日後的版本可能開始填入該值。）
                    if (EntityManager.HasComponent<PrefabData>(mesh)
                        && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(mesh),
                                                        out PrefabBase meshPrefab)
                        && (meshPrefab is RenderPrefab renderPrefab))
                    {
                        try
                        {
                            if (renderPrefab.TryGet(out DecalProperties properties)
                                && (properties != null) && properties.active)
                            {
                                layers |= properties.m_LayerMask;
                            }
                        }
                        catch (Exception ex)
                        {
                            _log.Warn($"Decal properties of `{renderPrefab.name}` did not resolve: {ex.Message}");
                        }
                    }
                }

                // Every drawn mesh, not merely one of them: a building with a grime decal on its
                // wall is still a building, and must keep its collision.
                // （須為所繪製的每個網格，而非其中之一：牆面帶有污漬貼花的建築仍是建築，必須保留碰撞。）
                decal = (meshes > 0) && (decals == meshes);
            }

            DecalInfo info = new(decal, layers, height);
            _decalPrefabs[prefabEntity] = info;
            return info;
        }

        /// <summary>
        /// Attach the semantic attributes that are cheap and reliable to read.
        /// （附加讀取成本低且可靠的語意屬性。）
        /// </summary>
        /// <param name="entity">The object entity.（物件實體。）</param>
        /// <param name="prefabEntity">Its prefab.（其預製模板。）</param>
        /// <param name="kind">The classification already decided for it.（已為其判定的分類。）</param>
        /// <param name="record">The record to populate.（待填入的紀錄。）</param>
        private void AddAttributes(Entity entity, Entity prefabEntity, string kind, InstanceRecord record)
        {
            // How far the object stands *above* the ground, not the ground's own height — the two
            // were confused in the contract's wording for a while, and the difference is the whole
            // value of the field. The game computes it as
            // `max(0, transform.y - SampleHeight(terrain, pos))` in
            // `ObjectToolBaseSystem.CreateDefinitionsJob`, so it is zero for anything sitting on
            // the ground and the component is simply absent for most objects.
            // （此為物件高出地面的距離，而非地面自身的高度；契約措辭曾一度混淆兩者，
            //   而其差別正是此欄位的全部價值。遊戲於
            //   `ObjectToolBaseSystem.CreateDefinitionsJob` 中以
            //   `max(0, transform.y - SampleHeight(terrain, pos))` 計算之，
            //   故凡置於地面者皆為零，且多數物件根本沒有此元件。）
            if (EntityManager.HasComponent<Game.Objects.Elevation>(entity))
            {
                Game.Objects.Elevation elevation = EntityManager.GetComponentData<Game.Objects.Elevation>(entity);
                record.Attributes["elevation"] = Math.Round((double)elevation.m_Elevation, 3);

                // The magnitude is clamped at zero, so it cannot express "below ground" at all.
                // `Lowered` is the only thing that says so, and a consumer cutting terrain for an
                // underpass needs it.
                // （其量值下限為零，因而完全無法表達「位於地面之下」。
                //   唯有 `Lowered` 能說明此事，而為地下道開挖地形的消費端需要它。）
                string[] flags = FlagNames(elevation.m_Flags.ToString());
                if (flags.Length > 0) record.Attributes["elevationFlags"] = flags;
            }

            // A decal is painted onto something, and which something is not a guess a consumer
            // should have to make — see the contract's §4.2a.
            // （貼花是被塗繪於某物之上的，而該物為何不應由消費端臆測——參見契約 §4.2a。）
            if (kind == "decal")
            {
                DecalInfo info = DecalOf(prefabEntity);

                string[] onto = FlagNames(info.Layers.ToString());
                if (onto.Length > 0) record.Attributes["decalLayers"] = onto;

                // The mesh is the painted quad, so the volume it was projected through is the one
                // thing the mesh layer no longer says. See `MeshRipSystem.TryFlattenDecal`.
                // （網格即所繪出的四邊形，故其投影所經的體積是網格層唯一不再表達之物。）
                if (info.Height > 0f) record.Attributes["decalHeight"] = math.round(info.Height * 1000f) / 1000f;
            }

            // Which of its prefab's colour variations this particular object was painted. CS2
            // chooses it once, by probability and a seed, and stores the answer here — so the
            // bundle records the answer rather than trying to reproduce the choice. Without it
            // every copy of a prefab comes out the same colour where the city shows a street of
            // different ones: 349,865 objects in a full city carry one.
            // （此特定物件被塗上其預製模板的哪一種顏色變化。CS2 依機率與種子選擇一次並將結果存於此處，
            //   故束記錄該結果，而非試圖重現該選擇。若無此項，同一預製模板的每個複本顏色皆相同，
            //   而城市中實為一整排不同顏色：完整城市中有 349,865 個物件帶有它。）
            if (EntityManager.HasComponent<Game.Objects.Color>(entity))
            {
                Game.Objects.Color color = EntityManager.GetComponentData<Game.Objects.Color>(entity);
                Dictionary<string, object> chosen = new()
                {
                    ["variation"] = color.m_Index,
                    ["value"] = Math.Round(color.m_Value / 255.0, 4)
                };

                if (color.m_SubColor) chosen["subColor"] = true;
                record.Attributes["color"] = chosen;
            }

            if (EntityManager.HasComponent<Tree>(entity))
            {
                Tree tree = EntityManager.GetComponentData<Tree>(entity);

                // m_Growth is a byte ramp; the contract defines growth as 0..1.
                // （m_Growth 為位元組級距，契約定義的 growth 為 0 至 1。）
                record.Attributes["growth"] = Math.Round(tree.m_Growth / 255d, 3);

                // Not `state`: the contract reserves that key for the building lifecycle enum, and
                // a tree's vocabulary (Teen/Adult/Elderly/Dead/Stump/Collected) is a different one.
                // A sapling has no flag set at all, where ToString() yields "0" — omit rather than
                // emit a meaningless value, since `growth` already carries the information.
                // （不使用 `state`：該鍵在契約中保留給建築生命週期枚舉，樹木的詞彙與之不同。
                //   幼苗未設任何旗標，ToString() 會得到 "0"，此時略過而非寫出無意義的值。）
                string treeState = tree.m_State.ToString();
                if (!string.IsNullOrEmpty(treeState) && !char.IsDigit(treeState[0]))
                    record.Attributes["treeState"] = treeState;
            }

            if (EntityManager.HasComponent<Game.Buildings.Building>(entity))
            {
                if (EntityManager.HasComponent<Game.Common.Destroyed>(entity)) record.Attributes["state"] = "destroyed";
                else if (EntityManager.HasComponent<Game.Buildings.Abandoned>(entity)) record.Attributes["state"] = "abandoned";
                else if (EntityManager.HasComponent<Game.Buildings.Condemned>(entity)) record.Attributes["state"] = "condemned";
                else if (EntityManager.HasComponent<Game.Objects.UnderConstruction>(entity)) record.Attributes["state"] = "construction";
                else record.Attributes["state"] = "normal";
            }
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// （將旗標列舉的名稱字串拆分為契約所定義的名稱陣列。）<br/><br/>
        /// Names rather than a bit field, for the same reason <c>lanes.jsonl</c> does it: the
        /// numeric values are CS2 internals that can change meaning between game versions, where a
        /// name that disappears is a name a consumer simply stops seeing. An enum with no bits set
        /// renders as <c>"0"</c>, which is not a name and is dropped.<br/>
        /// （採用名稱而非位元欄位，理由與 <c>lanes.jsonl</c> 相同：數值為 CS2 的內部值，
        /// 可能在版本間悄然改變意義；而消失的名稱只會讓消費端不再看到它。
        /// 未設任何位元的列舉會呈現為 <c>"0"</c>，那不是名稱，故予以捨棄。）
        /// </summary>
        /// <param name="flags">The enum's <c>ToString</c>.（列舉的 <c>ToString</c> 結果。）</param>
        /// <returns>The names, possibly empty.（各名稱，可能為空。）</returns>
        private static string[] FlagNames(string flags)
        {
            if (string.IsNullOrEmpty(flags) || char.IsDigit(flags[0])) return Array.Empty<string>();

            List<string> names = new();
            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) names.Add(trimmed);
            }

            return names.ToArray();
        }

        /// <summary>
        /// Write the bundle's <c>warnings.jsonl</c> layer, one line per thing the producer could
        /// not fully deliver.<br/>
        /// （寫出束的 <c>warnings.jsonl</c> 層，生產端每個無法完整產出的項目各佔一行。）<br/><br/>
        /// The contract defines this layer as advisory so a consumer may ignore it — its purpose is
        /// to make a stand-in asset explicable rather than mysterious.<br/>
        /// （契約將此層定義為建議性質，消費端可忽略；其用途是讓替代資產有跡可循。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root.（束的根目錄。）</param>
        /// <param name="unresolvedPrefabs">The prefab entity indices that would not resolve.（無法解析的預製模板實體索引。）</param>
        /// <param name="geometrylessPrefabs">Asset ids referenced here that no mesh will back.（此處參照、但不會有網格支撐的資產 ID。）</param>
        /// <param name="report">The report to note the outcome in.（用以記錄結果的報告。）</param>
        private void WriteWarnings(string outputDirectory, HashSet<int> unresolvedPrefabs,
                                   HashSet<string> geometrylessPrefabs, StringBuilder report)
        {
            string path = IOUtils.CombinePath(outputDirectory, "warnings.jsonl");

            try
            {
                string directory = System.IO.Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory)) System.IO.Directory.CreateDirectory(directory);

                // Keep every line this pass does not own, exactly as the mesh rip keeps ours.
                // Truncating instead made the file order-dependent: the mesh rip declares its
                // gaps and then a later instance pass erased all of them, so a bundle ripped
                // meshes-first reported 118 declarations and shipped 87. Every one of those
                // erased lines became an undeclared gap — a contract violation manufactured by
                // the writer rather than by anything missing.
                // （保留所有非本階段所擁有的行，正如網格擷取保留我們的行一樣。
                //   先前的截斷寫入使該檔案與執行順序相關：網格擷取宣告其缺口後，
                //   稍後的實例階段將其全數抹除，因此先擷取網格的束回報 118 筆宣告卻只輸出 87 筆。
                //   每一筆被抹除的行都成為未宣告的缺口——由寫出端而非任何缺失所製造的契約違規。）
                List<string> kept = new();
                if (System.IO.File.Exists(path))
                {
                    foreach (string line in System.IO.File.ReadAllLines(path))
                    {
                        if (string.IsNullOrWhiteSpace(line)) continue;
                        if (!IsOwnedByInstanceRip(line)) kept.Add(line);
                    }
                }

                using System.IO.StreamWriter writer = new(path, false, new System.Text.UTF8Encoding(false)) { NewLine = "\n" };
                foreach (string line in kept) writer.WriteLine(line);

                foreach (int prefabIndex in unresolvedPrefabs)
                {
                    Dictionary<string, object> warning = new()
                    {
                        ["kind"] = "prefab",
                        ["assetId"] = $"unresolved_{prefabIndex}",
                        ["reason"] = "managed prefab did not resolve; instances kept with placement only"
                    };
                    writer.WriteLine(Newtonsoft.Json.JsonConvert.SerializeObject(warning, Newtonsoft.Json.Formatting.None));
                }

                foreach (string assetId in geometrylessPrefabs)
                {
                    Dictionary<string, object> warning = new()
                    {
                        ["kind"] = "prefab",
                        ["assetId"] = assetId,
                        ["reason"] = "prefab has no SubMesh buffer, so no mesh is produced; "
                                   + "instances kept with placement only"
                    };
                    writer.WriteLine(Newtonsoft.Json.JsonConvert.SerializeObject(warning, Newtonsoft.Json.Formatting.None));
                }

                report.AppendLine($"Wrote warnings.jsonl ({unresolvedPrefabs.Count + geometrylessPrefabs.Count} entries: "
                                + $"{unresolvedPrefabs.Count} unresolved, {geometrylessPrefabs.Count} without geometry), "
                                + $"keeping {kept.Count} from the mesh pass");
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write warnings.jsonl: {ex.Message}");
            }
        }

        /// <summary>
        /// Whether a <c>warnings.jsonl</c> line is one this pass writes.
        /// （某 <c>warnings.jsonl</c> 行是否為本階段所寫出者。）<br/><br/>
        /// The instance pass owns <c>prefab</c>; the mesh pass owns <c>mesh</c>, <c>material</c>
        /// and <c>texture</c>. Each rewrites only its own kinds, so the two can run in either
        /// order, and re-running one does not double or erase the other's declarations.<br/>
        /// （實例階段擁有 <c>prefab</c>；網格階段擁有 <c>mesh</c>、<c>material</c> 與 <c>texture</c>。
        /// 各自僅重寫自身類別，故兩者可依任意順序執行，
        /// 且重跑其一不會使另一者的宣告重複或遭抹除。）
        /// </summary>
        /// <param name="line">One line of the file.（檔案中的一行。）</param>
        /// <returns>Whether this pass owns it.（本階段是否擁有該行。）</returns>
        private static bool IsOwnedByInstanceRip(string line)
        {
            try
            {
                return Newtonsoft.Json.Linq.JObject.Parse(line)["kind"]?.ToString() == "prefab";
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// List the component types present on an entity, for diagnostics.
        /// （列出實體上存在的元件型別，供診斷使用。）
        /// </summary>
        /// <param name="entity">The entity to describe.（待描述的實體。）</param>
        /// <returns>The type names.（型別名稱。）</returns>
        private List<string> DescribeComponents(Entity entity)
        {
            List<string> names = new();
            NativeArray<ComponentType> types = EntityManager.GetComponentTypes(entity, Allocator.TempJob);
            try
            {
                foreach (ComponentType type in types) names.Add(type.GetManagedType()?.Name ?? type.ToString());
            }
            finally
            {
                types.Dispose();
            }

            names.Sort(StringComparer.Ordinal);
            return names;
        }
    }
}
