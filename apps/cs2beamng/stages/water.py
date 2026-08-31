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
from csiibundle import colourspace, png, reader
from csiibundle import region as region_rule
from csiibundle import water as water_mod

from ..beamng import frame
from . import Stage, level_of

STAGE_KEY = "water"
BAND_M = 1.0
MIN_CELLS = 3
# how far down the depth-to-colour ramp runs (`depthGradientMax`, and the span `_depth_ramp`
# attenuates over)
DEPTH_GRADIENT_MAX_M = 50.0
# An inland body whose surface sits within this of the SEA's own level is the producer's level
# smear, not water: the rip re-bases the sim's coarse depth onto a point sample of the fine
# ground and dilates, which floats the sea's level over low land beside deep water (docklands).
# Standing water disconnected from the sea cannot hold the sea's level - the sim's own
# conservation - so those bodies are dropped, counted and logged. A pond or lake at its OWN
# level is untouched. Removed when the producer's re-based rip lands (TerrainRipSystem.Water).
PHANTOM_LEVEL_EPS_M = 2.0
# Water shallower than this never renders in the game (the shader fades by depth), so it does
# not become an opaque block here. TUNED - the exact fade is in the compiled shader. Inert on a
# pre-fix bundle (the smeared depths are metres); the finishing guard for the producer's
# re-based rip, whose honest levels leave millimetre films where the sim had surface moisture.
MIN_RENDER_DEPTH_M = 0.15
# A band component becomes one axis-aligned box, and a box that is mostly dry covers things -
# the dock basin's rectangle lay across the quay road (Naomi, 2026-08-31). The refinement is
# BUDGETED (`_refine_worst_boxes`): every component is collected first, then the box covering
# the most dry cells is bisected, again and again, while the total stays inside `MAX_BLOCKS` -
# so the dock class tightens and a winding river, whose box is mostly dry BY NATURE (a
# fill-ratio or absolute-overhang rule shatters every one: 746 and 612 pieces against the
# budget on the first two cuts, with real slivers dropped to pay for it), keeps its one piece
# unless it is among the worst.
BLOCK_SPLIT_MIN_OVERHANG_CELLS = 100  # a box covering less dry area than this is never split
MAX_BLOCKS = (
    200  # the vanilla-calibrated budget (`levelcheck/residency`); the refinement stays inside
)
DOWNSAMPLE = 8
SEA_MIN_FRACTION = 0.02  # a band must cover this share of the coarse grid to be "the sea"

