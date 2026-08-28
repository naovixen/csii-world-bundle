"""The §8 ground heights a consumer builds from — the game's own grading, kept.

The ground under a road is already where the game put it. `TerrainSystem.CullRoadsJob` does not
only clip: it also GRADES, emitting a `LaneSection` per road piece whose
`m_MinOffset`/`m_MaxOffset` are the composition's own `(edgeHeight, surfaceHeight.min,
edgeHeight)` triple, and `DrawHeightAdjustments` renders those into `m_HeightmapCascade` before
`UpdateGPUReadback` reads the cascade back into `m_CPUHeights`. `GetHeightData()` — and so
`heightmap.png` — is the graded ground; for an at-grade road min == max, so the height is
ASSIGNED, not clamped. Measured over the 2,307,777 samples a shallow band covers on the
2026-08-16 rip's 1.75 m grid, **98.37 %** already sit inside the game's own profile window
(97.10 % on the 3.5 m base map; audit §51). **A consumer therefore does not adjust these
heights.** The one thing it may do is the game's own ceiling: `clamp_carriageway` lowers the
samples that stand above the carriageway's profile top back onto it — pixels the game removes
with its stencil and a heightfield cannot. On the full city that is **11,683 of 2,297,403
(0.5 %)**, worst drop 0.31 m. Two tuned constants used to live here (a 0.15 m roadbed sink and
a clamp to the band FLOOR, `lane − 0.5`); together they moved 3.28M samples a median 0.25 m
below where CS2 grades them — 2.4 million m³ dug out from under the city's roads — and both
are gone.
"""

import numpy


def clamp_carriageway(ground_y, band_ceiling_w):
    """Lower ground that stands above the game's own carriageway profile back onto it.

    **This is the only edit a consumer makes to the shipped heights, and it is a declared
    approximation, not a rule.** The rule is the game's, and the game has already applied it —
    the module docstring carries the derivation and the numbers. What is left is the 1.17 %
    that stand ABOVE the carriageway's cross-section — median 0.025 m, max 0.299 m, 26,910
    samples of the 2026-08-16 rip, nearly all on the stamped carriageway. In practice the clamp
    fires on fewer: the ceiling raster is quantised to the heightmap's own 0.0625 m (it must
    be, or the `terraincut` mesh and the consumer's grid would not agree bit for bit at the
    seam), and the residual's median excess is smaller than half a quantum, so the full-city
    build lowers **11,683 (0.5 %)**.

    The game removes those pixels with its stencil (§46); a heightfield engine has no stencil
    and deleting the face is the move this repo has rejected twice (see `contain_faces`: at a
    node the clip fan reaches past the road mesh, so a deleted face there is sky). Lowering
    them onto the profile top is the smallest edit that closes it, it only ever lowers, and its
    target is the game's own number rather than a tuned constant.

    `band_ceiling_w` is the windowed ceiling raster `clip_span` wrote while it rasterised the
    bands: `lane + top − CLIP_BAND_EXPANSION` for every cell a shallow band selected, `+inf`
    everywhere else, with the lateral tilt of `lane_ramp` already in it and the LOWEST ceiling
    kept where two strips overlap. Taking it from there rather than re-deriving it from a
    station reference is the point — the clamp then cannot disagree with the test that chose
    the cell, which a second derivation (via last-writer-wins refs, over a mean-height lane)
    demonstrably did.

    Mutates `ground_y` in place. Returns `(vertices lowered, largest drop in metres)`.
    """
    drop = ground_y - band_ceiling_w  # +inf ceiling (no shallow band) never selects
    hit = drop > 0.0
    if not hit.any():
        return 0, 0.0
    ground_y[hit] = band_ceiling_w[hit]
    return int(hit.sum()), float(drop[hit].max())


def ground_heights(native_y, band_ceiling):
    """The §8 ground a consumer builds from the heightmap: the shipped heights, with only the
    samples standing over a carriageway lowered onto it (`clamp_carriageway`). Returns
    `(ground_y, clamped, clamp_drop)`; `native_y` is not modified.

    Nothing else is applied. Two constants used to be — a 0.15 m sink on every roadbed sample
    and a clamp of every band-covered sample to the band FLOOR (`lane − 0.5`) — and audit §51
    measured what they cost: 3,283,410 samples moved a median 0.25 m below where CS2 grades
    them, 2.4 million m³ of ground dug out from under the city's roads, which is the 25 cm
    trench at every kerb that BeamNG load #9 reported. Neither has a counterpart in the game.

    This is the one definition shared by the mesh harness (`build_lattice`) and a heightfield
    consumer's own grid, so a mesh cut from the lattice (bake `terraincut`) meets that
    heightfield sample for sample at its boundary."""
    ground_y = numpy.asarray(native_y).copy()
    clamped, clamp_drop = clamp_carriageway(ground_y, band_ceiling)
    return ground_y, clamped, clamp_drop
