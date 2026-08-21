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

Measured on the 2026-08-19 16:32 reference city: 15,393 roads (6,667 segments, 8,676 connectors,
50 joints), 51,197 nodes, **96.5 % of them in one connected component** over 35 components.

## Three properties of the emitted set that are not optional

Each was paid for by an in-game load — the first two by the `*** FATAL LUA ERROR` that ended one
(`lua/ge/map.lua:459`, audit A7.5), the third by a route up an overpass with no ramp under it
(A7.6). All three are properties of the *consumer's* graph model rather than of ours, which is
why they are enforced here and not in a consumer:

* **A terminal node carries the width of the thing it joins.** A connector's or a joint's first
  and last node take the width of the *segment end* they are snapped onto, not the pair's
  minimum. A consumer decides node identity by merging coincident terminal nodes, and the merge
  it performs compares radii as well as positions (map.lua:1211) — so a 3.5 m connector ending
  on a 6.5 m road's centerline end sat 0 m away from it and still stayed a *different node*.
  Measured on the reference city: 9,738 terminal nodes were the wrong width, and the graph
  BeamNG built out of them stood in **692 pieces with the largest holding 13.8 %** of the nodes
  at the point its optimiser ran, against 35 / 96.5 % once the widths matched.
* **It is a SIMPLE graph.** At most one road per unordered pair of nodes, and none whose two
  ends are the same node. `map.nodes[a].links[b]` holds exactly one edge and every walk over it
  skips `a == b`, so a second road between one pair is not a second edge — it is the first one
  silently replaced, and the pair of them is a triangle waiting for the optimiser. A turning
  movement from a junction back to itself is not a movement.
* **A node's radius does not reach a road at a different height.** The radius is the road's
  half-width, and four separate readers take "inside the radius" to mean "on this road" — most
  sharply `startPosLinks` (map.lua:2871), which costs a candidate start edge `max(0, 1 - R/|d|)`,
  so an edge whose radius covers the car costs **exactly zero** and the `wZ = 4` height bias in
  the very next line never runs. A 27 m carriageway claims a 13.5 m radius and a viaduct 7 m
  above it is well inside that, so the router is entitled to start the route on the deck. See
  `_clearance_clamp` and `stacked_coverage`.

Neither drops anything a route needs: a dropped duplicate leaves the same two nodes joined by
the road that carried the geometry, and a dropped self-loop joined nothing. Measured cost on the
reference city: 2,114 self-loops and 112 duplicates out of 17,619 roads, and connectivity after
the consumer's own merges is unchanged (96.51 % → 96.49 % of nodes in the largest component).

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
* `widthClearanceClamped` / `widthClearanceWorst` — segments whose emitted width was capped so
  their radius stops short of a road stacked over or under them, and the largest such reduction
  in metres. Those roads' widths are UNDERSTATED by that much; 19 segments on the reference city,
  worst 13.3 m.
* `speedInferred` — segments with no `attrs.limit`; the consumer is left to auto-calculate.
* `selfLoopsDropped` / `duplicateEdgesDropped` — what the simple-graph rule above removed. Not
  approximations so much as arithmetic, but they are road records that existed and no longer do,
  so they are counted where every other such number is.
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

# When are two roads' terminal nodes THE SAME NODE? Not a question this module gets to answer by
# itself: a consumer keys its edges on node ids, and the ids come from ITS merge of coincident
# terminal nodes. The two numbers below are the tightest such merge in evidence — BeamNG's
# `mergeOverlappingNodes(endNodesOnly)`, `lua/ge/map.lua:1209-1211`, which fuses two terminal
# nodes when they are within 0.1 m in 3-D **and** their radii agree to within 0.1 m. A looser
# consumer merge only fuses more, and `_simple_graph` is sound under any superset of this.
NODE_MERGE_DISTANCE = 0.1
NODE_MERGE_RADIUS = 0.1

# A road stacked over another by more than this is a DIFFERENT DECK, not the same road on a
# slope. 3 m is below any clearance a city gives a bridge and above any grade change within one
# carriageway; `_clearance_clamp` uses it to decide which neighbours a node's radius may not
# reach, and `tools/check_navgraph_vs_game.py` uses the same number for its false-weld question.
STACKED_CLEARANCE = 3.0

# How much of a gap `_clearance_clamp` leaves between a node's radius and the deck it must not
# claim. Small on purpose: the constraint is "does not reach", not "keeps its distance".
CLEARANCE_MARGIN = 0.5

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
_Joint = namedtuple("_Joint", (*Road._fields, "key_pair"))


