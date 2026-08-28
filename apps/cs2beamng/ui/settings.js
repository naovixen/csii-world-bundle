/* The settings screen: what the server serves, the preset row, the basics strip, the sidebar,
   search, the reference page, and putting a fresh set of values back into the controls. */

import { buildControl, childrenOff, updateDependencies } from './controls.js';
import { $, api, app, el, titleOf } from './core.js';
import { renderCost } from './cost.js';
import { renderDetected } from './detected.js';
import { scheduleCensus } from './progress.js';

// ---- settings ----------------------------------------------------------------------------
//
// Three levels of structure and no more (E2). Level one is the basics strip in the Level card
// — the six things a person must supply — plus the cost meter under it. Level two is the
// sidebar: one section on screen at a time, as a heading and a flat list. There is no level
// three: no accordions inside a section, no second tier of "advanced" inside a pane. The
// sidebar itself is built from `payload.sections`, so a new section is a data change.

export async function loadSettings() {
  renderSettings(await api("/api/settings"));
}

export function renderSettings(payload) {
  if (!payload || !payload.values) return;
  app.settings = payload;
  app.byKey = Object.fromEntries((payload.schema || []).map((s) => [s.key, s]));
  if (!app.shellBuilt) buildShell(payload);
  if (!app.paneBuilt) renderPane();
  applyValues(payload.values);
  renderProblems(payload.problems || [], payload.errors || []);
  renderPresets(payload.values.quality_preset, payload.presetChanged);
  renderNav();
  renderDetected(payload.detected, payload.canBrowse);
  renderCost();
  awaitEstimate(payload.estimate);
  updateDependencies(payload.values);
  $("startBtn").disabled = (payload.problems || []).length > 0 || (app.build && app.build.status === "running");
}

/* The two slow halves of the estimate are worked out on a background thread, so the answer
   arrives after the response that said it was coming. Ask again while it is still working —
   the server memoises, so a poll that finds it done costs one settings round trip. */
export function awaitEstimate(est) {
  clearTimeout(app.estimateTimer);
  if (!est || est.ready || !["index", "reading"].includes(est.waiting)) return;
  app.estimateTimer = setTimeout(loadSettings, est.waiting === "index" ? 3000 : 800);
}

export const PRESET_ORDER = ["ultra", "high", "performance", "medium", "low"];

/* The preset row, with Custom always on it. `quality_preset` names the preset last applied,
   or "custom" once ANY quality setting has been moved; the server keeps the custom values and
   the preset they came from in a file of their own, so pressing Custom comes back to exactly
   what you had. Looking at what Performance does therefore costs nothing and loses nothing,
   which is the whole reason the button is always there rather than only after the fact. */
export function renderPresets(active, changed) {
  const box = $("presets");
  const settings = app.settings || {};
  const info = settings.presetInfo || {};
  const known = settings.presets ? Object.keys(settings.presets) : [];
  const names = PRESET_ORDER.filter((n) => known.includes(n)).concat(known.filter((n) => !PRESET_ORDER.includes(n)));
  const custom = active === "custom";
  const base = settings.presetBase;
  const dirty = (changed || []).length;
  box.innerHTML = "";
  for (const name of names) {
    const b = el("button", name === active ? "active" : "", titleOf(name));
    b.type = "button";
    b.title = info[name] || "";
    b.onclick = () => applyPreset(name);
    if (name === recommendedPreset()) b.classList.add("recommended");
    if (custom && name === base) b.classList.add("base");
    box.appendChild(b);
  }
  const own = el("button", "custom" + (custom ? " active" : ""), "Custom");
  own.type = "button";
  own.disabled = !custom && !settings.hasCustom;
  own.title = own.disabled
    ? "Change any setting and it is kept here"
    : "Your own settings" + (base ? `, from ${titleOf(base)}` : "");
  own.onclick = () => applyPreset("custom");
  box.appendChild(own);

  const line = $("presetInfo");
  if (custom) {
    line.textContent = base && dirty
      ? `Your own settings — ${dirty} of them changed from ${titleOf(base)}. The sidebar marks where.`
      : "Your own settings.";
  } else {
    line.textContent = info[active] || "";
  }
  const rec = recommendedPreset();
  if (rec && rec !== active) {
    line.appendChild(el("span", "rec-note", `  ·  ${titleOf(rec)} is the one that fits this machine.`));
  }
}

