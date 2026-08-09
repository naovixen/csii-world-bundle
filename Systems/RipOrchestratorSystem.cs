using Carto.IO;
using Colossal.Logging;
using Game;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;

namespace Carto.Systems
{
    /// <summary>
    /// The one-button rip: runs the whole extraction pipeline in order, asynchronously, and
    /// reports everything in a single end-of-rip dialog (PLAN Phase 8).
    /// （一鍵擷取：依序、非同步地執行整條擷取管線，並以單一完成對話框回報全部結果（PLAN 第八階段）。）<br/><br/>
    /// <b>Stage order</b>: instances &amp; appearance → meshes/materials/textures → lane graph →
    /// networks &amp; lane geometry → terrain &amp; water → optional vector export →
    /// <b>manifest, always last and unconditionally</b> — it describes whatever the run left on
    /// disk, so it is also correct after a partial (layer-deselected) rip, and folding it in kills
    /// the "forgot to re-finalize" foot-gun.<br/>
    /// （<b>階段順序</b>：實例與外觀 → 網格／材質／貼圖 → 車道圖 → 路網與車道幾何 → 地形與水體 →
    /// 可選的向量輸出 → <b>宣告檔，恆為最後且無條件執行</b>——其描述本次執行留於磁碟的內容，
    /// 故部分（取消圖層的）擷取後亦屬正確，且併入管線後不再有「忘記重新定稿」的隱患。）<br/><br/>
    /// <b>Failure policy</b>: a stage that throws is recorded as failed and the pipeline moves on —
    /// the layers are independent by contract (absent layer = absent feature), and the manifest
    /// must still describe whatever did land.<br/>
    /// （<b>失敗原則</b>：擲出例外的階段記為失敗後管線繼續——各層依契約彼此獨立（缺層即缺特徵），
    /// 且宣告檔仍須描述實際落地的內容。）<br/><br/>
    /// The two long stages (meshes, networks) already run a slice per frame against a bounded
    /// writer queue; the pipeline waits for their completion callback. Short synchronous stages run
    /// through <see cref="RipProgress.RunDeferred"/> so their caption reaches the screen before
    /// they take the main thread.<br/>
    /// （兩個長階段（網格、路網）本就每幀推進一段並受限於有界寫出佇列；管線等待其完成回呼。
    /// 短的同步階段經 <see cref="RipProgress.RunDeferred"/> 執行，使說明文字先於其佔用主執行緒抵達畫面。）
    /// </summary>
    public partial class RipOrchestratorSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// What a rip should cover, and with which caps. The toggles map 1:1 to pipeline stages
        /// (coarse by design — the contract already handles partial bundles).
        /// （一次擷取應涵蓋的範圍及其上限。開關與管線階段一一對應（刻意粗粒度——契約本就支援部分束）。）
        /// </summary>
        public sealed class RipPlan
        {
            /// <summary>The bundle root to write into.（要寫入的束根目錄。）</summary>
            public string OutputDirectory;

            /// <summary>Buildings, props &amp; trees: instances + appearance, and the prefab meshes they reference.（建築、道具與樹木：實例與外觀，及其參照的預製模板網格。）</summary>
            public bool Objects = true;

            /// <summary>Roads &amp; lanes: the lane graph, network surfaces and lane-drawn geometry.（道路與車道：車道圖、路網表面與車道所繪幾何。）</summary>
            public bool Networks = true;

            /// <summary>Terrain &amp; water: heightmaps, splat, surfaces and clip volumes.（地形與水體：高度圖、地表混合、地表區域與裁剪體積。）</summary>
            public bool Terrain = true;

            /// <summary>The vanilla vector/raster export, alongside.（隨附的原版向量／網格輸出。）</summary>
            public bool Vector = false;

            /// <summary>The most prefabs the mesh stage rips, or <c>0</c> for all.（網格階段最多擷取的預製模板數，<c>0</c> 為全部。）</summary>
            public int MeshLimit;

