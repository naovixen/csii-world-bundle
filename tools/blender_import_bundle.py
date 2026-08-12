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

    blender --python tools/blender_import_bundle.py -- [bundle_dir] [options]

`bundle_dir` may be omitted: it defaults to the producer's output directory,
%USERPROFILE%/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto
(the development-era spike/ subfolder is retired as of 2026-08-09).

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
    --lane-markings              like --lanes but ONLY the decal parts — road markings, wear
                                 tint, crosswalks — skipping the heavy roadside furniture
                                 (fences, hedges, sleepers, cables). The light companion to
                                 --terrain --networks; ignored when --lanes is also given
    --skirts                     also place each building's <assetId>__base.glb foundation
                                 skirt, as authored (the 1 m band; the terrain drop of its
                                 v=0 ring is the consumer rule in contract §4.1)
    --colour                     replay the game's own colour grading from grading.json
                                 (exposure, white balance, contrast, saturation,
                                 shadows/midtones/highlights). Off by default: a plain
                                 import is a faithful 1:1 albedo view, and CS2's palette
                                 is post-processing the textures do not carry.
                                 It writes an OCIO config beside the bundle and prints
                                 how to switch it on; with that active the whole chain
                                 rides a "CS2 Graded" VIEW TRANSFORM, so it shows in the
                                 viewport and in renders alike. Without it, only the
                                 per-channel half can be applied and the log says so
    --terrain                    also place the §8 terrain under everything: the road-flattened
                                 heightmap as a ground grid (roadbed faces textured from
                                 surface_index.png and sunk slightly, steep faces textured
                                 rock, faces inside clipvolumes.jsonl deleted — the terrain the
                                 game stencil-clips over tunnel portals and trenches) and the
                                 water surface where water_level.png rises above the ground
                                 (same region filter as everything else). The ground, not the
                                 junction floors — junction interiors are the arms' own node
                                 meshes (audit §18)
    --clip-floor V               diagnostic: override every clip strip's `bottom` with V
                                 metres (relative to the lane surface) when rasterising
                                 clipvolumes.jsonl — previews a producer band-floor change
                                 against an existing bundle (implies --terrain)
    --areas                      also place the §8b surface areas from areas.jsonl — house
                                 driveways, parking-lot pavement, park paths: the painted
                                 ground of every meshless prefab
    --keep-existing              don't wipe the scene first
    --save out.blend             write a .blend and exit, so the import is paid for once

Examples:
    # the 1:1 building check — start here, it is fast and the most diagnostic
    blender --python tools/blender_import_bundle.py -- --kinds building

    # a dense neighbourhood with its trees and props, wherever the city actually is
    blender --python tools/blender_import_bundle.py -- \\
        --kinds building,tree,prop --center auto --radius 300

    # the Phase 5 check: the road network alone, coloured by form
    blender --python tools/blender_import_bundle.py -- --kinds none --networks

    # everything at once — buildings, trees, props, every network category, all textured —
    # over 500 m of downtown, which is small enough to import repeatedly
    blender --python tools/blender_import_bundle.py -- --everything 500

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
import re
import sys
import time

import bpy
import numpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# The engine-neutral half of the reconstruction lives in `tools/csiibundle/`, a package that
# never imports bpy: the frame conversion, the GLB and PNG readers, the splat decode and the
# grading derivation are all things a BeamNG builder or a Blender adapter needs identically, and
# keeping one copy is what stops two of them drifting apart (the last attempt at the grading had
# two calibrations that "converged" on different transforms). This script is one adapter over it
# — everything below is the part that is genuinely about Blender.
#
# Blender does not put a script's own folder on the path when it is run with `--python`, so add
# it explicitly before importing either.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import colour_model
from csiibundle import areas as area_math
from csiibundle import bezier, decals, drape, frames, lanes, networks, reader, tints
from csiibundle import terrain as terrain_math
from csiibundle import glb as glb_reader, png as png_reader, splat as splat_reader


# -- the frame conversion, contract §2 ------------------------------------------------------
#
# The whole derivation is `csiibundle.frames`; read its module docstring for why the formula is
# what it is. What this file adds is the ONE convention that is Blender's rather than the
# bundle's: the stock glTF importer rewrites Y-up to Z-up on import, so the mesh sitting in the
# scene is already G·v_glb and that has to be undone. `frames.GLTF_YUP_TO_ZUP` is that G, and
# `frames.zup_instance_matrix(record, G)` composes the lot:
#
#     P · T · R · S · diag(-1,1,1) · G⁻¹
#
# The sanity check on it is that P · diag(-1,1,1) · G⁻¹ = diag(-1,-1,1), a 180° turn about Z
# with no reflection — the mesh was already made right-handed by the producer, so reconstructing
# it in a right-handed application must not mirror anything again.

# The mesh-side basis Blender's glTF importer applies. Passed to `frames.zup_instance_matrix`
# for every placement; a consumer reading the .glb buffers directly passes None instead.
_BLENDER_GLTF_BASIS = frames.GLTF_YUP_TO_ZUP

# The ground-paint ladder is `csiibundle.decals.LIFT_LADDER` — one table, because splitting it
# across files is how it got broken. Its docstring records why the ORDER matters and the values
# do not, and why the bend's pre-drape offset is deliberately not a rung of it.
#
#   0.000  terrain / road surface           the thing being painted on
#   0.008  lane_tint                        lane wear tint          ) on the ROAD, draped
#   0.014  lane_marking                     markings, crosswalks    )  onto it per vertex
#   0.020  area                             §8b surface areas — pavement, driveways, paths
#   0.030  instance_decal                   §4.2a decal instances — bay markings, arrows
#
# Lane decal parts (§5B `decal`) cannot use a fixed lift at all: the game stores lane curves at
# the base plane of the carriageway, and the rendered surface crowns ABOVE them — measured on a
# full-city rip, the road top near centre lines is a median 7 mm above the lane path and beats
# path + the bend lift on 42% of samples, which is why bent markings vanish into the asphalt.
# The game never sees the problem because a decal is a projector volume, painted onto whatever
# surface it meets. The harness equivalent is a drape: sample the road surface under the marking
# and snap onto it (`_drape_block`).
_DECAL_LIFT = decals.BEND_LIFT
_INSTANCE_DECAL_LIFT = decals.LIFT["instance_decal"]
_DECAL_DRAPE_MARKING = decals.LIFT["lane_marking"]
_DECAL_DRAPE_TINT = decals.LIFT["lane_tint"]

_DRAPE_MISS_WARN = drape.MISS_WARN_FRACTION

_ROADBED_SINK = terrain_math.ROADBED_SINK

# The scene light rig. `clear_scene()` removes Blender's default light along with everything
# else, so an imported scene would have no lights and no world at all; these put a plain
# neutral sun and ambient back. White, and deliberately uncalibrated -- the bundle ships
# albedo, and the importer reproduces it 1:1 rather than grading it toward the game's look.
#
# Set by `--colour` to the `tools/colour_model.Grading` derived from this bundle's grading.json.
# NOTHING is applied to materials from it: the whole chain rides Blender's display pipeline,
# where it runs on final radiance in the game's own order and shows in the VIEWPORT. Putting any
# of it on the albedo instead reorders the chain — measured on this city's grass, moving the
# saturation onto the material alone costs 18% on the blue channel, which is the same size as the
# effect this work is trying to measure.
#
# Two constants that used to live here have been DELETED rather than tuned, because the model
# they were compensating for was wrong:
#
#   _GROUND_BRIGHTNESS = 3.09 lifted the terrain alone, on the argument that CS2 authors ground
#   albedo 1.63 stops darker than its object textures. The relative measurement is real, but the
#   game renders that dark ground too — the shipped sand albedo reaches the in-game eyedropper
#   colour under the game's own grading at ordinary scene exposure, with no ground-only boost. A
#   gain on the terrain alone therefore does not fix an imbalance, it creates one, and it is the
#   reason everything that was NOT boosted read as too dark once exposure was pulled back.
#
#   _VIEW_GAMMA = 1.3 was a hand-set tone-curve compensation "because Blender has no ACES view
#   transform". Blender 5.2 ships ACES 1.3 and ACES 2.0 as view transforms; the premise was
#   false. The game's contrast is now applied from its own number instead (grading.json's
#   contrast +20, solved through the ACEScc pivot into an exact power law).
_GRADING = None

# The ambient share of the rig: how much of a fully-lit surface's light comes from the sky rather
# than the sun. A uniform environment of radiance W gives a Lambertian surface outgoing radiance
# albedo*W, so this is directly "a quarter of the light is skylight".
_WORLD_LEVEL = 0.25

# The sun's strength is DERIVED, not chosen, from one requirement: flat, unshadowed, fully-lit
# ground should render at exactly its albedo. For a Lambertian surface under a sun of irradiance
# E at incidence cos(theta) plus a uniform sky of radiance W,
#     outgoing radiance = albedo * (E*cos(theta)/pi + W)
# so radiance == albedo when  E = pi * (1 - W) / cos(theta).
#
# Why bother: it makes "scene-linear" and "albedo" the same number on the ground, which is the
# assumption `tools/colour_model.py` works under. With an arbitrary sun the model's predictions
# and the render can only be compared after re-exposing one to the other, and under `--colour`
# the picture came out near-black — the game's own EV figures are relative to ITS light level,
# not to a rig picked by eye. `_sun_energy_for` computes it from the sun's actual orientation.
_SUN_TILT = (0.5, 0.3, 0.0)

