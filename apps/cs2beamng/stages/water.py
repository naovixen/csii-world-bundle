"""Stage `water` — §8 `water_level.png` as BeamNG water objects.

`water.bodies` gives connected wet regions on the game's own 8× (28 m) simulation grid. A body
whose level is one number is a lake; the reference city's sea+river is ONE body spanning
508–527 m, and a single flat plane over it would drown the valley floor or leave the sea dry —
so every body is split into LEVEL BANDS (`BAND_M` tall) and each 4-connected component of a
band becomes a `WaterBlock` sitting at the band's mean level, extending down past the deepest
ground under it. The one band that reaches the raster's edge with the largest area — the sea —
becomes a `WaterPlane` instead (infinite, so the horizon is water, not a cliff to nothing).

`WaterBlock`: `position` is the centre of the top face (the surface), `scale = [width, height,
depth]` and the block extends DOWN by `depth` (Torque's box is `(-.5,-.5,-1)..(.5,.5,0)`).
Appearance from `water.Appearance` (§8 `rasters.water`): base colour, fog density from the
absorption distance; the wave/foam/ripple tables are BeamNG's own defaults from `small_island`
because the bundle's shader-side wave parameters do not map onto them.

Budget: at most `MAX_BLOCKS` blocks (largest first); components under `MIN_CELLS` coarse
cells are dropped as noise. Region builds keep only bodies intersecting the region.
"""

import time

import numpy

from csiibundle import colour as colour_mod, png, reader, water as water_mod

from ..beamng import frame
from . import Stage, level_of

STAGE_KEY = "water"
BAND_M = 1.0
MIN_CELLS = 3
MAX_BLOCKS = 200
DOWNSAMPLE = 8
SEA_MIN_FRACTION = 0.02        # a band must cover this share of the coarse grid to be "the sea"

# BeamNG's own animation tables (small_island's Ocean1); the bundle has no equivalent
FOAM = [{"foamDir": [1, 0.5], "foamOpacity": 0.7, "foamSpeed": 0.002, "foamTexScale": [12, 12]},
        {"foamDir": [0.5, 1], "foamOpacity": 1.5, "foamSpeed": 0.03, "foamTexScale": [1.5, 1.5]}]
RIPPLES = [{"rippleDir": [-1, -1], "rippleMagnitude": 0.2, "rippleSpeed": 0.0025, "rippleTexScale": [200, 200]},
           {"rippleDir": [1, -1], "rippleMagnitude": 0.5, "rippleSpeed": 0.015, "rippleTexScale": [10, 10]},
           {"rippleDir": [-1, 1], "rippleMagnitude": 0.45, "rippleSpeed": 0.05, "rippleTexScale": [2, 2]}]
WAVES = [{"waveDir": [0, -1], "waveMagnitude": 0.8, "waveSpeed": 2},
         {"waveDir": [0.3, -1.2], "waveMagnitude": 0.5, "waveSpeed": -1.2},
         {"waveDir": [-0.3, -0.8], "waveMagnitude": 0.7, "waveSpeed": 2}]

# BeamNG's own reference wave amplitude for an open sea (small_island's Ocean1). The SHAPE of the
# swell stays BeamNG's — direction, speed and the three-band table have no counterpart in CS2's
# compiled water shader — but the AMPLITUDE no longer does: `WaterRenderSystem.ApplyMaterialParams`
# normalises exactly two wave quantities to 0..1 and hands them to its shader, and those two are
# portable (audit §56). So this is the reference the game's own multipliers scale:
#
#   sea   = REF x saturate(wavesMultiplier)      -> 1.300 saturates to 1.00, so 0.080 (unchanged)
#   lake  = REF x lakeWavesMultiplier            -> 0.130,                so 0.0104 (was 0.02)
#
# The lake number is the visible half: CS2 authors its lakes at a ninth of its sea and we were
# drawing them at a quarter. TUNED (the reference) / PORTED (both multipliers).
WAVE_MAGNITUDE_REF = 0.08


def enabled(settings):
    return bool(settings.get("water", True))


