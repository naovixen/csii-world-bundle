/* What the build is doing: the census before it starts, the stages while it runs, and the
   log underneath. */

import { $, api, app, el, fmtBytes, fmtInt, fmtSec, titleOf } from './core.js';
import { expectedSeconds, renderCost } from './cost.js';

// ---- census ------------------------------------------------------------------------------

export function scheduleCensus() {
  clearTimeout(app.censusTimer);
  app.censusTimer = setTimeout(loadCensus, 250);
}

export async function loadCensus() {
  $("bundleStatus").textContent = "counting…";
  try {
    renderCensus(await api("/api/census"));
  } catch (e) {
    $("bundleStatus").textContent = "census failed";
  }
}

export const COUNT_ORDER = ["instances", "meshes", "materials", "textures", "networkSegments", "networkNodes", "laneGeometry",
                     "lanes", "areas", "clipVolumes", "lights", "warnings"];

export function renderCensus(c) {
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

export function setWorker(text, cls) {
  const w = $("ovWorker");
  w.textContent = text;
  w.className = "v" + (cls ? " " + cls : "");
}

export function renderBuild(st) {
  app.build = st;
  const running = st.status === "running";
  buildControls(st, running);
  buildOverall(st, running);
  buildWorker(st, running);
  buildBanner(st, running);
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

/* The status pill and the two buttons that swap places while a build runs. */
function buildControls(st, running) {
  const pill = $("statusPill");
  pill.dataset.status = st.status;
  pill.textContent = st.status === "idle" ? "Idle" : titleOf(st.status);
  $("startBtn").hidden = running;
  $("cancelBtn").hidden = !running;
  $("cancelBtn").disabled = !!st.cancelRequested;
  $("cancelBtn").textContent = st.cancelRequested ? "Cancelling…" : "Cancel";
  $("startBtn").disabled = !!(app.settings && app.settings.problems && app.settings.problems.length);
}

/* The overall bar and the three figures beside it. */
function buildOverall(st, running) {
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
}

/* Is the worker alive? The heartbeat age is the only thing that can say so. */
function buildWorker(st, running) {
  if (!running) {
    setWorker(st.status === "idle" ? "–" : "stopped", "");
    return;
  }
  const age = st.heartbeatAge;
  if (age === null || age === undefined) setWorker("starting…", "");
  else if (age > 20) setWorker("no heartbeat", "dead");
  else if (age > 3) setWorker(`busy ${Math.round(age)} s`, "busy");
  else setWorker(st.workerPid ? `pid ${st.workerPid}` : "alive", "");
}

function buildBanner(st, running) {
  const ov = st.overall || {};
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
}

export function renderStages(stages) {
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

export async function showResults(st) {
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
    const score = rep && rep.report && rep.report.estimate && rep.report.estimate.score;
    if (score) { app.score = score; renderCost(); }
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

export function appendLog(entries) {
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

export function resetLog() {
  $("log").innerHTML = "";
  app.logLines = 0;
}

