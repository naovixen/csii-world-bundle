using System.Threading.Tasks;
using System.Threading;
using System.Collections.Concurrent;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Carto.IO
{
    /// <summary>
    /// The world bounds of a bundle, taken from each mesh's own glTF accessor min/max rather
    /// than from its vertices.
    /// </summary>
    public static partial class BundleWriter
    {
        /// <summary>
        /// Read the extents of every <c>.glb</c> in a directory, keyed by assetId (the file stem),
        /// in the glb frame.
        /// Only the JSON chunk is read — the accessor <c>min</c>/<c>max</c> the producer writes on
        /// every POSITION accessor, carried through each node's translation/rotation/scale. The
        /// binary chunk is never touched, which is what keeps twelve thousand files cheap.
        /// </summary>
        private static Dictionary<string, Aabb> ReadMeshBounds(string directory, StringBuilder report, ref int scanned, int total)
        {
            if (!Directory.Exists(directory)) return new Dictionary<string, Aabb>();

            // Twelve thousand files, each a JSON chunk to parse, and none depending on another:
            // across the cores rather than one after the other. The manifest stage was 145 s of
            // the 2026-08-29 rip and this scan the largest part of it (refactor Phase 10).
            //
            ConcurrentDictionary<string, Aabb> result = new();
            int unreadable = 0;
            int progress = scanned;
            Parallel.ForEach(Directory.GetFiles(directory, "*.glb"), path =>
            {
                // Any thread may report — this runs on the manifest worker (RipProgress contract).
                //
                RipProgress.Report(RipProgress.Phase.Manifest, Interlocked.Increment(ref progress), total);

                try
                {
                    Aabb glbBounds = ReadGlbBounds(path);
                    if (glbBounds.Any) result[Path.GetFileNameWithoutExtension(path)] = glbBounds;
                }
                catch (Exception ex)
                {
                    if (Interlocked.Increment(ref unreadable) == 1) Instance.Log.Warn($"Unreadable glb {path}: {ex.Message}");
                }
            });
            scanned = progress;
            if (unreadable > 0) report.AppendLine($"{unreadable} glb file(s) in {Path.GetFileName(directory)}/ were unreadable; their instances contribute position only.");

            return new Dictionary<string, Aabb>(result);
        }

        /// <summary>
        /// The whole-file extents of one glb, from its JSON chunk.
        /// </summary>
        private static Aabb ReadGlbBounds(string path)
        {
            JObject gltf = JObject.Parse(ReadGlbJsonChunk(path));

            // Per-mesh extents: the union of each primitive's POSITION accessor min/max.
            //
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
            //
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
        /// that child's TRS.
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
        /// </summary>
        private static Aabb TransformNode(in Aabb box, JToken node)
        {
            double[] translation = ToDoubles(node["translation"] as JArray, 3) ?? new double[] { 0, 0, 0 };
            double[] rotation = ToDoubles(node["rotation"] as JArray, 4) ?? new double[] { 0, 0, 0, 1 };
            double[] scale = ToDoubles(node["scale"] as JArray, 3);

            Aabb result = Aabb.Empty;

            // Reuse the placement transform: it is the same rotated-box carry.
            // The mirror inside EncapsulatePlacedMesh must not apply here, so pre-mirror to cancel it.
            //
            Aabb premirrored = box;
            premirrored.MinX = -box.MaxX;
            premirrored.MaxX = -box.MinX;
            EncapsulatePlacedMesh(ref result, premirrored, translation, rotation, scale);
            return result;
        }

        /// <summary>Read a fixed-length number array, or <c>null</c>.</summary>
        private static double[] ToDoubles(JArray array, int length)
        {
            if ((array == null) || (array.Count != length)) return null;

            double[] result = new double[length];
            for (int i = 0; i < length; i++) result[i] = array[i].Value<double>();
            return result;
        }
    }
}
