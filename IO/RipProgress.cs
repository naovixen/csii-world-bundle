using Colossal.Logging;
using Colossal.UI.Binding;
using System;
using System.Threading;

namespace Carto.IO
{
    /// <summary>
    /// The world ripper's progress indicator, shown through the game's own background-process
    /// indicator (the spinner and caption the base game uses for autosaves).
    /// The indicator is driven by the <c>app.backgroundProcessMessage</c> UI binding, whose value
    /// is a <b>locale key</b>, not free text — the UI renders an unknown key as an empty string. The
    /// percentage is therefore baked into the key: <see cref="Carto.Utils.LocaleUtils"/> expands one
    /// translated template per phase into <c>Carto.PROGRESS[&lt;phase&gt;.&lt;percent&gt;]</c> entries
    /// at load time, so reporting progress is a binding update and never a dictionary rebuild.
    /// <b>Any thread may report.</b> Reporting only writes fields; the binding is touched solely by
    /// <see cref="Pump"/>, which <see cref="Carto.Systems.MeshRipSystem"/> calls once per frame on
    /// the main thread. Off-thread writers therefore need no marshalling of their own.
    /// </summary>
    public static class RipProgress
    {
        /// <summary>
        /// Mod's logger.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The locale key prefix the progress entries are generated under.
        /// </summary>
        public const string LocalePrefix = "Carto.PROGRESS";

        /// <summary>
        /// The phases a rip reports under. Each needs a matching <c>Carto.PROGRESS[&lt;name&gt;]</c>
        /// template in every locale file.
        /// </summary>
        public enum Phase
        {
            /// <summary>No work in progress; the indicator is hidden.</summary>
            None,

            /// <summary>Enumerating the world to build the work list.</summary>
            Scan,

            /// <summary>Removing the previous rip's outputs of the selected layers.</summary>
            Clean,

            /// <summary>Enumerating placed objects and their appearance.</summary>
            Instances,

            /// <summary>Walking the lane graph.</summary>
            Lanes,

            /// <summary>Reading the terrain, water and surface rasters.</summary>
            Terrain,

            /// <summary>Reading prefab geometry.</summary>
            Meshes,

            /// <summary>Assembling and bending network geometry.</summary>
            Networks,

            /// <summary>Draining the writer queue to disk.</summary>
            Write,

            /// <summary>The vanilla vector/raster export.</summary>
            Export,

            /// <summary>Assembling the bundle manifest.</summary>
            Manifest
        }

        /// <summary>
        /// The phase currently being reported.
        /// </summary>
        private static volatile Phase _phase = Phase.None;

        /// <summary>
        /// Completed and total units of work in the current phase.
        /// A <see cref="_total"/> of zero means indeterminate — the caption is shown without a
        /// percentage.
        /// </summary>
        private static int _current, _total;

        /// <summary>
        /// The locale key last pushed to the binding, so <see cref="Pump"/> is a no-op while the
        /// displayed percentage has not changed.
        /// </summary>
        private static string _pushed;

        /// <summary>
        /// Work queued by <see cref="RunDeferred"/>, and the number of frames it has waited.
        /// </summary>
        private static Action _deferred;
        private static string _deferredMessage;
        private static int _deferredFrames;

        /// <summary>
        /// The number of frames a deferred action waits before running.
        /// The point of deferring is that the caption reaches the screen <i>before</i> a blocking
        /// operation freezes the main thread. One frame sets the binding, the next lets the UI
        /// process and paint it.
        /// </summary>
        private const int DeferredDelayFrames = 2;

        /// <summary>
        /// Report progress within a phase.
        /// </summary>
        /// <param name="phase">The phase being worked on.</param>
        /// <param name="current">Units of work completed.</param>
        /// <param name="total">Total units of work, or <c>0</c> when indeterminate.</param>
        public static void Report(Phase phase, int current = 0, int total = 0)
        {
            _phase = phase;
            Interlocked.Exchange(ref _current, current);
            Interlocked.Exchange(ref _total, total);
        }

        /// <summary>
        /// Hide the indicator.
        /// </summary>
        public static void Clear() => Report(Phase.None);

        /// <summary>
        /// Show <paramref name="message"/>, then run <paramref name="action"/> a couple of frames
        /// later on the main thread.
        /// This exists for work that genuinely blocks — the vanilla vector export runs as one
        /// synchronous call — so that the user sees "Carto is working" rather than a frozen game
        /// with no explanation. It does not make the work itself interruptible.
        /// </summary>
        /// <param name="phase">The phase to caption it with.</param>
        /// <param name="action">The work to run.</param>
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
        /// to reach the screen. Must be called on the main thread, once per frame.
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
            //
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
        //
        // ====================================================================================

