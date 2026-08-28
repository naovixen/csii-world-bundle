/* cs2beamng UI - vanilla JS, ES modules. Talks to the server: JSON API + one SSE stream.

   This file is the wiring: it binds the buttons that are in `index.html` and starts the page.
   Everything it calls lives in a module named for what it draws. */

import { $, TOKEN, api, app } from './core.js';
import { appendLog, loadCensus, renderBuild, resetLog, setWorker } from './progress.js';
import { loadSettings } from './settings.js';

// ---- actions -----------------------------------------------------------------------------

export async function startBuild() {
  $("startBtn").disabled = true;
  resetLog();                                                  // before the POST: the new build's first lines arrive at once
  $("resultsCard").hidden = true;
  let res = null;
  try { res = await api("/api/start", { method: "POST" }); } catch (e) { res = { ok: false, error: String(e) }; }
  if (!res || !res.ok) {
    const banner = $("banner");
    banner.hidden = false; banner.className = "banner danger";
    banner.textContent = (res && res.problems && res.problems.join(" · ")) || (res && res.error) || "Could not start.";
    $("startBtn").disabled = false;
    await restoreLog();                                        // nothing started: bring the previous build's log back
    if (app.build) { app.resultsFor = null; renderBuild(app.build); }
    return;
  }
  app.resultsFor = null;
  if (res.state) renderBuild(res.state);
}

export async function restoreLog() {
  try {
    const st = await api("/api/state");
    resetLog();
    app.lastSeq = 0;
    appendLog(st.log || []);
  } catch (_) { /* nothing to restore */ }
}

export async function cancelBuild() {
  $("cancelBtn").disabled = true;
  await api("/api/cancel", { method: "POST" });
}

export async function quit() {
  const running = app.build && app.build.status === "running";
  if (!window.confirm(running ? "A build is running. Cancel it and quit?" : "Quit cs2beamng?")) return;
  try { await api("/api/quit", { method: "POST" }); } catch (_) { /* server gone */ }
  showClosed();
}

export function showClosed() {
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:var(--text-dim);font:15px var(--font)">cs2beamng closed. You can close this tab.</div>';
}

function connectEvents() {
  if (app.events) { app.events.close(); app.events = null; }
  const source = new EventSource(`/events?t=${encodeURIComponent(TOKEN)}&since=${app.lastSeq}`);
  source.addEventListener("state", (ev) => renderBuild(JSON.parse(ev.data)));
  source.addEventListener("log", (ev) => appendLog(JSON.parse(ev.data)));
  source.addEventListener("bye", () => { source.close(); showClosed(); });
  source.onerror = () => {
    source.close();
    app.events = null;
    setWorker("reconnecting…", "busy");
    setTimeout(connectEvents, 1000);
  };
  app.events = source;
}

// ---- boot --------------------------------------------------------------------------------

$("version").textContent = window.CS2BEAMNG.version;
$("startBtn").addEventListener("click", startBuild);
$("cancelBtn").addEventListener("click", cancelBuild);
$("quitBtn").addEventListener("click", quit);
document.querySelectorAll("[data-open]").forEach((b) => {
  b.addEventListener("click", () => api("/api/open", { method: "POST", body: { what: b.dataset.open } }));
});
loadSettings().then(loadCensus).catch((e) => { $("bundleStatus").textContent = String(e); });
connectEvents();
