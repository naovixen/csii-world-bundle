using System;
using System.Collections.Generic;

namespace Carto.IO
{
    /// <summary>
    /// The manifest fields only the running game can answer, captured on the main thread.
    /// Everything else in <c>bundle.json</c> — layer presence, counts, bounds, the lifted
    /// <c>rasters</c> block — is derived from the bundle directory itself by
    /// <see cref="BundleWriter"/>, so that the manifest describes the bytes on disk rather than
    /// the state of the game at finalize time. This snapshot carries the remainder: identity
    /// (city, map, game build, mod version), the frame constants, and the geo anchor, which is
    /// the same user-configured projection the vector and GeoTIFF exports are referenced by —
    /// one anchor for every output, or the layers would not line up across formats.
    /// </summary>
    public sealed class BundleManifest
    {
        /// <summary>The producer identification, e.g. <c>carto-ripper 1.0.16</c>.</summary>
        public string Generator;

        /// <summary>The export wall-clock time, ISO 8601 UTC.</summary>
        public string GeneratedAt;

        /// <summary>The <c>game</c> block: name, version, mode.</summary>
        public Dictionary<string, object> Game;

        /// <summary>The city name, possibly empty in the editor.</summary>
        public string City;

        /// <summary>The map name, or <c>null</c> when unavailable.</summary>
        public string Map;

        /// <summary>The playable map edge length in metres.</summary>
        public double MapSize;

        /// <summary>The <c>geo</c> block (contract §2.3).</summary>
        public Dictionary<string, object> Geo;

        /// <summary>The texture-size cap in force, for <c>ripOptions</c>.</summary>
        public int TextureSizeCap;

        /// <summary>
        /// Capture the snapshot. Must run on the main thread — it touches ECS systems and the
        /// settings.
        /// </summary>
        /// <returns>The snapshot.</returns>
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
        /// shares one anchor.
        /// The anchor describes bundle-frame <c>[0,0,0]</c>: the vanilla GeoTIFF writer ties the
        /// raster's centre pixel — world origin — to <c>SourceCoordinates</c>, and this block
        /// states the identical fact as <c>originWorld</c>/<c>originProjected</c>.
        /// </summary>
        /// <param name="options">The export options.</param>
        /// <returns>The <c>geo</c> block.</returns>
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
                    // projection state.
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
                //
                ["north"] = 0.0
            };
        }

        /// <summary>
        /// The projection's Helmert parameters as the manifest's fixed 7-slot array.
        /// </summary>
        /// <param name="definition">The projection definition.</param>
        /// <returns>Seven values; all zero when no transform applies.</returns>
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
