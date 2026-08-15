"""Stage `terrain` — the §8 heightmap as a BeamNG `TerrainBlock`: `.ter` + `.terrain.json`,
one material per ground, textures from `terrain/surfaces/`, holes from the bake's clip cells.

What is exact and what is a choice:

* **Heights are lossless.** `maxHeight = 65536 · elevationScale`, `position.z = elevationOffset`,
  so `stored == the bundle's own 16-bit value` for every untouched sample. Two touches, both
  contract rules: samples the bake's shallow clip band covers are lowered to the band's floor
  (`derived/terrain/shallow_floor.png`; §8c "ground never pokes through a carriageway"), and
  roadbed samples (§8 surface id 1) sink `terrain.ROADBED_SINK` = 0.15 m.
* **Orientation** is the empirically settled one (`beamng/ter.py`): row 0 south, so the bundle
  arrays flip; `position = [originWorld.x, originWorld.z − (H−1)·cell, offset]`, `squareSize =
  cell` (3.5 m; 1.75 with `terrain_upsample = 2`).
* **One material per sample** = `splat.dominant_layer` (the visible-share argmax of the game's
  own splat composite) with `roadbed` stamped from `surface_index`. Names are the bundle's
  ground names (`rock`, `dirt`, `grass`, `extra1..4`, `roadbed`).
* **Holes** come from `derived/terrain/clip_cells.png`: `contain` (default) punches only faces
  wholly inside a deep clip band, `majority` also the deep-touched ones, `none` never.
  A face `(r, c)` in bundle order maps to the `.ter` layer byte at its SOUTHERN sample.
* Terrain materials point all three tiers (base/macro/detail) at the ground's own texture at
  three tilings; the numbers are the vanilla `small_island` values where BeamNG's own artists
  chose them (distances, attenuations) and plain guesses where the vanilla file is a per-level
  painting (`*BaseTexSize`) — tuning is A4's, on screen.
"""

import os
import time

import numpy

from csiibundle import png, reader, splat as splat_mod, terrain as terrain_mod

from ..beamng import ter as ter_mod
from . import Stage, level_of

STAGE_KEY = "terrain"

# groundmodel (physics surface) per ground name; BeamNG's names, upper case
GROUNDMODEL = {"grass": "GRASS", "dirt": "DIRT", "rock": "ROCK", "extra1": "GRASS", "extra2": "GRASS",
               "extra3": "DIRT", "extra4": "SAND", "roadbed": "DIRT"}
# a ground whose texture the bundle does not carry borrows this one's
TEXTURE_FALLBACK = {"roadbed": "dirt", "extra1": "grass", "extra2": "grass", "extra3": "dirt", "extra4": "dirt"}
BASE_TILE_M = 64.0        # the coarse tier's world tiling
MACRO_TILE_M = 24.0       # the mid tier's


def enabled(settings):
    return True


def units(census, settings):
    factor = int(settings.get("terrain_upsample") or 1)
    size = (((census or {}).get("bundle") or {}).get("rasterSize") or [4096, 4096])
    return int(size[0]) * int(size[1]) * factor * factor


