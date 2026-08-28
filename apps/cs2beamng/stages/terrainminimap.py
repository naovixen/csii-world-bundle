"""The level's minimap picture, drawn from the terrain the stage has just written.

`csiibundle.groundmap` draws it, which is the same code the app's own map view uses, so what a
person picks a region on and what the game shows in the corner are the same picture.
"""

import os

import numpy
from csiibundle import groundmap, reader

MINIMAP_SIZE = 2048


def write_minimap(level, bundle, heights, layers, grounds, paths, scale, cell):
    """`<name>_minimap.png` — the top-down ground image `TerrainBlock.minimapImage` names.

    The old dashboard/navigation map (`uiNavi.lua:354`) builds its tile from this field:
    `size = (blockSize, blockSize, maxHeight)`, `offset = (pos.x, pos.y + blockSize, 0)`. That
    `+ blockSize` is a TOP-LEFT convention — row 0 of the image is the NORTH edge of the block,
    which is also row 0 of the bundle's own rasters, so the array goes out unflipped. (The `.ter`
    is the one that flips; see `beamng/ter.py`.)

    The pixels are the terrain's own dominant ground, coloured by each ground texture's mean
    albedo and shaded by the heightfield's slope, with water tinted over it. It is the same
    information the level draws, at 7 m per pixel — not a render, and not pretending to be one.
    The minimap APP does not use this at all: it draws the navgraph as vectors (audit A7.0).
    """
    from csiibundle import png as png_mod

    if heights is None or not heights.size:
        return None
    step = max(1, heights.shape[0] // MINIMAP_SIZE)
    h = heights[::step, ::step].astype(numpy.float32)
    index = layers[::step, ::step][: h.shape[0], : h.shape[1]]
    palette = groundmap.ground_palette(os.path.join(bundle, "terrain", "surfaces"), grounds)

    surface = None
    if paths.water and os.path.isfile(paths.water):
        # the water raster is on the BASE grid; the heights may have been upsampled, so it is
        # resampled to the height grid before subsampling rather than sliced to fit
        water = png_mod.gray_array(paths.water)
        if water.shape != heights.shape:
            water = reader.resample_nearest(water, heights.shape)
        surface = water[::step, ::step].astype(numpy.float32)[: h.shape[0], : h.shape[1]] * scale

    # The picture itself is `csiibundle.groundmap`, because the settings screen's map view draws
    # the same one from the same bundle and two copies of it would drift apart in appearance
    # with nothing to catch them.
    rgb = groundmap.ground_image(h, index, palette, float(cell) * step, surface)
    rgba = numpy.concatenate([rgb, numpy.full((*h.shape, 1), 255.0)], axis=-1)
    name = f"{level.name}_minimap.png"
    level.write_bytes_if_changed(
        png_mod.encode_rgba(numpy.clip(rgba, 0, 255).astype(numpy.uint8)), name
    )
    return name
