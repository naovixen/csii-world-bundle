"""A fine-raster copy of the example bundle — support for the terrainFine gates."""

import json
import os
import shutil

import numpy
from csiibundle import png

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")


def make_fine_copy(tmp, factor=2, jitter=None):
    """A copy of the example bundle with `terrain/heightmap_fine.png` = the base map upsampled
    bilinearly by `factor` (texel centres at the quarter points, like the game's cascade) and the
    manifest declaring `layers.terrainFine` + `rasters.terrainFine`."""
    dst = os.path.join(tmp, "bundle")
    shutil.copytree(EXAMPLE, dst)
    manifest = json.load(open(os.path.join(dst, "bundle.json"), encoding="utf-8"))
    rasters = manifest["rasters"]
    base = png.gray_array(os.path.join(dst, "terrain", "heightmap.png")).astype(numpy.float64)
    h, w = base.shape
    cell = float(rasters["resolution"])
    fine_cell = cell / factor
    # fine texel (i, j) centre = base origin - cell/2 + (i + 0.5) * fine_cell -> bilinear sample of the base
    fy = (numpy.arange(h * factor) + 0.5) / factor - 0.5
    fx = (numpy.arange(w * factor) + 0.5) / factor - 0.5
    y0 = numpy.clip(numpy.floor(fy).astype(int), 0, h - 1)
    y1 = numpy.clip(y0 + 1, 0, h - 1)
    ty = numpy.clip(fy - y0, 0, 1)
    x0 = numpy.clip(numpy.floor(fx).astype(int), 0, w - 1)
    x1 = numpy.clip(x0 + 1, 0, w - 1)
    tx = numpy.clip(fx - x0, 0, 1)
    top = base[y0][:, x0] * (1 - tx)[None, :] + base[y0][:, x1] * tx[None, :]
    bot = base[y1][:, x0] * (1 - tx)[None, :] + base[y1][:, x1] * tx[None, :]
    fine = top * (1 - ty)[:, None] + bot * ty[:, None]
    if jitter is not None:
        fine = fine + jitter
    fine16 = numpy.ascontiguousarray(numpy.clip(numpy.rint(fine), 0, 65535).astype(numpy.uint16))
    png.write_gray(os.path.join(dst, "terrain", "heightmap_fine.png"), fine16, bit_depth=16)
    ox, _, oz = rasters["originWorld"]
    manifest["layers"]["terrainFine"] = "terrain/heightmap_fine.png"
    rasters["terrainFine"] = {
        "file": "terrain/heightmap_fine.png",
        "size": [w * factor, h * factor],
        "resolution": [fine_cell, fine_cell],
        "originWorld": [ox - cell / 2 + fine_cell / 2, 0.0, oz + cell / 2 - fine_cell / 2],
        "semantics": "test: bilinear upsample of the base map",
    }
    json.dump(manifest, open(os.path.join(dst, "bundle.json"), "w", encoding="utf-8"), indent=1)
    return dst, fine16, fine_cell
