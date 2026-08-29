using Carto.Domain;
using Colossal.Logging;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
// Carto.IO declares its own `System` enum, so the namespace is shadowed inside this namespace.
//
using System.Text;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Writes a <see cref="MeshAsset"/> as a glTF 2.0 binary (<c>.glb</c>) file, per the CS2 World
    /// Bundle contract in <c>docs/bundle-schema.md</c>.
    /// This is a deliberately small, dependency-free writer: it emits exactly the subset the
    /// bundle needs (one mesh, one primitive per material range, external texture references) and
    /// nothing else.
    /// </summary>
    public static partial class GltfWriter
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        // glTF / OpenGL constants.
        private const int ComponentTypeFloat = 5126;
        private const int ComponentTypeUnsignedInt = 5125;
        private const int TargetArrayBuffer = 34962;
        private const int TargetElementArrayBuffer = 34963;
        private const int ModeTriangles = 4;

        /// <summary>
        /// Write a single mesh to <paramref name="path"/> as a <c>.glb</c>.
        /// </summary>
        /// <param name="path">The destination file path.</param>
        /// <param name="mesh">The mesh to write.</param>
        /// <param name="describe">Resolves a material id to its maps and factors, or <c>null</c> to write neutral placeholders.</param>
        /// <returns>The number of bytes written.</returns>
        public static long Write(string path, MeshAsset mesh, Func<string, MaterialDescription> describe = null)
        {
            if (mesh == null) throw new ArgumentNullException(nameof(mesh));
            return Write(path, Wrap(mesh), describe);
        }

        /// <summary>
        /// Write an assembled prefab to <paramref name="path"/> as a <c>.glb</c>.
        /// </summary>
        /// <param name="path">The destination file path.</param>
        /// <param name="prefab">The prefab to write.</param>
        /// <param name="describe">Resolves a material id to its maps and factors, or <c>null</c> to write neutral placeholders.</param>
        /// <returns>The number of bytes written to disk.</returns>
        public static long Write(string path, PrefabMesh prefab, Func<string, MaterialDescription> describe = null)
        {
            // Through BundleFile rather than File.WriteAllBytes so that `meshes/` and `networks/`
            // keep sharing one on-disk policy with the JSON Lines layers — see BundleFile on why
            // that policy is currently "uncompressed".
            //
            return BundleFile.WriteAllBytes(path, Build(prefab, describe));
        }

        /// <summary>
        /// Build the in-memory <c>.glb</c> byte payload for a single mesh.
        /// </summary>
        /// <param name="mesh">The mesh to serialise.</param>
        /// <returns>The complete GLB container.</returns>
        public static byte[] Build(MeshAsset mesh) => Build(Wrap(mesh));

        /// <summary>
        /// Present a lone mesh as a one-part prefab, so both entry points share one writer.
        /// </summary>
        /// <param name="mesh">The mesh to wrap.</param>
        /// <returns>The one-part prefab.</returns>
        private static PrefabMesh Wrap(MeshAsset mesh) => new()
        {
            // The LOD suffix belongs on the root node, and for a lone mesh the root *is* the mesh.
            //
            AssetId = NodeName(mesh),
            SourceName = mesh.SourceName,
            Parts = { new PrefabMeshPart { Mesh = mesh, NodeName = NodeName(mesh) } }
        };

        /// <summary>
        /// Build the in-memory <c>.glb</c> byte payload for an assembled prefab.
        /// Every part shares one BIN chunk and becomes one glTF mesh hung off its own node under
        /// the root, carrying the part's offset within the prefab as the node's TRS. A prefab whose
        /// single part sits at the origin collapses to a bare root node with the mesh on it, since
        /// most prefabs — every tree and prop — are exactly that.
        /// </summary>
        /// <param name="prefab">The prefab to serialise.</param>
        /// <param name="describe">Resolves a material id to its maps and factors.</param>
        /// <returns>The complete GLB container.</returns>
        public static byte[] Build(PrefabMesh prefab, Func<string, MaterialDescription> describe = null)
        {
            if (prefab == null) throw new ArgumentNullException(nameof(prefab));
            if ((prefab.Parts == null) || (prefab.Parts.Count == 0))
                throw new ArgumentException($"Prefab `{prefab.AssetId}` has no parts.");

            using MemoryStream binStream = new();
            using BinaryWriter bin = new(binStream);

            List<object> bufferViews = new();
            List<object> accessors = new();
            List<object> materials = new();
            List<object> meshes = new();
            List<object> nodes = new();

            // Images are shared across every material in the file — a prefab's parts routinely
            // share an atlas — so they are pooled by URI rather than emitted per material.
            //
            TextureTable textures = new(describe);

            // Materials pool per file, not per part: several parts of one prefab routinely share a
            // surface, and §6 keys the join on the material's name — one entry per name keeps the
            // file honest about how many materials it actually has.
            //
            Dictionary<string, int> materialPool = new();

            // Reserve node 0 for the root; parts append after it and are listed as its children.
            //
            List<int> children = new();
            nodes.Add(null);

            foreach (PrefabMeshPart part in prefab.Parts)
            {
                MeshAsset mesh = part.Mesh;
                if (mesh == null) continue;
                if ((mesh.Vertices == null) || (mesh.Vertices.Length == 0))
                    throw new ArgumentException($"Mesh `{mesh.AssetId}` has no vertices.");
                if ((mesh.Indices == null) || (mesh.Indices.Length == 0))
                    throw new ArgumentException($"Mesh `{mesh.AssetId}` has no indices.");

                string name = part.NodeName ?? NodeName(mesh);
                meshes.Add(new Dictionary<string, object>
                {
                    ["name"] = name,
                    ["primitives"] = BuildPrimitives(mesh, bin, binStream, bufferViews, accessors, materials, materialPool, textures)
                });

                Dictionary<string, object> node = new()
                {
                    ["name"] = name,
                    ["mesh"] = meshes.Count - 1
                };

                // The bundle's mesh frame mirrors X, so a part's offset mirrors with it. A rotation
                // survives a reflection as a rotation whose axis is the mirrored pseudo-vector —
                // (x, -y, -z, w) — which is why y and z flip here while x does not.
                //
                float3 translation = part.Position;
                if (math.any(translation != float3.zero))
                    node["translation"] = new[] { -translation.x, translation.y, translation.z };

                float4 rotation = part.Rotation.value;
                if (math.lengthsq(rotation) > 1e-6f)
                {
                    rotation = math.normalize(rotation);
                    if (!rotation.Equals(new float4(0f, 0f, 0f, 1f)))
                        node["rotation"] = new[] { rotation.x, -rotation.y, -rotation.z, rotation.w };
                }

                // A per-axis magnitude, indifferent to the mirror — the reflection is carried by
                // the position/rotation/winding changes, not by a negative scale.
                //
                if (part.Scale.HasValue && math.any(part.Scale.Value != new float3(1f)))
                    node["scale"] = new[] { part.Scale.Value.x, part.Scale.Value.y, part.Scale.Value.z };

                nodes.Add(node);
                children.Add(nodes.Count - 1);
            }

            if (children.Count == 0)
                throw new ArgumentException($"Prefab `{prefab.AssetId}` has no readable parts.");

            // The contract names the root node exactly `<assetId>`.
            //
            Dictionary<string, object> root = new() { ["name"] = prefab.AssetId };
            Dictionary<string, object> only = (children.Count == 1) ? (Dictionary<string, object>)nodes[children[0]] : null;
            if ((only != null) && !only.ContainsKey("translation") && !only.ContainsKey("rotation") && !only.ContainsKey("scale"))
            {
                // A single part at the origin needs no intermediate node — put the mesh straight on
                // the root, which the contract requires to be named exactly `<assetId>`.
                //
                root["mesh"] = only["mesh"];
                nodes.RemoveAt(children[0]);
            }
            else
            {
                root["children"] = children;
            }

            nodes[0] = root;

            bin.Flush();
            byte[] binChunk = Pad(binStream.ToArray(), 0x00);

            Dictionary<string, object> gltf = new()
            {
                ["asset"] = new Dictionary<string, object>
                {
                    ["version"] = "2.0",
                    ["generator"] = $"csii-world-bundle {Instance.Version}"
                },
                ["scene"] = 0,
                ["scenes"] = new object[] { new Dictionary<string, object> { ["nodes"] = new[] { 0 } } },
                ["nodes"] = nodes,
                ["meshes"] = meshes,
                ["materials"] = materials,
                ["buffers"] = new object[] { new Dictionary<string, object> { ["byteLength"] = binChunk.Length } },
                ["bufferViews"] = bufferViews,
                ["accessors"] = accessors
            };

            // glTF forbids empty arrays for these, so a prefab whose materials carried no maps
            // must omit them entirely rather than write `"images": []`.
            //
            if (textures.Images.Count > 0)
            {
                gltf["images"] = textures.Images;
                gltf["textures"] = textures.Textures;
                gltf["samplers"] = textures.Samplers;
            }

            byte[] jsonChunk = Pad(Encoding.UTF8.GetBytes(
                JsonConvert.SerializeObject(gltf, Formatting.None)), 0x20);

            using MemoryStream glbStream = new();
            using BinaryWriter glb = new(glbStream);
            glb.Write(Encoding.ASCII.GetBytes("glTF"));
            glb.Write(2u);
            glb.Write((uint)(12 + 8 + jsonChunk.Length + 8 + binChunk.Length));
            glb.Write((uint)jsonChunk.Length);
            glb.Write(Encoding.ASCII.GetBytes("JSON"));
            glb.Write(jsonChunk);
            glb.Write((uint)binChunk.Length);
            glb.Write(new byte[] { 0x42, 0x49, 0x4E, 0x00 });
            glb.Write(binChunk);
            glb.Flush();

            return glbStream.ToArray();
        }


        /// <summary>
        /// Append a buffer view and return its index.
        /// </summary>
        private static int AddBufferView(List<object> bufferViews, int byteOffset, int byteLength, int target)
        {
            bufferViews.Add(new Dictionary<string, object>
            {
                ["buffer"] = 0,
                ["byteOffset"] = byteOffset,
                ["byteLength"] = byteLength,
                ["target"] = target
            });
            return bufferViews.Count - 1;
        }

        /// <summary>
        /// Append an accessor and return its index.
        /// </summary>
        private static int AddAccessor(List<object> accessors, int bufferView, int componentType, int count, string type)
        {
            accessors.Add(new Dictionary<string, object>
            {
                ["bufferView"] = bufferView,
                ["componentType"] = componentType,
                ["count"] = count,
                ["type"] = type
            });
            return accessors.Count - 1;
        }

        /// <summary>
        /// Pad a chunk to the 4-byte boundary the GLB container requires.
        /// </summary>
        private static byte[] Pad(byte[] data, byte filler)
        {
            int remainder = data.Length % 4;
            if (remainder == 0) return data;

            byte[] padded = new byte[data.Length + (4 - remainder)];
            Array.Copy(data, padded, data.Length);
            for (int i = data.Length; i < padded.Length; i++) padded[i] = filler;
            return padded;
        }
    }
}
