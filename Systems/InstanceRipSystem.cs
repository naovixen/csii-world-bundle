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
                    ComponentType.ReadOnly<Game.Tools.Temp>()
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

                    string assetId = ResolveAssetId(prefabEntity, assetIdCache);
                    if (assetId == null)
                    {
                        skippedNoPrefab++;
                        continue;
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

                    if (EntityManager.HasComponent<Attached>(entity))
                    {
                        Entity parent = EntityManager.GetComponentData<Attached>(entity).m_Parent;
                        if (parent != Entity.Null)
                        {
                            record.Parent = parent.Index;
                            withParent++;
                        }
                    }

                    AddAttributes(entity, record);

                    writer.Write(record);
                    assetIds.Add(assetId);
                    byKind.TryGetValue(kind, out int count);
                    byKind[kind] = count + 1;
                }

                report.AppendLine($"Wrote {writer.Count} instance(s) to instances.jsonl");
            }
            finally
            {
                entities.Dispose();
            }

            foreach (KeyValuePair<string, int> kvp in byKind) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine($"Sub-objects with a parent link: {withParent}");
            report.AppendLine($"Distinct assetIds referenced: {assetIds.Count}");
            if (skippedNoPrefab > 0) report.AppendLine($"Skipped (unresolvable prefab): {skippedNoPrefab}");
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
                record.Attributes["state"] = tree.m_State.ToString();
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
