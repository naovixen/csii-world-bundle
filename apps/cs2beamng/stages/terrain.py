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
# world tiling per tier (docs: base 128-2048 m, macro 30-80 m, detail 2-8 m); the SAME source
# texture is written once per tier under a distinct name because every vanilla level does that
# and sharing one path across tiers is the one thing our first loads did differently
BASE_TILE_M = 128.0
MACRO_TILE_M = 40.0
DETAIL_TILE_M = 4.0
TIERS = ("base", "macro", "detail")
# CS2's ground albedos are dark (grass ~0.06 linear: the game lights them with a 110 klux sun and
# exposure); BeamNG's pipeline expects mid-brightness albedo (vanilla grass ~0.2 linear), so one
# linear gain lifts CS2 grass to this luminance and every other ground by the same factor.
GRASS_TARGET_LUMINANCE = 0.16
MAX_GAIN = 6.0


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
    # terrain_upsample 2 = the 8192 grid: the rip's fine 1.75 m raster when present (else the
    # bilinear upsample below); 1 = the 4096 base map. 8192 hung BeamNG on load #11 (2026-08-16),
    # so 1 is the default; the bake's `ground` option follows the same choice (stages/bake.py).
    want_fine = int(settings.get("terrain_upsample") or 1) == 2
    field = reader.load_heightfield(bundle, prefer_fine=want_fine)
    if field is None:
        raise RuntimeError("the bundle has no terrain layer (terrain/heightmap.png + rasters block)")
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
    surface = png.gray_array(paths.surface_index) if paths.surface_index and os.path.isfile(paths.surface_index) else None
    if surface is not None and surface.shape != heights.shape:
        # the roadbed ids stay on the 3.5 m base grid by contract; read them onto this grid
        surface = reader.resample_nearest(surface, heights.shape) if field.fine else None
    progress.log(f"terrain: {'fine ' if field.fine else ''}heightmap {size}x{size} @ {cell:.3f} m ({os.path.basename(field.path)}"
                 f"{'' if field.fine or not want_fine else ' - no layers.terrainFine in this rip'}"
                 f"{' - terrain_upsample 1 keeps the base map' if paths.heightmap_fine and not want_fine else ''})")
    # the ground edits are `terrain.ground_heights` — the same definition the mesh harness and
    # the bake's pre-clipped ground meshes use, so a mesh in a heightfield hole meets these
    # samples exactly: roadbed sink (dilated one cell), then the shallow-band clamp
    lowered = 0
    floor = numpy.full(heights.shape, numpy.inf, dtype=numpy.float32)
    floor_path = os.path.join(ctx.derived_dir, "terrain", "shallow_floor.png")
    if os.path.isfile(floor_path):
        floor_raw = png.gray_array(floor_path)
        if floor_raw.shape == heights.shape:
            has = floor_raw > 0
            floor[has] = (floor_raw[has].astype(numpy.float64) * scale + offset).astype(numpy.float32)
    roadbed = (surface == 1) if surface is not None else None
    sunk = int(terrain_mod.dilate(roadbed, 1).sum()) if roadbed is not None else 0
    heights, lowered, _drop = terrain_mod.ground_heights(heights, roadbed, floor, terrain_mod.ROADBED_SINK if roadbed is not None else 0.0)
    punch, holes_mode, corridor_holes = _punch_mask(ctx, settings, size)
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
    progress.end(f"{len(names)} materials {names}, {holes:,} holes ({holes_mode}; {corridor_holes:,} under the pre-clipped ground meshes)")

    # -- upsample --------------------------------------------------------------------------
    # A bilinear upsample adds no information; with the fine raster (already 8192 at 1.75 m,
    # BeamNG's practical ceiling) it is skipped, and the setting only serves an older rip.
    factor = int(settings.get("terrain_upsample") or 1)
    if factor == 2 and (field.fine or size * 2 > 8192):
        progress.log(f"terrain: upsample x2 skipped ({size} samples already; the fine raster is the game's own grading)")
        factor = 1
    if factor == 2:
        progress.begin("terrain: upsample x2", total=None, unit="")
        heights = _upsample_bilinear(heights)
        layers = numpy.repeat(numpy.repeat(layers, 2, axis=0), 2, axis=1)[:heights.shape[0], :heights.shape[1]]
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
    ter_mod.write_terrain_json(level.path(f"{level.name}.terrain.json"), level.name, level.game_path(ter_name), size, names)
    position = [origin_x, origin_z - (size - 1) * cell, pos_z]
    level.add(["Level_objects"], {
        "name": "theTerrain", "class": "TerrainBlock",
        "position": position, "squareSize": cell, "maxHeight": max_height,
        "terrainFile": level.game_path(ter_name),
        "materialTextureSet": f"{level.name}_TerrainTextureSet",
    })
    materials = _terrain_materials(level, bundle, grounds, gain=_albedo_gain(os.path.join(bundle, "terrain", "surfaces"),
                                                                             settings.get("terrain_brightness", 1.0)))
    level.write_materials("terrains", materials)
    level.write_objects()
    progress.check()
    progress.end(f"{ter_name} {os.path.getsize(level.path(ter_name)) / 2**20:.0f} MB, {len(materials) - 4} terrain materials (+3 ground mesh materials), "
                 f"albedo gain x{_albedo_gain(os.path.join(bundle, 'terrain', 'surfaces'), settings.get('terrain_brightness', 1.0)):.2f}")
    shared = ctx.shared if ctx.shared is not None else {}
    shared["terrain"] = {"position": position, "squareSize": cell, "maxHeight": max_height, "size": size,
                         "cell": cell, "elevationScale": scale, "elevationOffset": offset}
    return {"size": size, "squareSize": cell, "materials": len(names), "holes": holes, "clamped": lowered,
            "roadbedSunk": sunk, "fine": bool(field.fine), "seconds": round(time.monotonic() - started, 1)}




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


