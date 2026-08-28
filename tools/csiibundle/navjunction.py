"""Junction synthesis — connectors and joints between segment ends.

The junctions are `car` lanes owned by a net NODE, and `connection` lanes are not the
junctions: of 2,000 sampled road car-lane endpoints, 62.1 % meet another `car` endpoint from a
different owner within 5 cm, and **0.0 % meet a `connection` endpoint** — those are air
corridors and transit links, 200–1,000 m above the roads. A connector road is one turning
movement whose two ends weld (5 cm) onto the lane ends of two *different* segment ends; its
first and last node are set to those two segments' own end points, so the join is **exact by
construction** and no consumer has to rely on a fuzzy merge to close it.
"""

from collections import defaultdict, namedtuple

import numpy

from . import polyline
from .navroad import Road
from .navspine import LANE_HALF_WIDTH

# A `Road` with the pair of segment ends a joint ties together, so `build` can mark both as
# joined without re-deriving them. Never emitted to a consumer; `Road` is what leaves here.
Joint = namedtuple("Joint", (*Road._fields, "key_pair"))


def terminal_widths(count, width, spines, x, y):
    """`[w0, ..., wN]` for a connector or joint: the interior keeps `width`, the two terminal
    nodes take the width of the segment end each is snapped onto.

    A road's terminal node sits at exactly `ends[x]` / `ends[y]`, which is where that segment's
    own road ends too — but a consumer that decides node identity by merging coincident terminal
    nodes compares their RADII as well (BeamNG: `map.lua:1211`, `abs(r1 - r2) < 0.1`). Handing it
    the pair's minimum width leaves the wider of the two segments a different node at the same
    coordinate, which is how a junction ends up as a triangle rather than a point.
    """
    out = [width] * max(count, 2)
    for index, end in ((0, x), (-1, y)):
        spine = spines.get(end[0])
        if spine is not None:
            out[index] = spine.width
    return out


def average_paths(paths):
    """Resample each path to a common station count by arclength fraction and average them."""
    usable = [p for p in paths if polyline.length(p) > 1e-6]
    if not usable:
        return None
    n = max(2, min(32, int(round(max(polyline.length(p) for p in usable) / 4.0)) + 1))
    stacked = [polyline.resample_count(p, n) for p in usable]
    return numpy.mean(stacked, axis=0)


def _match_movements(junction_lanes, end_points, end_keys, tolerance, stats):
    """`(movements, touched)` — junction lanes binned by the `(from_end, to_end)` pair their two
    endpoints weld onto. A lane touching segment ends at one end only is a movement into
    something outside the drivable set and is counted, not emitted."""
    from scipy.spatial import cKDTree

    tree = cKDTree(numpy.asarray(end_points, dtype=numpy.float64))
    movements = defaultdict(list)
    touched = set()
    for path in junction_lanes:
        a = {end_keys[i] for i in tree.query_ball_point(path[0], tolerance)}
        b = {end_keys[i] for i in tree.query_ball_point(path[-1], tolerance)}
        if not a and not b:
            stats["connectorsUnused"] += 1
            continue
        if not a or not b:
            stats["connectorsUnmatched"] += 1
            continue
        pairs = [(x, y) for x in a for y in b if x != y]
        if not pairs:
            stats["connectorsUnmatched"] += 1
            continue
        for x, y in pairs:
            movements[(x, y)].append(numpy.asarray(path, dtype=numpy.float64))
            touched.add(x)
            touched.add(y)
    return movements, touched


