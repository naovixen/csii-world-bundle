using Colossal.Logging;
using Colossal.UI.Binding;
using System;
using System.Threading;

namespace Carto.IO
{
    /// <summary>
    /// The world ripper's progress indicator, shown through the game's own background-process
    /// indicator (the spinner and caption the base game uses for autosaves).<br/>
    /// （世界擷取器的進度指示器，透過遊戲自身的背景程序指示器顯示，即原版遊戲於自動存檔時使用的旋轉圖示與說明文字。）<br/><br/>
    /// The indicator is driven by the <c>app.backgroundProcessMessage</c> UI binding, whose value
    /// is a <b>locale key</b>, not free text — the UI renders an unknown key as an empty string. The
    /// percentage is therefore baked into the key: <see cref="Carto.Utils.LocaleUtils"/> expands one
    /// translated template per phase into <c>Carto.PROGRESS[&lt;phase&gt;.&lt;percent&gt;]</c> entries
    /// at load time, so reporting progress is a binding update and never a dictionary rebuild.<br/>
    /// （指示器由 <c>app.backgroundProcessMessage</c> UI 繫結驅動，其值為<b>語系鍵</b>而非自由文字，
    /// 未知的鍵會被渲染為空字串。因此百分比被編入鍵中：載入時將每個階段的翻譯模板展開為
    /// <c>Carto.PROGRESS[&lt;階段&gt;.&lt;百分比&gt;]</c> 條目，使回報進度僅是一次繫結更新。）<br/><br/>
    /// <b>Any thread may report.</b> Reporting only writes fields; the binding is touched solely by
    /// <see cref="Pump"/>, which <see cref="Carto.Systems.MeshRipSystem"/> calls once per frame on
    /// the main thread. Off-thread writers therefore need no marshalling of their own.<br/>
    /// （<b>任何執行緒皆可回報。</b>回報僅寫入欄位，繫結只由主執行緒每幀呼叫的 <see cref="Pump"/> 觸碰。）
    /// </summary>
    public static class RipProgress
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The locale key prefix the progress entries are generated under.
        /// （產生進度條目所使用的語系鍵前綴。）
        /// </summary>
        public const string LocalePrefix = "Carto.PROGRESS";

        /// <summary>
        /// The phases a rip reports under. Each needs a matching <c>Carto.PROGRESS[&lt;name&gt;]</c>
        /// template in every locale file.<br/>
        /// （擷取過程可回報的階段。每個階段在各語系檔中都需要對應的 <c>Carto.PROGRESS[&lt;名稱&gt;]</c> 模板。）
        /// </summary>
        public enum Phase
        {
            /// <summary>No work in progress; the indicator is hidden.（沒有進行中的工作，指示器隱藏。）</summary>
            None,

            /// <summary>Enumerating the world to build the work list.（列舉世界以建立工作清單。）</summary>
            Scan,

            /// <summary>Removing the previous rip's outputs of the selected layers.（移除所選圖層的前次擷取輸出。）</summary>
            Clean,

            /// <summary>Enumerating placed objects and their appearance.（列舉已放置物件及其外觀。）</summary>
            Instances,

            /// <summary>Walking the lane graph.（走訪車道圖。）</summary>
            Lanes,

            /// <summary>Reading the terrain, water and surface rasters.（讀取地形、水體與地表網格。）</summary>
            Terrain,

            /// <summary>Reading prefab geometry.（讀取預製模板幾何。）</summary>
            Meshes,

            /// <summary>Assembling and bending network geometry.（組裝並彎折路網幾何。）</summary>
            Networks,

            /// <summary>Draining the writer queue to disk.（將寫出佇列排空至磁碟。）</summary>
            Write,

            /// <summary>The vanilla vector/raster export.（原版的向量／網格輸出。）</summary>
            Export,

            /// <summary>Assembling the bundle manifest.（組裝束的宣告檔。）</summary>
            Manifest
        }

        /// <summary>
        /// The phase currently being reported.（目前回報的階段。）
        /// </summary>
        private static volatile Phase _phase = Phase.None;

        /// <summary>
        /// Completed and total units of work in the current phase.
        /// （目前階段中已完成與總計的工作量。）<br/><br/>
        /// A <see cref="_total"/> of zero means indeterminate — the caption is shown without a
        /// percentage.<br/>（<see cref="_total"/> 為零代表進度未知，說明文字不附百分比。）
        /// </summary>
        private static int _current, _total;

        /// <summary>
        /// The locale key last pushed to the binding, so <see cref="Pump"/> is a no-op while the
        /// displayed percentage has not changed.<br/>
        /// （最後推送至繫結的語系鍵，使 <see cref="Pump"/> 在顯示的百分比未變時不做任何事。）
        /// </summary>
        private static string _pushed;