def units(census, settings):
    return 1


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    paths = reader.terrain_paths(ctx.bundle)
    if paths.rasters is None or not paths.water or not _isfile(paths.water) or not _isfile(paths.heightmap):
        progress.log("water: no water_level.png in the bundle; nothing to place")
        level.write_objects()
        return {"bodies": 0, "blocks": 0, "plane": False}
    rasters = paths.rasters
    cell = float(rasters["resolution"])
    scale = float(rasters.get("elevationScale", 1.0))
    offset = float(rasters.get("elevationOffset", 0.0))
    origin_x, _oy, origin_z = (float(v) for v in rasters["originWorld"])

    progress.begin("water: bodies", total=None, unit="")
    levels = png.gray_array(paths.water).astype(numpy.float64)
    levels = numpy.where(levels > 0, levels * scale + offset, 0.0)
    heights = png.gray_array(paths.heightmap).astype(numpy.float64) * scale + offset
    grid = water_mod.coarse_grid(levels, heights, DOWNSAMPLE)
    bodies = water_mod.bodies(levels, heights, cell, origin_x, origin_z, downsample=DOWNSAMPLE, min_cells=MIN_CELLS)
    progress.end(f"{len(bodies)} bodies")
    if grid is None or not bodies:
        level.write_objects()
        return {"bodies": 0, "blocks": 0, "plane": False}
    level_c, ground_c, wet = grid
    rows, cols = wet.shape
    coarse = cell * DOWNSAMPLE
    appearance = water_mod.load(ctx.bundle)

    # -- split every body into level bands, each band into components ------------------------
    progress.begin("water: bands", total=len(bodies), unit="bodies")
    pieces = []            # dicts: level, min_ground, cells, bbox rows/cols, touches_edge
    region = ctx.region
    for body in bodies:
        mask = body.mask
        band_of = numpy.rint(level_c / BAND_M).astype(numpy.int64)
        for band in numpy.unique(band_of[mask]):
            sub = mask & (band_of == band)
            labels, count = water_mod.label_components(sub)
            for k in range(1, count + 1):
                comp = labels == k
                n = int(comp.sum())
                if n < MIN_CELLS:
                    continue
                rr, cc = numpy.nonzero(comp)
                lv = float(level_c[comp].mean())
                min_ground = float(ground_c[comp].min())
                touches = bool(rr.min() == 0 or cc.min() == 0 or rr.max() == rows - 1 or cc.max() == cols - 1)
                # bundle-frame bbox (x east, z north); coarse cell (r, c) covers texels r*d..(r+1)*d
                min_x = origin_x + cc.min() * coarse - 0.5 * cell
                max_x = origin_x + (cc.max() + 1) * coarse - 0.5 * cell
                max_z = origin_z - rr.min() * coarse + 0.5 * cell
                min_z = origin_z - (rr.max() + 1) * coarse + 0.5 * cell
                if region is not None and not _intersects(region, min_x, min_z, max_x, max_z):
                    continue
                pieces.append({"level": lv, "minGround": min_ground, "cells": n, "touches": touches,
                               "bbox": (min_x, min_z, max_x, max_z)})
        progress.advance(1)
        progress.check()
    progress.end(f"{len(pieces)} level-band pieces")

    # -- the sea: the largest edge-touching piece over the size threshold -> WaterPlane --------
    sea = None
    candidates = [p for p in pieces if p["touches"] and p["cells"] >= SEA_MIN_FRACTION * rows * cols]
    if candidates and region is None:
        sea = max(candidates, key=lambda p: p["cells"])
    blocks = [p for p in pieces if p is not sea]
    blocks.sort(key=lambda p: -p["cells"])
    dropped = max(0, len(blocks) - MAX_BLOCKS)
    blocks = blocks[:MAX_BLOCKS]

    # -- objects ---------------------------------------------------------------------------
    progress.begin("water: objects", total=len(blocks) + (1 if sea else 0), unit="objects")
    common = _appearance_fields(appearance)
    if sea is not None:
        obj = {"name": "sea", "class": "WaterPlane", "position": [0.0, 0.0, round(sea["level"], 3)],
               "gridElementSize": 8.0, "gridSize": 400, "Foam": FOAM, "Ripples (texture animation)": RIPPLES,
               "Waves (vertex undulation)": WAVES,
               "overallWaveMagnitude": round(WAVE_MAGNITUDE_REF * appearance.sea_wave_scale, 5),
               "overallRippleMagnitude": 1, "overallFoamOpacity": 1.2}
        obj.update(common)
        level.add(["Level_objects", "water"], obj, key="water:sea")
        progress.advance(1)
    for i, p in enumerate(blocks):
        min_x, min_z, max_x, max_z = p["bbox"]
        cx, cz = 0.5 * (min_x + max_x), 0.5 * (min_z + max_z)
        depth = max(1.0, p["level"] - p["minGround"] + 1.0)
        bx, by, bz = frame.to_beamng(cx, p["level"], cz)
        obj = {"name": f"water_{i:03d}", "class": "WaterBlock", "position": [round(bx, 3), round(by, 3), round(bz, 3)],
               "rotationMatrix": frame.identity_matrix(),
               "scale": [round(max_x - min_x, 2), round(max_z - min_z, 2), round(depth, 2)],
               "Foam": FOAM[:1], "Ripples (texture animation)": RIPPLES, "Waves (vertex undulation)": WAVES,
               "overallWaveMagnitude": round(WAVE_MAGNITUDE_REF * appearance.lake_wave_scale, 5),
               "overallRippleMagnitude": 0.5, "overallFoamOpacity": 0.6}
        obj.update(common)
        level.add(["Level_objects", "water"], obj, key=f"water:block:{i}")
        progress.advance(1)
        if i % 32 == 0:
            progress.check()
    level.write_objects()
    progress.end(f"{'1 WaterPlane + ' if sea else ''}{len(blocks)} WaterBlocks" + (f", {dropped} small pieces dropped" if dropped else ""))
    return {"bodies": len(bodies), "pieces": len(pieces), "blocks": len(blocks), "plane": sea is not None,
            "seaLevel": None if sea is None else round(sea["level"], 2), "dropped": dropped,
            "seaWaveScale": round(appearance.sea_wave_scale, 4),
            "lakeWaveScale": round(appearance.lake_wave_scale, 4),
            # APPROXIMATED unless the game says the wind branch is dead: with
            # `seaWindAffectWavesDirection` true the wave direction and chaos follow live wind, so
            # a fixed direction table reproduces one moment rather than the game (audit §58.1).
            "waveDirection": "exact (game passes no wind)" if appearance.wave_direction_is_exact
                             else "APPROXIMATED (wind-driven in game; fixed here)",
            "seconds": round(time.monotonic() - started, 1)}