# BeamNG's own animation tables (small_island's Ocean1); the bundle has no equivalent
FOAM = [
    {"foamDir": [1, 0.5], "foamOpacity": 0.7, "foamSpeed": 0.002, "foamTexScale": [12, 12]},
    {"foamDir": [0.5, 1], "foamOpacity": 1.5, "foamSpeed": 0.03, "foamTexScale": [1.5, 1.5]},
]
RIPPLES = [
    {
        "rippleDir": [-1, -1],
        "rippleMagnitude": 0.2,
        "rippleSpeed": 0.0025,
        "rippleTexScale": [200, 200],
    },
    {
        "rippleDir": [1, -1],
        "rippleMagnitude": 0.5,
        "rippleSpeed": 0.015,
        "rippleTexScale": [10, 10],
    },
    {"rippleDir": [-1, 1], "rippleMagnitude": 0.45, "rippleSpeed": 0.05, "rippleTexScale": [2, 2]},
]
WAVES = [
    {"waveDir": [0, -1], "waveMagnitude": 0.8, "waveSpeed": 2},
    {"waveDir": [0.3, -1.2], "waveMagnitude": 0.5, "waveSpeed": -1.2},
    {"waveDir": [-0.3, -0.8], "waveMagnitude": 0.7, "waveSpeed": 2},
]

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
    if (
        paths.rasters is None
        or not paths.water
        or not _isfile(paths.water)
        or not _isfile(paths.heightmap)
    ):
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
    levels, fine_stats = _drop_enclosed_water(levels, heights, progress)
    grid = water_mod.coarse_grid(levels, heights, DOWNSAMPLE)
    bodies = water_mod.bodies(
        levels,
        heights,
        cell,
        origin_x,
        origin_z,
        downsample=DOWNSAMPLE,
        min_cells=MIN_CELLS,
        grid=grid,  # built once; `bodies` used to build the same 134 MB grid again (D3)
    )
    progress.end(f"{len(bodies)} bodies")
    if grid is None or not bodies:
        level.write_objects()
        return {"bodies": 0, "blocks": 0, "plane": False}
    level_c, ground_c, wet = grid
    rows, cols = wet.shape
    bodies, phantom = _drop_phantom_bodies(bodies, level_c, rows, cols)
    for note in phantom["notes"][:5]:
        progress.log("water: " + note)
    coarse = cell * DOWNSAMPLE
    appearance = water_mod.load(ctx.bundle)

    # -- split every body into level bands, each band into components ------------------------
    progress.begin("water: bands", total=len(bodies), unit="bodies")
    pieces = []  # dicts: level, min_ground, cells, bbox rows/cols, touches_edge
    region = ctx.region
    band_of = numpy.rint(level_c / BAND_M).astype(numpy.int64)  # hoisted: loop-invariant (D3)
    components = []
    for body in bodies:
        mask = body.mask
        for band in numpy.unique(band_of[mask]):
            sub = mask & (band_of == band)
            labels, count = water_mod.label_components(sub)
            for k in range(1, count + 1):
                rr, cc = numpy.nonzero(labels == k)
                if len(rr) >= MIN_CELLS:
                    components.append((rr, cc))
        progress.advance(1)
        progress.check()
    for rr, cc in _refine_worst_boxes(components):
        if len(rr) < MIN_CELLS:
            continue
        lv = float(level_c[rr, cc].mean())
        min_ground = float(ground_c[rr, cc].min())
        touches = bool(
            rr.min() == 0 or cc.min() == 0 or rr.max() == rows - 1 or cc.max() == cols - 1
        )
        # bundle-frame bbox (x east, z north); coarse cell (r, c) covers r*d..(r+1)*d
        min_x = origin_x + cc.min() * coarse - 0.5 * cell
        max_x = origin_x + (cc.max() + 1) * coarse - 0.5 * cell
        max_z = origin_z - rr.min() * coarse + 0.5 * cell
        min_z = origin_z - (rr.max() + 1) * coarse + 0.5 * cell
        if region is not None and not _intersects(region, min_x, min_z, max_x, max_z):
            continue
        pieces.append(
            {
                "level": lv,
                "minGround": min_ground,
                "cells": len(rr),
                "touches": touches,
                "bbox": (min_x, min_z, max_x, max_z),
            }
        )
    progress.end(f"{len(pieces)} level-band pieces")

    # -- the sea: the largest edge-touching piece over the size threshold -> WaterPlane --------
    sea = None
    candidates = [
        p for p in pieces if p["touches"] and p["cells"] >= SEA_MIN_FRACTION * rows * cols
    ]
    if candidates and region is None:
        sea = max(candidates, key=lambda p: p["cells"])
    blocks = [p for p in pieces if p is not sea]
    blocks.sort(key=lambda p: -p["cells"])
    dropped = max(0, len(blocks) - MAX_BLOCKS)
    blocks = blocks[:MAX_BLOCKS]

    # -- objects ---------------------------------------------------------------------------
    progress.begin("water: objects", total=len(blocks) + (1 if sea else 0), unit="objects")
    common = _appearance_fields(appearance, ramp_path=_depth_ramp(level, appearance))
    if sea is not None:
        obj = {
            "name": "sea",
            "class": "WaterPlane",
            "position": [0.0, 0.0, round(sea["level"], 3)],
            "gridElementSize": 8.0,
            "gridSize": 400,
            "Foam": FOAM,
            "Ripples (texture animation)": RIPPLES,
            "Waves (vertex undulation)": WAVES,
            "overallWaveMagnitude": round(WAVE_MAGNITUDE_REF * appearance.sea_wave_scale, 5),
            "overallRippleMagnitude": 1,
            "overallFoamOpacity": 1.2,
        }
        obj.update(common)
        level.add(["Level_objects", "water"], obj, key="water:sea")
        progress.advance(1)
    for i, p in enumerate(blocks):
        min_x, min_z, max_x, max_z = p["bbox"]
        cx, cz = 0.5 * (min_x + max_x), 0.5 * (min_z + max_z)
        depth = max(1.0, p["level"] - p["minGround"] + 1.0)
        bx, by, bz = frame.to_beamng(cx, p["level"], cz)
        obj = {
            "name": f"water_{i:03d}",
            "class": "WaterBlock",
            "position": [round(bx, 3), round(by, 3), round(bz, 3)],
            "rotationMatrix": frame.identity_matrix(),
            "scale": [round(max_x - min_x, 2), round(max_z - min_z, 2), round(depth, 2)],
            "Foam": FOAM[:1],
            "Ripples (texture animation)": RIPPLES,
            "Waves (vertex undulation)": WAVES,
            "overallWaveMagnitude": round(WAVE_MAGNITUDE_REF * appearance.lake_wave_scale, 5),
            "overallRippleMagnitude": 0.5,
            "overallFoamOpacity": 0.6,
        }
        obj.update(common)
        level.add(["Level_objects", "water"], obj, key=f"water:block:{i}")
        progress.advance(1)
        if i % 32 == 0:
            progress.check()
    level.write_objects()
    progress.end(
        f"{'1 WaterPlane + ' if sea else ''}{len(blocks)} WaterBlocks"
        + (f", {dropped} small pieces dropped" if dropped else "")
    )
    return {
        "bodies": len(bodies),
        "pieces": len(pieces),
        "blocks": len(blocks),
        "plane": sea is not None,
        "seaLevel": None if sea is None else round(sea["level"], 2),
        "dropped": dropped,
        # the producer's level smear over low land beside deep water, dropped by the sea-level
        # signature (`PHANTOM_LEVEL_EPS_M`); the real cure is the re-based rip
        "phantomBodiesDropped": phantom["count"],
        "phantomCells": phantom["cells"],
        "enclosedTexelsDropped": fine_stats["enclosedTexels"],
        "filmTexelsDropped": fine_stats["filmTexels"],
        "seaWaveScale": round(appearance.sea_wave_scale, 4),
        "lakeWaveScale": round(appearance.lake_wave_scale, 4),
        # APPROXIMATED unless the game says the wind branch is dead: with
        # `seaWindAffectWavesDirection` true the wave direction and chaos follow live wind, so
        # a fixed direction table reproduces one moment rather than the game (audit §58.1).
        "waveDirection": "exact (game passes no wind)"
        if appearance.wave_direction_is_exact
        else "APPROXIMATED (wind-driven in game; fixed here)",
        "seconds": round(time.monotonic() - started, 1),
    }


