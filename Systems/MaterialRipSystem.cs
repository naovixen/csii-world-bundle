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
using System.IO;
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
    /// <c>_EmissiveColorMap</c>, <c>_ControlMask</c>). Those five maps plus a handful of scalars are
    /// exactly the model the bundle contract froze in §7, so the reduction is mostly a lookup — the
    /// work is in getting the pixels out.<br/>
    /// （CS2 的 <see cref="RenderPrefab"/> 於每個材質槽位持有一個 <see cref="SurfaceAsset"/>，
    /// 而表面資產本身是一個屬性袋：以 HDRP 風格名稱為鍵的貼圖、顏色、浮點數與著色器關鍵字字典。
    /// 那五張貼圖加上少數純量恰為束契約 §7 所凍結的模型，因此化約大抵只是查表——真正的工作在於取出像素。）<br/><br/>
    /// <b>Note that not every map name ends in <c>Map</c>.</b> <c>_ControlMask</c> is the colour
    /// mask, and it went unnoticed because every sweep for CS2's texture properties had been
    /// written as a search for <c>_*Map</c> — so <c>colors.json</c> was built and shipped with a
    /// palette nothing could apply. See <see cref="MaterialMap.ColorMask"/>.<br/>
    /// （<b>並非每個貼圖名稱都以 <c>Map</c> 結尾。</b><c>_ControlMask</c> 即顏色遮罩，
    /// 而它之所以未被察覺，是因為歷來搜尋 CS2 貼圖屬性的方式皆寫成尋找 <c>_*Map</c>——
    /// 以致 <c>colors.json</c> 建構並出貨時，帶著一份無從套用的色盤。）<br/><br/>
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
        /// The colour mask. Not <c>*Map</c>-suffixed, which is exactly why it was missed.
        /// （顏色遮罩。其名稱不以 <c>*Map</c> 結尾，這正是它先前被遺漏的原因。）
        /// </summary>
        private const string ControlMask = "_ControlMask";

        /// <summary>
        /// The same five names in the contract's map order, for the passes that have to sweep every
        /// map rather than ask for one.<br/>
        /// （契約貼圖順序下的同樣五個名稱，供須遍歷所有貼圖而非索取單一貼圖的流程使用。）
        /// </summary>
        private static readonly string[] Maps = { BaseColorMap, NormalMap, MaskMap, EmissiveColorMap, ControlMask };

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
            /// Materials that came out with a colour mask.（產出時帶有顏色遮罩的材質數。）<br/><br/>
            /// Reported rather than assumed: if this is zero on a real city then the colour masks
            /// are not where this code looks for them, and <c>colors.json</c> is still decoration.
            /// A count is the difference between knowing that and believing it.<br/>
            /// （此數值須回報而非臆測：若於真實城市中為零，則顏色遮罩並不在此處所查找之處，
            /// 而 <c>colors.json</c> 仍只是裝飾。有無計數，正是「確知」與「相信」之別。）
            /// </summary>
            public int ColorMasks;

            /// <summary>
            /// Every shader property the material library says a surface decode binds, across the
            /// whole run.（整次工作中，材質庫所稱各表面解碼時會繫結的所有著色器屬性。）<br/><br/>
            /// The game's own answer to "what maps are there", so the next one does not need an
            /// offline sweep — and so a map outside the contract's five is visible in the report
            /// instead of silently unexported.<br/>
            /// （此即遊戲對「究竟有哪些貼圖」的自述，使下一次無須離線掃描；
            /// 亦使契約五張之外的貼圖顯現於報告中，而非默默地未被輸出。）
            /// </summary>
            public HashSet<string> TemplateProperties = new();

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

            /// <summary>Virtual-textured maps recovered from their preprocessed stack.（自預處理堆疊救回的虛擬貼圖數。）</summary>
            public int TexturesFromStack;

            /// <summary>Virtual-textured maps recovered from the surface's own VT data.（自表面自身 VT 資料救回的虛擬貼圖數。）</summary>
            public int TexturesFromSurfaceData;

            /// <summary>Surfaces whose own VT data was opened, and those whose load threw.（已開啟自身 VT 資料的表面數，以及載入時擲出例外者。）</summary>
            public int SurfaceDataLoads, SurfaceDataFailures;

            /// <summary>Surfaces skipped because a previous rip's crash marker names them.（因前次擷取的當機標記指名而跳過的表面數。）</summary>
            public int SurfaceDataGuarded;

            /// <summary>The surfaces <c>vt_crash_guard.txt</c> lists as having crashed the game.（<c>vt_crash_guard.txt</c> 所列曾使遊戲當機的表面。）</summary>
            public HashSet<string> CrashGuard = new();

            /// <summary>Maps recovered off a live material, once every other route failed.（其餘路徑皆失敗後，自實際材質救回的貼圖數。）</summary>
            public int TexturesFromMaterial;

            /// <summary>How many stack recoveries have been described in the log.（已於記錄中描述的堆疊救回數。）</summary>
            public int StackReports;

            /// <summary>How many surface-data recoveries have been described in the log.（已於記錄中描述的表面資料救回數。）</summary>
            public int SurfaceDataReports;

            /// <summary>
            /// Texture references already counted.（已計數的貼圖參照。）<br/><br/>
            /// Locating runs once per map slot, not once per texture, so the buckets have to be
            /// told apart from the slots that led to them — otherwise they report the number of
            /// times a texture was asked for rather than the number of textures.<br/>
            /// （定位是每個貼圖槽位執行一次，而非每張貼圖一次，因此各類別必須與導向它們的槽位區分開來，
            /// 否則所回報的將是貼圖被索取的次數，而非貼圖的數量。）
            /// </summary>
            public HashSet<string> Counted = new();

            /// <summary>How long the main thread spent reading pixels back.（主執行緒回讀像素所花的時間。）</summary>
            public double ReadbackMilliseconds;

            /// <summary>How many material ids needed a suffix to stay distinct.（為維持相異而需加上後綴的材質 ID 數量。）</summary>
            public int Disambiguated;

            /// <summary>Everything that produced no entry, and why.（所有未產出條目者及其原因。）</summary>
            public List<RipWarning> Skipped = new();

            /// <summary>
            /// Texture keys already declared missing.（已宣告為缺失的貼圖索引鍵。）<br/><br/>
            /// Locating a map runs once per surface that references it, but a gap is a property of
            /// the texture, not of each surface that wanted it — without this a map shared by two
            /// hundred surfaces would be declared two hundred times.<br/>
            /// （定位貼圖的動作會在每個參照它的表面上各執行一次，但缺口是貼圖本身的性質，
            /// 而非每個需要它的表面的性質；若無此集合，由兩百個表面共用的貼圖將被宣告兩百次。）
            /// </summary>
            public HashSet<string> Declared = new();
        }

        /// <summary>
        /// One surface's own virtual-texturing data, opened at most once and only if something
        /// needs it.<br/>
        /// （單一表面自身的虛擬貼圖資料，至多開啟一次，且僅於確有需要時開啟。）<br/><br/>
        /// <see cref="VTSurfaceAsset.Load"/> decodes the whole file — every stack and layer — and
        /// memoises the material it builds, so four map lookups cost one decode. That is also why
        /// this is deferred: of the 2,447 surfaces that ship a <c>.VTSurface</c>, 2,228 are already
        /// served by the shared stack, and their files run to tens of GiB in total. Opening one
        /// before the cheaper routes have failed would be paying the whole cost for nothing.<br/>
        /// （<see cref="VTSurfaceAsset.Load"/> 會解碼整個檔案——所有堆疊與層——並記憶其所建之材質，
        /// 故四次貼圖查找僅需一次解碼。這也正是其延後載入的原因：隨遊戲附帶 <c>.VTSurface</c>
        /// 的 2,447 個表面中，有 2,228 個已由共用堆疊供應，而這些檔案總計達數十 GiB。
        /// 在較省的路徑尚未失敗前就開啟其一，等於白付全部代價。）
        /// </summary>
        private class SurfaceData
        {
            /// <summary>The surface being ripped.（正在擷取的表面資產。）</summary>
            public SurfaceAsset Surface;

            /// <summary>Whether the load has been tried, successfully or not.（是否已嘗試載入，無論成敗。）</summary>
            public bool Attempted;

            /// <summary>The material the decode produced, with its maps bound.（解碼所產生、已繫結貼圖的材質。）</summary>
            public Material Material;
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

            LoadCrashGuard(_run);
        }

        /// <summary>The list of surfaces whose VT decode has crashed the game; survives restarts.（VT 解碼曾使遊戲當機的表面清單；重啟後仍保留。）</summary>
        private static string GuardPath => IOUtils.CombinePath(Instance.CartoDataPath, "vt_crash_guard.txt");

        /// <summary>The marker naming the surface whose VT decode is in flight right now.（指名此刻正在解碼 VT 的表面的標記檔。）</summary>
        private static string InflightPath => IOUtils.CombinePath(Instance.CartoDataPath, "vt_inflight.txt");

        /// <summary>
        /// Arm the crash fence around <c>VTSurfaceAsset.Load</c>.
        /// （為 <c>VTSurfaceAsset.Load</c> 設置當機防護。）<br/><br/>
        /// The game's decoder (<c>VTSurfaceDataHandler.FillDebugMaterial</c>) slices its
        /// decompressed tile stream by header-derived offsets with no bounds checks in the release
        /// build, so a header/stream mismatch is a <b>native</b> crash — no managed catch ever
        /// sees it, and one bad surface ends the whole rip and the game with it. The fence turns
        /// that into evidence: a marker file names each surface before its decode and is deleted
        /// after, so a marker still on disk at the next rip <i>is</i> the culprit, gets appended
        /// to the guard list, and is never decoded again — its maps fall through to the
        /// live-material route and the skip is declared in the report and warnings.
        /// （遊戲的解碼器以標頭推得的偏移切割解壓後的圖磚串流，發行版中毫無邊界檢查，
        /// 故標頭與串流不符即為<b>原生</b>當機——受管的 catch 永遠看不見，一個壞表面便終結整次擷取
        /// 連同遊戲。此防護將其化為證據：標記檔於每次解碼前指名該表面、成功後刪除；
        /// 下次擷取時仍在磁碟上的標記<i>即為</i>元兇，被附入防護清單後不再解碼——
        /// 其貼圖改走實際材質路徑，且跳過之事於報告與警告中言明。）
        /// </summary>
        /// <param name="run">The run to arm.（要設置防護的工作。）</param>
        private static void LoadCrashGuard(MaterialRun run)
        {
            try
            {
                if (File.Exists(InflightPath))
                {
                    string victim = File.ReadAllText(InflightPath).Trim();
                    File.Delete(InflightPath);
                    if (victim.Length > 0)
                    {
                        _log.Warn($"A previous rip crashed while decoding VT surface data for `{victim}`; "
                                + $"it is now on the skip list at {GuardPath}.");
                        File.AppendAllText(GuardPath, victim + Environment.NewLine);
                    }
                }

                if (File.Exists(GuardPath))
                {
                    foreach (string line in File.ReadAllLines(GuardPath))
                    {
                        if (line.Trim().Length > 0) run.CrashGuard.Add(line.Trim());
                    }
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Crash guard unavailable: {ex.Message}");
            }
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
                string materialId = Rip(run, surface, identity, renderPrefab, materialIndex);
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
        private string Rip(MaterialRun run, SurfaceAsset surface, string identity, RenderPrefab renderPrefab, int materialIndex)
        {
            // The property dictionaries are empty until the asset's data is read off disk. The flag
            // is not "load more" — it lands in `m_UsingVT = m_IsVTMaterial && flag`, so passing
            // false tells a virtual-textured surface it is not one, which is a state the game never
            // puts it in and which `IsHandledByVirtualTexturing` then reads back.
            // （在資產資料自磁碟讀入前，屬性字典皆為空。此旗標並非「載入更多」之意：
            //   它最終落入 `m_UsingVT = m_IsVTMaterial && flag`，因此傳入 false 等於告訴
            //   虛擬貼圖表面它並非虛擬貼圖——遊戲從不使其處於此狀態，
            //   而 `IsHandledByVirtualTexturing` 隨後又會讀回該值。）
            surface.LoadProperties(true);

            // Shared by all four map lookups so that a surface which needs its own VT data decodes
            // it once instead of four times. Nothing is opened unless a map actually asks.
            // （由四次貼圖查找共用，使需要自身 VT 資料的表面僅解碼一次而非四次。
            //   除非確有貼圖索取，否則不會開啟任何檔案。）
            SurfaceData own = new() { Surface = surface };

            try
            {
                MaterialAsset material = new()
                {
                    MaterialId = AssignId(run, surface, identity),
                    SourceName = surface.name
                };

                material.MapKeys[(int)MaterialMap.BaseColor] = Take(run, surface, BaseColorMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Normal] = Take(run, surface, NormalMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Mask] = Take(run, surface, MaskMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.Emissive] = Take(run, surface, EmissiveColorMap, renderPrefab, materialIndex, own);
                material.MapKeys[(int)MaterialMap.ColorMask] = Take(run, surface, ControlMask, renderPrefab, materialIndex, own);

                if (!string.IsNullOrEmpty(material.MapKeys[(int)MaterialMap.ColorMask])) run.ColorMasks++;

                // What the material library says this surface's decode binds, recorded so that
                // "is there a map we are still not exporting?" is answered by the run's own report
                // rather than by another offline sweep. The colour mask was missing because that
                // question had only ever been put to a regex, never to the game.
                // （記錄材質庫所稱此表面解碼時會繫結的屬性，
                //   使「是否仍有未輸出的貼圖？」由本次工作的報告回答，而非再一次離線掃描。
                //   顏色遮罩之所以缺席，正因為此問題向來只問過正規表示式，從未問過遊戲本身。）
                DeclaredProperties(run, surface);

                if (surface.TryGetColorProperty("_BaseColor", out Color tint))
                    material.Tint = new Unity.Mathematics.float4(tint.r, tint.g, tint.b, tint.a);

                if (surface.TryGetColorProperty("_EmissiveColor", out Color emissive))
                    material.EmissiveFactor = new Unity.Mathematics.float3(emissive.r, emissive.g, emissive.b);

                material.AlphaMode = AlphaMode(surface);
                if (surface.TryGetFloatProperty("_AlphaCutoff", out float cutoff))
                {
                    material.AlphaCutoff = cutoff;
                }
                else if (material.AlphaMode == "mask")
                {
                    // A surface that clips through its template alone carries no cutoff of its
                    // own either — the leaf surfaces serialise only wind floats — so the value
                    // the game actually tests against is the template's.
                    // （僅經模板裁切的表面同樣不帶自身門檻——樹葉表面只序列化風的浮點數——
                    //   遊戲實際比對的數值即為模板之值。）
                    try
                    {
                        UnityEngine.Material template = surface.GetTemplateMaterial();
                        if ((template != null) && template.HasProperty("_AlphaCutoff"))
                            material.AlphaCutoff = template.GetFloat("_AlphaCutoff");
                    }
                    catch (Exception) { /* glTF's own default of 0.5 applies */ }
                }

                DescribeTemplate(surface, material);

                // A decal is alpha compositing by definition — the shader projects it over
                // whatever is beneath and blends by the base colour's alpha — but decal shaders
                // carry neither of the lit-shader keywords the sweep above reads, so the mode
                // comes out `opaque` and a consumer renders the marking's whole texture rectangle
                // as a solid box. The template names the truth the keywords cannot.
                // （貼花的本質即為 alpha 合成——著色器將其投影於下方表面並依基礎色的 alpha 混合——
                //   但貼花著色器不帶上述掃描所讀的兩個標準關鍵字，模式因而判為 `opaque`，
                //   消費端便將標線的整個貼圖矩形繪成實心方塊。關鍵字說不出的真相，模板名稱說得出。）
                if ((material.AlphaMode == "opaque") && material.Flags.Contains("decal"))
                    material.AlphaMode = "blend";

                run.Materials[material.MaterialId] = material;
                return material.MaterialId;
            }
            finally
            {
                Release(run, own);

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
        private string Take(MaterialRun run, SurfaceAsset surface, string property, RenderPrefab renderPrefab,
                            int materialIndex, SurfaceData own)
        {
            if (!surface.TryGetTexture(property, out TextureAsset texture) || (texture == null)) return null;

            // Locate before claiming. For a virtual-textured map the identity that matters is the
            // preprocessed texture's, not the stub's — two surfaces sharing a packed layer hold
            // different stubs pointing at the same pixels, and keying on the stub reads those
            // pixels once per surface. A full-city rip made that concrete: 1,020 readbacks
            // produced 321 distinct images, so two thirds of the most expensive work in the phase
            // was spent re-decoding textures already in hand.
            // （先定位再取用。虛擬貼圖真正重要的識別是預處理貼圖的識別，而非殘根的：
            //   共用同一封裝層的兩個表面持有指向相同像素的不同殘根，以殘根為鍵將使該像素
            //   每個表面各讀一次。全城擷取已具體證實：1,020 次回讀僅產生 321 張相異影像，
            //   此階段最昂貴的工作有三分之二耗費在重新解碼早已在手的貼圖上。）
            TextureSource source = Locate(run, surface, property, texture, renderPrefab, materialIndex, own);
            if (source.Key == null) return null;

            // A claim that goes on to fail is still worth keeping: it stops every later surface
            // retrying a lookup already known to lose.
            // （即使取用後失敗仍值得保留：可避免其後每個表面重試已知會失敗的查找。）
            if (!run.Textures.Claim(source.Key)) return source.Key;

            Stopwatch clock = Stopwatch.StartNew();
            try
            {
                PendingTexture pending;
                if (source.IsVirtual) pending = ReadBackVirtual(run, source, surface.name, property);
                else if (source.Live != null) pending = ReadBackLive(run, source, surface.name);
                else pending = ReadBack(source.Standalone, source.Key, run.TextureSizeCap);

                if (pending == null) return null;

                // Normal maps only: the slot decides, the packing signature merely confirms.
                // Content sniffing alone was measured to false-positive on five mask maps of a
                // full city (metallic-1, varying smoothness), which reconstruction would corrupt.
                // （僅限法線貼圖：由槽位決定，封裝特徵僅作確認。單憑內容判斷經實測會在全城五張
                //   遮罩貼圖上誤判（金屬度 1、平滑度有變化），重建將破壞其內容。）
                if (property == NormalMap) ReconstructTwoChannelNormal(pending.Pixels);

                run.Enqueue(pending);
                run.TexturesRead++;
                return source.Key;
            }
            catch (Exception ex)
            {
                Skip(run, source.Key, $"{property}: {ex.GetType().Name} {ex.Message}");

                // Log the first few in full and count the rest. A failure that hits every texture
                // in the city writes the same stack trace thousands of times otherwise, which
                // buries whatever else the log had to say.
                // （前數筆完整記錄，其餘僅計數。若某項失敗遍及城市中每張貼圖，
                //   否則將重複寫出同一段呼叫堆疊數千次，掩埋記錄檔中其他訊息。）
                if (++run.ReadFailures <= 5) _log.Warn($"Failed to read texture `{source.Key}` ({property}): {ex}");
                return null;
            }
            finally
            {
                run.ReadbackMilliseconds += clock.Elapsed.TotalMilliseconds;
            }
        }

        /// <summary>
        /// Where one map's pixels actually live, and the identity they should be deduped under.
        /// （某張貼圖的像素實際所在之處，以及其應據以去重的識別。）
        /// </summary>
        private readonly struct TextureSource
        {
            /// <summary>The dedup key, or <c>null</c> when there are no pixels to be had.（去重索引鍵，若無可取得的像素則為 <c>null</c>。）</summary>
            public string Key { get; }

            /// <summary>The loadable standalone asset, when there is one.（可載入的獨立資產，若存在。）</summary>
            public TextureAsset Standalone { get; }

            /// <summary>The preprocessed texture holding the pixels, when the map is virtual-textured.（貼圖為虛擬貼圖時，持有像素的預處理貼圖。）</summary>
            public VTTextureAsset Packed { get; }

            /// <summary>A texture already bound to a live material.（已繫結至實際材質的貼圖。）</summary>
            public Texture Live { get; }

            /// <summary>The prefab whose materials must be released once <see cref="Live"/> is read.（讀取 <see cref="Live"/> 後須釋放其材質的預製模板。）</summary>
            public RenderPrefab Owner { get; }

            /// <summary>Whether the pixels come out of a VT stack.（像素是否來自 VT 堆疊。）</summary>
            public bool IsVirtual => Packed != null;

            private TextureSource(string key, TextureAsset standalone, VTTextureAsset packed, Texture live, RenderPrefab owner)
            {
                Key = key;
                Standalone = standalone;
                Packed = packed;
                Live = live;
                Owner = owner;
            }

            /// <summary>A map with its own texture asset.（擁有自身貼圖資產的貼圖。）</summary>
            public static TextureSource FromAsset(string key, TextureAsset asset) => new(key, asset, null, null, null);

            /// <summary>A map packed into a virtual-texturing stack.（被封裝進虛擬貼圖堆疊的貼圖。）</summary>
            public static TextureSource FromStack(string key, VTTextureAsset packed) => new(key, null, packed, null, null);

            /// <summary>A map read off a live material.（自實際材質讀取的貼圖。）</summary>
            public static TextureSource FromLive(string key, Texture live, RenderPrefab owner) => new(key, null, null, live, owner);

            /// <summary>No pixels anywhere.（何處皆無像素。）</summary>
            public static TextureSource None => default;
        }

        /// <summary>
        /// Work out where a map's pixels are, trying each route the game itself uses.
        /// （查明貼圖像素之所在，依序嘗試遊戲本身所使用的各條路徑。）<br/><br/>
        /// In order: the asset the surface handed over, if it is loadable; the same guid looked up
        /// in the global database, in case the reference was merely unbound; and failing both, the
        /// preprocessed virtual-texturing stack the map was packed into.<br/>
        /// （依序為：表面所交出的資產（若可載入）；於全域資料庫中查詢相同 guid（以防參照僅是未繫結）；
        /// 若兩者皆不成，則為該貼圖所封裝進的預處理虛擬貼圖堆疊。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="texture">The reference the surface handed over.（表面所交出的參照。）</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.（讀取來源，或 <see cref="TextureSource.None"/>。）</returns>
        private static TextureSource Locate(MaterialRun run, SurfaceAsset surface, string property, TextureAsset texture,
                                            RenderPrefab renderPrefab, int materialIndex, SurfaceData own)
        {
            string key = Identity(texture);

            // Locating happens per map slot, but the buckets describe textures. Count each
            // reference the first time it is seen and no more.
            // （定位是逐貼圖槽位進行，但各類別描述的是貼圖。每個參照僅於首次見到時計數一次。）
            bool first = run.Counted.Add(key);

            if (!IsUnbound(texture))
            {
                if (first) run.TexturesBound++;
                return TextureSource.FromAsset(key, texture);
            }

            // Unbound, but the guid survives. If the standalone texture did ship, this finds it.
            // （雖未繫結，但 guid 仍在。若獨立貼圖確實隨遊戲附帶，此處即可找到。）
            try
            {
                Colossal.Hash128 guid = texture.id.guid;
                if (guid.isValid
                    && AssetDatabase.global.TryGetAsset(guid, out TextureAsset bound)
                    && (bound != null) && !IsUnbound(bound))
                {
                    if (first) run.TexturesRebound++;
                    return TextureSource.FromAsset(key, bound);
                }
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Texture guid lookup failed for `{key}`: {ex.Message}");
            }

            string reason;
            if (IsVirtualTextured(surface, property, texture))
            {
                if (first) run.TexturesVirtual++;

                TextureSource packed = LocateInStack(run, surface, property, key, out reason);
                if (packed.Key != null) return packed;
            }
            else
            {
                if (first) run.TexturesUnbound++;
                reason = $"unbound texture reference ({property}): no asset in the database for this guid";
            }

            // The surface's own virtual-texturing data, which is the other half of the scheme and
            // where the buildings are. Tried after the shared stack because the stack's pixels are
            // shared between surfaces and so dedupe far better, and before the live material
            // because this is the actual source rather than a guess at one.
            // （表面自身的虛擬貼圖資料，此為該機制的另一半，也是建築所在之處。
            //   置於共用堆疊之後，因堆疊的像素為各表面共用、去重效果遠佳；
            //   置於實際材質之前，因為這是真正的來源，而非對來源的猜測。）
            TextureSource inner = LocateInSurfaceData(run, own, property, ref reason);
            if (inner.Key != null) return inner;

            // Last resort: ask the render prefab for the material the game would actually draw
            // with. A surface whose stub does not resolve and whose VT header is empty has still
            // been rendering all along, so if anything is bound to this property at all, this is
            // where it shows up. It is deliberately last — it allocates a Unity material per
            // surface, and every route above is cheaper and more direct.
            // （最後手段：向繪製預製模板索取遊戲實際會用來繪製的材質。殘根無法解析、
            //   且 VT 標頭為空的表面，一直以來仍在正常算繪；因此若該屬性上確實繫結了任何東西，
            //   即會於此顯現。此法刻意置於最後：它會為每個表面配置一個 Unity 材質，
            //   而上述每條路徑都更省且更直接。）
            TextureSource live = LocateInMaterial(run, renderPrefab, materialIndex, property, key);
            if (live.Key != null) return live;

            Skip(run, key, reason);
            return TextureSource.None;
        }

        /// <summary>
        /// Read a map straight off the Unity material the render prefab would draw with.
        /// （直接自繪製預製模板將用以繪製的 Unity 材質讀取貼圖。）<br/><br/>
        /// This asks the game the question at its own level — "what texture is bound to this
        /// property?" — instead of reconstructing the answer from asset records. It is the only
        /// route left for surfaces that have neither a loadable texture asset nor preprocessed
        /// virtual-texturing data, and it costs a material instantiation, so it runs only after
        /// everything else has failed.<br/>
        /// （此法以遊戲自身的層級提問——「此屬性上繫結了什麼貼圖？」——而非自資產紀錄重建答案。
        /// 對既無可載入貼圖資產、亦無預處理虛擬貼圖資料的表面而言，這是僅存的路徑；
        /// 其代價為實體化一個材質，故僅於其他一切皆失敗後才執行。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="renderPrefab">The render prefab owning the material.（擁有該材質的繪製預製模板。）</param>
        /// <param name="materialIndex">The material slot.（材質槽位。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="key">The texture key.（貼圖索引鍵。）</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.（讀取來源，或 <see cref="TextureSource.None"/>。）</returns>
        private static TextureSource LocateInMaterial(MaterialRun run, RenderPrefab renderPrefab, int materialIndex,
                                                      string property, string key)
        {
            if (renderPrefab == null) return TextureSource.None;

            try
            {
                Material material = renderPrefab.ObtainMaterial(materialIndex, false);
                if ((material == null) || !material.HasProperty(property)) return TextureSource.None;

                Texture bound = material.GetTexture(property);
                if (bound == null) return TextureSource.None;

                run.TexturesFromMaterial++;
                return TextureSource.FromLive(key, bound, renderPrefab);
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Material lookup failed for `{key}` ({property}): {ex.Message}");
                return TextureSource.None;
            }
        }

        /// <summary>
        /// Find the preprocessed texture a virtual-textured map was packed into.
        /// （找出虛擬貼圖所封裝進的預處理貼圖。）<br/><br/>
        /// The material template says where: <c>GetStackConfigIndex</c> and
        /// <c>GetLayerIndexInStack</c> turn a shader property name into the stack and layer its map
        /// occupies, and the surface's VT header holds a guid per slot.<br/>
        /// （材質模板指出其位置：<c>GetStackConfigIndex</c> 與 <c>GetLayerIndexInStack</c>
        /// 將著色器屬性名稱轉為其貼圖所占的堆疊與層，而表面的 VT 標頭於每個槽位持有一個 guid。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="key">The stub's key, for reporting.（殘根的索引鍵，供回報使用。）</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.（讀取來源，或 <see cref="TextureSource.None"/>。）</returns>
        private static TextureSource LocateInStack(MaterialRun run, SurfaceAsset surface, string property, string key, out string reason)
        {
            try
            {
                // Qualified through MaterialLibrary: the bundle's own MaterialDescription is a
                // different type with the same short name, and Carto.Domain is in scope here.
                // （以 MaterialLibrary 限定：束自身的 MaterialDescription 是同名的另一型別，
                //   且 Carto.Domain 於此處在作用範圍內。）
                MaterialLibrary.MaterialDescription description = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash);

                if (description == null)
                {
                    reason = $"virtual-textured ({property}): no material description for template {surface.materialTemplateHash}";
                    return TextureSource.None;
                }

                int stack = description.GetStackConfigIndex(property);
                int layer = (stack < 0) ? -1 : description.GetLayerIndexInStack(stack, property);
                if ((stack < 0) || (layer < 0))
                {
                    reason = $"virtual-textured ({property}): the material template packs no such map";
                    return TextureSource.None;
                }

                Colossal.Hash128 guid = PreProcessedGuid(surface, stack, layer);
                if (!guid.isValid)
                {
                    // These coordinates are not wrong, and neither is the empty header: this
                    // surface simply keeps its pixels in the other place. CS2 has two
                    // virtual-texturing schemes, and they are very nearly complementary — of the
                    // 5,832 surfaces the game ships, 2,014 use the shared preprocessed stack read
                    // above and 2,233 use a `.VTSurface` file of their own with a zeroed stack
                    // header. The second set is mostly buildings. `LocateInSurfaceData` reads it,
                    // so falling through here is a route change rather than a dead end.
                    // （此座標並無錯誤，標頭為空亦然：此表面只是將像素存放於另一處。
                    //   CS2 有兩套虛擬貼圖機制，且兩者幾近互補——遊戲所附的 5,832 個表面中，
                    //   2,014 個使用上方讀取的共用預處理堆疊，2,233 個則使用自身的 `.VTSurface`
                    //   檔案而堆疊標頭為零。後者多為建築。`LocateInSurfaceData` 會讀取之，
                    //   故自此落下是換路而非絕路。）
                    reason = $"virtual-textured ({property}): no preprocessed VT data, and the "
                           + "surface's own VT data could not be read either";
                    return TextureSource.None;
                }

                if (!AssetDatabase.global.TryGetAsset(guid, out VTTextureAsset packed) || (packed == null))
                {
                    reason = $"virtual-textured ({property}): preprocessed texture {guid} is not in the database";
                    return TextureSource.None;
                }

                // Keyed by the preprocessed texture, so surfaces sharing a packed layer share the
                // readback rather than each paying for it.
                // （以預處理貼圖為鍵，使共用同一封裝層的表面共享回讀，而非各自付出代價。）
                reason = null;
                return TextureSource.FromStack($"vt:{guid}", packed);
            }
            catch (Exception ex)
            {
                reason = $"virtual-textured ({property}): {ex.GetType().Name} {ex.Message}";
                if (run.BindFailures++ < 5) _log.Warn($"VT stack lookup failed for `{key}` ({property}): {ex}");
                return TextureSource.None;
            }
        }

        /// <summary>
        /// The guid of the preprocessed texture at one stack and layer of a surface's VT header.
        /// （表面 VT 標頭中，某堆疊與某層之預處理貼圖的 guid。）<br/><br/>
        /// <b>Do not replace this with <c>SurfaceAsset.GetPreProcessedTextureGuid(stack, layer)</c>,
        /// which is the obvious call and is wrong.</b> That method bounds-checks both arguments and
        /// then indexes the header with <c>stackIndex * layerIndex</c> where the flat index is
        /// <c>stackIndex * 4 + layerIndex</c>. Every coordinate with a zero in it — which is every
        /// coordinate a one-stack surface can produce, and layer 0 of any stack — therefore returns
        /// slot 0. Since stack 0 layer 0 is the base colour, asking for a surface's normal or mask
        /// hands back its base colour instead, and the caller cannot tell: the guid is valid, the
        /// asset loads, and the pixels are a real texture. A full-city rip wrote the base colour
        /// into the normal and mask of 1,428 of 1,926 materials this way, and the only visible
        /// symptom was a suspiciously high dedupe rate.<br/>
        /// （<b>切勿改用 <c>SurfaceAsset.GetPreProcessedTextureGuid(stack, layer)</c>——
        /// 該呼叫看似理所當然，實則有誤。</b>該方法對兩個引數做邊界檢查後，
        /// 以 <c>stackIndex * layerIndex</c> 索引標頭，而正確的平坦索引為
        /// <c>stackIndex * 4 + layerIndex</c>。因此凡含有零的座標——單堆疊表面所能產生的全部座標，
        /// 以及任何堆疊的第 0 層——皆會回傳槽位 0。由於堆疊 0 層 0 是基礎色，
        /// 索取表面的法線或遮罩時得到的其實是其基礎色，而呼叫端無從察覺：
        /// guid 有效、資產可載入、像素也是一張真實的貼圖。
        /// 一次全城擷取即以此方式將基礎色寫入 1,926 個材質中 1,428 個的法線與遮罩，
        /// 唯一可見的徵兆是高得可疑的去重率。）<br/><br/>
        /// The enumerator does not share the fault — it walks <c>stackCount * 4</c> slots by flat
        /// index, in order and unfiltered — so counting into it gives the value the accessor should
        /// have returned. At most eight slots, so the walk is not worth avoiding.<br/>
        /// （列舉子並無此缺陷——它以平坦索引依序走訪 <c>stackCount * 4</c> 個槽位且不作過濾——
        /// 故對其計數即可取得該存取子本應回傳的值。至多八個槽位，此走訪不值得規避。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="stack">The stack index.（堆疊索引。）</param>
        /// <param name="layer">The layer index within that stack.（該堆疊內的層索引。）</param>
        /// <returns>The guid, or an invalid one when the slot is past the end.（guid，若槽位超出範圍則為無效值。）</returns>
        private static Colossal.Hash128 PreProcessedGuid(SurfaceAsset surface, int stack, int layer)
        {
            int target = (stack * 4) + layer;
            int index = 0;

            foreach (Colossal.Hash128 guid in surface.preProcessedTextureGuids)
                if (index++ == target) return guid;

            return default;
        }

        /// <summary>
        /// Read a virtual-textured map out of the surface's own preprocessed data.
        /// （自表面自身的預處理資料中讀出虛擬貼圖。）<br/><br/>
        /// A <see cref="SurfaceAsset"/> can hold its virtual-texturing data in either of two
        /// places. <see cref="LocateInStack"/> covers the first: layers packed into
        /// <see cref="VTTextureAsset"/>s shared between every surface that uses them. The second is
        /// a <c>.VTSurface</c> file belonging to this surface alone, resolved when the surface's
        /// header is read and reached through <c>hasVTSurfaceAsset</c> / <c>vtSurfaceAsset</c>. A
        /// surface using the second has a zeroed stack header, which is why it looks empty-handed
        /// from the first route's point of view.<br/>
        /// （<see cref="SurfaceAsset"/> 的虛擬貼圖資料可存放於兩處之一。
        /// <see cref="LocateInStack"/> 涵蓋第一處：封裝進 <see cref="VTTextureAsset"/>、
        /// 由所有使用它的表面共用的層。第二處則是專屬於此表面的 <c>.VTSurface</c> 檔案，
        /// 於讀取表面標頭時解析，並經 <c>hasVTSurfaceAsset</c> / <c>vtSurfaceAsset</c> 取得。
        /// 使用第二處的表面其堆疊標頭為零，這正是它從第一條路徑看來兩手空空的緣故。）<br/><br/>
        /// <c>VTSurfaceAsset.Load</c> does the whole job: it reassembles the file's zstd tiles,
        /// instantiates the material template the surface names, and binds each decoded layer under
        /// the shader property the template gives it. What comes back is an ordinary Unity material
        /// with ordinary textures on it, so this route ends where every other one does.<br/>
        /// （<c>VTSurfaceAsset.Load</c> 一手包辦：重組檔案的 zstd 圖磚、實體化表面所指名的材質模板，
        /// 並將每個已解碼的層繫結至模板所賦予的著色器屬性。回傳者是持有一般貼圖的一般 Unity 材質，
        /// 故此路徑的終點與其他路徑相同。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="own">The surface's own VT data, opened on demand.（表面自身的 VT 資料，按需開啟。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="reason">Replaced only when this route is the one that failed.（僅於此路徑即為失敗者時才取代。）</param>
        /// <returns>Where to read from, or <see cref="TextureSource.None"/>.（讀取來源，或 <see cref="TextureSource.None"/>。）</returns>
        private static TextureSource LocateInSurfaceData(MaterialRun run, SurfaceData own, string property, ref string reason)
        {
            if ((own?.Surface == null) || !own.Surface.hasVTSurfaceAsset) return TextureSource.None;

            Material material = Obtain(run, own);
            if (material == null)
            {
                reason = $"virtual-textured ({property}): the surface's own VT data could not be decoded";
                return TextureSource.None;
            }

            if (!material.HasProperty(property)) return TextureSource.None;

            Texture bound = material.GetTexture(property);
            if (bound == null) return TextureSource.None;

            run.TexturesFromSurfaceData++;

            // Keyed by the VT data's own guid, which is the opposite of the rule the shared stack
            // follows: these pixels belong to one surface and are not shared with any other, so
            // keying them any more coarsely would make two surfaces collide on one image.
            // （以該 VT 資料自身的 guid 為鍵，與共用堆疊所循的規則相反：
            //   這些像素屬於單一表面、不與其他表面共用，若鍵取得更粗略將使兩個表面撞上同一張影像。）
            string key = $"vts:{own.Surface.vtSurfaceAsset.id.guid}:{property}";

            if (run.SurfaceDataReports++ < 4)
                _log.Info($"VT surface recovery [{run.SurfaceDataReports}] `{own.Surface.name}` {property}: "
                        + $"{bound.width}x{bound.height} {bound.graphicsFormat}");

            return TextureSource.FromLive(key, bound, null);
        }

        /// <summary>
        /// Open a surface's own virtual-texturing data, at most once per surface.
        /// （開啟表面自身的虛擬貼圖資料，每個表面至多一次。）<br/><br/>
        /// The attempt is recorded rather than the result, so a surface whose data will not decode
        /// is not asked again for each of its remaining maps.<br/>
        /// （記錄的是「已嘗試」而非結果，使資料無法解碼的表面不會在其餘每張貼圖上被重複詢問。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="own">The surface's own VT data.（表面自身的 VT 資料。）</param>
        /// <returns>The decoded material, or <c>null</c>.（已解碼的材質，或 <c>null</c>。）</returns>
        private static Material Obtain(MaterialRun run, SurfaceData own)
        {
            if (own.Attempted) return own.Material;
            own.Attempted = true;

            // The decode is a native-crash risk (see LoadCrashGuard), so the fence brackets it:
            // skip surfaces a previous crash convicted, and name the one in flight on disk so
            // that a crash here convicts it for the next run.
            // （解碼具原生當機風險（見 LoadCrashGuard），故防護將其夾住：跳過前次當機定罪的表面，
            //   並將進行中的表面指名於磁碟，使此處的當機能為下次執行定罪。）
            string marker = $"{own.Surface.name} ({own.Surface.vtSurfaceAsset?.id.guid})";
            if (run.CrashGuard.Contains(marker))
            {
                run.SurfaceDataGuarded++;
                run.Skipped.Add(new RipWarning("texture", own.Surface.name,
                    "own VT data not decoded: it crashed the game in a previous rip (vt_crash_guard.txt); "
                    + "maps fall back to the live material where possible"));
                return null;
            }

            try
            {
                run.SurfaceDataLoads++;
                try { File.WriteAllText(InflightPath, marker); } catch { /* fence unavailable; proceed */ }
                own.Material = own.Surface.vtSurfaceAsset.Load();
            }
            catch (Exception ex)
            {
                run.SurfaceDataFailures++;
                if (run.BindFailures++ < 5)
                    _log.Warn($"VT surface data failed to load for `{own.Surface.name}`: {ex}");
            }
            finally
            {
                // Reached on success and on managed failure alike; only a native crash leaves the
                // marker behind, which is precisely the signal it exists to give.
                // （成功與受管失敗皆會到達此處；唯有原生當機會留下標記——這正是其存在要給的訊號。）
                try { File.Delete(InflightPath); } catch { /* nothing useful to do */ }
            }

            return own.Material;
        }

        /// <summary>
        /// Close a surface's own virtual-texturing data and destroy what decoding it created.
        /// （關閉表面自身的虛擬貼圖資料，並銷毀解碼過程所建立之物。）<br/><br/>
        /// <c>VTSurfaceAsset.Unload</c> destroys the material and nothing else, but the decode also
        /// built a texture per layer, each held only by that material and marked so that Unity will
        /// not collect it. Left alone they accumulate for the whole rip: a city's worth of building
        /// surfaces at up to eight layers apiece, several megabytes each, is measured in gigabytes
        /// and ends the run long before the city does. So the textures are taken off the material
        /// and destroyed here, after their pixels have been read.<br/>
        /// （<c>VTSurfaceAsset.Unload</c> 僅銷毀材質，別無其他；但解碼過程另為每一層建立了貼圖，
        /// 各自僅由該材質持有，且經標記使 Unity 不會回收之。若置之不理，它們將於整次擷取中不斷累積：
        /// 一座城市的建築表面，每個至多八層、每層數 MB，總量以 GB 計，
        /// 遠在城市走完之前就會終結該次工作。故於其像素讀取完畢後，
        /// 在此將貼圖自材質上取下並銷毀。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="own">The surface's own VT data.（表面自身的 VT 資料。）</param>
        private static void Release(MaterialRun run, SurfaceData own)
        {
            if ((own == null) || !own.Attempted) return;

            Material material = own.Material;
            own.Material = null;

            try
            {
                if (material != null) foreach (string property in DecodedProperties(own.Surface))
                {
                    if (!material.HasProperty(property)) continue;

                    Texture texture = material.GetTexture(property);
                    if (texture == null) continue;

                    material.SetTexture(property, null);
                    UnityEngine.Object.DestroyImmediate(texture, true);
                }
            }
            catch (Exception ex)
            {
                if (run.BindFailures++ < 5) _log.Warn($"Releasing VT surface data for `{own.Surface?.name}` failed: {ex}");
            }
            finally
            {
                try { own.Surface?.vtSurfaceAsset?.Unload(false); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Record what the material library says this surface's decode binds.
        /// （記錄材質庫所稱此表面解碼時會繫結的屬性。）<br/><br/>
        /// The material template is the authority — it is what the decode itself consults to decide
        /// where each layer goes — so the stacks it declares name every texture the surface has,
        /// including any beyond the five the contract keeps. Accumulating them over a run turns
        /// "is there a map we are still missing?" from an offline question into a line in the
        /// report — which is what the colour mask needed, since that question had only ever been
        /// put to a regex.<br/>
        /// （材質模板即權威——解碼過程本身即據以決定每一層的去向——
        /// 故其所宣告的堆疊指名了該表面所擁有的每一張貼圖，包括契約所保留的五張以外者。
        /// 於整次工作中累積這些名稱，可將「是否仍有遺漏的貼圖？」從離線問題化為報告中的一行——
        /// 這正是顏色遮罩所需者，因為該問題向來只問過正規表示式。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        private static void DeclaredProperties(MaterialRun run, SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // A surface whose template will not resolve says nothing about the others.
                // （模板無法解析的表面，對其他表面不具說明力。）
                return;
            }

            if (stacks != null)
            {
                foreach (MaterialStackProperties stack in stacks)
                {
                    if (stack?.m_PropertyNames == null) continue;
                    foreach (string property in stack.m_PropertyNames)
                        if (!string.IsNullOrEmpty(property)) run.TemplateProperties.Add(property);
                }
            }

            // The stacks only name the virtual-textured maps. A non-VT surface binds whatever is
            // in its own texture dictionary (`SyncToUnityMaterial` walks `m_Textures` directly),
            // so those keys go into the same census — otherwise a map that exists only outside
            // the VT scheme could still ship unnoticed, which is the §4.2 failure shape again.
            // （堆疊僅指名虛擬貼圖。非 VT 表面繫結的是其自身貼圖字典中的內容
            //   （`SyncToUnityMaterial` 直接走訪 `m_Textures`），故該等鍵一併納入普查——
            //   否則僅存在於 VT 機制之外的貼圖仍可能無聲出貨，即 §4.2 失敗型態的重演。）
            try
            {
                foreach (KeyValuePair<string, TextureAsset> texture in surface.textures)
                    if (!string.IsNullOrEmpty(texture.Key)) run.TemplateProperties.Add(texture.Key);
            }
            catch (Exception)
            {
                // A surface whose dictionaries did not load adds nothing.
                // （字典未載入的表面不增添任何內容。）
            }
        }

        /// <summary>
        /// Every shader property the decode could have bound a texture to, so that the sweep in
        /// <see cref="Release"/> destroys all of them and not merely the ones the contract keeps.
        /// If the template cannot be had, the contract's own maps are still better than leaking
        /// every one.
        /// （解碼過程可能繫結貼圖的所有著色器屬性，使 <see cref="Release"/> 中的遍歷能銷毀全部
        /// 而非僅止於契約所保留者。若無法取得模板，則退回契約自身的貼圖清單，仍勝於全數洩漏。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <returns>The property names to sweep.（須遍歷的屬性名稱。）</returns>
        private static IEnumerable<string> DecodedProperties(SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks = null;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // Fall through to the contract's four.（退回契約的四張。）
            }

            if (stacks == null) return Maps;

            List<string> properties = new();
            foreach (MaterialStackProperties stack in stacks)
            {
                if (stack?.m_PropertyNames == null) continue;
                foreach (string property in stack.m_PropertyNames)
                    if (!string.IsNullOrEmpty(property)) properties.Add(property);
            }

            return (properties.Count > 0) ? properties : (IEnumerable<string>)Maps;
        }

        /// <summary>
        /// Whether a map is one the game reads out of a virtual-texturing stack rather than from a
        /// texture asset of its own.<br/>
        /// （某貼圖是否為遊戲自虛擬貼圖堆疊讀取、而非自其專屬貼圖資產讀取者。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="texture">The reference the surface handed over.（表面所交出的參照。）</param>
        /// <returns>Whether virtual texturing owns it.（虛擬貼圖是否擁有它。）</returns>
        private static bool IsVirtualTextured(SurfaceAsset surface, string property, TextureAsset texture)
        {
            try
            {
                return surface.IsHandledByVirtualTexturing(
                    new KeyValuePair<string, TextureAsset>(property, texture));
            }
            catch (Exception)
            {
                // The material library could not say. Unbound-and-unknown is still unbound.
                // （材質庫無法判定。未繫結且原因不明，仍屬未繫結。）
                return false;
            }
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
                return Capture(source, key, asset.name, cap);
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
        /// Read a virtual-textured map out of the preprocessed stack that replaced it.
        /// （自取代虛擬貼圖的預處理堆疊中讀出該貼圖。）<br/><br/>
        /// Most CS2 surfaces have no standalone texture assets: their maps were preprocessed into
        /// virtual-texturing stacks at build time, and what remains in
        /// <c>SurfaceAsset.textures</c> is an unloadable stub. The pixels are still shipped, just
        /// addressed differently — by stack and layer rather than by asset — and the route to them
        /// is entirely public:<br/>
        /// （CS2 多數表面沒有獨立的貼圖資產：其貼圖於建置時已預處理進虛擬貼圖堆疊，
        /// <c>SurfaceAsset.textures</c> 中僅存無法載入的殘根。像素仍隨遊戲附帶，只是定址方式不同
        /// ——以堆疊與層而非以資產定址——且通往它們的路徑完全是公開 API：）<br/><br/>
        /// <c>MaterialDescription.GetStackConfigIndex(property)</c> and
        /// <c>GetLayerIndexInStack(stack, property)</c> turn a shader property name into the
        /// coordinates its map was packed at; <c>SurfaceAsset.GetPreProcessedTextureGuid</c> turns
        /// those into a guid; and the <see cref="VTTextureAsset"/> behind it reassembles its own
        /// tiles into an ordinary <c>Texture</c> when asked. From there it is the same blit and
        /// readback as any other texture.<br/>
        /// （<c>MaterialDescription.GetStackConfigIndex(property)</c> 與
        /// <c>GetLayerIndexInStack(stack, property)</c> 將著色器屬性名稱轉為其貼圖被封裝的座標；
        /// <c>SurfaceAsset.GetPreProcessedTextureGuid</c> 再轉為 guid；
        /// 其背後的 <see cref="VTTextureAsset"/> 受請求時會自行將圖磚重組為一般的 <c>Texture</c>。
        /// 自此之後便與其他貼圖採用相同的 blit 與回讀。）<br/><br/>
        /// Must run on the main thread.（必須於主執行緒執行。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="surface">The surface the map belongs to.（該貼圖所屬的表面資產。）</param>
        /// <param name="property">The CS2 shader property name.（CS2 著色器屬性名稱。）</param>
        /// <param name="key">The texture key.（貼圖索引鍵。）</param>
        /// <returns>The pixels, or <c>null</c> when the stack could not be read.（像素，若無法讀取堆疊則為 <c>null</c>。）</returns>
        private static PendingTexture ReadBackVirtual(MaterialRun run, TextureSource source, string surfaceName, string property)
        {
            VTTextureAsset packed = source.Packed;
            try
            {
                Texture texture = packed.Load();
                PendingTexture pending = Capture(texture, source.Key, surfaceName, run.TextureSizeCap);
                if (pending == null)
                {
                    Skip(run, source.Key, $"virtual-textured ({property}): preprocessed texture yielded no pixels");
                    return null;
                }

                run.TexturesFromStack++;

                // The reassembled texture ought to come out at the size its own header declares.
                // A mismatch would mean the tile layout is leaking through — the one thing about
                // this path that cannot be established without the game running — so say so on the
                // first few rather than let it be discovered as odd-looking output.
                // （重組後的貼圖尺寸理應與其標頭所宣告者相符。若不符，即代表圖磚佈局外洩，
                //   而這正是此路徑中唯一無法在遊戲執行外確認之事，故於最初數筆明確回報，
                //   而非留待日後從古怪的輸出中發現。）
                if (run.StackReports++ < 4)
                    _log.Info($"VT recovery [{run.StackReports}] `{surfaceName}` {property}: "
                            + $"header {packed.width}x{packed.height}, loaded {texture.width}x{texture.height} "
                            + $"{texture.graphicsFormat}, written {pending.Width}x{pending.Height}");

                return pending;
            }
            finally
            {
                try { packed.Unload(false); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Copy a texture that is already bound to a live material.
        /// （複製已繫結至實際材質的貼圖。）<br/><br/>
        /// The texture belongs to the material, not to this rip, so it is read where it sits and
        /// only the prefab's hold on its materials is released afterwards.<br/>
        /// （此貼圖屬於該材質而非本次擷取，故就地讀取，事後僅釋放預製模板對其材質的持有。）<br/><br/>
        /// Must run on the main thread.（必須於主執行緒執行。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="source">The located texture.（已定位的貼圖。）</param>
        /// <param name="surfaceName">A name for the record.（供紀錄使用的名稱。）</param>
        /// <returns>The pixels, or <c>null</c>.（像素，或 <c>null</c>。）</returns>
        private static PendingTexture ReadBackLive(MaterialRun run, TextureSource source, string surfaceName)
        {
            try
            {
                return Capture(source.Live, source.Key, surfaceName, run.TextureSizeCap);
            }
            finally
            {
                try { source.Owner?.ReleaseMaterials(); } catch { /* nothing useful to do */ }
            }
        }

        /// <summary>
        /// Record a texture that produced no file.（記錄未產出檔案的貼圖。）
        /// </summary>
        /// <param name="run">The run in progress.（進行中的工作。）</param>
        /// <param name="key">The texture key.（貼圖索引鍵。）</param>
        /// <param name="reason">Why it produced nothing.（其未產出的原因。）</param>
        private static void Skip(MaterialRun run, string key, string reason)
        {
            if (run.Declared.Add(key)) run.Skipped.Add(new RipWarning("texture", key, reason));
        }

        /// <summary>
        /// Scale a loaded texture to the cap and copy its pixels out as <c>RGBA8</c>.
        /// （將已載入的貼圖縮至上限，並將其像素以 <c>RGBA8</c> 複製出來。）
        /// </summary>
        /// <param name="source">The loaded texture.（已載入的貼圖。）</param>
        /// <param name="key">Its texture key.（其貼圖索引鍵。）</param>
        /// <param name="name">A name for the record.（供紀錄使用的名稱。）</param>
        /// <param name="cap">The longest edge to write, or <c>0</c> for no cap.（寫出的最長邊上限，<c>0</c> 為不限。）</param>
        /// <returns>The pixels, or <c>null</c>.（像素，或 <c>null</c>。）</returns>
        internal static PendingTexture Capture(Texture source, string key, string name, int cap)
        {
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

            // Take the colour space from the loaded texture, never from the asset that produced it.
            // A VTTextureAsset's own `format` field is not populated by reading its header — only
            // by paths that upload through the streaming system — so it reports whatever it last
            // held. Trusting it wrote every normal and mask map through an sRGB render target,
            // gamma-mangling linear data, and the output still rendered: exactly the silent failure
            // §7.1 of the contract warns about. The Texture that came back cannot be stale.
            // （色彩空間取自已載入的貼圖，絕不取自產生它的資產。VTTextureAsset 自身的 `format` 欄位
            //   並非由讀取其標頭所填寫，而僅由經串流系統上傳的路徑填寫，因此它回報的是最後殘留的值。
            //   採信該欄位會使所有法線與遮罩貼圖經 sRGB 繪製目標寫出，扭曲線性資料，
            //   而輸出仍可正常算繪——正是契約 §7.1 所警告的無聲失敗。回傳的 Texture 則不可能陳舊。）
            byte[] pixels = Blit(source, width, height, GraphicsFormatUtility.IsSRGBFormat(source.graphicsFormat));
            if (pixels == null) return null;

            return new PendingTexture
            {
                Key = key,
                SourceName = name,
                Width = width,
                Height = height,
                Pixels = pixels
            };
        }

        /// <summary>
        /// Rebuild a two-channel-packed normal map into a standard tangent normal.
        /// （將雙通道封裝的法線貼圖重建為標準切線空間法線。）<br/><br/>
        /// Unity's DXT5nm/BC3n packing stores X in <b>alpha</b> and Y in green, forcing red to 1
        /// and leaving blue to mirror green; the shader reconstructs Z. Written out raw, a
        /// consumer reading it as an ordinary tangent normal map gets <c>normal.x = +1</c> for
        /// every texel — measured on a full city, every road surface shaded by its compass
        /// bearing, which is what "each road is a different colour" turned out to be. The
        /// packing's signature — red pinned at white while green varies and alpha is not
        /// constant — matches no legitimate colour or mask content (white-with-alpha decals have
        /// green pinned too), so it is detected here, where every readback route converges, and
        /// rebuilt to <c>(x, y, z, 1)</c> exactly as the shader would.
        /// （Unity 的 DXT5nm/BC3n 封裝將 X 存於<b>alpha</b>、Y 存於綠色，紅色強制為 1、
        /// 藍色複寫綠色；Z 由著色器重建。若原樣寫出，消費端視其為一般切線法線時，
        /// 每個像素皆得 <c>normal.x = +1</c>——實測全城，每條道路依其方位角被著色，
        /// 即「每條路顏色不同」的真相。該封裝的特徵——紅色恆為白、綠色有變化、alpha 非定值——
        /// 不會與任何正當的顏色或遮罩內容相符（帶 alpha 的白色貼花其綠色亦恆為白），
        /// 故於所有回讀路徑匯聚之處偵測並重建為 <c>(x, y, z, 1)</c>，與著色器一致。）
        /// </summary>
        /// <param name="pixels">RGBA8 pixels, rebuilt in place.（RGBA8 像素，就地重建。）</param>
        internal static void ReconstructTwoChannelNormal(byte[] pixels)
        {
            // The fraction test rather than a strict minimum: block compression dents a few
            // texels of the constant-white red channel (247–249, and one map to 191), and a
            // strict minimum was measured to miss 235 of 784 packed normals in a full city. A
            // standard three-channel normal has red centred at 128 and passes nowhere near 90 %.
            // （採比例判定而非嚴格最小值：區塊壓縮會使恆為白的紅色通道少數像素凹陷
            //   （247–249，甚至一張至 191），嚴格最小值經實測漏掉全城 784 張封裝法線中的 235 張。
            //   標準三通道法線的紅色以 128 為中心，遠不及 90%。）
            int count = pixels.Length / 4;
            if (count == 0) return;

            int highRed = 0, minA = 255;
            for (int i = 0; i < pixels.Length; i += 4)
            {
                if (pixels[i] >= 250) highRed++;
                if (pixels[i + 3] < minA) minA = pixels[i + 3];
            }

            if ((highRed < (count * 0.9f)) || (minA >= 250)) return;

            for (int i = 0; i < pixels.Length; i += 4)
            {
                float x = (pixels[i + 3] / 255f * 2f) - 1f;
                float y = (pixels[i + 1] / 255f * 2f) - 1f;
                float z = Mathf.Sqrt(Mathf.Max(0f, 1f - (x * x) - (y * y)));

                pixels[i] = (byte)Mathf.RoundToInt(((x * 0.5f) + 0.5f) * 255f);
                pixels[i + 2] = (byte)Mathf.RoundToInt(((z * 0.5f) + 0.5f) * 255f);
                pixels[i + 3] = 255;
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
        /// Read the transparency mode out of the surface's shader keywords, falling back to the
        /// material template the surface is built from.
        /// （自表面資產的著色器關鍵字讀出透明模式，並在必要時回退至其所依據的材質模板。）<br/><br/>
        /// The surface's own keyword set is only half the story: the runtime material is
        /// <c>new Material(template)</c> with the surface's properties overlaid
        /// (<c>SurfaceAsset.Load</c>), so a template that alpha-tests passes clipping to every
        /// surface built from it. Vegetation is the canonical case — the oak leaf surface
        /// serialises no keywords at all; its cutout lives entirely on the template.<br/>
        /// （表面自身的關鍵字只是一半的事實：執行期材質是以模板複製再疊上表面屬性而成，
        /// 故會裁切的模板將裁切傳給由其建構的每個表面。植被即為典型——橡樹樹葉表面
        /// 完全未序列化任何關鍵字，其裁切全在模板之上。）
        /// </summary>
        /// <param name="surface">The surface asset.（表面資產。）</param>
        /// <returns>The contract's alpha mode.（契約所定義的 alpha 模式。）</returns>
        private static string AlphaMode(SurfaceAsset surface)
        {
            // HDRP serialises the switches as floats and `HDMaterial.ValidateMaterial` — which
            // `SurfaceAsset.Load` ends on — re-derives the keywords from them, so a float the
            // surface carries overrides the template outright, in either direction.
            // （HDRP 以浮點數序列化開關，且 `SurfaceAsset.Load` 收尾的
            //   `HDMaterial.ValidateMaterial` 會依浮點數重新推導關鍵字，
            //   故表面所帶的浮點數在兩個方向上都直接凌駕模板。）
            bool surfaceAnswersType = false, surfaceAnswersClip = false;

            try
            {
                if (surface.HasKeyword("_SURFACE_TYPE_TRANSPARENT")) return "blend";
                if (surface.HasKeyword("_ALPHATEST_ON")) return "mask";

                if (surface.TryGetFloatProperty("_SurfaceType", out float surfaceType))
                {
                    surfaceAnswersType = true;
                    if (surfaceType > 0.5f) return "blend";
                }
                if (surface.TryGetFloatProperty("_AlphaCutoffEnable", out float clipEnable))
                {
                    surfaceAnswersClip = true;
                    if (clipEnable > 0.5f) return "mask";
                }
            }
            catch (Exception)
            {
                // A surface whose keywords did not load says nothing about transparency; the
                // template below still can.（關鍵字未載入的表面對透明度未置一詞，
                // 但下方的模板仍可能言明。）
            }

            try
            {
                UnityEngine.Material template = surface.GetTemplateMaterial();
                if (template != null)
                {
                    if (!surfaceAnswersType
                        && (template.IsKeywordEnabled("_SURFACE_TYPE_TRANSPARENT")
                            || (template.HasProperty("_SurfaceType") && (template.GetFloat("_SurfaceType") > 0.5f))))
                        return "blend";

                    if (!surfaceAnswersClip
                        && (template.IsKeywordEnabled("_ALPHATEST_ON")
                            || (template.HasProperty("_AlphaCutoffEnable") && (template.GetFloat("_AlphaCutoffEnable") > 0.5f))))
                        return "mask";
                }
            }
            catch (Exception)
            {
                // No template resolves; opaque is the contract's default.
                // （模板無法解析；契約的預設值即為不透明。）
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

            // Double-sidedness is a material *property* before it is a naming convention: HDRP's
            // switch is the `_DoubleSidedEnable` float, which the surface can carry itself
            // (`SyncToUnityMaterial` copies every float the shader knows) and the template
            // material holds otherwise. The name check stays as the fallback for templates whose
            // shader hides the property.
            // （雙面與否首先是材質<b>屬性</b>，其次才是命名慣例：HDRP 的開關是
            //   `_DoubleSidedEnable` 浮點數，表面自身可攜帶（`SyncToUnityMaterial` 會複製著色器
            //   認得的每個浮點數），否則由模板材質持有。名稱檢查保留為著色器隱藏該屬性時的後備。）
            if (surface.TryGetFloatProperty("_DoubleSidedEnable", out float doubleSided))
            {
                material.DoubleSided = doubleSided > 0.5f;
            }
            else
            {
                Material templateMaterial = null;
                try { templateMaterial = surface.GetTemplateMaterial(); } catch (Exception) { /* name fallback below */ }

                material.DoubleSided = ((templateMaterial != null) && templateMaterial.HasProperty("_DoubleSidedEnable"))
                    ? templateMaterial.GetFloat("_DoubleSidedEnable") > 0.5f
                    : name.Contains("doublesided");
            }

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
                            + $"{run.TexturesRebound} rebound by guid, {run.TexturesVirtual} virtual-textured "
                            + $"({run.TexturesFromStack} recovered from a shared stack, "
                            + $"{run.TexturesFromSurfaceData} from the surface's own VT data), "
                            + $"{run.TexturesUnbound} with nothing behind them; "
                            + $"{run.TexturesFromMaterial} taken off a live material as a last resort.");
            report.AppendLine($"Surface VT data: {run.SurfaceDataLoads} file(s) opened, {run.SurfaceDataFailures} failed to decode."
                + (run.SurfaceDataGuarded > 0
                    ? $" {run.SurfaceDataGuarded} skipped by vt_crash_guard.txt (crashed the game in a previous rip)."
                    : ""));

            // The colour mask is what makes colors.json applicable, so say plainly whether it came
            // out. Zero here does not mean the city has no colour variation — it means this code is
            // looking in the wrong place, and the line below says where to look instead.
            // （顏色遮罩正是使 colors.json 得以套用之物，故須明白說出它是否確實產出。
            //   此處為零並不代表城市沒有顏色變化，而是代表此段程式查找的位置有誤，
            //   而下一行會指出應改查何處。）
            report.AppendLine($"Colour masks: {run.ColorMasks} of {written} material(s) carry one "
                            + "(the texture colors.json needs to be applicable at all).");

            if (run.TemplateProperties.Count > 0)
            {
                List<string> properties = new(run.TemplateProperties);
                properties.Sort(StringComparer.Ordinal);

                List<string> unexported = new();
                foreach (string property in properties)
                    if (Array.IndexOf(Maps, property) < 0) unexported.Add(property);

                report.AppendLine($"Surface template properties declared by the material library "
                                + $"({properties.Count}): {string.Join(", ", properties)}");
                if (unexported.Count > 0)
                {
                    report.AppendLine($"  not exported by the contract's five: {string.Join(", ", unexported)}");
                }
            }

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
            string path = IOUtils.CombinePath(run.BundleRoot, "materials.json");

            // The table is bundle-wide but its producers are not: meshes and networks are separate
            // buttons, each ripping the materials of what it touches. Overwriting would mean a
            // network rip after a mesh rip leaves a bundle whose every building names a material
            // that is no longer in the file — every glTF material name has to stay a key (§6).
            // Keys this run resolved win, since they were read from the live game just now.
            // （材質表涵蓋整個束，其產生者則否：網格與路網為各自獨立的按鈕，各自擷取所觸及者的材質。
            //   若直接覆寫，則於網格擷取後再擷取路網，會使束中每棟建築所指名的材質不復存在於檔案中；
            //   而每個 glTF 材質名稱都必須是一個鍵（§6）。本次工作解析出的鍵優先，因其方才自遊戲讀出。）
            Dictionary<string, object> table = ReadTable(path);
            foreach (KeyValuePair<string, MaterialAsset> entry in run.Materials)
                table[entry.Key] = entry.Value.ToJson(run.Textures.Resolve);

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
        /// Read the material table a previous phase left on disk, or an empty one.
        /// （讀取前一階段留於磁碟上的材質表；若無則為空表。）<br/><br/>
        /// A file that will not parse is treated as absent rather than fatal: losing a phase's
        /// entries is bad, but refusing to write this phase's would lose both.<br/>
        /// （無法解析的檔案視為不存在而非致命錯誤：遺失某一階段的條目固然不好，
        /// 但拒絕寫出本階段的條目會使兩者俱失。）
        /// </summary>
        /// <param name="path">The <c>materials.json</c> path.（<c>materials.json</c> 的路徑。）</param>
        /// <returns>The entries already there.（已存在的條目。）</returns>
        private static Dictionary<string, object> ReadTable(string path)
        {
            try
            {
                if (!System.IO.File.Exists(path)) return new Dictionary<string, object>();

                Dictionary<string, object> existing =
                    JsonConvert.DeserializeObject<Dictionary<string, object>>(System.IO.File.ReadAllText(path));

                return existing ?? new Dictionary<string, object>();
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not read the existing materials.json, starting a new table: {ex.Message}");
                return new Dictionary<string, object>();
            }
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
