"""Stacked-road clearance — a node's radius may not reach a road at a different height.

## Why this is not a tuning knob

A navgraph node's radius is its road's half-width — BeamNG's own convention, and the one
`numOfLanesFromRadius` (map.lua:542) inverts to count lanes. Every consumer of the graph then
reads that radius as *the extent of this road*, and asks whether a position is inside it:

* `startPosLinks` (map.lua:2871) costs a candidate start edge `max(0, 1 - R/|d|)` — so an edge
  whose radius covers the car costs **exactly zero**, and the `wZ = 4` height bias in the next
  line never runs. Two roads that both cover the car are tied, and the router may begin the
  route on either;
* `mergeOverlappingNodes` welds two nodes closer than `max(r1, r2)` in 3-D (map.lua:1211);
* `mergeNodesToLines` splices a node into an edge within `min(radius, node radius)`, in 3-D;
* `resolveXJunctions` joins two crossing edges within `min(r1, r2) * 0.5`, in 3-D.

All four assume a road's radius describes ground the road actually occupies. A 27 m highway
claims a 13.5 m radius, and a viaduct 7 m above it is well inside that — so the engine is
entitled to conclude a car on the street is on the deck. It is the reading that put a route up
onto an overpass with no ramp under it (audit A7.6).

## What it compares, and the first cut got this wrong

A car is not on the centerline; it is anywhere on the **surface**, out to the half-width. So
the constraint is between one road's centerline (which is where its radius is measured from)
and every other road's SURFACE (which is where a car can be). Measuring centerline to
centerline — the first cut — left a deck whose radius still reached the kerb lane beneath it,
and the defect survived the rebuild.

It runs over every emitted road, not just the segments: half the roads stacked over something
are ramps, and a segment clamped only against other segments still reaches them.
"""

import numpy

from . import navmerge, polyline
from .navspine import LANE_HALF_WIDTH

# A road stacked over another by more than this is a DIFFERENT DECK, not the same road on a
# slope. 3 m is below any clearance a city gives a bridge and above any grade change within one
# carriageway; `clearance_clamp` uses it to decide which neighbours a node's radius may not
# reach, and `tools/check_navgraph_vs_game.py` uses the same number for its false-weld question.
STACKED_CLEARANCE = 3.0

# How much of a gap `clearance_clamp` leaves between a node's radius and the deck it must not
# claim. Small on purpose: the constraint is "does not reach", not "keeps its distance".
CLEARANCE_MARGIN = 0.5


def road_segments(key):
    """The `networks.jsonl` segment ids a road's key names. `seg_<id>` -> one; `conn_<a>_<i>_<b>_<j>`
    and `joint_...` -> two, and NOT the end indices `<i>`/`<j>`, which are 0 and 1 on half the
    roads in the level and would make every other pair look adjacent."""
    parts = key.split("_")
    if parts[0] == "seg":
        return frozenset(parts[1:2])
    if parts[0] in ("conn", "joint") and len(parts) == 5:
        return frozenset((parts[1], parts[3]))
    return frozenset()


def surface(road, step):
    """A road's DRIVABLE SURFACE, sampled: the centerline and both kerbs.

    A car is not on the centerline. It is anywhere within the half-width, which is the whole point
    of the radius — so a question about where a car can be has to be asked of the surface, and
    asking it of the centerline is what let `clearance_clamp`'s first cut pass while the defect
    was still in the level.
    """
    centre = polyline.resample(
        numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64), step
    )
    if len(centre) < 2:
        return centre
    half = 0.5 * max(n[3] for n in road.nodes)
    flat = centre.copy()
    flat[:, 1] = 0.0  # the lateral frame is horizontal
    _tangents, normals = polyline.frame(flat)
    return numpy.concatenate((centre, centre + normals * half, centre - normals * half))


def _sampled_roads(roads, step):
    """Every road's centerline and surface samples, each tagged with its road index:
    `(centres, centre_owner, skins, skin_owner)`."""
    centres, centre_owner, skins, skin_owner = [], [], [], []
    for index, road in enumerate(roads):
        line = polyline.resample(
            numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64), step
        )
        centres.append(line)
        centre_owner.append(numpy.full(len(line), index, dtype=numpy.int64))
        skin = surface(road, step)
        skins.append(skin)
        skin_owner.append(numpy.full(len(skin), index, dtype=numpy.int64))
    return (
        numpy.concatenate(centres),
        numpy.concatenate(centre_owner),
        numpy.concatenate(skins),
        numpy.concatenate(skin_owner),
    )


def _radius_limits(roads, half, ends, owned, clearance, step):
    """For each road whose radius reaches another deck's surface, the closest such reach:
    `{road index: gap in metres}`."""
    from scipy.spatial import cKDTree

    centres, centre_owner, skins, skin_owner = _sampled_roads(roads, step)
    radii = numpy.asarray([half[i] for i in centre_owner])
    tree = cKDTree(skins)
    limit = {}
    for point_index, hits in enumerate(tree.query_ball_point(centres, radii)):
        if not hits:
            continue
        i = int(centre_owner[point_index])
        point = centres[point_index]
        for skin_index in hits:
            j = int(skin_owner[skin_index])
            if i == j or (ends[i] & ends[j]) or (owned[i] & owned[j]):
                continue
            if abs(point[1] - skins[skin_index][1]) <= clearance:
                continue
            gap = float(numpy.linalg.norm(point - skins[skin_index]))
            if gap < limit.get(i, numpy.inf):
                limit[i] = gap
    return limit