NEUTRAL_MAPS = {          # every v1.5 terrain material must name all five maps at all three tiers
    "ao": 255,             # no occlusion
    "height": 128,         # flat mid height (blend weight between materials)
    "roughness": 217,      # 0.85: matte ground
}


def _neutral_map(level, kind, tier="detail", size=1024):
    """A flat gray PNG for a map the bundle does not carry (written once per level and tier)."""
    name = f"{level.name}_neutral_{kind}_{tier}.png"
    path = level.path("art", "terrains", name)
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(png.encode_gray(numpy.full((size, size), NEUTRAL_MAPS[kind], dtype=numpy.uint8), bit_depth=8))
    return level.game_path("art", "terrains", name)


def _read_rgb(path):
    """(h, w, 3) uint8 from an 8-bit RGB or RGBA PNG (the theme textures are either)."""
    try:
        return png.rgba_array(path)[:, :, :3]
    except ValueError:
        from PIL import Image  # noqa: WPS433 - only for the RGB (colour type 2) case
        return numpy.asarray(Image.open(path).convert("RGB"))


def _linear_luminance(rgb8):
    lin = ((rgb8.astype(numpy.float64) / 255.0) ** 2.2)
    return float((0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).mean())


def _albedo_gain(surfaces_dir, user_gain):
    """The linear gain applied to every ground albedo: calibrated on grass, times the user's."""
    path = os.path.join(surfaces_dir, "grass.png")
    gain = 1.0
    if os.path.isfile(path):
        try:
            lum = _linear_luminance(_read_rgb(path))
            if lum > 1e-4:
                gain = min(MAX_GAIN, GRASS_TARGET_LUMINANCE / lum)
        except Exception:  # noqa: BLE001
            gain = 1.0
    return gain * float(user_gain or 1.0)


def _centre_to_mid(rgb):
    """Shift each channel so its mean is 128 — a SIGNED detail texture.

    BeamNG's terrain shader combines the tiers as `base + (2·macro − 1)·mF + (2·detail − 1)·dF`
    (`terrain.h.hlsl calculateValue`), so the macro and detail tiers are deviations around
    mid-gray, not colours: a tier whose mean is `m` biases the result by `(2m − 1)·factor` every
    time it is sampled. CS2's ground albedos are dark (grass 0.40 encoded, extra2 0.33), and we
    put the same albedo in all three tiers, so that bias was applied twice on top of the base and
    the ground went black — evaluated on the shipped textures, `extra2` came out at (0, 6, 0)
    near the camera and grass at (41, 77, 0), which is the black patch and the oversaturated
    green of load #17. Centred tiers average to zero deviation, so the terrain renders at its
    base tier's own colour and the tiers add texture instead of darkness."""
    shifted = rgb.astype(numpy.float64) - rgb.reshape(-1, 3).mean(axis=0)[None, None, :] + 128.0
    return numpy.clip(shifted, 0.0, 255.0).astype(numpy.uint8)


