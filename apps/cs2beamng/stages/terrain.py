"""Stage `terrain` — the §8 heightmap as a BeamNG `TerrainBlock`: `.ter` + `.terrain.json`,
one material per ground, textures from `terrain/surfaces/`, holes from the bake's clip cells,
and a wall down to the road under every hole rim.

What is exact and what is a choice:

* **Heights are lossless** away from roads. `maxHeight = 65536 · elevationScale`, `position.z =
  elevationOffset`, so `stored == the bundle's own 16-bit value` for every untouched sample.
  Three touches, all under or beside a road, all LOWERING only (see `_ground_under_roads`):
    - a sample a shallow clip band covers (`derived/terrain/shallow_lane.png`, which since
      terrainmask v2 includes the ones standing above the band's roof — the spikes) is capped
      at `lane − UNDER_ROAD_SINK`, the plane the game's own grading already put its neighbours
      on. Not at the game's band floor (`lane − 0.5`): that number is the depth to which the
      game HIDES ground per pixel, and putting the whole heightfield there dug a second trench
      under every road that stepped back up at the kerb (2026-08-16 load: "annoying to drive
      off").
    - a sample next to a covered one (its 8-ring) is capped at the lane, so the sheet meets the
      road edge flush instead of rising through the kerb between two samples on a cross-slope.
    - roadbed (§8 surface id 1) samples are otherwise left where the game graded them; the old
      blanket 0.15 m sink beside the road was the other half of the step.
* **Orientation** is the empirically settled one (`beamng/ter.py`): row 0 south, so the bundle
  arrays flip; `position = [originWorld.x, originWorld.z − (H−1)·cell, offset]`, `squareSize =
  cell` (3.5 m; 1.75 with `terrain_upsample = 2`).
* **One material per sample** = `splat.dominant_layer` (the visible-share argmax of the game's
  own splat composite) with `roadbed` stamped from `surface_index`. Names are the bundle's
  ground names (`rock`, `dirt`, `grass`, `extra1..4`, `roadbed`).
* **Holes** come from `derived/terrain/clip_cells.png`: `touch` (default) punches every face a
  deep band reaches, `contain` only the ones wholly inside one, `none` never. `contain` is the
  contract's per-face rule and errs to keeping ground; on a heightfield that kept ground is the
  face the tunnel mouth passes through, standing across the road, so the app defaults to
  `touch`. A face `(r, c)` in bundle order maps to the `.ter` layer byte at its SOUTHERN sample.
* **Rim walls.** A heightfield hole shows the underside of the sheet — nothing — from any
  angle but straight down. Every edge between a punched face and a kept one gets a vertical
  quad from the rim samples down to the road under the hole (`derived/terrain/deep_lane.png`)
  minus `WALL_DROP`, textured with the level's rock, facing the hole, with collision, tiled
  like the derived layers (`HoleWalls` group). The game hides that terrain per pixel against
  the prism wall; a wall on the prism's outer rim is the heightfield-native stand-in.
* Terrain materials point all three tiers (base/macro/detail) at the ground's own texture at
  three tilings; the numbers are the vanilla `small_island` values where BeamNG's own artists
  chose them (distances, attenuations) and plain guesses where the vanilla file is a per-level
  painting (`*BaseTexSize`) — tuning is A4's, on screen.
"""

import os
import time
from collections import defaultdict

import numpy

from csiibundle import png, reader, splat as splat_mod

from ..beamng import dae, frame, ter as ter_mod
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

