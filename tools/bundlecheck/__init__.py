"""The standalone bundle validator's internals — `tools/validate_bundle.py` is the entry.

Standalone means exactly that: Python 3.8+ and the standard library, no pip installs, no game,
no producer source. This package deliberately does NOT import `csiibundle` — it is what a
consumer author runs on a bundle they were handed, before they have any of this repo — and the
two readers it re-implements (`report.png_header`, the sRGB-free raster checks) say so where
the copy is.

Modules:

* ``schema``    — the JSON Schema (draft 2020-12) subset evaluator. A general library: it knows
                  nothing about bundles, only about the keyword subset `docs/schema/*.json` uses.
* ``report``    — the `Report`, the per-run `Context` every check takes, and the layer loaders.
* ``glbcheck``  — the GLB container walk and the per-mesh/texture/quaternion checks.
* ``assets``    — the joined passes: materials -> textures, instances -> meshes, networks,
                  §5B lane geometry, and the unreferenced-file sweeps.
* ``polygons``  — the polygon layers (§8b areas, §8c clip volumes, §8d districts).
* ``auxlayers`` — §5A lanes, §7.2 appearance, §10 warnings, §8 grading.
* ``rasters``   — the §8 raster grid: PNG headers, bit depths, geo-reference blocks.

Every check is `check(ctx) -> None` on a `report.Context` — one signature, so
`validate_bundle.py` runs them as a registry.
"""
