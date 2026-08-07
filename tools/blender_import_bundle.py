#!/usr/bin/env python3
"""Reconstruct a CS2 World Bundle in Blender — the PLAN Phase 3, 4 and 5 acceptance harness.

This is a *test* tool, not a product. The repo's job is to produce a correct bundle; turning
one into a scene is a consumer's job and explicitly out of scope (see CLAUDE.md). This script
exists only to answer the acceptance questions by eye: does `instances.jsonl` + `meshes/`
actually reconstruct the city (Phase 3), do the buildings come out textured (Phase 4), and do
the roads reconstruct with elevated and tunnel runs told apart (Phase 5)? It reads nothing but
the documented contract, which is also what makes it a useful check — if it needs inside
knowledge of the mod, the contract is wrong.

Nothing here wires materials up by hand, and that is the point. Contract §6 has each `.glb`
reference its textures by relative path into `textures/`, so Blender's stock glTF importer
resolves them on its own; if the buildings arrive grey, the bundle is at fault, not the script.
The one exception is per-instance paint (contract §7.2): CS2 authors tintable surfaces —
foliage above all — desaturated, and colours them per object at draw time, so the tint *cannot*
live inside a shared `.glb`. Applying `attrs.colorSet` through the material's `colorMask` is a
documented consumer rule, and the harness implements exactly that rule and nothing more; without
it every tree and bush renders the grey the atlas actually stores.

    blender --python tools/blender_import_bundle.py -- <bundle_dir> [options]

Options (after the `--`):
    --kinds building,tree,prop   which instance kinds to place        (default: building)
                                 `--kinds none` places no instances at all
    --limit N                    stop after N instances               (default: 20000)
    --assets a,b,c               only place instances whose assetId contains one of these
                                 (case-insensitive substring), e.g. --assets pillar
    --everything R               every kind and every network category, within R metres of
                                 downtown — the whole bundle at neighbourhood scale. A base:
                                 any option named alongside it still wins.
    --center X Z --radius R      only place within R metres of a point, bundle-frame metres
    --center auto --radius R     centre on the densest part of the selected kinds instead
    --center downtown --radius R centre on the tallest 250 m cell of buildings, whatever kinds
                                 are being placed — the useful centre for a mixed look
    --center interchange         centre on the tallest skyline that has a highway or elevated
                                 segment in it — tall buildings and motorway junctions together
    --networks                   also extrude networks.jsonl          (Phase 5)
    --net-limit N                stop after N network records — the fast way to look
    --net-categories a,b,c       which network categories to extrude, or `all`
                                 (default: road,highway,train,tram,subway,pathway,taxiway,runway)
    --lanes                      also bend lanemeshes.jsonl — cables, fences, hedges assembled
                                 from laneprefabs.json + lanemeshes/ (contract §5B v2; same
                                 region filter as networks)
    --skirts                     also place each building's <assetId>__base.glb foundation
                                 skirt, as authored (the 1 m band; the terrain drop of its
                                 v=0 ring is the consumer rule in contract §4.1)
    --terrain                    also place the §8 terrain under everything: the road-flattened
                                 heightmap as a ground grid (roadbed faces textured from
                                 surface_index.png and sunk slightly, steep faces textured
                                 rock, faces marked in cutout.png deleted — the terrain the
                                 game stencil-clips over tunnel portals and trenches) and the
                                 water surface where water_level.png rises above the ground
                                 (same region filter as everything else). The ground, not the
                                 junction floors — junction interiors are the arms' own node
                                 meshes (audit §18)
    --areas                      also place the §8b surface areas from areas.jsonl — house
                                 driveways, parking-lot pavement, park paths: the painted
                                 ground of every meshless prefab
    --keep-existing              don't wipe the scene first
    --save out.blend             write a .blend and exit, so the import is paid for once

Examples:
    # the 1:1 building check — start here, it is fast and the most diagnostic
    blender --python tools/blender_import_bundle.py -- "…/spike" --kinds building

    # a dense neighbourhood with its trees and props, wherever the city actually is
    blender --python tools/blender_import_bundle.py -- "…/spike" \\
        --kinds building,tree,prop --center auto --radius 300

    # the Phase 5 check: the road network alone, coloured by form
    blender --python tools/blender_import_bundle.py -- "…/spike" --kinds none --networks

    # everything at once — buildings, trees, props, every network category, all textured —
    # over 500 m of downtown, which is small enough to import repeatedly
    blender --python tools/blender_import_bundle.py -- "…/spike" --everything 500

The map origin is not the city. A save's buildings can sit kilometres off (0,0), so a radius
around a guessed point mostly finds empty ground — which looks exactly like a broken import.
Every run prints the bounds and centroid of the kinds it selected before placing anything, and
`--center auto` picks the densest 250 m cell, so neither has to be guessed at.

Placing all 1.3M instances of a full city is deliberately not the default: Blender handles the
geometry fine (every instance is a collection instance sharing one mesh) but not that many
objects in the depsgraph. Buildings alone are usually only thousands and need no filter at all;
trees and props are the ones that need one.
"""

import json
import math
import os
import sys
import time

import bpy
import numpy
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree


# -- the frame conversion, contract §2 ------------------------------------------------------
#
# The bundle frame is CS2 world space verbatim: left-handed, Y up, X east, Z north (§2.1).
# Mesh payloads are the one thing converted: every .glb has its vertices mirrored on X so that
# it is right-handed and opens correctly on its own (§2.2). The contract gives the formula:
#
#     v_world = T(pos) · R(rot) · S(scale) · diag(-1,1,1) · v_glb
#
# Two further conversions are Blender's, not the bundle's:
#
#   G   Blender's glTF importer rewrites Y-up to Z-up on import: (x,y,z) → (x,-z,y). So the
#       mesh sitting in Blender is already G·v_glb, and undoing that is the price of letting
#       the importer do its job.
#   P   Blender is Z-up: bundle (x,y,z) → Blender (x,z,y). A transposition, determinant -1,
#       which is exactly what turns the bundle's left-handed frame into Blender's right-handed
#       one — no extra mirror needed anywhere.
#
# So the matrix applied to an imported object is  P · T · R · S · diag(-1,1,1) · G⁻¹, and since
# P is linear it can be split into a translation by P·pos and a constant linear tail:
#
#     P · diag(-1,1,1) · G⁻¹  =  diag(-1,-1,1)     — a 180° turn about Z, no reflection.
#
# It being a proper rotation is the sanity check: the mesh was already made right-handed, so
# reconstructing it in a right-handed application must not mirror anything again.

# P — bundle Y-up to Blender Z-up. A row swap, determinant -1, which is precisely what turns
# the bundle's left-handed frame into Blender's right-handed one.
_P = Matrix(((1, 0, 0, 0),
             (0, 0, 1, 0),
             (0, 1, 0, 0),
             (0, 0, 0, 1)))

# diag(-1,1,1) · G⁻¹ — undo the bundle's X mirror, then undo Blender's importer axis swap.
_UNMIRROR_UNIMPORT = Matrix(((-1, 0, 0, 0),
                             (0, 0, 1, 0),
                             (0, -1, 0, 0),
                             (0, 0, 0, 1)))

# How far a road marking is lifted off the carriageway, in metres of Blender world Z. Enough to
# beat depth precision at city scale, small enough that nothing downstream can care: a 1.5 cm
# kerb-height error is already inside the tolerance of the road surface it sits on.
_DECAL_LIFT = 0.015

# Lane decal parts (§5B `decal`) cannot use a fixed lift: the game stores lane curves at the
# base plane of the carriageway, and the rendered surface crowns ABOVE them — measured on a
# full-city rip, the road top near centre lines is a median 7 mm above the lane path and
# beats path+_DECAL_LIFT on 42% of samples, which is why bent markings vanish into the
# asphalt. The game never sees the problem because a decal is a projector volume, painted
# onto whatever surface it meets. The harness equivalent is a drape: sample the road surface
# under the marking and snap onto it (see _decal_rows / Tier 1b-1c near
# _bend_lane_part — the old fixed-step subdivide-every-vertex approach was the single
# biggest driver of a full-city import's memory and ray-cast cost, and is gone). Markings
# ride a hair higher than the ~12%-alpha wear tint so the two never z-fight where a
# crosswalk crosses a drive lane.
_DECAL_DRAPE_MARKING = 0.014
_DECAL_DRAPE_TINT = 0.008

# How far terrain faces marked roadbed (§8 surface id 1) sink below their sampled height —
# the game's own clamp direction: its road job pulls ground under a carriageway to the
# MINIMUM of the road's surface band, below the asphalt top, while a consumer's linearly
# interpolated (and possibly stride-subsampled) grid crests through it.
_ROADBED_SINK = 0.15

# Linear-space gain on the ground and surface-area textures. The theme diffuse is authored
# dark (the ripped grass averages sRGB (66, 72, 53) — a faithful rip, verified) and reads as
# it does in-game only under the game's exposure and sky light, neither of which a bare
# Blender scene has. Presentation only: the ripped pixels are untouched on disk.
_GROUND_GAIN = 2.8

# Ground steeper than this (rise over run) is textured rock instead of grass — the gorge
# embankments. The game classifies grass/dirt/rock per pixel on the GPU by slope, height and
# noise (§8: the bundle deliberately does not reproduce that); a plain slope threshold at
# ~40 degrees is the presentation-side stand-in.
_ROCK_SLOPE = 0.84


