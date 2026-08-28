"""Rasterising clip-band quads onto a sample grid, and the mask morphology built on it.

A §8c clip band reaches this module as station quads — four (x, y) points in a cell space
where cell (row, col) spans [col, col+1) × [row, row+1) — and leaves it as marked samples:
`fill_clip_quad` runs an even-odd scanline over the grid's sample points and hands every
covered span to `clip_span`, which applies the band's height test. Two scanline paths exist
because their costs cross over with quad height, and `tools/test_clip_cut.py` fuzzes them
against each other because a fast path that silently disagreed with its slow one is a bug this
repo has already shipped once.

**The band has two heights, not one.** A prism is swept between the road's left and right
curves and those do not sit level: measured over 49,464 strips the difference is 0.000 m at p50
but 0.299 m at p99 and 5.234 m at worst, and 1.0% of strips tilt by more than the whole 0.3 m
roof. Collapsing them to a mean is right in the middle of the carriageway and wrong at both
edges, which is exactly where terrain was still standing through the road. `lane_ramp` carries
the crossfall.

The morphology helpers (`dilate`, `mask_boundary`, `face_footprint_reduce`) are the whole-array
operations the refine corridors are grown from. They live beside the rasteriser because both
run on the same masks and Blender's Python has no scipy to fall back on.
"""

import math

import numpy

# Quads spanning fewer rows than this take the scalar scanline: below it the whole-array
# path's fixed cost (arange / sort / nonzero / clip) outweighs the per-row arithmetic it saves.
# Measured on the full city, where 471k quad fills split roughly two to one short-to-tall.
CLIP_SCANLINE_VECTOR_ROWS = 8


# The producer's own `TerrainRipSystem.ClipBandExpansion` — `GenerateClipDataJob` widens a clip
# volume by this much on both sides AFTER taking the composition's height offsets, so removing it
# from a shipped band's `top` recovers the game's carriageway profile top,
# `cmax(edgeHeight, surfaceHeight.min, edgeHeight) + clipHeightOffset.y`. Not a tuned constant:
# it is one number of the game's, inverted.
CLIP_BAND_EXPANSION = 0.3


def lane_ramp(quad, lane_y):
    """`(uniform lane height, lateral ramp or None)` for one strip quad.

    A clip band is a prism swept between the road's OWN left and right curves, and those two
    curves do not sit at the same height: a cambered, superelevated or side-slope road tilts
    across its width. Collapsing them to their mean makes the band a horizontal slab at
    mid-height, which is right in the middle and wrong at both edges — and the shallow band is
    only 0.8 m tall, so a tilt bigger than that puts one whole edge of the carriageway outside
    its own clip volume. Measured on the 2026-08-12 bundle: |mean(left.y) − mean(right.y)| is
    0.000 m at p50 and 0.019 m at p90 (the mean is fine for almost every strip) but 0.299 m at
    p99 and 5.234 m at worst, and **491 strips of 49,464 (1.0%) tilt by more than the whole
    0.3 m roof**. That 1.0% is exactly the road area still showing ground through it after the
    band floor clamp went in, and strip 1569180 at `(-342.6, 6893.4)` is the case that named
    it: left curve 532.55, right curve 533.82, mean 533.18, so the band tested [532.68, 533.28]
    while the pavement's low edge was at 532.55 and the terrain at 532.62 slipped under the
    slab and stood over the road.

    `lane_y` may be a plain height (an area record, which has no two sides) or a
    `(left, right)` pair. The ramp is a 1-D lateral interpolation along the quad's own
    cross-section — from the midpoint of its two left corners to the midpoint of its two right
    corners — which is what crossfall is. Cell space is an affine, uniformly scaled image of
    world space, so the parameter is the same in the native raster and in the sub-sample frame
    `contain_faces` rescales quads into; neither caller needs to know.

    Falls back to the mean for a degenerate cross-section or an untilted quad, so the uniform
    path stays exactly the arithmetic it always was.
    """
    if not isinstance(lane_y, tuple):
        return lane_y, None
    y_left, y_right = lane_y
    mean = (y_left + y_right) * 0.5
    if y_left == y_right:
        return mean, None
    lx = (quad[0][0] + quad[3][0]) * 0.5
    lz = (quad[0][1] + quad[3][1]) * 0.5
    rx = (quad[1][0] + quad[2][0]) * 0.5
    rz = (quad[1][1] + quad[2][1]) * 0.5
    ax, az = rx - lx, rz - lz
    len2 = ax * ax + az * az
    if len2 < 1e-9:
        return mean, None
    return mean, (y_left, y_right - y_left, lx, lz, ax, az, 1.0 / len2)