def _drop_enclosed_water(levels, heights, progress):
    """Hydraulic sanity at the FINE resolution, where quay walls and roads exist as barriers.

    Sea-level water that is ENCLOSED - no wet path to open water at the raster's own
    resolution - cannot stand there; it is the producer's level smear over low land
    (`PHANTOM_LEVEL_EPS_M` tells the story). Only ~0.5 km2 on the reference city, but it is
    the 0.5 km2 under people's houses; the smear that stays CONNECTED (it rides over quays
    that sit below the overshot level) is not separable consumer-side and is what the
    producer's re-based rip removes. Also drops films under `MIN_RENDER_DEPTH_M`, which the
    game's own depth fade never shows."""
    stats = {"enclosedTexels": 0, "filmTexels": 0}
    wet = levels > heights
    if wet.any():
        try:
            from scipy import ndimage
        except ImportError:
            progress.log("water: scipy not installed - enclosed-water check skipped")
        else:
            labels, _count = ndimage.label(wet, structure=numpy.ones((3, 3), dtype=bool))
            edge = numpy.unique(
                numpy.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
            )
            open_water = numpy.isin(labels, edge[edge != 0])
            if open_water.any():
                sea = float(numpy.median(levels[open_water]))
                drop = wet & ~open_water & (numpy.abs(levels - sea) <= PHANTOM_LEVEL_EPS_M)
                stats["enclosedTexels"] = int(drop.sum())
                if stats["enclosedTexels"]:
                    levels = numpy.where(drop, 0.0, levels)
                    progress.log(
                        f"water: {stats['enclosedTexels']:,} texels of ENCLOSED sea-level water "
                        f"dropped ({stats['enclosedTexels'] * 12.25 / 1e6:.2f} km2) - the "
                        f"producer's level smear, walled off at the fine resolution"
                    )
    film = (levels > heights) & (levels <= heights + MIN_RENDER_DEPTH_M)
    stats["filmTexels"] = int(film.sum())
    if stats["filmTexels"]:
        levels = numpy.where(film, 0.0, levels)
    return levels, stats