_ROCK_SLOPE = terrain_math.ROCK_SLOPE


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
                      smooth=True, degenerate_report=None, source_geometry=False,
                      poly_sizes=None):
    """Build a mesh from numpy arrays via foreach_set — the hot-path alternative to
    per-face `from_pydata` / per-loop attribute assignment.

    `verts` is `(N, 3)` float64 already in Blender frame (X east, Y north, Z up). `tris` is
    `(M, poly_size)` int, one row per polygon in loop order. `poly_size` defaults to 3
    (triangles); a caller that already has same-diagonal quads — the terrain grid — passes 4,
    so its faces tessellate exactly as `from_pydata` quads always did instead of picking a
    fresh triangulation diagonal that could flip a slope's silhouette under the pixel A/B
    gate. `uv_layers` is `{layer_name: (M*poly_size, 2)}` in the same loop order.

    `poly_sizes`, when given, replaces the uniform `poly_size`: `tris` is then a FLAT 1-D
    array of loop vertex indices and `poly_sizes` is `(M,)` per-polygon loop counts (their sum
    must equal `len(tris)`). This is how the terrain grid mixes 14 m quads, native 3.5 m
    quads and transition-fan triangles in one mesh. All degenerate-face semantics below apply
    unchanged; UV layers are per-loop in the same flat order.
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

    if poly_sizes is None:
        tris = numpy.asarray(tris, dtype=numpy.int64)
        if tris.size == 0:
            tris = tris.reshape(0, poly_size)
        if tris.ndim != 2 or tris.shape[1] != poly_size:
            raise ValueError(f"_mesh_from_arrays({name!r}): tris must be (M, {poly_size}), "
                             f"got shape {tris.shape}")
        loops_flat = tris.ravel()
        sizes = numpy.full(len(tris), poly_size, dtype=numpy.int64)
    else:
        loops_flat = numpy.asarray(tris, dtype=numpy.int64).ravel()
        sizes = numpy.asarray(poly_sizes, dtype=numpy.int64)
        if int(sizes.sum()) != len(loops_flat):
            raise ValueError(f"_mesh_from_arrays({name!r}): poly_sizes sum "
                             f"{int(sizes.sum())} != {len(loops_flat)} flat loop indices")

    # The drop itself is `csiibundle.glb.clean_degenerate_polygons` — engine-neutral, and it
    # trims `material_runs` and the UV layers in the same pass so the survivors keep what they
    # were given. What stays here is the REPORTING, because what a degenerate face means
    # depends on where the arrays came from and only this caller knows that.
    (loops_flat, sizes, uv_layers, material_runs,
     repeated_count, zero_area_count) = glb_reader.clean_degenerate_polygons(
        verts, loops_flat, sizes, uv_layers=uv_layers, material_runs=material_runs)
    poly_count = len(sizes)

    if repeated_count:
        # Downgradable ONLY for a source-geometry caller with its own aggregate report — every
        # computed caller (the default) gets the unconditional loud line, by construction: a
        # repeated index in COMPUTED math is historically real bad data, and no caller can opt
        # that class out of the alarm for itself.
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

    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    mesh.loops.add(len(loops_flat))
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(loops_flat, dtype=numpy.int32))
    mesh.polygons.add(poly_count)
    loop_start = numpy.concatenate(([0], numpy.cumsum(sizes)[:-1])).astype(numpy.int32) \
        if poly_count else numpy.zeros(0, dtype=numpy.int32)
    mesh.polygons.foreach_set("loop_start", loop_start)
    mesh.polygons.foreach_set("loop_total", numpy.ascontiguousarray(sizes, dtype=numpy.int32))
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
    return Vector(frames.bundle_to_zup_point(pos))


def instance_matrix(record):
    """The full world matrix for one instance record, ready to assign to matrix_world.

    `csiibundle.frames` composes it; this only converts to mathutils. The float64 the package
    works in is truncated to float32 on assignment either way, so the extra precision on the
    way through is free.
    """
    return Matrix(frames.zup_instance_matrix(record, _BLENDER_GLTF_BASIS).tolist())


# -- bundle reading -------------------------------------------------------------------------
#
# All of it is `csiibundle.reader`. The aliases keep the call sites below reading the way they
# always have; `resolve_layers` and `_terrain_paths` return namedtuples whose field order is
# the tuple order these call sites unpack.

read_jsonl = reader.read_jsonl
read_declared_gaps = reader.read_declared_gaps
resolve_layers = reader.resolve_layers


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
    prefab data and per-object state. `csiibundle.tints` resolves them and explains why
    ignoring this layer leaves every tree in the city grey.
    """
    for prop, channel in zip(_TINT_PROPS, tints.color_set(attrs)):
        obj[prop] = list(channel)


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
        image = _unpremultiply(bpy.data.images.load(mask_path, check_existing=True))
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


# -- networks, contract §5 ------------------------------------------------------------------
#
# A network record carries geometry in exactly one of two forms, and §5 is explicit that a
# consumer must implement both. Only the centerline form is implemented here, because that is
# the only one the ripper emits (§5.1) — a mesh-form record is counted and reported
# rather than silently dropped, so the day a producer starts emitting them this says so.

# Colour by `form`, which is the whole Phase 5 acceptance question: an elevated run and a
# tunnel have to be distinguishable without opening the JSON.
_FORM_COLOURS = {
    "normal": (0.22, 0.22, 0.24, 1.0),
    "elevated": (0.20, 0.45, 0.85, 1.0),
    "tunnel": (0.85, 0.30, 0.20, 1.0),
    "bridge": (0.85, 0.70, 0.20, 1.0),
}

_DRIVABLE = networks.DRIVABLE
_ALL_KINDS = networks.KINDS

_UNSET = object()

_segment_normals = networks.segment_normals


def ribbon(centerline, width, lift, mitre_limit=networks.DEFAULT_MITRE_LIMIT):
    """`csiibundle.networks.ribbon`, converted into Blender's frame.

    The mitre maths and the "a ribbon is a stand-in, not a road" caveat both live in the
    package; this only swaps the axes and hands back the list-of-tuples shape `build_networks`
    already merges.
    """
    built = networks.ribbon(centerline, width, lift, mitre_limit=mitre_limit)
    if built is None:
        return None
    verts, quads = built
    return ([Vector(frames.bundle_to_zup_point(v)) for v in verts],
            [tuple(int(i) for i in quad) for quad in quads])


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
# `csiibundle.lanes` is the reference implementation of the bend, `csiibundle.bezier` the curve
# maths under it, and `csiibundle.decals` everything about markings. All three are bpy-free and
# their docstrings carry the reasoning; what remains in this file is the Blender side — loading
# a part .glb, draping blocks onto the road, and turning the results into objects.
_LANE_ARC_SAMPLES = bezier.ARC_SAMPLES
_LANE_MAX_TILES = lanes.MAX_TILES
_TILE_CHORD_TOL = lanes.TILE_CHORD_TOL
_TILE_WIDTH_TOL = lanes.TILE_WIDTH_TOL
_DECAL_SHAPE_TOLERANCE = decals.SHAPE_TOLERANCE
_DECAL_DIP_FRACTION = decals.DIP_FRACTION
_DECAL_MAX_ROWS = decals.MAX_ROWS

_bezier_point_vec = bezier.evaluate
_bezier_tangent_vec = bezier.tangent
_cut_bezier = bezier.cut
_arc_table_vec = bezier.arc_table
_curve_length = bezier.length
_lane_tile_count = lanes.tile_count
_hang_drop_vec = lanes.hang_drop
_quat_apply_vec = lanes.quat_apply