def build(
    bundle_dir,
    categories=DEFAULT_CATEGORIES,
    weld_tolerance=WELD_TOLERANCE,
    max_width=None,
    simplify=SIMPLIFY_TOLERANCE,
    progress=None,
):
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
    log(
        f"navgraph: {counts['carLanes']:,} car lanes - {counts['segmentOwned']:,} on those "
        f"segments ({counts['sideConnections']:,} SideConnection stubs dropped), "
        f"{len(junction_lanes):,} node-owned"
    )

    stats = {"segments": len(attrs), "segmentsWithLanes": len(seg_lanes)}
    stats.update(counts)

    spines = {}
    for sid, lanes in seg_lanes.items():
        spine = _segment_centerline(lanes, attrs[sid], max_width)
        if spine is not None:
            spines[sid] = spine
    stats["segmentsWithoutCenterline"] = len(seg_lanes) - len(spines)

    roads = []
    ends = {}  # (sid, 0|1) -> end point of the emitted centerline
    end_lane_points = []  # every carriageway lane endpoint AT a segment end
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
        roads.append(
            Road(
                key=f"seg_{sid}",
                kind="segment",
                segment=sid,
                name=a.get("name"),
                category=a.get("category"),
                nodes=[(p[0], p[1], p[2], width) for p in nodes],
                oneway=(spine.lanes_left == 0),
                lanes_left=spine.lanes_left,
                lanes_right=spine.lanes_right,
                speed_kph=limit,
                elevated=a.get("form") in ("elevated", "tunnel"),
            )
        )
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

    connectors, conn_stats = _connectors(
        junction_lanes,
        end_lane_points,
        end_lane_key,
        ends,
        attrs,
        spines,
        weld_tolerance,
        simplify,
        max_width,
    )
    roads.extend(connectors)
    touched = conn_stats.pop("touched")
    joints = _joints(end_lane_points, end_lane_key, ends, attrs, spines, weld_tolerance)
    roads.extend(Road(*joint[: len(Road._fields)]) for joint in joints)
    touched.update(k for joint in joints for k in joint.key_pair)
    stats["joints"] = len(joints)
    stats.update(conn_stats)
    stats["endsWithNoJunction"] = sum(1 for key in ends if key not in touched)

    roads, clearance_stats = _clearance_clamp(roads)
    stats.update(clearance_stats)
    roads, simple_stats = _simple_graph(roads)
    stats.update(simple_stats)
    stats["roads"] = len(roads)
    stats["nodes"] = sum(len(r.nodes) for r in roads)
    stats["connectors"] = sum(1 for r in roads if r.kind == "connector")
    stats["joints"] = sum(1 for r in roads if r.kind == "joint")

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
            seg_lanes[owner].append((numpy.asarray(path, dtype=numpy.float64), "Invert" in flags))
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

Spine = namedtuple(
    "Spine",
    "points width measured_width lanes_left lanes_right "
    "start_lane_points end_lane_points forward_side",
)


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
    _tangents, normals = _frame(spine)

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
    return Spine(
        points=points,
        width=width,
        measured_width=measured,
        lanes_left=left,
        lanes_right=right,
        forward_side=forward_side,
        start_lane_points=start_pts,
        end_lane_points=end_pts,
    )


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


def _connectors(
    junction_lanes, end_points, end_keys, ends, attrs, spines, tolerance, simplify, max_width
):
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
    from scipy.spatial import cKDTree

    tree = cKDTree(numpy.asarray(end_points, dtype=numpy.float64))
    movements = defaultdict(list)  # (from_key, to_key) -> [path, ...]
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
        # The two terminal nodes take the width of the segment end they were snapped onto, so a
        # consumer's radius-aware node merge fuses them with it (see the module docstring).
        node_widths = _terminal_widths(len(line), width, spines, x, y)
        limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
        elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
        roads.append(
            Road(
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
        )
    return roads, stats


def _clearance_clamp(roads, clearance=STACKED_CLEARANCE, margin=CLEARANCE_MARGIN, step=4.0):
    """A node's radius may not reach a road at a different height. `(roads, stats)`.

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

    Terminal nodes stay consistent: every road ending on one node takes that node's smallest
    clamped width, so the terminal-width invariant that makes a junction ONE node (A7.5) holds.
    A road is never widened and never goes below one lane's half-width.
    """
    stats = {"widthClearanceClamped": 0, "widthClearanceWorst": 0.0}
    if len(roads) < 2:
        return roads, stats
    from scipy.spatial import cKDTree

    clusters = _terminal_clusters(roads)
    ends = [frozenset(pair) for pair in clusters]
    owned = [_road_segments(road.key) for road in roads]
    half = [0.5 * max(n[3] for n in road.nodes) for road in roads]

    centres, centre_owner, skins, skin_owner = [], [], [], []
    for index, road in enumerate(roads):
        line = _resample(numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64), step)
        centres.append(line)
        centre_owner.append(numpy.full(len(line), index, dtype=numpy.int64))
        skin = _surface(road, step)
        skins.append(skin)
        skin_owner.append(numpy.full(len(skin), index, dtype=numpy.int64))
    centres = numpy.concatenate(centres)
    centre_owner = numpy.concatenate(centre_owner)
    skins = numpy.concatenate(skins)
    skin_owner = numpy.concatenate(skin_owner)

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

    if not limit:
        return roads, stats
    cap = {}
    for i, gap in limit.items():
        capped = max(LANE_HALF_WIDTH, gap - margin)
        if capped < half[i]:
            cap[i] = capped
    if not cap:
        return roads, stats

    # One width per terminal node, so a junction is still ONE node after the consumer's merge
    # (A7.5). A node's cap is the tightest any road ending there was given.
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
    return out, stats


