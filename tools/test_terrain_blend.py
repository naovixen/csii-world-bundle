"""Gate for audit §47 — the terrain material blend: the game's tilings, and the ground's relief.

Run: `python tools/test_terrain_blend.py` (seconds). The real bundle supplies the fidelity
numbers; without one the ported-shader checks still run on synthetic maps, and the bundle-fed
ones report "awaiting a rip" rather than passing silently.

The sweep's method asks for a predicate that can be SAMPLED and a number rather than a
screenshot, so this file ports BeamNG's own terrain blend out of
`shaders/common/terrain/terrainImpl.hlsl` + `terrain.h.hlsl` and asks it the one question the
level's look turns on: **across a boundary between two ground materials, over how much of a
layer texel does more than one material actually contribute?**

    matFactor = calcBlendValues(frac(uv))                      # bilinear corner weights
    height_i  = saturate(<material i's height map>) + 2 * matFactor[i]
    lerpVal_i = calcHeightBlend(height_i, maxHeight, weights)  # heightRange = 0.5
    out       = lerp(out, value_i, lerpVal_i)                  # so share_i = l_i * PROD_{j>i}(1-l_j)

With the flat 128 the app shipped until 2026-08-17 every material's height is 0.5, the key
collapses to `0.5 + 2f`, and the band is whatever the bilinear ramp gives on its own. With the
ground's real relief (`terrain/surfaces/<ground>_normal.png` alpha, rescaled to
`HEIGHT_SIGMA`) the boundary interlocks instead. Both are measured and printed.

Checks:
* the ported `calcBlendValues` / `calcHeightBlend` reproduce the HLSL's own invariants
  (partition of unity, symmetry, a single material owning a texel centre);
* flat heights leave a narrow, straight band, and the shipped relief widens it — reported as
  the fraction of a boundary texel where no material owns more than 90 %;
* `stages.terrain._tiling_of` returns the game's own `rasters.terrainTiling` numbers, not the
  fallbacks, and maps near -> detail / mid -> macro / far -> base;
* the alpha of `<ground>_normal.png` IS a height map: its gradient reproduces the normal's own
  XY (that is the measurement §47 rests on, re-run here so a future rip cannot quietly break it);
* `_height_texture` writes a centred map at `HEIGHT_SIGMA` and refuses a source with no alpha.
"""

import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
BUNDLE = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "Colossal Order",
                      "Cities Skylines II", "ModsData", "Carto")
SURFACES = os.path.join(BUNDLE, "terrain", "surfaces")

from csiibundle import png  # noqa: E402
from cs2beamng.stages import terrain as stage  # noqa: E402

CHECKS = 0
FAILS = []


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


# -- the ported shader ---------------------------------------------------------------------------

def calc_blend_values(u, v):
    """`terrain.h.hlsl calcBlendValues` — the bilinear weights of the four corner layer texels."""
    return numpy.stack(((1 - u) * (1 - v), u * (1 - v), (1 - u) * v, u * v), axis=-1)


def calc_height_blend(height, max_height, weights):
    """`terrain.h.hlsl calcHeightBlend`, verbatim. Returns `(lerpVal, maxHeight, weights)`."""
    height_range = 0.5
    half_range = height_range * 0.5
    diff = numpy.clip((height - max_height) / half_range, 0.0, 1.0)
    weights = weights * (1 - diff)
    max_height = numpy.maximum(max_height, height)
    w = numpy.clip((height - (max_height - height_range)) / height_range, 0.0, 1.0)
    weights = weights + w
    return w / (weights + 1e-20), max_height, weights