def _connector_road(x, y, paths, reverse, ends, attrs, spines, simplify):
    """One `Road` for the movements between segment ends `x` and `y`, or None for a degenerate
    average. A pair with movements both ways is ONE two-way connector; one-way stays one-way,
    so CS2's turn restrictions survive as graph topology."""
    oneway = reverse is None
    origin = ends[x]
    both = list(paths) + [p[::-1] for p in (reverse or ())]
    both = [
        p if polyline.norm_sq(p[0] - origin) <= polyline.norm_sq(p[-1] - origin) else p[::-1]
        for p in both
    ]
    line = average_paths(both)
    if line is None or len(line) < 2:
        return None
    line[0] = ends[x]
    line[-1] = ends[y]
    line = polyline.simplify(line, simplify)
    widths = [spines[s].width for s, _k in (x, y) if s in spines]
    width = min(widths) if widths else 2.0 * LANE_HALF_WIDTH
    # The two terminal nodes take the width of the segment end they were snapped onto, so a
    # consumer's radius-aware node merge fuses them with it (see `navgraph`'s docstring).
    node_widths = terminal_widths(len(line), width, spines, x, y)
    limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
    elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
    return Road(
        key=f"conn_{x[0]}_{x[1]}_{y[0]}_{y[1]}",
        kind="connector",
        segment=x[0],
        name=attrs.get(x[0], {}).get("name"),
        category=attrs.get(x[0], {}).get("category"),
        nodes=[(p[0], p[1], p[2], w) for p, w in zip(line, node_widths)],
        oneway=oneway,
        lanes_left=0 if oneway else 1,
        lanes_right=1,
        speed_kph=min(limits) if limits else None,
        elevated=elevated,
    )


def connectors(junction_lanes, end_points, end_keys, ends, attrs, spines, tolerance, simplify):
    """One road per turning movement between two different segment ends.

    The junction lane's own interior shape is kept; only its two terminal nodes are moved, onto
    the two segments' own centerline ends — which is what makes the join exact instead of
    something a consumer's merge pass has to find.
    """
    stats = {
        "junctionLanes": len(junction_lanes),
        "connectorsUnmatched": 0,
        "connectorsUnused": 0,
        "touched": set(),
    }
    if not end_points:
        stats["connectorsUnused"] = len(junction_lanes)
        return [], stats
    movements, touched = _match_movements(junction_lanes, end_points, end_keys, tolerance, stats)
    stats["touched"] = touched

    # A pair with movements both ways is ONE two-way connector; a pair with movements one way is
    # one one-way connector. Emitting both directions separately would be two parallel edges
    # between the same two nodes, and a consumer keyed on node pairs (BeamNG's `links` table is)
    # keeps only the last of them.
    roads = []
    seen = set()
    for (x, y), paths in sorted(movements.items()):
        pair = (min(x, y), max(x, y))
        if pair in seen:
            continue
        seen.add(pair)
        road = _connector_road(x, y, paths, movements.get((y, x)), ends, attrs, spines, simplify)
        if road is not None:
            roads.append(road)
    return roads, stats


def joints(end_points, end_keys, ends, attrs, spines, tolerance):
    """Two-node roads where two segments abut with no turning movement between them.

    CS2 splits a straight road into segments at nodes that are not junctions at all — a change of
    asset, a bridge abutment, a district boundary — and there is no node-owned lane there because
    there is nothing to turn. Their carriageway lanes weld to each other directly instead
    (measured: 4,424 lane-end pairs on the reference city), and without this the graph falls into
    pieces at every one of them.

    Most of what this emits does not survive `navmerge.simple_graph`, and that is the point:
    1,245 of the reference city's 1,383 joints are ZERO LENGTH — the two segments' centerline
    ends are the same coordinate, so the pair is one node and a road between them joins nothing —
    and another 88 run between a pair a connector already covers. The 50 that remain are the ones
    doing the work: two ends a metre or two apart that nothing else ties together.
    """
    if not end_points:
        return []
    from scipy.spatial import cKDTree

    tree = cKDTree(numpy.asarray(end_points, dtype=numpy.float64))
    pairs = set()
    for i, point in enumerate(end_points):
        for j in tree.query_ball_point(point, tolerance):
            x, y = end_keys[i], end_keys[j]
            if x[0] != y[0]:
                pairs.add((min(x, y), max(x, y)))
    roads = []
    for x, y in sorted(pairs):
        a, b = ends[x], ends[y]
        width = min(spines[x[0]].width, spines[y[0]].width)
        wa, wb = terminal_widths(2, width, spines, x, y)
        limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
        elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
        roads.append(
            Joint(
                key=f"joint_{x[0]}_{x[1]}_{y[0]}_{y[1]}",
                kind="joint",
                segment=x[0],
                name=attrs.get(x[0], {}).get("name"),
                category=attrs.get(x[0], {}).get("category"),
                nodes=[(a[0], a[1], a[2], wa), (b[0], b[1], b[2], wb)],
                oneway=False,
                lanes_left=1,
                lanes_right=1,
                speed_kph=min(limits) if limits else None,
                elevated=elevated,
                key_pair=(x, y),
            )
        )
    return roads