            /// <summary>The longest edge a written texture may have, or <c>0</c> for no cap.（寫出貼圖的最長邊上限，<c>0</c> 為不限。）</summary>
            public int TextureSizeCap;
        }

        /// <summary>
        /// One pipeline stage: either synchronous work returning its summary, or an asynchronous
        /// begin/poll pair whose summary arrives through a completion callback.
        /// （管線的一個階段：或為回傳摘要的同步工作，或為以完成回呼交付摘要的非同步 begin／poll 對。）
        /// </summary>
        private sealed class Stage
        {
            /// <summary>The heading the stage reports under.（該階段回報時使用的標題。）</summary>
            public string Title;

            /// <summary>The caption phase while a synchronous stage runs.（同步階段執行時的說明文字階段。）</summary>
            public RipProgress.Phase Phase;

            /// <summary>Synchronous work, or <c>null</c> for an async stage.（同步工作；非同步階段為 <c>null</c>。）</summary>
            public Func<string> Work;

            /// <summary>Start an async stage, handing it the completion sink; returns the kickoff message.（啟動非同步階段並交付完成回呼；回傳啟動訊息。）</summary>
            public Func<Action<string>, string> Begin;

            /// <summary>Whether the async stage actually started.（非同步階段是否確實啟動。）</summary>
            public Func<bool> Running;
        }

        /// <summary>The stages of the rip in flight, or <c>null</c> when idle.（進行中擷取的各階段；閒置時為 <c>null</c>。）</summary>
        private List<Stage> _stages;

        /// <summary>The stage currently running.（目前執行中的階段。）</summary>
        private int _index;

        /// <summary>The plan the run was started with.（本次執行採用的計畫。）</summary>
        private RipPlan _plan;

        /// <summary>Set (with <see cref="_stageSummary"/>) when the current stage finishes.（目前階段完成時（連同 <see cref="_stageSummary"/>）設定。）</summary>
        private volatile bool _stageDone;
        private string _stageSummary;

        /// <summary>How many stages recorded a failure.（記錄為失敗的階段數。）</summary>
        private int _stagesFailed;

        /// <summary>Timing for the current stage and the whole run.（目前階段與整體執行的計時。）</summary>
        private Stopwatch _stageClock, _totalClock;

        /// <summary>The per-stage lines of the end-of-rip dialog.（完成對話框中各階段的行。）</summary>
        private StringBuilder _summary;

        /// <summary>
        /// Bumped whenever a run starts or is torn down, so a completion callback from an
        /// abandoned run cannot advance a later one.
        /// （每次執行開始或收拾時遞增，使被中止執行的完成回呼無法推進後續的執行。）
        /// </summary>
        private int _generation;

