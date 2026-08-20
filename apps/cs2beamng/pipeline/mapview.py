"""The map — the picture that makes "which part of my city?" and "where is this point?"
answerable by somebody who does not know their city's coordinates.

`region` is three numbers in bundle metres and `poi_file` is a path to a JSON list of more of
them. Both are free-text boxes whose valid answers are a place on a map, which is a design
failure whenever the answer is a place (plan §1). This module draws the map they are picked on:
**one drawing surface, two things on top of it** (plan §3.6/§3.7) — the region's centre and
radius, and the fast-travel points — because a circle widget now and a point widget later would
share nothing.

What is drawn is the bundle's own ground (`csiibundle.groundmap`, the same picture the level's
minimap is) with the drivable road network over it, and it is built ONCE per bundle: reading the
lane layer is 230 MiB of JSON, which is a background job and a cached PNG, not something to do
between two keystrokes. The georeference goes out beside it so the page can convert a pixel to a
place and back — the same conversion the rasters block documents and every reader already
performs.

The **suggested** fast-travel points come from here too, at the same time and off the same road
network, so the editor can offer them before any build has run. They are derived by
`stages.poi._derive`, which is what the build itself will use: a suggestion the build would not
have made is a suggestion that disappears the moment you accept it.
"""

import hashlib
import json
import math
import os
import time

import numpy

from csiibundle import groundmap, png as png_mod, reader, splat as splat_mod

from .. import settings as settings_mod

SCHEMA = "cs2beamng.mapview/1"
MAP_DIR = "mapview"
SIZE = 1024                       # pixels on the long edge; 14 m per pixel on a full city
ROAD_RGB = (236, 238, 244)
HIGHWAY_RGB = (255, 214, 122)
# A road is drawn one pixel wide and a highway two: at 14 m per pixel a carriageway is a third of
# a pixel, so the line width is legibility and not a measurement, and the two colours are what
# make a city's shape readable rather than a grey web.
HIGHWAY_CATEGORIES = {"highway"}
SUGGESTIONS = 12                  # offered, not placed; `poi_max` is what a build derives


def key_for(bundle):
    """A cache key over what the picture is drawn FROM."""
    parts = [os.path.abspath(bundle or "")]
    for rel in ("bundle.json", "lanes.jsonl", "networks.jsonl",
                os.path.join("terrain", "heightmap.png"),
                os.path.join("terrain", "splatmap.png"),
                os.path.join("terrain", "water_level.png")):
        try:
            stat = os.stat(os.path.join(bundle or "", rel))
            parts.append(f"{rel}:{stat.st_size}:{int(stat.st_mtime_ns)}")
        except OSError:
            parts.append(f"{rel}:-")
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=12).hexdigest()


def _paths(bundle):
    base = os.path.join(settings_mod.app_dir(), MAP_DIR, key_for(bundle))
    return base + ".png", base + ".json", base + ".npy"


