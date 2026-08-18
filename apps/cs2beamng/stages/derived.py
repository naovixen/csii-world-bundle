"""Stage `derived` — the bake's world-space tiles (markings, areas, skirts, lane furniture)
as one `.dae` + `TSStatic` per tile, per product.

The bake already did the hard part (draping, bending, conforming, tiling by 256 m, one
primitive per material); this stage is a frame change and a policy table:

| product      | group          | collision (default)                       | shadows |
|--------------|----------------|-------------------------------------------|---------|
| markings     | Markings       | none (`collision_paint` → Visible Mesh)   | off     |
| areas        | Areas          | none (`collision_paint` → Visible Mesh)   | off     |
| skirts       | Skirts         | Visible Mesh Final (`collision_skirts`)   | on      |
| lanegeometry | LaneFurniture  | Visible Mesh Final (`collision_furniture`)| on      |

Paint is a projection lifted millimetres above a surface that already collides — a collider
there is a lip that catches wheels (plan: collision policy). Each tile's geometry is written
relative to its tile origin. Materials bind `materials.mapto_name`.

## The LOD ladder (A4)

Every tile carries the GAME's own per-primitive draw-distance limits: the bake writes each
primitive's `m_MinLod` into its material name (`#L<n>`, `csiibundle.bake.tiles.MIN_LOD_MARK`),
this stage reads them back and builds one LOD level per distance band whose content is exactly
what CS2 still draws there — see `beamng/tilelod.py` for why that cannot crack and which half of
the arithmetic is the game's and which is BeamNG's. A `terraincut` tile carries no limits at all
and is always drawn: it IS the ground there, and the game never distance-culls terrain.

## One tile per worker (A5)

A tile reads one `.glb` and writes one `.dae` that no other tile writes, so the loop is
embarrassingly parallel and now runs on `jobs` processes. The parent keeps what is genuinely
shared: the scene tree (`level.add`), the set of materials this build bound, and the ladder
accounting. `imap` rather than `imap_unordered`, so the object list — and therefore the `main/`
diff of a rebuild — does not depend on which worker finished first.
"""

import math
import multiprocessing
import os
import re
import time

import numpy

from csiibundle import glb, lod
from csiibundle.bake.tiles import MIN_LOD_MARK, split_material_name

from ..beamng import dae, frame, tilelod
from . import Stage, level_of
from .materials import aliases_of, mapto_name

STAGE_KEY = "derived"
TILE_DETAIL_PX = tilelod.ALWAYS_PX      # what a tile nothing culls still ships (see beamng/tilelod)
PRODUCTS = {
    # product: (setting toggle, group, collision setting or None for paint, castShadows)
    "markings": ("markings", "Markings", "collision_paint", False),
    "areas": ("areas", "Areas", "collision_paint", False),
    "skirts": ("skirts", "Skirts", "collision_skirts", True),
    "lanegeometry": ("lane_furniture", "LaneFurniture", "collision_furniture", True),
    # the pre-clipped ground at trenches / portals: it IS the ground there (the heightfield is
    # punched underneath), so it always collides and casts shadows
    "terraincut": ("terrain_cut", "TerrainCut", None, True),
}
# bake material name -> the level's ground material (written by the terrain stage beside its
# TerrainMaterials, from the same detail textures)
GROUND_MATERIAL_NAMES = {"ground:grass": "grass", "ground:dirt": "dirt", "ground:rock": "rock"}
TILE_RE = re.compile(r"^(?P<product>[a-z]+)_(?P<tx>-?\d+)_(?P<tz>-?\d+)\.glb$")


def enabled(settings):
    return any(settings.get(toggle, True) for toggle, _g, _c, _s in PRODUCTS.values())


