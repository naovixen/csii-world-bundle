using System;
using System.Collections.Generic;

namespace Carto.IO
{
    /// <summary>
    /// The manifest fields only the running game can answer, captured on the main thread.
    /// （唯有執行中的遊戲能回答的宣告檔欄位，於主執行緒擷取。）<br/><br/>
    /// Everything else in <c>bundle.json</c> — layer presence, counts, bounds, the lifted
    /// <c>rasters</c> block — is derived from the bundle directory itself by
    /// <see cref="BundleWriter"/>, so that the manifest describes the bytes on disk rather than
    /// the state of the game at finalize time. This snapshot carries the remainder: identity
    /// (city, map, game build, mod version), the frame constants, and the geo anchor, which is
    /// the same user-configured projection the vector and GeoTIFF exports are referenced by —
    /// one anchor for every output, or the layers would not line up across formats.<br/>
    /// （<c>bundle.json</c> 的其餘內容——各層的存在與否、數量、邊界、抬升的 <c>rasters</c> 區塊——
    /// 皆由 <see cref="BundleWriter"/> 自束目錄本身推得，使宣告檔描述的是磁碟上的位元組，
    /// 而非定稿當下的遊戲狀態。此快照攜帶其餘欄位：識別資訊（城市、地圖、遊戲版本、模組版本）、
    /// 座標系常數，以及地理錨點。錨點與向量及 GeoTIFF 輸出所用的使用者投影設定相同——
    /// 所有輸出共用一個錨點，否則各格式的圖層將無法對齊。）
    /// </summary>
    public sealed class BundleManifest
    {
        /// <summary>The producer identification, e.g. <c>carto-ripper 1.0.16</c>.（產生器識別字串。）</summary>
        public string Generator;

        /// <summary>The export wall-clock time, ISO 8601 UTC.（輸出時刻，ISO 8601 UTC。）</summary>
        public string GeneratedAt;

        /// <summary>The <c>game</c> block: name, version, mode.（<c>game</c> 區塊：名稱、版本、模式。）</summary>
        public Dictionary<string, object> Game;

        /// <summary>The city name, possibly empty in the editor.（城市名稱，於編輯器中可能為空。）</summary>
        public string City;

        /// <summary>The map name, or <c>null</c> when unavailable.（地圖名稱，無法取得時為 <c>null</c>。）</summary>
        public string Map;

        /// <summary>The playable map edge length in metres.（可遊玩地圖的邊長，公尺。）</summary>
        public double MapSize;

        /// <summary>The <c>geo</c> block (contract §2.3).（<c>geo</c> 區塊（契約 §2.3）。）</summary>
        public Dictionary<string, object> Geo;

        /// <summary>The texture-size cap in force, for <c>ripOptions</c>.（生效中的貼圖尺寸上限，供 <c>ripOptions</c> 使用。）</summary>
        public int TextureSizeCap;

        /// <summary>
        /// Capture the snapshot. Must run on the main thread — it touches ECS systems and the
        /// settings.（擷取快照。必須於主執行緒執行——其觸及 ECS 系統與設定。）
        /// </summary>
        /// <returns>The snapshot.（快照。）</returns>
        public static BundleManifest Capture()
        {
            Options options = Instance.Settings.GetOptions();

            return new BundleManifest
            {
                Generator = $"carto-ripper {Instance.Version}",
                GeneratedAt = DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"),
                Game = new Dictionary<string, object>
                {
                    ["name"] = "Cities: Skylines II",
                    ["version"] = global::Game.Version.current.fullVersion,
                    ["mode"] = Instance.GameMode switch
                    {
                        global::Game.GameMode.Game => "Game",
                        global::Game.GameMode.Editor => "Editor",
                        global::Game.GameMode.MainMenu => "MainMenu",
                        _ => "Other"
                    }
                },
                City = Instance.City?.cityName ?? string.Empty,
                Map = (Instance.GameMode == global::Game.GameMode.Game) ? Instance.Map?.mapName : null,
                MapSize = Instance.Terrain.playableArea.x,
                Geo = BuildGeo(options),
                TextureSizeCap = Instance.Settings.RipTextureSize
            };
        }

