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
        /// Write a single mesh to <paramref name="path"/> as a <c>.glb</c>.
        /// （將單一網格以 <c>.glb</c> 格式寫至 <paramref name="path"/>。）
        /// </summary>
        /// <param name="path">The destination file path.（目標檔案路徑。）</param>
        /// <param name="mesh">The mesh to write.（待寫出的網格。）</param>
        /// <returns>The number of bytes written.（寫出的位元組數。）</returns>
        public static long Write(string path, MeshAsset mesh)
        {
            if (mesh == null) throw new ArgumentNullException(nameof(mesh));
            return Write(path, Wrap(mesh));
        }

        /// <summary>
        /// Write an assembled prefab to <paramref name="path"/> as a <c>.glb</c>.
        /// （將已組裝的預製模板以 <c>.glb</c> 格式寫至 <paramref name="path"/>。）
        /// </summary>
        /// <param name="path">The destination file path.（目標檔案路徑。）</param>
        /// <param name="prefab">The prefab to write.（待寫出的預製模板。）</param>
        /// <param name="describe">Resolves a material id to its maps and factors, or <c>null</c> to write neutral placeholders.（將材質 ID 解析為其貼圖與係數，若為 <c>null</c> 則寫出中性佔位值。）</param>
        /// <returns>The number of bytes written.（寫出的位元組數。）</returns>
        public static long Write(string path, PrefabMesh prefab, Func<string, MaterialDescription> describe = null)
        {
            byte[] glb = Build(prefab, describe);

            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory)) Directory.CreateDirectory(directory);
            File.WriteAllBytes(path, glb);
            return glb.LongLength;
        }

        /// <summary>
        /// Build the in-memory <c>.glb</c> byte payload for a single mesh.
        /// （為單一網格建構記憶體中的 <c>.glb</c> 位元組內容。）
        /// </summary>
        /// <param name="mesh">The mesh to serialise.（待序列化的網格。）</param>
        /// <returns>The complete GLB container.（完整的 GLB 容器。）</returns>
        public static byte[] Build(MeshAsset mesh) => Build(Wrap(mesh));

        /// <summary>
        /// Present a lone mesh as a one-part prefab, so both entry points share one writer.
        /// （將單一網格視為只有一個部件的預製模板，使兩個進入點共用同一個寫出器。）
        /// </summary>
        /// <param name="mesh">The mesh to wrap.（待包裝的網格。）</param>
        /// <returns>The one-part prefab.（只有一個部件的預製模板。）</returns>
        private static PrefabMesh Wrap(MeshAsset mesh) => new()
        {
            // The LOD suffix belongs on the root node, and for a lone mesh the root *is* the mesh.
            // （LOD 後綴屬於根節點，而單一網格的根節點即為該網格本身。）
            AssetId = NodeName(mesh),
            SourceName = mesh.SourceName,
            Parts = { new PrefabMeshPart { Mesh = mesh, NodeName = NodeName(mesh) } }
        };

        /// <summary>
        /// Build the in-memory <c>.glb</c> byte payload for an assembled prefab.
        /// （為已組裝的預製模板建構記憶體中的 <c>.glb</c> 位元組內容。）<br/><br/>
        /// Every part shares one BIN chunk and becomes one glTF mesh hung off its own node under
        /// the root, carrying the part's offset within the prefab as the node's TRS. A prefab whose
        /// single part sits at the origin collapses to a bare root node with the mesh on it, since
        /// most prefabs — every tree and prop — are exactly that.<br/>
        /// （所有部件共用單一 BIN 區塊，各自成為一個 glTF 網格，並掛在根節點之下的專屬節點上，
        /// 以節點的 TRS 帶有該部件在預製模板內的偏移。若預製模板僅有一個位於原點的部件，
        /// 則收攏為僅有根節點並直接掛上網格——大多數預製模板（所有樹木與道具）正是如此。）
        /// </summary>
        /// <param name="prefab">The prefab to serialise.（待序列化的預製模板。）</param>
        /// <param name="describe">Resolves a material id to its maps and factors.（將材質 ID 解析為其貼圖與係數。）</param>
        /// <returns>The complete GLB container.（完整的 GLB 容器。）</returns>
        public static byte[] Build(PrefabMesh prefab, Func<string, MaterialDescription> describe = null)
        {
            if (prefab == null) throw new ArgumentNullException(nameof(prefab));
            if ((prefab.Parts == null) || (prefab.Parts.Count == 0))
                throw new ArgumentException($"Prefab `{prefab.AssetId}` has no parts. 預製模板 `{prefab.AssetId}` 沒有任何部件。");

            using MemoryStream binStream = new();
            using BinaryWriter bin = new(binStream);

            List<object> bufferViews = new();
            List<object> accessors = new();
            List<object> materials = new();
            List<object> meshes = new();
            List<object> nodes = new();

            // Images are shared across every material in the file — a prefab's parts routinely
            // share an atlas — so they are pooled by URI rather than emitted per material.
            // （影像於檔案內的所有材質間共用（預製模板的各部件經常共用同一圖集），
            //   故以 URI 匯集，而非逐材質輸出。）
            TextureTable textures = new(describe);

            // Reserve node 0 for the root; parts append after it and are listed as its children.
            // （保留節點 0 給根節點，部件接續其後並列為其子節點。）
            List<int> children = new();
            nodes.Add(null);

            foreach (PrefabMeshPart part in prefab.Parts)
            {
                MeshAsset mesh = part.Mesh;
                if (mesh == null) continue;
                if ((mesh.Vertices == null) || (mesh.Vertices.Length == 0))
                    throw new ArgumentException($"Mesh `{mesh.AssetId}` has no vertices. 網格 `{mesh.AssetId}` 沒有頂點。");
                if ((mesh.Indices == null) || (mesh.Indices.Length == 0))
                    throw new ArgumentException($"Mesh `{mesh.AssetId}` has no indices. 網格 `{mesh.AssetId}` 沒有索引。");

                string name = part.NodeName ?? NodeName(mesh);
                meshes.Add(new Dictionary<string, object>
                {
                    ["name"] = name,
                    ["primitives"] = BuildPrimitives(mesh, bin, binStream, bufferViews, accessors, materials, textures)
                });

                Dictionary<string, object> node = new()
                {
                    ["name"] = name,
                    ["mesh"] = meshes.Count - 1
                };

                // The bundle's mesh frame mirrors X, so a part's offset mirrors with it. A rotation
                // survives a reflection as a rotation whose axis is the mirrored pseudo-vector —
                // (x, -y, -z, w) — which is why y and z flip here while x does not.
                // （束的網格座標系鏡射 X 軸，故部件偏移亦隨之鏡射。旋轉經鏡射後仍為旋轉，
                //   其軸為鏡射後的偽向量 (x, -y, -z, w)，因此此處翻轉 y 與 z 而非 x。）
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

                nodes.Add(node);
                children.Add(nodes.Count - 1);
            }

            if (children.Count == 0)
                throw new ArgumentException($"Prefab `{prefab.AssetId}` has no readable parts. 預製模板 `{prefab.AssetId}` 沒有可讀取的部件。");

            // The contract names the root node exactly `<assetId>`.
            // （契約規定根節點的名稱恰為 `<assetId>`。）
            Dictionary<string, object> root = new() { ["name"] = prefab.AssetId };
            Dictionary<string, object> only = (children.Count == 1) ? (Dictionary<string, object>)nodes[children[0]] : null;
            if ((only != null) && !only.ContainsKey("translation") && !only.ContainsKey("rotation"))
            {
                // A single part at the origin needs no intermediate node — put the mesh straight on
                // the root, which the contract requires to be named exactly `<assetId>`.
                // （位於原點的單一部件無須中間節點：將網格直接掛於根節點，
                //   契約要求其名稱恰為 `<assetId>`。）
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
                    ["generator"] = $"carto-ripper {Instance.Version}"
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
            // （glTF 不允許這些為空陣列，因此材質不含任何貼圖的預製模板必須完全省略，
            //   而非寫出 `"images": []`。）
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
        /// Append one mesh's vertex attributes and index ranges to the shared buffer, returning its
        /// glTF primitives.<br/>
        /// （將單一網格的頂點屬性與索引範圍附加至共用緩衝區，並回傳其 glTF primitive。）
        /// </summary>
        /// <param name="mesh">The mesh to append.（待附加的網格。）</param>
        /// <param name="bin">The writer over the shared BIN chunk.（共用 BIN 區塊的寫出器。）</param>
        /// <param name="binStream">The shared BIN chunk, for its current offset.（共用 BIN 區塊，用以取得目前位移。）</param>
        /// <param name="bufferViews">The document's buffer views.（文件的 buffer view 集合。）</param>
        /// <param name="accessors">The document's accessors.（文件的 accessor 集合。）</param>
        /// <param name="materials">The document's materials.（文件的材質集合。）</param>
        /// <param name="textures">The document's shared image pool.（文件的共用影像池。）</param>
        /// <returns>The primitives for this mesh.（此網格的 primitive。）</returns>
        private static List<object> BuildPrimitives(MeshAsset mesh, BinaryWriter bin, MemoryStream binStream,
                                                    List<object> bufferViews, List<object> accessors,
                                                    List<object> materials, TextureTable textures)
        {
            int vertexCount = mesh.Vertices.Length;
            bool hasNormals = (mesh.Normals != null) && (mesh.Normals.Length == vertexCount);
            bool hasUvs = (mesh.Uvs != null) && (mesh.Uvs.Length == vertexCount);
            bool hasTangents = (mesh.Tangents != null) && (mesh.Tangents.Length == vertexCount);

            List<MeshPrimitive> primitives = NormalizePrimitives(mesh);

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

            // -- Indices, one accessor per material -------------------------------------------
            //
            // Grouped by material, not emitted one per source range, because contract §6 is "one
            // primitive per material" and a source mesh routinely presents the same material
            // several times over — a road composition contributes one range per cross-section
            // piece, and a dozen of those pieces share one asphalt. Ungrouped, each range became
            // its own glTF material: measured over 60 network meshes, 651 materials carrying only
            // 60 distinct names. That is what a consumer pays for, too — importing those 60 files
            // into Blender built 651 PBR node trees instead of 60, and material creation was 51%
            // of the import's entire run time.
            // （依材質分組，而非逐來源範圍輸出，因為契約 §6 規定「每種材質一個 primitive」，
            //   而來源網格經常重複呈現同一材質——道路組成的每個橫斷面部件各貢獻一個範圍，
            //   其中十餘個部件共用同一種瀝青。若不分組，每個範圍都會成為獨立的 glTF 材質：
            //   實測 60 個道路網格產生 651 種材質，卻僅有 60 個相異名稱。
            //   消費端同樣為此付出代價——將該 60 個檔案匯入 Blender 會建立 651 棵 PBR 節點樹
            //   而非 60 棵，且材質建立占整個匯入執行時間的 51%。）
            List<string> materialOrder = new();
            Dictionary<string, List<MeshPrimitive>> byMaterial = new();
            foreach (MeshPrimitive primitive in primitives)
            {
                string materialId = string.IsNullOrEmpty(primitive.MaterialId)
                    ? $"{mesh.AssetId}_mat{primitive.MaterialIndex}"
                    : primitive.MaterialId;

                if (!byMaterial.TryGetValue(materialId, out List<MeshPrimitive> group))
                {
                    group = new List<MeshPrimitive>();
                    byMaterial[materialId] = group;
                    materialOrder.Add(materialId);
                }

                group.Add(primitive);
            }

            List<object> gltfPrimitives = new();
            foreach (string materialId in materialOrder)
            {
                int offset = (int)binStream.Position;
                int indexCount = 0;

                foreach (MeshPrimitive primitive in byMaterial[materialId])
                {
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

                    indexCount += triangles * 3;
                }

                if (indexCount == 0) continue;

                int indexAccessor = AddAccessor(accessors,
                    AddBufferView(bufferViews, offset, indexCount * 4, TargetElementArrayBuffer),
                    ComponentTypeUnsignedInt, indexCount, "SCALAR");

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

                materials.Add(textures.BuildMaterial(materialId));
            }

            return gltfPrimitives;
        }

        /// <summary>
        /// The glTF materials of one file, and the image pool they draw on.
        /// （單一檔案的 glTF 材質，以及其所取用的影像池。）<br/><br/>
        /// The contract (§6) references textures rather than embedding them: an image's <c>uri</c>
        /// is a relative path into the bundle's texture directory, so a shared atlas is stored once
        /// for the whole bundle instead of once per mesh that uses it. That is also what makes each
        /// <c>.glb</c> open textured on its own — which is the Phase 4 acceptance test.<br/>
        /// （契約 §6 規定貼圖採參照而非內嵌：影像的 <c>uri</c> 是指向束貼圖目錄的相對路徑，
        /// 使共用的圖集於整個束中只存放一次，而非每個使用它的網格各存一份。
        /// 這也是每個 <c>.glb</c> 能各自帶貼圖開啟的原因，而該點正是第四階段的驗收測試。）
        /// </summary>
        private class TextureTable
        {
            /// <summary>
            /// Resolves a material id, or <c>null</c> when no table is available.
            /// （解析材質 ID，若無材質表可用則為 <c>null</c>。）
            /// </summary>
            private readonly Func<string, MaterialDescription> _describe;

            /// <summary>
            /// Image index by URI, so an atlas shared by several materials is listed once.
            /// （以 URI 為鍵的影像索引，使多個材質共用的圖集只列出一次。）
            /// </summary>
            private readonly Dictionary<string, int> _byUri = new();

            /// <summary>The document's images, textures and samplers.（文件的影像、貼圖與取樣器集合。）</summary>
            public List<object> Images { get; } = new();

            /// <inheritdoc cref="Images"/>
            public List<object> Textures { get; } = new();

            /// <inheritdoc cref="Images"/>
            public List<object> Samplers { get; } = new();

            /// <summary>
            /// Build a table over a material resolver.（依材質解析器建立表。）
            /// </summary>
            /// <param name="describe">The resolver, or <c>null</c>.（解析器，或 <c>null</c>。）</param>
            public TextureTable(Func<string, MaterialDescription> describe) => _describe = describe;

            /// <summary>
            /// Build the glTF material for one bundle material id.
            /// （為單一束材質 ID 建構 glTF 材質。）<br/><br/>
            /// The <c>name</c> is the bundle's join key into <c>materials.json</c> and is always
            /// written. Everything else is a best-effort approximation for standalone viewing:
            /// where CS2's model and glTF's disagree, <c>materials.json</c> stays authoritative
            /// and the disagreeing term is simply left out rather than mistranslated.<br/>
            /// （<c>name</c> 是連結 <c>materials.json</c> 的鍵，恆會寫出。其餘皆為供獨立檢視的
            /// 盡力近似：凡 CS2 的模型與 glTF 不一致者，一律以 <c>materials.json</c> 為準，
            /// 並直接略去該項，而非做出錯誤的轉譯。）
            /// </summary>
            /// <param name="materialId">The bundle material id.（束材質 ID。）</param>
            /// <returns>The glTF material.（glTF 材質。）</returns>
            public Dictionary<string, object> BuildMaterial(string materialId)
            {
                Dictionary<string, object> pbr = new();
                Dictionary<string, object> material = new()
                {
                    ["name"] = materialId,
                    ["pbrMetallicRoughness"] = pbr
                };

                MaterialDescription description = (_describe == null) ? default : _describe(materialId);
                if (!description.Found)
                {
                    // No material table — the Phase 1 spike path — so write something neutral and
                    // let the name carry the join on its own.
                    // （沒有材質表（第一階段驗證路徑），故寫出中性值，
                    //   由名稱獨自承載連結關係。）
                    pbr["baseColorFactor"] = new[] { 1f, 1f, 1f, 1f };
                    pbr["metallicFactor"] = 0f;
                    pbr["roughnessFactor"] = 0.9f;
                    material["doubleSided"] = false;
                    return material;
                }

                float4 tint = description.Tint;
                pbr["baseColorFactor"] = new[] { tint.x, tint.y, tint.z, tint.w };

                // CS2's smoothness/AO/metallic packing does not line up with glTF's
                // metallic-roughness texture, so no mask map is wired here — see MaterialDescription.
                // A neutral non-metal is the honest stand-in.
                // （CS2 的平滑度／環境光遮蔽／金屬度封裝方式與 glTF 的金屬粗糙度貼圖不吻合，
                //   故此處不接上遮罩貼圖，詳見 MaterialDescription。中性的非金屬是誠實的替代值。）
                pbr["metallicFactor"] = 0f;
                pbr["roughnessFactor"] = 0.9f;

                int baseColor = Add(description.BaseColor);
                if (baseColor >= 0) pbr["baseColorTexture"] = new Dictionary<string, object> { ["index"] = baseColor };

                int normal = Add(description.Normal);
                if (normal >= 0) material["normalTexture"] = new Dictionary<string, object> { ["index"] = normal };

                int emissive = Add(description.Emissive);
                if (emissive >= 0) material["emissiveTexture"] = new Dictionary<string, object> { ["index"] = emissive };

                float3 emission = description.EmissiveFactor;
                if (math.any(emission != float3.zero))
                {
                    // glTF caps emissiveFactor at 1 without KHR_materials_emissive_strength, so the
                    // HDR value is clamped here and kept intact in materials.json.
                    // （在沒有 KHR_materials_emissive_strength 的情況下，glTF 的 emissiveFactor 上限為 1，
                    //   故此處夾住 HDR 值，並於 materials.json 中保留原值。）
                    float3 clamped = math.saturate(emission);
                    material["emissiveFactor"] = new[] { clamped.x, clamped.y, clamped.z };
                }

                if (description.AlphaMode != "opaque")
                {
                    material["alphaMode"] = description.AlphaMode.ToUpperInvariant();
                    if (description.AlphaMode == "mask") material["alphaCutoff"] = description.AlphaCutoff;
                }

                material["doubleSided"] = description.DoubleSided;
                return material;
            }

            /// <summary>
            /// Add an image by its bundle-relative path and return its glTF texture index.
            /// （依束相對路徑加入影像，並回傳其 glTF 貼圖索引。）
            /// </summary>
            /// <param name="bundlePath">The bundle-root-relative path, or <c>null</c>.（束根目錄相對路徑，或 <c>null</c>。）</param>
            /// <returns>The texture index, or <c>-1</c>.（貼圖索引，或 <c>-1</c>。）</returns>
            private int Add(string bundlePath)
            {
                if (string.IsNullOrEmpty(bundlePath)) return -1;

                // The .glb lives in meshes/, so a bundle-root-relative path needs one level up.
                // （.glb 位於 meshes/ 之下，故束根目錄相對路徑須向上一層。）
                string uri = $"../{bundlePath}";
                if (_byUri.TryGetValue(uri, out int existing)) return existing;

                Images.Add(new Dictionary<string, object> { ["uri"] = uri });
                if (Samplers.Count == 0)
                {
                    // One repeat sampler serves every map; CS2 tiles its textures and nothing in
                    // the bundle records a per-texture wrap mode to do better with.
                    // （單一重複取樣器即可服務所有貼圖；CS2 的貼圖採平鋪，
                    //   且束中未記錄逐貼圖的環繞模式，無從做得更細。）
                    Samplers.Add(new Dictionary<string, object> { ["wrapS"] = 10497, ["wrapT"] = 10497 });
                }

                Textures.Add(new Dictionary<string, object>
                {
                    ["sampler"] = 0,
                    ["source"] = Images.Count - 1
                });

                int index = Textures.Count - 1;
                _byUri[uri] = index;
                return index;
            }
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
