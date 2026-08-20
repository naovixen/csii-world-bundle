"""Does the navgraph reach every road CS2 has, and does it weld anything CS2 does not?

The A7 parity predicate (plan `docs/plans/a7-navigation.md`). A navgraph is not a picture, so it
cannot be judged by looking at it; every question below is answered against the GAME's own data
— the `car` lanes CS2 placed — and not against the other consumer.

| question | the game's answer |
|---|---|
| is every drivable road in the graph? | a point sampled on a `car` lane owned by a `road`/`highway` segment must have a navgraph edge within half that road's width |
| does the graph reach the whole city? | CS2's drivable network is one connected component; so must ours be |
| does it weld two roads the game keeps apart? | two navgraph nodes a consumer's radius merge would join, more than `VERTICAL_SEPARATION` apart in height, name a bridge welded to the street under it |
| are the directions the game's? | `attrs.direction` on the segment vs the emitted `oneway` |
| are the speeds the game's? | `attrs.limit` on the segment vs the emitted `speed_kph` |
| can a consumer tell which of two stacked roads the car is on? | `startPosLinks` costs an edge whose radius covers the car at ZERO, so no node's radius may reach a road at a different height |
| can the consumer's load-time graph build survive it? | `beamng.map_lua`, a port of `lua/ge/map.lua`: no node may sit on a triangle its optimiser walks into `map.lua:459` |

Usage:

    python tools/check_navgraph_vs_game.py <bundle> [samples] [--max-width M] [--json out.json]

Bundle only — it does **not** need a built level, so it is the offline gate that runs before a
build, and the in-game load is the confirmation rather than the measurement. Exits non-zero when
an exact question disagrees or the coverage misses more than `COVERAGE_FAIL` of its samples.
"""

import argparse
import collections
import json
import os
import random
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from csiibundle import navgraph as navgraph_mod, reader  # noqa: E402

# A sampled lane point is covered when an edge passes within this many metres of it. It is
# half the road's own carriageway width plus a lane: the navgraph is a CENTERLINE, so a point on
# the outermost lane of a 24 m road is legitimately 9 m from it, and demanding better would be
# demanding the graph be a lane graph.
COVERAGE_SLACK = navgraph_mod.LANE_HALF_WIDTH

# The fraction of sampled lane points that may be uncovered before this fails. Not zero: CS2
# places car lanes on segments that carry no through route at all (a bus bay, a service spur off
# a highway) and a centerline graph is not required to hold them.
COVERAGE_FAIL = 0.05

# Two navgraph nodes closer than the larger of their radii are welded by BeamNG's own
# `mergeOverlappingNodes`, and that test is 3-D (`map.lua:1188`). Any such pair separated by more
# than this in HEIGHT is a bridge about to be welded to whatever runs under it.
VERTICAL_SEPARATION = 3.0

SAMPLES = 4000


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", nargs="?", default=None)
    parser.add_argument("samples", nargs="?", type=int, default=SAMPLES)
    parser.add_argument("--max-width", type=float, default=None,
                        help="clamp the emitted carriageway width, as the consumer does")
    parser.add_argument("--json", default=None, help="write the full result here")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv[1:])

    bundle = args.bundle
    if not bundle:
        from csiibundle.bake.locate import default_bundle_dir
        bundle = default_bundle_dir()
    if not os.path.isdir(bundle):
        print(f"no bundle at {bundle}", file=sys.stderr)
        return 2

    print(f"bundle {bundle}")
    graph = navgraph_mod.build(bundle, max_width=args.max_width)
    for note in graph.notes:
        print("  " + note)

    results = {}
    _coverage(bundle, graph, args.samples, args.seed, results)
    _connectivity(graph, results)
    _false_welds(graph, results)
    _attributes(bundle, graph, results)
    _stacked(graph, results)
    _map_lua(graph, results)

    print()
    failed = 0
    for question, r in results.items():
        total, agree = r["total"], r["agree"]
        share = agree / total if total else 1.0
        exact = r.get("exact", True)
        mark = "OK " if (share >= 1.0 or (not exact and share >= r.get("pass", 1.0))) else "BAD"
        if mark == "BAD":
            failed += 1
        print(f"{mark} {question}: {agree:,}/{total:,} = {share:.2%}"
              + (f"  ({r['note']})" if r.get("note") else ""))
    payload = {"bundle": bundle, "stats": graph.stats, "notes": graph.notes, "results": results}
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=1, default=float)
        print(f"\nwrote {args.json}")
    return 1 if failed else 0


