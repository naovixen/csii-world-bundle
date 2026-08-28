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

import os
import sys

# The engine-neutral half of the reconstruction lives in `tools/csiibundle/`, a package that
# never imports bpy: the frame conversion, the GLB and PNG readers, the splat decode and the
# grading derivation are all things a BeamNG builder or a Blender adapter needs identically, and
# keeping one copy is what stops two of them drifting apart (the last attempt at the grading had
# two calibrations that "converged" on different transforms). The Blender half is
# `tools/blenderbundle/`, one module per concern; this file is the entry point over the two.
#
# Blender does not put a script's own folder on the path when it is run with `--python`, so add
# it explicitly before importing either.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blenderbundle import run

if __name__ == "__main__":
    sys.exit(run.main(__doc__))
