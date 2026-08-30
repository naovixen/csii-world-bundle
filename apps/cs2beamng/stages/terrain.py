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
from csiibundle import carriageway, png, reader
from csiibundle import splat as splat_mod

from ..beamng import ter as ter_mod
from . import Stage, level_of, terrainmaterials, terrainminimap, terraintextures

STAGE_KEY = "terrain"


def enabled(settings):
    return True


def units(census, settings):
    factor = int(settings.get("terrain_upsample") or 1)
    size = ((census or {}).get("bundle") or {}).get("rasterSize") or [4096, 4096]
    return int(size[0]) * int(size[1]) * factor * factor


def _referenced_textures(doc, out):
    """Every `art/terrains/<file>` a materials document names, recursively."""
    if isinstance(doc, dict):
        for value in doc.values():
            _referenced_textures(value, out)
    elif isinstance(doc, (list, tuple)):
        for value in doc:
            _referenced_textures(value, out)
    elif isinstance(doc, str) and "/art/terrains/" in doc:
        out.add(doc.rsplit("/", 1)[1].lower())


def _sweep_terrain_orphans(level, *material_docs):
    """Delete the textures in `art/terrains/` that no material of this build names.

    The counterpart of `dae.sweep_orphans` for the ground: the one-file-per-image writer
    (095455b) left the previous build's per-tier copies in place - 55 files, 79 MiB - and
    a file nothing names is not mounted by BeamNG, but it is counted by every instrument that
    reads the folder and it is shipped with the level. Surviving files keep their mtime.
    """
    keep = set()
    for doc in material_docs:
        _referenced_textures(doc, keep)
    directory = level.path("art", "terrains")
    if not keep or not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if name.lower().endswith((".png", ".dds")) and name.lower() not in keep:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    return removed


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
    minimap = terrainminimap.write_minimap(
        level, ctx.bundle, heights, layers, grounds, paths, scale, cell
    )
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
    albedo_gain = terraintextures.albedo_gain(
        os.path.join(bundle, "terrain", "surfaces"), settings.get("terrain_brightness", 1.0)
    )
    sharpen = float(settings.get("terrain_blend_sharpen") or 1.0)
    materials, ground_materials, notes = terrainmaterials.terrain_materials(
        level, bundle, grounds, gain=albedo_gain, rasters=rasters, sharpen=sharpen, cell=cell
    )
    level.write_materials("terrains", materials)  # TerrainMaterial only - vanilla convention
    level.write_materials("ground", ground_materials)  # the cut meshes' plain v1.5 Materials
    orphans = _sweep_terrain_orphans(level, materials, ground_materials)
    if orphans:
        progress.log(f"terrain: {orphans} textures in art/terrains/ no material names any more, removed")
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
        "orphansRemoved": orphans,
        "holes": holes,
        "minimap": minimap,
        "carriagewaySamples": covered,
        "carriagewayClamped": lowered,
        "carriagewayWorstDrop": round(worst, 3),
        "fine": bool(field.fine),
        "seconds": round(time.monotonic() - started, 1),
    }


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
# version 4: textures in art/terrains/ that no material names are removed (the v3 run left 79 MiB)
# version 3: one terrain PNG per distinct image (095455b landed the writer; the stage never re-ran, so
# the built level still carried 57 MiB of byte-identical tier copies - Phase 10 residency)
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
    version=4,
)