def run(ctx, progress):
    bundle = ctx.bundle
    level = level_of(ctx)
    settings = ctx.settings
    started = time.monotonic()
    paths = reader.terrain_paths(bundle)
    if paths.rasters is None or not os.path.isfile(paths.heightmap):
        raise RuntimeError("the bundle has no terrain layer (terrain/heightmap.png + rasters block)")
    rasters = paths.rasters
    cell = float(rasters["resolution"])
    scale = float(rasters.get("elevationScale", 1.0))
    offset = float(rasters.get("elevationOffset", 0.0))
    origin_x, _oy, origin_z = (float(v) for v in rasters["originWorld"])

    # -- heights ---------------------------------------------------------------------------
    progress.begin("terrain: heightmap", total=None, unit="samples")
    raw = png.gray_array(paths.heightmap)
    if raw.ndim != 2 or raw.shape[0] != raw.shape[1]:
        raise RuntimeError(f"heightmap must be square for a .ter, got {raw.shape}")
    size = raw.shape[0]
    heights = raw.astype(numpy.float64) * scale + offset
    surface = png.gray_array(paths.surface_index) if paths.surface_index and os.path.isfile(paths.surface_index) else None
    if surface is not None and surface.shape != heights.shape:
        surface = None
    lowered = 0
    floor_path = os.path.join(ctx.derived_dir, "terrain", "shallow_floor.png")
    if os.path.isfile(floor_path):
        floor_raw = png.gray_array(floor_path)
        if floor_raw.shape == heights.shape:
            floor = floor_raw.astype(numpy.float64) * scale + offset
            has = floor_raw > 0
            hit = has & (heights > floor)
            lowered = int(hit.sum())
            heights[hit] = floor[hit]
    sunk = 0
    if surface is not None:
        roadbed = surface == 1
        sunk = int(roadbed.sum())
        heights[roadbed] -= terrain_mod.ROADBED_SINK
    progress.check()
    progress.end(f"{size}x{size} @ {cell} m, {lowered:,} samples clamped to shallow bands, {sunk:,} roadbed samples sunk")

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
            index = _nearest_resample(index, heights.shape)
        layers = index
    if surface is not None:
        if "roadbed" not in names:
            names.append("roadbed")
        layers = numpy.where(surface == 1, names.index("roadbed"), layers).astype(numpy.uint8)
    progress.check()
    # holes
    holes_mode = str(settings.get("terrain_holes") or "contain")
    holes = 0
    cells_path = os.path.join(ctx.derived_dir, "terrain", "clip_cells.png")
    if holes_mode != "none" and os.path.isfile(cells_path):
        cells = png.gray_array(cells_path)
        if cells.shape == (size - 1, size - 1):
            punch = cells == 255 if holes_mode == "contain" else cells >= 128
            holes = int(punch.sum())
            # bundle face (r, c) spans samples r..r+1 north->south and c..c+1 west->east: the
            # .ter square is the one that starts at its SOUTH-WEST sample, i.e. bundle (r+1, c)
            target = layers[1:, :-1]
            target[punch] = ter_mod.HOLE
    progress.end(f"{len(names)} materials {names}, {holes:,} holes ({holes_mode})")

    # -- upsample --------------------------------------------------------------------------
    factor = int(settings.get("terrain_upsample") or 1)
    if factor == 2:
        progress.begin("terrain: upsample x2", total=None, unit="")
        heights = _upsample_bilinear(heights)
        layers = numpy.repeat(numpy.repeat(layers, 2, axis=0), 2, axis=1)[:heights.shape[0], :heights.shape[1]]
        cell = cell / 2.0
        size = heights.shape[0]
        progress.end(f"{size}x{size} @ {cell} m")

    # -- write .ter + block ----------------------------------------------------------------
    progress.begin("terrain: write", total=None, unit="")
    h16, l8, max_height, pos_z = ter_mod.from_bundle_arrays(heights, layers, scale, offset)
    ter_name = f"{level.name}.ter"
    ter_mod.write_ter(level.path(ter_name), h16, l8, names)
    ter_mod.write_terrain_json(level.path(f"{level.name}.terrain.json"), level.name, level.game_path(ter_name), size, names)
    position = [origin_x, origin_z - (size - 1) * cell, pos_z]
    level.add(["Level_objects"], {
        "name": "theTerrain", "class": "TerrainBlock",
        "position": position, "squareSize": cell, "maxHeight": max_height,
        "terrainFile": level.game_path(ter_name),
        "materialTextureSet": f"{level.name}_TerrainTextureSet",
    })
    materials = _terrain_materials(level, bundle, names)
    level.write_materials("terrains", materials)
    level.write_objects()
    progress.check()
    progress.end(f"{ter_name} {os.path.getsize(level.path(ter_name)) / 2**20:.0f} MB, {len(materials) - 1} terrain materials")
    shared = ctx.shared if ctx.shared is not None else {}
    shared["terrain"] = {"position": position, "squareSize": cell, "maxHeight": max_height, "size": size,
                         "cell": cell, "elevationScale": scale, "elevationOffset": offset}
    return {"size": size, "squareSize": cell, "materials": len(names), "holes": holes, "clamped": lowered,
            "roadbedSunk": sunk, "seconds": round(time.monotonic() - started, 1)}


def _nearest_resample(index, shape):
    rows = (numpy.arange(shape[0]) * index.shape[0] / shape[0]).astype(int)
    cols = (numpy.arange(shape[1]) * index.shape[1] / shape[1]).astype(int)
    return index[rows][:, cols]


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


NEUTRAL_MAPS = {          # every v1.5 terrain material must name all five maps at all three tiers
    "ao": 255,             # no occlusion
    "height": 128,         # flat mid height (blend weight between materials)
    "roughness": 217,      # 0.85: matte ground
}