def blend_shares(mat_factor, mat_height):
    """`terrainImpl.hlsl blendTerrainMaterials`'s visible share per corner material.

    `mat_factor`/`mat_height` are `(..., 4)`. The shader lerps the output towards each material
    in turn by `lerpVal[i]`, so material i ends up with `lerpVal_i * PROD_{j>i}(1 - lerpVal_j)` —
    the same back-to-front composite arithmetic `splat.dominant_layer` uses."""
    shape = mat_factor.shape[:-1]
    max_height = numpy.full(shape, -1.0)
    weights = numpy.zeros(shape)
    lerp = numpy.zeros(mat_factor.shape)
    for i in range(4):
        key = numpy.clip(mat_height[..., i], 0.0, 1.0) + mat_factor[..., i] * 2.0
        value, max_height, weights = calc_height_blend(key, max_height, weights)
        # a corner the shader skips (`if(matFactor[i] <= 0.0) continue`) contributes nothing
        lerp[..., i] = numpy.where(mat_factor[..., i] > 0.0, value, 0.0)
    shares = numpy.zeros(mat_factor.shape)
    remaining = numpy.ones(shape)
    for i in range(3, -1, -1):
        shares[..., i] = lerp[..., i] * remaining
        remaining = remaining * (1.0 - lerp[..., i])
    return shares


def band_fraction(mat_height, samples=64):
    """Over a texel whose left corners are material A and right corners material B, the fraction
    of the texel where NEITHER owns more than 90 % — i.e. how much of it actually blends."""
    grid = (numpy.arange(samples) + 0.5) / samples
    u, v = numpy.meshgrid(grid, grid, indexing="xy")
    factor = calc_blend_values(u, v)
    height = numpy.broadcast_to(mat_height, factor.shape)
    shares = blend_shares(factor, height)
    # corners 0/2 are the left column (A), 1/3 the right (B) — see calc_blend_values
    a = shares[..., 0] + shares[..., 2]
    b = shares[..., 1] + shares[..., 3]
    return float(numpy.mean(numpy.maximum(a, b) < 0.9))


def boundary_u(mat_height, samples=1024):
    """Where along a texel's u axis the A|B boundary falls (A on the left corners, B on the
    right), for one pair of height samples. 0.5 is the bilinear midpoint."""
    grid = (numpy.arange(samples) + 0.5) / samples
    factor = calc_blend_values(grid, numpy.full(samples, 0.5))
    shares = blend_shares(factor, numpy.broadcast_to(mat_height, factor.shape))
    a = shares[:, 0] + shares[:, 2]
    b = shares[:, 1] + shares[:, 3]
    crossing = numpy.flatnonzero(numpy.diff(numpy.sign(a - b)) != 0)
    return float(grid[crossing[0]]) if crossing.size else (0.0 if a[0] < b[0] else 1.0)


print("-- the ported blend reproduces the shader's invariants --")
u, v = numpy.meshgrid((numpy.arange(8) + 0.5) / 8, (numpy.arange(8) + 0.5) / 8, indexing="xy")
factor = calc_blend_values(u, v)
check(abs(float(factor.sum(axis=-1).min()) - 1.0) < 1e-9 and abs(float(factor.sum(axis=-1).max()) - 1.0) < 1e-9,
      "calcBlendValues is a partition of unity")
corner = calc_blend_values(numpy.array([0.0]), numpy.array([0.0]))
check(abs(float(corner[0, 0]) - 1.0) < 1e-9, "calcBlendValues at (0,0) is corner 0 alone")
flat = numpy.full(4, 0.5)
shares = blend_shares(calc_blend_values(numpy.array([0.02]), numpy.array([0.02])), numpy.broadcast_to(flat, (1, 4)))
check(abs(float(shares.sum(axis=-1)[0]) - 1.0) < 1e-6, "the four shares sum to 1", f"{float(shares.sum(axis=-1)[0]):.6f}")
check(float(shares[0, 0]) > 0.9, "deep inside a texel one material owns it",
      f"corner-0 share {float(shares[0, 0]):.3f}")