# How far below the carriageway the ground under a road sits (metres). MEASURED, not tuned: on
# the 2026-08-16 rip the game's own graded heightmap under a shallow band is at lane − 0.25 at
# the 25th, 50th and 75th percentile alike (573,689 samples), and the ring just outside sits at
# −0.06 median / 0.00 at p75 — the game grades a 25 cm bed under the carriageway and brings the
# ground back flush at the edge. Lowering only the samples standing above that plane (15% of
# them, the crossfall/curvature crests) puts them level with their neighbours, so nothing bumps
# under the asphalt and nothing steps at the edge. (The old rule sat everything at the band
# floor, lane − 0.5: a second 25 cm trench under every road that had to climb back at the kerb.)
UNDER_ROAD_SINK = 0.25
# A hole-rim wall reaches this far below the road under the hole, so its foot is out of sight
# under the shell / carriageway. TUNED.
WALL_DROP = 1.0
# The rim wall's texture repeat in metres (the terrain's own detail tiling).
WALL_UV_M = DETAIL_TILE_M
WALL_GROUP = "HoleWalls"
WALL_DETAIL_PX = 2      # detail sizes must be > 0 (BeamNG: "invalid detail levels")


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
    mask_dir = os.path.join(ctx.derived_dir, "terrain")
    lane = _height_raster(os.path.join(mask_dir, "shallow_lane.png"), heights.shape, scale, offset)
    deep_lane = _height_raster(os.path.join(mask_dir, "deep_lane.png"), heights.shape, scale, offset)
    hole_lane = _height_raster(os.path.join(mask_dir, "hole_lane.png"), (size - 1, size - 1), scale, offset)
    ground = _ground_under_roads(heights, lane)
    progress.check()
    progress.end(f"{size}x{size} @ {cell} m, {ground['underRoad']:,} samples put under a road "
                 f"(worst drop {ground['worstDrop']:.2f} m), {ground['ring']:,} ring samples capped at the lane")

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
    holes_mode = str(settings.get("terrain_holes") or "touch")
    holes = 0
    punch = None
    cells_path = os.path.join(mask_dir, "clip_cells.png")
    if holes_mode != "none":
        if not os.path.isfile(cells_path):
            raise RuntimeError(f"terrain: {cells_path} missing — the bake's terrainmask product did not run")
        cells = png.gray_array(cells_path)
        if cells.shape != (size - 1, size - 1):
            raise RuntimeError(f"terrain: clip_cells.png is {cells.shape}, heightmap faces are {(size - 1, size - 1)}")
        punch = cells == 255 if holes_mode == "contain" else cells >= 128
        holes = int(punch.sum())
        # bundle face (r, c) spans samples r..r+1 north->south and c..c+1 west->east: the
        # .ter square is the one that starts at its SOUTH-WEST sample, i.e. bundle (r+1, c)
        target = layers[1:, :-1]
        target[punch] = ter_mod.HOLE
    progress.end(f"{len(names)} materials {names}, {holes:,} holes ({holes_mode})")

    # -- rim walls -------------------------------------------------------------------------
    walls = {"tiles": 0, "quads": 0, "skipped": 0}
    if punch is not None and holes:
        progress.begin("terrain: hole rim walls", total=None, unit="")
        walls = _write_hole_walls(ctx, level, heights, punch, deep_lane, hole_lane, cell, origin_x, origin_z, progress)
        progress.end(f"{walls['quads']:,} wall quads in {walls['tiles']} tiles ({walls['skipped']} rim edges without a road height)")

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
    gain = _albedo_gain(os.path.join(bundle, "terrain", "surfaces"), settings.get("terrain_brightness", 1.0))
    materials = _terrain_materials(level, bundle, grounds, gain=gain)
    materials.update(_wall_material(level, bundle, gain))
    level.write_materials("terrains", materials)
    level.write_objects()
    progress.check()
    progress.end(f"{ter_name} {os.path.getsize(level.path(ter_name)) / 2**20:.0f} MB, {len(materials) - 2} terrain materials, "
                 f"albedo gain x{gain:.2f}")
    shared = ctx.shared if ctx.shared is not None else {}
    shared["terrain"] = {"position": position, "squareSize": cell, "maxHeight": max_height, "size": size,
                         "cell": cell, "elevationScale": scale, "elevationOffset": offset}
    return {"size": size, "squareSize": cell, "materials": len(names), "holes": holes, "holesMode": holes_mode,
            "underRoad": ground["underRoad"], "ring": ground["ring"], "worstDrop": ground["worstDrop"],
            "wallQuads": walls["quads"], "wallTiles": walls["tiles"],
            "seconds": round(time.monotonic() - started, 1)}


