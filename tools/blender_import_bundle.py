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

`bundle_dir` may be omitted: it defaults to wherever `csiibundle.bake.locate` says this
machine's bundle is — Windows, Proton or macOS, with `CS2_BUNDLE_DIR` overriding
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
    --sky                        light the scene with a physical sky at the bundle's OWN sun
                                 elevation and azimuth (grading.json `lighting.sun`) instead of
                                 the flat grey world. Off by default because the default rig is
                                 calibrated so flat ground renders at exactly its albedo; a sky
                                 breaks that identity. Turn it on to see REFLECTIONS — water at
                                 roughness 0.05 mirrors its environment faithfully, and a
                                 uniform grey environment mirrors as uniform grey, which reads
                                 as "the ocean has no reflection"
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
    --coarse-terrain             diagnostic: mesh the 3.5 m base heightmap even when the bundle
                                 carries layers.terrainFine (the "before" side of a fine A/B)
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
import sys
import time

import bpy
from mathutils import Vector

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

# The Blender half of the reconstruction, one module per concern. Same bootstrap: this
# package sits beside the script, and Blender does not put a script's folder on the path.
from blenderbundle import (
    frame,
    glbimport,
    lanebuild,
    materiallayer,
    netbuild,
    raysurface,
    sceneprep,
    shadernodes,
    survey,
    terrainbuild,
)
from blenderbundle.log import LOG
from csiibundle import areas as area_math
from csiibundle import (
    frames,
    instances,
    materials,
    networks,
    reader,
)

# The scene light rig. `sceneprep.clear_scene()` removes Blender's default light along with everything
# else, so an imported scene would have no lights and no world at all; these put a plain
# neutral sun and ambient back. White, and deliberately uncalibrated -- the bundle ships
# albedo, and the importer reproduces it 1:1 rather than grading it toward the game's look.
#
# `--colour` never touches this rig or any material: the whole grading chain rides Blender's
# display pipeline instead, for the reasons `_apply_grading` sets out.
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


# -- bundle reading -------------------------------------------------------------------------
#
# All of it is `csiibundle.reader`. The aliases keep the call sites below reading the way they
# always have; `resolve_layers` and `_terrain_paths` return namedtuples whose field order is
# the tuple order these call sites unpack.

# Sentinel for an option `--everything` may supply a default for but an explicit flag wins.
_UNSET = object()

read_jsonl = reader.read_jsonl
read_declared_gaps = reader.read_declared_gaps
resolve_layers = reader.resolve_layers


# The §8b conformance rules are `csiibundle.areas` — an area triangle is a FOOTPRINT the game
# projects onto the terrain, never a plane, and its docstring says what follows from that.
_AREA_UV_SCALE_FALLBACK = area_math.UV_SCALE_FALLBACK


