"""The game's own LOD chain: the ported switch limits, and the node split that finds a chain.

Run: `python tools/test_lod_authored.py`; pytest collects `gate()` (tools/conftest.py).
"""

import numpy
from csiibundle import glb, lod, lodchain


def test_authored_limits_are_monotonic_and_bounded():
    size = numpy.array([30.0, 60.0, 30.0])
    # richest first: LOD1 at a quarter of LOD0's 60k indices, LOD2 at a sixteenth
    levels = [(size, 15_000), (size, 4_000)]
    bias = lod.lod_bias(size, 4_000.0)
    limits = lodchain.authored_lod_limits(
        size, levels, bias, min_lod=lod.mesh_min_lod(size, 4_000.0)
    )
    assert len(limits) == 3
    assert all(0 <= x <= 255 for x in limits)
    # the game raises the limit at every finer level: LOD 0 draws nearest, the coarsest furthest
    assert limits[0] > limits[1] > limits[2]
    # the coarsest level keeps LOD 0's single-mesh cull (clamped by the chain length)
    assert limits[2] == min(lod.mesh_min_lod(size, 4_000.0), 255 - 2)
    # and the pixel sizes those limits map to fall with the level, as a Torque ladder must
    px = [lodchain.limit_px(20.0, x) for x in limits]
    assert px[0] > px[1] > px[2] > 0


def test_limit_px_matches_game_detail_px_for_a_single_mesh():
    size = numpy.array([10.0, 8.0, 12.0])
    area = 300.0
    radius = 0.5 * float(numpy.linalg.norm(size))
    single = lod.game_detail_px(radius, size, area)
    via_limit = lodchain.limit_px(radius, lod.mesh_min_lod(size, area))
    # the two routes to "where the game stops drawing this mesh" agree to the limit's
    # 1/6-octave quantisation (12.25 %)
    assert 0.8 < via_limit / single < 1.25


def test_lod_level_names_split_a_chain():
    assert glb.lod_level("Tower") == ("Tower", 0)
    assert glb.lod_level("Tower_LOD2") == ("Tower", 2)
    assert glb.lod_level("Tower_sub3_LOD1") == ("Tower_sub3", 1)


def main():
    test_authored_limits_are_monotonic_and_bounded()
    test_limit_px_matches_game_detail_px_for_a_single_mesh()
    test_lod_level_names_split_a_chain()
    print("  ok   authored LOD chain: limits, pixel sizes, node split")
    return 0


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_lod_authored.py`."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