class _Log:
    """Styled console narration: phase headers, timings, progress, colours.

    Colours and in-place progress bars only when stdout is a real console and NO_COLOR
    is unset; piped output (Select-String workflows) gets plain, line-oriented text.
    All counts stay in the text — the A/B gate diffs the numbers, not the styling.

    `done()` appends its elapsed time as `in N.N s` rather than `(N.N s)`: the A/B gate
    (tools/ab_compare.py --counts) strips exactly that `in <N> s` suffix before comparing
    the numbers on a line, so a phase-closing summary that also matches one of its count
    markers (`lanes:`, `networks:`, `terrain:`, `instance(s)`, …) still gates cleanly. The
    parenthesised form the gate does not know about would inject a run-to-run-varying
    number into every such line and fail the comparison on jitter alone. Calling `done()`
    with no phase open (self._phase_started is None — the phase already closed itself, or
    was never opened) appends nothing, which is how a final recap line that already spells
    out its own `in N.N s` avoids getting a second, redundant timing suffix.
    """

    def __init__(self):
        self.tty = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if self.tty and os.name == "nt":
            os.system("")  # enable VT processing in classic consoles
        self._phase_started = None
        self._bar_open = False
        self._last_draw = 0.0

    def _c(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.tty else text

    def phase(self, title):
        self.progress_end()
        self._phase_started = time.time()
        print(self._c("1;36", f"==> {title}"))

    def done(self, summary):
        self.progress_end()
        elapsed = f" in {time.time() - self._phase_started:.1f} s" if self._phase_started else ""
        self._phase_started = None
        print(self._c("32", f"    {summary}{elapsed}"))

    def end_phase(self):
        """Force a phase closed without printing anything, in case its own body never did.

        A phase's build_* function can early-return (a missing layer file) without ever
        reaching its own `done()` call, leaving `_phase_started` stale. Left alone, that
        stale timestamp is invisible until some LATER, unrelated `done()` call inherits it
        — printing a second `in N.N s` suffix on a line that may already have its own (the
        final "placed N instance(s) …" recap is exactly such a line). Called once at the
        natural end of each phase's owning block in `main()`, regardless of whether the
        phase's body already closed itself; harmless (a no-op) when it did.
        """
        self.progress_end()
        self._phase_started = None

    def step(self, msg):
        self.progress_end()
        print(f"  {msg}")

    def warn(self, msg):
        self.progress_end()
        print(self._c("31" if "CONTRACT VIOLATION" in msg else "33", f"  {msg}"))

    def progress(self, label, done, total):
        now = time.time()
        if self.tty:
            if now - self._last_draw < 0.1 and done != total:
                return
            self._last_draw = now
            if total:
                width = 28
                fill = int(width * done / max(total, 1))
                bar = "#" * fill + "-" * (width - fill)
                sys.stdout.write(f"\r  [{bar}] {label} {done}/{total}   ")
            else:
                sys.stdout.write(f"\r  … {label} {done}   ")
            sys.stdout.flush()
            self._bar_open = True
        else:
            stride = max(1, (total // 20) if total else 5000)
            if done % stride == 0 or done == total:
                print(f"  … {label} {done}" + (f"/{total}" if total else ""))

    def progress_end(self):
        if self._bar_open:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()
            self._bar_open = False


LOG = _Log()


def _mesh_from_arrays(name, verts, tris, uv_layers=None, material_runs=None, poly_size=3,
                      smooth=True, degenerate_report=None, source_geometry=False):
    """Build a mesh from numpy arrays via foreach_set — the hot-path alternative to
    per-face `from_pydata` / per-loop attribute assignment.

    `verts` is `(N, 3)` float64 already in Blender frame (X east, Y north, Z up). `tris` is
    `(M, poly_size)` int, one row per polygon in loop order. `poly_size` defaults to 3
    (triangles); a caller that already has same-diagonal quads — the terrain grid — passes 4,
    so its faces tessellate exactly as `from_pydata` quads always did instead of picking a
    fresh triangulation diagonal that could flip a slope's silhouette under the pixel A/B
    gate. `uv_layers` is `{layer_name: (M*poly_size, 2)}` in the same loop order.
    `material_runs` is `[(material, poly_count)]` consumed in polygon order; a `None`
    material keeps the default slot 0. `smooth` sets `use_smooth` on every polygon — there is
    no safe default across callers (the game's continuous heightfields want it on; faceted
    geometry such as lane decals must pass `smooth=False` explicitly), so every call site
    states its own choice rather than relying on this default.

    A row in `tris` with a repeated vertex index, or one that is geometrically zero-area
    (distinct indices that still collapse to a line or point — e.g. two part-seam vertices
    authored at the same position), is dropped BEFORE the mesh is built, with a matching
    trim of `material_runs` and `uv_layers` so the survivors keep the material/UV they were
    given. This is deliberately not left to `mesh.validate()`: validate() drops bad polygons
    positionally, and a caller has no way to tell afterward which of ITS polygons a run or a
    UV block was meant for, so re-zipping runs/UVs onto whatever validate() kept would
    silently mis-assign materials and UVs rather than fail loudly. Concatenated per-part
    arrays (lane meshes built from bent segments) are exactly the shape most likely to
    contain one of these at a seam. If `mesh.validate()` still disagrees with the polygon
    count after this pre-clean, that means it rejected something this check does not know
    about — raises rather than silently building a mis-aligned mesh.

    By default every drop is reported as `CONTRACT VIOLATION` — the wording is deliberately
    alarming, because for most callers (terrain, water, lanes) a degenerate face means the
    producer's MATH handed over bad geometry: `verts`/`tris` are numbers this harness just
    computed (a bend, a grid, a drape), so a zero-length edge means that computation broke.
    `source_geometry=True` says the opposite: `verts`/`tris` were read verbatim off a file on
    disk (the direct .glb fast path) — a degenerate triangle there is a faithful rip of
    whatever the game's own mesh already contained, not something this harness introduced,
    and the stock glTF importer builds the identical triangle from the identical file without
    complaint (`mesh.validate()` either drops the same repeated-index polygon or keeps the
    same zero-area one — either way the fast path isn't what put it there). The two ways a
    face can be degenerate are NOT equally diagnostic, and are always reported separately:

    - **Geometrically zero-area with distinct indices** (three different vertices that
      happen to be collinear or coincident) has one well-understood, non-defect source in
      COMPUTED lane geometry: a heavily tiled part's last tile can clip a whole cross-section
      to a single arc position (see `build_lanes`), producing a zero-area strip that is
      neither a bundle defect nor specific to any one instance. Any caller — computed or
      source — that has already established its drops are of that kind can pass
      `degenerate_report(name, zero_area_count, False)` to receive JUST this count instead of
      a per-call `CONTRACT VIOLATION` line, and aggregate/report it its own way; passing
      nothing (the default) keeps the alarming wording for this class too.
    - A **repeated vertex index** within one polygon's row is the classic sign of bad DATA
      (a genuinely zero-length edge). For a COMPUTED caller (`source_geometry=False`, the
      default — terrain, water, lanes) this means the bend/grid/drape math itself broke, so
      it ALWAYS gets the loud `CONTRACT VIOLATION` line, unconditionally — `degenerate_report`
      has no say over this class, by construction, so a future regression in computed geometry
      can never be swallowed by a caller's soft aggregate. Only a `source_geometry=True`
      caller with a `degenerate_report` may downgrade this class too, via
      `degenerate_report(name, repeated_count, True)` — because for that caller the defect (if
      any) is in the ripped file, not in anything this run computed.
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    if verts.size == 0:
        verts = verts.reshape(0, 3)
    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError(f"_mesh_from_arrays({name!r}): verts must be (N, 3), got shape {verts.shape}")

    tris = numpy.asarray(tris, dtype=numpy.int64)
    if tris.size == 0:
        tris = tris.reshape(0, poly_size)
    if tris.ndim != 2 or tris.shape[1] != poly_size:
        raise ValueError(f"_mesh_from_arrays({name!r}): tris must be (M, {poly_size}), "
                         f"got shape {tris.shape}")

    poly_count = len(tris)
    if poly_count:
        # A repeated vertex index within one polygon's row — the classic cause of a face
        # mesh.validate() drops (a zero-length edge, most often at a concatenated part seam).
        repeated = numpy.zeros(poly_count, dtype=bool)
        for i in range(poly_size):
            for j in range(i + 1, poly_size):
                repeated |= tris[:, i] == tris[:, j]
        # Geometrically zero-area: distinct indices that still collapse to a line or point.
        # Fan-triangulated from vertex 0 (v0, v[i], v[i+1]) so it covers any poly_size.
        v0 = verts[tris[:, 0]]
        area2 = numpy.zeros(poly_count)
        for i in range(1, poly_size - 1):
            edge_a = verts[tris[:, i]] - v0
            edge_b = verts[tris[:, i + 1]] - v0
            area2 += numpy.linalg.norm(numpy.cross(edge_a, edge_b), axis=1)
        zero_area = (~repeated) & (area2 < 1e-12)
        degenerate = repeated | zero_area

        if degenerate.any():
            repeated_count = int(repeated.sum())
            zero_area_count = int(zero_area.sum())
            if repeated_count:
                # Downgradable ONLY for a source-geometry caller with its own aggregate
                # report — every computed caller (default) gets the unconditional loud line,
                # by construction: a repeated index in COMPUTED math is historically real bad
                # data, and no caller can opt that class out of the alarm for itself.
                if source_geometry and degenerate_report is not None:
                    degenerate_report(name, repeated_count, True)
                else:
                    LOG.warn(f"CONTRACT VIOLATION: {name} array input had {repeated_count} "
                             "face(s) with a repeated vertex index; dropping before build so "
                             "materials/UVs stay aligned to the surviving polygons")
            if zero_area_count:
                if degenerate_report is not None:
                    degenerate_report(name, zero_area_count, False)
                else:
                    LOG.warn(f"CONTRACT VIOLATION: {name} array input had {zero_area_count} "
                             "geometrically zero-area face(s) (distinct indices collapsed to "
                             "a line or point); dropping before build so materials/UVs stay "
                             "aligned to the surviving polygons")
            keep = ~degenerate
            if material_runs:
                expanded = []
                for material, count in material_runs:
                    expanded.extend([material] * count)
                trimmed = []
                for material, kept in zip(expanded, keep):
                    if not kept:
                        continue
                    if trimmed and trimmed[-1][0] is material:
                        trimmed[-1] = (material, trimmed[-1][1] + 1)
                    else:
                        trimmed.append((material, 1))
                material_runs = trimmed
            if uv_layers:
                loop_keep = numpy.repeat(keep, poly_size)
                uv_layers = {layer_name: numpy.asarray(uvs)[loop_keep]
                             for layer_name, uvs in uv_layers.items()}
            tris = tris[keep]
            poly_count = len(tris)

    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    mesh.loops.add(poly_count * poly_size)
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(tris, dtype=numpy.int32).ravel())
    mesh.polygons.add(poly_count)
    mesh.polygons.foreach_set("loop_start",
                               numpy.arange(0, poly_count * poly_size, poly_size, dtype=numpy.int32))
    mesh.polygons.foreach_set("loop_total", numpy.full(poly_count, poly_size, dtype=numpy.int32))
    if material_runs:
        indices = numpy.empty(poly_count, dtype=numpy.int32)
        slots = {}
        cursor = 0
        for material, count in material_runs:
            if material is not None and material.name not in slots:
                slots[material.name] = len(mesh.materials)
                mesh.materials.append(material)
            indices[cursor:cursor + count] = slots[material.name] if material is not None else 0
            cursor += count
        mesh.polygons.foreach_set("material_index", indices)
    for layer_name, uvs in (uv_layers or {}).items():
        layer = mesh.uv_layers.new(name=layer_name)
        layer.data.foreach_set("uv", numpy.ascontiguousarray(uvs, dtype=numpy.float32).ravel())
    mesh.update()
    mesh.validate()
    if len(mesh.polygons) != poly_count:
        # The pre-clean above is supposed to make this unreachable. Surfacing it as a hard
        # error rather than silently building a mesh whose materials/UVs no longer line up
        # with whatever validate() decided to keep. Remove the mesh datablock before raising —
        # it was never returned to the caller, so nothing else can hold or clean up a
        # reference to it, and leaving it in `bpy.data.meshes` would both leak memory and,
        # for a caller re-trying under this same `name` (the stock-importer fallback after a
        # fast-path failure), steal the name out from under the retry's own datablock,
        # forcing it into a `.001` suffix — object/mesh naming is a frozen bundle contract.
        dropped_extra = poly_count - len(mesh.polygons)
        bpy.data.meshes.remove(mesh)
        raise RuntimeError(
            f"_mesh_from_arrays({name!r}): mesh.validate() dropped "
            f"{dropped_extra} more face(s) after the degenerate-face "
            "pre-clean — it rejected something this check does not detect")
    mesh.polygons.foreach_set("use_smooth", numpy.full(poly_count, smooth, dtype=bool))
    return mesh


def bundle_to_blender_point(pos):
    """Bundle-frame position (X east, Y up, Z north) to Blender (X east, Y north, Z up)."""
    return Vector((pos[0], pos[2], pos[1]))


def instance_matrix(record):
    """The full world matrix for one instance record, ready to assign to matrix_world.

    Composed left to right exactly as contract §2.2 states it, with Blender's two conventions
    bracketing the outside:  P · T · R · S · diag(-1,1,1) · G⁻¹
    """
    pos = record["pos"]
    rot = record.get("rot", [0.0, 0.0, 0.0, 1.0])
    scale = record.get("scale", [1.0, 1.0, 1.0])

    # The bundle stores quaternions [x,y,z,w]; mathutils wants (w,x,y,z). The rotation is
    # applied in bundle coordinates and P converts the result, which is why no term flips sign
    # here — the same quaternion algebra holds in a left-handed basis.
    rotation = Quaternion((rot[3], rot[0], rot[1], rot[2])).to_matrix().to_4x4()
    scaling = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))

    return _P @ Matrix.Translation(Vector(pos)) @ rotation @ scaling @ _UNMIRROR_UNIMPORT


# -- bundle reading -------------------------------------------------------------------------

def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_declared_gaps(bundle_dir):
    """assetIds the producer declared it could not deliver (contract §10)."""
    path = os.path.join(bundle_dir, "warnings.jsonl")
    gaps = {}
    if not os.path.isfile(path):
        return gaps
    for record in read_jsonl(path):
        asset_id = record.get("assetId")
        if isinstance(asset_id, str):
            gaps[asset_id] = record.get("reason", "")
    return gaps


def skyline_weight(record):
    """How much a building counts towards "this cell is downtown".

    The square of its height, so that a cluster of towers outranks a larger number of houses:
    five 60 m towers score 18,000 against a hundred 4 m houses' 1,600, where a plain count puts
    the houses twenty to one. Downtown is what makes a useful test region — it is where the tall
    meshes, the dense props and every road category are — and a plain densest-cell search finds
    suburbia instead, because that is where the object count is.
    """
    height = record.get("attrs", {}).get("height")
    try:
        height = float(height)
    except (TypeError, ValueError):
        height = 0.0
    # A building with no height attribute still counts as one building rather than nothing.
    return max(height, 1.0) ** 2


def survey_interchange(instances_path, networks_path, cell=250.0):
    """The tallest place that also has a motorway junction in it.

    Downtown and the motorway are usually not the same place, and looking at either alone leaves
    half the interesting geometry out of frame: junction flares and elevated forms are on the
    motorway, tall meshes and dense props are downtown. This scores only those cells that hold
    highway or elevated network records, by the building skyline over their 3x3 neighbourhood —
    a junction cell often holds no buildings itself, but the one next to it does.

    Returns (x, z) or None if the bundle has no highway records.
    """
    if not os.path.isfile(networks_path):
        return None

    junction_cells = set()
    for record in read_jsonl(networks_path):
        attrs = record.get("attrs", {})
        if attrs.get("category") != "highway" and attrs.get("form") != "elevated":
            continue
        where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
        junction_cells.add((math.floor(where[0] / cell), math.floor(where[2] / cell)))

    if not junction_cells:
        return None

    skyline = {}
    for record in read_jsonl(instances_path):
        if record.get("kind") != "building":
            continue
        x, _, z = record["pos"]
        key = (math.floor(x / cell), math.floor(z / cell))
        skyline[key] = skyline.get(key, 0.0) + skyline_weight(record)

    def neighbourhood(key):
        cx, cz = key
        return sum(skyline.get((cx + dx, cz + dz), 0.0)
                   for dx in (-1, 0, 1) for dz in (-1, 0, 1))

    best = max(junction_cells, key=neighbourhood)
    if neighbourhood(best) <= 0:
        return None
    return (best[0] * cell + cell / 2, best[1] * cell + cell / 2)


def survey(instances_path, kinds, cell=250.0, weight=None):
    """Where the selected kinds actually are: bounds, centroid, and the densest cell.

    A pre-pass over instances.jsonl, which costs a few seconds on a full city and saves the far
    more expensive mistake of importing a patch of empty ground and concluding the bundle is
    broken. Returns None when the selection is empty.

    `weight` scores each record's contribution to the densest cell; the default counts them.
    """
    count = 0
    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    sum_x = sum_z = 0.0
    grid = {}

    for record in read_jsonl(instances_path):
        if record.get("kind") not in kinds:
            continue
        x, _, z = record["pos"]
        count += 1
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_z, max_z = min(min_z, z), max(max_z, z)
        sum_x += x
        sum_z += z
        cell_key = (math.floor(x / cell), math.floor(z / cell))
        grid[cell_key] = grid.get(cell_key, 0) + (weight(record) if weight else 1)

    if count == 0:
        return None

    densest = max(grid, key=grid.get)
    return {
        "count": count,
        "bounds": (min_x, max_x, min_z, max_z),
        "centroid": (sum_x / count, sum_z / count),
        "densest": (densest[0] * cell + cell / 2, densest[1] * cell + cell / 2),
        "densest_count": grid[densest],
    }


def report_materials(bundle_dir):
    """Say what the material layer offers, so a grey scene has a diagnosis rather than a shrug.

    Reads materials.json only to count and to check that its texture paths resolve. The meshes
    reference textures themselves (contract §6), so nothing here feeds the import.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        LOG.warn("materials: no materials.json — meshes will import untextured")
        return

    with open(path, encoding="utf-8") as handle:
        materials = json.load(handle)

    slots = ("baseColor", "normal", "mask", "emissive", "colorMask")
    referenced, dangling, mapless, masked = set(), [], 0, 0
    for mat_id, material in materials.items():
        paths = [material[s] for s in slots if s in material]
        if not paths:
            mapless += 1
        if "colorMask" in material:
            masked += 1
        for rel in paths:
            referenced.add(rel)
            if not os.path.isfile(os.path.join(bundle_dir, rel)):
                dangling.append(f"{mat_id}.{rel}")

    LOG.step(f"materials: {len(materials)} ({mapless} with no maps), {len(referenced)} distinct textures")

    # colors.json is a palette; colorMask is what says where each channel of it lands. Counting
    # them here is how "did the colour layer actually come out?" gets answered by the harness
    # rather than by opening a texture folder and squinting.
    LOG.step(f"materials: {masked} with a colorMask (the map colors.json needs to be applicable)")
    if dangling:
        # A path in the table with no file behind it is a producer bug: §7 lets a map be absent,
        # but not present-and-broken.
        LOG.warn(f"CONTRACT VIOLATION: {len(dangling)} texture reference(s) do not resolve: {dangling[:5]}")


def show_materials():
    """Put every 3D viewport into material preview, so 'is it textured?' is a question you can
    answer by looking. Solid shading is the default and would hide the entire Phase 4 result."""
    for area in getattr(bpy.context.screen, "areas", []):
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.shading.type = "MATERIAL"


_TINT_PROPS = ("cs2_color0", "cs2_color1", "cs2_color2")
_TINT_LABEL = "cs2_tint"


def set_paint(obj, attrs):
    """Store a record's resolved paint (contract §7.2) as custom properties on its object.

    The three colours land on the instancing empty (or the placed mesh object, for lane
    geometry), where the material's Attribute nodes read them per instance — one shared
    material, thousands of individually-painted copies, exactly the game's own split between
    prefab data and per-object state. White is the identity for the multiply, so objects
    without a colorSet — older bundles predate the field — render the atlas untouched.
    """
    color_set = (attrs or {}).get("colorSet")
    for i, prop in enumerate(_TINT_PROPS):
        channel = color_set[i] if color_set and i < len(color_set) else None
        if channel:
            obj[prop] = [float(channel[0]), float(channel[1]), float(channel[2]),
                         float(channel[3]) if len(channel) > 3 else 1.0]
        else:
            obj[prop] = [1.0, 1.0, 1.0, 1.0]


def _bundle_material_id(name):
    """Blender suffixes `.001` on every duplicate name; the bundle id is what is left."""
    stem, dot, suffix = name.rpartition(".")
    return stem if dot and suffix.isdigit() else name


def _mix_socket(node, name, outputs=False):
    """Pick a Mix node socket by name among the *enabled* ones.

    A ShaderNodeMix carries float, vector and colour variants of `A`/`B`/`Result` under the
    same names, and name lookup returns the first — the float one. Only the sockets of the
    active data_type are enabled, so enabled-ness is the reliable selector.
    """
    pool = node.outputs if outputs else node.inputs
    return next(s for s in pool if s.name == name and s.enabled)


def apply_color_tints(bundle_dir):
    """Wire contract §7.2 into every imported material that carries a colorMask.

    For each such material: base colour becomes
    `lerp(base, base × colorN, colorMask.{R,G,B})`, chained over the three channels — the
    per-texel selection the contract documents, with the per-instance colours read from the
    `cs2_color*` properties `set_paint` stored. `colorMask.A` is snow coverage and a harness
    city has no weather, so it is ignored. Returns how many materials were rigged.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)

    rigged = 0
    for mat in bpy.data.materials:
        entry = table.get(_bundle_material_id(mat.name))
        if not entry or "colorMask" not in entry:
            continue
        tree = getattr(mat, "node_tree", None)
        if tree is None:
            continue
        if any(node.label == _TINT_LABEL for node in tree.nodes):
            continue                                            # already rigged (--keep-existing rerun)
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is None:
            continue
        base_in = principled.inputs.get("Base Color")
        if base_in is None or not base_in.links:
            continue                                            # no base texture to tint

        mask_path = os.path.join(bundle_dir, entry["colorMask"])
        if not os.path.isfile(mask_path):
            continue                                            # report_materials already flagged it
        image = bpy.data.images.load(mask_path, check_existing=True)
        image.colorspace_settings.name = "Non-Color"

        anchor = principled.location
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image, tex.label = image, _TINT_LABEL
        tex.location = (anchor.x - 1000, anchor.y - 600)
        sep = tree.nodes.new("ShaderNodeSeparateColor")
        sep.label = _TINT_LABEL
        sep.location = (anchor.x - 700, anchor.y - 600)
        tree.links.new(tex.outputs["Color"], sep.inputs["Color"])

        upstream = base_in.links[0].from_socket
        for i, (prop, channel) in enumerate(zip(_TINT_PROPS, ("Red", "Green", "Blue"))):
            attr = tree.nodes.new("ShaderNodeAttribute")
            attr.attribute_type = "INSTANCER"                   # instancer empty first, object itself second
            attr.attribute_name, attr.label = prop, _TINT_LABEL
            attr.location = (anchor.x - 700 + i * 60, anchor.y - 900)
            mix = tree.nodes.new("ShaderNodeMix")
            mix.data_type, mix.blend_type, mix.label = "RGBA", "MULTIPLY", _TINT_LABEL
            mix.location = (anchor.x - 450 + i * 150, anchor.y - 500)
            tree.links.new(sep.outputs[channel], _mix_socket(mix, "Factor"))
            tree.links.new(upstream, _mix_socket(mix, "A"))
            tree.links.new(attr.outputs["Color"], _mix_socket(mix, "B"))
            upstream = _mix_socket(mix, "Result", outputs=True)

        tree.links.new(upstream, base_in)                       # replaces the existing link
        rigged += 1

    return rigged


def resolve_layers(bundle_dir):
    """Read layer paths from bundle.json, falling back to the conventional layout.

    A Phase 3 bundle has no manifest yet — that is Phase 7 — so the fallback is what actually
    runs today, and the manifest path is here so this keeps working once it exists.
    """
    manifest_path = os.path.join(bundle_dir, "bundle.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            layers = json.load(handle).get("layers", {})
        meshes = layers.get("meshes", "meshes/")
        instances = layers.get("instances", "instances.jsonl")
        networks = layers.get("networks", "networks.jsonl")
        net_meshes = layers.get("networkMeshes", "networks/")
        lanes = layers.get("laneMeshes", "lanemeshes.jsonl")
        lane_meshes = layers.get("laneMeshDir", "lanemeshes/")
    else:
        meshes, instances = "meshes/", "instances.jsonl"
        networks, net_meshes = "networks.jsonl", "networks/"
        lanes, lane_meshes = "lanemeshes.jsonl", "lanemeshes/"

    return (os.path.join(bundle_dir, meshes),
            os.path.join(bundle_dir, instances),
            os.path.join(bundle_dir, networks),
            os.path.join(bundle_dir, net_meshes),
            os.path.join(bundle_dir, lanes),
            os.path.join(bundle_dir, lane_meshes),
            os.path.join(bundle_dir, "laneprefabs.json"))


# -- networks, contract §5 ------------------------------------------------------------------
#
# A network record carries geometry in exactly one of two forms, and §5 is explicit that a
# consumer must implement both. Only the centerline form is implemented here, because that is
# the only one the Carto ripper emits (§5.1) — a mesh-form record is counted and reported
# rather than silently dropped, so the day a producer starts emitting them this says so.

# Colour by `form`, which is the whole Phase 5 acceptance question: an elevated run and a
# tunnel have to be distinguishable without opening the JSON.
_FORM_COLOURS = {
    "normal": (0.22, 0.22, 0.24, 1.0),
    "elevated": (0.20, 0.45, 0.85, 1.0),
    "tunnel": (0.85, 0.30, 0.20, 1.0),
    "bridge": (0.85, 0.70, 0.20, 1.0),
}

_DRIVABLE = ("road", "highway", "train", "tram", "subway", "pathway", "taxiway", "runway")

# Contract §4: the closed `kind` set. Listed rather than derived so that a v2 kind shows up here
# as a deliberate edit instead of silently joining `--everything`.
_ALL_KINDS = ("building", "prop", "tree", "fence", "decal", "poi")

_UNSET = object()


def _segment_normals(clean):
    """The left-hand horizontal unit normal of each segment of a polyline."""
    normals = []
    for a, b in zip(clean, clean[1:]):
        direction = Vector((b.x - a.x, b.y - a.y, 0.0))
        if direction.length < 1e-6:
            direction = Vector((1.0, 0.0, 0.0))
        direction.normalize()
        normals.append(Vector((-direction.y, direction.x, 0.0)))
    return normals


def ribbon(centerline, width, lift, mitre_limit=4.0):
    """Extrude a centerline into a flat ribbon, in Blender coordinates.

    Contract §5: `width` is the *total* width, extruded symmetrically about the line. The
    offset is taken in the horizontal plane only — a banked road is not a thing CS2 builds, and
    a horizontal offset keeps a steep ramp's surface the right width in plan.

    Corners are properly mitred: the offset at a bend is pushed out by 1/cos(θ/2) so the ribbon
    keeps its full width *across* the turn instead of pinching to width·cos(θ/2). Taking the
    average normal alone is the easy version and is visibly wrong exactly where roads matter —
    at the junction ends the producer's centerlines run into. The limit caps a hairpin, where
    the exact mitre goes to infinity.
    """
    points = [bundle_to_blender_point(p) for p in centerline]

    # Duplicate points would give a zero-length direction and a NaN normal.
    clean = [points[0]]
    for point in points[1:]:
        if (point - clean[-1]).length > 1e-4:
            clean.append(point)
    if len(clean) < 2:
        return None

    half = width / 2.0
    normals = _segment_normals(clean)

    verts, faces = [], []
    for i, point in enumerate(clean):
        before = normals[max(i - 1, 0)]
        after = normals[min(i, len(normals) - 1)]

        bisector = before + after
        if bisector.length < 1e-6:
            # A perfect reversal has no bisector; there is no sane mitre, so butt it.
            offset = after * half
        else:
            bisector.normalize()
            scale = min(1.0 / max(bisector.dot(after), 1e-3), mitre_limit)
            offset = bisector * (half * scale)

        lifted = Vector((point.x, point.y, point.z + lift))
        verts.append(lifted - offset)
        verts.append(lifted + offset)

    for i in range(len(clean) - 1):
        a = 2 * i
        faces.append((a, a + 1, a + 3, a + 2))

    return verts, faces


def place_network_mesh(record, mesh_dir, collection):
    """Import <assetId>.glb and place it by the record's pos/rot, contract §2.2.

    Unlike a prefab mesh, a network mesh is unique to its segment, so it is imported once and
    used once — no collection instancing to set up.
    """
    path = os.path.join(mesh_dir, record["assetId"] + ".glb")
    if not os.path.isfile(path):
        return False

    imported = import_glb(path)
    if not imported:
        return False

    matrix = instance_matrix(record)
    for obj in imported:
        collection.objects.link(obj)
        if obj.parent is None:
            obj.matrix_world = matrix @ obj.matrix_world
        # Lane records carry resolved paint too (contract §5B) — hedges are the visible case.
        set_paint(obj, record.get("attrs"))

    return True


def build_networks(networks_path, categories, collection, net_mesh_dir=None, lift=0.05,
                   center=None, radius=None, limit=None):
    """Extrude every centerline record into one merged mesh per (category, form).

    Merged rather than one object per segment: a city has tens of thousands of edges, and
    Blender's depsgraph is the thing that struggles with object count, not with polygons.
    """
    if not os.path.isfile(networks_path):
        LOG.warn(f"no networks.jsonl at {networks_path} — skipping networks")
        return 0

    _glb_fast_path_counts_reset()
    groups = {}
    counts = {"form": {}, "category": {}}
    mesh_form = 0
    missing_mesh = 0
    skipped_category = 0
    skipped_region = 0
    skipped_limit = 0
    placed_records = 0
    degenerate = 0
    total = 0

    for record in read_jsonl(networks_path):
        total += 1
        # Label deliberately avoids every ab_compare.py count marker ("networks:",
        # "record(s)", …) — a live counter must never accidentally add a gated line the
        # baseline log does not have, whatever stride the helper throttles it to.
        LOG.progress("networks read", total, None)
        attrs = record.get("attrs", {})
        category = attrs.get("category", "other")
        form = attrs.get("form", "normal")

        if categories is not None and category not in categories:
            skipped_category += 1
            continue

        # Importing a network mesh costs a fixed ~100 ms per file whatever its size, so a whole
        # city is a quarter of an hour of overhead. Both filters exist to make looking at one
        # neighbourhood cheap enough to do repeatedly.
        if radius is not None and center is not None:
            where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
            if math.hypot(where[0] - center[0], where[2] - center[1]) > radius:
                skipped_region += 1
                continue

        if limit is not None and placed_records >= limit:
            skipped_limit += 1
            continue

        placed_records += 1

        if "centerline" not in record:
            # Mesh form: the producer assembled real geometry for this segment. It is placed the
            # same way an instance is, and is unique per segment rather than deduped, so there is
            # no point caching the collection — link the imported objects straight in.
            mesh_form += 1
            if net_mesh_dir is not None:
                placed = place_network_mesh(record, net_mesh_dir, collection)
                if placed:
                    counts["form"][form] = counts["form"].get(form, 0) + 1
                    counts["category"][category] = counts["category"].get(category, 0) + 1
                else:
                    missing_mesh += 1
            continue

        built = ribbon(record["centerline"], float(record["width"]), lift)
        if built is None:
            degenerate += 1
            continue

        verts, faces = built
        key = (category, form)
        if key not in groups:
            groups[key] = ([], [])
        all_verts, all_faces = groups[key]
        base = len(all_verts)
        all_verts.extend(verts)
        all_faces.extend(tuple(index + base for index in face) for face in faces)

        counts["form"][form] = counts["form"].get(form, 0) + 1
        counts["category"][category] = counts["category"].get(category, 0) + 1

    # Blender suffixes a duplicate material name on every import, so 7,380 segments sharing
    # ~170 cross-section pieces produce ~91,000 datablocks that are all the same handful of
    # materials. Collapse them back onto the first of each name: same look, a fraction of the
    # memory, and a .blend that opens.
    if net_mesh_dir is not None:
        canonical = {}
        merged = 0
        for mat in list(bpy.data.materials):
            base = mat.name.rsplit(".", 1)[0] if mat.name.rsplit(".", 1)[-1].isdigit() else mat.name
            if base not in canonical:
                canonical[base] = mat
        for obj in collection.objects:
            if obj.type != "MESH":
                continue
            for slot in obj.material_slots:
                if slot.material is None:
                    continue
                name = slot.material.name
                base = name.rsplit(".", 1)[0] if name.rsplit(".", 1)[-1].isdigit() else name
                keep = canonical.get(base)
                if keep is not None and keep is not slot.material:
                    slot.material = keep
                    merged += 1
        for mat in list(bpy.data.materials):
            if mat.users == 0:
                bpy.data.materials.remove(mat)
        if merged:
            LOG.step(f"merged {merged} duplicate material slot(s) onto "
                     f"{len(bpy.data.materials)} material(s)")

    placed = 0
    for (category, form), (verts, faces) in sorted(groups.items()):
        mesh = bpy.data.meshes.new(f"net_{category}_{form}")
        mesh.from_pydata([tuple(v) for v in verts], [], faces)
        mesh.update()

        material = bpy.data.materials.new(f"net_{form}")
        material.diffuse_color = _FORM_COLOURS.get(form, (0.5, 0.5, 0.5, 1.0))
        # New materials already carry a node tree; setting `use_nodes` is deprecated in 5.x.
        tree = getattr(material, "node_tree", None)
        principled = tree.nodes.get("Principled BSDF") if tree is not None else None
        if principled is not None:
            principled.inputs["Base Color"].default_value = material.diffuse_color
            principled.inputs["Roughness"].default_value = 0.9
        mesh.materials.append(material)

        obj = bpy.data.objects.new(f"net_{category}_{form}", mesh)
        collection.objects.link(obj)
        placed += len(faces)

    LOG.done(f"networks:  {total} record(s) read, {sum(counts['category'].values())} extruded "
             f"into {len(groups)} mesh(es), {placed} quad(s)")
    if net_mesh_dir is not None and mesh_form:
        _glb_fast_path_counts_log()
    for form, count in sorted(counts["form"].items()):
        LOG.step(f"form {form}: {count}")
    for category, count in sorted(counts["category"].items()):
        LOG.step(f"category {category}: {count}")
    if skipped_category:
        LOG.step(f"{skipped_category} skipped by --net-categories")
    if skipped_region:
        LOG.step(f"{skipped_region} skipped outside the region")
    if skipped_limit:
        LOG.step(f"{skipped_limit} skipped by --net-limit")
    if degenerate:
        LOG.warn(f"{degenerate} centerline(s) collapsed to a point")
    if mesh_form:
        LOG.step(f"{mesh_form} record(s) use the mesh form")
    if missing_mesh:
        # A referenced mesh that is not on disk is a broken reference, not a style choice.
        LOG.warn(f"CONTRACT VIOLATION: {missing_mesh} mesh-form record(s) reference a "
                 f"missing .glb under {net_mesh_dir}")

    return sum(counts["category"].values())


# -- lane geometry, contract §5B v2 ---------------------------------------------------------
#
# lanemeshes.jsonl records are bend parameters, not baked meshes: the straight part meshes are
# stored once under lanemeshes/, laneprefabs.json says how each lane prefab assembles them, and
# the consumer bends each record's parts along its bézier. The functions below are the
# reference implementation of the contract's bend algorithm, ported from the producer's former
# baking code — which itself reproduced the game's BatchDataSystem.UpdateLaneData /
# BatchInstanceSystem.UpdateLaneInstances. The math is deliberately bpy-free so it can be
# exercised outside Blender.

_LANE_ARC_SAMPLES = 32
_LANE_MAX_TILES = 256

# Tier 1c (2026-08-07 amendment): the fixed _DECAL_DRAPE_STEP = 3.0 m subdivision drove both
# the vertex count and the ray-cast count of the old per-vertex drape (2.24M of each for
# 83,110 markings). Replaced by a sagitta rule: subdivide only as much as the bézier's own
# curvature demands, measured against the arc-table samples the bend already computes (free
# reuse, no extra bézier evaluation). A flat run needs one row (its two end stations); a
# tight curve keeps subdividing until the worst deviation between the true curve and a
# straight sub-chord falls under tolerance.
#
# Two tolerances, because the rows serve two different jobs and only one of them can bury a
# marking (2026-08-08 fix — the single 3D tolerance below shipped for both and put 13.0% of
# the city's decal spans under the asphalt, against 3.6% for the fixed 3 m step it replaced):
#
#  * _DECAL_SHAPE_TOLERANCE governs how faithfully the flat lattice follows the bézier in
#    plan. Cutting a corner by 2 cm is invisible, and it cannot bury anything: the drape puts
#    every vertex on the surface at the vertex's OWN (x, z), wherever the chord placed it.
#  * _DECAL_DIP_FRACTION governs the axis that does bury: between two draped stations the
#    mesh is a straight edge, and a road crest RISES above that edge. The tolerance for that
#    rise cannot be picked independently of the lift — it has to be a fraction of the
#    marking's own _DECAL_DRAPE_* lift, or the rule permits exactly the burial the lift
#    exists to prevent. Half leaves the marking clear by at least its remaining half.
#
# Measured over the full city's 83,110 decal spans (tools/test_decal_rows.py): spans buried
# under their own road 3.6% with the 3 m step, 13.0% with the single 3D tolerance, 2 spans
# (0.002%) now — both pathological vertical curves pinned at the row cap. Vertices
# 2.24M -> 876k (buried) -> 1.14M, so the drape still costs half what the 3 m step did.
_DECAL_SHAPE_TOLERANCE = 0.02  # metres of plan-shape deviation from the true curve
_DECAL_DIP_FRACTION = 0.5      # of the part's own lift, for the vertical rise over a chord
_DECAL_MAX_ROWS = 64           # same ceiling the old fixed-step code used


def _bezier_point_vec(c, t):
    """De Casteljau, vectorised: `c` is the four (x, y, z) control points, `t` an (S,) array
    of parameters; returns (S, 3)."""
    c = numpy.asarray(c, dtype=numpy.float64)
    t = numpy.asarray(t, dtype=numpy.float64)
    u = 1.0 - t
    return ((u ** 3)[:, None] * c[0] + (3 * u * u * t)[:, None] * c[1]
            + (3 * u * t * t)[:, None] * c[2] + (t ** 3)[:, None] * c[3])


def _bezier_tangent_vec(c, t):
    c = numpy.asarray(c, dtype=numpy.float64)
    t = numpy.asarray(t, dtype=numpy.float64)
    u = 1.0 - t
    return ((3 * u * u)[:, None] * (c[1] - c[0]) + (6 * u * t)[:, None] * (c[2] - c[1])
            + (3 * t * t)[:, None] * (c[3] - c[2]))


def _cut_bezier(c, t0, t1):
    """The sub-segment of a cubic bézier over [t0, t1], by two De Casteljau splits."""
    def split_after(points, t):
        p01 = tuple((1 - t) * points[0][i] + t * points[1][i] for i in range(3))
        p12 = tuple((1 - t) * points[1][i] + t * points[2][i] for i in range(3))
        p23 = tuple((1 - t) * points[2][i] + t * points[3][i] for i in range(3))
        p012 = tuple((1 - t) * p01[i] + t * p12[i] for i in range(3))
        p123 = tuple((1 - t) * p12[i] + t * p23[i] for i in range(3))
        p = tuple((1 - t) * p012[i] + t * p123[i] for i in range(3))
        return (p, p123, p23, points[3]), (points[0], p01, p012, p)

    tail, _ = split_after(c, t0)
    if t1 >= 1.0 - 1e-9:
        return tail
    local = (t1 - t0) / max(1.0 - t0, 1e-9)
    _, head = split_after(tail, local)
    return head


def _arc_table_vec(c):
    """Cumulative arc length at _LANE_ARC_SAMPLES+1 evenly spaced parameters, vectorised.
    Returns `(t_table, arc_table)`, both `(_LANE_ARC_SAMPLES+1,)` — `t_table` the parameter
    at each sample, `arc_table` its cumulative arc length, so the pair is directly usable as
    `numpy.interp`'s `(xp, fp)` to invert arc position back to a curve parameter."""
    t_table = numpy.linspace(0.0, 1.0, _LANE_ARC_SAMPLES + 1)
    points = _bezier_point_vec(c, t_table)
    segment_lengths = numpy.linalg.norm(numpy.diff(points, axis=0), axis=1)
    arc_table = numpy.concatenate(([0.0], numpy.cumsum(segment_lengths)))
    return t_table, arc_table


def _curve_length(curve):
    return float(_arc_table_vec(curve)[1][-1])


def _shape_rows(points):
    """Fewest lattice rows whose chords stay within `_DECAL_SHAPE_TOLERANCE` of the curve.

    `points` are the `_LANE_ARC_SAMPLES+1` bézier samples the arc table already computed for
    this span — reused here so curvature costs nothing extra. A flat run (the curve's own
    worst deviation from its straight end-to-end chord already under tolerance) gets 1 row
    = 2 stations; a curved one keeps subdividing, using that a smooth arc's sagitta falls off
    roughly as the square of the subdivision count. Plan fidelity only — burial is
    `_decal_rows`'s job, on a different axis and a different tolerance.
    """
    chord = points[-1] - points[0]
    chord_len = float(numpy.linalg.norm(chord))
    if chord_len < 1e-6:
        return 1
    direction = chord / chord_len
    rel = points - points[0]
    perp = rel - numpy.outer(rel @ direction, direction)
    max_sagitta = float(numpy.linalg.norm(perp, axis=1).max())
    if max_sagitta <= _DECAL_SHAPE_TOLERANCE:
        return 1
    return max(1, min(_DECAL_MAX_ROWS,
                      int(math.ceil(math.sqrt(max_sagitta / _DECAL_SHAPE_TOLERANCE)))))


def _chord_dip(arc_table, height, rows):
    """How far the span's vertical profile rises above the chord polyline of `rows` rows.

    This is the quantity that buries a marking: the drape pins each station onto the road
    surface, the mesh between two stations is a straight edge, and anywhere the surface
    crests above that edge by more than the lift, the asphalt wins the depth test. Measured
    on the arc table's own samples, arc-parameterised exactly as `_bend_lane_part` places
    stations (`arc_pos = stations * total_arc`), so it costs two interpolations over
    `_LANE_ARC_SAMPLES+1` values and no bézier evaluation at all.

    `height` is the bundle-frame up axis of those samples. The lane curve is the proxy for
    the surface profile here — the carriageway is built along the same curve — so this sees
    grade changes, not the crown, which the per-vertex drape handles on its own.
    """
    total = float(arc_table[-1])
    station_arc = numpy.linspace(0.0, total, rows + 1)
    station_height = numpy.interp(station_arc, arc_table, height)
    return float((height - numpy.interp(arc_table, station_arc, station_height)).max())


def _decal_rows(points, arc_table, drape_eps):
    """Lattice rows for one decal span: plan fidelity, then refined until it cannot bury.

    The shape rule alone measures deviation from the 3D chord, which on a road is dominated
    by the horizontal bend — the axis that cannot bury a marking. So it both over-subdivides
    flat bends and, worse, leaves a long straight run over a crest at a single row: a 19 mm
    crest is inside a 20 mm tolerance and outside a 14 mm lift. The refinement below doubles
    the rows until the measured rise over a chord is at most `_DECAL_DIP_FRACTION` of the
    part's own lift — a measured bound, not the `1/rows²` extrapolation the shape rule
    assumes, which a bézier with concentrated curvature does not obey.

    `drape_eps` is None for a part that will not be draped, which needs plan fidelity only.
    """
    rows = _shape_rows(points)
    if drape_eps is None or float(arc_table[-1]) < 1e-6:
        return rows
    target = drape_eps * _DECAL_DIP_FRACTION
    height = points[:, 1]
    while rows < _DECAL_MAX_ROWS and _chord_dip(arc_table, height, rows) > target:
        rows = min(_DECAL_MAX_ROWS, rows * 2)
    return rows


def _lane_tile_count(span_length, mesh_length, tiling_count, geometry_tiling):
    """BatchDataHelpers.GetTileCount with the span's length in place of the whole curve's."""
    if tiling_count:
        clip = round(span_length / max(1.0, mesh_length) * tiling_count)
        tiles = (clip + tiling_count - 1) // tiling_count
        return (min(_LANE_MAX_TILES, tiles) if geometry_tiling else min(1, tiles)), clip
    if geometry_tiling:
        return max(1, min(_LANE_MAX_TILES,
                          math.ceil(span_length / max(1.0, mesh_length) - 0.0001))), 0
    return 1, 0


def _hang_drop_vec(control, t):
    control = numpy.asarray(control, dtype=numpy.float64)
    t = numpy.asarray(t, dtype=numpy.float64)
    u = 1.0 - t
    return ((u ** 3) * control[0] + (3 * u * u * t) * control[1]
            + (3 * u * t * t) * control[2] + (t ** 3) * control[3])


def _quat_apply_vec(q, v):
    """Rotate an (N, 3) array of bundle-frame vectors by bundle quaternion [x, y, z, w]."""
    x, y, z, w = q
    v = numpy.asarray(v, dtype=numpy.float64)
    tx = 2 * (y * v[:, 2] - z * v[:, 1])
    ty = 2 * (z * v[:, 0] - x * v[:, 2])
    tz = 2 * (x * v[:, 1] - y * v[:, 0])
    return numpy.stack((v[:, 0] + w * tx + y * tz - z * ty,
                        v[:, 1] + w * ty + z * tx - x * tz,
                        v[:, 2] + w * tz + x * ty - y * tx), axis=1)


class _RoadSurface:
    """Ray-cast access to the imported road surfaces, for draping lane decals.

    One BVH over every network mesh would hold ~10M triangles for a full city, most of it
    never queried. Instead the network objects are binned into a 128 m grid by bounding box
    and each object's BVH (world space, Blender frame) is built lazily on first query —
    a neighbourhood import builds only the handful it touches.
    """

    CELL = 128.0
    _DOWN = Vector((0.0, 0.0, -1.0))

    def __init__(self, collection):
        self._objects = []
        self._grid = {}
        self._trees = {}
        self.kerb_rejections = 0
        for obj in collection.objects:
            if obj.type != "MESH":
                continue
            index = len(self._objects)
            self._objects.append(obj)
            matrix = obj.matrix_world
            corners = [matrix @ Vector(corner) for corner in obj.bound_box]
            xs = [c.x for c in corners]
            ys = [c.y for c in corners]
            for cx in range(int(min(xs) // self.CELL), int(max(xs) // self.CELL) + 1):
                for cy in range(int(min(ys) // self.CELL), int(max(ys) // self.CELL) + 1):
                    self._grid.setdefault((cx, cy), []).append(index)

    def _tree(self, index):
        if index not in self._trees:
            obj = self._objects[index]
            mesh = obj.data
            if len(mesh.polygons) == 0:
                self._trees[index] = None
                return None
            # foreach_get + one matmul instead of a Python matrix apply per vertex —
            # a full city holds ~19M network vertices and this path builds most of them.
            count = len(mesh.vertices)
            co = numpy.empty(count * 3, dtype=numpy.float64)
            mesh.vertices.foreach_get("co", co)
            matrix = numpy.array(obj.matrix_world, dtype=numpy.float64)
            world = co.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
            mesh.calc_loop_triangles()
            tris = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
            mesh.loop_triangles.foreach_get("vertices", tris)
            self._trees[index] = BVHTree.FromPolygons(
                world.tolist(), tris.reshape(-1, 3).tolist())
        return self._trees[index]

    def drop(self, x, y, z, half_up, half_down):
        """Road surface hit nearest the expected height z under (x, y), Blender frame, or
        None if nothing falls inside `[z - half_down, z + half_up]`.

        Tier 0 (2026-08-07 amendment): `half_up`/`half_down` come from the marking's OWN
        projector half-height (laneprefabs.json `boundsMin`/`boundsMax` y-extent, median
        1.00 m -> ±0.5 m) instead of the old fixed `probe=3.0, depth=8.0` window, and the
        pick is the hit NEAREST z rather than the HIGHEST hit in range. Highest-wins snapped
        ~2.5% of drape samples onto kerb tops, median walls and bridge railings even inside a
        tightened window: a kerb 0.4 m above the road is still inside a ±0.5 m reach and
        would still win on height alone. Nearest-to-expected cannot make that mistake — the
        road surface is always the candidate closest to the un-draped height the lane curve
        itself encodes, and `kerb_rejections` counts how often the two picks would disagree.
        """
        origin = Vector((x, y, z + half_up))
        candidates = []
        for index in self._grid.get((int(x // self.CELL), int(y // self.CELL)), ()):
            tree = self._tree(index)
            if tree is None:
                continue
            location = tree.ray_cast(origin, self._DOWN, half_up + half_down)[0]
            if location is not None:
                candidates.append(location.z)
        if not candidates:
            return None
        nearest = min(candidates, key=lambda c: abs(c - z))
        if len(candidates) > 1 and nearest != max(candidates):
            self.kerb_rejections += 1
        return nearest


def _decal_quad_info(verts, tris, uvs):
    """Reduce a simple two-triangle decal rectangle to its four corners, for direct lattice
    generation (`_decal_lattice`) instead of subdividing a fixed mesh. Returns None for
    anything else — a non-rectangle decal source (rare) takes the general, unsubdivided
    path, exactly the old `_subdivide_decal_quad`'s `return source` guard.

    `verts` (4, 3), `tris` (2, 3) int, `uvs` (2, 3, 2) — one source asset, bundle frame /
    part-local, called once per unique asset id (there are dozens, not thousands): this
    stays plain Python, no part of it runs per marking.
    """
    if len(verts) != 4 or len(tris) != 2:
        return None
    corner_uv = {}
    for tri, tri_uv in zip(tris, uvs):
        for vertex, uv in zip(tri, tri_uv):
            corner_uv.setdefault(int(vertex), (float(uv[0]), float(uv[1])))
    z_sorted = sorted(float(v[2]) for v in verts)
    if abs(z_sorted[0] - z_sorted[1]) > 1e-4 or abs(z_sorted[2] - z_sorted[3]) > 1e-4:
        return None
    near = sorted((i for i in range(4) if abs(float(verts[i][2]) - z_sorted[0]) <= 1e-4),
                  key=lambda i: verts[i][0])
    far = sorted((i for i in range(4) if abs(float(verts[i][2]) - z_sorted[2]) <= 1e-4),
                 key=lambda i: verts[i][0])
    if len(near) != 2 or len(far) != 2:
        return None

    def tri_up(a, b, c):
        # y-component of the cross product: the winding as seen from above (part-local space)
        return (b[0] - a[0]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[0] - a[0])

    v0, v1, v2 = (verts[i] for i in tris[0])
    lattice_v0, lattice_v1, lattice_v3 = verts[near[0]], verts[near[1]], verts[far[0]]
    flip = tri_up(v0, v1, v2) * tri_up(lattice_v0, lattice_v1, lattice_v3) < 0
    return {
        "near_left": tuple(float(c) for c in verts[near[0]]),
        "near_right": tuple(float(c) for c in verts[near[1]]),
        "far_left": tuple(float(c) for c in verts[far[0]]),
        "far_right": tuple(float(c) for c in verts[far[1]]),
        "uv_near_left": corner_uv[int(near[0])], "uv_near_right": corner_uv[int(near[1])],
        "uv_far_left": corner_uv[int(far[0])], "uv_far_right": corner_uv[int(far[1])],
        "flip": flip,
    }


def _decal_lattice(quad, rows):
    """`(rows+1, 2)`-station lattice for one decal quad, part-local — verts
    `(2*(rows+1), 3)` interleaved `[left0, right0, left1, right1, ...]`, tris/uvs to match.

    This is the per-marking hot path (called once per bent decal instance — tens of
    thousands of times a city), so it is pure numpy with no Python loop over rows and no
    per-vertex list, unlike the mesh it replaces (`_subdivide_decal_quad`, which built a
    Python list of (material, indices, uvs) tuples per row).
    """
    t = numpy.linspace(0.0, 1.0, rows + 1)[:, None]  # (R+1, 1)
    nl, nr = numpy.array(quad["near_left"]), numpy.array(quad["near_right"])
    fl, fr = numpy.array(quad["far_left"]), numpy.array(quad["far_right"])
    left = nl + (fl - nl) * t
    right = nr + (fr - nr) * t
    verts = numpy.empty((2 * (rows + 1), 3), dtype=numpy.float64)
    verts[0::2] = left
    verts[1::2] = right

    unl, unr = numpy.array(quad["uv_near_left"]), numpy.array(quad["uv_near_right"])
    ufl, ufr = numpy.array(quad["uv_far_left"]), numpy.array(quad["uv_far_right"])
    uv_left = unl + (ufl - unl) * t
    uv_right = unr + (ufr - unr) * t
    uv = numpy.empty((2 * (rows + 1), 2), dtype=numpy.float64)
    uv[0::2] = uv_left
    uv[1::2] = uv_right

    row = numpy.arange(rows)
    l0, r0, l1, r1 = row * 2, row * 2 + 1, row * 2 + 2, row * 2 + 3
    tris = numpy.stack((numpy.stack((l0, r0, r1), axis=1),
                        numpy.stack((l0, r1, l1), axis=1)), axis=1).reshape(-1, 3)
    if quad["flip"]:
        tris = tris[:, ::-1]
    tri_uvs = uv[tris]
    return verts, tris, tri_uvs


def _bend_lane_part(part, source, curve, spans, curve_length, width_scale, hang, sink):
    """Bend one catalogue part along one lane record's curve, appending blocks to `sink`.

    `sink["blocks"]` collects one dict per (span, tile, material): `verts` (N, 3) bundle-
    frame world positions, `tris` (M, 3) local indices, `uvs` (M, 3, 2)`, `material`, and
    `drape_eps` (the lift epsilon, or None for a part that never drapes — the road-surface
    sampling itself happens later in `build_lanes`, once every block exists). Everything
    here mirrors the producer's former baking loop, which reproduced what the game's vertex
    shader does — vectorised across a part's stations instead of looping per vertex.

    A `decal` part is wear tint and road markings — crosswalks, centre lines, the drive
    lanes' ~12%-alpha darkening — projected over the junction surface the arms' node meshes
    pave (contract §5B, audit §18). Bent here like any part, it is lifted a little above the
    surface so it wins the depth test the projection wins in-game.
    """
    lift = _DECAL_LIFT if part.get("decal") else 0.0
    pos = numpy.asarray(part["pos"], dtype=numpy.float64)
    rot = part["rot"]
    bmin, bmax = part["boundsMin"], part["boundsMax"]
    mesh_length = max(bmax[2] - bmin[2], 0.001)
    tiling_count = part.get("tilingCount", 0)
    geometry_tiling = part.get("geometryTiling", False)
    inverted = part.get("inverted", False)
    is_decal = bool(part.get("decal"))
    # Tier 0: the marking's OWN projector reach, not a tuned probe/depth constant.
    half_height = (bmax[1] - bmin[1]) * 0.5

    quad = source.get("quad") if (is_decal and source is not None) else None
    asset_id = part.get("assetId", "") if is_decal else None

    for span in spans:
        span_length = curve_length * (span[1] - span[0])
        if span_length <= 0.1:
            continue

        bezier = _cut_bezier(curve, span[0], span[1])
        if inverted:
            bezier = (bezier[3], bezier[2], bezier[1], bezier[0])

        t_table, arc_table = _arc_table_vec(bezier)
        total_arc = max(float(arc_table[-1]), 0.001)

        # A decal part is a projector box the game stretches chord-to-chord along the lane
        # (BuildTransformMatrix's isDecal path) — it never tiles. Mapped by its authored
        # length, a 64 m box left the tail of longer connector lanes unpainted.
        if is_decal:
            tiles, clip, tiling_count = 1, 0, 0
        else:
            tiles, clip = _lane_tile_count(span_length, mesh_length, tiling_count, geometry_tiling)
        if tiles < 1 or (tiling_count > 0 and clip < 1):
            continue

        drape_eps = None
        if is_decal:
            drape_eps = (_DECAL_DRAPE_MARKING if ("Line" in asset_id or "Crosswalk" in asset_id)
                         else _DECAL_DRAPE_TINT)
        if is_decal and quad is not None:
            # Tier 1c: rows from THIS span's own curvature (reusing its arc-table samples),
            # not a fixed 3 m step. The lattice is generated directly — no static mesh to
            # subdivide, and no memo needed since this is already O(rows), not O(mesh size).
            # The row count needs the part's lift, so `drape_eps` is settled above it.
            curve_points_full = _bezier_point_vec(bezier, t_table)
            rows = _decal_rows(curve_points_full, arc_table, drape_eps)
            raw_verts, tris_local, uv_local = _decal_lattice(quad, rows)
            material_groups = [(quad["material"], numpy.arange(len(tris_local)))]
        elif is_decal:
            # Not a simple rectangle (rare) — general path, unsubdivided, same guard the old
            # _subdivide_decal_quad's `return source` gave it.
            raw_verts, tris_local, uv_local = source["verts"], source["tris"], source["uvs"]
            tri_mat = source["tri_material"]
            material_groups = [(source["materials"][m], numpy.nonzero(tri_mat == m)[0])
                               for m in numpy.unique(tri_mat)]
        else:
            raw_verts, tris_local, uv_local = source["verts"], source["tris"], source["uvs"]
            tri_mat = source["tri_material"]
            material_groups = [(source["materials"][m], numpy.nonzero(tri_mat == m)[0])
                               for m in numpy.unique(tri_mat)]

        local = _quat_apply_vec(rot, raw_verts) + pos
        z_fraction = (local[:, 2] - bmin[2]) / mesh_length
        stations, station_of_vert = numpy.unique(z_fraction, return_inverse=True)
        station_of_vert = station_of_vert.reshape(-1)  # numpy>=2.0 may keep input's ndim
        local_x = local[:, 0]
        local_y = local[:, 1]
        # A part with >1 material (rare — every decal source is one material; only a general
        # fence/cable mesh can mix them) gets a full `world.copy()` per extra material group
        # below, i.e. (#materials - 1) x #verts wasted rather than indexing just the subset
        # each group's tris reference. Deliberately not fixed: it would need renumbering each
        # group's tri indices into its own compact range, real code for a case this bundle's
        # lane sources essentially never hit (every multi-material part measured is a small,
        # non-tiled mesh — a handful of verts, not the million-vertex tiled parts Tier 1a/1c
        # target), so the complexity isn't worth it unless a future catalogue entry makes
        # multi-material tiled parts common.
        share_verts = len(material_groups) == 1

        for tile in range(tiles):
            if tiling_count > 0:
                clip0 = tile * clip // tiles
                clip1 = (tile + 1) * clip // tiles
                if clip1 <= clip0:
                    continue
                arc_start = total_arc * clip0 / clip
                arc_per_z = total_arc * tiling_count / clip
                arc_limit = total_arc * clip1 / clip
            elif is_decal:
                arc_start, arc_per_z, arc_limit = 0.0, total_arc, total_arc
            else:
                arc_start = tile * mesh_length
                arc_per_z = mesh_length
                arc_limit = total_arc

            arc_pos = numpy.clip(arc_start + stations * arc_per_z, 0.0, arc_limit)
            t = numpy.interp(arc_pos, arc_table, t_table)
            points = _bezier_point_vec(bezier, t)
            forward = _bezier_tangent_vec(bezier, t)
            flen = numpy.linalg.norm(forward, axis=1, keepdims=True)
            forward = forward / numpy.where(flen == 0, 1.0, flen)
            # right = normalize(cross(up, forward)) with up = +y (bundle frame)
            right = numpy.stack((forward[:, 2], numpy.zeros(len(t)), -forward[:, 0]), axis=1)
            rlen = numpy.linalg.norm(right[:, (0, 2)], axis=1, keepdims=True)
            right = right / numpy.clip(rlen, 1e-9, None)

            t_global = numpy.where(inverted, span[1] - (span[1] - span[0]) * t,
                                   span[0] + (span[1] - span[0]) * t)
            width = width_scale[0] + (width_scale[1] - width_scale[0]) * t_global
            drop = _hang_drop_vec(hang, t_global) if hang is not None else numpy.zeros_like(t_global)

            world = (points[station_of_vert]
                    + right[station_of_vert] * (local_x * width[station_of_vert])[:, None])
            world[:, 1] += local_y + lift - drop[station_of_vert]

            for material, tri_rows in material_groups:
                if len(tri_rows) == 0:
                    continue
                block = {
                    "verts": world if share_verts else world.copy(),
                    "tris": tris_local[tri_rows],
                    "uvs": uv_local[tri_rows],
                    "material": material,
                    "drape_eps": drape_eps,
                }
                if drape_eps is not None:
                    # Drape samples the vertex's OWN (x, z) — including its lateral offset —
                    # not the bézier centerline. `_drape_block` reads straight off
                    # `block["verts"]`, so no extra per-station bookkeeping is stored here.
                    block["half_height"] = half_height
                sink["blocks"].append(block)


def _load_lane_source(asset_id, lane_mesh_dir, cache):
    """Import one part .glb once and reduce it to bundle-frame arrays plus its materials.

    The importer hands back Blender-local coordinates (glTF x, -z, y); the bundle frame is
    recovered as (-x, z, -y) of those — the writer's X mirror undone, axes restored. That map
    reverses the triangle's apparent winding (an odd number of axis negations/swaps,
    determinant -1). `build_lanes` applies the SAME determinant-(-1) map again later
    (`verts[:, [0, 2, 1]]`, converting the bent bundle-frame verts to Blender frame), so the
    two cancel: net effect is a proper rotation (a 180° turn about the vertical axis — see
    the frame comment at the top of this file). The triangle winding read straight off the
    glTF mesh here, with NO reversal, already matches what the old load-then-from_pydata
    double reversal produced (each reversal undid the other — checked directly against the
    old pipeline's final loop order, not just the determinant argument). Road markings,
    draped and lit from above, render black the moment this is wrong — the gorge/suburb A/B
    renders catch it immediately.
    """
    if asset_id in cache:
        return cache[asset_id]

    path = os.path.join(lane_mesh_dir, asset_id + ".glb")
    source = None
    if os.path.isfile(path):
        imported = import_glb(path)
        for obj in imported:
            if obj.type != "MESH" or source is not None:
                continue
            mesh = obj.data
            count = len(mesh.vertices)
            co = numpy.empty(count * 3, dtype=numpy.float64)
            mesh.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            verts = numpy.stack((-co[:, 0], co[:, 2], -co[:, 1]), axis=1)

            mesh.calc_loop_triangles()
            tri_count = len(mesh.loop_triangles)
            tri_verts = numpy.empty(tri_count * 3, dtype=numpy.int64)
            mesh.loop_triangles.foreach_get("vertices", tri_verts)
            tris = tri_verts.reshape(-1, 3)

            materials = list(mesh.materials) if mesh.materials else [None]
            if mesh.materials:
                tri_poly = numpy.empty(tri_count, dtype=numpy.int64)
                mesh.loop_triangles.foreach_get("polygon_index", tri_poly)
                poly_material = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
                mesh.polygons.foreach_get("material_index", poly_material)
                tri_material = poly_material[tri_poly]
            else:
                tri_material = numpy.zeros(tri_count, dtype=numpy.int64)

            uv_layer = mesh.uv_layers.active
            if uv_layer is not None and tri_count:
                tri_loops = numpy.empty(tri_count * 3, dtype=numpy.int64)
                mesh.loop_triangles.foreach_get("loops", tri_loops)
                all_uv = numpy.empty(len(mesh.loops) * 2, dtype=numpy.float64)
                uv_layer.data.foreach_get("uv", all_uv)
                uvs = all_uv.reshape(-1, 2)[tri_loops].reshape(-1, 3, 2)
            else:
                uvs = numpy.zeros((tri_count, 3, 2), dtype=numpy.float64)

            quad = _decal_quad_info(verts, tris, uvs)
            if quad is not None:
                quad["material"] = materials[int(tri_material[0])] if tri_count else None
            source = {"verts": verts, "tris": tris, "uvs": uvs,
                     "tri_material": tri_material, "materials": materials, "quad": quad}
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)

    cache[asset_id] = source
    return source


_DRAPE_DEDUP_MM = 1.0  # quantise world (x, z) to this many millimetres before deduping ray-casts


def _drape_block(block, road_surface, counters):
    """Sample the road surface at each vertex's own (x, z) — including its lateral offset —
    deduping ray-casts on rounded world position, not on curve station.

    (2026-08-07, fix round 1: an earlier version of this function sampled the bézier
    CENTERLINE per station and broadcast the result to every vertex at that station,
    including ones offset sideways by the part's own width. That is only exact for a
    zero-width part; for anything wider (crosswalks, multi-lane decals) it reintroduced a
    milder version of the exact kerb/crown error Tier 0 was built to remove — the left and
    right edge of a cambered carriageway were both draped to a THIRD point's height, neither
    edge's own. Caught in review, not by the original Tier 1b measurement, because that
    measurement also only ever varied arc position, never width — see task-6-report.md
    "Fix round 1" for the corrected measurement.)

    Deduping on rounded (x, z) instead of on station is what makes this exact rather than a
    weaker approximation: two DIFFERENT stations that clip to the same arc position (the
    tiling-clip case in §5c) or two literal duplicate source vertices still cost one cast,
    but a station's left and right edge — different x — are never merged. For a plain
    2-vertex-per-station quad this gives up most of the old ~2x L/R dedup (every row's two
    edges are now almost always distinct casts); what remains is Tier 1c's station-count
    reduction. A miss (no surface within the marking's own reach) keeps that vertex's bent
    height, exactly like the old per-vertex miss path — no fill-in borrowed from a neighbour.
    """
    verts = block["verts"]
    half = block["half_height"]
    eps = block["drape_eps"]

    x, y_pre, z = verts[:, 0], verts[:, 1], verts[:, 2]
    scale = 1000.0 / _DRAPE_DEDUP_MM
    keys = numpy.stack((numpy.round(x * scale).astype(numpy.int64),
                        numpy.round(z * scale).astype(numpy.int64)), axis=1)
    _, first_index, inverse = numpy.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)

    new_y = y_pre.copy()
    draped = missed = 0
    for u, idx in enumerate(first_index):
        counters["queries"] += 1
        top = road_surface.drop(float(x[idx]), float(z[idx]), float(y_pre[idx]), half, half)
        at_key = inverse == u
        if top is None:
            missed += int(at_key.sum())
            continue
        new_y[at_key] = top + eps
        draped += int(at_key.sum())

    verts[:, 1] = new_y
    counters["draped"] += draped
    counters["missed"] += missed


def _lane_object_arrays(blocks):
    """Concatenate one prefab's blocks into a single verts/tris/uvs/material_runs set, still
    in bundle frame, ready for the one Blender-frame axis swap and `_mesh_from_arrays`."""
    counts = [len(block["verts"]) for block in blocks]
    offsets = numpy.concatenate(([0], numpy.cumsum(counts)))[:-1]
    verts = numpy.concatenate([block["verts"] for block in blocks], axis=0)
    tris = numpy.concatenate(
        [block["tris"] + offset for block, offset in zip(blocks, offsets)], axis=0)
    uvs = numpy.concatenate([block["uvs"].reshape(-1, 2) for block in blocks], axis=0)
    material_runs = [(block["material"], len(block["tris"])) for block in blocks]
    return verts, tris, uvs, material_runs


def build_lanes(lanes_path, catalogue_path, lane_mesh_dir, collection,
                center=None, radius=None, limit=None, road_surface=None):
    """Bend every lane record's parts along its curve, merged into one object per prefab."""
    if not os.path.isfile(lanes_path):
        LOG.warn(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")
        return 0
    if not os.path.isfile(catalogue_path):
        LOG.warn(f"CONTRACT VIOLATION: lanemeshes.jsonl without {catalogue_path}")
        return 0

    with open(catalogue_path, encoding="utf-8") as handle:
        catalogue = json.load(handle)

    sources = {}
    builders = {}
    counts = {}
    bent = missing_prefab = missing_mesh = skipped_region = skipped_limit = 0

    for record in read_jsonl(lanes_path):
        entry = catalogue.get(record.get("prefab"))
        if entry is None:
            missing_prefab += 1
            continue

        curve = record["curve"]
        if radius is not None and center is not None:
            if math.hypot(curve[0][0] - center[0], curve[0][2] - center[1]) > radius:
                skipped_region += 1
                continue
        if limit is not None and bent >= limit:
            skipped_limit += 1
            continue

        curve_length = _curve_length(curve)
        spans = record.get("spans") or [[0.0, 1.0]]
        width_scale = record.get("widthScale", [1.0, 1.0])

        hang = None
        distances = record.get("hang")
        hanging = entry.get("hanging", 0.0)
        if distances is not None or hanging:
            d0, d1 = distances if distances is not None else (0.0, 0.0)
            # The game's quadratic-to-cubic lift of the hanging parabola, at calm air.
            hang = (d0, (d0 + hanging * curve_length) * 2 / 3,
                    (d1 + hanging * curve_length) * 2 / 3, d1)

        # Grouped by (prefab, paint): one object per prefab would lose the per-lane colorSet
        # (§7.2), and the distinct paints per prefab are few — the variation table is small.
        color_set = record.get("colorSet")
        key = (record["prefab"], json.dumps(color_set, sort_keys=True) if color_set else "")
        sink = builders.setdefault(key, {"blocks": [], "colorSet": color_set})

        for index in record.get("parts", []):
            parts = entry.get("parts", [])
            if not 0 <= index < len(parts):
                continue
            source = _load_lane_source(parts[index]["assetId"], lane_mesh_dir, sources)
            if source is None:
                missing_mesh += 1
                continue
            _bend_lane_part(parts[index], source, curve, spans, curve_length,
                            width_scale, hang, sink)

        bent += 1
        category = record.get("category", "lane")
        counts[category] = counts.get(category, 0) + 1
        LOG.progress("lanes bent", bent, None)

    # The bend loop above prints its progress; everything below (drape ray-casts, lazy
    # road-surface BVH builds, object construction) is the quiet expensive part of a
    # full-city run — announce it so a long silence reads as work, not a hang.
    drape_blocks = [block for sink in builders.values() for block in sink["blocks"]
                    if block["drape_eps"] is not None]
    to_drape = sum(len(block["verts"]) for block in drape_blocks)
    if road_surface is not None and to_drape:
        LOG.step(f"bending done; draping {len(drape_blocks)} marking(s) "
                 f"({to_drape} decal vertex(es)) onto the road surface and "
                 f"building {len(builders)} object(s)")

    # One ray-cast per unique WORLD (x, z), not per vertex (see _drape_block) — exact
    # because it samples each vertex's own laterally-offset position (fix round 1: an
    # earlier version deduped per curve STATION and broadcast one centerline sample to a
    # whole row, which was wrong for any part with width). The remaining cut versus the old
    # per-vertex drape is genuinely-coincident positions (tiling-clip stations, duplicate
    # source verts) plus Tier 1c's fewer stations overall — NOT the ~2x L/R dedup a
    # station-keyed version would get, since a wide part's left and right edge now almost
    # always cast separately, as they must. Progress strides in markings (blocks), not
    # vertices or queries: there are two orders of magnitude fewer of them, and the "draped"
    # count on the summary line below is the one tools/ab_compare.py's COUNT_MARKERS is
    # expected to move (Tier 1c changed how many decal vertices exist at all) — quote old vs
    # new there.
    counters = {"draped": 0, "missed": 0, "queries": 0}
    if road_surface is not None:
        for seen, block in enumerate(drape_blocks, 1):
            _drape_block(block, road_surface, counters)
            if seen % 5000 == 0:
                LOG.step(f"{seen}/{len(drape_blocks)} marking(s) draped")
    draped, drape_missed = counters["draped"], counters["missed"]
    if counters["queries"]:
        LOG.step(f"{counters['queries']} drape ray-cast(s) issued for "
                 f"{to_drape} decal vertex(es)")

    # A heavily tiled non-decal part (train sleepers/rails, catenary cable — a repeated
    # section stamped along the whole curve) can have its LAST tile's clip window fall short
    # of a full section: every station past the clip boundary gets clamped to the same arc
    # position (see the `numpy.clip` in the tile loop above), so the vertices between them
    # collapse onto one point-and-width axis — geometrically zero area (distinct indices),
    # but not a hint of bad data, just where a fixed-size repeating section doesn't divide
    # the span evenly. This is NOT specific to the array rewrite: the previous
    # `mesh.from_pydata` pipeline built the identical degenerate triangles from the identical
    # bend math, it just never noticed (an invisible, zero-area face costs nothing to leave
    # in a mesh, so nothing ever caught it) — proven by direct old-vs-new classification, see
    # task-6-report.md §5c. `_mesh_from_arrays` now does notice, and would otherwise print a
    # `CONTRACT VIOLATION` per object for this ONE class — zero-area, distinct indices — which
    # on a full city is ~2,000 lines about a phenomenon that is not a violation. One
    # aggregated line at the end of the phase instead. This does NOT cover a repeated-vertex-
    # index face: `_mesh_from_arrays` reports that class with the loud wording unconditionally,
    # from every caller including this one — `degenerate_report` is never consulted for it, so
    # a genuine future regression in the bend (not this known, benign tiling-clip artifact)
    # still surfaces loudly instead of being swallowed by this aggregate.
    degenerate_totals = {"faces": 0, "objects": 0}

    def _report_lane_degenerate(_name, zero_area_count, repeated):
        # `repeated` is always False here: lanes are COMPUTED geometry (source_geometry
        # defaults to False on this call), so `_mesh_from_arrays` never routes a
        # repeated-vertex-index count through this callback — only the zero-area class is
        # ever downgradable for a computed caller. Asserting it costs nothing and pins the
        # invariant in place if that ever changes.
        assert not repeated
        degenerate_totals["faces"] += zero_area_count
        degenerate_totals["objects"] += 1

    built = 0
    for (prefab_key, _), sink in builders.items():
        blocks = [block for block in sink["blocks"] if len(block["tris"])]
        if not blocks:
            continue

        verts, tris, uvs, material_runs = _lane_object_arrays(blocks)
        verts = verts[:, [0, 2, 1]]  # bundle -> Blender: X stays, Y<->Z swap, no sign flip
        mesh = _mesh_from_arrays(f"lane_{prefab_key}", verts, tris,
                                 uv_layers={"UVMap": uvs}, material_runs=material_runs,
                                 poly_size=3, smooth=False, degenerate_report=_report_lane_degenerate)
        obj = bpy.data.objects.new(f"lane_{prefab_key}", mesh)
        collection.objects.link(obj)
        if sink["colorSet"] is not None:
            set_paint(obj, {"colorSet": sink["colorSet"]})
        built += 1

    LOG.done(f"lanes:     {bent} record(s) bent into {built} object(s), "
             f"{len(sources)} part mesh(es) loaded")
    if degenerate_totals["faces"]:
        # tools/ab_compare.py's COUNT_MARKERS ("face(s)", "object(s)") match this line, so any
        # two logs being diffed with --counts must have the identical number of degenerate
        # faces/objects to compare equal — a run-to-run change here is a real signal, but note
        # for lane-region regression scripts: this line did not exist before this task, so an
        # otherwise-unchanged lane import will now always show ONE extra matched line versus a
        # pre-this-task baseline (see task-6-report.md "Fix round 1" for how that was handled
        # in this task's own A/B gate).
        LOG.step(f"{degenerate_totals['faces']} zero-area degenerate face(s) dropped across "
                 f"{degenerate_totals['objects']} object(s) — a heavily tiled part's last "
                 "tile clipping short of a full section, not a bundle defect")
    if draped or drape_missed:
        LOG.step(f"{draped} decal vertex(es) draped onto the road surface"
                 + (f", {drape_missed} kept the fixed lift (no surface hit)" if drape_missed else ""))
    elif road_surface is None:
        LOG.warn("networks layer not built — decals keep the fixed lift, expect "
                 "markings to z-fight or sink into the crown")
    if road_surface is not None and road_surface.kerb_rejections:
        LOG.step(f"{road_surface.kerb_rejections} drape sample(s) where the nearest-surface "
                 "pick differed from the highest hit in range (Tier 0: kerb/wall snap avoided)")
    for category, count in sorted(counts.items()):
        LOG.step(f"category {category}: {count}")
    if skipped_region:
        LOG.step(f"{skipped_region} skipped outside the region")
    if skipped_limit:
        LOG.step(f"{skipped_limit} skipped by --net-limit")
    if missing_prefab:
        LOG.warn(f"CONTRACT VIOLATION: {missing_prefab} record(s) name a prefab "
                 f"missing from laneprefabs.json")
    if missing_mesh:
        LOG.warn(f"CONTRACT VIOLATION: {missing_mesh} part reference(s) missing "
                 f"their .glb under {lane_mesh_dir}")
    return bent


# -- terrain, contract §8 -------------------------------------------------------------------
#
# The terrain is the Phase 6 acceptance layer: the ground everything else stands on, with the
# road-flattened cells recorded in surface_index.png. (It is NOT what closes junctions — the
# arms' node meshes pave those; audit §18 corrected an earlier claim here.) The rasters are
# decoded in plain Python rather than through Blender's image loader so that no
# colour-management transform can touch the samples — the decoder below is also deliberately a
# reference implementation of §8's file format for any consumer to lift.


def _unfilter_slow(kind, line, previous, bpp):
    """Reference per-byte reconstruction for Average(3) and Paeth(4).

    Both predict from bytes of the CURRENT row that this same loop has already reconstructed
    (`line[i - bpp]`), so — unlike None/Up/Sub — they cannot be vectorised across the row and
    keep the original sequential algorithm. `line` is mutated in place and returned.
    Average(3) 和 Paeth(4) 都要用本行中【已经被本循环重建过】的字节做预测（`line[i - bpp]`），
    和 None/Up/Sub 不同，无法整行向量化，因此保留原始的逐字节顺序算法。`line` 原地修改并返回。
    """
    stride = len(line)
    if kind == 3:      # Average
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif kind == 4:    # Paeth
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up = previous[i]
            corner = previous[i - bpp] if i >= bpp else 0
            estimate = left + up - corner
            distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
            predictor = (left, up, corner)[distances.index(min(distances))]
            line[i] = (line[i] + predictor) & 0xFF
    else:
        raise ValueError(f"_unfilter_slow: unsupported filter type {kind}")
    return line


def _unfilter_scanlines(raw, width, height, bpp):
    """Undo the PNG scanline filters (contract §8) with numpy where the filter allows it.

    None(0) and Up(2) vectorise across the whole row (a straight copy / a row-wide add).
    Sub(1) is a running sum per interleaved byte lane — reshaping the row to (pixel, channel)
    and taking a cumulative sum down the pixel axis reproduces `recon[i] = filt[i] +
    recon[i - bpp]` for every channel at once; the sum is widened to uint32 before truncating
    back to uint8 because numpy's uint8 cumsum overflows silently instead of wrapping mod 256,
    and the truncation *is* the mod-256 the format requires. Average(3) and Paeth(4) predict
    from the current row's own already-reconstructed bytes and are genuinely sequential, so
    they fall back to `_unfilter_slow`, paid only on the rows that actually use them (the
    producer only ever writes filter 0, so in practice that fallback is cold).
    在 numpy 允许的范围内还原 PNG 的逐行滤波：None(0) 和 Up(2) 可以整行向量化（直接拷贝 /
    整行相加）；Sub(1) 是按交织字节通道的累加和——把整行重塑为 (像素, 通道) 后沿像素轴做累加和，
    等价于对每个通道并行计算 `recon[i] = filt[i] + recon[i - bpp]`；先加宽到 uint32 再截断回
    uint8，是因为 numpy 的 uint8 累加和会静默溢出而不是按 mod 256 回绕，这里的截断本身就是格式
    要求的 mod 256。Average(3) 和 Paeth(4) 依赖本行自身已重建的字节做预测，本质上是顺序算法，
    回退到 `_unfilter_slow`，只有实际用到这两种滤波的行才付出这个代价（生产者只写 filter 0，
    所以这条回退路径在实践中是冷路径）。
    """
    stride = width * bpp
    out = numpy.zeros((height, stride), dtype=numpy.uint8)
    previous = numpy.zeros(stride, dtype=numpy.uint8)
    for y in range(height):
        offset = y * (stride + 1)
        kind = raw[offset]
        line = numpy.frombuffer(raw, dtype=numpy.uint8, count=stride, offset=offset + 1).copy()
        if kind == 0:      # None
            pass
        elif kind == 2:    # Up
            line += previous
        elif kind == 1:    # Sub
            lanes = line.reshape(-1, bpp).astype(numpy.uint32)
            line = numpy.cumsum(lanes, axis=0, dtype=numpy.uint32).astype(numpy.uint8).reshape(-1)
        else:              # Average(3) / Paeth(4)
            slow = _unfilter_slow(kind, bytearray(line.tobytes()), bytes(previous.tobytes()), bpp)
            line = numpy.frombuffer(bytes(slow), dtype=numpy.uint8)
        out[y] = line
        previous = out[y]
    return bytearray(out.tobytes())


def _decode_png_gray(path):
    """Decode a single-channel PNG (§8) to (width, height, samples).

    Samples come back as an array of ints at the file's own bit depth — 16-bit values 0..65535
    for elevation and depth, 8-bit ids for the surface index. All five standard scanline
    filters are handled; the producer writes filter 0 (None) rows, which is the fast path.
    """
    import zlib
    from array import array

    data = open(path, "rb").read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{path} is not a PNG")

    pos = 8
    width = height = depth = None
    colour_type = None
    idat = []
    while pos + 8 <= len(data):
        (length,) = __import__("struct").unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = __import__("struct").unpack(">II", payload[:8])
            depth, colour_type = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break

    if colour_type != 0 or depth not in (8, 16):
        raise ValueError(f"{path}: contract §8 rasters are 8- or 16-bit grayscale, "
                         f"got colour type {colour_type} at depth {depth}")

    raw = zlib.decompress(b"".join(idat))
    bpp = depth // 8
    out = _unfilter_scanlines(raw, width, height, bpp)

    if depth == 16:
        samples = array("H", bytes(out))
        if sys.byteorder == "little":
            samples.byteswap()
    else:
        samples = array("B", bytes(out))
    return width, height, samples


def _decode_png_rgba(path):
    """Decode an RGBA8 PNG (the §8 splatmap) to (width, height, bytes).

    Same chunk walk and filter handling as _decode_png_gray at four bytes per pixel; the
    producer writes filter 0 (None) rows, which is the fast path.
    """
    import struct
    import zlib

    data = open(path, "rb").read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{path} is not a PNG")

    pos = 8
    width = height = depth = None
    colour_type = None
    idat = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
            depth, colour_type = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break

    if colour_type != 6 or depth != 8:
        raise ValueError(f"{path}: the splatmap is RGBA8, got colour type {colour_type} at depth {depth}")

    raw = zlib.decompress(b"".join(idat))
    out = _unfilter_scanlines(raw, width, height, 4)

    return width, height, bytes(out)


class _Splat:
    """The ripped splat weight map with its geo-reference — the game's own per-pixel ground
    blend (rasters.json "splatmap" block), sampled here only to calibrate which channel is
    which ground; the actual blending happens in the shader nodes."""

    def __init__(self, path, block):
        self.path = path
        self.width, self.height, self.pixels = _decode_png_rgba(path)
        size = block.get("size", [self.width, self.height])
        if [self.width, self.height] != list(size):
            raise ValueError(f"splatmap is {self.width}x{self.height}, block says {size}")
        cx, cz = block["resolution"]
        ox, _, oz = block["originWorld"]           # top-left texel CENTRE, row 0 north
        self.min_x = ox - cx * 0.5
        self.max_z = oz + cz * 0.5
        self.cell_x = cx
        self.cell_z = cz
        self.area_x = cx * self.width
        self.area_z = cz * self.height

    def sample(self, world_x, world_z):
        """Nearest-texel RGBA at a world position, 0..255 each."""
        col = int((world_x - self.min_x) / self.cell_x)
        row = int((self.max_z - world_z) / self.cell_z)
        col = min(max(col, 0), self.width - 1)
        row = min(max(row, 0), self.height - 1)
        offset = (row * self.width + col) * 4
        return self.pixels[offset:offset + 4]

    def uv(self, world_x, world_z):
        """Image UV (V = 0 at the south edge, matching row 0 = north) for the shader."""
        return ((world_x - self.min_x) / self.area_x,
                (world_z - (self.max_z - self.area_z)) / self.area_z)


def _terrain_paths(bundle_dir):
    """The §8 layer paths and the `rasters` geo-reference block.

    Reads bundle.json when it exists; otherwise the conventional layout plus the producer's
    pre-manifest terrain/rasters.json sidecar (§8).
    """
    manifest_path = os.path.join(bundle_dir, "bundle.json")
    layers = {}
    rasters = None
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        layers = manifest.get("layers", {})
        rasters = manifest.get("rasters")

    heightmap = os.path.join(bundle_dir, layers.get("terrain", "terrain/heightmap.png"))
    surface = os.path.join(bundle_dir, layers.get("surfaceIndex", "terrain/surface_index.png"))
    cutout = os.path.join(bundle_dir, layers.get("cutout", "terrain/cutout.png"))
    water = os.path.join(bundle_dir, layers.get("water", "terrain/water_level.png"))
    surfaces_dir = os.path.join(bundle_dir, layers.get("surfaces", "terrain/surfaces/"))
    splatmap = os.path.join(bundle_dir, layers.get("splatmap", "terrain/splatmap.png"))

    if rasters is None:
        sidecar = os.path.join(bundle_dir, "terrain", "rasters.json")
        if os.path.isfile(sidecar):
            with open(sidecar, encoding="utf-8") as handle:
                rasters = json.load(handle)

    return heightmap, surface, cutout, water, surfaces_dir, splatmap, rasters


def _splat_ground_material(surfaces_dir, splat, mapping, gain=1.0):
    """One ground material mixing grass/dirt/rock by the game's splat weights.

    The splatmap image is sampled with linear interpolation in its own UV layer (SplatUV),
    so the blend is per-pixel smooth — the game's look — instead of per-face. The ground
    textures keep the world-anchored 8 m tiling of the legacy path. `mapping` names the
    calibrated channel index for grass and, when calibrated, dirt and a painted-rock
    variant — either may be None (the game's own blend has no dirt response under
    roadbeds). Rock always blends regardless: the channels are PAINTED-ground weights and
    cliffs are simply unpainted — measured, very steep faces carry ≈0 in all four channels
    while rendering rock in-game — so the base ground is the residual
    max(0, 1 − (R+G+B+A)), textured rock here, plus the painted variant when named.
    """
    material = bpy.data.materials.new("terrain_ground")
    material.use_nodes = True
    tree = material.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95

    world_uv = tree.nodes.new("ShaderNodeUVMap")
    world_uv.uv_map = "UVMap"

    def ground_texture(name, fallback_colour):
        path = os.path.join(surfaces_dir, name)
        if os.path.isfile(path):
            node = tree.nodes.new("ShaderNodeTexImage")
            node.image = bpy.data.images.load(path)
            tree.links.new(world_uv.outputs["UV"], node.inputs["Vector"])
            return node.outputs["Color"]
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.outputs[0].default_value = fallback_colour
        return rgb.outputs[0]

    grass = ground_texture("grass.png", (0.20, 0.30, 0.12, 1.0))
    dirt = ground_texture("dirt.png", (0.35, 0.28, 0.20, 1.0)) if mapping.get("dirt") is not None else None
    rock = ground_texture("rock.png", (0.30, 0.28, 0.26, 1.0))

    splat_uv = tree.nodes.new("ShaderNodeUVMap")
    splat_uv.uv_map = "SplatUV"
    splat_node = tree.nodes.new("ShaderNodeTexImage")
    splat_node.image = bpy.data.images.load(splat.path)
    splat_node.image.colorspace_settings.name = "Non-Color"
    splat_node.interpolation = "Linear"
    splat_node.extension = "EXTEND"
    tree.links.new(splat_uv.outputs["UV"], splat_node.inputs["Vector"])

    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(splat_node.outputs["Color"], separate.inputs["Color"])
    channels = {0: separate.outputs["Red"], 1: separate.outputs["Green"],
                2: separate.outputs["Blue"], 3: splat_node.outputs["Alpha"]}

    def math(op, a, b):
        node = tree.nodes.new("ShaderNodeMath")
        node.operation = op
        for socket, value in zip((node.inputs[0], node.inputs[1]), (a, b)):
            if hasattr(value, "is_linked"):
                tree.links.new(value, socket)
            else:
                socket.default_value = value
        return node.outputs[0]

    def mix(colour_a, colour_b, fac):
        node = tree.nodes.new("ShaderNodeMixRGB")
        node.blend_type = "MIX"
        tree.links.new(fac, node.inputs["Fac"])
        tree.links.new(colour_a, node.inputs["Color1"])
        tree.links.new(colour_b, node.inputs["Color2"])
        return node.outputs["Color"]

    # The rock weight is the unpainted residual — max(0, 1 − ΣRGBA) — plus the painted
    # rock-variant channel when calibration named one.
    channel_sum = channels[0]
    for k in (1, 2, 3):
        channel_sum = math("ADD", channel_sum, channels[k])
    rock_weight = math("MAXIMUM", math("SUBTRACT", 1.0, channel_sum), 0.0)
    if mapping.get("rock") is not None:
        rock_weight = math("ADD", rock_weight, channels[mapping["rock"]])

    # Normalised chained lerp over the grounds: at each step
    # mix(so_far, next, w_next/(sum_so_far + w_next)) — exactly weights/sum, with an
    # epsilon so a zero-weight texel cannot divide by zero.
    blended = grass
    total = channels[mapping["grass"]]
    for weight, colour in ((channels[mapping["dirt"]] if dirt is not None else None, dirt),
                           (rock_weight, rock)):
        if colour is None:
            continue
        total = math("ADD", total, weight)
        blended = mix(blended, colour, math("DIVIDE", weight, math("ADD", total, 1e-4)))

    if gain != 1.0:
        gain_node = tree.nodes.new("ShaderNodeMixRGB")
        gain_node.blend_type = "MULTIPLY"
        gain_node.inputs["Fac"].default_value = 1.0
        gain_node.inputs["Color2"].default_value = (gain, gain, gain, 1.0)
        tree.links.new(blended, gain_node.inputs["Color1"])
        blended = gain_node.outputs["Color"]

    tree.links.new(blended, bsdf.inputs["Base Color"])
    return material


def _ground_material(name, texture_path, fallback_colour, gain=1.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    if texture_path and os.path.isfile(texture_path):
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = bpy.data.images.load(texture_path)
        upstream = image_node.outputs["Color"]
        if gain != 1.0:
            # Presentation gain (_GROUND_GAIN): the authored theme diffuse is dark and only
            # reads right under the game's own exposure. Multiply node, so the texture on
            # disk stays the faithful rip.
            mix = material.node_tree.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs["Fac"].default_value = 1.0
            mix.inputs["Color2"].default_value = (gain, gain, gain, 1.0)
            material.node_tree.links.new(upstream, mix.inputs["Color1"])
            upstream = mix.outputs["Color"]
        material.node_tree.links.new(upstream, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = fallback_colour
    return material


def build_terrain(bundle_dir, collection, center, radius):
    """Place the §8 terrain under everything: ground grid, roadbed split, water surface.

    Returns the number of objects placed (0 when the layer is absent). The region filter crops
    the grid to the same window as everything else; without one, the grid is subsampled to at
    most ~1024 cells per axis so a full 4096² map stays viewable.
    """
    heightmap_path, surface_path, cutout_path, water_path, surfaces_dir, splat_path, rasters = _terrain_paths(bundle_dir)
    if not os.path.isfile(heightmap_path):
        LOG.warn(f"no terrain layer at {heightmap_path} — skipping terrain")
        return 0
    if rasters is None:
        LOG.warn("terrain rasters present but no geo-reference (bundle.json rasters or "
                 "terrain/rasters.json) — skipping terrain")
        return 0

    width, height, heights = _decode_png_gray(heightmap_path)
    cell = float(rasters["resolution"])
    origin_x, _, origin_z = rasters["originWorld"]
    elevation_scale = float(rasters.get("elevationScale", 1.0))
    elevation_offset = float(rasters.get("elevationOffset", 0.0))

    surface_ids = None
    if os.path.isfile(surface_path):
        sw, sh, surface_ids = _decode_png_gray(surface_path)
        if (sw, sh) != (width, height):
            LOG.warn(f"CONTRACT VIOLATION: surface_index is {sw}x{sh}, heightmap {width}x{height}")
            surface_ids = None

    cut_ids = None
    if os.path.isfile(cutout_path):
        cw, ch, cut_ids = _decode_png_gray(cutout_path)
        if (cw, ch) != (width, height):
            LOG.warn(f"CONTRACT VIOLATION: cutout is {cw}x{ch}, heightmap {width}x{height}")
            cut_ids = None

    levels = None
    if os.path.isfile(water_path):
        ww, wh, levels = _decode_png_gray(water_path)
        if (ww, wh) != (width, height):
            LOG.warn(f"CONTRACT VIOLATION: water_level is {ww}x{wh}, heightmap {width}x{height}")
            levels = None

    # The crop window, in raster indices. Row 0 is the northernmost (§3: rows toward -Z).
    if center is not None and radius is not None:
        margin = 2.0 * cell
        col0 = max(0, int((center[0] - radius - margin - origin_x) / cell))
        col1 = min(width - 1, int(math.ceil((center[0] + radius + margin - origin_x) / cell)))
        row0 = max(0, int((origin_z - (center[1] + radius + margin)) / cell))
        row1 = min(height - 1, int(math.ceil((origin_z - (center[1] - radius - margin)) / cell)))
        stride = 1
    else:
        col0, col1, row0, row1 = 0, width - 1, 0, height - 1
        stride = max(1, (max(width, height) + 1023) // 1024)

    if col0 >= col1 or row0 >= row1:
        LOG.warn("terrain: the region window misses the raster entirely — skipping terrain")
        return 0

    cols = list(range(col0, col1 + 1, stride))
    rows = list(range(row0, row1 + 1, stride))

    nrows, ncols = len(rows), len(cols)
    rows_arr = numpy.asarray(rows, dtype=numpy.int64)
    cols_arr = numpy.asarray(cols, dtype=numpy.int64)

    # Heights: the decoded PNG16 raster as (H, W), windowed to the sampled grid by the same
    # cols/rows lists everything else uses BEFORE the elevationScale/Offset multiply-add —
    # a full 4096² city raster is 128 MB once promoted to float64, and only the sampled
    # window (a few hundred cells on a side, even unstrided) is ever read. sampled_y[r, c] is
    # exactly what the scalar code's world_y(rows[r], cols[c]) returned.
    heights_native = numpy.asarray(heights).reshape(height, width)
    sampled_y = heights_native[numpy.ix_(rows_arr, cols_arr)].astype(numpy.float64)
    sampled_y = sampled_y * elevation_scale + elevation_offset

    surface_grid = numpy.asarray(surface_ids).reshape(height, width) if surface_ids is not None else None
    cut_grid = numpy.asarray(cut_ids).reshape(height, width) if cut_ids is not None else None
    level_grid = numpy.asarray(levels).reshape(height, width) if levels is not None else None

    # Vertex grid: world X/Z (meshgrid) + sampled height, row-major (row outer, col inner) so
    # the flat vertex index a = r*ncols + c matches every face-winding formula below exactly
    # as the scalar loop's `index` counter did.
    world_x = origin_x + cols_arr.astype(numpy.float64) * cell
    world_z = origin_z - rows_arr.astype(numpy.float64) * cell
    grid_x, grid_z = numpy.meshgrid(world_x, world_z)  # both (nrows, ncols)

    # Roadbed faces sink by a small documented drop (§8 consumer note). The game's road job
    # clamps the ground under a carriageway to the MINIMUM of the road's surface band — below
    # the asphalt top — but this grid linearly interpolates 3.5 m samples (and subsamples by
    # stride without a region), so interpolated crests poke through the road meshes above.
    # Sinking in the game's own clamp direction settles it; every vertex touching a roadbed
    # face drops, so the sink bleeds one cell into the smoothing apron rather than tearing a
    # cliff at the boundary. A face reads "roadbed" from its single top-left sample (r, c) —
    # not all four corners — exactly as the scalar face_roadbed did.
    roadbed_corner = surface_grid[numpy.ix_(rows_arr, cols_arr)] != 0 if surface_grid is not None \
        else numpy.zeros((nrows, ncols), dtype=bool)
    face_roadbed = roadbed_corner[:-1, :-1]

    sink = _ROADBED_SINK if surface_ids is not None else 0.0
    vertex_sunk = numpy.zeros((nrows, ncols), dtype=bool)
    if sink and face_roadbed.size:
        fr, fc = numpy.nonzero(face_roadbed)
        vertex_sunk[fr, fc] = True
        vertex_sunk[fr, fc + 1] = True
        vertex_sunk[fr + 1, fc] = True
        vertex_sunk[fr + 1, fc + 1] = True

    # -- the ground grid -------------------------------------------------------------------
    grid_y = sampled_y - numpy.where(vertex_sunk, sink, 0.0)
    verts = numpy.stack((grid_x, grid_z, grid_y), axis=-1).reshape(-1, 3)  # Blender: X east, Y north, Z up

    for row_number in range(nrows):
        # "terrain rows" — deliberately not "terrain:" — stays outside every ab_compare.py
        # count marker so this progress line can print at whatever stride the helper picks
        # without ever becoming a gated line the A/B baseline does not have.
        LOG.progress("terrain rows", row_number + 1, nrows)

    # §8 cutout: cells the game stencil-clips per-frame instead of carving the heightmap —
    # the terrain covering tunnel portals and sunken trenches. Keeping their faces walls off
    # every underground network, so a face is dropped when any of its corner samples is cut;
    # the opening over-reaches by at most half a cell, leaving a rim instead of a wall.
    if cut_grid is not None:
        cut_corner = cut_grid[numpy.ix_(rows_arr, cols_arr)] != 0
        face_cut = (cut_corner[:-1, :-1] | cut_corner[:-1, 1:] |
                    cut_corner[1:, :-1] | cut_corner[1:, 1:])
    else:
        face_cut = numpy.zeros((max(nrows - 1, 0), max(ncols - 1, 0)), dtype=bool)
    face_kept = ~face_cut
    cut_faces = int(face_cut.sum())

    # Row 0 is the north edge, so winding (a, a+ncols, a+ncols+1, a+1) — south then east —
    # keeps the face normal pointing up in Blender's right-handed frame. Faces stay QUADS
    # (poly_size=4) rather than being split into triangles here: a quad's diagonal choice
    # changes its silhouette under a slope, and the scalar baseline never had to make that
    # choice, so keeping the same quad topology through _mesh_from_arrays lets Blender
    # tessellate it exactly as it always has instead of risking a diagonal mismatch under
    # the pixel A/B gate.
    r_idx = numpy.arange(max(nrows - 1, 0))
    c_idx = numpy.arange(max(ncols - 1, 0))
    a_grid = r_idx[:, None] * ncols + c_idx[None, :]
    quads_all = numpy.stack((a_grid, a_grid + ncols, a_grid + ncols + 1, a_grid + 1), axis=-1)
    quads = quads_all[face_kept]
    face_r, face_c = numpy.nonzero(face_kept)  # same row-major order as `quads`, cutout-skipping

    # -- ground classification per face, shared by both texturing paths -------------------
    # 0 = flat natural (grass), 1 = roadbed (surface_index), 2 = steep natural (rock).
    # Steep natural ground reads as rock (_ROCK_SLOPE) — the gorge embankments. Sampled
    # heights, not the sunk vertices, so the roadbed drop cannot flip a boundary face.
    run_x = numpy.maximum((cols_arr[1:] - cols_arr[:-1]).astype(numpy.float64) * cell, 1e-6)
    run_z = numpy.maximum((rows_arr[1:] - rows_arr[:-1]).astype(numpy.float64) * cell, 1e-6)
    y00, y01 = sampled_y[:-1, :-1], sampled_y[:-1, 1:]
    y10, y11 = sampled_y[1:, :-1], sampled_y[1:, 1:]
    gx = numpy.maximum(numpy.abs(y01 - y00), numpy.abs(y11 - y10)) / run_x[None, :]
    gz = numpy.maximum(numpy.abs(y10 - y00), numpy.abs(y11 - y01)) / run_z[:, None]
    face_class_all = numpy.where(numpy.hypot(gx, gz) > _ROCK_SLOPE, 2, 0)
    face_class_all = numpy.where(face_roadbed, 1, face_class_all)  # roadbed wins over slope

    face_class = face_class_all[face_kept]  # same order as `quads` and `face_r`/`face_c`
    roadbed_faces = int((face_class == 1).sum())
    rock_faces = int((face_class == 2).sum())

    # -- the game's own ground blend, when the bundle carries it --------------------------
    # terrain/splatmap.png is the 4096² weight map the terrain shader samples per pixel
    # (rasters "splatmap" block). Which RGBA channel is which ground is not stated anywhere
    # in managed game code, so it is CALIBRATED here against signals this importer already
    # trusts: mean channel weight over flat natural faces names grass, over steep faces
    # names rock. Dirt is OPTIONAL: measured on a full-city rip, the game's blend weights
    # the grass channel as heavily under roadbeds (mean 251/255) as under lawns (243) —
    # the dirt look beside roads comes from wear decals and surface areas, not this map —
    # so a roadbed winner equal to grass means "no dirt channel", not a failed calibration.
    # Only a grass channel that cannot be named at all falls back to per-face painting.
    splat = None
    splat_mapping = None
    splat_block = rasters.get("splatmap") if isinstance(rasters, dict) else None
    if splat_block and os.path.isfile(splat_path):
        try:
            splat = _Splat(splat_path, splat_block)
        except Exception as error:  # noqa: BLE001 - any decode failure just means fallback
            LOG.warn(f"splatmap unreadable ({error}); falling back to per-face ground painting")
            splat = None
    if splat is not None:
        # Sample by INDEX into the kept (post-cutout), row-major face list — same stride,
        # same order, same starting offset as the scalar `for index in range(0, len(face_rc),
        # stride_faces)` walk — so the subsample (and therefore the printed calibration
        # numbers) comes out identical, not just similarly distributed.
        stride_faces = max(1, len(face_r) // 60000)
        sel = numpy.arange(0, len(face_r), stride_faces)
        sel_r, sel_c, sel_cls = face_r[sel], face_c[sel], face_class[sel]
        sample_x = origin_x + (cols_arr[sel_c] + cols_arr[sel_c + 1]).astype(numpy.float64) * 0.5 * cell
        sample_z = origin_z - (rows_arr[sel_r] + rows_arr[sel_r + 1]).astype(numpy.float64) * 0.5 * cell

        splat_pixels = numpy.frombuffer(splat.pixels, dtype=numpy.uint8).reshape(splat.height, splat.width, 4)
        texel_col = numpy.clip(((sample_x - splat.min_x) / splat.cell_x).astype(numpy.int64), 0, splat.width - 1)
        texel_row = numpy.clip(((splat.max_z - sample_z) / splat.cell_z).astype(numpy.int64), 0, splat.height - 1)
        texels = splat_pixels[texel_row, texel_col].astype(numpy.float64)  # (S, 4), nearest-texel RGBA

        sums = [[0.0] * 4 for _ in range(3)]
        counts = [0, 0, 0]
        for cls in range(3):
            in_class = sel_cls == cls
            counts[cls] = int(in_class.sum())
            if counts[cls]:
                sums[cls] = texels[in_class].sum(axis=0).tolist()

        def winner(cls):
            means = [sums[cls][k] / max(1, counts[cls]) for k in range(4)]
            best = max(range(4), key=means.__getitem__)
            return best, means

        if counts[0] >= 200:
            grass_channel, grass_means = winner(0)
            dirt_channel = None
            if counts[1] >= 200:
                dirt_channel, dirt_means = winner(1)
                if dirt_channel == grass_channel:
                    dirt_channel = None  # the game's blend carries no roadbed response
            # Rock is named by CONTRAST (steep mean − flat mean), not by the steep winner:
            # on grassy mountainsides the grass channel can still dominate steep faces in
            # absolute weight, but the rock channel is the one that GAINS on them (measured
            # full-map: B +14 flat→steep while R drops 243). A winner-based pick fails
            # region-locally and drops whole gorges to the square per-face override.
            rock_channel = None
            if counts[2] >= 200:
                flat_means = [sums[0][k] / max(1, counts[0]) for k in range(4)]
                steep_means = [sums[2][k] / max(1, counts[2]) for k in range(4)]
                contrast = [steep_means[k] - flat_means[k] for k in range(4)]
                best = max(range(4), key=contrast.__getitem__)
                if contrast[best] >= 8.0 and best not in (grass_channel, dirt_channel):
                    rock_channel = best
            splat_mapping = {"grass": grass_channel, "dirt": dirt_channel, "rock": rock_channel}
            names = "RGBA"
            LOG.step(f"splatmap calibrated over {sum(counts)} samples: "
                     f"grass={names[grass_channel]} (mean {grass_means[grass_channel]:.0f}), "
                     f"dirt={'none (wear decals and areas carry the dirt look)' if dirt_channel is None else names[dirt_channel]}, "
                     f"rock=unpainted residual"
                     + (f" + painted variant {names[rock_channel]}" if rock_channel is not None else ""))
        else:
            LOG.warn(f"splatmap calibration short of samples (flat {counts[0]}); "
                     "falling back to per-face ground painting")

    # World-anchored UVs so the ground textures tile at a plausible density without any
    # per-face unwrap. 8 m per repeat reads roughly like the game's near tiling. Per-LOOP,
    # in the same loop order as `quads.ravel()` (each kept face's 4 corners, in face order).
    loop_verts = quads.ravel()
    loop_x, loop_z = verts[loop_verts, 0], verts[loop_verts, 1]
    uv_layers = {"UVMap": numpy.stack((loop_x / 8.0, loop_z / 8.0), axis=-1)}

    if splat_mapping is not None:
        material_runs = [(_splat_ground_material(surfaces_dir, splat, splat_mapping, gain=_GROUND_GAIN),
                           len(quads))]
        # No face overrides on the blend. Roadbed stays grass, as in game: the measured
        # splat paints it 251/255 on the grass channel, and the roadbed LOOK is carried by
        # the ~13%-alpha wear decals and surface areas drawn on top (the sink still
        # applies). Cliffs need no slope override either: unpainted texels ARE the rock
        # weight (the residual in _splat_ground_material), which is what keeps gorge walls
        # smooth instead of the per-face checkerboard.

        # The splat blend samples in its own geo-referenced UV space.
        uv_layers["SplatUV"] = numpy.stack(
            ((loop_x - splat.min_x) / splat.area_x,
             (loop_z - (splat.max_z - splat.area_z)) / splat.area_z), axis=-1)
    else:
        grass = _ground_material("terrain_natural", os.path.join(surfaces_dir, "grass.png"),
                                 (0.20, 0.30, 0.12, 1.0), gain=_GROUND_GAIN)
        dirt = _ground_material("terrain_roadbed", os.path.join(surfaces_dir, "dirt.png"),
                                (0.35, 0.28, 0.20, 1.0), gain=_GROUND_GAIN)
        rock = _ground_material("terrain_rock", os.path.join(surfaces_dir, "rock.png"),
                                (0.30, 0.28, 0.26, 1.0), gain=_GROUND_GAIN)
        by_class = (grass, dirt, rock)
        # Run-length encode face_class into (material, run) pairs — the array is already
        # spatially coherent (grass/dirt/rock form contiguous runs along each sampled row),
        # so this stays a handful of runs, not one per face, while giving every face the same
        # material assignment the scalar `face.material_index = (0, 1, 2)[cls]` walk did.
        if len(face_class):
            change = numpy.nonzero(numpy.diff(face_class) != 0)[0] + 1
            run_starts = numpy.concatenate(([0], change))
            run_ends = numpy.concatenate((change, [len(face_class)]))
            material_runs = [(by_class[int(face_class[start])], int(end - start))
                              for start, end in zip(run_starts, run_ends)]
        else:
            material_runs = []

    mesh = _mesh_from_arrays("bundle_terrain", verts, quads, uv_layers=uv_layers,
                              material_runs=material_runs, poly_size=4, smooth=True)
    # Smooth-shade the grid: the game's terrain is a continuous heightfield, and a
    # flat-shaded quad is uniformly lit — at the full-city stride (17.5 m faces) that reads
    # as a brightness checkerboard over every slope, squares no texture blend can hide.
    terrain_object = bpy.data.objects.new("bundle_terrain", mesh)
    collection.objects.link(terrain_object)
    placed = 1

    # -- the water surface -----------------------------------------------------------------
    # The water raster is the SURFACE LEVEL in the heightmap's encoding (§8, 0 = dry): water
    # exists where level > ground. A face is placed wherever any corner is wet; the level
    # extends one sim cell past the shoreline (producer dilation), so the surface runs flat
    # into the bank and the ground cuts the shoreline where the game's own shoreline is.
    wet_faces = 0
    if level_grid is not None:
        level_corner_raw = level_grid[numpy.ix_(rows_arr, cols_arr)]
        has_level = level_corner_raw != 0  # 0 = no data, same truthiness test as the scalar code
        level_value = level_corner_raw * elevation_scale + elevation_offset
        is_wet_corner = has_level & (level_value > sampled_y)

        face_wet = (is_wet_corner[:-1, :-1] | is_wet_corner[:-1, 1:] |
                    is_wet_corner[1:, :-1] | is_wet_corner[1:, 1:])
        wet_faces = int(face_wet.sum())

        if wet_faces:
            # Every grid point that is a corner of at least one wet face gets a vertex;
            # points never touched by a wet face are never allocated — same sparse vertex
            # set the scalar code's `water_verts` dict built, just addressed by grid position
            # instead of insertion order (invisible to the render: only the resulting
            # geometry and per-face winding matter, not the vertex numbering).
            referenced = numpy.zeros((nrows, ncols), dtype=bool)
            referenced[:-1, :-1] |= face_wet
            referenced[:-1, 1:] |= face_wet
            referenced[1:, :-1] |= face_wet
            referenced[1:, 1:] |= face_wet

            # A corner past the dilated ring carries no level; seat it on the ground so the
            # surface closes instead of tearing.
            surface_y = numpy.where(has_level, level_value, sampled_y)

            flat_ref = referenced.ravel()
            vertex_id = numpy.where(flat_ref, numpy.cumsum(flat_ref) - 1, -1).reshape(nrows, ncols)
            water_all = numpy.stack((grid_x, grid_z, surface_y), axis=-1).reshape(-1, 3)
            water_verts = water_all[flat_ref]

            # Same winding as the ground grid: (v00, v10, v11, v01) — south then east.
            water_quads_all = numpy.stack(
                (vertex_id[:-1, :-1], vertex_id[1:, :-1], vertex_id[1:, 1:], vertex_id[:-1, 1:]),
                axis=-1)
            water_quads = water_quads_all[face_wet]

            water_material = bpy.data.materials.new("terrain_water")
            water_material.use_nodes = True
            water_bsdf = water_material.node_tree.nodes.get("Principled BSDF")
            water_bsdf.inputs["Base Color"].default_value = (0.08, 0.22, 0.35, 1.0)
            water_bsdf.inputs["Roughness"].default_value = 0.05
            if "Alpha" in water_bsdf.inputs:
                water_bsdf.inputs["Alpha"].default_value = 0.8
            if hasattr(water_material, "blend_method"):
                water_material.blend_method = "BLEND"

            water_mesh = _mesh_from_arrays("bundle_water", water_verts, water_quads,
                                            material_runs=[(water_material, len(water_quads))],
                                            poly_size=4, smooth=False)
            water_object = bpy.data.objects.new("bundle_water", water_mesh)
            collection.objects.link(water_object)
            placed += 1

    LOG.done(f"terrain:   {len(rows)}x{ncols} sample window of {width}x{height} at {cell:g} m"
             + (f", stride {stride}" if stride > 1 else ""))
    LOG.step(f"{roadbed_faces} roadbed face(s) (sunk {_ROADBED_SINK:g} m), "
             f"{rock_faces} rock face(s), {wet_faces} water face(s), "
             f"{cut_faces} face(s) cut (tunnel portals / trenches)")
    return placed


# Lift for surface areas above the terrain grid, below everything that stands on them. The
# game draws them as terrain-layer decals; here they are thin geometry riding the ground.
_AREA_LIFT = 0.02


def build_areas(bundle_dir, collection, center, radius):
    """Place the §8b surface areas: driveways, parking-lot pavement, park paths.

    These are the painted patches `AreaBatchSystem` draws from area triangles — the visible
    ground of every meshless prefab (parking lots, sports courts, campsites) and of house
    driveways. One object per area prefab, world-anchored UVs, catalogue texture or colour.
    """
    path = os.path.join(bundle_dir, "areas.jsonl")
    if not os.path.isfile(path):
        LOG.warn("no areas.jsonl — skipping surface areas (bundle predates §8b)")
        return 0

    catalogue = {}
    catalogue_path = os.path.join(bundle_dir, "areaprefabs.json")
    if os.path.isfile(catalogue_path):
        with open(catalogue_path, encoding="utf-8") as handle:
            catalogue = json.load(handle)

    materials = {}

    def area_material(prefab):
        if prefab not in materials:
            entry = catalogue.get(prefab) or {}
            texture = entry.get("texture")
            colour = entry.get("color") or (0.42, 0.40, 0.38, 1.0)
            materials[prefab] = _ground_material(
                f"area_{prefab}",
                os.path.join(bundle_dir, texture) if texture else None,
                (colour[0], colour[1], colour[2], 1.0), gain=_GROUND_GAIN)
        return materials[prefab]

    builders = {}
    placed = skipped_region = dropped_bad = 0
    for record in read_jsonl(path):
        nodes = record.get("nodes") or []
        triangles = record.get("triangles") or []
        if len(nodes) < 3 or not triangles:
            continue

        if radius is not None and center is not None:
            cx = sum(n[0] for n in nodes) / len(nodes)
            cz = sum(n[2] for n in nodes) / len(nodes)
            if math.hypot(cx - center[0], cz - center[1]) > radius:
                skipped_region += 1
                continue

        sink = builders.setdefault(record.get("prefab"), {"verts": [], "faces": []})
        base = len(sink["verts"])
        sink["verts"].extend((n[0], n[2], n[1] + _AREA_LIFT) for n in nodes)
        for tri in triangles:
            if max(tri) >= len(nodes) or min(tri) < 0:
                dropped_bad += 1
                continue
            # Bundle → Blender flips handedness (§2.2), so faces reverse to keep facing up —
            # the same compensation the lane bend applies.
            sink["faces"].append(tuple(base + i for i in reversed(tri)))
        placed += 1

    built = 0
    for prefab, sink in builders.items():
        if not sink["faces"]:
            continue
        mesh = bpy.data.meshes.new(f"area_{prefab}")
        mesh.from_pydata(sink["verts"], [], sink["faces"])
        mesh.materials.append(area_material(prefab))
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            x, y, _ = mesh.vertices[loop.vertex_index].co
            uv_layer.data[loop.index].uv = (x / 4.0, y / 4.0)
        mesh.validate()
        obj = bpy.data.objects.new(f"area_{prefab}", mesh)
        collection.objects.link(obj)
        built += 1

    LOG.done(f"areas:     {placed} surface area(s) into {built} object(s) "
             f"over {len(builders)} prefab(s)"
             + (f", {skipped_region} outside the region" if skipped_region else ""))
    if dropped_bad:
        LOG.warn(f"CONTRACT VIOLATION: {dropped_bad} triangle(s) index out of range")
    return built


# -- scene building -------------------------------------------------------------------------

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


_SCRATCH_NAME = "_carto_import_scratch"


def find_layer_collection(collection, root=None):
    """The LayerCollection for `collection` in the active view layer, or None."""
    root = root or bpy.context.view_layer.layer_collection
    if root.collection is collection:
        return root
    for child in root.children:
        found = find_layer_collection(collection, child)
        if found is not None:
            return found
    return None


class parked_out_of_view_layer:                                 # noqa: N801 - used as a context
    """Keep a collection out of the view layer for the duration of a bulk import.

    The glTF importer ends every file with `bpy.ops.object.select_all(action='DESELECT')`, which
    walks *every object in the view layer* — so once a few thousand segments have landed, each
    further import pays for all of them. Profiled over 500 network meshes it was 70 s of 197 s,
    and it grows with the scene. Objects in an excluded collection are not in the view layer, so
    parking the destination there makes that deselect walk only the handful the current file just
    produced. Everything is linked back at the end and nothing is lost — exclusion hides a
    collection from the view layer, it does not unlink it.
    """

    def __init__(self, collection):
        self.collection = collection
        self.layer = None

    def __enter__(self):
        self.layer = find_layer_collection(self.collection)
        if self.layer is not None:
            self.was_excluded = self.layer.exclude
            self.layer.exclude = True
        return self

    def __exit__(self, *exc):
        if self.layer is not None:
            self.layer.exclude = self.was_excluded
        return False


# -- the direct .glb fast path ---------------------------------------------------------------
#
# The stock `bpy.ops.import_scene.gltf` call costs roughly 200 ms per file almost independent
# of its size (measured: 1541 s for 7,380 network segment .glb over a full-city run, ~209
# ms/file) — the operator/undo-stack/dependency-graph machinery Blender wraps around every
# import, not the parsing itself. A city ripped from CS2 imports thousands of these, and most
# of them reference materials Blender has already built from an earlier file in the same run
# (contract §4/§5: materials are shared across meshes, dedup'd by the producer). This path
# parses the handful of .glb it can prove are simple enough to build directly and reuses those
# already-built materials — it never constructs one, so it can never be the thing that makes a
# building render wrong.
#
# Every guard below exists to make one specific Blender feature the stock importer would apply
# fall through to the stock importer instead of being silently skipped: morph targets, skins,
# animations, non-float32 or sparse accessors, an unsupported primitive mode, an attribute this
# path does not carry through (vertex colours), or a material this run has not built yet. A
# file that trips none of them is one this path has fully accounted for.
#
# glTF is Y-up, right-handed; the bundle producer writes right-handed meshes for exactly this
# reason (§2.2). Blender's importer maps that to its own Z-up, right-handed frame via
# (x, y, z) -> (x, -z, y) — call it G, same letter the frame comment at the top of this file
# already uses for it. That map is a proper rotation (determinant +1, checked: the 3x3 has a
# single -1 off the diagonal and a matching +1, so two row swaps' worth of sign, not one) —
# which is what licenses using the glTF triangle winding as-is below: a rotation preserves
# which face is "front", a reflection would not.
_GLTF_TO_BLENDER = Matrix(((1, 0, 0, 0),
                           (0, 0, -1, 0),
                           (0, 1, 0, 0),
                           (0, 0, 0, 1)))

# Base material name -> the bpy.data.materials datablock a stock import first built for it.
# Populated by `import_glb`'s stock-import branch after every glTF import, consulted by
# `_fast_import_glb` before it will claim a file. Never written to by the fast path itself —
# that is what makes "every material this path uses already exists" a true statement rather
# than something the fast path could accidentally falsify for a later file.
_KNOWN_MATERIALS = {}

# How many .glb this run has built directly vs. handed to the stock importer, reset per phase
# by `_glb_fast_path_counts_reset()` and reported by `_glb_fast_path_counts_log()` — the
# headline number for this path (see module docstring above).
_FAST_GLB_COUNT = 0
_STOCK_GLB_COUNT = 0

# A degenerate polygon `_mesh_from_arrays` drops on the fast path is not a new defect: the
# stock importer builds the exact same source triangle from the exact same file and never
# complains either — `mesh.validate()` silently drops a repeated-index polygon (the same one
# this path's pre-clean removes) and silently keeps a distinct-index zero-area one as
# invisible geometry (verified against Address_Beach_Resort_Dubai.glb and seg_106037.glb, see
# task-7-report.md). Surfacing either class as a per-file CONTRACT VIOLATION here would be
# reporting on the source .glb, not on anything this path did — and, because which file first
# reaches the fast path depends on material-discovery order (a file only takes this path once
# every material it needs has been seen by an earlier stock import), the *set* of files that
# would shout is nondeterministic across --center/--radius/--limit even for the identical
# bundle. Both classes are counted quietly instead (`_record_fast_degenerate`, passed as this
# call's `degenerate_report` with `source_geometry=True`) and folded into one aggregate line
# per phase — deliberately worded without any tools/ab_compare.py COUNT_MARKERS substring
# ("polygon(s)", not "face(s)") so the line stays invisible to `--counts` and a live run's log
# stays comparable to a baseline captured before this path existed, and stays IDENTICAL text
# in shape across different --radius (only the two numbers move, and the gate never reads
# them). Fast/stock split itself is likewise kept off the marker list for the same reason.
_FAST_DEGENERATE_ZERO_AREA_POLYS = 0
_FAST_DEGENERATE_REPEATED_POLYS = 0


def _glb_fast_path_counts_reset():
    global _FAST_GLB_COUNT, _STOCK_GLB_COUNT
    global _FAST_DEGENERATE_ZERO_AREA_POLYS, _FAST_DEGENERATE_REPEATED_POLYS
    _FAST_GLB_COUNT = 0
    _STOCK_GLB_COUNT = 0
    _FAST_DEGENERATE_ZERO_AREA_POLYS = 0
    _FAST_DEGENERATE_REPEATED_POLYS = 0


def _glb_fast_path_counts_log():
    """`LOG.step` the fast/stock split since the last reset — kept off the count-gate markers
    (no "record(s)"/"object(s)"/"face(s)"/… substring) so a live run can call this and stay
    comparable against a baseline log captured before this path existed."""
    LOG.step(f"{_FAST_GLB_COUNT} glb fast-path, {_STOCK_GLB_COUNT} glb via glTF importer")
    total = _FAST_DEGENERATE_ZERO_AREA_POLYS + _FAST_DEGENERATE_REPEATED_POLYS
    if total:
        detail = []
        if _FAST_DEGENERATE_ZERO_AREA_POLYS:
            detail.append(f"{_FAST_DEGENERATE_ZERO_AREA_POLYS} zero-area")
        if _FAST_DEGENERATE_REPEATED_POLYS:
            detail.append(f"{_FAST_DEGENERATE_REPEATED_POLYS} repeated-index")
        LOG.step(f"{total} degenerate polygon(s) among them ({', '.join(detail)}), dropped "
                 "quietly — source .glb artifacts the stock importer tolerates identically "
                 "(silently drops the repeated-index kind, silently keeps the zero-area kind "
                 "invisible), not a defect this path introduced")


def _record_fast_degenerate(_name, dropped, repeated):
    global _FAST_DEGENERATE_ZERO_AREA_POLYS, _FAST_DEGENERATE_REPEATED_POLYS
    if repeated:
        _FAST_DEGENERATE_REPEATED_POLYS += dropped
    else:
        _FAST_DEGENERATE_ZERO_AREA_POLYS += dropped


def _gltf_node_local_matrix(node):
    """One glTF node's local TRS (or explicit `matrix`) as a `mathutils.Matrix`, glTF-space."""
    if "matrix" in node:
        m = node["matrix"]                                  # column-major, per the glTF spec
        return Matrix((m[0:4], m[4:8], m[8:12], m[12:16])).transposed()
    t = node.get("translation", (0.0, 0.0, 0.0))
    r = node.get("rotation", (0.0, 0.0, 0.0, 1.0))           # glTF stores quaternions [x,y,z,w]
    s = node.get("scale", (1.0, 1.0, 1.0))
    translation = Matrix.Translation(Vector(t))
    rotation = Quaternion((r[3], r[0], r[1], r[2])).to_matrix().to_4x4()
    scale = Matrix.Diagonal((s[0], s[1], s[2], 1.0))
    return translation @ rotation @ scale


def _gltf_node_matrix_to_blender(matrix):
    """Conjugate a glTF-space node matrix by G so it lands in Blender's Z-up frame.

    `C @ M @ C.inverted()` with `C` = `_GLTF_TO_BLENDER`. Verified against a stock import of
    Address_Beach_Resort_Dubai.glb (root empty + one translated, multi-material child node):
    `matrix_world` of the matching mesh object came out identical to the float ULP.
    """
    return _GLTF_TO_BLENDER @ matrix @ _GLTF_TO_BLENDER.inverted()


def _parse_glb_arrays(path):
    """Parse one .glb into per-node arrays, or `None` the moment anything is unproven-safe.

    Returns a list of dicts, one per mesh-bearing node (a node whose glTF mesh has more than
    one primitive — trunk + leaves, a building's wall + window materials — is one dict with
    several material runs, exactly mirroring the single Blender object with several material
    slots the stock importer builds for it; verified against AppleTree01.glb and
    Address_Beach_Resort_Dubai.glb, both real bundle files with multi-primitive meshes)::

        {"name": str, "matrix": Matrix (glTF-space world), "verts": (N,3) f32,
         "normals": (N,3) f32, "uvs": (N,2) f32 | None,
         "tris": (M,3) int, "material_runs": [(name, tri_count), ...]}

    Every `return None` below is a deliberate refusal, not an oversight — the caller's answer
    to "can't tell" is always the stock importer, never a guess. The guards enumerate every
    documented way a glTF can be exotic; the try/except around the whole body is the same
    answer for an undocumented one — a shape this parser did not anticipate must fall back to
    the stock importer, not raise and take the whole city-sized import down with it.
    """
    try:
        return _parse_glb_arrays_unguarded(path)
    except Exception:                                            # noqa: BLE001 - fall back, quietly
        return None


def _parse_glb_arrays_unguarded(path):
    import struct

    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 12 or data[:4] != b"glTF":
        return None
    _magic, version, total_length = struct.unpack_from("<III", data, 0)
    if version != 2:
        return None

    json_chunk = None
    bin_chunk = None
    pos = 12
    while pos + 8 <= total_length:
        chunk_length, chunk_type = struct.unpack_from("<II", data, pos)
        payload = data[pos + 8:pos + 8 + chunk_length]
        if chunk_type == 0x4E4F534A:                          # 'JSON'
            json_chunk = payload
        elif chunk_type == 0x004E4942:                         # 'BIN\0'
            bin_chunk = payload
        pos += 8 + chunk_length
    if json_chunk is None:
        return None
    doc = json.loads(json_chunk)

    if doc.get("extensionsRequired") or doc.get("skins") or doc.get("animations"):
        return None

    meshes = doc.get("meshes", [])
    for mesh in meshes:
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4 or prim.get("targets"):
                return None
            attrs = prim.get("attributes", {})
            if not {"POSITION", "NORMAL"} <= set(attrs):
                return None
            if set(attrs) - {"POSITION", "NORMAL", "TEXCOORD_0", "TANGENT"}:
                return None                                    # COLOR_0 or anything exotic
            if "indices" not in prim or "material" not in prim:
                return None

    accessors = doc.get("accessors", [])
    for accessor in accessors:
        if accessor.get("sparse"):
            return None

    buffers = doc.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0]:
        return None                                            # embedded BIN chunk only
    if bin_chunk is None or len(bin_chunk) < buffers[0].get("byteLength", 0):
        return None
    buffer_views = doc.get("bufferViews", [])

    def read_accessor(index, gltf_type, component_type, np_dtype, components):
        accessor = accessors[index]
        if accessor.get("normalized") or accessor.get("type") != gltf_type:
            return None
        if accessor["componentType"] != component_type:
            return None
        view = buffer_views[accessor["bufferView"]]
        if view.get("byteStride"):
            return None                                        # interleaved, not this path
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        count = accessor["count"] * components
        if offset + count * numpy.dtype(np_dtype).itemsize > len(bin_chunk):
            return None
        flat = numpy.frombuffer(bin_chunk, dtype=np_dtype, count=count, offset=offset)
        return flat.reshape(accessor["count"], components) if components > 1 else flat

    def read_indices(index):
        accessor = accessors[index]
        if accessor.get("normalized") or accessor.get("type") != "SCALAR":
            return None
        component_type = accessor["componentType"]
        np_dtype = {5123: numpy.uint16, 5125: numpy.uint32}.get(component_type)
        if np_dtype is None:
            return None
        return read_accessor(index, "SCALAR", component_type, np_dtype, 1)

    materials = doc.get("materials", [])

    scenes = doc.get("scenes", [])
    scene_index = doc.get("scene", 0)
    if not scenes or scene_index >= len(scenes):
        return None
    nodes = doc.get("nodes", [])

    mesh_nodes = []                                             # (name, world matrix, mesh index)

    def walk(node_index, parent_matrix):
        node = nodes[node_index]
        world = parent_matrix @ _gltf_node_local_matrix(node)
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            if mesh_index >= len(meshes) or not meshes[mesh_index].get("primitives"):
                raise ValueError(f"node {node_index} references an empty/missing mesh")
            name = node.get("name") or meshes[mesh_index].get("name") or f"mesh{mesh_index}"
            mesh_nodes.append((name, world, mesh_index))
        for child_index in node.get("children", ()):
            walk(child_index, world)

    for root_index in scenes[scene_index].get("nodes", ()):
        walk(root_index, Matrix.Identity(4))

    result = []
    for name, world, mesh_index in mesh_nodes:
        prims = meshes[mesh_index]["primitives"]

        # A mesh's primitives can share ONE vertex-attribute buffer — every primitive pointing
        # at the very same POSITION/NORMAL/TEXCOORD_0 accessors and differing only in which
        # face range its `indices` selects — rather than each owning independent geometry.
        # Verified against two real bundle files: AppleTree01.glb (trunk + leaves) and
        # NA_CommercialHigh01_L4_3x4.glb (wall + windows) both do this. Reading the attribute
        # accessors once per unique POSITION accessor, keyed by its index, handles both that
        # case and the "each primitive has its own buffer" case uniformly — the latter is just
        # every POSITION accessor index happening to be unique.
        blocks = {}                                              # POSITION accessor index -> …
        block_order = []
        for prim in prims:
            attrs = prim["attributes"]
            pos_index = attrs["POSITION"]
            pairing = (attrs.get("NORMAL"), attrs.get("TEXCOORD_0"))
            if pos_index in blocks:
                if blocks[pos_index][3] != pairing:
                    return None                                  # same POSITION, different NORMAL/UV
                continue
            verts = read_accessor(pos_index, "VEC3", 5126, numpy.float32, 3)
            normals = read_accessor(attrs["NORMAL"], "VEC3", 5126, numpy.float32, 3)
            if verts is None or normals is None or len(verts) != len(normals):
                return None
            uvs = None
            if "TEXCOORD_0" in attrs:
                uvs = read_accessor(attrs["TEXCOORD_0"], "VEC2", 5126, numpy.float32, 2)
                if uvs is None or len(uvs) != len(verts):
                    return None
            blocks[pos_index] = (verts, normals, uvs, pairing)
            block_order.append(pos_index)

        has_uv = blocks[block_order[0]][2] is not None
        if any((blocks[i][2] is not None) != has_uv for i in block_order):
            return None                                          # mixed UV presence in one mesh

        offsets, cursor = {}, 0
        for pos_index in block_order:
            offsets[pos_index] = cursor
            cursor += len(blocks[pos_index][0])

        tri_parts, runs = [], []
        for prim in prims:
            indices = read_indices(prim["indices"])
            if indices is None or len(indices) % 3 != 0:
                return None
            material_index = prim["material"]
            if material_index >= len(materials):
                return None
            material_name = materials[material_index].get("name")
            if not material_name:
                return None

            offset = offsets[prim["attributes"]["POSITION"]]
            tri_parts.append(indices.astype(numpy.int64).reshape(-1, 3) + offset)
            runs.append((material_name, len(indices) // 3))

        result.append({
            "name": name,
            "matrix": world,
            "verts": numpy.concatenate([blocks[i][0] for i in block_order]),
            "normals": numpy.concatenate([blocks[i][1] for i in block_order]),
            "uvs": numpy.concatenate([blocks[i][2] for i in block_order]) if has_uv else None,
            "tris": numpy.concatenate(tri_parts),
            "material_runs": runs,
        })

    return result


def _fast_import_glb(path):
    """Build `import_glb`'s result without the stock importer, when provably safe.

    Only files whose every material name is already registered qualify (`_KNOWN_MATERIALS`,
    populated from earlier stock imports in this run) — this path reuses materials and never
    constructs one, which is what makes it unable to regress rendering. Returns `None` to mean
    "use the stock importer", the same as every guard inside `_parse_glb_arrays`.
    """
    # The stock glTF importer scales every imported position/translation by
    # `1 / scene.unit_settings.scale_length`; this path applies glTF coordinates directly with
    # no such factor. Always 1.0 under `blender -b` with the default scene (unreachable today),
    # but a non-default scale would silently mix scaled stock-imported geometry with unscaled
    # fast-path geometry in the same city. Refuse rather than get it quietly wrong.
    if bpy.context.scene.unit_settings.scale_length != 1.0:
        return None
    nodes = _parse_glb_arrays(path)
    if not nodes:
        # `None` means a guard bailed; `[]` means the parse completed but found no
        # mesh-bearing node — a shape this path has never validated against the stock
        # importer (untested edge case, not one of the guarded-and-proven ones above).
        # Treat both the same: defer to the stock importer rather than have this path
        # confidently claim "zero objects is correct" for a file it cannot vouch for. This
        # also keeps `import_glb` — which treats *any* non-`None` return as "handled" — from
        # ever seeing a same-shaped `[]` for a reason other than "the stock importer tried
        # and failed", which is the only `[]` `load_prefab_collection` should read as missing.
        return None
    for node in nodes:
        for material_name, _count in node["material_runs"]:
            if material_name not in _KNOWN_MATERIALS:
                return None

    axis_flip = numpy.array([1.0, -1.0, 1.0])
    # Tracks both bare meshes (built, not yet wrapped in an object) and finished objects, in
    # creation order, so the except handler below can tear down whichever partial state this
    # attempt reached — including a mesh that built successfully but failed before
    # `bpy.data.objects.new` ever ran (e.g. `normals_split_custom_set_from_vertices` raising
    # on a shape this parser did not anticipate). A mesh left dangling in `bpy.data.meshes`
    # would leak and, worse, hold `node["name"]`, forcing the stock-importer retry that
    # follows a fast-path failure into a `.001` name — object naming is a frozen contract.
    created = []
    try:
        for node in nodes:
            # glTF -> Blender axis convention, exactly G: (x, y, z) -> (x, -z, y).
            verts = node["verts"][:, (0, 2, 1)] * axis_flip
            normals = node["normals"][:, (0, 2, 1)] * axis_flip
            uv_layers = None
            if node["uvs"] is not None:
                loop_uv = node["uvs"][node["tris"].ravel()]
                loop_uv = numpy.stack([loop_uv[:, 0], 1.0 - loop_uv[:, 1]], axis=1)  # glTF v-flip
                uv_layers = {"UVMap": loop_uv}
            material_runs = [(_KNOWN_MATERIALS[name], count)
                             for name, count in node["material_runs"]]

            # `smooth=True`: the stock importer's shading comes from the custom split normals
            # it sets on the mesh, not from the per-polygon flat/smooth flag — matched below
            # by setting those same custom normals, so `smooth` only has to be "not flat".
            mesh = _mesh_from_arrays(node["name"], verts, node["tris"], uv_layers=uv_layers,
                                     material_runs=material_runs, smooth=True,
                                     degenerate_report=_record_fast_degenerate,
                                     source_geometry=True)
            created.append(mesh)
            mesh.normals_split_custom_set_from_vertices(
                numpy.ascontiguousarray(normals, dtype=numpy.float32))

            obj = bpy.data.objects.new(node["name"], mesh)
            obj.matrix_world = _gltf_node_matrix_to_blender(node["matrix"])
            created[-1] = obj
    except Exception:                                            # noqa: BLE001 - fall back, quietly
        # A guard above missed something about this file's shape. Undo whatever partial mesh
        # data this attempt already allocated and let the stock importer have it instead of
        # returning half-built geometry or propagating a crash out of a per-file fast path.
        for item in created:
            if isinstance(item, bpy.types.Object):
                mesh = item.data
                bpy.data.objects.remove(item)
                if mesh is not None:
                    bpy.data.meshes.remove(mesh)
            else:
                bpy.data.meshes.remove(item)
        return None

    global _FAST_GLB_COUNT
    _FAST_GLB_COUNT += 1
    return created


def _register_known_materials(imported):
    """After a stock import, canonicalise its materials by base name and remember them.

    Blender suffixes a duplicate material name (`.001`, `.002`, …) on every import that
    re-declares one glTF already built earlier in this run — the same material, a fresh
    datablock. The first copy of each base name becomes canonical; every later slot pointing
    at a `.NNN` twin is repointed at it. That is what lets `_fast_import_glb` treat "the
    material is already registered" as a stable fact — the pool of canonical materials only
    grows, never forks — and it is also what keeps `build_networks`' own dedup pass (it runs
    later, over the whole scene) a near no-op: most of the duplication it used to clean up
    never gets created in the first place.
    """
    for obj in imported:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            suffix = material.name.rsplit(".", 1)[-1]
            base = material.name.rsplit(".", 1)[0] if suffix.isdigit() else material.name
            canonical = _KNOWN_MATERIALS.setdefault(base, material)
            if canonical is not material:
                slot.material = canonical


def _scratch_layer_collection():
    """A collection the glTF importer drops its objects into, so they can be found in O(1).

    The importer links what it creates into the view layer's *active* collection. Pointing that at
    a small scratch collection does two things. It turns "what did this import produce" into
    reading one short list, where the obvious `set(bpy.data.objects)` before/after diff is two
    passes over the whole scene — quadratic across a city, though measurement put it well below
    the costs that actually dominate. More importantly it is what lets the destination collection
    be parked outside the view layer entirely (see `parked_out_of_view_layer`): the importer has
    to link its output *somewhere* in the view layer, and if that somewhere is the scene's master
    collection then the scene grows and the parking buys nothing.
    """
    scene_collection = bpy.context.scene.collection
    scratch = bpy.data.collections.get(_SCRATCH_NAME)
    if scratch is None:
        scratch = bpy.data.collections.new(_SCRATCH_NAME)
    if scratch.name not in scene_collection.children:
        scene_collection.children.link(scratch)
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.collection is scratch:
            return layer
    return None


class _quiet_gltf_import:                                        # noqa: N801 - used as a context
    """Silence io_scene_gltf2's per-.glb INFO banner, for exactly the span of one operator call.

    `logging.getLogger("glTFImporter").setLevel(logging.WARNING)` looks like the fix and is
    not one: ImportGLTF2.import_gltf2 (io_scene_gltf2/__init__.py) runs
    `self.loglevel = set_debug_log()` on *every* call, and `set_debug_log()` maps
    `bpy.app.debug_value == 0` to `logging.INFO` — so the addon's own `Log` object
    (io/com/debug.py) resets the logger's level right back to INFO before the next import's
    first line, silently undoing whatever this script set beforehand. Confirmed by import —
    the getLogger/setLevel call left the "Data are loaded" / "glTF import finished" banner
    printing on the very next import.

    `set_debug_log()` reads `bpy.app.debug_value` itself, and returns `logging.WARNING` at
    debug_value 1 — a knob the addon re-reads every call rather than one this script has to
    fight after the fact. Only import-time INFO chatter is affected: `glTFImporter_errors`
    is always pinned to ERROR regardless, so a real warning or error from a malformed glb
    still surfaces.

    `bpy.app.debug_value` is Blender's, not this addon's — general-purpose, and read by
    whatever else happens to check it. Pinning it for the whole session would leave it at 1
    through terrain building, tinting and the final save, for no reason those steps need.
    Scoped to just the `bpy.ops.import_scene.gltf(...)` call instead: saved on entry,
    restored on exit (success, failure, or exception alike), so every other stage of the
    import sees whatever value it would have without this script ever having run.
    """

    def __enter__(self):
        self._previous = bpy.app.debug_value
        bpy.app.debug_value = 1
        return self

    def __exit__(self, *exc):
        bpy.app.debug_value = self._previous
        return False


def import_glb(path):
    """Import one .glb and return the objects it created, unlinked from every collection.

    The caller links them wherever they belong. Returns [] if the import failed, having already
    said why — one unreadable file should not end a city-sized import.

    Tries the direct-parse fast path first (see the block above `_scratch_layer_collection`):
    a file every one of whose materials this run has already built via a stock import can be
    built directly, skipping the ~200 ms/file stock-importer overhead that otherwise dominates
    a city-sized import. Any file the fast path is not provably safe for — first sight of a
    material, or any guard in `_parse_glb_arrays` — falls through to the stock importer
    unchanged, which is also where new materials get built and registered for next time.
    """
    fast = _fast_import_glb(path)
    if fast is not None:
        return fast

    layer = _scratch_layer_collection()
    if layer is None:
        return []

    view_layer = bpy.context.view_layer
    previous = view_layer.active_layer_collection
    view_layer.active_layer_collection = layer
    try:
        with _quiet_gltf_import():
            bpy.ops.import_scene.gltf(filepath=path)
    except Exception as exc:                                    # noqa: BLE001 - report and go on
        LOG.warn(f"! {os.path.basename(path)}: import failed — {exc}")
        return []
    finally:
        view_layer.active_layer_collection = previous

    imported = list(layer.collection.objects)
    for obj in imported:
        layer.collection.objects.unlink(obj)
    _register_known_materials(imported)
    global _STOCK_GLB_COUNT
    _STOCK_GLB_COUNT += 1
    return imported


def load_prefab_collection(asset_id, mesh_dir, sources):
    """Import <asset_id>.glb once and return a Collection holding it, or None.

    Each prefab becomes a collection so that instances can be collection-instanced: one object
    per instance in the scene, one copy of the geometry in memory, and multi-part prefabs keep
    their node hierarchy and per-part offsets for free.
    """
    path = os.path.join(mesh_dir, asset_id + ".glb")
    if not os.path.isfile(path):
        return None

    imported = import_glb(path)
    if not imported:
        return None

    collection = bpy.data.collections.new(f"prefab_{asset_id}")
    sources.children.link(collection)
    for obj in imported:
        collection.objects.link(obj)

    return collection


def aim_viewports(collection):
    """Point every 3D viewport at what was just placed, and move the far clip plane past it.

    A city is not near Blender's origin and is much bigger than Blender expects. An instance
    at bundle X 1375 Z -1625 lands at Blender (1375, -1625, h), some 2.1 km out, while the
    viewport's far clip defaults to 1000 m — so the import is complete, correct, and entirely
    invisible. Frame All does not rescue it either: framing pushes the camera further back,
    which clips the content harder. Both have to change together, so both are set here rather
    than left as two manual steps to rediscover on every run.

    No-ops under `-b`, where there are no screens to aim.
    """
    objects = list(collection.objects)
    if not objects:
        return

    # Instances are empties, so their origin *is* their position. A network ribbon is one big
    # mesh whose vertices are already world-space, so its origin sits at (0,0,0) and aiming at
    # that would point the camera at the map corner. Use the bounding box where there is one.
    points = []
    for obj in objects:
        if obj.type == "MESH" and len(obj.data.vertices):
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        else:
            points.append(obj.matrix_world.translation)
    if not points:
        return
    centre = Vector((sum(p.x for p in points) / len(points),
                     sum(p.y for p in points) / len(points),
                     sum(p.z for p in points) / len(points)))
    span = max((p - centre).length for p in points)

    aimed = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                # Far enough for a whole city; near plane lifted off 0.1 m to keep some depth
                # precision at that range, but still well under building scale.
                space.clip_start = 0.5
                space.clip_end = 100000.0
                region = space.region_3d
                if region is not None:
                    region.view_location = centre
                    region.view_distance = max(span * 2.5, 100.0)
                aimed += 1

    if aimed:
        LOG.step(f"aimed {aimed} viewport(s) at X {centre.x:.0f} Y {centre.y:.0f} Z {centre.z:.0f}, "
                 f"clip end 100000")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print(__doc__)
        return 1

    bundle_dir = argv[0]
    kinds = _UNSET
    limit = _UNSET
    center = None
    radius = None
    keep = False
    save_path = None
    do_networks = False
    do_lanes = False
    do_skirts = False
    do_terrain = False
    do_areas = False
    net_categories = _UNSET
    net_limit = None
    assets = None
    everything = None

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--kinds":
            kinds = {k.strip() for k in argv[i + 1].split(",") if k.strip()}
            if kinds == {"none"}:
                kinds = set()
            i += 2
        elif arg == "--networks":
            do_networks = True
            i += 1
        elif arg == "--lanes":
            do_lanes = True
            i += 1
        elif arg == "--skirts":
            do_skirts = True
            i += 1
        elif arg == "--terrain":
            do_terrain = True
            i += 1
        elif arg == "--areas":
            do_areas = True
            i += 1
        elif arg == "--net-limit":
            do_networks = True
            net_limit = int(argv[i + 1])
            i += 2
        elif arg == "--net-categories":
            do_networks = True
            value = argv[i + 1].strip()
            net_categories = None if value == "all" else {c.strip() for c in value.split(",") if c.strip()}
            i += 2
        elif arg == "--limit":
            limit = int(argv[i + 1])
            i += 2
        elif arg == "--assets":
            assets = [a.strip().lower() for a in argv[i + 1].split(",") if a.strip()]
            i += 2
        elif arg == "--everything":
            everything = float(argv[i + 1])
            i += 2
        elif arg == "--center":
            if argv[i + 1] in ("auto", "downtown", "interchange"):
                center = argv[i + 1]
                i += 2
            else:
                center = (float(argv[i + 1]), float(argv[i + 2]))
                i += 3
        elif arg == "--radius":
            radius = float(argv[i + 1])
            i += 2
        elif arg == "--keep-existing":
            keep = True
            i += 1
        elif arg == "--save":
            save_path = os.path.abspath(argv[i + 1])
            i += 2
        else:
            LOG.warn(f"unknown option {arg!r}")
            return 2

    # `--everything R` is a base, not an override: anything named explicitly still wins, so
    # `--everything 400 --kinds building` is a legal way to say "that region, buildings only".
    if everything is not None:
        do_networks = True
        do_lanes = True
        do_skirts = True
        do_terrain = True
        do_areas = True
        if kinds is _UNSET:
            kinds = set(_ALL_KINDS)
        if net_categories is _UNSET:
            net_categories = None                               # every category
        if limit is _UNSET:
            limit = None                                        # the radius is the limit
        if center is None:
            center = "downtown"
        if radius is None:
            radius = everything

    if kinds is _UNSET:
        kinds = {"building"}
    if net_categories is _UNSET:
        net_categories = set(_DRIVABLE)
    if limit is _UNSET:
        limit = 20000

    (mesh_dir, instances_path, networks_path, net_mesh_dir,
     lanes_path, lane_mesh_dir, lane_prefabs_path) = resolve_layers(bundle_dir)
    if kinds and not os.path.isfile(instances_path):
        LOG.warn(f"no instances.jsonl at {instances_path}")
        return 2

    declared_gaps = read_declared_gaps(bundle_dir)
    LOG.step(f"bundle:    {bundle_dir}")
    LOG.step(f"meshes:    {mesh_dir}")
    LOG.step(f"kinds:     {sorted(kinds) or 'none'}   limit: {'none' if limit is None else limit}")
    if assets is not None:
        LOG.step(f"assets:    matching {assets}")
    if do_networks:
        LOG.step(f"networks:  {networks_path}")
        LOG.step(f"           categories: {'all' if net_categories is None else sorted(net_categories)}")

    LOG.phase("Instances")
    _glb_fast_path_counts_reset()

    # Always survey before placing. Knowing where the selection is turns "nothing appeared" from
    # a mystery into an arithmetic error the user can see.
    where = survey(instances_path, kinds) if kinds else None
    if kinds and where is None:
        LOG.warn(f"no instances of kinds {sorted(kinds)} in this bundle")
        return 2

    if where is not None:
        min_x, max_x, min_z, max_z = where["bounds"]
        LOG.step(f"selected:  {where['count']} instance(s)")
        LOG.step(f"           X {min_x:.0f} .. {max_x:.0f}   Z {min_z:.0f} .. {max_z:.0f}")
        LOG.step(f"           centroid X {where['centroid'][0]:.0f} Z {where['centroid'][1]:.0f}"
                 f"   densest 250 m cell X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f}"
                 f" ({where['densest_count']} there)")

        if center == "auto":
            center = where["densest"]
            LOG.step(f"           --center auto resolved to X {center[0]:.0f} Z {center[1]:.0f}")
    elif center == "auto":
        LOG.warn("--center auto needs instances to survey; drop it or add --kinds")
        return 2

    # Downtown is decided by buildings whatever is being placed. Centring on the densest cell of
    # a selection that includes trees finds a forest, and of one that includes props finds a car
    # park; and with `--kinds none` there is nothing to survey at all, though a networks-only run
    # still wants to look at the middle of the city.
    if center == "downtown":
        skyline = survey(instances_path, {"building"}, weight=skyline_weight)
        if skyline is None:
            LOG.warn("--center downtown needs buildings in the bundle; "
                     "use --center auto, or give explicit X Z")
            return 2
        center = skyline["densest"]
        LOG.step(f"           --center downtown resolved to X {center[0]:.0f} Z {center[1]:.0f}"
                 f"   (tallest 250 m cell of {skyline['count']} building(s))")

    if center == "interchange":
        found = survey_interchange(instances_path, networks_path)
        if found is None:
            LOG.warn("--center interchange needs highway or elevated network records with buildings "
                     "near them; use --center downtown, or give explicit X Z")
            return 2
        center = found
        LOG.step(f"           --center interchange resolved to X {center[0]:.0f} Z {center[1]:.0f}"
                 f"   (tallest skyline with a highway junction in it)")

    if radius is not None:
        if center is None:
            LOG.warn("--radius needs --center (or --center auto)")
            return 2
        LOG.step(f"region:    within {radius} m of X {center[0]:.0f} Z {center[1]:.0f}")

    LOG.step(f"declared gaps in warnings.jsonl: {len(declared_gaps)}")
    report_materials(bundle_dir)

    if not keep:
        clear_scene()

    # Sources live in their own collection, excluded from the view layer, so the templates do
    # not render on top of the instances placed from them.
    sources = bpy.data.collections.new("bundle_sources")
    bpy.context.scene.collection.children.link(sources)
    placed_collection = bpy.data.collections.new("bundle_instances")
    bpy.context.scene.collection.children.link(placed_collection)
    decal_collection = bpy.data.collections.new("bundle_decals")
    bpy.context.scene.collection.children.link(decal_collection)

    # Park both out of the view layer for the placement pass, for the reason given on
    # `parked_out_of_view_layer`: every prefab import deselects the whole view layer, and the
    # instances placed so far are in it. They are put back before anything has to look at them.
    decal_park = parked_out_of_view_layer(decal_collection)
    decal_park.__enter__()
    instance_park = parked_out_of_view_layer(placed_collection)
    sources_park = parked_out_of_view_layer(sources)
    instance_park.__enter__()
    sources_park.__enter__()

    prefabs = {}
    skirt_prefabs = {}
    placed = 0
    skirts_placed = 0
    skipped_kind = 0
    skipped_region = 0
    skipped_asset = 0
    missing = {}
    started = time.time()

    for record in (read_jsonl(instances_path) if kinds else ()):
        if limit is not None and placed >= limit:
            break

        if record.get("kind") not in kinds:
            skipped_kind += 1
            continue

        # Substring rather than exact match: a family like the elevated-road supports spans
        # Vertical_Pillar, Pillar_Top_16, Horizontal_Pillar_12 …, and `--assets pillar` should
        # bring the whole family without having to enumerate it.
        if assets is not None:
            lowered = record["assetId"].lower()
            if not any(a in lowered for a in assets):
                skipped_asset += 1
                continue

        pos = record["pos"]
        if radius is not None and center is not None:
            if math.hypot(pos[0] - center[0], pos[2] - center[1]) > radius:
                skipped_region += 1
                continue

        asset_id = record["assetId"]
        if asset_id not in prefabs:
            prefabs[asset_id] = load_prefab_collection(asset_id, mesh_dir, sources)
            if prefabs[asset_id] is None:
                reason = declared_gaps.get(asset_id, "NOT DECLARED in warnings.jsonl")
                missing[asset_id] = reason

        collection = prefabs[asset_id]
        if collection is None:
            continue

        empty = bpy.data.objects.new(f"{asset_id}.{record.get('id', placed)}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = collection
        empty.empty_display_size = 0.01
        empty.matrix_world = instance_matrix(record)
        set_paint(empty, record.get("attrs"))

        # Decals go in their own collection. The producer ships a decal as the flat quad it
        # paints rather than as the projector volume it paints through, so these now lie on the
        # carriageway and read as the road markings they are — but they are still paint, not
        # surface, and must never become collision geometry in anything built from this.
        # Separating them keeps that one command away: hide the collection and the markings go,
        # and nothing else moves.
        # （貼花置於獨立集合。產出端輸出的是貼花所繪出的平面四邊形，而非其藉以投影的體積，
        #   故它們現已貼合於車道之上，呈現為其所代表的路面標線——但它們仍是塗料而非路面本身，
        #   在任何據此建構的成果中都絕不可成為碰撞幾何。分開放置使此事僅需一道指令。）
        # Foundation skirts, contract §4.1: <assetId>__base.glb placed at the same transform,
        # for instances resting on the ground. The band is shown as authored (a 1 m plinth);
        # dropping its v=0 ring to the terrain raster is a consumer's job, and this harness has
        # no terrain — so slopes will still show a gap below the band, and that is expected.
        if do_skirts and record.get("kind") == "building":
            flags = record.get("attrs", {}).get("elevationFlags")
            if not flags or flags == ["OnGround"]:
                base_id = asset_id + "__base"
                if base_id not in skirt_prefabs:
                    base_path = os.path.join(mesh_dir, base_id + ".glb")
                    skirt_prefabs[base_id] = (load_prefab_collection(base_id, mesh_dir, sources)
                                              if os.path.isfile(base_path) else None)
                if skirt_prefabs[base_id] is not None:
                    skirt = bpy.data.objects.new(f"{base_id}.{record.get('id', placed)}", None)
                    skirt.instance_type = "COLLECTION"
                    skirt.instance_collection = skirt_prefabs[base_id]
                    skirt.empty_display_size = 0.01
                    skirt.matrix_world = instance_matrix(record)
                    set_paint(skirt, record.get("attrs"))
                    placed_collection.objects.link(skirt)
                    skirts_placed += 1

        if record.get("kind") == "decal":
            # Coplanar paint z-fights the carriageway it sits on, which in the viewport looks
            # like the marking is flickering in and out of the road. A lift far below the
            # tolerance of anything downstream costs nothing and settles it.
            # （與其所在車道共面的塗料會產生深度衝突，於視埠中看似標線在路面上忽隱忽現。
            #   遠低於下游任何容差的微小抬升毫無代價，即可解決此問題。）
            empty.matrix_world = Matrix.Translation((0.0, 0.0, _DECAL_LIFT)) @ empty.matrix_world
            decal_collection.objects.link(empty)
        else:
            placed_collection.objects.link(empty)
        placed += 1

        LOG.progress("placed", placed, limit)

    # Not a gate-marked line (no "instance(s)"/"prefab(s)"): the closing summary the count
    # gate actually checks is the "placed N instance(s) …" line at the very end of main,
    # which already existed before _Log and must stay exactly where it was, after every
    # other phase's own summary — see the comment above it.
    LOG.done(f"{placed} placed, {len(prefabs)} mesh source(s) loaded")
    _glb_fast_path_counts_log()

    networks = 0
    network_collection = None
    if do_networks:
        LOG.phase("Networks")
        network_collection = bpy.data.collections.new("bundle_networks")
        bpy.context.scene.collection.children.link(network_collection)
        with parked_out_of_view_layer(network_collection):
            networks = build_networks(networks_path, net_categories, network_collection,
                                      net_mesh_dir, center=center, radius=radius,
                                      limit=net_limit)
        LOG.end_phase()  # build_networks() may have early-returned before its own done()

    lanes = 0
    if do_lanes:
        # Contract §5B v2: lanemeshes.jsonl carries bend parameters, laneprefabs.json the part
        # catalogue, lanemeshes/ the straight part meshes stored once. The bend runs here —
        # this importer is the contract's reference implementation of it.
        if os.path.isfile(lanes_path):
            lane_collection = bpy.data.collections.new("bundle_lanemeshes")
            bpy.context.scene.collection.children.link(lane_collection)
            LOG.phase("Lane geometry (lanemeshes.jsonl)")
            road_surface = _RoadSurface(network_collection) if networks else None
            with parked_out_of_view_layer(lane_collection):
                lanes = build_lanes(lanes_path, lane_prefabs_path, lane_mesh_dir,
                                    lane_collection, center=center, radius=radius,
                                    limit=net_limit, road_surface=road_surface)
            LOG.end_phase()  # build_lanes() may have early-returned before its own done()
        else:
            LOG.warn(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")

    terrain_objects = 0
    if do_terrain:
        LOG.phase("Terrain & water")
        terrain_collection = bpy.data.collections.new("bundle_terrain")
        bpy.context.scene.collection.children.link(terrain_collection)
        terrain_objects = build_terrain(bundle_dir, terrain_collection, center, radius)
        LOG.end_phase()  # build_terrain() may have early-returned before its own done()

    area_objects = 0
    if do_areas:
        LOG.phase("Surface areas")
        area_collection = bpy.data.collections.new("bundle_areas")
        bpy.context.scene.collection.children.link(area_collection)
        area_objects = build_areas(bundle_dir, area_collection, center, radius)
        LOG.end_phase()  # build_areas() may have early-returned before its own done()

    # Everything is imported, so the placed instances can come back into the view layer. This is
    # deliberately after the networks: an instance parked here is an instance the segment imports
    # do not have to deselect, and there are two orders of magnitude more of them than there are
    # segments. Un-parking before the network pass put 40,500 empties back in front of every
    # segment import and took each one from 0.06 s to 1.5 s.
    decal_park.__exit__()
    instance_park.__exit__()
    sources_park.__exit__()

    # Every .glb is in, so every material exists: apply the per-instance paint rule (§7.2).
    tinted = apply_color_tints(bundle_dir)
    if tinted:
        LOG.step(f"tinted:    {tinted} material(s) wired to per-instance colorSet via their colorMask")

    # The scratch collection is empty by construction — every import unlinks what it produced —
    # so drop it rather than leave an empty entry in the outliner.
    scratch = bpy.data.collections.get(_SCRATCH_NAME)
    if scratch is not None:
        if scratch.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(scratch)
        bpy.data.collections.remove(scratch)

    # Hide the templates from the view layer without unlinking them; instances still resolve.
    layer_child = bpy.context.view_layer.layer_collection.children.get(sources.name)
    if layer_child is not None:
        layer_child.exclude = True

    show_materials()

    # Aim at whatever there is to look at. With `--kinds none --networks` the instance
    # collection is empty, and aiming at an empty collection leaves the viewport at the origin
    # — which is where a city almost never is.
    aim_viewports(placed_collection if placed else
                  (network_collection if networks else
                   (lane_collection if lanes else placed_collection)))

    elapsed = time.time() - started
    # No phase is open at this point — Instances closed right after its loop, and whichever
    # of Networks/Lanes/Terrain/Areas ran last closed itself too — so LOG.done() adds no
    # elapsed suffix of its own here; `elapsed` above (measured from before the instance
    # loop, across the whole pipeline) is the only timing this line carries, exactly as
    # before _Log.
    LOG.done(f"placed {placed} instance(s) from {len(prefabs)} prefab(s) in {elapsed:.1f} s")
    if do_skirts:
        LOG.step(f"placed {skirts_placed} foundation skirt(s) (__base, as-authored 1 m bands — "
                 f"the terrain drop is the consumer rule, contract §4.1)")
    if do_terrain:
        LOG.step(f"placed {terrain_objects} terrain object(s) (ground grid + water; §8)")
    if do_areas:
        LOG.step(f"placed {area_objects} surface-area object(s) (driveways, lots, paths; §8b)")
    LOG.step(f"skipped: {skipped_kind} by kind, {skipped_asset} by --assets, {skipped_region} outside the region")

    # An empty-looking scene is nearly always the region filter, not the bundle. Say so, with the
    # coordinates that would have worked. The absolute floor matters as much as the ratio: a
    # deliberate 500 m window on a 14 km city keeps well under a twentieth of it by design, and
    # tens of thousands of instances is not an empty scene — pointing that run at `--center auto`
    # would have moved it from downtown to whichever field has the most trees.
    if (radius is not None and where is not None
            and placed < where["count"] / 20 and placed < 500):
        LOG.warn(f"NOTE: the region kept {placed} of {where['count']} selected instance(s). "
                 f"The densest 250 m cell is at X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f} — "
                 f"try --center auto, --center downtown, a larger --radius, or drop them.")

    if missing:
        undeclared = [a for a, r in missing.items() if r == "NOT DECLARED in warnings.jsonl"]
        LOG.warn(f"{len(missing)} assetId(s) had no mesh ({len(undeclared)} of them undeclared):")
        for asset_id, reason in list(missing.items())[:15]:
            LOG.step(f"- {asset_id}: {reason}")
        if undeclared:
            # A gap the producer did not declare is a contract violation, not a missing file:
            # the consumer had no way to know the asset was coming up short.
            LOG.warn(f"CONTRACT VIOLATION: {len(undeclared)} undeclared: {undeclared[:10]}")

    if save_path:
        # relative_remap=False because the textures stay where the rip put them, under
        # ModsData on the system drive, and a .blend saved anywhere else cannot express that
        # as a relative path. Blender's default is to try anyway and warn once per image --
        # "cannot be made relative" for every one of a few thousand textures, which buries the
        # import's own output. The paths are absolute either way; only the noise differs.
        bpy.ops.wm.save_as_mainfile(filepath=save_path, relative_remap=False)
        LOG.step(f"saved {save_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
