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

import json
import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
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

from cs2beamng.stages import terrain as stage  # noqa: E402
from csiibundle import png  # noqa: E402

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
check(
    abs(float(factor.sum(axis=-1).min()) - 1.0) < 1e-9
    and abs(float(factor.sum(axis=-1).max()) - 1.0) < 1e-9,
    "calcBlendValues is a partition of unity",
)
corner = calc_blend_values(numpy.array([0.0]), numpy.array([0.0]))
check(abs(float(corner[0, 0]) - 1.0) < 1e-9, "calcBlendValues at (0,0) is corner 0 alone")
flat = numpy.full(4, 0.5)
shares = blend_shares(
    calc_blend_values(numpy.array([0.02]), numpy.array([0.02])), numpy.broadcast_to(flat, (1, 4))
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

print("\n-- HEIGHT_SIGMA is the value BeamNG's own arithmetic asks for --")
# A height difference dh moves the tie point by dh/2 texels (the key is h + 2*matFactor), and dh
# between two independent N(0, sigma) grounds has sigma*sqrt(2) — so the boundary's own sigma is
# sigma/sqrt(2), and +-2 of those is the +-0.25-texel excursion HEIGHT_SIGMA is derived from.
# HEIGHT_SIGMA is BeamNG's own median, not the closed form; both are reported so a future change
# to either is visible. Vanilla: 189 TerrainMaterials, sigma median 0.178 / mean 0.156 / max 0.297.
check(
    0.10 <= stage.HEIGHT_SIGMA <= 0.30,
    "HEIGHT_SIGMA sits inside vanilla BeamNG's own range",
    f"{stage.HEIGHT_SIGMA} against median 0.178, max 0.297 over 189 vanilla terrain materials",
)
check(
    abs(2 * stage.HEIGHT_SIGMA / numpy.sqrt(2) - 2 * stage.HEIGHT_SIGMA / 1.41421356) < 1e-6,
    f"+-2 sigma moves a boundary by +-{2 * stage.HEIGHT_SIGMA / numpy.sqrt(2):.2f} texel",
    f"{2 * stage.HEIGHT_SIGMA / numpy.sqrt(2) * 1.75:.2f} m on the 1.75 m grid",
)
check(
    abs(measured_shift - stage.HEIGHT_SIGMA / numpy.sqrt(2)) < 0.02,
    "the sampled displacement matches that derivation",
    f"measured {measured_shift:.3f} vs predicted {stage.HEIGHT_SIGMA / numpy.sqrt(2):.3f}",
)

rasters_path = os.path.join(BUNDLE, "terrain", "rasters.json")

print("\n-- the tier encoding: BeamNG's ADD reproduces CS2's LERP --")
# `_tier_deviation` writes 0.5 + (T - base)/2 so that base + (2t - 1)*f == lerp(base, T, f), which
# is exactly what `terrain.h.hlsl calculateValue` and `defaultMat.hlsl blendDetail` both compute.
rng2 = numpy.random.default_rng(5)
albedo8 = (rng2.random((64, 64, 3)) * 255).astype(numpy.uint8)
albedo = albedo8.astype(numpy.float64) / 255.0
base = albedo.reshape(-1, 3).mean(axis=0)[None, None, :]
tier = stage._tier_deviation(albedo8).astype(numpy.float64) / 255.0
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
if os.path.isdir(SURFACES):
    worst = None
    for ground in ("grass", "dirt", "rock", "extra1", "extra2", "extra4"):
        path = os.path.join(SURFACES, f"{ground}.png")
        if not os.path.isfile(path):
            continue
        a8 = png.rgba_array(path)[:, :, :3]
        a = a8.astype(numpy.float64) / 255.0
        b = a.reshape(-1, 3).mean(axis=0)[None, None, :]
        t = stage._tier_deviation(a8).astype(numpy.float64) / 255.0
        v = numpy.clip(b + (2 * t - 1) * 1.0, 0.0, 1.0)
        frac = float(((v <= 1e-9) | (v >= 1 - 1e-9)).any(axis=-1).mean())
        # what the old encoding did to the same pixels, at its own strengths
        o = numpy.clip(a - a.reshape(-1, 3).mean(axis=0)[None, None, :] + 0.5, 0, 1)
        ov = numpy.clip(numpy.clip(b + (2 * o - 1) * 0.6, 0, 1) + (2 * o - 1) * 0.85, 0, 1)
        ofrac = float(((ov <= 1e-9) | (ov >= 1 - 1e-9)).any(axis=-1).mean())
        print(
            f"  {ground:8s} clamped: {frac * 100:5.1f} %   (the encoding it replaced: {ofrac * 100:5.1f} %)"
        )
        worst = frac if worst is None else max(worst, frac)
    check(
        worst is not None and worst < 0.01,
        "no shipped ground clamps on the real textures",
        f"worst {worst * 100:.2f} %" if worst is not None else "no bundle",
    )
old = numpy.clip(albedo - albedo.reshape(-1, 3).mean(axis=0)[None, None, :] + 0.5, 0, 1)
old_v = numpy.clip(numpy.clip(base + (2 * old - 1) * 0.6, 0, 1) + (2 * old - 1) * 0.85, 0, 1)
check(
    old_v.std() > full.std() * 1.5,
    "and it is materially flatter than what it replaced",
    f"contrast sigma {full.std():.3f} against the old {old_v.std():.3f}",
)

print("\n-- the tier strengths are the game's cross-fade, and they partition --")


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


if os.path.isfile(rasters_path):
    _block = json.load(open(rasters_path, encoding="utf-8")).get("terrainTiling")
    if isinstance(_block, dict):
        _m, dd, md, _f = stage._tiling_of(_block, "grass")
        check(
            dd == md,
            "one distance ramp serves both tiers - the check that the mapping is right",
            f"{[round(x) for x in dd]}",
        )
        for pd in (0, 100, 1000, 4000):
            det = strength(dd, [0, 0], [1, 0], pd)
            mac = strength(md, [0, 1], [0, 1], pd)
            print(f"  {pd:5d} m: near {det:.3f}  mid {mac:.3f}  far {1 - det - mac:.3f}")
        check(strength(dd, [0, 0], [1, 0], 0) > 0.99, "at the camera the NEAR tiling is what draws")
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

print("\n-- the tilings come from the game, not from us --")
if os.path.isfile(rasters_path):
    block = json.load(open(rasters_path, encoding="utf-8")).get("terrainTiling")
    if not isinstance(block, dict):
        check(
            False, "the bundle carries rasters.terrainTiling", "absent — this rip predates 095322a"
        )
    else:
        metres, detail_d, macro_d, from_game = stage._tiling_of(block, "grass")
        want = block["layers"]["grass"]["metresPerRepeat"]
        blend = block["layers"]["grass"]["lodBlend"]
        check(from_game, "grass reads the shipped block")
        check(
            abs(metres["detail"] - want[0]) < 1e-6
            and abs(metres["macro"] - want[1]) < 1e-6
            and abs(metres["base"] - want[2]) < 1e-6,
            "near -> detail, mid -> macro, far -> base",
            f"detail {metres['detail']:.2f} m, macro {metres['macro']:.2f} m, base {metres['base']:.1f} m",
        )
        # *TexSize is in LAYER TEXELS: the shader samples at (terrainUv * layerSize) / TexSize,
        # and vanilla's TexSize x squareSize lands on BeamNG's documented 2-8 / 30-80 / 128-2048 m
        for cell in (1.75, 3.5):
            got = {t: stage._tex_size(metres[t], cell) for t in ("detail", "macro", "base")}
            err = max(abs(got[t] * cell - metres[t]) / metres[t] for t in got)
            check(
                err < 0.04,
                f"grass tiers land within 4 % of the game's metres on a {cell} m grid",
                ", ".join(
                    f"{t} {got[t]} texels = {got[t] * cell:.1f} m (want {metres[t]:.1f})"
                    for t in got
                ),
            )
        check(
            stage._tex_size(14.336, 1.75) == 8 and stage._tex_size(14.336, 1.0) == 14,
            "TexSize scales with squareSize - it is texels, not metres",
            "14.34 m is 8 texels at 1.75 m and 14 at 1.0 m",
        )
        check(
            metres != stage.FALLBACK_TILE_M,
            "the pre-§47 constants are gone",
            f"was base {stage.FALLBACK_TILE_M['base']}/macro {stage.FALLBACK_TILE_M['macro']}/"
            f"detail {stage.FALLBACK_TILE_M['detail']} m for every ground",
        )
        check(
            detail_d == [0.0, blend[0], blend[1], blend[3]] and macro_d == detail_d,
            "the tier ramp is the prefab's own lodBlend",
            f"near->mid {blend[0]:.0f}-{blend[1]:.0f} m, mid->far ends {blend[3]:.0f} m",
        )
        # extras take the grass tiling: the bundle's own semantics string says so
        extra, _, _, extra_from_game = stage._tiling_of(block, "extra2")
        check(extra_from_game and extra == metres, "an extra tiles with grass")
        rock, _, _, _ = stage._tiling_of(block, "rock")
        check(
            rock["detail"] > metres["detail"],
            "rock is coarser than grass, as authored",
            f"rock detail {rock['detail']:.2f} m vs grass {metres['detail']:.2f} m",
        )
else:
    check(
        False,
        "the bundle carries terrain/rasters.json",
        "no bundle on this machine — awaiting a rip",
    )
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
        check(
            cx > 0.25 and cy < -0.25 and abs(cx + cy) < 0.25,
            f"{ground}: alpha's gradient reproduces the normal's XY",
            f"corr(nx,-dh/du) {cx:+.3f}, corr(ny,-dh/dv) {cy:+.3f}",
        )
    check(tested >= 3, "at least three grounds measured", f"{tested}")
else:
    check(False, "terrain/surfaces/ exists", "no bundle on this machine — awaiting a rip")

print("\n-- ground mesh maps carry BeamNG's colour-space suffix --")
# The suffix is how BeamNG decides sRGB vs linear. Without one an albedo is read as linear data
# and renders pale: measured off load #24 the cut pieces were (174,179,166) where the terrain
# beside them was (119,133,81), and "sRGB mistaken for linear, re-encoded" predicts (180,190,152).
# The convention is BeamNG's own: 1404 vanilla baseColorMaps end .color.png, and our own road and
# marking materials -- the ones that always rendered right -- end .color.dds.
with tempfile.TemporaryDirectory() as tmp2:

    class _GroundLevel:
        name = "gate"

        def __init__(self, root):
            self.root = root

        def write_bytes_if_changed(self, data, *parts):
            path = os.path.join(self.root, *parts)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)

        def game_path(self, *parts):
            return "/levels/gate/" + "/".join(parts)

    lv = _GroundLevel(tmp2)
    rngc = numpy.random.default_rng(9)
    src = os.path.join(tmp2, "g.png")
    with open(src, "wb") as handle:
        handle.write(png.encode_rgb((rngc.random((64, 64, 3)) * 200 + 20).astype(numpy.uint8)))
    base, detail = stage._ground_mesh_maps(lv, src, "gate_grass_ground_b", size=64)
    check(base.endswith(".color.png"), "the base map is tagged .color (sRGB)", base.split("/")[-1])
    check(
        detail.endswith(".data.png"),
        "the detail map is tagged .data (raw linear)",
        detail.split("/")[-1],
    )
    nrm, nrm_detail = stage._ground_mesh_maps(lv, src, "gate_grass_ground_nm", normal=True, size=64)
    check(
        nrm.endswith(".normal.png") and nrm_detail == nrm,
        "the normal map is tagged .normal",
        nrm.split("/")[-1],
    )
    # `.color` is decoded to linear before blendDetail runs, so the deviation must be linear too
    from PIL import Image as _Img

    det = (
        numpy.asarray(
            _Img.open(os.path.join(tmp2, "art", "terrains", "gate_grass_ground_b.data.png"))
        ).astype(numpy.float64)
        / 255.0
    )
    bas = (
        numpy.asarray(
            _Img.open(os.path.join(tmp2, "art", "terrains", "gate_grass_ground_b.color.png"))
        ).astype(numpy.float64)
        / 255.0
    )
    lin = (numpy.asarray(_Img.open(src).convert("RGB")).astype(numpy.float64) / 255.0) ** 2.2
    want = 0.5 + (lin - lin.reshape(-1, 3).mean(axis=0)[None, None, :]) * 0.5
    check(
        float(numpy.abs(det - want).max()) < 3.0 / 255.0,
        "the detail deviation is computed in the space blendDetail runs in",
        f"max err {float(numpy.abs(det - want).max()):.4f}",
    )
    # The base slot must be LOW-FREQUENCY or its own contrast is added a second time: the detail
    # carries the deviation from the image MEAN, so `base + (2*detail - 1)` only reproduces the
    # albedo where the base is locally that mean. Measured on the shipped grounds before this:
    # 1.8x the albedo's sigma and 32.5 % of extra2 clamped to black.
    base_lin = bas**2.2
    check(
        base_lin.std() < lin.std() * 0.5,
        "the base map is low-passed, not the full albedo",
        f"base sigma {base_lin.std():.4f} against the albedo's {lin.std():.4f}",
    )
    real = numpy.clip(base_lin + (2 * det - 1), 0.0, 1.0)
    check(
        abs(real.std() - lin.std()) < 0.02,
        "so it carries the albedo's contrast, not twice it",
        f"composite sigma {real.std():.4f} against the albedo's {lin.std():.4f}",
    )
