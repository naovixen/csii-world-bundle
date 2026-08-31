"""Stage `derived` — the bake's world-space tiles (markings, areas, skirts, lane furniture)
as one `.dae` + `TSStatic` per tile, per product.

The bake already did the hard part (draping, bending, conforming, tiling by 256 m, one
primitive per material); this stage is a frame change and a policy table:

| product      | group          | collision (default)                       | shadows |
|--------------|----------------|-------------------------------------------|---------|
| markings     | Markings       | none (`collision_paint` → Visible Mesh)   | off     |
| areas        | Areas          | none (`collision_paint` → Visible Mesh)   | off     |
| skirts       | Skirts         | Collision Mesh (`collision_skirts`)       | off     |
| lanegeometry | LaneFurniture  | Collision Mesh (`collision_furniture`)    | on      |

Paint is a projection lifted millimetres above a surface that already collides — a collider
there is a lip that catches wheels (plan: collision policy). Each tile's geometry is written
relative to its tile origin. Materials bind `beamng.materials.mapto_name`.

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

import os
import re
import time

import numpy
from csiibundle import glb, lod, parallel
from csiibundle import region as region_rule
from csiibundle.bake.tiles import MIN_LOD_MARK, split_material_name

from ..beamng import dae, daemesh, frame, tilemesh
from ..beamng import level as level_mod
from ..beamng.level import LevelPaths
from ..beamng.matbind import aliases_of, mapto_name
from . import Stage, level_of

STAGE_KEY = "derived"
TILE_DETAIL_PX = tilemesh.ALWAYS_PX  # what a tile nothing culls still ships (see beamng/tilelod)
PRODUCTS = {
    # product: (setting toggle, group, collision setting or None for paint, castShadows)
    "markings": ("markings", "Markings", "collision_paint", False),
    "areas": ("areas", "Areas", "collision_paint", False),
    # a 1 m plinth casts nothing anyone sees, and 641 never-culled tiles of it were in the
    # shadow pass (refactor Phase 10 survey §4)
    "skirts": ("skirts", "Skirts", "collision_skirts", False),
    "lanegeometry": ("lane_furniture", "LaneFurniture", "collision_furniture", True),
    # the pre-clipped ground at trenches / portals: it IS the ground there (the heightfield is
    # punched underneath), so it always collides and casts shadows
    "terraincut": ("terrain_cut", "TerrainCut", None, True),
}
# bake material name -> the level's ground material (written by the terrain stage beside its
# TerrainMaterials, from the same detail textures)
GROUND_MATERIAL_NAMES = {"ground:grass": "grass", "ground:dirt": "dirt", "ground:rock": "rock"}
TILE_RE = re.compile(r"^(?P<product>[a-z]+)_(?P<tx>-?\d+)_(?P<tz>-?\d+)\.glb$")
# Products whose tiles collide through a `Colmesh_` node cut from their COARSEST level rather
# than through `Visible Mesh Final`, which BeamNG rasterises at detail 0: a skirt is a foundation
# you can drive onto, not a surface you drive on, and at LOD 0 the skirts alone were 1.67 M
# collision triangles (refactor Phase 10 survey §4). The terrain cut IS the ground there and keeps
# the exact mesh.
COLMESH_PRODUCTS = ("skirts", "lanegeometry")
# products that are PAINT: their far levels stay exact subsets, never decimated (see `_tile_job`)
PAINT_PRODUCTS = ("markings", "areas")


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
    view_distance = (
        float(settings.get("view_distance") or 0) if settings.get("tile_lod", True) else None
    )
    workers = ctx.jobs or 1
    aliases = aliases_of(ctx.shared, level)
    shared_used = (ctx.shared if ctx.shared is not None else {}).setdefault("materials_used", set())
    summary = {}
    for product, (toggle, group, coll_setting, shadows) in PRODUCTS.items():
        # a skirt is a building's foundation band: with `buildings` off it would stand on
        # empty ground (B8)
        wanted = settings.get(toggle, True) and (
            product != "skirts" or settings.get("buildings", True)
        )
        if not wanted:
            # A PRODUCT TURNED OFF MUST ALSO HAVE ITS SHAPES REMOVED. `Level.write_objects`
            # already sweeps a group this build did not produce, so the `TSStatic`s go — but the
            # SHAPE FILES they pointed at are swept only by `sweep_orphans`, which this `continue`
            # skipped. `lane_furniture: false` therefore left 284 MiB of `art/shapes/lanegeometry`
            # on disk on any level that had once been built with it on, and said nothing (A8/P5).
            removed = dae.sweep_orphans(level.path("art", "shapes", product), set())
            if removed:
                progress.log(
                    f"derived: {product} is off — removed {removed} shape file(s) an "
                    f"earlier build left behind"
                )
            summary[product] = {"disabled": True, "shapesRemoved": removed}
            continue
        folder = os.path.join(ctx.derived_dir, product)
        if not os.path.isdir(folder):
            progress.log(f"derived: no {product} tiles baked")
            continue
        files = sorted(
            f for f in os.listdir(folder) if TILE_RE.match(f) and f.startswith(product + "_")
        )
        if ctx.region:
            files = [f for f in files if _tile_intersects(f, tile, ctx.region)]
        collide = collide_all and (
            coll_setting is None
            or bool(settings.get(coll_setting, coll_setting != "collision_paint"))
        )
        progress.begin(f"derived: {product}", total=len(files), unit="tiles")
        counts = {"written": 0, "tris": 0, "split_geoms": 0, "rewritten": 0}
        names = set()
        ladder_stats = tilemesh.new_stats()
        # `aliases` rides the pool initializer (`_init_worker`), not every tuple (D5)
        jobs = [
            (
                folder,
                name,
                product,
                tile,
                level.dir,
                level.name,
                detail_scale,
                view_distance,
            )
            for name in files
        ]

        def absorb(
            result,
            counts=counts,
            names=names,
            ladder_stats=ladder_stats,
            group=group,
            product=product,
            collide=collide,
            shadows=shadows,
        ):
            progress.advance(1)
            if result is None:
                return
            shared_used.update(result["used"])
            tilemesh.account_compact(ladder_stats, result["lod"])
            counts["split_geoms"] += result["extraGeoms"]
            counts["rewritten"] += 1 if result["changed"] else 0
            names.add(result["name"])
            if not collide:
                collision = "None"
            elif result["colmesh"]:
                collision = "Collision Mesh"
            else:
                collision = "Visible Mesh Final"
            obj = level_mod.tsstatic(
                result["name"],
                level.game_path("art", "shapes", product, result["name"] + ".dae"),
                (result["ox"], result["oy"], 0.0),
                frame.identity_matrix(),
                collision=collision,
            )
            if not shadows:
                obj["castShadows"] = False
            level.add([group], obj, key=f"derived:{result['name']}")
            counts["written"] += 1
            counts["tris"] += result["triangles"]

        # ORDERED so a tile lands in the level's object tree in the same place every build;
        # `absorb` advances progress itself, per tile.
        for result in parallel.each(
            _tile_job,
            jobs,
            workers,
            progress,
            ordered=True,
            chunksize=4,
            min_parallel=4,
            spawn=True,
            advance=False,
            initializer=_init_worker,
            initargs=(aliases,),
        ):
            absorb(result)
        # each product owns art/shapes/<product>/ (the folder is no longer cleared at build start)
        orphans = dae.sweep_orphans(level.path("art", "shapes", product), names)
        progress.end(
            f"{counts['written']} tiles ({counts['rewritten']} rewritten), {counts['tris']:,} triangles, collision "
            f"{'on' if collide else 'off'}, {orphans} orphans removed, "
            f"{counts['split_geoms']} extra geometries from the {daemesh.MAX_GEOMETRY_VERTS:,}-vertex cap"
        )
        summary[product] = {
            "tiles": counts["written"],
            "tilesRewritten": counts["rewritten"],
            "orphansRemoved": orphans,
            "triangles": counts["tris"],
            "collision": collide,
            "splitGeometries": counts["split_geoms"],
            "lod": tilemesh.ladder_summary(ladder_stats, detail_scale),
        }
    level.write_objects()
    return {"products": summary, "jobs": workers, "seconds": round(time.monotonic() - started, 1)}


_WORKER_ALIASES = {}


def _init_worker(aliases):
    """Pool initializer: the alias table every tile job binds materials against (D5)."""
    global _WORKER_ALIASES
    _WORKER_ALIASES = aliases


def _tile_job(args):
    """One baked tile → its `.dae`, in a worker process.

    Everything a tile needs is its own `.glb` plus the alias table, and it writes one file no
    other tile writes. It hands back only what the parent needs to place the `TSStatic` and to
    account for the ladder — never the mesh, and never the ladder's per-triangle masks.
    """
    folder, name, product, tile, level_dir, level_name, detail_scale, view_distance = args
    aliases = _WORKER_ALIASES
    m = TILE_RE.match(name)
    tx, tz = int(m.group("tx")), int(m.group("tz"))
    nodes = glb.parse_glb(os.path.join(folder, name))
    if not nodes:
        return None
    level = LevelPaths(level_dir, level_name)
    shared = {"materials_aliases": {"aliases": aliases}}
    ox, oy = (tx + 0.5) * tile, (tz + 0.5) * tile  # bundle x/z = BeamNG x/y
    parts = _tile_parts(nodes, ox, oy, level, shared)
    if not parts:
        return None
    shape_name = f"{product}_{tx}_{tz}"
    mesh = daemesh.group_by_material(daemesh.merge_meshes(shape_name, parts))
    # PAINT is never decimated: a marking or an area is thin alpha-masked quads, and the
    # vertex clusterer that serves a road tile defaces them - the A1 fix made the mid and far
    # bands VISIBLE (they used to vanish where the null tied a level's size), and what showed
    # there was clustered paint reading as black and colourless blotches (Naomi, 2026-08-31).
    # A paint tile's levels are the game's own culled subsets - a real ladder, nothing moved.
    levels, ladder = _tile_levels(
        shape_name, mesh, detail_scale, view_distance, tile, decimate=product not in PAINT_PRODUCTS
    )
    collision = _tile_colmesh(shape_name, levels) if product in COLMESH_PRODUCTS else ()
    path = level.path("art", "shapes", product, f"{shape_name}.dae")
    stats = dae.write_shape(
        path, dae.Shape(shape_name, levels, collision=collision, null_px=ladder["nullPx"])
    )
    return {
        "name": shape_name,
        "ox": ox,
        "oy": oy,
        "colmesh": bool(collision),
        "triangles": stats["triangles"],
        "changed": bool(stats.get("changed")),
        "lod": tilemesh.compact(levels, ladder),
        "extraGeoms": sum(len(ms) for _px, ms in levels) - len(levels),
        "used": sorted(shared.get("materials_used") or ()),
    }


def _tile_parts(nodes, ox, oy, level, shared):
    """Every non-empty node of a tile as a `merge_meshes` part, in BeamNG-local frame about the
    tile's origin, with its material runs bound to the level's names."""
    parts = []
    for node in nodes:
        if len(node["tris"]) == 0:
            continue
        mtx = numpy.asarray(node["matrix"], dtype=numpy.float64)
        v = numpy.asarray(node["verts"], dtype=numpy.float64) @ mtx[:3, :3].T + mtx[:3, 3]
        local, nn = daemesh.glb_to_local(v, node["normals"])
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
            bound = (
                _ground_mapto(level, base)
                if base.startswith("ground:")
                else mapto_name(shared, level, base)
            )
            runs.append((bound if min_lod is None else f"{bound}{MIN_LOD_MARK}{min_lod}", count))
        parts.append((local, nn, daemesh.glb_uvs(node["uvs"]), None, node["tris"], runs))
    return parts


def _tile_colmesh(shape_name, levels):
    """The tile's coarsest level as one `Colmesh_` mesh, or `()` when it has no triangles."""
    _px, meshes = levels[-1]
    parts = [(m.verts, None, None, None, m.tris, m.runs) for m in meshes]
    merged = daemesh.merge_meshes(f"Colmesh_{shape_name}", parts)
    return (merged,) if len(merged.tris) else ()


