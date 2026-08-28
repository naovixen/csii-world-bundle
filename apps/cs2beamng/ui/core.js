/* The pieces every other module needs: the page's shared state, the one HTTP helper and the
   four formatters.

   Nothing here renders anything and nothing here imports anything, which is what makes it the
   one module every other can depend on. The SSE stream used to live here and could not: it
   calls three renderers, so `core` imported the screen and the screen imported `core`. It is
   in `app.js` now, beside the rest of the wiring. */


export const TOKEN = window.CS2BEAMNG.token;
export const $ = (id) => document.getElementById(id);
export const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};

export const app = {
  settings: null,          // last /api/settings payload
  census: null,
  build: null,             // last state from SSE
  lastSeq: 0,
  resultsFor: null,        // finishedAt of the build whose results are on screen
  score: null,             // the last build's estimated-vs-actual, from its report
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
  estimateTimer: null,     // the poll that waits for a background estimate refresh
  logLines: 0,
};

// ---- transport ---------------------------------------------------------------------------

export async function api(path, opts = {}) {
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


// ---- formatting --------------------------------------------------------------------------

export const fmtInt = (n) => (n === null || n === undefined) ? "–" : Number(n).toLocaleString();
export function fmtSec(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return "?";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}
export function fmtBytes(b) {
  if (!b && b !== 0) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i === 0 ? 0 : (b < 10 ? 2 : 1))} ${units[i]}`;
}
export const titleOf = (key) => key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