# Clamping is a property of the real grounds' own contrast, so it is measured on them, not on the
# synthetic source above (uniform random, sigma 0.21 — far past any real ground, and it clamps by
# construction). Before the low-pass this was 32.5 % of extra2 crushed to black.
if os.path.isdir(SURFACES):
    from PIL import Image as _image2

    worst_clamp, worst_name = 0.0, None
    for ground in ("grass", "dirt", "rock", "extra1", "extra2", "extra4"):
        path = os.path.join(SURFACES, f"{ground}.png")
        if not os.path.isfile(path):
            continue
        a8 = numpy.asarray(_image2.open(path).convert("RGB")).astype(numpy.uint8)
        a = numpy.clip((a8.astype(numpy.float64) / 255.0) ** 2.2 * 2.57, 0, 1)  # the build's gain
        enc = _image2.fromarray((a ** (1 / 2.2) * 255).astype(numpy.uint8))
        low = numpy.asarray(
            enc.resize((stage.BASE_LOWPASS,) * 2, _image2.BOX).resize(enc.size, _image2.BILINEAR)
        )
        base = (low.astype(numpy.float64) / 255.0) ** 2.2
        comp = numpy.clip(base + (a - a.reshape(-1, 3).mean(axis=0)[None, None, :]), 0.0, 1.0)
        frac = float(((comp <= 1e-9) | (comp >= 1 - 1e-9)).any(axis=-1).mean())
        if frac > worst_clamp:
            worst_clamp, worst_name = frac, ground
    check(
        worst_clamp < 0.02,
        "and no shipped ground clamps in the mesh composite",
        f"worst {worst_name} {worst_clamp * 100:.2f} % (32.5 % before the low-pass)",
    )