/* Which preset this machine can run, from the card and render size the app detected. The
   numbers are BeamNG's own: `resident_budget_mib` is what a level may mount on that card at
   that resolution, and the presets below are ordered by what they mount. */
export function recommendedPreset() {
  const d = app.settings && app.settings.detected;
  const vram = d && d.budget && d.budget.vramMiB;
  if (!vram) return null;
  if (vram >= 16000) return "ultra";
  if (vram >= 11000) return "high";
  if (vram >= 7000) return "performance";
  if (vram >= 5000) return "medium";
  return "low";
}

export function renderProblems(problems, errors) {
  const box = $("settingsProblems");
  const all = [...errors, ...problems];
  box.innerHTML = "";
  box.hidden = all.length === 0;
  for (const p of all) box.appendChild(el("div", "", p));
  for (const [key, ctl] of app.controls) {
    ctl.input.classList.toggle("invalid", all.some((p) => p.startsWith(key + ":")));
  }
}

// ---- the shell: basics strip, sidebar, search ----------------------------------------------

export function buildShell(payload) {
  app.shellBuilt = true;
  app.section = payload.values.ui_section || (payload.sections[0] && payload.sections[0].id) || "world";

  // the bundle path lives in the Bundle card, above the census it decides
  const bundleInput = $("in_bundle");
  bundleInput.addEventListener("change", () => commit("bundle", bundleInput.value));
  app.controls.set("bundle", { input: bundleInput, row: bundleInput.parentElement, apply: (v) => { bundleInput.value = v || ""; } });
  document.querySelectorAll(".browse").forEach((b) => b.addEventListener("click", () => browse(b.dataset.for)));
  $("rescanBtn").addEventListener("click", () => loadSettings());

  // the basics strip: what a person must supply, always on screen, never behind a section
  const strip = $("levelBasics");
  const paired = ["level_name", "level_title"];           // two short names sit on one line
  strip.innerHTML = "";
  let pair = null;
  for (const key of payload.basics || []) {
    if (key === "bundle") continue;                       // it is the Bundle card's own field
    const control = buildControl(app.byKey[key]);
    if (paired.includes(key)) {
      if (!pair) { pair = el("div", "basics-pair"); strip.appendChild(pair); }
      pair.appendChild(control);
    } else {
      strip.appendChild(control);
    }
  }

  const search = $("settingsSearch");
  search.addEventListener("input", () => { app.query = search.value.trim(); renderPane(); renderNav(); });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { search.value = ""; app.query = ""; renderPane(); renderNav(); }
  });
  document.addEventListener("keydown", (ev) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName);
    if ((ev.key === "/" && !typing) || (ev.key.toLowerCase() === "f" && (ev.ctrlKey || ev.metaKey))) {
      ev.preventDefault();
      search.focus();
      search.select();
    }
  });
  $("navList").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    ev.preventDefault();
    const items = [...$("navList").querySelectorAll(".nav-item")];
    const at = items.indexOf(document.activeElement);
    const next = items[(at + (ev.key === "ArrowDown" ? 1 : items.length - 1) + items.length) % items.length];
    if (next) { next.focus(); next.click(); }
  });
  $("referenceBtn").addEventListener("click", () => showSection(REFERENCE));
}

export const REFERENCE = "reference";

/* One place a control is registered. `app.paneKeys` records what the CONTENT PANE built, so
   that rebuilding it can drop exactly those and leave the basics strip - which is built once
   and never rebuilt - alone. */