def _ground_mapto(level, mat):
    """The terrain stage's ground mesh material for a bake `ground:<name>` binding (any ground the
    heightfield paints — the terrain stage writes one mesh material per layer name)."""
    return f"{level.name}_ground_{GROUND_MATERIAL_NAMES.get(mat, mat.split(':', 1)[1])}"


def _tile_intersects(name, tile, region):
    m = TILE_RE.match(name)
    tx, tz = int(m.group("tx")), int(m.group("tz"))
    return region_rule.intersects_box(
        region, tx * tile, tz * tile, (tx + 1) * tile, (tz + 1) * tile
    )


# The five product toggles are read through a variable (`settings.get(toggle, True)`), so they
# are declared here by hand and caught at runtime by the recorder rather than by a source scan.
INPUTS = (*tuple(f"derived:{p}" for p in sorted(PRODUCTS)), "derived:materials.json")
SETTINGS = tuple(
    sorted(
        {toggle for toggle, _g, _c, _s in PRODUCTS.values()}
        | {c for _t, _g, c, _s in PRODUCTS.values() if c}
        | {
            "tile_size",
            "region",
            "collision_preset",
            "lod_detail_scale",
            "view_distance",
            "tile_lod",
            "area_conform_cm",
            "buildings",
        }
    )
)
OUTPUTS = ("art/shapes",)

