/* The two things picked on the map: the region square and the fast-travel points.

   Both exist because a free-text box whose valid answer is a PLACE is a design failure, and
   both draw on the one surface for the same reason: a circle widget now and a point widget
   later would have shared nothing. */

import { rowShell } from './controls.js';
import { $, api, app, el } from './core.js';
import { buildMapSurface, makeArrowMovable, makeDraggable } from './map.js';
import { commit, renderSettings, setControl } from './settings.js';

export function buildRegionControl(spec) {
  const row = rowShell(spec, "regionrow");
  const { ctl, mode, readout } = regionHeader(spec);
  row.appendChild(ctl);
  row.appendChild(el("div", "help", spec.blurb));

  let current = [0, 0, 1000];
  let pushing = null;
  const body = el("div", "region-body");
  const surface = buildMapSurface({
    onClick: (at) => { current = [at[0], at[1], current[2]]; push(); },
    onReady: () => draw(),
  });
  const { shape, handle } = regionMarkers(surface);
  body.appendChild(surface.root);
  const { radius, radiusVal } = regionRadius(body);
  row.appendChild(body);

  function draw() {
    drawRegion({ mode, body, row, readout, radius, radiusVal, surface, shape, handle }, current);
  }

  const push = () => {                             // one commit per gesture, not per pixel
    draw();
    clearTimeout(pushing);
    pushing = setTimeout(() => {
      commit("region", `${Math.round(current[0])} ${Math.round(current[1])} ${Math.round(current[2])}`);
    }, 180);
  };

  makeDraggable(handle, surface,
    (at) => { current = [at[0], at[1], current[2]]; draw(); },
    () => push());
  makeArrowMovable(handle, surface,
    () => [current[0], current[1]],
    (at) => { current = [at[0], at[1], current[2]]; draw(); },
    () => push());
  surface.refresh();
  radius.addEventListener("input", () => { current[2] = Number(radius.value); draw(); });
  radius.addEventListener("change", () => { current[2] = Number(radius.value); push(); });
  mode.addEventListener("change", () => {
    if (mode.value === "all") { draw(); commit("region", ""); return; }
    if (!current[2]) current[2] = 1000;
    push();
  });

  const apply = (v) => {
    const text = (v === null || v === undefined) ? "" : String(v).trim();
    const parts = text.replace(/,/g, " ").split(/\s+/).map(Number).filter((n) => !Number.isNaN(n));
    if (parts.length === 3) current = parts;
    mode.value = text ? "part" : "all";
    draw();
  };
  setControl("region", { input: mode, row, apply, detect: null });
  return row;
}

/* Whole city or part of it, and what the part currently is. */
function regionHeader(spec) {
  const ctl = el("div", "ctl region");
  const mode = document.createElement("select");
  mode.appendChild(new Option("The whole city", "all"));
  mode.appendChild(new Option("Part of it", "part"));
  ctl.appendChild(mode);
  const readout = el("span", "region-readout", "");
  ctl.appendChild(readout);
  return { ctl, mode, readout };
}

/* The square on the map and the dot at its middle. */
function regionMarkers(surface) {
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  shape.setAttribute("class", "region-shape");
  surface.svg.appendChild(shape);
  const handle = el("div", "map-pin region-centre");
  handle.title = "Drag, or focus and use the arrow keys, to move the middle of the patch";
  handle.setAttribute("role", "application");
  handle.setAttribute("aria-label", "The middle of the patch to build");
  surface.pins.appendChild(handle);
  return { shape, handle };
}

function regionRadius(body) {
  const radius = document.createElement("input");
  radius.type = "range";
  radius.min = 200; radius.max = 7000; radius.step = 100;
  const radiusRow = el("div", "region-radius");
  radiusRow.appendChild(el("span", "k", "How far across"));
  radiusRow.appendChild(radius);
  const radiusVal = el("span", "val", "");
  radiusRow.appendChild(radiusVal);
  body.appendChild(radiusRow);
  return { radius, radiusVal };
}

/* Put the current (x, z, half-width) on screen: the readout, the slider and the square. */
function drawRegion(parts, current) {
  const { mode, body, row, readout, radius, radiusVal, surface, shape, handle } = parts;
  const part = mode.value === "part";
  body.hidden = !part;
  row.classList.toggle("region-part", part);
  const [x, z, r] = current;
  readout.textContent = part
    ? `${Math.round(x)}, ${Math.round(z)} · ${(2 * r / 1000).toFixed(1)} km across`
    : "";
  radius.value = String(Math.min(7000, Math.max(200, Math.round(r))));
  radiusVal.textContent = `${(2 * r / 1000).toFixed(1)} km`;
  const at = surface.place(x, z);
  handle.style.left = `${at.left}%`;
  handle.style.top = `${at.top}%`;
  const w = surface.world;
  if (w) {
    const span = w.pixels * w.metresPerPixel;
    const side = (2 * r / span) * 100;
    shape.setAttribute("x", String(at.left - side / 2));
    shape.setAttribute("y", String(at.top - side / 2));
    shape.setAttribute("width", String(side));
    shape.setAttribute("height", String(side));
  }
  shape.style.display = part ? "" : "none";
  handle.style.display = part ? "" : "none";
}

