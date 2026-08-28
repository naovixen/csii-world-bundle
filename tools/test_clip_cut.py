"""Terrain clip cut: no ground may be deleted that nothing is drawn over.

The game never cuts terrain geometry. It draws the whole heightfield and discards per pixel
against a stencil built from the §8c clip volumes (`GenerateClipDataJob` ->
`TerrainSurface.RenderClipAreas`). A mesh cut at 3.5 m can only approximate that boundary, and
this test pins which way it errs.

WHAT THIS TEST SCORES AGAINST, AND WHY IT CHANGED (2026-08-11, second round). It used to score
the deleted set against the clip PRISM: sky meant "deleted where no prism reaches". That budget
was met — 1 m² over the width-transition window — while two triangles of ground were plainly
missing on screen, because the prism is not the pavement. At a node the clip fan runs out to the
segment's own side curve and the road mesh reaches only part of that sector (measured: short by
2.2 m at the rail's start, 4.4 m at the junction centre), so terrain deleted well inside the
prism had nothing over it. A test that cannot tell those apart cannot gate this rule.

So the primary score is now against the REAL segment meshes out of `networks/seg_*.glb`: a
pixel is sky when the cut deleted the ground AND no road triangle covers it. The prism check is
kept as a secondary, because it still catches the other failure direction.

The shipped rule that follows from this: a shallow band flattens but never carves; only a
trench or portal band (`top > _CLIP_DEEP_BAND_TOP`) deletes. See `_contain_faces`.

WHAT THIS SETTLED (2026-08-11). Three defects were reported after the split-band round — gaps at
junction intersections, square terrain overlapping cutout walls, and terrain clipping through
roads. Measured here, the first is the harness deleting terrain no prism covers, and its cause
was the deletion rule ANDing a face's four CORNER CELLS against the UNION of every prism: at a
junction the four corners are covered by four DIFFERENT prisms while the face's own interior
escapes them all. Sampling the face's interior instead (`_contain_faces`) removes it.

  window                    corner-AND | above     face interior (shipped)
  train tracks (873,1442)        1,380 m² sky            13 m²
  road junction (2936,1397)        398 m² sky            16 m²
  trench cluster (-2650,3850)      197 m² sky             1 m²
  deep cluster (2750,-350)         741 m² sky             8 m²
  clip areas (450,-2900)           618 m² sky             3 m²

The rule under test is the harness's own — `_contain_faces` and `_fill_clip_quad` are extracted
from the harness source (which imports `bpy` at module scope, so it cannot be imported under
system Python — the same trick as `tools/test_decal_rows.py`). The TRUTH it is scored against is
built here, at a finer sampling than the rule uses, so agreement means the cut respects the
prisms rather than that one rasteriser agrees with itself.

The scanline fuzz — both span-enumerator paths forced on every random quad — lives in
`tools/test_clip_fuzz.py`, which needs no bundle; this file is the half that scores the real
city's windows.

Run: `python tools/test_clip_cut.py`  (system Python + numpy + PIL; skipped when no bundle is on
disk).
"""

import os
import sys

import numpy
from csiibundle import clipraster, containment

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cliptruth
from cliptruth import TRUTH, carves, load_terrain, quads_of, road_cover, strips_near

BUNDLE = cliptruth.BUNDLE

# The five windows the 2026-08-11 defects were reported and located in: the user's two, the two
# densest deep-band (trench / tunnel-portal) clusters, and the densest clip-area cluster.
WINDOWS = (
    ("train tracks", 873.9, 1442.0, 120.0),
    ("road junction", 2936.9, 1397.5, 120.0),
    ("trench cluster", -2650.0, 3850.0, 120.0),
    ("deep cluster", 2750.0, -350.0, 120.0),
    ("clip areas", 450.0, -2900.0, 120.0),
    # The 2026-08-11 width transition: a 24 m Medium Road narrowing to an 8 m Alley
    # Oneway, whose node fans opened the two triangles the prism score could not see.
    ("width transition", 4324.5, 1550.5, 120.0),
)

# Sky, in m² per window, that the shipped rule is allowed to leave. Above zero only because the
# containment test samples at `_CLIP_CONTAIN_SAMPLES` per cell and the truth here is finer; the
# residual is sub-sample sliver along a prism wall.
SKY_BUDGET = 25.0

