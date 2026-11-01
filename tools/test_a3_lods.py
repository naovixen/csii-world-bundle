"""cs2beamng A3 gate, part 2c — the LOD ladder and Torque's detail selection.

Run: `python tools/test_a3_lods.py`. Split from `test_a3_rules.py` (refactor Phase 4): LOD switch
points may only ever DEFER a generated level, never advance it (A6.7); the coarsest level and
the tree billboard sit one pixel over the null; and the selection rule every ladder is written
against, pinned against `tools/beamng/lod_audit.py`.
"""

import itertools
import os
import sys
import tempfile

import numpy
from a3common import check, finish, start


def _switch_fixture(tmp):
    """An 8-triangle box mesh plus its two pre-written generated levels (4 and 2 triangles)."""
    from cs2beamng.beamng import dae
    from csiibundle import glbwrite

    box_v = numpy.array(
        [[0.0, 0, 0], [4, 0, 0], [4, 0, 6], [0, 0, 6], [0, 3, 0], [4, 3, 0], [4, 3, 6], [0, 3, 6]]
    )
    box_t = numpy.array(
        [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [2, 3, 7], [2, 7, 6]]
    )
    os.makedirs(os.path.join(tmp, "lods"), exist_ok=True)
    for level in (1, 2):
        glbwrite.write_glb(
            os.path.join(tmp, "lods", f"src_LOD{level}.glb"),
            [
                {
                    "name": "box",
                    "primitives": [
                        {
                            "verts": box_v,
                            "normals": None,
                            "uvs": None,
                            "tris": box_t[: 4 if level == 1 else 2],
                            "material": "mat",
                        }
                    ],
                }
            ],
        )
    return dae.Mesh("box", box_v, box_t, [("mat", len(box_t))])


def test_switch_points_only_ever_defer():
    """The A6.7 regression, as a gate: a measured displacement may push a generated level FURTHER
    out than the ladder puts it, never nearer.

    Allowing both directions shipped 2,586 of 2,903 prop shapes onto their decimated geometry
    closer than the ladder ever had — a streetlight's 24-triangle level from 13 m — and the
    level came back "cranes rotated and floating, highrises all diagonal". Surface distance is
    not visual error for thin geometry; the ladder's size-keyed spacing protects it."""
    print("LOD switch points defer, never advance")
    from cs2beamng.stages import shapelods, shapewrite  # noqa: F401

    with tempfile.TemporaryDirectory() as tmp:
        mesh = _switch_fixture(tmp)
        options = {"generate_lods": True, "switch_error_px": 6.0}
        moved = 0
        for null_px in (3.0, 9.0, 40.0):
            for metres in (None, 0.0, 0.001, 0.5, 5.0):
                _levels, stats, meshes = shapelods.lod_levels(
                    tmp,
                    "src",
                    "shape",
                    mesh,
                    null_px,
                    8.0,
                    [metres, metres],
                    "prop",
                    {},
                    {},
                    "level",
                    options,
                )
                sizes = stats["detailSizes"]
                ladder = [
                    max(2, int(round(null_px * r)))
                    for r in reversed(shapelods.LADDER_RATIOS[: len(meshes)])
                ]
                check(len(meshes) == 3, f"nullPx {null_px}: the two generated levels loaded")
                check(
                    all(got <= want + 0.5 for got, want in zip(sizes, ladder)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} never above the ladder {ladder}",
                )
                check(
                    all(a > b for a, b in itertools.pairwise(sizes)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} strictly decreases",
                )
                if any(got < want - 0.5 for got, want in zip(sizes, ladder)):
                    moved += 1
        check(moved > 0, f"and a level that IS out of shape is deferred ({moved} of 15 cases)")
        # generated levels are for PROPS only: a tree is LOD 0 then the billboard (the game's
        # own trees' LOD 1 is an impostor card), a building LOD 0 / its chain and the far ring
        # (a tree loads them for their switch SIZES; `shapebillboard.vegetation_imposter` then
        # replaces every one with the billboard at the first one's size)
        _levels, bld, _m = shapelods.lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "building", {}, {}, "level", options
        )
        check(bld["levels"] == 1, f"a building loads no generated level ({bld['levels']})")
        _levels, veg, _m = shapelods.lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "tree", {}, {}, "level", options
        )
        check(veg["levels"] == 3, f"a tree still loads them for their sizes ({veg['levels']})")


