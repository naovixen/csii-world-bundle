"""The segment centerline — one carriageway spine per drivable network segment.

A segment's `car` lanes are stacked laterally AND chained longitudinally: a 120 m 4-lane
'Medium Road' ships twelve of them — six lateral offsets duplicated across a split at the
segment's midpoint. Averaging all twelve *as polylines* produces a diagonal; they have to be
resolved into a spine plus one lateral offset, which is what `segment_centerline` does.
"""

from collections import defaultdict, namedtuple

import numpy

from . import polyline

# Half of CS2's own car lane width, used to turn a set of lane centre offsets into a carriageway
# half-width. MEASURED: lateral offsets inside a segment step by 1.5 m and 3.0 m, and the outer
# lane centre sits one half-lane inside the kerb.
LANE_HALF_WIDTH = 1.75

# The spine is resampled at this spacing before the lateral offset is applied, and simplified
# back afterwards. 2 m keeps a 10 m-radius junction curve to under a centimetre of chord error.
SPINE_STEP = 2.0

Spine = namedtuple(
    "Spine",
    "points width measured_width lanes_left lanes_right "
    "start_lane_points end_lane_points forward_side",
)


def _lane_counts(lanes, forward, attrs):
    """`(left, right)` — lanes against / with the segment's direction.

    `Invert` counts the lanes running against the segment's own direction; a lane chained into
    two pieces is still one lane, so the raw counts divide by the number of chain links the
    spine needed (how many longitudinal pieces the segment was split into).

    RECONCILED WITH THE ASSET'S OWN LANE COUNT, because the two answer different questions.
    CS2 places car lanes at 1.5 m as well as 3.0 m spacing — a 24 m 'Medium Road' the asset
    calls 4 lanes carries six lateral offsets each way — so counting lanes gives the traffic
    model's subdivisions, not the road's markings. `attrs.lanes` is the authored total across
    both directions and is what a driver sees; `Invert` is the only thing that says how it
    splits. So the RATIO is measured and the TOTAL is the game's.
    """
    left = sum(1 for _path, inverted in lanes if inverted)
    right = len(lanes) - left
    pieces = max(1, chain_pieces(forward))
    left = max(0, int(round(left / pieces)))
    right = max(1, int(round(right / pieces)))
    total = attrs.get("lanes")
    if isinstance(total, (int, float)) and total >= 1:
        total = int(total)
        if attrs.get("direction") == "oneway" or left == 0:
            left, right = 0, total
        else:
            share = right / float(left + right)
            right = max(1, int(round(total * share)))
            left = max(1, total - right)
    return left, right


def _forward_side(lanes, offsets):
    """WHICH SIDE TRAFFIC DRIVES ON, and it is a measurement rather than a setting.

    The lanes running with the segment sit to one side of the ones running against it, and
    which side that is IS the rule. The sign convention is calibrated against CS2's own
    `TurnRight` / `TurnLeft` lane flags: over 21,192 flagged junction lanes, a right turn
    sweeps a NEGATIVE cross product of (east, north) on 95.3 % and a left turn a positive one
    on 91.3 %, so the right-hand normal of a tangent (tx, tz) is (tz, -tx). Reported, never
    assumed: on the reference city 5,731 segments put the forward lanes right and 2 put them
    left.
    """
    with_flow = offsets[[i for i, (_p, inv) in enumerate(lanes) if not inv]]
    against = offsets[[i for i, (_p, inv) in enumerate(lanes) if inv]]
    if not (with_flow.size and against.size):
        return 0
    # `polyline.frame` returns the LEFT-hand normal ((-tz, tx) for a tangent (tx, tz)), so a
    # LARGER offset means further left: +1 = the with-flow lanes are on the left = left-hand
    # traffic
    return 1 if float(with_flow.mean()) > float(against.mean()) else -1


