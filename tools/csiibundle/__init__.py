"""`csiibundle` — read a CS2 World Bundle without an engine.

This package is the engine-neutral half of the reconstruction: everything a consumer needs to
turn a bundle on disk into geometry, transforms, rasters and colour, with no dependency on any
particular renderer. `tools/blender_import_bundle.py` is one adapter over it; a BeamNG level
builder, a glTF re-exporter or a headless validator are others.

    import sys, os
    sys.path.insert(0, "<repo>/tools")        # Blender's --python does not add it for you
    import csiibundle

**It never imports `bpy`.** `python -c "import csiibundle"` under a plain system Python is part
of the repo's gate, so anything that needs a scene graph, an image datablock or a shader node
belongs in the adapter, not here.

Dependencies: the standard library plus `numpy`. (`tools/validate_bundle.py` is separately
stdlib-only and does not import this package.)

The authoritative contract is `docs/bundle-schema.md`; every module names the section it
implements. Two kinds of constant appear throughout and are tagged in place:

* ``CONTRACT`` — fixed by the bundle format. Changing it makes the reader wrong.
* ``TUNED`` — a presentation choice with a measured justification. A consumer may pick another
  value and still be reading the bundle correctly.

Modules, roughly in the order a consumer needs them
---------------------------------------------------
``reader``    manifest, layer paths, JSONL iteration, catalogue loaders (§1, §3, §10)
``frames``    the §2 frame conversion: instance matrices, LH-Y-up to RH-Z-up
``glb``       GLB parsing, node flattening, the degenerate-face policy (§6)
``instances`` §4 placement rules: the `__base` skirt condition, the decal lift
``materials`` §7 channel packing — the metallic/roughness glTF does NOT carry
``water``     §8 `rasters.water` — the ocean AND every swimming pool
``tints``     §7.2 per-instance paint — why the trees are grey without it
``lights``    §4.3 photometry — and the two different record shapes in one file
``networks``  §5 centerline fallback: the ribbon, and its mitre
``lanegraph`` §5A `lanes.jsonl` — the lane GRAPH: paths, kinds, owners
``bezier``    the curve maths every linear layer is stored as
``lanes``     §5B: GetTileCount, the bend, the rigid-tile instancing gate
``decals``    §5B markings: the lift ladder, subdivision, the quad lattice
``png``       the §8 raster decode, 8/16-bit grayscale and RGBA8
``splat``     the §8 splatmap: channel map, packed-nibble decode, composite-over-base
``terrain``   §8 ground: clip rasterisation, the corridor lattice, face generation
``areas``     §8b surface areas: triangle conformance and the driver loop
``drape``     the ProjectionSurface protocol, and a numpy heightfield implementing it
``colour``    the §11 grading derivation — `grading.json` into applicable constants

**Minimum viable consumer**: `reader` + `frames` + `instances` + `glb` places every building,
prop and tree correctly. Then, in rough order of how wrong the result looks without each:

* `materials` — or the whole city renders at ONE flat roughness. The `.glb` carries no
  metallic-roughness map at all, and no consumer notices until it looks like plastic.
* `water` — or the sea is the wrong colour and every swimming pool is a rectangle of white
  paint. CS2 draws water with a shader, so those materials ship with no textures.
* `tints` — or all the foliage is grey.
* `terrain` + `png` + `splat` — the ground and its water surface.
* `lanes` + `decals` + `bezier` + `drape` — road markings and roadside furniture.
* `lanegraph` — what a SIMULATION consumer wants, and a geometry consumer can skip entirely.
* `colour` + `lights` last: the bundle ships albedo, and both the illumination and the grading
  on top of it are things the game does, not things the textures carry.
"""

from . import (areas, bezier, colour, decals, drape, frames, glb, instances, lanegraph,
               lanes, lights, materials, networks, png, reader, splat, terrain, tints,
               water)

__all__ = ["areas", "bezier", "colour", "decals", "drape", "frames", "glb", "instances",
           "lanegraph", "lanes", "lights", "materials", "networks", "png", "reader", "splat",
           "terrain", "tints", "water"]

# Bundle schema version this package reads. `reader.schema_version` reports what a bundle on
# disk declares, for a consumer that wants to compare. CONTRACT.
SCHEMA_VERSION = 2