        /// <summary>
        /// Whether a one-button rip is in flight.（一鍵擷取是否進行中。）
        /// </summary>
        public bool IsRipping => _stages != null;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// （當系統實例被創造時觸發的事件。）
        /// </summary>
        protected override void OnCreate()
        {
            base.OnCreate();
            _log.Debug("RipOrchestratorSystem instance created. 擷取管線系統實例創造完成。");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// （當系統實例被銷毀時所觸發的事件。）<br/><br/>
        /// A pipeline abandoned by a world unload is simply dropped: the async stages release
        /// themselves in their own <c>OnDestroy</c>, and every completion callback is fenced by
        /// <see cref="_generation"/>.<br/>
        /// （世界卸載時被中止的管線逕行捨棄：非同步階段於各自的 <c>OnDestroy</c> 中自行釋放，
        /// 且所有完成回呼皆受 <see cref="_generation"/> 防護。）
        /// </summary>
        protected override void OnDestroy()
        {
            if (_stages != null)
            {
                _log.Info($"World rip abandoned at stage {_index + 1}/{_stages.Count}; the world was unloaded. 世界擷取於世界卸載時中止。");
                Teardown();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Advance the pipeline: start the next stage once the current one has reported.
        /// （推進管線：目前階段回報後啟動下一階段。）
        /// </summary>
        protected override void OnUpdate()
        {
            if (_stages == null || !_stageDone) return;

            _stageClock.Stop();
            RecordStage(_stages[_index], _stageSummary, _stageClock.Elapsed.TotalSeconds);

            _index++;
            if (_index < _stages.Count) StartStage(_stages[_index]);
            else Finish();
        }

        /// <summary>
        /// Start the whole pipeline, returning at once.（啟動整條管線並立即返回。）
        /// </summary>
        /// <param name="plan">What to rip.（要擷取的範圍。）</param>
        /// <returns>What the caller should tell the user.（呼叫端應告知使用者的訊息。）</returns>
        public string BeginRip(RipPlan plan)
        {
            if (IsRipping) return "A world rip is already running.";
            if (Instance.InstanceRip.IsRipping) return "An instance rip is already running — wait for it to finish.";
            if (Instance.MeshRip.IsRipping) return "A mesh rip is already running — wait for it to finish.";
            if (Instance.LaneRip.IsRipping) return "A lane rip is already running — wait for it to finish.";
            if (Instance.NetworkRip.IsRipping) return "A network rip is already running — wait for it to finish.";

            List<Stage> stages = BuildStages(plan);
            System.IO.Directory.CreateDirectory(plan.OutputDirectory);

            _plan = plan;
            _stages = stages;
            _index = 0;
            _stagesFailed = 0;
            _summary = new StringBuilder();
            _totalClock = Stopwatch.StartNew();

            StringBuilder titles = new();
            foreach (Stage stage in stages) titles.Append(titles.Length == 0 ? "" : ", ").Append(stage.Title);
            _log.Info($"World rip started: {stages.Count} stage(s) — {titles} — into {plan.OutputDirectory}. 世界擷取開始。");

            StartStage(stages[0]);

            return $"Ripping the world in the background ({stages.Count} stage(s)).\n\n"
                 + "Close this options page to watch the progress indicator — it appears in the top-right of the game view, not over the menu.\n\n"
                 + "A single dialog reports every stage when the rip finishes.";
        }

        /// <summary>
        /// Assemble the stage list for a plan. The manifest is appended unconditionally.
        /// （為計畫組裝階段清單。宣告檔一律附加於最後。）
        /// </summary>
        /// <param name="plan">What to rip.（要擷取的範圍。）</param>
        /// <returns>The stages, in execution order.（依執行順序排列的階段。）</returns>
        private static List<Stage> BuildStages(RipPlan plan)
        {
            string directory = plan.OutputDirectory;
            List<Stage> stages = new();

            // A stage's outputs are truncated or overwritten by name when it re-runs — except the
            // content-hash-named textures/, whose files from an earlier run at a different cap are
            // orphans no record references, silently inflating the bundle's counts and sizes. So a
            // rip first deletes the outputs OWNED by its selected stages (a deselected layer's
            // files stay, by the partial-bundle contract). The shared texture store and material
            // table are cleared only when BOTH of their producers run: after that pair the rebuilt
            // table is complete, while a partial rip must keep them — the merge semantics are what
            // keep the other half's material references valid (MaterialRipSystem.WriteTable).
            // （階段重跑時其輸出會被截斷或依名稱覆寫——唯獨以內容雜湊命名的 textures/ 例外：
            //   先前以不同上限擷取的檔案成為無任何紀錄參照的孤兒，默默灌水束的計數與大小。
            //   因此擷取先刪除其所選階段「擁有」的輸出（未選圖層的檔案依部分束契約保留）。
            //   共用的貼圖庫與材質表僅於其兩個產生者皆執行時清除：該組合後重建的表是完整的，
            //   而部分擷取必須保留兩者——合併語義正是使另一半材質參照有效的機制。）
            List<string> staleFiles = new();
            List<string> staleDirectories = new();
            if (plan.Objects)
            {
                staleFiles.AddRange(new[] { "instances.jsonl", "colors.json", "lights.json" });
                staleDirectories.Add("meshes");
            }
            if (plan.Networks)
            {
                staleFiles.AddRange(new[] { "networks.jsonl", "lanes.jsonl", "lanemeshes.jsonl", "laneprefabs.json" });
                staleDirectories.AddRange(new[] { "networks", "lanemeshes" });
            }
            if (plan.Terrain)
            {
                staleFiles.AddRange(new[] { "areas.jsonl", "areaprefabs.json", "clipvolumes.jsonl" });
                staleDirectories.Add("terrain");
            }
            if (plan.Objects && plan.Networks)
            {
                staleFiles.Add("materials.json");
                staleDirectories.Add("textures");
            }

            if ((staleFiles.Count > 0) || (staleDirectories.Count > 0))
            {
                stages.Add(new Stage
                {
                    Title = "Clean",
                    Begin = completed =>
                    {
                        RipProgress.Report(RipProgress.Phase.Clean);

                        // Off the main thread: meshes/ and textures/ can be gigabytes, and
                        // deleting them is pure file I/O.（於主執行緒外：meshes/ 與 textures/
                        // 可達數 GB，而刪除純屬檔案讀寫。）
                        System.Threading.Tasks.Task.Run(() => completed(CleanStaleOutputs(directory, staleFiles, staleDirectories)));

                        return "Removing the previous rip's outputs of the selected layers on a background thread.";
                    },
                    Running = () => true
                });
            }

            if (plan.Objects)
            {
                stages.Add(new Stage
                {
                    Title = "Instances",
                    Begin = completed => Instance.InstanceRip.BeginRip(directory, completed),
                    Running = () => Instance.InstanceRip.IsRipping
                });

                // Colours and lights are per-render-prefab tables resolved from the same prefab
                // set the instances reference; a quick synchronous pass.
                // （顏色與燈光為逐繪製預製模板的表格，解析自實例參照的同一組預製模板；
                //   為快速的同步走訪。）
                stages.Add(new Stage
                {
                    Title = "Appearance",
                    Phase = RipProgress.Phase.Instances,
                    Work = () => Instance.AppearanceRip.RipAppearance(directory)
                });

                stages.Add(new Stage
                {
                    Title = "Meshes, materials & textures",
                    Begin = completed => Instance.MeshRip.BeginRip(directory, plan.MeshLimit, plan.TextureSizeCap, completed),
                    Running = () => Instance.MeshRip.IsRipping
                });
            }

            if (plan.Networks)
            {
                stages.Add(new Stage
                {
                    Title = "Lane graph",
                    Begin = completed => Instance.LaneRip.BeginRip(directory, completed),
                    Running = () => Instance.LaneRip.IsRipping
                });

                stages.Add(new Stage
                {
                    Title = "Networks & lane geometry",
                    Begin = completed => Instance.NetworkRip.BeginRip(directory, plan.TextureSizeCap, completed),
                    Running = () => Instance.NetworkRip.IsRipping
                });
            }

            if (plan.Terrain)
            {
                stages.Add(new Stage
                {
                    Title = "Terrain & water",
                    Phase = RipProgress.Phase.Terrain,
                    Work = () => Instance.TerrainRip.RipTerrain(directory)
                });
            }

            if (plan.Vector)
            {
                stages.Add(new Stage
                {
                    Title = "Vector export",
                    Phase = RipProgress.Phase.Export,
                    Work = () => { IO.IO.Export(); return "Vector/raster export completed."; }
                });
            }

            // Always last, unconditionally: the manifest describes whatever the run left on disk,
            // including the layers earlier runs left there and this one did not touch. The
            // game-state snapshot is captured on the main thread (cheap); the directory scan —
            // reading every record layer back — runs on a worker, and the pipeline waits on the
            // completion callback before reporting (PLAN Phase 8).
            // （恆為最後且無條件執行：宣告檔描述本次執行留於磁碟的內容，包括先前執行留下而本次
            //   未觸及的層。遊戲狀態快照於主執行緒擷取（廉價）；目錄掃描——讀回每個紀錄層——
            //   於工作執行緒進行，管線於回報前等待完成回呼（PLAN 第八階段）。）
            stages.Add(new Stage
            {
                Title = "Manifest",
                Begin = completed =>
                {
                    RipProgress.Report(RipProgress.Phase.Manifest);
                    BundleManifest snapshot = BundleManifest.Capture();

                    System.Threading.Tasks.Task.Run(() =>
                    {
                        string summary;
                        try
                        {
                            summary = BundleWriter.WriteManifest(directory, snapshot);
                        }
                        catch (Exception ex)
                        {
                            Instance.Log.Error($"World rip stage `Manifest` failed: {ex}");
                            summary = $"FAILED: {ex.Message}";
                        }

                        completed(summary);
                    });

                    return "Scanning the bundle on a background thread.";
                },
                Running = () => true
            });

            return stages;
        }

        /// <summary>
        /// Delete the previous rip's outputs of the selected stages. Runs on a worker thread.
        /// （刪除所選階段的前次擷取輸出。於工作執行緒執行。）
        /// </summary>
        /// <param name="directory">The bundle root.（束根目錄。）</param>
        /// <param name="files">Layer files to remove, relative to the root.（要移除的層檔案，相對於根目錄。）</param>
        /// <param name="directories">Layer directories to remove, relative to the root.（要移除的層目錄。）</param>
        /// <returns>What was removed.（移除了什麼。）</returns>
        private static string CleanStaleOutputs(string directory, List<string> files, List<string> directories)
        {
            List<string> removed = new();
            List<string> failed = new();

            foreach (string file in files)
            {
                string path = Utils.IOUtils.CombinePath(directory, file);
                try
                {
                    if (!System.IO.File.Exists(path)) continue;
                    System.IO.File.Delete(path);
                    removed.Add(file);
                }
                catch (Exception ex)
                {
                    failed.Add(file);
                    _log.Warn($"Could not remove stale `{file}`: {ex.Message}");
                }
            }

            foreach (string dir in directories)
            {
                string path = Utils.IOUtils.CombinePath(directory, dir);
                try
                {
                    if (!System.IO.Directory.Exists(path)) continue;
                    System.IO.Directory.Delete(path, recursive: true);
                    removed.Add(dir + "/");
                }
                catch (Exception ex)
                {
                    failed.Add(dir + "/");
                    _log.Warn($"Could not remove stale `{dir}/`: {ex.Message}");
                }
            }

            if ((removed.Count == 0) && (failed.Count == 0)) return "Nothing stale to remove.";

            string summary = (removed.Count > 0)
                ? $"Removed the previous rip's {string.Join(", ", removed)}."
                : "";
            if (failed.Count > 0)
                summary += $"{(summary.Length > 0 ? "\n" : "")}FAILED to remove {string.Join(", ", failed)} — stale files may inflate the bundle; see the log.";

            return summary;
        }

        /// <summary>
        /// Start one stage. Synchronous work is deferred a couple of frames so its caption paints
        /// first; asynchronous work is begun and awaited through its completion callback.
        /// （啟動一個階段。同步工作延後數幀使其說明文字先行繪出；非同步工作啟動後經完成回呼等待。）
        /// </summary>
        /// <param name="stage">The stage to start.（要啟動的階段。）</param>
        private void StartStage(Stage stage)
        {
            _stageDone = false;
            _stageSummary = null;
            _stageClock = Stopwatch.StartNew();

            int generation = _generation;
            void Done(string summary)
            {
                if (generation != _generation) return;
                _stageSummary = summary;
                _stageDone = true;
            }

            if (stage.Work != null)
            {
                // The exception is caught here, not left to RunDeferred's own catch, because the
                // pipeline must hear about the stage either way — a swallowed throw would leave it
                // waiting forever.（例外於此捕捉而不留給 RunDeferred 自身的 catch，
                //   因為無論如何管線都必須得到該階段的回報——被吞掉的例外會使其永遠等待。）
                RipProgress.RunDeferred(stage.Phase, () =>
                {
                    string summary;
                    try
                    {
                        summary = stage.Work();
                    }
                    catch (Exception ex)
                    {
                        _log.Error($"World rip stage `{stage.Title}` failed: {ex}");
                        summary = $"FAILED: {ex.Message}";
                    }

                    Done(summary);
                });

                return;
            }

            string kickoff;
            try
            {
                kickoff = stage.Begin(Done);
            }
            catch (Exception ex)
            {
                _log.Error($"World rip stage `{stage.Title}` failed to start: {ex}");
                Done($"FAILED to start: {ex.Message}");
                return;
            }

            _log.Info($"World rip stage `{stage.Title}`: {kickoff}");

            // Not started means there was nothing to do (or a refusal) — the kickoff message is
            // the whole story, and no completion callback will ever come.
            // （未啟動代表無事可做（或遭拒）——啟動訊息即全部內容，完成回呼永不會來。）
            if (!stage.Running()) Done(kickoff);
        }

        /// <summary>
        /// Append one finished stage to the end-of-rip summary.（將一個已完成的階段附加至完成摘要。）
        /// </summary>
        /// <param name="stage">The stage that finished.（已完成的階段。）</param>
        /// <param name="summary">What it reported.（其回報內容。）</param>
        /// <param name="seconds">How long it took.（其耗時。）</param>
        private void RecordStage(Stage stage, string summary, double seconds)
        {
            // Failures reach here as a summary prefix whichever thread they happened on — the
            // sync catch, the failed-to-start path and the manifest worker all speak it.
            // （無論失敗發生於哪個執行緒，皆以摘要前綴到達此處——同步 catch、啟動失敗路徑
            //   與宣告檔工作執行緒皆用此語。）
            if ((summary != null) && summary.StartsWith("FAILED", StringComparison.Ordinal)) _stagesFailed++;

            _summary.AppendLine($"{stage.Title} — {seconds:F1} s");

            if (!string.IsNullOrEmpty(summary))
            {
                // A few lines per stage keeps the one dialog readable; every stage's full report
                // is already in the log.（每階段數行使單一對話框可讀；各階段完整報告已在記錄檔中。）
                string[] lines = summary.Replace("\r\n", "\n").Split('\n');
                int shown = 0;
                foreach (string line in lines)
                {
                    if (string.IsNullOrWhiteSpace(line) || line.StartsWith("Full report:")) continue;
                    _summary.AppendLine($"    {line.Trim()}");
                    if (++shown >= 3) break;
                }
            }

            _log.Info($"World rip stage `{stage.Title}` finished in {seconds:F1} s:\n{summary}");
        }

        /// <summary>
        /// Close out the run: log, clear, and show the single end-of-rip dialog.
        /// （收尾：記錄、清理，並顯示單一的完成對話框。）
        /// </summary>
        private void Finish()
        {
            _totalClock.Stop();

            StringBuilder dialog = new();
            dialog.AppendLine(_stagesFailed == 0
                ? $"World rip finished in {_totalClock.Elapsed.TotalSeconds:F0} s."
                : $"World rip finished in {_totalClock.Elapsed.TotalSeconds:F0} s — {_stagesFailed} stage(s) FAILED.");
            dialog.AppendLine();
            dialog.Append(_summary);
            dialog.AppendLine();
            dialog.AppendLine($"Output: {_plan.OutputDirectory}");
            dialog.Append("Full report: Logs/Carto.log");

            _log.Info($"World rip finished. 世界擷取完成。\n{dialog}");
            Teardown();

            try
            {
                Game.UI.MessageDialog message = new("Options.SECTION[Carto.Carto.Mod]", Settings.Fit(dialog.ToString()), "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(message, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the world rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Drop all run state and fence off stale callbacks.（捨棄所有執行狀態並隔離過期回呼。）
        /// </summary>
        private void Teardown()
        {
            _generation++;
            _stages = null;
            _plan = null;
            _stageDone = false;
            _stageSummary = null;
            RipProgress.Clear();
        }
    }
}