class _ProjectionSurface(drape.ProjectionSurface):
    """Ray-cast access to an imported surface, for the layers the game paints ONTO one.

    Two layers need this and they need the same thing: lane decals project onto the road
    (`build_lanes`), surface areas project onto the terrain (`build_areas`). Both are
    projector volumes in the game (audit §19, §30) and both are reconstructed here as
    geometry that has to be conformed to whatever it paints.

    One BVH over every network mesh would hold ~10M triangles for a full city, most of it
    never queried. Instead the objects are binned into a 128 m grid by bounding box and each
    object's BVH (world space, Blender frame) is built lazily on first query — a
    neighbourhood import builds only the handful it touches.
    """

    CELL = 128.0
    _DOWN = Vector((0.0, 0.0, -1.0))

    def __init__(self, objects):
        self._objects = []
        self._grid = {}
        self._trees = {}
        self.kerb_rejections = 0
        for obj in objects:
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

        MEASURED AND LEFT ALONE, 2026-08-11. The regression plan proposed re-casting below
        every hit so each object contributes all its surfaces rather than only its topmost,
        on the theory that a kerb was shadowing the carriageway inside one merged segment
        mesh. `tools/test_drape_pick.py` refutes both halves of that on the shipped bundle:

          * There is nothing to enumerate. Segment meshes are single-layer shells — 24,059 of
            24,060 samples have exactly ONE surface beneath them — so the carriageway is never
            hiding under anything. Enumerating every hit and keeping this same nearest rule
            reproduces this rule's numbers to the digit (0.21% of samples > 2 cm above their
            own road, max +0.298 m, at the city's densest 300 m window).
          * Choosing the highest candidate at or below the lane curve instead is WORSE, not
            better: 0.46% > 2 cm, twice this rule's rate. Nearest-to-expected is the better
            calibrated discriminator, and neighbouring objects — not one object's own kerbs —
            are the only place a choice arises at all (2.1% of samples there, 0% in a plain
            road window).

        So the lift the plan set out to fix is not made here. Do not re-litigate this from
        static reading of the code; run the test.
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

    def height(self, x, y, ceiling=4000.0):
        """Topmost hit under (x, y) from far above, or None — the plain "what is the ground
        here" query. `drop`'s nearest-to-expected pick exists to keep a lane marking off a
        kerb it happens to pass; an area has no expected height to be near, only the surface
        it is painted on, so this takes the highest hit in a window tall enough to clear any
        terrain in the map."""
        origin = Vector((x, y, ceiling))
        best = None
        for index in self._grid.get((int(x // self.CELL), int(y // self.CELL)), ()):
            tree = self._tree(index)
            if tree is None:
                continue
            location = tree.ray_cast(origin, self._DOWN, 2.0 * ceiling)[0]
            if location is not None and (best is None or location.z > best):
                best = location.z
        return best


_decal_quad_info = decals.quad_info
_decal_lattice = decals.lattice
_quat_to_mat3 = lanes.quat_to_mat3
_rigid_tile_matrix = lanes.rigid_tile_matrix
_bend_lane_part = lanes.bend_part


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


_DRAPE_DEDUP_MM = drape.DEDUP_MM
_drape_block = drape.drape_block


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


def _lane_part_object(asset_id, source, cache, parts_collection, degenerate_report):
    """One shared Blender object per part mesh, at rest in the Blender frame, for instancing.

    Built from the same bundle-frame arrays the baked path bends, through the same axis swap
    (`[:, [0, 2, 1]]`), so a rigid instance of this datablock and a baked tile of the same
    part are the same triangles in the same winding. Lives in an excluded collection: the
    depsgraph still evaluates it for the instancers' Object Info, but it never draws itself.
    """
    if asset_id in cache:
        return cache[asset_id]

    verts = source["verts"][:, [0, 2, 1]]
    tri_mat = source["tri_material"]
    order = numpy.argsort(tri_mat, kind="stable")
    tris = source["tris"][order]
    uvs = source["uvs"][order].reshape(-1, 2)
    material_runs = [(source["materials"][m], int((tri_mat == m).sum()))
                     for m in numpy.unique(tri_mat)]
    mesh = _mesh_from_arrays(f"lanepart_{asset_id[:16]}", verts, tris,
                             uv_layers={"UVMap": uvs}, material_runs=material_runs,
                             poly_size=3, smooth=False, source_geometry=True,
                             degenerate_report=degenerate_report)
    obj = bpy.data.objects.new(f"lanepart_{asset_id[:16]}", mesh)
    parts_collection.objects.link(obj)
    cache[asset_id] = obj
    return obj


def _tile_instance_group(part_obj, cache):
    """One Geometry Nodes group per shared part object: Instance on Points of that part,
    each instance's full 4×4 taken from the point's `tile_matrix` attribute. The part is the
    Object Info node's own target (not a modifier input — object-socket modifier inputs are
    not assignable from Python on every Blender build), so all instancers of one part share
    one group and there are at most as many groups as part meshes."""
    if part_obj.name in cache:
        return cache[part_obj.name]
    group = bpy.data.node_groups.new(f"carto_tiles_{part_obj.name}", "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    n_in = group.nodes.new("NodeGroupInput")
    n_out = group.nodes.new("NodeGroupOutput")
    info = group.nodes.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = part_obj
    info.inputs["As Instance"].default_value = True
    points = group.nodes.new("GeometryNodeInstanceOnPoints")
    attr = group.nodes.new("GeometryNodeInputNamedAttribute")
    attr.data_type = "FLOAT4X4"
    attr.inputs["Name"].default_value = "tile_matrix"
    setm = group.nodes.new("GeometryNodeSetInstanceTransform")

    links = group.links
    links.new(n_in.outputs["Geometry"], points.inputs["Points"])
    links.new(info.outputs["Geometry"], points.inputs["Instance"])
    links.new(points.outputs["Instances"], setm.inputs["Instances"])
    links.new(attr.outputs["Attribute"], setm.inputs["Transform"])
    links.new(setm.outputs["Instances"], n_out.inputs["Geometry"])
    cache[part_obj.name] = group
    return group


def _tile_instancer_object(name, part_obj, matrices, group_cache):
    """A point-cloud mesh (one vertex per tile) carrying each tile's 4×4, instancing the
    shared part object through `_tile_instance_group`. Point positions duplicate the matrix
    translations only so the object has sensible bounds; the transform node replaces the
    instance matrix wholesale.

    `matrices` arrive from `lanes.bend_part` in the BUNDLE frame and are conjugated here — the
    same swap `_lane_part_object` applies to the shared datablock's vertices, which is what
    makes an instanced tile and a baked tile the same triangles in the same place.
    """
    mats = numpy.asarray([frames.to_zup_transform(m) for m in matrices], dtype=numpy.float64)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(mats))
    mesh.vertices.foreach_set(
        "co", numpy.ascontiguousarray(mats[:, :3, 3], dtype=numpy.float32).ravel())
    attr = mesh.attributes.new("tile_matrix", "FLOAT4X4", "POINT")
    for i, mat in enumerate(mats):
        attr.data[i].value = Matrix(mat.tolist())
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    modifier = obj.modifiers.new("tiles", "NODES")
    modifier.node_group = _tile_instance_group(part_obj, group_cache)
    return obj


def build_lanes(lanes_path, catalogue_path, lane_mesh_dir, collection,
                center=None, radius=None, limit=None, road_surface=None,
                markings_only=False, ground_surface=None):
    """Bend every lane record's parts along its curve, merged into one object per prefab.

    `markings_only` bends just the decal parts (road markings, wear tint) and skips the
    roadside furniture — the light import for looking at carriageways without the fence,
    hedge and sleeper geometry that dominates a full `--lanes` build.
    """
    if not os.path.isfile(lanes_path):
        LOG.warn(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")
        return 0
    if not os.path.isfile(catalogue_path):
        LOG.warn(f"CONTRACT VIOLATION: lanemeshes.jsonl without {catalogue_path}")
        return 0

    with open(catalogue_path, encoding="utf-8") as handle:
        catalogue = json.load(handle)

    # §5B: every decal part carries `decalRole`, which is what settles its lift (see
    # `_bend_lane_part`). Checked once here rather than per part, and reported rather than
    # guessed around — a decal with no role would silently take the wear tint's lower lift and
    # bury a marking, the exact class of failure the field was added to end.
    roleless = sorted({part.get("assetId", "?")
                       for entry in catalogue.values() for part in entry.get("parts", ())
                       if part.get("decal") and not part.get("decalRole")})
    if roleless:
        LOG.warn(f"CONTRACT VIOLATION: {len(roleless)} decal part(s) carry no §5B decalRole "
                 f"({', '.join(roleless[:4])}{' …' if len(roleless) > 4 else ''}) — they take "
                 "the wear-tint lift; re-rip the bundle")

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
        sink = builders.setdefault(key, {"blocks": [], "instances": [], "colorSet": color_set})

        for index in record.get("parts", []):
            parts = entry.get("parts", [])
            if not 0 <= index < len(parts):
                continue
            if markings_only and not parts[index].get("decal"):
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
    counters = {"draped": 0, "grounded": 0, "missed": 0, "queries": 0}
    if road_surface is not None:
        for seen, block in enumerate(drape_blocks, 1):
            _drape_block(block, road_surface, counters, ground_surface=ground_surface)
            if seen % 5000 == 0:
                LOG.step(f"{seen}/{len(drape_blocks)} marking(s) draped")
    draped, drape_missed = counters["draped"], counters["missed"]
    drape_grounded = counters["grounded"]
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

    # -- rigid tiles as instances ----------------------------------------------------------
    # One instancer object per (prefab, paint, part): a point cloud of tile matrices,
    # Geometry Nodes instancing one shared part mesh. set_paint lands on the instancer, where
    # the materials' per-instance attribute reads pick it up exactly as they do for the
    # collection-instance empties the prop layer uses.
    part_degenerates = {"faces": 0}

    def _report_part_degenerate(_name, count, _repeated):
        part_degenerates["faces"] += count

    instanced_tiles = 0
    instancers = 0
    part_cache = {}
    group_cache = {}
    parts_collection = None
    for (prefab_key, _), sink in builders.items():
        if not sink["instances"]:
            continue
        by_part = {}
        for asset_id, matrix in sink["instances"]:
            by_part.setdefault(asset_id, []).append(matrix)
        for asset_id, matrices in by_part.items():
            if parts_collection is None:
                parts_collection = bpy.data.collections.new("bundle_laneparts")
                bpy.context.scene.collection.children.link(parts_collection)
            part_obj = _lane_part_object(asset_id, sources[asset_id], part_cache,
                                         parts_collection, _report_part_degenerate)
            instancer = _tile_instancer_object(
                f"lanetiles_{prefab_key}_{asset_id[:12]}", part_obj, matrices, group_cache)
            collection.objects.link(instancer)
            if sink["colorSet"] is not None:
                set_paint(instancer, {"colorSet": sink["colorSet"]})
            instanced_tiles += len(matrices)
            instancers += 1
    if parts_collection is not None:
        # The shared part meshes must never draw on their own — the instancers' Object Info
        # still evaluates them from an excluded collection.
        layer = find_layer_collection(parts_collection)
        if layer is not None:
            layer.exclude = True

    LOG.done(f"lanes:     {bent} record(s) bent into {built} object(s), "
             f"{len(sources)} part mesh(es) loaded")
    if instanced_tiles:
        baked_tiles = sum(sink.get("tiles_baked", 0) for sink in builders.values())
        LOG.step(f"{instanced_tiles} straight tile(s) instanced across {instancers} "
                 f"instancer(s) ({len(part_cache)} shared part mesh(es)); "
                 f"{baked_tiles} tile(s) baked (curved, tapered, hung or clipped)")
    if part_degenerates["faces"]:
        LOG.step(f"{part_degenerates['faces']} degenerate face(s) dropped from shared part "
                 "meshes (authored in the source .glb)")
    dropped_decals = sum(sink.get("decal_dropped", 0) for sink in builders.values())
    if dropped_decals:
        # New line vs pre-§39 baselines (cf. the degenerate-face note below for the A/B gate).
        LOG.step(f"{dropped_decals} decal span(s) dropped (tiling clip 0 — the game draws "
                 "zero instances on a sub-half-period stub)")
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
    if draped or drape_missed or drape_grounded:
        LOG.step(f"{draped} decal vertex(es) draped onto the road surface"
                 + (f", {drape_grounded} onto the terrain instead (no road under them)"
                    if drape_grounded else "")
                 + (f", {drape_missed} kept the fixed lift (no surface hit)" if drape_missed else ""))
        # A miss is a marking floating or sunk at an authored height with nothing beneath it.
        # A few are expected at a region edge, where the marking is inside the window and the
        # road carrying it is not; a large share means the drape is not doing its job.
        total_drape = draped + drape_grounded + drape_missed
        if total_drape and drape_missed > total_drape * _DRAPE_MISS_WARN:
            LOG.warn(f"{drape_missed / total_drape * 100:.1f}% of decal vertices found no "
                     f"surface within their own projector reach"
                     + ("" if ground_surface is not None else
                        " — import --terrain as well and they fall back to the ground"))
    elif road_surface is None:
        LOG.warn("networks layer not built — decals keep the fixed lift, expect "
                 "markings to z-fight or sink into the crown")
    if road_surface is not None and road_surface.kerb_rejections:
        # A low number here is the truth, not blindness: only 2.1% of drape samples have more
        # than one object under them even at the city's densest junction, and 0% in a plain
        # road window (tools/test_drape_pick.py). Nothing to reject, nothing counted.
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
# arms' node meshes pave those; audit §18 corrected an earlier claim here.)
#
# The rasters are decoded in plain Python rather than through Blender's image loader so that no
# colour-management transform can touch the samples — they carry NUMBERS, not pictures. That
# decoder is `csiibundle.png`, where it also serves as a reference implementation of §8's file
# format for any consumer to port.
_unfilter_slow = png_reader.unfilter_slow
_unfilter_scanlines = png_reader.unfilter_scanlines
_decode_png_gray = png_reader.decode_png_gray
_decode_png_rgba = png_reader.decode_png_rgba


class _Splat(splat_reader.Splat):
    """`csiibundle.splat.Splat` plus the one Blender-side step: turning the unpacked paint
    weights into an image datablock the shader can sample.

    Everything about WHICH channel is which ground, and how the four extras are packed into
    two bytes, lives in the package — read its module docstring before touching any of it.
    """

    def extras_image(self):
        """The four packed paint weights as one ordinary RGBA image, `extra1..4` in R,G,B,A.

        Unpacking in numpy rather than in shader nodes is what lets this tap be filtered
        LINEARLY: a packed byte cannot be interpolated (halfway between 0x0F and 0x10 decodes
        to a nibble pair unrelated to either neighbour), so the old point-sampled + FLOOR chain
        gave every painted region — the player's beaches included — literal 3.5 m square steps.
        The decode is bit-exact; nothing derived is written into the bundle.
        """
        # Blender's image rows run south-to-north; the PNG's run north-to-south (§3, row 0
        # north), and SplatUV already puts V = 0 at the south edge — so the flip belongs here.
        flat = self.extras(flip_v=True).reshape(-1)

        image = bpy.data.images.new("terrain_splat_extras", self.width, self.height,
                                    alpha=True, float_buffer=False)
        # ORDER IS LOAD-BEARING, and getting it wrong fails silently — the image reads back as
        # the blank default (RGB 0, alpha 1), which paints the whole map with extra4 and
        # nothing else. Both settings must precede the write, because changing either one
        # afterwards re-generates the buffer and discards it, and `update()` must follow it.
        image.colorspace_settings.name = "Non-Color"
        # extra4 rides the alpha channel as DATA. Without this Blender multiplies the other
        # three weights by it on every read — the premultiply trap that once made the whole
        # terrain render at a tenth of its albedo.
        image.alpha_mode = "CHANNEL_PACKED"
        image.pixels.foreach_set(flat)
        image.update()
        image.pack()                    # a generated image has no file to reload from
        return image


# `csiibundle.reader.terrain_paths` returns a namedtuple in exactly this unpack order:
# (heightmap, surface_index, water, surfaces, splatmap, rasters).
_terrain_paths = reader.terrain_paths

# Which ground each splatmap channel weights, and the stand-in albedo for a ground whose
# texture the bundle does not carry. Both live in `csiibundle.splat`, whose docstring records
# the two measurements that fix R=dirt / G=grass and the nibble packing of B and A — the
# inverted map is what "the terrain renders brown" was, and it looked entirely plausible.
_SPLAT_CHANNELS_DEFAULT = splat_reader.SPLAT_CHANNELS_DEFAULT
_GROUND_FALLBACK_COLOUR = splat_reader.GROUND_FALLBACK_COLOUR


# The clip-strip rasteriser evaluated its own copy of the cubic Bézier — the same formula as
# the lane bend's, written twice. One copy now, in `csiibundle.bezier`.
_bezier_points = bezier.evaluate


# The clip rasterisation, the corridor lattice, the hole-rim snap and the shallow-band clamp
# are all `csiibundle.terrain`. Read its docstring before touching any of them — the
# containment invariant (only a DEEP band may carve; a shallow one flattens and never deletes),
# the sub-sample deletion rule and the sideways-only rim snap are each a correction this
# project shipped the wrong way round at least once.
_CLIP_SCANLINE_VECTOR_ROWS = terrain_math.CLIP_SCANLINE_VECTOR_ROWS
_CLIP_REF_STATION_BITS = terrain_math.CLIP_REF_STATION_BITS
_CLIP_CONTAIN_SAMPLES = terrain_math.CLIP_CONTAIN_SAMPLES
_CLIP_CONTAIN_BLOCK = terrain_math.CLIP_CONTAIN_BLOCK
_CLIP_SNAP_REACH_CELLS = terrain_math.CLIP_SNAP_REACH_CELLS
_CLIP_DEEP_BAND_TOP = terrain_math.CLIP_DEEP_BAND_TOP
_lane_ramp = terrain_math.lane_ramp
_clip_span = terrain_math.clip_span
_fill_clip_quad_rows = terrain_math.fill_clip_quad_rows
_fill_clip_quad = terrain_math.fill_clip_quad
_face_footprint_reduce = terrain_math.face_footprint_reduce
_dilate = terrain_math.dilate
_mask_boundary = terrain_math.mask_boundary
_transition_fans = terrain_math.transition_fans
_contain_faces = terrain_math.contain_faces
_clip_grid = terrain_math.clip_grid
_snap_hole_boundary = terrain_math.snap_hole_boundary
_clamp_shallow_bands = terrain_math.clamp_shallow_bands


_splat_mapping = splat_reader.channel_mapping


_GROUND_FALLBACK_TILING = terrain_math.GROUND_FALLBACK_TILING
_ground_tiling = terrain_math.ground_tiling


def _splat_ground_material(surfaces_dir, splat, mapping, tiling_block=None):
    """One ground material mixing the game's grounds by its own splat weights.

    The splatmap image is sampled with linear interpolation in its own UV layer (SplatUV),
    so the blend is per-pixel smooth — the game's look — instead of per-face. The ground
    textures keep the world-anchored 8 m tiling of the legacy path.

    `mapping` names the ground each of R, G, B, A weights, plus the `base` ground. The base
    is not a channel: it is the residual of the DIRT channel alone, which is why a cliff
    renders rock — measured, the dirt weight falls 251.6 → 9.4 from flat ground to a 45°+ face.

    All four channels composite over that base in channel order as opacities at
    saturate(weight · splatMultiplier)^splatPower. See the comment on the blend below for why
    normalising them, or splitting them into two kinds, both render the map wrong (audit
    SPLAT-3).
    """
    material = bpy.data.materials.new("terrain_ground")
    tree = _ensure_nodes(material)
    bsdf = tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    _diffuse_ground(bsdf)

    world_uv = tree.nodes.new("ShaderNodeUVMap")
    world_uv.uv_map = "WorldUV"                     # world metres; the scale is per ground

    camera = tree.nodes.new("ShaderNodeCameraData")
    blend_cache = {}

    def lod_factor(start, end):
        """0 below `start`, 1 above `end`, ramped between — the game's tiling LOD blend,
        keyed on view distance. Cached: grass, dirt and the extras all share one pair."""
        key = (start, end)
        if key not in blend_cache:
            node = tree.nodes.new("ShaderNodeMapRange")
            node.clamp = True
            tree.links.new(camera.outputs["View Distance"], node.inputs["Value"])
            node.inputs["From Min"].default_value = start
            node.inputs["From Max"].default_value = max(end, start + 1e-3)
            node.inputs["To Min"].default_value = 0.0
            node.inputs["To Max"].default_value = 1.0
            blend_cache[key] = node.outputs["Result"]
        return blend_cache[key]

    def tap(image, metres_per_repeat):
        node = tree.nodes.new("ShaderNodeMapping")
        node.vector_type = "POINT"
        node.inputs["Scale"].default_value = (1.0 / metres_per_repeat,
                                              1.0 / metres_per_repeat, 1.0)
        tree.links.new(world_uv.outputs["UV"], node.inputs["Vector"])
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        tree.links.new(node.outputs["Vector"], texture.inputs["Vector"])
        return texture.outputs["Color"]

    def ground_texture(ground):
        path = os.path.join(surfaces_dir, f"{ground}.png")
        if not os.path.isfile(path):
            rgb = tree.nodes.new("ShaderNodeRGB")
            rgb.outputs[0].default_value = _GROUND_FALLBACK_COLOUR.get(ground,
                                                                       (0.3, 0.3, 0.3, 1.0))
            return rgb.outputs[0]

        image = _unpremultiply(bpy.data.images.load(path))
        shipped = _ground_tiling(tiling_block, ground)
        if shipped is None:
            return tap(image, _GROUND_FALLBACK_TILING)

        (near, mid, far), (mid_start, mid_end, far_start, far_end) = shipped
        # near -> mid -> far, each fading in over its own camera-distance band. One image
        # datablock, three samples: the whole point is that the same texture repeats at three
        # different rates so no single rate's grid can dominate.
        blended_near_mid = mix(tap(image, near), tap(image, mid),
                               lod_factor(mid_start, mid_end))
        return mix(blended_near_mid, tap(image, far), lod_factor(far_start, far_end))

    splat_uv = tree.nodes.new("ShaderNodeUVMap")
    splat_uv.uv_map = "SplatUV"
    splat_node = tree.nodes.new("ShaderNodeTexImage")
    splat_node.image = _unpremultiply(bpy.data.images.load(splat.path))
    splat_node.image.colorspace_settings.name = "Non-Color"
    splat_node.interpolation = "Linear"
    splat_node.extension = "EXTEND"
    tree.links.new(splat_uv.outputs["UV"], splat_node.inputs["Vector"])

    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(splat_node.outputs["Color"], separate.inputs["Color"])
    channels = {0: separate.outputs["Red"], 1: separate.outputs["Green"],
                2: separate.outputs["Blue"], 3: splat_node.outputs["Alpha"]}

    # The four paint weights, already unpacked out of B and A into an ordinary RGBA image
    # (`_Splat.extras_image`). Because they are plain weights now, this tap is Linear like the
    # soil one: the player's painted regions blend across the splatmap's 3.5 m texels instead
    # of stepping at every texel edge, which is what the old point-sampled + FLOOR decode
    # forced. Nothing about the game's own values changes — the decode is bit-exact.
    extras_node = tree.nodes.new("ShaderNodeTexImage")
    extras_node.image = splat.extras_image()
    extras_node.image.colorspace_settings.name = "Non-Color"
    extras_node.interpolation = "Linear"
    extras_node.extension = "EXTEND"
    tree.links.new(splat_uv.outputs["UV"], extras_node.inputs["Vector"])
    separate_extras = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(extras_node.outputs["Color"], separate_extras.inputs["Color"])
    extra_weight = {"extra1": separate_extras.outputs["Red"],
                    "extra2": separate_extras.outputs["Green"],
                    "extra3": separate_extras.outputs["Blue"],
                    "extra4": extras_node.outputs["Alpha"]}

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

    # All four channels are opacities, composited over the rock base in channel order at
    # saturate(weight * splatMultiplier)^splatPower — the game's own multiplier and power,
    # shipped in the rasters block. This is NOT a partition: R and G are both near-saturated
    # on flat land (251.6 and 201.7) and routinely sum past 255, so normalising them turns
    # every meadow half brown. Rock is the residual of R alone and needs no channel: where the
    # dirt weight falls away, which is what a cliff face is, the base shows through.
    #
    # The order is what makes it read correctly: dirt (R) blankets everything including the
    # lakebed, then grass (G) covers the land on top of it, then the player's paint. So the
    # shoreline — dirt with grass falling to zero at the water — is the sandy band, and the
    # land above it is green.
    #
    # A channel the producer could not name is skipped — that ground is present but
    # unnameable, and painting it as a guess would be worse than leaving the ground below.
    #
    # B AND A ARE BIT-PACKED, two 4-bit weights each (audit SPLAT-6). That is how four paintable
    # extras fit an R8G8 user splatmap, and it is why reading them as single 0-255 opacities lost
    # the player's sand: it merged extra1 with extra2, extra3 with extra4, and rendered the
    # merged result with the wrong ground's texture entirely.
    #     B.low = extra1 (tool 01 "Dry")     B.high = extra2 (tool 02 "Lush")
    #     A.low = extra3 (tool 03 "Dirt")    A.high = extra4 (tool 04 "Sand")
    # The unpacking happens in numpy at import (`_Splat.extras_image`), not in shader nodes, so
    # `extra_weight` above already holds four continuous 0..1 weights.
    def opacity_of(weight):
        out = math("MINIMUM", math("MULTIPLY", weight, splat.multiplier), 1.0)
        return out if splat.power == 1.0 else math("POWER", out, splat.power)

    blended = ground_texture(mapping["base"] if mapping.get("base") else
                             _SPLAT_CHANNELS_DEFAULT["R"])

    # The two continuous soil weights first: dirt blankets everything including the lakebed,
    # then grass covers the land on top of it.
    for index, key in ((0, "R"), (1, "G")):
        ground = mapping.get(key)
        if ground:
            blended = mix(blended, ground_texture(ground), opacity_of(channels[index]))

    # Then the player's paint, in extra order.
    for ground in ("extra1", "extra2", "extra3", "extra4"):
        blended = mix(blended, ground_texture(ground), opacity_of(extra_weight[ground]))

    # Straight to the BSDF: no gain, no grading. The ground ships albedo, and `--colour` grades
    # the whole scene in the display pipeline rather than pre-multiplying the terrain.
    tree.links.new(blended, bsdf.inputs["Base Color"])
    return material


def _unpremultiply(image):
    """Stop Blender multiplying this texture's COLOUR by its ALPHA.

    THE SINGLE LARGEST FIDELITY BUG THIS IMPORTER HAS HAD. An Image Texture node's `Color`
    output is alpha-premultiplied when the image is loaded with the default `STRAIGHT` alpha
    mode. CS2 packs data into the alpha channel of its base-colour maps — it is not opacity —
    and it is mostly near zero: measured on this bundle, all seven ground textures have a mean
    alpha between 0.036 and 0.133 and **not one texel** at full opacity. The terrain was
    therefore drawn at 4-13% of the albedo the bundle ships. Measured at the sand reference
    point: `#24211d` against a shipped albedo of `#777066`, an 11x deficit.

    That is what "the import is very dark and the colours do not come through" was. It also
    explains the constants this file used to carry: `_GROUND_BRIGHTNESS = 3.09` was compensating
    for a ~10x darkening with a 3x gain, which is why it never quite worked and why removing it
    made things look worse rather than better.

    SCOPE: only the textures THIS file loads — the terrain grounds, the splatmap, the area
    surfaces. Blender's glTF importer already sets `CHANNEL_PACKED` itself, so object, network
    and prop textures were never affected; checked, rather than assumed, before claiming it.

    `CHANNEL_PACKED` is the right mode rather than `NONE`: it tells Blender the alpha is data,
    so `Color` comes through unmodified, while the `Alpha` output stays available for the
    materials that genuinely do cut out (foliage, decals).
    """
    if image is not None:
        image.alpha_mode = "CHANNEL_PACKED"
    return image


def _unpremultiply_all():
    """Apply `_unpremultiply` to every image in the file.

    A backstop rather than the fix: the load sites above are what actually mattered, and
    Blender's glTF importer already channel-packs its own. This catches anything that arrives
    by a path nobody thought about, and reports how many so a future change that reintroduces
    premultiplied colour shows up in the log instead of silently darkening the scene.
    """
    fixed = 0
    for image in bpy.data.images:
        if image.alpha_mode != "CHANNEL_PACKED":
            image.alpha_mode = "CHANNEL_PACKED"
            fixed += 1
    return fixed


def _ensure_nodes(datablock):
    """Return a material's/world's node tree, enabling nodes only if they are not already there.

    `use_nodes` is deprecated in Blender 5.x (removal slated for 6.0) because materials and worlds
    are always node-based now — assigning it emits a DeprecationWarning on every material the
    importer builds, which on a full city is thousands of lines of noise. Reading `node_tree`
    first is both quieter and correct: on 5.x it is already there, and on older Blender the
    assignment still runs.
    """
    if datablock.node_tree is None:
        datablock.use_nodes = True
    return datablock.node_tree


def _diffuse_ground(bsdf):
    """Zero the white dielectric specular on a terrain ground.

    Principled defaults to a dielectric reflection that is WHITE regardless of base colour. CS2's
    terrain is diffuse ground with nothing there to reflect, so zeroing it is a modelling
    correction rather than a colour adjustment, and that is the whole justification — the effect
    is modest.

    Re-measured 2026-08-11 at the sand reference point on the fixed pipeline: leaving the
    specular on costs saturation 0.154 -> 0.141 (8.4%) and *raises* value 0.450 -> 0.462. An
    earlier note here claimed 64% of the saturation and a large brightness loss; those numbers
    were taken while the ground was being drawn at a tenth of its albedo (audit §2.5 SPLAT-5) and
    through a probe that double-encoded its own readings, and neither survived the fixes.

    The socket was renamed across versions ("Specular" -> "Specular IOR Level"); try both.
    """
    for name in ("Specular IOR Level", "Specular"):
        socket = bsdf.inputs.get(name)
        if socket is not None:
            socket.default_value = 0.0
            return


def _set_alpha_blend(material):
    """Make a material's Alpha input actually read as transparency.

    Blender 4.2 moved EEVEE transparency from `blend_method` to `surface_render_method`, and the
    new default is DITHERED — stochastic transparency, which at low sample counts reads as noise
    or as fully opaque, not as a translucent surface. `blend_method` still exists on 5.x as a
    legacy alias, so a `hasattr(material, "blend_method")` guard passes and silently does nothing
    useful: the water was setting Alpha 0.8 and rendering solid. Set the modern property when it
    is there, and fall back for pre-4.2 Blender.
    """
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"


def _ground_material(name, texture_path, fallback_colour):
    material = bpy.data.materials.new(name)
    _ensure_nodes(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    _diffuse_ground(bsdf)
    if texture_path and os.path.isfile(texture_path):
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = _unpremultiply(bpy.data.images.load(texture_path))
        material.node_tree.links.new(image_node.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = fallback_colour
    return material


def build_terrain(bundle_dir, collection, center, radius, clip_floor=None):
    """Place the §8 terrain under everything: ground grid, roadbed split, water surface.

    Returns `(placed, ground)` — the number of objects placed (0 when the layer is absent)
    and the ground grid itself as `(object, cell_size)` for the layers painted onto it
    (`build_areas`), or None when no grid was built. The region filter crops the grid to the
    same window as everything else; without one, the coarse grid keeps at most ~1024 blocks
    per axis and the decision corridors (roadbed, clip, shoreline) are meshed at native
    resolution inside it. `clip_floor` overrides every clip strip's `bottom` (diagnostic,
    `--clip-floor`).
    """
    heightmap_path, surface_path, water_path, surfaces_dir, splat_path, rasters = _terrain_paths(bundle_dir)
    if not os.path.isfile(heightmap_path):
        LOG.warn(f"no terrain layer at {heightmap_path} — skipping terrain")
        return 0, None
    if rasters is None:
        LOG.warn("terrain rasters present but no geo-reference (bundle.json rasters or "
                 "terrain/rasters.json) — skipping terrain")
        return 0, None

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

    # Contract §8c. The terrain covering tunnel portals and sunken trenches is never carved out
    # of the heightmap — the game hides it per-frame with a stencil pass — so the clip volumes
    # are rasterised here against this heightmap. The layer is mandatory; _clip_grid raises on
    # a bundle without it.
    (cut_grid, contain_raster, band_count_raster, deep_raster, sub_bands,
     cut_refs, strip_geo, band_floor_raster) = _clip_grid(
        bundle_dir, width, height, heights, cell, origin_x, origin_z, elevation_scale,
        elevation_offset, floor_override=clip_floor)

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
        return 0, None

    # -- the lattice and its faces -----------------------------------------------------------
    # `csiibundle.terrain.build_lattice` is the whole of it: the corridor that decides where the
    # mesh goes native, the three face families welded onto one vertex set, the containment
    # deletion, the hole-rim snap and the sub-cut of a deep band too narrow to contain a face.
    # Its docstring carries the reasoning, including the two face-deletion rules that are
    # tempting and wrong.
    lattice = terrain_math.build_lattice(
        heights, width, height, surface_ids, levels,
        row0, col0, row1, col1, stride, cell, origin_x, origin_z,
        elevation_scale, elevation_offset,
        cut_grid, contain_raster, band_count_raster, deep_raster,
        cut_refs, band_floor_raster, sub_bands, strip_geo)
    if lattice is None:
        LOG.warn("terrain: the region window is smaller than one face — skipping terrain")
        return 0, None
    if lattice.stray_cells:
        LOG.warn(f"terrain: {lattice.stray_cells} cut cell(s) escaped the refine corridor — "
                 "coarse faces may wall off part of a tunnel")
    lat_h = lattice.lat_h
    lat_w = lattice.lat_w
    nfr = lattice.nfr
    nfc = lattice.nfc
    verts = lattice.verts
    loops = lattice.loops
    sizes = lattice.sizes
    face_class = lattice.face_class
    native_y = lattice.native_y
    ground_y = lattice.ground_y
    wet = lattice.wet
    has_level = lattice.has_level
    level_value = lattice.level_value
    roadbed = lattice.roadbed
    cut = lattice.cut
    contain = lattice.contain
    band_count = lattice.band_count
    block_refined = lattice.block_refined
    coarse_loops = lattice.coarse_loops
    cbr = lattice.cbr
    cbc = lattice.cbc
    cell_rr = lattice.cell_rr
    cell_cc = lattice.cell_cc
    refined_loops_all = lattice.refined_loops_all
    fan_loops = lattice.fan_loops
    fan_faces = lattice.fan_faces
    refined_faces = lattice.refined_faces
    cut_faces = lattice.cut_faces
    roadbed_faces = lattice.roadbed_faces
    rock_faces = lattice.rock_faces
    snapped = lattice.snapped
    unreferenced = lattice.unreferenced
    pushed = lattice.pushed
    sub_parents = lattice.sub_parents
    sub_dropped = lattice.sub_dropped
    sub_kept = lattice.sub_kept
    band_clamped = lattice.band_clamped
    band_clamp_drop = lattice.band_clamp_drop

    for row_number in range(lat_h):
        # "terrain rows" — deliberately not "terrain:" — stays outside every ab_compare.py count
        # marker so this progress line can print at whatever resolution the lattice has without
        # ever becoming a gated line the A/B baseline does not have.
        LOG.progress("terrain rows", row_number + 1, lat_h)


    # -- the game's own ground blend, when the bundle carries it --------------------------
    # terrain/splatmap.png is the 4096² weight map the terrain shader samples per pixel, and
    # the rasters "splatmap" block names which ground each channel weights (contract §8). It
    # is read, not calibrated: a calibration used to run here and got three of four channels
    # wrong — see _SPLAT_CHANNELS_DEFAULT for exactly which and why.
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
        splat_mapping, published = _splat_mapping(splat_block)
        named = ", ".join(f"{key}={splat_mapping[key] or 'unknown'}" for key in "RGBA")
        source = "published by the bundle" if published else "assumed"
        LOG.step(f"splatmap channels {source}: "
                 f"{named}, base={splat_mapping.get('base') or 'none'} (the residual of the "
                 f"first channel)")
    # World-anchored UVs so the ground textures tile at a plausible density without any
    # per-face unwrap. Per-LOOP, in the same flat loop order as `loops` (coarse quads, refined
    # quads, fan triangles).
    #
    # Two layers, deliberately. `UVMap` keeps the legacy 8 m-per-repeat convention the
    # per-class fallback materials are built against. `WorldUV` is plain world METRES, and the
    # splat material scales it per ground from the shipped `terrainTiling` — one ground tiles
    # at 14.34 m and another at 68.27 m, so a single baked-in rate cannot serve both.
    loop_x, loop_z = verts[loops, 0], verts[loops, 1]
    uv_layers = {"UVMap": numpy.stack((loop_x / 8.0, loop_z / 8.0), axis=-1)}

    if splat_mapping is not None:
        uv_layers["WorldUV"] = numpy.stack((loop_x, loop_z), axis=-1)
        tiling_block = rasters.get("terrainTiling") if isinstance(rasters, dict) else None
        named_tiling = [ground for ground in ("grass", "dirt", "rock")
                        if _ground_tiling(tiling_block, ground)]
        if named_tiling:
            detail = ", ".join(
                f"{ground} {'/'.join(f'{r:g}' for r in _ground_tiling(tiling_block, ground)[0])} m"
                for ground in named_tiling)
            LOG.step(f"ground tiling from the bundle (near/mid/far per repeat): {detail}; "
                     "extras tile with grass")
        else:
            LOG.warn(f"no rasters.terrainTiling in the bundle — every ground falls back to "
                     f"{_GROUND_FALLBACK_TILING:g} m per repeat, expect a visible grid")
        material_runs = [(_splat_ground_material(surfaces_dir, splat, splat_mapping,
                                                 tiling_block), len(sizes))]
        # No face overrides on the blend. The roadbed keeps whatever the splat says, as in
        # game — the blend has no roadbed response at all, and the roadbed LOOK is carried by
        # the ~13%-alpha wear decals and surface areas drawn on top (the sink still applies).
        # Cliffs need no slope override either: where the dirt weight falls away the rock base
        # shows through, which is what keeps gorge walls smooth instead of the per-face
        # checkerboard.

        # The splat blend samples in its own geo-referenced UV space.
        uv_layers["SplatUV"] = numpy.stack(
            ((loop_x - splat.min_x) / splat.area_x,
             (loop_z - (splat.max_z - splat.area_z)) / splat.area_z), axis=-1)
    else:
        grass = _ground_material("terrain_natural", os.path.join(surfaces_dir, "grass.png"),
                                 (0.20, 0.30, 0.12, 1.0))
        dirt = _ground_material("terrain_roadbed", os.path.join(surfaces_dir, "dirt.png"),
                                (0.35, 0.28, 0.20, 1.0))
        rock = _ground_material("terrain_rock", os.path.join(surfaces_dir, "rock.png"),
                                (0.30, 0.28, 0.26, 1.0))
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

    # Snapping two neighbouring boundary vertices onto the same point of a prism side
    # collapses their shared sliver face to zero area — a legitimate consequence of closing
    # the hole exactly, not broken math, so it gets a calm aggregate rather than the
    # CONTRACT VIOLATION wording (repeated-index faces still alarm loudly, by design).
    terrain_degenerates = {"faces": 0}

    def _report_terrain_degenerate(_name, count, repeated):
        assert not repeated
        terrain_degenerates["faces"] += count

    mesh = _mesh_from_arrays("bundle_terrain", verts, loops, uv_layers=uv_layers,
                              material_runs=material_runs, poly_sizes=sizes, smooth=True,
                              degenerate_report=_report_terrain_degenerate)
    if terrain_degenerates["faces"]:
        LOG.step(f"{terrain_degenerates['faces']} sliver face(s) collapsed by the "
                 "hole-boundary snap and dropped")
    # Smooth-shade the grid: the game's terrain is a continuous heightfield, and a
    # flat-shaded quad is uniformly lit — at the full-city coarse blocks that reads as a
    # brightness checkerboard over every slope, squares no texture blend can hide. The
    # shared lattice makes the smoothing correct: every seam's normal averages both sides.
    terrain_object = bpy.data.objects.new("bundle_terrain", mesh)
    collection.objects.link(terrain_object)
    placed = 1

    # -- the water surface -----------------------------------------------------------------
    # The water raster is the SURFACE LEVEL in the heightmap's encoding (§8, 0 = dry): water
    # exists where level > ground. A face is placed wherever any corner is wet, decided per
    # face family on the same lattice as the ground — native cells inside refined blocks
    # (the shoreline is a corridor, so the water boundary steps at native resolution, not at
    # the coarse block size), fans in transition blocks, coarse quads in the open water. The
    # level extends one sim cell past the shoreline (producer dilation), so the surface runs
    # flat into the bank and the ground cuts the shoreline where the game's own shoreline is.
    wet_faces = 0
    if level_value is not None and wet.any():
        coarse_wet = (wet[cbr * stride, cbc * stride] |
                      wet[(cbr + 1) * stride, cbc * stride] |
                      wet[(cbr + 1) * stride, (cbc + 1) * stride] |
                      wet[cbr * stride, (cbc + 1) * stride])
        water_coarse = coarse_loops[coarse_wet]

        refined_wet = (wet[cell_rr, cell_cc] | wet[cell_rr, cell_cc + 1] |
                       wet[cell_rr + 1, cell_cc] | wet[cell_rr + 1, cell_cc + 1])
        # NOT filtered by the cut: a clipped trench under a lake still has water over it,
        # exactly as the coarse rule always kept it.
        water_refined = refined_loops_all[refined_wet]

        if len(fan_loops):
            fan_tris = fan_loops.reshape(-1, 3)
            fan_wet = wet.reshape(-1)[fan_tris].any(axis=1)
            water_fans = fan_tris[fan_wet]
        else:
            water_fans = numpy.zeros((0, 3), dtype=numpy.int64)

        wet_faces = len(water_coarse) + len(water_refined) + len(water_fans)
        if wet_faces:
            water_loops_all = numpy.concatenate((water_coarse.reshape(-1),
                                                 water_refined.reshape(-1),
                                                 water_fans.reshape(-1)))
            water_sizes = numpy.concatenate((
                numpy.full(len(water_coarse), 4, dtype=numpy.int64),
                numpy.full(len(water_refined), 4, dtype=numpy.int64),
                numpy.full(len(water_fans), 3, dtype=numpy.int64)))

            # A corner past the dilated ring carries no level; seat it on the ground so the
            # surface closes instead of tearing.
            surface_y = numpy.where(has_level, level_value, native_y)

            wused = numpy.zeros(lat_h * lat_w, dtype=bool)
            wused[water_loops_all] = True
            wused_idx = numpy.nonzero(wused)[0]
            wvid = numpy.full(lat_h * lat_w, -1, dtype=numpy.int64)
            wvid[wused_idx] = numpy.arange(len(wused_idx))
            wur, wuc = numpy.divmod(wused_idx, lat_w)
            water_verts = numpy.stack(
                (origin_x + (col0 + wuc).astype(numpy.float64) * cell,
                 origin_z - (row0 + wur).astype(numpy.float64) * cell,
                 surface_y[wur, wuc].astype(numpy.float64)), axis=-1)

            water_material = bpy.data.materials.new("terrain_water")
            _ensure_nodes(water_material)
            water_bsdf = water_material.node_tree.nodes.get("Principled BSDF")
            water_bsdf.inputs["Base Color"].default_value = (0.08, 0.22, 0.35, 1.0)
            water_bsdf.inputs["Roughness"].default_value = 0.05
            if "Alpha" in water_bsdf.inputs:
                water_bsdf.inputs["Alpha"].default_value = 0.8
            _set_alpha_blend(water_material)

            water_mesh = _mesh_from_arrays("bundle_water", water_verts,
                                            wvid[water_loops_all],
                                            material_runs=[(water_material, wet_faces)],
                                            poly_sizes=water_sizes, smooth=False)
            water_object = bpy.data.objects.new("bundle_water", water_mesh)
            collection.objects.link(water_object)
            placed += 1

    LOG.done(f"terrain:   {nfr}x{nfc} block window of {width}x{height} at {cell:g} m"
             + (f", stride {stride}" if stride > 1 else ""))
    LOG.step(f"{roadbed_faces} roadbed face(s) (sunk {_ROADBED_SINK:g} m), "
             f"{rock_faces} rock face(s), {wet_faces} water face(s), "
             f"{cut_faces} face(s) cut (tunnel portals, trenches and area clips — a shallow "
             f"surface band flattens but never carves)")
    if band_clamped:
        LOG.step(f"{band_clamped} lattice vertex(es) standing inside a shallow surface band "
                 f"lowered to the band floor, worst drop {band_clamp_drop:.2f} m — the pixels "
                 f"the game discards there (see _clamp_shallow_bands)")
    band_samples = int(band_count.sum(dtype=numpy.int64))
    if cut_faces or band_samples:
        # The containment check, not a size check. Both sides are now the DEEP bands alone —
        # a shallow band no longer deletes (see `_contain_faces`), so it contributes neither
        # deleted faces nor covered area. This grid must never delete MORE than those prisms
        # cover: a ratio above 1.00 is terrain removed that nothing draws over, i.e. sky. Below
        # 1.00 is the safe direction — terrain kept under a road that hides it.
        #
        # Note what this ratio still cannot see: it scores the cut against the PRISM, and the
        # prism is not the pavement. That is what hid the width-transition notches for a week.
        # `tools/test_clip_cut.py` scores against the real segment meshes instead.
        #
        # The denominator is the clipped area itself, measured at the containment sampling —
        # not a face count, which quantises the very thing being measured. Under this deletion
        # rule the ratio cannot exceed 1.00 by construction, so a warning here means the
        # sampling missed a sliver, not that the rule drifted.
        sample = cell / _CLIP_CONTAIN_SAMPLES
        deleted_area = cut_faces * cell * cell
        clipped_area = band_samples * sample * sample
        ratio = (deleted_area / clipped_area) if clipped_area else float("nan")
        LOG.step(f"cut {deleted_area:,.0f} m² of terrain against {clipped_area:,.0f} m² the "
                 f"clip prisms cover (x{ratio:.2f}; must not exceed 1.00 — above it is sky)")
        if ratio > 1.0:
            LOG.warn(f"the cut deletes {(ratio - 1.0) * 100:.0f}% more terrain than the clip "
                     "prisms cover — expect see-through gaps beside roads")

    # The one residual this rule cannot reach. A face is 3.5 m; a clip strip narrower than two
    # of them holds no whole face, so nothing is deleted along it and there is no hole rim for
    # `_snap_hole_boundary` to push out to the wall either. Under a shallow surface band that is
    # invisible — the road covers it — but under a TRENCH or PORTAL band the terrain left behind
    # stands in the cutting, measured on the 2026-08-11 rip at a median 7.4 m proud of the road
    # where it happens. City-wide, strips under two cells wide carry 2.5% of the deep-band
    # footprint, so this is stated rather than fixed; fixing it means cutting those faces on the
    # prism boundary itself (see §41), which is a lattice change and needs a render to accept.
    # A deep band's own edge always meets faces it does not contain, and those are fine: they
    # are the hole rim, and the snap pushes them out to the wall. What cannot be reached is a
    # face a deep band meets with NO deleted face beside it — there is no rim there, because the
    # strip never held a whole face.
    if sub_parents:
        LOG.step(f"{sub_parents} face(s) met a trench or portal band too narrow for a "
                 f"{cell:g} m face to sit inside and were re-cut at "
                 f"{cell / _CLIP_CONTAIN_SAMPLES:.2f} m — {sub_dropped * (cell / _CLIP_CONTAIN_SAMPLES) ** 2:,.0f} m² "
                 f"dropped, {sub_kept} sub-face(s) kept")
    if snapped or unreferenced:
        LOG.step(f"{snapped} hole-boundary vertex(es) snapped onto the clip prisms "
                 f"({pushed} pushed out of a band, the rest pulled in)"
                 + (f", {unreferenced} found no strip to snap to (area-only cuts)"
                    if unreferenced else ""))
    if stride > 1:
        LOG.step(f"{refined_faces} native face(s) in {int(block_refined.sum())} corridor "
                 f"block(s), {fan_faces} transition fan face(s), "
                 f"{len(coarse_loops)} coarse {cell * stride:g} m face(s)")
    return placed, (terrain_object, cell * stride)


# The §8b conformance rules are `csiibundle.areas` — an area triangle is a FOOTPRINT the game
# projects onto the terrain, never a plane, and its docstring says what follows from that.
_AREA_LIFT = area_math.AREA_LIFT
_AREA_CONFORM_TOLERANCE = area_math.CONFORM_TOLERANCE
_AREA_MAX_DEPTH = area_math.MAX_DEPTH
_AREA_PIECES_PER_CELL = area_math.PIECES_PER_CELL
_AREA_PIECE_BUDGET = area_math.PIECE_BUDGET
_AREA_DEDUP_MM = area_math.DEDUP_MM
_AREA_UV_SCALE_FALLBACK = area_math.UV_SCALE_FALLBACK
_conform_area_triangle = area_math.conform_triangle


def build_areas(bundle_dir, collection, center, radius, ground=None, cell=3.5):
    """Place the §8b surface areas: driveways, parking-lot pavement, park paths.

    These are the painted patches `AreaBatchSystem` draws from area triangles — the visible
    ground of every meshless prefab (parking lots, sports courts, campsites) and of house
    driveways. One object per area prefab, world-anchored UVs, catalogue texture or colour.

    `ground` is a `_ProjectionSurface` over the terrain grid and `cell` its spacing. The game
    paints an area onto the terrain rather than laying a plane on it (audit §30), so with a
    ground to conform to, each triangle is split until it follows — see
    `_conform_area_triangle`. Without one (`--areas` and no `--terrain`) the flat triangles
    are all there is, and the ground will cut through them wherever it is not level.
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
                (colour[0], colour[1], colour[2], 1.0))
        return materials[prefab]

    # The ground each area is painted onto (audit §30). Without the terrain layer there is
    # nothing to conform to, and the flat triangles are all this can draw.
    sample_cache = {}
    scale = 1000.0 / _AREA_DEDUP_MM

    def ground_at(x, y):
        """Terrain height under Blender-frame (x, y), memoised on the rounded position."""
        key = (round(x * scale), round(y * scale))
        if key not in sample_cache:
            sample_cache[key] = ground.height(x, y) if ground is not None else None
        return sample_cache[key]

    builders = {}
    placed = skipped_region = dropped_bad = missed = 0
    counters = {"unconformed": 0, "unconformed_area": 0.0, "worst": 0.0}
    source_tris = painted_area = 0.0
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
        # Bundle → Blender: (x, y, z) → (x, z, y), and that flips handedness (§2.2), so every
        # face reverses to keep facing up — the same compensation the lane bend applies.
        corners = []
        for n in nodes:
            hit = ground_at(n[0], n[2])
            if hit is None:
                missed += 1
            corners.append((n[0], n[2], (n[1] if hit is None else hit)))
        for tri in triangles:
            if max(tri) >= len(nodes) or min(tri) < 0:
                dropped_bad += 1
                continue
            source_tris += 1
            a, b, c = (corners[i] for i in reversed(tri))
            painted_area += abs((b[0] - a[0]) * (c[1] - a[1])
                                - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
            for piece in _conform_area_triangle((a, b, c), cell, ground_at, counters):
                base = len(sink["verts"])
                sink["verts"].extend((p[0], p[1], p[2] + _AREA_LIFT) for p in piece)
                sink["faces"].append((base, base + 1, base + 2))
        placed += 1
        LOG.progress("areas conformed", placed, None)

    built = 0
    for prefab, sink in builders.items():
        if not sink["faces"]:
            continue
        mesh = bpy.data.meshes.new(f"area_{prefab}")
        mesh.from_pydata(sink["verts"], [], sink["faces"])
        mesh.materials.append(area_material(prefab))
        uv_layer = mesh.uv_layers.new(name="UVMap")
        # World-planar, at the prefab's own authored rate: `colossal_UVScale` is set straight
        # from `RenderedArea.m_UVScale` (AreaBatchSystem line 2109) and multiplies the world
        # position. `_AREA_UV_SCALE_FALLBACK` is the game's own field default, for a bundle
        # ripped before §8b carried the value.
        uv_scale = float((catalogue.get(prefab) or {}).get("uvScale")
                         or _AREA_UV_SCALE_FALLBACK)
        for loop in mesh.loops:
            x, y, _ = mesh.vertices[loop.vertex_index].co
            uv_layer.data[loop.index].uv = (x * uv_scale, y * uv_scale)
        mesh.validate()
        obj = bpy.data.objects.new(f"area_{prefab}", mesh)
        collection.objects.link(obj)
        built += 1

    LOG.done(f"areas:     {placed} surface area(s) into {built} object(s) "
             f"over {len(builders)} prefab(s)"
             + (f", {skipped_region} outside the region" if skipped_region else ""))

    # A prefab with neither texture nor colour renders the neutral grey fallback — say why,
    # per prefab, instead of leaving grey boxes as a riddle. `unresolved` is the producer's
    # flag for "the save references an asset (pack) that was not loaded in the rip session":
    # the game itself drew those areas grey (Missing Area fallback), and the cure is enabling
    # the pack in-game and re-ripping, not anything on this side.
    grey = [prefab for prefab in builders
            if not (catalogue.get(prefab) or {}).get("texture")
            and not (catalogue.get(prefab) or {}).get("color")]
    if grey:
        unresolved = [p for p in grey if (catalogue.get(p) or {}).get("unresolved")]
        for prefab in sorted(unresolved):
            name = (catalogue.get(prefab) or {}).get("name", prefab)
            LOG.warn(f"area prefab did not resolve at rip time (asset pack not loaded "
                     f"or version mismatch): {name}")
        if unresolved:
            LOG.warn(f"{len(unresolved)} area prefab(s) render grey because their asset "
                     "was missing in the rip session — the game drew them grey too "
                     "(Missing Area); enable the packs, load the save, re-rip")
        for prefab in sorted(set(grey) - set(unresolved)):
            LOG.warn(f"area prefab carries no colour and no texture: {prefab}")
    if ground is None:
        LOG.warn("terrain layer not built — surface areas stay flat, expect the ground to "
                 "cut through pavement on any slope (audit §30)")
    else:
        total = sum(len(sink["faces"]) for sink in builders.values())
        LOG.step(f"{int(source_tris)} area triangle(s) conformed to the terrain as "
                 f"{total} piece(s)"
                 + (f", {missed} node(s) found no ground" if missed else ""))
        if counters["unconformed"]:
            share = 100.0 * counters["unconformed_area"] / max(painted_area, 1e-9)
            LOG.step(f"{counters['unconformed_area']:.0f} m2 of {painted_area:.0f} m2 painted "
                     f"({share:.2f}%) stayed unconformed — ground up to "
                     f"{counters['worst']*100:.0f} cm above it, over "
                     f"{counters['unconformed']} piece(s)")
    if dropped_bad:
        LOG.warn(f"CONTRACT VIOLATION: {dropped_bad} triangle(s) index out of range")
    return built


# -- scene building -------------------------------------------------------------------------

def _view_transform_available(name):
    """Is `name` one of this Blender's view transforms?

    The enum is OCIO-driven, so it cannot be read off `bl_rna` (that reports a placeholder); the
    only reliable test is to try the assignment and put back what was there.
    """
    view_settings = bpy.context.scene.view_settings
    previous = view_settings.view_transform
    try:
        view_settings.view_transform = name
        return True
    except (TypeError, AttributeError):
        return False
    finally:
        try:
            view_settings.view_transform = previous
        except (TypeError, AttributeError):
            pass


def _apply_grading(bundle_dir):
    """Replay the game's colour grading from `grading.json` (the `--colour` flag).

    CS2 authors ground albedo near-grey (rock sRGB saturation 0.111, sand 0.143); the on-screen
    palette is HDRP post-processing on top. The producer ships the game's **resolved** volume-stack
    values, so this replays real numbers rather than approximating a screenshot. OFF by default:
    a plain import stays a faithful 1:1 albedo view.

    WHERE THE GRADING RUNS, AND WHY IT MATTERS MORE THAN THE NUMBERS

    A colour grade is a transform on *final radiance*, applied in a fixed order. There are three
    places to put it in Blender and only one of them is both correct and visible:

      * the COMPOSITOR runs on rendered output only. A scene graded there looks raw in the
        viewport, which is where an imported city is actually looked at. This is where the
        previous version put contrast, and it is why measurements of "what the importer produces"
        kept disagreeing with what was on screen.
      * the MATERIALS are visible, but they carry albedo, not radiance, and they come before the
        lighting rather than after it. Saturation happens to survive the move (it is homogeneous,
        so it commutes with a scalar light), but it then runs BEFORE the contrast instead of
        after, and measured on this city's grass that reordering costs 18% on the blue channel.
      * the VIEW TRANSFORM runs on final radiance, in order, and shows in the viewport and in F12
        alike. Everything cross-channel therefore belongs there — which needs an OCIO config, and
        `tools/colour_model.py` writes one from this bundle's own numbers.

    So: with the generated config active, this sets the view and the exposure and touches nothing
    else. Without it, the per-channel part of the chain still goes into Blender's native Color
    Management (exact, viewport-visible) and the cross-channel part is reported as MISSING rather
    than being moved somewhere it would silently change order.

    Exposure is left as the user's control on purpose. The game meters every frame
    (`AutomaticHistogram`), so absolute brightness is a property of where the camera was pointed,
    not of the city; the value set here is only the manual offset the bundle does know about.
    """
    grading = colour_model.load(bundle_dir)
    if grading is None:
        LOG.warn("--colour: this bundle has no grading.json (ripped before the grading layer); "
                 "leaving the scene ungraded")
        return

    global _GRADING
    _GRADING = grading
    scene = bpy.context.scene

    available = _view_transform_available(grading.view_name)
    view, exposure, gamma, gain, missing = grading.colour_management(available)

    scene.view_settings.exposure = exposure
    if hasattr(scene.view_settings, "gamma"):
        scene.view_settings.gamma = gamma
    if hasattr(scene.view_settings, "look"):
        scene.view_settings.look = "None"
    if hasattr(scene.view_settings, "use_curve_mapping"):
        scene.view_settings.use_curve_mapping = False
    if hasattr(scene.view_settings, "use_white_balance"):
        # Blender's own white balance is a Kelvin control on a different scale from HDRP's
        # -100..100; mapping between them would be a fit. The game's value is applied exactly
        # instead, either in the view transform or as a light gain.
        scene.view_settings.use_white_balance = False
    try:
        scene.view_settings.view_transform = view
    except (AttributeError, TypeError):
        LOG.warn(f"--colour: this Blender has no '{view}' view transform; leaving "
                 f"{scene.view_settings.view_transform}")

    # The per-channel gain rides the lights when it cannot ride the view. Scaling every light per
    # channel scales the whole render per channel, bounces included, so this is exact rather than
    # an approximation — but it is only reached on the fallback path.
    if any(abs(c - 1.0) > 1e-6 for c in gain):
        for ob in scene.objects:
            if ob.type == "LIGHT":
                ob.data.color = (gain[0], gain[1], gain[2])
        background = (scene.world.node_tree.nodes.get("Background")
                      if scene.world and scene.world.node_tree else None)
        if background is not None:
            base = background.inputs["Color"].default_value
            background.inputs["Color"].default_value = (base[0] * gain[0], base[1] * gain[1],
                                                        base[2] * gain[2], 1.0)

    LOG.step(f"grading applied from grading.json: view transform {view}, "
             f"exposure {exposure:+.2f} EV" + (f", gamma {gamma:.4g}" if gamma != 1.0 else "")
             + (f", light gain {tuple(round(c, 4) for c in gain)}"
                if any(abs(c - 1.0) > 1e-6 for c in gain) else ""))
    for line in grading.warnings:
        LOG.warn("--colour: " + line)

    if available:
        LOG.step(f"the '{grading.view_name}' view is carrying the whole chain — gain, contrast, "
                 f"saturation and shadows/midtones/highlights all run on final radiance, in the "
                 f"viewport")
        return

    for line in missing:
        LOG.warn("--colour: NOT applied - " + line)
    config = colour_model.blender_config_path()
    if config is None:
        LOG.warn("--colour: could not find Blender's own config.ocio to extend; set "
                 "CARTO_BLENDER_COLORMANAGEMENT to the folder holding it")
        return
    try:
        path = grading.write_ocio(os.path.join(bundle_dir, "colour"), config)
    except OSError as exc:
        LOG.warn(f"--colour: could not write the OCIO config ({exc})")
        return

    # LOUD, because this is the one step a person can silently skip and then judge the whole
    # feature by the result. Without the config Blender has no way to run a cross-channel
    # operator before the display transform, so the scene loses the saturation entirely and
    # reads flat and dark — which looks exactly like "the grading does not work".
    LOG.warn("=" * 78)
    LOG.warn(f"--colour is only HALF APPLIED. Blender loads its colour config at startup, so")
    LOG.warn(f"the '{grading.view_name}' view cannot be switched on from inside this script.")
    LOG.warn("")
    LOG.warn("  1. close Blender")
    LOG.warn(f'  2. setx OCIO "{path}"')
    LOG.warn("  3. reopen Blender, re-import, and set Render Properties > Color Management >")
    LOG.warn(f"     View Transform to '{grading.view_name}'")
    LOG.warn("")
    LOG.warn("`setx` sets it permanently and is safe: this config is Blender's own with one view")
    LOG.warn("added, so Standard, AgX, Filmic and the rest keep working exactly as before.")
    LOG.warn("=" * 78)


def _sun_from_bundle(bundle_dir):
    """The game's own sun direction, as a Blender-frame vector pointing AT the light.

    `grading.json`'s `lighting.sun.direction` is the vector the light TRAVELS along, in the
    bundle's frame (left-handed, X east, Y up, Z north — §2.1). Blender is right-handed with Z
    up, and the importer maps bundle (x, y, z) to Blender (x, z, y) everywhere else, so the same
    mapping applies here; negating it turns "where the light goes" into "where the light is".

    Worth using rather than the arbitrary default: the placeholder tilt sits at 33 degrees, and
    this city was ripped at noon with the sun at 66. Half the terrain that reads as shadow under
    the placeholder is lit in the game, which is most of "the import looks too dark".

    Returns None when the bundle predates the lighting block, and the placeholder stands.
    """
    if not bundle_dir:
        return None
    grading = colour_model.load(bundle_dir)
    if grading is None:
        return None
    sun = (grading.raw.get("lighting") or {}).get("sun") or {}
    travel = sun.get("direction")
    if not travel or len(travel) != 3:
        return None
    toward = Vector((-float(travel[0]), -float(travel[2]), -float(travel[1])))
    if toward.length < 1e-6 or toward.z <= 0.0:
        return None                       # below the horizon: a night rip lights nothing
    return toward.normalized()


def _setup_lighting(bundle_dir=None):
    """Give the scene a plain neutral sun and ambient.

    `clear_scene()` deletes Blender's default light along with everything else, so without this
    an imported scene has no lights and no world at all — it only looks lit because the viewport
    falls back to its own studio HDRI, whose warm tint then colours everything. A white sun and a
    neutral grey world make what you see the albedo the bundle actually ships, not a studio
    lighting look. The rig is not calibrated against the game and is not meant to be: matching
    CS2's on-screen colour needs its HDR exposure and colour grading, neither of which is in the
    bundle. Set up your own lighting if you want the game's look.
    """
    scene = bpy.context.scene

    if not any(ob.type == "LIGHT" for ob in scene.objects):
        sun_data = bpy.data.lights.new("carto_sun", type="SUN")
        sun_data.color = (1.0, 1.0, 1.0)
        sun = bpy.data.objects.new("carto_sun", sun_data)
        scene.collection.objects.link(sun)
        # A Blender sun shines along its local -Z, so the direction TOWARD it is local +Z, and
        # its world Z component is the incidence cosine on level ground.
        toward = _sun_from_bundle(bundle_dir)
        if toward is not None:
            sun.rotation_euler = toward.to_track_quat("Z", "Y").to_euler()
            cos_theta = max(1e-3, toward.z)
            LOG.step(f"sun placed from the bundle: {math.degrees(math.asin(cos_theta)):.1f} deg "
                     f"above the horizon, as the game had it at rip time")
        else:
            sun.rotation_euler = _SUN_TILT
            from mathutils import Euler
            cos_theta = max(1e-3, Euler(_SUN_TILT, "XYZ").to_matrix().col[2].z)
        sun_data.energy = math.pi * (1.0 - _WORLD_LEVEL) / cos_theta

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("carto_world")
        scene.world = world
    _ensure_nodes(world)
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (_WORLD_LEVEL, _WORLD_LEVEL,
                                                    _WORLD_LEVEL, 1.0)
        background.inputs["Strength"].default_value = 1.0

    # EEVEE ships with raytracing off, so nothing reflects — water included. Turn it on; a
    # ripped city with flat, mirror-less water reads as plastic.
    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "use_raytracing"):
        eevee.use_raytracing = True

    # Make the viewport obey the scene rig instead of its warm studio HDRI, and REPORT what it
    # found. A viewport left in Solid shading draws a uniform grey clay regardless of any
    # material, which is indistinguishable from "the import produced no colour" — so say so out
    # loud rather than leaving it to be guessed at.
    seen = []
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                shading = space.shading
                if shading.type in ("SOLID", "WIREFRAME"):
                    shading.type = "MATERIAL"
                if shading.type == "MATERIAL":
                    shading.use_scene_lights = True
                    shading.use_scene_world = True
                seen.append(shading.type)

    if seen:
        LOG.step(f"viewport shading set to {', '.join(sorted(set(seen)))} "
                 f"({len(seen)} 3D view(s))")
    else:
        LOG.warn("no 3D viewport found to configure (headless run, or the .blend was opened "
                 "afterwards) — if the scene looks flat grey, set the viewport to Material "
                 "Preview or Rendered: Solid shading ignores materials entirely")


def _pin_view_transform():
    """Force the Standard view transform.

    Blender defaults to AgX, a filmic transform that rolls colour off toward a desaturated warm
    neutral. These are ripped albedo textures, not scene-referred HDR, so Standard — which
    displays the values as they are — is the honest choice for a 1:1 import.
    """
    scene = bpy.context.scene
    try:
        scene.view_settings.view_transform = "Standard"
        if hasattr(scene.view_settings, "look"):
            scene.view_settings.look = "None"
    except (AttributeError, TypeError) as exc:
        LOG.warn(f"could not pin the Standard view transform ({exc}); "
                 f"ground colour will read desaturated under AgX")


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
#
# G itself is `frames.GLTF_YUP_TO_ZUP`, where the frame conventions all live; this is the same
# matrix as a mathutils Matrix, for the call sites that stay in Blender's types.
_GLTF_TO_BLENDER = Matrix(frames.GLTF_YUP_TO_ZUP.tolist())

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


def _gltf_node_matrix_to_blender(matrix):
    """Conjugate a glTF-space node matrix by G so it lands in Blender's Z-up frame.

    `C @ M @ C.inverted()` with `C` = `_GLTF_TO_BLENDER` — a transform changes frames by
    conjugation, not by a left-multiply. Verified against a stock import of
    Address_Beach_Resort_Dubai.glb (root empty + one translated, multi-material child node):
    `matrix_world` of the matching mesh object came out identical to the float ULP.
    """
    return Matrix(glb_reader.conjugate(matrix, frames.GLTF_YUP_TO_ZUP).tolist())


# The container parse itself is `csiibundle.glb.parse_glb`: it returns per-node numpy arrays
# with the node's world matrix in glTF space, or `None` the moment anything about the file is
# a shape it has not proven it reads correctly. Every one of those refusals is deliberate, and
# this caller's answer to `None` is always the stock importer, never a guess.
_parse_glb_arrays = glb_reader.parse_glb


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
                loop_uv = glb_reader.glb_uv_to_texture_uv(node["uvs"][node["tris"].ravel()])
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

    # The bundle directory may be omitted: since 2026-08-09 the producer writes straight into
    # ModsData/Carto (the development-era spike/ subfolder is retired), so that is the default.
    # An explicit first argument that is not an option still wins.
    if argv and not argv[0].startswith("--"):
        bundle_dir = argv[0]
        argv = argv[1:]
    else:
        bundle_dir = os.path.expanduser(
            "~/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto")
        if not os.path.isdir(bundle_dir):
            print(__doc__)
            print(f"\nNo bundle directory given and the default does not exist:\n  {bundle_dir}")
            return 1
        print(f"bundle: {bundle_dir} (default; pass a directory after -- to override)")
    kinds = _UNSET
    limit = _UNSET
    center = None
    radius = None
    keep = False
    save_path = None
    do_networks = False
    do_lanes = False
    do_lane_markings = False
    do_skirts = False
    do_terrain = False
    do_areas = False
    net_categories = _UNSET
    net_limit = None
    assets = None
    everything = None
    clip_floor = None
    do_colour = False

    i = 0
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
        elif arg == "--lane-markings":
            do_lane_markings = True
            i += 1
        elif arg == "--skirts":
            do_skirts = True
            i += 1
        elif arg == "--terrain":
            do_terrain = True
            i += 1
        elif arg == "--colour" or arg == "--color":
            do_colour = True
            i += 1
        elif arg == "--clip-floor":
            do_terrain = True
            clip_floor = float(argv[i + 1])
            i += 2
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

    # Unconditionally, including under --keep, and BEFORE `--colour` gets its say: --keep is
    # exactly the path where a graded scene from a previous run would otherwise survive and make
    # the next import look like it changed something it did not.
    _pin_view_transform()
    _setup_lighting(bundle_dir)
    if do_colour:
        _apply_grading(bundle_dir)

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
            empty.matrix_world = Matrix.Translation((0.0, 0.0, _INSTANCE_DECAL_LIFT)) @ empty.matrix_world
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

    # Terrain BEFORE lanes, since 2026-08-11. It depends on neither networks nor lanes, and
    # putting it first is what lets a lane marking that finds no road under it fall back to the
    # ground rather than keeping an authored height with nothing beneath it (§8b drape). Areas
    # still follow both, as they always have.
    terrain_objects = 0
    terrain_ground = None
    if do_terrain:
        LOG.phase("Terrain & water")
        terrain_collection = bpy.data.collections.new("bundle_terrain")
        bpy.context.scene.collection.children.link(terrain_collection)
        terrain_objects, terrain_ground = build_terrain(bundle_dir, terrain_collection,
                                                        center, radius, clip_floor=clip_floor)
        LOG.end_phase()  # build_terrain() may have early-returned before its own done()

    lanes = 0
    if do_lanes or do_lane_markings:
        # Contract §5B v2: lanemeshes.jsonl carries bend parameters, laneprefabs.json the part
        # catalogue, lanemeshes/ the straight part meshes stored once. The bend runs here —
        # this importer is the contract's reference implementation of it. --lane-markings
        # bends the decal parts only; a full --lanes wins when both are given.
        if os.path.isfile(lanes_path):
            lane_collection = bpy.data.collections.new("bundle_lanemeshes")
            bpy.context.scene.collection.children.link(lane_collection)
            LOG.phase("Lane markings (lanemeshes.jsonl, decal parts)" if not do_lanes
                      else "Lane geometry (lanemeshes.jsonl)")
            road_surface = _ProjectionSurface(network_collection.objects) if networks else None
            ground_surface = (_ProjectionSurface([terrain_ground[0]])
                              if terrain_ground is not None else None)
            with parked_out_of_view_layer(lane_collection):
                lanes = build_lanes(lanes_path, lane_prefabs_path, lane_mesh_dir,
                                    lane_collection, center=center, radius=radius,
                                    limit=net_limit, road_surface=road_surface,
                                    markings_only=not do_lanes,
                                    ground_surface=ground_surface)
            LOG.end_phase()  # build_lanes() may have early-returned before its own done()
        else:
            LOG.warn(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")

    area_objects = 0
    if do_areas:
        LOG.phase("Surface areas")
        area_collection = bpy.data.collections.new("bundle_areas")
        bpy.context.scene.collection.children.link(area_collection)
        # Areas are painted onto the terrain, so they need it built first — it is, above.
        ground_surface, ground_cell = (None, 3.5)
        if terrain_ground is not None:
            ground_surface = _ProjectionSurface([terrain_ground[0]])
            ground_cell = terrain_ground[1]
        area_objects = build_areas(bundle_dir, area_collection, center, radius,
                                   ground=ground_surface, cell=ground_cell)
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

    # After every glTF import, because that importer loads its own textures and leaves them
    # premultiplying colour by alpha. See `_unpremultiply` — this is worth several stops.
    repacked = _unpremultiply_all()
    if repacked:
        LOG.step(f"{repacked} texture(s) set to channel-packed alpha so their colour is not "
                 f"multiplied by packed alpha data")

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
