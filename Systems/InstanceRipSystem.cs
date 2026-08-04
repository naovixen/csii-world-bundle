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
            Dictionary<string, string> assetIdCache = new();
            int skippedNoPrefab = 0, withParent = 0;
            List<string> skipSamples = new();
            HashSet<int> unresolvedPrefabs = new();

            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                report.AppendLine($"Placed static objects: {entities.Length}");

                using InstanceWriter writer = new(path);

                foreach (Entity entity in entities)
                {
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
                    string assetId = ResolveAssetId(prefabEntity, assetIdCache);
                    bool unresolved = assetId == null;
                    if (unresolved)
                    {
                        assetId = $"unresolved_{prefabEntity.Index}";
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
                    string kind = ClassifyKind(entity);

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

                    AddAttributes(entity, record);

                    // Flag it in the record too, so a consumer can tell a stand-in from a real
                    // asset without cross-referencing warnings.jsonl.
                    // （於紀錄中一併標記，使消費端無須交叉比對 warnings.jsonl 即可辨識替代資產。）
                    if (unresolved) record.Attributes["unresolved"] = true;

                    writer.Write(record);
                    assetIds.Add(assetId);
                    byKind.TryGetValue(kind, out int count);
                    byKind[kind] = count + 1;
                }

                report.AppendLine($"Wrote {writer.Count} instance(s) to instances.jsonl");
                if (writer.NormalizedRotations > 0) report.AppendLine($"Normalised {writer.NormalizedRotations} non-unit rotation(s)");
            }
            finally
            {
                entities.Dispose();
            }

            foreach (KeyValuePair<string, int> kvp in byKind) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine($"Sub-objects with a parent link: {withParent}");
            report.AppendLine($"Distinct assetIds referenced: {assetIds.Count}");
            if (skippedNoPrefab > 0)
            {
                report.AppendLine($"Unresolved prefabs: {unresolvedPrefabs.Count} distinct, {skippedNoPrefab} instance(s) kept under synthetic ids");
                WriteWarnings(outputDirectory, unresolvedPrefabs, report);
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
        private string ClassifyKind(Entity entity)
        {
            if (EntityManager.HasComponent<Game.Buildings.Building>(entity)) return "building";

            // Trees and plants are both vegetation; the contract's closed enum has no separate
            // value for shrubs, and a consumer treats them the same way.
            // （樹木與植栽同屬植被；契約的封閉枚舉沒有灌木的獨立值，消費端亦以相同方式處理。）
            if (EntityManager.HasComponent<Tree>(entity) || EntityManager.HasComponent<Plant>(entity)) return "tree";

            return "prop";
        }

        /// <summary>
        /// Attach the semantic attributes that are cheap and reliable to read.
        /// （附加讀取成本低且可靠的語意屬性。）
        /// </summary>
        /// <param name="entity">The object entity.（物件實體。）</param>
        /// <param name="record">The record to populate.（待填入的紀錄。）</param>
        private void AddAttributes(Entity entity, InstanceRecord record)
        {
            if (EntityManager.HasComponent<Game.Objects.Elevation>(entity))
                record.Attributes["elevation"] = Math.Round((double)EntityManager.GetComponentData<Game.Objects.Elevation>(entity).m_Elevation, 3);

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
        /// Write the bundle's <c>warnings.jsonl</c> layer, one line per thing the producer could
        /// not fully deliver.<br/>
        /// （寫出束的 <c>warnings.jsonl</c> 層，生產端每個無法完整產出的項目各佔一行。）<br/><br/>
        /// The contract defines this layer as advisory so a consumer may ignore it — its purpose is
        /// to make a stand-in asset explicable rather than mysterious.<br/>
        /// （契約將此層定義為建議性質，消費端可忽略；其用途是讓替代資產有跡可循。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root.（束的根目錄。）</param>
        /// <param name="unresolvedPrefabs">The prefab entity indices that would not resolve.（無法解析的預製模板實體索引。）</param>
        /// <param name="report">The report to note the outcome in.（用以記錄結果的報告。）</param>
        private void WriteWarnings(string outputDirectory, HashSet<int> unresolvedPrefabs, StringBuilder report)
        {
            string path = IOUtils.CombinePath(outputDirectory, "warnings.jsonl");

            try
            {
                string directory = System.IO.Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory)) System.IO.Directory.CreateDirectory(directory);

                using System.IO.StreamWriter writer = new(path, false, new System.Text.UTF8Encoding(false)) { NewLine = "\n" };
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

                report.AppendLine($"Wrote warnings.jsonl ({unresolvedPrefabs.Count} entries)");
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write warnings.jsonl: {ex.Message}");
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

        /// <summary>
        /// Resolve a prefab entity to its sanitised asset id, caching by entity.
        /// （將預製模板實體解析為淨化後的資產 ID，並依實體快取。）<br/><br/>
        /// A city has hundreds of thousands of objects across a few thousand prefabs, so the
        /// managed prefab lookup is worth doing once per prefab rather than once per object.<br/>
        /// （城市中數十萬個物件僅使用數千個預製模板，故每個預製模板僅查詢一次。）
        /// </summary>
        private string ResolveAssetId(Entity prefabEntity, Dictionary<string, string> cache)
        {
            string key = prefabEntity.Index.ToString();
            if (cache.TryGetValue(key, out string cached)) return cached;

            string assetId = null;
            if (EntityManager.HasComponent<PrefabData>(prefabEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefabEntity), out PrefabBase prefab)
                && (prefab != null))
            {
                assetId = MeshRipSystem.SanitizeAssetId(prefab.name);
            }

            cache[key] = assetId;
            return assetId;
        }
    }
}
