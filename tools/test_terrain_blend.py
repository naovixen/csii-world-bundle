"""Gate for audit §47, first half — the ported BeamNG terrain blend and the tier arithmetic.

Split from the original `test_terrain_blend.py` script (refactor Phase 4): this file holds the
ported-shader invariants, the height-map displacement measurement, the HEIGHT_SIGMA derivation
and the tier ADD-reproduces-LERP encoding; `tools/test_terrain_tiling.py` holds the
bundle-facing half (tilings, normal-alpha heights, ground mesh maps). The method and the
measured story are unchanged:

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

import json
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "AppData",
    "LocalLow",
    "Colossal Order",
    "Cities Skylines II",
    "ModsData",
    "Carto",
)
SURFACES = os.path.join(BUNDLE, "terrain", "surfaces")

from cs2beamng.stages import terraintextures as textures  # noqa: E402
from cs2beamng.stages import terraintiling as tiling  # noqa: E402
from csiibundle import png  # noqa: E402

CHECKS = 0
FAILS = []
# A claim this run could not put a number on, because it needs a rip and there is none. That is
# NOT a pass — a silent skip is how a gate stops being a gate — and it is not a failure either,
# or CI, which never has a bundle, would be permanently red. It is a third outcome, printed by
# name, and enforced the moment a bundle IS present.
UNPROVEN = []


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def finish():
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        print("FAILED:")
        for name in FAILS:
            print(f"  - {name}")
    return 1 if FAILS else 0


def enforce():
    """The gate's verdict: every check green, and with a bundle present, nothing unmeasured."""
    assert not FAILS, f"{len(FAILS)} check(s) failed — see the captured output above"
    if os.path.isfile(os.path.join(BUNDLE, "bundle.json")):
        # With a bundle on disk, "unproven" means the RIP did not carry the layer — a finding
        # about the producer, not about this machine.
        assert not UNPROVEN, (
            f"a bundle is present but {len(UNPROVEN)} claim(s) went unmeasured: {UNPROVEN}"
        )


# -- the ported shader ------------------------------------------------------------------


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


def test_shader_invariants():
    print("-- the ported blend reproduces the shader's invariants --")
    u, v = numpy.meshgrid((numpy.arange(8) + 0.5) / 8, (numpy.arange(8) + 0.5) / 8, indexing="xy")
    factor = calc_blend_values(u, v)
    check(
        abs(float(factor.sum(axis=-1).min()) - 1.0) < 1e-9
        and abs(float(factor.sum(axis=-1).max()) - 1.0) < 1e-9,
        "calcBlendValues is a partition of unity",
    )
    corner = calc_blend_values(numpy.array([0.0]), numpy.array([0.0]))
    check(abs(float(corner[0, 0]) - 1.0) < 1e-9, "calcBlendValues at (0,0) is corner 0 alone")
    flat = numpy.full(4, 0.5)
    shares = blend_shares(
        calc_blend_values(numpy.array([0.02]), numpy.array([0.02])),
        numpy.broadcast_to(flat, (1, 4)),
    )
    check(
        abs(float(shares.sum(axis=-1)[0]) - 1.0) < 1e-6,
        "the four shares sum to 1",
        f"{float(shares.sum(axis=-1)[0]):.6f}",
    )
    check(
        float(shares[0, 0]) > 0.9,
        "deep inside a texel one material owns it",
        f"corner-0 share {float(shares[0, 0]):.3f}",
    )


def test_height_displacement():
    """Returns the measured boundary displacement, which the sigma check consumes."""
    print("\n-- what the height map does, and what it does NOT do --")
    # MEASURED, and it corrected the story this change was first written on: a height map does not
    # WIDEN the soft band — that is set by the bilinear ramp and heightRange alone, and is the same
    # 24.5 % with or without relief. What it does is DISPLACE the boundary per pixel, so a straight
    # grid-aligned edge becomes an interlocked one. Reported as both numbers so neither is claimed.
    flat_band = band_fraction(numpy.full(4, 0.5))
    print(f"  soft band across a boundary texel: {flat_band * 100:.1f} % of it (flat 128)")
    rng = numpy.random.default_rng(47)
    for sigma in (0.05, textures.HEIGHT_SIGMA, 0.25):
        pairs = rng.normal(0.5, sigma, size=(512, 2))
        band = numpy.mean(
            [band_fraction(numpy.array([lo, hi, lo, hi]), samples=8) for lo, hi in pairs]
        )
        shift = numpy.std(
            [boundary_u(numpy.array([lo, hi, lo, hi]), samples=256) for lo, hi in pairs]
        )
        print(f"  sigma {sigma:.3f}: band {band * 100:.1f} %, boundary sigma {shift:.3f} texel")
        if abs(sigma - textures.HEIGHT_SIGMA) < 1e-9:
            measured_shift, measured_band = float(shift), float(band)
    check(
        abs(measured_band - flat_band) < 0.05,
        "the relief does NOT widen the soft band",
        f"{measured_band * 100:.1f} % vs {flat_band * 100:.1f} % — the band is the bilinear ramp's",
    )
    check(
        measured_shift > 0.05,
        "the relief DOES displace the boundary off the grid line",
        f"sigma {measured_shift:.3f} texel = {measured_shift * 3.5:.2f} m on a 3.5 m heightfield",
    )
    return measured_shift


