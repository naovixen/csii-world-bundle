using Carto.Domain;
using Colossal.Logging;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// The glTF primitives one mesh becomes, and the material/texture table they reference.
    /// </summary>
    public static partial class GltfWriter
    {
        /// <summary>
        /// Append one mesh's vertex attributes and index ranges to the shared buffer, returning its
        /// glTF primitives.
        /// </summary>
        /// <param name="mesh">The mesh to append.</param>
        /// <param name="bin">The writer over the shared BIN chunk.</param>
        /// <param name="binStream">The shared BIN chunk, for its current offset.</param>
        /// <param name="bufferViews">The document's buffer views.</param>
        /// <param name="accessors">The document's accessors.</param>
        /// <param name="materials">The document's materials.</param>
        /// <param name="textures">The document's shared image pool.</param>
        /// <returns>The primitives for this mesh.</returns>
        private static List<object> BuildPrimitives(MeshAsset mesh, BinaryWriter bin, MemoryStream binStream,
                                                    List<object> bufferViews, List<object> accessors,
                                                    List<object> materials, Dictionary<string, int> materialPool,
                                                    TextureTable textures)
        {
            int vertexCount = mesh.Vertices.Length;
            bool hasNormals = (mesh.Normals != null) && (mesh.Normals.Length == vertexCount);
            bool hasUvs = (mesh.Uvs != null) && (mesh.Uvs.Length == vertexCount);
            bool hasTangents = (mesh.Tangents != null) && (mesh.Tangents.Length == vertexCount);

            List<MeshPrimitive> primitives = NormalizePrimitives(mesh);

            // -- POSITION --------------------------------------------------------------------
            // The bundle's mesh frame is right-handed: mirror X. Track the bounds *after* the
            // mirror, because glTF accessor min/max describe the data as stored.
            //
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
                    //
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
                    //
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
            //
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
                        //
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

                if (!materialPool.TryGetValue(materialId, out int materialIndex))
                {
                    materialIndex = materials.Count;
                    materials.Add(textures.BuildMaterial(materialId));
                    materialPool[materialId] = materialIndex;
                }

                gltfPrimitives.Add(new Dictionary<string, object>
                {
                    ["attributes"] = attributes,
                    ["indices"] = indexAccessor,
                    ["material"] = materialIndex,
                    ["mode"] = ModeTriangles
                });
            }

            return gltfPrimitives;
        }

        /// <summary>
        /// The glTF materials of one file, and the image pool they draw on.
        /// The contract (§6) references textures rather than embedding them: an image's <c>uri</c>
        /// is a relative path into the bundle's texture directory, so a shared atlas is stored once
        /// for the whole bundle instead of once per mesh that uses it. That is also what makes each
        /// <c>.glb</c> open textured on its own — which is the Phase 4 acceptance test.
        /// </summary>
        private class TextureTable
        {
            /// <summary>
            /// Resolves a material id, or <c>null</c> when no table is available.
            /// </summary>
            private readonly Func<string, MaterialDescription> _describe;

            /// <summary>
            /// Image index by URI, so an atlas shared by several materials is listed once.
            /// </summary>
            private readonly Dictionary<string, int> _byUri = new();

            /// <summary>The document's images, textures and samplers.</summary>
            public List<object> Images { get; } = new();

            /// <inheritdoc cref="Images"/>
            public List<object> Textures { get; } = new();

            /// <inheritdoc cref="Images"/>
            public List<object> Samplers { get; } = new();

            /// <summary>
            /// Build a table over a material resolver.
            /// </summary>
            /// <param name="describe">The resolver, or <c>null</c>.</param>
            public TextureTable(Func<string, MaterialDescription> describe) => _describe = describe;

            /// <summary>
            /// Build the glTF material for one bundle material id.
            /// The <c>name</c> is the bundle's join key into <c>materials.json</c> and is always
            /// written. Everything else is a best-effort approximation for standalone viewing:
            /// where CS2's model and glTF's disagree, <c>materials.json</c> stays authoritative
            /// and the disagreeing term is simply left out rather than mistranslated.
            /// </summary>
            /// <param name="materialId">The bundle material id.</param>
            /// <returns>The glTF material.</returns>
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
                    //
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
                //
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
                    //
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
            /// </summary>
            /// <param name="bundlePath">The bundle-root-relative path, or <c>null</c>.</param>
            /// <returns>The texture index, or <c>-1</c>.</returns>
            private int Add(string bundlePath)
            {
                if (string.IsNullOrEmpty(bundlePath)) return -1;

                // The .glb lives one level down from the bundle root — in meshes/ for a prefab, in
                // networks/ for a segment — so a bundle-root-relative path needs one level up.
                //
                string uri = $"../{bundlePath}";
                if (_byUri.TryGetValue(uri, out int existing)) return existing;

                Images.Add(new Dictionary<string, object> { ["uri"] = uri });
                if (Samplers.Count == 0)
                {
                    // One repeat sampler serves every map; CS2 tiles its textures and nothing in
                    // the bundle records a per-texture wrap mode to do better with.
                    //
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
        /// </summary>
        private static string NodeName(MeshAsset mesh) => mesh.Lod > 0 ? $"{mesh.AssetId}_LOD{mesh.Lod}" : mesh.AssetId;

        /// <summary>
        /// Return the mesh's primitives, clamped to the actual index buffer and with a synthesised
        /// whole-mesh primitive if the game reported none.
        /// Out-of-range ranges are dropped rather than trusted: a bad range would otherwise write a
        /// <c>.glb</c> that reads past its own buffer, which viewers report as corruption far from
        /// the real cause.
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
                    _log.Warn($"Mesh `{mesh.AssetId}`: dropping primitive range [{primitive.StartIndex}, {primitive.StartIndex + primitive.IndexCount}) which exceeds the {indexCount}-index buffer.");
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
    }
}
