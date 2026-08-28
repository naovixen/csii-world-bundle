/* The cost meter: what this level will cost to build and what it will cost the card, from
   the estimate the server recomputes on every settings change. */

import { $, app, el, fmtBytes, fmtInt, fmtSec } from './core.js';
import { showSection } from './settings.js';

// ---- the cost meter -------------------------------------------------------------------------

/* "Will this run on my machine?" — on screen the whole time, because it is the consequence of
   every setting rather than of any one of them. Two shapes: a BAR for a figure with a budget
   it can exceed, and a TILE for a figure with none. Every one of them states how it was
   arrived at, in its own words, under the number: an estimate nobody can weigh is an estimate
   nobody can trust. A figure the app cannot yet arrive at says WHAT IT IS WAITING FOR instead
   of showing an empty bar, which would be a promise it cannot keep. */
export const WAITING = {
  "no bundle": "Point the app at your city's export and this fills in.",
  bake: "The first build prepares your city's textures and shapes; the video memory figure needs them.",
  index: "Reading which shapes your city places — about twenty seconds, once per export.",
  reading: "Measuring your city's textures.",
  error: "This could not be worked out for these settings.",
};

export function renderCost() {
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
  if (est && !est.ready) {
    const busy = est.waiting === "index" || est.waiting === "reading";
    host.appendChild(el("div", "cost-waiting" + (busy ? " working" : ""),
      WAITING[est.waiting] || "Video memory cannot be worked out yet."));
  }

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

  costScore(host);
  costApproximations(host, est);
  costLightingWarning(host, budget);
}

/* What the last build actually weighed, beside what this said it would. The estimator's
   own score: without it nobody can tell a meter that is drifting from one that is right. */
function costScore(host) {
  if (app.score) {
    const line = el("div", "cost-score");
    line.appendChild(el("span", "cost-score-k", "Last build"));
    for (const [key, label] of [["total", "in all"], ["mesh", "shapes"], ["texture", "textures"]]) {
      const row = app.score[key];
      if (!row) continue;
      line.appendChild(el("span", "cost-score-v",
        `${label} ${fmtInt(row.estimatedMiB)} estimated, ${fmtInt(row.actualMiB)} MiB actual`));
    }
    host.appendChild(line);
  }
}

/* Every rule the figure cannot see, from the estimate's own declared list. */
function costApproximations(host, est) {
  if (est && (est.approximations || []).length) {
    const note = el("details", "cost-approx");
    note.appendChild(el("summary", "", `What this figure cannot know (${est.approximations.length})`));
    for (const a of est.approximations) {
      const item = el("div", "cost-approx-item");
      item.appendChild(el("span", "cost-approx-what", a.what));
      if (a.why) item.appendChild(el("span", "cost-approx-why", a.why));
      if (a.measured) item.appendChild(el("span", "cost-approx-measured", a.measured));
      note.appendChild(item);
    }
    host.appendChild(note);
  }
}

/* A local light needs the deferred renderer; on Basic Lighting it costs and lights nothing. */
function costLightingWarning(host, budget) {
  if (budget && budget.lighting && /basic/i.test(budget.lighting)) {
    host.appendChild(el("div", "cost-warn",
      `BeamNG is set to ${budget.lighting}, which never shades a point or spot light — street lights would cost and light nothing.`));
  }
}

export function buildCostTile(label, value, basis, band) {
  const tile = el("div", "cost-tile" + (band ? " " + band : ""));
  tile.appendChild(el("span", "cost-tile-k", label));
  tile.appendChild(el("span", "cost-tile-v", value));
  if (basis) tile.appendChild(el("span", "cost-tile-basis", basis));
  return tile;
}

/* The one expected-build-time figure, from wherever it is: the census computes it from the
   unit counts and the measured per-stage rates, and a build that has planned its stages
   carries the same sum. The meter and the Build card must never print two different ones. */
export function expectedSeconds() {
  const counted = app.census && app.census.totalSeconds;
  if (counted !== null && counted !== undefined) return counted;
  const stages = app.build && app.build.stages;
  if (stages && stages.length) return stages.reduce((a, s) => a + (s.estimateSeconds || 0), 0);
  return null;
}

export function buildCostBar(row) {
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
  line.appendChild(el("div", "cost-note", row.basis || ""));
  /* The advice is its own line and not a tail on the basis: one says where the number came
     from and the other says what to do about it, and run together they read as neither. */
  if (row.advice) {
    const advice = el("div", "cost-advice");
    advice.appendChild(document.createTextNode(row.advice + " "));
    if (row.section) {
      const jump = el("button", "linkish", "What should I change?");
      jump.type = "button";
      jump.onclick = () => showSection(row.section);
      advice.appendChild(jump);
    }
    line.appendChild(advice);
  }
  return line;
}

/* Detection was always there — the app finds the bundle, the BeamNG userfolder, the exe and
   the card by itself. It was rendered as a hidden <select>, so nobody saw it. Now it says what
   it found, in words, with one button per thing you might want to do about it. */