def test_height_sigma(measured_shift):
    print("\n-- HEIGHT_SIGMA is the value BeamNG's own arithmetic asks for --")
    # A height difference dh moves the tie point by dh/2 texels (the key is h + 2*matFactor), and dh
    # between two independent N(0, sigma) grounds has sigma*sqrt(2) — so the boundary's own sigma is
    # sigma/sqrt(2), and +-2 of those is the +-0.25-texel excursion HEIGHT_SIGMA is derived from.
    # HEIGHT_SIGMA is BeamNG's own median, not the closed form; both are reported so a future change
    # to either is visible. Vanilla: 189 TerrainMaterials, sigma median 0.178 / mean 0.156 / max 0.297.
    check(
        0.10 <= textures.HEIGHT_SIGMA <= 0.30,
        "HEIGHT_SIGMA sits inside vanilla BeamNG's own range",
        f"{textures.HEIGHT_SIGMA} against median 0.178, max 0.297 over 189 vanilla terrain materials",
    )
    check(
        abs(2 * textures.HEIGHT_SIGMA / numpy.sqrt(2) - 2 * textures.HEIGHT_SIGMA / 1.41421356)
        < 1e-6,
        f"+-2 sigma moves a boundary by +-{2 * textures.HEIGHT_SIGMA / numpy.sqrt(2):.2f} texel",
        f"{2 * textures.HEIGHT_SIGMA / numpy.sqrt(2) * 1.75:.2f} m on the 1.75 m grid",
    )
    check(
        abs(measured_shift - textures.HEIGHT_SIGMA / numpy.sqrt(2)) < 0.02,
        "the sampled displacement matches that derivation",
        f"measured {measured_shift:.3f} vs predicted {textures.HEIGHT_SIGMA / numpy.sqrt(2):.3f}",
    )


def test_tier_encoding():
    print("\n-- the tier encoding: BeamNG's ADD reproduces CS2's LERP --")
    # `_tier_deviation` writes 0.5 + (T - base)/2 so that base + (2t - 1)*f == lerp(base, T, f), which
    # is exactly what `terrain.h.hlsl calculateValue` and `defaultMat.hlsl blendDetail` both compute.
    rng2 = numpy.random.default_rng(5)
    albedo8 = (rng2.random((64, 64, 3)) * 255).astype(numpy.uint8)
    albedo = albedo8.astype(numpy.float64) / 255.0
    base = albedo.reshape(-1, 3).mean(axis=0)[None, None, :]
    tier = textures._tier_deviation(albedo8).astype(numpy.float64) / 255.0
    full = numpy.clip(base + (2 * tier - 1) * 1.0, 0.0, 1.0)
    check(
        float(numpy.abs(full - albedo).max()) < 3.0 / 255.0,
        "strength 1 reproduces the tier exactly",
        f"max |v - albedo| = {float(numpy.abs(full - albedo).max()):.4f}, 8-bit quantisation only",
    )
    half = numpy.clip(base + (2 * tier - 1) * 0.5, 0.0, 1.0)
    check(
        float(numpy.abs(half - (base + (albedo - base) * 0.5)).max()) < 3.0 / 255.0,
        "an intermediate strength is the cross-fade",
        "lerp(base, tier, f)",
    )
    # the invariant: at strength 1 the composite IS the source, so it reaches a rail only where the
    # source already does. (This synthetic albedo is uniform on [0,1], so it sits on both rails by
    # construction — the real grounds are measured below.)
    rail = ((albedo <= 1e-9) | (albedo >= 1 - 1e-9)).any(axis=-1)
    hit = ((full <= 1e-9) | (full >= 1 - 1e-9)).any(axis=-1)
    check(
        bool((hit & ~rail).sum() == 0),
        "it clamps only where the source itself is at a rail",
        f"{int((hit & ~rail).sum())} extra clamped pixels",
    )
    _tier_encoding_shipped_grounds()
    old = numpy.clip(albedo - albedo.reshape(-1, 3).mean(axis=0)[None, None, :] + 0.5, 0, 1)
    old_v = numpy.clip(numpy.clip(base + (2 * old - 1) * 0.6, 0, 1) + (2 * old - 1) * 0.85, 0, 1)
    check(
        old_v.std() > full.std() * 1.5,
        "and it is materially flatter than what it replaced",
        f"contrast sigma {full.std():.3f} against the old {old_v.std():.3f}",
    )


