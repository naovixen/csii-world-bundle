"""How far one ground texture stretches, at each of the three tiers.

BeamNG's base/macro/detail ARE CS2's far/mid/near: both engines draw one ground at three scales
and cross-blend them by camera distance. The metres per repeat and the two distance ramps are the
game's own authored numbers, read from `rasters.terrainTiling`; the fallbacks are for a bundle
ripped before the producer shipped them, and a level built on those says so in its report.
"""

import os

import numpy
from csiibundle import images, png
from csiibundle import splat as splat_mod

from .terraintextures import BASE_LOWPASS

# The pixel size the low-passed ground-mesh base map is written at (`_mesh_colour_maps`): it
# holds `BASE_LOWPASS` texels of information, and the sampler's bilinear filter upsamples.
BASE_MAP_PX = 16

TIERS = ("base", "macro", "detail")
# BeamNG's three terrain tiers ARE CS2's three ground tilings. Both engines draw one ground at
# three scales and cross-blend them by camera distance: CS2 as `_Terrain*TextureTiling` =
# [near, mid, far] repeats across the playable area with `_Terrain*TextureTilingBlend` =
# [midStart, midEnd, farStart, farEnd] camera metres (`Game.Rendering.TerrainMaterialSystem`
# .UpdateGrassTiling, `Game.Prefabs.Terrain.TerrainTilingData`/`TerrainLodBlendData`); BeamNG as
# base + signed macro + signed detail overlays with per-tier distance ramps (`terrain.h.hlsl`
# `blendDetailMacro`/`calculateStrenth`). near -> detail, mid -> macro, far -> base.
#
# The producer has shipped the game's authored numbers as `rasters.terrainTiling` since 095322a
# and the shared reader `csiibundle.splat.ground_tiling` has served them to the mesh harness
# since the post-audit pass; this app guessed three global constants until 2026-08-17 (audit
# §47). Grass near is 14.34 m per repeat and rock near is 68.27 m, against the one 4 m detail
# tile every ground used to get — which is the repeating fine grid on every screenshot.
CS2_TILING_TIER = {"detail": 0, "macro": 1, "base": 2}  # near, mid, far
# Only for a bundle whose rip predates `rasters.terrainTiling`. Counted in the stage report so a
# level built without the game's numbers says so. TUNED (the pre-§47 constants).
FALLBACK_TILE_M = {"base": 128.0, "macro": 40.0, "detail": 4.0}
# CS2 grass, for a bundle with no block: [midStart, midEnd, farStart, farEnd]. TUNED.
FALLBACK_LOD_BLEND = (100.0, 1000.0, 2000.0, 4000.0)


# The ground meshes read like the heightfield beside them because they are given the SAME two
# tiers by the SAME arithmetic. BeamNG's ordinary v1.5 material has its own detail-map system and
# `defaultMat.hlsl`'s
#
#     half3 blendDetail(mapSample, detailSample, f) { return saturate(mapSample + (2*detailSample - 1) * max(0, f)); }
#
# is byte-for-byte the terrain shader's tier overlay — so `_tier_deviation` transfers unchanged and
# `detailBaseColorMapStrength = 1` reproduces the ground's near tiling EXACTLY over the base map.
#
# This replaced a single pre-composited texture (a baked `base + macro*0.39 + detail*0.30`). That
# texture had to hold every tier at the mesh's own 40 m UV period, so once §47 moved the detail
# tier from a global 4 m to the game's 14-68 m the whole image went low-frequency: a 10 m cut piece
# spanned 0.75 of a detail period instead of 2.50 and rendered as a flat pale slab — the stepped
# pale border in load #20's screenshots. `detailScale` is a free UV multiplier, so the detail tier
# now sits at the ground's own period with no rounding at all.
#
# APPROXIMATED, declared and counted: a v1.5 material has TWO texture slots where the terrain has
# three, so the mesh gets the near tier (detail) over the mid tier (base) and no far tier, and the
# base map's period is the mesh's baked `terraincut.GROUND_UV_M` = 40 m rather than the game's mid
# tiling (71.68 m for grass) — a 1.8x scale error on the slowest-varying tier only. Over a cut
# piece, which is metres across, both are a wash.


