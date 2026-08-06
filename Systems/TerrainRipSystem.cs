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
    /// 本系統以相同的 <c>Segment</c> 貝茲曲線於 CPU 上將其光柵化。）
    /// </summary>
    public partial class TerrainRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// Depth metres per stored unit of <c>water_depth.png</c>. 1/32 m keeps 3 cm precision to
        /// a 2,048 m ceiling — above the game's own <c>kMaxSeaLevel</c> of 2,000.
        /// （<c>water_depth.png</c> 每儲存單位的水深公尺數。1/32 公尺在 2,048 公尺上限內保有
        /// 3 公分精度——高於遊戲自身 2,000 的 <c>kMaxSeaLevel</c>。）
        /// </summary>
        public const float DepthScale = 0.03125f;

        /// <summary>The query for placed network edges.（已放置路網邊的查詢。）</summary>
        EntityQuery _edgeQuery;

        /// <summary>The query for orphan nodes.（孤立節點的查詢。）</summary>
        EntityQuery _orphanQuery;

        // Stamp tallies for the report.（供報告使用的蓋印統計。）
        int _edgesStamped, _nodesStamped, _orphansStamped, _skippedForm, _skippedNoFlatten;

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
        /// roadbed surface index, the water depth grid, the theme's ground textures and the
        /// geo-referencing sidecar. Contract §8 of <c>docs/bundle-schema.md</c>.
        /// （將地形層擷取至 <c>terrain/</c>：最終（道路壓平後）高度圖、路床地表索引、水深網格、
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

            // ---- 3. Water depth, resampled onto the terrain grid so every raster shares one frame.
            // （水深：重採樣至地形網格，使所有網格共用同一參照框架。）
            ushort[] waterDepth = ResampleWaterDepth(heightData, out int wetCells, out float maxDepth);
            report.AppendLine((waterDepth != null)
                ? $"Water: {wetCells} wet cell(s), max depth {maxDepth:F1} m (stored at {DepthScale} m/unit)"
                : "Water: no simulation data resident; layer omitted");

            // ---- 4. The theme's ground textures, read off the live global bindings.
            // （主題的地面貼圖：讀取現行的全域繫結。）
            Dictionary<string, string> surfaceTextures = CaptureSurfaceTextures(terrainDirectory, report);

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
                ["elevationOffset"] = (double)(-offset.y),
                ["depthScale"] = (double)DepthScale
            };

            try
            {
                Task writer = Task.Run(() =>
                {
                    File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "heightmap.png"),
                                       Png.EncodeGray16(FlipRows(heights, width, height), width, height));
                    File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "surface_index.png"),
                                       Png.EncodeGray8(FlipRows(surfaceIndex, width, height), width, height));
                    if (waterDepth != null)
                    {
                        File.WriteAllBytes(IOUtils.CombinePath(terrainDirectory, "water_depth.png"),
                                           Png.EncodeGray16(FlipRows(waterDepth, width, height), width, height));
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
        /// Resample the water simulation's depth grid onto the terrain grid, bilinearly.
        /// （將水體模擬的水深網格雙線性重採樣至地形網格。）<br/><br/>
        /// The two grids cover the same playable area at different resolutions, and the manifest's
        /// <c>rasters</c> block geo-references all raster layers with one frame — so the coarser
        /// simulation grid is lifted onto the terrain grid rather than shipped in a second frame.
        /// Depth is smooth by construction (it is a fluid), so bilinear is faithful.<br/>
        /// （兩個網格以不同解析度涵蓋同一可遊玩區域，而宣告檔的 <c>rasters</c> 區塊以單一框架
        /// 地理參照所有網格層——故將較粗的模擬網格提升至地形網格，而非另立第二框架。
        /// 水深依其構成即為平滑（流體），故雙線性為忠實之法。）
        /// </summary>
        /// <param name="heightData">The terrain grid to resample onto.（作為目標的地形網格。）</param>
        /// <param name="wetCells">Receives how many cells hold water.（接收含水的格數。）</param>
        /// <param name="maxDepth">Receives the deepest sample.（接收最深的樣本。）</param>
        /// <returns>The depth grid at <see cref="DepthScale"/> m/unit, or <c>null</c> without data.（以 <see cref="DepthScale"/> 公尺／單位計的水深網格；無資料則為 <c>null</c>。）</returns>
        private ushort[] ResampleWaterDepth(TerrainHeightData heightData, out int wetCells, out float maxDepth)
        {
            wetCells = 0;
            maxDepth = 0f;

            WaterSurfaceData<SurfaceWater> water = Instance.Water.GetSurfaceData(out Unity.Jobs.JobHandle deps);
            deps.Complete();
            if (!water.depths.IsCreated || (water.resolution.x <= 0)) return null;

            int width = heightData.resolution.x;
            int height = heightData.resolution.z;
            ushort[] grid = new ushort[width * height];

            for (int z = 0; z < height; z++)
            {
                // Terrain cell -> world -> water grid, by each grid's own mapping
                // (world = cell / scale - offset; TerrainUtils.ToWorldSpace and its water twin).
                // （地形格 → 世界 → 水體網格，各依其自身映射。）
                float worldZ = (z / heightData.scale.z) - heightData.offset.z;
                float waterZ = (worldZ + water.offset.z) * water.scale.z;
                int z0 = math.clamp((int)math.floor(waterZ), 0, water.resolution.z - 1);
                int z1 = math.min(z0 + 1, water.resolution.z - 1);
                float tz = math.saturate(waterZ - z0);

                for (int x = 0; x < width; x++)
                {
                    float worldX = (x / heightData.scale.x) - heightData.offset.x;
                    float waterX = (worldX + water.offset.x) * water.scale.x;
                    int x0 = math.clamp((int)math.floor(waterX), 0, water.resolution.x - 1);
                    int x1 = math.min(x0 + 1, water.resolution.x - 1);
                    float tx = math.saturate(waterX - x0);

                    float depth = math.lerp(
                        math.lerp(water.depths[(z0 * water.resolution.x) + x0].m_Depth,
                                  water.depths[(z0 * water.resolution.x) + x1].m_Depth, tx),
                        math.lerp(water.depths[(z1 * water.resolution.x) + x0].m_Depth,
                                  water.depths[(z1 * water.resolution.x) + x1].m_Depth, tx), tz);

                    if (depth > 0f)
                    {
                        wetCells++;
                        maxDepth = math.max(maxDepth, depth);
                        grid[(z * width) + x] = (ushort)math.clamp(math.round(depth / DepthScale), 0f, 65535f);
                    }
                }
            }

            return grid;
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

            foreach (string[] pair in new[]
            {
                new[] { "grass", "colossal_TerrainGrassDiffuse" },
                new[] { "dirt", "colossal_TerrainDirtDiffuse" },
                new[] { "rock", "colossal_TerrainRockDiffuse" }
            })
            {
                string name = pair[0];
                try
                {
                    Texture texture = Shader.GetGlobalTexture(pair[1]);
                    if (texture == null)
                    {
                        report.AppendLine($"Surface texture '{name}': not bound; skipped");
                        continue;
                    }

                    PendingTexture captured = MaterialRipSystem.Capture(texture, name, name, 1024);
                    if (captured?.Pixels == null)
                    {
                        report.AppendLine($"Surface texture '{name}': readback failed; skipped");
                        continue;
                    }

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
                        StampSegment(surfaceIndex, heightData, geometry.m_Start, edgeTerrain, true, true);
                        StampSegment(surfaceIndex, heightData, geometry.m_End, edgeTerrain, true, true);
                        _edgesStamped++;
                    }

                    if (EntityManager.HasComponent<StartNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_StartNode, out TerrainComposition startTerrain))
                    {
                        StampNode(surfaceIndex, heightData,
                                  EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry, startTerrain);
                    }

                    if (EntityManager.HasComponent<EndNodeGeometry>(entity)
                        && TryGetGroundComposition(composition.m_EndNode, out TerrainComposition endTerrain))
                    {
                        StampNode(surfaceIndex, heightData,
                                  EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry, endTerrain);
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

                    StampOrphan(surfaceIndex, heightData,
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
        private void StampNode(byte[] surfaceIndex, TerrainHeightData heightData,
                               EdgeNodeGeometry node, TerrainComposition terrain)
        {
            // The game's own draw gate: nodes shorter than 5 cm on both sides draw nothing.
            // （遊戲自身的繪製門檻：兩側皆短於 5 公分的節點不繪製。）
            if (!(math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))) return;

            Segment strip;
            if (node.m_MiddleRadius > 0f)
            {
                // Roundabout: outer half to the middle curve, middle curve to the inner half,
                // then the same for the right half against the middle curve's end point.
                // （圓環：外半至中線曲線、中線曲線至內半；右半再對中線曲線終點重複。）
                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(surfaceIndex, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Left.m_Right;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, true);

                strip = node.m_Right;
                strip.m_Right = new Bezier4x3(node.m_Middle.d, node.m_Middle.d, node.m_Middle.d, node.m_Middle.d);
                StampSegment(surfaceIndex, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, true);
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
                StampSegment(surfaceIndex, heightData, strip, terrain, true, false);
                strip.m_Left = strip.m_Right;
                strip.m_Right = node.m_Middle;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, false);

                strip = node.m_Right;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, true);
                strip.m_Right = strip.m_Left;
                strip.m_Left = node.m_Middle;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, false);
            }
            else
            {
                // Straight through: left half to the middle, middle to the right half.
                // （直通：左半至中線、中線至右半。）
                strip = node.m_Left;
                strip.m_Right = node.m_Middle;
                StampSegment(surfaceIndex, heightData, strip, terrain, true, false);
                strip.m_Left = node.m_Middle;
                strip.m_Right = node.m_Right.m_Right;
                StampSegment(surfaceIndex, heightData, strip, terrain, false, true);
            }

            _nodesStamped++;
        }

        /// <summary>
        /// Stamp an orphan node's caps: two quarter-circle segment pairs per side, the game's own
        /// synthetic construction (<c>CullRoadsJob.AddOrphans</c>, world-axis aligned).
        /// （蓋印孤立節點的蓋：每側兩對四分之一圓區段，即遊戲自身的合成構造，對齊世界座標軸。）
        /// </summary>
        private void StampOrphan(byte[] surfaceIndex, TerrainHeightData heightData,
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
            StampSegment(surfaceIndex, heightData, segment, terrain, true, false);

            // +z half, right quarter — the left curve mirrored about the node.
            // （+z 半，右四分之一——左曲線對節點鏡射。）
            Segment mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x += width;
            mirrored.m_Right.b.x += width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(surfaceIndex, heightData, mirrored, terrain, false, true);

            // -z half, both quarters.（−z 半的兩個四分之一。）
            segment.m_Left = new Bezier4x3(
                new float3(position.x + half, position.y, position.z),
                new float3(position.x + half, position.y, position.z - (width * kappa)),
                new float3(position.x + (width * kappa), position.y, position.z - half),
                new float3(position.x, position.y, position.z - half));
            segment.m_Right = centre;
            StampSegment(surfaceIndex, heightData, segment, terrain, true, false);

            mirrored = segment;
            (mirrored.m_Left, mirrored.m_Right) = (mirrored.m_Right, mirrored.m_Left);
            mirrored.m_Right.a.x -= width;
            mirrored.m_Right.b.x -= width;
            mirrored.m_Right.c.x = (position.x * 2f) - mirrored.m_Right.c.x;
            StampSegment(surfaceIndex, heightData, mirrored, terrain, false, true);
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
        /// <param name="surfaceIndex">The mask, south-to-north rows.（遮罩，列序由南至北。）</param>
        /// <param name="heightData">The terrain grid.（地形網格。）</param>
        /// <param name="segment">The boundary curves.（邊界曲線。）</param>
        /// <param name="terrain">The composition's terrain offsets.（組成的地形偏移。）</param>
        /// <param name="trimLeft">Whether the left side takes its width offset.（左側是否套用寬度偏移。）</param>
        /// <param name="trimRight">Whether the right side takes its width offset.（右側是否套用寬度偏移。）</param>
        private static void StampSegment(byte[] surfaceIndex, TerrainHeightData heightData,
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

            float2 previousLeft = ToCell(segment.m_Left.a, heightData);
            float2 previousRight = ToCell(segment.m_Right.a, heightData);

            for (int i = 1; i <= steps; i++)
            {
                float t = (float)i / steps;
                float2 currentLeft = ToCell(Colossal.Mathematics.MathUtils.Position(segment.m_Left, t), heightData);
                float2 currentRight = ToCell(Colossal.Mathematics.MathUtils.Position(segment.m_Right, t), heightData);

                FillQuad(surfaceIndex, width, height, previousLeft, previousRight, currentRight, currentLeft);

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

        private static void FillQuad(byte[] surfaceIndex, int width, int height,
                                     float2 a, float2 b, float2 c, float2 d)
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
                        surfaceIndex[(row * width) + x] = 1;
                    }
                }
            }
        }
    }
}