# -- ground under roads -------------------------------------------------------------------------

def _height_raster(path, shape, scale, offset):
    """A gray16 height raster from the terrainmask bake → float64 metres, NaN where 0 (none).
    Missing or mis-sized is an error: the bake product that writes it runs earlier in the same
    build, so absence means an old cache or a failed bake, never a bundle without roads."""
    if not os.path.isfile(path):
        raise RuntimeError(f"terrain: {path} missing — the bake's terrainmask (v2) product did not run; "
                           "re-run the build so the bake refreshes derived/terrain/")
    q = png.gray_array(path)
    if q.shape != shape:
        raise RuntimeError(f"terrain: {os.path.basename(path)} is {q.shape}, heightmap is {shape}")
    out = q.astype(numpy.float64) * scale + offset
    out[q == 0] = numpy.nan
    return out


def _ground_under_roads(heights, lane):
    """Mutates `heights` in place; see the module docstring. Returns the counts."""
    band = numpy.isfinite(lane)
    target = lane - UNDER_ROAD_SINK
    hit = band & (heights > target)
    worst = float((heights - target)[hit].max()) if hit.any() else 0.0
    heights[hit] = target[hit]
    under = int(hit.sum())

    # ring: the highest neighbouring lane caps a sample that no band covers
    ring_lane = numpy.full(heights.shape, -numpy.inf)
    lane_filled = numpy.where(band, lane, -numpy.inf)
    h, w = heights.shape
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            src = lane_filled[max(0, dr):h + min(0, dr), max(0, dc):w + min(0, dc)]
            dst = ring_lane[max(0, -dr):h + min(0, -dr), max(0, -dc):w + min(0, -dc)]
            numpy.maximum(dst, src, out=dst)
    ring = (~band) & numpy.isfinite(ring_lane) & (heights > ring_lane)
    heights[ring] = ring_lane[ring]
    return {"underRoad": under, "ring": int(ring.sum()), "worstDrop": round(worst, 3)}


# -- hole rim walls -----------------------------------------------------------------------------

