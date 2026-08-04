using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that reads real mesh geometry out of the game and writes it as glTF.
    /// （自遊戲中讀取實際網格幾何並輸出為 glTF 的系統。）<br/><br/>
    /// CS2 keeps mesh geometry in plain ECS dynamic buffers on the prefab entity —
    /// <c>MeshVertex</c>, <c>MeshNormal</c>, <c>MeshUV0</c>, <c>MeshTangent</c>, <c>MeshIndex</c> —
    /// alongside a <c>MeshData</c> component and a <c>MeshMaterial</c> buffer describing each
    /// drawable range. The game itself reads these every frame for mouse picking
    /// (<c>Game.Common.RaycastSystem</c>) and camera collision
    /// (<c>Game.Rendering.CameraCollisionSystem</c>), so the data is CPU-resident for the whole
    /// session and needs no GPU readback.<br/>
    /// （CS2 將網格幾何存放於預製模板實體上的一般 ECS 動態緩衝區中。遊戲本身每幀都會為滑鼠拾取與
    /// 攝影機碰撞讀取這些資料，因此資料在整個遊戲期間都常駐於 CPU 端，無須自 GPU 回讀。）<br/><br/>
    /// Traversal is: object prefab entity → <c>SubMesh</c> buffer → mesh entity → geometry buffers.
    /// （走訪路徑為：物件預製模板實體 → <c>SubMesh</c> 緩衝區 → 網格實體 → 幾何緩衝區。）
    /// </summary>
    public partial class MeshRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for object prefabs that carry renderable sub-meshes.
        /// （帶有可繪製子網格的物件預製模板查詢。）
        /// </summary>
        EntityQuery _subMeshPrefabQuery;

        /// <summary>
        /// The query for buildings actually placed in the loaded city.
        /// （已載入城市中實際放置之建築的查詢。）<br/><br/>
        /// Placed buildings are what the ripper cares about, and — unlike the tens of thousands of
        /// catalogue prefabs in the asset database — their meshes are the ones the game has
        /// actually had to load.<br/>
        /// （已放置的建築才是擷取的目標，且不同於資產資料庫中數以萬計的目錄預製模板，
        /// 這些網格是遊戲確實已載入的。）
        /// </summary>
        EntityQuery _placedBuildingQuery;

        /// <summary>
        /// The query for every placed object, matching <see cref="InstanceRipSystem"/>'s exactly so
        /// the prefab set ripped here is the prefab set <c>instances.jsonl</c> references.<br/>
        /// （所有已放置物件的查詢，與 <see cref="InstanceRipSystem"/> 完全一致，
        /// 使此處擷取的預製模板集合恰為 <c>instances.jsonl</c> 所參照者。）
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// The run in progress, or <c>null</c> when idle.（進行中的擷取工作，閒置時為 <c>null</c>。）
        /// </summary>
        RipRun _run;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _subMeshPrefabQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<PrefabData>(),
                    ComponentType.ReadOnly<SubMesh>()
                }
            });

            _placedBuildingQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Buildings.Building>(),
                    ComponentType.ReadOnly<PrefabRef>()
                }
            });

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
                    ComponentType.ReadOnly<Moving>(),
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>(),
                    ComponentType.ReadOnly<Marker>(),
                    ComponentType.ReadOnly<Game.Creatures.Creature>(),
                    ComponentType.ReadOnly<Game.Vehicles.Vehicle>()
                }
            });

            base.OnCreate();
            _log.Debug("MeshRipSystem instance created. 網格擷取系統實例創造完成。");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）<br/><br/>
        /// A rip in flight must be released here. The writer task blocks on the queue until the
        /// reader declares it complete, and once this system stops updating — the user quit to the
        /// menu mid-rip — nothing else ever would.<br/>
        /// （進行中的擷取必須於此釋放。寫出工作會阻塞於佇列直到讀取端宣告完成，
        /// 而一旦此系統停止更新（使用者於擷取途中返回主選單），便再無他者會做此宣告。）
        /// </summary>
        protected override void OnDestroy()
        {
            RipRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Mesh rip abandoned at {run.Index}/{run.Items.Count} prefab(s); the world was unloaded. 網格擷取於世界卸載時中止。");
                try { run.Queue.CompleteAdding(); } catch { /* already completed */ }
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Drive the progress indicator, and advance any rip in progress by one frame's worth of
        /// work.<br/>
        /// （驅動進度指示器，並將進行中的擷取工作推進一幀份的進度。）<br/><br/>
        /// This system is registered at <see cref="SystemUpdatePhase.MainLoop"/> rather than with
        /// the simulation, because a rip is started from the options page — which pauses the
        /// simulation, so a simulation-phase system would never tick while the user is looking at
        /// it.<br/>
        /// （此系統註冊於 <see cref="SystemUpdatePhase.MainLoop"/> 而非模擬階段，因為擷取由選項頁面啟動，
        /// 而該頁面會暫停模擬，模擬階段的系統在使用者注視時將完全不會更新。）
        /// </summary>
        protected override void OnUpdate()
        {
            RipProgress.Pump();

            RipRun run = _run;
            if (run != null) Advance(run);
        }

        /// <summary>
        /// The PLAN Phase 1 feasibility spike: rip the meshes of the first few building prefabs in
        /// the loaded world and write one <c>.glb</c> per unique mesh.<br/>
        /// （PLAN 第一階段的可行性驗證：擷取已載入世界中前數個建築預製模板的網格，並為每個唯一網格輸出一個 <c>.glb</c>。）<br/><br/>
        /// This deliberately writes one file per <i>mesh</i> rather than per <i>building</i>: the
        /// point is to prove that real vertices, indices and UVs come out and open correctly, not
        /// to assemble multi-part buildings, which is Phase 3's job.<br/>
        /// （此處刻意以「網格」而非「建築」為單位輸出檔案：目的是驗證能取出正確的頂點、索引與 UV，
        /// 而組裝多部件建築屬於第三階段的工作。）
        /// </summary>
        /// <param name="maxPrefabs">The number of building prefabs to walk.（要走訪的建築預製模板數量。）</param>
        /// <returns>A human-readable report of what was written.（可供閱讀的輸出報告。）</returns>
        public string RipSpike(int maxPrefabs = 8)
        {
            string outputDirectory = IOUtils.CombinePath(Instance.CartoDataPath, "spike", "meshes");
            StringBuilder report = new();
            int prefabsWalked = 0, meshesWritten = 0, meshesSkipped = 0;
            long bytesWritten = 0;
            HashSet<string> written = new();

            List<Entity> prefabEntities = GetCandidatePrefabs(report);
            bool diagnosedOnce = false;

            foreach (Entity prefabEntity in prefabEntities)
            {
                if (prefabsWalked >= maxPrefabs) break;
                if (!EntityManager.HasBuffer<SubMesh>(prefabEntity)) continue;

                string prefabName = GetPrefabName(prefabEntity) ?? $"entity_{prefabEntity.Index}";
                prefabsWalked++;

                DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                report.AppendLine($"[{prefabsWalked}] {prefabName}  ({subMeshes.Length} sub-mesh)");

                for (int i = 0; i < subMeshes.Length; i++)
                {
                    Entity meshEntity = subMeshes[i].m_SubMesh;
                    if (meshEntity == Entity.Null)
                    {
                        report.AppendLine($"      - sub{i}: null mesh entity");
                        continue;
                    }

                    string meshName = GetPrefabName(meshEntity) ?? $"{prefabName}_sub{i}";
                    string assetId = SanitizeAssetId(meshName);
                    if (!written.Add(assetId))
                    {
                        report.AppendLine($"      = {assetId}: already written");
                        continue;
                    }

                    try
                    {
                        // Trace every hop on the first failure. Guessing twice at a structure the
                        // game can simply be asked about is how a spike stalls.
                        // （於首次失敗時記錄每一次跳轉。對於可直接詢問遊戲的結構重複猜測，
                        //   正是驗證停滯的原因。）
                        // Trace the first mesh whether or not it succeeds. A mesh that comes out
                        // but comes out *reduced* looks like success in the counts alone.
                        // （無論成功與否皆記錄首個網格。僅看數量時，讀到簡化網格與成功難以區分。）
                        List<string> trace = diagnosedOnce ? null : new();
                        MeshAsset mesh = ReadMesh(meshEntity, assetId, meshName, trace);

                        if (!diagnosedOnce)
                        {
                            diagnosedOnce = true;
                            report.AppendLine($"        resolution trace for {assetId}:");
                            foreach (string hop in trace) report.AppendLine($"          {hop}");
                        }

                        if (mesh == null)
                        {
                            meshesSkipped++;
                            report.AppendLine($"      - {assetId}: no geometry buffers");
                            continue;
                        }

                        string path = IOUtils.CombinePath(outputDirectory, $"{assetId}.glb");
                        long size = GltfWriter.Write(path, mesh);
                        bytesWritten += size;
                        meshesWritten++;
                        report.AppendLine($"      + {assetId}: {mesh.Vertices.Length} verts, {mesh.TriangleCount} tris, {mesh.Primitives.Count} prim, LOD{mesh.Lod}, {size} B");
                    }
                    catch (Exception ex)
                    {
                        meshesSkipped++;
                        report.AppendLine($"      ! {assetId}: {ex.Message}");
                        _log.Warn($"Failed to rip mesh `{assetId}`: {ex}");
                    }
                }
            }

            report.AppendLine();
            report.AppendLine($"Wrote {meshesWritten} mesh(es), skipped {meshesSkipped}, {bytesWritten} bytes total.");
            report.AppendLine($"Output: {outputDirectory}");

            // The full report goes to the log. The in-game MessageDialog does not scroll, so a
            // long body pushes its OK button off the bottom of the screen — return a summary.
            // （完整報告寫入記錄檔。遊戲內的 MessageDialog 無法捲動，過長的內容會將確定按鈕擠出畫面。）
            _log.Info(report.ToString());

            StringBuilder summary = new();
            summary.AppendLine($"Wrote {meshesWritten} mesh(es), skipped {meshesSkipped}.");
            summary.AppendLine($"{bytesWritten / 1024} KiB to ModsData/Carto/spike/meshes");
            summary.AppendLine();
            summary.Append("Full report: Logs/Carto.log");
            return summary.ToString();
        }

        // ====================================================================================
        // PLAN Phase 3 — mesh dump, one .glb per unique prefab.
        // （PLAN 第三階段：網格輸出，每個唯一預製模板一個 .glb。）
        // ====================================================================================

        /// <summary>
        /// One prefab waiting to be ripped.（一個等待擷取的預製模板。）
        /// </summary>
        private struct WorkItem
        {
            public Entity PrefabEntity;
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
        }

        /// <summary>
        /// The mutable state of a rip in progress.（進行中擷取工作的可變狀態。）<br/><br/>
        /// A full rip reads thousands of prefabs and writes gigabytes, which cannot happen inside a
        /// button handler without freezing the game for minutes. The work is therefore split in two:
        /// reading geometry stays on the main thread — <c>EntityManager</c> and the asset database
        /// permit nothing else — but only for a few milliseconds per frame, while glTF serialisation
        /// and disk writes run on a background task fed through <see cref="Queue"/>.<br/>
        /// （完整擷取需讀取數千個預製模板並寫出數 GB，在按鈕處理常式中進行必然使遊戲凍結數分鐘。
        /// 因此工作一分為二：讀取幾何仍在主執行緒（<c>EntityManager</c> 與資產資料庫別無選擇），
        /// 但每幀僅佔數毫秒；glTF 序列化與磁碟寫入則於背景工作中透過 <see cref="Queue"/> 進行。）
        /// </summary>
        private class RipRun
        {
            /// <summary>The <c>meshes/</c> directory being written into.（正在寫入的 <c>meshes/</c> 目錄。）</summary>
            public string OutputDirectory;

            /// <summary>The prefabs to rip, richest-covering first.（待擷取的預製模板，覆蓋最廣者優先。）</summary>
            public List<WorkItem> Items;

            /// <summary>How far through <see cref="Items"/> the reader has got.（讀取端於 <see cref="Items"/> 中的進度。）</summary>
            public int Index;

            /// <summary>Assembled prefabs waiting to be serialised.（等待序列化的已組裝預製模板。）</summary>
            public BlockingCollection<PrefabMesh> Queue;

            /// <summary>The background serialiser.（背景序列化工作。）</summary>
            public Task Writer;

            /// <summary>Whether the reader has finished handing work over.（讀取端是否已交付完所有工作。）</summary>
            public bool ReadingDone;

            /// <summary>
            /// Counters. The first three belong to the reader on the main thread, the rest to the
            /// writer task — kept apart so neither needs to synchronise with the other.
            /// （計數器。前三者屬於主執行緒的讀取端，其餘屬於寫出工作；分開存放使雙方皆無須彼此同步。）
            /// </summary>
            public int Read, Skipped, ReadFailed;
            public int Written, WriteFailed;
            public long Bytes;

            /// <summary>Everything that did not become a file.（所有未能成為檔案者。）</summary>
            public int Failed => ReadFailed + WriteFailed;

            /// <summary>Lines the writer wants in the report.（寫出端欲納入報告的訊息。）</summary>
            public ConcurrentQueue<string> WriterNotes = new();

            /// <summary>Per-file sizes, for the size breakdown.（各檔案大小，供大小分析使用。）</summary>
            public ConcurrentBag<KeyValuePair<string, long>> Sizes = new();

            /// <summary>
            /// Every prefab that produced no file, and why.（每個未產出檔案的預製模板及其原因。）<br/><br/>
            /// These asset ids are still referenced by <c>instances.jsonl</c>, so the bundle must
            /// account for them rather than leave a consumer to discover the gap.<br/>
            /// （這些資產 ID 仍被 <c>instances.jsonl</c> 參照，因此束必須交代它們，
            /// 而非讓消費端自行發現缺口。）
            /// </summary>
            public List<KeyValuePair<string, string>> SkippedAssets = new();

            /// <summary>The report accumulated on the main thread.（於主執行緒累積的報告。）</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.（整體工作的實際經過時間。）</summary>
            public Stopwatch Clock = Stopwatch.StartNew();
        }

        /// <summary>
        /// How long the reader may hold the main thread each frame.
        /// （讀取端每幀可佔用主執行緒的時間上限。）<br/><br/>
        /// One prefab is always processed per frame even when it overruns, so a single very large
        /// mesh cannot deadlock the run.<br/>
        /// （即使超時，每幀仍必定處理一個預製模板，使單一巨大網格不致使工作停滯。）
        /// </summary>
        private const double FrameBudgetMilliseconds = 6d;

        /// <summary>
        /// How many assembled prefabs may wait for the writer.
        /// （可等待寫出端的已組裝預製模板數量上限。）<br/><br/>
        /// Each one holds its full vertex arrays, so an unbounded queue would simply move the
        /// memory problem rather than solve it. The reader pauses when the queue is full.<br/>
        /// （每一個都持有完整的頂點陣列，因此無上限的佇列只是把記憶體問題搬家而非解決。
        /// 佇列滿時讀取端暫停。）
        /// </summary>
        private const int QueueCapacity = 4;

        /// <summary>
        /// Whether a rip is currently running.（目前是否有擷取工作進行中。）
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// Start ripping every unique prefab placed in the city into <c>meshes/&lt;assetId&gt;.glb</c>.
        /// （開始將城市中每個唯一的已放置預製模板擷取至 <c>meshes/&lt;assetId&gt;.glb</c>。）<br/><br/>
        /// Returns as soon as the work list is built; the rip itself proceeds a slice per frame in
        /// <see cref="OnUpdate"/> and reports its result in a dialog when it finishes.<br/>
        /// （工作清單建立後隨即返回；擷取本身於 <see cref="OnUpdate"/> 中每幀推進一段，
        /// 完成時以對話框回報結果。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write <c>meshes/</c> under.（要在其下寫入 <c>meshes/</c> 的束根目錄。）</param>
        /// <param name="maxPrefabs">The most prefabs to rip, or <c>0</c> for all of them.（最多擷取的預製模板數量，<c>0</c> 為全部。）</param>
        /// <returns>A short description of what was queued.（已排入工作的簡短說明。）</returns>
        public string BeginRip(string outputDirectory, int maxPrefabs = 0)
        {
            if (_run != null) return "A mesh rip is already running.";

            RipProgress.Report(RipProgress.Phase.Scan);

            RipRun run = new()
            {
                OutputDirectory = IOUtils.CombinePath(outputDirectory, "meshes"),
                Queue = new BlockingCollection<PrefabMesh>(QueueCapacity)
            };

            run.Items = CollectWorkItems(run.Report, maxPrefabs, out int distinctPrefabs, out int placedObjects);
            run.Report.AppendLine($"Placed objects: {placedObjects}, distinct prefabs: {distinctPrefabs}, queued: {run.Items.Count}");

            if (run.Items.Count == 0)
            {
                RipProgress.Clear();
                return "Nothing to rip: no placed objects resolved to a prefab.";
            }

            run.Writer = Task.Run(() => WriteQueued(run));
            _run = run;

            RipProgress.Report(RipProgress.Phase.Meshes, 0, run.Items.Count);

            // Be specific about where to look. The indicator draws in the game HUD, which this
            // options page is covering — told only to "watch the corner", the obvious thing to do
            // is stare at the menu and conclude nothing is happening.
            // （明確說明該看何處。指示器繪製於遊戲 HUD 上，而該處正被此選項頁面遮蔽；
            //   若只說「留意角落」，最自然的反應是盯著選單，並認定什麼事都沒發生。）
            return $"Ripping {run.Items.Count} prefab(s) in the background.\n\n"
                 + "Close this options page to watch the progress indicator — it appears in the top-right of the game view, not over the menu.\n\n"
                 + "A dialog reports the result when it finishes.";
        }

        /// <summary>
        /// Build the work list: every distinct prefab referenced by a placed object, ordered by how
        /// many objects use it.<br/>
        /// （建立工作清單：所有已放置物件參照的相異預製模板，依使用該模板的物件數量排序。）<br/><br/>
        /// Ordering matters only when the run is capped — ripping the most-used prefabs first means
        /// a capped run still covers most of the city rather than an arbitrary corner of it.<br/>
        /// （排序僅在限制數量時有意義：優先擷取最常用的預製模板，使受限的擷取仍能覆蓋城市的大部分，
        /// 而非任意一角。）
        /// </summary>
        /// <param name="report">The report to note discovery counts in.（用以記錄探索統計的報告。）</param>
        /// <param name="maxPrefabs">The cap, or <c>0</c> for none.（數量上限，<c>0</c> 為不限。）</param>
        /// <param name="distinctPrefabs">Receives the number of distinct prefabs found.（接收找到的相異預製模板數量。）</param>
        /// <param name="placedObjects">Receives the number of placed objects walked.（接收走訪的已放置物件數量。）</param>
        /// <returns>The work list.（工作清單。）</returns>
        private List<WorkItem> CollectWorkItems(StringBuilder report, int maxPrefabs, out int distinctPrefabs, out int placedObjects)
        {
            Dictionary<Entity, int> counts = new();
            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                placedObjects = entities.Length;
                foreach (Entity entity in entities)
                {
                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity == Entity.Null) continue;

                    counts.TryGetValue(prefabEntity, out int count);
                    counts[prefabEntity] = count + 1;
                }
            }
            finally
            {
                entities.Dispose();
            }

            distinctPrefabs = counts.Count;

            // Ids come from the shared assignment, not from each name in isolation, so that a
            // duplicated prefab name cannot make two prefabs write to the same file.
            // （ID 來自共用的指派而非各自獨立的名稱，使重複的預製模板名稱無法讓兩個模板寫入同一檔案。）
            Dictionary<Entity, AssetIdEntry> assetIds = BuildAssetIds(EntityManager, counts.Keys, out int disambiguated);
            if (disambiguated > 0) report.AppendLine($"Disambiguated {disambiguated} prefab(s) sharing a name.");

            List<WorkItem> items = new(counts.Count);
            foreach (KeyValuePair<Entity, int> kvp in counts)
            {
                AssetIdEntry entry = assetIds[kvp.Key];

                // An unresolvable prefab still gets ripped under the same synthetic id
                // InstanceRipSystem gives its instances, so the two layers keep joining up.
                // （無法解析的預製模板仍以 InstanceRipSystem 給予其實例的相同合成 ID 擷取，
                //   使兩個層級持續得以連結。）
                items.Add(new WorkItem
                {
                    PrefabEntity = kvp.Key,
                    AssetId = entry.AssetId,
                    SourceName = GetPrefabName(kvp.Key) ?? entry.AssetId,
                    InstanceCount = kvp.Value
                });
            }

            items.Sort((a, b) => b.InstanceCount.CompareTo(a.InstanceCount));

            if ((maxPrefabs > 0) && (items.Count > maxPrefabs))
            {
                report.AppendLine($"Capped to the {maxPrefabs} most-used prefab(s) of {items.Count}.");
                items.RemoveRange(maxPrefabs, items.Count - maxPrefabs);
            }

            return items;
        }

        /// <summary>
        /// Read one frame's worth of prefabs, then finish the run once the writer has drained.
        /// （讀取一幀份的預製模板，並於寫出端排空後結束此次工作。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        private void Advance(RipRun run)
        {
            if (!run.ReadingDone)
            {
                Stopwatch budget = Stopwatch.StartNew();

                while (run.Index < run.Items.Count)
                {
                    // Back-pressure: pause reading rather than pile assembled meshes up in memory
                    // faster than they can be written.（背壓：暫停讀取，而非讓已組裝的網格在記憶體中
                    // 堆積得比寫出更快。）
                    if (run.Queue.Count >= QueueCapacity) break;

                    ReadOne(run, run.Items[run.Index]);
                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
                }

                RipProgress.Report(RipProgress.Phase.Meshes, run.Index, run.Items.Count);

                if (run.Index >= run.Items.Count)
                {
                    run.ReadingDone = true;
                    run.Queue.CompleteAdding();
                }

                return;
            }

            RipProgress.Report(RipProgress.Phase.Write, System.Threading.Volatile.Read(ref run.Written), run.Read);
            if (run.Writer.IsCompleted) Finish(run);
        }

        /// <summary>
        /// Assemble one prefab and hand it to the writer.
        /// （組裝一個預製模板並交付寫出端。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="item">The prefab to read.（待讀取的預製模板。）</param>
        private void ReadOne(RipRun run, WorkItem item)
        {
            try
            {
                PrefabMesh prefab = BuildPrefabMesh(item, out string skipReason);

                if (prefab == null)
                {
                    run.Skipped++;
                    run.SkippedAssets.Add(new KeyValuePair<string, string>(item.AssetId, skipReason));
                    if (run.Skipped <= 20) run.Report.AppendLine($"  - {item.AssetId}: {skipReason}");
                    return;
                }

                run.Read++;
                run.Queue.Add(prefab);
            }
            catch (Exception ex)
            {
                run.ReadFailed++;
                run.Report.AppendLine($"  ! {item.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to assemble prefab `{item.AssetId}`: {ex}");
            }
        }

        /// <summary>
        /// Serialise queued prefabs to disk. Runs on a background task for the whole rip.
        /// （將排入佇列的預製模板序列化至磁碟。整個擷取期間於背景工作中執行。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        private static void WriteQueued(RipRun run)
        {
            foreach (PrefabMesh prefab in run.Queue.GetConsumingEnumerable())
            {
                try
                {
                    string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                    long size = GltfWriter.Write(path, prefab);

                    System.Threading.Interlocked.Increment(ref run.Written);
                    System.Threading.Interlocked.Add(ref run.Bytes, size);
                    run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
                }
                catch (Exception ex)
                {
                    System.Threading.Interlocked.Increment(ref run.WriteFailed);
                    run.WriterNotes.Enqueue($"  ! {prefab.AssetId}: {ex.GetType().Name} {ex.Message}");
                    _log.Warn($"Failed to write `{prefab.AssetId}.glb`: {ex}");
                }
            }
        }

        /// <summary>
        /// Assemble a prefab entity's sub-meshes into one <see cref="PrefabMesh"/>.
        /// （將預製模板實體的子網格組裝為單一 <see cref="PrefabMesh"/>。）
        /// </summary>
        /// <param name="item">The prefab to assemble.（待組裝的預製模板。）</param>
        /// <param name="skipReason">Why nothing came out, when the result is <c>null</c>.（結果為 <c>null</c> 時，說明未能產出的原因。）</param>
        /// <returns>The assembled prefab, or <c>null</c>.（已組裝的預製模板，或 <c>null</c>。）</returns>
        private PrefabMesh BuildPrefabMesh(WorkItem item, out string skipReason)
        {
            skipReason = null;

            if (!EntityManager.HasBuffer<SubMesh>(item.PrefabEntity))
            {
                skipReason = "no SubMesh buffer";
                return null;
            }

            DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(item.PrefabEntity, true);
            if (buffer.Length == 0)
            {
                skipReason = "empty SubMesh buffer";
                return null;
            }

            // Copy out before reading geometry: obtaining a mesh loads an asset, and a dynamic
            // buffer is only valid until the entity's chunk moves.
            // （於讀取幾何前先複製：取得網格會載入資產，而動態緩衝區僅在實體所屬區塊未移動前有效。）
            SubMesh[] subMeshes = new SubMesh[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) subMeshes[i] = buffer[i];

            List<int> selected = SelectSubMeshes(subMeshes);

            PrefabMesh prefab = new()
            {
                AssetId = item.AssetId,
                SourceName = item.SourceName,
                InstanceCount = item.InstanceCount,
                SkippedVariants = subMeshes.Length - selected.Count
            };

            HashSet<string> nodeNames = new();

            foreach (int index in selected)
            {
                Entity meshEntity = subMeshes[index].m_SubMesh;
                if (meshEntity == Entity.Null) continue;

                string meshName = GetPrefabName(meshEntity) ?? $"{item.AssetId}_sub{index}";
                string meshAssetId = SanitizeAssetId(meshName);

                MeshAsset mesh = ReadMesh(meshEntity, meshAssetId, meshName);
                if (mesh == null) continue;

                // Node names must be unique within the file; two entries pointing at the same
                // render prefab (a mirrored pair, say) would otherwise collide.
                // （節點名稱在檔案內必須唯一，否則兩筆指向同一繪製預製模板的條目（例如鏡射成對者）將衝突。）
                string nodeName = meshAssetId;
                for (int suffix = 2; !nodeNames.Add(nodeName); suffix++) nodeName = $"{meshAssetId}_{suffix}";

                prefab.Parts.Add(new PrefabMeshPart
                {
                    Mesh = mesh,
                    Position = subMeshes[index].m_Position,
                    Rotation = subMeshes[index].m_Rotation,
                    NodeName = nodeName
                });
            }

            if (prefab.Parts.Count == 0)
            {
                skipReason = $"none of {subMeshes.Length} sub-mesh(es) yielded geometry";
                return null;
            }

            return prefab;
        }

        /// <summary>
        /// Every <c>Require*</c> bit, i.e. every flag that makes a sub-mesh conditional.
        /// （所有 <c>Require*</c> 位元，亦即所有使子網格帶有條件的旗標。）
        /// </summary>
        private const SubMeshFlags RequireFlags =
            SubMeshFlags.RequireSafe | SubMeshFlags.RequireLevelCrossing | SubMeshFlags.RequireChild
            | SubMeshFlags.RequireTeen | SubMeshFlags.RequireAdult | SubMeshFlags.RequireElderly
            | SubMeshFlags.RequireDead | SubMeshFlags.RequireStump | SubMeshFlags.RequireEmpty
            | SubMeshFlags.RequireFull | SubMeshFlags.RequireEditor | SubMeshFlags.RequireClear
            | SubMeshFlags.RequireTrack | SubMeshFlags.RequireLeftHandTraffic
            | SubMeshFlags.RequireRightHandTraffic | SubMeshFlags.RequirePartial1
            | SubMeshFlags.RequirePartial2 | SubMeshFlags.RequireForward | SubMeshFlags.RequireBackward;

        /// <summary>
        /// The state a ripped prefab is exported in.（擷取的預製模板所輸出的狀態。）<br/><br/>
        /// A <c>SubMesh</c> buffer mixes two different things: parts drawn <i>together</i> (a
        /// building and its roof units) and <i>alternatives</i> the game picks between at draw time
        /// (a sapling, an adult tree, a dead tree and a stump are four entries on one tree prefab).
        /// Exporting all of them would stack every alternative into the same file. One state is
        /// therefore fixed and documented: a healthy adult, unloaded, undamaged, forward-facing.
        /// Traffic side is not a choice — it is read from the city.<br/>
        /// （<c>SubMesh</c> 緩衝區混合了兩種不同的東西：<i>共同</i>繪製的部件，以及遊戲於繪製時擇一的
        /// <i>替代方案</i>（一個樹木預製模板上，幼苗、成樹、枯樹與樹樁是四筆條目）。
        /// 全部輸出將使所有替代方案疊在同一個檔案中，因此固定並記錄單一狀態：
        /// 健康的成體、未裝載、未受損、朝向前方。行車方向並非選擇，而是讀自城市設定。）
        /// </summary>
        private SubMeshFlags DefaultState => SubMeshFlags.RequireSafe | SubMeshFlags.RequireAdult
            | SubMeshFlags.RequireEmpty | SubMeshFlags.RequireClear | SubMeshFlags.RequireForward
            | (Instance.City.leftHandTraffic ? SubMeshFlags.RequireLeftHandTraffic : SubMeshFlags.RequireRightHandTraffic);

        /// <summary>
        /// Choose which sub-mesh entries belong in the exported file.
        /// （選出應納入輸出檔案的子網格條目。）
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.（預製模板的子網格條目。）</param>
        /// <returns>The indices to export.（要輸出的索引。）</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes)
        {
            SubMeshFlags state = DefaultState;
            List<int> selected = new();

            for (int i = 0; i < subMeshes.Length; i++)
            {
                // Unconditional entries are always drawn; conditional ones only when everything
                // they ask for holds in the exported state.
                // （無條件的條目一律繪製；有條件者僅在其所要求的一切於輸出狀態下皆成立時繪製。）
                SubMeshFlags required = subMeshes[i].m_Flags & RequireFlags;
                if ((required & ~state) == 0) selected.Add(i);
            }

            // A prefab whose every entry is conditional — some props exist only as state variants —
            // would otherwise produce no file at all, breaking the contract's promise that every
            // referenced assetId resolves. Fall back to its first entry.
            // （若預製模板的每個條目皆有條件（部分道具僅以狀態變體存在），將完全不產生檔案，
            //   違背契約中「所有被參照的 assetId 皆可解析」的承諾。此時退回使用其首個條目。）
            if ((selected.Count == 0) && (subMeshes.Length > 0)) selected.Add(0);

            return selected;
        }

        /// <summary>
        /// Record every prefab that produced no mesh in the bundle's <c>warnings.jsonl</c>.
        /// （將每個未產出網格的預製模板記錄至束的 <c>warnings.jsonl</c>。）<br/><br/>
        /// A skipped prefab leaves an <c>assetId</c> that <c>instances.jsonl</c> still references
        /// with no <c>.glb</c> behind it. The contract treats an unresolvable reference as invalid,
        /// and rightly — but the honest fix is to <i>declare</i> the gap, not to hide it by dropping
        /// the instances, which would cost their placement for nothing.<br/>
        /// （被略過的預製模板會留下一個 <c>instances.jsonl</c> 仍在參照、卻沒有 <c>.glb</c> 的
        /// <c>assetId</c>。契約視無法解析的參照為無效，此判斷正確；但誠實的處理方式是<i>宣告</i>該缺口，
        /// 而非藉由丟棄實例來掩蓋，那將白白失去它們的擺放資訊。）<br/><br/>
        /// Appends, because <see cref="InstanceRipSystem"/> owns the first pass over this file.
        /// Ripping instances before meshes is the order the bundle is assembled in.<br/>
        /// （採用附加模式，因為此檔案的首次寫入由 <see cref="InstanceRipSystem"/> 負責。
        /// 先擷取實例再擷取網格，正是組裝束的順序。）
        /// </summary>
        /// <param name="run">The finished run.（已完成的工作。）</param>
        private void WriteMeshWarnings(RipRun run)
        {
            if (run.SkippedAssets.Count == 0) return;

            // warnings.jsonl sits at the bundle root, one level above meshes/.
            // （warnings.jsonl 位於束的根目錄，即 meshes/ 的上一層。）
            string root = System.IO.Path.GetDirectoryName(run.OutputDirectory);
            string path = IOUtils.CombinePath(root, "warnings.jsonl");

            try
            {
                using System.IO.StreamWriter writer = new(path, true, new UTF8Encoding(false)) { NewLine = "\n" };
                foreach (KeyValuePair<string, string> skipped in run.SkippedAssets)
                {
                    Dictionary<string, object> warning = new()
                    {
                        ["kind"] = "mesh",
                        ["assetId"] = skipped.Key,
                        ["reason"] = skipped.Value
                    };
                    writer.WriteLine(Newtonsoft.Json.JsonConvert.SerializeObject(warning, Newtonsoft.Json.Formatting.None));
                }

                run.Report.AppendLine($"Recorded {run.SkippedAssets.Count} skipped prefab(s) in warnings.jsonl");
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not append to warnings.jsonl: {ex.Message}");
            }
        }

        /// <summary>
        /// Write the report, clear the indicator and tell the user what came out.
        /// （寫出報告、清除指示器，並告知使用者結果。）
        /// </summary>
        /// <param name="run">The finished run.（已完成的工作。）</param>
        private void Finish(RipRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            try
            {
                run.Writer.Wait();
            }
            catch (Exception ex)
            {
                _log.Warn($"Mesh writer ended with an error: {ex}");
            }

            while (run.WriterNotes.TryDequeue(out string note)) run.Report.AppendLine(note);

            WriteMeshWarnings(run);

            // Sizes are the point of a capped run: they are what says whether the uncapped rip is
            // 3 GB or 30. Report the total, the mean and the worst offenders.
            // （大小正是受限擷取的重點：唯有它能說明未受限的擷取是 3 GB 還是 30 GB。
            //   回報總計、平均值與最大者。）
            List<KeyValuePair<string, long>> sizes = new(run.Sizes);
            sizes.Sort((a, b) => b.Value.CompareTo(a.Value));

            run.Report.AppendLine();
            run.Report.AppendLine($"Wrote {run.Written} .glb, skipped {run.Skipped}, failed {run.Failed}, in {run.Clock.Elapsed.TotalSeconds:F1} s.");
            run.Report.AppendLine($"Total {run.Bytes / (1024d * 1024d):F1} MiB, mean {(run.Written > 0 ? run.Bytes / (double)run.Written / 1024d : 0d):F0} KiB per prefab.");
            run.Report.AppendLine("Largest:");
            for (int i = 0; (i < 15) && (i < sizes.Count); i++)
                run.Report.AppendLine($"  {sizes[i].Value / 1024d:F0} KiB  {sizes[i].Key}");
            run.Report.AppendLine($"Output: {run.OutputDirectory}");

            _log.Info(run.Report.ToString());

            StringBuilder summary = new();
            summary.AppendLine($"Wrote {run.Written} mesh file(s), skipped {run.Skipped}, failed {run.Failed}.");
            summary.AppendLine($"{run.Bytes / (1024d * 1024d):F1} MiB in {run.Clock.Elapsed.TotalSeconds:F0} s.");
            summary.AppendLine();
            summary.Append("Full report: Logs/Carto.log");

            try
            {
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary.ToString(), "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the mesh rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Read full-detail geometry through the render prefab's <c>GeometryAsset</c>.
        /// （透過繪製預製模板的 <c>GeometryAsset</c> 讀取完整細節的幾何。）
        /// </summary>
        /// <param name="meshEntity">The sub-mesh (render prefab) entity.（子網格（繪製預製模板）實體。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <param name="trace">Optional diagnostics sink.（可選的診斷紀錄。）</param>
        /// <returns>The mesh, or <c>null</c> to fall back to the ECS buffers.（網格，若為 <c>null</c> 則退回 ECS 緩衝區。）</returns>
        private MeshAsset TryReadGeometryAsset(Entity meshEntity, string assetId, string sourceName, List<string> trace)
        {
            try
            {
                if (!Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase)
                    || prefabBase is not RenderPrefab renderPrefab)
                {
                    trace?.Add("geometry asset: entity does not resolve to a RenderPrefab");
                    return null;
                }

                if (!renderPrefab.hasGeometryAsset)
                {
                    trace?.Add("geometry asset: render prefab has none");
                    return null;
                }

                Colossal.IO.AssetDatabase.GeometryAsset asset = renderPrefab.geometryAsset;
                trace?.Add($"geometry asset: meshCount={renderPrefab.meshCount} vertexCount={renderPrefab.vertexCount} indexCount={renderPrefab.indexCount} materialCount={renderPrefab.materialCount}");

                // The asset's accessors stay empty until its data is resident, and there is no
                // public "ensure loaded" call — ObtainMeshes is the load trigger. Always release
                // again: a city has thousands of buildings and holding every mesh would balloon.
                // （資產的存取器在資料常駐前皆為空，且無公開的「確保載入」方法，ObtainMeshes 即為載入
                //   觸發點。務必釋放：城市有數千棟建築，全數保留將使記憶體暴增。）
                UnityEngine.Mesh[] loaded = null;
                try
                {
                    try
                    {
                        loaded = asset.ObtainMeshes(true);
                        trace?.Add($"geometry asset: ObtainMeshes returned {(loaded?.Length ?? 0)} mesh(es)");
                    }
                    catch (Exception ex)
                    {
                        trace?.Add($"geometry asset: ObtainMeshes failed — {ex.GetType().Name} {ex.Message}");
                    }

                    List<GeometryAssetReader.MeshInfo> meshes = GeometryAssetReader.Describe(asset, renderPrefab.meshCount);
                    if (trace != null) foreach (GeometryAssetReader.MeshInfo info in meshes) trace.Add($"  {info}");

                    if (meshes.Count > 0)
                    {
                        // Richest mesh wins, on the same reasoning as the ECS candidates: the LOD
                        // chain is ordered by convention, not by guarantee.
                        // （以最豐富者為準，理由同 ECS 候選：LOD 鏈的順序僅為慣例而非保證。）
                        GeometryAssetReader.MeshInfo best = meshes[0];
                        foreach (GeometryAssetReader.MeshInfo info in meshes)
                            if (info.IndexCount > best.IndexCount) best = info;

                        MeshAsset mesh = GeometryAssetReader.Read(asset, best.MeshIndex, assetId, sourceName);
                        if (mesh != null)
                        {
                            trace?.Add($"geometry asset: chose mesh{best.MeshIndex} ({mesh.TriangleCount} tris, {mesh.Primitives.Count} primitives)");
                            return mesh;
                        }
                    }

                    // Native accessors still empty: fall back to the loaded Unity meshes.
                    // （原生存取器仍為空：退回使用已載入的 Unity 網格。）
                    if ((loaded != null) && (loaded.Length > 0))
                    {
                        MeshAsset best = null;
                        foreach (UnityEngine.Mesh candidate in loaded)
                        {
                            MeshAsset converted = GeometryAssetReader.ReadUnityMesh(candidate, assetId, sourceName);
                            if ((converted != null) && ((best == null) || (converted.TriangleCount > best.TriangleCount))) best = converted;
                        }

                        if (best != null)
                        {
                            trace?.Add($"geometry asset: via Unity mesh ({best.TriangleCount} tris, {best.Primitives.Count} primitives)");
                            return best;
                        }
                    }

                    trace?.Add("geometry asset: no readable meshes after loading");
                    return null;
                }
                finally
                {
                    try { asset.ReleaseMeshes(); } catch { /* nothing useful to do if release fails */ }
                }
            }
            catch (Exception ex)
            {
                trace?.Add($"geometry asset: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Geometry asset unavailable for `{assetId}`: {ex.Message}");
                return null;
            }
        }

        /// <summary>
        /// Find the entity that actually holds geometry buffers, starting from a sub-mesh entity.
        /// （自子網格實體出發，尋找真正持有幾何緩衝區的實體。）<br/><br/>
        /// A render prefab entity is a container: it carries <c>MeshData</c>, a <c>LodMesh</c>
        /// buffer of per-LOD entities and a <c>SharedMeshData</c> pointer, but frequently no
        /// vertices of its own. Geometry hangs off one of those references, so follow them —
        /// <c>SharedMeshData</c> first, then LOD 0 — until a real vertex buffer turns up.<br/>
        /// （繪製預製模板實體是一個容器：帶有 <c>MeshData</c>、逐 LOD 實體的 <c>LodMesh</c> 緩衝區
        /// 與 <c>SharedMeshData</c> 指標，但往往不含自身的頂點。幾何掛在其中一個參照之下。）
        /// </summary>
        /// <param name="entity">The entity to start from.（起始實體。）</param>
        /// <param name="geometry">The entity holding the geometry buffers.（持有幾何緩衝區的實體。）</param>
        /// <param name="lod">The LOD level reached, <c>0</c> for full detail.（抵達的 LOD 層級，<c>0</c> 為完整細節。）</param>
        /// <param name="trace">Optional sink recording each hop, for diagnostics.（可選的走訪紀錄，供診斷使用。）</param>
        /// <param name="depth">The current recursion depth.（目前的遞迴深度。）</param>
        /// <returns>Whether geometry was found.（是否找到幾何。）</returns>
        private bool TryResolveGeometry(Entity entity, out Entity geometry, out int lod, List<string> trace, int depth = 0)
        {
            List<GeometryCandidate> candidates = new();
            Collect(entity, 0, "root", candidates, new HashSet<Entity>(), 0);

            if (trace != null)
            {
                trace.Add($"container {entity.Index} [{string.Join(", ", DescribeComponents(entity))}]");
                if (EntityManager.HasComponent<MeshData>(entity))
                {
                    MeshData data = EntityManager.GetComponentData<MeshData>(entity);
                    // What the container *claims* the mesh holds. If the richest candidate falls
                    // well short of m_IndexCount, we are reading a reduced LOD, not the real mesh.
                    // （容器所「宣稱」的網格內容。若最豐富的候選遠低於 m_IndexCount，即代表讀到的是簡化 LOD。）
                    trace.Add($"MeshData: indexCount={data.m_IndexCount} subMeshCount={data.m_SubMeshCount} minLod={data.m_MinLod} state={data.m_State}");
                }
                foreach (GeometryCandidate candidate in candidates)
                    trace.Add($"candidate via {candidate.Path}: entity {candidate.Entity.Index}, {candidate.VertexCount} verts, {candidate.IndexCount / 3} tris, materials={candidate.MaterialCount} [{string.Join(", ", DescribeComponents(candidate.Entity))}]");
            }

            if (candidates.Count == 0)
            {
                geometry = Entity.Null;
                lod = 0;
                return false;
            }

            // Pick the richest candidate. LOD 0 is by definition the one with the most geometry,
            // and picking by index count avoids depending on buffer ordering being detail-ordered.
            // （選取最豐富的候選。LOD 0 依定義即幾何最多者，以索引數選取可避免依賴緩衝區排序。）
            GeometryCandidate best = candidates[0];
            foreach (GeometryCandidate candidate in candidates)
                if (candidate.IndexCount > best.IndexCount) best = candidate;

            geometry = best.Entity;
            lod = best.Lod;
            trace?.Add($"chosen: {best.Path} ({best.IndexCount / 3} tris)");
            return true;
        }

        /// <summary>
        /// A reachable entity that carries real geometry, and how it was reached.
        /// （帶有實際幾何的可達實體，以及其抵達路徑。）
        /// </summary>
        private struct GeometryCandidate
        {
            public Entity Entity;
            public int Lod;
            public string Path;
            public int VertexCount;
            public int IndexCount;
            public int MaterialCount;
        }

        /// <summary>
        /// Walk every geometry reference reachable from <paramref name="entity"/>, collecting each
        /// entity that carries non-empty vertex and index buffers.<br/>
        /// （走訪自 <paramref name="entity"/> 可達的所有幾何參照，收集帶有非空頂點與索引緩衝區的實體。）<br/><br/>
        /// A render prefab entity is a container carrying <c>MeshData</c>, a <c>LodMesh</c> buffer
        /// and a <c>SharedMeshData</c> pointer, but often no vertices of its own. Taking the first
        /// match found risks silently settling for a reduced LOD, so gather all of them and let the
        /// caller choose.<br/>
        /// （繪製預製模板實體為容器，往往不含自身頂點。僅取首個符合者可能默默採用簡化 LOD，
        /// 因此全部收集後交由呼叫端選擇。）
        /// </summary>
        private void Collect(Entity entity, int lod, string path, List<GeometryCandidate> candidates, HashSet<Entity> visited, int depth)
        {
            if ((entity == Entity.Null) || (depth > 4) || !visited.Add(entity)) return;

            if (EntityManager.HasBuffer<MeshVertex>(entity) && EntityManager.HasBuffer<MeshIndex>(entity))
            {
                int vertexCount = EntityManager.GetBuffer<MeshVertex>(entity, true).Length;
                int indexCount = EntityManager.GetBuffer<MeshIndex>(entity, true).Length;
                if ((vertexCount > 0) && (indexCount > 0))
                {
                    candidates.Add(new GeometryCandidate
                    {
                        Entity = entity,
                        Lod = lod,
                        Path = path,
                        VertexCount = vertexCount,
                        IndexCount = indexCount,
                        MaterialCount = EntityManager.HasBuffer<MeshMaterial>(entity)
                            ? EntityManager.GetBuffer<MeshMaterial>(entity, true).Length : 0
                    });
                }
            }

            if (EntityManager.HasComponent<SharedMeshData>(entity))
                Collect(EntityManager.GetComponentData<SharedMeshData>(entity).m_Mesh, lod, $"{path}/shared", candidates, visited, depth + 1);

            if (EntityManager.HasBuffer<LodMesh>(entity))
            {
                DynamicBuffer<LodMesh> lods = EntityManager.GetBuffer<LodMesh>(entity, true);
                for (int i = 0; i < lods.Length; i++)
                    Collect(lods[i].m_LodMesh, i, $"{path}/lod{i}", candidates, visited, depth + 1);
            }
        }

        /// <summary>
        /// Collect the prefab entities worth ripping, preferring prefabs actually placed in the
        /// loaded city over the asset database's full catalogue.<br/>
        /// （收集值得擷取的預製模板實體，優先取用已放置於城市中者，而非資產資料庫的完整目錄。）<br/><br/>
        /// The first spike run walked the catalogue and found 20,017 prefabs whose meshes were all
        /// empty — unsurprising in hindsight, since the game has no reason to load geometry for an
        /// asset nobody placed. Placed buildings are the ones whose meshes must be resident.<br/>
        /// （首次驗證走訪目錄時找到 20,017 個預製模板，其網格皆為空。事後看來並不意外：
        /// 遊戲沒有理由為無人放置的資產載入幾何。已放置的建築才是網格必然常駐者。）
        /// </summary>
        /// <param name="report">The report to append discovery counts to.（用以附加探索統計的報告。）</param>
        /// <returns>Distinct prefab entities, in discovery order.（依探索順序排列的相異預製模板實體。）</returns>
        private List<Entity> GetCandidatePrefabs(StringBuilder report)
        {
            List<Entity> result = new();
            HashSet<Entity> seen = new();

            NativeArray<Entity> placed = _placedBuildingQuery.ToEntityArray(Allocator.TempJob);
            try
            {
                report.AppendLine($"Buildings placed in the city: {placed.Length}");
                foreach (Entity building in placed)
                {
                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(building).m_Prefab;
                    if ((prefabEntity != Entity.Null) && seen.Add(prefabEntity)) result.Add(prefabEntity);
                }
                report.AppendLine($"Distinct building prefabs in use: {result.Count}");
            }
            finally
            {
                placed.Dispose();
            }

            // An empty map (or the editor) has nothing placed; fall back to the catalogue so the
            // spike still reports something useful rather than silently doing nothing.
            // （空白地圖或編輯器中沒有已放置的物件；退回目錄查詢，使驗證仍能回報有用資訊。）
            if (result.Count == 0)
            {
                report.AppendLine("No placed buildings; falling back to the prefab catalogue.");
                NativeArray<Entity> catalogue = _subMeshPrefabQuery.ToEntityArray(Allocator.TempJob);
                try
                {
                    report.AppendLine($"Prefabs with a SubMesh buffer: {catalogue.Length}");
                    foreach (Entity prefabEntity in catalogue)
                    {
                        if (!Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefabEntity), out PrefabBase prefab)) continue;
                        if (prefab is not BuildingPrefab) continue;
                        if (seen.Add(prefabEntity)) result.Add(prefabEntity);
                    }
                }
                finally
                {
                    catalogue.Dispose();
                }
            }

            return result;
        }

        /// <summary>
        /// List the component and buffer types present on an entity, for diagnostics.
        /// （列出實體上存在的元件與緩衝區型別，供診斷使用。）
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
        /// Read one mesh entity's geometry into a <see cref="MeshAsset"/>.
        /// （將單一網格實體的幾何讀入 <see cref="MeshAsset"/>。）
        /// </summary>
        /// <param name="meshEntity">The mesh (render prefab) entity.（網格（繪製預製模板）實體。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The mesh, or <c>null</c> when the entity carries no geometry.（網格，若實體沒有幾何則為 <c>null</c>。）</returns>
        public MeshAsset ReadMesh(Entity meshEntity, string assetId, string sourceName, List<string> trace = null)
        {
            // Prefer the geometry asset: the ECS buffers only ever hold the reduced LOD the game
            // keeps CPU-side for raycasting, and carry no sub-mesh ranges at all.
            // （優先使用幾何資產：ECS 緩衝區僅存放遊戲為射線保留於 CPU 端的簡化 LOD，且不含子網格範圍。）
            MeshAsset fromAsset = TryReadGeometryAsset(meshEntity, assetId, sourceName, trace);
            if (fromAsset != null) return fromAsset;

            if (!TryResolveGeometry(meshEntity, out Entity geometryEntity, out int lod, trace)) return null;

            DynamicBuffer<MeshVertex> vertices = EntityManager.GetBuffer<MeshVertex>(geometryEntity, true);
            DynamicBuffer<MeshIndex> indices = EntityManager.GetBuffer<MeshIndex>(geometryEntity, true);

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertices.Length],
                Indices = new int[indices.Length]
            };

            for (int i = 0; i < vertices.Length; i++) mesh.Vertices[i] = vertices[i].m_Vertex;
            for (int i = 0; i < indices.Length; i++) mesh.Indices[i] = indices[i].m_Index;

            if (EntityManager.HasBuffer<MeshNormal>(geometryEntity))
            {
                DynamicBuffer<MeshNormal> normals = EntityManager.GetBuffer<MeshNormal>(geometryEntity, true);
                if (normals.Length == vertices.Length)
                {
                    mesh.Normals = new float3[normals.Length];
                    for (int i = 0; i < normals.Length; i++) mesh.Normals[i] = normals[i].m_Normal;
                }
            }

            if (EntityManager.HasBuffer<MeshUV0>(geometryEntity))
            {
                DynamicBuffer<MeshUV0> uvs = EntityManager.GetBuffer<MeshUV0>(geometryEntity, true);
                if (uvs.Length == vertices.Length)
                {
                    mesh.Uvs = new float2[uvs.Length];
                    for (int i = 0; i < uvs.Length; i++) mesh.Uvs[i] = uvs[i].m_Uv;
                }
            }

            if (EntityManager.HasBuffer<MeshTangent>(geometryEntity))
            {
                DynamicBuffer<MeshTangent> tangents = EntityManager.GetBuffer<MeshTangent>(geometryEntity, true);
                if (tangents.Length == vertices.Length)
                {
                    mesh.Tangents = new float4[tangents.Length];
                    for (int i = 0; i < tangents.Length; i++) mesh.Tangents[i] = tangents[i].m_Tangent;
                }
            }

            if (EntityManager.HasBuffer<MeshMaterial>(geometryEntity))
            {
                DynamicBuffer<MeshMaterial> materials = EntityManager.GetBuffer<MeshMaterial>(geometryEntity, true);
                for (int i = 0; i < materials.Length; i++)
                {
                    MeshMaterial material = materials[i];
                    mesh.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = material.m_StartIndex,
                        IndexCount = material.m_IndexCount,
                        StartVertex = material.m_StartVertex,
                        VertexCount = material.m_VertexCount,
                        MaterialIndex = material.m_MaterialIndex,
                        MaterialId = $"{assetId}_mat{material.m_MaterialIndex}"
                    });
                }
            }

            if (EntityManager.HasComponent<MeshData>(meshEntity))
            {
                MeshData data = EntityManager.GetComponentData<MeshData>(meshEntity);
                mesh.BoundsMin = data.m_Bounds.min;
                mesh.BoundsMax = data.m_Bounds.max;
            }

            return mesh;
        }

        /// <summary>
        /// Resolve a prefab entity's name, or <c>null</c> when it cannot be resolved.
        /// （解析預製模板實體的名稱，若無法解析則回傳 <c>null</c>。）
        /// </summary>
        /// <param name="entity">The prefab entity.（預製模板實體。）</param>
        /// <returns>The prefab name.（預製模板名稱。）</returns>
        private string GetPrefabName(Entity entity)
        {
            if (!EntityManager.HasComponent<PrefabData>(entity)) return null;
            return Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(entity), out PrefabBase prefab) ? prefab?.name : null;
        }

        /// <summary>
        /// Reduce a prefab name to the bundle contract's <c>assetId</c> pattern
        /// (<c>^[A-Za-z0-9][A-Za-z0-9._-]*$</c>), so it is safe both as a file name and as a
        /// cross-file join key.<br/>
        /// （將預製模板名稱縮減為束契約的 <c>assetId</c> 格式，使其可安全作為檔名與跨檔連結鍵。）
        /// </summary>
        /// <param name="name">The raw prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The sanitised asset id.（淨化後的資產 ID。）</returns>
        public static string SanitizeAssetId(string name)
        {
            if (string.IsNullOrWhiteSpace(name)) return "unnamed";

            StringBuilder builder = new(name.Length);
            foreach (char c in name)
            {
                bool keep = ((c >= 'A') && (c <= 'Z')) || ((c >= 'a') && (c <= 'z'))
                         || ((c >= '0') && (c <= '9')) || (c == '.') || (c == '_') || (c == '-');
                builder.Append(keep ? c : '_');
            }

            string result = builder.ToString();

            // The contract requires the first character to be alphanumeric.
            // （契約要求首字元必須是英數字。）
            int start = 0;
            while ((start < result.Length) && !char.IsLetterOrDigit(result[start])) start++;

            // Where sanitising destroys the name, the id must not. A prefab named entirely in a
            // non-Latin script reduces to nothing at all, and a first full-city rip proved the
            // consequence: five such prefabs all became `unnamed`, so four of their meshes were
            // silently overwritten by the fifth and the instance layer referenced one id for five
            // different assets. Falling back on a digest of the original name keeps distinct
            // prefabs distinct, and keeps both layers agreeing on the id, since both derive it
            // from the same prefab name.
            // （當淨化摧毀了名稱時，ID 不應隨之毀損。完全以非拉丁文字命名的預製模板會縮減為空，
            //   首次全城擷取即證實其後果：五個此類模板皆成為 `unnamed`，其中四個網格被第五個默默覆寫，
            //   且實例層以單一 ID 參照五個不同的資產。改以原始名稱的摘要作為後備，
            //   可使相異的預製模板保持相異，並使兩個層級對 ID 的認定一致，
            //   因為兩者皆自相同的預製模板名稱推導而來。）
            if (start >= result.Length) return $"asset_{Digest(name)}";

            result = result.Substring(start);

            // Truncation collides the same way, just more rarely: two long names sharing a prefix.
            // （截斷會以相同方式造成碰撞，只是更為罕見：兩個共用前綴的長名稱。）
            return (result.Length > 180) ? $"{result.Substring(0, 171)}_{Digest(name)}" : result;
        }

        /// <summary>
        /// A prefab's bundle <c>assetId</c>, and whether its managed prefab resolved at all.
        /// （預製模板的束 <c>assetId</c>，以及其受管預製模板是否成功解析。）
        /// </summary>
        public struct AssetIdEntry
        {
            /// <summary>The id both bundle layers join on.（兩個束層級用以連結的 ID。）</summary>
            public string AssetId;

            /// <summary>Whether a managed prefab was found behind the entity.（實體背後是否找到受管預製模板。）</summary>
            public bool Resolved;
        }

        /// <summary>
        /// Assign every prefab entity its bundle <c>assetId</c>, guaranteeing the ids are unique.
        /// （為每個預製模板實體指派其束 <c>assetId</c>，並保證這些 ID 唯一。）<br/><br/>
        /// <b>The id is the bundle's join key, so a duplicate is silent corruption:</b> two prefabs
        /// sharing an id means one mesh file overwrites the other and every instance of both then
        /// points at whichever won. A full-city rip proved it happens — writing 4,203 files left
        /// 4,201 on disk — because CS2 does not require prefab names to be unique.
        /// <see cref="SanitizeAssetId"/> alone cannot see this: it is handed one name at a time and
        /// duplication is a property of the whole set.<br/>
        /// （<b>ID 是束的連結鍵，重複即為無聲的資料損毀：</b>兩個預製模板共用 ID 意味著其中一個網格檔案
        /// 會覆寫另一個，而雙方的所有實例都將指向勝出者。全城擷取已證實此事會發生：
        /// 寫出 4,203 個檔案後磁碟上僅剩 4,201 個，因為 CS2 並不要求預製模板名稱唯一。）<br/><br/>
        /// Only the names that actually clash are suffixed, so ordinary ids stay readable. The
        /// suffix is a digest of the prefab's asset identifier, which makes the assignment a
        /// function of the prefab set rather than of iteration order — that is what lets
        /// <see cref="InstanceRipSystem"/> and this system compute the same ids independently.<br/>
        /// （僅有實際衝突的名稱會被加上後綴，使一般 ID 維持可讀。後綴為預製模板資產識別碼的摘要，
        /// 使指派結果僅取決於預製模板集合而非走訪順序；這正是兩個系統能各自算出相同 ID 的原因。）
        /// </summary>
        /// <param name="entityManager">The entity manager to read prefab data from.（用以讀取預製模板資料的實體管理器。）</param>
        /// <param name="prefabEntities">The distinct prefab entities to assign ids to.（待指派 ID 的相異預製模板實體。）</param>
        /// <param name="disambiguated">Receives how many ids needed a suffix.（接收需要加上後綴的 ID 數量。）</param>
        /// <returns>The id for every entity given.（所給定的每個實體對應的 ID。）</returns>
        public static Dictionary<Entity, AssetIdEntry> BuildAssetIds(EntityManager entityManager, ICollection<Entity> prefabEntities, out int disambiguated)
        {
            Dictionary<Entity, AssetIdEntry> result = new(prefabEntities.Count);
            Dictionary<string, List<Entity>> byBaseId = new(prefabEntities.Count);
            Dictionary<Entity, string> discriminators = new(prefabEntities.Count);

            foreach (Entity prefabEntity in prefabEntities)
            {
                PrefabBase prefab = null;
                if (entityManager.HasComponent<PrefabData>(prefabEntity))
                    Instance.Prefab.TryGetPrefab(entityManager.GetComponentData<PrefabData>(prefabEntity), out prefab);

                bool resolved = (prefab != null) && !string.IsNullOrEmpty(prefab.name);
                string baseId = resolved ? SanitizeAssetId(prefab.name) : $"unresolved_{prefabEntity.Index}";

                result[prefabEntity] = new AssetIdEntry { AssetId = baseId, Resolved = resolved };
                discriminators[prefabEntity] = Discriminator(prefab, prefabEntity);

                if (!byBaseId.TryGetValue(baseId, out List<Entity> sharing)) byBaseId[baseId] = sharing = new List<Entity>();
                sharing.Add(prefabEntity);
            }

            disambiguated = 0;
            foreach (KeyValuePair<string, List<Entity>> group in byBaseId)
            {
                if (group.Value.Count < 2) continue;

                foreach (Entity prefabEntity in group.Value)
                {
                    AssetIdEntry entry = result[prefabEntity];
                    entry.AssetId = $"{group.Key}_{Digest(discriminators[prefabEntity])}";
                    result[prefabEntity] = entry;
                    disambiguated++;
                }
            }

            return result;
        }

        /// <summary>
        /// Something that tells two same-named prefabs apart, stably across sessions.
        /// （能穩定區分兩個同名預製模板的依據，且跨執行階段一致。）<br/><br/>
        /// The asset identifier is unique per asset and survives a reload, which the entity index
        /// does not — an id derived from the entity index would change every time the save is
        /// loaded, and the two bundle layers are written by two separate button presses.<br/>
        /// （資產識別碼在每個資產上唯一且可跨重新載入；實體索引則否——由實體索引推導的 ID
        /// 每次載入存檔都會改變，而束的兩個層級是由兩次獨立的按鈕操作寫出的。）
        /// </summary>
        /// <param name="prefab">The managed prefab, if it resolved.（受管預製模板，若已解析。）</param>
        /// <param name="prefabEntity">The prefab entity, as a last resort.（預製模板實體，作為最後手段。）</param>
        /// <returns>A discriminating string.（用以區分的字串。）</returns>
        private static string Discriminator(PrefabBase prefab, Entity prefabEntity)
        {
            if (prefab == null) return $"entity:{prefabEntity.Index}";

            try
            {
                string identifier = prefab.asset?.identifier;
                if (!string.IsNullOrEmpty(identifier)) return identifier;
            }
            catch (Exception)
            {
                // An asset-less prefab (generated at runtime) throws rather than returning null.
                // （執行階段產生、不具資產的預製模板會擲出例外而非回傳 null。）
            }

            return $"{prefab.GetType().Name}:{prefab.name}";
        }

        /// <summary>
        /// A short, stable digest of a string, used to keep asset ids distinct when sanitising
        /// cannot.<br/>
        /// （字串的簡短穩定摘要，用於在淨化無法區分時保持資產 ID 的相異性。）<br/><br/>
        /// FNV-1a, written out rather than taken from <c>string.GetHashCode</c>, which is
        /// randomised per process and would hand the same prefab a different id on every run.<br/>
        /// （採用 FNV-1a 而非 <c>string.GetHashCode</c>：後者每個行程皆隨機化，
        /// 將使同一個預製模板每次執行都得到不同的 ID。）
        /// </summary>
        /// <param name="value">The original, unsanitised name.（原始未經淨化的名稱。）</param>
        /// <returns>Eight lowercase hex digits.（八位小寫十六進位數字。）</returns>
        private static string Digest(string value)
        {
            uint hash = 2166136261u;
            foreach (char c in value)
            {
                hash ^= c;
                hash *= 16777619u;
            }

            return hash.ToString("x8");
        }
    }
}
