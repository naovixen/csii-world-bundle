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

import math
import os
import sys
import time

import bpy

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

# The Blender half of the reconstruction, one module per concern. Same bootstrap: this
# package sits beside the script, and Blender does not put a script's folder on the path.
from blenderbundle import (
    areabuild,
    colourgrade,
    frame,
    glbimport,
    lanebuild,
    lighting,
    materiallayer,
    netbuild,
    raysurface,
    sceneprep,
    shadernodes,
    survey,
    terrainbuild,
)
from blenderbundle.log import LOG
from csiibundle import (
    instances,
    networks,
    reader,
)

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


# -- scene building -------------------------------------------------------------------------


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
    do_sky = False

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
            do_sky = True
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
    lighting.setup_lighting(bundle_dir, sky=do_sky)
    if do_colour:
        colourgrade.apply_grading(bundle_dir)

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
        area_objects = areabuild.build_areas(
            bundle_dir, area_collection, center, radius, ground=ground_surface, cell=ground_cell
        )
        LOG.end_phase()  # areabuild.build_areas() may have early-returned before its own done()

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