def _write_hole_walls(ctx, level, heights, punch, deep_lane, hole_lane, cell, origin_x, origin_z, progress):
    """One vertical quad per edge between a punched face and a kept one, from the two rim
    samples down to the road under the hole − WALL_DROP, facing the hole. Tiled like the
    derived layers, one `TSStatic` per tile with collision, material `<level>_holewall`.

    The road height a wall reaches for is the punched face's own `hole_lane` (the lowest lane
    of the deep quads that reached it — known for every `touch`/`contain` hole), else the
    nearest `deep_lane` sample within two cells."""
    tile = float(ctx.settings.get("tile_size") or 256)
    collide = True          # a wall stands where ground was: it always collides, like the ground did
    fh, fw = punch.shape
    ref = _nearest_finite(deep_lane, radius=2)

    quads = []          # (ax, ay, bx, by, top_a, top_b, bottom, nx, ny) in bundle x / bundle z / height
    skipped = 0

    def sample_xy(r, c):
        return origin_x + c * cell, origin_z - r * cell          # bundle x, bundle z (BeamNG x, y)

    # vertical edges: between face (r, c) and (r, c+1) -> along sample column c+1, rows r..r+1
    diff = punch[:, :-1] != punch[:, 1:]
    for r, c in zip(*numpy.nonzero(diff)):
        hole_right = punch[r, c + 1]
        col = c + 1
        a, b = (r, col), (r + 1, col)
        face = (r, c + 1) if hole_right else (r, c)
        # the hole lies east (+x) of the edge when the right face is punched
        skipped += _add_wall(quads, heights, ref, hole_lane[face], a, b, sample_xy, (1.0 if hole_right else -1.0, 0.0))
    # horizontal edges: between face (r, c) and (r+1, c) -> along sample row r+1, cols c..c+1
    diff = punch[:-1, :] != punch[1:, :]
    for r, c in zip(*numpy.nonzero(diff)):
        hole_south = punch[r + 1, c]
        row = r + 1
        a, b = (row, c), (row, c + 1)
        face = (r + 1, c) if hole_south else (r, c)
        # bundle z runs north; the southern face is punched -> the hole lies at -z
        skipped += _add_wall(quads, heights, ref, hole_lane[face], a, b, sample_xy, (0.0, -1.0 if hole_south else 1.0))
    # map-edge holes need no wall (there is no kept face beside them)

    if not quads:
        return {"tiles": 0, "quads": 0, "skipped": skipped}
    by_tile = defaultdict(list)
    for q in quads:
        mx, my = (q[0] + q[2]) * 0.5, (q[1] + q[3]) * 0.5
        by_tile[(int(numpy.floor(mx / tile)), int(numpy.floor(my / tile)))].append(q)
    material = f"{level.name}_holewall"
    written = 0
    for (tx, ty), items in sorted(by_tile.items()):
        ox, oy = (tx + 0.5) * tile, (ty + 0.5) * tile
        verts, uvs, normals, tris = [], [], [], []
        for ax, ay, bx, by, top_a, top_b, bottom, nx, ny in items:
            base = len(verts)
            length = float(numpy.hypot(bx - ax, by - ay))
            corners = [(ax, ay, top_a), (bx, by, top_b), (bx, by, bottom), (ax, ay, bottom)]
            for (x, y, z) in corners:
                verts.append((x - ox, y - oy, z))
                normals.append((nx, ny, 0.0))
            uvs += [(0.0, top_a / WALL_UV_M), (length / WALL_UV_M, top_b / WALL_UV_M),
                    (length / WALL_UV_M, bottom / WALL_UV_M), (0.0, bottom / WALL_UV_M)]
            # wind so the geometric normal points along (nx, ny): the hole side
            t1, t2 = (base, base + 1, base + 2), (base, base + 2, base + 3)
            p0, p1, p2 = (numpy.asarray(verts[i], dtype=numpy.float64) for i in t1)
            geo = numpy.cross(p1 - p0, p2 - p0)
            if geo[0] * nx + geo[1] * ny < 0:
                t1, t2 = (base, base + 2, base + 1), (base, base + 3, base + 2)
            tris += [t1, t2]
        shape_name = f"holewalls_{tx}_{ty}"
        mesh = dae.Mesh(shape_name, numpy.asarray(verts), numpy.asarray(tris), [(material, len(tris))],
                        normals=numpy.asarray(normals), uvs=numpy.asarray(uvs))
        path = level.path("art", "shapes", "holewalls", f"{shape_name}.dae")
        dae.write_shape(path, dae.Shape(shape_name, [(WALL_DETAIL_PX, [mesh])]))
        level.add([WALL_GROUP], {
            "name": shape_name, "class": "TSStatic",
            "shapeName": level.game_path("art", "shapes", "holewalls", f"{shape_name}.dae"),
            "position": [round(ox, 3), round(oy, 3), 0.0], "rotationMatrix": frame.identity_matrix(), "scale": [1, 1, 1],
            "collisionType": "Visible Mesh Final" if collide else "None", "decalType": "None",
            "useInstanceRenderData": True}, key=f"holewalls:{shape_name}")
        written += 1
        if written % 20 == 0:
            progress.check()
    return {"tiles": written, "quads": len(quads), "skipped": skipped}


