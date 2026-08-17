"""Simulate BeamNG's terrain rim beside a hole — the §50 grey rim, and what would close it.

    python tools/check_terrain_rim.py

**What this answers.** §50.6 listed "pad the material list to 32 so slot 31 exists and author it
with a zero height map" as the untried lead. This scores it before building it, and the answer is
that it helps by ~60 % and does not close the bug (audit §50.8). It is a probe, not a gate: the
domain warp's `terrainValueNoise` is modelled by smooth value noise of the same lattice, range and
frequency rather than BeamNG's exact murmur3 hash, so the numbers are statistical, not per-pixel.
Everything else is the shader verbatim, through the ported functions
`tools/test_terrain_blend.py` already gates.

Everything here is BeamNG's own shader, ported and already gated in tools/test_terrain_blend.py:
  * the hole test is `clip(1 - layerTex.Load(uv*size - 0.5).r - 0.0001)` — POINT sampled, NO warp
  * the material lookup is `layerTex.Gather(uv + layerWarp/layerSize)` — WARPED, bilinear 2x2
  * `matId = min(31, matId)`, so a 255 hole byte reads slot 31
  * blend by `calcHeightBlend` on `key = saturate(heightMap) + matFactor*2`

The two lookups use DIFFERENT positions, which is the crux: a pixel the clip keeps can still
gather the hole texel with a large weight, because the warp moves the gather up to +-0.5 texel.
"""
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("tb", os.path.join(HERE, "test_terrain_blend.py"))
tb = importlib.util.module_from_spec(spec)
sys.modules["tb"] = tb
# `test_terrain_blend` runs its own checks at import; swallow their output, we only want its
# ported shader functions.
import contextlib  # noqa: E402
import io as _io  # noqa: E402
with contextlib.redirect_stdout(_io.StringIO()):
    try:
        spec.loader.exec_module(tb)
    except SystemExit:
        pass

rng = numpy.random.default_rng(3)

# -- the scene: layer texels, hole on one side of a straight boundary ---------------------------
# Texel column index >= HOLE_AT is a hole (byte 255 -> matId 31); everything else is a real
# ground with its own relief. One texel is 1.75 m on the shipped grid.
N = 64                      # texels across
HOLE_AT = N // 2
PIX = 16                    # pixels per texel
WARP_FREQ, WARP_AMP = 1.3, 0.5


def value_noise(p):
    """`terrainValueNoise` — smooth value noise on the integer lattice, 0..1. The exact hash is
    irrelevant to the statistics; the lattice, the smoothstep and the range are what matter."""
    i = numpy.floor(p).astype(int)
    f = p - i
    f = f * f * (3.0 - 2.0 * f)
    lo, hi = i.min(), i.max() + 2
    table = rng.random((hi - lo + 2, hi - lo + 2))

    a = table[i[..., 0] - lo, i[..., 1] - lo]
    b = table[i[..., 0] - lo + 1, i[..., 1] - lo]
    c = table[i[..., 0] - lo, i[..., 1] - lo + 1]
    d = table[i[..., 0] - lo + 1, i[..., 1] - lo + 1]
    return (a * (1 - f[..., 0]) + b * f[..., 0]) * (1 - f[..., 1]) + \
           (c * (1 - f[..., 0]) + d * f[..., 0]) * f[..., 1]


def run(hole_height, label, real_height=0.5):
    """`hole_height` is what slot 31's height map returns; `real_height` the ground's own relief."""
    # pixel centres in LAYER TEXEL units
    px = (numpy.arange(N * PIX) + 0.5) / PIX
    py = (numpy.arange(N * PIX) + 0.5) / PIX
    X, Y = numpy.meshgrid(px, py, indexing="xy")
    P = numpy.stack((X, Y), axis=-1)

    warp = (numpy.stack((value_noise(P * WARP_FREQ), value_noise(P * WARP_FREQ + 37.2)), axis=-1)
            * 2.0 - 1.0) * WARP_AMP

    # the CLIP: nearest texel at the UNWARPED position (Load(uv*size - 0.5) -> floor(p - 0.5 + 0.5))
    own = numpy.floor(X).astype(int)
    drawn = own < HOLE_AT

    # the GATHER: warped, bilinear 2x2 around (p + warp)
    g = P + warp
    base = numpy.floor(g - 0.5).astype(int)              # top-left of the 2x2
    frac = (g - 0.5) - base
    factor = tb.calc_blend_values(frac[..., 0], frac[..., 1])
    cols = numpy.stack((base[..., 0], base[..., 0] + 1, base[..., 0], base[..., 0] + 1), axis=-1)
    is_hole = cols >= HOLE_AT

    height = numpy.where(is_hole, hole_height, real_height)
    shares = tb.blend_shares(factor, height)
    hole_share = numpy.where(is_hole, shares, 0.0).sum(axis=-1)

    hs = hole_share[drawn]
    dist = (HOLE_AT - X)[drawn]                          # texels from the boundary
    print(f"   {label}")
    print(f"      drawn pixels showing >10 % slot 31: {(hs > 0.10).mean() * 100:6.2f} %   "
          f">50 %: {(hs > 0.50).mean() * 100:6.2f} %   mean share {hs.mean() * 100:5.2f} %")
    for lo, hi in ((0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.5)):
        sel = (dist >= lo) & (dist < hi)
        if sel.sum():
            print(f"      {lo:.1f}-{hi:.1f} texel ({lo * 1.75:.2f}-{hi * 1.75:.2f} m) from the hole: "
                  f"mean slot-31 share {hs[sel].mean() * 100:5.2f} %, >50 % on {(hs[sel] > 0.5).mean() * 100:5.2f} %")
    return hs


print("== BeamNG rim beside a hole: how much of the drawn ground is slot 31? ==")
print("   (one layer texel = 1.75 m on the shipped grid)")
print()
print("A. TODAY - slot 31 is undefined and reads texture-array slice 0; its height map is")
print("   whatever slot 0's is, so it competes on equal footing:")
run(0.5, "slot 31 height == the real ground's (the garbage read)")
print()
print("B. PROPOSED - slot 31 padded in and authored with a ZERO height map:")
run(0.0, "slot 31 height == 0")
print()
print("C. Worst case for B: the real ground's relief at its DARKEST (height map 0), where")
print("   slot 31 has no height deficit to lose by:")
run(0.0, "slot 31 height == 0, real ground height == 0", real_height=0.0)
print()
print("D. For reference, the same with the warp OFF (TERRAIN_LAYER_WARP 0) - not something we")
print("   can switch, but it isolates how much of the rim the warp is responsible for:")
WARP_AMP = 0.0
run(0.5, "warp off, slot 31 height == the real ground's")
run(0.0, "warp off, slot 31 height == 0")
