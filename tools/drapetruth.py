"""The drape-pick check's independent ground truth — the raw .glb ray target.

Support for `tools/test_drape_pick.py`, not a test itself. It parses the .glb and intersects
the vertical ray in numpy, in the bundle frame, sharing no code with the implementation it
checks — so agreement means the geometry agrees, not that one bug is compared against itself.
"""

import json
import struct

import numpy

_COMPONENT = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def glb_triangles(path):
    """Every triangle of a .glb in the BUNDLE frame, SEGMENT-LOCAL, as an (M, 3, 3) array.

    Only POSITION and the indices are read — this is a ray target, not an import.

    The frame conversion is just `diag(-1, 1, 1)`, the bundle's X mirror. Do NOT reach for the
    harness's `_UNMIRROR_UNIMPORT` here: that matrix is `diag(-1,1,1) · G⁻¹` and it is applied
    to a mesh Blender's glTF importer has ALREADY rewritten Y-up to Z-up, so its G⁻¹ half
    cancels an axis swap that has not happened to these raw vertices. Applying it anyway puts
    the segment's length on the height axis, and every vertical ray then misses.

    Positions are local to the segment origin — `place_network_mesh` supplies the translation
    from `networks.jsonl` `pos` — so the lane curve is what moves into this frame, not the mesh.
    """
    data = open(path, "rb").read()
    assert data[:4] == b"glTF", path
    pos, gltf, binary = 12, None, b""
    while pos + 8 <= len(data):
        length, tag = struct.unpack("<I4s", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + length]
        pos += 8 + length
        if tag == b"JSON":
            gltf = json.loads(payload.decode("utf-8"))
        elif tag == b"BIN\x00":
            binary = payload
    assert gltf is not None, path

    tris = []
    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    for root in scene.get("nodes", range(len(gltf.get("nodes", ())))):
        _node_triangles(gltf, binary, root, numpy.eye(4), tris)
    if not tris:
        return numpy.zeros((0, 3, 3))
    glb = numpy.concatenate(tris, axis=0)
    glb[:, :, 0] *= -1.0
    return glb


def _accessor(gltf, binary, index):
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    components = _COUNT[acc["type"]]
    fmt = _COMPONENT[acc["componentType"]]
    stride = view.get("byteStride") or struct.calcsize("<" + fmt) * components
    raw = numpy.frombuffer(binary, dtype=numpy.uint8, count=stride * acc["count"], offset=start)
    wide = raw.reshape(acc["count"], stride)[:, : struct.calcsize("<" + fmt) * components]
    return wide.copy().view(numpy.dtype("<" + fmt)).reshape(acc["count"], components)


def _node_matrix(node):
    if "matrix" in node:
        return numpy.asarray(node["matrix"], dtype=numpy.float64).reshape(4, 4).T
    matrix = numpy.eye(4)
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    matrix[:3, :3] = numpy.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    ) * numpy.asarray(node.get("scale", (1.0, 1.0, 1.0)), dtype=numpy.float64)
    matrix[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
    return matrix


def _node_triangles(gltf, binary, index, parent, tris):
    node = gltf["nodes"][index]
    world = parent @ _node_matrix(node)
    if "mesh" in node:
        for primitive in gltf["meshes"][node["mesh"]]["primitives"]:
            if primitive.get("mode", 4) != 4 or "POSITION" not in primitive["attributes"]:
                continue
            verts = _accessor(gltf, binary, primitive["attributes"]["POSITION"]).astype(
                numpy.float64
            )
            verts = verts @ world[:3, :3].T + world[:3, 3]
            index_list = (
                _accessor(gltf, binary, primitive["indices"]).reshape(-1)
                if "indices" in primitive
                else numpy.arange(len(verts))
            )
            tris.append(verts[index_list].reshape(-1, 3, 3))
    for child in node.get("children", ()):
        _node_triangles(gltf, binary, child, world, tris)


def hits_below(tris, x, z, top, bottom):
    """Every surface height under (x, z) in (bottom, top], highest first.

    A vertical ray reduces the intersection test to "does the triangle's XZ projection contain
    (x, z)", so this is a barycentric point-in-triangle plus the plane's height there —
    vectorised over every triangle at once.
    """
    ax, az = tris[:, 0, 0], tris[:, 0, 2]
    bx, bz = tris[:, 1, 0], tris[:, 1, 2]
    cx, cz = tris[:, 2, 0], tris[:, 2, 2]
    det = (bz - cz) * (ax - cx) + (cx - bx) * (az - cz)
    live = numpy.abs(det) > 1e-12  # a triangle seen edge-on has no top
    safe = numpy.where(live, det, 1.0)
    u = ((bz - cz) * (x - cx) + (cx - bx) * (z - cz)) / safe
    v = ((cz - az) * (x - cx) + (ax - cx) * (z - cz)) / safe
    w = 1.0 - u - v
    inside = live & (u >= -1e-9) & (v >= -1e-9) & (w >= -1e-9)
    if not inside.any():
        return numpy.zeros(0)
    y = (u * tris[:, 0, 1] + v * tris[:, 1, 1] + w * tris[:, 2, 1])[inside]
    y = y[(y > bottom) & (y <= top)]
    return numpy.sort(y)[::-1]