def _ground_mesh_maps(level, src, name, gain=1.0, normal=False, size=2048):
    """`(base_map, detail_map)` game paths for one ground mesh material, or `(None, None)`.

    **The filename suffix is load-bearing.** BeamNG reads a texture's colour space out of the
    name — `.color` is sRGB, `.data` is raw linear, `.normal` is a normal map — and every one of
    our maps shipped without one until §49. MEASURED off load #24: the cut pieces rendered at
    (174, 179, 166) where the terrain beside them was (119, 133, 81), and treating the same sRGB
    albedo as linear and re-encoding it predicts **(180, 190, 152)**. That is the whole of the
    remaining pale, washed-out, low-contrast look — the pattern and the scale already matched.
    The convention is BeamNG's own: 1,404 vanilla `baseColorMap`s end `.color.png`, and our own
    road and marking materials — the ones that always rendered correctly — end `.color.dds`.

    Because `.color` is decoded to linear before `blendDetail` runs, the detail tier's deviation
    is computed in LINEAR light here and written as `.data` (raw, undecoded), so the identity
    `base + (2t − 1) = T` that `_tier_deviation` derives holds in the space the shader actually
    does the arithmetic. The terrain's own tiers keep the encoded-space deviation: they go
    through the TerrainCellMaterial packer, not this path, and that math is in the texel's own
    space (audit §35).
    """
    if not os.path.isfile(src):
        return None, None
    # The fallback this had - the SAME full-contrast `.color` map in both the base and the
    # detail slot - is the double-contrast defect of load #25 (32.5 % of `extra2` clamped),
    # reintroduced by any read error. A wrong encoding is worse than a failed stage (B10).
    try:
        rgb = _mesh_source(src, gain, normal, size)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"terrain: ground mesh source {src} is unreadable ({exc})") from exc
    if normal:
        return _mesh_normal_maps(level, rgb, f"{name}.normal.png")
    return _mesh_colour_maps(level, rgb, name, size)


def _mesh_source(src, gain, normal, size):
    """The ground's albedo at the mesh material's size, with the declared gain applied."""
    from PIL import Image

    rgb = images.load_rgb(src)
    if abs(gain - 1.0) > 1e-3 and not normal:
        lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
        rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
    if rgb.shape[0] != size:
        rgb = numpy.asarray(Image.fromarray(rgb).resize((size, size), Image.LANCZOS))
    return rgb


def _mesh_normal_maps(level, rgb, base_name):
    """A normal map is already signed around (128, 128, 255).

    The detail slot takes it verbatim and `detailNormalMapStrength` scales it.
    """
    level.write_bytes_if_changed(
        png.encode_rgb(numpy.ascontiguousarray(rgb)), "art", "terrains", base_name
    )
    path = level.game_path("art", "terrains", base_name)
    return path, path


def _mesh_colour_maps(level, rgb, name, size):
    """`(base, detail)` — a low-passed base and the albedo's deviation from its own mean.

    The BASE slot must be LOW-FREQUENCY, because the detail slot carries the ground's deviation
    from the image MEAN and `base + (2*detail - 1)` only reproduces the albedo where the base is
    locally that mean. On the terrain that is automatic — its base tier repeats every 240-410 m,
    so it is a smooth wash across one 14-68 m detail patch — but the mesh's base is drawn at
    `GROUND_UV_M` = 40 m, and putting the full-contrast albedo there added its contrast TWICE.
    MEASURED on the shipped grounds: 1.8x the albedo's sigma and **32.5 % of `extra2` clamped**,
    7-8 % of grass/dirt/extra1 — the near-black and blown-out cream pieces of load #25.
    Low-passed, the composite clamps 0.0 % and its sigma lands on the albedo's own (grass 0.068
    against 0.069).
    """
    from PIL import Image

    linear = (rgb.astype(numpy.float64) / 255.0) ** 2.2
    base_name = f"{name}.color.png"
    # The base map is band-limited to `BASE_LOWPASS` texels by construction, so it is written
    # at `BASE_MAP_PX`, not at `size`: a 2048 x 2048 texture carrying 16 pixels of information
    # was 12.6 MiB of VRAM per ground (7-10 grounds) on a level whose limiter is memory
    # (validation pass 2026-08-31, C2). The sampler's bilinear filter does the same upsampling
    # the resize did.
    low = numpy.asarray(
        Image.fromarray(rgb)
        .resize((BASE_LOWPASS, BASE_LOWPASS), Image.BOX)
        .resize((BASE_MAP_PX, BASE_MAP_PX), Image.BILINEAR)
    )
    level.write_bytes_if_changed(
        png.encode_rgb(numpy.ascontiguousarray(low)), "art", "terrains", base_name
    )
    base_path = level.game_path("art", "terrains", base_name)
    deviation = 0.5 + (linear - linear.reshape(-1, 3).mean(axis=0)[None, None, :]) * 0.5
    detail = numpy.clip(deviation * 255.0 + 0.5, 0.0, 255.0).astype(numpy.uint8)
    detail_name = f"{name}.data.png"
    level.write_bytes_if_changed(
        png.encode_rgb(numpy.ascontiguousarray(detail)), "art", "terrains", detail_name
    )
    return base_path, level.game_path("art", "terrains", detail_name)