# The one that matters: ground deleted with no road mesh over it, per window, outside the two
# deliberately-open classes below. The shipped rule leaves 0.0-0.2 m² — sub-sample sliver where
# the exemption footprint and the cut disagree by less than a truth pixel — so a budget of 1 m²
# is a real gate rather than a formality. Verified by probe: letting shallow bands carve again
# takes it to 4.3 / 2.3 / 0.9 / 0.9 / 14.1 m² and fails the width-transition window, which is
# the one the two triangles were reported in.
ROAD_SKY_BUDGET = 1.0

# Everything under test now lives in `csiibundle` rather than being regexed out of the harness
# (which imports `bpy` at module scope and cannot be imported under system Python). `H` stays as
# the one place naming what this file pins, so a rename in the package fails loudly right here.
H = {
    "_CLIP_CONTAIN_SAMPLES": containment.CLIP_CONTAIN_SAMPLES,
    "_CLIP_CONTAIN_BLOCK": containment.CLIP_CONTAIN_BLOCK,
    "_CLIP_DEEP_BAND_TOP": containment.CLIP_DEEP_BAND_TOP,
    "_fill_clip_quad": clipraster.fill_clip_quad,
    "_dilate": clipraster.dilate,
    "_contain_faces": containment.contain_faces,
}

failures = 0


def _window_frame(label, cx, cz, radius, heights, cell, origin_x, origin_z):
    """The window's terrain slice, its records and its cell-space mapping, or None when empty."""
    raster_h, raster_w = heights.shape
    col0 = max(0, int((cx - radius - origin_x) / cell) - 2)
    col1 = min(raster_w - 1, int((cx + radius - origin_x) / cell) + 2)
    row0 = max(0, int((origin_z - (cz + radius)) / cell) - 2)
    row1 = min(raster_h - 1, int((origin_z - (cz - radius)) / cell) + 2)
    lat_h, lat_w = row1 - row0 + 1, col1 - col0 + 1
    terrain = heights[row0 : row1 + 1, col0 : col1 + 1]
    records = strips_near(cx - radius, cx + radius, cz - radius, cz + radius)

    def to_cell(points):
        return numpy.stack(
            (
                (points[:, 0] - origin_x) / cell + 0.5 - col0,
                (origin_z - points[:, 2]) / cell + 0.5 - row0,
            ),
            axis=-1,
        )

    quads = [q for record in records for q in quads_of(record, to_cell, cell)]
    # Only a carving record reaches the deletion rule, exactly as `_clip_grid` bins it. Every
    # record still fills `cell_cut`, which is the refine corridor, not the cut.
    cutting = [q for record in records if carves(record) for q in quads_of(record, to_cell, cell)]
    if not quads:
        print(f"  {label:<16} no clip volumes in window — skipped")
        return None
    return terrain, records, to_cell, quads, cutting, (lat_h, lat_w, row0, col0)


def _harness_rule(terrain, quads, cutting, lat_h, lat_w):
    """The shipped deletion rule on this window: `(contain, deep_touch)`."""
    cell_cut = numpy.zeros((lat_h, lat_w), dtype=bool)
    for quad, lane_y, bottom, top in quads:
        H["_fill_clip_quad"](cell_cut, terrain, lat_w, lat_h, quad, lane_y, bottom, top)
    contain, _band_count, deep_touch = H["_contain_faces"](
        lat_w,
        lat_h,
        terrain,
        cell_cut,
        lambda r0, c0, r1, c1: iter(cutting),
        H["_CLIP_CONTAIN_SAMPLES"],
        H["_CLIP_CONTAIN_BLOCK"],
    )
    return contain, deep_touch


