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
    /// meeting in it. An earlier version of this comment said it did, and that is measurably false;
    /// see <see cref="DumpJunction"/> for what is missing and what has been ruled out.<br/>
    /// （節點不持有幾何，並不意味著路口即為交會其中的各區段端點之聯集。本註解的舊版曾如此宣稱，
    /// 而該說法可由量測證否；所缺者為何、已排除者為何，見 <see cref="DumpJunction"/>。）
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

            base.OnCreate();
            _log.Debug("NetworkRipSystem instance created. 路網擷取系統實例創造完成。");
        }

        /// <summary>
        /// A network rip in flight, carried between frames.
        /// （進行中的路網擷取，於幀與幀之間承載其狀態。）
        /// </summary>
        private class NetworkRun
        {
            public Entity[] Edges;
            public int Index;
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
            public BlockingCollection<PendingTexture> Textures;
            public Task Encoder;
            public List<RipWarning> Skipped = new();
        }

        /// <summary>
        /// How many read-back textures may wait to be encoded.
        /// （最多可有多少張已回讀的貼圖等待編碼。）<br/><br/>
        /// Each holds its pixels uncompressed at four bytes each, so an unbounded queue only moves
        /// the memory problem; the reader pauses when it is full, exactly as the mesh rip's does.
        /// <br/>（每張皆以每像素四位元組的未壓縮形式持有像素，故無上限的佇列只是把記憶體問題搬家；
        /// 佇列滿時讀取端暫停，與網格擷取的做法相同。）
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

                // The encoder is a live thread blocked on the queue, so abandoning the run without
                // closing it leaves it there for the rest of the session.
                // （編碼端是一條阻塞於佇列上的執行緒，若中止工作而不關閉佇列，
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
            _seamsFilled = 0;
            _seamsSkipped = 0;
            _fansBuilt = 0;
            _worstSeam = 0f;

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

            NetworkRun run = new()
            {
                Edges = edges,
                OutputDirectory = outputDirectory,
                MeshDirectory = IOUtils.CombinePath(outputDirectory, "networks"),
                Writer = new NetworkWriter(IOUtils.CombinePath(outputDirectory, "networks.jsonl"))
            };

            run.Report.AppendLine($"Network edges: {edges.Length}");

            // Materials ride along, as they do for meshes: a piece's surface assets are reached
            // through the very render prefab its geometry came from. The encoder runs off the main
            // thread because a texture is read back on it and compressed off it.
            // （材質隨之一併擷取，與網格相同：部件的表面資產正是經由其幾何來源的繪製預製模板取得。
            //   編碼於主執行緒之外進行，因貼圖於主執行緒回讀而於他處壓縮。）
            run.Textures = new BlockingCollection<PendingTexture>(QueueCapacity);
            Instance.MaterialRip.Begin(outputDirectory, textureSizeCap, texture => run.Textures.Add(texture));
            run.Encoder = Task.Run(() => Encode(run));

            _run = run;
            RipProgress.Report(RipProgress.Phase.Networks, 0, edges.Length);

            return $"Ripping {edges.Length} network edge(s).\n\n"
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

            RipProgress.Report(RipProgress.Phase.Networks, run.Index, run.Edges.Length);

            if (run.Index >= run.Edges.Length) Finish(run);
        }

        /// <summary>
        /// Dump everything the game knows about the junction under the camera, so the node's
        /// middle region can be worked out in seconds instead of a full rip.<br/>
        /// （傾印遊戲對攝影機下方路口所知的一切，使節點中央區域的推導能以秒計而非付出完整擷取的代價。）
        /// <br/><br/>
        /// <b>What is missing, and why this exists.</b> This system uses the four boundary curves
        /// in <c>CompositionMatrix0..3</c> and ignores the rest. For an <i>edge</i> that loses
        /// nothing: <c>CalculateEdgeParameters</c> writes <c>float4x4.identity</c> into matrices
        /// 4–7 and they are genuinely inert. For a <i>node</i> they are not. Whatever
        /// <c>m_MiddleRadius</c> is, <c>CalculateNodeParameters</c> puts <c>m_Middle</c> in matrix
        /// 4 and a curve built from each outer boundary's start tangent in matrices 6 and 7; only
        /// matrix 5 goes identity, and only when the radius is zero. So a node has three live
        /// curves beyond its four boundaries, and the four <c>CompositionSync</c> vectors besides.<br/>
        /// （<b>所缺者為何，以及此功能存在的理由。</b>本系統僅使用 <c>CompositionMatrix0..3</c>
        /// 中的四條邊界曲線，其餘一概忽略。對<i>邊</i>而言毫無損失：矩陣 4 至 7 皆為單位矩陣，
        /// 確為惰性。對<i>節點</i>則不然：無論 <c>m_MiddleRadius</c> 為何，矩陣 4 皆置入
        /// <c>m_Middle</c>，矩陣 6 與 7 則置入由各外側邊界起始切線所建構的曲線；
        /// 僅矩陣 5 為單位矩陣，且僅於半徑為零時如此。故節點於四條邊界之外尚有三條實際曲線。）
        /// </summary>
        /// <param name="outputDirectory">Unused; kept so the button reads like the others.（未使用，僅為使按鈕形式一致。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string DumpJunction(string outputDirectory)
        {
            StringBuilder report = new();
            float3 focus = default;
            bool hasFocus = false;

            try
            {
                Game.Rendering.IGameCameraController controller = Instance.Camera?.activeCameraController;
                if (controller != null)
                {
                    UnityEngine.Vector3 pivot = controller.pivot;
                    focus = new float3(pivot.x, pivot.y, pivot.z);
                    hasFocus = true;
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the camera pivot: {ex.Message}");
            }

            NativeArray<Entity> entities = _edgeQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                // Nearest junction first, then the widest road at it. Nearest edge picked an
                // invisible car path at a crossroads; widest edge in range picked a highway 118 m
                // away and ignored where the camera was pointing. What the camera selects is a
                // junction; what describes that junction is the widest thing meeting in it.
                // （先取最近的路口，再取其中最寬的道路。取最近的邊會選中十字路口上的不可見車道；
                //   取範圍內最寬的邊則會選中 118 公尺外的公路而無視攝影機朝向。
                //   攝影機所選定者為一個路口，而描述該路口者為交會其中最寬者。）
                Entity junctionNode = Entity.Null;
                float nearestJunction = float.MaxValue;

                foreach (Entity entity in entities)
                {
                    if (IsMarkerNetwork(entity)) continue;

                    Edge candidateEnds = EntityManager.GetComponentData<Edge>(entity);
                    foreach (Entity end in new[] { candidateEnds.m_Start, candidateEnds.m_End })
                    {
                        if ((end == Entity.Null) || !EntityManager.HasComponent<Node>(end)) continue;
                        if (CountConnections(end) < 3) continue;

                        float3 position = EntityManager.GetComponentData<Node>(end).m_Position;
                        float distance = hasFocus ? math.distance(position.xz, focus.xz) : end.Index;

                        if (distance < nearestJunction)
                        {
                            nearestJunction = distance;
                            junctionNode = end;
                        }
                    }
                }

                if (junctionNode == Entity.Null) return "No junction found.";

                Entity chosen = Entity.Null;
                float best = float.MaxValue;
                float widest = -1f;

                // Widest in range rather than nearest. Nearest picked an "Invisible Car Path -
                // 1xTwoway" at a crossroads — a real edge with a real node geometry, 68 vertices
                // of it, and no drawable surface at all — so the whole dump read 0% at every ring
                // and said nothing about the junction underneath it. A junction's character comes
                // from the widest thing meeting in it.
                // （取範圍內最寬者而非最近者。取最近者曾選中十字路口上的「Invisible Car Path」——
                //   一條具備真實節點幾何、僅 68 個頂點且完全無可繪製表面的邊——
                //   使整份傾印於各環皆為 0%，對其下的路口毫無著墨。
                //   路口的特徵取決於交會其中最寬者。）
                DynamicBuffer<ConnectedEdge> atJunction = EntityManager.GetBuffer<ConnectedEdge>(junctionNode, true);

                for (int i = 0; i < atJunction.Length; i++)
                {
                    Entity entity = atJunction[i].m_Edge;
                    if ((entity == Entity.Null) || !EntityManager.Exists(entity)) continue;
                    if (!EntityManager.HasComponent<StartNodeGeometry>(entity)) continue;
                    if (!EntityManager.HasComponent<Composition>(entity) || IsMarkerNetwork(entity)) continue;

                    EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                    float width = GetWidth(EntityManager.GetComponentData<Composition>(entity).m_Edge, entity, geometry);
                    if (width <= widest) continue;

                    widest = width;
                    best = nearestJunction;
                    chosen = entity;
                }

                if (chosen == Entity.Null) return "No junction found.";

                Composition parts = EntityManager.GetComponentData<Composition>(chosen);
                report.AppendLine($"Edge {chosen.Index} '{Instance.Name.GetPrefabName(EntityManager.GetComponentData<PrefabRef>(chosen).m_Prefab)}', "
                                + $"{best:F0} m from the camera");

                Edge chosenEnds = EntityManager.GetComponentData<Edge>(chosen);

                DumpNode(report, "START", parts.m_StartNode,
                         EntityManager.GetComponentData<StartNodeGeometry>(chosen).m_Geometry);

                if (EntityManager.HasComponent<EndNodeGeometry>(chosen))
                {
                    DumpNode(report, "END", parts.m_EndNode,
                             EntityManager.GetComponentData<EndNodeGeometry>(chosen).m_Geometry);
                }

                DumpCoverage(report, junctionNode);
                SurveySeams(report, entities);
                SurveyStandoff(report, entities);
            }
            finally
            {
                entities.Dispose();
            }

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Measure whether the flares of every edge meeting at a node actually cover the junction,
        /// and if not, where the hole is.<br/>
        /// （量測交會於某節點的各邊擴張部是否確實覆蓋該路口；若否，破洞位於何處。）<br/><br/>
        /// Three mechanisms have been ruled out by measurement — sync vertices are the identity
        /// here, <c>m_MiddleRadius</c> is zero, and the intersection piece occupies an ordinary
        /// 4 m slot. <c>NetCompositionArea</c> is a fourth: its only readers in the game are the
        /// net and object tools' snapping, so it places buildings rather than drawing anything.
        /// This samples rings outward from the node and reports what fraction of each falls inside
        /// an assembled triangle, which answers "is there a hole, how big, and in which direction"
        /// with numbers rather than another theory.<br/>
        /// （三項機制已由量測排除；<c>NetCompositionArea</c> 為第四項：其於遊戲中的唯一使用者
        /// 為路網與物件工具的吸附功能，故其用於配置建築而非繪製任何幾何。
        /// 此處自節點向外逐環取樣，回報各環有多少比例落於已組裝的三角形之內。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="node">The junction node.（路口節點。）</param>
        private void DumpCoverage(StringBuilder report, Entity node)
        {
            if ((node == Entity.Null) || !EntityManager.HasBuffer<ConnectedEdge>(node)) return;
            if (!EntityManager.HasComponent<Node>(node)) return;

            float3 centre = EntityManager.GetComponentData<Node>(node).m_Position;
            DynamicBuffer<ConnectedEdge> connected = EntityManager.GetBuffer<ConnectedEdge>(node, true);

            List<float3> vertices = new();
            List<float2> uvs = new();
            List<int> indices = new();
            List<MeshPrimitive> primitives = new();
            Dictionary<Entity, MeshAsset> cache = new();
            List<int3> spans = new();
            List<Entity> nodeCompositions = new();
            int contributing = 0;

            for (int i = 0; i < connected.Length; i++)
            {
                Entity edge = connected[i].m_Edge;
                if ((edge == Entity.Null) || !EntityManager.Exists(edge)) continue;
                if (!EntityManager.HasComponent<Composition>(edge) || !EntityManager.HasComponent<Edge>(edge)) continue;

                Composition parts = EntityManager.GetComponentData<Composition>(edge);
                Edge ends = EntityManager.GetComponentData<Edge>(edge);
                int before = vertices.Count;
                int beforeIndices = indices.Count;
                nodeCompositions.Add((ends.m_Start == node) ? parts.m_StartNode : parts.m_EndNode);

                if ((ends.m_Start == node) && EntityManager.HasComponent<StartNodeGeometry>(edge))
                {
                    EdgeNodeGeometry g = EntityManager.GetComponentData<StartNodeGeometry>(edge).m_Geometry;
                    AppendComposition(parts.m_StartNode, g.m_Left, g.m_Right, true, centre,
                                      "u", cache, vertices, uvs, indices, primitives, null);
                }

                if ((ends.m_End == node) && EntityManager.HasComponent<EndNodeGeometry>(edge))
                {
                    EdgeNodeGeometry g = EntityManager.GetComponentData<EndNodeGeometry>(edge).m_Geometry;
                    AppendComposition(parts.m_EndNode, g.m_Left, g.m_Right, true, centre,
                                      "u", cache, vertices, uvs, indices, primitives, null);
                }

                if (vertices.Count > before)
                {
                    contributing++;
                    spans.Add(new int3(edge.Index, beforeIndices, indices.Count));

                    // How far into the junction this edge's flare actually reaches. A flare that
                    // stops short of the node cannot help cover its middle however wide it is, and
                    // the union's gap is then nobody's fault in particular — so the nearest and
                    // furthest its geometry comes to the node is worth having beside the coverage.
                    // （此邊的擴張部實際深入路口多遠。未及節點的擴張部無論多寬都無助於覆蓋其中央，
                    //   故其幾何距節點的最近與最遠距離值得與覆蓋率並列。）
                    float nearest = float.MaxValue, furthest = 0f;
                    for (int v = before; v < vertices.Count; v++)
                    {
                        float radius = math.length(vertices[v].xz);
                        nearest = math.min(nearest, radius);
                        furthest = math.max(furthest, radius);
                    }

                    report.AppendLine($"  edge {edge.Index} contributed {vertices.Count - before} vertices, "
                                    + $"reaching {nearest:F1} m to {furthest:F1} m from the node");
                }
            }

            // The junction fill, measured as its own contributor. The rip adds it in
            // AssembleNetwork rather than in AppendComposition, so without this the rings would be
            // measuring an assembly the bundle does not contain.
            // （路口填補面亦視為獨立的貢獻者。擷取於 AssembleNetwork 而非 AppendComposition 中加入它，
            //   若無此段，各環所量測者將是束中並不存在的組裝結果。）
            int beforeFan = indices.Count;
            foreach (Entity candidate in nodeCompositions)
            {
                if (BuildJunctionFan(node, candidate, centre, cache, vertices, uvs, indices, primitives, report)) break;
            }

            if (indices.Count > beforeFan) spans.Add(new int3(-1, beforeFan, indices.Count));

            report.AppendLine($"--- COVERAGE at node {node.Index} {centre}: {connected.Length} connected edge(s), "
                            + $"{contributing} contributing, {vertices.Count} vertices, {indices.Count / 3} triangles");

            if (vertices.Count == 0) return;

            // A sample counts as covered when it falls inside an assembled triangle, projected to
            // the ground plane. The first version of this asked whether some vertex was within
            // 2.5 m instead, which cannot tell a hole from a coarse surface: a 4 m drive piece
            // carries vertices only at its two edges, and a flare widens those 4 m to over 5 m at
            // its far end, so the middle of an intact lane reads as missing. Triangle containment
            // has no such threshold to tune, and is what "is there a hole" actually means.
            // （當取樣點投影至地平面後落於某個已組裝三角形內，即視為已覆蓋。
            //   本測試的初版改問是否有頂點位於 2.5 公尺內，然而該問法無法區分破洞與粗糙的表面：
            //   4 公尺寬的車道部件僅於兩側帶有頂點，而擴張部會將該 4 公尺於遠端拉寬至逾 5 公尺，
            //   使完好車道的中央被判為缺失。三角形包含測試沒有需要調校的閾值。）
            const int Rays = 72;

            // Per edge as well as for the union. A hole in the union is one of two very different
            // faults: every flare stops short of it, in which case the missing geometry belongs to
            // all of them and is a shared mechanism; or one flare should reach it and does not, in
            // which case that edge is the one to look at. The union alone cannot tell those apart,
            // and guessing which it is has already cost several wrong mechanisms.
            // （除聯集外亦逐邊統計。聯集中的破洞可能出於兩種截然不同的成因：
            //   所有擴張部皆未及該處，則所缺幾何為各邊共有，屬普遍機制；
            //   或某一擴張部本應觸及卻未觸及，則該邊才是應檢視的對象。單看聯集無從分辨兩者。）
            StringBuilder heading = new("  ring   union");
            foreach (int3 span in spans) heading.Append((span.x < 0) ? "   junction fan" : $"   edge {span.x,-8}");
            report.AppendLine(heading.ToString());

            for (int r = 1; r <= 14; r++)
            {
                int covered = 0;
                List<int> missing = new();

                for (int a = 0; a < Rays; a++)
                {
                    float angle = (a / (float)Rays) * 2f * math.PI;
                    float2 sample = new(math.sin(angle) * r, math.cos(angle) * r);

                    if (Covers(vertices, indices, sample, 0, indices.Count)) covered++;
                    else missing.Add((int)math.round(math.degrees(angle)));
                }

                StringBuilder row = new($"  {r,3} m   {100f * covered / Rays,4:F0}%");

                foreach (int3 span in spans)
                {
                    int own = 0;
                    for (int a = 0; a < Rays; a++)
                    {
                        float angle = (a / (float)Rays) * 2f * math.PI;
                        float2 sample = new(math.sin(angle) * r, math.cos(angle) * r);
                        if (Covers(vertices, indices, sample, span.y, span.z)) own++;
                    }

                    row.Append($"   {100f * own / Rays,11:F0}%");
                }

                report.AppendLine(row.ToString());
                if (missing.Count > 0) report.AppendLine($"          missing {Sectors(missing, 360 / Rays)}");
            }
        }

        /// <summary>
        /// Whether a ground-plane point falls inside any assembled triangle.
        /// （某地平面上的點是否落於任一已組裝三角形之內。）
        /// </summary>
        /// <param name="vertices">The assembled vertices.（已組裝的頂點。）</param>
        /// <param name="indices">The assembled triangle list.（已組裝的三角形列表。）</param>
        /// <param name="point">The sample, in the same space.（取樣點，與前者同一空間。）</param>
        /// <param name="from">The first index to consider.（起始索引。）</param>
        /// <param name="to">One past the last index to consider.（結束索引之後一位。）</param>
        /// <returns>Whether anything covers it.（是否有幾何覆蓋之。）</returns>
        private static bool Covers(List<float3> vertices, List<int> indices, float2 point, int from, int to)
        {
            for (int i = from; (i + 2) < to; i += 3)
            {
                float2 a = vertices[indices[i]].xz;
                float2 b = vertices[indices[i + 1]].xz;
                float2 c = vertices[indices[i + 2]].xz;

                // The sign of the three edge cross products agrees exactly when the point is on
                // the same side of all three edges. Winding is mixed here — the assembly reverses
                // it for mirrored pieces — so both all-negative and all-positive count.
                // （當該點位於三條邊的同一側時，三個外積的正負號一致。此處的纏繞方向並不一致
                //   （組裝時會為鏡射部件反轉），故全負與全正皆計為包含。）
                float d0 = Cross(point - a, b - a);
                float d1 = Cross(point - b, c - b);
                float d2 = Cross(point - c, a - c);

                bool negative = (d0 < 0f) || (d1 < 0f) || (d2 < 0f);
                bool positive = (d0 > 0f) || (d1 > 0f) || (d2 > 0f);
                if (!(negative && positive)) return true;
            }

            return false;
        }

        /// <summary>
        /// The 2D cross product of two vectors.（兩向量的二維外積。）
        /// </summary>
        /// <param name="u">The first vector.（第一個向量。）</param>
        /// <param name="v">The second vector.（第二個向量。）</param>
        /// <returns>Its signed magnitude.（其帶號大小。）</returns>
        private static float Cross(float2 u, float2 v) => (u.x * v.y) - (u.y * v.x);

        /// <summary>
        /// Collapse a list of uncovered ray bearings into contiguous angular sectors, so a hole
        /// reads as one span rather than a column of numbers.<br/>
        /// （將未覆蓋的射線方位角合併為連續的角度區間，使破洞呈現為單一跨距而非一長串數字。）
        /// </summary>
        /// <param name="bearings">The uncovered bearings, ascending, in degrees.（未覆蓋的方位角，遞增，單位為度。）</param>
        /// <param name="step">The spacing between rays, in degrees.（射線間距，單位為度。）</param>
        /// <returns>A human-readable list of sectors.（可供閱讀的區間列表。）</returns>
        private static string Sectors(List<int> bearings, int step)
        {
            if (bearings.Count == 0) return "-";

            StringBuilder spans = new();
            int start = bearings[0], previous = start;

            for (int i = 1; i <= bearings.Count; i++)
            {
                if ((i < bearings.Count) && (bearings[i] == (previous + step)))
                {
                    previous = bearings[i];
                    continue;
                }

                if (spans.Length > 0) spans.Append(", ");
                spans.Append($"{start}-{previous + step} deg");

                if (i < bearings.Count) { start = bearings[i]; previous = start; }
            }

            return spans.ToString();
        }

        /// <summary>
        /// Measure the median seam across every node geometry in the city, so one junction's
        /// numbers are not mistaken for the mechanism.<br/>
        /// （量測全城每一份節點幾何的中線接縫，以免將單一路口的數值誤認為普遍機制。）<br/><br/>
        /// A node's flare is bent onto two ruled halves: the outer one spans <c>m_Left.m_Left</c>
        /// to <c>m_Left.m_Right</c>, the inner one <c>m_Right.m_Left</c> to <c>m_Right.m_Right</c>.
        /// The assembly assumes the two curves in the middle are one median. Where they are not,
        /// nothing is written between them. This reports how far apart they end up, split by
        /// whether the composition calls itself an intersection, which says whether the seam is a
        /// property of junctions or of one ramp.<br/>
        /// （節點的擴張部彎折於兩片直紋曲面之上。組裝時假設中間的兩條曲線為同一條分隔線；
        /// 若非如此，兩者之間即無任何幾何寫出。此處回報兩者最終相距多遠，
        /// 並依組成是否自稱路口分別統計，藉以判斷該接縫是路口的普遍性質，還是單一匝道的個案。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="edges">Every placed network edge.（所有已放置的路網邊。）</param>
        private void SurveySeams(StringBuilder report, NativeArray<Entity> edges)
        {
            // Buckets are 1 m wide up to 8 m, then everything above.
            // （分組寬度為 1 公尺，至 8 公尺為止，其餘歸入最後一組。）
            int[] intersectionBuckets = new int[10];
            int[] ordinaryBuckets = new int[10];
            float worst = 0f;
            Entity worstEdge = Entity.Null;

            foreach (Entity entity in edges)
            {
                if (!EntityManager.HasComponent<Composition>(entity)) continue;
                Composition parts = EntityManager.GetComponentData<Composition>(entity);

                if (EntityManager.HasComponent<StartNodeGeometry>(entity))
                {
                    Tally(EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, parts.m_StartNode,
                          intersectionBuckets, ordinaryBuckets, entity, ref worst, ref worstEdge);
                }

                if (EntityManager.HasComponent<EndNodeGeometry>(entity))
                {
                    Tally(EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, parts.m_EndNode,
                          intersectionBuckets, ordinaryBuckets, entity, ref worst, ref worstEdge);
                }
            }

            report.AppendLine("--- MEDIAN SEAM across the whole city");
            report.AppendLine("  gap at the node end   intersection   ordinary");

            for (int i = 0; i < intersectionBuckets.Length; i++)
            {
                if ((intersectionBuckets[i] == 0) && (ordinaryBuckets[i] == 0)) continue;
                string band = (i == 0) ? "     under 0.01 m"
                            : (i == intersectionBuckets.Length - 1) ? $"      over {i - 1} m"
                            : $"       {i - 1} - {i} m";
                report.AppendLine($"  {band}  {intersectionBuckets[i],12}   {ordinaryBuckets[i],8}");
            }

            report.AppendLine($"  worst {worst:F3} m on edge {worstEdge.Index}");
        }

        /// <summary>
        /// Measure how close the flares meeting at each junction actually get to the node they meet
        /// at, city-wide.<br/>
        /// （量測全城各路口交會的擴張部實際逼近其所交會節點的程度。）<br/><br/>
        /// On the junction measured so far, three flares reached no nearer than 0.8 m, 2.0 m and
        /// 2.4 m, and the two furthest covered exactly 0% of the rings at 1 m and 2 m — so the
        /// middle of that junction is covered by whichever flare happens to come closest rather
        /// than by anything that set out to cover it. That junction is an elevated highway ramp,
        /// which is the one the camera happened to be over every time. Before treating a standoff
        /// as the mechanism it has to be shown on ordinary junctions too, and shown to be the same
        /// size, which is what this reports.<br/>
        /// （於目前量測的路口，三片擴張部最近僅達 0.8、2.0 與 2.4 公尺，
        /// 且其中較遠的兩片於 1 公尺與 2 公尺環上的覆蓋率恰為 0%。該路口為高架公路匝道，
        /// 亦即攝影機每次恰好所在之處。在將此退縮視為普遍機制之前，
        /// 須證明其於一般路口亦然且幅度相同。）<br/><br/>
        /// The near edge of a flare is measured as the chord between its two outer boundaries at
        /// the node end, which needs four bezier evaluations rather than an assembled mesh — cheap
        /// enough to run over every edge in the city.<br/>
        /// （擴張部的近端以其節點端兩條外側邊界之間的弦量測，僅需四次貝茲求值而非組裝網格。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="edges">Every placed network edge.（所有已放置的路網邊。）</param>
        private void SurveyStandoff(StringBuilder report, NativeArray<Entity> edges)
        {
            Dictionary<Entity, float> nearest = new();
            Dictionary<Entity, bool> roadLike = new();

            foreach (Entity entity in edges)
            {
                if (!EntityManager.HasComponent<Composition>(entity) || !EntityManager.HasComponent<Edge>(entity)) continue;

                // The same exclusion the rip applies, plus the widths: a survey that counts pipe
                // anchors and invisible paths alongside carriageways is measuring the wrong
                // population, and the first version of this did exactly that.
                // （與擷取相同的排除條件，並加入寬度：若統計將管線錨與不可見路徑
                //   與車道一併計入，所量測的母體即為錯誤，而本測試的初版正是如此。）
                if (IsMarkerNetwork(entity)) continue;

                Composition parts = EntityManager.GetComponentData<Composition>(entity);
                Edge ends = EntityManager.GetComponentData<Edge>(entity);

                Entity prefab = EntityManager.HasComponent<PrefabRef>(entity)
                    ? EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab : Entity.Null;
                string category = GetCategory(prefab, parts.m_Edge);
                bool isRoad = (category == "road") || (category == "highway");

                if (EntityManager.HasComponent<StartNodeGeometry>(entity))
                {
                    Reach(EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry,
                          ends.m_Start, isRoad, nearest, roadLike);
                }

                if (EntityManager.HasComponent<EndNodeGeometry>(entity))
                {
                    Reach(EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry,
                          ends.m_End, isRoad, nearest, roadLike);
                }
            }

            float[] bands = { 0.25f, 0.5f, 1f, 2f, 4f, float.MaxValue };
            int[] roads = new int[bands.Length];
            int[] other = new int[bands.Length];

            foreach (KeyValuePair<Entity, float> entry in nearest)
            {
                // Junctions only. Two edges meeting is a bend, and a bend has no middle to leave
                // uncovered.（僅計路口。兩邊相接為轉折，而轉折沒有中央區域會未被覆蓋。）
                if (CountConnections(entry.Key) < 3) continue;

                int band = 0;
                while ((band < (bands.Length - 1)) && (entry.Value > bands[band])) band++;

                if (roadLike.TryGetValue(entry.Key, out bool isRoad) && isRoad) roads[band]++;
                else other[band]++;
            }

            report.AppendLine("--- JUNCTION STANDOFF across the whole city");
            report.AppendLine("  nearest flare reaches   road/highway   other");

            for (int i = 0; i < bands.Length; i++)
            {
                if ((roads[i] == 0) && (other[i] == 0)) continue;
                string band = (i == bands.Length - 1) ? $"        over {bands[i - 1]} m"
                            : $"        under {bands[i]} m";
                report.AppendLine($"  {band,-24}{roads[i],13}{other[i],8}");
            }
        }

        /// <summary>
        /// Record how near one edge's flare comes to the node it meets.
        /// （記錄某邊的擴張部逼近其所交會節點的程度。）
        /// </summary>
        /// <param name="geometry">The node geometry.（節點幾何。）</param>
        /// <param name="node">The node entity.（節點實體。）</param>
        /// <param name="isRoad">Whether this edge is a road or a highway.（此邊是否為道路或公路。）</param>
        /// <param name="nearest">The nearest reach per node so far.（各節點目前最近的逼近距離。）</param>
        /// <param name="roadLike">Whether any edge at each node is a carriageway.（各節點是否有任一邊為車道。）</param>
        private void Reach(EdgeNodeGeometry geometry, Entity node, bool isRoad,
                           Dictionary<Entity, float> nearest, Dictionary<Entity, bool> roadLike)
        {
            if ((node == Entity.Null) || !EntityManager.HasComponent<Node>(node)) return;

            float2 centre = EntityManager.GetComponentData<Node>(node).m_Position.xz;
            float2 left = Colossal.Mathematics.MathUtils.Position(geometry.m_Left.m_Left, 1f).xz;
            float2 right = Colossal.Mathematics.MathUtils.Position(geometry.m_Right.m_Right, 1f).xz;

            float distance = DistanceToSegment(centre, left, right);
            if (!nearest.TryGetValue(node, out float best) || (distance < best)) nearest[node] = distance;

            roadLike[node] = isRoad || (roadLike.TryGetValue(node, out bool was) && was);
        }

        /// <summary>
        /// The distance from a point to a line segment, in the ground plane.
        /// （地平面上某點至線段的距離。）
        /// </summary>
        /// <param name="point">The point.（該點。）</param>
        /// <param name="a">One end of the segment.（線段的一端。）</param>
        /// <param name="b">The other end.（另一端。）</param>
        /// <returns>The distance.（距離。）</returns>
        private static float DistanceToSegment(float2 point, float2 a, float2 b)
        {
            float2 span = b - a;
            float lengthSq = math.lengthsq(span);
            if (lengthSq < 1e-6f) return math.distance(point, a);

            float t = math.saturate(math.dot(point - a, span) / lengthSq);
            return math.distance(point, a + (span * t));
        }

        /// <summary>
        /// Add one node geometry's median seam to the survey.
        /// （將單一節點幾何的中線接縫計入統計。）
        /// </summary>
        /// <param name="node">The node geometry.（節點幾何。）</param>
        /// <param name="composition">Its composition entity.（其組成實體。）</param>
        /// <param name="intersectionBuckets">Counts for intersection compositions.（路口組成的計數。）</param>
        /// <param name="ordinaryBuckets">Counts for every other composition.（其餘組成的計數。）</param>
        /// <param name="entity">The edge, for reporting the worst case.（邊，用於回報最差情形。）</param>
        /// <param name="worst">The widest seam so far.（目前最寬的接縫。）</param>
        /// <param name="worstEdge">The edge holding it.（持有該接縫的邊。）</param>
        private void Tally(EdgeNodeGeometry node, Entity composition, int[] intersectionBuckets,
                           int[] ordinaryBuckets, Entity entity, ref float worst, ref Entity worstEdge)
        {
            // The seam is measured at the node end, which is where it is widest: the two halves
            // leave the edge together and separate as they reach into the junction.
            // （於節點端量測接縫，因該處最寬：兩半自邊出發時相接，深入路口時漸行漸遠。）
            float gap = math.distance(Colossal.Mathematics.MathUtils.Position(node.m_Left.m_Right, 1f),
                                      Colossal.Mathematics.MathUtils.Position(node.m_Right.m_Left, 1f));

            bool intersection = (composition != Entity.Null)
                             && EntityManager.HasComponent<NetCompositionData>(composition)
                             && ((EntityManager.GetComponentData<NetCompositionData>(composition).m_Flags.m_General
                                  & CompositionFlags.General.Intersection) != 0);

            int bucket = (gap < 0.01f) ? 0 : math.min(intersectionBuckets.Length - 1, 1 + (int)gap);
            if (intersection) intersectionBuckets[bucket]++;
            else ordinaryBuckets[bucket]++;

            if (gap > worst) { worst = gap; worstEdge = entity; }
        }

        /// <summary>
        /// Say where the three composition matrices a node uses and this system does not actually
        /// go, measured against the node they are supposed to serve.<br/>
        /// （指出節點所用而本系統未用的三個組成矩陣實際通往何處，並以其所應服務的節點為基準量測。）
        /// <br/><br/>
        /// The four boundary curves stop short of the node — measured city-wide, 1,607 of 1,817
        /// road junctions have every flare ending more than a metre away from it. Three curves
        /// remain that the game feeds the vertex shader and this system ignores: matrix 4 is
        /// <c>m_Middle</c>, and matrices 6 and 7 are <c>BuildCurve</c> run from each outer
        /// boundary's start along its start tangent for a full composition width. A curve that
        /// reaches the node is a candidate for what covers it; one that does not is not, and can be
        /// dropped without another round of reasoning.<br/>
        /// （四條邊界曲線皆未及節點——全城量測顯示 1,817 個道路路口中有 1,607 個的所有擴張部
        /// 皆止於距其一公尺之外。尚有三條曲線為遊戲送入頂點著色器而本系統忽略者：
        /// 矩陣 4 為 <c>m_Middle</c>，矩陣 6 與 7 則以 <c>BuildCurve</c> 自各外側邊界的起點
        /// 沿其起始切線延伸一整個組成寬度。能觸及節點的曲線方為覆蓋該處的候選者。）<br/><br/>
        /// <c>BuildCurve</c> is reproduced from its IL: the tangent is normalised across the ground
        /// plane and its rise clamped, and the curve runs a full <c>width</c> along it. Only the far
        /// end is needed to say where it arrives, so the two interior control points are not
        /// reconstructed.<br/>
        /// （<c>BuildCurve</c> 依其 IL 重現：切線於地平面上正規化並限制其升幅，
        /// 曲線沿其延伸一整個 <c>width</c>。欲知其抵達何處只需末端，故不重建兩個內部控制點。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="composition">The node composition entity.（節點組成實體。）</param>
        /// <param name="node">The node geometry.（節點幾何。）</param>
        private void DumpUnusedMatrices(StringBuilder report, Entity composition, EdgeNodeGeometry node)
        {
            if (!EntityManager.HasComponent<NetCompositionData>(composition)) return;
            float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;

            // The node end of the flare is t=1; the four boundary curves all run edge-to-node.
            // （擴張部的節點端為 t=1；四條邊界曲線皆自邊延伸至節點。）
            float3 nodeEnd = (Colossal.Mathematics.MathUtils.Position(node.m_Left.m_Right, 1f)
                            + Colossal.Mathematics.MathUtils.Position(node.m_Right.m_Left, 1f)) * 0.5f;

            report.AppendLine("  unused matrices, and where they end relative to the flare's node end:");
            report.AppendLine($"    m4 middle    {Colossal.Mathematics.MathUtils.Position(node.m_Middle, 1f)}  "
                            + $"{math.distance(Colossal.Mathematics.MathUtils.Position(node.m_Middle, 1f), nodeEnd):F2} m past it");

            float3 side0 = SideConnectionEnd(node.m_Left.m_Left, width);
            float3 side1 = SideConnectionEnd(node.m_Right.m_Right, width);

            report.AppendLine($"    m6 sideLeft  {side0}  {math.distance(side0, nodeEnd):F2} m from it");
            report.AppendLine($"    m7 sideRight {side1}  {math.distance(side1, nodeEnd):F2} m from it");
        }

        /// <summary>
        /// Where a side-connection curve arrives: a full composition width along the start tangent
        /// of an outer boundary.<br/>
        /// （側向連接曲線的終點：自外側邊界的起始切線延伸一整個組成寬度之處。）
        /// </summary>
        /// <param name="boundary">The outer boundary curve.（外側邊界曲線。）</param>
        /// <param name="width">The composition width.（組成寬度。）</param>
        /// <returns>The far end.（遠端點。）</returns>
        private static float3 SideConnectionEnd(Bezier4x3 boundary, float width)
        {
            float3 tangent = Colossal.Mathematics.MathUtils.StartTangent(boundary);
            float2 flat = math.normalizesafe(tangent.xz);
            return boundary.a + (new float3(flat.x, math.clamp(tangent.y, -1f, 1f), flat.y) * width);
        }

        /// <summary>
        /// Print one bezier's four control points and its chord length.
        /// （列印單一貝茲曲線的四個控制點與其弦長。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="label">What to call the curve.（曲線的名稱。）</param>
        /// <param name="curve">The curve.（曲線。）</param>
        private static void DumpCurve(StringBuilder report, string label, Bezier4x3 curve)
        {
            report.AppendLine($"{label} a {curve.a} d {curve.d}  chord {math.distance(curve.a, curve.d):F3} m");
            report.AppendLine($"{new string(' ', label.Length)} b {curve.b} c {curve.c}");
        }

        /// <summary>
        /// Dump one node composition and its geometry.（傾印單一節點組成及其幾何。）
        /// </summary>
        /// <param name="report">The report to write into.（要寫入的報告。）</param>
        /// <param name="label">Which end this is.（此為哪一端。）</param>
        /// <param name="composition">The node composition entity.（節點組成實體。）</param>
        /// <param name="node">The node geometry.（節點幾何。）</param>
        private void DumpNode(StringBuilder report, string label, Entity composition, EdgeNodeGeometry node)
        {
            report.AppendLine($"--- {label} node, composition {composition.Index}");
            report.AppendLine($"  middleRadius {node.m_MiddleRadius:F3}");
            report.AppendLine($"  lengths left {node.m_Left.m_Length} right {node.m_Right.m_Length}");

            // All four boundary curves, not two. The assembly lerps left.m_Left -> left.m_Right
            // across the outer half and right.m_Left -> right.m_Right across the inner one, so it
            // assumes the two inner curves are the same median. Printing only one curve per half
            // is what hid that they are not.
            // （四條邊界曲線，而非兩條。組裝時於外半部由 left.m_Left 內插至 left.m_Right、
            //   於內半部由 right.m_Left 內插至 right.m_Right，故假設兩條內側曲線為同一條分隔線。
            //   每半僅列印一條曲線，正是掩蓋了兩者並不相同的原因。）
            DumpCurve(report, "  left.left  ", node.m_Left.m_Left);
            DumpCurve(report, "  left.right ", node.m_Left.m_Right);
            DumpCurve(report, "  right.left ", node.m_Right.m_Left);
            DumpCurve(report, "  right.right", node.m_Right.m_Right);
            DumpCurve(report, "  middle     ", node.m_Middle);

            // The seam itself. `CalculateEdgeParameters` leaves composition matrices 4-7 identity
            // for an edge; `CalculateNodeParameters` puts m_Middle in matrix 4 for a node, so the
            // middle curve is live geometry the assembly ignores. If the two inner curves separate
            // and m_Middle runs between them, that gap is the junction hole.
            // （接縫本身。邊的組成矩陣 4 至 7 為單位矩陣，節點則將 m_Middle 置於矩陣 4，
            //   故中線是組裝過程忽略的實際幾何。若兩條內側曲線分離而 m_Middle 行於其間，
            //   該間隙即為路口的破洞。）
            report.AppendLine("  median seam:  t    gap    left.right -> right.left    middle offset");
            for (int i = 0; i <= 4; i++)
            {
                float t = i * 0.25f;
                float3 inner0 = Colossal.Mathematics.MathUtils.Position(node.m_Left.m_Right, t);
                float3 inner1 = Colossal.Mathematics.MathUtils.Position(node.m_Right.m_Left, t);
                float3 middle = Colossal.Mathematics.MathUtils.Position(node.m_Middle, t);

                report.AppendLine($"    {t:F2}  {math.distance(inner0, inner1),6:F3} m  "
                                + $"{inner0} -> {inner1}  "
                                + $"middle {middle} ({math.distance(middle, inner0):F3} / {math.distance(middle, inner1):F3})");
            }

            report.AppendLine($"  syncTargetsLeft  {node.m_SyncVertexTargetsLeft}");
            report.AppendLine($"  syncTargetsRight {node.m_SyncVertexTargetsRight}");

            DumpUnusedMatrices(report, composition, node);

            if (EntityManager.HasComponent<NetCompositionData>(composition))
            {
                NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
                report.AppendLine($"  syncOffsetsLeft  {data.m_SyncVertexOffsetsLeft}");
                report.AppendLine($"  syncOffsetsRight {data.m_SyncVertexOffsetsRight}");

                // How far the sync vertices actually move, in metres across the composition.
                // This was recorded as ruled out — "offsets equal targets" — but that was measured
                // on an ordinary node, where they are. On the intersection node in the same dump
                // the right-hand four differ, so the mechanism is live exactly where the hole is
                // and was closed on the wrong sample.
                // （同步頂點實際移動多遠，以組成寬度上的公尺計。此機制曾被記為已排除——
                //   「偏移等於目標」——然而該量測取自一般節點，於其上兩者確實相等。
                //   同一份傾印中的路口節點，右側四者並不相等，
                //   故此機制正活躍於破洞所在之處，而當初是以錯誤的樣本將其排除。）
                float4 movedLeft = (node.m_SyncVertexTargetsLeft - data.m_SyncVertexOffsetsLeft) * data.m_Width;
                float4 movedRight = (node.m_SyncVertexTargetsRight - data.m_SyncVertexOffsetsRight) * data.m_Width;
                report.AppendLine($"  syncMoves left {movedLeft} m, right {movedRight} m");
                report.AppendLine($"  width {data.m_Width:F3} middleOffset {data.m_MiddleOffset:F3} "
                                + $"widthOffset {data.m_WidthOffset:F3} nodeOffset {data.m_NodeOffset:F3}");
                report.AppendLine($"  sideConnectionOffset {data.m_SideConnectionOffset} minLod {data.m_MinLod}");
                report.AppendLine($"  flags general {data.m_Flags.m_General}");
                report.AppendLine($"  state {data.m_State}");
            }

            if (!EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return;
            NetCompositionMeshRef meshRef = EntityManager.GetComponentData<NetCompositionMeshRef>(composition);
            Entity meshEntity = meshRef.m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return;

            // The assembly reads m_Mesh and ignores m_Rotate. In the game m_Rotate swaps the draw
            // from the Edge sub-mesh slot to RotatedEdge, so a composition that sets it is placed
            // the other way round along its length.
            // （組裝時只讀取 m_Mesh 而忽略 m_Rotate。遊戲中 m_Rotate 會將繪製由 Edge 子網格槽位
            //   切換至 RotatedEdge，故設定該旗標的組成沿其長度方向反向放置。）
            report.AppendLine($"  meshRotate {meshRef.m_Rotate}");

            if (EntityManager.HasComponent<NetCompositionMeshData>(meshEntity))
            {
                NetCompositionMeshData mesh = EntityManager.GetComponentData<NetCompositionMeshData>(meshEntity);
                report.AppendLine($"  meshData indexFactor {mesh.m_IndexFactor:F3} state {mesh.m_State} "
                                + $"defaultLayers {mesh.m_DefaultLayers} availableLayers {mesh.m_AvailableLayers}");
            }

            DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);
            Dictionary<Entity, MeshAsset> cache = new();

            for (int i = 0; i < pieces.Length; i++)
            {
                NetCompositionPiece piece = pieces[i];
                string name = Instance.Name.GetPrefabName(piece.m_Piece);
                report.AppendLine($"  [{i}] '{name}' off {piece.m_Offset} "
                                + $"size {piece.m_Size} sec {piece.m_SectionFlags} pc {piece.m_PieceFlags}");

                if (EntityManager.HasComponent<NetPieceData>(piece.m_Piece))
                {
                    NetPieceData data = EntityManager.GetComponentData<NetPieceData>(piece.m_Piece);
                    report.AppendLine($"        pieceData width {data.m_Width:F3} length {data.m_Length:F3} "
                                    + $"widthOffset {data.m_WidthOffset:F3} nodeOffset {data.m_NodeOffset:F3} "
                                    + $"sideConnectionOffset {data.m_SideConnectionOffset:F3}");
                }

                // The shape itself, which is the question these two "Intersection" pieces raise.
                // A piece whose slot width is zero can still be a wide mesh — `m_Size.x` is the
                // slot, not the geometry — and one flagged `AsymmetricMeshZ` varies along its
                // length, so what its Z means decides whether it lands as a fill or a sliver.
                // （形狀本身，正是這兩個「Intersection」部件所提出的問題。槽位寬度為零的部件仍可能是
                //   寬闊的網格——`m_Size.x` 是槽位而非幾何——而標記 `AsymmetricMeshZ` 者沿其長度變化，
                //   故其 Z 的意義決定了它落地時是填充面還是細條。）
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset mesh))
                {
                    mesh = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(name), name);
                    cache[piece.m_Piece] = mesh;
                }

                if ((mesh == null) || (mesh.Vertices.Length == 0))
                {
                    report.AppendLine("        mesh: would not read");
                    continue;
                }

                float3 lo = mesh.Vertices[0], hi = mesh.Vertices[0];
                foreach (float3 v in mesh.Vertices) { lo = math.min(lo, v); hi = math.max(hi, v); }

                report.AppendLine($"        mesh {mesh.Vertices.Length}v {mesh.TriangleCount}t  "
                                + $"x [{lo.x:F3}, {hi.x:F3}]  y [{lo.y:F3}, {hi.y:F3}]  z [{lo.z:F3}, {hi.z:F3}]");

                // How many distinct stations the mesh has along each axis. A constant cross-section
                // swept along Z has two distinct x and many z; a fill patch has many of both, and
                // that difference is what says which axis is which.
                // （網格於各軸上有多少相異的取樣位置。沿 Z 掃掠的等斷面有兩個相異的 x 與許多 z；
                //   填充面則兩者皆多，此差異即可辨明各軸的意義。）
                HashSet<int> xs = new(), zs = new();
                foreach (float3 v in mesh.Vertices)
                {
                    xs.Add((int)math.round(v.x * 100f));
                    zs.Add((int)math.round(v.z * 100f));
                }

                report.AppendLine($"        distinct x {xs.Count}, distinct z {zs.Count}");
            }
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
                        run.Bytes += GltfWriter.Write(IOUtils.CombinePath(run.MeshDirectory, $"{assetId}.glb"), assembled);
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
            // became, so it is written once the encoder has drained.
            // （材質最後處理：材質表只能以貼圖檔案的內容雜湊指名該貼圖，故須待編碼端排空後方能寫出。）
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
        /// Encode and write every queued texture, until the rip stops queueing them.
        /// （編碼並寫出每張排入佇列的貼圖，直至擷取不再排入為止。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        private static void Encode(NetworkRun run)
        {
            foreach (PendingTexture texture in run.Textures.GetConsumingEnumerable())
            {
                // One bad texture must not take the encoder down with it: the queue would then
                // never drain, and the rip would block forever waiting for it.
                // （一張損壞的貼圖不可拖垮編碼端：否則佇列永不排空，擷取將永遠等待下去。）
                try
                {
                    Instance.MaterialRip.WriteTexture(texture);
                }
                catch (Exception ex)
                {
                    _log.Warn($"Could not write a network texture: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Close the texture queue, wait for the encoder, and write <c>materials.json</c>.
        /// （關閉貼圖佇列、等待編碼端，並寫出 <c>materials.json</c>。）
        /// </summary>
        /// <param name="run">The run being finished or abandoned.（正在結束或中止的工作。）</param>
        /// <returns>The material summary, or <c>null</c>.（材質摘要，或 <c>null</c>。）</returns>
        private string Drain(NetworkRun run)
        {
            try
            {
                run.Textures?.CompleteAdding();
                run.Encoder?.Wait();
            }
            catch (Exception ex)
            {
                _log.Warn($"Network texture encoder ended with an error: {ex.Message}");
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

            if (EntityManager.HasComponent<StartNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry;
                AppendComposition(parts.m_StartNode, node.m_Left, node.m_Right, true, center,
                                  "startNode", cache, vertices, uvs, indices, primitives, report);
            }

            AppendComposition(parts.m_Edge, geometry.m_Start, geometry.m_End, false, center,
                              "edge", cache, vertices, uvs, indices, primitives, report);

            if (EntityManager.HasComponent<EndNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry;
                AppendComposition(parts.m_EndNode, node.m_Left, node.m_Right, true, center,
                                  "endNode", cache, vertices, uvs, indices, primitives, report);
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
        private void AppendPart(DynamicBuffer<NetCompositionPiece> pieces, Segment half0, Segment half1,
                                bool splitAcross, float compositionWidth, float3 center, string label,
                                Dictionary<Entity, MeshAsset> cache, List<float3> vertices,
                                List<float2> uvs, List<int> indices,
                                List<MeshPrimitive> primitives, StringBuilder report)
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

                string pieceName = Instance.Name.GetPrefabName(piece.m_Piece);

                // Cache by piece prefab. A city has thousands of edges but a few hundred distinct
                // pieces, and reading one loads and releases its geometry asset - doing that per
                // edge would repeat the same expensive load tens of thousands of times.
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset source))
                {
                    source = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(pieceName), pieceName);
                    cache[piece.m_Piece] = source;
                }

                if ((source == null) || (source.Vertices.Length == 0)) continue;

                float minZ = source.Vertices[0].z, maxZ = source.Vertices[0].z;
                foreach (float3 v in source.Vertices) { minZ = math.min(minZ, v.z); maxZ = math.max(maxZ, v.z); }
                float tileLength = math.max(maxZ - minZ, 1e-3f);

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

                int startIndex = indices.Count;
                int startVertex = vertices.Count;

                for (int tile = 0; tile < tiles; tile++)
                {
                    int baseVertex = vertices.Count;

                    for (int i = 0; i < source.Vertices.Length; i++)
                    {
                        float3 local = source.Vertices[i];

                        float across = piece.m_Offset.x + ((invert ? -local.x : local.x) * widthScale);
                        float u = math.saturate((across + (compositionWidth * 0.5f)) / compositionWidth);

                        float withinTile = (local.z - minZ) / tileLength;
                        float along = math.saturate((tile + withinTile) / tiles);

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
                        float t, s;
                        Segment segment;

                        if (splitAcross)
                        {
                            bool leftHalf = u < 0.5f;
                            segment = leftHalf ? half0 : half1;
                            s = leftHalf ? (u * 2f) : ((u - 0.5f) * 2f);
                            t = along;
                        }
                        else
                        {
                            bool firstHalf = along < 0.5f;
                            segment = firstHalf ? half0 : half1;
                            s = u;
                            t = firstHalf ? (along * 2f) : ((along - 0.5f) * 2f);
                        }

                        float3 left = Colossal.Mathematics.MathUtils.Position(segment.m_Left, t);
                        float3 right = Colossal.Mathematics.MathUtils.Position(segment.m_Right, t);
                        float3 surface = math.lerp(left, right, s);

                        vertices.Add(surface - center + new float3(0f, local.y + piece.m_Offset.y, 0f));
                        uvs.Add((source.Uvs != null) ? source.Uvs[i] : default);
                    }

                    // A mirror reverses handedness, so an inverted piece's triangles have to be
                    // wound the other way or it ends up inside out — with normals rebuilt from the
                    // faces, that would light the whole sidewalk from underneath.
                    // （鏡射會反轉手性，故經反轉的部件其三角形必須改變纏繞方向，否則將裡外顛倒；
                    //   由於法線是自面重建，如此會使整條人行道由下方受光。）
                    for (int i = 0; i + 2 < source.Indices.Length; i += 3)
                    {
                        indices.Add(baseVertex + source.Indices[i]);
                        indices.Add(baseVertex + source.Indices[invert ? (i + 2) : (i + 1)]);
                        indices.Add(baseVertex + source.Indices[invert ? (i + 1) : (i + 2)]);
                    }
                }

                primitives.Add(new MeshPrimitive
                {
                    StartIndex = startIndex,
                    IndexCount = indices.Count - startIndex,
                    StartVertex = startVertex,
                    VertexCount = vertices.Count - startVertex,
                    MaterialIndex = primitives.Count,
                    MaterialId = ((source.Primitives.Count > 0)
                                  && !string.IsNullOrEmpty(source.Primitives[0].MaterialId))
                                 ? source.Primitives[0].MaterialId
                                 : $"{SanitizeId(pieceName)}_mat0"
                });

                report?.AppendLine($"  {label} [{p}] '{pieceName}': {tiles} tile(s) of {tileLength:F1} m "
                                + $"over {edgeLength:F1} m, widthScale {widthScale:F3}");
            }
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
                                       List<int> indices, List<MeshPrimitive> primitives, StringBuilder report)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return;

            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return;

            float width = EntityManager.HasComponent<NetCompositionData>(composition)
                ? EntityManager.GetComponentData<NetCompositionData>(composition).m_Width : 0f;
            if (!(width > 0f)) return;

            DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);

            AppendPart(pieces, half0, half1, splitAcross, width, center, label, cache,
                       vertices, uvs, indices, primitives, report);

            // Only a node splits across the width, and only a node has a median to bridge.
            // （唯有節點橫向分割，也唯有節點有中線需要橋接。）
            if (splitAcross)
            {
                AppendMedianFill(pieces, half0, half1, width, center, label, cache,
                                 vertices, uvs, indices, primitives, report);
            }
        }

        /// <summary>
        /// Bridge the wedge between the two halves of a junction flare — the junction hole.<br/>
        /// （橋接路口擴張部兩半之間的楔形空缺，即路口的破洞。）<br/><br/>
        /// <b>The defect this closes, in numbers.</b> A node's flare is two ruled halves:
        /// <c>m_Left</c> spans its own two curves and <c>m_Right</c> spans its own, and the
        /// assembly maps the cross-section across them assuming the two curves in the middle are
        /// one shared median. At an ordinary node they are — all 32,473 node geometries in a full
        /// city measured a gap under 0.01 m, byte-identical curves. At an <c>Intersection</c>
        /// composition they are not: 17,111 of 19,445 separate, by 1–2 m typically and past 8 m in
        /// 702 cases. The two halves leave the edge together and pull apart as they reach into the
        /// junction, so the gap is widest exactly at the node — which is why coverage measured 61%
        /// two metres from a junction centre and 100% nine metres out.<br/>
        /// （<b>所修補缺陷的數值。</b>節點的擴張部由兩片直紋曲面構成，組裝時假設中間的兩條曲線
        /// 為共用的分隔線。於一般節點確實如此——全城 32,473 份節點幾何的間隙皆小於 0.01 公尺。
        /// 於 <c>Intersection</c> 組成則否：19,445 份中有 17,111 份分離。
        /// 兩半自邊出發時相接，深入路口時漸行漸遠，故間隙恰於節點處最寬。）<br/><br/>
        /// The strip is ruled between those same two curves, so every position on it comes from
        /// geometry the game computed rather than from anything invented here. Its height, material
        /// and UV range are taken from the composition's own median piece — the piece that already
        /// occupies the middle of this cross-section — rather than from constants.<br/>
        /// （該帶狀面以那兩條曲線為界，故其上每個位置皆源自遊戲算出的幾何，而非此處憑空捏造。
        /// 其高度、材質與 UV 範圍取自組成自身的中線部件，而非常數。）
        /// </summary>
        /// <param name="pieces">The composition's cross-section.（組成的橫斷面。）</param>
        /// <param name="half0">The outer half's boundary curves.（外半部的邊界曲線。）</param>
        /// <param name="half1">The inner half's boundary curves.（內半部的邊界曲線。）</param>
        /// <param name="center">The origin the result is relative to.（結果所依據的原點。）</param>
        /// <param name="label">What to call this in the report.（此部分於報告中的名稱。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="vertices">The shared vertex list.（共用的頂點列表。）</param>
        /// <param name="uvs">The shared UV list.（共用的 UV 列表。）</param>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="primitives">The shared primitive list.（共用的圖元列表。）</param>
        /// <param name="report">The report to note the working in.（用以記錄過程的報告。）</param>
        private void AppendMedianFill(DynamicBuffer<NetCompositionPiece> pieces, Segment half0, Segment half1,
                                      float compositionWidth, float3 center, string label,
                                      Dictionary<Entity, MeshAsset> cache,
                                      List<float3> vertices, List<float2> uvs, List<int> indices,
                                      List<MeshPrimitive> primitives, StringBuilder report)
        {
            Bezier4x3 inner0 = half0.m_Right;
            Bezier4x3 inner1 = half1.m_Left;

            // Measured at the node end, where the wedge is widest. Below this the two curves are
            // the same median and there is nothing between them to bridge.
            // （於節點端量測，該處楔形最寬。低於此值時兩曲線即同一條分隔線，其間無物可橋接。）
            float gap = math.distance(Colossal.Mathematics.MathUtils.Position(inner0, 1f),
                                      Colossal.Mathematics.MathUtils.Position(inner1, 1f));
            if (gap < SeamThreshold) return;

            // A median cannot be wider than the road it runs down the middle of. Past that the two
            // curves are not a median that has parted — they are carriageways that have genuinely
            // diverged, and paving between them would claim road surface where a junction simply
            // splits. A city holds 702 node geometries whose halves part by more than 8 m, the
            // worst by 124.7 m, and a 124 m slab is a far worse defect than the hole it covers.
            // The bound is the composition's own width rather than a number chosen to fit.
            // （分隔線不可能寬於其所貫穿的道路。超過此限，兩條曲線便不是分開的分隔線，
            //   而是真正分岔的車道；於其間鋪面等同宣稱該處有路面，然而路口只是在此分岔。
            //   全城有 702 份節點幾何的兩半分離超過 8 公尺，最差者達 124.7 公尺，
            //   而 124 公尺的板面遠比它所覆蓋的破洞更糟。此界限取自組成自身的寬度。）
            if (gap > compositionWidth)
            {
                report?.AppendLine($"  {label} median fill skipped: {gap:F1} m wedge exceeds the "
                                 + $"{compositionWidth:F1} m composition width");
                _seamsSkipped++;
                return;
            }

            for (int p = 0; p < pieces.Length; p++)
            {
                NetCompositionPiece piece = pieces[p];

                // The median surface piece, and only it. A composition's median slot also holds a
                // bottom piece for elevated roads; bridging that too would mean deciding which way
                // its faces point and how thick the wedge is, neither of which is in the data.
                // The road surface is the hole anyone can see.
                // （僅取中線的表面部件。高架道路的中線槽位另有底部部件，一併橋接則須判定其面朝向
                //   與楔形厚度，而資料中皆無此資訊。路面才是看得見的破洞。）
                if ((piece.m_Piece == Entity.Null)
                    || ((piece.m_SectionFlags & NetSectionFlags.Median) == 0)
                    || ((piece.m_PieceFlags & NetPieceFlags.Surface) == 0)) continue;

                string pieceName = Instance.Name.GetPrefabName(piece.m_Piece);
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset source))
                {
                    source = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(pieceName), pieceName);
                    cache[piece.m_Piece] = source;
                }

                if ((source == null) || (source.Vertices.Length == 0)) continue;

                float minY = source.Vertices[0].y, maxY = source.Vertices[0].y;
                float2 minUv = (source.Uvs != null) ? source.Uvs[0] : default;
                float2 maxUv = minUv;

                for (int i = 0; i < source.Vertices.Length; i++)
                {
                    minY = math.min(minY, source.Vertices[i].y);
                    maxY = math.max(maxY, source.Vertices[i].y);

                    if (source.Uvs == null) continue;
                    minUv = math.min(minUv, source.Uvs[i]);
                    maxUv = math.max(maxUv, source.Uvs[i]);
                }

                // The surface sits a little below the boundary curve, exactly as the piece does,
                // so the bridge lands flush with the road either side of it rather than floating.
                // （表面略低於邊界曲線，與部件相同，使橋接面與兩側路面齊平而非浮於其上。）
                float height = ((minY + maxY) * 0.5f) + piece.m_Offset.y;

                int startIndex = indices.Count;
                int startVertex = vertices.Count;

                // Seventeen rows is what the flare's own pieces tessellate to over the same run, so
                // the bridge bends with them instead of cutting a straight chord across a curve.
                // （十七列正是擴張部各部件於同一段距離上的細分數，使橋接面與其一同彎折，
                //   而非以直弦橫切彎道。）
                const int Rows = 17;

                for (int r = 0; r < Rows; r++)
                {
                    float t = r / (float)(Rows - 1);
                    float3 left = Colossal.Mathematics.MathUtils.Position(inner0, t);
                    float3 right = Colossal.Mathematics.MathUtils.Position(inner1, t);

                    vertices.Add(left - center + new float3(0f, height, 0f));
                    vertices.Add(right - center + new float3(0f, height, 0f));
                    uvs.Add(new float2(minUv.x, math.lerp(minUv.y, maxUv.y, t)));
                    uvs.Add(new float2(maxUv.x, math.lerp(minUv.y, maxUv.y, t)));
                }

                // Which way round the two triangles of a quad go decides which way the strip faces,
                // and normals here are rebuilt from the faces. Rather than reason about the
                // handedness of two curves that can be either way round, take the first quad's
                // normal and flip the winding when it points at the ground.
                // （四邊形兩個三角形的繞向決定該帶狀面的朝向，而此處的法線由面重建。
                //   與其推論兩條可能任意排列的曲線之手性，不如取首個四邊形的法線，
                //   若其朝向地面則反轉繞向。）
                float3 a = vertices[startVertex], b = vertices[startVertex + 1], c = vertices[startVertex + 2];
                bool flip = math.cross(b - a, c - a).y < 0f;

                for (int r = 0; r + 1 < Rows; r++)
                {
                    int row = startVertex + (r * 2);
                    AddQuad(indices, row, row + 1, row + 2, row + 3, flip);
                }

                primitives.Add(new MeshPrimitive
                {
                    StartIndex = startIndex,
                    IndexCount = indices.Count - startIndex,
                    StartVertex = startVertex,
                    VertexCount = vertices.Count - startVertex,
                    MaterialIndex = primitives.Count,
                    MaterialId = ((source.Primitives.Count > 0)
                                  && !string.IsNullOrEmpty(source.Primitives[0].MaterialId))
                                 ? source.Primitives[0].MaterialId
                                 : $"{SanitizeId(pieceName)}_mat0"
                });

                _seamsFilled++;
                _worstSeam = math.max(_worstSeam, gap);

                report?.AppendLine($"  {label} median fill '{pieceName}': {gap:F2} m wedge at the node, "
                                 + $"height {height:F2} m");
            }
        }

        /// <summary>
        /// Close the middle of a junction: the polygon bounded by the node-end of every flare
        /// meeting there.<br/>
        /// （封閉路口的中央：由交會其中每片擴張部的節點端所圍成的多邊形。）<br/><br/>
        /// <b>Why this is a reconstruction and not a reproduction.</b> A node feeds the vertex
        /// shader three curves beyond its four boundaries — <c>m_Middle</c> in composition matrix 4
        /// and two side-connection curves in 6 and 7 — and which vertices ride which matrix is
        /// decided in the shader. CS2 ships its shaders compiled inside asset bundles, so that
        /// mapping is not recoverable from the managed assemblies the way
        /// <c>CalculateNodeParameters</c> itself was. Reproducing the game's own fill is therefore
        /// not available from this source, and guessing the mapping has already cost five wrong
        /// mechanisms.<br/>
        /// （<b>此為重建而非重現的理由。</b>節點於四條邊界之外另有三條曲線送入頂點著色器，
        /// 而何者頂點依循何者矩陣係由著色器決定。CS2 的著色器以編譯形式封裝於資產包中，
        /// 故該映射無法如 <c>CalculateNodeParameters</c> 本身那般自受管組件中取得。）<br/><br/>
        /// <b>What is measured, and what this does about it.</b> Across a full city, 1,607 of 1,817
        /// road junctions have every flare ending more than a metre short of the node, and 316 more
        /// than four metres; other network types are the other way round, 77% of them reaching
        /// within a metre. So the gap is a property of carriageways and it is the hole. Each flare
        /// presents a chord at its node end, from its left outer boundary to its right outer one —
        /// kerb to kerb. Those chords, taken in bearing order around the node, bound exactly the
        /// area no flare covers: the middle of the junction, and the corner fillets between
        /// adjacent roads. Filling that polygon uses no constant and invents no shape — every
        /// vertex is a point on a boundary curve the game computed, and the fill can never exceed
        /// the roads that bound it.<br/>
        /// （<b>已量測者為何，以及此處如何處置。</b>全城 1,817 個道路路口中有 1,607 個的所有擴張部
        /// 皆止於距節點一公尺之外，316 個超過四公尺；其他路網型別則相反，77% 可達一公尺以內。
        /// 故此間隙為車道所特有，即為該破洞。每片擴張部於其節點端呈現一條自左外側邊界至右外側邊界
        /// 的弦，即緣石至緣石。這些弦依繞節點的方位角排序後，恰好圍出無任何擴張部覆蓋之區域：
        /// 路口的中央，以及相鄰道路之間的轉角填角。填補該多邊形不使用任何常數、不捏造任何形狀。）
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
            List<float3> ring = new();
            int owner = int.MaxValue;

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

                owner = math.min(owner, other.Index);

                // t=1 is the node end; the four boundary curves all run edge-to-node.
                // （t=1 為節點端；四條邊界曲線皆自邊延伸至節點。）
                ring.Add(Colossal.Mathematics.MathUtils.Position(geometry.m_Left.m_Left, 1f));
                ring.Add(Colossal.Mathematics.MathUtils.Position(geometry.m_Right.m_Right, 1f));
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

            List<float3> ring = new();

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

                // t=1 is the node end; the four boundary curves all run edge-to-node.
                // （t=1 為節點端；四條邊界曲線皆自邊延伸至節點。）
                ring.Add(Colossal.Mathematics.MathUtils.Position(geometry.m_Left.m_Left, 1f));
                ring.Add(Colossal.Mathematics.MathUtils.Position(geometry.m_Right.m_Right, 1f));
            }

            if (ring.Count < 3) return false;
            if (!TryGetMedianSurface(composition, cache, out float height, out string materialId)) return false;

            float3 centre = EntityManager.GetComponentData<Node>(node).m_Position;

            // Bearing order around the node, so the ring is a simple polygon rather than a
            // self-crossing star. The flares arrive from different directions and the connected
            // edge buffer is in no particular order.
            // （依繞節點的方位角排序，使該環為簡單多邊形而非自交的星形。）
            ring.Sort((a, b) => math.atan2(a.x - centre.x, a.z - centre.z)
                       .CompareTo(math.atan2(b.x - centre.x, b.z - centre.z)));

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

            vertices.Add(new float3(centre.x, meanY + height, centre.z) - center);
            uvs.Add(new float2(0.5f, 0.5f));

            foreach (float3 point in ring)
            {
                vertices.Add(point - center + new float3(0f, height, 0f));
                uvs.Add(new float2(0.5f, 0.5f));
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
        /// The height and material of a composition's median surface piece — the piece already
        /// occupying the middle of that cross-section.<br/>
        /// （組成中線表面部件的高度與材質，即已佔據該橫斷面中央的部件。）<br/><br/>
        /// Taking both from the composition rather than from constants is what keeps a fill flush
        /// with the road either side of it and named by a material the bundle already carries.<br/>
        /// （高度與材質皆取自組成而非常數，方能使填補面與兩側路面齊平，
        /// 且其材質為束中已有者。）
        /// </summary>
        /// <param name="composition">The composition entity.（組成實體。）</param>
        /// <param name="cache">The piece mesh cache.（部件網格快取。）</param>
        /// <param name="height">Receives the surface height relative to the boundary curves.（接收相對於邊界曲線的表面高度。）</param>
        /// <param name="materialId">Receives the bundle material id.（接收束材質 ID。）</param>
        /// <returns>Whether the composition has one.（該組成是否具備此部件。）</returns>
        private bool TryGetMedianSurface(Entity composition, Dictionary<Entity, MeshAsset> cache,
                                         out float height, out string materialId)
        {
            height = 0f;
            materialId = null;

            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return false;

            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return false;

            DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);

            for (int p = 0; p < pieces.Length; p++)
            {
                NetCompositionPiece piece = pieces[p];
                if ((piece.m_Piece == Entity.Null)
                    || ((piece.m_SectionFlags & NetSectionFlags.Median) == 0)
                    || ((piece.m_PieceFlags & NetPieceFlags.Surface) == 0)) continue;

                string pieceName = Instance.Name.GetPrefabName(piece.m_Piece);
                if (!cache.TryGetValue(piece.m_Piece, out MeshAsset source))
                {
                    source = Instance.MeshRip.ReadMesh(piece.m_Piece, SanitizeId(pieceName), pieceName);
                    cache[piece.m_Piece] = source;
                }

                if ((source == null) || (source.Vertices.Length == 0)) continue;

                float minY = source.Vertices[0].y, maxY = source.Vertices[0].y;
                foreach (float3 v in source.Vertices) { minY = math.min(minY, v.y); maxY = math.max(maxY, v.y); }

                height = ((minY + maxY) * 0.5f) + piece.m_Offset.y;
                materialId = ((source.Primitives.Count > 0) && !string.IsNullOrEmpty(source.Primitives[0].MaterialId))
                    ? source.Primitives[0].MaterialId : $"{SanitizeId(pieceName)}_mat0";
                return true;
            }

            return false;
        }

        /// <summary>
        /// Add the two triangles of a quad, in one winding or the other.
        /// （加入四邊形的兩個三角形，依指定繞向。）
        /// </summary>
        /// <param name="indices">The shared index list.（共用的索引列表。）</param>
        /// <param name="a">The first corner.（第一個角。）</param>
        /// <param name="b">The second corner.（第二個角。）</param>
        /// <param name="c">The third corner.（第三個角。）</param>
        /// <param name="d">The fourth corner.（第四個角。）</param>
        /// <param name="flip">Whether to reverse the winding.（是否反轉繞向。）</param>
        private static void AddQuad(List<int> indices, int a, int b, int c, int d, bool flip)
        {
            if (flip)
            {
                indices.Add(a); indices.Add(c); indices.Add(b);
                indices.Add(b); indices.Add(c); indices.Add(d);
            }
            else
            {
                indices.Add(a); indices.Add(b); indices.Add(c);
                indices.Add(b); indices.Add(d); indices.Add(c);
            }
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

            if (EntityManager.HasComponent<Game.Net.Elevation>(entity))
            {
                float2 elevation = EntityManager.GetComponentData<Game.Net.Elevation>(entity).m_Elevation;
                record.Attributes["elevation"] = Math.Round((double)((elevation.x + elevation.y) * 0.5f), 3);
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