def _edge_tree(graph):
    """A kd-tree over every navgraph node, plus the segment midpoints, so a nearest-node query
    approximates a nearest-EDGE query. Nodes are 0.25 m-simplified polylines, so a long straight
    run has two of them and its middle would otherwise read as uncovered."""
    from scipy.spatial import cKDTree

    points = []
    widths = []
    for road in graph.roads:
        nodes = numpy.asarray([n[:3] for n in road.nodes], dtype=numpy.float64)
        width = road.nodes[0][3]
        step = 4.0
        for a, b in zip(nodes[:-1], nodes[1:]):
            length = float(numpy.linalg.norm(b - a))
            n = max(2, int(length / step) + 1)
            for t in numpy.linspace(0.0, 1.0, n):
                points.append(a + (b - a) * t)
                widths.append(width)
    return cKDTree(numpy.asarray(points)), numpy.asarray(widths)


def _coverage(bundle, graph, samples, seed, results):
    """Sample car-lane points the game placed and ask whether the graph reaches them."""
    tree, widths = _edge_tree(graph)
    segments = {}
    for record in reader.read_jsonl(reader.layer_path(bundle, "networks")):
        a = record.get("attrs") or {}
        if a.get("category") in navgraph_mod.DEFAULT_CATEGORIES:
            segments[record["id"]] = a

    rng = random.Random(seed)
    picked = []
    seen = 0
    for lane in reader.read_jsonl(reader.layer_path(bundle, "lanes")):
        if lane.get("kind") != "car" or lane.get("owner") not in segments:
            continue
        if "SideConnection" in (lane.get("flags") or ()):
            continue
        seen += 1
        item = (lane["owner"], lane["path"])
        if len(picked) < samples:
            picked.append(item)
        else:                                   # reservoir: one uniform pass, no second read
            j = rng.randrange(seen)
            if j < samples:
                picked[j] = item

    by_category = collections.defaultdict(lambda: [0, 0])
    misses = []
    for owner, path in picked:
        point = numpy.asarray(path[len(path) // 2], dtype=numpy.float64)
        attrs = segments[owner]
        limit = 0.5 * float(attrs.get("width") or 8.0) + COVERAGE_SLACK
        distance, index = tree.query(point)
        allowed = max(limit, 0.5 * float(widths[index]) + COVERAGE_SLACK)
        key = attrs.get("category")
        by_category[key][1] += 1
        if distance <= allowed:
            by_category[key][0] += 1
        else:
            misses.append((distance, owner, attrs.get("name")))
    total = sum(v[1] for v in by_category.values())
    agree = sum(v[0] for v in by_category.values())
    misses.sort(reverse=True)
    note = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_category.items()))
    if misses:
        note += f"; worst miss {misses[0][0]:.1f} m on {misses[0][2] or misses[0][1]!r}"
    results["every drivable lane point has a navgraph edge within half its road"] = {
        "total": total, "agree": agree, "exact": False, "pass": 1.0 - COVERAGE_FAIL,
        "note": note}


def _stacked(graph, results):
    """Can a consumer that asks "is the car inside this road's radius" answer with the deck?

    Every reader of a navgraph node's radius — BeamNG has four — reads it as the extent of the
    road. `startPosLinks` (map.lua:2871) is the sharpest: an edge whose radius covers the car
    costs exactly zero, so a viaduct wide enough to reach the street beneath it is as good a place
    to start a route as the street. That is a route onto an overpass with no ramp (audit A7.6).
    """
    hits = navgraph_mod.stacked_coverage(graph.roads)
    sites = {(round(h["at"][0] / 50), round(h["at"][2] / 50)) for h in hits}
    note = f"{len(sites)} site(s)"
    if hits:
        worst = max(hits, key=lambda h: h["radius"])
        note += (f"; worst {worst['road']} sits {worst['gap']:.1f} m from {worst['other']}, "
                 f"radius {worst['radius']:.1f}, {worst['height']:.1f} m of height, at "
                 f"{worst['at']}")
    results["no road point sits inside the radius of a road at another height"] = {
        "total": graph.stats["roads"], "agree": graph.stats["roads"] - len(sites), "note": note}