print("\n-- _height_texture writes the relief as a tier deviation --")


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

    written = (
        numpy.asarray(Image.open(os.path.join(tmp, "art", "terrains", "h.png"))).astype(
            numpy.float64
        )
        / 255.0
    )
    check(abs(written.mean() - 0.5) < 0.02, "centred on mid-gray", f"mean {written.mean():.4f}")
    # written at HALF sigma: the base tier is flat, so the additive overlay doubles it back
    check(
        abs(written.std() - stage.HEIGHT_SIGMA * 0.5) < 0.01,
        "written at half HEIGHT_SIGMA",
        f"sigma {written.std():.4f}, composite 0.5 + (2t - 1) = {stage.HEIGHT_SIGMA}",
    )
    composite = numpy.clip(0.5 + (2 * written - 1) * 1.0, 0.0, 1.0)
    check(
        abs(composite.std() - stage.HEIGHT_SIGMA) < 0.01,
        "and the composite lands on HEIGHT_SIGMA",
        f"sigma {composite.std():.4f}",
    )

    flat_src = os.path.join(tmp, "flat.png")
    rgba[:, :, 3] = 200
    with open(flat_src, "wb") as handle:
        handle.write(png.encode_rgba(rgba))
    check(
        stage._height_texture(level, flat_src, "flat_h.png") is None,
        "a constant alpha is NOT a height map",
        "the material keeps the neutral 128",
    )
    check(
        stage._height_texture(level, os.path.join(tmp, "nope.png"), "x.png") is None,
        "a missing source yields None",
    )

print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
if FAILS:
    print("FAILED:")
    for name in FAILS:
        print(f"  - {name}")
sys.exit(1 if FAILS else 0)
