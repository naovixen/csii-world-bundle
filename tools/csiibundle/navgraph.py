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
  `csiibundle.navclearance`.

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

from . import navclearance, navjunction, navmerge, navread, navreport, navspine, polyline
from .navroad import Navgraph, Road

__all__ = ["Navgraph", "Road", "build", "summarise"]

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

# Douglas-Peucker tolerance for the emitted polyline. A navgraph node every 2 m is a waste of a
# consumer's kd-tree; 0.25 m is a quarter of a lane marking's width and invisible to routing.
SIMPLIFY_TOLERANCE = 0.25


def _spines(seg_lanes, attrs, max_width, stats):
    """One carriageway spine per segment that yields one (`navspine.segment_centerline`)."""
    spines = {}
    for sid, lanes in seg_lanes.items():
        spine = navspine.segment_centerline(lanes, attrs[sid], max_width)
        if spine is not None:
            spines[sid] = spine
    stats["segmentsWithoutCenterline"] = len(seg_lanes) - len(spines)
    stats["forwardLanesRight"] = sum(1 for s in spines.values() if s.forward_side < 0)
    stats["forwardLanesLeft"] = sum(1 for s in spines.values() if s.forward_side > 0)
    stats["leftHandTraffic"] = stats["forwardLanesLeft"] > stats["forwardLanesRight"]
    return spines


def _segment_road(sid, spine, a, simplify):
    """The `Road` for one segment's spine."""
    nodes = polyline.simplify(spine.points, simplify)
    return Road(
        key=f"seg_{sid}",
        kind="segment",
        segment=sid,
        name=a.get("name"),
        category=a.get("category"),
        nodes=[(p[0], p[1], p[2], spine.width) for p in nodes],
        oneway=(spine.lanes_left == 0),
        lanes_left=spine.lanes_left,
        lanes_right=spine.lanes_right,
        speed_kph=a.get("limit"),
        elevated=a.get("form") in ("elevated", "tunnel"),
    )


def _segment_roads(spines, attrs, simplify, max_width, stats):
    """Every segment road, plus what the junction pass needs: `(roads, ends, end_lane_points,
    end_lane_key)` — the emitted centerline end per segment end, and every carriageway lane
    endpoint AT a segment end, keyed by which end it belongs to."""
    roads = []
    ends = {}
    end_lane_points = []
    end_lane_key = []
    divided = 0
    clamped = 0
    speed_inferred = 0
    for sid, spine in spines.items():
        a = attrs[sid]
        if max_width is not None and spine.measured_width > max_width + 1e-6:
            clamped += 1
        if spine.lanes_left and spine.lanes_right:
            divided += 1
        if a.get("limit") is None:
            speed_inferred += 1
        roads.append(_segment_road(sid, spine, a, simplify))
        ends[(sid, 0)] = spine.points[0]
        ends[(sid, 1)] = spine.points[-1]
        for k, pts in ((0, spine.start_lane_points), (1, spine.end_lane_points)):
            for p in pts:
                end_lane_points.append(p)
                end_lane_key.append((sid, k))
    stats["dividedCarriagewaysAveraged"] = divided
    stats["widthClamped"] = clamped
    stats["speedInferred"] = speed_inferred
    return roads, ends, end_lane_points, end_lane_key


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
    attrs, all_network_ids = navread.read_segments(bundle_dir, wanted)
    log(f"navgraph: {len(attrs):,} {'/'.join(wanted)} segments in networks.jsonl")
    seg_lanes, junction_lanes, counts = navread.read_car_lanes(
        bundle_dir, set(attrs), progress, networks=all_network_ids
    )
    log(
        f"navgraph: {counts['carLanes']:,} car lanes - {counts['segmentOwned']:,} on those "
        f"segments ({counts['sideConnections']:,} SideConnection stubs dropped), "
        f"{len(junction_lanes):,} node-owned"
    )
    stats = {"segments": len(attrs), "segmentsWithLanes": len(seg_lanes)}
    stats.update(counts)

    spines = _spines(seg_lanes, attrs, max_width, stats)
    roads, ends, end_lane_points, end_lane_key = _segment_roads(
        spines, attrs, simplify, max_width, stats
    )
    connectors, conn_stats = navjunction.connectors(
        junction_lanes,
        end_lane_points,
        end_lane_key,
        ends,
        attrs,
        spines,
        weld_tolerance,
        simplify,
    )
    roads.extend(connectors)
    touched = conn_stats.pop("touched")
    joints = navjunction.joints(end_lane_points, end_lane_key, ends, attrs, spines, weld_tolerance)
    roads.extend(Road(*joint[: len(Road._fields)]) for joint in joints)
    touched.update(k for joint in joints for k in joint.key_pair)
    stats["joints"] = len(joints)
    stats.update(conn_stats)
    stats["endsWithNoJunction"] = sum(1 for key in ends if key not in touched)

    roads, clearance_stats = navclearance.clearance_clamp(roads)
    stats.update(clearance_stats)
    roads, simple_stats = navmerge.simple_graph(roads)
    stats.update(simple_stats)
    stats["roads"] = len(roads)
    stats["nodes"] = sum(len(r.nodes) for r in roads)
    stats["connectors"] = sum(1 for r in roads if r.kind == "connector")
    stats["joints"] = sum(1 for r in roads if r.kind == "joint")
    stats.update(navmerge.components(roads))

    notes = navreport.notes(stats, max_width)
    for note in notes:
        log(note)
    return Navgraph(roads=roads, stats=stats, notes=notes)


def summarise(bundle_dir, **kwargs):
    """`build()`'s stats alone — what `tools/check_navgraph_vs_game.py` prints."""
    return build(bundle_dir, **kwargs).stats
