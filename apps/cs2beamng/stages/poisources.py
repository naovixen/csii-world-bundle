"""Where a fast-travel point can come from: your own list, CS2's districts, or the road network.

Three tiers, in that order. An authored list wins outright; failing that the game's own district
centres are places the city itself named; failing those the busiest road cells are derived."""

import json
import math
import os

import numpy
from csiibundle import reader

from ..beamng import frame

# Vanilla ships 3 (`italy`) to 9 (`west_coast_usa`) quick-travel points. A derived set is capped
# on the same scale: the big map is a menu, not an index.
DERIVED_CELL = 512.0  # metres; the grid the derived seeds are picked on

DERIVED_SEPARATION = 1800.0  # metres between two derived points

# -- tier 1: the authored list -------------------------------------------------------------------


def read_poi_file(path, progress):
    """`[{name, at_beamng, description}]` from the user's JSON list, or `[]`."""
    if not path:
        return []
    if not os.path.isfile(path):
        progress.log(f"poi: poi_file {path!r} does not exist - no authored points")
        return []
    with open(path, encoding="utf-8") as handle:
        try:
            doc = json.load(handle)
        except ValueError as exc:
            progress.log(f"poi: poi_file {path!r} is not JSON ({exc}) - no authored points")
            return []
    default_frame = "beamng"
    if isinstance(doc, dict):
        default_frame = str(doc.get("frame") or default_frame)
        doc = doc.get("points") or doc.get("pois") or []
    out = []
    for entry in doc if isinstance(doc, list) else []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("at"):
            progress.log(f"poi: skipping malformed entry {entry!r} (needs `name` and `at`)")
            continue
        at = [float(v) for v in entry["at"]]
        which = str(entry.get("frame") or default_frame).lower()
        if which.startswith("bundle"):
            # bundle X/Z (east/north) -> BeamNG X/Y; the height is irrelevant, the snap sets it
            at = [at[0], at[-1] if len(at) > 2 else at[1]]
        out.append(
            {
                "name": str(entry["name"]),
                "at_beamng": (at[0], at[1]),
                "description": entry.get("description"),
            }
        )
    if out:
        progress.log(f"poi: {len(out)} authored point(s) from {path}")
    return out


# -- tier 3: CS2's own districts -----------------------------------------------------------------


def read_districts(bundle_dir, progress):
    """`[{name, at_beamng, description}]` from `districts.jsonl` (§8d), largest district first.

    The centroid the producer ships is the polygon's NODE mean and is inside the district for any
    convex-ish shape; the snap moves it onto the road network anyway, so a concave district ends
    up on the nearest road to its middle rather than outside itself. Districts with no label are
    skipped — an unnamed marker on the big map is worse than no marker.
    """
    path = reader.layer_path(bundle_dir, "districts")
    if not path or not os.path.isfile(path):
        return []
    out = []
    unnamed = 0
    for record in reader.read_jsonl(path):
        name = (record.get("name") or "").strip()
        centroid = record.get("centroid")
        if not centroid or len(centroid) < 3:
            continue
        if not name:
            unnamed += 1
            continue
        x, y, _z = frame.to_beamng(centroid[0], centroid[1], centroid[2])
        out.append(
            {
                "name": name,
                "at_beamng": (x, y),
                "area": len(record.get("nodes") or ()),
                "description": f"{name} district",
            }
        )
    out.sort(key=lambda p: -p["area"])
    if out or unnamed:
        progress.log(
            f"poi: {len(out)} named CS2 district(s) from districts.jsonl"
            + (f", {unnamed} unnamed skipped" if unnamed else "")
        )
    return out


# -- tier 2: the derived set ---------------------------------------------------------------------


def derive(network, want, avoid):
    """The densest road cells, at least `DERIVED_SEPARATION` apart, named by their streets."""
    if want <= 0 or not len(network.points):
        return []
    labels = getattr(network, "labels", None) or [None] * len(network.points)
    cells = {}
    for i, point in enumerate(network.points):
        key = (int(math.floor(point[0] / DERIVED_CELL)), int(math.floor(point[1] / DERIVED_CELL)))
        bucket = cells.setdefault(key, {"n": 0, "sum": numpy.zeros(2), "names": {}})
        bucket["n"] += 1
        bucket["sum"] += point[:2]
        name = labels[i]
        if name:
            bucket["names"][name] = bucket["names"].get(name, 0) + 1
    ordered = sorted(cells.values(), key=lambda b: -b["n"])
    taken = [numpy.asarray(a, dtype=numpy.float64) for a in avoid]
    out = []
    for bucket in ordered:
        if len(out) >= want:
            break
        centre = bucket["sum"] / bucket["n"]
        if any(float(numpy.linalg.norm(centre - t)) < DERIVED_SEPARATION for t in taken):
            continue
        if not bucket["names"]:
            continue
        name = max(bucket["names"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        taken.append(centre)
        out.append(
            {
                "name": name,
                "at_beamng": (float(centre[0]), float(centre[1])),
                "description": f"{bucket['n']:,} road segments around {name}",
            }
        )
    return out