def _drop_phantom_bodies(bodies, level_c, rows, cols):
    """`(kept bodies, {"count", "cells", "notes"})` - see `PHANTOM_LEVEL_EPS_M`."""
    stats = {"count": 0, "cells": 0, "notes": []}
    if not bodies:
        return bodies, stats

    def touches_edge(mask):
        rr, cc = numpy.nonzero(mask)
        return bool(rr.min() == 0 or cc.min() == 0 or rr.max() == rows - 1 or cc.max() == cols - 1)

    edged = [b for b in bodies if touches_edge(b.mask)]
    if not edged:
        return bodies, stats
    sea = max(edged, key=lambda b: int(b.mask.sum()))
    sea_level = float(numpy.median(level_c[sea.mask]))
    kept = []
    for body in bodies:
        if body is sea or touches_edge(body.mask):
            kept.append(body)
            continue
        lv = float(numpy.median(level_c[body.mask]))
        if abs(lv - sea_level) <= PHANTOM_LEVEL_EPS_M:
            stats["count"] += 1
            stats["cells"] += int(body.mask.sum())
            rr, cc = numpy.nonzero(body.mask)
            stats["notes"].append(
                f"phantom body dropped: {int(body.mask.sum())} cells at the sea's level "
                f"({lv:.1f} vs {sea_level:.1f} m) around coarse cell "
                f"({int(rr.mean())}, {int(cc.mean())}) - the producer's level smear, not standing water"
            )
        else:
            kept.append(body)
    return kept, stats


def _refine_worst_boxes(components):
    """Bisect the components whose boxes cover the most dry cells, while the count stays
    inside `MAX_BLOCKS` - see the constants above. `components` is `[(rows, cols), ...]`."""
    import heapq

    def overhang(rr, cc):
        if len(rr) == 0:
            return 0
        h = int(rr.max() - rr.min() + 1)
        w = int(cc.max() - cc.min() + 1)
        return h * w - len(rr)

    out = list(components)
    heap = [(-overhang(rr, cc), i) for i, (rr, cc) in enumerate(out)]
    heapq.heapify(heap)
    while heap and len(out) < MAX_BLOCKS:
        neg, i = heapq.heappop(heap)
        if -neg <= BLOCK_SPLIT_MIN_OVERHANG_CELLS:
            break  # every remaining box already fits its water well enough
        rr, cc = out[i]
        h = int(rr.max() - rr.min() + 1)
        w = int(cc.max() - cc.min() + 1)
        if h >= w:
            mid = int(numpy.median(rr))
            near = rr <= mid
        else:
            mid = int(numpy.median(cc))
            near = cc <= mid
        if not near.any() or near.all():
            continue  # degenerate; this box cannot improve
        a = (rr[near], cc[near])
        b = (rr[~near], cc[~near])
        out[i] = a
        out.append(b)
        heapq.heappush(heap, (-overhang(*a), i))
        heapq.heappush(heap, (-overhang(*b), len(out) - 1))
    return out


def _isfile(path):
    import os

    return bool(path) and os.path.isfile(path)


def _intersects(region, min_x, min_z, max_x, max_z):
    return region_rule.intersects_box(region, min_x, min_z, max_x, max_z)


