using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The layer table: every file the contract names, and the row shape each is scanned as.
    /// </summary>
    public static partial class BundleWriter
    {
        /// <summary>
        /// One layer the producer may have written: its manifest key, its conventional path, and
        /// whether it is a directory.
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
        /// The paths here are the producer's defaults; the contract's rule is that consumers read
        /// them from <c>layers</c>, which is what makes this table a private detail.
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
            new("terrainFine", "terrain/heightmap_fine.png"),
            new("worldTerrain", "terrain/world_heightmap.png"),
            new("surfaceIndex", "terrain/surface_index.png"),
            new("splatmap", "terrain/splatmap.png"),
            new("userSplatmap", "terrain/user_splatmap.png"),
            new("surfaces", "terrain/surfaces/", isDirectory: true),
            new("water", "terrain/water_level.png"),
            new("areas", "areas.jsonl"),
            new("areaPrefabs", "areaprefabs.json"),
            new("districts", "districts.jsonl"),
            new("clipVolumes", "clipvolumes.jsonl"),
            new("grading", "grading.json"),
            new("vector", "vector/", isDirectory: true),
            new("warnings", "warnings.jsonl")
        };

        /// <summary>
        /// An axis-aligned box accumulated in doubles, empty until the first point.
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

        /// <summary>A record of a placed thing, as far as bounds care.</summary>
        private sealed class PlacementRow
        {
            public string assetId { get; set; }
            public double[] pos { get; set; }
            public double[] rot { get; set; }
            public double[] scale { get; set; }
            public double[][] centerline { get; set; }
            public double? width { get; set; }
        }

        /// <summary>A surface-area record, as far as bounds care.</summary>
        private sealed class AreaRow
        {
            public double[][] nodes { get; set; }
        }

        /// <summary>A clip-volume record, as far as bounds care.</summary>
        private sealed class ClipRow
        {
            public double? bottom { get; set; }
            public double? top { get; set; }
            public double[][] left { get; set; }
            public double[][] right { get; set; }
        }

        /// <summary>A lane-graph record, as far as bounds care: its tessellated path.</summary>
        private sealed class LaneRow
        {
            public double[][] path { get; set; }
        }

        /// <summary>
        /// A lane-geometry record, as far as bounds care: its four bézier control points. A cubic
        /// bézier lies inside its control points' convex hull, so encapsulating them encloses the
        /// drawn curve.
        /// </summary>
        private sealed class LaneGeometryRow
        {
            public double[][] curve { get; set; }
        }
    }
}
