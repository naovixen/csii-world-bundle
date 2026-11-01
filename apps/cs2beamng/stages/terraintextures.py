"""Authoring the ground textures BeamNG's terrain shader reads.

Three tiers per ground (base, macro, detail) from one CS2 surface, plus a height map from the
ground's own relief and one declared albedo gain. The tier encoding is the shader's own — a
strength IS a lerp weight — so `tier_deviation` is a port, and everything that is NOT a port says
so where it is written.
"""

import os

import numpy
from csiibundle import images, png

# a ground whose texture the bundle does not carry borrows this one's
TEXTURE_FALLBACK = {
    "roadbed": "dirt",
    "extra1": "grass",
    "extra2": "grass",
    "extra3": "dirt",
    "extra4": "dirt",
}

# WHY A GAIN EXISTS AT ALL, from CS2's source (audit §55). `Game.Rendering.LightingSystem.
# SetupPostprocessing` puts HDRP in `ExposureMode.Automatic`, and `DayNightCycleData` clamps it to
# [DayExposureMin, DayExposureMax] = [4, 14] EV by day — the exact pair the producer already ships
# in `grading.json`. So CS2 meters the scene every frame and normalises it, which is what lets its
# ground albedos be authored dark (grass ~0.06 linear) under a 110 klux sun. HDRP does not expose
# the metered value, so there is no number to read and replay; BeamNG has no equivalent metering on
# terrain. A fixed linear gain on the albedo is therefore the right SHAPE of answer, and it is
# declared as an approximation rather than a port.
#
# WHAT THE TARGET IS, and it is no longer tuned. The gain's job is to land CS2's grounds where
# BeamNG's own lighting was authored to receive them, so the authority is BeamNG's content, not
# CS2's. Measured over **149 vanilla ground albedos across 15 shipped levels** (Cliff, Industrial,
# Utah, automation_test_track, derby, driver_training, east_coast_usa, gridmap_v2, hirochi_raceway,
# italy, johnson_valley, jungle_rock_island, small_island, template, west_coast_usa): mean linear
# luminance 0.2085, median 0.2196, p10 0.1386, p90 0.2344. Restricted to the 26 grass-named ones —
# which is what this constant calibrates on — mean 0.2147, **median 0.2185**.
#
# **HELD AT 0.16 BY DECISION (2026-08-17), against that measurement.** §55 moved this to 0.2185
# — BeamNG's own grass median — and the build that shipped it drew black and cream slabs on the
# cut faces beside deep terrain. The cause was NOT established: measurement ruled out albedo
# clipping (0 % of ground texels clip at EITHER value), base/detail composite clipping (~0 % in
# the built textures), a frozen detail texel on a stretched face (never reaches black), and
# inverted or downward normals (0 % face downward). What the larger gain does do, measured, is
# amplify the base/detail mismatch on UV-stretched faces by exactly 1.366x — so it plausibly made
# a PRE-EXISTING defect louder without being its cause.
#
# That pre-existing defect is real and separately filed (audit §60): `bake/terraincut.py` projects
# UVs from the horizontal plane only, so a vertical cut wall gets UV span only from its horizontal
# footprint. Measured on the built level, vertical walls run **217.5 m per UV unit against 40.0
# for flat faces** — a 5.4x stretch, correlating with verticality at +0.800. That is why it shows
# up on "the side of deep terrain" and gets worse the deeper the cut.
#
# THAT DEFECT IS FIXED: `bake/terraincut.py` v18 projects each face in the plane it is most
# parallel to (triplanar UVs, audit §61), so the rationale above for holding the gain has
# expired. The value is still the last accepted look's: moving it to the anchor is a visual
# change on the whole terrain and waits for a look, not for another reason (validation pass
# 2026-08-31, E7). `tools/test_ground_albedo.py` still reports BeamNG's anchor and the gap.
GRASS_TARGET_LUMINANCE = 0.16

# What the measurement says this should be: the median linear luminance of BeamNG's own 26
# grass-named vanilla ground albedos. Kept beside the shipped value so the gap is visible in the
# source rather than only in an audit section.
MEASURED_GRASS_ANCHOR = 0.2185
MAX_GAIN = 6.0