/* The fast-travel editor. `poi_file` used to be a path to a file you wrote by hand, in a format
   documented only in its own help text; it is still a path, and the app writes the file (plan
   §3.7), so `--set poi_file=` and a headless build are unchanged.

   CLICK THE MAP TO ADD ONE. That is the gesture, and the button beside the list does the same
   thing in the middle of the map for anybody who would rather not aim. Every point is SNAPPED
   onto the road network by the server — the same network the build snaps against, so what you
   place is what you get — and it arrives named after the street it landed on, which is a better
   start than "Point 3" even when you rename it. Renaming and removing are in the list below,
   where a list belongs; the pin is only where the place is.

   A pin can be moved afterwards by dragging it or, with it focused, by the arrow keys. Both
   need the pin ELEMENT to survive the gesture, so pins are made once and moved rather than
   rebuilt on every change — rebuilding them is what stopped the drag working at all, because
   the node holding the pointer capture was thrown away on the first move event. */
export function buildPoiControl(spec) {
  const row = rowShell(spec, "poirow");
  row.appendChild(el("div", "help", spec.blurb));
  const body = el("div", "poi-body");
  const surface = buildMapSurface({ onReady: () => adopt(), onClick: (at) => addAt(at) });
  body.appendChild(surface.root);
  const list = el("div", "poi-list");
  body.appendChild(list);
  const { actions, add, hint } = poiActions();
  body.appendChild(actions);
  const suggestBox = el("div", "poi-suggest");
  body.appendChild(suggestBox);
  row.appendChild(body);

  /* One editor's state, shared with the four builders below. `points` is replaced wholesale by
     the server's own normalised list, so it lives on this object rather than as a `let` that
     every closure would have to be re-bound to. */
  const poi = {
    points: [],
    pins: [],                                      // one stable node per point, never rebuilt
    rows: [],
    surface, list, suggestBox, hint,
    save, snap, draw,
  };

  /* The server normalises the list (it is the thing that writes the file), so what comes back
     is what is stored and what the build will read. It goes into the cached map document as
     well as into this closure: the pane can be rebuilt from under us — switching sections does
     exactly that — and a fresh editor reads its points from there. */
  async function save() {
    const payload = await api("/api/poi", { method: "POST", body: { points: poi.points } });
    if (payload && payload.points) {
      poi.points = payload.points;
      if ((app.map || {}).doc) app.map.doc.points = poi.points.map((p) => ({ ...p }));
    }
    draw();
    if (payload && payload.values) renderSettings(payload);
  }

  function adopt() {                               // the stored points arrive with the map
    const doc = (app.map || {}).doc;
    if (doc && doc.points && !poi.points.length) poi.points = doc.points.map((p) => ({ ...p }));
    draw();
  }

  const addAt = (at) => addPoint(poi, at);
  function draw() { drawPoints(poi); }

  add.onclick = () => {
    const world = ((app.map || {}).doc || {}).world;
    const span = world ? world.pixels * world.metresPerPixel : 0;
    addAt(world ? [world.originX + span / 2, world.originZ - span / 2] : [0, 0]);
  };

  surface.refresh();
  setControl("poi_file", { input: add, row, apply: () => {}, detect: null });
  return row;
}

async function snap(at) {
  const out = await api("/api/poi/snap", { method: "POST", body: { at } });
  return out && out.at ? out : { at, name: null };
}

/* A new point lands on the nearest road and takes that road's name, then the editor puts the
   cursor in its name box: the one thing a person always wants to do next is call it something. */
async function addPoint(poi, at) {
  const landed = await snap(at);
  poi.points.push({ name: landed.name || `Point ${poi.points.length + 1}`, at: landed.at });
  await poi.save();
  const line = poi.rows[poi.points.length - 1];
  if (line) { line.name.focus(); line.name.select(); }
}

function poiActions() {
  const actions = el("div", "poi-actions");
  const add = el("button", "btn small", "Add a point");
  add.type = "button";
  actions.appendChild(add);
  const hint = el("span", "faint", "");
  actions.appendChild(hint);
  return { actions, add, hint };
}

