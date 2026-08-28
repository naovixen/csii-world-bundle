"""The `.ter` heightfield, with its holes and its per-sample material colour.

Terrain material colours are flat stand-ins per ground name — the oracle answers "where is the
ground and what layer is painted on it", not "what does the ground look like"; that question is
the bundle importer's.
"""

import bpy
import numpy
from cs2beamng.beamng import ter as ter_mod

from . import levelread, log

GROUND_COLOURS = {
    "grass": (0.18, 0.36, 0.10),
    "rock": (0.42, 0.40, 0.38),
    "dirt": (0.36, 0.26, 0.16),
    "roadbed": (0.22, 0.20, 0.18),
    "extra1": (0.5, 0.45, 0.3),
    "extra2": (0.4, 0.5, 0.3),
    "extra3": (0.5, 0.3, 0.3),
    "extra4": (0.3, 0.4, 0.5),
}


def _layer_materials(mesh, names):
    """One flat stand-in material per terrain layer, in the layer order the `.ter` records —
    so a face's material index IS its layer byte and the oracle shows coverage, not colour."""
    for name in names:
        m = bpy.data.materials.new(f"ter:{name}")
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get("Principled BSDF")
        col = next(
            (c for k, c in GROUND_COLOURS.items() if name == k or name.endswith("_" + k)),
            (0.5, 0.5, 0.5),
        )
        bsdf.inputs["Base Color"].default_value = (*col, 1.0)
        bsdf.inputs["Roughness"].default_value = 1.0
        mesh.materials.append(m)


def build_terrain(level_dir, objects, center, radius, ter_override=None):
    block = next((o for o in objects if o.get("class") == "TerrainBlock"), None)
    if block is None:
        log("no TerrainBlock in the level")
        return
    ter_path = ter_override or levelread.resolve(level_dir, block["terrainFile"])
    size, heights, layers, names = ter_mod.read_ter(ter_path)
    pos = [float(v) for v in block["position"]]
    cell = float(block.get("squareSize") or 1.0)
    max_height = float(block.get("maxHeight") or 2048.0)
    scale = max_height / 65536.0
    cx, cy = center
    c0 = max(0, int((cx - radius - pos[0]) / cell))
    c1 = min(size - 1, int((cx + radius - pos[0]) / cell) + 1)
    r0 = max(0, int((cy - radius - pos[1]) / cell))
    r1 = min(size - 1, int((cy + radius - pos[1]) / cell) + 1)
    if c1 <= c0 or r1 <= r0:
        log("region outside the terrain block")
        return
    hs = heights[r0 : r1 + 1, c0 : c1 + 1].astype(numpy.float64) * scale + pos[2]
    ls = layers[r0 : r1 + 1, c0 : c1 + 1]
    nr, nc = hs.shape
    xs = pos[0] + (numpy.arange(c0, c1 + 1) * cell)
    ys = pos[1] + (numpy.arange(r0, r1 + 1) * cell)
    verts = numpy.empty((nr * nc, 3), dtype=numpy.float64)
    verts[:, 0] = numpy.tile(xs, nr)
    verts[:, 1] = numpy.repeat(ys, nc)
    verts[:, 2] = hs.ravel()
    # a face's layer byte is the one at its south-west sample (BeamNG's convention: the square
    # starting at sample (r, c)); HOLE = 255 removes the square
    idx = numpy.arange(nr * nc).reshape(nr, nc)
    sw = idx[:-1, :-1]
    se = idx[:-1, 1:]
    nw = idx[1:, :-1]
    ne = idx[1:, 1:]
    keep = ls[:-1, :-1] != ter_mod.HOLE
    holes = int((~keep).sum())
    quads = numpy.stack([sw[keep], se[keep], ne[keep], nw[keep]], axis=1)
    face_layer = ls[:-1, :-1][keep]
    mesh = bpy.data.meshes.new("ter")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(len(quads) * 4)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", numpy.arange(len(quads)) * 4)
    mesh.polygons.foreach_set("loop_total", numpy.full(len(quads), 4))
    _layer_materials(mesh, names)
    mesh.polygons.foreach_set(
        "material_index", numpy.clip(face_layer, 0, max(0, len(names) - 1)).astype(numpy.int32)
    )
    mesh.update(calc_edges=True)
    mesh.validate()
    obj = bpy.data.objects.new("terrain", mesh)
    bpy.context.scene.collection.objects.link(obj)
    log(
        f"terrain: {nr}x{nc} samples, {len(quads):,} faces, {holes:,} holes in the region, materials {names}"
    )
