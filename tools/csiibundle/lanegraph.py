"""The lane graph (`lanes.jsonl`) — the road network as a graph, not as geometry.

This is the layer nothing in this repo consumed until now, and it is the one a **simulation**
consumer wants most. `lanemeshes.jsonl` (see `csiibundle.lanes`) is about drawing lane
FURNITURE — fences, markings, cables. `lanes.jsonl` is different: 680,629 records on the
reference city, one per lane, each a polyline through space with a kind, a length and the
network segment or node that owns it. It is what vehicles and pedestrians actually travel along.

A BeamNG level builder, a traffic simulation, a pathfinding export or a navmesh baker all want
this and none of them want the mesh. Nothing else in the bundle carries it: the network meshes
are surfaces with no direction, no connectivity and no lane count.

    {"id": 55355, "kind": "other", "path": [[x, y, z], …], "length": 37.699, "owner": 262583}

**`path` is a polyline, not a Bézier.** The producer has already sampled the lane's curve, so
these are points to be walked, not control points to be evaluated. Do not feed them to
`csiibundle.bezier`.

**`owner` is the join key**, and it points at the network record that owns the lane — the
segment it runs along or the node it crosses. `by_owner()` builds the index; `attach_networks()`
does the join. Lanes on a node are the turning movements THROUGH a junction, which is what makes
the graph connected: without them each segment's lanes are an island.

Positions are bundle-frame metres (X east, Y up, Z north). Convert with `csiibundle.frames`.
"""

from collections import defaultdict

from . import reader

# `kind` values seen on the reference city. NOT a closed set — the producer writes the game's own
# lane classification through, and an unknown kind should be carried, not dropped. `"other"` is
# the producer's own catch-all.
KNOWN_KINDS = ("vehicle", "pedestrian", "track", "parking", "service", "other")


def read_lanes(bundle_dir):
    """Stream `lanes.jsonl` — one dict per lane. A generator: a full city is 680k records."""
    return reader.read_jsonl(reader.layer_path(bundle_dir, "lanes"))


def by_owner(lanes):
    """`{owner: [lane, …]}` — every lane grouped by the network record that owns it.

    The index a consumer needs before it can ask "what lanes does this segment carry?". Lanes
    whose record has no `owner` are grouped under `None` rather than dropped, so nothing goes
    missing silently.
    """
    index = defaultdict(list)
    for lane in lanes:
        index[lane.get("owner")].append(lane)
    return dict(index)


def attach_networks(lanes, networks):
    """Yield `(lane, network_record_or_None)` for each lane, joined on `owner`.

    `networks` is an iterable of §5 network records; it is materialised into a dict by `id`, so
    pass the network layer (thousands of records) here and stream the lane layer (hundreds of
    thousands) as `lanes`. A lane whose owner is not in the network layer yields `None` — normal
    when the caller filtered the networks to a region, and a bundle defect otherwise.
    """
    owners = {record["id"]: record for record in networks if "id" in record}
    for lane in lanes:
        yield lane, owners.get(lane.get("owner"))


def polyline_length(path):
    """The summed segment length of a lane's `path`, in metres.

    The record ships its own `length` and that is the game's number — use it. This exists to
    CHECK it: a large disagreement means the path was resampled or truncated somewhere between
    the game and here, which is worth knowing before routing anything over it.
    """
    total = 0.0
    for a, b in zip(path, path[1:]):
        total += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2) ** 0.5
    return total


def summarise(bundle_dir):
    """`{kind: count}` plus totals — a cheap "what is in this layer" pass over the whole file."""
    counts, total_length, owned = {}, 0.0, 0
    n = 0
    for lane in read_lanes(bundle_dir):
        n += 1
        counts[lane.get("kind")] = counts.get(lane.get("kind"), 0) + 1
        total_length += float(lane.get("length") or 0.0)
        owned += lane.get("owner") is not None
    return {"lanes": n, "kinds": counts, "total_length_m": total_length, "with_owner": owned}