        /// <summary>
        /// Work queued by <see cref="RunDeferred"/>, and the number of frames it has waited.
        /// （由 <see cref="RunDeferred"/> 排入的工作，以及其已等待的幀數。）
        /// </summary>
        private static Action _deferred;
        private static string _deferredMessage;
        private static int _deferredFrames;

        /// <summary>
        /// The number of frames a deferred action waits before running.
        /// （延後執行的工作於執行前等待的幀數。）<br/><br/>
        /// The point of deferring is that the caption reaches the screen <i>before</i> a blocking
        /// operation freezes the main thread. One frame sets the binding, the next lets the UI
        /// process and paint it.<br/>
        /// （延後的用意在於讓說明文字於阻塞操作凍結主執行緒<i>之前</i>抵達畫面：
        /// 第一幀設定繫結，第二幀讓 UI 處理並繪製。）
        /// </summary>
        private const int DeferredDelayFrames = 2;

        /// <summary>
        /// Report progress within a phase.（回報某階段中的進度。）
        /// </summary>
        /// <param name="phase">The phase being worked on.（正在進行的階段。）</param>
        /// <param name="current">Units of work completed.（已完成的工作量。）</param>
        /// <param name="total">Total units of work, or <c>0</c> when indeterminate.（總工作量，未知時為 <c>0</c>。）</param>
        public static void Report(Phase phase, int current = 0, int total = 0)
        {
            _phase = phase;
            Interlocked.Exchange(ref _current, current);
            Interlocked.Exchange(ref _total, total);
        }

        /// <summary>
        /// Hide the indicator.（隱藏指示器。）
        /// </summary>
        public static void Clear() => Report(Phase.None);

        /// <summary>
        /// Show <paramref name="message"/>, then run <paramref name="action"/> a couple of frames
        /// later on the main thread.<br/>
        /// （顯示 <paramref name="message"/>，並於數幀後在主執行緒上執行 <paramref name="action"/>。）<br/><br/>
        /// This exists for work that genuinely blocks — the vanilla vector export runs as one
        /// synchronous call — so that the user sees "Carto is working" rather than a frozen game
        /// with no explanation. It does not make the work itself interruptible.<br/>
        /// （此機制用於確實會阻塞的工作，例如原版向量輸出為單一同步呼叫，
        /// 使使用者看到「Carto 正在工作」而非毫無說明的凍結畫面。它並不會讓工作本身變得可中斷。）
        /// </summary>
        /// <param name="phase">The phase to caption it with.（用以顯示說明文字的階段。）</param>
        /// <param name="action">The work to run.（要執行的工作。）</param>
        public static void RunDeferred(Phase phase, Action action)
        {
            if (action == null) return;

            Report(phase);
            _deferredMessage = BuildKey();
            _deferredFrames = 0;
            _deferred = action;
        }

        /// <summary>
        /// Push the current state to the UI, and run any deferred work whose caption has had time
        /// to reach the screen. Must be called on the main thread, once per frame.<br/>
        /// （將目前狀態推送至 UI，並執行說明文字已有時間抵達畫面的延後工作。必須於主執行緒每幀呼叫一次。）
        /// </summary>
        public static void Pump()
        {
            string key = BuildKey();
            if (!string.Equals(key, _pushed, StringComparison.Ordinal))
            {
                _pushed = key;
                TryPush(key);
                PushToast();
            }

            TickToastLinger();

            if (_deferred == null) return;

            // Wait until the caption this action was queued with is the one on screen, then give
            // the UI a frame to paint it.
            // （等到此工作排入時的說明文字確實已推送，再給 UI 一幀的時間繪製。）
            if (!string.Equals(_pushed, _deferredMessage, StringComparison.Ordinal)) return;
            if (++_deferredFrames < DeferredDelayFrames) return;

            Action action = _deferred;
            _deferred = null;
            _deferredFrames = 0;

            try
            {
                action();
            }
            catch (Exception ex)
            {
                _log.Error($"Deferred work failed: {ex}");
            }
            finally
            {
                Clear();
            }
        }

        // ====================================================================================
        // The notification toast — the game's own progress-bearing notification stack
        // (Game.PSI.NotificationSystem, public API; audit §37). The background-process caption
        // above stays as the second sink: the two cost one binding update each and fail
        // independently.
        // （通知橫幅——遊戲自身可承載進度的通知堆疊（公開 API；審計 §37）。上方的背景程序
        //   說明文字保留為第二個輸出端：兩者各僅一次繫結更新，且互不牽連地失效。）
        // ====================================================================================

        /// <summary>
        /// The one notification a rip drives. A stable id makes every push an in-place update of
        /// the same toast rather than a growing stack.
        /// （一次擷取驅動的單一通知。固定的識別碼使每次推送皆為同一橫幅的就地更新，而非不斷增長的堆疊。）
        /// </summary>
        private const string NotificationId = "Carto.RipWorld";

