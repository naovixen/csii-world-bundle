"""The units, the tables and the two file readers every half of the estimate needs.

Its own module because both halves need them and neither should import the other: the estimate
imports the mesh and texture halves, and both of those need what is here.
"""

import json
import os

MIB = 1024.0 * 1024.0

# The kinds `meshindex` records, and the settings section whose controls move each one - what
# the meter's "What should I change?" link points at.
KIND_SECTION = {"building": "world", "prop": "world", "tree": "world"}

# The merged world tiles: (settings switch, the bake product or bundle layer they are built from,
# the level's shape group). Their bytes follow their INPUT rather than an instance count, because
# a tile is one shape however many records went into it.
#
# The region is a SQUARE in every stage that reads one (`beamng.placement.in_region`,
# `stages/derived._tile_in_region`, `stages/water._intersects`, and `stages/networks` since
# 2026-08-20, when the map picker had to draw it and found a circle there).
TILE_GROUPS = [
    ("networks", ("networks",), "networks"),
    ("areas", ("derived", "areas"), "areas"),
    ("markings", ("derived", "markings"), "markings"),
    ("skirts", ("derived", "skirts"), "skirts"),
    ("terrain_cut", ("derived", "terraincut"), "terraincut"),
]


def load_json(path):
    """A JSON file, or None if it is not there or not JSON."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def dir_bytes(path, _cache={}):  # noqa: B006 - process-lived cache
    """Bytes under a directory, cached on its entry count and newest mtime."""
    try:
        stamp = (os.path.getmtime(path), len(os.listdir(path)))
    except OSError:
        return 0
    hit = _cache.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    _cache[path] = (stamp, total)
    return total