        /// <summary>
        /// The one notification a rip drives. A stable id makes every push an in-place update of
        /// the same toast rather than a growing stack.
        /// </summary>
        private const string NotificationId = "Carto.RipWorld";

        /// <summary>Whether the toast is currently showing.</summary>
        private static bool _toastShown;

        /// <summary>Consecutive <see cref="Pump"/> calls with no phase, for the linger window.</summary>
        private static int _idlePumps;

        /// <summary>Whether a toast failure has been logged.</summary>
        private static bool _toastFailedLogged;

        /// <summary>
        /// How many idle pumps the toast survives before it is completed and dismissed.
        /// The pipeline passes through <see cref="Phase.None"/> for a frame or two between
        /// stages (each stage clears the indicator as it finishes); popping and re-pushing across
        /// that gap would make the toast flicker once per stage. Pump runs a couple of times per
        /// frame, so this is roughly a second of real idleness.
        /// </summary>
        private const int ToastLingerPumps = 120;

        /// <summary>
        /// Create or update the rip's toast for the current phase and percentage.
        /// The text is the same per-phase locale key the caption uses — the progress bar carries
        /// the percentage, so the pre-expanded percent keys are not needed here. Only called when
        /// the built key changed, so an unchanged percentage costs nothing.
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
        /// </summary>
        /// <returns>The locale key, or <c>null</c> when nothing should be shown.</returns>
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
        /// The binding lives on <c>AppBindings</c>, which does not exist before the UI is up and is
        /// not something a mod should require — a missing indicator must never fail a rip.
        /// </summary>
        /// <param name="key">The locale key, or <c>null</c> to hide the indicator.</param>
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
                //
                if (!_pushLogged && (key != null))
                {
                    _pushLogged = true;
                    _log.Info($"Background-process indicator updated with `{key}`; translated = `{Carto.Utils.LocaleUtils.Translate(key)}`.");
                }
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not update the background-process indicator: {ex}");
            }
        }

        /// <summary>
        /// Whether the first successful push has already been logged.
        /// </summary>
        private static bool _pushLogged;

        /// <summary>
        /// The game's background-process binding, resolved once and cached.
        /// </summary>
        private static ValueBinding<string> _binding;

        /// <summary>
        /// Whether resolving the binding has already been attempted.
        /// </summary>
        private static bool _bindingResolved;

        /// <summary>
        /// Find the binding behind the game's background-process indicator.
        /// <c>AppBindings.m_BackgroundProcessMessageBinding</c> is private, and the game exposes no
        /// public way to write it, so it is read reflectively — once, and cached. A failure here is
        /// cosmetic: the rip still runs, just without a caption.
        /// </summary>
        /// <returns>The binding, or <c>null</c> when it could not be reached.</returns>
        private static ValueBinding<string> ResolveBinding()
        {
            Game.UI.AppBindings bindings = Instance.UI?.appBindings;
            if (bindings == null)
            {
                if (!_bindingsMissingLogged)
                {
                    _bindingsMissingLogged = true;
                    _log.Info("AppBindings is not available yet; the progress indicator will stay hidden.");
                }

                return null;
            }

            if (_bindingResolved) return _binding;
            _bindingResolved = true;

            // `System` resolves to Carto.IO's own enum inside this namespace, so the reflection
            // types need the global alias.
            global::System.Reflection.FieldInfo field = typeof(Game.UI.AppBindings).GetField("m_BackgroundProcessMessageBinding",
                global::System.Reflection.BindingFlags.Instance | global::System.Reflection.BindingFlags.NonPublic | global::System.Reflection.BindingFlags.Public);

            if (field == null)
            {
                _log.Info("The game's background-process indicator binding was not found; rips will run without an on-screen caption.");
                return null;
            }

            object value = field.GetValue(bindings);
            _binding = value as ValueBinding<string>;

            if (_binding == null)
            {
                _log.Info($"The background-process binding did not cast to ValueBinding<string> (it is {value?.GetType().FullName ?? "null"}); rips will run without an on-screen caption.");
            }

            return _binding;
        }

        /// <summary>
        /// Whether the missing-<c>AppBindings</c> notice has already been logged.
        /// </summary>
        private static bool _bindingsMissingLogged;
    }
}
