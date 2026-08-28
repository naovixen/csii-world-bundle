"""Stage `terrain` — the §8 heightmap as a BeamNG `TerrainBlock`: `.ter` + `.terrain.json`,
one material per ground, textures from `terrain/surfaces/`, holes from the bake's clip cells.

What is exact and what is a choice:

* **Heights are lossless, and they are already the game's.** `maxHeight = 65536 ·
  elevationScale`, `position.z = elevationOffset`, so `stored == the bundle's own 16-bit value`
  for every untouched sample — and CS2's road grading is already IN that value, because
  `TerrainSystem` renders it into the height cascade and `GetHeightData()` reads the cascade
  back (audit §51; 98.37 % of covered samples agree with the game's own profile). **One** touch
  remains: the samples that stand above the carriageway's cross-section are lowered onto it
  (`derived/terrain/carriageway_ceiling.png`), because the game removes those pixels with a
  stencil and a heightfield has none — 11,683 of the full city's 2,297,403 covered samples
  (0.5 %), counted in the stage's report line. The 0.15 m roadbed sink and the clamp to the
  band FLOOR that used to live here are gone — together they dug a 25 cm trench under every road.
* **Orientation** is the empirically settled one (`beamng/ter.py`): row 0 south, so the bundle
  arrays flip; `position = [originWorld.x, originWorld.z − (H−1)·cell, offset]`, `squareSize =
  cell` (3.5 m; 1.75 with `terrain_upsample = 2`).
* **One material per sample** = `splat.dominant_layer` (the visible-share argmax of the game's
  own splat composite). Names are the bundle's ground names (`rock`, `dirt`, `grass`,
  `extra1..4`). There is deliberately **no roadbed material**: the game paints none (its splatmap
  never sees net or lane data — audit §17 row 2), and stamping one from `surface_index` drew a
  staircase of squares along every road edge.
* **Holes** come from `derived/terrain/clip_cells.png`: `contain` (default) punches only faces
  wholly inside a deep clip band, `majority` also the deep-touched ones, `none` never — plus
  every face the bake's `terraincut` meshes replace (`corridor_faces.png`, setting
  `terrain_cut`): the Blender harness's cut lattice sits in those holes, so the trenches and
  portals look as they do in the harness. Nothing else touches the heights around a deep band
  (no lowering, no skirts: loads #12-#16 tried both and got teeth and curtains).
  A face `(r, c)` in bundle order maps to the `.ter` layer byte at its SOUTHERN sample.
* Terrain materials point all three tiers (base/macro/detail) at the ground's own texture, at
  **the game's own three tilings**: BeamNG's base/macro/detail ARE CS2's far/mid/near, and the
  metres per repeat and the two distance ramps come from `rasters.terrainTiling` through the
  shared `csiibundle.splat.ground_tiling` (audit §47). The tier **strengths** are the game's own
  cross-fade too: with `_tier_deviation`'s encoding a strength IS a lerp weight, so
  `detail = 1 − middle`, `macro = middle·(1 − far)` and the base is the remainder — the three
  always sum to 1, and both overlays read the one distance ramp `[0, midBlendStart, midBlendEnd,
  farBlendEnd]`.
* **Height maps are the ground's own relief**, from `terrain/surfaces/<ground>_normal.png`'s
  alpha: BeamNG picks between the four layer texels around a pixel by a height blend, and the flat
  128 we used to ship made that blend a straight bilinear ramp across one 3.5 m sample — the hard
  grid-aligned material boundary of load #19. See `HEIGHT_SIGMA`.
"""

import os
import time

import numpy
from csiibundle import carriageway, images, png, reader
from csiibundle import splat as splat_mod
from csiibundle.bake import terraincut

from ..beamng import ter as ter_mod
from . import Stage, level_of

STAGE_KEY = "terrain"

# groundmodel (physics surface) per ground name; BeamNG's names, upper case
GROUNDMODEL = {
    "grass": "GRASS",
    "dirt": "DIRT",
    "rock": "ROCK",
    "extra1": "GRASS",
    "extra2": "GRASS",
    "extra3": "DIRT",
    "extra4": "SAND",
    "roadbed": "DIRT",
}
# a ground whose texture the bundle does not carry borrows this one's
TEXTURE_FALLBACK = {
    "roadbed": "dirt",
    "extra1": "grass",
    "extra2": "grass",
    "extra3": "dirt",
    "extra4": "dirt",
}
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
# Until that is fixed and the look is re-validated, this stays at the value the last accepted
# build used. The measurement is not withdrawn — `tools/test_ground_albedo.py` still reports
# BeamNG's anchor every run and the gap to it — it is simply not being acted on yet.
GRASS_TARGET_LUMINANCE = 0.16

