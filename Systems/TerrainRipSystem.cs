using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using Game.Simulation;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Unity.Entities;
using Unity.Mathematics;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// The system that rips the terrain, water and ground-surface layers (PLAN Phase 6).
    /// （擷取地形、水體與地表層的系統（PLAN 第六階段）。）<br/><br/>
    /// <b>The heightmap is the final, road-flattened ground.</b> <c>TerrainSystem</c>'s road job
    /// flattens the height cascade under every net whose prefab carries
    /// <c>GeometryFlags.FlattenTerrain</c>/<c>ClipTerrain</c>, and <c>GetHeightData()</c> reads
    /// back exactly that cascade slice (<c>baseLod</c>) — the base heightmap is blitted in and
    /// the lot/lane/area height adjustments are drawn over it before the readback is requested.
    /// The elevation this system writes therefore already contains the flattened final ground.
    /// (It is the ground <i>beneath</i> junctions, not their visible surface — the arms' node
    /// meshes pave the interior; audit §18. An earlier revision claimed the heightmap closed the
    /// junctions; that was the §18 misreading.)<br/>
    /// （<b>高度圖即道路壓平後的最終地面。</b><c>TerrainSystem</c> 的道路作業於每個預製模板帶有
    /// <c>GeometryFlags.FlattenTerrain</c>／<c>ClipTerrain</c> 的路網之下壓平高度串接紋理，
    /// 而 <c>GetHeightData()</c> 回讀的正是該串接切片。故本系統寫出的高程已含壓平後的最終地面。
    /// （其為路口<i>之下</i>的地面而非其可見表面——各臂的節點網格鋪滿內部；審計 §18。
    /// 先前版本稱高度圖可閉合路口，即 §18 所載之誤讀。））<br/><br/>
    /// <b>Why the roadbed mask is stamped from net geometry.</b> The terrain material has no
    /// roadbed texture and no roadbed mask — its grounds are grass, dirt, rock and four paintable
    /// extras, classified per-pixel on the GPU. What makes flattened ground read as roadbed
    /// in-game is the flattening itself (plus the ~13%-alpha lane decals). The truthful mask is
    /// therefore the same footprint the road job draws: every edge segment, junction-node strip
    /// and orphan cap of a Flatten/ClipTerrain net, which this system rasterises on the CPU from
    /// the same <c>Segment</c> béziers (<c>CullRoadsJob.AddEdge/AddNode/AddOrphans</c>,
    /// decompiled).<br/>
    /// （<b>為何自路網幾何蓋印路床遮罩。</b>地形材質沒有路床貼圖也沒有路床遮罩——其地面僅有草、土、
    /// 岩與四個可繪製的額外層，於 GPU 逐像素分類。使壓平地面在遊戲中呈現路床樣貌的，正是壓平本身
    /// （加上約 13% 不透明度的車道貼花）。故誠實的遮罩即道路作業所繪的同一足跡：
    /// Flatten／ClipTerrain 路網的每個邊區段、路口節點條帶與孤立節點蓋，
    /// 本系統以相同的 <c>Segment</c> 貝茲曲線於 CPU 上將其光柵化。）<br/><br/>
    /// <b>Why a cutout mask exists at all.</b> The game never carves tunnel portals or sunken
    /// trenches out of the heightmap. A Lowered composition's ground is merely clamped into
    /// <c>[lane, lane + ElevationLimit·3]</c> and a shallow tube's cover is shifted <i>up</i> —
    /// the covering terrain stays in the height cascade, and every frame the <c>RoadClip</c>
    /// stencil pass (<c>GenerateClipDataJob</c> → <c>TerrainSurface.RenderClipAreas</c>) discards
    /// its pixels on screen instead. A consumer that rebuilds the ground from the heightmap alone
    /// therefore walls off every tunnel mouth and trench; <c>terrain/cutout.png</c> is that
    /// stencil pass projected top-down, and consumers must delete the cells it marks.<br/>
    /// （<b>為何需要裁切遮罩。</b>遊戲從不將隧道洞口或下沉壕溝自高度圖鑿除：Lowered 組成的地面僅被
    /// 鉗制於 <c>[車道, 車道 + ElevationLimit·3]</c>，淺埋管段的覆土則被向<i>上</i>推移——覆蓋的
    /// 地形留在高度串接紋理中，由 <c>RoadClip</c> 模板繪次每影格於螢幕上捨棄其像素。僅憑高度圖
    /// 重建地面的消費端因此會封死每個洞口與壕溝；<c>terrain/cutout.png</c> 即該繪次的俯視投影，
    /// 消費端必須刪除其標記的格。）
    /// </summary>
    public partial class TerrainRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The no-data sentinel of the sim-grid water level scratch array. A dry cell carries no
        /// surface elevation at all — <c>0f</c> would be a real height on maps whose terrain
        /// crosses zero metres.
        /// （模擬網格水面高程暫存陣列的無資料哨兵值。乾格全無水面高程——在地形跨越零公尺的地圖上，
        /// <c>0f</c> 會是真實高度。）
        /// </summary>
        const float NoLevel = float.MinValue;

        /// <summary>
        /// How far above the lane surface terrain must stand before the cutout removes it. The
        /// game's clip band reaches 0.3 m <i>below</i> the lane and so clips the roadbed itself —
        /// in-game the road mesh hides that hole. The bundle keeps the bed a consumer drives on
        /// and cuts only terrain that would block the way; 0.5 m clears curb heights and the
        /// readback's quantisation with room to spare.
        /// （地形須高出車道面多少才會被裁切。遊戲的裁切帶向下達車道<i>之下</i> 0.3 公尺，連路床
        /// 一併裁去——遊戲中道路網格遮住該洞。束保留消費端行駛其上的路床，僅裁去會擋路的地形；
        /// 0.5 公尺足以避開路緣高度與回讀的量化誤差。）
        /// </summary>
        const float CutoutFloor = 0.5f;

        /// <summary>The query for placed network edges.（已放置路網邊的查詢。）</summary>
        EntityQuery _edgeQuery;

        /// <summary>The query for orphan nodes.（孤立節點的查詢。）</summary>
        EntityQuery _orphanQuery;

        /// <summary>The query for rendered surface areas.（已繪製地表區域的查詢。）</summary>
        EntityQuery _surfaceAreaQuery;

        /// <summary>The query for terrain-clipping areas.（裁切地形的區域的查詢。）</summary>
        EntityQuery _clipAreaQuery;

        // Stamp tallies for the report.（供報告使用的蓋印統計。）
        int _edgesStamped, _nodesStamped, _orphansStamped, _skippedForm, _skippedNoFlatten;
        int _cutEdges, _cutNodes, _cutOrphans, _cutAreas, _cutRoundaboutApprox;

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
                    ComponentType.ReadOnly<Edge>(),
                    ComponentType.ReadOnly<EdgeGeometry>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
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
                    ComponentType.ReadOnly<Game.Net.OutsideConnection>(),
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            _surfaceAreaQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Areas.Surface>(),
                    ComponentType.ReadOnly<Game.Areas.Node>(),
                    ComponentType.ReadOnly<Game.Areas.Triangle>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            // Areas whose prefab carries ClipArea (a SurfacePrefab add-on): the terrain job draws
            // them into the same RoadClip stencil (its areaClipMesh path) instead of flattening.
            // （預製模板帶有 ClipArea 的區域：地形作業將其繪入同一 RoadClip 模板（areaClipMesh
            //   路徑）而非壓平。）
            _clipAreaQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Areas.Clip>(),
                    ComponentType.ReadOnly<Game.Areas.Node>(),
                    ComponentType.ReadOnly<Game.Areas.Triangle>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            base.OnCreate();
            _log.Debug("TerrainRipSystem instance created. 地形擷取系統實例創造完成。");
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
        /// Rip the terrain layers into <c>terrain/</c>: the final (road-flattened) heightmap, the
        /// roadbed surface index, the cutout mask (the cells the game's clip pass hides — tunnel
        /// portals, sunken trenches), the water surface level grid, the theme's ground textures
        /// and the geo-referencing sidecar. Contract §8 of <c>docs/bundle-schema.md</c>.
        /// （將地形層擷取至 <c>terrain/</c>：最終（道路壓平後）高度圖、路床地表索引、裁切遮罩
        /// （遊戲裁切繪次隱藏的格——隧道洞口、下沉壕溝）、水面高程網格、
        /// 主題的地面貼圖與地理參照附檔。契約見 <c>docs/bundle-schema.md</c> §8。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipTerrain(string outputDirectory)
        {
            StringBuilder report = new();
            string terrainDirectory = IOUtils.CombinePath(outputDirectory, "terrain");
            if (!Directory.Exists(terrainDirectory)) Directory.CreateDirectory(terrainDirectory);

            // ---- 1. Final heights: the cascade readback, waited for so the last road edit is in.
            // （最終高度：串接紋理的回讀，等待完成使最後的道路編輯亦包含在內。）
            TerrainHeightData heightData = Instance.Terrain.GetHeightData(waitForPending: true);
            if (!heightData.isCreated || (heightData.resolution.x <= 2))
            {
                return "Terrain rip failed: no height data is resident (is a city loaded?).";
            }

            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            ushort[] heights = heightData.heights.ToArray();
            float3 scale = heightData.scale;
            float3 offset = heightData.offset;

            report.AppendLine($"Terrain: {width}x{height} height samples, cell {1f / scale.x:F3} m, "
                            + $"height range {-offset.y:F1}..{(65535f / scale.y) - offset.y:F1} m");

            // ---- 2. The roadbed mask, stamped from the same net segments the game flattens with.
            // （路床遮罩：自遊戲用以壓平的同一批路網區段蓋印。）
            byte[] surfaceIndex = new byte[width * height];
            _edgesStamped = _nodesStamped = _orphansStamped = _skippedForm = _skippedNoFlatten = 0;
            StampRoadbed(surfaceIndex, heightData);

            int roadbedCells = 0;
            foreach (byte cell in surfaceIndex) if (cell != 0) roadbedCells++;
            report.AppendLine($"Roadbed: {_edgesStamped} edge(s), {_nodesStamped} node strip(s), {_orphansStamped} orphan cap(s) stamped; "
                            + $"{roadbedCells} cell(s) = {roadbedCells * 100.0 / surfaceIndex.Length:F1}% of the map");
            report.AppendLine($"  skipped {_skippedNoFlatten} edge(s) whose prefab does not flatten terrain, "
                            + $"{_skippedForm} elevated/tunnel composition(s) (their ground is not pulled to the road)");

            // ---- 2b. The terrain cutout, stamped from the game's clip sections. The game never
            // carves these holes into the heightmap: every frame the RoadClip stencil pass
            // discards the terrain pixels covering tunnel portals and sunken trenches, so the
            // readback legitimately contains ground that blocks them. This mask is that pass
            // projected top-down — a cell is cut where a clip section's ribbon crosses it and the
            // terrain stands above the lane within the section's clip band.
            // （地形裁切遮罩：自遊戲的裁切區段蓋印。遊戲從不在高度圖上開這些洞——每一影格
            //   RoadClip 模板繪次都會捨棄覆蓋隧道洞口與下沉壕溝的地形像素，故回讀本就含有
            //   擋住它們的地面。此遮罩即該繪次的俯視投影——凡裁切區段的帶狀範圍經過、且地形
            //   高於車道並落於該區段裁切帶內的格，皆被裁去。）
            byte[] cutout = new byte[width * height];
            _cutEdges = _cutNodes = _cutOrphans = _cutAreas = _cutRoundaboutApprox = 0;
            StampCutout(cutout, heightData);

            int cutCells = 0;
            foreach (byte cell in cutout) if (cell != 0) cutCells++;
            report.AppendLine($"Cutout: {cutCells} cell(s) opened over tunnel portals and sunken roads; "
                            + $"{_cutEdges} edge(s), {_cutNodes} node strip(s), {_cutOrphans} orphan cap(s), {_cutAreas} clip area(s) stamped");
            if (_cutRoundaboutApprox > 0)
                report.AppendLine($"  APPROXIMATED: {_cutRoundaboutApprox} roundabout node(s) cut with raw composition "
                                + "flags (the game's AddNode state-shedding and ring trims omitted — can over-cut, never under)");

            // ---- 3. Water surface LEVEL, built on the sim grid and resampled onto the terrain
            // grid. Depth was resampled directly once, and the river climbed its gorge: bilinear
            // depth from the ~28 m sim grid, added to the much finer ground, reads a mid-channel
            // depth halfway up a cliff face. Level (ground at the sim cell + depth) is what the
            // water surface actually is, and it is smooth across the shoreline in the way depth
            // is not.
            // （水面「高程」：於模擬網格建立，再重採樣至地形網格。曾直接重採樣水深，結果河水爬上峽谷：
            //   自約 28 公尺的模擬網格雙線性取得的水深，加到精細得多的地面上，會把河道中央的深度
            //   讀到崖面半腰。高程（模擬格處的地面加水深）才是水面本身，且其於岸線兩側平滑，
            //   水深則否。）
            ushort[] waterLevel = BuildWaterLevel(heightData, out int wetCells, out float maxDepth);
            report.AppendLine((waterLevel != null)
                ? $"Water: {wetCells} wet cell(s), max depth {maxDepth:F1} m (surface level grid, heightmap encoding)"
                : "Water: no simulation data resident; layer omitted");

            // ---- 4. The theme's ground textures, read off the live global bindings.
            // （主題的地面貼圖：讀取現行的全域繫結。）
            Dictionary<string, string> surfaceTextures = CaptureSurfaceTextures(terrainDirectory, report);

            // ---- 4a. The game's own ground blend: the splat weight map the terrain shader
            // samples per pixel. Ripping it replaces the consumer's per-cell classification —
            // the "square splotches" — with the game's smooth per-pixel mix, painted surfaces
            // and noise included. GPU readback happens here on the main thread; the PNG encode
            // joins the writer task below.
            // （遊戲自身的地面混合：地形著色器逐像素取樣的權重貼圖。擷取之即以遊戲平滑的
            //   逐像素混合（含手繪地表與雜訊）取代消費端的逐格分類——「方形色塊」。
            //   GPU 回讀於主執行緒此處進行；PNG 編碼併入下方的寫出工作。）
            PendingTexture splatPixels = CaptureSplatmap(out Dictionary<string, object> splatBlock, report);
            PendingTexture userSplatPixels = CaptureUserSplatmap(out Dictionary<string, object> userSplatBlock, report);

            // ---- 4b. The backdrop terrain that fills the horizon beyond the playable border.
            // （4b. 可遊玩邊界之外、構成地平線的背景地形。）
            ushort[] worldHeights = CaptureWorldHeightmap(out Dictionary<string, object> worldBlock,
                                                          out int worldWidth, out int worldHeight, report);

            // ---- 4b. Surface areas — the pavement patches nothing else carries.
            // （地表區域——其餘各層皆未攜帶的鋪面區塊。）
            RipSurfaceAreas(outputDirectory, terrainDirectory, report);

            // ---- 5. Encode and write everything off the main thread.
            // （於主執行緒之外編碼並寫出。）
            float cellSize = 1f / scale.x;
            var rasters = new SortedDictionary<string, object>
            {
                // The manifest `rasters` block, verbatim (contract §3): top-left cell centre,
                // rows toward -Z. The stamp arrays run south-to-north, so rows are flipped at
                // encode time below.
                // （即宣告檔的 `rasters` 區塊（契約 §3）：左上格心，列朝 −Z。蓋印陣列由南至北，
                //   故於下方編碼時翻轉列序。）
                ["resolution"] = Math.Round(cellSize, 6),
                ["size"] = new[] { width, height },
                ["originWorld"] = new[]
                {
                    Math.Round(-offset.x, 3), 0.0,
                    Math.Round(((height - 1) / scale.z) - offset.z, 3)
                },
                ["elevationUnits"] = "m",
                ["elevationScale"] = 1.0 / scale.y,
                ["elevationOffset"] = (double)(-offset.y)
            };
            if (splatBlock != null) rasters["splatmap"] = splatBlock;
            if (userSplatBlock != null) rasters["userSplatmap"] = userSplatBlock;
            if (worldBlock != null) rasters["worldTerrain"] = worldBlock;

            try
            {
                Task writer = Task.Run(() =>
                {
                    File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "heightmap.png"),
                                       Png.EncodeGray16(FlipRows(heights, width, height), width, height));
                    File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "surface_index.png"),
                                       Png.EncodeGray8(FlipRows(surfaceIndex, width, height), width, height));
                    File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "cutout.png"),
                                       Png.EncodeGray8(FlipRows(cutout, width, height), width, height));
                    if (waterLevel != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "water_level.png"),
                                           Png.EncodeGray16(FlipRows(waterLevel, width, height), width, height));
                    }
                    if (splatPixels != null)
                    {
                        // Capture already returns rows top-first, and the splat's V axis runs
                        // south→north, so row 0 is the north edge — the rasters convention —
                        // with no further flip.
                        // （Capture 回傳的列已由上而下，而 splat 的 V 軸由南向北，
                        //   故列 0 即北緣——正是 rasters 慣例——無需再翻轉。）
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "splatmap.png"),
                                           Png.EncodeRgba(splatPixels.Pixels, splatPixels.Width, splatPixels.Height));
                    }
                    if (userSplatPixels != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "user_splatmap.png"),
                                           Png.EncodeRgba(userSplatPixels.Pixels, userSplatPixels.Width, userSplatPixels.Height));
                    }
                    if (worldHeights != null)
                    {
                        // The GPU readback hands rows V=0-first (south), like the stamp arrays.
                        // （GPU 讀回的列以 V=0（南）為先，一如蓋印陣列。）
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "world_heightmap.png"),
                                           Png.EncodeGray16(FlipRows(worldHeights, worldWidth, worldHeight),
                                                            worldWidth, worldHeight));
                    }

                    File.WriteAllText(IOUtils.CombinePath(terrainDirectory, "rasters.json"),
                                      JsonConvert.SerializeObject(rasters, Formatting.Indented));

                    // Surface id → name and texture; contract §8. Id 0 is the ground the game
                    // classifies procedurally (grass/dirt/rock by slope and noise); id 1 is
                    // road-flattened ground, which in-game reads as dirt under the lane decals'
                    // wear tint — so dirt is its representative texture.
                    // （地表識別碼 → 名稱與貼圖；契約 §8。識別碼 0 為遊戲程序化分類的地面；
                    //   識別碼 1 為道路壓平的地面，於遊戲中呈現為車道貼花磨耗色之下的泥土，
                    //   故以泥土為其代表貼圖。）
                    var index = new SortedDictionary<string, object>
                    {
                        ["0"] = new Dictionary<string, string>
                        {
                            ["name"] = "natural",
                            ["texture"] = surfaceTextures.TryGetValue("grass", out string grass) ? grass : null
                        },
                        ["1"] = new Dictionary<string, string>
                        {
                            ["name"] = "roadbed",
                            ["texture"] = surfaceTextures.TryGetValue("dirt", out string dirt) ? dirt : null
                        }
                    };
                    string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
                    if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);
                    File.WriteAllText(IOUtils.CombinePath(surfacesDirectory, "index.json"),
                                      JsonConvert.SerializeObject(index, Formatting.Indented));
                });
                writer.Wait();
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Write failed: {ex.Message}");
            }

            report.AppendLine($"Output: {terrainDirectory}");
            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Reverse the row order of a raster — the stamp arrays run south-to-north (the game's
        /// <c>z</c>), the contract's rasters run north-to-south (row 0 toward −Z).
        /// （反轉網格的列序——蓋印陣列由南至北（遊戲的 <c>z</c>），契約的網格由北至南。）
        /// </summary>
        private static T[] FlipRows<T>(T[] grid, int width, int height)
        {
            T[] flipped = new T[grid.Length];
            for (int y = 0; y < height; y++)
            {
                Array.Copy(grid, y * width, flipped, (height - 1 - y) * width, width);
            }
            return flipped;
        }

        /// <summary>
        /// Build the water <i>surface level</i> grid: per wet sim cell, terrain height at that
        /// cell plus depth; dilated one sim cell; resampled bilinearly onto the terrain grid.
        /// （建立水面<i>高程</i>網格：每個含水模擬格取該格地面高度加水深，向外擴張一格，
        /// 再雙線性重採樣至地形網格。）<br/><br/>
        /// <b>Why level and not depth.</b> Depth lives on the ~28 m simulation grid; the ground it
        /// was measured against is that same coarse grid. Adding coarse depth to the ~3.5 m
        /// terrain grid's fine ground re-bases every sample onto ground the simulation never saw —
        /// against a gorge wall, mid-channel depth lands halfway up the cliff and the river floods
        /// its gorge. The surface level is the physical quantity that is actually smooth across a
        /// shoreline, so it is the one that survives bilinear resampling.<br/>
        /// （<b>為何取高程而非水深。</b>水深屬於約 28 公尺的模擬網格，其所對照的地面亦為該粗網格。
        /// 將粗水深加到約 3.5 公尺地形網格的精細地面上，等於把每個樣本重新架在模擬未曾見過的
        /// 地面上——貼著峽谷壁，河道中央的水深會落在崖面半腰，河水便淹沒峽谷。水面高程才是
        /// 真正跨越岸線仍然平滑的物理量，故唯有它經得起雙線性重採樣。）<br/><br/>
        /// <b>The one-cell dilation</b> keeps the shoreline honest: without it, a terrain cell
        /// between a wet sim cell and a dry one would lerp level against the dry cell's "no data"
        /// and drag the surface down right where the consumer needs it to cross the ground. The
        /// dilated ring copies its highest wet neighbour, so the surface extends flat past the
        /// shoreline and the ground plane cuts it where the game's own shoreline is.
        /// （<b>向外擴張一格</b>使岸線誠實：若無此步，介於含水與乾燥模擬格之間的地形格會把高程與
        /// 乾格的「無資料」內插，恰在消費端需要水面與地面相交之處將其拉低。擴張環帶取其最高
        /// 含水鄰格之值，水面因而平直延伸越過岸線，由地面於遊戲自身的岸線處將其切開。）
        /// </summary>
        /// <param name="heightData">The terrain grid to sample ground from and resample onto.（供取樣地面並作為重採樣目標的地形網格。）</param>
        /// <param name="wetCells">Receives how many terrain cells the level exceeds the ground in.（接收高程高於地面的地形格數。）</param>
        /// <param name="maxDepth">Receives the deepest level-over-ground.（接收高程超出地面的最大值。）</param>
        /// <returns>The level grid in the heightmap's encoding (0 = dry), or <c>null</c> without data.（以高度圖編碼表示的高程網格（0 為乾）；無資料則為 <c>null</c>。）</returns>
        private ushort[] BuildWaterLevel(TerrainHeightData heightData, out int wetCells, out float maxDepth)
        {
            wetCells = 0;
            maxDepth = 0f;

            WaterSurfaceData<SurfaceWater> water = Instance.Water.GetSurfaceData(out Unity.Jobs.JobHandle deps);
            deps.Complete();
            if (!water.depths.IsCreated || (water.resolution.x <= 0)) return null;

            int simWidth = water.resolution.x;
            int simHeight = water.resolution.z;

            // ---- Level per wet sim cell, on the ground the simulation itself sits on.
            // （每個含水模擬格的高程，架於模擬自身所處的地面。）
            float[] level = new float[simWidth * simHeight];
            for (int i = 0; i < level.Length; i++) level[i] = NoLevel;

            for (int z = 0; z < simHeight; z++)
            {
                // Sim cell -> world, the inverse of the sampling map below
                // (world = cell / scale - offset).
                // （模擬格 → 世界，即下方取樣映射之逆。）
                float worldZ = (z / water.scale.z) - water.offset.z;
                for (int x = 0; x < simWidth; x++)
                {
                    // The game's own decode (`WaterUtils.ToWorldSpace(float surfaceDepth)`):
                    // metres = raw / scale.y − offset.y. The raw grid value is not metres.
                    // （遊戲自身的解碼：公尺 = 原始值 / scale.y − offset.y。網格原始值並非公尺。）
                    float depth = (water.depths[(z * simWidth) + x].m_Depth / water.scale.y) - water.offset.y;
                    if (!(depth > 0f)) continue;

                    float worldX = (x / water.scale.x) - water.offset.x;
                    level[(z * simWidth) + x] = SampleTerrainHeight(heightData, worldX, worldZ) + depth;
                }
            }

            // ---- Dilate one sim cell: each dry cell bordering water takes its highest wet
            // neighbour.（向外擴張一格：鄰接水體的乾格取其最高含水鄰格之值。）
            float[] dilated = (float[])level.Clone();
            for (int z = 0; z < simHeight; z++)
            {
                for (int x = 0; x < simWidth; x++)
                {
                    if (level[(z * simWidth) + x] != NoLevel) continue;

                    float best = NoLevel;
                    for (int dz = -1; dz <= 1; dz++)
                    {
                        int nz = z + dz;
                        if ((nz < 0) || (nz >= simHeight)) continue;
                        for (int dx = -1; dx <= 1; dx++)
                        {
                            int nx = x + dx;
                            if ((nx < 0) || (nx >= simWidth)) continue;
                            best = math.max(best, level[(nz * simWidth) + nx]);
                        }
                    }
                    if (best != NoLevel) dilated[(z * simWidth) + x] = best;
                }
            }

            // ---- Resample onto the terrain grid. Corners without data drop out of the lerp by
            // weight, rather than dragging the surface toward a sentinel.
            // （重採樣至地形網格。無資料的角以權重剔除，而非將水面拉向哨兵值。）
            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            ushort[] grid = new ushort[width * height];

            for (int z = 0; z < height; z++)
            {
                float worldZ = (z / heightData.scale.z) - heightData.offset.z;
                float waterZ = (worldZ + water.offset.z) * water.scale.z;
                int z0 = math.clamp((int)math.floor(waterZ), 0, simHeight - 1);
                int z1 = math.min(z0 + 1, simHeight - 1);
                float tz = math.saturate(waterZ - z0);

                for (int x = 0; x < width; x++)
                {
                    float worldX = (x / heightData.scale.x) - heightData.offset.x;
                    float waterX = (worldX + water.offset.x) * water.scale.x;
                    int x0 = math.clamp((int)math.floor(waterX), 0, simWidth - 1);
                    int x1 = math.min(x0 + 1, simWidth - 1);
                    float tx = math.saturate(waterX - x0);

                    float sum = 0f, weightSum = 0f;
                    AddCorner(dilated[(z0 * simWidth) + x0], (1f - tx) * (1f - tz), ref sum, ref weightSum);
                    AddCorner(dilated[(z0 * simWidth) + x1], tx * (1f - tz), ref sum, ref weightSum);
                    AddCorner(dilated[(z1 * simWidth) + x0], (1f - tx) * tz, ref sum, ref weightSum);
                    AddCorner(dilated[(z1 * simWidth) + x1], tx * tz, ref sum, ref weightSum);
                    if (!(weightSum > 0f)) continue;

                    float cellLevel = sum / weightSum;

                    // The heightmap's own encoding, with 0 reserved for dry — a real level that
                    // would encode to 0 sits at the map's absolute height floor and cannot hold
                    // water anyway.
                    // （高度圖自身的編碼，0 保留給乾格——會編碼為 0 的真實高程位於地圖的絕對高度
                    //   下限，本就無法蓄水。）
                    grid[(z * width) + x] = (ushort)math.clamp(
                        math.round((cellLevel + heightData.offset.y) * heightData.scale.y), 1f, 65535f);

                    float ground = heightData.heights[(z * width) + x] / heightData.scale.y - heightData.offset.y;
                    if (cellLevel > ground)
                    {
                        wetCells++;
                        maxDepth = math.max(maxDepth, cellLevel - ground);
                    }
                }
            }

            return grid;
        }

        /// <summary>
        /// Accumulate one bilinear corner into the weighted sum, unless it carries no data.
        /// （將單一雙線性角累加入加權和，無資料者除外。）
        /// </summary>
        private static void AddCorner(float value, float weight, ref float sum, ref float weightSum)
        {
            if (value == NoLevel) return;
            sum += value * weight;
            weightSum += weight;
        }

        /// <summary>
        /// Terrain height in metres at a world position, bilinear over the height samples —
        /// <c>TerrainUtils.SampleHeight</c>'s mapping.
        /// （世界座標處的地形高度（公尺），於高度樣本上雙線性取樣——即
        /// <c>TerrainUtils.SampleHeight</c> 的映射。）
        /// </summary>
        private static float SampleTerrainHeight(TerrainHeightData heightData, float worldX, float worldZ)
        {
            float cellX = (worldX + heightData.offset.x) * heightData.scale.x;
            float cellZ = (worldZ + heightData.offset.z) * heightData.scale.z;

            int x0 = math.clamp((int)math.floor(cellX), 0, heightData.resolution.x - 1);
            int x1 = math.min(x0 + 1, heightData.resolution.x - 1);
            float tx = math.saturate(cellX - x0);
            int z0 = math.clamp((int)math.floor(cellZ), 0, heightData.resolution.z - 1);
            int z1 = math.min(z0 + 1, heightData.resolution.z - 1);
            float tz = math.saturate(cellZ - z0);

            float raw = math.lerp(
                math.lerp(heightData.heights[(z0 * heightData.resolution.x) + x0],
                          heightData.heights[(z0 * heightData.resolution.x) + x1], tx),
                math.lerp(heightData.heights[(z1 * heightData.resolution.x) + x0],
                          heightData.heights[(z1 * heightData.resolution.x) + x1], tx), tz);

            return (raw / heightData.scale.y) - heightData.offset.y;
        }

        /// <summary>
        /// Rip every rendered surface area — driveways, parking-lot pavement, park paths, the
        /// painted patches under meshless service buildings — into <c>areas.jsonl</c> +
        /// <c>areaprefabs.json</c>. Contract §8b.
        /// （擷取每個已繪製的地表區域——車道口、停車場鋪面、公園步道、無網格服務建築下的
        /// 鋪繪區塊——寫入 <c>areas.jsonl</c> 與 <c>areaprefabs.json</c>。契約 §8b。）<br/><br/>
        /// <b>Why this layer exists.</b> A parking lot is a <i>meshless</i> building prefab: its
        /// instance record places nothing visible, its markings are §4.2a decal instances, and the
        /// pavement itself is a <c>SurfacePrefab</c> area the building spawns as a sub-area —
        /// triangulated on the area grid (<c>Game.Areas.Node</c>/<c>Triangle</c> buffers) and drawn
        /// by <c>AreaBatchSystem</c> with the prefab's <c>RenderedArea.m_Material</c> (falling back
        /// to the global <c>AreasConfigurationPrefab.m_RenderedAreaMaterial</c>). House driveways
        /// are the same mechanism. Without this layer those surfaces simply do not exist in the
        /// bundle, which is why every reconstruction showed grass where the game shows pavement.
        /// （<b>此層為何存在。</b>停車場是<i>無網格</i>的建築預製模板：其實例紀錄不放置任何可見物，
        /// 其標線為 §4.2a 的貼花實例，鋪面本身則是建築以子區域生成的 <c>SurfacePrefab</c> 區域——
        /// 於區域網格上三角化，由 <c>AreaBatchSystem</c> 以模板的 <c>RenderedArea.m_Material</c>
        /// 繪製。住宅車道口同屬此機制。缺少此層，這些表面於束中根本不存在，
        /// 此即歷次重建於遊戲顯示鋪面之處盡呈草地的原因。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root.（束根目錄。）</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory, for textures.（<c>terrain/</c> 的絕對路徑，供貼圖使用。）</param>
        /// <param name="report">The report to note results in.（用以記錄結果的報告。）</param>
        private void RipSurfaceAreas(string outputDirectory, string terrainDirectory, StringBuilder report)
        {
            var areas = _surfaceAreaQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            int written = 0, skippedEmpty = 0;
            Dictionary<Entity, string> prefabKeys = new();
            HashSet<string> usedKeys = new();
            Dictionary<string, object> catalogue = new();

            try
            {
                using StreamWriter writer = new(IOUtils.CombinePath(outputDirectory, "areas.jsonl"),
                                                append: false, new UTF8Encoding(false));

                foreach (Entity entity in areas)
                {
                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0))
                    {
                        skippedEmpty++;
                        continue;
                    }

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (!prefabKeys.TryGetValue(prefab, out string key))
                    {
                        key = CatalogueAreaPrefab(prefab, terrainDirectory, usedKeys, catalogue);
                        prefabKeys[prefab] = key;
                    }

                    var record = new Dictionary<string, object>
                    {
                        ["id"] = entity.Index,
                        ["prefab"] = key
                    };

                    if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                    {
                        Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                        if (owner != Entity.Null) record["owner"] = owner.Index;
                    }

                    double[][] nodeArray = new double[nodes.Length][];
                    for (int i = 0; i < nodes.Length; i++)
                    {
                        float3 position = nodes[i].m_Position;
                        nodeArray[i] = new[]
                        {
                            Math.Round((double)position.x, 3),
                            Math.Round((double)position.y, 3),
                            Math.Round((double)position.z, 3)
                        };
                    }
                    record["nodes"] = nodeArray;

                    int[][] triangleArray = new int[triangles.Length][];
                    double[][] rangeArray = new double[triangles.Length][];
                    bool anyRange = false;
                    for (int i = 0; i < triangles.Length; i++)
                    {
                        Game.Areas.Triangle triangle = triangles[i];
                        int3 indices = triangle.m_Indices;
                        triangleArray[i] = new[] { indices.x, indices.y, indices.z };

                        // The game's own answer to "how far does the ground depart from this
                        // flat triangle": GeometrySystem.GetHeightRange walks every heightmap
                        // cell the triangle covers and takes min/max RELATIVE TO ITS PLANE,
                        // water included. AreaRenderSystem then draws the triangle as a prism
                        // spanning it and projects the surface material onto the terrain
                        // inside — the area is paint, not a plane (contract §8b, audit §30).
                        // Free to carry: the buffer is already in hand.
                        // （遊戲對「地面偏離此平面三角形多遠」的答案：相對於三角形平面的
                        //   最小/最大值，區域據此以稜柱投影上色——區域是塗料，而非平面。）
                        Bounds1 range = triangle.m_HeightRange;
                        rangeArray[i] = new[]
                        {
                            Math.Round((double)range.min, 3),
                            Math.Round((double)range.max, 3)
                        };
                        anyRange |= (range.min != 0f) || (range.max != 0f);
                    }
                    record["triangles"] = triangleArray;
                    if (anyRange) record["heightRanges"] = rangeArray;

                    writer.WriteLine(JsonConvert.SerializeObject(record));
                    written++;
                }
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Surface areas failed: {ex.Message}");
                return;
            }
            finally
            {
                areas.Dispose();
            }

            try
            {
                File.WriteAllText(IOUtils.CombinePath(outputDirectory, "areaprefabs.json"),
                                  JsonConvert.SerializeObject(catalogue, Formatting.Indented));
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Could not write areaprefabs.json: {ex.Message}");
            }

            report.AppendLine($"Surface areas: {written} area(s) over {catalogue.Count} prefab(s)"
                            + ((skippedEmpty > 0) ? $"; {skippedEmpty} empty skipped" : ""));
        }

        /// <summary>
        /// Add one area prefab to the catalogue: its name, its material's base colour, and its
        /// base texture captured into <c>terrain/surfaces/</c> when it has one.
        /// （將單一區域預製模板加入目錄：其名稱、材質底色，及其底圖——若有——擷取至
        /// <c>terrain/surfaces/</c>。）
        /// </summary>
        /// <param name="prefab">The area prefab entity.（區域預製模板實體。）</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory.（<c>terrain/</c> 的絕對路徑。）</param>
        /// <param name="usedKeys">Keys taken so far, for uniqueness over the run.（既用之鍵，維持整次擷取內唯一。）</param>
        /// <param name="catalogue">The catalogue to add the entry to.（要加入項目的目錄。）</param>
        /// <returns>The catalogue key the records reference.（紀錄所引用的目錄鍵。）</returns>
        private string CatalogueAreaPrefab(Entity prefab, string terrainDirectory,
                                           HashSet<string> usedKeys, Dictionary<string, object> catalogue)
        {
            string name = Instance.Name.GetPrefabName(prefab);
            if (string.IsNullOrEmpty(name)) name = $"area_{prefab.Index}";

            string key = MeshRipSystem.SanitizeAssetId(name);
            if (!usedKeys.Add(key))
            {
                key = $"{key}_{prefab.Index}";
                usedKeys.Add(key);
            }

            var entry = new Dictionary<string, object> { ["name"] = name };
            catalogue[key] = entry;

            try
            {
                if (EntityManager.HasComponent<PrefabData>(prefab)
                    && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefab),
                                                    out PrefabBase prefabBase)
                    && (prefabBase != null)
                    && prefabBase.TryGet(out RenderedArea rendered)
                    && (rendered != null))
                {
                    // The two authored numbers a consumer otherwise has to invent. `m_UVScale`
                    // goes straight to the shader as `colossal_UVScale` (AreaBatchSystem) and
                    // multiplies the WORLD position — the area's texture is world-planar, not
                    // UV-mapped, so without it every consumer guesses a metres-per-repeat.
                    // `RenderedAreaData.m_HeightOffset` expands the projection prism beyond the
                    // terrain's own departure from the triangle (contract §8b, audit §30).
                    // （消費端否則必須自行臆測的兩個作者設定值：UV 比例為世界平面貼圖比例，
                    //   高度偏移則為投影稜柱在地形起伏之外的擴張量。）
                    entry["uvScale"] = Math.Round((double)rendered.m_UVScale, 5);
                    if (EntityManager.HasComponent<RenderedAreaData>(prefab))
                    {
                        entry["heightOffset"] = Math.Round(
                            (double)EntityManager.GetComponentData<RenderedAreaData>(prefab).m_HeightOffset, 4);
                    }

                    Material material = rendered.m_Material;
                    if (material == null) return key;

                    if (material.HasProperty("_BaseColor"))
                    {
                        Color color = material.GetColor("_BaseColor");
                        entry["color"] = new[]
                        {
                            Math.Round((double)color.r, 4), Math.Round((double)color.g, 4),
                            Math.Round((double)color.b, 4), Math.Round((double)color.a, 4)
                        };
                    }

                    Texture texture = material.mainTexture;
                    if ((texture == null) && material.HasProperty("_BaseColorMap"))
                        texture = material.GetTexture("_BaseColorMap");

                    if (texture != null)
                    {
                        PendingTexture captured = MaterialRipSystem.Capture(texture, key, name, 1024);
                        if (captured?.Pixels != null)
                        {
                            byte[] png = Png.EncodeRgba(captured.Pixels, captured.Width, captured.Height);
                            string fileName = $"area_{key}.png";
                            string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
                            if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);
                            File.WriteAllBytes(IOUtils.CombinePath(surfacesDirectory, fileName), png);
                            entry["texture"] = $"terrain/surfaces/{fileName}";
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Area prefab `{name}` material did not rip: {ex.Message}");
            }

            return key;
        }

        /// <summary>
        /// Read the theme's ground textures off the live global shader bindings and write them
        /// under <c>terrain/surfaces/</c>.
        /// （自現行的全域著色器繫結讀取主題的地面貼圖，寫至 <c>terrain/surfaces/</c>。）<br/><br/>
        /// <c>TerrainMaterialSystem.ApplyRenderSettings</c> binds them as
        /// <c>colossal_Terrain*Diffuse</c> whenever a map is loaded, so reading the binding takes
        /// the exact textures the terrain is being drawn with — no asset-database round trip.<br/>
        /// （<c>TerrainMaterialSystem.ApplyRenderSettings</c> 於地圖載入時將其繫結為
        /// <c>colossal_Terrain*Diffuse</c>，故讀取繫結即取得地形當下繪製所用的貼圖。）
        /// </summary>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory.（<c>terrain/</c> 的絕對路徑。）</param>
        /// <param name="report">The report to note results in.（用以記錄結果的報告。）</param>
        /// <returns>Surface name to bundle-relative texture path.（地表名稱對應束相對貼圖路徑。）</returns>
        private Dictionary<string, string> CaptureSurfaceTextures(string terrainDirectory, StringBuilder report)
        {
            Dictionary<string, string> written = new();
            string surfacesDirectory = IOUtils.CombinePath(terrainDirectory, "surfaces");
            if (!Directory.Exists(surfacesDirectory)) Directory.CreateDirectory(surfacesDirectory);

            // The theme binds a diffuse AND a tangent normal per ground, plus up to four
            // paintable extras (`colossal_TerrainExtra*`) most themes leave unbound — those
            // skip silently. Normal maps go through the same two-channel-packing rebuild the
            // material rip applies (§24.4); the extras carry the surfaces a player paints,
            // weighted by the user splatmap.
            // （主題為每種地面繫結漫反射與切線法線各一，另有至多四個可繪製額外層——多數主題
            //   未繫結者靜默略過。法線貼圖經材質擷取相同的雙通道封裝重建；額外層即玩家
            //   手繪的地表，由使用者 splat 加權。）
            foreach (string[] entry in new[]
            {
                new[] { "grass", "colossal_TerrainGrassDiffuse", "core" },
                new[] { "grass_normal", "colossal_TerrainGrassNormal", "core" },
                new[] { "dirt", "colossal_TerrainDirtDiffuse", "core" },
                new[] { "dirt_normal", "colossal_TerrainDirtNormal", "core" },
                new[] { "rock", "colossal_TerrainRockDiffuse", "core" },
                new[] { "rock_normal", "colossal_TerrainRockNormal", "core" },
                new[] { "extra1", "colossal_TerrainExtra1Diffuse", "extra" },
                new[] { "extra1_normal", "colossal_TerrainExtra1Normal", "extra" },
                new[] { "extra2", "colossal_TerrainExtra2Diffuse", "extra" },
                new[] { "extra2_normal", "colossal_TerrainExtra2Normal", "extra" },
                new[] { "extra3", "colossal_TerrainExtra3Diffuse", "extra" },
                new[] { "extra3_normal", "colossal_TerrainExtra3Normal", "extra" },
                new[] { "extra4", "colossal_TerrainExtra4Diffuse", "extra" },
                new[] { "extra4_normal", "colossal_TerrainExtra4Normal", "extra" }
            })
            {
                string name = entry[0];
                bool core = entry[2] == "core";
                try
                {
                    Texture texture = Shader.GetGlobalTexture(entry[1]);
                    if (texture == null)
                    {
                        if (core) report.AppendLine($"Surface texture '{name}': not bound; skipped");
                        continue;
                    }

                    // Unbound slots are filled with Unity's built-in 4×4 placeholders
                    // (`ApplyRenderSettings` falls back to `Texture2D.grayTexture`); a real
                    // ground texture is never that small.（未繫結的槽位以 Unity 內建的 4×4
                    // 佔位貼圖填充；真實地面貼圖絕無此尺寸。）
                    if (!core && ((texture.width <= 8) || (texture.height <= 8))) continue;

                    PendingTexture captured = MaterialRipSystem.Capture(texture, name, name, 1024);
                    if (captured?.Pixels == null)
                    {
                        report.AppendLine($"Surface texture '{name}': readback failed; skipped");
                        continue;
                    }

                    if (name.EndsWith("_normal", StringComparison.Ordinal))
                        MaterialRipSystem.ReconstructTwoChannelNormal(captured.Pixels);

                    byte[] png = Png.EncodeRgba(captured.Pixels, captured.Width, captured.Height);
                    File.WriteAllBytes(IOUtils.CombinePath(surfacesDirectory, $"{name}.png"), png);
                    written[name] = $"terrain/surfaces/{name}.png";
                }
                catch (Exception ex)
                {
                    _log.Warn($"Surface texture '{name}' failed: {ex.Message}");
                    report.AppendLine($"Surface texture '{name}': {ex.Message}");
                }
            }

            report.AppendLine($"Surfaces: {written.Count} ground texture(s) written");
            return written;
        }

        /// <summary>
        /// Read back the game's splat weight map — the per-pixel ground classification the
        /// terrain shader blends grass/dirt/rock (and the painted surfaces) with.
        /// （回讀遊戲的 splat 權重貼圖——地形著色器據以逐像素混合草／土／岩及手繪地表者。）<br/><br/>
        /// <c>TerrainMaterialSystem.UpdateSplatmap</c> renders it on the GPU from the modified
        /// height cascade, water, noise and the user brush (<c>_SplatHeightVariance</c>,
        /// <c>_SplatRockLimit</c>, <c>_SplatGrassLimit</c>), into a 4096² RGBA8 render texture
        /// bound globally as <c>colossal_Splatmap</c> and exposed as the public
        /// <c>splatmap</c> property. It spans exactly the playable area:
        /// <c>world.xz = playableOffset + uv × playableArea</c> (its own <c>UpdateSplatmap</c>
        /// viewport math). Shipping it verbatim replaces the consumer-side per-cell ground
        /// classification with the game's own smooth per-pixel rule.<br/>
        /// （<c>UpdateSplatmap</c> 於 GPU 上自修改後的高度串接、水體、雜訊與使用者筆刷繪成，
        /// 為 4096² RGBA8 繪製目標，全域繫結為 <c>colossal_Splatmap</c>，並以公開的
        /// <c>splatmap</c> 屬性暴露。其恰好覆蓋可遊玩區域。原樣出貨即以遊戲自身的平滑
        /// 逐像素規則取代消費端的逐格分類。）<br/><br/>
        /// The channel-to-ground assignment is not stated in managed code (the generator and
        /// consumer are both shaders), so the block declares the generator inputs and the
        /// shaping constants and leaves the assignment to the consumer's calibration — the
        /// Blender harness derives it from the heightmap's slopes and the roadbed mask and
        /// prints what it found, so nothing here guesses.
        /// （通道與地面的對應未見於受管程式碼（產生與取用皆為著色器），故區塊宣告產生輸入與
        /// 成形常數，將對應留給消費端校準——Blender 匯入器由高度圖坡度與路床遮罩推得並
        /// 印出結果，此處不作任何猜測。）
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "splatmap" block, or <c>null</c>.（接收 <c>rasters.json</c> 的 "splatmap" 區塊，或 <c>null</c>。）</param>
        /// <param name="report">The report to note results in.（用以記錄結果的報告。）</param>
        /// <returns>The captured pixels for the writer task, or <c>null</c>.（供寫出工作使用的已擷取像素，或 <c>null</c>。）</returns>
        private PendingTexture CaptureSplatmap(out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture splat = materials?.splatmap;
                if (splat == null)
                {
                    report.AppendLine("Splatmap: not resident; consumers fall back to per-cell classification");
                    return null;
                }

                PendingTexture captured = MaterialRipSystem.Capture(splat, "splatmap", "terrain splatmap", 0);
                if (captured?.Pixels == null)
                {
                    report.AppendLine("Splatmap: readback failed; consumers fall back to per-cell classification");
                    return null;
                }

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;

                // Texel-centre geo-referencing, same convention as the terrain grid: top-left
                // texel centre, rows toward −Z.（texel 中心的地理參照，慣例與地形網格相同：
                // 左上 texel 中心，列朝 −Z。）
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "GPU-generated ground-blend weights (height variance, rock/grass "
                                  + "limits, noise, user brush); channel-to-ground assignment is "
                                  + "calibrated by the consumer against slope and flat ground — the "
                                  + "blend has no roadbed response (schema §8)",
                    ["splatMultiplier"] = Math.Round((double)materials.SplatMultiplier, 4),
                    ["splatPower"] = Math.Round((double)materials.SplatPower, 4)
                };

                report.AppendLine($"Splatmap: {captured.Width}x{captured.Height} ground-blend weights "
                                + $"captured (multiplier {materials.SplatMultiplier:F2}, power {materials.SplatPower:F2})");
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"Splatmap capture failed: {ex.Message}");
                report.AppendLine($"Splatmap: {ex.Message}; consumers fall back to per-cell classification");
                return null;
            }
        }

        /// <summary>
        /// Read back the user's painted-surface weights — the brush strokes that weight the
        /// paintable extra grounds (<c>terrain/surfaces/extra*.png</c>).
        /// （回讀使用者手繪地表的權重——為可繪製額外地面加權的筆刷筆跡。）<br/><br/>
        /// <c>TerrainMaterialSystem.UserSplatmap</c> (public; R8G8, playable extent, same
        /// mapping as the splatmap). Captured only when it exists and any texel is non-zero —
        /// a city with no painted surfaces ships nothing.
        /// （<c>UserSplatmap</c>（公開；R8G8，可遊玩範圍，映射同 splatmap）。僅於存在且任一
        /// texel 非零時擷取——未手繪的城市不出貨此檔。）
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "userSplatmap" block, or <c>null</c>.（接收 "userSplatmap" 區塊，或 <c>null</c>。）</param>
        /// <param name="report">The report.（報告。）</param>
        /// <returns>The captured pixels, or <c>null</c>.（已擷取像素，或 <c>null</c>。）</returns>
        private PendingTexture CaptureUserSplatmap(out Dictionary<string, object> block, StringBuilder report)
        {
            block = null;
            try
            {
                Game.Rendering.TerrainMaterialSystem materials =
                    World.GetOrCreateSystemManaged<Game.Rendering.TerrainMaterialSystem>();
                Texture user = materials?.UserSplatmap;
                if (user == null) return null;

                PendingTexture captured = MaterialRipSystem.Capture(user, "user_splatmap", "user splatmap", 0);
                if (captured?.Pixels == null) return null;

                bool painted = false;
                for (int i = 0; (i < captured.Pixels.Length) && !painted; i += 4)
                {
                    painted = (captured.Pixels[i] != 0) || (captured.Pixels[i + 1] != 0);
                }
                if (!painted) return null;

                float2 area = Instance.Terrain.playableArea;
                float2 origin = Instance.Terrain.playableOffset;
                double cellX = area.x / captured.Width;
                double cellZ = area.y / captured.Height;
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/user_splatmap.png",
                    ["size"] = new[] { captured.Width, captured.Height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + area.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "player-painted surface weights (R/G channels) for the "
                                  + "paintable extra grounds in terrain/surfaces/"
                };

                report.AppendLine($"User splatmap: painted surfaces present, {captured.Width}x{captured.Height} captured");
                return captured;
            }
            catch (Exception ex)
            {
                _log.Warn($"User splatmap capture failed: {ex.Message}");
                return null;
            }
        }

        /// <summary>
        /// Read back the backdrop (world) heightmap — the terrain beyond the playable border
        /// that fills the horizon.（回讀背景（世界）高度圖——可遊玩邊界之外、構成地平線的地形。）<br/><br/>
        /// <c>TerrainSystem.worldHeightmap</c> is a public <c>R16_UNorm</c> texture spanning
        /// <c>worldOffset … worldOffset + worldSize</c> (the playable area is a sub-rect of it).
        /// The height cascade blits this map and the playable map into one seamless surface, so
        /// they share the height encoding — the same <c>elevationScale</c>/<c>Offset</c> decode,
        /// which the importer can verify by continuity across the playable border. Read through
        /// <c>AsyncGPUReadback</c> (the game's own readback route for these maps), which returns
        /// the 16-bit samples losslessly where an 8-bit blit would quantise them.
        /// （<c>worldHeightmap</c> 為公開的 <c>R16_UNorm</c> 貼圖，涵蓋世界範圍（可遊玩區域為其
        /// 子矩形）。高度串接將其與可遊玩圖拼成無縫表面，故兩者共享高度編碼——匯入端可藉
        /// 邊界連續性驗證。經 <c>AsyncGPUReadback</c> 讀回，16 位元樣本無損。）
        /// </summary>
        /// <param name="block">Receives the <c>rasters.json</c> "worldTerrain" block, or <c>null</c>.（接收 "worldTerrain" 區塊，或 <c>null</c>。）</param>
        /// <param name="width">Receives the map width.（接收圖寬。）</param>
        /// <param name="height">Receives the map height.（接收圖高。）</param>
        /// <param name="report">The report.（報告。）</param>
        /// <returns>South-to-north rows of 16-bit samples, or <c>null</c>.（由南至北的 16 位元樣本列，或 <c>null</c>。）</returns>
        private ushort[] CaptureWorldHeightmap(out Dictionary<string, object> block,
                                               out int width, out int height, StringBuilder report)
        {
            block = null;
            width = height = 0;
            try
            {
                Texture world = Instance.Terrain.worldHeightmap;
                if (world == null)
                {
                    report.AppendLine("World terrain: no backdrop heightmap resident; layer omitted");
                    return null;
                }

                UnityEngine.Rendering.AsyncGPUReadbackRequest request =
                    UnityEngine.Rendering.AsyncGPUReadback.Request(world, 0);
                request.WaitForCompletion();
                if (request.hasError)
                {
                    report.AppendLine("World terrain: GPU readback failed; layer omitted");
                    return null;
                }

                Unity.Collections.NativeArray<ushort> data = request.GetData<ushort>();
                width = world.width;
                height = world.height;
                if (data.Length < width * height)
                {
                    report.AppendLine($"World terrain: readback returned {data.Length} of {width * height} samples; layer omitted");
                    return null;
                }

                ushort[] samples = new ushort[width * height];
                Unity.Collections.NativeArray<ushort>.Copy(data, samples, samples.Length);

                float2 size = Instance.Terrain.worldSize;
                float2 origin = Instance.Terrain.worldOffset;
                double cellX = size.x / width;
                double cellZ = size.y / height;
                block = new Dictionary<string, object>
                {
                    ["file"] = "terrain/world_heightmap.png",
                    ["size"] = new[] { width, height },
                    ["resolution"] = new[] { Math.Round(cellX, 6), Math.Round(cellZ, 6) },
                    ["originWorld"] = new[]
                    {
                        Math.Round(origin.x + (cellX * 0.5), 3), 0.0,
                        Math.Round(origin.y + size.y - (cellZ * 0.5), 3)
                    },
                    ["semantics"] = "backdrop terrain; same elevation decode as the main heightmap "
                                  + "(the cascade blits both into one seamless surface); the playable "
                                  + "area is a sub-rect and its samples are authoritative there"
                };

                report.AppendLine($"World terrain: {width}x{height} backdrop heightmap captured "
                                + $"({size.x:F0}x{size.y:F0} m)");
                return samples;
            }
            catch (Exception ex)
            {
                _log.Warn($"World heightmap capture failed: {ex.Message}");
                report.AppendLine($"World terrain: {ex.Message}; layer omitted");
                return null;
            }
        }

        // ------------------------------------------------------------------------------------
        // The roadbed stamp — the game's own flattening footprint, rasterised.
        // （路床蓋印——遊戲自身的壓平足跡，予以光柵化。）
        // ------------------------------------------------------------------------------------

        /// <summary>
        /// Stamp every net segment the game's road job flattens with into the surface index.
        /// （將遊戲道路作業用以壓平的每個路網區段蓋印至地表索引。）<br/><br/>
        /// The walk is <c>CullRoadsJob</c>'s, decompiled: edges contribute their two
        /// geometry halves, each connected node its strip set (four strips at a junction, two on
        /// a straight-through node, the ring pieces on a roundabout), and orphan nodes two
        /// quarter-circle cap pairs. Only compositions whose ground is actually pulled to the
        /// road surface are stamped — elevated decks and tunnels adjust terrain too, but
        /// downward/away, and their ground is not roadbed.<br/>
        /// （走訪即反編譯的 <c>CullRoadsJob</c>：邊貢獻其兩個幾何半段，所連節點貢獻其條帶組
        /// （路口四條、直通節點兩條、圓環為環帶部件），孤立節點則為兩對四分之一圓蓋。
        /// 僅蓋印地面確實被拉至路面的組成——高架橋面與隧道亦會調整地形，但方向向下或遠離，
        /// 其地面並非路床。）
        /// </summary>
        /// <param name="surfaceIndex">The mask to stamp into, south-to-north rows.（要蓋印的遮罩，列序由南至北。）</param>
        /// <param name="heightData">The terrain grid, for the world-to-cell mapping.（地形網格，供世界至格的映射。）</param>
        private void StampRoadbed(byte[] surfaceIndex, TerrainHeightData heightData)
        {
            StampContext context = new() { Mask = surfaceIndex, Value = 1 };
            var edges = _edgeQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in edges)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    // The gate the game's job applies per lane section, applied per prefab here:
                    // FlattenTerrain becomes ShiftTerrain, ClipTerrain stays ClipTerrain.
                    // （遊戲作業逐車道區段套用的條件，此處逐預製模板套用。）
                    Game.Net.GeometryFlags geometryFlags = EntityManager.GetComponentData<NetGeometryData>(prefab).m_Flags;
                    if ((geometryFlags & (Game.Net.GeometryFlags.FlattenTerrain | Game.Net.GeometryFlags.ClipTerrain)) == 0)
                    {
                        _skippedNoFlatten++;
                        continue;
                    }

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);

                    if (TryGetGroundComposition(composition.m_Edge, out TerrainComposition edgeTerrain))
                    {
                        EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                        StampSegment(in context, heightData, geometry.m_Start, edgeTerrain, true, true);
                        StampSegment(in context, heightData, geometry.m_End, edgeTerrain, true, true);
                        _edgesStamped++;
                    }

                    if (EntityManager.HasComponent<StartNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_StartNode, out TerrainComposition startTerrain)
                        && StampNode(in context, in context, heightData,
                                     EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain))
                    {
                        _nodesStamped++;
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_EndNode, out TerrainComposition endTerrain)
                        && StampNode(in context, in context, heightData,
                                     EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain))
                    {
                        _nodesStamped++;
                    }
                }
            }
            finally
            {
                edges.Dispose();
            }

            var orphans = _orphanQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in orphans)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    Game.Net.GeometryFlags geometryFlags = EntityManager.GetComponentData<NetGeometryData>(prefab).m_Flags;
                    if ((geometryFlags & (Game.Net.GeometryFlags.FlattenTerrain | Game.Net.GeometryFlags.ClipTerrain)) == 0) continue;

                    Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
                    if (!TryGetGroundComposition(composition, out TerrainComposition terrain)) continue;

                    float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
                    if (!(width > 0f)) continue;

                    StampOrphan(in context, heightData,
                                EntityManager.GetComponentData<Node>(entity).m_Position, width, terrain);
                    _orphansStamped++;
                }
            }
            finally
            {
                orphans.Dispose();
            }
        }

        /// <summary>
        /// Whether a composition's ground is pulled to the road surface, and its terrain offsets.
        /// （組成的地面是否被拉至路面，及其地形偏移。）<br/><br/>
        /// The game's <c>AddSegment</c> gives elevated compositions a downward-only clamp and
        /// tunnels an upward-only one — their terrain is shifted <i>away</i> from the road, not
        /// onto it — unless a side carries a transition, in which case the node handling sheds the
        /// elevated/tunnel state (the portal is ground). <c>HasSurface</c> excludes nets with no
        /// surface at all (power lines), whose clamp is clearance, not roadbed.<br/>
        /// （遊戲的 <c>AddSegment</c> 對高架組成僅施以向下的鉗制、對隧道僅向上——其地形被移
        /// <i>離</i>道路而非移上——除非某側帶有過渡，此時節點處理會卸除高架／隧道狀態
        /// （洞口即地面）。<c>HasSurface</c> 排除全無表面的路網（電力線），其鉗制為淨空而非路床。）
        /// </summary>
        /// <param name="composition">The composition entity.（組成實體。）</param>
        /// <param name="terrain">Receives the terrain offsets, or default.（接收地形偏移，或預設值。）</param>
        /// <returns>Whether to stamp this composition.（是否蓋印此組成。）</returns>
        private bool TryGetGroundComposition(Entity composition, out TerrainComposition terrain)
        {
            terrain = default;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return false;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            if ((data.m_State & CompositionState.HasSurface) == 0) return false;

            if ((data.m_Flags.m_General & (CompositionFlags.General.Elevated | CompositionFlags.General.Tunnel)) != 0)
            {
                CompositionFlags.Side sides = data.m_Flags.m_Left | data.m_Flags.m_Right;
                if ((sides & (CompositionFlags.Side.LowTransition | CompositionFlags.Side.HighTransition)) == 0)
                {
                    _skippedForm++;
                    return false;
                }
            }

            if (EntityManager.HasComponent<TerrainComposition>(composition))
            {
                terrain = EntityManager.GetComponentData<TerrainComposition>(composition);
            }
            return true;
        }

        /// <summary>
        /// One stamping pass's target and rule, threaded through the shared segment walk.
        /// （單次蓋印的目標與規則，貫穿共用的區段走訪。）<br/><br/>
        /// With <see cref="Cut"/> unset the fill is unconditional (the roadbed mask). With it
        /// set, a cell is written only where the terrain stands between
        /// <c>lane + CutBottom</c> and <c>lane + CutTop</c> — the clip band test.
        /// （未設 <see cref="Cut"/> 時無條件填入（路床遮罩）；設定時僅於地形介於
        /// <c>車道 + CutBottom</c> 與 <c>車道 + CutTop</c> 之間的格寫入——即裁切帶測試。）
        /// </summary>
        private struct StampContext
        {
            /// <summary>The mask, south-to-north rows.（遮罩，列序由南至北。）</summary>
            public byte[] Mask;

            /// <summary>The value written into covered cells.（寫入覆蓋格的值。）</summary>
            public byte Value;

            /// <summary>Whether the clip band test applies.（是否套用裁切帶測試。）</summary>
            public bool Cut;

            /// <summary>Band floor, metres above the lane surface.（帶底，高於車道面的公尺數。）</summary>
            public float CutBottom;

            /// <summary>Band ceiling, metres above the lane surface.（帶頂，高於車道面的公尺數。）</summary>
            public float CutTop;
        }

        /// <summary>
        /// A composition's clip band top, per the game's <c>CullRoadsJob.AddSegment</c>.
        /// （組成的裁切帶頂，依遊戲的 <c>CullRoadsJob.AddSegment</c>。）<br/><br/>
        /// The base is the composition's edge-height extreme (the start or end half, like the
        /// game's <c>isStart</c> select) plus <c>TerrainComposition.m_ClipHeightOffset</c>. Tunnel
        /// portals (Tunnel with a transition side) and open trenches (a Lowered side, not
        /// elevated) raise it to <c>ElevationLimit·3</c> — the 30 m open cut. The job's own
        /// 0.3 m expansion is added last. Deep tube sections keep the small base band: the
        /// terrain covering them sits far above it, so the band test cuts nothing there, which is
        /// the game's behaviour too (their cover is never clipped from above).<br/>
        /// （帶底取組成的緣高極值（依 <c>isStart</c> 選取起訖半段）加上
        /// <c>TerrainComposition.m_ClipHeightOffset</c>；隧道洞口（帶過渡側的 Tunnel）與明挖壕溝
        /// （帶 Lowered 側且非高架）將其抬升至 <c>ElevationLimit·3</c>——即 30 公尺的明挖範圍；
        /// 最後加上作業自身的 0.3 公尺擴張。深埋管段維持小帶：其覆土遠高於帶頂，故帶測試於彼處
        /// 不裁任何格，與遊戲行為一致。）
        /// </summary>
        /// <param name="composition">The composition entity.（組成實體。）</param>
        /// <param name="raisedTop">The prefab's <c>ElevationLimit * 3</c>.（預製模板的 <c>ElevationLimit * 3</c>。）</param>
        /// <param name="isStart">Which edge-height half applies.（適用哪一半的緣高。）</param>
        /// <param name="terrain">Receives the terrain offsets, or default.（接收地形偏移，或預設值。）</param>
        /// <param name="cutTop">Receives the band top.（接收帶頂。）</param>
        /// <returns>Whether the band can cut anything at all.（該帶是否可能裁到任何格。）</returns>
        private bool TryGetClipBand(Entity composition, float raisedTop, bool isStart,
                                    out TerrainComposition terrain, out float cutTop)
        {
            terrain = default;
            cutTop = 0f;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return false;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            if (EntityManager.HasComponent<TerrainComposition>(composition))
            {
                terrain = EntityManager.GetComponentData<TerrainComposition>(composition);
            }

            float3 baseHeights = isStart
                ? new float3(data.m_EdgeHeights.x, data.m_SurfaceHeight.min, data.m_EdgeHeights.y)
                : new float3(data.m_EdgeHeights.z, data.m_SurfaceHeight.min, data.m_EdgeHeights.w);
            cutTop = math.cmax(baseHeights) + terrain.m_ClipHeightOffset.y;

            bool tunnel = (data.m_Flags.m_General & CompositionFlags.General.Tunnel) != 0;
            bool elevated = (data.m_Flags.m_General & CompositionFlags.General.Elevated) != 0;
            CompositionFlags.Side sides = data.m_Flags.m_Left | data.m_Flags.m_Right;
            bool transition = (sides & (CompositionFlags.Side.LowTransition | CompositionFlags.Side.HighTransition)) != 0;
            bool lowered = (sides & CompositionFlags.Side.Lowered) != 0;
            if ((tunnel && transition) || (!tunnel && !elevated && lowered))
            {
                cutTop = math.max(cutTop, raisedTop);
            }
            cutTop += 0.3f;

            // A band that never rises above the floor cannot mark a cell; skip the walk.
            // （帶頂不高於帶底者不可能標記任何格；跳過走訪。）
            return cutTop > CutoutFloor;
        }

        /// <summary>
        /// Stamp the terrain cutout — the cells the game's <c>RoadClip</c> stencil pass hides
        /// every frame instead of carving them out of the heightmap.
        /// （蓋印地形裁切遮罩——遊戲的 <c>RoadClip</c> 模板繪次每影格隱藏、而非自高度圖鑿除的格。）<br/><br/>
        /// The walk is <c>CullRoadsJob</c>'s once more, but through <c>GenerateClipDataJob</c>'s
        /// gate: every edge segment, node strip and orphan cap of a net whose prefab carries
        /// <c>GeometryFlags.ClipTerrain</c> — tunnels and trenches <i>included</i> this time,
        /// because the clip is exactly what opens them. Areas whose prefab carries
        /// <c>ClipArea</c> join at the end (the pass's <c>areaClipMesh</c>). The band per
        /// composition is <see cref="TryGetClipBand"/>'s.<br/>
        /// （走訪仍是 <c>CullRoadsJob</c> 的，但改依 <c>GenerateClipDataJob</c> 的條件：預製模板帶有
        /// <c>GeometryFlags.ClipTerrain</c> 的路網的每個邊區段、節點條帶與孤立節點蓋——這次
        /// <i>包含</i>隧道與壕溝，因為裁切正是開通它們的機制。帶 <c>ClipArea</c> 的區域最後加入
        /// （即該繪次的 <c>areaClipMesh</c>）。各組成的帶見 <see cref="TryGetClipBand"/>。）
        /// </summary>
        /// <param name="cutout">The mask to stamp into, south-to-north rows.（要蓋印的遮罩，列序由南至北。）</param>
        /// <param name="heightData">The terrain grid.（地形網格。）</param>
        private void StampCutout(byte[] cutout, TerrainHeightData heightData)
        {
            StampContext context = new() { Mask = cutout, Value = 255, Cut = true, CutBottom = CutoutFloor };

            var edges = _edgeQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in edges)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & Game.Net.GeometryFlags.ClipTerrain) == 0) continue;
                    float raisedTop = netGeometry.m_ElevationLimit * 3f;

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);
                    bool stamped = false;

                    // The edge halves take the composition's start/end edge heights respectively,
                    // like the game's isStart select.（邊的兩半各取組成起／訖半段的緣高，
                    // 一如遊戲的 isStart 選取。）
                    if (TryGetClipBand(composition.m_Edge, raisedTop, true, out TerrainComposition edgeTerrain, out context.CutTop))
                    {
                        StampSegment(in context, heightData,
                                     EntityManager.GetComponentData<EdgeGeometry>(entity).m_Start, edgeTerrain, true, true);
                        stamped = true;
                    }
                    if (TryGetClipBand(composition.m_Edge, raisedTop, false, out edgeTerrain, out context.CutTop))
                    {
                        StampSegment(in context, heightData,
                                     EntityManager.GetComponentData<EdgeGeometry>(entity).m_End, edgeTerrain, true, true);
                        stamped = true;
                    }
                    if (stamped) _cutEdges++;

                    // The roundabout right pair rides the end-half band (`AddNode` passes
                    // isStart: false there); a band that cannot cut gets a zero top, which the
                    // band test rejects everywhere.（圓環右對取訖半帶；不可裁之帶取零頂，
                    // 帶測試將其全數拒絕。）
                    if (EntityManager.HasComponent<StartNodeGeometry>(entity)
                        && TryGetClipBand(composition.m_StartNode, raisedTop, true, out TerrainComposition startTerrain, out context.CutTop))
                    {
                        StampContext endContext = context;
                        if (!TryGetClipBand(composition.m_StartNode, raisedTop, false, out _, out endContext.CutTop))
                            endContext.CutTop = 0f;
                        if (StampNode(in context, in endContext, heightData,
                                      EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain))
                        {
                            _cutNodes++;
                        }
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetClipBand(composition.m_EndNode, raisedTop, true, out TerrainComposition endTerrain, out context.CutTop))
                    {
                        StampContext endContext = context;
                        if (!TryGetClipBand(composition.m_EndNode, raisedTop, false, out _, out endContext.CutTop))
                            endContext.CutTop = 0f;
                        if (StampNode(in context, in endContext, heightData,
                                      EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain))
                        {
                            _cutNodes++;
                        }
                    }
                }
            }
            finally
            {
                edges.Dispose();
            }

            var orphans = _orphanQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in orphans)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !EntityManager.HasComponent<NetGeometryData>(prefab)) continue;

                    NetGeometryData netGeometry = EntityManager.GetComponentData<NetGeometryData>(prefab);
                    if ((netGeometry.m_Flags & Game.Net.GeometryFlags.ClipTerrain) == 0) continue;

                    Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
                    if (!TryGetClipBand(composition, netGeometry.m_ElevationLimit * 3f, true,
                                        out TerrainComposition terrain, out context.CutTop)) continue;

                    float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
                    if (!(width > 0f)) continue;

                    StampOrphan(in context, heightData,
                                EntityManager.GetComponentData<Node>(entity).m_Position, width, terrain);
                    _cutOrphans++;
                }
            }
            finally
            {
                orphans.Dispose();
            }

            StampClipAreas(in context, heightData);
        }

        /// <summary>
        /// Stamp the areas whose prefab carries <c>ClipArea</c> — the terrain job draws their
        /// triangles into the same stencil (<c>areaClipMesh</c>). The band has no ceiling: an
        /// authored clip surface hides whatever terrain covers it.
        /// （蓋印預製模板帶有 <c>ClipArea</c> 的區域——地形作業將其三角形繪入同一模板
        /// （<c>areaClipMesh</c>）。此帶無頂：作者放置的裁切面會隱藏覆蓋其上的一切地形。）
        /// </summary>
        /// <param name="baseContext">The cutout pass's context.（裁切繪次的情境。）</param>
        /// <param name="heightData">The terrain grid.（地形網格。）</param>
        private void StampClipAreas(in StampContext baseContext, TerrainHeightData heightData)
        {
            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            StampContext context = baseContext;
            context.CutTop = float.MaxValue;

            var areas = _clipAreaQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            try
            {
                foreach (Entity entity in areas)
                {
                    if (!EntityManager.Exists(entity)) continue;

                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0)) continue;

                    for (int i = 0; i < triangles.Length; i++)
                    {
                        int3 indices = triangles[i].m_Indices;
                        if ((math.cmin(indices) < 0) || (math.cmax(indices) >= nodes.Length)) continue;

                        float3 a = nodes[indices.x].m_Position;
                        float3 b = nodes[indices.y].m_Position;
                        float3 c = nodes[indices.z].m_Position;

                        // A triangle is a quad with one doubled corner to the scanline fill.
                        // （對掃描線填色而言，三角形即一角重複的四邊形。）
                        FillQuad(in context, heightData, width, height,
                                 ToCell(a, heightData), ToCell(b, heightData),
                                 ToCell(c, heightData), ToCell(c, heightData),
                                 (a.y + b.y + c.y) / 3f);
                    }
                    _cutAreas++;
                }
            }
            finally
            {
                areas.Dispose();
            }
        }

        /// <summary>
        /// Stamp one node's strips — the flattened junction interior.
        /// （蓋印單一節點的條帶——壓平的路口內部。）<br/><br/>
        /// The three cases are <c>CullRoadsJob.AddNode</c>'s: a roundabout rides its middle curve
        /// ring, a true junction (parted corners) covers both halves plus the two wedges to the
        /// middle curve — the petals — and a straight-through node spans half-to-half across the
        /// middle. The roundabout's elevation-transition ring trims (<c>MoveTowards</c> by the
        /// sync offsets) are omitted: they shave a few metres off the ring start at elevated
        /// ramps, so omitting them over-stamps slightly, never under.<br/>
        /// （三種情形即 <c>CullRoadsJob.AddNode</c>：圓環依循其中線曲線環帶；真路口（角落分離）
        /// 涵蓋兩半外加通往中線曲線的兩個楔形——即花瓣；直通節點則自半跨越中線至另一半。
        /// 圓環於高架匝道處的過渡修剪予以省略：其僅於環帶起點削去數公尺，故省略只會多蓋而不少蓋。）
        /// </summary>
        /// <returns>Whether the node cleared the draw gate and was stamped.（節點是否通過繪製門檻並被蓋印。）</returns>
        /// <remarks>
        /// The roundabout's second strip pair is the one place the game evaluates a section with
        /// <c>isStart: false</c> (its <c>AddNode</c> passes it for both <c>node.m_Right</c>
        /// segments), so those two strips take <paramref name="contextEnd"/> — the band built
        /// from the composition's end-half edge heights. Every other strip, and both halves of
        /// every other node kind, is <c>isStart: true</c> and takes <paramref name="context"/>.
        /// （圓環的第二對條帶是遊戲唯一以 <c>isStart: false</c> 求值區段之處，故該兩條帶取
        /// <paramref name="contextEnd"/>——以組成訖半緣高建立的帶。其餘皆取
        /// <paramref name="context"/>。）
        /// </remarks>
        private bool StampNode(in StampContext context, in StampContext contextEnd,
                               TerrainHeightData heightData,
                               EdgeNodeGeometry node, TerrainComposition terrain)
        {
            // The game's own draw gate: nodes shorter than 5 cm on both sides draw nothing.
            // （遊戲自身的繪製門檻：兩側皆短於 5 公分的節點不繪製。）
            if (!(math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))) return false;

            Segment strip;
            if (node.m_MiddleRadius > 0f)
            {
                // Roundabout: outer half to the middle curve, middle curve to the inner half,
                // then the same for the right half against the middle curve's end point.
                // Approximation, counted for cut passes: the game's AddNode sheds the
                // elevated/tunnel + transition state (and MoveTowards-trims the ring ends) for
                // this second pair before building its band; the port keeps the raw flags, which
                // can only widen the band (over-cut), never narrow it.
                // （圓環：外半至中線曲線、中線曲線至內半；右半再對中線曲線終點重複。
                //   近似並於裁切繪次計數：遊戲的 AddNode 於第二對條帶前卸除高架／隧道與過渡狀態
                //   （並以 MoveTowards 修剪環帶端點）再建其帶；移植保留原始旗標，僅會使帶變寬
                //   （多裁）而不會變窄。）
                if (context.Cut) _cutRoundaboutApprox++;

                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Left.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);

                strip = node.m_Right;
                strip.m_Right = new Bezier4x3(node.m_Middle.d, node.m_Middle.d, node.m_Middle.d, node.m_Middle.d);
                StampSegment(in contextEnd, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(in contextEnd, heightData, strip, terrain, false, true);
            }
            else if (math.lengthsq(node.m_Left.m_Right.d - node.m_Right.m_Left.d) > 0.0001f)
            {
                // A junction arm: both halves as authored, plus each half's wedge to the middle
                // curve — the same strips the game's terrain job flattens (its AddNode does
                // exactly this). The node MESH paves the same wedges above (audit §18); this is
                // the ground beneath them.
                // （路口臂：兩半照製作蓋印，外加各半通往中線曲線的楔形——與遊戲地形作業壓平的
                //   條帶相同（其 AddNode 正是如此）。節點網格於其上鋪滿同樣的楔形（審計 §18）；
                //   此處為其下的地面。）
                strip = node.m_Left;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, false, false);

                strip = node.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);
                strip.m_Right = strip.m_Left;
                strip.m_Left = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, false, false);
            }
            else
            {
                // Straight through: left half to the middle, middle to the right half.
                // （直通：左半至中線、中線至右半。）
                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(in context, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(in context, heightData, strip, terrain, false, true);
            }

            return true;
        }

        /// <summary>
        /// Stamp an orphan node's caps: two quarter-circle segment pairs per side, the game's own
        /// synthetic construction (<c>CullRoadsJob.AddOrphans</c>, world-axis aligned).
        /// （蓋印孤立節點的蓋：每側兩對四分之一圓區段，即遊戲自身的合成構造，對齊世界座標軸。）
        /// </summary>
        private void StampOrphan(in StampContext context, TerrainHeightData heightData,
                                 float3 position, float width, TerrainComposition terrain)
        {
            const float kappa = 0.2761424f; // the game's own circle constant（遊戲自身的圓常數）
            float half = width * 0.5f;

            Segment segment = default;
            Bezier4x3 centre = new(position, position, position, position);

            // +z half, left quarter.（+z 半，左四分之一。）
            segment.m_Left = new Bezier4x3(
                new float3(position.x - half, position.y, position.z),
                new float3(position.x - half, position.y, position.z + (width * kappa)),
                new float3(position.x - (width * kappa), position.y, position.z + half),
                new float3(position.x, position.y, position.z + half));
            segment.m_Right = centre;
            segment.m_Length = new float2(width * (math.PI / 2f), 0f);
            StampSegment(in context, heightData, segment, terrain, true, false);

            // +z half, right quarter — the left curve mirrored about the node.
            // （+z 半，右四分之一——左曲線對節點鏡射。）
            Segment mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x += width;
            mirrored.m_Right.b.x += width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(in context, heightData, mirrored, terrain, false, true);

            // -z half, both quarters.（−z 半的兩個四分之一。）
            segment.m_Left = new Bezier4x3(
                new float3(position.x + half, position.y, position.z),
                new float3(position.x + half, position.y, position.z - (width * kappa)),
                new float3(position.x + (width * kappa), position.y, position.z - half),
                new float3(position.x, position.y, position.z - half));
            segment.m_Right = centre;
            StampSegment(in context, heightData, segment, terrain, true, false);

            mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x -= width;
            mirrored.m_Right.b.x -= width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(in context, heightData, mirrored, terrain, false, true);
        }

        /// <summary>
        /// Stamp the area between one segment's boundary béziers into the mask.
        /// （將單一區段兩條邊界貝茲曲線之間的面積蓋印至遮罩。）<br/><br/>
        /// The width trim is <c>AddSegment</c>'s: <c>TerrainComposition.m_WidthOffset</c> narrows
        /// each side by lerping its boundary toward the other, per control point, skipped on a
        /// side that is the shared middle of a node strip. The smoothing margin
        /// (<c>GetTerrainSmoothingWidth</c>) is deliberately not stamped: it is the blend apron
        /// where terrain <i>slopes</i> to meet the road, not the flattened bed itself.<br/>
        /// （寬度修剪即 <c>AddSegment</c> 所為：<c>TerrainComposition.m_WidthOffset</c> 以逐控制點
        /// 向對側內插使該側收窄；作為節點條帶共用中線的一側則跳過。平滑邊緣刻意不蓋印：
        /// 其為地形<i>傾斜</i>以銜接道路的過渡帶，並非壓平的路床本身。）
        /// </summary>
        /// <param name="context">The target mask and stamp rule.（目標遮罩與蓋印規則。）</param>
        /// <param name="heightData">The terrain grid.（地形網格。）</param>
        /// <param name="segment">The boundary curves.（邊界曲線。）</param>
        /// <param name="terrain">The composition's terrain offsets.（組成的地形偏移。）</param>
        /// <param name="trimLeft">Whether the left side takes its width offset.（左側是否套用寬度偏移。）</param>
        /// <param name="trimRight">Whether the right side takes its width offset.（右側是否套用寬度偏移。）</param>
        private static void StampSegment(in StampContext context, TerrainHeightData heightData,
                                         Segment segment, TerrainComposition terrain,
                                         bool trimLeft, bool trimRight)
        {
            if (math.any(math.isnan(segment.m_Left.a)) || math.any(math.isnan(segment.m_Right.a))) return;

            if (math.any(terrain.m_WidthOffset != 0f))
            {
                Segment authored = segment;
                float4 inverseWidths = 1f / math.max(0.001f, new float4(
                    math.distance(authored.m_Left.a.xz, authored.m_Right.a.xz),
                    math.distance(authored.m_Left.b.xz, authored.m_Right.b.xz),
                    math.distance(authored.m_Left.c.xz, authored.m_Right.c.xz),
                    math.distance(authored.m_Left.d.xz, authored.m_Right.d.xz)));

                if ((terrain.m_WidthOffset.x != 0f) && trimLeft)
                {
                    float4 t = terrain.m_WidthOffset.x * inverseWidths;
                    segment.m_Left = LerpCurve(authored.m_Left, authored.m_Right, t);
                }
                if ((terrain.m_WidthOffset.y != 0f) && trimRight)
                {
                    float4 t = terrain.m_WidthOffset.y * inverseWidths;
                    segment.m_Right = LerpCurve(authored.m_Right, authored.m_Left, t);
                }
            }

            int width = heightData.resolution.x;
            int height = heightData.resolution.z;

            // One station per terrain cell of arc length keeps the fill within half a cell of the
            // true curve.（每格弧長一個取樣站，使填色與真實曲線的偏差保持在半格之內。）
            float length = math.max(math.cmax(segment.m_Length), 1f);
            int steps = math.clamp((int)math.ceil(length * heightData.scale.x), 1, 1024);

            float3 previousLeft = segment.m_Left.a;
            float3 previousRight = segment.m_Right.a;

            for (int i = 1; i <= steps; i++)
            {
                float t = (float)i / steps;
                float3 currentLeft = Colossal.Mathematics.MathUtils.Position(segment.m_Left, t);
                float3 currentRight = Colossal.Mathematics.MathUtils.Position(segment.m_Right, t);

                // The quad spans one cell of arc; its corner mean is the lane height reference
                // the cut band hangs from.（四邊形僅跨一格弧長；其四角平均即裁切帶所繫的車道高。）
                float laneY = (previousLeft.y + previousRight.y + currentLeft.y + currentRight.y) * 0.25f;
                FillQuad(in context, heightData, width, height,
                         ToCell(previousLeft, heightData), ToCell(previousRight, heightData),
                         ToCell(currentRight, heightData), ToCell(currentLeft, heightData), laneY);

                previousLeft = currentLeft;
                previousRight = currentRight;
            }
        }

        /// <summary>
        /// Lerp one boundary bézier toward another by a per-control-point parameter — the game's
        /// <c>MathUtils.Lerp(Bezier4x3, Bezier4x3, Bezier4x1)</c>.
        /// （以逐控制點參數將一條邊界貝茲曲線向另一條內插——即遊戲的
        /// <c>MathUtils.Lerp(Bezier4x3, Bezier4x3, Bezier4x1)</c>。）
        /// </summary>
        private static Bezier4x3 LerpCurve(Bezier4x3 from, Bezier4x3 to, float4 t)
        {
            return new Bezier4x3(
                math.lerp(from.a, to.a, t.x),
                math.lerp(from.b, to.b, t.y),
                math.lerp(from.c, to.c, t.z),
                math.lerp(from.d, to.d, t.w));
        }

        /// <summary>
        /// World position to (fractional) terrain cell — <c>TerrainUtils.ToHeightmapSpace</c>.
        /// （世界座標至（含小數的）地形格——即 <c>TerrainUtils.ToHeightmapSpace</c>。）
        /// </summary>
        private static float2 ToCell(float3 world, TerrainHeightData heightData)
        {
            return ((world + heightData.offset) * heightData.scale).xz;
        }

        /// <summary>
        /// Fill one convex quad in cell space, even-odd scanline.
        /// （以奇偶掃描線填入格空間中的單一凸四邊形。）
        /// </summary>
        /// <remarks>
        /// The scratch buffers are static because the stamp runs single-threaded on the main
        /// thread; per-quad allocation over a city's ~10⁶ quads is the only alternative.
        /// （暫存區為靜態，因蓋印於主執行緒單線執行；否則全城約 10⁶ 個四邊形須逐一配置。）
        /// </remarks>
        private static readonly float2[] _quadScratch = new float2[4];
        private static readonly float[] _crossingScratch = new float[4];

        private static void FillQuad(in StampContext context, TerrainHeightData heightData,
                                     int width, int height,
                                     float2 a, float2 b, float2 c, float2 d, float laneY)
        {
            float2[] poly = _quadScratch;
            poly[0] = a; poly[1] = b; poly[2] = c; poly[3] = d;
            float[] crossings = _crossingScratch;

            float minY = math.max(0f, math.min(math.min(a.y, b.y), math.min(c.y, d.y)));
            float maxY = math.min(height - 1, math.max(math.max(a.y, b.y), math.max(c.y, d.y)));

            for (int row = (int)math.floor(minY); row <= (int)math.ceil(maxY); row++)
            {
                if ((row < 0) || (row >= height)) continue;
                float y = row + 0.5f;

                int count = 0;
                for (int e = 0; e < 4; e++)
                {
                    float2 p = poly[e];
                    float2 q = poly[(e + 1) & 3];
                    if ((p.y <= y) == (q.y <= y)) continue;
                    crossings[count++] = p.x + ((y - p.y) / (q.y - p.y) * (q.x - p.x));
                }

                if (count < 2) continue;

                // At most four crossings; a tiny insertion sort keeps the spans paired.
                // （至多四個交點；小型插入排序即可保持區間成對。）
                for (int i = 1; i < count; i++)
                {
                    float value = crossings[i];
                    int j = i - 1;
                    while ((j >= 0) && (crossings[j] > value)) { crossings[j + 1] = crossings[j]; j--; }
                    crossings[j + 1] = value;
                }

                for (int pair = 0; (pair + 1) < count; pair += 2)
                {
                    int from = math.max(0, (int)math.round(crossings[pair]));
                    int to = math.min(width - 1, (int)math.round(crossings[pair + 1]));
                    for (int x = from; x <= to; x++)
                    {
                        int cellIndex = (row * width) + x;
                        if (context.Cut)
                        {
                            // The clip band test: only terrain standing over the lane, within
                            // the composition's band, is cut away.（裁切帶測試：僅裁去高於車道、
                            // 且落於組成之帶內的地形。）
                            float terrainY = (heightData.heights[cellIndex] / heightData.scale.y) - heightData.offset.y;
                            float overLane = terrainY - laneY;
                            if ((overLane <= context.CutBottom) || (overLane >= context.CutTop)) continue;
                        }
                        context.Mask[cellIndex] = context.Value;
                    }
                }
            }
        }
    }
}