def segment_centerline(lanes, attrs, max_width):
    """The carriageway centerline of one segment, from its own car lanes.

    1. Orient every lane to the segment's forward direction using `Invert`.
    2. Chain them end to end (they weld exactly at CS2's own split points) and keep the longest
       chain — that is the spine, one lane wide, at some lateral offset.
    3. Measure every lane's signed lateral offset from that spine and shift the spine by the
       length-weighted mean of them: the carriageway centre.
    4. The half-width is the widest offset from that centre plus a half lane.
    """
    forward = [(path[::-1] if inverted else path) for path, inverted in lanes]
    if not forward:
        return None
    spine = longest_chain(forward)
    if spine is None or len(spine) < 2:
        return None
    spine = polyline.resample(spine, SPINE_STEP)
    _tangents, normals = polyline.frame(spine)

    offsets = []
    weights = []
    for path in forward:
        mid = path[len(path) // 2]
        i = polyline.nearest_station(spine, mid)
        offsets.append(float(numpy.dot(mid - spine[i], normals[i])))
        weights.append(float(numpy.sum(numpy.linalg.norm(numpy.diff(path, axis=0), axis=1))))
    weights = numpy.asarray(weights)
    offsets = numpy.asarray(offsets)
    total = weights.sum()
    centre = float((offsets * weights).sum() / total) if total > 1e-9 else float(offsets.mean())
    half = float(numpy.abs(offsets - centre).max()) + LANE_HALF_WIDTH
    measured = 2.0 * half
    width = measured if max_width is None else min(measured, float(max_width))

    points = spine + normals * centre
    # `Invert` counts the lanes running against the segment's own direction. Both non-zero means
    # this one segment carries both carriageways and the centerline is the average of them.
    left, right = _lane_counts(lanes, forward, attrs)
    start_pts, end_pts = terminal_lane_points(forward, spine)
    return Spine(
        points=points,
        width=width,
        measured_width=measured,
        lanes_left=left,
        lanes_right=right,
        forward_side=_forward_side(lanes, offsets),
        start_lane_points=start_pts,
        end_lane_points=end_pts,
    )


def longest_chain(paths):
    """The longest run of lanes welded end to end, as one polyline.

    CS2 splits a segment's lanes at interior nodes and every piece's start is bit-close to the
    previous piece's end, so the chain is exact rather than a nearest-neighbour guess.
    """
    starts = {}
    for i, path in enumerate(paths):
        starts.setdefault(polyline.weld_key(path[0]), []).append(i)
    best = None
    best_len = -1.0
    for i, path in enumerate(paths):
        chain = [i]
        used = {i}
        total = polyline.length(path)
        tail = path[-1]
        while True:
            nxt = None
            for j in starts.get(polyline.weld_key(tail), ()):
                if j not in used:
                    nxt = j
                    break
            if nxt is None:
                break
            used.add(nxt)
            chain.append(nxt)
            total += polyline.length(paths[nxt])
            tail = paths[nxt][-1]
        if total > best_len:
            best_len = total
            best = chain
    if not best:
        return None
    out = [paths[best[0]]]
    for j in best[1:]:
        out.append(paths[j][1:])
    return numpy.vstack(out)


def chain_pieces(paths):
    """How many longitudinal pieces the segment's lanes are split into — the length of the
    longest chain. Used to turn a lane COUNT into a lanes-across-the-road count."""
    starts = {}
    for i, path in enumerate(paths):
        starts.setdefault(polyline.weld_key(path[0]), []).append(i)
    best = 1
    for i, path in enumerate(paths):
        used = {i}
        tail = path[-1]
        count = 1
        while True:
            nxt = next((j for j in starts.get(polyline.weld_key(tail), ()) if j not in used), None)
            if nxt is None:
                break
            used.add(nxt)
            count += 1
            tail = paths[nxt][-1]
        best = max(best, count)
    return best


def terminal_lane_points(paths, spine):
    """The lane endpoints at each END of the segment, as `(start_points, end_points)`.

    A lane's endpoint at an INTERIOR split is not an end of the segment, and welding a junction
    lane onto one would invent a junction in the middle of a road. The split is found from CS2's
    own chaining rather than from a distance: a weld key where some lane starts *and* some lane
    ends is interior, because that is what a split is. Everything else is a terminus, and which
    of the two it belongs to is decided by the nearer end of the spine (all lanes are already
    oriented forward, so in a well-formed segment every start is at one end and every end at the
    other; the nearest-terminus test is what keeps a lane with a missing `Invert` from being
    counted at the wrong end).
    """
    starting, ending = defaultdict(int), defaultdict(int)
    for path in paths:
        starting[polyline.weld_key(path[0])] += 1
        ending[polyline.weld_key(path[-1])] += 1
    interior = {k for k in starting if k in ending}
    head, tail = spine[0], spine[-1]
    start, end = [], []
    for path in paths:
        for point in (path[0], path[-1]):
            if polyline.weld_key(point) in interior:
                continue
            near_head = polyline.norm_sq(point - head) <= polyline.norm_sq(point - tail)
            (start if near_head else end).append(point)
    return start, end
