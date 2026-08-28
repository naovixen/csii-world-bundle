"""The containment invariant — which terrain faces a clip band is allowed to delete (§8c).

The game never cuts terrain geometry: it draws the whole heightfield and discards per pixel
against a stencil built from the clip prisms (`GenerateClipDataJob` ->
`TerrainSurface.RenderClipAreas`). A mesh cut at 3.5 m can only approximate that boundary, and
the invariant says which way to err: deleting beyond a prism is sky, deleting less leaves
terrain under a road that already hides it. Requiring the WHOLE face to be inside honours the
invariant directly.

**Only a DEEP band gets to delete at all**, and that is the correction of 2026-08-11. This used
to say `discarded ⊆ covered by road mesh` held "by construction", so a see-through gap beside a
road could not happen. It does not hold at a node. Measured on the 2026-08-11 rip at the width
transition `(4325.3, 1562.2)` / `(4321.4, 1542.0)`: the node clip fan runs out to the segment's
own side curve, the road mesh reaches only part of that sector — short by 2.2 m at the rail's
start, widening to 4.4 m at the junction centre — and the ground between them was deleted with
nothing over it. Two visible triangles, and they survived with the snap disabled, so they were
never the snap (`docs/cs2-api-audit.md` §41.7 got that wrong). The producer's own
`surface_index` calls that ground roadbed (1), i.e. the game paints it as road surface, so
keeping it is right whether the game paves it with mesh or with paint — and keeping it cannot
show sky, which deleting it demonstrably can.

A shallow band therefore flattens and paints but never carves; a trench or portal band
(`top > CLIP_DEEP_BAND_TOP`) still carves, because that is the one the game really needs
stencilled — without it every tunnel mouth is walled off. Measured across six windows this
takes see-through under a network strip to 0.0 m² and costs the terrain faces under a road
that the road hides.

**What the sampled rule replaces, and why:** the rule here used to AND the face's four CORNERS
against the union of every prism. Four corners covered by four DIFFERENT prisms passed the test
while the face's own interior escaped them all — which is exactly the geometry of a junction,
and was the mechanism behind the residual junction gaps. Measured over the two junctions
reported on 2026-08-11, corner-AND deleted 398 m² and 197 m² of terrain that no prism covers;
sampling the face's interior takes both to a few m² of sub-sample sliver.
"""

import numpy

from . import clipraster

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
# ...that number is per 3.5 m cell: what it fixes is the sub-sample SPACING (0.4375 m), so a
# heightfield at another cell size takes the count that keeps that spacing (`contain_samples`) —
# 4 on the 1.75 m `terrainFine` raster, same cost per square metre, same see-through table.
CLIP_CONTAIN_SAMPLE_M = 3.5 / CLIP_CONTAIN_SAMPLES


def contain_samples(cell):
    """Sub-samples per cell per axis for a heightfield of `cell` metres — `CLIP_CONTAIN_SAMPLES`
    at 3.5 m, scaled to keep the sub-sample spacing, never below 2."""
    return max(2, int(round(float(cell) / CLIP_CONTAIN_SAMPLE_M)))


CLIP_CONTAIN_BLOCK = 128

# A band this tall is a trench or a tunnel portal (`ElevationLimit * 3` — 12.3 m for roads,
# 6.3 m for pathways), not the everyday ±0.3 m shell over a surface road. The difference matters
# for reporting: terrain left inside a shallow band is under the road that hides it, terrain left
# inside a deep one stands in the cutting.
CLIP_DEEP_BAND_TOP = 3.0


def _block_heights(terrain_y, width, height, r0, c0, sh, sw, samples):
    """The bilinear terrain height at every sub-sample of one face block.

    Sample (i, j) sits at cell-space (c0 + 0.5 + (j + 0.5) / K, r0 + 0.5 + (i + 0.5) / K) — the
    interior of face (r0 + i // K, c0 + j // K). Terrain there is the bilinear interpolation the
    renderer already draws between the cell centres, so the test reads the same surface the eye
    sees.
    """
    gc = numpy.clip(c0 + (numpy.arange(sw) + 0.5) / samples, 0, width - 1.001)
    gr = numpy.clip(r0 + (numpy.arange(sh) + 0.5) / samples, 0, height - 1.001)
    ci = gc.astype(numpy.int64)
    fu = gc - ci
    ri = gr.astype(numpy.int64)
    fv = gr - ri
    top_row = (
        terrain_y[numpy.ix_(ri, ci)] * (1.0 - fu)[None, :]
        + terrain_y[numpy.ix_(ri, ci + 1)] * fu[None, :]
    )
    bot_row = (
        terrain_y[numpy.ix_(ri + 1, ci)] * (1.0 - fu)[None, :]
        + terrain_y[numpy.ix_(ri + 1, ci + 1)] * fu[None, :]
    )
    return top_row * (1.0 - fv)[:, None] + bot_row * fv[:, None]


