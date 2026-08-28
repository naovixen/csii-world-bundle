"""A7.5/A7.6 gate — the navgraph BeamNG's `map.lua` can build, and can locate a car in.

Run: `python tools/test_navgraph_map.py` (seconds on the synthetic cases; the real-bundle check
is skipped when the ModsData export is absent).

The defect this stands on: a level whose `DecalRoad`s put a degree-2 node between two nodes that
are already linked ends the load with

    *** FATAL LUA ERROR: [string "lua/ge/map.lua"]:459: attempt to compare nil with string

`optimizeNodes` drops that node, builds its replacement edge **without a `lanes` field**, and
hands it to `edgeCompare`, whose `lanes` test is the one comparison of the ten carrying no
guard. It is BeamNG's bug, we cannot patch the game's Lua, and the only move available to a level
is never to hand it that shape. Checked here, in three layers:

* the **port** (`beamng.map_lua`) reproduces the crash on the topology that caused it — a
  three-node connector plus a stub between the same two segment ends — and says so;
* the **rule** (`csiibundle.navmerge.simple_graph` and the terminal widths) removes it;
* the **emission** (`stages.navgraph.decal_road` over a real bundle) has none, which is the only
  one of the three that is a statement about what the level will contain.

A7.6 adds the other half of the same lesson — a radius is read as "the car is on this road", so a
node's radius may not reach a road at a different height (`stacked_coverage`). That one did not
crash; it routed the car onto an overpass it could not get to.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))

from cs2beamng.beamng import map_lua  # noqa: E402
from csiibundle import navclearance, navjunction, navmerge  # noqa: E402
from csiibundle import navgraph as navgraph_mod  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f" - {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def road(name, nodes, lanes_left=1, lanes_right=1, speed=13.889, drivability=1.0):
    return {
        "name": name,
        "class": "DecalRoad",
        "autoLanes": False,
        "useSubdivisions": False,
        "drivability": drivability,
        "oneWay": False,
        "lanesLeft": lanes_left,
        "lanesRight": lanes_right,
        "autoJunction": True,
        "speedLimit": speed,
        "nodes": [list(n) for n in nodes],
    }


def test_port_reproduces_the_crash():
    """The shape the reference city loaded with, reduced: two segment ends 0.4 mm apart, a
    connector looping between them through an interior node, and a stub across the same pair."""
    print("map.lua port")
    width = 21.834
    a = (-6249.2453, -6671.4610, 516.062, width)
    b = (-6249.2449, -6671.4611, 516.062, width)
    mid = (-6248.3792, -6672.5536, 516.0635, width)
    roads = [
        road("conn_1_0_2_1", [a, (-6248.5069, -6671.6013, 516.0646, width), mid, b]),
        road("joint_1_0_2_1", [a, b]),
    ]
    stats, _risks = map_lua.check(roads)
    check(
        "a connector and a stub between one pair of nodes reach map.lua:459",
        stats["crashRisks"] > 0,
        f"{stats['chords']} triangles, {stats['crashRisks']} raising",
    )

    # ...and a plain chain of the same roads, with nothing doubled, does not
    straight = [
        road("seg_1", [(0.0, 0.0, 0.0, 8.0), (50.0, 0.0, 0.0, 8.0)]),
        road("seg_2", [(50.0, 0.0, 0.0, 8.0), (100.0, 0.0, 0.0, 8.0)]),
    ]
    stats, _risks = map_lua.check(straight)
    check("a simple chain reaches nothing", stats["chords"] == 0 and stats["crashRisks"] == 0)
    check(
        "the two segment ends at one coordinate merged into one node",
        stats["endNodesMerged"] == 1,
        f"{stats['endNodesMerged']} merged",
    )

    # the radius half of map.lua:1211: same coordinate, widths 3.5 m apart -> still two nodes
    mismatched = [
        road("seg_1", [(0.0, 0.0, 0.0, 8.0), (50.0, 0.0, 0.0, 8.0)]),
        road("seg_2", [(50.0, 0.0, 0.0, 15.0), (100.0, 0.0, 0.0, 15.0)]),
    ]
    stats, _risks = map_lua.check(mismatched)
    check(
        "two ends at one coordinate with unlike widths stay two nodes",
        stats["endNodesMerged"] == 0,
        f"{stats['endNodesMerged']} merged",
    )


def test_the_rule_removes_it():
    """`simple_graph` over the same topology, as `Road` records."""
    print("csiibundle.navgraph")
    width = 21.834
    a = (-6249.2453, 516.062, -6671.4610, width)  # bundle frame: X, Y up, Z
    b = (-6249.2449, 516.062, -6671.4611, width)
    mid = (-6248.3792, 516.0635, -6672.5536, width)
    fields = {
        "kind": "connector",
        "segment": 1,
        "name": None,
        "category": "road",
        "oneway": False,
        "lanes_left": 1,
        "lanes_right": 1,
        "speed_kph": 50.0,
        "elevated": False,
    }
    roads = [
        navgraph_mod.Road(key="conn_1_0_2_1", nodes=[a, mid, b], **fields),
        navgraph_mod.Road(key="joint_1_0_2_1", nodes=[a, b], **dict(fields, kind="joint")),
    ]
    kept, stats = navmerge.simple_graph(roads)
    check(
        "both roads are dropped: each is a self-loop once the ends merge",
        not kept and stats["selfLoopsDropped"] == 2,
        f"{stats}",
    )

    # a real pair of nodes with a real duplicate across it: one edge survives, the geometry wins
    far = (-6200.0, 516.0, -6600.0, width)
    roads = [
        navgraph_mod.Road(key="conn_1_0_2_1", nodes=[a, mid, far], **fields),
        navgraph_mod.Road(key="joint_1_0_2_1", nodes=[a, far], **dict(fields, kind="joint")),
    ]
    kept, stats = navmerge.simple_graph(roads)
    check(
        "a duplicate across one pair keeps exactly the road with the geometry",
        [r.key for r in kept] == ["conn_1_0_2_1"] and stats["duplicateEdgesDropped"] == 1,
        f"{[r.key for r in kept]}, {stats}",
    )

    # a segment is never thinned: a ring road legitimately begins and ends at one node
    ring = navgraph_mod.Road(key="seg_9", nodes=[a, mid, b], **dict(fields, kind="segment"))
    kept, stats = navmerge.simple_graph([ring])
    check("a segment that closes on itself is kept", [r.key for r in kept] == ["seg_9"])


def test_terminal_widths():
    """A connector's ends take the segment's width, not the pair's minimum."""
    print("terminal widths")

    class _Spine:
        def __init__(self, width):  # tuple's one field this reads
            self.width = width

    spines = {7: _Spine(21.834), 9: _Spine(3.5)}
    widths = navjunction.terminal_widths(4, 3.5, spines, (7, 1), (9, 0))
    check(
        "the first node takes its own segment's width",
        widths[0] == 21.834 and widths[-1] == 3.5 and widths[1:3] == [3.5, 3.5],
        f"{widths}",
    )


