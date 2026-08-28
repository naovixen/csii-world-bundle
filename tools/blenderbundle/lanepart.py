"""The §5B v2 lane PARTS: loading one part .glb, and the two ways a tile of it reaches the scene.

A lane is a curve plus a list of parts stamped along it. A part that has to bend is baked into
the merged per-prefab mesh (`lanebuild`); a rigid tile is instanced instead, through one shared
datablock per part and a Geometry Nodes group that takes each tile's full 4x4 off a point
attribute. Both paths must produce the same triangles in the same winding, which is why the
axis swap here and the one in `lanebuild` are stated together in `load_source`'s docstring.

`csiibundle.lanes` is the reference implementation of the bend, `csiibundle.bezier` the curve
maths under it, and `csiibundle.decals` everything about markings. All three are bpy-free.
"""

import os

import bpy
import numpy
from csiibundle import decals, frames
from mathutils import Matrix

from . import glbimport, meshbuild


def load_source(asset_id, lane_mesh_dir, cache):
    """Import one part .glb once and reduce it to bundle-frame arrays plus its materials.

    The importer hands back Blender-local coordinates (glTF x, -z, y); the bundle frame is
    recovered as (-x, z, -y) of those — the writer's X mirror undone, axes restored. That map
    reverses the triangle's apparent winding (an odd number of axis negations/swaps,
    determinant -1). `build_lanes` applies the SAME determinant-(-1) map again later
    (`verts[:, [0, 2, 1]]`, converting the bent bundle-frame verts to Blender frame), so the
    two cancel: net effect is a proper rotation (a 180° turn about the vertical axis — see
    the frame comment in `blenderbundle.frame`). The triangle winding read straight off the
    glTF mesh here, with NO reversal, already matches what the old load-then-from_pydata
    double reversal produced (each reversal undid the other — checked directly against the
    old pipeline's final loop order, not just the determinant argument). Road markings,
    draped and lit from above, render black the moment this is wrong — the gorge/suburb A/B
    renders catch it immediately.
    """
    if asset_id in cache:
        return cache[asset_id]

    path = os.path.join(lane_mesh_dir, asset_id + ".glb")
    source = None
    if os.path.isfile(path):
        imported = glbimport.import_glb(path)
        for obj in imported:
            if obj.type != "MESH" or source is not None:
                continue
            source = _source_arrays(obj.data)
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)

    cache[asset_id] = source
    return source


