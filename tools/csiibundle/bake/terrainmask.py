"""Bake product `terrainmask` — the §8c decision rasters for a heightmap-native consumer.

`terrain.hole_mask` rasterises `clipvolumes.jsonl` against the heightmap once (~80 s on a full
city) and answers the questions an engine terrain needs:

* `derived/terrain/clip_cells.png` — gray8 at FACE resolution `(h−1, w−1)`: 255 where a
  terrain square lies wholly inside a DEEP clip band (punch a hole), 128 where a deep band
  reaches it without containing it (a `touch` rule punches these too — it is what clears a
  tunnel mouth, see the app's terrain stage), 0 elsewhere. Face `(r, c)` spans heightmap
  samples `(r..r+1, c..c+1)`.
* `derived/terrain/shallow_floor.png` — gray16 at SAMPLE resolution, in the heightmap's own
  quantisation (`value·elevationScale + elevationOffset`, 0 = none): the floor of the shallow
  band covering the sample (`lane + bottom`, the game's number). `terrain.clamp_shallow_bands`
  lowers ground standing above it to it. Since v2 the covered set includes samples standing
  above the band's roof by less than `terrain.CLIP_SHALLOW_REACH` — the spikes.
* `derived/terrain/shallow_lane.png` (v2) — gray16, same quantisation and coverage: the
  carriageway height itself. A consumer that wants ground "under the road by its own margin"
  and the road's neighbours flush with its edge reads this, not the floor.
* `derived/terrain/deep_lane.png` (v2; v4 covers every footprint sample, not only those
  above the band's floor) — gray16: the road height under every DEEP-band footprint sample.
* `derived/terrain/hole_lane.png` (v5) — gray16 at FACE resolution: the lowest lane of the
  carving quads (deep strips, authored clip areas) reaching each face at sub-sample resolution
  (`terrain.contain_faces`); the surface a hole in that face is FOR. Hole rims hang their
  walls down to this, `deep_lane` second.

`derived/terrain/mask.json` records the sizes, the counts, and the option `holes` the app
should apply by default.
"""

import json
import os
import time

import numpy

from .. import png, terrain
from . import Product, register

PRODUCT_NAME = "terrainmask"
INPUTS = ("clipVolumes", "terrain")
VERSION = 6


def _quantise(raster, hm):
    """float32 heights (+inf = none) -> uint16 in the heightmap's own quantisation, 0 = none."""
    has = numpy.isfinite(raster)
    q = numpy.zeros(raster.shape, dtype=numpy.uint16)
    if has.any():
        raw = numpy.rint((raster[has].astype(numpy.float64) - hm.elevation_offset) / max(hm.elevation_scale, 1e-9))
        q[has] = numpy.clip(raw, 1, 65535).astype(numpy.uint16)
    return q, has


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "terrain")
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()
    progress.begin("terrainmask: clip volumes", total=None, unit="")
    hm = terrain.hole_mask(bundle)
    progress.end(f"{int(hm.contain.sum()):,} contained faces, {int(hm.deep_touch.sum()):,} deep-touched, "
                 f"{int(numpy.isfinite(hm.band_floor).sum()):,} samples under a shallow band")

    cells = numpy.zeros(hm.contain.shape, dtype=numpy.uint8)
    cells[hm.deep_touch] = 128
    cells[hm.contain] = 255
    png.write_gray(os.path.join(out_dir, "clip_cells.png"), cells)

    q_floor, has = _quantise(hm.band_floor, hm)
    png.write_gray(os.path.join(out_dir, "shallow_floor.png"), q_floor, bit_depth=16)
    q_lane, _ = _quantise(hm.band_lane, hm)
    png.write_gray(os.path.join(out_dir, "shallow_lane.png"), q_lane, bit_depth=16)
    q_deep, has_deep = _quantise(hm.deep_lane, hm)
    png.write_gray(os.path.join(out_dir, "deep_lane.png"), q_deep, bit_depth=16)
    q_face, _ = _quantise(hm.face_lane, hm)
    png.write_gray(os.path.join(out_dir, "hole_lane.png"), q_face, bit_depth=16)

    # how much a clamp would move: informational
    lowered = 0
    worst = 0.0
    if has.any():
        drop = hm.heights - hm.band_floor.astype(numpy.float64)
        hit = has & (drop > 0)
        lowered = int(hit.sum())
        worst = float(drop[hit].max()) if hit.any() else 0.0
    doc = {"schema": "csiibundle.bake.terrainmask/6", "faceSize": [int(cells.shape[0]), int(cells.shape[1])],
           "sampleSize": [int(q_floor.shape[0]), int(q_floor.shape[1])], "cell": hm.cell,
           "originWorld": [hm.origin_x, hm.origin_z], "elevationScale": hm.elevation_scale,
           "elevationOffset": hm.elevation_offset, "holesDefault": "touch",
           "counts": {"contain": int(hm.contain.sum()), "deepTouch": int(hm.deep_touch.sum()),
                      "shallowSamples": int(has.sum()), "deepSamples": int(has_deep.sum()),
                      "wouldLower": lowered, "worstDrop": round(worst, 3)}}
    with open(os.path.join(out_dir, "mask.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=1)
    return {"contain": doc["counts"]["contain"], "deepTouch": doc["counts"]["deepTouch"],
            "shallowSamples": doc["counts"]["shallowSamples"], "deepSamples": doc["counts"]["deepSamples"],
            "wouldLower": lowered, "worstDrop": round(worst, 3), "seconds": round(time.monotonic() - started, 1)}


register(Product(PRODUCT_NAME, INPUTS, "terrain/", run, VERSION))