def _isfile(path):
    import os
    return bool(path) and os.path.isfile(path)


def _intersects(region, min_x, min_z, max_x, max_z):
    r = float(region["radius"])
    cx, cz = float(region["centerX"]), float(region["centerZ"])
    return not (max_x < cx - r or min_x > cx + r or max_z < cz - r or min_z > cz + r)


def _appearance_fields(appearance):
    """WaterObject fields from the bundle's water appearance (§8 `rasters.water`)."""
    base = appearance.base_color
    srgb = [int(round(255 * min(1.0, max(0.0, colour_mod.linear_to_srgb(float(c)))))) for c in base[:3]]
    alpha = int(round(255 * float(base[3]))) if len(base) > 3 else 255
    under = appearance.scattering_color
    under_srgb = [int(round(255 * min(1.0, max(0.0, colour_mod.linear_to_srgb(float(c)))))) for c in under[:3]]
    fog = 1.0 / max(appearance.absorption_distance, 1.0)
    return {"baseColor": srgb + [max(alpha, 200)], "underwaterColor": under_srgb + [253],
            "waterFogDensity": round(fog, 4), "waterFogDensityOffset": 0.1,
            "clarity": 0.15, "fresnelBias": 0.1, "fresnelPower": 2.5, "fullReflect": False,
            "reflectivity": 0.3, "specularPower": 200, "depthGradientMax": 50,
            "distortStartDist": 0.1, "distortEndDist": 20, "distortFullDepth": 3.5,
            "foamAmbientLerp": 1, "foamMaxDepth": 0.7, "foamRippleInfluence": 0.005,
            "foamTex": "core/art/water/foam.dds", "rippleTex": "core/art/water/ripple.dds",
            "wetDarkening": 0.3, "wetDepth": 0.5, "viscosity": 0.001, "density": 1}


INPUTS = ("layer:water", "layer:terrain", "layer:terrainFine")
SETTINGS = ("water", "terrain_upsample")

STAGE = Stage(STAGE_KEY, "Water", enabled, units, run, inputs=INPUTS, settings_keys=SETTINGS)