NEUTRAL_MAPS = {  # every v1.5 terrain material must name all five maps at all three tiers
    "ao": 255,  # no occlusion
    "height": 128,  # flat mid height - the LAST RESORT: see _height_texture
    # a flat tangent-space normal, written RGB (128, 128, 255) = (0, 0, +1): the height
    # neutral (one channel, 128) read as a normal decodes to (0, 0, 0) - a zero-length
    # normal, not "up" - under a three-channel read, and nothing cites BeamNG reading two (B11)
    "normal": (128, 128, 255),
    "roughness": 217,  # 0.85: matte ground. HYPOTHESIS pending sweep area #4 - the ground
    # albedo's own alpha is a plausible smoothness (means 9-34/255,
    # correlating +0.31..+0.83 with the height), but nothing in the game's
    # source names that channel, so it is measured and NOT shipped
}

# How far a ground's height map is allowed to move a material boundary, and why that number.
#
# BeamNG blends the four layer texels around a pixel by a height blend, not by their bilinear
# weights alone (`terrain/terrainImpl.hlsl blendTerrainMaterials`):
#
#     height  = saturate(<the material's height map>) + 2 * matFactor[i]
#     lerpVal = calcHeightBlend(height, maxHeight, weights)      # heightRange = 0.5
#
# `matFactor` is the bilinear corner weight (0..1 across ONE layer texel, `calcBlendValues`), so
# two grounds tie where `2*(f1 - f2) == h2 - h1`: a height difference `dh` moves the boundary by
# `dh/2` texels. With our flat 128 for every ground, `dh` is identically 0 and the boundary is a
# straight bilinear ramp across a single 3.5 m sample - a hard, grid-aligned step. That is the
# "blacky transition between textures" and the checkerboard of load #19, and it is why every
# vanilla level ships real height maps. `TERRAIN_LAYER_WARP` is already `#define ... 1` in
# `terrain.h.hlsl`, so its +-0.5-texel domain warp is NOT the missing piece.
#
# The bundle carries the map already: `terrain/surfaces/<ground>_normal.png` ALPHA is the
# ground's height. Measured on the 2026-08-16 rip - its gradient reproduces the normal's own XY
# (corr(nx, -dh/du) = +0.36..+0.54, corr(ny, -dh/dv) the same with the sign flipped, the usual
# OpenGL/DirectX green convention) and it correlates +0.23..+0.59 with the albedo's luminance.
# CS2 consumes it as `_GrassDepthScale`/`_DirtDepthScale`/`_RockDepthScale` and
# `_TriplanarHeightmapBlending`; BeamNG packs it into the normal array's G and reads it back as
# `data0[*].y`.
#
# Sigma, and the two DELIBERATE DEVIATIONS on it, both declared because the two engines' height
# blends are different arithmetic and nothing in CS2 transfers:
#  1. Each ground is CENTRED on mid-gray, base tier included. CS2's absolute height levels differ
#     per ground by up to 0.39 (dirt 0.27, extra1 0.66), which in BeamNG's key would hand extra1
#     a permanent 0.2-texel land grab over dirt. The relief decides the SHAPE of the boundary;
#     the splat decides where it sits.
#  2. Each ground is scaled to the same sigma. From BeamNG's own numbers: `dh` of +-0.5 moves the
#     boundary by +-0.25 texel - a visible interlock that never reaches the neighbouring texel -
#     so +-2 sigma = 0.5 gives sigma = 0.125. The shipped maps sit at sigma 0.04 (extra4) to 0.20
#     (rock), i.e. between 3x too flat and 1.6x too strong for an equal say.
#
# The value is BeamNG's OWN, not that derivation. CS2 has no counterpart to port here, so the
# reference is the consumer engine's own art: 189 TerrainMaterials across the shipped vanilla
# levels carry a real height map, and their sigma runs min 0.000 / median 0.178 / mean 0.156 /
# max 0.297 (Utah's grounds are the top of the range). The derivation above lands at 0.125, just
# under that median, and where the two disagree the engine's own artists win - they authored
# against this exact blend. MEASURED, from `content/levels/*.zip`.
HEIGHT_SIGMA = 0.178

# The ground mesh's BASE slot is the albedo reduced to this many samples across `GROUND_UV_M`
# before being scaled back up — a low-pass that leaves only variation slower than the detail
# tier's own period, which is what the terrain's far tier is over the same footprint. See
# `_ground_mesh_maps`; 4 across 40 m keeps features coarser than 10 m, against a detail period of
# 14-68 m.
BASE_LOWPASS = 4