        /// <summary>
        /// Build the <c>geo</c> block from the export options — the same projection state the
        /// vector export and the GeoTIFF tiepoint are computed from, so every output of a session
        /// shares one anchor.（自輸出設定建構 <c>geo</c> 區塊——與向量輸出及 GeoTIFF 連接點
        /// 所用者為同一投影狀態，使同一工作階段的所有輸出共用一個錨點。）<br/><br/>
        /// The anchor describes bundle-frame <c>[0,0,0]</c>: the vanilla GeoTIFF writer ties the
        /// raster's centre pixel — world origin — to <c>SourceCoordinates</c>, and this block
        /// states the identical fact as <c>originWorld</c>/<c>originProjected</c>.<br/>
        /// （錨點描述束座標系的 <c>[0,0,0]</c>：原版 GeoTIFF 寫出器將網格中心像素——世界原點——
        /// 繫於 <c>SourceCoordinates</c>，此區塊以 <c>originWorld</c>/<c>originProjected</c>
        /// 陳述同一事實。）
        /// </summary>
        /// <param name="options">The export options.（輸出設定。）</param>
        /// <returns>The <c>geo</c> block.（<c>geo</c> 區塊。）</returns>
        private static Dictionary<string, object> BuildGeo(Options options)
        {
            Geodata.Coord projected = options.GetTMCoord();
            Geodata.CRS projection = options.GetTMProjection();
            Geodata.ProjectionDefinition definition = options.GetTMProjectionDefinition();

            Geodata.Coord wgs84 = Geodata.Transform.Apply(options.SourceCoordinates, options.SourceProjection,
                                                          Geodata.CRS.WGS84, options.SourceProjectionDefinition, new());

            string crs;
            Dictionary<string, object> tmParams;
            if (projection == Geodata.CRS.UTM)
            {
                bool north = projected.Hemisphere == Geodata.Hemisphere.North;
                int zone = projected.UTMZone;
                crs = $"EPSG:{(north ? 326 : 327)}{zone:D2}";
                tmParams = new Dictionary<string, object>
                {
                    // UTM's parameters are fixed by its definition, not carried on the parsed
                    // projection state.（UTM 的參數由其定義決定，而非攜帶於解析後的投影狀態。）
                    ["ellipsoid"] = "WGS84",
                    ["semiMajor"] = 6378137.0,
                    ["inverseFlattening"] = 298.257223563,
                    ["originLon"] = (zone * 6.0) - 183.0,
                    ["originLat"] = 0.0,
                    ["falseEasting"] = 500000.0,
                    ["falseNorthing"] = north ? 0.0 : 1E7,
                    ["scaleFactor"] = 0.9996,
                    ["utmZone"] = zone,
                    ["hemisphere"] = north ? "N" : "S",
                    ["toWgs84"] = new double[7]
                };
            }
            else
            {
                crs = "custom-TM";
                Geodata.EllipsoidDefinition ellipsoid = definition.ellipsoid;
                bool isWgs84 = (Math.Abs(ellipsoid.a - 6378137.0) < 1E-6) && (Math.Abs(ellipsoid.rf - 298.257223563) < 1E-6);
                tmParams = new Dictionary<string, object>
                {
                    ["ellipsoid"] = isWgs84 ? "WGS84" : "custom",
                    ["semiMajor"] = ellipsoid.a,
                    ["inverseFlattening"] = ellipsoid.rf,
                    ["originLon"] = definition.origin.x,
                    ["originLat"] = definition.origin.y,
                    ["falseEasting"] = definition.shift.x,
                    ["falseNorthing"] = definition.shift.y,
                    ["scaleFactor"] = definition.scaleFactor,
                    ["toWgs84"] = ToHelmert7(definition)
                };
            }

            return new Dictionary<string, object>
            {
                ["crs"] = crs,
                ["projection"] = (projection == Geodata.CRS.UTM) ? "UTM" : "TransverseMercator",
                ["tmParams"] = tmParams,
                ["originLatLon"] = new[] { wgs84.y, wgs84.x },
                ["originProjected"] = new[] { projected.x, projected.y },
                ["originWorld"] = new[] { 0.0, 0.0, 0.0 },
                // CS2's compass north is the +Z axis; the game has no map-rotation concept.
                // （CS2 的羅盤北方即 +Z 軸；遊戲沒有地圖旋轉的概念。）
                ["north"] = 0.0
            };
        }

        /// <summary>
        /// The projection's Helmert parameters as the manifest's fixed 7-slot array.
        /// （投影的赫爾默特參數，整理為宣告檔固定 7 欄的陣列。）
        /// </summary>
        /// <param name="definition">The projection definition.（投影定義。）</param>
        /// <returns>Seven values; all zero when no transform applies.（七個數值；無轉換時全為零。）</returns>
        private static double[] ToHelmert7(Geodata.ProjectionDefinition definition)
        {
            double[] result = new double[7];
            if (!definition.HasTransform) return result;

            double[] source = definition.transform.ToArray();
            for (int i = 0; i < Math.Min(source.Length, 7); i++) result[i] = source[i];
            return result;
        }
    }
}
