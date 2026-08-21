"""Gate for `csiibundle.region` — the build region is ONE shape, and it is a square.

Run: `python tools/test_region.py` (instant, no bundle needed).

This exists because the region was two shapes for months. `stages/networks.py` was changed from
a disc to a square on 2026-08-20 with a post-mortem attached — *"a region build kept a square of
buildings and a disc of roads and the corners of the area had houses and no streets"* — and six
other sites were still discs when refactor Phase 1 counted them: the surface areas, the road
surface that markings and skirts drape onto, the markings themselves, lane geometry, skirts, the
generated LODs, and the AI road graph.

**21.5 % of a region build was in that gap** (a square of half-width r is 4r², the disc inside it
πr²). Measured on the reference city at centre (1399, 1153), half-width 1000 m: 16,786 more
placed objects, 135 more road segments, 887 more surface areas.

So the checks below are not about arithmetic. Each one asks a caller — the real function, not a
copy — whether it agrees with `region.contains`, and the point of the file is that they all do.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

from csiibundle import region  # noqa: E402

CHECKS = 0
FAILS = []


def check(ok, claim, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(claim)
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


BOX = {"centerX": 100.0, "centerZ": -50.0, "radius": 10.0}

# The four corners of the box. Each is at distance r*sqrt(2) from the centre, so a disc of
# radius r drops every one of them — this is exactly the ground that was being lost.
CORNERS = ((110.0, -40.0), (110.0, -60.0), (90.0, -40.0), (90.0, -60.0))


def test_the_shape():
    print("the region is a square, and its corners are inside it")
    for x, z in CORNERS:
        check(region.contains(x, z, BOX), f"corner ({x:.0f}, {z:.0f}) is in the region")
        disc = (x - BOX["centerX"]) ** 2 + (z - BOX["centerZ"]) ** 2 <= BOX["radius"] ** 2
        check(not disc, "...and a disc would have dropped it, which is the whole bug")

    check(not region.contains(110.01, -50.0, BOX), "just past the edge is out")
    check(region.contains(110.0, -50.0, BOX), "exactly on the edge is in")
    check(region.contains(0.0, 0.0, None), "no region means the whole bundle")
    check(
        region.contains(119.0, -50.0, BOX, reach=10.0)
        and not region.contains(121.0, -50.0, BOX, reach=10.0),
        "`reach` grows the box and nothing else",
    )
    check(region.bounds(BOX) == (90.0, 110.0, -60.0, -40.0), "bounds are the box")
    check(region.bounds(None) is None, "and None for no region")
    check(
        region.intersects_box(BOX, 105, -45, 500, 500)
        and not region.intersects_box(BOX, 110.01, -45, 500, 500),
        "a box overlapping the region is kept, one past its edge is not",
    )
    check(region.intersects_box(None, 1e9, 1e9, 2e9, 2e9), "no region intersects everything")


def test_every_caller_agrees():
    print("\nevery region test in either consumer is this one")
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
    from cs2beamng.beamng import placement
    from cs2beamng.stages import derived, navgraph, water
    from csiibundle.bake import markings

    for x, z in CORNERS:
        check(
            placement.in_region({"pos": [x, 0.0, z]}, BOX),
            f"placement.in_region keeps the corner ({x:.0f}, {z:.0f})",
        )
        check(markings._in_region(x, z, BOX), "bake.markings keeps it too")

    class _Road:
        nodes = ((110.0, 0.0, -40.0, 4.0),)

    check(navgraph._in_region(_Road(), BOX), "the AI road graph keeps a road on the corner")

    # A tile whose only overlap with the region is a corner must still be built. The tile at
    # (1, -1) of 100 m spans x 100..200, z -100..0, so it meets the box only at (110, -60).
    check(
        derived._tile_intersects("terraincut_1_-1.glb", 100.0, BOX),
        "a tile the region clips a corner of is still built",
    )
    check(
        water._intersects(BOX, 105.0, -45.0, 500.0, 500.0),
        "water asks the same question of a box",
    )


def main():
    test_the_shape()
    test_every_caller_agrees()
    print()
    if FAILS:
        print(f"{len(FAILS)} of {CHECKS} FAILED:")
        for name in FAILS:
            print("  - " + name)
        return 1
    print(f"{CHECKS}/{CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_region.py`."""
    assert main() in (None, 0)