def _map_lua(graph, results):
    """Can the consumer's own load-time graph build even survive this? (`beamng.map_lua`.)

    The one question here whose answer is a crash rather than a wrong number. BeamNG's
    `optimizeNodes` walks a degree-2 node whose neighbours are already linked into an edge
    comparison it cannot make (`map.lua:459`), so the load ends with `*** FATAL LUA ERROR` and
    the level never opens. This asks the port whether any such node exists, on the roads the
    `navgraph` stage would write from THIS graph.
    """
    try:
        from cs2beamng.beamng import map_lua
        from cs2beamng.stages.navgraph import decal_road
    except ImportError:                          # the app is not installed; this question is its
        return                                   # own, and the bundle-level ones still stand
    stats, risks = map_lua.check([decal_road(road) for road in graph.roads])
    note = (f"{stats['mapNodes']:,} map nodes, {stats['endNodesMerged']:,} terminal nodes merged, "
            f"{stats['chords']} triangles")
    if risks:
        note += (f"; first {risks[0]['node']} - road {risks[0]['road']}, chord "
                 f"{risks[0]['chord']}")
    results["map.lua can build a graph from these roads without raising"] = {
        "total": stats["mapNodes"], "agree": stats["mapNodes"] - stats["chords"], "note": note}


def _connectivity(graph, results):
    stats = graph.stats
    results["the drivable network is one connected component"] = {
        "total": stats["roads"], "agree": stats["largestComponent"], "exact": False,
        "pass": 0.95,
        "note": f"{stats['components']} components, {stats['endsWithNoJunction']:,} segment ends "
                f"weld to nothing"}


def _false_welds(graph, results):
    """Pairs a consumer's radius merge would join across a height difference — the bridge trap.

    BeamNG's `mergeOverlappingNodes` broad-phases in 2-D and tests in 3-D against the LARGER of
    the two radii, so a 24 m road's node reaches 12 m in every direction including down.
    """
    from scipy.spatial import cKDTree

    points, radii, roads, owners = [], [], [], []
    for road in graph.roads:
        # a connector's key names both segments it ties together, so two roads that MEET have a
        # segment in common; a pair that shares one is adjacent by construction and a height
        # difference between them is a ramp, not a false weld
        own = frozenset(part for part in road.key.split("_")[1:] if part.isdigit())
        for node in road.nodes:
            points.append(node[:3])
            radii.append(0.5 * node[3])
            roads.append(road.key)
            owners.append(own)
    points = numpy.asarray(points, dtype=numpy.float64)
    radii = numpy.asarray(radii)
    tree = cKDTree(points)
    worst = []
    checked = 0
    for i, radius in enumerate(radii):
        for j in tree.query_ball_point(points[i], float(radius)):
            if j <= i or roads[j] == roads[i] or (owners[i] & owners[j]):
                continue
            checked += 1
            dy = abs(points[i][1] - points[j][1])
            if dy > VERTICAL_SEPARATION:
                worst.append((dy, roads[i], roads[j]))
    worst.sort(reverse=True)
    note = f"{checked:,} node pairs are inside a merge radius"
    if worst:
        note += f"; worst {worst[0][0]:.1f} m between {worst[0][1]} and {worst[0][2]}"
    results["no two navgraph nodes weld across a height difference"] = {
        "total": checked or 1, "agree": (checked or 1) - len(worst), "exact": True, "note": note}


def _attributes(bundle, graph, results):
    """Direction and speed limit come straight off the segment; they must survive verbatim."""
    segments = {}
    for record in reader.read_jsonl(reader.layer_path(bundle, "networks")):
        a = record.get("attrs") or {}
        if a.get("category") in navgraph_mod.DEFAULT_CATEGORIES:
            segments[record["id"]] = a
    direction = [0, 0]
    speed = [0, 0]
    for road in graph.roads:
        if road.kind != "segment":
            continue
        a = segments.get(road.segment) or {}
        direction[1] += 1
        direction[0] += int(road.oneway == (a.get("direction") == "oneway"))
        if a.get("limit") is not None:
            speed[1] += 1
            speed[0] += int(road.speed_kph == a["limit"])
    # NOT exact, and the reason is worth writing down: `attrs.direction` is the ASSET's authored
    # property and `Invert` is what the game's own traffic uses. Where they disagree (three
    # alleys on the reference city, marked `twoway` with no lane running back) the lanes win,
    # because a lane is what a vehicle occupies.
    results["a segment road's direction agrees with attrs.direction"] = {
        "total": direction[1], "agree": direction[0], "exact": False, "pass": 0.999,
        "note": f"{direction[1] - direction[0]} disagree; the lane layer's `Invert` is taken as "
                f"the authority over the asset's authored `direction`"}
    results["a segment road carries CS2's own speed limit"] = {
        "total": speed[1], "agree": speed[0], "exact": True}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