def clip_span(
    mask, terrain_y, row, lo, hi, lane_y, ramp, bottom, top, refs, ref_value, ceiling, offset
):
    """Mark one scanline span of a clip quad, and record what the span was marked against.

    The body both scanline paths share, so the two cannot drift apart on it —
    `tools/test_clip_cut.py` fuzzes them against each other precisely because they once did.

    `ceiling`, when given, receives `lane + top - CLIP_BAND_EXPANSION` for every sample inside
    the band: the top of the game's own carriageway cross-section under this strip, which is
    where the game's grading pass leaves the ground at its highest. Writing it here rather than
    re-deriving it later is what keeps `clamp_carriageway` definitionally consistent with the
    band test — the clamp uses the ceiling of the very band that selected the sample, including
    its lateral tilt, with no second opinion about which station or which side.
    """
    if ramp is None:
        lane = lane_y
    else:
        y_left, dy, lx, lz, ax, az, inv_len2 = ramp
        px = numpy.arange(lo, hi + 1, dtype=numpy.float64) + offset
        s = numpy.clip(((px - lx) * ax + (row + offset - lz) * az) * inv_len2, 0.0, 1.0)
        lane = y_left + dy * s

    over = terrain_y[row, lo : hi + 1] - lane
    inside = (over > bottom) & (over < top)
    mask[row, lo : hi + 1] |= inside
    if refs is not None and ref_value >= 0:
        span = refs[row, lo : hi + 1]
        span[inside] = ref_value
    if ceiling is not None:
        span = ceiling[row, lo : hi + 1]
        # Lowest ceiling wins where strips overlap, so a cell claimed by two roads ends up under
        # both of them rather than under whichever was rasterised last. `casting="unsafe"` is
        # only the float64 lane arithmetic landing in the float32 raster, which is deliberate.
        numpy.minimum(
            span, lane + (top - CLIP_BAND_EXPANSION), out=span, where=inside, casting="unsafe"
        )


def spans_scalar(quad, offset, width, row_lo, row_hi):
    """`(row, lo, hi)` for every covered span — even-odd scanline, one row at a time.

    The reference path `fill_clip_quad` dispatches to for short quads. Kept whole rather than
    folded into a branch of the vector path so the two can be fuzzed against each other; see
    `tools/test_clip_cut.py`.
    """
    for row in range(row_lo, row_hi + 1):
        y = row + offset
        crossings = []
        for e in range(4):
            px, py = quad[e]
            qx, qy = quad[(e + 1) & 3]
            if (py <= y) == (qy <= y):
                continue
            crossings.append(px + (y - py) / (qy - py) * (qx - px))
        if len(crossings) < 2:
            continue
        crossings.sort()

        for pair in range(0, len(crossings) - 1, 2):
            lo = max(0, int(math.ceil(crossings[pair] - offset)))
            hi = min(width - 1, int(math.floor(crossings[pair + 1] - offset)))
            if hi < lo:
                continue
            yield row, lo, hi


def spans_vector(quad, offset, width, row_lo, row_hi):
    """`(row, lo, hi)` for every covered span — edge crossings for EVERY row at once.

    Four Python comparisons and a `sort()` per row is cheaper for the short quads that dominate
    by count, and much dearer for the tall ones, so the two paths split on row count and
    `tools/test_clip_cut.py` fuzzes them against each other. (The exact clip reverted on
    2026-08-11 had a fast path that silently disagreed with its slow one; that is the test
    this pairing owes.)
    """
    rows = numpy.arange(row_lo, row_hi + 1, dtype=numpy.int64)
    y = rows + offset
    crossings = numpy.full((4, len(rows)), numpy.inf)
    for e in range(4):
        px, py = quad[e]
        qx, qy = quad[(e + 1) & 3]
        hit = (py <= y) != (qy <= y)
        if not hit.any():
            continue
        # `qy != py` wherever `hit`, so the division is safe on the selected entries alone.
        yh = y[hit]
        crossings[e, hit] = px + (yh - py) / (qy - py) * (qx - px)

    ordered = numpy.sort(crossings, axis=0)  # inf pads sort to the end
    count = numpy.isfinite(ordered).sum(axis=0)

    for pair in (0, 2):  # a quad crosses in 2 or 4 places
        live = numpy.nonzero(count >= pair + 2)[0]
        if not len(live):
            break
        # Centre-coverage on the column axis: entry k's sample sits at k + offset (same
        # convention the row sample at `row + offset` above already uses), so the span
        # [x0, x1] covers exactly the entries with x0 ≤ k + offset ≤ x1. The previous
        # `round()` implicitly put centres at integers — a systematic half-cell bias on
        # this one axis that serrated every near-diagonal cut boundary.
        # `lo` is clamped up to `width`, not `width - 1`: a span entirely off the right edge
        # must still fail `hi >= lo` and write nothing, the way `max(0, ...)` / `min(w - 1, ...)`
        # did when both bounds were scalars.
        los = numpy.clip(numpy.ceil(ordered[pair, live] - offset), 0, width).astype(int)
        his = numpy.clip(numpy.floor(ordered[pair + 1, live] - offset), -1, width - 1).astype(int)
        for k in numpy.nonzero(his >= los)[0]:
            yield int(rows[live[k]]), int(los[k]), int(his[k])