        /// <summary>Whether the toast is currently showing.（橫幅目前是否顯示中。）</summary>
        private static bool _toastShown;

        /// <summary>Consecutive <see cref="Pump"/> calls with no phase, for the linger window.（連續無階段的 <see cref="Pump"/> 次數，供停留視窗使用。）</summary>
        private static int _idlePumps;

        /// <summary>Whether a toast failure has been logged.（橫幅失敗是否已記錄。）</summary>
        private static bool _toastFailedLogged;

        /// <summary>
        /// How many idle pumps the toast survives before it is completed and dismissed.
        /// （橫幅於完成並消退前可經歷的閒置次數。）<br/><br/>
        /// The pipeline passes through <see cref="Phase.None"/> for a frame or two between
        /// stages (each stage clears the indicator as it finishes); popping and re-pushing across
        /// that gap would make the toast flicker once per stage. Pump runs a couple of times per
        /// frame, so this is roughly a second of real idleness.
        /// （管線於階段之間會經過一兩幀的 <see cref="Phase.None"/>（各階段完成時清除指示器）；
        /// 若在該間隙彈出再重推，橫幅將每階段閃爍一次。Pump 每幀執行數次，故此值約為一秒的實際閒置。）
        /// </summary>
        private const int ToastLingerPumps = 120;