# ---- NOT PARITY. A deliberate deviation, kept apart from everything above. --------------------
#
# `terrain_blend_sharpen` multiplies HEIGHT_SIGMA. At 1.0 the relief is what BeamNG's own
# arithmetic derives and the terrain blends the way the game's does. Above 1.0 it is a WORKAROUND
# with no counterpart in CS2, and it exists because one defect has no faithful fix available:
#
# every softening mechanism in BeamNG's terrain shader is sized in LAYER TEXELS (`calcLayerWarp`
# warpAmp 0.5 at a 0.77-texel period; `calcHeightBlend` heightRange 0.5 giving a 0.245-texel soft
# band), vanilla levels run `squareSize` 0.3-0.75 m, and we run 1.75 m — so all of it is 2.3-5.8x
# oversize and renders as metre-scale blobs of whole ground. `TERRAIN_LAYER_WARP` is a `#define`,
# `heightRange` and `warpAmp` are shader literals, and the `.ter` layer array shares the
# heightmap's grid, so nothing but heightfield resolution reaches it — and `terrain_upsample` is
# already at its maximum. The faithful fix is a 16384² `.ter` (0.875 m, 768 MB against 192 MB).
#
# What this does instead: the relief displaces a boundary by sigma/sqrt(2) texels, at the height
# TEXTURE's period (14-68 m over a 1024 px map, i.e. per-pixel) rather than the layer texel's. Push
# sigma up and the boundary dissolves into fine interlock instead of reading as blobs — the blobs
# are still there, they are just below the size the eye reads as structure. It is cosmetic, it
# makes the blend rougher than the game's, and it is the only lever short of the .ter size.
# Measured in the §47 render: 0.125 -> 0.088 texel of displacement, 0.25 -> 0.167, 0.40 -> 0.28.
MAX_HEIGHT_SHARPEN = 4.0
# ----------------------------------------------------------------------------------------------


def _neutral_map(level, kind, tier="detail", size=1024):
    """A flat gray PNG for a map the bundle does not carry — ONE file per kind, named in every
    tier's slot. It used to be written once per tier as well: three byte-identical files, and
    three texture objects in VRAM, for one flat gray (refactor Phase 10 survey §6)."""
    del tier
    name = f"{level.name}_neutral_{kind}.png"
    value = NEUTRAL_MAPS[kind]
    if isinstance(value, tuple):
        data = png.encode_rgb(numpy.tile(numpy.asarray(value, dtype=numpy.uint8), (size, size, 1)))
    else:
        data = png.encode_gray(numpy.full((size, size), value, dtype=numpy.uint8), bit_depth=8)
    # written when the BYTES differ, not only when the file is absent: a neutral left by an
    # older build with another value or size was never corrected
    level.write_bytes_if_changed(data, "art", "terrains", name)
    return level.game_path("art", "terrains", name)


def _height_texture(level, src, name, sharpen=1.0):
    """The ground's own height map, from `<ground>_normal.png`'s alpha, as a TIER DEVIATION —
    the game path, or None when the source carries no height.

    See `HEIGHT_SIGMA` for what BeamNG does with this. Height rides the same additive overlay as
    the albedo (`calculateValue`, `MatDecodeNone`), so this is written at HALF the target sigma
    and the BASE tier is left at the neutral flat 128: the composite
    `0.5 + (2·(0.5 + σ/2) − 1)·1` then lands at exactly `0.5 ± HEIGHT_SIGMA` close up. Writing
    the full sigma into all three tiers, as this did before `_tier_deviation` existed, tripled it.
    """
    # `images.load_alpha` decodes; whether the file is there at all is this caller's question, and
    # `normal_src` is a path built by name — a ground with no normal map simply has no file.
    # `_rgb_texture` guards the same way for the same reason.
    if not os.path.isfile(src):
        return None
    alpha = images.load_alpha(src)
    if alpha is None:
        return None
    values = alpha.astype(numpy.float64) / 255.0
    sigma = float(values.std())
    if sigma < 1e-4:
        return None
    target = HEIGHT_SIGMA * 0.5 * min(MAX_HEIGHT_SHARPEN, max(0.0, float(sharpen)))
    scaled = (values - values.mean()) * (target / sigma) + 0.5
    grey = numpy.clip(scaled * 255.0 + 0.5, 0.0, 255.0).astype(numpy.uint8)
    level.write_bytes_if_changed(
        png.encode_gray(numpy.ascontiguousarray(grey), bit_depth=8), "art", "terrains", name
    )
    return level.game_path("art", "terrains", name)


