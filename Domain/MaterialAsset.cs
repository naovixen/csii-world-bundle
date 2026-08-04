using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// The four texture slots the bundle contract defines, in the order they appear in
    /// <c>materials.json</c>.<br/>
    /// （束契約所定義的四個貼圖槽位，順序與 <c>materials.json</c> 中一致。）<br/><br/>
    /// The contract's material object is <b>closed</b> — a map outside this set cannot be written
    /// at all, which is deliberate: a consumer must never silently drop a map it should have
    /// handled. CS2 surfaces carry plenty of other textures (wind flutter, emissive ids, interior
    /// atlases); they are read but not exported in schema 1.<br/>
    /// （契約的材質物件為<b>封閉</b>結構，此集合之外的貼圖無法寫出。此為刻意設計：
    /// 消費端絕不應默默丟棄本應處理的貼圖。CS2 的表面資產另帶有許多其他貼圖，
    /// 於 schema 1 中讀取但不輸出。）
    /// </summary>
    public enum MaterialMap
    {
        /// <summary>Albedo, sRGB, alpha in A.（反照率，sRGB，A 通道為透明度。）</summary>
        BaseColor = 0,

        /// <summary>Tangent-space normal, linear, OpenGL convention.（切線空間法線，線性，OpenGL 慣例。）</summary>
        Normal = 1,

        /// <summary>CS2 MADS map: R metallic, G AO, B detail, A smoothness.（CS2 MADS 貼圖。）</summary>
        Mask = 2,

        /// <summary>Emission, sRGB.（自發光，sRGB。）</summary>
        Emissive = 3
    }

    /// <summary>
    /// A material with its texture keys already resolved to paths — what a glTF writer needs, as
    /// opposed to what <c>materials.json</c> needs.<br/>
    /// （已將貼圖索引鍵解析為路徑的材質，即 glTF 寫出器所需者，有別於 <c>materials.json</c> 所需者。）<br/><br/>
    /// Only the three maps that map cleanly onto glTF's own PBR model appear here.
    /// <see cref="MaterialMap.Mask"/> is deliberately absent: CS2 packs metallic in R, ambient
    /// occlusion in G and <i>smoothness</i> in A, while glTF's metallic-roughness texture wants
    /// metallic in B and <i>roughness</i> in G. Wiring one to the other would not be an
    /// approximation, it would be wrong — and silently so, since the result still renders. The
    /// mask stays in <c>materials.json</c>, which the contract makes authoritative for exactly
    /// this reason.<br/>
    /// （此處僅含能乾淨對應至 glTF 自身 PBR 模型的三張貼圖。<see cref="MaterialMap.Mask"/> 刻意缺席：
    /// CS2 將金屬度置於 R、環境光遮蔽置於 G、<i>平滑度</i>置於 A，而 glTF 的金屬粗糙度貼圖
    /// 要求金屬度位於 B、<i>粗糙度</i>位於 G。將兩者相接並非近似而是錯誤，且是無聲的錯誤，
    /// 因為結果仍能算繪。遮罩貼圖留在 <c>materials.json</c> 中，
    /// 契約正是為此而規定該檔案具權威性。）
    /// </summary>
    public struct MaterialDescription
    {
        /// <summary>Whether a material was found at all.（是否確實找到材質。）</summary>
        public bool Found { get; set; }

        /// <summary>Bundle-root-relative texture paths, or <c>null</c>.（束根目錄相對的貼圖路徑，或 <c>null</c>。）</summary>
        public string BaseColor { get; set; }

        /// <inheritdoc cref="BaseColor"/>
        public string Normal { get; set; }

        /// <inheritdoc cref="BaseColor"/>
        public string Emissive { get; set; }

        /// <summary>Multiplied into the base colour.（乘入基礎色。）</summary>
        public float4 Tint { get; set; }

        /// <summary>Multiplied into the emission.（乘入自發光。）</summary>
        public float3 EmissiveFactor { get; set; }

        /// <summary><c>opaque</c>, <c>mask</c> or <c>blend</c>.（<c>opaque</c>、<c>mask</c> 或 <c>blend</c>。）</summary>
        public string AlphaMode { get; set; }

        /// <summary>The cutoff <c>mask</c> tests against.（<c>mask</c> 模式所比對的裁切門檻。）</summary>
        public float AlphaCutoff { get; set; }

        /// <summary>Whether back faces are drawn.（是否繪製背面。）</summary>
        public bool DoubleSided { get; set; }
    }

    /// <summary>
    /// One CS2 <c>SurfaceAsset</c> reduced to the bundle contract's material model, ready to become
    /// one entry in <c>materials.json</c>.<br/>
    /// （單一 CS2 <c>SurfaceAsset</c> 化約為束契約的材質模型，可成為 <c>materials.json</c> 中的一筆條目。）<br/><br/>
    /// Texture slots hold a <i>key</i> rather than a path while the rip is running. A texture's
    /// bundle path is its content hash, which is only known once the PNG has been encoded — and
    /// encoding happens on a background task, long after the material record is built. The keys are
    /// resolved to paths in one pass when the table is written.<br/>
    /// （擷取進行期間，貼圖槽位存放的是<i>索引鍵</i>而非路徑。貼圖的束路徑即其內容雜湊，
    /// 而該值須待 PNG 編碼完成後方能得知，且編碼於背景工作中進行，遠晚於材質記錄的建立時點。
    /// 索引鍵於寫出材質表時一次解析為路徑。）
    /// </summary>
    public class MaterialAsset
    {
        /// <summary>
        /// The bundle material id — the key in <c>materials.json</c>, and the join key a glTF
        /// primitive carries in its material <c>name</c>.<br/>
        /// （束材質 ID：<c>materials.json</c> 的鍵，亦即 glTF primitive 於材質 <c>name</c> 中帶有的連結鍵。）
        /// </summary>
        public string MaterialId { get; set; }

        /// <summary>
        /// The surface asset's original name, kept for logging.（表面資產的原始名稱，供記錄使用。）
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// The texture key per <see cref="MaterialMap"/> slot, or <c>null</c> where the surface has
        /// no such map.（各 <see cref="MaterialMap"/> 槽位的貼圖索引鍵，若表面沒有該貼圖則為 <c>null</c>。）
        /// </summary>
        public string[] MapKeys { get; } = new string[4];

        /// <summary>
        /// Multiplied into <see cref="MaterialMap.BaseColor"/>. Linear RGBA in 0..1. <c>null</c>
        /// means white.（乘入 <see cref="MaterialMap.BaseColor"/> 的顏色，線性 RGBA，<c>null</c> 代表白色。）
        /// </summary>
        public float4? Tint { get; set; }

        /// <summary>
        /// Multiplied into <see cref="MaterialMap.Emissive"/>. Linear RGB, may exceed 1.
        /// （乘入 <see cref="MaterialMap.Emissive"/> 的係數，線性 RGB，可超過 1。）
        /// </summary>
        public float3? EmissiveFactor { get; set; }

        /// <summary>
        /// <c>opaque</c>, <c>mask</c> or <c>blend</c>.（<c>opaque</c>、<c>mask</c> 或 <c>blend</c>。）
        /// </summary>
        public string AlphaMode { get; set; }

        /// <summary>
        /// The cutoff <see cref="AlphaMode"/> <c>mask</c> tests against.（<c>mask</c> 模式所比對的裁切門檻。）
        /// </summary>
        public float? AlphaCutoff { get; set; }

        /// <summary>
        /// Whether back faces are drawn.（是否繪製背面。）
        /// </summary>
        public bool DoubleSided { get; set; }

        /// <summary>
        /// Producer hints a consumer may act on or ignore.（消費端可採用或忽略的產生端提示。）
        /// </summary>
        public List<string> Flags { get; } = new();

        /// <summary>
        /// Serialise to the contract's material object, omitting everything unset.
        /// （序列化為契約的材質物件，未設定者一律省略。）<br/><br/>
        /// Omission is meaningful here: the contract says a consumer substitutes its own default
        /// for an absent map, so writing an explicit default would be a claim the producer cannot
        /// make.<br/>
        /// （此處的省略具有意義：契約規定缺少的貼圖由消費端以自身預設值替代，
        /// 因此寫出明確的預設值等於做出產生端無法擔保的宣告。）
        /// </summary>
        /// <param name="resolve">Maps a texture key to its bundle-relative path, or <c>null</c> when the texture never reached disk.（將貼圖索引鍵對應至束相對路徑，若該貼圖未能寫入磁碟則回傳 <c>null</c>。）</param>
        /// <returns>The material object.（材質物件。）</returns>
        public Dictionary<string, object> ToJson(System.Func<string, string> resolve)
        {
            Dictionary<string, object> result = new();

            AddMap(result, "baseColor", MaterialMap.BaseColor, resolve);
            AddMap(result, "normal", MaterialMap.Normal, resolve);
            AddMap(result, "mask", MaterialMap.Mask, resolve);
            AddMap(result, "emissive", MaterialMap.Emissive, resolve);

            if (Tint.HasValue)
            {
                float4 tint = math.saturate(Tint.Value);
                if (!tint.Equals(new float4(1f, 1f, 1f, 1f)))
                    result["tint"] = new[] { tint.x, tint.y, tint.z, tint.w };
            }

            if (EmissiveFactor.HasValue)
            {
                float3 emissive = math.max(EmissiveFactor.Value, float3.zero);
                if (math.any(emissive != float3.zero))
                    result["emissiveFactor"] = new[] { emissive.x, emissive.y, emissive.z };
            }

            if (!string.IsNullOrEmpty(AlphaMode) && (AlphaMode != "opaque")) result["alphaMode"] = AlphaMode;
            if (AlphaCutoff.HasValue && (AlphaMode == "mask")) result["alphaCutoff"] = math.saturate(AlphaCutoff.Value);
            if (DoubleSided) result["doubleSided"] = true;
            if (Flags.Count > 0) result["flags"] = Flags.ToArray();

            return result;
        }

        /// <summary>
        /// Resolve this material for a glTF writer.（為 glTF 寫出器解析此材質。）
        /// </summary>
        /// <param name="resolve">Maps a texture key to its bundle-relative path.（將貼圖索引鍵對應至束相對路徑。）</param>
        /// <returns>The resolved description.（已解析的描述。）</returns>
        public MaterialDescription Describe(System.Func<string, string> resolve)
        {
            return new MaterialDescription
            {
                Found = true,
                BaseColor = Path(MaterialMap.BaseColor, resolve),
                Normal = Path(MaterialMap.Normal, resolve),
                Emissive = Path(MaterialMap.Emissive, resolve),
                Tint = Tint.HasValue ? math.saturate(Tint.Value) : new float4(1f, 1f, 1f, 1f),
                EmissiveFactor = EmissiveFactor.HasValue ? math.max(EmissiveFactor.Value, float3.zero) : float3.zero,
                AlphaMode = string.IsNullOrEmpty(AlphaMode) ? "opaque" : AlphaMode,
                AlphaCutoff = AlphaCutoff.HasValue ? math.saturate(AlphaCutoff.Value) : 0.5f,
                DoubleSided = DoubleSided
            };
        }

        /// <summary>
        /// Resolve one texture slot to a path.（將單一貼圖槽位解析為路徑。）
        /// </summary>
        private string Path(MaterialMap slot, System.Func<string, string> resolve)
        {
            string key = MapKeys[(int)slot];
            return string.IsNullOrEmpty(key) ? null : resolve?.Invoke(key);
        }

        /// <summary>
        /// Write one texture slot, if it resolved to a file.（若該貼圖槽位已解析為檔案則寫出。）
        /// </summary>
        private void AddMap(Dictionary<string, object> target, string name, MaterialMap slot, System.Func<string, string> resolve)
        {
            string key = MapKeys[(int)slot];
            if (string.IsNullOrEmpty(key)) return;

            string path = resolve?.Invoke(key);
            if (!string.IsNullOrEmpty(path)) target[name] = path;
        }
    }
}
