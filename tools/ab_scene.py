"""A/B gate: dump an imported Blender scene as text, so two imports can be diffed exactly.

    blender --background --python tools/ab_scene.py -- <out.txt>

Run it after an import (or on a saved `.blend`) and diff the two dumps. This exists because
the pixel gate cannot answer the question a refactor asks: rendering the *same* `.blend` twice
gives `max=1` (EEVEE is not bit-deterministic), so "the render is identical" is never proof.
The scene is: every object's world matrix, every mesh's vertex/loop/UV/attribute digest, every
material's node graph, and the colour-management state. A pure code move must leave all of it
byte-identical.
"""

import hashlib
import sys

import bpy
import numpy


def _digest(values):
    """A short stable hash of a float sequence, quantised so ULP noise does not show."""
    array = numpy.asarray(list(values), dtype=numpy.float64).round(6) + 0.0
    return hashlib.sha256(array.tobytes()).hexdigest()[:16]


def _bulk_digest(collection, attribute, width, dtype=numpy.float32):
    """`_digest` over a Blender collection property, read in one `foreach_get`.

    A full-map terrain import carries tens of millions of floats; a per-element Python loop
    over them takes longer than the import it is checking.
    """
    array = numpy.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(attribute, array)
    if dtype is not numpy.float32:
        return hashlib.sha256(array.tobytes()).hexdigest()[:16]
    return _digest_array(array)


def _digest_array(array):
    """`_digest` for an array that is already flat."""
    return hashlib.sha256(array.astype(numpy.float64).round(6).tobytes()).hexdigest()[:16]


def _mesh_lines(mesh):
    """One line per mesh datablock: counts plus digests of position, loops, UVs, colours."""
    line = (
        f"  verts={len(mesh.vertices)} polys={len(mesh.polygons)} loops={len(mesh.loops)}"
        f" co={_bulk_digest(mesh.vertices, 'co', 3)}"
        f" idx={_bulk_digest(mesh.loops, 'vertex_index', 1, numpy.int32)}"
    )
    for layer in mesh.uv_layers:
        line += f" uv[{layer.name}]={_bulk_digest(layer.uv, 'vector', 2)}"
    for layer in mesh.color_attributes:
        line += f" col[{layer.name}]={_bulk_digest(layer.data, 'color', 4)}"
    return line


def _object_lines(obj):
    """World matrix, data link, custom properties and material slots of one object."""
    matrix = [c for row in obj.matrix_world for c in row]
    lines = [f"object {obj.name} type={obj.type} matrix={_digest(matrix)}"]
    props = sorted(k for k in obj.keys() if not k.startswith("_"))
    for key in props:
        lines.append(f"  prop {key}={obj[key]!r}")
    if obj.instance_collection is not None:
        lines.append(f"  instances {obj.instance_collection.name} type={obj.instance_type}")
    for slot in obj.material_slots:
        lines.append(f"  slot {slot.name}")
    if obj.type == "MESH" and obj.data is not None:
        lines.append(f"  data {obj.data.name}")
    return lines


def _node_lines(material):
    """Every node's type, its non-default inputs and every link, in a stable order."""
    lines = [f"material {material.name} nodes={material.node_tree is not None}"]
    if material.node_tree is None:
        lines.append(f"  diffuse {_digest(material.diffuse_color)}")
        return lines
    for node in sorted(material.node_tree.nodes, key=lambda n: n.name):
        lines.append(f"  node {node.name} {node.bl_idname}")
        for socket in node.inputs:
            if socket.is_linked or not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            try:
                value = _digest(value)
            except TypeError:
                value = f"{value:.6f}" if isinstance(value, float) else repr(value)
            lines.append(f"    in {socket.identifier} {value}")
        if node.bl_idname == "ShaderNodeTexImage" and node.image is not None:
            lines.append(f"    image {node.image.name} cs={node.image.colorspace_settings.name}")
    links = sorted(
        f"    link {ln.from_node.name}.{ln.from_socket.identifier}"
        f" -> {ln.to_node.name}.{ln.to_socket.identifier}"
        for ln in material.node_tree.links
    )
    lines.extend(links)
    return lines


def dump(path):
    """Write the whole scene — objects, meshes, materials, colour management — to `path`."""
    lines = []
    scene = bpy.context.scene
    lines.append(f"view_transform {scene.view_settings.view_transform}")
    lines.append(f"look {scene.view_settings.look} exposure {scene.view_settings.exposure:.6f}")
    lines.append(f"display {scene.display_settings.display_device}")
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        lines.extend(_object_lines(obj))
    for mesh in sorted(bpy.data.meshes, key=lambda m: m.name):
        lines.append(f"mesh {mesh.name}")
        lines.append(_mesh_lines(mesh))
    for material in sorted(bpy.data.materials, key=lambda m: m.name):
        lines.extend(_node_lines(material))
    for light in sorted(bpy.data.lights, key=lambda ln: ln.name):
        lines.append(f"light {light.name} {light.type} energy={light.energy:.6f}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"ab_scene wrote {path}: {len(lines)} lines")


if __name__ == "__main__":
    dump(sys.argv[sys.argv.index("--") + 1 :][0])