def _linear_luminance(rgb8):
    lin = (rgb8.astype(numpy.float64) / 255.0) ** 2.2
    return float((0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).mean())


def albedo_gain(surfaces_dir, user_gain):
    """The linear gain applied to every ground albedo: calibrated on grass, times the user's."""
    path = os.path.join(surfaces_dir, "grass.png")
    gain = 1.0
    if os.path.isfile(path):
        # an unreadable grass albedo used to fall back to gain 1.0 in silence - the whole
        # terrain up to 6x darker with no log line (B10); the ground stage fails instead
        try:
            lum = _linear_luminance(images.load_rgb(path))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"terrain: the grass albedo {path} is unreadable ({exc})") from exc
        if lum > 1e-4:
            gain = min(MAX_GAIN, GRASS_TARGET_LUMINANCE / lum)
    return gain * float(user_gain or 1.0)


def _tier_deviation(rgb):
    """`0.5 + (albedo − base)/2` — the encoding that makes BeamNG's ADD reproduce CS2's LERP.

    CS2 cross-fades three tilings of a ground by camera distance; BeamNG adds two signed overlays
    to a base tier, `value = base + (2·t − 1)·f` (`terrain.h.hlsl calculateValue`, and the very
    same `blendDetail` in `defaultMat.hlsl` for an ordinary material's detail map). Those are the
    same operation if — and only if — the tier is written as half its deviation from the base:

        t = 0.5 + (T − base)/2   ⇒   base + (2t − 1)·f = base + (T − base)·f = lerp(base, T, f)

    so `f = 1` reproduces the near tiling EXACTLY and `f` in between is CS2's own cross-fade.

    The previous encoding centred each tier on its own mean (`T − mean + 0.5`), which is TWICE
    this deviation, and the tier strengths then multiplied it again. Measured on the shipped
    textures at close range: it inflated the albedo's contrast from σ 0.088 to σ 0.208 — 2.4× the
    real texture — and clamped **15.8 % of `extra2`'s pixels to zero in at least one channel**,
    8 % of `dirt` and 6.4 % of `rock` to one. Clamping one channel and not its neighbours is a
    hue shift, which is the green-black blotching in load #20's screenshots. This encoding clamps
    **0.0 %** of every ground and reproduces the near tiling to within the base tier's own
    large-scale variation (mean error 0.007–0.029).

    `base` is approximated by the image's mean: the base tier repeats every 240–410 m and a
    near-tier patch is 14–68 m, so over one patch the far tiling is a slowly-varying wash. That
    residual is what the remaining error is, and it is the large-scale colour variation the far
    tier exists to provide — DECLARED, not a defect."""
    values = rgb.astype(numpy.float64)
    mean = values.reshape(-1, 3).mean(axis=0)[None, None, :]
    return numpy.clip(0.5 * (values - mean) + 128.0, 0.0, 255.0).astype(numpy.uint8)


def _rgb_texture(level, src, name, gain=1.0, deviation=False):
    """Copy `src` into art/terrains/ as an alpha-free RGB PNG, optionally lifted by `gain` in
    linear light and encoded as a tier deviation (`_tier_deviation`); the game path, or None."""
    if not os.path.isfile(src):
        return None
    dst = level.path("art", "terrains", name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # An unreadable source used to be COPIED raw instead - alpha and all, which BeamNG's
    # terrain packer reads as coverage (load #2's purple/green), ungained, and for an overlay
    # tier at full contrast where the shader expects a signed deviation (load #25). A texture
    # in the wrong encoding is worse than a failed stage, so this raises (B10).
    try:
        rgb = images.load_rgb(src)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"terrain: {src} is unreadable ({exc})") from exc
    if abs(gain - 1.0) > 1e-3:
        lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
        rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
    if deviation:
        rgb = _tier_deviation(rgb)
    level.write_bytes_if_changed(png.encode_rgb(rgb), "art", "terrains", name)
    return level.game_path("art", "terrains", name)