def load(bundle):
    """The stored map document for this bundle, or None. `image` is the PNG's path on disk."""
    image, meta, _samples = _paths(bundle)
    if not (os.path.isfile(image) and os.path.isfile(meta)):
        return None
    try:
        with open(meta, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    doc["image"] = image
    return doc


def build(bundle, cancelled=None):
    """Draw the map for this bundle, store it, and return the document."""
    started = time.monotonic()
    manifest = reader.load_manifest(bundle)
    if manifest is None:
        return None
    rasters = manifest.get("rasters") or {}
    ground, world = _ground(bundle, rasters, manifest)
    if ground is None:
        return None
    if cancelled is not None and cancelled():
        return None
    roads, midpoints = _roads(bundle, world, cancelled)
    if cancelled is not None and cancelled():
        return None
    image = _compose(ground, roads)

    image_path, meta_path, samples_path = _paths(bundle)
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    _write(image_path, png_mod.encode_rgb(image))
    # The snap's own point set, saved beside the picture rather than re-derived: dropping a pin
    # must not cost a second pass over a 230 MiB layer, and the same pass has just walked it.
    names = sorted({name for _x, _y, _z, name in midpoints if name})
    _save_samples(samples_path, midpoints, names)
    doc = {"schema": SCHEMA, "bundle": os.path.abspath(bundle), "key": key_for(bundle),
           "world": world, "suggestions": _suggestions(bundle, midpoints),
           "roadNames": names, "roadSamples": len(midpoints),
           "seconds": round(time.monotonic() - started, 1)}
    _write(meta_path, json.dumps(doc, separators=(",", ":")).encode("utf-8"))
    doc["image"] = image_path
    return doc


# -- the ground ------------------------------------------------------------------------------

def _ground(bundle, rasters, manifest):
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

    grounds, layers = _layers(bundle, rasters, heights.shape)
    palette = groundmap.ground_palette(os.path.join(bundle, "terrain", "surfaces"), grounds)
    surface = None
    water_path = os.path.join(bundle, "terrain", "water_level.png")
    if os.path.isfile(water_path):
        water = png_mod.gray_array(water_path)
        if water.shape != raw.shape:
            water = reader.resample_nearest(water, raw.shape)
        surface = water[::step, ::step].astype(numpy.float32) * scale
    rgb = groundmap.ground_image(heights, layers, palette, cell, surface)
    world = {"originX": float(origin[0]), "originZ": float(origin[2]),
             "metresPerPixel": cell, "pixels": int(heights.shape[0]),
             "mapSize": float(manifest.get("mapSize") or heights.shape[0] * cell)}
    return numpy.clip(rgb, 0, 255).astype(numpy.uint8), world


def _layers(bundle, rasters, shape):
    """`(names, index)` — the dominant ground per pixel, the terrain stage's own answer."""
    block = rasters.get("splatmap")
    path = os.path.join(bundle, "terrain", "splatmap.png")
    if not block or not os.path.isfile(path):
        return ["rock"], numpy.zeros(shape, dtype=numpy.uint8)
    try:
        sp = splat_mod.Splat(path, block)
        mapping, _published = splat_mod.channel_mapping(block)
        index, names = splat_mod.dominant_layer(sp, mapping, surface_index=None)
    except Exception:                                     # noqa: BLE001 - a map is never fatal
        return ["rock"], numpy.zeros(shape, dtype=numpy.uint8)
    if index.shape != shape:
        index = reader.resample_nearest(index, shape)
    return names, index


# -- the roads -------------------------------------------------------------------------------

def _roads(bundle, world, cancelled=None):
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
                segments[record["id"]] = (attrs.get("name"),
                                          attrs.get("category") in HIGHWAY_CATEGORIES)
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
        _draw(mask, points, world, 2 if entry[1] else 1)
        middle = points[len(points) // 2]
        midpoints.append((float(middle[0]), float(middle[1]), float(middle[2]), entry[0]))
    return mask, midpoints


def _draw(mask, points, world, value):
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
        if steps > 4096:                                  # a path that leaves the map: skip it
            continue
        t = numpy.linspace(0.0, 1.0, steps + 1)
        cx = numpy.clip((px[i] + (px[i + 1] - px[i]) * t).astype(numpy.int32), 0, size - 1)
        cy = numpy.clip((py[i] + (py[i + 1] - py[i]) * t).astype(numpy.int32), 0, size - 1)
        current = mask[cy, cx]
        mask[cy, cx] = numpy.maximum(current, value)


def _compose(ground, roads):
    out = ground.copy()
    out[roads == 1] = ROAD_RGB
    out[roads == 2] = HIGHWAY_RGB
    return out


# -- the suggestions -------------------------------------------------------------------------

def _suggestions(bundle, midpoints):
    """Places worth offering as fast-travel points, by the build's own rule.

    `stages.poi._derive` picks the densest road cells at least `DERIVED_SEPARATION` apart and
    names each by the commonest street in it. Calling it here rather than restating it is what
    keeps a suggestion the same place the build would have chosen — accepting one must not move
    the point. It derives in the BeamNG frame, so the answer comes back through `to_bundle`.
    """
    if not midpoints:
        return []
    from ..beamng import frame                            # noqa: PLC0415
    from ..stages import poi as poi_stage                 # noqa: PLC0415

    points, labels = [], []
    for x, y, z, label in midpoints:
        points.append(frame.to_beamng(x, y, z))
        labels.append(label)
    network = poi_stage._Network(points, [(1.0, 0.0)] * len(points))
    network.labels = labels
    out = []
    for entry in poi_stage._derive(network, SUGGESTIONS, []):
        bx, by = entry["at_beamng"]
        x, _up, z = frame.to_bundle(bx, by, 0.0)
        out.append({"name": entry["name"], "at": [round(x, 1), round(z, 1)],
                    "description": entry["description"]})
    return out


# -- placing a point -------------------------------------------------------------------------

def snap(bundle, at):
    """The nearest drivable point to `at` (bundle X/Z), or `at` unchanged where there is no road.

    The editor snaps a dropped pin onto the same network the BUILD snaps it onto, so what you
    place is what you get. It uses the map's own road samples rather than the level's, because a
    point can be placed before any level exists.
    """
    samples = _road_samples(bundle)
    if samples is None or not len(samples):
        return {"at": [float(at[0]), float(at[1])], "snapped": False}
    from scipy.spatial import cKDTree                     # noqa: PLC0415 - a hard dependency

    tree = _tree_for(bundle, samples[:, :2], cKDTree)
    distance, index = tree.query(numpy.asarray([float(at[0]), float(at[1])], dtype=numpy.float64))
    point = samples[int(index)]
    doc = load(bundle) or {}
    names = doc.get("roadNames") or []
    which = int(point[2])
    return {"at": [round(float(point[0]), 1), round(float(point[1]), 1)],
            "name": names[which] if 0 <= which < len(names) else None,
            "snapped": True, "movedMetres": round(float(distance), 1)}


_SAMPLE_CACHE = {}
_TREE_CACHE = {}


def _save_samples(path, midpoints, names):
    """`(N, 3)` float32: x, z, and the index of the street's name in `names`.

    The name rides along because a point dropped on a map should arrive CALLED something — "the
    keys, bottom-left" is what a person will rename it to, but "Hickory Highway" is a better
    starting point than "Point 3", and the street is the one thing the map already knows.
    """
    lookup = {name: index for index, name in enumerate(names)}
    rows = [(x, z, lookup.get(name, -1)) for x, _y, z, name in midpoints]
    array = numpy.asarray(rows, dtype=numpy.float32).reshape(-1, 3)
    try:
        with open(path + ".tmp", "wb") as handle:
            numpy.save(handle, array)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def _road_samples(bundle):
    """`(N, 3)` bundle X/Z plus a street-name index, from the file the map build wrote.

    Never re-derived here: deriving it is a pass over a 230 MiB layer, and a drag that pays that
    is a drag nobody finishes. Where the file is missing the snap simply does not snap, and says
    so, rather than freezing the page to go and find out.
    """
    stamp = key_for(bundle)
    hit = _SAMPLE_CACHE.get(bundle)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    _image, _meta, samples = _paths(bundle)
    try:
        with open(samples, "rb") as handle:
            out = numpy.load(handle)
    except (OSError, ValueError):
        out = numpy.zeros((0, 3), dtype=numpy.float32)
    if out.ndim != 2 or out.shape[1] != 3:                # a file from an older map build
        out = numpy.zeros((0, 3), dtype=numpy.float32)
    _SAMPLE_CACHE.clear()
    _TREE_CACHE.clear()
    _SAMPLE_CACHE[bundle] = (stamp, out)
    return out


def _tree_for(bundle, samples, factory):
    hit = _TREE_CACHE.get(bundle)
    if hit is None:
        hit = _TREE_CACHE[bundle] = factory(samples)
    return hit


def _write(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError:
        pass


# -- the fast-travel points the app keeps ------------------------------------------------------
#
# `poi_file` still names a file and the build still reads it: the CLI contract does not change at
# all (plan §3.7). What changes is that a person no longer has to write it. It is stored per
# BUNDLE rather than per level, because a fast-travel point is a place in a city and not a
# property of the folder a level was built into.

POI_DIR = "pois"


def poi_path(bundle):
    return os.path.join(settings_mod.app_dir(), POI_DIR, key_for(bundle) + ".json")


def load_pois(bundle):
    """`[{name, at, description}]` in bundle X/Z, or `[]`."""
    try:
        with open(poi_path(bundle), encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return []
    points = doc.get("points") if isinstance(doc, dict) else doc
    return [p for p in (points or []) if isinstance(p, dict) and p.get("name") and p.get("at")]


def save_pois(bundle, points):
    """Write the list in the shape `stages.poi._read_poi_file` reads, and return its path.

    `frame: "bundle"` on the wrapper, so the points are stored in the coordinates the map works
    in and the build converts them - rather than the app storing BeamNG X/Y and the map having
    to convert twice.
    """
    path = poi_path(bundle)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = []
    for point in points or []:
        try:
            at = [float(point["at"][0]), float(point["at"][1])]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        name = str(point.get("name") or "").strip()
        if not name:
            continue
        entry = {"name": name, "at": [round(at[0], 1), round(at[1], 1)]}
        if point.get("description"):
            entry["description"] = str(point["description"])
        clean.append(entry)
    _write(path, json.dumps({"frame": "bundle", "points": clean}, indent=1).encode("utf-8"))
    return path, clean
