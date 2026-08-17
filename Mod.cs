using Carto.Systems;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game;
using Game.Modding;

namespace Carto
{
    public class Mod : IMod
    {
        /// <summary>
        /// Mod's logger.（模組的記錄器。）<br/>
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The event triggered when the mod instance is being loaded.
        /// （當模組實例被載入時觸發的事件。）
        /// </summary>
        /// <param name="updateSystem">The system managing game updates.（管理遊戲狀態更新的系統。）</param>
        public void OnLoad(UpdateSystem updateSystem)
        {
            // Register settings in the options page.
            // （將設定註冊在選項頁面。）
            Instance.Settings = new Settings(this);
            Instance.Settings.RegisterInOptionsUI();
            AssetDatabase.global.LoadSettings(nameof(Carto), Instance.Settings, new Settings(this));

            // Load the locales to the game.
            // （載入語系檔案至遊戲中。）
            LocaleUtils.Load();

            // Register system instances into the game.
            // （將系統實例註冊在遊戲中。）
            updateSystem.UpdateBefore<AreaSystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<BuildingSystem>(SystemUpdatePhase.GameSimulation);
            // The instance and lane walks advance a slice per frame from OnUpdate and are started
            // from the (simulation-pausing) options page — MainLoop, like the other rip systems.
            // InstanceRipSystem used to sit at GameSimulation, which was fine while its work was a
            // single blocking call; a frame-budgeted pass there would freeze whenever the
            // simulation pauses.
            // （實例與車道走訪於 OnUpdate 中每幀推進一段，且由（暫停模擬的）選項頁面啟動——
            //   與其他擷取系統相同，故用 MainLoop。InstanceRipSystem 過去位於 GameSimulation，
            //   當其工作為單一阻塞呼叫時尚無妨；分幀走訪在該處會於模擬暫停時凍結。）
            updateSystem.UpdateAt<InstanceRipSystem>(SystemUpdatePhase.MainLoop);
            updateSystem.UpdateAt<LaneRipSystem>(SystemUpdatePhase.MainLoop);
            // MainLoop, not the simulation: a rip is driven a slice per frame from OnUpdate, and it
            // is started from the options page — which pauses the simulation, so a simulation-phase
            // system would sit frozen exactly while the user is watching it.
            // （使用 MainLoop 而非模擬階段：擷取由 OnUpdate 每幀推進一段，且由選項頁面啟動，
            //   而該頁面會暫停模擬，模擬階段的系統將恰好在使用者注視時停滯。）
            updateSystem.UpdateAt<MeshRipSystem>(SystemUpdatePhase.MainLoop);
            // Registered for its lifecycle, not for a tick: MaterialRipSystem does no per-frame work
            // of its own, but a rip abandoned when the world unloads must be released in OnDestroy.
            // （註冊是為了生命週期而非逐幀更新：材質擷取系統本身沒有逐幀工作，
            //   但世界卸載時中止的擷取必須於 OnDestroy 中釋放。）
            updateSystem.UpdateAt<MaterialRipSystem>(SystemUpdatePhase.MainLoop);
            updateSystem.UpdateBefore<NetworkSystem>(SystemUpdatePhase.GameSimulation);
            // MainLoop for the same reason as the mesh rip: a network rip advances a slice per
            // frame from OnUpdate and is started from the options page, which pauses the
            // simulation - so a simulation-phase system would sit frozen exactly while the user
            // is watching its progress bar.
            updateSystem.UpdateAt<NetworkRipSystem>(SystemUpdatePhase.MainLoop);
            // The pipeline behind the one-button rip advances between stages from OnUpdate, and it
            // too is started from the (simulation-pausing) options page — MainLoop for the same
            // reason as the mesh and network rips.
            // （一鍵擷取背後的管線於 OnUpdate 中在階段間推進，且同樣由（暫停模擬的）選項頁面啟動——
            //   與網格及路網擷取相同，故用 MainLoop。）
            updateSystem.UpdateAt<RipOrchestratorSystem>(SystemUpdatePhase.MainLoop);
            updateSystem.UpdateAt<TerrainRipSystem>(SystemUpdatePhase.MainLoop);
            updateSystem.UpdateBefore<POISystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<RasterSystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<RouteSystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<SharedDataCollectionSystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<SoundSystem>(SystemUpdatePhase.GameSimulation);
            updateSystem.UpdateBefore<ZoningSystem>(SystemUpdatePhase.GameSimulation);

            // Create neccessary folders.
            //（創造必要的資料夾。）
            IOUtils.CreateUserDataDirectories();

            // Extract QGIS style presets.
            // （萃取 QGIS 樣式模板。）
            IOUtils.ExtractEmbeddedStyles(IOUtils.CombinePath(Instance.CartoDataPath, "Styles"));

            _log.Info("Mod instance loaded. 模組實例載入完成。");
        }

        /// <summary>
        /// The event triggered when the mod instance is being disposed of.
        /// （當模組實例被銷毀時觸發的事件。）
        /// </summary>
        public void OnDispose()
        {
            if (Instance.Settings != null)
            {
                Instance.Settings.UnregisterInOptionsUI();
                Instance.Settings = null;
            }

            _log.Info("Mod instance had been disposed of. 模組實例已被銷毀。");
        }
    }
}
