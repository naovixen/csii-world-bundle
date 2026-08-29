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


def test_worked_example_against_the_decompiled_arithmetic():
    """One chain by hand, from `RequiredBatchesSystem.InitializeBatchGroup` and `MeshSystem`
    (decompiled 2026-08-29), so the port is pinned to the game's numbers and not to itself.

    A 30 × 60 × 30 m box with 4,000 m² of triangles and levels of 15,000 and 4,000 indices:
    * `ObjectInitializeSystem`'s clamp leaves every axis (each cap is 240), rendering size 40.
    * box area 2·(1800 + 1800 + 900) = 9,000; `bias = −log2(sqrt(4000/9000)) = 0.585`.
    * `CalculateLodLimit(40, 0.585)`: 40·2^−0.585 = 26.67, squared 711, cubed 3.59e8 = 2^28.4,
      exponent field 155, limit 255 − 155 = **100** = LOD 0's `m_MinLod`; `min(100, 253)` = 100.
    * coarsest first: the 4,000-index level takes 100; its own size is
      `csum(size)·0.57735/sqrt(4000)` = 1.0954, biased 0.7304, sq 0.5335, cubed 0.1518 = 2^−2.72
      → field 124 → 131, +1 = **132**, clamp(132, 101, 254) = 132.
    * the 15,000-index level takes 132; size 0.5657, biased 0.3772, sq 0.1423, cubed 0.00288 =
      2^−8.44 → field 118 → 137, +1 = **138**, clamp(138, 133, 255) = 138 = LOD 0's limit.
    """
    size = numpy.array([30.0, 60.0, 30.0])
    bias = lod.lod_bias(size, 4_000.0)
    assert abs(bias - 0.585) < 1e-3
    assert lod.rendering_size(size) == 40.0
    assert lod.mesh_min_lod(size, 4_000.0) == 100
    limits = lodchain.authored_lod_limits(
        size, [(size, 15_000), (size, 4_000)], bias, min_lod=lod.mesh_min_lod(size, 4_000.0)
    )
    assert limits == [138, 132, 100]


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
    test_worked_example_against_the_decompiled_arithmetic()
    test_limit_px_matches_game_detail_px_for_a_single_mesh()
    test_lod_level_names_split_a_chain()
    print("  ok   authored LOD chain: limits, pixel sizes, node split")
    return 0


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_lod_authored.py`."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