def test_stacked_roads():
    """A node's radius may not reach a road at a different height (A7.6)."""
    print("stacked roads")
    fields = {
        "kind": "segment",
        "segment": 1,
        "name": None,
        "category": "road",
        "oneway": False,
        "lanes_left": 1,
        "lanes_right": 1,
        "speed_kph": 50.0,
        "elevated": False,
    }
    ground = [(0.0, 100.0, 0.0, 26.0), (200.0, 100.0, 0.0, 26.0)]  # 26 m, radius 13
    deck = [(100.0, 107.0, -60.0, 26.0), (100.0, 107.0, 60.0, 26.0)]  # crossing, 7 m up
    hits = navclearance.stacked_coverage(
        [
            navgraph_mod.Road(key="seg_1", nodes=ground, **fields),
            navgraph_mod.Road(key="seg_2", nodes=deck, **fields),
        ]
    )
    check(
        "a 26 m deck 7 m up covers the road beneath it", bool(hits), f"{len(hits)} covered points"
    )

    # ...and the same pair once the clamp has taken the width below the clearance
    narrow_ground = [(x, y, z, 12.0) for x, y, z, _w in ground]
    narrow_deck = [(x, y, z, 12.0) for x, y, z, _w in deck]
    hits = navclearance.stacked_coverage(
        [
            navgraph_mod.Road(key="seg_1", nodes=narrow_ground, **fields),
            navgraph_mod.Road(key="seg_2", nodes=narrow_deck, **fields),
        ]
    )
    check("at 12 m of width the same pair is clear", not hits, f"{len(hits)} covered points")

    # a ramp climbing away from the road it joins is stacked over it and is NOT a wrong snap
    hits = navclearance.stacked_coverage(
        [
            navgraph_mod.Road(key="seg_7", nodes=ground, **fields),
            navgraph_mod.Road(key="conn_7_1_9_0", nodes=deck, **dict(fields, kind="connector")),
        ]
    )
    check(
        "a road sharing a segment with the one above it is exempt",
        not hits,
        f"{len(hits)} covered points",
    )


def test_real_bundle():
    """The only layer that is a statement about the level: what the stage would write."""
    from csiibundle.bake.locate import default_bundle_dir

    bundle = default_bundle_dir()
    if not bundle or not os.path.isdir(bundle):
        print("real bundle: skipped (no ModsData export)")
        return
    print(f"real bundle {bundle}")
    from cs2beamng.stages.navgraph import decal_road

    graph = navgraph_mod.build(bundle)
    stats, risks = map_lua.check([decal_road(r) for r in graph.roads])
    covered = navclearance.stacked_coverage(graph.roads)
    check(
        "no road point sits inside the radius of a road at another height",
        not covered,
        f"{len(covered)} covered points"
        + (f", first {covered[0]['road']} under {covered[0]['other']}" if covered else ""),
    )
    check(
        "no emitted DecalRoad puts map.lua on a triangle",
        stats["chords"] == 0 and stats["crashRisks"] == 0,
        f"{stats['chords']} triangles, {stats['crashRisks']} raising"
        + (f", first {risks[0]}" if risks else ""),
    )
    check(
        "the graph is still one network",
        graph.stats["largestComponentFraction"] >= 0.95,
        f"{graph.stats['components']} components, largest "
        f"{graph.stats['largestComponentFraction']:.1%}",
    )


def main():
    test_port_reproduces_the_crash()
    test_the_rule_removes_it()
    test_terminal_widths()
    test_stacked_roads()
    test_real_bundle()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print("  " + name)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_navgraph_map.py`."""
    assert main() in (None, 0)