        /// <summary>
        /// Create or update the rip's toast for the current phase and percentage.
        /// （為目前的階段與百分比建立或更新擷取橫幅。）<br/><br/>
        /// The text is the same per-phase locale key the caption uses — the progress bar carries
        /// the percentage, so the pre-expanded percent keys are not needed here. Only called when
        /// the built key changed, so an unchanged percentage costs nothing.
        /// （文字沿用說明文字的逐階段語系鍵——百分比由進度條呈現，故此處無須預先展開的百分比鍵。
        /// 僅於建構的鍵改變時呼叫，百分比未變即無成本。）
        /// </summary>
        private static void PushToast()
        {
            Phase phase = _phase;
            if (phase == Phase.None) return;

            try
            {
                int total = Volatile.Read(ref _total);
                int percent = 0;
                if (total > 0)
                {
                    percent = (int)((Volatile.Read(ref _current) * 100L) / total);
                    percent = (percent < 0) ? 0 : ((percent > 100) ? 100 : percent);
                }

                Game.PSI.NotificationSystem.Push(NotificationId,
                    title: Game.UI.Localization.LocalizedString.Id("Options.SECTION[Carto.Carto.Mod]"),
                    text: Game.UI.Localization.LocalizedString.Id($"{LocalePrefix}[{phase}]"),
                    progressState: (total > 0) ? Colossal.PSI.Common.ProgressState.Progressing
                                               : Colossal.PSI.Common.ProgressState.Indeterminate,
                    progress: percent);

                _toastShown = true;
                _idlePumps = 0;
            }
            catch (Exception ex)
            {
                if (!_toastFailedLogged)
                {
                    _toastFailedLogged = true;
                    _log.Warn($"Could not drive the notification toast; the caption still works: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Complete and dismiss the toast once no phase has reported for the linger window.
        /// （於停留視窗內無任何階段回報後，將橫幅標記完成並消退。）
        /// </summary>
        private static void TickToastLinger()
        {
            if (!_toastShown || (_phase != Phase.None)) return;
            if (++_idlePumps < ToastLingerPumps) return;

            _toastShown = false;
            _idlePumps = 0;

            try
            {
                Game.PSI.NotificationSystem.Pop(NotificationId, delay: 2f,
                    progressState: Colossal.PSI.Common.ProgressState.Complete, progress: 100);
            }
            catch (Exception ex)
            {
                if (!_toastFailedLogged)
                {
                    _toastFailedLogged = true;
                    _log.Warn($"Could not dismiss the notification toast: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Build the locale key for the current phase and percentage.
        /// （為目前的階段與百分比建構語系鍵。）
        /// </summary>
        /// <returns>The locale key, or <c>null</c> when nothing should be shown.（語系鍵，若不應顯示任何內容則為 <c>null</c>。）</returns>
        private static string BuildKey()
        {
            Phase phase = _phase;
            if (phase == Phase.None) return null;

            int total = Volatile.Read(ref _total);
            if (total <= 0) return $"{LocalePrefix}[{phase}]";

            int percent = (int)((Volatile.Read(ref _current) * 100L) / total);
            percent = (percent < 0) ? 0 : ((percent > 100) ? 100 : percent);
            return $"{LocalePrefix}[{phase}.{percent}]";
        }

        /// <summary>
        /// Set the game's background-process binding, tolerating its absence.
        /// （設定遊戲的背景程序繫結，並容忍其不存在。）<br/><br/>
        /// The binding lives on <c>AppBindings</c>, which does not exist before the UI is up and is
        /// not something a mod should require — a missing indicator must never fail a rip.<br/>
        /// （該繫結位於 <c>AppBindings</c> 上，在 UI 啟動前並不存在，且不應成為模組的必要條件；
        /// 指示器缺失絕不應導致擷取失敗。）
        /// </summary>
        /// <param name="key">The locale key, or <c>null</c> to hide the indicator.（語系鍵，若要隱藏指示器則為 <c>null</c>。）</param>
        private static void TryPush(string key)
        {
            try
            {
                ValueBinding<string> binding = ResolveBinding();
                if (binding == null) return;

                binding.Update(key);

                // Say once, out loud, that a caption really did reach the binding. Every failure
                // path here used to be silent, which made "no indicator appeared" indistinguishable
                // from "the indicator appeared and you looked away".
                // （明確記錄一次說明文字確實已送達繫結。此處先前每條失敗路徑皆無聲，
                //   使「未出現指示器」與「指示器出現但未被看見」無從區分。）
                if (!_pushLogged && (key != null))
                {
                    _pushLogged = true;
                    _log.Info($"Background-process indicator updated with `{key}`; translated = `{Carto.Utils.LocaleUtils.Translate(key)}`. 已更新背景程序指示器。");
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not update the background-process indicator: {ex}");
            }
        }

        /// <summary>
        /// Whether the first successful push has already been logged.
        /// （首次成功推送是否已被記錄。）
        /// </summary>
        private static bool _pushLogged;

        /// <summary>
        /// The game's background-process binding, resolved once and cached.
        /// （遊戲的背景程序繫結，僅解析一次並快取。）
        /// </summary>
        private static ValueBinding<string> _binding;

        /// <summary>
        /// Whether resolving the binding has already been attempted.
        /// （是否已嘗試過解析繫結。）
        /// </summary>
        private static bool _bindingResolved;

        /// <summary>
        /// Find the binding behind the game's background-process indicator.
        /// （尋找遊戲背景程序指示器背後的繫結。）<br/><br/>
        /// <c>AppBindings.m_BackgroundProcessMessageBinding</c> is private, and the game exposes no
        /// public way to write it, so it is read reflectively — once, and cached. A failure here is
        /// cosmetic: the rip still runs, just without a caption.<br/>
        /// （<c>AppBindings.m_BackgroundProcessMessageBinding</c> 為私有，且遊戲未提供公開的寫入途徑，
        /// 故以反射讀取一次並快取。此處失敗僅影響外觀：擷取照常進行，只是沒有說明文字。）
        /// </summary>
        /// <returns>The binding, or <c>null</c> when it could not be reached.（繫結，若無法取得則為 <c>null</c>。）</returns>
        private static ValueBinding<string> ResolveBinding()
        {
            Game.UI.AppBindings bindings = Instance.UI?.appBindings;
            if (bindings == null)
            {
                if (!_bindingsMissingLogged)
                {
                    _bindingsMissingLogged = true;
                    _log.Info("AppBindings is not available yet; the progress indicator will stay hidden. 目前無法取得 AppBindings，進度指示器將維持隱藏。");
                }

                return null;
            }

            if (_bindingResolved) return _binding;
            _bindingResolved = true;

            // `System` resolves to Carto.IO's own enum inside this namespace, so the reflection
            // types need the global alias.（在此命名空間中 `System` 會解析為 Carto.IO 自身的枚舉，
            //   因此反射型別需使用全域別名。）
            global::System.Reflection.FieldInfo field = typeof(Game.UI.AppBindings).GetField("m_BackgroundProcessMessageBinding",
                global::System.Reflection.BindingFlags.Instance | global::System.Reflection.BindingFlags.NonPublic | global::System.Reflection.BindingFlags.Public);

            if (field == null)
            {
                _log.Info("The game's background-process indicator binding was not found; rips will run without an on-screen caption. 找不到遊戲的背景程序指示器繫結，擷取將不顯示畫面說明文字。");
                return null;
            }

            object value = field.GetValue(bindings);
            _binding = value as ValueBinding<string>;

            if (_binding == null)
            {
                _log.Info($"The background-process binding did not cast to ValueBinding<string> (it is {value?.GetType().FullName ?? "null"}); rips will run without an on-screen caption. 背景程序繫結的型別不符，擷取將不顯示畫面說明文字。");
            }

            return _binding;
        }

        /// <summary>
        /// Whether the missing-<c>AppBindings</c> notice has already been logged.
        /// （缺少 <c>AppBindings</c> 的提示是否已被記錄。）
        /// </summary>
        private static bool _bindingsMissingLogged;
    }
}
