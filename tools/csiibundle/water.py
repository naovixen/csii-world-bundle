"""Water (§8 `rasters.water`) — the ocean, the lakes, and every swimming pool.

**Why pools render white.** CS2 draws water with a shader, not a texture. A pool's water surface
is its own material carrying `template:WaterGeneric` and **no maps at all** — the bundle cannot
export a compiled shader, so there is nothing for it to reference. A glTF loader meeting a
material with no `baseColorTexture` falls back to `baseColorFactor`, which the producer writes
as `[1, 1, 1, 1]`. The result is a pool full of flat white paint, and the same happens to every
fountain and stream. 48 materials on the reference city are `WaterGeneric` and every one of them
is textureless.

The appearance those materials need is shipped — just somewhere else. `rasters.water` carries
`WaterRenderSettingsPrefab` verbatim: colour, scattering, absorption, smoothness, foam, caustics
and waves. It is the same authored settings the OCEAN uses, because in the game it is the same
shader. So one block drives both, and `Appearance` below is what a consumer applies to a lake
surface and to a swimming pool alike.

**What is exact and what is modelled.** The parameters are the game's own, read verbatim; the
mapping onto a physically-based renderer is not, because CS2's water shader is compiled inside
an asset bundle and cannot be read. So:

* `base_color`, `scattering_color`, `absorption_distance`, the smoothness pair — **exact**,
  straight from the prefab.
* `endSmoothness = min(smoothnessDistant, smoothnessClose)` — **exact**.
* `roughness = 1 - smoothness` — the §7 convention, consistent with every other material.
* IOR 1.333 — water's, not the game's; CS2 does not ship one.
* Turning `absorption_distance` into a volume density is **modelled**: Beer-Lambert with the
  shipped distance as the depth at which transmission falls to 1/e. A consumer with a real
  volumetric water shader should use the distance directly instead.

**It is NOT the game's only bind-time transform, and this file used to say it was** (corrected
2026-08-17 from `WaterRenderSystem.ApplyMaterialParams`, audit §56). Four more exist, and every
one is camera- or wind-dependent — which is exactly why they cannot be shipped as values, and why
a consumer has to be told which of these numbers reaches a shader unchanged:

1. **`wavesMultiplier` decays with camera height.** `t = saturate((cameraHeight − seaLevel − 750)
   / 250)`, and when the authored value exceeds 1 the game lerps it toward 1 by `t`. Above 1000 m
   over the sea it is exactly 1.0.
2. **The wave bands take `saturate(wavesMultiplier)`.** `largeBand0Multiplier` and
   `largeBand1Multiplier` are both the saturated value, so the reference city's authored **1.3
   never reaches a band amplitude at all** — it reads 1.0 there.
3. **`foamAmount` is scaled by a camera-height fade** over `foamFadeRange`.
4. **The wind terms are live**: `largeWindSpeed = lerp(0, distanceWindSpeed, wavesMultiplier²)`
   and `largeChaos` comes from the current wind strength, both gated by
   `seaWindAffectWavesDirection`. When that flag is false the game passes 0 for both, and calm
   water is then the game exactly rather than an approximation of it.

**Read the flag; do not assume it.** On the reference city (2026-08-17 rip) it is **true**, so the
wind branch IS live there and calm water is an approximation of one windy moment — not a faithful
replay. It was measured the day it started shipping, which is the only reason this paragraph does
not still say the comfortable thing.

A consumer replaying a static scene should take the **ground-level calm** case: bands at
`saturate(wavesMultiplier)`, foam at the full `foamAmount`, no wind terms — and, where the flag is
true, declare that as the approximation it is.

**`seaLevel` is a datum, not a surface, and a consumer must never seat water on it.** It is what
backdrop ocean is reset to and the plane the renderer measures camera depth against for
`smoothnessRange`. On the reference bundle it reads 15.0 while the mapped water stands at
508.6–528.3 m. **`terrain/water_level.png` is the authoritative surface.**

**Smoothness varies with camera distance** (`smoothnessRange` = `[start, distance]` metres), which
is an anti-aliasing measure for a rippled surface seen far away, not an appearance choice. A
consumer with no distance-varying material should use the CLOSE value and accept sparkle at
range; `roughness_at()` is there for one that can.
"""

import math

import numpy

from . import reader

