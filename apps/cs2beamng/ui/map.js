/* The map surface: the picture, the georeference that turns a pixel into a place, and making
   a thing on it movable by mouse and by arrow key. What is drawn ON it is `mapcontrols.js`. */

import { api, app, el } from './core.js';

// ---- the map (E3) ---------------------------------------------------------------------------

/* ONE drawing surface, two things on top of it. `region` is three numbers in bundle metres and
   the fast-travel points are a list of more of them, and nobody knows their city's
   coordinates — so both are picked on a picture of the city instead (plan §3.6/§3.7). The
   picture and its georeference come from `/api/map`; everything drawn over it is placed by the
   one conversion below, which is the one the rasters block documents.

   Neither control shipped before the other on purpose: a circle widget now and a point widget
   later would have shared nothing. */

export function mapState() {
  if (!app.map) {
    app.map = { doc: null, waiting: "drawing", surfaces: new Set(), timer: null };
    loadMap();
  }
  return app.map;
}

export async function loadMap() {
  const doc = await api("/api/map");
  const state = app.map || (app.map = { surfaces: new Set() });
  state.doc = doc && doc.ready ? doc : null;
  state.waiting = doc && doc.ready ? null : (doc && doc.waiting) || "drawing";
  clearTimeout(state.timer);
  if (!state.doc) state.timer = setTimeout(loadMap, 2500);
  for (const surface of state.surfaces) surface.refresh();
}

/* A map surface: the picture, an SVG for the region shape, and a layer of pins. `place` puts a
   world point on it and `locate` reads one back, both through the georeference the server
   sent — pixel (0,0) is the NORTH-WEST corner and bundle Z runs north, which is the one sign
   that catches everybody. */
export function buildMapSurface({ onClick, onReady } = {}) {
  const parts = buildMapParts();
  const surface = {
    root: parts.root, svg: parts.svg, pins: parts.pins,
    world: null,
    ...mapGeometry(parts.root, () => surface),
    refresh() { refreshSurface(surface, parts, onReady); },
  };
  if (onClick) {
    parts.root.addEventListener("pointerdown", (event) => {
      if (event.target.closest(".map-pin") || !surface.world) return;
      const at = surface.locate(event);
      if (at) onClick(at, event);
    });
  }
  mapState().surfaces.add(surface);
  // NOT refreshed here. `onReady` has to be a constructor argument, because a map already
  // drawn for this bundle is ready before the caller gets the surface back and a callback
  // attached one line later never fires at all — but calling it from inside the constructor
  // runs the caller's draw code against half its own variables, which is a dead-zone throw
  // that takes the whole control off the screen. So the caller refreshes when it is built.
  return surface;
}

/* The four layers of the map: the picture, the vector overlay, the pins, and the waiting note. */
function buildMapParts() {
  const root = el("div", "mapview");
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  const pins = el("div", "map-pins");
  const note = el("div", "map-note", "");
  root.appendChild(img);
  root.appendChild(svg);
  root.appendChild(pins);
  root.appendChild(note);
  return { root, img, svg, pins, note };
}

/* Bundle metres <-> the box on screen. Everything drawn on the map is in metres, so this is
   the only place either conversion is written. */
function mapGeometry(root, self) {
  return {
    place(x, z) {                                  // bundle metres -> percentages of the box
      const w = self().world;
      if (!w) return { left: 50, top: 50 };
      const span = w.pixels * w.metresPerPixel;
      return { left: ((x - w.originX) / span) * 100, top: ((w.originZ - z) / span) * 100 };
    },
    locate(event) {                                // a pointer event -> bundle metres
      const w = self().world;
      const box = root.getBoundingClientRect();
      if (!w || !box.width) return null;
      const span = w.pixels * w.metresPerPixel;
      const fx = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
      const fy = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
      return [w.originX + fx * span, w.originZ - fy * span];
    },
    metres(pixels) {                               // a length on screen, in bundle metres
      const w = self().world;
      const box = root.getBoundingClientRect();
      if (!w || !box.width) return 0;
      return pixels * (w.pixels * w.metresPerPixel) / box.width;
    },
  };
}

function refreshSurface(surface, parts, onReady) {
  const state = app.map || {};
  surface.world = state.doc ? state.doc.world : null;
  parts.root.classList.toggle("ready", !!state.doc);
  if (state.doc) {
    if (parts.img.getAttribute("src") !== state.doc.image) {
      parts.img.setAttribute("src", state.doc.image);
    }
    parts.note.textContent = "";
  } else {
    parts.note.textContent = state.waiting === "no bundle"
      ? "Point the app at your city's export and the map appears here."
      : "Drawing your city — the ground, the coast and every road. About twenty seconds, once.";
  }
  if (onReady) onReady();
}

/* A pin that can be dragged. `onMove` runs while it moves and `onDrop` once, at the end —
   the snap is a round trip to the server and must not fire sixty times a second. */
export function makeDraggable(node, surface, onMove, onDrop) {
  node.addEventListener("pointerdown", (event) => {
    if (!surface.world) return;
    event.preventDefault();
    node.setPointerCapture(event.pointerId);
    node.classList.add("dragging");
    const move = (e) => { const at = surface.locate(e); if (at) onMove(at); };
    const up = (e) => {
      node.releasePointerCapture(event.pointerId);
      node.classList.remove("dragging");
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", up);
      const at = surface.locate(e);
      if (at && onDrop) onDrop(at);
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", up);
  });
}

/* A marker moves with the arrow keys as well as with a pointer, because a map that can only be
   used with a mouse is a map some people cannot use at all. One press is one PIXEL of the map
   as drawn — so the step means the same thing at any size of window — and Shift is ten of them.
   The move itself is free; `onSettle` fires once the keys stop, which is where the snap and
   the save belong (snapping on every press would fight the person pressing).

   BOTH of these need the node to SURVIVE the gesture. A marker rebuilt on each move loses its
   pointer capture and its keyboard focus mid-gesture, which is exactly why the fast-travel pins
   could not be dragged: they were recreated on every redraw. Markers are made once and moved. */
export const SETTLE_MS = 500;

export function makeArrowMovable(node, surface, get, onMove, onSettle) {
  let settling = null;
  node.tabIndex = 0;
  node.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!delta || !surface.world) return;
    event.preventDefault();
    const step = surface.metres(event.shiftKey ? 10 : 1);
    const at = get();
    onMove([at[0] + delta[0] * step, at[1] - delta[1] * step]);   // screen down is south
    clearTimeout(settling);
    settling = setTimeout(() => onSettle(get()), SETTLE_MS);
  });
  node.addEventListener("blur", () => {
    if (settling) { clearTimeout(settling); settling = null; onSettle(get()); }
  });
}

/* `region` is a string ("X Z R", empty = the whole city) and reads as one. As a control it is
   a choice — all of it, or a patch of it — so it is built as one, and the patch is dragged on
   the map rather than typed. The shape drawn is a SQUARE because a square is what every stage
   that reads a region keeps (`csiibundle.region`). The setting's own name says "radius", which
   reads as a disc; it is a half-width, and the name is what is wrong. */
