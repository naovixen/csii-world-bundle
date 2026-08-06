using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Net;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that writes the drivable graph — every lane, its path and what may use it —
    /// into <c>lanes.jsonl</c>.
    /// （將可行駛圖——每條車道、其路徑與可使用者——寫入 <c>lanes.jsonl</c> 的系統。）<br/><br/>
    /// <b>Why this is a layer of its own.</b> <c>networks/</c> carries the road surface: where the
    /// asphalt is, what it is made of, how it meets a junction. It says nothing about where a
    /// vehicle may drive, in which direction, or how fast — and a level that only has the surface
    /// is scenery, not a road network. CS2 keeps the graph as plain ECS components with no
    /// readback risk, so this is among the cheapest useful things in the game to extract.<br/>
    /// （<b>此層獨立存在之由。</b><c>networks/</c> 承載道路表面：柏油何在、由何構成、如何與路口相接；
    /// 它並未說明車輛可於何處行駛、朝哪個方向、以及多快。僅有表面的關卡是場景而非道路網。
    /// CS2 將此圖保存為一般 ECS 元件，無讀回風險，故此為遊戲中最廉價而有用的擷取項目之一。）<br/><br/>
    /// <b>Master and slave lanes.</b> CS2 stores a lane once and derives its neighbours: a
    /// <c>SecondaryLane</c> is generated from a master rather than placed. Both are emitted, the
    /// derived ones marked, because a consumer laying out traffic wants every lane a vehicle can
    /// occupy, while one drawing lane lines may want only the placed ones.<br/>
    /// （<b>主車道與從屬車道。</b>CS2 只儲存一次車道並推導其相鄰者：<c>SecondaryLane</c> 由主車道產生
    /// 而非獨立擺放。兩者皆輸出，並標記推導而來者：安排交通的消費端需要車輛可佔用的每一條車道，
    /// 而繪製車道線者可能只需要實際擺放的那些。）
    /// </summary>
    public partial class LaneRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed lanes.（已放置車道的查詢。）
        /// </summary>
        EntityQuery _laneQuery;

        /// <summary>
        /// How finely a lane's bézier is sampled. A lane is a driving line rather than a surface,
        /// so it needs enough points to steer along and no more; eight segments holds a junction
        /// turn to a few centimetres.
        /// （車道貝茲曲線的取樣密度。車道是行駛路線而非表面，故取樣點只需足以沿之轉向即可；
        /// 八段可使路口轉彎的誤差維持在數公分內。）
        /// </summary>
        private const int Samples = 8;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _laneQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Net.Lane>(),
                    ComponentType.ReadOnly<Curve>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            base.OnCreate();
            _log.Debug("LaneRipSystem instance created. 車道擷取系統實例創造完成。");
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
        /// Write every lane in the city to <c>lanes.jsonl</c>.
        /// （將城市中的每條車道寫入 <c>lanes.jsonl</c>。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipLanes(string outputDirectory)
        {
            string path = IOUtils.CombinePath(outputDirectory, "lanes.jsonl");
            StringBuilder report = new();
            Dictionary<string, int> byKind = new();
            int secondary = 0, degenerate = 0;

            // Lanes are not only a graph: a lane whose prefab carries a `SubMesh` buffer is drawn
            // world geometry — hanging utility cables, fence lanes, hedge rows — assembled along
            // the lane's curve by `BatchInstanceSystem.UpdateLaneInstances`. The network rip
            // bakes that geometry into the `lanemeshes/` layer (contract §5B); this census stays
            // as the cross-check — the count here should track what that stage bakes plus what
            // it deliberately skips (tunnel, pipeline, decal), and a mismatch is a bug.
            // （車道不僅是圖：其預製模板帶有 `SubMesh` 緩衝區的車道是被繪製的世界幾何——
            //   懸掛的公用設施電纜、圍籬車道、樹籬——由 `UpdateLaneInstances` 沿車道曲線組裝。
            //   路網擷取將該幾何烘焙至 `lanemeshes/` 層（契約 §5B）；此普查保留作為交叉檢核——
            //   此處的計數應與該階段所烘焙者加上其刻意略過者（隧道、管線、貼花）相符，
            //   不符即為缺陷。）
            Dictionary<Entity, bool> prefabHasGeometry = new();
            Dictionary<Entity, int> drawnLanesByPrefab = new();
            int drawnLanes = 0;

            NativeArray<Entity> entities = _laneQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                report.AppendLine($"Lanes: {entities.Length}");

                using LaneWriter writer = new(path);

                foreach (Entity entity in entities)
                {
                    Curve curve = EntityManager.GetComponentData<Curve>(entity);

                    if (EntityManager.HasComponent<Game.Prefabs.PrefabRef>(entity))
                    {
                        Entity lanePrefab = EntityManager.GetComponentData<Game.Prefabs.PrefabRef>(entity).m_Prefab;
                        if (lanePrefab != Entity.Null)
                        {
                            if (!prefabHasGeometry.TryGetValue(lanePrefab, out bool drawn))
                            {
                                drawn = EntityManager.HasBuffer<Game.Prefabs.SubMesh>(lanePrefab)
                                     && (EntityManager.GetBuffer<Game.Prefabs.SubMesh>(lanePrefab, true).Length > 0);
                                prefabHasGeometry[lanePrefab] = drawn;
                            }

                            if (drawn)
                            {
                                drawnLanes++;
                                drawnLanesByPrefab.TryGetValue(lanePrefab, out int seen);
                                drawnLanesByPrefab[lanePrefab] = seen + 1;
                            }
                        }
                    }

                    LaneRecord record = new()
                    {
                        Id = entity.Index,
                        Kind = Classify(entity),
                        Length = curve.m_Length,
                        Secondary = EntityManager.HasComponent<SecondaryLane>(entity)
                    };

                    // A lane's owner is the edge or node it belongs to, which is what ties this
                    // file to networks.jsonl.
                    // （車道的擁有者是其所屬的邊或節點，此即本檔案與 networks.jsonl 的連結。）
                    if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                    {
                        Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                        if (owner != Entity.Null) record.Owner = owner.Index;
                    }

                    Tessellate(curve.m_Bezier, record.Path);
                    if (record.Path.Count < 2) { degenerate++; continue; }

                    AddLaneData(entity, record);

                    writer.Write(record);
                    if (record.Secondary) secondary++;
                    byKind.TryGetValue(record.Kind, out int count);
                    byKind[record.Kind] = count + 1;
                }

                report.AppendLine($"Wrote {writer.Count} lane(s) to lanes.jsonl");
                foreach (KeyValuePair<string, int> pair in byKind) report.AppendLine($"  {pair.Key}: {pair.Value}");
                report.AppendLine($"  {secondary} derived from a master lane");
                if (degenerate > 0) report.AppendLine($"Skipped {degenerate} lane(s) with no usable curve");

                if (drawnLanes > 0)
                {
                    int drawnPrefabs = 0;
                    foreach (KeyValuePair<Entity, bool> pair in prefabHasGeometry) if (pair.Value) drawnPrefabs++;

                    report.AppendLine($"Lane geometry cross-check: {drawnLanes} lane(s) over "
                                    + $"{drawnPrefabs} prefab(s) draw a mesh along their curve "
                                    + "(baked by the network rip into lanemeshes/, minus its counted skips). Largest:");

                    List<KeyValuePair<Entity, int>> ranked = new(drawnLanesByPrefab);
                    ranked.Sort((a, b) => b.Value.CompareTo(a.Value));
                    for (int i = 0; (i < 10) && (i < ranked.Count); i++)
                    {
                        string name = Instance.Name.GetPrefabName(ranked[i].Key);
                        report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {ranked[i].Key.Index}" : name),-40} {ranked[i].Value,8}");
                    }
                }
            }
            finally
            {
                entities.Dispose();
            }

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// What may use this lane.（何者可使用此車道。）
        /// </summary>
        /// <param name="entity">The lane.（車道。）</param>
        /// <returns>The contract's lane kind.（契約所定義的車道類別。）</returns>
        private string Classify(Entity entity)
        {
            if (EntityManager.HasComponent<CarLane>(entity)) return "car";
            if (EntityManager.HasComponent<TrackLane>(entity)) return "track";
            if (EntityManager.HasComponent<PedestrianLane>(entity)) return "pedestrian";
            if (EntityManager.HasComponent<ParkingLane>(entity)) return "parking";
            if (EntityManager.HasComponent<ConnectionLane>(entity)) return "connection";
            return "other";
        }

        /// <summary>
        /// Carry across whatever the lane's own component knows: speed, bendiness and flags.
        /// （帶出車道自身元件所知的一切：速度、彎曲程度與旗標。）<br/><br/>
        /// The speed limit is stored in metres per second and published in km/h, matching the
        /// <c>limit</c> that <c>networks.jsonl</c> already carries so the two agree.<br/>
        /// （速限以公尺/秒儲存，並以公里/小時發布，與 <c>networks.jsonl</c> 既有的 <c>limit</c> 一致。）
        /// </summary>
        /// <param name="entity">The lane.（車道。）</param>
        /// <param name="record">The record to fill.（待填入的紀錄。）</param>
        private void AddLaneData(Entity entity, LaneRecord record)
        {
            if (EntityManager.HasComponent<CarLane>(entity))
            {
                CarLane lane = EntityManager.GetComponentData<CarLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<TrackLane>(entity))
            {
                TrackLane lane = EntityManager.GetComponentData<TrackLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<PedestrianLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<PedestrianLane>(entity).m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<ParkingLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<ParkingLane>(entity).m_Flags.ToString());
            }
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// （將旗標列舉的名稱字串拆分為契約所定義的名稱陣列。）<br/><br/>
        /// Names rather than a bit field: the numbers are CS2's internal values and would silently
        /// change meaning between game versions, where a name that disappears is a name a consumer
        /// simply stops seeing.<br/>
        /// （採用名稱而非位元欄位：數值為 CS2 的內部值，可能在版本間悄然改變意義；
        /// 而消失的名稱只會讓消費端不再看到它。）
        /// </summary>
        /// <param name="record">The record to add to.（待新增的紀錄。）</param>
        /// <param name="flags">The enum's <c>ToString</c>.（列舉的 <c>ToString</c> 結果。）</param>
        private static void AddFlags(LaneRecord record, string flags)
        {
            if (string.IsNullOrEmpty(flags) || (flags == "0")) return;

            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) record.Flags.Add(trimmed);
            }
        }

        /// <summary>
        /// Sample a lane's bézier into a polyline.（將車道的貝茲曲線取樣為折線。）
        /// </summary>
        /// <param name="curve">The lane's curve.（車道的曲線。）</param>
        /// <param name="points">The polyline to fill.（待填入的折線。）</param>
        private static void Tessellate(Colossal.Mathematics.Bezier4x3 curve, List<float3> points)
        {
            for (int i = 0; i <= Samples; i++)
            {
                float3 point = Colossal.Mathematics.MathUtils.Position(curve, i / (float)Samples);

                // A lane that doubles back on itself produces repeated points; a consumer steering
                // along the path would divide by a zero-length segment.
                // （來回折返的車道會產生重複點，沿路徑轉向的消費端將因零長度線段而除以零。）
                if ((points.Count > 0) && (math.distancesq(points[points.Count - 1], point) < 1e-6f)) continue;
                points.Add(point);
            }
        }
    }
}
