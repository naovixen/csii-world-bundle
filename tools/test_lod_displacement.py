"""Tests for `csiibundle.lod`'s displacement half — switch points and surface displacement.

Split from `tools/test_lod.py` (refactor Phase 4); same conventions, same
`python tools/test_lod_displacement.py` run shape.
"""

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


def test_displacement_switch_points():
    print("displacement_switch_px - a level is allowed where its own error is worth N px")
    radius, error = 16.0, 6.0
    tight = lod.displacement_switch_px(radius, 0.02, error)
    loose = lod.displacement_switch_px(radius, 2.0, error)
    check(
        "a level that is further out takes over further out", loose < tight, f"{loose} vs {tight}"
    )
    check(
        "the error at the threshold IS the budget",
        near(lod.displacement_error_px(radius, 2.0, loose), error),
        f"{loose}",
    )
    check(
        "and it is the budget for the tight one too",
        near(lod.displacement_error_px(radius, 0.02, tight), error),
        f"{tight}",
    )
    check(
        "a level identical to LOD 0 is always allowed",
        lod.displacement_switch_px(radius, 0.0, error) == lod.SWITCH_CEILING_PX,
    )
    check(
        "an unmeasured level is treated as identical",
        lod.displacement_switch_px(radius, None, error) == lod.SWITCH_CEILING_PX,
    )
    # the whole point of A6.7, as an invariant rather than a statistic: two shapes of the SAME
    # size whose levels are a hundredfold apart in error get the same threshold from a ladder of
    # ratios - and so a hundredfold difference in what you see - where the rule gives them the
    # same picture and different thresholds. On the reference city that spread is real: 603 of
    # 4,261 LOD1s are identical to LOD 0 and the worst is 37 m out.
    same_ladder = 24 * 16.0  # `nullPx * LADDER_RATIOS[-1]`
    clean = lod.displacement_error_px(radius, 0.02, same_ladder)
    coarse = lod.displacement_error_px(radius, 2.0, same_ladder)
    check(
        "one ratio for both shows a hundredfold difference on screen",
        near(coarse / clean, 100.0, 1e-6),
        f"{coarse:.1f} px vs {clean:.1f} px",
    )
    check(
        "the rule shows the same thing on both",
        near(
            lod.displacement_error_px(radius, 0.02, tight),
            lod.displacement_error_px(radius, 2.0, loose),
        ),
        "",
    )


def test_surface_displacement():
    print("displacement.level_displacement - the distance from a level's surface back to LOD 0's")
    import numpy
    from csiibundle import displacement

    # a 10 m square in the z = 0 plane, as two triangles: a point above it is that high above
    # the SURFACE, which nearest-vertex would answer as the distance to a corner
    verts = numpy.array([[0.0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]])
    tris = numpy.array([[0, 1, 2], [0, 2, 3]])
    d = displacement.distance_to_surface(
        numpy.array([[5.0, 5, 1.0], [1, 1, 0.25], [-3, 5, 0]]), verts, tris
    )
    check("point-to-SURFACE, not point-to-vertex", near(d[0], 1.0) and near(d[1], 0.25), f"{d}")
    check("a point off the edge measures to the edge", near(d[2], 3.0), f"{d[2]}")

    # exactness: the ranking is a lower bound, so a big triangle must not crowd out the small
    # one the point is sitting on (measured at up to 16.9 m wrong before the check was added)
    rng = numpy.random.default_rng(5)
    big = numpy.array([[-50.0, -50, -20], [50, -50, -20], [0, 50, -20]])
    mesh_v = numpy.concatenate([verts, big])
    mesh_t = numpy.concatenate([tris, numpy.array([[4, 5, 6]])])
    points = numpy.column_stack(
        [rng.uniform(1, 9, 200), rng.uniform(1, 9, 200), rng.uniform(0.1, 2.0, 200)]
    )
    got = displacement.distance_to_surface(points, mesh_v, mesh_t)
    check(
        "a distant giant triangle does not hide the near small one",
        numpy.allclose(got, points[:, 2]),
        f"worst {float(numpy.abs(got - points[:, 2]).max()):.4f}",
    )

    # a level that IS the source measures zero; one lifted by a metre measures a metre
    source = {"verts": verts, "tris": tris, "material_runs": [(None, 2)]}
    same, _d, _w = displacement.level_displacement(source, source)
    lifted = dict(source, verts=verts + numpy.array([0.0, 0, 1.0]))
    moved, _d, _w = displacement.level_displacement(lifted, source)
    check("an identical level is zero", near(same, 0.0, 1e-9), f"{same}")
    check("a level lifted 1 m measures 1 m", near(moved, 1.0, 1e-6), f"{moved}")


def main():
    test_displacement_switch_points()
    test_surface_displacement()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all displacement checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_lod_displacement.py`."""
    assert main() in (None, 0)
