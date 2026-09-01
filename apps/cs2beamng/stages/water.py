"""Stage `water` — §8 `water_level.png` as BeamNG water objects.

`water.bodies` gives connected wet regions on the game's own 8× (28 m) simulation grid. A body
whose level is one number is a lake; the reference city's sea+river is ONE body spanning
508–527 m, and a single flat plane over it would drown the valley floor or leave the sea dry —
so every body is split into LEVEL BANDS (`BAND_M` tall) and each 4-connected component of a
band becomes `WaterBlock`s sitting at the band's mean level, extending down past the deepest
ground under them.

THERE IS NO `WaterPlane`. BeamNG's plane is infinite, and this city has land BELOW sea level —
dug dock basins behind quays, a 500 m excavated lake, canal districts — 155.8 ha of dry streets
more than 0.5 m under the sea's surface on the reference city (measured 2026-09-01). An
infinite plane floods every one of them; CS2 decides water per-texel from its simulation grid
and BeamNG has no counterpart, so the sea is boxes like everything else, and the boxes that
reach the raster's edge with an open-water edge strip are extruded `HORIZON_EXTEND_M` outward
so the horizon is still water, not a cliff to nothing.

DECLARED APPROXIMATION (BeamNG's water vocabulary is axis-aligned boxes; CS2's is a texture):
a box may cover dry land. `_flood_safe_boxes` carves every band component against the land
that would VISIBLY flood — dry ground more than `FLOOD_MARGIN_M` below the piece's surface, in
clusters big enough to see (`MIN_FLOOD_CLUSTER_TEXELS`); nearby clusters merge into district
windows (`WINDOW_MERGE_TEXELS`) so one cut isolates a dockland street grid instead of
shattering the ocean. What ships is counted in the report: `floodedHa` (tolerated speck
puddles under boxes), `leftDryHa` (real water dropped — slivers under `MIN_WET_TEXELS`,
uncleanable window remnants, and pieces beyond `MAX_BLOCKS`).

`WaterBlock`: `position` is the centre of the top face (the surface), `scale = [width, height,
depth]` and the block extends DOWN by `depth` (Torque's box is `(-.5,-.5,-1)..(.5,.5,0)`).
Appearance from `water.Appearance` (§8 `rasters.water`): base colour, fog density from the
absorption distance; the wave/foam/ripple tables are BeamNG's own defaults from `small_island`
because the bundle's shader-side wave parameters do not map onto them.
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
# Water shallower than this never renders in the game (the shader fades by depth), so it does
# not become an opaque block here. TUNED - the exact fade is in the compiled shader. The rip's
# honest levels leave millimetre films where the sim had surface moisture.
MIN_RENDER_DEPTH_M = 0.15
# -- the flood-safe cover (all measured on the reference city, 2026-09-01) -----------------
# Dry ground this far under a piece's surface floods VISIBLY if a box covers it; nearer the
# surface it is the shoreline's own discretisation (a beach texel a hand-width under the
# waterline) and covering it is correct rendering.
FLOOD_MARGIN_M = 0.5
# A flood cluster smaller than this (~0.2 ha) is a puddle, not a defect; treating the 8,729
# speck clusters on the reference city as obstacles forced 5,000+ boxes for nothing.
MIN_FLOOD_CLUSTER_TEXELS = 160
# Flood clusters within this many fine texels (~52 m) merge into one window, so a dockland
# street GRID is carved out with one subtraction instead of a cut per street.
WINDOW_MERGE_TEXELS = 15
# A box covering less wet than this (~0.05 ha) is noise; its water is counted `leftDryHa`.
MIN_WET_TEXELS = 40
# NOT a vanilla maximum, deliberately (the residency table's lights and DecalRoads carry the
# same reasoning): 199 blocks are proven on the reference machine, and vanilla never boxed a
# SEA. At 450 the budget's tail still held whole canals - long, thin, unmistakable when dry -
# while at 1,000 the tail is sub-0.1 ha slivers. A block the camera is nowhere near is culled;
# what a level pays per block is scene-graph load cost. `levelcheck/residency` carries the
# same number. If a load ever stutters on water, this is the first knob.
MAX_BLOCKS = 1000
# Sea boxes on the raster's edge extend this far outward so the horizon stays water. Only a
# side whose edge strip is nearly all open water extends - a below-sea CORNER pocket's boxes
# also touch the edge, and extruding those would flood the far ring's continuation of the
# same low land.
HORIZON_EXTEND_M = 12000.0
HORIZON_WET_FRACTION = 0.9
SEA_LEVEL_EPS_M = 0.75
DOWNSAMPLE = 8

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
    levels, film_texels = _drop_thin_films(levels, heights)
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
    level_c, _ground_c, _wet = grid
    appearance = water_mod.load(ctx.bundle)

    # -- split every body into level bands, each band into components ------------------------
    progress.begin("water: bands", total=len(bodies), unit="bodies")
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
    progress.end(f"{len(components)} level-band components")

    progress.begin("water: flood-safe cover", total=len(components), unit="components")
    fine_wet = levels > heights
    boxes, stats = _flood_safe_boxes(fine_wet, heights, level_c, components, progress)
    boxes.sort(key=lambda b: -b[0])
    if len(boxes) > MAX_BLOCKS:
        stats["leftDryTexels"] += sum(b[0] for b in boxes[MAX_BLOCKS:])
        stats["piecesOverBudget"] = len(boxes) - MAX_BLOCKS
        boxes = boxes[:MAX_BLOCKS]
    ha = cell * cell / 1e4
    progress.end(
        f"{len(boxes)} flood-safe boxes; tolerated puddles {stats['floodedTexels'] * ha:.1f} ha, "
        f"left dry {stats['leftDryTexels'] * ha:.1f} ha"
    )

    # -- the sea: the level of the largest edge-touching box; its edge boxes reach the horizon --
    sea_level = None
    fh, fw = fine_wet.shape
    edge_boxes = [b for b in boxes if b[2] == 0 or b[3] >= fh or b[4] == 0 or b[5] >= fw]
    if edge_boxes:
        sea_level = max(edge_boxes, key=lambda b: b[0])[1]

    # -- objects ---------------------------------------------------------------------------
    progress.begin("water: objects", total=len(boxes), unit="objects")
    common = _appearance_fields(appearance, ramp_path=_depth_ramp(level, appearance))
    placed = 0
    for i, (_wet_n, lv, r0, r1, c0, c1) in enumerate(boxes):
        # fine texel c spans origin + c*cell +- cell/2 (r southward); r1/c1 exclusive
        min_x = origin_x + c0 * cell - 0.5 * cell
        max_x = origin_x + c1 * cell - 0.5 * cell
        max_z = origin_z - r0 * cell + 0.5 * cell
        min_z = origin_z - r1 * cell + 0.5 * cell
        min_ground = float(heights[r0:r1, c0:c1].min())
        if sea_level is not None and abs(lv - sea_level) <= SEA_LEVEL_EPS_M:
            min_x, min_z, max_x, max_z = _extrude_to_horizon(
                fine_wet, r0, r1, c0, c1, cell, min_x, min_z, max_x, max_z
            )
        if region is not None and not _intersects(region, min_x, min_z, max_x, max_z):
            continue
        cx, cz = 0.5 * (min_x + max_x), 0.5 * (min_z + max_z)
        depth = max(1.0, lv - min_ground + 1.0)
        bx, by, bz = frame.to_beamng(cx, lv, cz)
        obj = {
            "name": f"water_{i:03d}",
            "class": "WaterBlock",
            "position": [round(bx, 3), round(by, 3), round(bz, 3)],
            "rotationMatrix": frame.identity_matrix(),
            "scale": [round(max_x - min_x, 2), round(max_z - min_z, 2), round(depth, 2)],
            "Foam": FOAM[:1],
            "Ripples (texture animation)": RIPPLES,
            "Waves (vertex undulation)": WAVES,
            "overallWaveMagnitude": round(
                WAVE_MAGNITUDE_REF
                * (
                    appearance.sea_wave_scale
                    if sea_level is not None and abs(lv - sea_level) <= SEA_LEVEL_EPS_M
                    else appearance.lake_wave_scale
                ),
                5,
            ),
            "overallRippleMagnitude": 0.5,
            "overallFoamOpacity": 0.6,
        }
        obj.update(common)
        level.add(["Level_objects", "water"], obj, key=f"water:block:{i}")
        placed += 1
        progress.advance(1)
        if i % 32 == 0:
            progress.check()
    level.write_objects()
    progress.end(f"{placed} WaterBlocks (no plane - the city has land below sea level)")
    return {
        "bodies": len(bodies),
        "pieces": len(components),
        "blocks": placed,
        "plane": False,
        "seaLevel": None if sea_level is None else round(sea_level, 2),
        # the box-vocabulary approximation, counted (module docstring): puddle specks a box
        # tolerates, and real water no flood-safe box could ship
        "floodedHa": round(stats["floodedTexels"] * ha, 1),
        "leftDryHa": round(stats["leftDryTexels"] * ha, 1),
        "piecesOverBudget": stats.get("piecesOverBudget", 0),
        "filmTexelsDropped": film_texels,
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


def _drop_thin_films(levels, heights):
    """Zero water shallower than `MIN_RENDER_DEPTH_M`; the game's own depth fade never shows it."""
    film = (levels > heights) & (levels <= heights + MIN_RENDER_DEPTH_M)
    n = int(film.sum())
    if n:
        levels = numpy.where(film, 0.0, levels)
    return levels, n