def contain_faces(width, height, terrain_y, cell_cut, quad_source, samples, block):
    """Faces wholly inside the union of the DEEP clip bands — the deletion rule (contract §8c).

    The rule, its history and its measurements are the module docstring; this routine is the
    tiled evaluation. Tiled over `block`² faces so the sub-sample raster stays small: only
    blocks holding a cut cell are visited at all, which on a full city is 7.5% of the map.
    `quad_source(r0, c0, r1, c1)` yields `(quad, lane_y, bottom, top)` for every station quad
    whose face-space bounds touch the block, in NATIVE cell space; this routine scales them
    into the block's own frame.

    Returns `(contain, band_count, deep_touch)` — the (height − 1, width − 1) deletion mask; the
    count of in-band sub-samples PER FACE, which is the true clipped area in units of
    `(cell / samples)²` and, being per-face, can be windowed alongside the mask so the
    containment ratio in `build_terrain`'s report means the same thing for a region import as
    for a whole city; and which faces a DEEP band reaches without containing, the residual
    `build_terrain` reports (see `CLIP_DEEP_BAND_TOP`).
    """
    face_h, face_w = height - 1, width - 1
    contain = numpy.zeros((face_h, face_w), dtype=bool)
    band_count = numpy.zeros((face_h, face_w), dtype=numpy.uint8)  # <= samples², 6² = 36
    deep_touch = numpy.zeros((face_h, face_w), dtype=bool)

    # Only blocks holding a cut cell are visited. A face wholly inside a band has its four
    # CORNERS inside it too, and those corners are cell centres (contract §3) — which is what
    # `cell_cut` records. The converse does not hold, so this skips work, never a deletion; and
    # where the sampling and the corners disagree the face is kept, which is the safe side.
    cut_face = cell_cut[:-1, :-1] | cell_cut[:-1, 1:] | cell_cut[1:, :-1] | cell_cut[1:, 1:]
    if not cut_face.any():
        return contain, band_count, deep_touch

    for r0 in range(0, face_h, block):
        r1 = min(r0 + block, face_h)
        for c0 in range(0, face_w, block):
            c1 = min(c0 + block, face_w)
            if not cut_face[r0:r1, c0:c1].any():
                continue
            nfr, nfc = r1 - r0, c1 - c0
            sh, sw = nfr * samples, nfc * samples
            fine_y = _block_heights(terrain_y, width, height, r0, c0, sh, sw, samples)

            band = numpy.zeros((sh, sw), dtype=bool)
            deep = numpy.zeros((sh, sw), dtype=bool)
            origin = (c0 + 0.5, r0 + 0.5)
            for quad, lane_y, bottom, top in quad_source(r0, c0, r1, c1):
                local = [
                    ((px - origin[0]) * samples, (py - origin[1]) * samples) for px, py in quad
                ]
                clipraster.fill_clip_quad(band, fine_y, sw, sh, local, lane_y, bottom, top)
                if top > CLIP_DEEP_BAND_TOP:
                    clipraster.fill_clip_quad(deep, fine_y, sw, sh, local, lane_y, bottom, top)

            per_face = band.reshape(nfr, samples, nfc, samples)
            contain[r0:r1, c0:c1] = per_face.all(axis=(1, 3))
            band_count[r0:r1, c0:c1] = per_face.sum(axis=(1, 3), dtype=numpy.uint16)
            deep_touch[r0:r1, c0:c1] = deep.reshape(nfr, samples, nfc, samples).any(axis=(1, 3))

    return contain, band_count, deep_touch
