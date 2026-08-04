using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that reads real mesh geometry out of the game and writes it as glTF.
    /// （自遊戲中讀取實際網格幾何並輸出為 glTF 的系統。）<br/><br/>
    /// CS2 keeps mesh geometry in plain ECS dynamic buffers on the prefab entity —
    /// <c>MeshVertex</c>, <c>MeshNormal</c>, <c>MeshUV0</c>, <c>MeshTangent</c>, <c>MeshIndex</c> —
    /// alongside a <c>MeshData</c> component and a <c>MeshMaterial</c> buffer describing each
    /// drawable range. The game itself reads these every frame for mouse picking
    /// (<c>Game.Common.RaycastSystem</c>) and camera collision
    /// (<c>Game.Rendering.CameraCollisionSystem</c>), so the data is CPU-resident for the whole
    /// session and needs no GPU readback.<br/>
    /// （CS2 將網格幾何存放於預製模板實體上的一般 ECS 動態緩衝區中。遊戲本身每幀都會為滑鼠拾取與
    /// 攝影機碰撞讀取這些資料，因此資料在整個遊戲期間都常駐於 CPU 端，無須自 GPU 回讀。）<br/><br/>
    /// Traversal is: object prefab entity → <c>SubMesh</c> buffer → mesh entity → geometry buffers.
    /// （走訪路徑為：物件預製模板實體 → <c>SubMesh</c> 緩衝區 → 網格實體 → 幾何緩衝區。）
    /// </summary>
    public partial class MeshRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for object prefabs that carry renderable sub-meshes.
        /// （帶有可繪製子網格的物件預製模板查詢。）
        /// </summary>
        EntityQuery _subMeshPrefabQuery;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _subMeshPrefabQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<PrefabData>(),
                    ComponentType.ReadOnly<SubMesh>()
                }
            });

            base.OnCreate();
            _log.Debug("MeshRipSystem instance created. 網格擷取系統實例創造完成。");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）
        /// </summary>
        protected override void OnDestroy() { base.OnDestroy(); }

        /// <summary>
        /// The event triggered when the system instance is updated.
        /// （當系統實例被更新時觸發的事件。）
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// The PLAN Phase 1 feasibility spike: rip the meshes of the first few building prefabs in
        /// the loaded world and write one <c>.glb</c> per unique mesh.<br/>
        /// （PLAN 第一階段的可行性驗證：擷取已載入世界中前數個建築預製模板的網格，並為每個唯一網格輸出一個 <c>.glb</c>。）<br/><br/>
        /// This deliberately writes one file per <i>mesh</i> rather than per <i>building</i>: the
        /// point is to prove that real vertices, indices and UVs come out and open correctly, not
        /// to assemble multi-part buildings, which is Phase 3's job.<br/>
        /// （此處刻意以「網格」而非「建築」為單位輸出檔案：目的是驗證能取出正確的頂點、索引與 UV，
        /// 而組裝多部件建築屬於第三階段的工作。）
        /// </summary>
        /// <param name="maxPrefabs">The number of building prefabs to walk.（要走訪的建築預製模板數量。）</param>
        /// <returns>A human-readable report of what was written.（可供閱讀的輸出報告。）</returns>
        public string RipSpike(int maxPrefabs = 8)
        {
            string outputDirectory = IOUtils.CombinePath(Instance.CartoDataPath, "spike", "meshes");
            StringBuilder report = new();
            int prefabsWalked = 0, meshesWritten = 0, meshesSkipped = 0;
            long bytesWritten = 0;
            HashSet<string> written = new();

            NativeArray<Entity> prefabEntities = _subMeshPrefabQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                report.AppendLine($"Prefabs with a SubMesh buffer: {prefabEntities.Length}");

                foreach (Entity prefabEntity in prefabEntities)
                {
                    if (prefabsWalked >= maxPrefabs) break;

                    // Only buildings, so the spike is checked against something recognisable in
                    // Blender rather than an arbitrary prop.
                    // （僅取建築，以便在 Blender 中對照可辨識的物件。）
                    if (!Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefabEntity), out PrefabBase prefab)) continue;
                    if (prefab is not BuildingPrefab) continue;

                    prefabsWalked++;
                    report.AppendLine($"[{prefabsWalked}] {prefab.name}");

                    DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                    for (int i = 0; i < subMeshes.Length; i++)
                    {
                        Entity meshEntity = subMeshes[i].m_SubMesh;
                        if (meshEntity == Entity.Null) continue;

                        string meshName = GetPrefabName(meshEntity) ?? $"{prefab.name}_sub{i}";
                        string assetId = SanitizeAssetId(meshName);
                        if (!written.Add(assetId)) continue;

                        try
                        {
                            MeshAsset mesh = ReadMesh(meshEntity, assetId, meshName);
                            if (mesh == null)
                            {
                                meshesSkipped++;
                                report.AppendLine($"      - {assetId}: no geometry buffers");
                                continue;
                            }

                            string path = IOUtils.CombinePath(outputDirectory, $"{assetId}.glb");
                            long size = GltfWriter.Write(path, mesh);
                            bytesWritten += size;
                            meshesWritten++;
                            report.AppendLine($"      + {assetId}: {mesh.Vertices.Length} verts, {mesh.TriangleCount} tris, {mesh.Primitives.Count} prim, {size} B");
                        }
                        catch (Exception ex)
                        {
                            meshesSkipped++;
                            report.AppendLine($"      ! {assetId}: {ex.Message}");
                            _log.Warn($"Failed to rip mesh `{assetId}`: {ex}");
                        }
                    }
                }
            }
            finally
            {
                prefabEntities.Dispose();
            }

            report.AppendLine();
            report.AppendLine($"Wrote {meshesWritten} mesh(es), skipped {meshesSkipped}, {bytesWritten} bytes total.");
            report.AppendLine($"Output: {outputDirectory}");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Read one mesh entity's geometry into a <see cref="MeshAsset"/>.
        /// （將單一網格實體的幾何讀入 <see cref="MeshAsset"/>。）
        /// </summary>
        /// <param name="meshEntity">The mesh (render prefab) entity.（網格（繪製預製模板）實體。）</param>
        /// <param name="assetId">The sanitised asset id.（淨化後的資產 ID。）</param>
        /// <param name="sourceName">The original prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The mesh, or <c>null</c> when the entity carries no geometry.（網格，若實體沒有幾何則為 <c>null</c>。）</returns>
        public MeshAsset ReadMesh(Entity meshEntity, string assetId, string sourceName)
        {
            if (!EntityManager.HasBuffer<MeshVertex>(meshEntity)) return null;
            if (!EntityManager.HasBuffer<MeshIndex>(meshEntity)) return null;

            DynamicBuffer<MeshVertex> vertices = EntityManager.GetBuffer<MeshVertex>(meshEntity, true);
            DynamicBuffer<MeshIndex> indices = EntityManager.GetBuffer<MeshIndex>(meshEntity, true);
            if ((vertices.Length == 0) || (indices.Length == 0)) return null;

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = sourceName,
                Vertices = new float3[vertices.Length],
                Indices = new int[indices.Length]
            };

            for (int i = 0; i < vertices.Length; i++) mesh.Vertices[i] = vertices[i].m_Vertex;
            for (int i = 0; i < indices.Length; i++) mesh.Indices[i] = indices[i].m_Index;

            if (EntityManager.HasBuffer<MeshNormal>(meshEntity))
            {
                DynamicBuffer<MeshNormal> normals = EntityManager.GetBuffer<MeshNormal>(meshEntity, true);
                if (normals.Length == vertices.Length)
                {
                    mesh.Normals = new float3[normals.Length];
                    for (int i = 0; i < normals.Length; i++) mesh.Normals[i] = normals[i].m_Normal;
                }
            }

            if (EntityManager.HasBuffer<MeshUV0>(meshEntity))
            {
                DynamicBuffer<MeshUV0> uvs = EntityManager.GetBuffer<MeshUV0>(meshEntity, true);
                if (uvs.Length == vertices.Length)
                {
                    mesh.Uvs = new float2[uvs.Length];
                    for (int i = 0; i < uvs.Length; i++) mesh.Uvs[i] = uvs[i].m_Uv;
                }
            }

            if (EntityManager.HasBuffer<MeshTangent>(meshEntity))
            {
                DynamicBuffer<MeshTangent> tangents = EntityManager.GetBuffer<MeshTangent>(meshEntity, true);
                if (tangents.Length == vertices.Length)
                {
                    mesh.Tangents = new float4[tangents.Length];
                    for (int i = 0; i < tangents.Length; i++) mesh.Tangents[i] = tangents[i].m_Tangent;
                }
            }

            if (EntityManager.HasBuffer<MeshMaterial>(meshEntity))
            {
                DynamicBuffer<MeshMaterial> materials = EntityManager.GetBuffer<MeshMaterial>(meshEntity, true);
                for (int i = 0; i < materials.Length; i++)
                {
                    MeshMaterial material = materials[i];
                    mesh.Primitives.Add(new MeshPrimitive
                    {
                        StartIndex = material.m_StartIndex,
                        IndexCount = material.m_IndexCount,
                        StartVertex = material.m_StartVertex,
                        VertexCount = material.m_VertexCount,
                        MaterialIndex = material.m_MaterialIndex,
                        MaterialId = $"{assetId}_mat{material.m_MaterialIndex}"
                    });
                }
            }

            if (EntityManager.HasComponent<MeshData>(meshEntity))
            {
                MeshData data = EntityManager.GetComponentData<MeshData>(meshEntity);
                mesh.BoundsMin = data.m_Bounds.min;
                mesh.BoundsMax = data.m_Bounds.max;
            }

            return mesh;
        }

        /// <summary>
        /// Resolve a prefab entity's name, or <c>null</c> when it cannot be resolved.
        /// （解析預製模板實體的名稱，若無法解析則回傳 <c>null</c>。）
        /// </summary>
        /// <param name="entity">The prefab entity.（預製模板實體。）</param>
        /// <returns>The prefab name.（預製模板名稱。）</returns>
        private string GetPrefabName(Entity entity)
        {
            if (!EntityManager.HasComponent<PrefabData>(entity)) return null;
            return Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(entity), out PrefabBase prefab) ? prefab?.name : null;
        }

        /// <summary>
        /// Reduce a prefab name to the bundle contract's <c>assetId</c> pattern
        /// (<c>^[A-Za-z0-9][A-Za-z0-9._-]*$</c>), so it is safe both as a file name and as a
        /// cross-file join key.<br/>
        /// （將預製模板名稱縮減為束契約的 <c>assetId</c> 格式，使其可安全作為檔名與跨檔連結鍵。）
        /// </summary>
        /// <param name="name">The raw prefab name.（原始的預製模板名稱。）</param>
        /// <returns>The sanitised asset id.（淨化後的資產 ID。）</returns>
        public static string SanitizeAssetId(string name)
        {
            if (string.IsNullOrWhiteSpace(name)) return "unnamed";

            StringBuilder builder = new(name.Length);
            foreach (char c in name)
            {
                bool keep = ((c >= 'A') && (c <= 'Z')) || ((c >= 'a') && (c <= 'z'))
                         || ((c >= '0') && (c <= '9')) || (c == '.') || (c == '_') || (c == '-');
                builder.Append(keep ? c : '_');
            }

            string result = builder.ToString();

            // The contract requires the first character to be alphanumeric.
            // （契約要求首字元必須是英數字。）
            int start = 0;
            while ((start < result.Length) && !char.IsLetterOrDigit(result[start])) start++;
            result = (start >= result.Length) ? "unnamed" : result.Substring(start);

            return (result.Length > 180) ? result.Substring(0, 180) : result;
        }
    }
}
