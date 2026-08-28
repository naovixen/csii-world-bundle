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
``navgraph``  the drivable network as a routable centerline graph, over `navroad`/`navread`/
              `navspine`/`navjunction`/`navmerge`/`navclearance`/`navreport`
``bezier``    the curve maths every linear layer is stored as
``polyline``  resampling, framing, simplification and weld keys over evaluated points
``lanes``     §5B: GetTileCount, the bend, the rigid-tile instancing gate
``decals``    §5B markings: the lift ladder, subdivision, the quad lattice
``png``       the §8 raster decode, 8/16-bit grayscale and RGBA8
``splat``     the §8 splatmap: channel map, packed-nibble decode, composite-over-base
``clipraster``  §8c clip-band quads rasterised onto a sample grid, and mask morphology
``containment`` §8c: the deletion rule — only a deep band carves, and only a whole face
``clipgrid``    §8c: `clipvolumes.jsonl` onto the heightmap grid, and the hole mask
``carriageway`` §8: the shipped ground heights, and the one clamp a consumer applies
``lattice``     §8 ground mesh: the corridor lattice and its three face families
``latticecut``  §8c: the exact prism cut of lattice faces
``watersurface`` §8 water surface, on the same lattice as the ground
``areas``     §8b surface areas: triangle conformance and the driver loop
``drape``     the ProjectionSurface protocol; a numpy heightfield AND a numpy triangle-soup
              surface implementing it (the road surface for a consumer with no ray-caster)
``colour``    the §11 grading derivation — `grading.json` into applicable constants
``colourspace`` transfer curves, Rec.709<->ACEScg, and HDRP's matrix operators, ported
``colourocio``  the grading as OCIO: view picking, on-screen evaluation, the view transform
``illumination`` the §11 lighting block: sun, moon, sky, indirect and the clock
``glbwrite``  the inverse of `glb`: arrays back to a §6-shaped `.glb` (the bake's tiles)
``decimate``  generated LOD chains (quadric via optional `pyfqmr`, else vertex clustering)
``collision`` simple colliders: thickness rule, box, trunk cylinder, wind vertex colours
``lod``       CS2's own LOD arithmetic, ported: how far the game draws a mesh, from that
              mesh's bounds and triangle area alone (audit §62)
``bake``      the offline second stage: `python -m csiibundle.bake` writes `derived/` (§13)

**Minimum viable consumer**: `reader` + `frames` + `instances` + `glb` places every building,
prop and tree correctly. Then, in rough order of how wrong the result looks without each:

* `materials` — or the whole city renders at ONE flat roughness. The `.glb` carries no
  metallic-roughness map at all, and no consumer notices until it looks like plastic.
* `water` — or the sea is the wrong colour and every swimming pool is a rectangle of white
  paint. CS2 draws water with a shader, so those materials ship with no textures.
* `tints` — or all the foliage is grey.
* `lattice` + `clipgrid` + `png` + `splat` — the ground and its water surface.
* `lanes` + `decals` + `bezier` + `drape` — road markings and roadside furniture.
* `lanegraph` — what a SIMULATION consumer wants, and a geometry consumer can skip entirely.
* `colour` + `lights` last: the bundle ships albedo, and both the illumination and the grading
  on top of it are things the game does, not things the textures carry.
"""

from . import (
    areas,
    bezier,
    carriageway,
    clipgrid,
    clipraster,
    collision,
    colour,
    colourocio,
    colourspace,
    containment,
    decals,
    decimate,
    drape,
    frames,
    glb,
    glbwrite,
    illumination,
    instances,
    lanegraph,
    lanes,
    lattice,
    latticecut,
    lights,
    lod,
    materials,
    navclearance,
    navgraph,
    navjunction,
    navmerge,
    navread,
    navreport,
    navroad,
    navspine,
    networks,
    png,
    polyline,
    reader,
    splat,
    tints,
    water,
    watersurface,
)

__all__ = [
    "areas",
    "bezier",
    "carriageway",
    "clipgrid",
    "clipraster",
    "collision",
    "colour",
    "colourocio",
    "colourspace",
    "containment",
    "decals",
    "decimate",
    "drape",
    "frames",
    "glb",
    "glbwrite",
    "illumination",
    "images",
    "instances",
    "lanegraph",
    "lanes",
    "lattice",
    "latticecut",
    "lights",
    "lod",
    "materials",
    "navclearance",
    "navgraph",
    "navjunction",
    "navmerge",
    "navread",
    "navreport",
    "navroad",
    "navspine",
    "networks",
    "png",
    "polyline",
    "reader",
    "splat",
    "tints",
    "water",
    "watersurface",
]

# Bundle schema version this package reads. `reader.schema_version` reports what a bundle on
# disk declares, for a consumer that wants to compare. CONTRACT.
SCHEMA_VERSION = 2