# -- the ladder ------------------------------------------------------------------------------------
#
# Each LOD level is the set of triangles CS2 still draws at that distance, so a switch removes
# only what the game had already stopped drawing and no level can crack against another. The
# whole argument and every helper live in `beamng/tilelod.py`, shared with the networks stage.


def _tile_levels(shape_name, mesh, detail_scale, view_distance=None, tile=None, decimate=True):
    """`([(px, [Mesh]), ...], ladder)` for one merged tile."""
    return tilemesh.build_levels(
        dae,
        shape_name,
        mesh,
        detail_scale,
        lod.DEFAULT_LEVEL_OF_DETAIL,
        TILE_DETAIL_PX,
        view_distance=view_distance,
        targets=tilemesh.TILE_TARGETS if (view_distance and decimate) else None,
        tile=tile,
    )


# version 12: paint tiles (markings, areas) are never decimated - their far levels are the
# game's own culled subsets; the clustered paint read as black/colourless blotches once the A1
# fix made those bands visible (Naomi, 2026-08-31)
# version 11: skirts need `buildings` on as well as `skirts` (B8)
# version 10: a tile's `nulldetail` is kept UNDER its coarsest detail (`tilelod.tile_levels`);
# it sat `detail_scale` times above it wherever the game's own cull was inside the view
# distance, and Torque drew nothing there (validation pass 2026-08-31, A1)
STAGE = Stage(
    STAGE_KEY,
    "Markings, areas, skirts, furniture",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    shared_keys=("materials_used",),
    version=12,
)
