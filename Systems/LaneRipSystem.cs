using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Net;
using System;
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
        /// （當系統實例被銷毀時所觸發的事件。）<br/><br/>
        /// A run in flight holds a persistent native array and an open file — both must be
        /// released when the world unloads mid-rip.<br/>
        /// （進行中的執行持有常駐原生陣列與開啟中的檔案——世界於擷取途中卸載時兩者皆須釋放。）
        /// </summary>
        protected override void OnDestroy()
        {
            LaneRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Lane rip abandoned at {run.Index}/{run.Entities.Length} lane(s); the world was unloaded. 車道擷取於世界卸載時中止。");
                Abandon(run);
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Advance any rip in progress by one frame's worth of work.
        /// （將進行中的擷取推進一幀份的工作。）
        /// </summary>
        protected override void OnUpdate()
        {
            LaneRun run = _run;
            if (run != null) Advance(run);
        }

        /// <summary>
        /// One lane rip in flight.（一次進行中的車道擷取。）
        /// </summary>
        private class LaneRun
        {
            /// <summary>The record file being written.（寫入中的紀錄檔。）</summary>
            public string Path;

            /// <summary>The lanes to walk, held across frames (persistent allocator).（跨幀持有的待走訪車道（常駐配置器）。）</summary>
            public NativeArray<Entity> Entities;

            /// <summary>How far the walk has got.（走訪進度。）</summary>
            public int Index;

            /// <summary>The record writer, open for the whole run.（整次執行保持開啟的紀錄寫出器。）</summary>
            public LaneWriter Writer;

            // The tallies the walk keeps.（走訪期間維護的統計。）
            public Dictionary<string, int> ByKind = new();
            public int Secondary, Degenerate, ProcessFailed;
            public Dictionary<Entity, bool> PrefabHasGeometry = new();
            public Dictionary<Entity, int> DrawnLanesByPrefab = new();
            public int DrawnLanes;

            /// <summary>The report accumulated as the run goes.（隨執行累積的報告。）</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.（整體工作的實際經過時間。）</summary>
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// （由管線驅動時完成摘要的去處；<c>null</c> 代表手動啟動，經自身對話框回報。）
            /// </summary>
            public Action<string> Completed;
        }

        /// <summary>The rip in flight, or <c>null</c>.（進行中的擷取，或 <c>null</c>。）</summary>
        private LaneRun _run;

        /// <summary>
        /// Whether a rip is currently running.（目前是否有擷取工作進行中。）
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How long the walk may hold the main thread each frame. At least one lane is always
        /// processed, so an expensive one cannot stall the run.
        /// （走訪每幀可佔用主執行緒的時間上限。每幀必定至少處理一條車道，故單一昂貴車道不致使執行停滯。）
        /// </summary>
        private const double FrameBudgetMilliseconds = 6d;

        /// <summary>
        /// Start writing every lane in the city to <c>lanes.jsonl</c>, returning at once; the walk
        /// proceeds a slice per frame in <see cref="OnUpdate"/> (PLAN Phase 8 — the pass used to
        /// block the main thread whole).
        /// （開始將城市中的每條車道寫入 <c>lanes.jsonl</c> 並立即返回；走訪於 <see cref="OnUpdate"/>
        /// 中每幀推進一段（PLAN 第八階段——此趟過去會整段阻塞主執行緒）。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.（由管線驅動時接收完成摘要以取代對話框。）</param>
        /// <returns>A short description of what was queued.（已排入工作的簡短說明。）</returns>
        public string BeginRip(string outputDirectory, Action<string> completed = null)
        {
            if (_run != null) return "A lane rip is already running.";

            LaneRun run = new()
            {
                Path = IOUtils.CombinePath(outputDirectory, "lanes.jsonl"),
                Entities = _laneQuery.ToEntityArray(Allocator.Persistent),
                Completed = completed
            };

            try
            {
                run.Report.AppendLine($"Lanes: {run.Entities.Length}");
                run.Writer = new LaneWriter(run.Path);
            }
            catch
            {
                Abandon(run);
                throw;
            }

            _run = run;
            RipProgress.Report(RipProgress.Phase.Lanes, 0, run.Entities.Length);

            return $"Walking {run.Entities.Length} lane(s) in the background.";
        }

        /// <summary>
        /// Advance a rip by one frame's budget.（以一幀的預算推進擷取。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        private void Advance(LaneRun run)
        {
            System.Diagnostics.Stopwatch budget = System.Diagnostics.Stopwatch.StartNew();

            while (run.Index < run.Entities.Length)
            {
                // One bad lane must not cost the rest of the graph its records.
                // （一條異常車道不得使圖的其餘部分失去紀錄。）
                try
                {
                    ProcessOne(run, run.Entities[run.Index]);
                }
                catch (Exception ex)
                {
                    run.ProcessFailed++;
                    if (run.ProcessFailed <= 5) _log.Warn($"Failed to record lane entity {run.Entities[run.Index].Index}: {ex}");
                }

                run.Index++;

                if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
            }

            RipProgress.Report(RipProgress.Phase.Lanes, run.Index, run.Entities.Length);
            if (run.Index >= run.Entities.Length) Finish(run);
        }

        /// <summary>
        /// Handle one lane: census its drawn geometry, then record the graph edge.
        /// （處理一條車道：普查其繪製幾何，再記錄圖的邊。）<br/><br/>
        /// Lanes are not only a graph: a lane whose prefab carries a <c>SubMesh</c> buffer is drawn
        /// world geometry — hanging utility cables, fence lanes, hedge rows — assembled along
        /// the lane's curve by <c>BatchInstanceSystem.UpdateLaneInstances</c>. The network rip
        /// bakes that geometry into the <c>lanemeshes/</c> layer (contract §5B); this census stays
        /// as the cross-check — the count here should track what that stage bakes plus what
        /// it deliberately skips (tunnel, pipeline, decal), and a mismatch is a bug.<br/>
        /// （車道不僅是圖：其預製模板帶有 <c>SubMesh</c> 緩衝區的車道是被繪製的世界幾何——
        /// 懸掛的公用設施電纜、圍籬車道、樹籬——由 <c>UpdateLaneInstances</c> 沿車道曲線組裝。
        /// 路網擷取將該幾何烘焙至 <c>lanemeshes/</c> 層（契約 §5B）；此普查保留作為交叉檢核——
        /// 此處的計數應與該階段所烘焙者加上其刻意略過者（隧道、管線、貼花）相符，不符即為缺陷。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        /// <param name="entity">The lane.（車道。）</param>
        private void ProcessOne(LaneRun run, Entity entity)
        {
            Curve curve = EntityManager.GetComponentData<Curve>(entity);

            if (EntityManager.HasComponent<Game.Prefabs.PrefabRef>(entity))
            {
                Entity lanePrefab = EntityManager.GetComponentData<Game.Prefabs.PrefabRef>(entity).m_Prefab;
                if (lanePrefab != Entity.Null)
                {
                    if (!run.PrefabHasGeometry.TryGetValue(lanePrefab, out bool drawn))
                    {
                        drawn = EntityManager.HasBuffer<Game.Prefabs.SubMesh>(lanePrefab)
                             && (EntityManager.GetBuffer<Game.Prefabs.SubMesh>(lanePrefab, true).Length > 0);
                        run.PrefabHasGeometry[lanePrefab] = drawn;
                    }

                    if (drawn)
                    {
                        run.DrawnLanes++;
                        run.DrawnLanesByPrefab.TryGetValue(lanePrefab, out int seen);
                        run.DrawnLanesByPrefab[lanePrefab] = seen + 1;
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
            if (record.Path.Count < 2) { run.Degenerate++; return; }

            AddLaneData(entity, record);

            run.Writer.Write(record);
            if (record.Secondary) run.Secondary++;
            run.ByKind.TryGetValue(record.Kind, out int count);
            run.ByKind[record.Kind] = count + 1;
        }

        /// <summary>
        /// Close the file, report, and hand the summary to whoever is listening.
        /// （關閉檔案、產出報告，並將摘要交予聆聽者。）
        /// </summary>
        /// <param name="run">The finished run.（已完成的工作。）</param>
        private void Finish(LaneRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            StringBuilder report = run.Report;
            report.AppendLine($"Wrote {run.Writer.Count} lane(s) to lanes.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
            foreach (KeyValuePair<string, int> pair in run.ByKind) report.AppendLine($"  {pair.Key}: {pair.Value}");
            report.AppendLine($"  {run.Secondary} derived from a master lane");
            if (run.Degenerate > 0) report.AppendLine($"Skipped {run.Degenerate} lane(s) with no usable curve");
            if (run.ProcessFailed > 0) report.AppendLine($"FAILED to record {run.ProcessFailed} lane(s) — see the log");

            if (run.DrawnLanes > 0)
            {
                int drawnPrefabs = 0;
                foreach (KeyValuePair<Entity, bool> pair in run.PrefabHasGeometry) if (pair.Value) drawnPrefabs++;

                report.AppendLine($"Lane geometry cross-check: {run.DrawnLanes} lane(s) over "
                                + $"{drawnPrefabs} prefab(s) draw a mesh along their curve "
                                + "(baked by the network rip into lanemeshes/, minus its counted skips). Largest:");

                List<KeyValuePair<Entity, int>> ranked = new(run.DrawnLanesByPrefab);
                ranked.Sort((a, b) => b.Value.CompareTo(a.Value));
                for (int i = 0; (i < 10) && (i < ranked.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(ranked[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {ranked[i].Key.Index}" : name),-40} {ranked[i].Value,8}");
                }
            }

            int laneCount = run.Writer.Count;
            Abandon(run);

            _log.Info(report.ToString());

            if (run.Completed != null)
            {
                try { run.Completed(report.ToString()); }
                catch (Exception ex) { _log.Warn($"Lane rip completion callback failed: {ex}"); }
                return;
            }

            try
            {
                string summary = $"Wrote {laneCount} lane record(s) in {run.Clock.Elapsed.TotalSeconds:F1} s.\n\nFull report: Logs/Carto.log";
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary, "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the lane rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Release what a run holds open — the record file and the entity array.
        /// （釋放執行所持有的資源——紀錄檔與實體陣列。）
        /// </summary>
        /// <param name="run">The run to release.（要釋放的工作。）</param>
        private static void Abandon(LaneRun run)
        {
            try { run.Writer?.Dispose(); } catch { /* already closed（已關閉） */ }
            run.Writer = null;
            if (run.Entities.IsCreated) run.Entities.Dispose();
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