# What the measurement says this should be: the median linear luminance of BeamNG's own 26
# grass-named vanilla ground albedos. Kept beside the shipped value so the gap is visible in the
# source rather than only in an audit section.
MEASURED_GRASS_ANCHOR = 0.2185
MAX_GAIN = 6.0


def enabled(settings):
    return True


def units(census, settings):
    factor = int(settings.get("terrain_upsample") or 1)
    size = ((census or {}).get("bundle") or {}).get("rasterSize") or [4096, 4096]
    return int(size[0]) * int(size[1]) * factor * factor


def run(ctx, progress):
    bundle = ctx.bundle
    level = level_of(ctx)
    settings = ctx.settings
    started = time.monotonic()
    paths = reader.terrain_paths(bundle)
    # terrain_upsample 2 = the 8192 grid: the rip's fine 1.75 m raster when present (else the
    # bilinear upsample below); 1 = the 4096 base map. 8192 hung BeamNG on load #11 (2026-08-16),
    # so 1 is the default; the bake's `ground` option follows the same choice (stages/bake.py).
    want_fine = int(settings.get("terrain_upsample") or 1) == 2
    field = reader.load_heightfield(bundle, prefer_fine=want_fine)
    if field is None:
        raise RuntimeError(
            "the bundle has no terrain layer (terrain/heightmap.png + rasters block)"
        )
    rasters = paths.rasters
    # The ground the .ter is built from: the bundle's fine 1.75 m `terrainFine` raster when
    # present — the game's own near-camera ground, its road grading rasterised at that texel
    # size, so a heightfield follows a kerb the way the game's does — else the 3.5 m base map.
    cell = float(field.cell)
    scale = float(field.elevation_scale)
    offset = float(field.elevation_offset)
    origin_x, origin_z = float(field.origin_x), float(field.origin_z)

    # -- heights ---------------------------------------------------------------------------
    progress.begin("terrain: heightmap", total=None, unit="samples")
    raw = field.raw
    if raw.ndim != 2 or raw.shape[0] != raw.shape[1]:
        raise RuntimeError(f"heightmap must be square for a .ter, got {raw.shape}")
    size = raw.shape[0]
    # float32, in exactly the mesh harness's arithmetic (`build_lattice`: raw -> float32 * scale +
    # offset), so the bake's pre-clipped ground meshes quantise to the same uint16 as this .ter
    heights = raw.astype(numpy.float32) * numpy.float32(scale) + numpy.float32(offset)
    # `terrain/surface_index.png` is deliberately NOT read here any more: nothing in this stage
    # keys off the roadbed footprint. The game paints no roadbed material (audit §17 row 2), and
    # since §51 it moves no roadbed height either — the heightmap arrives already graded.
    progress.log(
        f"terrain: {'fine ' if field.fine else ''}heightmap {size}x{size} @ {cell:.3f} m ({os.path.basename(field.path)}"
        f"{'' if field.fine or not want_fine else ' - no layers.terrainFine in this rip'}"
        f"{' - terrain_upsample 1 keeps the base map' if paths.heightmap_fine and not want_fine else ''})"
    )
    # the ground edit is `carriageway.ground_heights` — the same definition the mesh harness and the
    # bake's pre-clipped ground meshes use, so a mesh in a heightfield hole meets these samples
    # exactly. It is the carriageway clamp and nothing else; see the module docstring.
    lowered = 0
    worst = 0.0
    ceiling = numpy.full(heights.shape, numpy.inf, dtype=numpy.float32)
    ceiling_path = os.path.join(ctx.derived_dir, "terrain", "carriageway_ceiling.png")
    if os.path.isfile(ceiling_path):
        ceiling_raw = png.gray_array(ceiling_path)
        if ceiling_raw.shape == heights.shape:
            has = ceiling_raw > 0
            ceiling[has] = (ceiling_raw[has].astype(numpy.float64) * scale + offset).astype(
                numpy.float32
            )
    covered = int(numpy.isfinite(ceiling).sum())
    heights, lowered, worst = carriageway.ground_heights(heights, ceiling)
    punch, holes_mode, corridor_holes = _punch_mask(ctx, settings, size)
    progress.check()
    # APPROXIMATION, counted: the game discards these pixels with its road stencil; a heightfield
    # cannot, so they are lowered onto the game's own carriageway profile top instead. Everything
    # else keeps the grading CS2 itself baked into the heightmap.
    progress.end(
        f"{size}x{size} @ {cell} m, {covered:,} samples under a carriageway, of which "
        f"{lowered:,} ({lowered / max(1, covered) * 100:.1f} %) stood over it and were "
        f"lowered onto its profile (worst {worst:.2f} m)"
    )

    # -- layers ----------------------------------------------------------------------------
    progress.begin("terrain: ground layers", total=None, unit="")
    names = ["rock"]
    layers = numpy.zeros(heights.shape, dtype=numpy.uint8)
    block = rasters.get("splatmap")
    if block and paths.splatmap and os.path.isfile(paths.splatmap):
        sp = splat_mod.Splat(paths.splatmap, block)
        mapping, _published = splat_mod.channel_mapping(block)
        index, names = splat_mod.dominant_layer(sp, mapping, surface_index=None)
        if index.shape != heights.shape:
            index = reader.resample_nearest(index, heights.shape)
        layers = index
    # NO roadbed material is painted. Validated from source: `TerrainMaterialSystem` computes the
    # splatmap on the GPU from the height cascade, the water texture, noise and the user brush
    # (`UpdateSplatmap`) — the whole 1,505-line system contains not one reference to a net, lane,
    # area or surface component, and the terrain's ground roster is rock/dirt/grass plus four
    # paintable extras. The in-game "roadbed" look is the splat classifier responding to
    # machine-graded ground, plus the road mesh and its ~13 %-alpha wear decals on top (audit §17
    # row 2, re-verified 2026-08-17). Stamping `surface_index == 1` as its own terrain material was
    # an invention of ours, and because the ids live on the 3.5 m base grid it painted a staircase
    # of whole squares along every road edge and dug-out — the "square texture" of load #18. The
    # ground under a road is now whatever the splat says, as in game.
    progress.check()
    # holes (mask decided above)
    holes = 0
    if punch is not None:
        holes = int(punch.sum())
        # bundle face (r, c) spans samples r..r+1 north->south and c..c+1 west->east: the
        # .ter square is the one that starts at its SOUTH-WEST sample, i.e. bundle (r+1, c)
        target = layers[1:, :-1]
        target[punch] = ter_mod.HOLE
    progress.end(
        f"{len(names)} materials {names}, {holes:,} holes ({holes_mode}; {corridor_holes:,} under the pre-clipped ground meshes)"
    )

    # -- upsample --------------------------------------------------------------------------
    # A bilinear upsample adds no information; with the fine raster (already 8192 at 1.75 m,
    # BeamNG's practical ceiling) it is skipped, and the setting only serves an older rip.
    factor = int(settings.get("terrain_upsample") or 1)
    if factor == 2 and (field.fine or size * 2 > 8192):
        progress.log(
            f"terrain: upsample x2 skipped ({size} samples already; the fine raster is the game's own grading)"
        )
        factor = 1
    if factor == 2:
        progress.begin("terrain: upsample x2", total=None, unit="")
        heights = _upsample_bilinear(heights)
        layers = numpy.repeat(numpy.repeat(layers, 2, axis=0), 2, axis=1)[
            : heights.shape[0], : heights.shape[1]
        ]
        cell = cell / 2.0
        size = heights.shape[0]
        progress.end(f"{size}x{size} @ {cell} m")

    # -- write .ter + block ----------------------------------------------------------------
    # Terrain material names are a GLOBAL, case-insensitive registry in BeamNG, and loose level
    # folders under levels/ are always mounted: two of our levels (or ours and a vanilla one)
    # both defining "grass" shadow each other, and the loser's terrain draws garbage (load #3).
    # So every internal name carries the level name.
    grounds = list(names)
    names = [f"{level.name}_{n}" for n in grounds]
    progress.begin("terrain: write", total=None, unit="")
    h16, l8, max_height, pos_z = ter_mod.from_bundle_arrays(heights, layers, scale, offset)
    ter_name = f"{level.name}.ter"
    ter_mod.write_ter(level.path(ter_name), h16, l8, names)
    ter_mod.write_terrain_json(
        level.path(f"{level.name}.terrain.json"), level.name, level.game_path(ter_name), size, names
    )
    position = [origin_x, origin_z - (size - 1) * cell, pos_z]
    minimap = _write_minimap(level, ctx.bundle, heights, layers, grounds, paths, scale, cell)
    block = {
        "name": "theTerrain",
        "class": "TerrainBlock",
        "position": position,
        "squareSize": cell,
        "maxHeight": max_height,
        "terrainFile": level.game_path(ter_name),
        "materialTextureSet": f"{level.name}_TerrainTextureSet",
    }
    if minimap:
        # `uiNavi.minimapFromTerrainBlock` reads THIS field and nothing else when `info.json`
        # carries no `minimap` array — and no level BeamNG ships carries one, so the vanilla
        # mechanism is the one to port. `west_coast_usa` sets it to a 2048 px PNG.
        block["minimapImage"] = level.game_path(minimap).lstrip("/")
    level.add(["Level_objects"], block)
    albedo_gain = _albedo_gain(
        os.path.join(bundle, "terrain", "surfaces"), settings.get("terrain_brightness", 1.0)
    )
    sharpen = float(settings.get("terrain_blend_sharpen") or 1.0)
    materials, ground_materials, notes = _terrain_materials(
        level, bundle, grounds, gain=albedo_gain, rasters=rasters, sharpen=sharpen, cell=cell
    )
    level.write_materials("terrains", materials)  # TerrainMaterial only - vanilla convention
    level.write_materials("ground", ground_materials)  # the cut meshes' plain v1.5 Materials
    level.write_objects()
    progress.check()
    for note in notes:  # every approximation is counted where it is made
        progress.log(note)
    terrain_materials = sum(1 for m in materials.values() if m.get("class") == "TerrainMaterial")
    progress.end(
        f"{ter_name} {os.path.getsize(level.path(ter_name)) / 2**20:.0f} MB, {terrain_materials} terrain materials "
        f"in art/terrains/ (+{len(ground_materials)} ground mesh materials in art/ground/), "
        f"albedo gain x{albedo_gain:.2f}"
    )
    shared = ctx.shared if ctx.shared is not None else {}
    shared["terrain"] = {
        "position": position,
        "squareSize": cell,
        "maxHeight": max_height,
        "size": size,
        "cell": cell,
        "elevationScale": scale,
        "elevationOffset": offset,
        # what the ground is painted with: `grounds` are the bundle's own layer
        # names, `layers` the level-prefixed TerrainMaterial internalNames that
        # a GroundCover's `layer` field has to match
        "grounds": list(grounds),
        "layers": list(names),
    }
    return {
        "size": size,
        "squareSize": cell,
        "materials": len(names),
        "holes": holes,
        "minimap": minimap,
        "carriagewaySamples": covered,
        "carriagewayClamped": lowered,
        "carriagewayWorstDrop": round(worst, 3),
        "fine": bool(field.fine),
        "seconds": round(time.monotonic() - started, 1),
    }


