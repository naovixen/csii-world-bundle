"""The level's forest items — the A3 stage's whole output, which no `TSStatic` references.

Without them the oracle shows an empty city.
"""

import json
import os

import bpy
import numpy
from cs2beamng.beamng import daeread
from mathutils import Matrix

from . import levelread, log, meshes


def import_forest(level_dir, center, radius, limit):
    """Place the level's `forest/*.forest4.json` items - the A3 stage's whole output, which no
    `TSStatic` references and which the oracle would otherwise show as an empty city.

    Every item of one type shares ONE mesh datablock (Blender linked duplicates), so a region
    with 18k trees is 18k objects over a few hundred meshes rather than 18k meshes.
    """
    item_path = os.path.join(level_dir, "art", "forest", "managedItemData.json")
    forest_dir = os.path.join(level_dir, "forest")
    if not os.path.isfile(item_path) or not os.path.isdir(forest_dir):
        log("no forest in this level")
        return 0
    with open(item_path, encoding="utf-8") as handle:
        item_data = json.load(handle) or {}
    cx, cy = center
    cache = {}
    placed = skipped = 0
    for name in sorted(os.listdir(forest_dir)):
        if not name.endswith(".forest4.json"):
            continue
        with open(os.path.join(forest_dir, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                pos = item["pos"]
                if abs(pos[0] - cx) > radius or abs(pos[1] - cy) > radius:
                    continue
                if placed >= limit:
                    skipped += 1
                    continue
                meshes = _forest_meshes(cache, level_dir, item_data, item["type"])
                if not meshes:
                    continue
                scale = float(item.get("scale") or 1.0)
                # the stored nine floats are the item's own axes in groups of three
                # (`beamng.frame.basis_flat`), so the rotation matrix is their TRANSPOSE —
                # reading them row-major turns every item by the inverse of its own rotation,
                # which is what made this importer agree with a level that was wrong (A6.8)
                rot = numpy.asarray(item["rotationMatrix"], dtype=numpy.float64).reshape(3, 3).T
                for mesh, local in meshes:
                    obj = bpy.data.objects.new(f"{item['type']}:{placed}", mesh)
                    m = numpy.eye(4)
                    m[:3, :3] = rot @ local[:3, :3] * scale
                    m[:3, 3] = numpy.asarray(pos) + rot @ (local[:3, 3] * scale)
                    # a mathutils.Matrix, NOT a list of numpy rows: Blender takes the
                    # assignment silently and leaves the object at the origin
                    obj.matrix_world = Matrix([[float(v) for v in row] for row in m])
                    obj["cs2_product"] = "forest"
                    bpy.context.scene.collection.objects.link(obj)
                placed += 1
    log(f"{placed} forest items placed ({len(cache)} types, {skipped} beyond --forest-max {limit})")
    return placed


def _forest_meshes(cache, level_dir, item_data, type_name):
    """`[(mesh datablock, node matrix), ...]` for one forest type, read once and reused."""
    if type_name in cache:
        return cache[type_name]
    entry = item_data.get(type_name) or {}
    path = levelread.resolve(level_dir, entry.get("shapeFile"))
    out = []
    if path and os.path.isfile(path):
        try:
            for node_name, verts, _n, uvs, tris, runs, matrix in daeread.nearest_detail(
                daeread.read_shape(path)
            ):
                obj = meshes.mesh_object(
                    f"forestproto:{type_name}:{node_name}", verts, tris, runs, uvs, "forest"
                )
                mesh = obj.data
                bpy.data.objects.remove(obj)  # keep the datablock, drop the prototype
                out.append((mesh, numpy.asarray(matrix, dtype=numpy.float64)))
        except Exception as exc:
            log(f"forest type {type_name}: dae read failed: {exc!r}")
    cache[type_name] = out
    return out
