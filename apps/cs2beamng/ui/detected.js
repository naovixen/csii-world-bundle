/* What this machine offers: the three paths the app found for you, and whether the ripper
   mod is installed. Both are answers about the machine, not about the level. */

import { $, api, app, el } from './core.js';
import { commit, loadSettings } from './settings.js';

export function renderDetected(detected, canBrowse) {
  document.querySelectorAll(".browse").forEach((b) => { b.hidden = !canBrowse; });
  detected = detected || {};
  const values = (app.settings && app.settings.values) || {};

  renderDetectLine($("bundleDetect"), {
    current: values.bundle,
    options: detected.bundles || [],
    key: "bundle",
    okText: "Found your CS2 export",
    noneText: "No bundle here yet. Rip one in CS2: Options → Carto → Ripper → Rip World.",
  });
  const outCtl = app.controls.get("output");
  if (outCtl && outCtl.detect) {
    renderDetectLine(outCtl.detect, {
      current: values.output,
      options: detected.levelsDirs || [],
      key: "output",
      okText: "Found your BeamNG levels folder",
      noneText: "No BeamNG userfolder found — run BeamNG.drive once, or pick the folder yourself.",
    });
  }
  const exeCtl = app.controls.get("beamng_exe");
  if (exeCtl && exeCtl.detect) {
    const exe = detected.beamngExe;
    exeCtl.detect.innerHTML = "";
    exeCtl.detect.appendChild(el("span", exe ? "good" : "warn",
      exe ? `Found BeamNG: ${exe}` : "BeamNG not found — the level will ship uncooked sources."));
  }
  renderModCallout();
}

export function renderDetectLine(host, opts) {
  if (!host) return;
  host.innerHTML = "";
  const inUse = opts.options.some((p) => samePath(p, opts.current));
  if (inUse) {
    host.appendChild(el("span", "good", `${opts.okText}: ${opts.current}`));
  } else if (opts.options.length) {
    host.appendChild(el("span", "warn", `${opts.okText} somewhere else:`));
    for (const path of opts.options) {
      const b = el("button", "btn tiny", path);
      b.type = "button";
      b.onclick = () => commit(opts.key, path);
      host.appendChild(b);
    }
  } else {
    host.appendChild(el("span", "bad", opts.noneText));
  }
}

export const samePath = (a, b) => !!a && !!b &&
  String(a).replace(/[\\/]+$/, "").toLowerCase() === String(b).replace(/[\\/]+$/, "").toLowerCase();

/* The producer half. A release build ships the ripper mod beside the exe, so the app can put
   it in the CS2 mods folder itself — which is the difference between one download and two. */
export function renderModCallout() {
  const box = $("modCallout");
  const mod = (app.settings && app.settings.mod) || {};
  const haveBundle = ((app.settings && app.settings.detected && app.settings.detected.bundles) || []).length > 0;
  box.innerHTML = "";
  if (mod.installed && haveBundle) { box.hidden = true; return; }
  box.hidden = false;
  if (mod.installed) {
    box.appendChild(el("div", "", `The ripper mod is installed (version ${mod.installed}). Start CS2, load your city, then Options → Carto → Ripper → Rip World.`));
    return;
  }
  box.appendChild(el("div", "", "The ripper mod is not installed in Cities: Skylines II — that mod is what writes the bundle this app reads."));
  if (mod.canInstall) {
    const b = el("button", "btn small primary", `Install the ripper mod${mod.bundled ? " " + mod.bundled : ""}`);
    b.type = "button";
    b.onclick = () => installMod(b);
    box.appendChild(b);
    if (mod.dir) box.appendChild(el("div", "muted", `It goes in ${mod.dir}`));
  } else {
    box.appendChild(el("div", "muted",
      mod.dir ? `Download CartoRipper-*.zip from the releases page and unzip it into ${mod.dir}`
              : "Run Cities: Skylines II once so it creates its user folder, then press Rescan."));
  }
}

export async function installMod(button) {
  button.disabled = true;
  button.textContent = "Installing…";
  const res = await api("/api/installmod", { method: "POST", body: {} });
  if (res && res.ok) {
    await loadSettings();
    const box = $("modCallout");
    box.appendChild(el("div", "good", `Installed into ${res.dir}. Start CS2 and rip your city.`));
  } else {
    button.disabled = false;
    button.textContent = "Install the ripper mod";
    $("modCallout").appendChild(el("div", "bad", (res && res.error) || "could not install it"));
  }
}

/* Whether a SUBSET setting is live, derived from its parent's value rather than from a list
   kept by hand beside it. `subsetOf` comes from the metadata, so a new child indents, greys
   out and re-enables itself with no edit here. */
