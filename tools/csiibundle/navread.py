"""Reading the navgraph's inputs out of the bundle — one pass over each layer.

`lanes.jsonl` is 242 MB on the reference city, so `read_car_lanes` makes exactly one pass and
holds the junction lanes as float32 arrays while the segment spines are built
(`csiibundle.navspine`). The flags it honours are the game's own: `SideConnection` marks a
driveway stub (dropped — the flag is the game's answer and a length threshold would be ours)
and `Invert` a lane's direction relative to its segment.
"""

from collections import defaultdict

import numpy

from . import reader


def read_segments(bundle_dir, categories):
    """`({id: attrs} for the wanted categories, frozenset of EVERY network id)`.

    One pass serves both questions: `read_car_lanes` needs the full id set to tell a node-owned
    lane from a segment-owned one, and used to walk `networks.jsonl` a second time for it (D6).
    """
    out = {}
    ids = set()
    path = reader.layer_path(bundle_dir, "networks")
    if not path:
        return out, frozenset()
    for record in reader.read_jsonl(path):
        if "id" in record:
            ids.add(record["id"])
        a = record.get("attrs") or {}
        if a.get("category") in categories:
            out[record["id"]] = a
    return out, frozenset(ids)


def read_car_lanes(bundle_dir, segment_ids, progress=None, networks=None):
    """One pass over `lanes.jsonl`.

    Returns `(seg_lanes, junction_lanes, counts)`:

    * `seg_lanes[sid]` — `[(path, inverted), ...]` for the CARRIAGEWAY lanes of that segment
      (`SideConnection` dropped);
    * `junction_lanes` — `[path, ...]` for every `car` lane owned by something that is not a
      network record at all. That is the node-owned set (the turning movements) plus the
      lot-owned set (driveways and parking aisles); which is which is decided later, by whether
      a lane's two ends land on two different segment ends, and the lot-owned ones fall out
      because they do not.
    """
    seg_lanes = defaultdict(list)
    junction_lanes = []
    counts = {"carLanes": 0, "segmentOwned": 0, "sideConnections": 0}
    if networks is None:
        networks = network_ids(bundle_dir)
    n = 0
    for lane in reader.read_jsonl(reader.layer_path(bundle_dir, "lanes")):
        n += 1
        if progress is not None and n % 50000 == 0:
            progress.advance(50000)
            progress.check()
        if lane.get("kind") != "car":
            continue
        counts["carLanes"] += 1
        path = lane.get("path") or []
        if len(path) < 2:
            continue
        owner = lane.get("owner")
        if owner in segment_ids:
            counts["segmentOwned"] += 1
            flags = lane.get("flags") or ()
            if "SideConnection" in flags:
                counts["sideConnections"] += 1
                continue
            seg_lanes[owner].append((numpy.asarray(path, dtype=numpy.float64), "Invert" in flags))
        elif owner not in networks:
            junction_lanes.append(numpy.asarray(path, dtype=numpy.float32))
    if progress is not None:
        progress.advance(n % 50000)
    return dict(seg_lanes), junction_lanes, counts


def network_ids(bundle_dir):
    """Every id in `networks.jsonl`, drivable or not."""
    path = reader.layer_path(bundle_dir, "networks")
    if not path:
        return frozenset()
    return frozenset(record["id"] for record in reader.read_jsonl(path) if "id" in record)