# Water's index of refraction. NOT from the game — CS2 ships no IOR — but it is a physical
# constant, not a taste. TUNED only in the sense that a consumer may prefer its own.
WATER_IOR = 1.333

# Fallbacks for a bundle with no `rasters.water` block, so a consumer degrades to plausible water
# rather than to white. TUNED.
FALLBACK_COLOR = (0.08, 0.22, 0.35, 1.0)
FALLBACK_SMOOTHNESS = 0.95


class Appearance:
    """`rasters.water` as constants a renderer can apply. See the module docstring for the
    exact/modelled split."""

    def __init__(self, block):
        block = block or {}
        self.raw = block
        self.present = bool(block)
        self.prefab = block.get("prefab")

        self.base_color = tuple(block.get("waterColor") or FALLBACK_COLOR)[:4]
        self.scattering_color = tuple(block.get("scatteringColor") or self.base_color)[:4]
        self.absorption_distance = float(block.get("absorptionDistance", 16.0) or 16.0)
        self.max_refraction_distance = float(block.get("maxRefractionDistance", 3.0) or 3.0)

        self.smoothness_close = float(
            block.get("smoothnessClose", FALLBACK_SMOOTHNESS) or FALLBACK_SMOOTHNESS
        )
        self.smoothness_distant = float(
            block.get("smoothnessDistant", self.smoothness_close) or self.smoothness_close
        )
        # The game's one bind-time transform, from `WaterRenderSystem.ApplyMaterialParams`.
        self.end_smoothness = min(self.smoothness_distant, self.smoothness_close)
        rng = block.get("smoothnessRange") or (0.0, 0.0)
        self.smoothness_range = (float(rng[0]), float(rng[1]))

        self.foam_amount = float(block.get("foamAmount", 0.0) or 0.0)
        self.caustics_intensity = float(block.get("causticsIntensity", 0.0) or 0.0)
        self.waves_multiplier = float(block.get("wavesMultiplier", 0.0) or 0.0)
        self.lake_waves_multiplier = float(block.get("lakeWavesMultiplier", 0.0) or 0.0)

        # Whether the wind branch of `ApplyMaterialParams` is live at all (audit §56/§58.1).
        # `None` on a bundle ripped before the field shipped — which is NOT the same as false, so
        # `wave_direction_is_exact` reports the difference rather than assuming calm.
        raw = block.get("seaWindAffectWavesDirection")
        self.sea_wind_affects_waves = None if raw is None else bool(raw)

        # The sea DATUM. Not a surface. See the module docstring before using it for anything.
        self.sea_level = block.get("seaLevel")

    # -- the two wave quantities that ARE portable ---------------------------------------------

    @property
    def sea_wave_scale(self):
        """The sea's wave-band amplitude, 0..1 — the game's own `saturate(wavesMultiplier)`.

        `ApplyMaterialParams` sets `largeBand0Multiplier = largeBand1Multiplier =
        saturate(num3)`, so an authored value above 1 never reaches a band: the reference city's
        1.3 reads **1.0** here. This and `lake_wave_scale` are the only wave numbers the game
        normalises to 0..1, which is what makes them portable at all — every other term in that
        method is a live camera or wind quantity (audit §56).
        """
        return max(0.0, min(1.0, self.waves_multiplier))

    @property
    def lake_wave_scale(self):
        """A lake's wave amplitude, 0..1 — `_LakeWavesMultiplier`, bound verbatim.

        Authored 0.13 on the reference city, i.e. lakes are nearly still where the sea is not.
        """
        return max(0.0, min(1.0, self.lake_waves_multiplier))

    @property
    def wave_direction_is_exact(self):
        """Whether a consumer's FIXED wave direction reproduces the game, or approximates it.

        `m_SeaWindAffectWavesDirection` false ⇒ the game passes 0 for both the wind orientation
        and the wind strength, so a fixed direction IS the game. True ⇒ direction and chaos
        follow live wind and any static choice is an approximation of one moment. `None` ⇒ the
        bundle predates the field and a consumer cannot tell; treat as approximate and say so.
        """
        return self.sea_wind_affects_waves is False

    # -- what a PBR renderer needs ------------------------------------------------------------

    @property
    def roughness(self):
        """Roughness at the camera-close end — `1 - smoothnessClose`. The value to use when the
        renderer has no distance-varying material."""
        return 1.0 - self.smoothness_close

    @property
    def roughness_distant(self):
        return 1.0 - self.end_smoothness

    def roughness_at(self, camera_distance):
        """Roughness at a given camera distance, ramping across `smoothness_range`.

        `smoothnessRange` is `[start, distance]`: the ramp begins at `start` metres and takes
        `distance` metres to complete.
        """
        start, span = self.smoothness_range
        if span <= 0.0:
            return self.roughness
        t = min(max((camera_distance - start) / span, 0.0), 1.0)
        smoothness = self.smoothness_close + (self.end_smoothness - self.smoothness_close) * t
        return 1.0 - smoothness

    def volume_density(self):
        """Beer-Lambert extinction per metre, from `absorption_distance`. MODELLED.

        The shipped distance is treated as the depth at which transmission falls to `1/e`, so
        density is its reciprocal. A renderer with a real absorption-distance input should take
        `absorption_distance` directly rather than this.
        """
        return 1.0 / max(self.absorption_distance, 1e-6)

    def surface_alpha(self, depth=None):
        """Opacity for a renderer with no volumetrics, from Beer-Lambert over `depth` metres.

        With no depth given it uses `max_refraction_distance`, which is how far the game lets a
        refracted ray travel — the closest thing the prefab has to "how deep does this look".
        A flat alpha is a poor model of water and this says so rather than pretending: prefer
        transmission plus `volume_density()` where the renderer supports it.
        """
        d = self.max_refraction_distance if depth is None else depth
        return min(1.0, 1.0 - math.exp(-self.volume_density() * max(d, 0.0)))


