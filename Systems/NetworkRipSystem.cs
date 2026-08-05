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
    /// cheap. <c>RipNetworkSpike</c> assembles and bends the pieces into real geometry, which is
    /// what carries a road's thickness, shoulders, kerbs and materials.<br/>
    /// （兩種形式皆源自同一份 CPU 端資料。<see cref="RipNetworks"/> 寫出中心線形式；
    /// <c>RipNetworkSpike</c> 則組裝並彎折部件為真實幾何，
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
        /// Write every network edge in the loaded city into <c>networks.jsonl</c>.
        /// （將已載入城市中的所有路網邊寫出至 <c>networks.jsonl</c>。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipNetworks(string outputDirectory)
        {
            string path = IOUtils.CombinePath(outputDirectory, "networks.jsonl");
            StringBuilder report = new();
            Dictionary<string, int> byCategory = new();
            Dictionary<string, int> byForm = new();
            Dictionary<Entity, string> aggregateNames = new();
            int skippedMarker = 0, skippedNoWidth = 0, skippedDegenerate = 0;
            int points = 0;

            NativeArray<Entity> entities = _edgeQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                report.AppendLine($"Network edges: {entities.Length}");

                using NetworkWriter writer = new(path);

                foreach (Entity entity in entities)
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
                        skippedMarker++;
                        continue;
                    }

                    if (isTaxiway
                        && ((EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition).m_Flags & TaxiwayFlags.Airspace) != 0))
                    {
                        skippedMarker++;
                        continue;
                    }

                    EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                    Edge edge = EntityManager.GetComponentData<Edge>(entity);

                    float width = GetWidth(edgeComposition, entity, geometry);
                    if (!(width > 0f))
                    {
                        // The schema's centerline form requires a positive width, since a consumer
                        // extrudes by it; a zero-width run is nothing anyone can draw.
                        // （結構描述的中心線形式要求正值寬度，因消費端據以擠出；零寬度者無從繪製。）
                        skippedNoWidth++;
                        continue;
                    }

                    List<float3> centerline = BuildCenterline(entity, edge, geometry);
                    if (centerline.Count < 2)
                    {
                        skippedDegenerate++;
                        continue;
                    }

                    NetworkRecord record = new()
                    {
                        Id = entity.Index,
                        Kind = "segment",
                        Centerline = centerline,
                        Width = width
                    };

                    AddAttributes(entity, edgeComposition, width, record, aggregateNames);

                    writer.Write(record);
                    points += centerline.Count;

                    string category = record.Attributes.TryGetValue("category", out object c) ? c.ToString() : "other";
                    byCategory.TryGetValue(category, out int categoryCount);
                    byCategory[category] = categoryCount + 1;

                    string form = record.Attributes.TryGetValue("form", out object f) ? f.ToString() : "normal";
                    byForm.TryGetValue(form, out int formCount);
                    byForm[form] = formCount + 1;
                }

                report.AppendLine($"Wrote {writer.Count} segment(s) to networks.jsonl, {points} centerline point(s)");
            }
            finally
            {
                entities.Dispose();
            }

            foreach (KeyValuePair<string, int> kvp in byCategory) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine("By form:");
            foreach (KeyValuePair<string, int> kvp in byForm) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            if (skippedMarker > 0) report.AppendLine($"Skipped {skippedMarker} marker network(s)");
            if (skippedNoWidth > 0) report.AppendLine($"Skipped {skippedNoWidth} edge(s) with no usable width");
            if (skippedDegenerate > 0) report.AppendLine($"Skipped {skippedDegenerate} edge(s) whose centerline collapsed to a point");
            report.AppendLine($"Output: {path}");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Dump one curved road edge's composition mesh, raw and deformed, so the deform can be
        /// worked out without paying for a full rip (PLAN Phase 5, level 2 spike).<br/>
        /// （傾印一條彎曲道路邊的組成網格，包含原始與變形兩版，
        /// 使變形方式的推導無須付出完整擷取的代價（PLAN 第五階段，第二層驗證）。）<br/><br/>
        /// <b>What is already known, from the game's own code.</b>
        /// <c>BatchDataHelpers.CalculateEdgeParameters</c> fills the eight per-instance
        /// <c>CompositionMatrix</c> values the net shader deforms by, and
        /// <c>BuildEdgeMatrix</c> shows each one is nothing but a bezier's four control points
        /// (relative to the edge's bounds centre) with UV mapping offsets in <c>w</c>. For an edge
        /// they are <c>m_Start.m_Left</c>, <c>m_End.m_Left</c>, <c>m_Start.m_Right</c>,
        /// <c>m_End.m_Right</c>, and matrices 4–7 are identity. So the shader's whole input is
        /// <see cref="EdgeGeometry"/> — data this system already reads. The one thing the managed
        /// assemblies cannot answer is how the mesh encodes its along- and across-parameters,
        /// which is what the ranges below are for.<br/>
        /// （由遊戲自身程式碼已知：八個 <c>CompositionMatrix</c> 各自僅是一條貝茲曲線的四個控制點，
        /// 對邊而言即 <see cref="EdgeGeometry"/> 的四條邊界曲線。唯一無法自受管組件得知者，
        /// 是網格如何編碼其縱向與橫向參數，此即下列數值範圍的用途。）<br/><br/>
        /// A curved edge is chosen deliberately: on a straight one, a wrong deform still looks
        /// plausible.<br/>
        /// （刻意選用彎曲的邊：在筆直的邊上，錯誤的變形看起來仍屬合理。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipNetworkSpike(string outputDirectory)
        {
            StringBuilder report = new();
            NativeArray<Entity> entities = _edgeQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                // Rip the road the player is looking at. Comparing a ripped mesh against the game
                // means finding the same road in both, and CS2 offers no way to fly to a
                // coordinate — so the camera picks the subject instead, and the comparison is
                // already framed before the button is pressed.
                // （擷取玩家正在注視的道路。將擷取的網格與遊戲比對，意味著必須在兩邊找到同一條路，
                //   而 CS2 並無飛往指定座標的方式——因此改由攝影機決定對象，
                //   按下按鈕之前，比對的畫面就已經對好了。）
                bool hasFocus = false;
                float3 focus = default;
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
                    _log.Warn($"Could not read the camera pivot, falling back to the whole city: {ex.Message}");
                }

                report.AppendLine(hasFocus
                    ? $"Camera pivot {focus} — choosing the most curved road within {FocusRadius} m of it."
                    : "No camera available — choosing the most curved road in the city.");

                Entity chosen = Entity.Null;
                Entity chosenComposition = Entity.Null;
                float chosenScore = -1f;
                Entity nearest = Entity.Null;
                Entity nearestComposition = Entity.Null;
                float nearestDistance = float.MaxValue;

                foreach (Entity entity in entities)
                {
                    Composition composition = EntityManager.GetComponentData<Composition>(entity);
                    Entity edgeComposition = composition.m_Edge;
                    if (edgeComposition == Entity.Null) continue;
                    if (EntityManager.HasComponent<Game.Net.Marker>(entity)) continue;

                    string category = GetCategory(EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab, edgeComposition);
                    if ((category != "road") && (category != "highway")) continue;
                    if (!EntityManager.HasComponent<NetCompositionMeshRef>(edgeComposition)) continue;

                    EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                    float3 edgeCenter = Colossal.Mathematics.MathUtils.Center(geometry.m_Bounds);

                    // Horizontal distance only: the pivot sits on the ground and an elevated road
                    // overhead is still the road being looked at.
                    // （僅計水平距離：樞紐點位於地面，而頭頂的高架道路仍是所注視的那條路。）
                    float distance = hasFocus ? math.distance(edgeCenter.xz, focus.xz) : 0f;
                    if (distance < nearestDistance)
                    {
                        nearestDistance = distance;
                        nearest = entity;
                        nearestComposition = edgeComposition;
                    }

                    if (hasFocus && (distance > FocusRadius)) continue;

                    // Prefer an edge that runs into a real intersection, then the most curved of
                    // those. Junction fill is the thing being judged, and a road meeting three or
                    // more others is where a missing flare shows as a hole; curvature is the
                    // tie-breaker because a wrong deform still looks plausible on a straight road.
                    // （優先取駛入真實路口的邊，再於其中取最彎者。受檢驗者為路口的填補，
                    //   而與三條以上道路相接之處，正是缺漏的擴張部會顯現為破洞之處；
                    //   彎曲度作為平手時的判準，因錯誤的變形在筆直道路上看起來仍屬合理。）
                    Edge ends = EntityManager.GetComponentData<Edge>(entity);
                    int connections = math.max(CountConnections(ends.m_Start), CountConnections(ends.m_End));

                    Bezier4x3 mid = Midline(geometry.m_Start);
                    float bend = math.distance(mid.b, math.lerp(mid.a, mid.d, 1f / 3f))
                               + math.distance(mid.c, math.lerp(mid.a, mid.d, 2f / 3f));

                    // A junction is worth more than any amount of bend, so it dominates the score
                    // rather than competing with it.
                    // （路口的價值高於任何彎曲程度，故其於評分中居主導地位而非與之競爭。）
                    float score = ((connections >= 3) ? 1000f : 0f) + bend;

                    if (score > chosenScore)
                    {
                        chosenScore = score;
                        chosen = entity;
                        chosenComposition = edgeComposition;
                    }
                }

                // Nothing in range is not a failure — the camera may simply be over open ground.
                // （範圍內沒有道路並非失敗：攝影機可能只是停在空地上方。）
                if ((chosen == Entity.Null) && (nearest != Entity.Null))
                {
                    chosen = nearest;
                    chosenComposition = nearestComposition;
                    report.AppendLine($"No road within {FocusRadius} m; took the nearest, {nearestDistance:F0} m away.");
                }

                if (chosen == Entity.Null) return "Network spike: no road edge with a composition mesh was found.";

                NetCompositionMeshRef meshRef = EntityManager.GetComponentData<NetCompositionMeshRef>(chosenComposition);
                Entity meshEntity = meshRef.m_Mesh;
                EdgeGeometry chosenGeometry = EntityManager.GetComponentData<EdgeGeometry>(chosen);
                float3 center = Colossal.Mathematics.MathUtils.Center(chosenGeometry.m_Bounds);

                Edge chosenEnds = EntityManager.GetComponentData<Edge>(chosen);
                report.AppendLine($"Start node has {CountConnections(chosenEnds.m_Start)} connected edge(s), "
                                + $"end node {CountConnections(chosenEnds.m_End)} — 3 or more is an intersection.");
                report.AppendLine($"Edge {chosen.Index}, score {chosenScore:F2}, "
                                + $"asset '{Instance.Name.GetPrefabName(EntityManager.GetComponentData<PrefabRef>(chosen).m_Prefab)}'");
                report.AppendLine($"Composition {chosenComposition.Index}, mesh entity {meshEntity.Index}, rotate={meshRef.m_Rotate}");
                report.AppendLine($"Mesh entity components: [{string.Join(", ", DescribeComponents(meshEntity))}]");

                if (EntityManager.HasComponent<NetCompositionMeshData>(meshEntity))
                {
                    NetCompositionMeshData meshData = EntityManager.GetComponentData<NetCompositionMeshData>(meshEntity);
                    report.AppendLine($"NetCompositionMeshData: width {meshData.m_Width:F3}, middleOffset {meshData.m_MiddleOffset:F3}, "
                                    + $"indexFactor {meshData.m_IndexFactor:F3}, heightRange [{meshData.m_HeightRange.min:F3}, {meshData.m_HeightRange.max:F3}], "
                                    + $"state {meshData.m_State}, layers {meshData.m_DefaultLayers}");
                }

                if (EntityManager.HasComponent<NetCompositionData>(chosenComposition))
                {
                    NetCompositionData compositionData = EntityManager.GetComponentData<NetCompositionData>(chosenComposition);
                    report.AppendLine($"NetCompositionData: width {compositionData.m_Width:F3}, middleOffset {compositionData.m_MiddleOffset:F3}, "
                                    + $"widthOffset {compositionData.m_WidthOffset:F3}, nodeOffset {compositionData.m_NodeOffset:F3}");

                    // Whether the boundary curves sit at the drawn road surface, or the surface is
                    // meant to sit below them. The pieces are all authored at y = -0.2, so this
                    // says whether that offset is real or already accounted for.
                    // （邊界曲線是否位於實際繪製的路面高度，抑或路面應在其下方。
                    //   各部件皆製作於 y = -0.2，此處說明該偏移是真實存在，或已被計入。）
                    report.AppendLine($"NetCompositionData surfaceHeight: [{compositionData.m_SurfaceHeight.min:F3}, "
                                    + $"{compositionData.m_SurfaceHeight.max:F3}], heightRange ["
                                    + $"{compositionData.m_HeightRange.min:F3}, {compositionData.m_HeightRange.max:F3}], "
                                    + $"edgeHeights {compositionData.m_EdgeHeights}");
                }

                report.AppendLine($"Segment lengths: start {chosenGeometry.m_Start.m_Length}, end {chosenGeometry.m_End.m_Length}");
                report.AppendLine($"Bounds centre {center}");

                // A composition mesh entity holds no vertices of its own. It is an assembly: a
                // `NetCompositionPiece` buffer naming the cross-section's parts — carriageway,
                // kerb, pavement, barrier — each an ordinary prefab with its own geometry, placed
                // by an offset and a size. That is why a road has sidewalks and markings the
                // centerline form could never carry, and it means net geometry comes through the
                // same proven route as buildings rather than needing anything new.
                // （組成網格實體本身不含頂點。它是一份組裝清單：`NetCompositionPiece` 緩衝區
                //   列出橫斷面的各個部件——車道、緣石、人行道、護欄——每個都是具有自身幾何的
                //   一般預製模板，依偏移與尺寸擺放。）
                if (EntityManager.HasBuffer<NetCompositionPiece>(meshEntity))
                {
                    DynamicBuffer<NetCompositionPiece> pieces = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);
                    report.AppendLine($"NetCompositionPiece buffer: {pieces.Length} piece(s)");

                    for (int i = 0; i < pieces.Length; i++)
                    {
                        NetCompositionPiece piece = pieces[i];
                        string name = Instance.Name.GetPrefabName(piece.m_Piece);

                        report.AppendLine($"  [{i}] '{name}' offset {piece.m_Offset} size {piece.m_Size} "
                                        + $"section {piece.m_SectionIndex} sectionFlags {piece.m_SectionFlags} "
                                        + $"pieceFlags {piece.m_PieceFlags}");

                        if ((piece.m_Piece != Entity.Null) && EntityManager.HasComponent<NetPieceData>(piece.m_Piece))
                        {
                            NetPieceData pieceData = EntityManager.GetComponentData<NetPieceData>(piece.m_Piece);
                            report.AppendLine($"        NetPieceData: width {pieceData.m_Width:F3} length {pieceData.m_Length:F3} "
                                            + $"widthOffset {pieceData.m_WidthOffset:F3} heightRange "
                                            + $"[{pieceData.m_HeightRange.min:F3}, {pieceData.m_HeightRange.max:F3}] "
                                            + $"surfaceHeights {pieceData.m_SurfaceHeights}");
                        }
                    }

                    // `NetPiecePrefab` derives from `RenderPrefab`, so a piece entity *is* the
                    // render prefab — there is no `SubMesh` indirection to follow, which is why
                    // every piece above reports none. The mesh rip's reader takes it directly,
                    // which means road geometry needs no new machinery at all: it is the Phase 1
                    // geometry-asset route and the Phase 4 material route, unchanged.
                    // （`NetPiecePrefab` 衍生自 `RenderPrefab`，故部件實體本身即是繪製預製模板，
                    //   無須經由 `SubMesh` 轉介。網格擷取的讀取器可直接處理，
                    //   意即道路幾何完全無須新機制。）
                    string pieceDirectory = IOUtils.CombinePath(outputDirectory, "networks");

                    for (int i = 0; i < pieces.Length; i++)
                    {
                        Entity pieceEntity = pieces[i].m_Piece;
                        if (pieceEntity == Entity.Null) continue;

                        string pieceName = Instance.Name.GetPrefabName(pieceEntity);
                        List<string> trace = new();
                        MeshAsset pieceMesh = Instance.MeshRip.ReadMesh(pieceEntity, $"net_spike_piece{i}", pieceName, trace);

                        if (pieceMesh == null)
                        {
                            report.AppendLine($"  [{i}] '{pieceName}' geometry would not read: {string.Join("; ", trace)}");
                            continue;
                        }

                        float3 minPiece = pieceMesh.Vertices[0], maxPiece = pieceMesh.Vertices[0];
                        foreach (float3 v in pieceMesh.Vertices) { minPiece = math.min(minPiece, v); maxPiece = math.max(maxPiece, v); }

                        report.AppendLine($"  [{i}] '{pieceName}': {pieceMesh.Vertices.Length} verts, {pieceMesh.TriangleCount} tris, "
                                        + $"{pieceMesh.Primitives.Count} prim, x [{minPiece.x:F3}, {maxPiece.x:F3}] "
                                        + $"y [{minPiece.y:F3}, {maxPiece.y:F3}] z [{minPiece.z:F3}, {maxPiece.z:F3}]");

                        GltfWriter.Write(IOUtils.CombinePath(pieceDirectory, $"net_spike_piece{i}.glb"), pieceMesh);
                    }

                    report.AppendLine($"Wrote net_spike_piece*.glb to {pieceDirectory}");

                    // The edge, plus the junction flare at each end. `Composition` names three
                    // composition entities, and the two node ones carry their own piece lists —
                    // an intersection's surface is genuinely different geometry from the road
                    // leading into it. Assembling all three into one mesh is what closes the
                    // junction gaps, because between them they cover node to node with nothing
                    // left over.
                    // （邊，加上兩端的路口擴張部。`Composition` 指名三個組成實體，
                    //   其中兩個節點組成各有自身的部件清單——路口表面確實是與駛入其中的道路
                    //   截然不同的幾何。將三者組裝為單一網格，正是路口空隙得以閉合之處。）
                    Composition chosenParts = EntityManager.GetComponentData<Composition>(chosen);
                    List<float3> vertices = new();
                    List<float2> uvs = new();
                    List<int> indices = new();
                    List<MeshPrimitive> primitives = new();

                    if (EntityManager.HasComponent<StartNodeGeometry>(chosen))
                    {
                        EdgeNodeGeometry node = EntityManager.GetComponentData<StartNodeGeometry>(chosen).m_Geometry;
                        AppendComposition(chosenParts.m_StartNode, node.m_Left, node.m_Right, true, center,
                                          "startNode", vertices, uvs, indices, primitives, report);
                    }

                    AppendComposition(chosenParts.m_Edge, chosenGeometry.m_Start, chosenGeometry.m_End, false, center,
                                      "edge", vertices, uvs, indices, primitives, report);

                    if (EntityManager.HasComponent<EndNodeGeometry>(chosen))
                    {
                        EdgeNodeGeometry node = EntityManager.GetComponentData<EndNodeGeometry>(chosen).m_Geometry;
                        AppendComposition(chosenParts.m_EndNode, node.m_Left, node.m_Right, true, center,
                                          "endNode", vertices, uvs, indices, primitives, report);
                    }

                    MeshAsset assembled = Finish("net_spike_edge", vertices, uvs, indices, primitives);
                    if (assembled != null)
                    {
                        long bytes = GltfWriter.Write(IOUtils.CombinePath(pieceDirectory, "net_spike_edge.glb"), assembled);
                        report.AppendLine($"Wrote net_spike_edge.glb ({bytes} bytes): {assembled.Vertices.Length} verts, "
                                        + $"{assembled.TriangleCount} tris, {assembled.Primitives.Count} primitive(s)");
                        report.AppendLine($"Place it at world position {center} with identity rotation.");
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
                                List<float3> vertices, List<float2> uvs, List<int> indices,
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
                MeshAsset source = Instance.MeshRip.ReadMesh(piece.m_Piece, $"{label}_p{p}", pieceName);
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
                    MaterialId = $"{pieceName}_mat0"
                });

                report.AppendLine($"  {label} [{p}] '{pieceName}': {tiles} tile(s) of {tileLength:F1} m "
                                + $"over {edgeLength:F1} m, widthScale {widthScale:F3}");
            }
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
                                       float3 center, string label, List<float3> vertices, List<float2> uvs,
                                       List<int> indices, List<MeshPrimitive> primitives, StringBuilder report)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return;

            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return;

            float width = EntityManager.HasComponent<NetCompositionData>(composition)
                ? EntityManager.GetComponentData<NetCompositionData>(composition).m_Width : 0f;
            if (!(width > 0f)) return;

            AppendPart(EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true), half0, half1,
                       splitAcross, width, center, label, vertices, uvs, indices, primitives, report);
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
        /// List the component types present on an entity, for the spike's diagnostics.
        /// （列出實體上存在的元件型別，供驗證診斷使用。）
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