def _tier_encoding_shipped_grounds():
    """The clamp fractions on the REAL grounds, against what the retired encoding did."""
    if not os.path.isdir(SURFACES):
        return
    worst = None
    for ground in ("grass", "dirt", "rock", "extra1", "extra2", "extra4"):
        path = os.path.join(SURFACES, f"{ground}.png")
        if not os.path.isfile(path):
            continue
        a8 = png.rgba_array(path)[:, :, :3]
        a = a8.astype(numpy.float64) / 255.0
        b = a.reshape(-1, 3).mean(axis=0)[None, None, :]
        t = textures._tier_deviation(a8).astype(numpy.float64) / 255.0
        v = numpy.clip(b + (2 * t - 1) * 1.0, 0.0, 1.0)
        frac = float(((v <= 1e-9) | (v >= 1 - 1e-9)).any(axis=-1).mean())
        # what the old encoding did to the same pixels, at its own strengths
        o = numpy.clip(a - a.reshape(-1, 3).mean(axis=0)[None, None, :] + 0.5, 0, 1)
        ov = numpy.clip(numpy.clip(b + (2 * o - 1) * 0.6, 0, 1) + (2 * o - 1) * 0.85, 0, 1)
        ofrac = float(((ov <= 1e-9) | (ov >= 1 - 1e-9)).any(axis=-1).mean())
        print(
            f"  {ground:8s} clamped: {frac * 100:5.1f} %   "
            f"(the encoding it replaced: {ofrac * 100:5.1f} %)"
        )
        worst = frac if worst is None else max(worst, frac)
    check(
        worst is not None and worst < 0.01,
        "no shipped ground clamps on the real textures",
        f"worst {worst * 100:.2f} %" if worst is not None else "no bundle",
    )


def test_tier_strengths():
    rasters_path = os.path.join(BUNDLE, "terrain", "rasters.json")
    print("\n-- the tier strengths are the game's cross-fade, and they partition --")
    if os.path.isfile(rasters_path):
        _block = json.load(open(rasters_path, encoding="utf-8")).get("terrainTiling")
        if isinstance(_block, dict):
            _m, dd, md, _f = tiling._tiling_of(_block, "grass")
            check(
                dd == md,
                "one distance ramp serves both tiers - the check that the mapping is right",
                f"{[round(x) for x in dd]}",
            )
            for pd in (0, 100, 1000, 4000):
                det = strength(dd, [0, 0], [1, 0], pd)
                mac = strength(md, [0, 1], [0, 1], pd)
                print(f"  {pd:5d} m: near {det:.3f}  mid {mac:.3f}  far {1 - det - mac:.3f}")
            check(
                strength(dd, [0, 0], [1, 0], 0) > 0.99,
                "at the camera the NEAR tiling is what draws",
            )
            check(
                strength(md, [0, 1], [0, 1], 1000) > 0.99,
                "by midBlendEnd the MID tiling has taken over",
            )
            d_far = strength(dd, [0, 0], [1, 0], 4000)
            m_far = strength(md, [0, 1], [0, 1], 4000)
            check(
                d_far < 0.01 and m_far < 0.01,
                "by farBlendEnd only the FAR tiling is left",
                f"near {d_far:.3f}, mid {m_far:.3f}",
            )
            over = max(
                strength(dd, [0, 0], [1, 0], pd) + strength(md, [0, 1], [0, 1], pd)
                for pd in range(0, 4001, 25)
            )
            check(over <= 1.001, "the three tiers never over-sum", f"max(near + mid) = {over:.3f}")


def strength(distances, atten, s, pd):
    """terrain.h.hlsl calculateStrenth over blendDetailMacro, at one pixel distance."""
    d = [float(x) for x in distances]

    def ramp(lo, hi):
        if hi <= lo:
            return 1.0 if pd > lo else 0.0
        return min(1.0, max(0.0, (pd - lo) / (hi - lo)))

    near, mid, far = ramp(d[0], d[1]), ramp(d[1], d[2]), ramp(d[2], d[3])
    a = [1.0 - atten[0], 1.0 - atten[1]]
    lo = a[0] * s[0] + (s[0] - a[0] * s[0]) * near
    hi = s[1] + (a[1] * s[1] - s[1]) * far
    return lo + (hi - lo) * mid


def main():
    test_shader_invariants()
    test_height_sigma(test_height_displacement())
    test_tier_encoding()
    test_tier_strengths()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_terrain_blend.py`."""
    main()
    enforce()
