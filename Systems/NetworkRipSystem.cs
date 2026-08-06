using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that writes every road, rail, path and utility run into <c>networks.jsonl</c>
    /// (PLAN Phase 5).<br/>
    /// （將所有道路、軌道、路徑與管線寫出至 <c>networks.jsonl</c> 的系統（PLAN 第五階段）。）<br/><br/>
    /// <b>Two geometry forms, and where each comes from.</b> CS2 stores no per-segment road mesh.
    /// A composition names a cross-section — a <c>NetCompositionPiece</c> buffer of carriageway,
    /// shoulder, kerb and side pieces, each an ordinary <c>NetPiecePrefab</c> (which derives from
    /// <c>RenderPrefab</c>, so its geometry reads through the same route as a building's). The
    /// game tiles those pieces along the edge and bends them onto the road surface in the
    /// <i>vertex shader</i>, from the eight <c>CompositionMatrix</c> values
    /// <c>BatchDataHelpers.CalculateEdgeParameters</c> writes per instance — and
    /// <c>BuildEdgeMatrix</c> shows each of those is nothing but a bezier's four control points.
    /// For an edge they are precisely <see cref="EdgeGeometry"/>'s four boundary curves.<br/>
    /// （<b>兩種幾何形式及其來源。</b>CS2 未儲存逐區段的道路網格。組成僅指名一份橫斷面——
    /// 由車道、路肩、緣石與側面部件構成的 <c>NetCompositionPiece</c> 緩衝區，各為一般的
    /// <c>NetPiecePrefab</c>（其衍生自 <c>RenderPrefab</c>，故幾何讀取途徑與建築相同）。
    /// 遊戲將這些部件沿邊鋪排，並於<i>頂點著色器</i>中彎折至路面之上，
    /// 而其八個矩陣各自僅是一條貝茲曲線的四個控制點，對邊而言即 <see cref="EdgeGeometry"/>
    /// 的四條邊界曲線。）<br/><br/>
    /// So both forms come off the same CPU-side data. <see cref="RipNetworks"/> writes the
    /// centerline form — the midline of the left and right boundary curves, which is exact and
    /// cheap, and the mesh form — the pieces assembled and bent into real geometry, which is
    /// what carries a road's thickness, shoulders, kerbs and materials. Which one a network gets
    /// depends on whether it has a cross-section to assemble at all.<br/>
    /// （兩種形式皆源自同一份 CPU 端資料。<see cref="RipNetworks"/> 寫出中心線形式；
    /// 亦寫出網格形式——將部件組裝並彎折為真實幾何，
    /// 後者方能承載道路的厚度、路肩、緣石與材質。）<br/><br/>
    /// <b>No <c>kind: "node"</c> records.</b> CS2 keeps no junction geometry on the node. The node
    /// entity carries <c>Game.Net.NodeGeometry</c>, and that is bounds, height, flatness and an
    /// offset — no surface at all; the flare where an edge meets an intersection is stored on the
    /// <i>edge</i>, as <c>StartNodeGeometry</c> / <c>EndNodeGeometry</c>. Emitting node records
    /// would invent geometry the game does not have.<br/>
    /// （<b>不輸出 <c>kind: "node"</c> 紀錄。</b>CS2 未在節點上保存路口幾何：節點實體帶有
    /// <c>Game.Net.NodeGeometry</c>，其僅含邊界、高度、平坦度與偏移，全無表面可言；
    /// 邊與路口相接處的擴張形狀存於<i>邊</i>之上。輸出節點紀錄等同憑空捏造遊戲所無的幾何。）<br/><br/>
    /// A node holding no geometry does <i>not</i> make a junction the union of the segment ends
    /// meeting in it. An earlier version of this comment said it did, and that is measurably false:
    /// across a full city, 1,607 of 1,817 road junctions have every flare ending more than a metre
    /// short of the node. The gap is closed by <see cref="AppendJunctionFan"/> and
    /// <see cref="AppendMedianFill"/>; what has been ruled out, and what remains a reconstruction
    /// rather than a reproduction, is recorded in <c>docs/bundle-schema.md</c> §5.1.<br/>
    /// （節點不持有幾何，並不意味著路口即為交會其中的各區段端點之聯集。本註解的舊版曾如此宣稱，
    /// 而該說法可由量測證否：全城 1,817 個道路路口中，有 1,607 個的所有擴張部皆止於距節點一公尺以外。
    /// 此空缺由 <see cref="AppendJunctionFan"/> 與 <see cref="AppendMedianFill"/> 填補；
    /// 已排除者為何、以及何以仍屬重建而非重現，記於 <c>docs/bundle-schema.md</c> §5.1。）
    /// </summary>
    public partial class NetworkRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The distance a bezier may deviate from its chord and still be written as a straight
        /// line, in metres.<br/>
        /// （貝茲曲線與其弦之間仍可視為直線的最大偏差距離，單位為公尺。）<br/><br/>
        /// Tighter than the vector export's 1 m, because a road ribbon is looked at from close up
        /// rather than plotted on a map; a straight road still collapses to its two end points.<br/>
        /// （較向量輸出的 1 公尺更嚴格，因為道路帶會被近距離檢視而非繪於地圖上；
        /// 筆直的道路仍會化簡為首尾兩點。）
        /// </summary>
        private const float StraightThreshold = 0.2f;

        /// <summary>
        /// The spacing between tessellated points on a curved centerline, in metres.
        /// （彎曲中心線上細分點的間距，單位為公尺。）
        /// </summary>
        private const float TessellationGap = 3f;

        /// <summary>
        /// How far from the camera the spike looks for a road to rip, in metres.
        /// （驗證擷取時，自攝影機尋找道路的範圍半徑，單位為公尺。）<br/><br/>
        /// Wide enough that the road on screen is in range at any normal zoom, tight enough that
        /// what comes out is the road being looked at rather than one across the city.<br/>
        /// （足夠寬，使一般縮放下畫面中的道路都在範圍內；也足夠窄，
        /// 使結果是正在注視的那條路，而非城市另一端的某條路。）
        /// </summary>
        private const float FocusRadius = 250f;

        /// <summary>
        /// How far the two halves of a node's flare must part before the wedge between them is
        /// bridged, in metres.<br/>
        /// （節點擴張部兩半須分離多遠，其間的楔形才需橋接，單位為公尺。）<br/><br/>
        /// An ordinary node shares one median curve between its halves, and every one of a full
        /// city's 32,473 measured to a gap under 0.01 m — so this is a float-comparison epsilon
        /// rather than a tuned tolerance. It exists so that a shared median is recognised as shared
        /// however it was arrived at, not to decide how big a hole is worth filling.<br/>
        /// （一般節點的兩半共用同一條分隔線，全城 32,473 份的間隙皆小於 0.01 公尺；
        /// 故此為浮點比較的容許誤差，而非經調校的閾值。其存在是為使共用的分隔線
        /// 無論如何算得皆能被認出，而非用以判定多大的破洞才值得填補。）
        /// </summary>
        private const float SeamThreshold = 0.01f;

        /// <summary>
        /// The query for placed network edges.（已放置路網邊的查詢。）
        /// </summary>
        EntityQuery _edgeQuery;

        /// <summary>
        /// The query for orphan nodes — a placed node no edge connects to, which the game draws
        /// as two half-disc caps (<c>NetSubMesh.Orphan1/2</c> via <c>CalculateOrphanParameters</c>).
        /// A rip that only walks edges misses them entirely.<br/>
        /// （孤立節點的查詢——無任何邊相連的已放置節點，遊戲以兩個半圓蓋繪製之。
        /// 僅走訪邊的擷取會完全遺漏它們。）
        /// </summary>
        EntityQuery _orphanQuery;

        /// <summary>
        /// How many junction wedges this rip has bridged, and the widest of them.
        /// （本次擷取橋接的路口楔形數量，及其中最寬者。）<br/><br/>
        /// On the system rather than threaded through the assembly: a rip works one edge at a time
        /// on the main thread, so this is the whole of the state, and passing it down five
        /// signatures would bury the geometry in bookkeeping. The wide tail is worth watching —
        /// 702 of a city's junctions part by more than 8 m, and a bridge that wide is a claim about
        /// the road surface that deserves to be looked at rather than assumed.<br/>
        /// （置於系統上而非貫穿組裝流程：擷取於主執行緒上逐邊處理，故此即全部狀態，
        /// 而將其傳遞於五層簽章之間只會使幾何程式碼淹沒於記帳之中。
        /// 寬幅的長尾值得留意——全城有 702 處路口分離超過 8 公尺。）
        /// </summary>
        private int _seamsFilled;
        private int _seamsSkipped;
        private int _fansBuilt;
        private float _worstSeam;

        /// <summary>
        /// How often the game rules this rip reproduces or defers were actually met — the
        /// validation matrix's live side. Rotated edges are reproduced; the rest are counted so a
        /// gap is a number in the report, not a surprise in a consumer.
        /// （本次擷取所重現或暫緩之遊戲規則的實際出現次數——驗證矩陣的即時面。
        /// 旋轉的邊已重現；其餘計數之，使缺口成為報告中的數字，而非消費端的意外。）
        /// </summary>
        private int _rotatedEdges;
        private int _rotatedNodeParts;
        private int _roundaboutParts;
        private int _skipBottomPieces;
        private int _railPieces;
        private int _orphanCaps;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _edgeQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Composition>(),
                    ComponentType.ReadOnly<Curve>(),
                    ComponentType.ReadOnly<Edge>(),
                    ComponentType.ReadOnly<EdgeGeometry>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    // Mid-edit bookkeeping that must never reach the bundle, exactly as the
                    // instance rip excludes it.
                    // （編輯中的暫存狀態，絕不可進入束中，與實例擷取的排除方式相同。）
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            _orphanQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Net.Orphan>(),
                    ComponentType.ReadOnly<Node>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    // The game's own exclusions: outside connections draw no orphan caps, and the
                    // usual mid-edit bookkeeping never reaches the bundle.
                    // （遊戲自身的排除條件：對外連接不繪製孤立節點蓋，
                    //   編輯中的暫存狀態亦一如既往不得進入束中。）
                    ComponentType.ReadOnly<Game.Net.OutsideConnection>(),
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            base.OnCreate();
            _log.Debug("NetworkRipSystem instance created. 路網擷取系統實例創造完成。");
        }

        /// <summary>
        /// One unit of work for the writer: a segment mesh to serialise, or a texture to encode.
        /// （寫出端的一項工作：待序列化的路段網格，或待編碼的貼圖。）<br/><br/>
        /// <b>Both share one queue, drained in order by one writer.</b> A texture's bundle path is
        /// the content hash of the PNG it becomes, which is not known until it has been encoded, so
        /// a <c>.glb</c> can only name its textures if they were written first. An edge resolves and
        /// queues its pieces' textures while it is being assembled — before the assembled mesh is
        /// queued — so a single ordered writer always encodes them first. Writing the <c>.glb</c>
        /// inline on the main thread instead, as this once did, races the encoder and loses: the
        /// material is named but every map resolves to <c>null</c>, and the segment opens
        /// untextured.<br/>
        /// （<b>兩者共用單一佇列，由單一寫出端依序排空。</b>貼圖的束路徑是其所成 PNG 的內容雜湊，
        /// 須待編碼後方能得知，故 <c>.glb</c> 唯有在貼圖先寫出時才能指名它們。邊於組裝期間即解析
        /// 並排入其部件的貼圖——早於已組裝網格的排入——因此單一有序的寫出端必定先編碼貼圖。
        /// 若如先前那樣在主執行緒上直接寫出 <c>.glb</c>，則會與編碼端競爭而落敗：
        /// 材質雖有名稱，但每張貼圖皆解析為 <c>null</c>，路段開啟時毫無貼圖。）
        /// </summary>
        private struct NetworkItem
        {
            /// <summary>The assembled segment mesh to serialise, or <c>null</c>.（待序列化的已組裝路段網格，或 <c>null</c>。）</summary>
            public MeshAsset Mesh;

            /// <summary>The texture to encode, or <c>null</c>.（待編碼的貼圖，或 <c>null</c>。）</summary>
            public PendingTexture Texture;
        }

        /// <summary>
        /// A network rip in flight, carried between frames.
        /// （進行中的路網擷取，於幀與幀之間承載其狀態。）
        /// </summary>
        private class NetworkRun
        {
            public Entity[] Edges;
            public int Index;
            public Entity[] Orphans;
            public int OrphanIndex;
            public NetworkWriter Writer;
            public string OutputDirectory;
            public string MeshDirectory;
            public StringBuilder Report = new();
            public Dictionary<Entity, MeshAsset> PieceCache = new();
            public Dictionary<Entity, string> AggregateNames = new();
            public Dictionary<string, int> ByCategory = new();
            public Dictionary<string, int> ByForm = new();
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();
            public int SkippedMarker, SkippedNoWidth, SkippedDegenerate, SkippedStale;
            public int Points, Triangles;
            public long Bytes;
            public BlockingCollection<NetworkItem> Queue;
            public Task Encoder;
            public List<RipWarning> Skipped = new();

            /// <summary>
            /// The material system, resolved on the main thread and held for the writer.
            /// （材質系統，於主執行緒解析後供寫出端持有。）<br/><br/>
            /// Looking a system up goes through the ECS world, which is a main-thread affair; the
            /// writer needs the reference but must not be the one to obtain it.<br/>
            /// （查找系統須經由 ECS 世界，屬主執行緒事務；寫出端需要該參照，但不應由它取得。）
            /// </summary>
            public MaterialRipSystem Materials;
        }

        /// <summary>
        /// How many read-back textures and assembled segment meshes may wait to be written.
        /// （最多可有多少張已回讀的貼圖與已組裝的路段網格等待寫出。）<br/><br/>
        /// Each holds a full uncompressed payload — a texture's pixels at four bytes each, or a
        /// segment's vertex arrays — so an unbounded queue only moves the memory problem; the
        /// reader pauses when it is full, exactly as the mesh rip's does.
        /// <br/>（每個項目皆持有完整未壓縮的內容：貼圖每像素四位元組的像素，或路段的頂點陣列。
        /// 故無上限的佇列只是把記憶體問題搬家；佇列滿時讀取端暫停，與網格擷取的做法相同。）
        /// </summary>
        private const int QueueCapacity = 8;

        /// <summary>
        /// The rip in flight, or <c>null</c>.（進行中的擷取，或 <c>null</c>。）
        /// </summary>
        NetworkRun _run;

        /// <summary>
        /// Whether a rip is running.（是否有擷取正在進行。）
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How long a single frame may spend advancing a rip, in milliseconds.
        /// （單幀可用於推進擷取的時間上限，單位為毫秒。）<br/><br/>
        /// A budget rather than a fixed count of edges: a junction of a wide road costs many times
        /// what a straight footpath does, so a fixed count would stutter on exactly the parts of a
        /// city that are worth watching.<br/>
        /// （採用時間預算而非固定邊數：寬闊道路的路口成本數倍於筆直的人行道，
        /// 固定邊數會恰在城市中最值得觀看之處造成卡頓。）
        /// </summary>
        private const double FrameBudgetMs = 12d;

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）<br/><br/>
        /// A rip in flight holds an open file, so it has to be closed here — the user quitting to
        /// the menu mid-rip is otherwise a truncated <c>networks.jsonl</c>.<br/>
        /// （進行中的擷取持有開啟的檔案，故須於此關閉；否則使用者於擷取途中返回主選單，
        /// 將留下被截斷的 <c>networks.jsonl</c>。）
        /// </summary>
        protected override void OnDestroy()
        {
            NetworkRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Network rip abandoned at {run.Index}/{run.Edges.Length} edge(s); the world was unloaded. 路網擷取於世界卸載時中止。");
                run.Writer?.Dispose();

                // The writer is a live thread blocked on the queue, so abandoning the run without
                // closing it leaves it there for the rest of the session.
                // （寫出端是一條阻塞於佇列上的執行緒，若中止工作而不關閉佇列，
                //   它將滯留至本次執行階段結束。）
                Drain(run);
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Drive the progress indicator, and advance any rip in progress by one frame's worth.
        /// （驅動進度指示器，並將進行中的擷取推進一幀份的進度。）
        /// </summary>
        protected override void OnUpdate()
        {
            RipProgress.Pump();

            NetworkRun run = _run;
            if (run != null) Advance(run);
        }

        /// <summary>
        /// Start a network rip, returning at once.（開始路網擷取並立即返回。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <param name="textureSizeCap">The longest edge a written texture may have, or <c>0</c> for no cap.（寫出貼圖的最長邊上限，<c>0</c> 為不限。）</param>
        /// <returns>What the caller should tell the user.（呼叫端應告知使用者的訊息。）</returns>
        public string BeginRip(string outputDirectory, int textureSizeCap = 2048)
        {
            if (_run != null) return "A network rip is already running.";

            // The material table has one owner at a time. A cross-section piece reads through the
            // same `MeshRipSystem.ReadMesh` a building does, and that only resolves a material when
            // a material run is in flight — so without one, every road's primitives keep the
            // synthetic `<piece>_mat0` id, nothing declares it, and the roads import untextured.
            // （材質表同時只有一個擁有者。橫斷面部件經由建築所用的同一個 `ReadMesh` 讀取，
            //   而該處僅在材質擷取進行中時才會解析材質；若無，則每條道路的 primitive 都保留合成的
            //   `<piece>_mat0` ID，無人宣告，道路匯入後即無材質。）
            if (Instance.MeshRip.IsRipping)
                return "A mesh rip is running. Wait for it to finish — the two share the material table.";

            RipProgress.Report(RipProgress.Phase.Scan);
            // Keyed by the mesh objects in the run's own piece cache, so it has the run's lifetime.
            // （以該次工作自身部件快取中的網格物件為鍵，故其生命週期與該次工作相同。）
            m_Tiling.Clear();
            m_FrontHalf.Clear();
            _seamsFilled = 0;
            _seamsSkipped = 0;
            _fansBuilt = 0;
            _worstSeam = 0f;
            _rotatedEdges = 0;
            _rotatedNodeParts = 0;
            _roundaboutParts = 0;
            _skipBottomPieces = 0;
            _railPieces = 0;
            _orphanCaps = 0;

            NativeArray<Entity> entities = _edgeQuery.ToEntityArray(Allocator.TempJob);
            Entity[] edges;
            try
            {
                edges = entities.ToArray();
            }
            finally
            {
                entities.Dispose();
            }

            NativeArray<Entity> orphanEntities = _orphanQuery.ToEntityArray(Allocator.TempJob);
            Entity[] orphans;
            try
            {
                orphans = orphanEntities.ToArray();
            }
            finally
            {
                orphanEntities.Dispose();
            }

            NetworkRun run = new()
            {
                Edges = edges,
                Orphans = orphans,
                OutputDirectory = outputDirectory,
                MeshDirectory = IOUtils.CombinePath(outputDirectory, "networks"),
                Writer = new NetworkWriter(IOUtils.CombinePath(outputDirectory, "networks.jsonl")),
                Materials = Instance.MaterialRip
            };

            run.Report.AppendLine($"Network edges: {edges.Length}");

            // Materials ride along, as they do for meshes: a piece's surface assets are reached
            // through the very render prefab its geometry came from. The writer runs off the main
            // thread because a texture is read back on it and compressed off it, and it serialises
            // the segment meshes too so that each one is written after the textures it names.
            // （材質隨之一併擷取，與網格相同：部件的表面資產正是經由其幾何來源的繪製預製模板取得。
            //   寫出端於主執行緒之外進行，因貼圖於主執行緒回讀而於他處壓縮；
            //   路段網格亦由其序列化，以確保每個網格皆晚於其所指名的貼圖寫出。）
            run.Queue = new BlockingCollection<NetworkItem>(QueueCapacity);
            Instance.MaterialRip.Begin(outputDirectory, textureSizeCap,
                                       texture => run.Queue.Add(new NetworkItem { Texture = texture }));
            run.Encoder = Task.Run(() => Encode(run));

            _run = run;
            RipProgress.Report(RipProgress.Phase.Networks, 0, edges.Length + orphans.Length);

            return $"Ripping {edges.Length} network edge(s) and {orphans.Length} orphan node(s).\n\n"
                 + "Progress shows in the corner; a dialog reports the result when it finishes.";
        }

        /// <summary>
        /// Advance a rip by one frame's budget.（以一幀的預算推進擷取。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        private void Advance(NetworkRun run)
        {
            double started = run.Clock.Elapsed.TotalMilliseconds;

            while (run.Index < run.Edges.Length)
            {
                RipOne(run, run.Edges[run.Index]);
                run.Index++;

                if ((run.Clock.Elapsed.TotalMilliseconds - started) >= FrameBudgetMs) break;
            }

            // Orphan caps after the edges, under the same frame budget.
            // （孤立節點蓋於各邊之後處理，共用同一幀預算。）
            if (run.Index >= run.Edges.Length)
            {
                while ((run.OrphanIndex < run.Orphans.Length)
                       && ((run.Clock.Elapsed.TotalMilliseconds - started) < FrameBudgetMs))
                {
                    RipOrphan(run, run.Orphans[run.OrphanIndex]);
                    run.OrphanIndex++;
                }
            }

            RipProgress.Report(RipProgress.Phase.Networks, run.Index + run.OrphanIndex,
                               run.Edges.Length + run.Orphans.Length);

            if ((run.Index >= run.Edges.Length) && (run.OrphanIndex >= run.Orphans.Length)) Finish(run);
        }

        /// <summary>
        /// Rip one network edge into the run.（將單一路網邊擷取至該次工作中。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        /// <param name="entity">The edge to rip.（待擷取的邊。）</param>
        private void RipOne(NetworkRun run, Entity entity)
        {
                {
                    // The work list is captured once and then worked through over many frames, so
                    // the world moves underneath it: the simulation deletes and rebuilds net
                    // entities as it runs, and an edge captured at the start may be gone by the
                    // time its turn comes. Nothing in ECS makes a stale Entity safe to ask about —
                    // it faults inside EntityComponentStore rather than answering false — so the
                    // liveness check has to come first.
                    // （工作清單只擷取一次，卻分散於多幀處理，故世界會在其下改變：
                    //   模擬執行期間會刪除並重建路網實體，開始時擷取的邊，輪到它時可能已不存在。
                    //   ECS 中沒有任何機制能讓失效的 Entity 可安全查詢——
                    //   它會在 EntityComponentStore 內部發生錯誤而非回傳 false——
                    //   故存活檢查必須置於最前。）
                    if (!EntityManager.Exists(entity))
                    {
                        run.SkippedStale++;
                        return;
                    }

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);
                    Entity edgeComposition = composition.m_Edge;
                    bool isTaxiway = (edgeComposition != Entity.Null)
                                  && EntityManager.HasComponent<TaxiwayComposition>(edgeComposition);

                    // Network markers — pipe and cable junction anchors, and airspace — are the
                    // same invisible simulation furniture the instance rip drops, so the two
                    // layers agree on what "the built environment" means. Taxiways carry the
                    // marker component and are genuinely drawn, which is why they are the one
                    // exception, exactly as the vector export has it.
                    // （路網標記物——管線與電纜的接點錨、以及空域——與實例擷取所丟棄者同屬不可見的
                    //   模擬用元件，兩層因而對「建成環境」的定義一致。滑行道帶有標記元件卻確實會被繪製，
                    //   故為唯一例外，與向量輸出的處理相同。）
                    if (EntityManager.HasComponent<Game.Net.Marker>(entity) && !isTaxiway)
                    {
                        run.SkippedMarker++;
                        return;
                    }

                    if (isTaxiway
                        && ((EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition).m_Flags & TaxiwayFlags.Airspace) != 0))
                    {
                        run.SkippedMarker++;
                        return;
                    }

                    EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                    Edge edge = EntityManager.GetComponentData<Edge>(entity);
                    float3 center = Colossal.Mathematics.MathUtils.Center(geometry.m_Bounds);

                    float width = GetWidth(edgeComposition, entity, geometry);
                    if (!(width > 0f))
                    {
                        // The schema's centerline form requires a positive width, since a consumer
                        // extrudes by it; a zero-width run is nothing anyone can draw.
                        // （結構描述的中心線形式要求正值寬度，因消費端據以擠出；零寬度者無從繪製。）
                        run.SkippedNoWidth++;
                        return;
                    }

                    NetworkRecord record = new() { Id = entity.Index, Kind = "segment" };

                    // Mesh form where the network has a cross-section to assemble, centerline form
                    // where it does not. The contract allows a bundle to mix them freely and this
                    // is what that is for: a road, a rail line or a path is built from pieces and
                    // becomes real geometry, while a pipe or a cable has no surface at all and is
                    // only ever a line the consumer extrudes.
                    // （有橫斷面可組裝者採網格形式，無者採中心線形式。契約允許束自由混用兩者，
                    //   其用意正在於此：道路、軌道與路徑由部件構成，可成為真實幾何；
                    //   而管線或電纜根本沒有表面，永遠只是一條供消費端擠出的線。）
                    string assetId = $"seg_{entity.Index}";
                    MeshAsset assembled = AssembleNetwork(entity, geometry, center, assetId, run.PieceCache, null);

                    if (assembled != null)
                    {
                        // Handed to the writer rather than serialised here: assembling the edge has
                        // just queued its pieces' textures, and the .glb can only name them once
                        // they carry a content hash. See NetworkItem.
                        // （交由寫出端而非於此序列化：組裝此邊時方才排入其部件的貼圖，
                        //   而 .glb 唯有在貼圖具備內容雜湊後才能指名它們。詳見 NetworkItem。）
                        run.Queue.Add(new NetworkItem { Mesh = assembled });
                        record.AssetId = assetId;
                        record.Position = center;
                        record.Rotation = quaternion.identity;
                        run.Triangles += assembled.TriangleCount;
                    }
                    else
                    {
                        List<float3> centerline = BuildCenterline(entity, edge, geometry);
                        if (centerline.Count < 2)
                        {
                            run.SkippedDegenerate++;
                            return;
                        }

                        record.Centerline = centerline;
                        record.Width = width;
                        run.Points += centerline.Count;
                    }

                    AddAttributes(entity, edgeComposition, width, record, run.AggregateNames);

                    run.Writer.Write(record);

                    string category = record.Attributes.TryGetValue("category", out object c) ? c.ToString() : "other";
                    run.ByCategory.TryGetValue(category, out int categoryCount);
                    run.ByCategory[category] = categoryCount + 1;

                    string form = record.Attributes.TryGetValue("form", out object f) ? f.ToString() : "normal";
                    run.ByForm.TryGetValue(form, out int formCount);
                    run.ByForm[form] = formCount + 1;
                }

        }

        /// <summary>
        /// Rip one orphan node — a node no edge connects to — as the two half-disc caps the game
        /// draws for it.（將單一孤立節點——無任何邊相連的節點——擷取為遊戲為其繪製的兩個半圓蓋。）
        /// <br/><br/>
        /// The geometry is <c>CalculateOrphanParameters</c>, decompiled: each half is a quarter
        /// circle of béziers from one side of the node around to its apex, ruled against a
        /// degenerate curve at the node's centre, and the node composition's pieces bend across
        /// that exactly as they bend across a flare. The circle is built in world axes from the
        /// node position and the composition width, which is the game's own construction — the
        /// cap does not turn with the node.<br/>
        /// （其幾何即反編譯的 <c>CalculateOrphanParameters</c>：每一半為自節點一側繞至其頂點的
        /// 四分之一貝茲圓弧，與節點中心的退化曲線構成直紋面，節點組成的部件如彎折於擴張部般
        /// 彎折其上。圓以世界座標軸自節點位置與組成寬度建構，此為遊戲自身的作法——
        /// 蓋不隨節點旋轉。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        /// <param name="entity">The orphan node entity.（孤立節點實體。）</param>
        private void RipOrphan(NetworkRun run, Entity entity)
        {
            if (!EntityManager.Exists(entity) || !EntityManager.HasComponent<Game.Net.Orphan>(entity)
                || !EntityManager.HasComponent<Node>(entity))
            {
                run.SkippedStale++;
                return;
            }

            if (EntityManager.HasComponent<Game.Net.Marker>(entity))
            {
                run.SkippedMarker++;
                return;
            }

            Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition))
            {
                run.SkippedDegenerate++;
                return;
            }

            float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
            if (!(width > 0f))
            {
                run.SkippedNoWidth++;
                return;
            }

            float3 position = EntityManager.GetComponentData<Node>(entity).m_Position;

            List<float3> vertices = new();
            List<float2> uvs = new();
            List<int> indices = new();
            List<MeshPrimitive> primitives = new();

            AppendOrphanHalf(composition, position, width, true, run.PieceCache,
                             vertices, uvs, indices, primitives);
            AppendOrphanHalf(composition, position, width, false, run.PieceCache,
                             vertices, uvs, indices, primitives);

            string assetId = $"orphan_{entity.Index}";
            MeshAsset assembled = Finish(assetId, vertices, uvs, indices, primitives);
            if (assembled == null)
            {
                run.SkippedDegenerate++;
                return;
            }

            run.Queue.Add(new NetworkItem { Mesh = assembled });
            run.Triangles += assembled.TriangleCount;
            _orphanCaps++;

            Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            NetworkRecord record = new()
            {
                Id = entity.Index,
                Kind = "segment",
                AssetId = assetId,
                Position = position,
                Rotation = quaternion.identity
            };

            string asset = Instance.Name.GetPrefabName(prefab);
            if (!string.IsNullOrEmpty(asset)) record.Attributes["asset"] = asset;
            record.Attributes["category"] = GetCategory(prefab, composition);
            record.Attributes["form"] = "normal";
            record.Attributes["width"] = Math.Round((double)width, 3);

            run.Writer.Write(record);

            string category = record.Attributes["category"].ToString();
            run.ByCategory.TryGetValue(category, out int categoryCount);
            run.ByCategory[category] = categoryCount + 1;
            run.ByForm.TryGetValue("normal", out int formCount);
            run.ByForm["normal"] = formCount + 1;
        }

        /// <summary>
        /// One half of an orphan cap, built from the game's own synthetic segments.
        /// （孤立節點蓋的一半，以遊戲自身的合成區段建構。）
        /// </summary>
        /// <param name="composition">The orphan's composition.（孤立節點的組成。）</param>
        /// <param name="position">The node position.（節點位置。）</param>
        /// <param name="width">The composition width.（組成寬度。）</param>
        /// <param name="primary">Whether this is the +z half.（是否為 +z 的一半。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        private void AppendOrphanHalf(Entity composition, float3 position, float width, bool primary,
                                      Dictionary<Entity, MeshAsset> cache, List<float3> vertices,
                                      List<float2> uvs, List<int> indices, List<MeshPrimitive> primitives)
        {
            // 0.2761424 is the game's own circle constant — the bezier control offset that makes
            // four cubics a circle of radius width/2.
            // （0.2761424 是遊戲自身的圓常數——使四段三次曲線構成半徑 width/2 之圓的控制點偏移。）
            float side = primary ? -1f : 1f;
            float half = width * 0.5f;
            float control = width * 0.2761424f;

            Bezier4x3 arcNear = new(
                new float3(position.x + (side * half), position.y, position.z),
                new float3(position.x + (side * half), position.y, position.z - (side * control)),
                new float3(position.x + (side * control), position.y, position.z - (side * half)),
                new float3(position.x, position.y, position.z - (side * half)));

            Bezier4x3 arcFar = arcNear;
            arcFar.a.x -= side * width;
            arcFar.b.x -= side * width;
            arcFar.c.x = (position.x * 2f) - arcFar.c.x;

            Bezier4x3 centre = new(position, position, position, position);
            float arcLength = width * (math.PI / 2f);

            Segment half0 = new() { m_Left = arcNear, m_Right = centre, m_Length = new float2(arcLength, 0f) };
            Segment half1 = new() { m_Left = centre, m_Right = arcFar, m_Length = new float2(0f, arcLength) };

            AppendComposition(composition, half0, half1, true, position,
                              primary ? "orphan+" : "orphan-", cache,
                              vertices, uvs, indices, primitives, null, null);
        }

        /// <summary>
        /// Close the file, report, and tell the user what came out.
        /// （關閉檔案、產出報告，並告知使用者結果。）
        /// </summary>
        /// <param name="run">The finished run.（已完成的工作。）</param>
        private void Finish(NetworkRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();
            run.Writer.Dispose();

            // Materials last: the table can only name a texture by the content hash of the file it
            // became, so it is written once the writer has drained. Draining also flushes the last
            // segment meshes, which is why the byte count below is only final afterwards.
            // （材質最後處理：材質表只能以貼圖檔案的內容雜湊指名該貼圖，故須待寫出端排空後方能寫出。
            //   排空亦會沖出最後的路段網格，故下方的位元組數須於其後方為最終值。）
            string materials = Drain(run);

            StringBuilder report = run.Report;
            report.AppendLine($"Wrote {run.Writer.Count} segment(s) to networks.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
            report.AppendLine($"  mesh form: {run.Writer.MeshForm} ({run.Triangles} triangles, "
                            + $"{run.Bytes / 1048576d:F1} MiB of glTF in networks/)");
            report.AppendLine($"  centerline form: {run.Writer.CenterlineForm} ({run.Points} point(s))");
            report.AppendLine($"Distinct cross-section pieces read: {run.PieceCache.Count}");
            report.AppendLine($"Junctions closed: {_fansBuilt} fan(s)");
            report.AppendLine($"Junction wedges bridged: {_seamsFilled} (widest {_worstSeam:F1} m), "
                            + $"{_seamsSkipped} too wide to be a median");
            if (_rotatedEdges > 0) report.AppendLine($"Rotated edges assembled: {_rotatedEdges}");
            if (_orphanCaps > 0) report.AppendLine($"Orphan node caps: {_orphanCaps}");

            // Game rules seen but not reproduced — the numbers say whether the gap matters in
            // this city. （已見而未重現的遊戲規則——這些數字說明其缺口於本城市是否要緊。）
            if (_roundaboutParts > 0) report.AppendLine($"APPROXIMATED: {_roundaboutParts} roundabout part(s) — "
                                                      + "halves + wedge cover outer/inner/island; blend weights are shader-side");
            if (_railPieces > 0) report.AppendLine($"Corner-rail (PreserveShape) pieces placed: {_railPieces}");
            if (_skipBottomPieces > 0) report.AppendLine($"SkipBottomHalf pieces halved: {_skipBottomPieces}");
            if (_rotatedNodeParts > 0) report.AppendLine($"NOT REPRODUCED: {_rotatedNodeParts} rotated node composition part(s)");

            foreach (KeyValuePair<string, int> kvp in run.ByCategory) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine("By form:");
            foreach (KeyValuePair<string, int> kvp in run.ByForm) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            if (run.SkippedMarker > 0) report.AppendLine($"Skipped {run.SkippedMarker} marker network(s)");
            if (run.SkippedNoWidth > 0) report.AppendLine($"Skipped {run.SkippedNoWidth} edge(s) with no usable width");
            if (run.SkippedDegenerate > 0) report.AppendLine($"Skipped {run.SkippedDegenerate} edge(s) whose centerline collapsed to a point");
            if (run.SkippedStale > 0) report.AppendLine($"Skipped {run.SkippedStale} edge(s) the simulation removed mid-rip");
            if (run.Skipped.Count > 0) report.AppendLine($"{run.Skipped.Count} surface(s) or texture(s) produced no material entry");
            if (!string.IsNullOrEmpty(materials)) report.AppendLine($"Materials: {materials}");
            report.AppendLine($"Output: {run.OutputDirectory}");

            _log.Info(report.ToString());

            try
            {
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]",
                                                   Settings.Fit(report.ToString()), "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the network rip result: {ex.Message}");
            }
        }

        /// <summary>
        /// Encode every queued texture and serialise every queued segment mesh, in the order the
        /// rip produced them, until it stops queueing.
        /// （依擷取產生的順序編碼每張排入佇列的貼圖並序列化每個排入佇列的路段網格，直至不再排入為止。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        private static void Encode(NetworkRun run)
        {
            MaterialRipSystem materials = run.Materials;

            foreach (NetworkItem item in run.Queue.GetConsumingEnumerable())
            {
                // One bad item must not take the writer down with it: the queue would then never
                // drain, and the rip would block forever waiting for it.
                // （單一損壞的項目不可拖垮寫出端：否則佇列永不排空，擷取將永遠等待下去。）
                try
                {
                    // Textures first, always: they arrive before the segment that named them, and
                    // encoding one is what gives it the content hash that segment's .glb writes down.
                    // （貼圖恆先處理：它們早於指名它們的路段抵達，
                    //   而編碼貼圖正是賦予其內容雜湊的動作，該雜湊將寫入該路段的 .glb。）
                    if (item.Texture != null)
                    {
                        materials.WriteTexture(item.Texture);
                        continue;
                    }

                    MeshAsset mesh = item.Mesh;
                    if (mesh == null) continue;

                    string path = IOUtils.CombinePath(run.MeshDirectory, $"{mesh.AssetId}.glb");
                    run.Bytes += GltfWriter.Write(path, mesh, materials.Describe);
                }
                catch (Exception ex)
                {
                    _log.Warn($"Could not write a network {(item.Texture != null ? "texture" : "mesh")}: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Close the writer's queue, wait for it, and write <c>materials.json</c>.
        /// （關閉寫出端的佇列、等待其完成，並寫出 <c>materials.json</c>。）
        /// </summary>
        /// <param name="run">The run being finished or abandoned.（正在結束或中止的工作。）</param>
        /// <returns>The material summary, or <c>null</c>.（材質摘要，或 <c>null</c>。）</returns>
        private string Drain(NetworkRun run)
        {
            try
            {
                run.Queue?.CompleteAdding();
                run.Encoder?.Wait();
            }
            catch (Exception ex)
            {
                _log.Warn($"Network writer ended with an error: {ex.Message}");
            }

            try
            {
                return Instance.MaterialRip.Finish(run.Report, run.Skipped);
            }
            catch (Exception ex)
            {
                _log.Error($"Material rip failed to finish: {ex}");
                return null;
            }
        }

        /// <summary>
        /// Assemble one network edge into a single mesh: the junction flare at its start, the run
        /// between the nodes, and the flare at its end.<br/>
        /// （將單一路網邊組裝為單一網格：起點的路口擴張部、節點之間的路段，以及終點的擴張部。）
        /// <br/><br/>
        /// <c>Composition</c> names three composition entities and each has its own cross-section,
        /// because an intersection's surface is genuinely different geometry from the road leading
        /// into it. Assembling all three covers node to node with nothing left between them, which
        /// is what a constant-width ribbon could never do at a skewed junction.<br/>
        /// （<c>Composition</c> 指名三個組成實體，各有自身的橫斷面，因為路口表面確實是與駛入其中
        /// 的道路截然不同的幾何。組裝三者即可自節點涵蓋至節點，中間毫無空隙。）
        /// </summary>
        /// <param name="entity">The edge entity.（邊實體。）</param>
        /// <param name="geometry">Its edge geometry.（其邊的幾何。）</param>
        /// <param name="center">The origin the mesh is built around.（網格所環繞的原點。）</param>
        /// <param name="assetId">The asset id to label it with.（用以標記的資產 ID。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="report">Where to note the working, or <c>null</c> to stay quiet.（記錄過程之處，或 <c>null</c> 表示不記錄。）</param>
        /// <returns>The mesh, or <c>null</c> when the network has no pieces to assemble.（網格；若該路網無部件可組裝則為 <c>null</c>。）</returns>
        private MeshAsset AssembleNetwork(Entity entity, EdgeGeometry geometry, float3 center, string assetId,
                                          Dictionary<Entity, MeshAsset> cache, StringBuilder report)
        {
            Composition parts = EntityManager.GetComponentData<Composition>(entity);

            List<float3> vertices = new();
            List<float2> uvs = new();
            List<int> indices = new();
            List<MeshPrimitive> primitives = new();

            // The game's own draw gates (`BatchInstanceSystem.UpdateNetInstances`): an edge part
            // needs more than 0.1 m of run, a flare more than 0.05 m on either half. Below that
            // the game draws nothing, and assembling it here would emit degenerate slivers the
            // game never shows.
            // （遊戲自身的繪製門檻：邊的部分須超過 0.1 公尺，擴張部任一半須超過 0.05 公尺。
            //   低於此值遊戲不繪製任何內容，於此組裝只會產出遊戲從不顯示的退化碎面。）
            if (EntityManager.HasComponent<StartNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry;
                if (math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))
                {
                    AppendComposition(parts.m_StartNode, node.m_Left, node.m_Right, true, center,
                                      "startNode", cache, vertices, uvs, indices, primitives, node, report);
                }
            }

            if (math.any((geometry.m_Start.m_Length + geometry.m_End.m_Length) > 0.1f))
            {
                AppendComposition(parts.m_Edge, geometry.m_Start, geometry.m_End, false, center,
                                  "edge", cache, vertices, uvs, indices, primitives, null, report);
            }

            if (EntityManager.HasComponent<EndNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry;
                if (math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))
                {
                    AppendComposition(parts.m_EndNode, node.m_Left, node.m_Right, true, center,
                                      "endNode", cache, vertices, uvs, indices, primitives, node, report);
                }
            }

            // The junction itself, once per junction rather than once per edge meeting in it.
            // （路口本身，每個路口僅產生一次，而非交會其中的每條邊各產生一次。）
            Edge ends = EntityManager.GetComponentData<Edge>(entity);
            AppendJunctionFan(entity, ends.m_Start, parts.m_StartNode, center, cache,
                              vertices, uvs, indices, primitives, report);
            AppendJunctionFan(entity, ends.m_End, parts.m_EndNode, center, cache,
                              vertices, uvs, indices, primitives, report);

            return Finish(assetId, vertices, uvs, indices, primitives);
        }

        /// <summary>
        /// Reduce a name to the character set the contract allows an <c>assetId</c>.
        /// （將名稱化簡為契約允許 <c>assetId</c> 使用的字元集合。）
        /// </summary>
        /// <param name="name">The raw name.（原始名稱。）</param>
        /// <returns>A safe id.（可安全使用的識別碼。）</returns>
        private static string SanitizeId(string name)
        {
            if (string.IsNullOrEmpty(name)) return "unnamed";

            StringBuilder safe = new(name.Length);
            foreach (char c in name)
            {
                safe.Append(char.IsLetterOrDigit(c) || (c == '.') || (c == '-') || (c == '_') ? c : '_');
            }

            // The pattern requires the first character to be alphanumeric.
            // （該樣式要求首字元為英數字。）
            if (!char.IsLetterOrDigit(safe[0])) safe.Insert(0, 'x');
            return safe.ToString();
        }

        /// <summary>
        /// Whether an edge is the invisible simulation furniture both rip queries drop — a pipe or
        /// cable junction anchor, or airport airspace.<br/>
        /// （某邊是否為兩個擷取查詢皆丟棄的不可見模擬用元件：管線或電纜的接點錨，或機場空域。）
        /// <br/><br/>
        /// Taxiways carry the marker component and are genuinely drawn, which is why they are the
        /// one exception, exactly as the vector export has it.<br/>
        /// （滑行道帶有標記元件卻確實會被繪製，故為唯一例外，與向量輸出的處理相同。）
        /// </summary>
        /// <param name="entity">The edge entity.（邊實體。）</param>
        /// <returns>Whether it is a marker network.（其是否為標記類路網。）</returns>
        private bool IsMarkerNetwork(Entity entity)
        {
            if (!EntityManager.HasComponent<Composition>(entity)) return false;

            Entity edgeComposition = EntityManager.GetComponentData<Composition>(entity).m_Edge;
            bool isTaxiway = (edgeComposition != Entity.Null)
                          && EntityManager.HasComponent<TaxiwayComposition>(edgeComposition);

            if (EntityManager.HasComponent<Game.Net.Marker>(entity) && !isTaxiway) return true;

            return isTaxiway
                && ((EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition).m_Flags
                     & TaxiwayFlags.Airspace) != 0);
        }

        /// <summary>
        /// How many edges meet at a node. Three or more makes it an intersection rather than a
        /// bend or a dead end.<br/>
        /// （於某節點交會的邊數。三條以上者為路口，而非轉折或盡頭。）
        /// </summary>
        /// <param name="node">The node entity.（節點實體。）</param>
        /// <returns>The number of connected edges.（相接的邊數。）</returns>
        private int CountConnections(Entity node)
        {
            if ((node == Entity.Null) || !EntityManager.HasBuffer<ConnectedEdge>(node)) return 0;
            return EntityManager.GetBuffer<ConnectedEdge>(node, true).Length;
        }


        /// <summary>
        /// Assemble one edge's road geometry: every cross-section piece, tiled along the edge and
        /// bent onto the surface the game paints.<br/>
        /// （組裝單一邊的道路幾何：將橫斷面的每個部件沿邊鋪排，並彎折至遊戲所繪製的表面上。）
        /// <br/><br/>
        /// <b>The two coordinate mappings, which are the whole of it.</b> A piece's mesh is
        /// authored in its own space: <c>x</c> spans its width centred on zero, <c>z</c> spans the
        /// authored tile length centred on zero, and <c>y</c> is height relative to the road
        /// surface. <c>NetCompositionPiece.m_Offset.x</c> then says where across the composition
        /// that piece sits. So:<br/>
        /// （<b>兩項座標映射即是全部。</b>部件網格以自身空間製作：<c>x</c> 為以零為中心的寬度，
        /// <c>z</c> 為以零為中心的鋪排長度，<c>y</c> 為相對於路面的高度。）
        /// <list type="bullet">
        /// <item>across → <c>u = (m_Offset.x + x + width/2) / width</c>, the lerp between the
        /// edge's left and right boundary bezier;</item>
        /// <item>along → the tile index plus <c>(z + half) / length</c>, over the edge as a
        /// whole.</item>
        /// </list>
        /// The boundary curves already carry the road's real widening and its height, so a bent
        /// piece lands exactly where the game draws it, junction flares included.<br/>
        /// （邊界曲線本就帶有道路真實的加寬與高度，故彎折後的部件恰落於遊戲繪製之處。）<br/><br/>
        /// Tiling is kept rather than stretching one tile over the whole edge: a tile's 18 rows
        /// give a station every ~3.5 m, which is what keeps a bend smooth, and any along-length
        /// detail in a piece survives.<br/>
        /// （保留鋪排而非將單一區塊拉伸至整條邊：每區塊的 18 列可提供約每 3.5 公尺一個取樣站，
        /// 這正是彎道保持平滑的原因，且部件沿長度方向的細節得以保留。）
        /// </summary>
        /// <param name="pieces">The composition's cross-section.（組成的橫斷面。）</param>
        /// <param name="geometry">The edge to bend onto.（要彎折至其上的邊。）</param>
        /// <param name="compositionWidth">The composition's nominal width.（組成的標稱寬度。）</param>
        /// <param name="center">The edge's bounds centre, which the result is relative to.（邊的邊界中心，結果以其為原點。）</param>
        /// <param name="assetId">The asset id to label it with.（用以標記的資產 ID。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        /// <returns>The assembled mesh, or <c>null</c> when no piece would read.（組裝後的網格；若無部件可讀則為 <c>null</c>。）</returns>
        /// <summary>
        /// Per-piece work that does not change from one edge to the next.<br/>
        /// （逐部件、且不隨邊改變的計算結果。）<br/><br/>
        /// Bending a piece onto a curve asks two questions of every vertex: how far along the run
        /// it sits, and where the boundary curves are at that distance. The first depends only on
        /// the vertex's <c>z</c> in the authored mesh, and the second only on the first — so both
        /// are properties of the <i>piece</i>, not of the edge it is being placed on. A road piece
        /// is a lattice: the 1,150-vertex elevated side piece has 83 distinct <c>z</c> values, so
        /// evaluating the béziers per vertex evaluates each one about fourteen times over. Doing it
        /// per distinct <c>z</c> instead costs the same answer once.<br/>
        /// （將部件彎折至曲線上，需對每個頂點提問兩件事：其沿線位置，以及邊界曲線於該處之所在。
        /// 前者僅取決於頂點在製作網格中的 <c>z</c>，後者又僅取決於前者——故兩者皆為<i>部件</i>的性質，
        /// 而非其所置於之邊的性質。道路部件是格狀結構：1,150 個頂點的高架側邊部件僅有 83 個相異
        /// <c>z</c> 值，故逐頂點求值等同於將每個答案重算約十四次。改為逐相異 <c>z</c> 求值，
        /// 同樣的答案只需算一次。）<br/><br/>
        /// The <c>z</c> census itself was also being retaken for every edge; a city has tens of
        /// thousands of edges and a few hundred pieces.<br/>
        /// （<c>z</c> 值的普查本身亦曾對每條邊重做一次；城市有數萬條邊，卻僅有數百種部件。）
        /// </summary>
        private sealed class PieceTiling
        {
            public float MinZ;
            public float TileLength;

            /// <summary>
            /// How much V one tile advances — the game's texture density times the tile length.
            /// （單一鋪排單元推進的 V 量——遊戲的貼圖密度乘以鋪排長度。）<br/><br/>
            /// For tiled pieces the game does not sample the authored V at all: it bakes the mean
            /// |ΔV|/|Δz| of the piece's triangles as a density and lets the shader run
            /// <c>V = along × density</c>, continuous over the whole part. Repeating the authored
            /// V per tile instead differs by <c>frac(span)</c> at every tile boundary — a visible
            /// texture seam whenever the authored span is not a whole number. Offsetting each
            /// tile's V by <c>tile × VSpan</c> reproduces the game's accumulation exactly.
            /// Triangles containing a negative authored V are excluded from the mean and from the
            /// offset, as the game excludes them — negative V is its marker for absolute mapping.
            /// （對鋪排部件，遊戲完全不取樣製作 V：它將三角形的平均 |ΔV|/|Δz| 烘焙為密度，
            /// 由著色器計算 <c>V = 沿線距離 × 密度</c>，於整段連續。若改為逐鋪排重複製作 V，
            /// 每個鋪排交界處將相差 <c>frac(跨度)</c>——凡跨度非整數即為可見的貼圖接縫。
            /// 將每個鋪排的 V 偏移 <c>tile × VSpan</c> 即精確重現遊戲的累積。
            /// 含負 V 的三角形不計入平均亦不偏移，與遊戲相同——負 V 是其絕對映射的標記。）
            /// </summary>
            public float VSpan;

            /// <summary>Per source vertex, which distinct-<c>z</c> slot it belongs to.
            /// （逐來源頂點，其所屬的相異 <c>z</c> 槽位。）</summary>
            public int[] Slot;

            /// <summary>Per slot, its fraction along one tile.（逐槽位，其於單一鋪排單元中的比例。）</summary>
            public float[] Within;

            // Scratch, refilled per tile. A is the first half's curve pair, B the second's, used
            // only when the part splits across the width. Middle is the node's own middle curve,
            // used only by the wedge bend.
            // （暫存區，逐鋪排單元重填。A 為第一半的曲線對，B 為第二半的，僅於橫向分割時使用。
            //   Middle 為節點自身的中線曲線，僅供楔形彎折使用。）
            public float3[] LeftA, RightA, LeftB, RightB, Middle;

            public static PieceTiling Build(MeshAsset source)
            {
                float3[] verts = source.Vertices;
                float minZ = verts[0].z, maxZ = verts[0].z;
                for (int i = 0; i < verts.Length; i++)
                {
                    minZ = math.min(minZ, verts[i].z);
                    maxZ = math.max(maxZ, verts[i].z);
                }

                float tileLength = math.max(maxZ - minZ, 1e-3f);
                PieceTiling tiling = new()
                {
                    MinZ = minZ,
                    TileLength = tileLength,
                    Slot = new int[verts.Length]
                };

                // Exact equality is the right test: these are the authored coordinates of a lattice,
                // so vertices that share a row share the bit pattern. A tolerance would risk welding
                // two genuinely close rows into one and shearing the piece.
                // （精確相等是正確的判準：這些是格狀結構的製作座標，同一列的頂點具有相同的位元樣式。
                //   採用容差反而可能將兩個確實相近的列併為一列，使部件產生剪切變形。）
                Dictionary<float, int> slots = new();
                List<float> within = new();
                for (int i = 0; i < verts.Length; i++)
                {
                    float z = verts[i].z;
                    if (!slots.TryGetValue(z, out int slot))
                    {
                        slot = within.Count;
                        slots[z] = slot;
                        within.Add((z - minZ) / tileLength);
                    }
                    tiling.Slot[i] = slot;
                }

                tiling.Within = within.ToArray();
                tiling.LeftA = new float3[within.Count];
                tiling.RightA = new float3[within.Count];
                tiling.LeftB = new float3[within.Count];
                tiling.RightB = new float3[within.Count];
                tiling.Middle = new float3[within.Count];

                // The game's own density estimate: sum of |ΔV| over sum of |Δz| across the
                // triangles whose V is entirely non-negative.
                // （遊戲自身的密度估計：於 V 全為非負的三角形上，|ΔV| 總和除以 |Δz| 總和。）
                if ((source.Uvs != null) && (source.Uvs.Length == verts.Length) && (source.Indices != null))
                {
                    float dv = 0f, dz = 0f;
                    for (int i = 0; i + 2 < source.Indices.Length; i += 3)
                    {
                        int a = source.Indices[i], b = source.Indices[i + 1], c = source.Indices[i + 2];
                        float3 v = new(source.Uvs[a].y, source.Uvs[b].y, source.Uvs[c].y);
                        if (math.any(v < 0f)) continue;
                        dv += math.abs(v.y - v.x) + math.abs(v.z - v.y) + math.abs(v.x - v.z);
                        dz += math.abs(verts[b].z - verts[a].z) + math.abs(verts[c].z - verts[b].z)
                            + math.abs(verts[a].z - verts[c].z);
                    }

                    tiling.VSpan = (dz > 1e-3f) ? ((dv / dz) * tileLength) : 0f;
                }

                return tiling;
            }

            /// <summary>
            /// Place every distinct <c>z</c> of one tile on a junction's five curves: both halves'
            /// boundary pairs, and the node's own middle curve between them.<br/>
            /// （將單一鋪排單元的每個相異 <c>z</c> 置於路口的五條曲線上：
            /// 兩半各自的邊界曲線對，及其間節點自身的中線曲線。）<br/><br/>
            /// The middle curve is evaluated at <c>t × 0.99</c> because that is what the game does —
            /// <c>CalculateNodeParameters</c> splits <c>m_Middle</c> at 0.99 before handing it to
            /// the shader, so the middles of the edges meeting at a node stop just short of one
            /// another instead of stacking at the node centre.<br/>
            /// （中線曲線以 <c>t × 0.99</c> 求值，因為遊戲即如此處理——
            /// <c>CalculateNodeParameters</c> 先於 0.99 處分割 <c>m_Middle</c> 再交予著色器，
            /// 使交會於節點的各邊中線恰好互不相疊，而非齊聚於節點中央。）
            /// </summary>
            public void EvaluateWedge(int tile, int tiles, Segment half0, Segment half1,
                                      Bezier4x3 middle, bool flipZ)
            {
                bool middleValid = !math.any(math.isnan(middle.a)) && !math.any(math.isnan(middle.d));

                for (int k = 0; k < Within.Length; k++)
                {
                    float within = flipZ ? (1f - Within[k]) : Within[k];
                    float along = math.saturate((tile + within) / tiles);
                    LeftA[k] = Colossal.Mathematics.MathUtils.Position(half0.m_Left, along);
                    RightA[k] = Colossal.Mathematics.MathUtils.Position(half0.m_Right, along);
                    LeftB[k] = Colossal.Mathematics.MathUtils.Position(half1.m_Left, along);
                    RightB[k] = Colossal.Mathematics.MathUtils.Position(half1.m_Right, along);
                    Middle[k] = middleValid
                        ? Colossal.Mathematics.MathUtils.Position(middle, along * 0.99f)
                        : math.lerp(RightA[k], LeftB[k], 0.5f);
                }
            }

            /// <summary>
            /// Place every distinct <c>z</c> of one tile on a junction's two corner rails — the
            /// straight matrices 6/7 a <c>PreserveShape</c> piece rides. The along-parameter is
            /// the game's own: <c>(z + size.z/2) / width</c>, which stands the piece at the rails'
            /// start — the flare's edge-side mouth — at its authored length.<br/>
            /// （將單一鋪排單元的每個相異 <c>z</c> 置於路口的兩條轉角軌上——即
            /// <c>PreserveShape</c> 部件所依循的筆直矩陣 6/7。沿線參數為遊戲自身的
            /// <c>(z + size.z/2) / width</c>，使部件以其製作長度立於軌的起點——
            /// 擴張部靠邊的入口處。）
            /// </summary>
            public void EvaluateRails(Bezier4x3 left, Bezier4x3 right, float sizeZ, float width, bool flipZ)
            {
                for (int k = 0; k < Within.Length; k++)
                {
                    float z = MinZ + (Within[k] * TileLength);
                    if (flipZ) z = -z;
                    float t = math.saturate((z + (sizeZ * 0.5f)) / math.max(width, 1e-3f));
                    LeftA[k] = Colossal.Mathematics.MathUtils.Position(left, t);
                    RightA[k] = Colossal.Mathematics.MathUtils.Position(right, t);
                }
            }

            /// <summary>
            /// Place every distinct <c>z</c> of one tile on the boundary curves.
            /// （將單一鋪排單元的每個相異 <c>z</c> 置於邊界曲線之上。）
            /// </summary>
            public void Evaluate(int tile, int tiles, bool splitAcross, Segment half0, Segment half1,
                                 bool flipZ, float alongScale = 1f)
            {
                for (int k = 0; k < Within.Length; k++)
                {
                    float within = flipZ ? (1f - Within[k]) : Within[k];
                    float along = math.saturate(((tile + within) / tiles) * alongScale);

                    if (splitAcross)
                    {
                        LeftA[k] = Colossal.Mathematics.MathUtils.Position(half0.m_Left, along);
                        RightA[k] = Colossal.Mathematics.MathUtils.Position(half0.m_Right, along);
                        LeftB[k] = Colossal.Mathematics.MathUtils.Position(half1.m_Left, along);
                        RightB[k] = Colossal.Mathematics.MathUtils.Position(half1.m_Right, along);
                    }
                    else
                    {
                        bool firstHalf = along < 0.5f;
                        Segment segment = firstHalf ? half0 : half1;
                        float t = firstHalf ? (along * 2f) : ((along - 0.5f) * 2f);
                        LeftA[k] = Colossal.Mathematics.MathUtils.Position(segment.m_Left, t);
                        RightA[k] = Colossal.Mathematics.MathUtils.Position(segment.m_Right, t);
                    }
                }
            }
        }

        /// <summary>The per-piece tiling cache, valid for one rip.（逐部件鋪排快取，於單次擷取內有效。）</summary>
        private readonly Dictionary<MeshAsset, PieceTiling> m_Tiling = new();

        private PieceTiling GetTiling(MeshAsset source)
        {
            if (!m_Tiling.TryGetValue(source, out PieceTiling tiling))
            {
                tiling = PieceTiling.Build(source);
                m_Tiling[source] = tiling;
            }

            return tiling;
        }

        /// <summary>The per-piece half-cut cache, valid for one rip; keyed by piece and side.
        /// （逐部件半裁切快取，於單次擷取內有效；以部件與側別為鍵。）</summary>
        private readonly Dictionary<(MeshAsset, bool), MeshAsset> m_FrontHalf = new();

        /// <summary>
        /// A straight corner rail — the game's <c>BuildCurve</c>, verbatim: a cubic whose control
        /// points sit evenly along one direction, normalised over the ground plane with its climb
        /// clamped.（筆直的轉角軌——即遊戲的 <c>BuildCurve</c>：控制點沿單一方向均勻分布的三次曲線，
        /// 於地平面正規化並限制其爬升。）
        /// </summary>
        /// <param name="start">The rail's start.（軌的起點。）</param>
        /// <param name="direction">The rail's direction.（軌的方向。）</param>
        /// <param name="length">The rail's length.（軌的長度。）</param>
        /// <returns>The rail bezier.（軌的貝茲曲線。）</returns>
        private static Bezier4x3 BuildRail(float3 start, float3 direction, float length)
        {
            direction = Colossal.Mathematics.MathUtils.Normalize(direction, direction.xz);
            direction.y = math.clamp(direction.y, -1f, 1f);
            return new Bezier4x3(
                start,
                start + (direction * (length * (1f / 3f))),
                start + (direction * (length * (2f / 3f))),
                start + (direction * length));
        }

        /// <summary>
        /// The node-side half of a one-off piece — the game's own selection for what a flare draws.
        /// （一次性部件靠節點的那一半——遊戲自身對擴張部繪製內容的取捨。）<br/><br/>
        /// <c>GenerateCompositionMesh</c> keeps a triangle at a node only when all three of its
        /// vertices sit at authored <c>z ≥ −0.01</c>, so an end cap contributes its rounded front
        /// half and nothing else. Bending the whole cap instead, as this once did, wrapped the
        /// cap's straight back half across the flare's converged tip curves — the crossing-triangle
        /// dart that stood where a path's rounded end should have been, and the reason end caps
        /// did not line up with the run they capped.<br/>
        /// （<c>GenerateCompositionMesh</c> 於節點僅保留三個頂點皆位於製作座標 <c>z ≥ −0.01</c>
        /// 的三角形，故端蓋只貢獻其圓弧前半。若如舊版彎折整個端蓋，其筆直後半將被包覆於
        /// 擴張部匯聚的端點曲線上——即原本該是路徑圓端之處出現的交叉三角形箭鏃，
        /// 亦即端蓋與其所封閉路段無法對齊之因。）
        /// </summary>
        /// <param name="source">The full piece mesh.（完整的部件網格。）</param>
        /// <param name="front">The node-side half (<c>z ≥ −0.01</c>) when true; the far half
        /// (<c>z ≤ 0.01</c>, the game's <c>SkipBottomHalf</c> selection) when false.
        /// （為 true 時取靠節點的一半（<c>z ≥ −0.01</c>）；為 false 時取遠側的一半
        /// （<c>z ≤ 0.01</c>，即遊戲 <c>SkipBottomHalf</c> 的取捨）。）</param>
        /// <returns>The half, or the piece itself when the cut would change nothing or drop everything.（該半；若裁切不改變任何內容或將刪去全部，則為部件本身。）</returns>
        private MeshAsset GetHalf(MeshAsset source, bool front = true)
        {
            if (m_FrontHalf.TryGetValue((source, front), out MeshAsset half)) return half;

            int usable = source.Indices.Length - (source.Indices.Length % 3);
            List<MeshPrimitive> ranges = new();
            foreach (MeshPrimitive sub in source.Primitives)
            {
                if ((sub.StartIndex < 0) || (sub.IndexCount < 3)) continue;
                if (sub.StartIndex + sub.IndexCount > usable) continue;
                ranges.Add(sub);
            }

            if (ranges.Count == 0)
            {
                ranges.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = usable,
                    MaterialIndex = 0,
                    MaterialId = $"{source.AssetId}_mat0"
                });
            }

            int[] remap = new int[source.Vertices.Length];
            for (int i = 0; i < remap.Length; i++) remap[i] = -1;

            List<float3> vertices = new();
            List<float2> uvs = new();
            List<int> indices = new();
            List<MeshPrimitive> primitives = new();

            foreach (MeshPrimitive sub in ranges)
            {
                int startIndex = indices.Count;
                int startVertex = vertices.Count;
                int end = sub.StartIndex + sub.IndexCount;

                for (int i = sub.StartIndex; (i + 2) < end; i += 3)
                {
                    int a = source.Indices[i], b = source.Indices[i + 1], c = source.Indices[i + 2];
                    bool kept = front
                        ? ((source.Vertices[a].z >= -0.01f) && (source.Vertices[b].z >= -0.01f)
                           && (source.Vertices[c].z >= -0.01f))
                        : ((source.Vertices[a].z <= 0.01f) && (source.Vertices[b].z <= 0.01f)
                           && (source.Vertices[c].z <= 0.01f));
                    if (!kept) continue;

                    foreach (int v in new[] { a, b, c })
                    {
                        if (remap[v] < 0)
                        {
                            remap[v] = vertices.Count;
                            vertices.Add(source.Vertices[v]);
                            uvs.Add((source.Uvs != null) ? source.Uvs[v] : default);
                        }
                        indices.Add(remap[v]);
                    }
                }

                if (indices.Count == startIndex) continue;

                primitives.Add(new MeshPrimitive
                {
                    StartIndex = startIndex,
                    IndexCount = indices.Count - startIndex,
                    StartVertex = startVertex,
                    VertexCount = vertices.Count - startVertex,
                    MaterialIndex = sub.MaterialIndex,
                    MaterialId = sub.MaterialId
                });
            }

            // A cut that dropped everything, or dropped nothing, is no cut: hand back the piece.
            // （若裁切刪去全部或未刪去任何內容，即等同未裁切：交回部件本身。）
            half = ((vertices.Count == 0) || (vertices.Count == source.Vertices.Length))
                ? source
                : new MeshAsset
                {
                    AssetId = source.AssetId,
                    SourceName = source.SourceName,
                    Vertices = vertices.ToArray(),
                    Uvs = uvs.ToArray(),
                    Indices = indices.ToArray(),
                    Primitives = primitives
                };

            m_FrontHalf[(source, front)] = half;
            return half;
        }

        /// <summary>
        /// The across-width vertex remap CS2 applies to a junction flare.<br/>
        /// （CS2 施加於路口擴張部的橫向頂點重映射。）<br/><br/>
        /// A composition names four positions across each half of its cross-section
        /// (<c>NetCompositionData.m_SyncVertexOffsets*</c>), and a node geometry names where those
        /// four have to end up in this particular junction
        /// (<c>EdgeNodeGeometry.m_SyncVertexTargets*</c>). The game hands both to the vertex shader
        /// as <c>m_CompositionSync0..3</c> and lets it move each vertex across the width to match —
        /// which is how a lane edge still lines up with the lane it meets when four roads of
        /// different widths share a junction.<br/>
        /// （組成為其橫斷面每一半指名四個橫向位置，節點幾何則指名這四者在此特定路口應落於何處。
        /// 遊戲將兩者作為 <c>m_CompositionSync0..3</c> 交予頂點著色器，使其橫向移動各頂點以吻合——
        /// 此即四條不同寬度的道路共用一個路口時，車道邊界仍能對齊其所接車道之由。）<br/><br/>
        /// The knots are measured per half, from the outer edge to the median, which is the
        /// coordinate the assembly's own <c>s</c> already runs in — <c>CalculateSyncVertexOffsets</c>
        /// normalises by <c>(x - middleOffset) / (width / 2)</c>, and every real composition
        /// measured has <c>middleOffset</c> zero. Both outermost knots are fixed points in the
        /// measured data, so this moves lane boundaries within a flare and never its outer kerb or
        /// its median: it is not what closes a junction, it is what stops the pieces inside one
        /// from stepping.<br/>
        /// （節點以每一半為單位量測，自外緣至中線，此即組裝所用 <c>s</c> 所在的座標系。
        /// 實測資料中最外側的兩個節點皆為不動點，故此重映射僅移動擴張部內的車道邊界，
        /// 絕不動及其外側緣石或中線：它並非用以閉合路口，而是使路口內的部件不再錯位。）<br/><br/>
        /// The interpolation between knots is the shader's, and CS2 ships shaders compiled inside
        /// asset bundles, so it is read here as piecewise linear — the reading the measured knots
        /// support and the only one that keeps every knot exactly on its target. Outside the
        /// outermost knots the shift is held constant rather than extrapolated, so a composition
        /// whose knots do not span the full half cannot throw a vertex past the kerb.<br/>
        /// （節點之間的內插屬於著色器，而 CS2 的著色器編譯於資產包內，故此處以分段線性解讀——
        /// 此為實測節點所支持、且是唯一能使每個節點精確落於其目標的解讀。
        /// 於最外側節點之外，位移保持定值而不外推，故節點未涵蓋整個半幅的組成不會將頂點拋出緣石之外。）
        /// </summary>
        private readonly struct VertexSync
        {
            private readonly float4 m_OffsetsLeft, m_TargetsLeft, m_OffsetsRight, m_TargetsRight;
            private readonly bool m_Active;

            /// <summary>No remap — what an edge, and any composition without sync data, gets.
            /// （不重映射，邊與任何無同步資料的組成皆適用。）</summary>
            public static VertexSync None => default;

            public VertexSync(NetCompositionData composition, EdgeNodeGeometry node)
            {
                m_OffsetsLeft = composition.m_SyncVertexOffsetsLeft;
                m_TargetsLeft = node.m_SyncVertexTargetsLeft;
                m_OffsetsRight = composition.m_SyncVertexOffsetsRight;
                m_TargetsRight = node.m_SyncVertexTargetsRight;

                // An unset target arrives as NaN, and one NaN would propagate into every vertex of
                // the flare. Where the two agree there is nothing to do anyway, so treat that as
                // inactive too and keep the cost off the ordinary node.
                // （未設定的目標值為 NaN，而單一個 NaN 會擴散至擴張部的每個頂點。
                //   兩者相同時本就無事可做，故亦視為未啟用，使一般節點免除此開銷。）
                m_Active = !math.any(math.isnan(m_OffsetsLeft)) && !math.any(math.isnan(m_TargetsLeft))
                        && !math.any(math.isnan(m_OffsetsRight)) && !math.any(math.isnan(m_TargetsRight))
                        && (math.any(m_OffsetsLeft != m_TargetsLeft)
                            || math.any(m_OffsetsRight != m_TargetsRight));
            }

            /// <summary>
            /// Move one across-half position from where the cross-section authored it to where this
            /// junction needs it.（將某橫向位置自橫斷面所製作之處，移至此路口所需之處。）
            /// </summary>
            /// <param name="s">The position across this half, 0 at the outer edge.
            /// （於此半幅的橫向位置，0 為外緣。）</param>
            /// <param name="leftHalf">Whether this is the left half.（是否為左半幅。）</param>
            /// <returns>The remapped position.（重映射後的位置。）</returns>
            public float Apply(float s, bool leftHalf)
            {
                if (!m_Active) return s;
                return leftHalf ? Remap(s, m_OffsetsLeft, m_TargetsLeft)
                                : Remap(s, m_OffsetsRight, m_TargetsRight);
            }

            private static float Remap(float s, float4 o, float4 t)
            {
                if (s <= o.x) return s + (t.x - o.x);
                if (s <= o.y) return Between(s, o.x, o.y, t.x, t.y);
                if (s <= o.z) return Between(s, o.y, o.z, t.y, t.z);
                if (s <= o.w) return Between(s, o.z, o.w, t.z, t.w);
                return s + (t.w - o.w);
            }

            // Two knots at the same offset are a collapsed interval, not a slope; snapping to the
            // far target keeps the map monotone instead of dividing by zero.
            // （兩個節點位於相同偏移量代表區間退化，而非斜率；取用遠端目標可維持映射單調，避免除以零。）
            private static float Between(float s, float o0, float o1, float t0, float t1)
                => ((o1 - o0) > 1e-6f) ? math.lerp(t0, t1, (s - o0) / (o1 - o0)) : t1;
        }

        private void AppendPart(DynamicBuffer<NetCompositionPiece> pieces, Segment half0, Segment half1,
                                bool splitAcross, float compositionWidth, float3 center, string label,
                                Dictionary<Entity, MeshAsset> cache, List<float3> vertices,
                                List<float2> uvs, List<int> indices,
                                List<MeshPrimitive> primitives, VertexSync sync, bool skipMedianSurface,
                                bool roundabout, StringBuilder report)
        {
            // The part's own length decides how many times a piece tiles. Left and right differ on
            // a curve, so the mean is what the tiling should follow — and when the two Segments
            // sit side by side across the width rather than end to end, they measure the same run
            // twice, so the mean is over all four curves instead of the sum of two halves.
            // （該部分本身的長度決定部件鋪排的次數。彎道上左右長度不同，故應依平均值鋪排；
            //   而當兩個 Segment 橫向並列而非首尾相接時，兩者量測的是同一段距離，
            //   故取四條曲線的平均值，而非兩半之和。）
            float edgeLength = splitAcross
                ? ((half0.m_Length.x + half0.m_Length.y + half1.m_Length.x + half1.m_Length.y) * 0.25f)
                : ((half0.m_Length.x + half0.m_Length.y + half1.m_Length.x + half1.m_Length.y) * 0.5f);
            if (edgeLength <= 0.01f) return;

            for (int p = 0; p < pieces.Length; p++)
            {
                NetCompositionPiece piece = pieces[p];
                if (piece.m_Piece == Entity.Null) continue;

                // At a parted junction the median surface piece is handed to the wedge bend
                // instead, exactly as the game maps it: its vertices ride the middle matrices
                // rather than the two halves. Mapping it here as well would pinch a second copy
                // onto the inner curves and z-fight the wedge from underneath.
                // （於分離的路口，中線表面部件交由楔形彎折處理，與遊戲的映射一致：
                //   其頂點依循中線矩陣而非兩半。若在此一併映射，將於內側曲線上擠出第二份副本，
                //   自下方與楔形面互相閃爍。）
                if (skipMedianSurface
                    && ((piece.m_SectionFlags & NetSectionFlags.Median) != 0)
                    && ((piece.m_PieceFlags & NetPieceFlags.Surface) != 0)) continue;

                // Cache by piece prefab. A city has thousands of edges but a few hundred distinct
                // pieces, and reading one loads and releases its geometry asset - doing that per
                // edge would repeat the same expensive load tens of thousands of times. The name
                // lookup belongs inside the miss for the same reason: the mesh already carries the
                // name and its sanitised id, so asking the prefab again on every one of tens of
                // thousands of edges buys nothing.
                // （依部件預製模板快取。城市有數千條邊，卻僅有數百種相異部件，而讀取其一需載入並
                //   釋放其幾何資產——逐邊執行將使同一次昂貴的載入重複數萬次。
                //   名稱查詢基於相同理由置於快取未命中的分支內：網格本身已帶有名稱及其淨化後的 ID，
                //   故於數萬條邊上反覆詢問預製模板毫無所得。）
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset source))
                {
                    string name = Instance.Name.GetPrefabName(piece.m_Piece);
                    source = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(name), name);
                    cache[piece.m_Piece] = source;
                }

                if ((source == null) || (source.Vertices.Length == 0)) continue;

                PieceTiling tiling = GetTiling(source);
                float tileLength = tiling.TileLength;

                // A piece flagged `DisableTiling` is a one-off — an end cap or a transition, not a
                // repeating section — so tiling it fills the whole run with stacked copies of a
                // cap. Measured on a real city: one elevated road came out at 393,235 triangles,
                // 388,000 of which were three "Ending" pieces repeated down its length, where an
                // ordinary drive piece costs 768. Half of a 1.4 GiB network layer sat in 2% of its
                // files for this reason.
                // （標記 `DisableTiling` 的部件是一次性的——端蓋或轉換段，而非可重複的區段——
                //   故將其鋪排會使整段路填滿層疊的端蓋副本。實測於真實城市：
                //   一條高架道路產生 393,235 個三角形，其中 388,000 個來自沿其長度重複的三個
                //   「Ending」部件，而一般車道部件僅需 768 個。）
                bool tiled = (piece.m_PieceFlags & NetPieceFlags.DisableTiling) == 0;
                int tiles = tiled ? math.max(1, (int)math.round(edgeLength / tileLength)) : 1;

                // A `PreserveShape` piece at a node does not bend onto the flare at all: it rides
                // the two straight corner rails (matrices 6/7) — `BuildCurve` from the flare's
                // edge-side corners along their start tangents, length = composition width — and
                // stands at the rails' start at its authored length. That is how a crosswalk sits
                // straight across a junction entry however the flare curves.
                // （節點上的 `PreserveShape` 部件完全不彎折至擴張部之上：它依循兩條筆直的轉角軌
                //   （矩陣 6/7）——自擴張部靠邊的兩角沿其起始切線 `BuildCurve`，長度為組成寬度——
                //   並以其製作長度立於軌的起點。此即無論擴張部如何彎曲，
                //   斑馬線都筆直橫置於路口入口之由。）
                bool preserveShape = (piece.m_PieceFlags & NetPieceFlags.PreserveShape) != 0;
                bool rails = splitAcross && preserveShape;
                Bezier4x3 rail6 = default, rail7 = default;
                if (rails)
                {
                    tiles = 1;
                    _railPieces++;

                    // At a roundabout node the game builds both rails from the *left* half's two
                    // corners — the approach's own edge — rather than from the outer corners of
                    // both halves.（於環形交叉口節點，遊戲以*左*半幅的兩角建構兩條軌——
                    // 即進入端自身的邊緣——而非兩半幅的外側角。）
                    if (roundabout)
                    {
                        rail6 = BuildRail(half0.m_Left.a,
                                          Colossal.Mathematics.MathUtils.StartTangent(half0.m_Left), compositionWidth);
                        rail7 = BuildRail(half0.m_Right.a,
                                          Colossal.Mathematics.MathUtils.StartTangent(half0.m_Right), compositionWidth);
                    }
                    else
                    {
                        rail6 = BuildRail(half0.m_Left.a,
                                          Colossal.Mathematics.MathUtils.StartTangent(half0.m_Left), compositionWidth);
                        rail7 = BuildRail(half1.m_Right.a,
                                          Colossal.Mathematics.MathUtils.StartTangent(half1.m_Right), compositionWidth);
                    }
                }

                // At a node, a one-off piece contributes only its node-side half, exactly as the
                // game selects it — see <see cref="GetHalf"/>; the game's halving rule exempts
                // `PreserveShape`. An end cap's rounded half then spans the flare and its tip
                // lands on the node end, instead of the whole cap being wrapped around the
                // flare's converged curves.
                // （於節點，一次性部件僅貢獻其靠節點的一半，與遊戲的取捨相同——詳見
                //   <see cref="GetHalf"/>；遊戲的裁半規則豁免 `PreserveShape`。
                //   端蓋的圓弧半因而橫跨擴張部、其端點落於節點端。）
                bool nodeCut = splitAcross && !tiled && !preserveShape;
                if (nodeCut)
                {
                    source = GetHalf(source);
                    tiling = GetTiling(source);
                    tileLength = tiling.TileLength;
                }

                // A `HalfLength` section is the same cut everywhere: the game drops the piece's
                // back half and doubles the front half's z scale, so the node-side half spans the
                // full run. Substituted after `tiles` was counted from the full tile length, which
                // is what reproduces that stretch — the half's 0..1 span covers a full-length tile.
                // （`HalfLength` 區段於任何位置皆為同一種裁切：遊戲捨棄部件後半並將前半的 z 尺度
                //   加倍，使靠節點的一半橫跨整段。於以完整鋪排長度計得 `tiles` 之後才替換，
                //   正是重現該延展之法——前半的 0..1 跨度覆蓋一個完整長度的鋪排單元。）
                bool halfLength = !rails && ((piece.m_SectionFlags & NetSectionFlags.HalfLength) != 0);
                if (halfLength)
                {
                    source = GetHalf(source);
                    tiling = GetTiling(source);
                }

                // `SkipBottomHalf` keeps the far half instead (`z ≤ 0.01`). On an edge the game
                // maps that half onto the start Segment only — the piece covers the first half of
                // the run and leaves the rest to whatever adjoins; at a node it spans the flare
                // like anything else.
                // （`SkipBottomHalf` 反而保留遠側的一半（`z ≤ 0.01`）。於邊上，遊戲僅將該半
                //   映射至起始 Segment——部件覆蓋前半段，其餘留給相鄰者；於節點則如常橫跨擴張部。）
                float alongScale = 1f;
                if (!rails && !halfLength && !nodeCut
                    && ((piece.m_PieceFlags & NetPieceFlags.SkipBottomHalf) != 0))
                {
                    source = GetHalf(source, false);
                    tiling = GetTiling(source);
                    _skipBottomPieces++;
                    if (!splitAcross)
                    {
                        tiles = 1;
                        alongScale = 0.5f;
                    }
                }

                // A piece authored at a different width than the slot it fills is scaled across,
                // which is how one shoulder mesh serves several road widths.
                // （若部件製作寬度與其所填槽位不同，則橫向縮放，此即同一路肩網格可服務多種路寬之由。）
                float authoredWidth = EntityManager.HasComponent<NetPieceData>(piece.m_Piece)
                    ? EntityManager.GetComponentData<NetPieceData>(piece.m_Piece).m_Width : 0f;
                float widthScale = ((authoredWidth > 1e-3f) && (piece.m_Size.x > 1e-3f)) ? (piece.m_Size.x / authoredWidth) : 1f;

                // CS2 authors one side of a road and mirrors it for the other, marking the mirrored
                // sections `Invert`. It matters wherever a piece is `AsymmetricMeshX` — a sidewalk
                // with a parking bay has its kerb on one side only, so an unmirrored copy puts the
                // bay against the carriageway and the kerb against the buildings. Mirroring a
                // symmetric piece changes nothing, so the section flag alone decides.
                // （CS2 只製作道路的一側，另一側則以鏡射產生，並將鏡射的區段標記為 `Invert`。
                //   此點在部件具 `AsymmetricMeshX` 時尤其重要：帶停車彎的人行道僅單側有緣石，
                //   未經鏡射的複本會使停車彎緊靠車道而緣石朝向建築。
                //   鏡射對稱的部件不會有任何改變，故僅由區段旗標決定。）
                bool invert = (piece.m_SectionFlags & NetSectionFlags.Invert) != 0;

                // `FlipMesh` is the same idea along the length: the far carriageway of a two-way
                // road runs the other direction, so its sections are reversed end for end. The
                // game applies it as a z scale of −1 (`GenerateCompositionMesh`'s vertex scale
                // select) and — critically — reverses triangle winding on `Invert` XOR `FlipMesh`,
                // because each mirror flips handedness and two mirrors restore it. Reversing on
                // `Invert` alone, as this once did, turned every `FlipMesh` piece inside out: its
                // faces wound downward, the rebuilt normals followed, and one side of the road lit
                // from below — visibly darker than the other with a smooth blend between.
                // （`FlipMesh` 是同一概念沿長度方向的版本：雙向道路的對向車道行進方向相反，
                //   其區段因此首尾顛倒。遊戲以 z 尺度 −1 施加之，且——關鍵地——依
                //   `Invert` XOR `FlipMesh` 反轉三角形繞向，因為每次鏡射翻轉手性，兩次鏡射則復原。
                //   先前僅依 `Invert` 反轉，使每個 `FlipMesh` 部件裡外顛倒：其面朝下，
                //   重建的法線隨之朝下，道路的一側因而由下方受光——明顯暗於另一側。）
                bool flipMesh = (piece.m_SectionFlags & NetSectionFlags.FlipMesh) != 0;

                int startVertex = vertices.Count;

                for (int tile = 0; tile < tiles; tile++)
                {
                    if (rails)
                    {
                        tiling.EvaluateRails(rail6, rail7, piece.m_Size.z, compositionWidth, flipMesh);
                    }
                    else
                    {
                        tiling.Evaluate(tile, tiles, splitAcross, half0, half1, flipMesh, alongScale);
                    }

                    for (int i = 0; i < source.Vertices.Length; i++)
                    {
                        float3 local = source.Vertices[i];
                        int slot = tiling.Slot[i];

                        float across = piece.m_Offset.x + ((invert ? -local.x : local.x) * widthScale);
                        float u = math.saturate((across + (compositionWidth * 0.5f)) / compositionWidth);

                        // The two Segments split the part in two, but along different axes, which
                        // the game's identical matrix slots hide. An edge's `m_Start`/`m_End` are
                        // halves **along the length**, each spanning the full width. A node's
                        // `m_Left`/`m_Right` are halves **across the width**, split at the median
                        // — which is why a node's piece list has a median piece in the centre and
                        // an edge's does not. Treating a node like an edge lerps from the outer
                        // kerb only as far as the centreline, and the flare comes out exactly half
                        // the road's width.
                        // （兩個 Segment 皆將該部分一分為二，但分割軸不同，而遊戲中相同的矩陣槽位
                        //   掩蓋了這點。邊的 `m_Start`/`m_End` 是**沿長度**的兩半，各自橫跨全寬；
                        //   節點的 `m_Left`/`m_Right` 則是**橫跨寬度**的兩半，於中央分隔帶處分開——
                        //   此即節點的部件清單中央有分隔帶部件而邊沒有的緣故。
                        //   若將節點當作邊處理，橫向內插只會由外側緣石延伸至中線，
                        //   擴張部因而恰好只有道路的一半寬。）
                        float s;
                        float3 left, right;

                        if (splitAcross && !rails)
                        {
                            bool leftHalf = u < 0.5f;
                            s = leftHalf ? (u * 2f) : ((u - 0.5f) * 2f);

                            // Where the cross-section sits across a flare is not where it sits
                            // across the edge: a junction stretches some lanes and squeezes others
                            // so that lane edges still meet what they meet on the far side. The
                            // remap is the game's own, and `s` is already in its coordinate — see
                            // <see cref="VertexSync"/>.
                            // （橫斷面於擴張部的橫向位置與其於邊上的位置並不相同：路口會拉伸某些
                            //   車道並壓縮另一些，使車道邊界仍能與對側所接之物對齊。
                            //   此重映射取自遊戲本身，且 `s` 已位於其座標系中。）
                            s = sync.Apply(s, leftHalf);

                            left = leftHalf ? tiling.LeftA[slot] : tiling.LeftB[slot];
                            right = leftHalf ? tiling.RightA[slot] : tiling.RightB[slot];
                        }
                        else
                        {
                            s = u;
                            left = tiling.LeftA[slot];
                            right = tiling.RightA[slot];
                        }

                        float3 surface = math.lerp(left, right, s);

                        vertices.Add(surface - center + new float3(0f, local.y + piece.m_Offset.y, 0f));

                        // V accumulates across tiles rather than repeating — see PieceTiling.VSpan.
                        // （V 於鋪排單元間累積而非重複——詳見 PieceTiling.VSpan。）
                        float2 uv = (source.Uvs != null) ? source.Uvs[i] : default;
                        if (tiled && (uv.y >= 0f)) uv.y += tile * tiling.VSpan;
                        uvs.Add(uv);
                    }
                }

                int emitted = AppendPieceIndices(source, startVertex, tiles, invert ^ flipMesh, indices, primitives);

                report?.AppendLine($"  {label} [{p}] '{source.SourceName}': {tiles} tile(s) of {tileLength:F1} m "
                                + $"over {edgeLength:F1} m, widthScale {widthScale:F3}, {emitted} sub-mesh(es)");
            }
        }

        /// <summary>
        /// Emit one primitive per sub-mesh of a bent piece, rather than one per piece.
        /// （為彎折後的部件逐子網格產生一個 primitive，而非整個部件僅產生一個。）<br/><br/>
        /// <b>Why, and how much it is worth.</b> A net piece's mesh carries a <c>MeshMaterial</c>
        /// range per sub-mesh, and the game's own assembly
        /// (<c>BatchMeshHelpers.GenerateBatchMeshJob.GenerateCompositionMesh</c>) loops that buffer
        /// and resolves a separate material for every entry. Collapsing a piece to one primitive
        /// under sub-mesh 0's material would therefore drop a material wherever a piece has more
        /// than one, and draw that geometry's authored UVs out of the wrong atlas.<br/><br/>
        /// <b>Measured, no piece in this city does.</b> Over 7,380 segment meshes the per-mesh
        /// material histogram is byte-identical before and after this change, so every net piece
        /// vanilla ships has exactly one sub-mesh. This is correctness against the game's own
        /// loop and insurance against modded pieces, not a fix for anything observed — do not cite
        /// it as the cause of a shading defect.<br/>
        /// （<b>理由及其價值。</b>路網部件的網格為每個子網格各帶一段 <c>MeshMaterial</c> 範圍，
        /// 而遊戲自身的組裝流程會逐一走訪該緩衝區並各自解析材質。
        /// 若將部件併為單一 primitive，凡具多個子網格者皆會遺失材質。
        /// <b>然而實測顯示本城市中並無此類部件</b>：7,380 個路段網格的材質直方圖於變更前後
        /// 逐位元組相同。此變更是對照遊戲自身迴圈的正確性保障與對模組部件的預防，
        /// 而非針對任何已觀察到之缺陷的修正——切勿將其列為著色缺陷的成因。）<br/><br/>
        /// The vertices are already laid down, one contiguous copy per tile, so each sub-mesh's
        /// index range is replayed across every tile and the whole run becomes one primitive.
        /// A piece whose mesh declares no sub-mesh at all keeps the previous behaviour: the entire
        /// index buffer under <c>&lt;assetId&gt;_mat0</c>.<br/>
        /// （頂點已逐鋪排副本連續寫入，故將各子網格的索引範圍於每個鋪排上重播，
        /// 整段合為一個 primitive。若網格未宣告任何子網格，則維持原行為。）
        /// </summary>
        /// <param name="source">The piece mesh the vertices were copied from.（頂點所複製自的部件網格。）</param>
        /// <param name="startVertex">Where this piece's first tile began.（此部件首個鋪排的起點。）</param>
        /// <param name="tiles">How many copies were laid down.（已寫入的副本數。）</param>
        /// <param name="reverse">Whether the piece was mirrored an odd number of times — `Invert`
        /// XOR `FlipMesh`, the game's own winding rule.（部件是否經奇數次鏡射——即 `Invert` XOR
        /// `FlipMesh`，遊戲自身的繞向規則。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <returns>How many primitives were added.（新增的 primitive 數量。）</returns>
        private static int AppendPieceIndices(MeshAsset source, int startVertex, int tiles, bool reverse,
                                              List<int> indices, List<MeshPrimitive> primitives)
        {
            int stride = source.Vertices.Length;
            int usable = source.Indices.Length - (source.Indices.Length % 3);

            // Only ranges that actually address this index buffer. A sub-mesh table that disagrees
            // with the buffer it describes would otherwise walk off the end of it.
            // （僅採用確實落在此索引緩衝區內的範圍，否則與緩衝區不符的子網格表將越界讀取。）
            List<MeshPrimitive> subs = new();
            foreach (MeshPrimitive sub in source.Primitives)
            {
                if ((sub.StartIndex < 0) || (sub.IndexCount < 3)) continue;
                if (sub.StartIndex + sub.IndexCount > usable) continue;
                subs.Add(sub);
            }

            if (subs.Count == 0)
            {
                subs.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = usable,
                    MaterialIndex = 0,
                    MaterialId = $"{source.AssetId}_mat0"
                });
            }

            int added = 0;
            foreach (MeshPrimitive sub in subs)
            {
                int startIndex = indices.Count;
                int end = sub.StartIndex + sub.IndexCount;

                for (int tile = 0; tile < tiles; tile++)
                {
                    int baseVertex = startVertex + (tile * stride);

                    // A mirror reverses handedness, so a piece mirrored an odd number of times has
                    // its triangles wound the other way or it ends up inside out — with normals
                    // rebuilt from the faces, that would light the whole surface from underneath.
                    // （鏡射會反轉手性，故經奇數次鏡射的部件其三角形必須改變纏繞方向，
                    //   否則將裡外顛倒；由於法線是自面重建，如此會使整個表面由下方受光。）
                    for (int i = sub.StartIndex; (i + 2) < end; i += 3)
                    {
                        indices.Add(baseVertex + source.Indices[i]);
                        indices.Add(baseVertex + source.Indices[reverse ? (i + 2) : (i + 1)]);
                        indices.Add(baseVertex + source.Indices[reverse ? (i + 1) : (i + 2)]);
                    }
                }

                if (indices.Count == startIndex) continue;

                primitives.Add(new MeshPrimitive
                {
                    StartIndex = startIndex,
                    IndexCount = indices.Count - startIndex,
                    StartVertex = startVertex,
                    VertexCount = tiles * stride,
                    MaterialIndex = primitives.Count,
                    MaterialId = !string.IsNullOrEmpty(sub.MaterialId)
                        ? sub.MaterialId
                        : $"{source.AssetId}_mat{sub.MaterialIndex}"
                });

                added++;
            }

            return added;
        }

        /// <summary>
        /// Bend the pieces of one composition onto a geometry, appending to a shared mesh.
        /// （將某組成的部件彎折至某幾何之上，並附加至共用的網格。）<br/><br/>
        /// A composition entity does not hold the piece list itself — <c>NetCompositionMeshRef</c>
        /// points at the entity that does.<br/>
        /// （組成實體本身不持有部件清單，<c>NetCompositionMeshRef</c> 指向持有它的實體。）
        /// </summary>
        /// <param name="composition">The composition entity.（組成實體。）</param>
        /// <param name="half0">The first half's boundary curves.（第一半的邊界曲線。）</param>
        /// <param name="half1">The second half's boundary curves.（第二半的邊界曲線。）</param>
        /// <param name="center">The origin the result is relative to.（結果所依據的原點。）</param>
        /// <param name="label">What to call this part in the report.（此部分於報告中的名稱。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        private void AppendComposition(Entity composition, Segment half0, Segment half1, bool splitAcross,
                                       float3 center, string label, Dictionary<Entity, MeshAsset> cache,
                                       List<float3> vertices, List<float2> uvs,
                                       List<int> indices, List<MeshPrimitive> primitives,
                                       EdgeNodeGeometry? node, StringBuilder report)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return;

            NetCompositionMeshRef meshRef = EntityManager.GetComponentData<NetCompositionMeshRef>(composition);
            Entity meshEntity = meshRef.m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return;

            if (!EntityManager.HasComponent<NetCompositionData>(composition)) return;
            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            float width = data.m_Width;
            if (!(width > 0f)) return;

            // A composition whose cross-section is another's mirror image does not get its own
            // baked mesh — `NetCompositionMeshRefSystem` points it at the existing one with
            // `m_Rotate` set, and the game draws the edge as `RotatedEdge`:
            // `CalculateEdgeParameters(isRotated: true)` builds the matrices from the *inverted*
            // curves with start↔end and left↔right both swapped, a 180° turn of the whole
            // assembly. Ignoring the flag, as this once did, mirrored the cross-section of every
            // such edge — an asymmetric road's wide side on the wrong side.
            // （橫斷面為他者鏡像的組成不會獲得自己的烘焙網格——`NetCompositionMeshRefSystem`
            //   使其指向既有網格並設定 `m_Rotate`，遊戲便以 `RotatedEdge` 繪製該邊：
            //   `CalculateEdgeParameters(isRotated: true)` 以*反轉*的曲線建構矩陣，
            //   且首尾與左右皆互換——整個組裝旋轉 180°。忽略此旗標（如舊版）
            //   會使每條此類邊的橫斷面鏡像——不對稱道路的寬側落在錯誤的一側。）
            if (meshRef.m_Rotate)
            {
                if (splitAcross)
                {
                    // The game routes a rotated *node* composition through the edge parameters,
                    // which cannot be meant literally for a flare; seen ones are counted rather
                    // than guessed at.
                    // （遊戲將旋轉的*節點*組成導入邊的參數，對擴張部而言不可能是字面之意；
                    //   遇見者僅計數，不作臆測。）
                    _rotatedNodeParts++;
                }
                else
                {
                    Segment start = half0, end = half1;
                    half0 = new Segment
                    {
                        m_Left = Colossal.Mathematics.MathUtils.Invert(end.m_Right),
                        m_Right = Colossal.Mathematics.MathUtils.Invert(end.m_Left),
                        m_Length = end.m_Length.yx
                    };
                    half1 = new Segment
                    {
                        m_Left = Colossal.Mathematics.MathUtils.Invert(start.m_Right),
                        m_Right = Colossal.Mathematics.MathUtils.Invert(start.m_Left),
                        m_Length = start.m_Length.yx
                    };
                    _rotatedEdges++;
                }
            }

            // Roundabout compositions use a mapping of their own — the middle-radius branch of
            // `CalculateNodeParameters` and a distinct vertex path — that is not reproduced yet.
            // They are assembled with the ordinary node mapping and counted, so a city that has
            // any says so in the report.
            // （環形交叉口組成使用自成一格的映射——`CalculateNodeParameters` 的中間半徑分支與
            //   獨立的頂點路徑——尚未重現。目前以一般節點映射組裝並計數，
            //   使含有環形交叉口的城市能於報告中顯明。）
            if ((data.m_Flags.m_General & CompositionFlags.General.Roundabout) != 0) _roundaboutParts++;

            DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);

            // Only a node carries sync targets; an edge's cross-section is where it was authored.
            // （唯有節點帶有同步目標；邊的橫斷面即其製作時所在之處。）
            VertexSync sync = node.HasValue ? new VertexSync(data, node.Value) : VertexSync.None;

            // Only a node splits across the width, and only a node has a wedge between its halves.
            // Whether one exists is decided here, before the halves are assembled, because it
            // changes what the halves contain: a parted median surface piece belongs to the wedge
            // bend and must not also be pinched onto the inner curves.
            // （唯有節點橫向分割，也唯有節點的兩半之間有楔形。楔形是否存在於此、
            //   於兩半組裝之前判定，因為它改變兩半的內容：分離的中線表面部件屬於楔形彎折，
            //   不可再被擠壓至內側曲線之上。）
            float gap = 0f;
            bool wedge = false;

            if (splitAcross && node.HasValue)
            {
                gap = math.distance(Colossal.Mathematics.MathUtils.Position(half0.m_Right, 1f),
                                    Colossal.Mathematics.MathUtils.Position(half1.m_Left, 1f));

                if (gap > width)
                {
                    // A median cannot be wider than the road it runs down the middle of. Past that
                    // the two curves are not a median that has parted — they are carriageways that
                    // have genuinely diverged, and paving between them would claim road surface
                    // where a junction simply splits.
                    // （分隔線不可能寬於其所貫穿的道路。超過此限，兩條曲線便不是分開的分隔線，
                    //   而是真正分岔的車道；於其間鋪面等同宣稱該處有路面，然而路口只是在此分岔。）
                    report?.AppendLine($"  {label} median fill skipped: {gap:F1} m wedge exceeds the "
                                     + $"{width:F1} m composition width");
                    _seamsSkipped++;
                }
                else
                {
                    wedge = gap >= SeamThreshold;
                }
            }

            bool roundabout = (data.m_Flags.m_General & CompositionFlags.General.Roundabout) != 0;
            AppendPart(pieces, half0, half1, splitAcross, width, center, label, cache,
                       vertices, uvs, indices, primitives, sync, wedge, roundabout, report);

            if (wedge)
            {
                AppendMedianFill(pieces, half0, half1, node.Value, width, gap, center, label, cache,
                                 vertices, uvs, indices, primitives, report);
            }
        }

        /// <summary>
        /// Bridge the wedge between the two halves of a junction flare — the junction hole — by
        /// bending the composition's own median surface piece across it, the way the game does.<br/>
        /// （以組成自身的中線表面部件彎折橫跨，橋接路口擴張部兩半之間的楔形空缺——
        /// 即路口的破洞——作法與遊戲相同。）<br/><br/>
        /// <b>The defect this closes, in numbers.</b> A node's flare is two ruled halves:
        /// <c>m_Left</c> spans its own two curves and <c>m_Right</c> spans its own, and the
        /// assembly maps the cross-section across them assuming the two curves in the middle are
        /// one shared median. At an ordinary node they are — all 32,473 node geometries in a full
        /// city measured a gap under 0.01 m, byte-identical curves. At an <c>Intersection</c>
        /// composition they are not: 17,111 of 19,445 separate, by 1–2 m typically and past 8 m in
        /// 702 cases. The two halves leave the edge together and pull apart as they reach into the
        /// junction, so the gap is widest exactly at the node.<br/>
        /// （<b>所修補缺陷的數值。</b>節點的擴張部由兩片直紋曲面構成，組裝時假設中間的兩條曲線
        /// 為共用的分隔線。於一般節點確實如此——全城 32,473 份節點幾何的間隙皆小於 0.01 公尺。
        /// 於 <c>Intersection</c> 組成則否：19,445 份中有 17,111 份分離。
        /// 兩半自邊出發時相接，深入路口時漸行漸遠，故間隙恰於節點處最寬。）<br/><br/>
        /// <b>The mapping is the game's own, decompiled from <c>GenerateCompositionMesh</c>.</b>
        /// A median piece's centre column (<c>|x − offset.x| &lt; 0.01</c>, the game's own test)
        /// rides <c>EdgeNodeGeometry.m_Middle</c> — matrix 4, cut at t = 0.99 by
        /// <c>CalculateNodeParameters</c> — while every other vertex keeps its proportional
        /// position within its half's frame, the same frame the drive pieces are mapped in. That
        /// is what keeps the median's kerbs their authored width and its edges flush with the
        /// lanes either side while its centre stretches across the wedge. Two earlier versions
        /// were wrong in opposite ways: a flat constant-UV strip sampled a different atlas band
        /// than the flares and z-fought the pinched median copy underneath it, and a full-width
        /// stretch smeared the piece's cross-bands into long gradients while pushing its edges
        /// onto the inner curves, under the drive pieces.<br/>
        /// （<b>此映射取自遊戲本身，自 <c>GenerateCompositionMesh</c> 反編譯而得。</b>
        /// 中線部件的中央頂點列（<c>|x − offset.x| &lt; 0.01</c>，遊戲自身的判準）依循
        /// <c>EdgeNodeGeometry.m_Middle</c>——矩陣 4，經 <c>CalculateNodeParameters</c> 於
        /// t = 0.99 截斷——其餘頂點則於其半幅座標系中保持比例位置，與車道部件所用者相同。
        /// 分隔帶的緣石因而維持製作寬度、邊緣與兩側車道齊平，唯其中央橫跨楔形延展。
        /// 先前兩版各於相反方向出錯：恆定 UV 的平面帶狀面取樣於與擴張部不同的圖集色帶，
        /// 且與其下被擠壓的中線副本互相閃爍；全寬拉伸則將部件的橫向色帶暈成長條漸層，
        /// 並將其邊緣推至內側曲線上、疊於車道部件之下。）
        /// </summary>
        /// <param name="pieces">The composition's cross-section.（組成的橫斷面。）</param>
        /// <param name="half0">The outer half's boundary curves.（外半部的邊界曲線。）</param>
        /// <param name="half1">The inner half's boundary curves.（內半部的邊界曲線。）</param>
        /// <param name="node">The node geometry carrying the middle curve.（帶有中線曲線的節點幾何。）</param>
        /// <param name="compositionWidth">The composition's nominal width — the half-frame scale.（組成的標稱寬度，即半幅座標系的尺度。）</param>
        /// <param name="gap">The wedge width at the node, already bounded by the caller.（節點處的楔形寬度，呼叫端已檢核其上限。）</param>
        /// <param name="center">The origin the result is relative to.（結果所依據的原點。）</param>
        /// <param name="label">What to call this in the report.（此部分於報告中的名稱。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        private void AppendMedianFill(DynamicBuffer<NetCompositionPiece> pieces, Segment half0, Segment half1,
                                      EdgeNodeGeometry node, float compositionWidth, float gap, float3 center,
                                      string label, Dictionary<Entity, MeshAsset> cache,
                                      List<float3> vertices, List<float2> uvs, List<int> indices,
                                      List<MeshPrimitive> primitives, StringBuilder report)
        {
            Bezier4x3 middle = node.m_Middle;
            float halfWidth = math.max(compositionWidth * 0.5f, 1e-3f);

            bool Bend(NetCompositionPiece piece, bool median)
            {
                string pieceName = Instance.Name.GetPrefabName(piece.m_Piece);
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset source))
                {
                    source = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(pieceName), pieceName);
                    cache[piece.m_Piece] = source;
                }

                if ((source == null) || (source.Vertices.Length == 0)) return false;

                PieceTiling tiling = GetTiling(source);
                float wedgeLength = (Colossal.Mathematics.MathUtils.Length(half0.m_Right)
                                   + Colossal.Mathematics.MathUtils.Length(half1.m_Left)) * 0.5f;
                if (wedgeLength <= 0.01f) return false;

                bool tiled = (piece.m_PieceFlags & NetPieceFlags.DisableTiling) == 0;
                int tiles = tiled ? math.max(1, (int)math.round(wedgeLength / tiling.TileLength)) : 1;

                float pieceWidth = math.max(piece.m_Size.x, 1e-3f);
                float authoredWidth = EntityManager.HasComponent<NetPieceData>(piece.m_Piece)
                    ? EntityManager.GetComponentData<NetPieceData>(piece.m_Piece).m_Width : 0f;
                float widthScale = (authoredWidth > 1e-3f) ? (pieceWidth / authoredWidth) : 1f;
                bool invert = (piece.m_SectionFlags & NetSectionFlags.Invert) != 0;
                bool flipMesh = (piece.m_SectionFlags & NetSectionFlags.FlipMesh) != 0;

                // The game's median mapping needs a centre column to put on the middle curve —
                // its own test is `|x − offset.x| < 0.01`. A piece authored without one cannot be
                // mapped that way without reopening the hole, so it falls back to the stretch.
                // （遊戲的中線映射需要一列中央頂點置於中線曲線上——其自身的判準即
                //   `|x − offset.x| < 0.01`。未如此製作的部件無法採用該映射而不重開破洞，
                //   故退回整幅拉伸。）
                // A zero-width median collapses to its offset — the game's own
                // `select(vertex.x, offset.x, size.x == 0)` — so every vertex is centre column.
                // （零寬度的分隔帶收攏至其偏移——即遊戲的
                //   `select(vertex.x, offset.x, size.x == 0)`——故所有頂點皆屬中央列。）
                bool zeroWidth = piece.m_Size.x < 1e-3f;

                bool hasCentre = zeroWidth;
                for (int i = 0; (i < source.Vertices.Length) && !hasCentre; i++)
                {
                    float x = (invert ? -source.Vertices[i].x : source.Vertices[i].x) * widthScale;
                    hasCentre = math.abs(piece.m_Offset.x + x) < 0.011f;
                }

                bool halfFrame = median && hasCentre;
                int startVertex = vertices.Count;

                for (int tile = 0; tile < tiles; tile++)
                {
                    tiling.EvaluateWedge(tile, tiles, half0, half1, middle, flipMesh);

                    for (int i = 0; i < source.Vertices.Length; i++)
                    {
                        float3 local = source.Vertices[i];
                        int slot = tiling.Slot[i];
                        float dx = zeroWidth ? 0f : (piece.m_Offset.x + ((invert ? -local.x : local.x) * widthScale));
                        float3 surface;

                        if (halfFrame)
                        {
                            // The game's own mapping, from `GenerateCompositionMesh`: the centre
                            // column rides the middle curve, and every other vertex keeps its
                            // proportional position within its half's frame — the same frame the
                            // drive pieces are mapped in, which is what keeps the median's kerbs
                            // their authored width and its edges flush with the lanes either side.
                            // Stretching the piece's full width across the wedge instead smeared
                            // its cross-bands into long gradients and pushed its edges onto the
                            // inner curves, under the drive pieces' innermost strips.
                            // （遊戲自身的映射，取自 `GenerateCompositionMesh`：中央頂點列依循
                            //   中線曲線，其餘頂點於其半幅座標系中保持比例位置——與車道部件相同
                            //   的座標系，分隔帶的緣石因而維持製作寬度，其邊緣與兩側車道齊平。
                            //   若改將部件全寬拉伸橫跨楔形，其橫向色帶將暈成長條漸層，
                            //   邊緣亦被推至內側曲線上、疊於車道部件最內側之下。）
                            if (dx > 0.01f)
                            {
                                surface = math.lerp(tiling.LeftB[slot], tiling.RightB[slot],
                                                    math.saturate(dx / halfWidth));
                            }
                            else if (dx < -0.01f)
                            {
                                surface = math.lerp(tiling.RightA[slot], tiling.LeftA[slot],
                                                    math.saturate(-dx / halfWidth));
                            }
                            else
                            {
                                surface = tiling.Middle[slot];
                            }
                        }
                        else
                        {
                            // No centre column, or no median at all: stretch the piece across the
                            // wedge — inner-left through the middle curve to inner-right — so the
                            // surface stays closed with the road's own texture.
                            // （無中央頂點列、或全無分隔帶：將部件拉伸橫跨楔形——
                            //   自內左經中線曲線至內右——使表面以道路自身的貼圖保持封閉。）
                            float s = math.saturate(0.5f + (((invert ? -local.x : local.x) * widthScale) / pieceWidth));
                            surface = (s < 0.5f)
                                ? math.lerp(tiling.RightA[slot], tiling.Middle[slot], s * 2f)
                                : math.lerp(tiling.Middle[slot], tiling.LeftB[slot], (s - 0.5f) * 2f);
                        }

                        vertices.Add(surface - center + new float3(0f, local.y + piece.m_Offset.y, 0f));
                        uvs.Add((source.Uvs != null) ? source.Uvs[i] : default);
                    }
                }

                AppendPieceIndices(source, startVertex, tiles, invert ^ flipMesh, indices, primitives);

                _seamsFilled++;
                _worstSeam = math.max(_worstSeam, gap);

                report?.AppendLine($"  {label} median fill '{source.SourceName}': {gap:F2} m wedge at the node, "
                                 + $"{tiles} tile(s) over {wedgeLength:F1} m"
                                 + (halfFrame ? "" : ", stretched"));
                return true;
            }

            // The median surface piece — the same selection AppendPart excludes when a wedge is
            // being built, so the piece is emitted exactly once. A composition's median slot also
            // holds a bottom piece for elevated roads; that one keeps the half mapping, since the
            // road surface is the hole anyone can see.
            // （中線的表面部件——與 AppendPart 於楔形成立時排除者為同一選擇，故該部件恰好輸出一次。
            //   高架道路的中線槽位另有底部部件，其仍依兩半映射，因為路面才是看得見的破洞。）
            bool bent = false;
            int widest = -1;
            float widestWidth = 0f;

            for (int p = 0; p < pieces.Length; p++)
            {
                NetCompositionPiece piece = pieces[p];
                if ((piece.m_Piece == Entity.Null)
                    || ((piece.m_PieceFlags & NetPieceFlags.Surface) == 0)) continue;

                if ((piece.m_SectionFlags & NetSectionFlags.Median) != 0)
                {
                    bent |= Bend(piece, true);
                }
                else if (piece.m_Size.x > widestWidth)
                {
                    widest = p;
                    widestWidth = piece.m_Size.x;
                }
            }

            // A composition without a median — a one-way carriageway, most highways — still parts
            // at an intersection, and in the game the hole it leaves is covered by other geometry
            // this producer does not assemble (the junction's own pieces on the corner matrices).
            // Bending the widest carriageway piece across the wedge instead keeps the surface
            // closed with the road's own texture; without it, every median-less junction arm
            // showed a wedge of missing road.
            // （沒有分隔帶的組成——單向車道、多數高速公路——於路口同樣分離，遊戲中其留下的空缺
            //   由本產生器未組裝的其他幾何覆蓋（路口自身位於轉角矩陣上的部件）。
            //   改以最寬的車道部件彎折橫跨楔形，可用道路自身的貼圖保持表面封閉；
            //   若不如此，每個無分隔帶的路口臂都會缺一塊楔形路面。）
            if (!bent && (widest >= 0)) Bend(pieces[widest], false);
        }

        /// <summary>
        /// Close the middle of a junction: the polygon bounded by the node-end of every flare
        /// meeting there.<br/>
        /// （封閉路口的中央：由交會其中每片擴張部的節點端所圍成的多邊形。）<br/><br/>
        /// <b>How much of the game's own fill is reproduced, and what remains a reconstruction.</b>
        /// A node feeds the vertex shader three curves beyond its four boundaries — <c>m_Middle</c>
        /// behind composition matrices 4/5 and two side-connection curves behind 6/7. An earlier
        /// version of this comment claimed the vertex-to-matrix mapping lives in the compiled
        /// shaders and is unrecoverable; that is wrong. <c>GenerateCompositionMesh</c> — managed
        /// code — bakes the matrix indices into each vertex's colour, and reading it settled the
        /// median case: a median piece's vertices blend between the middle matrices and the inner
        /// boundaries, which <see cref="AppendMedianFill"/> now reproduces. What is still not
        /// reproduced is the corner case: side pieces near a flare's outer corners blend toward the
        /// 6/7 connection curves under flag bits whose meaning has not been decoded. The corner
        /// fillets between adjacent roads therefore still fall to this fan, and a consumer should
        /// expect them flat where CS2 may have shaped them.<br/>
        /// （<b>遊戲自身填補的重現程度，與仍屬重建的部分。</b>節點於四條邊界之外另有三條曲線送入
        /// 頂點著色器——矩陣 4/5 之後的 <c>m_Middle</c>，與矩陣 6/7 之後的兩條轉角連接曲線。
        /// 本註解舊版宣稱頂點與矩陣的映射封裝於已編譯的著色器中而無從取得；此說有誤。
        /// <c>GenerateCompositionMesh</c>——受管程式碼——將矩陣索引烘焙至各頂點的色彩中，
        /// 讀取之後已解決分隔帶的情形：中線部件的頂點於中線矩陣與內側邊界之間混合，
        /// <see cref="AppendMedianFill"/> 現已重現此映射。尚未重現者為轉角情形：
        /// 擴張部外側轉角附近的側面部件依未解碼的旗標位元混合至 6/7 連接曲線。
        /// 故相鄰道路之間的轉角填角仍由此扇形面填補，消費端應預期其為平面。）<br/><br/>
        /// <b>What is measured, and what this does about it.</b> Across a full city, 1,607 of 1,817
        /// road junctions have every flare ending more than a metre short of the node, and 316 more
        /// than four metres; other network types are the other way round, 77% of them reaching
        /// within a metre. So the gap is a property of carriageways and it is the hole. Each flare
        /// presents five points at its node end — its outer boundaries, its inner boundaries, and
        /// its middle curve at the game's own 0.99 cut. Those points, taken in bearing order around
        /// the node, bound exactly the area neither the flares nor the wedge fills cover: the
        /// middle of the junction, and the corner fillets between adjacent roads. Every vertex is a
        /// point on a curve the game computed, the fan's boundary coincides with where the
        /// assembled geometry actually ends, and the fill can never exceed the roads that bound
        /// it.<br/>
        /// （<b>已量測者為何，以及此處如何處置。</b>全城 1,817 個道路路口中有 1,607 個的所有擴張部
        /// 皆止於距節點一公尺之外，316 個超過四公尺；其他路網型別則相反，77% 可達一公尺以內。
        /// 故此間隙為車道所特有，即為該破洞。每片擴張部於其節點端呈現五個點——外側邊界、
        /// 內側邊界，及其中線曲線於遊戲自身 0.99 截斷處。這些點依繞節點的方位角排序後，
        /// 恰好圍出擴張部與楔形填補皆未覆蓋之區域：路口的中央，以及相鄰道路之間的轉角填角。
        /// 每個頂點皆為遊戲算出曲線上的一點，扇形面的邊界與組裝幾何實際結束之處重合。）
        /// </summary>
        /// <param name="entity">The edge being assembled.（正在組裝的邊。）</param>
        /// <param name="node">The node at one of its ends.（其一端的節點。）</param>
        /// <param name="composition">That end's node composition.（該端的節點組成。）</param>
        /// <param name="center">The origin the result is relative to.（結果所依據的原點。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        private void AppendJunctionFan(Entity entity, Entity node, Entity composition, float3 center,
                                       Dictionary<Entity, MeshAsset> cache, List<float3> vertices,
                                       List<float2> uvs, List<int> indices,
                                       List<MeshPrimitive> primitives, StringBuilder report)
        {
            if ((node == Entity.Null) || !EntityManager.HasComponent<Node>(node)) return;
            if (!EntityManager.HasBuffer<ConnectedEdge>(node)) return;

            DynamicBuffer<ConnectedEdge> connected = EntityManager.GetBuffer<ConnectedEdge>(node, true);

            // A bend is two edges meeting and has no middle to leave uncovered.
            // （轉折為兩邊相接，並無中央區域會未被覆蓋。）
            if (connected.Length < 3) return;

            // The junction is shared, so exactly one of the edges meeting in it emits the fill.
            // Lowest entity index wins, which is stable for a given world state and needs no
            // communication between the edges as the rip walks them one at a time.
            // （路口為共有，故僅由交會其中的一條邊產生此填補。以最小實體索引者勝出，
            //   該規則於給定的世界狀態下穩定，且擷取逐邊處理時無須邊與邊之間溝通。）
            int owner = int.MaxValue;

            for (int i = 0; i < connected.Length; i++)
            {
                Entity other = connected[i].m_Edge;
                if ((other == Entity.Null) || !EntityManager.Exists(other)) continue;
                if (!EntityManager.HasComponent<Edge>(other) || IsMarkerNetwork(other)) continue;

                Edge otherEnds = EntityManager.GetComponentData<Edge>(other);

                if (((otherEnds.m_Start == node) && EntityManager.HasComponent<StartNodeGeometry>(other))
                    || ((otherEnds.m_End == node) && EntityManager.HasComponent<EndNodeGeometry>(other)))
                {
                    owner = math.min(owner, other.Index);
                }
            }

            if (owner != entity.Index) return;
            BuildJunctionFan(node, composition, center, cache, vertices, uvs, indices, primitives, report);
        }

        /// <summary>
        /// Build the junction fill itself, with no question of which edge owns it.
        /// （建構路口填補面本身，不涉及其歸屬於哪條邊的問題。）<br/><br/>
        /// Separate from <see cref="AppendJunctionFan"/> so the coverage diagnostic can measure the
        /// same geometry the rip emits. Measuring an assembly the rip does not produce is how a
        /// diagnostic comes to disagree with the bundle.<br/>
        /// （與 <see cref="AppendJunctionFan"/> 分離，使覆蓋率診斷得以量測擷取實際輸出的同一份幾何。
        /// 量測與擷取產物不符的組裝結果，正是診斷與束產生分歧之由。）
        /// </summary>
        /// <param name="node">The junction node.（路口節點。）</param>
        /// <param name="composition">A node composition to take height and material from.（用以取得高度與材質的節點組成。）</param>
        /// <param name="center">The origin the result is relative to.（結果所依據的原點。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        /// <returns>Whether anything was built.（是否建構出任何幾何。）</returns>
        private bool BuildJunctionFan(Entity node, Entity composition, float3 center,
                                      Dictionary<Entity, MeshAsset> cache, List<float3> vertices,
                                      List<float2> uvs, List<int> indices,
                                      List<MeshPrimitive> primitives, StringBuilder report)
        {
            if ((node == Entity.Null) || !EntityManager.HasComponent<Node>(node)) return false;
            if (!EntityManager.HasBuffer<ConnectedEdge>(node)) return false;

            DynamicBuffer<ConnectedEdge> connected = EntityManager.GetBuffer<ConnectedEdge>(node, true);
            if (connected.Length < 3) return false;

            float3 centre = EntityManager.GetComponentData<Node>(node).m_Position;
            List<List<float3>> flares = new();

            for (int i = 0; i < connected.Length; i++)
            {
                Entity other = connected[i].m_Edge;
                if ((other == Entity.Null) || !EntityManager.Exists(other)) continue;
                if (!EntityManager.HasComponent<Edge>(other) || IsMarkerNetwork(other)) continue;

                Edge otherEnds = EntityManager.GetComponentData<Edge>(other);
                EdgeNodeGeometry geometry;

                if ((otherEnds.m_Start == node) && EntityManager.HasComponent<StartNodeGeometry>(other))
                {
                    geometry = EntityManager.GetComponentData<StartNodeGeometry>(other).m_Geometry;
                }
                else if ((otherEnds.m_End == node) && EntityManager.HasComponent<EndNodeGeometry>(other))
                {
                    geometry = EntityManager.GetComponentData<EndNodeGeometry>(other).m_Geometry;
                }
                else continue;

                // t=1 is the node end; every curve runs edge-to-node. Five points per flare, not
                // two: the outer boundaries, the inner boundaries, and the middle curve at the
                // 0.99 cut the game itself uses. The assembled flare and its wedge fill end at
                // exactly these positions, so the fan shares its boundary with the geometry it
                // abuts instead of cutting kerb-to-kerb chords across it — the chords are what
                // used to overlap the wedge fills and z-fight them.
                // （t=1 為節點端；所有曲線皆自邊延伸至節點。每片擴張部取五點而非兩點：
                //   外側邊界、內側邊界，以及遊戲自身採用的 0.99 截斷處之中線曲線。
                //   組裝後的擴張部及其楔形填補恰止於這些位置，故扇形面與其鄰接的幾何共用邊界，
                //   而非以緣石至緣石的弦橫切其上——正是那些弦先前覆蓋楔形填補並與之閃爍。）
                List<float3> flare = new(5)
                {
                    Colossal.Mathematics.MathUtils.Position(geometry.m_Left.m_Left, 1f),
                    Colossal.Mathematics.MathUtils.Position(geometry.m_Left.m_Right, 1f),
                    Colossal.Mathematics.MathUtils.Position(geometry.m_Right.m_Left, 1f),
                    Colossal.Mathematics.MathUtils.Position(geometry.m_Right.m_Right, 1f)
                };

                Bezier4x3 middle = geometry.m_Middle;
                if (!math.any(math.isnan(middle.a)) && !math.any(math.isnan(middle.d)))
                {
                    flare.Add(Colossal.Mathematics.MathUtils.Position(middle, 0.99f));
                }

                // A middle curve can end within centimetres of the node itself, and the bearing
                // of a sub-5 cm offset is numerical noise. A point that close to the hub bounds
                // nothing the hub's own triangles do not already cover, so it is dropped.
                // （中線曲線可能止於距節點僅數公分之處，而不足五公分之偏移的方位角只是數值雜訊。
                //   如此貼近中心點者，其所圍之處中心點自身的三角形本已覆蓋，故捨棄。）
                flare.RemoveAll(point => math.distancesq(point.xz, centre.xz) < 0.0025f);
                if (flare.Count > 0) flares.Add(flare);
            }

            if (flares.Count == 0) return false;
            if (!TryGetFanSurface(composition, cache, out float height, out string materialId,
                                  out float2 surfaceUv)) return false;

            // The polygon is assembled flare by flare, not by one global bearing sort. Sorting
            // every point around the node breaks exactly where junctions are hardest: two roads
            // meeting at a shallow angle present flares whose points interleave in bearing, and
            // the sorted ring zigzags between them — a self-crossing polygon whose fan leaves
            // sawtooth holes. Keeping each flare's points contiguous makes interleaving
            // impossible; only the flares themselves are ordered by bearing, and road ends are
            // disjoint, so the ring stays simple. Within a flare the points are ordered by
            // bearing *relative to the flare's own mean*, which is stable however the node's
            // curves happen to be handed.
            // （多邊形逐擴張部組裝，而非對所有點做單一方位角排序。全域排序恰在路口最困難之處失效：
            //   兩條道路以淺角度相會時，其擴張部的點在方位角上互相交錯，排序後的環於兩者之間
            //   來回鋸齒——自交的多邊形，其扇形面留下鋸齒狀破洞。使各擴張部的點保持連續，
            //   交錯便不可能發生；僅擴張部本身依方位角排序，而道路端點彼此不相交，故環保持簡單。
            //   擴張部內部的點依「相對於該擴張部平均方位角」排序，無論節點曲線的手性如何皆穩定。）
            float Bearing(float3 point) => math.atan2(point.x - centre.x, point.z - centre.z);

            float MeanBearing(List<float3> flare)
            {
                // Averaged as a direction, not as angles: the mean of +179° and −179° is 0°, not
                // the 180° the arithmetic mean gives.
                // （以方向而非角度取平均：+179° 與 −179° 的平均是 0°，而非算術平均給出的 180°。）
                float2 sum = float2.zero;
                foreach (float3 point in flare) sum += math.normalizesafe(point.xz - centre.xz);
                return math.atan2(sum.x, sum.y);
            }

            flares.Sort((a, b) => MeanBearing(a).CompareTo(MeanBearing(b)));

            List<float3> ring = new();
            foreach (List<float3> flare in flares)
            {
                float mean = MeanBearing(flare);
                flare.Sort((a, b) =>
                {
                    float da = math.atan2(math.sin(Bearing(a) - mean), math.cos(Bearing(a) - mean));
                    float db = math.atan2(math.sin(Bearing(b) - mean), math.cos(Bearing(b) - mean));
                    return da.CompareTo(db);
                });
                ring.AddRange(flare);
            }

            // At an arm whose halves share their median the two inner points coincide, and a
            // coincident pair triangulates to a zero-area sliver. Dropping the duplicate keeps the
            // fan's triangles real.
            // （兩半共用分隔線的路口臂，其兩個內側點重合，重合的點對只會產生零面積的碎三角形。
            //   去除重複點可使扇形面的三角形皆為實面。）
            for (int i = ring.Count - 1; i > 0; i--)
            {
                if (math.distancesq(ring[i], ring[i - 1]) < 1e-4f) ring.RemoveAt(i);
            }
            if ((ring.Count > 1) && (math.distancesq(ring[0], ring[ring.Count - 1]) < 1e-4f))
            {
                ring.RemoveAt(ring.Count - 1);
            }
            if (ring.Count < 3) return false;

            // The hub sits at the mean height of the ring rather than at the node's own, which is
            // the centreline height and can be a road's crown rather than the surface the flares
            // actually end on.
            // （中心點取環上各點的平均高度，而非節點自身的高度；後者為中線高度，
            //   可能是道路的拱頂而非擴張部實際結束所在的表面。）
            float meanY = 0f;
            foreach (float3 point in ring) meanY += point.y;
            meanY /= ring.Count;

            int startIndex = indices.Count;
            int startVertex = vertices.Count;

            // One UV for the whole fan, taken from the carriageway surface piece rather than
            // invented. A constant is right here — the fan is a flat polygon standing in for the
            // junction interior, and there is no authored mapping to stretch across it, so any
            // gradient would be fabricated. Which constant matters, and it has been wrong twice:
            // a hard-coded (0.5, 0.5) landed in a lighter band at the centre of the atlas, and the
            // median piece's top — the next attempt — samples the median band, which on the
            // measured road sheet is darker (luminance ~83) than the band the drive pieces'
            // authored UVs sit in (~86–90, and the kerbs at ~130). The junction interior stands in
            // for carriageway, so it has to sample where the carriageway samples — the widest
            // drive surface piece — or every junction reads as a differently-shaded slab against
            // the flares around it.
            // （整片扇形面共用一個 UV，取自車道表面部件而非憑空指定。此處採用常數是正確的：
            //   扇形面是代表路口內部的平面多邊形，並無可供延展的既有映射，任何漸層皆屬捏造。
            //   取用哪一個常數至關重要，且已錯過兩次：寫死的 (0.5, 0.5) 落在圖集中央較淺的色帶，
            //   其後改取的中線部件頂面則取樣分隔帶色帶——實測道路圖集中其亮度（約 83）
            //   低於車道部件製作 UV 所在的色帶（約 86–90）。路口內部代表的是車道，
            //   故必須取樣於車道所取樣之處——最寬的車道表面部件——
            //   否則每個路口都是一塊與周圍擴張部色調不同的板面。）
            vertices.Add(new float3(centre.x, meanY + height, centre.z) - center);
            uvs.Add(surfaceUv);

            foreach (float3 point in ring)
            {
                vertices.Add(point - center + new float3(0f, height, 0f));
                uvs.Add(surfaceUv);
            }

            float3 hub = vertices[startVertex];
            bool flip = math.cross(vertices[startVertex + 1] - hub, vertices[startVertex + 2] - hub).y < 0f;

            for (int i = 0; i < ring.Count; i++)
            {
                int a = startVertex + 1 + i;
                int b = startVertex + 1 + ((i + 1) % ring.Count);

                indices.Add(startVertex);
                indices.Add(flip ? b : a);
                indices.Add(flip ? a : b);
            }

            primitives.Add(new MeshPrimitive
            {
                StartIndex = startIndex,
                IndexCount = indices.Count - startIndex,
                StartVertex = startVertex,
                VertexCount = vertices.Count - startVertex,
                MaterialIndex = primitives.Count,
                MaterialId = materialId
            });

            _fansBuilt++;
            report?.AppendLine($"  junction fan at node {node.Index}: {ring.Count} ring point(s) "
                             + $"from {connected.Length} edge(s), height {height:F2} m");
            return true;
        }

        /// <summary>
        /// The height, material and top UV of the surface piece a junction fill should stand in
        /// for: the widest carriageway surface piece, falling back to the median one.<br/>
        /// （路口填補面所應代表的表面部件之高度、材質與頂面 UV：
        /// 取最寬的車道表面部件，無則退回中線部件。）<br/><br/>
        /// The junction interior is where lanes cross, so it reads as carriageway; taking the
        /// sample from the median piece — as an earlier version did — shaded every junction with
        /// the median band of the atlas, visibly darker than the flares built from the drive
        /// pieces around it. Preferring the drive piece also gives a fan to compositions that
        /// have no median at all, which previously produced no fill and left the junction open.<br/>
        /// （路口內部是車道交會之處，其外觀即為車道；舊版取樣自中線部件，
        /// 使每個路口染上圖集的分隔帶色帶，明顯深於周圍由車道部件構成的擴張部。
        /// 優先取車道部件亦使全無分隔帶的組成得以產生扇形面——舊版於此不產生任何填補，
        /// 路口因而開洞。）
        /// </summary>
        /// <param name="composition">The composition entity.（組成實體。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="height">Receives the surface-top height relative to the boundary curves.（接收相對於邊界曲線的表面頂高。）</param>
        /// <param name="materialId">Receives the bundle material id.（接收束材質 ID。）</param>
        /// <param name="uv">Receives the top UV to sample at.（接收取樣用的頂面 UV。）</param>
        /// <returns>Whether the composition has any surface piece.（該組成是否具備任何表面部件。）</returns>
        private bool TryGetFanSurface(Entity composition, Dictionary<Entity, MeshAsset> cache,
                                      out float height, out string materialId, out float2 uv)
        {
            height = 0f;
            materialId = null;

            // Only reached if the piece has no usable UVs of its own. It is the centre of the
            // atlas, which is a guess — see the note in <see cref="BuildJunctionFan"/>.
            // （僅於部件本身無可用 UV 時採用。此為圖集中心，屬臆測值。）
            uv = new float2(0.5f, 0.5f);

            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return false;

            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return false;

            DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);

            int best = -1;
            bool bestIsDrive = false;
            float bestWidth = 0f;

            for (int p = 0; p < pieces.Length; p++)
            {
                NetCompositionPiece piece = pieces[p];
                if ((piece.m_Piece == Entity.Null)
                    || ((piece.m_PieceFlags & NetPieceFlags.Surface) == 0)) continue;

                bool drive = (piece.m_SectionFlags & NetSectionFlags.Median) == 0;
                float pieceWidth = piece.m_Size.x;

                // Drive beats median; among equals the widest wins, being the piece most of the
                // junction interior stands in for.
                // （車道優於分隔帶；同類之中取最寬者，因其為路口內部所代表的主要部件。）
                if ((best >= 0) && (bestIsDrive == drive) && (pieceWidth <= bestWidth)) continue;
                if ((best >= 0) && bestIsDrive && !drive) continue;

                best = p;
                bestIsDrive = drive;
                bestWidth = pieceWidth;
            }

            if (best < 0) return false;

            NetCompositionPiece chosen = pieces[best];
            string pieceName = Instance.Name.GetPrefabName(chosen.m_Piece);
            if (!cache.TryGetValue(chosen.m_Piece, out MeshAsset source))
            {
                source = Instance.MeshRip.ReadMesh(chosen.m_Piece, SanitizeId(pieceName), pieceName);
                cache[chosen.m_Piece] = source;
            }

            if ((source == null) || (source.Vertices.Length == 0)) return false;

            float maxY = source.Vertices[0].y;
            foreach (float3 v in source.Vertices) maxY = math.max(maxY, v.y);

            // The *top* of the piece, not its mid-height: the fan stands level with the surface
            // the flares end on, so a half-thickness step cannot open between them.
            // （取部件的**頂面**而非其中間高度：扇形面與擴張部所止於的表面齊平，
            //   兩者之間不致出現半個厚度的落差。）
            height = maxY + chosen.m_Offset.y;
            materialId = ((source.Primitives.Count > 0) && !string.IsNullOrEmpty(source.Primitives[0].MaterialId))
                ? source.Primitives[0].MaterialId : $"{SanitizeId(pieceName)}_mat0";

            // Where this piece's *top* samples the atlas. The fan is a stand-in for road
            // surface, so it has to sample where road surface samples — and a piece's own top
            // vertices are the only place that answer exists. Averaging over the top rather
            // than over the whole piece matters: a surface piece's sides and underside sit in
            // other bands of the same sheet, and including them drags the mean across a band
            // boundary into a shade the road never uses.
            // （此部件的**頂面**於圖集中的取樣位置。扇形面是路面的替代物，
            //   故必須取樣於路面所取樣之處，而該答案僅存在於部件自身的頂部頂點。
            //   取頂面平均而非整體平均至關重要：表面部件的側面與底面位於同一張圖的其他色帶，
            //   一併納入會使平均值跨越色帶邊界，落入道路從未使用的色調。）
            if ((source.Uvs != null) && (source.Uvs.Length == source.Vertices.Length))
            {
                float2 sum = float2.zero;
                int counted = 0;
                float top = maxY - 0.001f;

                for (int i = 0; i < source.Vertices.Length; i++)
                {
                    if (source.Vertices[i].y < top) continue;
                    sum += source.Uvs[i];
                    counted++;
                }

                if (counted == 0)
                {
                    for (int i = 0; i < source.Uvs.Length; i++) sum += source.Uvs[i];
                    counted = source.Uvs.Length;
                }

                if (counted > 0) uv = sum / counted;
            }

            return true;
        }

        /// <summary>
        /// Finish a shared mesh: pack the accumulated lists and rebuild normals.
        /// （完成共用網格：封裝累積的列表並重建法線。）
        /// </summary>
        /// <param name="assetId">The asset id to label it with.（用以標記的資產 ID。）</param>
        /// <param name="vertices">The accumulated vertices.（累積的頂點。）</param>
        /// <param name="uvs">The accumulated UVs.（累積的 UV。）</param>
        /// <param name="indices">The accumulated indices.（累積的索引。）</param>
        /// <param name="primitives">The accumulated primitives.（累積的圖元。）</param>
        /// <returns>The mesh, or <c>null</c> when nothing was assembled.（網格；若無任何組裝結果則為 <c>null</c>。）</returns>
        private static MeshAsset Finish(string assetId, List<float3> vertices, List<float2> uvs,
                                        List<int> indices, List<MeshPrimitive> primitives)
        {
            if (vertices.Count == 0) return null;

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = assetId,
                Vertices = vertices.ToArray(),
                Uvs = uvs.ToArray(),
                Indices = indices.ToArray(),
                Primitives = primitives
            };

            // Normals have to be rebuilt, not carried over: bending the mesh rotates every face,
            // and a piece's authored normal points wherever the straight tile faced.
            // （法線必須重建而非沿用：彎折會旋轉每個面，而部件製作時的法線指向筆直區塊的朝向。）
            mesh.Normals = BuildNormals(mesh.Vertices, mesh.Indices);
            return mesh;
        }

        /// <summary>
        /// Rebuild vertex normals by averaging the faces that share each vertex.
        /// （以共用各頂點的面取平均，重建頂點法線。）
        /// </summary>
        /// <param name="vertices">The positions.（位置。）</param>
        /// <param name="indices">The triangle list.（三角形列表。）</param>
        /// <returns>One normal per vertex.（每頂點一個法線。）</returns>
        private static float3[] BuildNormals(float3[] vertices, int[] indices)
        {
            float3[] normals = new float3[vertices.Length];

            for (int i = 0; i + 2 < indices.Length; i += 3)
            {
                int a = indices[i], b = indices[i + 1], c = indices[i + 2];
                float3 face = math.cross(vertices[b] - vertices[a], vertices[c] - vertices[a]);
                normals[a] += face;
                normals[b] += face;
                normals[c] += face;
            }

            for (int i = 0; i < normals.Length; i++)
            {
                float length = math.length(normals[i]);
                normals[i] = (length > 1e-9f) ? (normals[i] / length) : new float3(0f, 1f, 0f);
            }

            return normals;
        }
        /// <summary>
        /// Build the world-space centerline of one edge, from its start node to its end node.
        /// （建構單一邊自起點節點至終點節點的世界座標中心線。）<br/><br/>
        /// The two halves of <see cref="EdgeGeometry"/> give the road surface's left and right
        /// bezier. Bezier evaluation is linear in the control points, so averaging the control
        /// points of the two produces exactly the pointwise midline — no sampling error, and the
        /// existing straightness test still applies to it.<br/>
        /// （<see cref="EdgeGeometry"/> 的兩半給出道路表面的左右貝茲曲線。貝茲曲線的求值對控制點為線性，
        /// 因此平均兩者的控制點即精確得到逐點中線，無取樣誤差，且既有的直線判定仍適用。）<br/><br/>
        /// The result is then extended to the two node positions, so a junction is closed by the
        /// segments meeting in it rather than left as a hole.<br/>
        /// （結果再延伸至兩個節點位置，使路口由交會於其中的各區段閉合，而非留下空缺。）
        /// </summary>
        /// <param name="entity">The edge entity.（邊實體。）</param>
        /// <param name="edge">Its <see cref="Edge"/> component.（其 <see cref="Edge"/> 元件。）</param>
        /// <param name="geometry">Its <see cref="EdgeGeometry"/> component.（其 <see cref="EdgeGeometry"/> 元件。）</param>
        /// <returns>The tessellated polyline.（細分後的折線。）</returns>
        private List<float3> BuildCenterline(Entity entity, Edge edge, EdgeGeometry geometry)
        {
            List<float3> points = new();

            Tessellate(Midline(geometry.m_Start), points);
            Tessellate(Midline(geometry.m_End), points);

            // A pipe or a power line has no road surface, so its `EdgeGeometry` is empty and the
            // midline collapses. Its `Curve` is still the run itself — every edge has one — so
            // falling back to it keeps utility networks in the bundle as the lines they are,
            // rather than dropping a whole category for want of a surface.
            // （管線或電纜沒有道路表面，其 `EdgeGeometry` 為空，中線因而退化。
            //   但其 `Curve` 仍是該段路線本身——每個邊皆有——故退而採用之，
            //   使管線類路網以其本來的線形保留於束中，而非因缺少表面而整類遭捨棄。）
            if ((points.Count < 2) && EntityManager.HasComponent<Curve>(entity))
            {
                points.Clear();
                Tessellate(EntityManager.GetComponentData<Curve>(entity).m_Bezier, points);
            }

            if (points.Count < 2) return points;

            bool hasStart = EntityManager.HasComponent<Node>(edge.m_Start);
            bool hasEnd = EntityManager.HasComponent<Node>(edge.m_End);
            float3 startPosition = hasStart ? EntityManager.GetComponentData<Node>(edge.m_Start).m_Position : default;
            float3 endPosition = hasEnd ? EntityManager.GetComponentData<Node>(edge.m_End).m_Position : default;

            // `EdgeGeometry.m_Start` is documented as the half nearest the start node, but an
            // inverted composition is a real state in CS2 and a reversed centerline would put the
            // segment's `startNode` end at the wrong junction. Comparing both endpoints against the
            // start node settles it for two lines and no assumptions.
            // （`EdgeGeometry.m_Start` 應為靠近起點節點的一半，但反轉的組成在 CS2 中確實存在，
            //   而反向的中心線會使區段的起點端落在錯誤的路口。以兩端點與起點節點比對即可確定。）
            if (hasStart && hasEnd
                && (math.distancesq(points[0], startPosition) > math.distancesq(points[points.Count - 1], startPosition)))
            {
                points.Reverse();
            }

            if (hasStart) Prepend(points, startPosition);
            if (hasEnd) Append(points, endPosition);

            return points;
        }

        /// <summary>
        /// The pointwise midline of a network segment's left and right boundary curves.
        /// （路網區段左右邊界曲線的逐點中線。）
        /// </summary>
        /// <param name="segment">The boundary pair.（邊界曲線對。）</param>
        /// <returns>The midline bezier.（中線貝茲曲線。）</returns>
        private static Bezier4x3 Midline(Segment segment) => new(
            (segment.m_Left.a + segment.m_Right.a) * 0.5f,
            (segment.m_Left.b + segment.m_Right.b) * 0.5f,
            (segment.m_Left.c + segment.m_Right.c) * 0.5f,
            (segment.m_Left.d + segment.m_Right.d) * 0.5f);

        /// <summary>
        /// Append a bezier's points to a polyline, skipping duplicates at the join.
        /// （將貝茲曲線的點附加至折線，並略過接合處的重複點。）
        /// </summary>
        /// <param name="curve">The curve to tessellate.（待細分的曲線。）</param>
        /// <param name="points">The polyline being built.（建構中的折線。）</param>
        private static void Tessellate(Bezier4x3 curve, List<float3> points)
        {
            if (Utils.MathUtils.IsStraightLine(curve, StraightThreshold))
            {
                Append(points, curve.a);
                Append(points, curve.d);
                return;
            }

            int sections = (int)math.ceil(Colossal.Mathematics.MathUtils.Length(curve) / TessellationGap);
            if (sections < 1) sections = 1;

            for (int i = 0; i <= sections; i++)
            {
                Append(points, Colossal.Mathematics.MathUtils.Position(curve, (float)i / sections));
            }
        }

        /// <summary>
        /// Add a point unless it repeats the last one.
        /// （加入一個點，除非它與最後一點重複。）<br/><br/>
        /// The halves of an edge share an endpoint and a straight curve repeats its own, so
        /// without this every centerline would carry duplicate vertices — which a consumer
        /// extruding a ribbon turns into zero-length quads.<br/>
        /// （邊的兩半共用一個端點，直線亦會重複自身端點；若無此檢查，每條中心線都會帶有重複頂點，
        /// 使擠出帶狀幾何的消費端產生零長度的四邊形。）
        /// </summary>
        /// <param name="points">The polyline being built.（建構中的折線。）</param>
        /// <param name="point">The candidate point.（候選點。）</param>
        private static void Append(List<float3> points, float3 point)
        {
            if ((points.Count > 0) && (math.distancesq(points[points.Count - 1], point) < 1e-6f)) return;
            points.Add(point);
        }

        /// <summary>
        /// Add a point at the front unless it repeats the first one.
        /// （於前端加入一個點，除非它與第一點重複。）
        /// </summary>
        /// <param name="points">The polyline being built.（建構中的折線。）</param>
        /// <param name="point">The candidate point.（候選點。）</param>
        private static void Prepend(List<float3> points, float3 point)
        {
            if ((points.Count > 0) && (math.distancesq(points[0], point) < 1e-6f)) return;
            points.Insert(0, point);
        }

        /// <summary>
        /// The total width to extrude the centerline by, in metres.
        /// （中心線擠出的總寬度，單位為公尺。）<br/><br/>
        /// <see cref="NetCompositionData.m_Width"/> is the nominal width the game itself lays the
        /// composition out to, so it is preferred; measuring the surface boundary is the fallback
        /// for compositions that do not carry it.<br/>
        /// （<see cref="NetCompositionData.m_Width"/> 是遊戲自身據以配置組成的標稱寬度，故優先採用；
        /// 對不帶該元件的組成，則退而量測表面邊界。）
        /// </summary>
        /// <param name="edgeComposition">The edge composition entity.（邊的組成實體。）</param>
        /// <param name="entity">The edge entity.（邊實體。）</param>
        /// <param name="geometry">The edge geometry.（邊的幾何。）</param>
        /// <returns>The width, or zero when none can be established.（寬度，若無法確立則為零。）</returns>
        private float GetWidth(Entity edgeComposition, Entity entity, EdgeGeometry geometry)
        {
            if ((edgeComposition != Entity.Null) && EntityManager.HasComponent<NetCompositionData>(edgeComposition))
            {
                float width = EntityManager.GetComponentData<NetCompositionData>(edgeComposition).m_Width;
                if (width > 0f) return width;
            }

            float measured = math.max(math.distance(geometry.m_Start.m_Left.a, geometry.m_Start.m_Right.a),
                                      math.distance(geometry.m_End.m_Left.d, geometry.m_End.m_Right.d));
            if (measured > 0f) return measured;

            Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            if ((prefab != Entity.Null) && EntityManager.HasComponent<NetGeometryData>(prefab))
                return EntityManager.GetComponentData<NetGeometryData>(prefab).m_DefaultWidth;

            return 0f;
        }

        /// <summary>
        /// Attach the contract's network attributes.
        /// （附加契約定義的路網屬性。）
        /// </summary>
        /// <param name="entity">The edge entity.（邊實體。）</param>
        /// <param name="edgeComposition">The edge composition entity.（邊的組成實體。）</param>
        /// <param name="width">The width already established.（已確立的寬度。）</param>
        /// <param name="record">The record to populate.（待填入的紀錄。）</param>
        /// <param name="aggregateNames">The cache of resolved road names.（已解析道路名稱的快取。）</param>
        private void AddAttributes(Entity entity, Entity edgeComposition, float width, NetworkRecord record,
                                   Dictionary<Entity, string> aggregateNames)
        {
            Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;

            string asset = Instance.Name.GetPrefabName(prefab);
            if (!string.IsNullOrEmpty(asset)) record.Attributes["asset"] = asset;

            // A road's name lives on the aggregate that groups its edges, not on the edge, and
            // resolving one goes through the game's localised name system. A city has far fewer
            // aggregates than edges, so the result is cached per aggregate.
            // （道路名稱位於彙集其各邊的聚合體上而非邊本身，解析時須經遊戲的在地化名稱系統。
            //   城市中聚合體數量遠少於邊，故依聚合體快取結果。）
            if (EntityManager.HasComponent<Aggregated>(entity))
            {
                Entity aggregate = EntityManager.GetComponentData<Aggregated>(entity).m_Aggregate;
                if (aggregate != Entity.Null)
                {
                    if (!aggregateNames.TryGetValue(aggregate, out string name))
                    {
                        name = ResolveName(aggregate);
                        aggregateNames[aggregate] = name;
                    }

                    if (!string.IsNullOrEmpty(name)) record.Attributes["name"] = name;
                }
            }

            record.Attributes["category"] = GetCategory(prefab, edgeComposition);

            string form = "normal";
            string direction = "unknown";
            float limit = 0f;

            if (edgeComposition != Entity.Null)
            {
                if (EntityManager.HasComponent<NetCompositionData>(edgeComposition))
                {
                    NetCompositionData compositionData = EntityManager.GetComponentData<NetCompositionData>(edgeComposition);

                    CompositionFlags.General general = compositionData.m_Flags.m_General;
                    if ((general & CompositionFlags.General.Elevated) != 0) form = "elevated";
                    if ((general & CompositionFlags.General.Tunnel) != 0) form = "tunnel";

                    // The contract's fourth form, `bridge`, has no CS2 counterpart: the game
                    // elevates a road and lets the terrain and water decide what that looks like.
                    // A consumer that wants bridges reads `elevated` against the Phase 6 water
                    // raster; inventing the distinction here would be a guess.
                    // （契約的第四種形式 `bridge` 在 CS2 中無對應：遊戲僅將道路架高，
                    //   其外觀交由地形與水體決定。需要橋樑的消費端應以 `elevated` 對照
                    //   第六階段的水體柵格判定。）

                    CompositionState state = compositionData.m_State;
                    bool forward = ((state & CompositionState.HasForwardRoadLanes) != 0)
                                || ((state & CompositionState.HasForwardTrackLanes) != 0);
                    bool backward = ((state & CompositionState.HasBackwardRoadLanes) != 0)
                                 || ((state & CompositionState.HasBackwardTrackLanes) != 0);
                    if (forward && backward) direction = "twoway";
                    else if (forward || backward) direction = "oneway";
                }

                if (EntityManager.HasBuffer<NetCompositionLane>(edgeComposition))
                {
                    DynamicBuffer<NetCompositionLane> lanes = EntityManager.GetBuffer<NetCompositionLane>(edgeComposition, true);
                    int count = 0;

                    for (int i = 0; i < lanes.Length; i++)
                    {
                        LaneFlags flags = lanes[i].m_Flags;

                        // Road and track lanes are the ones a consumer means by "lanes"; the
                        // master lane is the group's stand-in rather than a lane of its own, and
                        // counting it would inflate every multi-lane road by one.
                        // （消費端所謂的「車道」指的是道路與軌道車道；主車道是群組的代表而非獨立車道，
                        //   計入它會使每條多車道道路多算一條。）
                        if ((((flags & LaneFlags.Road) != 0) || ((flags & LaneFlags.Track) != 0))
                            && ((flags & LaneFlags.Master) == 0)) count++;
                    }

                    record.Attributes["lanes"] = count;
                }

                limit = GetSpeedLimit(edgeComposition);
            }

            record.Attributes["form"] = form;
            record.Attributes["direction"] = direction;
            if (limit > 0f) record.Attributes["limit"] = Math.Round((double)limit, 1);

            if (EntityManager.HasComponent<Curve>(entity))
                record.Attributes["length"] = Math.Round((double)EntityManager.GetComponentData<Curve>(entity).m_Length, 3);

            // Also in `attrs`, as the contract's own example has it: the top-level `width` is the
            // geometry a consumer extrudes by, the attribute is the semantic road width, and the
            // two coincide in the centerline form.
            // （亦置於 `attrs` 中，如契約範例所示：頂層的 `width` 是消費端據以擠出的幾何，
            //   屬性中者為語意上的道路寬度，兩者在中心線形式下相同。）
            record.Attributes["width"] = Math.Round((double)width, 3);

            // CS2 stores an elevation per end, not per segment, and a segment that climbs from the
            // ground onto a viaduct is exactly where that matters. The mean is kept because the
            // contract already names `elevation` and a consumer reading only it must keep working,
            // but both ends are published alongside: a segment running 0 m to 12 m and one sitting
            // flat at 6 m are the same number and completely different roads, and it is the
            // sloping ones a consumer has to cut terrain for or bridge.
            // （CS2 是逐端點而非逐路段儲存高程，而自地面爬上高架的路段正是此事關鍵之處。
            //   此處保留平均值，因契約已指名 `elevation`，僅讀取它的消費端必須仍能運作；
            //   但兩端一併發布：自 0 公尺升至 12 公尺的路段，與平坦地維持在 6 公尺的路段，
            //   其數值相同卻是截然不同的道路，而需要開挖地形或架橋的正是傾斜的那些。）
            if (EntityManager.HasComponent<Game.Net.Elevation>(entity))
            {
                float2 elevation = EntityManager.GetComponentData<Game.Net.Elevation>(entity).m_Elevation;
                record.Attributes["elevation"] = Math.Round((double)((elevation.x + elevation.y) * 0.5f), 3);
                record.Attributes["elevationStart"] = Math.Round((double)elevation.x, 3);
                record.Attributes["elevationEnd"] = Math.Round((double)elevation.y, 3);
            }
        }

        /// <summary>
        /// The displayable name of a road aggregate, or an empty string when it has none.
        /// （道路聚合體的可顯示名稱；若無則為空字串。）<br/><br/>
        /// The game's label name is not always a name: an unnamed road hands back the locale key
        /// its UI would look up, such as <c>Assets.NAME[Medium Road]</c>. Writing that into the
        /// bundle would hand a consumer a string from CS2's localisation system and call it a
        /// street name, so a key is resolved through the active dictionary and dropped when it
        /// resolves to nothing but itself — an unnamed road is simply unnamed.<br/>
        /// （遊戲的標籤名稱未必是名稱：未命名的道路會回傳其 UI 所用的語系鍵，
        /// 例如 <c>Assets.NAME[Medium Road]</c>。將其寫入束等同把 CS2 在地化系統的字串
        /// 當作街道名稱，故此處以現行字典解析語系鍵，若解析結果仍為其本身則捨棄。）
        /// </summary>
        /// <param name="aggregate">The aggregate entity.（聚合體實體。）</param>
        /// <returns>The name, or an empty string.（名稱，或空字串。）</returns>
        private static string ResolveName(Entity aggregate)
        {
            string label = Instance.Name.GetLabelName(aggregate);
            if (string.IsNullOrEmpty(label)) return string.Empty;

            // A locale key, not a name — `Namespace.SECTION[Value]`.
            // （此為語系鍵而非名稱——`Namespace.SECTION[Value]`。）
            if (label.IndexOf('[') < 0) return label;

            return LocaleUtils.TryTranslate(label, out string translated) ? translated : string.Empty;
        }

        /// <summary>
        /// Classify a network into a coarse, engine-neutral category name.
        /// （將路網歸類為粗略且與引擎無關的分類名稱。）<br/><br/>
        /// Read off the composition's own components rather than the lane make-up: a composition
        /// that carries <c>RoadComposition</c> is a road because the game says so, which survives
        /// modded networks that the vector export's lane-prefab table has never seen.<br/>
        /// （依組成自身的元件判定，而非車道組成：帶有 <c>RoadComposition</c> 的組成即為道路，
        /// 此判準對向量輸出的車道模板表未曾見過的模組化路網同樣成立。）
        /// </summary>
        /// <param name="prefab">The net prefab entity.（路網預製模板實體。）</param>
        /// <param name="edgeComposition">The edge composition entity.（邊的組成實體。）</param>
        /// <returns>The category name.（分類名稱。）</returns>
        private string GetCategory(Entity prefab, Entity edgeComposition)
        {
            if (edgeComposition != Entity.Null)
            {
                if (EntityManager.HasComponent<TaxiwayComposition>(edgeComposition))
                {
                    TaxiwayComposition taxiway = EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition);
                    return ((taxiway.m_Flags & TaxiwayFlags.Runway) != 0) ? "runway" : "taxiway";
                }

                if (EntityManager.HasComponent<TrackComposition>(edgeComposition))
                {
                    TrackTypes trackType = EntityManager.GetComponentData<TrackComposition>(edgeComposition).m_TrackType;
                    if ((trackType & TrackTypes.Train) != 0) return "train";
                    if ((trackType & TrackTypes.Tram) != 0) return "tram";
                    if ((trackType & TrackTypes.Subway) != 0) return "subway";
                    return "track";
                }

                if (EntityManager.HasComponent<RoadComposition>(edgeComposition))
                {
                    RoadComposition road = EntityManager.GetComponentData<RoadComposition>(edgeComposition);
                    return ((road.m_Flags & Game.Prefabs.RoadFlags.UseHighwayRules) != 0) ? "highway" : "road";
                }

                if (EntityManager.HasComponent<WaterwayComposition>(edgeComposition)) return "waterway";
                if (EntityManager.HasComponent<PathwayComposition>(edgeComposition)) return "pathway";
            }

            if (prefab != Entity.Null)
            {
                if (EntityManager.HasComponent<PipelineData>(prefab)) return "pipe";
                if (EntityManager.HasComponent<PowerLineData>(prefab)) return "cable";
                if (EntityManager.HasComponent<FenceData>(prefab)) return "fence";
            }

            return "other";
        }

        /// <summary>
        /// The composition's speed limit in km/h, or zero when it declares none.
        /// （組成的速限，單位為公里／小時；若未宣告則為零。）<br/><br/>
        /// CS2 stores speed in its own simulation unit; the vector export's 1.8 factor converts it
        /// to km/h and is reused here so both layers of a bundle quote the same number.<br/>
        /// （CS2 以自身的模擬單位儲存速度；此處沿用向量輸出的 1.8 倍換算為公里／小時，
        /// 使束的兩層引述相同數值。）
        /// </summary>
        /// <param name="edgeComposition">The edge composition entity.（邊的組成實體。）</param>
        /// <returns>The speed limit.（速限。）</returns>
        private float GetSpeedLimit(Entity edgeComposition)
        {
            // The same precedence the vector export uses when a composition carries several:
            // pathway < waterway < taxiway < track < road.
            // （組成帶有多種時，沿用向量輸出的優先順序：路徑 < 航道 < 滑行道 < 軌道 < 道路。）
            float limit = 0f;

            if (EntityManager.HasComponent<PathwayComposition>(edgeComposition))
                limit = EntityManager.GetComponentData<PathwayComposition>(edgeComposition).m_SpeedLimit;
            if (EntityManager.HasComponent<WaterwayComposition>(edgeComposition))
                limit = EntityManager.GetComponentData<WaterwayComposition>(edgeComposition).m_SpeedLimit;
            if (EntityManager.HasComponent<TaxiwayComposition>(edgeComposition))
                limit = EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition).m_SpeedLimit;
            if (EntityManager.HasComponent<TrackComposition>(edgeComposition))
                limit = EntityManager.GetComponentData<TrackComposition>(edgeComposition).m_SpeedLimit;
            if (EntityManager.HasComponent<RoadComposition>(edgeComposition))
                limit = EntityManager.GetComponentData<RoadComposition>(edgeComposition).m_SpeedLimit;

            return limit * 1.8f;
        }
    }
}
