"""What the terrain phase says it did: the shipped tiling, the cut, and the face-family mix.

Split from `terrainbuild` for the same reason `lanereport` is split from `lanebuild` — every
line here measures something that already happened, and three of them carry the reason a number
is what it is rather than a description of the number.
"""

import numpy
from csiibundle import containment, terrainclip
from csiibundle import splat as splat_reader

from . import splatground
from .log import LOG


def tiling(tiling_block):
    """Say what tiling rates the bundle shipped, or that it shipped none."""
    named = [
        ground
        for ground in ("grass", "dirt", "rock")
        if splat_reader.ground_tiling(tiling_block, ground)
    ]
    if named:
        detail = ", ".join(
            f"{ground} "
            f"{'/'.join(f'{r:g}' for r in splat_reader.ground_tiling(tiling_block, ground)[0])} m"
            for ground in named
        )
        LOG.step(
            f"ground tiling from the bundle (near/mid/far per repeat): {detail}; "
            "extras tile with grass"
        )
    else:
        LOG.warn(
            f"no rasters.terrainTiling in the bundle — every ground falls back to "
            f"{splatground.FALLBACK_TILING:g} m per repeat, expect a visible grid"
        )


def cut(lattice, cell, shallow_skipped):
    """The clamp, the cut area against the prism area, and the sub-centimetre ribbons.

    The exact cut has no residual to report: every face a prism reaches is cut on the prism's
    own surface, which is where the game's stencil discards. What IS worth stating is how much
    ground it removed and how many sub-centimetre ribbons were dropped with it (the game
    resolves those per pixel; a polygon renders them as a hairline — see `terrainclip`).
    """
    if lattice.band_clamped:
        # APPROXIMATION, counted: the heights are the game's own graded ground (audit §51) and
        # are kept; only the samples standing ABOVE the carriageway's cross-section move, onto
        # the game's own profile top, because the game discards those pixels with a stencil.
        LOG.step(
            f"{lattice.band_clamped} lattice vertex(es) standing over a carriageway lowered onto "
            f"its profile top, worst drop {lattice.band_clamp_drop:.2f} m — the pixels the game "
            f"discards there (see csiibundle.carriageway.clamp_carriageway)"
        )
    band_samples = int(lattice.band_count.sum(dtype=numpy.int64))
    if lattice.cut_removed_m2 or band_samples:
        # How much ground the cut removed, against how much the prisms cover — the same sanity
        # check as ever, but both sides now measure AREA rather than counting whole faces, because
        # the cut is exact and removes fractions of a face. A ratio above 1.00 would mean terrain
        # removed that no prism covers, i.e. sky; the cut cannot do that by construction (it only
        # ever subtracts a prism's own interior), so a warning here means the DENOMINATOR is
        # short — the containment sampling missed part of a prism.
        sample = cell / containment.contain_samples(cell)
        clipped_area = band_samples * sample * sample
        ratio = (lattice.cut_removed_m2 / clipped_area) if clipped_area else float("nan")
        LOG.step(
            f"cut {lattice.cut_removed_m2:,.0f} m² of terrain against {clipped_area:,.0f} m² the "
            f"clip prisms cover (x{ratio:.2f})"
        )
        if ratio > 1.05:
            LOG.warn(
                f"the cut removed {(ratio - 1.0) * 100:.0f}% more than the sampled prism "
                "area — check the containment sampling, not the cut"
            )
    if lattice.cut_parents:
        LOG.step(
            f"{lattice.cut_parents} face(s) met a clip prism and were CUT on it — "
            f"{lattice.cut_pieces} piece(s) kept, {lattice.cut_removed_m2:,.0f} m² removed"
            + (
                f", {lattice.cut_slivers} ribbon(s) under "
                f"{terrainclip.MIN_WIDTH_M * 100:g} cm dropped"
                if lattice.cut_slivers
                else ""
            )
        )
    if shallow_skipped:
        LOG.step(
            f"{shallow_skipped} shallow clip volume(s) skipped: the ±0.3 m shell over a "
            "surface road is under the carriageway that hides it (contract §8c)"
        )


def summary(lattice, shape, cell, stride, wet_faces, shallow_skipped):
    """The phase summary, the face-class split, the cut, and the coarse/fan/native mix."""
    height, width = shape
    LOG.done(
        f"terrain:   {lattice.nfr}x{lattice.nfc} block window of {width}x{height} at {cell:g} m"
        + (f", stride {stride}" if stride > 1 else "")
    )
    LOG.step(
        f"{lattice.roadbed_faces} roadbed face(s), "
        f"{lattice.rock_faces} rock face(s), {wet_faces} water face(s), "
        f"{lattice.cut_faces} face(s) cut (tunnel portals, trenches and area clips — a shallow "
        f"surface band never carves)"
    )
    cut(lattice, cell, shallow_skipped)
    if stride > 1:
        LOG.step(
            f"{lattice.refined_faces} native face(s) in {int(lattice.block_refined.sum())} "
            f"corridor block(s), {lattice.fan_faces} transition fan face(s), "
            f"{len(lattice.coarse_loops)} coarse {cell * stride:g} m face(s)"
        )
