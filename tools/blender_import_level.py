"""Reconstruct a cs2beamng BeamNG level in Blender — the consumer-side oracle.

    blender --background --python tools/blender_import_level.py -- --level <levels/cs2_city> \
        --center <X> <Y> --radius 250 [--products networks,markings,lanegeometry,holewalls] \
        [--no-terrain] [--no-forest] [--forest-max N] [--ter other.ter] --save out.blend

Reads what BeamNG reads — the level folder, nothing from the bundle — so a render answers
"what will the game draw here" for the parts Blender can show: the `.ter` heightfield with its
holes and per-sample material colour, every `TSStatic` `.dae` tile whose footprint meets the
region (Blender 5 ships no COLLADA importer, so `read_dae` here is a small general COLLADA 1.4
triangles reader — sources/accessors, `<vertices>`, per-input offsets, node matrices), and
the level's v1.5 materials wired as Principled BSDF: `baseColorMap` (+ `opacityMap` as alpha,
`alphaTest`/`translucent` → blend), `diffuseColor` when there is no map. Center/radius are
BeamNG world x/y (= bundle x/z). Terrain material colours are flat stand-ins per ground name.

**Forest items too** (A3): `forest/*.forest4.json` + `art/forest/managedItemData.json`, one mesh
datablock per type and a linked duplicate per item. Without them the oracle shows an empty city,
because a forest item is referenced by no `TSStatic`. Only the NEAREST detail level of a shape is
imported - a shape with an LOD ladder writes one node per level.

Companion of `tools/blender_import_bundle.py` (the producer-side truth): import the same
region from both and compare — the difference is the app's doing. Frame: BeamNG is Z-up
right-handed like Blender, so nothing is transformed beyond each tile's `position`.
"""

import os
import sys
import time

import bpy

# Blender ships its own Python, which has never had `pip install -e .` run into it, so neither
# `tools/` nor `apps/` is importable here by default and neither can be. The oracle reads what
# BeamNG reads through `cs2beamng.beamng` — the level, the `.ter` and the `.dae` reader that
# sits beside the writer — so both roots go on the path before anything is imported.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
sys.path.insert(0, HERE)

from blenderlevel import (  # noqa: E402
    forest,
    levelread,
    log,
    matwire,
    options,
    shapes,
    terrain,
)


def main():
    opts = options.parse_args()
    started = time.monotonic()
    level_dir = os.path.abspath(opts["level"])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    objects, materials = levelread.read_level(level_dir)
    log(f"{len(objects)} objects, {len(materials)} materials in {level_dir}")
    if opts["terrain"]:
        terrain.build_terrain(
            level_dir, objects, opts["center"], opts["radius"], ter_override=opts["ter"]
        )
    tiles = shapes.tiles_in_region(
        level_dir, objects, opts["center"], opts["radius"], opts["products"]
    )
    log(f"{len(tiles)} shape tiles meet the region")
    shapes.import_tiles(tiles, materials, level_dir, opts["center"], opts["radius"])
    if opts["forest"]:
        forest.import_forest(level_dir, opts["center"], opts["radius"], opts["forest_max"])
    matwire.wire_materials(materials, level_dir)
    if opts["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(opts["save"]))
        log(f"saved {opts['save']}")
    log(f"done in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
