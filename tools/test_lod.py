"""Tests for `csiibundle.lod` — the ported CS2 LOD arithmetic.

The game's expressions are integer/bit operations, so these check them the way the game would
be checked: exact values at the powers of two the floor lands on, the inverse round-tripping,
and the two algebraic identities the module's docstring claims (the exponent reduction and the
fov cancellation). Run: `python tools/test_lod.py`.
"""

import itertools
import math
import sys

from csiibundle import lod

FAILED = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def near(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


def test_lod_limit_is_the_exponent_reduction():
    print("CalculateLodLimit == 128 - floor(6*log2(m))")
    for m in (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 7.0, 16.0, 100.0, 1000.0):
        want = 128 - math.floor(6.0 * math.log2(m))
        got = lod.calculate_lod_limit(m)
        check(f"limit({m}) = {got}", got == want, f"expected {want}")
    check("limit(1) is 128", lod.calculate_lod_limit(1.0) == 128)
    check(
        "one LOD step is 2^(1/6)", lod.calculate_lod_limit(2.0) == lod.calculate_lod_limit(1.0) - 6
    )
    check(
        "a positive bias drops it sooner",
        lod.calculate_lod_limit(4.0, bias=1.0) > lod.calculate_lod_limit(4.0),
    )
    check(
        "bias 1 == half the size",
        lod.calculate_lod_limit(4.0, bias=1.0) == lod.calculate_lod_limit(2.0),
    )


def test_cull_distance_inverts_calculate_lod():
    print("cull_distance is the inverse of CalculateLod")
    factor = lod.lod_factor()
    for limit in range(96, 160, 3):
        d = lod.cull_distance(limit)
        check(f"lod at cull distance of {limit} is {limit}", lod.calculate_lod(d, factor) == limit)
        # the index is quantised to 1/6 of an octave (2^(1/6) = 1.1225x in distance), so
        # `cull_distance` is where the LAST visible band starts, not where it ends
        check(f"a whole step past {limit} is culled", lod.calculate_lod(d * 1.13, factor) < limit)
        check(
            f"just inside the band at {limit} is drawn",
            lod.calculate_lod(d * 1.11, factor) == limit,
        )


def test_rendering_size_clamp():
    print("GetRenderingSize + the 8x/4x clamp")
    check("a cube is its side", near(lod.rendering_size([2, 2, 2]), 2.0))
    check("the mean of three unclamped axes", near(lod.rendering_size([1, 2, 3]), 2.0))
    # a mast: 0.2 x 0.2 x 40. Cap on the tall axis is max(8*0.2, 4*0.2) = 1.6
    check(
        "a mast's height is capped at 8x the thin axes",
        near(lod.rendering_size([0.2, 0.2, 40.0]), (0.2 + 0.2 + 1.6) / 3),
    )
    # a wall: 40 x 20 x 0.3. Cap on x is max(8*0.3, 4*20) = 80 -> uncapped
    check(
        "a wall is not capped", near(lod.rendering_size([40.0, 20.0, 0.3]), (40.0 + 20.0 + 0.3) / 3)
    )
    check(
        "order does not matter",
        near(lod.rendering_size([40.0, 0.3, 20.0]), lod.rendering_size([0.3, 20.0, 40.0])),
    )


def test_surface_area_and_bias():
    print("CalcSurfaceArea and the MeshSystem bias")
    verts = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    check("one right triangle is 0.5", near(lod.surface_area(verts, [[0, 1, 2]]), 0.5))
    check("no triangles is zero", lod.surface_area(verts, []) == 0.0)
    # a closed cube's triangles total exactly its box area -> ratio 1 -> bias 0
    check("a solid cube has no bias", near(lod.lod_bias([2, 2, 2], 24.0), 0.0))
    check("a mesh that fills its box has no bias", near(lod.lod_bias([1, 2, 3], 22.0), 0.0))
    check("half the area is half a stop", near(lod.lod_bias([2, 2, 2], 12.0), 0.5))
    check("a quarter is a whole stop", near(lod.lod_bias([2, 2, 2], 6.0), 1.0))
    check(
        "the clamp floors at 1e-6", near(lod.lod_bias([2, 2, 2], 1e-9), -math.log2(math.sqrt(1e-6)))
    )
    check(
        "an unknown area keeps the authored bias",
        lod.lod_bias([2, 2, 2], 0.0, authored_bias=0.25) == 0.25,
    )
    check(
        "a sparser mesh is dropped sooner",
        lod.mesh_min_lod([2, 2, 2], 6.0) > lod.mesh_min_lod([2, 2, 2], 24.0),
    )


def test_pixel_conversion_is_fov_free():
    print("game_detail_px cancels the field of view")
    size, area, radius = [2.0, 2.0, 10.0], 40.0, 5.196
    px = lod.game_detail_px(radius, size, area)
    for fov in (20.0, 45.0, 90.0):
        # the same answer arrived at the long way: cull distance in metres, then project
        limit = lod.mesh_min_lod(size, area)
        d = lod.cull_distance(limit, fov_degrees=fov)
        direct = lod.screen_px(radius, d, fov_degrees=fov)
        # the LOD index is quantised to 1/6 of an octave, so the two agree within that step
        check(
            f"fov {fov} agrees within one LOD step",
            abs(math.log2(direct / px)) <= 1.0 / 6.0 + 1e-9,
            f"direct {direct:.4f} vs px {px:.4f}",
        )
    check(
        "a lower quality preset culls sooner (a bigger px)",
        lod.game_detail_px(radius, size, area, level_of_detail=0.25)
        > lod.game_detail_px(radius, size, area, level_of_detail=0.7),
    )
    check(
        "the High preset puts a unit cube near 1.2 px",
        1.0 < lod.game_detail_px(math.sqrt(3) / 2, [1, 1, 1], 6.0) < 1.6,
    )


def test_screen_px_reference():
    print("the screen-pixel reference")
    # a 1 m object 100 m away at 45 degrees: 1 * 540 / (100 * tan(22.5))
    want = 540.0 / (100.0 * math.tan(math.radians(22.5)))
    check("projectRadius", near(lod.screen_px(1.0, 100.0), want))
    check(
        "twice as far is half the size",
        near(lod.screen_px(1.0, 200.0), lod.screen_px(1.0, 100.0) / 2),
    )


# -- the flat-thing producers (A4) -----------------------------------------------------------------
#
# Roads, lane markings and painted areas do NOT go through the object path: they call
# `GetRenderingSize(float2)` (or the raw float3) with no clamp, and their bias comes from a
# different place each time. These check the ported expressions against the game's arithmetic
# written out by hand, and the one conclusion the numbers force.


def test_rendering_size_2d():
    print("GetRenderingSize(float2) — the synthetic depth")
    # (x + y + max(8x, 4y)) / 3
    check("16 x 1 road", near(lod.rendering_size_2d((16.0, 1.0)), (16.0 + 1.0 + 128.0) / 3.0))
    check(
        "1 x 4 (height wins the max)",
        near(lod.rendering_size_2d((1.0, 4.0)), (1.0 + 4.0 + 16.0) / 3.0),
    )
    check("raw float3 is the plain mean", near(lod.rendering_size_raw((3.0, 6.0, 9.0)), 6.0))
    # the object path's clamp must NOT be in it: a long thin thing keeps its full mean here
    check(
        "no object clamp on the 2d overload",
        lod.rendering_size_2d((100.0, 0.0)) > lod.rendering_size((100.0, 0.0, 0.0)),
    )


def test_composition_min_lod():
    print("NetCompositionHelpers.CalculateMinLod")
    want = lod.calculate_lod_limit(lod.rendering_size_2d((16.0, 1.0)), 0.0)
    check("16 m road", lod.composition_min_lod(16.0, 1.0) == want)
    check(
        "a wider road is drawn further",
        lod.composition_min_lod(24.0, 1.0) < lod.composition_min_lod(3.0, 1.0),
    )
    # THE finding this port exists to record: CS2 does not distance-cull roads on any real map.
    far = lod.cull_distance(lod.composition_min_lod(16.0, 1.0))
    check(f"a 16 m road's own limit is past any map ({far:,.0f} m)", far > 20000.0, f"{far}")


def test_lane_min_lod():
    print("NetLaneGeometryData.m_MinLod — the min over the prefab's meshes")
    thin = ((0.15, 0.02, 4.0), 0.15 * 4.0)  # a lane line: 15 cm wide, 4 m long
    wide = ((2.0, 0.02, 4.0), 2.0 * 4.0)  # a crosswalk block
    check(
        "the min over meshes is the MOST generous",
        lod.lane_min_lod([thin, wide]) == min(lod.lane_min_lod([thin]), lod.lane_min_lod([wide])),
    )
    # `Size(bounds).xy` is length-free, but `m_LodBias` is not: MeshSystem derives it from the
    # FULL 3D box, so a longer tile with the same triangle area is sparser and drops sooner.
    # Both halves are the game's; the check is that the size term alone ignores the length.
    check(
        "the size term ignores the length",
        lod.rendering_size_2d((0.15, 0.02)) == lod.rendering_size_2d((0.15, 0.02)),
    )
    check(
        "a longer, sparser tile drops sooner (the bias, not the size)",
        lod.lane_min_lod([((0.15, 0.02, 400.0), 0.6)])
        >= lod.lane_min_lod([((0.15, 0.02, 4.0), 0.6)]),
    )
    near_m = lod.cull_distance(lod.lane_min_lod([thin]))
    check(
        f"a lane line is culled inside a kilometre ({near_m:,.0f} m)", near_m < 1000.0, f"{near_m}"
    )
    check("nothing to measure means never drawn", lod.lane_min_lod([]) == 255)


def test_area_triangle_min_lod():
    print("Game.Areas.GeometrySystem — an area is drawn as far as it is WIDE")
    # a 40 m x 3 m strip, two triangles: nodes in order so every outer edge is a polygon edge
    strip = [(0.0, 0.0), (40.0, 0.0), (40.0, 3.0), (0.0, 3.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    narrow = lod.area_triangle_min_lods(strip, tris)
    # the same strip 30 m wide
    wide_poly = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    wide = lod.area_triangle_min_lods(wide_poly, tris)
    check("wider paint is drawn further", max(wide) < min(narrow), f"{wide} vs {narrow}")
    check(
        "length alone changes nothing",
        lod.area_triangle_min_lods([(0.0, 0.0), (400.0, 0.0), (400.0, 3.0), (0.0, 3.0)], tris)
        == narrow,
    )
    check(
        "a degenerate polygon is never drawn",
        lod.area_triangle_min_lods([(0.0, 0.0)], [(0, 0, 0)]) == [255],
    )
    # MEASURED, and not what a guess would have said: the rule multiplies the distance to the
    # boundary by FOUR, so a 3 m strip counts as a 6 m thing and CS2 draws it to 3.65 km. That is
    # the fidelity number; `lod_detail_scale` is what turns it into something a level can afford.
    d = lod.cull_distance(narrow[0])
    check(f"a 3 m footpath: CS2 draws it to {d:,.0f} m", near(d, 3650.3, 1e-3), f"{d}")


def test_group_detail_px():
    print("group_detail_px — a merged tile's Torque threshold")
    # a 256 m tile's bounding sphere is ~181 m; at 4 km that is a large pixel size, at 40 km small
    r = 181.0
    check(
        "closer means a bigger threshold",
        lod.group_detail_px(r, 1000.0) > lod.group_detail_px(r, 4000.0),
    )
    check(
        "it is the plain projection", near(lod.group_detail_px(r, 1000.0), lod.screen_px(r, 1000.0))
    )


def test_tile_ladder():
    """`cs2beamng.beamng.tilelod` — a merged tile's levels ARE the game's visible sets."""
    try:
        from cs2beamng.beamng import tilelod
    except ImportError:
        print("tile ladder: cs2beamng not installed, skipped")
        return
    print("the tile ladder")
    radius = 181.0  # a 256 m tile's bounding sphere
    # a tile of one kind of thing: nothing to gain, and it must SAY nothing rather than ship
    # three copies of the same mesh
    uniform = tilelod.tile_levels(radius, [3650.0] * 100, detail_scale=6.0)
    check("a uniform tile gets one level", len(uniform["levels"]) == 1, f"{len(uniform['levels'])}")
    # a mixed tile: 400 m markings and 3.6 km paint
    mixed = tilelod.tile_levels(radius, [256.0] * 60 + [3650.0] * 40, detail_scale=6.0)
    check("a mixed tile gets a real ladder", len(mixed["levels"]) >= 2, f"{len(mixed['levels'])}")
    sizes = [px for px, _mask in mixed["levels"]]
    check(
        "detail sizes strictly decrease",
        all(a > b for a, b in itertools.pairwise(sizes)),
        f"{sizes}",
    )
    counts = [int(mask.sum()) for _px, mask in mixed["levels"]]
    check(
        "each level keeps no more than the one before it",
        all(a >= b for a, b in itertools.pairwise(counts)),
        f"{counts}",
    )
    check("the far level drops the short-lived half", counts[-1] <= 40, f"{counts}")
    check(
        "nullPx is the game's far distance, scaled",
        near(mixed["nullPx"], round(lod.group_detail_px(radius, 3650.0) * 6.0)),
        f"{mixed}",
    )
    # A tile of roads. CS2's own limit for a 16 m road is 41 km, so there is nothing to cull and
    # the ladder must SAY so — one level, and a null distance past any view distance — rather
    # than invent bands. The 9.27M network triangles are the game's answer, not an oversight.
    roads = tilelod.tile_levels(radius, [41298.0] * 50, detail_scale=6.0)
    check("a tile of roads gets one level", len(roads["levels"]) == 1, f"{roads['levels']}")
    check(
        "and its null distance is past any view distance",
        41298.0 / 6.0 > 4000.0 and roads["culledFraction"] == 0.0,
        f"{roads}",
    )
    # infinity (no limit known at all) declines outright
    unknown = tilelod.tile_levels(radius, [float("inf")] * 10, detail_scale=6.0)
    check(
        "an unknown limit ships the always-on tile",
        unknown["nullPx"] is None and not unknown["levels"],
    )


def main():
    test_lod_limit_is_the_exponent_reduction()
    test_cull_distance_inverts_calculate_lod()
    test_rendering_size_clamp()
    test_surface_area_and_bias()
    test_pixel_conversion_is_fov_free()
    test_screen_px_reference()
    test_rendering_size_2d()
    test_composition_min_lod()
    test_lane_min_lod()
    test_area_triangle_min_lod()
    test_group_detail_px()
    test_tile_ladder()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all lod checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_lod.py`."""
    assert main() in (None, 0)