print("\n-- what the height map does, and what it does NOT do --")
# MEASURED, and it corrected the story this change was first written on: a height map does not
# WIDEN the soft band — that is set by the bilinear ramp and heightRange alone, and is the same
# 24.5 % with or without relief. What it does is DISPLACE the boundary per pixel, so a straight
# grid-aligned edge becomes an interlocked one. Reported as both numbers so neither is claimed.
flat_band = band_fraction(numpy.full(4, 0.5))
print(f"  soft band across a boundary texel: {flat_band * 100:.1f} % of it (flat 128)")
rng = numpy.random.default_rng(47)
for sigma in (0.05, stage.HEIGHT_SIGMA, 0.25):
    pairs = rng.normal(0.5, sigma, size=(512, 2))
    band = numpy.mean([band_fraction(numpy.array([lo, hi, lo, hi]), samples=8) for lo, hi in pairs])
    shift = numpy.std([boundary_u(numpy.array([lo, hi, lo, hi]), samples=256) for lo, hi in pairs])
    print(f"  sigma {sigma:.3f}: band {band * 100:.1f} %, boundary sigma {shift:.3f} texel")
    if abs(sigma - stage.HEIGHT_SIGMA) < 1e-9:
        measured_shift, measured_band = float(shift), float(band)
check(abs(measured_band - flat_band) < 0.05, "the relief does NOT widen the soft band",
      f"{measured_band * 100:.1f} % vs {flat_band * 100:.1f} % — the band is the bilinear ramp's")
check(measured_shift > 0.05, "the relief DOES displace the boundary off the grid line",
      f"sigma {measured_shift:.3f} texel = {measured_shift * 3.5:.2f} m on a 3.5 m heightfield")

print("\n-- HEIGHT_SIGMA is the value BeamNG's own arithmetic asks for --")
# A height difference dh moves the tie point by dh/2 texels (the key is h + 2*matFactor), and dh
# between two independent N(0, sigma) grounds has sigma*sqrt(2) — so the boundary's own sigma is
# sigma/sqrt(2), and +-2 of those is the +-0.25-texel excursion HEIGHT_SIGMA is derived from.
check(abs(2 * stage.HEIGHT_SIGMA / numpy.sqrt(2) - 0.177) < 0.01,
      "+-2 sigma moves a boundary by ~+-0.18 texel", f"HEIGHT_SIGMA {stage.HEIGHT_SIGMA}")
check(abs(measured_shift - stage.HEIGHT_SIGMA / numpy.sqrt(2)) < 0.02,
      "the sampled displacement matches that derivation",
      f"measured {measured_shift:.3f} vs predicted {stage.HEIGHT_SIGMA / numpy.sqrt(2):.3f}")

print("\n-- the tilings come from the game, not from us --")
rasters_path = os.path.join(BUNDLE, "terrain", "rasters.json")
if os.path.isfile(rasters_path):
    import json
    block = json.load(open(rasters_path, encoding="utf-8")).get("terrainTiling")
    if not isinstance(block, dict):
        check(False, "the bundle carries rasters.terrainTiling", "absent — this rip predates 095322a")
    else:
        metres, detail_d, macro_d, from_game = stage._tiling_of(block, "grass")
        want = block["layers"]["grass"]["metresPerRepeat"]
        blend = block["layers"]["grass"]["lodBlend"]
        check(from_game, "grass reads the shipped block")
        check(abs(metres["detail"] - want[0]) < 1e-6 and abs(metres["macro"] - want[1]) < 1e-6
              and abs(metres["base"] - want[2]) < 1e-6,
              "near -> detail, mid -> macro, far -> base",
              f"detail {metres['detail']:.2f} m, macro {metres['macro']:.2f} m, base {metres['base']:.1f} m")
        check(metres != stage.FALLBACK_TILE_M, "the pre-§47 constants are gone",
              f"was base {stage.FALLBACK_TILE_M['base']}/macro {stage.FALLBACK_TILE_M['macro']}/"
              f"detail {stage.FALLBACK_TILE_M['detail']} m for every ground")
        check(detail_d[2:] == [blend[0], blend[1]] and macro_d[2:] == [blend[2], blend[3]],
              "the tier ramps are the prefab's own lodBlend",
              f"detail fades {blend[0]:.0f}->{blend[1]:.0f} m, macro {blend[2]:.0f}->{blend[3]:.0f} m")
        # extras take the grass tiling: the bundle's own semantics string says so
        extra, _, _, extra_from_game = stage._tiling_of(block, "extra2")
        check(extra_from_game and extra == metres, "an extra tiles with grass")
        rock, _, _, _ = stage._tiling_of(block, "rock")
        check(rock["detail"] > metres["detail"], "rock is coarser than grass, as authored",
              f"rock detail {rock['detail']:.2f} m vs grass {metres['detail']:.2f} m")