export let inPane = false;

export function setControl(key, entry) {
  app.controls.set(key, entry);
  if (inPane) app.paneKeys.add(key);
}

export function sectionById(id) {
  return ((app.settings && app.settings.sections) || []).find((s) => s.id === id) || null;
}

export const keysOfSection = (sec) => sec.blocks.reduce((a, b) => a.concat(b.keys), []);

export function showSection(id) {
  app.section = id;
  const search = $("settingsSearch");
  if (search.value) { search.value = ""; app.query = ""; }
  renderPane();
  renderNav();
  if (id !== REFERENCE) commit("ui_section", id);
}

/* The sidebar. Grouped headings, a badge counting what you have moved off the preset, and a
   warning marker where a section is what is pushing the cost meter over budget. Built from
   the data every time, so a section added on the server appears here with no edit. */
export function renderNav() {
  const nav = $("navList");
  const sections = (app.settings && app.settings.sections) || [];
  const changed = new Set((app.settings && app.settings.presetChanged) || []);
  const over = new Set((app.settings && app.settings.estimate && app.settings.estimate.overSections) || []);
  nav.innerHTML = "";
  let group = null;
  for (const sec of sections) {
    if (sec.group !== group) {
      group = sec.group;
      nav.appendChild(el("div", "nav-group", group));
    }
    const item = el("button", "nav-item" + (!app.query && app.section === sec.id ? " active" : ""));
    item.type = "button";
    item.setAttribute("role", "tab");
    item.title = sec.tagline || "";
    item.appendChild(el("span", "nav-label", sec.label));
    const n = keysOfSection(sec).filter((k) => changed.has(k)).length;
    if (n) {
      const badge = el("span", "nav-badge", String(n));
      badge.title = `${n} setting${n === 1 ? "" : "s"} here differ from the preset`;
      item.appendChild(badge);
    }
    if (over.has(sec.id)) {
      const warn = el("span", "nav-warn", "!");
      warn.title = "This section is what is pushing the level over your card";
      item.appendChild(warn);
    }
    item.onclick = () => showSection(sec.id);
    nav.appendChild(item);
  }
  $("referenceBtn").classList.toggle("active", app.section === REFERENCE && !app.query);
}

// ---- the content pane -----------------------------------------------------------------------

/* Rebuilt only when the section or the search changes — never on a value commit, which would
   take the keyboard out from under whoever is typing. `app.paneKeys` is what this pane
   registered, so the controls map can be trimmed to what is actually on screen. */
export function renderPane() {
  const pane = $("settingsPane");
  for (const key of (app.paneKeys || [])) app.controls.delete(key);
  app.paneKeys = new Set();
  pane.innerHTML = "";
  app.paneBuilt = true;
  inPane = true;
  try {
    if (app.query) renderSearchResults(pane);
    else if (app.section === REFERENCE) renderReference(pane);
    else renderSectionPane(pane, sectionById(app.section) || (app.settings.sections || [])[0]);
  } finally {
    inPane = false;
  }
  pane.scrollTop = 0;
  if (app.settings) { applyValues(app.settings.values); updateDependencies(app.settings.values); }
}

export function paneHead(pane, title, tagline) {
  const head = el("div", "pane-head");
  head.appendChild(el("h3", "", title));
  if (tagline) head.appendChild(el("span", "pane-tagline", tagline));
  pane.appendChild(head);
}

export function renderSectionPane(pane, sec) {
  if (!sec) return;
  paneHead(pane, sec.label, sec.tagline);
  if (sec.note) pane.appendChild(el("p", "pane-note", sec.note));
  for (const block of sec.blocks) {
    if (block.title) pane.appendChild(el("h4", "block-title", block.title));
    pane.appendChild(buildList(block.keys));
  }
}

