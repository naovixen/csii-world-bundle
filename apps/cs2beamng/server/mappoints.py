"""The two things the map is picked on: a snapped point, and the fast-travel points.

The snap answers "where is the nearest drivable road to where I clicked", off the road midpoints
the map's own drawing pass already collected, so dropping a pin never costs a second walk of a
230 MiB layer. The suggestions are derived by `stages.poi.derive` — the same function the build
uses, because a suggestion the build would not have made is one that disappears the moment you
accept it. The fast-travel point FILE lives here too: `poi_file` names it and a build reads it.
"""

import json
import os

import numpy

from .mapstore import key_for, load, paths, poi_path, write

SUGGESTIONS = 12  # offered, not placed; `poi_max` is what a build derives


def suggestions(_bundle, midpoints):
    """Places worth offering as fast-travel points, by the build's own rule.

    `stages.poi._derive` picks the densest road cells at least `DERIVED_SEPARATION` apart and
    names each by the commonest street in it. Calling it here rather than restating it is what
    keeps a suggestion the same place the build would have chosen — accepting one must not move
    the point. It derives in the BeamNG frame, so the answer comes back through `to_bundle`.
    """
    if not midpoints:
        return []
    from ..beamng import frame
    from ..stages import poi as poi_stage

    points, labels = [], []
    for x, y, z, label in midpoints:
        points.append(frame.to_beamng(x, y, z))
        labels.append(label)
    network = poi_stage.Network(points, [(1.0, 0.0)] * len(points))
    network.labels = labels
    out = []
    for entry in poi_stage.derive(network, SUGGESTIONS, []):
        bx, by = entry["at_beamng"]
        x, _up, z = frame.to_bundle(bx, by, 0.0)
        out.append(
            {
                "name": entry["name"],
                "at": [round(x, 1), round(z, 1)],
                "description": entry["description"],
            }
        )
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
    from scipy.spatial import cKDTree

    tree = _tree_for(bundle, samples[:, :2], cKDTree)
    distance, index = tree.query(numpy.asarray([float(at[0]), float(at[1])], dtype=numpy.float64))
    point = samples[int(index)]
    doc = load(bundle) or {}
    names = doc.get("roadNames") or []
    which = int(point[2])
    return {
        "at": [round(float(point[0]), 1), round(float(point[1]), 1)],
        "name": names[which] if 0 <= which < len(names) else None,
        "snapped": True,
        "movedMetres": round(float(distance), 1),
    }


_SAMPLE_CACHE = {}
_TREE_CACHE = {}


def save_samples(path, midpoints, names):
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
    _image, _meta, samples = paths(bundle)
    try:
        with open(samples, "rb") as handle:
            out = numpy.load(handle)
    except (OSError, ValueError):
        out = numpy.zeros((0, 3), dtype=numpy.float32)
    if out.ndim != 2 or out.shape[1] != 3:  # a file from an older map build
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
    write(path, json.dumps({"frame": "bundle", "points": clean}, indent=1).encode("utf-8"))
    return path, clean
