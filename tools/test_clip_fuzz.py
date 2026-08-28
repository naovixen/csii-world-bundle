"""The two clip-scanline paths must agree, quad for quad — the fuzz half of the clip-cut gate.

`fill_clip_quad` splits on row count: short quads take `spans_scalar`, tall ones
`spans_vector`. A fast path that silently disagrees with its slow one is what sank the exact
clip reverted on 2026-08-11 (`2878393` -> `22a8ca2`), whose own note says the missing test was
exactly this. So: random quads, including the shapes the layer really ships — triangles with a
doubled corner (`area` records), degenerate zero-area quads (a node strip whose far side
collapses to a point) and self-intersecting bowties — through BOTH enumerators forced on every
quad, and through the real dispatch end to end.

The window-scoring half of the gate (the deletion mask against an independent truth raster on
the real bundle) is `tools/test_clip_cut.py`; this half is pure arithmetic and needs no bundle.

Run: `python tools/test_clip_fuzz.py`  (system Python + numpy)
"""

import math
import random
import sys

import numpy
from csiibundle import clipraster

# What this file pins, by name, so a rename in the package fails loudly right here.
H = {
    "_CLIP_SCANLINE_VECTOR_ROWS": clipraster.CLIP_SCANLINE_VECTOR_ROWS,
    "_lane_ramp": clipraster.lane_ramp,
    "_clip_span": clipraster.clip_span,
    "_spans_scalar": clipraster.spans_scalar,
    "_spans_vector": clipraster.spans_vector,
    "_fill_clip_quad": clipraster.fill_clip_quad,
}


def fill_forced(
    spans, mask, refs, ceiling, terrain, w, quad, lane, bottom, top, offset, row_lo, row_hi
):
    """One quad through ONE forced span enumerator, marking the way `fill_clip_quad` does."""
    lane_u, ramp = H["_lane_ramp"](quad, lane)
    for row, lo, hi in spans(quad, offset, w, row_lo, row_hi):
        H["_clip_span"](
            mask, terrain, row, lo, hi, lane_u, ramp, bottom, top, refs, 7, ceiling, offset
        )


def _random_quad(rng, trial, w, h):
    """One random quad in the shapes the layer really ships, with its band and offset."""
    corner = [(rng.uniform(-3.0, w + 3.0), rng.uniform(-3.0, h + 3.0)) for _ in range(4)]
    shape = trial % 4
    if shape == 1:  # triangle, as `area` records ship
        corner[3] = corner[2]
    elif shape == 2:  # degenerate: both sides collapsed
        corner = [corner[0], corner[1], corner[0], corner[1]]
    elif shape == 3:  # bowtie
        corner = [corner[0], corner[2], corner[1], corner[3]]
    # Half the trials use the tilted `(left, right)` band, half the plain height an `area`
    # record ships. Both reach the same span body, and a ramp that disagreed between the
    # two paths would be exactly the class of defect this fuzz exists for.
    lane = rng.uniform(-2.0, 2.0)
    if trial % 2:
        lane = (lane, lane + rng.uniform(-3.0, 3.0))
    bottom = rng.uniform(-3.0, 0.0)
    top = bottom + rng.uniform(0.0, 4.0)
    offset = rng.choice([0.5, 1.0])
    return corner, shape, lane, bottom, top, offset


def _rasters(h, w):
    return (
        numpy.zeros((h, w), dtype=bool),
        numpy.full((h, w), -1, dtype=numpy.int32),
        numpy.full((h, w), numpy.inf),
    )


def _trial_disagrees(rng, trial):
    """Run one random quad through both forced paths and the dispatch; True on any mismatch."""
    w, h = rng.randint(3, 24), rng.randint(3, 24)
    terrain = numpy.asarray([[rng.uniform(-5.0, 5.0) for _ in range(w)] for _ in range(h)])
    corner, shape, lane, bottom, top, offset = _random_quad(rng, trial, w, h)
    ys = [p[1] for p in corner]
    row_lo = max(0, int(math.ceil(min(ys) - offset)))
    row_hi = min(h - 1, int(math.floor(max(ys) - offset)))

    rows_mask, rows_refs, rows_ceiling = _rasters(h, w)
    both_mask, both_refs, both_ceiling = _rasters(h, w)
    quad_args = (terrain, w, corner, lane, bottom, top, offset, row_lo, row_hi)
    if row_hi >= row_lo:
        fill_forced(H["_spans_scalar"], rows_mask, rows_refs, rows_ceiling, *quad_args)
        fill_forced(H["_spans_vector"], both_mask, both_refs, both_ceiling, *quad_args)
    # The real dispatch end to end too: whichever enumerator it picks, same rasters.
    disp_mask, disp_refs, disp_ceiling = _rasters(h, w)
    H["_fill_clip_quad"](
        disp_mask,
        terrain,
        w,
        h,
        corner,
        lane,
        bottom,
        top,
        refs=disp_refs,
        ref_value=7,
        offset=offset,
        ceiling=disp_ceiling,
    )

    # The ceiling raster carries the lateral ramp, so comparing it is what actually pins the
    # two paths to the same per-sample lane height; the mask alone only sees its sign.
    agrees = (
        numpy.array_equal(rows_mask, both_mask)
        and numpy.array_equal(rows_refs, both_refs)
        and numpy.allclose(rows_ceiling, both_ceiling, equal_nan=True)
        and numpy.array_equal(disp_mask, rows_mask)
        and numpy.array_equal(disp_refs, rows_refs)
        and numpy.allclose(disp_ceiling, rows_ceiling, equal_nan=True)
    )
    if not agrees:
        print(
            f"  FAIL scanline paths disagree: shape {shape}, offset {offset}, "
            f"lane {lane}, quad {corner}"
        )
    return not agrees


def fuzz_scanline_paths(trials=4000, seed=20260811):
    print("clip cut, scanline paths:")
    rng = random.Random(seed)
    bad = sum(1 for trial in range(trials) if _trial_disagrees(rng, trial))
    if bad:
        raise SystemExit(f"FAIL {bad} of {trials} random quads rasterise differently")
    print(f"  ok   scanline paths agree on {trials} random quads (triangles, degenerates, bowties)")


def gate():
    fuzz_scanline_paths()


if __name__ == "__main__":
    sys.path.insert(0, ".")
    gate()