def _flood_safe_boxes(fine_wet, heights, level_c, components, progress):
    """Fine-resolution flood-safe rectangles per band component — the module's declared
    approximation, carved.

    Per component: dry ground more than `FLOOD_MARGIN_M` under the piece's surface, in clusters
    over `MIN_FLOOD_CLUSTER_TEXELS`, is an obstacle; nearby clusters merge into windows
    (`WINDOW_MERGE_TEXELS`). Each window is subtracted as a unit — four clean slabs around it
    keep the open water in big pieces, and the window itself recurses so basins INSIDE a
    dockland district still get their boxes. A region no cut can clean drops its water,
    counted. Returns `([(wet, level, r0, r1, c0, c1)], stats)` in fine raster coordinates.
    """
    from scipy import ndimage

    D = DOWNSAMPLE
    out = []
    stats = {"floodedTexels": 0, "leftDryTexels": 0}
    eight = numpy.ones((3, 3), dtype=bool)
    for rr, cc in components:
        piece_level = float(level_c[rr, cc].mean())
        R0, C0 = int(rr.min()) * D, int(cc.min()) * D
        member = numpy.zeros((int(rr.max() - rr.min()) + 1, int(cc.max() - cc.min()) + 1), bool)
        member[rr - rr.min(), cc - cc.min()] = True
        member_f = numpy.kron(member, numpy.ones((D, D), dtype=bool))
        H, W = member_f.shape
        win_wet = fine_wet[R0 : R0 + H, C0 : C0 + W]
        if win_wet.shape != member_f.shape:  # a raster not divisible by DOWNSAMPLE
            member_f = member_f[: win_wet.shape[0], : win_wet.shape[1]]
            H, W = member_f.shape
        w = win_wet & member_f
        harm_raw = (~win_wet) & (heights[R0 : R0 + H, C0 : C0 + W] < piece_level - FLOOD_MARGIN_M)
        labels, n = ndimage.label(harm_raw, structure=eight)
        if n:
            sizes = ndimage.sum(harm_raw, labels, range(1, n + 1))
            big = numpy.zeros(n + 1, bool)
            big[1:] = sizes >= MIN_FLOOD_CLUSTER_TEXELS
            harm = big[labels]
        else:
            harm = harm_raw
        windows = []
        if harm.any():
            merged = (
                ndimage.maximum_filter(harm.astype(numpy.uint8), size=2 * WINDOW_MERGE_TEXELS + 1)
                > 0
            )
            mlab, _mn = ndimage.label(merged, structure=eight)
            windows = ndimage.find_objects(mlab)
        # integral images (int32: counts stay far under 2^31)
        IW = numpy.zeros((H + 1, W + 1), numpy.int32)
        IH = numpy.zeros_like(IW)
        IR = numpy.zeros_like(IW)
        numpy.cumsum(numpy.cumsum(w, 0, dtype=numpy.int32), 1, out=IW[1:, 1:])
        numpy.cumsum(numpy.cumsum(harm, 0, dtype=numpy.int32), 1, out=IH[1:, 1:])
        numpy.cumsum(numpy.cumsum(harm_raw, 0, dtype=numpy.int32), 1, out=IR[1:, 1:])

        def s(mat, r0, r1, c0, c1):
            return int(mat[r1, c1] - mat[r0, c1] - mat[r1, c0] + mat[r0, c0])

        stack = [(0, H, 0, W)]
        while stack:
            r0, r1, c0, c1 = stack.pop()
            if r1 <= r0 or c1 <= c0 or s(IW, r0, r1, c0, c1) == 0:
                continue
            while s(IW, r0, r0 + 1, c0, c1) == 0:
                r0 += 1
            while s(IW, r1 - 1, r1, c0, c1) == 0:
                r1 -= 1
            while s(IW, r0, r1, c0, c0 + 1) == 0:
                c0 += 1
            while s(IW, r0, r1, c1 - 1, c1) == 0:
                c1 -= 1
            nw = s(IW, r0, r1, c0, c1)
            if s(IH, r0, r1, c0, c1) == 0:
                if nw >= MIN_WET_TEXELS:
                    out.append((nw, piece_level, r0 + R0, r1 + R0, c0 + C0, c1 + C0))
                    stats["floodedTexels"] += s(IR, r0, r1, c0, c1)
                else:
                    stats["leftDryTexels"] += nw
                continue
            best, best_n = None, 0
            for win in windows:
                wr0, wr1 = max(win[0].start, r0), min(win[0].stop, r1)
                wc0, wc1 = max(win[1].start, c0), min(win[1].stop, c1)
                if wr0 >= wr1 or wc0 >= wc1:
                    continue
                n_in = s(IH, wr0, wr1, wc0, wc1)
                if n_in > best_n:
                    best_n, best = n_in, (wr0, wr1, wc0, wc1)
            if best is None:
                # filtered harm with no window can't happen; defensive: ship if small wet
                stats["leftDryTexels"] += nw
                continue
            wr0, wr1, wc0, wc1 = best
            if wr0 <= r0 and wr1 >= r1 and wc0 <= c0 and wc1 >= c1:
                # the window swallows the whole box: one guillotine cut at the harm median,
                # then each half faces its own (smaller) windows
                sub = numpy.nonzero(numpy.diff(IH[r0 : r1 + 1, c1] - IH[r0 : r1 + 1, c0]) > 0)[0]
                if (r1 - r0) >= (c1 - c0):
                    mid = r0 + (int(numpy.median(sub)) + 1 if len(sub) else (r1 - r0) // 2)
                    if mid <= r0 or mid >= r1:
                        mid = (r0 + r1) // 2
                    if mid <= r0 or mid >= r1:
                        stats["leftDryTexels"] += nw
                        continue
                    stack += [(r0, mid, c0, c1), (mid, r1, c0, c1)]
                else:
                    subc = numpy.nonzero(numpy.diff(IH[r1, c0 : c1 + 1] - IH[r0, c0 : c1 + 1]) > 0)[
                        0
                    ]
                    mid = c0 + (int(numpy.median(subc)) + 1 if len(subc) else (c1 - c0) // 2)
                    if mid <= c0 or mid >= c1:
                        mid = (c0 + c1) // 2
                    if mid <= c0 or mid >= c1:
                        stats["leftDryTexels"] += nw
                        continue
                    stack += [(r0, r1, c0, mid), (r0, r1, mid, c1)]
                continue
            stack += [
                (r0, wr0, c0, c1),  # slab above the window
                (wr1, r1, c0, c1),  # below
                (max(r0, wr0), min(r1, wr1), c0, wc0),  # left
                (max(r0, wr0), min(r1, wr1), wc1, c1),  # right
                (max(r0, wr0), min(r1, wr1), max(c0, wc0), min(c1, wc1)),  # the window itself
            ]
        progress.advance(1)
        progress.check()
    return out, stats


def _extrude_to_horizon(fine_wet, r0, r1, c0, c1, cell, min_x, min_z, max_x, max_z):
    """Extend a sea box's raster-edge sides outward so the horizon stays water — only sides
    whose edge strip is nearly all open water (`HORIZON_WET_FRACTION`), so a below-sea corner
    pocket's boxes stay put."""
    H, W = fine_wet.shape
    if r0 == 0 and fine_wet[0, c0:c1].mean() >= HORIZON_WET_FRACTION:
        max_z += HORIZON_EXTEND_M
    if r1 == H and fine_wet[H - 1, c0:c1].mean() >= HORIZON_WET_FRACTION:
        min_z -= HORIZON_EXTEND_M
    if c0 == 0 and fine_wet[r0:r1, 0].mean() >= HORIZON_WET_FRACTION:
        min_x -= HORIZON_EXTEND_M
    if c1 == W and fine_wet[r0:r1, W - 1].mean() >= HORIZON_WET_FRACTION:
        max_x += HORIZON_EXTEND_M
    return min_x, min_z, max_x, max_z


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

# version 7: the phantom-water theory is DEAD - the 2026-09-01 re-rip plus the bundle's own
# instances (stilt pillars, deck trees, bridge decals standing over the "smear") proved the
# disconnected sea-level water REAL: dug basins sealed at the sea's level by the sim's own
# conservation. v4's body drop and v6's enclosed drop deleted real water and are removed. The
# actual defect was OURS: the infinite WaterPlane floods 155.8 ha of genuinely below-sea dry
# streets, and band-component bboxes overhang docklands - both replaced by the flood-safe
# fine-resolution cover (no plane, `_flood_safe_boxes`, horizon extrusion, counted tolerances)
# version 6: enclosed sea-level water dropped at the fine resolution [REVERTED in v7]
# version 5: the block split is OVERHANG-driven (a fill-ratio split shattered every river and
# overflowed the vanilla-calibrated 200-block budget)
# version 4: phantom bodies dropped and counted [REVERTED in v7]; band-component box split
# version 3: ...and the ramp is OURS, from the bundle's scattering colour and absorption -
# vanilla's pale-teal ramp painted the sea's coastal level-band blocks as milky squares along
# the map borders ("square fog", a halved-looking view distance)
# version 2: every water object names its `depthGradientTex` (A2)
STAGE = Stage(
    STAGE_KEY, "Water", enabled, units, run, inputs=INPUTS, settings_keys=SETTINGS, version=7
)