def units(census, settings):
    return 3000


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    tile = float(settings.get("tile_size") or 256)
    collide_all = str(settings.get("collision_preset") or "simplified") != "off"
    detail_scale = float(settings.get("lod_detail_scale", 6.0) or 6.0)
    # `tile_lod` off keeps A4's ladder exactly: switches anchored on the game's own null distance
    # and no decimation at all.
    view_distance = float(settings.get("view_distance") or 0) if settings.get("tile_lod", True) else None
    workers = ctx.jobs or 1
    aliases = aliases_of(ctx.shared, level)
    shared_used = (ctx.shared if ctx.shared is not None else {}).setdefault("materials_used", set())
    summary = {}
    for product, (toggle, group, coll_setting, shadows) in PRODUCTS.items():
        if not settings.get(toggle, True):
            continue
        folder = os.path.join(ctx.derived_dir, product)
        if not os.path.isdir(folder):
            progress.log(f"derived: no {product} tiles baked")
            continue
        files = sorted(f for f in os.listdir(folder) if TILE_RE.match(f) and f.startswith(product + "_"))
        if ctx.region:
            files = [f for f in files if _tile_intersects(f, tile, ctx.region)]
        collide = collide_all and (coll_setting is None or bool(settings.get(coll_setting, coll_setting != "collision_paint")))
        progress.begin(f"derived: {product}", total=len(files), unit="tiles")
        counts = {"written": 0, "tris": 0, "split_geoms": 0, "rewritten": 0}
        names = set()
        ladder_stats = tilelod.new_stats()
        jobs = [(folder, name, product, tile, level.dir, level.name, aliases, detail_scale,
                 view_distance) for name in files]

        def absorb(result, counts=counts, names=names, ladder_stats=ladder_stats,
                   group=group, product=product, collide=collide, shadows=shadows):
            progress.advance(1)
            if result is None:
                return
            shared_used.update(result["used"])
            tilelod.account_compact(ladder_stats, result["lod"])
            counts["split_geoms"] += result["extraGeoms"]
            counts["rewritten"] += 1 if result["changed"] else 0
            names.add(result["name"])
            obj = {"name": result["name"], "class": "TSStatic",
                   "shapeName": level.game_path("art", "shapes", product, result["name"] + ".dae"),
                   "position": [round(result["ox"], 3), round(result["oy"], 3), 0.0],
                   "rotationMatrix": frame.identity_matrix(), "scale": [1, 1, 1],
                   "collisionType": "Visible Mesh Final" if collide else "None", "decalType": "None",
                   "useInstanceRenderData": True}
            if not shadows:
                obj["castShadows"] = False
            level.add([group], obj, key=f"derived:{result['name']}")
            counts["written"] += 1
            counts["tris"] += result["triangles"]

        if workers > 1 and len(jobs) > 4:
            mp = multiprocessing.get_context("spawn")
            with mp.Pool(min(workers, len(jobs))) as pool:
                for index, result in enumerate(pool.imap(_tile_job, jobs, chunksize=4)):
                    absorb(result)
                    if index % 25 == 0:
                        progress.check()
        else:
            for index, job in enumerate(jobs):
                absorb(_tile_job(job))
                if index % 25 == 0:
                    progress.check()
        # each product owns art/shapes/<product>/ (the folder is no longer cleared at build start)
        orphans = dae.sweep_orphans(level.path("art", "shapes", product), names)
        progress.end(f"{counts['written']} tiles ({counts['rewritten']} rewritten), {counts['tris']:,} triangles, collision "
                     f"{'on' if collide else 'off'}, {orphans} orphans removed, "
                     f"{counts['split_geoms']} extra geometries from the {dae.MAX_GEOMETRY_VERTS:,}-vertex cap")
        summary[product] = {"tiles": counts["written"], "tilesRewritten": counts["rewritten"],
                            "orphansRemoved": orphans, "triangles": counts["tris"], "collision": collide,
                            "splitGeometries": counts["split_geoms"],
                            "lod": tilelod.ladder_summary(ladder_stats, detail_scale)}
    level.write_objects()
    return {"products": summary, "jobs": workers, "seconds": round(time.monotonic() - started, 1)}