export function buildList(keys) {
  const body = el("div", "pane-body");
  const specs = keys.map((k) => app.byKey[k]).filter(Boolean);
  if (specs.length && specs.every((s) => s.control === "switch")) body.classList.add("checks");
  for (const spec of specs) body.appendChild(buildControl(spec));
  return body;
}

/* Search is a first-class route into the settings, not a fallback: at seventy-nine controls
   "which section is that in?" is the commonest question there is, so every hit is listed
   under the name of the section it lives in and one click away from it. */
export function renderSearchResults(pane) {
  const q = app.query.toLowerCase();
  const sections = (app.settings && app.settings.sections) || [];
  let hits = 0;
  paneHead(pane, `Search`, `“${app.query}”`);
  for (const sec of sections) {
    const keys = keysOfSection(sec).filter((k) => {
      const s = app.byKey[k];
      return s && (s.label.toLowerCase().includes(q) || (s.blurb || "").toLowerCase().includes(q) ||
                   k.toLowerCase().includes(q));
    });
    if (!keys.length) continue;
    hits += keys.length;
    const head = el("h4", "block-title");
    const jump = el("button", "linkish", sec.label);
    jump.type = "button";
    jump.onclick = () => showSection(sec.id);
    head.appendChild(jump);
    pane.appendChild(head);
    pane.appendChild(buildList(keys));
  }
  if (!hits) pane.appendChild(el("p", "pane-note", "Nothing matches that. The reference page below lists every setting, including the ones no control drives."));
}

/* The permanent link at the foot of the sidebar. The measured arguments — what was tried,
   what it cost, why a default is the default — are not deleted and not softened; they are
   simply not printed under every control. This is the page they live on, and it is built from
   the same metadata the controls are, so it can never go stale against them. */
export function renderReference(pane) {
  paneHead(pane, "Every setting", "Including the ones another control now drives");
  pane.appendChild(el("p", "pane-note",
    "The name in this colour is what the command line takes after --set, and what your settings file holds."));
  const specs = (app.settings.schema || []).slice().sort((a, b) => a.key.localeCompare(b.key));
  for (const spec of specs) {
    const item = el("div", "ref-item");
    const head = el("div", "ref-head");
    head.appendChild(el("code", "ref-key", spec.key));
    head.appendChild(el("span", "ref-label", spec.label));
    head.appendChild(el("span", "ref-default", `default ${spec.default === null ? "—" : String(spec.default)}`));
    item.appendChild(head);
    item.appendChild(el("div", "ref-blurb", spec.blurb));
    if (spec.drivenBy) item.appendChild(el("div", "ref-driven", `No control of its own — set by ${spec.drivenBy}.`));
    if (spec.detail) item.appendChild(el("div", "ref-detail", spec.detail));
    else if (spec.help && spec.help !== spec.blurb) item.appendChild(el("div", "ref-detail", spec.help));
    pane.appendChild(item);
  }
}


export function applyValues(values) {
  for (const [key, ctl] of app.controls) {
    if (document.activeElement === ctl.input) continue;      // never fight the user's typing
    ctl.apply(values[key]);
  }
}

export async function commit(key, value) {
  const seq = ++app.commitSeq;
  const body = { [key]: value };
  const parent = childrenOff(key, value);
  if (parent) body[parent.key] = parent.value;
  const payload = await api("/api/settings", { method: "POST", body: { values: body } });
  if (seq !== app.commitSeq) return;                           // a newer commit's response already landed
  renderSettings(payload);
  scheduleCensus();
}

export async function applyPreset(name) {
  const payload = await api("/api/preset", { method: "POST", body: { name } });
  renderPane();                    // a preset moves controls this pane may not be showing
  renderSettings(payload);
  scheduleCensus();
}

export async function browse(key) {
  const current = app.settings && app.settings.values[key];
  const res = await api("/api/browse", { method: "POST", body: { key, current } });
  if (res && res.path) commit(key, res.path);
}