def _source_arrays(mesh):
    """One imported part mesh as bundle-frame verts, triangles, UVs and per-triangle material."""
    count = len(mesh.vertices)
    co = numpy.empty(count * 3, dtype=numpy.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    verts = numpy.stack((-co[:, 0], co[:, 2], -co[:, 1]), axis=1)

    mesh.calc_loop_triangles()
    tri_count = len(mesh.loop_triangles)
    tri_verts = numpy.empty(tri_count * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", tri_verts)
    tris = tri_verts.reshape(-1, 3)

    slot_materials = list(mesh.materials) if mesh.materials else [None]
    tri_material = _triangle_materials(mesh, tri_count)
    uvs = _triangle_uvs(mesh, tri_count)

    quad = decals.quad_info(verts, tris, uvs)
    if quad is not None:
        quad["material"] = slot_materials[int(tri_material[0])] if tri_count else None
    return {
        "verts": verts,
        "tris": tris,
        "uvs": uvs,
        "tri_material": tri_material,
        "materials": slot_materials,
        "quad": quad,
    }


def _triangle_materials(mesh, tri_count):
    """Each loop triangle's material slot index, or all-zero when the mesh has no slots."""
    if not mesh.materials:
        return numpy.zeros(tri_count, dtype=numpy.int64)
    tri_poly = numpy.empty(tri_count, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("polygon_index", tri_poly)
    poly_material = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("material_index", poly_material)
    return poly_material[tri_poly]


def _triangle_uvs(mesh, tri_count):
    """Per-triangle-corner UVs as (M, 3, 2), zeros when the mesh carries no active layer."""
    uv_layer = mesh.uv_layers.active
    if uv_layer is None or not tri_count:
        return numpy.zeros((tri_count, 3, 2), dtype=numpy.float64)
    tri_loops = numpy.empty(tri_count * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("loops", tri_loops)
    all_uv = numpy.empty(len(mesh.loops) * 2, dtype=numpy.float64)
    uv_layer.data.foreach_get("uv", all_uv)
    return all_uv.reshape(-1, 2)[tri_loops].reshape(-1, 3, 2)


def object_arrays(blocks):
    """Concatenate one prefab's blocks into a single verts/tris/uvs/material_runs set, still
    in bundle frame, ready for the one Blender-frame axis swap and `meshbuild.from_arrays`."""
    counts = [len(block["verts"]) for block in blocks]
    offsets = numpy.concatenate(([0], numpy.cumsum(counts)))[:-1]
    verts = numpy.concatenate([block["verts"] for block in blocks], axis=0)
    tris = numpy.concatenate(
        [block["tris"] + offset for block, offset in zip(blocks, offsets)], axis=0
    )
    uvs = numpy.concatenate([block["uvs"].reshape(-1, 2) for block in blocks], axis=0)
    material_runs = [(block["material"], len(block["tris"])) for block in blocks]
    return verts, tris, uvs, material_runs


def part_object(asset_id, source, cache, parts_collection, degenerate_report):
    """One shared Blender object per part mesh, at rest in the Blender frame, for instancing.

    Built from the same bundle-frame arrays the baked path bends, through the same axis swap
    (`[:, [0, 2, 1]]`), so a rigid instance of this datablock and a baked tile of the same
    part are the same triangles in the same winding. Lives in an excluded collection: the
    depsgraph still evaluates it for the instancers' Object Info, but it never draws itself.
    """
    if asset_id in cache:
        return cache[asset_id]

    verts = source["verts"][:, [0, 2, 1]]
    tri_mat = source["tri_material"]
    order = numpy.argsort(tri_mat, kind="stable")
    tris = source["tris"][order]
    uvs = source["uvs"][order].reshape(-1, 2)
    material_runs = [
        (source["materials"][m], int((tri_mat == m).sum())) for m in numpy.unique(tri_mat)
    ]
    mesh = meshbuild.from_arrays(
        f"lanepart_{asset_id[:16]}",
        verts,
        tris,
        uv_layers={"UVMap": uvs},
        material_runs=material_runs,
        poly_size=3,
        smooth=False,
        source_geometry=True,
        degenerate_report=degenerate_report,
    )
    obj = bpy.data.objects.new(f"lanepart_{asset_id[:16]}", mesh)
    parts_collection.objects.link(obj)
    cache[asset_id] = obj
    return obj


def _instance_group(part_obj, cache):
    """One Geometry Nodes group per shared part object: Instance on Points of that part,
    each instance's full 4×4 taken from the point's `tile_matrix` attribute. The part is the
    Object Info node's own target (not a modifier input — object-socket modifier inputs are
    not assignable from Python on every Blender build), so all instancers of one part share
    one group and there are at most as many groups as part meshes."""
    if part_obj.name in cache:
        return cache[part_obj.name]
    group = bpy.data.node_groups.new(f"carto_tiles_{part_obj.name}", "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    n_in = group.nodes.new("NodeGroupInput")
    n_out = group.nodes.new("NodeGroupOutput")
    info = group.nodes.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = part_obj
    info.inputs["As Instance"].default_value = True
    points = group.nodes.new("GeometryNodeInstanceOnPoints")
    attr = group.nodes.new("GeometryNodeInputNamedAttribute")
    attr.data_type = "FLOAT4X4"
    attr.inputs["Name"].default_value = "tile_matrix"
    setm = group.nodes.new("GeometryNodeSetInstanceTransform")

    links = group.links
    links.new(n_in.outputs["Geometry"], points.inputs["Points"])
    links.new(info.outputs["Geometry"], points.inputs["Instance"])
    links.new(points.outputs["Instances"], setm.inputs["Instances"])
    links.new(attr.outputs["Attribute"], setm.inputs["Transform"])
    links.new(setm.outputs["Instances"], n_out.inputs["Geometry"])
    cache[part_obj.name] = group
    return group


def instancer_object(name, part_obj, matrices, group_cache):
    """A point-cloud mesh (one vertex per tile) carrying each tile's 4×4, instancing the
    shared part object through `_instance_group`. Point positions duplicate the matrix
    translations only so the object has sensible bounds; the transform node replaces the
    instance matrix wholesale.

    `matrices` arrive from `lanes.bend_part` in the BUNDLE frame and are conjugated here — the
    same swap `part_object` applies to the shared datablock's vertices, which is what
    makes an instanced tile and a baked tile the same triangles in the same place.
    """
    mats = numpy.asarray([frames.to_zup_transform(m) for m in matrices], dtype=numpy.float64)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(mats))
    mesh.vertices.foreach_set(
        "co", numpy.ascontiguousarray(mats[:, :3, 3], dtype=numpy.float32).ravel()
    )
    attr = mesh.attributes.new("tile_matrix", "FLOAT4X4", "POINT")
    for i, mat in enumerate(mats):
        attr.data[i].value = Matrix(mat.tolist())
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    modifier = obj.modifiers.new("tiles", "NODES")
    modifier.node_group = _instance_group(part_obj, group_cache)
    return obj
