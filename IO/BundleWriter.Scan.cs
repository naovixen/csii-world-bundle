using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The manifest's counts and bounds, measured by streaming each layer rather than loading it.
    /// </summary>
    /// <remarks>
    /// A full city is 1.28 M instances and 680 k lanes; nothing here may hold a layer in memory.
    /// Every scan is one pass that counts records and grows an AABB, and the mesh bounds come
    /// from each <c>.glb</c>'s own accessor min/max rather than from its vertices.
    /// </remarks>
    public static partial class BundleWriter
    {
        /// <summary>
        /// Count the records of a JSON Lines layer. Every line is LF-terminated (contract §1,
        /// enforced by <see cref="BundleFile.CreateText"/>), so records equal newlines.
        /// </summary>
        /// <param name="path">The layer file.</param>
        /// <returns>The record count.</returns>
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

        /// <summary>Count the files matching a pattern, zero when the directory is absent.</summary>
        private static int CountFiles(string directory, string pattern)
        {
            return Directory.Exists(directory) ? Directory.GetFiles(directory, pattern).Length : 0;
        }

        /// <summary>
        /// Scan a placement layer (<c>instances.jsonl</c> / <c>networks.jsonl</c>): count records
        /// and grow the bounds by each placement — the position itself, the centerline points, and
        /// the placed mesh's extents under the record's rotation and scale.
        /// </summary>
        /// <param name="path">The layer file.</param>
        /// <param name="meshBounds">Mesh extents by assetId, in the glb frame.</param>
        /// <param name="bounds">The bounds to grow.</param>
        /// <returns>The record count.</returns>
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
                //
                if (row.centerline != null)
                {
                    // The centerline form has no mesh; its geometry is the consumer's extrusion,
                    // so the record's width is what encloses it.
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
        /// The glb extents are mirrored back to the bundle frame (`x → -x`, contract §2.2), then
        /// carried through scale and rotation as a rotated box: centre through the full transform,
        /// half-extents through the rotation's absolute matrix — exact for a box, no sampling.
        /// </summary>
        private static void EncapsulatePlacedMesh(ref Aabb bounds, in Aabb glb, double[] pos, double[] rot, double[] scale)
        {
            // Mirror: the bundle-frame local box of glb extents [minX,maxX] is [-maxX,-minX].
            //
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

            // Row-major rotation matrix from the quaternion.
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
        /// Scan a JSON Lines layer whose records carry one world-space point list (<c>lanes.jsonl</c>
        /// paths, <c>lanemeshes.jsonl</c> bézier control points): count records and grow the bounds
        /// by every point.
        /// </summary>
        /// <param name="path">The layer file.</param>
        /// <param name="points">Select the record's point list, possibly <c>null</c>.</param>
        /// <param name="bounds">The bounds to grow.</param>
        /// <returns>The record count.</returns>
        private static long ScanPointRows<TRow>(string path, Func<TRow, double[][]> points, ref Aabb bounds)
        {
            long count = 0;
            JsonSerializer serializer = JsonSerializer.CreateDefault();
            using StreamReader stream = new(path);
            using JsonTextReader reader = new(stream) { SupportMultipleContent = true };
            while (reader.Read())
            {
                if (reader.TokenType != JsonToken.StartObject) continue;
                TRow row = serializer.Deserialize<TRow>(reader);
                count++;
                if (row == null) continue;

                double[][] list = points(row);
                if (list == null) continue;
                foreach (double[] point in list)
                {
                    if ((point != null) && (point.Length == 3)) bounds.Encapsulate(point[0], point[1], point[2]);
                }
            }

            return count;
        }

        /// <summary>
        /// Scan <c>clipvolumes.jsonl</c>: count records and grow the bounds by each strip's curves
        /// and band — a tunnel's clip bottom can lie below every other layer's geometry.
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
        /// on it.
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
                throw new InvalidDataException("terrain/rasters.json is missing its grid keys.");

            int width = size[0].Value<int>();
            int height = size[1].Value<int>();
            double originX = origin[0].Value<double>();
            double originZ = origin[2].Value<double>();

            // originWorld is the top-left cell centre; rows run toward -Z.
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
                //
                bounds.Encapsulate(west, bounds.Any ? bounds.MinY : 0, south);
                bounds.Encapsulate(east, bounds.Any ? bounds.MaxY : 0, north);
            }

            if (hasWater)
            {
                // The water surface can stand above the ground it covers; 0 means dry (§8).
                //
                ushort[] samples = Png.DecodeGray16(File.ReadAllBytes(Utils.IOUtils.CombinePath(outputDirectory, "terrain", "water_level.png")), out _, out _);
                ushort max = 0;
                foreach (ushort sample in samples)
                {
                    if (sample > max) max = sample;
                }
                if (max > 0) bounds.Encapsulate(west, (max * scale) + offset, south);
            }
        }

    }
}
