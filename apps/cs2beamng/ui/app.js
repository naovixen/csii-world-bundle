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
    controls: new Map(),     // key -> {input, apply(value)}
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

  async function loadSettings() {
    renderSettings(await api("/api/settings"));
  }

  function renderSettings(payload) {
    if (!payload || !payload.values) return;
    app.settings = payload;
    if (!app.controls.size) buildSettingsDom(payload);
    applyValues(payload.values);
    renderProblems(payload.problems || [], payload.errors || []);
    renderPresets(payload.values.quality_preset);
    renderDetected(payload.detected, payload.canBrowse);
    updateDependencies(payload.values);
    $("startBtn").disabled = (payload.problems || []).length > 0 || (app.build && app.build.status === "running");
  }

  function renderPresets(active) {
    const box = $("presets");
    box.innerHTML = "";
    for (const name of ["ultra", "high", "medium", "low"]) {
      const b = el("button", name === active ? "active" : "", titleOf(name));
      b.type = "button";
      b.onclick = () => applyPreset(name);
      box.appendChild(b);
    }
    if (active === "custom") {
      const b = el("button", "custom active", "Custom");
      b.type = "button";
      b.disabled = true;
      box.appendChild(b);
    }
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

  function renderDetected(detected, canBrowse) {
    document.querySelectorAll(".browse").forEach((b) => { b.hidden = !canBrowse; });
    const bundleSel = document.querySelector('select.detected[data-for="bundle"]');
    const options = (detected && detected.bundles) || [];
    bundleSel.innerHTML = "";
    bundleSel.hidden = options.length === 0;
    if (options.length) {
      bundleSel.appendChild(new Option("detected…", ""));
      for (const p of options) bundleSel.appendChild(new Option(p, p));
      bundleSel.onchange = () => { if (bundleSel.value) commit("bundle", bundleSel.value); bundleSel.value = ""; };
    }
    const outSel = app.controls.get("output") && app.controls.get("output").detected;
    if (outSel) {
      const dirs = (detected && detected.levelsDirs) || [];
      outSel.innerHTML = "";
      outSel.hidden = dirs.length === 0;
      if (dirs.length) {
        outSel.appendChild(new Option("detected…", ""));
        for (const p of dirs) outSel.appendChild(new Option(p, p));
        outSel.onchange = () => { if (outSel.value) commit("output", outSel.value); outSel.value = ""; };
      }
    }
  }

  const DEPENDS = {
    lights_max: (v) => v.lights,
    lane_furniture_categories: (v) => v.lane_furniture,
    collision_buildings: (v) => v.collision_preset !== "off",
    collision_props: (v) => v.collision_preset !== "off",
    collision_trees: (v) => v.collision_preset !== "off",
    collision_furniture: (v) => v.collision_preset !== "off",
    collision_skirts: (v) => v.collision_preset !== "off",
    collision_paint: (v) => v.collision_preset !== "off",
    beamng_exe: (v) => v.cook,
    foliage_density: (v) => v.trees,
    prop_density: (v) => v.props,
  };

  function updateDependencies(values) {
    for (const [key, test] of Object.entries(DEPENDS)) {
      const ctl = app.controls.get(key);
      if (!ctl) continue;
      const on = !!test(values);
      ctl.row.classList.toggle("disabled", !on);
      ctl.input.disabled = !on;
    }
  }

  const SLIDERS = new Set(["foliage_density", "prop_density"]);
  const HIDDEN = new Set(["quality_preset", "bundle"]);   // shown elsewhere

  function buildSettingsDom(payload) {
    const host = $("settingsGroups");
    host.innerHTML = "";
    const byKey = Object.fromEntries(payload.schema.map((s) => [s.key, s]));
    let first = true;
    for (const [title, keys] of payload.groups) {
      const visible = keys.filter((k) => byKey[k] && !HIDDEN.has(k));
      if (!visible.length) continue;
      const details = el("details", "group");
      details.open = first;
      first = false;
      details.appendChild(el("summary", "", title));
      const body = el("div", "group-body");
      for (const key of visible) body.appendChild(buildControl(byKey[key]));
      details.appendChild(body);
      host.appendChild(details);
    }
    // the bundle path lives in the Bundle card
    const bundleInput = $("in_bundle");
    bundleInput.addEventListener("change", () => commit("bundle", bundleInput.value));
    app.controls.set("bundle", { input: bundleInput, row: bundleInput.parentElement, apply: (v) => { bundleInput.value = v || ""; } });
    document.querySelectorAll(".browse").forEach((b) => {
      b.addEventListener("click", () => browse(b.dataset.for));
    });
  }

  function buildControl(spec) {
    const key = spec.key;
    const row = el("div", "setting");
    const name = el("div", "name", titleOf(key));
    const ctl = el("div", "ctl");
    let input, apply, detected = null;
    const wide = spec.kind === "path" || (spec.kind === "str" && ["region", "level_title", "lane_furniture_categories"].includes(key));
    if (spec.kind === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.addEventListener("change", () => commit(key, input.checked));
      apply = (v) => { input.checked = !!v; };
      ctl.appendChild(input);
    } else if (spec.kind === "int" && SLIDERS.has(key)) {
      input = document.createElement("input");
      input.type = "range"; input.min = 0; input.max = 100; input.step = 1;
      const val = el("span", "val", "");
      input.addEventListener("input", () => { val.textContent = input.value + " %"; });
      input.addEventListener("change", () => commit(key, Number(input.value)));
      apply = (v) => { input.value = v; val.textContent = v + " %"; };
      ctl.appendChild(input); ctl.appendChild(val);
    } else if (spec.kind === "int" || spec.kind === "float") {
      input = document.createElement("input");
      input.type = "number";
      if (spec.kind === "int") input.step = 1;
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = (v === null || v === undefined) ? "" : v; };
      ctl.appendChild(input);
    } else if (spec.kind === "choice") {
      input = document.createElement("select");
      for (const c of spec.choices) input.appendChild(new Option(choiceLabel(key, c), c));
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = String(v); };
      ctl.appendChild(input);
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.spellcheck = false;
      input.addEventListener("change", () => commit(key, input.value));
      apply = (v) => { input.value = (v === null || v === undefined) ? "" : v; };
      ctl.appendChild(input);
      if (spec.kind === "path") {
        const b = el("button", "btn small browse", "Browse");
        b.type = "button"; b.dataset.for = key; b.hidden = true;
        ctl.appendChild(b);
        if (key === "output") {
          detected = document.createElement("select");
          detected.className = "detected"; detected.hidden = true;
          ctl.appendChild(detected);
        }
      }
    }
    if (wide) row.classList.add("wide");
    input.id = "in_" + key;
    row.appendChild(name);
    row.appendChild(ctl);
    row.appendChild(el("div", "help", spec.help));
    app.controls.set(key, { input, row, apply, detected });
    return row;
  }

  function choiceLabel(key, c) {
    if (key === "texture_max") return c === "0" ? "full size" : `${c} px`;
    if (key === "terrain_upsample") return c === "1" ? "×1 (4096, 3.5 m)" : "×2 (8192, 1.75 m — the rip's fine terrain when present; hung BeamNG once)";
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
    const payload = await api("/api/settings", { method: "POST", body: { values: { [key]: value } } });
    if (seq !== app.commitSeq) return;                           // a newer commit's response already landed
    renderSettings(payload);
    scheduleCensus();
  }

  async function applyPreset(name) {
    renderSettings(await api("/api/preset", { method: "POST", body: { name } }));
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
    const exp = el("div", "expected");
    exp.appendChild(el("span", "muted", "Expected build time"));
    exp.appendChild(el("b", "", c.totalSeconds === null || c.totalSeconds === undefined ? "unknown" : fmtSec(c.totalSeconds)));
    derived.appendChild(exp);
    if (!app.build || !(app.build.stages && app.build.stages.length)) $("ovExpected").textContent = fmtSec(c.totalSeconds);
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