def build_areas(bundle_dir, collection, center, radius, ground=None, cell=3.5):
    """Place the §8b surface areas: driveways, parking-lot pavement, park paths.

    These are the painted patches `AreaBatchSystem` draws from area triangles — the visible
    ground of every meshless prefab (parking lots, sports courts, campsites) and of house
    driveways. One object per area prefab, world-anchored UVs, catalogue texture or colour.

    `ground` is a `_ProjectionSurface` over the terrain grid and `cell` its spacing. The game
    paints an area onto the terrain rather than laying a plane on it (audit §30), so with a
    ground to conform to, each triangle is split until it follows — see
    `area_math.conform_triangle`. Without one (`--areas` and no `--terrain`) the flat triangles
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

    area_materials = {}
    area_packings = {}

    def area_material(prefab):
        """The §8b surface as the PBR material the catalogue actually describes.

        `areaprefabs.json` carries `normalMap`, `maskMap`, `metallic` and `smoothness` beside
        the diffuse; before it did, pavement and plazas rendered flat-lit with guessed specular
        (audit #40 P11). The maskMap packs §7's channels, so roughness is `1 - smoothness` and
        metallic is R — the same two `csiibundle.materials` wires for object materials, and for
        the same reason.
        """
        if prefab in area_materials:
            return area_materials[prefab]
        surface = area_math.surface_material(catalogue.get(prefab))
        colour = surface["color"] or (0.42, 0.40, 0.38, 1.0)
        material = shadernodes.ground_material(
            f"area_{prefab}",
            os.path.join(bundle_dir, surface["texture"]) if surface["texture"] else None,
            (colour[0], colour[1], colour[2], 1.0),
        )
        tree = material.node_tree
        bsdf = tree.nodes.get("Principled BSDF")

        # Scalars first, so an entry with no maskMap still gets its authored values instead of
        # the generic ground roughness.
        if surface["metallic"] is not None:
            bsdf.inputs["Metallic"].default_value = surface["metallic"]
        if surface["roughness"] is not None:
            bsdf.inputs["Roughness"].default_value = surface["roughness"]

        normal_path = (
            os.path.join(bundle_dir, surface["normal_map"]) if surface["normal_map"] else None
        )
        if normal_path and os.path.isfile(normal_path):
            tex = tree.nodes.new("ShaderNodeTexImage")
            tex.image = shadernodes.unpremultiply(
                bpy.data.images.load(normal_path, check_existing=True)
            )
            tex.image.colorspace_settings.name = "Non-Color"  # set BEFORE sampling the pixels

            # An area normal map is not reliably three-channel. Feeding a packed one straight
            # into a Normal Map node points every shading normal sideways or into the surface,
            # and the surface renders BLACK — see `materials.detect_normal_packing`. The
            # producer rebuilds these to §7.1 as of this change; until a bundle is re-ripped,
            # this is what keeps the ground lit.
            packing = materials.detect_normal_packing(shadernodes.sample_rgba(tex.image))
            area_packings[packing] = area_packings.get(packing, 0) + 1

            nmap = tree.nodes.new("ShaderNodeNormalMap")  # CS2 authors OpenGL +Y, as Blender wants
            tree.links.new(
                shadernodes.tangent_normal_colour(tree, tex, packing, bsdf.location),
                nmap.inputs["Color"],
            )
            tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

        mask_path = os.path.join(bundle_dir, surface["mask_map"]) if surface["mask_map"] else None
        if mask_path and os.path.isfile(mask_path):
            tex = tree.nodes.new("ShaderNodeTexImage")
            tex.image = shadernodes.unpremultiply(
                bpy.data.images.load(mask_path, check_existing=True)
            )
            tex.image.colorspace_settings.name = "Non-Color"
            sep = tree.nodes.new("ShaderNodeSeparateColor")
            tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
            tree.links.new(sep.outputs["Red"], bsdf.inputs["Metallic"])
            invert = tree.nodes.new("ShaderNodeMath")
            invert.operation = "SUBTRACT"
            invert.inputs[0].default_value = 1.0
            tree.links.new(tex.outputs["Alpha"], invert.inputs[1])  # A is smoothness
            tree.links.new(invert.outputs[0], bsdf.inputs["Roughness"])

        area_materials[prefab] = material
        return material

    # The ground each area is painted onto (audit §30), memoised — the conformance re-samples
    # the same points constantly. Without the terrain layer there is nothing to conform to and
    # the flat triangles are all this can draw.
    ground_at = area_math.memoised_sampler(ground)

    # The whole loop is `csiibundle.areas.build_pieces`: region filter, frame change, winding
    # reversal, and the adaptive split. What comes back is Z-up triangle soup per prefab.
    builders, stats = area_math.build_pieces(
        read_jsonl(path),
        cell,
        ground_at,
        center=center,
        radius=radius,
        progress=lambda n: LOG.progress("areas conformed", n, None),
        catalogue=catalogue,
    )
    placed = stats["placed"]
    skipped_region = stats["skipped_region"]
    dropped_bad = stats["dropped_bad"]
    missed = stats["missed"]
    source_tris = stats["source_tris"]
    painted_area = stats["painted_area"]
    counters = stats["counters"]

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
        uv_scale = float((catalogue.get(prefab) or {}).get("uvScale") or _AREA_UV_SCALE_FALLBACK)
        for loop in mesh.loops:
            x, y, _ = mesh.vertices[loop.vertex_index].co
            uv_layer.data[loop.index].uv = (x * uv_scale, y * uv_scale)
        mesh.validate()
        obj = bpy.data.objects.new(f"area_{prefab}", mesh)
        collection.objects.link(obj)
        built += 1

    LOG.done(
        f"areas:     {placed} surface area(s) into {built} object(s) "
        f"over {len(builders)} prefab(s)"
        + (f", {skipped_region} outside the region" if skipped_region else "")
    )

    # Say it out loud when a bundle ships normal maps the contract does not describe: rebuilt
    # here they light correctly, but the fix belongs in the producer, and a silent rebuild is
    # how a consumer written against §7.1 alone inherits black ground without ever knowing why.
    packed = {k: v for k, v in area_packings.items() if k != materials.NORMAL_RGB}
    if packed:
        LOG.warn(
            f"{sum(packed.values())} of {sum(area_packings.values())} area normal map(s) "
            f"are two-channel packed ({', '.join(f'{n}x {k}' for k, n in sorted(packed.items()))}) "
            "rather than the tangent RGB of contract §7.1 — rebuilt here (they would "
            "otherwise shade black); re-rip to ship them unpacked"
        )

    # A prefab with neither texture nor colour renders the neutral grey fallback — say why,
    # per prefab, instead of leaving grey boxes as a riddle. `unresolved` is the producer's
    # flag for "the save references an asset (pack) that was not loaded in the rip session":
    # the game itself drew those areas grey (Missing Area fallback), and the cure is enabling
    # the pack in-game and re-ripping, not anything on this side.
    grey = [
        prefab
        for prefab in builders
        if not (catalogue.get(prefab) or {}).get("texture")
        and not (catalogue.get(prefab) or {}).get("color")
    ]
    if grey:
        unresolved = [p for p in grey if (catalogue.get(p) or {}).get("unresolved")]
        for prefab in sorted(unresolved):
            name = (catalogue.get(prefab) or {}).get("name", prefab)
            LOG.warn(
                f"area prefab did not resolve at rip time (asset pack not loaded "
                f"or version mismatch): {name}"
            )
        if unresolved:
            LOG.warn(
                f"{len(unresolved)} area prefab(s) render grey because their asset "
                "was missing in the rip session — the game drew them grey too "
                "(Missing Area); enable the packs, load the save, re-rip"
            )
        for prefab in sorted(set(grey) - set(unresolved)):
            LOG.warn(f"area prefab carries no colour and no texture: {prefab}")
    if ground is None:
        LOG.warn(
            "terrain layer not built — surface areas stay flat, expect the ground to "
            "cut through pavement on any slope (audit §30)"
        )
    else:
        total = sum(len(sink["faces"]) for sink in builders.values())
        LOG.step(
            f"{int(source_tris)} area triangle(s) conformed to the terrain as "
            f"{total} piece(s)" + (f", {missed} node(s) found no ground" if missed else "")
        )
        if counters["unconformed"]:
            share = 100.0 * counters["unconformed_area"] / max(painted_area, 1e-9)
            LOG.step(
                f"{counters['unconformed_area']:.0f} m2 of {painted_area:.0f} m2 painted "
                f"({share:.2f}%) stayed unconformed — ground up to "
                f"{counters['worst'] * 100:.0f} cm above it, over "
                f"{counters['unconformed']} piece(s)"
            )
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
        LOG.warn(
            "--colour: this bundle has no grading.json (ripped before the grading layer); "
            "leaving the scene ungraded"
        )
        return

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
        LOG.warn(
            f"--colour: this Blender has no '{view}' view transform; leaving "
            f"{scene.view_settings.view_transform}"
        )

    # The per-channel gain rides the lights when it cannot ride the view. Scaling every light per
    # channel scales the whole render per channel, bounces included, so this is exact rather than
    # an approximation — but it is only reached on the fallback path.
    if any(abs(c - 1.0) > 1e-6 for c in gain):
        for ob in scene.objects:
            if ob.type == "LIGHT":
                ob.data.color = (gain[0], gain[1], gain[2])
        background = (
            scene.world.node_tree.nodes.get("Background")
            if scene.world and scene.world.node_tree
            else None
        )
        if background is not None:
            base = background.inputs["Color"].default_value
            background.inputs["Color"].default_value = (
                base[0] * gain[0],
                base[1] * gain[1],
                base[2] * gain[2],
                1.0,
            )

    LOG.step(
        f"grading applied from grading.json: view transform {view}, "
        f"exposure {exposure:+.2f} EV"
        + (f", gamma {gamma:.4g}" if gamma != 1.0 else "")
        + (
            f", light gain {tuple(round(c, 4) for c in gain)}"
            if any(abs(c - 1.0) > 1e-6 for c in gain)
            else ""
        )
    )
    for line in grading.warnings:
        LOG.warn("--colour: " + line)

    if available:
        LOG.step(
            f"the '{grading.view_name}' view is carrying the whole chain — gain, contrast, "
            f"saturation and shadows/midtones/highlights all run on final radiance, in the "
            f"viewport"
        )
        return

    for line in missing:
        LOG.warn("--colour: NOT applied - " + line)
    config = colour_model.blender_config_path()
    if config is None:
        LOG.warn(
            "--colour: could not find Blender's own config.ocio to extend; set "
            "CARTO_BLENDER_COLORMANAGEMENT to the folder holding it"
        )
        return
    try:
        path = colour_model.write_ocio(grading, os.path.join(bundle_dir, "colour"), config)
    except OSError as exc:
        LOG.warn(f"--colour: could not write the OCIO config ({exc})")
        return

    # LOUD, because this is the one step a person can silently skip and then judge the whole
    # feature by the result. Without the config Blender has no way to run a cross-channel
    # operator before the display transform, so the scene loses the saturation entirely and
    # reads flat and dark — which looks exactly like "the grading does not work".
    LOG.warn("=" * 78)
    LOG.warn("--colour is only HALF APPLIED. Blender loads its colour config at startup, so")
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
    toward = grading.lighting.sun.toward  # None below the horizon: a night rip lights nothing
    if toward is None:
        return None
    return Vector(frames.bundle_to_zup_point(toward)).normalized()


# Set by `--sky`. Off by default ON PURPOSE: the default rig is a uniform grey world plus a sun
# whose energy is DERIVED so that flat, fully-lit ground renders at exactly its albedo, which is
# what makes "scene-linear" and "albedo" the same number and is the assumption
# `tools/colour_model.py` works under. A sky replaces that uniform ambient with a directional
# one and breaks the identity — worth it to see reflections, wrong as a default for measurement.
_SKY = False

# Mean radiance of Blender's atmosphere model over the UPPER hemisphere, measured with a 128x64
# equirectangular Cycles render at the reference elevation below (`--sky` normalises against it;
# see `_build_sky`). Re-measure with the same render if the sky model changes.
_SKY_REFERENCE_MEAN = 14.28
_SKY_REFERENCE_ELEVATION = 66.263


def _build_sky(world, background, bundle_dir):
    """Replace the flat grey world with a physical sky at the bundle's OWN sun position.

    **Why this exists: a mirror needs something to be a mirror OF.** Water at roughness 0.05
    reflects its environment faithfully, and a uniform grey environment reflects as uniform grey
    — which reads as "the ocean has no reflection". The fix is not to change the water.

    What is exact and what is not, in the manner of `csiibundle.colour`:

    * the sun's ELEVATION and AZIMUTH are the game's own, from `grading.json`'s
      `lighting.sun` — so the sky's gradient, its horizon glow and the specular glint on water
      all sit where the game put them.
    * the sky MODEL is Blender's, not CS2's. The game uses HDRP's PhysicallyBasedSky and ships
      only its three tints and an exposure, not a sky texture, so an atmosphere model driven by
      the right sun is the closest honest reconstruction. `groundTint` is applied as the
      below-horizon colour, which is the one tint that is not white on the reference bundle.
    * `sky.exposure` is applied as a strength multiplier on the result.
    """
    grading = colour_model.load(bundle_dir) if bundle_dir else None
    lighting = grading.lighting if grading is not None else None

    tree = world.node_tree
    sky = tree.nodes.new("ShaderNodeTexSky")
    available = sky.bl_rna.properties["sky_type"].enum_items.keys()
    for preferred in ("MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "HOSEK_WILKIE", "PREETHAM"):
        if preferred in available:
            sky.sky_type = preferred
            break

    if lighting is not None and lighting.sun.elevation_degrees is not None:
        sky.sun_elevation = math.radians(float(lighting.sun.elevation_degrees))
        # The bundle's azimuth is degrees clockwise from -Z toward the light; Blender's
        # `sun_rotation` is counter-clockwise from +Y, and bundle -Z maps to Blender -Y.
        sky.sun_rotation = math.radians(180.0 - float(lighting.sun.azimuth_degrees or 0.0))
    if hasattr(sky, "sun_intensity"):
        # The disc itself is the sun LAMP's job; leaving it in double-counts the key light.
        sky.sun_intensity = 0.0

    tree.links.new(sky.outputs["Color"], background.inputs["Color"])

    # NORMALISE, or the sky bleaches the scene. Blender's atmosphere model emits physical
    # radiance: measured with a 128x64 equirectangular render at this bundle's own sun
    # elevation, its mean over the UPPER hemisphere is 14.28, against the flat rig's
    # _WORLD_LEVEL of 0.25 — 57x. Left alone it blows out every surface and the whole city
    # renders pale blue, which is what the first attempt at this did.
    #
    # So the sky is scaled to deliver the SAME mean ambient the grey world did. That keeps
    # `_sun_energy_for`'s derivation intact — flat lit ground still renders at its albedo — and
    # changes only the DIRECTIONALITY of the ambient, which is the whole point: a mirror needs
    # something to be a mirror of.
    #
    # The elevation term is MODELLED, not measured: sky radiance falls roughly with the sine of
    # the sun's height, so the reference measurement is scaled by sin(elev)/sin(reference). At
    # this bundle's own elevation it is exact by construction; at another time of day it is
    # approximate, which is acceptable for an ambient rig whose job is to be reflected.
    elevation = getattr(sky, "sun_elevation", math.radians(_SKY_REFERENCE_ELEVATION))
    reference = math.sin(math.radians(_SKY_REFERENCE_ELEVATION))
    scale = max(math.sin(elevation), 0.05) / reference
    strength = _WORLD_LEVEL / max(_SKY_REFERENCE_MEAN * scale, 1e-6)
    if lighting is not None:
        strength *= 2.0 ** float(lighting.sky.exposure)
    background.inputs["Strength"].default_value = strength
    LOG.step(
        f"sky:       physical sky at the bundle's own sun ({math.degrees(elevation):.1f}° "
        f"elevation), normalised to the rig's {_WORLD_LEVEL:g} ambient (strength "
        f"{strength:.4f}) so reflections gain a horizon without changing exposure"
    )
    return sky


def _setup_lighting(bundle_dir=None):
    """Give the scene a plain neutral sun and ambient.

    `sceneprep.clear_scene()` deletes Blender's default light along with everything else, so without this
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
            LOG.step(
                f"sun placed from the bundle: {math.degrees(math.asin(cos_theta)):.1f} deg "
                f"above the horizon, as the game had it at rip time"
            )
        else:
            sun.rotation_euler = _SUN_TILT
            from mathutils import Euler

            cos_theta = max(1e-3, Euler(_SUN_TILT, "XYZ").to_matrix().col[2].z)
        sun_data.energy = math.pi * (1.0 - _WORLD_LEVEL) / cos_theta

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("carto_world")
        scene.world = world
    shadernodes.ensure_nodes(world)
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (_WORLD_LEVEL, _WORLD_LEVEL, _WORLD_LEVEL, 1.0)
        background.inputs["Strength"].default_value = 1.0

    if _SKY and background is not None:
        _build_sky(world, background, bundle_dir)

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
        LOG.step(f"viewport shading set to {', '.join(sorted(set(seen)))} ({len(seen)} 3D view(s))")
    else:
        LOG.warn(
            "no 3D viewport found to configure (headless run, or the .blend was opened "
            "afterwards) — if the scene looks flat grey, set the viewport to Material "
            "Preview or Rendered: Solid shading ignores materials entirely"
        )


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []

    # The bundle directory may be omitted: since 2026-08-09 the producer writes straight into
    # ModsData/Carto (the development-era spike/ subfolder is retired), so that is the default.
    # An explicit first argument that is not an option still wins.
    if argv and not argv[0].startswith("--"):
        bundle_dir = argv[0]
        argv = argv[1:]
    else:
        # Not spelled out here: `locate` knows Windows, Proton and macOS and honours
        # `CS2_BUNDLE_DIR`, and this was one of four places that had its own Windows-only copy.
        from csiibundle.bake.locate import default_bundle_dir

        bundle_dir = default_bundle_dir()
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
    coarse_terrain = False
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
        elif arg == "--sky":
            global _SKY
            _SKY = True
            i += 1
        elif arg == "--clip-floor":
            do_terrain = True
            clip_floor = float(argv[i + 1])
            i += 2
        elif arg == "--coarse-terrain":
            coarse_terrain = True
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
            net_categories = (
                None if value == "all" else {c.strip() for c in value.split(",") if c.strip()}
            )
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
            kinds = set(networks.KINDS)
        if net_categories is _UNSET:
            net_categories = None  # every category
        if limit is _UNSET:
            limit = None  # the radius is the limit
        if center is None:
            center = "downtown"
        if radius is None:
            radius = everything

    if kinds is _UNSET:
        kinds = {"building"}
    if net_categories is _UNSET:
        net_categories = set(networks.DRIVABLE)
    if limit is _UNSET:
        limit = 20000

    (
        mesh_dir,
        instances_path,
        networks_path,
        net_mesh_dir,
        lanes_path,
        lane_mesh_dir,
        lane_prefabs_path,
    ) = resolve_layers(bundle_dir)
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
        LOG.step(
            f"           categories: {'all' if net_categories is None else sorted(net_categories)}"
        )

    LOG.phase("Instances")
    glbimport.counts_reset()

    # Always survey before placing. Knowing where the selection is turns "nothing appeared" from
    # a mystery into an arithmetic error the user can see.
    where = survey.locate(instances_path, kinds) if kinds else None
    if kinds and where is None:
        LOG.warn(f"no instances of kinds {sorted(kinds)} in this bundle")
        return 2

    if where is not None:
        min_x, max_x, min_z, max_z = where["bounds"]
        LOG.step(f"selected:  {where['count']} instance(s)")
        LOG.step(f"           X {min_x:.0f} .. {max_x:.0f}   Z {min_z:.0f} .. {max_z:.0f}")
        LOG.step(
            f"           centroid X {where['centroid'][0]:.0f} Z {where['centroid'][1]:.0f}"
            f"   densest 250 m cell X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f}"
            f" ({where['densest_count']} there)"
        )

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
        skyline = survey.locate(instances_path, {"building"}, weight=survey.skyline_weight)
        if skyline is None:
            LOG.warn(
                "--center downtown needs buildings in the bundle; "
                "use --center auto, or give explicit X Z"
            )
            return 2
        center = skyline["densest"]
        LOG.step(
            f"           --center downtown resolved to X {center[0]:.0f} Z {center[1]:.0f}"
            f"   (tallest 250 m cell of {skyline['count']} building(s))"
        )

    if center == "interchange":
        found = survey.interchange(instances_path, networks_path)
        if found is None:
            LOG.warn(
                "--center interchange needs highway or elevated network records with buildings "
                "near them; use --center downtown, or give explicit X Z"
            )
            return 2
        center = found
        LOG.step(
            f"           --center interchange resolved to X {center[0]:.0f} Z {center[1]:.0f}"
            f"   (tallest skyline with a highway junction in it)"
        )

    if radius is not None:
        if center is None:
            LOG.warn("--radius needs --center (or --center auto)")
            return 2
        LOG.step(f"region:    within {radius} m of X {center[0]:.0f} Z {center[1]:.0f}")

    LOG.step(f"declared gaps in warnings.jsonl: {len(declared_gaps)}")
    materiallayer.report_materials(bundle_dir)

    if not keep:
        sceneprep.clear_scene()

    # Unconditionally, including under --keep, and BEFORE `--colour` gets its say: --keep is
    # exactly the path where a graded scene from a previous run would otherwise survive and make
    # the next import look like it changed something it did not.
    sceneprep.pin_view_transform()
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
    decal_park = sceneprep.parked_out_of_view_layer(decal_collection)
    decal_park.__enter__()
    instance_park = sceneprep.parked_out_of_view_layer(placed_collection)
    sources_park = sceneprep.parked_out_of_view_layer(sources)
    instance_park.__enter__()
    sources_park.__enter__()

    prefabs = {}
    skirt_prefabs = {}
    placed = 0
    skirts_placed = 0
    skipped_kind = 0
    skipped_region = 0
    skipped_asset = 0
    skipped_state = 0
    missing = {}
    started = time.time()

    for record in read_jsonl(instances_path) if kinds else ():
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

        # §4 D4: `state: "construction"` means the game draws NO building here — the site's
        # crane and fence are separate records the bundle already carries.
        if not instances.should_draw(record):
            skipped_state += 1
            continue

        # §6.1/§9: a `.glb` is ONE STATE of a prefab, and this record says which state it is
        # in. `mesh_candidates` orders the derived files best-first (a tree's `__elderly`, a
        # stockpile's `__partial2`) with the base state last, and a variant file exists only
        # where the prefab flags that state — so this takes the first one that is actually on
        # disk. Without it 153,991 of this city's 697,256 trees draw as healthy adults.
        asset_id = record["assetId"]
        for candidate in instances.mesh_candidates(record):
            if candidate not in prefabs:
                path = os.path.join(mesh_dir, candidate + ".glb")
                prefabs[candidate] = (
                    glbimport.load_prefab_collection(candidate, mesh_dir, sources)
                    if os.path.isfile(path)
                    else None
                )
            if prefabs[candidate] is not None:
                asset_id = candidate
                break
        else:
            reason = declared_gaps.get(asset_id, "NOT DECLARED in warnings.jsonl")
            missing[asset_id] = reason

        collection = prefabs[asset_id]
        if collection is None:
            continue

        empty = bpy.data.objects.new(f"{asset_id}.{record.get('id', placed)}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = collection
        empty.empty_display_size = 0.01
        empty.matrix_world = frame.instance_matrix(record)
        materiallayer.set_paint(empty, record.get("attrs"))

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
        if do_skirts and instances.wants_skirt(record):
            base_id = instances.skirt_asset_id(record)
            if base_id not in skirt_prefabs:
                base_path = os.path.join(mesh_dir, base_id + ".glb")
                skirt_prefabs[base_id] = (
                    glbimport.load_prefab_collection(base_id, mesh_dir, sources)
                    if os.path.isfile(base_path)
                    else None
                )
            if skirt_prefabs[base_id] is not None:
                skirt = bpy.data.objects.new(f"{base_id}.{record.get('id', placed)}", None)
                skirt.instance_type = "COLLECTION"
                skirt.instance_collection = skirt_prefabs[base_id]
                skirt.empty_display_size = 0.01
                skirt.matrix_world = frame.instance_matrix(record)
                materiallayer.set_paint(skirt, record.get("attrs"))
                placed_collection.objects.link(skirt)
                skirts_placed += 1

        if record.get("kind") == "decal":
            # Coplanar paint z-fights the carriageway it sits on, which in the viewport looks
            # like the marking is flickering in and out of the road. A lift far below the
            # tolerance of anything downstream costs nothing and settles it.
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
    glbimport.counts_log()

    network_count = 0
    network_collection = None
    if do_networks:
        LOG.phase("Networks")
        network_collection = bpy.data.collections.new("bundle_networks")
        bpy.context.scene.collection.children.link(network_collection)
        with sceneprep.parked_out_of_view_layer(network_collection):
            network_count = netbuild.build_networks(
                networks_path,
                net_categories,
                network_collection,
                net_mesh_dir,
                center=center,
                radius=radius,
                limit=net_limit,
            )
        LOG.end_phase()  # netbuild.build_networks() may have early-returned before its own done()

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
        terrain_objects, terrain_ground = terrainbuild.build_terrain(
            bundle_dir,
            terrain_collection,
            center,
            radius,
            clip_floor=clip_floor,
            coarse=coarse_terrain,
        )
        LOG.end_phase()  # terrainbuild.build_terrain() may have early-returned before its own done()

    lane_count = 0
    if do_lanes or do_lane_markings:
        # Contract §5B v2: lanemeshes.jsonl carries bend parameters, laneprefabs.json the part
        # catalogue, lanemeshes/ the straight part meshes stored once. The bend runs here —
        # this importer is the contract's reference implementation of it. --lane-markings
        # bends the decal parts only; a full --lanes wins when both are given.
        if os.path.isfile(lanes_path):
            lane_collection = bpy.data.collections.new("bundle_lanemeshes")
            bpy.context.scene.collection.children.link(lane_collection)
            LOG.phase(
                "Lane markings (lanemeshes.jsonl, decal parts)"
                if not do_lanes
                else "Lane geometry (lanemeshes.jsonl)"
            )
            road_surface = (
                raysurface.ProjectionSurface(network_collection.objects) if network_count else None
            )
            ground_surface = (
                raysurface.ProjectionSurface([terrain_ground[0]])
                if terrain_ground is not None
                else None
            )
            with sceneprep.parked_out_of_view_layer(lane_collection):
                lane_count = lanebuild.build_lanes(
                    lanes_path,
                    lane_prefabs_path,
                    lane_mesh_dir,
                    lane_collection,
                    center=center,
                    radius=radius,
                    limit=net_limit,
                    road_surface=road_surface,
                    markings_only=not do_lanes,
                    ground_surface=ground_surface,
                )
            LOG.end_phase()  # lanebuild.build_lanes() may have early-returned before its own done()
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
            ground_surface = raysurface.ProjectionSurface([terrain_ground[0]])
            ground_cell = terrain_ground[1]
        area_objects = build_areas(
            bundle_dir, area_collection, center, radius, ground=ground_surface, cell=ground_cell
        )
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
    masked = materiallayer.apply_pbr_masks(bundle_dir)
    shader_water, shader_glass, shader_tint = materiallayer.apply_shader_only_materials(bundle_dir)
    if masked:
        LOG.step(
            f"pbr:       {masked} material(s) took metallic + roughness from their §7 mask "
            "(the .glb ships neither — it hard-codes roughness 0.9)"
        )
    if shader_water or shader_glass or shader_tint:
        LOG.step(
            f"shaders:   {shader_water} water, {shader_glass} glass, {shader_tint} tinted "
            "material(s) given an appearance — CS2 draws these with a shader and no "
            "texture, so the .glb leaves them white"
        )
    tinted = materiallayer.apply_color_tints(bundle_dir)
    if tinted:
        LOG.step(
            f"tinted:    {tinted} material(s) wired to per-instance colorSet via their colorMask"
        )

    # The scratch collection is empty by construction — every import unlinks what it produced —
    # so drop it rather than leave an empty entry in the outliner.
    scratch = bpy.data.collections.get(glbimport.SCRATCH_NAME)
    if scratch is not None:
        if scratch.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(scratch)
        bpy.data.collections.remove(scratch)

    # Hide the templates from the view layer without unlinking them; instances still resolve.
    layer_child = bpy.context.view_layer.layer_collection.children.get(sources.name)
    if layer_child is not None:
        layer_child.exclude = True

    sceneprep.show_materials()

    # Aim at whatever there is to look at. With `--kinds none --networks` the instance
    # collection is empty, and aiming at an empty collection leaves the viewport at the origin
    # — which is where a city almost never is.
    sceneprep.aim_viewports(
        placed_collection
        if placed
        else (
            network_collection
            if network_count
            else (lane_collection if lane_count else placed_collection)
        )
    )

    elapsed = time.time() - started
    # No phase is open at this point — Instances closed right after its loop, and whichever
    # of Networks/Lanes/Terrain/Areas ran last closed itself too — so LOG.done() adds no
    # elapsed suffix of its own here; `elapsed` above (measured from before the instance
    # loop, across the whole pipeline) is the only timing this line carries, exactly as
    # before _Log.
    LOG.done(f"placed {placed} instance(s) from {len(prefabs)} prefab(s) in {elapsed:.1f} s")

    # After every glTF import, because that importer loads its own textures and leaves them
    # premultiplying colour by alpha. See `_unpremultiply` — this is worth several stops.
    repacked = shadernodes.unpremultiply_all()
    if repacked:
        LOG.step(
            f"{repacked} texture(s) set to channel-packed alpha so their colour is not "
            f"multiplied by packed alpha data"
        )

    if do_skirts:
        LOG.step(
            f"placed {skirts_placed} foundation skirt(s) (__base, as-authored 1 m bands — "
            f"the terrain drop is the consumer rule, contract §4.1)"
        )
    if do_terrain:
        LOG.step(f"placed {terrain_objects} terrain object(s) (ground grid + water; §8)")
    if do_areas:
        LOG.step(f"placed {area_objects} surface-area object(s) (driveways, lots, paths; §8b)")
    LOG.step(
        f"skipped: {skipped_kind} by kind, {skipped_asset} by --assets, {skipped_region} outside the region"
    )

    # An empty-looking scene is nearly always the region filter, not the bundle. Say so, with the
    # coordinates that would have worked. The absolute floor matters as much as the ratio: a
    # deliberate 500 m window on a 14 km city keeps well under a twentieth of it by design, and
    # tens of thousands of instances is not an empty scene — pointing that run at `--center auto`
    # would have moved it from downtown to whichever field has the most trees.
    if radius is not None and where is not None and placed < where["count"] / 20 and placed < 500:
        LOG.warn(
            f"NOTE: the region kept {placed} of {where['count']} selected instance(s). "
            f"The densest 250 m cell is at X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f} — "
            f"try --center auto, --center downtown, a larger --radius, or drop them."
        )

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
