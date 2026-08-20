/* cs2beamng UI — vanilla JS. Talks to server.py: JSON API + one SSE stream. */
(() => {
  "use strict";

  const TOKEN = window.CS2BEAMNG.token;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };

  const app = {
    settings: null,          // last /api/settings payload
    census: null,
    build: null,             // last state from SSE
    lastSeq: 0,
    resultsFor: null,        // finishedAt of the build whose results are on screen
    commitSeq: 0,            // ordering token for /api/settings responses
    controls: new Map(),     // key -> {input, row, apply(value), detect}
    paneKeys: new Set(),     // the controls the CONTENT PANE built, dropped when it rebuilds
    byKey: {},               // key -> its schema entry
    section: "world",        // which sidebar section is on screen
    query: "",               // the sidebar's search box
    shellBuilt: false,
    paneBuilt: false,
    events: null,
    censusTimer: null,
    logLines: 0,
  };

  // ---- transport ---------------------------------------------------------------------------

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: { "X-Token": TOKEN } };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok && !(data && (data.problems || data.error))) {
      throw new Error(`${path}: HTTP ${res.status}`);
    }
    return data;
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

  // ---- formatting --------------------------------------------------------------------------

  const fmtInt = (n) => (n === null || n === undefined) ? "–" : Number(n).toLocaleString();
  function fmtSec(s) {
    if (s === null || s === undefined || Number.isNaN(s)) return "?";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  }
  function fmtBytes(b) {
    if (!b && b !== 0) return "–";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(i === 0 ? 0 : (b < 10 ? 2 : 1))} ${units[i]}`;
  }
  const titleOf = (key) => key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

  // ---- settings ----------------------------------------------------------------------------
  //
  // Three levels of structure and no more (E2). Level one is the basics strip in the Level card
  // — the six things a person must supply — plus the cost meter under it. Level two is the
  // sidebar: one section on screen at a time, as a heading and a flat list. There is no level
  // three: no accordions inside a section, no second tier of "advanced" inside a pane. The
  // sidebar itself is built from `payload.sections`, so a new section is a data change.

  async function loadSettings() {
    renderSettings(await api("/api/settings"));
  }

  function renderSettings(payload) {
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
    updateDependencies(payload.values);
    $("startBtn").disabled = (payload.problems || []).length > 0 || (app.build && app.build.status === "running");
  }

  const PRESET_ORDER = ["ultra", "high", "performance", "medium", "low"];

  /* The preset row, with Custom always on it. `quality_preset` names the preset last applied,
     or "custom" once ANY quality setting has been moved; the server keeps the custom values and
     the preset they came from in a file of their own, so pressing Custom comes back to exactly
     what you had. Looking at what Performance does therefore costs nothing and loses nothing,
     which is the whole reason the button is always there rather than only after the fact. */
  function renderPresets(active, changed) {
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
  function recommendedPreset() {
    const d = app.settings && app.settings.detected;
    const vram = d && d.budget && d.budget.vramMiB;
    if (!vram) return null;
    if (vram >= 16000) return "ultra";
    if (vram >= 11000) return "high";
    if (vram >= 7000) return "performance";
    if (vram >= 5000) return "medium";
    return "low";
  }

  function renderProblems(problems, errors) {
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

  function buildShell(payload) {
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

  const REFERENCE = "reference";

  /* One place a control is registered. `app.paneKeys` records what the CONTENT PANE built, so
     that rebuilding it can drop exactly those and leave the basics strip - which is built once
     and never rebuilt - alone. */
  let inPane = false;

  function setControl(key, entry) {
    app.controls.set(key, entry);
    if (inPane) app.paneKeys.add(key);
  }

  function sectionById(id) {
    return ((app.settings && app.settings.sections) || []).find((s) => s.id === id) || null;
  }

  const keysOfSection = (sec) => sec.blocks.reduce((a, b) => a.concat(b.keys), []);

  function showSection(id) {
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
  function renderNav() {
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
  function renderPane() {
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

  function paneHead(pane, title, tagline) {
    const head = el("div", "pane-head");
    head.appendChild(el("h3", "", title));
    if (tagline) head.appendChild(el("span", "pane-tagline", tagline));
    pane.appendChild(head);
  }

  function renderSectionPane(pane, sec) {
    if (!sec) return;
    paneHead(pane, sec.label, sec.tagline);
    if (sec.note) pane.appendChild(el("p", "pane-note", sec.note));
    for (const block of sec.blocks) {
      if (block.title) pane.appendChild(el("h4", "block-title", block.title));
      pane.appendChild(buildList(block.keys));
    }
  }

  function buildList(keys) {
    const body = el("div", "pane-body");
    const specs = keys.map((k) => app.byKey[k]).filter(Boolean);
    if (specs.length && specs.every((s) => s.control === "switch")) body.classList.add("checks");
    for (const spec of specs) body.appendChild(buildControl(spec));
    return body;
  }

  /* Search is a first-class route into the settings, not a fallback: at seventy-nine controls
     "which section is that in?" is the commonest question there is, so every hit is listed
     under the name of the section it lives in and one click away from it. */
  function renderSearchResults(pane) {
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
  function renderReference(pane) {
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

  // ---- the cost meter -------------------------------------------------------------------------

  /* "Will this run on my machine?" — on screen the whole time, because it is the consequence of
     every setting rather than of any one of them. Two shapes: a BAR for a figure with a budget
     it can exceed, and a TILE for a figure with none. Every one of them states how it was
     arrived at, in its own words, under the number: an estimate nobody can weigh is an estimate
     nobody can trust. The bars arrive with the estimator; the tiles are here today, and an
     empty bar in the meantime would be a promise the app cannot keep. */
  function renderCost() {
    const host = $("costMeter");
    const est = (app.settings && app.settings.estimate) || null;
    const budget = (app.settings && app.settings.detected && app.settings.detected.budget) || null;
    host.innerHTML = "";

    const head = el("div", "cost-head");
    head.appendChild(el("h4", "", "What this will cost"));
    head.appendChild(el("span", "cost-target", budget
      ? `for a ${fmtInt(budget.vramMiB)} MiB card at ${(budget.pixels / 1e6).toFixed(2)} Mpixel`
      : "no graphics card detected"));
    host.appendChild(head);

    for (const row of (est && est.rows) || []) host.appendChild(buildCostBar(row));

    const tiles = el("div", "cost-tiles");
    const seconds = expectedSeconds();
    tiles.appendChild(buildCostTile("Build time", seconds === null ? "unknown" : fmtSec(seconds),
      seconds === null ? "no bundle counted yet" : "from the measured stage rates", null));
    if (est && est.diskBytes) {
      tiles.appendChild(buildCostTile("On disk", fmtBytes(est.diskBytes), est.diskBasis || "", null));
    }
    tiles.appendChild(buildCostTile("Your card holds",
      budget ? `${fmtInt(budget.residentMiB)} MiB` : "unknown",
      budget ? `of level, at ${(budget.pixels / 1e6).toFixed(2)} Mpixel` : "run BeamNG once so it logs a card",
      budget ? null : "warn"));
    host.appendChild(tiles);

    if (budget && budget.lighting && /basic/i.test(budget.lighting)) {
      host.appendChild(el("div", "cost-warn",
        `BeamNG is set to ${budget.lighting}, which never shades a point or spot light — street lights would cost and light nothing.`));
    }
  }

  function buildCostTile(label, value, basis, band) {
    const tile = el("div", "cost-tile" + (band ? " " + band : ""));
    tile.appendChild(el("span", "cost-tile-k", label));
    tile.appendChild(el("span", "cost-tile-v", value));
    if (basis) tile.appendChild(el("span", "cost-tile-basis", basis));
    return tile;
  }

  /* The one expected-build-time figure, from wherever it is: the census computes it from the
     unit counts and the measured per-stage rates, and a build that has planned its stages
     carries the same sum. The meter and the Build card must never print two different ones. */
  function expectedSeconds() {
    const counted = app.census && app.census.totalSeconds;
    if (counted !== null && counted !== undefined) return counted;
    const stages = app.build && app.build.stages;
    if (stages && stages.length) return stages.reduce((a, s) => a + (s.estimateSeconds || 0), 0);
    return null;
  }

  function buildCostBar(row) {
    const line = el("div", "cost-row " + (row.band || "fits"));
    const top = el("div", "cost-row-head");
    top.appendChild(el("span", "cost-name", row.label));
    top.appendChild(el("span", "cost-value", row.value));
    if (row.of) top.appendChild(el("span", "cost-of", row.of));
    line.appendChild(top);
    const bar = el("div", "bar cost-bar");
    const fill = el("div", "fill");
    fill.style.width = `${Math.max(0, Math.min(100, Math.round((row.fraction || 0) * 100)))}%`;
    bar.appendChild(fill);
    if (row.markFraction) {
      const mark = el("div", "cost-mark");
      mark.style.left = `${Math.max(0, Math.min(100, row.markFraction * 100))}%`;
      bar.appendChild(mark);
    }
    line.appendChild(bar);
    const note = el("div", "cost-note");
    note.appendChild(el("span", "cost-basis", row.basis || ""));
    if (row.advice) {
      note.appendChild(el("span", "cost-advice", " " + row.advice));
      if (row.section) {
        const jump = el("button", "linkish", "What should I change?");
        jump.type = "button";
        jump.onclick = () => showSection(row.section);
        note.appendChild(jump);
      }
    }
    line.appendChild(note);
    return line;
  }

  /* Detection was always there — the app finds the bundle, the BeamNG userfolder, the exe and
     the card by itself. It was rendered as a hidden <select>, so nobody saw it. Now it says what
     it found, in words, with one button per thing you might want to do about it. */
  function renderDetected(detected, canBrowse) {
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

  function renderDetectLine(host, opts) {
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

  const samePath = (a, b) => !!a && !!b &&
    String(a).replace(/[\\/]+$/, "").toLowerCase() === String(b).replace(/[\\/]+$/, "").toLowerCase();

  /* The producer half. A release build ships the ripper mod beside the exe, so the app can put
     it in the CS2 mods folder itself — which is the difference between one download and two. */
  function renderModCallout() {
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

  async function installMod(button) {
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
  function parentIsOn(parentKey, values) {
    const off = (app.settings && app.settings.parentOff) || {};
    const value = values[parentKey];
    if (parentKey in off) return value !== off[parentKey];
    if (typeof value === "boolean") return value;
    if (typeof value === "string") return value.length > 0;
    return !!value;
  }

  function updateDependencies(values) {
    const settings = app.settings || {};
    const gated = Object.assign({}, settings.subsetOf || {}, settings.needs || {});
    for (const [key, parent] of Object.entries(gated)) {
      const ctl = app.controls.get(key);
      if (!ctl) continue;
      const on = parentIsOn(parent, values);
      ctl.row.classList.toggle("disabled", !on);
      for (const input of ctl.row.querySelectorAll("input, select, button.browse")) input.disabled = !on;
    }
  }

  /* Switching the LAST thing under a parent off switches the parent off. A parent with nothing
     left under it is off, and a level that ships an empty layer because the UI let the two
     disagree is the bug this closes. It runs in the UI, where the boxes are: `--set` stays
     literal, so a headless build still does exactly and only what it was told. */
  function childrenOff(key, value) {
    const settings = app.settings;
    if (!settings) return null;
    const parent = (settings.subsetOf || {})[key];
    if (!parent) return null;
    const spec = app.byKey[key];
    const values = Object.assign({}, settings.values, { [key]: value });
    const siblings = Object.entries(settings.subsetOf || {})
      .filter(([, p]) => p === parent)
      .map(([k]) => k)
      .filter((k) => app.byKey[k] && (app.byKey[k].control === "switch" || app.byKey[k].control === "categories"));
    if (!siblings.length || !(spec.control === "switch" || spec.control === "categories")) return null;
    const anyOn = siblings.some((k) => {
      const v = values[k];
      return app.byKey[k].control === "categories" ? String(v || "").trim().length > 0 : !!v;
    });
    if (anyOn || !parentIsOn(parent, values)) return null;
    return { key: parent, value: ((settings.parentOff || {})[parent] !== undefined) ? settings.parentOff[parent] : false };
  }

  // ---- the controls ---------------------------------------------------------------------------

  function buildControl(spec) {
    if (!spec) return el("div", "");
    if (spec.control === "region") return buildRegionControl(spec);
    const key = spec.key;
    const row = el("div", "setting");
    const name = el("div", "name", spec.label);
    const ctl = el("div", "ctl");
    let input;
    let apply;
    if (spec.control === "switch") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.addEventListener("change", () => commit(key, input.checked));
      apply = (v) => { input.checked = !!v; };
      ctl.appendChild(input);
      row.classList.add("switchrow");
    } else if (spec.control === "percent" || spec.control === "distance") {
      const range = spec.range || { min: 0, max: 100, step: 1, suffix: "" };
      input = document.createElement("input");
      input.type = "range";
      input.min = range.min; input.max = range.max; input.step = range.step;
      const val = el("span", "val", "");
      const show = (v) => {
        val.textContent = (spec.zeroLabel && Number(v) === 0) ? spec.zeroLabel : v + range.suffix;
        val.classList.toggle("zero", !!spec.zeroLabel && Number(v) === 0);
      };
      input.addEventListener("input", () => show(input.value));
      input.addEventListener("change", () => commit(key, Number(input.value)));
      apply = (v) => { input.value = v; show(input.value); };
      ctl.appendChild(input);
      ctl.appendChild(val);
    } else if (spec.control === "clock") {
      return buildClockControl(spec);
    } else if (spec.control === "categories") {
      return buildCategoriesControl(spec);
    } else if (spec.control === "segmented") {
      input = document.createElement("select");
      for (const c of spec.choices) input.appendChild(new Option(choiceLabel(key, c), c));
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = String(v); };
      ctl.appendChild(input);
    } else if (spec.control === "choice") {
      input = document.createElement("select");
      for (const c of spec.choices) input.appendChild(new Option(choiceLabel(key, c), c));
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = String(v); };
      ctl.appendChild(input);
    } else if (spec.control === "number") {
      input = document.createElement("input");
      input.type = "number";
      if (spec.kind === "int") input.step = 1;
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = (v === null || v === undefined) ? "" : v; };
      ctl.appendChild(input);
      // "0.8" with nothing after it has not been answered: metres? feet? a fraction of one?
      if (spec.unit) ctl.appendChild(el("span", "unit", spec.unit));
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.spellcheck = false;
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = (v === null || v === undefined) ? "" : v; };
      ctl.appendChild(input);
      if (spec.control === "path") {
        const b = el("button", "btn small browse", "Browse");
        b.type = "button"; b.dataset.for = key; b.hidden = true;
        b.addEventListener("click", () => browse(key));
        ctl.appendChild(b);
        row.classList.add("wide");
      }
    }
    input.id = "in_" + key;
    if (spec.subsetOf) row.classList.add("subset");        // indented under the thing it belongs to
    row.appendChild(name);
    row.appendChild(ctl);
    row.appendChild(el("div", "help", spec.blurb));
    let detect = null;
    if (key === "output" || key === "beamng_exe") {
      detect = el("div", "detect");
      row.appendChild(detect);
    }
    setControl(key, { input, row, apply, detect });
    return row;
  }

  function rowShell(spec, extraClass) {
    const row = el("div", "setting wide" + (extraClass ? " " + extraClass : ""));
    row.appendChild(el("div", "name", spec.label));
    return row;
  }

  /* A comma-separated string whose set of valid answers is fixed and small, as that set of
     checkboxes. `choiceSet` comes from the metadata, so this serves the roadside-furniture
     kinds and the recolouring kinds alike — and both were boxes nothing on screen told you how
     to fill in. Written back as the same string the command line takes. */
  function buildCategoriesControl(spec) {
    const row = rowShell(spec, "categories");
    if (spec.subsetOf) row.classList.add("subset");
    const ctl = el("div", "ctl checkset");
    const choices = spec.choiceSet || [];
    const boxes = [];
    const push = () => commit(spec.key, boxes.filter((b) => b.checked).map((b) => b.dataset.id).join(","));
    for (const choice of choices) {
      const wrap = el("label", "check");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.dataset.id = choice.id;
      box.addEventListener("change", push);
      wrap.appendChild(box);
      wrap.appendChild(el("span", "", choice.label));
      ctl.appendChild(wrap);
      boxes.push(box);
    }
    const apply = (v) => {
      const on = new Set(String(v || "").split(",").map((s) => s.trim()).filter(Boolean));
      for (const box of boxes) box.checked = on.has(box.dataset.id);
    };
    row.appendChild(ctl);
    row.appendChild(el("div", "help", spec.blurb));
    setControl(spec.key, { input: boxes[0] || ctl, row, apply, detect: null });
    return row;
  }

  /* Time of day is a number where -1 means "whatever hour the city was ripped at". A number box
     cannot say that, so the control is the choice it actually is. */
  function buildClockControl(spec) {
    const row = rowShell(spec, "clock");
    const ctl = el("div", "ctl");
    const mode = document.createElement("select");
    mode.appendChild(new Option("The hour your city was ripped at", "rip"));
    mode.appendChild(new Option("A time you pick", "pick"));
    const slider = document.createElement("input");
    slider.type = "range"; slider.min = 0; slider.max = 24; slider.step = 0.25;
    const val = el("span", "val", "");
    const clock = (h) => `${String(Math.floor(h)).padStart(2, "0")}:${String(Math.round((h % 1) * 60)).padStart(2, "0")}`;
    const sync = () => {
      const pick = mode.value === "pick";
      slider.hidden = !pick;
      val.hidden = !pick;
    };
    mode.addEventListener("change", () => { sync(); commit(spec.key, mode.value === "pick" ? Number(slider.value) : -1); });
    slider.addEventListener("input", () => { val.textContent = clock(Number(slider.value)); });
    slider.addEventListener("change", () => commit(spec.key, Number(slider.value)));
    const apply = (v) => {
      const n = Number(v);
      mode.value = n < 0 ? "rip" : "pick";
      slider.value = n < 0 ? 12 : n;
      val.textContent = clock(Number(slider.value));
      sync();
    };
    ctl.appendChild(mode);
    ctl.appendChild(slider);
    ctl.appendChild(val);
    slider.id = "in_" + spec.key;
    row.appendChild(ctl);
    row.appendChild(el("div", "help", spec.blurb));
    setControl(spec.key, { input: slider, row, apply, detect: null });
    return row;
  }

  /* `region` is a string ("X Z R", empty = the whole city) and reads as one. As a control it is
     a choice — all of it, or a disc round a point — so it is built as one. The map that makes
     those three numbers answerable is E3; until it exists they are at least labelled. */
  function buildRegionControl(spec) {
    const row = rowShell(spec);
    const ctl = el("div", "ctl region");
    const mode = document.createElement("select");
    mode.appendChild(new Option("The whole city", "all"));
    mode.appendChild(new Option("Part of it", "part"));
    const input = document.createElement("input");
    input.type = "text";
    input.spellcheck = false;
    input.placeholder = "X Z radius, in metres — e.g. 2994 1853 200";
    input.className = "region-value";
    const sync = () => {
      const part = mode.value === "part";
      input.hidden = !part;
      row.classList.toggle("region-part", part);
    };
    mode.addEventListener("change", () => {
      sync();
      if (mode.value === "all") commit("region", "");
      else input.focus();
    });
    input.addEventListener("change", () => commit("region", input.value.trim()));
    const apply = (v) => {
      const text = (v === null || v === undefined) ? "" : String(v);
      input.value = text;
      mode.value = text.trim() ? "part" : "all";
      sync();
    };
    ctl.appendChild(mode);
    ctl.appendChild(input);
    input.id = "in_region";
    row.appendChild(ctl);
    row.appendChild(el("div", "help", spec.blurb));
    setControl("region", { input, row, apply, detect: null });
    return row;
  }

  function choiceLabel(key, c) {
    if (key.startsWith("texture_max")) return c === "0" ? "full size" : `${c} px`;
    if (key === "terrain_upsample") return c === "1" ? "Standard" : "Fine — sharper underfoot, more memory";
    if (key === "collision_preset") return { exact: "Exact shapes", simplified: "Simple boxes", off: "Nothing solid" }[c] || c;
    if (key === "terrain_holes") return { contain: "Only where the hole is fully covered", majority: "Wherever most of it is", none: "Never" }[c] || c;
    if (key === "ui_browser") return { window: "A window of its own", chrome: "Chrome", edge: "Edge", firefox: "Firefox", default: "Your usual browser" }[c] || c;
    return c;
  }

  function applyValues(values) {
    for (const [key, ctl] of app.controls) {
      if (document.activeElement === ctl.input) continue;      // never fight the user's typing
      ctl.apply(values[key]);
    }
  }

  async function commit(key, value) {
    const seq = ++app.commitSeq;
    const body = { [key]: value };
    const parent = childrenOff(key, value);
    if (parent) body[parent.key] = parent.value;
    const payload = await api("/api/settings", { method: "POST", body: { values: body } });
    if (seq !== app.commitSeq) return;                           // a newer commit's response already landed
    renderSettings(payload);
    scheduleCensus();
  }

  async function applyPreset(name) {
    const payload = await api("/api/preset", { method: "POST", body: { name } });
    renderPane();                    // a preset moves controls this pane may not be showing
    renderSettings(payload);
    scheduleCensus();
  }

  async function browse(key) {
    const current = app.settings && app.settings.values[key];
    const res = await api("/api/browse", { method: "POST", body: { key, current } });
    if (res && res.path) commit(key, res.path);
  }

  // ---- census ------------------------------------------------------------------------------

  function scheduleCensus() {
    clearTimeout(app.censusTimer);
    app.censusTimer = setTimeout(loadCensus, 250);
  }

  async function loadCensus() {
    $("bundleStatus").textContent = "counting…";
    try {
      renderCensus(await api("/api/census"));
    } catch (e) {
      $("bundleStatus").textContent = "census failed";
    }
  }

  const COUNT_ORDER = ["instances", "meshes", "materials", "textures", "networkSegments", "networkNodes", "laneGeometry",
                       "lanes", "areas", "clipVolumes", "lights", "warnings"];

  function renderCensus(c) {
    app.census = c;
    const box = $("census");
    const derived = $("derived");
    box.innerHTML = "";
    derived.innerHTML = "";
    if (!c || !c.bundle) {
      box.appendChild(el("div", "census-empty muted", (c && c.warnings && c.warnings[0]) || "No bundle loaded."));
      $("bundleStatus").textContent = "not found";
      $("ovExpected").textContent = "–";
      return;
    }
    const b = c.bundle;
    $("bundleStatus").textContent = b.city ? `${b.city}` : "loaded";
    const meta = el("div", "census-meta");
    const add = (k, v) => { meta.appendChild(el("span", "k", k)); meta.appendChild(el("span", "v", v)); };
    add("City", b.city || "–");
    add("Ripped", b.generatedAt ? new Date(b.generatedAt).toLocaleString() : "–");
    add("Schema", `${b.schema ?? "–"}  ·  ${b.generator || ""}`.trim());
    add("Map", b.mapSize ? `${fmtInt(b.mapSize)} m  ·  ${fmtBytes(b.sizeBytes)} on disk` : fmtBytes(b.sizeBytes));
    if (c.region) add("Region", `${c.region.centerX}, ${c.region.centerZ}  r ${c.region.radius} m  (${(c.scale * 100).toFixed(1)} % of the map)`);
    box.appendChild(meta);
    const counts = el("div", "chips");
    const keys = Object.keys(b.counts || {}).sort((x, y) => {
      const ix = COUNT_ORDER.indexOf(x), iy = COUNT_ORDER.indexOf(y);
      return (ix < 0 ? 99 : ix) - (iy < 0 ? 99 : iy) || x.localeCompare(y);
    });
    for (const k of keys) {
      const chip = el("span", "chip");
      chip.appendChild(el("b", "", fmtInt(b.counts[k])));
      chip.appendChild(el("span", "k", k));
      counts.appendChild(chip);
    }
    box.appendChild(counts);
    if (c.warnings && c.warnings.length) {
      const w = el("div", "muted", c.warnings.join(" · "));
      w.style.marginTop = "8px";
      box.appendChild(w);
    }
    // derived cache
    const products = (c.derived && c.derived.products) || {};
    const names = Object.keys(products);
    if (names.length) {
      derived.appendChild(el("h3", "", "Derived cache"));
      const chips = el("div", "chips");
      for (const name of names) {
        const p = products[name];
        const chip = el("span", "chip " + (p.status || "absent"));
        chip.title = p.reason || "";
        chip.appendChild(el("b", "", name));
        chip.appendChild(el("span", "k", p.status));
        chips.appendChild(chip);
      }
      derived.appendChild(chips);
    }
    if (!app.build || !(app.build.stages && app.build.stages.length)) $("ovExpected").textContent = fmtSec(c.totalSeconds);
    renderCost();                       // the meter states the build time; the census supplies it
  }

  // ---- build -------------------------------------------------------------------------------

  function setWorker(text, cls) {
    const w = $("ovWorker");
    w.textContent = text;
    w.className = "v" + (cls ? " " + cls : "");
  }

  function renderBuild(st) {
    app.build = st;
    const running = st.status === "running";
    const pill = $("statusPill");
    pill.dataset.status = st.status;
    pill.textContent = st.status === "idle" ? "Idle" : titleOf(st.status);
    $("startBtn").hidden = running;
    $("cancelBtn").hidden = !running;
    $("cancelBtn").disabled = !!st.cancelRequested;
    $("cancelBtn").textContent = st.cancelRequested ? "Cancelling…" : "Cancel";
    $("startBtn").disabled = !!(app.settings && app.settings.problems && app.settings.problems.length);

    const ov = st.overall || {};
    const fill = $("overallFill");
    const bar = fill.parentElement;
    fill.style.width = `${Math.round((ov.fraction || 0) * 100)}%`;
    bar.className = "bar big " + (st.status === "done" ? "done" : st.status === "failed" ? "failed" : st.status === "cancelled" ? "cancelled" : "");
    $("ovElapsed").textContent = st.startedAt ? fmtSec(ov.elapsed) : "–";
    $("ovEta").textContent = running ? fmtSec(ov.eta) : (st.status === "idle" ? "–" : "0 s");
    if (st.stages && st.stages.length) {
      const expected = st.stages.reduce((a, s) => a + (s.estimateSeconds || 0), 0);
      $("ovExpected").textContent = fmtSec(expected);
    }
    const expected = expectedSeconds();
    if (app.settings && expected !== app.shownExpected) { app.shownExpected = expected; renderCost(); }
    if (running) {
      const age = st.heartbeatAge;
      if (age === null || age === undefined) setWorker("starting…", "");
      else if (age > 20) setWorker("no heartbeat", "dead");
      else if (age > 3) setWorker(`busy ${Math.round(age)} s`, "busy");
      else setWorker(st.workerPid ? `pid ${st.workerPid}` : "alive", "");
    } else {
      setWorker(st.status === "idle" ? "–" : "stopped", "");
    }

    // banner
    const banner = $("banner");
    banner.hidden = true;
    if (st.status === "failed") {
      banner.hidden = false; banner.className = "banner danger";
      banner.textContent = st.error || "The build failed — see the log.";
    } else if (running && st.cancelRequested) {
      banner.hidden = false; banner.className = "banner warn";
      banner.textContent = "Cancelling — waiting for the worker to finish its current chunk.";
    } else if (running && st.heartbeatAge > 3) {
      banner.hidden = false; banner.className = "banner warn";
      banner.textContent = "The worker is inside a long operation and has not reported for a few seconds. This is normal for big numpy passes; it is not frozen unless the heartbeat stays away.";
    } else if (st.status === "done") {
      banner.hidden = false; banner.className = "banner ok";
      banner.textContent = `Level built in ${fmtSec(ov.elapsed)}.`;
    } else if (st.status === "cancelled") {
      banner.hidden = false; banner.className = "banner warn";
      banner.textContent = "Build cancelled.";
    }

    renderStages(st.stages || []);

    // results whenever a finished build is on screen that we have not shown yet - covers the
    // running->finished edge AND a page opened/reloaded after the build already ended
    const finished = st.status === "done" || st.status === "failed" || st.status === "cancelled";
    if (finished && st.finishedAt && app.resultsFor !== st.finishedAt) {
      app.resultsFor = st.finishedAt;
      showResults(st);
      scheduleCensus();                                          // derived/ changed under us
    }
  }

  function renderStages(stages) {
    const list = $("stages");
    for (const s of stages) {
      let li = list.querySelector(`[data-key="${s.key}"]`);
      if (!li) {
        li = el("li", "stage");
        li.dataset.key = s.key;
        li.innerHTML = `<span class="icon"></span>
          <div class="title"><span class="t"></span><span class="phase"></span></div>
          <div class="right"></div>
          <div class="sub"><div class="bar"><div class="fill"></div></div><span class="count"></span></div>
          <div class="summary"></div>`;
        list.appendChild(li);
      }
      li.className = "stage " + s.status;
      li.querySelector(".t").textContent = s.title;
      li.querySelector(".phase").textContent = s.status === "running" && s.phase ? `› ${s.phase}` : (s.status === "skipped" ? "skipped" : s.status === "fresh" ? "unchanged" : "");
      const right = li.querySelector(".right");
      if (s.status === "running") {
        right.innerHTML = `<b>${fmtSec(s.elapsed)}</b> · eta ${fmtSec(s.eta)}`;
      } else if (s.status === "pending") {
        right.textContent = s.estimateSeconds ? `~${fmtSec(s.estimateSeconds)}` : "";
      } else if (s.status === "skipped") {
        right.textContent = "";
      } else {
        right.innerHTML = `<b>${fmtSec(s.elapsed)}</b>`;
      }
      const bar = li.querySelector(".bar");
      const fill = bar.querySelector(".fill");
      const hasTotal = s.status === "running" && s.phaseTotal;
      bar.className = "bar" + (s.status === "running" && !hasTotal && !s.estimateSeconds ? " indeterminate" : "") +
        (s.status === "done" || s.status === "fresh" ? " done" : s.status === "failed" ? " failed" : s.status === "cancelled" ? " cancelled" : "");
      const live = (s.phaseFraction !== null && s.phaseFraction !== undefined) ? s.phaseFraction : (s.fraction || 0);
      const frac = (s.status === "done" || s.status === "fresh") ? 1 : (s.status === "running" ? live : 0);
      fill.style.width = `${Math.round(frac * 100)}%`;
      const count = li.querySelector(".count");
      if (s.status === "running" && s.phaseTotal) {
        count.textContent = `${fmtInt(s.phaseDone)} / ${fmtInt(s.phaseTotal)} ${s.unit || ""}`.trim();
      } else if (s.status === "running" && s.phaseDone) {
        count.textContent = `${fmtInt(s.phaseDone)} ${s.unit || ""}`.trim();
      } else {
        count.textContent = "";
      }
      const summary = li.querySelector(".summary");
      summary.textContent = s.summary || (s.status === "running" ? (s.phaseSummary || "") : "");
      summary.hidden = !summary.textContent;
    }
  }

  async function showResults(st) {
    const card = $("resultsCard");
    const body = $("resultsBody");
    body.innerHTML = "";
    card.hidden = false;
    const head = el("div", "result-head");
    head.appendChild(el("b", "", st.status === "done" ? "Build complete" : st.status === "cancelled" ? "Build cancelled" : "Build failed"));
    head.appendChild(el("span", "muted", `${fmtSec(st.overall && st.overall.elapsed)} total`));
    body.appendChild(head);
    const table = el("table", "results-table");
    table.innerHTML = "<thead><tr><th>Stage</th><th>Status</th><th class='num'>Time</th><th>Summary</th></tr></thead>";
    const tbody = el("tbody");
    for (const s of st.stages || []) {
      const tr = el("tr");
      tr.appendChild(el("td", "", s.title));
      tr.appendChild(el("td", "status-" + s.status, s.status));
      tr.appendChild(el("td", "num", s.status === "skipped" || s.status === "pending" ? "" : fmtSec(s.elapsed)));
      tr.appendChild(el("td", "", s.summary || ""));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    body.appendChild(table);
    if (st.error) body.appendChild(el("div", "banner danger", st.error));
    try {
      const rep = await api("/api/report");
      const problems = rep && rep.report && rep.report.validation && rep.report.validation.problems;
      if (problems && problems.length) {
        body.appendChild(el("div", "muted", `${problems.length} validation problem(s):`));
        const ul = el("ul", "problem-list");
        for (const p of problems.slice(0, 50)) ul.appendChild(el("li", "", p));
        body.appendChild(ul);
      } else if (rep && rep.report && st.status === "done") {
        body.appendChild(el("div", "muted", "Validation: no problems."));
      }
    } catch (_) { /* no report yet */ }
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---- log ---------------------------------------------------------------------------------

  function appendLog(entries) {
    const log = $("log");
    const follow = $("logFollow").checked;
    for (const e of entries) {
      if (e.seq <= app.lastSeq) continue;
      app.lastSeq = e.seq;
      const line = el("span", "l" + (e.stage === "system" ? " system" : "") + (/Traceback|Error/.test(e.text) ? " error" : ""));
      const stamp = new Date((e.at || 0) * 1000).toLocaleTimeString();
      line.appendChild(el("span", "faint", `${stamp} `));
      line.appendChild(el("span", "s", `[${e.stage || "-"}] `));
      line.appendChild(document.createTextNode(e.text));
      log.appendChild(line);
      app.logLines++;
    }
    if (follow) {                                                // never pull lines out from under a reader
      while (app.logLines > 2500) { log.removeChild(log.firstChild); app.logLines--; }
      log.scrollTop = log.scrollHeight;
    }
  }

  function resetLog() {
    $("log").innerHTML = "";
    app.logLines = 0;
  }

  // ---- actions -----------------------------------------------------------------------------

  async function startBuild() {
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

  async function restoreLog() {
    try {
      const st = await api("/api/state");
      resetLog();
      app.lastSeq = 0;
      appendLog(st.log || []);
    } catch (_) { /* nothing to restore */ }
  }

  async function cancelBuild() {
    $("cancelBtn").disabled = true;
    await api("/api/cancel", { method: "POST" });
  }

  async function quit() {
    const running = app.build && app.build.status === "running";
    if (!window.confirm(running ? "A build is running. Cancel it and quit?" : "Quit cs2beamng?")) return;
    try { await api("/api/quit", { method: "POST" }); } catch (_) { /* server gone */ }
    showClosed();
  }

  function showClosed() {
    document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:var(--text-dim);font:15px var(--font)">cs2beamng closed. You can close this tab.</div>';
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
})();
