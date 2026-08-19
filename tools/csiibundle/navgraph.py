"""The **navigation graph** — the drivable road network as a routable centerline graph.

This is `csiibundle.lanegraph`'s consumer-facing companion. `lanegraph` hands back the bundle's
680k lane records as they are; this module answers the question every simulation consumer
actually asks: *what is the road network, as roads, with a node shared wherever two roads meet?*
A BeamNG navgraph, an OSM export, a traffic model and a pathfinder all want that one shape, so
it lives here rather than in either consumer (CLAUDE.md: a fix that only one consumer gets is in
the wrong place).

Bundle frame in, bundle frame out (X east, Y up, Z north); convert at the consumer boundary with
`csiibundle.frames`.

## Why the geometry comes from `lanes.jsonl` and not from `networks.jsonl`

Only 970 of the reference city's 8,259 network records carry a `centerline` — the 7,289 that
have a mesh do not (§5). So the geometry has to be derived from the lane layer, and the lane
layer has a shape worth writing down, because every naive reading of it is wrong:

* **A segment's `car` lanes are stacked laterally AND chained longitudinally.** A 120 m
  4-lane 'Medium Road' ships twelve of them: six lateral offsets (0, −1.5, −3, −6, −7.5, −9 m)
  duplicated across a split at the segment's midpoint. Averaging all twelve *as polylines*
  produces a diagonal; they have to be resolved into a spine plus one lateral offset.
* **`SideConnection` is the game's own flag for a driveway stub.** 26,644 of the 52,974
  segment-owned car lanes carry it (95.1 % of everything shorter than a third of its segment):
  they are the 10 m hooks that cross the pavement into a lot, at lateral offsets outside the
  carriageway, and they are not the road. They are dropped by that flag, not by a length
  threshold — the flag is the game's answer and a threshold would be ours.
* **`Invert` is the lane's direction relative to its segment.** It is what makes "how many lanes
  run each way" a read rather than a guess, and it is what orients the spine.
* **The junctions are `car` lanes owned by a net NODE, and `connection` lanes are not the
  junctions.** Of 2,000 sampled road car-lane endpoints, 62.1 % meet another `car` endpoint from
  a different owner within 5 cm, and **0.0 % meet a `connection` endpoint**. `connection` lanes
  are air corridors and transit links, 200–1,000 m above the roads. See `lanegraph`'s docstring,
  which said the opposite until this module measured it.

## The graph this builds

Two kinds of edge, and **no invented node position anywhere**:

* A **segment road** per drivable network segment: the spine of its carriageway lanes, offset
  laterally onto the carriageway centre, from one end of the segment to the other.
* A **connector road** per turning movement: a node-owned `car` lane whose two ends weld (5 cm)
  onto the lane ends of two *different* segment ends. Its first and last node are set to those
  two segments' own end points, so the join is **exact by construction** and no consumer has to
  rely on a fuzzy merge to close it. Movements that share an end pair are averaged into one
  connector, and a pair with movements in only one direction stays one-way — so CS2's turn
  restrictions survive as graph topology rather than being collapsed away.

Measured on the 2026-08-19 16:32 reference city: 6,667 segment roads and 10,070 connectors,
~69k nodes, **96.3 % of segments in one connected component** over 29 components.

The alternative — collapsing each junction to one hub node and running every centerline into it
— was built first and measured: the hubs chain through shared junction owners into blobs with a
median horizontal extent of 33 m and a maximum of 896 m, which turns one motorway interchange
into a star of half-kilometre straight lines. It is not in this module and the numbers are here
so nobody rebuilds it.

## What is approximated, and it is counted

`build()` returns a `Navgraph` whose `stats` carries every one of these as a number, and the
consumer is expected to print them (CLAUDE.md: an undeclared approximation is indistinguishable
from a bug once its artefact appears):

* `dividedCarriagewaysAveraged` — a segment whose lanes run both ways becomes ONE two-way
  centerline down the middle of them. Where CS2 models the two carriageways as separate segments
  (as it does for divided highways) nothing is lost; where it models them as one, the median
  strip is not represented.
* `sideConnectionsDropped` — driveway stubs, excluded above.
* `endsWithNoJunction` — a segment end that welds to nothing: a genuine dead end, or a join this
  module failed to find.
* `connectorsUnmatched` — node-owned car lanes that touch a segment's lanes at one end only.
  They are turning movements into something outside the drivable set (a parking lot, a
  pedestrian path) and are not emitted.
* `widthClamped` — segments whose carriageway half-width was reduced to `max_width / 2`.
* `speedInferred` — segments with no `attrs.limit`; the consumer is left to auto-calculate.
"""

