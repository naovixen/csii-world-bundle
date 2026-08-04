using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// The system that resolves CS2 materials and textures into the bundle's <c>materials.json</c>
    /// and <c>textures/</c> (PLAN Phase 4).<br/>
    /// （將 CS2 材質與貼圖解析至束的 <c>materials.json</c> 與 <c>textures/</c> 的系統（PLAN 第四階段）。）<br/><br/>
    /// A CS2 <see cref="RenderPrefab"/> holds one <see cref="SurfaceAsset"/> per material slot, and
    /// a surface is a property bag: dictionaries of textures, colours, floats and shader keywords,
    /// keyed by HDRP-style names (<c>_BaseColorMap</c>, <c>_NormalMap</c>, <c>_MaskMap</c>,
    /// <c>_EmissiveColorMap</c>). Those four maps plus a handful of scalars are exactly the model
    /// the bundle contract froze in §7, so the reduction is mostly a lookup — the work is in
    /// getting the pixels out.<br/>
    /// （CS2 的 <see cref="RenderPrefab"/> 於每個材質槽位持有一個 <see cref="SurfaceAsset"/>，
    /// 而表面資產本身是一個屬性袋：以 HDRP 風格名稱為鍵的貼圖、顏色、浮點數與著色器關鍵字字典。
    /// 那四張貼圖加上少數純量恰為束契約 §7 所凍結的模型，因此化約大抵只是查表——真正的工作在於取出像素。）<br/><br/>
    /// <b>Deduplication is the whole economy of this phase.</b> A surface asset is shared by every
    /// prefab that uses it and a texture by every surface, so both are keyed by asset identity and
    /// resolved once. What lands on disk is deduped a second time by content hash, because two
    /// distinct assets can still hold the same image.<br/>
    /// （<b>去重是此階段的全部經濟性所在。</b>表面資產由所有使用它的預製模板共用，貼圖又由所有表面共用，
    /// 因此兩者皆以資產識別為鍵、僅解析一次。落於磁碟者再以內容雜湊去重一次，
    /// 因為兩個相異的資產仍可能持有相同的影像。）
    /// </summary>
    public partial class MaterialRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        // The CS2 surface property names behind each of the contract's four maps. CS2 is HDRP-
        // derived, so these are the stock HDRP Lit names.
        // （契約四張貼圖各自對應的 CS2 表面屬性名稱。CS2 衍生自 HDRP，故沿用 HDRP Lit 的名稱。）
        private const string BaseColorMap = "_BaseColorMap";
        private const string NormalMap = "_NormalMap";
        private const string MaskMap = "_MaskMap";
        private const string EmissiveColorMap = "_EmissiveColorMap";

        /// <summary>
        /// The materials ripped so far in the run, or <c>null</c> when idle.
        /// （本次工作中已擷取的材質，閒置時為 <c>null</c>。）
        /// </summary>
        MaterialRun _run;

        /// <summary>
        /// The mutable state of a material rip. Lives exactly as long as the mesh rip that drives
        /// it, because a material is only ever discovered by way of the prefab that uses it.<br/>
        /// （材質擷取的可變狀態。其生命週期恰與驅動它的網格擷取相同，
        /// 因為材質只會經由使用它的預製模板被發現。）
        /// </summary>
        private class MaterialRun
        {
            /// <summary>The bundle root.（束的根目錄。）</summary>
            public string BundleRoot;

            /// <summary>The longest edge a written texture may have.（寫出貼圖的最長邊上限。）</summary>
            public int TextureSizeCap;

            /// <summary>The texture encoder and its dedup tables.（貼圖編碼器與其去重表。）</summary>
            public TextureWriter Textures;

            /// <summary>
            /// Hands a read-back texture to the rip's single writer.
            /// （將已回讀的貼圖交付擷取工作的單一寫出端。）
            /// </summary>
            public Action<PendingTexture> Enqueue;

            /// <summary>
            /// The material table, keyed by bundle material id.（以束材質 ID 為鍵的材質表。）<br/><br/>
            /// Concurrent because the writer thread reads it — to resolve a mesh's texture
            /// references — while the main thread is still adding to it.<br/>
            /// （採用並行字典，因為寫出執行緒會於主執行緒仍在新增時讀取它，以解析網格的貼圖參照。）
            /// </summary>
            public ConcurrentDictionary<string, MaterialAsset> Materials = new();

            /// <summary>Surface asset identity to the material id it was given.（表面資產識別對應其所獲得的材質 ID。）</summary>
            public Dictionary<string, string> BySurface = new();

            /// <summary>Surfaces seen, and how many produced no material.（已見的表面數，以及未產出材質者。）</summary>
            public int Surfaces, Failed;

            /// <summary>Textures handed to the encoder.（已交付編碼端的貼圖數。）</summary>
            public int TexturesRead;

            /// <summary>
            /// How each distinct texture reference turned out.（各相異貼圖參照的處理結果。）<br/><br/>
            /// These four are the diagnosis, not decoration: they separate "the surface handed us a
            /// loadable asset", "it did not, but the database had one under the same guid", "the
            /// pixels are in a VT stack" and "there is nothing anywhere". Only the third calls for
            /// a VT decoder; the others would be different bugs entirely.<br/>
            /// （這四項是診斷而非裝飾：它們區分「表面交出了可載入的資產」、
            /// 「表面未交出，但資料庫中有相同 guid 者」、「像素位於 VT 堆疊中」與「何處皆無」。
            /// 唯有第三種需要 VT 解碼器，其餘者將是完全不同的問題。）
            /// </summary>
            public int TexturesBound, TexturesRebound, TexturesVirtual, TexturesUnbound;

            /// <summary>How many failures have been logged in full.（已完整記錄的失敗數。）</summary>
            public int ReadFailures, BindFailures;

            /// <summary>How many VT surfaces have been probed.（已探測的 VT 表面數。）</summary>
            public int Probes;

            /// <summary>How long the main thread spent reading pixels back.（主執行緒回讀像素所花的時間。）</summary>
            public double ReadbackMilliseconds;

            /// <summary>How many material ids needed a suffix to stay distinct.（為維持相異而需加上後綴的材質 ID 數量。）</summary>
            public int Disambiguated;

            /// <summary>Everything that produced no entry, and why.（所有未產出條目者及其原因。）</summary>
            public List<RipWarning> Skipped = new();
        }

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            base.OnCreate();
            _log.Debug("MaterialRipSystem instance created. 材質擷取系統實例創造完成。");
        }

        /// <summary>
        /// This system does no per-frame work of its own — it is driven entirely by
        /// <see cref="MeshRipSystem"/>, which resolves a prefab's materials at the moment it reads
        /// that prefab's geometry.<br/>
        /// （此系統本身沒有逐幀工作，完全由 <see cref="MeshRipSystem"/> 驅動：
        /// 後者於讀取預製模板幾何的同時解析其材質。）
        /// </summary>
        protected override void OnUpdate() { }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）
        /// </summary>
        protected override void OnDestroy()
        {
            Abandon();
            base.OnDestroy();
        }

        /// <summary>
        /// Whether a material rip is in progress.（是否有材質擷取進行中。）
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// Start collecting materials and textures for a bundle.
        /// （開始為某個束收集材質與貼圖。）
        /// </summary>
        /// <param name="bundleRoot">The bundle root to write <c>textures/</c> and <c>materials.json</c> under.（要在其下寫入 <c>textures/</c> 與 <c>materials.json</c> 的束根目錄。）</param>
        /// <param name="textureSizeCap">The longest edge a written texture may have, or <c>0</c> for no cap.（寫出貼圖的最長邊上限，<c>0</c> 為不限。）</param>
        /// <param name="enqueue">Hands a read-back texture to the rip's writer.（將已回讀的貼圖交付擷取工作的寫出端。）</param>
        public void Begin(string bundleRoot, int textureSizeCap, Action<PendingTexture> enqueue)
        {
            Abandon();

            _run = new MaterialRun
            {
                BundleRoot = bundleRoot,
                TextureSizeCap = textureSizeCap,
                Textures = new TextureWriter(IOUtils.CombinePath(bundleRoot, "textures")),
                Enqueue = enqueue
            };
        }

        /// <summary>
        /// Release a run in flight.（釋放進行中的工作。）
        /// </summary>
        public void Abandon() => _run = null;

        /// <summary>
        /// Encode and write one queued texture. Called on the rip's writer thread.
        /// （編碼並寫出一張排入佇列的貼圖。於擷取工作的寫出執行緒上呼叫。）
        /// </summary>
        /// <param name="texture">The texture to write.（待寫出的貼圖。）</param>
        public void WriteTexture(PendingTexture texture) => _run?.Textures.Write(texture);

        /// <summary>
        /// Resolve a material id to what a glTF writer needs, texture paths and all.
        /// （將材質 ID 解析為 glTF 寫出器所需者，含貼圖路徑。）<br/><br/>
        /// Safe from the writer thread, and correct there because of the order the rip enqueues
        /// work in: a prefab's surfaces are resolved and its textures queued <i>before</i> the
        /// prefab itself is queued, and one writer drains that queue in order. So by the time a
        /// mesh is serialised, every texture it names has already been encoded and has a hash.<br/>
        /// （可安全地於寫出執行緒呼叫，且因擷取工作的排入順序而正確：預製模板的表面先被解析、
        /// 其貼圖先排入佇列，<i>之後</i>該預製模板本身才排入，且由單一寫出端依序排空該佇列。
        /// 因此網格序列化時，其指名的每張貼圖皆已編碼完成並具有雜湊。）
        /// </summary>
        /// <param name="materialId">The bundle material id.（束材質 ID。）</param>
        /// <returns>The description, with <c>Found</c> false when the id is not in the table.（描述；若 ID 不在表中則 <c>Found</c> 為 false。）</returns>
        public MaterialDescription Describe(string materialId)
        {
            MaterialRun run = _run;
            if ((run == null) || string.IsNullOrEmpty(materialId)) return default;
            return run.Materials.TryGetValue(materialId, out MaterialAsset material)
                ? material.Describe(run.Textures.Resolve) : default;
        }

        /// <summary>
        /// Resolve one material slot of a render prefab to its bundle material id, ripping the
        /// surface and its textures the first time they are seen.<br/>
        /// （將繪製預製模板的單一材質槽位解析為束材質 ID，並於首次見到該表面與其貼圖時進行擷取。）<br/><br/>
        /// Returns <c>null</c> when the slot resolves to nothing, which leaves the caller free to
        /// fall back to a synthetic id rather than write a glTF primitive with no material at all.
        /// <br/>（若該槽位無法解析則回傳 <c>null</c>，使呼叫端得以退回合成 ID，
        /// 而非寫出完全沒有材質的 glTF primitive。）
        /// </summary>
        /// <param name="renderPrefab">The render prefab the mesh came from.（網格來源的繪製預製模板。）</param>
        /// <param name="materialIndex">The material slot.（材質槽位。）</param>
        /// <returns>The bundle material id, or <c>null</c>.（束材質 ID，或 <c>null</c>。）</returns>
        public string Resolve(RenderPrefab renderPrefab, int materialIndex)
        {
            MaterialRun run = _run;
            if ((run == null) || (renderPrefab == null)) return null;
            if ((materialIndex < 0) || (materialIndex >= renderPrefab.materialCount)) return null;

            SurfaceAsset surface;
            try
            {
                surface = renderPrefab.GetSurfaceAsset(materialIndex);
            }
            catch (Exception ex)
            {
                _log.Warn($"Surface asset {materialIndex} of `{renderPrefab.name}` did not resolve: {ex.Message}");
                return null;
            }

            if (surface == null) return null;

            string identity = Identity(surface);
            if (run.BySurface.TryGetValue(identity, out string existing)) return existing;

            run.Surfaces++;

            try
            {
                string materialId = Rip(run, surface, identity);
                run.BySurface[identity] = materialId;
                return materialId;
            }
            catch (Exception ex)
            {
                run.Failed++;
                run.BySurface[identity] = null;
                run.Skipped.Add(new RipWarning("material", surface.name ?? identity, $"{ex.GetType().Name} {ex.Message}"));
                _log.Warn($"Failed to rip surface `{surface.name}`: {ex}");
                return null;
            }
        }

        /// <summary>
        /// Declare a material id the meshes use but no surface asset stood behind, so that it is a
        /// key in <c>materials.json</c> with no maps rather than a dangling reference.<br/>
        /// （宣告網格所使用、但其背後沒有表面資產的材質 ID，使其成為 <c>materials.json</c> 中
        /// 不含任何貼圖的鍵，而非懸空的參照。）<br/><br/>
        /// §6 of the contract makes a glTF material's name a key into the table, so the honest
        /// response to an unresolvable surface is an empty entry plus a line in
        /// <c>warnings.jsonl</c> — the same "declare the gap" rule the mesh layer already follows.
        /// A consumer sees a material with no maps and substitutes its own default, which is
        /// precisely what §7 says it should do.<br/>
        /// （契約 §6 使 glTF 材質名稱成為材質表的鍵，因此對無法解析的表面，
        /// 誠實的處理方式是一筆空白條目加上 <c>warnings.jsonl</c> 中的一行——
        /// 即網格層已遵循的同一條「宣告缺口」規則。消費端看到沒有貼圖的材質後會以自身預設值替代，
        /// 這正是 §7 所規定的行為。）
        /// </summary>
        /// <param name="materialId">The id the meshes reference.（網格所參照的 ID。）</param>
        /// <param name="reason">Why no surface stood behind it.（其背後沒有表面資產的原因。）</param>
        public void Declare(string materialId, string reason)
        {
            MaterialRun run = _run;
            if ((run == null) || string.IsNullOrEmpty(materialId)) return;
            if (run.Materials.ContainsKey(materialId)) return;

            run.Materials[materialId] = new MaterialAsset { MaterialId = materialId, SourceName = materialId };
            run.Skipped.Add(new RipWarning("material", materialId, reason));
        }

        /// <summary>
        /// Reduce one surface asset to a <see cref="MaterialAsset"/> and queue its textures.
        /// （將單一表面資產化約為 <see cref="MaterialAsset"/>，並將其貼圖排入佇列。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="identity">Its stable identity.（其穩定識別。）</param>
        /// <returns>The bundle material id.（束材質 ID。）</returns>
        private string Rip(MaterialRun run, SurfaceAsset surface, string identity)
        {
            // The property dictionaries are empty until the asset's data is read off disk.
            // （在資產資料自磁碟讀入前，屬性字典皆為空。）
            surface.LoadProperties(false);

            try
            {
                MaterialAsset material = new()
                {
                    MaterialId = AssignId(run, surface, identity),
                    SourceName = surface.name
                };

                material.MapKeys[(int)MaterialMap.BaseColor] = Take(run, surface, BaseColorMap);
                material.MapKeys[(int)MaterialMap.Normal] = Take(run, surface, NormalMap);
                material.MapKeys[(int)MaterialMap.Mask] = Take(run, surface, MaskMap);
                material.MapKeys[(int)MaterialMap.Emissive] = Take(run, surface, EmissiveColorMap);

                if (surface.TryGetColorProperty("_BaseColor", out Color tint))
                    material.Tint = new Unity.Mathematics.float4(tint.r, tint.g, tint.b, tint.a);

                if (surface.TryGetColorProperty("_EmissiveColor", out Color emissive))
                    material.EmissiveFactor = new Unity.Mathematics.float3(emissive.r, emissive.g, emissive.b);

                material.AlphaMode = AlphaMode(surface);
                if (surface.TryGetFloatProperty("_AlphaCutoff", out float cutoff)) material.AlphaCutoff = cutoff;

                DescribeTemplate(surface, material);

                run.Materials[material.MaterialId] = material;
                return material.MaterialId;
            }
            finally
            {
                // Every surface in a city held open at once is a large amount of managed state for
                // dictionaries we have already copied out of.
                // （同時保持城市中所有表面資產載入，等於為已複製完的字典佔用大量受管狀態。）
                try { surface.UnloadProperties(false); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Give a surface asset its bundle material id, keeping ids distinct across the bundle.
        /// （為表面資產指派束材質 ID，並使 ID 於束內維持相異。）<br/><br/>
        /// The id is a join key, so a collision is silent corruption in exactly the way a colliding
        /// <c>assetId</c> is: two surfaces sharing an id means one entry in <c>materials.json</c>
        /// overwrites the other and every primitive naming it gets whichever won. CS2 does not
        /// require surface names to be unique, and generic names like <c>Default</c> are common.<br/>
        /// （ID 是連結鍵，因此其碰撞與 <c>assetId</c> 碰撞同樣是無聲的資料損毀：兩個表面共用 ID
        /// 意味著 <c>materials.json</c> 中一筆條目覆寫另一筆，而所有指名它的 primitive 都指向勝出者。
        /// CS2 並不要求表面名稱唯一，且 <c>Default</c> 之類的通用名稱十分常見。）<br/><br/>
        /// Unlike the prefab set, surfaces are discovered one at a time as the rip walks prefabs —
        /// there is no point at which the whole set is in hand to assign over. The first claimant
        /// therefore keeps the readable name and later ones take a suffix, which is collision-free
        /// within a bundle. That is all the contract needs: a material id only ever has to join
        /// <c>meshes/*.glb</c> to the <c>materials.json</c> written beside them in the same run.<br/>
        /// （與預製模板集合不同，表面資產是隨擷取走訪預製模板而逐一被發現的，
        /// 沒有任何時點能取得完整集合以一次指派。因此首位取用者保留可讀名稱，其後者加上後綴，
        /// 此法於束內即無碰撞。契約所需僅止於此：材質 ID 只須連結同一次工作中並列寫出的
        /// <c>meshes/*.glb</c> 與 <c>materials.json</c>。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="identity">Its stable identity.（其穩定識別。）</param>
        /// <returns>An id no other material in this bundle holds.（此束中無其他材質持有的 ID。）</returns>
        private static string AssignId(MaterialRun run, SurfaceAsset surface, string identity)
        {
            string id = MeshRipSystem.SanitizeAssetId(surface.name);
            if (!run.Materials.ContainsKey(id)) return id;

            run.Disambiguated++;
            return MeshRipSystem.SanitizeAssetId($"{id}_{Digest(identity)}");
        }

        /// <summary>
        /// Look up one of the contract's maps on a surface and make sure its pixels are on their
        /// way to disk.<br/>
        /// （於表面資產上查找契約所定義的貼圖之一，並確保其像素正前往磁碟。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <returns>The texture key, or <c>null</c> when the surface has no such map.（貼圖索引鍵，若表面沒有該貼圖則為 <c>null</c>。）</returns>
        private string Take(MaterialRun run, SurfaceAsset surface, string property)
        {
            if (!surface.TryGetTexture(property, out TextureAsset texture) || (texture == null)) return null;

            string key = Identity(texture);

            // Claim before reading: a texture shared by fifty surfaces must be read back once, and
            // the claim is what the other forty-nine see. A claim that goes on to fail is still
            // worth keeping — it stops the next forty-nine retrying a lookup already known to lose.
            // （先取用再讀取：由五十個表面共用的貼圖只須回讀一次，其餘四十九次所見的即是此取用標記。
            //   即使取用後失敗仍值得保留：可避免其餘四十九次重試已知會失敗的查找。）
            if (!run.Textures.Claim(key)) return key;

            TextureAsset source = Bind(run, surface, property, texture, key);
            if (source == null) return null;

            Stopwatch clock = Stopwatch.StartNew();
            try
            {
                PendingTexture pending = ReadBack(source, key, run.TextureSizeCap);
                if (pending == null)
                {
                    run.Skipped.Add(new RipWarning("texture", Describe(source, key), "no readable pixels"));
                    return null;
                }

                run.Enqueue(pending);
                run.TexturesRead++;
                return key;
            }
            catch (Exception ex)
            {
                run.Skipped.Add(new RipWarning("texture", Describe(source, key), $"{ex.GetType().Name} {ex.Message}"));

                // Log the first few in full and count the rest. A failure that hits every texture
                // in the city writes the same stack trace thousands of times otherwise, which
                // buries whatever else the log had to say.
                // （前數筆完整記錄，其餘僅計數。若某項失敗遍及城市中每張貼圖，
                //   否則將重複寫出同一段呼叫堆疊數千次，掩埋記錄檔中其他訊息。）
                if (++run.ReadFailures <= 5) _log.Warn($"Failed to read texture `{Describe(source, key)}`: {ex}");
                return null;
            }
            finally
            {
                run.ReadbackMilliseconds += clock.Elapsed.TotalMilliseconds;
            }
        }

        /// <summary>
        /// Turn the texture reference a surface hands out into one that can actually be loaded, or
        /// classify why it cannot.<br/>
        /// （將表面資產交出的貼圖參照轉換為確實可載入者，或說明其為何不可載入。）<br/><br/>
        /// <b>A surface's texture dictionary does not hold loadable assets.</b> Most CS2 surfaces
        /// are virtual-textured, and for those the per-map entries are <i>dummy</i>
        /// <see cref="TextureAsset"/>s: no name, no database, nothing behind them but a guid,
        /// because the pixels were preprocessed into a VT stack at build time and the standalone
        /// texture is not shipped. Calling <see cref="TextureAsset.Load(int)"/> on one dies inside
        /// <c>AssetData.GetReadStream</c> on the null database.<br/>
        /// （<b>表面資產的貼圖字典並不持有可載入的資產。</b>CS2 多數表面採虛擬貼圖，
        /// 其逐貼圖條目為<i>虛設的</i> <see cref="TextureAsset"/>：沒有名稱、沒有資料庫，
        /// 背後僅有一個 guid，因為像素於建置時已預處理進 VT 堆疊，獨立的貼圖並未隨遊戲附帶。
        /// 對其呼叫 <see cref="TextureAsset.Load(int)"/> 會在 <c>AssetData.GetReadStream</c> 中
        /// 因資料庫為 null 而失敗。）<br/><br/>
        /// The game never makes that call: <c>SurfaceAsset.SyncToUnityMaterial</c> guards every
        /// texture with <c>IsHandledByVirtualTexturing</c> and binds only the ones left over. This
        /// does the same, and additionally tries the global asset database by guid first — a
        /// reference can be unbound simply because the dictionary was deserialised without one,
        /// and where that is the case the real asset is still there to be found.<br/>
        /// （遊戲本身從不做此呼叫：<c>SurfaceAsset.SyncToUnityMaterial</c> 以
        /// <c>IsHandledByVirtualTexturing</c> 守衛每張貼圖，僅繫結其餘者。此處作法相同，
        /// 並額外先以 guid 查詢全域資產資料庫：參照可能僅因字典反序列化時未帶資料庫而未繫結，
        /// 若屬此情形，真正的資產仍可被找到。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface the reference came from.（該參照的來源表面資產。）</param>
        /// <param name="property">The shader property it sits under.（其所屬的著色器屬性。）</param>
        /// <param name="texture">The reference to bind.（待繫結的參照。）</param>
        /// <param name="key">Its texture key, for reporting.（其貼圖索引鍵，供回報使用。）</param>
        /// <returns>A loadable asset, or <c>null</c>.（可載入的資產，或 <c>null</c>。）</returns>
        private static TextureAsset Bind(MaterialRun run, SurfaceAsset surface, string property, TextureAsset texture, string key)
        {
            if (!IsUnbound(texture))
            {
                run.TexturesBound++;
                return texture;
            }

            // Unbound, but the guid survives. If the standalone texture did ship, this finds it.
            // （雖未繫結，但 guid 仍在。若獨立貼圖確實隨遊戲附帶，此處即可找到。）
            try
            {
                // Fully qualified: `using UnityEngine` brings in a different Hash128.
                // （完整限定：`using UnityEngine` 會引入另一個同名的 Hash128。）
                Colossal.Hash128 guid = texture.id.guid;
                if (guid.isValid
                    && AssetDatabase.global.TryGetAsset(guid, out TextureAsset bound)
                    && (bound != null) && !IsUnbound(bound))
                {
                    run.TexturesRebound++;
                    return bound;
                }
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Texture guid lookup failed for `{key}`: {ex.Message}");
            }

            // Nothing to load. Say which of the two reasons it is, because they call for different
            // work: a virtual-textured map needs the VT stack decoding, while a merely missing one
            // is a gap in the install.
            // （無可載入者。須說明是兩種原因中的哪一種，因為兩者所需的處理不同：
            //   虛擬貼圖需要解碼 VT 堆疊，而單純缺失者則是安裝內容的缺口。）
            bool virtualTextured;
            try
            {
                virtualTextured = surface.IsHandledByVirtualTexturing(
                    new KeyValuePair<string, TextureAsset>(property, texture));
            }
            catch (Exception)
            {
                // The material library could not say. Unbound-and-unknown is still unbound.
                // （材質庫無法判定。未繫結且原因不明，仍屬未繫結。）
                virtualTextured = false;
            }

            if (virtualTextured)
            {
                run.TexturesVirtual++;
                run.Skipped.Add(new RipWarning("texture", key,
                    $"virtual-textured ({property}): the pixels live in a VT stack, not a standalone texture asset"));
                Probe(run, surface, property);
            }
            else
            {
                run.TexturesUnbound++;
                run.Skipped.Add(new RipWarning("texture", key,
                    $"unbound texture reference ({property}): no asset in the database for this guid"));
            }

            return null;
        }

        /// <summary>
        /// Log what a virtual-textured surface offers instead of its textures, for the first few
        /// such surfaces in a rip.<br/>
        /// （對擷取工作中最初數個虛擬貼圖表面，記錄其以何者代替貼圖提供。）<br/><br/>
        /// Purely diagnostic, and deliberately so. Whether a VT decoder is worth building depends
        /// on facts that can only be established with the game running: whether the preprocessed
        /// stacks resolve to assets at all, how many layers each carries, and what shape they are.
        /// Guessing at that and writing the decoder anyway is how the standalone-texture assumption
        /// got this far in the first place.<br/>
        /// （純屬診斷，且刻意如此。是否值得建構 VT 解碼器，取決於唯有在遊戲執行時才能確立的事實：
        /// 預處理堆疊是否確實可解析為資產、各自帶有多少層、其形狀為何。
        /// 憑猜測逕行撰寫解碼器，正是「貼圖為獨立資產」這個假設得以走到此地步的原因。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The virtual-textured surface.（虛擬貼圖的表面資產。）</param>
        /// <param name="property">The map that could not be taken.（無法取得的貼圖。）</param>
        private static void Probe(MaterialRun run, SurfaceAsset surface, string property)
        {
            if (run.Probes++ >= 3) return;

            StringBuilder report = new();
            report.AppendLine($"VT probe [{run.Probes}] surface `{surface.name}`, missing map {property}:");

            try
            {
                report.AppendLine($"  isVTMaterial={surface.isVTMaterial} isCurrentlyUsingVT={surface.isCurrentlyUsingVT} hasVTSurfaceAsset={surface.hasVTSurfaceAsset}");

                int index = 0;
                foreach (Colossal.Hash128 guid in surface.preProcessedTextureGuids)
                {
                    bool resolved = false;
                    string detail = "no asset in the database";

                    try
                    {
                        if (AssetDatabase.global.TryGetAsset(guid, out VTTextureAsset vt) && (vt != null))
                        {
                            resolved = true;
                            vt.LoadHeader();
                            detail = $"{vt.width}x{vt.height} {vt.format}, textureAsset={(IsUnbound(vt.textureAsset) ? "unbound" : "bound")}";
                        }
                    }
                    catch (Exception ex)
                    {
                        detail = $"{ex.GetType().Name} {ex.Message}";
                    }

                    report.AppendLine($"  preProcessed[{index++}] {guid} -> {(resolved ? "VTTextureAsset" : "nothing")}: {detail}");
                }

                if (index == 0) report.AppendLine("  no preprocessed texture guids at all");
            }
            catch (Exception ex)
            {
                report.AppendLine($"  probe failed: {ex}");
            }

            _log.Info(report.ToString());
        }

        /// <summary>
        /// Whether an asset reference has nothing behind it to read.
        /// （資產參照背後是否無可讀取之物。）<br/><br/>
        /// The database is checked first and separately: <c>isDummy</c> is itself defined in terms
        /// of the database, so asking a reference that has none is not obviously safe.<br/>
        /// （先單獨檢查資料庫：<c>isDummy</c> 本身即以資料庫定義，
        /// 對沒有資料庫的參照詢問該屬性並非顯然安全。）
        /// </summary>
        /// <param name="asset">The asset to test.（待檢測的資產。）</param>
        /// <returns>Whether loading it would fail.（載入它是否將會失敗。）</returns>
        private static bool IsUnbound(AssetData asset)
        {
            if (asset == null) return true;

            try
            {
                if (asset.database == null) return true;
                return asset.isDummy;
            }
            catch (Exception)
            {
                return true;
            }
        }

        /// <summary>
        /// Name a texture for a report. A dummy reference has no name, so the key stands in.
        /// （為報告命名貼圖。虛設的參照沒有名稱，故以索引鍵替代。）
        /// </summary>
        /// <param name="asset">The asset.（資產。）</param>
        /// <param name="key">Its texture key.（其貼圖索引鍵。）</param>
        /// <returns>Something a human can act on.（可供人判讀處理的字串。）</returns>
        private static string Describe(AssetData asset, string key)
        {
            try
            {
                string name = asset?.name;
                if (!string.IsNullOrEmpty(name)) return name;
            }
            catch (Exception)
            {
                // An unbound asset throws rather than returning null.（未繫結的資產會擲出例外。）
            }

            return key;
        }

        /// <summary>
        /// Copy a texture asset's pixels out of the GPU as <c>RGBA8</c>.
        /// （自 GPU 將貼圖資產的像素以 <c>RGBA8</c> 複製出來。）<br/><br/>
        /// Shipped CS2 art is block-compressed (BC5, BC7 and friends), and there is no CPU decoder
        /// for those formats anywhere in the game's libraries — the asset database's own
        /// <c>ToImageData</c> handles uncompressed surfaces only. Letting the GPU do the decoding is
        /// therefore not an optimisation but the only path: blit through a render target of the
        /// right colour space and read the result back.<br/>
        /// （CS2 隨遊戲附帶的美術資源採區塊壓縮（BC5、BC7 等），而遊戲程式庫中並無任何 CPU 解碼器
        /// ——資產資料庫自身的 <c>ToImageData</c> 僅處理未壓縮的表面。因此讓 GPU 負責解碼並非最佳化，
        /// 而是唯一可行路徑：經由色彩空間正確的繪製目標進行 blit，再將結果回讀。）<br/><br/>
        /// The render target's colour space matches the source's, so the bytes pass through
        /// unchanged: an sRGB base colour stays sRGB and a linear normal or mask map is not
        /// gamma-mangled on the way out. That is what §7.1 of the contract promises.<br/>
        /// （繪製目標的色彩空間與來源一致，使位元組原封不動地通過：sRGB 的基礎色維持 sRGB，
        /// 線性的法線或遮罩貼圖不會在輸出途中被伽瑪扭曲。此即契約 §7.1 的承諾。）<br/><br/>
        /// Must run on the main thread.（必須於主執行緒執行。）
        /// </summary>
        /// <param name="asset">The texture asset.（貼圖資產。）</param>
        /// <param name="key">Its stable identity.（其穩定識別。）</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.（寫出的最長邊上限，<c>0</c> 為不限。）</param>
        /// <returns>The pixels, or <c>null</c> when nothing came back.（像素，若無回讀結果則為 <c>null</c>。）</returns>
        private static PendingTexture ReadBack(TextureAsset asset, string key, int cap)
        {
            // Loading a smaller mip is cheaper than loading the full image and shrinking it, so ask
            // for the smallest one that still satisfies the cap and let the blit do the remainder.
            // （載入較小的 mip 比載入完整影像再縮小更省，因此先索取仍能滿足上限的最小者，
            //   其餘交由 blit 完成。）
            int mipBias = MipBias(asset.width, asset.height, asset.mipsCount, cap);

            Texture source = null;
            try
            {
                source = asset.Load(mipBias);
                if (source == null) return null;

                int width = source.width, height = source.height;
                if ((width <= 0) || (height <= 0)) return null;

                if (cap > 0)
                {
                    // Scale the long edge to the cap and the short edge with it, so a non-square
                    // texture keeps its aspect ratio and its UVs stay valid.
                    // （將長邊縮至上限，短邊等比隨之縮放，使非正方形貼圖維持長寬比且 UV 仍然有效。）
                    int longest = Mathf.Max(width, height);
                    if (longest > cap)
                    {
                        width = Mathf.Max(1, (width * cap) / longest);
                        height = Mathf.Max(1, (height * cap) / longest);
                    }
                }

                bool srgb = GraphicsFormatUtility.IsSRGBFormat(asset.format);
                byte[] pixels = Blit(source, width, height, srgb);
                if (pixels == null) return null;

                return new PendingTexture
                {
                    Key = key,
                    SourceName = asset.name,
                    Width = width,
                    Height = height,
                    Pixels = pixels
                };
            }
            finally
            {
                // The texture was loaded for this one readback. Holding every texture a city uses
                // would defeat the point of streaming them in the first place.
                // （此貼圖僅為本次回讀而載入。保留城市所用的每一張貼圖，將使串流載入失去意義。）
                if (source != null) { try { asset.Unload(false); } catch { /* nothing useful to do */ } }
            }
        }

        /// <summary>
        /// Blit a texture through a render target and read the result back as <c>RGBA8</c>.
        /// （將貼圖經繪製目標 blit 後，以 <c>RGBA8</c> 回讀結果。）<br/><br/>
        /// Rows come back bottom-up, which is Unity's convention and the opposite of PNG's, so they
        /// are flipped here. The glTF writer separately flips <c>V</c> on the mesh side; both are
        /// the same change of origin and the two must agree or every texture lands upside down.<br/>
        /// （回讀的列由下而上，此為 Unity 慣例，與 PNG 相反，故於此翻轉。
        /// glTF 寫出器另於網格端翻轉 <c>V</c>；兩者皆是同一次原點變換，必須一致，否則所有貼圖將上下顛倒。）
        /// </summary>
        /// <param name="source">The loaded texture.（已載入的貼圖。）</param>
        /// <param name="width">The output width.（輸出寬度。）</param>
        /// <param name="height">The output height.（輸出高度。）</param>
        /// <param name="srgb">Whether the source stores sRGB-encoded values.（來源是否存放 sRGB 編碼的數值。）</param>
        /// <returns>Row-major <c>RGBA8</c> pixels, top row first.（以列為主的 <c>RGBA8</c> 像素，由上而下。）</returns>
        private static byte[] Blit(Texture source, int width, int height, bool srgb)
        {
            RenderTexture target = RenderTexture.GetTemporary(width, height, 0, RenderTextureFormat.ARGB32,
                srgb ? RenderTextureReadWrite.sRGB : RenderTextureReadWrite.Linear);
            RenderTexture previous = RenderTexture.active;
            Texture2D readback = null;

            try
            {
                Graphics.Blit(source, target);
                RenderTexture.active = target;

                readback = new Texture2D(width, height, TextureFormat.RGBA32, false, linear: !srgb);
                readback.ReadPixels(new Rect(0f, 0f, width, height), 0, 0, false);
                readback.Apply(false, false);

                byte[] raw = readback.GetRawTextureData();
                int stride = width * 4;
                if ((raw == null) || (raw.Length < stride * height)) return null;

                byte[] pixels = new byte[stride * height];
                for (int y = 0; y < height; y++)
                    Buffer.BlockCopy(raw, (height - 1 - y) * stride, pixels, y * stride, stride);

                return pixels;
            }
            finally
            {
                RenderTexture.active = previous;
                RenderTexture.ReleaseTemporary(target);
                if (readback != null) UnityEngine.Object.Destroy(readback);
            }
        }

        /// <summary>
        /// The smallest mip level whose longest edge still meets the cap.
        /// （最長邊仍符合上限的最小 mip 層級。）
        /// </summary>
        /// <param name="width">The full-size width.（完整尺寸的寬度。）</param>
        /// <param name="height">The full-size height.（完整尺寸的高度。）</param>
        /// <param name="mips">How many mip levels the asset has.（資產的 mip 層級數。）</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.（寫出的最長邊上限，<c>0</c> 為不限。）</param>
        /// <returns>The mip bias to load at.（載入時使用的 mip 偏移。）</returns>
        private static int MipBias(int width, int height, int mips, int cap)
        {
            if ((cap <= 0) || (mips <= 1)) return 0;

            int bias = 0, longest = Mathf.Max(width, height);
            while ((longest > cap) && (bias < mips - 1))
            {
                longest /= 2;
                bias++;
            }

            return bias;
        }

        /// <summary>
        /// Read the transparency mode out of the surface's shader keywords.
        /// （自表面資產的著色器關鍵字讀出透明模式。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <returns>The contract's alpha mode.（契約所定義的 alpha 模式。）</returns>
        private static string AlphaMode(SurfaceAsset surface)
        {
            try
            {
                if (surface.HasKeyword("_SURFACE_TYPE_TRANSPARENT")) return "blend";
                if (surface.HasKeyword("_ALPHATEST_ON")) return "mask";
            }
            catch (Exception)
            {
                // A surface whose keywords did not load says nothing about transparency, and
                // opaque is the contract's default.（關鍵字未載入的表面對透明度未置一詞，
                // 而契約的預設值即為不透明。）
            }

            return "opaque";
        }

        /// <summary>
        /// Fill in the flags and face mode a surface's material template implies.
        /// （依表面資產的材質模板填入旗標與面向模式。）<br/><br/>
        /// CS2 builds every surface from a named template — <c>defaultCutoutDoubleSided</c>,
        /// <c>vegetationLeaves</c>, <c>glass</c> and so on — and the name is the only place that
        /// says whether back faces are drawn or what kind of surface this is meant to be. The
        /// contract's flags are hints a consumer may ignore, so only signals that are actually
        /// legible in the template name are emitted: a wrong hint is worse than a missing one.<br/>
        /// （CS2 的每個表面資產都由具名模板建構，而該名稱是唯一能說明是否繪製背面、
        /// 以及此表面意圖為何的地方。契約的旗標僅為消費端可忽略的提示，
        /// 因此只輸出確實能自模板名稱判讀的訊號：錯誤的提示比缺少提示更糟。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="material">The material being built.（正在建構的材質。）</param>
        private static void DescribeTemplate(SurfaceAsset surface, MaterialAsset material)
        {
            string template;
            try
            {
                template = surface.GetTemplateMaterial()?.name;
            }
            catch (Exception ex)
            {
                _log.Verbose($"No material template for `{surface.name}`: {ex.Message}");
                return;
            }

            if (string.IsNullOrEmpty(template)) return;

            string name = template.ToLowerInvariant();
            material.DoubleSided = name.Contains("doublesided");

            if (name.Contains("vegetation") || name.Contains("grass") || name.Contains("leaves") || name.Contains("branches"))
                material.Flags.Add("foliage");

            if (name.Contains("glass")) material.Flags.Add("glass");
            if (name.Contains("decal")) material.Flags.Add("decal");

            // CS2 lights windows individually by looking up an id in the emissive map's companion
            // textures; a surface carrying those is the emissive-windows case the contract names.
            // （CS2 透過查找自發光貼圖之伴隨貼圖中的 ID 來逐一點亮窗戶；
            //   帶有這些貼圖的表面即契約所指的自發光窗戶情形。）
            if (!string.IsNullOrEmpty(material.MapKeys[(int)MaterialMap.Emissive])
                && surface.HasProperty("_EmissiveID0"))
                material.Flags.Add("emissiveWindows");
        }

        // ====================================================================================
        // Finishing.（收尾。）
        // ====================================================================================

        /// <summary>
        /// Drain the encoder, write <c>materials.json</c>, and report what came out.
        /// （排空編碼端、寫出 <c>materials.json</c>，並回報結果。）
        /// </summary>
        /// <param name="report">The report to append to.（要附加內容的報告。）</param>
        /// <param name="warnings">Receives everything that produced no entry, for <c>warnings.jsonl</c>.（接收所有未產出條目者，供 <c>warnings.jsonl</c> 使用。）</param>
        /// <returns>A one-line summary for the result dialog.（供結果對話框使用的單行摘要。）</returns>
        public string Finish(StringBuilder report, List<RipWarning> warnings)
        {
            MaterialRun run = _run;
            _run = null;
            if (run == null) return null;

            while (run.Textures.Skipped.TryDequeue(out RipWarning skipped)) run.Skipped.Add(skipped);

            int written = WriteTable(run);

            report.AppendLine();
            // The table can hold more entries than there were surfaces: an unresolvable surface is
            // still declared, so that a mesh naming it does not dangle.
            // （材質表的條目數可能多於表面數：無法解析的表面仍會被宣告，
            //   使指名它的網格不致懸空。）
            report.AppendLine($"Materials: {written} entries in materials.json from {run.Surfaces} surface(s), failed {run.Failed}, disambiguated {run.Disambiguated}.");
            report.AppendLine($"Textures: {run.TexturesRead} read back, {run.Textures.Written} file(s), {run.Textures.Deduped} duplicate(s) by content hash, failed {run.Textures.Failed}.");
            report.AppendLine($"Texture bytes: {run.Textures.Bytes / (1024d * 1024d):F1} MiB; readback held the main thread {run.ReadbackMilliseconds / 1000d:F1} s.");

            // The four buckets every distinct texture reference lands in. Read these before
            // concluding anything about the texture layer — they say which problem this is.
            // （所有相異貼圖參照歸屬的四個類別。對貼圖層下任何結論前請先讀這幾項，
            //   它們指出這是哪一種問題。）
            int references = run.TexturesBound + run.TexturesRebound + run.TexturesVirtual + run.TexturesUnbound;
            report.AppendLine($"Texture references: {references} distinct — {run.TexturesBound} loadable as given, "
                            + $"{run.TexturesRebound} rebound by guid, {run.TexturesVirtual} virtual-textured, "
                            + $"{run.TexturesUnbound} with nothing behind them.");

            if (run.Skipped.Count > 0)
            {
                report.AppendLine($"Skipped {run.Skipped.Count}:");
                for (int i = 0; (i < 20) && (i < run.Skipped.Count); i++)
                    report.AppendLine($"  - {run.Skipped[i].Kind} {run.Skipped[i].AssetId}: {run.Skipped[i].Reason}");
            }

            warnings?.AddRange(run.Skipped);

            return $"{written} material(s), {run.Textures.Written} texture(s), {run.Textures.Bytes / (1024d * 1024d):F1} MiB.";
        }

        /// <summary>
        /// Write <c>materials.json</c>, resolving every texture key to the file its content hash
        /// named.<br/>
        /// （寫出 <c>materials.json</c>，並將每個貼圖索引鍵解析為其內容雜湊所命名的檔案。）
        /// </summary>
        /// <param name="run">The finished run.（已完成的工作。）</param>
        /// <returns>The number of materials written.（寫出的材質數量。）</returns>
        private int WriteTable(MaterialRun run)
        {
            Dictionary<string, object> table = new(run.Materials.Count);
            foreach (KeyValuePair<string, MaterialAsset> entry in run.Materials)
                table[entry.Key] = entry.Value.ToJson(run.Textures.Resolve);

            string path = IOUtils.CombinePath(run.BundleRoot, "materials.json");

            try
            {
                string directory = System.IO.Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory) && !System.IO.Directory.Exists(directory))
                    System.IO.Directory.CreateDirectory(directory);

                System.IO.File.WriteAllText(path,
                    JsonConvert.SerializeObject(table, Formatting.Indented), new UTF8Encoding(false));
            }
            catch (Exception ex)
            {
                _log.Error($"Could not write materials.json: {ex}");
                return 0;
            }

            return table.Count;
        }

        /// <summary>
        /// A stable, session-independent identity for an asset.
        /// （資產的穩定識別，與執行階段無關。）<br/><br/>
        /// The asset identifier is unique per asset; a runtime-generated asset has none, in which
        /// case its name plus type is the best available and still distinguishes it from every
        /// other asset that has one.<br/>
        /// （資產識別碼於每個資產上唯一；執行階段產生的資產沒有識別碼，
        /// 此時以名稱加型別為最佳可得者，且仍能與所有具識別碼的資產區分開來。）
        /// </summary>
        /// <param name="asset">The asset.（資產。）</param>
        /// <returns>The identity string.（識別字串。）</returns>
        private static string Identity(AssetData asset)
        {
            try
            {
                string identifier = asset.identifier;
                if (!string.IsNullOrEmpty(identifier)) return identifier;
            }
            catch (Exception)
            {
                // An asset with no backing file throws rather than returning null.
                // （沒有實體檔案的資產會擲出例外而非回傳 null。）
            }

            return $"{asset.GetType().Name}:{asset.name}";
        }

        /// <summary>
        /// A short, stable digest, matching <see cref="MeshRipSystem"/>'s so that both layers
        /// disambiguate the same way.<br/>
        /// （簡短且穩定的摘要，與 <see cref="MeshRipSystem"/> 一致，使兩個層級以相同方式消除歧義。）
        /// </summary>
        /// <param name="value">The string to digest.（待摘要的字串。）</param>
        /// <returns>Eight lowercase hex digits.（八位小寫十六進位數字。）</returns>
        private static string Digest(string value)
        {
            uint hash = 2166136261u;
            foreach (char c in value)
            {
                hash ^= c;
                hash *= 16777619u;
            }

            return hash.ToString("x8");
        }
    }
}
