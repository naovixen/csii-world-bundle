"""One `.dae` node as a Blender mesh object, and the material slots it names.

Shared by the two things that place geometry — the `TSStatic` tiles and the forest items — so
both build the same mesh from the same reader output, which is what makes their positions
comparable.
"""

import re

import bpy
import numpy


def material(name):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
    return m


def mesh_object(name, verts, tris, runs, uvs, product):
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    n = len(tris)
    mesh.loops.add(n * 3)
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(tris, dtype=numpy.int32).ravel())
    mesh.polygons.add(n)
    mesh.polygons.foreach_set("loop_start", numpy.arange(n, dtype=numpy.int32) * 3)
    mesh.polygons.foreach_set("loop_total", numpy.full(n, 3, dtype=numpy.int32))
    mat_index = numpy.zeros(n, dtype=numpy.int32)
    start = 0
    slot = {}
    for target, count in runs:
        # material target id "<name>-material" -> the level's mapTo name
        mname = re.sub(r"-material$", "", target)
        if mname not in slot:
            slot[mname] = len(mesh.materials)
            mesh.materials.append(material(mname))
        mat_index[start : start + count] = slot[mname]
        start += count
    mesh.polygons.foreach_set("material_index", mat_index)
    if uvs is not None and len(uvs) == len(verts):
        layer = mesh.uv_layers.new(name="UVMap")
        uv_loops = numpy.asarray(uvs, dtype=numpy.float32)[numpy.asarray(tris).ravel()]
        layer.data.foreach_set("uv", uv_loops.ravel())
    mesh.update(calc_edges=True)
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    obj["cs2_product"] = product
    bpy.context.scene.collection.objects.link(obj)
    return obj