def test_torque_detail_selection():
    """The rule every ladder here is written against, stated once: Torque draws the detail with
    the LARGEST size not above the projected pixel size, so a level is drawn for p >= its size
    and a level's own size is where it stops. A null under the coarsest level stops nothing
    nearer than that level's size; a null ABOVE a level's size hides that level."""
    print("Torque's detail selection")
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    sel = audit.select
    check(sel([44, 3], 2, None, 100) == "mesh:0", "p=100: the 44 px level")
    check(sel([44, 3], 2, None, 10) == "mesh:1", "p=10: the 3 px level (drawn down to 3 px)")
    check(sel([44, 3], 2, None, 2.5) == "null", "p=2.5: the null - nothing drawn")
    check(
        sel([44], 3, None, 10) == "null",
        "a null ABOVE the coarsest size hides everything under 44 px",
    )
    check(
        sel([44, 3], 2, 2.5, 2.7) == "billboard",
        "a billboard between the coarsest level and the null",
    )
    check(sel([200, 20], 5, None, 4) == "none", "under every size: nothing")


def test_budget_detail_at_matches_the_audit():
    """Two implementations of Torque's one rule - `budget.detail_at` (what the budget instrument
    counts) and `lod_audit.select` (what the audit checks) - must agree on every ladder, null
    and billboard, or one of them is not measuring the engine (E5)."""
    print("budget.detail_at agrees with lod_audit.select")
    import importlib.util
    import random

    from cs2beamng.beamng import budget

    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    rng = random.Random(7)
    disagreements = 0
    for _ in range(300):
        sizes = sorted({rng.randint(2, 800) for _ in range(rng.randint(1, 3))}, reverse=True)
        null_px = rng.choice([None, rng.randint(1, sizes[-1] + 5)])
        bb = rng.choice([None, rng.randint(1, sizes[-1] + 5)])
        levels = {s: 100 * (i + 1) for i, s in enumerate(sizes)}
        for p in (0.5, 1, 3, 10, 40, 200, 900):
            picked = audit.select(sizes, null_px, bb, p)
            expect = (
                0
                if picked in ("null", "none")
                else budget.BILLBOARD_TRIANGLES
                if picked == "billboard"
                else levels[sizes[int(picked.split(":")[1])]]
            )
            # `detail_at` takes a radius/distance; feed it a pair that projects to exactly p px
            got = budget.detail_at(
                levels, p, budget.screen_px(1.0, 1.0), null_px=null_px, billboard_px=bb
            )
            disagreements += int(got != expect)
    check(disagreements == 0, f"the two selections agree on 2,100 cases ({disagreements} differ)")


def test_billboard_runs_to_the_null():
    """A billboard detail's SIZE is its far stop, like any Torque detail - vanilla's
    pine_radiata_forest.dae ships meshes a800/a350/a150 and `bb_autobillboard25`, the card one
    step over nothing. Written at the takeover size instead (v30-v33), the band [null, bb)
    selected the null: SpruceTree01 (mesh 655, bb 164, null 27) drew NOTHING from 99 m to
    601 m while every level and the billboard individually looked legal (audit R8 pins this)."""
    print("the billboard runs to the null")
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    sel = audit.select
    # the defect shape: billboard at the takeover size - the mid-range selects the null
    check(sel([655], 27, 164, 700) == "mesh:0", "near: LOD 0")
    check(sel([655], 27, 164, 300) == "billboard", "the takeover band still draws the card")
    check(sel([655], 27, 164, 100) == "null", "the defect: [null, bb) draws NOTHING mid-range")
    # the fix: billboard adjacent to the null - the card covers the whole band to the cull
    check(sel([655], 27, 28, 300) == "billboard", "fixed: the card at 300 px")
    check(sel([655], 27, 28, 100) == "billboard", "fixed: the card at 100 px")
    check(sel([655], 27, 28, 27.5) == "null", "the null is still the far stop")


