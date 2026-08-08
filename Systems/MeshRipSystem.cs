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
        /// The query for the game's <c>MeshSettings</c> singleton, which names the default base
        /// mesh a building's foundation skirt draws with when its prefab declares no
        /// <c>BaseProperties</c> of its own.<br/>
        /// （遊戲 <c>MeshSettings</c> 單例的查詢；當預製模板未宣告自身的 <c>BaseProperties</c> 時，
        /// 建築基座裙板即以其指名的預設基座網格繪製。）
        /// </summary>
        EntityQuery _meshSettingsQuery;

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

            _meshSettingsQuery = GetEntityQuery(ComponentType.ReadOnly<MeshSettingsData>());

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
                Instance.MaterialRip.Abandon();
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
        /// One thing waiting to be written: either an assembled prefab or a read-back texture.
        /// （等待寫出的單一項目：已組裝的預製模板或已回讀的貼圖，二者其一。）<br/><br/>
        /// <b>Both travel in one queue, drained by one writer, in the order they were produced.</b>
        /// That ordering is not incidental — it is what lets a <c>.glb</c> reference its textures.
        /// A texture's bundle path is the content hash of the PNG it becomes, which is only known
        /// after encoding; the reader resolves and queues a prefab's textures before it queues the
        /// prefab, so a single ordered writer always encodes them first and the mesh can name the
        /// files by the time it is serialised. Two queues, or two writers, would race.<br/>
        /// （<b>兩者共用單一佇列，由單一寫出端依產生順序排空。</b>此順序並非偶然，
        /// 而是 <c>.glb</c> 得以參照其貼圖的關鍵。貼圖的束路徑是其所成 PNG 的內容雜湊，
        /// 須待編碼後方能得知；讀取端於排入預製模板之前先解析並排入其貼圖，
        /// 因此單一有序的寫出端必定先編碼貼圖，網格序列化時即可指名這些檔案。
        /// 若採用兩個佇列或兩個寫出端，則會發生競爭。）
        /// </summary>
        private struct RipItem
        {
            /// <summary>The prefab to serialise, or <c>null</c>.（待序列化的預製模板，或 <c>null</c>。）</summary>
            public PrefabMesh Prefab;

            /// <summary>The texture to encode, or <c>null</c>.（待編碼的貼圖，或 <c>null</c>。）</summary>
            public PendingTexture Texture;

            /// <summary>A foundation skirt to generate and serialise, or <c>null</c>.（待產生並序列化的基座裙板，或 <c>null</c>。）</summary>
            public SkirtWork Skirt;
        }

        /// <summary>
        /// One prefab's foundation-skirt job, generated on the writer task so the silhouette scan
        /// costs the main thread nothing.
        /// （單一預製模板的基座裙板工作，於寫出工作中產生，使輪廓掃描不佔主執行緒。）<br/><br/>
        /// A prefab part whose <c>MeshData</c> carries <c>MeshFlags.Base</c> draws one extra
        /// generated batch — the skirt between its floor and the terrain (see
        /// <see cref="IO.BaseSkirtBuilder"/>). The band is written as
        /// <c>&lt;assetId&gt;__base.glb</c>, separate from the prefab's own file, because it obeys
        /// a draw rule of its own: a consumer drops its bottom ring to the terrain surface, and
        /// skips it entirely for instances not resting on the ground.<br/>
        /// （<c>MeshData</c> 帶有 <c>MeshFlags.Base</c> 的部件會多繪製一批產生的幾何——
        /// 地板與地形之間的裙板。帶狀幾何寫為獨立的 <c>&lt;assetId&gt;__base.glb</c>，
        /// 因其有自己的繪製規則：消費端將其下環下拉至地形表面，且對未貼地的實例整體略過。）
        /// </summary>
        private class SkirtWork
        {
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
            public List<SkirtPart> Parts = new();
        }

        /// <inheritdoc cref="SkirtWork"/>
        private struct SkirtPart
        {
            public MeshAsset Source;
            public float BaseOffset;
            public float3 Position;
            public quaternion Rotation;
            public string NodeName;
            public string MaterialId;
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

            /// <summary>Assembled prefabs and read-back textures waiting to be written.（等待寫出的已組裝預製模板與已回讀貼圖。）</summary>
            public BlockingCollection<RipItem> Queue;

            /// <summary>The background serialiser.（背景序列化工作。）</summary>
            public Task Writer;

            /// <summary>
            /// The material system, resolved on the main thread and held for the writer.
            /// （材質系統，於主執行緒解析後供寫出端持有。）<br/><br/>
            /// Looking a system up goes through the ECS world, which is a main-thread affair; the
            /// writer needs the reference but must not be the one to obtain it.<br/>
            /// （查找系統須經由 ECS 世界，屬主執行緒事務；寫出端需要該參照，但不應由它取得。）
            /// </summary>
            public MaterialRipSystem Materials;

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

            /// <summary>
            /// Foundation skirts written, and Base-flagged parts whose mesh presented no
            /// silhouette at the base level (a valid outcome — the game generates an empty batch
            /// for those too). Writer-task counters, read after it drains.
            /// （已寫出的基座裙板數，以及帶 Base 旗標但於基準面無輪廓的部件數（此為有效結果——
            ///   遊戲對其亦產生空批次）。寫出工作的計數器，於其排空後讀取。）
            /// </summary>
            public int SkirtsWritten, SkirtsEmpty;

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
            public List<RipWarning> SkippedAssets = new();

            /// <summary>The report accumulated on the main thread.（於主執行緒累積的報告。）</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.（整體工作的實際經過時間。）</summary>
            public Stopwatch Clock = Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// （由管線驅動時完成摘要的去處；<c>null</c> 代表手動啟動，經自身對話框回報。）
            /// </summary>
            public Action<string> Completed;
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
        /// How many items may wait for the writer.（可等待寫出端的項目數量上限。）<br/><br/>
        /// Every item holds a full uncompressed payload — a prefab's vertex arrays, or a texture's
        /// pixels at four bytes each — so an unbounded queue would simply move the memory problem
        /// rather than solve it. The reader pauses when the queue is full.<br/>
        /// （每個項目都持有完整未壓縮的內容：預製模板的頂點陣列，或每像素四位元組的貼圖像素。
        /// 因此無上限的佇列只是把記憶體問題搬家而非解決。佇列滿時讀取端暫停。）
        /// </summary>
        private const int QueueCapacity = 8;

        /// <summary>
        /// Whether a rip is currently running.（目前是否有擷取工作進行中。）
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How many prefabs merged more than one geometry-asset mesh since the rip began.
        /// （自擷取開始以來，合併了多於一個幾何資產網格的預製模板數量。）<br/><br/>
        /// Zero on a vanilla city means every placed prefab keeps one mesh per asset; a non-zero
        /// count is the rule <c>RenderPrefab.ObtainMesh</c> flattens actually firing, and the
        /// report states it rather than leaving the merge unobserved.<br/>
        /// （在原版城市中為零代表所有已放置的預製模板每資產僅一個網格；非零則代表
        /// <c>RenderPrefab.ObtainMesh</c> 所平坦化的規則確實生效，報告據實陳述而非任其無人聞問。）
        /// </summary>
        public int MultiMeshPrefabs { get; private set; }

        /// <summary>
        /// How many prefabs yielded fewer meshes than their geometry asset holds.
        /// （產出網格數少於其幾何資產所持有者的預製模板數量。）<br/><br/>
        /// A geometry asset's meshes are simultaneous parts, so a partial merge is dropped
        /// geometry — the adult oak's leaves are mesh 1 behind the same asset as its trunk, and
        /// losing them silently is how every tree in a bundle came out dead. Non-zero demands a
        /// look at the log, where each drop names its prefab.<br/>
        /// （幾何資產的網格是同時繪製的部件，部分合併即為丟失的幾何——成年橡樹的樹葉是與樹幹
        /// 同一資產下的網格 1，無聲丟棄它便是整個束中每棵樹都枯死的原因。非零時應查看記錄，
        /// 每筆丟棄都記有其預製模板名稱。）
        /// </summary>
        public int PartialMeshPrefabs { get; private set; }

        /// <summary>
        /// Sub-mesh entries skipped because every layer they default to is an underground cutaway
        /// layer — the per-part form of the §17.5 prefab rule, for prefabs that mix visible parts
        /// with utility furniture.
        /// （因其預設圖層全屬地下剖視層而略過的子網格條目——§17.5 預製模板規則的逐部件形式，
        /// 用於可見部件與公用設施配件混雜的預製模板。）
        /// </summary>
        public int PipelinePartsSkipped { get; private set; }

        /// <summary>
        /// How many sub-mesh entries were the obsolete-content placeholder, stretched to the
        /// prefab's bounds the way the renderer stretches it.
        /// （屬於過時內容佔位網格、並依繪製器方式拉伸至預製模板邊界的子網格條目數量。）
        /// </summary>
        public int PlaceholderParts { get; private set; }

        /// <summary>
        /// How many stacked prefabs also carried parts with no stack role, shipped as the
        /// <c>__stack_rest</c> file the instance pass places once per stacked instance.
        /// （另帶有無堆疊角色部件的堆疊式預製模板數量；該等部件以 <c>__stack_rest</c> 檔案輸出，
        /// 由實例階段為每個堆疊實例擺放一次。）
        /// </summary>
        public int StackRestParts { get; private set; }

        /// <summary>
        /// Where a partial-read declaration goes when no mesh rip is in flight.
        /// （無網格擷取進行中時，部分讀取宣告的去處。）<br/><br/>
        /// The network rip reads lane part meshes through <see cref="ReadMesh"/>; a mesh that
        /// comes up short there was declared only when a <i>mesh</i> run existed — a full-city
        /// rip logged <c>Highway_Lane_4_Mesh</c> unreadable and its bundle's
        /// <c>warnings.jsonl</c> said nothing. The active network run registers its own warning
        /// list here for the duration (contract §10: incompleteness is declared, never silent).
        /// （路網擷取經由 <see cref="ReadMesh"/> 讀取車道部件網格；該處短少的網格過去僅在
        /// <i>網格</i>擷取存在時才被宣告——全城擷取記錄了 <c>Highway_Lane_4_Mesh</c> 無法讀取，
        /// 而其束的 <c>warnings.jsonl</c> 卻隻字未提。進行中的路網擷取於此註冊其警告列表
        /// （契約 §10：不完整必須宣告，絕不沉默）。）
        /// </summary>
        public List<RipWarning> WarningSink;

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
        /// <param name="textureSizeCap">The longest edge a written texture may have, or <c>0</c> for no cap.（寫出貼圖的最長邊上限，<c>0</c> 為不限。）</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.（由管線驅動時接收完成摘要以取代對話框。）</param>
        /// <returns>A short description of what was queued.（已排入工作的簡短說明。）</returns>
        public string BeginRip(string outputDirectory, int maxPrefabs = 0, int textureSizeCap = 2048, Action<string> completed = null)
        {
            if (_run != null) return "A mesh rip is already running.";

            MultiMeshPrefabs = 0;
            PartialMeshPrefabs = 0;
            PipelinePartsSkipped = 0;
            PlaceholderParts = 0;
            StackRestParts = 0;

            RipProgress.Report(RipProgress.Phase.Scan);

            RipRun run = new()
            {
                OutputDirectory = IOUtils.CombinePath(outputDirectory, "meshes"),
                Queue = new BlockingCollection<RipItem>(QueueCapacity),
                Materials = Instance.MaterialRip,
                Completed = completed
            };

            run.Items = CollectWorkItems(run.Report, maxPrefabs, out int distinctPrefabs, out int placedObjects);
            run.Report.AppendLine($"Placed objects: {placedObjects}, distinct prefabs: {distinctPrefabs}, queued: {run.Items.Count}");

            if (run.Items.Count == 0)
            {
                RipProgress.Clear();
                return "Nothing to rip: no placed objects resolved to a prefab.";
            }

            // Materials ride along with the meshes: a prefab's surface assets are reached through
            // the very render prefab its geometry came from, so resolving them in a second pass
            // would mean walking the whole prefab set twice for data already in hand.
            // （材質隨網格一同擷取：預製模板的表面資產正是經由其幾何來源的繪製預製模板取得，
            //   若另闢第二回合解析，等於為已在手的資料再走訪整個預製模板集合一次。）
            run.Materials.Begin(outputDirectory, textureSizeCap,
                texture => run.Queue.Add(new RipItem { Texture = texture }));

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
                int utilityNodes = 0, vehicleCargo = 0;

                foreach (Entity entity in entities)
                {
                    // Dropped before it is counted, so the prefab set here matches the one
                    // InstanceRipSystem builds and the two layers assign the same ids.
                    // （於計數前即丟棄，使此處的預製模板集合與 InstanceRipSystem 所建者一致，
                    //   兩層因而指派相同的 ID。）
                    if (IsUtilityMarkerSubObject(EntityManager, entity))
                    {
                        utilityNodes++;
                        continue;
                    }

                    if (IsVehicleCargoSubObject(EntityManager, entity))
                    {
                        vehicleCargo++;
                        continue;
                    }

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity == Entity.Null) continue;

                    counts.TryGetValue(prefabEntity, out int count);
                    counts[prefabEntity] = count + 1;
                }

                if (utilityNodes > 0) report.AppendLine($"Skipped {utilityNodes} buried utility-network node object(s).");
                if (vehicleCargo > 0) report.AppendLine($"Skipped {vehicleCargo} cargo prop(s) riding vehicles.");
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
                PrefabMesh prefab = BuildPrefabMesh(item, out string skipReason, out SkirtWork skirt);

                if (prefab == null)
                {
                    run.Skipped++;
                    run.SkippedAssets.Add(new RipWarning("mesh", item.AssetId, skipReason));
                    if (run.Skipped <= 20) run.Report.AppendLine($"  - {item.AssetId}: {skipReason}");
                    return;
                }

                run.Read++;
                run.Queue.Add(new RipItem { Prefab = prefab });

                // The skirt rides behind the prefab that owns it. Its base surface was resolved —
                // and its textures queued — while the prefab was assembled, so the ordered writer
                // encodes them before the band's .glb needs their names.
                // （裙板緊隨其所屬的預製模板。其基座表面於組裝該模板時解析、貼圖亦已排入，
                //   故有序的寫出端必在帶狀 .glb 需要其名稱前先行編碼。）
                if (skirt != null) run.Queue.Add(new RipItem { Skirt = skirt });

                // A stacked prefab also ships one file per stack role, so that the instance pass
                // can place a foot once, a repeating section as many times as that instance needs,
                // and a cap once. The whole-prefab file above stays, and is still the right thing
                // for an instance of the same prefab that has no `Stack` component — a pillar used
                // as a standalone prop rather than under a viaduct.
                // （堆疊式預製模板另外為每個堆疊角色輸出一個檔案，使實例階段能夠將基座擺放一次、
                //   將可重複區段依該實例所需擺放多次、並將頂蓋擺放一次。上方的整體預製模板檔案予以保留，
                //   對於同一預製模板但不具 `Stack` 元件的實例（作為獨立道具而非高架橋下的橋墩）
                //   它仍是正確的選擇。）
                bool stacked = false;
                foreach (StackRole role in prefab.StackRoles())
                {
                    stacked = true;
                    PrefabMesh part = new()
                    {
                        AssetId = PrefabMesh.StackPartId(prefab.AssetId, role),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == role) part.Parts.Add(p);

                    run.Queue.Add(new RipItem { Prefab = part });
                }

                // Parts with no stack role on a stacked prefab are drawn once, untiled, at the
                // instance transform — the game's sub-mesh test grants `IsStack*` bits through the
                // instance's `Stack` component and an unflagged entry passes regardless. They get
                // their own file because the instance pass never references the whole-prefab file
                // for a stacked prefab (every instance of one carries `Stack` by archetype), and
                // the stack part files must not double them per tile.
                // （堆疊式預製模板上無堆疊角色的部件僅於實例變換處繪製一次、不鋪排——
                //   遊戲的子網格測試經由實例的 `Stack` 元件授予 `IsStack*` 位元，
                //   而未標記的條目無論如何都會通過。它們獲得自己的檔案，因為實例階段對堆疊式
                //   預製模板從不參照整體檔案（其每個實例依原型必帶 `Stack`），
                //   且堆疊部件檔案不得使其隨鋪排重複。）
                if (stacked)
                {
                    PrefabMesh rest = new()
                    {
                        AssetId = PrefabMesh.StackRestId(prefab.AssetId),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == null) rest.Parts.Add(p);

                    if (rest.Parts.Count > 0)
                    {
                        StackRestParts++;
                        run.Queue.Add(new RipItem { Prefab = rest });
                    }
                }
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
            MaterialRipSystem materials = run.Materials;

            foreach (RipItem item in run.Queue.GetConsumingEnumerable())
            {
                // Textures first, always: they arrive before the prefab that named them, and
                // encoding one is what gives it the content hash the prefab's .glb writes down.
                // （貼圖恆先處理：它們早於指名它們的預製模板抵達，
                //   而編碼貼圖正是賦予其內容雜湊的動作，該雜湊將寫入預製模板的 .glb。）
                if (item.Texture != null)
                {
                    materials.WriteTexture(item.Texture);
                    continue;
                }

                if (item.Skirt != null)
                {
                    WriteSkirt(run, item.Skirt);
                    continue;
                }

                PrefabMesh prefab = item.Prefab;
                if (prefab == null) continue;

                try
                {
                    string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                    long size = GltfWriter.Write(path, prefab, materials.Describe);

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
        /// Generate one prefab's foundation-skirt band and write it as
        /// <c>&lt;assetId&gt;__base.glb</c>. Runs on the writer task.
        /// （產生單一預製模板的基座裙板帶並寫為 <c>&lt;assetId&gt;__base.glb</c>。於寫出工作中執行。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="skirt">The gathered inputs.（已收集的輸入。）</param>
        private static void WriteSkirt(RipRun run, SkirtWork skirt)
        {
            try
            {
                PrefabMesh prefab = new()
                {
                    AssetId = skirt.AssetId,
                    SourceName = skirt.SourceName,
                    InstanceCount = skirt.InstanceCount
                };

                foreach (SkirtPart part in skirt.Parts)
                {
                    MeshAsset band = BaseSkirtBuilder.Build(part.Source, part.BaseOffset,
                                                            skirt.AssetId, skirt.SourceName, part.MaterialId);
                    if (band == null) continue;

                    prefab.Parts.Add(new PrefabMeshPart
                    {
                        Mesh = band,
                        Position = part.Position,
                        Rotation = part.Rotation,
                        NodeName = part.NodeName
                    });
                }

                // No silhouette at the base level is a valid outcome — the game generates an empty
                // batch there too — so it is counted, not warned about.
                // （基準面無輪廓屬有效結果——遊戲於該處亦產生空批次——故僅計數而不警告。）
                if (prefab.Parts.Count == 0)
                {
                    System.Threading.Interlocked.Increment(ref run.SkirtsEmpty);
                    return;
                }

                string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                long size = GltfWriter.Write(path, prefab, run.Materials.Describe);

                System.Threading.Interlocked.Increment(ref run.SkirtsWritten);
                System.Threading.Interlocked.Add(ref run.Bytes, size);
                run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
            }
            catch (Exception ex)
            {
                System.Threading.Interlocked.Increment(ref run.WriteFailed);
                run.WriterNotes.Enqueue($"  ! {skirt.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to write `{skirt.AssetId}.glb`: {ex}");
            }
        }

        /// <summary>
        /// Assemble a prefab entity's sub-meshes into one <see cref="PrefabMesh"/>.
        /// （將預製模板實體的子網格組裝為單一 <see cref="PrefabMesh"/>。）
        /// </summary>
        /// <param name="item">The prefab to assemble.（待組裝的預製模板。）</param>
        /// <param name="skipReason">Why nothing came out, when the result is <c>null</c>.（結果為 <c>null</c> 時，說明未能產出的原因。）</param>
        /// <returns>The assembled prefab, or <c>null</c>.（已組裝的預製模板，或 <c>null</c>。）</returns>
        private PrefabMesh BuildPrefabMesh(WorkItem item, out string skipReason, out SkirtWork skirt)
        {
            skipReason = null;
            skirt = null;

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

                PrefabMeshPart part = new()
                {
                    Mesh = mesh,
                    Position = subMeshes[index].m_Position,
                    Rotation = subMeshes[index].m_Rotation,
                    NodeName = nodeName,
                    StackRole = PrefabMesh.RoleOf(subMeshes[index].m_Flags)
                };

                // The obsolete-content placeholder — the box a prefab gets when its asset went
                // missing — is not drawn at its authored size. The renderer stretches it over
                // `ObjectGeometryData.m_Bounds` (`BatchDataSystem.UpdateObjectData`,
                // `DefaultMissingMesh` branch: translate to the bounds' centre, scale to half
                // their size), so the exported node carries the same TRS.
                // （過時內容的佔位網格——資產遺失時預製模板所得的方盒——並非以其製作尺寸繪製。
                //   繪製器將其拉伸覆蓋 `ObjectGeometryData.m_Bounds`（平移至邊界中心、
                //   縮放至其半尺寸），故輸出節點帶有相同的 TRS。）
                if (((subMeshes[index].m_Flags & SubMeshFlags.DefaultMissingMesh) != 0)
                    && EntityManager.HasComponent<ObjectGeometryData>(item.PrefabEntity))
                {
                    Colossal.Mathematics.Bounds3 bounds = EntityManager.GetComponentData<ObjectGeometryData>(item.PrefabEntity).m_Bounds;
                    part.Position += (bounds.min + bounds.max) * 0.5f;
                    part.Scale = (bounds.max - bounds.min) * 0.5f;
                    PlaceholderParts++;
                }

                prefab.Parts.Add(part);

                // A part whose MeshData carries `MeshFlags.Base` draws one extra generated batch:
                // the foundation skirt between its floor and the terrain. `MeshSystem` sets the
                // flag for a render prefab declaring `BaseProperties`, and
                // `ObjectInitializeSystem` grants it to every building mesh even without one, so
                // the ECS state — not the managed component — is the authoritative gate. The
                // silhouette scan itself runs on the writer task; here only the inputs are
                // gathered, including the base surface, which must be resolved on the main thread
                // while the material window is open.
                // （MeshData 帶有 `MeshFlags.Base` 的部件會多繪製一批產生的幾何：地板與地形之間的
                //   基座裙板。`MeshSystem` 為宣告 `BaseProperties` 的繪製預製模板設置此旗標，
                //   `ObjectInitializeSystem` 則授予每個建築網格，即使其未宣告——故以 ECS 狀態
                //   而非受控元件為權威判準。輪廓掃描於寫出工作中執行；此處僅收集輸入，
                //   包括必須於主執行緒、材質窗口開啟期間解析的基座表面。）
                if (EntityManager.HasComponent<MeshData>(meshEntity))
                {
                    MeshData meshData = EntityManager.GetComponentData<MeshData>(meshEntity);
                    if ((meshData.m_State & MeshFlags.Base) != 0)
                    {
                        skirt ??= new SkirtWork
                        {
                            AssetId = $"{item.AssetId}__base",
                            SourceName = item.SourceName,
                            InstanceCount = item.InstanceCount
                        };

                        skirt.Parts.Add(new SkirtPart
                        {
                            Source = mesh,
                            BaseOffset = ((meshData.m_State & MeshFlags.MinBounds) != 0) ? meshData.m_Bounds.min.y : 0f,
                            Position = part.Position,
                            Rotation = part.Rotation,
                            NodeName = $"{nodeName}__base",
                            MaterialId = ResolveBaseMaterial(meshEntity)
                        });
                    }
                }
            }

            if (prefab.Parts.Count == 0)
            {
                skipReason = $"none of {subMeshes.Length} sub-mesh(es) yielded geometry";
                return null;
            }

            return prefab;
        }

        /// <summary>
        /// Resolve the surface a part's foundation skirt draws with: surface 0 of the render
        /// prefab's <c>BaseProperties.m_BaseType</c>, or of the game-wide default base mesh when
        /// the prefab declares none (<c>ManagedBatchSystem.CreateBatch</c>, the
        /// <c>GeneratedType.ObjectBase</c> branch).<br/>
        /// （解析部件基座裙板所用的表面：繪製預製模板 <c>BaseProperties.m_BaseType</c> 的表面 0，
        /// 或當模板未宣告時，全遊戲預設基座網格的表面 0。）
        /// </summary>
        /// <param name="meshEntity">The part's mesh entity.（部件的網格實體。）</param>
        /// <returns>The bundle material id.（束材質 ID。）</returns>
        private string ResolveBaseMaterial(Entity meshEntity)
        {
            RenderPrefab baseType = null;

            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase)
                && (prefabBase is RenderPrefab renderPrefab))
            {
                BaseProperties properties = renderPrefab.GetComponent<BaseProperties>();
                if ((properties != null) && (properties.m_BaseType != null)) baseType = properties.m_BaseType;
            }

            if ((baseType == null) && (_meshSettingsQuery.CalculateEntityCount() == 1))
            {
                MeshSettingsData settings = _meshSettingsQuery.GetSingleton<MeshSettingsData>();
                if ((settings.m_DefaultBaseMesh != Entity.Null)
                    && EntityManager.HasComponent<PrefabData>(settings.m_DefaultBaseMesh)
                    && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(settings.m_DefaultBaseMesh), out PrefabBase defaultBase))
                    baseType = defaultBase as RenderPrefab;
            }

            string materialId = ((baseType != null) && Instance.MaterialRip.IsRipping)
                              ? Instance.MaterialRip.Resolve(baseType, 0) : null;

            if (string.IsNullOrEmpty(materialId))
            {
                materialId = "base_default_mat0";
                if (Instance.MaterialRip.IsRipping)
                    Instance.MaterialRip.Declare(materialId, "base surface did not resolve");
            }

            return materialId;
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
        public static SubMeshFlags DefaultState => SubMeshFlags.RequireSafe | SubMeshFlags.RequireAdult
            | SubMeshFlags.RequireEmpty | SubMeshFlags.RequireClear | SubMeshFlags.RequireForward
            | (Instance.City.leftHandTraffic ? SubMeshFlags.RequireLeftHandTraffic : SubMeshFlags.RequireRightHandTraffic);

        /// <summary>
        /// Whether one sub-mesh entry belongs in the exported state.
        /// （單一子網格條目是否屬於輸出狀態。）<br/><br/>
        /// Public and static so <see cref="InstanceRipSystem"/> can apply the identical test when
        /// deciding whether a stacked prefab has untiled rest parts — the two layers must agree on
        /// which entries exist or the <c>__stack_rest</c> reference dangles.<br/>
        /// （設為公開靜態，使 <see cref="InstanceRipSystem"/> 於判斷堆疊式預製模板是否帶有
        /// 不鋪排的其餘部件時能套用完全相同的測試——兩層對條目的認定必須一致，
        /// 否則 <c>__stack_rest</c> 參照將懸空。）
        /// </summary>
        /// <param name="flags">The entry's flags.（條目的旗標。）</param>
        /// <returns>Whether the entry is exported.（該條目是否輸出。）</returns>
        public static bool ExportsSubMesh(SubMeshFlags flags)
            => ((flags & SubMeshFlags.OutlineOnly) == 0) && (((flags & RequireFlags) & ~DefaultState) == 0);

        /// <summary>
        /// Choose which sub-mesh entries belong in the exported file.
        /// （選出應納入輸出檔案的子網格條目。）
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.（預製模板的子網格條目。）</param>
        /// <returns>The indices to export.（要輸出的索引。）</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes)
        {
            List<int> selected = new();

            for (int i = 0; i < subMeshes.Length; i++)
            {
                // An `OutlineOnly` entry is never world geometry. The game clears every layer but
                // `Outline` for it (`BatchInstanceJob.UpdateObjectInstances` does
                // `layers &= ~Outline; if (OutlineOnly) layers &= Outline;`), so outside a tool's
                // selection highlight its layer mask is empty and it draws nothing at all. It is
                // the silhouette shell a highlight is drawn from, and exporting it buries the real
                // mesh inside a second copy of itself.
                // （`OutlineOnly` 條目絕非世界幾何。遊戲會為其清除 `Outline` 以外的所有圖層
                //   （`BatchInstanceJob.UpdateObjectInstances` 執行
                //   `layers &= ~Outline; if (OutlineOnly) layers &= Outline;`），
                //   故在工具的選取高亮之外，其圖層遮罩為空，完全不會繪製。
                //   它是繪製高亮所用的輪廓外殼，輸出它會使真正的網格埋在自身的第二個複本之中。）
                // Unconditional entries are always drawn; conditional ones only when everything
                // they ask for holds in the exported state.
                // （無條件的條目一律繪製；有條件者僅在其所要求的一切於輸出狀態下皆成立時繪製。）
                if (!ExportsSubMesh(subMeshes[i].m_Flags)) continue;

                // The game replaces a part's batch layer with its `m_DefaultLayers` when set
                // (`UpdateObjectInstances`), so a part defaulting only to Pipeline/SubPipeline —
                // utility hookup furniture inside an otherwise visible prefab — never draws in
                // the world view. The per-part form of the §17.5 rule, which drops whole
                // instances when *every* part is such.
                // （遊戲於部件設有 `m_DefaultLayers` 時以之取代其批次圖層，故僅預設至
                //   Pipeline／SubPipeline 的部件——可見預製模板內的公用設施接點配件——
                //   絕不在世界視圖繪製。此為 §17.5 規則的逐部件形式。）
                if (EntityManager.HasComponent<MeshData>(subMeshes[i].m_SubMesh))
                {
                    const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
                    MeshLayer layers = EntityManager.GetComponentData<MeshData>(subMeshes[i].m_SubMesh).m_DefaultLayers;
                    if ((layers != 0) && ((layers & ~underground) == 0))
                    {
                        PipelinePartsSkipped++;
                        continue;
                    }
                }

                selected.Add(i);
            }

            // A prefab whose every entry is conditional — some props exist only as state variants —
            // would otherwise produce no file at all, breaking the contract's promise that every
            // referenced assetId resolves. Fall back to its first entry.
            // （若預製模板的每個條目皆有條件（部分道具僅以狀態變體存在），將完全不產生檔案，
            //   違背契約中「所有被參照的 assetId 皆可解析」的承諾。此時退回使用其首個條目。）
            // The fallback skips outline shells for the same reason the loop does: falling back to
            // one would hand the bundle a silhouette in place of the object.
            // （後備方案基於與迴圈相同的理由略過輪廓外殼：退回使用它會讓束得到輪廓而非物件本身。）
            if (selected.Count == 0)
            {
                for (int i = 0; i < subMeshes.Length; i++)
                {
                    if ((subMeshes[i].m_Flags & SubMeshFlags.OutlineOnly) != 0) continue;
                    selected.Add(i);
                    break;
                }
            }

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
            // No early return on an empty list: a rip that closed every gap still has to clear the
            // declarations the previous one left, or the bundle keeps claiming losses it no longer
            // has.（即使清單為空也不提前返回：已修復所有缺口的擷取仍須清除前次留下的宣告，
            //   否則束將持續宣稱其實已不存在的缺失。）

            // warnings.jsonl sits at the bundle root, one level above meshes/.
            // （warnings.jsonl 位於束的根目錄，即 meshes/ 的上一層。）
            string root = System.IO.Path.GetDirectoryName(run.OutputDirectory);
            string path = IOUtils.CombinePath(root, "warnings.jsonl");

            try
            {
                // Drop this rip's own kinds from any previous run before writing them again.
                // The instance pass owns the file and its lines must survive, but re-ripping
                // meshes used to append a second full set — so a bundle ripped twice declared
                // every gap twice, and a gap since closed stayed declared forever.
                // （寫入前先移除前次工作中屬於本次擷取之類別的行。實例階段擁有此檔案，其行必須保留；
                //   但重新擷取網格過去會再附加一整份，使擷取兩次的束將每個缺口宣告兩次，
                //   且已修復的缺口仍永久留存。）
                List<string> kept = new();
                if (System.IO.File.Exists(path))
                {
                    foreach (string line in System.IO.File.ReadAllLines(path))
                    {
                        if (string.IsNullOrWhiteSpace(line)) continue;
                        if (!IsOwnedByMeshRip(line)) kept.Add(line);
                    }
                }

                using System.IO.StreamWriter writer = new(path, false, new UTF8Encoding(false)) { NewLine = "\n" };
                foreach (string line in kept) writer.WriteLine(line);

                foreach (RipWarning skipped in run.SkippedAssets)
                {
                    Dictionary<string, object> warning = new()
                    {
                        ["kind"] = skipped.Kind,
                        ["assetId"] = skipped.AssetId,
                        ["reason"] = skipped.Reason
                    };
                    writer.WriteLine(Newtonsoft.Json.JsonConvert.SerializeObject(warning, Newtonsoft.Json.Formatting.None));
                }

                run.Report.AppendLine($"Recorded {run.SkippedAssets.Count} gap(s) in warnings.jsonl, keeping {kept.Count} from the instance pass");
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write warnings.jsonl: {ex.Message}");
            }
        }

        /// <summary>
        /// Whether a <c>warnings.jsonl</c> line was written by a mesh rip rather than by the
        /// instance pass, and so is this rip's to replace.<br/>
        /// （某行 <c>warnings.jsonl</c> 是否由網格擷取而非實例階段寫出，因而應由本次擷取取代。）<br/><br/>
        /// Matched on the declared kind rather than by parsing: a line this system cannot read is
        /// someone else's and is kept.<br/>
        /// （以所宣告的類別比對而非解析全文：本系統無法讀取的行即屬他者，予以保留。）
        /// </summary>
        /// <param name="line">The line to classify.（待分類的行。）</param>
        /// <returns>Whether the mesh rip owns it.（網格擷取是否擁有該行。）</returns>
        private static bool IsOwnedByMeshRip(string line)
        {
            try
            {
                string kind = Newtonsoft.Json.Linq.JObject.Parse(line)["kind"]?.ToString();
                return (kind == "mesh") || (kind == "material") || (kind == "texture");
            }
            catch (Exception)
            {
                return false;
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

            // Materials last: the table can only name a texture by the content hash of the file it
            // became, so it is written once the encoder has drained.
            // （材質最後處理：材質表只能以貼圖檔案的內容雜湊指名該貼圖，故須待編碼端排空後方能寫出。）
            string materials = null;
            try
            {
                materials = Instance.MaterialRip.Finish(run.Report, run.SkippedAssets);
            }
            catch (Exception ex)
            {
                _log.Error($"Material rip failed to finish: {ex}");
            }

            WriteMeshWarnings(run);

            // Sizes are the point of a capped run: they are what says whether the uncapped rip is
            // 3 GB or 30. Report the total, the mean and the worst offenders.
            // （大小正是受限擷取的重點：唯有它能說明未受限的擷取是 3 GB 還是 30 GB。
            //   回報總計、平均值與最大者。）
            List<KeyValuePair<string, long>> sizes = new(run.Sizes);
            sizes.Sort((a, b) => b.Value.CompareTo(a.Value));

            run.Report.AppendLine();
            run.Report.AppendLine($"Wrote {run.Written} .glb, skipped {run.Skipped}, failed {run.Failed}, in {run.Clock.Elapsed.TotalSeconds:F1} s.");

            // The rules the game applies that a vanilla city may never exercise — counted so a rip
            // states how often each fired instead of leaving them unobservable.
            // （遊戲適用、但原版城市未必觸發的規則——予以計數，使擷取據實陳述其觸發次數。）
            if (MultiMeshPrefabs > 0) run.Report.AppendLine($"Merged multi-mesh geometry assets: {MultiMeshPrefabs} prefab(s).");
            if (PartialMeshPrefabs > 0) run.Report.AppendLine($"Prefabs that lost meshes their geometry asset holds: {PartialMeshPrefabs} — see the log.");
            if (PipelinePartsSkipped > 0) run.Report.AppendLine($"Skipped {PipelinePartsSkipped} underground-only sub-mesh entr(ies) inside otherwise visible prefabs (Pipeline layers).");
            if (PlaceholderParts > 0) run.Report.AppendLine($"Missing-asset placeholder parts stretched to prefab bounds: {PlaceholderParts}.");
            if (StackRestParts > 0) run.Report.AppendLine($"Stacked prefabs with untiled rest parts (__stack_rest): {StackRestParts}.");
            if ((run.SkirtsWritten > 0) || (run.SkirtsEmpty > 0))
                run.Report.AppendLine($"Foundation skirts (__base): {run.SkirtsWritten} file(s); "
                                    + $"{run.SkirtsEmpty} Base-flagged prefab(s) presented no silhouette at the base level.");
            run.Report.AppendLine($"Total {run.Bytes / (1024d * 1024d):F1} MiB, mean {(run.Written > 0 ? run.Bytes / (double)run.Written / 1024d : 0d):F0} KiB per prefab.");
            run.Report.AppendLine("Largest:");
            for (int i = 0; (i < 15) && (i < sizes.Count); i++)
                run.Report.AppendLine($"  {sizes[i].Value / 1024d:F0} KiB  {sizes[i].Key}");
            run.Report.AppendLine($"Output: {run.OutputDirectory}");

            _log.Info(run.Report.ToString());

            StringBuilder summary = new();
            summary.AppendLine($"Wrote {run.Written} mesh file(s), skipped {run.Skipped}, failed {run.Failed}.");
            summary.AppendLine($"{run.Bytes / (1024d * 1024d):F1} MiB in {run.Clock.Elapsed.TotalSeconds:F0} s.");
            if (!string.IsNullOrEmpty(materials)) summary.AppendLine(materials);
            summary.AppendLine();
            summary.Append("Full report: Logs/Carto.log");

            // A pipeline-driven run reports into the pipeline's combined summary; only a run the
            // user started by hand gets its own dialog.（管線驅動的擷取回報至管線的合併摘要；
            //   唯有使用者手動啟動的擷取才有自身的對話框。）
            if (run.Completed != null)
            {
                try { run.Completed(summary.ToString()); }
                catch (Exception ex) { _log.Warn($"Mesh rip completion callback failed: {ex}"); }
                return;
            }

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
        /// <param name="renderPrefab">The render prefab behind the entity.（實體背後的繪製預製模板。）</param>
        /// <param name="trace">Optional diagnostics sink.（可選的診斷紀錄。）</param>
        /// <returns>The mesh, or <c>null</c> to fall back to the ECS buffers.（網格，若為 <c>null</c> 則退回 ECS 緩衝區。）</returns>
        private MeshAsset TryReadGeometryAsset(string assetId, string sourceName, RenderPrefab renderPrefab, List<string> trace)
        {
            try
            {
                if (renderPrefab == null)
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

                    // Which count to trust: the asset's own file header, not the prefab's
                    // serialized `m_MeshCount`. They agree on vanilla content (`MeshSystem`
                    // warns whenever meshCount != materialCount and a vanilla load logs none),
                    // but the header is what `CreateMeshes` actually iterates, and after
                    // `ObtainMeshes` it is resident — so it is the ground truth at zero cost.
                    // （該信任哪個數量：資產自身的檔頭，而非預製模板序列化的 `m_MeshCount`。
                    //   兩者在原版內容上一致，但檔頭才是 `CreateMeshes` 實際走訪的數量，
                    //   且於 `ObtainMeshes` 之後已常駐——故其為零成本的權威事實。）
                    int meshCount = renderPrefab.meshCount;
                    try { if (asset.meshCount > 0) meshCount = asset.meshCount; }
                    catch (Exception) { /* header not resident; the prefab's declared count is the best available */ }

                    if (trace != null)
                        foreach (GeometryAssetReader.MeshInfo info in GeometryAssetReader.Describe(asset, meshCount))
                            trace.Add($"  {info}");

                    // `ObtainMeshes` loads the CPU-side data only when no instances exist yet.
                    // For a prefab the game is rendering right now — every drawn lane mesh, for
                    // one — `m_Instances` is already non-null, the call returns the live meshes
                    // without touching `m_Data`, and every native accessor then throws
                    // `ArgumentOutOfRangeException` off `data.meshCount == 0`. (Its
                    // `keepReadable` parameter is dead in this game build — the decompiled body
                    // never reads it.) That is not a failed read, it is a path choice: skip the
                    // native accessors entirely and read the live Unity meshes, which
                    // `CreateMeshes` built readable. A full-city rip warned 16 times over
                    // exactly this and every one of the 16 files was fine.
                    // （`ObtainMeshes` 僅在尚無實例時載入 CPU 端資料。對於遊戲此刻正在繪製的
                    //   預製模板——所有繪製中的車道網格皆然——`m_Instances` 已非空，呼叫直接
                    //   回傳現存網格而不觸及 `m_Data`，其後所有原生存取器皆因
                    //   `data.meshCount == 0` 拋出例外。（其 `keepReadable` 參數於本版遊戲為
                    //   死參數——反編譯後的方法主體從未讀取它。）這並非讀取失敗而是路徑選擇：
                    //   全然跳過原生存取器，改讀 `CreateMeshes` 建立為可讀的現存 Unity 網格。
                    //   全城擷取正是為此警告了 16 次，而 16 個檔案無一有缺。）
                    bool dataResident = asset.data.IsValid;
                    if (!dataResident)
                    {
                        trace?.Add("geometry asset: CPU data not resident (the game holds live instances); reading the Unity meshes");
                        _log.Debug($"`{assetId}`: geometry data not resident (live instances held by the game); reading Unity meshes instead.");
                    }

                    // Every mesh of the asset, not the richest one. The meshes are simultaneous
                    // parts — LODs live behind LodProperties as separate render prefabs — and the
                    // game binds surface i to the i-th (mesh, sub-mesh) pair in flat order
                    // (`RenderPrefab.ObtainMesh`), which is exactly the ordinal the reader assigns.
                    // Picking one mesh dropped its siblings and, off the first slot, mis-assigned
                    // their surfaces.
                    // （讀取資產的每個網格而非最豐富者。這些網格是同時繪製的部件——LOD 以獨立繪製
                    //   預製模板存在於 LodProperties 之後——且遊戲將表面 i 繫結至平坦順序下第 i 個
                    //   （網格、子網格）配對（`RenderPrefab.ObtainMesh`），讀取器指派的序數正是它。
                    //   只挑一個網格會丟棄其兄弟部件，且於首槽位之外錯配其表面。）
                    int meshesMerged = 0;
                    MeshAsset merged = dataResident
                        ? GeometryAssetReader.ReadAll(asset, meshCount, assetId, sourceName, out meshesMerged)
                        : null;
                    if (merged != null)
                    {
                        if (meshesMerged > 1) MultiMeshPrefabs++;
                        if (meshesMerged < meshCount)
                        {
                            // The meshes of one asset draw together, so coming up short is lost
                            // geometry, not a variant choice — counted, named in the log, and
                            // declared in warnings.jsonl: the .glb exists but is incomplete,
                            // which a consumer cannot see from the file alone (contract §10).
                            // （同一資產的網格是共同繪製的，故短少即為丟失的幾何而非變體選擇——
                            //   予以計數、具名記錄，並於 warnings.jsonl 宣告：.glb 存在但不完整，
                            //   消費端無法僅憑檔案察覺。）
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: geometry asset holds {meshCount} mesh(es) but only {meshesMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {meshesMerged} of {meshCount} geometry-asset meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: merged {meshesMerged} of {meshCount} mesh(es) ({merged.TriangleCount} tris, {merged.Primitives.Count} primitives)");
                        return merged;
                    }

                    // Native accessors still empty: fall back to the loaded Unity meshes, merged
                    // under the same flat ordinal.
                    // （原生存取器仍為空：退回使用已載入的 Unity 網格，並以同一平坦序數合併。）
                    MeshAsset viaUnity = GeometryAssetReader.ReadUnityMeshes(loaded, assetId, sourceName, out int unityMerged);
                    if (viaUnity != null)
                    {
                        if (unityMerged > 1) MultiMeshPrefabs++;
                        if ((loaded != null) && (unityMerged < loaded.Length))
                        {
                            PartialMeshPrefabs++;
                            _log.Warn($"`{assetId}`: {loaded.Length} loaded Unity mesh(es) but only {unityMerged} could be read; the rest are missing from the bundle.");
                            (_run?.SkippedAssets ?? WarningSink)?.Add(new RipWarning("mesh", assetId,
                                $"partial: {unityMerged} of {loaded.Length} Unity meshes read; the .glb is incomplete"));
                        }
                        trace?.Add($"geometry asset: via {unityMerged} Unity mesh(es) ({viaUnity.TriangleCount} tris, {viaUnity.Primitives.Count} primitives)");
                        return viaUnity;
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
            // The render prefab is both where the authored geometry lives and where the material
            // slots live, so resolve it once and let both readers share it.
            // （繪製預製模板既是製作完成的幾何所在，也是材質槽位所在，故解析一次供兩端共用。）
            RenderPrefab renderPrefab = null;
            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase))
                renderPrefab = prefabBase as RenderPrefab;

            // Prefer the geometry asset: the ECS buffers only ever hold the reduced LOD the game
            // keeps CPU-side for raycasting, and carry no sub-mesh ranges at all.
            // （優先使用幾何資產：ECS 緩衝區僅存放遊戲為射線保留於 CPU 端的簡化 LOD，且不含子網格範圍。）
            MeshAsset fromAsset = TryReadGeometryAsset(assetId, sourceName, renderPrefab, trace);
            if (fromAsset != null)
            {
                AssignMaterials(fromAsset, renderPrefab);
                TryFlattenDecal(fromAsset, renderPrefab, trace);
                return fromAsset;
            }

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
                // The material slot is the buffer *position*, never the stored `m_MaterialIndex`.
                // Nothing in the game ever writes authored ranges into this buffer; its one writer
                // is `MeshSystem.OnUpdate`, which runs at prefab creation, resizes the buffer to
                // one entry per surface asset *in order*, stores a session-global library id in
                // `m_MaterialIndex`, and leaves the range fields uninitialised. The game itself
                // resolves a surface from this buffer by position (`ManagedBatchSystem.CreateBatch`
                // matches the global id and then calls `GetSurfaceAsset(position)`), so position
                // is the only meaning the slot has — and the global id, used positionally, would
                // pick an arbitrary surface. The garbage ranges are dropped by the range check in
                // `GltfWriter.NormalizePrimitives` / `NetworkRipSystem.AppendPieceIndices`.
                // （材質槽位是緩衝區的<b>位置</b>，絕非其中儲存的 `m_MaterialIndex`。遊戲中無人寫入
                //   製作時的範圍：此緩衝區唯一的寫入者是預製模板建立時執行的 `MeshSystem.OnUpdate`，
                //   它依序為每個表面資產各存一筆、於 `m_MaterialIndex` 存入全域材質庫 ID，
                //   並使範圍欄位維持未初始化。遊戲自身亦以位置解析表面，
                //   故位置是槽位僅有的意義；全域 ID 若按位置使用將選中任意表面。
                //   無效的範圍由下游的範圍檢查丟棄。）
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
                        MaterialIndex = i,
                        MaterialId = $"{assetId}_mat{i}"
                    });
                }
            }

            if (EntityManager.HasComponent<MeshData>(meshEntity))
            {
                MeshData data = EntityManager.GetComponentData<MeshData>(meshEntity);
                mesh.BoundsMin = data.m_Bounds.min;
                mesh.BoundsMax = data.m_Bounds.max;
            }

            AssignMaterials(mesh, renderPrefab);
            TryFlattenDecal(mesh, renderPrefab, trace);
            return mesh;
        }

        /// <summary>
        /// Replace a decal's projector volume with the quad it actually paints.
        /// （以貼花實際繪出的四邊形取代其投影體積。）<br/><br/>
        /// <b>Why the volume alone is useless.</b> A decal's mesh is not its artwork — it is the
        /// box the game projects artwork down through, and every variant shares one box. Measured
        /// in a real city: <c>NA_RoadArrow_Forward</c>, <c>NA_RoadArrow_Forward_Left90</c> and
        /// <c>NA_RoadArrow_Left180</c> are byte-identical 3 × 1 × 6 m boxes with byte-identical
        /// UVs and one shared material, and their mesh UVs are a default box unwrap — six faces
        /// across six unrelated atlas rects, one of them with a zero normal. Nothing in the mesh
        /// says which arrow it is.<br/>
        /// （<b>單有體積為何無用。</b>貼花的網格並非其圖樣，而是遊戲藉以向下投影圖樣的方盒，
        /// 且所有變體共用同一個方盒。實測於真實城市：三種不同的路面箭頭其方盒、UV 與材質
        /// 皆逐位元組相同，而其網格 UV 僅是預設的方盒展開——六個面散落於六塊無關的圖集矩形，
        /// 其中一面的法線甚至為零。網格中沒有任何資訊指出它是哪一種箭頭。）<br/><br/>
        /// <b>What does say.</b> <c>DecalProperties.m_TextureArea</c> — a <c>Bounds2</c> the game
        /// hands the shader as <c>_TextureArea</c> alongside <c>_MeshSize</c> and
        /// <c>_DecalLayerMask</c> (see <c>ManagedBatchSystem.CreateBatch</c> and
        /// <c>RenderPrefabRenderer.Instance.SetDecalProperties</c>). It is the sub-rect of the
        /// atlas this prefab draws, and the arrow sheet is a 8 × 4 grid of 0.125 × 0.25 cells whose
        /// aspect matches the 3 × 6 m footprint exactly. Baking that rect across the footprint
        /// turns a shared box into the marking it stands for, and shrinks the mesh from 24
        /// vertices to 4.<br/>
        /// （<b>真正指出的是什麼。</b><c>DecalProperties.m_TextureArea</c> 即該預製模板所繪的
        /// 圖集子矩形。箭頭圖集為 8 × 4 的網格，每格 0.125 × 0.25，其長寬比恰與 3 × 6 公尺的
        /// 投影足跡相符。將該矩形烘焙至足跡上，即可將共用的方盒化為其所代表的標線，
        /// 並使網格由 24 個頂點縮減為 4 個。）<br/><br/>
        /// The quad sits at the volume's mid-height, not its floor: a projector is centred on the
        /// surface it paints so that it catches ground a little above and below, so the midpoint is
        /// the best estimate of the carriageway and the floor would bury the marking half a metre
        /// under it.<br/>
        /// （四邊形置於體積的半高處而非底面：投影體以其所繪表面為中心，方能涵蓋略高與略低的
        /// 地面，故中點是車道位置的最佳估計，若取底面則會使標線埋入路面下半公尺。）
        /// </summary>
        /// <param name="mesh">The mesh to rewrite in place.（待就地改寫的網格。）</param>
        /// <param name="renderPrefab">The render prefab that may carry decal properties.（可能帶有貼花屬性的繪製預製模板。）</param>
        /// <param name="trace">Optional sink recording the decision.（可選的決策紀錄。）</param>
        /// <returns>Whether the mesh was replaced.（網格是否已被取代。）</returns>
        /// <summary>
        /// The import pipeline's own up-facing vertex test (`AssetImportPipeline.
        /// SetupDecalComponent`): a real normal (`SqrMagnitude > 0.01`) within 0.01° of straight
        /// up. cos(0.01°) ≈ 0.99999998.
        /// （匯入管線自身的朝上頂點測試：真實法線且與正上方夾角小於 0.01°。）
        /// </summary>
        private static bool IsExactlyUp(float3 normal)
        {
            float lengthSq = math.lengthsq(normal);
            if (lengthSq <= 0.01f) return false;
            return (normal.y / math.sqrt(lengthSq)) >= 0.99999998f;
        }

        private bool TryFlattenDecal(MeshAsset mesh, RenderPrefab renderPrefab, List<string> trace)
        {
            if ((mesh == null) || (renderPrefab == null)) return false;
            if ((mesh.Vertices == null) || (mesh.Vertices.Length == 0)) return false;

            DecalProperties decal;
            try
            {
                if (!renderPrefab.TryGet(out decal) || (decal == null) || !decal.active) return false;
            }
            catch (Exception ex)
            {
                trace?.Add($"decal: properties did not resolve — {ex.GetType().Name} {ex.Message}");
                return false;
            }

            float3 min = mesh.Vertices[0], max = mesh.Vertices[0];
            for (int i = 1; i < mesh.Vertices.Length; i++)
            {
                min = math.min(min, mesh.Vertices[i]);
                max = math.max(max, mesh.Vertices[i]);
            }

            // A volume with no footprint projects nothing; leaving it alone keeps a degenerate
            // prefab visible as itself rather than as a zero-area quad.
            // （沒有足跡的體積不會投影出任何東西；保留原狀可使退化的預製模板維持原貌，
            //   而非化為零面積的四邊形。）
            if (!((max.x - min.x) > 1e-4f) || !((max.z - min.z) > 1e-4f)) return false;

            float y = (min.y + max.y) * 0.5f;

            float3[] corners =
            {
                new float3(min.x, y, min.z), new float3(max.x, y, min.z),
                new float3(max.x, y, max.z), new float3(min.x, y, max.z)
            };

            // The UV PAIRING is taken from the authored mesh, never rebuilt from `m_TextureArea`
            // alone: reconstruction from the bounds has to guess which way `u` and `v` run
            // across the quad. An earlier version guessed `+x → +u` and pre-flipped `v`; the
            // guess was mirrored, and every road-marking text in the city read backwards
            // ("TAXI" as "IXAT", a backwards S in "BUS"), while the pre-flip cancelled the
            // writer's Unity→glTF flip and cropped some markings to the wrong band of their
            // atlas outright. Sampling each corner's nearest up-facing authored vertex keeps
            // orientation exactly as the game renders it, and the writer's usual flip applies
            // as it does to every other mesh. (The RECT is a different matter — see below.)
            // （UV「配對」取自製作原始網格，絕不僅憑 `m_TextureArea` 重建：僅憑邊界重建必然要
            //   猜測 `u`、`v` 於四邊形上的走向。舊版猜測 `+x → +u` 並預先翻轉 `v`：該猜測為鏡像，
            //   全城的路面文字因而反讀（「TAXI」成「IXAT」、「BUS」的 S 反向），預翻轉又抵銷了
            //   寫出端的 Unity→glTF 翻轉，使部分標線裁切至圖集的錯誤色帶。對每個角取最近的
            //   朝上製作頂點，可使方向與遊戲的繪製完全一致，寫出端的例行翻轉亦照常適用。
            //   （「矩形」則另當別論——見下方。））
            // The same up-facing selection the game's derivation uses; a projector volume whose
            // top face carries no upward normal (none measured, but authored content varies)
            // falls back to every vertex rather than shipping an unmapped quad.
            // （與遊戲推導所用相同的朝上頂點選擇；若投影體頂面無朝上法線（實測未見，
            //   惟製作內容各異），則退回全部頂點，而非輸出未映射的四邊形。）
            // Pass 0 is the import pipeline's own vertex test (`SetupDecalComponent`:
            // `SqrMagnitude > 0.01` and `Angle(up, n) < 0.01°` — exactly-up, not merely upward),
            // so that for DefaultDecal objects the rect below reproduces `m_TextureArea` by
            // construction and the rescale is a true identity. Pass 1 keeps the previously
            // validated ≥ 0.9 tolerance for authored content the strict test misses; pass 2
            // takes everything rather than shipping an unmapped quad.
            // （第 0 遍即匯入管線自身的頂點測試——「恰好朝上」而非僅「向上」——使 DefaultDecal
            //   物件的矩形依構造重現 `m_TextureArea`，縮放成為真正的恆等變換。第 1 遍保留既驗證
            //   的 ≥ 0.9 容差；第 2 遍全取，而非輸出未映射的四邊形。）
            float2[] cornerUvs = new float2[4];
            for (int c = 0; c < 4; c++)
            {
                float best = float.MaxValue;
                for (int pass = 0; (pass < 3) && (best == float.MaxValue); pass++)
                {
                    for (int i = 0; i < mesh.Vertices.Length; i++)
                    {
                        if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                            && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;

                        float2 offset = mesh.Vertices[i].xz - corners[c].xz;
                        float distance = math.lengthsq(offset);
                        if (distance < best)
                        {
                            best = distance;
                            cornerUvs[c] = ((mesh.Uvs != null) && (i < mesh.Uvs.Length)) ? mesh.Uvs[i] : default;
                        }
                    }
                }
            }

            // The authored pairing gives the ORIENTATION; `m_TextureArea` gives the RECT. The
            // shader never samples the mesh UVs of a decal — it is handed `_TextureArea`
            // (ManagedBatchSystem.CreateBatch) and maps the projector footprint into that rect.
            // For DefaultDecal imports the pipeline derives the area from these same UVs, so the
            // rescale below is the identity and the road-marking text keeps rendering exactly as
            // validated. But the LANE decal parts — centre lines, crosswalks, lane lines — ship
            // authored box UVs that all crop one generic, mostly-transparent slab of the LaneLine
            // atlas ([0.625, 0.25]–[0.875, 0.5], measured: mean α 27/255), while their prefabs'
            // `m_TextureArea` selects the actual opaque marking band. Keeping the authored rect
            // is why every crosswalk and centre line in the first full import was invisible.
            // Rescaling the authored rect into the area keeps the pairing (the mirrored-text
            // lesson above) and takes the crop from where the game actually takes it.
            // （製作配對給出「方向」；`m_TextureArea` 給出「矩形」。著色器從不取樣貼花的網格 UV——
            //   它拿到的是 `_TextureArea`，並將投影足跡映射入該矩形。DefaultDecal 匯入的面積
            //   正是由這些 UV 推導，故下方的縮放為恆等變換，路面文字維持既驗證的樣貌。
            //   但車道貼花部件——中線、行穿線、車道線——的製作方盒 UV 全都裁向 LaneLine 圖集中
            //   同一塊近乎透明的泛用區域，其模板的 `m_TextureArea` 才選中實際不透明的標線色帶。
            //   保留製作矩形正是首次全城匯入中所有行穿線與中線隱形的原因。將製作矩形縮放入
            //   面積矩形，既保留配對（前述鏡像教訓），又自遊戲實際取用之處取得裁切。）
            float2 rectMin = new(float.MaxValue), rectMax = new(float.MinValue);
            for (int pass = 0; (pass < 3) && (rectMin.x == float.MaxValue); pass++)
            {
                for (int i = 0; i < mesh.Vertices.Length; i++)
                {
                    if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                        && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;
                    if ((mesh.Uvs == null) || (i >= mesh.Uvs.Length)) continue;

                    rectMin = math.min(rectMin, mesh.Uvs[i]);
                    rectMax = math.max(rectMax, mesh.Uvs[i]);
                }
            }

            float2 rectSize = rectMax - rectMin;
            float2 areaMin = decal.m_TextureArea.min;
            float2 areaSize = decal.m_TextureArea.max - decal.m_TextureArea.min;
            bool remapped = false;
            if (math.all(rectSize > 1e-6f) && math.all(areaSize > 0f)
                && (math.any(math.abs(rectMin - areaMin) > 1e-4f)
                    || math.any(math.abs(rectSize - areaSize) > 1e-4f)))
            {
                for (int c = 0; c < 4; c++)
                {
                    cornerUvs[c] = areaMin + (((cornerUvs[c] - rectMin) / rectSize) * areaSize);
                }
                remapped = true;
            }

            mesh.Vertices = corners;
            mesh.Uvs = cornerUvs;

            mesh.Normals = new[] { math.up(), math.up(), math.up(), math.up() };
            mesh.Tangents = null;

            // Same rule the network layer's generated strips use: take the winding the quad
            // actually produces rather than assume one, and reverse it when it faces the ground.
            // （與路網層產生的帶狀面採用同一準則：依四邊形實際產生的繞向決定，而非預設某一方向，
            //   若其朝向地面則反轉。）
            bool flip = math.cross(mesh.Vertices[1] - mesh.Vertices[0],
                                   mesh.Vertices[2] - mesh.Vertices[0]).y < 0f;

            mesh.Indices = flip ? new[] { 0, 2, 1, 0, 3, 2 } : new[] { 0, 1, 2, 0, 2, 3 };

            string materialId = ((mesh.Primitives.Count > 0) && !string.IsNullOrEmpty(mesh.Primitives[0].MaterialId))
                ? mesh.Primitives[0].MaterialId : $"{mesh.AssetId}_mat0";

            mesh.Primitives.Clear();
            mesh.Primitives.Add(new MeshPrimitive
            {
                StartIndex = 0,
                IndexCount = 6,
                StartVertex = 0,
                VertexCount = 4,
                MaterialIndex = 0,
                MaterialId = materialId
            });

            mesh.BoundsMin = new float3(min.x, y, min.z);
            mesh.BoundsMax = new float3(max.x, y, max.z);

            trace?.Add($"decal: flattened to a {max.x - min.x:F2} × {max.z - min.z:F2} m quad at y {y:F2}, "
                     + $"corner uvs [{cornerUvs[0].x:F4}, {cornerUvs[0].y:F4}]–[{cornerUvs[2].x:F4}, {cornerUvs[2].y:F4}]"
                     + (remapped ? $" (authored rect rescaled into textureArea [{areaMin.x:F4}, {areaMin.y:F4}]+"
                                 + $"[{areaSize.x:F4}, {areaSize.y:F4}])" : ""));
            return true;
        }

        /// <summary>
        /// Give every primitive of a freshly read mesh a material id that resolves.
        /// （為剛讀取完成的網格中每個 primitive 指派可解析的材質 ID。）<br/><br/>
        /// The real id comes from the render prefab's surface assets, which are ripped — textures
        /// and all — the first time each is seen. Where a surface does not resolve, the synthetic
        /// <c>&lt;assetId&gt;_mat&lt;n&gt;</c> id stays and is <i>declared</i> in the material table
        /// as an entry with no maps. Neither dropping the material nor leaving the name dangling is
        /// acceptable: §6 of the contract requires a glTF material's name to be a key in
        /// <c>materials.json</c>, so an undeclared placeholder is exactly the unresolvable
        /// reference the validator is there to catch.<br/>
        /// （真實 ID 來自繪製預製模板的表面資產，並於首次見到各表面時連同貼圖一併擷取。
        /// 若表面無法解析，則保留合成的 <c>&lt;assetId&gt;_mat&lt;n&gt;</c> ID，
        /// 並於材質表中<i>宣告</i>為一筆不含任何貼圖的條目。丟棄材質或放任名稱懸空皆不可接受：
        /// 契約 §6 要求 glTF 材質名稱必須是 <c>materials.json</c> 的鍵，
        /// 未經宣告的佔位正是驗證器意在攔截的無法解析參照。）
        /// </summary>
        /// <param name="mesh">The mesh whose primitives to relabel.（要重新標記其 primitive 的網格。）</param>
        /// <param name="renderPrefab">The render prefab holding the surface assets.（持有表面資產的繪製預製模板。）</param>
        private void AssignMaterials(MeshAsset mesh, RenderPrefab renderPrefab)
        {
            if (mesh == null) return;

            // A mesh the game reported no drawable ranges for still draws with slot 0, and glTF
            // still needs one primitive covering the whole index buffer. Making that explicit here
            // rather than leaving it to the writer is what lets its material be declared at all —
            // the writer runs on a background task, long after the material table is reachable.
            // （遊戲未回報可繪製範圍的網格仍以槽位 0 繪製，而 glTF 仍需一個涵蓋整個索引緩衝區的
            //   primitive。於此明確建立而非留給寫出端，正是其材質得以被宣告的原因：
            //   寫出端於背景工作中執行，時點遠晚於材質表可及之時。）
            if ((mesh.Primitives.Count == 0) && (mesh.Indices != null) && (mesh.Indices.Length >= 3))
            {
                mesh.Primitives.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = mesh.Indices.Length - (mesh.Indices.Length % 3),
                    StartVertex = 0,
                    VertexCount = mesh.Vertices?.Length ?? 0,
                    MaterialIndex = 0,
                    MaterialId = $"{mesh.AssetId}_mat0"
                });
            }

            if (!Instance.MaterialRip.IsRipping) return;

            foreach (MeshPrimitive primitive in mesh.Primitives)
            {
                string materialId = Instance.MaterialRip.Resolve(renderPrefab, primitive.MaterialIndex);

                if (!string.IsNullOrEmpty(materialId)) primitive.MaterialId = materialId;
                else Instance.MaterialRip.Declare(primitive.MaterialId,
                    (renderPrefab == null) ? "no render prefab behind the mesh entity"
                                           : $"surface asset {primitive.MaterialIndex} did not resolve");
            }
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
            // Capped at 160 rather than the contract's 180 so a derived id — `__stack_first` and
            // friends add up to 13 characters — still fits the schema's assetId pattern.
            // （截斷會以相同方式造成碰撞，只是更為罕見：兩個共用前綴的長名稱。
            //   上限取 160 而非契約的 180，使衍生 ID（`__stack_first` 等最多加 13 字元）
            //   仍符合綱要的 assetId 樣式。）
            return (result.Length > 160) ? $"{result.Substring(0, 151)}_{Digest(name)}" : result;
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
        /// Whether an object is the node furniture of a buried utility network, and so is not part
        /// of the built environment the bundle describes.
        /// （物件是否為地下公用設施網路的節點配件，因而不屬於束所描述的建成環境。）<br/><br/>
        /// Every road lays hidden water, sewage and electricity along itself, as <i>marker</i>
        /// networks the player never places or sees, and each of their nodes hangs a prop off
        /// itself: <c>Water_Pipe_Node_*</c>, <c>Sewage_Pipe_Node_*</c>, <c>Ground_Cable_Node_*</c>.
        /// A full city has tens of thousands — the counts give it away, ~7,300 water pipe nodes
        /// against the 105 pipe segments a player actually built. The props themselves are ordinary
        /// objects, not <see cref="Marker"/>s, so the query's marker exclusion never reaches
        /// them.<br/>
        /// （每條道路都沿線鋪設隱藏的自來水、污水與電力，作為玩家從不擺放也看不見的<i>標記</i>網路，
        /// 且其每個節點都掛著一個道具。整座城市有數萬個——數量本身即是證據：約 7,300 個自來水管節點，
        /// 而玩家實際建造的管線僅 105 段。這些道具本身是一般物件而非 <see cref="Marker"/>，
        /// 故查詢的標記排除永遠無法涵蓋它們。）<br/><br/>
        /// <b>The owner must be a <c>Game.Net.Marker</c>.</b> A real high-voltage line's nodes are
        /// not markers, and its pylons are node props exactly like these, so testing the network
        /// type alone would delete every pylon in the city.<br/>
        /// （<b>所有者必須是 <c>Game.Net.Marker</c>。</b>真實高壓線的節點並非標記，
        /// 而其塔架正是與此完全相同的節點道具，故僅測試網路型別將刪光全城的塔架。）<br/><br/>
        /// <b>Buildings hang the same furniture with themselves as owner</b>, so the owner test
        /// alone is not the whole story: every lot's water, sewage and cable connection nodes have
        /// the building as <c>Owner</c> and no marker net anywhere in the chain. Those are caught
        /// by <see cref="IsUndergroundOnlyPrefab"/> — the game's own draw-layer rule — which is
        /// checked first.<br/>
        /// （<b>建築以自身為所有者掛載相同的配件</b>，故所有者測試並非全貌：每個地塊的自來水、
        /// 污水與電纜接點皆以建築為 <c>Owner</c>，鏈上無任何標記網路。這些由
        /// <see cref="IsUndergroundOnlyPrefab"/>——遊戲自身的繪製圖層規則——優先攔下。）<br/><br/>
        /// <b>For the owner test itself, one exception.</b> An earlier version also required
        /// the owner's prefab to carry <c>PipelineData</c> or <c>PowerLineData</c>, which is what
        /// let the buried nodes through: those two components come only from
        /// <c>PipelinePrefab</c> and <c>PowerLinePrefab</c>
        /// (<c>GetPrefabComponents</c>), and the marker nets a road lays along itself are not
        /// those classes. The extra clause could only ever narrow a test that was already exact —
        /// <c>Game.Net.Marker</c> is added by <c>MarkerNet.GetArchetypeComponents</c> and by
        /// nothing else in the game, to the node archetype and the edge archetype alike, so an
        /// owner carrying it <i>is</i> a marker net by construction.<br/>
        /// （<b>而這便是全部的判準，僅有一項例外。</b>先前的版本另要求所有者的預製模板帶有
        /// <c>PipelineData</c> 或 <c>PowerLineData</c>，這正是埋設節點得以漏網之處：
        /// 該二元件僅來自 <c>PipelinePrefab</c> 與 <c>PowerLinePrefab</c>，
        /// 而道路沿線鋪設的標記網路並非這些類別。此附加條件只能使一個本已精確的判準變窄——
        /// <c>Game.Net.Marker</c> 由 <c>MarkerNet.GetArchetypeComponents</c> 加入，
        /// 且遊戲中別無他處，節點與邊的原型皆然，故帶有它的所有者依其構成即為標記網路。）<br/><br/>
        /// <b>The exception is taxiways</b>, which are marker nets and yet are genuinely drawn —
        /// <see cref="NetworkRipSystem"/> already makes the same exception. They are named directly
        /// by <c>TaxiwayData</c>, which <c>TaxiwayPrefab.GetPrefabComponents</c> is the only source
        /// of, rather than indirectly by listing the network types that are not taxiways.<br/>
        /// （<b>例外是滑行道</b>：其為標記網路卻確實會被繪製，<see cref="NetworkRipSystem"/>
        /// 已設有相同例外。此處以 <c>TaxiwayData</c> 直接指名之（其唯一來源為
        /// <c>TaxiwayPrefab.GetPrefabComponents</c>），而非反過來列舉所有並非滑行道的網路型別。）<br/><br/>
        /// <b>Both rip systems must apply this identically.</b> The asset ids are assigned over the
        /// prefab set as a whole, so a set that differs between the two layers can shift a
        /// disambiguating suffix and break the join — see <see cref="BuildAssetIds"/>.<br/>
        /// （<b>兩個擷取系統必須以完全相同的方式套用此判斷。</b>資產 ID 就整個預製模板集合指派，
        /// 故兩層所見集合若有差異，可能改變消歧義後綴而破壞連結，詳見 <see cref="BuildAssetIds"/>。）
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.（用以查詢的實體管理器。）</param>
        /// <param name="entity">The placed object.（已放置的物件。）</param>
        /// <returns>Whether to leave it out of the bundle.（是否將其排除於束之外。）</returns>
        public static bool IsUtilityMarkerSubObject(EntityManager entityManager, Entity entity)
        {
            // The owner test alone proved incomplete in the field: a full-city rip still carried
            // 29,648 pipe and cable node props whose owner is the *building* whose lot spawns
            // them, not a marker net — buildings hang their own utility connection furniture as
            // sub-objects, and those owners carry no `Game.Net.Marker`. The prefab test below is
            // the game's own draw rule and catches both parents.
            // （僅測試所有者已於實地證明不完整：全城擷取仍帶著 29,648 個管線與電纜節點道具，
            //   其所有者是生成它們的<i>建築</i>而非標記網路——建築以子物件掛載自己的公用設施
            //   接點配件，而這些所有者不帶 `Game.Net.Marker`。下方的預製模板測試是遊戲自身的
            //   繪製規則，兩種親代皆能涵蓋。）
            if (IsUndergroundOnlyPrefab(entityManager, entity)) return true;

            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if ((owner == Entity.Null) || !entityManager.HasComponent<Game.Net.Marker>(owner)) return false;
            if (!entityManager.HasComponent<PrefabRef>(owner)) return false;

            Entity ownerPrefab = entityManager.GetComponentData<PrefabRef>(owner).m_Prefab;
            if (ownerPrefab == Entity.Null) return false;

            // Drawn, despite being a marker net. The only such case the game has.
            // （雖為標記網路卻確實會被繪製。此為遊戲中唯一的此類情形。）
            return !entityManager.HasComponent<TaxiwayData>(ownerPrefab);
        }

        /// <summary>
        /// Whether an object is cargo riding a vehicle — a log pile on a truck trailer, crates on
        /// a flatbed — rather than scenery standing in the world.
        /// （物件是否為載於車輛上的貨物——貨車拖車上的原木堆、平板車上的木箱——而非立於世界中的場景物。）<br/><br/>
        /// The queries already exclude vehicles and creatures themselves, but cargo props are
        /// separate <i>sub-object</i> entities owned by the vehicle, and nothing about the prop
        /// itself says "agent": it is an ordinary object with a transform and a prefab. The tell is
        /// its <c>Owner</c> — the same one-level link the utility-marker test reads. A full-city rip
        /// carried ~590 such records (<c>TruckTrailer03_LogPile01</c>, <c>CarTrailer01_Props01</c>, …)
        /// as world scenery before this test existed.
        /// （查詢本已排除車輛與生物本身，但貨物道具是由車輛所擁有的獨立<i>子物件</i>實體，
        /// 道具自身毫無「行為者」的跡象：它只是帶有變換與預製模板的普通物件。判準在其
        /// <c>Owner</c>——與公用設施標記判斷所讀的同一層連結。此判斷存在之前，
        /// 全城擷取曾將約 590 筆此類記錄當作世界場景寫出。）<br/><br/>
        /// Every vehicle archetype carries <c>Game.Vehicles.Vehicle</c>
        /// (<c>VehiclePrefab.GetArchetypeComponents</c> adds it unconditionally, and
        /// <c>CarTrailerPrefab</c> derives from it through <c>CarBasePrefab</c>), so the single
        /// component test covers trailers too; <c>CarTrailer</c> is tested as well only so the rule
        /// reads as what it means and survives an archetype reshuffle.
        /// （每個車輛原型皆帶 <c>Game.Vehicles.Vehicle</c>——<c>VehiclePrefab.GetArchetypeComponents</c>
        /// 無條件加入，<c>CarTrailerPrefab</c> 經 <c>CarBasePrefab</c> 繼承之——故單一元件測試已涵蓋拖車；
        /// 另測 <c>CarTrailer</c> 僅為使規則如實可讀，並在原型調整後依然成立。）<br/><br/>
        /// <b>Both rip systems must apply this identically</b>, for the same asset-id join reason as
        /// <see cref="IsUtilityMarkerSubObject"/>.
        /// （<b>兩個擷取系統必須以完全相同的方式套用此判斷</b>，理由與
        /// <see cref="IsUtilityMarkerSubObject"/> 的資產 ID 連結相同。）
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.（用以查詢的實體管理器。）</param>
        /// <param name="entity">The placed object.（已放置的物件。）</param>
        /// <returns>Whether to leave it out of the bundle.（是否將其排除於束之外。）</returns>
        public static bool IsVehicleCargoSubObject(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<Game.Common.Owner>(entity)) return false;

            Entity owner = entityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
            if (owner == Entity.Null) return false;

            return entityManager.HasComponent<Game.Vehicles.Vehicle>(owner)
                || entityManager.HasComponent<Game.Vehicles.CarTrailer>(owner);
        }

        /// <summary>
        /// Whether an object's prefab draws only on the underground utility layers, and so is never
        /// visible in normal rendering.
        /// （物件的預製模板是否僅於地下公用設施圖層繪製，因而在一般繪製中永不可見。）<br/><br/>
        /// The game's rule, from <c>BatchInstanceSystem.UpdateObjectInstances</c>: a sub-mesh whose
        /// <c>MeshData.m_DefaultLayers</c> is non-zero has its batch layer replaced by those
        /// defaults, and <c>Pipeline</c>/<c>SubPipeline</c> map to camera layers only the
        /// underground cutaway view renders. A prefab all of whose sub-meshes live there — the
        /// pipe and cable connection nodes — therefore draws nothing in the world view, exactly
        /// like the Pipeline-layer lane parts the lane catalogue already excludes. A prefab with
        /// even one ordinary sub-mesh keeps drawing and is kept: the test is every-part, not
        /// any-part.<br/>
        /// （遊戲的規則，出自 <c>BatchInstanceSystem.UpdateObjectInstances</c>：
        /// <c>MeshData.m_DefaultLayers</c> 非零的子網格，其批次圖層會被該預設值取代，而
        /// <c>Pipeline</c>／<c>SubPipeline</c> 對應的攝影機圖層僅於地下剖視圖繪製。
        /// 所有子網格皆屬於該處的預製模板——管線與電纜接點——因而在世界視圖中一無所繪，
        /// 與車道目錄已排除的 Pipeline 圖層部件如出一轍。只要有一個一般子網格，模板便仍會繪製，
        /// 故此測試為「每個部件」而非「任一部件」。）
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.（用以查詢的實體管理器。）</param>
        /// <param name="entity">The placed object.（已放置的物件。）</param>
        /// <returns>Whether every sub-mesh is underground-only.（是否所有子網格皆僅屬地下圖層。）</returns>
        private static bool IsUndergroundOnlyPrefab(EntityManager entityManager, Entity entity)
        {
            if (!entityManager.HasComponent<PrefabRef>(entity)) return false;

            Entity prefab = entityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            if ((prefab == Entity.Null) || !entityManager.HasBuffer<SubMesh>(prefab)) return false;

            DynamicBuffer<SubMesh> subMeshes = entityManager.GetBuffer<SubMesh>(prefab, true);
            if (subMeshes.Length == 0) return false;

            const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
            foreach (SubMesh subMesh in subMeshes)
            {
                if (!entityManager.HasComponent<MeshData>(subMesh.m_SubMesh)) return false;

                MeshLayer layers = entityManager.GetComponentData<MeshData>(subMesh.m_SubMesh).m_DefaultLayers;
                if ((layers == 0) || ((layers & ~underground) != 0)) return false;
            }

            return true;
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
