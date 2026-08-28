"""Bake product `terrainmask` — the §8c decision rasters for a heightmap-native consumer.

`clipgrid.hole_mask` rasterises `clipvolumes.jsonl` against the heightmap once (~80 s on a full
city) and answers two questions an engine terrain needs:

* `derived/terrain/clip_cells.png` — gray8 at FACE resolution `(h−1, w−1)`: 255 where a
  terrain square lies wholly inside a DEEP clip band (punch a hole), 128 where a deep band
  reaches it without containing it (informational; a `majority` rule may use it), 0 elsewhere.
  Face `(r, c)` spans heightmap samples `(r..r+1, c..c+1)`.
* `derived/terrain/carriageway_ceiling.png` — gray16 at SAMPLE resolution, in the heightmap's
  own quantisation (`value·elevationScale + elevationOffset`, 0 = no shallow band): the top of
  the game's own road cross-section under the shallow band covering the sample
  (`lane + top − CLIP_BAND_EXPANSION`). A consumer lowers any sample standing above it onto it
  — `carriageway.clamp_carriageway`. That is the 1.1 % of covered samples the game's stencil
  discards and a heightfield cannot; the other 98.9 % are left exactly as the game graded them
  (audit §51). This replaced `shallow_floor.png`, which recorded the band FLOOR (`lane − 0.5`)
  and so dug 0.25 m of trench under every road in the city.

`derived/terrain/mask.json` records the sizes, the counts, and the option `holes` the app
should apply by default (`contain`).

The grid is the ground the consumer will mesh: the bundle's 1.75 m `terrainFine` raster when
it carries one (`reader.load_heightfield`; `mask.json` says `"fine": true`, sizes 8192/8191),
else the 3.5 m base map. Both are inputs, so a re-rip that adds the fine raster re-bakes.
"""

import json
import os
import time

import numpy

from .. import clipgrid, png
from . import Product, register

PRODUCT_NAME = "terrainmask"
INPUTS = ("clipVolumes", "terrain", "terrainFine")


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "terrain")
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()
    progress.begin("terrainmask: clip volumes", total=None, unit="")
    hm = clipgrid.hole_mask(bundle, prefer_fine=context.options.get("ground", "fine") == "fine")
    progress.end(
        f"{int(hm.contain.sum()):,} contained faces, {int(hm.deep_touch.sum()):,} deep-touched, "
        f"{int(numpy.isfinite(hm.band_ceiling).sum()):,} samples under a shallow band"
    )

    cells = numpy.zeros(hm.contain.shape, dtype=numpy.uint8)
    cells[hm.deep_touch] = 128
    cells[hm.contain] = 255
    png.write_gray(os.path.join(out_dir, "clip_cells.png"), cells)

    ceiling = hm.band_ceiling.astype(numpy.float64)
    has = numpy.isfinite(ceiling)
    q = numpy.zeros(ceiling.shape, dtype=numpy.uint16)
    if has.any():
        raw = numpy.rint((ceiling[has] - hm.elevation_offset) / max(hm.elevation_scale, 1e-9))
        q[has] = numpy.clip(raw, 1, 65535).astype(numpy.uint16)
    png.write_gray(os.path.join(out_dir, "carriageway_ceiling.png"), q, bit_depth=16)

    # v2's deep_floor.png / deep_footprint.png (a heightfield-only stand-in that LOWERED in-band
    # samples) stay retired: moving the ground is a fidelity loss — the game hides terrain with a
    # stencil and never moves it (audit §46) — and §49 showed the cut mesh's mismatch was a unit
    # error in `*TexSize`, not something the rim geometry could fix.
    # `shallow_floor.png` (v3) joins them: it recorded the band FLOOR, which is the depth to
    # which the game DISCARDS terrain, not a height it moves ground to (audit §51).
    for stale in ("deep_floor.png", "deep_footprint.png", "shallow_floor.png"):
        try:
            os.remove(os.path.join(out_dir, stale))
        except OSError:
            pass

    # how much a clamp would move: informational
    lowered = 0
    worst = 0.0
    if has.any():
        drop = hm.heights - ceiling
        hit = has & (drop > 0)
        lowered = int(hit.sum())
        worst = float(drop[hit].max()) if hit.any() else 0.0
    doc = {
        "schema": "csiibundle.bake.terrainmask/4",
        "faceSize": [int(cells.shape[0]), int(cells.shape[1])],
        "sampleSize": [int(q.shape[0]), int(q.shape[1])],
        "cell": hm.cell,
        "fine": bool(hm.fine),
        "originWorld": [hm.origin_x, hm.origin_z],
        "elevationScale": hm.elevation_scale,
        "elevationOffset": hm.elevation_offset,
        "holesDefault": "contain",
        "counts": {
            "contain": int(hm.contain.sum()),
            "deepTouch": int(hm.deep_touch.sum()),
            "shallowSamples": int(has.sum()),
            "wouldLower": lowered,
            "worstDrop": round(worst, 3),
        },
    }
    with open(os.path.join(out_dir, "mask.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=1)
    return {
        "contain": doc["counts"]["contain"],
        "deepTouch": doc["counts"]["deepTouch"],
        "shallowSamples": doc["counts"]["shallowSamples"],
        "wouldLower": lowered,
        "worstDrop": round(worst, 3),
        "seconds": round(time.monotonic() - started, 1),
    }


# v4: shallow_floor.png (the band FLOOR) replaced by carriageway_ceiling.png (the game's own
# road cross-section top) — audit §51.
register(Product(PRODUCT_NAME, INPUTS, "terrain/", run, version=4, options=("ground",)))
