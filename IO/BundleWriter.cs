using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The Phase 7 finalize step: assemble <c>bundle.json</c> from the bundle directory.
    /// （第七階段的定稿步驟：自束目錄組裝 <c>bundle.json</c>。）<br/><br/>
    /// The manifest is derived from the <b>bytes on disk</b>, not from the game: the rip runs as
    /// separate steps (instances, meshes, networks, terrain) pressed at different times, and the
    /// manifest's counts must equal what those steps actually left behind — that equality is how a
    /// consumer detects a truncated or stale bundle (contract §3). Only identity and the geo
    /// anchor come from the running game, via <see cref="BundleManifest.Capture"/>.<br/>
    /// （宣告檔推導自<b>磁碟上的位元組</b>而非遊戲：擷取分為多個步驟（實例、網格、路網、地形）
    /// 於不同時刻執行，宣告檔的數量必須等於這些步驟實際留下的內容——此一相等性正是消費端
    /// 偵測截斷或過期束的方法（契約 §3）。僅識別資訊與地理錨點取自執行中的遊戲。）<br/><br/>
    /// Runs synchronously on the main thread inside <see cref="RipProgress.RunDeferred"/>, like
    /// the vanilla vector export: it reads a few hundred megabytes of JSON Lines and every mesh's
    /// GLB header, roughly half a minute on a full city. It writes exactly one file and never
    /// touches the layers.（如原版向量輸出般，於 <see cref="RipProgress.RunDeferred"/> 中在主執行緒
    /// 同步執行：讀取數百 MB 的 JSON Lines 與每個網格的 GLB 標頭，全城約半分鐘。僅寫出一個檔案，
    /// 絕不觸碰各層。）
    /// </summary>
    public static class BundleWriter
    {
        /// <summary>
        /// One layer the producer may have written: its manifest key, its conventional path, and
        /// whether it is a directory.（產出器可能寫出的一層：其宣告檔鍵、慣用路徑，及是否為目錄。）
        /// </summary>
        private readonly struct LayerDef
        {
            public readonly string Key;
            public readonly string Path;
            public readonly bool IsDirectory;

            public LayerDef(string key, string path, bool isDirectory = false)
            {
                Key = key;
                Path = path;
                IsDirectory = isDirectory;
            }
        }

        /// <summary>
        /// Every layer this producer can emit, in the order the manifest lists them.
        /// （此產出器可能寫出的所有層，依宣告檔列出的順序。）<br/><br/>
        /// The paths here are the producer's defaults; the contract's rule is that consumers read
        /// them from <c>layers</c>, which is what makes this table a private detail.<br/>
        /// （此處的路徑為產出器的預設值；契約規定消費端須自 <c>layers</c> 讀取路徑，
        /// 因此本表僅為私有細節。）
        /// </summary>
        private static readonly LayerDef[] Layers =
        {
            new("instances", "instances.jsonl"),
            new("networks", "networks.jsonl"),
            new("lanes", "lanes.jsonl"),
            new("laneGeometry", "lanemeshes.jsonl"),
            new("lanePrefabs", "laneprefabs.json"),
            new("materials", "materials.json"),
            new("colors", "colors.json"),
            new("lights", "lights.json"),
            new("meshes", "meshes/", isDirectory: true),
            new("networkMeshes", "networks/", isDirectory: true),
            new("laneMeshes", "lanemeshes/", isDirectory: true),
            new("textures", "textures/", isDirectory: true),
            new("terrain", "terrain/heightmap.png"),
            new("worldTerrain", "terrain/world_heightmap.png"),
            new("surfaceIndex", "terrain/surface_index.png"),
            new("splatmap", "terrain/splatmap.png"),
            new("userSplatmap", "terrain/user_splatmap.png"),
            new("surfaces", "terrain/surfaces/", isDirectory: true),
            new("water", "terrain/water_level.png"),
            new("areas", "areas.jsonl"),
            new("areaPrefabs", "areaprefabs.json"),
            new("clipVolumes", "clipvolumes.jsonl"),
            new("vector", "vector/", isDirectory: true),
            new("warnings", "warnings.jsonl")
        };

        /// <summary>
        /// An axis-aligned box accumulated in doubles, empty until the first point.
        /// （以雙精度累積的軸對齊方框，於首個點之前為空。）
        /// </summary>
        private struct Aabb
        {
            public double MinX, MinY, MinZ, MaxX, MaxY, MaxZ;
            public bool Any;

            public static Aabb Empty => new()
            {
                MinX = double.MaxValue, MinY = double.MaxValue, MinZ = double.MaxValue,
                MaxX = double.MinValue, MaxY = double.MinValue, MaxZ = double.MinValue
            };

            public void Encapsulate(double x, double y, double z)
            {
                if (x < MinX) MinX = x;
                if (y < MinY) MinY = y;
                if (z < MinZ) MinZ = z;
                if (x > MaxX) MaxX = x;
                if (y > MaxY) MaxY = y;
                if (z > MaxZ) MaxZ = z;
                Any = true;
            }

            public void Encapsulate(in Aabb other)
            {
                if (!other.Any) return;
                Encapsulate(other.MinX, other.MinY, other.MinZ);
                Encapsulate(other.MaxX, other.MaxY, other.MaxZ);
            }
        }

        /// <summary>A record of a placed thing, as far as bounds care.（就邊界所需而言的放置紀錄。）</summary>
        private sealed class PlacementRow
        {
            public string assetId { get; set; }
            public double[] pos { get; set; }
            public double[] rot { get; set; }
            public double[] scale { get; set; }
            public double[][] centerline { get; set; }
            public double? width { get; set; }
        }

        /// <summary>A surface-area record, as far as bounds care.（就邊界所需而言的地表區域紀錄。）</summary>
        private sealed class AreaRow
        {
            public double[][] nodes { get; set; }
        }

        /// <summary>A clip-volume record, as far as bounds care.（就邊界所需而言的裁剪體紀錄。）</summary>
        private sealed class ClipRow
        {
            public double? bottom { get; set; }
            public double? top { get; set; }
            public double[][] left { get; set; }
            public double[][] right { get; set; }
        }

        /// <summary>
        /// Assemble and write <c>bundle.json</c> for the bundle at <paramref name="outputDirectory"/>.
        /// （為位於 <paramref name="outputDirectory"/> 的束組裝並寫出 <c>bundle.json</c>。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root.（束的根目錄。）</param>
        /// <returns>A human-readable report.（供人閱讀的報告。）</returns>
        public static string WriteManifest(string outputDirectory)
        {
            if (!Directory.Exists(outputDirectory))
                throw new DirectoryNotFoundException($"No bundle at {outputDirectory}. 該處沒有束。");

            BundleManifest snapshot = BundleManifest.Capture();
            StringBuilder report = new();

            // ---- 1. Which layers exist.（哪些層存在。）
            Dictionary<string, string> layers = new();
            foreach (LayerDef layer in Layers)
            {
                string path = Utils.IOUtils.CombinePath(outputDirectory, layer.Path.TrimEnd('/'));
                bool present = layer.IsDirectory
                    ? Directory.Exists(path) && (Directory.GetFileSystemEntries(path).Length > 0)
                    : File.Exists(path);
                if (present) layers[layer.Key] = layer.Path;
            }
            if (layers.Count == 0) throw new InvalidOperationException("The directory holds no bundle layer at all — rip something first. 目錄中沒有任何束層——請先行擷取。");

            // ---- 2. Mesh extents, for bounds.（網格範圍，供邊界使用。）
            Dictionary<string, Aabb> meshBounds = ReadMeshBounds(Utils.IOUtils.CombinePath(outputDirectory, "meshes"), report);
            Dictionary<string, Aabb> networkMeshBounds = ReadMeshBounds(Utils.IOUtils.CombinePath(outputDirectory, "networks"), report);

            // ---- 3. Record layers: counts and bounds in one pass each.（紀錄層：一次掃描同時計數與納界。）
            Aabb bounds = Aabb.Empty;
            long instanceCount = 0, networkCount = 0, areaCount = 0, clipCount = 0;

            if (layers.ContainsKey("instances"))
            {
                instanceCount = ScanPlacements(Utils.IOUtils.CombinePath(outputDirectory, "instances.jsonl"), meshBounds, ref bounds);
            }
            if (layers.ContainsKey("networks"))
            {
                networkCount = ScanPlacements(Utils.IOUtils.CombinePath(outputDirectory, "networks.jsonl"), networkMeshBounds, ref bounds);
            }
            if (layers.ContainsKey("areas"))
            {
                areaCount = ScanAreas(Utils.IOUtils.CombinePath(outputDirectory, "areas.jsonl"), ref bounds);
            }
            if (layers.ContainsKey("clipVolumes"))
            {
                clipCount = ScanClipVolumes(Utils.IOUtils.CombinePath(outputDirectory, "clipvolumes.jsonl"), ref bounds);
            }

            long laneCount = layers.ContainsKey("lanes") ? CountLines(Utils.IOUtils.CombinePath(outputDirectory, "lanes.jsonl")) : 0;
            long laneGeometryCount = layers.ContainsKey("laneGeometry") ? CountLines(Utils.IOUtils.CombinePath(outputDirectory, "lanemeshes.jsonl")) : 0;
            long warningCount = layers.ContainsKey("warnings") ? CountLines(Utils.IOUtils.CombinePath(outputDirectory, "warnings.jsonl")) : 0;

            int materialCount = 0;
            if (layers.ContainsKey("materials"))
            {
                JObject materials = JObject.Parse(File.ReadAllText(Utils.IOUtils.CombinePath(outputDirectory, "materials.json")));
                materialCount = materials.Count;
            }

            int meshFileCount = CountFiles(Utils.IOUtils.CombinePath(outputDirectory, "meshes"), "*.glb");
            int textureFileCount = CountFiles(Utils.IOUtils.CombinePath(outputDirectory, "textures"), "*.png");

            // ---- 4. The rasters block, lifted verbatim from the terrain rip's sidecar; the
            // playable rasters also contribute to bounds (contract §3 — the world backdrop is
            // deliberately excluded).（rasters 區塊，自地形擷取的側檔原樣抬升；可遊玩網格亦納入
            // 邊界（契約 §3——世界背景刻意排除在外）。）
            JObject rasters = null;
            bool anyRaster = layers.ContainsKey("terrain") || layers.ContainsKey("worldTerrain")
                          || layers.ContainsKey("surfaceIndex") || layers.ContainsKey("water");
            if (anyRaster)
            {
                string sidecar = Utils.IOUtils.CombinePath(outputDirectory, "terrain", "rasters.json");
                if (!File.Exists(sidecar))
                    throw new FileNotFoundException("Raster layers exist but terrain/rasters.json does not — re-run the terrain rip. 網格層存在但缺少 terrain/rasters.json——請重新執行地形擷取。");
                rasters = JObject.Parse(File.ReadAllText(sidecar));

                EncapsulateRasters(outputDirectory, rasters, layers.ContainsKey("terrain"),
                                   layers.ContainsKey("water"), ref bounds);
            }

            if (!bounds.Any)
                throw new InvalidOperationException("No layer contributed any geometry, so bounds cannot be stated. 沒有任何層提供幾何，無從陳述邊界。");

            // ---- 5. Assemble.（組裝。）
            Dictionary<string, object> counts = new()
            {
                ["meshes"] = meshFileCount,
                ["materials"] = materialCount,
                ["textures"] = textureFileCount,
                ["instances"] = instanceCount,
                ["networkSegments"] = networkCount
            };
            if (layers.ContainsKey("warnings")) counts["warnings"] = warningCount;
            if (layers.ContainsKey("lanes")) counts["lanes"] = laneCount;
            if (layers.ContainsKey("laneGeometry")) counts["laneGeometry"] = laneGeometryCount;
            if (layers.ContainsKey("areas")) counts["areas"] = areaCount;
            if (layers.ContainsKey("clipVolumes")) counts["clipVolumes"] = clipCount;

            Dictionary<string, object> manifest = new()
            {
                ["schema"] = 1,
                ["generator"] = snapshot.Generator,
                ["generatedAt"] = snapshot.GeneratedAt,
                ["game"] = snapshot.Game,
                ["city"] = snapshot.City
            };
            if (!string.IsNullOrEmpty(snapshot.Map)) manifest["map"] = snapshot.Map;

            manifest["frame"] = new Dictionary<string, object>
            {
                ["units"] = "m",
                ["handedness"] = "left",
                ["up"] = "Y",
                ["axes"] = "X=east,Y=up,Z=north",
                ["meshFrame"] = new Dictionary<string, object>
                {
                    ["handedness"] = "right",
                    ["conversion"] = "negate-x",
                    ["windingReversed"] = true
                }
            };
            manifest["geo"] = snapshot.Geo;
            manifest["bounds"] = new Dictionary<string, object>
            {
                // Rounded outward, so the stated bounds still enclose after rounding.
                // （向外進位，使陳述的邊界於捨入後仍能圍住內容。）
                ["min"] = new[] { Floor3(bounds.MinX), Floor3(bounds.MinY), Floor3(bounds.MinZ) },
                ["max"] = new[] { Ceil3(bounds.MaxX), Ceil3(bounds.MaxY), Ceil3(bounds.MaxZ) }
            };
            manifest["mapSize"] = snapshot.MapSize;
            manifest["counts"] = counts;
            manifest["layers"] = layers;
            if (rasters != null) manifest["rasters"] = rasters;
            manifest["ripOptions"] = new Dictionary<string, object>
            {
                ["meshSource"] = "runtime",
                ["lodCap"] = 0,
                ["textureSizeCap"] = snapshot.TextureSizeCap,
                ["includeSubObjects"] = true,
                ["includeVector"] = layers.ContainsKey("vector")
            };

            string manifestPath = Utils.IOUtils.CombinePath(outputDirectory, "bundle.json");
            File.WriteAllText(manifestPath, JsonConvert.SerializeObject(manifest, Formatting.Indented));

            // ---- 6. Report.（報告。）
            report.Insert(0, $"Wrote bundle.json — {layers.Count} layers.\n"
                           + $"{instanceCount} instances, {networkCount} network records, {laneCount} lanes,\n"
                           + $"{meshFileCount} meshes, {materialCount} materials, {textureFileCount} textures,\n"
                           + $"{areaCount} areas, {clipCount} clip volumes, {warningCount} warnings.\n"
                           + $"Bounds [{Floor3(bounds.MinX)}, {Floor3(bounds.MinY)}, {Floor3(bounds.MinZ)}] .. "
                           + $"[{Ceil3(bounds.MaxX)}, {Ceil3(bounds.MaxY)}, {Ceil3(bounds.MaxZ)}].\n");
            Instance.Log.Info($"BundleWriter: {report}");
            return report.ToString();
        }

        /// <summary>Round down to millimetres.（向下捨入至公釐。）</summary>
        private static double Floor3(double value) => Math.Floor(value * 1000.0) / 1000.0;

        /// <summary>Round up to millimetres.（向上進位至公釐。）</summary>
        private static double Ceil3(double value) => Math.Ceiling(value * 1000.0) / 1000.0;

        /// <summary>
        /// Count the records of a JSON Lines layer. Every line is LF-terminated (contract §1,
        /// enforced by <see cref="BundleFile.CreateText"/>), so records equal newlines.
        /// （計算 JSON Lines 層的紀錄數。每行皆以 LF 結尾（契約 §1），故紀錄數即換行數。）
        /// </summary>
        /// <param name="path">The layer file.（層檔案。）</param>
        /// <returns>The record count.（紀錄數。）</returns>
        private static long CountLines(string path)
        {
            using FileStream stream = File.OpenRead(path);
            byte[] buffer = new byte[1 << 20];
            long lines = 0;
            int read;
            while ((read = stream.Read(buffer, 0, buffer.Length)) > 0)
            {
                for (int i = 0; i < read; i++)
                {
                    if (buffer[i] == (byte)'\n') lines++;
                }
            }

            return lines;
        }

        /// <summary>Count the files matching a pattern, zero when the directory is absent.（計算符合樣式的檔案數，目錄不存在時為零。）</summary>
        private static int CountFiles(string directory, string pattern)
        {
            return Directory.Exists(directory) ? Directory.GetFiles(directory, pattern).Length : 0;
        }

        /// <summary>
        /// Scan a placement layer (<c>instances.jsonl</c> / <c>networks.jsonl</c>): count records
        /// and grow the bounds by each placement — the position itself, the centerline points, and
        /// the placed mesh's extents under the record's rotation and scale.
        /// （掃描放置層：計數並以每筆放置擴張邊界——位置本身、中心線各點，
        /// 以及該紀錄旋轉與縮放下所放置網格的範圍。）
        /// </summary>
        /// <param name="path">The layer file.（層檔案。）</param>
        /// <param name="meshBounds">Mesh extents by assetId, in the glb frame.（依 assetId 的網格範圍，glb 座標系。）</param>
        /// <param name="bounds">The bounds to grow.（待擴張的邊界。）</param>
        /// <returns>The record count.（紀錄數。）</returns>
        private static long ScanPlacements(string path, Dictionary<string, Aabb> meshBounds, ref Aabb bounds)
        {
            long count = 0;
            JsonSerializer serializer = JsonSerializer.CreateDefault();
            using StreamReader stream = new(path);
            using JsonTextReader reader = new(stream) { SupportMultipleContent = true };
            while (reader.Read())
            {
                if (reader.TokenType != JsonToken.StartObject) continue;
                PlacementRow row = serializer.Deserialize<PlacementRow>(reader);
                count++;
                if (row == null) continue;

                // A network record carries either a placement or an inline centerline (§5) —
                // a centerline-only record still bounds real geometry, so never skip it.
                // （路網紀錄攜帶放置點或內嵌中心線兩者之一（§5）——僅有中心線的紀錄
                //   仍界定真實幾何，故絕不可略過。）
                if (row.centerline != null)
                {
                    // The centerline form has no mesh; its geometry is the consumer's extrusion,
                    // so the record's width is what encloses it.（中心線形式沒有網格；其幾何為
                    // 消費端的擠出結果，故以紀錄的寬度圍住它。）
                    double half = (row.width ?? 0) * 0.5;
                    foreach (double[] point in row.centerline)
                    {
                        if ((point == null) || (point.Length != 3)) continue;
                        bounds.Encapsulate(point[0] - half, point[1], point[2] - half);
                        bounds.Encapsulate(point[0] + half, point[1], point[2] + half);
                    }
                }

                if ((row.pos == null) || (row.pos.Length != 3)) continue;

                bounds.Encapsulate(row.pos[0], row.pos[1], row.pos[2]);

                if ((row.assetId != null) && meshBounds.TryGetValue(row.assetId, out Aabb glb) && glb.Any)
                {
                    EncapsulatePlacedMesh(ref bounds, glb, row.pos, row.rot, row.scale);
                }
            }

            return count;
        }

        /// <summary>
        /// Grow the bounds by a mesh's extents placed at an instance transform.
        /// （以置於實例變換下的網格範圍擴張邊界。）<br/><br/>
        /// The glb extents are mirrored back to the bundle frame (`x → -x`, contract §2.2), then
        /// carried through scale and rotation as a rotated box: centre through the full transform,
        /// half-extents through the rotation's absolute matrix — exact for a box, no sampling.
        /// （glb 範圍先鏡射回束座標系（契約 §2.2），再以旋轉方框方式通過縮放與旋轉：
        /// 中心走完整變換，半徑走旋轉矩陣的絕對值——對方框而言為精確解。）
        /// </summary>
        private static void EncapsulatePlacedMesh(ref Aabb bounds, in Aabb glb, double[] pos, double[] rot, double[] scale)
        {
            // Mirror: the bundle-frame local box of glb extents [minX,maxX] is [-maxX,-minX].
            // （鏡射：glb 範圍 [minX,maxX] 於束座標系的局部方框為 [-maxX,-minX]。）
            double centerX = -(glb.MinX + glb.MaxX) * 0.5;
            double centerY = (glb.MinY + glb.MaxY) * 0.5;
            double centerZ = (glb.MinZ + glb.MaxZ) * 0.5;
            double halfX = (glb.MaxX - glb.MinX) * 0.5;
            double halfY = (glb.MaxY - glb.MinY) * 0.5;
            double halfZ = (glb.MaxZ - glb.MinZ) * 0.5;

            double scaleX = 1, scaleY = 1, scaleZ = 1;
            if ((scale != null) && (scale.Length == 3))
            {
                scaleX = scale[0];
                scaleY = scale[1];
                scaleZ = scale[2];
            }
            centerX *= scaleX; centerY *= scaleY; centerZ *= scaleZ;
            halfX *= Math.Abs(scaleX); halfY *= Math.Abs(scaleY); halfZ *= Math.Abs(scaleZ);

            double qx = 0, qy = 0, qz = 0, qw = 1;
            if ((rot != null) && (rot.Length == 4))
            {
                qx = rot[0]; qy = rot[1]; qz = rot[2]; qw = rot[3];
            }

            // Row-major rotation matrix from the quaternion.（由四元數展開的旋轉矩陣，列為主。）
            double r00 = 1 - 2 * (qy * qy + qz * qz), r01 = 2 * (qx * qy - qz * qw), r02 = 2 * (qx * qz + qy * qw);
            double r10 = 2 * (qx * qy + qz * qw), r11 = 1 - 2 * (qx * qx + qz * qz), r12 = 2 * (qy * qz - qx * qw);
            double r20 = 2 * (qx * qz - qy * qw), r21 = 2 * (qy * qz + qx * qw), r22 = 1 - 2 * (qx * qx + qy * qy);

            double worldCenterX = pos[0] + r00 * centerX + r01 * centerY + r02 * centerZ;
            double worldCenterY = pos[1] + r10 * centerX + r11 * centerY + r12 * centerZ;
            double worldCenterZ = pos[2] + r20 * centerX + r21 * centerY + r22 * centerZ;
            double worldHalfX = Math.Abs(r00) * halfX + Math.Abs(r01) * halfY + Math.Abs(r02) * halfZ;
            double worldHalfY = Math.Abs(r10) * halfX + Math.Abs(r11) * halfY + Math.Abs(r12) * halfZ;
            double worldHalfZ = Math.Abs(r20) * halfX + Math.Abs(r21) * halfY + Math.Abs(r22) * halfZ;

            bounds.Encapsulate(worldCenterX - worldHalfX, worldCenterY - worldHalfY, worldCenterZ - worldHalfZ);
            bounds.Encapsulate(worldCenterX + worldHalfX, worldCenterY + worldHalfY, worldCenterZ + worldHalfZ);
        }

        /// <summary>
        /// Scan <c>areas.jsonl</c>: count records and grow the bounds by every node.
        /// （掃描 <c>areas.jsonl</c>：計數並以每個節點擴張邊界。）
        /// </summary>
        private static long ScanAreas(string path, ref Aabb bounds)
        {
            long count = 0;
            JsonSerializer serializer = JsonSerializer.CreateDefault();
            using StreamReader stream = new(path);
            using JsonTextReader reader = new(stream) { SupportMultipleContent = true };
            while (reader.Read())
            {
                if (reader.TokenType != JsonToken.StartObject) continue;
                AreaRow row = serializer.Deserialize<AreaRow>(reader);
                count++;
                if (row?.nodes == null) continue;
                foreach (double[] node in row.nodes)
                {
                    if ((node != null) && (node.Length == 3)) bounds.Encapsulate(node[0], node[1], node[2]);
                }
            }

            return count;
        }

        /// <summary>
        /// Scan <c>clipvolumes.jsonl</c>: count records and grow the bounds by each strip's curves
        /// and band — a tunnel's clip bottom can lie below every other layer's geometry.
        /// （掃描 <c>clipvolumes.jsonl</c>：計數並以每條帶的曲線與高度帶擴張邊界——
        /// 隧道裁剪帶的底部可能低於其他所有層的幾何。）
        /// </summary>
        private static long ScanClipVolumes(string path, ref Aabb bounds)
        {
            long count = 0;
            JsonSerializer serializer = JsonSerializer.CreateDefault();
            using StreamReader stream = new(path);
            using JsonTextReader reader = new(stream) { SupportMultipleContent = true };
            while (reader.Read())
            {
                if (reader.TokenType != JsonToken.StartObject) continue;
                ClipRow row = serializer.Deserialize<ClipRow>(reader);
                count++;
                if (row == null) continue;

                foreach (double[][] curve in new[] { row.left, row.right })
                {
                    if (curve == null) continue;
                    foreach (double[] point in curve)
                    {
                        if ((point == null) || (point.Length != 3)) continue;
                        bounds.Encapsulate(point[0], point[1], point[2]);
                        if (row.bottom.HasValue) bounds.Encapsulate(point[0], row.bottom.Value, point[2]);
                        if (row.top.HasValue) bounds.Encapsulate(point[0], row.top.Value, point[2]);
                    }
                }
            }

            return count;
        }

        /// <summary>
        /// Grow the bounds by the playable raster layers: the grid's rectangle in X/Z, and the
        /// decoded height range — terrain can rise above and fall below everything that was built
        /// on it.（以可遊玩網格層擴張邊界：X/Z 為網格矩形，高度取自解碼後的樣本範圍——
        /// 地形可高於或低於其上建造的一切。）
        /// </summary>
        private static void EncapsulateRasters(string outputDirectory, JObject rasters,
                                               bool hasTerrain, bool hasWater, ref Aabb bounds)
        {
            double resolution = rasters.Value<double?>("resolution") ?? 0;
            JArray size = rasters["size"] as JArray;
            JArray origin = rasters["originWorld"] as JArray;
            double scale = rasters.Value<double?>("elevationScale") ?? 0;
            double offset = rasters.Value<double?>("elevationOffset") ?? 0;
            if ((resolution <= 0) || (size == null) || (origin == null))
                throw new InvalidDataException("terrain/rasters.json is missing its grid keys. terrain/rasters.json 缺少網格鍵。");

            int width = size[0].Value<int>();
            int height = size[1].Value<int>();
            double originX = origin[0].Value<double>();
            double originZ = origin[2].Value<double>();

            // originWorld is the top-left cell centre; rows run toward -Z.（originWorld 為左上格心；列朝 -Z。）
            double west = originX - (resolution * 0.5);
            double east = west + (resolution * width);
            double north = originZ + (resolution * 0.5);
            double south = north - (resolution * height);

            if (hasTerrain)
            {
                ushort[] samples = Png.DecodeGray16(File.ReadAllBytes(Utils.IOUtils.CombinePath(outputDirectory, "terrain", "heightmap.png")), out _, out _);
                ushort min = ushort.MaxValue, max = ushort.MinValue;
                foreach (ushort sample in samples)
                {
                    if (sample < min) min = sample;
                    if (sample > max) max = sample;
                }
                bounds.Encapsulate(west, (min * scale) + offset, south);
                bounds.Encapsulate(east, (max * scale) + offset, north);
            }
            else
            {
                // No height information without the terrain layer — the rectangle alone.
                // （無地形層即無高度資訊——僅納入矩形。）
                bounds.Encapsulate(west, bounds.Any ? bounds.MinY : 0, south);
                bounds.Encapsulate(east, bounds.Any ? bounds.MaxY : 0, north);
            }

            if (hasWater)
            {
                // The water surface can stand above the ground it covers; 0 means dry (§8).
                // （水面可高於其覆蓋的地面；0 代表無水（§8）。）
                ushort[] samples = Png.DecodeGray16(File.ReadAllBytes(Utils.IOUtils.CombinePath(outputDirectory, "terrain", "water_level.png")), out _, out _);
                ushort max = 0;
                foreach (ushort sample in samples)
                {
                    if (sample > max) max = sample;
                }
                if (max > 0) bounds.Encapsulate(west, (max * scale) + offset, south);
            }
        }

        /// <summary>
        /// Read the extents of every <c>.glb</c> in a directory, keyed by assetId (the file stem),
        /// in the glb frame.（讀取目錄中每個 <c>.glb</c> 的範圍，以 assetId（檔名主幹）為鍵，
        /// glb 座標系。）<br/><br/>
        /// Only the JSON chunk is read — the accessor <c>min</c>/<c>max</c> the producer writes on
        /// every POSITION accessor, carried through each node's translation/rotation/scale. The
        /// binary chunk is never touched, which is what keeps twelve thousand files cheap.
        /// （僅讀取 JSON 區塊——產出器在每個 POSITION accessor 上寫出的 <c>min</c>/<c>max</c>，
        /// 並通過各節點的平移／旋轉／縮放。二進位區塊完全不碰，萬餘檔案因此廉價。）
        /// </summary>
        private static Dictionary<string, Aabb> ReadMeshBounds(string directory, StringBuilder report)
        {
            Dictionary<string, Aabb> result = new();
            if (!Directory.Exists(directory)) return result;

            int unreadable = 0;
            foreach (string path in Directory.GetFiles(directory, "*.glb"))
            {
                try
                {
                    Aabb glbBounds = ReadGlbBounds(path);
                    if (glbBounds.Any) result[Path.GetFileNameWithoutExtension(path)] = glbBounds;
                }
                catch (Exception ex)
                {
                    if (unreadable++ == 0) Instance.Log.Warn($"Unreadable glb {path}: {ex.Message}");
                }
            }
            if (unreadable > 0) report.AppendLine($"{unreadable} glb file(s) in {Path.GetFileName(directory)}/ were unreadable; their instances contribute position only.");

            return result;
        }

        /// <summary>
        /// The whole-file extents of one glb, from its JSON chunk.（單一 glb 的整檔範圍，取自其 JSON 區塊。）
        /// </summary>
        private static Aabb ReadGlbBounds(string path)
        {
            JObject gltf = JObject.Parse(ReadGlbJsonChunk(path));

            // Per-mesh extents: the union of each primitive's POSITION accessor min/max.
            // （逐網格範圍：各 primitive 之 POSITION accessor min/max 的聯集。）
            JArray accessors = gltf["accessors"] as JArray;
            JArray meshes = gltf["meshes"] as JArray;
            List<Aabb> meshBounds = new();
            if (meshes != null)
            {
                foreach (JToken mesh in meshes)
                {
                    Aabb box = Aabb.Empty;
                    foreach (JToken primitive in (mesh["primitives"] as JArray) ?? new JArray())
                    {
                        JToken position = primitive["attributes"]?["POSITION"];
                        if ((position == null) || (accessors == null)) continue;
                        JToken accessor = accessors[position.Value<int>()];
                        JArray min = accessor["min"] as JArray;
                        JArray max = accessor["max"] as JArray;
                        if ((min == null) || (max == null) || (min.Count != 3) || (max.Count != 3)) continue;
                        box.Encapsulate(min[0].Value<double>(), min[1].Value<double>(), min[2].Value<double>());
                        box.Encapsulate(max[0].Value<double>(), max[1].Value<double>(), max[2].Value<double>());
                    }
                    meshBounds.Add(box);
                }
            }

            // Walk the scene's node tree, carrying child extents through each child's TRS.
            // （走訪場景的節點樹，令子節點範圍通過其平移／旋轉／縮放。）
            JArray nodes = gltf["nodes"] as JArray;
            Aabb total = Aabb.Empty;
            JToken scene = gltf["scenes"]?[0]?["nodes"];
            if ((scene is JArray roots) && (nodes != null))
            {
                foreach (JToken root in roots)
                {
                    Aabb rootBounds = NodeBounds(nodes, meshBounds, root.Value<int>(), 0);
                    if (rootBounds.Any) total.Encapsulate(TransformNode(rootBounds, nodes[root.Value<int>()]));
                }
            }

            return total;
        }

        /// <summary>
        /// A node's extents in its own frame: its mesh plus each child's extents carried through
        /// that child's TRS.（節點於自身座標系的範圍：其網格加上各子節點經其變換後的範圍。）
        /// </summary>
        private static Aabb NodeBounds(JArray nodes, List<Aabb> meshBounds, int index, int depth)
        {
            Aabb result = Aabb.Empty;
            if ((depth > 16) || (index < 0) || (index >= nodes.Count)) return result;

            JToken node = nodes[index];
            JToken mesh = node["mesh"];
            if ((mesh != null) && (mesh.Value<int>() >= 0) && (mesh.Value<int>() < meshBounds.Count))
            {
                result.Encapsulate(meshBounds[mesh.Value<int>()]);
            }

            foreach (JToken child in (node["children"] as JArray) ?? new JArray())
            {
                int childIndex = child.Value<int>();
                Aabb childBounds = NodeBounds(nodes, meshBounds, childIndex, depth + 1);
                if (childBounds.Any) result.Encapsulate(TransformNode(childBounds, nodes[childIndex]));
            }

            return result;
        }

        /// <summary>
        /// Carry a box through a glTF node's translation/rotation/scale (matrix order T·R·S).
        /// （令方框通過 glTF 節點的平移／旋轉／縮放（矩陣次序 T·R·S）。）
        /// </summary>
        private static Aabb TransformNode(in Aabb box, JToken node)
        {
            double[] translation = ToDoubles(node["translation"] as JArray, 3) ?? new double[] { 0, 0, 0 };
            double[] rotation = ToDoubles(node["rotation"] as JArray, 4) ?? new double[] { 0, 0, 0, 1 };
            double[] scale = ToDoubles(node["scale"] as JArray, 3);

            Aabb result = Aabb.Empty;

            // Reuse the placement transform: it is the same rotated-box carry.（重用放置變換：同為旋轉方框運算。）
            // The mirror inside EncapsulatePlacedMesh must not apply here, so pre-mirror to cancel it.
            // （EncapsulatePlacedMesh 內的鏡射於此不適用，故預先鏡射以抵銷之。）
            Aabb premirrored = box;
            premirrored.MinX = -box.MaxX;
            premirrored.MaxX = -box.MinX;
            EncapsulatePlacedMesh(ref result, premirrored, translation, rotation, scale);
            return result;
        }

        /// <summary>Read a fixed-length number array, or <c>null</c>.（讀取定長數字陣列，否則為 <c>null</c>。）</summary>
        private static double[] ToDoubles(JArray array, int length)
        {
            if ((array == null) || (array.Count != length)) return null;

            double[] result = new double[length];
            for (int i = 0; i < length; i++) result[i] = array[i].Value<double>();
            return result;
        }

        /// <summary>
        /// Read a GLB container's JSON chunk as text, without touching the binary chunk.
        /// （讀取 GLB 容器的 JSON 區塊為文字，不觸碰二進位區塊。）
        /// </summary>
        private static string ReadGlbJsonChunk(string path)
        {
            using FileStream stream = File.OpenRead(path);
            using BinaryReader reader = new(stream);

            if (reader.ReadUInt32() != 0x46546C67) throw new InvalidDataException("Bad GLB magic. GLB 魔數不符。");
            if (reader.ReadUInt32() != 2) throw new InvalidDataException("Not a GLB v2 container. 並非 GLB v2 容器。");
            reader.ReadUInt32(); // Total length; unused here.（總長度；此處不需。）

            while (stream.Position + 8 <= stream.Length)
            {
                uint chunkLength = reader.ReadUInt32();
                uint chunkType = reader.ReadUInt32();
                if (chunkType == 0x4E4F534A) // "JSON"
                {
                    return Encoding.UTF8.GetString(reader.ReadBytes((int)chunkLength));
                }

                stream.Seek(chunkLength + ((4 - (chunkLength % 4)) % 4), SeekOrigin.Current);
            }

            throw new InvalidDataException("No JSON chunk. 缺少 JSON 區塊。");
        }
    }
}