def _road_segments(key):
    """The `networks.jsonl` segment ids a road's key names. `seg_<id>` -> one; `conn_<a>_<i>_<b>_<j>`
    and `joint_...` -> two, and NOT the end indices `<i>`/`<j>`, which are 0 and 1 on half the
    roads in the level and would make every other pair look adjacent."""
    parts = key.split("_")
    if parts[0] == "seg":
        return frozenset(parts[1:2])
    if parts[0] in ("conn", "joint") and len(parts) == 5:
        return frozenset((parts[1], parts[3]))
    return frozenset()


def _surface(road, step):
    """A road's DRIVABLE SURFACE, sampled: the centerline and both kerbs.

    A car is not on the centerline. It is anywhere within the half-width, which is the whole point
    of the radius — so a question about where a car can be has to be asked of the surface, and
    asking it of the centerline is what let `_clearance_clamp`'s first cut pass while the defect
    was still in the level.
    """
    centre = _resample(numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64), step)
    if len(centre) < 2:
        return centre
    half = 0.5 * max(n[3] for n in road.nodes)
    flat = centre.copy()
    flat[:, 1] = 0.0  # the lateral frame is horizontal
    _tangents, normals = _frame(flat)
    return numpy.concatenate((centre, centre + normals * half, centre - normals * half))