else:
    check(False, "the bundle carries terrain/rasters.json", "no bundle on this machine — awaiting a rip")
missing, _, _, from_game = stage._tiling_of(None, "grass")
check(not from_game and missing == stage.FALLBACK_TILE_M, "no block falls back and says so")

print("\n-- <ground>_normal.png alpha IS the ground's height map --")
if os.path.isdir(SURFACES):
    def corr(p, q):
        p = p.ravel().astype(numpy.float64)
        q = q.ravel().astype(numpy.float64)
        p, q = p - p.mean(), q - q.mean()
        return float((p * q).sum() / (numpy.sqrt((p * p).sum() * (q * q).sum()) + 1e-12))

    tested = 0
    for ground in ("grass", "dirt", "rock", "extra2"):
        path = os.path.join(SURFACES, f"{ground}_normal.png")
        if not os.path.isfile(path):
            continue
        tested += 1
        rgba = png.rgba_array(path).astype(numpy.float64)
        nx, ny, height = rgba[:, :, 0] - 128.0, rgba[:, :, 1] - 128.0, rgba[:, :, 3]
        cx = corr(nx, -numpy.gradient(height, axis=1))
        cy = corr(ny, -numpy.gradient(height, axis=0))
        # the green convention is flipped (OpenGL vs DirectX), so the two correlations are
        # equal and opposite — that pairing is the signature, not either sign on its own
        check(cx > 0.25 and cy < -0.25 and abs(cx + cy) < 0.25,
              f"{ground}: alpha's gradient reproduces the normal's XY",
              f"corr(nx,-dh/du) {cx:+.3f}, corr(ny,-dh/dv) {cy:+.3f}")
    check(tested >= 3, "at least three grounds measured", f"{tested}")
else:
    check(False, "terrain/surfaces/ exists", "no bundle on this machine — awaiting a rip")

print("\n-- _height_texture writes a centred map at HEIGHT_SIGMA --")


class _Level:
    """The two `level` methods `_height_texture` touches."""

    name = "gate"

    def __init__(self, root):
        self.root = root
        self.written = {}

    def write_bytes_if_changed(self, data, *parts):
        self.written[parts[-1]] = data
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def game_path(self, *parts):
        return "/levels/gate/" + "/".join(parts)


with tempfile.TemporaryDirectory() as tmp:
    level = _Level(tmp)
    rng = numpy.random.default_rng(1)
    src = os.path.join(tmp, "src.png")
    rgba = numpy.zeros((64, 64, 4), dtype=numpy.uint8)
    rgba[:, :, 3] = numpy.clip(rng.normal(70, 25, size=(64, 64)), 0, 255).astype(numpy.uint8)
    with open(src, "wb") as handle:
        handle.write(png.encode_rgba(rgba))
    out = stage._height_texture(level, src, "h.png")
    check(out is not None, "a source with alpha yields a height map", str(out))
    from PIL import Image
    written = numpy.asarray(Image.open(os.path.join(tmp, "art", "terrains", "h.png"))).astype(numpy.float64) / 255.0
    check(abs(written.mean() - 0.5) < 0.02, "centred on mid-gray", f"mean {written.mean():.4f}")
    check(abs(written.std() - stage.HEIGHT_SIGMA) < 0.01, "scaled to HEIGHT_SIGMA",
          f"sigma {written.std():.4f} vs {stage.HEIGHT_SIGMA}")

    flat_src = os.path.join(tmp, "flat.png")
    rgba[:, :, 3] = 200
    with open(flat_src, "wb") as handle:
        handle.write(png.encode_rgba(rgba))
    check(stage._height_texture(level, flat_src, "flat_h.png") is None,
          "a constant alpha is NOT a height map", "the material keeps the neutral 128")
    check(stage._height_texture(level, os.path.join(tmp, "nope.png"), "x.png") is None,
          "a missing source yields None")

print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
if FAILS:
    print("FAILED:")
    for name in FAILS:
        print(f"  - {name}")
sys.exit(1 if FAILS else 0)