import math
from collections import defaultdict, namedtuple

import numpy

from . import reader

# The drivable network categories, and the only ones this module will build a road for by
# default. CS2's own `attrs.category` values; `taxiway` is offered because an airport is
# drivable, and everything else in the layer (`pathway`, `tram`, `train`, `waterway`, `cable`,
# `pipe`) is not a car road and is never in the graph.
DEFAULT_CATEGORIES = ("road", "highway")
OPTIONAL_CATEGORIES = ("taxiway",)

# The weld tolerance between two lane endpoints that the game placed at the same point. CS2
# emits them bit-identical or within a millimetre; 5 cm is slack, and the sweep behind it is in
# `docs/plans/a7-navigation.md` (the matched fraction is flat from 5 cm to 50 cm, so the
# tolerance is not doing any work and nothing is being welded that is not already coincident).
WELD_TOLERANCE = 0.05

# Half of CS2's own car lane width, used to turn a set of lane centre offsets into a carriageway
# half-width. MEASURED: lateral offsets inside a segment step by 1.5 m and 3.0 m, and the outer
# lane centre sits one half-lane inside the kerb.
LANE_HALF_WIDTH = 1.75

# The spine is resampled at this spacing before the lateral offset is applied, and simplified
# back afterwards. 2 m keeps a 10 m-radius junction curve to under a centimetre of chord error.
SPINE_STEP = 2.0

# Douglas-Peucker tolerance for the emitted polyline. A navgraph node every 2 m is a waste of a
# consumer's kd-tree; 0.25 m is a quarter of a lane marking's width and invisible to routing.
SIMPLIFY_TOLERANCE = 0.25

Road = namedtuple(
    "Road",
    "key kind segment name category nodes oneway lanes_left lanes_right speed_kph elevated",
)
Road.__doc__ = """One edge of the navgraph.

* `key` — unique and stable within one build (`seg_<id>` / `conn_<a>_<b>`).
* `kind` — `"segment"` or `"connector"`.
* `segment` — the `networks.jsonl` id this came from (the FROM segment for a connector).
* `nodes` — `[(x, y, z, width), ...]` in the BUNDLE frame. `width` is the full carriageway
  width, so a consumer wanting a radius halves it.
* `oneway` — traffic runs from `nodes[0]` towards `nodes[-1]` only.
* `lanes_left` / `lanes_right` — lanes against / with that direction, counted from `Invert`.
* `speed_kph` — CS2's own `attrs.limit`, or None where it ships none.
* `elevated` — the segment's `attrs.form` is `elevated` or `tunnel`: a consumer that welds nodes
  by proximity must not weld this road's interior to whatever it passes over or under.
"""

Navgraph = namedtuple("Navgraph", "roads stats notes")

# A `Road` with the pair of segment ends a joint ties together, so `build` can mark both as
# joined without re-deriving them. Never emitted to a consumer; `Road` is what leaves here.
_Joint = namedtuple("_Joint", Road._fields + ("key_pair",))


