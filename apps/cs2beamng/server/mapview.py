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

import json
import os
import time

from csiibundle import png as png_mod
from csiibundle import reader

from . import mapimage, mappoints
from .mapstore import SCHEMA, key_for, paths, write


def build(bundle, cancelled=None):
    """Draw the map for this bundle, store it, and return the document."""
    started = time.monotonic()
    manifest = reader.load_manifest(bundle)
    if manifest is None:
        return None
    rasters = manifest.get("rasters") or {}
    ground, world = mapimage.ground(bundle, rasters, manifest)
    if ground is None:
        return None
    if cancelled is not None and cancelled():
        return None
    roads, midpoints = mapimage.roads(bundle, world, cancelled)
    if cancelled is not None and cancelled():
        return None
    image = mapimage.compose(ground, roads)

    image_path, meta_path, samples_path = paths(bundle)
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    write(image_path, png_mod.encode_rgb(image))
    # The snap's own point set, saved beside the picture rather than re-derived: dropping a pin
    # must not cost a second pass over a 230 MiB layer, and the same pass has just walked it.
    names = sorted({name for _x, _y, _z, name in midpoints if name})
    mappoints.save_samples(samples_path, midpoints, names)
    doc = {
        "schema": SCHEMA,
        "bundle": os.path.abspath(bundle),
        "key": key_for(bundle),
        "world": world,
        "suggestions": mappoints.suggestions(bundle, midpoints),
        "roadNames": names,
        "roadSamples": len(midpoints),
        "seconds": round(time.monotonic() - started, 1),
    }
    write(meta_path, json.dumps(doc, separators=(",", ":")).encode("utf-8"))
    doc["image"] = image_path
    return doc


# -- the fast-travel points the app keeps ------------------------------------------------------
#
# `poi_file` still names a file and the build still reads it: the CLI contract does not change at
# all (plan §3.7). What changes is that a person no longer has to write it. It is stored per
# BUNDLE rather than per level, because a fast-travel point is a place in a city and not a
# property of the folder a level was built into.
