"""Where the map's files are and what invalidates them.

The cache key, the three paths derived from it, and the atomic write. Its own module because
both halves of the map need it and neither should have to import the other: `mapview` writes the
document, `mappoints` reads the road samples beside it and stores the fast-travel points under
the same key.
"""

import hashlib
import json
import os

from .. import settings as settings_mod

SCHEMA = "cs2beamng.mapview/1"
MAP_DIR = "mapview"
POI_DIR = "pois"


def key_for(bundle):
    """A cache key over what the picture is drawn FROM."""
    parts = [os.path.abspath(bundle or "")]
    for rel in (
        "bundle.json",
        "lanes.jsonl",
        "networks.jsonl",
        os.path.join("terrain", "heightmap.png"),
        os.path.join("terrain", "splatmap.png"),
        os.path.join("terrain", "water_level.png"),
    ):
        try:
            stat = os.stat(os.path.join(bundle or "", rel))
            parts.append(f"{rel}:{stat.st_size}:{int(stat.st_mtime_ns)}")
        except OSError:
            parts.append(f"{rel}:-")
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=12).hexdigest()


def paths(bundle):
    """`(image, meta, samples)` for this bundle's drawn map."""
    base = os.path.join(settings_mod.app_dir(), MAP_DIR, key_for(bundle))
    return base + ".png", base + ".json", base + ".npy"


def poi_path(bundle):
    """Where this bundle's fast-travel points are kept.

    Per BUNDLE rather than per level, because a fast-travel point is a place in a city and not a
    property of the folder a level was built into.
    """
    return os.path.join(settings_mod.app_dir(), POI_DIR, key_for(bundle) + ".json")


def write(path, payload):
    """Write bytes through a temporary file, and say nothing if the disk refuses.

    Everything here is a cache: failing to write one costs the next request the time to redraw,
    and must never be the reason a settings page does not load.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError:
        pass


def load(bundle):
    """The stored map document for this bundle, or None. `image` is the PNG's path on disk."""
    image, meta, _samples = paths(bundle)
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
