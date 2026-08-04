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
        /// The query for buildings actually placed in the loaded city.
        /// （已載入城市中實際放置之建築的查詢。）<br/><br/>
        /// Placed buildings are what the ripper cares about, and — unlike the tens of thousands of
        /// catalogue prefabs in the asset database — their meshes are the ones the game has
        /// actually had to load.<br/>
        /// （已放置的建築才是擷取的目標，且不同於資產資料庫中數以萬計的目錄預製模板，
        /// 這些網格是遊戲確實已載入的。）
        /// </summary>
        EntityQuery _placedBuildingQuery;

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

            _placedBuildingQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Buildings.Building>(),
                    ComponentType.ReadOnly<PrefabRef>()
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

            List<Entity> prefabEntities = GetCandidatePrefabs(report);
            bool diagnosedOnce = false;

            foreach (Entity prefabEntity in prefabEntities)
            {
                if (prefabsWalked >= maxPrefabs) break;
                if (!EntityManager.HasBuffer<SubMesh>(prefabEntity)) continue;

                string prefabName = GetPrefabName(prefabEntity) ?? $"entity_{prefabEntity.Index}";
                prefabsWalked++;

                DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefabEntity, true);
                report.AppendLine($"[{prefabsWalked}] {prefabName}  ({subMeshes.Length} sub-mesh)");

                for (int i = 0; i < subMeshes.Length; i++)
                {
                    Entity meshEntity = subMeshes[i].m_SubMesh;
                    if (meshEntity == Entity.Null)
                    {
                        report.AppendLine($"      - sub{i}: null mesh entity");
                        continue;
                    }

                    string meshName = GetPrefabName(meshEntity) ?? $"{prefabName}_sub{i}";
                    string assetId = SanitizeAssetId(meshName);
                    if (!written.Add(assetId))
                    {
                        report.AppendLine($"      = {assetId}: already written");
                        continue;
                    }

                    try
                    {
                        MeshAsset mesh = ReadMesh(meshEntity, assetId, meshName);
                        if (mesh == null)
                        {
                            meshesSkipped++;
                            report.AppendLine($"      - {assetId}: no geometry buffers");

                            // Dump what the entity actually carries, once. Guessing twice at a
                            // structure the game can simply be asked about is how a spike stalls.
                            // （傾印實體實際帶有的元件一次。對於可直接詢問遊戲的結構重複猜測，
                            //   正是驗證停滯的原因。）
                            if (!diagnosedOnce)
                            {
                                diagnosedOnce = true;
                                report.AppendLine($"        components on {assetId}:");
                                foreach (string component in DescribeComponents(meshEntity))
                                    report.AppendLine($"          {component}");
                            }
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

            report.AppendLine();
            report.AppendLine($"Wrote {meshesWritten} mesh(es), skipped {meshesSkipped}, {bytesWritten} bytes total.");
            report.AppendLine($"Output: {outputDirectory}");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Collect the prefab entities worth ripping, preferring prefabs actually placed in the
        /// loaded city over the asset database's full catalogue.<br/>
        /// （收集值得擷取的預製模板實體，優先取用已放置於城市中者，而非資產資料庫的完整目錄。）<br/><br/>
        /// The first spike run walked the catalogue and found 20,017 prefabs whose meshes were all
        /// empty — unsurprising in hindsight, since the game has no reason to load geometry for an
        /// asset nobody placed. Placed buildings are the ones whose meshes must be resident.<br/>
        /// （首次驗證走訪目錄時找到 20,017 個預製模板，其網格皆為空。事後看來並不意外：
        /// 遊戲沒有理由為無人放置的資產載入幾何。已放置的建築才是網格必然常駐者。）
        /// </summary>
        /// <param name="report">The report to append discovery counts to.（用以附加探索統計的報告。）</param>
        /// <returns>Distinct prefab entities, in discovery order.（依探索順序排列的相異預製模板實體。）</returns>
        private List<Entity> GetCandidatePrefabs(StringBuilder report)
        {
            List<Entity> result = new();
            HashSet<Entity> seen = new();

            NativeArray<Entity> placed = _placedBuildingQuery.ToEntityArray(Allocator.TempJob);
            try
            {
                report.AppendLine($"Buildings placed in the city: {placed.Length}");
                foreach (Entity building in placed)
                {
                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(building).m_Prefab;
                    if ((prefabEntity != Entity.Null) && seen.Add(prefabEntity)) result.Add(prefabEntity);
                }
                report.AppendLine($"Distinct building prefabs in use: {result.Count}");
            }
            finally
            {
                placed.Dispose();
            }

            // An empty map (or the editor) has nothing placed; fall back to the catalogue so the
            // spike still reports something useful rather than silently doing nothing.
            // （空白地圖或編輯器中沒有已放置的物件；退回目錄查詢，使驗證仍能回報有用資訊。）
            if (result.Count == 0)
            {
                report.AppendLine("No placed buildings; falling back to the prefab catalogue.");
                NativeArray<Entity> catalogue = _subMeshPrefabQuery.ToEntityArray(Allocator.TempJob);
                try
                {
                    report.AppendLine($"Prefabs with a SubMesh buffer: {catalogue.Length}");
                    foreach (Entity prefabEntity in catalogue)
                    {
                        if (!Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(prefabEntity), out PrefabBase prefab)) continue;
                        if (prefab is not BuildingPrefab) continue;
                        if (seen.Add(prefabEntity)) result.Add(prefabEntity);
                    }
                }
                finally
                {
                    catalogue.Dispose();
                }
            }

            return result;
        }

        /// <summary>
        /// List the component and buffer types present on an entity, for diagnostics.
        /// （列出實體上存在的元件與緩衝區型別，供診斷使用。）
        /// </summary>
        /// <param name="entity">The entity to describe.（待描述的實體。）</param>
        /// <returns>The type names.（型別名稱。）</returns>
        private List<string> DescribeComponents(Entity entity)
        {
            List<string> names = new();
            NativeArray<ComponentType> types = EntityManager.GetComponentTypes(entity, Allocator.TempJob);
            try
            {
                foreach (ComponentType type in types) names.Add(type.GetManagedType()?.Name ?? type.ToString());
            }
            finally
            {
                types.Dispose();
            }

            names.Sort(StringComparer.Ordinal);
            return names;
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
