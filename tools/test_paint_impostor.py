"""A prefab whose `.glb` carries the game's impostor level still names its materials.

A vanilla tree's authored LOD 1 is a POSITION-only card (`MeshFlags.Impostor`). Before
2026-08-29 `glb.parse_glb` refused the whole file for it, so the bake's paint pass saw no
materials for the tree, wrote no tinted variant, and every tinted forest type bound the
untinted palette-only material a forest item cannot colour - grey trees. This pins both halves:
the parser keeps the level-0 node, and `paint.asset_materials_job` names its materials.

Run: `python tools/test_paint_impostor.py`; pytest collects `gate()` (tools/conftest.py).
"""

import json
import os
import struct
import tempfile

import numpy
from csiibundle import glb
from csiibundle.bake import paint


def _glb_with_impostor(path):
    verts = numpy.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=numpy.float32)
    normals = numpy.array([[0, 0, 1]] * 3, dtype=numpy.float32)
    indices = numpy.array([0, 1, 2], dtype=numpy.uint32)
    blobs = [verts.tobytes(), normals.tobytes(), indices.tobytes()]
    views, offset = [], 0
    for blob in blobs:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(blob)})
        offset += len(blob)
    binary = b"".join(blobs)
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [{"name": "Tree_Mesh", "mesh": 0}, {"name": "Tree_Mesh_LOD1", "mesh": 1}],
        "meshes": [
            {
                "name": "Tree_Mesh",
                "primitives": [
                    {"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "material": 0}
                ],
            },
            {
                # the impostor: positions only, no normals, its own material
                "name": "Tree_Mesh_LOD1",
                "primitives": [{"attributes": {"POSITION": 0}, "indices": 2, "material": 1}],
            },
        ],
        "materials": [{"name": "LeafMat"}, {"name": "ImpostorMat"}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
                "min": [0, 0, 0],
                "max": [1, 1, 0],
            },
            {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": 3, "type": "SCALAR"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    payload = json.dumps(doc).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    binary += b"\0" * (-len(binary) % 4)
    body = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    body += struct.pack("<II", len(binary), 0x004E4942) + binary
    with open(path, "wb") as handle:
        handle.write(struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body)


def test_paint_names_the_materials_of_a_tree_with_an_impostor_level():
    with tempfile.TemporaryDirectory() as meshes:
        _glb_with_impostor(os.path.join(meshes, "Tree.glb"))
        nodes = glb.parse_glb(os.path.join(meshes, "Tree.glb"))
        assert nodes is not None, "the impostor level must not refuse the file"
        assert [n["name"] for n in nodes] == ["Tree_Mesh"]
        asset, names = paint.asset_materials_job((meshes, "Tree"))
        assert asset == "Tree"
        assert names == ["LeafMat"], names


def main():
    test_paint_names_the_materials_of_a_tree_with_an_impostor_level()
    print("  ok   paint names a tree's materials through its impostor level")
    return 0


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_paint_impostor.py`."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