def check(label, cx, cz, radius, heights, cell, origin_x, origin_z):
    """Score the harness's deletion mask on one window against a finer independent truth."""
    global failures
    frame = _window_frame(label, cx, cz, radius, heights, cell, origin_x, origin_z)
    if frame is None:
        return
    terrain, records, to_cell, quads, cutting, (lat_h, lat_w, row0, col0) = frame
    contain, deep_touch = _harness_rule(terrain, quads, cutting, lat_h, lat_w)
    fine_y, band, footprint = cliptruth.truth_rasters(terrain, quads, lat_h, lat_w)
    fine_h, fine_w = band.shape

    area = (cell / TRUTH) ** 2
    deleted = numpy.kron(contain, numpy.ones((TRUTH, TRUTH), dtype=bool))
    sky = float((deleted & ~footprint).sum()) * area
    band_area = float(band.sum()) * area
    deleted_area = float(contain.sum()) * cell * cell
    ratio = deleted_area / band_area if band_area else 0.0
    poke = float((~deleted & band).sum()) * area

    # The primary score: ground deleted with no road triangle over it. This is what a viewer
    # sees as a hole, and the only measure that would have caught the width-transition notches.
    #
    # Two exclusions, and they are exclusions of SCOPE, not amnesties. A tunnel portal's mouth
    # is deliberately open — that is the whole reason a deep band carves — and its road mesh
    # runs down inside the hill rather than over the hole, so it can never "cover" it. A lot
    # clip is covered by the building or area surface that owns it, which is not a network mesh
    # and is not this test's subject. What is left is exactly the class the notches belong to:
    # ground under a SHALLOW network band. That is 0 while only deep bands carve, and it is
    # what goes non-zero the moment a shallow band is let back into the deletion — which is the
    # regression this test exists to catch.
    # NOTE the exemption is defined here, NOT as `carves()`. Deriving it from the rule under
    # test makes the test vacuous: flip the rule and the exemption flips with it, so every
    # deletion is excused and the score reads 0 whatever the rule does. (Tried; it read 0.8 m²
    # for the very behaviour that put two triangles on screen.)
    cover = road_cover(cx, cz, radius, origin_x, origin_z, col0, row0, cell, fine_h, fine_w)
    seen_through = None
    if cover is not None:
        exempt = numpy.zeros((fine_h, fine_w), dtype=bool)
        for record in records:
            deep = record.get("top") is None or float(record["top"]) > H["_CLIP_DEEP_BAND_TOP"]
            if not (record.get("kind") == "area" or deep):
                continue
            for quad, _lane_y, _bottom, _top in quads_of(record, to_cell, cell):
                local = [((px - 0.5) * TRUTH, (py - 0.5) * TRUTH) for px, py in quad]
                H["_fill_clip_quad"](exempt, fine_y, fine_w, fine_h, local, 0.0, -1e18, 1e18)
        seen_through = float((deleted & ~cover & ~exempt).sum()) * area

    ok = True
    if seen_through is not None and seen_through > ROAD_SKY_BUDGET:
        print(
            f"  FAIL {label}: {seen_through:.1f} m² of ground deleted with no road mesh over "
            f"it (budget {ROAD_SKY_BUDGET:.1f} m²) — visible holes"
        )
        ok = False
    if sky > SKY_BUDGET:
        print(
            f"  FAIL {label}: {sky:.0f} m² deleted where no clip prism reaches "
            f"(budget {SKY_BUDGET:.0f} m²) — see-through gaps"
        )
        ok = False
    if ratio > 1.0:
        print(
            f"  FAIL {label}: cut {deleted_area:.0f} m² against {band_area:.0f} m² clipped "
            f"(x{ratio:.2f}) — above 1.00 is sky"
        )
        ok = False
    if not ok:
        failures += 1
        return
    # Faces a TRENCH or PORTAL band meets with no deleted face beside them — a strip too narrow
    # for a whole face to sit inside, so containment deletes nothing along it and the snap has
    # no hole rim to push out. `build_terrain` re-cuts exactly these at the containment
    # sampling; the figure is reported so their number stays visible, not because they are left.
    recut = int((deep_touch & ~contain & ~H["_dilate"](contain, 1)).sum())
    hole = f"{seen_through:5.1f} m²" if seen_through is not None else "  (no meshes)"
    print(
        f"  ok   {label:<16} hole {hole}   sky {sky:5.0f} m²   cut/clipped x{ratio:.2f}   "
        f"kept-in-band {100 * poke / max(band_area, 1e-9):4.1f}%   "
        f"re-cut {recut * cell * cell:5.0f} m²   "
        f"({len(records)} record(s), {len(cutting)} carving quad(s))"
    )


def main():
    if not os.path.isfile(os.path.join(BUNDLE, "clipvolumes.jsonl")):
        print(f"no bundle at {BUNDLE} — window checks skipped")
        return 1 if failures else 0
    heights, cell, origin_x, origin_z = load_terrain()
    print(
        f"clip cut, {H['_CLIP_CONTAIN_SAMPLES']} sample(s)/cell against {cell / TRUTH:.2f} m "
        f"truth, {cell:g} m faces:"
    )
    for label, cx, cz, radius in WINDOWS:
        check(label, cx, cz, radius, heights, cell, origin_x, origin_z)
    print("clip cut: FAILED" if failures else "clip cut: ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_clip_cut.py`."""
    assert main() in (None, 0)