def _neutral_map(level, kind, size=1024):
    """A flat gray PNG for a map the bundle does not carry (written once per level)."""
    name = f"neutral_{kind}.png"
    path = level.path("art", "terrains", name)
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(png.encode_gray(numpy.full((size, size), NEUTRAL_MAPS[kind], dtype=numpy.uint8), bit_depth=8))
    return level.game_path("art", "terrains", name)


def _terrain_materials(level, bundle, names):
    """`{name: TerrainMaterial}` + the texture set; copies the ground textures into
    `art/terrains/`. Every one of the fifteen `*Tex` slots is filled: BeamNG resolves an
    absent slot to `/` and logs `Missing Terrain texture: /` per material, and the terrain
    draws untextured (load #1)."""
    surfaces_dir = os.path.join(bundle, "terrain", "surfaces")
    out = {}
    set_name = f"{level.name}_TerrainTextureSet"
    out[set_name] = {"name": set_name, "class": "TerrainMaterialTextureSet", "persistentId": level.pid("terrain:textureset"),
                     "baseTexSize": [1024, 1024], "detailTexSize": [1024, 1024], "macroTexSize": [1024, 1024]}
    neutral = {kind: _neutral_map(level, kind) for kind in NEUTRAL_MAPS}
    for name in names:
        source = name
        if not os.path.isfile(os.path.join(surfaces_dir, f"{source}.png")):
            source = TEXTURE_FALLBACK.get(name, "grass")
        albedo_src = os.path.join(surfaces_dir, f"{source}.png")
        normal_src = os.path.join(surfaces_dir, f"{source}_normal.png")
        albedo = None
        normal = None
        if os.path.isfile(albedo_src):
            level.copy_file(albedo_src, "art", "terrains", f"{source}.png")
            albedo = level.game_path("art", "terrains", f"{source}.png")
        if os.path.isfile(normal_src):
            level.copy_file(normal_src, "art", "terrains", f"{source}_normal.png")
            normal = level.game_path("art", "terrains", f"{source}_normal.png")
        pid = level.pid(f"terrain:material:{name}")
        albedo = albedo or neutral["ao"]              # a ground with no texture at all: flat white
        normal = normal or neutral["height"]           # flat normal stand-in (mid gray reads as up)
        mat = {"name": name, "internalName": name, "class": "TerrainMaterial", "persistentId": pid,
               "annotation": name.upper(), "groundmodelName": GROUNDMODEL.get(name, "DIRT"),
               "detailDistances": [0, 0, 50, 70], "detailDistAtten": [0, 0.9],
               "macroDistances": [0, 0, 400, 8000], "macroDistAtten": [0.35, 1],
               "baseColorBaseTex": albedo, "baseColorBaseTexSize": BASE_TILE_M,
               "baseColorMacroTex": albedo, "baseColorMacroTexSize": MACRO_TILE_M, "baseColorMacroStrength": [0.1, 0.4],
               "baseColorDetailTex": albedo, "baseColorDetailStrength": [0.4, 0],
               "normalBaseTex": normal, "normalBaseTexSize": BASE_TILE_M,
               "normalMacroTex": normal, "normalMacroTexSize": MACRO_TILE_M, "normalMacroStrength": [0.4, 1.1],
               "normalDetailTex": normal, "normalDetailStrength": [0.7, 0.2],
               "aoBaseTex": neutral["ao"], "aoBaseTexSize": BASE_TILE_M,
               "aoMacroTex": neutral["ao"], "aoMacroTexSize": MACRO_TILE_M, "aoDetailTex": neutral["ao"],
               "heightBaseTex": neutral["height"], "heightBaseTexSize": BASE_TILE_M,
               "heightMacroTex": neutral["height"], "heightMacroTexSize": MACRO_TILE_M, "heightDetailTex": neutral["height"],
               "roughnessBaseTex": neutral["roughness"], "roughnessBaseTexSize": BASE_TILE_M,
               "roughnessMacroTex": neutral["roughness"], "roughnessMacroTexSize": MACRO_TILE_M,
               "roughnessMacroStrength": [0.9, 0.9], "roughnessDetailTex": neutral["roughness"],
               "roughnessDetailStrength": [0.9, 0.7]}
        out[name] = mat
    return out


STAGE = Stage(STAGE_KEY, "Terrain", enabled, units, run)
