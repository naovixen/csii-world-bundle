/* One control per setting, and the rules that grey one out.

   `buildControl` is the only entry; `updateDependencies` decides which controls a value change
   switches off or disables, which is a property of the controls rather than of the cost. */

import { $, app, el } from './core.js';
import { buildPoiControl, buildRegionControl } from './mapcontrols.js';
import { browse, commit, setControl } from './settings.js';

export function parentIsOn(parentKey, values) {
  const off = (app.settings && app.settings.parentOff) || {};
  const value = values[parentKey];
  if (parentKey in off) return value !== off[parentKey];
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return value.length > 0;
  return !!value;
}

export function updateDependencies(values) {
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
export function childrenOff(key, value) {
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

export function buildControl(spec) {
  if (!spec) return el("div", "");
  if (spec.control === "region") return buildRegionControl(spec);
  if (spec.control === "poi") return buildPoiControl(spec);
  if (spec.control === "clock") return buildClockControl(spec);
  if (spec.control === "categories") return buildCategoriesControl(spec);
  const key = spec.key;
  const row = el("div", "setting");
  const ctl = el("div", "ctl");
  const { input, apply } = buildInput(spec, key, row, ctl);
  input.id = "in_" + key;
  if (spec.subsetOf) row.classList.add("subset");        // indented under the thing it belongs to
  row.appendChild(el("div", "name", spec.label));
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

/* The input element itself and how a value is written into it, per control kind. Appends to
   `ctl`, and may mark `row` — a switch and a path box change the row's own layout. */
function buildInput(spec, key, row, ctl) {
  if (spec.control === "switch") return buildSwitch(key, row, ctl);
  if (spec.control === "percent" || spec.control === "distance") return buildSlider(spec, key, ctl);
  if (spec.control === "segmented" || spec.control === "choice") return buildSelect(spec, key, ctl);
  if (spec.control === "number") return buildNumber(spec, key, ctl);
  return buildText(spec, key, row, ctl);
}

function buildSwitch(key, row, ctl) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.addEventListener("change", () => commit(key, input.checked));
  ctl.appendChild(input);
  row.classList.add("switchrow");
  return { input, apply: (v) => { input.checked = !!v; } };
}

function buildSlider(spec, key, ctl) {
  const range = spec.range || { min: 0, max: 100, step: 1, suffix: "" };
  const input = document.createElement("input");
  input.type = "range";
  input.min = range.min; input.max = range.max; input.step = range.step;
  const val = el("span", "val", "");
  const show = (v) => {
    val.textContent = (spec.zeroLabel && Number(v) === 0) ? spec.zeroLabel : v + range.suffix;
    val.classList.toggle("zero", !!spec.zeroLabel && Number(v) === 0);
  };
  input.addEventListener("input", () => show(input.value));
  input.addEventListener("change", () => commit(key, Number(input.value)));
  ctl.appendChild(input);
  ctl.appendChild(val);
  return { input, apply: (v) => { input.value = v; show(input.value); } };
}

function buildSelect(spec, key, ctl) {
  const input = document.createElement("select");
  for (const c of spec.choices) input.appendChild(new Option(choiceLabel(key, c), c));
  input.addEventListener("change", () => commit(key, input.value));
  ctl.appendChild(input);
  return { input, apply: (v) => { input.value = String(v); } };
}

function buildNumber(spec, key, ctl) {
  const input = document.createElement("input");
  input.type = "number";
  if (spec.kind === "int") input.step = 1;
  input.addEventListener("change", () => commit(key, input.value));
  ctl.appendChild(input);
  // "0.8" with nothing after it has not been answered: metres? feet? a fraction of one?
  if (spec.unit) ctl.appendChild(el("span", "unit", spec.unit));
  return { input, apply: (v) => { input.value = (v === null || v === undefined) ? "" : v; } };
}

function buildText(spec, key, row, ctl) {
  const input = document.createElement("input");
  input.type = "text";
  input.spellcheck = false;
  input.addEventListener("change", () => commit(key, input.value));
  ctl.appendChild(input);
  if (spec.control === "path") {
    const b = el("button", "btn small browse", "Browse");
    b.type = "button"; b.dataset.for = key; b.hidden = true;
    b.addEventListener("click", () => browse(key));
    ctl.appendChild(b);
    row.classList.add("wide");
  }
  return { input, apply: (v) => { input.value = (v === null || v === undefined) ? "" : v; } };
}

export function rowShell(spec, extraClass) {
  const row = el("div", "setting wide" + (extraClass ? " " + extraClass : ""));
  row.appendChild(el("div", "name", spec.label));
  return row;
}

/* A comma-separated string whose set of valid answers is fixed and small, as that set of
   checkboxes. `choiceSet` comes from the metadata, so this serves the roadside-furniture
   kinds and the recolouring kinds alike — and both were boxes nothing on screen told you how
   to fill in. Written back as the same string the command line takes. */
export function buildCategoriesControl(spec) {
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
export function buildClockControl(spec) {
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


export function choiceLabel(key, c) {
  if (key.startsWith("texture_max")) return c === "0" ? "full size" : `${c} px`;
  if (key === "terrain_upsample") return c === "1" ? "Standard" : "Fine — sharper underfoot, more memory";
  if (key === "collision_preset") return { exact: "Exact shapes", simplified: "Simple boxes", off: "Nothing solid" }[c] || c;
  if (key === "terrain_holes") return { contain: "Only where the hole is fully covered", majority: "Wherever most of it is", none: "Never" }[c] || c;
  if (key === "ui_browser") return { window: "A window of its own", chrome: "Chrome", edge: "Edge", firefox: "Firefox", default: "Your usual browser" }[c] || c;
  return c;
}