def build(bundle_dir, categories=DEFAULT_CATEGORIES, weld_tolerance=WELD_TOLERANCE,
          max_width=None, simplify=SIMPLIFY_TOLERANCE, progress=None):
    """Build the navgraph. `categories` are `attrs.category` values; `max_width` clamps the
    emitted carriageway width (None = the measured value, whatever it is).

    Two passes over `lanes.jsonl` are deliberately avoided — the layer is 242 MB — so the
    junction lanes are held as float32 arrays while the segment spines are built.
    """
    log = progress.log if progress is not None else (lambda _text: None)
    wanted = tuple(categories)
    attrs = _read_segments(bundle_dir, wanted)
    log(f"navgraph: {len(attrs):,} {'/'.join(wanted)} segments in networks.jsonl")

    seg_lanes, junction_lanes, counts = _read_car_lanes(bundle_dir, set(attrs), progress)
    log(f"navgraph: {counts['carLanes']:,} car lanes - {counts['segmentOwned']:,} on those "
        f"segments ({counts['sideConnections']:,} SideConnection stubs dropped), "
        f"{len(junction_lanes):,} node-owned")

    stats = {"segments": len(attrs), "segmentsWithLanes": len(seg_lanes)}
    stats.update(counts)

    spines = {}
    for sid, lanes in seg_lanes.items():
        spine = _segment_centerline(lanes, attrs[sid], max_width)
        if spine is not None:
            spines[sid] = spine
    stats["segmentsWithoutCenterline"] = len(seg_lanes) - len(spines)

    roads = []
    ends = {}                       # (sid, 0|1) -> end point of the emitted centerline
    end_lane_points = []            # every carriageway lane endpoint AT a segment end
    end_lane_key = []
    divided = 0
    clamped = 0
    speed_inferred = 0
    for sid, spine in spines.items():
        a = attrs[sid]
        nodes = _simplify(spine.points, simplify)
        width = spine.width
        if max_width is not None and spine.measured_width > max_width + 1e-6:
            clamped += 1
        if spine.lanes_left and spine.lanes_right:
            divided += 1
        limit = a.get("limit")
        if limit is None:
            speed_inferred += 1
        roads.append(Road(
            key=f"seg_{sid}", kind="segment", segment=sid, name=a.get("name"),
            category=a.get("category"), nodes=[(p[0], p[1], p[2], width) for p in nodes],
            oneway=(spine.lanes_left == 0), lanes_left=spine.lanes_left,
            lanes_right=spine.lanes_right, speed_kph=limit,
            elevated=a.get("form") in ("elevated", "tunnel")))
        ends[(sid, 0)] = spine.points[0]
        ends[(sid, 1)] = spine.points[-1]
        for k, pts in ((0, spine.start_lane_points), (1, spine.end_lane_points)):
            for p in pts:
                end_lane_points.append(p)
                end_lane_key.append((sid, k))
    stats["dividedCarriagewaysAveraged"] = divided
    stats["widthClamped"] = clamped
    stats["speedInferred"] = speed_inferred
    stats["forwardLanesRight"] = sum(1 for s in spines.values() if s.forward_side < 0)
    stats["forwardLanesLeft"] = sum(1 for s in spines.values() if s.forward_side > 0)
    stats["leftHandTraffic"] = stats["forwardLanesLeft"] > stats["forwardLanesRight"]

    connectors, conn_stats = _connectors(junction_lanes, end_lane_points, end_lane_key, ends,
                                         attrs, spines, weld_tolerance, simplify, max_width)
    roads.extend(connectors)
    touched = conn_stats.pop("touched")
    joints = _joints(end_lane_points, end_lane_key, ends, attrs, spines, weld_tolerance)
    roads.extend(Road(*joint[:len(Road._fields)]) for joint in joints)
    touched.update(k for joint in joints for k in joint.key_pair)
    stats["joints"] = len(joints)
    stats.update(conn_stats)
    stats["endsWithNoJunction"] = sum(1 for key in ends if key not in touched)
    stats["roads"] = len(roads)
    stats["nodes"] = sum(len(r.nodes) for r in roads)
    stats["connectors"] = len(connectors)

    stats.update(_components(roads))
    notes = _notes(stats, max_width)
    for note in notes:
        log(note)
    return Navgraph(roads=roads, stats=stats, notes=notes)


# -- the bundle read ---------------------------------------------------------------------------

