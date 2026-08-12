"""Terrain (§8) — the ground grid, and the two rules that decide where it is NOT.

The heightmap is the easy half: `csiibundle.png` decodes it, the manifest's `rasters` block
places it, and a consumer meshes it at whatever resolution it likes. This module is the hard
half — everything about **holes**.

CS2 does not carve tunnels out of its heightmap. The heightmap under a tunnel is intact ground,
and the game hides it at draw time by stencil-clipping against `clipvolumes.jsonl` (§8c): a
prism swept along each road's own left and right curves, spanning a band above and below the
carriageway. A consumer that meshes the heightmap and stops there gets a city whose tunnels are
filled with earth and whose trenches have no walls, and one that deletes everything a prism
touches gets holes in the open sky. `clip_grid` and `contain_faces` are the two answers.

**Only a DEEP band may carve; a shallow one flattens and never deletes.** This is the
containment invariant, and it is the correction that matters most here. A ±0.3 m shell over an
ordinary surface road is not a tunnel — the terrain inside it is under the asphalt that hides
it, so deleting it opens a hole the road does not fill. `CLIP_DEEP_BAND_TOP` is where the line
sits. Where a shallow band selects ground, `clamp_shallow_bands` lowers it to that band's own
floor instead: the same band, including its lateral tilt, with no second opinion.

**A face is deleted only if EVERY sub-sample is inside a band.** Scoring a face by its corners
alone left 197–741 m² of see-through per window; `CLIP_CONTAIN_SAMPLES`² sub-samples take that
to 2–5 m². The trade is one-directional — raising the sample count is always stricter, so it
converts see-through into terrain kept under a road, never the reverse.

**The band has two heights, not one.** A prism is swept between the road's left and right
curves and those do not sit level: measured over 49,464 strips the difference is 0.000 m at p50
but 0.299 m at p99 and 5.234 m at worst, and 1.0% of strips tilt by more than the whole 0.3 m
roof. Collapsing them to a mean is right in the middle of the carriageway and wrong at both
edges, which is exactly where terrain was still standing through the road. `lane_ramp` carries
the crossfall.

**The hole rim moves sideways, never up.** `snap_hole_boundary` pushes a rim vertex OUT to the
prism wall so the hole's edge lands on the tunnel mouth rather than on the nearest cell centre.
Sideways only: lifting a rim vertex to close a gap makes a visible ridge along every portal.

The other half of this module is the LATTICE — a welded corridor of native-resolution cells
around every cut, coarse cells everywhere else, and `transition_fans` joining the two without
T-junctions. It replaced a stride patchwork that produced spikes, chevrons and a serrated
shoreline. Never couple the cut resolution to the mesh density: that coupling was the bug.

Nothing here builds a mesh — it produces masks, lattices, index arrays and heights. Turning
those into geometry is the consumer's, and stays in the adapter.
"""

import math
import os
from collections import namedtuple

import numpy

from . import bezier, reader


# Quads spanning fewer rows than this take the scalar scanline: below it the whole-array
# path's fixed cost (arange / sort / nonzero / clip) outweighs the per-row arithmetic it saves.
# Measured on the full city, where 471k quad fills split roughly two to one short-to-tall.
CLIP_SCANLINE_VECTOR_ROWS = 8


# How far terrain faces marked roadbed (§8 surface id 1) sink below their sampled height. The
# game's own clamp direction: its road job pulls ground under a carriageway to the MINIMUM of the
# road's surface band, below the asphalt top, while a consumer's linearly interpolated (and
# possibly stride-subsampled) grid crests through it.
#
# This is the FALLBACK, not the main clamp. It guesses a constant against a road surface it never
# looks at, and 0.15 m is smaller than the 0.3 m roof of the band it is guessing about — which is
# precisely how terrain came through a carriageway once a shallow band stopped carving. Where a
# clip band covers the cell there is a real lane height to reference and `clamp_shallow_bands`
# uses it instead, so this now only covers roadbed cells no band reaches: the flattening apron,
# where nothing better is available. TUNED.
ROADBED_SINK = 0.15

# Ground steeper than this (rise over run) is textured rock instead of grass — the gorge
# embankments. The game classifies grass/dirt/rock per pixel on the GPU by slope, height and
# noise, and §8 deliberately does not reproduce that; a plain slope threshold at ~40 degrees is
# the presentation-side stand-in. TUNED, and purely presentational.
ROCK_SLOPE = 0.84


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


def clip_span(mask, terrain_y, row, lo, hi, lane_y, ramp, bottom, top,
               refs, ref_value, floor, offset):
    """Mark one scanline span of a clip quad, and record what the span was marked against.

    The body both scanline paths share, so the two cannot drift apart on it —
    `tools/test_clip_cut.py` fuzzes them against each other precisely because they once did.

    `floor`, when given, receives `lane + bottom` for every sample inside the band: the height
    at which the game's own surviving terrain resumes under this strip. Writing it here rather
    than re-deriving it later is what keeps `clamp_shallow_bands` definitionally consistent
    with the band test — the clamp lowers ground to the floor of the very band that selected it,
    including its lateral tilt, with no second opinion about which station or which side.
    """
    if ramp is None:
        lane = lane_y
    else:
        y_left, dy, lx, lz, ax, az, inv_len2 = ramp
        px = numpy.arange(lo, hi + 1, dtype=numpy.float64) + offset
        s = numpy.clip(((px - lx) * ax + (row + offset - lz) * az) * inv_len2, 0.0, 1.0)
        lane = y_left + dy * s

    over = terrain_y[row, lo:hi + 1] - lane
    inside = (over > bottom) & (over < top)
    mask[row, lo:hi + 1] |= inside
    if refs is not None and ref_value >= 0:
        span = refs[row, lo:hi + 1]
        span[inside] = ref_value
    if floor is not None:
        span = floor[row, lo:hi + 1]
        # Lowest floor wins where strips overlap, so a cell claimed by two roads ends up under
        # both of them rather than under whichever was rasterised last. `casting="unsafe"` is
        # only the float64 lane arithmetic landing in the float32 raster, which is deliberate.
        numpy.minimum(span, lane + bottom, out=span, where=inside, casting="unsafe")


def fill_clip_quad_rows(mask, terrain_y, width, height, quad, lane_y, bottom, top,
                         refs, ref_value, offset, row_lo, row_hi, floor=None):
    """The even-odd scanline, one row at a time — the reference path `fill_clip_quad` splits
    from. Kept whole rather than folded into a branch so the two can be fuzzed against each
    other; see `tools/test_clip_cut.py`."""
    lane_y, ramp = lane_ramp(quad, lane_y)
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
            clip_span(mask, terrain_y, row, lo, hi, lane_y, ramp, bottom, top,
                       refs, ref_value, floor, offset)


def fill_clip_quad(mask, terrain_y, width, height, quad, lane_y, bottom, top,
                    refs=None, ref_value=-1, offset=0.5, floor=None):
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
    if row_hi - row_lo < CLIP_SCANLINE_VECTOR_ROWS:
        fill_clip_quad_rows(mask, terrain_y, width, height, quad, lane_y, bottom, top,
                             refs, ref_value, offset, row_lo, row_hi, floor)
        return
    lane_y, ramp = lane_ramp(quad, lane_y)

    # Edge crossings for EVERY row at once. Four Python comparisons and a `sort()` per row is
    # cheaper for the short quads that dominate by count, and much dearer for the tall ones, so
    # the two paths split on row count and `tools/test_clip_cut.py` fuzzes them against each
    # other. (The exact clip reverted on 2026-08-11 had a fast path that silently disagreed with
    # its slow one; that is the test this pairing owes.)
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

    ordered = numpy.sort(crossings, axis=0)          # inf pads sort to the end
    count = numpy.isfinite(ordered).sum(axis=0)

    for pair in (0, 2):                              # a quad crosses in 2 or 4 places
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
            row = int(rows[live[k]])
            lo, hi = int(los[k]), int(his[k])
            clip_span(mask, terrain_y, row, lo, hi, lane_y, ramp, bottom, top,
                       refs, ref_value, floor, offset)


def face_footprint_reduce(native, row0, col0, nfr, nfc, stride):
    """`(any, all)` of a native flag over each coarse face's real footprint.

    A grid vertex sits at a cell CENTRE (contract §3), so the quad between four vertices
    straddles the four cells they name and everything between — `(stride + 1)²` cells, not
    `stride²`. Reducing over that footprint is what makes the coarse answer mean something, and
    at stride 1 it collapses to exactly the four corner cells the harness has always used.

    Done as `(stride + 1)²` strided views rather than one fancy-indexed gather: stride is 4 on
    the largest map, so this is 25 cheap whole-array ops instead of a 26M-element index array.
    """
    base = native[row0:row0 + nfr * stride + 1, col0:col0 + nfc * stride + 1]
    any_ = numpy.zeros((nfr, nfc), dtype=bool)
    all_ = numpy.ones((nfr, nfc), dtype=bool)
    for i in range(stride + 1):
        for j in range(stride + 1):
            sub = base[i::stride, j::stride][:nfr, :nfc]
            if sub.shape != (nfr, nfc):          # a partial last block: leave it to `any`
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