MINIMAP_SIZE = 2048


def _write_minimap(level, bundle, heights, layers, grounds, paths, scale, cell):
    """`<name>_minimap.png` — the top-down ground image `TerrainBlock.minimapImage` names.

    The old dashboard/navigation map (`uiNavi.lua:354`) builds its tile from this field:
    `size = (blockSize, blockSize, maxHeight)`, `offset = (pos.x, pos.y + blockSize, 0)`. That
    `+ blockSize` is a TOP-LEFT convention — row 0 of the image is the NORTH edge of the block,
    which is also row 0 of the bundle's own rasters, so the array goes out unflipped. (The `.ter`
    is the one that flips; see `beamng/ter.py`.)

    The pixels are the terrain's own dominant ground, coloured by each ground texture's mean
    albedo and shaded by the heightfield's slope, with water tinted over it. It is the same
    information the level draws, at 7 m per pixel — not a render, and not pretending to be one.
    The minimap APP does not use this at all: it draws the navgraph as vectors (audit A7.0).
    """
    from csiibundle import groundmap
    from csiibundle import png as png_mod

    if heights is None or not heights.size:
        return None
    step = max(1, heights.shape[0] // MINIMAP_SIZE)
    h = heights[::step, ::step].astype(numpy.float32)
    index = layers[::step, ::step][: h.shape[0], : h.shape[1]]
    palette = groundmap.ground_palette(os.path.join(bundle, "terrain", "surfaces"), grounds)

    surface = None
    if paths.water and os.path.isfile(paths.water):
        # the water raster is on the BASE grid; the heights may have been upsampled, so it is
        # resampled to the height grid before subsampling rather than sliced to fit
        water = png_mod.gray_array(paths.water)
        if water.shape != heights.shape:
            water = reader.resample_nearest(water, heights.shape)
        surface = water[::step, ::step].astype(numpy.float32)[: h.shape[0], : h.shape[1]] * scale

    # The picture itself is `csiibundle.groundmap`, because the settings screen's map view draws
    # the same one from the same bundle and two copies of it would drift apart in appearance
    # with nothing to catch them.
    rgb = groundmap.ground_image(h, index, palette, float(cell) * step, surface)
    rgba = numpy.concatenate([rgb, numpy.full((*h.shape, 1), 255.0)], axis=-1)
    name = f"{level.name}_minimap.png"
    level.write_bytes_if_changed(
        png_mod.encode_rgba(numpy.clip(rgba, 0, 255).astype(numpy.uint8)), name
    )
    return name


def _punch_mask(ctx, settings, size):
    """`(punch, holes_mode, corridor_holes)`: the (size-1, size-1) bool mask of .ter squares to
    punch (bundle face grid), or None.

    With the pre-cut ground meshes on (bake `terraincut`, setting `terrain_cut`),
    `corridor_faces.png` is the WHOLE answer and the only one: it marks exactly the squares the
    exact cut took area from, and the mesh that replaces them is in the tiles. `clip_cells.png` is
    the older SCORED rule (a square is a hole iff every sub-sample is in-band, `terrain_holes`),
    and unioning the two punches squares the exact cut keeps — 8,132 of them on the reference
    city, every one a gap with no mesh under it. It is the fallback for a build with the meshes
    off, where whole-square holes are all a heightfield can do."""
    holes_mode = str(settings.get("terrain_holes") or "contain")
    corridor_path = os.path.join(ctx.derived_dir, "terrain", "corridor_faces.png")
    if settings.get("terrain_cut", True) and os.path.isfile(corridor_path):
        corridor = png.gray_array(corridor_path)
        if corridor.shape == (size - 1, size - 1):
            cmask = corridor > 0
            return cmask, "terraincut (the exact cut)", int(cmask.sum())
    cells_path = os.path.join(ctx.derived_dir, "terrain", "clip_cells.png")
    if holes_mode != "none" and os.path.isfile(cells_path):
        cells = png.gray_array(cells_path)
        if cells.shape == (size - 1, size - 1):
            return (cells == 255 if holes_mode == "contain" else cells >= 128), holes_mode, 0
    return None, holes_mode, 0


def _upsample_bilinear(h):
    """(n, n) -> (2n, 2n) with new samples midway (the last row/col repeat)."""
    n = h.shape[0]
    out = numpy.empty((2 * n, 2 * n), dtype=h.dtype)
    out[0::2, 0::2] = h
    right = numpy.concatenate([h[:, 1:], h[:, -1:]], axis=1)
    down = numpy.concatenate([h[1:, :], h[-1:, :]], axis=0)
    diag = numpy.concatenate([down[:, 1:], down[:, -1:]], axis=1)
    out[0::2, 1::2] = 0.5 * (h + right)
    out[1::2, 0::2] = 0.5 * (h + down)
    out[1::2, 1::2] = 0.25 * (h + right + down + diag)
    return out


NEUTRAL_MAPS = {  # every v1.5 terrain material must name all five maps at all three tiers
    "ao": 255,  # no occlusion
    "height": 128,  # flat mid height - the LAST RESORT: see _height_texture
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
    """A flat gray PNG for a map the bundle does not carry (written once per level and tier)."""
    name = f"{level.name}_neutral_{kind}_{tier}.png"
    path = level.path("art", "terrains", name)
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(
                png.encode_gray(
                    numpy.full((size, size), NEUTRAL_MAPS[kind], dtype=numpy.uint8), bit_depth=8
                )
            )
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


def _albedo_gain(surfaces_dir, user_gain):
    """The linear gain applied to every ground albedo: calibrated on grass, times the user's."""
    path = os.path.join(surfaces_dir, "grass.png")
    gain = 1.0
    if os.path.isfile(path):
        try:
            lum = _linear_luminance(images.load_rgb(path))
            if lum > 1e-4:
                gain = min(MAX_GAIN, GRASS_TARGET_LUMINANCE / lum)
        except Exception:
            gain = 1.0
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
    try:
        rgb = images.load_rgb(src)
        if abs(gain - 1.0) > 1e-3:
            lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
            rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
        if deviation:
            rgb = _tier_deviation(rgb)
        level.write_bytes_if_changed(png.encode_rgb(rgb), "art", "terrains", name)
    except Exception:
        level.copy_file(src, "art", "terrains", name)
    return level.game_path("art", "terrains", name)


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
    try:
        rgb = images.load_rgb(src)
        if abs(gain - 1.0) > 1e-3 and not normal:
            lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
            rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
        from PIL import Image

        if rgb.shape[0] != size:
            rgb = numpy.asarray(Image.fromarray(rgb).resize((size, size), Image.LANCZOS))
        base_name = f"{name}.normal.png" if normal else f"{name}.color.png"
        if normal:
            level.write_bytes_if_changed(
                png.encode_rgb(numpy.ascontiguousarray(rgb)), "art", "terrains", base_name
            )
            # a normal map is already signed around (128, 128, 255); the detail slot takes it
            # verbatim and `detailNormalMapStrength` scales it
            path = level.game_path("art", "terrains", base_name)
            return path, path
        linear = (rgb.astype(numpy.float64) / 255.0) ** 2.2
        # The BASE slot must be LOW-FREQUENCY, because the detail slot carries the ground's
        # deviation from the image MEAN and `base + (2*detail - 1)` only reproduces the albedo
        # where the base is locally that mean. On the terrain that is automatic — its base tier
        # repeats every 240-410 m, so it is a smooth wash across one 14-68 m detail patch — but
        # the mesh's base is drawn at `GROUND_UV_M` = 40 m, and putting the full-contrast albedo
        # there added its contrast TWICE. MEASURED on the shipped grounds: 1.8x the albedo's
        # sigma and **32.5 % of `extra2` clamped**, 7-8 % of grass/dirt/extra1 — the near-black
        # and blown-out cream pieces of load #25. Low-passed, the composite clamps 0.0 % and its
        # sigma lands on the albedo's own (grass 0.068 against 0.069).
        low = numpy.asarray(
            Image.fromarray(rgb)
            .resize((BASE_LOWPASS, BASE_LOWPASS), Image.BOX)
            .resize((size, size), Image.BILINEAR)
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
    except Exception:
        plain = _rgb_texture(level, src, f"{name}.color.png", gain=gain if not normal else 1.0)
        return plain, plain


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


def _terrain_materials(level, bundle, names, gain=1.0, rasters=None, sharpen=1.0, cell=1.0):
    """`{name: TerrainMaterial}` + the texture set; copies the ground textures into
    `art/terrains/`. Every one of the fifteen `*Tex` slots is filled: BeamNG resolves an
    absent slot to `/` and logs `Missing Terrain texture: /` per material, and the terrain
    draws untextured (load #1).

    Returns `(terrain_materials, ground_mesh_materials, notes)` — two dicts, because the two
    classes must NOT share a file (see the comment at the split). `notes` is the counted
    approximation list the stage report prints (audit §47/§48)."""
    surfaces_dir = os.path.join(bundle, "terrain", "surfaces")
    tiling_block = (rasters or {}).get("terrainTiling")
    guessed_tiling, missing_height = [], []
    out = {}
    set_name = f"{level.name}_TerrainTextureSet"
    out[set_name] = {
        "name": set_name,
        "class": "TerrainMaterialTextureSet",
        "persistentId": level.pid("terrain:textureset"),
        "baseTexSize": [1024, 1024],
        "detailTexSize": [1024, 1024],
        "macroTexSize": [1024, 1024],
    }
    neutral = {
        (kind, tier): _neutral_map(level, kind, tier=tier)
        for kind in NEUTRAL_MAPS
        for tier in TIERS
    }
    for name in names:
        source = name
        if not os.path.isfile(os.path.join(surfaces_dir, f"{source}.png")):
            source = TEXTURE_FALLBACK.get(name, "grass")
        albedo_src = os.path.join(surfaces_dir, f"{source}.png")
        normal_src = os.path.join(surfaces_dir, f"{source}_normal.png")
        # written as RGB: the theme textures carry a mask in alpha, and BeamNG's terrain
        # texture packer reads PNG alpha as coverage (load #2: the detail tier vanished close
        # up into purple/green). Vanilla terrain textures are colour type 2, no alpha.
        # the BASE tier carries the ground's colour; MACRO and DETAIL are signed deviations around
        # half their deviation from it (`_tier_deviation`) so the overlay reproduces a lerp
        albedo = {
            tier: _rgb_texture(
                level,
                albedo_src,
                f"{level.name}_{source}_{tier}_b.png",
                gain=gain,
                deviation=(tier != "base"),
            )
            for tier in TIERS
        }
        normal = {
            tier: _rgb_texture(level, normal_src, f"{level.name}_{source}_{tier}_nm.png")
            for tier in TIERS
        }
        # the ground's own relief, so BeamNG's height blend has something to interlock with
        height = {
            tier: _height_texture(
                level, normal_src, f"{level.name}_{source}_{tier}_h.png", sharpen=sharpen
            )
            for tier in TIERS
        }
        if height["detail"] is None:
            missing_height.append(name)
        pid = level.pid(f"terrain:material:{name}")
        size, detail_distances, macro_distances, from_game = _tiling_of(tiling_block, name)
        if not from_game:
            guessed_tiling.append(name)
        internal = f"{level.name}_{name}"  # globally unique: see run()
        # The strengths ARE the game's cross-fade, ported exactly rather than guessed. With
        # `_tier_deviation`'s encoding a tier's strength is its lerp weight, so CS2's
        # near -> mid -> far cross-fade is a three-way partition of the two overlays:
        #
        #   detail = 1 - middle                  full near tiling, gone by midBlendEnd
        #   macro  = middle * (1 - far)          rises as the near tier goes, gone by farBlendEnd
        #   base                                 what is left, so the three always sum to 1
        #
        # `calculateStrenth(distanceFactor, 1 - atten, s)` =
        #   lerp(lerp(atten'.x*s.x, s.x, near), lerp(s.y, atten'.y*s.y, far), middle), so
        #   detail: s = [1, 0], atten = [0, 0] -> lerp(1, 0, middle)          = 1 - middle
        #   macro:  s = [0, 1], atten = [0, 1] -> lerp(0, 1 - far, middle)    = middle*(1 - far)
        # and both read the SAME four distances (`blendDetailMacro`: near = d0..d1, middle =
        # d1..d2, far = d2..d3) = [0, midBlendStart, midBlendEnd, farBlendEnd]. That the one
        # vector serves both is the check that the mapping is right.
        #
        # The vanilla `small_island` numbers this replaced (macro 0.6/0.6, detail 0.85/0.3) were
        # an approximation on top of a doubled encoding; together they inflated the albedo's
        # contrast 2.4x and clamped 15.8 % of `extra2` to black. See `_tier_deviation`.
        #
        # DECLARED: BeamNG cuts the detail sample at 250 m and the macro at 1000 m whatever the
        # ramps say (`calculateValue`: `pixelDistance < min(250, ...)` / `min(1000, ...)`), which
        # is short of CS2's midBlendEnd/farBlendEnd. Invisible by construction — both tiers are
        # deviations around mid-gray, so a mip that has converged to the tier's mean contributes
        # exactly zero, and a 14 m repeat is long since mipped flat at 250 m.
        mat = {
            "name": f"{internal}-{pid}",
            "internalName": internal,
            "class": "TerrainMaterial",
            "persistentId": pid,
            "annotation": name.upper(),
            "groundmodelName": GROUNDMODEL.get(name, "DIRT"),
            "detailDistances": detail_distances,
            "detailDistAtten": [0, 0],
            "macroDistances": macro_distances,
            "macroDistAtten": [0, 1],
            "baseColorMacroStrength": [0, 1],
            "baseColorDetailStrength": [1, 0],
            "normalMacroStrength": [0, 1],
            "normalDetailStrength": [1, 0],
            "roughnessMacroStrength": [0, 1],
            "roughnessDetailStrength": [1, 0],
            "heightMacroStrength": [0, 1],
            "heightDetailStrength": [1, 0],
        }
        for tier in TIERS:
            cap = tier.capitalize()
            mat[f"baseColor{cap}Tex"] = (
                albedo[tier] or neutral[("ao", tier)]
            )  # no texture at all: flat white
            mat[f"normal{cap}Tex"] = (
                normal[tier] or neutral[("height", tier)]
            )  # mid gray reads as up
            mat[f"ao{cap}Tex"] = neutral[("ao", tier)]
            # the base tier is the neutral flat 128 by design: height rides the additive
            # overlay, so the relief belongs in macro/detail only (`_height_texture`)
            mat[f"height{cap}Tex"] = (
                neutral[("height", tier)]
                if tier == "base"
                else height[tier] or neutral[("height", tier)]
            )
            mat[f"roughness{cap}Tex"] = neutral[("roughness", tier)]
            for group in ("baseColor", "normal", "ao", "height", "roughness"):
                mat[f"{group}{cap}TexSize"] = _tex_size(size[tier], cell)
        out[f"{internal}-{pid}"] = mat
    # v1.5 Materials for the bake's pre-clipped ground meshes (`terraincut`, bound as
    # ground:<layer> -> <level>_ground_<layer>, one per ground the heightfield paints, plus the
    # three classic names for an older bake): base map at the mesh's own world-planar UV period
    # (csiibundle.bake.terraincut.GROUND_UV_M metres), detail map at the ground's near tiling
    # through `detailScale` — see `_ground_mesh_maps`.
    for gname in sorted({*list(names), "grass", "dirt", "rock"}):
        source = (
            gname
            if os.path.isfile(os.path.join(surfaces_dir, f"{gname}.png"))
            else TEXTURE_FALLBACK.get(gname, "grass")
        )
        metres, _dd, _md, _from_game = _tiling_of(tiling_block, gname)
        albedo, albedo_detail = _ground_mesh_maps(
            level,
            os.path.join(surfaces_dir, f"{source}.png"),
            f"{level.name}_{source}_ground_b",
            gain=gain,
        )
        normal, normal_detail = _ground_mesh_maps(
            level,
            os.path.join(surfaces_dir, f"{source}_normal.png"),
            f"{level.name}_{source}_ground_nm",
            normal=True,
        )
        # how many times the near tiling repeats within one mesh UV period; a UV multiplier, so
        # unlike a baked composite it needs no rounding
        detail_scale = round(terraincut.GROUND_UV_M / max(1e-6, metres["detail"]), 4)
        mname = f"{level.name}_ground_{gname}"
        stage = {"roughnessFactor": 0.95, "metallicFactor": 0.0}
        if albedo:
            stage["baseColorMap"] = albedo
            stage["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]  # blendDetail MULTIPLIES this in
            if albedo_detail:
                stage["detailMap"] = albedo_detail
                stage["detailScale"] = [detail_scale, detail_scale]
                # 1.0 = `base + (near - base)` = the near tiling exactly (`_tier_deviation`)
                stage["detailBaseColorMapStrength"] = 1.0
        else:
            stage["baseColorFactor"] = [0.35, 0.4, 0.25, 1.0]
        if normal:
            stage["normalMap"] = normal
            if normal_detail:
                stage["detailNormalMap"] = normal_detail
                stage["detailNormalMapStrength"] = 1.0
                stage.setdefault("detailScale", [detail_scale, detail_scale])
        # doubleSided: the harness draws its lattice from either side (Blender culls no
        # backfaces) and a sub-cut patch on a portal's cliff can be near-vertical, where "faces
        # up" is a coin toss — both sides must draw
        out[mname] = {
            "name": mname,
            "mapTo": mname,
            "class": "Material",
            "persistentId": level.pid(f"material:ground:{gname}"),
            "version": 1.5,
            "materialTag0": "beamng",
            "materialTag1": "cs2",
            "doubleSided": True,
            "Stages": [stage],
        }
    # The ground mesh materials go in their OWN file, NOT beside the TerrainMaterials.
    # Measured against every shipped vanilla level: `art/terrains/*.materials.json` holds 191
    # `TerrainMaterial` entries and ZERO plain `Material` entries, and not one vanilla file mixes
    # the two classes. We had our 7 ground mesh materials in there, which is a convention with no
    # precedent in the engine's own content — and the cut meshes rendered as flat untextured grey
    # through loads #19-#21 while three rounds of texture work on them changed nothing visible.
    # `art/cs2/` is out (the materials stage rewrites that file wholesale, after this one runs) and
    # `art/shapes/` is out (the mesh compiler writes placeholder materials files there and the cook
    # stage deletes what it finds), so they get `art/ground/`.
    notes = [
        f"terrain: ground meshes get 2 tiers where the terrain gets 3 (a v1.5 material has "
        f"two texture slots) and their base map runs at the mesh's {terraincut.GROUND_UV_M:.0f} m "
        f"UV period, not the game's mid tiling - APPROXIMATED for "
        f"{sum(1 for v in out.values() if v.get('class') == 'Material')} "
        f"ground mesh material(s); the near tier is exact via detailScale"
    ]
    if guessed_tiling:
        notes.append(
            f"terrain: no rasters.terrainTiling in this rip - {len(guessed_tiling)} ground(s) "
            f"fall back to the pre-audit-47 guesses ({', '.join(sorted(guessed_tiling))})"
        )
    if abs(sharpen - 1.0) > 1e-6:
        notes.append(
            f"terrain: terrain_blend_sharpen x{sharpen:g} - NOT PARITY, a declared deviation: "
            f"the ground relief is {sharpen:g}x what BeamNG's blend derives, to dissolve the "
            f"blocky boundaries our 1.75 m layer grid causes (audit §48.3)"
        )
    if missing_height:
        notes.append(
            f"terrain: {len(missing_height)} ground(s) ship no height map "
            f"({', '.join(sorted(missing_height))}) - flat 128, so BeamNG's height blend "
            f"degenerates to the bilinear ramp for them"
        )
    ground = {k: v for k, v in out.items() if v.get("class") == "Material"}
    terrain_only = {k: v for k, v in out.items() if v.get("class") != "Material"}
    return terrain_only, ground, notes


# What this stage is a function of, for the worker's per-stage skip (pipeline/state.py). The
# §8 rasters plus the bake's terrain product (the carriageway ceiling, the corridor faces and
# the clip cells the punch is read from); `terrain_brightness` and `terrain_blend_sharpen` are
# in the list because they change the ground textures this stage writes, not just its heights.
INPUTS = (
    "layer:terrain",
    "layer:terrainFine",
    "layer:surfaceIndex",
    "layer:splatmap",
    "layer:userSplatmap",
    "layer:surfaces",
    "layer:water",
    "derived:terrain",
)
SETTINGS = (
    "terrain_upsample",
    "terrain_holes",
    "terrain_cut",
    "terrain_brightness",
    "terrain_blend_sharpen",
)
OUTPUTS = ("art/terrains/main.materials.json",)

# version 2: the block carries `minimapImage` and the stage writes the 2048 px `<name>_minimap.png`
# it names (A7.2) - the old dashboard map drew nothing without it
STAGE = Stage(
    STAGE_KEY,
    "Terrain",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    shared_keys=("terrain",),
    version=2,
)