def _read_segments(bundle_dir, categories):
    """`{id: attrs}` for every network segment in the wanted categories."""
    out = {}
    path = reader.layer_path(bundle_dir, "networks")
    if not path:
        return out
    for record in reader.read_jsonl(path):
        a = record.get("attrs") or {}
        if a.get("category") in categories:
            out[record["id"]] = a
    return out


def _read_car_lanes(bundle_dir, segment_ids, progress=None):
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
    networks = _network_ids(bundle_dir)
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
            seg_lanes[owner].append((numpy.asarray(path, dtype=numpy.float64),
                                     "Invert" in flags))
        elif owner not in networks:
            junction_lanes.append(numpy.asarray(path, dtype=numpy.float32))
    if progress is not None:
        progress.advance(n % 50000)
    return dict(seg_lanes), junction_lanes, counts


def _network_ids(bundle_dir):
    path = reader.layer_path(bundle_dir, "networks")
    if not path:
        return frozenset()
    return frozenset(record["id"] for record in reader.read_jsonl(path) if "id" in record)


# -- the segment centerline --------------------------------------------------------------------

Spine = namedtuple("Spine", "points width measured_width lanes_left lanes_right "
                            "start_lane_points end_lane_points forward_side")


def _segment_centerline(lanes, attrs, max_width):
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
    spine = _longest_chain(forward)
    if spine is None or len(spine) < 2:
        return None
    spine = _resample(spine, SPINE_STEP)
    tangents, normals = _frame(spine)

    offsets = []
    weights = []
    for path in forward:
        mid = path[len(path) // 2]
        i = _nearest_station(spine, mid)
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
    left = sum(1 for _path, inverted in lanes if inverted)
    right = len(lanes) - left
    # a lane chained into two pieces is still one lane: divide by the number of chain links the
    # spine needed, which is how many longitudinal pieces the segment was split into
    pieces = max(1, _chain_pieces(forward))
    left = max(0, int(round(left / pieces)))
    right = max(1, int(round(right / pieces)))
    # RECONCILED WITH THE ASSET'S OWN LANE COUNT, because the two answer different questions.
    # CS2 places car lanes at 1.5 m as well as 3.0 m spacing — a 24 m 'Medium Road' the asset
    # calls 4 lanes carries six lateral offsets each way — so counting lanes gives the traffic
    # model's subdivisions, not the road's markings. `attrs.lanes` is the authored total across
    # both directions and is what a driver sees; `Invert` is the only thing that says how it
    # splits. So the RATIO is measured and the TOTAL is the game's.
    total = attrs.get("lanes")
    if isinstance(total, (int, float)) and total >= 1:
        total = int(total)
        if attrs.get("direction") == "oneway" or left == 0:
            left, right = 0, total
        else:
            share = right / float(left + right)
            right = max(1, int(round(total * share)))
            left = max(1, total - right)

    # WHICH SIDE TRAFFIC DRIVES ON, and it is a measurement rather than a setting. The lanes
    # running with the segment sit to one side of the ones running against it, and which side
    # that is IS the rule. The sign convention is calibrated against CS2's own `TurnRight` /
    # `TurnLeft` lane flags: over 21,192 flagged junction lanes, a right turn sweeps a NEGATIVE
    # cross product of (east, north) on 95.3 % and a left turn a positive one on 91.3 %, so the
    # right-hand normal of a tangent (tx, tz) is (tz, -tx). Reported, never assumed: on the
    # reference city 5,731 segments put the forward lanes right and 2 put them left.
    with_flow = offsets[[i for i, (_p, inv) in enumerate(lanes) if not inv]]
    against = offsets[[i for i, (_p, inv) in enumerate(lanes) if inv]]
    forward_side = 0
    if with_flow.size and against.size:
        # `_frame` returns the LEFT-hand normal ((-tz, tx) for a tangent (tx, tz)), so a LARGER
        # offset means further left: +1 = the with-flow lanes are on the left = left-hand traffic
        forward_side = 1 if float(with_flow.mean()) > float(against.mean()) else -1

    start_pts, end_pts = _terminal_lane_points(forward, spine)
    return Spine(points=points, width=width, measured_width=measured,
                 lanes_left=left, lanes_right=right, forward_side=forward_side,
                 start_lane_points=start_pts, end_lane_points=end_pts)


def _longest_chain(paths):
    """The longest run of lanes welded end to end, as one polyline.

    CS2 splits a segment's lanes at interior nodes and every piece's start is bit-close to the
    previous piece's end, so the chain is exact rather than a nearest-neighbour guess.
    """
    starts = {}
    for i, path in enumerate(paths):
        starts.setdefault(_key(path[0]), []).append(i)
    best = None
    best_len = -1.0
    for i, path in enumerate(paths):
        chain = [i]
        used = {i}
        total = _length(path)
        tail = path[-1]
        while True:
            nxt = None
            for j in starts.get(_key(tail), ()):
                if j not in used:
                    nxt = j
                    break
            if nxt is None:
                break
            used.add(nxt)
            chain.append(nxt)
            total += _length(paths[nxt])
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


def _chain_pieces(paths):
    """How many longitudinal pieces the segment's lanes are split into — the length of the
    longest chain. Used to turn a lane COUNT into a lanes-across-the-road count."""
    starts = {}
    for i, path in enumerate(paths):
        starts.setdefault(_key(path[0]), []).append(i)
    best = 1
    for i, path in enumerate(paths):
        used = {i}
        tail = path[-1]
        count = 1
        while True:
            nxt = next((j for j in starts.get(_key(tail), ()) if j not in used), None)
            if nxt is None:
                break
            used.add(nxt)
            count += 1
            tail = paths[nxt][-1]
        best = max(best, count)
    return best


def _terminal_lane_points(paths, spine):
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
        starting[_key(path[0])] += 1
        ending[_key(path[-1])] += 1
    interior = {k for k in starting if k in ending}
    head, tail = spine[0], spine[-1]
    start, end = [], []
    for path in paths:
        for point in (path[0], path[-1]):
            if _key(point) in interior:
                continue
            (start if _sq(point - head) <= _sq(point - tail) else end).append(point)
    return start, end


# -- the connectors ----------------------------------------------------------------------------

def _connectors(junction_lanes, end_points, end_keys, ends, attrs, spines, tolerance, simplify,
                max_width):
    """One road per turning movement between two different segment ends.

    The junction lane's own interior shape is kept; only its two terminal nodes are moved, onto
    the two segments' own centerline ends — which is what makes the join exact instead of
    something a consumer's merge pass has to find.
    """
    stats = {"junctionLanes": len(junction_lanes), "connectorsUnmatched": 0,
             "connectorsUnused": 0, "touched": set()}
    if not end_points:
        stats["connectorsUnused"] = len(junction_lanes)
        return [], stats
    from scipy.spatial import cKDTree

    tree = cKDTree(numpy.asarray(end_points, dtype=numpy.float64))
    movements = defaultdict(list)               # (from_key, to_key) -> [path, ...]
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
        reverse = movements.get((y, x))
        oneway = reverse is None
        origin = ends[x]
        both = list(paths) + [p[::-1] for p in (reverse or ())]
        both = [p if _sq(p[0] - origin) <= _sq(p[-1] - origin) else p[::-1] for p in both]
        line = _average_paths(both)
        if line is None or len(line) < 2:
            continue
        line[0] = ends[x]
        line[-1] = ends[y]
        line = _simplify(line, simplify)
        widths = [spines[s].width for s, _k in (x, y) if s in spines]
        width = min(widths) if widths else 2.0 * LANE_HALF_WIDTH
        limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
        elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
        roads.append(Road(
            key=f"conn_{x[0]}_{x[1]}_{y[0]}_{y[1]}", kind="connector", segment=x[0],
            name=attrs.get(x[0], {}).get("name"), category=attrs.get(x[0], {}).get("category"),
            nodes=[(p[0], p[1], p[2], width) for p in line],
            oneway=oneway, lanes_left=0 if oneway else 1, lanes_right=1,
            speed_kph=min(limits) if limits else None, elevated=elevated))
    return roads, stats


def _sq(delta):
    return float(numpy.dot(delta, delta))


def _average_paths(paths):
    """Resample each path to a common station count by arclength fraction and average them."""
    usable = [p for p in paths if _length(p) > 1e-6]
    if not usable:
        return None
    n = max(2, min(32, int(round(max(_length(p) for p in usable) / 4.0)) + 1))
    stacked = [_resample_count(p, n) for p in usable]
    return numpy.mean(stacked, axis=0)


def _joints(end_points, end_keys, ends, attrs, spines, tolerance):
    """Two-node roads where two segments abut with no turning movement between them.

    CS2 splits a straight road into segments at nodes that are not junctions at all — a change of
    asset, a bridge abutment, a district boundary — and there is no node-owned lane there because
    there is nothing to turn. Their carriageway lanes weld to each other directly instead
    (measured: 4,424 lane-end pairs on the reference city), and without this the graph falls into
    pieces at every one of them.
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
        limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
        elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
        roads.append(_Joint(
            key=f"joint_{x[0]}_{x[1]}_{y[0]}_{y[1]}", kind="joint", segment=x[0],
            name=attrs.get(x[0], {}).get("name"), category=attrs.get(x[0], {}).get("category"),
            nodes=[(a[0], a[1], a[2], width), (b[0], b[1], b[2], width)],
            oneway=False, lanes_left=1, lanes_right=1,
            speed_kph=min(limits) if limits else None, elevated=elevated, key_pair=(x, y)))
    return roads


# -- polyline maths ----------------------------------------------------------------------------

def _length(path):
    return float(numpy.sum(numpy.linalg.norm(numpy.diff(path, axis=0), axis=1)))


def _key(point, quantum=0.05):
    """A weld key: the point rounded to the tolerance grid. Two lane ends CS2 placed at the same
    coordinate hash together; a grid is used rather than a kd-tree because a segment's own lanes
    are exact to the millimetre and this runs 6,667 times."""
    return (round(float(point[0]) / quantum), round(float(point[1]) / quantum),
            round(float(point[2]) / quantum))


def _resample(path, step):
    n = max(2, int(math.ceil(_length(path) / step)) + 1)
    return _resample_count(path, n)


def _resample_count(path, n):
    seg = numpy.linalg.norm(numpy.diff(path, axis=0), axis=1)
    s = numpy.concatenate([[0.0], numpy.cumsum(seg)])
    if s[-1] < 1e-9:
        return numpy.repeat(path[:1], n, axis=0)
    t = numpy.linspace(0.0, s[-1], n)
    return numpy.stack([numpy.interp(t, s, path[:, k]) for k in range(3)], axis=1)


def _frame(points):
    """Per-vertex tangent and left-hand HORIZONTAL normal of a polyline. The offset is taken in
    the horizontal plane only, for the same reason `networks.ribbon` does: CS2 does not bank a
    road, and a horizontal offset keeps a ramp the right width in plan."""
    delta = numpy.gradient(points, axis=0)
    flat = numpy.stack((delta[:, 0], delta[:, 2]), axis=1)
    norm = numpy.linalg.norm(flat, axis=1, keepdims=True)
    flat = numpy.where(norm < 1e-9, numpy.array([1.0, 0.0]), flat / numpy.where(norm < 1e-9, 1.0, norm))
    tangents = numpy.zeros_like(points)
    tangents[:, 0] = flat[:, 0]
    tangents[:, 2] = flat[:, 1]
    normals = numpy.zeros_like(points)
    normals[:, 0] = -flat[:, 1]
    normals[:, 2] = flat[:, 0]
    return tangents, normals


def _nearest_station(points, target):
    d = points - numpy.asarray(target, dtype=numpy.float64)
    return int(numpy.argmin(numpy.einsum("ij,ij->i", d, d)))


def _simplify(points, tolerance):
    """Ramer-Douglas-Peucker, iterative. Endpoints are always kept."""
    points = numpy.asarray(points, dtype=numpy.float64)
    if len(points) <= 2 or tolerance <= 0:
        return points
    keep = numpy.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = points[i], points[j]
        ab = b - a
        denom = float(numpy.dot(ab, ab))
        seg = points[i + 1:j] - a
        if denom < 1e-12:
            dist = numpy.linalg.norm(seg, axis=1)
        else:
            t = numpy.clip((seg @ ab) / denom, 0.0, 1.0)[:, None]
            dist = numpy.linalg.norm(seg - t * ab, axis=1)
        k = int(numpy.argmax(dist))
        if dist[k] > tolerance:
            keep[i + 1 + k] = True
            stack.append((i, i + 1 + k))
            stack.append((i + 1 + k, j))
    return points[keep]


# -- reporting ---------------------------------------------------------------------------------

def _components(roads):
    """Connected-component sizes over the emitted graph.

    Two roads are joined where they share a terminal node coordinate — which, by construction, a
    segment and its connectors do exactly. A navgraph in many pieces routes nowhere, and the size
    of the largest component is the one number that says whether the junction join worked at all.
    """
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for road in roads:
        union(_key(road.nodes[0][:3]), _key(road.nodes[-1][:3]))
    sizes = defaultdict(int)
    for road in roads:
        sizes[find(_key(road.nodes[0][:3]))] += 1
    ordered = sorted(sizes.values(), reverse=True)
    total = sum(ordered)
    return {"components": len(ordered), "largestComponent": ordered[0] if ordered else 0,
            "largestComponentFraction": round(ordered[0] / total, 4) if total else 0.0}


def _notes(stats, max_width):
    """One line per approximation this build made, with its count. Print them."""
    notes = []
    if stats.get("sideConnections"):
        n = stats["sideConnections"]
        notes.append(f"navgraph: {n:,} SideConnection driveway stubs excluded from the "
                     f"carriageway - CS2's own flag, not a length threshold")
    if stats.get("dividedCarriagewaysAveraged"):
        notes.append(f"navgraph: APPROXIMATION - {stats['dividedCarriagewaysAveraged']:,} segments "
                     f"carry lanes in both directions and become ONE centerline down the middle "
                     f"of them; a median strip inside a single segment is not represented")
    if stats.get("endsWithNoJunction"):
        notes.append(f"navgraph: {stats['endsWithNoJunction']:,} segment ends weld to no turning "
                     f"movement - a dead end, or a join not found")
    if stats.get("connectorsUnmatched"):
        notes.append(f"navgraph: {stats['connectorsUnmatched']:,} node-owned car lanes touch a "
                     f"drivable segment at one end only (a movement into a lot or a path) and "
                     f"are not emitted")
    if stats.get("widthClamped"):
        notes.append(f"navgraph: APPROXIMATION - {stats['widthClamped']:,} segments had their "
                     f"carriageway width clamped to {max_width} m, so their node radius understates "
                     f"the real road")
    if stats.get("speedInferred"):
        notes.append(f"navgraph: {stats['speedInferred']:,} segments ship no attrs.limit; the "
                     f"consumer is left to auto-calculate a speed for them")
    if stats.get("joints"):
        notes.append(f"navgraph: {stats['joints']:,} joints where two segments abut with no "
                     f"turning movement between them (an asset change or a bridge abutment, not "
                     f"a junction)")
    votes = stats.get("forwardLanesRight", 0) + stats.get("forwardLanesLeft", 0)
    if votes:
        side = "LEFT" if stats.get("leftHandTraffic") else "RIGHT"
        notes.append(f"navgraph: traffic drives on the {side} - MEASURED, "
                     f"{stats['forwardLanesRight']:,} two-way segments put the with-flow lanes "
                     f"right and {stats['forwardLanesLeft']:,} put them left")
    frac = stats.get("largestComponentFraction") or 0.0
    notes.append(f"navgraph: {stats.get('roads', 0):,} roads / {stats.get('nodes', 0):,} nodes, "
                 f"{stats.get('components', 0)} connected components, largest holds "
                 f"{frac:.1%} of them")
    return notes


def summarise(bundle_dir, **kwargs):
    """`build()`'s stats alone — what `tools/check_navgraph_vs_game.py` prints."""
    return build(bundle_dir, **kwargs).stats