def stacked_coverage(roads, clearance=STACKED_CLEARANCE, step=4.0):
    """Every place a car ON one road can stand INSIDE the radius of a road at a different height.

    `[{road, other, gap, radius, height, at}, ...]`, and it should be empty. This is the predicate
    `_clearance_clamp` exists to satisfy, checked on the emitted roads rather than on the spines
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

    clusters = [frozenset(pair) for pair in _terminal_clusters(roads)]
    owned = [_road_segments(road.key) for road in roads]
    half = numpy.asarray([0.5 * max(n[3] for n in road.nodes) for road in roads])
    centres, centre_owner, surfaces, surface_owner = [], [], [], []
    for index, road in enumerate(roads):
        centre = _resample(numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64), step)
        centres.append(centre)
        centre_owner.append(numpy.full(len(centre), index, dtype=numpy.int64))
        skin = _surface(road, step)
        surfaces.append(skin)
        surface_owner.append(numpy.full(len(skin), index, dtype=numpy.int64))
    centres = numpy.concatenate(centres)
    centre_owner = numpy.concatenate(centre_owner)
    surfaces = numpy.concatenate(surfaces)
    surface_owner = numpy.concatenate(surface_owner)

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


def _terminal_widths(count, width, spines, x, y):
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

    Most of what this emits does not survive `_simple_graph`, and that is the point: 1,245 of the
    reference city's 1,383 joints are ZERO LENGTH — the two segments' centerline ends are the same
    coordinate, so the pair is one node and a road between them joins nothing — and another 88 run
    between a pair a connector already covers. The 50 that remain are the ones doing the work: two
    ends a metre or two apart that nothing else ties together.
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
        wa, wb = _terminal_widths(2, width, spines, x, y)
        limits = [attrs[s].get("limit") for s, _k in (x, y) if attrs.get(s, {}).get("limit")]
        elevated = any(attrs.get(s, {}).get("form") in ("elevated", "tunnel") for s, _k in (x, y))
        roads.append(
            _Joint(
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


# -- polyline maths ----------------------------------------------------------------------------


def _length(path):
    return float(numpy.sum(numpy.linalg.norm(numpy.diff(path, axis=0), axis=1)))


def _key(point, quantum=0.05):
    """A weld key: the point rounded to the tolerance grid. Two lane ends CS2 placed at the same
    coordinate hash together; a grid is used rather than a kd-tree because a segment's own lanes
    are exact to the millimetre and this runs 6,667 times."""
    return (
        round(float(point[0]) / quantum),
        round(float(point[1]) / quantum),
        round(float(point[2]) / quantum),
    )


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
    flat = numpy.where(
        norm < 1e-9, numpy.array([1.0, 0.0]), flat / numpy.where(norm < 1e-9, 1.0, norm)
    )
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
        seg = points[i + 1 : j] - a
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


def _terminal_clusters(roads, distance=NODE_MERGE_DISTANCE, radius=NODE_MERGE_RADIUS):
    """`[(a, b), ...]` — the node each road's first and last node belongs to, one entry per road.

    The node identity a consumer will have: coincident terminal nodes fused, transitively, by the
    merge documented on `NODE_MERGE_DISTANCE`. Everything downstream that asks "is this one node
    or two?" — the simple-graph rule, the component count — has to ask it here, or the two
    answers disagree about the same graph.
    """
    from scipy.spatial import cKDTree

    points = numpy.asarray(
        [node[:3] for road in roads for node in (road.nodes[0], road.nodes[-1])],
        dtype=numpy.float64,
    )
    radii = numpy.asarray(
        [0.5 * road.nodes[i][3] for road in roads for i in (0, -1)], dtype=numpy.float64
    )
    parent = list(range(len(points)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, j in cKDTree(points).query_pairs(distance):
        if abs(radii[i] - radii[j]) < radius:
            ra, rb = find(i), find(j)
            if ra != rb:
                parent[rb] = ra
    return [(find(2 * i), find(2 * i + 1)) for i in range(len(roads))]


def _simple_graph(roads, distance=NODE_MERGE_DISTANCE, radius=NODE_MERGE_RADIUS):
    """Make the emitted set a SIMPLE graph on the nodes a consumer will actually have.

    `(roads, stats)`. Terminal nodes are clustered by the consumer merge documented on
    `NODE_MERGE_DISTANCE` — within `distance` in 3-D and with radii within `radius`, transitively
    — and then:

    * a **connector or joint whose two ends land in one cluster** is a self-loop. It joins a node
      to itself, which is not an edge in any graph a consumer keys on node pairs, and it is what
      turns into a closed 3-cycle the moment the merge runs. Dropped.
    * **a second road between one pair of clusters** is not a second edge. Whichever arrives last
      replaces the first in `links`, and the pair of them (one direct, one through an interior
      node) is the triangle whose middle node BeamNG's `optimizeNodes` walks into
      `map.lua:459`. One is kept — the one carrying the geometry — and the rest dropped.

    **A segment road is never dropped**: it is the road network itself, a segment CS2 modelled as
    a closed ring legitimately begins and ends at one node, and two segments sharing a node pair
    are a divided carriageway, not a duplicate. Connectors and joints are what this thins, and a
    dropped one always leaves its two nodes joined by the road that kept the turn's shape.
    """
    stats = {"selfLoopsDropped": 0, "duplicateEdgesDropped": 0}
    if not roads:
        return roads, stats
    ends = _terminal_clusters(roads, distance, radius)

    # A segment first, then the road carrying the most geometry: where a stub and a real turning
    # path join the same two nodes, the turn's shape is the one worth keeping.
    order = sorted(
        range(len(roads)),
        key=lambda i: (roads[i].kind != "segment", -len(roads[i].nodes), roads[i].key),
    )
    taken = {}
    keep = [False] * len(roads)
    for i in order:
        road = roads[i]
        a, b = ends[i]
        if road.kind != "segment":
            if a == b:
                stats["selfLoopsDropped"] += 1
                continue
            pair = (min(a, b), max(a, b))
            if pair in taken:
                stats["duplicateEdgesDropped"] += 1
                continue
            taken[pair] = road.key
        else:
            taken.setdefault((min(a, b), max(a, b)), road.key)
        keep[i] = True
    return [road for i, road in enumerate(roads) if keep[i]], stats


def _components(roads):
    """Connected-component sizes over the emitted graph.

    Two roads are joined where their terminal nodes are THE SAME NODE — `_terminal_clusters`, the
    consumer's own merge, and not a coordinate match. The difference is not academic: on the
    reference city a coordinate match calls this graph 96 % connected and the graph BeamNG builds
    out of it stood in 692 pieces, because a 3.5 m connector and the 6.5 m road it ended on were
    two nodes at one coordinate. A navgraph in many pieces routes nowhere, and the size of the
    largest component is the one number that says whether the junction join worked at all.
    """
    if not roads:
        return {"components": 0, "largestComponent": 0, "largestComponentFraction": 0.0}
    ends = _terminal_clusters(roads)
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

    for a, b in ends:
        union(a, b)
    sizes = defaultdict(int)
    for a, _b in ends:
        sizes[find(a)] += 1
    ordered = sorted(sizes.values(), reverse=True)
    total = sum(ordered)
    return {
        "components": len(ordered),
        "largestComponent": ordered[0] if ordered else 0,
        "largestComponentFraction": round(ordered[0] / total, 4) if total else 0.0,
    }


def _notes(stats, max_width):
    """One line per approximation this build made, with its count. Print them."""
    notes = []
    if stats.get("sideConnections"):
        n = stats["sideConnections"]
        notes.append(
            f"navgraph: {n:,} SideConnection driveway stubs excluded from the "
            f"carriageway - CS2's own flag, not a length threshold"
        )
    if stats.get("dividedCarriagewaysAveraged"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['dividedCarriagewaysAveraged']:,} segments "
            f"carry lanes in both directions and become ONE centerline down the middle "
            f"of them; a median strip inside a single segment is not represented"
        )
    if stats.get("endsWithNoJunction"):
        notes.append(
            f"navgraph: {stats['endsWithNoJunction']:,} segment ends weld to no turning "
            f"movement - a dead end, or a join not found"
        )
    if stats.get("connectorsUnmatched"):
        notes.append(
            f"navgraph: {stats['connectorsUnmatched']:,} node-owned car lanes touch a "
            f"drivable segment at one end only (a movement into a lot or a path) and "
            f"are not emitted"
        )
    if stats.get("widthClamped"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['widthClamped']:,} segments had their "
            f"carriageway width clamped to {max_width} m, so their node radius understates "
            f"the real road"
        )
    if stats.get("speedInferred"):
        notes.append(
            f"navgraph: {stats['speedInferred']:,} segments ship no attrs.limit; the "
            f"consumer is left to auto-calculate a speed for them"
        )
    if stats.get("widthClearanceClamped"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['widthClearanceClamped']:,} segments had "
            f"their width capped so their node radius stops short of a road stacked "
            f"over or under them (worst {stats.get('widthClearanceWorst', 0)} m); their "
            f"width is understated by that much, and without it a consumer that asks "
            f"'is the car inside this radius' answers with the deck overhead"
        )
    if stats.get("selfLoopsDropped"):
        notes.append(
            f"navgraph: {stats['selfLoopsDropped']:,} connectors/joints dropped as "
            f"SELF-LOOPS - both ends land on one node once coincident terminal nodes "
            f"merge, so they join nothing (map.lua:459 crashed on the 3-cycle they make)"
        )
    if stats.get("duplicateEdgesDropped"):
        notes.append(
            f"navgraph: {stats['duplicateEdgesDropped']:,} connectors/joints dropped as "
            f"DUPLICATE edges - a second road between one pair of nodes is not a second "
            f"edge; the one carrying the turn's geometry is kept"
        )
    if stats.get("joints"):
        notes.append(
            f"navgraph: {stats['joints']:,} joints where two segments abut with no "
            f"turning movement between them (an asset change or a bridge abutment, not "
            f"a junction)"
        )
    votes = stats.get("forwardLanesRight", 0) + stats.get("forwardLanesLeft", 0)
    if votes:
        side = "LEFT" if stats.get("leftHandTraffic") else "RIGHT"
        notes.append(
            f"navgraph: traffic drives on the {side} - MEASURED, "
            f"{stats['forwardLanesRight']:,} two-way segments put the with-flow lanes "
            f"right and {stats['forwardLanesLeft']:,} put them left"
        )
    frac = stats.get("largestComponentFraction") or 0.0
    notes.append(
        f"navgraph: {stats.get('roads', 0):,} roads / {stats.get('nodes', 0):,} nodes, "
        f"{stats.get('components', 0)} connected components, largest holds "
        f"{frac:.1%} of them"
    )
    return notes


def summarise(bundle_dir, **kwargs):
    """`build()`'s stats alone — what `tools/check_navgraph_vs_game.py` prints."""
    return build(bundle_dir, **kwargs).stats