def fill_clip_quad(
    mask,
    terrain_y,
    width,
    height,
    quad,
    lane_y,
    bottom,
    top,
    refs=None,
    ref_value=-1,
    offset=0.5,
    ceiling=None,
):
    """Mark the samples one strip quad clips — even-odd scanline over sample points.

    `quad` is four (x, y) points in cell space, where cell (row, col) spans [col, col+1) ×
    [row, row+1). `offset` says where in that space entry (row, col) of `mask` is sampled:
    `(col + offset, row + offset)`, on BOTH axes. A sample is clipped only where the terrain
    stands *strictly* inside (lane + bottom, lane + top); both ends open, as the producer has
    them.

    Two rasters are built from this one routine and they differ only in the frame the caller
    hands it (see `clip_grid`):

      * the CELL-CENTRE raster, at `offset = 0.5` in the native cell space, entry (row, col) =
        cell (row, col). Contract §3 puts a lattice vertex at a cell centre, so this is the
        sample the refine corridor and the hole-boundary `refs` are keyed on.
      * the SUB-SAMPLE raster of `contain_faces`, which passes quads already scaled into a
        block-local frame of `CLIP_CONTAIN_SAMPLES` samples per cell per axis. Deletion reads
        that one, reduced per face.

    When `refs` is given, every sample this quad cuts also records `ref_value` — the packed
    (strip, station) reference the hole-boundary snap reads back.
    """
    ys = [p[1] for p in quad]
    # Rows whose sample line `row + offset` falls inside the quad's y-range. Exact, so the
    # loop never spins on a row whose scanline cannot cross an edge.
    row_lo = max(0, int(math.ceil(min(ys) - offset)))
    row_hi = min(height - 1, int(math.floor(max(ys) - offset)))
    if row_hi < row_lo:
        return
    spans = spans_scalar if row_hi - row_lo < CLIP_SCANLINE_VECTOR_ROWS else spans_vector
    lane_y, ramp = lane_ramp(quad, lane_y)
    for row, lo, hi in spans(quad, offset, width, row_lo, row_hi):
        clip_span(
            mask,
            terrain_y,
            row,
            lo,
            hi,
            lane_y,
            ramp,
            bottom,
            top,
            refs,
            ref_value,
            ceiling,
            offset,
        )


def face_footprint_reduce(native, row0, col0, nfr, nfc, stride):
    """`(any, all)` of a native flag over each coarse face's real footprint.

    A grid vertex sits at a cell CENTRE (contract §3), so the quad between four vertices
    straddles the four cells they name and everything between — `(stride + 1)²` cells, not
    `stride²`. Reducing over that footprint is what makes the coarse answer mean something, and
    at stride 1 it collapses to exactly the four corner cells the harness has always used.

    Done as `(stride + 1)²` strided views rather than one fancy-indexed gather: stride is 4 on
    the largest map, so this is 25 cheap whole-array ops instead of a 26M-element index array.
    """
    base = native[row0 : row0 + nfr * stride + 1, col0 : col0 + nfc * stride + 1]
    any_ = numpy.zeros((nfr, nfc), dtype=bool)
    all_ = numpy.ones((nfr, nfc), dtype=bool)
    for i in range(stride + 1):
        for j in range(stride + 1):
            sub = base[i::stride, j::stride][:nfr, :nfc]
            if sub.shape != (nfr, nfc):  # a partial last block: leave it to `any`
                continue
            any_ |= sub
            all_ &= sub
    return any_, all_


def dilate(mask, iterations):
    """4-neighbour binary dilation by `iterations` cells, as numpy shifted-slice ORs.

    Blender's Python has no scipy; at `iterations` = 3 this is 12 whole-array ORs, cheap even
    on a 4096² raster.
    """
    out = mask.copy()
    for _ in range(iterations):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def mask_boundary(mask):
    """Cells on either side of a mask's 4-neighbour boundary — the shoreline of `wet`."""
    edge = numpy.zeros_like(mask)
    edge[1:, :] |= mask[1:, :] != mask[:-1, :]
    edge[:-1, :] |= mask[:-1, :] != mask[1:, :]
    edge[:, 1:] |= mask[:, 1:] != mask[:, :-1]
    edge[:, :-1] |= mask[:, :-1] != mask[:, 1:]
    return edge