def transition_fans(block_refined, block_transition, stride, lattice_w):
    """Triangle fans that join coarse blocks to refined ones without cracks.

    A transition block is a coarse block 4-adjacent to at least one refined block. Its face
    is emitted as a fan from the block's centre lattice point to an ordered perimeter loop:
    on a side shared with a REFINED block the perimeter includes every native lattice point
    of that edge (so the refined neighbour's true edge is part of this face — no T-junction,
    no crack, no pinning); on a side shared with a coarse/transition block only the two
    corners (that neighbour draws a straight segment there, and adding interior points would
    tear it). All indices are into the shared native lattice, flat (`row * lattice_w + col`).

    Returns `(loops, block_ids)`: `loops` a flat int array of fan-triangle loop indices
    (3 per triangle, CCW from above — same up-normal as the quad winding), `block_ids` one
    entry per triangle naming the (row-major) transition-block it belongs to, for per-face
    class lookup.
    """
    nfr, nfc = block_refined.shape
    half = stride // 2
    loops = []
    block_ids = []
    tr, tc = numpy.nonzero(block_transition)
    for bR, bC in zip(tr.tolist(), tc.tolist()):
        r0, c0 = bR * stride, bC * stride

        def edge_points(refined, points):
            # Each side contributes its starting corner plus, when the neighbour is refined,
            # the interior lattice points; the closing corner belongs to the next side.
            return points if refined else points[:1]

        west = 0 <= bC - 1 and block_refined[bR, bC - 1]
        south = bR + 1 < nfr and block_refined[bR + 1, bC]
        east = bC + 1 < nfc and block_refined[bR, bC + 1]
        north = 0 <= bR - 1 and block_refined[bR - 1, bC]

        perimeter = (
            edge_points(west, [(r0 + k, c0) for k in range(stride)]) +
            edge_points(south, [(r0 + stride, c0 + k) for k in range(stride)]) +
            edge_points(east, [(r0 + stride - k, c0 + stride) for k in range(stride)]) +
            edge_points(north, [(r0, c0 + stride - k) for k in range(stride)])
        )
        centre = (r0 + half) * lattice_w + (c0 + half)
        count = len(perimeter)
        for i in range(count):
            pr, pc = perimeter[i]
            qr, qc = perimeter[(i + 1) % count]
            loops.extend((centre, pr * lattice_w + pc, qr * lattice_w + qc))
            block_ids.append(bR * nfc + bC)

    return (numpy.asarray(loops, dtype=numpy.int64),
            numpy.asarray(block_ids, dtype=numpy.int64))


CLIP_REF_STATION_BITS = 11  # stations per strip cap at 1024 (< 2^11)

# Sub-samples per native cell per axis for the containment test, and the face-block edge the
# test is tiled over.
#
# A face is deleted only if EVERY one of its `CLIP_CONTAIN_SAMPLES`² samples is inside a clip
# band, so the test is only as exact as the sampling. Measured on the 2026-08-11 rip against an
# independent 0.29 m truth raster, see-through (terrain deleted where no prism covers it) over
# five 240 m windows:
#
#     K:            2        3        4        6       8       12      16
#     see-through   34-307   14-101   5-47     1-16    2-5     1-2     0.1-0.8   m² per window
#
# against 197-741 m² for the corner-AND rule this replaces. Raising K makes the test STRICTER,
# so it trades see-through for terrain kept under a road — never the other way round, and the
# terrain it keeps is under a SHALLOW band (the road hides it) because a deep one is re-cut at
# this same sampling further down.
#
# 8 because it is free: measured on the full city, `clip_grid` runs 78.4 s at 8 against 86.9 s
# at 6 (the whole-array scanline amortises the extra rows), and it more than halves the residual
# slivers at a junction — 12.3 -> 4.7 m² over a 180 m window. 12 halves them again but costs
# 115.5 s and 2.4x the sub-cut geometry (338k sub-quads against 143k), which is a real price for
# a sliver already below half a metre.
CLIP_CONTAIN_SAMPLES = 8
CLIP_CONTAIN_BLOCK = 128

# How far, in cells, `snap_hole_boundary` may push a hole-rim vertex OUT to a prism wall.
CLIP_SNAP_REACH_CELLS = 1.5

# A band this tall is a trench or a tunnel portal (`ElevationLimit * 3` — 12.3 m for roads,
# 6.3 m for pathways), not the everyday ±0.3 m shell over a surface road. The difference matters
# for reporting: terrain left inside a shallow band is under the road that hides it, terrain left
# inside a deep one stands in the cutting.
CLIP_DEEP_BAND_TOP = 3.0


def contain_faces(width, height, terrain_y, cell_cut, quad_source, samples, block):
    """Faces wholly inside the union of the DEEP clip bands — the deletion rule (contract §8c).

    The game never cuts terrain geometry: it draws the whole heightfield and discards per pixel
    against a stencil built from these prisms (`GenerateClipDataJob` ->
    `TerrainSurface.RenderClipAreas`). A mesh cut at 3.5 m can only approximate that boundary,
    and the invariant says which way to err: deleting beyond a prism is sky, deleting less
    leaves terrain under a road that already hides it. Requiring the WHOLE face to be inside
    honours the invariant directly.

    Only a DEEP band gets to delete at all, and that is the correction of 2026-08-11. This used
    to say `discarded ⊆ covered by road mesh` held "by construction", so a see-through gap
    beside a road could not happen. It does not hold at a node. Measured on the 2026-08-11 rip
    at the width transition `(4325.3, 1562.2)` / `(4321.4, 1542.0)`: the node clip fan runs out
    to the segment's own side curve, the road mesh reaches only part of that sector — short by
    2.2 m at the rail's start, widening to 4.4 m at the junction centre — and the ground between
    them was deleted with nothing over it. Two visible triangles, and they survived with the
    snap disabled, so they were never the snap (`docs/cs2-api-audit.md` §41.7 got that wrong).
    The producer's own `surface_index` calls that ground roadbed (1), i.e. the game paints it as
    road surface, so keeping it is right whether the game paves it with mesh or with paint —
    and keeping it cannot show sky, which deleting it demonstrably can.

    A shallow band therefore flattens and paints but never carves; a trench or portal band
    (`top > CLIP_DEEP_BAND_TOP`) still carves, because that is the one the game really needs
    stencilled — without it every tunnel mouth is walled off. Measured across six windows this
    takes see-through under a network strip to 0.0 m² and costs the terrain faces under a road
    that the road hides.

    What this replaces, and why: the rule here used to AND the face's four CORNERS against the
    union of every prism. Four corners covered by four DIFFERENT prisms passed the test while
    the face's own interior escaped them all — which is exactly the geometry of a junction, and
    was the mechanism behind the residual junction gaps. Measured over the two junctions
    reported on 2026-08-11, corner-AND deleted 398 m² and 197 m² of terrain that no prism
    covers; sampling the face's interior takes both to a few m² of sub-sample sliver.

    Tiled over `block`² faces so the sub-sample raster stays small: only blocks holding a cut
    cell are visited at all, which on a full city is 7.5% of the map. `quad_source(r0, c0, r1,
    c1)` yields `(quad, lane_y, bottom, top)` for every station quad whose face-space bounds
    touch the block, in NATIVE cell space; this routine scales them into the block's own frame.

    Returns `(contain, band_count, deep_touch, sub_bands)` — the (height − 1, width − 1) deletion mask; the
    count of in-band sub-samples PER FACE, which is the true clipped area in units of
    `(cell / samples)²` and, being per-face, can be windowed alongside the mask so the
    containment ratio in `build_terrain`'s report means the same thing for a region import as
    for a whole city; and which faces a DEEP band reaches without containing, the residual
    `build_terrain` reports (see `CLIP_DEEP_BAND_TOP`); and, for each of those faces, its own
    sample block, so the caller can cut the face at the sampling rather than choose for it.
    """
    face_h, face_w = height - 1, width - 1
    contain = numpy.zeros((face_h, face_w), dtype=bool)
    band_count = numpy.zeros((face_h, face_w), dtype=numpy.uint8)  # <= samples², 6² = 36
    deep_touch = numpy.zeros((face_h, face_w), dtype=bool)
    sub_bands = {}                       # (face row, face col) -> (samples, samples) bool

    # Only blocks holding a cut cell are visited. A face wholly inside a band has its four
    # CORNERS inside it too, and those corners are cell centres (contract §3) — which is what
    # `cell_cut` records. The converse does not hold, so this skips work, never a deletion; and
    # where the sampling and the corners disagree the face is kept, which is the safe side.
    cut_face = cell_cut[:-1, :-1] | cell_cut[:-1, 1:] | cell_cut[1:, :-1] | cell_cut[1:, 1:]
    if not cut_face.any():
        return contain, band_count, deep_touch, sub_bands

    for r0 in range(0, face_h, block):
        r1 = min(r0 + block, face_h)
        for c0 in range(0, face_w, block):
            c1 = min(c0 + block, face_w)
            if not cut_face[r0:r1, c0:c1].any():
                continue
            nfr, nfc = r1 - r0, c1 - c0
            sh, sw = nfr * samples, nfc * samples

            # Sample (i, j) sits at cell-space (c0 + 0.5 + (j + 0.5) / K, r0 + 0.5 + (i + 0.5)
            # / K) — the interior of face (r0 + i // K, c0 + j // K). Terrain there is the
            # bilinear interpolation the renderer already draws between the cell centres, so
            # the test reads the same surface the eye sees.
            gc = numpy.clip(c0 + (numpy.arange(sw) + 0.5) / samples, 0, width - 1.001)
            gr = numpy.clip(r0 + (numpy.arange(sh) + 0.5) / samples, 0, height - 1.001)
            ci = gc.astype(numpy.int64); fu = gc - ci
            ri = gr.astype(numpy.int64); fv = gr - ri
            top_row = (terrain_y[numpy.ix_(ri, ci)] * (1.0 - fu)[None, :]
                       + terrain_y[numpy.ix_(ri, ci + 1)] * fu[None, :])
            bot_row = (terrain_y[numpy.ix_(ri + 1, ci)] * (1.0 - fu)[None, :]
                       + terrain_y[numpy.ix_(ri + 1, ci + 1)] * fu[None, :])
            fine_y = top_row * (1.0 - fv)[:, None] + bot_row * fv[:, None]

            band = numpy.zeros((sh, sw), dtype=bool)
            deep = numpy.zeros((sh, sw), dtype=bool)
            origin = (c0 + 0.5, r0 + 0.5)
            for quad, lane_y, bottom, top in quad_source(r0, c0, r1, c1):
                local = [((px - origin[0]) * samples, (py - origin[1]) * samples)
                         for px, py in quad]
                fill_clip_quad(band, fine_y, sw, sh, local, lane_y, bottom, top)
                if top > CLIP_DEEP_BAND_TOP:
                    fill_clip_quad(deep, fine_y, sw, sh, local, lane_y, bottom, top)

            per_face = band.reshape(nfr, samples, nfc, samples)
            whole = per_face.all(axis=(1, 3))
            touched = deep.reshape(nfr, samples, nfc, samples).any(axis=(1, 3))
            contain[r0:r1, c0:c1] = whole
            band_count[r0:r1, c0:c1] = per_face.sum(axis=(1, 3), dtype=numpy.uint16)
            deep_touch[r0:r1, c0:c1] = touched

            # A face a DEEP band reaches but does not contain cannot be resolved by keeping or
            # deleting it whole: a trench narrower than the face holds no whole face, so nothing
            # is deleted along it and the face is left lying across the cutting. Keep its own
            # sample block so `build_terrain` can cut it at the sampling instead — the samples
            # are already computed, and their centres are exactly the sub-face centres.
            for i, j in zip(*numpy.nonzero(touched & ~whole)):
                sub_bands[(r0 + int(i), c0 + int(j))] = per_face[i, :, j, :].copy()

    return contain, band_count, deep_touch, sub_bands


