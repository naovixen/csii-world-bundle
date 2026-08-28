"""Drawing the map picture: the bundle's ground, the road network over it, and the compose.

Split out of `mapview` because it is the only part that touches rasters and pixels; what remains
there is the document — the cache key, the georeference and what is written to disk.
"""

import os

import numpy
from csiibundle import groundmap, reader
from csiibundle import png as png_mod
from csiibundle import splat as splat_mod

SIZE = 1024  # pixels on the long edge; 14 m per pixel on a full city
ROAD_RGB = (236, 238, 244)
HIGHWAY_RGB = (255, 214, 122)
# A road is drawn one pixel wide and a highway two: at 14 m per pixel a carriageway is a third of
# a pixel, so the line width is legibility and not a measurement, and the two colours are what
# make a city's shape readable rather than a grey web.
HIGHWAY_CATEGORIES = {"highway"}

# -- the ground ------------------------------------------------------------------------------


def ground(bundle, rasters, manifest):
    """`(rgb, world)` — the bundle's ground at map size, and the pixel <-> metre conversion.

    `world` is what the page needs and nothing more: where pixel (0, 0) sits in bundle metres,
    how many metres a pixel is, and how many pixels there are. Bundle Z runs NORTH and the raster
    runs south down its rows, which is why the conversion subtracts.
    """
    heightmap = os.path.join(bundle, "terrain", "heightmap.png")
    if not os.path.isfile(heightmap):
        return None, None
    raw = png_mod.gray_array(heightmap)
    size = raw.shape[0]
    step = max(1, size // SIZE)
    scale = float(rasters.get("elevationScale") or 1.0)
    offset = float(rasters.get("elevationOffset") or 0.0)
    heights = raw[::step, ::step].astype(numpy.float32) * scale + offset
    cell = float(rasters.get("resolution") or 3.5) * step
    origin = rasters.get("originWorld") or [0.0, 0.0, 0.0]

    grounds, layers = ground_layers(bundle, rasters, heights.shape)
    palette = groundmap.ground_palette(os.path.join(bundle, "terrain", "surfaces"), grounds)
    surface = None
    water_path = os.path.join(bundle, "terrain", "water_level.png")
    if os.path.isfile(water_path):
        water = png_mod.gray_array(water_path)
        if water.shape != raw.shape:
            water = reader.resample_nearest(water, raw.shape)
        surface = water[::step, ::step].astype(numpy.float32) * scale
    rgb = groundmap.ground_image(heights, layers, palette, cell, surface)
    world = {
        "originX": float(origin[0]),
        "originZ": float(origin[2]),
        "metresPerPixel": cell,
        "pixels": int(heights.shape[0]),
        "mapSize": float(manifest.get("mapSize") or heights.shape[0] * cell),
    }
    return numpy.clip(rgb, 0, 255).astype(numpy.uint8), world


def ground_layers(bundle, rasters, shape):
    """`(names, index)` — the dominant ground per pixel, the terrain stage's own answer."""
    block = rasters.get("splatmap")
    path = os.path.join(bundle, "terrain", "splatmap.png")
    if not block or not os.path.isfile(path):
        return ["rock"], numpy.zeros(shape, dtype=numpy.uint8)
    try:
        sp = splat_mod.Splat(path, block)
        mapping, _published = splat_mod.channel_mapping(block)
        index, names = splat_mod.dominant_layer(sp, mapping, surface_index=None)
    except Exception:
        return ["rock"], numpy.zeros(shape, dtype=numpy.uint8)
    if index.shape != shape:
        index = reader.resample_nearest(index, shape)
    return names, index


# -- the roads -------------------------------------------------------------------------------


def roads(bundle, world, cancelled=None):
    """`(mask, midpoints)` — a (pixels, pixels) uint8 of 0 / 1 road / 2 highway, and one
    `(x, z, name)` per lane: the point set the snap and the suggestions are both taken from.

    The drivable network, `stages.poi._lane_network`'s own definition of it: `car` lanes owned by
    a segment in the navgraph's default categories. Drawn from the lane PATHS rather than the
    segment positions, because a segment record carries a point and a length and a map needs the
    curve.
    """
    from csiibundle import navgraph as navgraph_mod

    segments = {}
    path = reader.layer_path(bundle, "networks")
    if path:
        for record in reader.read_jsonl(path):
            attrs = record.get("attrs") or {}
            if attrs.get("category") in navgraph_mod.DEFAULT_CATEGORIES:
                segments[record["id"]] = (
                    attrs.get("name"),
                    attrs.get("category") in HIGHWAY_CATEGORIES,
                )
    pixels = int(world["pixels"])
    mask = numpy.zeros((pixels, pixels), dtype=numpy.uint8)
    midpoints = []
    lanes_path = reader.layer_path(bundle, "lanes")
    if not lanes_path or not segments:
        return mask, midpoints
    read = 0
    for lane in reader.read_jsonl(lanes_path):
        read += 1
        if cancelled is not None and read % 50000 == 0 and cancelled():
            return mask, midpoints
        if lane.get("kind") != "car":
            continue
        entry = segments.get(lane.get("owner"))
        if entry is None or "SideConnection" in (lane.get("flags") or ()):
            continue
        points = lane.get("path") or []
        if len(points) < 2:
            continue
        draw(mask, points, world, 2 if entry[1] else 1)
        middle = points[len(points) // 2]
        midpoints.append((float(middle[0]), float(middle[1]), float(middle[2]), entry[0]))
    return mask, midpoints


def draw(mask, points, world, value):
    """One lane path into the mask, as points sampled along it at about a pixel apart.

    A line drawn by sampling rather than by Bresenham: the paths are already dense (a CS2 lane is
    a handful of nodes over tens of metres) and the gaps a sparse one leaves are filled by
    stepping along each span. It costs one numpy call per span and reads the same at this size.
    """
    scale = 1.0 / float(world["metresPerPixel"])
    ox, oz = float(world["originX"]), float(world["originZ"])
    size = mask.shape[0]
    xs = numpy.fromiter((p[0] for p in points), dtype=numpy.float64, count=len(points))
    zs = numpy.fromiter((p[2] for p in points), dtype=numpy.float64, count=len(points))
    px = (xs - ox) * scale
    py = (oz - zs) * scale
    for i in range(len(points) - 1):
        steps = int(max(abs(px[i + 1] - px[i]), abs(py[i + 1] - py[i]))) + 1
        if steps > 4096:  # a path that leaves the map: skip it
            continue
        t = numpy.linspace(0.0, 1.0, steps + 1)
        cx = numpy.clip((px[i] + (px[i + 1] - px[i]) * t).astype(numpy.int32), 0, size - 1)
        cy = numpy.clip((py[i] + (py[i + 1] - py[i]) * t).astype(numpy.int32), 0, size - 1)
        current = mask[cy, cx]
        mask[cy, cx] = numpy.maximum(current, value)


def compose(ground_rgb, road_mask):
    """The ground with the road network painted over it."""
    out = ground_rgb.copy()
    out[road_mask == 1] = ROAD_RGB
    out[road_mask == 2] = HIGHWAY_RGB
    return out
