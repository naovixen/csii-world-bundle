using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// The system that writes the two things a render prefab knows about its own appearance that
    /// its geometry does not carry: the colours it may be painted, and the light it casts.
    /// （寫出繪製預製模板對自身外觀所知、但其幾何未承載的兩件事：可被塗上的顏色，以及其投射的光。）
    /// <br/><br/>
    /// <b>Both hang off the sub-mesh, not the object.</b> That is not a guess: a census over 4,427
    /// referenced prefabs in a full city found the colour palette on 3,984 <i>sub-meshes</i>
    /// covering 1,132,295 instances and on <i>no</i> object prefab at all, and procedural lights on
    /// 1,184 sub-meshes covering 153,548 instances, likewise none at the object level. Building
    /// either against the object prefab would have silently produced an empty file.<br/>
    /// （<b>兩者皆掛於子網格而非物件之上。</b>此非臆測：對完整城市中 4,427 個被參照預製模板的普查發現，
    /// 顏色色盤位於 3,984 個<i>子網格</i>上、涵蓋 1,132,295 個實例，而<i>沒有任何</i>物件預製模板持有它；
    /// 程序式燈光同樣位於 1,184 個子網格上、涵蓋 153,548 個實例，物件層級亦無。
    /// 若針對物件預製模板建構，只會默默產出空檔案。）<br/><br/>
    /// Keyed by the sub-mesh's asset id, which is the glTF node name inside the owning prefab's
    /// <c>.glb</c> — so a consumer joins a node to its palette and its lights by name.<br/>
    /// （以子網格的資產 ID 為鍵，該 ID 即擁有它的預製模板 <c>.glb</c> 內的 glTF 節點名稱，
    /// 故消費端可依名稱將節點連結至其色盤與燈光。）
    /// </summary>
    public partial class AppearanceRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed objects, matching the instance rip so the two agree on which
        /// prefabs the city actually uses.
        /// （已放置物件的查詢，與實例擷取一致，使兩者對城市實際使用哪些預製模板取得共識。）
        /// </summary>
        EntityQuery _objectQuery;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            _objectQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Objects.Object>(),
                    ComponentType.ReadOnly<PrefabRef>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>(),
                    ComponentType.ReadOnly<Game.Objects.Marker>()
                }
            });

            base.OnCreate();
            _log.Debug("AppearanceRipSystem instance created. 外觀擷取系統實例創造完成。");
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
        /// Write <c>colors.json</c> and <c>lights.json</c>.
        /// （寫出 <c>colors.json</c> 與 <c>lights.json</c>。）
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.（要寫入的束根目錄。）</param>
        /// <returns>A human-readable report.（可供閱讀的報告。）</returns>
        public string RipAppearance(string outputDirectory)
        {
            StringBuilder report = new();
            Dictionary<string, object> palettes = new();
            Dictionary<string, object> lights = new();

            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);
            HashSet<Entity> seen = new();

            try
            {
                foreach (Entity entity in entities)
                {
                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if ((prefab == Entity.Null) || !seen.Add(prefab)) continue;
                    if (!EntityManager.HasBuffer<SubMesh>(prefab)) continue;

                    DynamicBuffer<SubMesh> subMeshes = EntityManager.GetBuffer<SubMesh>(prefab, true);
                    for (int i = 0; i < subMeshes.Length; i++)
                    {
                        Entity mesh = subMeshes[i].m_SubMesh;
                        if ((mesh == Entity.Null) || !seen.Add(mesh)) continue;

                        string id = MeshRipSystem.SanitizeAssetId(Instance.Name.GetPrefabName(mesh));
                        if (string.IsNullOrEmpty(id)) continue;

                        if (EntityManager.HasBuffer<ColorVariation>(mesh) && !palettes.ContainsKey(id))
                        {
                            List<object> variations = ReadPalette(mesh);
                            if (variations.Count > 0) palettes[id] = variations;
                        }

                        if (EntityManager.HasBuffer<ProceduralLight>(mesh) && !lights.ContainsKey(id))
                        {
                            List<object> emitters = ReadLights(mesh);
                            if (emitters.Count > 0) lights[id] = emitters;
                        }
                    }
                }
            }
            finally
            {
                entities.Dispose();
            }

            Write(IOUtils.CombinePath(outputDirectory, "colors.json"), palettes);
            Write(IOUtils.CombinePath(outputDirectory, "lights.json"), lights);

            report.AppendLine($"Wrote colors.json ({palettes.Count} palette(s)) "
                            + $"and lights.json ({lights.Count} emitter set(s))");

            _log.Info(report.ToString());
            return report.ToString();
        }

        /// <summary>
        /// Read a sub-mesh's colour variations.（讀取子網格的顏色變化。）<br/><br/>
        /// Each variation is CS2's <c>ColorSet</c>: three channels, which the material's mask map
        /// selects between. The probability is carried because it is what CS2 weights its own
        /// random choice by — a consumer re-colouring a city rather than reproducing this one
        /// needs it, and it costs one number.<br/>
        /// （每個變化即 CS2 的 <c>ColorSet</c>：三個通道，由材質的遮罩貼圖決定各處採用何者。
        /// 機率值一併帶出，因其為 CS2 加權隨機選擇的依據——
        /// 若消費端欲重新為城市上色而非重現此城，便需要它，而其成本僅為一個數字。）
        /// </summary>
        /// <param name="mesh">The render prefab.（繪製預製模板。）</param>
        /// <returns>The variations.（各項變化。）</returns>
        private List<object> ReadPalette(Entity mesh)
        {
            DynamicBuffer<ColorVariation> buffer = EntityManager.GetBuffer<ColorVariation>(mesh, true);
            List<object> variations = new(buffer.Length);

            for (int i = 0; i < buffer.Length; i++)
            {
                ColorVariation variation = buffer[i];
                variations.Add(new Dictionary<string, object>
                {
                    ["channels"] = new[]
                    {
                        Channel(variation.m_ColorSet.m_Channel0),
                        Channel(variation.m_ColorSet.m_Channel1),
                        Channel(variation.m_ColorSet.m_Channel2)
                    },
                    ["probability"] = variation.m_Probability
                });
            }

            return variations;
        }

        /// <summary>
        /// Read a sub-mesh's procedural lights.（讀取子網格的程序式燈光。）<br/><br/>
        /// <b>What is here and what is not.</b> The colours, the response speed and the animation
        /// index are the light's own data and are exact. The <i>position</i> is not exported,
        /// because CS2 does not store one: a procedural light binds to a material slot
        /// (<c>EmissiveProperties.SingleLightMapping.materialId</c>) and its place in the world is
        /// wherever the geometry using that material is. A consumer places each emitter at the
        /// centroid of the primitive using the corresponding material in the same <c>.glb</c>.
        /// Inventing a transform here would be inventing data, on the same grounds as §5.1.<br/>
        /// （<b>此處有什麼、沒有什麼。</b>顏色、反應速度與動畫索引是燈光自身的資料，且為精確值。
        /// <i>位置</i>未輸出，因為 CS2 並未儲存位置：程序式燈光綁定於某個材質槽位，
        /// 其在世界中的位置即為使用該材質的幾何所在之處。
        /// 消費端應將各發光體置於同一 <c>.glb</c> 中使用對應材質的圖元之形心。
        /// 於此處臆造一個變換即等同於捏造資料，理由同 §5.1。）
        /// </summary>
        /// <param name="mesh">The render prefab.（繪製預製模板。）</param>
        /// <returns>The emitters.（各發光體。）</returns>
        private List<object> ReadLights(Entity mesh)
        {
            DynamicBuffer<ProceduralLight> buffer = EntityManager.GetBuffer<ProceduralLight>(mesh, true);
            List<object> emitters = new(buffer.Length);

            float? range = null, temperature = null;
            if (EntityManager.HasComponent<LightEffectData>(mesh))
            {
                LightEffectData effect = EntityManager.GetComponentData<LightEffectData>(mesh);
                range = effect.m_Range;
                temperature = effect.m_ColorTemperature;
            }

            for (int i = 0; i < buffer.Length; i++)
            {
                ProceduralLight light = buffer[i];
                Dictionary<string, object> emitter = new()
                {
                    // The slot is the join to the mesh: the light is wherever this material is.
                    // （槽位即與網格的連結：燈光位於此材質所在之處。）
                    ["slot"] = i,
                    ["color"] = Channel(light.m_Color),
                    ["colorOff"] = Channel(light.m_Color2),
                    ["purpose"] = light.m_Purpose.ToString(),
                    ["responseSpeed"] = System.Math.Round(light.m_ResponseSpeed, 4)
                };

                if (light.m_AnimationIndex >= 0) emitter["animation"] = light.m_AnimationIndex;
                if (range.HasValue) emitter["range"] = System.Math.Round(range.Value, 3);
                if (temperature.HasValue) emitter["temperature"] = System.Math.Round(temperature.Value, 1);

                emitters.Add(emitter);
            }

            return emitters;
        }

        /// <summary>
        /// A colour as the contract writes them: linear RGBA, four numbers.
        /// （契約所定義的顏色寫法：線性 RGBA，四個數字。）
        /// </summary>
        /// <param name="color">The colour.（顏色。）</param>
        /// <returns>The RGBA array.（RGBA 陣列。）</returns>
        private static double[] Channel(Color color) => new double[]
        {
            System.Math.Round(color.r, 4), System.Math.Round(color.g, 4),
            System.Math.Round(color.b, 4), System.Math.Round(color.a, 4)
        };

        private static double[] Channel(Unity.Mathematics.float4 color) => new double[]
        {
            System.Math.Round(color.x, 4), System.Math.Round(color.y, 4),
            System.Math.Round(color.z, 4), System.Math.Round(color.w, 4)
        };

        /// <summary>
        /// Write a table, or remove a stale one when there is nothing to say.
        /// （寫出一份表；若無內容可寫，則移除舊有的過時檔案。）
        /// </summary>
        /// <param name="path">Where to write.（寫出位置。）</param>
        /// <param name="table">The table.（表。）</param>
        private static void Write(string path, Dictionary<string, object> table)
        {
            if (table.Count == 0)
            {
                if (System.IO.File.Exists(path)) System.IO.File.Delete(path);
                return;
            }

            string directory = System.IO.Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory))
                System.IO.Directory.CreateDirectory(directory);

            System.IO.File.WriteAllText(path, JsonConvert.SerializeObject(table, Formatting.Indented));
        }
    }
}
