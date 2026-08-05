using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
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
    /// <b>No <c>kind: "node"</c> records.</b> CS2 keeps no junction geometry on the node: the flare
    /// where an edge meets an intersection is stored on the <i>edge</i>, as
    /// <c>StartNodeGeometry</c> / <c>EndNodeGeometry</c>. A junction is therefore the union of the
    /// segment ends meeting there, which is exactly what every segment's centerline running
    /// node-to-node already expresses. Emitting node records would either duplicate that or invent
    /// geometry the game does not have.<br/>
    /// （<b>不輸出 <c>kind: "node"</c> 紀錄。</b>CS2 未在節點上保存路口幾何：邊與路口相接處的
    /// 擴張形狀存於<i>邊</i>之上。路口即為交會於該處的各區段端點之聯集，
    /// 而每個區段的中心線本就自節點延伸至節點。）
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
        /// The query for placed network edges.（已放置路網邊的查詢。）
        /// </summary>
        EntityQuery _edgeQuery;

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
            public int SkippedMarker, SkippedNoWidth, SkippedDegenerate;
            public int Points, Triangles;
            public long Bytes;
        }

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
        /// <returns>What the caller should tell the user.（呼叫端應告知使用者的訊息。）</returns>
        public string BeginRip(string outputDirectory)
        {
            if (_run != null) return "A network rip is already running.";

            RipProgress.Report(RipProgress.Phase.Scan);

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
        /// Rip one network edge into the run.（將單一路網邊擷取至該次工作中。）
        /// </summary>
        /// <param name="run">The rip in flight.（進行中的擷取。）</param>
        /// <param name="entity">The edge to rip.（待擷取的邊。）</param>
        private void RipOne(NetworkRun run, Entity entity)
        {
                {
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

            StringBuilder report = run.Report;
            report.AppendLine($"Wrote {run.Writer.Count} segment(s) to networks.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
            report.AppendLine($"  mesh form: {run.Writer.MeshForm} ({run.Triangles} triangles, "
                            + $"{run.Bytes / 1048576d:F1} MiB of glTF in networks/)");
            report.AppendLine($"  centerline form: {run.Writer.CenterlineForm} ({run.Points} point(s))");
            report.AppendLine($"Distinct cross-section pieces read: {run.PieceCache.Count}");

            foreach (KeyValuePair<string, int> kvp in run.ByCategory) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine("By form:");
            foreach (KeyValuePair<string, int> kvp in run.ByForm) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            if (run.SkippedMarker > 0) report.AppendLine($"Skipped {run.SkippedMarker} marker network(s)");
            if (run.SkippedNoWidth > 0) report.AppendLine($"Skipped {run.SkippedNoWidth} edge(s) with no usable width");
            if (run.SkippedDegenerate > 0) report.AppendLine($"Skipped {run.SkippedDegenerate} edge(s) whose centerline collapsed to a point");
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

                int tiles = math.max(1, (int)math.round(edgeLength / tileLength));

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

            AppendPart(EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true), half0, half1,
                       splitAcross, width, center, label, cache, vertices, uvs, indices, primitives, report);
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