def _add_wall(quads, heights, ref, face_lane, a, b, sample_xy, normal):
    """Append one wall quad for the edge a→b (sample indices) if a road height is known under
    it and the rim stands above the wall's foot. Returns 1 when skipped for want of a height."""
    ra, ca = a
    rb, cb = b
    if numpy.isfinite(face_lane):
        lane_ref = float(face_lane)
    else:
        refs = [v for v in (ref[ra, ca], ref[rb, cb]) if numpy.isfinite(v)]
        if not refs:
            return 1
        lane_ref = min(refs)
    bottom = float(lane_ref) - WALL_DROP
    top_a, top_b = float(heights[ra, ca]), float(heights[rb, cb])
    if max(top_a, top_b) <= bottom + 0.05:
        return 0
    ax, ay = sample_xy(ra, ca)
    bx, by = sample_xy(rb, cb)
    quads.append((ax, ay, bx, by, max(top_a, bottom), max(top_b, bottom), bottom, normal[0], normal[1]))
    return 0


def _nearest_finite(raster, radius=2):
    """Each sample's own value where finite, else the minimum finite value within `radius`
    samples (Chebyshev), else NaN."""
    out = raster.copy()
    missing = ~numpy.isfinite(out)
    if not missing.any():
        return out
    h, w = raster.shape
    best = numpy.full(raster.shape, numpy.inf)
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            src = raster[max(0, dr):h + min(0, dr), max(0, dc):w + min(0, dc)]
            dst = best[max(0, -dr):h + min(0, -dr), max(0, -dc):w + min(0, -dc)]
            numpy.fmin(dst, src, out=dst)
    fill = missing & numpy.isfinite(best)
    out[fill] = best[fill]
    return out


def _wall_material(level, bundle, gain):
    """The rim walls' v1.5 Material: the level's rock texture, tiled every WALL_UV_M metres."""
    surfaces_dir = os.path.join(bundle, "terrain", "surfaces")
    source = "rock" if os.path.isfile(os.path.join(surfaces_dir, "rock.png")) else "dirt"
    albedo = _rgb_texture(level, os.path.join(surfaces_dir, f"{source}.png"), f"{level.name}_{source}_detail_b.png", gain=gain)
    normal = _rgb_texture(level, os.path.join(surfaces_dir, f"{source}_normal.png"), f"{level.name}_{source}_detail_nm.png")
    name = f"{level.name}_holewall"
    stage = {"roughnessFactor": 0.95, "metallicFactor": 0.0}
    if albedo:
        stage["baseColorMap"] = albedo
    else:
        stage["baseColorFactor"] = [0.35, 0.32, 0.28, 1.0]
    if normal:
        stage["normalMap"] = normal
    return {name: {"name": name, "mapTo": name, "class": "Material", "persistentId": level.pid("material:holewall"),
                   "version": 1.5, "materialTag0": "beamng", "materialTag1": "cs2",
                   "Stages": [stage, {}, {}, {}]}}


# -- helpers ------------------------------------------------------------------------------------

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


def _rgb_texture(level, src, name, gain=1.0):
    """Copy `src` into art/terrains/ as an alpha-free RGB PNG, optionally lifted by `gain` in
    linear light; the game path, or None. Written once per level (the terrain materials and
    the wall material share the detail tier)."""
    if not os.path.isfile(src):
        return None
    dst = level.path("art", "terrains", name)
    written = level.__dict__.setdefault("_terrain_textures", set())
    if dst in written:
        return level.game_path("art", "terrains", name)
    written.add(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        rgb = _read_rgb(src)
        if abs(gain - 1.0) > 1e-3:
            lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2 * gain
            rgb = (numpy.clip(lin, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(numpy.uint8)
        with open(dst, "wb") as handle:
            handle.write(png.encode_rgb(rgb))
    except Exception:  # noqa: BLE001 - an odd PNG: ship it as is rather than nothing
        level.copy_file(src, "art", "terrains", name)
    return level.game_path("art", "terrains", name)


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
        albedo = {tier: _rgb_texture(level, albedo_src, f"{level.name}_{source}_{tier}_b.png", gain=gain) for tier in TIERS}
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
    return out


STAGE = Stage(STAGE_KEY, "Terrain", enabled, units, run)