def _tile_job(args):
    """One baked tile → its `.dae`, in a worker process.

    Everything a tile needs is its own `.glb` plus the alias table, and it writes one file no
    other tile writes. It hands back only what the parent needs to place the `TSStatic` and to
    account for the ladder — never the mesh, and never the ladder's per-triangle masks.
    """
    folder, name, product, tile, level_dir, level_name, aliases, detail_scale, view_distance = args
    m = TILE_RE.match(name)
    tx, tz = int(m.group("tx")), int(m.group("tz"))
    nodes = glb.parse_glb(os.path.join(folder, name))
    if not nodes:
        return None
    level = LevelPaths(level_dir, level_name)
    shared = {"materials_aliases": {"aliases": aliases}}
    ox, oy = (tx + 0.5) * tile, (tz + 0.5) * tile      # bundle x/z = BeamNG x/y
    parts = []
    for node in nodes:
        if len(node["tris"]) == 0:
            continue
        mtx = numpy.asarray(node["matrix"], dtype=numpy.float64)
        v = numpy.asarray(node["verts"], dtype=numpy.float64) @ mtx[:3, :3].T + mtx[:3, 3]
        local, nn = dae.glb_to_local(v, node["normals"])
        local[:, 0] -= ox
        local[:, 1] -= oy
        # The bake's material name carries the GAME's own draw-distance limit for these
        # triangles (`#L<n>`, tiles.MIN_LOD_MARK). It is kept on the bound name through
        # the merge so `group_by_material` keeps the limits in separate runs, and
        # stripped once the ladder has read it back — a `.dae` must bind the real name.
        runs = []
        for mat, count in node["material_runs"]:
            # `_variant` is the tile sink's paint-variant tag; no world-space product
            # emits one today (paint variants live on object shapes), and if one ever
            # does it will need its own bound name here rather than the canonical.
            base, _variant, min_lod = split_material_name(mat)
            bound = _ground_mapto(level, base) if base.startswith("ground:") else mapto_name(shared, level, base)
            runs.append((bound if min_lod is None else f"{bound}{MIN_LOD_MARK}{min_lod}", count))
        parts.append((local, nn, dae.glb_uvs(node["uvs"]), None, node["tris"], runs))
    if not parts:
        return None
    shape_name = f"{product}_{tx}_{tz}"
    mesh = dae.group_by_material(dae.merge_meshes(shape_name, parts))
    levels, ladder = _tile_levels(shape_name, mesh, detail_scale, view_distance, tile)
    path = level.path("art", "shapes", product, f"{shape_name}.dae")
    stats = dae.write_shape(path, dae.Shape(shape_name, levels, null_px=ladder["nullPx"]))
    return {"name": shape_name, "ox": ox, "oy": oy, "triangles": stats["triangles"],
            "changed": bool(stats.get("changed")), "lod": tilelod.compact(levels, ladder),
            "extraGeoms": sum(len(ms) for _px, ms in levels) - len(levels),
            "used": sorted(shared.get("materials_used") or ())}


class LevelPaths:
    """The `Level` methods a tile worker needs, without a `Level` — which owns the scene tree and
    the persistent-id set and must stay in the parent process."""

    def __init__(self, level_dir, name):
        self.dir = level_dir
        self.name = name

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def game_path(self, *parts):
        return "/levels/" + self.name + "/" + "/".join(p.replace("\\", "/") for p in parts)


def _ground_mapto(level, mat):
    """The terrain stage's ground mesh material for a bake `ground:<name>` binding (any ground the
    heightfield paints — the terrain stage writes one mesh material per layer name)."""
    return f"{level.name}_ground_{GROUND_MATERIAL_NAMES.get(mat, mat.split(':', 1)[1])}"


def _tile_intersects(name, tile, region):
    m = TILE_RE.match(name)
    tx, tz = int(m.group("tx")), int(m.group("tz"))
    x0, x1 = tx * tile, (tx + 1) * tile
    z0, z1 = tz * tile, (tz + 1) * tile
    cx, cz, r = float(region["centerX"]), float(region["centerZ"]), float(region["radius"])
    return not (x1 < cx - r or x0 > cx + r or z1 < cz - r or z0 > cz + r)


# The five product toggles are read through a variable (`settings.get(toggle, True)`), so they
# are declared here by hand and caught at runtime by the recorder rather than by a source scan.
INPUTS = tuple(f"derived:{p}" for p in sorted(PRODUCTS)) + ("derived:materials.json",)
SETTINGS = tuple(sorted({toggle for toggle, _g, _c, _s in PRODUCTS.values()}
                        | {c for _t, _g, c, _s in PRODUCTS.values() if c}
                        | {"tile_size", "region", "collision_preset", "lod_detail_scale",
                           "view_distance", "tile_lod"}))
OUTPUTS = ("art/shapes",)

# -- the ladder ------------------------------------------------------------------------------------
#
# Each LOD level is the set of triangles CS2 still draws at that distance, so a switch removes
# only what the game had already stopped drawing and no level can crack against another. The
# whole argument and every helper live in `beamng/tilelod.py`, shared with the networks stage.

def _tile_levels(shape_name, mesh, detail_scale, view_distance=None, tile=None):
    """`([(px, [Mesh]), ...], ladder)` for one merged tile."""
    return tilelod.build_levels(dae, shape_name, mesh, detail_scale,
                                lod.DEFAULT_LEVEL_OF_DETAIL, TILE_DETAIL_PX,
                                view_distance=view_distance,
                                targets=tilelod.TILE_TARGETS if view_distance else None,
                                tile=tile)


# version 5: `lod_detail_scale` no longer multiplies the LADDER, only the null threshold
# version 4: an uncullable tile (skirts, terraincut) gets a detail ladder too, and the
# decimator's UV bin scales with the mesh's own UV range
STAGE = Stage(STAGE_KEY, "Markings, areas, skirts, furniture", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS, outputs=OUTPUTS, shared_keys=("materials_used",), version=5)