/* Reconcile rather than rebuild: the nodes that exist keep existing and only their contents
   move. That is what lets a pin hold focus while the arrow keys walk it across the city, and
   what lets a name being typed survive a redraw. */
function drawPoints(poi) {
  const { points, pins, rows, surface } = poi;
  while (pins.length < points.length) pins.push(makePin(poi, pins.length));
  while (pins.length > points.length) pins.pop().node.remove();
  while (rows.length < points.length) rows.push(makeRow(poi, rows.length));
  while (rows.length > points.length) rows.pop().line.remove();
  points.forEach((point, index) => {
    const at = surface.place(point.at[0], point.at[1]);
    const pin = pins[index].node;
    pin.style.left = `${at.left}%`;
    pin.style.top = `${at.top}%`;
    pin.title = `${point.name} — drag it, or use the arrow keys`;
    pin.setAttribute("aria-label", `Fast-travel point ${index + 1}, ${point.name}`);
    const line = rows[index];
    if (document.activeElement !== line.name) line.name.value = point.name || "";
    line.where.textContent = `${Math.round(point.at[0])}, ${Math.round(point.at[1])}`;
  });
  poi.hint.textContent = points.length
    ? "Click the map for another. Arrow keys move a selected pin; it lands on the nearest road."
    : "Click anywhere on the map to put one there.";
  drawSuggestions(poi, (app.map || {}).doc);
}

function makePin(poi, index) {
  // Two elements, not one: the wrapper is what is placed and what takes the gestures, and
  // the teardrop inside it is what is rotated. See `.poi-pin` in map.css — the tip has to
  // land on the coordinate, and a rotation composes with a percentage offset in a way that
  // sends it somewhere else entirely.
  const node = el("div", "map-pin poi-pin");
  const teardrop = el("div", "poi-pin-body");
  teardrop.appendChild(el("span", "", String(index + 1)));
  node.appendChild(teardrop);
  node.setAttribute("role", "application");
  // moving is free; the snap and the save happen once the gesture ends, because snapping
  // after every arrow press would drag the point back out from under the person moving it
  const where = () => (poi.points[index] ? poi.points[index].at : [0, 0]);
  const move = (to) => {
    if (!poi.points[index]) return;
    poi.points[index].at = [Math.round(to[0]), Math.round(to[1])];
    poi.draw();
  };
  const settle = async () => {
    if (!poi.points[index]) return;
    poi.points[index].at = (await poi.snap(poi.points[index].at)).at;
    poi.save();
  };
  makeDraggable(node, poi.surface, move, (to) => { move(to); settle(); });
  makeArrowMovable(node, poi.surface, where, move, settle);
  node.addEventListener("click", () => {                  // the pin is the place, not the name
    const line = poi.rows[index];
    if (line) { line.name.focus(); line.name.select(); }
  });
  poi.surface.pins.appendChild(node);
  return { node };
}

function makeRow(poi, index) {
  const line = el("div", "poi-row");
  const badge = el("span", "poi-n", String(index + 1));
  badge.title = "Show this one on the map";
  badge.onclick = () => { if (poi.pins[index]) poi.pins[index].node.focus(); };
  line.appendChild(badge);
  const name = document.createElement("input");
  name.type = "text";
  name.className = "poi-name";
  name.setAttribute("aria-label", "The name of this fast-travel point");
  name.addEventListener("change", () => {
    if (!poi.points[index]) return;
    poi.points[index].name = name.value.trim();
    poi.save();
  });
  line.appendChild(name);
  const where = el("span", "poi-at", "");
  line.appendChild(where);
  const drop = el("button", "btn small ghost", "Remove");
  drop.type = "button";
  drop.onclick = () => { poi.points.splice(index, 1); poi.save(); };
  line.appendChild(drop);
  poi.list.appendChild(line);
  return { line, name, where };
}

function drawSuggestions(poi, doc) {
  poi.suggestBox.innerHTML = "";
  const taken = new Set(poi.points.map((p) => p.name));
  const offers = ((doc && doc.suggestions) || []).filter((s) => !taken.has(s.name)).slice(0, 8);
  if (!offers.length) return;
  poi.suggestBox.appendChild(el("span", "poi-suggest-k", "Busiest places"));
  for (const offer of offers) {
    const chip = el("button", "chip poi-offer", offer.name);
    chip.type = "button";
    chip.title = `${offer.description} — click to make it one of yours`;
    chip.onclick = () => { poi.points.push({ name: offer.name, at: offer.at.slice() }); poi.save(); };
    poi.suggestBox.appendChild(chip);
  }
}

