"""Gate for the road/terrain interface — audit §51.

The rule, from `Game.Simulation.TerrainSystem`: `CullRoadsJob.AddSegment` emits a `LaneSection`
whose `m_MinOffset`/`m_MaxOffset` are the composition's `(edgeHeight, surfaceHeight.min,
edgeHeight)` triple plus `TerrainComposition.m_Min/m_MaxHeightOffset`, `DrawHeightAdjustments`
renders those into `m_HeightmapCascade`, and `UpdateGPUReadback` reads the cascade back into
`m_CPUHeights` — which `GetHeightData()` returns and the producer ships as `heightmap.png`. **The
ground under a road arrives already graded, so a consumer does not adjust it.**

The one edit that remains is `clamp_carriageway`: the ~1 % of covered samples that stand ABOVE
the carriageway's own cross-section are lowered onto it, because the game removes those pixels
with its road stencil (§46) and a heightfield has none. Its target is the game's own number,
`lane + top − CLIP_BAND_EXPANSION`, recovered from the shipped band by inverting the producer's
own `GenerateClipDataJob` expansion.

Pure arithmetic, no bundle needed. The measured agreement against a real city is
`tools/check_roadgrade_vs_game.py` (98.37 % of 2,307,777 covered samples on the 2026-08-16 rip).
"""

import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle import terrain  # noqa: E402

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def test_no_tuned_sink():
    """The two constants audit §51 deleted must stay deleted, and nothing may re-derive them."""
    print("road grade: the deleted inventions")
    check(not hasattr(terrain, "ROADBED_SINK"),
          "ROADBED_SINK is gone (it sank 914,616 samples 0.15 m below the game's own grade)")
    check(not hasattr(terrain, "clamp_shallow_bands"),
          "clamp_shallow_bands is gone (it lowered 100 % of covered samples to lane - 0.5)")
    check(terrain.CLIP_BAND_EXPANSION == 0.3,
          "CLIP_BAND_EXPANSION is the producer's own 0.3 m, inverted — not a tuned constant")


def test_ground_heights_only_lowers():
    """`ground_heights` is the identity except over a carriageway, and only ever lowers."""
    print("road grade: ground_heights")
    rng = numpy.random.default_rng(7)
    native = (rng.random((64, 64)) * 40.0 + 500.0).astype(numpy.float32)

    # No band anywhere: the heights are the game's and must come back bit for bit.
    ceiling = numpy.full(native.shape, numpy.inf, dtype=numpy.float32)
    ground, clamped, drop = terrain.ground_heights(native, ceiling)
    check(numpy.array_equal(ground, native) and clamped == 0 and drop == 0.0,
          "with no carriageway the shipped heights pass through untouched")

    # A carriageway strip whose ceiling sits at the mean: only what stands over it moves, and
    # only downward. Nothing below the ceiling is raised — that is what the band FLOOR clamp got
    # wrong in the other direction.
    ceiling[20:30, :] = numpy.float32(native[20:30, :].mean())
    ground, clamped, drop = terrain.ground_heights(native, ceiling)
    over = native > ceiling
    check(clamped == int(over.sum()) and clamped > 0,
          f"exactly the samples standing over the carriageway move ({clamped})")
    check(numpy.all(ground <= native + 1e-6), "the clamp only ever lowers")
    check(numpy.array_equal(ground[~over], native[~over]),
          "a sample at or below the carriageway profile is left exactly where the game graded it")
    check(numpy.allclose(ground[over], ceiling[over]),
          "a sample over the carriageway lands ON the profile top, not on the band floor")
    check(abs(drop - float((native - ceiling)[over].max())) < 1e-5,
          f"the reported worst drop is the real one ({drop:.3f} m)")
    check(native.dtype == ground.dtype, "the grid keeps its dtype")


def test_ceiling_is_the_games_profile_top():
    """`clip_span` records `lane + top - CLIP_BAND_EXPANSION`, with the crossfall, lowest wins."""
    print("road grade: the recorded ceiling")
    h = w = 12
    terrain_y = numpy.full((h, w), 100.0)
    lane, bottom, top = 100.2, -0.5, 0.3
    # A full-width quad over rows 4..8 of cell space; both sides level, so no ramp.
    quad = [(0.0, 4.0), (float(w), 4.0), (float(w), 8.0), (0.0, 8.0)]

    mask = numpy.zeros((h, w), dtype=bool)
    ceiling = numpy.full((h, w), numpy.inf)
    terrain.fill_clip_quad(mask, terrain_y, w, h, quad, lane, bottom, top, ceiling=ceiling)
    hit = numpy.isfinite(ceiling)
    check(hit.any() and numpy.array_equal(hit, mask),
          "every sample the band marks gets a ceiling, and only those")
    check(numpy.allclose(ceiling[hit], lane + top - terrain.CLIP_BAND_EXPANSION),
          f"the ceiling is lane + top - {terrain.CLIP_BAND_EXPANSION} = "
          f"{lane + top - terrain.CLIP_BAND_EXPANSION:.3f}, the game's carriageway profile top")

    # Two strips over the same cells: the LOWER carriageway must win, so the ground ends up
    # under both roads rather than under whichever was rasterised last.
    terrain.fill_clip_quad(mask, terrain_y, w, h, quad, lane - 0.4, bottom, top, ceiling=ceiling)
    check(numpy.allclose(ceiling[hit], lane - 0.4 + top - terrain.CLIP_BAND_EXPANSION),
          "where two carriageways overlap the LOWER ceiling wins")

    # Crossfall: a tilted strip's ceiling must ramp across the width, not sit at the mean.
    tilted = numpy.full((h, w), numpy.inf)
    m2 = numpy.zeros((h, w), dtype=bool)
    terrain.fill_clip_quad(m2, terrain_y, w, h, quad, (100.0, 101.0), bottom, top, ceiling=tilted)
    row = tilted[6, :]
    live = numpy.isfinite(row)
    # Only the part of the width where the flat terrain still falls inside (lane + bottom,
    # lane + top) is marked, so the visible ramp is a fraction of the full 1.0 m tilt.
    check(live.sum() > 4 and row[live][0] < row[live][-1] - 0.3,
          f"the ceiling carries the crossfall ({row[live][0]:.3f} -> {row[live][-1]:.3f}), "
          "it is not the mean of the two sides")


def test_clamp_is_idempotent():
    """Clamping twice changes nothing — the mesh lattice and the heightfield must not drift."""
    print("road grade: idempotence")
    rng = numpy.random.default_rng(11)
    native = (rng.random((40, 40)) * 5.0 + 500.0).astype(numpy.float32)
    ceiling = numpy.full(native.shape, numpy.inf, dtype=numpy.float32)
    ceiling[:, 10:20] = numpy.float32(502.0)
    once, n1, _ = terrain.ground_heights(native, ceiling)
    twice, n2, _ = terrain.ground_heights(once, ceiling)
    check(numpy.array_equal(once, twice) and n1 > 0 and n2 == 0,
          f"a second pass moves nothing ({n1} then {n2}) — mesh and heightfield cannot drift")


def main():
    test_no_tuned_sink()
    test_ground_heights_only_lowers()
    test_ceiling_is_the_games_profile_top()
    test_clamp_is_idempotent()
    print()
    if FAILED:
        print(f"road grade: FAILED {len(FAILED)}")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("road grade: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
