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

Modules
-------
``reader``   manifest, layer paths, JSONL iteration, catalogue loaders (§1, §3, §10)
``frames``   the §2 frame conversion: instance matrices, LH-Y-up to RH-Z-up
``glb``      GLB parsing, node flattening, the degenerate-face policy (§6)
``png``      the §8 raster decode, 8/16-bit grayscale and RGBA8
``splat``    the §8 splatmap: channel map, packed-nibble decode
``colour``   the §11 grading derivation — `grading.json` into applicable constants
"""

from . import colour, frames, glb, png, reader, splat

__all__ = ["colour", "frames", "glb", "png", "reader", "splat"]

# Bundle schema version this package reads. `reader.check_schema` compares a manifest against
# it. CONTRACT.
SCHEMA_VERSION = 2