def load(bundle_dir):
    """This bundle's water appearance. Never `None` — an absent block yields the fallbacks, so a
    consumer always has something better than white."""
    manifest = reader.load_manifest(bundle_dir) or {}
    rasters = manifest.get("rasters") or {}
    return Appearance(rasters.get("water"))


# -- water bodies: connected wet regions with a level, for an engine that wants planes ---------


class Body:
    """One connected water body on the downsampled grid.

    `level_mean/min/max` metres; `area_m2`; `bbox` `(min_x, min_z, max_x, max_z)` in bundle
    metres (Z north); `cells` count on the coarse grid; `touches_edge` when the body reaches the
    raster's border (the sea rather than a lake); `mask` the coarse (rows, cols) bool array.
    """

    __slots__ = (
        "area_m2",
        "bbox",
        "cells",
        "level_max",
        "level_mean",
        "level_min",
        "mask",
        "touches_edge",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return (
            f"Body(level={self.level_mean:.2f} [{self.level_min:.2f}..{self.level_max:.2f}], "
            f"area={self.area_m2:.0f} m2, cells={self.cells}, edge={self.touches_edge})"
        )


def bodies(levels, heights, cell, origin_x, origin_z, downsample=8, min_cells=2, grid=None):
    """Connected water bodies from the §8 level and height rasters.

    `levels` and `heights` are `(rows, cols)` world-metre arrays (row 0 north); `levels` is 0
    where there is no water data. Water exists where `level > ground`, decided per texel. Both
    are reduced by `downsample` (the game simulates water on a coarser grid than the heightmap —
    28 m against 3.5, i.e. 8 — so finer detail is resampling, not information): a coarse cell is
    wet when a quarter of its texels are, and its level/ground are the means over those wet
    texels (see `coarse_grid`). The wet mask is labelled by 4-connectivity, and each component
    becomes a `Body` with its level statistics.
    Bodies of fewer than `min_cells` coarse cells are dropped (a single wet texel is noise).

    A heightmap-native engine turns each body into a water plane or block at `level_mean`;
    a body whose `level_max − level_min` is large (a river descending its valley) wants
    splitting into level bands or a spline river — the caller's choice, the numbers are here.
    """
    lv = numpy.asarray(levels, dtype=numpy.float64)
    hg = numpy.asarray(heights, dtype=numpy.float64)
    if lv.shape != hg.shape:
        raise ValueError(f"levels {lv.shape} and heights {hg.shape} differ")
    d = max(1, int(downsample))
    grid = coarse_grid(lv, hg, d)
    if grid is None:
        return []
    level_c, _hg_c, wet = grid
    rows, cols = wet.shape
    labels, count = _label4(wet)
    out = []
    coarse_cell = cell * d
    for k in range(1, count + 1):
        m = labels == k
        n = int(m.sum())
        if n < min_cells:
            continue
        rr, cc = numpy.nonzero(m)
        lvls = level_c[m]
        touches = bool(
            rr.min() == 0 or cc.min() == 0 or rr.max() == rows - 1 or cc.max() == cols - 1
        )
        min_x = origin_x + cc.min() * coarse_cell - 0.5 * cell
        max_x = origin_x + (cc.max() + 1) * coarse_cell - 0.5 * cell
        max_z = origin_z - rr.min() * coarse_cell + 0.5 * cell
        min_z = origin_z - (rr.max() + 1) * coarse_cell + 0.5 * cell
        out.append(
            Body(
                level_mean=float(lvls.mean()),
                level_min=float(lvls.min()),
                level_max=float(lvls.max()),
                area_m2=float(n * coarse_cell * coarse_cell),
                bbox=(float(min_x), float(min_z), float(max_x), float(max_z)),
                cells=n,
                touches_edge=touches,
                mask=m,
            )
        )
    out.sort(key=lambda b: -b.area_m2)
    return out


def coarse_grid(levels, heights, downsample=8):
    """`(level_c, ground_c, wet)` on the block-mean grid `bodies` works on, or None when the
    rasters are smaller than one block. Public so a consumer can split a body further (by level
    band, say) on exactly the grid the bodies were found on."""
    lv = numpy.asarray(levels, dtype=numpy.float64)
    hg = numpy.asarray(heights, dtype=numpy.float64)
    d = max(1, int(downsample))
    rows, cols = lv.shape[0] // d, lv.shape[1] // d
    if rows == 0 or cols == 0:
        return None
    lv_c = lv[: rows * d, : cols * d].reshape(rows, d, cols, d)
    hg_b = hg[: rows * d, : cols * d].reshape(rows, d, cols, d)
    # wet is decided per TEXEL (level > ground) and a coarse cell is wet when a quarter of its
    # texels are; level and ground are then averaged over the wet texels only. Averaging land
    # into a shoreline cell's ground made every river narrower than a few cells read as dry.
    wet_t = (lv_c > 0) & (lv_c > hg_b)
    wet_n = wet_t.sum(axis=(1, 3))
    wet = wet_n >= max(1, (d * d) // 4)
    denom = numpy.maximum(wet_n, 1)
    level_c = numpy.where(wet_n > 0, numpy.where(wet_t, lv_c, 0.0).sum(axis=(1, 3)) / denom, 0.0)
    hg_c = numpy.where(
        wet_n > 0, numpy.where(wet_t, hg_b, 0.0).sum(axis=(1, 3)) / denom, hg_b.mean(axis=(1, 3))
    )
    return level_c, hg_c, wet


def label_components(mask):
    """Public name for the 4-connected labeller: `(labels, count)`, 0 = background."""
    return _label4(mask)


def _label4(mask):
    """Connected-component labelling (4-neighbour) with numpy only: iterative label
    propagation over the wet cells. Returns `(labels int32 (rows, cols), count)`; 0 = dry."""
    mask = numpy.asarray(mask, dtype=bool)
    rows, cols = mask.shape
    labels = numpy.zeros((rows, cols), dtype=numpy.int64)
    idx = numpy.flatnonzero(mask)
    if idx.size == 0:
        return labels.astype(numpy.int32), 0
    labels.reshape(-1)[idx] = idx + 1  # each wet cell starts as its own label (index+1)
    # propagate the MIN label across 4-neighbours until nothing changes; each pass halves the
    # remaining disagreement along a chain, and real water bodies are compact, so it converges
    # in tens of passes on a 512² grid.
    while True:
        prev = labels.copy()
        for shift in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            neighbour = numpy.roll(labels, shift, axis=(0, 1))
            if shift == (0, 1):
                neighbour[:, 0] = 0
            elif shift == (0, -1):
                neighbour[:, -1] = 0
            elif shift == (1, 0):
                neighbour[0, :] = 0
            else:
                neighbour[-1, :] = 0
            better = mask & (neighbour > 0) & ((neighbour < labels) | (labels == 0))
            labels = numpy.where(better, neighbour, labels)
        if numpy.array_equal(prev, labels):
            break
    # compact labels to 1..count
    uniq, inv = numpy.unique(labels[mask], return_inverse=True)
    compact = numpy.zeros((rows, cols), dtype=numpy.int32)
    compact[mask] = (inv + 1).astype(numpy.int32)
    return compact, len(uniq)
