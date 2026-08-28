"""Every `TSStatic` whose footprint meets the region, imported from its `.dae`."""

import os

import numpy
from cs2beamng.beamng import daeread

from . import levelread, log, meshes


def tiles_in_region(level_dir, objects, center, radius, products):
    cx, cy = center
    out = []
    for o in objects:
        if o.get("class") != "TSStatic" or not o.get("shapeName"):
            continue
        product = o["shapeName"].split("/")[-2] if "/" in o["shapeName"] else ""
        if products and product not in products:
            continue
        pos = [float(v) for v in o.get("position") or (0, 0, 0)]
        # tile shapes are written relative to their tile centre; a tile is 256 m (default) —
        # take everything whose centre lies within radius + 200 m and let the geometry decide
        if abs(pos[0] - cx) <= radius + 200 and abs(pos[1] - cy) <= radius + 200:
            out.append((o["name"], levelread.resolve(level_dir, o["shapeName"]), pos, product))
    return out


def import_tiles(tiles, materials, level_dir, center, radius):
    imported = 0
    for name, path, pos, product in tiles:
        if not os.path.isfile(path):
            log(f"missing shape {path}")
            continue
        try:
            parts = daeread.read_shape(path)
        except Exception as exc:
            log(f"{name}: dae read failed: {exc!r}")
            continue
        for node_name, verts, _normals, uvs, tris, runs, matrix in daeread.nearest_detail(parts):
            v = verts @ matrix[:3, :3].T + matrix[:3, 3] + numpy.asarray(pos)
            meshes.mesh_object(f"{name}:{node_name}", v, tris, runs, uvs, product)
        imported += 1
    log(f"{imported} shape tiles imported")
    return imported