def test_coarsest_level_stops_one_pixel_over_the_null():
    """A Torque detail's size is where it STOPS, so the coarsest generated level sits ONE pixel
    over the null - the same rule as the tree billboard (`bb = null + 1`, R8). At 1.5x the null
    (the ladder's old last rung) every generated-ladder shape stopped at D_null / 1.5 while the
    report read D_null off the null, so `lod_detail_scale` 4 behaved as 6 and the draw distance
    the build printed was 1.5x optimistic (validation pass 2026-08-31, A5)."""
    print("the coarsest generated level stops one pixel over the null")
    from cs2beamng.stages import shapelods

    with tempfile.TemporaryDirectory() as tmp:
        mesh = _switch_fixture(tmp)
        options = {"generate_lods": True, "switch_error_px": 6.0}
        for null_px in (3.0, 9.0, 40.0):
            _levels, stats, meshes = shapelods.lod_levels(
                tmp,
                "src",
                "shape",
                mesh,
                null_px,
                8.0,
                [None, None],
                "prop",
                {},
                {},
                "level",
                options,
            )
            sizes = stats["detailSizes"]
            check(
                len(meshes) == 3 and sizes[-1] == int(round(null_px + 1)),
                f"nullPx {null_px}: the coarsest level is at {sizes[-1]} px (null + 1), ladder {sizes}",
            )


def test_tree_with_one_generated_level_keeps_its_billboard():
    """A tree hands from LOD 0 straight to the engine billboard; the band the card needs is
    between LOD 0's own size (where it stops) and the null. The guard used to read the first
    GENERATED level's size instead - with that level one pixel over the null (A5) it read "no
    band" and 28 trees shipped a bare mid-level and no card (audit R3, Batch 1 rebuild)."""
    print("a tree with one generated level at the floor still gets its billboard")
    from types import SimpleNamespace

    from cs2beamng.beamng import dae
    from cs2beamng.stages import shapebillboard

    def job():
        return SimpleNamespace(kind="tree", options={"imposters": True})

    tri = numpy.array([[0, 1, 2]], dtype=numpy.int64)
    verts = numpy.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=numpy.float32)
    lod0 = dae.Mesh("lod0", verts, tri, [("m", 1)])
    lod1 = dae.Mesh("lod1", verts, tri, [("m", 1)])
    null = 10.0
    levels = [(50, [lod0]), (int(null) + 1, [lod1])]  # LOD 0 stops at 50 px; LOD 1 at null + 1
    stats = {}
    out, bb = shapebillboard.vegetation_imposter(job(), levels, None, null, stats)
    check(len(out) == 1 and out[0][0] == 50, f"LOD 0 alone remains ({[p for p, _m in out]})")
    check(bb == null + 1, f"and the card runs from LOD 0's stop to the null ({bb} px)")
    check(
        stats.get("imposter", {}).get("handoverPx") == 50, "the hand-over recorded is LOD 0's stop"
    )
    # a runtime billboard is for vegetation only (C3): a prop or a building never earns one,
    # whatever its reach - the engine bakes one atlas per shape at play time and never frees it
    opts = {"imposters": True, "imposter_min_metres": 0.0, "level_of_detail": 0.7}
    size = numpy.array([30.0, 30.0, 60.0])
    bb, _null = shapebillboard.imposter_thresholds(20.0, 4.0, size, 0.0, opts, None, kind="prop")
    check(bb is None, "a prop gets no runtime billboard")
    bb, _null = shapebillboard.imposter_thresholds(
        20.0, 4.0, size, 0.0, opts, None, kind="building"
    )
    check(bb is None, "a building gets no runtime billboard")
    bb, null_out = shapebillboard.imposter_thresholds(20.0, 4.0, size, 0.0, opts, None, kind="tree")
    check(bb is not None and null_out < 20.0, f"a tree does ({bb} px, null {null_out:.1f})")
    # no band at all: LOD 0 itself stops at the null - nothing to replace
    out, bb = shapebillboard.vegetation_imposter(
        job(), [(int(null) + 1, [lod0]), (int(null) + 1, [lod1])], None, null, {}
    )
    check(bb is None and len(out) == 2, "a tree whose LOD 0 already stops at the null gets no card")


def main():
    start()
    test_switch_points_only_ever_defer()
    test_coarsest_level_stops_one_pixel_over_the_null()
    test_tree_with_one_generated_level_keeps_its_billboard()
    test_torque_detail_selection()
    test_billboard_runs_to_the_null()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_lods.py`."""
    assert main() in (None, 0)