def _depth_ramp(level, appearance):
    """`art/water/depthcolor_ramp.png` — OUR depth-to-colour ramp, from the bundle's own water.

    The first ramp shipped was vanilla west_coast_usa's (validation pass A2): it stopped the
    per-block `Texture missing` errors, but its pale-teal shallow end painted the sea's coastal
    level-band blocks as milky squares along the map borders — Naomi's "square fog", and the
    wall that read as a halved view distance. The ramp is BeamNG's mechanism; the colours are
    CS2's: Beer-Lambert from the water's own scattering colour down over `depthGradientMax`
    metres at the bundle's `absorptionDistance` — the same attenuation the fog density models.
    """
    base = [max(0.0, float(c)) for c in (appearance.scattering_color or (0.1, 0.2, 0.3, 1))[:3]]
    absorption = max(1.0, float(appearance.absorption_distance))
    t = numpy.arange(256, dtype=numpy.float64) / 255.0
    linear = numpy.outer(numpy.exp(-(t * DEPTH_GRADIENT_MAX_M) / absorption), base)
    srgb = numpy.clip(
        [[colourspace.linear_to_srgb(float(v)) for v in row] for row in linear], 0.0, 1.0
    )
    ramp = (numpy.asarray(srgb).reshape(1, 256, 3) * 255.0 + 0.5).astype(numpy.uint8)
    level.write_bytes_if_changed(png.encode_rgb(ramp), "art", "water", "depthcolor_ramp.png")
    return level.game_path("art", "water", "depthcolor_ramp.png")


def _appearance_fields(appearance, ramp_path=None):
    """WaterObject fields from the bundle's water appearance (§8 `rasters.water`)."""
    base = appearance.base_color
    srgb = [
        int(round(255 * min(1.0, max(0.0, colourspace.linear_to_srgb(float(c)))))) for c in base[:3]
    ]
    alpha = int(round(255 * float(base[3]))) if len(base) > 3 else 255
    under = appearance.scattering_color
    under_srgb = [
        int(round(255 * min(1.0, max(0.0, colourspace.linear_to_srgb(float(c))))))
        for c in under[:3]
    ]
    fog = 1.0 / max(appearance.absorption_distance, 1.0)
    return {
        "baseColor": [*srgb, max(alpha, 200)],
        "underwaterColor": [*under_srgb, 253],
        "waterFogDensity": round(fog, 4),
        "waterFogDensityOffset": 0.1,
        "clarity": 0.15,
        "fresnelBias": 0.1,
        "fresnelPower": 2.5,
        "fullReflect": False,
        "reflectivity": 0.3,
        "specularPower": 200,
        "depthGradientMax": DEPTH_GRADIENT_MAX_M,
        "distortStartDist": 0.1,
        "distortEndDist": 20,
        "distortFullDepth": 3.5,
        "foamAmbientLerp": 1,
        "foamMaxDepth": 0.7,
        "foamRippleInfluence": 0.005,
        "foamTex": "core/art/water/foam.dds",
        "rippleTex": "core/art/water/ripple.dds",
        # The depth-to-colour ramp the shader reads against `depthGradientMax`. Every vanilla
        # WaterBlock names one; without one the engine logged `WaterObject::initTextures|
        # Texture missing:` once per block - 188 times on the 2026-08-31 load (A2). OURS,
        # generated from the bundle's water colours (`_depth_ramp`); the vanilla teal ramp is
        # the declared fallback for a caller with no level to write into.
        "depthGradientTex": ramp_path
        or "assets/materials/tileable/water/depthcolor_ramp/depthcolor_ramp_b.png",
        "wetDarkening": 0.3,
        "wetDepth": 0.5,
        "viscosity": 0.001,
        "density": 1,
    }


INPUTS = ("layer:water", "layer:terrain", "layer:terrainFine")
SETTINGS = ("water", "terrain_upsample")

# version 2: every water object names its `depthGradientTex` (A2)
# version 6: enclosed sea-level water is dropped at the FINE resolution (quay walls exist
# there; the connected smear is the re-rip's to remove), and films under `MIN_RENDER_DEPTH_M`
# never become blocks - the finishing guard for the producer's re-based rip
# version 5: the block split is OVERHANG-driven (a fill-ratio split shattered every river and
# overflowed the vanilla-calibrated 200-block budget)
# version 4: phantom bodies (the producer's sea-level smear over docklands) are dropped and
# counted; a band component's box is split until it fits its water (the dock rectangle covered
# the quay road) - both from Naomi's screenshots, 2026-08-31
# version 3: ...and the ramp is OURS, from the bundle's scattering colour and absorption -
# vanilla's pale-teal ramp painted the sea's coastal level-band blocks as milky squares along
# the map borders ("square fog", a halved-looking view distance)
STAGE = Stage(
    STAGE_KEY, "Water", enabled, units, run, inputs=INPUTS, settings_keys=SETTINGS, version=6
)
