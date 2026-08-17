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
"""

import math
import os
import re
import time

import numpy

from csiibundle import glb

from ..beamng import dae, frame
from . import Stage, level_of
from .materials import mapto_name

STAGE_KEY = "derived"
TILE_DETAIL_PX = 2      # detail sizes must be > 0 (BeamNG: "invalid detail levels"); 2 px = always drawn
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
        written = tris = split_geoms = 0
        for name in files:
            m = TILE_RE.match(name)
            tx, tz = int(m.group("tx")), int(m.group("tz"))
            nodes = glb.parse_glb(os.path.join(folder, name))
            progress.advance(1)
            if not nodes:
                continue
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
                runs = [(_ground_mapto(level, mat) if mat.startswith("ground:") else mapto_name(ctx.shared, level, mat), count)
                        for mat, count in node["material_runs"]]
                parts.append((local, nn, dae.glb_uvs(node["uvs"]), None, node["tris"], runs))
            if not parts:
                continue
            shape_name = f"{product}_{tx}_{tz}"
            mesh = dae.group_by_material(dae.merge_meshes(shape_name, parts))
            meshes = dae.split_mesh(mesh)                # <= MAX_GEOMETRY_VERTS per geometry (load #10 shards)
            split_geoms += len(meshes) - 1
            path = level.path("art", "shapes", product, f"{shape_name}.dae")
            stats = dae.write_shape(path, dae.Shape(shape_name, [(TILE_DETAIL_PX, meshes)]))
            obj = {"name": shape_name, "class": "TSStatic", "shapeName": level.game_path("art", "shapes", product, f"{shape_name}.dae"),
                   "position": [round(ox, 3), round(oy, 3), 0.0], "rotationMatrix": frame.identity_matrix(), "scale": [1, 1, 1],
                   "collisionType": "Visible Mesh Final" if collide else "None", "decalType": "None",
                   "useInstanceRenderData": True}
            if not shadows:
                obj["castShadows"] = False
            level.add([group], obj, key=f"derived:{shape_name}")
            written += 1
            tris += stats["triangles"]
            if written % 25 == 0:
                progress.check()
        progress.end(f"{written} tiles, {tris:,} triangles, collision {'on' if collide else 'off'}, "
                     f"{split_geoms} extra geometries from the {dae.MAX_GEOMETRY_VERTS:,}-vertex cap")
        summary[product] = {"tiles": written, "triangles": tris, "collision": collide, "splitGeometries": split_geoms}
    level.write_objects()
    return {"products": summary, "seconds": round(time.monotonic() - started, 1)}


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


STAGE = Stage(STAGE_KEY, "Markings, areas, skirts, furniture", enabled, units, run)
