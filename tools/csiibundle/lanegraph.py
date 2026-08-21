"""The lane graph (`lanes.jsonl`) — the road network as a graph, not as geometry.

This is the layer nothing in this repo consumed until now, and it is the one a **simulation**
consumer wants most. `lanemeshes.jsonl` (see `csiibundle.lanes`) is about drawing lane
FURNITURE — fences, markings, cables. `lanes.jsonl` is different: 680,629 records on the
reference city, one per lane, each a polyline through space with a kind, a length and the
network segment or node that owns it. It is what vehicles and pedestrians actually travel along.

A BeamNG level builder, a traffic simulation, a pathfinding export or a navmesh baker all want
this and none of them want the mesh. Nothing else in the bundle carries it: the network meshes
are surfaces with no direction, no connectivity and no lane count.

    {"id": 55355, "kind": "other", "path": [[x, y, z], …], "length": 37.699, "owner": 262583,
     "limit": 50, "curviness": 0.02, "secondary": true, "flags": ["Twoway", "Yield"]}

**`path` is a polyline, not a Bézier.** The producer has already sampled the lane's curve, so
these are points to be walked, not control points to be evaluated. Do not feed them to
`csiibundle.bezier`.

**`length` is CS2's own curve length, not the polyline's.** Use the shipped value; the polyline
under-measures a curve, as any chord does. `polyline_length` exists to CHECK it, not to replace
it.

**`owner` is an advisory grouping key, not a join** *(contract §5A, corrected 2026-08-14, H10)*.
The producer writes `Game.Common.Owner.m_Owner.Index` verbatim — whatever entity owns the lane.
Measured on the reference city: of 677,566 lanes carrying an owner, 21.7% resolve into
`networks.jsonl`, 19.1% into `instances.jsonl` (lot-owned parking and pedestrian lanes) and
**59.2% into neither**, because the commonest owner is a net NODE and the bundle emits no node
records (§5.1). A further 3,063 lanes carry no owner at all. Node-owned lanes are the turning
movements THROUGH a junction — the connections that make the graph a graph — so never filter
a lane out for an owner that does not resolve. `by_owner()` groups the owner-less under `None`;
`attach_networks()` yields `None` for the ~78% that are not edge-owned, which is normal.

**That is about the OWNER and not about the `kind`, and the two were conflated in this module
until A7 measured them apart.** The junction movements are node-owned **`car`** lanes — 40,708 of
them bridge two different road segments' lane ends within 5 cm on the reference city — and they
are what makes the drivable graph connected. `kind == "connection"` is something else entirely;
see `DRIVABLE_KINDS` below for what it turned out to be.

**The other four fields are what a simulation actually needs**, and every one of them is
optional so a consumer must handle absence:

* `limit` — speed limit in **km/h**, on the kinds that have one (`car`, `track`).
* `flags` — lane-type flag **NAMES** (`Twoway`, `Yield`, `Runway`, …), deliberately not a bit
  field: CS2's numeric values are internals that can change meaning between game versions. The
  set is **open** — ignore what you do not recognise rather than rejecting the record.
* `secondary` — `true` where CS2 derived this lane from a master lane rather than placing it.
  **Both are emitted**, which is a choice a consumer has to make: laying out traffic wants every
  lane a vehicle can occupy, drawing lane lines wants only the masters. `masters()` filters.
* `curviness` — how much the lane bends, on CS2's own scale, carried rather than recomputed.

**For a ROUTE graph, use `csiibundle.navgraph` instead of this module.** It answers "what is the
road network, as roads, with a node shared wherever two roads meet?" — the segment centerlines,
the turning movements between them and the connectivity — where this one hands back the lane
records as they are. It also documents the three traps in this layer that cost a day to find:
`SideConnection` marks a driveway stub, `Invert` is a lane's direction relative to its segment,
and a segment's car lanes are stacked laterally AND chained longitudinally.

Positions are bundle-frame metres (X east, Y up, Z north). Convert with `csiibundle.frames`.
"""

import itertools
from collections import defaultdict

from . import reader

# The closed `kind` set in schema v1. `"other"` is the producer's own catch-all, and an
# unrecognised value should still be carried rather than dropped. CONTRACT.
KINDS = ("car", "track", "pedestrian", "parking", "connection", "other")

# The kinds a vehicle can occupy — what a traffic or routing consumer wants. `pedestrian` and
# `parking` are not.
#
# **`connection` is NOT the road junctions**, though this module said it was until A7 measured it
# (audit A7.1, 2026-08-19). Of 3,000 sampled road car-lane endpoints, **0.0 % have a `connection`
# endpoint within 5 cm** (0.2 % within 0.5 m) while **62.1 % have another `car` endpoint from a
# different owner** there; sampled `connection` lanes sit at y = 721–1612 m against the roads'
# 508–594 m. They are air corridors and transit links. THE JUNCTIONS ARE `car` LANES, owned by a
# net node — which is what `csiibundle.navgraph` builds the drivable graph from. `connection` is
# kept in this tuple because a transit consumer wants it and because the set is a statement about
# what a vehicle occupies, not about what a road junction is.
DRIVABLE_KINDS = ("car", "track", "connection")


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
    thousands) as `lanes`. A lane whose owner is not in the network layer yields `None` — and on
    a full bundle that is the COMMON case (~78%: node-owned and lot-owned lanes, §5A), not a
    defect. Only edge-owned lanes join.
    """
    owners = {record["id"]: record for record in networks if "id" in record}
    for lane in lanes:
        yield lane, owners.get(lane.get("owner"))


def masters(lanes):
    """Only the lanes CS2 placed, dropping the ones it derived from a master (`secondary`).

    The right filter for drawing lane lines and the WRONG one for traffic: a derived lane is
    still a lane a vehicle occupies. See the module docstring.
    """
    return (lane for lane in lanes if not lane.get("secondary"))


def drivable(lanes):
    """Only the lanes a vehicle can occupy, by `kind`. Keeps derived lanes."""
    return (lane for lane in lanes if lane.get("kind") in DRIVABLE_KINDS)


def has_flag(lane, name):
    """Whether a lane carries a named flag. Absent `flags` reads as "no", not as an error."""
    return name in (lane.get("flags") or ())


def polyline_length(path):
    """The summed segment length of a lane's `path`, in metres.

    The record ships its own `length` and that is the game's number — use it. This exists to
    CHECK it: a large disagreement means the path was resampled or truncated somewhere between
    the game and here, which is worth knowing before routing anything over it.
    """
    total = 0.0
    for a, b in itertools.pairwise(path):
        total += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2) ** 0.5
    return total


def summarise(bundle_dir):
    """`{kind: count}` plus totals — a cheap "what is in this layer" pass over the whole file."""
    counts, flags, total_length, owned, secondary, limited = {}, {}, 0.0, 0, 0, 0
    n = 0
    for lane in read_lanes(bundle_dir):
        n += 1
        counts[lane.get("kind")] = counts.get(lane.get("kind"), 0) + 1
        total_length += float(lane.get("length") or 0.0)
        owned += lane.get("owner") is not None
        secondary += bool(lane.get("secondary"))
        limited += lane.get("limit") is not None
        for flag in lane.get("flags") or ():
            flags[flag] = flags.get(flag, 0) + 1
    return {
        "lanes": n,
        "kinds": counts,
        "flags": flags,
        "total_length_m": total_length,
        "with_owner": owned,
        "secondary": secondary,
        "with_limit": limited,
    }