def _apply_caps(roads, cap, clusters, stats):
    """Shrink each capped road's node widths, keeping every terminal node consistent: every
    road ending on one node takes that node's smallest clamped width, so the terminal-width
    invariant that makes a junction ONE node (A7.5) holds. A road is never widened."""
    node_cap = {}
    for i in range(len(roads)):
        this = cap.get(i, numpy.inf)
        for node in clusters[i]:
            node_cap[node] = min(node_cap.get(node, this), this)

    out = []
    for i, road in enumerate(roads):
        this = cap.get(i, numpy.inf)
        first, last = clusters[i]
        limits = [this] * len(road.nodes)
        limits[0] = min(this, node_cap.get(first, numpy.inf))
        limits[-1] = min(this, node_cap.get(last, numpy.inf))
        # never widen: every node keeps its own width unless its cap is tighter
        nodes = [
            (n[0], n[1], n[2], min(n[3], 2.0 * capped)) for n, capped in zip(road.nodes, limits)
        ]
        if all(new[3] == old[3] for new, old in zip(nodes, road.nodes)):
            out.append(road)
            continue
        stats["widthClearanceClamped"] += 1
        stats["widthClearanceWorst"] = round(
            max(
                stats["widthClearanceWorst"],
                max(old[3] - new[3] for old, new in zip(road.nodes, nodes)),
            ),
            2,
        )
        out.append(road._replace(nodes=nodes))
    return out


def clearance_clamp(roads, clearance=STACKED_CLEARANCE, margin=CLEARANCE_MARGIN, step=4.0):
    """Cap road widths so no node's radius reaches a road at a different height. `(roads, stats)`.

    The reading of the radius this satisfies, and why the comparison is centerline-to-SURFACE,
    are the module docstring. A road is never widened and never goes below one lane's
    half-width.
    """
    stats = {"widthClearanceClamped": 0, "widthClearanceWorst": 0.0}
    if len(roads) < 2:
        return roads, stats
    clusters = navmerge.terminal_clusters(roads)
    ends = [frozenset(pair) for pair in clusters]
    owned = [road_segments(road.key) for road in roads]
    half = [0.5 * max(n[3] for n in road.nodes) for road in roads]

    limit = _radius_limits(roads, half, ends, owned, clearance, step)
    if not limit:
        return roads, stats
    cap = {}
    for i, gap in limit.items():
        capped = max(LANE_HALF_WIDTH, gap - margin)
        if capped < half[i]:
            cap[i] = capped
    if not cap:
        return roads, stats
    return _apply_caps(roads, cap, clusters, stats), stats


def stacked_coverage(roads, clearance=STACKED_CLEARANCE, step=4.0):
    """Every place a car ON one road can stand INSIDE the radius of a road at a different height.

    `[{road, other, gap, radius, height, at}, ...]`, and it should be empty. This is the predicate
    `clearance_clamp` exists to satisfy, checked on the emitted roads rather than on the spines
    it was computed from, and it is the offline form of "the route started on the overpass above
    me": a consumer that decides *which road a car is on* by asking whether the car is inside a
    node's radius answers with the deck (audit A7.6).

    The car's side is sampled as a SURFACE and the deck's as a centerline, because those are the
    two things the engine compares — a position, and a road's radius about its own line.

    Roads that share a terminal node, or a SEGMENT, are exempt. A ramp climbing away from the road
    it joins is stacked over it within a few metres of the junction, and a route that starts on
    either of two roads you can actually drive between is not a wrong route.
    """
    if len(roads) < 2:
        return []
    from scipy.spatial import cKDTree

    clusters = [frozenset(pair) for pair in navmerge.terminal_clusters(roads)]
    owned = [road_segments(road.key) for road in roads]
    half = numpy.asarray([0.5 * max(n[3] for n in road.nodes) for road in roads])
    centres, centre_owner, surfaces, surface_owner = _sampled_roads(roads, step)

    tree = cKDTree(centres)
    out = []
    for point_index, near in enumerate(tree.query_ball_point(surfaces, float(half.max()))):
        i = int(surface_owner[point_index])
        for centre_index in near:
            j = int(centre_owner[centre_index])
            if i == j or (clusters[i] & clusters[j]) or (owned[i] & owned[j]):
                continue
            height = abs(float(surfaces[point_index][1] - centres[centre_index][1]))
            if height <= clearance:
                continue
            gap = float(numpy.linalg.norm(surfaces[point_index] - centres[centre_index]))
            if gap < half[j]:
                out.append(
                    {
                        "road": roads[i].key,
                        "other": roads[j].key,
                        "gap": gap,
                        "radius": float(half[j]),
                        "height": height,
                        "at": [round(float(v), 1) for v in surfaces[point_index]],
                    }
                )
    return out