def _tex_size(metres, cell):
    """`*TexSize` for a tier that should repeat every `metres` — in LAYER TEXELS, not metres.

    MEASURED, and it was wrong here from §47 until §49. The terrain shader works in texels:
    `blendTerrainMaterials` takes `uv = terrainUv * layerSize`, a position in layer texels, and
    `sampleTextures`/`calculateValue` then sample at `uv / texBaseIdxSize.y` — so the period is
    `TexSize` TEXELS, i.e. `TexSize x squareSize` metres. Two independent confirmations:

    * the shader arithmetic above (`terrain.h.hlsl`, `terrainImpl.hlsl`);
    * every shipped vanilla level. `TexSize x squareSize` puts the medians at detail **2.10 m**,
      macro **40.00 m**, base **2048.00 m**, against BeamNG's own documented 2-8 / 30-80 /
      128-2048. Read as metres instead, the base tier's median would be 4096 m, twice the
      documented maximum.

    We were writing metres, so on the 1.75 m grid every tier drew **1.75x too coarse** — grass
    detail at 24.5 m where the game tiles it at 14.34, rock at 119 m. That also put the cut mesh
    and the terrain at periods 1.75x apart across their seam, because a mesh's world-planar UVs
    are in true metres.

    Packed into a `uint2` (`unpackUint2(property.texBaseIdxSizePacked)`), so it is an integer and
    is rounded here rather than truncated by the engine; at least 1.
    """
    return max(1, int(round(float(metres) / max(1e-6, float(cell)))))


def _tiling_of(tiling_block, ground):
    """`([base, macro, detail] metres per repeat, [d0..d3] detail, [d0..d3] macro, from_game)`.

    The metres are the game's own per-ground tilings (near → detail, mid → macro, far → base;
    `CS2_TILING_TIER`). The two distance ramps come from the same prefab's
    `_Terrain*TextureTilingBlend`: CS2 finishes blending the near tiling into the mid one over
    `[midStart, midEnd]` and the mid into the far over `[farStart, farEnd]`, so those are exactly
    where BeamNG's detail and macro tiers should fade out (`blendDetailMacro`: element 2 is where
    the strength leaves its near value, element 3 where it bottoms out).

    BeamNG then hard-cuts the detail sample at 250 m and the macro sample at 1000 m
    (`calculateValue`: `pixelDistance < min(250, …)` / `min(1000, …)`) whatever we ask for, so a
    ramp that runs past those ends in a cutoff. It is invisible by construction: both tiers are
    centred on mid-gray, so a mip that has converged to the tier's mean contributes exactly zero
    deviation, and at 250 m a 14 m repeat is long since mipped flat. DECLARED, not counted — it
    is a property of the arithmetic, not a per-level approximation."""
    got = splat_mod.ground_tiling(tiling_block, ground)
    if got is None:
        blend = FALLBACK_LOD_BLEND
        ramp = [0.0, blend[0], blend[1], blend[3]]
        return dict(FALLBACK_TILE_M), ramp, list(ramp), False
    repeats, blend = got
    metres = {tier: float(repeats[index]) for tier, index in CS2_TILING_TIER.items()}
    # [d0, midBlendStart, midBlendEnd, farBlendEnd]: `blendDetailMacro` reads near = d0..d1,
    # middle = d1..d2, far = d2..d3, and the cross-fade above needs exactly those four
    ramp = [0.0, blend[0], blend[1], blend[3]]
    return metres, ramp, list(ramp), True