def clip_grid(bundle_dir, width, height, heights, cell, origin_x, origin_z,
               elevation_scale, elevation_offset, floor_override=None):
    """Rasterise `clipvolumes.jsonl` onto the heightmap grid — contract §8c.

    The volumes are what the producer intersects against the heightmap; a consumer meshing at
    its own resolution does the intersection itself. The layer is mandatory (contract §8c): a
    bundle with terrain but no clip volumes would silently wall off every tunnel, so its
    absence is an error, not a fallback.

    `floor_override`, when not None, replaces every strip's `bottom` — a diagnostic knob
    (`--clip-floor`) for previewing a producer band-floor change against an existing bundle.

    Returns `(mask, contain, band_count, deep_touch, sub_bands, refs, strip_geo)`: the
    (height, width) bool cut mask over CELL centres; the (height − 1, width − 1) bool deletion
    mask from `contain_faces`; its per-face count of in-band sub-samples, which is the true
    clipped area in `(cell / CLIP_CONTAIN_SAMPLES)²` units; which faces a deep band reaches
    without containing, and those faces' own sample blocks; an int32 raster of packed (strip <<
    CLIP_REF_STATION_BITS | station) references for every cut cell (−1 where uncut or cut only
    by an `area` record, which has no curves to snap to); and `strip_geo[strip] = (left_pts,
    right_pts, lane, bottom, top, carves)` — the evaluated boundary polylines the hole-boundary
    snap projects onto, and whether that strip is allowed to DELETE terrain (see
    `CLIP_DEEP_BAND_TOP`). Every strip still contributes to `mask` and `refs`, because those
    drive the refine corridor, not the deletion.

    The cell raster and the deletion mask answer different questions and both are needed.
    "Which cells does the clip volume cover?" decides the refine corridor and gives the
    boundary snap its reference curve; "is this face WHOLLY inside the volume?" decides whether
    the face survives. The strips are walked twice: once here at cell centres, and once per
    face-block inside `contain_faces`, which re-evaluates the quads from `strip_geo` rather
    than holding a quarter of a million of them in memory.
    """
    path = reader.layer_path(bundle_dir, "clipVolumes")
    if not os.path.isfile(path):
        raise RuntimeError(
            "CONTRACT VIOLATION: terrain layer present but no clipvolumes.jsonl — "
            "the producer always writes the §8c clip layer; re-rip the bundle")

    strips = [record for record in reader.read_jsonl(path)]
    refs = numpy.full((height, width), -1, dtype=numpy.int32)
    strip_geo = []
    area_geo = []
    if not strips:
        return (numpy.zeros((height, width), dtype=bool),
                numpy.zeros((height - 1, width - 1), dtype=bool),
                numpy.zeros((height - 1, width - 1), dtype=numpy.uint8),
                numpy.zeros((height - 1, width - 1), dtype=bool), {}, refs, strip_geo,
                numpy.full((height, width), numpy.inf, dtype=numpy.float32))

    terrain_y = (numpy.asarray(heights, dtype=numpy.float64).reshape(height, width)
                 * elevation_scale + elevation_offset)
    mask = numpy.zeros((height, width), dtype=bool)

    # Where the game's terrain resumes under a SHALLOW band, per cell — +inf where no shallow
    # band covers it. `clamp_shallow_bands` reads this and nothing else; a deep band writes
    # nothing here because it is carved instead (`contain_faces`). float32 deliberately: this
    # is one full-map raster (67 MiB at 4096², against 134 at float64) and it holds a height
    # around 500 m, where float32 still resolves better than a ten-thousandth of a millimetre.
    band_floor = numpy.full((height, width), numpy.inf, dtype=numpy.float32)

    def fill(quad, lane_y, bottom, top, ref_value=-1, floor=None):
        fill_clip_quad(mask, terrain_y, width, height, quad, lane_y, bottom, top,
                        refs=(refs if ref_value >= 0 else None), ref_value=ref_value,
                        floor=floor)

    def to_cell(points):
        """World positions to cell space, cell (row, col) centred on (col + .5, row + .5)."""
        return numpy.stack(((points[:, 0] - origin_x) / cell + 0.5,
                            (origin_z - points[:, 2]) / cell + 0.5), axis=-1)

    unbounded = float("inf")
    for strip in strips:
        bottom = float(strip["bottom"]) if floor_override is None else float(floor_override)
        top = float(strip["top"]) if strip.get("top") is not None else unbounded

        if strip.get("kind") == "area":
            # An `area` record carries its own polygon since 2026-08-09 (§8c) — the game never
            # paints clip surfaces, so areas.jsonl stopped carrying them (§36) and the old join
            # here read geometry that no longer exists.
            nodes, triangles = strip.get("nodes") or [], strip.get("triangles") or []
            for tri in triangles:
                if any(not 0 <= i < len(nodes) for i in tri):
                    continue
                pts = numpy.asarray([nodes[i] for i in tri], dtype=numpy.float64)
                cells = to_cell(pts)
                # A triangle reaches the scanline as a quad with one doubled corner, and its
                # lane reference is the corner mean — both as the producer has them. No ref:
                # an area cut has no boundary curves for the snap to project onto.
                fill([tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
                     float(pts[:, 1].mean()), bottom, top)
                area_geo.append((cells, float(pts[:, 1].mean()), bottom, top))
            continue

        left, right = strip.get("left"), strip.get("right")
        if not left or not right:
            continue

        # One station per cell of arc keeps the fill within half a cell of the true curve. The
        # control polygon bounds the arc from above, so this errs toward more stations.
        polygon = numpy.asarray(left, dtype=numpy.float64)
        span = float(numpy.linalg.norm(numpy.diff(polygon, axis=0), axis=1).sum())
        steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))

        t = numpy.linspace(0.0, 1.0, steps + 1)
        left_pts = bezier.evaluate(left, t)
        right_pts = bezier.evaluate(right, t)
        left_cells = to_cell(left_pts)
        right_cells = to_cell(right_pts)
        left_y, right_y = left_pts[:, 1], right_pts[:, 1]
        lane = (left_y + right_y) * 0.5
        carves = top > CLIP_DEEP_BAND_TOP

        strip_index = len(strip_geo)
        strip_geo.append((left_pts, right_pts, lane, bottom, top, carves))
        for i in range(1, steps + 1):
            # The band's two sides, not their mean: see `lane_ramp`. A deep band writes no
            # floor — it is carved, and a carved cell has no ground left to clamp.
            fill([tuple(left_cells[i - 1]), tuple(right_cells[i - 1]),
                  tuple(right_cells[i]), tuple(left_cells[i])],
                 (float((left_y[i - 1] + left_y[i]) * 0.5),
                  float((right_y[i - 1] + right_y[i]) * 0.5)), bottom, top,
                 ref_value=(strip_index << CLIP_REF_STATION_BITS) | i,
                 floor=(None if carves else band_floor))

    # -- the deletion mask ----------------------------------------------------------------
    # Each strip in cell space with PER-STATION bounds, so a face block replays only the
    # stations that reach it. Without the per-station filter a long strip binned to a block
    # re-scans all of its quads there, and the containment pass costs ~3x what it needs to.
    strip_cells = []
    for left_pts, right_pts, lane, bottom, top, carves in strip_geo:
        lc, rc = to_cell(left_pts), to_cell(right_pts)
        quad_r = numpy.stack((lc[:-1, 1], rc[:-1, 1], lc[1:, 1], rc[1:, 1]), axis=-1)
        quad_c = numpy.stack((lc[:-1, 0], rc[:-1, 0], lc[1:, 0], rc[1:, 0]), axis=-1)
        strip_cells.append((lc, rc, (left_pts[:, 1], right_pts[:, 1]), bottom, top,
                            quad_r.min(axis=1), quad_r.max(axis=1),
                            quad_c.min(axis=1), quad_c.max(axis=1), carves))
    boxes = [(rmin.min(), cmin.min(), rmax.max(), cmax.max())
             for _, _, _, _, _, rmin, rmax, cmin, cmax, _ in strip_cells]
    boxes += [(c[:, 1].min(), c[:, 0].min(), c[:, 1].max(), c[:, 0].max())
              for c, _, _, _ in area_geo]

    # Bin each strip into the face blocks it reaches, once. Face (r, c) spans cell space
    # [c + 0.5, c + 1.5] x [r + 0.5, r + 1.5], so block (br, bc) covers cell space
    # [bc*B + 0.5, (bc+1)*B + 0.5] x same in rows; one cell of slack keeps a strip that only
    # clips a block edge in play.
    block = CLIP_CONTAIN_BLOCK
    bins = {}
    for index, (top_, left_, bottom_, right_) in enumerate(boxes):
        # A shallow band never deletes, so it is never binned: it costs nothing in the
        # containment pass and cannot open a hole. See `contain_faces` for why.
        if index < len(strip_cells) and not strip_cells[index][-1]:
            continue
        br0 = max(0, int((top_ - 1.5) // block))
        br1 = min((height - 2) // block, int((bottom_ + 0.5) // block))
        bc0 = max(0, int((left_ - 1.5) // block))
        bc1 = min((width - 2) // block, int((right_ + 0.5) // block))
        for br in range(br0, br1 + 1):
            for bc in range(bc0, bc1 + 1):
                bins.setdefault((br, bc), []).append(index)

    def quad_source(r0, c0, r1, c1):
        """Every station quad whose strip reaches this face block, in native cell space."""
        for index in bins.get((r0 // block, c0 // block), ()):
            if index < len(strip_cells):
                lc, rc, (left_y, right_y), low, high, rmin, rmax, cmin, cmax, _ = \
                    strip_cells[index]
                live = numpy.nonzero((rmax >= r0 - 0.5) & (rmin <= r1 + 1.5)
                                     & (cmax >= c0 - 0.5) & (cmin <= c1 + 1.5))[0]
                for station in live:
                    i = int(station) + 1
                    yield ([tuple(lc[i - 1]), tuple(rc[i - 1]), tuple(rc[i]), tuple(lc[i])],
                           (float((left_y[i - 1] + left_y[i]) * 0.5),
                            float((right_y[i - 1] + right_y[i]) * 0.5)), low, high)
            else:
                cells, lane_y, low, high = area_geo[index - len(strip_cells)]
                yield ([tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
                       lane_y, low, high)

    contain, band_count, deep_touch, sub_bands = contain_faces(
        width, height, terrain_y, mask, quad_source, CLIP_CONTAIN_SAMPLES,
        CLIP_CONTAIN_BLOCK)
    return mask, contain, band_count, deep_touch, sub_bands, refs, strip_geo, band_floor


def snap_hole_boundary(verts, vid, used_grid, cut_face_grid, refs_w, strip_geo, ground_y,
                        origin_x, origin_z, row0, col0, cell):
    """Move every cut-hole boundary vertex onto the clip prism's own surface.

    The game clips terrain per pixel against smooth swept prisms; this grid deletes whole
    3.5 m cells, so the hole's rim lands within a cell of the prism wall on whichever side the
    deletion rule errs. Snapping closes the difference exactly, in BOTH directions:

      * a rim vertex laterally outside the strip moves IN, onto the nearest point of the
        strip's side polyline (the prism wall), with its height resampled from the ground
        there;
      * a rim vertex over the strip and INSIDE the band is one the game discards, so it is
        pushed OUT to that same wall — within `CLIP_SNAP_REACH_CELLS`, which is what
        distinguishes a vertex bounded by the band's wall from one bounded by its roof or
        floor in the middle of a wide prism;
      * a rim vertex over the strip but above the band is clamped in height only: it drops to
        `lane + top`, the portal-roof iso-edge.

    **The snap moves a vertex sideways, never upward.** Every height it assigns is a minimum
    against the height the vertex already had. That invariant is what makes the lateral move
    safe next to a vertical heightmap step, and its absence was the "deep nested road" defect
    of 2026-08-12 — see the `onto` branch for the measurement. Band heights are read at the
    vertex's own LATERAL position between the two curves rather than at their mean, because the
    curves tilt across the road's width (`lane_ramp` documents that in full).

    Kept faces then stretch to the exact clip boundary, the way the game's surviving pixels end
    at it. Verts cut only by `area` records (ref −1) stay — an authored clip surface has no
    curves to project onto.

    The outward push is what the containment deletion rule needs and the old over-deleting rule
    did not: with `contain_faces` the hole is INSIDE the prism, so its rim sits in the band and
    the height clamp alone leaves it there. In a shallow surface band that is invisible (the
    road covers it), but in a trench or portal band, whose roof is `ElevationLimit * 3`, it is a
    3.5 m square of hillside standing in the cutting — the "square terrain overlapping the
    cutout walls" reported on 2026-08-11.

    Where several strips overlap — every junction, every pair of close-run tracks — a boundary
    vertex's neighbourhood names SEVERAL strips, and which one it is projected onto decides
    where it lands. Taking the first non-negative reference (the rule this replaced) picked
    arbitrarily among them: measured on the 2026-08-11 rip, 46% of boundary vertices at a
    double-track run and 50% at a road junction are claimed by more than one strip, and the
    arbitrary pick lands a median 2.03 m from where the nearest strip would put them (p90
    4.43 m, max 8.00 m; 23% displaced more than a whole cell at the junction). A vertex dragged
    onto some other road's prism wall tears the terrain open — the residual junction gaps that
    survived the face-centre cut. So EVERY candidate is evaluated and the nearest target wins:
    the same "nearest prism wall" rule this already used within a strip, extended across them.

    Candidates are ranked by distance to the strip's own wall, not by how far the vertex would
    move — a vertex over a prism would otherwise score a free zero-distance win against the wall
    it actually belongs to, which suppresses the lateral snap entirely and leaves the raw cell
    staircase behind (seen, measured, rejected).

    Everything is in the window lattice: `used_grid`/`cut_face_grid` are (lat_h, lat_w),
    `refs_w` the windowed reference raster, `verts` the built vertex array `vid` indexes.
    Returns `(moved, unreferenced, pushed)` — vertices snapped, boundary vertices that found no
    strip reference in their 3x3 neighbourhood and were therefore left where they were, and how
    many of the moves were outward pushes out of a band.

    NOTE on that second number: it counts vertices with NO candidate, and it is 0 on a real
    city. It says nothing about whether the RIGHT strip was chosen among several, which is what
    the paragraph above is about — reading a zero there as "the snap is fine" is the mistake
    this rewrite corrects.
    """
    lat_h, lat_w = used_grid.shape
    cut_corner = numpy.zeros((lat_h, lat_w), dtype=bool)
    fr, fc = numpy.nonzero(cut_face_grid)
    for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1)):
        cut_corner[fr + dr, fc + dc] = True
    br, bc = numpy.nonzero(cut_corner & used_grid)
    if not len(br):
        return 0, 0, 0

    # Every distinct reference in each boundary vertex's 3×3 neighbourhood becomes a candidate.
    # One row per (vertex, candidate) pair, so the whole set is scored in one pass and the
    # per-strip loop below still runs once per strip rather than once per vertex.
    neighbourhood = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            rr = numpy.clip(br + dr, 0, lat_h - 1)
            cc = numpy.clip(bc + dc, 0, lat_w - 1)
            neighbourhood.append(refs_w[rr, cc].astype(numpy.int64))
    candidates = numpy.stack(neighbourhood, axis=1)                  # (N, 9)
    live = candidates >= 0
    unreferenced = int((~live.any(axis=1)).sum())
    if not live.any():
        return 0, unreferenced, 0

    vertex_of = numpy.repeat(numpy.arange(len(br), dtype=numpy.int64), 9)[live.reshape(-1)]
    ref = candidates.reshape(-1)[live.reshape(-1)]

    vertex_x = origin_x + (col0 + bc).astype(numpy.float64) * cell
    vertex_z = origin_z - (row0 + br).astype(numpy.float64) * cell
    ids = vid[br * lat_w + bc]
    vertex_y = verts[ids, 2].copy()

    # Expanded to one entry per (vertex, candidate); collapsed back after scoring.
    x = vertex_x[vertex_of]
    z = vertex_z[vertex_of]
    y = vertex_y[vertex_of]

    station_mask = (1 << CLIP_REF_STATION_BITS) - 1
    strip_of = (ref >> CLIP_REF_STATION_BITS).astype(numpy.int64)
    station_of = (ref & station_mask).astype(numpy.int64)

    def bilinear_ground(px, pz):
        gc = numpy.clip((px - origin_x) / cell - col0, 0.0, lat_w - 1.001)
        gr = numpy.clip((origin_z - pz) / cell - row0, 0.0, lat_h - 1.001)
        c0 = gc.astype(numpy.int64)
        r0g = gr.astype(numpy.int64)
        fu = gc - c0
        fv = gr - r0g
        g = ground_y
        return ((g[r0g, c0] * (1 - fu) + g[r0g, c0 + 1] * fu) * (1 - fv)
                + (g[r0g + 1, c0] * (1 - fu) + g[r0g + 1, c0 + 1] * fu) * fv)

    # Scored in place, one entry per (vertex, candidate strip). `wall_distance` is the score:
    # how far this vertex is from that strip's nearer side polyline — the prism wall — which is
    # what "the nearest prism" means. Ranking by how far the vertex would MOVE instead is a
    # trap: a vertex standing over a strip is only clamped in height, so it scores a zero move
    # and wins every contest, which suppresses the lateral snap that closes the sawtooth and
    # leaves the raw cell staircase behind (seen, measured, rejected).
    # How far a vertex may be pushed out to a prism wall. A rim vertex is a corner of a deleted
    # face and of a kept one, and a kept face has a sample outside the band, so a vertex whose
    # band boundary is a WALL is within a face of it; anything further out is bounded by a roof
    # or a floor instead and stays put. One and a half cells is that face plus slack.
    reach = cell * CLIP_SNAP_REACH_CELLS
    pushed_flag = numpy.zeros(len(x), dtype=bool)
    target_x = x.copy()
    target_z = z.copy()
    target_y = y.copy()
    wall_distance = numpy.full(len(x), numpy.inf)

    order = numpy.argsort(strip_of, kind="stable")
    bounds = numpy.searchsorted(strip_of[order], numpy.arange(len(strip_geo) + 1))
    for s in numpy.unique(strip_of):
        sel = order[bounds[s]:bounds[s + 1]]
        left_pts, right_pts, lane, bottom, top, carves = strip_geo[s]
        # A rim vertex belongs to the volume that made the hole. A shallow band deletes nothing
        # now, so its wall is not a boundary the terrain ends at, and letting it win a contest
        # would drag a deep hole's rim sideways onto a surface road.
        if not carves:
            continue
        i = numpy.clip(station_of[sel], 1, len(lane) - 1)
        l0 = left_pts[i - 1]
        l1 = left_pts[i]
        r0 = right_pts[i - 1]
        r1 = right_pts[i]
        # The band's height at each SIDE, kept apart rather than averaged. The two curves tilt
        # across the road's width and the snap is a lateral operation, so it interpolates
        # between them once the wall distances are known — `lane_ramp` makes the same
        # correction to the rasteriser, and testing a vertex against a horizontal slab at the
        # mean is what put a whole carriageway edge outside its own band there.
        lane_left = (l0[:, 1] + l1[:, 1]) * 0.5
        lane_right = (r0[:, 1] + r1[:, 1]) * 0.5
        px, pz, py = x[sel], z[sel], y[sel]

        def closest(a, b):
            ax, az = a[:, 0], a[:, 2]
            dx, dz = b[:, 0] - ax, b[:, 2] - az
            denom = numpy.maximum(dx * dx + dz * dz, 1e-9)
            t = numpy.clip(((px - ax) * dx + (pz - az) * dz) / denom, 0.0, 1.0)
            cxp = ax + t * dx
            czp = az + t * dz
            return cxp, czp, numpy.hypot(px - cxp, pz - czp)

        lxp, lzp, ld = closest(l0, l1)
        rxp, rzp, rd = closest(r0, r1)
        side_l = (l1[:, 0] - l0[:, 0]) * (pz - l0[:, 2]) - (l1[:, 2] - l0[:, 2]) * (px - l0[:, 0])
        side_r = (r1[:, 0] - r0[:, 0]) * (pz - r0[:, 2]) - (r1[:, 2] - r0[:, 2]) * (px - r0[:, 0])
        between = numpy.sign(side_l) != numpy.sign(side_r)
        # The band's own height at THIS vertex: its lateral position follows straight from the
        # two wall distances, so the band tracks the road's crossfall the way the prism does.
        lane_y = lane_left + (lane_right - lane_left) * (ld / numpy.maximum(ld + rd, 1e-9))

        new_x, new_z, new_y = px.copy(), pz.copy(), py.copy()
        finite_top = numpy.isfinite(numpy.full_like(py, top))
        # A vertex standing OVER the strip and INSIDE the band is one the game discards per
        # pixel, so leaving it is the 3.5 m square of hillside that stands in a trench and
        # clips through the road. The nearest way out of the volume is the prism wall, and the
        # rim is by construction within a face of it — a kept face has a sample outside the
        # band — so the reach gate below only ever declines a vertex whose band boundary is the
        # band's ROOF or FLOOR rather than its wall (the middle of a wide prism), which is
        # exactly the vertex that must not be dragged sideways across a road.
        in_band = between & (py > lane_y + bottom) & (~finite_top | (py < lane_y + top))
        push = in_band & (numpy.minimum(ld, rd) <= reach)
        # Over the strip, above the band: drop to the roof, the portal-mouth iso-edge. There is
        # deliberately no matching raise from below the floor — terrain under a band's floor is
        # terrain the game keeps and the road hides, and lifting it to `lane + bottom` would
        # break the never-upward invariant for nothing.
        hold = between & ~push
        roof = hold & finite_top & (py > lane_y + top)
        new_y[roof] = (lane_y + top)[roof]
        # Onto the nearer side polyline, height from the ground there. Vertices outside the
        # strip come IN; in-band rim vertices go OUT. Both land the kept terrain's edge on the
        # prism wall, which is where the game's surviving pixels end.
        onto = (~between) | push
        use_left = onto & (ld <= rd)
        use_right = onto & ~use_left
        new_x[use_left] = lxp[use_left]
        new_z[use_left] = lzp[use_left]
        new_x[use_right] = rxp[use_right]
        new_z[use_right] = rzp[use_right]
        if onto.any():
            # Resample the ground at the destination, but never let the move LIFT the vertex.
            #
            # This is the vertical-step fix, and the reason it has to be a minimum rather than a
            # smarter interpolation. `bilinear_ground` is only meaningful where the heightmap is
            # smooth; a trench wall is not. At `(1031.6, 6525.0)` the game's own heightmap rises
            # 12.3 m — `ElevationLimit * 3` exactly — across ONE 3.5 m cell, so the interpolant
            # between those two texels sweeps the entire 12.3 m and a destination a metre either
            # way of the wall reads anywhere in it. Instrumented on that trench: a rim vertex
            # sitting on the trench FLOOR at 528.23 was pushed 3.05 m sideways and resampled to
            # 533.58 — lifted 5.35 m onto the wall, over a carriageway at 528.3 — and a second
            # went up 5.91 m. The sub-patch bilinear over those moved corners then dragged the
            # surface to 530.3, standing 1.96 m proud across 3.75 m of road: the "deep nested
            # road" report. Disabling the snap outright collapsed it to +0.09 m, which is how
            # the branch was identified rather than guessed at.
            #
            # Taking the minimum keeps every case the resample was for — a vertex pulled IN from
            # outside is coming DOWN into the cutting, so the resampled height wins and the rim
            # still follows the wall — while a push that would climb the wall simply keeps the
            # height it already had, which is by construction a real height of the ground it
            # came from. Low is always safe here: the road hides it.
            gy = numpy.minimum(bilinear_ground(new_x[onto], new_z[onto]), py[onto])
            if numpy.isfinite(top):
                gy = numpy.minimum(gy, (lane_y + top)[onto])
            new_y[onto] = gy

        target_x[sel] = new_x
        target_z[sel] = new_z
        target_y[sel] = new_y
        wall_distance[sel] = numpy.minimum(ld, rd)
        pushed_flag[sel] = push

    # One winner per vertex: the strip whose wall it stands nearest to.
    pick = numpy.lexsort((wall_distance, vertex_of))
    winner = pick[numpy.concatenate(([True], vertex_of[pick][1:] != vertex_of[pick][:-1]))]

    chosen_vertex = vertex_of[winner]
    new_x, new_z, new_y = target_x[winner], target_z[winner], target_y[winner]
    px, pz, py = vertex_x[chosen_vertex], vertex_z[chosen_vertex], vertex_y[chosen_vertex]
    changed = (new_x != px) | (new_z != pz) | (new_y != py)
    idx = ids[chosen_vertex][changed]
    verts[idx, 0] = new_x[changed]
    verts[idx, 1] = new_z[changed]
    verts[idx, 2] = new_y[changed]
    return int(changed.sum()), unreferenced, int((pushed_flag[winner] & changed).sum())


def clamp_shallow_bands(ground_y, band_floor_w):
    """Drop ground standing inside a SHALLOW clip band to that band's own floor.

    The counterpart to `contain_faces`' refusal to carve a shallow band, and the reason that
    refusal does not put terrain through the road. The game discards terrain per pixel inside
    `(lane + bottom, lane + top)` — for a surface road that shell is only `[-0.5, +0.3]` m, but
    the top half of it is *above* the carriageway, so terrain kept there wins the depth test
    against a road running nearly parallel to it. Measured on the 2026-08-12 bundle at
    `(-435.1, 6859.4)`: 1.9% of the road area with ground over it, median 0.063 m, max
    **0.303 m** — the band's own roof, to the centimetre, which is what identifies the cause.

    Deleting the face instead is the move this repo has already rejected twice (see
    `contain_faces`): at a node the clip fan reaches past the road mesh, so a deleted face
    there is sky. Lowering it cannot be: the ground stays, still painted roadbed, and merely
    sits where the game's own surviving pixels resume — the band floor. That is the rule
    "never loosen the deletion rule to close an intrusion, move the surface instead".

    `lane + bottom` is the game's number and needs no tuned constant of its own, which is the
    argument for it over the blind `_ROADBED_SINK`: that sink guesses 0.15 m against a road
    surface it never looks at, and 0.15 < 0.303 is exactly why it failed. The sink still covers
    roadbed cells no band reaches (the flattening apron), where there is no lane height to
    reference.

    `band_floor_w` is the windowed floor raster `clip_span` wrote while it rasterised the
    bands: `lane + bottom` for every cell a shallow band selected, `+inf` everywhere else, with
    the lateral tilt of `lane_ramp` already in it and the LOWEST floor kept where two strips
    overlap. Taking it from there rather than re-deriving it from a station reference is the
    point — the clamp then cannot disagree with the test that chose the cell, which a second
    derivation (via last-writer-wins refs, over a mean-height lane) demonstrably did.

    Mutates `ground_y` in place. Returns `(vertices lowered, largest drop in metres)`.
    """
    drop = ground_y - band_floor_w       # +inf floor (no shallow band) never selects
    hit = drop > 0.0
    if not hit.any():
        return 0, 0.0
    ground_y[hit] = band_floor_w[hit]
    return int(hit.sum()), float(drop[hit].max())


GROUND_FALLBACK_TILING = 8.0


def ground_tiling(block, ground):
    """`([near, mid, far] metres per repeat, [midStart, midEnd, farStart, farEnd] camera m)`
    for one ground, from the shipped `rasters.terrainTiling`, or None when it is absent.

    The game does not tile a ground at one scale: it blends THREE by camera distance
    (`_Terrain*TextureTiling` / `_Terrain*TextureTilingBlend`, driven in
    `Game.Rendering.TerrainMaterialSystem.UpdateGrassTiling`), which is its own anti-tiling.
    The producer has shipped the authored numbers since 095322a and nothing read them (audit
    P5, producer half done, consumer half never started) — the harness UV'd the whole terrain
    at a hardcoded 8 m per repeat, tighter than the game's grass near tiling (14.34 m) and
    8.5x tighter than rock (68.27 m), which is the repeating grid.

    The extras have no tiling block of their own and take the GRASS layer's: the bundle's own
    semantics string says so (`_TerrainExtraTextureTiling` is bound from `m_TerrainGrassTiling`).
    """
    layers = (block or {}).get("layers")
    if not isinstance(layers, dict):
        return None
    entry = layers.get("grass" if str(ground).startswith("extra") else ground)
    if not isinstance(entry, dict):
        return None
    repeats = entry.get("metresPerRepeat")
    if not isinstance(repeats, list) or len(repeats) != 3 or not all(r > 0 for r in repeats):
        return None
    blend = entry.get("lodBlend")
    if not isinstance(blend, list) or len(blend) != 4:
        return None
    return [float(r) for r in repeats], [float(b) for b in blend]


# -- the lattice, and the faces built on it -----------------------------------------------------

# What `build_lattice` hands back. Geometry first (`verts`/`loops`/`sizes`/`face_class` are the
# mesh), then the rasters a caller needs for UVs and materials, then counts a caller may report.
# `face_class` is 0 grass / 1 roadbed / 2 rock — presentation, not contract.
LatticeMesh = namedtuple("LatticeMesh", (
    "lat_h lat_w nfr nfc "
    "verts loops sizes face_class "
    "native_y ground_y wet has_level level_value roadbed cut contain band_count "
    "block_refined coarse_loops cbr cbc cell_rr cell_cc refined_loops_all fan_loops "
    "fan_faces refined_faces cut_faces roadbed_faces rock_faces "
    "snapped unreferenced pushed sub_parents sub_dropped sub_kept "
    "band_clamped band_clamp_drop stray_cells"))


def build_lattice(heights, width, height, surface_ids, levels,
                  row0, col0, row1, col1, stride, cell, origin_x, origin_z,
                  elevation_scale, elevation_offset,
                  cut_grid, contain_raster, band_count_raster, deep_raster,
                  cut_refs, band_floor_raster, sub_bands, strip_geo,
                  roadbed_sink=None):
    """Build the §8 ground mesh for one window: vertices, faces, and what each face is.

    This is where every other function in this module meets. It returns a `LatticeMesh`, or
    `None` when the window is smaller than a single face.

    **One lattice, one vertex set, welded by construction.** The vertex grid IS the native texel
    grid (§3 puts a vertex at each cell centre), and all three face families index it: coarse
    block quads away from anything interesting, native cells inside the refine corridors, and
    `transition_fans` joining the two. Nothing duplicates a position, no edge is pinned to a
    subsample, and normals are shared across every seam.

    **Never couple the cut resolution to the mesh density.** That coupling was a real bug — a
    stride patchwork that produced spikes, chevrons and a serrated shoreline. The game keeps mesh
    density and per-pixel decisions independent, and the refine CORRIDOR is how this grid does
    the same: every decision boundary (clip volumes, roadbed, shoreline) gets native cells, the
    rest of the map stays coarse, and the two are joined without T-junctions.

    **A face is deleted only where a prism CONTAINS it**, and two tempting rules are wrong. A
    face deleted on its own CENTRE is unbiased, and half of an unbiased error deletes terrain no
    road covers — 1,772-6,040 m² of sky per 240 m window against the 0-16 m² this leaves. A face
    deleted when all four CORNER CELLS are cut tests the corners against the UNION of every
    prism, so four corners covered by four DIFFERENT prisms pass while the interior escapes them
    all — which is the geometry of a junction, and was the mechanism behind the junction gaps.

    `roadbed_sink` defaults to `ROADBED_SINK` and is applied to the lattice ONCE, before any face
    family is built, so coarse, refined and transition faces all read identical heights.
    """
    roadbed_sink = ROADBED_SINK if roadbed_sink is None else roadbed_sink
    stray_cells = 0
    # -- the shared native lattice ---------------------------------------------------------
    # The window is nfr×nfc blocks of stride² native cells; the vertex lattice is the native
    # texel grid itself (a vertex sits at a cell centre, contract §3). Every face family
    # below — coarse block quads, native cells inside the refined corridors, and the
    # transition fans that join the two — indexes this ONE lattice, so the mesh is welded by
    # construction: no duplicated positions, no edges pinned to a subsample, shared smooth
    # normals across every seam. The game keeps mesh density and per-pixel decisions
    # (clip, flatten) independent; the corridors are how this grid stops coupling them.
    nfr = (row1 - row0) // stride
    nfc = (col1 - col0) // stride
    if nfr < 1 or nfc < 1:
        return None                       # smaller than one face; the caller reports it
    lat_h = nfr * stride + 1
    lat_w = nfc * stride + 1

    heights_native = numpy.asarray(heights).reshape(height, width)
    native_y = heights_native[row0:row0 + lat_h, col0:col0 + lat_w].astype(numpy.float32) \
        * numpy.float32(elevation_scale) + numpy.float32(elevation_offset)

    roadbed = (numpy.asarray(surface_ids).reshape(height, width)
               [row0:row0 + lat_h, col0:col0 + lat_w] != 0) if surface_ids is not None \
        else numpy.zeros((lat_h, lat_w), dtype=bool)
    cut = cut_grid[row0:row0 + lat_h, col0:col0 + lat_w]
    # Face (r, c) of this window is face (row0 + r, col0 + c) of the raster, and the raster is
    # one entry smaller than the cell grid on each axis, so the window's last lattice row and
    # column carry no face — hence lat_h - 1 / lat_w - 1.
    contain = contain_raster[row0:row0 + lat_h - 1, col0:col0 + lat_w - 1]
    band_count = band_count_raster[row0:row0 + lat_h - 1, col0:col0 + lat_w - 1]
    deep_touch = deep_raster[row0:row0 + lat_h - 1, col0:col0 + lat_w - 1]

    if levels is not None:
        level_raw = numpy.asarray(levels).reshape(height, width)[row0:row0 + lat_h,
                                                                 col0:col0 + lat_w]
        has_level = level_raw != 0  # 0 = no data, same truthiness test as ever (§8)
        level_value = level_raw.astype(numpy.float32) * numpy.float32(elevation_scale) \
            + numpy.float32(elevation_offset)
        wet = has_level & (level_value > native_y)
    else:
        has_level = numpy.zeros((lat_h, lat_w), dtype=bool)
        level_value = None
        wet = has_level

    # -- corridors: where the mesh goes native ---------------------------------------------
    # Every decision boundary — clip volumes, roadbed, shoreline — gets native-resolution
    # cells; the rest of the map keeps the coarse grid. The 3-cell (~10.5 m at 3.5 m) dilation
    # covers the game's 8 m terrain smoothing apron, which the roadbed mask deliberately
    # excludes — the meld gradients down to a portal live inside it.
    # A deleted face samples its own interior, so its four corner CELLS need not be cut (they
    # are not among the samples). Such a face must still land in a refined block, or the
    # deletion below would have no native face to delete. Fold the face mask back onto the
    # cells it spans before dilating.
    cut_for_corridor = cut.copy()
    cut_for_corridor[:-1, :-1] |= contain
    cut_for_corridor[:-1, 1:] |= contain
    cut_for_corridor[1:, :-1] |= contain
    cut_for_corridor[1:, 1:] |= contain
    refine_cells = dilate(roadbed | cut_for_corridor | mask_boundary(wet), 3)

    if stride == 1:
        # A region import is already native everywhere; the whole window is one corridor and
        # the transition machinery has nothing to join.
        block_refined = numpy.ones((nfr, nfc), dtype=bool)
        block_transition = numpy.zeros((nfr, nfc), dtype=bool)
    else:
        block_refined, _ = face_footprint_reduce(refine_cells, 0, 0, nfr, nfc, stride)
        block_transition = dilate(block_refined, 1) & ~block_refined
    block_coarse = ~block_refined & ~block_transition

    # Safety net: every cut cell must sit inside a refined block (the corridor contains the
    # cut mask by construction). A stray one would mean a coarse face silently kept over a
    # tunnel.
    if stride > 1 and cut_for_corridor.any():
        s_rr, s_cc = numpy.nonzero(cut_for_corridor)
        stray = ~block_refined[numpy.minimum(s_rr // stride, nfr - 1),
                               numpy.minimum(s_cc // stride, nfc - 1)]
        stray_cells = int(stray.sum())

    # -- heights, with the anti-poke sink --------------------------------------------------
    # The game clamps ground under a carriageway down into the road's surface band; this
    # lattice linearly interpolates 3.5 m samples, so an interpolated crest can still rise
    # through the road mesh above. Every vertex whose cell touches roadbed — dilated one
    # cell, so the sink bleeds into the smoothing apron instead of tearing a step — drops by
    # the documented sink, applied to the lattice ONCE before any face family is built, so
    # coarse, refined and transition faces all read identical heights. The refine corridor is
    # the roadbed dilated 3 cells, so a sunk vertex only ever sits inside a refined block:
    # the sink cannot move a coarse corner or a fan edge.
    sink = roadbed_sink if surface_ids is not None else 0.0
    sunk = dilate(roadbed, 1) if sink else numpy.zeros((lat_h, lat_w), dtype=bool)
    ground_y = native_y - numpy.where(sunk, numpy.float32(sink), numpy.float32(0.0))

    # Where a SHALLOW band covers the cell there is a real road surface to reference, so the
    # blind sink above is replaced by the band's own floor. This is the other half of "a
    # shallow band flattens but never carves": without it the kept ground stands up through the
    # carriageway by as much as the band's roof, 0.3 m. See `clamp_shallow_bands`.
    refs_w = cut_refs[row0:row0 + lat_h, col0:col0 + lat_w]
    band_clamped, band_clamp_drop = clamp_shallow_bands(
        ground_y, band_floor_raster[row0:row0 + lat_h, col0:col0 + lat_w])

    # -- block-level ground class (0 grass / 2 rock), from the coarse corners --------------
    # Sampled (unsunk) heights, so the roadbed drop cannot flip a boundary face. Coarse and
    # transition blocks can never be roadbed: a block containing any roadbed cell is refined
    # by construction.
    corner_ys = native_y[::stride, ::stride]  # (nfr+1, nfc+1), exact by lattice construction
    run = float(stride) * cell
    gx = numpy.maximum(numpy.abs(corner_ys[:-1, 1:] - corner_ys[:-1, :-1]),
                       numpy.abs(corner_ys[1:, 1:] - corner_ys[1:, :-1])) / run
    gz = numpy.maximum(numpy.abs(corner_ys[1:, :-1] - corner_ys[:-1, :-1]),
                       numpy.abs(corner_ys[1:, 1:] - corner_ys[:-1, 1:])) / run
    block_class = numpy.where(numpy.hypot(gx, gz) > ROCK_SLOPE, 2, 0)

    # -- coarse quads ----------------------------------------------------------------------
    # Winding (NW, SW, SE, NE) — south then east — keeps the normal up in Blender's frame,
    # and faces stay QUADS so Blender tessellates them exactly as the scalar baseline did.
    lattice_flat = numpy.arange(lat_h * lat_w, dtype=numpy.int64).reshape(lat_h, lat_w)
    corner_flat = lattice_flat[::stride, ::stride]  # (nfr+1, nfc+1)
    cbr, cbc = numpy.nonzero(block_coarse)
    coarse_loops = numpy.stack((corner_flat[cbr, cbc], corner_flat[cbr + 1, cbc],
                                corner_flat[cbr + 1, cbc + 1], corner_flat[cbr, cbc + 1]),
                               axis=-1)
    coarse_class = block_class[cbr, cbc]

    # -- native cells inside refined blocks, cut only where the prism CONTAINS the face -----
    # A face is deleted iff the whole face lies inside a clip band — `_contain_faces` decides
    # that by sampling the face's own interior, and the reasoning is in its docstring. The rule
    # is the game's own invariant, not a tuned preference: the terrain shader discards PER PIXEL
    # inside a volume built from the road's own composition, so in game `clipped ⊆ covered by
    # road mesh` always holds and a see-through gap beside a road is impossible.
    #
    # Two earlier rules are recorded here because both are tempting and both are wrong:
    #
    #   * a face deleted on its own CENTRE. Unbiased, and half of an unbiased error deletes
    #     terrain no road covers — that half is sky. Measured 1,772-6,040 m² of it per 240 m
    #     window against the 0-16 m² this rule leaves.
    #   * a face deleted when all four of its CORNER CELLS are cut. That tests the corners
    #     against the UNION of every prism, so four corners covered by four different prisms
    #     passed while the face's interior escaped them all — the geometry of a junction, and
    #     the mechanism behind the junction gaps this replaces (398 m² and 197 m² of sky at the
    #     two junctions reported on 2026-08-11).
    #
    # The band used to be SPLIT at the carriageway — contained below the lane curve, face-centre
    # above it — to stop pure corner-containment walling off tunnels (§8c). That compensation is
    # gone with the rule that needed it: measured over the city's three densest tunnel-portal
    # clusters, containment sampled per face deletes 100%, 100% and 96.7% of the buried
    # centreline faces, which is the split band's own number to the digit.
    rbr, rbc = numpy.nonzero(block_refined)
    span = numpy.arange(stride, dtype=numpy.int64)
    cell_rr = numpy.broadcast_to(rbr[:, None, None] * stride + span[None, :, None],
                                 (len(rbr), stride, stride)).reshape(-1)
    cell_cc = numpy.broadcast_to(rbc[:, None, None] * stride + span[None, None, :],
                                 (len(rbr), stride, stride)).reshape(-1)
    a = cell_rr * lat_w + cell_cc
    refined_loops_all = numpy.stack((a, a + lat_w, a + lat_w + 1, a + 1), axis=-1)
    face_cut = contain[cell_rr, cell_cc]
    # Faces a DEEP band reaches but cannot contain are neither kept whole nor deleted whole —
    # they are cut at the containment sampling further down. Hold them out of the quad set here
    # so the slab and its replacement are never both drawn.
    sub_face = numpy.zeros(len(cell_rr), dtype=bool)
    if sub_bands:
        sub_face = numpy.fromiter(
            (((row0 + int(r), col0 + int(c)) in sub_bands) for r, c in zip(cell_rr, cell_cc)),
            dtype=bool, count=len(cell_rr)) & ~face_cut
    refined_loops = refined_loops_all[~face_cut & ~sub_face]
    cut_faces = int(face_cut.sum())

    # Refined faces classify from their OWN corners at native resolution; roadbed reads the
    # face's top-left cell sample, the rule the scalar face_roadbed always used.
    y00 = native_y[cell_rr, cell_cc]
    y01 = native_y[cell_rr, cell_cc + 1]
    y10 = native_y[cell_rr + 1, cell_cc]
    y11 = native_y[cell_rr + 1, cell_cc + 1]
    ngx = numpy.maximum(numpy.abs(y01 - y00), numpy.abs(y11 - y10)) / cell
    ngz = numpy.maximum(numpy.abs(y10 - y00), numpy.abs(y11 - y01)) / cell
    refined_class = numpy.where(numpy.hypot(ngx, ngz) > ROCK_SLOPE, 2, 0)
    refined_class_all = numpy.where(roadbed[cell_rr, cell_cc], 1, refined_class)
    refined_class = refined_class_all[~face_cut & ~sub_face]

    # -- transition fans -------------------------------------------------------------------
    fan_loops, fan_blocks = transition_fans(block_refined, block_transition, stride, lat_w)
    fan_class = block_class.reshape(-1)[fan_blocks]

    # -- one shared vertex set over the lattice --------------------------------------------
    all_loops = numpy.concatenate((coarse_loops.reshape(-1), refined_loops.reshape(-1),
                                   fan_loops))
    sizes = numpy.concatenate((numpy.full(len(coarse_loops), 4, dtype=numpy.int64),
                               numpy.full(len(refined_loops), 4, dtype=numpy.int64),
                               numpy.full(len(fan_loops) // 3, 3, dtype=numpy.int64)))
    face_class = numpy.concatenate((coarse_class, refined_class, fan_class))

    used = numpy.zeros(lat_h * lat_w, dtype=bool)
    used[all_loops] = True
    # A sub-cut face draws no quad of its own, but the sub-patch is interpolated from its four
    # corners and welds to them, so all four must exist in the lattice vertex set — including
    # their post-snap positions. A corner whose own corner sub-face then falls inside the band
    # ends up referenced by nothing; the prune after the patch is built removes it.
    if sub_face.any():
        used[refined_loops_all[sub_face].reshape(-1)] = True
    used_idx = numpy.nonzero(used)[0]
    vid = numpy.full(lat_h * lat_w, -1, dtype=numpy.int64)
    vid[used_idx] = numpy.arange(len(used_idx))
    ur, uc = numpy.divmod(used_idx, lat_w)
    verts = numpy.stack((origin_x + (col0 + uc).astype(numpy.float64) * cell,
                         origin_z - (row0 + ur).astype(numpy.float64) * cell,
                         ground_y[ur, uc].astype(numpy.float64)), axis=-1)
    loops = vid[all_loops]

    # Close the cut holes onto the clip prisms exactly (before UVs — they read final verts).
    snapped = unreferenced = pushed = 0
    if cut_faces and strip_geo:
        cut_face_grid = numpy.zeros((lat_h, lat_w), dtype=bool)
        cut_face_grid[cell_rr[face_cut], cell_cc[face_cut]] = True
        snapped, unreferenced, pushed = snap_hole_boundary(
            verts, vid, used.reshape(lat_h, lat_w), cut_face_grid,
            refs_w, strip_geo,
            ground_y, origin_x, origin_z, row0, col0, cell)

    # -- sub-cut the faces a deep band reaches but cannot contain --------------------------
    # A trench or portal band narrower than one face holds no whole face, so containment
    # deletes nothing along it and the face is left lying ACROSS the cutting — a 3.5 m slab
    # over the carriageway, which is what "land artifacts on the road" looks like from inside
    # a gorge. Neither keeping nor deleting the face whole is right, so it is cut at the
    # containment sampling instead: each face becomes `CLIP_CONTAIN_SAMPLES`² sub-quads and
    # the ones whose own sample is inside a band are dropped.
    #
    # Done AFTER the snap and interpolated from the FINAL corner positions, so a sub-face
    # follows a corner the snap moved and the patch still meets its neighbours exactly. The
    # boundary of a bilinear quad is linear, so a sub-vertex inserted along a shared edge sits
    # exactly on it: the T-junction against an un-subdivided neighbour is geometrically
    # watertight, and `sub_index` welds the seam between two subdivided faces.
    sub_loops, sub_class, sub_extra, sub_index = [], [], [], {}
    sub_parents = sub_dropped = 0
    if sub_face.any():
        samples = CLIP_CONTAIN_SAMPLES
        corner_id = {}                      # lattice corner -> its vertex id, for the weld

        def node(anchor, r, c, i, j):
            """The sub-patch vertex at (i, j) of face (r, c), welded across faces and, at a
            face's own corners, to the lattice vertex itself."""
            key = ((row0 + r) * samples + i, (col0 + c) * samples + j)
            found = sub_index.get(key)
            if found is not None:
                return found
            if i in (0, samples) and j in (0, samples):
                found = int(vid[(r + i // samples) * lat_w + c + j // samples])
            else:
                u, v = i / samples, j / samples
                p00, p10, p11, p01 = anchor
                sub_extra.append((1 - u) * (1 - v) * p00 + (1 - u) * v * p01
                                 + u * (1 - v) * p10 + u * v * p11)
                found = len(verts) + len(sub_extra) - 1
            sub_index[key] = found
            return found

        for pick in numpy.nonzero(sub_face)[0]:
            r, c = int(cell_rr[pick]), int(cell_cc[pick])
            block = sub_bands.get((row0 + r, col0 + c))
            ids = (int(vid[r * lat_w + c]), int(vid[(r + 1) * lat_w + c]),
                   int(vid[(r + 1) * lat_w + c + 1]), int(vid[r * lat_w + c + 1]))
            if block is None or min(ids) < 0:
                continue
            anchor = tuple(verts[i] for i in ids)             # NW, SW, SE, NE, post-snap
            parent = int(refined_class_all[pick])
            for i in range(samples):
                for j in range(samples):
                    if block[i, j]:
                        sub_dropped += 1
                        continue
                    sub_loops.extend((node(anchor, r, c, i, j), node(anchor, r, c, i + 1, j),
                                      node(anchor, r, c, i + 1, j + 1),
                                      node(anchor, r, c, i, j + 1)))
                    sub_class.append(parent)
            sub_parents += 1

        if sub_loops:
            if sub_extra:
                verts = numpy.concatenate((verts, numpy.asarray(sub_extra, dtype=numpy.float64)))
            loops = numpy.concatenate((loops, numpy.asarray(sub_loops, dtype=numpy.int64)))
            sizes = numpy.concatenate((sizes, numpy.full(len(sub_class), 4, dtype=numpy.int64)))
            face_class = numpy.concatenate((face_class,
                                            numpy.asarray(sub_class, dtype=face_class.dtype)))

        # Anchoring a sub-cut face's corners can leave one referenced by nothing, when its own
        # corner sub-face fell inside the band. Prune, so the mesh carries no stray vertex.
        referenced = numpy.zeros(len(verts), dtype=bool)
        referenced[loops] = True
        if not referenced.all():
            verts = verts[referenced]
            loops = (numpy.cumsum(referenced) - 1)[loops]

    refined_faces = len(refined_loops)
    fan_faces = len(fan_blocks)
    roadbed_faces = int((face_class == 1).sum())
    rock_faces = int((face_class == 2).sum())

    return LatticeMesh(
        lat_h=lat_h, lat_w=lat_w, nfr=nfr, nfc=nfc, verts=verts, loops=loops, sizes=sizes,
        face_class=face_class, native_y=native_y, ground_y=ground_y, wet=wet, has_level=has_level, level_value=level_value,
        roadbed=roadbed, cut=cut, contain=contain, band_count=band_count, block_refined=block_refined, coarse_loops=coarse_loops,
        cbr=cbr, cbc=cbc, cell_rr=cell_rr, cell_cc=cell_cc, refined_loops_all=refined_loops_all, fan_loops=fan_loops, fan_faces=fan_faces,
        refined_faces=refined_faces, cut_faces=cut_faces, roadbed_faces=roadbed_faces, rock_faces=rock_faces,
        snapped=snapped, unreferenced=unreferenced, pushed=pushed, sub_parents=sub_parents, sub_dropped=sub_dropped, sub_kept=len(sub_class),
        band_clamped=band_clamped, band_clamp_drop=band_clamp_drop, stray_cells=stray_cells)
