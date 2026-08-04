using Carto.Domain;
using Colossal.Logging;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
// Carto.IO declares its own `System` enum, so the namespace is shadowed inside this namespace.
// （Carto.IO 內宣告了同名的 `System` 枚舉，因此該命名空間在此被遮蔽。）
using System.Text;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// Writes a <see cref="MeshAsset"/> as a glTF 2.0 binary (<c>.glb</c>) file, per the CS2 World
    /// Bundle contract in <c>docs/bundle-schema.md</c>.<br/>
    /// （依 <c>docs/bundle-schema.md</c> 的 CS2 World Bundle 契約，將 <see cref="MeshAsset"/> 寫為 glTF 2.0 二進位檔。）<br/><br/>
    /// This is a deliberately small, dependency-free writer: it emits exactly the subset the
    /// bundle needs (one mesh, one primitive per material range, external texture references) and
    /// nothing else.<br/>
    /// （這是一個刻意精簡、無外部相依的寫出器，只輸出束所需的子集。）
    /// </summary>
    public static class GltfWriter
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        // glTF / OpenGL constants.（glTF 與 OpenGL 常數。）
        private const int ComponentTypeFloat = 5126;
        private const int ComponentTypeUnsignedInt = 5125;
        private const int TargetArrayBuffer = 34962;
        private const int TargetElementArrayBuffer = 34963;
        private const int ModeTriangles = 4;

        /// <summary>
        /// Write a mesh to <paramref name="path"/> as a <c>.glb</c>.
        /// （將網格以 <c>.glb</c> 格式寫至 <paramref name="path"/>。）
        /// </summary>
        /// <param name="path">The destination file path.（目標檔案路徑。）</param>
        /// <param name="mesh">The mesh to write.（待寫出的網格。）</param>
        /// <returns>The number of bytes written.（寫出的位元組數。）</returns>
        public static long Write(string path, MeshAsset mesh)
        {
            if (mesh == null) throw new ArgumentNullException(nameof(mesh));
            if (mesh.Vertices == null || mesh.Vertices.Length == 0)
                throw new ArgumentException($"Mesh `{mesh.AssetId}` has no vertices. 網格 `{mesh.AssetId}` 沒有頂點。");
            if (mesh.Indices == null || mesh.Indices.Length == 0)
                throw new ArgumentException($"Mesh `{mesh.AssetId}` has no indices. 網格 `{mesh.AssetId}` 沒有索引。");

            byte[] glb = Build(mesh);

            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);
            File.WriteAllBytes(path, glb);
            return glb.LongLength;
        }

        /// <summary>
        /// Build the in-memory <c>.glb</c> byte payload.
        /// （建構記憶體中的 <c>.glb</c> 位元組內容。）
        /// </summary>
        /// <param name="mesh">The mesh to serialise.（待序列化的網格。）</param>
        /// <returns>The complete GLB container.（完整的 GLB 容器。）</returns>
        public static byte[] Build(MeshAsset mesh)
        {
            int vertexCount = mesh.Vertices.Length;
            bool hasNormals = (mesh.Normals != null) && (mesh.Normals.Length == vertexCount);
            bool hasUvs = (mesh.Uvs != null) && (mesh.Uvs.Length == vertexCount);
            bool hasTangents = (mesh.Tangents != null) && (mesh.Tangents.Length == vertexCount);

            List<MeshPrimitive> primitives = NormalizePrimitives(mesh);

            using MemoryStream binStream = new();
            using BinaryWriter bin = new(binStream);

            List<object> bufferViews = new();
            List<object> accessors = new();

            // -- POSITION --------------------------------------------------------------------
            // The bundle's mesh frame is right-handed: mirror X. Track the bounds *after* the
            // mirror, because glTF accessor min/max describe the data as stored.
            // （束的網格座標系為右手系，因此鏡射 X 軸。邊界須於鏡射後計算。）
            float3 min = new(float.MaxValue), max = new(float.MinValue);
            int positionOffset = (int)binStream.Position;
            for (int i = 0; i < vertexCount; i++)
            {
                float3 v = mesh.Vertices[i];
                v.x = -v.x;
                bin.Write(v.x); bin.Write(v.y); bin.Write(v.z);
                min = math.min(min, v);
                max = math.max(max, v);
            }
            int positionView = AddBufferView(bufferViews, positionOffset, vertexCount * 12, TargetArrayBuffer);
            int positionAccessor = accessors.Count;
            accessors.Add(new Dictionary<string, object>
            {
                ["bufferView"] = positionView,
                ["componentType"] = ComponentTypeFloat,
                ["count"] = vertexCount,
                ["type"] = "VEC3",
                ["min"] = new[] { min.x, min.y, min.z },
                ["max"] = new[] { max.x, max.y, max.z }
            });

            // -- NORMAL ----------------------------------------------------------------------
            int normalAccessor = -1;
            if (hasNormals)
            {
                int offset = (int)binStream.Position;
                for (int i = 0; i < vertexCount; i++)
                {
                    float3 n = mesh.Normals[i];
                    bin.Write(-n.x); bin.Write(n.y); bin.Write(n.z);
                }
                normalAccessor = AddAccessor(accessors, AddBufferView(bufferViews, offset, vertexCount * 12, TargetArrayBuffer),
                                             ComponentTypeFloat, vertexCount, "VEC3");
            }

            // -- TEXCOORD_0 ------------------------------------------------------------------
            int uvAccessor = -1;
            if (hasUvs)
            {
                int offset = (int)binStream.Position;
                for (int i = 0; i < vertexCount; i++)
                {
                    float2 uv = mesh.Uvs[i];
                    // Unity's UV origin is bottom-left, glTF's is top-left.
                    // （Unity 的 UV 原點在左下，glTF 在左上。）
                    bin.Write(uv.x); bin.Write(1f - uv.y);
                }
                uvAccessor = AddAccessor(accessors, AddBufferView(bufferViews, offset, vertexCount * 8, TargetArrayBuffer),
                                         ComponentTypeFloat, vertexCount, "VEC2");
            }

            // -- TANGENT ---------------------------------------------------------------------
            int tangentAccessor = -1;
            if (hasTangents)
            {
                int offset = (int)binStream.Position;
                for (int i = 0; i < vertexCount; i++)
                {
                    float4 t = mesh.Tangents[i];
                    // Mirroring X flips handedness, so the bitangent sign in w flips with it.
                    // （鏡射 X 會翻轉手性，因此位於 w 的副切線符號亦須翻轉。）
                    bin.Write(-t.x); bin.Write(t.y); bin.Write(t.z); bin.Write(-t.w);
                }
                tangentAccessor = AddAccessor(accessors, AddBufferView(bufferViews, offset, vertexCount * 16, TargetArrayBuffer),
                                              ComponentTypeFloat, vertexCount, "VEC4");
            }

            // -- Indices, one accessor per primitive ------------------------------------------
            List<object> gltfPrimitives = new();
            List<object> materials = new();
            foreach (MeshPrimitive primitive in primitives)
            {
                int offset = (int)binStream.Position;
                int triangles = primitive.IndexCount / 3;
                for (int t = 0; t < triangles; t++)
                {
                    int b = primitive.StartIndex + (t * 3);
                    // Mirroring X inverts triangle facing, so reverse the winding to compensate
                    // and keep glTF front faces counter-clockwise.
                    // （鏡射 X 會反轉三角形朝向，因此反轉繞序以維持 glTF 正面為逆時針。）
                    bin.Write((uint)mesh.Indices[b]);
                    bin.Write((uint)mesh.Indices[b + 2]);
                    bin.Write((uint)mesh.Indices[b + 1]);
                }

                int indexAccessor = AddAccessor(accessors,
                    AddBufferView(bufferViews, offset, primitive.IndexCount * 4, TargetElementArrayBuffer),
                    ComponentTypeUnsignedInt, primitive.IndexCount, "SCALAR");

                Dictionary<string, object> attributes = new() { ["POSITION"] = positionAccessor };
                if (normalAccessor >= 0) attributes["NORMAL"] = normalAccessor;
                if (uvAccessor >= 0) attributes["TEXCOORD_0"] = uvAccessor;
                if (tangentAccessor >= 0) attributes["TANGENT"] = tangentAccessor;

                gltfPrimitives.Add(new Dictionary<string, object>
                {
                    ["attributes"] = attributes,
                    ["indices"] = indexAccessor,
                    ["material"] = materials.Count,
                    ["mode"] = ModeTriangles
                });

                // The material's `name` is the bundle's join key into materials.json. The PBR
                // values here are only a neutral placeholder so the mesh is viewable standalone;
                // materials.json remains authoritative.
                // （材質的 `name` 是連結 materials.json 的鍵。此處的 PBR 值僅為中性佔位。）
                materials.Add(new Dictionary<string, object>
                {
                    ["name"] = primitive.MaterialId,
                    ["pbrMetallicRoughness"] = new Dictionary<string, object>
                    {
                        ["baseColorFactor"] = new[] { 1f, 1f, 1f, 1f },
                        ["metallicFactor"] = 0f,
                        ["roughnessFactor"] = 0.9f
                    },
                    ["doubleSided"] = false
                });
            }

            bin.Flush();
            byte[] binChunk = Pad(binStream.ToArray(), 0x00);

            Dictionary<string, object> gltf = new()
            {
                ["asset"] = new Dictionary<string, object>
                {
                    ["version"] = "2.0",
                    ["generator"] = $"carto-ripper {Instance.Version}"
                },
                ["scene"] = 0,
                ["scenes"] = new object[] { new Dictionary<string, object> { ["nodes"] = new[] { 0 } } },
                // The contract names the root node after the assetId, and LOD n > 0 as
                // "<assetId>_LOD<n>".（契約規定根節點以 assetId 命名，LOD n > 0 則為 "<assetId>_LOD<n>"。）
                ["nodes"] = new object[] { new Dictionary<string, object> { ["name"] = NodeName(mesh), ["mesh"] = 0 } },
                ["meshes"] = new object[] { new Dictionary<string, object> { ["name"] = NodeName(mesh), ["primitives"] = gltfPrimitives } },
                ["materials"] = materials,
                ["buffers"] = new object[] { new Dictionary<string, object> { ["byteLength"] = binChunk.Length } },
                ["bufferViews"] = bufferViews,
                ["accessors"] = accessors
            };

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
        /// Produce the node / mesh name required by the bundle contract.
        /// （產生束契約所要求的節點與網格名稱。）
        /// </summary>
        private static string NodeName(MeshAsset mesh) => mesh.Lod > 0 ? $"{mesh.AssetId}_LOD{mesh.Lod}" : mesh.AssetId;

        /// <summary>
        /// Return the mesh's primitives, clamped to the actual index buffer and with a synthesised
        /// whole-mesh primitive if the game reported none.<br/>
        /// （回傳網格的 primitive；範圍會被夾至實際索引緩衝區，若遊戲未回報任何範圍則合成一個涵蓋整個網格的 primitive。）<br/><br/>
        /// Out-of-range ranges are dropped rather than trusted: a bad range would otherwise write a
        /// <c>.glb</c> that reads past its own buffer, which viewers report as corruption far from
        /// the real cause.<br/>
        /// （超出範圍者會被丟棄而非採信，否則將寫出讀取越界的 <c>.glb</c>。）
        /// </summary>
        private static List<MeshPrimitive> NormalizePrimitives(MeshAsset mesh)
        {
            List<MeshPrimitive> result = new();
            int indexCount = mesh.Indices.Length;

            foreach (MeshPrimitive primitive in mesh.Primitives)
            {
                if ((primitive.IndexCount <= 0) || (primitive.IndexCount % 3 != 0)) continue;
                if (primitive.StartIndex < 0) continue;
                if (primitive.StartIndex + primitive.IndexCount > indexCount)
                {
                    _log.Warn($"Mesh `{mesh.AssetId}`: dropping primitive range [{primitive.StartIndex}, {primitive.StartIndex + primitive.IndexCount}) which exceeds the {indexCount}-index buffer. 網格 `{mesh.AssetId}` 的 primitive 範圍超出索引緩衝區，已丟棄。");
                    continue;
                }
                result.Add(primitive);
            }

            if (result.Count == 0)
            {
                result.Add(new MeshPrimitive
                {
                    StartIndex = 0,
                    IndexCount = indexCount - (indexCount % 3),
                    StartVertex = 0,
                    VertexCount = mesh.Vertices.Length,
                    MaterialIndex = 0,
                    MaterialId = $"{mesh.AssetId}_mat0"
                });
            }

            return result;
        }

        /// <summary>
        /// Append a buffer view and return its index.（附加一個 buffer view 並回傳其索引。）
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
        /// Append an accessor and return its index.（附加一個 accessor 並回傳其索引。）
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
        /// （將區塊填補至 GLB 容器所要求的 4 位元組邊界。）
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