def _rgb_texture(level, src, name, gain=1.0, centre=False):
    """Copy `src` into art/terrains/ as an alpha-free RGB PNG, optionally lifted by `gain` in
    linear light and centred on mid-gray (`_centre_to_mid`); the game path, or None."""
    if not os.path.isfile(src):
        return None
    dst = level.path("art", "terrains", name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        rgb = _read_rgb(src)
        if abs(gain - 1.0) > 1e-3:
            lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
            rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
        if centre:
            rgb = _centre_to_mid(rgb)
        level.write_bytes_if_changed(png.encode_rgb(rgb), "art", "terrains", name)
    except Exception:  # noqa: BLE001 - an odd PNG: ship it as is rather than nothing
        level.copy_file(src, "art", "terrains", name)
    return level.game_path("art", "terrains", name)


# The ground meshes are composited by the same arithmetic the terrain shader applies to its
# tiers, so a mesh in a hole reads like the heightfield beside it. With the macro and detail tiers
# CENTRED on mid-gray (`_centre_to_mid`) both sides average to the ground's own base colour and
# only the texture varies — before centring, both sides applied a dark albedo's bias twice and the
# darkest grounds clipped to black.
# What BeamNG's terrain shader does with our three tiers, read from `shaders/common/terrain/
# terrain.h.hlsl` (`calculateValue`): value = base + (macro·2 − 1)·macroFactor + (detail·2 − 1)·
# detailFactor, on the SAMPLED texel values, the factors from our TerrainMaterial's strengths and
# distance attenuation (`calculateStrenth`: macro 0.6 × (1 − 0.35) = 0.39 up to 400 m; detail
# 0.85 at the camera falling to 0.3 at 50 m). With a mid-tone texture at all three scales that
# overlay DARKENS: load #14's screenshots put terrain grass at ≈ 0.22 (encoded) where the same
# texture on a plain mesh read 0.40 — the sum of factors ≈ 0.9 at that range, and the math is in
# the texel's own (encoded) space: done in linear light the terrain would be black near the camera,
# which it is not. That difference is why the ground meshes drew as pale squares against the
# heightfield (loads #12-#14). The composite applies the same overlay at a middle range (~60 m).
GROUND_MESH_MACRO_FACTOR = 0.39
GROUND_MESH_DETAIL_FACTOR = 0.30


def _ground_mesh_texture(level, src, name, gain=1.0, normal=False, size=2048):
    """One texture for the pre-clipped ground meshes that reads like the heightfield: the ground
    texture at the MACRO scale (one repeat = the whole image = `MACRO_TILE_M` metres) and the
    DETAIL scale tiled inside it (`MACRO_TILE_M / DETAIL_TILE_M` repeats), combined the way the
    terrain shader combines its tiers (`GROUND_MESH_*_FACTOR`, the base tier as the image's mean
    colour: the base repeat is 128 m and cannot tile in a 40 m texture). The mesh UVs repeat every
    `csiibundle.bake.terraincut.GROUND_UV_M` (= `MACRO_TILE_M`) metres. A normal map takes the
    detail tier only (a macro normal would be a 40 m bump). Presentation, not contract; the game
    classifies and blends per pixel."""
    if not os.path.isfile(src):
        return None
    try:
        from PIL import Image
        rgb = _read_rgb(src)
        if abs(gain - 1.0) > 1e-3 and not normal:
            lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
            rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
        im = Image.fromarray(rgb)
        repeats = max(1, int(round(MACRO_TILE_M / DETAIL_TILE_M)))
        per = max(1, size // repeats)
        detail = numpy.asarray(im.resize((per, per), Image.LANCZOS))
        detail = numpy.tile(detail, (repeats + 1, repeats + 1, 1))[:size, :size]
        if normal:
            out = detail
        else:
            macro = _centre_to_mid(numpy.asarray(im.resize((size, size), Image.BILINEAR))).astype(numpy.float64) / 255.0
            det = _centre_to_mid(detail).astype(numpy.float64) / 255.0
            base = numpy.asarray(im).reshape(-1, 3).mean(axis=0) / 255.0
            val = base[None, None, :] + (macro * 2.0 - 1.0) * GROUND_MESH_MACRO_FACTOR + (det * 2.0 - 1.0) * GROUND_MESH_DETAIL_FACTOR
            out = (numpy.clip(val, 0.0, 1.0) * 255.0 + 0.5).astype(numpy.uint8)
        level.write_bytes_if_changed(png.encode_rgb(numpy.ascontiguousarray(out)), "art", "terrains", name)
        return level.game_path("art", "terrains", name)
    except Exception:  # noqa: BLE001 - fall back to the plain detail texture
        return _rgb_texture(level, src, name, gain=gain if not normal else 1.0)


def _terrain_materials(level, bundle, names, gain=1.0):
    """`{name: TerrainMaterial}` + the texture set; copies the ground textures into
    `art/terrains/`. Every one of the fifteen `*Tex` slots is filled: BeamNG resolves an
    absent slot to `/` and logs `Missing Terrain texture: /` per material, and the terrain
    draws untextured (load #1)."""
    surfaces_dir = os.path.join(bundle, "terrain", "surfaces")
    out = {}
    set_name = f"{level.name}_TerrainTextureSet"
    out[set_name] = {"name": set_name, "class": "TerrainMaterialTextureSet", "persistentId": level.pid("terrain:textureset"),
                     "baseTexSize": [1024, 1024], "detailTexSize": [1024, 1024], "macroTexSize": [1024, 1024]}
    neutral = {(kind, tier): _neutral_map(level, kind, tier=tier) for kind in NEUTRAL_MAPS for tier in TIERS}
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
        # mid-gray (`_centre_to_mid`) because that is what `calculateValue` adds them as
        albedo = {tier: _rgb_texture(level, albedo_src, f"{level.name}_{source}_{tier}_b.png",
                                     gain=gain, centre=(tier != "base"))
                  for tier in TIERS}
        normal = {tier: _rgb_texture(level, normal_src, f"{level.name}_{source}_{tier}_nm.png") for tier in TIERS}
        pid = level.pid(f"terrain:material:{name}")
        size = {"base": BASE_TILE_M, "macro": MACRO_TILE_M, "detail": DETAIL_TILE_M}
        internal = f"{level.name}_{name}"               # globally unique: see run()
        mat = {"name": f"{internal}-{pid}", "internalName": internal, "class": "TerrainMaterial", "persistentId": pid,
               "annotation": name.upper(), "groundmodelName": GROUNDMODEL.get(name, "DIRT"),
               "detailDistances": [0, 0, 50, 70], "detailDistAtten": [0, 0.9],
               "macroDistances": [0, 0, 400, 8000], "macroDistAtten": [0.35, 1],
               "baseColorMacroStrength": [0.6, 0.6], "baseColorDetailStrength": [0.85, 0.3],
               "normalMacroStrength": [0.6, 1.0], "normalDetailStrength": [0.8, 0.3],
               "roughnessMacroStrength": [0.9, 0.9], "roughnessDetailStrength": [0.9, 0.7]}
        for tier in TIERS:
            cap = tier.capitalize()
            mat[f"baseColor{cap}Tex"] = albedo[tier] or neutral[("ao", tier)]       # no texture at all: flat white
            mat[f"normal{cap}Tex"] = normal[tier] or neutral[("height", tier)]      # mid gray reads as up
            mat[f"ao{cap}Tex"] = neutral[("ao", tier)]
            mat[f"height{cap}Tex"] = neutral[("height", tier)]
            mat[f"roughness{cap}Tex"] = neutral[("roughness", tier)]
            for group in ("baseColor", "normal", "ao", "height", "roughness"):
                mat[f"{group}{cap}TexSize"] = size[tier]
        out[f"{internal}-{pid}"] = mat
    # v1.5 Materials for the bake's pre-clipped ground meshes (`terraincut`, bound as
    # ground:<layer> -> <level>_ground_<layer>, one per ground the heightfield paints, plus the
    # three classic names for an older bake): the same textures composited like the terrain's
    # tiers, world-planar UVs at csiibundle.bake.terraincut.GROUND_UV_M metres per repeat.
    for gname in sorted(set(list(names) + ["grass", "dirt", "rock"])):
        source = gname if os.path.isfile(os.path.join(surfaces_dir, f"{gname}.png")) else TEXTURE_FALLBACK.get(gname, "grass")
        albedo = _ground_mesh_texture(level, os.path.join(surfaces_dir, f"{source}.png"), f"{level.name}_{source}_ground_b.png", gain=gain)
        normal = _ground_mesh_texture(level, os.path.join(surfaces_dir, f"{source}_normal.png"), f"{level.name}_{source}_ground_nm.png", normal=True)
        mname = f"{level.name}_ground_{gname}"
        stage = {"roughnessFactor": 0.95, "metallicFactor": 0.0}
        if albedo:
            stage["baseColorMap"] = albedo
        else:
            stage["baseColorFactor"] = [0.35, 0.4, 0.25, 1.0]
        if normal:
            stage["normalMap"] = normal
        # doubleSided: the harness draws its lattice from either side (Blender culls no
        # backfaces) and a sub-cut patch on a portal's cliff can be near-vertical, where "faces
        # up" is a coin toss — both sides must draw
        out[mname] = {"name": mname, "mapTo": mname, "class": "Material", "persistentId": level.pid(f"material:ground:{gname}"),
                      "version": 1.5, "materialTag0": "beamng", "materialTag1": "cs2", "doubleSided": True, "Stages": [stage]}
    return out


STAGE = Stage(STAGE_KEY, "Terrain", enabled, units, run)
